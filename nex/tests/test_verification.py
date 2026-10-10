"""Implementation-vs-proof verification matrix (agent/verification.py).

A tool call succeeding is not "the feature works." These tests prove
each proof dimension (implemented/compiled/runtime_tested/performance_
tested/visually_reviewed/regression_checked) is computed separately and
deterministically from real task/gate evidence, and that OVERALL can
only be PASS when every REQUIRED dimension actually passed — mirroring
the user's own worked example (vehicle system: everything green except
save/load -> overall NOT VERIFIED).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-verification-tests")

from agent.verification import FAIL, PASS, UNPROVEN, WARNING, verify_systems  # noqa: E402
from agent.task_graph import Task, SUCCESS, FAILED                # noqa: E402
from agent.mock_mcp import MockMCPServer                          # noqa: E402
from test_agent_loop import FakeManager                            # noqa: E402


def tool(name):
    return {"name": name, "description": "",
            "inputSchema": {"type": "object", "properties": {}}}


def task(name, server, tool_name, status=SUCCESS):
    return Task(id=name, name=name, server=server, tool=tool_name,
               status=status)


class VerifySystemsTests(unittest.TestCase):
    def setUp(self):
        server = MockMCPServer("engine", [
            tool("spawn_vehicle"), tool("build_project"),
            tool("start_pie"), tool("spawn_traffic"), tool("create_mission"),
            tool("save_game_slot"), tool("player_interact_vehicle"),
        ])
        self.registry = FakeManager([server]).registry()

    def test_untouched_system_is_not_in_the_matrix_at_all(self):
        matrix = verify_systems([], self.registry)
        self.assertEqual(matrix, {})

    def test_implemented_but_nothing_else_proven_is_unproven_overall(self):
        tasks = [task("s1", "engine", "spawn_vehicle")]
        matrix = verify_systems(tasks, self.registry)
        v = matrix["vehicles"]
        self.assertEqual(v["implemented"], PASS)
        self.assertEqual(v["compiled"], UNPROVEN)
        self.assertEqual(v["runtime_tested"], UNPROVEN)
        self.assertEqual(v["overall"], UNPROVEN)

    def test_a_failed_implementation_attempt_is_an_honest_fail(self):
        tasks = [task("s1", "engine", "spawn_vehicle", status=FAILED)]
        matrix = verify_systems(tasks, self.registry)
        self.assertEqual(matrix["vehicles"]["implemented"], FAIL)
        self.assertEqual(matrix["vehicles"]["overall"], FAIL)

    def test_build_and_runtime_evidence_is_run_wide_not_system_isolated(self):
        tasks = [
            task("s1", "engine", "spawn_vehicle"),
            task("s2", "engine", "build_project"),
            task("s3", "engine", "start_pie"),
        ]
        matrix = verify_systems(tasks, self.registry)
        v = matrix["vehicles"]
        self.assertEqual(v["compiled"], PASS)
        self.assertEqual(v["runtime_tested"], PASS)
        self.assertTrue(any("run-wide evidence" in n for n in v["notes"]))

    def test_a_build_failure_this_run_marks_compiled_fail(self):
        tasks = [
            task("s1", "engine", "spawn_vehicle"),
            task("s2", "engine", "build_project", status=FAILED),
        ]
        matrix = verify_systems(tasks, self.registry)
        self.assertEqual(matrix["vehicles"]["compiled"], FAIL)
        self.assertEqual(matrix["vehicles"]["overall"], FAIL)

    def test_overall_matches_the_users_own_worked_example(self):
        # Vehicle: implementation/build/runtime all PASS, but its
        # dependent (persistence/save-load) was never itself touched —
        # regression_checked becomes WARNING, and since implemented/
        # compiled/runtime_tested are all proven, overall is WARNING, not
        # a silent PASS.
        tasks = [
            task("s1", "engine", "spawn_vehicle"),
            task("s2", "engine", "build_project"),
            task("s3", "engine", "start_pie"),
        ]
        matrix = verify_systems(tasks, self.registry)
        v = matrix["vehicles"]
        self.assertEqual(v["regression_checked"], WARNING)
        self.assertEqual(v["overall"], WARNING)
        self.assertNotEqual(v["overall"], PASS)

    def test_overall_is_pass_only_when_dependents_are_also_exercised(self):
        tasks = [
            task("s1", "engine", "spawn_vehicle"),
            task("s2", "engine", "build_project"),
            task("s3", "engine", "start_pie"),
            task("s4", "engine", "spawn_traffic"),
            task("s5", "engine", "create_mission"),
            task("s6", "engine", "save_game_slot"),
            task("s7", "engine", "player_interact_vehicle"),
        ]
        matrix = verify_systems(tasks, self.registry)
        v = matrix["vehicles"]
        self.assertEqual(v["regression_checked"], PASS)
        self.assertEqual(v["overall"], PASS)

    def test_performance_and_visual_are_informative_not_overall_blocking(self):
        tasks = [
            task("s1", "engine", "spawn_vehicle"),
            task("s2", "engine", "build_project"),
            task("s3", "engine", "start_pie"),
            task("s4", "engine", "spawn_traffic"),
            task("s5", "engine", "create_mission"),
            task("s6", "engine", "save_game_slot"),
            task("s7", "engine", "player_interact_vehicle"),
        ]
        matrix = verify_systems(tasks, self.registry,
                                visual_critiques_recorded=0)
        v = matrix["vehicles"]
        self.assertEqual(v["performance_tested"], UNPROVEN)
        self.assertEqual(v["visually_reviewed"], UNPROVEN)
        self.assertEqual(v["overall"], PASS,
                         "missing perf/visual proof must not block overall "
                         "PASS — they are informative, not required")

    def test_a_real_visual_critique_is_reflected(self):
        matrix = verify_systems(
            [task("s1", "engine", "spawn_vehicle")], self.registry,
            visual_critiques_recorded=2)
        self.assertEqual(matrix["vehicles"]["visually_reviewed"], PASS)

    def test_a_system_with_no_dependency_knowledge_is_unproven_not_pass(self):
        # "ui" has no DEPENDENTS entries of its own listed as a KEY, so
        # there is nothing to check — that must read as UNPROVEN (no
        # claim either way), never a silent PASS.
        tasks = [task("s1", "engine", "spawn_vehicle"),
                task("s2", "engine", "build_project"),
                task("s3", "engine", "start_pie")]
        matrix = verify_systems(tasks, self.registry)
        # vehicles DOES have dependents, so assert on a hypothetical one
        # with none by checking the mechanism directly via a system with
        # an empty DEPENDENTS tuple: "player_interaction" has dependents
        # ("ui",), so use "ui" itself, which has none.
        from agent.task_graph import Task as T
        ui_task = T(id="ui1", name="ui1", server="engine", tool="hud_widget",
                   status=SUCCESS)
        matrix2 = verify_systems([ui_task], self.registry)
        self.assertIn("ui", matrix2)
        self.assertEqual(matrix2["ui"]["regression_checked"], UNPROVEN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
