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


# THE CAPABILITY BOUNDARY, stated to the planner verbatim.
PLAN_BOUNDARY = (
    "HARD CAPABILITY BOUNDARY: you can act ONLY through tools exposed "
    "by explicitly connected MCP servers. You have NO filesystem, shell, "
    "operating-system, or arbitrary network access — those tools do not "
    "exist for you. If a goal truly requires something outside this "
    "boundary, say so plainly in the plan's 'assumptions' and stop; "
    "never invent or simulate such a step."
)


def _catalog(registry, goal: str = "", max_catalog: int = 80) -> str:
    """Capability-filtered tool catalog for the planning prompt.

    With huge MCP catalogs the full dump is context poison, so tools are
    ranked by lexical relevance to the goal and the top max_catalog are
    listed, with an honest "(+N more available)" trailer. The model is
    told it may name ANY tool — validation runs against the FULL registry.
    """
    tools, omitted = registry.relevant_tools(goal, limit=max_catalog)
    lines: List[str] = []
    for t in tools:
        cat = (t.capability.category if t.capability else "unknown")
        lines.append("- %s (server=%s, category=%s): %s" % (
            t.full_name, t.server, cat, (t.description or "")[:140]))
    if omitted > 0:
        lines.append("(+%d more tools available on connected servers — you "
                     "may reference any of them by name; use the most "
                     "fitting one)" % omitted)
    return "\n".join(lines)


def _stage_for(tv) -> str:
    return (tv.capability.category if tv.capability else "unknown").lower()


def _memory_block(memory: Dict[str, Any]) -> str:
    """Project-memory section of the planning prompt.

    Four parts, all BOUNDED (project_state caps them) — the model gets an
    orientation, never a chat log:
      * PROJECT SYSTEMS — what exists, what works, what we are building
        now. This is what lets a small model stop re-deriving the whole
        project every round.
      * LEARNED — durable facts about this project/engine.
      * CONFIRMED DEFECTS — observation-verified bugs still open. A plan
        that re-creates them is a plan defect.
      * OBSERVE RULE — after runtime-affecting work, the plan must prove
        it by observing the running game (build -> run -> observe).
    """
    parts: List[str] = []
    systems = memory.get("systems") or {}
    if systems:
        rows = []
        for name, info in list(systems.items())[:12]:
            if not isinstance(info, dict):
                continue
            rows.append("- %s: %s%s"
                        % (name, info.get("status", "planned"),
                           (" (" + str(info.get("notes"))[:80] + ")")
                           if info.get("notes") else ""))
        if rows:
            parts.append("PROJECT SYSTEMS (what exists; do NOT rebuild "
                         "completed ones):\n" + "\n".join(rows))
    knowledge = memory.get("knowledge") or {}
    if knowledge:
        parts.append("LEARNED ABOUT THIS PROJECT (facts, keep using "
                     "them):\n" + "\n".join(
                         "- %s: %s" % (k, v) for k, v in
                         list(knowledge.items())[:8]))
    bugs = [b for b in (memory.get("known_bugs") or [])
            if isinstance(b, dict) and b.get("message")]
    if bugs:
        parts.append("CONFIRMED DEFECTS, STILL OPEN (repair these FIRST — "
                     "a plan that re-creates them is a plan defect):\n"
                     + "\n".join("- [%s] %s"
                                 % (b.get("kind", "defect"),
                                    b.get("message", "")) for b in bugs[:8]))
    from agent.observations import summarize
    parts.append("Latest runtime observations: %s"
                 % summarize(memory.get("observations") or []))
    parts.append(
        "OBSERVE RULE: after any step that changes RUNTIME behavior "
        "(building, or changing levels/actors/scripts/materials), include "
        "the observation steps that PROVE the change — launch the game, "
        "then capture screenshot / logs / scene-state from the catalog "
        "(depends_on the build step). A runtime-affecting step without an "
        "observation step after it is a plan defect.")
    return "\n\n" + "\n\n".join(parts)


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


def validate_plan_deep(plan: Dict[str, Any],
                       registry) -> Tuple[List[str], List[str]]:
    """Canonical live-registry validation for ANY plan (model-made or MC-made).

    Checks, per step:
      * the tool actually exists on a CONNECTED MCP server (registry lookup,
        bare name or server.name),
      * the capability is not policy-denied (mcp.policy.ALWAYS_DENIED),
      * required args from the live inputSchema are present,
      * no unknown top-level args (warn — schemas are sometimes loose).

    Returns (errors, warnings). Empty errors == the plan is executable
    against the current registry.
    """
    from mcp.policy import authorize

    errors: List[str] = []
    warnings: List[str] = []
    for i, s in enumerate(plan.get("steps", [])):
        tool = s.get("tool", "")
        label = s.get("name") or tool or ("step_%d" % i)
        tv = registry.by_name(tool)
        if tv is None and "." in tool:
            tv = registry.by_name(tool.split(".", 1)[-1])
        if tv is None:
            errors.append("step '%s': tool '%s' not found on any connected "
                          "MCP server" % (label, tool))
            continue
        if not authorize(tv.server, tv.name, tv.capability).allowed:
            errors.append("step '%s': tool '%s' is denied by policy"
                          % (label, tv.name))
            continue
        schema = tv.schema if isinstance(tv.schema, dict) else {}
        props = schema.get("properties", {}) or {}
        required = schema.get("required", []) or []
        args = s.get("args", {}) or {}
        if not isinstance(args, dict):
            errors.append("step '%s': args must be an object" % label)
            continue
        for req in required:
            if req not in args:
                errors.append("step '%s': missing required arg '%s' "
                              "(required by live schema of '%s')"
                              % (label, req, tv.name))
        for key in args:
            if props and key not in props:
                warnings.append("step '%s': arg '%s' not in live schema of "
                                "'%s' (server may ignore it)"
                                % (label, key, tv.name))
    return errors, warnings


def plan_to_graph(plan: Dict[str, Any], registry,
                  scope: Optional[Dict[str, Any]] = None) -> TaskGraph:
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
            expect=s.get("expect"),
            system=(str(s.get("system") or "").strip().lower()
                    or (scope or {}).get("system") or ""),
            criteria=([str(c)[:200] for c in (s.get("criteria") or [])
                       if str(c).strip()][:8]
                      or list((scope or {}).get("success") or [])[:4]),
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
                          feedback: Optional[List[str]] = None,
                          design: Optional[Dict[str, Any]] = None,
                          locked: Optional[List[str]] = None,
                          memory: Optional[Dict[str, Any]] = None,
                          scope: Optional[Dict[str, Any]] = None
                          ) -> Tuple[TaskGraph, Optional[Dict[str, Any]]]:
    """Ask the LLM for a goal-specific plan, parse + validate it, and return a
    TaskGraph. Returns (graph, plan_dict). On any failure returns the fallback
    skeleton graph and None.

    `llm` is a callable ``llm(messages: List[dict]) -> str`` (injected by the
    server so this module stays model-server agnostic).

    `feedback` (optional list of strings) is incorporated into the prompt for an
    improvement round — e.g. judge suggestions from a previous attempt.

    `memory` (optional dict) is the project memory: {known_bugs: [...],
    observations: [...]} — confirmed defects and the latest runtime evidence,
    so an improvement round REPAIRS instead of re-creating known problems.
    `systems`/`knowledge` (same dict) give the model its orientation: what
    exists, what works, what it learned — bounded, upserted state.

    `scope` (optional dict, agent.director.scope_envelope) is the SINGLE
    objective of this planning round. When present the model is told
    exactly which system it is building, what proves success, and what it
    must NOT touch — the anti-"I improved the entire project" guard.
    """
    fallback = fallback or default_planner
    if llm is None:
        return fallback(goal, registry), None

    catalog = _catalog(registry, goal=goal, max_catalog=max_catalog)
    system = PLAN_SYSTEM + "\n\n" + PLAN_BOUNDARY
    if design:
        from agent.design import design_summary
        system += ("\n\nAPPROVED DESIGN DOCUMENT (implement THIS — do not "
                   "restart the project, do not contradict it):\n"
                   + design_summary(design))
    if locked:
        system += ("\n\nLOCKED DESIGN DECISIONS (binding; any step that "
                   "re-litigates them will be blocked):\n"
                   + "\n".join("- " + d for d in locked[:8]))
    if memory:
        system += _memory_block(memory)
    if scope:
        from agent.director import scope_block
        system += ("\n\n" + scope_block(scope) +
                   "\n\nEvery step MUST carry \"system\": \"%s\" and, when "
                   "it proves one, \"criteria\": [\"<success criterion>\"]."
                   % (scope.get("system") or ""))
    user_msg = "Goal: %s\n\nProduce the plan JSON now." % goal
    if feedback:
        user_msg += ("\n\nAddress these concrete findings before "
                     "re-planning:\n- " + "\n- ".join(feedback[:5]))
    messages = [
        {"role": "system", "content": system
            + "\n\nLIVE TOOL CATALOG:\n" + catalog},
        {"role": "user", "content": user_msg},
    ]
    try:
        from agent.plans import extract_plan
        reply = llm(messages)
        plan = extract_plan(reply)
    except Exception:  # noqa: BLE001
        return fallback(goal, registry), None

    if not isinstance(plan, dict) or not plan.get("steps"):
        return fallback(goal, registry), None

    valid, missing = validate_plan_tools(plan, registry)
    if not valid:
        return fallback(goal, registry), None

    graph = plan_to_graph(plan, registry, scope=scope)
    if not graph.all():
        return fallback(goal, registry), None

    # Stash provenance on the graph for reporting.
    graph._model_plan = plan
    graph._model_plan_missing = missing
    return graph, plan
