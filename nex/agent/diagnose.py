"""LLM-driven failure diagnosis (STAGE 27 — the real thing, not future work).

When a tool call fails or verification rejects a result, the deterministic
repairs (regex missing-parameter fixes, alt-server switches, retries) run
first because they're free. When they DON'T apply, this module asks the
model for a structured recovery decision instead of a free-form guess:

    {"action": "correct_args" | "switch_tool" | "give_up",
     "args": {...},            # for correct_args
     "tool": "other_tool",     # for switch_tool
     "reason": "..." }

The decision is VALIDATED before use:
  * correct_args -> args must be a non-empty dict, different from current
  * switch_tool  -> the proposed tool must EXIST in the live registry and
                    be policy-allowed (no hallucinated escapes)
  * give_up      -> accepted as-is; the loop marks the task failed with the
                    model's reason instead of a generic one

Bounded: the caller (agent.loop) runs this at most once per task via
Task.llm_diagnosed, so a broken model can never loop the pipeline.

If no LLM is available everything here is skipped — the deterministic
repairs remain the fallback path (never the other way around).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

SYSTEM = (
    "You are Nex's failure-diagnosis agent. A tool call inside an "
    "autonomous task graph failed. Decide how to recover. Reply with ONLY "
    "a JSON object: {\"action\": \"correct_args\"|\"switch_tool\"|"
    "\"give_up\", \"args\": {...}, \"tool\": \"name\", \"reason\": \"...\"}. "
    "Prefer correct_args with fixed arguments; use switch_tool only if "
    "another available tool clearly serves the same purpose; use give_up "
    "with an honest reason if the step cannot succeed."
)


def build_failure_context(task: Any, err: str, registry,
                          phase: str = "execute") -> List[Dict[str, str]]:
    """One focused message pair: what failed, with what, and what exists."""
    # Compact live catalog so switch_tool proposals are grounded in reality.
    names = []
    try:
        for tv in registry.all_tools():
            names.append(tv.name)
    except Exception:  # noqa: BLE001
        names = []
    catalog = ", ".join(sorted(set(names))[:60]) if names else "(none)"
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            "phase: %s\ntask: %s\ntool: %s on server %s\nargs: %s\n"
            "error/verifier note: %s\n\navailable tools: %s\n\n"
            "How do we recover? JSON only."
            % (phase, getattr(task, "name", "?"),
               getattr(task, "tool", "?"), getattr(task, "server", "?"),
               _safe_json(getattr(task, "args", {})), err[:500],
               catalog))},
    ]


def parse_decision(reply: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse + sanity-shape the model's decision. Returns None on garbage."""
    if not reply:
        return None
    try:
        from agent.judges import _extract_json
        obj = _extract_json(reply)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    action = obj.get("action")
    if action not in ("correct_args", "switch_tool", "give_up"):
        # Tolerate the minimal protocol: infer from what the model sent.
        if isinstance(obj.get("args"), dict):
            action = "correct_args"
        elif obj.get("tool"):
            action = "switch_tool"
        else:
            return None
        obj["action"] = action
    return obj


def apply_decision(decision: Dict[str, Any], task: Any, registry
                   ) -> Optional[Dict[str, Any]]:
    """Validate a parsed decision against the live registry.

    Returns a normalized instruction for the loop:
      {"kind": "correct_args", "args": {...}, "reason": ...}
      {"kind": "switch_tool", "tool": "name", "reason": ...}
      {"kind": "give_up", "reason": ...}
    or None when the decision is unusable (hallucinated tool, identical
    args, unknown action shape).
    """
    action = decision.get("action")
    reason = str(decision.get("reason") or "")[:300]
    if action == "correct_args":
        args = decision.get("args")
        if (isinstance(args, dict) and args
                and args != getattr(task, "args", None)):
            return {"kind": "correct_args", "args": dict(args),
                    "reason": reason or "LLM-proposed corrected arguments"}
        return None
    if action == "switch_tool":
        tool = str(decision.get("tool") or "").strip()
        if not tool:
            return None
        tv = registry.by_name(tool)
        if tv is None and "." in tool:
            tv = registry.by_name(tool.split(".", 1)[-1])
        if tv is None:
            return None  # hallucinated escape hatch — rejected
        if tv.name == getattr(task, "tool", None):
            return None  # not actually a switch
        from mcp.policy import authorize
        if not authorize(tv.server, tv.name, tv.capability).allowed:
            return None  # policy-denied escape hatch — rejected
        return {"kind": "switch_tool", "tool": tv.name, "server": tv.server,
                "reason": reason or ("LLM proposed '%s' instead" % tv.name)}
    if action == "give_up":
        return {"kind": "give_up",
                "reason": reason or "model judged the step unrecoverable"}
    return None


def diagnose(task: Any, err: str, registry, llm,
             phase: str = "execute") -> Optional[Dict[str, Any]]:
    """One-shot bounded diagnosis: context -> model -> parsed -> validated."""
    if llm is None:
        return None
    try:
        reply = llm(build_failure_context(task, err, registry, phase))
        decision = parse_decision(reply)
        if decision is None:
            return None
        return apply_decision(decision, task, registry)
    except Exception:  # noqa: BLE001
        return None


def _safe_json(obj: Any) -> str:
    try:
        import json
        return json.dumps(obj, ensure_ascii=False, default=str)[:400]
    except Exception:  # noqa: BLE001
        return str(obj)[:400]
