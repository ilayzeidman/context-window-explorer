"""Local viewer for Claude Code session files: what is on disk vs what the model sees.

  python ctxview.py [latest | <session-id prefix> | <path.jsonl>] [--port 8765] [--at LINE] [--no-open]

--at LINE opens the timeline at that 0-based line of the session file (also ?at=LINE in the URL).

Serves index.html and a read-only JSON API on 127.0.0.1. Only files under ~/.claude/projects are readable
(override the root with env CLAUDE_PROJECTS_DIR).
"""
import hashlib
import json
import os
import re
import sys
import threading
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects").resolve()
HERE = Path(__file__).resolve().parent
SENT = {"user", "assistant", "attachment", "system"}
# record types written for the app (resume, titles, undo, cost), never sent to the model
BOOK = {"ai-title", "mode", "permission-mode", "atis-latch", "last-prompt", "queue-operation", "file-history-snapshot",
        "file-history-delta", "pr-link", "cost-state", "summary", "custom-title", "tag"}
ATT_NAMES = {"instructions": "CLAUDE.md + MEMORY.md", "nested_memory": "nested CLAUDE.md", "skill_listing": "skill listing",
             "invoked_skills": "skill bodies (re-injected)", "file": "file re-read", "compact_file_reference": "file (path only)",
             "edited_text_file": "file changed on disk", "deferred_tools_delta": "deferred tool names",
             "agent_listing_delta": "subagent types", "mcp_instructions_delta": "MCP instructions", "hook_success": "hook output",
             "hook_additional_context": "hook context", "date": "date", "environment": "environment", "model": "model line"}
ATT_EV = {"nested_memory", "instructions", "skill_listing", "invoked_skills"}
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---- copied from the reference ctx.py (keep in step so the numbers match) ----

def slug(p):
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def resolve(arg):
    if arg and arg.endswith(".jsonl"):
        return Path(arg)
    if arg and arg != "latest":
        hits = sorted(ROOT.glob(f"*/{arg}*.jsonl"), key=os.path.getmtime)
        if not hits:
            sys.exit(f"no session starting with {arg} under {ROOT}")
        return hits[-1]
    here = sorted((ROOT / slug(Path.cwd())).glob("*.jsonl"), key=os.path.getmtime)
    return here[-1] if here else sorted(ROOT.glob("*/*.jsonl"), key=os.path.getmtime)[-1]


def load(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            r["_i"], r["_n"] = i, len(line)
            out.append(r)
    return out


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "thinking":
            parts.append(b.get("thinking", ""))
        elif t == "tool_use":
            parts.append(json.dumps(b.get("input", {}), ensure_ascii=False))
        elif t == "tool_result":
            parts.append(text_of(b.get("content")))
        elif t == "image":
            parts.append("x" * 6400)  # an image is roughly 1,600 tokens
    return "\n".join(parts)


def tokens(r):
    if r["type"] == "attachment":
        return sum(len(x.get("content", "")) for x in r.get("rendered") or []) // 4
    if r["type"] == "system":
        return len(str(r.get("content", ""))) // 4
    return len(text_of((r.get("message") or {}).get("content"))) // 4


def window_chain(recs):
    """the records the next request is built from: from the newest record back through parentUuid to the last compaction"""
    by = {r["uuid"]: r for r in recs if r.get("uuid") and not r.get("isSidechain")}
    leaf = next((r for r in reversed(recs) if r.get("uuid") in by and r["type"] in SENT), None)
    chain = []
    while leaf:
        chain.append(leaf)
        if leaf.get("subtype") == "compact_boundary":
            break
        leaf = by.get(leaf.get("parentUuid"))
    return chain[::-1]


def in_window(r):
    if r["type"] == "attachment":
        return bool(r.get("rendered"))
    if r["type"] == "system":
        return False  # turn_duration, local_command, the boundary marker itself: shown to you, not to the model
    # the compact summary is flagged transcript-only for the UI, but it is what the model reads in place of the history
    return r["type"] in ("user", "assistant") and (r.get("isCompactSummary") or not r.get("isVisibleInTranscriptOnly"))


def kind(r, names):
    t = r["type"]
    if t == "attachment":
        a = r["attachment"].get("type")
        return "reminder: " + ATT_NAMES.get(a, a)
    if t == "system":
        return "system/" + str(r.get("subtype"))
    m = r.get("message") or {}
    c = m.get("content")
    if t == "user":
        if r.get("isCompactSummary"):
            return "compact summary"
        if isinstance(c, list) and c and c[0].get("type") == "tool_result":
            res = text_of(c[0].get("content"))
            name = names.get(c[0].get("tool_use_id"), "tool")
            return f"tool result: {name}" + (" (persisted, preview only)" if "<persisted-output>" in res else "")
        s = c if isinstance(c, str) else text_of(c)
        if "<command-name>" in s:
            return "slash command"
        if "<task-notification>" in s:
            return "subagent result"
        return "you"
    if isinstance(c, list) and c:
        b = c[-1]
        if b.get("type") == "tool_use":
            return f"tool call: {b.get('name')}"
        if b.get("type") == "thinking":
            return "thinking"
    return "Claude"


def preview(r, n=70):
    if r["type"] == "attachment":
        a = r["attachment"]
        s = a.get("filename") or a.get("path") or ", ".join(f.get("path", "") for f in a.get("files", [])) or a.get("type")
    elif r["type"] == "system":
        s = r.get("content") or r.get("subtype")
    else:
        s = text_of((r.get("message") or {}).get("content"))
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:n] + ("..." if len(s) > n else "")


def tool_names(recs):
    names = {}
    for r in recs:
        if r["type"] == "assistant":
            for b in (r.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
    return names


def subagents(path):  # cmd_subagents, returning rows
    out = []
    for f in sorted((path.parent / path.stem / "subagents").glob("*.jsonl"), key=os.path.getmtime):
        mf = f.parent / (f.stem + ".meta.json")
        meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
        rs = load(f)
        start = [r["attachment"]["type"] for r in rs[:40] if r["type"] == "attachment" and r.get("rendered")]
        files = next((r["attachment"].get("files") for r in rs[:40] if (r.get("attachment") or {}).get("type") == "instructions"), [])
        types = {x.get("type") for x in files}
        loaded = ", ".join(sorted(types)) or "no CLAUDE.md"
        u = next((r["message"]["usage"] for r in reversed(rs) if r["type"] == "assistant" and (r.get("message") or {}).get("usage")), {})
        peak = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        last = next((r for r in reversed(rs) if r["type"] == "assistant"), None)
        back = len(text_of(last["message"]["content"])) // 4 if last else 0
        first_a = next((k for k, r in enumerate(rs) if r["type"] == "assistant"), len(rs))
        out.append({"path": str(f), "agentType": meta.get("agentType"), "description": meta.get("description", ""),
                    "toolUseId": meta.get("toolUseId"), "records": len(rs), "peak": peak, "back": back, "loaded": loaded,
                    "memory": "AutoMem" in types, "reminders": sorted(set(start)),
                    "fork": sum(r["type"] in ("user", "assistant") for r in rs[:first_a]) > 20})
    return out


def compacts(recs):  # cmd_compacts, returning rows; recs only needs the 40 records after each boundary in full
    out = []
    for k, r in enumerate(recs):
        if r.get("subtype") != "compact_boundary":
            continue
        m = r.get("compactMetadata", {})
        after = Counter()
        for q in recs[k + 1:k + 40]:
            if q["type"] == "assistant":
                break
            if q["type"] not in SENT or not in_window(q):
                continue  # ctx.py lists these too, but prompt_snapshot and bookkeeping are never sent
            after[kind(q, {}) if q["type"] != "attachment" else ATT_NAMES.get(q["attachment"].get("type"), q["attachment"].get("type"))] += 1
        kept = len((m.get("preservedMessages") or {}).get("uuids", []))
        out.append({"i": r["_i"], "trigger": m.get("trigger"), "pre": m.get("preTokens", 0), "post": m.get("postTokens", 0),
                    "s": round(m.get("durationMs", 0) / 1000, 1), "kept": kept, "reinjected": [[a, b] for a, b in after.items()]})
    return out


# ---- the real request: Claude Code 2.1.325+ writes a copy of every API request into the session file ----

CATS = ("system", "tools", "tool_additions", "instructions", "skills", "reminders", "messages", "tool_results", "summary")
REQ_KIND = {"api-request-shape": "request copy: shape", "api-request-blob": "request copy: blob", "api-request": "request copy: request"}


def mhash(msg):
    return hashlib.sha256(json.dumps(msg, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def head_of(s):  # how a rendered attachment is found inside a request block: its first 120 chars, wrapper removed
    return s.strip().removeprefix("<system-reminder>").removesuffix("</system-reminder>").strip()[:120]


def scrub(x):  # image data replaced, the rest as is
    if isinstance(x, dict):
        return {k: "[image]" if k == "source" and x.get("type") == "image" else scrub(v) for k, v in x.items()}
    return [scrub(v) for v in x] if isinstance(x, list) else x


def nimg(x):
    if isinstance(x, dict):
        return (x.get("type") == "image") + sum(nimg(v) for v in x.values())
    return sum(nimg(v) for v in x) if isinstance(x, list) else 0


def btext(b):  # /api/block: text blocks as text, the rest as JSON without image data
    return b.get("text") or "" if b.get("type") == "text" else json.dumps(scrub(b), ensure_ascii=False, indent=1)


def plain(b):  # the counted part of a block: text, thinking without its signature, tool input; never image data
    t = b.get("type")
    if t in ("text", "thinking"):
        return b.get(t) or ""
    if t == "image":
        return ""
    if t == "tool_use":
        return str(b.get("name")) + json.dumps(b.get("input"), ensure_ascii=False)
    if t == "tool_result":
        co = b.get("content")
        return co if isinstance(co, str) else "".join(plain(x) for x in co or [] if isinstance(x, dict))
    return json.dumps(scrub(b), ensure_ascii=False)


def spans(s, heads):
    """a system-role text holds many reminders back to back: cut it where each rendered attachment starts"""
    hits = sorted((at, cat, i) for h, (cat, i) in heads.items() if (at := s.find(h)) >= 0)
    out, pos, cur = [], 0, ("reminders", None)
    for at, cat, i in hits + [(len(s), None, None)]:
        if at > pos:
            out.append([cur[0], at - pos, cur[1]])
        pos, cur = at, (cat, i)
    return out or [["reminders", 0, None]]


def blocks_of(msg, heads, summary):
    role, out = msg.get("role"), []
    co = msg.get("content")
    for b in co if isinstance(co, list) else [{"type": "text", "text": co}] if isinstance(co, str) else []:
        b = b if isinstance(b, dict) else {"type": "text", "text": str(b)}
        t, s = b.get("type"), plain(b)
        if t == "text" and role == "system":
            split = spans(s, heads)
        else:
            cat, src = {"image": "images", "tool_result": "tool_results", "tool_addition": "tool_additions"}.get(t, "messages"), None
            if t == "text" and role == "user":
                cat, src = heads.get(head_of(s)) or ("summary" if summary else "skills" if s.startswith("Base directory for this skill")
                                                     else "messages", None)
            split = [[cat, len(s), src]]
        out.append({"type": t, "chars": len(s), "cat": max(split, key=lambda x: x[1])[0], "head": (s or btext(b))[:80],
                    "split": split, "images": nimg(b)})
    return out


def message(p, c, h):
    """(message, from, k) for a tail hash: a blob, the response to a request, a user record, or missing"""
    if h in c["blobs"]:
        k = c["blobs"][h]
        return full(p, c, k).get("message") or {}, "blob", k
    if h in c["ah"]:
        ks = c["ah"][h]
        return {"role": "assistant", "content": [b for k in ks for b in full(p, c, k)["message"].get("content") or []]}, "response", ks[0]
    if h in c["urec"]:
        k = c["urec"][h]
        co = full(p, c, k)["message"].get("content")
        return {"role": "user", "content": co if isinstance(co, list) else [{"type": "text", "text": co}]}, "record", k
    return None, "missing", None


def shape(p, c, sh):
    if sh not in c["shapes"]:
        return None
    if sh not in c["sm"]:
        k = c["shapes"][sh]
        s = full(p, c, k).get("shape") or {}
        c["sm"][sh] = {"i": c["recs"][k]["i"], "system": [(len(plain(b)), plain(b)[:80]) for b in s.get("system") or []],
                       "tools": [(b.get("name"), len(json.dumps(b, ensure_ascii=False))) for b in s.get("tools") or []]}
    return c["sm"][sh]


def apportion(chars_by, images, total, cpt):
    """parts = chars / cpt, images 1,600 each; largest remainder so the parts sum exactly to the measured total"""
    raw = {k: v / cpt for k, v in chars_by.items()}
    raw["images"] = 1600 * images
    if total is None:
        return {k: round(v) for k, v in raw.items()}
    out = {k: int(v) for k, v in raw.items()}
    # ponytail: assumes total > 1600*images; a request that is nearly all images would not sum exactly
    for k in sorted(raw, key=lambda k: out[k] - raw[k])[:total - sum(out.values())]:
        out[k] += 1
    return out


def requests(p, c):
    """every api-request in the file rebuilt: messages(req) = messages(req.base)[:keep] + resolve(tail). Memo per file size."""
    if c["built"][0] == c["off"]:
        return c["built"][1]
    for key, ks in c["groups"].items():  # a response: records sharing (requestRef, message.id), in apiBlockIndex order
        if c["ahk"].get(key, (0,))[0] != len(ks):
            ks = [k for _, k in sorted(ks)]
            c["ahk"][key] = (len(ks), mhash({"role": "assistant", "content": [b for k in ks for b in full(p, c, k)["message"].get("content") or []]}), ks)
    c["ah"] = {h: ks for _, h, ks in c["ahk"].values()}
    out, by_id, prev = [], {}, 4.0
    for n, k in enumerate(c["rq"]):
        r = full(p, c, k)
        keep = r.get("keep") or 0
        base = by_id.get(r.get("base"))
        msgs = base[1]["msgs"][:keep] if base and keep else []
        msgs = msgs + [(None, "missing", None)] * (keep - len(msgs))
        for h in r.get("tail") or []:
            frm = "blob" if h in c["blobs"] else "response" if h in c["ah"] else "record" if h in c["urec"] else "missing"
            msgs.append((h, frm, {"blob": c["blobs"], "response": c["ah"], "record": c["urec"]}.get(frm, {}).get(h)))
        chars_by, images = dict.fromkeys(CATS, 0), 0
        sh = shape(p, c, r.get("shapeHash"))
        if sh:
            chars_by["system"] = sum(x for x, _ in sh["system"])
            chars_by["tools"] = sum(x for _, x in sh["tools"])
        for h, frm, _ in msgs:
            if h and c["mc"].get(h, (-1,))[0] != len(c["heads"]):
                m = message(p, c, h)[0] or {}
                c["mc"][h] = (len(c["heads"]), m.get("role"), blocks_of(m, c["heads"], h in c["summ"]))
            for b in c["mc"][h][2] if h else []:
                images += b["images"]
                for cat, x, _ in b["split"]:
                    if cat != "images":
                        chars_by[cat] += x
        rk = c["resp"].get(r.get("id"))
        u = c["recs"][rk]["usage"] if rk is not None else None
        total = u["in"] + u["cr"] + u["cw"] if u else None
        chars = sum(chars_by.values())
        cpt = chars / (total - 1600 * images) if total and chars and total > 1600 * images else prev
        prev = cpt if total else prev
        row = {"n": n, "i": c["recs"][k]["i"], "id": r.get("id"), "ts": r.get("timestamp"), "resp_i": c["recs"][rk]["i"] if rk is not None else None,
               "total": total, "chars": chars, "images": images, "cpt": round(cpt, 3), "keep": keep, "msgs": len(msgs),
               "missing": sum(f == "missing" for _, f, _ in msgs), "tokens": apportion(chars_by, images, total, cpt), "chars_by": chars_by}
        x = {"msgs": msgs, "shape": r.get("shapeHash"), "model": (r.get("params") or {}).get("model"),
             "cache": {tuple(cc.get("path") or []): " ".join(v for v in ((cc.get("value") or {}).get("ttl") or "5m", (cc.get("value") or {}).get("scope")) if v)
                       for cc in r.get("cacheControl") or []}}
        by_id[r.get("id")] = (row, x)
        out.append((row, x))
    c["built"] = (c["off"], out)
    return out


def request(p, c, n):
    row, x = requests(p, c)[n]
    sh, ca = shape(p, c, x["shape"]) or {"i": None, "system": [], "tools": []}, x["cache"]
    return {"n": n, "i": row["i"], "model": x["model"], "cpt": row["cpt"], "total": row["total"], "images": row["images"],
            "system": [{"ref": f"system.{j}", "chars": ch, "cat": "system", "cache": ca.get(("system", j)), "head": hd, "src": sh["i"]}
                       for j, (ch, hd) in enumerate(sh["system"])],
            "tools": [{"ref": f"tools.{j}", "name": nm, "chars": ch, "cache": ca.get(("tools", j)), "src": sh["i"]} for j, (nm, ch) in enumerate(sh["tools"])],
            "messages": [{"idx": m, "role": c["mc"][h][1] if h else None, "from": frm, "new": m >= row["keep"],
                          "src": c["recs"][k if isinstance(k, int) else k[0]]["i"] if k is not None else None,
                          "blocks": [dict(b, ref=f"messages.{m}.content.{j}", cache=ca.get(("messages", m, "content", j)))
                                     for j, b in enumerate(c["mc"][h][2] if h else [])]}
                         for m, (h, frm, k) in enumerate(x["msgs"])]}


def block(p, c, n, ref):
    """full text of one block of request n; KeyError/IndexError/ValueError when the ref does not exist"""
    _, x = requests(p, c)[n]
    a = ref.split(".")
    if not all(v.isdigit() for v in a[1::2]):
        raise KeyError(ref)
    if a[0] in ("system", "tools") and len(a) == 2:
        return btext((full(p, c, c["shapes"][x["shape"]]).get("shape") or {})[a[0]][int(a[1])])
    if a[0] == "messages" and len(a) == 4 and a[2] == "content":
        m = message(p, c, x["msgs"][int(a[1])][0])[0]
        return btext(m["content"][int(a[3])])
    raise KeyError(ref)


# ---- per-file cache with incremental tail reads ----

CACHE, LOCK = {}, threading.Lock()  # ponytail: one global lock, per-path locks if many viewers poll big files at once


ROOTS = [ROOT, Path(os.path.abspath(os.environ.get("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects"))]  # main() adds the folder of a session file named on the command line


def under(p):
    # lexical check first: resolve() on a UNC path would reach out over SMB before we could refuse it
    if not isinstance(p, str) or p[:2] in ("\\\\", "//") or not any(Path(os.path.abspath(p)).is_relative_to(r) for r in ROOTS):
        return None
    try:
        q = Path(p).resolve()
    except (OSError, ValueError):
        return None
    return q if any(q.is_relative_to(r) for r in ROOTS) else None


def usage(u):
    cc = u.get("cache_creation") or {}
    return {"in": u.get("input_tokens", 0), "cr": u.get("cache_read_input_tokens", 0), "cw": u.get("cache_creation_input_tokens", 0),
            "out": u.get("output_tokens", 0), "cw1h": cc.get("ephemeral_1h_input_tokens", 0), "cw5m": cc.get("ephemeral_5m_input_tokens", 0)}


def agent_meta(sdir, aid):
    mf = sdir / f"agent-{aid}.meta.json"
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def slim(r, names, path, by_tool):
    t, i = r["type"], r["_i"]
    a = (r.get("attachment") or {}) if t == "attachment" else {}
    at = a.get("type")
    m = r.get("message") or {}
    c = m.get("content")
    k = kind(r, names) if t in SENT else REQ_KIND.get(t, t)
    ev, cmd, pers, link, skill, model, cat, sread = [], None, None, None, None, None, None, None
    sdir = path.parent / path.stem / "subagents"
    if t == "user":
        s = c if isinstance(c, str) else text_of(c)
        hit = re.search(r"<command-name>/?([^<]+)</command-name>", s)
        cmd = "/" + hit.group(1).strip() if hit else None
        ev += {"/clear": ["clear"], "/model": ["model"], "/handoff": ["handoff"]}.get(cmd, [])
        tur = r.get("toolUseResult") if isinstance(r.get("toolUseResult"), dict) else {}
        if isinstance(c, list) and c and c[0].get("type") == "tool_result":
            res = text_of(c[0].get("content"))
            if "<persisted-output>" in res:
                size = tur.get("persistedOutputSize")
                kb = re.search(r"([\d.]+)\s*KB", res)
                pp = tur.get("persistedOutputPath")
                pp = under(pp) if pp else None
                pers = {"kb": round(size / 1024, 1) if size else (float(kb.group(1)) if kb else None),
                        "path": str(pp) if pp else None, "tok": size // 4 if size else None}
                ev.append("persisted")
        if tur.get("agentId"):
            aid = str(tur["agentId"])
            meta = agent_meta(sdir, aid)
            f = sdir / f"agent-{aid}.jsonl"
            link = {"id": aid, "path": str(f) if f.exists() else None, "agentType": meta.get("agentType"), "description": meta.get("description")}
        cat = "summary" if r.get("isCompactSummary") else "tool_results" if k.startswith("tool result") else \
            "skills" if r.get("isMeta") and s.startswith("Base directory for this skill") else "messages"
    elif t == "assistant":
        cat = "messages"
        model = m.get("model") if m.get("model") != "<synthetic>" else None
        for b in c if isinstance(c, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                # a SKILL.md read as a file: the body comes back as a tool result, not as an invoked skill
                hit = re.search(r"\b(?:cat|head|tail|sed|less|more|bat|awk|grep|type|Get-Content)\b[^|;&\n]*?([^\s/\\'\"*]+)[/\\]SKILL\.md", str(inp.get("command") or "")) \
                    if b.get("name") == "Bash" else re.search(r"([^/\\]+)[/\\]SKILL\.md$", str(inp.get("file_path") or "")) if b.get("name") == "Read" else None
                if b.get("name") == "Skill":
                    skill = inp.get("skill")
                elif hit:
                    sread = hit.group(1)
                elif b.get("name") in ("Agent", "Task") and b.get("id") in by_tool:
                    aid, meta = by_tool[b["id"]]
                    link = {"id": aid, "path": str(sdir / f"agent-{aid}.jsonl"), "agentType": meta.get("agentType"),
                            "description": meta.get("description")}
        if skill:
            ev.append("skill")
        elif sread:
            skill = sread
            ev.append("skill_read")
    elif t == "attachment":
        cat = "instructions" if at in ("instructions", "nested_memory") else "skills" if at in ("skill_listing", "invoked_skills") \
            else "reminders" if r.get("rendered") else None
        if at == "model":
            model = (a.get("identity") or {}).get("modelId")
        if at == "invoked_skills":
            skill = ", ".join(str(x.get("name")) for x in a.get("skills") or [] if isinstance(x, dict)) or None
        ev += [at] if at in ATT_EV else ["edited_file"] if at == "edited_text_file" else []
    if link:
        ev.append("subagent")
    cm = None
    if r.get("subtype") == "compact_boundary":
        x = r.get("compactMetadata") or {}
        uu = (x.get("preservedMessages") or {}).get("uuids") or []
        cm = {"trigger": x.get("trigger"), "pre": x.get("preTokens"), "post": x.get("postTokens"), "ms": x.get("durationMs"),
              "kept": len(uu), "keptUuids": uu,
              "anchor": (x.get("preservedSegment") or {}).get("anchorUuid") or (x.get("preservedMessages") or {}).get("anchorUuid")}
        ev.append("compact")
    u = m.get("usage") if t == "assistant" else None
    return {"i": i, "type": t, "subtype": r.get("subtype"), "att": at, "uuid": r.get("uuid"), "parent": r.get("parentUuid"),
            "side": bool(r.get("isSidechain")), "rendered": bool(r.get("rendered")), "summary": bool(r.get("isCompactSummary")),
            "tonly": bool(r.get("isVisibleInTranscriptOnly")), "tok": tokens(r),
            "bytes": r["_n"], "kind": k, "cat": cat, "preview": preview(r, 120), "ts": r.get("timestamp"),
            "usage": usage(u) if u else None, "model": model, "cmd": cmd, "cm": cm, "persisted": pers, "agentLink": link,
            "skill": skill, "ev": ev, "initial": a.get("isInitial")}  # initial: False on a skill_listing that only adds skills


def index(c, r, k):
    """what requests() needs to rebuild the real request later, without keeping record bodies in memory"""
    t, m = r["type"], r.get("message") if isinstance(r.get("message"), dict) else {}
    if t == "api-request-shape":
        c["shapes"][r.get("shapeHash")] = k
    elif t == "api-request-blob":
        c["blobs"][r.get("hash")] = k
    elif t == "api-request":
        c["rq"].append(k)
    elif t == "user" and m:
        co = m.get("content")
        h = mhash({"role": "user", "content": co if isinstance(co, list) else [{"type": "text", "text": co}]})
        c["urec"][h] = k
        if r.get("isCompactSummary"):
            c["summ"].add(h)
    elif t == "assistant" and r.get("requestRef"):
        c["groups"].setdefault((r["requestRef"], m.get("id")), []).append((r.get("apiBlockIndex") or 0, k))
        c["resp"].setdefault(r["requestRef"], k)
    elif t == "attachment":
        for x in r.get("rendered") or []:
            h = head_of(str(x.get("content", ""))) if isinstance(x, dict) else ""
            if h:
                c["heads"][h] = (c["recs"][k]["cat"], r["_i"])


def parse(path):
    """bring the cache for path up to date: read only complete lines appended since last time, restart if the file shrank"""
    size = path.stat().st_size
    with open(path, "rb") as f:
        first = f.read(256)  # a rewritten file of the same or larger size starts differently
    c = CACHE.get(path)
    if not c or size < c["off"] or c["first"] != first[:len(c["first"])]:
        c = CACHE[path] = {"off": 0, "n": 0, "names": {}, "recs": [], "offs": [], "k": {}, "sid": None, "prev": None, "first": first,
                         "shapes": {}, "blobs": {}, "rq": [], "urec": {}, "summ": set(), "groups": {}, "resp": {}, "heads": {},
                         "ahk": {}, "ah": {}, "mc": {}, "sm": {}, "built": (None, [])}
    c["first"] = first
    if size == c["off"]:
        return c
    with open(path, "rb") as f:
        f.seek(c["off"])
        data = f.read(size - c["off"])
    end = data.rfind(b"\n") + 1  # a half-written last line waits for the next call
    by_tool = {}
    sdir = path.parent / path.stem / "subagents"
    for mf in sdir.glob("*.meta.json"):
        meta = agent_meta(sdir, mf.name[6:-10])
        if meta.get("toolUseId"):
            by_tool[meta["toolUseId"]] = (mf.name[6:-10], meta)
    pos = c["off"]
    for raw in data[:end].split(b"\n")[:-1]:
        i, at = c["n"], pos
        c["n"], pos = i + 1, pos + len(raw) + 1
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        r.setdefault("type", "?")
        r["_i"], r["_n"] = i, len(raw) + 1
        if r["type"] == "assistant":
            for b in (r.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    c["names"][b.get("id")] = b.get("name")
        c["sid"] = c["sid"] or r.get("sessionId")
        old = r.get("session_id")
        if not c["prev"] and old and old != r.get("sessionId") and (path.parent / f"{old}.jsonl").exists():
            c["prev"] = {"path": str(path.parent / f"{old}.jsonl"), "id": old}
        c["k"][i] = len(c["recs"])
        c["recs"].append(slim(r, c["names"], path, by_tool))
        index(c, r, c["k"][i])
        c["offs"].append((at, len(raw)))
    c["off"] += end
    return c


def full(path, c, k):
    off, n = c["offs"][k]
    with open(path, "rb") as f:
        f.seek(off)
        r = json.loads(f.read(n))
    r.setdefault("type", "?")
    r["_i"] = c["recs"][k]["i"]
    return r


def title(p):
    # ponytail: looks at the last and first 1 MB only; a title written mid-file of a huge session is missed
    with open(p, "rb") as f:
        f.seek(max(0, p.stat().st_size - (1 << 20)))
        tail = f.read()
        f.seek(0)
        head = f.read(1 << 20)
    for chunk in (tail, head):
        for line in reversed(chunk.split(b"\n")):
            if b'"ai-title"' in line or b'"custom-title"' in line:
                try:
                    r = json.loads(line)
                    return r.get("customTitle") or r.get("aiTitle") or ""
                except ValueError:
                    pass
    return ""


def sessions(q):
    rows = [(p, p.stat()) for p in ROOT.glob("*/*.jsonl")]
    rows.sort(key=lambda x: -x[1].st_mtime)
    if q and q != "latest":
        ql = q.lower()
        rows = [x for x in rows if x[0].stem.startswith(q) or ql in x[0].parent.name.lower()][:50]
    else:
        rows = rows[:30]
    return [{"path": str(p), "id": p.stem, "project": p.parent.name, "mtime": s.st_mtime, "size": s.st_size, "title": title(p)}
            for p, s in rows]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return self.send(403, {"error": "bad host"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/sessions":
            return self.send(200, sessions(q.get("q", "")))
        if not u.path.startswith("/api/"):
            return self.send(404, {"error": "not found"})
        p = under(q.get("path", ""))
        if not p:
            return self.send(403, {"error": "path outside " + str(ROOT)})
        if not p.is_file():
            return self.send(404, {"error": "no such file"})
        try:
            if u.path == "/api/file":
                with open(p, "rb") as f:
                    return self.send(200, f.read(2 << 20), "text/plain; charset=utf-8")
            if p.suffix != ".jsonl":
                return self.send(403, {"error": "not a session file"})
            if u.path == "/api/subagents":
                return self.send(200, subagents(p))
            with LOCK:
                c = parse(p)
                if u.path == "/api/session":
                    since = int(q.get("since") or 0)
                    meta = p.parent / (p.stem + ".meta.json")
                    agent = None
                    if p.parent.name == "subagents" and meta.exists():
                        m = json.loads(meta.read_text(encoding="utf-8"))
                        agent = {"agentType": m.get("agentType"), "description": m.get("description"), "toolUseId": m.get("toolUseId")}
                    body = {"path": str(p), "id": p.stem, "size": c["off"], "total": len(c["recs"]), "sessionId": c["sid"],
                            "prev": c["prev"], "agent": agent, "records": c["recs"][since:]}
                elif u.path == "/api/compacts":
                    ks = [k for k, s in enumerate(c["recs"]) if s["subtype"] == "compact_boundary"]
                    near = {j: full(p, c, j) for k in ks for j in range(k, min(k + 40, len(c["recs"])))}
                    body = compacts([near.get(j, {"type": "?"}) for j in range(len(c["recs"]))])
                elif u.path == "/api/text":
                    k = c["k"].get(int(q.get("i", -1)))
                    if k is None:
                        return self.send(404, {"error": "no such record"})
                    r = full(p, c, k)
                    if r["type"] in ("user", "assistant"):
                        t = text_of((r.get("message") or {}).get("content"))
                    else:
                        t = "\n".join(str(x.get("content", "")) for x in r.get("rendered") or [] if isinstance(x, dict)) if r["type"] == "attachment" else ""
                        t = t or json.dumps({k: v for k, v in r.items() if k != "_i"}, ensure_ascii=False, indent=1)
                    body = {"text": t[:200_000] + ("\n[truncated]" if len(t) > 200_000 else "")}
                elif u.path == "/api/raw":  # the line exactly as on disk, not full(), which adds _i and type
                    k = c["k"].get(int(q.get("i", -1)))
                    if k is None:
                        return self.send(404, {"error": "no such record"})
                    off, n = c["offs"][k]
                    with open(p, "rb") as f:
                        f.seek(off)
                        body = f.read(n).rstrip(b"\r")
                elif u.path == "/api/requests":
                    body = [row for row, _ in requests(p, c)]
                elif u.path in ("/api/request", "/api/block"):
                    n = int(q.get("n", -1))
                    if not 0 <= n < len(requests(p, c)):
                        return self.send(404, {"error": "no such request"})
                    if u.path == "/api/request":
                        body = request(p, c, n)
                    else:
                        try:
                            t = block(p, c, n, q.get("ref", ""))
                        except (KeyError, IndexError, TypeError):
                            return self.send(404, {"error": "no such block"})
                        body = {"text": t[:200_000] + ("\n[truncated]" if len(t) > 200_000 else "")}
                else:
                    return self.send(404, {"error": "not found"})
            return self.send(200, body)
        except (ValueError, OSError) as e:
            return self.send(400, {"error": str(e)})


def main(argv):
    if "-h" in argv or "--help" in argv:
        sys.exit(__doc__)
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8765
    at = int(argv[argv.index("--at") + 1]) if "--at" in argv else None
    args = [a for k, a in enumerate(argv) if not a.startswith("-") and (k == 0 or argv[k - 1] not in ("--port", "--at"))]
    path = resolve(args[0] if args else None).resolve()
    if not path.is_relative_to(ROOT):
        ROOTS.append(path.parent)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?path={quote(str(path))}"
    if at is not None:
        url += f"&at={at}"
    print(f"ctxview on {url}  (Ctrl+C to stop)", flush=True)
    if "--no-open" not in argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
