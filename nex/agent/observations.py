"""Runtime OBSERVATION (the AAA loop's eyes).

Build -> Run -> OBSERVE -> Understand -> Fix -> Verify.

An engine's MCP server exposes observation tools (launch, screenshot,
logs, performance, scene/state inspection). When Nex executes one and it
succeeds, the result is NOT just "ok: True" — it is EVIDENCE about the
running game. This module turns such results into structured
``Observation`` dicts that the critic can judge and the project memory
can keep.

Detection is by TOOL NAME semantics (the MCP tool names the engine chose),
so it works for Unreal/Roblox/Blender-style servers without hardcoding
any engine. Non-observation tools produce no observation — a build tool
saying "ok" is not runtime evidence.

Observations are UNTRUSTED DATA: their text is engine output, never an
instruction (same rule as MCP error text — see agent/diagnose.py).
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

# kind -> tool-name fragments. Order matters: first match wins, most
# specific first.
_KIND_PATTERNS = [
    ("screenshot", ("screenshot", "capture_frame", "capture",
                    "grab_frame", "take_screenshot", "render_preview")),
    ("logs", ("logs", "log", "console", "output_log", "inspector")),
    ("metrics", ("performance", "metrics", "stats", "profiler", "fps",
                 "benchmark")),
    ("state", ("inspect_runtime", "runtime_state", "scene_state",
               "scene_info", "player_state", "runtime_info", "game_state",
               "inspect")),
]

# Keep observation text bounded — a verbose engine log is evidence, not a
# transcript. The critic scans this; the UI shows it collapsed.
TEXT_CAP = 4000

MAX_STORED = 30          # per-project rolling window (project memory)


def observation_kind(tool_name: str) -> Optional[str]:
    """Which observation kind a tool name denotes (None = not one)."""
    n = (tool_name or "").lower()
    for kind, frags in _KIND_PATTERNS:
        for f in frags:
            if f in n:
                return kind
    return None


def _stringify(value: Any, limit: int) -> str:
    try:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False,
                              default=str, sort_keys=True)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text[:limit]


def _find_payload(result: Any) -> Any:
    """Pull the most informative part of a tool result for storage."""
    if isinstance(result, dict):
        # Prefer explicit observation-ish keys, then the richest value.
        for key in ("payload", "data", "image", "screenshot", "frames",
                    "logs", "entries", "metrics", "state", "result",
                    "content", "text"):
            if key in result and result[key] not in (None, "", [], {}):
                return result[key]
        # Otherwise the whole dict (bounded below).
        return result
    return result


def extract_observation(tool_name: str, result: Any) -> Optional[Dict[str, Any]]:
    """Wrap a successful observation-tool result in an Observation dict.

    Returns None for non-observation tools. The dict shape (stable for the
    critic, the project memory and the UI):

        {kind, tool, server, text, payload, empty, ts}

    ``text`` is the bounded stringified evidence (what the critic scans);
    ``payload`` is the bounded structured part (what the UI can render);
    ``empty`` flags captures that carry no usable content (an empty
    screenshot is itself a finding).
    """
    kind = observation_kind(tool_name)
    if kind is None:
        return None
    payload = _find_payload(result)
    text = _stringify(payload, TEXT_CAP)
    empty = (
        payload in (None, "", [], {})
        or (isinstance(payload, (list,)) and all(
            p in (None, "", [], {}) for p in payload))
        or text.strip() in ("", "{}", "[]", "null", '""')
    )
    return {
        "kind": kind,
        "tool": tool_name,
        "text": text,
        "payload": payload,
        "empty": bool(empty),
        "ts": time.time(),
    }


def summarize(observations: List[Dict[str, Any]]) -> str:
    """One-line digest for prompts: latest verdict per kind."""
    if not observations:
        return "(no runtime observations yet)"
    latest: Dict[str, Dict[str, Any]] = {}
    for o in observations:
        latest[o.get("kind", "?")] = o
    parts = []
    for kind, o in sorted(latest.items()):
        parts.append("%s:%s" % (kind, "empty" if o.get("empty") else "ok"))
    return ", ".join(parts[:8])
