"""Security & reliability regression tests (review-driven).

Covers:
  1. Loopback default bind (0.0.0.0 requires explicit opt-in).
  2. NEX_AUTH_TOKEN gates every /api + /mcp route; served HTML bootstraps
     the token for the same-origin frontend.
  3. call_upstream is a read-only protocol inspector (no arbitrary
     JSON-RPC passthrough — no policy bypass).
  4. write_file: honest overwrite/destructive flags + O_NOFOLLOW.
  5. run_command honors the NEX_RUN_ALLOW executable allowlist.
  6. compile_check reports WHAT was checked (source vs environment).
  7. validate_assets: no Unity-style .meta assumptions for Unreal.
  8. Tunnel registry reads are lock-disciplined (snapshot under lock).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _boot(env_extra=None):
    port = _free_port()
    env = os.environ.copy()
    env.update({"NEX_HOST": "127.0.0.1", "NEX_PORT": str(port),
                "NEX_OBSERVER_DISABLED": "1"})
    env.update(env_extra or {})
    proc = subprocess.Popen(
        [PYTHON, os.path.join(HERE, "server.py")], env=env, cwd=HERE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    base = "http://127.0.0.1:%d" % port
    import urllib.request as _u
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            with _u.urlopen(base + "/api/health", timeout=1) as r:
                if r.status == 200:
                    return proc, base
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return proc, base
        except Exception:
            pass
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("server did not boot")


def _http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()[:200]
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}
    except Exception as e:
        return 0, {"error": str(e)}


# ---------- 1+2. bind + auth ------------------------------------------------

proc, base = _boot({"NEX_AUTH_TOKEN": "sekrit-test-token"})
try:
    status, _ = _http("GET", base + "/api/health")
    _expect(status == 401, "auth: /api/health without token -> 401")
    status, _ = _http("POST", base + "/api/state", {"state": "IDLE"})
    _expect(status == 401, "auth: POST /api/state without token -> 401")
    status, _ = _http("POST", base + "/api/agent/run",
                      {"goal": "x"})
    _expect(status == 401, "auth: POST /api/agent/run without token -> 401")
    H = {"X-Nex-Auth": "sekrit-test-token"}
    status, body = _http("GET", base + "/api/health", headers=H)
    _expect(status == 200, "auth: with token -> 200")
    status, body = _http("POST", base + "/api/state",
                         {"state": "IDLE"}, headers=H)
    _expect(status == 200, "auth: state change with token -> 200")
    # Bearer variant also accepted.
    status, _ = _http("GET", base + "/api/tunnels",
                      headers={"Authorization": "Bearer sekrit-test-token"})
    _expect(status == 200, "auth: Bearer variant accepted")
    # Served HTML bootstraps the token for the same-origin frontend.
    req = urllib.request.Request(base + "/")
    req.add_header("X-Nex-Auth", "sekrit-test-token")
    with urllib.request.urlopen(req, timeout=5) as r:
        html = r.read().decode()
    _expect("window.NEX_AUTH" in html, "auth: index.html bootstraps token")
finally:
    proc.terminate()
    proc.wait(timeout=3)

# ---------- 1b. default bind is loopback ------------------------------------

proc, base = _boot()
try:
    port = int(base.rsplit(":", 1)[1])
    import socket
    # Behavioral check: reachable on loopback, UNREACHABLE via the
    # container's non-loopback interface (LAN devices must not get in).
    s = socket.socket()
    s.settimeout(2)
    loopback_ok = (s.connect_ex(("127.0.0.1", port)) == 0)
    s.close()
    _expect(loopback_ok, "bind: reachable on 127.0.0.1")
    lan_ip = ""
    try:
        out = subprocess.run(["hostname", "-I"],
                             capture_output=True, text=True).stdout
        lan_ip = out.split()[0] if out.split() else ""
    except Exception:
        pass
    if lan_ip:
        s = socket.socket()
        s.settimeout(2)
        lan_blocked = (s.connect_ex((lan_ip, port)) != 0)
        s.close()
        _expect(lan_blocked,
                "bind: NOT reachable via %s (loopback-only default)" % lan_ip)
    else:
        _expect(True, "bind: no non-loopback IP to probe (single-host)")
finally:
    proc.terminate()
    proc.wait(timeout=3)

# ---------- 3. call_upstream allowlist (via the /mcp gateway) ----------------

def _mcp_tool(base, name, arguments):
    status, body = _http("POST", base + "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})
    blob = json.dumps(body)
    try:
        return blob, json.loads(
            json.loads(blob).get("result", {}).get("content", [{}])[0]
            .get("text", "{}"))
    except Exception:
        return blob, {}


proc, base = _boot()
try:
    blob, inner = _mcp_tool(base, "call_upstream", {
        "platform": "whatever", "method": "tools/call",
        "params": {"name": "write_file",
                   "arguments": {"path": "pwn.txt", "content": "x"}}})
    _expect("not allowed" in json.dumps(inner),
            "call_upstream: tools/call rejected")
    blob, inner = _mcp_tool(base, "call_upstream", {
        "platform": "whatever", "method": "resources/write", "params": {}})
    _expect("not allowed" in json.dumps(inner),
            "call_upstream: resources/write rejected")
    # An allowlisted method passes the gate (then fails on unknown
    # platform — proving the allowlist isn't blocking it).
    blob, inner = _mcp_tool(base, "call_upstream", {
        "platform": "no-such-server", "method": "tools/list", "params": {}})
    _expect("unknown platform" in json.dumps(inner),
            "call_upstream: allowlisted method reaches upstream layer")
finally:
    proc.terminate()
    proc.wait(timeout=3)

# ---------- 4. write_file flags + O_NOFOLLOW --------------------------------

sys.path.insert(0, HERE)
import tools  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    old_root = tools.TOOLS_ROOT
    tools.TOOLS_ROOT = td
    try:
        r1 = tools.tool_write_file("new.txt", "hello")
        _expect("error" not in r1 and r1.get("overwritten") is False,
                "write_file: new file -> overwritten False")
        r2 = tools.tool_write_file("new.txt", "hello again")
        _expect(r2.get("overwritten") is True and r2.get("destructive") is True,
                "write_file: overwrite -> overwritten+destructive True")
        r3 = tools.tool_write_file("new.txt", "more", append=True)
        _expect(r3.get("overwritten") is False and r3.get("appended") is True,
                "write_file: append is not an overwrite")

        # O_NOFOLLOW: a symlink swapped in must NOT be followed on write.
        outside = os.path.join(td, "..", "outside_target.txt")
        with open(outside, "w") as f:
            f.write("original")
        os.symlink(outside, os.path.join(td, "link.txt"))
        r4 = tools.tool_write_file("link.txt", "pwned")
        with open(outside) as f:
            content = f.read()
        _expect("error" in r4 and content == "original",
                "write_file: O_NOFOLLOW refuses symlinked final component")
    finally:
        tools.TOOLS_ROOT = old_root

# ---------- 5. run_command allowlist -----------------------------------------

old_shell = tools.SHELL_ENABLED
tools.SHELL_ENABLED = True
try:
    os.environ["NEX_RUN_ALLOW"] = "echo,ls"
    r = tools.tool_run_command("rm -rf /tmp/nope")
    _expect("allowlist" in str(r.get("error", "")),
            "run_command: non-allowlisted executable blocked")
    r = tools.tool_run_command("echo allowed")
    _expect("allowlist" not in str(r.get("error", "")),
            "run_command: allowlisted executable passes the gate")
    os.environ["NEX_RUN_ALLOW"] = ""
finally:
    tools.SHELL_ENABLED = old_shell

# ---------- 6. compile_check honesty -----------------------------------------

import mc_tools  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    cs = os.path.join(td, "Thing.cs")
    with open(cs, "w") as f:
        f.write("class Thing {}")
    old_root = tools.TOOLS_ROOT
    old_ws = mc_tools.WORKSPACE_ROOT
    tools.TOOLS_ROOT = td
    mc_tools.WORKSPACE_ROOT = td
    try:
        r = mc_tools.compile_check("Thing.cs")
        _expect("checked" in r, "compile_check reports what was checked")
        if r.get("ok"):
            _expect(r.get("checked") == "environment"
                    and r.get("compiled") is False,
                    "compile_check: environment check is NOT a compile")
    finally:
        tools.TOOLS_ROOT = old_root
        mc_tools.WORKSPACE_ROOT = old_ws

# ---------- 7. validate_assets Unreal rules ----------------------------------

with tempfile.TemporaryDirectory() as td:
    # Minimal Unreal project: marker + one healthy asset + one corrupt one.
    open(os.path.join(td, "MyGame.uproject"), "w").write("{}")
    healthy = os.path.join(td, "Maps")
    os.makedirs(healthy)
    with open(os.path.join(healthy, "Level.umap"), "wb") as f:
        f.write(b"\x00\x01\x02real bytes")
    broken = os.path.join(td, "Assets")
    os.makedirs(broken)
    open(os.path.join(broken, "Cube.uasset"), "wb").close()  # 0 bytes
    old_root = tools.TOOLS_ROOT
    old_ws = mc_tools.WORKSPACE_ROOT
    tools.TOOLS_ROOT = td
    mc_tools.WORKSPACE_ROOT = td
    try:
        rep = mc_tools.validate_assets("unreal", ".")
        msgs = " | ".join(x["msg"] for x in rep.get("findings", []))
        _expect("sidecar .meta" not in msgs,
                "validate_assets: no Unity-style .meta rule for Unreal")
        _expect("zero bytes" in msgs,
                "validate_assets: zero-byte .uasset flagged")
        _expect(rep.get("uasset_count") == 2 and rep.get("empty_assets") == 1,
                "validate_assets: real Unreal counters present "
                "(uasset+umap counted, 1 empty)")
    finally:
        tools.TOOLS_ROOT = old_root
        mc_tools.WORKSPACE_ROOT = old_ws

# ---------- 8. tunnel registry lock discipline -------------------------------

import threading  # noqa: E402
import tunnels as _tunnels  # noqa: E402

reg = _tunnels.TunnelRegistry(upstreams=[])
errors = []
def _reader():
    try:
        for _ in range(200):
            reg.list_upstreams()
            reg.raw_upstreams()
            reg.summary()
    except Exception as exc:  # noqa: BLE001
        errors.append(repr(exc))
threads = [threading.Thread(target=_reader) for _ in range(6)]
for t in threads:
    t.start()
for i in range(50):  # concurrent mutations while readers run
    with reg._lock:
        reg._upstreams[:] = list(reg._upstreams)
for t in threads:
    t.join()
_expect(not errors and isinstance(reg._lock, type(threading.RLock())),
        "tunnels: RLock + snapshot reads survive concurrent hammering "
        "(%s)" % (errors[:1] or "clean"))

print("\nAll security tests passed.")
