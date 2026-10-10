"""Roblox per-system verification — the health model, aggregated honestly.

Extends agent/verification.py's generic IMPLEMENTED/COMPILED/
RUNTIME_TESTED/PERFORMANCE_TESTED/VISUALLY_REVIEWED/REGRESSION_CHECKED
matrix with two dimensions that matrix has no notion of, because they
only make sense for a live-service, multiplayer engine:

    PERSISTENCE_TESTED   a persistence case relevant to this system was
                         CONFIRMED this run (roblox/persistence.py)
    MULTIPLAYER_TESTED   a multiplayer scenario relevant to this system
                         was CONFIRMED this run (roblox/multiplayer.py)

A system only gets scored on these dimensions when roblox/test_generation
.py's own catalog says a relevant case/scenario actually EXISTS for it —
a system with no multiplayer-relevant canonical scenario is marked
NOT_APPLICABLE, never penalized for missing evidence that was never
possible to collect. This is the same "separate what's PROVEN from
what's merely CLAIMED" discipline as agent/verification.py, extended for
Roblox's live-service reality: a feature can exist (IMPLEMENTED) while
still being completely UNVERIFIED for multiplayer correctness, and that
distinction must stay visible rather than being averaged away.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

from agent.verification import FAIL, PASS, UNPROVEN, WARNING, verify_systems
from roblox import multiplayer as roblox_multiplayer
from roblox import persistence as roblox_persistence
from roblox.test_generation import generate_test_plan

NOT_APPLICABLE = "NOT_APPLICABLE"


def _persistence_status(system_id: str, tasks: List[Any], registry: Any,
                        applicable_ids: List[str]) -> str:
    if not applicable_ids:
        return NOT_APPLICABLE
    cases = [c for c in roblox_persistence.CASES if c.id in applicable_ids]
    verdicts = [roblox_persistence.case_evidence(
        roblox_persistence.resolve_case(c, registry), tasks)["verdict"]
        for c in cases]
    if any(v == "confirmed" for v in verdicts):
        return PASS
    if any(v == "partially_confirmed" for v in verdicts):
        return WARNING
    return UNPROVEN


def _multiplayer_status(system_id: str, tasks: List[Any], registry: Any,
                        applicable_ids: List[str]) -> str:
    if not applicable_ids:
        return NOT_APPLICABLE
    scenarios = [s for s in roblox_multiplayer.SCENARIOS
                if s.id in applicable_ids]
    verdicts = [roblox_multiplayer.scenario_evidence(
        roblox_multiplayer.resolve_scenario(s, registry), tasks)["verdict"]
        for s in scenarios]
    if any(v == "confirmed" for v in verdicts):
        return PASS
    if any(v == "partially_confirmed" for v in verdicts):
        return WARNING
    return UNPROVEN


def roblox_verify_systems(tasks: Iterable[Any], registry: Any,
                          visual_critiques_recorded: int = 0
                          ) -> Dict[str, Dict[str, Any]]:
    """agent/verification.py's matrix, with PERSISTENCE_TESTED and
    MULTIPLAYER_TESTED added for the systems a canonical catalog entry
    actually covers.
    """
    tasks = list(tasks)
    base = verify_systems(tasks, registry, visual_critiques_recorded)
    out: Dict[str, Dict[str, Any]] = {}
    for sys_id, record in base.items():
        record = dict(record)
        plan = generate_test_plan([sys_id])
        persistence_status = _persistence_status(
            sys_id, tasks, registry, plan["persistence"])
        multiplayer_status = _multiplayer_status(
            sys_id, tasks, registry, plan["multiplayer"])
        record["persistence_tested"] = persistence_status
        record["multiplayer_tested"] = multiplayer_status
        # Informational only, same philosophy as performance/visual in the
        # base matrix: these never downgrade overall by themselves, but a
        # live FAIL-worthy signal doesn't exist at this layer (resolution
        # failures already show as UNPROVEN, not FAIL, via the catalogs).
        if persistence_status == WARNING or multiplayer_status == WARNING:
            if record["overall"] == PASS:
                record["overall"] = WARNING
        out[sys_id] = record
    return out
