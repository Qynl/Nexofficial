"""Deterministic routine-vs-hard workload classification.

This is deliberately not model-driven: user text cannot directly select a
provider, and deciding where to spend hosted quota must not itself cost a
hosted request. The classifier only labels planning. Every resulting plan still
passes the same live MCP schema, policy, approval and evidence checks.
"""
from __future__ import annotations

import re
from typing import Any

_HARD_PHRASES = (
    "production ready", "production-ready", "release ready", "release-ready",
    "high quality", "high-quality", "open world", "open-world", "full game",
    "complete game", "entire game", "large scale", "large-scale",
    "end to end", "end-to-end", "root cause", "performance profile",
    "security audit", "architecture migration", "breaking change",
)
_HARD_WORDS = frozenset({
    "architect", "architecture", "benchmark", "complex", "debug", "diagnose",
    "integrate", "migration", "optimize", "production", "refactor", "release",
    "scalable", "security", "ship", "system", "workflow",
})
_EXPLICIT_HARD_WORDS = frozenset({
    "architect", "architecture", "benchmark", "debug", "diagnose", "migrate",
    "migration", "optimize", "refactor", "security",
})
_ACTION_WORDS = frozenset({
    "add", "analyze", "audit", "build", "change", "create", "debug", "delete",
    "design", "diagnose", "fix", "implement", "inspect", "integrate", "make",
    "migrate", "modify", "optimize", "profile", "refactor", "remove", "repair",
    "review", "run", "test", "update", "validate", "verify",
})


def planning_purpose(goal: str, *, game_production: bool = False,
                     large_program: bool = False,
                     registry: Any = None) -> str:
    """Return ``planning-routine`` or ``planning-hard`` without an LLM call.

    Game-production and hierarchical programs always use the hard-work lane.
    Generic work becomes hard when scope, explicit quality language, or several
    distinct actions indicate that a small local plan is not enough.
    ``registry`` is optional and used only for a bounded live-tool-count hint.
    """
    if game_production or large_program:
        return "planning-hard"
    text = (goal or "").strip().lower()
    words = re.findall(r"[a-z0-9_-]+", text)
    word_set = set(words)
    score = 0
    if any(phrase in text for phrase in _HARD_PHRASES):
        score += 3
    if word_set & _EXPLICIT_HARD_WORDS:
        score += 3
    else:
        score += min(3, len(word_set & _HARD_WORDS))
    actions = word_set & _ACTION_WORDS
    if len(actions) >= 3:
        score += 2
    elif len(actions) >= 2:
        score += 1
    if len(text) >= 500:
        score += 2
    elif len(text) >= 220:
        score += 1
    if text.count(" and ") + text.count(",") >= 3:
        score += 1
    if registry is not None:
        try:
            # A broad live production surface plus a multi-action goal usually
            # benefits from the stronger lane; never inspect tool output here.
            if len(registry.all_tools()) >= 40 and len(actions) >= 2:
                score += 1
        except Exception:  # noqa: BLE001
            pass
    return "planning-hard" if score >= 3 else "planning-routine"
