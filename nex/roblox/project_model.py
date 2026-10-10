"""The live Roblox DataModel, as far as real evidence actually shows it.

Nex has no tool here that dumps "the whole DataModel tree" in one shot
that would stay valid after the next edit, and no permission to reach
Studio's actual tree outside MCP. What it DOES have is every successful
tool call's own arguments — the service, instance, class, and parent an
action actually named. This module builds a deterministic project model
(services, instances, scripts, remotes, UI, and their parent/child
relationships) from exactly that evidence, the same ground-truth
discipline as agent/project_graph.py, but Roblox-aware: it classifies
each identifier into a concrete DataModel kind (service / script /
remote / bindable / ui / datastore / instance) using Roblox's own
vocabulary instead of a generic co-occurrence graph.

Nothing here is invented: an Instance never mentioned in any successful
call's arguments does not exist in this model, even if it exists in the
live place. Every "relationship" is a parent/child edge this run (or an
earlier one, once merged) actually declared through a real tool call.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_NODES = 400
MAX_CHILDREN_PER_NODE = 40

# Known top-level Roblox services. An identifier matching one of these
# exactly (case-insensitively) is classified as a service outright,
# regardless of what argument key it came from.
SERVICES = {
    "workspace", "replicatedstorage", "serverscriptservice", "serverstorage",
    "startergui", "starterplayer", "starterpack", "lighting", "soundservice",
    "players", "teams", "marketplaceservice", "datastoreservice",
    "tweenservice", "runservice", "userinputservice", "contextactionservice",
    "debris", "chat", "textservice", "httpservice", "collectionservice",
    "pathfindingservice", "physicsservice", "badgeservice", "gamepassservice",
    "messagingservice", "starterplayerscripts", "startercharacterscripts",
}

# Instance ClassName keyword -> project-model kind. Matched against the
# class/type argument value, not the free-text description.
_CLASS_KIND: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("script", ("modulescript", "localscript", "script")),
    ("remote", ("remoteevent", "remotefunction", "unreliableremoteevent")),
    ("bindable", ("bindableevent", "bindablefunction")),
    ("datastore", ("datastore", "ordereddatastore", "memorystore")),
    ("ui", ("screengui", "frame", "textlabel", "textbutton", "imagelabel",
           "imagebutton", "scrollingframe", "uilistlayout", "billboardgui",
           "surfacegui", "viewportframe")),
    ("instance", ("part", "meshpart", "model", "folder", "tool", "humanoid",
                 "animation", "sound", "particleemitter", "attachment",
                 "weld", "motor6d", "union", "wedgepart")),
)

_NAME_KEYS = ("name", "instance_name")
_CLASS_KEYS = ("class_name", "classname", "class", "type", "instance_type")
_PARENT_KEYS = ("parent", "parent_path", "parent_name")

_CONTROL_RE = re.compile(r"[\x00-\x1f]")


def _clean_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    v = _CONTROL_RE.sub("", value).strip()
    return v[:100]


def _first(args: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        if key in args:
            cleaned = _clean_str(args[key])
            if cleaned:
                return cleaned
    return ""


def _classify_kind(name: str, class_name: str) -> str:
    low_name = name.lower()
    if low_name in SERVICES:
        return "service"
    low_class = class_name.lower()
    for kind, hints in _CLASS_KIND:
        if any(h in low_class for h in hints):
            return kind
    if low_class in SERVICES:
        return "service"
    return "unknown"


def empty_model() -> Dict[str, Any]:
    return {"nodes": {}, "children": {}}


def _node_key(name: str, parent: str) -> str:
    return "%s.%s" % (parent, name) if parent else name


def extract_declarations(args: Any) -> List[Dict[str, str]]:
    """Zero or one Instance declaration named in one successful call's
    arguments. A call with no identifiable name declares nothing."""
    if not isinstance(args, dict):
        return []
    name = _first(args, _NAME_KEYS)
    if not name:
        return []
    class_name = _first(args, _CLASS_KEYS)
    parent = _first(args, _PARENT_KEYS)
    return [{"name": name, "class_name": class_name, "parent": parent}]


def merge_project_model(previous: Any, tasks: Iterable[Any],
                        run_no: int) -> Dict[str, Any]:
    """Fold this run's successful calls' Instance declarations into the
    persisted Roblox project model."""
    prev = previous or {}
    model: Dict[str, Any] = {
        "nodes": {k: dict(v) for k, v in (prev.get("nodes") or {}).items()},
        "children": {k: list(v)
                    for k, v in (prev.get("children") or {}).items()},
    }
    for t in tasks:
        if getattr(t, "status", None) != "success":
            continue
        for decl in extract_declarations(getattr(t, "args", None)):
            key = _node_key(decl["name"], decl["parent"])
            kind = _classify_kind(decl["name"], decl["class_name"])
            node = dict(model["nodes"].get(key) or {
                "name": decl["name"], "kind": kind, "class_name": "",
                "parent": decl["parent"], "first_seen_run": run_no,
                "mentions": 0, "tools": [],
            })
            node["last_seen_run"] = run_no
            node["mentions"] = int(node.get("mentions", 0)) + 1
            if decl["class_name"]:
                node["class_name"] = decl["class_name"]
            if kind != "unknown":
                node["kind"] = kind
            tool_name = str(getattr(t, "tool", "") or "")
            if tool_name and tool_name not in node["tools"]:
                node["tools"] = (list(node["tools"]) + [tool_name])[-5:]
            model["nodes"][key] = node

            if decl["parent"]:
                parent_key = decl["parent"]
                # A bare service name IS its own key (no further parent),
                # so a parent reference always resolves to a stable key.
                kids = model["children"].setdefault(parent_key, [])
                if key not in kids:
                    kids.append(key)
                if len(kids) > MAX_CHILDREN_PER_NODE:
                    model["children"][parent_key] = kids[-MAX_CHILDREN_PER_NODE:]
    _prune(model)
    return model


def _prune(model: Dict[str, Any]) -> None:
    nodes = model["nodes"]
    if len(nodes) <= MAX_NODES:
        return
    ordered = sorted(nodes.items(),
                     key=lambda kv: kv[1].get("last_seen_run", 0),
                     reverse=True)[:MAX_NODES]
    keep = {k for k, _ in ordered}
    model["nodes"] = dict(ordered)
    model["children"] = {
        parent: [c for c in kids if c in keep]
        for parent, kids in model["children"].items() if parent in keep
        or parent in SERVICES
    }


def nodes_of_kind(model: Any, kind: str) -> List[Dict[str, Any]]:
    return [dict(v, key=k) for k, v in (model or {}).get("nodes", {}).items()
           if v.get("kind") == kind]


def children_of(model: Any, parent: str) -> List[str]:
    return list((model or {}).get("children", {}).get(parent, []))


def to_public(model: Any, max_nodes: int = 80) -> Dict[str, Any]:
    nodes = (model or {}).get("nodes") or {}
    ordered = sorted(nodes.items(),
                     key=lambda kv: kv[1].get("last_seen_run", 0),
                     reverse=True)[:max_nodes]
    by_kind: Dict[str, int] = {}
    for _, node in nodes.items():
        by_kind[node.get("kind", "unknown")] = (
            by_kind.get(node.get("kind", "unknown"), 0) + 1)
    return {
        "node_count": len(nodes),
        "by_kind": by_kind,
        "nodes": {k: v for k, v in ordered},
        "note": ("Built entirely from this project's own successful "
                "tool-call arguments. An Instance never named in a "
                "successful call is invisible here even if it exists "
                "live in Studio; this is evidence of what Nex itself "
                "has touched or inspected, not a full DataModel dump."),
    }
