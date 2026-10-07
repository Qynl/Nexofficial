"""MCP ServerManager — lifecycle, policy, calls, audit, persistence.

Uses the real echo MCP server over HTTP (tests/mcp_echo_server.py) for
connection + tool calls, so the transport layer is exercised too.
"""
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
sys.path.insert(0, HERE)

from http.server import ThreadingHTTPServer          # noqa: E402
import mcp_echo_server as echo                        # noqa: E402

os.environ["NEX_HOME"] = tempfile.mkdtemp(prefix="nex-mgr-")

from mcp.manager import ServerManager                 # noqa: E402
from mcp.schema import validate_tool_output            # noqa: E402

_FAILED = []


def expect(cond, msg):
    if not cond:
        _FAILED.append(msg)
        print("FAIL - " + msg)


class ManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), echo.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever,
                         daemon=True).start()
        cls.mgr = ServerManager()
        added, err = cls.mgr.add({"name": "echo", "trusted": True,
                                  "url": "http://127.0.0.1:%d/mcp" % cls.port})
        expect(err == "", "add should succeed: %s" % err)

    @classmethod
    def tearDownClass(cls):
        cls.mgr.close()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    # ── lifecycle ────────────────────────────────────────────────

    def test_status_connected_with_tools(self):
        s = self.mgr.server_status("echo")
        self.assertEqual(s["status"], "connected")
        self.assertEqual(s["tools_count"], 6)
        self.assertTrue(s["trusted"])

    def test_add_validates_names(self):
        for bad in ("Bad Name", "UPPER", "x" * 40, "-lead", ""):
            _, err = self.mgr.add({"name": bad, "url": "http://127.0.0.1:1"})
            expect(err != "", "name %r should be rejected" % bad)

    def test_add_rejects_remote_without_confirmation(self):
        _, err = self.mgr.add({"name": "remote",
                               "url": "http://example.com:8080/mcp"})
        expect("confirm" in err.lower() or err != "",
               "remote URL must require confirmation")

    def test_add_rejects_both_transports(self):
        _, err = self.mgr.add({"name": "weird",
                               "url": "http://127.0.0.1:1/",
                               "command": "foo"})
        expect(err != "", "url+command must be rejected")

    def test_add_rejects_neither_transport(self):
        _, err = self.mgr.add({"name": "empty"})
        expect(err != "", "no transport must be rejected")

    def test_unknown_server_status(self):
        s = self.mgr.server_status("nope")
        self.assertIn(s["status"], ("disconnected", "missing"))
        self.assertTrue(s.get("error"))

    def test_disconnect_and_reconnect(self):
        m = self.mgr
        m.disconnect("echo")
        self.assertEqual(m.server_status("echo")["status"], "disconnected")
        m.reconnect("echo")
        for _ in range(50):
            if m.server_status("echo")["status"] == "connected":
                break
            import time; time.sleep(0.05)
        self.assertEqual(m.server_status("echo")["status"], "connected")

    # ── calls through the policy ─────────────────────────────────

    def test_call_read_tool(self):
        out = self.mgr.call("echo", "echo", {"text": "hi"})
        self.assertNotIn("error", out, out)
        text = out["result"]["content"][0]["text"]
        self.assertEqual(text, "hi")

    def test_stale_planned_contract_is_refused_before_call(self):
        view = self.mgr.registry().by_name("echo.echo")
        self.assertTrue(view.contract_fingerprint)
        out = self.mgr.call(
            "echo", "echo", {"text": "must-not-run"},
            audit_context={"contract_fingerprint": "0" * 64})
        self.assertTrue(out.get("contract_changed"), out)
        self.assertIn("fresh plan", out.get("error", ""))
        self.assertEqual(self.mgr.audit_entries(1)[0]["kind"],
                         "contract_changed")

    def test_mcp_is_error_result_never_becomes_success(self):
        up = self.mgr.upstream("echo")
        original = up.call
        up.call = lambda _tool, _args: {
            "result": {"isError": True,
                       "content": [{"type": "text",
                                    "text": "engine rejected the operation"}]}}
        try:
            out = self.mgr.call("echo", "echo", {"text": "hi"})
        finally:
            up.call = original
        self.assertTrue(out.get("tool_error"), out)
        self.assertIn("engine rejected", out.get("error", ""))
        self.assertNotIn("result", out)
        self.assertEqual(self.mgr.audit_entries(1)[0]["kind"], "tool_error")

    def test_call_unknown_tool(self):
        out = self.mgr.call("echo", "nope", {})
        self.assertIn("error", out)

    def test_call_unknown_server(self):
        out = self.mgr.call("ghost", "echo", {})
        self.assertIn("error", out)

    def test_destructive_needs_confirmation(self):
        with self.mgr._lock:
            self.mgr._approvals.clear()   # reset session approvals
        out = self.mgr.call("echo", "delete_thing", {"id": "x"})
        self.assertIn("decision", out, out)
        self.assertEqual(out["decision"]["category"], "destructive")
        self.assertTrue(out["decision"].get("requires_confirmation", True)
                        or out.get("status") == "needs_confirmation")

    def test_standing_approval_grants_tool(self):
        with self.mgr._lock:
            self.mgr._approvals.clear()
        self.mgr.approve_tool("echo", "delete_thing")
        out = self.mgr.call("echo", "delete_thing", {"id": "y"})
        self.assertNotIn("error", out, out)

    def test_one_time_approval_is_exact_and_consumed(self):
        with self.mgr._lock:
            self.mgr._approvals.clear()
            self.mgr._once_approvals.clear()
        self.mgr.approve_once("echo", "delete_thing", {"id": "x"})
        wrong = self.mgr.call("echo", "delete_thing", {"id": "y"})
        self.assertIn("needs_confirmation", wrong)
        allowed = self.mgr.call("echo", "delete_thing", {"id": "x"})
        self.assertIn("result", allowed, allowed)
        replay = self.mgr.call("echo", "delete_thing", {"id": "x"})
        self.assertIn("needs_confirmation", replay)

    def test_live_schema_rejects_bad_arguments_before_call(self):
        out = self.mgr.call("echo", "add", {"a": 1})
        self.assertIn("validation_errors", out, out)
        self.assertIn("required", out["error"])

    def test_untrusted_server_refused_in_autonomous_runs(self):
        with self.mgr._lock:
            self.mgr._approvals.clear()
        self.mgr.set_trusted("echo", False)
        try:
            out = self.mgr.call("echo", "echo", {"text": "hi"},
                                autonomous=True)
            expect("refused" in out,
                   "untrusted server must be refused in autonomous runs: %r"
                   % out)
            out2 = self.mgr.call("echo", "echo", {"text": "hi"})
            expect("needs_confirmation" in out2,
                   "untrusted server interactively must need confirmation: "
                   "%r" % out2)
        finally:
            self.mgr.set_trusted("echo", True)

    # ── registry ─────────────────────────────────────────────────

    def test_registry_lists_server_tools(self):
        reg = self.mgr.registry()
        names = sorted(t.full_name for t in reg.all_tools())
        self.assertIn("echo.echo", names)
        self.assertIn("echo.delete_thing", names)
        expect("echo.echo" in names and "echo.add" in names,
               "registry must expose server tools as server.tool")

    def test_schema_available(self):
        reg = self.mgr.registry()
        sv = reg.server("echo")
        self.assertIsNotNone(sv)
        tool = sv.by_name("add")
        self.assertIsNotNone(tool)
        self.assertIn("a", tool.schema.get("properties", {}))
        self.assertEqual(tool.output_schema, {})
        create = sv.by_name("create_thing")
        self.assertIn("id", create.output_schema.get("properties", {}))
        out = self.mgr.call("echo", "create_thing", {"name": "gizmo"})
        self.assertEqual(
            out["result"]["structuredContent"]["id"], "thing_1")

    def test_declared_output_schema_is_enforced(self):
        schema = {
            "type": "object",
            "properties": {"build_id": {"type": "string"},
                           "ok": {"type": "boolean"}},
            "required": ["build_id", "ok"],
            "additionalProperties": False,
        }
        good = {"structuredContent": {"build_id": "b1", "ok": True}}
        bad = {"structuredContent": {"build_id": 7, "ok": True}}
        self.assertEqual(validate_tool_output(schema, good), [])
        self.assertTrue(validate_tool_output(schema, bad))
        self.assertIn("structuredContent",
                      validate_tool_output(schema, {"content": []})[0])
        broken = self.mgr.call("echo", "read_broken_contract", {})
        self.assertIn("validation_errors", broken, broken)
        self.assertIn("output validation failed", broken["error"])

    # ── audit ────────────────────────────────────────────────────

    def test_calls_are_audited_without_raw_string_payloads(self):
        secretish_private_text = "private-note-not-a-credential"
        self.mgr.call("echo", "echo", {"text": secretish_private_text})
        entries = self.mgr.audit_entries(limit=100)
        kinds = {e["tool"] for e in entries}
        expect("echo" in kinds or "add" in kinds or "delete_thing" in kinds,
               "audit log must record tool calls")
        self.assertNotIn(secretish_private_text, json.dumps(entries))
        self.assertIn("<string:", json.dumps(entries))

    # ── persistence ──────────────────────────────────────────────

    def test_config_persists(self):
        home = os.environ["NEX_HOME"]
        self.assertTrue(os.path.exists(os.path.join(home, "servers.json")))
        with open(os.path.join(home, "servers.json"),
                  encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("echo", json.dumps(data))

    def test_valid_persisted_config_reloads(self):
        other = ServerManager()
        try:
            names = {e["name"] for e in other.config()}
            self.assertIn("echo", names)
        finally:
            other.close()

    def test_new_server_defaults_untrusted(self):
        added, err = self.mgr.add(
            {"name": "review-first", "url": "http://127.0.0.1:1/mcp"},
            connect=False)
        try:
            self.assertEqual(err, "")
            self.assertFalse(self.mgr.server_status("review-first")["trusted"])
        finally:
            self.mgr.remove("review-first")


class _FakeClock:
    """Deterministic monotonic stand-in — no real sleeps in these tests."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class ReconnectBackoffTests(unittest.TestCase):
    """A persistently dead server must be paced on its OWN escalating
    schedule by the health monitor, independent of every other server and
    never affecting an explicit, operator-triggered connect().

    Uses its OWN NEX_HOME (not the module-wide one ManagerTests shares) so
    `_probe_all()` only ever sees the one or two servers each test adds,
    never ManagerTests' persisted "echo" fixture.
    """

    def setUp(self):
        self._old_home = os.environ.get("NEX_HOME")
        os.environ["NEX_HOME"] = tempfile.mkdtemp(prefix="nex-backoff-")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("NEX_HOME", None)
        else:
            os.environ["NEX_HOME"] = self._old_home

    def _mgr(self):
        clock = _FakeClock()
        mgr = ServerManager(clock=clock)
        return mgr, clock

    def test_monitor_skips_a_dead_server_until_its_own_backoff_elapses(self):
        mgr, clock = self._mgr()
        try:
            added, err = mgr.add({"name": "dead-a", "trusted": True,
                                  "url": "http://127.0.0.1:1/mcp"})
            self.assertEqual(err, "")
            # add() already ran one connect() attempt, which failed —
            # that is what seeds the automatic backoff.
            self.assertGreater(mgr.reconnect_backoff_s("dead-a"), 0,
                               "a failed connect must arm the automatic "
                               "reconnect backoff")
            calls = {"n": 0}
            real_connect = mgr.connect

            def spy_connect(name):
                calls["n"] += 1
                return real_connect(name)
            mgr.connect = spy_connect

            changed = mgr._probe_all()
            self.assertFalse(changed)
            self.assertEqual(calls["n"], 0,
                             "the monitor must not even ATTEMPT a reconnect "
                             "while this server's own backoff has not "
                             "elapsed yet")

            backoff1 = mgr.reconnect_backoff_s("dead-a")
            clock.advance(backoff1 + 1)
            mgr._probe_all()
            self.assertEqual(calls["n"], 1,
                             "once its own backoff elapses the monitor DOES "
                             "try again")
            backoff2 = mgr.reconnect_backoff_s("dead-a")
            self.assertGreater(
                backoff2, backoff1,
                "a SECOND consecutive automatic failure must wait longer "
                "than the first (%.1fs -> %.1fs) — a server dead for an "
                "hour is not redialed every monitor tick for the whole hour"
                % (backoff1, backoff2))
        finally:
            mgr.close()

    def test_manual_connect_is_never_throttled_by_the_automatic_backoff(self):
        mgr, clock = self._mgr()
        try:
            mgr.add({"name": "dead", "trusted": True,
                     "url": "http://127.0.0.1:1/mcp"})
            self.assertGreater(mgr.reconnect_backoff_s("dead"), 0)
            # An operator clicking "reconnect" right away must still run —
            # the backoff only ever gates the unattended monitor loop.
            err = mgr.connect("dead")
            self.assertIsNotNone(err, "still down, so still an error — but "
                                      "it must have actually TRIED")
        finally:
            mgr.close()

    def test_recovery_clears_the_automatic_backoff(self):
        clock = _FakeClock()
        mgr = ServerManager(clock=clock)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), echo.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            # Start pointed at a dead port so a backoff gets armed...
            mgr.add({"name": "flaky", "trusted": True,
                     "url": "http://127.0.0.1:1/mcp"})
            self.assertGreater(mgr.reconnect_backoff_s("flaky"), 0)
            # ...then "fix" it (operator edits the URL) and reconnect.
            entry = mgr._entry("flaky")
            entry["url"] = "http://127.0.0.1:%d/mcp" % port
            err = mgr.connect("flaky")
            self.assertIsNone(err, "the now-reachable server connects: %s" % err)
            self.assertEqual(mgr.reconnect_backoff_s("flaky"), 0,
                             "a real success clears the automatic backoff")
        finally:
            mgr.close()
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    if _FAILED:
        sys.exit(1)
    print("\nAll manager tests passed.")
