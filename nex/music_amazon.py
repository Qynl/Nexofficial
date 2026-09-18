"""Amazon Music — the dedicated, explicitly allowlisted music integration.

This is one of exactly TWO things Nex's AI can touch:

    1. explicitly connected MCP servers
    2. THIS module's music controls

It is a first-class connector that duck-types an MCP Upstream, so it flows
through the same discovery -> registry -> policy -> executor pipeline as
every MCP server — there is no special path, no side door.

The ALLOWLIST is the whole surface (nothing else exists here):

    am_play         start/resume playback
    am_pause        pause playback
    am_toggle       play/pause toggle
    am_next         next track
    am_previous     previous track
    am_volume       set volume 0..100
    am_search_play  search and play the best match

No filesystem, no shell, no arbitrary URLs — the OS deep-link opener (when
the `url` backend is enabled) is infrastructure INSIDE this module, bounded
to the amazonmusic:// scheme, and is never exposed as a capability.

Backends (NEX_MUSIC_BACKEND):
    "stub" (default) — records commands, returns honest structured results.
                       Used in sandboxes/CI where no player exists.
    "url"            — opens amazonmusic:// deep links via the platform
                       opener (Amazon Music desktop app).
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

SERVER_NAME = "amazon-music"
DEEP_LINK = "amazonmusic://"


# ---------------------------------------------------------------------------
# Backend: the ONLY place Nex is allowed to touch anything outside itself
# for music — bounded to the Amazon Music deep-link scheme.
# ---------------------------------------------------------------------------

def _open_deep_link(path: str) -> Dict[str, Any]:
    """Open an amazonmusic:// deep link. Returns a structured result."""
    import urllib.parse
    link = DEEP_LINK + path
    backend = os.environ.get("NEX_MUSIC_BACKEND", "stub")
    if backend != "url":
        return {"ok": True, "backend": "stub", "link": link,
                "note": "stub backend (set NEX_MUSIC_BACKEND=url to open "
                        "the Amazon Music app)"}
    try:
        if hasattr(os, "startfile"):          # Windows
            os.startfile(link)  # type: ignore[attr-defined]
            opener = "startfile"
        elif sys_darwin():
            import subprocess
            subprocess.Popen(["open", link],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            opener = "open"
        else:
            import subprocess
            subprocess.Popen(["xdg-open", link],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            opener = "xdg-open"
        return {"ok": True, "backend": "url", "link": link, "opener": opener}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "url", "link": link,
                "error": "could not open Amazon Music: " + repr(exc)}


def sys_darwin() -> bool:
    import sys
    return sys.platform == "darwin"


# ---------------------------------------------------------------------------
# Command log (Nex's own telemetry for its music actions — infra).
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()
_LOG: List[Dict[str, Any]] = []
_LOG_CAP = 100


def _record(op: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
    with _LOG_LOCK:
        _LOG.append({"op": op, "args": args, "result": result,
                     "ts": time.time()})
        del _LOG[:-_LOG_CAP]


def recent_commands(limit: int = 20) -> List[Dict[str, Any]]:
    with _LOG_LOCK:
        return list(_LOG[-limit:])


# ---------------------------------------------------------------------------
# The allowlisted operations. This is the ENTIRE capability surface.
# ---------------------------------------------------------------------------

def op_play(args: Dict[str, Any]) -> Dict[str, Any]:
    r = _open_deep_link("play")
    _record("am_play", args, r)
    return r


def op_pause(args: Dict[str, Any]) -> Dict[str, Any]:
    r = _open_deep_link("pause")
    _record("am_pause", args, r)
    return r


def op_toggle(args: Dict[str, Any]) -> Dict[str, Any]:
    r = _open_deep_link("toggle")
    _record("am_toggle", args, r)
    return r


def op_next(args: Dict[str, Any]) -> Dict[str, Any]:
    r = _open_deep_link("next")
    _record("am_next", args, r)
    return r


def op_previous(args: Dict[str, Any]) -> Dict[str, Any]:
    r = _open_deep_link("previous")
    _record("am_previous", args, r)
    return r


def op_volume(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        level = int(args.get("level"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "am_volume needs integer 'level' 0..100"}
    if not 0 <= level <= 100:
        return {"ok": False, "error": "volume must be 0..100"}
    r = _open_deep_link("volume/%d" % level)
    r["level"] = level
    _record("am_volume", {"level": level}, r)
    return r


def op_search_play(args: Dict[str, Any]) -> Dict[str, Any]:
    import urllib.parse
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "am_search_play needs 'query'"}
    if len(query) > 200:
        return {"ok": False, "error": "query too long (max 200 chars)"}
    r = _open_deep_link("search/" + urllib.parse.quote(query, safe=""))
    r["query"] = query
    _record("am_search_play", {"query": query}, r)
    return r


# (name, description, schema, handler) — same tuple shape tools.py uses.
MUSIC_TOOLS: List[Any] = [
    ("am_play",
     "Amazon Music: start/resume playback.",
     {"type": "object", "properties": {}, "required": []},
     op_play),
    ("am_pause",
     "Amazon Music: pause playback.",
     {"type": "object", "properties": {}, "required": []},
     op_pause),
    ("am_toggle",
     "Amazon Music: toggle play/pause.",
     {"type": "object", "properties": {}, "required": []},
     op_toggle),
    ("am_next",
     "Amazon Music: skip to the next track.",
     {"type": "object", "properties": {}, "required": []},
     op_next),
    ("am_previous",
     "Amazon Music: go to the previous track.",
     {"type": "object", "properties": {}, "required": []},
     op_previous),
    ("am_volume",
     "Amazon Music: set playback volume (0-100).",
     {"type": "object",
      "properties": {"level": {"type": "integer",
                               "description": "0..100"}},
      "required": ["level"]},
     op_volume),
    ("am_search_play",
     "Amazon Music: search for a song/artist/album and play the best match.",
     {"type": "object",
      "properties": {"query": {"type": "string"}},
      "required": ["query"]},
     op_search_play),
]

MUSIC_TOOL_NAMES = frozenset(name for (name, _d, _s, _h) in MUSIC_TOOLS)


def tool_definitions() -> List[Dict[str, Any]]:
    return [{"name": n, "description": d, "inputSchema": s}
            for (n, d, s, _h) in MUSIC_TOOLS]


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    for (n, _d, _s, handler) in MUSIC_TOOLS:
        if n == name:
            return handler(arguments or {})
    return {"error": "unknown Amazon Music control: " + name}


# ---------------------------------------------------------------------------
# MusicUpstream — duck-types upstream.Upstream so the Amazon Music
# connector flows through discovery -> CapabilityRegistry -> policy ->
# executor exactly like a real MCP server. No special-casing anywhere.
# ---------------------------------------------------------------------------

class MusicUpstream:
    """The explicitly-implemented Amazon Music connector, as a connector."""

    def __init__(self) -> None:
        self.name = SERVER_NAME
        self.label = "Amazon Music"
        self.url = DEEP_LINK
        self.probe_timeout = 0.2
        self.call_timeout = 5.0
        self._initialized = True
        self._server_info = {"serverInfo": {
            "name": SERVER_NAME, "version": "1.0",
            "integration": "explicit-allowlist"}}
        self._last_error = None
        self._tools_cache = list(tool_definitions())

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> Dict[str, Any]:
        self._initialized = True
        return {"serverInfo": self._server_info["serverInfo"]}

    # -- surface ------------------------------------------------------------
    def tools(self) -> List[Dict[str, Any]]:
        return [dict(t) for t in self._tools_cache]

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        # Return the MCP content envelope directly (tunnels._call_upstream
        # passes it through), so refusals keep isError=True instead of
        # being swallowed as empty results.
        result = call_tool(tool, args or {})
        text = json.dumps(result, ensure_ascii=False, indent=2,
                          sort_keys=True)
        return {"result": {"content": [{"type": "text", "text": text}],
                           "isError": "error" in result}}

    def status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "online": True,
            "connected": True,
            "server_info": self._server_info,
            "tools_count": len(self._tools_cache),
            "url": self.url,
            "last_error": self._last_error,
            "health": "ok",
        }

    def __repr__(self) -> str:  # pragma: no cover
        return "<MusicUpstream %s (%d controls)>" % (self.name, len(self._tools_cache))
