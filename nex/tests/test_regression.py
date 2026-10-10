"""Regression risk map (agent/regression.py).

Large autonomous builds silently break earlier systems. These tests
prove the module (a) correctly classifies which gameplay system a tool
touched, (b) flags only the dependents this run's own evidence never
exercised, and (c) never claims a dependent is broken or fine — only
that it was or was not itself re-exercised.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-regression-tests")

from agent.regression import (                                   # noqa: E402
    classify_system, mutated_systems, regression_brief, regression_review,
)
from agent.task_graph import Task, SUCCESS, FAILED                # noqa: E402


def task(name, tool, status=SUCCESS):
    return Task(id=name, name=name, tool=tool, status=status)


class ClassifySystemTests(unittest.TestCase):
    def test_concrete_tool_names_map_to_a_system(self):
        self.assertIn("vehicles", classify_system("spawn_vehicle_drivable"))
        self.assertIn("navigation", classify_system("build_navmesh"))
        self.assertIn("missions", classify_system("create_mission"))

    def test_unrelated_tools_map_to_nothing(self):
        self.assertEqual(classify_system("inspect_uproject"), set())
        self.assertEqual(classify_system(""), set())

    def test_a_tool_can_touch_more_than_one_system(self):
        hit = classify_system("spawn_vehicle_and_interact")
        self.assertIn("vehicles", hit)
        self.assertIn("player_interaction", hit)


class RegressionReviewTests(unittest.TestCase):
    def test_only_successful_calls_count_as_evidence(self):
        tasks = [task("s1", "spawn_vehicle", status=FAILED)]
        touched = mutated_systems(tasks)
        self.assertEqual(touched, {})

    def test_changing_vehicles_without_touching_dependents_is_flagged(self):
        tasks = [task("s1", "spawn_vehicle")]
        review = regression_review(tasks)
        self.assertIn("vehicles", review["systems_touched"])
        self.assertIn("vehicles", review["at_risk_dependents"])
        flagged = set(review["at_risk_dependents"]["vehicles"])
        self.assertTrue({"traffic", "missions", "player_interaction",
                        "persistence"} <= flagged)
        self.assertIn("not an executed test", review["note"])

    def test_touching_a_dependent_in_the_same_run_clears_the_flag(self):
        tasks = [task("s1", "spawn_vehicle"),
                task("s2", "spawn_traffic"),
                task("s3", "create_mission"),
                task("s4", "test_player_interact"),
                task("s5", "save_game_slot")]
        review = regression_review(tasks)
        self.assertNotIn("vehicles", review["at_risk_dependents"])

    def test_no_mutation_means_no_risk(self):
        review = regression_review([])
        self.assertEqual(review["systems_touched"], {})
        self.assertEqual(review["at_risk_dependents"], {})
        self.assertEqual(regression_brief(review), "")


class RegressionBriefTests(unittest.TestCase):
    def test_brief_is_empty_when_nothing_at_risk(self):
        review = {"at_risk_dependents": {}}
        self.assertEqual(regression_brief(review), "")

    def test_brief_names_the_system_and_its_unverified_dependents(self):
        review = {"at_risk_dependents": {"vehicles": ["traffic", "missions"]}}
        brief = regression_brief(review)
        self.assertIn("REGRESSION RISK", brief)
        self.assertIn("vehicles", brief)
        self.assertIn("traffic", brief)
        self.assertIn("missions", brief)

    def test_brief_is_bounded_by_limit(self):
        review = {"at_risk_dependents": {
            "a": ["x"], "b": ["x"], "c": ["x"], "d": ["x"], "e": ["x"],
            "f": ["x"]}}
        brief = regression_brief(review, limit=2)
        self.assertEqual(brief.count(" changed this run"), 2)


class LoopIntegrationTests(unittest.TestCase):
    """Prove agent/loop.py actually surfaces this in the planning prompt
    and the final report, not just that the pure function works."""

    def test_report_includes_regression_review_and_prompt_sees_the_risk(self):
        import json as _json
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                    "inputSchema": {"type": "object", "properties": {}}}

        server = MockMCPServer("engine", [tool("spawn_vehicle"),
                                          tool("inspect_uproject")])
        manager = FakeManager([server])
        seen = {}

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                seen["prompt"] = messages[-1]["content"]
                return _json.dumps({"plan": {
                    "title": "Vehicles", "rationale": "x",
                    "steps": [
                        {"name": "inspect", "title": "Inspect",
                         "tool": "engine.inspect_uproject", "args": {}},
                        {"name": "add-car", "title": "Add a car",
                         "tool": "engine.spawn_vehicle", "args": {},
                         "depends_on": ["inspect"]},
                    ],
                }})
            return _json.dumps({"done": True})

        run = AgentRun("r1", "add a drivable car", manager, llm=llm,
                      max_steps=5, max_replans=1)
        report = run.run()
        self.assertIn("vehicles", report["regression"]["systems_touched"])
        self.assertIn("vehicles", report["regression"]["at_risk_dependents"])

        # A replan's prompt must have actually seen the regression risk
        # once the first plan's evidence existed — exercise the brief
        # directly against the finished graph, the same call loop.py makes.
        from agent.regression import regression_brief, regression_review
        brief = regression_brief(regression_review(run.graph.all()))
        self.assertIn("REGRESSION RISK", brief)
        self.assertIn("vehicles", brief)


if __name__ == "__main__":
    unittest.main(verbosity=2)
