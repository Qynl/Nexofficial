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
    than shipping nothing as 'success'.

    Richer criteria: rejects nested empties too — empty lists, dicts whose
    values are all empty, {"result": None}-style wrappers, whitespace-only
    strings, and {"ok": true} with no payload keys.
    """
    if result is None:
        return True
    if isinstance(result, str):
        return result.strip() == ""
    if isinstance(result, (list, tuple)):
        return len(result) == 0 or all(_is_empty_result(x) for x in result)
    if isinstance(result, dict):
        if len(result) == 0:
            return True
        # Unwrap single-key envelope styles before judging.
        if set(result.keys()) <= {"result", "data", "content"}:
            return _is_empty_result(next(iter(result.values())))
        values = [v for v in result.values() if v is not None]
        if not values:
            return True
        # All values trivially empty (e.g. {"ok": True} with no payload).
        return all(_is_empty_result(v) for v in values)
    return False


def check_expect(result: Any, expect: Any) -> tuple:
    """Rich acceptance criteria for a task result, driven by the plan's
    ``expect`` field (plumbed from plan steps onto Task.expect).

    ``expect`` may be:
      * None / ""                -> only the triviality gate applies
                                    (handled by the caller); report ok.
      * a plain string           -> treated as a human description: require
                                    a non-trivial result (see
                                    _is_empty_result) — no fake matching.
      * a dict with any of:
          keys      [str]        -> every key must exist in the result dict
          contains  str|[str]    -> each fragment must appear in the
                                    stringified result
          min_len   int          -> len(result) >= min_len (str/list/dict)
          is        any          -> deep equality with the result
          not_empty bool         -> require a non-trivial result

    Returns (ok: bool, note: str). Unknown spec keys are ignored so plans
    stay forward-compatible; a malformed spec never crashes the loop.
    """
    if expect is None or expect == "":
        return True, "no expect criteria"
    if isinstance(expect, str):
        if _is_empty_result(result):
            return False, "expect: result is empty/trivial"
        return True, "expect: non-trivial result"
    if not isinstance(expect, dict):
        return True, "expect: unrecognized criteria ignored"

    failures = []
    keys = expect.get("keys")
    if isinstance(keys, (list, tuple)):
        if not isinstance(result, dict):
            failures.append("result is not an object (wanted keys %s)"
                            % list(keys))
        else:
            missing = [k for k in keys if k not in result]
            if missing:
                failures.append("missing key(s) %s" % missing)
    contains = expect.get("contains")
    if isinstance(contains, str):
        contains = [contains]
    if isinstance(contains, (list, tuple)):
        blob = _stringify(result)
        for frag in contains:
            if str(frag) not in blob:
                failures.append("result does not contain %r" % frag)
    min_len = expect.get("min_len")
    if isinstance(min_len, int):
        try:
            if len(result) < min_len:
                failures.append("result length %d < required %d"
                                % (len(result), min_len))
        except TypeError:
            failures.append("result has no length to compare with min_len")
    if "is" in expect:
        if result != expect["is"]:
            failures.append("result != expected value %r" % (expect["is"],))
    if expect.get("not_empty") and _is_empty_result(result):
        failures.append("result is empty/trivial")

    if failures:
        return False, "expect failed: " + "; ".join(failures)
    return True, "expect criteria met"


def _stringify(result: Any) -> str:
    try:
        import json
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(result)


def verify_task(task: Any, registry: CapabilityRegistry,
                result: Any) -> VerificationResult:
    """Run the task's verify_tool against the registry, if present.

    Layers of criteria (richer than a single boolean):
      1. the task's ``expect`` acceptance criteria (from the plan step:
         required keys / contains / min_len / equals / not_empty),
      2. the minimum triviality gate: an information-free result
         (None / {} / '' / empty lists / envelope-of-nothing) is NOT
         accepted as success,
      3. the task's ``verify_tool`` — a REAL check via a discovered tool.

    If no verifier is available we still report UNVERIFIED (never fake
    success) — but only after 1 and 2 passed.
    """
    # 1) Plan-declared acceptance criteria.
    expect = getattr(task, "expect", None)
    if expect:
        ok, note = check_expect(result, expect)
        if not ok:
            return VerificationResult(ok=False, verified=False, note=note)

    # 2) Triviality gate when no verifier exists (see below for verifier).
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
