"""Visual before/after EVIDENCE coverage — ordering, not pixel comparison.

agent/visual.py already tracks what a vision model's CRITIQUE says about
a screenshot it was actually shown. What it does not check is whether a
screenshot was captured on both sides of a visual-relevant change at
all — a run can mutate a level's lighting and never capture anything
before doing so, making any later "it looks better now" claim
unfalsifiable.

This module checks exactly that, structurally: for every successful
CREATE/MODIFY/DESTRUCTIVE/BUILD call this run made, was a capture tool
(screenshot/viewport_capture/capture_frame/snapshot) called successfully
both BEFORE and AFTER it, in this run's own step sequence? This is
honest about its limits: it is ORDERING evidence only. No pixel
comparison happens here — there is no image-processing capability
connected to compare two screenshots' actual content, so a before/after
pair existing is evidence a review COULD happen, never that the
before/after difference was reviewed or what it showed.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from mcp.capability import BUILD, CREATE, DESTRUCTIVE, MODIFY

_CAPTURE_HINTS = ("screenshot", "viewport_capture", "capture_frame",
                  "snapshot")
_VISUALLY_RELEVANT_CATEGORIES = {CREATE, MODIFY, DESTRUCTIVE, BUILD}


def _tool_view(registry: Any, server: Optional[str], tool: str) -> Any:
    try:
        if server:
            return registry.by_name("%s.%s" % (server, tool))
        return registry.by_name(tool)
    except Exception:  # noqa: BLE001 - evidence lookup must not crash a run
        return None


def _is_capture(tool_name: str) -> bool:
    low = (tool_name or "").lower()
    return any(h in low for h in _CAPTURE_HINTS)


def before_after_coverage(tasks: Iterable[Any], registry: Any
                          ) -> Dict[str, Any]:
    """Which of this run's visually-relevant mutations have a real
    capture call both before and after them, in sequence."""
    tasks = list(tasks)
    capture_indices = [
        i for i, t in enumerate(tasks)
        if getattr(t, "status", None) == "success"
        and _is_capture(getattr(t, "tool", "") or "")
    ]
    covered: List[Dict[str, Any]] = []
    uncovered: List[Dict[str, Any]] = []
    for i, t in enumerate(tasks):
        if getattr(t, "status", None) != "success":
            continue
        tool_name = getattr(t, "tool", None)
        if not tool_name or _is_capture(tool_name):
            continue
        tv = _tool_view(registry, getattr(t, "server", None), tool_name)
        category = (getattr(getattr(tv, "capability", None), "category", "")
                   if tv is not None else "")
        if category not in _VISUALLY_RELEVANT_CATEGORIES:
            continue
        has_before = any(c < i for c in capture_indices)
        has_after = any(c > i for c in capture_indices)
        entry = {"step": getattr(t, "name", "?"), "tool": tool_name}
        if has_before and has_after:
            covered.append(entry)
        else:
            missing = []
            if not has_before:
                missing.append("before")
            if not has_after:
                missing.append("after")
            entry["missing"] = missing
            uncovered.append(entry)
    total = len(covered) + len(uncovered)
    return {
        "mutations_checked": total,
        "with_before_after_capture": covered,
        "missing_before_after_capture": uncovered,
        "coverage_pct": round(100 * len(covered) / total) if total else 0,
        "note": ("Structural capture ORDERING only — whether a capture "
                "tool ran before/after a visual-relevant mutation in this "
                "run's own sequence. Never a pixel comparison: no "
                "image-processing capability is connected here, so a "
                "before/after pair existing is evidence a review COULD "
                "happen, not that the actual visual difference was ever "
                "looked at or that it improved anything."),
    }
