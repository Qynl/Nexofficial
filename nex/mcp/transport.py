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
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
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

_MAX_FRAME_BYTES = 32 * 1024 * 1024      # 32 MiB per frame


class StdioDecoder:
    """Stateful buffer that accepts LSP and NDJSON frames and yields
    complete JSON bodies."""

    def __init__(self) -> None:
        self._buf = b""
        self._expected_len: Optional[int] = None

    def feed(self, chunk: bytes) -> List[bytes]:
        """Append bytes; return any complete bodies ready to parse."""
        self._buf += chunk
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
                if clen > _MAX_FRAME_BYTES:
                    raise ValueError(
                        "frame too large: %d bytes (max %d)"
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
                 call_timeout: float = 60.0) -> None:
        self.name = name
        self.url = url
        self.label = label or name
        self.probe_timeout = float(probe_timeout)
        self.call_timeout = float(call_timeout)
        self._session_id: Optional[str] = None
        self._initialized = False
        self._tools_cache: List[Dict[str, Any]] = []
        self._tools_fetched_at = 0.0
        self._tools_cache_ttl_s = 30.0
        self._server_info: Dict[str, Any] = {}
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
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
                0, round(self._circuit_open_until - time.monotonic(), 2)),
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
            with urllib.request.urlopen(
                req, timeout=timeout or self.call_timeout,
            ) as resp:
                return (resp.status, dict(resp.headers),
                        resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            return (e.code, dict(e.headers or {}), body)
        except urllib.error.URLError as e:
            raise UpstreamError(
                "connection refused or unreachable: " + str(e.reason)) from e
        except (TimeoutError, OSError) as e:
            raise UpstreamError("transport error: " + repr(e)) from e

    # ---------- stdio transport ---------------------------------------------

    def _ensure_stdio_proc(self) -> None:
        """Lazy-spawn the stdio MCP child process."""
        import subprocess
        if self._stdio_proc is not None and self._stdio_proc.poll() is None:
            return
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
        """Forward the child's stderr to ours so its debug output never
        goes unobserved. Prefixed with the server name."""
        import sys
        try:
            for line in iter(stream.readline, b""):
                if not line:
                    return
                sys.stderr.write("[mcp:%s] " % self.name
                                 + line.decode("utf-8", "replace"))
                sys.stderr.flush()
        except (BrokenPipeError, OSError):
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
                try:
                    proc.kill()
                except OSError:
                    pass
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
            except UpstreamError:
                # The child still holds the unanswered request; a late
                # reply would poison the NEXT call, so recycle it.
                try:
                    proc.kill()
                except OSError:
                    pass
                self._stdio_proc = None
                raise
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
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    self._stdio_proc = None
                    raise UpstreamError("stdio write failed: " + repr(exc))
            return
        try:
            self._post(payload, None, timeout)
        except UpstreamError:
            pass

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
             id: Optional[int] = None) -> Dict[str, Any]:
        """Send a single JSON-RPC envelope and parse the SSE-or-JSON reply."""
        payload = {
            "jsonrpc": "2.0",
            "id": id if id is not None else int(time.time() * 1000) % 10**9,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        try:
            status, headers, body = self._post(payload)
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
        ctype = headers.get("Content-Type", "").lower()
        if "text/event-stream" in ctype:
            data_lines = []
            for line in body.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
            text = "\n".join(data_lines).strip()
            if not text:
                self._on_failure("empty SSE reply")
                return {"jsonrpc": "2.0",
                        "error": {"code": -32002,
                                  "message": "server returned empty SSE"},
                        "id": payload["id"]}
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                self._on_failure("invalid SSE JSON: " + str(exc))
                return {"jsonrpc": "2.0",
                        "error": {"code": -32002,
                                  "message": "server returned invalid JSON"},
                        "id": payload["id"]}
        else:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                self._on_failure("invalid JSON: " + str(exc))
                return {"jsonrpc": "2.0",
                        "error": {"code": -32002,
                                  "message": "server returned invalid JSON"},
                        "id": payload["id"]}
        sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if "result" in parsed or "error" in parsed:
            self._on_success()
        return parsed

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._last_error = None

    def _on_failure(self, why: str) -> None:
        self._consecutive_failures += 1
        self._last_error = why
        if self._consecutive_failures >= 3:
            self._circuit_open_until = time.monotonic() + 10.0

    def _circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    # ---------- handshake ----------------------------------------------------

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
        self._server_info = result.get("serverInfo", {})
        self._protocol_version = result.get("protocolVersion")
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
        return result

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
                try:
                    proc.kill()
                except OSError:
                    pass

    # ---------- tools/list / tools/call --------------------------------------

    def _fetch_tools(self) -> List[Dict[str, Any]]:
        if not self._initialized:
            self.connect()
        resp = self._rpc("tools/list")
        if "error" in resp:
            raise UpstreamError(
                "tools/list failed: " + json.dumps(resp["error"]))
        return list(resp.get("result", {}).get("tools", []))

    def tools(self) -> List[Dict[str, Any]]:
        """Cached tool list, refreshed every `_tools_cache_ttl_s`."""
        with self._lock:
            if (not self._tools_cache
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
             arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke a tool on this server. Returns the raw JSON-RPC reply."""
        with self._lock:
            if not self._initialized:
                self.connect()
            t0 = time.monotonic()
            try:
                resp = self._rpc("tools/call",
                                 {"name": tool_name,
                                  "arguments": arguments or {}})
            except UpstreamError as exc:
                self._record_failure(exc)
                raise
            self._latency_ms = round((time.monotonic() - t0) * 1000, 1)
            if "error" in resp:
                err = resp["error"]
                self._last_error = err.get("message", "unknown")
                self._health = "degraded"
                raise UpstreamError(
                    "tool error: " + err.get("message", "unknown")
                    + " (code " + str(err.get("code", -1)) + ")")
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
            if not self._initialized:
                self.connect()
            try:
                resp = self._rpc("resources/list")
            except UpstreamError:
                return []
            if "error" in resp:
                return []
            return list(resp.get("result", {}).get("resources", []))

    def prompts(self) -> List[Dict[str, Any]]:
        """List MCP prompts exposed by this server (cached)."""
        with self._lock:
            if not self._initialized:
                self.connect()
            try:
                resp = self._rpc("prompts/list")
            except UpstreamError:
                return []
            if "error" in resp:
                return []
            return list(resp.get("result", {}).get("prompts", []))
