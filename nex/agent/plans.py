"""The plan engine — canonical home (agent/plans.py).

ONE plan system for Nex. This module owns everything about model-emitted
plans: shape validation, step classification, the PlanStore lifecycle
(pending_confirm -> confirmed -> completed / cancelled), the sequential
step executor ("step mode"), and plan extraction from model replies.

It lives in agent/ because plans are NOT a separate architecture: a plan
becomes a TaskGraph and runs through the same AutonomousAgent pipeline as
every other goal (see server.py's /api/plan/<id>/autonomous bridge and
agent.model_planner.validate_plan_deep / plan_to_graph). The sequential
executor here is the explicit manual "step mode" fallback for stepping
through a plan one call at a time.

mc.py re-exports this module's surface for backwards compatibility
(test_mc.py and the /api/plan routes keep working unchanged); new code
should import from agent.plans directly.

Classification derives its base vocabulary from mcp.capability — one
canonical capability/policy vocabulary across the whole codebase.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from mcp.capability import DESTRUCTIVE, READ

# ---------------------------------------------------------------------------
# 1. Step destruction classifier. The model doesn't tag steps — the harness
#    does, based on the tool's identity. This keeps the protocol simple
#    and prevents the model from accidentally hiding a destructive step
#    inside a normal-looking one.
#
#    SINGLE SOURCE OF TRUTH: the base keyword vocabulary is DERIVED from
#    mcp.capability.category_hints() — the canonical capability model.
#    mc.py only adds the plan-confirmation superset (create/spawn/etc.
#    are tagged destructive here purely so the user confirms plan steps
#    that mutate an editor). The canonical policy/authorization layer
#    remains mcp/policy.py + agent verification; this 3-bucket tag only
#    drives the plan UX.
# ---------------------------------------------------------------------------

from mcp.capability import category_hints as _category_hints

_H = _category_hints()

# Plan-confirmation superset: mutating verbs that the canonical capability
# model calls CREATE/MODIFY/BUILD but which the plan UX should surface for
# user confirmation before they run against a live editor.
_PLAN_CONFIRM_EXTRAS = (
    "write_file", "append_to_file", "commit", "push", "publish", "deploy",
    "save", "build", "compile", "package", "bake",
    "spawn", "create_actor", "add_asset", "create",
    "register", "install", "configure", "set_", "update_",
    "send", "post", "upload",
    "exec", "execute_command", "run_command",
    "write", "append", "overwrite",
    "move", "rename", "copy",
    "import", "export",
    "tool_call", "tools/call",
)
# Plan-UX read-only extras (canonical READ hints + Nex introspection tools).
_PLAN_SAFE_EXTRAS = (
    "who_am_i", "list_platforms", "ping", "echo",
    "speak", "log_event", "recent_events",
)

_DESTRUCTIVE_HINTS = tuple(_H[DESTRUCTIVE]) + _PLAN_CONFIRM_EXTRAS
_REVERSIBLE_HINTS = tuple(_H[READ]) + _PLAN_SAFE_EXTRAS


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
                # Director layer: which SYSTEM this step belongs to (scope
                # attribution) and the checklist criteria it is meant to
                # prove. Optional — absence is tolerated, never invented.
                "system": str(s.get("system") or "").strip().lower(),
                "criteria": [str(c)[:200] for c in (s.get("criteria") or [])
                             if str(c).strip()][:8],
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
        "verified": p.get("verified", False),
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
    # HONESTY: tool success != task success. A completed plan is marked
    # `verified: False` unless a real verifier ran (the canonical verified
    # execution path is the AutonomousAgent / agent.verification, reached
    # via the /api/plan/<id>/autonomous bridge). This sequential executor
    # never claims verification it didn't perform.
    if step_index == len(p["plan"]["steps"]) - 1:
        p["completed"] = True
        p["status"] = "completed"
        p["verified"] = False
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


