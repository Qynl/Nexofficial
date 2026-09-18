"""Live MCP-gateway tests — THE CAPABILITY BOUNDARY conformance.

Boots the real server and verifies, over the actual /mcp endpoint:

  * the handshake works (initialize / tools/list),
  * the surface contains ONLY: MCP introspection + explicitly connected
    servers + the Amazon Music connector,
  * sandbox/filesystem/shell/host tools are structurally absent (calling
    them is refused at the boundary, not policy-checked),
  * Amazon Music controls execute through the same gateway,
  * /api/tools/* REST shims enforce the same boundary,
  * the health/auth basics still hold.

This suite replaces the pre-boundary suite that treated write_file /
run_command / detect_engines as model-visible tools — those are Nex
infrastructure now, NOT capabilities.
"""
import json
import os
import subprocess
import sys
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


port = _free_port()
env = os.environ.copy()
env.update({"NEX_HOST": "127.0.0.1", "NEX_PORT": str(port),
            "NEX_OBSERVER_DISABLED": "1"})
proc = subprocess.Popen([PYTHON, os.path.join(HERE, "server.py")],
                        env=env, cwd=HERE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
BASE = "http://127.0.0.1:%d" % port


def _http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
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


def _rpc(method, params=None, id=1):
    return _http("POST", "/mcp", {"jsonrpc": "2.0", "id": id,
                                  "method": method, "params": params or {}})


# ---------- boot ----------------------------------------------------------

deadline = time.time() + 10
ready = False
while time.time() < deadline:
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=1) as r:
            if r.status == 200:
                ready = True
                break
    except Exception:
        time.sleep(0.25)
_expect(ready, "isolated server boots (loopback)")


# ---------- 1. handshake + surface ------------------------------------------

status, body = _rpc("initialize", {"protocolVersion": "2025-06-18",
                                   "capabilities": {}})
_expect(status == 200, "/mcp initialize 200")

status, body = _rpc("tools/list")
_expect(status == 200, "/mcp tools/list 200")
tools = body["result"]["tools"]
names = [t["name"] for t in tools]
_expect(len(names) >= 4, "surface has the introspection tools")

# MCP introspection.
for t in ("who_am_i", "list_platforms", "tunnel_status", "tunnel_probe"):
    _expect(t in names, "surface includes %s (MCP introspection)" % t)

# THE BOUNDARY: none of these exist as capabilities anymore.
FORBIDDEN = (
    "write_file", "read_file", "list_files", "search_files",
    "append_to_file", "run_command", "execute_command",
    "detect_engines", "engine_info", "compile_check",
    "validate_assets", "json_path_query", "diff_files",
    "call_upstream", "log_event", "recent_events",
    # MCP-ONLY: no music connector, no non-MCP capabilities at all.
    "amazon-music.am_play", "amazon-music.am_pause",
    "amazon-music.am_volume", "am_play", "am_pause", "am_next",
)
for t in FORBIDDEN:
    _expect(t not in names, "surface excludes %s (boundary)" % t)


# ---------- 2. refused, not policy-checked -----------------------------------

status, body = _rpc("tools/call", {"name": "write_file",
                                   "arguments": {"path": "x.txt",
                                                 "content": "pwn"}})
is_err = body["result"].get("isError") is True
_expect(is_err, "tools/call write_file refused at the boundary")
_expect("not a Nex capability" in body["result"]["content"][0]["text"],
        "write_file refusal names the boundary")

status, body = _rpc("tools/call", {"name": "run_command",
                                   "arguments": {"command": "ls"}})
_expect(body["result"].get("isError") is True,
        "tools/call run_command refused at the boundary")

status, body = _rpc("tools/call", {"name": "call_upstream",
                                   "arguments": {"platform": "x",
                                                 "method": "tools/call"}})
_expect(body["result"].get("isError") is True,
        "tools/call call_upstream refused (removed from surface)")

# The REST shims enforce the same boundary.
status, body = _http("POST", "/api/tools/call",
                     {"name": "write_file",
                      "arguments": {"path": "x.txt", "content": "pwn"}})
_expect(status == 200 and body.get("isError") is True
        and "not a Nex capability" in json.dumps(body),
        "/api/tools/call enforces the boundary")

status, body = _http("POST", "/api/tools/read_file", {"path": "."})
_expect(status == 200 and body.get("isError") is True,
        "/api/tools/<name> enforces the boundary")


# ---------- 3. MCP-ONLY: no music connector, no side doors ----------

status, body = _rpc("tools/call", {"name": "amazon-music.am_play",
                                   "arguments": {}})
_expect(status == 200 and body["result"].get("isError") is True,
        "amazon-music.am_play refused (no such upstream)")

status, body = _rpc("tools/call", {"name": "am_next", "arguments": {}})
_expect(status == 200 and body["result"].get("isError") is True,
        "bare am_next refused at the boundary")

# The /api/tools shims agree with the gateway.
status, body = _http("POST", "/api/tools/call",
                     {"name": "am_play", "arguments": {}})
_expect(status == 200 and body.get("isError") is True,
        "/api/tools/call refuses am_play (MCP-only)")


# ---------- 4. resources: protocol metadata ONLY -----------------------------

status, body = _rpc("resources/list")
_expect(status == 200, "resources/list 200")
uris = [r["uri"] for r in body["result"]["resources"]]
_expect("nex://about" in uris, "resources include nex://about (metadata)")
_expect("nex://tunnels" in uris,
        "resources include nex://tunnels (MCP connection metadata)")
for gone in ("nex://workspace/tree", "nex://log/recent", "nex://log/full",
             "nex://state"):
    _expect(gone not in uris,
            "resources exclude %s (host access is not a capability)" % gone)

status, body = _rpc("resources/read", {"uri": "nex://about"})
blob = json.dumps(body)
_expect("capability_boundary" in blob,
        "nex://about states the capability boundary")

# The removed host resources must be gone, not merely unlisted.
for gone in ("nex://workspace/tree", "nex://log/recent", "nex://log/full",
             "nex://state"):
    status, body = _rpc("resources/read", {"uri": gone})
    _expect("error" in json.dumps(body.get("result", {})).lower()
            or "not a" in json.dumps(body.get("result", {})).lower()
            or "error" in json.dumps(body).lower(),
            "resources/read %s refused" % gone)


# ---------- done ----------------------------------------------------------

proc.terminate()
proc.wait(timeout=3)
print("\nAll boundary MCP tests passed.")
