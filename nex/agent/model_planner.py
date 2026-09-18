"""Model-driven goal planning (STAGE 19 / STAGE 24).

Replaces the fixed capability skeleton with a *goal-specific* plan generated
by the LLM, while keeping the skeleton as a safe fallback. This is what lets
Nex "build the actual game the user asked for" instead of always running the
same generic pipeline.

Pipeline:

  goal + live tool catalog
        |
        v  (LLM, injected — no hard dependency on any model server)
  plan JSON (mc plan schema: title / rationale / steps[...])
        |
        v  (mc.extract_plan)
  plan dict
        |
        v  (plan_to_graph)
  TaskGraph (tools validated against the live registry)

Key safety properties (so the model can't degrade quality):

  * Only tools that actually exist in the registry are turned into tasks.
    A model-hallucinated tool name is dropped and reported as "missing",
    never executed.
  * The LLM plan's `depends_on` becomes real DAG edges.
  * If the model is unreachable, returns garbage, or produces zero valid
    steps, we silently fall back to the capability-driven skeleton.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.planner import plan as default_planner
from agent.task_graph import Task, TaskGraph
from mcp.capability import ToolCapability


# Prompt the LLM to produce a goal-specific, AAA-minded plan. We deliberately
# steer toward *fun* and *quality*, not just "playable": a core loop, clear
# player feedback, a moment of delight, and polish where tools allow.
PLAN_SYSTEM = (
    "You are NEX, an expert game engineer. You will receive a game goal and a "
    "catalog of tools that are LIVE right now (each line: 'tool.name "
    "(server=..., category=...) — description'). Produce a concrete build plan "
    "as JSON ONLY, of this exact shape:\n"
    '{"plan": {"title": str, "rationale": str, "verification": str,'
    ' "steps": [{"name": str, "tool": str, "args": {}, "why": str,'
    ' "expect": str, "depends_on": [step_name, ...]}]}}\n'
    "Rules:\n"
    "1. Use ONLY tools from the catalog, written exactly as 'server.tool'. "
    "Never invent a tool not listed.\n"
    "2. Chain order with depends_on (by step name).\n"
    "3. Aim for a HIGH-QUALITY, FUN result, not just a playable one: include a "
    "clear core game loop, immediate player feedback (audio/visual), at least "
    "one moment of delight, and polish (art/audio/fx) wherever tools allow.\n"
    "4. Keep the plan deterministic and minimal — no redundant steps.\n"
    "5. Output the JSON object and nothing else."
)


def _catalog(registry) -> str:
    lines: List[str] = []
    for t in registry.all_tools():
        cat = (t.capability.category if t.capability else "unknown")
        lines.append("- %s (server=%s, category=%s): %s" % (
            t.full_name, t.server, cat, (t.description or "")[:140]))
    return "\n".join(lines)


def _stage_for(tv) -> str:
    return (tv.capability.category if tv.capability else "unknown").lower()


def validate_plan_tools(plan: Dict[str, Any], registry) -> Tuple[List[str], List[str]]:
    """Return (valid_tool_names, missing_tool_names) for a parsed plan."""
    valid, missing = [], []
    for s in plan.get("steps", []):
        tool = s.get("tool", "")
        tv = registry.by_name(tool)
        if tv is None and "." in tool:
            tv = registry.by_name(tool.split(".", 1)[-1])
        if tv is None:
            missing.append(tool)
        else:
            valid.append(tool)
    return valid, missing


def plan_to_graph(plan: Dict[str, Any], registry) -> TaskGraph:
    """Convert a parsed mc-style plan into a validated TaskGraph.

    Steps referencing tools that don't exist in the registry are skipped
    (and reported via plan metadata by the caller). 'verify_*' steps become
    verifiers attached to the task(s) they depend on.
    """
    g = TaskGraph()
    name_to_id: Dict[str, str] = {}
    order: List[str] = []

    # First pass: create tasks for non-verify steps.
    for i, s in enumerate(plan.get("steps", [])):
        raw_tool = s.get("tool", "")
        tv = registry.by_name(raw_tool)
        if tv is None and "." in raw_tool:
            tv = registry.by_name(raw_tool.split(".", 1)[-1])
        is_verifier = (raw_tool.lower().startswith("verify")
                       or (s.get("name", "").lower().startswith("verify")))
        if tv is None:
            # Tool not available — drop the step but remember its name so
            # downstream depends_on edges don't dangle.
            name_to_id[s.get("name", "step_%d" % i)] = "__missing__"
            continue
        if is_verifier:
            name_to_id[s.get("name", "step_%d" % i)] = "__verify__"
            continue
        tid = "step_%d" % i
        deps = [name_to_id[d] for d in s.get("depends_on", [])
                if name_to_id.get(d) and name_to_id[d] not in ("__missing__", "__verify__")]
        g.add(Task(
            id=tid,
            name=s.get("name", tid),
            stage=_stage_for(tv),
            server=tv.server,
            tool=tv.name,
            args=dict(s.get("args", {})),
            deps=deps,
        ))
        name_to_id[s.get("name", tid)] = tid
        order.append(tid)

    # Second pass: wire verify steps to their dependents.
    for s in plan.get("steps", []):
        raw_tool = s.get("tool", "")
        if not (raw_tool.lower().startswith("verify")
                or s.get("name", "").lower().startswith("verify")):
            continue
        tv = registry.by_name(raw_tool)
        if tv is None and "." in raw_tool:
            tv = registry.by_name(raw_tool.split(".", 1)[-1])
        if tv is None:
            continue
        for dep_name in s.get("depends_on", []):
            dep_id = name_to_id.get(dep_name)
            if dep_id and dep_id not in ("__missing__", "__verify__"):
                t = g.get(dep_id)
                if t:
                    t.verify_tool = tv.name

    return g


def model_driven_planner(goal: str, registry, llm: Optional[Callable] = None,
                          fallback=None, max_catalog: int = 80,
                          feedback: Optional[List[str]] = None
                          ) -> Tuple[TaskGraph, Optional[Dict[str, Any]]]:
    """Ask the LLM for a goal-specific plan, parse + validate it, and return a
    TaskGraph. Returns (graph, plan_dict). On any failure returns the fallback
    skeleton graph and None.

    `llm` is a callable ``llm(messages: List[dict]) -> str`` (injected by the
    server so this module stays model-server agnostic).

    `feedback` (optional list of strings) is incorporated into the prompt for an
    improvement round — e.g. judge suggestions from a previous attempt.
    """
    fallback = fallback or default_planner
    if llm is None:
        return fallback(goal, registry), None

    catalog = _catalog(registry)
    if max_catalog and len(catalog) > max_catalog * 3:  # crude length guard
        catalog = "\n".join(catalog.splitlines()[:max_catalog])
    user_msg = "Goal: %s\n\nProduce the plan JSON now." % goal
    if feedback:
        user_msg += ("\n\nA previous attempt scored low. Address these concrete "
                     "improvements:\n- " + "\n- ".join(feedback[:5]))
    messages = [
        {"role": "system", "content": PLAN_SYSTEM + "\n\nLIVE TOOL CATALOG:\n" + catalog},
        {"role": "user", "content": user_msg},
    ]
    try:
        from mc import extract_plan
        reply = llm(messages)
        plan = extract_plan(reply)
    except Exception:  # noqa: BLE001
        return fallback(goal, registry), None

    if not isinstance(plan, dict) or not plan.get("steps"):
        return fallback(goal, registry), None

    valid, missing = validate_plan_tools(plan, registry)
    if not valid:
        return fallback(goal, registry), None

    graph = plan_to_graph(plan, registry)
    if not graph.all():
        return fallback(goal, registry), None

    # Stash provenance on the graph for reporting.
    graph._model_plan = plan
    graph._model_plan_missing = missing
    return graph, plan
