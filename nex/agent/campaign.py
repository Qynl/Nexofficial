"""Campaigns — fully autonomous, long-running game creation.

One goal -> one TaskGraph -> one run is a SCENE. A game is a CAMPAIGN: a
roadmap of milestones, each planned + built + verified + judged through the
same canonical pipeline (run_agent_goal -> AutonomousAgent), with a
checkpoint after every milestone so a crash/restart RESUMES instead of
starting over.

Flow:
  1. ROADMAP — the LLM splits the goal into ordered milestones
     ({"milestones":[{"title","goal","done_when"},...]}). Without an LLM
     the whole goal becomes a single milestone (campaign = one run).
  2. LOOP — per milestone: emit agent.milestone_started, run the canonical
     pipeline, emit agent.milestone_completed with the honest report,
     checkpoint progress. A failed milestone does NOT abort the campaign:
     its reason is carried into the next milestone's planning context.
  3. RESUME — run_campaign(resume=True) reloads the last campaign
     checkpoint and skips milestones already COMPLETED, so long-running
     builds survive restarts.

Bounded by max_milestones; the model never runs unbounded.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

CHECKPOINT_FILE = os.path.join(
    os.path.expanduser("~"), ".nex", "campaign_checkpoint.json")

ROADMAP_SYSTEM = (
    "You are Nex's campaign planner. Split a game-creation goal into 2-6 "
    "ordered milestones. Each milestone must be independently achievable "
    "with MCP tools and independently verifiable. Reply with ONLY JSON: "
    '{"milestones": [{"title": "...", "goal": "...", '
    '"done_when": "observable, checkable condition"}]}')


def plan_roadmap(goal: str, llm: Optional[Callable],
                 max_milestones: int = 6) -> List[Dict[str, str]]:
    """LLM roadmap (validated) or single-milestone fallback."""
    if llm is None:
        return [{"title": goal[:60], "goal": goal, "done_when": ""}]
    try:
        reply = llm([
            {"role": "system", "content": ROADMAP_SYSTEM},
            {"role": "user", "content": (
                "Goal: %s\nAt most %d milestones. JSON only."
                % (goal, max_milestones))},
        ])
        from agent.judges import _extract_json
        obj = _extract_json(reply or "")
        items = obj.get("milestones") if isinstance(obj, dict) else None
        out = []
        for m in (items or [])[:max_milestones]:
            if isinstance(m, dict) and (m.get("goal") or m.get("title")):
                out.append({
                    "title": str(m.get("title") or m.get("goal"))[:80],
                    "goal": str(m.get("goal") or m.get("title")),
                    "done_when": str(m.get("done_when") or ""),
                })
        if out:
            return out
    except Exception:  # noqa: BLE001
        pass
    return [{"title": goal[:60], "goal": goal, "done_when": ""}]


def _save_progress(payload: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(CHECKPOINT_FILE), exist_ok=True)
        tmp = CHECKPOINT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, CHECKPOINT_FILE)
    except OSError:
        pass


def load_progress() -> Optional[Dict[str, Any]]:
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def run_campaign(goal: str,
                 bus: Optional[Callable] = None,
                 llm_call: Optional[Callable] = None,
                 llm_reachable: bool = False,
                 max_milestones: int = 6,
                 resume: bool = True) -> Dict[str, Any]:
    """Run (or resume) a full campaign through the canonical pipeline.

    `run_agent_goal` is resolved lazily from agent.server_run so tests can
    patch it and so the import graph stays light.
    """
    import time as _time
    from agent import server_run as _sr

    def _emit(evt: Dict[str, Any]) -> None:
        if bus is not None:
            evt = dict(evt, ts=_time.time())
            try:
                bus(evt)
            except Exception as exc:  # noqa: BLE001
                import sys as _sys
                _sys.stderr.write("campaign emit failed: %r\n" % (exc,))

    llm = llm_call if llm_reachable else None

    # --- resume: skip milestones already completed -----------------------
    done_titles: Dict[str, Any] = {}
    if resume:
        prev = load_progress()
        if prev and prev.get("goal") == goal:
            done_titles = {m.get("title"): m.get("status")
                           for m in prev.get("milestones", [])
                           if m.get("status") == "COMPLETED"}

    roadmap = plan_roadmap(goal, llm, max_milestones=max_milestones)
    _emit({"type": "agent.campaign_started", "goal": goal,
           "milestones": roadmap,
           "resumed": sorted(done_titles.keys())})

    results: List[Dict[str, Any]] = []
    prior_notes: List[str] = []
    for i, m in enumerate(roadmap):
        if done_titles.get(m["title"]) == "COMPLETED":
            results.append({"title": m["title"], "status": "COMPLETED",
                            "skipped": True, "resumed": True})
            _emit({"type": "agent.milestone_completed", "index": i,
                   "title": m["title"], "status": "COMPLETED",
                   "resumed": True})
            continue

        mgoal = m["goal"]
        if m.get("done_when"):
            mgoal += " (done when: %s)" % m["done_when"]
        if prior_notes:
            mgoal += ("\n\nEarlier milestones in this campaign: "
                      + "; ".join(prior_notes[-3:]))

        _emit({"type": "agent.milestone_started", "index": i,
               "title": m["title"], "goal": mgoal,
               "milestone": i + 1, "of": len(roadmap)})
        try:
            result = _sr.run_agent_goal(
                mgoal, bus=bus, llm_call=llm_call,
                llm_reachable=llm_reachable, mode="build")
            report = (result or {}).get("report") or {}
            status = report.get("status", "UNKNOWN")
        except Exception as exc:  # noqa: BLE001
            result = {"error": repr(exc)}
            report = {}
            status = "ERROR"

        note = "%s -> %s" % (m["title"], status)
        if report.get("missing"):
            note += (" (missing: "
                     + ", ".join(sorted({x.get("tool", "?")
                                         for x in report["missing"]}))
                     + ")")
        prior_notes.append(note)
        results.append({"title": m["title"], "status": status,
                        "skipped": False, "report": report,
                        "missing": report.get("missing", [])})
        _emit({"type": "agent.milestone_completed", "index": i,
               "title": m["title"], "status": status,
               "result": {"status": status}})

        # Checkpoint after EVERY milestone — crash-safe resume.
        _save_progress({"goal": goal, "milestones": results,
                        "roadmap": roadmap})

    completed = sum(1 for r in results if r["status"] == "COMPLETED")
    summary = {
        "goal": goal,
        "milestones_total": len(roadmap),
        "milestones_completed": completed,
        "status": "COMPLETED" if completed == len(roadmap) else "PARTIAL",
        "results": results,
    }
    _save_progress({"goal": goal, "milestones": results, "roadmap": roadmap,
                    "summary": summary})
    _emit({"type": "agent.campaign_done", "summary": summary})
    return summary
