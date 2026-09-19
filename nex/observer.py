#!/usr/bin/env python3
"""NEX autonomous observer.

The observer runs as a background thread and watches for activity that
Nex might want to comment on. It exists so Nex is not just a reaction
machine — it can speak up when something happens (a build finishes,
a test passes, a file appears, the user has been idle long enough).

LAYER SEPARATION (hard architectural contract, enforced by
test_escape.py section A7):

  * The observer is ASSISTANT-LAYER ONLY. Its single side channel is
    publishing `speak.*` events to the server's EventBus. It has NO
    import of — and no access to — the MCP registry, the tunnel
    transports, tools.py, subprocess, or the agent loop. It cannot
    place an actor, run a command, open a network connection, or
    touch the workspace.
  * The ACTION RIGHT stays exactly where the security architecture
    puts it: the MCP gateway (authorize() → Upstream.call), reachable
    only through an LLM plan that was authorized step by step.
  * Consequently the model can "comment on" anything but can never
    cause external change through the assistant layer, no matter how
    it misbehaves. test_security.py proves this structurally
    (import scan) and behaviorally (the observer's only emits are
    speak events).

Sources of triggers:

  1. The Nex activity log (`.nex_log.jsonl`). Each new entry is
     consumed and may be reported as a speak event.
  2. File-watch on a configured directory. When new files appear
     Nex may comment.
  3. Idle timer. If no chat for >N seconds AND the log has fresh
     entries, Nex summarises what's been happening.

Autonomous speech follows the same `[STATE]` protocol as user-driven
chat, so the front-end handles it identically.

Run policy:
  - The observer respects a "speaking cooldown" — it won't fire
    two speak events within X seconds (default 12s) so it doesn't
    become annoying.
  - It is started by the server thread on boot.
  - It can be disabled by setting NEX_OBSERVER_DISABLED=1.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISABLED = os.environ.get("NEX_OBSERVER_DISABLED", "0") == "1"
COOLDOWN_S = float(os.environ.get("NEX_OBSERVER_COOLDOWN", "12"))
IDLE_TRIGGER_S = float(os.environ.get("NEX_OBSERVER_IDLE", "30"))
LOG_POLL_MS = float(os.environ.get("NEX_OBSERVER_POLL_MS", "500"))
TOOLS_ROOT = os.path.abspath(
    os.environ.get("NEX_TOOLS_ROOT", os.path.join(os.path.expanduser("~"), "nex_workspace"))
)


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------

class Observer:
    """Background thread that polls the Nex activity log and decides
    when Nex should speak autonomously."""

    def __init__(self, bus_publisher, tools_root: Optional[str] = None) -> None:
        self._publish = bus_publisher
        self._tools_root = tools_root or TOOLS_ROOT
        self._log_path = os.path.join(self._tools_root, ".nex_log.jsonl")
        self._last_offset = 0          # bytes already consumed
        self._last_speak_t = 0.0       # last time we emitted an autonomous speak
        self._last_activity_t = 0.0     # last chat/log activity (drives idle timer)
        self._recent_summarised = []   # entries already summarised
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Lock so tests / external threads can poke the observer safely.
        self._lock = threading.Lock()

    # ----- public API -----------------------------------------------------

    def start(self) -> None:
        if DISABLED or self._thread is not None:
            return
        # Prime the offset to "current end of file" so we don't fire
        # on historical entries.
        try:
            if os.path.isfile(self._log_path):
                self._last_offset = os.path.getsize(self._log_path)
        except OSError:
            pass
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="nex-observer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def last_summarised_count(self) -> int:
        with self._lock:
            return len(self._recent_summarised)

    def force_speak(self, text: str, emotion: Optional[str] = None) -> None:
        """Bypass the cooldown and speak immediately. Used by tests and
        by other tools that want to inject autonomous speech."""
        self._emit_speak(text, emotion, force=True)

    # ----- internals ------------------------------------------------------

    def _run(self) -> None:
        # Wait briefly for the server thread to finish booting so the
        # first emit doesn't race the WAKE→IDLE transition.
        time.sleep(0.5)
        self._last_activity_t = time.monotonic()
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                # Never let the observer die silently.
                import sys
                sys.stderr.write("observer error: %s\n" % exc)
            self._stop.wait(LOG_POLL_MS / 1000.0)

    def _tick(self) -> None:
        # 1) Read any new log entries since last offset.
        new_entries = self._read_new_entries()
        if new_entries:
            # New activity resets the idle clock, so an idle summary
            # never fires while the user is actively working.
            self._last_activity_t = time.monotonic()
            self._maybe_summarise(new_entries)

        # 2) Idle trigger: no chat activity for IDLE_TRIGGER_S AND we
        #    have fresh, unsummarised entries.
        now = time.monotonic()
        if (now - self._last_activity_t) >= IDLE_TRIGGER_S \
                and self._recent_summarised:
            self._idle_summary()

    def _read_new_entries(self) -> List[Dict[str, Any]]:
        """Read any new lines appended since last poll. Returns parsed dicts."""
        if not os.path.isfile(self._log_path):
            return []
        try:
            size = os.path.getsize(self._log_path)
        except OSError:
            return []
        if size <= self._last_offset:
            # File was truncated/rotated — reset.
            if size < self._last_offset:
                self._last_offset = 0
            return []
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                f.seek(self._last_offset)
                chunk = f.read(size - self._last_offset)
        except OSError:
            return []
        self._last_offset = size
        entries: List[Dict[str, Any]] = []
        for line in chunk.splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def _maybe_summarise(self, entries: List[Dict[str, Any]]) -> None:
        """Add the entries to the summary queue. The summary is emitted
        when the user goes idle (so Nex doesn't talk over active chat)."""
        with self._lock:
            self._recent_summarised.extend(entries)
            # Cap memory.
            if len(self._recent_summarised) > 200:
                self._recent_summarised = self._recent_summarised[-200:]

    def _idle_summary(self) -> None:
        """Speak a short summary of the most recent activity."""
        with self._lock:
            entries = list(self._recent_summarised)
            self._recent_summarised = []
        if not entries:
            return
        # Build a compact summary, max 3 entries.
        head = entries[:3]
        lines = []
        for e in head:
            kind = e.get("kind", "event")
            msg = e.get("message", "").strip()
            if len(msg) > 80:
                msg = msg[:77] + "..."
            lines.append("- " + kind + ": " + msg)
        text = "Hey, I noticed some activity:\n" + "\n".join(lines) + \
               " [FOCUSED]"
        self._emit_speak(text, emotion="FOCUSED")

    def _emit_speak(self, text: str, emotion: Optional[str] = None,
                    force: bool = False) -> None:
        now = time.monotonic()
        # Honour cooldown unless the caller explicitly bypasses it
        # (force_speak does — used by tools/tests that must emit now).
        if not force and (now - self._last_speak_t) < COOLDOWN_S:
            return
        self._last_speak_t = now
        # Strip any inline tags from the text and extract the first state.
        # We use the injected parser so observer.py doesn't depend on
        # server.py at import time (which causes sys.modules issues).
        if _parse_tags_fn is None:
            clean, state = text, None
        else:
            clean, state = _parse_tags_fn(text)
        if emotion and not state:
            state = emotion
        payload = {
            "type": "speak.delta", "delta": clean,
            "text": clean, "ts": time.time(),
        }
        if state:
            payload["state"] = state
            payload["stateSource"] = "observer"
        self._publish(payload)
        self._publish({
            "type": "speak.end", "text": clean, "tts_text": clean,
            "tags": [state] if state else [],
            "source": "observer", "ts": time.time(),
        })


# Injected by server.py via configure().
_parse_tags_fn = None


def configure(*, parse_tags) -> None:
    """Wire observer to server-provided helpers."""
    global _parse_tags_fn
    _parse_tags_fn = parse_tags


# Module-level singleton — populated by start_observer().
OBSERVER: Optional[Observer] = None


def start_observer(bus_publisher, tools_root: Optional[str] = None) -> Observer:
    """Start the global observer. Idempotent."""
    global OBSERVER
    if OBSERVER is not None:
        return OBSERVER
    OBSERVER = Observer(bus_publisher, tools_root)
    OBSERVER.start()
    return OBSERVER


def stop_observer() -> None:
    global OBSERVER
    if OBSERVER is not None:
        OBSERVER.stop()
        OBSERVER = None


def force_speak(text: str, emotion: Optional[str] = None) -> None:
    """Module-level force_speak that delegates to the running observer
    (no-op if observer hasn't been started or is disabled)."""
    if OBSERVER is not None:
        OBSERVER.force_speak(text, emotion)
