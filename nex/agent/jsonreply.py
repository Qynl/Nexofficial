"""Extract structured JSON from model replies.

Models wrap JSON in prose and fences, truncate it, or append stray
punctuation. These helpers pull the first well-formed JSON object out
of a reply, with brace-balanced recovery for the common cases.

Every consumer of model output (planner, evaluator, diagnose, the chat
ACT directive) goes through here — deterministic parsing before use,
always.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, Optional

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _scan_objects(reply: str) -> Iterator[Dict[str, Any]]:
    """Every balanced {...} object in `reply`, in a single O(n) pass.

    A stack of open-brace positions (outside of string literals) is walked
    once; each closing '}' resolves the innermost still-open '{', yielding
    one candidate object per close. A '{' that never closes — stray prose
    mentioning a brace, a truncated object the model got cut off mid-way
    through — just sits on the stack and is never revisited: it cannot
    hide a valid object that follows it, and it cannot turn a long reply
    with many stray braces into quadratic work the way re-scanning from
    every '{' in turn would.
    """
    stack = []
    in_str = False
    esc = False
    for i, c in enumerate(reply):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            stack.append(i)
        elif c == "}":
            if not stack:
                continue
            start = stack.pop()
            try:
                obj = json.loads(reply[start:i + 1])
                if isinstance(obj, dict):
                    yield obj
            except json.JSONDecodeError:
                pass


def extract_json(reply: str) -> Optional[Dict[str, Any]]:
    """The first JSON object in the reply, or None."""
    if not reply:
        return None
    m = _FENCE_RE.search(reply)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    for obj in _scan_objects(reply):
        return obj
    return None


def extract_json_with_key(reply: str, key: str) -> Optional[Dict[str, Any]]:
    """The first JSON object in the reply that carries `key`."""
    if not reply:
        return None
    m = _FENCE_RE.search(reply)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict) and key in obj:
                return obj
        except json.JSONDecodeError:
            pass
    for obj in _scan_objects(reply):
        if key in obj:
            return obj
    return None
