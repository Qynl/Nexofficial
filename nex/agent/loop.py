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

import os
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
from agent.project_state import SYSTEM_COMPLETE, ProjectState
from agent.registry import CapabilityRegistry
from agent.task_graph import TaskGraph, PENDING, RUNNING, SUCCESS, FAILED, SKIPPED
from agent.verification import verify_task
from mcp.capability import CODE_EXECUTION, ToolCapability
from mcp.policy import Decision, authorize, current_policy


@dataclass
class CompletionReport:
    status: str
    goal: str
    completed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    missing: List[Dict[str, Any]] = field(default_factory=list)
    # Criteria declared for the scoped system with no proof from the
    # running game (mandatory-verification gate).
    unverified: List[Dict[str, str]] = field(default_factory=list)
    # Systems this run worked on (a campaign works more than one).
    systems: List[str] = field(default_factory=list)
    # What a run without an engine still produced: the build blueprint.
    blueprint: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "goal": self.goal,
            "completed": self.completed, "failed": self.failed,
            "skipped": self.skipped, "reasons": self.reasons,
            "missing": self.missing,
            "unverified": self.unverified,
            "systems": self.systems,
            "blueprint": self.blueprint,
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


def _diagnose(error: str, task: Any,
              schema_props: Optional[set] = None):
    """Generic, non-hardcoded repair: if the error names a missing argument,
    supply a placeholder default for it and retry.

    SCHEMA-GATED: when the live inputSchema's properties are known
    (`schema_props`), the repair only fires for args the schema actually
    declares. Before this gate, a missing-arg error from an execution
    tool would retry with literal `__default_<name>` values — e.g.
    `execute_luau` would run the string `__default_code` in a LIVE
    editor. When the schema is unknown (None) the old behavior stands:
    a wrong retry is cheap; executing garbage in a real engine is not,
    but unknown-schema tools are the mock/test world where it's safe.
    """
    m = re.search(r"'([a-zA-Z_][a-zA-Z0-9_]*)'", error or "")
    if m:
        field_name = m.group(1)
        if field_name in (task.args or {}):
            return None, None
        if schema_props is not None and field_name not in schema_props:
            return None, None
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


def _critique_feedback(goal: str, critique, state) -> List[str]:
    """Turn a critique into concrete planner feedback strings (the same
    channel judges use)."""
    lines = []
    for f in critique.findings[:5]:
        lines.append("%s: %s" % (f.get("kind"), f.get("message")))
    if getattr(state, "design", None):
        from agent.design import design_summary
        lines.append("Keep implementing THE DESIGN (do not restart, do "
                     "not change locked decisions): "
                     + design_summary(state.design)[:400].replace("\n",
                                                                  " | "))
    if critique.note:
        lines.append("critic note: " + critique.note)
    return lines[:5]


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
                 llm: Optional[Callable] = None,
                 builder: Optional[Callable] = None,
                 max_critique_cycles: int = 1,
                 design_enabled: bool = True,
                 persist: bool = True,
                 workspace_root: Optional[str] = None) -> None:
        self.registry = registry
        self.planner = planner or default_planner
        self.policy = policy or current_policy()
        # --- Director layer (agent.director) ---------------------------------
        # max_systems: how many systems one game may consist of (a weak
        # model asked for "everything" is what this bounds).
        # max_steps_per_system: the scope envelope's hard step budget —
        # the anti-"I improved the entire project" knob.
        self.max_systems = int(os.environ.get("NEX_MAX_SYSTEMS", "6"))
        self.max_steps_per_system = int(
            os.environ.get("NEX_MAX_STEPS_PER_SYSTEM", "4"))
        # --- AAA AUTOMATION (bounded campaign) ---------------------------
        # One run should be able to finish more than one system — that is
        # what "make me a game" means — but never without a budget. The
        # campaign is bounded by BOTH a system count and a wall clock, and
        # every stop is reported with its reason.
        self.max_systems_per_run = max(1, int(
            os.environ.get("NEX_MAX_SYSTEMS_PER_RUN", "2")))
        self.run_budget_s = float(os.environ.get("NEX_RUN_BUDGET_S", "900"))
        # Quality target: the critic may only finish a system at/above it.
        # Expressed 0..1 (the critics score 1-10 internally); the model
        # judges, Nex enforces.
        self.quality_target = float(os.environ.get("NEX_QUALITY_TARGET",
                                                   "0.6"))
        self.max_quality_rounds = max(0, int(
            os.environ.get("NEX_MAX_QUALITY_ROUNDS", "1")))
        self._run_systems: List[str] = []   # systems this run worked on
        self._quality_rounds = 0
        self.game_plan = None      # last GamePlan (director output)
        self._scope: Optional[Dict[str, Any]] = None
        self.audit = audit or AuditLog()
        self.bus = bus
        # CONFIRMATION IS A REAL GATE. Without an explicit approver an
        # autonomous run does NOT assume "yes": a task whose authorization
        # requires a human decision stops here and is reported, instead of
        # quietly executing something nobody approved. The operator can
        # grant a standing approval on purpose (capability file
        # {"approved": true} for one tool, NEX_ALLOW_CODE_EXECUTION for
        # code/process tools on one server) — but never by default.
        self.approver = approver or (lambda task: False)
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
        # The BUILDER is a separate role on purpose: planning/reasoning runs
        # on the planner (local model or GPT), while the work inside the
        # build loop runs on the builder (NVIDIA NIM by default, ~40 RPM).
        # Defaults to `llm` so every existing caller/test keeps its behavior.
        self.builder = builder or llm
        self.last_plan = None
        # NEX 2.0: design-first planning + the build->critique->improve
        # loop. `max_critique_cycles` bounds self-criticism so Nex never
        # churns forever. The critique loop runs only when there is a
        # design document or an LLM critic (a bare skeleton run with no
        # design has nothing to critique against).
        self.max_critique_cycles = max(0, max_critique_cycles)
        self.design_enabled = design_enabled
        self.persist = persist
        self.last_critique = None
        # Workspace the agent writes game files into — the target for
        # experiment snapshots/rollback (defaults to the tools workspace).
        self._workspace_root_override = workspace_root

    # ----- event helper ---------------------------------------------------
    def _emit(self, agent_state=None, event_type=None, **payload) -> None:
        if self.bus is None:
            return
        try:
            self.bus(agent_event(agent_state, event_type, **payload))
        except Exception:  # noqa: BLE001
            pass

    # ----- experiment safety (workspace snapshots) -------------------------
    def _workspace_root(self) -> str:
        """The directory the agent's file-producing tools write into."""
        if self._workspace_root_override:
            return self._workspace_root_override
        try:
            from tools import TOOLS_ROOT  # type: ignore
            return TOOLS_ROOT
        except Exception:  # noqa: BLE001
            import os
            return os.path.join(os.path.expanduser("~"), "nex_workspace")

    def _workspace_snapshot(self, label: str):
        """Snapshot the workspace before a risky improvement run.
        Returns the snapshot path or None (no workspace yet, or I/O error
        — in that case the run simply proceeds without rollback ability)."""
        root = self._workspace_root()
        if not os.path.isdir(root):
            return None
        try:
            from agent.checkpoints import (prune_snapshots,
                                           snapshot_workspace)
            snap = snapshot_workspace(root, label=label)
            if snap is not None:
                prune_snapshots(root, keep=5)
            return snap
        except Exception:  # noqa: BLE001
            return None

    # ----- main entry ------------------------------------------------------
    def run(self, goal: str, state: Optional[ProjectState] = None,
            graph: Optional[TaskGraph] = None,
            resume_checkpoint: Optional[str] = None,
            scope: Optional[Dict[str, Any]] = None,
            game_plan=None) -> CompletionReport:
        original_graph_provided = graph is not None
        self._emit("OBSERVING", "agent.observe",
                   servers=[s.name for s in self.registry.servers])
        if state is None:
            state = ProjectState(goal=goal)
        # Which engines are live in THIS run: the project memory carries it so
        # later reviews (critic/tester) know which engine's defect classes to
        # look for — a Roblox character sinks, an Unreal pawn never moves.
        try:
            from mcp_engines import normalize_platform
            _plats = [normalize_platform(s.name) for s in self.registry.servers]
            state.set_engines([p for p in _plats if p])
        except Exception:  # noqa: BLE001
            pass

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

        # --- NEX 2.0: understand BEFORE planning --------------------------
        # The design document answers WHAT we are building and WHY before
        # any tool call. Structured data (agent.design), stored on the
        # project state, feeds the planner, the critic and the UI.
        if self.design_enabled and not state.design and self.llm is not None \
                and graph is None:
            self._make_design(goal, state)

        # --- GAME DIRECTOR: decompose the request into SYSTEMS -----------
        # "Make me a game" is not a plan. The Director decides which
        # systems the game needs and in what order (recipe library +
        # optional model refinement), stores them in the system map, and
        # picks the ONE system this round is scoped to. The planner then
        # gets a narrow objective instead of the whole project.
        # Direct when we are planning from scratch. A caller that already
        # directed this run (the server path) passes its GamePlan in — and
        # an empty pre-built graph must not silently discard it, because
        # the blueprint, the campaign and the gate all need the systems.
        if game_plan is not None and self.game_plan is None:
            self.game_plan = game_plan
        if graph is None and self.game_plan is None:
            self._direct(goal, state, game_plan=game_plan)
        if scope is not None:
            # The caller already directed this run (server path): adopt its
            # objective instead of silently re-scoping the work.
            self._scope = scope
            state.current_system = scope.get("system") or state.current_system
        self._emit("PLANNING", "agent.plan_started", goal=goal,
                   objective=(self._scope or {}).get("objective", ""),
                   system=(self._scope or {}).get("system", ""))
        if graph is None:
            graph = self._make_plan(goal, state)
        self._design_guard(graph, state)
        self._emit("PLANNING", "agent.plan_updated",
                   task_count=len(graph.all()),
                   stages=[t.stage for t in graph.all()])

        # HONEST BLOCK: zero executable tasks is not PARTIAL (which reads
        # as "we did some of it"). With no tools connected — or a plan
        # that validated down to nothing — the run is BLOCKED with a
        # concrete, actionable reason, and the missing-capability list
        # says exactly what to connect.
        if not graph.all():
            servers = [s.name for s in self.registry.servers]
            # NO ENGINE IS NOT "NO RESULT". Everything the Director knows
            # is deterministic: systems, order, proven structure, success
            # criteria and the capability each step needs. That is a
            # blueprint the user can act on today (connect, or build the
            # parts that need no engine) — and it is explicitly a PLAN,
            # never a claim that something was built.
            blueprint = self._blueprint(goal, state, servers)
            if not servers:
                reason = ("no MCP servers are connected — nothing is "
                          "executable. Connect an editor (Roblox Studio "
                          "/ Unreal / Blender) via the Settings page, "
                          "then re-run. A build blueprint for %d system(s) "
                          "with their checklists is attached."
                          % len(blueprint.get("systems") or []))
            else:
                reason = ("the plan produced no executable tasks against "
                          "the live registry (servers: %s) — every step "
                          "was dropped as missing or invalid. Blueprint "
                          "for %d system(s) attached."
                          % (", ".join(servers),
                             len(blueprint.get("systems") or [])))
            self._emit("BLOCKED", "agent.project_blocked",
                       status=STATUS_BLOCKED, completed=[], failed=[],
                       missing=[{"task": "capability", "stage": "connect",
                                 "server": None, "tool": None,
                                 "reason": reason}])
            from agent.blueprint import blueprint_markdown
            self._emit("PLANNING", "agent.blueprint", goal=goal,
                       game=blueprint.get("game", ""),
                       systems=[{"id": s.get("id"), "title": s.get("title"),
                                 "layer": s.get("layer"),
                                 "ready": s.get("ready"),
                                 "steps": len(s.get("steps") or []),
                                 "criteria": len(s.get("checklist") or [])}
                                for s in blueprint.get("systems") or []],
                       missing_capabilities=blueprint.get(
                           "missing_capabilities", []),
                       tests=len(blueprint.get("tests") or []),
                       markdown=blueprint_markdown(blueprint))
            state.phase = "BLOCKED"
            report = CompletionReport(
                status=STATUS_BLOCKED, goal=goal,
                reasons=[reason],
                missing=[{"task": "capability", "stage": "connect",
                          "server": None, "tool": None, "reason": reason}],
                systems=(list(self._run_systems)
                         or ([state.current_system]
                             if getattr(state, "current_system", "") else [])),
                blueprint=blueprint)
            if self.persist:
                try:
                    from agent.projects_store import save_project
                    save_project(state, report)
                except Exception:  # noqa: BLE001
                    pass
            return report

        # --- CAMPAIGN: work the game plan, not just one system ------------
        # "Make me a game" is 4-8 systems. Bounded by BOTH a system count
        # and a wall clock; whatever stops the campaign is reported.
        # An approved plan is binding: the campaign must NOT continue into
        # the next system, because that work was never signed off on.
        approved_plan = original_graph_provided
        state.phase = "BUILDING"
        if state.current_system and state.current_system not in self._run_systems:
            self._run_systems.append(state.current_system)
        deadline = time.time() + self.run_budget_s
        rounds: List[CompletionReport] = []
        report, work_ok = self._execute_round(goal, state, graph)
        rounds.append(report)

        while (work_ok and not approved_plan and self.game_plan is not None
               and len(rounds) < self.max_systems_per_run
               and time.time() < deadline):
            nxt = self._next_system(state)
            if nxt is None:
                self._emit("PLANNING", "agent.campaign_finished",
                           systems=list(self._run_systems),
                           note="every system in the game plan is done")
                break
            self._emit("PLANNING", "agent.system_started", system=nxt,
                       objective=(self._scope or {}).get("objective", ""),
                       index=len(self._run_systems),
                       of=len(self.game_plan.systems))
            graph = self._make_plan(goal, state)
            if not graph.all():
                self._emit("PLANNING", "agent.campaign_stopped",
                           system=nxt,
                           reason=("no executable tasks for '%s' against "
                                   "the live registry" % nxt))
                break
            report, work_ok = self._execute_round(goal, state, graph)
            rounds.append(report)
        else:
            if approved_plan:
                # not a campaign: exactly the approved plan was executed
                pass
            elif (work_ok and self.game_plan is not None and time.time()
                    >= deadline):
                self._emit("PLANNING", "agent.campaign_stopped",
                           reason="run budget exhausted (NEX_RUN_BUDGET_S)")
            elif (work_ok and self.game_plan is not None
                  and len(rounds) >= self.max_systems_per_run):
                self._emit("PLANNING", "agent.campaign_stopped",
                           reason=("system budget reached "
                                   "(NEX_MAX_SYSTEMS_PER_RUN=%d)"
                                   % self.max_systems_per_run))

        report = self._merge_rounds(goal, state, rounds)

        # Mandatory verification: no "COMPLETED" while the scoped objective
        # still has unproven criteria.
        report = self._completion_gate(state, report)

        # Bounded memory: the state is fed back into every later planning
        # round, so enforce the caps once per run and report what rolled out.
        try:
            trimmed = state.compact_memory()
            if trimmed:
                self._emit("OBSERVING", "agent.memory_compacted",
                           trimmed=trimmed, size=state.memory_size())
        except Exception:  # noqa: BLE001
            pass

        if self.persist:
            try:
                from agent.projects_store import save_project
                save_project(state, report)
            except Exception:  # noqa: BLE001
                pass
        return report

    # ------------------------------------------------------------------
    def _engine_look_for(self, state: ProjectState) -> List[str]:
        """Engine-specific 'what to look at' lines for the running playtest.

        Comes from the curated platform guides; empty when no engine is
        known (the review then stays exactly as it was).
        """
        try:
            from mcp_engines import playtest_guidance_for
            return playtest_guidance_for(getattr(state, "engines", []) or [])
        except Exception:  # noqa: BLE001
            return []

    def _review_system(self, goal: str, state: ProjectState) -> None:
        """REVIEWER role: prove/fail the scoped system's criteria from the
        runtime evidence. Scoped context, validated output, never fatal."""
        sid = getattr(state, "current_system", "")
        if not sid or self.llm is None:
            return
        crit = state.criteria_for(sid)
        if not crit:
            return
        try:
            from agent.roles import (REVIEWER, apply_review, reviewer_context,
                                     reviewer_json, system_prompt)
            obs = list(getattr(state, "observations", []) or [])
            reply = self.llm([{"role": "user", "content":
                               system_prompt(REVIEWER) + "\n\n"
                               + reviewer_context(
                                   crit, obs,
                                   playtest=self._engine_look_for(state))}])
            data = reviewer_json(reply)
            if not data:
                return
            passed = apply_review(data, state, sid)
            self._emit("OBSERVING", "agent.reviewed", system=sid,
                       proven=passed, verdict=data.get("verdict"),
                       criteria=[{"criterion": c,
                                  "state": state.criterion_state(sid, c)}
                                 for c in crit])
        except Exception:  # noqa: BLE001
            pass

    def _close_system(self, state: ProjectState, cycle: int) -> None:
        """Mark the scoped system's checklist as PROVEN (clean observation)
        and move the system map forward."""
        sid = getattr(state, "current_system", "")
        if not sid:
            return
        n = state.prove_criteria(sid, note="observed clean in cycle %d" % cycle)
        state.upsert_system(sid, status="complete",
                            notes="verified by observation")
        self._emit("COMPLETED", "agent.system_verified",
                   system=sid, proven=n,
                   criteria=state.criteria_for(sid),
                   systems={k: (v or {}).get("status")
                            for k, v in (state.systems or {}).items()})
        # Next objective, if the game has one.
        nxt = None
        try:
            gp = self.game_plan
            remaining = [s for s in (gp.systems if gp else [])
                         if s.id != sid and s.status != "complete"]
            nxt = remaining[0].id if remaining else None
        except Exception:  # noqa: BLE001
            nxt = None
        if nxt:
            self._emit("PLANNING", "agent.system_next", system=nxt)

    def _completion_gate(self, state: ProjectState,
                         report: CompletionReport) -> CompletionReport:
        """THE MANDATORY-VERIFICATION GATE.

        A run may not report COMPLETED while the objective it was scoped
        to still has unproven success criteria. The honest outcome is
        PARTIAL plus the exact list of unproven criteria — "Done!" without
        evidence is the most expensive lie an autonomous builder can tell.
        """
        if report.status != STATUS_COMPLETED:
            return report
        # EVERY system this run worked on, not just the last one: a
        # campaign that verified system 1 and guessed at system 3 is not
        # done, and saying so is the point.
        systems = list(self._run_systems) or (
            [state.current_system] if getattr(state, "current_system", "")
            else [])
        if not systems:
            return report
        unmet = []
        for sid in systems:
            for u in state.unmet_criteria(sid):
                unmet.append(dict(u, system=sid))
        if not unmet:
            return report
        report.status = STATUS_PARTIAL
        report.reasons = list(report.reasons) + [
            "NOT verified (no evidence from the running game): %s"
            % uc["criterion"] for uc in unmet[:6]]
        report.unverified = unmet
        self._emit("OBSERVING", "agent.verification_gate",
                   status=report.status, system=sid, unverified=unmet)
        return report

    def _run_passes(self, graph, state) -> None:
        """Execute the graph with multi-pass recovery.
        Pass 1 executes everything that's ready. If failures leave the graph
        non-terminal (skipped dependents / failed leaves), we REVIVE and try
        again (up to `recovery_passes` extra times) instead of stopping.
        This is what keeps Nex from "always stopping" at the first failure:
        it re-runs repair + same-tool-on-another-server recovery before it
        ever reports a task as truly blocked.
        """
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
                self._llm_diagnose_batch(graph, state)
                if not self._revive(graph):
                    break
                continue
            self._llm_diagnose_batch(graph, state)
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

    # ----- NEX 2.0: design-first + critic helpers ------------------------
    def _make_design(self, goal: str, state: ProjectState) -> None:
        """Produce the structured Design Document (one LLM call). Failure
        is honest: the run continues without a design, never with an
        invented one."""
        try:
            from agent.design import (design_prompt, parse_design,
                                      missing_sections, design_summary)
            state.phase = "DESIGNING"
            self._emit("THINKING", "agent.design_started", goal=goal)
            catalog_text = ""
            try:
                from agent.model_planner import _catalog
                catalog_text = _catalog(self.registry, goal)
            except Exception:  # noqa: BLE001
                catalog_text = ""
            prompt = design_prompt(goal, state.engine, catalog_text,
                                   state.locked_decisions())
            reply = self.llm([{"role": "user", "content": prompt}])
            design = parse_design(reply, idea=goal, engine=state.engine)
            if design is None:
                self._emit("PLANNING", "agent.design_failed",
                           note="model returned no parseable design JSON")
                state.phase = "PLANNING"
                return
            state.design = design
            # Design milestones that name concrete tasks become design-
            # grounded hints for the planner (via plan_feedback context).
            missing = missing_sections(design)
            self._emit("PLANNING", "agent.design_ready",
                       design={k: design.get(k) for k in
                               ("concept", "genre", "gameplay_loop",
                                "design_pillars", "mechanics", "world",
                                "milestones", "quality_gates")},
                       summary=design_summary(design),
                       missing=missing)
        except Exception as exc:  # noqa: BLE001
            self._emit("PLANNING", "agent.design_failed", error=repr(exc))
            state.phase = "PLANNING"

    def _design_guard(self, graph, state) -> int:
        """Deterministic design stability: any task whose name re-litigates
        a LOCKED decision is skipped before execution (with the reason
        recorded). The planner seeing locked decisions is the first
        guard; this is the safety net."""
        locked = state.locked_decisions()
        if not locked:
            return 0
        try:
            from agent.design import conflicts_locked
        except ImportError:  # noqa: BLE001
            return 0
        n = 0
        for task in graph.all():
            if task.status != PENDING:
                continue
            dec = conflicts_locked(task.name, locked)
            if dec:
                graph.mark_skipped(task.id,
                                   "conflicts with locked design "
                                   "decision: " + dec)
                n += 1
        if n:
            self._emit("PLANNING", "agent.design_guard",
                       blocked=n,
                       locked=locked)
        return n

    def _critique_and_improve(self, goal: str, state: ProjectState,
                              graph, report):
        """Run the critic and, for POLISH/REPLAN verdicts, do bounded
        improvement runs through the ONE canonical pipeline."""
        if self.max_critique_cycles <= 0:
            return report
        # The critic measures work against the DESIGN DOCUMENT. Without a
        # design there is nothing to critique against — single-goal model
        # runs already have the judge round in server_run; bare skeleton
        # runs would only gain noise.
        if not getattr(state, "design", {}):
            state.phase = ("COMPLETE" if report.status == STATUS_COMPLETED
                           else "PAUSED")
            return report

        try:
            from agent.critic import (critique as run_critique,
                                      ACTION_COMPLETE, ACTION_REPLAN)
        except ImportError:  # noqa: BLE001
            return report

        current = report
        for cycle in range(1, self.max_critique_cycles + 1):
            state.phase = "CRITIQUING"
            # REVIEWER role (before the critic): does the EVIDENCE prove the
            # system's success criteria? This is the other honest half of
            # mandatory verification — the reviewer can PROVE criteria from
            # bounded evidence, while the critic judges overall quality.
            self._review_system(goal, state)
            self._emit("OBSERVING", "agent.critique_started", cycle=cycle)
            c = run_critique(goal, state, current, llm=self.llm,
                             cycle=cycle)
            self.last_critique = c
            state.record_cycle(dict(c.to_dict(), ts=time.time()))
            # Surface observation findings to the UI. (The critic already
            # recorded them in state.known_bugs — project memory.)
            for f in c.findings:
                if f.get("source") == "observation":
                    self._emit("OBSERVING", "agent.bug_recorded",
                               kind=f.get("kind"), message=f.get("message"),
                               severity=f.get("severity"))
            state.phase = ("COMPLETE" if c.action == ACTION_COMPLETE
                           else ("PLANNING" if c.action == ACTION_REPLAN
                                 else "POLISHING"))
            self._emit("COMPLETED" if c.verdict == "PASS" else "OBSERVING",
                       "agent.critique", verdict=c.verdict, action=c.action,
                       findings=c.findings, scores=c.scores, note=c.note,
                       cycle=cycle)
            if c.action == ACTION_COMPLETE:
                # QUALITY TARGET: the critic saying PASS is necessary, not
                # sufficient. Below the target the round is not finished —
                # it is another polish round (bounded), because "AAA" is a
                # number the user set, not a feeling the model had.
                score = self._round_score(c)
                if (score is not None and score < self.quality_target
                        and self._quality_rounds < self.max_quality_rounds):
                    self._quality_rounds += 1
                    self._emit("POLISHING", "agent.quality_gate",
                               score=round(score, 2),
                               target=self.quality_target,
                               round=self._quality_rounds,
                               note=("below target — polishing instead of "
                                     "finishing"))
                    feedback = _critique_feedback(goal, c, state) + [
                        ("Quality score %.2f is below the target %.2f — "
                         "raise it rather than declaring the system done."
                         % (score, self.quality_target))]
                    state.plan_feedback = feedback
                    state.record_cycle(dict(c.to_dict(), ts=time.time(),
                                            quality_gate="polish"))
                    continue
                if score is not None:
                    self._emit("OBSERVING", "agent.quality_gate",
                               score=round(score, 2),
                               target=self.quality_target,
                               round=self._quality_rounds,
                               note="target met — system may close")
                # The round's objective was OBSERVED CLEAN. That is the
                # proof the checklist was waiting for: a criterion is only
                # ever "pass" because the running game (or a reviewer)
                # confirmed it, never because a tool call returned.
                self._close_system(state, cycle)
                if current.status != STATUS_COMPLETED:
                    current.status = (
                        STATUS_COMPLETED if current.completed
                        and not current.failed else current.status)
                return current

            # Improvement run: same pipeline, critique-aware planning.
            feedback = _critique_feedback(goal, c, state)
            state.plan_feedback = feedback
            state.phase = "POLISHING" if c.action != ACTION_REPLAN \
                else "PLANNING"
            self._emit(state.phase, "agent.improve_started",
                       cycle=cycle, action=c.action)
            try:
                g2 = self._make_plan(goal, state)
            except Exception as exc:  # noqa: BLE001
                self._emit("BLOCKED", "agent.improve_plan_failed",
                           error=repr(exc))
                return current
            self._design_guard(g2, state)
            if not g2.all():
                self._emit("OBSERVING", "agent.improve_no_plan",
                           note="critic asked for changes but no "
                                "improvement plan was possible")
                return current
            # The new plan (Plan B) is deliberate: persist it as the
            # project's current approved plan so resume/inspect see the
            # plan Nex is ACTUALLY working from.
            if self.persist:
                try:
                    from agent.projects_store import save_project
                    save_project(state, graph=g2,
                                 plan=getattr(g2, "_model_plan", None))
                except Exception:  # noqa: BLE001
                    pass
            # Experiment safety: the improvement run may change the game.
            # Snapshot the workspace first; if this run makes things WORSE
            # (more failed tasks than before), roll the files back — the
            # plan is kept, the damage is not.
            snap = self._workspace_snapshot("improve_c%d" % cycle)
            before_failed = len(current.failed)
            self._run_passes(g2, state)

            # Merge outcomes.
            succeeded = list(dict.fromkeys(
                list(current.completed)
                + [t.name for t in g2.completed()]))
            failed_names = [t.name for t in g2.failed()]
            skipped = list(dict.fromkeys(
                list(current.skipped) + [t.name for t in g2.skipped()]))
            failed = list(current.failed) + failed_names
            reasons = list(current.reasons) + [
                "%s: %s" % (t.name, t.error or "failed")
                for t in g2.failed()]
            skipped_like = [t for t in g2.skipped()]
            missing = list(current.missing) + [{
                "task": t.name, "stage": t.stage, "server": t.server,
                "tool": t.tool, "reason": t.error or t.notes or "unavailable"}
                for t in skipped_like]
            # Rollback check: the experiment made things WORSE (more failed
            # tasks than before the run) -> restore the snapshot. The plan
            # and the critique memory survive; the file damage does not.
            if snap and len(failed) > before_failed:
                try:
                    from agent.checkpoints import restore_workspace
                    restored = restore_workspace(
                        self._workspace_root(), snap)
                    self._emit("BLOCKED", "agent.experiment_rolled_back",
                               cycle=cycle, restored=restored,
                               note="improvement run made things worse "
                                    "(%d -> %d failed) — workspace "
                                    "restored to pre-change snapshot"
                                    % (before_failed, len(failed)))
                    if restored:
                        state.note_decision(
                            "improvement cycle %d rolled back (made "
                            "things worse); files restored to snapshot"
                            % cycle,
                            reason="experiment safety")
                except Exception:  # noqa: BLE001
                    pass
            if failed and not succeeded:
                status = STATUS_FAILED
            elif failed or skipped:
                status = STATUS_PARTIAL
            else:
                status = STATUS_COMPLETED
            current = CompletionReport(status=status, goal=goal,
                                       completed=succeeded, failed=failed,
                                       skipped=skipped, reasons=reasons,
                                       missing=missing)
            state.completed = succeeded
            state.failed = [{"task": n, "reason": r.split(": ", 1)[-1]}
                            for n, r in zip(failed, reasons)]
        state.phase = ("COMPLETE" if current.status == STATUS_COMPLETED
                       else "PAUSED")
        return current

    # ----- planning (canonical: LLM-driven, skeleton fallback) -----------
    # ------------------------------------------------------------------
    def _direct(self, goal: str, state: ProjectState,
                game_plan=None) -> None:
        """Establish the system map + this round's scope envelope.

        `game_plan` lets the caller (server_run) pass the outcome of a
        Director run that already happened — the server plans BEFORE the
        agent starts, so re-directing here would re-decompose and could
        pick a different objective. Never fatal: without a model (or on
        any failure) the recipe library is the Director."""
        try:
            from agent.director import direct, scope_envelope
            gp = game_plan
            if gp is None:
                gp = direct(goal, llm=self.llm,
                            design=(getattr(state, "design", {}) or None),
                            memory={"systems": (getattr(state, "systems", {})
                                                or {}),
                                    "known_bugs": state.open_bugs()},
                            limit=self.max_systems)
            self.game_plan = gp
            for sys_plan in gp.systems:
                state.upsert_system(sys_plan.id, recipe=sys_plan.recipe,
                                    notes=sys_plan.why)
                state.set_criteria(sys_plan.id, sys_plan.checklist)
                state.set_quality(sys_plan.id, getattr(sys_plan, "quality", []) or [])
            cur = gp.current()
            env = scope_envelope(cur, gp.remaining(),
                                 max_steps=self.max_steps_per_system)
            self._scope = env if cur is not None else None
            state.current_system = (cur.id if cur is not None else "")
            if cur is not None:
                state.upsert_system(cur.id, status="in_progress")
            self._emit("PLANNING", "agent.directed",
                       game=gp.game, source=gp.source,
                       systems=[{"id": s.id, "title": s.title,
                                 "layer": s.layer, "status": s.status,
                                 "criteria": len(s.checklist)}
                                for s in gp.systems],
                       current=state.current_system,
                       objective=(env.get("objective") if env else ""),
                       success=(env.get("success") if env else []))
        except Exception as exc:  # noqa: BLE001
            self._emit("PLANNING", "agent.direct_failed", error=repr(exc))

    def _make_plan(self, goal: str, state: ProjectState) -> TaskGraph:
        """ONE planning path: the model produces the goal-specific plan when
        an llm is attached; the deterministic skeleton is the fallback."""
        if self.llm is not None:
            try:
                from agent.model_planner import model_driven_planner
                feedback = getattr(state, "plan_feedback", None)
                state.plan_feedback = None  # feedback is single-shot
                g, plan = model_driven_planner(
                    goal, self.registry, self.llm,
                    feedback=feedback,
                    design=getattr(state, "design", {}) or None,
                    locked=state.locked_decisions() or None,
                    memory={
                        "known_bugs": state.open_bugs(),
                        "observations": list(
                            getattr(state, "observations", []) or []),
                        "systems": dict(getattr(state, "systems", {}) or {}),
                        "knowledge": dict(getattr(state, "knowledge", {}) or {}),
                    },
                    scope=self._scope)
                if g is not None and g.all():
                    self.last_plan = plan
                    return g
            except Exception:  # noqa: BLE001
                pass
        # NO MODEL (or the model failed). The Director already produced a
        # proven structure for this system, so the plan does not have to
        # be a generic skeleton: it can be the RECIPE, matched against the
        # live catalog by deterministic name rules. The model becomes an
        # optimization, not a prerequisite.
        g = self._recipe_plan(state)
        if g is not None and g.all():
            self._emit("PLANNING", "agent.recipe_planned",
                       system=(self._scope or {}).get("system", ""),
                       task_count=len(g.all()),
                       note="planned from the recipe library (no model call)")
            return g
        from agent.planner import plan as skeleton_plan
        return skeleton_plan(goal, self.registry)

    def _recipe_plan(self, state: ProjectState) -> Optional[TaskGraph]:
        """Build a TaskGraph from the scoped recipe steps + the live tools.

        Deterministic: each step's intent names the capability it needs
        (agent/blueprint.capability_for_intent), and the registry supplies
        the concrete tool. Steps with no matching tool are DROPPED and
        reported — never invented.
        """
        scope = self._scope or {}
        steps = scope.get("steps") or []
        if not steps:
            return None
        from agent.blueprint import _match_tool, capability_for_intent
        from agent.task_graph import TaskGraph, Task
        names = [getattr(t, "name", "") or ""
                 for t in self.registry.all_tools()]
        by_name = {getattr(t, "name", ""): t
                   for t in self.registry.all_tools()}
        graph = TaskGraph()
        prev: Optional[str] = None
        for i, st in enumerate(steps):
            intent = st.get("intent", "") if isinstance(st, dict) else str(st)
            cap = capability_for_intent(intent)
            tool = by_name.get(_match_tool(cap, intent, names))
            if tool is None:
                # No connected tool serves this step. Dropped and reported
                # (the blueprint names the missing capability), never
                # invented.
                continue
            tid = "step_%d_%s" % (i + 1, re.sub(r"[^a-z0-9]+", "_",
                                                cap))[:40]
            t = Task(id=tid, name=intent[:80], stage=cap,
                     server=tool.server, tool=tool.name, args={},
                     notes=intent,
                     expect=(st.get("evidence", "")
                             if isinstance(st, dict) else ""),
                     system=scope.get("system", ""),
                     criteria=list(scope.get("success") or [])[:4])
            if prev:
                t.deps = [prev]
            graph.add(t)
            prev = tid
        return graph

    @staticmethod
    def _round_score(critique: Any) -> Optional[float]:
        """The critic's own numbers as one 0..1 score (None when the model
        returned none — then there is nothing to enforce and the verdict
        stands)."""
        scores = getattr(critique, "scores", None) or {}
        nums = []
        for v in scores.values():
            if isinstance(v, (int, float)):
                nums.append(float(v))
        if not nums:
            return None
        avg = sum(nums) / len(nums)
        return avg / 10.0 if avg > 1.0 else avg

    def _execute_round(self, goal: str, state: ProjectState,
                       graph: TaskGraph):
        """Execute ONE system's graph and critique it. Returns
        (report, work_ok) — `work_ok` is about the WORK (every task ran
        that could run), not about verification, which the campaign must
        not confuse with progress."""
        state.pending = [t.name for t in graph.all() if t.status == PENDING]

        # --- Execution with multi-pass recovery --------------------------
        self._run_passes(graph, state)

        succeeded = [t.name for t in graph.completed()]
        failed = [t.name for t in graph.failed()]
        skipped = [t.name for t in graph.skipped()]

        waiting = [t for t in graph.all() if t.status == "waiting"]
        if not failed and not skipped and succeeded and not waiting:
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
        # A task that stopped for a human decision is not a success and not
        # a failure — it is an open question, and the report says so.
        for t in waiting:
            reasons.append("%s: %s" % (t.name, t.error or
                                       "waiting for confirmation"))

        # Transparency: list the exact capabilities that were needed but
        # could not be delivered, so the user knows precisely what to
        # connect (no silent low-quality partial).
        missing = []
        for t in list(graph.failed()) + list(graph.skipped()) + waiting:
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

        report = CompletionReport(status=status, goal=goal,
                                  completed=succeeded, failed=failed,
                                  skipped=skipped, reasons=reasons,
                                  missing=missing)

        # --- NEX 2.0: build -> critique -> improve (bounded) --------------
        # Nex doesn't get a free pass after one iteration. The critic asks
        # "is this actually GOOD?", and POLISH/REPLAN verdicts send the
        # agent back to work (bounded by max_critique_cycles).
        report = self._critique_and_improve(goal, state, graph, report)
        work_ok = bool(succeeded) and not failed and not skipped \
            and not waiting
        return report, work_ok

    def _blueprint(self, goal: str, state: ProjectState,
                   servers: List[str]) -> Dict[str, Any]:
        """The deterministic build plan (no model, no engine required)."""
        try:
            from agent.blueprint import build_blueprint, blueprint_markdown
            names = []
            for t in self.registry.all_tools():
                names.append(getattr(t, "name", "") or "")
            bp = build_blueprint(
                goal, self.game_plan, names,
                design=getattr(state, "design", {}) or None)
            # Keep the rendered document on the state so a later run (or
            # the project page) has the same plan the user saw.
            state.blueprint_md = blueprint_markdown(bp)
            return bp
        except Exception as exc:  # noqa: BLE001
            self._emit("PLANNING", "agent.blueprint_failed", error=repr(exc))
            return {"goal": goal, "systems": [], "missing_capabilities": [],
                    "tests": []}

    def _next_system(self, state: ProjectState) -> Optional[str]:
        """Advance the game plan to the next system and scope this round
        to it. Returns the system id, or None when the plan is finished."""
        gp = self.game_plan
        if gp is None:
            return None
        sid = getattr(state, "current_system", "")
        for s in gp.systems:
            if s.id == sid:
                s.status = "complete"
        # Honest bookkeeping: advancing the campaign means the WORK is
        # done, not that it is proven. A system whose criteria were never
        # confirmed is recorded as `unverified` — visible in the map, and
        # the completion gate names its criteria.
        if sid:
            if state.system_status(sid) != SYSTEM_COMPLETE:
                state.upsert_system(
                    sid, status="unverified",
                    notes="work done; criteria not proven (no reviewer "
                          "verdict / clean observation)")
        nxt = gp.current()
        if nxt is None:
            return None
        nxt.status = "in_progress"
        state.current_system = nxt.id
        state.upsert_system(nxt.id, status="in_progress")
        from agent.director import scope_envelope
        self._scope = scope_envelope(nxt, gp.remaining(),
                                     max_steps=self.max_steps_per_system)
        if nxt.id not in self._run_systems:
            self._run_systems.append(nxt.id)
        return nxt.id

    def _merge_rounds(self, goal: str, state: ProjectState,
                      rounds: List[CompletionReport]) -> CompletionReport:
        """Aggregate the campaign into ONE honest report.

        The rule: a run is COMPLETED only if EVERY round was. A round that
        did its work but could not prove it stays PARTIAL (the completion
        gate names the unproven criteria) — that distinction is the whole
        point of mandatory verification.
        """
        if len(rounds) == 1:
            return rounds[0]
        completed = [t for r in rounds for t in r.completed]
        failed = [t for r in rounds for t in r.failed]
        skipped = [t for r in rounds for t in r.skipped]
        reasons = [x for r in rounds for x in r.reasons]
        missing = [m for r in rounds for m in r.missing]
        unverified = [u for r in rounds for u in (r.unverified or [])]
        if all(r.status == STATUS_COMPLETED for r in rounds) and completed:
            status = STATUS_COMPLETED
        elif completed and not failed:
            status = STATUS_PARTIAL
        elif not completed:
            status = STATUS_FAILED
        else:
            status = STATUS_PARTIAL
        report = CompletionReport(status=status, goal=goal,
                                  completed=completed, failed=failed,
                                  skipped=skipped, reasons=reasons,
                                  missing=missing, unverified=unverified)
        report.systems = list(self._run_systems)
        return report

    def _queue_diagnosis(self, task: Any, err: str,
                         phase: str = "execute") -> None:
        """Record a failure for the WAVE diagnosis instead of calling now.

        A builder on a rate-limited provider (~40 RPM on NIM) must not spend
        one request per broken task. Every failure that the deterministic
        repairs cannot fix is queued here; `_llm_diagnose_batch` then spends
        exactly ONE provider request for the whole wave (or one focused call
        when only a single task is pending).

        Bounded like before: a task is queued at most once (llm_diagnosed),
        so a broken model can never loop the pipeline.
        """
        if self.builder is None or getattr(task, "llm_diagnosed", False):
            return
        task.llm_diagnose_pending = True
        task.llm_diagnose_error = str(err or "")[:500]
        task.llm_diagnose_phase = phase

    def _llm_diagnose_batch(self, graph: TaskGraph, state: Any) -> int:
        """ONE builder call for ALL failed tasks of this pass.

        A rate-limited builder (~40 RPM on NIM) must not spend a request per
        broken task. Tasks that were already diagnosed inline are skipped, a
        single failure keeps the normal focused path, and any decision that
        cannot be parsed for a task falls back to that task's own call.

        Returns the number of tasks that received a usable decision.
        """
        if self.builder is None:
            return 0
        pending = [t for t in graph.failed()
                   if not getattr(t, "llm_diagnosed", False)]
        if not pending:
            return 0
        from agent.diagnose import diagnose_many
        items = [{"key": t.id, "task": t,
                  "error": (getattr(t, "llm_diagnose_error", "")
                            or getattr(t, "error", "") or "")}
                 for t in pending]
        try:
            decisions = diagnose_many(items, self.registry, self.builder,
                                      phase=getattr(pending[0],
                                                    "llm_diagnose_phase",
                                                    "execute"))
        except Exception:  # noqa: BLE001 — repair must never break the run
            return 0
        applied = 0
        for task in pending:
            task.llm_diagnosed = True          # bounded: once, batch or not
            decision = decisions.get(task.id)
            if decision is None:
                continue
            kind = decision.get("kind")
            self._emit("REPAIRING", "agent.repair_started", task=task.id,
                       note=decision.get("reason", ""), action=kind)
            if kind == "correct_args":
                task.args = dict(decision.get("args") or {})
                applied += 1
            elif kind == "switch_tool":
                task.tool = decision.get("tool")
                task.server = decision.get("server", task.server)
                task.attempts = 0
                applied += 1
            elif kind == "give_up":
                # Honest stop: keep the failure, and remember the model's
                # reason so the report can name it.
                task.llm_gave_up = True
                if decision.get("reason"):
                    task.error = decision["reason"]
                applied += 1
        if applied:
            self._emit("REPAIRING", "agent.repair_batched",
                       tasks=[t.id for t in pending], applied=applied,
                       note="one builder call diagnosed %d failed steps"
                            % len(pending))
        return applied

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
                # A task the builder diagnosed as unrecoverable stays failed —
                # reviving it would burn another pass to reach the same place.
                if getattr(t, "llm_gave_up", False):
                    continue
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
        decision = authorize(task.server, task.tool, cap, self.policy,
                             args=task.args)
        # Laundering: a code-execution call that only names a FILE gets the
        # file's content scanned too — otherwise "write the payload, then
        # run the file" sidesteps the argument scan entirely.
        if (decision.allowed and cap.category == CODE_EXECUTION):
            try:
                from mcp.policy import code_file_argument, scan_file_payload
                fpath = code_file_argument(task.args)
                if fpath:
                    leak = scan_file_payload(
                        fpath, getattr(self, "workspace_root", "") or "")
                    if leak:
                        decision = Decision(False, False, leak,
                                            decision.category)
            except Exception as exc:  # noqa: BLE001
                # FAIL CLOSED: a code-execution call that names a file must
                # be deniable. If the payload scan itself blew up, the safe
                # outcome is NOT to let the call through — an old version
                # swallowed the exception here, which is how a broken scan
                # (a NameError in scan_file_payload) silently disabled the
                # laundering check for every run.
                import sys as _sys
                _sys.stderr.write(
                    "[nex] file payload scan failed for %s: %r — denying "
                    "(fail closed)\n" % (task.tool, exc))
                decision = Decision(False, False,
                                    "file payload scan failed (%r) — "
                                    "denied fail-closed" % (exc,),
                                    decision.category)
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
            task.error = "confirmation required: " + decision.reason
            self.audit.record(task_id=task.id, server=task.server,
                              tool=task.tool, args=task.args,
                              classification=cap.to_dict(),
                              authorized=False,
                              auth_reason="confirmation required",
                              result="waiting")
            self._emit("WAITING", "agent.waiting_for_confirmation",
                       task=task.id, tool=task.tool,
                       server=task.server, category=decision.category,
                       reason=decision.reason)
            self._emit("BLOCKED", "agent.confirmation_required",
                       task=task.id, tool=task.tool, server=task.server,
                       category=decision.category, reason=decision.reason)
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
                tv_now = (self.registry.by_name(task.tool)
                          if task.tool else None)
                schema_props = (set((tv_now.schema or {}).get(
                    "properties", {}) or {})
                    if tv_now and isinstance(getattr(tv_now, "schema",
                                                     None), dict)
                    else None)
                corrected, note = _diagnose(err, task,
                                            schema_props=schema_props)
                if corrected is None:
                    # B2) genuine LLM diagnosis — QUEUED, never called here.
                    # One failure is repaired in the recovery pass by a
                    # single focused call; several failures of the same wave
                    # share ONE batched builder call. The deterministic
                    # alternatives (C/D) below still run first, and the regex
                    # repair above stays the fast path.
                    self._queue_diagnosis(task, err, "execute")
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
                # A verifier rejection gets the same treatment as a tool
                # failure: queued for the wave diagnosis (one call), then
                # retried with corrected args in the recovery pass.
                self._queue_diagnosis(task, vres.note, "verify")
                time.sleep(min(self.backoff_base, self.max_backoff))
                continue

            self._emit("VERIFYING", "agent.verification_passed", task=task.id)
            task.result = result
            graph.mark_success(task.id, result)
            # Scope attribution: "<task>@<system>" lets the critic detect
            # work that left the current objective's cage.
            state.record_success(task.name + ("@" + task.system
                                              if task.system else ""))
            # Mandatory verification, honest half: a successful tool call
            # is INTENT, not proof. The criteria this step was meant to
            # prove are recorded as weak EVIDENCE; only a clean
            # re-observation (here) or a reviewer verdict (roles.REVIEWER)
            # upgrades them to PROVEN.
            for crit in (task.criteria or []):
                state.mark_evidence(task.system, crit,
                                    note="step '%s' ran" % task.name)
            # OBSERVE loop: when the tool IS a runtime observation
            # (launch/screenshot/logs/state/performance), its result is
            # EVIDENCE about the running game — capture it for the critic,
            # the project memory and the UI. Untrusted data: it is judged,
            # never obeyed.
            try:
                from agent.observations import extract_observation
                obs = extract_observation(task.tool, result)
                if obs is not None:
                    obs["task"] = task.name
                    obs["server"] = task.server
                    state.record_observation(obs)
                    self._emit("OBSERVING", "agent.observation",
                               task=task.id, task_name=task.name,
                               server=task.server, kind=obs["kind"],
                               empty=obs["empty"],
                               text=obs["text"][:600],
                               payload=obs["payload"])
            except Exception:  # noqa: BLE001
                pass
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
