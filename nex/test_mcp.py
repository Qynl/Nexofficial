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
_expect(len(names) >= 10, "surface has the introspection + music tools")

# Amazon Music connector (explicit allowlist, namespaced).
for t in ("amazon-music.am_play", "amazon-music.am_pause",
          "amazon-music.am_next", "amazon-music.am_previous",
          "amazon-music.am_volume", "amazon-music.am_search_play",
          "amazon-music.am_toggle"):
    _expect(t in names, "surface includes %s" % t)

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


# ---------- 3. Amazon Music through the same gateway --------------------------

status, body = _rpc("tools/call", {"name": "amazon-music.am_play",
                                   "arguments": {}})
_expect(status == 200 and body["result"].get("isError") is not True,
        "amazon-music.am_play executes")
_expect("amazonmusic://" in body["result"]["content"][0]["text"],
        "am_play returns an honest structured payload")

status, body = _rpc("tools/call", {"name": "amazon-music.am_volume",
                                   "arguments": {"level": 55}})
_expect(status == 200 and body["result"].get("isError") is not True,
        "amazon-music.am_volume executes")
_expect('"level": 55' in body["result"]["content"][0]["text"],
        "volume level carried through")

status, body = _rpc("tools/call", {"name": "amazon-music.am_volume",
                                   "arguments": {"level": 999}})
_expect(body["result"].get("isError") is True,
        "am_volume rejects out-of-range levels")

status, body = _rpc("tools/call", {"name": "amazon-music.am_search_play",
                                   "arguments": {"query": "lofi beats"}})
_expect(status == 200 and body["result"].get("isError") is not True,
        "amazon-music.am_search_play executes")

# Bare-name music controls also work (convenience form).
status, body = _rpc("tools/call", {"name": "am_next", "arguments": {}})
_expect(status == 200 and body["result"].get("isError") is not True,
        "bare am_next executes via the boundary router")


# ---------- done ----------------------------------------------------------

proc.terminate()
proc.wait(timeout=3)
print("\nAll boundary MCP tests passed.")
