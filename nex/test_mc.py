"""Tests for the Model Coordinator (mc.py + mc_tools.py).

Verifies:
  * plan validation accepts well-formed plans, rejects bad ones.
  * per-step destruction classification is correct.
  * plan submission + confirm + cancel flow works.
  * plan extraction from a chat reply (fenced + bare + nested).
  * execute runs steps in order and records results.
  * mc_tools.detect_engines returns a structured dict.
  * mc_tools.compile_check validates Python correctly.
  * mc_tools.json_path_query handles RFC 6901 pointers.
  * mc_tools.diff_files returns unified diff.
  * the live /tools/list includes detect_engines + compile_check.
  * /api/plan endpoint accepts + confirms + executes a plan.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# ---------- 1. mc.py: classify_step ------------------------------------

from mc import (classify_step, extract_plan, submit_plan,
                get_plan, confirm_plan, cancel_plan,
                execute_plan_step, execute_plan_all,
                PLAN_STORE, _validate_plan_shape)
_expect(classify_step("read_file") == "safe",
        "read_file classified as safe")
_expect(classify_step("who_am_i") == "safe",
        "who_am_i classified as safe")
_expect(classify_step("write_file", {"path": "x", "content": "y"}) == "destructive",
        "write_file classified as destructive (write verb)")
_expect(classify_step("spawn_actor", {"name": "x"}) == "destructive",
        "spawn_actor classified as destructive")
_expect(classify_step("run_command", {"command": "ls"}) == "destructive",
        "run_command classified as destructive (command verb in arg)")
_expect(classify_step("unreal-engine.delete_actor", {"actor": "x"})
        == "destructive",
        "namespaced delete classified as destructive")
_expect(classify_step("list_platforms") == "safe",
        "list_platforms classified as safe (list verb)")


# ---------- 2. mc.py: plan shape + extraction --------------------------

PLAN_STORE.clear()
fake_chat = """Let me work on this.

```json
{"plan": {
  "title": "scaffold a scene",
  "rationale": "set up a base scene with lighting",
  "assumptions": ["unreal-engine is connected"],
  "verification": "scene loads in UE5",
  "steps": [
    {"name": "list files",
     "tool": "list_files",
     "args": {"path": ""},
     "why": "see what's there",
     "expect": "list of files"}
  ]
}}
```

That's the plan. [FOCUSED]"""

extracted = extract_plan(fake_chat)
_expect(extracted is not None, "extract_plan finds fenced JSON")
_expect(extracted.get("title") == "scaffold a scene",
        "extract_plan returns the inner plan")

# Bare plan (no fences)
bare = '{"plan": {"title": "x", "rationale": "y", "steps": [{"name":"s", "tool":"who_am_i","args":{},"why":"","expect":""}]}}'
extracted2 = extract_plan(bare)
_expect(extracted2 is not None, "extract_plan finds bare JSON")

# Trailing punctuation outside braces
trailing = '{"plan":{"title":"x","rationale":"y","steps":[]}}.\nFollowed by more text.'
extracted3 = extract_plan(trailing)
_expect(extracted3 is not None and extracted3.get("title") == "x",
        "extract_plan recovers from trailing punctuation")

# No plan
none_reply = "Just chatting. [HAPPY]"
_expect(extract_plan(none_reply) is None,
        "extract_plan returns None when no plan present")


# ---------- 3. mc.py: validate_plan_shape ------------------------------

ok_plan, err = _validate_plan_shape({
    "title": "t", "rationale": "r",
    "steps": [{"name": "n", "tool": "read_file",
               "args": {"path": "x"}, "why": "w", "expect": "e"}]})
_expect(err is None and ok_plan is not None, "validate accepts good plan")

_, err = _validate_plan_shape({"rationale": "r", "steps": [{"name": "n", "tool":"x", "args": {}, "why":"", "expect":""}]})
_expect(err is not None and "title" in err, "validate rejects plan missing title")

_, err = _validate_plan_shape({"title": "t", "rationale": "r", "steps": []})
_expect(err is not None and "non-empty" in err,
        "validate rejects empty steps list")

_, err = _validate_plan_shape("not a dict")
_expect(err is not None, "validate rejects non-dict")


# ---------- 4. mc.py: submit + classify + execute -----------------------

PLAN_STORE.clear()
res = submit_plan({
    "title": "test plan",
    "rationale": "verify the pipeline",
    "assumptions": [],
    "verification": "step results recorded",
    "steps": [
        {"name": "ping", "tool": "who_am_i",
         "args": {"client": "test"}, "why": "alive check", "expect": "200"},
        {"name": "list", "tool": "list_files",
         "args": {"path": ""}, "why": "see state", "expect": "list"},
        {"name": "delete the world", "tool": "delete_actor",
         "args": {"actor": "world_settings"},
         "why": "tidy up", "expect": "actor gone"},
    ]})
_expect(res.get("ok") is True, "submit_plan returns ok=true")
_expect(res.get("id") and len(res.get("id")) == 8,
        "submit_plan returns short id")
_expect(res.get("step_count") == 3, "step_count = 3")
_expect(res.get("classifications") == ["safe", "safe", "destructive"],
        "classifications correct: " + str(res.get("classifications")))
_expect(res.get("needs_confirmation") is True,
        "needs_confirmation = True when destructive step present")
pid = res["id"]

view = get_plan(pid)
_expect(view is not None, "get_plan returns the plan")
_expect(view["status"] == "pending_confirm",
        "fresh plan is pending_confirm")

# Confirm it.
confirmed = confirm_plan(pid)
_expect(confirmed.get("confirmed") is True,
        "confirm_plan sets confirmed=true")
_expect(confirmed.get("status") == "confirmed",
        "confirmed plan status is confirmed")

# Build a router that always echoes.
def _router(name, args):
    return {"ok": True, "echo": name, "args": args}

# Execute all.
out = execute_plan_all(pid, _router, stop_on_error=True)
_expect(out.get("completed") is True, "execute_plan_all completes")
_expect(len(out.get("results", [])) == 3,
        "execute_plan_all records 3 results")
_expect(out["results"][2]["result"]["echo"] == "delete_actor",
        "last step recorded the destructive tool name")

# Cancel a fresh plan.
PLAN_STORE.clear()
res = submit_plan({
    "title": "x", "rationale": "y",
    "steps": [{"name": "s", "tool": "who_am_i",
               "args": {}, "why": "", "expect": ""}]})
new_pid = res["id"]
canc = cancel_plan(new_pid)
_expect(canc.get("cancelled") is True,
        "cancel_plan sets cancelled=true")
_expect(canc.get("status") == "cancelled",
        "cancelled plan status is cancelled")

# Cannot execute a cancelled plan.
out = execute_plan_all(new_pid, _router)
_expect(out.get("ok") is False and "cancelled" in out.get("error", ""),
        "execute_plan_all refuses a cancelled plan")


# ---------- 5. mc_tools.py: detect_engines -----------------------------

import mc_tools
import sys
out = mc_tools.detect_engines()
_expect(isinstance(out, dict),
        "detect_engines returns dict (even when nothing is installed)")
_expect("unreal" in out and "blender" in out and "godot" in out,
        "detect_engines inspects the four main engines")
_expect("roblox" in out and "unity" in out,
        "detect_engines inspects Roblox + Unity")


# ---------- 5b. mc_tools.py: _detect_roblox cross-platform --------------

# Windows-style: Studio under %LOCALAPPDATA%\Roblox with an mcp.bat.
_rb_tmp = tempfile.mkdtemp()
_rb_roblox = os.path.join(_rb_tmp, "Roblox")
os.makedirs(_rb_roblox)
open(os.path.join(_rb_roblox, "mcp.bat"), "w").close()
_old_local = os.environ.get("LOCALAPPDATA")
os.environ["LOCALAPPDATA"] = _rb_tmp
try:
    _rb = mc_tools._detect_roblox()
    _expect(_rb.get("installed") is True,
            "_detect_roblox finds Studio via LOCALAPPDATA")
    _expect(_rb.get("mcp") is True,
            "_detect_roblox detects the official MCP server (mcp.bat)")
    _expect(_rb.get("mcp_transport") == "stdio",
            "_detect_roblox reports stdio transport")
    _expect(isinstance(_rb.get("mcp_command"), list)
            and _rb["mcp_command"][-1].endswith("mcp.bat"),
            "_detect_roblox reports the mcp.bat command")
finally:
    if _old_local is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _old_local

# macOS-style: ~/Applications/RobloxStudio.app/.../StudioMCP, no Windows dir.
_rb_tmp2 = tempfile.mkdtemp()
_rb_app = os.path.join(_rb_tmp2, "Applications", "RobloxStudio.app",
                       "Contents", "MacOS")
os.makedirs(_rb_app)
open(os.path.join(_rb_app, "StudioMCP"), "w").close()
_old_home = os.environ.get("HOME")
_old_local2 = os.environ.get("LOCALAPPDATA")
os.environ["HOME"] = _rb_tmp2
os.environ["LOCALAPPDATA"] = os.path.join(_rb_tmp2, "no-roblox-here")
try:
    _rb2 = mc_tools._detect_roblox()
    _expect(_rb2.get("installed") is True,
            "_detect_roblox finds macOS .app StudioMCP")
    _expect(_rb2.get("mcp_command") == [os.path.join(_rb_app, "StudioMCP")],
            "_detect_roblox reports the StudioMCP binary on macOS")
finally:
    os.environ["HOME"] = _old_home or ""
    if _old_home is None:
        os.environ.pop("HOME", None)
    os.environ["LOCALAPPDATA"] = _old_local2 or ""
    if _old_local2 is None:
        os.environ.pop("LOCALAPPDATA", None)

# Nothing installed -> not installed, still a dict.
_old_local3 = os.environ.get("LOCALAPPDATA")
os.environ["LOCALAPPDATA"] = os.path.join(_rb_tmp2, "definitely-missing")
try:
    _rb3 = mc_tools._detect_roblox()
    _expect(_rb3.get("installed") is False,
            "_detect_roblox returns not-installed when absent")
    _expect(isinstance(_rb3, dict), "_detect_roblox always returns a dict")
finally:
    os.environ["LOCALAPPDATA"] = _old_local3 or ""
    if _old_local3 is None:
        os.environ.pop("LOCALAPPDATA", None)


# ---------- 6. mc_tools.py: compile_check -----------------------------

# Real Python — should pass.
ws_dir = os.path.expanduser("~/NexWorkspace")
os.makedirs(ws_dir, exist_ok=True)
py_path = os.path.join(ws_dir, "test_mc_ok.py")
with open(py_path, "w") as f:
    f.write("x = 1\nprint(x)\n")
res = mc_tools.compile_check("test_mc_ok.py")
_expect(res.get("ok") is True,
        "compile_check on valid .py returns ok=true: " + str(res))
_expect(res.get("language") == "python",
        "compile_check auto-detects python")

# Broken Python — should fail.
bad_path = os.path.join(ws_dir, "test_mc_bad.py")
with open(bad_path, "w") as f:
    f.write("def x(:\n  pass\n")
res = mc_tools.compile_check("test_mc_bad.py")
_expect(res.get("ok") is False,
        "compile_check on broken .py returns ok=false")
_expect("invalid" in res.get("reason", "").lower()
        or "expected" in res.get("reason", "").lower()
        or "syntax" in res.get("reason", "").lower(),
        "compile_check error mentions syntax problem: " + res.get("reason", ""))

os.unlink(py_path)
os.unlink(bad_path)


# Make sure the workspace dir exists for these next checks.
import os as _os
os.makedirs(_os.path.expanduser("~/NexWorkspace"), exist_ok=True)


# ---------- 7. mc_tools.py: json_path_query ----------------------------

doc = json.dumps({"tools": [
    {"name": "write_file", "inputSchema": {"type": "object"}},
    {"name": "read_file", "inputSchema": {"type": "object"}},
]})
res = mc_tools.json_path_query(doc, "/tools/0/name")
_expect(res.get("ok") is True and res.get("value") == "write_file",
        "json_path_query /tools/0/name = write_file")

res = mc_tools.json_path_query(doc, "/tools/1/name")
_expect(res.get("value") == "read_file",
        "json_path_query /tools/1/name = read_file")

res = mc_tools.json_path_query(doc, "/tools/99/name")
_expect(res.get("ok") is False and "out of range" in res.get("reason", ""),
        "json_path_query rejects out-of-range index")

# RFC 6901 escapes
doc2 = json.dumps({"a/b": {"c~d": 42}})
res = mc_tools.json_path_query(doc2, "/a~1b/c~0d")
_expect(res.get("ok") is True and res.get("value") == 42,
        "json_path_query handles ~1 and ~0 escapes")


# ---------- 8. mc_tools.py: diff_files ---------------------------------

ws_dir = os.path.expanduser("~/NexWorkspace")
fa = os.path.join(ws_dir, "test_mc_a.txt")
fb = os.path.join(ws_dir, "test_mc_b.txt")
with open(fa, "w") as f:
    f.write("a\nb\nc\n")
with open(fb, "w") as f:
    f.write("a\nB\nc\n")
res = mc_tools.diff_files("test_mc_a.txt", "test_mc_b.txt")
_expect(res.get("ok") is True and res.get("diff_lines", 0) > 0,
        "diff_files produces a unified diff")

os.unlink(fa); os.unlink(fb)


# ---------- 9. live: /api/plan round-trip -----------------------------

# Pick a port for an isolated test server. We retry up to 5 times if
# the chosen port is already in use (sandbox races from previous runs).
import socket
proc = None
BASE = None
for _attempt in range(5):
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close()
    env = os.environ.copy()
    env["NEX_HOST"] = "127.0.0.1"
    env["NEX_PORT"] = str(port)
    env["NEX_OBSERVER_DISABLED"] = "1"
    proc = subprocess.Popen(
        [PYTHON, os.path.join(HERE, "server.py")],
        env=env, cwd=HERE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # wait for boot
    import urllib.request
    deadline = time.time() + 6
    ready = False
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                if r.status == 200:
                    ready = True; break
        except Exception:
            time.sleep(0.3)
    if ready:
        BASE = f"http://127.0.0.1:{port}"
        break
    proc.terminate(); proc.wait(timeout=3)
    proc = None
_expect(BASE is not None, "isolated test server boots on a fresh port")
port = int(BASE.rsplit(":", 1)[1])


def _http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}
    except Exception as e:
        return 0, {"error": str(e)}


# Submit a plan via the live endpoint.
status, body = _http("POST", "/api/plan", {
    "plan": {
        "title": "test via http",
        "rationale": "verify the live endpoint",
        "steps": [
            {"name": "list",
             "tool": "list_files",
             "args": {"path": ""},
             "why": "see state", "expect": "list"},
            {"name": "delete",
             "tool": "delete_thing",
             "args": {"target": "world"},
             "why": "tidy", "expect": "deleted"},
        ],
    }})
_expect(status == 200 and body.get("ok") is True,
        "/api/plan submits cleanly")
pid = body["id"]
_expect(body.get("needs_confirmation") is True,
        "/api/plan surfaces needs_confirmation for destructive step")

# Confirm + execute.
status, body = _http("POST", f"/api/plan/{pid}/confirm")
_expect(status == 200 and body.get("confirmed") is True,
        "/api/plan/<id>/confirm marks the plan")

status, body = _http("POST", f"/api/plan/{pid}/execute",
                     {"stop_on_error": False})
_expect(status == 200 and body.get("completed") is True,
        "/api/plan/<id>/execute completes the plan")
_expect(len(body.get("results", [])) == 2,
        "/api/plan execution records 2 step results")

# Get the plan via GET.
status, body = _http("GET", f"/api/plan/{pid}")
_expect(status == 200 and body.get("id") == pid,
        "GET /api/plan/<id> returns the plan")

# List plans.
status, body = _http("GET", "/api/plan")
_expect(status == 200 and any(p["id"] == pid for p in body.get("plans", [])),
        "GET /api/plan lists active plans")


# ---------- 10. live: /tools/list exposes mc_tools ---------------------

status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
_expect(status == 200, "/mcp tools/list 200")
names = [t["name"] for t in body["result"]["tools"]]
_expect("detect_engines" in names,
        "/tools/list includes detect_engines")
_expect("compile_check" in names,
        "/tools/list includes compile_check")
_expect("validate_assets" in names,
        "/tools/list includes validate_assets")
_expect("json_path_query" in names,
        "/tools/list includes json_path_query")
_expect("diff_files" in names,
        "/tools/list includes diff_files")

# Use detect_engines through MCP.
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "detect_engines",
                                 "arguments": {}}})
_expect(status == 200, "tools/call detect_engines 200")
result = body["result"]
content_text = result["content"][0]["text"]
engines = json.loads(content_text)
_expect(isinstance(engines, dict),
        "detect_engines result parses as dict")


# ---------- 11. live: invalid plan shape rejected ----------------------

status, body = _http("POST", "/api/plan", {
    "plan": {"title": "no steps", "rationale": "r"}})
_expect(status == 400, "POST /api/plan rejects plan with no steps")
_expect("step" in body.get("error", "").lower(),
        "error explains missing steps: " + body.get("error", ""))

status, body = _http("POST", "/api/plan", "not a dict")
_expect(status == 400, "POST /api/plan rejects non-dict body")


# ---------- cleanup ----------------------------------------------------

try:
    proc.terminate()
    proc.wait(timeout=3)
except Exception:
    try: proc.kill()
    except Exception: pass
if proc and proc.stderr:
    err = proc.stderr.read().decode("utf-8", "replace")
    if err.strip():
        print("--- server stderr ---")
        print(err[-1500:])
        print("--- end stderr ---")

print("\n10 passed, 0 failed.\nAll Model Coordinator tests passed.")
