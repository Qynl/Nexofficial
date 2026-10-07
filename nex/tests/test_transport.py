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
from http.server import BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

os.environ.setdefault("NEX_HOME", tempfile.mkdtemp(prefix="nex-transport-"))

from mcp.transport import (StdioDecoder, encode_frame, Upstream,  # noqa: E402
                           UpstreamError, _stdio_environment)

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


# A child that answers ONE tools/call and then exits — simulating a crash —
# so the respawn-must-reinitialize behavior can be tested deterministically.
# Every received method is logged as "<pid>:<method>" to a side-channel file
# since each process instance can't otherwise be told apart from outside.
CRASH_AFTER_ONE_CALL_SERVER = r'''
import json, os, sys
log_path = sys.argv[1]
def log(method):
    with open(log_path, "a") as f:
        f.write("%d:%s\n" % (os.getpid(), method))
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
    log(method)
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "crashy", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "upper", "description": "uppercase text",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}}}}]}
    elif method == "tools/call":
        p = req.get("params") or {}
        text = (p.get("arguments") or {}).get("text", "").upper()
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                     "result": {"content": [
                                         {"type": "text", "text": text}]}})
                         + "\n")
        sys.stdout.flush()
        sys.exit(0)          # "crash" right after answering
    elif method == "notifications/initialized":
        continue
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                 "result": result}) + "\n")
    sys.stdout.flush()
'''


class PerCallTimeoutTests(unittest.TestCase):
    """call(..., timeout=X) must override call_timeout for that ONE call
    only — other calls on the same connection keep using the configured
    default. This is what lets a manager give a known-slow BUILD action
    (baking lighting, packaging) more time without raising the timeout
    for every ordinary call on that server."""

    def _mk_fake(self, call_timeout=60.0):
        up = Upstream("x", "http://127.0.0.1:1/mcp", call_timeout=call_timeout)
        up._initialized = True
        seen = []

        def fake_post(payload, headers=None, timeout=None):
            seen.append(timeout)
            body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"),
                               "result": {"content": []}})
            return (200, {}, body)

        up._post = fake_post
        return up, seen

    def test_explicit_timeout_overrides_just_this_call(self):
        up, seen = self._mk_fake(call_timeout=60.0)
        up.call("some_tool", {}, timeout=900.0)
        expect(seen == [900.0],
               "an explicit per-call timeout must reach _post: %r" % seen)
        up.call("some_tool", {})
        expect(seen == [900.0, None],
               "the NEXT call without an override must not inherit the "
               "previous call's timeout — None means 'use call_timeout', "
               "not 'use whatever was passed last time': %r" % seen)

    def test_default_call_has_no_override(self):
        up, seen = self._mk_fake(call_timeout=45.0)
        up.call("some_tool", {})
        expect(seen == [None],
               "an ordinary call must pass timeout=None through to _post "
               "(self.call_timeout is the fallback already applied inside "
               "_post/_post_stdio): %r" % seen)


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

    def test_negative_frame_rejected(self):
        d = StdioDecoder()
        with self.assertRaises(ValueError):
            d.feed(b"Content-Length: -1\r\n\r\n")

    def test_partial_line_kept(self):
        d = StdioDecoder()
        self.assertEqual(d.feed(b'{"not": "yet"'), [])
        bodies = d.feed(b'}\n')
        self.assertEqual(json.loads(bodies[0]), {"not": "yet"})


class StdioTransportTests(unittest.TestCase):
    def test_child_environment_does_not_inherit_nex_or_provider_secrets(self):
        old_auth = os.environ.get("NEX_AUTH_TOKEN")
        old_key = os.environ.get("OPENAI_API_KEY")
        old_allow = os.environ.get("NEX_STDIO_ENV_ALLOW")
        try:
            os.environ["NEX_AUTH_TOKEN"] = "never-delegate"
            os.environ["OPENAI_API_KEY"] = "provider-secret"
            os.environ.pop("NEX_STDIO_ENV_ALLOW", None)
            child = _stdio_environment()
            self.assertNotIn("NEX_AUTH_TOKEN", child)
            self.assertNotIn("OPENAI_API_KEY", child)
            self.assertIn("PATH", child)
        finally:
            for key, value in (("NEX_AUTH_TOKEN", old_auth),
                               ("OPENAI_API_KEY", old_key),
                               ("NEX_STDIO_ENV_ALLOW", old_allow)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

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

    def test_respawned_stdio_child_is_reinitialized_before_next_call(self):
        # A stdio child that crashes is lazily respawned (_ensure_stdio_proc)
        # on the NEXT request — but the fresh process has never seen
        # `initialize`. Without _ensure_initialized()'s liveness check, the
        # respawned child would be sent `tools/call` as its very first
        # message, which violates the MCP handshake and most real servers
        # would simply reject it.
        work_dir = tempfile.mkdtemp(prefix="nex-crashy-")
        script = os.path.join(work_dir, "server.py")
        log_path = os.path.join(work_dir, "log.txt")
        with open(script, "w", encoding="utf-8") as f:
            f.write(CRASH_AFTER_ONE_CALL_SERVER)
        up = Upstream("crashy", "stdio://local", call_timeout=5)
        up.stdio_command = (sys.executable, [script, log_path])
        try:
            up.connect()
            resp1 = up.call("upper", {"text": "first"})
            text1 = resp1["result"]["content"][0]["text"]
            expect(text1 == "FIRST", "first call (first process): %r" % text1)

            # The child answered and exited ("crashed"). This call must
            # transparently respawn AND re-run the handshake before
            # retrying the actual tool call — all inside one call().
            resp2 = up.call("upper", {"text": "second"})
            text2 = resp2["result"]["content"][0]["text"]
            expect(text2 == "SECOND",
                   "call after an underlying crash must still succeed: %r"
                   % text2)

            with open(log_path, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            by_pid = {}
            for ln in lines:
                pid_s, method = ln.split(":", 1)
                by_pid.setdefault(int(pid_s), []).append(method)
            pids = sorted(by_pid)
            expect(len(pids) == 2,
                   "two distinct child processes must have run: %r" % pids)
            if len(pids) == 2:
                expect(by_pid[pids[1]][0] == "initialize",
                       "the RESPAWNED child's very first message must be "
                       "initialize, not a tool call: %r" % by_pid[pids[1]])
        finally:
            up.disconnect()

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

    def test_tool_result_is_error_is_not_success(self):
        up = Upstream("echo", "http://127.0.0.1:%d/mcp" % self.port)
        up.connect()
        with self.assertRaises(UpstreamError):
            up.call("fail_always", {})

    def test_unreachable_server_raises_upstream_error(self):
        up = Upstream("dead", "http://127.0.0.1:1/mcp")
        with self.assertRaises(UpstreamError):
            up.connect()

    def test_cross_origin_redirect_is_refused(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        hits = []

        class Target(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
            def do_POST(self):  # noqa: N802
                hits.append(True)
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        target_port = target.server_address[1]

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:%d/mcp"
                                 % target_port)
                self.send_header("Content-Length", "0")
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=target.serve_forever, daemon=True).start()
        threading.Thread(target=redirect.serve_forever, daemon=True).start()
        try:
            up = Upstream("redirect", "http://127.0.0.1:%d/mcp"
                          % redirect.server_address[1])
            with self.assertRaises(UpstreamError):
                up.connect()
            self.assertEqual(hits, [], "redirect target must never receive MCP data")
        finally:
            redirect.shutdown(); redirect.server_close()
            target.shutdown(); target.server_close()

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


class _FakeClock:
    """Deterministic monotonic stand-in — no real sleeps in these tests."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FlakyHandler(BaseHTTPRequestHandler):
    """initialize/resources/prompts always succeed; tools/list fails while
    `fail` is True — enough to drive the circuit breaker on purpose."""

    fail = True
    hits = 0

    def log_message(self, *a):  # noqa: D401 — quiet
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        method = body.get("method")
        rid = body.get("id")
        type(self).hits += 1
        if method == "initialize":
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "flaky", "version": "1"}}})
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "tools/list" and type(self).fail:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "tools/list":
            self._send(200, {"jsonrpc": "2.0", "id": rid,
                             "result": {"tools": []}})
            return
        if method in ("resources/list", "prompts/list"):
            key = "resources" if method == "resources/list" else "prompts"
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {key: []}})
            return
        self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {}})

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ToolsCacheTests(unittest.TestCase):
    """tools(force=True) must bypass the TTL cache — the health monitor
    depends on this to actually probe the connection instead of just
    re-reading a stale cached list."""

    def test_force_bypasses_the_ttl_cache(self):
        up = Upstream("x", "http://127.0.0.1:1/mcp")
        up._initialized = True
        calls = {"n": 0}

        def fake_fetch():
            calls["n"] += 1
            return [{"name": "t%d" % calls["n"]}]

        up._fetch_tools = fake_fetch
        first = up.tools()
        expect(calls["n"] == 1, "first call must fetch: %r" % calls)
        expect(first[0]["name"] == "t1", "first result: %r" % first)

        # Cache is fresh (TTL is 30s, no time passed) — an un-forced call
        # must NOT hit the network again.
        second = up.tools()
        expect(calls["n"] == 1,
               "fresh cache must be reused without forcing: %r" % calls)
        expect(second[0]["name"] == "t1", "cached result: %r" % second)

        # force=True must re-fetch even though the cache is still fresh —
        # this is what the health monitor relies on every probe tick.
        third = up.tools(force=True)
        expect(calls["n"] == 2,
               "force=True must bypass the TTL cache: %r" % calls)
        expect(third[0]["name"] == "t2", "forced result: %r" % third)

    def test_force_failure_is_reported_even_with_a_fresh_cache(self):
        up = Upstream("x", "http://127.0.0.1:1/mcp")
        up._initialized = True
        up._tools_cache = [{"name": "stale-but-cached"}]
        up._tools_fetched_at = time.monotonic()

        def fail_fetch():
            raise UpstreamError("server went away")

        up._fetch_tools = fail_fetch
        with self.assertRaises(UpstreamError):
            up.tools(force=True)
        # An un-forced call right after must still see the TTL honored and
        # NOT raise (same recent fetch timestamp as before the forced
        # attempt was reverted by the failure — the cache itself was left
        # untouched by the failed forced refresh).
        cached = up.tools()
        expect(cached[0]["name"] == "stale-but-cached",
               "a failed forced refresh must not corrupt the existing "
               "cache: %r" % cached)


class CircuitBreakerTests(unittest.TestCase):
    """The breaker must protect an already-connected session too, and back
    off FURTHER on each repeat trip instead of a flat reopen time forever."""

    def _server(self):
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def test_breaker_fast_fails_an_already_initialized_session(self):
        _FlakyHandler.fail = True
        _FlakyHandler.hits = 0
        httpd = self._server()
        try:
            clock = _FakeClock()
            up = Upstream("flaky", "http://127.0.0.1:%d/mcp"
                          % httpd.server_address[1], clock=clock)
            up.connect()
            # connect() tolerates the tools/list failure and stays
            # initialized; resources/prompts succeeded, so the failure
            # counter is back at 0 — three FRESH failures are needed.
            self.assertEqual(up._consecutive_failures, 0)
            for _ in range(3):
                with self.assertRaises(UpstreamError):
                    up.tools()
            self.assertGreaterEqual(up._circuit_trips, 1,
                                    "three consecutive failures must trip "
                                    "the breaker")
            hits_before = _FlakyHandler.hits
            with self.assertRaises(UpstreamError) as ctx:
                up.tools()
            self.assertIn("circuit breaker", str(ctx.exception))
            self.assertEqual(
                _FlakyHandler.hits, hits_before,
                "a call while the breaker is OPEN must not touch the "
                "network at all — this is what makes the breaker actually "
                "protect an ongoing (already-initialized) session, not "
                "just a fresh connect()")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_repeat_trips_back_off_further_each_time(self):
        _FlakyHandler.fail = True
        _FlakyHandler.hits = 0
        httpd = self._server()
        try:
            clock = _FakeClock()
            up = Upstream("flaky", "http://127.0.0.1:%d/mcp"
                          % httpd.server_address[1], clock=clock)
            up.connect()
            for _ in range(3):
                with self.assertRaises(UpstreamError):
                    up.tools()
            wait1 = up._circuit_open_until - clock()
            self.assertGreaterEqual(wait1, 10.0)
            self.assertLessEqual(wait1, 11.5)   # base 10s + <=10% jitter
            clock.advance(wait1 + 0.1)
            with self.assertRaises(UpstreamError):
                up.tools()                       # breaker closed, retries, fails again
            wait2 = up._circuit_open_until - clock()
            self.assertGreater(wait2, wait1,
                               "a SECOND consecutive trip must wait longer "
                               "than the first (%.1fs -> %.1fs)"
                               % (wait1, wait2))
            # recovery: let the server start answering and advance past the
            # (longer) second cooldown — one real success must reset both
            # the failure streak and the trip escalation.
            clock.advance(wait2 + 0.1)
            _FlakyHandler.fail = False
            up.tools()
            self.assertEqual(up._consecutive_failures, 0)
            self.assertEqual(up._circuit_trips, 0)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    if _FAILED:
        sys.exit(1)
    print("\nAll transport tests passed.")
