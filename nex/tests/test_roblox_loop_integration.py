"""Proves agent/loop.py actually wires the roblox/* modules end to end,
gated correctly by real engine detection — not just that the pure
roblox/* functions work in isolation.
"""
import json as _json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-loop-tests")

from agent.loop import AgentRun                                    # noqa: E402
from agent.mock_mcp import MockMCPServer                           # noqa: E402
from test_agent_loop import FakeManager                            # noqa: E402


def tool(name):
    return {"name": name, "description": "",
           "inputSchema": {"type": "object", "properties": {}}}


class RobloxDetectionGatingTests(unittest.TestCase):
    def test_a_roblox_flavored_project_gets_the_full_roblox_report(self):
        server = MockMCPServer("roblox_studio", [
            tool("create_remote_event"), tool("datastore_set"),
            tool("play_solo")])

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return _json.dumps({"plan": {
                    "title": "Shop", "rationale": "x",
                    "steps": [{"name": "add-remote", "title": "Add a remote",
                              "tool": "roblox_studio.create_remote_event",
                              "args": {"name": "PurchaseItem",
                                      "class_name": "RemoteEvent",
                                      "parent": "ReplicatedStorage"}}],
                }})
            return _json.dumps({"done": True})

        run = AgentRun("r1", "add a RemoteEvent to my roblox studio game",
                       server and FakeManager([server]), llm=llm,
                       max_steps=3, max_replans=0)
        report = run.run()
        self.assertTrue(report["roblox"]["active"])
        self.assertIn("ReplicatedStorage.PurchaseItem",
                      report["roblox_project_model"]["nodes"])
        self.assertTrue(any(c["name"] == "PurchaseItem"
                           for c in report["roblox"]["remote_contracts"]))
        self.assertIn("networking", report["roblox"]["test_plan"]["systems"])

    def test_a_non_roblox_project_gets_an_honestly_inactive_report(self):
        server = MockMCPServer("unreal_editor", [tool("spawn_actor")])

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return _json.dumps({"plan": {
                    "title": "Car", "rationale": "x",
                    "steps": [{"name": "add-car", "title": "Add a car",
                              "tool": "unreal_editor.spawn_actor",
                              "args": {}}],
                }})
            return _json.dumps({"done": True})

        run = AgentRun("r1", "add a drivable car in unreal engine",
                       FakeManager([server]), llm=llm,
                       max_steps=3, max_replans=0)
        report = run.run()
        self.assertFalse(report["roblox"]["active"])
        self.assertIsNone(report["roblox_project_model"])
        self.assertIsNone(report["roblox_asset_model"])

    def test_roblox_project_model_persists_across_two_runs(self):
        server = MockMCPServer("roblox_studio", [tool("create_remote_event")])

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return _json.dumps({"plan": {
                    "title": "Shop", "rationale": "x",
                    "steps": [{"name": "add-remote", "title": "Add a remote",
                              "tool": "roblox_studio.create_remote_event",
                              "args": {"name": "PurchaseItem",
                                      "class_name": "RemoteEvent",
                                      "parent": "ReplicatedStorage"}}],
                }})
            return _json.dumps({"done": True})

        run1 = AgentRun("r1", "add a RemoteEvent to my roblox studio game",
                       FakeManager([server]), llm=llm,
                       max_steps=3, max_replans=0)
        report1 = run1.run()
        prior_model = report1["roblox_project_model"]
        self.assertIn("ReplicatedStorage.PurchaseItem", prior_model["nodes"])

        run2 = AgentRun("r2", "add another remote to my roblox studio game",
                       FakeManager([MockMCPServer(
                           "roblox_studio", [tool("create_remote_event")])]),
                       llm=llm, max_steps=3, max_replans=0,
                       roblox_project_model=prior_model)
        report2 = run2.run()
        # The instance declared in run 1 is still known in run 2's model.
        key = "ReplicatedStorage.PurchaseItem"
        self.assertIn(key, report2["roblox_project_model"]["nodes"])
        self.assertEqual(
            report2["roblox_project_model"]["nodes"][key]["mentions"], 2)


class RobloxTestPlanBriefTests(unittest.TestCase):
    """Proves an open playtest/persistence stage gate turns into concrete,
    catalog-grounded candidate plan steps in the planning brief itself —
    not only report-visible advisory data after the run already ended."""

    def _run(self, stage_index, mutating_tool="create_remote_event"):
        from agent.task_graph import Task, SUCCESS
        from agent.production import STAGES

        server = MockMCPServer("roblox_studio", [tool(mutating_tool)])
        run = AgentRun("r1", "x", FakeManager([server]), llm=None,
                      max_steps=1, max_replans=0)
        run._program_active = True
        run._program_stage = stage_index
        run._engine_targets = [{"id": "roblox_studio", "name": "Roblox"}]
        stage = STAGES[stage_index]
        run.graph.add(Task(
            id="t1", name="add remote", slug="add-remote",
            server="roblox_studio", tool=mutating_tool,
            status=SUCCESS, phase=stage.id,
            result={"ok": True, "evidence": "created"}))
        return run

    def test_vertical_slice_open_playtest_gate_surfaces_concrete_steps(self):
        run = self._run(2)  # vertical_slice requires gate:playtest
        brief = run._roblox_test_plan_brief(run.manager.registry())
        self.assertIn("gate:playtest", brief)
        self.assertIn("[playtest] purchase_flow:", brief)
        self.assertIn("expected:", brief)

    def test_foundation_open_persistence_gate_surfaces_concrete_steps(self):
        run = self._run(1)  # foundation requires discipline:data_progression
        brief = run._roblox_test_plan_brief(run.manager.registry())
        self.assertIn("discipline:data_progression", brief)

    def test_stage_with_no_relevant_gate_yields_no_brief(self):
        run = self._run(0)  # discovery only needs gate:inspection
        brief = run._roblox_test_plan_brief(run.manager.registry())
        self.assertEqual(brief, "")

    def test_non_roblox_target_yields_no_brief(self):
        run = self._run(2)
        run._engine_targets = [{"id": "unreal_5_8", "name": "Unreal"}]
        brief = run._roblox_test_plan_brief(run.manager.registry())
        self.assertEqual(brief, "")

    def test_non_program_run_yields_no_brief(self):
        run = self._run(2)
        run._program_active = False
        brief = run._roblox_test_plan_brief(run.manager.registry())
        self.assertEqual(brief, "")

    def test_no_touched_systems_yields_no_brief(self):
        from agent.task_graph import Task, SUCCESS
        from agent.production import STAGES
        server = MockMCPServer("roblox_studio", [tool("spawn_unrelated")])
        run = AgentRun("r1", "x", FakeManager([server]), llm=None,
                      max_steps=1, max_replans=0)
        run._program_active = True
        run._program_stage = 2
        run._engine_targets = [{"id": "roblox_studio", "name": "Roblox"}]
        stage = STAGES[2]
        run.graph.add(Task(
            id="t1", name="unrelated", slug="unrelated",
            server="roblox_studio", tool="spawn_unrelated",
            status=SUCCESS, phase=stage.id,
            result={"ok": True, "evidence": "x"}))
        brief = run._roblox_test_plan_brief(run.manager.registry())
        self.assertEqual(brief, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
