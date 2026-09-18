"""Checkpoints (STAGE 14).

After meaningful milestones, Nex saves a checkpoint containing the project
state + task graph. If Nex restarts, it recovers from the latest valid
checkpoint instead of starting over.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from agent.project_state import ProjectState
from agent.task_graph import TaskGraph


def save_checkpoint(path: str, goal: str, state: ProjectState,
                    graph: TaskGraph, extra: Optional[Dict[str, Any]] = None) -> None:
    """Persist a checkpoint atomically-ish (write-then-rename)."""
    payload: Dict[str, Any] = {
        "goal": goal,
        "state": state.to_dict(),
        "graph": graph.to_dict(),
        "extra": extra or {},
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load_checkpoint(path: str) -> Optional[Tuple[str, ProjectState, TaskGraph, Dict[str, Any]]]:
    """Load a checkpoint. Returns None if missing/unreadable."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    state = ProjectState.from_dict(data.get("state", {}))
    graph = TaskGraph.from_dict(data.get("graph", {}))
    return data.get("goal", ""), state, graph, data.get("extra", {})


def is_resumable(path: str) -> bool:
    return load_checkpoint(path) is not None


def reconcile_on_resume(path: str, registry,
                        bus=None) -> Optional[Tuple[str, ProjectState, TaskGraph, Dict[str, Any]]]:
    """Reconcile a checkpoint against CURRENT reality before resuming.

    A checkpoint is a claim about the world; the world moved on while Nex
    was down. Before trusting it we:

      1. re-discover capabilities (the caller passes a FRESH registry built
         from live MCP upstreams — servers may have connected/disconnected),
      2. check every task's tool still exists; pending tasks whose tool
         vanished are marked FAILED with a note instead of blowing up
         mid-run,
      3. flag required-args drift between the saved args and the live
         inputSchema,
      4. RE-VERIFY completed tasks that have a verify_tool still available:
         if verification now fails, the task is demoted to pending so the
         loop genuinely redoes it (honest resume, not blind resume).

    Step 4 goes through agent.verification.verify_task — the SAME canonical
    verifier the execution loop uses — against the freshly reconnected
    registry.

    Returns (goal, state, graph, reconciliation_report) or None.
    """
    from agent.task_graph import FAILED, PENDING, SUCCESS

    loaded = load_checkpoint(path)
    if loaded is None:
        return None
    goal, state, graph, extra = loaded

    report: Dict[str, Any] = {
        "goal": goal,
        "tasks_checked": 0,
        "missing_tools": [],
        "args_drift": [],
        "reverified": 0,
        "reverify_failed": [],
    }

    def _tv(tool: str):
        tv = registry.by_name(tool)
        if tv is None and "." in tool:
            tv = registry.by_name(tool.split(".", 1)[-1])
        return tv

    for t in graph.all():
        report["tasks_checked"] += 1
        tv = _tv(t.tool)
        if tv is None:
            report["missing_tools"].append(t.tool)
            if t.status != SUCCESS:
                t.status = FAILED
                t.error = ("tool '%s' unavailable since checkpoint" % t.tool)
                t.notes = (t.notes + " | reconciled: tool vanished").strip(" |")
                if bus is not None:
                    bus({"type": "agent.checkpoint_reconciled", "task": t.id,
                         "issue": "missing_tool", "tool": t.tool})
                continue
        # Required-args drift vs the live schema.
        schema = tv.schema if isinstance(tv.schema, dict) else {}
        for req in (schema.get("required", []) or []):
            if req not in (t.args or {}):
                report["args_drift"].append(
                    {"task": t.id, "tool": t.tool, "missing_arg": req})
        # Re-verify completed work whose verifier still exists. This is the
        # canonical verifier: it performs the live registry call itself.
        if t.status == SUCCESS and t.verify_tool:
            vtv = _tv(t.verify_tool)
            if vtv is not None:
                try:
                    from agent.verification import verify_task
                    vres = verify_task(t, registry,
                                       t.result if isinstance(t.result, dict) else None)
                    report["reverified"] += 1
                    if not vres.ok:
                        t.status = PENDING
                        t.notes = (t.notes +
                                   " | reconciled: re-verify failed, redo"
                                   ).strip(" |")
                        report["reverify_failed"].append(t.id)
                        if bus is not None:
                            bus({"type": "agent.checkpoint_reconciled",
                                 "task": t.id, "issue": "reverify_failed",
                                 "note": vres.note})
                except Exception as exc:  # noqa: BLE001
                    report["args_drift"].append(
                        {"task": t.id, "tool": t.verify_tool,
                         "reverify_error": repr(exc)})

    if bus is not None:
        bus({"type": "agent.checkpoint_resumed", "goal": goal,
             "report": report})
    return goal, state, graph, report
