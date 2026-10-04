"""Large-scale game production program and MCP readiness tests."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-production-tests")

from agent.loop import AgentRun                                  # noqa: E402
from agent.mock_mcp import MockMCPServer                         # noqa: E402
from agent.model_planner import catalog_text                     # noqa: E402
from agent.production import (                                  # noqa: E402
    STAGES, is_large_game_goal, readiness, stage_brief, stage_evidence,
)
from agent.task_graph import Task                                # noqa: E402
from test_agent_loop import FakeManager                          # noqa: E402


def tool(name, description="", properties=None):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object",
                            "properties": properties or {}}}


STUDIO_TOOLS = [
    tool("inspect_project", "Inspect project scene assets and hierarchy."),
    tool("create_system", "Create gameplay state and save data system."),
    tool("create_level", "Create a world level and environment."),
    tool("create_gameplay", "Create player combat vehicle mission gameplay."),
    tool("create_ai_navigation", "Create NPC AI behavior traffic and nav."),
    tool("create_world", "Create terrain and districts."),
    tool("create_presentation_audio_animation", "Create lighting and presentation."),
    tool("create_ui_accessibility", "Create UI HUD and accessibility."),
    tool("create_save_progression", "Create save data and progression."),
    tool("update_game", "Update full-loop integration."),
    tool("build_game", "Build and compile the game."),
    tool("run_game", "Run the game in a real playtest session."),
    tool("capture_frame", "Capture a viewport screenshot."),
    tool("analyze_screenshot", "Analyze screenshot composition and defects."),
    tool("inspect_logs", "Inspect runtime logs and diagnostics."),
    tool("verify_game", "Verify functional acceptance tests.",
         {"build": {"type": "string"}}),
    tool("get_performance_metrics", "Get FPS frame-time memory telemetry."),
]


class ScopeAndReadinessTests(unittest.TestCase):
    def test_gta_goal_is_large_scale_but_focused_level_is_not(self):
        self.assertTrue(is_large_game_goal("Make GTA 7"))
        self.assertTrue(is_large_game_goal("Build a complete open-world game"))
        self.assertFalse(is_large_game_goal(
            "Create a polished boss level for my game"))

    def test_readiness_surfaces_mcp_breadth_and_hard_blockers(self):
        full = FakeManager([
            MockMCPServer("engine", STUDIO_TOOLS)
        ]).registry()
        report = readiness(full)
        self.assertGreaterEqual(report["score"], 70)
        self.assertTrue(report["ready_for_large_scope"])
        self.assertEqual(report["blockers"], [])

        sparse = FakeManager([
            MockMCPServer("engine", [tool("create_level")])
        ]).registry()
        report = readiness(sparse)
        self.assertFalse(report["ready_for_large_scope"])
        self.assertIn("playtest", report["blockers"])
        self.assertIn("visual", report["blockers"])

    def test_stage_brief_is_bounded_and_stage_specific(self):
        registry = FakeManager([
            MockMCPServer("engine", STUDIO_TOOLS)
        ]).registry()
        first = stage_brief("Make GTA 7", 0, registry)
        validation = stage_brief("Make GTA 7", 6, registry)
        self.assertIn("stage 1/8", first.lower())
        self.assertIn("Discovery", first)
        self.assertIn("Validation", validation)
        self.assertIn("3-8 concrete MCP steps", validation)
        self.assertIn("Readiness gaps:", validation)

    def test_one_broad_tool_cannot_certify_a_vertical_slice(self):
        broad = tool(
            "create_gameplay_ui_accessibility_presentation_run_playtest")
        registry = FakeManager([
            MockMCPServer("engine", [broad])
        ]).registry()
        task = Task(
            id="one", name="Do everything", server="engine",
            tool=broad["name"], status="success", result={"ok": True})
        review = stage_evidence(STAGES[2], registry, [task])
        self.assertFalse(review["passed"])
        self.assertIn("minimum_evidence_steps:4", review["missing"])
        self.assertEqual(review["evidence_steps"], 1)

    def test_planner_sees_declared_output_fields_for_dataflow(self):
        spec = tool("build_game", "Build game")
        spec["outputSchema"] = {
            "type": "object",
            "properties": {"build_id": {"type": "string"},
                           "warnings": {"type": "array"}},
        }
        registry = FakeManager([
            MockMCPServer("engine", [spec])
        ]).registry()
        text = catalog_text(registry, "build game")
        self.assertIn("returns: [build_id:string] [warnings:array]", text)


class ProgramLoopTests(unittest.TestCase):
    def test_large_goal_runs_all_stages_then_quality_review(self):
        mock = MockMCPServer("engine", STUDIO_TOOLS)
        mgr = FakeManager([mock])
        planner_stages = []

        stage_tools = {
            "Discovery & constraints": ["inspect_project"],
            "Production foundation": ["create_system",
                                      "create_save_progression"],
            "Playable vertical slice": [
                "create_level", "create_gameplay",
                "create_presentation_audio_animation",
                "create_ui_accessibility", "run_game"],
            "Scalable game systems": [
                "create_gameplay", "create_ai_navigation",
                "create_save_progression"],
            "World, content & presentation": [
                "create_world", "create_presentation_audio_animation",
                "create_ai_navigation"],
            "Full-loop integration": [
                "update_game", "create_gameplay", "create_ui_accessibility",
                "create_save_progression", "run_game"],
            "Validation & optimization": [
                "build_game", "run_game", "capture_frame",
                "analyze_screenshot", "inspect_logs", "verify_game",
                "get_performance_metrics"],
            "Evidence-driven polish": [
                "update_game", "run_game", "analyze_screenshot",
                "verify_game"],
        }

        def plan(names, prefix):
            steps = []
            previous = None
            for i, name in enumerate(names):
                slug = "%s-%s" % (prefix, name)
                args = {}
                if name == "verify_game":
                    args = {"build": "$validation-build_game.id"}
                step = {
                    "name": slug,
                    "title": name.replace("_", " ").title(),
                    "tool": "engine." + name,
                    "args": args,
                    "expect": "A structured positive result",
                }
                if previous:
                    step["depends_on"] = [previous]
                # verify references build directly as well as the prior chain.
                if name == "verify_game":
                    step["depends_on"] = list(dict.fromkeys(
                        (step.get("depends_on") or []) +
                        ["validation-build_game"]))
                steps.append(step)
                previous = slug
            return json.dumps({"plan": {
                "title": prefix.replace("-", " ").title(),
                "rationale": "Build and verify this production milestone.",
                "steps": steps,
            }})

        def llm(messages):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "planning mind" in system:
                for label, names in stage_tools.items():
                    if ("Current stage" in user and label in user):
                        prefix = label.split()[0].lower().replace("&", "and")
                        if label == "Validation & optimization":
                            prefix = "validation"
                        planner_stages.append(label)
                        return plan(names, prefix)
                return json.dumps({"plan": {"title": "No stage",
                                            "rationale": "missing stage",
                                            "steps": []}})
            if "progress evaluator" in system:
                return json.dumps({"done": False, "adjust": "none",
                                   "reason": "Continue current stage"})
            return "All production stages and evidence were reported honestly."

        events = []
        report = AgentRun(
            "studio-program", "Make GTA 7 as a complete open-world game",
            mgr, llm=llm, bus=events.append,
            max_steps=64, max_production_stages=len(STAGES)).run()

        self.assertEqual(report["status"], "completed")
        program = report["production_program"]
        self.assertTrue(program["complete"])
        self.assertEqual(len(program["completed_stages"]), len(STAGES))
        self.assertEqual(planner_stages, [s.label for s in STAGES])
        self.assertTrue(report["quality"]["passed"])
        self.assertEqual(report["quality"]["score"], 100)
        self.assertGreaterEqual(report["steps_total"], len(STAGES))
        self.assertTrue(any(e.get("type") == "run.program" for e in events))
        phase_values = {e["step"].get("phase") for e in events
                        if e.get("type") == "run.step"}
        self.assertTrue({s.id for s in STAGES}.issubset(phase_values))

    def test_missing_stage_evidence_triggers_bounded_correction(self):
        mock = MockMCPServer("engine", STUDIO_TOOLS)
        mgr = FakeManager([mock])
        plans = []

        def llm(messages):
            if "planning mind" in messages[0]["content"]:
                user = messages[-1]["content"]
                plans.append(user)
                name = "inspect_project" if "stage evidence is incomplete" in user \
                    else "create_level"
                return json.dumps({"plan": {"title": "Discovery",
                    "rationale": "Correct discovery evidence", "steps": [{
                        "name": "discovery-" + name,
                        "title": name.replace("_", " ").title(),
                        "tool": "engine." + name, "args": {}}]}})
            return "Discovery evidence reviewed."

        report = AgentRun(
            "studio-correct", "Make GTA 7", mgr, llm=llm,
            max_production_stages=1, max_steps=8).run()
        self.assertEqual(len(plans), 2)
        self.assertEqual(report["replans"], 1)
        review = report["production_program"]["stage_reviews"]["discovery"]
        self.assertTrue(review["passed"])
        self.assertEqual(report["production_program"]["completed_stages"],
                         ["discovery"])
        self.assertEqual(report["status"], "partial")

    def test_stage_cap_reports_partial_instead_of_fake_completion(self):
        mock = MockMCPServer("engine", STUDIO_TOOLS)
        mgr = FakeManager([mock])

        def llm(messages):
            if "planning mind" in messages[0]["content"]:
                user = messages[-1]["content"]
                if "Production foundation" in user:
                    return json.dumps({"plan": {"title": "Foundation",
                        "rationale": "Foundation evidence", "steps": [
                            {"name": "foundation-system",
                             "title": "Create system",
                             "tool": "engine.create_system", "args": {}},
                            {"name": "foundation-save",
                             "title": "Create save progression",
                             "tool": "engine.create_save_progression",
                             "args": {},
                             "depends_on": ["foundation-system"]}]}})
                return json.dumps({"plan": {"title": "Inspect",
                    "rationale": "Discovery only", "steps": [{
                        "name": "discovery-inspect", "title": "Inspect project",
                        "tool": "engine.inspect_project", "args": {}}]}})
            return "Discovery completed; later stages remain."

        report = AgentRun(
            "studio-cap", "Make GTA 7", mgr, llm=llm,
            max_production_stages=2, max_steps=8).run()
        # Each stage receives distinct namespaced work. Hitting an operator cap
        # never redefines two stages as a finished eight-stage product.
        self.assertFalse(report["production_program"]["complete"])
        self.assertEqual(report["production_program"]["stage_cap"], 2)
        self.assertEqual(report["production_program"]["stages_total"], 8)
        self.assertEqual(len(report["production_program"]["completed_stages"]), 2)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["quality"]["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
