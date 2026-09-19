"""The Design Document — NEX 2.0's "understand before building" stage.

A plan that jumps straight to tool calls produces the generic "AI game"
Nex exists to avoid. So before ANY task plan, the agent produces a
structured DESIGN DOCUMENT that answers the design questions for the
idea: what is the player experiencing, what does the player do, where,
why does it create tension/joy/progression, what systems are required,
how will quality be judged?

The design document is STRUCTURED DATA (not Markdown) so Nex can reason
about it later: the critic checks work against the design pillars and
quality gates, and LOCKED design decisions are protected from later
"helpful" changes.

Nothing here is game-specific: the schema is generic (any project —
game, level, tool, world), the prompt asks questions instead of
prescribing content, and parsing is defensive.

ONE SYSTEM: the design doc feeds the canonical planner
(model_planner.model_driven_planner) as context — design and plan are
two stages of the same pipeline, never a second executor.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

# Sections that make a design document buildable. Everything else is
# optional flavor. Kept deliberately small: an honest minimal design
# beats a sprawling mandatory template.
REQUIRED_SECTIONS = ("concept", "genre", "gameplay_loop",
                     "milestones", "quality_gates")

ALL_SECTIONS = ("concept", "genre", "engine", "design_pillars",
                "gameplay_loop", "mechanics", "world", "story", "audio",
                "visual_direction", "technical_architecture",
                "milestones", "dependencies", "acceptance_criteria",
                "tests", "quality_gates")


# ---------------------------------------------------------------------------
# Normalization — LLM output is parsed defensively into the canonical shape.
# ---------------------------------------------------------------------------

def _as_str(v: Any, default: str = "") -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    return default


def _as_str_list(v: Any) -> List[str]:
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        out = []
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                # {"name": ...} shaped entries collapse to their name/text.
                name = item.get("name") or item.get("title") or \
                    item.get("text") or ""
                if name:
                    out.append(str(name).strip())
        return out
    return []


def _as_milestones(v: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(v, list):
        for m in v:
            if isinstance(m, str) and m.strip():
                out.append({"name": m.strip(), "tasks": []})
            elif isinstance(m, dict):
                name = _as_str(m.get("name") or m.get("title"))
                if not name:
                    continue
                out.append({"name": name,
                            "tasks": _as_str_list(m.get("tasks"))})
    return out


def _as_gates(v: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(v, list):
        for g in v:
            if isinstance(g, str) and g.strip():
                out.append({"name": g.strip(), "done": False})
            elif isinstance(g, dict):
                name = _as_str(g.get("name") or g.get("gate"))
                if not name:
                    continue
                out.append({"name": name, "done": bool(g.get("done"))})
    return out


def normalize_design(raw: Any, idea: str = "", engine: Optional[str] = None
                     ) -> Dict[str, Any]:
    """Coerce arbitrary parsed JSON into the canonical design shape.
    Missing sections become empty (never invented)."""
    raw = raw if isinstance(raw, dict) else {}
    design: Dict[str, Any] = {}
    for key in ("concept", "genre", "story", "audio", "visual_direction"):
        design[key] = _as_str(raw.get(key))
    design["engine"] = _as_str(raw.get("engine")) or (engine or "")
    design["design_pillars"] = _as_str_list(raw.get("design_pillars"))
    design["gameplay_loop"] = _as_str(raw.get("gameplay_loop"))
    design["mechanics"] = _as_str_list(raw.get("mechanics"))
    world = raw.get("world")
    if isinstance(world, dict):
        design["world"] = {"summary": _as_str(world.get("summary")),
                           "locations": _as_str_list(world.get("locations"))}
    elif isinstance(world, str):
        design["world"] = {"summary": world.strip(), "locations": []}
    else:
        design["world"] = {"summary": "", "locations": []}
    design["technical_architecture"] = _as_str_list(
        raw.get("technical_architecture"))
    design["milestones"] = _as_milestones(raw.get("milestones"))
    design["dependencies"] = _as_str_list(raw.get("dependencies"))
    design["acceptance_criteria"] = _as_str_list(
        raw.get("acceptance_criteria"))
    design["tests"] = _as_str_list(raw.get("tests"))
    design["quality_gates"] = _as_gates(raw.get("quality_gates"))
    design["idea"] = _as_str(raw.get("idea")) or idea
    design["created_at"] = time.time()
    return design


def empty_design(idea: str, engine: Optional[str] = None) -> Dict[str, Any]:
    return normalize_design({}, idea=idea, engine=engine)


def missing_sections(design: Dict[str, Any]) -> List[str]:
    """Which REQUIRED sections are still empty — surfaced honestly, never
    silently filled with invented content."""
    out = []
    if not _as_str(design.get("concept")):
        out.append("concept")
    if not _as_str(design.get("genre")):
        out.append("genre")
    if not _as_str(design.get("gameplay_loop")):
        out.append("gameplay_loop")
    if not design.get("milestones"):
        out.append("milestones")
    if not design.get("quality_gates"):
        out.append("quality_gates")
    return out


# ---------------------------------------------------------------------------
# Parsing the model's design answer
# ---------------------------------------------------------------------------

def _first_balanced_object(text: str) -> Optional[str]:
    """Depth-scan for the first balanced {...} (string-aware). More
    robust than the greedy brace regex: model replies that add prose
    (with or without braces) after the JSON object still parse."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_design(text: str, idea: str = "", engine: Optional[str] = None
                 ) -> Optional[Dict[str, Any]]:
    """Extract the first JSON object from an LLM reply and normalize it.
    Returns None when nothing parseable — the caller stays honest and
    can retry or continue without a design."""
    if not text:
        return None
    candidates: List[str] = []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        candidates.append(fenced.group(1))
    # Depth scan FIRST (the non-greedy fence regex above truncates nested
    # JSON at the first '}', so the balanced scan is the reliable one).
    balanced = _first_balanced_object(text)
    if balanced:
        candidates.append(balanced)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        # Unwrap common envelopes: {"design": {...}} / {"data": {...}}
        if isinstance(data, dict):
            for wrap in ("design", "data", "project"):
                inner = data.get(wrap)
                if isinstance(inner, dict):
                    data = inner
                    break
        return normalize_design(data, idea=idea, engine=engine)
    return None


# ---------------------------------------------------------------------------
# The design prompt — questions, not recipes
# ---------------------------------------------------------------------------

def design_prompt(idea: str, engine: Optional[str] = None,
                  catalog_text: str = "",
                  locked_decisions: Optional[List[str]] = None) -> str:
    locked = ""
    if locked_decisions:
        locked = ("\n\nLOCKED DESIGN DECISIONS (binding, do not contradict "
                  "or re-litigate them):\n" + "\n".join(
                      "- " + d for d in locked_decisions))
    catalog = ""
    if catalog_text:
        catalog = ("\n\nCONNECTED CAPABILITIES (what can actually be built "
                   "with the currently connected MCP servers — design "
                   "within what is possible, and name anything you need "
                   "that is missing as a dependency):\n" + catalog_text)
    return (
        "You are the design lead. Before any implementation, produce the "
        "design document for this request as ONE JSON object (no prose "
        "outside the JSON).\n\n"
        "REQUEST: " + idea + "\n"
        + ("TARGET: " + (engine or "the connected platform(s)") + "\n"
           if engine else "")
        + "\nAnswer the design questions concretely and specifically to "
        "THIS request (no generic filler, no boilerplate):\n"
        "- concept: what is the player/user experiencing?\n"
        "- genre: the honest category\n"
        "- gameplay_loop: what does one moment-to-moment loop actually "
        "look like? (describe it as a short flow)\n"
        "- design_pillars: 3-5 principles every decision is measured "
        "against\n"
        "- mechanics: the systems that must exist for the loop to work\n"
        "- world: {\"summary\": where this takes place, \"locations\": "
        "[places that need to exist]}\n"
        "- story: how information is revealed (or \"\" if purely "
        "systemic)\n"
        "- audio: how sound participates\n"
        "- visual_direction: the look, in words\n"
        "- technical_architecture: what must be implemented, in order of "
        "risk\n"
        "- milestones: 2-5 named milestones, each with concrete tasks\n"
        "- dependencies: external things required (assets, servers, "
        "capabilities that may not exist yet — be honest)\n"
        "- acceptance_criteria: how you will verify each milestone did "
        "what it claimed\n"
        "- tests: concrete checks to run\n"
        "- quality_gates: the bars the result must clear before it may "
        "be called done (gameplay works, atmosphere works, no broken "
        "interactions, no placeholder content, performance — as applies "
        "to THIS request)"
        + locked + catalog + "\n\nRespond with ONLY the JSON object."
    )


def design_summary(design: Dict[str, Any], max_items: int = 6) -> str:
    """Compact human-readable summary for logs, events and prompts."""
    if not design or not isinstance(design, dict):
        return ""
    parts = []
    concept = _as_str(design.get("concept"))
    if concept:
        parts.append(concept)
    genre = _as_str(design.get("genre"))
    if genre:
        parts.append("genre: " + genre)
    loop = _as_str(design.get("gameplay_loop"))
    if loop:
        parts.append("loop: " + loop)
    pillars = design.get("design_pillars") or []
    if pillars:
        parts.append("pillars: " + "; ".join(pillars[:max_items]))
    gates = design.get("quality_gates") or []
    if gates:
        parts.append("gates: " + ", ".join(
            g.get("name", "") for g in gates[:max_items] if isinstance(g, dict)))
    ms = design.get("milestones") or []
    if ms:
        parts.append("milestones: " + ", ".join(
            m.get("name", "") for m in ms[:max_items] if isinstance(m, dict)))
    missing = missing_sections(design)
    if missing:
        parts.append("missing: " + ", ".join(missing))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Design-stability guard: LOCKED decisions survive later "helpfulness".
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]{4,}")
_STOP = {"that", "this", "with", "from", "have", "will", "should", "been",
         "were", "their", "them", "they", "than", "then", "when", "what",
         "into", "only", "also", "because", "game", "games", "make",
         "player", "used", "using", "uses"}


def _stem(tok: str) -> str:
    """Tiny stemmer so 'shown'/'show' and 'added'/'add' collide."""
    if len(tok) > 4:
        if tok.endswith("wn"):
            return tok[:-1]
        if tok.endswith("ing") and len(tok[:-3]) >= 3:
            return tok[:-3]
        if tok.endswith("ed") and len(tok[:-2]) >= 3:
            return tok[:-2]
    return tok


def _tokens(text: str) -> List[str]:
    toks = [t for t in _TOKEN_RE.findall((text or "").lower())
            if t not in _STOP]
    out = list(toks)
    for t in toks:
        s = _stem(t)
        if s != t and len(s) >= 3:
            out.append(s)
    return out


def decision_tokens(text: str) -> List[str]:
    return _tokens(text)


def conflicts_locked(text: str, locked_decisions: List[str],
                     threshold: float = 0.6) -> Optional[str]:
    """Heuristic conflict check: does a proposed task/step contradict a
    LOCKED decision? Token-overlap based, deliberately conservative —
    it returns the locked decision text when the overlap is high enough
    that the step is probably re-litigating it. Prompt-level respect
    (the planner sees locked decisions) stays the FIRST guard; this is
    the deterministic safety net."""
    if not locked_decisions:
        return None
    t_toks = set(_tokens(text))
    if not t_toks:
        return None
    # Negation carriers flip meaning: "no health bar" locked, a step
    # "add health bar" shares the head noun but not the negation — the
    # token check below handles the common case; explicit negation
    # tokens are kept in the comparison for this reason.
    for dec in locked_decisions:
        d_toks = set(_tokens(dec))
        if not d_toks:
            continue
        overlap = len(t_toks & d_toks) / max(1, min(len(t_toks),
                                                    len(d_toks)))
        if overlap >= threshold:
            return dec
    return None
