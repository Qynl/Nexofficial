"""Structured MCP audit log (STAGE 8 / STAGE 19).

Every external action produces an audit record. We avoid logging sensitive
argument values verbatim — only a short, redacted summary is kept.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Dict, List, Optional


def _arg_summary(args: Dict[str, Any]) -> Dict[str, Any]:
    """Redact potentially-large/secret values; keep keys + short previews."""
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        s = repr(v)
        out[k] = s if len(s) <= 60 else s[:57] + "..."
    return out


class AuditLog:
    def __init__(self, maxlen: int = 1000) -> None:
        self._entries: "deque" = deque(maxlen=maxlen)

    def record(self, *, task_id: Optional[str], server: Optional[str],
               tool: Optional[str], args: Dict[str, Any],
               classification: Optional[Dict[str, Any]], authorized: bool,
               auth_reason: str, result: str,
               duration_ms: Optional[float] = None,
               verification: Optional[str] = None,
               error: Optional[str] = None) -> Dict[str, Any]:
        rec = {
            "ts": time.time(),
            "task_id": task_id,
            "server": server,
            "tool": tool,
            "arguments": _arg_summary(args),
            "classification": classification,
            "authorized": authorized,
            "auth_reason": auth_reason,
            "result": result,            # ok | denied | error
            "duration_ms": duration_ms,
            "verification": verification,
            "error": error,
        }
        self._entries.append(rec)
        return rec

    def recent(self, n: int = 50) -> List[Dict[str, Any]]:
        return list(self._entries)[-n:]

    def to_list(self) -> List[Dict[str, Any]]:
        return list(self._entries)
