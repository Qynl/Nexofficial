"""Agent event taxonomy — the vocabulary the UI understands.

The agent publishes semantic events on the server's EventBus; the
frontend renders them as run progress (phase timeline, step list,
tool activity). The contract:

  * Events describe EXECUTION STATE, never private chain-of-thought.
    A phase ("Evaluating progress"), a step title, a tool name, a
    bounded result preview — all things an operator could read over
    your shoulder. No raw model scratchpad is ever forwarded.
  * Events are additive: the frontend must be able to render a run
    from `run.plan` + `run.step` + `run.tool` alone. `run.quality` adds a
    public, evidence-backed game-production scorecard when applicable.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

# Coarse run phases (the visible loop).
PHASES = (
    "planning",      # decomposing the goal
    "executing",     # acting through MCP tools
    "evaluating",    # checking progress against the goal
    "adapting",      # re-planning after evidence
    "waiting",       # paused for the user (approval / question)
    "finishing",     # composing the final report
)

# Run outcomes.
STATUS_COMPLETED = "completed"        # goal met, evidence recorded
STATUS_PARTIAL = "partial"            # some steps failed, goal unproven
STATUS_FAILED = "failed"              # the goal could not be reached
STATUS_BLOCKED = "blocked"            # missing capability / refused
STATUS_WAITING_USER = "waiting_user"  # paused on the user
STATUS_CANCELLED = "cancelled"        # operator stopped the run


def run_event(run_id: str, event_type: str, **payload: Any) -> Dict[str, Any]:
    return {"type": event_type, "run_id": run_id, "ts": time.time(),
            **payload}


def emit(bus: Optional[Callable], run_id: str, event_type: str,
         **payload: Any) -> None:
    """Publish a run event on the bus (never raises)."""
    if bus is None:
        return
    try:
        bus(run_event(run_id, event_type, **payload))
    except Exception:  # noqa: BLE001
        pass
