"""A real Roblox playtest agent — not "I started the game."

Starting Play Solo proves a session opened. It proves nothing about
whether a checkpoint respawn, a purchase, or an NPC interaction actually
behaves correctly. This module gives Nex a Roblox-flavored abstract
action vocabulary (START/STOP/WAIT/MOVE/LOOK/JUMP/INTERACT/CLICK/TYPE/
TRIGGER/RESPAWN/JOIN/LEAVE/REJOIN) plus a small set of canonical,
reusable test cases built from that vocabulary, resolved against
whatever the connected MCP environment actually supports.

Same discipline as agent/playtest.py (the engine-agnostic version used
for Unreal-style gameplay loops): an action with no connected tool is
reported as unavailable, never silently skipped or assumed; a scenario
is only "confirmed" when this run's own successful, in-order evidence
shows it actually ran — never an LLM's self-report that a test passed.

This module never calls a tool itself; execution stays on the existing
manager.call() path with all its approval/policy/audit machinery intact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

UNAVAILABLE = "unavailable through current MCP capability"

# Bounded abstract action vocabulary. Only actions a connected tool
# actually resolves are ever exposed for a scenario to use.
ACTION_HINTS: Dict[str, Tuple[str, ...]] = {
    "START": ("play_solo", "start_play", "run_playtest", "start_server",
             "start_local_server"),
    "STOP": ("stop_play", "stop_playtest", "end_play", "stop_server"),
    "WAIT": ("wait", "sleep", "delay"),
    "MOVE": ("move_to", "move_player", "set_position", "walk_to",
            "navigate_to"),
    "LOOK": ("set_camera", "look_at", "rotate_camera"),
    "JUMP": ("jump", "set_jump"),
    "INTERACT": ("interact", "activate_prompt", "proximity_prompt",
                "use_tool", "fire_tool"),
    "CLICK": ("click", "click_button", "fire_mouse_click", "mouse_click"),
    "TYPE": ("type_text", "send_chat", "set_text_input"),
    "TRIGGER": ("fire_remote", "invoke_remote", "fire_server",
               "fire_client"),
    "RESPAWN": ("respawn", "load_character", "reset_character"),
    "JOIN": ("start_client", "join_server", "test_players", "add_player"),
    "LEAVE": ("leave_server", "disconnect_client", "remove_player"),
    "REJOIN": ("rejoin", "reconnect_client", "reconnect_player"),
}

# Observation evidence: not an action itself, but what a step's outcome
# is checked against. A scenario with resolved actions but zero connected
# observation tool can run blind — that gap is reported, not hidden.
_OBSERVATION_HINTS = (
    "studio_output", "output_log", "console_output", "inspect_logs",
    "screenshot", "viewport_capture", "get_state", "query_actor",
    "test_result", "run_tests",
)


@dataclass(frozen=True)
class AbstractStep:
    action: str          # a key in ACTION_HINTS
    detail: str           # human label, e.g. "the checkpoint"


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    expected: str          # what correct behaviour looks like, stated plainly
    steps: Tuple[AbstractStep, ...]
    # agent/regression.py system ids this scenario is relevant to — lets
    # roblox/test_generation.py select scenarios by what a feature
    # actually touches instead of a fixed checklist.
    systems: Tuple[str, ...] = ()


SCENARIOS: Tuple[Scenario, ...] = (
    Scenario(
        "checkpoint_respawn", "Checkpoint respawn",
        "Player respawns at the latest activated checkpoint, not the "
        "original spawn.",
        (AbstractStep("START", "a play session"),
         AbstractStep("MOVE", "to the checkpoint"),
         AbstractStep("INTERACT", "activate the checkpoint"),
         AbstractStep("RESPAWN", "trigger a respawn")),
        systems=("persistence", "player_interaction")),
    Scenario(
        "inventory_roundtrip", "Pick up and use an inventory item",
        "The item appears in inventory, the UI reflects it, and it "
        "persists after a respawn.",
        (AbstractStep("START", "a play session"),
         AbstractStep("MOVE", "to the item"),
         AbstractStep("INTERACT", "pick up the item"),
         AbstractStep("RESPAWN", "confirm the item survives respawn")),
        systems=("economy", "persistence", "ui")),
    Scenario(
        "purchase_flow", "Purchase an item through a RemoteEvent",
        "Currency decreases exactly once and the item is granted exactly "
        "once per valid request.",
        (AbstractStep("START", "a play session"),
         AbstractStep("CLICK", "the purchase button"),
         AbstractStep("TRIGGER", "fire the purchase remote")),
        systems=("economy", "networking")),
    Scenario(
        "join_leave_rejoin", "Player join, leave, and rejoin",
        "Player state (position, inventory, currency) is correctly "
        "restored on rejoin, not reset.",
        (AbstractStep("JOIN", "a client"),
         AbstractStep("LEAVE", "the server"),
         AbstractStep("REJOIN", "the server")),
        systems=("persistence", "networking")),
)
_SCENARIO_BY_ID = {s.id: s for s in SCENARIOS}


def _resolve_action(action: str, registry: Any) -> Optional[str]:
    hints = ACTION_HINTS.get(action, ())
    if not hints or registry is None:
        return None
    try:
        tools = registry.all_tools()
    except Exception:  # noqa: BLE001 - registry is a live/optional surface
        return None
    for tool in tools:
        name = (getattr(tool, "name", "") or "").lower()
        if any(hint in name for hint in hints):
            return getattr(tool, "name", None)
    return None


def _has_observation_tool(registry: Any) -> bool:
    if registry is None:
        return False
    try:
        tools = registry.all_tools()
    except Exception:  # noqa: BLE001
        return False
    return any(any(h in (getattr(t, "name", "") or "").lower()
                  for h in _OBSERVATION_HINTS) for t in tools)


def resolve_scenario(scenario: Scenario, registry: Any) -> Dict[str, Any]:
    resolved = []
    for step in scenario.steps:
        tool = _resolve_action(step.action, registry)
        resolved.append({"action": step.action, "detail": step.detail,
                         "tool": tool, "available": tool is not None})
    unavailable = [r["action"] for r in resolved if not r["available"]]
    observable = _has_observation_tool(registry)
    return {
        "scenario": scenario.id, "title": scenario.title,
        "expected": scenario.expected,
        "steps": resolved, "runnable": not unavailable and observable,
        "unavailable_actions": unavailable,
        "observable": observable,
        "note": (None if (not unavailable and observable) else
                ("; ".join(filter(None, [
                    ("%s: %s" % (UNAVAILABLE, ", ".join(unavailable)))
                    if unavailable else None,
                    "no connected observation tool (Output/screenshot/"
                    "state query) to tell what actually happened"
                    if not observable else None,
                ])))),
    }


def scenario_evidence(resolution: Dict[str, Any], tasks: Iterable[Any]
                      ) -> Dict[str, Any]:
    successful_tools = [t.tool for t in tasks
                        if getattr(t, "status", None) == "success"
                        and getattr(t, "tool", None)]
    confirmed = 0
    cursor = 0
    for step in resolution["steps"]:
        if not step["available"]:
            continue
        found_at = None
        for i in range(cursor, len(successful_tools)):
            if successful_tools[i] == step["tool"]:
                found_at = i
                break
        if found_at is not None:
            confirmed += 1
            cursor = found_at + 1
    available_steps = [s for s in resolution["steps"] if s["available"]]
    total_available = len(available_steps)
    if not resolution["runnable"]:
        verdict = "not_runnable"
    elif confirmed == 0:
        verdict = "not_attempted"
    elif confirmed == total_available:
        verdict = "confirmed"
    else:
        verdict = "partially_confirmed"
    return {
        "scenario": resolution["scenario"], "title": resolution["title"],
        "expected": resolution["expected"], "verdict": verdict,
        "steps_confirmed": confirmed, "steps_available": total_available,
        "steps_total": len(resolution["steps"]),
        "unavailable_actions": resolution["unavailable_actions"],
        "observable": resolution["observable"],
    }


def playtest_review(tasks: Iterable[Any], registry: Any,
                    scenario_ids: Optional[Iterable[str]] = None
                    ) -> Dict[str, Any]:
    """Report-only entry point. `scenario_ids` lets a caller scope this to
    scenarios relevant to the feature just worked on; omit for all of them.
    """
    tasks = list(tasks)
    scenarios = ([_SCENARIO_BY_ID[i] for i in scenario_ids
                 if i in _SCENARIO_BY_ID] if scenario_ids is not None
                else list(SCENARIOS))
    results = []
    for scenario in scenarios:
        resolution = resolve_scenario(scenario, registry)
        results.append(scenario_evidence(resolution, tasks))
    return {
        "scenarios_checked": results,
        "note": ("Deterministic scripted-sequence evidence. A scenario "
                "marked 'not_runnable' means either an action has no "
                "connected tool, or nothing can observe the outcome; "
                "'not_attempted' means the tools exist but this run never "
                "actually called them in sequence."),
    }
