"""Bottleneck-aware priority advisory for long runs.

A long run should not spend its remaining budget as evenly-distributed
random tool calls. This module composes signals ALREADY computed
elsewhere (agent/failures.py's classification of current failures,
agent/quality.py's evidence scorecard, agent/visual.py's open-defect
count) into one plain-language directive for the planner: which kind of
work matters most right now, given what is actually broken or missing —
never a rigid fixed percentage split, and never invented from nothing.

This changes no execution logic; a plan is still written entirely by the
model. It only changes what the model is told to prioritize.
"""
from __future__ import annotations

from collections import Counter
from typing import Dict, Sequence


def bottleneck_note(*, failure_kinds: Sequence[str] = (),
                    quality_missing: Sequence[str] = (),
                    visual_open_count: int = 0) -> str:
    """One clear directive for the single biggest bottleneck, or "" when
    nothing currently dominates (the default plan priority order stands).

    Order matters and is deliberate: a broken build/runtime blocks every
    other kind of evidence, so it always wins first. Missing proof (no
    measurement at all) is reported before polish of what IS measured.
    Visual polish is only prioritized once there is no open functional
    gap competing for the same effort.
    """
    counts = Counter(k for k in failure_kinds if k)
    if counts.get("compilation", 0) >= 1:
        return ("BOTTLENECK: a compilation failure is currently open. Stop "
                "adding new content — nothing built on top of a broken "
                "compile can be proven to work. Repair it, rebuild, and "
                "confirm the build succeeds before anything else.")
    if counts.get("runtime", 0) >= 2:
        return ("BOTTLENECK: repeated runtime crashes are open (%dx). Stop "
                "adding new content and diagnose the crashing subsystem "
                "from logs before expanding further." % counts["runtime"])
    missing = set(quality_missing or ())
    if "performance" in missing:
        return ("BOTTLENECK: no performance measurement exists yet for "
                "this stage. Capture one before adding more content — "
                "'optimized' is not a claim this run may make without a "
                "number.")
    functional_gaps = missing - {"visual", "visual_review"}
    if not functional_gaps and visual_open_count >= 3:
        return ("BOTTLENECK: %d open visual defects and no missing "
                "functional evidence — shift effort toward visual polish "
                "instead of new systems this pass." % visual_open_count)
    if counts.get("architecture", 0) >= 1:
        return ("BOTTLENECK: an architectural failure is open (circular "
                "dependency, broken reference, or similar). Resolve the "
                "structural issue before building more on top of it.")
    return ""
