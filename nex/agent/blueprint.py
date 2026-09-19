"""The BLUEPRINT: what Nex will build, with or without an engine.

A blocked run used to say exactly one thing — "connect an editor". That is
honest, but it throws away everything the Director already knows: the
systems, their build order, the proven structure of each one, the success
criteria, the tests that would prove them, and the MCP capability each
step needs.

The blueprint is that knowledge, made concrete and saved with the project.
It is deliberately NOT a fake plan: it never claims work happened, it
names the required capability per step (tool name when the live catalog
has one, otherwise the capability the operator must connect), and it
carries the same checklists the verification gate would use once the
engine IS connected.

Design rules:
  * Deterministic — no model call, no guessing. Built from the GamePlan
    (recipes) + the live tool catalog (names only).
  * Honest — a step whose capability is missing is marked `blocked`, not
    silently dropped or invented as a tool call.
  * Useful without MCP — the document is the part of "make me a game"
    that does not need an engine, so the artist/developer can start now.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Capability classes a step can need, in PRIORITY order (first match
# wins). These are the *intents* the recipe library uses ("create the
# controllable character", "run the game and observe"), not tool names.
# Whole-word matching only: "playable" is not "play", and a rule that
# fires on a prefix would misplace half the catalog.
_CAP_RULES = (
    ("verify", ("verify", "validate", "validate_assets", "check", "assert",
                "test", "tests", "measure")),
    ("observe", ("screenshot", "capture", "log", "logs", "console",
                 "observe", "inspect", "profile", "metrics", "telemetry",
                 "performance", "monitor", "output")),
    ("build", ("build", "compile", "package", "bake", "cook", "deploy",
               "export")),
    ("run", ("run", "launch", "play", "playtest", "simulate", "pie",
             "session", "runtime")),
    ("write_code", ("script", "code", "luau", "lua", "python", "file",
                    "module", "source")),
    ("create", ("create", "add", "spawn", "generate", "import", "insert",
                "duplicate", "instantiate", "place", "make", "new")),
    ("configure", ("set", "update", "configure", "apply", "assign", "wire",
                   "connect", "adjust", "attach", "parent", "tune", "enable",
                   "disable")),
)


# Tool-name vocabulary per capability class, REAL engine first. The generic
# intent rules below answer "which word of the intent matches a tool?", this
# table answers the better question: "which of the connected tools is the
# tool an engine developer would actually use for this?" — so a plan names
# `play_solo`/`pie_start` for a run step instead of whatever happens to
# share a word with the sentence.
_TOOL_TOKENS: Dict[str, tuple] = {
    "run": ("play_solo", "pie_start", "start_pie", "play_in_editor",
            "start_play", "run_game", "launch", "playtest", "play"),
    "observe": ("take_screenshot", "screenshot", "capture", "get_output_log",
                "get_console_output", "get_logs", "console", "logs",
                "profile", "metrics"),
    "verify": ("verify_game", "verify_asset", "validate", "check", "assert",
               "automation_test", "run_tests", "measure"),
    "build": ("compile_blueprint", "build_project", "package_project",
              "compile", "build", "package", "bake", "cook"),
    "create": ("create_part", "create_instance", "create_character",
               "create_actor", "spawn_actor", "create_blueprint",
               "create_level", "insert_model", "create", "spawn", "insert",
               "add_component", "instantiate"),
    "configure": ("set_property", "set_actor_property", "set_component_property",
                  "set_actor_transform", "set_attributes", "set", "apply",
                  "assign", "parent", "tag", "weld"),
    "write_code": ("create_script", "edit_script", "set_script_source",
                   "execute_luau", "script", "code", "module", "source"),
}


def _words(text: str) -> List[str]:
    return [w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w]


def _rule_words() -> Dict[str, tuple]:
    return {cap: keys for cap, keys in _CAP_RULES}


def capability_for_intent(intent: str) -> str:
    """Which capability CLASS a step intent needs (deterministic).

    Whole-word rules in priority order; nothing matches => configure,
    which is the honest default (the step is an edit of something that
    already exists).
    """
    words = _words(intent)
    wordset = set(words)
    first = words[0] if words else ""
    for cap, keys in _CAP_RULES:
        if cap == "run":
            # "run" is ambiguous: recipe intents say "Run the game and
            # observe", while a step about movement input says
            # "walk/run/strafe". The run rule therefore needs the verb
            # UP FRONT, or a target word telling us what runs.
            if first in keys:
                return cap
            if wordset & set(keys) and wordset & {
                    "game", "project", "editor", "level", "simulation",
                    "build", "scene", "playtest"}:
                return cap
            continue
        if wordset & set(keys):
            return cap
    return "configure"


def _match_tool(cap: str, intent: str, tool_names: List[str]) -> str:
    """The live tool that would serve this step, if the catalog has one.

    Name-level matching only (no model): the blueprint says "this step
    needs a screenshot capability, and `engine.screenshot` provides it" —
    or "nothing connected provides it".
    """
    # 1) the real-engine vocabulary first (exact tool-name shapes)
    tokens = _TOOL_TOKENS.get(cap, ())
    best = ""
    for tok in tokens:
        for name in tool_names or []:
            if tok in name.lower():
                if not best or len(name) < len(best):
                    best = name
        if best:
            return best
    # 2) the generic intent vocabulary
    keys = _rule_words().get(cap, ())
    for name in tool_names or []:
        low = name.lower()
        for k in keys:
            if k in low:
                # prefer the shortest matching name (least decorated)
                if not best or len(name) < len(best):
                    best = name
                break
    if best:
        return best
    # Intent-word fallback ONLY where the object is the tool name: a
    # create step ("create the controllable character") is often served by
    # `create_character` even when the catalog has no generic create tool.
    # A CONFIGURE step must never be answered with a create tool — that
    # would build a second character instead of wiring the first one.
    if cap not in ("create", "write_code"):
        return ""
    for w in _words(intent):
        if len(w) < 4:
            continue
        for name in tool_names or []:
            if w in name.lower():
                return name
    return ""


def build_blueprint(goal: str,
                    game_plan: Any,
                    tool_names: Optional[List[str]] = None,
                    design: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Structured, deterministic build plan. Never claims work was done."""
    tools = list(tool_names or [])
    systems: List[Dict[str, Any]] = []
    for s in (game_plan.systems if game_plan is not None else []):
        steps = []
        quality = [str(q) for q in (getattr(s, "quality", None) or [])]
        for st in (getattr(s, "steps", None) or []):
            intent = st.get("intent", "") if isinstance(st, dict) else str(st)
            cap = capability_for_intent(intent)
            tool = _match_tool(cap, intent, tools)
            steps.append({
                "intent": intent,
                "needs": cap,
                "tool": tool,
                "evidence": (st.get("evidence", "")
                             if isinstance(st, dict) else ""),
                "status": "ready" if tool else "blocked",
            })
        systems.append({
            "id": s.id,
            "title": s.title,
            "layer": s.layer,
            "why": s.why,
            "recipe": s.recipe,
            "provides": s.provides,
            "risks": list(getattr(s, "risks", []) or []),
            "checklist": list(getattr(s, "checklist", []) or []),
            # The standard, not the pass/fail criteria: what "good" means.
            "quality": quality,
            "steps": steps,
            "ready": bool(steps) and all(x["status"] == "ready"
                                         for x in steps),
        })
    missing_caps = sorted({x["needs"] for s in systems for x in s["steps"]
                           if not x["tool"]})
    return {
        "goal": goal,
        "game": getattr(game_plan, "game", "") if game_plan else "",
        "source": getattr(game_plan, "source", "") if game_plan else "",
        "genre": (design or {}).get("genre", ""),
        "systems": systems,
        "tool_count": len(tools),
        "missing_capabilities": missing_caps,
        "tests": test_plan(systems),
    }


def test_plan(systems: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """What would PROVE each system once an engine is connected.

    Every criterion is paired with the observation that would prove it —
    the same pairing the reviewer and the completion gate use, so the
    plan and the verification cannot drift apart.
    """
    out: List[Dict[str, str]] = []
    for s in systems or []:
        # Quality bars first: they are what separates a prototype from a
        # game, and they are checkable by looking at the running game.
        for q in s.get("quality", []) or []:
            out.append({"system": s.get("id", ""), "kind": "quality",
                        "criterion": str(q),
                        "proves": "judged against the running game "
                                  "(screenshot / log / feel)"})
        for c in s.get("checklist", []) or []:
            out.append({"system": s.get("id", ""),
                        "criterion": c,
                        "kind": "criterion",
                        "prove_by": "run the game and observe: " + c,
                        "status": "unproven"})
    return out


def blueprint_markdown(bp: Dict[str, Any]) -> str:
    """Render the blueprint as a document (saved with the project)."""
    lines = ["# Build blueprint: %s" % (bp.get("goal") or "game"),
             "",
             "Genre: %s   Systems: %d   Connected tools: %d"
             % (bp.get("genre") or "unspecified", len(bp.get("systems") or []),
                bp.get("tool_count", 0)),
             "",
             "This is a PLAN. Nothing has been built or verified yet.", ""]
    for s in bp.get("systems") or []:
        lines.append("## %s — %s" % (s.get("title"), s.get("id")))
        if s.get("provides"):
            lines.append("_Provides:_ %s" % s["provides"])
        if s.get("why"):
            lines.append("_Why:_ %s" % s["why"])
        lines.append("")
        lines.append("| step | needs | tool |")
        lines.append("|---|---|---|")
        for st in s.get("steps") or []:
            lines.append("| %s | %s | %s |"
                         % (st.get("intent"), st.get("needs"),
                            st.get("tool") or "_not connected_"))
        if s.get("checklist"):
            lines.append("")
            lines.append("Done only when ALL of these are proven:")
            for c in s["checklist"]:
                lines.append("- [ ] %s" % c)
        if s.get("quality"):
            lines.append("")
            lines.append("Quality bar (a build that misses these is not "
                         "finished):")
            for q in s["quality"]:
                lines.append("- [ ] %s" % q)
        lines.append("")
    missing = bp.get("missing_capabilities") or []
    if missing:
        lines += ["## Missing capabilities", "",
                  "Connect an MCP server providing: " + ", ".join(missing),
                  ""]
    tests = bp.get("tests") or []
    if tests:
        quality = [t for t in tests if t.get("kind") == "quality"]
        functional = [t for t in tests if t.get("kind") != "quality"]
        lines += ["## Test plan", "",
                  "Every check below is made against the RUNNING game — a "
                  "tool call returning is not a result.", ""]
        for t in functional:
            lines.append("- **%s** — %s" % (t["system"], t["criterion"]))
        if quality:
            lines += ["", "Quality checks (judged, not just measured):", ""]
            for t in quality:
                lines.append("- **%s** — %s" % (t["system"], t["criterion"]))
        lines.append("")
    return "\n".join(lines)
