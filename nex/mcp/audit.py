"""Structured audit log — every external action leaves a record.

The log is a bounded in-memory ring (plus whatever the server chooses
to do with entries). It records WHAT was called, on WHICH server,
WHETHER the policy allowed it, how long it took, and a short redacted
summary of the arguments — never full secrets or huge payloads.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional


_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|"
    r"credential|private[_-]?key)")


def _value_shape(value: Any) -> Any:
    """Describe an argument without retaining its possibly-private value."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return "<string:%d chars>" % len(value)
    if isinstance(value, bytes):
        return "<bytes:%d>" % len(value)
    if isinstance(value, dict):
        return "<object:%d keys>" % len(value)
    if isinstance(value, (list, tuple)):
        return "<array:%d items>" % len(value)
    return "<%s>" % type(value).__name__


def _arg_summary(args: Any, cap: int = 60) -> Any:
    """Keep argument names and shapes, never raw string payloads/secrets."""
    if isinstance(args, dict):
        out: Dict[str, Any] = {}
        for i, (k, v) in enumerate(args.items()):
            if i >= 50:
                out["…"] = "<additional keys omitted>"
                break
            key = str(k)[:cap]
            out[key] = "<redacted>" if _SECRET_KEY.search(key) \
                else _value_shape(v)
        return out
    return _value_shape(args)


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
