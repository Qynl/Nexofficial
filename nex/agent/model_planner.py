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
  * `depends_on` becomes real DAG edges; dangling names fail closed.
  * Required-args are checked against the live inputSchema.
  * If the model is unreachable or returns nothing usable, the caller
    falls back to the deterministic planner (agent/planner.py).

Argument references: any nested argument may be the string "$step-name" (a
dependency's whole result) or "$step-name.key" (a field of it). They are
resolved at execution time by agent/loop.py and must point at an explicitly
declared dependency.
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
            spec = props.get(k, {})
            typ = spec.get("type", "?") if isinstance(spec, dict) else "?"
            mark = "*" if k in req else ""
            schema_bits.append("%s:%s%s" %
                               (str(k)[:64], str(typ)[:24], mark))
        line = "- %s (category=%s)" % (t.full_name, cat)
        if schema_bits:
            line += " args: " + ", ".join(schema_bits)
        if req:
            line += "  (* = required)"
        output = t.output_schema if isinstance(t.output_schema, dict) else {}
        output_props = output.get("properties", {}) or {}
        if isinstance(output_props, dict) and output_props:
            returns = []
            for k in list(output_props.keys())[:8]:
                spec = output_props.get(k, {})
                typ = spec.get("type", "?") if isinstance(spec, dict) else "?"
                returns.append("%s:%s" %
                               (str(k)[:64], str(typ)[:24]))
            line += " returns: " + ", ".join(returns)
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
        server = str(s.get("server") or "").strip()
        tv = _lookup(registry, tool, server=server or None)
        if tv is None:
            missing.append(tool)
        else:
            valid.append(tool)
    return valid, missing


def _lookup(registry, tool: str, server: str = None):
    """Resolve a tool without ever discarding a claimed namespace.

    Falling back from ``evil.echo`` to the first bare ``echo`` silently
    changed the server the model named. A qualified name is now exact; an
    explicit ``server`` field must agree with its prefix.
    """
    if not isinstance(tool, str) or not tool:
        return None
    if server:
        sv = registry.server(server)
        if sv is None:
            return None
        if "." in tool:
            prefix, bare = tool.split(".", 1)
            if prefix != server:
                return None
            return sv.by_name(bare)
        return sv.by_name(tool)
    return registry.by_name(tool)


def _argument_references(value: Any, path: str = "$", depth: int = 0
                         ) -> List[Tuple[str, str]]:
    """Bounded recursive ``(path, reference)`` pairs from JSON arguments."""
    if depth > 40:
        return []
    if isinstance(value, str) and value.startswith("$"):
        return [(path, value)]
    out: List[Tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in list(value.items())[:2048]:
            out.extend(_argument_references(
                child, "%s.%s" % (path, str(key)[:80]), depth + 1))
    elif isinstance(value, list):
        for i, child in enumerate(value[:4096]):
            out.extend(_argument_references(
                child, "%s[%d]" % (path, i), depth + 1))
    return out[:10000]


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
        server = str(s.get("server") or "").strip()
        tv = _lookup(registry, tool, server=server or None)
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
        for key in args:
            if props and key not in props:
                warnings.append("step '%s': arg '%s' not in live schema of "
                                "'%s' (server may ignore it)"
                                % (label, key, tv.name))
        deps = set(s.get("depends_on") or [])
        for path, ref in _argument_references(args):
            m = _REF_RE.match(ref)
            if not m:
                warnings.append("step '%s': arg '%s' reference %r does "
                                "not look like $step or $step.key"
                                % (label, path, ref[:40]))
            elif m.group(1) not in names:
                errors.append("step '%s': arg '%s' references unknown "
                              "step '%s'" % (label, path, m.group(1)))
            elif m.group(1) not in deps:
                errors.append("step '%s': arg '%s' references step '%s' "
                              "without declaring it in depends_on"
                              % (label, path, m.group(1)))
    return errors, warnings


def plan_to_graph(plan: Dict[str, Any], registry,
                  external_refs: Optional[Dict[str, str]] = None,
                  id_prefix: str = "") -> TaskGraph:
    """Convert a model plan into a validated, executable DAG.

    Missing tools, duplicate names, dangling dependencies and dependency
    cycles fail closed; dependency names are never silently discarded.
    ``external_refs`` maps successful historical slugs to task ids during a
    replan, allowing corrective work to consume prior outputs without rerunning
    the producing tool.
    """
    g = TaskGraph()
    external_refs = dict(external_refs or {})
    steps = plan.get("steps", []) or []
    candidates = []
    dropped: List[str] = []
    names = set()

    for i, s in enumerate(steps[:200]):
        if not isinstance(s, dict):
            dropped.append("step_%d: step must be an object" % i)
            continue
        name = str(s.get("name") or "s%d" % i)
        if name in names or name in external_refs:
            dropped.append("%s: duplicate current/historical step name" % name)
            continue
        names.add(name)
        if not isinstance(s.get("args", {}) or {}, dict):
            dropped.append("%s: args must be an object" % name)
            continue
        raw_tool = str(s.get("tool") or "")
        step_server = str(s.get("server") or "").strip()
        tv = _lookup(registry, raw_tool, server=step_server or None)
        if tv is None:
            dropped.append("%s: tool %r does not exist on the named server"
                           % (name, raw_tool))
            continue
        candidates.append((i, name, s, tv))

    # A dependency on a dropped/nonexistent step invalidates its dependent;
    # repeat because that invalidation may cascade.
    changed = True
    while changed:
        changed = False
        available = ({name for _, name, _, _ in candidates}
                     | set(external_refs))
        kept = []
        for item in candidates:
            _, name, s, _ = item
            deps = s.get("depends_on") or []
            invalid_dep = (not isinstance(deps, list) or any(
                not isinstance(d, str) or d not in available for d in deps))
            invalid_ref = False
            for _path, ref in _argument_references(s.get("args", {}) or {}):
                match = _REF_RE.match(ref)
                if (match is None or match.group(1) not in available or
                        match.group(1) not in deps):
                    invalid_ref = True
                    break
            if invalid_dep or invalid_ref:
                dropped.append("%s: dependency/reference is missing or invalid"
                               % name)
                changed = True
            else:
                kept.append(item)
        candidates = kept

    deps_by_name = {
        name: list(s.get("depends_on") or []) for _, name, s, _ in candidates
    }
    visiting, visited = set(), set()

    def cyclic(name: str) -> bool:
        if name in visiting:
            return True
        if name in visited:
            return False
        visiting.add(name)
        if any(cyclic(dep) for dep in deps_by_name.get(name, [])):
            return True
        visiting.remove(name)
        visited.add(name)
        return False

    if any(cyclic(name) for name in list(deps_by_name)):
        empty = TaskGraph()
        empty.plan_meta = {
            "dropped": dropped + ["plan contains a dependency cycle"],
            "title": str(plan.get("title") or ""),
            "rationale": str(plan.get("rationale") or ""),
            "requested": len(steps), "kept": 0,
        }
        return empty

    name_to_id = dict(external_refs)
    name_to_id.update({name: "%ss%d" % (id_prefix, i)
                       for i, name, _, _ in candidates})
    for i, name, s, tv in candidates:
        raw_args = s.get("args", {}) or {}
        if not isinstance(raw_args, dict):
            dropped.append("%s: args must be an object" % name)
            continue
        g.add(Task(
            id=name_to_id[name],
            name=str(s.get("title") or name),
            slug=name,
            server=tv.server,
            tool=tv.name,
            args=dict(raw_args),
            deps=[name_to_id[d] for d in (s.get("depends_on") or [])],
            expect=s.get("expect"),
            why=str(s.get("why") or ""),
        ))

    g.plan_meta = {
        "dropped": dropped,
        "title": str(plan.get("title") or ""),
        "rationale": str(plan.get("rationale") or ""),
        "requested": len(steps),
        "kept": len(g.all()),
    }
    return g


def model_driven_planner(goal: str, registry,
                         llm: Optional[Callable] = None,
                         max_catalog: int = 80,
                         note: str = "",
                         feedback: Optional[List[str]] = None,
                         quality_brief: str = "",
                         completed_refs: Optional[Dict[str, str]] = None,
                         id_prefix: str = ""
                         ) -> Tuple[TaskGraph, Optional[Dict[str, Any]]]:
    """Ask the LLM for a plan; validate it; return (graph, plan_dict).

    If no plan document can be parsed, returns an empty graph and ``None`` so
    the caller may use its deterministic fallback. A parsed but invalid plan
    returns its empty/partial graph plus the plan, preserving validation
    findings rather than silently substituting unrelated work.

    `note` is guidance for a RE-PLAN (what the previous attempt taught).
    `feedback` is a list of concrete findings to address.
    `quality_brief` is a deterministic, live-catalog production contract for
    game-authoring runs; empty for ordinary goals. `completed_refs` exposes
    successful historical step slugs as dependencies/references during a
    replan so outputs can be reused without repeating effects. `id_prefix`
    keeps newly planned task ids distinct from retained history.
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
    if quality_brief:
        user_msg += "\n\n" + quality_brief
    if completed_refs:
        user_msg += (
            "\n\nSuccessful historical steps available to this replan:\n- " +
            "\n- ".join("%s (reference its result as $%s or $%s.field; "
                         "it may appear in depends_on and MUST NOT be rerun)"
                         % (name, name, name)
                         for name in list(completed_refs)[:24]))
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
        graph = TaskGraph()
        graph.plan_meta = {
            "dropped": ["all proposed tools were unavailable"],
            "missing_tools": missing,
            "title": str(plan.get("title") or ""),
            "rationale": str(plan.get("rationale") or ""),
            "requested": len(plan.get("steps") or []), "kept": 0,
        }
        return graph, plan
    graph = plan_to_graph(plan, registry,
                          external_refs=completed_refs,
                          id_prefix=id_prefix)
    graph.plan_meta["missing_tools"] = missing
    return graph, plan
