"""Model-driven planning — goal + live tool catalog → validated TaskGraph.

Pipeline:

  goal + live tool catalog
        |
        v  (LLM, injected — no hard dependency on any provider)
  plan JSON (agent/prompts.py PLANNER_SYSTEM shape)
        |
        v  (agent/jsonreply.extract_json_with_key)
  plan dict
        |
        v  (plan_to_graph)
  TaskGraph — tools validated against the LIVE registry

Safety properties (so the model cannot degrade the plan):

  * Only tools that actually exist in the registry become steps.
    A hallucinated tool name is dropped and reported as "missing",
    never executed.
  * `depends_on` becomes real DAG edges; dangling names are ignored.
  * Required-args are checked against the live inputSchema.
  * If the model is unreachable or returns nothing usable, the caller
    falls back to the deterministic planner (agent/planner.py).

Argument references: a step arg may be the string "$step-name" (a
dependency's whole result) or "$step-name.key" (a field of it). They
are resolved at execution time by agent/loop.py — validated here only
for shape (they must point at a real step in the plan).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.jsonreply import extract_json_with_key
from agent.prompts import PLANNER_SYSTEM, BOUNDARY
from agent.task_graph import Task, TaskGraph

_REF_RE = re.compile(r"^\$([a-z0-9_-]+)(?:\.([a-z0-9_.-]+))?$", re.IGNORECASE)


def catalog_text(registry, goal: str = "",
                 max_catalog: int = 80) -> str:
    """Capability-filtered tool catalog for the planning prompt.

    With huge MCP catalogs the full dump is context poison, so tools
    are ranked by lexical relevance to the goal and the top
    `max_catalog` are listed, with an honest "(+N more available)"
    trailer. The model is told it may name ANY tool — validation runs
    against the FULL registry.
    """
    tools, omitted = registry.relevant_tools(goal, limit=max_catalog)
    lines: List[str] = []
    for t in tools:
        cat = (t.capability.category if t.capability else "unknown")
        schema_bits = []
        schema = t.schema if isinstance(t.schema, dict) else {}
        props = schema.get("properties", {}) or {}
        req = schema.get("required", []) or []
        for k in list(props.keys())[:8]:
            typ = props.get(k, {}).get("type", "?")
            mark = "*" if k in req else ""
            schema_bits.append("%s:%s%s" % (k, typ, mark))
        line = "- %s (category=%s)" % (t.full_name, cat)
        if schema_bits:
            line += " args: " + ", ".join(schema_bits)
        if req:
            line += "  (* = required)"
        desc = (t.description or "").strip().split("\n")[0]
        if desc:
            line += " — " + desc[:180]
        lines.append(line)
    if omitted > 0:
        lines.append("(+%d more tools available on connected servers — you "
                     "may reference any of them by name)" % omitted)
    return "\n".join(lines)


def validate_plan_tools(plan: Dict[str, Any],
                        registry) -> Tuple[List[str], List[str]]:
    """(valid_tool_names, missing_tool_names) for a parsed plan."""
    valid, missing = [], []
    for s in plan.get("steps", []):
        tool = s.get("tool", "")
        tv = _lookup(registry, tool)
        if tv is None:
            missing.append(tool)
        else:
            valid.append(tool)
    return valid, missing


def _lookup(registry, tool: str, server: str = None):
    """Find a tool by name. With `server`, only that server counts —
    a plan naming a server that does not expose the tool is invalid."""
    if server:
        sv = registry.server(server)
        if sv is not None:
            tv = sv.by_name(tool)
            if tv is not None:
                return tv
            if "." in tool:
                return sv.by_name(tool.split(".", 1)[-1])
        return None
    tv = registry.by_name(tool)
    if tv is None and "." in tool:
        tv = registry.by_name(tool.split(".", 1)[-1])
    return tv


def validate_plan_deep(plan: Dict[str, Any],
                       registry) -> Tuple[List[str], List[str]]:
    """Canonical live-registry validation for any plan.

    Checks, per step:
      * the tool exists on a CONNECTED server,
      * the capability is not policy-denied,
      * required args from the live inputSchema are present,
      * $references point at steps that exist in the plan,
      * no unknown top-level args (warn — schemas are sometimes loose).
    """
    from mcp.policy import authorize

    errors: List[str] = []
    warnings: List[str] = []
    names = {str(s.get("name") or "") for s in plan.get("steps", [])}
    for i, s in enumerate(plan.get("steps", [])):
        tool = s.get("tool", "")
        label = s.get("name") or tool or ("step_%d" % i)
        tv = _lookup(registry, tool)
        if tv is None:
            errors.append("step '%s': tool '%s' not found on any connected "
                          "server" % (label, tool))
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
        for key, val in args.items():
            if props and key not in props:
                warnings.append("step '%s': arg '%s' not in live schema of "
                                "'%s' (server may ignore it)"
                                % (label, key, tv.name))
            if isinstance(val, str) and val.startswith("$"):
                m = _REF_RE.match(val)
                if not m:
                    warnings.append("step '%s': arg '%s' reference %r does "
                                    "not look like $step or $step.key"
                                    % (label, key, val[:40]))
                elif m.group(1) not in names:
                    errors.append("step '%s': arg '%s' references unknown "
                                  "step '%s'" % (label, key, m.group(1)))
    return errors, warnings


def plan_to_graph(plan: Dict[str, Any], registry) -> TaskGraph:
    """Convert a parsed plan into a TaskGraph.

    Steps referencing tools that don't exist in the registry are
    dropped (the caller reports them as missing capability). Dependency
    names are resolved to ids; unresolvable ones are ignored.
    """
    g = TaskGraph()
    name_to_id: Dict[str, str] = {}
    steps = plan.get("steps", []) or []
    kept: List[Dict[str, Any]] = []

    dropped: List[str] = []
    for i, s in enumerate(steps):
        raw_tool = str(s.get("tool") or "")
        step_server = str(s.get("server") or "").strip()
        tv = _lookup(registry, raw_tool, server=step_server or None)
        if tv is None:
            if step_server and _lookup(registry, raw_tool) is not None:
                dropped.append("%s: tool %r is not on server %r"
                               % (s.get("name") or ("step_%d" % i),
                                  raw_tool, step_server))
            else:
                dropped.append("%s: tool %r does not exist"
                               % (s.get("name") or ("step_%d" % i),
                                  raw_tool))
            name_to_id[str(s.get("name") or "step_%d" % i)] = "__missing__"
            continue
        kept.append((i, s, tv))

    for i, s, tv in kept:
        tid = "s%d" % i
        name = str(s.get("name") or tid)
        deps = [name_to_id[d] for d in (s.get("depends_on") or [])
                if name_to_id.get(d)
                and name_to_id[d] not in ("__missing__",)]
        g.add(Task(
            id=tid,
            name=str(s.get("title") or name),
            slug=name,
            server=tv.server,
            tool=tv.name,
            args=dict(s.get("args", {}) or {}),
            deps=deps,
            expect=s.get("expect"),
            why=str(s.get("why") or ""),
        ))
        name_to_id[name] = tid

    g.plan_meta = {
        "dropped": dropped,
        "title": str(plan.get("title") or ""),
        "rationale": str(plan.get("rationale") or ""),
        "requested": len(steps),
        "kept": len(kept),
    }
    return g


def model_driven_planner(goal: str, registry,
                         llm: Optional[Callable] = None,
                         max_catalog: int = 80,
                         note: str = "",
                         feedback: Optional[List[str]] = None
                         ) -> Tuple[TaskGraph, Optional[Dict[str, Any]]]:
    """Ask the LLM for a plan; validate it; return (graph, plan_dict).

    On any failure returns a graph with no tasks and None — the caller
    decides how to report it (the deterministic planner is the caller's
    fallback).

    `note` is guidance for a RE-PLAN (what the previous attempt taught).
    `feedback` is a list of concrete findings to address.
    """
    if llm is None:
        return TaskGraph(), None

    system = PLANNER_SYSTEM + "\n\n" + BOUNDARY
    user_msg = "Goal: %s\n\nProduce the plan JSON now." % goal
    if note:
        user_msg += ("\n\nGuidance from the previous attempt:\n" + note)
    if feedback:
        user_msg += ("\n\nAddress these findings:\n- "
                     + "\n- ".join(str(f) for f in feedback[:6]))
    messages = [
        {"role": "system",
         "content": system + "\n\nLIVE TOOL CATALOG "
         "(server.tool — args — description):\n"
         + catalog_text(registry, goal=goal, max_catalog=max_catalog)},
        {"role": "user", "content": user_msg},
    ]
    try:
        reply = llm(messages)
    except Exception:  # noqa: BLE001
        return TaskGraph(), None
    if not reply or not isinstance(reply, str):
        return TaskGraph(), None

    obj = extract_json_with_key(reply, "plan")
    plan = obj.get("plan") if isinstance(obj, dict) else None
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        # Models sometimes omit the wrapper key and reply with the plan
        # object itself — accept that shape too (validation still applies).
        bare = extract_json_with_key(reply, "steps")
        if isinstance(bare, dict) and isinstance(bare.get("steps"), list):
            plan = bare
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return TaskGraph(), None
    if not plan.get("steps"):
        # An explicitly empty plan is a valid answer: "not doable".
        g = TaskGraph()
        g.plan_meta = {"title": str(plan.get("title") or ""),
                       "rationale": str(plan.get("rationale") or ""),
                       "requested": 0, "kept": 0,
                       "empty_reason": str(plan.get("rationale") or "")}
        return g, plan

    valid, missing = validate_plan_tools(plan, registry)
    if not valid:
        return TaskGraph(), None
    graph = plan_to_graph(plan, registry)
    if not graph.all():
        return TaskGraph(), None
    graph.plan_meta["missing_tools"] = missing
    return graph, plan
