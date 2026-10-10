"""Regression risk — system -> dependent systems -> evidence this run.

Large autonomous builds break earlier systems. This module does NOT
execute any tests itself (it has no tool-call authority — see
mcp/policy.py for the only action path) and it never claims a dependent
system still works. It only maintains bounded, static domain knowledge
of which gameplay systems commonly depend on which other ones, matches
that against which systems this run's SUCCESSFUL tool calls actually
touched, and flags the dependents that were never themselves exercised
in the same run. That flag is advisory evidence for the planner and the
report — "verify this before calling the stage done" — never a pass/fail
verdict and never a substitute for an actual playtest/test tool call.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set, Tuple

# Keyword -> system id. Deliberately concrete multi-character substrings
# (never bare short words) so an unrelated tool name cannot accidentally
# match, mirroring agent/visual.py's classify_defects() bounded matching.
_SYSTEM_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("world_streaming", ("world_partition", "level_stream", "streaming_",
                         "sublevel")),
    ("navigation", ("navmesh", "navigation", "pathfind", "nav_mesh")),
    ("npc", ("npc", "ai_controller", "behavior_tree", "spawn_character")),
    ("traffic", ("traffic", "crowd_spawn")),
    ("vehicles", ("vehicle", "drivable", "driving")),
    ("missions", ("mission", "quest", "objective")),
    ("economy", ("economy", "currency", "shop", "inventory", "item_def")),
    ("persistence", ("save_game", "savegame", "load_game", "persistence",
                     "checkpoint_save")),
    ("player_interaction", ("interact", "possess", "input_action")),
    ("ui", ("widget", "hud", "ui_")),
)

# Static, conservative dependency knowledge: when the KEY system changes,
# these dependents commonly break silently and deserve re-verification.
# Intentionally the same direction/spirit as production.py's stage-order
# advisory, scoped to within-stage system coupling rather than stage order.
DEPENDENTS: Dict[str, Tuple[str, ...]] = {
    "world_streaming": ("navigation",),
    "navigation": ("npc", "traffic"),
    "npc": ("traffic", "missions"),
    "vehicles": ("traffic", "missions", "player_interaction", "persistence"),
    "traffic": ("missions",),
    "missions": ("economy", "persistence"),
    "economy": ("persistence", "ui"),
    "player_interaction": ("ui",),
}


def classify_system(tool_name: str) -> Set[str]:
    """Which system(s) a tool name indicates work on, if any."""
    name = (tool_name or "").lower()
    hit: Set[str] = set()
    for system_id, keywords in _SYSTEM_KEYWORDS:
        if any(k in name for k in keywords):
            hit.add(system_id)
    return hit


def mutated_systems(tasks: Iterable[Any]) -> Dict[str, List[str]]:
    """system_id -> step names of this run's successful tool calls that
    touched it. Only SUCCESS matters: a failed mutation did not actually
    change the system, so it creates no regression risk to track."""
    out: Dict[str, List[str]] = {}
    for t in tasks:
        status = getattr(t, "status", None)
        if status != "success" or not getattr(t, "tool", None):
            continue
        for system_id in classify_system(t.tool):
            out.setdefault(system_id, []).append(getattr(t, "name", "?"))
    return out


def regression_review(tasks: Iterable[Any]) -> Dict[str, Any]:
    """Bounded, honest summary: what this run touched, and which of its
    systems' usual dependents were never themselves exercised here."""
    touched = mutated_systems(tasks)
    at_risk: Dict[str, List[str]] = {}
    for system_id in touched:
        unverified = [d for d in DEPENDENTS.get(system_id, ())
                     if d not in touched]
        if unverified:
            at_risk[system_id] = unverified
    return {
        "systems_touched": {k: v[-5:] for k, v in touched.items()},
        "at_risk_dependents": at_risk,
        "note": ("Static dependency knowledge, not an executed test. A "
                "dependent system not listed here may still be broken; "
                "one listed as 'at risk' may still be fine — this only "
                "says it was never itself exercised by a successful tool "
                "call in this run."),
    }


def regression_brief(review: Dict[str, Any], limit: int = 5) -> str:
    """Planner-facing text, or "" when nothing is flagged."""
    at_risk = (review or {}).get("at_risk_dependents") or {}
    if not at_risk:
        return ""
    lines = [
        "REGRESSION RISK (static dependency knowledge, not a verified "
        "result — see note in the report): this run's successful changes "
        "may have affected systems that were never re-exercised here:",
    ]
    for system_id, deps in list(at_risk.items())[:limit]:
        lines.append("- '%s' changed this run; re-verify: %s" %
                     (system_id, ", ".join(deps)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cross-run regression state — the within-run check above only sees ONE
# run's tasks, so a system modified in run 3 whose dependent was verified
# back in run 1 (and never since) looks "fine" to it even though two runs
# of unrelated changes have landed in between. This closes that gap with a
# small, bounded, persisted ledger — never a model's self-report, built
# the same way as agent/memory.py: a plain dict this module can merge,
# with persistence left entirely to store.py/server.py (agent/ must not
# import store — tests/test_architecture.py enforces this).
# ---------------------------------------------------------------------------

MAX_TRACKED_SYSTEMS = 32


def empty_regression_state() -> Dict[str, Any]:
    return {"systems": {}}


def merge_regression_state(previous: Any, review: Dict[str, Any],
                           run_no: int) -> Dict[str, Any]:
    """Fold one run's regression_review() into the persisted cross-run
    ledger. For every system touched THIS run: record when it was last
    touched, and track the run number since which it has had unverified
    dependents (`since_run`) — cleared the moment ANY later run exercises
    that dependent, regardless of which run originally changed the
    parent system.
    """
    state = {"systems": dict((previous or {}).get("systems") or {})}
    touched = (review or {}).get("systems_touched") or {}
    at_risk = (review or {}).get("at_risk_dependents") or {}

    for system_id in touched:
        entry = dict(state["systems"].get(system_id) or {})
        entry["last_touched_run"] = run_no
        unverified = at_risk.get(system_id) or []
        if unverified:
            entry["since_run"] = entry.get("since_run") or run_no
            entry["unverified_dependents"] = list(unverified)
        else:
            entry.pop("since_run", None)
            entry["unverified_dependents"] = []
        state["systems"][system_id] = entry

    # A dependent exercised THIS run clears the flag on every system that
    # still listed it as unverified, no matter which run first flagged it.
    for touched_sys in touched:
        for parent_id, deps in DEPENDENTS.items():
            if touched_sys not in deps:
                continue
            parent = state["systems"].get(parent_id)
            if not parent:
                continue
            remaining = [d for d in parent.get("unverified_dependents") or ()
                        if d != touched_sys]
            if remaining != (parent.get("unverified_dependents") or []):
                parent["unverified_dependents"] = remaining
                if not remaining:
                    parent.pop("since_run", None)

    # Bounded: keep the most recently touched systems only.
    if len(state["systems"]) > MAX_TRACKED_SYSTEMS:
        ordered = sorted(state["systems"].items(),
                         key=lambda kv: kv[1].get("last_touched_run", 0),
                         reverse=True)
        state["systems"] = dict(ordered[:MAX_TRACKED_SYSTEMS])
    return state


def cross_run_risk(state: Any, run_no: int,
                   stale_after: int = 2) -> List[Dict[str, Any]]:
    """Systems whose dependents have been unverified for `stale_after` or
    more runs — risk that has been accumulating ACROSS runs, not just
    within the latest one. Still advisory, still never a verified result.
    """
    out: List[Dict[str, Any]] = []
    for system_id, entry in ((state or {}).get("systems") or {}).items():
        since = entry.get("since_run")
        deps = entry.get("unverified_dependents") or []
        if not since or not deps:
            continue
        age = run_no - int(since) + 1
        if age >= stale_after:
            out.append({"system": system_id, "unverified_dependents": deps,
                        "runs_unverified": age,
                        "since_run": int(since)})
    return sorted(out, key=lambda e: e["runs_unverified"], reverse=True)

