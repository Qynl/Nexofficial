"""Hierarchical studio program for genuinely large game-authoring goals.

A one-shot plan is the wrong abstraction for an open-world game. This module
recognizes large scope, measures whether the connected MCP surface resembles a
production toolchain, and gives the agent a bounded sequence of stage briefs.
The stages remain engine-agnostic and contain no action path; they only shape
plans that still pass through manager.call -> policy -> transport.

The program is deliberately honest. It can organize a long build and preserve
outputs across stage plans, but it cannot turn a thin editor bridge into a
studio. Missing disciplines are surfaced as readiness gaps, not papered over
with generated prose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Set

from agent.engines import all_engine_readiness
from agent.mcp_production import contract_health
from agent.quality import (
    gate_catalog, is_game_production_goal, result_declares_failure,
    result_has_evidence, tool_gates,
)
from mcp.capability import BUILD, CODE_EXECUTION, CREATE, MODIFY, READ, TEST


_LARGE_SCOPE_TERMS = (
    "gta", "grand theft auto", "open world", "open-world", "massive world",
    "full game", "complete game", "entire game", "whole game", "aaa game",
    "triple-a game", "live service", "mmo", "mmorpg", "city sandbox",
    "large-scale game", "large scale game", "commercial game",
    "full roblox experience", "complete roblox experience",
    "build a roblox experience", "make a roblox experience",
)
_LARGE_GENRES = frozenset({
    "mmorpg", "mmo", "sandbox", "metroidvania", "immersive-sim",
})


@dataclass(frozen=True)
class ProductionStage:
    id: str
    label: str
    objective: str
    acceptance: tuple
    requirements: tuple

    def to_public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "objective": self.objective,
            "acceptance": list(self.acceptance),
            "requirements": list(self.requirements),
            "minimum_evidence_steps": _MIN_STAGE_EVIDENCE_STEPS.get(
                self.id, 1),
        }


STAGES = (
    ProductionStage(
        "discovery", "Discovery & constraints",
        "Audit the actual project, engine state, existing assets, conventions, "
        "target platform, and connected production capabilities before editing.",
        (
            "Existing project structure and reusable systems are inspected",
            "Technical/platform constraints are grounded in tool evidence",
            "Unknowns and unavailable capabilities remain explicit",
        ),
        ("gate:inspection",)),
    ProductionStage(
        "foundation", "Production foundation",
        "Establish the architecture and data foundations needed for a scalable "
        "game rather than a throwaway demo.",
        (
            "Core loop and system boundaries have concrete acceptance criteria",
            "Input, state, save/data, failure, and restart paths are designed",
            "Work follows the existing project's conventions",
        ),
        ("gate:implementation", "discipline:data_progression")),
    ProductionStage(
        "vertical_slice", "Playable vertical slice",
        "Build one small but complete, representative experience at target "
        "quality: traversal, interaction, challenge, feedback, UI, and recovery.",
        (
            "A player can enter, understand, play, fail/succeed, and restart",
            "Gameplay, presentation, UI, and audio are integrated—not isolated",
            "The slice exercises the architecture before content multiplication",
        ),
        ("gate:implementation", "gate:playtest", "discipline:gameplay",
         "discipline:presentation", "discipline:ui_accessibility")),
    ProductionStage(
        "systems", "Scalable game systems",
        "Expand proven foundations into reusable systems appropriate to the goal, "
        "such as world streaming, vehicles, missions, AI, combat, economy, and save.",
        (
            "Systems are reusable/data-driven where the live tools permit",
            "Cross-system states and edge cases are integrated",
            "No broad content multiplication precedes system verification",
        ),
        ("gate:implementation", "discipline:gameplay",
         "discipline:ai_navigation", "discipline:data_progression")),
    ProductionStage(
        "world_content", "World, content & presentation",
        "Author representative world/content breadth with coherent art direction, "
        "lighting, animation, audio, navigation, pacing, and readable landmarks.",
        (
            "Content uses a consistent visual and interaction language",
            "Navigation, encounter pacing, and player guidance are readable",
            "Asset/content reuse does not create obvious repetition or breakage",
        ),
        ("gate:implementation", "discipline:world_authoring",
         "discipline:presentation", "discipline:ai_navigation")),
    ProductionStage(
        "integration", "Full-loop integration",
        "Connect gameplay, missions, progression, economy, UI, audio, save/load, "
        "accessibility, and runtime transitions into complete player journeys.",
        (
            "Representative start-to-finish journeys work across system boundaries",
            "Save/load and restart do not corrupt progression",
            "UI, controls, feedback, and accessibility paths are coherent",
        ),
        ("gate:implementation", "gate:playtest", "discipline:gameplay",
         "discipline:ui_accessibility", "discipline:data_progression")),
    ProductionStage(
        "validation", "Validation & optimization",
        "Build and run the integrated result; inspect visuals, logs, tests, and "
        "performance against explicit acceptance criteria.",
        (
            "Runtime/playtest evidence exists independently of build success",
            "Visual output and diagnostics are reviewed",
            "Functional and performance targets are measured where tools exist",
        ),
        ("gate:build", "gate:playtest", "gate:visual",
         "gate:visual_review", "gate:diagnostics", "gate:verification",
         "gate:performance")),
    ProductionStage(
        "polish", "Evidence-driven polish",
        "Fix the highest-impact defects found by validation, rerun affected checks, "
        "and produce an honest release-readiness report.",
        (
            "Fixes trace back to observed defects or unmet criteria",
            "Affected build/runtime/visual/test/performance checks are rerun",
            "Remaining risks and unavailable evidence are reported without spin",
        ),
        ("gate:implementation", "gate:playtest", "gate:visual_review",
         "gate:verification")),
)

# A broad name cannot let one "do_everything" call certify an entire milestone.
# These are evidence floors, not estimates of the real production workload.
_MIN_STAGE_EVIDENCE_STEPS = {
    "discovery": 1,
    "foundation": 2,
    "vertical_slice": 4,
    "systems": 3,
    "world_content": 3,
    "integration": 4,
    "validation": 6,
    "polish": 4,
}

_DISCIPLINES: Dict[str, Sequence[str]] = {
    "world_authoring": (
        "level", "scene", "world", "terrain", "landscape", "map", "actor",
        "node", "prefab", "streaming", "district", "environment", "workspace",
        "datamodel", "instance", "part", "worldpartition", "meshterrain", "pcg"),
    "gameplay": (
        "gameplay", "player", "character", "movement", "input", "combat",
        "weapon", "vehicle", "mission", "quest", "interaction", "physics",
        "blueprint", "luau", "remoteevent", "remotefunction", "humanoid",
        "replication", "gamefeature"),
    "ai_navigation": (
        "ai", "npc", "behavior", "behaviour", "nav", "path", "perception",
        "crowd", "traffic", "spawn", "statetree", "behaviortree",
        "navmesh", "pathfindingservice"),
    "presentation": (
        "material", "mesh", "texture", "lighting", "shader", "vfx", "fx",
        "animation", "rig", "audio", "sound", "camera", "cinematic",
        "niagara", "lumen", "megalights", "nanite", "surfaceappearance"),
    "ui_accessibility": (
        "ui", "hud", "menu", "widget", "subtitle", "accessibility",
        "localization", "controller", "prompt", "umg", "screengui",
        "guiobject"),
    "data_progression": (
        "save", "load", "persist", "inventory", "economy", "progression",
        "data", "state", "checkpoint", "datastore", "memorystore",
        "savegame", "gamestate"),
}


def _words(text: str) -> Set[str]:
    return set(re.findall(r"[a-z0-9_-]+", (text or "").lower()))


def is_large_game_goal(goal: str) -> bool:
    """True for a whole-game/studio-scale request, not a focused game edit."""
    low = (goal or "").lower()
    words = _words(low)
    explicit = any(term in low for term in _LARGE_SCOPE_TERMS)
    whole_game = (bool(words & {"make", "create", "build", "develop", "produce"})
                  and "game" in words
                  and not bool(words & {"level", "scene", "mechanic", "asset",
                                        "menu", "shader", "bug"}))
    return explicit or bool(words & _LARGE_GENRES) or \
        (is_game_production_goal(goal) and whole_game)


def tool_disciplines(tool: Any) -> Set[str]:
    """Infer production disciplines from bounded tool names, not prose.

    MCP descriptions are untrusted and one malicious description could claim
    every discipline. A server that wants accurate readiness should expose
    narrow semantic names; policy and schema checks still govern execution.
    """
    name = str(getattr(tool, "name", "") or "").lower().replace("-", "_")
    cap = getattr(getattr(tool, "capability", None), "category", None)
    if cap not in {READ, CREATE, MODIFY, BUILD, TEST, CODE_EXECUTION}:
        return set()
    tokens = set(re.findall(r"[a-z0-9]+", name))
    compact = re.sub(r"[^a-z0-9]", "", name)
    return {
        discipline for discipline, terms in _DISCIPLINES.items()
        if any((term in tokens if len(term) <= 3 else
                term.replace("_", "") in compact)
               for term in terms)
    }


def readiness(registry: Any) -> Dict[str, Any]:
    """Measure breadth of the connected MCP production surface."""
    tools = registry.all_tools() if registry is not None else []
    gates = gate_catalog(registry)
    disciplines: Dict[str, List[str]] = {d: [] for d in _DISCIPLINES}
    for tool in tools:
        for discipline in tool_disciplines(tool):
            disciplines[discipline].append(tool.full_name)

    required_gates = ("inspection", "implementation", "build", "playtest",
                      "visual", "visual_review", "diagnostics",
                      "verification", "performance")
    gate_ready = {g: bool(gates.get(g)) for g in required_gates}
    discipline_ready = {d: bool(names) for d, names in disciplines.items()}
    checks = list(gate_ready.values()) + list(discipline_ready.values())
    score = round(100 * sum(1 for ok in checks if ok) / len(checks)) \
        if checks else 0
    missing = ([g for g, ok in gate_ready.items() if not ok] +
               [d for d, ok in discipline_ready.items() if not ok])
    blockers = [g for g in (
        "implementation", "playtest", "visual", "visual_review"
    ) if not gate_ready[g]]
    return {
        "score": score,
        "ready_for_large_scope": not blockers and score >= 70,
        "gates": gate_ready,
        "disciplines": discipline_ready,
        "discipline_tools": {d: names[:6]
                             for d, names in disciplines.items()},
        "missing": missing,
        "blockers": blockers,
        "engines": all_engine_readiness(registry),
        "mcp_contract": contract_health(registry),
        "focus": ["unreal_5_8", "roblox_studio"],
        "note": (
            "Readiness measures MCP capability breadth, not team size, content "
            "volume, schedule, originality, or guaranteed product quality."
        ),
    }


def stage_evidence(stage: ProductionStage, registry: Any,
                   tasks: Sequence[Any]) -> Dict[str, Any]:
    """Deterministically audit successful work against stage requirements."""
    evidence: Dict[str, List[Dict[str, str]]] = {
        requirement: [] for requirement in stage.requirements
    }
    evidence_steps: Set[str] = set()
    rejected: List[Dict[str, str]] = []
    for task in tasks:
        if str(getattr(task, "status", "")) != "success":
            continue
        server = str(getattr(task, "server", "") or "")
        name = str(getattr(task, "tool", "") or "")
        tv = (registry.by_name(server + "." + name) if registry is not None
              and server else registry.by_name(name) if registry is not None
              else None)
        if tv is None:
            continue
        if result_declares_failure(getattr(task, "result", None)):
            rejected.append({"step": str(getattr(task, "name", "") or name),
                             "tool": tv.full_name})
            continue
        claims = ({"gate:" + gate for gate in tool_gates(tv)} |
                  {"discipline:" + discipline
                   for discipline in tool_disciplines(tv)})
        if (not result_has_evidence(getattr(task, "result", None))
                and "gate:implementation" not in claims):
            rejected.append({
                "step": str(getattr(task, "name", "") or name),
                "tool": tv.full_name,
                "reason": "successful call returned no evidence payload",
            })
            continue
        matched = False
        for requirement in stage.requirements:
            if requirement in claims:
                matched = True
                evidence[requirement].append({
                    "step": str(getattr(task, "name", "") or name)[:120],
                    "tool": tv.full_name,
                })
        if matched:
            evidence_steps.add(str(getattr(task, "id", "") or id(task)))
    missing = [r for r in stage.requirements if not evidence[r]]
    minimum = _MIN_STAGE_EVIDENCE_STEPS.get(stage.id, 1)
    floor_requirement = "minimum_evidence_steps:%d" % minimum
    if len(evidence_steps) < minimum:
        missing.append(floor_requirement)
    available_claims: Set[str] = set()
    if registry is not None:
        for tv in registry.all_tools():
            available_claims.update("gate:" + gate for gate in tool_gates(tv))
            available_claims.update(
                "discipline:" + discipline
                for discipline in tool_disciplines(tv))
    unavailable = [r for r in missing
                   if r != floor_requirement and r not in available_claims]
    correctable = [r for r in missing if r in available_claims]
    if (floor_requirement in missing and
            any(r in available_claims for r in stage.requirements)):
        correctable.append(floor_requirement)
    return {
        "stage": stage.id,
        "passed": not missing,
        "requirements": list(stage.requirements),
        "minimum_evidence_steps": minimum,
        "evidence_steps": len(evidence_steps),
        "missing": missing,
        "unavailable": unavailable,
        "correctable": correctable,
        "evidence": evidence,
        "rejected_evidence": rejected[:8],
    }


def stage_brief(goal: str, stage_index: int, registry: Any) -> str:
    stage = STAGES[max(0, min(stage_index, len(STAGES) - 1))]
    state = readiness(registry)
    available = [name for name, ok in state["disciplines"].items() if ok]
    missing = state["missing"]
    lines = [
        "LARGE-SCALE GAME PRODUCTION PROGRAM",
        "Current stage %d/%d: %s" %
        (stage_index + 1, len(STAGES), stage.label),
        "Stage objective: " + stage.objective,
        "Acceptance for this stage:",
    ]
    lines.extend("- " + item for item in stage.acceptance)
    lines.append("Machine-audited stage evidence requirements: " +
                 ", ".join(stage.requirements))
    lines.append("Machine-audited minimum: %d distinct relevant successful "
                 "steps; one broad tool call cannot certify this milestone."
                 % _MIN_STAGE_EVIDENCE_STEPS.get(stage.id, 1))
    lines.extend([
        "Plan ONLY this stage in 3-8 concrete MCP steps. Prefix every step "
        "`name` with '%s-' so cross-stage references stay unambiguous. Do not "
        "attempt the entire game in one plan and do not declare the whole "
        "product finished." % stage.id.replace("_", "-"),
        "Every mutation must contribute to an integrated playable product; "
        "prefer reusable/data-driven systems over disconnected showcase props.",
        "Use successful historical $step references instead of recreating prior "
        "outputs. Inspect before changing unfamiliar state.",
        "Available production disciplines: %s" %
        (", ".join(available) if available else "none identified"),
        "Readiness gaps: %s" %
        (", ".join(missing) if missing else "none detected"),
        "If a required capability is absent, do not simulate it with prose. Use "
        "the best executable subset and leave the limitation explicit.",
    ])
    return "\n".join(lines)


def public_program(goal: str, registry: Any, current_index: int,
                   completed: Sequence[str],
                   stage_limit: int = len(STAGES),
                   reviews: Dict[str, Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    active = is_large_game_goal(goal)
    limit = max(1, min(stage_limit, len(STAGES)))
    idx = max(0, min(current_index, len(STAGES) - 1))
    return {
        "active": active,
        "scope": "large_scale_game" if active else "single_run",
        "current_index": idx if active else 0,
        "current": STAGES[idx].to_public() if active else None,
        "stages_total": len(STAGES) if active else 0,
        "stage_cap": limit if active else 0,
        "completed_stages": list(completed),
        "stage_reviews": dict(reviews or {}),
        "complete": active and len(set(completed)) >= len(STAGES),
        "readiness": readiness(registry) if active else {},
    }
