"""Game-production quality protocol and bounded polish pass tests."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-quality-tests")

from agent.loop import AgentRun                                  # noqa: E402
from agent.mock_mcp import MockMCPServer                         # noqa: E402
from agent.quality import (                                      # noqa: E402
    assess, gate_catalog, is_game_production_goal, planning_brief,
    profile_for_goal,
)
from agent.task_graph import Task, SUCCESS                       # noqa: E402
from test_agent_loop import FakeManager                          # noqa: E402


def tool(name, description=""):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": {}}}


GAME_TOOLS = [
    tool("inspect_project", "Inspect the existing game project and scene."),
    tool("create_level", "Create and integrate a playable level."),
    tool("build_game", "Build the game project."),
    tool("run_game", "Run a real game session for playtesting."),
    tool("capture_frame", "Capture a viewport screenshot."),
    tool("analyze_screenshot", "Review a screenshot for visual defects."),
    tool("inspect_logs", "Read runtime logs and errors."),
    tool("verify_game", "Verify gameplay acceptance criteria."),
    tool("get_performance_metrics", "Read FPS and frame-time telemetry."),
]


class IntentTests(unittest.TestCase):
    def test_authoring_goal_activates_protocol(self):
        self.assertTrue(is_game_production_goal(
            "Build a polished boss level for my game"))
        p = profile_for_goal("Build a polished boss level for my game")
        self.assertEqual(p.tier, "flagship")
        self.assertIn("performance", p.required_gates)

    def test_unreal_and_roblox_authoring_terms_activate_protocol(self):
        self.assertTrue(is_game_production_goal(
            "Compile and fix this Blueprint"))
        self.assertTrue(is_game_production_goal(
            "Refactor this Luau RemoteEvent handler"))

    def test_discussion_or_runtime_request_does_not_overreach(self):
        self.assertFalse(is_game_production_goal("What makes a game fun?"))
        self.assertFalse(is_game_production_goal("Run the game"))
        self.assertFalse(profile_for_goal("Show me a game").active)


class CatalogAndScoreTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockMCPServer("engine", GAME_TOOLS)
        self.mgr = FakeManager([self.mock])
        self.registry = self.mgr.registry()
        self.profile = profile_for_goal("Create an AAA-quality game level")

    def test_live_catalog_maps_to_distinct_evidence_gates(self):
        catalog = gate_catalog(self.registry)
        for gate in self.profile.required_gates:
            self.assertTrue(catalog[gate], gate + " should be available")
        self.assertEqual(
            [t.name for t in catalog["visual"]], ["capture_frame"])
        self.assertEqual(
            [t.name for t in catalog["visual_review"]],
            ["analyze_screenshot"])
        self.assertEqual(
            [t.name for t in catalog["performance"]],
            ["get_performance_metrics"])

    def test_planning_contract_names_real_tools_and_missing_capabilities(self):
        brief = planning_brief(self.profile, self.registry)
        self.assertIn("GAME PRODUCTION QUALITY CONTRACT", brief)
        self.assertIn("engine.capture_frame", brief)
        self.assertIn("literal AAA quality", brief)

        sparse = FakeManager([
            MockMCPServer("engine", [tool("create_level")])
        ]).registry()
        sparse_brief = planning_brief(self.profile, sparse)
        self.assertIn("UNAVAILABLE", sparse_brief)
        self.assertNotIn("engine.capture_frame", sparse_brief)

    def test_playtest_gate_adds_concrete_objective_decomposition(self):
        # A planner told to just "playtest it" tends to launch the game
        # and call that proof. The contract must spell out what a real
        # playtest objective actually requires.
        self.assertIn("playtest", self.profile.required_gates)
        brief = planning_brief(self.profile, self.registry)
        self.assertIn("approach the specific system/area under test", brief)
        self.assertIn("verify the resulting state actually changed", brief)
        self.assertIn("deliberately try a failure case", brief)
        self.assertIn("inspect logs/console output", brief)

        import dataclasses
        base = profile_for_goal("Fix a visual bug in this level")
        no_playtest = dataclasses.replace(
            base, required_gates=tuple(
                g for g in base.required_gates if g != "playtest"))
        quiet_brief = planning_brief(no_playtest, self.registry)
        self.assertNotIn("deliberately try a failure case", quiet_brief)

    def test_only_successful_tool_calls_count_as_evidence(self):
        tasks = []
        for i, spec in enumerate(GAME_TOOLS):
            tasks.append(Task(
                id="s%d" % i, name=spec["name"], server="engine",
                tool=spec["name"], status=SUCCESS,
                result={"ok": True, "artifact": spec["name"]}))
        scorecard = assess(self.profile, self.registry, tasks)
        self.assertTrue(scorecard["passed"])
        self.assertEqual(scorecard["score"], 100)

        tasks[-1].status = "failed"
        scorecard = assess(self.profile, self.registry, tasks)
        self.assertFalse(scorecard["passed"])
        self.assertIn("performance", scorecard["missing"])
        self.assertEqual(scorecard["correctable"], ["performance"])

    def test_explicit_negative_verdict_is_not_positive_evidence(self):
        task = Task(id="verify", name="Verify game", server="engine",
                    tool="verify_game", status=SUCCESS,
                    result={"result": {"playable": False}})
        scorecard = assess(self.profile, self.registry, [task])
        self.assertIn("verification", scorecard["missing"])
        self.assertEqual(len(scorecard["rejected_evidence"]), 1)
        self.assertEqual(scorecard["rejected_evidence"][0]["tool"],
                         "engine.verify_game")


class LoopQualityTests(unittest.TestCase):
    def test_missing_engine_evidence_makes_aaa_result_partial(self):
        mock = MockMCPServer("engine", [tool("create_level")])
        mgr = FakeManager([mock])

        def llm(messages):
            system = messages[0]["content"]
            if "planning mind" in system:
                return json.dumps({"steps": [{
                    "name": "implement", "title": "Implement the level",
                    "tool": "engine.create_level", "args": {}
                }]})
            return "Work completed with explicit limitations."

        report = AgentRun(
            "quality-incomplete", "Create an AAA-quality game level",
            mgr, llm=llm).run()
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["quality"]["passed"])
        self.assertIn("playtest", report["quality"]["unavailable"])
        self.assertEqual(report["quality_passes"], 0)

    def test_one_bounded_pass_fills_omitted_available_gate(self):
        mock = MockMCPServer("engine", GAME_TOOLS)
        mgr = FakeManager([mock])
        planner_calls = []
        initial_names = [t["name"] for t in GAME_TOOLS
                         if t["name"] != "capture_frame"]

        def plan_for(names):
            steps = []
            previous = None
            for name in names:
                step = {"name": name, "title": name.replace("_", " ").title(),
                        "tool": "engine." + name, "args": {}}
                if previous:
                    step["depends_on"] = [previous]
                steps.append(step)
                previous = name
            return json.dumps({"plan": {"title": "Vertical slice",
                                        "rationale": "Evidence first",
                                        "steps": steps}})

        def llm(messages):
            system = messages[0]["content"]
            if "planning mind" in system:
                user = messages[-1]["content"]
                planner_calls.append(user)
                if "Previous attempt" in user:
                    corrective = json.loads(plan_for(["capture_frame"]))
                    step = corrective["plan"]["steps"][0]
                    step["depends_on"] = ["create_level"]
                    step["args"] = {"project": "$create_level.id"}
                    return json.dumps(corrective)
                initial = json.loads(plan_for(initial_names))
                verify = next(s for s in initial["plan"]["steps"]
                              if s["name"] == "verify_game")
                verify["args"] = {"build": "$build_game.id"}
                verify["depends_on"] = list(dict.fromkeys(
                    (verify.get("depends_on") or []) + ["build_game"]))
                return json.dumps(initial)
            return "Production evidence was checked."

        events = []
        report = AgentRun(
            "quality-pass", "Create a polished AAA-quality game level",
            mgr, llm=llm, bus=events.append,
            max_quality_passes=1).run()

        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["quality"]["passed"])
        self.assertEqual(report["quality"]["score"], 100)
        self.assertEqual(report["quality_passes"], 1)
        self.assertEqual(report["replans"], 1)
        self.assertEqual(len(planner_calls), 2)
        self.assertIn("GAME PRODUCTION QUALITY CONTRACT", planner_calls[0])
        self.assertIn("do NOT repeat", planner_calls[1])
        self.assertEqual(
            [name for _, name, _ in mgr.calls].count("capture_frame"), 1)
        capture_args = [args for _, name, args in mgr.calls
                        if name == "capture_frame"][0]
        self.assertEqual(capture_args["project"], "create_level_1")
        self.assertTrue(any(e.get("type") == "run.quality" for e in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
