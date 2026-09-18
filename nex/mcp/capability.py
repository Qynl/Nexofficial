"""Capability classification for MCP tools.

This is the hard capability model described in the NEX overhaul spec
(STAGE 2 / STAGE 3). It gives every tool an explicit, machine-checkable
classification:

    read_only, reversible, destructive, network, requires_confirmation
    + a coarse category (READ / CREATE / MODIFY / BUILD / TEST /
      DESTRUCTIVE / NETWORK / UNKNOWN)

Design rules (from the spec):
  * Prefer EXPLICIT metadata when the live MCP server provides it
    (MCP 2025 tool ``annotations``: readOnlyHint, destructiveHint,
    idempotentHint, openWorldHint).
  * Fall back to keyword heuristics ONLY when no explicit metadata exists.
  * Unknown tools are classified CONSERVATIVELY (assume they may need
    confirmation; never assume safe).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# --- category constants ----------------------------------------------------
READ = "read"
CREATE = "create"
MODIFY = "modify"
BUILD = "build"
TEST = "test"
DESTRUCTIVE = "destructive"
NETWORK = "network"
UNKNOWN = "unknown"

CATEGORIES = (READ, CREATE, MODIFY, BUILD, TEST, DESTRUCTIVE, NETWORK, UNKNOWN)

# Heuristic keyword sets, used ONLY as a fallback (STAGE 3 rule).
_CAT_HINTS: Dict[str, tuple] = {
    READ: ("get", "read", "list", "find", "search", "inspect", "query",
           "describe", "fetch", "status", "state", "discover", "export",
           "download", "show", "peek", "analyze"),
    CREATE: ("create", "add", "new", "spawn", "make", "generate", "import",
             "place", "insert", "build_asset", "add_actor", "create_actor"),
    MODIFY: ("set", "update", "edit", "modify", "change", "apply", "adjust",
             "configure", "write", "animate", "move", "transform", "rename"),
    BUILD: ("compile", "build", "package", "bake", "cook", "deploy",
            "export_package"),
    TEST: ("run", "launch", "play", "test", "simulate", "verify",
           "playtest", "inspect_runtime"),
    DESTRUCTIVE: ("delete", "remove", "destroy", "erase", "drop", "reset",
                  "purge", "wipe", "kill", "terminate", "clear", "uninstall"),
    NETWORK: ("fetch_url", "http_request", "upload", "publish", "share",
              "send", "post", "fetch_remote"),
}


@dataclass
class ToolCapability:
    """Explicit, machine-checkable classification for one tool."""
    category: str = UNKNOWN
    read_only: bool = False
    reversible: bool = False
    destructive: bool = False
    network: bool = False
    requires_confirmation: bool = True   # conservative default
    source: str = "conservative"          # explicit | heuristic | conservative
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "read_only": self.read_only,
            "reversible": self.reversible,
            "destructive": self.destructive,
            "network": self.network,
            "requires_confirmation": self.requires_confirmation,
            "source": self.source,
        }


def _norm(s: str) -> str:
    return (s or "").lower()


def classify_capability(name: str,
                        annotations: Optional[Dict[str, Any]] = None,
                        schema: Optional[Dict[str, Any]] = None) -> ToolCapability:
    """Classify a tool.

    Priority:
      1. Explicit MCP annotations (live MCP truth).
      2. Keyword heuristics on the tool name (fallback).
      3. Conservative defaults (unknown).
    """
    name_l = _norm(name)
    ann = annotations or {}

    # 1) Explicit annotations (MCP 2025).
    if ann:
        read_only = bool(ann.get("readOnlyHint", False))
        destructive = bool(ann.get("destructiveHint", False))
        cat = _category_from_annotations(ann, name_l)
        cap = ToolCapability(
            category=cat,
            read_only=read_only,
            destructive=destructive,
            reversible=bool(ann.get("idempotentHint", False)),
            network=_CAT_HINTS[NETWORK] and _has_hint(name_l, NETWORK),
            requires_confirmation=destructive,
            source="explicit",
        )
        if cap.requires_confirmation is False and cat in (READ,):
            cap.requires_confirmation = False
        return cap

    # 2) Heuristic fallback.
    cat = _heuristic_category(name_l)
    destructive = cat == DESTRUCTIVE
    return ToolCapability(
        category=cat,
        read_only=(cat == READ),
        reversible=(cat in (MODIFY, BUILD, TEST)),
        destructive=destructive,
        network=(cat == NETWORK),
        requires_confirmation=destructive or cat == UNKNOWN,
        # Unknown tools are flagged conservative so callers treat them as
        # needing caution (STAGE 3: "Unknown tools should be classified
        # conservatively").
        source="conservative" if cat == UNKNOWN else "heuristic",
        notes="heuristic classification; live annotations preferred",
    )


def _category_from_annotations(ann: Dict[str, Any], name_l: str) -> str:
    if ann.get("destructiveHint"):
        return DESTRUCTIVE
    if ann.get("readOnlyHint"):
        return READ
    # openWorldHint implies touching external/shared state.
    if ann.get("openWorldHint"):
        return NETWORK if _has_hint(name_l, NETWORK) else MODIFY
    return _heuristic_category(name_l)


def _has_hint(name_l: str, cat: str) -> bool:
    for h in _CAT_HINTS[cat]:
        if h in name_l:
            return True
    return False


def _heuristic_category(name_l: str) -> str:
    # Order matters: most specific first.
    for cat in (DESTRUCTIVE, NETWORK, BUILD, TEST, CREATE, MODIFY, READ):
        if _has_hint(name_l, cat):
            return cat
    return UNKNOWN


def capability_for_tool(tool: Dict[str, Any]) -> ToolCapability:
    """Convenience: classify straight from a discovered tool dict."""
    return classify_capability(
        tool.get("name", ""),
        annotations=tool.get("annotations"),
        schema=tool.get("inputSchema"),
    )


def category_hints() -> Dict[str, tuple]:
    """Public read-only view of the heuristic keyword sets.

    This is the single source of truth for name-based classification.
    Other modules (e.g. mc.py's 3-bucket plan pre-tagger) must DERIVE
    their hints from here rather than keeping their own copy, so there
    is exactly one canonical capability/policy vocabulary.
    """
    return {k: tuple(v) for k, v in _CAT_HINTS.items()}
