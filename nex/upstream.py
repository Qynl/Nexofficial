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

    # ---------- introspection ------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Snapshot of the upstream's health. Cheap to call."""
        return {
            "name": self.name,
            "label": self.label,
            "url": self.url,
            "initialized": self._initialized,
            "session_id": self._session_id,
            "tools_count": len(self._tools_cache),
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
        import select
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
            try:
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        raise UpstreamError(
                            "stdio read timeout after " + str(to) + "s")
                    # Non-blocking read with timeout.
                    try:
                        r, _, _ = select.select(
                            [proc.stdout], [], [],
                            min(0.5, max(0.0, deadline - now)))
                    except (OSError, ValueError):
                        r = [proc.stdout]
                    if not r:
                        continue
                    try:
                        chunk = proc.stdout.read(4096)
                    except (OSError, ValueError):
                        chunk = b""
                    if not chunk:
                        raise UpstreamError(
                            "stdio child closed before reply "
                            "(is it actually an MCP stdio server?)")
                    decoder.feed(chunk)
                    body_bytes = self._drain_one_body(decoder)
                    if body_bytes is not None:
                        return (200, {}, body_bytes.decode("utf-8", "replace"))
            except UpstreamError:
                raise

    def _drain_one_body(self, decoder):
        """Walk `decoder` to extract exactly one complete body, if any."""
        buf = decoder._buf
        if not buf:
            return None
        # LSP style: \r\n\r\n header, then body.
        idx = buf.find(b"\r\n\r\n")
        delim = b"\r\n\r\n"
        if idx < 0:
            idx = buf.find(b"\n\n")
            delim = b"\n\n"
        if idx >= 0:
            hdr = buf[:idx].decode("ascii", "replace")
            body_off = idx + len(delim)
            clen = None
            for line in hdr.splitlines():
                if line.lower().startswith("content-length:"):
                    try:
                        clen = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                    break
            if clen is not None and len(buf) >= body_off + clen:
                body = bytes(buf[body_off:body_off + clen])
                decoder._buf = bytes(buf[body_off + clen:])
                return body
            if clen is None:
                msg = buf[:idx]
                decoder._buf = bytes(buf[body_off:])
                return msg
        # NDJSON fallback — one message per line.
        nl = buf.find(b"\n")
        if nl >= 0:
            line = buf[:nl].rstrip(b"\r")
            decoder._buf = bytes(buf[nl + 1:])
            return line
        return None

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
        """
        if self._circuit_open():
            return {
                "error": "circuit breaker open: " +
                (self._last_error or "upstream down"),
            }
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
        # Send notifications/initialized — required for the session to
        # begin accepting tools/call per the MCP spec.
        try:
            self._rpc("notifications/initialized", id=None)
        except UpstreamError:
            # Not fatal — some upstreams don't care about the notif.
            pass
        self._initialized = True
        # Refresh tools immediately so they show up on first /tools/list.
        try:
            self._tools_cache = self._fetch_tools()
            self._tools_fetched_at = time.monotonic()
        except UpstreamError:
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
        if (not self._tools_cache
                or time.monotonic() - self._tools_fetched_at
                > self._tools_cache_ttl_s):
            try:
                self._tools_cache = self._fetch_tools()
                self._tools_fetched_at = time.monotonic()
            except UpstreamError as exc:
                # Keep last good cache; record the failure so the
                # gateway can report stale but usable.
                if not self._tools_cache:
                    self._last_error = str(exc)
                raise
        return list(self._tools_cache)

    def call(self, tool_name: str,
             arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke a tool on this upstream. Returns the raw JSON-RPC reply."""
        if not self._initialized:
            self.connect()
        resp = self._rpc("tools/call",
                         {"name": tool_name, "arguments": arguments or {}})
        if "error" in resp:
            # Surface a small, LLM-readable message.
            err = resp["error"]
            raise UpstreamError(
                "tool error: " + err.get("message", "unknown")
                + " (code " + str(err.get("code", -1)) + ")")
        return resp


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
        if _is_stdio(c):
            u._stdio_command = (c["command"], list(c.get("args") or []))
        out.append(u)
    return out


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
