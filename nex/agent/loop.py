"""Autonomous agent loop (STAGE 6 / STAGE 7 / STAGE 9 / STAGE 27).

Implements the observe -> plan -> act -> verify -> reflect -> repair ->
continue cycle against a live CapabilityRegistry. It is generic and does NOT
hardcode any game recipe: it uses the planner to build a dependency graph
from discovered capabilities, then executes it with retry/repair and
dependency-aware failure propagation.

When a task fails it tries, in order:
  A. retry unchanged          (transient error, maybe fixed)
  B. retry with corrected args (diagnose a missing parameter)
  C. use another MCP tool     (same capability category, e.g. a backup
                               importer) — never gives up on the first tool
  E. stop + report            (only after exhausting A/B/C)

So the agent does not "always stop" at the first failure — it repairs, and
only reports a capability as missing when every recovery option is exhausted.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent.audit import AuditLog
from agent.events import (
    STATUS_COMPLETED, STATUS_PARTIAL, STATUS_BLOCKED, STATUS_FAILED,
    STATUS_WAITING_USER, agent_event,
)
from agent.planner import plan as default_planner
from agent.project_state import ProjectState
from agent.registry import CapabilityRegistry
from agent.task_graph import TaskGraph, PENDING, RUNNING, SUCCESS, FAILED, SKIPPED
from agent.verification import verify_task
from mcp.capability import ToolCapability
from mcp.policy import authorize, current_policy


@dataclass
class CompletionReport:
    status: str
    goal: str
    completed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "goal": self.goal,
            "completed": self.completed, "failed": self.failed,
            "skipped": self.skipped, "reasons": self.reasons,
        }


# Max times we retry on the SAME error signature before giving up on that path.
_MAX_REPEAT = 2


def _error_signature(error: str) -> str:
    norm = re.sub(r"'[^']*'", "'?'", error or "")
    norm = re.sub(r"\d+", "N", norm)
    return norm[:120]


def _diagnose(error: str, task: Any):
    """Generic, non-hardcoded repair: if the error names a missing argument,
    supply a placeholder default for it and retry."""
    m = re.search(r"'([a-zA-Z_][a-zA-Z0-9_]*)'", error or "")
    if m:
        field_name = m.group(1)
        if field_name not in (task.args or {}):
            new_args = dict(task.args or {})
            new_args[field_name] = "__default_" + field_name
            return new_args, "added missing argument '%s'" % field_name
    return None, None


def _resolve_args(task: Any, graph: TaskGraph) -> Dict[str, Any]:
    args = dict(task.args or {})
    for dep_id in task.deps:
        dep = graph.get(dep_id)
        if dep and isinstance(dep.result, dict):
            for key in ("id", "asset", "project", "anim", "script", "world"):
                if key in dep.result and key not in args:
                    args[key] = dep.result[key]
    return args


class AutonomousAgent:
    def __init__(self, registry: CapabilityRegistry,
                 planner: Callable = None,
                 policy=None,
                 audit: Optional[AuditLog] = None,
                 bus: Optional[Callable[[dict], None]] = None,
                 approver: Optional[Callable[[Any], bool]] = None,
                 max_iterations: int = 400,
                 backoff_base: float = 0.1,
                 max_backoff: float = 2.0) -> None:
        self.registry = registry
        self.planner = planner or default_planner
        self.policy = policy or current_policy()
        self.audit = audit or AuditLog()
        self.bus = bus
        self.approver = approver or (lambda task: True)
        self.max_iterations = max_iterations
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff

    # ----- event helper ---------------------------------------------------
    def _emit(self, agent_state=None, event_type=None, **payload) -> None:
        if self.bus is None:
            return
        try:
            self.bus(agent_event(agent_state, event_type, **payload))
        except Exception:  # noqa: BLE001
            pass

    # ----- main entry ------------------------------------------------------
    def run(self, goal: str, state: Optional[ProjectState] = None,
            graph: Optional[TaskGraph] = None) -> CompletionReport:
        self._emit("OBSERVING", "agent.observe",
                   servers=[s.name for s in self.registry.servers])
        if state is None:
            state = ProjectState(goal=goal)

        self._emit("PLANNING", "agent.plan_started", goal=goal)
        if graph is None:
            graph = self.planner(goal, self.registry)
        self._emit("PLANNING", "agent.plan_updated",
                   task_count=len(graph.all()),
                   stages=[t.stage for t in graph.all()])

        state.pending = [t.name for t in graph.all() if t.status == PENDING]

        iterations = 0
        while not graph.is_terminal() and iterations < self.max_iterations:
            iterations += 1
            ready = graph.ready()
            if not ready:
                break
            for task in ready:
                self._execute_task(task, state, graph)
                if graph.is_terminal():
                    break

        succeeded = [t.name for t in graph.completed()]
        failed = [t.name for t in graph.failed()]
        skipped = [t.name for t in graph.skipped()]

        if not failed and not skipped and succeeded:
            status = STATUS_COMPLETED
        elif failed and not succeeded:
            status = STATUS_FAILED
        elif skipped and not failed:
            status = STATUS_PARTIAL
        else:
            status = STATUS_PARTIAL

        reasons = []
        for t in graph.failed():
            reasons.append("%s: %s" % (t.name, t.error or "failed"))
        for t in graph.skipped():
            reasons.append("%s: %s" % (t.name, t.notes or "skipped"))

        state.completed = succeeded
        state.failed = [{"task": n, "reason": r.split(": ", 1)[-1]}
                        for n, r in zip(failed, reasons)]
        self._emit("COMPLETED" if status == STATUS_COMPLETED else "BLOCKED",
                   "agent.project_completed" if status == STATUS_COMPLETED
                   else "agent.project_blocked",
                   status=status, completed=succeeded, failed=failed)

        return CompletionReport(status=status, goal=goal, completed=succeeded,
                               failed=failed, skipped=skipped, reasons=reasons)

    # ----- per-task execution with retry/repair ---------------------------
    def _find_alternative(self, task: Any) -> Optional[Any]:
        """Another MCP server exposes the SAME tool (an equivalent capability
        on a different connection — e.g. a second Roblox Studio instance, or a
        backup importer). This is a safe substitution: same tool, different
        endpoint. We do NOT switch to a merely same-category tool (that would
        silently turn create_animation into create_asset)."""
        for t in self.registry.all_tools():
            if t.name == task.tool and t.server != task.server:
                return t
        return None

    def _execute_task(self, task: Any, state: ProjectState, graph: TaskGraph) -> None:
        self._emit("EXECUTING", "agent.task_started", task=task.id,
                   tool=task.tool, server=task.server)
        task.status = RUNNING
        state.current = task.name

        # Authorization (architecture-level, not prompt-level).
        tv = self.registry.by_name(task.tool) if task.tool else None
        cap = tv.capability if tv else ToolCapability()
        decision = authorize(task.server, task.tool, cap, self.policy)
        if not decision.allowed:
            self.audit.record(task_id=task.id, server=task.server,
                             tool=task.tool, args=task.args,
                             classification=cap.to_dict(),
                             authorized=False, auth_reason=decision.reason,
                             result="denied")
            task.error = decision.reason
            graph.mark_failed(task.id, decision.reason,
                              _error_signature(decision.reason))
            self._emit("BLOCKED", "agent.tool_failed", task=task.id,
                       reason=decision.reason)
            return

        if decision.requires_confirmation and not self.approver(task):
            task.status = "waiting"
            self._emit("WAITING", "agent.waiting_for_confirmation",
                       task=task.id, tool=task.tool)
            return

        seen_sigs: Dict[str, int] = {}
        args = _resolve_args(task, graph)

        while task.attempts < task.max_attempts:
            task.attempts += 1
            self._emit("EXECUTING", "agent.tool_called", task=task.id,
                       tool=task.tool, args_summary=list(args.keys()))
            try:
                resp = self.registry.call(task.server, task.tool, args)
            except Exception as exc:  # noqa: BLE001
                resp = {"error": type(exc).__name__ + ": " + str(exc)}

            if isinstance(resp, dict) and "error" in resp:
                err = str(resp["error"])
                sig = _error_signature(err)
                self.audit.record(task_id=task.id, server=task.server,
                                 tool=task.tool, args=args,
                                 classification=cap.to_dict(),
                                 authorized=True, auth_reason="allowed",
                                 result="error", error=err)
                self._emit("REPAIRING", "agent.tool_failed", task=task.id,
                           error=err)

                seen_sigs[sig] = seen_sigs.get(sig, 0) + 1
                if seen_sigs[sig] > _MAX_REPEAT:
                    task.error = err
                    graph.mark_failed(task.id, err, sig)
                    state.record_failure(task.name, err)
                    return

                # B) retry with corrected args (missing-parameter repair)
                corrected, note = _diagnose(err, task)
                if corrected is not None:
                    task.args = corrected
                    args = _resolve_args(task, graph)
                    self._emit("REPAIRING", "agent.repair_started",
                               task=task.id, note=note, action="correct_args")
                    time.sleep(min(self.backoff_base * (2 ** (task.attempts - 1)),
                                   self.max_backoff))
                    continue

                # C) use another MCP tool in the same capability category
                alt = self._find_alternative(task)
                if alt is not None and alt.full_name not in (task.tried_alts or []):
                    task.tried_alts = (task.tried_alts or []) + [alt.full_name]
                    self._emit("REPAIRING", "agent.repair_started",
                               task=task.id,
                               note="switched to equivalent tool %s" % alt.full_name,
                               action="switch_tool", from_tool=task.tool,
                               to_tool=alt.name)
                    task.server = alt.server
                    task.tool = alt.name
                    task.attempts = 0
                    seen_sigs.clear()
                    args = _resolve_args(task, graph)
                    time.sleep(min(self.backoff_base, self.max_backoff))
                    continue

                # E) cannot repair -> stop and propagate
                task.error = err
                graph.mark_failed(task.id, err, sig)
                state.record_failure(task.name, err)
                return

            # Success path -> VERIFY.
            result = resp.get("result") if isinstance(resp, dict) else resp
            self.audit.record(task_id=task.id, server=task.server,
                             tool=task.tool, args=args,
                             classification=cap.to_dict(),
                             authorized=True, auth_reason="allowed",
                             result="ok")

            self._emit("VERIFYING", "agent.verification_started", task=task.id)
            vres = verify_task(task, self.registry, result)
            if not vres.ok:
                sig = _error_signature(vres.note)
                seen_sigs[sig] = seen_sigs.get(sig, 0) + 1
                self._emit("VERIFYING", "agent.verification_failed",
                           task=task.id, note=vres.note)
                if seen_sigs[sig] > _MAX_REPEAT:
                    task.error = vres.note
                    graph.mark_failed(task.id, vres.note, sig)
                    state.record_failure(task.name, vres.note)
                    return
                time.sleep(min(self.backoff_base, self.max_backoff))
                continue

            self._emit("VERIFYING", "agent.verification_passed", task=task.id)
            task.result = result
            graph.mark_success(task.id, result)
            state.record_success(task.name)
            self._emit("EXECUTING", "agent.tool_succeeded", task=task.id,
                       tool=task.tool)
            return

        task.error = task.error or "exhausted retries"
        graph.mark_failed(task.id, task.error, _error_signature(task.error or ""))
        state.record_failure(task.name, task.error)
