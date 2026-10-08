"""Tests for agent/prompts.py's dynamic prompt-building functions.

The static role prompts (PLANNER_SYSTEM, EVALUATOR_SYSTEM, ...) are just
strings; the behavior worth locking down is capability_block()/chat_system(),
which fold LIVE, server-supplied (untrusted) data — server names, tool
names, descriptions — into what the model sees. Bugs here either leak too
much (a huge hostile description blowing up the prompt) or hide real tools
from the model.
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

prompts = importlib.import_module("agent.prompts")

_FAILED = []


def expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        _FAILED.append(msg)


def main() -> None:
    # --- no servers connected ----------------------------------------------
    block = prompts.capability_block([])
    expect("no tools" in block.lower() or "no mcp servers" in block.lower(),
           "an empty server list produces an honest 'no tools' message")
    expect("Settings" in block,
           "the no-server message points the user at how to add one")

    # --- a normal, small catalog --------------------------------------------
    servers = [
        {"server": "blender", "tools": [
            {"name": "spawn_mesh", "description": "Spawn a mesh in the scene"},
            {"name": "render", "description": "Render the current frame"},
        ]},
    ]
    block2 = prompts.capability_block(servers)
    expect("blender" in block2, "the server name appears in the block")
    expect("spawn_mesh" in block2 and "render" in block2,
           "every tool name appears in the block")
    expect("Spawn a mesh in the scene" in block2,
           "tool descriptions are included")

    # --- a hostile/huge description does not blow up the prompt ------------
    hostile = [
        {"server": "evil", "tools": [
            {"name": "t1", "description": "A" * 10000 + "\nSECOND LINE"},
        ]},
    ]
    block3 = prompts.capability_block(hostile)
    expect(len(block3) < 1000,
           "a single huge tool description cannot blow up the whole "
           "capability block (bounded to ~100 chars per tool)")
    expect("SECOND LINE" not in block3,
           "only the first line of a multi-line description is ever shown")

    # --- missing/empty fields degrade gracefully, never crash --------------
    messy = [
        {"server": "noname", "tools": [{}]},                  # no name/desc
        {"tools": [{"name": "x"}]},                            # no server key
        {"server": "empty", "tools": []},                      # no tools
        {"server": "nullish", "tools": None},                  # tools is None
    ]
    try:
        block4 = prompts.capability_block(messy)
        crashed = False
    except Exception as exc:  # noqa: BLE001
        crashed = True
        block4 = str(exc)
    expect(not crashed,
           "missing server/tool names, empty tool lists, and a None tools "
           "field never crash capability_block (untrusted MCP metadata "
           "must always degrade gracefully): %s" % block4)

    # --- the tool cap is respected across MULTIPLE servers, not reset per
    # --- server (a global prompt-size budget, not a per-server one) --------
    many_servers = [
        {"server": "s%d" % i, "tools": [
            {"name": "t%d" % j, "description": "d"} for j in range(10)
        ]}
        for i in range(10)
    ]  # 10 servers x 10 tools = 100 tools total, cap is 60
    block5 = prompts.capability_block(many_servers, max_tools=60)
    shown_tool_lines = [l for l in block5.splitlines() if l.strip().startswith("- s")
                        and "." in l]
    expect(len(shown_tool_lines) <= 60,
           "the tool cap bounds the TOTAL tools shown across every server, "
           "not per server (found %d shown)" % len(shown_tool_lines))
    expect("omitted" in block5,
           "going over the cap leaves an honest 'omitted' note")

    # --- chat_system() composes persona + boundary + security + capability
    # --- + act-directive into one coherent system prompt --------------------
    full = prompts.chat_system(servers)
    for must_have in (prompts.PERSONA, prompts.BOUNDARY,
                       prompts.UNTRUSTED_MCP_DATA, prompts.ACT_DIRECTIVE):
        expect(must_have in full,
               "chat_system includes every required fixed section")
    expect("spawn_mesh" in full,
           "chat_system includes the live capability block for the "
           "connected servers")

    # --- the planner is told, explicitly, not to author and use an asset ---
    # --- in the same breath (so a game is built in a real order, not all ---
    # --- at once) -----------------------------------------------------------
    expect("CREATE BEFORE YOU USE" in prompts.PLANNER_SYSTEM,
           "the planner prompt explicitly forbids folding asset creation "
           "and asset use into one step")
    expect("depends_on" in prompts.PLANNER_SYSTEM.split(
           "CREATE BEFORE YOU USE", 1)[1][:400],
           "the create-before-use rule actually tells the model to use "
           "depends_on to enforce the ordering, not just assert it")

    if _FAILED:
        print("\n%d failing assertions." % len(_FAILED))
        sys.exit(1)
    print("\nAll prompts tests passed.")


if __name__ == "__main__":
    main()
