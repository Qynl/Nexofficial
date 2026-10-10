"""Compact, structured, persistent project memory.

The failure mode this prevents: a multi-run production (the same
conversation, resumed hours or days later) either knows nothing about what
happened before, or — worse — a naive fix would dump the entire prior
conversation/run history back into every prompt until the context window is
garbage. Neither is acceptable for a long production.

Instead this module produces a small, bounded, JSON-serializable summary of
what a run actually proved, built ONLY from structural evidence already
computed elsewhere in this codebase (completed production stages, failed
tasks + their agent/failures.py classification, tool success/failure tallies,
agent/visual.py's defect board) — never from asking a model to "remember"
or inventing semantic understanding this module cannot actually verify.

This is cross-RUN memory within one conversation, not a mid-run checkpoint:
agent/loop.py still builds a fresh TaskGraph and re-inspects the live project
every run. Memory is advisory context for the planner ("here is what earlier
runs in this conversation found"), never a substitute for re-verifying
against the actual connected MCP servers.

Persistence itself lives at the server layer (server.py + store.py), never
here and never in agent/loop.py — agent/ must not import store
(tests/test_architecture.py enforces this). This module only produces and
renders plain dicts.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

MAX_LIST = 20
MAX_RISKS = 8
MAX_PERF_NOTES = 10


def empty_memory(conversation_id: str = "") -> Dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "runs_recorded": 0,
        "updated_at": 0.0,
        "milestones_done": [],
        "milestones_open": [],
        "known_bugs": [],
        "resolved_bugs": [],
        "visual_issues_open": [],
        "visual_issues_resolved": [],
        "performance_notes": [],
        "risks": [],
        "tool_success_counts": {},
        "tool_failure_counts": {},
    }


def _dedup_tail(items: Sequence[str], limit: int) -> List[str]:
    """Preserve order, drop duplicates, keep the most recent `limit`."""
    seen = set()
    out: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out[-limit:] if limit else out


def merge_from_run(previous: Optional[Dict[str, Any]], *,
                   conversation_id: str,
                   milestones_done: Sequence[str] = (),
                   milestones_open: Sequence[str] = (),
                   failed: Sequence[Dict[str, Any]] = (),
                   tool_outcomes: Sequence[Dict[str, Any]] = (),
                   visual_public: Optional[Dict[str, Any]] = None,
                   performance_evidence: Sequence[str] = (),
                   max_list: int = MAX_LIST) -> Dict[str, Any]:
    """Fold one run's real evidence into the running project memory.

    `failed`: report["failed"]-shaped dicts (tool, error, failure_kind).
    `tool_outcomes`: [{"tool": str, "ok": bool}, ...] — one per executed
    task, used both for reliability tallies and to decide whether a
    previously-known bug's tool has since succeeded (the only signal this
    module uses to call a bug "likely resolved" — a heuristic, same
    honesty convention as agent/visual.py's likely_resolved).
    """
    prev = dict(previous) if previous else empty_memory(conversation_id)
    run_no = int(prev.get("runs_recorded", 0)) + 1

    done = _dedup_tail(
        list(prev.get("milestones_done", [])) + list(milestones_done),
        max_list)
    open_raw = _dedup_tail(
        list(prev.get("milestones_open", [])) + list(milestones_open),
        max_list)
    open_ = [m for m in open_raw if m not in done]

    prev_bugs = {b["key"]: b for b in prev.get("known_bugs", [])
                if isinstance(b, dict) and b.get("key")}
    current_keys = set()
    still_open: List[Dict[str, Any]] = []
    for f in failed:
        tool = str(f.get("tool") or "")
        kind = str(f.get("failure_kind") or "unknown")
        if not tool and not kind:
            continue
        key = "%s|%s" % (tool, kind)
        current_keys.add(key)
        summary = str(f.get("error") or "")[:160]
        prior = prev_bugs.get(key)
        if prior:
            entry = dict(prior)
            entry["occurrences"] = int(prior.get("occurrences", 1)) + 1
            entry["last_seen_run"] = run_no
            if summary:
                entry["summary"] = summary
        else:
            entry = {"key": key, "tool": tool, "failure_kind": kind,
                     "summary": summary, "first_seen_run": run_no,
                     "last_seen_run": run_no, "occurrences": 1}
        still_open.append(entry)

    succeeded_tools = {str(o.get("tool")) for o in tool_outcomes
                       if o.get("ok") and o.get("tool")}
    resolved = list(prev.get("resolved_bugs", []))
    for key, bug in prev_bugs.items():
        if key in current_keys:
            continue      # handled above, still failing
        if bug.get("tool") in succeeded_tools:
            resolved_entry = dict(bug)
            resolved_entry["resolved_at_run"] = run_no
            resolved.append(resolved_entry)
        else:
            # Neither re-failed nor re-proven this run; carry forward as
            # still-open rather than silently dropping known evidence.
            still_open.append(bug)

    known_bugs = still_open[-max_list:]
    resolved_bugs = resolved[-max_list:]

    risks = list(prev.get("risks", []))
    for bug in known_bugs:
        if int(bug.get("occurrences", 1)) >= 2:
            line = ("%s has failed %dx across separate runs (%s): %s" % (
                bug.get("tool") or "?", bug.get("occurrences"),
                bug.get("failure_kind"), bug.get("summary") or ""))[:220]
            if line not in risks:
                risks.append(line)
    risks = _dedup_tail(risks, MAX_RISKS)

    tool_success = dict(prev.get("tool_success_counts", {}))
    tool_failure = dict(prev.get("tool_failure_counts", {}))
    for o in tool_outcomes:
        tool = o.get("tool")
        if not tool:
            continue
        bucket = tool_success if o.get("ok") else tool_failure
        bucket[tool] = int(bucket.get(tool, 0)) + 1

    visual_public = visual_public or {}
    visual_open = list(visual_public.get("open") or [])[:max_list]
    prev_visual_resolved = [str(x) for x in
                            (prev.get("visual_issues_resolved") or [])]
    new_visual_resolved = [
        str(i.get("category") or i.get("label") or "")
        for i in (visual_public.get("likely_resolved") or []) if i]
    visual_resolved = _dedup_tail(
        prev_visual_resolved + new_visual_resolved, max_list)


    perf_notes = _dedup_tail(
        list(prev.get("performance_notes", [])) + list(performance_evidence),
        MAX_PERF_NOTES)

    return {
        "conversation_id": conversation_id or prev.get("conversation_id", ""),
        "runs_recorded": run_no,
        "updated_at": time.time(),
        "milestones_done": done,
        "milestones_open": open_,
        "known_bugs": known_bugs,
        "resolved_bugs": resolved_bugs,
        "visual_issues_open": visual_open,
        "visual_issues_resolved": visual_resolved,
        "performance_notes": perf_notes,
        "risks": risks,
        "tool_success_counts": tool_success,
        "tool_failure_counts": tool_failure,
    }


def to_prompt_block(memory: Optional[Dict[str, Any]], limit: int = 5,
                    char_budget: int = 2200) -> str:
    """Compact planner-facing rendering, or "" when there is nothing to say.

    Never includes raw tool output or conversation text — only the already-
    bounded structured fields merge_from_run() produced.
    """
    if not memory or not memory.get("runs_recorded"):
        return ""
    lines = [
        "PROJECT MEMORY (from %d earlier run(s) in this conversation — "
        "verify before trusting; the live project is the source of truth, "
        "this is only a compact pointer to what earlier runs found):"
        % int(memory.get("runs_recorded", 0)),
    ]
    done = memory.get("milestones_done") or []
    if done:
        lines.append("Milestones previously completed: " +
                     ", ".join(done[-limit:]))
    open_ = memory.get("milestones_open") or []
    if open_:
        lines.append("Milestones still open: " + ", ".join(open_[:limit]))
    bugs = sorted(memory.get("known_bugs") or [],
                 key=lambda b: b.get("occurrences", 1), reverse=True)
    if bugs:
        lines.append("Known open bugs (most recurring first):")
        for b in bugs[:limit]:
            lines.append("- %s (%s, seen %dx, last run #%s): %s" % (
                b.get("tool") or "?", b.get("failure_kind") or "unknown",
                b.get("occurrences", 1), b.get("last_seen_run", "?"),
                b.get("summary") or ""))
    resolved = memory.get("resolved_bugs") or []
    if resolved:
        lines.append("Bugs a later run found likely fixed (heuristic, not "
                     "re-verified): " +
                     ", ".join((b.get("tool") or "?") for b in resolved[-limit:]))
    vis_open = memory.get("visual_issues_open") or []
    if vis_open:
        lines.append("Open visual defects from earlier runs: " +
                     ", ".join((i.get("label") or i.get("category") or "?")
                              for i in vis_open[:limit]))
    risks = memory.get("risks") or []
    if risks:
        lines.append("Recurring risks across runs:")
        for r in risks[:limit]:
            lines.append("- " + r)
    perf = memory.get("performance_notes") or []
    if perf:
        lines.append("Performance notes carried from earlier runs: " +
                     " | ".join(perf[-limit:]))
    text = "\n".join(lines)
    if len(text) > char_budget:
        text = text[:char_budget] + "\n… (older memory truncated)"
    return text
