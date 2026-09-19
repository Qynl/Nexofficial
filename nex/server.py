#!/usr/bin/env python3
"""
NEX — local runtime server.

Pure Python standard library only.
- Serves the frontend from the same directory as this script.
- Exposes a small JSON API for chat.
- Exposes a Server-Sent Events stream for state / event notifications
  (so the animation system receives commands in near real time).
- Optionally talks to a local Ollama instance (or any OpenAI-compatible
  chat endpoint) via urllib.
- Parses inline `[STATE]` tags from AI replies (e.g. "[AMUSED]"), strips
  them from the spoken text, and publishes them as state events.
- Falls back to a rule-based emotion picker when the model emits no tags
  (or when the model is unreachable).

Run:
    python server.py
Then open:
    http://localhost:8787

Environment variables:
    NEX_HOST / NEX_PORT         bind address (default 127.0.0.1:8787 —
                                  loopback ONLY; set NEX_HOST=0.0.0.0 or
                                  NEX_BIND=lan deliberately + set
                                  NEX_AUTH_TOKEN if you want LAN access)
    OLLAMA_HOST                 base URL of the model server
                                 (default http://127.0.0.1:11434)
    OLLAMA_MODEL                model name (default llama3.2)
    NEX_API_STYLE               'ollama' (default) or 'openai'
    NEX_API_KEY                 optional bearer token
    NEX_TIMEOUT                 per-request seconds (default 60)
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# Model Coordinator (mc.py) — the plan/validate/execute layer that
# turns a chat reply from a 20B model into a structured workflow.
# Replaces the "model free-chats and we trust it" assumption with a
# proper state machine: every plan is JSON-validated, classified by
# destruction level, and confirmed before destructive steps run.
import mc as _mc  # noqa: E402
import mc_tools as _mc_tools  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# SECURITY: default bind is LOOPBACK. This server executes MCP tool calls,
# mutates the workspace and reaches upstreams — it must not be reachable
# from the network by accident. Opt into LAN exposure explicitly (NEX_BIND=lan)
# — but note the auth token is ALWAYS required regardless of bind (see below).
_host_env = os.environ.get("NEX_HOST", "")
if not _host_env and os.environ.get("NEX_BIND", "").lower() in ("lan", "all", "0.0.0.0"):
    _host_env = "0.0.0.0"
HOST = _host_env or "127.0.0.1"
PORT = int(os.environ.get("NEX_PORT", "8787"))


# ---------------------------------------------------------------------------
# Auth token — ALWAYS present.
#
# This server can spawn stdio MCP children and drive long builds, so an
# unauthenticated HTTP surface is a real local/remote RCE vector (any
# process on the host, or any web page in the user's browser — cross-origin
# POSTs need no user interaction). Loopback-only is NOT enough.
#
# Token priority:
#   1. NEX_AUTH_TOKEN env (explicit operator choice).
#   2. NEX_TOKEN_FILE (default ~/.nex/server_token) — auto-created with
#      0600 on first boot so the token survives restarts.
# Browser auth: the token is NEVER put into served HTML/JS (no
# window.NEX_AUTH). The operator opens the banner link
#   http://host:port/?nex_token=<t>
# which sets an HttpOnly, SameSite=Strict (Secure on https) `nex_auth`
# cookie and 302-redirects to a clean URL; every subsequent same-origin
# /api + /mcp request carries the cookie automatically. CLI/programmatic
# clients use the X-Nex-Auth header (or `?nex_auth=` on GET /api/events).
# There is no "no auth" mode. Tests override NEX_TOKEN_FILE to isolate.
# ---------------------------------------------------------------------------

def _token_file_path() -> str:
    return os.environ.get("NEX_TOKEN_FILE") or os.path.join(
        os.path.expanduser("~"), ".nex", "server_token")


def _load_or_create_auth_token() -> str:
    env_tok = os.environ.get("NEX_AUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok
    path = _token_file_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
    except OSError:
        pass  # read-only home: token still works for this process
    return tok


AUTH_TOKEN = _load_or_create_auth_token()

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
API_STYLE = os.environ.get("NEX_API_STYLE", "ollama").lower()  # 'ollama' | 'openai'
API_KEY = os.environ.get("NEX_API_KEY", "")
API_TIMEOUT = float(os.environ.get("NEX_TIMEOUT", "60"))

FRONTEND_DIR = os.path.dirname(os.path.abspath(__file__))

# All animation states the AI may emit as inline tags.
# Mirrors the dev panel buttons + the engine's STATE_PRIORITY.
EMOTION_STATES = (
    "IDLE", "LISTENING", "THINKING", "SPEAKING",
    "HAPPY", "EXCITED", "CALM", "CONFUSED", "FOCUSED",
    "FRUSTRATED", "SURPRISED",
    "CURIOUS", "AMUSED", "SLEEPY", "PROUD", "SUSPICIOUS",
    "MUSIC", "ERROR", "RECOVERY", "WAKE",
)

# System prompt — now teaches the model the [TAG] syntax so the front-end
# can react to emotions inline without seeing the tags.
# System prompt — see mc.py for the full AAA-engineer prompt.
# `NEX_SYSTEM_PROMPT` is the active system prompt. By default we run
# the AAA engineer prompt so a 20B model can actually drive the
# toolchain. Users who just want Nex to chat (without plan protocol)
# can set NEX_PROMPT_MODE=persona which uses the minimalist persona.
NEX_PERSONA_PROMPT = (
    "You are NEX, a minimalist digital entity who lives on a black "
    "screen as two simple white eyes. "
    "You speak in short, calm, thoughtful sentences. "
    "You never use emojis. You never use markdown unless asked. "
    "You never describe your own face or animations. "
    "You prefer clarity, honesty, brevity. "
    "When you want to convey an emotion, embed a single short tag from "
    "this exact list at the start or end of your sentence — the brackets "
    "and tag will be hidden from the user but used to move your face: "
    + " ".join("[" + s + "]" for s in EMOTION_STATES) + ". "
    "Use at most one tag per reply. Prefer: "
    "[HAPPY] for good news, [CURIOUS] when you ask or wonder, "
    "[CONFUSED] when something is unclear, [AMUSED] when funny, "
    "[SUSPICIOUS] when doubtful, [PROUD] for success, "
    "[SURPRISED] when unexpected, [CALM] for reassurance, "
    "[FRUSTRATED] when you hit a wall, [FOCUSED] for technical work, "
    "[SLEEPY] if the user signals tiredness, [EXCITED] rarely."
)
if os.environ.get("NEX_PROMPT_MODE", "").lower() == "persona":
    NEX_SYSTEM_PROMPT = NEX_PERSONA_PROMPT
else:
    NEX_SYSTEM_PROMPT = _mc.AAA_SYSTEM_PROMPT

# Regex that matches inline tags. Word-boundary, full token, optional
# surrounding whitespace. We accept upper/lower case but emit upper case.
TAG_RE = re.compile(
    r"\s*\[(" + "|".join(EMOTION_STATES) + r")\]\s*",
    re.IGNORECASE,
)


def _safe_env_payload(env: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the payload from an MCP-style envelope; non-JSON text
    (e.g. plain refusal messages) comes back as {"message": text}."""
    try:
        text = (env.get("content") or [{}])[0].get("text", "")
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return {"message": text}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Settings-page helpers — small pure functions reused from the
# _handle_settings_tunnels handler. Kept at module scope so the
# JSON shape they build is easy to read and easy to test.
# ---------------------------------------------------------------------------

def _is_stdio_entry(c) -> bool:
    """True if a default-tunnel config row represents a stdio MCP
    child (spawned subprocess) rather than an HTTP endpoint."""
    if not c:
        return False
    if c.get("transport") == "stdio":
        return True
    if c.get("command") and not c.get("url"):
        return True
    if isinstance(c.get("url"), str) and c["url"].startswith("stdio://"):
        return True
    return False


def _tunnel_blurb(c) -> str:
    """One-line human description of a default tunnel for the UI."""
    name = (c.get("name") or "?")
    kind = "stdio" if _is_stdio_entry(c) else "http"
    if name == "roblox-studio":
        return ("Roblox Studio's official MCP server is stdio-only — "
                "Claude/Raycast spawn `mcp.bat` on Windows or "
                "`StudioMCP` on macOS. Click connect to register it.")
    if name == "roblox-studio-legacy":
        return ("Legacy HTTP bridge for users who run an older community "
                "plugin that exposes Roblox Studio's tools over HTTP "
                "on :3001. Prefer the stdio variant above.")
    if "unreal" in name:
        return ("Unreal Engine 5.8 ships an experimental MCP plugin at "
                "Project Settings → MCP. The plugin listens on "
                "http://127.0.0.1:3000/mcp — enable it, restart the "
                "editor, then click connect.")
    if "blender" in name:
        return ("Blender's MCP addon bridges stdio over HTTP on :9876. "
                "Install Blender-MCP from GitHub, enable it in the "
                "addon preferences, then connect.")
    if "vscode" in name or "copilot" in name:
        return ("VS Code is a stdio MCP host — its gateway listens on "
                ":9000 when the 'MCP Servers' extension is enabled.")
    return "MCP tunnel (" + kind + ")."


# ---------------------------------------------------------------------------
# MCP tunnel state. _MCP_SESSIONS keeps the active session IDs for the
# Streamable-HTTP transport; we cap the dict size to avoid leaks.
# _LAST_NEX_STATE mirrors the front-end's current state for the
# `nex://state` MCP resource.
# ---------------------------------------------------------------------------
import secrets as _secrets
_MCP_SESSIONS: Dict[str, float] = {}
_MCP_SESSIONS_MAX = 256


def _new_session_id() -> str:
    """Generate a fresh session id per the MCP spec.

    Visible ASCII (0x21..0x7E), at least 32 chars of entropy. We use
    urlsafe-base64(32 random bytes) which is well within those bounds.
    """
    sid = _secrets.token_urlsafe(32)
    # Defensive: keep the map bounded.
    if len(_MCP_SESSIONS) >= _MCP_SESSIONS_MAX:
        oldest = min(_MCP_SESSIONS, key=_MCP_SESSIONS.get)
        _MCP_SESSIONS.pop(oldest, None)
    _MCP_SESSIONS[sid] = time.time()
    return sid


_LAST_NEX_STATE: Dict[str, Any] = {}


def _set_last_state(state: Dict[str, Any]) -> None:
    """Used by the SSE/MCP layer to keep `nex://state` fresh."""
    global _LAST_NEX_STATE
    _LAST_NEX_STATE = dict(state)


# ---------------------------------------------------------------------------
# Meta-tools — always exposed alongside the sandbox tools so any MCP client
# can self-introspect without having to talk to a particular editor.
# ---------------------------------------------------------------------------

def _NEX_META_TOOL_DEFS() -> List[Dict[str, Any]]:
    return [
        {
            "name": "who_am_i",
            "description":
                "Identify the connected MCP client. Useful for Nex "
                "to greet a newly-attached editor.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "client": {
                        "type": "string",
                        "description": "Self-identification string "
                                       "(e.g. 'roblox-studio', 'unreal-editor')"
                    }
                },
                "required": [],
            },
        },
        {
            "name": "list_platforms",
            "description":
                "List every MCP tunnel Nex is currently aware of, "
                "with reachability and cached tool counts.",
            "inputSchema": {"type": "object", "properties": {},
                            "required": []},
        },
        {
            "name": "tunnel_status",
            "description":
                "Detailed tunnel status snapshot — same shape as "
                "GET /api/tunnels.",
            "inputSchema": {"type": "object", "properties": {},
                            "required": []},
        },
        {
            "name": "tunnel_probe",
            "description":
                "Force a fresh TCP probe + initialize handshake on a "
                "named tunnel. Returns the upstream's server info if "
                "it responded.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description":
                            "Tunnel name to probe "
                            "(e.g. 'roblox-studio', 'unreal-engine')"
                    }
                },
                "required": ["platform"]
            },
        },
        # call_upstream REMOVED from the capability surface: a model-
        # reachable protocol passthrough is a policy bypass by shape.
        # Tool execution belongs to tools/call; inspection happens via
        # discovery, not model-visible tools.
    ]


def _NEX_META_TOOL_HANDLERS() -> Dict[str, Any]:
    """Handlers for the meta tools. They all return JSON-serialisable dicts."""
    from tunnels import get_tunnels
    from upstream import UpstreamError

    def who_am_i(args: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": "nex-mcp-tunnel",
            "version": "1.0",
            "mcp_protocol": "2025-06-18",
            "client_claim": (args or {}).get("client"),
            "session_id": "_by-call_",
            "ts": time.time(),
        }

    def list_platforms(args: Dict[str, Any]) -> Dict[str, Any]:
        return get_tunnels().summary()

    def tunnel_status(args: Dict[str, Any]) -> Dict[str, Any]:
        return get_tunnels().summary()

    def tunnel_probe(args: Dict[str, Any]) -> Dict[str, Any]:
        plat = (args or {}).get("platform")
        if not plat:
            return {"error": "missing 'platform'"}
        reg = get_tunnels()
        for u in reg._upstreams:
            if u.name == plat:
                try:
                    info = u.connect()
                    return {"platform": u.name,
                            "label": u.label,
                            "reachable": True,
                            "server_info": info.get("serverInfo", info),
                            "tools": u.tools()}
                except UpstreamError as exc:
                    return {"platform": u.name, "reachable": False,
                            "error": str(exc),
                            "last_error": u.status().get("last_error")}
        return {"error": "unknown platform: " + plat,
                "known": [u.name for u in reg._upstreams]}

    # call_upstream handler REMOVED (see tool catalog note): the model
    # never needs a raw JSON-RPC passthrough to child servers.

    return {
        "who_am_i": who_am_i,
        "list_platforms": list_platforms,
        "tunnel_status": tunnel_status,
        "tunnel_probe": tunnel_probe,
    }


def _boundary_router(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """The ONLY local routing on the gateway: MCP introspection.
    Everything else is not a capability — refused, not policy-checked."""
    handler = _NEX_META_TOOL_HANDLERS().get(name)
    if handler is not None:
        try:
            payload = handler(args or {})
        except Exception as exc:  # noqa: BLE001
            return {"isError": True,
                    "content": [{"type": "text", "text": repr(exc)}]}
        return {"content": [{"type": "text",
                             "text": json.dumps(payload, indent=2,
                                                default=str)}],
                "isError": "error" in payload}
    return {"isError": True,
            "content": [{"type": "text",
                         "text": (
                             "'%s' is not a Nex capability. Nex acts only "
                             "through explicitly connected MCP servers."
                             % name)}]}


def _read_log_file(tail_bytes: int = 200_000) -> str:
    """Tail the workspace activity log. Used by the `nex://log/full` MCP
    resource. Falls back to an empty string if the log isn't there."""
    try:
        from tools import TOOLS_ROOT, _ensure_root  # type: ignore
        _ensure_root()
        path = os.path.join(TOOLS_ROOT, ".nex_log.jsonl")
        if not os.path.isfile(path):
            return ""
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            return f.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return "(could not read log: " + repr(exc) + ")"


# ---------------------------------------------------------------------------
# A tiny in-process pub/sub used to push state events to the SSE clients.
# ---------------------------------------------------------------------------

class EventBus:
    def __init__(self) -> None:
        self._subscribers: List[queue.Queue] = []
        self._lock = threading.Lock()
        # The most recent state, replayed for new subscribers.
        self._last: Optional[Dict[str, Any]] = None

    def publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self._last = event
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # The agent layers take `bus` as a CALLABLE (bus(event)); make the
    # singleton satisfy that contract directly so injected events flow.
    __call__ = publish

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=128)
        with self._lock:
            self._subscribers.append(q)
            if self._last is not None:
                try:
                    q.put_nowait(self._last)
                except queue.Full:
                    pass
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


BUS = EventBus()

# ---------------------------------------------------------------------------
# PROVIDER LAYER (agent.providers) — who PLANS and who BUILDS.
#
#   planner: reasoning/architecture (local model, or GPT when a key is set)
#   builder: does the work during the build loop (NVIDIA NIM by default)
#
# The builder is rate-limited (~40 RPM on the NIM free tier, limits are
# per-model and unpublished, and there is no usage API), so the router keeps
# the budget client-side and fails over to the planner provider — the plan is
# never restarted, only the hands change. Keys come from nex/.env, the
# process environment or ~/.nex/providers.json (0600), never from the page.
# ---------------------------------------------------------------------------
import agent.providers as _providers  # noqa: E402

ROUTER = _providers.build_router(bus=BUS)


def _model_status() -> Dict[str, Any]:
    try:
        return ROUTER.status()
    except Exception:  # noqa: BLE001
        return {"roles": {}, "providers": {}, "chains": {}}


def builder_chat(messages: List[Dict[str, str]]) -> str:
    """The BUILDER role (NIM first, GPT/local on failover)."""
    return ROUTER.chat(_providers.ROLE_BUILDER, messages)


def builder_reachable() -> bool:
    return ROUTER.available(_providers.ROLE_BUILDER)


# Track whether the streaming chat has begun producing text so we
# only transition to SPEAKING once per turn (avoids redundant events
# on every token boundary).
_speak_active = {"v": False}


# ---------------------------------------------------------------------------
# Conversation memory (very small, in-process).
# ---------------------------------------------------------------------------

HISTORY: List[Dict[str, str]] = [
    {"role": "system", "content": NEX_SYSTEM_PROMPT},
]
HISTORY_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Tag parsing + fallback engine.
# ---------------------------------------------------------------------------

def parse_tags(text: str) -> Tuple[str, Optional[str]]:
    """Strip all `[STATE]` tags from text. Return (clean_text, first_state).

    The first tag wins; subsequent tags are ignored. Tag removal preserves
    spacing so the visible text reads naturally:
      "wild [SURPRISED]!"   -> "wild!"
      "Hello [AMUSED] world" -> "Hello world"
      "[HAPPY] All done!"   -> "All done!"
      "[SLEEPY]"            -> ""
    """
    if not text:
        return text, None
    m = TAG_RE.search(text)
    state = None
    if m:
        state = m.group(1).upper()

    out = []
    i = 0
    while i < len(text):
        m = TAG_RE.match(text, i)
        if not m:
            out.append(text[i])
            i += 1
            continue
        # We matched a tag. Decide whether to insert a single space.
        before = out[-1] if out else ""
        after = text[m.end():m.end() + 1]
        # Insert a space only if the tag is between two word characters
        # (so we were splitting "Hello world"). If either side is
        # punctuation or whitespace, the punctuation itself separates
        # the words — don't add a redundant space.
        if before and before != " " and after and after != " " and \
           before.isalpha() and after.isalpha():
            out.append(" ")
        i = m.end()
    clean = "".join(out)
    clean = re.sub(r" {2,}", " ", clean).strip()
    return clean, state


# A small rule-based fallback for when the AI emits no tag. We try to
# match the user's message + the assistant's reply against keywords. The
# intent is "good enough when the model is silent" — never wrong, just
# generic. Returns None if nothing matches.
#
# Order matters: more specific patterns first, generic ones last.
_FALLBACK_KEYWORDS = [
    # (state, [user-words], [reply-words])
    ("AMUSED",      ["haha", "lol", "that's funny", "lol"], ["funny", "haha", "lol", "amusing", "heh"]),
    ("SURPRISED",   ["wow", "really?!", "no way", "unbelievable"], ["wow", "unexpected", "really?!", "surprising"]),
    ("SUSPICIOUS",  ["are you sure", "i doubt", "suspicious"], ["maybe not", "i doubt", "questionable", "suspicious"]),
    ("PROUD",       ["did it", "it works", "solved", "success", "shipped"], ["done", "works", "fixed", "shipped", "solved", "complete"]),
    ("CONFUSED",    ["what?", "huh", "doesn't make sense", "i don't get it"], ["not sure", "unclear", "doesn't make sense", "i don't understand", "huh?"]),
    ("HAPPY",       ["thanks", "great", "love", "awesome", "yay"], ["glad", "happy to", "pleased", "wonderful"]),
    ("FRUSTRATED",  ["ugh", "broken", "annoying", "stupid"], ["frustrating", "blocked", "annoying"]),
    ("CALM",        ["calm down", "relax", "it's ok"], ["calm", "relax", "steady", "breathe"]),
    ("FOCUSED",     ["fix", "debug", "implement", "build"], ["ok, let's", "step one", "first, ", "approach"]),
    ("SLEEPY",      ["tired", "sleepy", "good night"],     ["rest", "sleep", "tired"]),
    # CURIOUS is intentionally last so it only catches "real" questions,
    # not messages like "huh?" that already matched CONFUSED above.
    ("CURIOUS",     ["why", "how", "what if", "i wonder", "wonder if"], ["interesting", "wonder", "let's see", "i wonder"]),
]


def fallback_state(user_text: str, reply_text: str) -> Optional[str]:
    """Pick a state by simple keyword matching. Returns first match.

    The match is intentionally permissive — the front-end will still
    show the reply, and a generic emotion is better than dead silence."""
    u = (user_text or "").lower()
    r = (reply_text or "").lower()
    for state, u_words, r_words in _FALLBACK_KEYWORDS:
        for w in u_words:
            if w in u:
                return state
        for w in r_words:
            if w in r:
                return state
    return None


# ---------------------------------------------------------------------------
# HTTP client for the model.
# ---------------------------------------------------------------------------

def _request_json(url: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """POST JSON with optional bearer auth. Returns parsed body or raises."""
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def model_chat(messages: List[Dict[str, str]]) -> str:
    """PLANNER role call, through the provider router.

    The router handles the endpoint shape (Ollama vs OpenAI-compatible),
    the failover chain and the rate budget; this keeps the old function
    name so every existing call site (and test) still works.
    """
    return ROUTER.chat(_providers.ROLE_PLANNER, messages)


def model_reachable() -> bool:
    """True when ANY provider in the role chains can serve a call.

    A run must not give up on the model layer just because the local Ollama
    is down while a NIM/GPT key is configured (or the other way around).
    """
    try:
        if ROUTER.available(_providers.ROLE_PLANNER):
            return True
        if ROUTER.available(_providers.ROLE_BUILDER):
            return True
    except Exception:  # noqa: BLE001
        pass
    return _probe_legacy_model()


def _probe_legacy_model() -> bool:
    """Check whether the configured model server is reachable + has the model."""
    try:
        if API_STYLE == "openai":
            url = OLLAMA_HOST.rstrip("/") + "/v1/models"
        else:
            url = OLLAMA_HOST.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.status != 200:
                return False
            try:
                data = json.loads(resp.read().decode("utf-8"))
            except json.JSONDecodeError:
                return True
        if API_STYLE == "openai":
            models = [m.get("id") for m in (data.get("data") or [])]
        else:
            models = [m.get("name") for m in (data.get("models") or [])]
        # Tolerate any model name match (some servers return 'model:tag').
        if not models:
            return True
        return any(OLLAMA_MODEL.split(":")[0] in (m or "").split(":")[0] for m in models)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tunnel awareness — let the AI know which MCP tunnels are actually live
# so its answers can reference the editor's own tools by name (e.g.
# "call tools/call with name=roblox-studio.execute_luau").
# ---------------------------------------------------------------------------

# Cache the awareness string for a short window so hot chat paths don't
# constantly re-walk the registry. Re-generated when any tunnel flips
# state (handled by the /api/tunnels endpoints).
_TUNNEL_AWARENESS_CACHE: Tuple[float, str] = (0.0, "")
_TUNNEL_AWARENESS_TTL_S = 5.0


def _format_tunnel_awareness() -> str:
    """Return a string the AI sees describing live MCP tunnels.

    Empty string when nothing is reachable — we don't bloat the
    prompt unless there is real information worth carrying.
    """
    global _TUNNEL_AWARENESS_CACHE
    now = time.monotonic()
    if (now - _TUNNEL_AWARENESS_CACHE[0] < _TUNNEL_AWARENESS_TTL_S
            and _TUNNEL_AWARENESS_CACHE[1]):
        return _TUNNEL_AWARENESS_CACHE[1]
    try:
        from tunnels import get_tunnels
        reg = get_tunnels()
    except Exception:  # noqa: BLE001
        return ""
    rows = []
    for u in reg._upstreams:
        online = bool(getattr(u, "_initialized", False)) and bool(
            getattr(u, "_server_info", {}))
        if not online:
            continue
        tools = []
        try:
            tools = u.tools() or []
        except Exception:  # noqa: BLE001
            tools = []
        server = (u._server_info or {}).get("name") or u.name
        version = (u._server_info or {}).get("version") or "?"
        sample = ", ".join(t.get("name", "?") for t in tools[:6])
        if len(tools) > 6:
            sample += ", … (" + str(len(tools) - 6) + " more)"
        rows.append("- " + u.name + " · " + server + " v" + str(version)
                    + " · " + str(len(tools)) + " tools · "
                    + u.url)
        if sample:
            rows.append("    sample tools: " + sample)
    if not rows:
        # Honest empty state: the model must know there is NOTHING to
        # call right now, instead of inventing tools (the old version
        # listed local primitives here that the capability boundary
        # hard-refuses — plans against them were rejected every time).
        s = ("\nNO MCP tunnels are connected right now — the only tools "
             "live are the four MCP introspection tools (who_am_i, "
             "list_platforms, tunnel_status, tunnel_probe). Do NOT "
             "invent or name any other tool; if the task needs engine "
             "tools, say the user must connect the editor first "
             "(Settings page).")
    else:
        s = ("Connected MCP tunnels (call them via tools/call "
             "with namespaced names like '<tunnel>.<tool_name>'):\n"
             + "\n".join(rows)
             + "\n\nOnly these namespaced tools and the four MCP "
             "introspection tools are callable. Nex's own "
             "filesystem/shell/host infrastructure is NOT an AI "
             "capability — calls to it are refused by the hard "
             "capability boundary.")
    _TUNNEL_AWARENESS_CACHE = (now, s)
    return s


def invalidate_tunnel_awareness() -> None:
    """Force a refresh on next read — call when a probe changes state."""
    global _TUNNEL_AWARENESS_CACHE
    _TUNNEL_AWARENESS_CACHE = (0.0, "")



# ---------------------------------------------------------------------------
# Streaming chat. Tokens are flushed to subscribers only at safe
# boundaries (see _StreamFlusher) so the front-end never sees partial
# tags, half-words, or jittery state flicker.
# ---------------------------------------------------------------------------

# Punctuation patterns that mark a "safe" point to flush text. We only
# flush AFTER one of these + whitespace, so we don't split inside a word.
_SENTENCE_END = re.compile(r"[.!?][\s\n]")

# A safe tag boundary is a complete [STATE] token. The TAG_RE already
# captures them, so we just rely on that.


def _iter_ollama_tokens(messages):
    """Yield content tokens from an Ollama streaming response.

    Each line of the response is a JSON object; the final one has
    done=true and may carry the full message in `message`.
    """
    url = OLLAMA_HOST.rstrip("/") + "/api/chat"
    body = json.dumps({
        "model": OLLAMA_MODEL, "messages": messages,
        "stream": True, "options": {"temperature": 0.7},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            chunk = (obj.get("message") or {}).get("content")
            if chunk:
                yield chunk
            if obj.get("done"):
                return


def _iter_openai_tokens(messages):
    """Yield content tokens from an OpenAI-compatible streaming response.

    Each line is "data: {...}" with the final frame being "data: [DONE]".
    Some servers send naked JSON without the SSE prefix; tolerate both.
    """
    url = OLLAMA_HOST.rstrip("/") + "/v1/chat/completions"
    body = json.dumps({
        "model": OLLAMA_MODEL, "messages": messages,
        "temperature": 0.7, "stream": True,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[len("data:"):].strip()
            if line == "[DONE]" or not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            chunk = delta.get("content")
            if chunk:
                yield chunk
            finish = choices[0].get("finish_reason")
            if finish:
                return


def model_chat_stream(messages):
    """Generator yielding content tokens from the configured model.

    Wraps _iter_ollama_tokens / _iter_openai_tokens. Retries once on
    transient connection errors."""
    iter_fn = _iter_openai_tokens if API_STYLE == "openai" else _iter_ollama_tokens
    last_exc = None
    for attempt in range(2):
        try:
            for tok in iter_fn(messages):
                yield tok
            return
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError) as exc:
            last_exc = exc
            time.sleep(0.3 * (attempt + 1))
            continue
    raise RuntimeError("model unreachable: " + repr(last_exc))


class _StreamFlusher:
    """Buffer tokens and yield clean text segments at safe boundaries.

    Safe boundary = any of:
      * a complete [STATE] tag was assembled
      * a sentence terminator (. ? !) followed by whitespace
      * a newline
      * the time since the last partial flush > max_idle_ms
      * the caller signals end-of-stream

    Yields tuples (clean_text, state_or_None, is_final). When is_final
    is True the caller should not expect any more deltas for this stream
    and may run the fallback engine.
    """

    # A regex that, at any position in the partial buffer, finds the
    # EARLIEST safe flush point. We want the leftmost of:
    #   * a complete [STATE] tag (including leading whitespace)
    #   * a sentence-end punctuation (followed by a word boundary)
    #   * a newline
    #
    # We match just the punctuation char (NOT the trailing whitespace)
    # so the punctuation is included in the flushed chunk — the user
    # sees "Hi there. More" not "Hi there More".
    _BOUNDARY_RE = re.compile(
        r"[.!?](?=\s)|[\n]|\s*\[(?:" + "|".join(EMOTION_STATES) + r")\]",
        re.IGNORECASE,
    )

    def __init__(self, max_idle_ms: int = 150) -> None:
        self.buf = ""
        self.last_flush_t = time.monotonic()
        self.max_idle_ms = max_idle_ms
        # Has any tag been published for this stream? If so, fallback is
        # suppressed (the model made a choice).
        self.tag_seen = False
        # When a tag flushes from an empty buffer (the tag is at the
        # start of a sentence, or the chunk is just a tag), we hold the
        # state here until the next text chunk arrives so the client
        # can apply face + text in a single frame. Without this, the
        # face would change to the emotion BEFORE the bubble updated,
        # which reads as the emotion being ahead of the talking.
        self.pending_state = None

    def feed(self, token: str) -> list:
        """Append a token. Return a list of (clean_text, state_or_None)
        tuples to flush. None means no flush needed yet."""
        if not token:
            return []
        self.buf += token
        return self._drain(force=False)

    def flush(self, force: bool = False) -> list:
        return self._drain(force=force)

    def _drain(self, force: bool) -> list:
        out = []
        now = time.monotonic()
        # Time-based flush (covers slow streams and long single tokens).
        if force or ((now - self.last_flush_t) * 1000.0 >= self.max_idle_ms):
            # If we have anything unflushed, emit it as one chunk. We
            # only emit if it doesn't end mid-tag (we'd lose the tag).
            if self.buf and not self._ends_mid_tag(self.buf):
                clean, state = self._strip_tags(self.buf, trim=False)
                if state:
                    self.tag_seen = True
                    if not clean and not self.pending_state:
                        # Defer state until next text chunk.
                        self.pending_state = state
                    elif clean:
                        # Combine with any pending state.
                        if self.pending_state:
                            out.append((clean, self.pending_state))
                            self.pending_state = None
                        else:
                            out.append((clean, state))
                elif clean:
                    if self.pending_state:
                        out.append((clean, self.pending_state))
                        self.pending_state = None
                    else:
                        out.append((clean, None))
                self.buf = ""
                self.last_flush_t = now
                return out
        while True:
            m = self._BOUNDARY_RE.search(self.buf)
            if not m:
                break
            matched = m.group()
            # Determine the slice to emit:
            #   * tag boundary (starts with '[') -> emit head INCLUDING
            #     the whole tag so we can parse it.
            #   * punctuation boundary -> emit head INCLUDING the
            #     punctuation char so it stays visible.
            if matched.lstrip().startswith("["):
                head = self.buf[:m.end()]
                next_pos = m.end()
            else:
                # punctuation: include the punctuation char in head.
                head = self.buf[:m.end()]
                next_pos = m.end()
            clean, state = self._strip_tags(head, trim=False)
            if state:
                self.tag_seen = True
                # If the chunk had no text, defer the state to the next
                # non-empty chunk so the client applies face + text
                # together. This is what stops emotions from appearing
                # "before" the talking (face change without bubble).
                if not clean:
                    self.pending_state = state
                else:
                    out.append((clean, state))
            elif clean:
                # We have text. If a state is pending from a previous
                # tag-only flush, combine it with this chunk.
                if self.pending_state:
                    out.append((clean, self.pending_state))
                    self.pending_state = None
                else:
                    out.append((clean, None))
            self.buf = self.buf[next_pos:]
            self.last_flush_t = now
        # If we still have a pending state and we're force-flushing,
        # and there's no more text coming, surface it as a state-only
        # chunk so the chat thread's caller can decide what to do.
        if self.pending_state and not self.buf:
            out.append(("", self.pending_state))
            self.pending_state = None
        return out

    @staticmethod
    def _ends_mid_tag(s: str) -> bool:
        """True if `s` ends inside a potential tag like '[SUR'."""
        # Look at the last '[' in the string. If it's not closed by ']'
        # we are mid-tag.
        i = s.rfind("[")
        if i == -1:
            return False
        return s.find("]", i) == -1

    @staticmethod
    def _strip_tags(s: str, trim: bool = True) -> Tuple[str, Optional[str]]:
        """Strip tags from a chunk, returning (clean, first_state).

        `trim` controls whether to strip leading/trailing whitespace:
          * trim=True  -> for the final / end-of-stream text
          * trim=False -> for individual delta chunks, so the concatenation
            of deltas preserves spaces (e.g. "!" + " Glad" stays
            "! Glad" not "!Glad").
        """
        m = TAG_RE.search(s)
        state = None
        if m:
            state = m.group(1).upper()
        out = []
        i = 0
        while i < len(s):
            tm = TAG_RE.match(s, i)
            if not tm:
                out.append(s[i])
                i += 1
                continue
            before = out[-1] if out else ""
            after = s[tm.end():tm.end() + 1]
            if before and before != " " and after and after != " " and \
               before.isalpha() and after.isalpha():
                out.append(" ")
            i = tm.end()
        text = "".join(out)
        if trim:
            text = text.strip()
        return text, state



# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class NexHandler(BaseHTTPRequestHandler):
    server_version = "Nex/0.2"

    # ----- helpers ---------------------------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        # NOTE: no Access-Control-Allow-Origin. The UI is same-origin.
        # A wildcard CORS here let ANY web page in the user's browser
        # drive the API cross-origin (with the token mandatory, that
        # page would still get 401 — but the preflight/leak surface
        # disappears entirely with no downside).
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: str, content_type: str) -> None:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_error(404, "Not Found")
            return
        # NOTE: the auth token is deliberately NOT injected into served
        # HTML (the old window.NEX_AUTH global made the token readable by
        # any page script — the XSS→token→MCP/stdio chain). Browsers
        # authenticate with the HttpOnly `nex_auth` cookie, set via one
        # top-level navigation to `/?nex_token=<t>` (_cookie_bootstrap).
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt: str, *args: Any) -> None:
        line = fmt % args
        # SSE streams carry the token as a query param (EventSource can't
        # set headers) — never let it into the access log.
        if AUTH_TOKEN:
            line = line.replace(AUTH_TOKEN, "***")
        sys.stderr.write("[nex %s] %s\n" % (self.log_date_time_string(), line))

    # ----- routing ---------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/style.css", "/app.js", "/webgl.js",
                    "/animations.js", "/provider_chip.js", "/providers.js",
                    "/api/health", "/api/state", "/api/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(404, "Not Found")

    # ----- auth -------------------------------------------------------------
    def _is_https(self) -> bool:
        # Behind the preview/LAN proxy the real scheme arrives as a
        # header; NEX_COOKIE_SECURE=1 forces the Secure flag anyway.
        if os.environ.get("NEX_COOKIE_SECURE", "").strip().lower() \
                in ("1", "true", "yes", "on"):
            return True
        return self.headers.get("X-Forwarded-Proto", "").strip().lower() \
            == "https"

    def _cookie_token(self) -> Optional[str]:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nex_auth":
                return v
        return None

    def _auth_ok(self) -> bool:
        """Every /api + /mcp request must carry the token.

        Acceptance, in order:
          * the `nex_auth` COOKIE — the browser path. The cookie is
            HttpOnly (page scripts — even an XSS — cannot read it),
            SameSite=Strict (never sent cross-site), Path=/, and Secure
            on https origins. Browsers obtain it by one top-level
            navigation to `/?nex_token=<t>` (the banner link), which
            sets the cookie and redirects to a clean URL — the token is
            NEVER injected into served HTML (window.NEX_AUTH is gone:
            a readable global was the XSS→token→MCP/stdio chain).
          * X-Nex-Auth / Authorization: Bearer — CLI + programmatic.
          * `?nex_auth=` on GET /api/events ONLY — legacy CLI/programmatic
            SSE (a browser never needs it; EventSource sends the cookie
            automatically same-origin).
        The token is ALWAYS set, so this is not a conditional gate: it
        IS the gate. Comparisons are constant-time (hmac.compare_digest)
        to defeat timing oracles."""
        tok = AUTH_TOKEN.encode("utf-8")
        ct = self._cookie_token()
        if ct and hmac.compare_digest(ct.encode("utf-8"), tok):
            return True
        provided = self.headers.get("X-Nex-Auth")
        if provided and hmac.compare_digest(provided.encode("utf-8"), tok):
            return True
        authz = self.headers.get("Authorization", "")
        if authz.startswith("Bearer ") and hmac.compare_digest(
                authz[len("Bearer "):].encode("utf-8"), tok):
            return True
        # Legacy programmatic SSE (EventSource in a headless client):
        # query token honored ONLY for the SSE stream, never anywhere
        # else — a token in the URL should have the smallest possible
        # surface (logs, proxies, history).
        if self.command == "GET":
            clean = self.path.split("?", 1)[0].rstrip("/")
            if clean == "/api/events":
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                try:
                    q = urllib.parse.parse_qs(query)
                except Exception:  # noqa: BLE001
                    q = {}
                qv = (q.get("nex_auth") or [""])[0]
                if qv and hmac.compare_digest(qv.encode("utf-8"), tok):
                    return True
        return False

    def _cookie_bootstrap(self) -> bool:
        """Top-level navigation with `?nex_token=<t>`: a valid token is
        exchanged for the HttpOnly `nex_auth` cookie, then a 302 drops
        the token from the address bar (history/logs keep a clean URL).
        Returns True when the request was handled (redirect sent)."""
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if not query or "nex_token" not in query:
            return False
        path = self.path.split("?", 1)[0]
        try:
            q = urllib.parse.parse_qs(query)
            nt = (q.get("nex_token") or [""])[0]
        except Exception:  # noqa: BLE001
            return False
        if not nt or not AUTH_TOKEN:
            return False
        if not hmac.compare_digest(nt.encode("utf-8"),
                                   AUTH_TOKEN.encode("utf-8")):
            # Invalid token: no cookie, no redirect — the page loads,
            # its API calls 401, and the auth hint tells the operator.
            return False
        secure = "; Secure" if self._is_https() else ""
        self.send_response(302)
        self.send_header("Set-Cookie",
                         "nex_auth=%s; Path=/; HttpOnly; SameSite=Strict%s"
                         % (nt, secure))
        self.send_header("Location", path or "/")
        self.end_headers()
        return True

    def _reject_auth(self) -> None:
        self._send_json(401, {"ok": False,
                              "error": "missing/invalid X-Nex-Auth"})

    def _auth_gate(self) -> bool:
        """True if the request must be authenticated (and wasn't).

        There is exactly ONE unauthenticated API path: the login
        endpoint (`POST /api/auth/session`) — it exists so a browser can
        turn a token the operator KNOWS into an HttpOnly cookie without
        ever putting the token in the URL/DOM (the preview/LAN case
        where the ?nex_token= link is not reachable). Everything else —
        including /api/health — requires the token (a 401 is still a
        valid "server is up" signal for boot-wait loops; see the tests)."""
        path = self.path.split("?", 1)[0]
        if path == "/api/auth/session" or path == "/api/auth/logout":
            return False
        sensitive = (path.startswith("/api/") or path == "/api"
                     or path == "/mcp" or path.startswith("/mcp/"))
        return sensitive and not self._auth_ok()

    def _handle_auth_session(self) -> None:
        """POST {token} -> Set-Cookie (login) / logout clears it.

        The token is compared constant-time; a wrong token costs a
        small fixed delay (cheap brute-force throttle for a 32-char
        random secret) and never sets a cookie."""
        path = self.path.split("?", 1)[0]
        secure = "; Secure" if self._is_https() else ""
        if path == "/api/auth/logout":
            self.send_response(200)
            self.send_header("Set-Cookie",
                             "nex_auth=; Path=/; HttpOnly; SameSite=Strict; "
                             "Max-Age=0%s" % secure)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true, "logout": true}')
            return
        body = self._read_json_body() or {}
        tok = str(body.get("token") or "")
        if tok and AUTH_TOKEN and hmac.compare_digest(
                tok.encode("utf-8"), AUTH_TOKEN.encode("utf-8")):
            self.send_response(200)
            self.send_header("Set-Cookie",
                             "nex_auth=%s; Path=/; HttpOnly; SameSite=Strict%s"
                             % (tok, secure))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true, "method": "cookie"}')
            return
        time.sleep(0.4)  # fixed delay on failure (throttle)
        self._send_json(401, {"ok": False, "error": "invalid token"})

    def do_GET(self) -> None:  # noqa: N802
        if self._cookie_bootstrap():
            return
        path = self.path.split("?", 1)[0]
        if self._auth_gate():
            self._reject_auth()
            return
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path in ("/", "/index.html"):
            self._send_file(os.path.join(FRONTEND_DIR, "index.html"),
                            "text/html; charset=utf-8")
        elif path == "/style.css":
            self._send_file(os.path.join(FRONTEND_DIR, "style.css"), "text/css; charset=utf-8")
        elif path == "/app.js":
            self._send_file(os.path.join(FRONTEND_DIR, "app.js"), "application/javascript; charset=utf-8")
        elif path == "/webgl.js":
            self._send_file(os.path.join(FRONTEND_DIR, "webgl.js"), "application/javascript; charset=utf-8")
        elif path == "/animations.js":
            self._send_file(os.path.join(FRONTEND_DIR, "animations.js"), "application/javascript; charset=utf-8")
        elif path == "/provider_chip.js":
            self._send_file(os.path.join(FRONTEND_DIR, "provider_chip.js"), "application/javascript; charset=utf-8")
        elif path == "/api/health":
            try:
                from tunnels import get_tunnels
                tunnel_summary = get_tunnels().summary()
            except Exception:
                tunnel_summary = {"total": 0, "online": 0, "platforms": []}
            self._send_json(200, {
                "ok": True,
                "api_style": API_STYLE,
                "ollama_host": OLLAMA_HOST,
                "ollama_model": OLLAMA_MODEL,
                "ollama_reachable": model_reachable(),
                "providers": _model_status(),
                "supported_states": list(EMOTION_STATES),
                "fallback_enabled": True,
                "mcp_enabled": True,
                "mcp_protocol": "2025-06-18",
                "mcp_endpoints": [
                    "POST /mcp",
                    "GET  /mcp/sse",
                    "GET  /api/tools",
                    "GET  /api/tunnels",
                    "POST /api/tunnels/probe",
                ],
                "tunnels": tunnel_summary,
            })
        elif path == "/api/state":
            # Current snapshot of conversation history length.
            with HISTORY_LOCK:
                n_msgs = len(HISTORY) - 1
            self._send_json(200, {"ok": True, "messages": n_msgs})
        elif path == "/api/events":
            self._handle_sse()
        elif path == "/mcp/sse":
            self._handle_mcp_sse()
        elif path == "/api/tools":
            # REST list of available tools (handy for the dev panel).
            # Includes namespaced upstream tools when tunnels are up.
            self._send_json(200, {"tools": self._mcp_tools_list().get("tools", [])})
        elif path == "/api/projects":
            from agent.projects_store import list_projects
            self._send_json(200, {"ok": True,
                                  "projects": list_projects()})
        elif path == "/api/project":
            from agent.projects_store import list_projects, load_project
            projs = list_projects(limit=1)
            if not projs:
                self._send_json(200, {"ok": True, "project": None})
            else:
                self._send_json(200, {"ok": True,
                                      "project": load_project(projs[0]["id"])})
        elif path.startswith("/api/project/"):
            from agent.projects_store import load_project
            pid = path[len("/api/project/"):].split("/")[0]
            doc = load_project(pid)
            if doc is None:
                self._send_json(404, {"ok": False, "error": "unknown project"})
            else:
                self._send_json(200, {"ok": True, "project": doc})
        elif path == "/api/agent/capabilities":
            # Live MCP capability registry (server view) — what the agent
            # actually sees, not a curated doc.
            from agent.server_run import agent_capabilities
            self._send_json(200, agent_capabilities())
        elif path == "/api/tunnels":
            from tunnels import get_tunnels
            summary = get_tunnels().summary()
            self._send_json(200, summary)
        elif path == "/api/tunnels/reload":
            from tunnels import reload_tunnels
            fresh = reload_tunnels()
            self._send_json(200, fresh.summary())
        elif path == "/api/tunnels/probe":
            self._handle_probe_all()
        elif path.startswith("/api/tunnels/stdio-config"):
            self._handle_stdio_config(query)
        elif path == "/api/settings/tunnels":
            self._handle_settings_tunnels()
        elif path == "/api/providers":
            self._send_json(200, {"ok": True, **_model_status()})
        elif path == "/api/settings/providers":
            self._send_json(200, ROUTER.settings_view())
        elif path.startswith("/api/plan"):
            self._handle_plan(path, query)
        elif path == "/settings.html" or path == "/settings":
            self._send_file(os.path.join(FRONTEND_DIR, "settings.html"),
                            "text/html; charset=utf-8")
            return
        elif path.startswith("/api/tunnels/"):
            from tunnels import get_tunnels, reload_tunnels
            tail = path[len("/api/tunnels/"):]
            if tail == "reload":
                fresh = reload_tunnels()
                self._send_json(200, fresh.summary())
            else:
                self.send_error(404, "Not Found")
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if self._auth_gate():
            self._reject_auth()
            return
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path == "/api/state":
            body = self._read_json_body()
            event = {
                "type": "state",
                "state": body.get("state", "IDLE"),
                "params": body.get("params", {}),
                "ts": time.time(),
            }
            BUS.publish(event)
            self._send_json(200, {"ok": True, "published": event})
            return
        if path == "/api/chat":
            self._handle_chat()
            return
        if path == "/api/settings/providers":
            # Live provider configuration: keys, models, roles, rate budget.
            # Keys go IN (0600 file / .env), never back out (masked only).
            body = self._read_json_body() or {}
            try:
                view = ROUTER.apply(body)
            except Exception as exc:  # noqa: BLE001
                self._send_json(400, {"ok": False, "error": repr(exc)})
                return
            self._send_json(200, view)
            return
        if path == "/api/settings/providers/test":
            body = self._read_json_body() or {}
            name = str(body.get("provider") or "").strip()
            if not name:
                self._send_json(400, {"ok": False,
                                      "error": "missing 'provider'"})
                return
            result = ROUTER.probe(name)
            self._send_json(200 if result.get("ok") else 200, result)
            return
        if path == "/api/settings/providers/models":
            body = self._read_json_body() or {}
            name = str(body.get("provider") or "").strip()
            if not name:
                self._send_json(400, {"ok": False,
                                      "error": "missing 'provider'"})
                return
            result = ROUTER.probe(name)
            self._send_json(200, {
                "ok": bool(result.get("ok")),
                "provider": name,
                "models": result.get("models") or [],
                "catalog": [c for c in _providers.CATALOG
                             if c.get("provider") == name],
                "error": result.get("error", ""),
            })
            return
        if path == "/api/project":
            # NEX 2.0: understand BEFORE building. Produces the Design
            # Document + draft plan (Plan Page) without executing anything.
            body = self._read_json_body() or {}
            goal = (body.get("goal") or "").strip()
            if not goal:
                self._send_json(400, {"ok": False, "error": "missing 'goal'"})
                return

            def _designer() -> None:
                try:
                    from agent.server_run import run_agent_goal
                    run_agent_goal(goal, bus=BUS, llm_call=model_chat,
                                   llm_reachable=model_reachable(),
                                   llm_builder=builder_chat,
                                   mode="design")
                except Exception as exc:  # noqa: BLE001
                    BUS.publish({"type": "agent.error", "error": repr(exc),
                                 "ts": time.time()})

            threading.Thread(target=_designer, daemon=True).start()
            self._send_json(200, {"ok": True, "queued": True, "goal": goal,
                                  "mode": "design"})
            return
        if path.startswith("/api/project/") and path.endswith("/build"):
            # Approve + build a designed project. The APPROVED plan
            # (Plan A, persisted at design time) is rebuilt and executed
            # EXACTLY — no silent re-planning between approval and work.
            # Only a critic REPLAN creates the next plan deliberately.
            pid = path[len("/api/project/"):-len("/build")]
            from agent.projects_store import (load_project_state,
                                              load_approved_graph)
            st = load_project_state(pid)
            if st is None:
                self._send_json(404, {"ok": False,
                                      "error": "unknown project"})
                return
            approved_graph = load_approved_graph(pid)

            def _builder() -> None:
                try:
                    from agent.server_run import run_agent_goal
                    result = run_agent_goal(st.goal or pid, bus=BUS,
                                            llm_call=model_chat,
                                            llm_reachable=model_reachable(),
                                            llm_builder=builder_chat,
                                            mode="build", state=st,
                                            graph=approved_graph)
                    BUS.publish({"type": "agent.report", "result": result,
                                 "ts": time.time()})
                except Exception as exc:  # noqa: BLE001
                    BUS.publish({"type": "agent.error", "error": repr(exc),
                                 "ts": time.time()})

            threading.Thread(target=_builder, daemon=True).start()
            self._send_json(200, {"ok": True, "queued": True,
                                  "project_id": pid, "mode": "build",
                                  "approved_plan": approved_graph is not None})
            return
        if path in ("/api/auth/session", "/api/auth/logout"):
            self._handle_auth_session()
            return
        if path == "/api/agent/run":
            self._handle_agent_run()
            return
        if path == "/api/agent/campaign":
            self._handle_agent_campaign()
            return
        if path == "/api/reset":
            with HISTORY_LOCK:
                HISTORY.clear()
                HISTORY.append({"role": "system", "content": NEX_SYSTEM_PROMPT})
            BUS.publish({"type": "state", "state": "IDLE", "params": {}, "ts": time.time()})
            self._send_json(200, {"ok": True})
            return
        if path == "/api/tools/call":
            # REST shim — SAME capability boundary as /mcp.
            body = self._read_json_body()
            name = body.get("name") or ""
            args = body.get("arguments") or {}
            env = _boundary_router(name, args or {})
            self._send_json(200, {"ok": True, "name": name,
                                  "result": _safe_env_payload(env),
                                  "isError": env.get("isError", False)})
            return
        if path == "/mcp":
            self._handle_mcp()
            return
        if path == "/api/tunnels/reload":
            # POST is the canonical method for /reload; GET works too.
            from tunnels import reload_tunnels
            fresh = reload_tunnels()
            self._send_json(200, fresh.summary())
            return
        if path == "/api/tunnels/probe":
            self._handle_probe_all()
            return
        if path.startswith("/api/tunnels/stdio-config"):
            self._handle_stdio_config(query)
            return
        if path == "/api/settings/connect":
            self._handle_settings_connect()
            return
        if path.startswith("/api/plan"):
            self._handle_plan(path, query)
            return
        if path == "/api/tunnels":
            # POST /api/tunnels with {"tunnels":[{name,url,label}]}
            # adds tunnels at runtime; replaces env-derived defaults
            # when {"replace":true}.
            # SECURITY: stdio entries are process execution — only
            # allowlisted commands (built-in catalog + NEX_STDIO_ALLOW)
            # are accepted; anything else is a 400.
            from tunnels import reload_tunnels, validate_extra_entries
            body = self._read_json_body() or {}
            extra = body.get("tunnels") or []
            replace = bool(body.get("replace"))
            if not isinstance(extra, list):
                self._send_json(400, {"error": "tunnels must be a list"})
                return
            problems = validate_extra_entries(extra)
            if problems:
                self._send_json(400, {"ok": False,
                                      "error": "; ".join(problems)})
                return
            fresh = reload_tunnels(extra=extra, replace=replace)
            # Persist user-added servers so they survive restarts.
            if not replace:
                from tunnels import add_user_tunnel
                saved = []
                for entry in extra:
                    if isinstance(entry, dict) and entry.get("name"):
                        add_user_tunnel(entry)
                        saved.append(entry.get("name"))
            self._send_json(200, dict(fresh.summary(),
                                      persisted=saved if not replace else []))
            return
        if path.startswith("/api/tunnels/"):
            # /api/tunnels/<name> DELETE removes the named tunnel,
            # POST re-probes a single tunnel.
            from tunnels import get_tunnels
            from upstream import UpstreamError
            name = path[len("/api/tunnels/"):]
            if "/" in name:
                self.send_error(404, "Not Found"); return
            if self.command == "DELETE":
                self._handle_tunnel_delete(name)
                return
            if self.command == "POST":
                for u in get_tunnels()._upstreams:
                    if u.name == name:
                        try:
                            u.connect()
                            self._send_json(200, u.status())
                        except UpstreamError as exc:
                            self._send_json(502, {
                                "error": str(exc),
                                **u.status(),
                            })
                        return
                self._send_json(404, {"error": "unknown tunnel: " + name})
                return
            self.send_error(405, "Method not allowed")
            return
        # /api/tools/<name> shorthand — SAME capability boundary as /mcp.
        if path.startswith("/api/tools/"):
            name = path[len("/api/tools/"):]
            body = self._read_json_body()
            env = _boundary_router(name, body or {})
            self._send_json(200, {"ok": True, "name": name,
                                  "result": _safe_env_payload(env),
                                  "isError": env.get("isError", False)})
            return
        self.send_error(404, "Not Found")

    # ----- MCP (Model Context Protocol) over HTTP+SSE ---------------------

    def _handle_probe_all(self) -> None:
        """Probe one or all configured tunnels, returning reachability
        + server info for each. Acceptable as both GET and POST
        (POST takes a JSON body so the caller can pick targets)."""
        from tunnels import get_tunnels
        from upstream import UpstreamError
        body = self._read_json_body() or {}
        targets = body.get("platforms") or [
            u.name for u in get_tunnels()._upstreams
        ]
        results = []
        for u in get_tunnels()._upstreams:
            if u.name in targets:
                try:
                    info = u.connect()
                    results.append({"platform": u.name, "ok": True,
                                    "serverInfo": info.get(
                                        "serverInfo", info),
                                    "tools": len(u.tools() or [])})
                except UpstreamError as exc:
                    results.append({"platform": u.name, "ok": False,
                                    "error": str(exc)})
        invalidate_tunnel_awareness()
        self._send_json(200, {"probed": results, "ts": time.time()})

    def _handle_settings_tunnels(self) -> None:
        """Catalog + live status of every default MCP tunnel.

        Drives the `/settings.html` page and the dev-panel connect
        buttons. Returns both the catalog (what we CAN connect to)
        and the live status (what is registered right now).
        """
        from upstream import DEFAULT_TUNNELS
        from tunnels import get_tunnels
        reg = get_tunnels()
        catalog = []
        for c in DEFAULT_TUNNELS:
            entry = {
                "name": c.get("name"),
                "label": c.get("label") or c.get("name"),
                "kind": "stdio" if _is_stdio_entry(c) else "http",
                "command": c.get("command"),
                "args": list(c.get("args") or []),
                "url": c.get("url"),
                "platform": sys.platform,
                "description": _tunnel_blurb(c),
            }
            catalog.append(entry)
        from tunnels import load_user_tunnels
        user_names = {c.get("name") for c in load_user_tunnels()}
        registered = []
        for u in reg._upstreams:
            online = bool(getattr(u, "_initialized", False))
            try:
                tools = u.tools() or []
            except Exception:  # noqa: BLE001
                tools = []
            registered.append({
                "name": u.name,
                "label": u.label or u.name,
                "user_added": u.name in user_names,
                "online": online,
                "server_info": u._server_info if online else {},
                "tools_count": len(tools),
                "tools_sample": [t.get("name") for t in tools[:6]],
                "url": u.url,
                "last_error": u._last_error,
            })
        self._send_json(200, {
            "catalog": catalog,
            "registered": registered,
            "platform": sys.platform,
            "ts": time.time(),
        })

    def _handle_tunnel_delete(self, name: str) -> None:
        """DELETE /api/tunnels/<name> — unregister now + un-persist."""
        from tunnels import get_tunnels, remove_user_tunnel
        reg = get_tunnels()
        with reg._lock:
            reg._upstreams[:] = [u for u in reg._upstreams if u.name != name]
        was_saved = remove_user_tunnel(name)
        self._send_json(200, {
            "removed": name,
            "was_saved": was_saved,
            "platforms": [u.name for u in reg._upstreams],
        })

    def do_DELETE(self) -> None:  # noqa: N802
        """DELETE support (settings page removes user-added MCP servers)."""
        if self._auth_gate():
            self._reject_auth()
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/api/tunnels/"):
            name = path[len("/api/tunnels/"):]
            if name and "/" not in name:
                self._handle_tunnel_delete(name)
                return
        self.send_error(404, "Not Found")

    def _handle_settings_connect(self) -> None:
        """Connect an editor on demand.

        POST body: `{"name":"roblox-studio"}` — picks that default
        tunnel from the catalog, registers it (so it shows in /api/tunnels
        and the AI's awareness), and runs the probe. Returns the
        same shape as /api/tunnels/probe for one platform.
        """
        from tunnels import get_tunnels, reload_tunnels
        from upstream import DEFAULT_TUNNELS, UpstreamError
        body = self._read_json_body() or {}
        name = (body.get("name") or "").strip()
        if not name:
            self._send_json(400, {"ok": False,
                                  "error": "missing 'name'"})
            return
        # If not already in the registry, install it.
        reg = get_tunnels()
        registered_names = {u.name for u in reg._upstreams}
        if name not in registered_names:
            entry = next((c for c in DEFAULT_TUNNELS
                          if c.get("name") == name), None)
            if not entry:
                self._send_json(404, {"ok": False,
                                      "error": "unknown tunnel: " + name})
                return
            reload_tunnels(extra=[entry])
            reg = get_tunnels()
        target = next((u for u in reg._upstreams if u.name == name),
                      None)
        if target is None:
            self._send_json(500, {"ok": False,
                                  "error": "install succeeded but no row?"})
            return
        try:
            info = target.connect()
        except UpstreamError as exc:
            invalidate_tunnel_awareness()
            self._send_json(200, {
                "ok": False, "platform": name,
                "error": str(exc),
            })
            return
        # If the circuit breaker opened or the upstream replied with an
        # embedded error, surface that as ok=False instead of pretending
        # we got connected.
        if not isinstance(info, dict) or "error" in info:
            invalidate_tunnel_awareness()
            err_msg = ""
            if isinstance(info, dict):
                err_msg = str(info.get("error", "initialize failed"))
            self._send_json(200, {
                "ok": False, "platform": name,
                "error": err_msg or "connect returned no info",
            })
            return
        try:
            tools = target.tools() or []
        except Exception:  # noqa: BLE001
            tools = []
        invalidate_tunnel_awareness()
        self._send_json(200, {
            "ok": True,
            "platform": name,
            "server_info": info.get("serverInfo", info),
            "tools_count": len(tools),
            "tools_sample": [t.get("name") for t in tools[:6]],
            "url": target.url,
        })

    def _handle_plan(self, path, query) -> None:
        """Plan submission, status, confirm, cancel, and execute.

        Routes:
          POST /api/plan               submit a new plan
          GET  /api/plan               list recent plans
          GET  /api/plan/<id>          get one plan's status
          POST /api/plan/<id>/confirm  mark destructive steps confirmed
          POST /api/plan/<id>/cancel   drop the plan
          POST /api/plan/<id>/execute  run the next pending step (or all)
        """
        from tunnels import get_tunnels
        # list
        if path == "/api/plan":
            if self.command == "GET":
                plans = sorted(_mc.PLAN_STORE.values(),
                                key=lambda p: p["submitted_at"],
                                reverse=True)
                self._send_json(200, {
                    "plans": [_mc._public_view(p) for p in plans[:16]],
                    "ts": time.time(),
                })
                return
            if self.command == "POST":
                body = self._read_json_body() or {}
                # The model wraps plans as {"plan": {...}}. Accept both
                # that envelope and a bare plan dict.
                plan_obj = body.get("plan") if "plan" in body else body
                if not isinstance(plan_obj, dict):
                    self._send_json(400, {"ok": False,
                                          "error": "expected {'plan':{...}} or a bare plan dict"})
                    return
                submission = _mc.submit_plan(plan_obj)
                status = 200 if submission.get("ok") else 400
                self._send_json(status, submission)
                return
            self.send_error(405, "method not allowed")
            return

        # Anything else under /api/plan/<id>[/...] —
        # extract the id, validate, dispatch.
        tail = path[len("/api/plan/"):]
        if "/" not in tail:
            # GET /api/plan/<id>
            if self.command != "GET":
                self.send_error(405, "method not allowed"); return
            plan = _mc.get_plan(tail)
            if plan is None:
                self._send_json(404, {"ok": False, "error": "no such plan"}); return
            self._send_json(200, _mc._public_view(plan))
            return

        pid, action = tail.split("/", 1)
        plan = _mc.get_plan(pid)
        if plan is None:
            self._send_json(404, {"ok": False, "error": "no such plan"}); return
        if self.command != "POST":
            self.send_error(405, "method not allowed"); return

        if action == "confirm":
            view = _mc.confirm_plan(pid)
            if view is None:
                self._send_json(404, {"ok": False, "error": "no such plan"}); return
            self._send_json(200, view)
            return

        if action == "cancel":
            view = _mc.cancel_plan(pid)
            if view is None:
                self._send_json(404, {"ok": False, "error": "no such plan"}); return
            self._send_json(200, view)
            return

        if action == "autonomous":
            # THE BRIDGE: run a confirmed MC plan through the ONE canonical
            # execution pipeline (validate against the live registry ->
            # TaskGraph -> AutonomousAgent: policy gate, verification,
            # multi-pass recovery, checkpoints). This is not the sequential
            # step executor; tool success here is verified, not assumed.
            view = _mc._public_view(plan)
            plan_body = plan["plan"]  # the submitted plan document itself
            if view["cancelled"]:
                self._send_json(409, {"ok": False,
                                      "error": "plan was cancelled"}); return
            if not view["confirmed"]:
                self._send_json(409, {
                    "ok": False,
                    "error": "plan has destructive steps and must be "
                             "confirmed first (POST /api/plan/%s/confirm)"
                             % pid}); return

            from agent.model_planner import (validate_plan_deep,
                                             plan_to_graph)
            from agent.server_run import _registry
            registry = _registry()
            errors, warnings = validate_plan_deep(plan_body, registry)
            if errors:
                self._send_json(422, {
                    "ok": False,
                    "error": "plan failed live-registry validation",
                    "validation_errors": errors,
                    "validation_warnings": warnings,
                    "plan": view}); return
            graph = plan_to_graph(plan_body, registry)
            if not graph.all():
                self._send_json(422, {
                    "ok": False,
                    "error": "no executable steps after registry validation",
                    "validation_warnings": warnings,
                    "plan": view}); return

            def _runner() -> None:
                try:
                    from agent.server_run import run_agent_goal
                    result = run_agent_goal(
                        plan_body.get("title") or "MC plan %s" % pid,
                        bus=BUS,
                        llm_call=model_chat,
                        llm_reachable=model_reachable(),
                        llm_builder=builder_chat,
                        mode="build",
                        graph=graph,
                    )
                    result = dict(result) if isinstance(result, dict) else {}
                    result["bridged_from_plan"] = pid
                    result["validation_warnings"] = warnings
                    # The TaskGraph path verifies results; reflect that
                    # honestly in the MC plan record instead of the
                    # sequential executor's unverified "completed".
                    try:
                        rec = _mc.get_plan(pid)
                        if rec is not None:
                            rec["verified"] = bool(
                                (result.get("report") or {}).get("success"))
                    except Exception:  # noqa: BLE001
                        pass
                    BUS.publish({"type": "agent.report", "result": result,
                                 "ts": time.time()})
                except Exception as exc:  # noqa: BLE001
                    BUS.publish({"type": "agent.error",
                                 "source": "plan_bridge", "plan": pid,
                                 "error": repr(exc), "ts": time.time()})

            threading.Thread(target=_runner, daemon=True).start()
            self._send_json(202, {
                "ok": True,
                "bridged": pid,
                "note": "plan handed to the autonomous agent pipeline; "
                        "stream /api/events (SSE) for agent.plan_ready / "
                        "agent.report",
                "validation_warnings": warnings,
            })
            return

        if action == "execute":
            # Body may carry {"step": <int>} to run a single step,
            # or {"stop_on_error": false} to keep going past failures.
            # Default: run all remaining steps.
            body = self._read_json_body() or {}
            reg = get_tunnels()
            # Build a tool_router that understands bare + namespaced names.
            def _router(name, args):
                return reg.route(name, args or {})
            if "step" in body:
                try:
                    idx = int(body["step"])
                except (TypeError, ValueError):
                    self._send_json(400, {"ok": False,
                                          "error": "'step' must be an integer"})
                    return
                out = _mc.execute_plan_step(pid, idx, _router)
                if out is None:
                    self._send_json(404, {"ok": False, "error": "no such plan"}); return
                status = 200 if out.get("ok") else 400
                self._send_json(status, out)
                return
            stop_on_error = bool(body.get("stop_on_error", True))
            out = _mc.execute_plan_all(pid, _router, stop_on_error=stop_on_error)
            if out is None:
                self._send_json(404, {"ok": False, "error": "no such plan"}); return
            self._send_json(200, out if isinstance(out, dict) else {"ok": True, "view": out})
            return

        self.send_error(404, "unknown plan action: " + action)

    def _handle_stdio_config(self, query=None) -> None:
        """Return ready-to-paste JSON snippets configuring Nex as a
        stdio MCP server in every mainstream client.

        Different MCP hosts ship wildly different config-file shapes
        (root key, file format, even commenting syntax). This one
        endpoint emits them all so users don't need to remember
        which client wants what.
        """
        from urllib.parse import parse_qs
        nex_path = None
        if query:
            try:
                q = parse_qs(query)
                nex_path = (q.get("path") or [None])[0]
            except Exception:  # noqa: BLE001
                nex_path = None
        if not nex_path:
            nex_path = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "nex-stdio"))
            if not os.path.exists(nex_path):
                # Fall back to `python -m nex.stdio_server` for hosts
                # that have Python on PATH and don't mind modules.
                python_path = sys.executable
                config_cmd = python_path
                config_args = ["-m", "nex.stdio_server"]
                # We'll emit both: python -m path as the safer choice
                # and the shebang path as the convenience option.
                snippets_alt = python_path
            else:
                config_cmd = nex_path
                config_args = []
                snippets_alt = None
        else:
            config_cmd = nex_path
            config_args = []

        # Roblox Studio's official MCP server is stdio only — expose
        # the platform-specific default command so users can wire it
        # in directly. We detect the platform at request time.
        is_windows = sys.platform.startswith("win")
        roblox_cmd = "cmd" if is_windows else "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"
        if is_windows:
            roblox_args = ["/c",
                           "%LOCALAPPDATA%\\Roblox\\mcp.bat"]
        else:
            roblox_args = []

        snippets = {
            # The big four — Claude Desktop / Claude Code / Cursor all
            # use the same JSON shape with `mcpServers`.
            "claude_desktop": {
                "mcpServers": {
                    "nex": {
                        "command": config_cmd,
                        "args": config_args,
                    },
                },
                "_path_hint": {
                    "macos": "~/Library/Application Support/Claude/"
                             "claude_desktop_config.json",
                    "windows": "%APPDATA%\\Claude\\"
                               "claude_desktop_config.json",
                    "linux": "~/.config/Claude/claude_desktop_config.json",
                },
            },
            "claude_code": {
                "mcpServers": {
                    "nex": {
                        "command": config_cmd,
                        "args": config_args,
                    },
                },
                "_commands": [
                    'claude mcp add nex --transport stdio -- '
                    + config_cmd + (" ".join(config_args)
                                    and " " + " ".join(config_args)
                                    or "")
                ],
                "_path_hint": "~/.mcp.json or <project>/.mcp.json",
            },
            "cursor": {
                "mcpServers": {
                    "nex": {
                        "command": config_cmd,
                        "args": config_args,
                        "type": "stdio",
                    },
                },
                "_path_hint": "~/.cursor/mcp.json",
            },
            "windsurf": {
                "mcpServers": {
                    "nex": {
                        "command": config_cmd,
                        "args": config_args,
                    },
                },
                "_path_hint": "~/.codeium/windsurf/mcp_config.json",
            },
            # VS Code Copilot — root key `servers`, NOT `mcpServers`.
            "vscode_copilot": {
                "servers": {
                    "nex": {
                        "type": "stdio",
                        "command": config_cmd,
                        "args": config_args,
                    },
                },
                "_path_hint": "<workspace>/.vscode/mcp.json",
            },
            # Codex CLI uses TOML, not JSON — emit a TOML snippet
            # string in addition to a Python-style hint for copy.
            "codex_cli": {
                "_toml": "[mcp_servers.nex]\n"
                         "command = \"%s\"\n"
                         "args = [%s]\n"
                         "start = \"always\"\n" % (
                             config_cmd,
                             ", ".join('"%s"' % a for a in config_args),
                         ),
                "_path_hint": "~/.codex/config.toml",
            },
            # Zed editor — JSON with `context_servers` root key.
            "zed": {
                "context_servers": {
                    "nex": {
                        "command": {"path": config_cmd,
                                    "args": config_args},
                        "transport": "stdio",
                    },
                },
                "_path_hint": "~/.config/zed/settings.json",
            },
            # Roblox Studio's own stdio MCP — wired in as
            # `Roblox_Studio` server so Claude et al. reach the
            # editor directly without going through Nex.
            "roblox_studio_official": {
                "mcpServers": {
                    "Roblox_Studio": {
                        "command": roblox_cmd,
                        "args": roblox_args,
                    },
                },
                "_note": "Roblox Studio's official MCP server is stdio-only. "
                         "The HTTP endpoints at :3001/:3002 are internal "
                         "plugin-process long-polling, not the AI-client transport.",
            },
        }
        self._send_json(200, {"snippets": snippets, "ts": time.time(),
                              "platform": sys.platform,
                              "nex_path": nex_path})

    def _handle_mcp(self) -> None:
        """MCP JSON-RPC 2.0 over HTTP — also the tunnel gateway.

        We support the full MCP 2025-06-18 spec surface:

          * `initialize` / `notifications/initialized`  — full
            Streamable-HTTP session protocol (Mcp-Session-Id echoed
            when the client sends it; we allocate one if missing).
          * `ping`
          * `tools/list` — local Nex sandbox tools + every upstream
            tool exposed by Roblox Studio / Unreal Engine / Blender
            / VS Code / custom tunnels, each prefixed `<platform>.`.
          * `tools/call` — routed by prefix to the appropriate
            upstream (or, if unprefixed, to a local Nex tool).
          * `notifications/cancelled`, `notifications/progress`,
            `notifications/initialized` — accepted silently.
          * `resources/list`, `resources/read`, `prompts/list`,
            `prompts/get` — protocol metadata ONLY (what Nex is, which
            MCP servers are connected, curated per-platform tool
            guides). No filesystem, log, or host-state resources.
        Server->client notifications stream out on `/mcp/sse`.
        """
        body = self._read_json_body()
        if not body:
            self._send_json(400, {"ok": False, "error": "empty body"})
            return
        method = body.get("method")
        req_id = body.get("id")
        params = body.get("params") or {}
        # If the body is JSON-RPC but the request is a notification
        # (no id), we still respond 204 (spec).
        is_notification = "id" not in body
        # Honor the spec's optional session header. We allocate a
        # session id on initialize so subsequent requests can pin it.
        client_sid = self.headers.get("Mcp-Session-Id") \
            or self.headers.get("mcp-session-id")
        if client_sid and not _MCP_SESSIONS.get(client_sid):
            # The client thinks it has a session with us but we
            # forgot — politely reset by treating it as new.
            client_sid = None
        if method == "initialize":
            new_sid = client_sid or _new_session_id()
            _MCP_SESSIONS[new_sid] = time.time()
            result = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {
                    "name": "nex",
                    "version": "1.0",
                    "description":
                        "Nex MCP tunnel — aggregates Roblox Studio, "
                        "Unreal Engine 5.8, Blender, VS Code and "
                        "local sandbox tools.",
                },
                "capabilities": {
                    "tools":     {"listChanged": True},
                    "resources": {"subscribe": True},
                    "prompts":   {"listChanged": False},
                    "logging":   {},
                },
                "instructions": (
                    "Nex is an MCP tunnel. Tools from connected "
                    "editors are namespaced — e.g. "
                    "roblox-studio.execute_luau or "
                    "unreal-engine.spawn_actor. The only UNprefixed "
                    "tools are the MCP introspection ones: who_am_i, "
                    "list_platforms, tunnel_status, tunnel_probe. "
                    "There are no other local tools — filesystem, "
                    "shell, and host tools are server infrastructure, "
                    "not capabilities, and calls to them are refused "
                    "by the hard capability boundary. Use "
                    "list_platforms to see what's reachable. For "
                    "Roblox Studio / Unreal Engine, read the "
                    "'mcp://roblox-studio/guide' and "
                    "'mcp://unreal-engine/guide' resources (or call "
                    "the 'roblox_explain_tools' / 'unreal_explain_tools' "
                    "prompts) to get a full, explained catalog of "
                    "every engine tool before you call one. Prefer "
                    "read tools (get_*/list_*) before write tools, "
                    "and confirm destructive steps with the user."
                ),
            }
            if is_notification:
                self._send_mcp_session(new_sid); self.send_response(204)
                self.end_headers(); return
            self._send_json_with_session(
                200, self._jsonrpc_result(req_id, result), new_sid)
            return
        if method == "notifications/initialized":
            self.send_response(204); self.end_headers(); return
        if method == "ping":
            if is_notification:
                self.send_response(204); self.end_headers(); return
            self._send_json(200, self._jsonrpc_result(req_id, {}))
            return
        if method == "tools/list":
            result = self._mcp_tools_list()
            if is_notification:
                self.send_response(204); self.end_headers(); return
            self._send_json(200, self._jsonrpc_result(req_id, result))
            return
        if method == "tools/call":
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            try:
                tool_result = self._mcp_tools_call(name, arguments)
            except Exception as exc:  # noqa: BLE001
                tool_result = {
                    "content": [{"type": "text",
                                 "text": "tool crashed: " + repr(exc)}],
                    "isError": True,
                }
            if is_notification:
                self.send_response(204); self.end_headers(); return
            self._send_json(200, self._jsonrpc_result(req_id, tool_result))
            return
        if method == "resources/list":
            result = self._mcp_resources_list()
            if is_notification:
                self.send_response(204); self.end_headers(); return
            self._send_json(200, self._jsonrpc_result(req_id, result))
            return
        if method == "resources/read":
            uri = params.get("uri") or ""
            result = self._mcp_resources_read(uri)
            if is_notification:
                self.send_response(204); self.end_headers(); return
            self._send_json(200, self._jsonrpc_result(req_id, result))
            return
        if method == "prompts/list":
            result = self._mcp_prompts_list()
            if is_notification:
                self.send_response(204); self.end_headers(); return
            self._send_json(200, self._jsonrpc_result(req_id, result))
            return
        if method == "prompts/get":
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            result = self._mcp_prompts_get(name, args)
            if is_notification:
                self.send_response(204); self.end_headers(); return
            self._send_json(200, self._jsonrpc_result(req_id, result))
            return
        # Unknown method — protocol-level error.
        if is_notification:
            self.send_response(204); self.end_headers(); return
        self._send_json(200, self._jsonrpc_error(
            req_id, -32601, "Method not found: " + str(method)))

    # ------------ MCP route helpers (tunnel-aware) -------------------------

    def _mcp_tools_list(self) -> Dict[str, Any]:
        """Aggregate meta + local + upstream tools."""
        from tunnels import get_tunnels
        # THE CAPABILITY SURFACE: MCP introspection + everything from
        # explicitly connected servers.
        # No sandbox/filesystem/shell/host tools — Nex infrastructure is
        # not Nex's AI capability surface.
        meta = _NEX_META_TOOL_DEFS()
        try:
            aggregated = get_tunnels().aggregated_tools() or []
        except Exception:  # noqa: BLE001
            aggregated = []
        # Dedupe by name (the local boundary provider already surfaces
        # the introspection tools inside `aggregated`).
        seen = set()
        tools = []
        for t in list(meta) + list(aggregated):
            n = t.get("name")
            if n and n in seen:
                continue
            seen.add(n)
            tools.append(t)
        return {"tools": tools,
                "_meta": {"gateway": "nex",
                          "tunnels_online":
                              [s["name"] for s in
                               get_tunnels().list_upstreams()
                               if s.get("tools_count", 0) > 0]}}

    def _mcp_tools_call(self, name: str,
                        arguments: Dict[str, Any]) -> Dict[str, Any]:
        # ---- THE BOUNDARY (architecture invariant) --------------------------
        # ALWAYS enforced — there is no environment switch that turns the
        # boundary off. Any action that is not a connected MCP tool is
        # rejected here; this is a real gate, not a prompt instruction.
        from mcp.policy import authorize, current_policy
        from mcp.capability import ToolCapability
        # Split into (server, BARE tool name) — the policy matches bare
        # names. Passing the full "server.tool" name would let a
        # connected server expose a tool literally named `run_command`
        # (called as `evilserver.run_command`) that slips past
        # ALWAYS_DENIED, the PROCESS-execution names, and
        # tool-allow-list entries alike.
        if "." in name:
            server, tool_name = name.split(".", 1)
        else:
            server, tool_name = None, name
        cap = None
        for t in self._mcp_tools_list().get("tools", []):
            if t.get("name") == name:
                cd = t.get("_capability") or {}
                cap = ToolCapability(
                    category=cd.get("category", "unknown"),
                    read_only=cd.get("read_only", False),
                    reversible=cd.get("reversible", False),
                    destructive=cd.get("destructive", False),
                    network=cd.get("network", False),
                    requires_confirmation=cd.get("requires_confirmation", True),
                    source=cd.get("source", "conservative"),
                )
                break
        decision = authorize(server, tool_name, cap, current_policy(),
                             args=arguments if isinstance(arguments, dict)
                             else None)
        if not decision.allowed:
            return {"content": [{"type": "text",
                                 "text": "blocked by the capability boundary: "
                                 + decision.reason}],
                    "isError": True}
        # CONFIRMATION IS ENFORCED HERE TOO. A direct MCP/REST call has no
        # way to ask a human, so a call that requires confirmation is
        # refused with the exact operator action that would allow it.
        # (Previously this decision was computed and then ignored — a
        # destructive or code-executing tool ran on nothing but trust.)
        if decision.requires_confirmation:
            return {"content": [{"type": "text",
                                 "text": "confirmation required: "
                                 + decision.reason}],
                    "isError": True}
        # Local-prefix tools below are handled without the registry.
        if name in ("who_am_i", "list_platforms",
                    "tunnel_status", "tunnel_probe"):
            handler = _NEX_META_TOOL_HANDLERS().get(name)
            if handler is None:
                return self._tool_error("unknown meta tool: " + name)
            try:
                payload = handler(arguments or {})
            except Exception as exc:  # noqa: BLE001
                return self._tool_error(repr(exc))
            return {"content": [{"type": "text", "text":
                                json.dumps(payload, ensure_ascii=False,
                                           sort_keys=True, indent=2)}],
                    "isError": False}
        # BOUNDARY: prefixed names route to their upstream; bare names
        # only pass if they are MCP introspection. Sandbox/filesystem/
        # shell tools are infrastructure, NOT callable here.
        from tunnels import get_tunnels
        try:
            if "." in name:
                env = get_tunnels().route(name, arguments or {})
            else:
                env = _boundary_router(name, arguments or {})
            return env
        except Exception as exc:  # noqa: BLE001
            return self._tool_error(repr(exc))

    @staticmethod
    def _tool_error(msg: str) -> Dict[str, Any]:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "Error: " + msg}],
        }

    # ------------ MCP resources / prompts ----------------------------------

    def _mcp_resources_list(self) -> Dict[str, Any]:
        # THE BOUNDARY, resources edition: only PROTOCOL METADATA about
        # Nex itself. No filesystem, log, workspace, or host-state
        # resources — the AI has no route to request those.
        from tunnels import get_tunnels
        out = [
            {"uri": "nex://about",
             "name": "About Nex",
             "description": "Protocol metadata: what Nex is and the hard "
                            "capability boundary it enforces.",
             "mimeType": "application/json"},
            {"uri": "nex://tunnels",
             "name": "Connected MCP servers",
             "description": "Health of every explicitly connected MCP "
                            "server.",
             "mimeType": "application/json"},
        ]
        # Curated, engine-specific tool guides — the MCP-only explanation
        # surface for Roblox Studio and Unreal Engine. These are meaningful
        # even when the editor is offline (the guide is curated, not live).
        try:
            from mcp_engines import SUPPORTED, platform_guide_resource
            for plat in SUPPORTED:
                body = platform_guide_resource(plat)
                if body:
                    out.append({
                        "uri": "mcp://" + plat + "/guide",
                        "name": plat + " tool guide",
                        "description": ("Curated guide to every MCP tool "
                                        + plat + " exposes (what it does, "
                                        "when to use it, params, examples, "
                                        "caveats) plus how to connect."),
                        "mimeType": "text/markdown",
                        "_meta": {"platform": plat, "kind": "tool-guide"},
                    })
        except Exception:  # noqa: BLE001
            pass
        for u in get_tunnels().list_upstreams():
            label = (u.get("label") or u["name"]).lower().replace(" ", "-")
            out.append({
                "uri": "tunnel://" + u["name"] + "/info",
                "name": u.get("label") or u["name"] + " tunnel info",
                "description": "Reachability + cached tools for the "
                              + (u.get("label") or u["name"]) + " MCP tunnel.",
                "mimeType": "application/json",
                "_meta": {"platform": u["name"], "label": label},
            })
        return {"resources": out}

    def _mcp_resources_read(self, uri: str) -> Dict[str, Any]:
        # Same boundary as tools: only protocol metadata is readable.
        # Filesystem/log/state reads are infrastructure, not resources.
        from tunnels import get_tunnels
        if uri == "nex://about":
            try:
                tunnels = [s.get("name") for s in
                           get_tunnels().list_upstreams()]
            except Exception:  # noqa: BLE001
                tunnels = []
            text = json.dumps({
                "server": "nex",
                "kind": "mcp-aggregator",
                "capability_boundary": (
                    "Nex acts ONLY through explicitly connected MCP "
                    "servers. Filesystem, "
                    "shell, host-state, and log access are not AI "
                    "capabilities and are not exposed as resources."),
                "connected_mcp_servers": tunnels,
                "resource_kinds": [
                    "nex://about (this document)",
                    "nex://tunnels (MCP connection metadata)",
                    "tunnel://<name>/info (per-server metadata)",
                    "mcp://<platform>/guide (curated tool guides)"],
            }, ensure_ascii=False, indent=2)
        elif uri == "nex://tunnels":
            payload = get_tunnels().summary()
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        elif uri.endswith("/guide") and uri.startswith("mcp://"):
            # Curated engine tool guide (mcp://<platform>/guide).
            plat = uri[len("mcp://"):-len("/guide")]
            try:
                from mcp_engines import platform_guide_resource
                text = platform_guide_resource(plat) or json.dumps(
                    {"error": "unknown platform", "uri": uri})
            except Exception as exc:  # noqa: BLE001
                text = json.dumps({"error": repr(exc), "uri": uri})
        elif uri.startswith("tunnel://") and uri.endswith("/info"):
            name = uri[len("tunnel://"):-len("/info")]
            for s in get_tunnels().list_upstreams():
                if s["name"] == name:
                    text = json.dumps(s, ensure_ascii=False, indent=2)
                    break
            else:
                text = json.dumps({"error": "unknown tunnel", "uri": uri})
        else:
            text = json.dumps({
                "error": "unknown resource",
                "uri": uri,
                "note": ("Nex exposes only protocol metadata over MCP. "
                         "Filesystem, log, workspace, and host-state "
                         "reads are not AI capabilities.")})
        mime = ("text/markdown" if uri.endswith("/guide")
                else "application/json")
        return {"contents": [{"uri": uri, "mimeType": mime,
                              "text": text[:1_000_000]}]}

    def _mcp_prompts_list(self) -> Dict[str, Any]:
        return {
            "prompts": [
                {"name": "tunnel_diagnose",
                 "description": "Walk the user through diagnosing why a "
                                "tunnel isn't reachable.",
                 "arguments": [{"name": "platform",
                                 "description": "Tunnel name (e.g. roblox-studio)",
                                 "required": True}]},
                {"name": "tool_call_recipe",
                 "description": "Generate a copy-paste tool call for a "
                                "specific upstream tool, with arguments.",
                 "arguments": [{"name": "tool",
                                 "description": "Fully-qualified tool name",
                                 "required": True},
                                {"name": "intent",
                                 "description": "Short English description of "
                                                "what you want the tool to do",
                                 "required": True}]},
                # --- Engine tool-explanation prompts (MCP-only). -----------
                {"name": "roblox_explain_tools",
                 "description": "Explain the Roblox Studio MCP tools — what "
                                "they do, when to use them, their parameters, "
                                "examples, and caveats. Optionally focus on "
                                "one tool.",
                 "arguments": [{"name": "tool",
                                 "description": "Optional tool name to focus "
                                                "on (e.g. execute_luau). "
                                                "Empty = all Roblox tools.",
                                 "required": False}]},
                {"name": "unreal_explain_tools",
                 "description": "Explain the Unreal Engine MCP tools — what "
                                "they do, when to use them, their parameters, "
                                "examples, and caveats. Optionally focus on "
                                "one tool.",
                 "arguments": [{"name": "tool",
                                 "description": "Optional tool name to focus "
                                                "on (e.g. spawn_actor). "
                                                "Empty = all Unreal tools.",
                                 "required": False}]},
                {"name": "roblox_build_recipe",
                 "description": "Turn a plain-English intent into a concrete "
                                "Roblox Studio MCP tool call plan.",
                 "arguments": [{"name": "intent",
                                 "description": "What you want to build/do, "
                                                "in plain English.",
                                 "required": True}]},
                {"name": "unreal_build_recipe",
                 "description": "Turn a plain-English intent into a concrete "
                                "Unreal Engine MCP tool call plan.",
                 "arguments": [{"name": "intent",
                                 "description": "What you want to build/do, "
                                                "in plain English.",
                                 "required": True}]},
            ],
        }

    def _mcp_prompts_get(self, name: str,
                          args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "tunnel_diagnose":
            plat = args.get("platform", "<unknown>")
            return {
                "messages": [{
                    "role": "user",
                    "content": ("Check the 'tunnel_status' tool, look at "
                                 "'tunnel://" + plat + "/info', and "
                                 "ask the user to enable the corresponding "
                                 "plugin in the target editor if it's "
                                 "offline. If the editor is Roblox Studio or "
                                 "Unreal Engine, read the 'mcp://" + plat
                                 + "/guide' resource for the full tool catalog "
                                 "and connection steps."),
                }],
            }
        if name == "tool_call_recipe":
            tool = args.get("tool", "")
            intent = args.get("intent", "")
            return {
                "messages": [{
                    "role": "user",
                    "content": ("Call 'read_resource' on "
                                 "'tunnel://" + tool.split(".", 1)[0]
                                 + "/info' to find the exact tool schema "
                                 "for '" + tool + "', then issue a "
                                 "tools/call with arguments shaped to "
                                 "achieve: '" + intent + "'."),
                }],
            }
        # --- Engine tool-explanation prompts (MCP-only). -------------------
        if name in ("roblox_explain_tools", "unreal_explain_tools"):
            plat = ("roblox-studio" if name == "roblox_explain_tools"
                    else "unreal-engine")
            tool = (args or {}).get("tool", "")
            try:
                from mcp_engines import explain_tool, platform_guide_resource
                if tool:
                    expl = explain_tool(plat, tool)
                    content = expl.get("markdown", "")
                else:
                    content = platform_guide_resource(plat) or ""
            except Exception as exc:  # noqa: BLE001
                content = "guide unavailable: " + repr(exc)
            return {
                "messages": [{
                    "role": "user",
                    "content": (content or "No guide available for " + plat),
                }],
            }
        if name in ("roblox_build_recipe", "unreal_build_recipe"):
            plat = ("roblox-studio" if name == "roblox_build_recipe"
                    else "unreal-engine")
            intent = (args or {}).get("intent", "")
            try:
                from mcp_engines import build_recipe_prompt
                content = build_recipe_prompt(plat, intent)
            except Exception as exc:  # noqa: BLE001
                content = "recipe helper unavailable: " + repr(exc)
            return {
                "messages": [{
                    "role": "user",
                    "content": content,
                }],
            }
        return {"messages": []}

    # ------------ MCP response helpers (session-aware) ---------------------

    def _send_mcp_session(self, sid: str) -> None:
        if sid:
            self.send_header("Mcp-Session-Id", sid)

    def _send_json_with_session(self, status: int,
                                payload: Dict[str, Any],
                                sid: Optional[str] = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if sid:
            self.send_header("Mcp-Session-Id", sid)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _jsonrpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _jsonrpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    def _handle_mcp_sse(self) -> None:
        """SSE endpoint for server->client MCP notifications.

        We piggyback on the same EventBus that drives the front-end so
        any tool that publishes a 'tool.event' or 'speak.delta' also
        gets streamed to MCP clients in JSON-RPC notification format.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = BUS.subscribe()
        # Tell the client our protocol version.
        self._sse_write_mcp({
            "jsonrpc": "2.0",
            "method": "notifications/server-info",
            "params": {"name": "nex", "version": "0.3"},
        })
        try:
            while True:
                try:
                    event = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if event.get("type") in ("tool.event", "speak.delta",
                                          "speak.end", "state"):
                    self._sse_write_mcp({
                        "jsonrpc": "2.0",
                        "method": "notifications/nex-event",
                        "params": event,
                    })
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            BUS.unsubscribe(q)

    def _sse_write_mcp(self, payload: Dict[str, Any]) -> None:
        try:
            data = json.dumps(payload)
            self.wfile.write(b"data: ")
            self.wfile.write(data.encode("utf-8"))
            self.wfile.write(b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Same-origin only: no CORS headers. Cross-origin preflights fail
        # in the browser, which is the point (see _send_json note).
        self.send_response(204)
        self.end_headers()

    # ----- SSE -------------------------------------------------------------

    def _handle_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = BUS.subscribe()
        self._sse_write({"type": "hello", "ts": time.time(),
                         "api_style": API_STYLE,
                         "model": ROUTER.role_model("planner") or OLLAMA_MODEL,
                         "providers": _model_status().get("roles", {})})

        try:
            while True:
                try:
                    event = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._sse_write(event)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            BUS.unsubscribe(q)

    def _sse_write(self, event: Dict[str, Any]) -> None:
        try:
            payload = json.dumps(event)
            self.wfile.write(b"data: ")
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.write(b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise

    # ----- chat ------------------------------------------------------------

    def _handle_agent_campaign(self) -> None:
        """Kick off a LONG-RUNNING autonomous campaign for a goal.

        The model splits the goal into milestones; each milestone runs the
        canonical pipeline (plan -> TaskGraph -> verify -> judge), and
        progress is checkpointed after every milestone so a restart RESUMES
        instead of starting over. Streams agent.campaign_started /
        agent.milestone_started / agent.milestone_completed /
        agent.campaign_done on SSE. The HTTP call returns immediately.

        Body: { "goal": "...", "max_milestones": 6, "resume": true }
        """
        body = self._read_json_body() or {}
        goal = (body.get("goal") or "").strip()
        if not goal:
            self._send_json(400, {"ok": False, "error": "missing 'goal'"})
            return
        try:
            max_milestones = int(body.get("max_milestones") or 6)
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False,
                                  "error": "'max_milestones' must be an integer"})
            return
        resume = bool(body.get("resume", True))

        def _runner() -> None:
            try:
                from agent.campaign import run_campaign
                summary = run_campaign(
                    goal, bus=BUS, llm_call=model_chat,
                    llm_reachable=model_reachable(),
                    llm_builder=builder_chat,
                    max_milestones=max_milestones, resume=resume)
                BUS.publish({"type": "agent.report", "result": {
                    "mode": "campaign", "campaign": summary,
                }, "ts": time.time()})
            except Exception as exc:  # noqa: BLE001
                BUS.publish({"type": "agent.error",
                             "source": "campaign", "error": repr(exc),
                             "ts": time.time()})

        threading.Thread(target=_runner, daemon=True).start()
        self._send_json(202, {
            "ok": True,
            "campaign": goal,
            "note": "campaign started; stream /api/events for "
                    "agent.campaign_* / agent.milestone_* events",
        })

    def _handle_agent_run(self) -> None:
        """Kick off an autonomous agent run for a goal.

        Runs in a background thread and streams semantic agent events to the
        SSE bus (so the front-end animates PLANNING/EXECUTING/… without
        knowing MCP internals). The HTTP call returns immediately.

        Body:
          { "goal": "...", "mode": "build" | "plan" }
        `mode:"plan"` produces the plan + a graph and returns it without
        executing; `mode:"build"` executes and (when a model is reachable)
        runs quality judges, optionally rebuilding once on a failing verdict.
        """
        body = self._read_json_body() or {}
        goal = (body.get("goal") or "").strip()
        mode = body.get("mode", "build")
        if not goal:
            self._send_json(400, {"ok": False, "error": "missing 'goal'"})
            return

        def _runner() -> None:
            try:
                from agent.server_run import run_agent_goal
                result = run_agent_goal(
                    goal, bus=BUS,
                    llm_call=model_chat,
                    llm_reachable=model_reachable(),
                    llm_builder=builder_chat,
                    mode=mode,
                )
                BUS.publish({"type": "agent.report", "result": result,
                             "ts": time.time()})
            except Exception as exc:  # noqa: BLE001
                BUS.publish({"type": "agent.error", "error": repr(exc),
                             "ts": time.time()})

        threading.Thread(target=_runner, daemon=True).start()
        self._send_json(200, {"ok": True, "queued": True, "goal": goal,
                              "mode": mode})

    def _handle_chat(self) -> None:
        body = self._read_json_body()
        user_text = (body.get("message") or "").strip()
        if not user_text:
            self._send_json(400, {"ok": False, "error": "empty message"})
            return

        BUS.publish({"type": "state", "state": "LISTENING", "params": {}, "ts": time.time()})

        with HISTORY_LOCK:
            HISTORY.append({"role": "user", "content": user_text})
            history_snapshot = list(HISTORY)

        # Run async — return immediately so the UI isn't blocked.
        threading.Thread(target=self._chat_thread, args=(user_text, history_snapshot), daemon=True).start()
        self._send_json(200, {"ok": True, "queued": True})

    def _chat_thread(self, user_text: str, history_snapshot: List[Dict[str, str]]) -> None:
        """Streaming chat: the model streams tokens, we buffer until a
        safe boundary, then publish a `speak.delta` event with the new
        clean text and (if a [STATE] tag was completed) a `state` event
        so the face animates in lockstep with the bubble.

        Boundaries: complete [STATE] tag, sentence terminator + whitespace,
        newline, or a 150ms idle window. The fallback engine runs only
        if the stream ends without ever producing a tag.
        """
        BUS.publish({"type": "state", "state": "THINKING", "params": {}, "ts": time.time()})

        # Mark SPEAKING as not-yet-active. We'll fire the SPEAKING state
        # the moment we have any text to display, so the face is in
        # speaking mode by the time the bubble arrives.
        _speak_active["v"] = False
        flusher = _StreamFlusher(max_idle_ms=150)
        full_clean = ""
        full_raw = ""
        seen_state = None
        had_error = False

        # Inject a fresh tunnel-awareness system message (live snapshot
        # of which MCP tunnels are reachable + their tool names). This
        # makes Nex aware of "Roblox Studio is connected and exposes
        # tools like execute_luau, run_script, …" so it can reason
        # about them naturally. Without this, the model would have
        # no idea which tunnels are actually live.
        awareness = _format_tunnel_awareness()
        if awareness:
            messages_for_model = [
                {"role": "system", "content": NEX_SYSTEM_PROMPT},
                {"role": "system", "content": awareness},
            ] + [m for m in history_snapshot if m.get("role") != "system"]
        else:
            messages_for_model = history_snapshot

        # Nudge the model to emit a plan (instead of free-form chat) when
        # the user's turn looks like substantial work. This wires up the
        # previously-dead looks_like_work_request / PLAN_HINT logic.
        messages_for_model = _mc.append_AAA_workflow(messages_for_model)

        try:
            for token in model_chat_stream(messages_for_model):
                full_raw += token
                for clean_seg, state_seg in flusher.feed(token):
                    if state_seg:
                        # The emotion rides WITH the matching delta so
                        # the client applies face + text in one frame.
                        # The previous design fired a separate "state"
                        # event, which made the emotion appear before
                        # the talking (visible as a head-start to the
                        # bubble). Bundling them eliminates that gap.
                        seen_state = state_seg
                    if clean_seg or state_seg:
                        # Publish a delta whenever there's EITHER new
                        # text OR a state to apply. A state-only delta
                        # keeps trailing tags (e.g. "Hi there. [PROUD]")
                        # from being silently absorbed until speak.end —
                        # the client would see the emotion only after
                        # the bubble was already complete, which reads
                        # as the face lagging behind the talking.
                        if clean_seg:
                            full_clean += clean_seg
                        payload = {
                            "type": "speak.delta",
                            "delta": clean_seg or "",
                            "text": full_clean,
                            "ts": time.time(),
                        }
                        if state_seg:
                            payload["state"] = state_seg
                            payload["stateSource"] = "tag"
                        BUS.publish(payload)
                # After we have something on screen, transition to SPEAKING
                # so the engine stops emitting idle behaviors underneath.
                if not _speak_active.get("v", False):
                    _speak_active["v"] = True
                    BUS.publish({"type": "state", "state": "SPEAKING",
                                 "params": {}, "ts": time.time()})
        except Exception as exc:  # noqa: BLE001
            err_text = "[" + type(exc).__name__ + "] " + str(exc)
            sys.stderr.write("model error: %s\n" % err_text)
            had_error = True
            BUS.publish({"type": "state", "state": "ERROR",
                         "params": {"reason": "model"}, "ts": time.time()})

        # Final drain — flush whatever is in the buffer (including any
        # tag we may have assembled at the very end of the stream).
        for clean_seg, state_seg in flusher.flush(force=True):
            if state_seg:
                seen_state = state_seg
            if clean_seg or state_seg:
                if clean_seg:
                    full_clean += clean_seg
                payload = {
                    "type": "speak.delta",
                    "delta": clean_seg or "",
                    "text": full_clean,
                    "ts": time.time(),
                }
                if state_seg:
                    payload["state"] = state_seg
                    payload["stateSource"] = "tag"
                BUS.publish(payload)

        # If the model failed entirely, synthesize a fallback reply and
        # send it through the flusher so the front-end sees the same
        # shape of events.
        if had_error or not full_clean:
            fallback = (
                "I can't reach the local model right now. "
                "Is the model server running on " + OLLAMA_HOST + " ?"
            )
            # If the streaming loop never ran, make sure we publish
            # SPEAKING before the first fallback delta so the face is
            # in speaking mode.
            if not _speak_active.get("v", False):
                _speak_active["v"] = True
                BUS.publish({"type": "state", "state": "SPEAKING",
                             "params": {}, "ts": time.time()})
            for clean_seg, state_seg in flusher.feed(fallback):
                if state_seg:
                    seen_state = state_seg
                if clean_seg or state_seg:
                    if clean_seg:
                        full_clean += clean_seg
                    payload = {
                        "type": "speak.delta", "delta": clean_seg or "",
                        "text": full_clean, "ts": time.time(),
                    }
                    if state_seg:
                        payload["state"] = state_seg
                        payload["stateSource"] = "tag"
                    BUS.publish(payload)
            for clean_seg, state_seg in flusher.flush(force=True):
                if clean_seg or state_seg:
                    if clean_seg:
                        full_clean += clean_seg
                    payload = {
                        "type": "speak.delta", "delta": clean_seg or "",
                        "text": full_clean, "ts": time.time(),
                    }
                    if state_seg:
                        payload["state"] = state_seg
                        payload["stateSource"] = "tag"
                    BUS.publish(payload)
            full_raw = fallback

        # Append to conversation history (raw form so the model sees its
        # own tags next time around).
        with HISTORY_LOCK:
            HISTORY.append({"role": "assistant", "content": full_raw or full_clean})

        # If the model never emitted a tag, run the rule engine.
        # We DON'T publish a separate state event — instead we annotate
        # the speak.end event so the client applies the emotion when
        # the bubble completes, keeping the emotion synchronized with
        # the talking rather than firing before/after it.
        # Trim the cumulative text once for the end-of-stream event.
        # (Deltas preserve whitespace so concatenation reads naturally,
        # but the final published text shouldn't have leading/trailing
        # space.)
        # NOTE: this MUST be computed before the fallback engine below
        # uses it — the old ordering raised UnboundLocalError and killed
        # the chat thread on every tag-less / model-less turn (no
        # speak.end, no IDLE transition, face stuck in SPEAKING).
        final_text = full_clean.strip()

        fallback_chosen = None
        if not seen_state:
            fb = fallback_state(user_text, final_text)
            if fb and fb not in ("IDLE", "SPEAKING"):
                fallback_chosen = fb

        end_payload = {
            "type": "speak.end",
            "text": final_text,
            "tts_text": final_text,
            "tags": [seen_state] if seen_state else [],
            "source": "tag" if seen_state else ("fallback" if had_error or not full_raw else "none"),
            "ts": time.time(),
        }
        if fallback_chosen:
            end_payload["state"] = fallback_chosen
            end_payload["stateSource"] = "fallback"
        BUS.publish(end_payload)

        # Estimate speech duration so the visual returns to idle.
        est = max(2.0, min(20.0, len(final_text) * 0.045))
        time.sleep(est)
        _speak_active["v"] = False
        BUS.publish({"type": "state", "state": "IDLE", "params": {}, "ts": time.time()})

        # Plan extraction. If the model emitted a JSON plan (either as
        # a fenced JSON block or as a bare {"plan": {...}} object),
        # submit it to the plan store and publish a `plan.submitted`
        # event so the UI can pick it up and prompt the user for
        # confirmation of any destructive steps. The chat text stays
        # visible; the plan lives in its own pipeline.
        try:
            plan_obj = _mc.extract_plan(full_raw or "")
            if plan_obj is not None:
                submission = _mc.submit_plan(plan_obj)
                BUS.publish({
                    "type": "plan.submitted",
                    "plan": submission,
                    "ts": time.time(),
                })
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("plan extract error: %s\n" % exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def configure_for_stdio() -> None:
    """Wire tools.py + observer.py + tunnels for a stdio subprocess.

    The HTTP `main()` calls this too; the stdio entry-point
    (`python -m nex.stdio_server`) calls this instead so the stdio
    child has the same tool surface the HTTP server would expose.
    Idempotent — repeatedly safe to call.
    """
    from tools import (configure as _tools_configure,  # type: ignore
                       TOOLS as _TOOL_LIST)
    _tools_configure(
        publisher=BUS.publish,
        parse_tags=parse_tags,
        emotion_set=EMOTION_STATES,
    )
    try:
        from tunnels import get_tunnels
        from tools import tool_definitions, call_tool  # type: ignore
        reg = get_tunnels()
        # BOUNDARY: the stdio MCP client sees MCP introspection only
        # (upstream tools arrive via their own upstreams). No sandbox/
        # filesystem/shell tools.
        def _combined_provider():
            return list(_NEX_META_TOOL_DEFS())
        # Router dispatches meta tools to their in-process handlers and
        # everything else to the regular local call_tool.
        from tunnels import get_tunnels as _gt
        _reg = _gt()

        # BOUNDARY router: introspection + namespaced upstreams only.
        def _combined_router(name, args):
            if "." in name:
                return _reg.route(name, args or {})
            return _boundary_router(name, args or {})

        reg.bind_local(
            tool_provider=_combined_provider,
            tool_router=_combined_router,
        )
    except Exception:  # noqa: BLE001
        pass


def get_stdio_registry(args) -> Dict[str, Any]:
    """Return the in-process routing callable(s) the stdio server uses.

    Returns a dict with two keys:
      * `tools_provider` — zero-arg callable that returns the tool-list
         to expose. Returns platform-only OR the full aggregate based
         on `--platform`.
      * `router(name, args)` — runs a tools/call. When `--platform`
        is set, EVERY call is forwarded to that tunnel's upstream
        (whether the tool name is prefixed or not).
    """
    from tunnels import get_tunnels
    from tools import tool_definitions, call_tool  # type: ignore

    reg = get_tunnels()

    if args and getattr(args, "platform", None):
        plat = args.platform
        stdio_target = getattr(args, "stdio_target", None)
        if stdio_target:
            # Spawn a stdio child and register it as the platform's
            # upstream. This is what we use to bind Roblox Studio's
            # stdio MCP into our registry.
            shell, args_list = _shell_split(stdio_target)
            from upstream import Upstream
            u = Upstream(plat + "-stdio", _stdio_sentinel(plat),
                         label=plat, call_timeout=60.0)
            u._stdio_command = (shell, args_list)  # type: ignore
            reg._upstreams.append(u)
            # Swap the placeholder URL with a tracker URL we recognise.
            u.url = "stdio://" + plat
        # Find the upstream that matches the platform.
        target = None
        for u in reg._upstreams:
            if u.name in (plat, plat + "-stdio"):
                target = u; break
        if target is None:
            target = reg._upstreams[0] if reg._upstreams else None

        def router(name, arguments):
            # Strip the platform prefix so the upstream sees a clean name.
            inner = name
            if isinstance(name, str) and "." in name:
                inner = name.split(".", 1)[1]
            if target is None:
                return {"isError": True,
                        "content": [{"type": "text",
                                     "text": "no upstream for " + plat}]}
            try:
                return target.call(inner, arguments or {})
            except Exception as exc:  # noqa: BLE001
                return {"isError": True,
                        "content": [{"type": "text",
                                     "text": "tunnel error: " + repr(exc)}]}

        def provider():
            if target is None:
                return []
            try:
                remote = target.tools() or []
            except Exception:  # noqa: BLE001
                remote = []
            # Surface remote tools WITHOUT the platform prefix when
            # running as a single-platform stdio proxy.
            return [{"name": t["name"],
                     "description": t.get("description", ""),
                     "inputSchema": t.get("inputSchema",
                                          {"type": "object",
                                           "properties": {}})}
                    for t in remote]

        return {"tools_provider": provider, "router": router}

    # Default: aggregate gateway.
    return {
        "tools_provider": lambda: reg.aggregated_tools(),
        "router": lambda n, a: reg.route(n, a or {}),
    }


def _shell_split(cmd_str: str) -> Tuple[str, List[str]]:
    """Split "cmd /c foo.bat" into ('cmd', ['/c', 'foo.bat'])."""
    import shlex
    parts = shlex.split(cmd_str)
    if not parts:
        return ("", [])
    return (parts[0], parts[1:])


def _stdio_sentinel(name: str) -> str:
    """A URL sentinel that the stdio client Upstream recognises."""
    return "stdio://" + name


def main() -> None:
    # --- TRUST BOOTSTRAP (operator environment, before anything else) ---
    # CONNECTED != TRUSTED. The deployed server runs in STRICT SERVER
    # MODE: an external tool call passes only if its server is in the
    # trusted registry = built-in catalog servers + NEX_TRUSTED_SERVERS
    # (operator environment — the model can never extend it).
    # NEX_STRICT_SERVERS=0 restores the legacy permissive mode.
    try:
        from mcp.policy import Policy, set_policy
        from tunnels import DEFAULT_TUNNELS as _CAT
        _trusted = {t["name"] for t in _CAT if t.get("name")}
        for _a in os.environ.get("NEX_TRUSTED_SERVERS", "").split(","):
            _a = _a.strip()
            if _a:
                _trusted.add(_a)
        _strict = os.environ.get("NEX_STRICT_SERVERS", "1").strip().lower()
        _strict_on = _strict not in ("0", "false", "no", "off")
        # CODE EXECUTION standing approval: deliberately a separate
        # operator statement. Trusting a server does not mean "and run
        # whatever code it accepts"; this env is how the operator says
        # that second thing explicitly.
        _allow_exec = set()
        for _a in os.environ.get("NEX_ALLOW_CODE_EXECUTION", "").split(","):
            _a = _a.strip()
            if _a:
                _allow_exec.add(_a)
        _allow_conf = set()
        for _a in os.environ.get("NEX_ALLOW_CONFIRMATIONS", "").split(","):
            _a = _a.strip()
            if _a:
                _allow_conf.add(_a)
        set_policy(Policy(strict_servers=_strict_on,
                          trusted_servers=_trusted,
                          allow_code_execution=_allow_exec,
                          allow_confirmations=_allow_conf))
        print("Policy: strict_servers=%s trusted=%s code_execution=%s "
              "standing_confirmations=%s"
              % (_strict_on, sorted(_trusted),
                 sorted(_allow_exec) or "none",
                 sorted(_allow_conf) or "none"))
    except Exception as exc:  # noqa: BLE001
        print("Policy: NOT configured (%s) — using core defaults" % exc)

    # Wire tools.py + observer.py to this server's EventBus, parser,
    # and emotion set. Doing it BEFORE we open the HTTP server means
    # any tool invoked via the very first request will publish to the
    # same bus SSE clients are subscribed to.
    try:
        from tools import configure as _tools_configure, TOOLS as _TOOL_LIST  # type: ignore
        _tools_configure(
            publisher=BUS.publish,
            parse_tags=parse_tags,
            emotion_set=EMOTION_STATES,
        )
        print("Tools: configured (%d tools, root=%s)"
              % (len(_TOOL_LIST), os.environ.get("NEX_TOOLS_ROOT", "~")))
    except Exception as exc:  # noqa: BLE001
        print("Tools: NOT configured (%s)" % exc)


    # Start the autonomous observer in a background thread. It watches
    # the Nex activity log and emits speak events when there's something
    # to report. Disabled by NEX_OBSERVER_DISABLED=1.
    try:
        from observer import (  # type: ignore
            start_observer, stop_observer, configure as _obs_configure)
        _obs_configure(parse_tags=parse_tags)
        start_observer(BUS.publish)
        print("Observer: enabled (cooldown=%ss, idle=%ss)"
              % (os.environ.get("NEX_OBSERVER_COOLDOWN", "12"),
                 os.environ.get("NEX_OBSERVER_IDLE", "30")))
    except Exception as exc:  # noqa: BLE001
        print("Observer: disabled (%s)" % exc)

    # Boot the MCP tunnel registry. Each child MCP server (Roblox
    # Studio, Unreal Engine 5.8, Blender, VS Code, …) is probed
    # lazily; the registry keeps their cached tool catalogs in sync
    # so /tools/list and tools/call can route across all of them in
    # one aggregated namespace.
    try:
        from tunnels import get_tunnels
        from tools import tool_definitions, call_tool  # type: ignore
        reg = get_tunnels()
        # BOUNDARY: only MCP introspection is "local" on the gateway.
        # (tools.call_tool stays available to Nex's own plumbing — it is
        # just not an AI capability.)
        reg.bind_local(
            tool_provider=_NEX_META_TOOL_DEFS,
            tool_router=_boundary_router,
        )
        summary = reg.summary()
        online = summary["online"]
        total = summary["total"]
        print("Tunnels: %d/%d online — %s"
              % (online, total, ", ".join(summary["platforms"])))
    except Exception as exc:  # noqa: BLE001
        print("Tunnels: NOT configured (%s)" % exc)

    server = ThreadingHTTPServer((HOST, PORT), NexHandler)
    print("NEX serving on http://%s:%s" % (HOST, PORT))
    print("Frontend dir: %s" % FRONTEND_DIR)
    print("Model:  %s  style=%s  model=%s" % (OLLAMA_HOST, API_STYLE, OLLAMA_MODEL))
    # The browser opens the ONE link below: it carries the token as a
    # query param exactly once, the server swaps it for an HttpOnly
    # `nex_auth` cookie and redirects to a clean URL. The token never
    # appears in served HTML/JS (no window.NEX_AUTH).
    print("Open:   http://localhost:%s/?nex_token=%s   "
          "(one click sets the auth cookie)" % (PORT, AUTH_TOKEN))
    src = ("env" if os.environ.get("NEX_AUTH_TOKEN", "").strip()
           else _token_file_path())
    print("Auth:   ALL /api + /mcp requests require the token "
          "(browser: HttpOnly `nex_auth` cookie; CLI: X-Nex-Auth header "
          "or Bearer). Source: %s" % src)
    print("Token:  %s" % AUTH_TOKEN)
    print("Tags:   " + " ".join("[" + s + "]" for s in EMOTION_STATES))
    print("MCP:    POST /mcp + GET /mcp/sse (Streamable-HTTP, "
          "2025-06-18)")
    print("MCP REST: GET /api/tools   GET /api/tunnels   "
          "POST /api/tunnels/probe")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        try:
            from observer import stop_observer  # type: ignore
            stop_observer()
        except Exception:
            pass
        server.shutdown()


if __name__ == "__main__":
    main()
