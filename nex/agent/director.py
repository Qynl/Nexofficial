"""The Game Director — turns "make me a game" into concrete systems.

The problem this solves: a small local model asked to "make a AAA game"
produces an unfocused 4000-line plan, or drowns in the whole project at
once. The Director sits ABOVE the planner and does the decomposition
that a human technical director would do:

    GAME REQUEST
         |
         v  Director: what systems does this game need, and in what order?
    SYSTEM MAP  (world / gameplay / ui / systems — each with a status)
         |
         v  Planner: what are the next FEW steps for ONE system?
    BUILD -> OBSERVE -> VERIFY (per system)

Two sources of truth, in this order:
  1. the RECIPE LIBRARY (agent/recipes.py) — proven architectures with
     explicit completion checklists. Deterministic, works with no model.
  2. the MODEL — asked to refine the decomposition for this specific
     request (genre-specific systems the library does not know).

The model refines; it cannot remove. The roots (a character and a place
to play) and the checklists always survive, so a weak model cannot
"simplify" the game into something unverified.

Design rules:
  * one system at a time is "current" — the planner is given a narrow
    objective, never the whole project;
  * every system carries its checklist (the mandatory-verification gate);
  * the Director never names tools — that stays with the live registry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent.project_state import (SYSTEM_BROKEN, SYSTEM_COMPLETE,
                                 SYSTEM_IN_PROGRESS, SYSTEM_PLANNED)
from agent.recipes import (RECIPES_BY_ID, Recipe, checklist_for,
                           select_recipes)

# ---------------------------------------------------------------------------
# Prompt (tight, role-scoped: the Director never sees the tool catalog)
# ---------------------------------------------------------------------------

DIRECTOR_PROMPT = (
    "You are the GAME DIRECTOR of an autonomous game studio. You do NOT "
    "write code, choose tools, or plan steps — the planner does that. Your "
    "only job: decide which SYSTEMS this game needs.\n\n"
    "You are given a game request and a list of PROVEN RECIPES (each has an "
    "id). Answer with JSON ONLY:\n"
    '{"game": "<short game type>", "systems": [{"id": "<recipe id>", '
    '"why": "<one short line>"}], "extra_systems": [{"name": "<system>", '
    '"layer": "world|gameplay|ui|systems", "why": "...", "checklist": '
    '["criterion", "..."]}]}\n\n'
    "Rules:\n"
    "1. Pick recipes by their EXACT id. 4 to 8 systems — a focused game, "
    "not a feature list.\n"
    "2. Order them the way they must be BUILT (foundations first).\n"
    "3. Use extra_systems ONLY for something this specific game needs that "
    "no recipe covers; keep each to at most 4 criteria.\n"
    "4. Every system must be verifiable from the running game. 'Polished "
    "AAA graphics' is not a system.\n"
    "5. Output the JSON object and nothing else."
)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# GamePlan
# ---------------------------------------------------------------------------

@dataclass
class SystemPlan:
    id: str
    title: str
    layer: str
    provides: str = ""
    recipe: str = ""
    checklist: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    steps: List[Dict[str, str]] = field(default_factory=list)
    why: str = ""
    status: str = SYSTEM_PLANNED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "layer": self.layer,
            "provides": self.provides, "recipe": self.recipe,
            "checklist": list(self.checklist), "risks": list(self.risks),
            "steps": list(self.steps), "why": self.why,
            "status": self.status,
        }


@dataclass
class GamePlan:
    goal: str = ""
    game: str = ""
    systems: List[SystemPlan] = field(default_factory=list)
    source: str = "recipes"          # recipes | model | mixed
    note: str = ""

    # ----- views the rest of the system uses ------------------------------
    def by_id(self, sid: str) -> Optional[SystemPlan]:
        sid = (sid or "").strip().lower()
        for s in self.systems:
            if s.id == sid:
                return s
        return None

    def current(self) -> Optional[SystemPlan]:
        """The system to build NOW: the first incomplete one in build
        order. Rationale: the Director ordered systems by dependency, so
        the first unfinished one is the one everything else waits on."""
        for s in self.systems:
            if s.status not in (SYSTEM_COMPLETE,):
                return s
        return None

    def remaining(self) -> List[SystemPlan]:
        return [s for s in self.systems if s.status != SYSTEM_COMPLETE]

    def checklists(self) -> List[str]:
        out: List[str] = []
        for s in self.systems:
            for c in s.checklist:
                if c not in out:
                    out.append(c)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "game": self.game,
                "systems": [s.to_dict() for s in self.systems],
                "source": self.source, "note": self.note}


# ---------------------------------------------------------------------------
# Deterministic decomposition (no model needed)
# ---------------------------------------------------------------------------

def plan_from_recipes(goal: str, limit: int = 6) -> GamePlan:
    """The always-available Director: the recipe library decides.

    This is what makes the architecture model-independent — with a broken
    or absent model, Nex still decomposes the game correctly and still
    has checklists to verify against.
    """
    picked = select_recipes(goal, limit=limit)
    systems: List[SystemPlan] = []
    for r in picked:
        systems.append(SystemPlan(
            id=r.id, title=r.title, layer=r.system, provides=r.provides,
            recipe=r.id, checklist=list(r.checklist), risks=list(r.risks),
            steps=[{"intent": s.intent, "evidence": s.evidence}
                   for s in r.steps],
            why="proven recipe for: " + r.provides))
    gp = GamePlan(goal=goal, game=(goal or "").strip()[:60] or "game",
                  systems=systems, source="recipes")
    return apply_status(gp, {})


def apply_status(plan: GamePlan, systems: Dict[str, Dict[str, Any]]
                 ) -> GamePlan:
    """Fold the project's system map (memory) into a fresh GamePlan, so
    'what exists' is not re-planned as if it were new."""
    for s in plan.systems:
        st = (systems or {}).get(s.id) or {}
        if st.get("status"):
            s.status = st["status"]
        if st.get("notes"):
            s.why = (s.why + " — " + str(st["notes"]))[:200]
    return plan


# ---------------------------------------------------------------------------
# Model-refined decomposition
# ---------------------------------------------------------------------------

def _library_brief(max_recipes: int = 16) -> str:
    lines = []
    for rid, r in list(RECIPES_BY_ID.items())[:max_recipes]:
        lines.append("- %s [%s] %s (provides: %s; needs: %s)"
                     % (rid, r.system, r.title, r.provides,
                        ", ".join(r.requires) or "nothing"))
    return "\n".join(lines)


def director_prompt(goal: str, design: Optional[Dict[str, Any]] = None,
                    memory: Optional[Dict[str, Any]] = None) -> str:
    parts = ["GAME REQUEST: " + (goal or "").strip(), "",
             "PROVEN RECIPES (pick by exact id):", _library_brief()]
    if design:
        bits = []
        for key in ("concept", "genre", "player_experience"):
            v = design.get(key)
            if v:
                bits.append("%s: %s" % (key, v))
        if bits:
            parts += ["", "DESIGN DOCUMENT: " + " | ".join(bits)]
    if memory:
        existing = memory.get("systems") or {}
        if existing:
            brief = "; ".join("%s=%s" % (k, (v or {}).get("status"))
                              for k, v in list(existing.items())[:12])
            parts += ["", "SYSTEMS ALREADY IN THE PROJECT (do not drop "
                          "them): " + brief]
        bugs = memory.get("known_bugs") or []
        if bugs:
            parts += ["", "OPEN DEFECTS: "
                     + "; ".join(str(b.get("kind")) for b in bugs[:5])]
    parts += ["", DIRECTOR_PROMPT]
    return "\n".join(parts)


def plan_with_model(goal: str,
                    llm: Callable[[List[Dict[str, str]]], str],
                    design: Optional[Dict[str, Any]] = None,
                    memory: Optional[Dict[str, Any]] = None,
                    limit: int = 6) -> Optional[GamePlan]:
    """Ask the model to refine the decomposition. Returns None on any
    failure (the caller keeps the deterministic plan)."""
    try:
        reply = llm([{"role": "user",
                      "content": director_prompt(goal, design, memory)}])
        data = _extract_json(reply)
        if not data:
            return None
        chosen = data.get("systems") or []
        systems: List[SystemPlan] = []
        # 1) the model's recipe picks, in the model's order
        for item in chosen[:12]:
            rid = ""
            if isinstance(item, dict):
                rid = str(item.get("id") or "").strip().lower()
                why = str(item.get("why") or "")[:200]
            else:
                rid = str(item).strip().lower()
                why = ""
            r: Optional[Recipe] = RECIPES_BY_ID.get(rid)
            if r is None:
                continue          # hallucinated recipe id -> dropped
            if any(s.id == r.id for s in systems):
                continue
            systems.append(SystemPlan(
                id=r.id, title=r.title, layer=r.system, provides=r.provides,
                recipe=r.id, checklist=list(r.checklist), risks=list(r.risks),
                steps=[{"intent": st.intent, "evidence": st.evidence}
                       for st in r.steps],
                why=why or ("proven recipe for: " + r.provides)))
        # 2) genre-specific systems the library does not cover
        for item in (data.get("extra_systems") or [])[:4]:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            sid = re.sub(r"[^a-z0-9_]+", "_",
                         str(item["name"]).strip().lower()).strip("_")
            if not sid or any(s.id == sid for s in systems):
                continue
            layer = str(item.get("layer") or "gameplay").strip().lower()
            if layer not in ("world", "gameplay", "ui", "systems"):
                layer = "gameplay"
            cl = [str(c)[:160] for c in (item.get("checklist") or [])
                  if str(c).strip()][:4]
            systems.append(SystemPlan(
                id=sid, title=str(item["name"])[:80], layer=layer,
                provides=str(item.get("why") or "")[:160],
                recipe="", checklist=cl,
                why=str(item.get("why") or "")[:200]))
        if not systems:
            return None
        # 3) SAFETY: the structural roots can never be dropped. A game
        #    with no player control or no space to play in is not a game,
        #    and a weak model must not be able to "simplify" that away.
        root_order = ["character_controller", "level_blockout"]
        present = {s.id for s in systems}
        for root in root_order:
            if root in present or root not in RECIPES_BY_ID:
                continue
            r = RECIPES_BY_ID[root]
            # Insert the missing root where it BELONGS (right after the
            # roots that come before it in the library order), so the
            # build order stays: player -> space -> gameplay.
            pos = 0
            for i, s in enumerate(systems):
                if s.id in root_order and \
                        root_order.index(s.id) < root_order.index(root):
                    pos = i + 1
            systems.insert(pos, SystemPlan(
                id=r.id, title=r.title, layer=r.system,
                provides=r.provides, recipe=r.id,
                checklist=list(r.checklist), risks=list(r.risks),
                steps=[{"intent": st.intent, "evidence": st.evidence}
                       for st in r.steps],
                why="structural root (always required)"))
            present.add(root)
        # 4) Bound the decomposition: the model orders, we cut the tail —
        #    a 20-system "everything game" is exactly the failure mode
        #    this layer exists to prevent.
        if len(systems) > limit + 2:
            systems = systems[:limit + 2]
        game = str(data.get("game") or "").strip()[:60] or (
            (goal or "").strip()[:60] or "game")
        return GamePlan(goal=goal, game=game, systems=systems,
                        source="model")
    except Exception:  # noqa: BLE001
        return None


def direct(goal: str, llm: Optional[Callable] = None,
           design: Optional[Dict[str, Any]] = None,
           memory: Optional[Dict[str, Any]] = None,
           limit: int = 6) -> GamePlan:
    """The Director entry point. Model-refined when possible, recipe-based
    always — and the deterministic checklists always win."""
    base = plan_from_recipes(goal, limit=limit)
    gp = None
    if llm is not None:
        gp = plan_with_model(goal, llm, design=design, memory=memory,
                             limit=limit)
    if gp is None:
        # No model (or a failed refinement): the library IS the Director.
        base.source = "recipes"
        return apply_status(base, (memory or {}).get("systems") or {})
    # Merge: a system the model dropped but the library considers
    # structural keeps its checklist from the library.
    by_id = {s.id: s for s in base.systems}
    for s in gp.systems:
        ref = by_id.get(s.id)
        if ref and not s.checklist:
            s.checklist = list(ref.checklist)
            s.risks = list(ref.risks)
    gp.source = "model" if len(gp.systems) >= 3 else "recipes"
    return apply_status(gp, (memory or {}).get("systems") or {})


# ---------------------------------------------------------------------------
# The SCOPE ENVELOPE (the anti-"I improved the entire project" guard)
# ---------------------------------------------------------------------------

def scope_envelope(system: Optional[SystemPlan],
                   remaining: Optional[List[SystemPlan]] = None,
                   max_steps: int = 4) -> Dict[str, Any]:
    """The narrow brief handed to the planner for ONE system.

    A small model does not fail because it cannot write code — it fails
    because it changes everything at once. This envelope states the single
    objective, the success criteria, and the explicit DO-NOT list.
    """
    if system is None:
        return {"objective": "finish the game", "success": [],
                "do_not": [], "max_steps": max_steps}
    others = [s.title for s in (remaining or []) if s.id != system.id]
    if system.status == SYSTEM_COMPLETE:
        others = []
    return {
        "objective": "Implement the '%s' system: %s"
                     % (system.title, system.provides or ""),
        "system": system.id,
        "success": list(system.checklist),
        "steps": [dict(st) for st in system.steps[:max_steps]],
        "do_not": [("do not restructure the project or refactor unrelated "
                    "systems"),
                   ("do not modify a system that is already complete"),
                   ("do not start a different system")] +
                  [("do not change: " + o) for o in others[:4]],
        "max_steps": max_steps,
        "risks": list(system.risks),
    }


def scope_block(env: Dict[str, Any]) -> str:
    """Render the envelope for the planner prompt (tight, unambiguous)."""
    lines = ["CURRENT OBJECTIVE (do NOT exceed it):", env.get("objective", "")]
    if env.get("steps"):
        lines.append("Proven structure to adapt (one step per line, "
                     "same order):")
        for i, st in enumerate(env["steps"], 1):
            ev = (" (prove it: %s)" % st["evidence"]) if st.get("evidence") else ""
            lines.append("  %d. %s%s" % (i, st.get("intent", ""), ev))
    if env.get("success"):
        lines.append("SUCCESS CRITERIA (every one must be verifiable from "
                     "the running game):")
        lines += ["  - " + c for c in env["success"]]
    if env.get("do_not"):
        lines.append("DO NOT:")
        lines += ["  - " + d for d in env["do_not"]]
    if env.get("risks"):
        lines.append("Known failure modes of this system (avoid them): "
                     + "; ".join(env["risks"][:4]))
    lines.append("Plan AT MOST %d steps for this objective. A plan that "
                 "leaves the objective unfinished but touches other systems "
                 "is a FAILED plan." % env.get("max_steps", 4))
    return "\n".join(lines)


def system_status_from_evidence(system_id: str, findings: List[Dict[str, Any]],
                                met: int, total: int) -> str:
    """Derive a system's status from what the OBSERVE loop proved.

    Rules (honest, conservative):
      * an open defect ATTRIBUTED to this system -> BROKEN;
      * all criteria met -> COMPLETE;
      * otherwise -> IN_PROGRESS.
    """
    for f in findings or []:
        if (f.get("system") == system_id
                and f.get("severity") == "major"):
            return SYSTEM_BROKEN
    if total and met >= total:
        return SYSTEM_COMPLETE
    return SYSTEM_IN_PROGRESS


def suggest_checklist(system_id: str) -> List[str]:
    """Checklist for a system id (recipe lookup, [] when unknown)."""
    return checklist_for([system_id])
