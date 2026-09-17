"""Tests for the MCP tunnel gateway.

Spins up a fake MCP server on an open localhost port and registers a
temporary tunnel pointing at it. Then it exercises the full client
loop:
  * initialize (with Streamable-HTTP session)
  * tools/list  — confirms the tunnel tool shows up under its prefix.
  * tools/call  — confirms it reaches the fake upstream.
  * /api/tunnels /probe — confirms the management surfaces report
    the new tunnel as reachable.
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


BASE = os.environ.get("NEX_BASE", "http://localhost:8787")


# ---------------------------------------------------------------------------
# Fake MCP upstream — speaks the 2025-06-18 protocol just enough to exercise
# the tunnel tests: initialize, tools/list, tools/call, ping.
# ---------------------------------------------------------------------------

class FakeMCPHandler(BaseHTTPRequestHandler):
    """Static MCP server. Replies from `FAKE_STATE`."""

    def log_message(self, *_a, **_kw):  # quiet
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400); return
        method = req.get("method")
        id_ = req.get("id")
        # Initialize.
        if method == "initialize":
            FAKE_STATE["initialized"] = True
            FAKE_STATE["session_id"] = "fake-sid-12345"
            payload = {
                "jsonrpc": "2.0", "id": id_,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "fake-upstream", "version": "9.9"},
                    "capabilities": {"tools": {"listChanged": False}},
                },
            }
            self._json_session(payload, FAKE_STATE["session_id"])
            return
        if method == "tools/list":
            self._json(id_, {"result": {"tools": [
                {"name": "ping_roblox",
                 "description": "Pings the fake Roblox endpoint.",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "execute_luau",
                 "description": "Runs an arbitrary Luau string.",
                 "inputSchema": {"type": "object",
                                 "properties": {"code": {"type": "string"}},
                                 "required": ["code"]}},
            ]}})
            return
        if method == "tools/call":
            name = req["params"]["name"]
            args = req["params"].get("arguments", {})
            if name == "ping_roblox":
                FAKE_STATE["ping_count"] += 1
                self._json(id_, {"result": {
                    "content": [{"type": "text",
                                 "text": json.dumps({"pong": True,
                                                      "echoed_args": args})}],
                    "isError": False,
                }})
                return
            if name == "execute_luau":
                self._json(id_, {"result": {
                    "content": [{"type": "text",
                                 "text": json.dumps({
                                     "ran_code": args.get("code", "")[:80],
                                     "ok": True})}],
                    "isError": False,
                }})
                return
            self._json(id_, {"error": {"code": -32601,
                                       "message": "unknown tool: " + name}})
            return
        if method == "ping":
            self._json(id_, {"result": {}})
            return
        if method == "notifications/initialized":
            self.send_response(204); self.end_headers(); return
        if id_ is None:
            self.send_response(204); self.end_headers(); return
        self._json(id_, {"error": {"code": -32601,
                                   "message": "unknown method"}})

    def _json(self, id_, result_or_error):
        if "result" in result_or_error:
            payload = {"jsonrpc": "2.0", "id": id_,
                       "result": result_or_error["result"]}
        else:
            payload = {"jsonrpc": "2.0", "id": id_,
                       "error": result_or_error["error"]}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json_session(self, payload, sid):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", sid)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


FAKE_STATE = {"initialized": False, "session_id": None, "ping_count": 0}


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _http_json(method, path, payload=None, headers=None):
    url = BASE + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def _expect(c, m):
    print(("ok   - " if c else "FAIL - ") + m)
    if not c:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Boot the fake upstream on a free port and register a tunnel for it via
# NEX_TUNNELS + /api/tunnels/reload hot reload.
# ---------------------------------------------------------------------------

PORT = _free_port()
os.environ["NEX_TUNNELS"] = (
    "fake-roblox=http://127.0.0.1:%d/mcp" % PORT
)
# Spin up the fake server.
srv = ThreadingHTTPServer(("127.0.0.1", PORT), FakeMCPHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
# Wait for it to come up.
for _ in range(50):
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=0.2)
        s.close(); break
    except OSError:
        time.sleep(0.05)

# Configure the runtime tunnel directly via the management API. This
# works regardless of whether the SERVER's env had NEX_TUNNELS set at
# boot, which is the right shape for tests + dev workflows.
status, body = _http_json(
    "POST", "/api/tunnels",
    {"tunnels": [
        {"name": "fake-roblox", "label": "Fake Roblox (test)",
         "url": "http://127.0.0.1:%d/mcp" % PORT}
    ], "replace": True})
_expect(status == 200, "POST /api/tunnels returns 200")
_expect([t["name"] for t in body["tunnels"]] == ["fake-roblox"],
        "registry replaced with the fake tunnel only")


try:
    # ---- 1. /api/health advertises the tunnel registry --------------------
    status, body = _http_json("GET", "/api/health")
    _expect(status == 200, "/api/health 200")
    _expect("tunnels" in body, "/api/health exposes tunnels")
    platforms = [t["name"] for t in body["tunnels"]["tunnels"]]
    _expect("fake-roblox" in platforms,
            "fake-roblox registered as a tunnel")

    # ---- 2. MCP initialize with Streamable-HTTP session ------------------
    status, body = _http_json("POST", "/mcp",
                              {"jsonrpc": "2.0", "id": 1,
                               "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18",
                                          "clientInfo":
                                              {"name": "tunnel-test",
                                               "version": "0"}}})
    _expect(status == 200, "initialize 200")
    _expect(body.get("result", {}).get("serverInfo", {}).get("name")
            == "nex", "initialize reports nex")
    _expect(body.get("result", {}).get("protocolVersion") == "2025-06-18",
            "initialize negotiates 2025-06-18")

    # ---- 3. tools/list includes the upstream tool under its prefix --------
    status, body = _http_json("POST", "/mcp",
                              {"jsonrpc": "2.0", "id": 2,
                               "method": "tools/list"})
    _expect(status == 200, "tools/list 200")
    names = {t["name"] for t in body["result"]["tools"]}
    _expect("fake-roblox.ping_roblox" in names,
            "fake-roblox.ping_roblox exposed via prefix")
    _expect("fake-roblox.execute_luau" in names,
            "fake-roblox.execute_luau exposed via prefix")
    _expect("who_am_i" in names,
            "who_am_i meta tool exposed")

    # ---- 4. tools/call routes the prefixed call to the upstream ----------
    status, body = _http_json("POST", "/mcp",
                              {"jsonrpc": "2.0", "id": 3,
                               "method": "tools/call",
                               "params": {"name": "fake-roblox.ping_roblox",
                                          "arguments": {"echo": 1}}})
    _expect(status == 200, "tools/call ping 200")
    res = body.get("result", {})
    _expect(res.get("isError") is False, "ping not error")
    text = res["content"][0]["text"]
    parsed = json.loads(text)
    _expect(parsed.get("pong") is True,
            "upstream echoed pong: " + text)
    _expect(FAKE_STATE["ping_count"] >= 1,
            "fake upstream received the call")

    # ---- 5. local tools still work (unprefixed) --------------------------
    status, body = _http_json("POST", "/mcp",
                              {"jsonrpc": "2.0", "id": 4,
                               "method": "tools/call",
                               "params": {"name": "list_platforms",
                                          "arguments": {}}})
    _expect(status == 200, "tools/call list_platforms 200")
    text = body["result"]["content"][0]["text"]
    summary = json.loads(text)
    _expect("fake-roblox" in summary.get("names", []),
            "list_platforms includes fake-roblox")
    _expect(summary["total"] >= 1, "at least one platform registered")

    # ---- 6. unknown prefixed tool returns a clean error -------------------
    status, body = _http_json("POST", "/mcp",
                              {"jsonrpc": "2.0", "id": 5,
                               "method": "tools/call",
                              "params": {"name": "bogus-unknown.tool",
                                          "arguments": {}}})
    res = body.get("result", {})
    _expect(res.get("isError") is True,
            "unknown prefixed call returns isError")

    # ---- 7. resources/list has tunnel:// entries --------------------------
    status, body = _http_json("POST", "/mcp",
                              {"jsonrpc": "2.0", "id": 6,
                               "method": "resources/list"})
    uris = [r["uri"] for r in body["result"]["resources"]]
    _expect("nex://tunnels" in uris, "nex://tunnels resource exists")
    _expect(any(u.startswith("tunnel://fake-roblox/")
                for u in uris),
            "tunnel://fake-roblox/info resource exists")

    # ---- 8. resources/read on nex://tunnels -----------------------------
    status, body = _http_json("POST", "/mcp",
                              {"jsonrpc": "2.0", "id": 7,
                               "method": "resources/read",
                               "params": {"uri": "nex://tunnels"}})
    text = body["result"]["contents"][0]["text"]
    payload = json.loads(text)
    _expect(payload["total"] >= 1, "/tunnels lists at least one platform")

    # ---- 9. prompts/get response shape -----------------------------------
    status, body = _http_json("POST", "/mcp",
                              {"jsonrpc": "2.0", "id": 8,
                               "method": "prompts/get",
                               "params": {"name": "tunnel_diagnose",
                                          "arguments":
                                              {"platform": "fake-roblox"}}})
    _expect("messages" in body.get("result", {}),
            "tunnel_diagnose prompt returns messages")

    # ---- 10. /api/tunnels/probe finds the fake ----------------------------
    status, body = _http_json("POST", "/api/tunnels/probe",
                              {"platforms": ["fake-roblox"]})
    _expect(status == 200, "/api/tunnels/probe 200")
    probed = body["probed"]
    _expect(probed and probed[0]["platform"] == "fake-roblox",
            "fake-roblox is probed")
    _expect(probed[0]["ok"] is True,
            "fake-roblox probe reports ok")

    # ---- 11. /api/tunnels includes the fake as reachable ------------------
    status, body = _http_json("GET", "/api/tunnels")
    found = [t for t in body["tunnels"] if t["name"] == "fake-roblox"]
    _expect(found and found[0]["initialized"],
            "fake-roblox flagged initialized in /api/tunnels")
    _expect(found and found[0]["server_info"].get("name") == "fake-upstream",
            "fake upstream reported its own server name")

    # ---- 12. forwarding the session id works ------------------------------
    sid = "fake-sid-12345"
    status, body = _http_json(
        "POST", "/mcp",
        {"jsonrpc": "2.0", "id": 12, "method": "ping"},
        headers={"Mcp-Session-Id": sid},
    )
    _expect(status == 200, "session-tagged ping returns 200")

    # ---- 13. /api/tunnels/stdio-config returns per-client snippets --------
    status, body = _http_json(
        "GET", "/api/tunnels/stdio-config?path=/path/to/nex-stdio")
    _expect(status == 200, "stdio-config 200")
    snips = body["snippets"]
    _expect("claude_desktop" in snips and
            "mcpServers" in snips["claude_desktop"] and
            snips["claude_desktop"]["mcpServers"]["nex"]["command"]
                == "/path/to/nex-stdio",
            "claude_desktop uses /path/to/nex-stdio")
    _expect("vscode_copilot" in snips and
            "servers" in snips["vscode_copilot"],
            "vscode_copilot uses root key `servers`")
    _expect("codex_cli" in snips and
            "_toml" in snips["codex_cli"] and
            "[mcp_servers.nex]" in snips["codex_cli"]["_toml"],
            "codex_cli uses TOML with [mcp_servers.nex]")
    _expect("roblox_studio_official" in snips,
            "stdio-config mentions Roblox Studio stdio command")
    _expect("command" in snips["roblox_studio_official"]["mcpServers"]
            ["Roblox_Studio"],
            "roblox stdio config has a command")
    _expect(body["platform"] in ("win32", "linux", "darwin"),
            "stdio-config reports current OS platform")

    print("\n14 passed, 0 failed.\nAll MCP tunnel tests passed.")
finally:
    srv.shutdown()
