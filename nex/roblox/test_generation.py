"""Derive which test categories a feature actually needs — not a fixed
checklist applied uniformly regardless of what the feature is.

Given the gameplay systems a feature touches (agent/regression.py's
classify_system vocabulary — the same vocabulary roblox/playtest.py,
roblox/multiplayer.py, and roblox/persistence.py's canonical scenarios
are tagged with), this assembles a bounded, deterministic test plan: only
the FUNCTIONAL / MULTIPLAYER / PERSISTENCE / REGRESSION / PERFORMANCE
categories that are actually relevant, built from literal catalog
look-ups rather than an LLM's guess about what to test. An LLM may still
design NEW test cases this catalog doesn't cover, or decide which of
these generated tests matter most to run first — but which deterministic
scenarios exist and which systems they apply to is never invented here.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set

from agent.regression import DEPENDENTS
from roblox import multiplayer as roblox_multiplayer
from roblox import persistence as roblox_persistence
from roblox import playtest as roblox_playtest

# Systems where a silent slowdown is a common, expensive failure mode —
# conservative and small on purpose; this is a prompt for a performance
# pass, never a measurement itself (see roblox/performance.py for that).
_PERFORMANCE_SENSITIVE_SYSTEMS = {"npc", "traffic", "world_streaming"}


def generate_test_plan(systems: Iterable[str]) -> Dict[str, Any]:
    """A bounded, deterministic test plan for a feature touching `systems`.

    Each category is a list of catalog ids (scenario/case ids from the
    roblox.playtest / roblox.multiplayer / roblox.persistence catalogs),
    never freshly-invented prose test descriptions.
    """
    touched: Set[str] = set(systems)
    functional = [s.id for s in roblox_playtest.SCENARIOS
                 if set(s.systems) & touched]
    multiplayer_tests = [s.id for s in roblox_multiplayer.SCENARIOS
                         if set(s.systems) & touched]
    persistence_tests = [c.id for c in roblox_persistence.CASES
                         if set(c.systems) & touched]
    lifecycle = (["join_leave_rejoin"]
                if "join_leave_rejoin" in functional else [])
    functional = [f for f in functional if f != "join_leave_rejoin"]
    regression = sorted({dep for sys_id in touched
                        for dep in DEPENDENTS.get(sys_id, ())
                        if dep not in touched})
    performance = sorted(touched & _PERFORMANCE_SENSITIVE_SYSTEMS)

    return {
        "systems": sorted(touched),
        "functional": functional,
        "multiplayer": multiplayer_tests,
        "lifecycle": lifecycle,
        "persistence": persistence_tests,
        "regression": regression,
        "performance": performance,
        "note": ("Only categories with at least one matching catalog "
                "entry or dependent system are populated; an empty "
                "category means nothing in the existing catalog applies "
                "to these systems, not that no testing is needed."),
    }


def affected_systems_for_feature(tool_names: Iterable[str]) -> Set[str]:
    """Convenience: classify a feature's intended tool surface into
    systems before it has even run, using the SAME classifier
    agent/regression.py uses for already-executed evidence. Useful for
    planning a test plan BEFORE implementation, not just after."""
    from agent.regression import classify_system
    out: Set[str] = set()
    for name in tool_names:
        out |= classify_system(name)
    return out
