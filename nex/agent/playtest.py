"""Runtime playtesting — canonical scripted sequences, honestly resolved.

Today's "playtest" evidence (agent/quality.py's PLAYTEST gate) is
satisfied the moment ANY tool whose name contains "play"/"run_game"/
"launch_game" was called once. That proves a play session STARTED; it
proves nothing about whether the actual gameplay loop a system implies
(spawn a vehicle, get in, drive it, get out) ever ran, let alone worked.

This module defines a small, bounded CATALOG of canonical gameplay
sequences (one per gameplay system agent/regression.py already
classifies tool calls into) as an abstract action vocabulary — never raw
tool names, because tool names differ per MCP server. Each abstract step
is resolved against the LIVE connected registry with the same
keyword-hint discipline as mcp/capability.py: if no connected tool's
name matches an action's hints, that step is honestly reported as
"unavailable through current MCP capability" — never silently skipped,
never guessed, never faked as having run.

Execution still goes exclusively through agent/loop.py's existing
manager.call() path (approval, policy, diagnostics, audit all apply
unchanged) — this module never calls a tool itself. What it adds is:
  (a) a deterministic RESOLVABILITY check — can this canonical gameplay
      loop even be attempted with what's connected right now?
  (b) a deterministic EVIDENCE check — did THIS run's already-executed,
      already-successful steps actually cover the resolved sequence, in
      order? A scenario is only "confirmed" when its resolved tools were
      called successfully in the same relative order the scenario
      specifies; out-of-order or missing steps are reported, not papered
      over.
This is strictly more honest than, and does not replace, the existing
PLAYTEST gate; it is additive, report-only evidence (see agent/loop.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent.regression import classify_system

UNAVAILABLE = "unavailable through current MCP capability"

# Bounded abstract action vocabulary. NOT tool names — every connected MCP
# server names things differently. Each action is a verb a scripted
# gameplay loop needs, resolved per-run against whatever is actually
# connected.
_ACTION_HINTS: Dict[str, Tuple[str, ...]] = {
    "spawn": ("spawn", "create_actor", "add_actor", "instantiate",
             "place_actor"),
    "possess": ("possess", "enter_vehicle", "control_pawn", "mount"),
    "move": ("move_to", "move_actor", "set_location", "teleport",
            "navigate_to", "set_transform"),
    "drive": ("drive", "throttle", "steer", "accelerate", "set_input"),
    "interact": ("interact", "use_item", "activate", "trigger_", "pickup",
                "pick_up"),
    "exit": ("exit_vehicle", "unpossess", "dismount", "leave_vehicle"),
    "talk": ("dialogue", "talk_to", "start_conversation", "converse"),
    "purchase": ("purchase", "buy_item", "sell_item", "trade"),
    "observe": ("screenshot", "capture", "get_state", "query_actor",
               "inspect_actor"),
    "despawn": ("destroy_actor", "despawn", "remove_actor", "delete_actor"),
}


@dataclass(frozen=True)
class AbstractStep:
    action: str              # a key in _ACTION_HINTS
    subject: str             # human label only, e.g. "the vehicle"


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    system: str              # agent/regression.py system id this exercises
    steps: Tuple[AbstractStep, ...]


# One canonical, reusable scripted sequence per gameplay system Nex's
# classifier already knows about. Deliberately small: these are the
# roundtrips a shipped feature in that system must survive, not an
# attempt to enumerate every possible playtest.
SCENARIOS: Tuple[Scenario, ...] = (
    Scenario("vehicle_roundtrip", "Spawn a vehicle, drive it, and exit",
            "vehicles", (
                AbstractStep("spawn", "the vehicle"),
                AbstractStep("possess", "the vehicle"),
                AbstractStep("drive", "the vehicle"),
                AbstractStep("exit", "the vehicle"),
            )),
    Scenario("npc_encounter", "Approach an NPC and interact with it",
            "npc", (
                AbstractStep("spawn", "the NPC"),
                AbstractStep("move", "toward the NPC"),
                AbstractStep("talk", "the NPC"),
            )),
    Scenario("item_pickup", "Pick up an item and confirm it registers",
            "economy", (
                AbstractStep("spawn", "the item"),
                AbstractStep("interact", "the item"),
                AbstractStep("observe", "the inventory"),
            )),
    Scenario("player_object_interaction",
            "Approach and interact with a placed object", "player_interaction",
            (
                AbstractStep("move", "toward the object"),
                AbstractStep("interact", "the object"),
            )),
)

_SCENARIO_BY_SYSTEM = {s.system: s for s in SCENARIOS}


def _resolve_action(action: str, registry: Any) -> Optional[str]:
    """Bare tool name of the first connected tool matching `action`'s
    hints, or None if nothing connected resolves it."""
    hints = _ACTION_HINTS.get(action, ())
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


def resolve_scenario(scenario: Scenario, registry: Any) -> Dict[str, Any]:
    """Which of this scenario's abstract steps a LIVE registry can serve.

    Never claims a step is runnable without a real matching connected
    tool; an unresolved step is reported by name, not hidden.
    """
    resolved: List[Dict[str, Any]] = []
    for step in scenario.steps:
        tool = _resolve_action(step.action, registry)
        resolved.append({
            "action": step.action, "subject": step.subject,
            "tool": tool, "available": tool is not None,
        })
    unavailable = [r["action"] for r in resolved if not r["available"]]
    return {
        "scenario": scenario.id,
        "title": scenario.title,
        "system": scenario.system,
        "steps": resolved,
        "runnable": not unavailable,
        "unavailable_actions": unavailable,
        "note": (None if not unavailable else
                "%s: %s" % (UNAVAILABLE, ", ".join(unavailable))),
    }


def relevant_scenarios(tasks: Iterable[Any]) -> List[Scenario]:
    """Canonical scenarios worth checking THIS run — only for systems this
    run's successful tool calls actually touched. Never proposes a
    scenario for a system nothing in this run worked on."""
    touched: set = set()
    for t in tasks:
        if getattr(t, "status", None) != "success" or not getattr(t, "tool", None):
            continue
        touched |= classify_system(t.tool)
    return [s for system_id, s in _SCENARIO_BY_SYSTEM.items()
           if system_id in touched]


def scenario_evidence(resolution: Dict[str, Any], tasks: Iterable[Any]
                      ) -> Dict[str, Any]:
    """Did THIS run's successful calls actually cover the resolved
    sequence, in order? Only resolved (available) steps can ever be
    confirmed — an unavailable step can never silently count as done."""
    successful_tools = [t.tool for t in tasks
                        if getattr(t, "status", None) == "success"
                        and getattr(t, "tool", None)]
    confirmed = 0
    cursor = 0
    for step in resolution["steps"]:
        if not step["available"]:
            continue
        tool = step["tool"]
        found_at = None
        for i in range(cursor, len(successful_tools)):
            if successful_tools[i] == tool:
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
        "verdict": verdict,
        "steps_confirmed": confirmed, "steps_available": total_available,
        "steps_total": len(resolution["steps"]),
        "unavailable_actions": resolution["unavailable_actions"],
    }


def playtest_review(tasks: Iterable[Any], registry: Any) -> Dict[str, Any]:
    """Top-level report-only entry point for agent/loop.py.

    Bounded to the systems this run actually touched; never claims
    coverage of a system nothing here worked on.
    """
    tasks = list(tasks)
    scenarios = relevant_scenarios(tasks)
    results = []
    for scenario in scenarios:
        resolution = resolve_scenario(scenario, registry)
        results.append(scenario_evidence(resolution, tasks))
    return {
        "scenarios_checked": results,
        "note": ("Deterministic scripted-sequence evidence, distinct from "
                "the PLAYTEST quality gate (which only checks that some "
                "play-session tool was called). A scenario marked "
                "'not_runnable' means no connected tool resolves one of "
                "its required actions; 'not_attempted' means the tools "
                "exist but this run's plan never actually called them in "
                "sequence."),
    }
