"""Cross-run checkpoint/rollback (agent/checkpoints.py).

Proves checkpoints are a thin, honest tag over agent/reversal.py's own
output (never re-derived), multiple runs combine newest-first with each
run's internal LIFO order preserved, and totals/coverage are recomputed
correctly across the combined set.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-checkpoints-tests")

from agent.checkpoints import (                                    # noqa: E402
    checkpoint_summary, combine_rollback_plan, make_checkpoint,
)


def reversal_plan(mutations, reversible, steps):
    return {
        "available": bool(steps), "mutations": mutations,
        "reversible": reversible, "irreversible": mutations - reversible,
        "coverage_pct": round(100 * reversible / mutations) if mutations else 0,
        "steps": steps, "blocked": [],
    }


class MakeCheckpointTests(unittest.TestCase):
    def test_tags_the_plan_with_run_metadata(self):
        plan = reversal_plan(2, 1, [{"tool": "destroy_actor"}])
        cp = make_checkpoint(plan, "run-1", run_no=3)
        self.assertEqual(cp["run_id"], "run-1")
        self.assertEqual(cp["run_no"], 3)
        self.assertEqual(cp["mutations"], 2)
        self.assertEqual(cp["steps"], [{"tool": "destroy_actor"}])

    def test_an_empty_plan_is_handled_safely(self):
        cp = make_checkpoint({}, "run-1", run_no=1)
        self.assertFalse(cp["available"])
        self.assertEqual(cp["steps"], [])


class CombineRollbackPlanTests(unittest.TestCase):
    def test_no_checkpoints_is_an_honest_empty_plan(self):
        plan = combine_rollback_plan([])
        self.assertFalse(plan["available"])
        self.assertEqual(plan["runs_included"], [])

    def test_newer_runs_come_before_older_runs(self):
        cp1 = make_checkpoint(
            reversal_plan(1, 1, [{"tool": "destroy_a"}]), "r1", run_no=1)
        cp2 = make_checkpoint(
            reversal_plan(1, 1, [{"tool": "destroy_b"}]), "r2", run_no=2)
        plan = combine_rollback_plan([cp1, cp2])
        self.assertEqual(plan["runs_included"], [2, 1])
        self.assertEqual([s["tool"] for s in plan["steps"]],
                         ["destroy_b", "destroy_a"])

    def test_each_runs_own_step_order_is_preserved(self):
        cp = make_checkpoint(
            reversal_plan(2, 2, [{"tool": "destroy_second"},
                                {"tool": "destroy_first"}]),
            "r1", run_no=1)
        plan = combine_rollback_plan([cp])
        self.assertEqual([s["tool"] for s in plan["steps"]],
                         ["destroy_second", "destroy_first"])

    def test_since_run_no_excludes_older_checkpoints(self):
        cp1 = make_checkpoint(
            reversal_plan(1, 1, [{"tool": "destroy_a"}]), "r1", run_no=1)
        cp2 = make_checkpoint(
            reversal_plan(1, 1, [{"tool": "destroy_b"}]), "r2", run_no=2)
        cp3 = make_checkpoint(
            reversal_plan(1, 1, [{"tool": "destroy_c"}]), "r3", run_no=3)
        plan = combine_rollback_plan([cp1, cp2, cp3], since_run_no=1)
        self.assertEqual(plan["runs_included"], [3, 2])

    def test_totals_and_coverage_are_recomputed_across_the_combined_set(
            self):
        cp1 = make_checkpoint(reversal_plan(4, 2, [{"tool": "a"},
                                                   {"tool": "b"}]),
                             "r1", run_no=1)
        cp2 = make_checkpoint(reversal_plan(2, 2, [{"tool": "c"},
                                                   {"tool": "d"}]),
                             "r2", run_no=2)
        plan = combine_rollback_plan([cp1, cp2])
        self.assertEqual(plan["mutations"], 6)
        self.assertEqual(plan["reversible"], 4)
        self.assertEqual(plan["irreversible"], 2)
        self.assertEqual(plan["coverage_pct"], round(100 * 4 / 6))

    def test_steps_are_tagged_with_their_origin_run(self):
        cp = make_checkpoint(
            reversal_plan(1, 1, [{"tool": "destroy_a"}]), "r1", run_no=7)
        plan = combine_rollback_plan([cp])
        self.assertEqual(plan["steps"][0]["from_run"], 7)


class CheckpointSummaryTests(unittest.TestCase):
    def test_summary_never_exposes_raw_steps(self):
        cp = make_checkpoint(
            reversal_plan(1, 1, [{"tool": "destroy_a", "args": {"x": 1}}]),
            "r1", run_no=1)
        summary = checkpoint_summary(cp)
        self.assertNotIn("steps", summary)
        self.assertEqual(summary["run_no"], 1)


class LoopIntegrationTests(unittest.TestCase):
    def test_a_real_run_produces_a_checkpoint_in_its_report(self):
        import json as _json
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                   "inputSchema": {"type": "object", "properties": {}}}

        server = MockMCPServer("engine", [tool("spawn_vehicle"),
                                          tool("destroy_vehicle")])
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
        self.assertIn("checkpoint", report)
        self.assertEqual(report["checkpoint"]["run_id"], "r1")
        self.assertEqual(report["checkpoint"]["run_no"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
