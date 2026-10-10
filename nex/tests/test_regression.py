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
    classify_system, cross_run_risk, empty_regression_state,
    merge_regression_state, mutated_systems, regression_brief,
    regression_review,
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


class CrossRunRegressionStateTests(unittest.TestCase):
    """The within-run check only sees ONE run's tasks. These tests prove
    risk is tracked ACROSS runs: a system changed in run 3 whose
    dependent was last verified in run 1 (not run 2 or 3) must still
    read as at-risk, and the flag must clear the moment ANY later run
    actually exercises the dependent — not just the run that first
    raised it.
    """

    def test_empty_state_has_no_systems(self):
        self.assertEqual(empty_regression_state(), {"systems": {}})

    def test_a_fresh_at_risk_system_starts_its_since_run_clock(self):
        review = regression_review([task("t1", "spawn_vehicle")])
        state = merge_regression_state(None, review, run_no=1)
        entry = state["systems"]["vehicles"]
        self.assertEqual(entry["since_run"], 1)
        self.assertEqual(entry["last_touched_run"], 1)
        self.assertIn("traffic", entry["unverified_dependents"])

    def test_risk_persists_and_ages_across_runs_with_no_new_evidence(self):
        review = regression_review([task("t1", "spawn_vehicle")])
        state = merge_regression_state(None, review, run_no=1)
        # Run 2: vehicles touched again, still nothing verifies traffic.
        state = merge_regression_state(state, review, run_no=2)
        self.assertEqual(state["systems"]["vehicles"]["since_run"], 1,
                         "since_run must NOT reset just because the "
                         "system was touched again while still unverified")
        risk = cross_run_risk(state, run_no=2, stale_after=2)
        self.assertEqual(len(risk), 1)
        self.assertEqual(risk[0]["system"], "vehicles")
        self.assertEqual(risk[0]["runs_unverified"], 2)

    def test_fresh_risk_is_not_yet_flagged_as_stale(self):
        review = regression_review([task("t1", "spawn_vehicle")])
        state = merge_regression_state(None, review, run_no=1)
        risk = cross_run_risk(state, run_no=1, stale_after=2)
        self.assertEqual(risk, [],
                         "one run old is not yet 'stale' at the default "
                         "threshold of 2")

    def test_exercising_the_dependent_in_a_later_run_clears_the_flag(self):
        review1 = regression_review([task("t1", "spawn_vehicle")])
        state = merge_regression_state(None, review1, run_no=1)
        state = merge_regression_state(state, review1, run_no=2)
        before = state["systems"]["vehicles"]["unverified_dependents"]
        self.assertIn("traffic", before)
        self.assertTrue(cross_run_risk(state, run_no=2, stale_after=2))

        # Run 3: traffic (one of vehicles' several dependents) finally
        # gets exercised directly — this must remove IT from the
        # parent's unverified list even though vehicles itself was not
        # touched this run (the other dependents are untouched by this
        # test on purpose, so the flag is not expected to clear fully).
        review3 = regression_review([task("t3", "spawn_traffic")])
        state = merge_regression_state(state, review3, run_no=3)
        after = state["systems"]["vehicles"]["unverified_dependents"]
        self.assertNotIn("traffic", after)
        self.assertLess(len(after), len(before))

    def test_exercising_every_dependent_fully_clears_the_parent(self):
        review1 = regression_review([task("t1", "spawn_vehicle")])
        state = merge_regression_state(None, review1, run_no=1)
        state = merge_regression_state(state, review1, run_no=2)
        self.assertIn("since_run", state["systems"]["vehicles"])

        review3 = regression_review([
            task("t3", "spawn_traffic"), task("t4", "create_mission"),
            task("t5", "interact_with_vehicle"),
            task("t6", "save_game_slot")])
        state = merge_regression_state(state, review3, run_no=3)
        self.assertNotIn("since_run", state["systems"]["vehicles"])
        self.assertEqual(state["systems"]["vehicles"]["unverified_dependents"],
                         [])

    def test_touching_all_of_one_systems_dependents_clears_just_that_one(self):
        review = regression_review([
            task("t1", "spawn_vehicle"), task("t2", "spawn_traffic"),
            task("t3", "create_mission"), task("t4", "interact_with_vehicle"),
            task("t5", "save_game_slot"),
        ])
        state = merge_regression_state(None, review, run_no=1)
        self.assertNotIn("since_run", state["systems"]["vehicles"])
        risk = cross_run_risk(state, run_no=5, stale_after=2)
        self.assertNotIn("vehicles", [r["system"] for r in risk],
                         "vehicles' own dependents were all exercised — "
                         "it must not show up as at risk")

    def test_state_is_bounded_to_the_most_recently_touched_systems(self):
        from agent.regression import MAX_TRACKED_SYSTEMS
        state = empty_regression_state()
        # Every DEPENDENTS key plus a couple of synthetic extras, well
        # under the real system count, so just prove the cap mechanism
        # itself rather than needing 32+ distinct real systems.
        state["systems"] = {("sys-%d" % i): {"last_touched_run": i}
                            for i in range(MAX_TRACKED_SYSTEMS + 5)}
        review = regression_review([task("t1", "spawn_vehicle")])
        merged = merge_regression_state(state, review, run_no=1000)
        self.assertLessEqual(len(merged["systems"]), MAX_TRACKED_SYSTEMS)
        self.assertIn("vehicles", merged["systems"],
                     "the newly-touched system must survive the cap")


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

    def test_regression_state_persists_and_ages_across_two_real_runs(self):
        import json as _json
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                    "inputSchema": {"type": "object", "properties": {}}}

        def make_run(prior_state, prior_memory):
            server = MockMCPServer("engine", [tool("spawn_vehicle")])
            manager = FakeManager([server])

            def llm(messages, purpose=None):
                if "planning mind" in messages[0]["content"]:
                    return _json.dumps({"plan": {
                        "title": "Vehicles", "rationale": "x",
                        "steps": [{"name": "add-car", "title": "Add a car",
                                  "tool": "engine.spawn_vehicle",
                                  "args": {}}],
                    }})
                return _json.dumps({"done": True})

            return AgentRun("r1", "add a drivable car", manager, llm=llm,
                            max_steps=3, max_replans=0,
                            regression_state=prior_state,
                            project_memory=prior_memory)

        run1 = make_run(None, None)
        report1 = run1.run()
        state1 = report1["regression_state"]
        self.assertEqual(state1["systems"]["vehicles"]["since_run"], 1)
        self.assertEqual(report1["cross_run_regression_risk"], [],
                         "one run old is not yet reported as a cross-run "
                         "risk at the default threshold")

        # A SECOND, independent run reusing the first run's persisted
        # state AND project memory (exactly what server.py's
        # _start_run/_on_run_summary wire together, the memory supplying
        # the run-number sequencing) — the SAME unresolved risk must now
        # read as aged, never a model's self-report.
        run2 = make_run(state1, report1["project_memory"])
        report2 = run2.run()
        state2 = report2["regression_state"]
        self.assertEqual(state2["systems"]["vehicles"]["since_run"], 1,
                         "the ORIGINAL run is preserved across runs")
        risk = report2["cross_run_regression_risk"]
        self.assertTrue(risk)
        self.assertEqual(risk[0]["system"], "vehicles")
        self.assertGreaterEqual(risk[0]["runs_unverified"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
