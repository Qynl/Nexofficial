"""NEX upstream MCP connection.

This module is the bridge between Nex and a child MCP server such as
the Roblox Studio plugin, Unreal Engine's Remote Control API wrapped
as MCP, the Blender addon bridge, or the VS Code Copilot gateway.

Responsibilities:

  * Probe a URL/port to see if it speaks MCP.
  * Perform the MCP `initialize` handshake (Streamable HTTP transport
    with optional `Mcp-Session-Id`).
  * Cache the discovered `tools/list` for the upstream.
  * Forward `tools/call` JSON-RPC requests to the upstream and route
    the response back to the caller.
  * Send SSE-style server-to-client notifications the upstream emits,
    merged onto Nex's own SSE stream.

This is intentionally minimal — stdlib only — so it has no side
deps. For longer-running tools we accept the upstream's own timeout
and surface the result verbatim.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


class UpstreamError(RuntimeError):
    """Anything that went wrong talking to the upstream MCP server.

    We surface this as a JSON-RPC error to the client so the LLM can
    react — e.g. "Roblox Studio is not running, ask the user to start
    the plugin before retrying".
    """


class Upstream:
    """A connection to one MCP server upstream.

    Lifetime:
        u = Upstream(name="roblox", url="http://127.0.0.1:3001/mcp")
        u.connect()      # performs initialize + notifications/initialized
        u.tools()        # returns the cached tools/list
        u.call("execute_luau", {"code": "print('hi')"})
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
        self._tools_cache_ttl_s = 30.0    # refresh every 30s
        self._server_info: Dict[str, Any] = {}
        self._last_error: Optional[str] = None
        self._consecutive_failures = 0
        # Circuit breaker — stop hammering a dead upstream so MCP
        # /tools/list stays responsive.
        self._circuit_open_until = 0.0
        # When `url` is "stdio://<something>" + `_stdio_command` is set
        # to (program, argv), this upstream speaks MCP over the
        # child's stdin/stdout instead of HTTP. See _post_stdio.
        self._stdio_command: Optional[Tuple[str, List[str]]] = None
        self._stdio_proc: Any = None
        self._stdio_lock: Any = None
        self._stdio_writer = None
        self._stdio_reader = None
        # Re-entrant lock: connect() -> _fetch_tools() -> _rpc() can
        # recurse, and concurrent tool calls must serialize on one
        # upstream so we never double-initialize or clobber state.
        self._lock = threading.RLock()
        # --- live registry stats (STAGE 3/4) ---
        self._resources_cache: List[Dict[str, Any]] = []
        self._prompts_cache: List[Dict[str, Any]] = []
        self._last_success_ts: float = 0.0
        self._last_error: Optional[str] = None
        self._latency_ms: Optional[float] = None
        self._health: str = "unknown"   # unknown | ok | degraded | down
        self._protocol_version: Optional[str] = None

    # ---------- introspection ------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Snapshot of the upstream's health. Cheap to call."""
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
                0, round(self._circuit_open_until - time.monotonic(), 2)
            ),
        }

    # ---------- low-level transport -----------------------------------------

    def _tcp_open(self, host: str, port: int) -> bool:
        """Cheap TCP probe to see if anything is listening."""
        try:
            with socket.create_connection((host, port), timeout=self.probe_timeout):
                return True
        except OSError:
            return False

    def is_reachable(self) -> bool:
        """True iff the upstream's port answers a TCP SYN right now."""
        u = urlparse(self.url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        return self._tcp_open(host, port)

    def _post(self, payload: Dict[str, Any],
              headers: Optional[Dict[str, str]] = None,
              timeout: Optional[float] = None) -> Tuple[int, Dict[str, str], str]:
        """Send an MCP POST and return (status, response_headers, body).

        Raises UpstreamError on transport errors (connection refused,
        timeout, etc.). On HTTP errors the server still returns its
        JSON-RPC envelope in the body — never re-raise.

        Transport selection:

            * url starts with ``http://`` or ``https://`` — HTTP POST.
            * url starts with ``stdio://`` — speak MCP over the
              child's stdin/stdout (set via `_stdio_command`).
        """
        if isinstance(self.url, str) and self.url.startswith("stdio://"):
            return self._post_stdio(payload, headers, timeout)
        url = self.url
        hdr = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Nex-MCP-Tunnel/1.0",
        }
        if self._session_id:
            hdr["Mcp-Session-Id"] = self._session_id
        if headers:
            hdr.update(headers)
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
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
                "connection refused or unreachable: " + str(e.reason)
            ) from e
        except (TimeoutError, OSError) as e:
            raise UpstreamError("transport error: " + repr(e)) from e

    # ------------------------------------------------------------------
    # stdio transport
    # ------------------------------------------------------------------

    def _ensure_stdio_proc(self) -> None:
        """Lazy-spawn the stdio MCP child process."""
        import subprocess
        import threading
        if self._stdio_proc is not None and self._stdio_proc.poll() is None:
            return
        if not self._stdio_command:
            raise UpstreamError(
                "stdio upstream %s has no _stdio_command set" % self.name)
        prog, argv = self._stdio_command
        try:
            self._stdio_proc = subprocess.Popen(
                [prog] + list(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise UpstreamError(
                "stdio child not found: " + str(exc))
        self._stdio_lock = threading.Lock()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._stdio_proc.stderr,),
            daemon=True,
        )
        self._stderr_thread.start()

    def _drain_stderr(self, stream) -> None:
        """Forward the child's stderr to ours so its debug never goes
        unobserved. Prefixed so users can tell which platform's
        output it is."""
        import sys
        try:
            for line in iter(stream.readline, b""):
                if not line:
                    return
                sys.stderr.write("[stdio:%s] " % self.name
                                 + line.decode("utf-8", "replace"))
                sys.stderr.flush()
        except (BrokenPipeError, OSError):
            pass

    def _post_stdio(self, payload: Dict[str, Any],
                    headers: Optional[Dict[str, str]],
                    timeout: Optional[float]) -> Tuple[int, Dict[str, str], str]:
        """Send a JSON-RPC envelope over the child's stdin and read
        the matching reply from its stdout. We use LSP-style
        Content-Length framing for compatibility with every modern
        MCP stdio client (Claude Code, Roblox Studio, VS Code, …).

        Returns a (status, headers, body) tuple that's shaped like the
        HTTP variant so the rest of the dispatcher stays identical.
        """
        from stdio_server import encode_frame, _StdioDecoder
        import os
        import time
        self._ensure_stdio_proc()
        proc = self._stdio_proc
        body = json.dumps(payload).encode("utf-8")
        frame = encode_frame(body)
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
            decoder = _StdioDecoder()
            # Cross-platform deadline-bounded reads. (select.select on a
            # pipe is POSIX-only — on Windows it raises, and the old
            # fallback treated the pipe as always-ready, so read() could
            # block forever and the timeout never fired. set_blocking is
            # the portable primitive.)
            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            try:
                try:
                    while True:
                        now = time.monotonic()
                        if now >= deadline:
                            raise UpstreamError(
                                "stdio read timeout after " + str(to) + "s")
                        try:
                            chunk = proc.stdout.read(4096)
                        except (BlockingIOError, InterruptedError):
                            time.sleep(min(0.02, max(0.0, deadline - now)))
                            continue
                        except (OSError, ValueError):
                            chunk = b""
                        if chunk is None:
                            # glibc: a non-blocking pipe read with no data
                            # yet returns None (not EAGAIN). None is NOT
                            # EOF — waiting and retrying is. (Treating it
                            # as EOF killed healthy children at startup.)
                            time.sleep(min(0.02, max(0.0, deadline - now)))
                            continue
                        if not chunk:
                            # b"" is the only real EOF on this pipe.
                            raise UpstreamError(
                                "stdio child closed before reply "
                                "(is it actually an MCP stdio server?)")
                        # feed() RETURNS the complete bodies it has parsed
                        # (and empties its buffer). The old code ignored
                        # that return value and instead re-read the now
                        # empty decoder buffer via _drain_one_body — so
                        # every stdio reply was silently dropped and every
                        # stdio call timed out. Consume the return value.
                        for body_bytes in decoder.feed(chunk):
                            try:
                                msg = json.loads(body_bytes)
                            except ValueError:
                                continue
                            # The reply is the envelope that carries OUR
                            # request id; server-to-client notifications
                            # (no matching id) are skipped, not answered.
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
        """Send a JSON-RPC NOTIFICATION (no id) and do NOT wait for a
        reply. Per the JSON-RPC/MCP spec a server must not answer
        notifications, so blocking on one is pure stall — and on stdio
        it consumed a full call_timeout on every connect()."""
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if isinstance(self.url, str) and self.url.startswith("stdio://"):
            from stdio_server import encode_frame
            self._ensure_stdio_proc()
            proc = self._stdio_proc
            frame = encode_frame(json.dumps(payload).encode("utf-8"))
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
            # A successful write to the child's stdin is all a
            # notification is — there is nothing to read back.
            return
        # HTTP: POST it; most servers answer 202/empty. Ignore the
        # reply — waiting for a JSON-RPC envelope here would be wrong.
        try:
            self._post(payload, None, timeout)
        except UpstreamError:
            pass

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
             id: Optional[int] = None) -> Dict[str, Any]:
        """Send a single JSON-RPC envelope and parse the SSE-or-JSON reply.

        MCP Streamable-HTTP servers may reply with either:
          * `application/json` — body is a single JSON-RPC envelope.
          * `text/event-stream` — body is an SSE stream of frames, the
            FIRST of which is the JSON-RPC response, the rest are
            server-to-client notifications.
        """
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
        # Surface upstream HTTP errors as JSON-RPC errors.
        if status >= 400:
            err = {
                "jsonrpc": "2.0",
                "error": {"code": -32001,
                          "message": "upstream HTTP " + str(status),
                          "data": body[:500]},
                "id": payload["id"],
            }
            self._on_failure("HTTP " + str(status))
            return err
        ctype = headers.get("Content-Type", "").lower()
        if "text/event-stream" in ctype:
            # Strip SSE framing — the response frame is the one with
            # a `data:` line carrying a JSON-RPC envelope.
            data_lines = []
            for line in body.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
            text = "\n".join(data_lines).strip()
            if not text:
                self._on_failure("empty SSE reply")
                return {"jsonrpc": "2.0",
                        "error": {"code": -32002,
                                  "message": "upstream returned empty SSE"},
                        "id": payload["id"]}
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                self._on_failure("invalid SSE JSON: " + str(exc))
                return {"jsonrpc": "2.0",
                        "error": {"code": -32002,
                                  "message": "upstream returned invalid JSON"},
                        "id": payload["id"]}
        else:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                self._on_failure("invalid JSON: " + str(exc))
                return {"jsonrpc": "2.0",
                        "error": {"code": -32002,
                                  "message": "upstream returned invalid JSON"},
                        "id": payload["id"]}
        # Cache the session id if the upstream assigned one.
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
        # Open the circuit breaker after 3 failures in a row for 10s.
        if self._consecutive_failures >= 3:
            self._circuit_open_until = time.monotonic() + 10.0

    def _circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    # ---------- handshake ----------------------------------------------------

    def connect(self) -> Dict[str, Any]:
        """Run the MCP `initialize` handshake + `notifications/initialized`.

        Returns the InitializeResult. Caches `serverInfo` and the
        session id for future calls. Idempotent: re-calling resets the
        session if the upstream has forgotten us.

        Raises UpstreamError on any failure (probe, handshake, or a
        circuit breaker that is open) so callers can report it uniformly;
        the previous "return a dict with an 'error' key" shortcut for the
        circuit breaker was inconsistent with every other failure path.
        """
        with self._lock:
            return self._connect_locked()

    def _connect_locked(self) -> Dict[str, Any]:
        # Reset any stale state FIRST so a previously-cached-but-now-dead
        # upstream is fully re-probed, and a failed connect never leaves
        # half-initialized state behind for the next caller.
        self._initialized = False
        self._server_info = {}
        self._tools_cache = []
        if self._circuit_open():
            raise UpstreamError(
                "circuit breaker open: " + (self._last_error or "upstream down"))
        # stdio children don't need a TCP probe — we'll spawn on first
        # send. is_reachable() returns False for unknown schemes, so
        # special-case the stdio URL.
        is_stdio = isinstance(self.url, str) and self.url.startswith("stdio://")
        if not is_stdio and not self.is_reachable():
            self._on_failure("TCP probe: nothing listening")
            raise UpstreamError(
                "upstream not reachable: " + self.url
                + " (is the editor plugin running?)")
        if is_stdio and not self._stdio_command:
            self._on_failure("no stdio command configured")
            raise UpstreamError(
                "stdio upstream %s has no _stdio_command set" % self.name)
        params = {
            "protocolVersion": "2025-06-18",
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": True},
                "prompts": {"listChanged": True},
            },
            "clientInfo": {
                "name": "nex",
                "version": "1.0",
            },
        }
        resp = self._rpc("initialize", params)
        if "error" in resp:
            raise UpstreamError(
                "initialize failed: " + json.dumps(resp["error"])
            )
        result = resp.get("result", {})
        self._server_info = result.get("serverInfo", {})
        self._protocol_version = result.get("protocolVersion")
        # Send notifications/initialized — required for the session to
        # begin accepting tools/call per the MCP spec. It is a true
        # NOTIFICATION (no id, no reply expected); the old code sent it
        # through _rpc with an auto-generated id and then waited a full
        # call_timeout for a reply the spec says never comes — a 60s
        # stall on every stdio handshake.
        try:
            self._notify("notifications/initialized")
        except UpstreamError:
            # Not fatal — some upstreams don't care about the notif.
            pass
        self._initialized = True
        # Refresh tools immediately so they show up on first /tools/list.
        try:
            self._tools_cache = self._fetch_tools()
            self._tools_fetched_at = time.monotonic()
        except UpstreamError:
            # Handshake succeeded but tools/list failed — stay
            # initialized and let tools() retry on demand.
            pass
        return result

    def disconnect(self) -> None:
        # Best-effort close — most upstreams don't expose DELETE.
        self._initialized = False
        self._session_id = None
        self._tools_cache = []

    # ---------- tools/list / tools/call -------------------------------------

    def _fetch_tools(self) -> List[Dict[str, Any]]:
        if not self._initialized:
            self.connect()
        resp = self._rpc("tools/list")
        if "error" in resp:
            raise UpstreamError("tools/list failed: " + json.dumps(resp["error"]))
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
                    # Keep last good cache; record the failure so the
                    # gateway can report stale but usable.
                    if not self._tools_cache:
                        self._last_error = str(exc)
                        self._health = "down"
                    raise
            return list(self._tools_cache)

    def call(self, tool_name: str,
             arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke a tool on this upstream. Returns the raw JSON-RPC reply."""
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
                # Surface a small, LLM-readable message.
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


# ----------------------------------------------------------------------------
# Built-in catalog. Each entry is the well-known endpoint for a child MCP
# server that editors ship with. Users can add more via NEX_TUNNELS env
# (comma-separated "name=url" pairs).
# ----------------------------------------------------------------------------

DEFAULT_TUNNELS: List[Dict[str, Any]] = [
    # Roblox Studio's OFFICIAL MCP server is stdio-only (per Roblox
    # docs and the user's research). The HTTP endpoints at :3001/:3002
    # are INTERNAL plugin-process long-polling, NOT the AI-client
    # transport. We register the stdio command here so users get the
    # right thing out of the box. The legacy HTTP bridge is kept as
    # `roblox-studio-legacy` for users with community bridges.
    {"name": "roblox-studio", "label": "Roblox Studio (official)",
     "transport": "stdio",
     "command": "cmd" if os.name == "nt" else
                 "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP",
     "args": ["/c", "%LOCALAPPDATA%\\Roblox\\mcp.bat"]
              if os.name == "nt" else []},
    {"name": "roblox-studio-legacy", "label": "Roblox Studio (legacy HTTP bridge)",
     "url": "http://127.0.0.1:3001/mcp"},
    # Unreal Engine 5.8 — Epic shipped an experimental MCP plugin in
    # 5.8 that exposes a Streamable HTTP endpoint on /mcp. UE 5.6 and
    # below don't have it; users can run `npx unreal-engine-mcp-server`
    # to get the stdio bridge.
    {"name": "unreal-engine", "label": "Unreal Engine 5.8",
     "url": "http://127.0.0.1:3000/mcp"},
    # Blender addon's MCP bridge exposes HTTP on :9876 by default.
    {"name": "blender-bridge", "label": "Blender addon bridge",
     "url": "http://127.0.0.1:9876/mcp"},
    # VS Code Copilot is a stdio MCP HOST — config goes in
    # `.vscode/mcp.json` under root key `servers`. There is no
    # standalone server to register here; this entry is for the
    # companion proxy that some workflows use.
    {"name": "vscode-copilot", "label": "VS Code Copilot gateway",
     "url": "http://127.0.0.1:9000/mcp"},
    # NexMinecraftMCP — Nex's OWN Minecraft mod sandbox, built in.
    # Unlike the editor tunnels above, this one is part of Nex: a
    # stdio MCP child that lets the AI create, build and test Fabric
    # mods with NO shell, NO arbitrary paths and NO command parameter —
    # only Minecraft-specific operations, all confined to
    # NEX_MINECRAFT_ROOT (default ~/NexMinecraft), Java locked to 21.
    # See minecraft_mcp.py for the four-wall security model and the
    # explicit list of what the server does NOT provide.
    {"name": "minecraft",
     "label": "Minecraft Mod Sandbox (NexMinecraftMCP, built-in)",
     "transport": "stdio",
     "command": os.path.join(
         os.path.dirname(os.path.abspath(__file__)), "nex-minecraft-mcp"),
     "args": [],
     # Gradle builds take minutes; give this tunnel its own call
     # deadline (per-tunnel timeout_s, operator config, see
     # _apply_call_timeout).
     "timeout_s": 900,
     "mcp_note": "built-in: available wherever Nex runs; sandbox root is "
                 "NEX_MINECRAFT_ROOT (default ~/NexMinecraft)"},
]


def _parse_extra_tunnels() -> List[Dict[str, Any]]:
    """Parse NEX_TUNNELS env for additional upstream URLs.

    Format: `name1=url1,name2=url2,...`
    Each name must be a short identifier; the URL must be absolute
    and start with http:// or https://. Examples:

        NEX_TUNNELS=mydev=http://127.0.0.1:4000/mcp,figma=http://127.0.0.1:5555/mcp

    For stdio upstreams, set `command=…` and `args=…` instead of `url=…`.
    """
    raw = os.environ.get("NEX_TUNNELS", "").strip()
    if not raw:
        return []
    out: List[Dict[str, Any]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, val = chunk.split("=", 1)
        name = name.strip()
        val = val.strip()
        if not name:
            continue
        if val.startswith(("http://", "https://")):
            out.append({"name": name, "label": name, "url": val})
        # Other schemes are ignored silently; users with stdio
        # upstreams can either hand-configure the registry or use
        # `reload_tunnels(extra=...)` over HTTP.
    return out


def default_registry() -> List[Upstream]:
    """All upstreams Nex knows about, in probe order.

    Connection is lazy — `is_reachable()` decides whether we
    initialize and pull tools from each. If unreachable, the upstream
    is reported as offline but the rest of the registry still works.
    """
    cfg = list(DEFAULT_TUNNELS) + _parse_extra_tunnels()
    out: List[Upstream] = []
    for c in cfg:
        url = c.get("url") or _stdio_url(c)
        u = Upstream(c["name"], url, c.get("label") or c["name"])
        _apply_call_timeout(u, c)
        if _is_stdio(c):
            u._stdio_command = (c["command"], list(c.get("args") or []))
        out.append(u)
    return out


def _apply_call_timeout(u: "Upstream", c: Dict[str, Any]) -> None:
    """Per-tunnel call timeout (`timeout_s` in the tunnel config).

    Long operations (a Minecraft Gradle build, a full test run) need
    more than the default 60 s read deadline. The value comes from
    OPERATOR configuration (built-in catalog / tunnels.json / env) —
    it is not model input. Out-of-range values are ignored, so a
    poisoned config cannot turn a tunnel into a 10-minute stall or
    a zero-deadline."""
    raw = c.get("timeout_s")
    if raw is None:
        return
    try:
        to = float(raw)
    except (TypeError, ValueError):
        return
    if 1.0 <= to <= 3600.0:
        u.call_timeout = to


def _is_stdio(c: Dict[str, Any]) -> bool:
    if c.get("transport") == "stdio":
        return True
    if (c.get("command") and not c.get("url")):
        return True
    return False


def _stdio_url(c: Dict[str, Any]) -> str:
    """URL placeholder for a stdio upstream (Upstream.__init__
    refuses `None` — synthetic URL lets it store the row)."""
    return "stdio://" + c["name"]
