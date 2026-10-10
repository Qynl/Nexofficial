"""Runtime playtesting (agent/playtest.py).

Proves: scenarios only get proposed for systems this run actually
touched; resolution never claims a step is runnable without a real
connected tool; an unresolved step is reported by name, never hidden or
faked; evidence only counts a step confirmed when a successful call to
the resolved tool actually happened, in order.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-playtest-tests")

from agent.playtest import (                                      # noqa: E402
    SCENARIOS, UNAVAILABLE, playtest_review, relevant_scenarios,
    resolve_scenario, scenario_evidence,
)
from agent.task_graph import Task, SUCCESS, FAILED                # noqa: E402


def task(name, tool, status=SUCCESS):
    return Task(id=name, name=name, tool=tool, status=status)


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeRegistry:
    def __init__(self, tool_names):
        self._tools = [FakeTool(n) for n in tool_names]

    def all_tools(self):
        return self._tools


VEHICLE = next(s for s in SCENARIOS if s.id == "vehicle_roundtrip")


class RelevantScenariosTests(unittest.TestCase):
    def test_no_tasks_proposes_nothing(self):
        self.assertEqual(relevant_scenarios([]), [])

    def test_only_systems_actually_touched_are_proposed(self):
        scenarios = relevant_scenarios([task("t1", "spawn_vehicle")])
        ids = {s.id for s in scenarios}
        self.assertIn("vehicle_roundtrip", ids)
        self.assertNotIn("npc_encounter", ids)

    def test_a_failed_call_does_not_make_a_system_relevant(self):
        scenarios = relevant_scenarios(
            [task("t1", "spawn_vehicle", status=FAILED)])
        self.assertEqual(scenarios, [])


class ResolveScenarioTests(unittest.TestCase):
    def test_fully_connected_registry_resolves_every_step(self):
        registry = FakeRegistry([
            "spawn_vehicle", "possess_pawn", "set_vehicle_throttle",
            "exit_vehicle"])
        res = resolve_scenario(VEHICLE, registry)
        self.assertTrue(res["runnable"])
        self.assertEqual(res["unavailable_actions"], [])
        self.assertTrue(all(s["available"] for s in res["steps"]))

    def test_a_missing_action_is_reported_honestly_not_hidden(self):
        registry = FakeRegistry(["spawn_vehicle", "possess_pawn"])
        res = resolve_scenario(VEHICLE, registry)
        self.assertFalse(res["runnable"])
        self.assertIn("drive", res["unavailable_actions"])
        self.assertIn("exit", res["unavailable_actions"])
        self.assertIn(UNAVAILABLE, res["note"])

    def test_an_empty_registry_resolves_nothing(self):
        res = resolve_scenario(VEHICLE, FakeRegistry([]))
        self.assertFalse(res["runnable"])
        self.assertEqual(len(res["unavailable_actions"]), len(VEHICLE.steps))

    def test_none_registry_is_handled_without_crashing(self):
        res = resolve_scenario(VEHICLE, None)
        self.assertFalse(res["runnable"])


class ScenarioEvidenceTests(unittest.TestCase):
    def test_not_runnable_when_a_step_cannot_resolve(self):
        resolution = resolve_scenario(VEHICLE, FakeRegistry(["spawn_vehicle"]))
        evidence = scenario_evidence(resolution, [])
        self.assertEqual(evidence["verdict"], "not_runnable")

    def test_not_attempted_when_resolvable_but_never_called(self):
        registry = FakeRegistry([
            "spawn_vehicle", "possess_pawn", "set_vehicle_throttle",
            "exit_vehicle"])
        resolution = resolve_scenario(VEHICLE, registry)
        evidence = scenario_evidence(resolution, [
            task("t1", "spawn_vehicle"),   # called but not the whole chain
        ])
        self.assertIn(evidence["verdict"],
                      ("partially_confirmed", "not_attempted"))
        self.assertGreaterEqual(evidence["steps_confirmed"], 1)

    def test_fully_confirmed_when_the_whole_sequence_ran_in_order(self):
        registry = FakeRegistry([
            "spawn_vehicle", "possess_pawn", "set_vehicle_throttle",
            "exit_vehicle"])
        resolution = resolve_scenario(VEHICLE, registry)
        tasks = [
            task("t1", "spawn_vehicle"),
            task("t2", "possess_pawn"),
            task("t3", "set_vehicle_throttle"),
            task("t4", "exit_vehicle"),
        ]
        evidence = scenario_evidence(resolution, tasks)
        self.assertEqual(evidence["verdict"], "confirmed")
        self.assertEqual(evidence["steps_confirmed"], 4)

    def test_out_of_order_calls_are_not_counted_as_the_full_sequence(self):
        registry = FakeRegistry([
            "spawn_vehicle", "possess_pawn", "set_vehicle_throttle",
            "exit_vehicle"])
        resolution = resolve_scenario(VEHICLE, registry)
        # exit called before spawn/possess/drive — this is NOT a correct
        # roundtrip even though every tool was technically called once.
        tasks = [
            task("t1", "exit_vehicle"),
            task("t2", "spawn_vehicle"),
            task("t3", "possess_pawn"),
            task("t4", "set_vehicle_throttle"),
        ]
        evidence = scenario_evidence(resolution, tasks)
        self.assertNotEqual(evidence["verdict"], "confirmed")

    def test_a_failed_call_is_never_counted_as_confirming_evidence(self):
        registry = FakeRegistry(["spawn_vehicle"])
        scenario = next(s for s in SCENARIOS if s.id == "item_pickup")
        resolution = resolve_scenario(scenario, FakeRegistry(["spawn_item"]))
        evidence = scenario_evidence(
            resolution, [task("t1", "spawn_item", status=FAILED)])
        self.assertEqual(evidence["steps_confirmed"], 0)


class PlaytestReviewTests(unittest.TestCase):
    def test_review_is_empty_when_nothing_relevant_was_touched(self):
        review = playtest_review([], FakeRegistry([]))
        self.assertEqual(review["scenarios_checked"], [])

    def test_review_covers_a_touched_system_end_to_end(self):
        tasks = [
            task("t1", "spawn_vehicle"),
            task("t2", "possess_pawn"),
            task("t3", "set_vehicle_throttle"),
            task("t4", "exit_vehicle"),
        ]
        registry = FakeRegistry([
            "spawn_vehicle", "possess_pawn", "set_vehicle_throttle",
            "exit_vehicle"])
        review = playtest_review(tasks, registry)
        # possess_pawn also matches "player_interaction" (classify_system's
        # own "possess" keyword), so BOTH systems are legitimately touched
        # here — the vehicle roundtrip is what this test actually cares
        # about confirming end to end.
        by_id = {c["scenario"]: c for c in review["scenarios_checked"]}
        self.assertIn("vehicle_roundtrip", by_id)
        self.assertEqual(by_id["vehicle_roundtrip"]["verdict"], "confirmed")

    def test_review_is_honest_about_an_unresolvable_system(self):
        review = playtest_review(
            [task("t1", "spawn_vehicle")], FakeRegistry(["spawn_vehicle"]))
        self.assertEqual(
            review["scenarios_checked"][0]["verdict"], "not_runnable")


class LoopIntegrationTests(unittest.TestCase):
    def test_a_real_run_produces_a_playtest_report(self):
        import json as _json
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                    "inputSchema": {"type": "object", "properties": {}}}

        server = MockMCPServer("engine", [tool("spawn_vehicle")])
        manager = FakeManager([server])

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return _json.dumps({"plan": {
                    "title": "Vehicles", "rationale": "x",
                    "steps": [{"name": "add-car", "title": "Add a car",
                              "tool": "engine.spawn_vehicle", "args": {}}],
                }})
            return _json.dumps({"done": True})

        run = AgentRun("r1", "add a drivable car", manager, llm=llm,
                      max_steps=3, max_replans=0)
        report = run.run()
        self.assertIn("playtest", report)
        checked = report["playtest"]["scenarios_checked"]
        self.assertTrue(any(c["scenario"] == "vehicle_roundtrip"
                           for c in checked))


if __name__ == "__main__":
    unittest.main(verbosity=2)
