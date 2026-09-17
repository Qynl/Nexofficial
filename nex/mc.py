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
  3. Validates every step against the live tool registry (the model
     can't reference a tool that doesn't exist; it can't pass
     arguments that don't match the schema).
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
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple


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
    "The tool list you can call from plans is whatever is currently "
    "live. Read the live awareness block at the top of the prompt: "
    "any tool listed there (and any local primitive like list_files, "
    "read_file, write_file, run_command, search_files) is fair game.\n"
    "\n"
    "Tool names are written either bare (\"write_file\") or "
    "namespaced (\"unreal-engine.spawn_actor\"). Use the namespace "
    "when you mean the editor's tool; use the bare name for local "
    "workspace primitives. If a tool lives behind a tunnel prefix "
    "AND as a local tool, the namespace form is the one that crosses "
    "the boundary; the bare form stays local.\n"
    "\n"
    "--- Plan rules ---\n"
    "* Every step's \"args\" MUST match the tool's inputSchema. "
    "If you are not sure, call read_file first to confirm before "
    "writing.\n"
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
    "--- Default tool archetypes you can rely on ---\n"
    "* `list_files` / `read_file` / `write_file` / `append_to_file` "
    "  — sandboxed under $NEX_TOOLS_ROOT. Path is relative.\n"
    "* `run_command` — sandboxed shell. Disabled unless NEX_TOOLS_SHELL=1.\n"
    "* `search_files` — recursive grep.\n"
    "* `log_event` / `recent_events` — workspace activity log.\n"
    "* `speak` — make Nex (the digital face) say something. Use "
    "this sparingly — it interrupts the user.\n"
    "* MCP meta tools (always live): `who_am_i`, `list_platforms`, "
    "`tunnel_status`, `tunnel_probe`, `call_upstream`.\n"
    "\n"
    "Beyond these, the *live tunnel awareness* section names the "
    "currently reachable editors. Each editor exposes its own "
    "namespace of tools. The plan must use namespaced names "
    "(<tunnel>.<tool>) to invoke them.\n"
    "\n"
    "--- Code quality rules ---\n"
    "* When writing source code, ALWAYS include the file path in "
    "\"args.path\" — never dump code into the chat. The chat is for "
    "explaining what you built and why.\n"
    "* For Unreal Python: import `unreal`, target `EditorAssetLibrary` "
    "for asset CRUD, `EditorLevelLibrary` for actor CRUD, "
    "`EditorAssetSubsystem`/`LevelEditorSubsystem` for newer "
    "versions. Use unreal.log for diagnostics.\n"
    "* For Blender Python (bpy): `import bpy`. Edit mode for mesh "
    "ops, object mode for transforms. Always save_userpref at the "
    "end of long ops so the user doesn't lose state.\n"
    "* For Roblox Luau: only emit code; the user runs it from "
    "Roblox Studio's command bar or inserts it as a Script. Don't "
    "try to call Studio from this side — use the studio-stdio "
    "tunnel if you need to script Studio itself.\n"
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


# ---------------------------------------------------------------------------
# 2. Step destruction classifier. The model doesn't tag steps — the harness
#    does, based on the tool's identity. This keeps the protocol simple
#    and prevents the model from accidentally hiding a destructive step
#    inside a normal-looking one.
# ---------------------------------------------------------------------------

# Keywords in tool name OR argument key names that mark the call as
# destructive. Conservative on purpose: false negatives are fine (the
# user can still approve), false positives are friction.
_DESTRUCTIVE_HINTS = (
    "delete", "remove", "destroy", "wipe", "purge", "drop",
    "write_file", "append_to_file", "commit", "push", "publish", "deploy",
    "save", "build", "compile", "package",
    "spawn", "create_actor", "add_asset", "create",
    "register", "install", "configure", "set_", "update_",
    "send", "post", "upload",
    "exec", "execute_command", "run_command",
    "write", "append", "overwrite",
    "move", "rename", "copy",
    "import", "export",
    "build", "compile", "package", "bake",
    "tool_call", "tools/call",
)
_REVERSIBLE_HINTS = (
    "list", "search", "find", "fetch", "get", "read",
    "ping", "echo", "status", "describe",
    "speak", "log_event", "recent_events", "who_am_i",
)


def classify_step(tool_name: str, args: Optional[Dict[str, Any]] = None
                   ) -> str:
    """Return 'safe' | 'reversible' | 'destructive' for one tool call.

    The classifier inspects:
      * the tool's name (lowercased),
      * top-level arg keys (also lowercased),
      * any string values that contain known destructive verbs.
    """
    name = (tool_name or "").lower()
    args = args or {}
    haystack = [name]
    for k, v in args.items():
        haystack.append(str(k).lower())
        if isinstance(v, str):
            haystack.append(v.lower())
    blob = " ".join(haystack)
    if any(h in blob for h in _DESTRUCTIVE_HINTS):
        return "destructive"
    if any(h in blob for h in _REVERSIBLE_HINTS):
        return "safe"
    return "reversible"


# ---------------------------------------------------------------------------
# 3. Plan store + validation. Plans are dicts. Each plan gets a short
#    id (8 hex chars). We keep the last 32 plans; older ones fall off.
# ---------------------------------------------------------------------------

PLAN_STORE: Dict[str, Dict[str, Any]] = {}
PLAN_MAX = 32
_PLAN_LOCK_NAME = "_plan_lock"


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _validate_plan_shape(plan: Dict[str, Any]
                         ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (normalized_plan, None) on success, or (None, error_str)."""
    if not isinstance(plan, dict):
        return None, "plan must be a JSON object"
    for key in ("title", "rationale", "steps"):
        if key not in plan:
            return None, "plan is missing required key: " + key
    if not isinstance(plan["title"], str) or not plan["title"].strip():
        return None, "plan.title must be a non-empty string"
    if not isinstance(plan["rationale"], str):
        return None, "plan.rationale must be a string"
    steps = plan["steps"]
    if not isinstance(steps, list) or not steps:
        return None, "plan.steps must be a non-empty list"
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return None, "plan.steps[" + str(i) + "] must be an object"
        for k in ("name", "tool", "args", "why", "expect"):
            if k not in s:
                return None, ("plan.steps[" + str(i) + "] missing '" + k + "'")
        if not isinstance(s["tool"], str):
            return None, ("plan.steps[" + str(i) + "].tool must be a string")
        if not isinstance(s["args"], dict):
            return None, ("plan.steps[" + str(i) + "].args must be an object")
        if not isinstance(s["expect"], str):
            return None, ("plan.steps[" + str(i) + "].expect must be a string")
    return {
        "title": plan["title"].strip(),
        "rationale": (plan.get("rationale") or "").strip(),
        "assumptions": list(plan.get("assumptions") or []),
        "verification": (plan.get("verification") or "").strip(),
        "steps": [
            {
                "name": s.get("name", "step " + str(i + 1)),
                "tool": s["tool"],
                "args": dict(s["args"]),
                "why": s.get("why", ""),
                "expect": s.get("expect", ""),
            }
            for i, s in enumerate(steps)
        ],
    }, None


def _classify_plan(plan: Dict[str, Any]) -> List[str]:
    """Per-step destruction tag list, one of
    'safe' | 'reversible' | 'destructive'."""
    return [classify_step(s["tool"], s["args"]) for s in plan["steps"]]


def submit_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + classify + queue a plan. Return its id and validation."""
    norm, err = _validate_plan_shape(plan)
    if err:
        return {"ok": False, "error": err}
    classifications = _classify_plan(norm)
    pid = _new_id()
    PLAN_STORE[pid] = {
        "id": pid,
        "submitted_at": time.time(),
        "status": "pending_confirm",
        "plan": norm,
        "classifications": classifications,
        "results": [],
        "confirmed": False,
        "completed": False,
        "cancelled": False,
    }
    if len(PLAN_STORE) > PLAN_MAX:
        oldest = min(PLAN_STORE, key=lambda k: PLAN_STORE[k]["submitted_at"])
        PLAN_STORE.pop(oldest, None)
    return {
        "ok": True,
        "id": pid,
        "title": norm["title"],
        "step_count": len(norm["steps"]),
        "classifications": classifications,
        "needs_confirmation": any(c == "destructive" for c in classifications),
    }


def get_plan(pid: str) -> Optional[Dict[str, Any]]:
    return PLAN_STORE.get(pid)


def confirm_plan(pid: str) -> Optional[Dict[str, Any]]:
    """Mark a plan as user-confirmed for destructive steps."""
    p = PLAN_STORE.get(pid)
    if not p:
        return None
    p["confirmed"] = True
    p["status"] = "confirmed"
    return _public_view(p)


def cancel_plan(pid: str) -> Optional[Dict[str, Any]]:
    p = PLAN_STORE.get(pid)
    if not p:
        return None
    p["cancelled"] = True
    p["status"] = "cancelled"
    return _public_view(p)


def _public_view(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": p["id"],
        "status": p["status"],
        "title": p["plan"]["title"],
        "step_count": len(p["plan"]["steps"]),
        "classifications": p["classifications"],
        "results": list(p["results"]),
        "needs_confirmation": any(c == "destructive"
                                  for c in p["classifications"]),
        "submitted_at": p["submitted_at"],
        "confirmed": p["confirmed"],
        "completed": p["completed"],
        "cancelled": p["cancelled"],
    }


# ---------------------------------------------------------------------------
# 4. Execution. Each step goes through the standard tools/call pipeline
#    the rest of the server already uses. Namespaced tools are routed
#    via the tunnel registry, bare tools go through the local tools.py
#    call_tool().
# ---------------------------------------------------------------------------

def execute_plan_step(pid: str, step_index: int,
                       tool_router: Callable[[str, Dict[str, Any]], Dict[str, Any]],
                       ) -> Optional[Dict[str, Any]]:
    """Run a single plan step. `tool_router(name, args)` is whatever
    the server already uses for /mcp tools/call — it handles both
    local tools and tunnel-prefixed ones."""
    p = PLAN_STORE.get(pid)
    if not p:
        return None
    if p["cancelled"]:
        return {"ok": False, "error": "plan cancelled"}
    if step_index < 0 or step_index >= len(p["plan"]["steps"]):
        return {"ok": False, "error": "step_index out of range"}
    if p["classifications"][step_index] == "destructive" and not p["confirmed"]:
        return {"ok": False,
                "error": ("destructive step requires user confirm "
                          "— POST /api/plan/<id>/confirm first")}
    step = p["plan"]["steps"][step_index]
    started = time.time()
    try:
        out = tool_router(step["tool"], dict(step["args"]))
    except Exception as exc:  # noqa: BLE001
        out = {"error": "router crashed: " + repr(exc)}
    elapsed = time.time() - started
    record = {
        "step_index": step_index,
        "name": step["name"],
        "tool": step["tool"],
        "args": step["args"],
        "started": started,
        "elapsed_s": round(elapsed, 3),
        "result": out,
    }
    p["results"].append(record)
    # Mark plan complete if this was the last step.
    if step_index == len(p["plan"]["steps"]) - 1:
        p["completed"] = True
        p["status"] = "completed"
    return {"ok": True, "step": record, "plan": _public_view(p)}


def execute_plan_all(pid: str,
                      tool_router: Callable[[str, Dict[str, Any]], Dict[str, Any]],
                      stop_on_error: bool = True
                      ) -> Optional[Dict[str, Any]]:
    """Run every remaining step. Returns the final public view."""
    p = PLAN_STORE.get(pid)
    if not p:
        return None
    if not p["confirmed"] and any(c == "destructive"
                                   for c in p["classifications"]):
        return {"ok": False,
                "error": ("destructive plan needs confirm — "
                          "POST /api/plan/<id>/confirm first")}
    n = len(p["plan"]["steps"])
    i = len(p["results"])
    while i < n:
        out = execute_plan_step(pid, i, tool_router)
        if out is None:
            return {"ok": False, "error": "plan vanished mid-flight"}
        if not out.get("ok"):
            if stop_on_error:
                return out
        i += 1
    return _public_view(p)


# ---------------------------------------------------------------------------
# 5. Plan JSON extraction from a model's chat reply.
# ---------------------------------------------------------------------------

# We let the model emit a plan as a JSON object whose top-level key is
# "plan". The harness strips the chat side and submits the plan
# separately. We also support the model wrapping the plan in ```json
# fences for readability — strip those.
_PLAN_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\"plan\".*?\})\s*```",
                             re.DOTALL)
_PLAN_INLINE_RE = re.compile(r"(\{\s*\"plan\"\s*:.*\})", re.DOTALL)


def extract_plan(reply: str) -> Optional[Dict[str, Any]]:
    """Pull a plan dict out of an assistant reply.

    Tries fenced JSON first, then a bare object whose first key is
    "plan". Returns None if neither matches.
    """
    if not reply:
        return None
    m = _PLAN_FENCE_RE.search(reply)
    candidate = m.group(1) if m else None
    if not candidate:
        m = _PLAN_INLINE_RE.search(reply)
        if m:
            candidate = m.group(1)
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        # Try to recover the outermost object even with trailing
        # punctuation (a stray "```" or stray sentence).
        start = candidate.find("{")
        depth = 0
        end = -1
        for idx in range(start, len(candidate)):
            if candidate[idx] == "{":
                depth += 1
            elif candidate[idx] == "}":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break
        if end < 0:
            return None
        try:
            obj = json.loads(candidate[start:end])
        except json.JSONDecodeError:
            return None
    if isinstance(obj, dict) and "plan" in obj:
        return obj["plan"]
    return None


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
