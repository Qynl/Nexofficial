"""First-class production profiles for Unreal Engine 5.8 and Roblox Studio.

Profiles shape plans and measure the *live MCP surface*; they never add an
action path. Engine names, tool names, and successful MCP evidence remain the
only machine truth. Descriptions are intentionally excluded from matching
because MCP prose is untrusted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class EngineRequirement:
    id: str
    label: str
    terms: Tuple[str, ...]
    blocker: bool = False


@dataclass(frozen=True)
class EngineProfile:
    id: str
    label: str
    version: str
    aliases: Tuple[str, ...]
    signals: Tuple[str, ...]
    requirements: Tuple[EngineRequirement, ...]
    workflow: Tuple[str, ...]
    official_url: str

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "version": self.version,
            "requirements": [
                {"id": item.id, "label": item.label,
                 "blocker": item.blocker}
                for item in self.requirements
            ],
            "official_url": self.official_url,
        }


UNREAL_58 = EngineProfile(
    id="unreal_5_8",
    label="Unreal Engine 5.8",
    version="5.8",
    aliases=("unreal engine 5.8", "unreal 5.8", "ue 5.8", "ue5.8",
             "ue58", "unreal engine", "unreal"),
    signals=("unreal", "unreal_editor", "unrealeditor", "uproject",
             "blueprint", "start_pie", "pie_start", "unreal_insights",
             "world_outliner", "unrealbuildtool"),
    # This matrix is deliberately granular. Earlier, coarser requirement ids
    # (project_inspection, authoring, compile_build, runtime, observation,
    # automation, performance) remain exactly as before for compatibility;
    # the entries below them narrow each one into the actual disciplines an
    # Unreal MCP bridge may or may not expose, so readiness/stage planning
    # can tell "can create Blueprints" apart from "can also compile them",
    # or "can start PIE" apart from "can launch a standalone build" — the
    # planner must never assume a capability exists just because a
    # same-sounding sibling does.
    requirements=(
        EngineRequirement(
            "project_inspection", "Project, assets, levels and plugins",
            ("uproject", "inspect_project", "project_info", "engine_version",
             "asset_registry", "content_browser", "world_outliner",
             "list_assets", "list_levels", "list_modules", "list_plugins",
             "project_settings"), True),
        EngineRequirement(
            "plugin_inspection", "Enabled/available plugin discovery",
            ("list_plugins", "plugin_info", "enabled_plugins",
             "plugin_status", "plugin_browser")),
        EngineRequirement(
            "asset_inspection", "Asset registry browsing and metadata",
            ("asset_registry", "list_assets", "find_asset", "asset_info",
             "content_browser", "asset_metadata")),
        EngineRequirement(
            "authoring", "Actors, Blueprints, C++ and content authoring",
            ("blueprint", "actor", "component", "level", "material", "niagara",
             "pcg", "mesh_terrain", "source_file", "cpp", "cxx"), True),
        EngineRequirement(
            "blueprint_creation", "Creating new Blueprint classes/actors",
            ("create_blueprint", "new_blueprint", "add_blueprint",
             "blueprint_actor", "spawn_blueprint_class")),
        EngineRequirement(
            "blueprint_modification", "Editing existing Blueprint graphs",
            ("modify_blueprint", "edit_blueprint", "set_blueprint_property",
             "add_blueprint_node", "add_node", "blueprint_variable",
             "blueprint_function")),
        EngineRequirement(
            "blueprint_compilation", "Compiling a Blueprint after edits",
            ("compile_blueprint",)),
        EngineRequirement(
            "cpp_editing", "Writing or editing C++ source",
            ("source_file", "edit_source", "write_cpp", "cpp_class",
             "create_cpp_class", "edit_header", "edit_cpp")),
        EngineRequirement(
            "cpp_compilation", "Building a C++ module/target",
            ("compile_project", "build_project", "build_game", "ubt",
             "unrealbuildtool", "build_module", "hot_reload")),
        EngineRequirement(
            "actor_spawning", "Spawning/placing actors in a level",
            ("spawn_actor", "create_actor", "place_actor", "add_actor")),
        EngineRequirement(
            "component_editing", "Adding/editing actor components",
            ("add_component", "edit_component", "set_component",
             "remove_component", "component_property")),
        EngineRequirement(
            "world_editing", "Level/world editing (outliner, placement)",
            ("edit_level", "create_level", "load_level", "save_level",
             "world_outliner", "level_editing", "open_level")),
        EngineRequirement(
            "world_partition", "World Partition / data layers / streaming",
            ("world_partition", "data_layer", "streaming_source",
             "partition_grid", "one_file_per_actor")),
        EngineRequirement(
            "pcg", "Procedural Content Generation graphs",
            ("pcg", "procedural_content", "pcg_graph", "pcg_component")),
        EngineRequirement(
            "materials", "Material/material-instance authoring",
            ("material", "create_material", "edit_material",
             "material_instance", "material_graph")),
        EngineRequirement(
            "niagara", "Niagara VFX system authoring",
            ("niagara", "vfx_system", "particle_system",
             "niagara_emitter")),
        EngineRequirement(
            "animation", "Animation Blueprints, montages, Sequencer",
            ("animation", "anim_blueprint", "sequencer", "montage",
             "anim_graph", "skeletal_control")),
        EngineRequirement(
            "audio", "Sound cues, MetaSounds, audio placement",
            ("audio", "sound_cue", "metasound", "sound_wave",
             "audio_component")),
        EngineRequirement(
            "compile_build", "Blueprint compile, C++ build, cook or package",
            ("compile_blueprint", "compile_project", "build_project",
             "build_game", "ubt", "unrealbuildtool", "cook", "package",
             "commandlet"), True),
        EngineRequirement(
            "packaging", "Packaging a build for a target platform",
            ("package_project", "package_game", "package_build")),
        EngineRequirement(
            "cooking", "Cooking content for a target platform",
            ("cook_content", "cook_project", "cook_game")),
        EngineRequirement(
            "target_platform_builds", "Building for a non-editor target platform",
            ("target_platform", "build_target_platform", "platform_build",
             "build_cooked_content")),
        EngineRequirement(
            "runtime", "Play In Editor or standalone runtime",
            ("start_pie", "play_in_editor", "pie_start", "launch_game",
             "run_game", "standalone_game", "simulate_game",
             "runtime_session"), True),
        EngineRequirement(
            "pie", "Play In Editor specifically",
            ("start_pie", "play_in_editor", "pie_start", "stop_pie")),
        EngineRequirement(
            "standalone_launch", "Standalone (non-editor) game launch",
            ("launch_game", "run_game", "standalone_game",
             "standalone_launch")),
        EngineRequirement(
            "observation", "Viewport capture and Output Log diagnostics",
            ("viewport_capture", "capture_viewport", "capture_frame",
             "screenshot", "output_log", "message_log", "runtime_log",
             "inspect_logs", "get_diagnostics", "console_output"), True),
        EngineRequirement(
            "viewport_screenshots", "Capturing a viewport/frame image",
            ("viewport_capture", "capture_viewport", "capture_frame",
             "screenshot", "snapshot_view")),
        EngineRequirement(
            "logs", "Reading the Output/Message Log or crash reports",
            ("output_log", "message_log", "runtime_log", "inspect_logs",
             "get_diagnostics", "crash_report")),
        EngineRequirement(
            "console_commands", "Executing in-editor/runtime console commands",
            ("console_command", "execute_console_command", "exec_command",
             "run_console_command")),
        EngineRequirement(
            "automation", "Automation or functional tests",
            ("automation_test", "functional_test", "gauntlet", "run_tests",
             "verify_game", "test_report", "session_frontend")),
        EngineRequirement(
            "automation_tests", "Unreal Automation Spec/test framework",
            ("automation_test", "automation_spec", "run_automation_test")),
        EngineRequirement(
            "functional_tests", "Functional test actors/maps",
            ("functional_test", "functional_test_actor")),
        EngineRequirement(
            "gauntlet_tests", "Gauntlet-style device/build test runs",
            ("gauntlet", "gauntlet_test", "device_test")),
        EngineRequirement(
            "performance", "Unreal Insights, trace or frame profiling",
            ("unreal_insights", "insights_trace", "trace_capture", "stat_unit",
             "stat_gpu", "profilegpu", "memreport", "profiler_capture",
             "get_performance_metrics", "performance_profile")),
        EngineRequirement(
            "unreal_insights", "Unreal Insights trace capture/analysis",
            ("unreal_insights", "insights_trace", "trace_capture",
             "trace_analysis")),
        EngineRequirement(
            "profiling", "stat/profiler commands and frame/memory metrics",
            ("stat_unit", "stat_gpu", "profilegpu", "memreport",
             "profiler_capture", "get_performance_metrics")),
    ),
    workflow=(
        "Confirm the live project's EngineAssociation is 5.8 and inspect the "
        ".uproject, target platforms, enabled plugins, Source and Content before editing.",
        "Preserve the project's C++/Blueprint split, module boundaries, naming, "
        "asset paths and source-control conventions; never silently upgrade the project.",
        "Use UE 5.8 systems only when appropriate and enabled. Treat experimental "
        "features such as Mesh Terrain as opt-in, not automatic replacements.",
        "Compile every changed Blueprint and affected C++ target. A saved asset is "
        "not a successful compile, cook, or package.",
        "Run a real PIE/standalone gameplay path, inspect Output Log, capture the "
        "viewport, run automation, and profile with Unreal Insights where tools exist.",
    ),
    official_url=(
        "https://dev.epicgames.com/documentation/unreal-engine/"
        "unreal-engine-5-8-release-notes"),
)


ROBLOX_STUDIO = EngineProfile(
    id="roblox_studio",
    label="Roblox Studio",
    version="current Studio place format",
    aliases=("roblox studio", "roblox experience", "roblox game", "roblox",
             "luau", "rbxl", "rbxlx"),
    signals=("roblox", "roblox_studio", "datamodel", "create_part",
             "play_solo", "testservice", "replicatedstorage", "remoteevent",
             "microprofiler", "rbxl", "rbxlx"),
    requirements=(
        EngineRequirement(
            "datamodel_inspection", "DataModel, services and script ownership",
            ("datamodel", "data_model", "inspect_project", "explorer_tree",
             "instance_tree", "list_instances", "list_services",
             "get_descendants", "place_info", "game_tree"), True),
        EngineRequirement(
            "authoring", "Instances, terrain, UI and typed Luau authoring",
            ("create_instance", "create_part", "insert_instance", "set_property",
             "terrain", "screen_gui", "screengui", "module_script", "luau",
             "script_source", "create_script"), True),
        EngineRequirement(
            "client_server", "Client/server boundary and remote validation",
            ("remote_event", "remoteevent", "remote_function", "remotefunction",
             "server_script", "serverscriptservice", "local_script",
             "replicated_storage", "replicatedstorage", "network_test"), True),
        EngineRequirement(
            "runtime", "Studio play, local server and multi-client sessions",
            ("play_solo", "start_play", "run_playtest", "start_server",
             "start_local_server", "start_client", "test_players",
             "run_service"), True),
        EngineRequirement(
            "observation", "Viewport capture, Output and script analysis",
            ("screenshot", "viewport_capture", "capture_frame",
             "studio_output", "output_log", "inspect_logs", "script_analysis",
             "diagnostics", "console_output"), True),
        EngineRequirement(
            "persistence", "DataStore-safe persistence and migration checks",
            ("datastore", "data_store", "memorystore", "memory_store",
             "ordered_store", "profile_store", "persistence_test")),
        EngineRequirement(
            "performance_streaming", "MicroProfiler, device and streaming tests",
            ("microprofiler", "micro_profiler", "script_profiler",
             "performance_stats", "developer_console", "streaming_enabled",
             "streaming_test", "device_emulation", "memory_stats")),
        EngineRequirement(
            "verification", "TestService or automated Luau verification",
            ("testservice", "test_service", "run_tests", "unit_test",
             "integration_test", "validate_place", "lint_luau")),
    ),
    workflow=(
        "Inspect the live DataModel and existing service/module ownership before "
        "creating Instances or replacing scripts.",
        "Keep authoritative game state and validation on the server. Treat every "
        "client RemoteEvent/RemoteFunction argument as untrusted input.",
        "Prefer typed Luau modules, explicit lifecycle/cleanup, deterministic "
        "round resets and clear ServerScriptService/ReplicatedStorage boundaries.",
        "Test with Studio server plus multiple clients, not Play Solo alone; review "
        "Output and script analysis for both server and client failures.",
        "Use separate test data for DataStore work. Validate StreamingEnabled and "
        "baseline devices with MicroProfiler/Developer Console where tools exist.",
        "Saving a place and publishing are distinct. Publishing remains an explicit "
        "network action subject to MCP policy and operator approval.",
    ),
    official_url="https://create.roblox.com/docs/studio",
)


PROFILES: Tuple[EngineProfile, ...] = (UNREAL_58, ROBLOX_STUDIO)
_PROFILE_BY_ID = {profile.id: profile for profile in PROFILES}
_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_SAFE_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,160}\Z")


def _normalized(value: str) -> str:
    return " ".join(_TOKEN_RE.sub(" ", (value or "").lower()).split())


def _tool_name(tool: Any) -> str:
    return _normalized("%s %s %s" % (
        getattr(tool, "server", ""), getattr(tool, "name", ""),
        getattr(tool, "full_name", "")))


def _term_match(blob: str, term: str) -> bool:
    return (" " + _normalized(term) + " ") in (" " + blob + " ")


def _safe_full_name(tool: Any) -> str:
    """Return a prompt/UI-safe MCP identifier, or exclude it from evidence."""
    value = str(getattr(tool, "full_name", ""))
    return value if _SAFE_TOOL_NAME_RE.fullmatch(value) else ""


def detect_engine_targets(goal: str, registry: Any = None) -> List[str]:
    """Detect explicit targets, then cautiously infer from MCP namespaces."""
    goal_blob = " " + _normalized(goal) + " "
    targets = [profile.id for profile in PROFILES
               if any((" " + _normalized(alias) + " ") in goal_blob
                      for alias in profile.aliases)]
    if targets or registry is None:
        return targets
    try:
        blobs = [_tool_name(tool) for tool in registry.all_tools()
                 if _safe_full_name(tool)]
    except Exception:  # noqa: BLE001
        return []
    joined = " ".join(blobs)
    # Inference requires unmistakable namespace/file-format terms. Generic
    # words like actor, part, level, or script are intentionally insufficient.
    if any(_term_match(joined, term) for term in (
            "unreal", "unreal editor", "unrealeditor", "uproject",
            "unrealed", "unrealbuildtool")):
        targets.append(UNREAL_58.id)
    if any(_term_match(joined, term) for term in (
            "roblox", "datamodel", "rbxl", "replicatedstorage")):
        targets.append(ROBLOX_STUDIO.id)
    return targets


def profile(profile_id: str) -> EngineProfile:
    return _PROFILE_BY_ID[profile_id]


def profile_readiness(registry: Any, profile_id: str) -> Dict[str, Any]:
    target = profile(profile_id)
    discovered = registry.all_tools() if registry is not None else []
    safe_tools = [tool for tool in discovered if _safe_full_name(tool)]
    # Once one unmistakable signal identifies an engine server, its generic
    # names (inspect_project, capture_frame, run_game) may satisfy disciplines.
    # A generic tool on an unrelated server must not make both cards look ready.
    engine_servers = {
        getattr(tool, "server", "") for tool in safe_tools
        if any(_term_match(_tool_name(tool), signal)
               for signal in target.signals)
    }
    tools = [tool for tool in safe_tools
             if getattr(tool, "server", "") in engine_servers]
    evidence: Dict[str, List[str]] = {}
    for requirement in target.requirements:
        matches = [_safe_full_name(tool) for tool in tools
                   if any(_term_match(_tool_name(tool), term)
                          for term in requirement.terms)]
        evidence[requirement.id] = matches[:8]
    checks = {item.id: bool(evidence[item.id])
              for item in target.requirements}
    blockers = [item.id for item in target.requirements
                if item.blocker and not checks[item.id]]
    missing = [item.id for item in target.requirements if not checks[item.id]]
    score = round(100 * sum(1 for value in checks.values() if value)
                  / len(checks)) if checks else 0
    matched_names = {name for names in evidence.values() for name in names}
    matched_tools = [tool for tool in tools
                     if _safe_full_name(tool) in matched_names]
    input_typed = sum(
        1 for tool in matched_tools
        if isinstance(getattr(tool, "schema", None), dict)
        and getattr(tool, "schema").get("type") == "object")
    output_typed = sum(
        1 for tool in matched_tools
        if isinstance(getattr(tool, "output_schema", None), dict)
        and bool(getattr(tool, "output_schema")))
    contract_total = len(matched_tools)
    schema_health = {
        "score": round(100 * (input_typed + output_typed)
                       / (2 * contract_total)) if contract_total else 0,
        "matched_tools": contract_total,
        "input_schema_coverage": round(100 * input_typed / contract_total)
        if contract_total else 0,
        "output_schema_coverage": round(100 * output_typed / contract_total)
        if contract_total else 0,
    }
    return {
        **target.public(),
        "score": score,
        "ready": not blockers and score >= 70,
        "servers": sorted(str(name) for name in engine_servers if name),
        "schema_health": schema_health,
        "checks": checks,
        "evidence": evidence,
        "missing": missing,
        "blockers": blockers,
        "note": ("Readiness measures named live MCP capabilities, not whether "
                 "an engine project is open or a shipped game is high quality."),
    }


def all_engine_readiness(registry: Any) -> Dict[str, Dict[str, Any]]:
    return {target.id: profile_readiness(registry, target.id)
            for target in PROFILES}


def planning_brief(goal: str, registry: Any) -> str:
    """Engine-specific contract grounded in live MCP tool names."""
    targets = detect_engine_targets(goal, registry)
    if not targets:
        return ""
    blocks: List[str] = [
        "ENGINE-SPECIFIC PRODUCTION CONTRACT",
        "The following engine profile narrows workflow and acceptance criteria. "
        "It does not grant tools: every action still uses a live MCP tool.",
    ]
    if len(targets) > 1:
        blocks.append(
            "This request targets multiple engines. Keep project state, assets, "
            "runtime evidence and acceptance checks separate per engine; never "
            "send an Unreal identifier to Roblox or vice versa.")
    for target_id in targets:
        target = profile(target_id)
        state = profile_readiness(registry, target_id)
        blocks.extend([
            "",
            "TARGET: %s" % target.label,
            "Required workflow:",
        ])
        blocks.extend("- " + item for item in target.workflow)
        blocks.append("Live MCP coverage by engine discipline:")
        for requirement in target.requirements:
            names = state["evidence"].get(requirement.id) or []
            blocks.append("- %s: %s" % (
                requirement.label,
                "AVAILABLE via " + ", ".join(names[:4]) if names
                else "UNAVAILABLE — do not invent or simulate it"))
    blocks.append(
        "Use exact server.tool names from the live catalog. Engine documentation "
        "is guidance; MCP results are the only evidence that work happened. Do "
        "not invent or simulate unavailable editor, runtime, test, or ship actions.")
    return "\n".join(blocks)


def public_targets(goal: str, registry: Any) -> List[Dict[str, Any]]:
    return [profile_readiness(registry, target_id)
            for target_id in detect_engine_targets(goal, registry)]
