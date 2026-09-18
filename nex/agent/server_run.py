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

from typing import Any, Callable, Dict, List, Optional


def _registry() -> Any:
    from tunnels import get_tunnels
    from agent.registry import CapabilityRegistry
    # Use the live Upstream objects (not status-dict summaries) so the
    # registry can discover tools/resources/prompts directly.
    return CapabilityRegistry.from_upstreams(get_tunnels().raw_upstreams())


def agent_capabilities() -> Dict[str, Any]:
    """Compact capability summary of every live MCP server."""
    try:
        reg = _registry()
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc), "servers": []}
    return reg.compact_summary()


def _build_plan(goal: str, reg, llm_call, reachable: bool,
                feedback: Optional[List[str]] = None):
    """Return (TaskGraph, plan_dict|None). Uses the model when reachable."""
    from agent.planner import plan as default_planner
    from agent.model_planner import model_driven_planner
    if llm_call is not None and reachable:
        return model_driven_planner(goal, reg, llm_call, feedback=feedback)
    return default_planner(goal, reg), None


def run_agent_goal(goal: str,
                   bus: Optional[Callable] = None,
                   policy=None,
                   llm_call: Optional[Callable] = None,
                   llm_reachable: bool = False,
                   mode: str = "build",
                   judge_iterations: int = 1,
                   graph: Optional[Any] = None) -> Any:
    """Plan (and optionally build + judge) a goal against the live registry.

    `mode`:
      * "plan"  — produce the plan + a graph, emit a plan event, do NOT execute.
      * "build" — execute and (if a model is available) run quality judges,
                  optionally rebuilding once on a failing verdict.

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
    reg = _registry()
    pol = current_policy()

    if graph is not None:
        # Pre-built graph (e.g. the /api/plan/<id>/autonomous bridge): an MC
        # plan already validated against the live registry. It IS the
        # TaskGraph; skip re-planning. No synthetic model plan is attached.
        plan = None
    else:
        graph, plan = _build_plan(goal, reg, llm_call, llm_reachable)

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
    agent = AutonomousAgent(reg, bus=bus, policy=pol, llm=llm_call)
    report = agent.run(goal, graph=graph)

    verdict = None
    if llm_call is not None and llm_reachable:
        verdict = judge(goal, report, plan, llm_call, reg)
        if bus is not None:
            bus({"type": "agent.judged", "goal": goal, "verdict": verdict})

        # One improvement round: if the verdict fails, ask the model to revise
        # the plan with the suggestions and rebuild. Bounded to avoid loops.
        if (not verdict.get("pass")) and judge_iterations > 0:
            for _round in range(judge_iterations):
                new_graph, new_plan = _build_plan(
                    goal, reg, llm_call, True,
                    feedback=verdict.get("suggestions"))
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
                report = agent.run(goal, graph=new_graph)
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
