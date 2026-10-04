"""Evidence-based production quality protocol for game-making runs.

Nex is engine-agnostic: it cannot assume that Unity, Unreal, Godot,
Roblox Studio, Blender, or any particular MCP vocabulary is present.  This
module therefore derives a small production contract from two facts only:

* what the user asked for; and
* what tools the live MCP registry actually exposes.

The protocol does *not* call tools.  It classifies the catalog, gives the
planner capability-aware guidance, and audits successful task evidence at
completion.  That separation keeps the MCP-only action boundary intact.

"AAA" is intentionally an ambition label, never an automatic claim.  A run
only clears a gate when a successful MCP step used a live tool whose
metadata supports that gate.  Missing tools remain visible as unavailable;
we do not convert an intention, a model sentence, or a successful mutation
into visual/playtest/performance evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from mcp.capability import BUILD, CODE_EXECUTION, CREATE, MODIFY, READ, TEST


# The order is the production narrative shown in prompts and reports.
GATE_ORDER = (
    "inspection",
    "implementation",
    "build",
    "playtest",
    "visual",
    "visual_review",
    "diagnostics",
    "verification",
    "performance",
)

_GATE_LABELS = {
    "inspection": "Inspect the existing project and constraints",
    "implementation": "Implement and integrate the playable slice",
    "build": "Compile, build, bake, cook, or package successfully",
    "playtest": "Run the game in a real runtime or editor play session",
    "visual": "Capture real visual output from the game or editor",
    "visual_review": "Review captured output for composition, clarity, and defects",
    "diagnostics": "Review runtime logs, errors, or diagnostics",
    "verification": "Verify acceptance criteria with tests or checks",
    "performance": "Measure performance with profiling or telemetry",
}

_GAME_NOUNS = frozenset({
    "game", "games", "gameplay", "level", "levels", "scene", "scenes",
    "player", "players", "enemy", "enemies", "npc", "npcs", "quest",
    "quests", "combat", "platformer", "shooter", "rpg", "puzzle",
    "world", "worlds", "boss", "hud", "menu", "menus", "character",
    "characters", "mechanic", "mechanics", "checkpoint", "arena", "gta",
    "sandbox", "mmorpg", "mmo", "metroidvania", "obby", "tycoon",
    "experience", "roblox", "unreal", "ue5", "blueprint", "blueprints",
    "luau", "datamodel", "remoteevent", "remotefunction",
})
_AUTHORING_WORDS = frozenset({
    "make", "create", "build", "develop", "design", "implement", "add",
    "produce", "prototype", "polish", "upgrade", "improve", "fix", "ship",
    "author", "craft", "remake", "refactor", "integrate", "compile",
    "cook", "package", "publish",
})
_FLAGSHIP_WORDS = (
    "aaa", "aaa-style", "triple-a", "triple a", "high quality",
    "high-quality", "production quality", "production-quality", "polished",
    "professional", "shippable", "release ready", "release-ready",
    "commercial quality", "cinematic", "premium",
)


def _tokens(text: str) -> Set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def is_game_production_goal(goal: str) -> bool:
    """Conservative intent check: authoring a game, not merely discussing it."""
    low = (goal or "").lower()
    words = _tokens(low)
    has_game_subject = bool(words & _GAME_NOUNS)
    has_authoring = bool(words & _AUTHORING_WORDS)
    explicit_quality = any(term in low for term in _FLAGSHIP_WORDS)
    return has_game_subject and (has_authoring or explicit_quality)


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    return any(term in text for term in terms)


def tool_gates(tool: Any) -> Set[str]:
    """Map a live ToolView to gates supported by its own metadata.

    Names dominate because descriptions are untrusted prose and can contain
    prompt injection.  Descriptions are only a secondary lexical signal;
    they never grant execution and the manager still authorizes every call.
    """
    name = str(getattr(tool, "name", "") or "").lower()
    desc = str(getattr(tool, "description", "") or "").lower()[:500]
    cap = getattr(tool, "capability", None)
    category = getattr(cap, "category", None)
    gates: Set[str] = set()

    if category in (CREATE, MODIFY, CODE_EXECUTION):
        gates.add("implementation")
    if category == BUILD or _contains_any(name, (
            "build", "compile", "package", "cook", "bake", "bundle")):
        gates.add("build")

    # A runtime/play session is stronger evidence than a generic "test".
    if _contains_any(name, (
            "playtest", "run_game", "launch_game", "play_game", "start_pie",
            "pie_start", "play_in_editor", "start_play", "simulate_game",
            "runtime_session", "start_session", "play_solo",
            "start_local_server", "start_server", "start_client",
            "test_players", "standalone_game")):
        gates.add("playtest")

    if _contains_any(name, (
            "screenshot", "screen_grab", "screengrab", "capture_frame",
            "capture_view", "render_preview", "visual_preview",
            "viewport_capture", "snapshot_view", "capture_viewport",
            "studio_screenshot")):
        gates.add("visual")

    if _contains_any(name, (
            "inspect_visual", "analyze_visual", "analyse_visual",
            "analyze_screenshot", "analyse_screenshot", "review_frame",
            "review_screenshot", "visual_diff", "compare_screenshot",
            "validate_visual", "check_visual", "analyze_frame")):
        gates.add("visual_review")
        # Reviewing an existing image is independent from capturing one. A
        # name containing "screenshot" for context must not clear both gates.
        if not _contains_any(name, ("capture", "render", "snapshot")):
            gates.discard("visual")

    if _contains_any(name, (
            "log", "console_output", "diagnostic", "error_report", "crash",
            "warning", "runtime_output", "output_log", "message_log",
            "studio_output", "script_analysis")):
        gates.add("diagnostics")

    if _contains_any(name, (
            "profile", "profiler", "performance", "frametime", "frame_time",
            "fps", "memory_stats", "gpu_stats", "telemetry", "benchmark",
            "unreal_insights", "trace_capture", "stat_unit", "stat_gpu",
            "microprofiler", "micro_profiler", "script_profiler",
            "performance_stats", "developer_console")):
        gates.add("performance")

    if _contains_any(name, (
            "verify", "validate", "automation_test", "unit_test",
            "integration_test", "functional_test", "acceptance_test",
            "check_game", "check_asset", "audit_game", "testservice",
            "test_service", "validate_place", "lint_luau", "gauntlet")):
        gates.add("verification")
    elif category == TEST and "play" not in name and "run_game" not in name:
        gates.add("verification")

    specialized_observation = gates.intersection({
        "visual", "visual_review", "diagnostics", "performance"
    })
    if category == READ and not specialized_observation and _contains_any(
            name, ("inspect", "list", "get", "read", "query", "describe",
                   "state", "tree", "hierarchy", "scene", "project",
                   "asset", "actor", "node", "component", "blueprint")):
        gates.add("inspection")

    # Descriptions can disambiguate intentionally generic engine APIs, but
    # require a matching category so prose alone cannot claim a quality gate.
    if category == READ and not gates.intersection({
            "inspection", "visual", "visual_review", "diagnostics",
            "performance"}):
        if _contains_any(desc, ("inspect project", "inspect scene",
                                "project state", "scene hierarchy")):
            gates.add("inspection")
    if category == TEST and "playtest" in desc:
        gates.add("playtest")

    return gates


@dataclass(frozen=True)
class QualityProfile:
    active: bool
    tier: str
    label: str
    required_gates: tuple
    threshold: int

    def to_public(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "tier": self.tier,
            "label": self.label,
            "required_gates": list(self.required_gates),
            "threshold": self.threshold,
        }


def profile_for_goal(goal: str) -> QualityProfile:
    if not is_game_production_goal(goal):
        return QualityProfile(False, "none", "Not a game-production run", (), 0)
    low = (goal or "").lower()
    flagship = any(term in low for term in _FLAGSHIP_WORDS)
    if flagship:
        return QualityProfile(
            True, "flagship", "Flagship production target (aspirational)",
            GATE_ORDER, 100)
    return QualityProfile(
        True, "production", "Production game-making target",
        ("inspection", "implementation", "build", "playtest", "visual",
         "visual_review", "diagnostics", "verification"),
        100)


def gate_catalog(registry: Any) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {g: [] for g in GATE_ORDER}
    tools = registry.all_tools() if registry is not None else []
    for tool in tools:
        for gate in tool_gates(tool):
            out[gate].append(tool)
    return out


def planning_brief(profile: QualityProfile, registry: Any) -> str:
    """A compact, capability-aware quality contract for the planner."""
    if not profile.active:
        return ""
    catalog = gate_catalog(registry)
    lines = [
        "GAME PRODUCTION QUALITY CONTRACT",
        "Target: %s. This is a workflow target, not permission to claim "
        "literal AAA quality." % profile.label,
        "Use a vertical-slice sequence: inspect -> establish explicit "
        "acceptance criteria -> implement systems/content -> integrate -> "
        "build/run -> observe -> verify -> correct once if evidence fails.",
        "Each evidence gate below must be a separate dependency-ordered MCP "
        "step when a suitable live tool exists. Use only catalog tools and "
        "do not substitute prose or implementation success for observation.",
    ]
    for gate in profile.required_gates:
        tools = catalog.get(gate, [])
        names = ", ".join(t.full_name for t in tools[:4])
        if tools:
            lines.append("- %s: AVAILABLE via %s" %
                         (_GATE_LABELS[gate], names))
        else:
            lines.append("- %s: UNAVAILABLE in the live MCP catalog; do not "
                         "invent it" % _GATE_LABELS[gate])
    lines.extend([
        "Plan the smallest coherent playable slice first. Prefer inspecting "
        "before mutation and reuse existing project conventions/assets.",
        "Make acceptance criteria concrete (gameplay behavior, visual result, "
        "runtime errors, and performance where measurable). A create/build "
        "call is not a playtest; a playtest is not a screenshot; capturing a "
        "screenshot is not reviewing it; and a visual review is not a "
        "performance profile.",
        "Do not repeat already-successful work during a corrective pass. Keep "
        "polish bounded and report every unverified dimension honestly.",
    ])
    return "\n".join(lines)


def result_declares_failure(value: Any, depth: int = 0) -> bool:
    """Catch explicit negative verdicts without guessing from prose.

    Transport success is necessary but a verifier returning ``valid: false``
    is not positive quality evidence. Only conventional structured fields are
    considered; arbitrary strings remain untrusted data, not instructions or
    semantic truth.
    """
    if depth > 8:
        return True
    if isinstance(value, dict):
        for key, item in value.items():
            low = str(key).lower()
            if low in {"ok", "success", "succeeded", "valid", "playable",
                       "passed", "compiled", "built"} and item is False:
                return True
            if low in {"error", "errors", "failure", "failures"} and item:
                return True
            if low == "status" and isinstance(item, str) and item.lower() in {
                    "failed", "failure", "error", "invalid", "broken"}:
                return True
        return any(result_declares_failure(item, depth + 1)
                   for key, item in value.items()
                   if str(key).lower() in {"result", "data", "payload"})
    if isinstance(value, list):
        return any(result_declares_failure(item, depth + 1)
                   for item in value[:100])
    return False


def assess(profile: QualityProfile, registry: Any,
           tasks: Iterable[Any]) -> Dict[str, Any]:
    """Build the public scorecard from successful, positive tool evidence."""
    if not profile.active:
        return profile.to_public()
    catalog = gate_catalog(registry)
    evidence: Dict[str, List[Dict[str, str]]] = {g: [] for g in GATE_ORDER}
    rejected: List[Dict[str, str]] = []
    seen = set()
    for task in tasks:
        if str(getattr(task, "status", "")) != "success":
            continue
        server = str(getattr(task, "server", "") or "")
        name = str(getattr(task, "tool", "") or "")
        tv: Optional[Any] = None
        if registry is not None:
            tv = registry.by_name(server + "." + name) if server else \
                registry.by_name(name)
        if tv is None:
            continue
        if result_declares_failure(getattr(task, "result", None)):
            rejected.append({
                "step": str(getattr(task, "name", "") or name)[:120],
                "tool": tv.full_name,
                "reason": "tool result contains an explicit negative verdict",
            })
            continue
        for gate in tool_gates(tv):
            key = (gate, server, name, str(getattr(task, "id", "")))
            if key in seen:
                continue
            seen.add(key)
            evidence[gate].append({
                "step": str(getattr(task, "name", "") or name)[:120],
                "tool": tv.full_name,
            })

    gates: List[Dict[str, Any]] = []
    passed_count = 0
    missing: List[str] = []
    unavailable: List[str] = []
    for gate in profile.required_gates:
        available = bool(catalog.get(gate))
        passed = bool(evidence.get(gate))
        if passed:
            state = "passed"
            passed_count += 1
        elif available:
            state = "not_run"
            missing.append(gate)
        else:
            state = "unavailable"
            missing.append(gate)
            unavailable.append(gate)
        gates.append({
            "id": gate,
            "label": _GATE_LABELS[gate],
            "status": state,
            "evidence": evidence.get(gate, [])[:6],
            "available_tools": [t.full_name for t in catalog.get(gate, [])[:6]],
        })

    total = len(profile.required_gates)
    score = round(100.0 * passed_count / total) if total else 100
    return {
        **profile.to_public(),
        "score": score,
        "passed": not missing and score >= profile.threshold,
        "gates": gates,
        "missing": missing,
        "unavailable": unavailable,
        "correctable": [g for g in missing if g not in unavailable],
        "rejected_evidence": rejected[:12],
        "disclaimer": (
            "This score measures MCP-backed production evidence, not artistic "
            "taste, market readiness, or guaranteed AAA quality."
        ),
    }


def correction_note(scorecard: Dict[str, Any]) -> str:
    gates = scorecard.get("correctable") or []
    if not gates:
        return ""
    labels = [_GATE_LABELS.get(g, g) for g in gates]
    return (
        "Quality review found available evidence gates that were not run: %s. "
        "Add only the missing inspect/build/playtest/observation/verification "
        "steps; do not repeat successful implementation work."
        % "; ".join(labels)
    )
