"""Nex MCP transport — one connection to one external MCP server.

This module is deliberately boring: it moves JSON-RPC envelopes between
Nex and an MCP server and nothing else.

  * HTTP transport (Streamable HTTP, protocol 2025-06-18) with optional
    `Mcp-Session-Id` session handling and SSE-framed replies.
  * stdio transport — spawn a child process and speak LSP-framed MCP
    over its stdin/stdout.

Responsibilities:

  * Probe a URL/port to see if it speaks MCP.
  * Perform the MCP `initialize` handshake.
  * Cache the discovered `tools/list` (bounded TTL).
  * Forward `tools/call` requests and return the parsed reply.
  * A circuit breaker so a dead server cannot stall every request.

Security note: WHO gets an Upstream, and WHICH commands it may spawn,
is decided by the operator through `mcp/manager.py` — never by the
model. This module has no policy and no catalog; it connects to
whatever it was handed.

stdlib only.
"""
from __future__ import annotations

import json
import random
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse


class UpstreamError(RuntimeError):
    """Anything that went wrong talking to an MCP server.

    Surfaced to callers (and then to the agent) as a readable message
    so recovery code can decide: retry, adapt, switch tool, or report.
    The message is DATA — never an instruction.
    """


# ---------------------------------------------------------------------------
# stdio framing (LSP-style Content-Length, plus NDJSON tolerance on read)
# ---------------------------------------------------------------------------

_MAX_FRAME_BYTES = 8 * 1024 * 1024       # bounded untrusted MCP frame
_MAX_HTTP_BYTES = 8 * 1024 * 1024        # bounded untrusted HTTP response
_MAX_HEADER_BYTES = 16 * 1024
_MAX_SESSION_ID_CHARS = 1024
_MAX_TOOLS = 5000
_MAX_LIST_PAGES = 50
_MAX_RESOURCES = 2000
_MAX_PROMPTS = 500
_MAX_RESOURCE_CHARS = 64 * 1024          # bounded untrusted resource body

# Circuit breaker: how many CONSECUTIVE failures (no success between them)
# before a dead/hung server stops being hit for every call, and how long it
# stays shut once it trips. A flat reopen time would mean a server that has
# been down for ten minutes gets re-probed every ten seconds for the whole
# ten minutes; escalating means the probing tapers off the longer the outage
# lasts, while still trying again well within _CIRCUIT_MAX_S regardless.
_CIRCUIT_TRIP_THRESHOLD = 3
_CIRCUIT_BASE_S = 10.0
_CIRCUIT_MULTIPLIER = 1.6
_CIRCUIT_MAX_S = 120.0


def _jittered(wait: float, spread: float = 0.1) -> float:
    """Pad `wait` by 0-`spread` extra, never less — so several Upstreams that
    broke at the same moment (one gateway process restarting, one network
    blip touching several servers at once) do not all retry in the exact
    same instant.
    """
    if wait <= 0:
        return wait
    return wait + random.uniform(0.0, wait * spread)


def _origin(url: str) -> Tuple[str, str, int]:
    p = urlparse(url)
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower().rstrip(".")
    try:
        port = p.port or (443 if scheme == "https" else 80)
    except ValueError:
        port = -1
    return scheme, host, port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never let a configured MCP endpoint redirect calls/session IDs away."""
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code,
                "refused cross-origin MCP redirect to %s" % newurl,
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTP_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


def _read_bounded(stream: Any, limit: int = _MAX_HTTP_BYTES) -> bytes:
    body = stream.read(limit + 1)
    if len(body) > limit:
        raise UpstreamError("MCP response exceeds %d bytes" % limit)
    return body


def _stdio_environment() -> Dict[str, str]:
    """Minimal child environment; provider/Nex tokens are not inherited.

    A stdio server is operator-selected local code, but silently handing it
    every API key in Nex's environment is unnecessary ambient authority.
    Operators can explicitly pass required variable *names* with
    NEX_STDIO_ENV_ALLOW (for example ``GITHUB_TOKEN``).
    """
    import os
    safe = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
        "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    }
    out = {k: v for k, v in os.environ.items() if k.upper() in safe}
    allow = {n.strip() for n in os.environ.get(
        "NEX_STDIO_ENV_ALLOW", "").split(",") if n.strip()}
    # Nex's own browser auth credential can never be delegated to a child.
    allow.discard("NEX_AUTH_TOKEN")
    for name in allow:
        if name in os.environ:
            out[name] = os.environ[name]
    return out


class StdioDecoder:
    """Stateful buffer that accepts LSP and NDJSON frames and yields
    complete JSON bodies."""

    def __init__(self) -> None:
        self._buf = b""
        self._expected_len: Optional[int] = None

    def feed(self, chunk: bytes) -> List[bytes]:
        """Append bytes; return complete bodies, failing closed on overflow."""
        self._buf += chunk
        if self._expected_len is None:
            stripped = self._buf.lstrip()
            is_header = stripped.lower().startswith(b"content-length:")
            cap = _MAX_HEADER_BYTES if is_header else _MAX_FRAME_BYTES
            cr_end = self._buf.find(b"\r\n\r\n")
            lf_end = self._buf.find(b"\n\n")
            ends = [i for i in (cr_end, lf_end) if i >= 0]
            header_end = min(ends) if ends else -1
            if is_header and header_end > _MAX_HEADER_BYTES:
                raise ValueError("stdio header exceeds %d bytes"
                                 % _MAX_HEADER_BYTES)
            if len(self._buf) > cap and (not is_header or header_end < 0):
                raise ValueError("unterminated stdio frame exceeds %d bytes" % cap)
        elif len(self._buf) > _MAX_FRAME_BYTES:
            raise ValueError("stdio frame exceeds %d bytes" % _MAX_FRAME_BYTES)
        out: List[bytes] = []
        while True:
            if self._expected_len is None:
                idx_cr = self._buf.find(b"\r\n\r\n")
                idx_lf = self._buf.find(b"\n\n")
                hdr_end = -1
                if idx_cr >= 0 and (idx_lf < 0 or idx_cr < idx_lf):
                    header = self._buf[:idx_cr].decode("ascii", "replace")
                    body_offset = idx_cr + 4
                    hdr_end = idx_cr
                elif idx_lf >= 0:
                    header = self._buf[:idx_lf].decode("ascii", "replace")
                    body_offset = idx_lf + 2
                    hdr_end = idx_lf
                else:
                    # No blank line yet. This may still be the header of
                    # an LSP frame whose "\r\n\r\n" has not fully
                    # arrived — a fragmented read MUST NOT swallow the
                    # header line as if it were an NDJSON message.
                    idx_nl = self._buf.find(b"\n")
                    if idx_nl >= 0:
                        line = self._buf[:idx_nl].rstrip(b"\r")
                        s = line.strip()
                        if s.lower().startswith(b"content-length:"):
                            return out      # hold: LSP header in progress
                        if s[:1] in (b"{", b"["):
                            out.append(line)
                        # anything else (blank/garbage) is dropped
                        self._buf = self._buf[idx_nl + 1:]
                        continue
                    return out
                clen = None
                for line in header.splitlines():
                    if line.lower().startswith("content-length:"):
                        try:
                            clen = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            clen = None
                        break
                if clen is None:
                    # A blank line with no Content-Length: not an LSP
                    # frame. Only pass through JSON-looking content.
                    pre = self._buf[:hdr_end].rstrip(b"\r\n")
                    if pre.strip() and pre.lstrip()[:1] in (b"{", b"["):
                        out.append(pre)
                    self._buf = (self._buf[hdr_end + 4:]
                                 if idx_cr >= 0 else self._buf[hdr_end + 2:])
                    continue
                if clen is not None and (clen < 0 or clen > _MAX_FRAME_BYTES):
                    raise ValueError(
                        "invalid frame length: %d bytes (max %d)"
                        % (clen, _MAX_FRAME_BYTES))
                self._expected_len = clen
                self._buf = self._buf[body_offset:]
            else:
                if len(self._buf) >= self._expected_len:
                    body = self._buf[:self._expected_len]
                    out.append(body)
                    self._buf = self._buf[self._expected_len:]
                    self._expected_len = None
                else:
                    return out


def encode_frame(body: bytes) -> bytes:
    """LSP-style Content-Length frame as bytes."""
    return (b"Content-Length: %d\r\n\r\n" % len(body)) + body


class Upstream:
    """A connection to one MCP server.

    Lifetime:
        u = Upstream(name="blender", url="http://127.0.0.1:9876/mcp")
        u.connect()      # initialize + notifications/initialized
        u.tools()        # cached tools/list
        u.call("execute_code", {"code": "..."})

    For stdio servers pass ``url="stdio://<name>"`` and set
    ``stdio_command=(program, argv)`` — the child is spawned lazily and
    recycled on failure.
    """

    def __init__(self, name: str, url: str, label: str = "",
                 probe_timeout: float = 1.0,
                 call_timeout: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.url = url
        self.label = label or name
        self.probe_timeout = float(probe_timeout)
        self.call_timeout = float(call_timeout)
        # Injectable so circuit-breaker escalation can be tested without
        # real sleeps — the rest of the transport keeps real wall-clock
        # timeouts for actual I/O (those are not what is under test here).
        self._clock = clock
        self._session_id: Optional[str] = None
        self._initialized = False
        self._tools_cache: List[Dict[str, Any]] = []
        self._tools_fetched_at = 0.0
        self._tools_cache_ttl_s = 30.0
        self._server_info: Dict[str, Any] = {}
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        # How many times the breaker has TRIPPED (gone from closed to open)
        # without an intervening success — what the escalation is based on.
        self._circuit_trips = 0
        self.stdio_command: Optional[Tuple[str, List[str]]] = None
        self._stdio_proc: Any = None
        self._stdio_lock: Any = None
        self._stdio_writer = None
        self._stdio_reader = None
        # Re-entrant lock: connect() -> _fetch_tools() -> _rpc() can
        # recurse, and concurrent tool calls must serialize on one
        # upstream so we never double-initialize or clobber state.
        self._lock = threading.RLock()
        self._resources_cache: List[Dict[str, Any]] = []
        self._prompts_cache: List[Dict[str, Any]] = []
        self._last_success_ts: float = 0.0
        self._last_error: Optional[str] = None
        self._latency_ms: Optional[float] = None
        self._health: str = "unknown"   # unknown | ok | degraded | down
        self._protocol_version: Optional[str] = None

    # ---------- introspection ---------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Snapshot of the connection's health. Cheap to call."""
        return {
            "name": self.name,
            "label": self.label,
            "url": self.url,
            "initialized": self._initialized,
            "session_id": self._session_id,
            "protocol_version": self._protocol_version,
            "health": self._health,
            "latency_ms": self._latency_ms,
            "last_success_ts": self._last_success_ts,
            "tools_count": len(self._tools_cache),
            "resources_count": len(self._resources_cache),
            "prompts_count": len(self._prompts_cache),
            "server_info": self._server_info,
            "last_error": self._last_error,
            "failures": self._consecutive_failures,
            "circuit_open_seconds": max(
                0, round(self._circuit_open_until - self._clock(), 2)),
            "circuit_trips": self._circuit_trips,
        }

    # ---------- low-level transport ----------------------------------------

    def _tcp_open(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port),
                                          timeout=self.probe_timeout):
                return True
        except OSError:
            return False

    def is_reachable(self) -> bool:
        """True iff the server's port answers a TCP SYN right now."""
        u = urlparse(self.url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        return self._tcp_open(host, port)

    def _post(self, payload: Dict[str, Any],
              headers: Optional[Dict[str, str]] = None,
              timeout: Optional[float] = None) -> Tuple[int, Dict[str, str], str]:
        """Send an MCP POST and return (status, response_headers, body)."""
        if isinstance(self.url, str) and self.url.startswith("stdio://"):
            return self._post_stdio(payload, headers, timeout)
        hdr = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Nex/2.0",
        }
        if self._session_id:
            hdr["Mcp-Session-Id"] = self._session_id
        if headers:
            hdr.update(headers)
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            method="POST", headers=hdr,
        )
        try:
            with _HTTP_OPENER.open(
                req, timeout=timeout or self.call_timeout,
            ) as resp:
                raw = _read_bounded(resp)
                return (resp.status, dict(resp.headers),
                        raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                body = (_read_bounded(e).decode("utf-8", "replace")
                        if e.fp else "")
            except UpstreamError:
                body = "<oversized error response>"
            return (e.code, dict(e.headers or {}), body)
        except urllib.error.URLError as e:
            raise UpstreamError(
                "connection refused or unreachable: " + str(e.reason)) from e
        except (TimeoutError, OSError) as e:
            raise UpstreamError("transport error: " + repr(e)) from e

    # ---------- stdio transport ---------------------------------------------

    def _ensure_stdio_proc(self) -> None:
        """Lazy-spawn the stdio MCP child process."""
        import os
        import subprocess
        if self._stdio_proc is not None and self._stdio_proc.poll() is None:
            return
        if self._stdio_proc is not None:
            # The previous child exited on its own (crash, OOM-kill, the
            # user closing it, ...). `poll()` already reaped it, but its
            # stdin/stdout/stderr pipe fds are still open Python file
            # objects — close them now instead of leaving that to GC, so a
            # server that crash-loops over a long Nex session doesn't slowly
            # leak file descriptors.
            for stream in (self._stdio_proc.stdin, self._stdio_proc.stdout,
                           self._stdio_proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        if not self.stdio_command:
            raise UpstreamError(
                "stdio server %s has no command configured" % self.name)
        prog, argv = self.stdio_command
        try:
            self._stdio_proc = subprocess.Popen(
                [prog] + list(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                close_fds=True,
                env=_stdio_environment(),
                start_new_session=(os.name == "posix"),
            )
        except FileNotFoundError as exc:
            raise UpstreamError("stdio command not found: " + str(exc))
        except OSError as exc:
            raise UpstreamError("stdio spawn failed: " + repr(exc))
        self._stdio_lock = threading.Lock()
        threading.Thread(
            target=self._drain_stderr, args=(self._stdio_proc.stderr,),
            daemon=True, name="nex-stdio-%s" % self.name,
        ).start()

    def _drain_stderr(self, stream) -> None:
        """Forward bounded child diagnostics, prefixed with the server name."""
        import sys
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", "replace")
                sys.stderr.write("[mcp:%s] " % self.name + text)
                sys.stderr.flush()
        except (BrokenPipeError, OSError):
            pass

    @staticmethod
    def _terminate_stdio_proc(proc: Any) -> None:
        import os
        import signal
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - best-effort process reaping
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        for stream in (getattr(proc, "stdin", None),
                       getattr(proc, "stdout", None),
                       getattr(proc, "stderr", None)):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _post_stdio(self, payload: Dict[str, Any],
                    headers: Optional[Dict[str, str]],
                    timeout: Optional[float]) -> Tuple[int, Dict[str, str], str]:
        """Send a JSON-RPC envelope over the child's stdin and read the
        matching reply from its stdout.

        Framing: newline-delimited JSON, as the MCP stdio transport
        specifies — every real `mcp-server-*` child reads line-delimited
        messages. (The decoder accepts LSP Content-Length frames on the
        way back too, but we must not SEND them.)

        Returns a (status, headers, body) tuple shaped like the HTTP
        variant so the rest of the dispatcher stays identical.
        """
        import os
        self._ensure_stdio_proc()
        proc = self._stdio_proc
        body = json.dumps(payload).encode("utf-8")
        frame = body + b"\n"
        to = timeout or self.call_timeout
        deadline = time.monotonic() + to
        with self._stdio_lock:
            try:
                proc.stdin.write(frame)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._terminate_stdio_proc(proc)
                self._stdio_proc = None
                raise UpstreamError("stdio write failed: " + repr(exc))
            decoder = StdioDecoder()
            # Cross-platform deadline-bounded reads (select.select on a
            # pipe is POSIX-only; set_blocking is the portable primitive).
            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            try:
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        raise UpstreamError(
                            "stdio read timeout after %ss" % to)
                    try:
                        chunk = proc.stdout.read(4096)
                    except (BlockingIOError, InterruptedError):
                        time.sleep(min(0.02, max(0.0, deadline - now)))
                        continue
                    except (OSError, ValueError):
                        chunk = b""
                    if chunk is None:
                        # glibc: a non-blocking pipe read with no data
                        # yet returns None (not EAGAIN). None is NOT EOF.
                        time.sleep(min(0.02, max(0.0, deadline - now)))
                        continue
                    if not chunk:
                        # b"" is the only real EOF on this pipe.
                        raise UpstreamError(
                            "stdio server closed before replying "
                            "(is it an MCP stdio server?)")
                    for body_bytes in decoder.feed(chunk):
                        try:
                            msg = json.loads(body_bytes)
                        except ValueError:
                            continue
                        if isinstance(msg, dict) and \
                                msg.get("id") == payload.get("id"):
                            return (200, {},
                                    body_bytes.decode("utf-8", "replace"))
            except (UpstreamError, ValueError) as exc:
                # The child still holds the unanswered request; a late
                # reply would poison the NEXT call, so recycle its process
                # group. Decoder failures are transport failures too.
                self._terminate_stdio_proc(proc)
                self._stdio_proc = None
                if isinstance(exc, UpstreamError):
                    raise
                raise UpstreamError("invalid stdio frame: %s" % exc) from exc
            finally:
                try:
                    os.set_blocking(fd, True)
                except (OSError, ValueError):
                    pass

    def _notify(self, method: str,
                params: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> None:
        """Send a JSON-RPC NOTIFICATION (no id) and do not wait."""
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if isinstance(self.url, str) and self.url.startswith("stdio://"):
            self._ensure_stdio_proc()
            proc = self._stdio_proc
            # NDJSON, same as _post_stdio: a Content-Length frame here
            # would poison the child's stream (it waits for a newline
            # that never comes and every later reply is lost).
            frame = json.dumps(payload).encode("utf-8") + b"\n"
            with self._stdio_lock:
                try:
                    proc.stdin.write(frame)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    self._terminate_stdio_proc(proc)
                    self._stdio_proc = None
                    raise UpstreamError("stdio write failed: " + repr(exc))
            return
        try:
            self._post(payload, None, timeout)
        except UpstreamError:
            pass

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
             id: Optional[int] = None,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send a single JSON-RPC envelope and parse the SSE-or-JSON reply.

        This is the ONE chokepoint every call site uses (initialize, a tool
        call, resources/prompts, …), so the circuit-breaker check lives here
        rather than being repeated in each public method. Before this check
        existed here, an already-`_initialized` session kept issuing full
        network requests (and paying `call_timeout`, up to 60s by default)
        on every single tool call even while the breaker was tripped open —
        the breaker only ever protected a fresh `connect()`, never an
        ongoing, already-connected session that had started failing.

        ``timeout`` overrides `self.call_timeout` for just this one
        round-trip — real engine work (baking lighting, packaging a build,
        cooking content) can legitimately run far longer than an ordinary
        query, and a single flat timeout per server forces an operator to
        choose between a long build failing outright or a hung/broken call
        taking many minutes to be noticed.
        """
        if self._circuit_open():
            raise UpstreamError(
                "circuit breaker open: " + (self._last_error or "server down"))
        payload = {
            "jsonrpc": "2.0",
            "id": id if id is not None else int(time.time() * 1000) % 10**9,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        try:
            status, headers, body = self._post(payload, timeout=timeout)
        except UpstreamError as exc:
            self._on_failure(str(exc))
            raise
        if status >= 400:
            err = {
                "jsonrpc": "2.0",
                "error": {"code": -32001,
                          "message": "server HTTP " + str(status),
                          "data": body[:500]},
                "id": payload["id"],
            }
            self._on_failure("HTTP " + str(status))
            return err
        lower_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        ctype = lower_headers.get("content-type", "").lower()
        candidates: List[Any] = []
        if "text/event-stream" in ctype:
            # Each SSE event has its own data block.  Joining every data line
            # in the response together corrupts valid multi-event replies.
            data_lines: List[str] = []
            for line in body.splitlines() + [""]:
                if line == "":
                    if data_lines:
                        text = "\n".join(data_lines).strip()
                        try:
                            candidates.append(json.loads(text))
                        except json.JSONDecodeError:
                            pass
                        data_lines = []
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())
        else:
            try:
                candidates.append(json.loads(body))
            except json.JSONDecodeError as exc:
                self._on_failure("invalid JSON: " + str(exc))
                raise UpstreamError("server returned invalid JSON") from exc

        parsed = next((m for m in candidates
                       if isinstance(m, dict)
                       and m.get("id") == payload["id"]), None)
        if parsed is None:
            why = ("SSE contained no matching JSON-RPC response"
                   if "text/event-stream" in ctype
                   else "invalid JSON-RPC response envelope or id")
            self._on_failure(why)
            raise UpstreamError(why)
        if parsed.get("jsonrpc") not in (None, "2.0") or not \
                ("result" in parsed or "error" in parsed):
            self._on_failure("invalid JSON-RPC response envelope")
            raise UpstreamError("invalid JSON-RPC response envelope")

        sid = lower_headers.get("mcp-session-id")
        if sid:
            if len(sid) > _MAX_SESSION_ID_CHARS or "\r" in sid or "\n" in sid:
                self._on_failure("invalid MCP session id")
                raise UpstreamError("server returned an invalid MCP session id")
            self._session_id = sid
        self._on_success()
        return parsed

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._circuit_trips = 0
        self._last_error = None

    def _on_failure(self, why: str) -> None:
        self._consecutive_failures += 1
        self._last_error = why
        if self._consecutive_failures >= _CIRCUIT_TRIP_THRESHOLD:
            # Each trip past the threshold waits LONGER than the last one —
            # a server down for minutes is not re-probed every ten seconds
            # for the whole outage, but one real success resets this to the
            # base figure immediately (_on_success above).
            self._circuit_trips += 1
            wait = _jittered(min(
                _CIRCUIT_BASE_S * (_CIRCUIT_MULTIPLIER ** (self._circuit_trips - 1)),
                _CIRCUIT_MAX_S))
            self._circuit_open_until = self._clock() + wait

    def _circuit_open(self) -> bool:
        return self._clock() < self._circuit_open_until

    # ---------- handshake ----------------------------------------------------

    def _ensure_initialized(self) -> None:
        """Reconnect if never initialized, OR if a stdio child died (and was
        lazily respawned) since the last call.

        `_ensure_stdio_proc()` silently spawns a replacement process when the
        old one exited — that keeps a crashed server from staying dead
        forever, but the FRESH process has never seen `initialize`. Without
        this check, `_initialized` stays True from the OLD process and every
        caller (call/tools/resources/prompts/...) would send it a
        `tools/call` or `tools/list` before any handshake — a protocol
        violation most servers simply reject. Checking liveness here, before
        the real request goes out, means the respawn is invisible to the
        caller: one fresh `connect()` happens first, same call succeeds.
        """
        if (isinstance(self.url, str) and self.url.startswith("stdio://")
                and self._stdio_proc is not None
                and self._stdio_proc.poll() is not None):
            self._initialized = False
        if not self._initialized:
            self.connect()

    def connect(self) -> Dict[str, Any]:
        """Run the MCP `initialize` handshake + `notifications/initialized`.

        Returns the InitializeResult. Idempotent: re-calling resets the
        session if the server has forgotten us. Raises UpstreamError on
        any failure so callers can report it uniformly.
        """
        with self._lock:
            return self._connect_locked()

    def _connect_locked(self) -> Dict[str, Any]:
        self._initialized = False
        self._server_info = {}
        self._tools_cache = []
        if self._circuit_open():
            raise UpstreamError(
                "circuit breaker open: "
                + (self._last_error or "server down"))
        is_stdio = isinstance(self.url, str) and self.url.startswith("stdio://")
        if not is_stdio and not self.is_reachable():
            self._on_failure("TCP probe: nothing listening")
            raise UpstreamError(
                "server not reachable: " + self.url
                + " (is it running?)")
        if is_stdio and not self.stdio_command:
            self._on_failure("no stdio command configured")
            raise UpstreamError(
                "stdio server %s has no command configured" % self.name)
        params = {
            "protocolVersion": "2025-06-18",
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": True},
                "prompts": {"listChanged": True},
            },
            "clientInfo": {"name": "nex", "version": "2.0"},
        }
        resp = self._rpc("initialize", params)
        if "error" in resp:
            raise UpstreamError(
                "initialize failed: " + json.dumps(resp["error"]))
        result = resp.get("result", {})
        if not isinstance(result, dict):
            raise UpstreamError("initialize returned a non-object result")
        info = result.get("serverInfo", {})
        self._server_info = info if isinstance(info, dict) else {}
        version = result.get("protocolVersion")
        self._protocol_version = str(version)[:80] if version is not None else None
        try:
            self._notify("notifications/initialized")
        except UpstreamError:
            pass
        self._initialized = True
        try:
            self._tools_cache = self._fetch_tools()
            self._tools_fetched_at = time.monotonic()
        except UpstreamError:
            # Handshake succeeded but tools/list failed — stay
            # initialized and let tools() retry on demand.
            pass
        # Resources and prompts are optional MCP surfaces. They are context,
        # not actions, so discovery failures never fail the connection.
        self._resources_cache = self._safe_list("resources")
        self._prompts_cache = self._safe_list("prompts")
        return result

    def _safe_list(self, kind: str) -> List[Dict[str, Any]]:
        """Best-effort ``resources/list`` / ``prompts/list`` discovery."""
        limit = _MAX_RESOURCES if kind == "resources" else _MAX_PROMPTS
        key = kind
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen_cursors: set = set()
        for _page in range(_MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else None
            try:
                resp = self._rpc("%s/list" % kind, params)
            except UpstreamError:
                return out
            if not isinstance(resp, dict) or "error" in resp:
                return out
            result = resp.get("result")
            if not isinstance(result, dict):
                return out
            items = result.get(key)
            if not isinstance(items, list):
                return out
            for item in items:
                if not isinstance(item, dict):
                    continue
                ident = item.get("uri") if kind == "resources" \
                    else item.get("name")
                if not isinstance(ident, str) or not ident \
                        or len(ident) > 1024 \
                        or any(ord(ch) < 32 or ord(ch) == 127 for ch in ident):
                    continue
                out.append(item)
                if len(out) >= limit:
                    return out
            nxt = result.get("nextCursor")
            if not isinstance(nxt, str) or not nxt or len(nxt) > 2048 \
                    or nxt in seen_cursors:
                return out
            seen_cursors.add(nxt)
            cursor = nxt
        return out

    def disconnect(self) -> None:
        """Close the session (kills a stdio child)."""
        with self._lock:
            self._initialized = False
            self._session_id = None
            self._tools_cache = []
            proc = self._stdio_proc
            self._stdio_proc = None
            if proc is not None:
                try:
                    proc.stdin.close()
                except (OSError, AttributeError):
                    pass
                self._terminate_stdio_proc(proc)

    # ---------- tools/list / tools/call --------------------------------------

    def _fetch_tools(self) -> List[Dict[str, Any]]:
        self._ensure_initialized()
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen_cursors = set()
        seen_names = set()
        for _page in range(_MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else None
            resp = self._rpc("tools/list", params)
            if "error" in resp:
                raise UpstreamError(
                    "tools/list failed: " + json.dumps(resp["error"]))
            result = resp.get("result", {})
            if not isinstance(result, dict):
                raise UpstreamError("tools/list returned a non-object result")
            tools = result.get("tools", [])
            if not isinstance(tools, list):
                raise UpstreamError("tools/list returned a non-array tools field")
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                name = tool.get("name")
                if not isinstance(name, str) or not name or len(name) > 128 \
                        or any(ord(ch) < 33 or ord(ch) == 127 for ch in name):
                    continue
                if name in seen_names:
                    raise UpstreamError("server returned duplicate tool name %r"
                                        % name)
                schema = tool.get("inputSchema")
                output_schema = tool.get("outputSchema")
                if schema is not None and not isinstance(schema, dict):
                    continue
                if output_schema is not None and not isinstance(
                        output_schema, dict):
                    continue
                clean = dict(tool)
                clean["description"] = (tool.get("description")
                                        if isinstance(tool.get("description"), str)
                                        else "")
                clean["annotations"] = (tool.get("annotations")
                                        if isinstance(tool.get("annotations"), dict)
                                        else {})
                clean["inputSchema"] = schema or {}
                if output_schema is not None:
                    clean["outputSchema"] = output_schema
                out.append(clean)
                seen_names.add(name)
                if len(out) > _MAX_TOOLS:
                    raise UpstreamError("server exposes more than %d tools"
                                        % _MAX_TOOLS)
            nxt = result.get("nextCursor")
            if not isinstance(nxt, str) or not nxt:
                return out
            if len(nxt) > 2048 or nxt in seen_cursors:
                raise UpstreamError("invalid/repeated tools/list cursor")
            seen_cursors.add(nxt)
            cursor = nxt
        raise UpstreamError("tools/list exceeded %d pagination pages"
                            % _MAX_LIST_PAGES)

    def tools(self, force: bool = False) -> List[Dict[str, Any]]:
        """Cached tool list, refreshed every `_tools_cache_ttl_s`.

        ``force=True`` bypasses the TTL and always does a real round-trip.
        The health monitor relies on this: its probe interval (20s default)
        is shorter than the tools cache TTL (30s), so an un-forced refresh
        would often return stale cached data and silently skip checking
        whether the server is actually still alive.
        """
        with self._lock:
            if (force or not self._tools_cache
                    or time.monotonic() - self._tools_fetched_at
                    > self._tools_cache_ttl_s):
                try:
                    self._tools_cache = self._fetch_tools()
                    self._tools_fetched_at = time.monotonic()
                    self._record_success()
                except UpstreamError as exc:
                    if not self._tools_cache:
                        self._last_error = str(exc)
                        self._health = "down"
                    raise
            return list(self._tools_cache)

    def call(self, tool_name: str,
             arguments: Dict[str, Any],
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """Invoke a tool on this server. Returns the raw JSON-RPC reply.

        ``timeout`` overrides the server's configured `call_timeout` for
        this one call only (e.g. a known-slow BUILD-category operation);
        it never changes the timeout any other call on this connection
        uses.
        """
        with self._lock:
            self._ensure_initialized()
            t0 = time.monotonic()
            try:
                resp = self._rpc("tools/call",
                                 {"name": tool_name,
                                  "arguments": arguments or {}},
                                 timeout=timeout)
            except UpstreamError as exc:
                self._record_failure(exc)
                raise
            self._latency_ms = round((time.monotonic() - t0) * 1000, 1)
            if "error" in resp:
                err = resp["error"]
                err = err if isinstance(err, dict) else {"message": str(err)}
                message = str(err.get("message", "unknown"))[:1000]
                self._last_error = message
                self._health = "degraded"
                raise UpstreamError(
                    "tool error: " + message
                    + " (code " + str(err.get("code", -1)) + ")")
            result = resp.get("result")
            # MCP tool-level failures use result.isError rather than the
            # JSON-RPC error member.  Treating these as success would make
            # the agent falsely report failed work as completed.
            if isinstance(result, dict) and result.get("isError") is True:
                detail = "MCP tool reported an error"
                content = result.get("content")
                if isinstance(content, list):
                    texts = [str(x.get("text", "")) for x in content
                             if isinstance(x, dict) and x.get("text")]
                    if texts:
                        detail += ": " + " ".join(texts)[:900]
                self._last_error = detail
                self._health = "degraded"
                raise UpstreamError(detail)
            self._record_success()
            return resp

    def _record_success(self) -> None:
        self._last_success_ts = time.time()
        self._last_error = None
        if self._health in ("down", "unknown"):
            self._health = "ok"

    def _record_failure(self, exc: Exception) -> None:
        self._last_error = (type(exc).__name__ + ": " + str(exc))[:300]
        self._health = "down"

    def resources(self) -> List[Dict[str, Any]]:
        """List MCP resources exposed by this server (cached)."""
        with self._lock:
            self._ensure_initialized()
            if not self._resources_cache:
                self._resources_cache = self._safe_list("resources")
            return list(self._resources_cache)

    def prompts(self) -> List[Dict[str, Any]]:
        """List MCP prompts exposed by this server (cached)."""
        with self._lock:
            self._ensure_initialized()
            if not self._prompts_cache:
                self._prompts_cache = self._safe_list("prompts")
            return list(self._prompts_cache)

    def read_resource(self, uri: str) -> List[Dict[str, Any]]:
        """Read one MCP resource. Returns bounded content entries.

        Resource bodies are untrusted project data, never instructions, so
        text is clipped and binary blobs are described rather than carried.
        """
        if not isinstance(uri, str) or not uri or len(uri) > 1024:
            raise UpstreamError("invalid resource uri")
        with self._lock:
            self._ensure_initialized()
            resp = self._rpc("resources/read", {"uri": uri})
            if "error" in resp:
                raise UpstreamError(
                    "resources/read failed: " + json.dumps(resp["error"]))
            result = resp.get("result")
            if not isinstance(result, dict):
                raise UpstreamError("resources/read returned a non-object")
            contents = result.get("contents")
            if not isinstance(contents, list):
                raise UpstreamError("resources/read returned no contents")
            out: List[Dict[str, Any]] = []
            budget = _MAX_RESOURCE_CHARS
            for item in contents[:64]:
                if not isinstance(item, dict):
                    continue
                entry = {
                    "uri": str(item.get("uri") or uri)[:1024],
                    "mimeType": str(item.get("mimeType") or "")[:120],
                }
                text = item.get("text")
                if isinstance(text, str):
                    entry["text"] = text[:max(0, budget)]
                    budget -= len(entry["text"])
                elif isinstance(item.get("blob"), str):
                    entry["binary_bytes"] = len(item["blob"])
                out.append(entry)
                if budget <= 0:
                    break
            return out

    def get_prompt(self, name: str,
                   arguments: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
        """Fetch one server-authored MCP prompt (untrusted guidance text)."""
        if not isinstance(name, str) or not name or len(name) > 256:
            raise UpstreamError("invalid prompt name")
        with self._lock:
            self._ensure_initialized()
            resp = self._rpc("prompts/get",
                             {"name": name, "arguments": arguments or {}})
            if "error" in resp:
                raise UpstreamError(
                    "prompts/get failed: " + json.dumps(resp["error"]))
            result = resp.get("result")
            if not isinstance(result, dict):
                raise UpstreamError("prompts/get returned a non-object")
            return result
