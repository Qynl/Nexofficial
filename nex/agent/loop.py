"""Autonomous agent loop (STAGE 6 / STAGE 7 / STAGE 9 / STAGE 27).

Implements the observe -> plan -> act -> verify -> reflect -> repair ->
continue cycle against a live CapabilityRegistry.

PLANNING (unified): when an `llm` is attached, the GOAL-SPECIFIC plan comes
from the model (agent.model_planner — LLM plan JSON validated against the
LIVE registry and converted to a TaskGraph). The deterministic skeleton in
agent/planner.py is only a FALLBACK when no model is available or the model
returns nothing usable. There is exactly one execution pipeline: this loop
(policy -> registry -> verification -> recovery -> checkpoints).

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
    missing: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "goal": self.goal,
            "completed": self.completed, "failed": self.failed,
            "skipped": self.skipped, "reasons": self.reasons,
            "missing": self.missing,
        }


# Max times we retry on the SAME error signature before giving up on that path.
_MAX_REPEAT = 2


def _error_signature(error: str) -> str:
    norm = re.sub(r"'[^']*'", "'?'", error or "")
    norm = re.sub(r"\d+", "N", norm)
    return norm[:120]


def _is_transient(error: str) -> bool:
    """Transient errors (timeouts, unreachable, rate-limit, 5xx) are worth
    retrying harder than logical/validation errors (which won't fix
    themselves)."""
    return bool(re.search(
        r"(timeout|timed out|unreachable|connection reset|econnrefused|"
        r"503|502|504|circuit breaker|rate limit|temporarily)", error or "",
        re.IGNORECASE))


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


def _safe_json(obj: Any) -> str:
    try:
        import json as _json
        return _json.dumps(obj, ensure_ascii=False, default=str)[:400]
    except Exception:  # noqa: BLE001
        return str(obj)[:400]


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
                 max_backoff: float = 2.0,
                 recovery_passes: int = 1,
                 llm: Optional[Callable] = None) -> None:
        self.registry = registry
        self.planner = planner or default_planner
        self.policy = policy or current_policy()
        self.audit = audit or AuditLog()
        self.bus = bus
        self.approver = approver or (lambda task: True)
        self.max_iterations = max_iterations
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        # Extra recovery passes: after the first pass, revive skipped/failed
        # tasks (re-running repair + same-tool-on-another-server recovery)
        # before declaring the run blocked. This is what keeps the agent from
        # "always stopping" at the first failure.
        self.recovery_passes = max(0, recovery_passes)
        # Canonical model hook: when set, goals are planned by the LLM
        # (goal-specific TaskGraph), with the skeleton as fallback.
        self.llm = llm
        self.last_plan = None

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
            graph: Optional[TaskGraph] = None,
            resume_checkpoint: Optional[str] = None) -> CompletionReport:
        self._emit("OBSERVING", "agent.observe",
                   servers=[s.name for s in self.registry.servers])
        if state is None:
            state = ProjectState(goal=goal)

        # --- Resume from a checkpoint (RECONCILE-ON-RESUME) -----------------
        # A checkpoint is a claim; reality may have moved (servers down,
        # tools gone, artifacts deleted). reconcile_on_resume re-discovers
        # capabilities against the LIVE registry and demotes any completed
        # task whose verifier now fails, so resume redoes real work instead
        # of trusting stale state.
        if resume_checkpoint and graph is None:
            try:
                from agent.checkpoints import reconcile_on_resume
                recon = reconcile_on_resume(
                    resume_checkpoint, self.registry, bus=self.bus)
                if recon is not None:
                    goal_c, state_c, graph_c, report_c = recon
                    state = state_c
                    state.goal = state.goal or goal_c
                    graph = graph_c
                    self._emit("OBSERVING", "agent.checkpoint_resumed",
                               checkpoint=resume_checkpoint,
                               reconciled=report_c)
            except Exception as exc:  # noqa: BLE001
                self._emit("WAITING", "agent.checkpoint_resume_failed",
                           checkpoint=resume_checkpoint, error=repr(exc))

        self._emit("PLANNING", "agent.plan_started", goal=goal)
        if graph is None:
            graph = self._make_plan(goal, state)
        self._emit("PLANNING", "agent.plan_updated",
                   task_count=len(graph.all()),
                   stages=[t.stage for t in graph.all()])

        state.pending = [t.name for t in graph.all() if t.status == PENDING]

        # --- Execution with multi-pass recovery ------------------------------
        # Pass 1 executes everything that's ready. If failures leave the graph
        # non-terminal (skipped dependents / failed leaves), we REVIVE and try
        # again (up to `recovery_passes` extra times) instead of stopping.
        # This is what keeps Nex from "always stopping" at the first failure:
        # it re-runs repair + same-tool-on-another-server recovery before it
        # ever reports a task as truly blocked.
        passes = 0
        max_passes = 1 + self.recovery_passes
        while passes < max_passes:
            passes += 1
            # Drain every ready wave within this pass (traverse the DAG).
            while True:
                ready = graph.ready()
                if not ready:
                    break
                for task in ready:
                    self._execute_task(task, state, graph)
            # Either everything is terminal, or some tasks are blocked
            # (failed / skipped). If terminal, we're done.
            if graph.is_terminal():
                # Only failed/skipped remain. Try to revive them for another
                # pass before declaring the run blocked.
                if not self._revive(graph):
                    break
                continue
            if not self._revive(graph):
                break

        # Guaranteed final drain: a recovery pass may have just re-PENDINGed a
        # previously-failed task right as the pass budget ran out. Give it one
        # execution so its outcome (success or genuine failure) is reported
        # instead of being left dangling as PENDING.
        while True:
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

        # Transparency: list the exact capabilities that were needed but could
        # not be delivered, so the user knows precisely what to connect (no
        # silent low-quality partial).
        missing = []
        for t in list(graph.failed()) + list(graph.skipped()):
            missing.append({
                "task": t.name,
                "stage": t.stage,
                "server": t.server,
                "tool": t.tool,
                "reason": t.error or t.notes or "unavailable",
            })

        state.completed = succeeded
        state.failed = [{"task": n, "reason": r.split(": ", 1)[-1]}
                        for n, r in zip(failed, reasons)]
        self._emit("COMPLETED" if status == STATUS_COMPLETED else "BLOCKED",
                   "agent.project_completed" if status == STATUS_COMPLETED
                   else "agent.project_blocked",
                   status=status, completed=succeeded, failed=failed,
                   missing=missing)

        return CompletionReport(status=status, goal=goal, completed=succeeded,
                               failed=failed, skipped=skipped, reasons=reasons,
                               missing=missing)

    # ----- planning (canonical: LLM-driven, skeleton fallback) -----------
    def _make_plan(self, goal: str, state: ProjectState) -> TaskGraph:
        """ONE planning path: the model produces the goal-specific plan when
        an llm is attached; the deterministic skeleton is the fallback."""
        if self.llm is not None:
            try:
                from agent.model_planner import model_driven_planner
                feedback = getattr(state, "plan_feedback", None)
                g, plan = model_driven_planner(goal, self.registry, self.llm,
                                               feedback=feedback)
                if g is not None and g.all():
                    self.last_plan = plan
                    return g
            except Exception:  # noqa: BLE001
                pass
        from agent.planner import plan as skeleton_plan
        return skeleton_plan(goal, self.registry)

    def _llm_diagnose(self, task: Any, err: str, phase: str = "execute"):
        """Genuine LLM failure diagnosis (agent.diagnose): the model gets the
        failure context + the LIVE tool catalog and returns a structured
        recovery decision — corrected args, a different (validated, live)
        tool, or an honest give-up. Registry-validated before use, and
        bounded to one attempt per task (llm_diagnosed) so a broken model
        can't loop the pipeline. The deterministic repairs stay first.

        Returns the normalized decision dict or None.
        """
        if self.llm is None or getattr(task, "llm_diagnosed", False):
            return None
        task.llm_diagnosed = True
        from agent.diagnose import diagnose as _diagnose_llm
        decision = _diagnose_llm(task, err, self.registry, self.llm,
                                 phase=phase)
        if decision is not None:
            self._emit("REPAIRING", "agent.repair_started", task=task.id,
                       note=decision.get("reason", ""),
                       action=decision.get("kind"))
        return decision

    def _revive(self, graph: TaskGraph) -> bool:
        """Prepare the graph for another recovery pass.

        Returns True if anything changed (so the caller knows to loop again).
        Revives:
          * SKIPPED tasks whose dependencies are now SUCCESS (a previously
            failed dependency may have been recovered).
          * FAILED tasks -> reset to PENDING with a fresh attempt budget so the
            full repair + alt-server path runs once more.
        Authorization/confirmation failures are NOT revived (they won't fix
        themselves) — they stay FAILED.
        """
        changed = False
        for t in graph.all():
            if t.status == SKIPPED and graph.deps_met(t):
                t.status = PENDING
                t.notes = ""
                changed = True
            elif t.status == FAILED:
                # If it was denied by policy, leave it failed.
                if t.error and "never authorized" in (t.error or ""):
                    continue
                t.status = PENDING
                t.attempts = 0
                t.error = None
                t.error_signature = None
                t.tried_alts = []
                changed = True
        return changed

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

                # Transient errors get more retries (they often self-heal);
                # logical/validation errors stop sooner.
                cap_retries = 8 if _is_transient(err) else _MAX_REPEAT
                seen_sigs[sig] = seen_sigs.get(sig, 0) + 1
                if seen_sigs[sig] > cap_retries:
                    task.error = err
                    graph.mark_failed(task.id, err, sig)
                    state.record_failure(task.name, err)
                    return

                # B) retry with corrected args (missing-parameter repair)
                corrected, note = _diagnose(err, task)
                decision = None
                if corrected is None:
                    # B2) genuine LLM diagnosis (bounded once per task);
                    # the regex repair above stays as the fast path.
                    decision = self._llm_diagnose(task, err)
                    if decision is not None:
                        if decision["kind"] == "correct_args":
                            corrected, note = (decision["args"],
                                               decision.get("reason", ""))
                        elif decision["kind"] == "switch_tool":
                            # Registry-validated escape hatch: same task,
                            # different live tool.
                            task.tool = decision["tool"]
                            task.server = decision.get("server", task.server)
                            task.attempts = 0
                            self._emit("REPAIRING",
                                       "agent.repair_succeeded",
                                       task=task.id,
                                       note=decision.get("reason", ""))
                            continue
                        else:  # give_up — honest failure with a reason
                            task.error = decision.get(
                                "reason", "model judged unrecoverable")
                            graph.mark_failed(task.id, task.error,
                                              _error_signature(task.error))
                            state.record_failure(task.name, task.error)
                            return
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

                # D) no repair available -> naive retry. The per-task attempt
                # budget (max_attempts) and the repeated-error guard above
                # decide when to give up; this lets flaky/transient failures
                # self-heal instead of failing on the first try, and lets a
                # recovery pass get a fresh attempt at a now-healthy upstream.
                task.error = err
                time.sleep(min(self.backoff_base * (2 ** (task.attempts - 1)),
                               self.max_backoff))
                continue

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
                # Verification failure is ALSO a diagnosis trigger: let the
                # model propose corrected args (bounded once per task).
                vfix = self._llm_diagnose(task, vres.note, phase="verify")
                if vfix is not None and vfix["kind"] == "correct_args":
                    task.args = vfix["args"]
                    args = _resolve_args(task, graph)
                    time.sleep(min(self.backoff_base, self.max_backoff))
                    continue
                time.sleep(min(self.backoff_base, self.max_backoff))
                continue

            self._emit("VERIFYING", "agent.verification_passed", task=task.id)
            task.result = result
            graph.mark_success(task.id, result)
            state.record_success(task.name)
            # Executor-level honesty: an autonomous run that REPLACED an
            # existing file says so loudly (the result carries it; the
            # audit log gets its own entry-shaped event).
            if isinstance(result, dict) and result.get("overwritten"):
                self._emit("EXECUTING", "agent.destructive_overwrite",
                           task=task.id, tool=task.tool,
                           path=result.get("path"))
                task.notes = (task.notes +
                              " | destructive: overwrote existing file"
                              ).strip(" |")
            self._emit("EXECUTING", "agent.tool_succeeded", task=task.id,
                       tool=task.tool)
            return

        task.error = task.error or "exhausted retries"
        graph.mark_failed(task.id, task.error, _error_signature(task.error or ""))
        state.record_failure(task.name, task.error)
