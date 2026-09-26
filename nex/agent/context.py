"""Bounded working memory for a run.

The failure mode this prevents: a long run that dumps every full tool
output into every model call until the context window is garbage. The
rule here is the opposite of a chat log:

  * every observation is TRUNCATED to a budget when recorded,
  * only the most recent `keep_recent` observations are shown in full,
  * everything older is compacted to one line,
  * the planner and evaluator always see: goal, step one-liners, the
    recent evidence window, and open failures. Never more.

Tool output is DATA. It is summarized, never obeyed.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_RESULT_BUDGET = 1600     # chars per stored observation
DEFAULT_KEEP_RECENT = 6          # full observations kept for prompts
DEFAULT_PREVIEW_BUDGET = 280     # chars for the UI/event preview


def truncate_text(text: str, budget: int = DEFAULT_RESULT_BUDGET) -> Tuple[str, bool]:
    """Clip `text` to `budget` chars, cutting at a line boundary when
    possible. Returns (text, truncated)."""
    if len(text) <= budget:
        return text, False
    cut = text[:budget]
    nl = cut.rfind("\n", 0, min(len(cut), budget // 2))
    if nl > budget // 3:
        cut = cut[:nl]
    return cut + "\n… (+%d chars truncated)" % (len(text) - len(cut)), True


def result_to_text(result: Any, budget: int = DEFAULT_RESULT_BUDGET) -> Tuple[str, bool]:
    """Render any tool result as bounded text.

    MCP results are dicts like {"content": [{"type": "text", "text": ...}],
    "isError": false} — but we accept anything (strings, lists, nested
    JSON) because servers vary.
    """
    if result is None:
        return "(no result)", False
    if isinstance(result, str):
        return truncate_text(result, budget)
    # MCP content envelope
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts: List[str] = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(str(c.get("text", "")))
                elif isinstance(c, dict):
                    parts.append(json.dumps(c, ensure_ascii=False)[:400])
                else:
                    parts.append(str(c))
            if parts:
                joined = "\n".join(p for p in parts if p)
                if result.get("isError"):
                    joined = "(server reported an error) " + joined
                return truncate_text(joined, budget)
        if "error" in result:
            return truncate_text(
                "error: " + json.dumps(result["error"], ensure_ascii=False),
                budget)
        try:
            return truncate_text(
                json.dumps(result, ensure_ascii=False, indent=1), budget)
        except (TypeError, ValueError):
            return truncate_text(str(result), budget)
    try:
        return truncate_text(json.dumps(result, ensure_ascii=False), budget)
    except (TypeError, ValueError):
        return truncate_text(str(result), budget)


def result_preview(result: Any, budget: int = DEFAULT_PREVIEW_BUDGET) -> str:
    """A short single-line preview for UI events."""
    text, _ = result_to_text(result, budget)
    text = " ".join(text.split())
    return text[:budget]


class Observation:
    __slots__ = ("step", "tool", "server", "text", "ok")

    def __init__(self, step: str, tool: str, server: str,
                 text: str, ok: bool) -> None:
        self.step = step
        self.tool = tool
        self.server = server
        self.text = text
        self.ok = ok

    def one_line(self) -> str:
        first = self.text.strip().split("\n")[0]
        return "%s (%s) — %s" % (self.step, self.tool, first[:160])


class RunContext:
    """Everything the model is allowed to remember about a run."""

    def __init__(self, goal: str,
                 result_budget: int = DEFAULT_RESULT_BUDGET,
                 keep_recent: int = DEFAULT_KEEP_RECENT) -> None:
        self.goal = goal
        self.result_budget = result_budget
        self.keep_recent = keep_recent
        self.observations: List[Observation] = []
        self.failures: List[str] = []        # one-liners, capped
        self.max_failures = 20

    # ----- recording ----------------------------------------------------------

    def observe(self, step: str, tool: str, server: str,
                result: Any, ok: bool = True) -> str:
        text, _ = result_to_text(result, self.result_budget)
        obs = Observation(step, tool, server, text, ok)
        self.observations.append(obs)
        return text

    def record_failure(self, step: str, tool: str, error: str) -> None:
        line = "%s (%s): %s" % (step, tool or "?",
                                " ".join(str(error).split())[:200])
        if line not in self.failures:
            self.failures.append(line)
        if len(self.failures) > self.max_failures:
            self.failures = self.failures[-self.max_failures:]

    # ----- rendering ---------------------------------------------------------

    def step_lines(self, graph) -> List[str]:
        """One line per step: title — status — key fact."""
        out: List[str] = []
        for t in graph.all():
            fact = ""
            if t.status == "success" and t.result is not None:
                fact = result_preview(t.result, 120)
            elif t.error:
                fact = " ".join(str(t.error).split())[:120]
            line = "- [%s] %s" % (t.status, t.name)
            if t.tool:
                line += " (%s)" % t.tool
            if fact:
                line += " — " + fact
            out.append(line)
        return out

    def evidence_block(self) -> str:
        """Recent observations in full, older ones as one-liners."""
        if not self.observations:
            return "(no observations yet)"
        parts: List[str] = []
        older = self.observations[:-self.keep_recent] \
            if len(self.observations) > self.keep_recent else []
        if older:
            parts.append("Earlier (compacted):")
            parts.extend("  " + o.one_line() for o in older[-8:])
        parts.append("Recent:")
        for o in self.observations[-self.keep_recent:]:
            parts.append("  --- %s (%s via %s) ---" % (o.step, o.tool, o.server))
            parts.append("  " + o.text.replace("\n", "\n  "))
        return "\n".join(parts)

    def failure_block(self) -> str:
        if not self.failures:
            return ""
        return "\n".join("- " + f for f in self.failures[-8:])

    def size_chars(self) -> int:
        return sum(len(o.text) for o in self.observations)
