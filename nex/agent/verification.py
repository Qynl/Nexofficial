"""Separate IMPLEMENTATION from PROOF, per gameplay system.

A tool call succeeding is not the same claim as "this system works."
This module composes evidence that is ALREADY computed elsewhere —
agent/regression.py's system classification + dependency map, and
agent/quality.py's own tool_gates() (build/playtest/performance/
visual_review) — into a per-system matrix of SEPARATE, never-collapsed
proof dimensions:

    IMPLEMENTED        a successful mutation touched this system
    COMPILED           a build/compile tool succeeded this run
    RUNTIME_TESTED     a PIE/standalone/play session succeeded this run
    PERFORMANCE_TESTED a profiling/telemetry tool succeeded this run
    VISUALLY_REVIEWED  a real AI critique of a captured screenshot ran
    REGRESSION_CHECKED this system's usual dependents were themselves
                       exercised in the same run (agent/regression.py)
    OVERALL            PASS only when every REQUIRED dimension passed

COMPILED/RUNTIME_TESTED/PERFORMANCE_TESTED/VISUALLY_REVIEWED are honestly
RUN-WIDE evidence, not proof isolated to one system — no current MCP
capability can say "the build succeeded FOR JUST the vehicle system".
That caveat is attached to every system's record explicitly so nothing
here is ever read as more certain than it actually is.

This module produces data for the report only. It is deliberately NOT
wired into any LLM prompt: the whole point is a machine-readable fact
table a human (or a future automated check) can read directly, not more
text for a model to reinterpret.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set

from agent.quality import tool_gates
from agent.regression import DEPENDENTS, classify_system

PASS = "PASS"
FAIL = "FAIL"
WARNING = "WARNING"
UNPROVEN = "UNPROVEN"

DIMENSIONS = ("implemented", "compiled", "runtime_tested",
             "performance_tested", "visually_reviewed",
             "regression_checked")
# Dimensions that can actually veto OVERALL. The rest are informative —
# many systems have no meaningful visual/performance proof requirement,
# so their absence is a note, not an automatic failure.
_REQUIRED = ("implemented", "compiled", "runtime_tested")


def _tool_view(registry: Any, server: Any, tool: str) -> Any:
    try:
        if server:
            return registry.by_name("%s.%s" % (server, tool))
        return registry.by_name(tool)
    except Exception:  # noqa: BLE001 - evidence lookup must not crash a run
        return None


def verify_systems(tasks: Iterable[Any], registry: Any,
                   visual_critiques_recorded: int = 0
                   ) -> Dict[str, Dict[str, Any]]:
    """Per-system proof matrix for every system this run touched or tried
    to touch, built entirely from this run's own tasks + the live
    registry's declared tool gates — never from a model's self-report.
    """
    tasks = list(tasks)
    touched: Dict[str, List[str]] = {}
    failed_systems: Set[str] = set()

    run_build_ok = run_build_bad = False
    run_runtime_ok = run_runtime_bad = False
    run_performance_ok = False

    for t in tasks:
        tool_name = getattr(t, "tool", None)
        if not tool_name:
            continue
        status = getattr(t, "status", None)
        systems = classify_system(tool_name)

        tv = _tool_view(registry, getattr(t, "server", None), tool_name)
        gates = tool_gates(tv) if tv is not None else set()
        if status == "success":
            if "build" in gates:
                run_build_ok = True
            if "playtest" in gates:
                run_runtime_ok = True
            if "performance" in gates:
                run_performance_ok = True
        elif status == "failed":
            if "build" in gates:
                run_build_bad = True
            if "playtest" in gates:
                run_runtime_bad = True

        for sys_id in systems:
            if status == "success":
                touched.setdefault(sys_id, []).append(getattr(t, "name", "?"))
            elif status == "failed":
                failed_systems.add(sys_id)

    compiled = PASS if run_build_ok else (FAIL if run_build_bad else UNPROVEN)
    runtime_tested = PASS if run_runtime_ok else (
        FAIL if run_runtime_bad else UNPROVEN)
    performance_tested = PASS if run_performance_ok else UNPROVEN
    visually_reviewed = PASS if visual_critiques_recorded > 0 else UNPROVEN

    out: Dict[str, Dict[str, Any]] = {}
    for sys_id in sorted(set(touched) | failed_systems):
        steps = touched.get(sys_id, [])
        if steps:
            implemented = PASS
        elif sys_id in failed_systems:
            implemented = FAIL
        else:
            implemented = UNPROVEN

        dependents = DEPENDENTS.get(sys_id, ())
        unverified_deps = [d for d in dependents if d not in touched]
        if not dependents:
            regression_checked = UNPROVEN
        elif unverified_deps:
            regression_checked = WARNING
        else:
            regression_checked = PASS

        required_values = {implemented, compiled, runtime_tested}
        if FAIL in required_values:
            overall = FAIL
        elif UNPROVEN in required_values:
            overall = UNPROVEN
        elif regression_checked == WARNING:
            overall = WARNING
        else:
            overall = PASS

        notes: List[str] = []
        if unverified_deps:
            notes.append("dependents never re-exercised this run: " +
                        ", ".join(unverified_deps))
        if compiled != FAIL and run_build_bad and not run_build_ok:
            notes.append("a build failure is open this run")
        notes.append("compiled/runtime_tested/performance_tested/"
                    "visually_reviewed are run-wide evidence, not proof "
                    "isolated to this system")

        out[sys_id] = {
            "system": sys_id,
            "implemented": implemented,
            "compiled": compiled,
            "runtime_tested": runtime_tested,
            "performance_tested": performance_tested,
            "visually_reviewed": visually_reviewed,
            "regression_checked": regression_checked,
            "overall": overall,
            "evidence_steps": steps[-5:],
            "notes": notes,
        }
    return out
