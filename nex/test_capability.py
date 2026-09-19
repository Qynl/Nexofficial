"""Tests for the capability classification model (STAGE 2/3).

This file used to be an empty shell (it printed "all passed" without a
single assertion) even though capability/policy is the core security
model. It now actually exercises:
  * keyword-heuristic classification per category,
  * explicit MCP annotations beating the heuristics,
  * conservative defaults for unknown tools,
  * mcp.policy: ALWAYS_DENIED, the internal boundary, external
    authorization, confirmation requirements, allow-lists, and the
    non-configurable MCP_ONLY constant.
"""

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


# ---------- 1. heuristic classification -----------------------------------

c = cap.classify_capability("read_file")
_expect(c.category == cap.READ, "read_file classified READ by heuristic")
_expect(c.read_only is True, "read_file read_only=True")

c = cap.classify_capability("create_asset")
_expect(c.category == cap.CREATE, "create_asset classified CREATE")

c = cap.classify_capability("set_actor_transform")
_expect(c.category == cap.MODIFY, "set_actor_transform classified MODIFY")

c = cap.classify_capability("build")
_expect(c.category == cap.BUILD, "build classified BUILD")

c = cap.classify_capability("delete_actor")
_expect(c.category == cap.DESTRUCTIVE, "delete_actor classified DESTRUCTIVE")
_expect(c.destructive is True, "delete_actor destructive=True")

c = cap.classify_capability("fetch_url")
_expect(c.category == cap.NETWORK, "fetch_url classified NETWORK")
_expect(c.network is True, "fetch_url network=True")

# Unknown verb -> unknown category, CONSERVATIVELY requires confirmation.
# (Name must avoid every heuristic hint — "widget" contains "get" and
# would classify READ.)
c = cap.classify_capability("zorken_quibble")
_expect(c.category == cap.UNKNOWN, "unknown tool -> UNKNOWN category")
_expect(c.requires_confirmation is True,
        "unknown tool requires confirmation (conservative default)")
_expect(c.source == "conservative", "unknown tool source=conservative")

# Explicit MCP annotations are UNTRUSTED HINTS: they may only RAISE
# caution (severity-max), never lower it. A server advertising a
# destructive tool as readOnlyHint must not get it.
ann = {"readOnlyHint": True, "destructiveHint": False,
       "idempotentHint": True, "openWorldHint": False}
c = cap.classify_capability("delete_project", annotations=ann)
_expect(c.category == cap.DESTRUCTIVE,
        "malicious readOnlyHint does NOT downgrade a destructive name "
        "(got %r)" % c.category)
_expect(c.read_only is False,
        "delete_project NOT read_only despite the lie")
_expect(c.destructive is True,
        "delete_project stays destructive (name wins over hint)")
_expect(c.requires_confirmation is True,
        "delete_project requires confirmation despite the lie")
_expect(c.source == "explicit+heuristic",
        "annotated tool source=explicit+heuristic (both signals used)")

# Both signals agreeing on READ is the only way to get read_only.
c = cap.classify_capability("get_state",
                            annotations={"readOnlyHint": True})
_expect(c.category == cap.READ and c.read_only is True,
        "name READ + explicit readOnlyHint -> read_only=True")

# Absent readOnlyHint (MCP hints default to false) -> not read_only.
c = cap.classify_capability("get_state",
                            annotations={"idempotentHint": True})
_expect(c.read_only is False,
        "no positive readOnlyHint -> read_only stays False (hint "
        "default is false)")

# An annotation CAN raise caution: destructiveHint on a read-looking name.
c = cap.classify_capability("get_state",
                            annotations={"destructiveHint": True})
_expect(c.destructive is True and c.category == cap.DESTRUCTIVE,
        "explicit destructiveHint=True honored on a read-looking name")


# ---------- 2. capability_for_tool (schema-level entry point) --------------

tv = cap.capability_for_tool(
    {"name": "spawn_actor", "description": "spawn an actor",
     "inputSchema": {"type": "object", "properties": {}}})
_expect(tv.category in (cap.CREATE, cap.UNKNOWN),
        "capability_for_tool returns a classified ToolView capability")


# ---------- 3. policy: the hard parts ---------------------------------------

# run_command is NEVER authorized, regardless of server/allow-lists.
d = policy_mod.authorize("some-server", "run_command")
_expect(d.allowed is False, "run_command never authorized")

d = policy_mod.authorize(None, "run_command")
_expect(d.allowed is False, "run_command never authorized (internal)")

# THE BOUNDARY: internal tools are ONLY the MCP introspection set.
for t in ("who_am_i", "list_platforms", "tunnel_status", "tunnel_probe"):
    d = policy_mod.authorize(None, t)
    _expect(d.allowed is True,
            "internal introspection tool allowed: %s" % t)

for t in ("write_file", "read_file", "list_files", "search_files",
          "compile_check", "speak", "log_event"):
    d = policy_mod.authorize(None, t)
    _expect(d.allowed is False,
            "internal infrastructure tool NOT a capability: %s" % t)
    _expect("boundary" in d.reason,
            "refusal names the boundary: %s" % t)

# External MCP tools are allowed (subject to confirmation).
d = policy_mod.authorize("roblox-studio", "execute_luau",
                         cap.capability_for_tool(
                             {"name": "execute_luau",
                              "description": "run Luau in Studio"}))
_expect(d.allowed is True, "external MCP tool allowed")

# Destructive external tools require confirmation.
c = cap.capability_for_tool(
    {"name": "delete_actor",
     "description": "delete an actor from the scene"})
d = policy_mod.authorize("unreal-engine", "delete_actor", c)
_expect(d.allowed is True and d.requires_confirmation is True,
        "destructive external tool allowed but requires confirmation")

# Server allow-list narrows the surface.
pol = policy_mod.Policy(server_allowlist={"roblox-studio"})
d = policy_mod.authorize("unreal-engine", "spawn_actor", None, policy=pol)
_expect(d.allowed is False, "server not in allow-list -> denied")
d = policy_mod.authorize("roblox-studio", "spawn_actor", None, policy=pol)
_expect(d.allowed is True, "server in allow-list -> allowed")

# Tool allow-list for one server.
pol = policy_mod.Policy(
    tool_allowlist={"roblox-studio": {"execute_luau"}})
d = policy_mod.authorize("roblox-studio", "execute_luau", None, policy=pol)
_expect(d.allowed is True, "tool in per-server allow-list -> allowed")
d = policy_mod.authorize("roblox-studio", "delete_actor", None, policy=pol)
_expect(d.allowed is False, "tool NOT in per-server allow-list -> denied")

# The boundary constant is not a policy field — it cannot be toggled.
_expect(policy_mod.MCP_ONLY is True, "MCP_ONLY is True (constant)")
_expect(not hasattr(policy_mod.Policy(), "mcp_only"),
        "Policy has no mcp_only field (not configurable)")

# ---------- 4. policy: process execution + unknown tools ------------------

# The ALWAYS_DENIED match is on the BARE tool name: a connected server
# exposing a tool literally named `run_command` is denied too (this was
# a real bypass when the full "server.tool" name was policy-checked).
d = policy_mod.authorize("evilserver", "run_command")
_expect(d.allowed is False,
        "external tool NAMED run_command is denied (bare-name match)")

# Internal process names fall through the boundary, not the PROCESS rule.
d = policy_mod.authorize(None, "exec")
_expect(d.allowed is False, "internal 'exec' denied by the boundary")

# External process-execution names: allowed but ALWAYS confirmation.
d = policy_mod.authorize("engine", "exec")
_expect(d.allowed is True and d.requires_confirmation is True,
        "external 'exec' allowed only with confirmation")
# Round 3 unified the label: a process tool IS code execution, so it now
# reports the real category instead of a parallel pseudo-category. One
# vocabulary means one place to reason about the risk.
_expect(d.category == "code_execution",
        "external 'exec' categorized code_execution (unified with the "
        "capability model, not a separate PROCESS label)")

# Escape payloads are refused outright — never offered for approval.
from mcp.capability import CODE_EXECUTION  # noqa: E402
ce = cap.capability_for_tool({"name": "execute_luau"})
_expect(ce.category == CODE_EXECUTION,
        "execute_luau classified as code execution")
d = policy_mod.authorize("roblox-studio", "execute_luau", ce,
                         args={"code": "os.execute('rm -rf /')"})
_expect(d.allowed is False,
        "escape payload refused even on a trusted server: %s"
        % d.reason[:70])
d = policy_mod.authorize("roblox-studio", "execute_luau", ce,
                         args={"code": "workspace.Gravity = 196.2"})
_expect(d.allowed is True and d.requires_confirmation is True,
        "real game code is allowed but needs the operator's approval")
d = policy_mod.authorize(
    "roblox-studio", "execute_luau", ce,
    policy=policy_mod.Policy(allow_code_execution={"roblox-studio"}),
    args={"code": "workspace.Gravity = 196.2"})
_expect(d.allowed is True and d.requires_confirmation is False,
        "NEX_ALLOW_CODE_EXECUTION is the operator's standing approval")

# Path escapes are refused on EVERY tool, with a named reason.
d = policy_mod.authorize("roblox-studio", "create_script", None,
                         args={"path": "/home/dev/.ssh/id_rsa"})
_expect(d.allowed is False and "escape payload" in d.reason,
        "sensitive path refused on a plain create tool: %s"
        % d.reason[:60])

# Unclassifiable external tools: untrusted by default -> confirmation,
# unless the operator explicitly relaxes it (confirm_unknown=False).
unc = cap.capability_for_tool({"name": "zorken_quibble"})
d = policy_mod.authorize("engine", "zorken_quibble", unc)
_expect(d.allowed is True and d.requires_confirmation is True,
        "unknown external tool requires confirmation by default")
d = policy_mod.authorize("engine", "zorken_quibble", unc,
                         policy=policy_mod.Policy(confirm_unknown=False))
_expect(d.allowed is True and d.requires_confirmation is False,
        "confirm_unknown=False relaxes the unknown-tool gate")

print("\nAll capability/policy tests passed.")
