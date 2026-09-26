"""The MCP transport: frame codec, stdio child lifecycle, HTTP round-trips.

The stdio test spawns a real Python child speaking NDJSON MCP over its
stdin/stdout — the same shape a third-party `npx mcp-server-*` would
have.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

os.environ.setdefault("NEX_HOME", tempfile.mkdtemp(prefix="nex-transport-"))

from mcp.transport import (StdioDecoder, encode_frame, Upstream,  # noqa: E402
                           UpstreamError)

_FAILED = []


def expect(cond, msg):
    if not cond:
        _FAILED.append(msg)
        print("FAIL - " + msg)


# A minimal stdio MCP server as an executable Python snippet.
STDIO_SERVER = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except ValueError:
        continue
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "stdio-echo", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "upper", "description": "uppercase text",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}}}}]}
    elif method == "tools/call":
        p = req.get("params") or {}
        result = {"content": [{"type": "text",
                               "text": (p.get("arguments")
                                        or {}).get("text", "").upper()}]}
    elif method == "notifications/initialized":
        continue
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                 "result": result}) + "\n")
    sys.stdout.flush()
'''


# A child that REPLIES with LSP Content-Length frames (non-conforming
# but tolerated on the read side).
LSP_REPLY_CHILD = r"""
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except ValueError:
        continue
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "lsp-child", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "upper", "description": "uppercase text",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}}}}]}
    elif method == "tools/call":
        p = req.get("params") or {}
        result = {"content": [{"type": "text",
                               "text": (p.get("arguments")
                                        or {}).get("text", "").upper()}]}
    elif method == "notifications/initialized":
        continue
    else:
        result = {}
    msg = json.dumps({"jsonrpc": "2.0", "id": rid,
                      "result": result}).encode()
    sys.stdout.buffer.write(
        b"Content-Length: %d\r\n\r\n" % len(msg) + msg)
    sys.stdout.flush()
"""


class FrameCodecTests(unittest.TestCase):
    def test_ndjson_frames(self):
        d = StdioDecoder()
        bodies = d.feed(b'{"a": 1}\n{"b": 2}\n')
        self.assertEqual([json.loads(b) for b in bodies],
                         [{"a": 1}, {"b": 2}])

    def test_lsp_frames(self):
        d = StdioDecoder()
        body1 = json.dumps({"hello": "world"}).encode()
        body2 = json.dumps({"x": 2}).encode()
        chunk = encode_frame(body1) + encode_frame(body2)
        bodies = d.feed(chunk)
        self.assertEqual([json.loads(b) for b in bodies],
                         [{"hello": "world"}, {"x": 2}])

    def test_split_chunks_reassemble(self):
        d = StdioDecoder()
        body = json.dumps({"key": "value", "n": 42}).encode()
        frame = encode_frame(body)
        out = []
        for i in range(0, len(frame), 3):     # hostile fragmentation
            out.extend(d.feed(frame[i:i + 3]))
        self.assertEqual([json.loads(b) for b in out],
                         [{"key": "value", "n": 42}])

    def test_crlf_headers(self):
        d = StdioDecoder()
        body = b'{"crlf": true}'
        frame = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        bodies = d.feed(frame)
        self.assertEqual(json.loads(bodies[0]), {"crlf": True})

    def test_oversize_frame_rejected(self):
        d = StdioDecoder()
        with self.assertRaises(ValueError):
            d.feed(b"Content-Length: 999999999\r\n\r\n")

    def test_partial_line_kept(self):
        d = StdioDecoder()
        self.assertEqual(d.feed(b'{"not": "yet"'), [])
        bodies = d.feed(b'}\n')
        self.assertEqual(json.loads(bodies[0]), {"not": "yet"})


class StdioTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = os.path.join(
            tempfile.mkdtemp(prefix="nex-stdio-"), "server.py")
        with open(cls.script, "w", encoding="utf-8") as f:
            f.write(STDIO_SERVER)

    def _mk(self):
        up = Upstream("stdio-echo", "stdio://local")
        up.stdio_command = (sys.executable, [self.script])
        return up

    def test_connect_initialize_tools(self):
        up = self._mk()
        try:
            up.connect()
            tools = up.tools()
            names = [t["name"] for t in tools]
            expect(names == ["upper"], "tools over stdio: %r" % names)
            expect(up.status().get("initialized") is True,
                   "status after connect must be initialized: %r"
                   % up.status())
        finally:
            up.disconnect()

    def test_call_roundtrip(self):
        up = self._mk()
        try:
            up.connect()
            resp = up.call("upper", {"text": "hello stdio"})
            text = resp["result"]["content"][0]["text"]
            expect(text == "HELLO STDIO", "stdio call result: %r" % text)
        finally:
            up.disconnect()

    def test_disconnect_kills_child(self):
        up = self._mk()
        up.connect()
        proc = up._stdio_proc
        expect(proc is not None and proc.poll() is None,
               "child must be alive while connected")
        up.disconnect()
        deadline = time.time() + 5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
        expect(proc.poll() is not None,
               "disconnect must terminate the child process")

    def test_lsp_framed_child_also_handled(self):
        # Some non-conforming children answer with LSP frames; the
        # decoder accepts both. (We SEND NDJSON per the MCP spec.)
        script = os.path.join(tempfile.mkdtemp(prefix="nex-lsp-"), "s.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(LSP_REPLY_CHILD)
        up = Upstream("lsp-child", "stdio://local", call_timeout=5)
        up.stdio_command = (sys.executable, [script])
        try:
            up.connect()
            tools = up.tools()
            expect([t["name"] for t in tools] == ["upper"],
                   "LSP-framed child replies must parse")
        finally:
            up.disconnect()

    def test_command_with_no_server_fails_cleanly(self):
        up = Upstream("broken", "stdio://local")
        up.stdio_command = (sys.executable, ["-c", "import sys;"
                                             "sys.exit(3)"])
        with self.assertRaises(UpstreamError):
            up.connect()


class HTTPTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        import tests.mcp_echo_server as echo
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), echo.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_http_connect_and_call(self):
        up = Upstream("echo", "http://127.0.0.1:%d/mcp" % self.port)
        up.connect()
        tools = up.tools()
        expect(any(t["name"] == "echo" for t in tools),
               "http tools listed")
        resp = up.call("add", {"a": 20, "b": 22})
        text = resp["result"]["content"][0]["text"]
        expect(text == "42", "http call: %r" % text)

    def test_unreachable_server_raises_upstream_error(self):
        up = Upstream("dead", "http://127.0.0.1:1/mcp")
        with self.assertRaises(UpstreamError):
            up.connect()

    def test_bad_json_response_is_upstream_error(self):
        # A server that returns non-JSON: the transport must raise
        # UpstreamError, never crash the caller.
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Garbage(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = b"<html>not json</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Garbage)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            up = Upstream("garbage", "http://127.0.0.1:%d/" %
                          httpd.server_address[1])
            with self.assertRaises(UpstreamError):
                up.connect()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    if _FAILED:
        sys.exit(1)
    print("\nAll transport tests passed.")
