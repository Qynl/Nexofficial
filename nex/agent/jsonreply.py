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
from typing import Any, Dict, Optional

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _balanced_object(text: str, start: int) -> Optional[str]:
    """The substring of `text` from `start` holding one balanced {...}."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
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
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


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
    idx = reply.find("{")
    while idx >= 0:
        chunk = _balanced_object(reply, idx)
        if chunk is None:
            break
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx = reply.find("{", idx + 1)
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
    idx = reply.find("{")
    while idx >= 0:
        chunk = _balanced_object(reply, idx)
        if chunk is None:
            break
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict) and key in obj:
                return obj
        except json.JSONDecodeError:
            pass
        idx = reply.find("{", idx + 1)
    return None
