"""Quality judges for autonomous game builds (STAGE 19 / STAGE 24).

After a run, the judges answer: \"is this actually a good, fun, AAA-feeling
game, or just something that technically runs?\" They produce a verdict with
numeric scores and concrete suggestions. The orchestrator can use a failing
verdict to re-plan and rebuild (one improvement round), so the agent ships
quality instead of the first thing that compiles.

Two layers:

  * Rule judge   — cheap, deterministic, always runs. Inspects the plan +
                  completion report for AAA ingredients (core loop, feedback,
                  art, audio, progression, polish) and penalizes missing
                  capabilities / verification gaps.
  * LLM judge   — optional. Asks the model to rate fun/quality/playability
                  and list concrete fixes. Parsed defensively; if it can't be
                  parsed we trust the rule judge.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from agent.events import STATUS_COMPLETED

# Ingredient keywords -> the 'fun/quality' dimension they satisfy.
_CORE_LOOP = ("gameplay", "script", "logic", "simulate", "physics", "loop",
              "mechanic", "rule", "spawn", "enemy", "player", "controller")
_FEEDBACK = ("audio", "sound", "hud", "ui", "feedback", "particle", "fx",
             "vfx", "visual", "camera", "shake")
_ART = ("asset", "mesh", "model", "material", "texture", "skin", "anim",
        "sprite", "light", "environment", "world", "level", "prop")
_AUDIO = ("audio", "music", "sound", "sfx", "voice")
_PROGRESSION = ("score", "progres", "level", "quest", "reward", "unlock",
                "win", "lose", "goal", "challenge", "difficulty")
_POLISH = ("shader", "post", "bloom", "gi", "reflection", "shadow", "smooth",
           "juice", "tween", "ease", "polish")


def _has(tokens: List[str], keywords) -> bool:
    blob = " ".join(tokens).lower()
    return any(k in blob for k in keywords)


def rule_judge(goal: str, report, plan: Optional[Dict[str, Any]],
               registry=None) -> Dict[str, Any]:
    """Deterministic quality estimate from the plan + completion report."""
    completed = list(report.completed)
    failed = list(report.failed)
    skipped = list(report.skipped)
    plan_steps = (plan or {}).get("steps", []) if isinstance(plan, dict) else []
    step_tools = [s.get("tool", "") for s in plan_steps]
    all_tokens = completed + step_tools + [goal]

    quality = 0.5  # baseline
    notes: List[str] = []
    dimensions = {}

    if report.status == STATUS_COMPLETED:
        quality += 0.15
    else:
        notes.append("run did not complete (status=%s)" % report.status)

    if _has(all_tokens, _CORE_LOOP):
        dimensions["core_loop"] = 1.0
        quality += 0.10
    else:
        dimensions["core_loop"] = 0.0
        notes.append("no clear core game loop found")

    if _has(all_tokens, _FEEDBACK):
        dimensions["feedback"] = 1.0
        quality += 0.08
    else:
        dimensions["feedback"] = 0.0
        notes.append("add player feedback (audio/visual)")

    if _has(all_tokens, _ART):
        dimensions["art"] = 1.0
        quality += 0.06
    else:
        dimensions["art"] = 0.0
        notes.append("add art/assets for visual identity")

    if _has(all_tokens, _AUDIO):
        dimensions["audio"] = 1.0
        quality += 0.05
    else:
        dimensions["audio"] = 0.0
        notes.append("add audio/music for feel")

    if _has(all_tokens, _PROGRESSION):
        dimensions["progression"] = 1.0
        quality += 0.06
    else:
        dimensions["progression"] = 0.0
        notes.append("add goal/score/progression so it's a game, not a tech demo")

    if _has(all_tokens, _POLISH):
        dimensions["polish"] = 1.0
        quality += 0.05
    else:
        dimensions["polish"] = 0.0
        notes.append("add polish/juice (tweens, fx, camera)")

    if failed or skipped:
        quality -= min(0.15, 0.03 * (len(failed) + len(skipped)))
        notes.append("%d failed / %d skipped step(s)" % (len(failed), len(skipped)))

    quality = max(0.0, min(1.0, quality))
    return {
        "rule": True,
        "score": round(quality, 3),
        "dimensions": dimensions,
        "notes": notes,
    }


def llm_judge(goal: str, report, plan: Optional[Dict[str, Any]],
              llm: Callable[[List[Dict[str, str]], str]]):
    """Ask the model to critique the plan for fun + quality. Defensive parse."""
    plan_text = json_safe(plan)
    report_text = ("completed=%s failed=%s skipped=%s status=%s"
                   % (report.completed, report.failed, report.skipped, report.status))
    messages = [
        {"role": "system", "content": (
            "You are a senior game designer reviewing an AI-built game plan. "
            "Rate it 1-10 for FUN, QUALITY, and PLAYABILITY. Then list up to 3 "
            "concrete, specific improvements. Reply as JSON only: "
            '{"fun": <int>, "quality": <int>, "playability": <int>, '
            '"suggestions": ["...", "..."]}')},
        {"role": "user", "content": (
            "Goal: %s\nPlan:\n%s\nResult: %s\n\nReview it now."
            % (goal, plan_text, report_text))},
    ]
    try:
        reply = llm(messages)
        obj = _extract_json(reply)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    fun = _clamp10(obj.get("fun"))
    qual = _clamp10(obj.get("quality"))
    play = _clamp10(obj.get("playability"))
    sugg = obj.get("suggestions")
    if not isinstance(sugg, list):
        sugg = []
    avg = (fun + qual + play) / 3.0 / 10.0 if (fun or qual or play) else None
    return {
        "fun": fun, "quality": qual, "playability": play,
        "score": round(avg, 3) if avg is not None else None,
        "suggestions": [str(s) for s in sugg[:3]],
    }


def judge(goal: str, report, plan: Optional[Dict[str, Any]],
          llm: Optional[Callable] = None, registry=None) -> Dict[str, Any]:
    """Combine rule + optional LLM judges into one verdict."""
    r = rule_judge(goal, report, plan, registry)
    verdict = {
        "goal": goal,
        "status": report.status,
        "score": r["score"],
        "dimensions": r["dimensions"],
        "suggestions": list(r["notes"]),
        "pass": r["score"] >= 0.6,
        "judges": {"rule": r},
    }
    if llm is not None:
        lj = llm_judge(goal, report, plan, llm)
        if lj is not None:
            verdict["judges"]["llm"] = lj
            if lj.get("score") is not None:
                # Blend rule and LLM; the LLM understands 'fun' better.
                verdict["score"] = round(0.5 * r["score"] + 0.5 * lj["score"], 3)
            verdict["suggestions"] = (r["notes"] + lj.get("suggestions", []))[:5]
            verdict["pass"] = verdict["score"] >= 0.6
    return verdict


# ---- helpers ---------------------------------------------------------------

def _clamp10(v) -> Optional[int]:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return max(1, min(10, n))


def _extract_json(text: str) -> Any:
    import json as _json
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        return _json.loads(text[start:end])
    except _json.JSONDecodeError:
        return None


def json_safe(obj) -> str:
    import json as _json
    try:
        return _json.dumps(obj, ensure_ascii=False)[:1500]
    except Exception:  # noqa: BLE001
        return str(obj)[:1500]
