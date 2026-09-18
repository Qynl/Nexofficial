"""Deterministic skeleton planner — FALLBACK ONLY.

The CANONICAL planner is the model-driven one (agent.model_planner): when
an LLM is attached, AutonomousAgent._make_plan() asks the model for a
goal-specific plan and validates it against the live registry. This module
is the deterministic fallback used when no model is available or the model
returns nothing usable — so the agent still produces a dependency-aware
graph from the *discovered* capabilities.
It does NOT hardcode a game recipe — it reacts to whatever tools the live
servers expose. Each stage is gated on capability discovery:

    project -> asset -> material -> rig -> import -> verify_asset
              asset -> animation -> anim_controller
              project -> script
              project -> world -> lighting
              project -> audio
              project -> ui
              world/script -> ai
              build (depends on asset/import/script/animation/world/ui)
              run (build) -> inspect_logs -> verify_game

Stages with no discovered tool are simply skipped, so the same planner
drives a tiny prototype or a full AAA pipeline depending on what MCP
servers are connected. This is the "react to available MCP capabilities"
requirement: Nex can skip irrelevant stages, reorder independent ones,
and clearly report missing capabilities instead of pretending.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.registry import CapabilityRegistry, ToolView
from agent.task_graph import Task, TaskGraph


# Stage -> candidate tool names (first discovered wins). Additive: more
# patterns => the planner can build a richer graph when those tools exist.
_STAGE_TOOLS: Dict[str, List[str]] = {
    "project":        ["create_project", "new_project", "create_game"],
    "asset":          ["create_asset", "make_asset", "create_mesh", "create_model"],
    "material":       ["create_material", "make_material", "create_material_instance"],
    "rig":            ["create_rig", "generate_rig", "create_skeleton"],
    "import":         ["import_asset", "import"],
    "animation":      ["create_animation", "make_animation"],
    "anim_controller": ["create_animation_controller", "make_anim_blueprint",
                        "create_animator"],
    "script":         ["create_script", "make_script", "write_script"],
    "world":          ["create_world", "create_level", "generate_level",
                       "create_environment"],
    "lighting":       ["create_lighting", "setup_lights", "create_light_rig"],
    "audio":          ["create_audio", "import_audio", "create_sound"],
    "ui":             ["create_ui", "create_widget", "make_hud"],
    "ai":             ["create_ai", "create_behavior_tree", "make_ai_controller"],
    "build":          ["build", "compile", "package"],
    "run":            ["run_game", "playtest", "launch", "run"],
    "logs":           ["inspect_logs", "get_logs", "read_logs"],
    "verify_asset":   ["verify_asset", "validate_asset"],
    "verify_game":    ["verify_game", "validate_game"],
}

# Which stage verifies which (attach a verifier when discoverable).
_VERIFIERS = {
    "asset": "verify_asset",
    "import": "verify_asset",
    "build": "verify_game",
    "run": "verify_game",
}


def _resolve(registry: CapabilityRegistry, stage: str) -> Optional[ToolView]:
    for name in _STAGE_TOOLS.get(stage, []):
        tv = registry.by_name(name)
        if tv is not None:
            return tv
    return None


def plan(goal: str, registry: CapabilityRegistry) -> TaskGraph:
    """Build a dependency-aware task graph for `goal` from live capabilities."""
    graph = TaskGraph()
    resolved = {s: _resolve(registry, s) for s in _STAGE_TOOLS}
    ids: Dict[str, Optional[str]] = {}

    def add(stage: str, deps: List[str]) -> Optional[str]:
        tv = resolved.get(stage)
        if tv is None:
            ids[stage] = None
            return None
        tid = "stage_" + stage
        t = Task(id=tid, name=stage, stage=stage,
                 server=tv.server, tool=tv.name, args={}, deps=list(deps))
        graph.add(t)
        ids[stage] = tid
        return tid

    ids["project"] = add("project", [])
    ids["asset"] = add("asset", [ids["project"]] if ids["project"] else [])
    ids["material"] = add("material",
                          [d for d in (ids["asset"], ids["project"]) if d])
    ids["rig"] = add("rig", [d for d in (ids["asset"], ids["project"]) if d])
    ids["import"] = add("import", [ids["asset"]] if ids["asset"] else (
        [ids["project"]] if ids["project"] else []))
    ids["animation"] = add("animation",
                           [d for d in (ids["asset"], ids["rig"]) if d])
    ids["anim_controller"] = add("anim_controller",
                                 [ids["animation"]] if ids["animation"] else [])
    ids["script"] = add("script", [ids["project"]] if ids["project"] else [])
    ids["world"] = add("world", [ids["project"]] if ids["project"] else [])
    ids["lighting"] = add("lighting",
                          [d for d in (ids["world"], ids["asset"]) if d])
    ids["audio"] = add("audio", [ids["project"]] if ids["project"] else [])
    ids["ui"] = add("ui", [ids["project"]] if ids["project"] else [])
    ids["ai"] = add("ai", [d for d in (ids["world"], ids["script"]) if d])
    ids["build"] = add("build", [
        d for d in (ids["import"], ids["asset"], ids["script"],
                    ids["animation"], ids["world"], ids["ui"]) if d
    ])
    ids["run"] = add("run", [ids["build"]] if ids["build"] else [])
    ids["logs"] = add("logs", [ids["run"]] if ids["run"] else [])
    ids["verify_asset"] = add("verify_asset",
                              [ids["import"]] if ids["import"] else (
                                  [ids["asset"]] if ids["asset"] else []))
    ids["verify_game"] = add("verify_game",
                              [ids["logs"]] if ids["logs"] else (
                                  [ids["run"]] if ids["run"] else []))

    # Attach verifiers now that all tasks exist.
    for stage, verify_stage in _VERIFIERS.items():
        tid = ids.get(stage)
        vtv = resolved.get(verify_stage)
        if tid and vtv:
            t = graph.get(tid)
            if t:
                t.verify_tool = vtv.name

    return graph
