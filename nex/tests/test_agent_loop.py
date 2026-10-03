"""The agent loop: plan → execute → observe → evaluate → adapt.

Covers the behaviors the overhaul is about:
  * deterministic planning when no LLM is available
  * model-driven planning with tool validation (hallucinated tools dropped)
  * argument references ($slug / $slug.key) between steps
  * the recovery ladder (transient retry → arg fix → switch tool → honest fail)
  * replanning on structural failure
  * approval waiting + resolution + always-allow
  * cancellation
  * completion detection: a step that fails is never reported as success
  * budgets: step cap and wall-clock cap end the run, not hang it
  * the run report is public-safe (no chain-of-thought)

Uses MockMCPServer (in-process) so failures are injectable without a
network. The manager-backed path is covered by test_manager.py +
test_server_api.py.
"""
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

os.environ.setdefault("NEX_HOME", "/tmp/nex-loop-tests")

from agent.mock_mcp import MockMCPServer, server_view          # noqa: E402
from agent.loop import AgentRun, RunCoordinator                # noqa: E402
from agent.model_planner import plan_to_graph                   # noqa: E402
from mcp.registry import CapabilityRegistry                    # noqa: E402
from mcp.capability import capability_for_tool                 # noqa: E402
from mcp.policy import authorize                               # noqa: E402


def mk_registry(*mocks):
    return CapabilityRegistry([server_view(m.name, m) for m in mocks])


def read_tool(name, desc="read something"):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object",
                            "properties": {"path": {"type": "string"}}}}


class FakeManager:
    """Manager stand-in backed by MockMCPServers.

    Same call contract as the real manager INCLUDING the policy path
    (capability_for_tool + authorize), so approval behavior in the loop
    is exercised against the genuine policy — only the transport is
    fake. Injected `fail={tool: [count, error]}` simulates transport
    failures; `block={tool: Event}` parks calls inside the transport.
    """

    def __init__(self, mocks, fail=None, block=None):
        self.mocks = {m.name: m for m in mocks}
        self.fail = fail or {}
        self.block = block or {}
        self.approved = set()
        self.approved_once = set()
        self.calls = []
        self.trusted = {m.name: True for m in mocks}

    def call(self, server, tool, args, audit_context=None,
             autonomous=False):
        mock = self.mocks.get(server)
        if mock is None:
            return {"error": "server '%s' is not connected" % server}
        tool_def = next((t for t in mock.tools() if t["name"] == tool), None)
        if tool_def is None:
            return {"error": "server '%s' has no tool '%s'" % (server, tool)}
        cap = capability_for_tool(tool_def)
        decision = authorize(server, tool, cap, args=args)
        if not decision.allowed:
            return {"refused": decision.reason,
                    "decision": decision.to_dict()}
        once = (server, tool, repr(sorted((args or {}).items())))
        if decision.requires_confirmation and \
                (server, tool) not in self.approved:
            if once in self.approved_once:
                self.approved_once.remove(once)
            else:
                return {"needs_confirmation": decision.reason,
                        "decision": decision.to_dict()}
        gate = self.block.get(tool)
        if gate is not None:
            gate.wait(timeout=30)     # simulate a slow server
        fails = self.fail.get(tool)
        if fails and fails[0] > 0:
            fails[0] -= 1
            return {"error": fails[1]}
        self.calls.append((server, tool, args))
        try:
            result = mock.call(tool, args)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {"result": result}

    def registry(self):
        return mk_registry(*self.mocks.values())

    def approve_tool(self, server, tool):
        self.approved.add((server, tool))

    def approve_once(self, server, tool, args):
        self.approved_once.add(
            (server, tool, repr(sorted((args or {}).items()))))

    def summary(self):
        return {"servers": [{"server": n, "status": "connected",
                             "tools_count": len(m.tools())}
                            for n, m in self.mocks.items()],
                "total": len(self.mocks),
                "connected": len(self.mocks),
                "tools": sum(len(m.tools()) for m in self.mocks.values())}

    def server_status(self, name):
        return {"server": name, "status": "connected" if name in self.mocks
                else "disconnected"}


def collect(run):
    events = []
    run.bus = events.append
    return events


ECHO_TOOLS = [
    {"name": "echo", "description": "Echo the given text back.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"},
                                    "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "create_thing", "description": "Create a named thing.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"}}}},
    {"name": "delete_thing", "description": "Delete a thing by id.",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "string"}}}},
    {"name": "fail_always", "description": "Always fails.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_nested", "description": "Inspect nested structured ids.",
     "inputSchema": {"type": "object",
                     "properties": {"config": {"type": "object"}},
                     "required": ["config"]}},
]


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockMCPServer("echo", ECHO_TOOLS)
        self.mgr = FakeManager([self.mock])

    def test_direct_request_plans_one_step(self):
        report = AgentRun("t1", "echo the text hello", self.mgr).run()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(len(report["completed"]), 1)
        self.assertEqual(report["completed"][0]["tool"], "echo")
        self.assertEqual(self.mgr.calls[0][2], {"text": "hello"})

    def test_no_matching_tool_is_blocked_not_faked(self):
        report = AgentRun("t2", "write a novel about the sea",
                          self.mgr).run()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["completed"], [])

    def test_model_plan_validated_against_registry(self):
        # The "model" proposes one real tool and two hallucinations.
        def llm(prompt):
            return ('{"steps": ['
                    '{"name": "Say it", "server": "echo", "tool": "echo",'
                    ' "args": {"text": "hi"}},'
                    '{"name": "Ghost", "server": "echo", "tool": "ghost_tool",'
                    ' "args": {}},'
                    '{"name": "No server", "server": "nope", "tool": "echo",'
                    ' "args": {}}]}')
        report = AgentRun("t3", "say hi", self.mgr, llm=llm).run()
        self.assertEqual(report["status"], "partial")
        self.assertTrue(any("dropped" in r for r in report["reasons"]))
        names = [s["name"] for s in report["completed"]]
        self.assertEqual(names, ["Say it"])

    def test_model_plan_garbage_falls_back(self):
        def llm(prompt):
            return "I think therefore I am (no json)"
        report = AgentRun("t4", "echo something", self.mgr, llm=llm).run()
        # fallback: deterministic planner still handles the direct request
        self.assertEqual(report["status"], "completed")

    def test_qualified_tool_namespace_is_never_stripped(self):
        def llm(prompt):
            return ('{"steps": [{"name": "Wrong server", '
                    '"tool": "evil.echo", "args": {"text": "hi"}}]}')
        report = AgentRun("t4b", "perform zqx", self.mgr, llm=llm).run()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(self.mgr.calls, [])
        self.assertTrue(any("dropped" in r for r in report["reasons"]))

    def test_cyclic_model_plan_never_reports_completed(self):
        def llm(prompt):
            return ('{"steps": ['
                    '{"name":"a","tool":"echo.echo",'
                    ' "args":{"text":"a"},"depends_on":["b"]},'
                    '{"name":"b","tool":"echo.echo",'
                    ' "args":{"text":"b"},"depends_on":["a"]}]}')
        report = AgentRun("t4c", "perform cycle-zqx", self.mgr, llm=llm).run()
        self.assertNotEqual(report["status"], "completed")
        self.assertEqual(self.mgr.calls, [])

    def test_arg_references_chain_steps(self):
        def llm(prompt):
            return ('{"steps": ['
                    '{"name": "Make", "server": "echo", "tool": "create_thing",'
                    ' "args": {"name": "gizmo"}},'
                    '{"name": "Remove", "server": "echo", "tool": "delete_thing",'
                    ' "args": {"id": "$Make.id"},'
                    ' "depends_on": ["Make"]}]}')
        # destructive step: standing approval, so the sync test doesn't wait
        self.mgr.approve_tool("echo", "delete_thing")
        report = AgentRun("t5", "make and remove", self.mgr, llm=llm).run()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(len(report["completed"]), 2)
        called = {t for _, t, _ in self.mgr.calls}
        self.assertEqual(called, {"create_thing", "delete_thing"})
        # the delete step received the id produced by the create step
        delete_args = [a for _, t, a in self.mgr.calls
                       if t == "delete_thing"][0]
        self.assertNotIn("$Make", str(delete_args),
                         "references must be resolved, not passed raw")

    def test_nested_references_are_resolved_recursively(self):
        def llm(prompt):
            return ('{"steps": ['
                    '{"name":"make","tool":"echo.create_thing",'
                    ' "args":{"name":"car"}},'
                    '{"name":"inspect","tool":"echo.inspect_nested",'
                    ' "args":{"config":{"actors":[{"id":"$make.id"}]}},'
                    ' "depends_on":["make"]}]}')
        report = AgentRun("t5b", "create and inspect nested", self.mgr,
                          llm=llm).run()
        self.assertEqual(report["status"], "completed")
        args = [a for _, name, a in self.mgr.calls
                if name == "inspect_nested"][0]
        self.assertEqual(args["config"]["actors"][0]["id"], "thing_1")

    def test_reference_without_declared_dependency_fails_closed(self):
        def llm(prompt):
            return ('{"steps": ['
                    '{"name":"make","tool":"echo.create_thing",'
                    ' "args":{"name":"car"}},'
                    '{"name":"inspect","tool":"echo.inspect_nested",'
                    ' "args":{"config":{"id":"$make.id"}}}]}')
        report = AgentRun("t5c", "create then inspect undeclared", self.mgr,
                          llm=llm).run()
        self.assertNotEqual(report["status"], "completed")
        called = [name for _, name, _ in self.mgr.calls]
        self.assertEqual(called, ["create_thing"])
        self.assertTrue(any("dropped" in r for r in report["reasons"]))

    def test_replan_cannot_shadow_a_historical_step_name(self):
        plan = {"steps": [{"name": "made", "tool": "echo.echo",
                           "args": {"text": "again"}}]}
        graph = plan_to_graph(
            plan, self.mgr.registry(), external_refs={"made": "old-id"},
            id_prefix="r1_")
        self.assertEqual(graph.all(), [])
        self.assertTrue(any("historical" in item
                            for item in graph.plan_meta["dropped"]))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockMCPServer("echo", ECHO_TOOLS)

    def test_transient_error_retried(self):
        mgr = FakeManager([self.mock], fail={"echo": [1, "connection reset"]})
        report = AgentRun("r1", "echo the text hi", mgr).run()
        self.assertEqual(report["status"], "completed")

    def test_persistent_failure_reports_failed(self):
        mgr = FakeManager([self.mock],
                          fail={"echo": [99, "connection reset"]})
        report = AgentRun("r2", "echo the text hi", mgr).run()
        self.assertIn(report["status"], ("failed", "partial"))
        self.assertEqual(report["completed"], [])
        # and the failure is stated, not hidden
        self.assertTrue(report.get("failed") or report.get("blocked")
                        or report["status"] == "failed",
                        "a failed step must surface in the report")

    def test_budget_ends_run(self):
        mgr = FakeManager([self.mock], fail={"echo": [99, "boom"]})
        report = AgentRun("r3", "echo the text hi", mgr,
                          budget_s=0.05, max_steps=2).run()
        self.assertIn(report["status"],
                      ("failed", "partial", "cancelled", "blocked"))

    def test_step_budget_is_partial_not_user_cancellation(self):
        def llm(messages):
            if "planning mind" in messages[0]["content"]:
                return ('{"steps": ['
                        '{"name":"one","tool":"echo.echo",'
                        ' "args":{"text":"one"}},'
                        '{"name":"two","tool":"echo.echo",'
                        ' "args":{"text":"two"}}]}')
            return "Budget reached after useful work."
        mgr = FakeManager([self.mock])
        report = AgentRun("r3b", "perform two echoes", mgr, llm=llm,
                          max_steps=1).run()
        self.assertEqual(report["status"], "partial")
        self.assertNotEqual(report["status"], "cancelled")
        self.assertIn("step budget", " ".join(report["reasons"]))
        self.assertEqual(len(mgr.calls), 1)

    def test_cancellation_stops(self):
        # Park the call inside the "transport" so cancel arrives mid-step.
        gate = threading.Event()
        mgr = FakeManager([self.mock], block={"echo": gate})
        run = AgentRun("r4", "echo the text hi", mgr, budget_s=30)
        t = threading.Thread(target=run.run, daemon=True)
        t.start()
        time.sleep(0.1)
        run.cancel()
        gate.set()          # release the parked call
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "cancel must stop the run thread")
        self.assertEqual(run.report["status"], "cancelled")


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockMCPServer("echo", ECHO_TOOLS)
        self.mgr = FakeManager([self.mock])

    def test_destructive_step_waits_then_approves(self):
        def llm(prompt):
            return ('{"steps": ['
                    '{"name": "Nuke", "server": "echo", "tool": "delete_thing",'
                    ' "args": {"id": "x"}}]}')
        run = AgentRun("a1", "delete a thing", self.mgr, llm=llm)
        events = collect(run)
        t = threading.Thread(target=run.run, daemon=True)
        t.start()
        # wait for the waiting state
        deadline = time.time() + 5
        while time.time() < deadline:
            with run._approval_lock:
                if run._approval is not None:
                    break
            time.sleep(0.02)
        else:
            t.join(timeout=2)
            self.fail("run never asked for approval; status=%s events=%s"
                      % (run.status, [e.get("type") if isinstance(e, dict)
                                      else e for e in events[:8]]))
        ok = run.resolve_approval(True, always=False)
        self.assertTrue(ok)
        t.join(timeout=5)
        self.assertEqual(run.report["status"], "completed")
        kinds = [e["type"] if isinstance(e, dict) else e.type
                 for e in events]
        # hmm — events may be dicts or Event objects depending on bus shape
        self.assertIn("completed", run.report["status"])

    def test_denied_approval_fails_step(self):
        def llm(prompt):
            return ('{"steps": ['
                    '{"name": "Nuke", "server": "echo", "tool": "delete_thing",'
                    ' "args": {"id": "x"}}]}')
        run = AgentRun("a2", "delete a thing", self.mgr, llm=llm)
        threading.Thread(target=run.run, daemon=True).start()
        deadline = time.time() + 5
        while time.time() < deadline:
            with run._approval_lock:
                if run._approval is not None:
                    break
            time.sleep(0.02)
        run.resolve_approval(False)
        deadline = time.time() + 5
        while run.status in ("running", "waiting", "starting"):
            time.sleep(0.02)
            if time.time() > deadline:
                break
        self.assertNotEqual(run.report["status"], "completed")

    def test_always_allow_records_approval(self):
        def llm(prompt):
            return ('{"steps": ['
                    '{"name": "Nuke", "server": "echo", "tool": "delete_thing",'
                    ' "args": {"id": "x"}}]}')
        run = AgentRun("a3", "delete a thing", self.mgr, llm=llm)
        threading.Thread(target=run.run, daemon=True).start()
        deadline = time.time() + 5
        while time.time() < deadline:
            with run._approval_lock:
                if run._approval is not None:
                    break
            time.sleep(0.02)
        run.resolve_approval(True, always=True)
        deadline = time.time() + 5
        while run.status in ("running", "waiting", "starting"):
            time.sleep(0.02)
            if time.time() > deadline:
                break
        self.assertIn(("echo", "delete_thing"), self.mgr.approved)


class CoordinatorTests(unittest.TestCase):
    def test_start_and_wait(self):
        mock = MockMCPServer("echo", ECHO_TOOLS)
        mgr = FakeManager([mock])
        coord = RunCoordinator(mgr, llm_factory=lambda: None)
        run_id = coord.start("echo the text hello", conversation_id="c1")
        deadline = time.time() + 10
        while time.time() < deadline:
            if coord.get(run_id) is None or \
                    (coord.get(run_id) and coord.get(run_id).report):
                break
            time.sleep(0.05)
        run = coord.get(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run.report.get("status"), "completed")

    def test_cancel_unknown_run(self):
        mock = MockMCPServer("echo", ECHO_TOOLS)
        coord = RunCoordinator(FakeManager([mock]),
                               llm_factory=lambda: None)
        self.assertFalse(coord.cancel("ghost"))


class ReportSafetyTests(unittest.TestCase):
    def test_report_has_no_raw_llm_output(self):
        mock = MockMCPServer("echo", ECHO_TOOLS)
        mgr = FakeManager([mock])
        def llm(prompt):
            return ('{"steps": [{"name": "S", "server": "echo",'
                    ' "tool": "echo", "args": {"text": "x"}}],'
                    ' "thinking": "SECRET chain of thought"}')
        report = AgentRun("p1", "echo x", mgr, llm=llm).run()
        blob = repr(report)
        self.assertNotIn("SECRET", blob, "raw model output must not leak "
                        "into the public report")


if __name__ == "__main__":
    unittest.main(verbosity=2)
