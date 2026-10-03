"""The agent engine — plan → act → observe → evaluate → adapt.

One `AgentRun` executes one goal against the live capability surface
(the MCP servers the operator connected). The loop:

    PLAN       model proposes steps, validated against the live registry
        ↓
    EXECUTE    each ready step: authorize → call → observe (bounded)
        ↓      failures climb a recovery ladder:
        ↓        retry → fix args → switch tool → honest failure
    EVALUATE   model judges progress against the goal
        ↓
    ADAPT      re-plan with evidence when the plan is wrong (bounded)
        ↓
    COMPLETE   an honest report + a user-facing summary

Every transition emits a semantic event the UI renders as progress.
Events describe execution state, never private chain-of-thought.

Boundaries (all enforced here, not by prompts):
  * the ONLY action path is manager.call() → mcp.policy.authorize →
    the server's transport. There is no other effect in this file.
  * tool output is DATA: it is truncated into bounded observations and
    never parsed as instructions.
  * budgets: wall-clock, total steps, replans, retries per step. A run
    that hits a budget reports honestly instead of looping forever.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent import diagnose
from agent.context import RunContext, result_preview, result_to_text
from agent.events import (
    STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED, STATUS_BLOCKED,
    STATUS_CANCELLED, emit,
)
from agent.model_planner import model_driven_planner, validate_plan_deep
from agent.planner import plan as skeleton_plan
from agent.prompts import EVALUATOR_SYSTEM, SUMMARIZER_SYSTEM
from agent.quality import (
    assess as assess_quality,
    correction_note as quality_correction_note,
    planning_brief as quality_planning_brief,
    profile_for_goal,
)
from agent.task_graph import (
    Task, TaskGraph, SUCCESS, FAILED, SKIPPED, PENDING, RUNNING, WAITING,
)
from agent.jsonreply import extract_json_with_key

# ---------------------------------------------------------------------------
# Defaults (overridable via env by the operator, never by the model)
# ---------------------------------------------------------------------------

import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


DEFAULT_MAX_STEPS = _env_int("NEX_MAX_STEPS", 24)
DEFAULT_MAX_REPLANS = _env_int("NEX_MAX_REPLANS", 2)
DEFAULT_MAX_QUALITY_PASSES = _env_int("NEX_MAX_QUALITY_PASSES", 1)
DEFAULT_BUDGET_S = _env_float("NEX_RUN_BUDGET_S", 900.0)
DEFAULT_APPROVAL_TIMEOUT_S = _env_float("NEX_APPROVAL_TIMEOUT_S", 600.0)

_MAX_REPEAT = 2          # retries for the SAME error signature


def _error_signature(error: str) -> str:
    norm = re.sub(r"'[^']*'", "'?'", error or "")
    norm = re.sub(r"\d+", "N", norm)
    return norm[:120]


def _is_transient(error: str) -> bool:
    """Transient errors (timeouts, unreachable, rate limits, 5xx) are
    worth retrying; logical/validation errors are not."""
    return bool(re.search(
        r"(timeout|timed out|unreachable|connection reset|econnrefused|"
        r"refused|503|502|504|circuit breaker|rate limit|temporarily|"
        r"broken pipe|reset by peer)", error or "", re.IGNORECASE))


def _arg_fix(task: Task, error: str,
             schema_props: Optional[set]) -> Optional[Dict[str, Any]]:
    """Deterministic repair: the error names a missing parameter that the
    live schema knows — supply a placeholder default and retry."""
    if not schema_props:
        return None
    m = re.search(
        r"(?:missing|required|expects?|needs?|no parameter|unknown parameter)"
        r"[^a-z0-9_]*['\"]?([a-z_][a-z0-9_]*)['\"]?", (error or ""),
        re.IGNORECASE)
    if not m:
        return None
    name = m.group(1)
    if name not in schema_props or name in (task.args or {}):
        return None
    args = dict(task.args or {})
    args[name] = ""
    return args


def _result_value(result: Any, key: Optional[str]) -> Any:
    """Resolve a `$step` / `$step.key` reference from a tool result.

    MCP results are envelopes; the useful payload is usually the text
    content (often itself JSON). We dig for `key` at every level we can
    honestly find it, and fall back to a regex scan of the text.
    """
    if result is None:
        return None
    parsed = result
    if isinstance(parsed, dict) and isinstance(parsed.get("content"), list):
        texts = [str(c.get("text", "")) for c in parsed["content"]
                 if isinstance(c, dict) and c.get("type") == "text"]
        joined = "\n".join(t for t in texts if t)
        try:
            import json as _json
            parsed = _json.loads(joined)
        except (ValueError, TypeError):
            parsed = joined if joined else result
    if key is None:
        return parsed
    # Walk dicts by dotted path.
    node = parsed
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            node = None
            break
    if node is not None:
        return node
    # Regex fallback over the textual form.
    try:
        import json as _json
        text = parsed if isinstance(parsed, str) else _json.dumps(
            parsed, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(parsed)
    m = re.search(r'"?%s"?\s*[:=]\s*"?([^",\}\]\n]+)' % re.escape(key), text)
    return m.group(1).strip() if m else None


_REF_RE = re.compile(r"^\$([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_.-]+))?$")


class ArgResolutionError(Exception):
    """A $reference in a step's arguments could not be resolved."""


class ApprovalRequest:
    def __init__(self, step: Task, reason: str, decision: Dict[str, Any]):
        self.step = step
        self.reason = reason
        self.decision = decision
        self.event = threading.Event()
        self.answer: Optional[Dict[str, Any]] = None   # {"approved": bool, "always": bool}

    def resolve(self, approved: bool, always: bool = False) -> None:
        self.answer = {"approved": approved, "always": always}
        self.event.set()


class AgentRun:
    """One goal execution. Created by RunCoordinator.start()."""

    def __init__(self, run_id: str, goal: str, manager: Any,
                 llm: Optional[Callable] = None,
                 bus: Optional[Callable] = None,
                 conversation_id: Optional[str] = None,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 max_replans: int = DEFAULT_MAX_REPLANS,
                 max_quality_passes: int = DEFAULT_MAX_QUALITY_PASSES,
                 budget_s: float = DEFAULT_BUDGET_S,
                 on_summary: Optional[Callable] = None):
        self.run_id = run_id
        self.goal = goal
        self.manager = manager
        self.llm = llm
        self.bus = bus
        self.conversation_id = conversation_id
        self.max_steps = max_steps
        self.max_replans = max_replans
        self.max_quality_passes = max(0, max_quality_passes)
        self.budget_s = budget_s
        self.on_summary = on_summary      # (run, text) -> None
        self._stop = threading.Event()
        self._approval: Optional[ApprovalRequest] = None
        self._approval_lock = threading.Lock()
        self.graph = TaskGraph()
        self.context = RunContext(goal)
        self.status = "starting"
        self.report: Dict[str, Any] = {}
        self.started_at = time.time()
        self._steps_executed = 0
        self._replans = 0
        self._quality_passes = 0
        self._quality_profile = profile_for_goal(goal)
        self._eval_note = ""
        self.thread: Optional[threading.Thread] = None

    # ----- plumbing ----------------------------------------------------------

    def _emit(self, event_type: str, **payload: Any) -> None:
        emit(self.bus, self.run_id, event_type,
             conversation_id=self.conversation_id, **payload)

    def _check_stop(self) -> bool:
        return self._stop.is_set() or (time.time() - self.started_at
                                       > self.budget_s)

    def cancel(self) -> None:
        self._stop.set()
        with self._approval_lock:
            if self._approval is not None:
                self._approval.resolve(approved=False)
                self._approval = None

    def resolve_approval(self, approved: bool, always: bool = False) -> bool:
        with self._approval_lock:
            req = self._approval
            self._approval = None
        if req is None:
            return False
        req.resolve(approved, always)
        return True

    # ----- the loop ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        self.status = "running"
        self._emit("run.started", goal=self.goal)

        # ---- PLAN ------------------------------------------------------------
        self._emit("run.phase", phase="planning",
                   detail="Deciding how to reach the goal")
        self.graph = self._make_plan()
        if not self.graph.all():
            report = self._blocked_report()
            return self._finish(report)

        self._emit("run.plan", plan=self._plan_public())

        # ---- EXECUTE / EVALUATE / ADAPT ---------------------------------------
        while not self._check_stop():
            progressed = self._execute_wave()
            if self._stop.is_set():
                break
            if self.graph.is_terminal():
                # A successful implementation is not automatically a
                # production-quality game.  Audit distinct evidence gates and
                # use one bounded corrective plan when suitable live tools
                # were available but omitted.
                if self._try_quality_pass():
                    continue
                break
            if not progressed:
                # Nothing ready and not terminal — stuck deps or all
                # waiting; evaluate before declaring anything.
                verdict = self._evaluate()
                if verdict is None or verdict.get("adjust") == "stop":
                    break
                if verdict.get("done"):
                    break
                if verdict.get("adjust") == "replan":
                    if not self._replan(verdict):
                        break
                elif not progressed:
                    break   # nothing more we can do
                continue
            verdict = self._evaluate()
            if self._stop.is_set():
                break
            if verdict is None:
                continue
            if verdict.get("done"):
                break
            if verdict.get("adjust") == "stop":
                break
            if verdict.get("adjust") == "replan":
                if not self._replan(verdict):
                    break

        if self._stop.is_set():
            return self._finish(self._build_report(STATUS_CANCELLED,
                                                   ["stopped by the user"]))
        if time.time() - self.started_at > self.budget_s:
            return self._finish(self._build_report(
                STATUS_FAILED, ["run budget of %ds exhausted"
                                % int(self.budget_s)]))
        return self._finish(self._build_report(None))

    # ----- planning ---------------------------------------------------------

    def _make_plan(self) -> TaskGraph:
        reg = self.manager.registry()
        graph, _plan = model_driven_planner(
            self.goal, reg, llm=self.llm,
            note=self._eval_note,
            quality_brief=quality_planning_brief(
                self._quality_profile, reg))
        if not graph.all():
            graph = skeleton_plan(self.goal, reg)
        if graph.all():
            errors, warnings = validate_plan_deep(
                _plan_as_dict(graph), reg) if _plan else ([], [])
            # Missing-required-arg errors: keep the step; the recovery
            # ladder may still fix it at runtime. Report as warnings.
            for w in warnings:
                self.context.failures.append("plan: " + w)
        return graph

    def _plan_public(self) -> Dict[str, Any]:
        meta = dict(getattr(self.graph, "plan_meta", {}) or {})
        return {
            "title": meta.get("title") or "Plan",
            "rationale": meta.get("rationale") or "",
            "steps": [t.to_public() for t in self.graph.all()],
        }

    # ----- execution ----------------------------------------------------------

    def _execute_wave(self) -> bool:
        """Run every currently-ready step (sequentially — MCP servers are
        often single-session). Returns True if any step changed state."""
        progressed = False
        while not self._check_stop():
            ready = self.graph.ready()
            if not ready:
                break
            for task in ready:
                if self._check_stop():
                    break
                self._steps_executed += 1
                if self._steps_executed > self.max_steps:
                    self._stop.set()
                    self.context.failures.append(
                        "run exceeded the %d-step budget" % self.max_steps)
                    break
                before = task.status
                self._execute_task(task)
                if task.status != before:
                    progressed = True
                self._emit_progress()
            break
        return progressed

    def _resolve_args(self, task: Task) -> Dict[str, Any]:
        """Fill `$step` / `$step.key` references from dependency results.

        A reference that cannot be resolved at execution time is a hard
        step failure — sending the raw "$name.key" string to a server
        would be quiet garbage, never an honest action.
        """
        args = dict(task.args or {})
        for k, v in list(args.items()):
            if isinstance(v, str) and v.startswith("$") and len(v) > 1:
                m = _REF_RE.match(v)
                if not m:
                    continue
                ref_name, key = m.group(1), m.group(2)
                # Find the referenced task (by plan name → id map).
                ref_task = self._task_by_name(ref_name)
                if ref_task is None:
                    raise ArgResolutionError(
                        "argument %r references step %r which is not in "
                        "the plan" % (k, ref_name))
                if ref_task.status != SUCCESS:
                    raise ArgResolutionError(
                        "argument %r depends on step %r which did not "
                        "succeed" % (k, ref_name))
                val = _result_value(ref_task.result, key)
                if val is None:
                    raise ArgResolutionError(
                        "step %r produced no %r to fill argument %r"
                        % (ref_name, key or "value", k))
                args[k] = val
        return args

    def _task_by_name(self, name: str) -> Optional[Task]:
        name_l = (name or "").lower()
        # Newer re-plan tasks come after historical successes. Prefer them
        # when a corrective plan reuses a human slug such as "verify-game".
        tasks = self.graph.all()
        for t in reversed(tasks):
            if t.slug.lower() == name_l or t.id == name_l:
                return t
        for t in reversed(tasks):
            if t.name.lower() == name_l:
                return t
        return None

    def _execute_task(self, task: Task) -> None:
        self.graph.mark_running(task.id)
        self._emit("run.step", step=task.to_public())

        try:
            args = self._resolve_args(task)
        except ArgResolutionError as exc:
            self.graph.mark_failed(task.id, str(exc),
                                   _error_signature(str(exc)))
            self.context.record_failure(task.name, task.tool, str(exc))
            self._emit("run.tool", step_id=task.id, tool=task.tool,
                       server=task.server, phase="error",
                       preview=str(exc)[:200])
            self._emit("run.step", step=task.to_public())
            return
        self._emit("run.tool", step_id=task.id, tool=task.tool,
                   server=task.server, phase="called",
                   args=_public_args(args))

        outcome = self.manager.call(
            task.server, task.tool, args,
            audit_context={"run": self.run_id, "step": task.id})

        # --- user approval --------------------------------------------------
        if "needs_confirmation" in outcome:
            approved = self._await_approval(task, outcome, args)
            if self._stop.is_set():
                return
            if not approved:
                self.graph.mark_failed(
                    task.id, "user declined the confirmation",
                    _error_signature("user declined"))
                self.context.record_failure(task.name, task.tool,
                                            "user declined confirmation")
                self._emit("run.step", step=task.to_public())
                return
            outcome = self.manager.call(
                task.server, task.tool, args,
                audit_context={"run": self.run_id, "step": task.id})
            if "needs_confirmation" in outcome:
                # Fail closed if a buggy/custom manager did not consume the
                # exact approval. Never reinterpret another prompt as success.
                outcome = {"refused":
                           "the one-time approval did not match this exact call"}

        # --- refusal (policy) -------------------------------------------------
        if "refused" in outcome:
            task.attempts += 1
            self.graph.mark_failed(task.id, outcome["refused"],
                                   _error_signature(outcome["refused"]))
            self.context.record_failure(task.name, task.tool,
                                        outcome["refused"])
            self._emit("run.tool", step_id=task.id, tool=task.tool,
                       server=task.server, phase="error",
                       preview=outcome["refused"][:200])
            self._emit("run.step", step=task.to_public())
            return

        # --- failure → recovery ladder ---------------------------------------
        if "error" in outcome:
            self._emit("run.tool", step_id=task.id, tool=task.tool,
                       server=task.server, phase="error",
                       preview=str(outcome["error"])[:200])
            self._recover(task, str(outcome["error"]))
            return

        # --- success ------------------------------------------------------------
        result = outcome.get("result")
        self.graph.mark_success(task.id, result)
        text = self.context.observe(task.name, task.tool or "?",
                                    task.server or "?", result, ok=True)
        self._emit("run.tool", step_id=task.id, tool=task.tool,
                   server=task.server, phase="ok",
                   preview=result_preview(result))
        self._emit("run.step", step=task.to_public())

    def _await_approval(self, task: Task, outcome: Dict[str, Any],
                        resolved_args: Dict[str, Any]) -> bool:
        req = ApprovalRequest(task, outcome.get("needs_confirmation", ""),
                              outcome.get("decision", {}))
        with self._approval_lock:
            self._approval = req
        self.graph.mark_waiting(task.id)
        self._emit("run.waiting", kind="approval",
                   step=task.to_public(), tool=task.tool, server=task.server,
                   # Approval is for the resolved arguments, not the plan's
                   # pre-reference placeholders. The policy caps inspected
                   # payloads so this remains bounded.
                   args=resolved_args, reason=req.reason,
                   decision=req.decision)
        self._emit("run.phase", phase="waiting",
                   detail="Waiting for your approval")
        deadline = time.time() + DEFAULT_APPROVAL_TIMEOUT_S
        while not req.event.wait(0.25):
            if self._stop.is_set() or time.time() > deadline:
                with self._approval_lock:
                    self._approval = None
                self._emit("run.phase", phase="executing",
                           detail="Approval timed out — continuing without")
                return False
        answer = req.answer or {"approved": False}
        if answer.get("approved"):
            if answer.get("always"):
                self.manager.approve_tool(task.server, task.tool)
            else:
                # One-shot approval is argument-bound and consumed by the
                # immediately retried manager call.
                approve_once = getattr(self.manager, "approve_once", None)
                if callable(approve_once):
                    approve_once(task.server, task.tool, resolved_args)
        self.graph.mark_pending(task.id)
        self._emit("run.resumed", step_id=task.id,
                   approved=bool(answer.get("approved")))
        self._emit("run.phase", phase="executing", detail="Executing steps")
        return bool(answer.get("approved"))

    # ----- recovery ------------------------------------------------------------

    def _attempt(self, task: Task) -> Optional[Dict[str, Any]]:
        """One try at calling the task's tool with resolved args.

        Returns the outcome dict on a COMPLETED call (success, refusal,
        or error — the caller decides), or None when the attempt could
        not even be made (unresolvable $reference). On success the
        bookkeeping (graph, context, events) is done here.
        """
        try:
            args = self._resolve_args(task)
        except ArgResolutionError as exc:
            self._emit("run.tool", step_id=task.id, tool=task.tool,
                       server=task.server, phase="error",
                       preview=str(exc)[:200])
            return None
        outcome = self.manager.call(
            task.server, task.tool, args,
            audit_context={"run": self.run_id, "step": task.id})
        if "result" in outcome:
            self.graph.mark_success(task.id, outcome["result"])
            self.context.observe(task.name, task.tool or "?",
                                 task.server or "?",
                                 outcome["result"], ok=True)
            self._emit("run.tool", step_id=task.id, tool=task.tool,
                       server=task.server, phase="ok",
                       preview=result_preview(outcome["result"]))
            self._emit("run.step", step=task.to_public())
            return outcome
        return outcome

    def _recover(self, task: Task, error: str) -> None:
        """The ladder: retry → fix args → switch tool → fail honestly."""
        sig = _error_signature(error)
        repeated = (task.error_signature == sig)
        task.error_signature = sig
        task.attempts += 1
        self.context.record_failure(task.name, task.tool, error)

        def outcome_error(outcome: Dict[str, Any]) -> str:
            if "error" in outcome:
                return str(outcome["error"])
            return "refused: " + str(outcome.get(
                "refused", outcome.get("needs_confirmation", "")))

        # A. retry unchanged (transient error, within budget)
        if _is_transient(error) and not repeated \
                and task.attempts < task.max_attempts:
            self._emit("run.tool", step_id=task.id, tool=task.tool,
                       server=task.server, phase="retry",
                       preview="transient error — retrying")
            outcome = self._attempt(task)
            if outcome is None:
                self.graph.mark_failed(
                    task.id, "arguments could not be resolved", sig)
                self._emit("run.step", step=task.to_public())
                return
            if "result" in outcome:
                return
            error = outcome_error(outcome)

        # B. fix the arguments (deterministic first, then LLM — bounded)
        fixed = self._fix_args(task, error)
        if fixed is not None:
            task.args = fixed
            self._emit("run.tool", step_id=task.id, tool=task.tool,
                       server=task.server, phase="retry",
                       preview="adjusted arguments — retrying")
            outcome = self._attempt(task)
            if outcome is None:
                self.graph.mark_failed(
                    task.id, "arguments could not be resolved", sig)
                self._emit("run.step", step=task.to_public())
                return
            if "result" in outcome:
                return
            error = outcome_error(outcome)

        # C. model diagnosis: repair args or switch to an alternative tool
        if not task.llm_diagnosed:
            task.llm_diagnosed = True
            decision = diagnose.diagnose(task, error,
                                         self.manager.registry(), self.llm)
            if decision and decision["kind"] == "correct_args":
                task.args = decision["args"]
                self._emit("run.tool", step_id=task.id, tool=task.tool,
                           server=task.server, phase="retry",
                           preview="repaired arguments — retrying")
                outcome = self._attempt(task)
                if outcome is None:
                    self.graph.mark_failed(
                        task.id, "arguments could not be resolved", sig)
                    self._emit("run.step", step=task.to_public())
                    return
                if "result" in outcome:
                    return
                error = outcome_error(outcome)
            elif decision and decision["kind"] == "switch_tool":
                task.tried_alts.append(task.tool or "")
                task.server = decision["server"]
                task.tool = decision["tool"]
                self._emit("run.tool", step_id=task.id, tool=task.tool,
                           server=task.server, phase="retry",
                           preview="switched tool — retrying")
                outcome = self._attempt(task)
                if outcome is None:
                    self.graph.mark_failed(
                        task.id, "arguments could not be resolved", sig)
                    self._emit("run.step", step=task.to_public())
                    return
                if "result" in outcome:
                    return
                error = outcome_error(outcome)

        # D. honest failure
        self.graph.mark_failed(task.id, error, sig)
        self._emit("run.step", step=task.to_public())

    def _fix_args(self, task: Task, error: str) -> Optional[Dict[str, Any]]:
        props = self._schema_props(task)
        fixed = _arg_fix(task, error, props)
        if fixed is not None:
            return fixed
        return None

    def _schema_props(self, task: Task) -> Optional[set]:
        reg = self.manager.registry()
        tv = reg.by_name("%s.%s" % (task.server, task.tool)) \
            if task.server else None
        if tv is None and task.tool:
            tv = reg.by_name(task.tool)
        if tv is None:
            return None
        schema = tv.schema if isinstance(tv.schema, dict) else {}
        return set((schema.get("properties") or {}).keys())

    # ----- evaluation ---------------------------------------------------------

    def _evaluate(self) -> Optional[Dict[str, Any]]:
        if self.llm is None:
            return None
        self._emit("run.phase", phase="evaluating",
                   detail="Checking progress against the goal")
        parts = [
            "Goal: %s" % self.goal,
            "",
            "Step outcomes:",
        ]
        parts.extend(self.context.step_lines(self.graph))
        parts.append("")
        parts.append("Evidence:\n" + self.context.evidence_block())
        failures = self.context.failure_block()
        if failures:
            parts.append("\nOpen failures:\n" + failures)
        scorecard = self._quality_scorecard()
        if scorecard.get("active"):
            parts.append("\nGame-production evidence scorecard:\n" +
                         _jsonish(scorecard))
        parts.append(
            "\nDecide: done? continue? replan? stop? Reply with the JSON "
            "object only.")
        messages = [
            {"role": "system", "content": EVALUATOR_SYSTEM},
            {"role": "user", "content": "\n".join(parts)},
        ]
        try:
            reply = self.llm(messages)
        except Exception:  # noqa: BLE001
            return None
        obj = extract_json_with_key(reply, "done")
        if not isinstance(obj, dict):
            return None
        adjust = str(obj.get("adjust") or "none")
        if adjust not in ("none", "replan", "stop"):
            adjust = "none"
        note = str(obj.get("note") or obj.get("reason") or "")[:400]
        verdict = {"done": bool(obj.get("done")), "adjust": adjust,
                   "reason": str(obj.get("reason") or "")[:300],
                   "note": note}
        self._emit("run.eval", verdict=verdict)
        if adjust == "replan" and note:
            self._eval_note = note
        return verdict

    # ----- production quality review -------------------------------------------

    def _quality_scorecard(self) -> Dict[str, Any]:
        """Assess only successful calls to tools in the current live registry."""
        return assess_quality(self._quality_profile, self.manager.registry(),
                              self.graph.all())

    def _try_quality_pass(self) -> bool:
        """Start one bounded evidence/polish pass when it can improve proof.

        Missing *capabilities* do not cause a loop: they are reported as
        unavailable. Only gates with suitable live tools are correctable.
        """
        scorecard = self._quality_scorecard()
        if not scorecard.get("active"):
            return False
        self._emit("run.quality", scorecard=scorecard)
        if scorecard.get("passed"):
            return False
        if not scorecard.get("correctable"):
            return False
        if self.llm is None or self._quality_passes >= self.max_quality_passes:
            return False
        if self._replans >= self.max_replans or self._check_stop():
            return False
        note = quality_correction_note(scorecard)
        if not note:
            return False
        self._eval_note = note
        self._emit("run.phase", phase="evaluating",
                   detail="Reviewing production evidence (%d/100)" %
                          scorecard.get("score", 0))
        if not self._replan({
                "done": False,
                "adjust": "replan",
                "reason": "production evidence is incomplete",
                "note": note}):
            return False
        self._quality_passes += 1
        return True

    # ----- adaptation ------------------------------------------------------------

    def _replan(self, verdict: Dict[str, Any]) -> bool:
        if self._replans >= self.max_replans:
            self.context.failures.append(
                "replan budget exhausted (%d)" % self.max_replans)
            return False
        self._replans += 1
        self._emit("run.phase", phase="adapting",
                   detail="Adjusting the plan (attempt %d)" % self._replans)
        completed = [t for t in self.graph.all() if t.status == SUCCESS]
        completed_refs = {(t.slug or t.id): t.id for t in completed}
        summary = "\n".join(
            "- DONE $%s: %s (%s) — %s" %
            (t.slug or t.id, t.name, t.tool,
             result_preview(t.result, 100))
            for t in completed)
        note = self._eval_note or verdict.get("note") or verdict.get("reason")
        feedback = self.context.failures[-6:]
        reg = self.manager.registry()
        graph, _ = model_driven_planner(
            self.goal, reg, llm=self.llm,
            note=("Previous attempt: %s\nWhat already succeeded (do NOT "
                  "repeat it):\n%s\nWhat to change: %s"
                  % (verdict.get("reason", ""), summary or "(nothing)",
                     note or "")),
            feedback=feedback,
            quality_brief=quality_planning_brief(
                self._quality_profile, reg),
            completed_refs=completed_refs,
            id_prefix="r%d_" % self._replans)
        if graph is None or not graph.all():
            return False
        # Keep completed history; adopt the new pending steps.
        new_graph = TaskGraph()
        for t in completed:
            t2 = Task(id=t.id, name=t.name, slug=t.slug,
                      server=t.server, tool=t.tool,
                      args=t.args, deps=[], status=SUCCESS,
                      result=t.result, expect=t.expect, why=t.why)
            new_graph.add(t2)
        # The replan planner has already namespaced new task ids while leaving
        # dependencies on historical success ids intact.
        for t in graph.all():
            if t.status == PENDING:
                new_graph.add(t)
        new_graph.plan_meta = dict(getattr(graph, "plan_meta", {}) or {})
        if not new_graph.pending():
            return False
        self.graph = new_graph
        self._emit("run.plan", plan=self._plan_public(),
                   replan=self._replans)
        return True

    # ----- completion ------------------------------------------------------------

    def _emit_progress(self) -> None:
        tasks = self.graph.all()
        done = sum(1 for t in tasks if t.status in (SUCCESS, FAILED, SKIPPED))
        current = next((t for t in tasks if t.status == RUNNING), None)
        self._emit("run.progress", done=done, total=len(tasks),
                   note=(current.name if current else ""))

    def _build_report(self, status_override: Optional[str],
                      reasons: Optional[List[str]] = None) -> Dict[str, Any]:
        tasks = self.graph.all()
        completed = [t for t in tasks if t.status == SUCCESS]
        failed = [t for t in tasks if t.status == FAILED]
        skipped = [t for t in tasks if t.status == SKIPPED]
        unfinished = [t for t in tasks
                      if t.status not in (SUCCESS, FAILED, SKIPPED)]
        if status_override is not None:
            status = status_override
        elif not tasks:
            status = STATUS_BLOCKED
        elif not completed and (failed or unfinished):
            status = STATUS_FAILED
        elif failed or skipped or unfinished:
            status = STATUS_PARTIAL
        else:
            status = STATUS_COMPLETED
        missing = (getattr(self.graph, "plan_meta", {})
                   .get("missing_tools") or [])
        out_reasons = list(reasons or [])
        if status == STATUS_BLOCKED:
            out_reasons.append(
                "no executable plan could be made for this goal")
        quality = self._quality_scorecard()
        if quality.get("active") and not quality.get("passed"):
            # A graph where every mutation returned successfully can still be
            # unproven as a playable, visual, stable product. Preserve that
            # distinction in the machine-readable outcome.
            if status == STATUS_COMPLETED:
                status = STATUS_PARTIAL
            missing_gates = quality.get("missing") or []
            if missing_gates:
                out_reasons.append(
                    "production evidence incomplete: " +
                    ", ".join(missing_gates))
        return {
            "status": status,
            "goal": self.goal,
            "steps_total": len(tasks),
            "completed": [{"name": t.name, "tool": t.tool,
                           "preview": result_preview(t.result)}
                          for t in completed],
            "failed": ([{"name": t.name, "tool": t.tool,
                         "error": (t.error or "")[:200]} for t in failed]
                       + [{"name": t.name, "tool": t.tool,
                           "error": "step was not executed (plan stalled)"}
                          for t in unfinished]),
            "skipped": [{"name": t.name, "note": t.notes} for t in skipped],
            "reasons": out_reasons,
            "missing": missing,
            "replans": self._replans,
            "quality_passes": self._quality_passes,
            "quality": quality,
            "duration_s": round(time.time() - self.started_at, 1),
        }

    def _blocked_report(self) -> Dict[str, Any]:
        summary = self.manager.summary()
        if summary["connected"] == 0:
            reason = ("no MCP servers are connected — connect capability "
                      "servers in Settings → Capabilities, then try again")
        else:
            reason = ("the connected servers expose no tools that can "
                      "serve this goal")
        self.context.failures.append(reason)
        return self._build_report(STATUS_BLOCKED, [reason])

    def _finish(self, report: Dict[str, Any]) -> Dict[str, Any]:
        self.report = report
        self.status = report["status"]
        self._emit("run.phase", phase="finishing",
                   detail="Writing the summary")
        if (report.get("quality") or {}).get("active"):
            self._emit("run.quality", scorecard=report["quality"], final=True)
        text = self._summarize(report)
        self._emit("run.completed", report=report, summary=text)
        if self.on_summary is not None:
            try:
                self.on_summary(self, text, report)
            except Exception:  # noqa: BLE001
                pass
        return report

    def _summarize(self, report: Dict[str, Any]) -> str:
        if self.llm is not None:
            try:
                reply = self.llm([
                    {"role": "system", "content": SUMMARIZER_SYSTEM},
                    {"role": "user", "content":
                     "Goal: %s\n\nReport:\n%s" % (
                         self.goal, _jsonish(report))},
                ])
                if reply and isinstance(reply, str) and len(reply.strip()) > 10:
                    return reply.strip()
            except Exception:  # noqa: BLE001
                pass
        return _fallback_summary(report)

    # ----- introspection for the API ------------------------------------------

    def public(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "goal": self.goal,
            "status": self.status,
            "started_at": self.started_at,
            "steps": [t.to_public() for t in self.graph.all()],
            "waiting": (self._approval is not None),
            "quality": self._quality_scorecard(),
        }


def _public_args(args: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in (args or {}).items():
        s = repr(v)
        out[k] = s if len(s) <= 120 else s[:117] + "..."
    return out


def _jsonish(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, indent=1, default=str)
    except (TypeError, ValueError):
        return str(obj)


def _fallback_summary(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    status = report.get("status")
    goal = report.get("goal", "")
    if status == STATUS_COMPLETED:
        lines.append("Done — **%s**" % goal)
    elif status == STATUS_PARTIAL:
        lines.append("Partially done — **%s**" % goal)
    elif status == STATUS_BLOCKED:
        lines.append("I couldn't work on **%s**." % goal)
    elif status == STATUS_CANCELLED:
        lines.append("Stopped — **%s**" % goal)
    else:
        lines.append("Failed — **%s**" % goal)
    for r in (report.get("reasons") or [])[:3]:
        lines.append("\n" + r)
    done = report.get("completed") or []
    if done:
        lines.append("\nCompleted:")
        lines.extend("- %s" % d["name"] for d in done[:10])
    failed = report.get("failed") or []
    if failed:
        lines.append("\nFailed:")
        lines.extend("- %s — %s" % (f["name"], f["error"])
                     for f in failed[:6])
    skipped = report.get("skipped") or []
    if skipped:
        lines.append("\nSkipped: %d step(s)" % len(skipped))
    quality = report.get("quality") or {}
    if quality.get("active"):
        gates = quality.get("gates") or []
        passed = sum(1 for g in gates if g.get("status") == "passed")
        lines.append("\nProduction evidence: **%d/100** (%d/%d gates)." %
                     (quality.get("score", 0), passed, len(gates)))
        if quality.get("missing"):
            lines.append("Unverified: %s." %
                         ", ".join(quality.get("missing") or []))
        if quality.get("unavailable"):
            lines.append("No connected MCP capability for: %s." %
                         ", ".join(quality.get("unavailable") or []))
        lines.append("This is an evidence score, not a guarantee of AAA or "
                     "commercial quality.")
    return "\n".join(lines)


def _plan_as_dict(graph: TaskGraph) -> Dict[str, Any]:
    return {"steps": [
        {"name": t.id, "tool": (t.server + "." + t.tool) if t.server
         else (t.tool or ""), "args": t.args}
        for t in graph.all()]}


# ---------------------------------------------------------------------------
# Coordinator — what the server talks to
# ---------------------------------------------------------------------------

class RunCoordinator:
    """Owns active runs. `start()` spawns the thread; `resolve()` and
    `cancel()` are the user's levers."""

    def __init__(self, manager: Any, llm_factory: Callable,
                 bus: Optional[Callable] = None,
                 on_summary: Optional[Callable] = None):
        self.manager = manager
        self.llm_factory = llm_factory      # () -> llm callable (or None)
        self.bus = bus
        self.on_summary = on_summary
        self._runs: Dict[str, AgentRun] = {}
        self._lock = threading.Lock()

    def start(self, goal: str, conversation_id: Optional[str] = None,
              **opts: Any) -> str:
        run_id = "run-" + uuid.uuid4().hex[:10]
        run = AgentRun(
            run_id, goal, self.manager,
            llm=self.llm_factory(),
            bus=self.bus,
            conversation_id=conversation_id,
            on_summary=self.on_summary,
            **opts)
        with self._lock:
            self._runs[run_id] = run
        run.thread = threading.Thread(
            target=self._run_wrapper, args=(run,), daemon=True,
            name="nex-run-%s" % run_id)
        run.thread.start()
        return run_id

    def _run_wrapper(self, run: AgentRun) -> None:
        try:
            run.run()
        except Exception as exc:  # noqa: BLE001
            report = run._build_report(
                STATUS_FAILED, ["internal error: %s" % exc])
            run._finish(report)
        finally:
            with self._lock:
                # keep the last 50 finished runs for the API
                if len(self._runs) > 50:
                    dead = [rid for rid, r in self._runs.items()
                            if r.status not in ("running", "starting")
                            and r.thread is not None
                            and not r.thread.is_alive()]
                    for rid in dead[:len(self._runs) - 50]:
                        self._runs.pop(rid, None)

    def get(self, run_id: str) -> Optional[AgentRun]:
        with self._lock:
            return self._runs.get(run_id)

    def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None:
            return False
        run.cancel()
        return True

    def resolve(self, run_id: str, approved: bool,
                always: bool = False) -> bool:
        run = self.get(run_id)
        if run is None:
            return False
        return run.resolve_approval(approved, always)

    def waiting(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.public() for r in self._runs.values() if r._approval]

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.public() for r in self._runs.values()]
