"""Compensating actions — the honest form of "undo" for MCP mutations.

There is no general undo for an arbitrary remote side effect. MCP has no
transaction, no savepoint, and no rollback verb, and a server that created
a level has not promised it can un-create it. Anything calling itself
"undo" here would be a lie the first time it silently skipped a step.

What IS possible deterministically:

  * every successful mutating call is journalled with the identity the
    server itself returned;
  * for each one, look in the LIVE catalog for the inverse tool on the
    SAME server (create -> delete, add -> remove, spawn -> destroy) using
    verb-pair classification rather than guesswork;
  * resolve WHAT to compensate from the mutation's structured result, not
    from prose;
  * emit a compensation plan in reverse chronological order (LIFO: undo
    the newest first, because later work may depend on earlier work);
  * and state plainly which mutations have NO compensating action, and
    why.

Coverage is always reported. A partial reversal that presents itself as a
clean rollback is worse than no reversal at all, because the operator
stops looking. Nothing here executes: the plan goes back through
manager.call, so every compensating step passes the same trust, policy,
approval, and audit gate as the mutation it reverses.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from mcp.capability import (
    CREATE, DESTRUCTIVE, MODIFY, BUILD, CODE_EXECUTION, NETWORK,
)

# Verb pairs where the inverse is a genuine semantic opposite. Deliberately
# conservative: "update" has no inverse without the previous value, and
# "build"/"publish"/"run" have no inverse at all.
_INVERSE: Dict[str, Tuple[str, ...]] = {
    "create": ("delete", "destroy", "remove"),
    "add": ("remove", "delete", "detach"),
    "spawn": ("destroy", "despawn", "delete", "remove"),
    "insert": ("remove", "delete"),
    "import": ("delete", "remove", "unimport"),
    "attach": ("detach", "remove"),
    "enable": ("disable",),
    "disable": ("enable",),
    "show": ("hide",),
    "hide": ("show",),
    "mount": ("unmount",),
    "link": ("unlink", "detach"),
    "assign": ("unassign", "clear"),
    "register": ("unregister", "deregister"),
    "duplicate": ("delete", "remove"),
    "clone": ("delete", "remove"),
    "new": ("delete", "remove"),
}

# Identifier-ish keys a server may return for the thing it just made.
_IDENTITY_KEYS = (
    "id", "uid", "guid", "uuid", "path", "asset_path", "assetpath",
    "object_path", "name", "asset_id", "actor_id", "instance_id",
    "level_id", "uri", "ref", "handle",
)

# Categories that mutate the world. BUILD/CODE_EXECUTION/NETWORK are
# mutating but not compensable: you cannot un-publish by calling a verb.
_MUTATING = {CREATE, MODIFY, DESTRUCTIVE, BUILD, CODE_EXECUTION, NETWORK}
_COMPENSABLE_CATEGORIES = {CREATE, MODIFY}

_TOK = re.compile(r"[^a-z0-9]+")


def _tokens(name: str) -> List[str]:
    return [t for t in _TOK.split((name or "").lower()) if t]


def _structured(result: Any) -> Any:
    """The schema-backed payload, or JSON parsed out of text content."""
    if not isinstance(result, dict):
        return result
    if isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list):
        texts = [str(c.get("text", "")) for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        joined = "\n".join(t for t in texts if t)
        if joined:
            try:
                return json.loads(joined)
            except (ValueError, TypeError):
                return joined
    return result


def _walk(node: Any, depth: int = 0):
    if depth > 4 or not isinstance(node, dict):
        return
    for key, value in node.items():
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            yield str(key).lower(), value
        elif isinstance(value, dict):
            for item in _walk(value, depth + 1):
                yield item


def identity_from_result(result: Any) -> Optional[Tuple[str, Any]]:
    """The identity the SERVER reported for what it just changed.

    Returns ``(key, value)`` or None. Prose is never mined for an
    identifier: if the server did not return one in a structured field,
    Nex does not know what it would be compensating.
    """
    payload = _structured(result)
    if not isinstance(payload, dict):
        return None
    found = dict(_walk(payload))
    for key in _IDENTITY_KEYS:
        if key in found:
            value = found[key]
            if isinstance(value, str) and not value.strip():
                continue
            return key, value
    return None


def _inverse_candidates(tool: str) -> List[Tuple[str, List[str]]]:
    """(inverse_verb, remaining_noun_tokens) pairs for a tool name."""
    toks = _tokens(tool)
    out: List[Tuple[str, List[str]]] = []
    for i, tok in enumerate(toks):
        for inverse in _INVERSE.get(tok, ()):
            out.append((inverse, toks[:i] + toks[i + 1:]))
    return out


def find_inverse_tool(registry: Any, server: str, tool: str) -> Optional[Any]:
    """An inverse tool on the SAME server, or None.

    Same-server only: another server claiming it can delete this server's
    asset is an assumption, not a contract.
    """
    candidates = _inverse_candidates(tool)
    if not candidates:
        return None
    pool = [tv for tv in getattr(registry, "all_tools", lambda: [])()
            if tv.server == server]
    best = None
    best_score = 0
    for tv in pool:
        cand_toks = _tokens(tv.name)
        for verb, nouns in candidates:
            if verb not in cand_toks:
                continue
            rest = [t for t in cand_toks if t != verb]
            # The noun must match: delete_level reverses create_level,
            # never create_material.
            if not nouns or not rest:
                continue
            overlap = len(set(nouns) & set(rest))
            if overlap != len(set(nouns)) or overlap != len(set(rest)):
                continue
            score = 10 + overlap
            if score > best_score:
                best, best_score = tv, score
    return best


def _compensation_args(inverse_tv: Any, identity: Tuple[str, Any],
                       original_args: Dict[str, Any]
                       ) -> Optional[Dict[str, Any]]:
    """Arguments for the inverse call, or None when we would be guessing."""
    schema = inverse_tv.schema if isinstance(inverse_tv.schema, dict) else {}
    props = schema.get("properties") if isinstance(
        schema.get("properties"), dict) else {}
    required = [k for k in (schema.get("required") or [])
                if isinstance(k, str)]
    key, value = identity
    args: Dict[str, Any] = {}
    # Prefer the exact identity key when the inverse tool accepts it.
    if key in props:
        args[key] = value
    else:
        for cand in required or list(props.keys()):
            if cand.lower() in _IDENTITY_KEYS:
                args[cand] = value
                break
    if not args:
        return None
    # Any other required argument must be reproducible from the original
    # call; inventing one would make the compensation a new action.
    for field in required:
        if field in args:
            continue
        if field in original_args:
            args[field] = original_args[field]
        else:
            return None
    return args


def _reason_for(category: str, has_inverse: bool,
                has_identity: bool) -> str:
    if category == DESTRUCTIVE:
        return ("deletion cannot be compensated — the previous state was "
                "not captured anywhere Nex can read")
    if category == BUILD:
        return "a build/cook artifact has no inverse verb"
    if category == NETWORK:
        return ("a publish/network action is externally visible and cannot "
                "be recalled by a tool call")
    if category == CODE_EXECUTION:
        return ("arbitrary code may have had effects no single inverse "
                "call can reverse")
    if not has_inverse:
        return "no inverse tool is exposed by this server"
    if not has_identity:
        return ("the server returned no structured identifier, so Nex "
                "cannot tell the inverse tool what to act on")
    return "no compensating action available"


def plan_reversal(tasks: Any, registry: Any,
                  limit: int = 50) -> Dict[str, Any]:
    """Build a compensation plan for a run's successful mutations.

    Returns a dict with ``steps`` (LIFO compensating calls), ``blocked``
    (mutations with no compensating action and why), and honest coverage
    counters. Executing the plan is a separate, gated act.
    """
    from agent.task_graph import SUCCESS

    mutations: List[Any] = []
    for task in list(tasks or []):
        if getattr(task, "status", None) != SUCCESS:
            continue
        if not getattr(task, "server", None) or not getattr(task, "tool", None):
            continue
        tv = registry.by_name("%s.%s" % (task.server, task.tool))
        if tv is None:
            continue
        category = getattr(getattr(tv, "capability", None), "category", "")
        if category not in _MUTATING:
            continue
        mutations.append((task, tv, category))

    steps: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    # LIFO: the newest mutation is compensated first, because earlier work
    # may be a dependency of later work.
    for task, tv, category in reversed(mutations[-limit:]):
        label = "%s.%s" % (task.server, task.tool)
        if category not in _COMPENSABLE_CATEGORIES:
            blocked.append({"step": task.name, "tool": label,
                            "category": category,
                            "reason": _reason_for(category, False, False)})
            continue
        inverse = find_inverse_tool(registry, task.server, task.tool)
        identity = identity_from_result(getattr(task, "result", None))
        if inverse is None or identity is None:
            blocked.append({
                "step": task.name, "tool": label, "category": category,
                "reason": _reason_for(category, inverse is not None,
                                      identity is not None)})
            continue
        args = _compensation_args(inverse, identity,
                                  getattr(task, "args", {}) or {})
        if args is None:
            blocked.append({
                "step": task.name, "tool": label, "category": category,
                "reason": ("the inverse tool %s needs arguments the original "
                           "call did not provide" % inverse.full_name)})
            continue
        steps.append({
            "undoes_step": getattr(task, "id", ""),
            "undoes": task.name,
            "undoes_tool": label,
            "server": inverse.server,
            "tool": inverse.name,
            "args": args,
            "identity": {"key": identity[0], "value": identity[1]},
            "contract_fingerprint": getattr(inverse, "contract_fingerprint",
                                            ""),
            "category": getattr(getattr(inverse, "capability", None),
                                "category", ""),
        })

    total = len(mutations)
    return {
        "available": bool(steps),
        "mutations": total,
        "reversible": len(steps),
        "irreversible": len(blocked),
        "coverage_pct": round(100 * len(steps) / total) if total else 0,
        "steps": steps,
        "blocked": blocked,
        "note": ("Compensating actions, not a transaction rollback. Each "
                 "step is a fresh authorized tool call that runs newest "
                 "first and can itself fail or require approval. "
                 "%d of %d mutation(s) have no compensating action."
                 % (len(blocked), total) if total else
                 "This run performed no mutations."),
    }
