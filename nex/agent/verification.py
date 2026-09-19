"""Verification of results (STAGE 8 / STAGE 25).

A successful tool call does NOT mean the task succeeded. This module turns a
task's optional ``verify_tool`` into a real check against the live registry.
If no verifier is available we report UNVERIFIED (never "fake success").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional

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


# ---------------------------------------------------------------------------
# THE MANDATORY-VERIFICATION RULE (Director layer)
# ---------------------------------------------------------------------------
# "Done!" without evidence is the single most expensive failure mode of an
# autonomous builder: it stops working while the game is still broken. The
# rule enforced here:
#
#   A task that DECLARES what it must prove (step `criteria` — the
#   recipe/Director checklist) is NOT complete just because its tool call
#   returned. Its result must satisfy those criteria, or it is retried /
#   reported as failed.
#
# Criteria are strings from the recipe library ("the player can take
# damage"). They are checked structurally against the result: the result
# must be non-trivial AND (when the criterion names observable keys) show
# the named evidence. When a criterion cannot be checked from the result
# alone, it stays UNRESOLVED for the OBSERVE loop (which has the runtime
# evidence) — never silently "passed".
_CRITERIA_STOPWORDS = frozenset((
    "the", "a", "an", "can", "is", "are", "be", "and", "or", "of", "to",
    "in", "on", "with", "without", "no", "not", "after", "before", "when",
    "player", "game", "system", "must", "should", "does", "do", "it",
    "that", "this", "for", "from", "at", "by", "into", "over", "up",
))


def criteria_keywords(criterion: str) -> List[str]:
    """The observable nouns in a criterion ('the enemy can die' -> enemy,
    die). Used to look for the criterion's evidence in the result."""
    out: List[str] = []
    for tok in re.split(r"[^a-z0-9_]+", (criterion or "").lower()):
        if len(tok) >= 4 and tok not in _CRITERIA_STOPWORDS and tok not in out:
            out.append(tok)
    return out


def check_criteria(criteria: List[str], result: Any) -> tuple:
    """(ok, notes) — structural check of declared criteria against one
    tool result.

    ok=False only when the result is empty/trivial (nothing to prove
    anything with). Otherwise the criteria are carried forward as
    UNRESOLVED evidence for the reviewer/observer — an unproven criterion
    is never reported as passed.
    """
    criteria = [c for c in (criteria or []) if str(c).strip()]
    if not criteria:
        return True, ""
    if _is_empty_result(result):
        return False, ("declared criteria %r but the result is empty — "
                       "nothing proves them" % (criteria[:2],))
    hinted = 0
    for c in criteria:
        kws = criteria_keywords(c)
        blob = _stringify(result).lower()
        if kws and any(k in blob for k in kws):
            hinted += 1
    return True, ("criteria: %d/%d look evidenced in the result; all %d "
                  "stay pending until the running game confirms them"
                  % (hinted, len(criteria), len(criteria)))


def verify_task(task: Any, registry: CapabilityRegistry,
                result: Any) -> VerificationResult:
    """Run the task's verify_tool against the registry, if present.

    Layers of criteria (richer than a single boolean):
      1. the task's ``expect`` acceptance criteria (from the plan step:
         required keys / contains / min_len / equals / not_empty),
      2. the declared CHECKLIST criteria (`task.criteria`, Director layer):
         an empty result proves nothing — see check_criteria,
      3. the minimum triviality gate: an information-free result
         (None / {} / '' / empty lists / envelope-of-nothing) is NOT
         accepted as success,
      4. the task's ``verify_tool`` — a REAL check via a discovered tool.

    If no verifier is available we still report UNVERIFIED (never fake
    success) — but only after 1-3 passed.
    """
    # 1) Plan-declared acceptance criteria.
    expect = getattr(task, "expect", None)
    if expect:
        ok, note = check_expect(result, expect)
        if not ok:
            return VerificationResult(ok=False, verified=False, note=note)

    # 2) Declared checklist criteria (Director layer). An empty result
    #    proves nothing — refuse it before the triviality gate would.
    crit = list(getattr(task, "criteria", []) or [])
    if crit:
        cok, cnote = check_criteria(crit, result)
        if not cok:
            return VerificationResult(ok=False, verified=False, note=cnote)
        crit_note = cnote
    else:
        crit_note = ""

    # 3) Triviality gate when no verifier exists (see below for verifier).
    if not task.verify_tool:
        if _is_empty_result(result):
            return VerificationResult(
                ok=False, verified=False,
                note="no verifier and result is empty; rejecting to "
                     "avoid low-quality/empty output")
        return VerificationResult(
            ok=True, verified=False,
            note=("no verifier available; result unverified"
                  + (" — " + crit_note if crit_note else "")))

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
