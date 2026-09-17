#!/usr/bin/env python3
"""NEX tools — the actions Nex can perform when invoked via MCP or
the internal observer loop.

Tools are intentionally simple and dependency-free. Anything that
needs heavy lifting (build systems, asset imports, etc.) is left to
the caller (Unreal, Roblox, Blender) which calls back into Nex via
MCP to report results.

Sandboxing:
    NEX_TOOLS_ROOT  — directory all file ops are restricted to
                      (default: $HOME/nex_workspace)
    NEX_TOOLS_SHELL — "1" to allow arbitrary shell commands, otherwise
                      disabled (default: "0")

Each tool exposes a small JSON-friendly schema so MCP clients can
discover what Nex knows how to do.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOOLS_ROOT = os.path.abspath(
    os.environ.get("NEX_TOOLS_ROOT", os.path.join(os.path.expanduser("~"), "nex_workspace"))
)
SHELL_ENABLED = os.environ.get("NEX_TOOLS_SHELL", "0") == "1"
# Hard timeout for any shell command (seconds).
SHELL_TIMEOUT = float(os.environ.get("NEX_TOOLS_TIMEOUT", "30"))


# ---------------------------------------------------------------------------
# Server hooks. The server wires these up at boot so tools.py can
# publish events on the EventBus without importing server (which would
# create a circular import and a confusing sys.modules layout).
# ---------------------------------------------------------------------------

_publish_bus = lambda event: None  # default: drop events
_parse_tags_fn = None
_emotion_set: Optional[set] = None


def configure(*, publisher, parse_tags, emotion_set) -> None:
    """Wire tools.py to the server's EventBus + parser.

    Called by server.py at boot. Each tool that publishes events uses
    the injected publisher so events always reach the same bus the
    SSE clients are subscribed to, even under sys.modules quirks."""
    global _publish_bus, _parse_tags_fn, _emotion_set
    _publish_bus = publisher
    _parse_tags_fn = parse_tags
    _emotion_set = set(emotion_set)


# ---------------------------------------------------------------------------
# Path sandboxing
# ---------------------------------------------------------------------------

def _ensure_root() -> None:
    """Make sure the tools root exists."""
    os.makedirs(TOOLS_ROOT, exist_ok=True)


def _resolve(path: str) -> str:
    """Resolve `path` to an absolute path within TOOLS_ROOT. Raises on escape.

    An empty string resolves to TOOLS_ROOT itself (the workspace root).
    """
    # Reject absolute paths and parent traversal BEFORE resolving.
    if os.path.isabs(path):
        raise PermissionError("absolute paths are not allowed: " + path)
    if not path:
        return TOOLS_ROOT
    candidate = os.path.normpath(os.path.join(TOOLS_ROOT, path))
    # After join + normpath, ensure the result is still under TOOLS_ROOT.
    if not (candidate == TOOLS_ROOT or candidate.startswith(TOOLS_ROOT + os.sep)):
        raise PermissionError("path escapes TOOLS_ROOT: " + path)
    return candidate


# ---------------------------------------------------------------------------
# Tool implementations. Each takes a kwargs dict and returns a JSON-safe
# result dict. Errors become {"error": "..."} so callers can render them.
# ---------------------------------------------------------------------------

def tool_list_files(path: str = "") -> Dict[str, Any]:
    """List files under `path` (relative to TOOLS_ROOT). Returns names + sizes."""
    _ensure_root()
    try:
        full = _resolve(path)
    except PermissionError as exc:
        return {"error": str(exc)}
    if not os.path.isdir(full):
        return {"error": "not a directory: " + path}
    out: List[Dict[str, Any]] = []
    try:
        for name in sorted(os.listdir(full)):
            p = os.path.join(full, name)
            st = os.stat(p)
            out.append({
                "name": name,
                "type": "dir" if os.path.isdir(p) else "file",
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    except OSError as exc:
        return {"error": str(exc)}
    return {"path": path or ".", "entries": out}


def tool_read_file(path: str, max_bytes: int = 1_000_000) -> Dict[str, Any]:
    """Read a text file. Truncates at max_bytes."""
    _ensure_root()
    try:
        full = _resolve(path)
    except PermissionError as exc:
        return {"error": str(exc)}
    if not os.path.isfile(full):
        return {"error": "not a file: " + path}
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(max_bytes + 1)
    except OSError as exc:
        return {"error": str(exc)}
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return {"path": path, "content": data, "truncated": truncated,
            "size": os.path.getsize(full)}


def tool_write_file(path: str, content: str, append: bool = False) -> Dict[str, Any]:
    """Write a text file. Overwrites by default; pass append=true to append."""
    _ensure_root()
    try:
        full = _resolve(path)
    except PermissionError as exc:
        return {"error": str(exc)}
    os.makedirs(os.path.dirname(full), exist_ok=True)
    mode = "a" if append else "w"
    try:
        with open(full, mode, encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        return {"error": str(exc)}
    return {"path": path, "bytes": len(content), "appended": append}


def tool_run_command(command: str, cwd: Optional[str] = None,
                     timeout: Optional[float] = None) -> Dict[str, Any]:
    """Run a shell command. Disabled unless NEX_TOOLS_SHELL=1.

    The command is split with shlex (NOT through a shell), so metachars
    like ; | & ` $() are treated as literal characters. cwd is
    sandboxed to TOOLS_ROOT.
    """
    if not SHELL_ENABLED:
        return {"error": "shell commands are disabled (set NEX_TOOLS_SHELL=1)"}
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {"error": "could not parse command: " + str(exc)}
    if not argv:
        return {"error": "empty command"}
    # Reject obviously dangerous commands regardless of argv parsing.
    forbidden = {"rm", "sudo", "mkfs", "dd", "shutdown", "reboot", "halt",
                 "passwd", "useradd", "userdel", "mount", "umount"}
    if os.path.basename(argv[0]) in forbidden:
        return {"error": "refusing to run: " + argv[0]}
    workdir = TOOLS_ROOT
    if cwd:
        try:
            workdir = _resolve(cwd)
        except PermissionError as exc:
            return {"error": str(exc)}
    to = timeout if timeout is not None else SHELL_TIMEOUT
    try:
        t0 = time.monotonic()
        proc = subprocess.run(
            argv, cwd=workdir, capture_output=True,
            text=True, timeout=to,
        )
        dt = time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return {"error": "command timed out after " + str(to) + "s",
                "timed_out": True}
    except FileNotFoundError as exc:
        return {"error": "command not found: " + str(exc)}
    except OSError as exc:
        return {"error": str(exc)}
    return {
        "command": command,
        "argv": argv,
        "exit": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_s": round(dt, 3),
    }


def tool_search_files(query: str, path: str = "", max_results: int = 50) -> Dict[str, Any]:
    """Recursively grep for `query` under `path`. Returns matching paths + line numbers."""
    _ensure_root()
    try:
        full = _resolve(path)
    except PermissionError as exc:
        return {"error": str(exc)}
    if not os.path.isdir(full):
        return {"error": "not a directory: " + path}
    out: List[Dict[str, Any]] = []
    needle = query.lower()
    try:
        for root, dirs, files in os.walk(full):
            # Skip hidden and VCS directories.
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
            for f in files:
                if len(out) >= max_results:
                    return {"query": query, "results": out, "truncated": True}
                p = os.path.join(root, f)
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if needle in line.lower():
                                rel = os.path.relpath(p, TOOLS_ROOT)
                                out.append({"path": rel, "line": i,
                                            "text": line.rstrip("\n")[:200]})
                                if len(out) >= max_results:
                                    break
                except (OSError, UnicodeError):
                    continue
    except OSError as exc:
        return {"error": str(exc)}
    return {"query": query, "results": out, "truncated": False}


def tool_log_event(kind: str, message: str,
                   tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Append a timestamped entry to the Nex activity log and publish
    a state event so the front-end notices. Tags may include emotion
    names (HAPPY, CURIOUS, etc.) which are surfaced to the bubble."""
    _ensure_root()
    log_path = os.path.join(TOOLS_ROOT, ".nex_log.jsonl")
    entry = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": kind,
        "message": message,
        "tags": tags or [],
    }
    try:
        os.makedirs(TOOLS_ROOT, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        return {"error": str(exc)}
    # Publish on the bus so the front-end can react. We use the
    # injected publisher (set via set_publisher) so this module
    # doesn't depend on the server module at import time.
    payload = {"type": "tool.event", "kind": kind, "message": message,
               "tags": tags or [], "ts": entry["ts"]}
    _publish_bus(payload)
    return {"logged": True, "path": ".nex_log.jsonl"}


def tool_recent_events(limit: int = 20) -> Dict[str, Any]:
    """Return the most recent log entries (most recent first)."""
    _ensure_root()
    log_path = os.path.join(TOOLS_ROOT, ".nex_log.jsonl")
    if not os.path.isfile(log_path):
        return {"events": []}
    out: List[Dict[str, Any]] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        return {"error": str(exc)}
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"events": out}


def tool_speak(text: str, emotion: Optional[str] = None) -> Dict[str, Any]:
    """Publish a speak event so Nex says something.

    The text is published as a `speak.delta` (immediate) and a
    `speak.end` (cumulative). The optional emotion tag is honored.
    This is the tool external systems use to make Nex react to a
    build result, asset import, etc.
    """
    clean, state = _parse_tags_fn(text) if _parse_tags_fn else (text, None)
    if emotion and _emotion_set:
        if emotion not in _emotion_set:
            return {"error": "unknown emotion: " + emotion}
        if not state:
            state = emotion
    # Publish a single chunk — speak.delta with cumulative text.
    _publish_bus({
        "type": "speak.delta",
        "delta": clean,
        "text": clean,
        "ts": time.time(),
    })
    if state:
        _publish_bus({"type": "state", "state": state,
                      "params": {"source": "tool"}, "ts": time.time()})
    _publish_bus({
        "type": "speak.end",
        "text": clean,
        "tts_text": clean,
        "tags": [state] if state else [],
        "source": "tool",
        "ts": time.time(),
    })
    return {"spoken": clean, "emotion": state}


# ---------------------------------------------------------------------------
# Tool registry. Each entry is (name, description, input_schema, handler).
# ---------------------------------------------------------------------------

TOOL_INPUT_NONE: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}


TOOLS: List[Tuple[str, str, Dict[str, Any], Any]] = [
    ("list_files", "List files in the sandboxed workspace.",
     {"type": "object",
      "properties": {"path": {"type": "string",
                              "description": "Relative path; '' = workspace root."}},
      "required": []}, tool_list_files),

    ("read_file", "Read a text file from the sandboxed workspace.",
     {"type": "object",
      "properties": {"path": {"type": "string"},
                     "max_bytes": {"type": "integer", "default": 1_000_000}},
      "required": ["path"]}, tool_read_file),

    ("write_file", "Write or append to a text file in the sandboxed workspace.",
     {"type": "object",
      "properties": {"path": {"type": "string"},
                     "content": {"type": "string"},
                     "append": {"type": "boolean", "default": False}},
      "required": ["path", "content"]}, tool_write_file),

    ("run_command", "Run a shell command (sandboxed to the workspace). "
                    "Disabled unless NEX_TOOLS_SHELL=1.",
     {"type": "object",
      "properties": {"command": {"type": "string"},
                     "cwd": {"type": "string"},
                     "timeout": {"type": "number"}},
      "required": ["command"]}, tool_run_command),

    ("search_files", "Recursively grep for a string under the workspace.",
     {"type": "object",
      "properties": {"query": {"type": "string"},
                     "path": {"type": "string", "default": ""},
                     "max_results": {"type": "integer", "default": 50}},
      "required": ["query"]}, tool_search_files),

    ("log_event", "Append an entry to the Nex activity log and notify "
                  "the front-end. Useful for marking build steps, asset "
                  "imports, or test results.",
     {"type": "object",
      "properties": {"kind": {"type": "string"},
                     "message": {"type": "string"},
                     "tags": {"type": "array", "items": {"type": "string"}}},
      "required": ["kind", "message"]}, tool_log_event),

    ("recent_events", "Return the most recent log entries.",
     TOOL_INPUT_NONE, tool_recent_events),

    ("speak", "Make Nex say something (with optional emotion tag).",
     {"type": "object",
      "properties": {"text": {"type": "string"},
                     "emotion": {"type": "string",
                                 "description": "One of HAPPY/CURIOUS/AMUSED/etc."}},
      "required": ["text"]}, tool_speak),
]


def tool_definitions() -> List[Dict[str, Any]]:
    """Return the JSON-friendly tool list for MCP /tools/list."""
    return [
        {"name": name, "description": desc, "inputSchema": schema}
        for (name, desc, schema, _handler) in TOOLS
    ]


def call_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Invoke a tool by name. Returns the result dict."""
    arguments = arguments or {}
    for (n, _d, _s, handler) in TOOLS:
        if n == name:
            try:
                return handler(**arguments)
            except TypeError as exc:
                return {"error": "bad arguments: " + str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"error": "tool failed: " + repr(exc)}
    return {"error": "unknown tool: " + name}
