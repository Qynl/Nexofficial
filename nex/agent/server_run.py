"""Server-facing integration for the autonomous agent (STAGE 5/16/19/21/24).

Thin glue: build a live CapabilityRegistry from the running TunnelRegistry,
plan a goal (model-driven when a model is reachable, capability skeleton
otherwise), and run the AutonomousAgent. Keeps the HTTP layer (server.py) a
thin entrypoint and the agent logic independently testable.

Public:
  * agent_capabilities()            — compact capability summary of live MCP
  * run_agent_goal(goal, **opts)   — plan + (optionally) build + judge
"""

from __future__ import annotations

import os

from typing import Any, Callable, Dict, List, Optional


def _registry() -> Any:
    from tunnels import get_tunnels
    from agent.registry import CapabilityRegistry
    # Use the live Upstream objects (not status-dict summaries) so the
    # registry can discover tools/resources/prompts directly.
    return CapabilityRegistry.from_upstreams(get_tunnels().raw_upstreams())


def _design_goal(goal: str, reg, bus, llm_call, reachable: bool,
                 state=None, policy=None) -> Dict[str, Any]:
    """NEX 2.0 design phase: understand BEFORE building.

    Produces the structured Design Document (one model call), then a DRAFT
    task plan that implements it (validated against the live registry),
    persists the project, and emits `agent.design_ready` — the Plan Page
    renders that. Nothing is built until the user approves.
    """
    from agent.project_state import ProjectState

    if llm_call is None or not reachable:
        # Honest: design requires the model. A skeleton run is still
        # possible (build mode without design), but no design doc can be
        # invented here.
        if bus is not None:
            bus({"type": "agent.design_failed",
                 "error": "no model reachable — the design stage needs "
                          "the model (skeleton builds run without one)",
                 "goal": goal})
        return {"mode": "design", "goal": goal, "ok": False,
                "error": "model unreachable"}

    st = state or ProjectState(goal=goal, engine=None)
    st.goal = st.goal or goal
    st.phase = "DESIGNING"

    from agent.loop import AutonomousAgent
    designer = AutonomousAgent(reg, bus=bus, policy=policy,
                               llm=llm_call, persist=False)
    designer._make_design(goal, st)
    if not st.design or not st.design.get("concept"):
        return {"mode": "design", "goal": goal, "ok": False,
                "error": "design did not parse"}

    # Draft plan implementing the design (validated, not executed).
    design = st.design
    locked = st.locked_decisions()
    graph, plan = _build_plan(goal, reg, llm_call, True,
                              design=design, locked=locked)
    plan_steps = (plan or {}).get("steps", []) if isinstance(plan, dict)         else []

    # Persist so START BUILD builds EXACTLY this plan (Plan A is stored
    # with the project; approval is binding, not a suggestion).
    try:
        from agent.projects_store import save_project, new_project_id
        st.project_id = st.project_id or new_project_id(goal)
        st.phase = "PLANNING"
        save_project(st, graph=graph, plan=plan)
    except Exception:  # noqa: BLE001
        pass

    if bus is not None:
        bus({
            "type": "agent.design_ready",
            "goal": goal,
            "project_id": getattr(st, "project_id", ""),
            "design": {k: design.get(k) for k in
                       ("concept", "genre", "engine", "gameplay_loop",
                        "design_pillars", "mechanics", "world", "story",
                        "audio", "visual_direction", "milestones",
                        "dependencies", "acceptance_criteria", "tests",
                        "quality_gates")},
            "plan": {"title": (plan or {}).get("title") if isinstance(plan, dict) else None,
                     "steps": plan_steps,
                     "model_driven": plan is not None,
                     "stage_count": len(graph.all()) if graph else 0},
            "locked": locked,
        })
    return {"mode": "design", "goal": goal, "ok": True,
            "project_id": getattr(st, "project_id", ""),
            "design": design,
            "plan": plan,
            "stages": [t.stage for t in graph.all()] if graph else []}


def agent_capabilities() -> Dict[str, Any]:
    """Compact capability summary of every live MCP server."""
    try:
        reg = _registry()
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc), "servers": []}
    return reg.compact_summary()


def _attach_scope(graph, scope) -> None:
    """Give a pre-built graph (approved plan bridge) the system attribution
    and criteria of the current objective, so verification can run."""
    if graph is None or not scope:
        return
    sysname = scope.get("system") or ""
    crit = list(scope.get("success") or [])[:4]
    for t in graph.all():
        if not getattr(t, "system", ""):
            t.system = sysname
        if not getattr(t, "criteria", None):
            t.criteria = list(crit)


def _direct(goal: str, state, llm_call, reachable: bool,
            bus: Optional[Callable] = None):
    """Run the Game Director for a server-side run.

    Returns the SCOPE ENVELOPE (the single objective this round is scoped
    to) or None. Also writes the system map + checklists onto the project
    state, so the reviewer/tester/gate in the agent loop can use them.
    Never fatal — without a model the recipe library is the Director.
    """
    try:
        from agent.director import direct as direct_goal, scope_envelope
        from agent.loop import _safe_json  # noqa: F401  (import sanity)
    except Exception:  # noqa: BLE001
        return None
    try:
        llm = llm_call if reachable else None
        gp = direct_goal(
            goal, llm=llm,
            design=(getattr(state, "design", {}) or None) if state else None,
            memory={"systems": getattr(state, "systems", {}) or {},
                    "known_bugs": (state.open_bugs()
                                   if hasattr(state, "open_bugs") else [])},
            limit=int(os.environ.get("NEX_MAX_SYSTEMS", "6")))
        if state is not None:
            for sys_plan in gp.systems:
                state.upsert_system(sys_plan.id, recipe=sys_plan.recipe,
                                    notes=sys_plan.why)
                state.set_criteria(sys_plan.id, sys_plan.checklist)
        cur = gp.current()
        if cur is None:
            return None, gp
        if state is not None:
            state.current_system = cur.id
            state.upsert_system(cur.id, status="in_progress")
        env = scope_envelope(
            cur, gp.remaining(),
            max_steps=int(os.environ.get("NEX_MAX_STEPS_PER_SYSTEM", "4")))
        if bus is not None:
            bus({"type": "agent.directed", "goal": goal, "game": gp.game,
                 "source": gp.source, "current": cur.id,
                 "objective": env.get("objective"),
                 "success": env.get("success"),
                 "systems": [{"id": s.id, "title": s.title,
                              "layer": s.layer, "status": s.status,
                              "criteria": len(s.checklist)}
                             for s in gp.systems]})
        return env, gp
    except Exception as exc:  # noqa: BLE001
        if bus is not None:
            bus({"type": "agent.direct_failed", "error": repr(exc)})
        return None


def _build_plan(goal: str, reg, llm_call, reachable: bool,
                feedback: Optional[List[str]] = None,
                design: Optional[Dict[str, Any]] = None,
                locked: Optional[List[str]] = None,
                scope: Optional[Dict[str, Any]] = None):
    """Return (TaskGraph, plan_dict|None). Uses the model when reachable.
    Design-aware: when a Design Document exists, the plan implements it."""
    from agent.planner import plan as default_planner
    from agent.model_planner import model_driven_planner
    if llm_call is not None and reachable:
        return model_driven_planner(goal, reg, llm_call, feedback=feedback,
                                    design=design, locked=locked,
                                    scope=scope)
    graph = default_planner(goal, reg)
    if scope:
        # The deterministic skeleton has no system attribution; give it the
        # scope so evaluation/attribution still work without a model.
        sysname = scope.get("system") or ""
        for t in graph.all():
            if not getattr(t, "system", ""):
                t.system = sysname
            if not getattr(t, "criteria", None):
                t.criteria = list(scope.get("success") or [])[:4]
    return graph, None


def run_agent_goal(goal: str,
                   bus: Optional[Callable] = None,
                   policy=None,
                   llm_call: Optional[Callable] = None,
                   llm_reachable: bool = False,
                   mode: str = "build",
                   judge_iterations: int = 1,
                   graph: Optional[Any] = None,
                   state: Optional[Any] = None,
                   registry: Optional[Any] = None) -> Any:
    """Plan (and optionally build + judge) a goal against the live registry.

    `mode`:
      * "design" — NEX 2.0: produce the Design Document + a DRAFT task plan,
                  persist the project, emit agent.design_ready, do NOT build.
                  This is what the Plan Page renders for approval.
      * "plan"  — produce the plan + a graph, emit a plan event, do NOT execute.
      * "build" — execute and (if a model is available) run quality judges,
                  optionally rebuilding once on a failing verdict. The
                  build -> critique -> improve loop runs inside the agent.

    `llm_call` is an injected ``llm(messages) -> str`` (the server passes its
    own model_chat so this module stays model-server agnostic). `llm_reachable`
    gates whether we bother asking the model at all.

    Emits semantic agent events on `bus` (the server EventBus) so the frontend
    can react (face state, plan panel, judge readout).
    """
    from agent.loop import AutonomousAgent
    from mcp.policy import current_policy, set_policy
    from agent.judges import judge

    if policy is not None:
        set_policy(policy)
    # `registry` injection (tests / embedding); default: the LIVE
    # registry built from the running tunnel registry.
    reg = registry if registry is not None else _registry()
    pol = current_policy()

    if mode == "design":
        return _design_goal(goal, reg, bus, llm_call, llm_reachable,
                            state=state, policy=pol)

    # --- GAME DIRECTOR: decompose into systems + scope this round --------
    # Even when a pre-built graph is passed in (the approved-plan bridge),
    # the Director still establishes the system map and the objective the
    # work is scoped to — that is what the reviewer, the tester and the
    # completion gate need to verify anything at all.
    scope = None
    game_plan = None
    if mode != "design":
        directed = _direct(goal, state, llm_call, llm_reachable, bus=bus)
        if directed:
            scope, game_plan = directed

    if graph is not None:
        # Pre-built graph (e.g. the /api/plan/<id>/autonomous bridge): an MC
        # plan already validated against the live registry. It IS the
        # TaskGraph; skip re-planning. No synthetic model plan is attached.
        plan = None
        _attach_scope(graph, scope)
    else:
        design = getattr(state, "design", None) if state is not None else None
        locked = state.locked_decisions() if state is not None else None
        graph, plan = _build_plan(goal, reg, llm_call, llm_reachable,
                                  design=design, locked=locked, scope=scope)

    if bus is not None:
        steps = (plan or {}).get("steps", []) if isinstance(plan, dict) else []
        bus({
            "type": "agent.plan_ready",
            "goal": goal,
            "title": (plan or {}).get("title") if isinstance(plan, dict) else None,
            "rationale": (plan or {}).get("rationale") if isinstance(plan, dict) else None,
            "steps": steps,
            "model_driven": plan is not None,
            "stage_count": len(graph.all()),
        })

    if mode == "plan":
        return {
            "mode": "plan",
            "goal": goal,
            "model_driven": plan is not None,
            "plan": plan,
            "stages": [t.stage for t in graph.all()],
            "stage_count": len(graph.all()),
        }

    # ---- BUILD ----
    # llm is attached so planning goes through the canonical model-driven
    # path (skeleton fallback) and failed tool calls get genuine LLM repair.
    # A pre-built graph (MC plan bridge) is still honored and runs as-is.
    # max_critique_cycles=2: build -> critique -> improve -> RE-CRITIQUE.
    # The improve round deserves a verification pass; the loop stays
    # bounded inside the agent.
    agent = AutonomousAgent(reg, bus=bus, policy=pol, llm=llm_call,
                            max_critique_cycles=2)
    report = agent.run(goal, graph=graph, state=state, scope=scope,
                       game_plan=game_plan)

    verdict = None
    # The quality judge scores "is this a good game" — that question only
    # makes sense for runs that actually produced work. A BLOCKED run
    # (e.g. nothing connected) or a total failure must not get a numeric
    # verdict that could read as "passed".
    from agent.events import STATUS_COMPLETED, STATUS_PARTIAL
    if (llm_call is not None and llm_reachable
            and report.status in (STATUS_COMPLETED, STATUS_PARTIAL)):
        verdict = judge(goal, report, plan, llm_call, reg)
        if bus is not None:
            bus({"type": "agent.judged", "goal": goal, "verdict": verdict})

        # One improvement round: if the verdict fails, ask the model to revise
        # the plan with the suggestions and rebuild. Bounded to avoid loops.
        if (not verdict.get("pass")) and judge_iterations > 0:
            for _round in range(judge_iterations):
                new_graph, new_plan = _build_plan(
                    goal, reg, llm_call, True,
                    feedback=verdict.get("suggestions"), scope=scope)
                if new_plan is None or not new_graph.all():
                    break
                bus({
                    "type": "agent.plan_ready",
                    "goal": goal,
                    "title": (new_plan or {}).get("title"),
                    "steps": (new_plan or {}).get("steps", []),
                    "model_driven": True,
                    "stage_count": len(new_graph.all()),
                    "revised": True,
                })
                report = agent.run(goal, graph=new_graph, state=state,
                                   scope=scope, game_plan=game_plan)
                verdict = judge(goal, report, new_plan, llm_call, reg)
                plan = new_plan
                if bus is not None:
                    bus({"type": "agent.judged", "goal": goal,
                         "verdict": verdict, "revised": True})
                if verdict.get("pass"):
                    break

    return {
        "mode": "build",
        "goal": goal,
        "model_driven": plan is not None,
        "report": report.to_dict(),
        "verdict": verdict,
    }
