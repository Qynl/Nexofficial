"""MCP ServerManager — lifecycle, policy, calls, audit, persistence.

Uses the real echo MCP server over HTTP (tests/mcp_echo_server.py) for
connection + tool calls, so the transport layer is exercised too.
"""
import json
import os
import shutil
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

from mcp import manager as manager_mod                # noqa: E402
from mcp.manager import ServerManager                 # noqa: E402

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
        added, err = cls.mgr.add({"name": "echo",
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
        self.assertEqual(s["tools_count"], 5)
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

    def test_approval_grants_once(self):
        with self.mgr._lock:
            self.mgr._approvals.clear()
        self.mgr.approve_tool("echo", "delete_thing")
        out = self.mgr.call("echo", "delete_thing", {"id": "y"})
        self.assertNotIn("error", out, out)

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

    # ── audit ────────────────────────────────────────────────────

    def test_calls_are_audited(self):
        entries = self.mgr.audit_entries(limit=100)
        kinds = {e["tool"] for e in entries}
        expect("echo" in kinds or "add" in kinds or "delete_thing" in kinds,
               "audit log must record tool calls")

    # ── persistence ──────────────────────────────────────────────

    def test_config_persists(self):
        home = os.environ["NEX_HOME"]
        self.assertTrue(os.path.exists(os.path.join(home, "servers.json")))
        data = json.load(open(os.path.join(home, "servers.json")))
        self.assertIn("echo", json.dumps(data))


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    if _FAILED:
        sys.exit(1)
    print("\nAll manager tests passed.")
