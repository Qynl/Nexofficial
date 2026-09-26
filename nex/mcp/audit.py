"""Structured audit log — every external action leaves a record.

The log is a bounded in-memory ring (plus whatever the server chooses
to do with entries). It records WHAT was called, on WHICH server,
WHETHER the policy allowed it, how long it took, and a short redacted
summary of the arguments — never full secrets or huge payloads.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional


def _arg_summary(args: Any, cap: int = 60) -> Any:
    """Redact potentially-large/secret values; keep keys + short previews."""
    if isinstance(args, dict):
        out: Dict[str, Any] = {}
        for k, v in args.items():
            s = repr(v)
            out[str(k)] = s if len(s) <= cap else s[:cap - 3] + "..."
        return out
    s = repr(args)
    return s if len(s) <= cap else s[:cap - 3] + "..."


class AuditLog:
    def __init__(self, maxlen: int = 1000) -> None:
        self._entries: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, kind: str, *, server: Optional[str] = None,
               tool: Optional[str] = None, ok: bool = True,
               args: Any = None, detail: str = "",
               duration_ms: Optional[float] = None,
               context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Append one audit record.

        kind: call | refuse | confirm_required | connect | disconnect |
              remove | add
        """
        rec = {
            "ts": time.time(),
            "kind": kind,
            "server": server,
            "tool": tool,
            "args": _arg_summary(args) if args else None,
            "ok": bool(ok),
            "detail": (detail or "")[:300],
            "duration_ms": duration_ms,
            "context": context or None,
        }
        with self._lock:
            self._entries.append(rec)
        return rec

    def recent(self, n: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._entries)[-n:]

    def to_list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._entries)
