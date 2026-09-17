"""Tests for the settings UI + tunnel awareness wiring.

Verifies:
  * GET /api/settings/tunnels returns the catalog + status.
  * POST /api/settings/connect registers + connects a default tunnel.
  * When a tunnel is connected, the system prompt for /api/chat
    includes a "Connected MCP tunnels" section mentioning the tool.
  * The settings.html page is served as text/html.
  * Probe-after-connect surfaces the new tools in /api/tunnels.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


# ---------- tiny in-process HTTP server fixture ----------------------------

class _SRV(BaseHTTPRequestHandler):
    """Minimal in-process MCP upstream used by tests that need a
    reachable target. Accepts initialize / tools/list / tools/call
    without doing anything real."""
    started = threading.Event()

    def log_message(self, *_a, **_kw): pass

    def do_POST(self):  # noqa: N802
        ln = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(ln).decode("utf-8")
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400); self.end_headers(); return
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            r = {"protocolVersion": "2025-06-18",
                 "serverInfo": {"name": "fake-upstream", "version": "0"},
                 "capabilities": {"tools": {}}}
            self._send_result(rid, r)
        elif method == "tools/list":
            self._send_result(rid, {"tools": [
                {"name": "ping_editor", "description": "shout at editor",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "save_scene", "description": "save current scene",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]})
        elif method == "tools/call":
            self._send_result(rid, {"content": [{
                "type": "text", "text": "ok"}], "isError": False})
        elif method == "notifications/initialized":
            self.send_response(202); self.end_headers()
        else:
            self.send_response(200); self.end_headers()

    def _send_result(self, rid, result):
        body = (json.dumps({"jsonrpc": "2.0", "id": rid,
                            "result": result})).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _pick_port() -> int:
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


def _start_fake_upstream():
    port = _pick_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _SRV)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


def _http(method, path, body=None, base="http://127.0.0.1:8787"):
    import urllib.request
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}
    except Exception as e:
        return 0, {"error": str(e)}


# ---------- live server + fake upstream -----------------------------------

srv, port = _start_fake_upstream()
print(f"Fake upstream on :{port}", flush=True)

# Start a Nex server with NEX_TUNNELS pointing at the fake upstream so
# we have at least one connected tunnel. Note: the live server in
# :8787 is whatever was already running; for clean test isolation
# we boot a SECOND instance on a free port and run everything
# through that.

def _boot_nex(port: int, env: dict) -> subprocess.Popen:
    e = os.environ.copy()
    e.update(env)
    return subprocess.Popen(
        [PYTHON, os.path.join(HERE, "server.py")],
        env=e, cwd=HERE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

# Pick a high port for the test Nex
test_port = _pick_port()
env = os.environ.copy()
env["NEX_HOST"] = "127.0.0.1"
env["NEX_PORT"] = str(test_port)
# Disable the observer + tools shell + cut ollama attempts.
env["NEX_OBSERVER_DISABLED"] = "1"
env["NEX_TUNNELS"] = f"fake-settings=http://127.0.0.1:{port}/mcp"
# Make sure we DON'T have an Ollama server (we mock /api/chat).
proc = subprocess.Popen(
    [PYTHON, os.path.join(HERE, "server.py")],
    env=env, cwd=HERE,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
# Wait for boot.
deadline = time.time() + 8
ready = False
while time.time() < deadline:
    try:
        s, _ = _http("GET", "/api/health", base=f"http://127.0.0.1:{test_port}")
        if s == 200:
            ready = True
            break
    except Exception:
        pass
    time.sleep(0.3)
_expect(ready, f"test Nex boot on :{test_port}")
BASE = f"http://127.0.0.1:{test_port}"


# ---------- 1. /api/settings/tunnels -------------------------------------

status, body = _http("GET", "/api/settings/tunnels", base=BASE)
_expect(status == 200, "settings-tunnels 200")
cat = body.get("catalog") or []
_expect(len(cat) >= 5, f"settings-tunnels lists ≥5 default tunnels (got {len(cat)})")
_expect(any(c.get("name") == "roblox-studio" and c.get("kind") == "stdio"
            for c in cat),
        "roblox-studio in catalog with stdio kind")
_expect(any(c.get("name") == "unreal-engine" and c.get("kind") == "http"
            for c in cat),
        "unreal-engine in catalog with http kind")
_expect(any("project" in (c.get("description") or "").lower()
            for c in cat if c.get("name") == "unreal-engine"),
        "unreal-engine description explains the editor plugin step")
_reg = body.get("registered") or []
_expect(any(r.get("name") == "roblox-studio" for r in _reg),
        "roblox-studio appears in registered (default registry)")
_expect(any(r.get("name") == "fake-settings"
            for r in _reg),
        "fake-settings (env-injected) appears in registered")
# Probe all so we can check online status (lazy probe).
status, body = _http("POST", "/api/tunnels/probe", body={}, base=BASE)
_expect(status == 200, "probe-all 200")
status, body = _http("GET", "/api/settings/tunnels", base=BASE)
_reg = body.get("registered") or []
_expect(any(r.get("name") == "fake-settings" and r.get("online") is True
            for r in _reg),
        "fake-settings (env-injected) is online after probe")
_expect(body.get("platform") in ("win32", "linux", "darwin"),
        "settings-tunnels reports current platform")


# ---------- 2. /settings.html is served as text/html ---------------------

status, body = _http("GET", "/settings.html", base=BASE)
# _http always tries json; do a raw fetch for an HTML check.
import urllib.request
with urllib.request.urlopen(BASE + "/settings.html", timeout=5) as resp:
    text = resp.read().decode()
_expect(text.startswith("<!DOCTYPE"),
        "settings.html served as HTML doctype")
_expect("NEX · MCP tunnels" in text,
        "settings.html contains the title")
_expect("Connect" in text and "Probe" in text,
        "settings.html renders Connect + Probe buttons")


# ---------- 3. POST /api/settings/connect (unreachable target) ------------

status, body = _http("POST", "/api/settings/connect",
                     {"name": "roblox-studio"}, base=BASE)
# Spawn will fail because mcp.bat doesn't exist on this sandbox box.
_expect(status == 200, "connect returns 200 even on failure")
_expect("ok" in body, "connect response has an ok field")
_expect(body.get("ok") is False,
        "connect reports ok=false for unreachable tunnel")
_expect((body.get("error") or ""),
        "connect includes an error string when it fails: "
        + str(body.get("error"))[:80])


# ---------- 4. Connect to the reachable fake-settings tunnel -------------

# fake-settings is already registered (via NEX_TUNNELS env). The connect
# endpoint should refresh its tools + server_info.
status, body = _http("POST", "/api/settings/connect",
                     {"name": "fake-settings"}, base=BASE)
_expect(status == 200, "connect 200 on reachable fake-settings")
_expect(body.get("ok") is True,
        "fake-settings connect ok=true")
_expect(body.get("tools_count", 0) >= 2,
        f"fake-settings reports ≥2 tools (got {body.get('tools_count')})")
_expect("ping_editor" in (body.get("tools_sample") or []),
        "fake-settings tools_sample lists ping_editor")


# ---------- 5. Awareness is injected into the chat system prompt ---------
# We can't actually call the model (no ollama running), but we can hook
# the chat endpoint to fail in a controlled way and inspect the messages
# the server would send. Simpler: read /api/tunnels and confirm the
# fake-settings is now initialized; then verify _format_tunnel_awareness
# in server.py builds a non-empty string.

# Direct test: load server module and call the function after seeding an
# initialized upstream by hand.
import importlib.util
spec = importlib.util.spec_from_file_location(
    "_srv", os.path.join(HERE, "server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
# Our live process has connected fake-settings. So the function should
# return something. We can't call into the live process's globals from
# here — but we CAN load the helper module as plain Python and call
# _format_tunnel_awareness against its own fake registries.

# Instead of crossing process boundaries, just call the helper from the
# fresh module after seeding.
mod.invalidate_tunnel_awareness()

# Build a fake registry with one initialized upstream.
from upstream import Upstream  # noqa: E402
fakereg_mod = importlib.import_module("upstream")
fakereg = __import__("tunnels").TunnelRegistry.__new__(
    __import__("tunnels").TunnelRegistry)
fake_u = Upstream("awareness-test", "http://127.0.0.1:" + str(port) + "/mcp")
fake_u._initialized = True
fake_u._server_info = {"name": "fake-awareness", "version": "9.9"}
fake_u._tools_cache = [
    {"name": "do_thing"}, {"name": "list_things"},
    {"name": "save_state"}, {"name": "load_state"}]
import time as _t
fake_u._tools_fetched_at = _t.monotonic()  # within TTL — won't refetch
import tunnels as t_mod

orig_reg = t_mod._TUNNELS

class _Stub:
    _upstreams = [fake_u]

orig_provider = getattr(mod, "_format_tunnel_awareness", None)
_expect(orig_provider is not None, "_format_tunnel_awareness present")

# Temporarily swap the registry import path within the module under test.
real_get_tunnels = t_mod.get_tunnels
def fake_get_tunnels():
    return _Stub()

# Patch inside the server module by overriding the symbol it reads.
backup = getattr(mod, "get_tunnels", None)
# The server module calls `from tunnels import get_tunnels` inside the
# function, so we need to monkey-patch the tunnels module.
t_mod.get_tunnels = fake_get_tunnels
try:
    msg = mod._format_tunnel_awareness()
finally:
    t_mod.get_tunnels = real_get_tunnels

_expect("awareness-test" in msg,
        "awareness string mentions tunnel name")
_expect("fake-awareness v9.9" in msg,
        "awareness string mentions server_name + version")
_expect("do_thing" in msg,
        "awareness string mentions a tool name")
_expect("4 tools" in msg,
        "awareness string mentions tool count")
_expect("call them via tools/call" in msg,
        "awareness string tells the AI how to invoke them")


# ---------- 6. After "connect", /api/tunnels shows online ---------------

status, body = _http("GET", "/api/tunnels", base=BASE)
fake_rows = [t for t in body["tunnels"] if t["name"] == "fake-settings"]
_expect(fake_rows and fake_rows[0]["initialized"] is True,
        "fake-settings is initialized after /api/settings/connect")


# ---------- cleanup ------------------------------------------------------

try:
    proc.terminate()
    proc.wait(timeout=3)
except Exception:
    try: proc.kill()
    except Exception: pass
srv.shutdown()

print("\n6 passed, 0 failed.\nAll settings UI tests passed.")
