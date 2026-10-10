"""Deterministic project dependency graph — identifiers, not guesses.

Nex has no schema-level access to the Unreal project through MCP: no
tool here returns "this Blueprint references these three assets". What
IS real ground truth is the exact arguments of every successful tool
call this run made — the map/actor/Blueprint/asset names it actually
operated on. This module extracts identifier-SHAPED values from those
arguments deterministically (never from free prose, never guessed by a
model), tags each with the gameplay system agent/regression.py would
classify its tool call into, and records which identifiers were named
TOGETHER in the same successful call as a co-occurrence edge — the
cheapest real "these two are related" signal available without a true
engine reflection API.

This is bounded and cross-run mergeable, the same shape as
agent/memory.py and agent/regression.py's cross-run ledger, with
persistence left entirely to store.py/server.py (agent/ must not import
store — tests/test_architecture.py enforces this). It never claims
asset-level reference information the project has not actually
disclosed through a real tool call.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple

from agent.regression import classify_system

MAX_NODES = 300
MAX_EDGES_PER_NODE = 20
MAX_TOOLS_PER_NODE = 5

# An argument key suggesting its value NAMES something (an asset, actor,
# Blueprint, map, ...) rather than carrying a number/flag/free-text
# description. Bounded, concrete substrings — same matching discipline
# as agent/regression.py's _SYSTEM_KEYWORDS.
_IDENTIFIER_KEY_HINTS = (
    "name", "path", "class", "actor", "blueprint", "asset", "map", "level",
    "component", "material", "widget", "mesh", "sound", "audio", "vfx",
    "niagara", "texture", "target", "object", "id",
)

_VALID_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_/.\-]+$")
_ENGINE_PATH_RE = re.compile(r"^/(Game|Engine)/")


def _looks_like_identifier(value: str) -> bool:
    v = value.strip()
    if not (1 <= len(v) <= 80):
        return False
    if " " in v or "\n" in v:
        return False
    if not _VALID_IDENTIFIER_RE.match(v):
        return False
    if v.isdigit():
        return False
    return True


def extract_identifiers(args: Any) -> List[str]:
    """Identifier-shaped values out of one tool call's arguments.

    Two independent signals, either is sufficient: the value itself
    looks like an Unreal content path (/Game/... or /Engine/...), or the
    argument's own KEY hints it names something and the value has
    identifier shape (no spaces, no free prose).
    """
    out: List[str] = []
    if not isinstance(args, dict):
        return out
    for key, value in args.items():
        if not isinstance(value, str):
            continue
        if _ENGINE_PATH_RE.match(value.strip()):
            out.append(value.strip())
            continue
        key_l = str(key).lower()
        if any(hint in key_l for hint in _IDENTIFIER_KEY_HINTS) and \
                _looks_like_identifier(value):
            out.append(value.strip())
    # Stable de-dup, order preserved.
    seen = set()
    unique: List[str] = []
    for ident in out:
        if ident not in seen:
            seen.add(ident)
            unique.append(ident)
    return unique


def empty_graph() -> Dict[str, Any]:
    return {"nodes": {}, "edges": {}}


def _bump_edge(edges: Dict[str, Dict[str, int]], a: str, b: str) -> None:
    adj = edges.setdefault(a, {})
    adj[b] = adj.get(b, 0) + 1
    if len(adj) > MAX_EDGES_PER_NODE:
        weakest = min(adj, key=lambda k: adj[k])
        if weakest != b:
            adj.pop(weakest, None)


def _prune(graph: Dict[str, Any]) -> None:
    nodes = graph["nodes"]
    if len(nodes) <= MAX_NODES:
        return
    ordered = sorted(nodes.items(),
                     key=lambda kv: kv[1].get("last_seen_run", 0),
                     reverse=True)[:MAX_NODES]
    keep = {k for k, _ in ordered}
    graph["nodes"] = dict(ordered)
    graph["edges"] = {k: {o: c for o, c in v.items() if o in keep}
                      for k, v in graph["edges"].items() if k in keep}


def merge_project_graph(previous: Any, tasks: Iterable[Any],
                        run_no: int) -> Dict[str, Any]:
    """Fold this run's successful tool calls into the persisted graph."""
    prev = previous or {}
    graph: Dict[str, Any] = {
        "nodes": {k: dict(v) for k, v in (prev.get("nodes") or {}).items()},
        "edges": {k: dict(v) for k, v in (prev.get("edges") or {}).items()},
    }
    for t in tasks:
        if getattr(t, "status", None) != "success":
            continue
        idents = extract_identifiers(getattr(t, "args", None))
        if not idents:
            continue
        tool_name = str(getattr(t, "tool", "") or "")
        systems = sorted(classify_system(tool_name))
        for ident in idents:
            node = dict(graph["nodes"].get(ident) or {
                "systems": [], "first_seen_run": run_no, "mentions": 0,
                "tools": [],
            })
            node["last_seen_run"] = run_no
            node["mentions"] = int(node.get("mentions", 0)) + 1
            for sys_id in systems:
                if sys_id not in node["systems"]:
                    node["systems"].append(sys_id)
            if tool_name and tool_name not in node["tools"]:
                node["tools"] = (list(node["tools"]) + [tool_name])[
                    -MAX_TOOLS_PER_NODE:]
            graph["nodes"][ident] = node
        for i, a in enumerate(idents):
            for b in idents[i + 1:]:
                _bump_edge(graph["edges"], a, b)
                _bump_edge(graph["edges"], b, a)
    _prune(graph)
    return graph


def related(graph: Any, identifier: str, limit: int = 10
           ) -> List[Tuple[str, int]]:
    """Identifiers most often named alongside `identifier`, strongest first."""
    adj = ((graph or {}).get("edges") or {}).get(identifier) or {}
    return sorted(adj.items(), key=lambda kv: kv[1], reverse=True)[:limit]

def dependents_of(graph: Any, identifier: str, limit: int = 10) -> List[str]:
    """Plain identifier list version of related() for simple call sites."""
    return [ident for ident, _ in related(graph, identifier, limit)]


def to_public(graph: Any, max_nodes: int = 50) -> Dict[str, Any]:
    """Bounded, report-facing view: the most recently active identifiers."""
    nodes = (graph or {}).get("nodes") or {}
    ordered = sorted(nodes.items(),
                     key=lambda kv: kv[1].get("last_seen_run", 0),
                     reverse=True)[:max_nodes]
    return {
        "node_count": len(nodes),
        "nodes": {k: v for k, v in ordered},
        "note": ("Deterministic identifier co-occurrence extracted from "
                "this project's own successful tool-call arguments — not "
                "a true engine reference graph. No current MCP capability "
                "tells Nex what a Blueprint or material actually "
                "references; an identifier never named together with "
                "another one may still be related, and one shown here as "
                "related may only ever have appeared in the same call by "
                "coincidence."),
    }
