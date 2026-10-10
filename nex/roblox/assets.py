"""Roblox asset intelligence — know what depends on an asset first.

Tracks asset references (meshes, textures/decals, sounds, animations,
particles, UI images) the same way roblox/project_model.py tracks
Instances: deterministically, from real successful tool-call arguments
only. An asset id never named in a successful call does not exist here.

Two things this module is explicit about NOT claiming, because no
current MCP capability can substantiate them:

  * TRUE duplicate-content detection (the same art uploaded twice under
    two different asset ids) needs to read and compare asset bytes —
    unavailable through current MCP capability. What IS tracked is
    SHARED usage: one asset id referenced by more than one owner, which
    is normal and often desirable, not a defect.
  * "Broken reference" in the sense of "this asset id no longer resolves
    in Roblox's CDN" needs a live asset-existence check this module has
    no tool for. What IS tracked is "possibly broken": an asset id that
    only ever appeared in FAILED calls, never a successful one.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from roblox.project_model import _NAME_KEYS, _first

MAX_ASSETS = 300

_ASSET_ID_RE = re.compile(r"rbxassetid://(\d+)")
_ASSET_KEY_HINTS = ("mesh_id", "mesh", "texture_id", "texture", "sound_id",
                    "sound", "animation_id", "animation", "decal_id",
                    "decal", "image_id", "image", "particle")

_ASSET_KIND_HINTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("mesh", ("mesh",)),
    ("texture", ("texture", "decal", "image")),
    ("sound", ("sound", "audio")),
    ("animation", ("animation", "anim")),
    ("particle", ("particle", "vfx")),
)


def _classify_asset_kind(key: str) -> str:
    low = key.lower()
    for kind, hints in _ASSET_KIND_HINTS:
        if any(h in low for h in hints):
            return kind
    return "unknown"


def extract_asset_refs(args: Any) -> List[Dict[str, str]]:
    """Asset ids named in one call's own arguments — either a literal
    rbxassetid:// URL, or a bare numeric id under an asset-shaped key."""
    out: List[Dict[str, str]] = []
    if not isinstance(args, dict):
        return out
    for key, value in args.items():
        if not isinstance(value, str):
            continue
        key_low = key.lower()
        m = _ASSET_ID_RE.search(value)
        if m:
            out.append({"asset_id": m.group(1),
                       "kind": _classify_asset_kind(key)})
            continue
        if any(h in key_low for h in _ASSET_KEY_HINTS) and value.strip().isdigit():
            out.append({"asset_id": value.strip(),
                       "kind": _classify_asset_kind(key)})
    return out


def empty_model() -> Dict[str, Any]:
    return {"assets": {}}


def merge_asset_model(previous: Any, tasks: Iterable[Any],
                      run_no: int) -> Dict[str, Any]:
    prev = previous or {}
    model: Dict[str, Any] = {
        "assets": {k: dict(v) for k, v in (prev.get("assets") or {}).items()},
    }
    for t in tasks:
        status = getattr(t, "status", None)
        args = getattr(t, "args", None) or {}
        refs = extract_asset_refs(args)
        if not refs:
            continue
        owner = _first(args, _NAME_KEYS) if isinstance(args, dict) else ""
        for ref in refs:
            aid = ref["asset_id"]
            node = dict(model["assets"].get(aid) or {
                "kind": "unknown", "referenced_by": [], "mentions": 0,
                "first_seen_run": run_no, "ever_succeeded": False,
            })
            node["last_seen_run"] = run_no
            node["mentions"] = int(node.get("mentions", 0)) + 1
            if ref["kind"] != "unknown":
                node["kind"] = ref["kind"]
            if status == "success":
                node["ever_succeeded"] = True
                if owner and owner not in node["referenced_by"]:
                    node["referenced_by"] = (
                        list(node["referenced_by"]) + [owner])[-20:]
            model["assets"][aid] = node
    _prune(model)
    return model


def _prune(model: Dict[str, Any]) -> None:
    assets = model["assets"]
    if len(assets) <= MAX_ASSETS:
        return
    ordered = sorted(assets.items(),
                     key=lambda kv: kv[1].get("last_seen_run", 0),
                     reverse=True)[:MAX_ASSETS]
    model["assets"] = dict(ordered)


def shared_assets(model: Any) -> List[Dict[str, Any]]:
    """Asset ids used by more than one owner — informational reuse, not
    a defect; flagged purely so a removal doesn't surprise a second
    system that also depends on it."""
    return [dict(v, asset_id=k) for k, v in (model or {}).get("assets", {}).items()
           if len(v.get("referenced_by", [])) > 1]


def possibly_broken_assets(model: Any) -> List[Dict[str, Any]]:
    """Asset ids that have only ever appeared in FAILED calls. Weak
    signal (the failure could be unrelated to the asset itself), never a
    confirmed broken-reference claim."""
    return [dict(v, asset_id=k) for k, v in (model or {}).get("assets", {}).items()
           if v.get("mentions", 0) > 0 and not v.get("ever_succeeded")]


def dependents_of_asset(model: Any, asset_id: str) -> List[str]:
    """Which named Instances depend on this asset, before removing it."""
    return list((model or {}).get("assets", {}).get(asset_id, {})
               .get("referenced_by", []))


def to_public(model: Any, max_assets: int = 50) -> Dict[str, Any]:
    assets = (model or {}).get("assets") or {}
    ordered = sorted(assets.items(),
                     key=lambda kv: kv[1].get("last_seen_run", 0),
                     reverse=True)[:max_assets]
    by_kind: Dict[str, int] = {}
    for _, node in assets.items():
        by_kind[node.get("kind", "unknown")] = (
            by_kind.get(node.get("kind", "unknown"), 0) + 1)
    return {
        "asset_count": len(assets), "by_kind": by_kind,
        "assets": {k: v for k, v in ordered},
        "note": ("Tracks only assets this project actually referenced in "
                "a real tool call. True duplicate-content detection and "
                "live broken-reference checks are unavailable through "
                "current MCP capability; 'shared' means one asset used by "
                "multiple owners (informational), and 'possibly broken' "
                "means an asset id only ever seen in failed calls."),
    }
