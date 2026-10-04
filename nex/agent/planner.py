"""Deterministic fallback planner — used only when no model can plan.

Honesty rule: without a model, Nex cannot know HOW an arbitrary goal
maps onto arbitrary tools. What it CAN do without intelligence is use
structure that is already deterministic:

  * lexically match the goal against the live tool catalog, and if a
    tool clearly IS the request (goal "take a screenshot" → tool
    `take_screenshot`), plan that single step;
  * for a game-production goal, assemble the production evidence loop
    (inspect → implement → build → play → capture → review → diagnose →
    verify → profile) from the deterministic gate classification, in the
    same causal order the plan auditor enforces;
  * otherwise return an EMPTY graph — the run reports a blocked status
    with the capability summary instead of pretending.

The evidence loop is structural, not semantic: a step is only planned
when its arguments are honestly satisfiable (no required arguments, or a
single required string the goal itself supplies). Guessing arguments is
still the model's job, and faking it here would produce confident
garbage.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from mcp.registry import CapabilityRegistry, ToolView
from agent.quality import GATE_ORDER, gate_catalog, is_game_production_goal
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


# Causal order the production auditor enforces: each gate's evidence must
# descend from the work it is supposed to prove.
_GATE_PARENTS = {
    "inspection": (),
    "implementation": ("inspection",),
    "build": ("implementation",),
    "playtest": ("build", "implementation"),
    "visual": ("playtest",),
    "visual_review": ("visual",),
    "diagnostics": ("playtest",),
    "verification": ("playtest", "build"),
    "performance": ("playtest",),
}


def _satisfiable_args(tv: ToolView,
                      payload: str) -> Optional[Dict[str, Any]]:
    """Arguments we can supply honestly, or None when guessing would start."""
    schema = tv.schema if isinstance(tv.schema, dict) else {}
    props = schema.get("properties") or {}
    required = [k for k in (schema.get("required") or [])
                if isinstance(k, str)]
    if not required:
        return {}
    if len(required) == 1:
        spec = props.get(required[0]) if isinstance(props, dict) else None
        if isinstance(spec, dict) and spec.get("type") == "string" \
                and "enum" not in spec and payload:
            return {required[0]: payload}
    return None


def _best_for_gate(tools: List[ToolView], words: List[str]
                   ) -> List[ToolView]:
    return sorted(tools, key=lambda tv: (-_score(tv, words), tv.full_name))


def production_plan(goal: str, registry: CapabilityRegistry) -> TaskGraph:
    """Deterministic production evidence loop from the live gate catalog."""
    g = TaskGraph()
    if not is_game_production_goal(goal):
        return g
    catalog = gate_catalog(registry)
    words = _goal_words(goal)
    payload = " ".join(words)[:200]
    chosen: Dict[str, Task] = {}
    skipped: List[str] = []
    index = 0
    for gate in GATE_ORDER:
        candidates = catalog.get(gate) or []
        if not candidates:
            continue
        picked: Optional[ToolView] = None
        args: Dict[str, Any] = {}
        for tv in _best_for_gate(candidates, words):
            maybe = _satisfiable_args(tv, payload)
            if maybe is not None:
                picked, args = tv, maybe
                break
        if picked is None:
            skipped.append(gate)
            continue
        deps = [chosen[parent].id for parent in _GATE_PARENTS.get(gate, ())
                if parent in chosen]
        if gate != "inspection" and not deps:
            # Nothing upstream exists to attach this evidence to, so it would
            # prove nothing. Report it instead of planning a floating step.
            skipped.append(gate)
            continue
        task = Task(
            id="d%d" % index,
            name=picked.name.replace("_", " ").strip().capitalize(),
            slug=gate,
            server=picked.server,
            tool=picked.name,
            args=args,
            deps=deps,
            contract_fingerprint=getattr(picked, "contract_fingerprint", ""),
            expect="%s evidence from %s" % (gate.replace("_", " "),
                                            picked.full_name),
            why="deterministic production loop: %s" % gate.replace("_", " "),
        )
        g.add(task)
        chosen[gate] = task
        index += 1
    if "implementation" not in chosen:
        # Without an authoring step this is an inspection run, not production.
        return TaskGraph()
    g.plan_meta = {
        "title": "Deterministic production evidence loop",
        "rationale": ("No planning model was available. Nex assembled the "
                      "causal inspect/build/run/observe/verify loop from the "
                      "live capability classification and planned only steps "
                      "whose arguments it could supply honestly."),
        "requested": len(chosen) + len(skipped),
        "kept": len(chosen),
        "dropped": ["%s: needs arguments only a planning model can supply"
                    % gate for gate in skipped],
        "deterministic": True,
    }
    return g


def plan(goal: str, registry: CapabilityRegistry) -> TaskGraph:
    """Production evidence loop, single-step lexical plan, or empty graph."""
    structured = production_plan(goal, registry)
    if structured.all():
        return structured
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
