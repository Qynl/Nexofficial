"""NEX MCP engine adapters — curated Roblox Studio + Unreal Engine guides.

This module is PURELY about the Model Context Protocol surface for the two
game engines Nex binds to: **Roblox Studio** and **Unreal Engine**. It does
not touch the chat persona, the animation engine, the sandbox tools, or the
plan/validate pipeline. It is a documentation + adaptation layer whose only
job is to make the engine MCP tools *explain themselves* to the AI local
assistant (NEX).

Why this exists
---------------
When Roblox Studio or Unreal Engine connect over MCP, their ``tools/list`` is
forwarded more or less verbatim. Editors often ship terse, auto-generated
descriptions ("Run a script", "Spawn an actor"). A small/medium local model
can't reliably use a tool it doesn't understand. This module curates, for the
tools those editors typically expose, a model-friendly explanation:

  * what the tool does,
  * when to use it (and when NOT),
  * its parameters, with types + required flags + a one-line note each,
  * a copy-paste example call,
  * caveats (sandbox limits, read-only properties, destructive behavior),
  * "see also" cross-links to related tools.

It then *enriches* the live ``tools/list`` with that knowledge so the model
sees a great explanation instead of the thin upstream string.

Public surface
-------------
* ``SUPPORTED`` — canonical platform keys this adapter knows about.
* ``normalize_platform(name)`` — map "roblox"/"unreal" aliases to a key.
* ``find_guide(platform, tool_name)`` — match a tool to its curated guide.
* ``enrich_upstream_tools(platform, tool)`` — return a copy of the upstream
  tool dict with an upgraded ``description`` (MCP-only; safe to call on any
  tool; non-matching tools just get a generic safety note).
* ``explain_tool(platform, tool_name)`` — dict / markdown for one tool.
* ``explain_platform(platform)`` — dict for a whole platform.
* ``platform_guide_resource(platform)`` — markdown body for an MCP resource.
* ``platform_connect_help(platform)`` — how to enable MCP in that editor.
* ``build_recipe_prompt(platform, intent)`` — text for a "recipe" prompt.

Stdlib-only. Importable with no server running (so it is trivially testable).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Platform registry. Canonical keys match DEFAULT_TUNNELS in upstream.py:
#   "roblox-studio"  (Roblox Studio's official stdio MCP server)
#   "unreal-engine"  (Unreal Engine 5.8 experimental MCP plugin, /mcp)
# ---------------------------------------------------------------------------

SUPPORTED = ("roblox-studio", "unreal-engine")

_PLATFORM_ALIASES = {
    "roblox-studio": "roblox-studio",
    "roblox_studio": "roblox-studio",
    "roblox": "roblox-studio",
    "robloxstudio": "roblox-studio",
    "unreal-engine": "unreal-engine",
    "unreal_engine": "unreal-engine",
    "unreal": "unreal-engine",
    "unrealengine": "unreal-engine",
    "ue": "unreal-engine",
    "ue5": "unreal-engine",
}


def normalize_platform(name: str) -> Optional[str]:
    """Map a platform name/alias to a canonical key in SUPPORTED."""
    if not name:
        return None
    key = _PLATFORM_ALIASES.get(name.strip().lower())
    return key


def _norm(s: str) -> str:
    """Normalize a tool name for fuzzy matching: lowercase, alnum only."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---------------------------------------------------------------------------
# Curated guides. Each guide is keyed by canonical tool name with a list of
# aliases so it still matches when an editor names the tool slightly
# differently (execute_luau / run_luau / execute_script / ...).
# ---------------------------------------------------------------------------

_PLATFORM_GUIDES: Dict[str, Dict[str, Any]] = {
    "roblox-studio": {
        "label": "Roblox Studio (official MCP)",
        "connect": (
            "Roblox Studio's official MCP server is stdio-only. On Windows, "
            "Claude/Raycast/Cursor spawn `%LOCALAPPDATA%\\Roblox\\mcp.bat`; on "
            "macOS the binary is "
            "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP. Enable "
            "the 'MCP Server' beta feature in Studio > File > Beta Features, "
            "open a place, then connect. Nex registers this automatically as "
            "the 'roblox-studio' tunnel."
        ),
        "tools": [
            {
                "name": "execute_luau",
                "title": "execute_luau — run Luau in the open place",
                "aliases": ["run_luau", "execute_script", "run_script",
                            "eval_luau", "execute_code", "luau"],
                "purpose": (
                    "Execute Luau source inside Roblox Studio's Lua VM, in "
                    "the context of the currently open place. Best general "
                    "purpose tool: read the DataModel, query services, "
                    "mutate instances, and print diagnostics."
                ),
                "when_to_use": [
                    "Inspect the place: list Workspace children, read a "
                    "service's instances, count objects.",
                    "Mutate the place when you don't have a dedicated tool: "
                    "create/reparent instances, set properties, call methods.",
                    "Validate an idea quickly before writing a permanent "
                    "Script.",
                ],
                "params": [
                    {"name": "code", "type": "string", "required": True,
                     "note": "Luau source. Use 'workspace', 'game', "
                             "services like game:GetService('ServerStorage')."},
                    {"name": "sandbox", "type": "boolean", "required": False,
                     "note": "If the editor exposes it, run without side "
                             "effects (read-only). Prefer true for probes."},
                    {"name": "timeout", "type": "number", "required": False,
                     "note": "Max seconds before the snippet is aborted."},
                ],
                "example": (
                    'execute_luau(code="""\n'
                    "local ws = workspace\n"
                    'print(#ws:GetChildren(), "children in Workspace")\n'
                    'for _, v in ipairs(ws:GetChildren()) do\n'
                    '    print(v.ClassName, v.Name)\n'
                    "end\n"
                    '""")'
                ),
                "caveats": [
                    "Runs in Studio's Lua VM — errors abort the call and are "
                    "returned as the result, they do not crash Studio.",
                    "It does NOT create a Script object; code runs ad hoc. To "
                    "persist logic, use create_script instead.",
                    "Some APIs are server/client only; a place opened in edit "
                    "mode may not have RuntimeItems/DataStores available.",
                ],
                "see_also": ["create_script", "get_datamodel_tree",
                             "get_properties", "set_property",
                             "create_instance"],
            },
            {
                "name": "get_datamodel_tree",
                "title": "get_datamodel_tree — explore the instance hierarchy",
                "aliases": ["get_explorer", "datamodel_tree", "get_hierarchy",
                            "list_instances", "get_tree"],
                "purpose": (
                    "Return the instance tree beneath a root (default "
                    "Workspace or game) so you can understand scene "
                    "structure before acting on it."
                ),
                "when_to_use": [
                    "Before spawning/mutating: find the right parent and the "
                    "names of existing objects.",
                    "Answering 'what's in this place?' questions.",
                ],
                "params": [
                    {"name": "root", "type": "string", "required": False,
                     "note": "Path/ref like 'workspace' or "
                             "'game.ServerStorage'. Empty = whole DataModel."},
                    {"name": "depth", "type": "integer", "required": False,
                     "note": "How many levels deep to recurse. Keep small "
                             "(<=3) on big places."},
                    {"name": "class_filter", "type": "string",
                     "required": False,
                     "note": "Only include instances of this ClassName."},
                ],
                "example": 'get_datamodel_tree(root="workspace", depth=2)',
                "caveats": [
                    "Large places can return huge trees — always pass a "
                    "depth limit.",
                    "This is read-only and safe to call freely.",
                ],
                "see_also": ["execute_luau", "get_properties",
                             "find_actors" if False else "search",
                             "get_class_info"],
            },
            {
                "name": "create_script",
                "title": "create_script — add a Script/LocalScript/ModuleScript",
                "aliases": ["new_script", "add_script", "insert_script",
                            "create_module"],
                "purpose": (
                    "Create a new script object in the place and optionally "
                    "seed it with source. Use this to persist logic (unlike "
                    "execute_luau, which is ad hoc)."
                ),
                "when_to_use": [
                    "You have finished prototyping with execute_luau and want "
                    "the behavior to live in the place.",
                    "Scaffolding a ModuleScript library or a LocalScript.",
                ],
                "params": [
                    {"name": "className", "type": "string", "required": True,
                     "note": "One of 'Script', 'LocalScript', 'ModuleScript'."},
                    {"name": "parent", "type": "string", "required": False,
                     "note": "Target path/ref (e.g. 'workspace' or "
                             "'ServerScriptService')."},
                    {"name": "name", "type": "string", "required": False,
                     "note": "Instance Name; auto-generated if omitted."},
                    {"name": "source", "type": "string", "required": False,
                     "note": "Initial Luau source to write into the script."},
                ],
                "example": (
                    'create_script(className="ModuleScript", '
                    'parent="ReplicatedStorage", name="MathUtils", '
                    'source="local M = {}\\nfunction M.add(a,b) return a+b end'
                    '\\nreturn M")'
                ),
                "caveats": [
                    "Persists to the place — a destructive-ish (reviewable) "
                    "action; the plan harness will surface it for confirm.",
                    "Source is plain text; Roblox will compile it on save.",
                ],
                "see_also": ["execute_luau", "get_open_scripts", "get_script"],
            },
            {
                "name": "get_open_scripts",
                "title": "get_open_scripts — list scripts open in the editor",
                "aliases": ["list_scripts", "open_scripts", "list_open"],
                "purpose": (
                    "Return the scripts currently open in Studio's script "
                    "editor, with their refs/paths. Useful before reading or "
                    "editing a specific one."
                ),
                "when_to_use": ["Discovering which scripts you can read/edit.",
                                "Deciding where to put new logic."],
                "params": [
                    {"name": "include_source", "type": "boolean",
                     "required": False,
                     "note": "If true, include the source bodies (can be "
                             "large)."},
                ],
                "example": "get_open_scripts()",
                "caveats": ["Read-only.", "Paths are editor refs, not "
                            "filesystem paths."],
                "see_also": ["get_script", "create_script"],
            },
            {
                "name": "get_script",
                "title": "get_script — read a script's source",
                "aliases": ["read_script", "get_source", "read_source"],
                "purpose": "Fetch the Luau source of a script by ref/name.",
                "when_to_use": ["You need to read/modify an existing script.",
                                "Auditing logic before editing."],
                "params": [
                    {"name": "script", "type": "string", "required": True,
                     "note": "Ref or name of the script."},
                ],
                "example": 'get_script(script="ReplicatedStorage.MathUtils")',
                "caveats": ["Read-only.", "Combine with create_script or "
                            "execute_luau to change it."],
                "see_also": ["get_open_scripts", "create_script",
                             "execute_luau"],
            },
            {
                "name": "get_class_info",
                "title": "get_class_info — members of a Roblox class",
                "aliases": ["get_class", "class_info", "describe_class"],
                "purpose": (
                    "Return the properties, methods, and events of a Roblox "
                    "class so you know what you can set/call before you do."
                ),
                "when_to_use": [
                    "Before set_property — confirm the property name + type.",
                    "Discovering what an instance can do.",
                ],
                "params": [
                    {"name": "className", "type": "string", "required": True,
                     "note": "e.g. 'Part', 'BasePart', 'Model'."},
                ],
                "example": 'get_class_info(className="Part")',
                "caveats": ["Read-only.", "Inheritance is included — check "
                            "the parent class for shared members."],
                "see_also": ["get_properties", "set_property",
                             "create_instance"],
            },
            {
                "name": "get_properties",
                "title": "get_properties — read an instance's properties",
                "aliases": ["get_instance_properties", "read_properties"],
                "purpose": "Return the current property values of an instance.",
                "when_to_use": ["Inspecting an object before changing it.",
                                "Debugging 'why is X wrong?'."],
                "params": [
                    {"name": "instance", "type": "string", "required": True,
                     "note": "Ref/path of the target instance."},
                    {"name": "filter", "type": "array", "required": False,
                     "note": "Only return these property names."},
                ],
                "example": 'get_properties(instance="workspace.SpawnPad")',
                "caveats": ["Read-only.", "Some properties are computed and "
                            "not settable."],
                "see_also": ["set_property", "get_class_info"],
            },
            {
                "name": "set_property",
                "title": "set_property — change an instance property",
                "aliases": ["set_instance_property", "update_property"],
                "purpose": "Set a single property value on an instance.",
                "when_to_use": ["Tweaking a value you discovered via "
                                "get_properties.",
                                "Applying a fix without writing a full script."],
                "params": [
                    {"name": "instance", "type": "string", "required": True,
                     "note": "Ref/path of the target instance."},
                    {"name": "property", "type": "string", "required": True,
                     "note": "Property name (match get_class_info exactly)."},
                    {"name": "value", "type": "any", "required": True,
                     "note": "New value; type must match the property."},
                ],
                "example": ('set_property(instance="workspace.SpawnPad", '
                            'property="Anchored", value=true)'),
                "caveats": [
                    "Type must match the property or the call errors.",
                    "Some properties are read-only or script-only.",
                    "Persists to the place — reviewable/destructive.",
                ],
                "see_also": ["get_properties", "get_class_info",
                             "create_instance"],
            },
            {
                "name": "create_instance",
                "title": "create_instance — make a new Instance under a parent",
                "aliases": ["insert_instance", "create_object", "insert_object",
                            "spawn_instance"],
                "purpose": (
                    "Create an Instance of a ClassName under a parent, "
                    "optionally seeding properties. Higher-level than "
                    "execute_luau for simple construction."
                ),
                "when_to_use": ["Adding a Part, Folder, Model, etc. to the "
                                "place programmatically."],
                "params": [
                    {"name": "className", "type": "string", "required": True,
                     "note": "Roblox ClassName, e.g. 'Part', 'Model'."},
                    {"name": "parent", "type": "string", "required": True,
                     "note": "Parent ref/path (e.g. 'workspace')."},
                    {"name": "name", "type": "string", "required": False,
                     "note": "Instance Name."},
                    {"name": "properties", "type": "object",
                     "required": False,
                     "note": "Map of property name -> value to set at create."},
                ],
                "example": (
                    'create_instance(className="Part", parent="workspace", '
                    'name="SpawnPad", properties={"Anchored": true, '
                    '"Size": [4, 1, 4]})'
                ),
                "caveats": ["Persists to the place — reviewable/destructive.",
                            "Property values must match the class."],
                "see_also": ["set_property", "get_class_info",
                             "execute_luau"],
            },
            {
                "name": "search",
                "title": "search — find instances in the DataModel",
                "aliases": ["search_instances", "find", "find_instances",
                            "query"],
                "purpose": "Search the place for instances by name/class/text.",
                "when_to_use": ["Locating objects whose ref you don't know.",
                                "Bulk-discovery before a transform."],
                "params": [
                    {"name": "query", "type": "string", "required": False,
                     "note": "Substring to match against names."},
                    {"name": "class", "type": "string", "required": False,
                     "note": "Restrict to a ClassName."},
                    {"name": "root", "type": "string", "required": False,
                     "note": "Search subtree only."},
                    {"name": "limit", "type": "integer", "required": False,
                     "note": "Max results."},
                ],
                "example": 'search(query="Coin", class="Part", limit=20)',
                "caveats": ["Read-only.", "Use a limit on large places."],
                "see_also": ["get_datamodel_tree", "get_properties"],
            },
            {
                "name": "get_catalog_items",
                "title": "get_catalog_items — query the marketplace catalog",
                "aliases": ["catalog_search", "search_catalog"],
                "purpose": (
                    "Search the Roblox catalog (avatar items, models, etc.) "
                    "for assets you might insert or reference."
                ),
                "when_to_use": ["Finding a model/asset by keyword to "
                                "reference or purchase/insert."],
                "params": [
                    {"name": "search", "type": "string", "required": False,
                     "note": "Keyword."},
                    {"name": "category", "type": "string", "required": False,
                     "note": "Catalog category filter."},
                    {"name": "limit", "type": "integer", "required": False,
                     "note": "Max results."},
                ],
                "example": 'get_catalog_items(search="tree", limit=10)',
                "caveats": ["Read-only.", "Returns catalog metadata, not the "
                            "asset binary."],
                "see_also": ["search", "import_asset"],
            },
        ],
    },
    "unreal-engine": {
        "label": "Unreal Engine 5.8 (MCP plugin)",
        "connect": (
            "Unreal Engine 5.8 ships an experimental MCP plugin. Enable it "
            "under Editor Preferences / Project Settings -> MCP, which "
            "listens on http://127.0.0.1:3000/mcp. Restart the editor after "
            "enabling, open your project, then connect. Nex registers this "
            "automatically as the 'unreal-engine' tunnel. For UE 5.6 and "
            "below, run a stdio bridge (e.g. npx unreal-engine-mcp-server) and "
            "point Nex at it."
        ),
        "tools": [
            {
                "name": "spawn_actor",
                "title": "spawn_actor — add an actor to the current level",
                "aliases": ["create_actor", "add_actor", "spawn"],
                "purpose": (
                    "Spawn an actor of a given class into the open level at a "
                    "transform. The workhorse for building out a scene."
                ),
                "when_to_use": ["Placing lights, meshes, triggers, volumes.",
                                "Programmatically populating a level."],
                "params": [
                    {"name": "actor_class", "type": "string",
                     "required": True,
                     "note": "Unreal class, e.g. 'PointLight', "
                             "'StaticMeshActor', 'TriggerBox'."},
                    {"name": "location", "type": "array", "required": False,
                     "note": "[x, y, z] world coordinates (cm)."},
                    {"name": "rotation", "type": "array", "required": False,
                     "note": "[pitch, yaw, roll] degrees."},
                    {"name": "scale", "type": "array", "required": False,
                     "note": "[x, y, z]."},
                    {"name": "label", "type": "string", "required": False,
                     "note": "Friendly name / ActorLabel."},
                ],
                "example": (
                    'spawn_actor(actor_class="PointLight", '
                    'location=[0, 0, 200], label="KeyLight")'
                ),
                "caveats": [
                    "Persists to the level — reviewable/destructive.",
                    "Class must exist in the project; check get_class_info.",
                    "Coordinates are centimeters (Unreal's unit).",
                ],
                "see_also": ["delete_actor", "set_actor_transform",
                             "get_all_actors", "get_class_info"],
            },
            {
                "name": "delete_actor",
                "title": "delete_actor — remove an actor from the level",
                "aliases": ["destroy_actor", "remove_actor"],
                "purpose": "Delete an actor by name/path. Destructive.",
                "when_to_use": ["Cleaning up spawned/test actors.",
                                "Fixing a duplicated or misplaced object."],
                "params": [
                    {"name": "actor", "type": "string", "required": True,
                     "note": "Actor name or path returned by get_all_actors."},
                ],
                "example": 'delete_actor(actor="KeyLight")',
                "caveats": [
                    "DESTRUCTIVE — the plan harness will require confirmation.",
                    "Irreversible without an undo; prefer confirming with the "
                    "user first.",
                ],
                "see_also": ["spawn_actor", "get_all_actors"],
            },
            {
                "name": "get_actor_details",
                "title": "get_actor_details — inspect one actor",
                "aliases": ["get_actor", "describe_actor", "actor_info"],
                "purpose": "Return an actor's class, transform, components, "
                           "and key properties.",
                "when_to_use": ["Learning what an actor is before changing it.",
                                "Debugging transforms/proarenting."],
                "params": [
                    {"name": "actor", "type": "string", "required": True,
                     "note": "Actor name/path."},
                ],
                "example": 'get_actor_details(actor="KeyLight")',
                "caveats": ["Read-only."],
                "see_also": ["get_all_actors", "set_actor_transform"],
            },
            {
                "name": "get_all_actors",
                "title": "get_all_actors — list actors in the level",
                "aliases": ["list_actors", "get_actors"],
                "purpose": "Enumerate actors in the current level, optionally "
                           "filtered by class.",
                "when_to_use": ["Discovering what's in the level.",
                                "Finding an actor to act on."],
                "params": [
                    {"name": "class", "type": "string", "required": False,
                     "note": "Only return actors of this class."},
                    {"name": "world", "type": "string", "required": False,
                     "note": "World/level name if not the current one."},
                ],
                "example": 'get_all_actors(class="PointLight")',
                "caveats": ["Read-only.", "Large levels can return many "
                            "rows — filter by class."],
                "see_also": ["find_actors_by_name", "find_actors_by_class",
                             "get_actor_details"],
            },
            {
                "name": "set_actor_transform",
                "title": "set_actor_transform — move/rotate/scale an actor",
                "aliases": ["move_actor", "transform_actor", "set_transform"],
                "purpose": "Set an actor's location/rotation/scale.",
                "when_to_use": ["Repositioning after spawn.",
                                "Aligning objects programmatically."],
                "params": [
                    {"name": "actor", "type": "string", "required": True,
                     "note": "Actor name/path."},
                    {"name": "location", "type": "array", "required": False,
                     "note": "[x, y, z] cm."},
                    {"name": "rotation", "type": "array", "required": False,
                     "note": "[pitch, yaw, roll] degrees."},
                    {"name": "scale", "type": "array", "required": False,
                     "note": "[x, y, z]."},
                ],
                "example": (
                    'set_actor_transform(actor="KeyLight", '
                    'location=[100, 0, 250])'
                ),
                "caveats": ["Persists to the level — reviewable/destructive.",
                            "Don't fight physics; static actors snap, "
                            "simulating actors may be overridden."],
                "see_also": ["spawn_actor", "get_actor_details"],
            },
            {
                "name": "get_actor_transform",
                "title": "get_actor_transform — read an actor's placement",
                "aliases": ["get_transform"],
                "purpose": "Return an actor's location/rotation/scale.",
                "when_to_use": ["Reading current placement before adjusting."],
                "params": [
                    {"name": "actor", "type": "string", "required": True,
                     "note": "Actor name/path."},
                ],
                "example": 'get_actor_transform(actor="KeyLight")',
                "caveats": ["Read-only."],
                "see_also": ["set_actor_transform"],
            },
            {
                "name": "find_actors_by_name",
                "title": "find_actors_by_name — locate actors by name",
                "aliases": ["find_by_name", "search_actors"],
                "purpose": "Find actors whose name matches a pattern.",
                "when_to_use": ["You know part of an actor's name.",
                                "Bulk operations on similarly-named actors."],
                "params": [
                    {"name": "name", "type": "string", "required": True,
                     "note": "Substring or regex (editor-dependent)."},
                    {"name": "regex", "type": "boolean", "required": False,
                     "note": "Treat name as a regular expression."},
                ],
                "example": 'find_actors_by_name(name="Coin")',
                "caveats": ["Read-only."],
                "see_also": ["find_actors_by_class", "get_all_actors"],
            },
            {
                "name": "find_actors_by_class",
                "title": "find_actors_by_class — locate actors by class",
                "aliases": ["find_by_class"],
                "purpose": "Find all actors of a given class.",
                "when_to_use": ["Acting on every Light, every TriggerBox, etc."],
                "params": [
                    {"name": "class", "type": "string", "required": True,
                     "note": "Unreal class name."},
                ],
                "example": 'find_actors_by_class(class="TriggerBox")',
                "caveats": ["Read-only."],
                "see_also": ["find_actors_by_name", "get_all_actors"],
            },
            {
                "name": "create_blueprint",
                "title": "create_blueprint — author a new Blueprint class",
                "aliases": ["new_blueprint", "add_blueprint"],
                "purpose": "Create a Blueprint asset derived from a parent "
                           "class at a content path.",
                "when_to_use": ["Scaffolding reusable game objects.",
                                "Building a Blueprint you'll later compile/"
                                "populate."],
                "params": [
                    {"name": "name", "type": "string", "required": True,
                     "note": "Asset name."},
                    {"name": "parent_class", "type": "string",
                     "required": True,
                     "note": "Base class, e.g. 'Actor', 'Pawn', 'Character'."},
                    {"name": "path", "type": "string", "required": False,
                     "note": "Content path like '/Game/Blueprints'."},
                ],
                "example": (
                    'create_blueprint(name="BP_Coin", parent_class="Actor", '
                    'path="/Game/Blueprints")'
                ),
                "caveats": ["Persists an asset — reviewable/destructive.",
                            "Compile after adding components/logic."],
                "see_also": ["compile_blueprint", "add_component_to_blueprint"],
            },
            {
                "name": "compile_blueprint",
                "title": "compile_blueprint — compile a Blueprint",
                "aliases": ["compile"],
                "purpose": "Compile a Blueprint asset so changes take effect.",
                "when_to_use": ["After create_blueprint or adding components.",
                                "Before spawning an instance of it."],
                "params": [
                    {"name": "blueprint", "type": "string", "required": True,
                     "note": "Blueprint name/path."},
                ],
                "example": 'compile_blueprint(blueprint="/Game/Blueprints/BP_Coin")',
                "caveats": ["Compile errors are returned, not thrown away.",
                            "A failed compile means later spawns may fail."],
                "see_also": ["create_blueprint", "add_component_to_blueprint"],
            },
            {
                "name": "add_component_to_blueprint",
                "title": "add_component_to_blueprint — attach a component",
                "aliases": ["add_component"],
                "purpose": "Add a component (e.g. StaticMesh, BoxCollision) to "
                           "a Blueprint.",
                "when_to_use": ["Giving a Blueprint a mesh/collision/logic "
                                "component."],
                "params": [
                    {"name": "blueprint", "type": "string", "required": True,
                     "note": "Target Blueprint."},
                    {"name": "component_class", "type": "string",
                     "required": True,
                     "note": "Component class, e.g. 'StaticMeshComponent'."},
                    {"name": "name", "type": "string", "required": False,
                     "note": "Component name."},
                ],
                "example": (
                    'add_component_to_blueprint(blueprint="/Game/Blueprints/'
                    'BP_Coin", component_class="StaticMeshComponent", '
                    'name="Mesh")'
                ),
                "caveats": ["Persists — reviewable/destructive.", "Compile "
                            "afterwards to apply."],
                "see_also": ["create_blueprint", "compile_blueprint"],
            },
            {
                "name": "create_level",
                "title": "create_level — make a new level",
                "aliases": ["new_level"],
                "purpose": "Create a new (usually empty) level asset.",
                "when_to_use": ["Scaffolding a fresh map."],
                "params": [
                    {"name": "name", "type": "string", "required": True,
                     "note": "Level name."},
                    {"name": "path", "type": "string", "required": False,
                     "note": "Content path to save under."},
                ],
                "example": 'create_level(name="Arena", path="/Game/Maps")',
                "caveats": ["Persists an asset — reviewable/destructive."],
                "see_also": ["open_level", "save_current_level"],
            },
            {
                "name": "open_level",
                "title": "open_level — load a level",
                "aliases": ["load_level"],
                "purpose": "Open an existing level in the editor.",
                "when_to_use": ["Switching the editor to the map you'll edit."],
                "params": [
                    {"name": "path", "type": "string", "required": True,
                     "note": "Level asset path."},
                ],
                "example": 'open_level(path="/Game/Maps/Arena")',
                "caveats": ["Unsaved changes in the current level may be "
                            "prompted/lost — save first."],
                "see_also": ["save_current_level", "create_level"],
            },
            {
                "name": "save_current_level",
                "title": "save_current_level — persist the open level",
                "aliases": ["save_level"],
                "purpose": "Save the currently open level.",
                "when_to_use": ["After a batch of edits, before building.",
                                "Before the user disconnects."],
                "params": [],
                "example": "save_current_level()",
                "caveats": ["Writes to disk — cheap insurance."],
                "see_also": ["open_level", "build"],
            },
            {
                "name": "import_asset",
                "title": "import_asset — bring a file into the project",
                "aliases": ["import", "add_asset"],
                "purpose": "Import a file (FBX, texture, audio, etc.) into the "
                           "project's content.",
                "when_to_use": ["Adding a mesh, texture, or sound the user "
                                "provided."],
                "params": [
                    {"name": "file_path", "type": "string", "required": True,
                     "note": "Absolute path on the editor machine."},
                    {"name": "destination", "type": "string",
                     "required": False,
                     "note": "Content path to import into."},
                    {"name": "import_options", "type": "object",
                     "required": False,
                     "note": "Importer-specific options."},
                ],
                "example": (
                    'import_asset(file_path="C:/assets/tree.fbx", '
                    'destination="/Game/Meshes")'
                ),
                "caveats": ["Persists to the project — reviewable/destructive.",
                            "Path is on the editor's filesystem, not Nex's."],
                "see_also": ["create_blueprint", "get_class_info"],
            },
            {
                "name": "execute_python",
                "title": "execute_python — run Unreal Python in the editor",
                "aliases": ["run_python", "execute_unreal_python",
                            "run_unreal_python", "py"],
                "purpose": (
                    "Execute Unreal Python (editor scripting) in the editor "
                    "process. The most powerful tool: drive EditorAssetLibrary, "
                    "EditorLevelLibrary, automation, bulk asset ops, and "
                    "anything the Python API exposes."
                ),
                "when_to_use": [
                    "Bulk asset operations no single tool covers.",
                    "Reading project state via unrealEditorSubsystems.",
                    "Anything the dedicated tools don't expose.",
                ],
                "params": [
                    {"name": "code", "type": "string", "required": True,
                     "note": "Python source. Import 'unreal'; print() for "
                             "output."},
                    {"name": "timeout", "type": "number", "required": False,
                     "note": "Max seconds before abort."},
                ],
                "example": (
                    'execute_python(code="""\n'
                    "import unreal\n"
                    "lib = unreal.EditorAssetLibrary\n"
                    "assets = lib.list_assets('/Game', recursive=True)\n"
                    'print(len(assets), "assets under /Game")\n'
                    '""")'
                ),
                "caveats": [
                    "Runs in the editor Python — heavy ops BLOCK the editor "
                    "and the MCP call until they finish.",
                    "Can mutate the project; treat as reviewable/destructive.",
                    "Some APIs require the editor to be in a specific mode "
                    "(e.g. a level open).",
                ],
                "see_also": ["get_editor_state", "spawn_actor",
                             "import_asset"],
            },
            {
                "name": "get_editor_state",
                "title": "get_editor_state — editor/world snapshot",
                "aliases": ["editor_state", "get_editor_status",
                            "get_world"],
                "purpose": "Return editor + project state: open level, project "
                           "name, engine version, play state.",
                "when_to_use": ["Deciding what's actionable right now.",
                                "Diagnosing 'why did that fail?'."],
                "params": [],
                "example": "get_editor_state()",
                "caveats": ["Read-only."],
                "see_also": ["execute_python", "get_project"],
            },
            {
                "name": "get_project",
                "title": "get_project — project identity + paths",
                "aliases": ["get_project_info", "project_info"],
                "purpose": "Return the project name and key content paths.",
                "when_to_use": ["Building content paths for import/create.",
                                "Reporting which project is loaded."],
                "params": [],
                "example": "get_project()",
                "caveats": ["Read-only."],
                "see_also": ["get_editor_state"],
            },
            {
                "name": "build",
                "title": "build — build/compile the level",
                "aliases": ["build_geometry", "build_level", "build_actors"],
                "purpose": "Run a build step (geometry, lighting, etc.) on the "
                           "open level.",
                "when_to_use": ["After laying out geometry/lights, before a "
                                "play-test or package."],
                "params": [
                    {"name": "options", "type": "object", "required": False,
                     "note": "Which build types to run."},
                ],
                "example": "build(options={'geometry': true, 'lighting': true})",
                "caveats": ["Can be slow on large levels; runs in the editor.",
                            "Reviewable/destructive-ish (writes build data)."],
                "see_also": ["save_current_level", "get_editor_state"],
            },
        ],
    },
}


# Pre-build an alias index per platform for fast matching.
_ALIAS_INDEX: Dict[str, List[Dict[str, Any]]] = {}
for _plat, _data in _PLATFORM_GUIDES.items():
    _idx: List[Dict[str, Any]] = []
    for _g in _data["tools"]:
        _entry = {"guide": _g, "norm_name": _norm(_g["name"]),
                  "norm_aliases": [_norm(a) for a in _g.get("aliases", [])]}
        _idx.append(_entry)
    _ALIAS_INDEX[_plat] = _idx


def _strip_prefix(name: str) -> str:
    """Drop a leading namespace like 'roblox-studio.' or 'unreal.'."""
    if "." in name:
        return name.split(".", 1)[1]
    return name


def find_guide(platform: str,
               tool_name: str) -> Optional[Dict[str, Any]]:
    """Match a tool (canonical name or alias, with or without a platform
    prefix) to its curated guide. Returns the guide dict or None."""
    key = normalize_platform(platform)
    if key is None:
        return None
    inner = _strip_prefix(tool_name)
    n = _norm(inner)
    if not n:
        return None
    idx = _ALIAS_INDEX.get(key, [])
    # Pass 1: exact normalized match on name or alias.
    for entry in idx:
        if entry["norm_name"] == n or n in entry["norm_aliases"]:
            return entry["guide"]
    # Pass 2: substring containment (robust to extra suffixes).
    for entry in idx:
        if entry["norm_name"] and (entry["norm_name"] in n
                                   or n in entry["norm_name"]):
            return entry["guide"]
        for a in entry["norm_aliases"]:
            if a and (a in n or n in a):
                return entry["guide"]
    return None


def _render_guide_md(platform_label: str,
                     guide: Dict[str, Any]) -> str:
    """Render one guide as a compact markdown block."""
    lines: List[str] = []
    lines.append("### " + guide["title"])
    lines.append("")
    lines.append(guide["purpose"])
    if guide.get("when_to_use"):
        lines.append("")
        lines.append("**When to use**")
        for w in guide["when_to_use"]:
            lines.append("- " + w)
    if guide.get("params"):
        lines.append("")
        lines.append("**Parameters** (see the tool's inputSchema for exact "
                     "types/required flags)")
        for p in guide["params"]:
            req = "required" if p.get("required") else "optional"
            typ = p.get("type", "")
            lines.append("- `%s` (%s, %s): %s"
                         % (p.get("name", ""), typ, req, p.get("note", "")))
    if guide.get("example"):
        lines.append("")
        lines.append("**Example**")
        lines.append("```")
        lines.append(guide["example"])
        lines.append("```")
    if guide.get("caveats"):
        lines.append("")
        lines.append("**Caveats**")
        for c in guide["caveats"]:
            lines.append("- " + c)
    if guide.get("see_also"):
        lines.append("")
        lines.append("**See also:** " + ", ".join(guide["see_also"]))
    return "\n".join(lines)


def explain_tool(platform: str,
                 tool_name: str) -> Dict[str, Any]:
    """Return a structured explanation for one tool.

    Keys: platform, platform_label, tool, found, guide (dict or None),
    markdown (string)."""
    key = normalize_platform(platform)
    label = (_PLATFORM_GUIDES.get(key, {}).get("label")
             if key else platform)
    guide = find_guide(platform, tool_name) if key else None
    inner = _strip_prefix(tool_name)
    if guide is None:
        md = ("No curated guide for `%s` on %s yet. Inspect its inputSchema "
              "for parameters, and prefer read tools (get_*/list_*) before "
              "write tools." % (inner, label))
        return {"platform": key, "platform_label": label, "tool": inner,
                "found": False, "guide": None, "markdown": md}
    md = _render_guide_md(label, guide)
    return {"platform": key, "platform_label": label, "tool": inner,
            "found": True, "guide": guide, "markdown": md}


def platform_connect_help(platform: str) -> Optional[str]:
    """How to enable MCP in that editor. None if unknown platform."""
    key = normalize_platform(platform)
    if key is None:
        return None
    return _PLATFORM_GUIDES[key].get("connect")


def platform_guide_resource(platform: str) -> Optional[str]:
    """Markdown body for an MCP resource documenting a whole platform:

    how to connect + every tool guide. None if unknown platform."""
    key = normalize_platform(platform)
    if key is None:
        return None
    data = _PLATFORM_GUIDES[key]
    out: List[str] = []
    out.append("# " + data["label"] + " — MCP tool guide")
    out.append("")
    out.append("Curated by NEX so the AI local assistant can use this "
               "editor's MCP tools well. Tool names below are the bare "
               "names; over MCP they are namespaced as `%s.<tool>`." % key)
    out.append("")
    out.append("## How to connect")
    out.append("")
    out.append(data.get("connect", ""))
    out.append("")
    out.append("## Tools")
    out.append("")
    for g in data["tools"]:
        out.append(_render_guide_md(data["label"], g))
        out.append("")
    return "\n".join(out).strip() + "\n"


def enrich_upstream_tools(platform: str,
                          tool: Dict[str, Any]) -> Dict[str, Any]:
    """Return a COPY of an upstream tool dict with an upgraded ``description``.

    Matching tools get the curated guide prepended (so the most useful
    explanation survives any downstream truncation), followed by the
    editor's original description. Non-matching tools get a generic, safe
    usage note. The tool ``name`` and ``inputSchema`` are preserved.

    This is the MCP-only adaptation step — it never mutates the chat
    persona or sandbox tools.
    """
    name = tool.get("name", "") if isinstance(tool, dict) else ""
    original = ((tool or {}).get("description") or "").strip()
    label = (_PLATFORM_GUIDES.get(normalize_platform(platform), {})
             .get("label", platform)) if normalize_platform(platform) else platform
    guide = find_guide(platform, name)

    body: List[str] = []
    if guide is not None:
        body.append(_render_guide_md(label, guide))
        if original:
            body.append("")
            body.append("---")
            body.append("Upstream description: " + original)
    else:
        if original:
            body.append(original)
        else:
            body.append("[%s] tool" % label)
        body.append("")
        body.append("[NEX] Live tool exposed by %s. Inspect its inputSchema "
                    "for parameters. Prefer read tools (get_*/list_*) before "
                    "write tools, and confirm destructive steps with the user."
                    % label)

    new_desc = "\n".join(body).strip()
    return {**(tool or {}), "description": new_desc,
            "_nex_matched": guide is not None,
            "_nex_guide": (guide["title"] if guide else None)}


def explain_platform(platform: str) -> Dict[str, Any]:
    """Structured explanation of a whole platform (connect + tool list)."""
    key = normalize_platform(platform)
    if key is None:
        return {"platform": platform, "found": False,
                "tools": [], "connect": None}
    data = _PLATFORM_GUIDES[key]
    return {
        "platform": key,
        "platform_label": data["label"],
        "found": True,
        "connect": data.get("connect"),
        "tools": [{"name": g["name"],
                   "aliases": g.get("aliases", []),
                   "purpose": g["purpose"]} for g in data["tools"]],
    }


def build_recipe_prompt(platform: str, intent: str) -> str:
    """Turn a plain-English intent into a concrete engine tool call plan.

    Returned as text for an MCP ``prompts/get`` result. Pure MCP helper —
    it tells the model which namespaced tool to use and how to shape the
    arguments, without ever running anything itself.
    """
    key = normalize_platform(platform)
    if key is None:
        return ("Unknown platform '%s'. Supported: %s. Ask the user which "
                "editor they mean, or use list_platforms to see what's "
                "connected." % (platform, ", ".join(SUPPORTED)))
    label = _PLATFORM_GUIDES[key]["label"]
    prefix = key
    guide = platform_guide_resource(key) or ""
    return (
        "You are helping the user drive %s over MCP. The intent is:\n\n"
        "  \"%s\"\n\n"
        "Steps:\n"
        "1. Read the tool guide below to pick the right namespaced tool "
        "(call it as `%s.<tool_name>`).\n"
        "2. Prefer a read/probe tool first (e.g. get_datamodel_tree / "
        "get_all_actors / get_editor_state) to confirm the current state.\n"
        "3. Shape the arguments to match the tool's inputSchema exactly.\n"
        "4. If the chosen step is destructive (spawn/delete/set/write/"
        "import/create), emit it as a plan step so the user can confirm it.\n"
        "5. Return the concrete tool call(s) — name + arguments — and a "
        "one-line reason each.\n\n"
        "---\n%s" % (label, intent, prefix, guide)
    )
