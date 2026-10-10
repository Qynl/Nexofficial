"""Multiplayer correctness as a first-class Roblox testing discipline.

Single-client playtesting (roblox/playtest.py) cannot see replication
bugs, race conditions, or broken server authority — those only exist
with two or more clients talking to the same server at once. This module
defines canonical multi-client scenarios the same way roblox/playtest.py
defines single-client ones: an abstract per-client action sequence,
resolved against the live registry, with evidence that only counts when
MULTIPLE distinct client sessions actually ran this turn (a single
start_client call can never satisfy a multiplayer scenario — see
roblox/capabilities.py's MULTI_CLIENT_PLAYTEST, which this module's
"runnable" check reuses the same reasoning for).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from roblox.playtest import _has_observation_tool, _resolve_action

UNAVAILABLE = "unavailable through current MCP capability"

MIN_CLIENTS = 2


@dataclass(frozen=True)
class ClientStep:
    client: str          # "server", "client_a", "client_b", ...
    action: str           # a key in roblox.playtest.ACTION_HINTS


@dataclass(frozen=True)
class MultiplayerScenario:
    id: str
    title: str
    expected: str
    steps: Tuple[ClientStep, ...]
    systems: Tuple[str, ...] = ()


SCENARIOS: Tuple[MultiplayerScenario, ...] = (
    MultiplayerScenario(
        "simultaneous_limited_purchase",
        "Two clients purchase the same limited item simultaneously",
        "Only one valid transaction succeeds; the other is rejected "
        "server-side, not granted twice.",
        (ClientStep("client_a", "JOIN"), ClientStep("client_b", "JOIN"),
         ClientStep("client_a", "TRIGGER"), ClientStep("client_b", "TRIGGER")),
        systems=("economy", "networking")),
    MultiplayerScenario(
        "player_isolation",
        "Two players' private state never leaks across clients",
        "Client A never observes Client B's inventory/currency/private "
        "UI state.",
        (ClientStep("client_a", "JOIN"), ClientStep("client_b", "JOIN"),
         ClientStep("client_a", "INTERACT"), ClientStep("client_b", "MOVE")),
        systems=("player_interaction", "ui")),
    MultiplayerScenario(
        "reconnect_state_restore",
        "A client disconnects and rejoins mid-session",
        "State (position, inventory, currency) is restored on rejoin, "
        "not reset to defaults.",
        (ClientStep("client_a", "JOIN"), ClientStep("client_a", "LEAVE"),
         ClientStep("client_a", "REJOIN")),
        systems=("persistence", "networking")),
)
_SCENARIO_BY_ID = {s.id: s for s in SCENARIOS}


def resolve_scenario(scenario: MultiplayerScenario, registry: Any
                     ) -> Dict[str, Any]:
    resolved = []
    for step in scenario.steps:
        tool = _resolve_action(step.action, registry)
        resolved.append({"client": step.client, "action": step.action,
                         "tool": tool, "available": tool is not None})
    unavailable = [r["action"] for r in resolved if not r["available"]]
    distinct_clients = {r["client"] for r in resolved}
    multi_client_shape = len(distinct_clients) >= MIN_CLIENTS or any(
        c != "server" for c in distinct_clients)
    observable = _has_observation_tool(registry)
    runnable = not unavailable and observable
    notes = []
    if unavailable:
        notes.append("%s: %s" % (UNAVAILABLE, ", ".join(unavailable)))
    if not observable:
        notes.append("no connected observation tool to confirm outcomes")
    return {
        "scenario": scenario.id, "title": scenario.title,
        "expected": scenario.expected,
        "steps": resolved, "runnable": runnable,
        "unavailable_actions": unavailable, "observable": observable,
        "distinct_clients": sorted(distinct_clients),
        "note": "; ".join(notes) if notes else None,
    }


def scenario_evidence(resolution: Dict[str, Any], tasks: Iterable[Any]
                      ) -> Dict[str, Any]:
    """Only counts as attempted multiplayer evidence when distinct
    client-session tool calls (JOIN-style actions) actually ran MORE
    THAN ONCE this run — one client session can never prove a
    multi-client scenario, regardless of how many other steps succeeded.
    """
    successful_tools = [t.tool for t in tasks
                        if getattr(t, "status", None) == "success"
                        and getattr(t, "tool", None)]
    join_tools = {s["tool"] for s in resolution["steps"]
                 if s["action"] == "JOIN" and s["available"]}
    join_calls = [t for t in successful_tools if t in join_tools]

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
    if not resolution["runnable"]:
        verdict = "not_runnable"
    elif len(join_calls) < MIN_CLIENTS:
        verdict = "single_client_only"
    elif confirmed == 0:
        verdict = "not_attempted"
    elif confirmed == len(available_steps):
        verdict = "confirmed"
    else:
        verdict = "partially_confirmed"

    return {
        "scenario": resolution["scenario"], "title": resolution["title"],
        "expected": resolution["expected"], "verdict": verdict,
        "steps_confirmed": confirmed, "steps_available": len(available_steps),
        "client_sessions_observed": len(join_calls),
        "unavailable_actions": resolution["unavailable_actions"],
    }


def multiplayer_review(tasks: Iterable[Any], registry: Any,
                       scenario_ids: Optional[Iterable[str]] = None
                       ) -> Dict[str, Any]:
    tasks = list(tasks)
    scenarios = ([_SCENARIO_BY_ID[i] for i in scenario_ids
                 if i in _SCENARIO_BY_ID] if scenario_ids is not None
                else list(SCENARIOS))
    results = [scenario_evidence(resolve_scenario(s, registry), tasks)
              for s in scenarios]
    return {
        "scenarios_checked": results,
        "note": ("'single_client_only' means every resolved action ran, "
                "but fewer than %d distinct client sessions were observed "
                "this run — a real multiplayer claim needs genuinely "
                "concurrent clients, not one client called twice in "
                "sequence." % MIN_CLIENTS),
    }
