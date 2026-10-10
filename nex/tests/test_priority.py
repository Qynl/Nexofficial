"""Bottleneck-aware priority advisory (agent/priority.py).

A long run must not spend its budget as evenly-distributed random tool
calls. These tests prove the single biggest bottleneck is identified
from real signals (current failure classifications, the quality
scorecard, open visual defects) with a sane precedence order, and that
nothing is claimed when no bottleneck actually dominates.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-priority-tests")

from agent.priority import bottleneck_note                      # noqa: E402


class BottleneckNoteTests(unittest.TestCase):
    def test_no_signals_means_no_override(self):
        self.assertEqual(bottleneck_note(), "")
        self.assertEqual(bottleneck_note(
            failure_kinds=[], quality_missing=[], visual_open_count=0), "")

    def test_compilation_failure_always_wins_first(self):
        note = bottleneck_note(
            failure_kinds=["compilation"],
            quality_missing=["performance"], visual_open_count=10)
        self.assertIn("compilation failure", note)
        self.assertIn("Stop adding new content", note)

    def test_a_single_runtime_crash_is_not_yet_a_bottleneck(self):
        # One crash could be a fluke; the recovery ladder already retries/
        # repairs it. Only a REPEATED crash should override priority.
        self.assertEqual(bottleneck_note(failure_kinds=["runtime"]), "")

    def test_repeated_runtime_crashes_are_a_bottleneck(self):
        note = bottleneck_note(failure_kinds=["runtime", "runtime"])
        self.assertIn("runtime crashes", note)

    def test_missing_performance_evidence_is_flagged(self):
        note = bottleneck_note(quality_missing=["performance"])
        self.assertIn("no performance measurement", note)

    def test_visual_backlog_only_wins_with_no_functional_gap(self):
        no_gap = bottleneck_note(
            quality_missing=["visual_review"], visual_open_count=5)
        self.assertIn("shift effort toward visual polish", no_gap)

        with_gap = bottleneck_note(
            quality_missing=["playtest"], visual_open_count=5)
        self.assertEqual(with_gap, "")

    def test_a_couple_open_visual_defects_do_not_override_priority(self):
        self.assertEqual(bottleneck_note(visual_open_count=1), "")

    def test_architecture_failure_is_flagged_when_nothing_else_dominates(self):
        note = bottleneck_note(failure_kinds=["architecture"])
        self.assertIn("architectural failure", note)


class LoopIntegrationTests(unittest.TestCase):
    """Prove agent/loop.py's own _planning_brief() actually carries the
    bottleneck directive once a real tool call has failed with a real
    failure_kind on the graph — not just that the pure function works."""

    def test_a_real_compilation_failure_appears_in_the_next_planning_brief(self):
        import json as _json
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                    "inputSchema": {"type": "object", "properties": {}}}

        server = MockMCPServer("engine", [tool("build_project")])
        manager = FakeManager([server], fail={
            "build_project": [99, "UnrealBuildTool: build failed"]})

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return _json.dumps({"plan": {
                    "title": "Build", "rationale": "x",
                    "steps": [{"name": "build-it", "title": "Build it",
                              "tool": "engine.build_project", "args": {}}],
                }})
            return _json.dumps({"done": False, "adjust": "stop",
                               "reason": "broken build"})

        run = AgentRun("r1", "build the project", manager, llm=llm,
                      max_steps=3, max_replans=1)
        run.run()
        failed = [t for t in run.graph.all() if t.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].failure_kind, "compilation")

        # Exactly the call agent/loop.py itself makes before every
        # plan/replan — proves the wiring, not a reimplementation of it.
        brief = run._planning_brief(run.manager.registry())
        self.assertIn("BOTTLENECK", brief)
        self.assertIn("compilation failure", brief)
        self.assertIn("Stop adding new content", brief)


if __name__ == "__main__":
    unittest.main(verbosity=2)
