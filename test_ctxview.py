"""python test_ctxview.py  -- starts ctxview on a free port against fixtures/ and checks the API and the window rule."""
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


def main():
    env = dict(os.environ, CLAUDE_PROJECTS_DIR=str(FIX), PYTHONIOENCODING="utf-8")
    assert "--at LINE" in subprocess.run([sys.executable, str(HERE / "ctxview.py"), "--help"], capture_output=True, text=True, timeout=10).stderr
    srv = subprocess.Popen([sys.executable, str(HERE / "ctxview.py"), str(SAMPLE), "--port", "0", "--at", "11", "--no-open"],
                           env=env, stdout=subprocess.PIPE, text=True)
    live = FIX / "live-test.jsonl"
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


if __name__ == "__main__":
    main()
