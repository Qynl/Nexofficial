#!/usr/bin/env python3
"""Nex 2.0 — the application server.

One process, stdlib only:

    Browser ──cookie-auth REST + SSE──> this file
        ├── store.py        conversations (SQLite)
        ├── agent/providers the model layer (roles, chains, pacing)
        ├── agent/loop      plan → act → observe → evaluate → adapt
        └── mcp/manager     external MCP servers = the ONLY action surface

Security posture
----------------
* Every /api request is authenticated (HttpOnly cookie, X-Nex-Auth
  header, or Bearer). The token lives in ~/.nex/server_token (0600).
* Mutating requests must carry the `X-Nex: 1` header — a cross-site
  page can submit a form POST but cannot set custom headers, so the
  cookie alone is never enough to act.
* The model's action surface is exactly mcp/manager.call → policy →
  transport. There is no shell, no filesystem tool, no bypass route.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configured as early as possible -- before any nex-internal import gets a
# chance to log anything during its own module-level setup. NEX_LOG_LEVEL
# follows the same "0/unset/invalid falls back to a sane default" contract
# as every other NEX_* knob in this codebase (see store.py's _env_int).
# Destination is stderr, matching every print()/stderr.write() this file
# used before -- operators piping/redirecting today's output see no change.
_LOG_LEVEL_NAME = (os.environ.get("NEX_LOG_LEVEL") or "INFO").strip().upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL_NAME, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
# Per-area loggers, one per bracket tag the old stderr.write() calls used
# ([http], [chat], [api]) -- enables exactly the per-area filtering the
# improvement plan called out as the reason to ever bother with this.
_log_http = logging.getLogger("server.http")
_log_chat = logging.getLogger("server.chat")
_log_api = logging.getLogger("server.api")

from agent import providers as _providers

# Load operator .env before importing modules whose defaults are evaluated at
# import time (agent budgets) or constructing stateful singletons.
_providers.load_env()

from agent import prompts as _prompts
from agent.engines import public_targets as public_engine_targets
from agent.jsonreply import extract_json_with_key
from agent.loop import RunCoordinator, budget_for_minutes
from agent.production import readiness as production_readiness
from mcp.manager import get_manager
from mcp.policy import Policy, set_policy
from store import Store

# Loopback by default: Nex is a local operator console, and binding every
# interface would publish the console to the café Wi-Fi / office LAN. An
# operator who genuinely wants remote access opts in with NEX_HOST and gets
# a warning at startup.
HOST = os.environ.get("NEX_HOST", "127.0.0.1")
PORT = int(os.environ.get("NEX_PORT", "8787"))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _nex_dir() -> str:
    base = os.environ.get("NEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".nex")
    os.makedirs(base, mode=0o700, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower().strip("[]")
    if h in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _token_file_path() -> str:
    return os.path.join(_nex_dir(), "server_token")


def _load_or_create_auth_token() -> str:
    env_token = os.environ.get("NEX_AUTH_TOKEN", "").strip()
    if env_token:
        if len(env_token) < 32:
            raise RuntimeError("NEX_AUTH_TOKEN must be at least 32 characters")
        return env_token
    path = _token_file_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        # Another process won the creation race. Read its complete token.
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
        raise RuntimeError("authentication token file is empty")
    return tok


AUTH_TOKEN = _load_or_create_auth_token()

# Failed-authentication throttle. The token is 256-bit, so this is not what
# makes brute force infeasible — it stops an unauthenticated peer from
# burning CPU and filling logs, and it bounds the damage if an operator ever
# sets a weak NEX_AUTH_TOKEN by hand.
_AUTH_FAIL_WINDOW_S = 60.0
_AUTH_FAIL_LIMIT = 20
_auth_fail_lock = threading.Lock()
_auth_fails: Dict[str, List[float]] = {}


def _auth_throttled(peer: str) -> bool:
    now = time.time()
    with _auth_fail_lock:
        hits = [t for t in _auth_fails.get(peer, ())
                if now - t < _AUTH_FAIL_WINDOW_S]
        _auth_fails[peer] = hits
        if len(_auth_fails) > 1024:          # bounded memory
            _auth_fails.clear()
        return len(hits) >= _AUTH_FAIL_LIMIT


def _note_auth_failure(peer: str) -> None:
    now = time.time()
    with _auth_fail_lock:
        _auth_fails.setdefault(peer, []).append(now)


# General API rate limit for ALREADY-AUTHENTICATED requests. The 256-bit
# token is what actually keeps strangers out; this is cheap defense-in-depth
# against OUR OWN bugs — e.g. a client-side retry loop with no backoff
# hammering /api/chat against a real, metered provider and running up cost.
# Generous on purpose: a single-operator, local-first tool should never see
# its interactive traffic anywhere near this ceiling.
_RATE_WINDOW_S = 60.0
_RATE_LIMIT = 600
_rate_lock = threading.Lock()
_rate_hits: Dict[str, List[float]] = {}


def _rate_limited(peer: str) -> bool:
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(peer, ())
                if now - t < _RATE_WINDOW_S]
        hits.append(now)
        _rate_hits[peer] = hits
        if len(_rate_hits) > 1024:          # bounded memory
            _rate_hits.clear()
        return len(hits) > _RATE_LIMIT


# ---------------------------------------------------------------------------
# Event bus (SSE fan-out)
# ---------------------------------------------------------------------------

class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: List[Any] = []
        self._last: List[Dict[str, Any]] = []

    def publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self._last.append(event)
            if len(self._last) > 200:
                self._last = self._last[-200:]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass

    def subscribe(self) -> Any:
        import queue
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: Any) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def recent(self, n: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._last)[-n:]


BUS = EventBus()

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

STORE = Store()
ROUTER = _providers.build_router(bus=BUS)
MANAGER = get_manager(bus=BUS)


def _env_set(name: str) -> set:
    return {v.strip() for v in (os.environ.get(name, "") or "").split(",")
            if v.strip()}


# Operator standing approvals are composition-root configuration, never model
# input. Server trust itself is enforced dynamically by ServerManager.
set_policy(Policy(
    allow_code_execution=_env_set("NEX_ALLOW_CODE_EXECUTION") or None,
    allow_confirmations=_env_set("NEX_ALLOW_CONFIRMATIONS") or None,
))

# ---------------------------------------------------------------------------
# Error taxonomy (the UI renders these distinctly)
# ---------------------------------------------------------------------------

ERR_USER = "user_error"
ERR_MODEL = "model_error"
ERR_MCP = "mcp_error"
ERR_NETWORK = "network_error"
ERR_CONFIG = "config_error"
ERR_INTERNAL = "internal_error"
ERR_TIMEOUT = "timeout"

_FRIENDLY = {
    ERR_USER: "That request didn't look right.",
    ERR_MODEL: "The model couldn't answer.",
    ERR_MCP: "A capability server failed.",
    ERR_NETWORK: "Network problem.",
    ERR_CONFIG: "Configuration problem.",
    ERR_INTERNAL: "Something went wrong inside Nex.",
    ERR_TIMEOUT: "That took too long.",
}


def _query_param(raw_path: str, key: str) -> str:
    """Extract one query-string parameter (percent-decoded)."""
    from urllib.parse import parse_qs, urlparse
    try:
        vals = parse_qs(urlparse(raw_path).query).get(key)
        return vals[0] if vals else ""
    except ValueError:
        return ""


def error_payload(code: str, detail: str = "") -> Dict[str, Any]:
    return {"ok": False, "error": {
        "code": code,
        "message": _FRIENDLY.get(code, "Error."),
        "detail": detail[:400],
    }}


# ---------------------------------------------------------------------------
# Chat pipeline
# ---------------------------------------------------------------------------

MAX_HISTORY_MESSAGES = 18
MAX_HISTORY_CHARS = 24 * 1024
MAX_MESSAGE_CHARS = 4000
MAX_USER_MESSAGE_CHARS = 32 * 1024
_DIRECTIVE_BUFFER_LIMIT = 4096

# one chat generation at a time per conversation
_chat_locks: Dict[str, threading.Lock] = {}
_chat_locks_guard = threading.Lock()


def _chat_lock(cid: str) -> threading.Lock:
    with _chat_locks_guard:
        if cid not in _chat_locks:
            _chat_locks[cid] = threading.Lock()
        return _chat_locks[cid]


def _capability_summary() -> List[Dict[str, Any]]:
    """Live capability surface for the chat system prompt."""
    reg = MANAGER.registry()
    servers: List[Dict[str, Any]] = []
    for s in reg.servers:
        servers.append({
            "server": s.name,
            "tools": [t.to_summary() for t in s.tools],
        })
    return servers


def _history_messages(cid: str,
                      include_last_user: bool = True) -> List[Dict[str, str]]:
    msgs = STORE.get_messages(cid)
    out: List[Dict[str, str]] = []
    for m in msgs:
        content = (m.get("content") or "")[:MAX_MESSAGE_CHARS]
        if not content.strip():
            continue
        if m.get("kind") == "run":
            content = "[agent run: %s]" % content
        out.append({"role": m["role"], "content": content})
    if not include_last_user and out and out[-1]["role"] == "user":
        out = out[:-1]
    # Keep the newest coherent turns under both a message and character cap.
    # Local context is finite too; carrying 90k chars into every routine chat
    # makes Ollama slower and leaves less room for the actual answer.
    kept: List[Dict[str, str]] = []
    total = 0
    for item in reversed(out[-MAX_HISTORY_MESSAGES:]):
        size = len(item["content"])
        if kept and total + size > MAX_HISTORY_CHARS:
            break
        kept.append(item)
        total += size
    return list(reversed(kept))


def _agent_llm() -> Optional[Callable]:
    def llm(messages: List[Dict[str, str]], purpose: str = "agent") -> str:
        # Trusted internal purpose tags select a lane; raw user text is never a
        # provider name/route override. Routine plans/summaries stay local-first,
        # while difficult planning, evaluation and diagnosis use
        # NIM -> configured GPT -> Ollama.
        role = _providers.role_for_purpose(purpose, _providers.ROLE_AGENT)
        return ROUTER.chat(role, messages, purpose=purpose)
    llm.supports_purpose = True  # type: ignore[attr-defined]
    return llm


def _on_run_summary(run: Any, text: str, report: Dict[str, Any]) -> None:
    """Persist the run's final summary as an assistant message, plus its
    compact structured project memory (agent/memory.py) for the NEXT run
    in this same conversation — never a raw history dump."""
    cid = run.conversation_id
    if not cid:
        return
    STORE.add_message(cid, "assistant", text, kind="text",
                      meta={"run_id": run.run_id,
                            "run_status": report.get("status")})
    memory = report.get("project_memory")
    if isinstance(memory, dict) and memory:
        try:
            STORE.save_project_memory(cid, memory)
        except Exception:  # noqa: BLE001 - memory is advisory, never blocking
            pass


RUNS = RunCoordinator(MANAGER, _agent_llm, bus=BUS.publish,
                          on_summary=_on_run_summary)


def _publish_chat(cid: str, event_type: str, **payload: Any) -> None:
    BUS.publish({"type": event_type, "conversation_id": cid,
                 "ts": time.time(), **payload})


def _start_run(cid: str, goal: str, say: str, **run_opts: Any) -> None:
    """The ACT path: persist the ack + a run message, then execute.

    `run_opts` (e.g. budget_s/max_steps/max_program_replans, from
    budget_for_minutes()) pass straight through to RunCoordinator.start();
    empty by default, which is exactly today's behavior.
    """
    if say:
        m = STORE.add_message(cid, "assistant", say)
        _publish_chat(cid, "chat.delta", message_id=m["id"], delta=say,
                      text=say)
        _publish_chat(cid, "chat.done", message_id=m["id"], content=say)
    try:
        targets = public_engine_targets(goal, MANAGER.registry())
    except Exception:  # noqa: BLE001 - optional display metadata
        targets = []
    compact_targets = [{
        "id": target.get("id"),
        "label": target.get("label"),
        "version": target.get("version"),
        "score": target.get("score"),
    } for target in targets]
    run_message = STORE.add_message(
        cid, "assistant", goal, kind="run", meta={"run": "starting"})
    try:
        prior_memory = STORE.get_project_memory(cid)
    except Exception:  # noqa: BLE001 - memory is advisory, never blocking
        prior_memory = None
    run_id = RUNS.start(goal, conversation_id=cid,
                        project_memory=prior_memory, **run_opts)
    STORE.update_message(run_message["id"], meta={
        "run": "starting", "run_id": run_id,
        "engine_targets": compact_targets,
        "budget_s": run_opts.get("budget_s"),
    })


def _chat_turn(cid: str, user_text: str,
               history: List[Dict[str, str]]) -> None:
    """Stream one assistant reply; detect the ACT directive."""
    system = _prompts.chat_system(_capability_summary())
    messages = [{"role": "system", "content": system}] + history
    _publish_chat(cid, "chat.generating")

    buffered = ""
    streaming = False
    message_id = "m-" + secrets.token_hex(6)

    def _emit_delta(chunk: str) -> None:
        nonlocal streaming
        if not streaming:
            streaming = True
            _publish_chat(cid, "chat.started", message_id=message_id)
        _publish_chat(cid, "chat.delta", message_id=message_id, delta=chunk,
                      text=buffered)

    try:
        for token in ROUTER.chat_stream(_providers.ROLE_CHAT, messages,
                                        purpose="chat"):
            buffered += token
            if not streaming:
                stripped = buffered.lstrip()
                if stripped.startswith("{") and \
                        len(buffered) < _DIRECTIVE_BUFFER_LIMIT:
                    continue        # possible ACT directive — keep buffering
            _emit_delta(token)
    except Exception as exc:  # noqa: BLE001
        code = ERR_MODEL
        detail = "%s: %s" % (type(exc).__name__, exc)
        if "timeout" in detail.lower():
            code = ERR_TIMEOUT
        elif "connection" in detail.lower() or "unreachable" in detail.lower():
            code = ERR_NETWORK
        _publish_chat(cid, "chat.error", code=code,
                      message=error_payload(code, detail)["error"]["message"],
                      detail=detail)
        _log_chat.warning(detail)
        return

    if not buffered.strip():
        code = ERR_MODEL
        _publish_chat(cid, "chat.error", code=code,
                      message=_FRIENDLY[ERR_MODEL],
                      detail="the model returned an empty answer")
        return

    # ACT directive?
    directive = extract_json_with_key(buffered, "act")
    if isinstance(directive, dict) and isinstance(directive.get("act"), str) \
            and directive["act"].strip():
        goal = directive["act"].strip()[:1000]
        say = str(directive.get("say") or "").strip()[:500]
        run_opts = budget_for_minutes(directive.get("minutes"))
        _start_run(cid, goal, say or "On it — %s" % goal, **run_opts)
        return

    # Plain reply.
    if not streaming:
        _emit_delta(buffered)
    STORE.add_message(cid, "assistant", buffered, kind="text",
                      meta=None)
    _publish_chat(cid, "chat.done", message_id=message_id, content=buffered)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class NexHandler(BaseHTTPRequestHandler):
    server_version = "Nex/2.0"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def end_headers(self) -> None:
        # Defense in depth for the local operator UI. No endpoint enables
        # CORS, framing, plugins, inline scripts, or cross-origin requests.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy",
                         "camera=(), geolocation=(), microphone=(self)")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "object-src 'none'; form-action 'self'; connect-src 'self'; "
            "img-src 'self' data:; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'")
        super().end_headers()

    # ----- helpers ---------------------------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: str) -> None:
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json(404, error_payload(ERR_USER, "not found"))
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            # BaseHTTPRequestHandler does not decode chunked request bodies;
            # close rather than leave bytes to be parsed as another request.
            self.close_connection = True
            return {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return {}
        if length <= 0 or length > 2 * 1024 * 1024:
            if length > 0:
                self.close_connection = True
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt: str, *args: Any) -> None:
        _log_http.info("%s %s", self.command, self.path.split("?")[0])

    # ----- auth ------------------------------------------------------------

    def _cookie_token(self) -> Optional[str]:
        header = self.headers.get("Cookie") or ""
        for part in header.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == "nex_auth":
                    return v.strip()
        return None

    def _auth_ok(self) -> bool:
        tok = self._cookie_token()
        if not tok:
            tok = (self.headers.get("X-Nex-Auth") or "").strip()
        if not tok:
            auth = (self.headers.get("Authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                tok = auth[7:].strip()
        # Query-string credentials are accepted only by the root bootstrap
        # redirect in do_GET; APIs never accept secrets in URLs.
        return bool(tok and secrets.compare_digest(tok, AUTH_TOKEN))

    def _set_auth_cookie(self) -> None:
        secure = "; Secure" if os.environ.get(
            "NEX_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes") \
            else ""
        self.send_header(
            "Set-Cookie",
            "nex_auth=%s; Path=/; HttpOnly; SameSite=Strict; "
            "Max-Age=31536000%s" % (AUTH_TOKEN, secure))

    def _mutating_origin_ok(self) -> bool:
        """CSRF defense: mutating requests must carry our custom header.
        A cross-site page can submit forms but cannot set custom headers
        on them (and we never answer CORS preflights)."""
        return (self.headers.get("X-Nex") or "").strip() == "1"

    def _peer(self) -> str:
        try:
            return str(self.client_address[0])
        except Exception:  # noqa: BLE001
            return "?"

    def _auth_gate(self, mutating: bool) -> bool:
        sensitive = self.path.startswith("/api/") or self.path == "/api"
        if not sensitive:
            return True
        peer = self._peer()
        if _auth_throttled(peer):
            self.close_connection = True
            self._send_json(429, error_payload(
                ERR_USER, "too many failed authentication attempts; wait a "
                          "minute"))
            return False
        if not self._auth_ok():
            _note_auth_failure(peer)
            self._send_json(401, error_payload(ERR_USER, "authentication "
                                                "required"))
            return False
        if _rate_limited(peer):
            self.close_connection = True
            self._send_json(429, error_payload(
                ERR_USER, "too many requests; slow down"))
            return False
        if mutating and not self._mutating_origin_ok():
            self._send_json(403, error_payload(ERR_USER,
                                               "missing X-Nex header"))
            return False
        return True

    def _handle_login(self) -> None:
        peer = self._peer()
        if _auth_throttled(peer):
            self.close_connection = True
            self._send_json(429, error_payload(
                ERR_USER, "too many failed authentication attempts; wait a "
                          "minute"))
            return
        body = self._read_json_body()
        tok = str((body or {}).get("token") or "").strip()
        if not (tok and secrets.compare_digest(tok, AUTH_TOKEN)):
            _note_auth_failure(peer)
        if tok and secrets.compare_digest(tok, AUTH_TOKEN):
            payload = b'{"ok": true}'
            self.send_response(200)
            self._set_auth_cookie()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._send_json(401, error_payload(ERR_USER, "token rejected"))

    # ----- dispatch ----------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/api"):
            if not self._auth_gate(mutating=False):
                return
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        target = None
        if path in ("/", "/index.html"):
            target = os.path.join(WEB_DIR, "index.html")
        elif path.startswith("/css/") or path.startswith("/js/"):
            rel = os.path.normpath(path.lstrip("/"))
            if rel.startswith(("css", "js")) and ".." not in rel:
                target = os.path.join(WEB_DIR, rel)
        elif path == "/favicon.svg":
            target = os.path.join(WEB_DIR, "favicon.svg")
        if not target or not os.path.isfile(target):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target)[0]
                         or "application/octet-stream")
        self.send_header("Content-Length", str(os.path.getsize(target)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        # Bootstrap links exchange the token for an HttpOnly cookie and
        # immediately remove it from the address bar/history/referrers.
        query_token = _query_param(self.path, "nex_token")
        if path in ("/", "/index.html") and query_token and \
                secrets.compare_digest(query_token, AUTH_TOKEN):
            self.send_response(303)
            self._set_auth_cookie()
            self.send_header("Location", path)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._auth_gate(mutating=False):
            return
        if path == "/api/auth/logout":
            self.send_response(200)
            self.send_header("Set-Cookie",
                             "nex_auth=; Path=/; HttpOnly; Max-Age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/", "/index.html"):
            self._send_file(os.path.join(WEB_DIR, "index.html"))
        elif path.startswith("/css/") or path.startswith("/js/"):
            rel = os.path.normpath(path.lstrip("/"))
            if not rel.startswith(("css", "js")) or ".." in rel:
                self._send_json(404, error_payload(ERR_USER, "not found"))
                return
            self._send_file(os.path.join(WEB_DIR, rel))
        elif path == "/favicon.svg":
            self._send_file(os.path.join(WEB_DIR, "favicon.svg"))
        elif path == "/api/health":
            self._send_json(200, {
                "ok": True, "version": "2.0",
                "providers": ROUTER.status().get("roles"),
                "servers": MANAGER.summary(),
                "storage": STORE.stats(),
            })
        elif path == "/api/state":
            self._send_json(200, {
                "conversations": STORE.list_conversations(),
                "servers": MANAGER.summary(),
                "production_readiness": production_readiness(
                    MANAGER.registry()),
                "provider": _provider_view(),
                "audit": MANAGER.audit.recent(20),
            })
        elif path == "/api/conversations":
            q = _query_param(self.path, "q")
            if q:
                self._send_json(200, {"conversations":
                                      STORE.search_conversations(q),
                                      "query": q})
            else:
                self._send_json(200,
                                {"conversations":
                                 STORE.list_conversations()})
        elif path.startswith("/api/conversations/"):
            self._handle_conversation_get(path)
        elif path == "/api/servers":
            self._send_json(200, MANAGER.summary())
        elif path == "/api/production/readiness":
            self._send_json(200, production_readiness(MANAGER.registry()))
        elif path.startswith("/api/servers/"):
            self._handle_server_get(path)
        elif path == "/api/audit":
            self._send_json(200, {"entries": MANAGER.audit.recent(200)})
        elif path == "/api/providers":
            self._send_json(200, _provider_view())
        elif path == "/api/events":
            self._handle_sse()
        else:
            self._send_json(404, error_payload(ERR_USER, "not found"))

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        # Login must be reachable WITHOUT a session — it is how a
        # session is established. The submitted token is itself the
        # proof of intent, so the CSRF header is not required here.
        if path == "/api/auth/session":
            self._handle_login()
            return
        if not self._auth_gate(mutating=True):
            return
        try:
            if path == "/api/conversations":
                c = STORE.create_conversation()
                self._send_json(200, {"conversation": c})
                return
            if path == "/api/chat":
                self._handle_chat()
                return
            if path.startswith("/api/conversations/") and path.endswith("/regenerate"):
                cid = path.split("/")[3]
                self._handle_regenerate(cid)
                return
            if path.startswith("/api/runs/"):
                self._handle_run_action(path)
                return
            if path == "/api/servers":
                self._handle_server_add()
                return
            if path.startswith("/api/servers/"):
                self._handle_server_action(path)
                return
            if path == "/api/providers":
                self._handle_providers_post()
                return
            if path == "/api/providers/models":
                self._handle_provider_models()
                return
            if path == "/api/providers/test":
                self._handle_provider_test()
                return
            self._send_json(404, error_payload(ERR_USER, "not found"))
        except Exception as exc:  # noqa: BLE001
            _log_api.exception("internal error on %s: %s", path, exc)
            self._send_json(500, error_payload(ERR_INTERNAL, str(exc)))

    def do_PATCH(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if not self._auth_gate(mutating=True):
            return
        if path.startswith("/api/conversations/"):
            cid = path.split("/")[3]
            body = self._read_json_body()
            if STORE.rename_conversation(cid, str(body.get("title") or "")):
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, error_payload(ERR_USER,
                                                   "no such conversation"))
            return
        self._send_json(404, error_payload(ERR_USER, "not found"))

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if not self._auth_gate(mutating=True):
            return
        if path.startswith("/api/conversations/"):
            cid = path.split("/")[3]
            if STORE.delete_conversation(cid):
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, error_payload(ERR_USER,
                                                   "no such conversation"))
            return
        if path.startswith("/api/servers/"):
            name = path.split("/")[3]
            ok, err = MANAGER.remove(name)
            if ok:
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, error_payload(ERR_MCP, err))
            return
        self._send_json(404, error_payload(ERR_USER, "not found"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        # We never answer CORS preflights — see _mutating_origin_ok.
        self._send_json(404, error_payload(ERR_USER, "no CORS"))

    # ----- GET handlers ---------------------------------------------------------

    def _handle_conversation_get(self, path: str) -> None:
        parts = path.strip("/").split("/")
        cid = parts[2] if len(parts) > 2 else ""
        if len(parts) >= 4 and parts[3] == "messages":
            conv = STORE.get_conversation(cid)
            if conv is None:
                self._send_json(404, error_payload(ERR_USER,
                                                   "no such conversation"))
                return
            self._send_json(200, {
                "conversation": conv,
                "messages": STORE.get_messages(cid),
            })
            return
        conv = STORE.get_conversation(cid)
        if conv is None:
            self._send_json(404, error_payload(ERR_USER,
                                               "no such conversation"))
            return
        self._send_json(200, {"conversation": conv})

    def _handle_server_get(self, path: str) -> None:
        parts = path.strip("/").split("/")
        name = parts[2] if len(parts) > 2 else ""
        if len(parts) >= 4 and parts[3] == "tools":
            st = MANAGER.server_status(name)
            if st.get("status") == "not configured":
                self._send_json(404, error_payload(ERR_MCP,
                                                   "no such server"))
                return
            self._send_json(200, {"server": st,
                                  "tools": MANAGER.tools(name)})
            return
        st = MANAGER.server_status(name)
        self._send_json(200, {"server": st})

    # ----- POST handlers ----------------------------------------------------------

    def _handle_chat(self) -> None:
        body = self._read_json_body()
        cid = str(body.get("conversation_id") or "").strip()
        message = str(body.get("message") or "").strip()
        if not cid or not message:
            self._send_json(400, error_payload(ERR_USER,
                                               "conversation_id and "
                                               "message are required"))
            return
        if len(message) > MAX_USER_MESSAGE_CHARS:
            self._send_json(413, error_payload(
                ERR_USER, "message exceeds %d characters"
                % MAX_USER_MESSAGE_CHARS))
            return
        if STORE.get_conversation(cid) is None:
            self._send_json(404, error_payload(ERR_USER,
                                               "no such conversation"))
            return
        lock = _chat_lock(cid)
        if not lock.acquire(blocking=False):
            self._send_json(409, error_payload(ERR_USER,
                                               "a reply is already being "
                                               "generated in this conversation"))
            return
        user_msg = STORE.add_message(cid, "user", message)
        history = _history_messages(cid, include_last_user=True)
        _publish_chat(cid, "chat.accepted", message=user_msg)

        def _worker() -> None:
            try:
                _chat_turn(cid, message, history)
            except Exception as exc:  # noqa: BLE001
                _publish_chat(cid, "chat.error", code=ERR_INTERNAL,
                              message=_FRIENDLY[ERR_INTERNAL],
                              detail=str(exc))
                _log_chat.exception("turn crashed: %s", exc)
            finally:
                lock.release()
        threading.Thread(target=_worker, daemon=True,
                          name="nex-chat-%s" % cid).start()
        self._send_json(200, {"ok": True, "message": user_msg})

    def _handle_regenerate(self, cid: str) -> None:
        conv = STORE.get_conversation(cid)
        if conv is None:
            self._send_json(404, error_payload(ERR_USER,
                                               "no such conversation"))
            return
        msgs = STORE.get_messages(cid)
        last_user = None
        for m in reversed(msgs):
            if m["role"] == "user":
                last_user = m
                break
        if last_user is None:
            self._send_json(400, error_payload(ERR_USER,
                                               "nothing to regenerate from"))
            return
        STORE.truncate_after(last_user["id"])
        history = _history_messages(cid, include_last_user=True)
        lock = _chat_lock(cid)
        if not lock.acquire(blocking=False):
            self._send_json(409, error_payload(ERR_USER, "busy"))
            return
        _publish_chat(cid, "chat.regenerating")

        def _worker() -> None:
            try:
                _chat_turn(cid, last_user["content"], history)
            except Exception as exc:  # noqa: BLE001
                _publish_chat(cid, "chat.error", code=ERR_INTERNAL,
                              message=_FRIENDLY[ERR_INTERNAL],
                              detail=str(exc))
            finally:
                lock.release()
        threading.Thread(target=_worker, daemon=True,
                          name="nex-chat-r").start()
        self._send_json(200, {"ok": True})

    def _handle_run_action(self, path: str) -> None:
        parts = path.strip("/").split("/")
        run_id = parts[2] if len(parts) > 2 else ""
        action = parts[3] if len(parts) > 3 else ""
        body = self._read_json_body()
        if action == "resolve":
            approved = bool(body.get("approved"))
            always = bool(body.get("always"))
            ok = RUNS.resolve(run_id, approved, always)
            self._send_json(200 if ok else 404,
                            {"ok": ok} if ok
                            else error_payload(ERR_USER, "no waiting run"))
            return
        if action == "cancel":
            ok = RUNS.cancel(run_id)
            self._send_json(200 if ok else 404,
                            {"ok": ok} if ok
                            else error_payload(ERR_USER, "no such run"))
            return
        if action == "revert":
            # Compensating actions. Each one re-enters the normal policy
            # path, so a destructive inverse can still demand confirmation.
            out = RUNS.revert(run_id)
            self._send_json(200 if out else 404,
                            out if out
                            else error_payload(ERR_USER, "no such run"))
            return
        self._send_json(404, error_payload(ERR_USER, "unknown run action"))

    def _handle_server_add(self) -> None:
        body = self._read_json_body()
        entry = {
            "name": str(body.get("name") or "").strip().lower(),
            "transport": str(body.get("transport") or "http"),
        }
        if entry["transport"] == "stdio":
            entry["command"] = str(body.get("command") or "").strip()
            argv = body.get("args")
            if isinstance(argv, list):
                entry["args"] = [str(a) for a in argv]
            elif isinstance(argv, str) and argv.strip():
                entry["args"] = [a for a in argv.split() if a]
        else:
            entry["url"] = str(body.get("url") or "").strip()
        if "trusted" in body:
            entry["trusted"] = bool(body.get("trusted"))
        if body.get("timeout_s"):
            entry["timeout_s"] = body.get("timeout_s")
        allow_remote = bool(body.get("confirm_remote"))
        added, err = MANAGER.add(entry, allow_remote=allow_remote)
        if added is None:
            self._send_json(400, error_payload(ERR_CONFIG, err))
            return
        st = MANAGER.server_status(added["name"])
        self._send_json(200, {"ok": True, "server": st})

    def _handle_server_action(self, path: str) -> None:
        parts = path.strip("/").split("/")
        name = parts[2] if len(parts) > 2 else ""
        action = parts[3] if len(parts) > 3 else ""
        body = self._read_json_body()
        if action == "connect":
            err = MANAGER.connect(name)
            if err:
                self._send_json(502, error_payload(ERR_MCP, err))
            else:
                self._send_json(200, {"ok": True,
                                      "server": MANAGER.server_status(name)})
            return
        if action == "disconnect":
            ok, err = MANAGER.disconnect(name)
            self._send_json(200 if ok else 404,
                            {"ok": True} if ok
                            else error_payload(ERR_MCP, err))
            return
        if action == "reconnect":
            err = MANAGER.reconnect(name)
            if err:
                self._send_json(502, error_payload(ERR_MCP, err))
            else:
                self._send_json(200, {"ok": True,
                                      "server": MANAGER.server_status(name)})
            return
        if action == "trust":
            ok, err = MANAGER.set_trusted(name, bool(body.get("trusted")))
            self._send_json(200 if ok else 404,
                            {"ok": True} if ok
                            else error_payload(ERR_MCP, err))
            return
        self._send_json(404, error_payload(ERR_USER, "unknown server action"))

    def _handle_providers_post(self) -> None:
        body = self._read_json_body()
        try:
            ROUTER.apply(body)
            view = _provider_view()
            BUS.publish({"type": "provider.status", "ts": time.time(),
                         "roles": view["roles"]})
            self._send_json(200, {"ok": True, "provider": view})
        except ValueError as exc:
            self._send_json(400, error_payload(ERR_CONFIG, str(exc)))

    def _handle_provider_models(self) -> None:
        body = self._read_json_body()
        name = str(body.get("provider") or "").strip()
        spec = ROUTER.specs.get(name)
        if spec is None:
            self._send_json(404, error_payload(ERR_CONFIG,
                                               "no such provider"))
            return
        try:
            models = ROUTER.list_models(name)
            self._send_json(200, {"models": models})
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, error_payload(ERR_NETWORK,
                                               "could not list models: %s"
                                               % exc))

    def _handle_provider_test(self) -> None:
        body = self._read_json_body()
        name = str(body.get("provider") or "").strip()
        spec = ROUTER.specs.get(name)
        if spec is None:
            self._send_json(404, error_payload(ERR_CONFIG,
                                               "no such provider"))
            return
        try:
            result = ROUTER.probe(name)
            self._send_json(200, result)
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, error_payload(ERR_NETWORK, str(exc)))

    # ----- SSE ---------------------------------------------------------------

    def _handle_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = BUS.subscribe()

        def _write(event: Dict[str, Any]) -> bool:
            try:
                payload = json.dumps(event, ensure_ascii=False,
                                     default=str)
                self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        hello = {
            "type": "hello", "ts": time.time(),
            "servers": MANAGER.summary(),
            "provider": _provider_view(),
        }
        if not _write(hello):
            BUS.unsubscribe(q)
            return
        import queue
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    if not _write({"type": "ping", "ts": time.time()}):
                        break
                    continue
                if not _write(event):
                    break
        finally:
            BUS.unsubscribe(q)


# ---------------------------------------------------------------------------
# Provider view (masked — keys never leave the server)
# ---------------------------------------------------------------------------


def _provider_view() -> Dict[str, Any]:
    try:
        st = ROUTER.status()
    except Exception:  # noqa: BLE001
        st = {}
    return {
        "providers": [
            {
                "name": name,
                "label": spec.label,
                "kind": spec.kind,
                "base_url": spec.base_url,
                "model": spec.model,
                "rpm": spec.rpm,
                "enabled": spec.enabled,
                "structured_outputs": spec.structured_outputs,
                "configured": spec.configured,
                "note": spec.note,
                "key_masked": _providers.mask_key(spec.raw_key),
                "key_host": spec.key_host,
                "key_mismatch": spec.key_mismatch,
                "state": (ROUTER.states.get(name).to_dict()
                          if name in ROUTER.states else {}),
            }
            for name, spec in ROUTER.specs.items()
        ],
        "roles": st.get("roles", {}),
        "chains": st.get("chains", {}),
        "role_models": {
            role: ROUTER.role_model(role) for role in _providers.ROLES
        },
        "catalog": list(_providers.CATALOG),
        "workload_policy": dict(_providers.WORKLOAD_POLICY),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # Connect configured MCP servers (best effort; the monitor heals later).
    try:
        MANAGER.connect_all()
        MANAGER.start_monitor()
    except Exception as exc:  # noqa: BLE001
        print("MCP manager: startup issue (%s)" % exc)

    server = ThreadingHTTPServer((HOST, PORT), NexHandler)
    print("Nex 2.0 serving on http://%s:%s" % (HOST, PORT))
    if not _is_loopback_host(HOST):
        print("WARNING: NEX_HOST=%s exposes this console beyond this machine. "
              "Anyone who can reach %s:%s can attempt to authenticate. Prefer "
              "127.0.0.1 plus an SSH tunnel." % (HOST, HOST, PORT))
    print("Open: http://localhost:%s/?nex_token=%s   (one click sets the "
          "auth cookie)" % (PORT, AUTH_TOKEN))
    print("Token: %s" % AUTH_TOKEN)
    print("Config: %s" % _nex_dir())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        MANAGER.stop_monitor()
        server.shutdown()


if __name__ == "__main__":
    main()
