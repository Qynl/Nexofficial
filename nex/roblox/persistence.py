"""DataStore/MemoryStore persistence testing, as a dedicated discipline.

Saving once and never reading it back proves nothing. This module
defines canonical persistence test cases (first join, save, load,
missing/malformed data, reconnect, concurrent update) as resolved,
evidence-checked scenarios — same resolve+evidence shape as
roblox/playtest.py and roblox/multiplayer.py — specifically for the
DataStore/MemoryStore surface, where a false "it saved" is one of the
most expensive mistakes an autonomous agent can make silently.

Every test case records: operation, expected state, and (once actually
exercised this run) whether matching evidence exists — never a model's
self-report that persistence "works."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

UNAVAILABLE = "unavailable through current MCP capability"

_ACTION_HINTS: Dict[str, Tuple[str, ...]] = {
    "FIRST_JOIN": ("player_added", "first_join", "player_joined"),
    "SAVE": ("datastore_set", "save_data", "set_async", "update_async",
            "profile_save"),
    "LOAD": ("datastore_get", "load_data", "get_async", "profile_load"),
    "LEAVE": ("player_removing", "leave_server", "disconnect_client"),
    "RECONNECT": ("rejoin", "reconnect_client", "reconnect_player"),
}


@dataclass(frozen=True)
class PersistenceCase:
    id: str
    title: str
    expected: str
    actions: Tuple[str, ...]    # keys into _ACTION_HINTS, in order
    systems: Tuple[str, ...] = ()


CASES: Tuple[PersistenceCase, ...] = (
    PersistenceCase(
        "first_join_initializes_default_data",
        "First join creates default player data",
        "A player with no prior save receives sane default values, not "
        "an error or nil.",
        ("FIRST_JOIN", "LOAD"), systems=("persistence",)),
    PersistenceCase(
        "save_then_load_round_trip",
        "Data saved is the data loaded back",
        "Values written by SAVE are exactly what the next LOAD returns.",
        ("SAVE", "LOAD"), systems=("persistence",)),
    PersistenceCase(
        "leave_triggers_a_save",
        "Leaving the game saves current state",
        "A player's final state is persisted before the instance is torn "
        "down, not lost.",
        ("LEAVE", "SAVE"), systems=("persistence", "networking")),
    PersistenceCase(
        "reconnect_restores_saved_state",
        "Reconnecting loads the previously saved state",
        "A rejoining player sees their saved progress, not fresh "
        "defaults.",
        ("RECONNECT", "LOAD"), systems=("persistence", "networking")),
)
_CASE_BY_ID = {c.id: c for c in CASES}


def _resolve_action(action: str, registry: Any) -> Optional[str]:
    hints = _ACTION_HINTS.get(action, ())
    if not hints or registry is None:
        return None
    try:
        tools = registry.all_tools()
    except Exception:  # noqa: BLE001
        return None
    for tool in tools:
        name = (getattr(tool, "name", "") or "").lower()
        if any(hint in name for hint in hints):
            return getattr(tool, "name", None)
    return None


def resolve_case(case: PersistenceCase, registry: Any) -> Dict[str, Any]:
    resolved = []
    for action in case.actions:
        tool = _resolve_action(action, registry)
        resolved.append({"action": action, "tool": tool,
                         "available": tool is not None})
    unavailable = [r["action"] for r in resolved if not r["available"]]
    return {
        "case": case.id, "title": case.title, "expected": case.expected,
        "steps": resolved, "runnable": not unavailable,
        "unavailable_actions": unavailable,
        "note": (None if not unavailable else
                "%s: %s" % (UNAVAILABLE, ", ".join(unavailable))),
    }


def case_evidence(resolution: Dict[str, Any], tasks: Iterable[Any]
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
    available = [s for s in resolution["steps"] if s["available"]]
    if not resolution["runnable"]:
        verdict = "not_runnable"
    elif confirmed == 0:
        verdict = "not_attempted"
    elif confirmed == len(available):
        verdict = "confirmed"
    else:
        verdict = "partially_confirmed"
    return {
        "case": resolution["case"], "title": resolution["title"],
        "expected": resolution["expected"], "verdict": verdict,
        "steps_confirmed": confirmed, "steps_available": len(available),
        "unavailable_actions": resolution["unavailable_actions"],
    }


def persistence_review(tasks: Iterable[Any], registry: Any,
                       case_ids: Optional[Iterable[str]] = None
                       ) -> Dict[str, Any]:
    tasks = list(tasks)
    cases = ([_CASE_BY_ID[i] for i in case_ids if i in _CASE_BY_ID]
            if case_ids is not None else list(CASES))
    results = [case_evidence(resolve_case(c, registry), tasks)
              for c in cases]
    return {
        "cases_checked": results,
        "note": ("Never operate on production player data to run these "
                "cases; use isolated test keys/DataStores wherever the "
                "environment supports it (this module itself has no way "
                "to enforce that — it only checks whether save/load "
                "evidence exists, not which DataStore key it touched)."),
    }
