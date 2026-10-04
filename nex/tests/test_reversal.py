"""Compensating actions: coverage must be honest, never optimistic."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
sys.path.insert(0, HERE)
os.environ.setdefault("NEX_HOME", "/tmp/nex-reversal-tests")

from agent.mock_mcp import MockMCPServer                                # noqa: E402
from agent.reversal import (                                           # noqa: E402
    find_inverse_tool, identity_from_result, plan_reversal,
)
from agent.task_graph import FAILED, SUCCESS, Task                      # noqa: E402
from test_agent_loop import FakeManager                                 # noqa: E402

_FAILED = []


def tool(name, *, properties=None, required=None, description=""):
    schema = {"type": "object", "properties": properties or {},
              "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": description, "inputSchema": schema}


ENGINE_TOOLS = [
    tool("inspect_project"),
    tool("create_level", properties={"name": {"type": "string"}},
         required=["name"]),
    tool("delete_level", properties={"id": {"type": "string"}},
         required=["id"]),
    tool("spawn_actor", properties={"kind": {"type": "string"}}),
    tool("destroy_actor", properties={"id": {"type": "string"}},
         required=["id"]),
    tool("create_material", properties={"name": {"type": "string"}}),
    tool("build_game"),
    tool("publish_game"),
]


def ok(result_payload):
    return {"content": [{"type": "text", "text": "done"}],
            "structuredContent": result_payload}


def step(tid, tool_name, result, args=None, status=SUCCESS, server="engine"):
    return Task(id=tid, name=tool_name, server=server, tool=tool_name,
                args=args or {}, status=status, result=result)


class IdentityTests(unittest.TestCase):
    def test_identity_comes_from_structured_payload(self):
        self.assertEqual(identity_from_result(ok({"id": "L-1"})),
                         ("id", "L-1"))
        self.assertEqual(
            identity_from_result(ok({"data": {"asset_path": "/Game/A"}})),
            ("asset_path", "/Game/A"))

    def test_prose_is_never_mined_for_an_identifier(self):
        prose = {"content": [{"type": "text",
                              "text": "created level id=L-9 successfully"}]}
        self.assertIsNone(identity_from_result(prose))

    def test_missing_or_empty_identity_is_none(self):
        self.assertIsNone(identity_from_result(ok({"ok": True})))
        self.assertIsNone(identity_from_result(ok({"id": "   "})))
        self.assertIsNone(identity_from_result(None))


class InverseToolTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeManager(
            [MockMCPServer("engine", ENGINE_TOOLS)]).registry()

    def test_matching_noun_is_required(self):
        found = find_inverse_tool(self.registry, "engine", "create_level")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "delete_level")

    def test_no_inverse_when_the_noun_has_none(self):
        # create_material has no delete_material in the catalog.
        self.assertIsNone(
            find_inverse_tool(self.registry, "engine", "create_material"))

    def test_verbs_without_a_true_opposite_have_no_inverse(self):
        for name in ("build_game", "publish_game", "inspect_project"):
            self.assertIsNone(
                find_inverse_tool(self.registry, "engine", name), name)

    def test_inverse_must_live_on_the_same_server(self):
        registry = FakeManager([
            MockMCPServer("engine", [ENGINE_TOOLS[1]]),
            MockMCPServer("other", [ENGINE_TOOLS[2]]),
        ]).registry()
        self.assertIsNone(
            find_inverse_tool(registry, "engine", "create_level"))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager([MockMCPServer("engine", ENGINE_TOOLS)])
        self.registry = self.manager.registry()

    def test_creations_become_lifo_compensating_steps(self):
        tasks = [
            step("t1", "create_level", ok({"id": "L-1"}), {"name": "a"}),
            step("t2", "spawn_actor", ok({"id": "A-7"}), {"kind": "npc"}),
        ]
        plan = plan_reversal(tasks, self.registry)
        self.assertTrue(plan["available"])
        self.assertEqual(plan["reversible"], 2)
        self.assertEqual(plan["coverage_pct"], 100)
        # Newest mutation is compensated first.
        self.assertEqual(plan["steps"][0]["tool"], "destroy_actor")
        self.assertEqual(plan["steps"][0]["args"], {"id": "A-7"})
        self.assertEqual(plan["steps"][1]["tool"], "delete_level")
        self.assertEqual(plan["steps"][1]["args"], {"id": "L-1"})

    def test_reads_and_failures_are_not_mutations(self):
        tasks = [
            step("t1", "inspect_project", ok({"actors": 3})),
            step("t2", "create_level", ok({"id": "L-2"}), status=FAILED),
        ]
        plan = plan_reversal(tasks, self.registry)
        self.assertEqual(plan["mutations"], 0)
        self.assertFalse(plan["available"])
        self.assertIn("no mutations", plan["note"])

    def test_build_and_publish_are_reported_irreversible(self):
        tasks = [step("t1", "build_game", ok({"build_id": "B-1"})),
                 step("t2", "publish_game", ok({"url": "https://x"}))]
        plan = plan_reversal(tasks, self.registry)
        self.assertEqual(plan["reversible"], 0)
        self.assertEqual(plan["irreversible"], 2)
        reasons = " ".join(b["reason"] for b in plan["blocked"])
        self.assertIn("no inverse verb", reasons)
        self.assertIn("cannot be recalled", reasons)

    def test_creation_without_an_identifier_is_blocked_not_guessed(self):
        tasks = [step("t1", "create_level", ok({"ok": True}), {"name": "a"})]
        plan = plan_reversal(tasks, self.registry)
        self.assertEqual(plan["reversible"], 0)
        self.assertIn("no structured identifier",
                      plan["blocked"][0]["reason"])

    def test_missing_inverse_tool_is_blocked_not_guessed(self):
        tasks = [step("t1", "create_material", ok({"id": "M-1"}))]
        plan = plan_reversal(tasks, self.registry)
        self.assertEqual(plan["reversible"], 0)
        self.assertIn("no inverse tool", plan["blocked"][0]["reason"])

    def test_coverage_is_reported_when_only_some_steps_reverse(self):
        tasks = [step("t1", "create_level", ok({"id": "L-1"}), {"name": "a"}),
                 step("t2", "build_game", ok({"build_id": "B-1"}))]
        plan = plan_reversal(tasks, self.registry)
        self.assertEqual(plan["reversible"], 1)
        self.assertEqual(plan["irreversible"], 1)
        self.assertEqual(plan["coverage_pct"], 50)
        self.assertIn("1 of 2", plan["note"])

    def test_plan_never_claims_to_be_a_rollback(self):
        tasks = [step("t1", "create_level", ok({"id": "L-1"}), {"name": "a"})]
        note = plan_reversal(tasks, self.registry)["note"].lower()
        self.assertIn("compensating", note)
        self.assertIn("not a transaction rollback", note)


class ExecutionTests(unittest.TestCase):
    """Reverting is a normal gated mutation path, not a back door."""

    def _coordinator(self, manager):
        from agent.loop import RunCoordinator
        return RunCoordinator(manager, lambda: None)

    def test_revert_executes_compensations_through_the_manager(self):
        manager = FakeManager([MockMCPServer("engine", ENGINE_TOOLS)])
        coord = self._coordinator(manager)
        run_id = "run-x"

        class _Stub:
            status = "completed"
            report = None

            def __init__(self, registry_tasks):
                self._tasks = registry_tasks

            def _reversal_plan(self):
                return plan_reversal(self._tasks, manager.registry())

        tasks = [step("t1", "create_level", ok({"id": "L-1"}), {"name": "a"})]
        coord._runs[run_id] = _Stub(tasks)
        manager.approve_tool("engine", "delete_level")
        out = coord.revert(run_id)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["applied"]), 1)
        called = [c for c in manager.calls if c[1] == "delete_level"]
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0][2], {"id": "L-1"})

    def test_unknown_run_is_not_revertible(self):
        coord = self._coordinator(
            FakeManager([MockMCPServer("engine", ENGINE_TOOLS)]))
        self.assertIsNone(coord.revert("nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    print("\nAll reversal tests passed.")
