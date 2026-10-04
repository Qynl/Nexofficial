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
from agent.engines import (
    planning_brief as engine_planning_brief,
    public_targets as public_engine_targets,
)
from agent.events import (
    STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED, STATUS_BLOCKED,
    STATUS_CANCELLED, emit,
)
from agent.llm import call as call_llm
from agent.mcp_production import (
    context_block, rank_context_resources, server_prompt_names,
)
from agent.model_planner import model_driven_planner, validate_plan_deep
from agent.planner import plan as skeleton_plan
from agent.reversal import plan_reversal
from agent.prompts import EVALUATOR_SYSTEM, SUMMARIZER_SYSTEM
from agent.production import (
    STAGES as PRODUCTION_STAGES,
    is_large_game_goal,
    public_program,
    stage_brief as production_stage_brief,
    stage_evidence as assess_stage_evidence,
)
from agent.quality import (
    assess as assess_quality,
    correction_note as quality_correction_note,
    planning_brief as quality_planning_brief,
    profile_for_goal,
)
from agent.workload import planning_purpose
from agent.task_graph import (
    Task, TaskGraph, SUCCESS, FAILED, SKIPPED, PENDING, RUNNING, WAITING,
)
from agent.jsonreply import extract_json_with_key
from mcp.policy import authorize as authorize_tool

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


DEFAULT_MAX_STEPS = _env_int("NEX_MAX_STEPS", 64)
DEFAULT_MAX_REPLANS = _env_int("NEX_MAX_REPLANS", 3)
DEFAULT_MAX_QUALITY_PASSES = _env_int("NEX_MAX_QUALITY_PASSES", 1)
DEFAULT_MAX_PRODUCTION_STAGES = _env_int("NEX_MAX_PRODUCTION_STAGES", 8)
# A model evaluation after every dependency wave burns hosted RPM without
# adding value when a validated plan is progressing normally. Evaluate at a
# bounded checkpoint, and immediately on stalls/failures.
DEFAULT_EVAL_EVERY_STEPS = max(1, _env_int("NEX_EVAL_EVERY_STEPS", 6))
DEFAULT_BUDGET_S = _env_float("NEX_RUN_BUDGET_S", 1800.0)
# Bounded project-context priming from MCP resources (read-only, audited).
MAX_CONTEXT_RESOURCES = max(0, _env_int("NEX_MAX_CONTEXT_RESOURCES", 6))
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


def _dotted_path(node: Any, key: str) -> Any:
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() \
                and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


_AMBIGUOUS = object()


def _unique_key_value(node: Any, key: str) -> Any:
    """Bounded search for ONE unambiguous occurrence of a leaf key.

    Engine MCP servers wrap payloads inconsistently (``data``, ``result``,
    ``asset``...). A single unambiguous match is honest dataflow; several
    conflicting matches are not, so those resolve to nothing rather than
    silently choosing one.
    """
    found: List[Any] = []
    stack: List[Tuple[Any, int]] = [(node, 0)]
    visited = 0
    while stack and visited < 4000 and len(found) < 4:
        current, depth = stack.pop()
        visited += 1
        if depth > 8:
            continue
        if isinstance(current, dict):
            if key in current:
                found.append(current[key])
            for value in list(current.values())[:200]:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(current, list):
            for value in current[:200]:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
    if not found:
        return None
    if len({repr(item) for item in found}) != 1:
        return _AMBIGUOUS
    return found[0]


def _result_value(result: Any, key: Optional[str]) -> Any:
    """Resolve a `$step` / `$step.key` reference from a tool result.

    Resolution order follows MCP itself, strongest contract first:

      1. ``structuredContent`` — the schema-backed machine-readable payload;
      2. JSON parsed from the text content blocks;
      3. the raw envelope.

    Each candidate is tried with an exact dotted path, then one unambiguous
    nested-key match. A loose textual scan is the final fallback, used only
    when nothing structured matched.
    """
    if result is None:
        return None
    import json as _json

    structured = None
    parsed = result
    if isinstance(result, dict):
        if isinstance(result.get("structuredContent"), (dict, list)):
            structured = result["structuredContent"]
        if isinstance(result.get("content"), list):
            texts = [str(c.get("text", "")) for c in result["content"]
                     if isinstance(c, dict) and c.get("type") == "text"]
            joined = "\n".join(t for t in texts if t)
            try:
                parsed = _json.loads(joined)
            except (ValueError, TypeError):
                parsed = joined if joined else result
    if key is None:
        return structured if structured is not None else parsed

    candidates = [c for c in (structured, parsed, result) if c is not None]
    for candidate in candidates:
        node = _dotted_path(candidate, key)
        if node is not None:
            return node
    leaf = key.split(".")[-1]
    for candidate in candidates:
        node = _unique_key_value(candidate, leaf)
        if node is _AMBIGUOUS:
            # Several conflicting values: guessing one would be dishonest
            # dataflow, and a text scan would guess too.
            return None
        if node is not None:
            return node
    # Textual fallback: only when nothing structured answered.
    try:
        text = parsed if isinstance(parsed, str) else _json.dumps(
            parsed, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(parsed)
    m = re.search(r'"?%s"?\s*[:=]\s*"?([^",\}\]\n]+)' % re.escape(leaf), text)
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
                 max_production_stages: int = DEFAULT_MAX_PRODUCTION_STAGES,
                 eval_every_steps: int = DEFAULT_EVAL_EVERY_STEPS,
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
        self.max_production_stages = max(
            1, min(max_production_stages, len(PRODUCTION_STAGES)))
        self.eval_every_steps = max(1, int(eval_every_steps))
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
        self._steps_at_last_eval = 0
        self._failures_at_last_eval = 0
        self._step_budget_hit = False
        self._replans = 0
        self._evaluations = 0
        self._quality_passes = 0
        self._quality_profile = profile_for_goal(goal)
        self._program_active = is_large_game_goal(goal)
        self._engine_targets: List[Dict[str, Any]] = []
        self._context_block: Optional[str] = None
        self._program_stage = 0
        self._program_completed: List[str] = []
        self._program_reviews: Dict[str, Dict[str, Any]] = {}
        self._eval_note = ""
        self.thread: Optional[threading.Thread] = None

    # ----- plumbing ----------------------------------------------------------

    def _emit(self, event_type: str, **payload: Any) -> None:
        emit(self.bus, self.run_id, event_type,
             conversation_id=self.conversation_id, **payload)

    def _check_stop(self) -> bool:
        return (self._stop.is_set() or self._step_budget_hit or
                (time.time() - self.started_at > self.budget_s))

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
        try:
            self._engine_targets = public_engine_targets(
                self.goal, self.manager.registry())
        except Exception:  # noqa: BLE001 - planning reports registry failures
            self._engine_targets = []
        self._emit("run.started", goal=self.goal,
                   engine_targets=[{
                       "id": target.get("id"),
                       "label": target.get("label"),
                       "version": target.get("version"),
                       "score": target.get("score"),
                   } for target in self._engine_targets])
        if self._program_active:
            self._emit("run.program", program=self._program_public())

        # ---- PLAN ------------------------------------------------------------
        self._emit("run.phase", phase="planning",
                   detail="Deciding how to reach the goal")
        self.graph = self._make_plan()
        self._failures_at_last_eval = len(self.context.failures)
        if not self.graph.all():
            report = self._blocked_report()
            return self._finish(report)

        self._emit("run.plan", plan=self._plan_public(),
                   preflight=self._preflight())

        # ---- EXECUTE / EVALUATE / ADAPT ---------------------------------------
        while not self._check_stop():
            progressed = self._execute_wave()
            if self._stop.is_set() or self._step_budget_hit or \
                    time.time() - self.started_at > self.budget_s:
                break
            if self.graph.is_terminal():
                # Studio-scale goals advance through several independently
                # planned stages. A single successful graph is one production
                # milestone, never proof that an open-world game is finished.
                if self._program_active:
                    if self._advance_production_stage():
                        continue
                    if not self._program_is_complete():
                        break
                # A successful implementation is not automatically a
                # production-quality game. Audit distinct evidence gates and
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
            # A validated plan that is progressing does not need a hosted
            # judgement after every dependency wave. Check at a bounded step
            # interval, or immediately when new failures appear.
            if not self._evaluation_due():
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
        if self._step_budget_hit:
            # Preserve useful completed work as partial rather than reporting a
            # user cancellation or discarding it as a total failure.
            return self._finish(self._build_report(
                None, ["step budget of %d reached" % self.max_steps]))
        return self._finish(self._build_report(None))

    # ----- planning ---------------------------------------------------------

    def _planning_brief(self, registry: Any) -> str:
        parts: List[str] = []
        # Early studio stages should not waste calls proving an intentionally
        # incomplete foundation. The full evidence contract joins the program
        # for validation and polish, then remains active for final scoring.
        if (not self._program_active or
                self._program_stage >= len(PRODUCTION_STAGES) - 2):
            parts.append(quality_planning_brief(
                self._quality_profile, registry))
        if self._program_active:
            parts.append(production_stage_brief(
                self.goal, self._program_stage, registry))
        parts.append(engine_planning_brief(self.goal, registry))
        parts.append(self._project_context(registry))
        return "\n\n".join(p for p in parts if p)

    def _project_context(self, registry: Any) -> str:
        """Read a bounded set of MCP resources describing the live project.

        MCP servers publish project state as resources, not only as tools.
        Reading them is a gated, audited, read-only operation, and it lets a
        plan match real conventions instead of guessing at them. The result
        is cached per run and treated as untrusted data.
        """
        if self._context_block is not None:
            return self._context_block
        self._context_block = ""
        if not (self._quality_profile.active or self._program_active
                or self._engine_targets):
            return ""
        reader = getattr(self.manager, "read_resource", None)
        if not callable(reader):
            return ""
        servers = sorted({name for target in self._engine_targets
                          for name in (target.get("servers") or [])})
        picks = rank_context_resources(registry, self.goal,
                                       servers=servers or None,
                                       limit=MAX_CONTEXT_RESOURCES)
        entries: List[Dict[str, Any]] = []
        for pick in picks:
            if self._check_stop():
                break
            outcome = reader(pick["server"], pick["uri"],
                             audit_context={"run": self.run_id,
                                            "purpose": "project-context"})
            if not isinstance(outcome, dict) or "result" not in outcome:
                continue
            contents = (outcome["result"] or {}).get("contents") or []
            text = "\n".join(str(c.get("text") or "") for c in contents[:8]
                             if isinstance(c, dict))
            if not text.strip():
                continue
            entries.append({
                "source": "%s %s" % (pick["server"], pick["uri"]),
                "text": text,
            })
        prompts = server_prompt_names(registry, servers=servers or None)
        block = context_block(entries)
        if prompts:
            note = ("Server-published MCP prompts available for this project: "
                    + ", ".join(prompts)
                    + ". They are server-authored guidance, not Nex policy.")
            block = (block + "\n" + note) if block else note
        self._context_block = block
        if entries:
            self._emit("run.context", sources=[e["source"] for e in entries],
                       chars=sum(len(e["text"]) for e in entries))
        return block

    def _plan_purpose(self, registry: Any) -> str:
        return planning_purpose(
            self.goal,
            game_production=bool(self._quality_profile.active),
            large_program=self._program_active,
            registry=registry,
        )

    def _make_plan(self) -> TaskGraph:
        reg = self.manager.registry()
        graph, model_plan = model_driven_planner(
            self.goal, reg, llm=self.llm,
            purpose=self._plan_purpose(reg),
            note=self._eval_note,
            quality_brief=self._planning_brief(reg),
            id_prefix=("p0_" if self._program_active else ""),
            production_contract=(self._quality_profile.active
                                 or self._program_active
                                 or bool(self._engine_targets)))
        # Fall back only when no usable model-plan document existed. An
        # explicitly empty or wholly invalid model plan is an honest block,
        # not permission to substitute an unrelated keyword-matched action.
        if not graph.all() and model_plan is None:
            graph = skeleton_plan(self.goal, reg)
        if graph.all():
            errors, warnings = validate_plan_deep(
                _plan_as_dict(graph), reg) if model_plan else ([], [])
            # Missing-required-arg errors: keep the step; the recovery
            # ladder may still fix it at runtime. Report as warnings.
            for w in warnings:
                self.context.failures.append("plan: " + w)
        if self._program_active and graph.all():
            stage = PRODUCTION_STAGES[self._program_stage]
            for task in graph.all():
                task.phase = stage.id
            graph.plan_meta["production_stage"] = stage.to_public()
        return graph

    def _preflight(self) -> Dict[str, Any]:
        """Which planned steps will stop and ask before they run.

        Autonomy fails in practice when a human is paged one step at a time
        with no warning. Nex now states the whole consent surface up front —
        from the live policy, before anything executes — so the operator can
        grant what they accept and leave the rest to interrupt them.
        """
        reg = self.manager.registry()
        trusted = set()
        getter = getattr(self.manager, "trusted_servers", None)
        if callable(getter):
            try:
                trusted = set(getter() or ())
            except Exception:  # noqa: BLE001
                trusted = set()
        will_ask: List[Dict[str, Any]] = []
        autonomous: List[str] = []
        for task in self.graph.all():
            if not task.server or not task.tool:
                continue
            tv = reg.by_name("%s.%s" % (task.server, task.tool))
            if tv is None:
                continue
            decision = authorize_tool(task.server, task.tool, tv.capability)
            untrusted = task.server not in trusted
            if not decision.allowed:
                continue
            if decision.requires_confirmation or untrusted:
                will_ask.append({
                    "step_id": task.id,
                    "step": task.name,
                    "server": task.server,
                    "tool": task.tool,
                    "category": decision.category,
                    "reason": ("server is not marked trusted" if untrusted
                               else decision.reason),
                })
            else:
                autonomous.append(task.id)
        return {
            "will_ask": will_ask[:32],
            "will_ask_count": len(will_ask),
            "autonomous_count": len(autonomous),
            "note": ("Approve these tools in Capabilities to let the run "
                     "continue unattended; otherwise it pauses at each one."),
        }

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
                if self._steps_executed >= self.max_steps:
                    self._step_budget_hit = True
                    self.context.failures.append(
                        "run reached the %d-step budget" % self.max_steps)
                    break
                self._steps_executed += 1
                before = task.status
                self._execute_task(task)
                if task.status != before:
                    progressed = True
                self._emit_progress()
            break
        return progressed

    def _resolve_args(self, task: Task) -> Dict[str, Any]:
        """Recursively fill `$step` / `$step.key` references from dependencies.

        Real engine schemas commonly nest actor ids inside arrays/objects. Every
        exact reference is resolved at any bounded depth and must name a
        declared dependency. Hidden ordering based on list position is refused.
        """
        seen = set()
        nodes = [0]

        def walk(value: Any, path: str, depth: int) -> Any:
            nodes[0] += 1
            if depth > 40 or nodes[0] > 10000:
                raise ArgResolutionError(
                    "argument %r is too deeply nested to resolve safely" % path)
            if isinstance(value, str) and value.startswith("$"):
                m = _REF_RE.match(value)
                if not m:
                    raise ArgResolutionError(
                        "argument %r contains invalid reference %r"
                        % (path, value[:80]))
                ref_name, key = m.group(1), m.group(2)
                ref_task = self._task_by_name(ref_name)
                if ref_task is None:
                    raise ArgResolutionError(
                        "argument %r references step %r which is not in "
                        "the plan" % (path, ref_name))
                if ref_task.id not in task.deps:
                    raise ArgResolutionError(
                        "argument %r references step %r without declaring it "
                        "in depends_on" % (path, ref_name))
                if ref_task.status != SUCCESS:
                    raise ArgResolutionError(
                        "argument %r depends on step %r which did not "
                        "succeed" % (path, ref_name))
                resolved = _result_value(ref_task.result, key)
                if resolved is None:
                    raise ArgResolutionError(
                        "step %r produced no %r to fill argument %r"
                        % (ref_name, key or "value", path))
                return resolved
            if isinstance(value, (dict, list)):
                ident = id(value)
                if ident in seen:
                    raise ArgResolutionError(
                        "argument %r contains a cyclic structure" % path)
                seen.add(ident)
                try:
                    if isinstance(value, dict):
                        return {k: walk(v, "%s.%s" % (path, str(k)[:80]),
                                        depth + 1)
                                for k, v in value.items()}
                    return [walk(v, "%s[%d]" % (path, i), depth + 1)
                            for i, v in enumerate(value)]
                finally:
                    seen.remove(ident)
            return value

        resolved = walk(task.args or {}, "$", 0)
        if not isinstance(resolved, dict):
            raise ArgResolutionError("arguments did not resolve to an object")
        return resolved

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

        call_context = {
            "run": self.run_id,
            "step": task.id,
            "contract_fingerprint": task.contract_fingerprint,
        }
        outcome = self.manager.call(
            task.server, task.tool, args, audit_context=call_context)

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
                task.server, task.tool, args, audit_context=call_context)
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

    def _retry_is_safe(self, task: Task) -> bool:
        """Only repeat a call automatically when live policy says read-only.

        A timed-out mutation may have succeeded upstream before its reply was
        lost. Retrying it could duplicate an asset, purchase, publish, delete,
        or code execution. Server-provided idempotent hints are untrusted and
        cannot downgrade that ambiguity.
        """
        reg = self.manager.registry()
        tv = reg.by_name("%s.%s" % (task.server, task.tool)) \
            if task.server and task.tool else None
        if tv is None and task.tool:
            tv = reg.by_name(task.tool)
        return bool(tv is not None and tv.capability
                    and tv.capability.read_only)

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
            audit_context={
                "run": self.run_id,
                "step": task.id,
                "contract_fingerprint": task.contract_fingerprint,
            })
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

        # A. retry unchanged only when duplicate execution is structurally
        # safe. A timeout on a mutating tool has an UNKNOWN outcome: the call
        # may have completed before the response was lost.
        transient = _is_transient(error)
        if transient and not self._retry_is_safe(task):
            guarded = ("%s; outcome is unknown and Nex did not automatically "
                       "repeat the non-read-only MCP call" % error)
            self.context.record_failure(task.name, task.tool, guarded)
            self.graph.mark_failed(task.id, guarded, sig)
            self._emit("run.tool", step_id=task.id, tool=task.tool,
                       server=task.server, phase="error",
                       preview="unknown outcome — unsafe retry prevented")
            self._emit("run.step", step=task.to_public())
            return
        if transient and not repeated and task.attempts < task.max_attempts:
            self._emit("run.tool", step_id=task.id, tool=task.tool,
                       server=task.server, phase="retry",
                       preview="transient read-only error — retrying")
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

    def _evaluation_due(self) -> bool:
        if self.llm is None:
            return False
        if len(self.context.failures) > self._failures_at_last_eval:
            return True
        return (self._steps_executed - self._steps_at_last_eval
                >= self.eval_every_steps)

    def _evaluate(self) -> Optional[Dict[str, Any]]:
        if self.llm is None:
            return None
        # Advance the checkpoint even when the provider is temporarily down;
        # fallback/recovery belongs to the router, not a tight evaluation loop.
        self._steps_at_last_eval = self._steps_executed
        self._failures_at_last_eval = len(self.context.failures)
        self._evaluations += 1
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
        if self._program_active:
            parts.append("\nLarge-scale production program:\n" +
                         _jsonish(self._program_public()))
        scorecard = self._quality_scorecard()
        quality_due = (not self._program_active or
                       self._program_stage >= len(PRODUCTION_STAGES) - 2)
        if scorecard.get("active") and quality_due:
            parts.append("\nGame-production evidence scorecard:\n" +
                         _jsonish(scorecard))
        elif scorecard.get("active"):
            parts.append("\nFinal quality scoring is deferred until the "
                         "validation stage; evaluate only the current "
                         "production-stage acceptance criteria.")
        parts.append(
            "\nDecide: done? continue? replan? stop? Reply with the JSON "
            "object only.")
        messages = [
            {"role": "system", "content": EVALUATOR_SYSTEM},
            {"role": "user", "content": "\n".join(parts)},
        ]
        try:
            reply = call_llm(self.llm, messages, "evaluation")
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

    # ----- hierarchical game production ----------------------------------------

    def _program_public(self) -> Dict[str, Any]:
        return public_program(
            self.goal, self.manager.registry(), self._program_stage,
            self._program_completed, stage_limit=self.max_production_stages,
            reviews=self._program_reviews)

    def _program_is_complete(self) -> bool:
        return (not self._program_active or
                len(set(self._program_completed)) >= len(PRODUCTION_STAGES))

    def _advance_production_stage(self) -> bool:
        """Close one successful milestone and plan the next one.

        Returns True only when a new executable stage was installed. Every
        prior successful task remains in the graph, so `$slug.field` dataflow
        can cross stage boundaries without rerunning side effects.
        """
        if not self._program_active:
            return False
        stage = PRODUCTION_STAGES[self._program_stage]
        stage_tasks = [t for t in self.graph.all() if t.phase == stage.id]
        if not stage_tasks or any(t.status != SUCCESS for t in stage_tasks):
            self.context.failures.append(
                "production stage '%s' did not complete" % stage.label)
            return False
        review = assess_stage_evidence(
            stage, self.manager.registry(), stage_tasks)
        self._program_reviews[stage.id] = review
        if not review.get("passed"):
            missing = ", ".join(review.get("missing") or [])
            self.context.failures.append(
                "production stage '%s' lacks evidence: %s" %
                (stage.label, missing))
            correctable = review.get("correctable") or []
            if (correctable and self.llm is not None and
                    self._replans < self.max_replans and not self._check_stop()):
                note = (
                    "Current production stage '%s' is not complete. Add only "
                    "work proving these missing machine-audited requirements: "
                    "%s. Reuse all successful work."
                    % (stage.label, ", ".join(correctable)))
                self._eval_note = note
                return self._replan({
                    "done": False, "adjust": "replan",
                    "reason": "stage evidence is incomplete", "note": note})
            return False
        if stage.id not in self._program_completed:
            self._program_completed.append(stage.id)
        self._emit("run.program", program=self._program_public())

        if self._program_stage + 1 >= self.max_production_stages:
            if self.max_production_stages < len(PRODUCTION_STAGES):
                self.context.failures.append(
                    "production stage cap reached (%d/%d)" %
                    (self.max_production_stages, len(PRODUCTION_STAGES)))
            return False
        if self.llm is None or self._check_stop():
            self.context.failures.append(
                "the next production stage needs an agent model")
            return False
        if self._steps_executed >= self.max_steps:
            self.context.failures.append(
                "production stage budget ended after '%s'" % stage.label)
            return False

        self._program_stage += 1
        next_stage = PRODUCTION_STAGES[self._program_stage]
        self._emit("run.phase", phase="planning",
                   detail="Planning stage %d/%d · %s" %
                          (self._program_stage + 1,
                           self.max_production_stages, next_stage.label))
        self._emit("run.program", program=self._program_public())

        completed = [t for t in self.graph.all() if t.status == SUCCESS]
        completed_refs = {(t.slug or t.id): t.id for t in completed}
        recent = completed[-24:]
        summary = "\n".join(
            "- DONE $%s: %s (%s) — %s" %
            (t.slug or t.id, t.name, t.tool,
             result_preview(t.result, 100))
            for t in recent)
        reg = self.manager.registry()
        graph, _ = model_driven_planner(
            self.goal, reg, llm=self.llm,
            purpose="planning-hard",
            note=("The previous production stage completed. Preserve and "
                  "reuse these results; plan only the newly assigned stage:\n"
                  + (summary or "(no reusable outputs)")),
            quality_brief=self._planning_brief(reg),
            completed_refs=completed_refs,
            id_prefix="p%d_" % self._program_stage,
            production_contract=True)
        if graph is None or not graph.all():
            self.context.failures.append(
                "could not make an executable plan for production stage '%s'"
                % next_stage.label)
            return False

        new_graph = TaskGraph()
        for t in completed:
            new_graph.add(Task(
                id=t.id, name=t.name, slug=t.slug, server=t.server,
                tool=t.tool, args=t.args, deps=[],
                contract_fingerprint=t.contract_fingerprint, status=SUCCESS,
                result=t.result, expect=t.expect, why=t.why,
                phase=t.phase))
        for t in graph.all():
            if t.status == PENDING:
                t.phase = next_stage.id
                new_graph.add(t)
        if not new_graph.pending():
            self.context.failures.append(
                "production stage '%s' contained no new work" %
                next_stage.label)
            return False
        new_graph.plan_meta = dict(getattr(graph, "plan_meta", {}) or {})
        new_graph.plan_meta["production_stage"] = next_stage.to_public()
        self.graph = new_graph
        self._emit("run.plan", plan=self._plan_public(),
                   production_stage=self._program_stage)
        return True

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
            purpose=self._plan_purpose(reg),
            note=("Previous attempt: %s\nWhat already succeeded (do NOT "
                  "repeat it):\n%s\nWhat to change: %s"
                  % (verdict.get("reason", ""), summary or "(nothing)",
                     note or "")),
            feedback=feedback,
            quality_brief=self._planning_brief(reg),
            completed_refs=completed_refs,
            id_prefix="r%d_" % self._replans,
            production_contract=(self._quality_profile.active
                                 or self._program_active
                                 or bool(self._engine_targets)))
        if graph is None or not graph.all():
            return False
        # Keep completed history; adopt the new pending steps.
        new_graph = TaskGraph()
        for t in completed:
            t2 = Task(id=t.id, name=t.name, slug=t.slug,
                      server=t.server, tool=t.tool,
                      args=t.args, deps=[],
                      contract_fingerprint=t.contract_fingerprint,
                      status=SUCCESS, result=t.result,
                      expect=t.expect, why=t.why,
                      phase=t.phase)
            new_graph.add(t2)
        # The replan planner has already namespaced new task ids while leaving
        # dependencies on historical success ids intact.
        for t in graph.all():
            if t.status == PENDING:
                if self._program_active:
                    t.phase = PRODUCTION_STAGES[self._program_stage].id
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
        plan_meta = getattr(self.graph, "plan_meta", {}) or {}
        missing = plan_meta.get("missing_tools") or []
        dropped = plan_meta.get("dropped") or []
        out_reasons = list(reasons or [])
        if (dropped or missing) and status == STATUS_COMPLETED:
            status = STATUS_PARTIAL
        if dropped:
            out_reasons.append(
                "plan validation dropped %d invalid step(s): %s" %
                (len(dropped), "; ".join(str(x) for x in dropped[:3])))
        if missing:
            out_reasons.append(
                "plan referenced unavailable tools: " +
                ", ".join(str(x) for x in missing[:6]))
        if status == STATUS_BLOCKED:
            out_reasons.append(
                "no executable plan could be made for this goal")
        program = self._program_public() if self._program_active else {
            "active": False, "scope": "single_run", "complete": True}
        if program.get("active") and not program.get("complete"):
            if status == STATUS_COMPLETED:
                status = STATUS_PARTIAL
            current_obj = program.get("current") or {}
            current = current_obj.get("label", "next stage")
            out_reasons.append(
                "large-scale production program incomplete at: " + current)
            review = (program.get("stage_reviews") or {}).get(
                current_obj.get("id"))
            if review and review.get("missing"):
                out_reasons.append(
                    "stage evidence missing: " +
                    ", ".join(review.get("missing") or []))
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
            "model_evaluations": self._evaluations,
            "evaluation_interval_steps": self.eval_every_steps,
            "quality_passes": self._quality_passes,
            "quality": quality,
            "mcp_production_review": plan_meta.get("production_review") or {},
            "reversal": self._reversal_plan(),
            "engine_targets": list(self._engine_targets),
            "production_program": program,
            "duration_s": round(time.time() - self.started_at, 1),
        }

    def _reversal_plan(self) -> Dict[str, Any]:
        """Compensating actions for this run's mutations (never executed here)."""
        try:
            return plan_reversal(self.graph.all(), self.manager.registry())
        except Exception as exc:  # noqa: BLE001 - reporting must not fail a run
            return {"available": False, "mutations": 0, "reversible": 0,
                    "irreversible": 0, "coverage_pct": 0, "steps": [],
                    "blocked": [],
                    "note": "reversal analysis unavailable: %s" % exc}

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
        if (report.get("production_program") or {}).get("active"):
            self._emit("run.program",
                       program=report["production_program"], final=True)
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
                reply = call_llm(self.llm, [
                    {"role": "system", "content": SUMMARIZER_SYSTEM},
                    {"role": "user", "content":
                     "Goal: %s\n\nReport:\n%s" % (
                         self.goal, _jsonish(report))},
                ], "summary")
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
            "mcp_production_review": dict(
                (getattr(self.graph, "plan_meta", {}) or {}).get(
                    "production_review") or {}),
            "engine_targets": list(self._engine_targets),
            "production_program": (
                self._program_public() if self._program_active else
                {"active": False}),
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
    program = report.get("production_program") or {}
    if program.get("active"):
        completed_stages = program.get("completed_stages") or []
        lines.append("\nProduction program: **%d/%d stages** complete." %
                     (len(completed_stages),
                      program.get("stages_total", 0)))
        if not program.get("complete"):
            current = (program.get("current") or {}).get("label", "next stage")
            lines.append("Current/incomplete stage: %s." % current)
        readiness = program.get("readiness") or {}
        lines.append("Connected MCP production readiness: **%d/100**." %
                     readiness.get("score", 0))
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

    def reversal(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The compensation plan for a finished run (read-only)."""
        run = self.get(run_id)
        if run is None:
            return None
        return (run.report or {}).get("reversal") or run._reversal_plan()

    def revert(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Execute a run's compensating actions, newest mutation first.

        Each step goes through manager.call, so it is authorized, possibly
        confirmation-gated, and audited exactly like any other mutation.
        Execution stops at the first failure: a half-applied compensation
        chain is reported as-is rather than pushed further out of shape.
        """
        run = self.get(run_id)
        if run is None:
            return None
        if run.status in ("running", "starting"):
            return {"ok": False, "error": "run is still active; cancel it "
                                          "before reverting"}
        plan = self.reversal(run_id) or {}
        steps = plan.get("steps") or []
        applied: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for step in steps:
            if failed:
                break
            label = "%s.%s" % (step.get("server"), step.get("tool"))
            try:
                outcome = self.manager.call(
                    step["server"], step["tool"], step.get("args") or {},
                    audit_context={"run": run_id, "purpose": "reversal",
                                   "undoes": step.get("undoes_tool"),
                                   "contract_fingerprint":
                                       step.get("contract_fingerprint", "")})
            except Exception as exc:  # noqa: BLE001
                failed.append({"tool": label, "error": str(exc)[:200]})
                break
            if "result" in outcome:
                applied.append({"tool": label,
                                "undoes": step.get("undoes_tool"),
                                "identity": step.get("identity")})
            else:
                failed.append({
                    "tool": label,
                    "error": str(outcome.get("refused")
                                 or outcome.get("error"))[:200]})
        return {
            "ok": not failed,
            "run": run_id,
            "applied": applied,
            "failed": failed,
            "not_attempted": max(0, len(steps) - len(applied) - len(failed)),
            "irreversible": plan.get("blocked") or [],
            "note": ("Compensating actions ran newest-first. "
                     "%d applied, %d failed, %d mutation(s) never had a "
                     "compensating action."
                     % (len(applied), len(failed),
                        len(plan.get("blocked") or []))),
        }

    def waiting(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.public() for r in self._runs.values() if r._approval]

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.public() for r in self._runs.values()]
