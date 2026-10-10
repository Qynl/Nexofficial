"""Visual before/after evidence coverage (agent/visual_evidence.py).

Proves coverage is pure call-ORDER evidence (never pixel comparison),
only visually-relevant mutating categories are checked, and a mutation
missing either side is reported by name, never hidden.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-visual-evidence-tests")

from agent.visual_evidence import before_after_coverage               # noqa: E402
from agent.task_graph import Task, SUCCESS                            # noqa: E402
from mcp.capability import ToolCapability, CREATE, READ               # noqa: E402


def task(name, tool, status=SUCCESS):
    return Task(id=name, name=name, tool=tool, status=status)


class FakeTool:
    def __init__(self, name, category):
        self.name = name
        self.capability = ToolCapability(category=category, read_only=False,
                                         reversible=False, destructive=False,
                                         network=False,
                                         requires_confirmation=False)


class FakeRegistry:
    def __init__(self, tools):
        self._tools = tools

    def by_name(self, name):
        for t in self._tools:
            if t.name == name:
                return t
        return None


class BeforeAfterCoverageTests(unittest.TestCase):
    def _registry(self):
        return FakeRegistry([
            FakeTool("spawn_actor", CREATE),
            FakeTool("screenshot", READ),
        ])

    def test_a_mutation_with_captures_on_both_sides_is_covered(self):
        tasks = [task("t1", "screenshot"), task("t2", "spawn_actor"),
                task("t3", "screenshot")]
        result = before_after_coverage(tasks, self._registry())
        self.assertEqual(len(result["with_before_after_capture"]), 1)
        self.assertEqual(result["missing_before_after_capture"], [])
        self.assertEqual(result["coverage_pct"], 100)

    def test_a_mutation_with_no_capture_at_all_is_reported_missing_both(self):
        tasks = [task("t1", "spawn_actor")]
        result = before_after_coverage(tasks, self._registry())
        self.assertEqual(len(result["missing_before_after_capture"]), 1)
        self.assertEqual(
            set(result["missing_before_after_capture"][0]["missing"]),
            {"before", "after"})

    def test_a_mutation_with_only_a_before_capture_is_missing_after(self):
        tasks = [task("t1", "screenshot"), task("t2", "spawn_actor")]
        result = before_after_coverage(tasks, self._registry())
        self.assertEqual(
            result["missing_before_after_capture"][0]["missing"], ["after"])

    def test_a_failed_mutation_is_never_counted(self):
        from agent.task_graph import FAILED
        tasks = [task("t1", "spawn_actor", status=FAILED)]
        result = before_after_coverage(tasks, self._registry())
        self.assertEqual(result["mutations_checked"], 0)

    def test_a_non_mutating_call_is_never_checked(self):
        tasks = [task("t1", "list_actors")]
        registry = FakeRegistry([FakeTool("list_actors", READ)])
        result = before_after_coverage(tasks, registry)
        self.assertEqual(result["mutations_checked"], 0)

    def test_no_registry_match_is_handled_without_crashing(self):
        tasks = [task("t1", "unknown_tool")]
        result = before_after_coverage(tasks, FakeRegistry([]))
        self.assertEqual(result["mutations_checked"], 0)

    def test_note_is_explicit_about_not_comparing_pixels(self):
        result = before_after_coverage([], self._registry())
        self.assertIn("Never a pixel comparison", result["note"])


class LoopIntegrationTests(unittest.TestCase):
    def test_a_real_run_produces_visual_evidence_coverage_in_its_report(self):
        import json as _json
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                   "inputSchema": {"type": "object", "properties": {}}}

        server = MockMCPServer("engine", [tool("spawn_actor")])
        manager = FakeManager([server])

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return _json.dumps({"plan": {
                    "title": "Scene", "rationale": "x",
                    "steps": [{"name": "add-actor", "title": "Add an actor",
                              "tool": "engine.spawn_actor", "args": {}}],
                }})
            return _json.dumps({"done": True})

        run = AgentRun("r1", "add an actor to the level", manager, llm=llm,
                      max_steps=3, max_replans=0)
        report = run.run()
        self.assertIn("visual_evidence_coverage", report)
        self.assertIn("coverage_pct", report["visual_evidence_coverage"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
