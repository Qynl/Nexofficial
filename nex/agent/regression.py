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
