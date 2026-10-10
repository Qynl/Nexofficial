"""Named, persisted rollback points spanning MULTIPLE runs.

agent/reversal.py already computes an honest, per-RUN compensation plan
(never a real transaction rollback — see its own module docstring for
why). What it does not do on its own is survive past the run that
computed it, or combine across several runs: "undo everything since
checkpoint #3" needs runs #4, #5, and #6's compensating steps chained
together in the right order, not just the most recent run's.

This module adds exactly that thin layer on top of agent/reversal.py's
existing output, without re-deriving or second-guessing it:

  * make_checkpoint() tags one run's already-computed reversal plan with
    the run metadata needed to find and order it later;
  * combine_rollback_plan() merges several runs' checkpoints into one
    compensating plan, newest run first (each run's own steps are
    already LIFO-ordered internally by agent/reversal.py).

Persistence of the checkpoint LIST across runs lives at the server layer
(server.py + store.py), same as every other cross-run structure in this
codebase — this module only produces and combines plain dicts. Nothing
here executes anything: a combined plan still goes back through
manager.call for every single step, with the exact same policy/approval/
audit gate as the original mutation it compensates.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

MAX_STEPS = 200
MAX_CHECKPOINTS_COMBINED = 50


def make_checkpoint(reversal_plan: Dict[str, Any], run_id: str,
                    run_no: int) -> Dict[str, Any]:
    """Tag one run's agent/reversal.py output as a named checkpoint."""
    plan = dict(reversal_plan or {})
    return {
        "run_id": run_id, "run_no": run_no, "created_at": time.time(),
        "available": bool(plan.get("available")),
        "mutations": int(plan.get("mutations") or 0),
        "reversible": int(plan.get("reversible") or 0),
        "irreversible": int(plan.get("irreversible") or 0),
        "coverage_pct": int(plan.get("coverage_pct") or 0),
        "steps": list(plan.get("steps") or [])[:MAX_STEPS],
        "blocked": list(plan.get("blocked") or [])[:MAX_STEPS],
    }


def combine_rollback_plan(checkpoints: Iterable[Dict[str, Any]],
                          since_run_no: Optional[int] = None
                          ) -> Dict[str, Any]:
    """Merge several runs' checkpoints into one compensating plan.

    Only checkpoints with run_no > since_run_no are included (None means
    "every checkpoint given"). Runs are applied newest-first — undo the
    most recent work before older work it may depend on — and each run's
    own steps keep the LIFO order agent/reversal.py already gave them.
    """
    relevant = [c for c in checkpoints
               if since_run_no is None or c.get("run_no", 0) > since_run_no]
    relevant = sorted(relevant, key=lambda c: c.get("run_no", 0),
                      reverse=True)[:MAX_CHECKPOINTS_COMBINED]

    steps: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    total_mutations = total_reversible = total_irreversible = 0
    runs_included: List[int] = []
    for cp in relevant:
        runs_included.append(cp.get("run_no"))
        total_mutations += int(cp.get("mutations") or 0)
        total_reversible += int(cp.get("reversible") or 0)
        total_irreversible += int(cp.get("irreversible") or 0)
        for step in cp.get("steps") or []:
            tagged = dict(step)
            tagged["from_run"] = cp.get("run_no")
            steps.append(tagged)
        for b in cp.get("blocked") or []:
            tagged_b = dict(b)
            tagged_b["from_run"] = cp.get("run_no")
            blocked.append(tagged_b)

    steps = steps[:MAX_STEPS]
    coverage_pct = (round(100 * total_reversible / total_mutations)
                   if total_mutations else 0)
    return {
        "available": bool(steps),
        "runs_included": runs_included,
        "mutations": total_mutations,
        "reversible": total_reversible,
        "irreversible": total_irreversible,
        "coverage_pct": coverage_pct,
        "steps": steps,
        "blocked": blocked,
        "note": ("Compensating actions spanning %d run(s), newest first — "
                "not a transaction rollback (see agent/reversal.py). "
                "Nothing here has executed; every step still goes through "
                "manager.call with the same policy/approval/audit gate as "
                "the mutation it compensates." % len(runs_included)),
    }


def checkpoint_summary(checkpoint: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, list-view-safe summary (no raw steps/args) for a
    checkpoint picker UI."""
    return {
        "run_id": checkpoint.get("run_id"),
        "run_no": checkpoint.get("run_no"),
        "created_at": checkpoint.get("created_at"),
        "mutations": checkpoint.get("mutations"),
        "reversible": checkpoint.get("reversible"),
        "irreversible": checkpoint.get("irreversible"),
        "coverage_pct": checkpoint.get("coverage_pct"),
    }
