"""python test_ctxview.py  -- starts ctxview on a free port against fixtures/ and checks the API and the window rule."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
SAMPLE = FIX / "sample.jsonl"


def get(port, url, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{url}", headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req) as r:
            assert r.headers["Cache-Control"] == "no-store"
            assert "Access-Control-Allow-Origin" not in r.headers
            body = r.read()
            return r.status, json.loads(body) if r.headers["Content-Type"].startswith("application/json") else body
    except urllib.error.HTTPError as e:
        return e.code, None


def window_at(recs, n):  # Python port of index.html windowAt, without the kept-tail splice (= ctx.py)
    sl = [r for r in recs if r["i"] <= n]
    by = {r["uuid"]: r for r in sl if r["uuid"] and not r["side"]}
    leaf = next((r for r in reversed(sl) if r["uuid"] in by and r["type"] in ("user", "assistant", "attachment", "system")), None)
    chain = []
    while leaf:
        chain.append(leaf)
        if leaf["subtype"] == "compact_boundary":
            break
        leaf = by.get(leaf["parent"])

    def inw(r):
        if r["type"] == "attachment":
            return r["rendered"]
        return r["type"] in ("user", "assistant") and (r["summary"] or not r["tonly"])
    return [r["uuid"] for r in chain[::-1] if inw(r)]


def h(msg):  # the hash Claude Code puts in api-request tails
    return hashlib.sha256(json.dumps(msg, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def att(i, typ, text):
    return {"type": "attachment", "uuid": f"t{i}", "attachment": {"type": typ}, "rendered": [{"content": f"<system-reminder>\n{text}\n</system-reminder>"}]}


SYS_TEXT = "Lead in. " + "The following skills are available: demo, other\n\n" + "Today's date is 2026-11-16."
TOOL = {"name": "Bash", "description": "run a command", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}
ADD = {"type": "tool_addition", "tool": {"name": "Late", "description": "loaded later"}}
B_SYS = {"role": "system", "content": [{"type": "text", "text": SYS_TEXT}, ADD]}
B_USER = {"role": "user", "content": [{"type": "text", "text": "<system-reminder>\nProject rules: be brief.\n</system-reminder>"}]}
IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD" * 50}}
B_RES = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": [{"type": "text", "text": "an image"}, IMG]},
                                     {"type": "text", "text": "Base directory for this skill: /demo\nbody"}]}
RESP = [{"type": "thinking", "thinking": "ponder", "signature": "SIG" * 40}, {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "a.png"}}]


def request_fixture(path):
    """3 requests: a fresh one, one on a base with keep, and a keep:0 reset with no response yet and one missing message"""
    usage = lambda n: {"input_tokens": 10, "cache_read_input_tokens": n, "cache_creation_input_tokens": 0, "output_tokens": 5}
    resp1 = {"role": "assistant", "content": RESP}
    summ = "Summary of the earlier conversation."
    lines = [
        att(0, "instructions", "Project rules: be brief."),
        att(1, "skill_listing", "The following skills are available: demo, other"),
        att(2, "date", "Today's date is 2026-11-16."),
        {"type": "user", "uuid": "q3", "message": {"role": "user", "content": "hi"}},
        {"type": "api-request-shape", "shapeHash": "S1", "shape": {"system": [{"type": "text", "text": "You are X."}, {"type": "text", "text": "Be good."}], "tools": [TOOL]}},
        {"type": "api-request-blob", "hash": h(B_SYS), "message": B_SYS},
        {"type": "api-request-blob", "hash": h(B_USER), "message": B_USER},
        {"type": "api-request", "id": "R0", "keep": 0, "tail": [h(B_SYS), h(B_USER), h({"role": "user", "content": [{"type": "text", "text": "hi"}]})],
         "shapeHash": "S1", "params": {"model": "m-1"}, "timestamp": "T0",
         "cacheControl": [{"path": ["system", 1], "value": {"type": "ephemeral", "ttl": "1h", "scope": "global"}},
                          {"path": ["messages", 2, "content", 0], "value": {"type": "ephemeral", "ttl": "1h"}},
                          {"path": ["messages", 9, "content", 0], "value": {"type": "ephemeral"}}]},
        {"type": "assistant", "uuid": "a8", "requestRef": "R0", "apiBlockIndex": 0, "message": {"id": "m1", "role": "assistant", "content": RESP[:1], "usage": usage(1500)}},
        {"type": "assistant", "uuid": "a9", "requestRef": "R0", "apiBlockIndex": 1, "message": {"id": "m1", "role": "assistant", "content": RESP[1:], "usage": usage(1500)}},
        {"type": "api-request-blob", "hash": h(B_RES), "message": B_RES},
        {"type": "api-request", "id": "R1", "base": "R0", "keep": 3, "tail": [h(resp1), h(B_RES)], "shapeHash": "S1", "params": {"model": "m-1"},
         "cacheControl": [{"path": ["messages", 4, "content", 1], "value": {"type": "ephemeral"}}]},
        {"type": "assistant", "uuid": "a12", "requestRef": "R1", "apiBlockIndex": 0, "message": {"id": "m2", "role": "assistant",
                                                                                              "content": [{"type": "text", "text": "done"}], "usage": usage(3000)}},
        {"type": "user", "uuid": "q13", "isCompactSummary": True, "message": {"role": "user", "content": summ}},
        {"type": "api-request", "id": "R2", "base": "R1", "keep": 0, "tail": [h({"role": "user", "content": [{"type": "text", "text": summ}]}), "deadbeef"],
         "shapeHash": "S1", "params": {"model": "m-1"}, "cacheControl": []},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def main():
    env = dict(os.environ, CLAUDE_PROJECTS_DIR=str(FIX), PYTHONIOENCODING="utf-8")
    assert "--at LINE" in subprocess.run([sys.executable, str(HERE / "ctxview.py"), "--help"], capture_output=True, text=True, timeout=10).stderr
    srv = subprocess.Popen([sys.executable, str(HERE / "ctxview.py"), str(SAMPLE), "--port", "0", "--at", "11", "--no-open"],
                           env=env, stdout=subprocess.PIPE, text=True)
    live = FIX / "live-test.jsonl"
    reqf = FIX / "request-test.jsonl"
    try:
        first = srv.stdout.readline()
        assert "&at=11" in first, first
        port = int(first.split("127.0.0.1:")[1].split("/")[0])
        P = quote(str(SAMPLE))

        code, s = get(port, f"/api/session?path={P}&since=0")
        assert code == 200, code
        recs = s["records"]
        by = {r["uuid"]: r for r in recs if r["uuid"]}
        assert s["total"] == len(recs) == 24 and s["sessionId"] == "s-new"
        assert s["prev"] == {"path": str(SAMPLE), "id": "sample"}, s["prev"]
        assert s["agent"] is None
        assert window_at(recs, 23) == ["u13", "a14", "a15", "a17", "a19"]  # compact summary in, branch a18 out, tonly out
        assert window_at(recs, 11) == ["u0", "a1", "u3", "a4", "u5", "a6", "u7", "a8", "u10", "a11"]  # sidechain + unrendered out
        assert by["u0"]["cmd"] == "/clear" and "clear" in by["u0"]["ev"]
        assert by["u5"]["cmd"] == "/model" and "model" in by["u5"]["ev"]
        assert by["a2"]["model"] == "model-a" and by["a6"]["model"] == "model-b"
        assert by["u7"]["persisted"] == {"kb": 2.0, "path": None, "tok": 512} and "persisted" in by["u7"]["ev"]
        assert by["u7"]["kind"] == "tool result: Bash (persisted, preview only)" and by["u7"]["cat"] == "tool_results"
        for u in ("a8", "u10"):
            link = by[u]["agentLink"]
            assert link["id"] == "x" and link["agentType"] == "general-purpose" and link["path"].endswith("agent-x.jsonl"), link
        assert by["s9"]["side"] and by["b12"]["cm"]["kept"] == 2 and by["b12"]["cm"]["keptUuids"] == ["u10", "a11"]
        assert by["u13"]["cat"] == "summary" and by["a14"]["skill"] == "demo" and by["a17"]["skill"] == "demo"
        assert by["a1"]["cat"] == "instructions" and by["a15"]["cat"] == "instructions" and by["a14"]["cat"] == "skills"
        assert by["a20"]["ev"] == ["edited_file"] and not by["a20"]["rendered"]
        assert by["a4"]["usage"] == {"in": 5, "cr": 900, "cw": 100, "out": 20, "cw1h": 100, "cw5m": 0}
        assert recs[-1]["type"] == "last-prompt" and recs[-2]["kind"] == "ai-title"
        assert all(len(r["preview"]) <= 123 and "message" not in r for r in recs)  # slim: no bodies

        assert get(port, f"/api/session?path={P}&since=20")[1]["records"][0]["i"] == 20
        assert get(port, f"/api/text?path={P}&i=3")[1]["text"] == "hello there, list the files"
        assert get(port, f"/api/text?path={P}&i=1")[1]["text"] == "Project rules: keep it short."
        assert get(port, f"/api/raw?path={P}&i=3")[1] == json.loads(SAMPLE.read_text(encoding="utf-8").splitlines()[3])
        assert get(port, f"/api/raw?path={P}&i=9999")[0] == 404
        assert get(port, f"/api/compacts?path={P}")[1] == [{"i": 12, "trigger": "auto", "pre": 1000, "post": 200, "s": 5.0, "kept": 2,
                                                           "reinjected": [["compact summary", 1], ["skill bodies (re-injected)", 1],
                                                                          ["nested CLAUDE.md", 1]]}]  # u16 is transcript-only
        sub = get(port, f"/api/subagents?path={P}")[1]
        assert len(sub) == 1 and sub[0]["toolUseId"] == "tu2" and sub[0]["peak"] == 115 and sub[0]["back"] == 6, sub
        assert sub[0]["memory"] and sub[0]["loaded"] == "AutoMem, Project" and sub[0]["reminders"] == ["date", "instructions"]
        assert not sub[0]["fork"] and sub[0]["records"] == 4
        ag = get(port, f"/api/session?path={quote(sub[0]['path'])}")[1]
        assert ag["agent"]["toolUseId"] == "tu2" and ag["total"] == 4
        assert get(port, f"/api/file?path={quote(sub[0]['path'])}")[0] == 200

        # skill listings (full vs delta) and a SKILL.md read as a file instead of through the Skill tool
        sk = {r["uuid"]: r for r in get(port, f"/api/session?path={quote(str(FIX / 'skills.jsonl'))}")[1]["records"]}
        assert sk["k0"]["initial"] is True and sk["k1"]["initial"] is False and sk["k0"]["ev"] == ["skill_listing"]
        assert sk["k2"]["ev"] == ["skill_read"] and sk["k2"]["skill"] == "senza-connection"
        assert sk["k3"]["ev"] == ["skill_read"] and sk["k3"]["skill"] == "demo"
        assert sk["k4"]["ev"] == [] and sk["k5"]["ev"] == ["skill"] and sk["k5"]["skill"] == "demo"

        # the real request: old files have none; the fixture rebuilds 3 requests from blobs, a response, records and a base
        assert get(port, f"/api/requests?path={P}") == (200, [])
        request_fixture(reqf)
        Q = quote(str(reqf))
        assert [r["kind"] for r in get(port, f"/api/session?path={Q}")[1]["records"][4:8]] == \
            ["request copy: shape", "request copy: blob", "request copy: blob", "request copy: request"]
        code, rq = get(port, f"/api/requests?path={Q}")
        assert code == 200 and [(r["n"], r["i"], r["resp_i"], r["keep"], r["msgs"], r["missing"]) for r in rq] == \
            [(0, 7, 8, 0, 3, 0), (1, 11, 12, 3, 5, 0), (2, 14, None, 0, 2, 1)], rq
        d = SYS_TEXT.index("Today")
        user0 = len(B_USER["content"][0]["text"])
        assert rq[0]["chars_by"] == {"system": 18, "tools": len(json.dumps(TOOL)), "tool_additions": len(json.dumps(ADD)), "instructions": user0,
                                     "skills": d - 9, "reminders": 9 + len(SYS_TEXT) - d, "messages": 2, "tool_results": 0, "summary": 0}, rq[0]
        assert rq[0]["total"] == 1510 and sum(rq[0]["tokens"].values()) == 1510 and rq[0]["chars"] == sum(rq[0]["chars_by"].values())
        assert rq[0]["cpt"] == round(rq[0]["chars"] / 1510, 3) and rq[0]["images"] == 0
        assert rq[1]["total"] == 3010 and sum(rq[1]["tokens"].values()) == 3010 and rq[1]["images"] == 1 and rq[1]["tokens"]["images"] == 1600
        assert rq[1]["chars_by"]["tool_results"] == len("an image") and rq[1]["chars_by"]["skills"] == d - 9 + len(B_RES["content"][1]["text"])
        assert rq[1]["chars_by"]["messages"] == 2 + len("ponder") + len("Read") + len(json.dumps({"file_path": "a.png"}))  # no signature
        assert rq[2]["total"] is None and rq[2]["cpt"] == rq[1]["cpt"] and rq[2]["chars_by"]["summary"] == len("Summary of the earlier conversation.")

        r0 = get(port, f"/api/request?path={Q}&n=0")[1]
        assert r0["model"] == "m-1" and r0["total"] == 1510 and "input_schema" not in json.dumps(r0)  # never the tool schemas
        assert [(s["ref"], s["chars"], s["cache"], s["src"]) for s in r0["system"]] == [("system.0", 10, None, 4), ("system.1", 8, "1h global", 4)]
        assert r0["tools"] == [{"ref": "tools.0", "name": "Bash", "chars": len(json.dumps(TOOL)), "cache": None, "src": 4}]
        assert [(m["role"], m["from"], m["src"], m["new"]) for m in r0["messages"]] == [("system", "blob", 5, True), ("user", "blob", 6, True), ("user", "record", 3, True)]
        b0 = r0["messages"][0]["blocks"]
        assert b0[0]["split"] == [["reminders", 9, None], ["skills", d - 9, 1], ["reminders", len(SYS_TEXT) - d, 2]] and b0[0]["cat"] == "skills"
        assert b0[1]["cat"] == "tool_additions" and b0[1]["ref"] == "messages.0.content.1"
        assert r0["messages"][1]["blocks"][0]["split"] == [["instructions", user0, 0]] and r0["messages"][1]["blocks"][0]["head"].startswith("<system-reminder>")
        assert r0["messages"][2]["blocks"][0]["cache"] == "1h" and r0["messages"][2]["blocks"][0]["cat"] == "messages"
        r1 = get(port, f"/api/request?path={Q}&n=1")[1]
        assert [(m["role"], m["from"], m["src"], m["new"]) for m in r1["messages"][2:]] == [("user", "record", 3, False), ("assistant", "response", 8, True), ("user", "blob", 10, True)]
        assert [(b["type"], b["chars"]) for b in r1["messages"][3]["blocks"]] == [("thinking", 6), ("tool_use", 4 + len(json.dumps({"file_path": "a.png"})))]
        b4 = r1["messages"][4]["blocks"]
        assert (b4[0]["cat"], b4[0]["chars"], b4[0]["images"]) == ("tool_results", 8, 1) and (b4[1]["cat"], b4[1]["cache"]) == ("skills", "5m")
        r2 = get(port, f"/api/request?path={Q}&n=2")[1]
        assert r2["messages"][0]["blocks"][0]["cat"] == "summary" and r2["messages"][1]["from"] == "missing" and r2["messages"][1]["blocks"] == []

        blk = lambda n, ref: get(port, f"/api/block?path={Q}&n={n}&ref={ref}")
        assert blk(0, "system.0") == (200, {"text": "You are X."}) and blk(0, "messages.0.content.0")[1]["text"] == SYS_TEXT
        assert '"Bash"' in blk(0, "tools.0")[1]["text"]
        t = blk(1, "messages.4.content.0")[1]["text"]
        assert '"[image]"' in t and "QUJD" not in t and json.loads(t)["content"][0]["text"] == "an image"
        assert "SIG" in blk(1, "messages.3.content.0")[1]["text"]  # the raw block, signature included
        assert blk(0, "messages.9.content.0")[0] == 404 and blk(0, "nope.0")[0] == 404 and blk(0, "system.-1")[0] == 404
        assert blk(2, "messages.1.content.0")[0] == 404 and blk(9, "system.0")[0] == 404
        assert get(port, f"/api/request?path={Q}&n=3")[0] == 404 and get(port, f"/api/request?path={Q}&n=x")[0] == 400

        sessions = get(port, "/api/sessions?q=latest")[1]
        assert isinstance(sessions, list)

        # trust boundary: outside the root, a bad Host, a traversal
        assert get(port, f"/api/session?path={quote(str(HERE / 'test_ctxview.py'))}")[0] == 403
        assert get(port, f"/api/file?path={quote(str(HERE / 'ctxview.py'))}")[0] == 403
        assert get(port, f"/api/text?path={quote(str(FIX / '..' / 'ctxview.py'))}&i=0")[0] == 403
        assert get(port, f"/api/sessions", host="evil.example:80")[0] == 403
        t0 = time.time()  # a UNC path is refused before anything touches the network
        assert get(port, "/api/file?path=" + quote(r"\\127.0.0.1\c$\x.txt"))[0] == 403 and time.time() - t0 < 1

        # the shipped JS rule, run in node: the kept tail (u10, a11) is spliced in after the compact summary
        if shutil.which("node"):
            html = (HERE / "index.html").read_text(encoding="utf-8")
            js = html[html.index("// ---------- window rule"):html.index("// ---------- derive")]
            prog = ("const SENT = new Set(['user','assistant','attachment','system']); const total = u => u ? u.in + u.cr + u.cw : null;"
                    "const S = {meta: null, winCache: new Map(), reqs: [], nextReq: []};" + js +
                    "S.recs = JSON.parse(require('fs').readFileSync(0, 'utf8')); S.recs.forEach((r, k) => r._p = k);"
                    "console.log(JSON.stringify(windowAt(S.recs.length - 1).rows.map(r => r.uuid)));")
            out = subprocess.run(["node", "-e", prog], input=json.dumps(recs), capture_output=True, text=True, check=True).stdout
            assert json.loads(out) == ["u13", "u10", "a11", "a14", "a15", "a17", "a19"], out

        # live tail: half-written line waits, completed line arrives, a shrunk file is re-read
        lines = SAMPLE.read_bytes().split(b"\n")
        live.write_bytes(b"\n".join(lines[:5]) + b"\n")
        L = quote(str(live))
        assert get(port, f"/api/session?path={L}")[1]["total"] == 5
        with open(live, "ab") as f:
            f.write(lines[5][:20])
        assert get(port, f"/api/session?path={L}&since=5")[1]["total"] == 5
        with open(live, "ab") as f:
            f.write(lines[5][20:] + b"\n")
        s = get(port, f"/api/session?path={L}&since=5")[1]
        assert s["total"] == 6 and [r["uuid"] for r in s["records"]] == ["u5"]
        live.write_bytes(b"\n".join(lines[:2]) + b"\n")
        assert get(port, f"/api/session?path={L}")[1]["total"] == 2
        live.write_bytes(b"\n".join(lines[3:7]) + b"\n")  # rewritten, not shrunk: starts differently
        assert [r["uuid"] for r in get(port, f"/api/session?path={L}")[1]["records"]] == ["u3", "a4", "u5", "a6"]
        print("ok")
    finally:
        srv.terminate()
        live.unlink(missing_ok=True)
        reqf.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
