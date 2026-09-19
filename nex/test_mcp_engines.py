"""Tests for the MCP-only Roblox Studio / Unreal Engine tool-guide adapter.

Run as:
    python3 test_mcp_engines.py

Covers the curated adapter (mcp_engines.py) plus the live wiring into the
MCP server (resources + prompts). Pure stdlib; boots an isolated server
for the wiring checks, mirroring test_mc.py.
"""

import importlib
import json
import os
import socket
import tempfile
import subprocess
import sys
import time
import urllib.error
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
sys.path.insert(0, HERE)


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------------------
# 1. Module-level adapter tests (no server needed).
# ---------------------------------------------------------------------------

mcp_engines = importlib.import_module("mcp_engines")

# 1a. Supported platforms.
_expect(set(mcp_engines.SUPPORTED) == {"roblox-studio", "unreal-engine"},
        "SUPPORTED is exactly roblox-studio + unreal-engine")

# 1b. normalize_platform maps aliases.
_expect(mcp_engines.normalize_platform("roblox") == "roblox-studio",
        "normalize_platform('roblox') -> roblox-studio")
_expect(mcp_engines.normalize_platform("unreal") == "unreal-engine",
        "normalize_platform('unreal') -> unreal-engine")
_expect(mcp_engines.normalize_platform("nope") is None,
        "normalize_platform('nope') -> None")

# 1c. find_guide matches canonical names + aliases + prefixes.
_expect(mcp_engines.find_guide("roblox-studio", "execute_luau") is not None,
        "find_guide matches canonical execute_luau")
_expect(mcp_engines.find_guide("roblox-studio", "run_luau") is not None,
        "find_guide matches alias run_luau")
_expect(mcp_engines.find_guide("roblox-studio",
                               "roblox-studio.execute_luau") is not None,
        "find_guide matches platform-prefixed name")
_expect(mcp_engines.find_guide("unreal-engine", "spawn_actor") is not None,
        "find_guide matches canonical spawn_actor")
_expect(mcp_engines.find_guide("unreal-engine", "create_actor") is not None,
        "find_guide matches alias create_actor")
_expect(mcp_engines.find_guide("roblox-studio", "totally_unknown_tool")
        is None,
        "find_guide returns None for unknown tool")

# 1d. enrich_upstream_tools keeps name/schema, upgrades description, flags match.
raw = {"name": "execute_luau",
       "description": "Run a script",
       "inputSchema": {"type": "object",
                       "properties": {"code": {"type": "string"}}}}
enriched = mcp_engines.enrich_upstream_tools("roblox-studio", raw)
_expect(enriched["name"] == "execute_luau",
        "enrich keeps the tool name")
_expect(enriched["inputSchema"] == raw["inputSchema"],
        "enrich keeps inputSchema")
_expect("execute_luau" in enriched["description"],
        "enrich description mentions the tool")
_expect("Example" in enriched["description"],
        "enrich description includes an Example section")
_expect(enriched.get("_nex_matched") is True,
        "enrich flags _nex_matched=True for a known tool")

# 1e. Enriching an unknown tool still produces a safe description + keeps name.
unknown = {"name": "frobnicate", "description": "", "inputSchema": {}}
eu = mcp_engines.enrich_upstream_tools("unreal-engine", unknown)
_expect(eu["name"] == "frobnicate",
        "enrich keeps name for unknown tool")
_expect("NEX" in eu["description"],
        "enrich adds a generic safety note for unknown tools")
_expect(eu.get("_nex_matched") is False,
        "enrich flags _nex_matched=False for unknown tool")

# 1f. explain_tool for one tool returns structured guide + markdown.
et = mcp_engines.explain_tool("roblox-studio", "execute_luau")
_expect(et["found"] is True and et["tool"] == "execute_luau",
        "explain_tool returns found guide")
_expect("**Parameters**" in et["markdown"],
        "explain_tool markdown lists Parameters")
_expect("**Caveats**" in et["markdown"],
        "explain_tool markdown lists Caveats")

# 1g. platform_guide_resource returns non-empty markdown with every tool.
guide = mcp_engines.platform_guide_resource("unreal-engine")
_expect(isinstance(guide, str) and "spawn_actor" in guide,
        "unreal guide resource documents spawn_actor")
_expect("execute_python" in guide,
        "unreal guide resource documents execute_python")
_expect("How to connect" in guide,
        "unreal guide resource includes connection steps")
_expect(mcp_engines.platform_guide_resource("nope") is None,
        "platform_guide_resource returns None for unknown platform")

# 1h. build_recipe_prompt returns intent-shaped text.
recipe = mcp_engines.build_recipe_prompt("roblox-studio",
                                         "add a red spinning coin")
_expect("red spinning coin" in recipe,
        "build_recipe_prompt embeds the intent")
_expect("roblox-studio" in recipe,
        "build_recipe_prompt references the namespaced prefix")

# 1i. explain_platform lists tools.
ep = mcp_engines.explain_platform("roblox-studio")
_expect(ep["found"] is True and len(ep["tools"]) > 5,
        "explain_platform lists the Roblox tools")


# ---------------------------------------------------------------------------
# 2. Live wiring: boot an isolated server and verify resources + prompts.
# ---------------------------------------------------------------------------

proc = None
BASE = None
for _attempt in range(5):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = os.environ.copy()
    env["NEX_HOST"] = "127.0.0.1"
    env["NEX_PORT"] = str(port)
    env["NEX_OBSERVER_DISABLED"] = "1"
    env["NEX_AUTH_TOKEN"] = "engines-test-token"
    env["PYTHONPATH"] = HERE
    proc = subprocess.Popen([PYTHON, os.path.join(HERE, "server.py")],
                            env=env, cwd=HERE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 8
    ready = False
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/health" % port)
            req.add_header("X-Nex-Auth", "engines-test-token")
            with urllib.request.urlopen(req, timeout=1) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.3)
    if ready:
        BASE = "http://127.0.0.1:%d" % port
        break
    proc.terminate()
    proc.wait(timeout=3)
    proc = None

_expect(BASE is not None, "isolated server boots for MCP wiring checks")
port = int(BASE.rsplit(":", 1)[1])


def _mcp(payload, timeout=8):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + "/mcp", data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Nex-Auth": "engines-test-token"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


# 2a. initialize + prompts/list includes the new engine prompts.
init = _mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}})
_expect("result" in init, "initialize ok")

prompts = _mcp({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
pnames = {p["name"] for p in prompts["result"]["prompts"]}
for want in ("roblox_explain_tools", "unreal_explain_tools",
             "roblox_build_recipe", "unreal_build_recipe"):
    _expect(want in pnames, "prompts/list exposes %s" % want)

# 2b. prompts/get for roblox_explain_tools returns a markdown guide.
got = _mcp({"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
            "params": {"name": "roblox_explain_tools",
                       "arguments": {"tool": "execute_luau"}}})
msg = got["result"]["messages"][0]["content"]
_expect("execute_luau" in msg, "roblox_explain_tools prompt returns the guide")

# 2c. resources/list includes the curated guide URIs.
res = _mcp({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
uris = {r["uri"] for r in res["result"]["resources"]}
_expect("mcp://roblox-studio/guide" in uris,
        "resources/list exposes mcp://roblox-studio/guide")
_expect("mcp://unreal-engine/guide" in uris,
        "resources/list exposes mcp://unreal-engine/guide")

# 2d. resources/read returns the guide markdown.
rread = _mcp({"jsonrpc": "2.0", "id": 5, "method": "resources/read",
              "params": {"uri": "mcp://unreal-engine/guide"}})
body = rread["result"]["contents"][0]["text"]
_expect("spawn_actor" in body, "resources/read of unreal guide documents tools")
_expect(rread["result"]["contents"][0]["mimeType"] == "text/markdown",
        "unreal guide resource is text/markdown")


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

try:
    proc.terminate()
    proc.wait(timeout=3)
except Exception:
    try:
        proc.kill()
    except Exception:
        pass
if proc and proc.stderr:
    err = proc.stderr.read().decode("utf-8", "replace")
    if err.strip():
        print("--- server stderr ---")
        print(err[-1500:])
        print("--- end stderr ---")

print("\nAll mcp_engines tests passed.")
