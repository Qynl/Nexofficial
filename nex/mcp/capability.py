"""Capability classification for MCP tools.

This is the hard capability model described in the NEX overhaul spec
(STAGE 2 / STAGE 3). It gives every tool an explicit, machine-checkable
classification:

    read_only, reversible, destructive, network, requires_confirmation
    + a coarse category (READ / CREATE / MODIFY / BUILD / TEST /
      CODE_EXECUTION / DESTRUCTIVE / NETWORK / UNKNOWN)

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
import re
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional

# --- category constants ----------------------------------------------------
READ = "read"
CREATE = "create"
MODIFY = "modify"
BUILD = "build"
TEST = "test"
# CODE_EXECUTION = the tool RUNS CODE (a script, a shell, a Lua/Python
# payload) on the engine's behalf. Not "just another test tool": whatever
# the payload contains executes with the engine's privileges. Ranked just
# below DESTRUCTIVE (a delete is final) and above NETWORK; UNKNOWN stays
# the most cautious rank of all.
CODE_EXECUTION = "code_execution"
DESTRUCTIVE = "destructive"
NETWORK = "network"
UNKNOWN = "unknown"

CATEGORIES = (READ, CREATE, MODIFY, BUILD, TEST, CODE_EXECUTION,
              DESTRUCTIVE, NETWORK, UNKNOWN)

# Heuristic keyword sets, used ONLY as a fallback (STAGE 3 rule).
# The vocabulary below is the EDITOR vocabulary: it must place ordinary
# engine tools correctly, because a mislabeled tool is now a tool that
# stalls an autonomous run (UNKNOWN => confirmation). Adding a hint to a
# LOW-severity bucket (READ) can never escalate anything — the
# severity-max rule only ever moves tools toward caution.
_CAT_HINTS: Dict[str, tuple] = {
    READ: ("get", "read", "list", "find", "search", "inspect", "query",
           "console_output", "output_log", "datamodel_tree", "editor_state",
           "open_scripts", "actor_details",
           "describe", "fetch", "status", "state", "discover", "export",
           "download", "show", "peek", "analyze",
           # observation + editor inspection (screenshot/log/console are
           # the tools the OBSERVE loop depends on)
           "screenshot", "screengrab", "capture", "snapshot", "log", "logs",
           # health checks: the plan-UX classifier in agent/plans.py already
           # treats ping/echo as safe, so the capability model must agree —
           # otherwise a health check stalls an autonomous run.
           "ping", "pong", "echo",
           "console_output", "telemetry", "metric", "metrics", "stats",
           "info", "health", "version", "open", "close", "preview",
           "diff", "compare", "check"),
    CREATE: ("create", "add", "new", "spawn", "make", "generate", "import",
             "place", "insert", "build_asset", "add_actor", "create_actor",
             "duplicate", "clone", "instantiate",
             "create_part", "create_instance", "create_blueprint",
             "create_level", "create_widget", "insert_model", "insert_asset",
             "spawn_actor", "add_component", "add_widget", "attach_component"),
    MODIFY: ("set", "update", "edit", "modify", "change", "apply", "adjust",
             "configure", "write", "animate", "move", "transform", "rename",
             "save", "undo", "redo", "pause", "resume", "stop", "teleport",
             "group", "align", "snap", "parent", "tag", "label", "assign",
             "open_level", "load_level", "save_level", "save_place",
             "save_current_level", "weld", "sculpt", "paint_terrain",
             "retarget", "rig_character", "set_actor_transform",
             "set_actor_property", "set_component_property",
             "pie_stop", "exit_play"),
    BUILD: ("compile", "build", "package", "bake", "cook", "deploy",
            "export_package",
            "compile_blueprint", "build_project", "package_project",
            "cook_content", "bake_lighting"),
    # Engine vocabulary. These are WHOLE tool names (the matcher is a
    # substring test on the lowercased name), chosen so real Roblox Studio /
    # Unreal MCP tools land in the right bucket instead of UNKNOWN — an
    # UNKNOWN tool stalls an autonomous run on a confirmation prompt, and a
    # false TEST costs nothing (TEST is not confirmation-gated).
    TEST: ("run", "launch", "play", "test", "simulate", "verify",
           "playtest", "inspect_runtime",
           "play_solo", "start_play", "stop_play", "start_pie", "pie_start",
           "play_in_editor", "run_playtest", "automation_test",
           "start_session"),
    DESTRUCTIVE: ("delete", "remove", "destroy", "erase", "drop", "reset",
                  "purge", "wipe", "kill", "terminate", "clear", "uninstall"),
    NETWORK: ("fetch_url", "http_request", "http_get", "httpget", "upload",
              "publish", "share", "send", "post", "fetch_remote",
              "curl", "wget", "webhook"),
}


# --- CODE EXECUTION: exact token rules ------------------------------------
# A tool NAME alone never proves code execution, but these token shapes
# are unambiguous in the MCP ecosystem. Tokens are split on
# non-alphanumerics, so `execute_luau` and `run_script` are decided by
# their PARTS rather than by substring luck (which is how `run_python`
# ended up in TEST: it contains "run").
#
# The rules are exactly as narrow as the risk demands: a false positive
# here cannot be undone (the operator registry may only RAISE severity),
# so engine nouns like `script`, `console`, `command` or `process` are
# NOT hard tokens — they appear in legitimate editor tools
# (`create_script`, `get_console_output`, `list_commands`). They become
# code execution only next to an execution VERB.
_TOK_SPLIT = re.compile(r"[^a-z0-9]+")

# Tokens that ALONE mean "runs code / a shell / a language runtime".
# `terminal` is deliberately included: an engine server exposing a
# terminal IS a shell, and no game tool is named "terminal".
_CODE_TOKENS = frozenset({
    "eval", "exec", "shell", "bash", "sh", "zsh",
    "powershell", "pwsh", "subprocess", "popen", "pty",
    "loadstring", "dofile", "loadfile",
    "lua", "luau", "python", "python3", "ruby", "perl", "php",
    "javascript", "js", "node", "nodejs", "terminal",
})
# Tokens that name WHAT is run — they need an execution verb beside them.
_CODE_NOUNS = frozenset({
    "script", "scripts", "code", "lua", "luau", "python", "sh", "shell",
    "cmd", "command", "commands", "console", "file", "expression", "expr",
    "snippet", "string", "payload", "program", "process", "module",
    "tool", "sandbox", "automation",
})
_CODE_VERBS = frozenset({
    "run", "execute", "exec", "eval", "evaluate", "invoke", "spawn",
    "launch", "start", "load", "interpret", "shell",
})
# NOUNS that are legitimate engine nouns and must NOT be dragged into code
# execution by a verb: `run_game`, `run_tests`, `playtest`, `spawn_enemy`,
# `start_timer`, `load_asset`, `launch_editor`, `apply_physics_material`.
_CODE_SAFE_NOUNS = frozenset({
    "game", "games", "editor", "player", "players", "level", "levels",
    "scene", "scenes", "test", "tests", "playtest", "playtests",
    "simulation", "sim", "world", "worlds", "animation", "anim",
    "animations", "physics", "material", "materials", "asset", "assets",
    "mesh", "meshes", "texture", "textures", "audio", "sound", "sounds",
    "part", "parts", "actor", "actors", "enemy", "enemies", "npc", "npcs",
    "timer", "tween", "camera", "cameras", "light", "lights", "particle",
    "particles", "shader", "shaders", "ui", "widget", "widgets", "hud",
    "build", "compile", "package", "deploy", "preview", "render", "frame",
    "frames", "session", "instance", "instances", "model", "models", "rig",
    "rigs", "skeleton", "behavior", "behaviour", "event", "events",
    "signal", "signals", "metric", "metrics", "balance", "profile",
    "profiler", "timeline", "playback",
})


def tokenize(name_l: str) -> tuple:
    return tuple(t for t in _TOK_SPLIT.split(name_l or "") if t)


def _is_code_execution(name_l: str) -> bool:
    """True when the TOOL NAME says it runs code / a shell / a process.

    Deterministic token rules:
      * a token that IS a code/process primitive (`eval`, `shell`,
        `exec`, `subprocess`, `loadstring`, `luau`, `python`, ...), or
      * an execution verb next to a noun that is not a known-safe engine
        noun (`run_script`, `execute_python`, `spawn_process`,
        `run_console_command`) — while `run_game`, `run_tests`,
        `create_script`, `spawn_enemy` stay in their own buckets.
    """
    toks = tokenize(name_l)
    if not toks:
        return False
    for t in toks:
        if t in _CODE_TOKENS:
            return True
    for t in toks:
        if t in _CODE_VERBS:
            for u in toks:
                if u == t or u in _CODE_SAFE_NOUNS:
                    continue
                if u in _CODE_NOUNS or u in _CODE_TOKENS:
                    return True
    return False


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
    unknown (unclassifiable = untrusted by default), network (leaves the
    machine) and CODE EXECUTION (runs arbitrary code with the engine's
    privileges — the escape primitive)."""
    return cat in (UNKNOWN, NETWORK, CODE_EXECUTION)


def _more_dangerous(a: str, b: str) -> str:
    """The category with the higher severity. UNKNOWN ranks highest so
    'we can't classify it' can never be downgraded by a
    friendly-looking annotation."""
    order = {READ: 1, CREATE: 2, MODIFY: 3, BUILD: 4, TEST: 5,
             NETWORK: 6, CODE_EXECUTION: 7, DESTRUCTIVE: 8, UNKNOWN: 9}
    return a if order.get(a, 9) >= order.get(b, 9) else b


def _category_from_annotations(ann: Dict[str, Any], name_l: str) -> str:
    if ann.get("destructiveHint"):
        return DESTRUCTIVE
    if _is_code_execution(name_l):
        return CODE_EXECUTION
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
    # Order matters: most specific first. Code execution outranks the
    # generic buckets because a running payload can do everything a
    # create/modify/build/test tool can do — and more. DESTRUCTIVE still
    # outranks it (a delete is final).
    if _is_code_execution(name_l):
        return CODE_EXECUTION
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
                 NETWORK: 6, CODE_EXECUTION: 7, DESTRUCTIVE: 8,
                 UNKNOWN: 9}

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
    # deliberately NOT honored (severity-max, see above). A STANDING
    # APPROVAL is a different statement and must be explicit, per tool:
    # `"approved": true` means "the operator has read this tool and takes
    # responsibility for it running autonomously". Only the operator's own
    # file can say that; it is never inferred from a name or a hint.
    if e.get("approved") is True:
        out = replace(out, requires_confirmation=False,
                      source=out.source + "+operator_approved")
    return out
