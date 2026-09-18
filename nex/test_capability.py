"""Tests for the capability classification model (STAGE 2/3)."""

import importlib
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

cap = importlib.import_module("mcp.capability")
policy_mod = importlib.import_module("mcp.policy")


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# --- explicit annotations win ------------------------------------------------
ann = {"readOnlyHint": True, "destructiveHint": False}
c = cap.classify_capability("get_scene", annotations=ann)
_expect(c.category == cap.READ, "explicit readOnlyHint -> READ")
_expect(c.read_only is True, "explicit read_only honored")
_expect(c.source == "explicit", "source is 'explicit'")
_expect(c.requires_confirmation is False, "read-only tool needs no confirmation")

ann2 = {"destructiveHint": True}
c2 = cap.classify_capability("delete_actor", annotations=ann2)
_expect(c2.category == cap.DESTRUCTIVE, "explicit destructiveHint -> DESTRUCTIVE")
_expect(c2.requires_confirmation is True, "destructive requires confirmation")

# --- heuristic fallback ------------------------------------------------------
h = cap.classify_capability("spawn_actor")
_expect(h.category == cap.CREATE, "heuristic: spawn_* -> CREATE")
_expect(h.source == "heuristic", "fallback source is 'heuristic'")
_expect(h.read_only is False, "create is not read_only")

h2 = cap.classify_capability("delete_thing")
_expect(h2.category == cap.DESTRUCTIVE, "heuristic: delete_* -> DESTRUCTIVE")

h3 = cap.classify_capability("list_files")
_expect(h3.category == cap.READ, "heuristic: list_* -> READ")

# --- conservative default for unknown --------------------------------------
u = cap.classify_capability("do_mystery_thing")
_expect(u.category == cap.UNKNOWN, "unknown tool -> UNKNOWN")
_expect(u.requires_confirmation is True, "unknown tool requires confirmation (conservative)")
_expect(u.source == "conservative", "source is 'conservative'")

# --- capability_for_tool reads tool dict ------------------------------------
tool = {"name": "get_asset", "annotations": {"readOnlyHint": True}}
c3 = cap.capability_for_tool(tool)
_expect(c3.read_only is True, "capability_for_tool reads annotations")


# --- POLICY / MCP-only enforcement (STAGE 4/18) ----------------------------
Policy = policy_mod.Policy

# Default policy: permissive, but destructive needs confirmation.
p = Policy()
d = policy_mod.authorize("unreal-engine", "delete_actor",
                        cap.classify_capability("delete_actor"))
_expect(d.allowed is True, "destructive allowed by default policy")
_expect(d.requires_confirmation is True, "destructive requires confirmation")

# THE BOUNDARY: internal filesystem tools are NOT AI capabilities anymore —
# denied even as "internal", because they are infrastructure, not surface.
d2 = policy_mod.authorize(None, "write_file", cap.classify_capability("write_file"))
_expect(d2.allowed is False, "boundary: internal write_file DENIED (infra, not capability)")
d2b = policy_mod.authorize(None, "read_file", cap.classify_capability("read_file"))
_expect(d2b.allowed is False, "boundary: internal read_file DENIED")
d2c = policy_mod.authorize(None, "detect_engines", cap.classify_capability("detect_engines"))
_expect(d2c.allowed is False, "boundary: host scanning DENIED")
# MCP introspection stays.
d2d = policy_mod.authorize(None, "who_am_i", cap.classify_capability("who_am_i"))
_expect(d2d.allowed is True, "boundary: who_am_i allowed (MCP introspection)")
# Amazon Music allowlist (bare form).
d2e = policy_mod.authorize(None, "am_play", cap.classify_capability("am_play"))
_expect(d2e.allowed is True, "boundary: am_play allowed (explicit connector)")

# MCP-only blocks shell, always.
p_mcp = Policy(mcp_only=True)
d3 = policy_mod.authorize(None, "run_command", cap.classify_capability("run_command"))
_expect(d3.allowed is False, "MCP-only blocks run_command (shell)")

# MCP-only still allows connected upstream tools (that is the whole point).
d4 = policy_mod.authorize("unreal-engine", "spawn_actor",
                          cap.classify_capability("spawn_actor"), p_mcp)
_expect(d4.allowed is True, "MCP-only allows external MCP tools")

# Server allow-list.
p_al = Policy(server_allowlist={"roblox-studio"})
d5 = policy_mod.authorize("unreal-engine", "spawn_actor",
                          cap.classify_capability("spawn_actor"), p_al)
_expect(d5.allowed is False, "server not in allow-list is denied")
d6 = policy_mod.authorize("roblox-studio", "execute_luau",
                         cap.classify_capability("execute_luau"), p_al)
_expect(d6.allowed is True, "server in allow-list is allowed")

# Tool allow-list narrows per server.
p_ta = Policy(tool_allowlist={"unreal-engine": {"spawn_actor"}})
d7 = policy_mod.authorize("unreal-engine", "delete_actor",
                          cap.classify_capability("delete_actor"), p_ta)
_expect(d7.allowed is False, "tool not in server tool-allow-list is denied")


# --------------------------------------------------------------------------
# Amazon Music connector invariants: the allowlist is EXACTLY the 7 controls,
# every op is bounded to the amazonmusic:// scheme, invalid args are refused,
# and the connector is the ONLY non-MCP capability.
# --------------------------------------------------------------------------
import music_amazon as m  # noqa: E402

_expect({t[0] for t in m.MUSIC_TOOLS} == {
    "am_play", "am_pause", "am_toggle", "am_next", "am_previous",
    "am_volume", "am_search_play"},
    "music allowlist is exactly the 7 controls")

for op in ("am_play", "am_pause", "am_toggle", "am_next", "am_previous"):
    r = m.call_tool(op, {})
    _expect(r.get("ok") is True and r["link"].startswith("amazonmusic://"),
            "music: %s bounded to amazonmusic:// scheme" % op)

_expect(m.call_tool("am_volume", {"level": 300})
        == {"ok": False, "error": "volume must be 0..100"},
        "music: volume >100 refused")
_expect(m.call_tool("am_volume", {"level": -1})
        == {"ok": False, "error": "volume must be 0..100"},
        "music: negative volume refused")
_expect("error" in m.call_tool("am_search_play",
                               {"query": "podcast" * 99}),
        "music: oversized query refused")
_expect("unknown Amazon Music control" in
        m.call_tool("rm_rf", {})["error"],
        "music: non-allowlisted control refused")
r = m.call_tool("am_search_play", {"query": "AC/DC"})
_expect(r["ok"] is True and r["link"] == "amazonmusic://search/AC%2FDC",
        "music: search query is URL-quoted into the deep link")

# Connector duck-types Upstream for the agent registry.
up = m.MusicUpstream()
agent_registry = importlib.import_module("agent.registry")
reg_m = agent_registry.CapabilityRegistry.from_upstreams([up])
_expect([v.name for v in reg_m.all_tools()]
        == [t[0] for t in m.MUSIC_TOOLS],
        "music: agent registry sees exactly the 7 controls")
_expect(reg_m.by_name("write_file") is None,
        "music: agent registry has NO filesystem tools")


print("\nAll capability/policy tests passed.")
