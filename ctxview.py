"""Local viewer for Claude Code session files: what is on disk vs what the model sees.

  python ctxview.py [latest | <session-id prefix> | <path.jsonl>] [--port 8765] [--no-open]

Serves index.html and a read-only JSON API on 127.0.0.1. Only files under ~/.claude/projects are readable
(override the root with env CLAUDE_PROJECTS_DIR).
"""
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
    k = kind(r, names) if t in SENT else t
    ev, cmd, pers, link, skill, model, cat = [], None, None, None, None, None, None
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
                if b.get("name") == "Skill":
                    skill = (b.get("input") or {}).get("skill")
                elif b.get("name") in ("Agent", "Task") and b.get("id") in by_tool:
                    aid, meta = by_tool[b["id"]]
                    link = {"id": aid, "path": str(sdir / f"agent-{aid}.jsonl"), "agentType": meta.get("agentType"),
                            "description": meta.get("description")}
        if skill:
            ev.append("skill")
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
            "skill": skill, "ev": ev}


def parse(path):
    """bring the cache for path up to date: read only complete lines appended since last time, restart if the file shrank"""
    size = path.stat().st_size
    with open(path, "rb") as f:
        first = f.read(256)  # a rewritten file of the same or larger size starts differently
    c = CACHE.get(path)
    if not c or size < c["off"] or c["first"] != first[:len(c["first"])]:
        c = CACHE[path] = {"off": 0, "n": 0, "names": {}, "recs": [], "offs": [], "k": {}, "sid": None, "prev": None, "first": first}
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
                else:
                    return self.send(404, {"error": "not found"})
            return self.send(200, body)
        except (ValueError, OSError) as e:
            return self.send(400, {"error": str(e)})


def main(argv):
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8765
    args = [a for k, a in enumerate(argv) if not a.startswith("-") and (k == 0 or argv[k - 1] != "--port")]
    path = resolve(args[0] if args else None).resolve()
    if not path.is_relative_to(ROOT):
        ROOTS.append(path.parent)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?path={quote(str(path))}"
    print(f"ctxview on {url}  (Ctrl+C to stop)", flush=True)
    if "--no-open" not in argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
