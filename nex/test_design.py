"""Tests for the NEX 2.0 Design Document (agent/design.py).

The design document is the "understand before building" stage: structured
data (not Markdown), parsed defensively, prompted with QUESTIONS (never a
game recipe), and guarded by locked-decision conflict detection.
"""
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

design = importlib.import_module("agent.design")


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------------------
# 1. normalize_design: defensive coercion into the canonical shape
# ---------------------------------------------------------------------------

raw = {
    "concept": "A deep-sea salvage story",
    "genre": "narrative exploration",
    "gameplay_loop": "dive -> scan -> salvage -> surface -> decide",
    "design_pillars": ["tension through scarcity", "the sea does not chase you",
                       {"name": "silent storytelling"}],
    "mechanics": "oxygen budget",
    "world": {"summary": "a flooded research coast",
              "locations": ["the pier", {"name": "the trench gate"}]},
    "story": "revealed through recovered logs",
    "milestones": ["Hull", {"name": "The Dive", "tasks": ["wreck scan",
                                                          "salvage loop"]}],
    "quality_gates": ["diving feels tense", {"name": "no placeholder art"}],
    "tests": ["loop completes without stuck states"],
    "acceptance_criteria": ["each dive ends in a decision"],
    "technical_architecture": ["save system first"],
    "dependencies": [],
    "engine": "",
}
d = design.normalize_design(raw, idea="deep sea salvage", engine="unreal")
_expect(d["concept"] == "A deep-sea salvage story", "concept preserved")
_expect(d["engine"] == "unreal", "engine fallback when raw engine empty")
_expect(d["design_pillars"] == ["tension through scarcity",
                                "the sea does not chase you",
                                "silent storytelling"],
        "pillars coerce dicts to names")
_expect(d["mechanics"] == ["oxygen budget"], "string mechanics -> list")
_expect(d["world"]["locations"] == ["the pier", "the trench gate"],
        "world.locations coerces mixed shapes")
_expect(d["milestones"][0] == {"name": "Hull", "tasks": []},
        "string milestone -> {name, tasks}")
_expect(d["milestones"][1]["tasks"] == ["wreck scan", "salvage loop"],
        "milestone tasks preserved")
_expect(d["quality_gates"][1] == {"name": "no placeholder art",
                                  "done": False},
        "gates normalize with done=False")

empty = design.empty_design("some idea")
_expect(empty["idea"] == "some idea" and empty["concept"] == "",
        "empty_design is an honest skeleton (nothing invented)")
_expect(design.missing_sections(empty) == list(design.REQUIRED_SECTIONS),
        "missing_sections lists all required gaps")
_expect(design.missing_sections(d) == [], "complete design has no gaps")


# ---------------------------------------------------------------------------
# 2. parse_design: fenced, bare, envelope, garbage
# ---------------------------------------------------------------------------

doc = {"concept": "c", "genre": "g", "gameplay_loop": "l",
       "milestones": [{"name": "M1", "tasks": ["t1"]}],
       "quality_gates": [{"name": "G1"}]}

p = design.parse_design("```json\n" + json.dumps(doc) + "\n```",
                        idea="x")
_expect(p and p["concept"] == "c", "fenced JSON parses")
p = design.parse_design("Sure! Here it is: " + json.dumps(doc),
                        idea="x")
_expect(p and p["concept"] == "c", "bare JSON in prose parses")
p = design.parse_design(json.dumps({"design": doc}), idea="x")
_expect(p and p["concept"] == "c", "{'design': ...} envelope unwraps")
_expect(design.parse_design("no json at all", idea="x") is None,
        "garbage -> None (honest, never invented)")
_expect(design.parse_design("", idea="x") is None, "empty -> None")


# ---------------------------------------------------------------------------
# 3. design_prompt: questions, not recipes; carries locked + catalog
# ---------------------------------------------------------------------------

prompt = design.design_prompt(
    "a quiet gardening sim", engine="roblox",
    catalog_text="- roblox-studio.create_part",
    locked_decisions=["No conventional health bar."])
_expect("design document" in prompt.lower(), "prompt asks for a design doc")
for q in ("experiencing", "gameplay_loop", "design_pillars", "milestones",
          "quality_gates", "audio"):
    _expect(q in prompt, "prompt asks the '%s' question" % q)
_expect("LOCKED DESIGN DECISIONS" in prompt
        and "No conventional health bar." in prompt,
        "locked decisions are binding in the prompt")
_expect("roblox-studio.create_part" in prompt,
        "live capabilities inform what is designed")
# No recipe: the prompt must not prescribe specific game content.
for recipe in ("health bar", "corridor -> noise", "zombie", "jump scare"):
    _expect(recipe not in prompt.split("LOCKED")[0],
            "prompt contains no '%s' recipe" % recipe)


# ---------------------------------------------------------------------------
# 4. design_summary
# ---------------------------------------------------------------------------

s = design.design_summary(d)
_expect("deep-sea salvage story" in s, "summary carries the concept")
_expect("tension through scarcity" in s, "summary carries pillars")
_expect("missing:" not in s, "complete design reports nothing missing")
s2 = design.design_summary(empty)
_expect("missing:" in s2, "incomplete design reports its gaps")


# ---------------------------------------------------------------------------
# 5. Design stability: locked-decision conflict detection
# ---------------------------------------------------------------------------

locked = ["No conventional health bar.",
          "The entity is never shown directly."]
_expect(design.conflicts_locked("add health bar to player", locked)
        == "No conventional health bar.",
        "step re-litigating a locked decision is flagged")
_expect(design.conflicts_locked("show the entity in a jumpscare", locked)
        == "The entity is never shown directly.",
        "second locked decision also enforced")
_expect(design.conflicts_locked("build the dock lighting", locked) is None,
        "unrelated work passes the guard")
_expect(design.conflicts_locked("anything", []) is None,
        "no locked decisions -> no guard")
_expect(design.conflicts_locked("", locked) is None,
        "empty step text -> no guard")


# ---------------------------------------------------------------------------
# 6. Locking lives on the project state
# ---------------------------------------------------------------------------

project_state = importlib.import_module("agent.project_state")
st = project_state.ProjectState(goal="t")
st.lock_decision("No conventional health bar.", "uncertainty is the horror")
st.note_decision("prefer cold color palette")
_expect(st.locked_decisions() == ["No conventional health bar."],
        "locked_decisions returns only LOCKED entries")
_expect(st.decisions_ledger[1]["status"] == "OPEN",
        "note_decision stays OPEN")
# Round-trip through to_dict/from_dict (v0.2 fields persist)
st.design = d
st.record_cycle({"cycle": 1, "verdict": "WEAK", "action": "POLISH"})
st.phase = "BUILDING"
st.project_id = "test-1"
st2 = project_state.ProjectState.from_dict(st.to_dict())
_expect(st2.design.get("concept") == d["concept"],
        "design survives the round-trip")
_expect(st2.locked_decisions() == st.locked_decisions(),
        "decisions ledger survives the round-trip")
_expect(st2.cycles and st2.phase == "BUILDING"
        and st2.project_id == "test-1",
        "cycles/phase/project_id survive the round-trip")
# v0.1 file tolerance
old = project_state.ProjectState.from_dict({"goal": "old"})
_expect(old.design == {} and old.phase == "PLANNING",
        "v0.1 state files load without the new fields")


print("\nAll design-document tests passed.")
