"""NEX stdio MCP server.

Runs Nex as a stdio MCP server so any MCP client (Claude Desktop,
Claude Code, Cursor, VS Code Copilot, Codex CLI, Windsurf, Zed, …
and Roblox Studio's own `mcp.bat` UX) can configure us with
`{"command": "python", "args": ["-m", "nex.stdio_server", ...]}`.

Framing
-------
The official MCP stdio transport (modelcontextprotocol.io) uses
LSP-style `Content-Length: N\\r\\n\\r\\n<body>` framing. Many open-source
implementations also accept newline-delimited JSON. We accept BOTH
on read and emit BOTH on write so we interop with the widest set of
clients.

Modus operandi
--------------
* Default — aggregates LOCAL tools + every registered tunnel's
  namespaced upstream tools, serving them under ONE MCP server.
  One process, one client, everything visible.
* `--platform=<name>` — proxies ALL MCP traffic to that one tunnel.
  Used by clients that want the Roblox-only / Unreal-only experience.
* `--stdio-target=<command>` — when paired with `--platform`, spawns a
  child process that speaks stdio MCP and bridges it as a tunnel.
  E.g. for the built-in Roblox Studio MCP that lives on stdio only:
    python -m nex.stdio_server --platform=roblox \
        --stdio-target="cmd /c %LOCALAPPDATA%\\Roblox\\mcp.bat"
* `--bind-http=<port>` — also exposes the same MCP surface over HTTP
  on the chosen port (in addition to stdio). Lets curl / debuggers
  poke the same server.

Logging
-------
All debug messages go to stderr. Anything written to stdout is a JSON
message — Claude Code's transport will choke if we pollute it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

# Per the MCP 2024-11-05 / 2025-06-18 spec. We accept:
#   - LSP style "Content-Length: NNN\r\n\r\n<body>"
#   - LSP style with bare-LF newlines (some clients omit \r)
#   - Plain NDJSON line (one message per line, no embedded \n)
#
# We emit LSP style in the canonical form (Content-Length, \r\n, body
# of exactly N bytes). NDJSON-friendly clients parse either format.

_MAX_FRAME_BYTES = 32 * 1024 * 1024      # 32 MiB per frame


class _StdioDecoder:
    """Stateful buffer that accepts both LSP+NDJSON frames and yields
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
                # Look for either format header boundary.
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
                    # Maybe pure newline-delimited JSON.
                    idx_nl = self._buf.find(b"\n")
                    if idx_nl >= 0:
                        line = self._buf[:idx_nl].rstrip(b"\r")
                        if line.strip():
                            out.append(line)
                        self._buf = self._buf[idx_nl + 1:]
                        continue
                    return out
                # Parse Content-Length if present.
                clen = None
                for line in header.splitlines():
                    if line.lower().startswith("content-length:"):
                        try:
                            clen = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            clen = None
                        break
                if clen is None:
                    # No Content-Length — fall back to NDJSON for the
                    # rest of the frame, treating the bytes BEFORE the
                    # \r\n\r\n as a complete message.
                    pre = self._buf[:hdr_end].rstrip(b"\r\n")
                    if pre.strip():
                        out.append(pre)
                    self._buf = self._buf[hdr_end + 4:] \
                        if idx_cr >= 0 else self._buf[hdr_end + 2:]
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
        # unreachable


def encode_frame(body: bytes) -> bytes:
    """LSP-style Content-Length frame as bytes."""
    return (b"Content-Length: %d\r\n\r\n" % len(body)) + body


# ---------------------------------------------------------------------------
# Stdio server skeleton — minimal JSON-RPC 2.0 + MCP shape, all in stdlib
# (the real engine is the registry + tools module loaded by the main).
# ---------------------------------------------------------------------------

class _StdioServer:
    """Drives one stdio MCP session: read frames, dispatch, write frames."""

    def __init__(self, *, registry_provider,
                 local_router,
                 local_provider,
                 server_info: Dict[str, Any]) -> None:
        self._registry_provider = registry_provider
        self._local_router = local_router
        self._local_provider = local_provider
        self._server_info = server_info
        self._stop = threading.Event()

    def run(self) -> None:
        """Block reading stdin / writing stdout until EOF or stop()."""
        # Re-bind stdio to binary so byte-counts work; restore on exit.
        stdin = os.fdopen(0, "rb", buffering=0)
        stdout = os.fdopen(1, "wb", buffering=0)
        decoder = _StdioDecoder()
        self._log("stdio server up: %s" % self._server_info.get("name"))
        try:
            while not self._stop.is_set():
                chunk = stdin.read(4096)
                if not chunk:
                    self._log("EOF on stdin — exiting stdio loop")
                    return
                bodies = []
                try:
                    bodies = decoder.feed(chunk)
                except ValueError as exc:
                    self._jsonrpc_error(None, -32700,
                                        "parse error: " + str(exc))
                    self._flush(stdout)
                    continue
                for body in bodies:
                    self._handle(body)
                    self._flush(stdout)
        except (BrokenPipeError, OSError) as exc:
            self._log("stdio loop closed: " + str(exc))
            return

    def stop(self) -> None:
        self._stop.set()

    # ---------- request handler -------------------------------------------

    def _handle(self, body: bytes) -> None:
        try:
            req = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            self._jsonrpc_error(None, -32700, "invalid JSON: " + str(exc))
            return
        method = req.get("method")
        req_id = req.get("id")  # notifications have None
        params = req.get("params") or {}
        if method == "initialize":
            self._initialize(req_id, params)
        elif method == "notifications/initialized":
            # No reply.
            pass
        elif method == "ping":
            self._jsonrpc_result(req_id, {})
        elif method == "tools/list":
            self._tools_list(req_id)
        elif method == "tools/call":
            self._tools_call(req_id, params)
        elif method == "resources/list":
            self._resources_list(req_id)
        elif method == "resources/read":
            self._resources_read(req_id, params)
        elif method == "prompts/list":
            self._prompts_list(req_id)
        elif method == "prompts/get":
            self._prompts_get(req_id, params)
        else:
            if req_id is not None:
                self._jsonrpc_error(req_id, -32601,
                                    "method not found: " + str(method))

    # ---------- JSON-RPC helpers -----------------------------------------

    def _initialize(self, req_id: Any, params: Dict[str, Any]) -> None:
        # Honor the spec's flexibility — we answer with whichever version
        # the client asked for, falling back to 2025-06-18.
        requested = params.get("protocolVersion") or "2025-06-18"
        if requested not in ("2024-11-05", "2025-03-26",
                             "2025-06-18", "2025-11-25"):
            requested = "2025-06-18"
        result = {
            "protocolVersion": requested,
            "serverInfo": self._server_info,
            "capabilities": {
                "tools":     {"listChanged": True},
                "resources": {"subscribe": True},
                "prompts":   {"listChanged": False},
            },
        }
        self._jsonrpc_result(req_id, result)

    def _tools_list(self, req_id: Any) -> None:
        try:
            tools = self._registry_provider()
        except Exception as exc:  # noqa: BLE001
            self._jsonrpc_error(req_id, -32603,
                                "tools/list failed: " + repr(exc))
            return
        self._jsonrpc_result(req_id, {"tools": tools})

    def _tools_call(self, req_id: Any, params: Dict[str, Any]) -> None:
        name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        if not name:
            self._jsonrpc_error(req_id, -32602,
                                "missing 'name' in params")
            return
        try:
            env = self._local_router(name, arguments)
        except Exception as exc:  # noqa: BLE001
            self._jsonrpc_error(req_id, -32603,
                                "tool failed: " + repr(exc))
            return
        self._jsonrpc_result(req_id, env)

    def _resources_list(self, req_id: Any) -> None:
        resources = [
            {"uri": "nex://log/recent", "name": "Recent Nex activity",
             "mimeType": "application/json"},
            {"uri": "nex://tunnels", "name": "Tunnel status",
             "mimeType": "application/json"},
        ]
        self._jsonrpc_result(req_id, {"resources": resources})

    def _resources_read(self, req_id: Any, params: Dict[str, Any]) -> None:
        uri = params.get("uri") or ""
        if uri == "nex://tunnels":
            summary = {"placeholder": "see /api/tunnels"}
            text = json.dumps(summary)
        else:
            text = json.dumps({"error": "unknown resource", "uri": uri})
        self._jsonrpc_result(req_id, {
            "contents": [{"uri": uri,
                          "mimeType": "application/json",
                          "text": text}]
        })

    def _prompts_list(self, req_id: Any) -> None:
        self._jsonrpc_result(req_id, {"prompts": []})

    def _prompts_get(self, req_id: Any, params: Dict[str, Any]) -> None:
        self._jsonrpc_result(req_id, {"messages": []})

    def _jsonrpc_result(self, req_id: Any, result: Any) -> None:
        msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
        self._write(json.dumps(msg).encode("utf-8"))

    def _jsonrpc_error(self, req_id: Any, code: int, message: str) -> None:
        msg = {"jsonrpc": "2.0", "id": req_id,
               "error": {"code": code, "message": message}}
        self._write(json.dumps(msg).encode("utf-8"))

    # ---------- low level ------------------------------------------------

    def _write(self, body: bytes) -> None:
        sys.stdout.buffer.write(encode_frame(body))

    def _flush(self, stdout) -> None:
        try:
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            pass

    def _log(self, msg: str) -> None:
        sys.stderr.write("[nex-stdio] " + msg + "\n")
        try:
            sys.stderr.flush()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run Nex as a stdio MCP server (LSP-style framing).")
    p.add_argument("--platform", default=None,
                   help="If set, proxy all MCP traffic to this named "
                        "tunnel (e.g. 'roblox-studio', 'unreal-engine').")
    p.add_argument("--stdio-target", default=None,
                   help="Command line of the stdio MCP child to spawn "
                        "and proxy to (e.g. 'cmd /c mcp.bat'). "
                        "Used together with --platform when the platform's "
                        "upstream speaks stdio, not HTTP.")
    p.add_argument("--bind-http", type=int, default=None,
                   help="Bind to this TCP port (HTTP MCP) as well.")
    p.add_argument("--host", default="127.0.0.1",
                   help="HTTP bind host.")
    return p


def _run_stdio_session(args: argparse.Namespace) -> int:
    """Boot the stdio MCP server with the registry from server.py."""
    # Import the in-process registry from server.py.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import server  # noqa: E402
    server.configure_for_stdio()
    registry = server.get_stdio_registry(args)
    registry_provider = lambda: registry["tools_provider"]()
    local_router = registry["router"]
    srv = _StdioServer(
        registry_provider=registry_provider,
        local_router=local_router,
        local_provider=registry_provider,
        server_info={
            "name": "nex-stdio",
            "version": "1.0",
            "description": "Nex MCP tunnel — speaks stdio MCP for any "
                           "client (Claude Code, Cursor, VS Code Copilot, "
                           "Roblox Studio's built-in mcp.bat, …).",
        },
    )
    try:
        srv.run()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    if args.bind_http:
        # Spin an HTTP listener in a thread, run stdio in the main thread.
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        # Lazy import — only when needed.
        import server as _srv

        _srv.configure_for_stdio()

        class _S(BaseHTTPRequestHandler):
            def log_message(self, *_a, **_kw):
                pass

        httpd = ThreadingHTTPServer((args.host, args.bind_http), _S)
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return _run_stdio_session(args)


if __name__ == "__main__":
    sys.exit(main())
