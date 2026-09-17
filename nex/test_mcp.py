"""Tests for the MCP server backend + autonomous observer.

Run as:
    python3 test_mcp.py

Assumes the server is reachable at http://localhost:8787.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error


BASE = os.environ.get("NEX_BASE", "http://localhost:8787")
WORKSPACE = os.path.expanduser(os.environ.get("NEX_TOOLS_ROOT",
                                              os.path.join("~", "nex_workspace")))


def _http(method, path, payload=None, timeout=10):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------------
# 1. JSON-RPC initialize
# ---------------------------------------------------------------------
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18",
                                 "client": {"name": "test-suite",
                                            "version": "0.0.1"}}})
_expect(status == 200, "POST /mcp initialize returns 200")
_init = json.loads(body)
_expect("result" in _init, "initialize has result")
_expect(_init["result"]["protocolVersion"] == "2025-06-18",
        "initialize protocolVersion == 2025-06-18")
_expect("tools" in _init["result"].get("capabilities", {}),
        "initialize advertises tools capability")


# ---------------------------------------------------------------------
# 2. tools/list
# ---------------------------------------------------------------------
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
_expect(status == 200, "POST /mcp tools/list returns 200")
_tools = json.loads(body)["result"]["tools"]
_names = {t["name"] for t in _tools}
_required = {"list_files", "read_file", "write_file",
             "run_command", "search_files",
             "log_event", "recent_events", "speak"}
_missing = _required - _names
_expect(not _missing, "tools/list exposes all required tools (%s missing)"
        % _missing)
# Every tool has the MCP-required fields.
for t in _tools:
    _expect({"name", "description", "inputSchema"} <= set(t),
            "tool %r has name/description/inputSchema" % t["name"])


# ---------------------------------------------------------------------
# 3. tools/call: list_files
# ---------------------------------------------------------------------
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "list_files", "arguments": {}}})
_expect(status == 200, "POST /mcp tools/call list_files returns 200")
_resp = json.loads(body)["result"]
_expect(_resp["isError"] is False, "list_files not an error")
_content = json.loads(_resp["content"][0]["text"])
_expect(_content["path"] == ".", "list_files path == '.' (sandbox root)")
_expect("entries" in _content, "list_files returns entries")


# ---------------------------------------------------------------------
# 4. tools/call: write_file then read_file round-trip
# ---------------------------------------------------------------------
test_path = "test_mcp_%d.txt" % int(time.time())
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "write_file",
                                 "arguments": {"path": test_path,
                                                "content": "hello mcp"}}})
_expect(status == 200, "POST /mcp write_file returns 200")
_write = json.loads(body)["result"]
_expect(_write["isError"] is False, "write_file not error")
_expect("bytes" in json.loads(_write["content"][0]["text"]),
        "write_file reports bytes written")

status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                      "params": {"name": "read_file",
                                 "arguments": {"path": test_path}}})
_expect(status == 200, "POST /mcp read_file returns 200")
_read = json.loads(body)["result"]
_expect(json.loads(_read["content"][0]["text"])["content"] == "hello mcp",
        "read_file returns content written by write_file")


# ---------------------------------------------------------------------
# 5. Sandbox: absolute path rejected
# ---------------------------------------------------------------------
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                      "params": {"name": "read_file",
                                 "arguments": {"path": "/etc/passwd"}}})
_expect(status == 200, "sandbox rejection still 200 (error inside result)")
_resp = json.loads(body)["result"]
_expect(_resp["isError"] is True,
        "read_file of /etc/passwd is an error")
_expect("absolute" in _resp["content"][0]["text"].lower(),
        "error mentions absolute paths")


# ---------------------------------------------------------------------
# 6. Sandbox: parent traversal rejected
# ---------------------------------------------------------------------
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                      "params": {"name": "read_file",
                                 "arguments": {"path": "../escape"}}})
_resp = json.loads(body)["result"]
_expect(_resp["isError"] is True, "../escape is an error")
_expect("escape" in _resp["content"][0]["text"].lower(),
        "error mentions path escape")


# ---------------------------------------------------------------------
# 7. tools/call: speak publishes events to the bus
# ---------------------------------------------------------------------
# Open SSE in background; capture speak events.
import threading, queue

events_q = queue.Queue()
got_delta = threading.Event()
got_end = threading.Event()


def _drain_sse():
    req = urllib.request.Request(BASE + "/api/events")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            buf = b""
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk
                while b"\n\n" in buf:
                    frame, buf = buf.split(b"\n\n", 1)
                    for line in frame.split(b"\n"):
                        if line.startswith(b"data:"):
                            try:
                                evt = json.loads(line[5:].decode("utf-8"))
                            except Exception:
                                continue
                            if evt.get("type") == "speak.delta":
                                events_q.put(evt)
                                got_delta.set()
                            elif evt.get("type") == "speak.end":
                                events_q.put(evt)
                                got_end.set()
    except Exception:
        pass


t = threading.Thread(target=_drain_sse, daemon=True)
t.start()
time.sleep(0.5)

status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                      "params": {"name": "speak",
                                 "arguments": {"text": "Tests are green [HAPPY]",
                                                "emotion": "HAPPY"}}})
_expect(status == 200, "tools/call speak returns 200")
_speak_resp = json.loads(body)["result"]
_expect(_speak_resp["isError"] is False, "speak not error")

# Wait up to 5s for SSE delivery.
_expect(got_delta.wait(5), "speak.delta arrived on /api/events SSE")
_expect(got_end.wait(5), "speak.end arrived on /api/events SSE")

# Verify the text in the delivered event was cleaned of the [HAPPY] tag.
# (SSE may replay older buffered events first, so find the one matching
# our text specifically.)
_our_deltas = [e for e in list(events_q.queue)
               if e.get("text") == "Tests are green"]
_expect(_our_deltas, "our speak.delta arrived on /api/events SSE")
_expect(_our_deltas[0].get("text") and "[" not in _our_deltas[0]["text"],
        "no raw tag fragment in SSE payload")


# ---------------------------------------------------------------------
# 8. tools/call: log_event appends to .nex_log.jsonl
# ---------------------------------------------------------------------
# Reset the .nex_log file so we can see the new entry appear.
log_path = os.path.join(WORKSPACE, ".nex_log.jsonl")
_existing_before = 0
if os.path.exists(log_path):
    _existing_before = sum(1 for _ in open(log_path))

status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                      "params": {"name": "log_event",
                                 "arguments": {"kind": "test",
                                                "message": "log_event works",
                                                "tags": ["HAPPY"]}}})
_expect(status == 200, "tools/call log_event returns 200")
_le = json.loads(body)["result"]
_expect(_le["isError"] is False, "log_event not error")

# Allow a moment for the write to flush, then verify the file grew.
time.sleep(0.2)
_existing_after = 0
if os.path.exists(log_path):
    _existing_after = sum(1 for _ in open(log_path))
_expect(_existing_after > _existing_before,
        ".nex_log.jsonl grew after log_event (%d -> %d)"
        % (_existing_before, _existing_after))


# ---------------------------------------------------------------------
# 9. ping
# ---------------------------------------------------------------------
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 10, "method": "ping"})
_expect(status == 200, "POST /mcp ping returns 200")
_expect("result" in json.loads(body), "ping has result")


# ---------------------------------------------------------------------
# 10. JSON-RPC error: unknown method
# ---------------------------------------------------------------------
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 11, "method": "bogus/method"})
_err = json.loads(body)
_expect("error" in _err, "unknown method returns JSON-RPC error")
_expect(_err["error"]["code"] == -32601, "error code is -32601 (method not found)")


# ---------------------------------------------------------------------
# 11. REST shim: /api/tools returns same shape as MCP tools/list
# ---------------------------------------------------------------------
status, body = _http("GET", "/api/tools")
_expect(status == 200, "GET /api/tools returns 200")
_rest_tools = json.loads(body)["tools"]
_expect({t["name"] for t in _rest_tools} == _names,
        "/api/tools lists the same tools as /mcp tools/list")


# ---------------------------------------------------------------------
# 12. Observer module loads and can be imported standalone.
# ---------------------------------------------------------------------
# We don't actually fire autonomous speaks (they're time-based); just
# confirm the module is importable and the singleton API works.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import importlib  # noqa: E402
observer = importlib.import_module("observer")
_expect(hasattr(observer, "start_observer"), "observer.start_observer exists")
_expect(hasattr(observer, "force_speak"), "observer.force_speak exists")
_expect(callable(observer.force_speak), "observer.force_speak is callable")

# 12b. force_speak on a standalone Observer instance publishes to its
# bus callback. Confirms the observer can speak autonomously.
collected = []


class _FakeObserver:
    def __init__(self):
        self._events = []

    def publish(self, e):
        self._events.append(e)


fo = _FakeObserver()
# Instantiate directly (bypass module singleton so we don't disturb the
# server's actual observer).
o = observer.Observer(fo.publish)
o.force_speak("Test autonomous speak", emotion="PROUD")
_expect(fo._events, "Observer.force_speak emits to bus")
_types = [e.get("type") for e in fo._events]
_expect("speak.delta" in _types, "force_speak produced speak.delta")
_expect("speak.end" in _types, "force_speak produced speak.end")
_expect(o._last_speak_t > 0, "force_speak updated last_speak_t (cooldown anchor)")


# ---------------------------------------------------------------------
# 13. Sandbox: search_files constrained to workspace
# ---------------------------------------------------------------------
status, body = _http("POST", "/mcp",
                     {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                      "params": {"name": "search_files",
                                 "arguments": {"query": "hello"}}})
_sf = json.loads(body)["result"]
_expect(_sf["isError"] is False, "search_files not error")
_search = json.loads(_sf["content"][0]["text"])
_expect("results" in _search, "search_files returns results array")
_expect(any(h.get("path", "").endswith(test_path)
            for h in _search.get("results", [])),
        "search_files finds the test file we just wrote")


print("\n13 passed, 0 failed.\nAll MCP tests passed.")
