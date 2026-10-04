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
    requirements=(
        EngineRequirement(
            "project_inspection", "Project, assets, levels and plugins",
            ("uproject", "inspect_project", "project_info", "engine_version",
             "asset_registry", "content_browser", "world_outliner",
             "list_assets", "list_levels", "list_modules", "list_plugins",
             "project_settings"), True),
        EngineRequirement(
            "authoring", "Actors, Blueprints, C++ and content authoring",
            ("blueprint", "actor", "component", "level", "material", "niagara",
             "pcg", "mesh_terrain", "source_file", "cpp", "cxx"), True),
        EngineRequirement(
            "compile_build", "Blueprint compile, C++ build, cook or package",
            ("compile_blueprint", "compile_project", "build_project",
             "build_game", "ubt", "unrealbuildtool", "cook", "package",
             "commandlet"), True),
        EngineRequirement(
            "runtime", "Play In Editor or standalone runtime",
            ("start_pie", "play_in_editor", "pie_start", "launch_game",
             "run_game", "standalone_game", "simulate_game",
             "runtime_session"), True),
        EngineRequirement(
            "observation", "Viewport capture and Output Log diagnostics",
            ("viewport_capture", "capture_viewport", "capture_frame",
             "screenshot", "output_log", "message_log", "runtime_log",
             "inspect_logs", "get_diagnostics", "console_output"), True),
        EngineRequirement(
            "automation", "Automation or functional tests",
            ("automation_test", "functional_test", "gauntlet", "run_tests",
             "verify_game", "test_report", "session_frontend")),
        EngineRequirement(
            "performance", "Unreal Insights, trace or frame profiling",
            ("unreal_insights", "insights_trace", "trace_capture", "stat_unit",
             "stat_gpu", "profilegpu", "memreport", "profiler_capture",
             "get_performance_metrics", "performance_profile")),
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
