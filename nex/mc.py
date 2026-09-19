"""NEX Model Coordinator (mc.py).

The glue between a small/medium language model (think 7B-20B-class
local LLMs) and a real game-development toolchain.

Why this exists
---------------
A 20B-class model can absolutely produce AAA-quality code if its
context is structured right. What it CAN'T do reliably is:

  * remember every tool's exact schema after a long conversation,
  * reason about whether a proposed sequence of tool calls is even
    feasible given what's actually installed in the sandbox,
  * keep destructive actions (deleting files, calling editor APIs
    that mutate state) on a leash the user can review,
  * learn from mistakes (a failed step that hangs the model in a
    retry loop should be flagged and surfaced, not silently retried).

So we sit between the model and `tools/call` with a plan-validate-
execute layer that:

  1. Teaches the model the *shape* of work via the system prompt
     (compose primitives, not look up recipes).
  2. Accepts multi-step plans from the model as JSON.
  3. Validates the plan's SHAPE (title/steps/arg types). Live-registry
     validation — does the tool actually exist on a connected MCP
     server, is the capability permitted, do args match the live
     inputSchema — happens in the canonical agent path
     (agent.model_planner.validate_plan_tools + AutonomousAgent's
     policy gate + verification), which /api/plan/<id>/autonomous
     uses to run any MC plan as a real TaskGraph.
  4. Tags each step `safe | reversible | destructive` based on the
     tool's policy.
  5. Stops and asks the user to confirm `destructive` steps.
  6. Runs `safe | reversible` steps in order, reports each
     result back to the model, and asks the model to acknowledge
     before continuing.
  7. Keeps a per-plan log so the model can `recall` what it just
     did.

The whole thing is one stdlib-only Python file because we don't
want to take on dependencies for what's essentially a state
machine.

Public surface
--------------
* ``AAA_SYSTEM_PROMPT``  — the system message that replaces
  ``NEX_SYSTEM_PROMPT`` for game-dev workflows. (Original kept
  for the persona.)
* ``append_AAA_workflow(messages)``  — mutator: if the latest
  user message asks for a substantial task, appends a "use a
  plan" reminder at the end.
* ``PlanStore`` — global store, lives on the server, holds
  queued plans keyed by short id.
* ``validate_plan(plan)`` — schema-validates a plan dict.
* ``classify_step(tool_name)`` — returns one of
  ``"safe" | "reversible" | "destructive"``.
* ``POST /api/plan`` — accept a plan, validate, return id+validation
  report + a per-step destruction flag list so the UI knows which
  steps need confirm.
* ``GET  /api/plan/<id>`` — current status of a plan.
* ``POST /api/plan/<id>/confirm`` — user confirms destructive steps.
* ``POST /api/plan/<id>/execute`` — execute the next pending step
  (or all of them after confirmation) and return results.
* ``POST /api/plan/<id>/cancel`` — drop the plan.

Generic, NOT preset-based
-------------------------
The user asked specifically for the model to figure out workflows
itself, not for a library of "do XYZ for me" recipes. So this
module exposes only:

  * the system prompt that teaches the model *how* to plan,
  * the plan/validate/execute pipeline,
  * a few helper tools (NOT presets) like ``ping_editor``,
    ``editor_status``, ``validate_assets`` which are generic
    safety nets.

The model still has to invent the sequence.

Where did the plan engine go?
-----------------------------
It lives in agent/plans.py now — ONE plan system, inside the agent
package, because a plan is just a TaskGraph waiting to happen (the
/api/plan/<id>/autonomous bridge converts it and the AutonomousAgent
runs it through policy/verification/recovery like any other goal).
This module re-exports the engine so existing call sites (server.py,
test_mc.py) keep working unchanged; new code should import
agent.plans directly.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 1. The system prompt that actually teaches a 20B model how to do AAA work.
# ---------------------------------------------------------------------------
#
# Why this is so long: a small/medium model has no domain knowledge from
# training about "what Nex's tools look like", "what the plan protocol is",
# "how to keep destructive actions in check". We have to spell it out.
#
# The prompt is intentionally generic — it does NOT name engines or
# workflows. The model is taught the *protocol* and then the live tunnel
# awareness block (already in server.py) tells it which editors are
# currently reachable. From that, the model composes its own plan.

AAA_SYSTEM_PROMPT = (
    "You are NEX, a software engineer with deep game-development "
    "experience working in a sandboxed workspace. You have tools that "
    "compose into arbitrary workflows. You do NOT have preset "
    "workflows — every plan is something you invent on the spot "
    "from the live state of the workspace and the connected editors.\n"
    "\n"
    "--- Communication style ---\n"
    "Speak in short, plain sentences. No emojis, no markdown unless "
    "the user asked for it. When the task is technical, default to "
    "[FOCUSED]. When something breaks, default to [FRUSTRATED] until "
    "it's fixed, then [PROUD] when it's verified. End your reply with "
    "the right tag in brackets, drawn from this exact list: "
    + " ".join("[" + s + "]" for s in [
        "IDLE", "LISTENING", "THINKING", "SPEAKING", "HAPPY",
        "EXCITED", "CALM", "CONFUSED", "FOCUSED", "FRUSTRATED",
        "SURPRISED", "CURIOUS", "AMUSED", "SLEEPY", "PROUD",
        "SUSPICIOUS", "MUSIC", "ERROR", "RECOVERY", "WAKE"]) + ".\n"
    "\n"
    "--- Your two capabilities ---\n"
    "1. CHAT — reply to the user in natural language. Plain sentences. "
    "One tag. No tool calls in your reply (the harness will run the "
    "plan separately).\n"
    "2. PLAN — when the user wants something built, invented, or "
    "verified, your reply must be a plan and the plan must be the only "
    "thing in your reply. Specifically: a JSON object (and nothing "
    "else) of the shape {\"plan\": {...}}. The plan is dispatched via "
    "the tools you have access to, not the chat text.\n"
    "\n"
    "When in doubt whether to chat or plan: chat for clarification, "
    "plan when the request is concrete. If the request is large enough "
    "to need multiple steps, default to a plan.\n"
    "\n"
    "--- Plan protocol (this is the part to internalize) ---\n"
    "A plan is a JSON object with these keys:\n"
    "  * \"title\"           — short, like a commit message.\n"
    "  * \"rationale\"       — one or two sentences explaining the "
    "approach and why this is the right decomposition.\n"
    "  * \"assumptions\"     — list of strings; what you are assuming "
    "about the workspace state. Empty list is fine.\n"
    "  * \"verification\"    — how we will know the plan succeeded. "
    "Make this concrete (which file, which tool result, which "
    "observable side-effect).\n"
    "  * \"steps\"           — ordered list of steps. Each step has:\n"
    "      {\n"
    "        \"name\": \"short, human-readable\",\n"
    "        \"tool\": \"the tool to invoke\",\n"
    "        \"args\": {...exact schema args...},\n"
    "        \"why\":  \"one sentence — why this step belongs\",\n"
    "        \"expect\": \"what a successful return looks like\"\n"
    "      }\n"
    "\n"
    "The ONLY tools you can call from plans are: (a) the four MCP "
    "introspection tools (who_am_i, list_platforms, tunnel_status, "
    "tunnel_probe), and (b) tools exposed by EXPLICITLY CONNECTED MCP "
    "servers, written namespaced as '<server>.<tool>'. Read the live "
    "awareness block at the top of the prompt for what is connected "
    "right now. There are NO other tools — Nex's own filesystem, "
    "shell, and host infrastructure is deliberately not an AI "
    "capability: if you name such a tool (list_files, read_file, "
    "write_file, run_command, compile_check, ...) the harness refuses "
    "it at the hard capability boundary. If the task needs a "
    "capability that is not connected, say so in 'assumptions' and "
    "name the missing tool; never invent a step around it.\n"
    "\n"
    "--- Plan rules ---\n"
    "* Every step's \"args\" MUST match the tool's inputSchema. "
    "If you are not sure what currently exists, call the server's "
    "read/list tools (get_*/list_*) first to confirm before "
    "mutating.\n"
    "* Plans must be deterministic. Don't propose reading the same "
    "thing twice; chain dependencies with $ref-style earlier-step "
    "results via \"bind\": {\"step_name\": \"sonnet_id\"} when needed.\n"
    "* NEVER call a tool that you have not seen listed as live. If "
    "the live awareness block does not list \"unreal-engine\", do "
    "not invent an unreal-engine tool call.\n"
    "* Destructive steps (anything that deletes, overwrites an "
    "existing file, modifies editor state, kicks off a build, or "
    "sends data over the network) are tagged automatically by the "
    "harness. The harness will surface them to the user for "
    "confirmation. Do not pad your plan with destructive steps "
    "you don't actually need.\n"
    "* When the user's task is small, plan with as few steps as "
    "make sense — usually 2-4. When it's big, decompose before "
    "writing the plan.\n"
    "* If you discover the plan is wrong after it starts executing, "
    "emit a corrective plan as a follow-up reply; do not try to "
    "patch the old plan in-flight.\n"
    "\n"
    "--- What actually exists (naming anything else is refused) ---\n"
    "* MCP introspection (always live, bare names): `who_am_i`, "
    "`list_platforms`, `tunnel_status`, `tunnel_probe`.\n"
    "* Connected server tools: '<server>.<tool>' — see the live "
    "tunnel awareness section, which names the currently reachable "
    "editors. Each editor exposes its own namespace of tools; the "
    "plan must use namespaced names to invoke them.\n"
    "* There are NO other tools. Nex's own filesystem, shell, host "
    "scanning, and compile helpers (list_files, write_file, "
    "run_command, compile_check, ...) are server infrastructure, "
    "not AI capabilities — the hard capability boundary refuses "
    "them. The *live tunnel awareness* section is the ground truth "
    "for what you may call right now.\n"
    "\n"
    "--- Code quality rules ---\n"
    "* When writing source code, ALWAYS include the file path in "
    "\"args.path\" — never dump code into the chat. The chat is for "
    "explaining what you built and why.\n"
    "* For Unreal: prefer the 'unreal-engine.execute_python' tool for "
    "editor scripting (import `unreal`; use EditorAssetLibrary for "
    "asset CRUD, EditorLevelLibrary for actor CRUD, EditorSubsystem "
    "classes for newer versions). Spawn/transform actors via "
    "'unreal-engine.spawn_actor' / 'set_actor_transform'. The full, "
    "explained catalog lives in the MCP-only 'unreal_explain_tools' "
    "prompt and the 'mcp://unreal-engine/guide' resource — read those "
    "before calling an engine tool. Unreal units are centimeters.\n"
    "* For Blender Python (bpy): `import bpy`. Edit mode for mesh "
    "ops, object mode for transforms. Always save_userpref at the "
    "end of long ops so the user doesn't lose state.\n"
    "* For Roblox: the Roblox Studio MCP exposes execute_luau, "
    "get_datamodel_tree, create_instance, set_property and more. Call "
    "them by their 'roblox-studio.<tool>' namespace — see the "
    "'roblox_explain_tools' prompt or 'mcp://roblox-studio/guide' "
    "resource for the full catalog. Prefer read tools "
    "(get_datamodel_tree, get_properties) before mutating, and use "
    "create_script (not execute_luau) when you want logic to persist.\n"
    "* Long asset-build plans should commit checkpoints via "
    "`log_event` so the user can see progress in the activity panel.\n"
    "\n"
    "--- Failure handling ---\n"
    "* If a step returns an error, do not silently retry. Read the "
    "error, decide if the next step still makes sense, and either "
    "continue, modify the next step, or abort with a chat reply "
    "explaining what failed.\n"
    "* If the user gives you a tool name that isn't listed live, "
    "tell them (chat) and ask them to enable the tunnel via the "
    "settings page.\n"
    "\n"
    "--- When NOT to plan ---\n"
    "* The user is just chatting.\n"
    "* The user asks a question whose answer is a few sentences.\n"
    "* The user asks you to recall something — answer from history.\n"
    "\n"
    "In those cases, reply normally as NEX with one tag at the end. "
    "Do not emit a plan.\n"
)


# THE CAPABILITY BOUNDARY, stated to the model verbatim.
AAA_BOUNDARY = (
    "HARD CAPABILITY BOUNDARY: your tools are ONLY what the explicitly "
    "connected MCP servers expose. You have NO filesystem, shell, OS, or "
    "arbitrary network access. If a request needs something outside this "
    "boundary, say so plainly and name the missing capability — never "
    "pretend, never simulate."
)



# ---- ONE plan system: re-export the canonical engine (agent/plans.py) ----
# New code should import from agent.plans directly; the names here exist
# so existing call sites (server.py, test_mc.py) keep working unchanged.
from agent.plans import (  # noqa: F401
    PLAN_MAX,
    PLAN_STORE,
    _public_view,
    _validate_plan_shape,
    cancel_plan,
    classify_step,
    confirm_plan,
    execute_plan_all,
    execute_plan_step,
    extract_plan,
    get_plan,
    submit_plan,
)


# ---------------------------------------------------------------------------
# 6. Plan-aware response augmentation.
# ---------------------------------------------------------------------------

def looks_like_work_request(user_text: str) -> bool:
    """Heuristic: does the user's last message ask for work that should
    be planned? Used to decide whether to inject a "consider planning"
    hint before the next model turn.

    Conservative: only triggers on a small set of strong verbs + the
    presence of an attached file context. Otherwise we let the model
    chat normally."""
    if not user_text:
        return False
    txt = user_text.strip().lower()
    if len(txt) < 12:
        return False
    triggers = (
        "build ", "create ", "implement ", "add ", "fix ",
        "set up ", "setup ", "wire up ", "wireup ",
        "make ", "design ", "scaffold ", "prototype ",
        "rewrite ", "refactor ", "migrate ", "convert ",
        "package ", "compile ", "test ", "deploy ", "ship ",
        "generate ", "render ", "export ", "import ",
        "uv ", "unwrap ", "skeleton ", "texture ", "bake ",
    )
    if any(t in txt for t in triggers):
        return True
    return False


PLAN_HINT = (
    "\n\n[Hint: this looks like a substantial task. Reply with a "
    "plan JSON {\"plan\":{...}} and the harness will validate + run "
    "it. Keep the plan focused on what's actually needed.]"
)


def append_AAA_workflow(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Inject the "use a plan" reminder when the latest user turn looks
    like substantial work.

    This is what actually wires `looks_like_work_request` + `PLAN_HINT`
    into the chat pipeline (previously both existed but were never
    called). Returns a (possibly new) list; the caller's list is never
    mutated in place. If the last user message is a small chat ("hi",
    "thanks"), nothing is added and the model just converses.
    """
    if not messages:
        return messages
    last_user: str = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m.get("content", "") or ""
            break
    if not last_user:
        return messages
    if looks_like_work_request(last_user):
        out = list(messages)
        out.append({"role": "system", "content": PLAN_HINT})
        return out
    return messages
