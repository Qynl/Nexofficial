"""Bounded, user-visible creative contract for game-production runs.

A short game brief gives every production stage the same concrete target:
who the player is, what they do repeatedly, what makes the game distinctive,
how it should look/sound, and how success can be observed. The brief is a
model-proposed design artifact, not evidence that the game implements it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Tuple


_SCALAR_LIMITS: Dict[str, int] = {
    "title": 100,
    "pitch": 420,
    "player_fantasy": 360,
    "target_audience": 220,
    "target_platform": 180,
    "signature_mechanic": 420,
    "visual_direction": 480,
    "audio_direction": 320,
    "session_shape": 260,
    "progression": 360,
}
_LIST_LIMITS: Dict[str, Tuple[int, int]] = {
    "core_loop": (6, 220),
    "design_pillars": (5, 220),
    "accessibility": (6, 220),
    "assumptions": (6, 220),
}
_CRITERION_LIMIT = 6
_CRITERION_TEXT_LIMIT = 260

# Completeness mirrors PLANNER_SYSTEM's required fields. No prompted section
# in this stage-shared contract silently becomes optional.
_REQUIRED_SCALARS = (
    "pitch", "player_fantasy", "target_audience", "target_platform",
    "signature_mechanic", "visual_direction", "audio_direction",
    "session_shape", "progression",
)
_REQUIRED_LISTS = {
    "core_loop": 3,
    "design_pillars": 3,
    "accessibility": 1,
    "assumptions": 1,
    "acceptance_criteria": 4,
}


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Keep plain text only, discard control characters, and bound every field.
    value = "".join(ch for ch in value if ch in "\t\n\r" or ord(ch) >= 32)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _string_list(value: Any, max_items: int, item_limit: int) -> List[str]:
    if isinstance(value, str):
        source: Iterable[Any] = re.split(r"\n+", value)
    elif isinstance(value, (list, tuple)):
        source = value
    else:
        return []
    out: List[str] = []
    seen = set()
    for item in source:
        text = _clean_text(item, item_limit)
        key = text.casefold()
        if text and key not in seen:
            out.append(text)
            seen.add(key)
        if len(out) >= max_items:
            break
    return out


def _criteria(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for item in value[:_CRITERION_LIMIT * 2]:
        if isinstance(item, Mapping):
            criterion = _clean_text(item.get("criterion"), _CRITERION_TEXT_LIMIT)
            verification = _clean_text(item.get("verification"),
                                       _CRITERION_TEXT_LIMIT)
        else:
            criterion = _clean_text(item, _CRITERION_TEXT_LIMIT)
            verification = ""
        key = criterion.casefold()
        if criterion and key not in seen:
            out.append({"criterion": criterion, "verification": verification})
            seen.add(key)
        if len(out) >= _CRITERION_LIMIT:
            break
    return out


def normalize_game_brief(value: Any) -> Dict[str, Any]:
    """Return only known, size-bounded fields from untrusted model output."""
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for field, limit in _SCALAR_LIMITS.items():
        text = _clean_text(value.get(field), limit)
        if text:
            out[field] = text
    for field, (max_items, item_limit) in _LIST_LIMITS.items():
        items = _string_list(value.get(field), max_items, item_limit)
        if items:
            out[field] = items
    criteria = _criteria(value.get("acceptance_criteria"))
    if criteria:
        out["acceptance_criteria"] = criteria
    return out


def _missing_normalized(brief: Dict[str, Any]) -> List[str]:
    """Check a normalized brief without repeating its allowlist pass."""
    missing = [field for field in _REQUIRED_SCALARS if not brief.get(field)]
    for field, minimum in _REQUIRED_LISTS.items():
        if len(brief.get(field) or []) < minimum:
            missing.append(field)
    criteria = brief.get("acceptance_criteria") or []
    if (len(criteria) >= _REQUIRED_LISTS["acceptance_criteria"]
            and any(not item.get("verification") for item in criteria)):
        missing.append("acceptance_criteria.verification")
    return missing


def missing_brief_fields(value: Any) -> List[str]:
    """List the creative-contract pieces still missing or too thin to use."""
    return _missing_normalized(normalize_game_brief(value))


def brief_health(value: Any) -> Dict[str, Any]:
    brief = normalize_game_brief(value)
    missing = _missing_normalized(brief)
    return {
        "complete": not missing,
        "missing": missing,
        "note": ("A creative contract is a proposed design target, not "
                 "proof the game implements or satisfies it."),
    }
