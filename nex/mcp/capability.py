"""Capability classification for MCP tools.

This is the hard capability model described in the NEX overhaul spec
(STAGE 2 / STAGE 3). It gives every tool an explicit, machine-checkable
classification:

    read_only, reversible, destructive, network, requires_confirmation
    + a coarse category (READ / CREATE / MODIFY / BUILD / TEST /
      DESTRUCTIVE / NETWORK / UNKNOWN)

Design rules (from the spec + audit hardening):
  * BOTH signals are always consulted: keyword heuristics on the tool
    name AND explicit MCP annotations (readOnlyHint, destructiveHint,
    idempotentHint, openWorldHint).
  * MCP annotations are UNTRUSTED: they come from the very server the
    classification guards, and a malicious or broken server may lie
    (e.g. ``{"name": "delete_project",
    "annotations": {"readOnlyHint": true}}``). They may therefore only
    RAISE caution — the final category is the MORE dangerous of
    (heuristic, annotation); a hint can never downgrade a tool.
  * Unknown tools are classified CONSERVATIVELY (assume they may need
    confirmation; never assume safe).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
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

    Two signals are combined, and the more dangerous one wins:
      1. Keyword heuristics on the tool name (Nex's own, unspoofable
         view of what the name says).
      2. Explicit MCP annotations (live server-provided metadata).

    MCP annotations are UNTRUSTED metadata — they are supplied by the
    very server the classification is meant to gate, and a malicious or
    broken server may lie (e.g. a tool named ``delete_project`` that
    claims ``readOnlyHint: true``). The rule is therefore SEVERITY-MAX,
    never severity-min:

        final category = the MORE dangerous of (heuristic, annotation)
        destructive    = heuristic-destructive OR destructiveHint
        read_only      = ONLY when both signals agree (heuristic READ +
                         the server positively asserts readOnlyHint)

    A hint may RAISE caution but can never LOWER it.
    """
    name_l = _norm(name)
    ann = annotations or {}

    # Heuristic baseline — always computed, even when annotations exist.
    base_cat = _heuristic_category(name_l)
    base_destructive = base_cat == DESTRUCTIVE

    if ann:
        ann_cat = _category_from_annotations(ann, name_l)
        # Severity-max: an annotation can only make a tool look MORE
        # dangerous, never less.
        cat = _more_dangerous(base_cat, ann_cat)
        destructive = (base_destructive
                       or bool(ann.get("destructiveHint", False)))
        # read_only requires BOTH signals to agree: the name must read as
        # READ and the (untrusted) server must positively assert it.
        read_only = (base_cat == READ and not destructive
                     and bool(ann.get("readOnlyHint", False)))
        network = (cat == NETWORK) or _has_hint(name_l, NETWORK)
        return ToolCapability(
            category=cat,
            read_only=read_only,
            destructive=destructive,
            reversible=bool(ann.get("idempotentHint", False)),
            network=network,
            requires_confirmation=(destructive or _needs_confirm(cat)),
            source="explicit+heuristic",
            notes=("MCP annotations treated as UNTRUSTED hints; "
                   "severity-max with the name heuristic — a hint can "
                   "raise caution but never lower it"),
        )

    # 2) No annotations at all: pure heuristic fallback.
    cat = base_cat
    destructive = base_destructive
    return ToolCapability(
        category=cat,
        read_only=(cat == READ),
        reversible=(cat in (MODIFY, BUILD, TEST)),
        destructive=destructive,
        network=(cat == NETWORK),
        requires_confirmation=destructive or _needs_confirm(cat),
        # Unknown tools are flagged conservative so callers treat them as
        # needing caution (STAGE 3: "Unknown tools should be classified
        # conservatively").
        source="conservative" if cat == UNKNOWN else "heuristic",
        notes="heuristic classification; live annotations preferred",
    )


def _needs_confirm(cat: str) -> bool:
    """Categories that warrant confirmation even when not destructive:
    unknown (unclassifiable = untrusted by default) and network
    (leaves the machine)."""
    return cat in (UNKNOWN, NETWORK)


def _more_dangerous(a: str, b: str) -> str:
    """The category with the higher severity. UNKNOWN ranks highest so
    'we can't classify it' can never be downgraded by a
    friendly-looking annotation."""
    order = {READ: 1, CREATE: 2, MODIFY: 3, BUILD: 4,
             TEST: 5, NETWORK: 6, DESTRUCTIVE: 7, UNKNOWN: 8}
    return a if order.get(a, 8) >= order.get(b, 8) else b


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


# --- the OPERATOR CAPABILITY REGISTRY (layer 3) ----------------------------
# A local, operator-owned file pins the classification of specific tools
# where the heuristics + (untrusted) annotations are not enough — e.g. to
# escalate a suspiciously-named tool:
#
#     $NEX_CAPABILITY_FILE  (default: ~/.nex/capabilities.json)
#     {
#       "roblox-studio": {
#         "apply_decision": {"category": "destructive"},
#         "inspect_project": {"category": "read"}
#       }
#     }
#
# Semantics are the SAME severity-max rule as for MCP annotations:
# the registry can only RAISE caution (a higher-severity category, or
# requires_confirmation=true). It can never LOWER it — a pin claiming a
# destructive tool is "read" is ignored. An invalid file or an unknown
# category is ignored (with a warning); the heuristic + annotation
# classification stays in effect. This file is operator infrastructure,
# not model input: the model never reads or writes it.
_REG_SEVERITY = {READ: 1, CREATE: 2, MODIFY: 3, BUILD: 4, TEST: 5,
                 NETWORK: 6, DESTRUCTIVE: 7, UNKNOWN: 8}

_reg_cache: Dict[str, Any] = {"path": None, "mtime": None, "data": {}}


def capability_registry_path() -> str:
    p = os.environ.get("NEX_CAPABILITY_FILE", "").strip()
    if p:
        return p
    return os.path.join(os.path.expanduser("~"), ".nex", "capabilities.json")


def capability_registry() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Load the registry (mtime-cached). Never raises."""
    import json as _json
    path = capability_registry_path()
    try:
        st = os.stat(path)
    except OSError:
        return {}
    if _reg_cache.get("path") == path and _reg_cache.get("mtime") == st.st_mtime:
        return _reg_cache["data"]  # type: ignore[return-value]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[nex] capability registry %r ignored (%s)\n"
                         % (path, exc))
        data = {}
    if not isinstance(data, dict):
        data = {}
    _reg_cache.update(path=path, mtime=st.st_mtime, data=data)
    return data  # type: ignore[return-value]


def registry_entry(server: Optional[str], tool: str) -> Optional[Dict[str, Any]]:
    if not server or not tool:
        return None
    servers = capability_registry()
    entry = servers.get(server)
    if not isinstance(entry, dict):
        return None
    e = entry.get(tool)
    return e if isinstance(e, dict) else None


def apply_capability_registry(cap: ToolCapability, server: Optional[str],
                              tool: str) -> ToolCapability:
    """Apply the operator registry layer over a classification.

    STRICTEST WINS: the returned capability is the input, possibly
    ESCALATED (higher-severity category / requires_confirmation=true).
    Downgrades are silently ignored — the same rule that keeps a lying
    server annotation from softening a tool keeps a lying registry pin
    from softening one.
    """
    e = registry_entry(server, tool)
    if not e:
        return cap
    out = cap
    cat = e.get("category")
    if isinstance(cat, str):
        cat_l = cat.strip().lower()
        if cat_l in _REG_SEVERITY and \
                _REG_SEVERITY[cat_l] > _REG_SEVERITY.get(out.category, 8):
            out = replace(
                out,
                category=cat_l,
                destructive=out.destructive or (cat_l == DESTRUCTIVE),
                network=out.network or (cat_l == NETWORK),
                read_only=(out.read_only and cat_l == READ),
                source=out.source + "+registry",
            )
    if e.get("requires_confirmation") is True:
        out = replace(out, requires_confirmation=True)
    # `requires_confirmation: false` / `read_only: true` pins are
    # deliberately NOT honored (severity-max, see above).
    return out
