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

from agent.mcp_production import redact_untrusted_text

DEFAULT_RESULT_BUDGET = 1600     # chars per stored observation
DEFAULT_KEEP_RECENT = 6          # full observations kept for prompts
DEFAULT_PREVIEW_BUDGET = 280     # chars for the UI/event preview

_SENSITIVE_KEYS = {
    "api_key", "apikey", "access_token", "refresh_token", "password",
    "secret", "authorization", "private_key", "credential", "credentials",
}
_BINARY_KEYS = {"data", "blob", "bytes", "base64", "image_data", "audio_data"}


def _context_safe_value(value: Any, depth: int = 0) -> Any:
    """Bound structured output before serialization into model context."""
    if depth > 12:
        return "[depth limit]"
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in list(value.items())[:256]:
            low = str(key).lower().replace("-", "_")
            if low in _SENSITIVE_KEYS:
                out[str(key)[:100]] = "[REDACTED]"
            elif low in _BINARY_KEYS and isinstance(item, (str, bytes)):
                out[str(key)[:100]] = "[binary payload: %d bytes]" % len(item)
            else:
                out[str(key)[:100]] = _context_safe_value(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_context_safe_value(item, depth + 1) for item in value[:256]]
    if isinstance(value, bytes):
        return "[binary payload: %d bytes]" % len(value)
    return value


def truncate_text(text: str, budget: int = DEFAULT_RESULT_BUDGET) -> Tuple[str, bool]:
    """Redact and clip untrusted output while preserving both head and tail.

    Build and editor logs commonly put the summary at the top and the actual
    failure at the bottom. Head-only clipping hid exactly the evidence needed
    for repair. Credential-shaped values are removed before any model sees the
    observation.
    """
    text = redact_untrusted_text(text)
    if len(text) <= budget:
        return text, False
    marker_budget = 56
    usable = max(80, budget - marker_budget)
    head_size = max(48, round(usable * 0.66))
    tail_size = max(32, usable - head_size)
    head = text[:head_size]
    tail = text[-tail_size:]
    head_line = head.rfind("\n")
    if head_line > head_size // 2:
        head = head[:head_line]
    tail_line = tail.find("\n")
    if 0 <= tail_line < tail_size // 2:
        tail = tail[tail_line + 1:]
    omitted = max(0, len(text) - len(head) - len(tail))
    return (head + "\n… (+%d chars omitted; tail preserved) …\n" % omitted
            + tail), True


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
            for c in content[:100]:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(str(c.get("text", "")))
                elif isinstance(c, dict) and c.get("type") == "image":
                    data = c.get("data")
                    size = len(data) if isinstance(data, str) else 0
                    parts.append("[image mime=%s encoded_bytes=%d]" %
                                 (str(c.get("mimeType") or "unknown")[:80],
                                  size))
                elif isinstance(c, dict) and c.get("type") == "audio":
                    data = c.get("data")
                    size = len(data) if isinstance(data, str) else 0
                    parts.append("[audio mime=%s encoded_bytes=%d]" %
                                 (str(c.get("mimeType") or "unknown")[:80],
                                  size))
                elif isinstance(c, dict) and c.get("type") == "resource":
                    resource = c.get("resource") or {}
                    if isinstance(resource, dict):
                        uri = str(resource.get("uri") or "")[:240]
                        text = resource.get("text")
                        parts.append("[resource %s]%s" %
                                     (uri, "\n" + str(text)
                                      if text is not None else ""))
                    else:
                        parts.append("[resource content]")
                elif isinstance(c, dict):
                    # Never pour opaque image/blob payloads into model context.
                    safe = _context_safe_value(c)
                    parts.append(json.dumps(safe, ensure_ascii=False)[:400])
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
                json.dumps(_context_safe_value(result),
                           ensure_ascii=False, indent=1), budget)
        except (TypeError, ValueError):
            return truncate_text(str(result), budget)
    try:
        return truncate_text(json.dumps(_context_safe_value(result),
                                        ensure_ascii=False), budget)
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
