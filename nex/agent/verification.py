"""Verification of results (STAGE 8 / STAGE 25).

A successful tool call does NOT mean the task succeeded. This module turns a
task's optional ``verify_tool`` into a real check against the live registry.
If no verifier is available we report UNVERIFIED (never "fake success").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from agent.registry import CapabilityRegistry


@dataclass
class VerificationResult:
    ok: bool            # did the check pass (or was it unverifiable)?
    verified: bool      # did an actual verifier run and pass?
    note: str
    detail: Any = None


def _is_empty_result(result: Any) -> bool:
    """A produced result that carries no information is a low-quality
    outcome — treat it as a failure so the loop retries/repairs rather
    than shipping nothing as 'success'."""
    if result is None:
        return True
    if isinstance(result, dict) and len(result) == 0:
        return True
    if isinstance(result, str) and result.strip() == "":
        return True
    return False


def verify_task(task: Any, registry: CapabilityRegistry,
                result: Any) -> VerificationResult:
    """Run the task's verify_tool against the registry, if present.

    If no verifier exists, we still apply a minimum quality gate: an empty
    result (None / {} / '') is NOT accepted as success. This is what stops
    the agent from silently shipping low-quality (or no) output.
    """
    if not task.verify_tool:
        if _is_empty_result(result):
            return VerificationResult(
                ok=False, verified=False,
                note="no verifier and result is empty; rejecting to "
                     "avoid low-quality/empty output")
        return VerificationResult(
            ok=True, verified=False,
            note="no verifier available; result unverified")

    tv = registry.by_name(task.verify_tool)
    if tv is None:
        return VerificationResult(
            ok=False, verified=False,
            note="verify tool '%s' not discovered" % task.verify_tool)

    args = dict(getattr(task, "verify_args", {}) or {})
    # Fill likely id from the just-produced result if the verifier needs it.
    if isinstance(result, dict):
        for key in ("id", "asset", "project", "anim", "script"):
            if key in result and key not in args:
                args[key] = result[key]

    resp = registry.call(tv.server, tv.name, args)
    if isinstance(resp, dict) and "error" in resp:
        return VerificationResult(
            ok=False, verified=True,
            note="verification failed: %s" % resp["error"], detail=resp)
    return VerificationResult(
        ok=True, verified=True, note="verified", detail=resp.get("result"))
