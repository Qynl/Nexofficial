"""Failure classification taxonomy (agent/failures.py).

A failed task's raw error string alone does not tell an operator (or a
future smarter recovery strategy) whether this was a dropped connection,
a missing argument, a broken C++ build, or a runtime crash. These tests
prove the classifier distinguishes them, and that AgentRun actually
labels failed tasks with it end to end.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-failures-tests")

from agent.failures import (                                    # noqa: E402
    ARCHITECTURE, ARGUMENT, COMPILATION, GAMEPLAY, MISSING_CAPABILITY,
    PERFORMANCE, RUNTIME, SCHEMA, TRANSIENT, UNKNOWN, VISUAL,
    classify_failure, label, recovery_strategy,
)
from agent.loop import AgentRun                                  # noqa: E402
from agent.mock_mcp import MockMCPServer                         # noqa: E402
from agent.task_graph import FAILED                              # noqa: E402
from test_agent_loop import FakeManager                          # noqa: E402


def tool(name, description="", properties=None):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object",
                            "properties": properties or {}}}


class ClassifyFailureTests(unittest.TestCase):
    def test_transient_network_errors(self):
        self.assertEqual(classify_failure("connection timed out"), TRANSIENT)
        self.assertEqual(classify_failure("503 Service Unavailable"),
                         TRANSIENT)
        self.assertEqual(classify_failure("rate limit exceeded"), TRANSIENT)

    def test_missing_capability_beats_everything_else(self):
        # A server that flatly cannot do something must never be
        # misreported as some other category just because the refusal
        # text also happens to mention a timeout-shaped word.
        self.assertEqual(
            classify_failure("no MCP servers are connected"),
            MISSING_CAPABILITY)
        self.assertEqual(
            classify_failure("capability is not available on this server"),
            MISSING_CAPABILITY)

    def test_schema_vs_argument(self):
        self.assertEqual(
            classify_failure("validation failed: does not match schema"),
            SCHEMA)
        self.assertEqual(
            classify_failure("missing required parameter 'actor_id'"),
            ARGUMENT)

    def test_compilation_vs_runtime(self):
        self.assertEqual(
            classify_failure("UnrealBuildTool: build failed, 2 errors"),
            COMPILATION)
        self.assertEqual(
            classify_failure("Unhandled exception: segmentation fault"),
            RUNTIME)

    def test_visual_and_performance(self):
        self.assertEqual(
            classify_failure("material compile error: missing shader"),
            VISUAL)
        self.assertEqual(
            classify_failure("frame time exceeded the performance budget"),
            PERFORMANCE)

    def test_architecture_and_gameplay(self):
        self.assertEqual(
            classify_failure("circular dependency detected between systems"),
            ARCHITECTURE)
        self.assertEqual(
            classify_failure("mission failed: objective state corrupted"),
            GAMEPLAY)

    def test_unrecognized_error_is_honestly_unknown(self):
        self.assertEqual(classify_failure("something went sideways"),
                         UNKNOWN)
        self.assertEqual(classify_failure(""), UNKNOWN)
        self.assertEqual(classify_failure(None), UNKNOWN)

    def test_tool_gate_breaks_ties_for_a_generic_error(self):
        # A vague error from a tool whose ONLY declared evidence gate is
        # "build" is more usefully reported as a compilation failure than
        # as a flat unknown — but only when that gate is unambiguous.
        self.assertEqual(
            classify_failure("it did not work", gates={"build"}),
            COMPILATION)
        self.assertEqual(
            classify_failure("it did not work", gates={"build", "visual"}),
            UNKNOWN)

    def test_label_and_strategy_cover_every_kind(self):
        for kind in (TRANSIENT, SCHEMA, ARGUMENT, COMPILATION, RUNTIME,
                    GAMEPLAY, VISUAL, PERFORMANCE, ARCHITECTURE,
                    MISSING_CAPABILITY, UNKNOWN):
            self.assertTrue(label(kind))
            self.assertTrue(recovery_strategy(kind))


class LoopIntegrationTests(unittest.TestCase):
    """The classification must actually reach the task and the report,
    not just exist as an unused pure function."""

    def test_a_failed_task_is_labeled_and_reaches_the_report(self):
        server = MockMCPServer(
            "engine", [tool("build_project", "Build the project", {})])
        manager = FakeManager([server], fail={
            "build_project": [99,
                              "UnrealBuildTool: build failed, see log"]})

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return json.dumps({"plan": {
                    "title": "Build", "rationale": "x",
                    "steps": [{"name": "build-it", "title": "Build it",
                              "tool": "engine.build_project", "args": {}}],
                }})
            return json.dumps({"done": False, "adjust": "stop",
                               "reason": "build is broken"})

        run = AgentRun("f1", "build the project", manager, llm=llm,
                      max_steps=3, max_replans=0)
        report = run.run()
        failed_task = next(t for t in run.graph.all()
                           if t.status == FAILED)
        self.assertEqual(failed_task.failure_kind, COMPILATION)
        failed_entries = report["failed"]
        self.assertEqual(len(failed_entries), 1)
        self.assertEqual(failed_entries[0]["failure_kind"], COMPILATION)
        self.assertIn("Build/compile", failed_entries[0]["failure_label"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
