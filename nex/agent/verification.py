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


def verify_task(task: Any, registry: CapabilityRegistry,
                result: Any) -> VerificationResult:
    """Run the task's verify_tool against the registry, if present."""
    if not task.verify_tool:
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
