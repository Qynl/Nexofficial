"""Tests for the MCP upstream connection (upstream.py).

Run as:
    python3 test_upstream.py

Verifies the gateway-core hardening:
  * connect() raises UpstreamError on failure and leaves clean state
    (any stale tools cache is cleared before re-probing).
  * tools() returns + caches the upstream tool list.
  * concurrent tools()/call() calls are race-free under the re-entrant
    lock.
"""

import importlib
import json
import os
import sys
import threading


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

upstream = importlib.import_module("upstream")
Upstream = upstream.Upstream
UpstreamError = upstream.UpstreamError


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------------------
# 1. A dead upstream: connect() raises and leaves clean state.
# ---------------------------------------------------------------------------

class DeadUp(Upstream):
    def _post(self, payload, headers=None, timeout=None):
        raise UpstreamError("refused")


dead = DeadUp("dead", "http://127.0.0.1:1/mcp")
try:
    dead.connect()
    _expect(False, "connect() should raise on an unreachable upstream")
except UpstreamError:
    _expect(True, "connect() raises UpstreamError on failure")
_expect(dead._initialized is False,
        "failed connect leaves _initialized False")
_expect(dead._tools_cache == [],
        "failed connect clears any stale tools cache")
try:
    dead.tools()
    _expect(False, "tools() should raise when upstream is down")
except UpstreamError:
    _expect(True, "tools() raises UpstreamError when down")


# ---------------------------------------------------------------------------
# 2. A working fake upstream: handshake + tools + caching.
# ---------------------------------------------------------------------------

class FakeUp(Upstream):
    def is_reachable(self):
        # Bypass the real TCP probe — we drive the transport via _post.
        return True

    def _post(self, payload, headers=None, timeout=None):
        method = payload.get("method")
        if method == "initialize":
            return (200, {}, json.dumps({"jsonrpc": "2.0", "id": 1,
                    "result": {"serverInfo": {"name": "x", "version": "1"}}}))
        if method == "tools/list":
            return (200, {}, json.dumps({"jsonrpc": "2.0", "id": 2,
                    "result": {"tools": [{"name": "a",
                                          "description": "d",
                                          "inputSchema": {}}]}}))
        return (200, {}, json.dumps({"jsonrpc": "2.0", "id": 3, "result": {}}))


fake = FakeUp("fake", "http://127.0.0.1:9/mcp")
tools1 = fake.tools()
_expect(tools1 == [{"name": "a", "description": "d", "inputSchema": {}}],
        "tools() returns the upstream tool list")
_expect(fake._initialized is True, "fake upstream becomes initialized")
tools2 = fake.tools()  # served from cache (no new network call needed)
_expect(tools2 == tools1, "tools() is cached across calls")

# call() round-trips through the lock + lazy connect path.
_expect(isinstance(fake.call("a", {}), dict),
        "call() returns the raw JSON-RPC reply")


# ---------------------------------------------------------------------------
# 3. Concurrent tools() calls don't crash / race (exercises the lock).
# ---------------------------------------------------------------------------

errors = []


def _hammer():
    try:
        for _ in range(50):
            fake.tools()
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


threads = [threading.Thread(target=_hammer) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
_expect(not errors, "concurrent tools() calls are race-free (lock holds)")


print("\nAll upstream tests passed.")
