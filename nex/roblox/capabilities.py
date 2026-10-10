"""Granular Roblox capability model — proven, not assumed.

agent/engines.py's ROBLOX_STUDIO profile answers "which broad disciplines
does this MCP surface claim to cover" from tool-NAME keyword matching
alone. That is a reasonable first filter, but it conflates two very
different claims that this module deliberately keeps separate:

  * a tool whose name suggests a capability is CONNECTED
    ("create_remote_event" exists on a connected server), and
  * that capability has actually been EXERCISED with evidence this run
    ("a RemoteEvent was created AND a server script that could validate
    it was also touched" / "start_playtest succeeded AND an Output/
    screenshot observation also succeeded afterward").

A tool existing does not prove its capability works, and some capability
claims (REMOTE_VALIDATION, RUNTIME_PLAYTEST, REGRESSION_TESTING, ...)
cannot be substantiated by a single tool call at all — they need more
than one kind of evidence to co-occur in the SAME run. This module
defines exactly what evidence each capability needs and never upgrades
a capability's state past what that evidence actually shows.

Four states only, matching the brief's vocabulary exactly:
    AVAILABLE    tool(s) connected AND this run's evidence proves use
    PARTIAL      tool(s) connected but never actually exercised yet
    UNAVAILABLE  no connected tool resolves this capability at all
    UNKNOWN      no registry to even check (not connected to an engine)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

AVAILABLE = "AVAILABLE"
PARTIAL = "PARTIAL"
UNAVAILABLE = "UNAVAILABLE"
UNKNOWN = "UNKNOWN"

# Confidence mirrors the brief's architecture-memory vocabulary (point 13)
# so the whole codebase speaks one language about how sure a fact is.
CONFIRMED = "CONFIRMED"     # evidence collected THIS run
INFERRED = "INFERRED"       # tool(s) connected, never actually exercised
UNVERIFIED = "UNVERIFIED"   # nothing connected to even attempt this


@dataclass(frozen=True)
class CapabilityDef:
    id: str
    description: str
    # Tool-name keyword hints. ANY match against a connected tool's bare
    # name makes the capability's underlying tool(s) "present" (PARTIAL).
    required_tool_hints: Tuple[str, ...]
    # Supporting tool-name hints that strengthen the picture but are never
    # required on their own (e.g. a validator alongside an editor).
    optional_tool_hints: Tuple[str, ...] = ()
    # Each inner tuple is one REQUIRED kind of evidence: at least one
    # SUCCESSFUL call this run must match one of its keywords. ALL groups
    # must be satisfied for AVAILABLE. A capability with a single group
    # equal to required_tool_hints just needs one successful matching
    # call; a capability with two+ groups needs each kind of evidence
    # (e.g. "a playtest started" AND "something was observed") to exist
    # in the SAME run before it is called proven.
    proof_groups: Tuple[Tuple[str, ...], ...] = ()
    # Parallel to proof_groups: how many DISTINCT successful calls must
    # match that group's hints (default 1). Used for capabilities whose
    # proof is fundamentally about COUNT, not variety — e.g. "more than
    # one client session" or "a save AND a load", where a single call
    # must never be read as satisfying two different pieces of evidence.
    proof_group_min_calls: Tuple[int, ...] = ()
    note: str = ""


CAPABILITIES: Tuple[CapabilityDef, ...] = (
    CapabilityDef(
        "DATAMODEL_INSPECTION",
        "Reading the live DataModel: services, instance hierarchy, "
        "properties, attributes, tags",
        ("datamodel", "data_model", "inspect_project", "explorer_tree",
         "instance_tree", "list_instances", "list_services",
         "get_descendants", "place_info", "game_tree"),
    ),
    CapabilityDef(
        "LUAU_READ",
        "Reading existing Script/LocalScript/ModuleScript source",
        ("get_script_source", "read_script", "script_source",
         "get_source", "open_scripts"),
    ),
    CapabilityDef(
        "LUAU_EDIT",
        "Writing or modifying Luau source in place",
        ("set_script_source", "edit_script", "write_script",
         "update_script", "create_script", "module_script"),
    ),
    CapabilityDef(
        "LUAU_VALIDATION",
        "Static check / lint / type-check of Luau source before it runs",
        ("lint_luau", "validate_place", "static_analysis", "type_check",
         "luau_analyze", "script_analysis"),
        note=("Nex never invents a lint/type-check result. Without a "
             "connected validator, every Luau edit is UNVERIFIED until "
             "a runtime error or a real playtest proves otherwise."),
    ),
    CapabilityDef(
        "INSTANCE_AUTHORING",
        "Creating/editing Instances, terrain, parts, models",
        ("create_instance", "create_part", "insert_instance",
         "set_property", "terrain", "destroy_instance"),
    ),
    CapabilityDef(
        "UI_AUTHORING",
        "Building ScreenGui/Frame/TextLabel and other UI hierarchy",
        ("screen_gui", "screengui", "create_gui", "gui_object", "frame_",
         "text_label", "text_button", "image_label"),
    ),
    CapabilityDef(
        "SERVER_CLIENT_ARCHITECTURE",
        "Placing code correctly across ServerScriptService/"
        "ReplicatedStorage/StarterPlayerScripts boundaries",
        ("server_script", "serverscriptservice", "local_script",
         "replicated_storage", "replicatedstorage", "starterplayer"),
    ),
    CapabilityDef(
        "REMOTE_VALIDATION",
        "RemoteEvent/RemoteFunction request validated server-side, not "
        "merely created",
        ("remote_event", "remoteevent", "remote_function", "remotefunction"),
        optional_tool_hints=("server_script", "serverscriptservice"),
        proof_groups=(
            ("remote_event", "remoteevent", "remote_function",
             "remotefunction"),
            ("server_script", "serverscriptservice"),
        ),
        note=("A RemoteEvent/RemoteFunction existing alongside a touched "
             "server script is a heuristic co-occurrence signal, never "
             "proof the handler actually validates its arguments — that "
             "requires reading and understanding the Luau body, which is "
             "LLM/diagnosis work, not a deterministic fact."),
    ),
    CapabilityDef(
        "RUNTIME_PLAYTEST",
        "Starting a real Studio/standalone play session AND observing "
        "what happened in it",
        ("play_solo", "start_play", "run_playtest", "start_server",
         "start_local_server"),
        optional_tool_hints=("studio_output", "output_log", "screenshot"),
        proof_groups=(
            ("play_solo", "start_play", "run_playtest", "start_server",
             "start_local_server"),
            ("studio_output", "output_log", "inspect_logs", "screenshot",
             "viewport_capture", "console_output"),
        ),
        note=("Starting a session is not the same claim as observing it. "
             "AVAILABLE requires BOTH a session-start call and a "
             "separate observation call to have succeeded this run."),
    ),
    CapabilityDef(
        "MULTI_CLIENT_PLAYTEST",
        "Running more than one simultaneous client for multiplayer "
        "scenarios",
        ("start_client", "test_players", "run_service",
         "multi_client", "team_test"),
        proof_groups=(
            ("start_client", "test_players", "multi_client", "team_test"),
        ),
        proof_group_min_calls=(2,),
        note=("Proven only when this run's evidence shows TWO OR MORE "
             "distinct client-session calls succeeded; a single "
             "start_client call proves single-client runtime, not "
             "multi-client behaviour."),
    ),
    CapabilityDef(
        "VISUAL_CAPTURE",
        "Capturing a real viewport screenshot for before/after review",
        ("screenshot", "viewport_capture", "capture_frame", "snapshot"),
    ),
    CapabilityDef(
        "PERSISTENCE_TESTING",
        "Exercising DataStore/MemoryStore save+load round-trips",
        ("datastore", "data_store", "memorystore", "memory_store",
         "ordered_store", "profile_store"),
        proof_groups=(
            ("datastore", "data_store", "memorystore", "memory_store",
             "ordered_store", "profile_store"),
        ),
        proof_group_min_calls=(2,),
        note=("A single successful persistence call only proves a save OR "
             "a load happened, not a round-trip; AVAILABLE requires TWO "
             "OR MORE distinct successful persistence-flavored calls this "
             "run, approximating a save+load pair."),
    ),
    CapabilityDef(
        "PERFORMANCE_PROFILING",
        "MicroProfiler / script-profiler / frame-time measurement",
        ("microprofiler", "micro_profiler", "script_profiler",
         "performance_stats", "memory_stats", "frame_time", "frametime"),
    ),
    CapabilityDef(
        "REGRESSION_TESTING",
        "Running TestService or another automated Luau test suite",
        ("testservice", "test_service", "run_tests", "unit_test",
         "integration_test"),
    ),
    CapabilityDef(
        "PUBLISH_VERIFICATION",
        "Publishing a place and confirming the publish actually took",
        ("publish_place", "publish_game", "save_to_roblox", "upload_place"),
        note=("Publishing is a network-visible, policy-gated action (see "
             "mcp/policy.py) independent of this capability check; this "
             "only reports whether a publish-flavored tool is connected "
             "and was ever successfully called, never whether it should "
             "be."),
    ),
)

_BY_ID = {c.id: c for c in CAPABILITIES}


def _tool_names(registry: Any) -> List[str]:
    if registry is None:
        return []
    try:
        tools = registry.all_tools()
    except Exception:  # noqa: BLE001 - registry is a live/optional surface
        return []
    return [str(getattr(t, "name", "") or "").lower() for t in tools]


def _matches(names: Iterable[str], hints: Tuple[str, ...]) -> List[str]:
    if not hints:
        return []
    return [n for n in names if any(h in n for h in hints)]


def assess_capability(cap: CapabilityDef, registry: Any,
                      tasks: Iterable[Any]) -> Dict[str, Any]:
    """One capability's state, confidence, and the evidence behind it."""
    if registry is None:
        return {
            "id": cap.id, "description": cap.description,
            "state": UNKNOWN, "confidence": UNVERIFIED,
            "required_tools_present": [], "optional_tools_present": [],
            "proof_evidence": [], "note": cap.note,
        }
    names = _tool_names(registry)
    required_present = _matches(names, cap.required_tool_hints)
    optional_present = _matches(names, cap.optional_tool_hints)

    if not required_present:
        return {
            "id": cap.id, "description": cap.description,
            "state": UNAVAILABLE, "confidence": UNVERIFIED,
            "required_tools_present": [], "optional_tools_present": [],
            "proof_evidence": [], "note": cap.note,
        }

    successful_tools = [str(getattr(t, "tool", "") or "").lower()
                        for t in tasks
                        if getattr(t, "status", None) == "success"
                        and getattr(t, "tool", None)]
    groups = cap.proof_groups or (cap.required_tool_hints,)
    min_calls = cap.proof_group_min_calls or ((1,) * len(groups))
    proof_evidence: List[str] = []
    all_groups_satisfied = True
    for i, group in enumerate(groups):
        need = min_calls[i] if i < len(min_calls) else 1
        hits = [tool for tool in successful_tools
               if any(h in tool for h in group)]
        proof_evidence.extend(hits[:need])
        if len(hits) < need:
            all_groups_satisfied = False

    if all_groups_satisfied:
        state, confidence = AVAILABLE, CONFIRMED
    else:
        state, confidence = PARTIAL, INFERRED

    return {
        "id": cap.id, "description": cap.description,
        "state": state, "confidence": confidence,
        "required_tools_present": required_present[:8],
        "optional_tools_present": optional_present[:8],
        "proof_evidence": proof_evidence[:8],
        "note": cap.note,
    }


def capability_report(registry: Any, tasks: Iterable[Any] = ()
                      ) -> Dict[str, Dict[str, Any]]:
    """Every Roblox capability's state for this project right now."""
    tasks = list(tasks)
    return {cap.id: assess_capability(cap, registry, tasks)
           for cap in CAPABILITIES}


def capability(cap_id: str) -> Optional[CapabilityDef]:
    return _BY_ID.get(cap_id)
