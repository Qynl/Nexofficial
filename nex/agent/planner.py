"""Deterministic fallback planner — used only when no model can plan.

Honesty rule: without a model, Nex cannot know HOW an arbitrary goal
maps onto arbitrary tools. This planner therefore does the one thing
that is safe to do without intelligence:

  * lexically match the goal against the live tool catalog, and if a
    tool clearly IS the request (goal "take a screenshot" → tool
    `take_screenshot`), plan that single step;
  * otherwise return an EMPTY graph — the run reports a blocked status
    with the capability summary instead of pretending.

It never invents multi-step workflows. That judgment is the model's
job; faking it here would produce confident garbage.
"""
from __future__ import annotations

import re
from typing import List

from mcp.registry import CapabilityRegistry, ToolView
from agent.task_graph import Task, TaskGraph

_STOP = {
    "the", "a", "an", "and", "or", "to", "for", "with", "please", "can",
    "you", "me", "my", "in", "on", "at", "of", "it", "is", "are", "do",
    "does", "using", "use", "then", "that", "this", "into", "from",
    "make", "create", "build", "get", "give", "show", "want", "need",
    "would", "like", "some", "new", "up",
}
# Words that name the medium, not the content ("echo the TEXT hello"):
# for filling a single-argument tool they are filler.
_PAYLOAD_FILLER = {"text", "message", "string", "word", "words", "value",
                   "number", "saying", "line"}


def _goal_words(goal: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (goal or "").lower())
            if len(w) > 2 and w not in _STOP]


def _score(tv: ToolView, words: List[str]) -> int:
    name_toks = set(re.findall(r"[a-z0-9]+", tv.name.lower()))
    desc_toks = set(re.findall(r"[a-z0-9]+",
                               (tv.description or "").lower())) if tv.description else set()
    score = 0
    for w in words:
        if w in name_toks:
            score += 3
            # A goal word that IS the tool's whole name ("echo hello"
            # → tool `echo`) is as direct as a request gets.
            if name_toks == {w}:
                score += 3
        elif any(tok.startswith(w) or w.startswith(tok)
                 for tok in name_toks if len(tok) > 2):
            score += 2
        elif w in desc_toks:
            score += 1
    return score


def plan(goal: str, registry: CapabilityRegistry) -> TaskGraph:
    """Single-step lexical plan, or an honest empty graph."""
    g = TaskGraph()
    words = _goal_words(goal)
    if not words:
        return g
    best: ToolView | None = None
    best_score = 0
    for tv in registry.all_tools():
        s = _score(tv, words)
        if s > best_score:
            best, best_score = tv, s
    # Require a clear signal: most goal words matched the tool's NAME.
    if best is None or best_score < max(6, 2 * len(words)):
        return g
    # The words the tool's own name did not consume are the REQUEST'S
    # payload ("echo the text hello" → payload "hello"). If the tool
    # wants exactly one required string, hand it over.
    name_toks = set(re.findall(r"[a-z0-9]+", best.name.lower()))
    payload = [w for w in words
               if w not in name_toks and w not in _PAYLOAD_FILLER]
    args: dict = {}
    schema = best.schema if isinstance(best.schema, dict) else {}
    props = schema.get("properties") or {}
    required = [k for k in (schema.get("required") or [])
                if isinstance(props.get(k), dict)
                and props[k].get("type") == "string"]
    if payload and len(required) == 1:
        args[required[0]] = " ".join(payload)
    g.add(Task(
        id="s0",
        name=best.name.replace("_", " ").strip().capitalize(),
        server=best.server,
        tool=best.name,
        args=args,
        contract_fingerprint=getattr(best, "contract_fingerprint", ""),
        expect="the tool's result answers the request",
        why="direct match for the request (planned without a model)",
    ))
    g.plan_meta = {
        "title": "Single step: %s" % best.name,
        "rationale": ("No planning model was available; the request matched "
                      "one connected tool directly."),
        "requested": 1, "kept": 1,
    }
    return g
