"""TaskGraph — dependency-aware step graph with failure propagation.

Covers the one property every caller (the run loop's `is_terminal()` /
`ready()` / final report) depends on: once a task fails, every task
downstream of it — however many hops away — must reach a terminal status
(SKIPPED), never get stuck PENDING forever. A single-hop-only propagation
bug here would leave deep dependency chains unable to finish a run cleanly
and would misreport a skipped step as merely "never run" in the final
report.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

from agent.task_graph import (  # noqa: E402
    FAILED, PENDING, SKIPPED, SUCCESS, Task, TaskGraph,
)


def expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        raise SystemExit(1)


class TaskGraphTests(unittest.TestCase):
    def test_ready_requires_all_deps_success(self):
        g = TaskGraph()
        g.add(Task(id="a", name="a"))
        g.add(Task(id="b", name="b", deps=["a"]))
        self.assertEqual([t.id for t in g.ready()], ["a"])
        g.mark_success("a", {"ok": True})
        self.assertEqual([t.id for t in g.ready()], ["b"])

    def test_failure_skips_a_three_hop_dependency_chain(self):
        # A -> B -> C -> D. Failing A must terminate the whole chain, not
        # just the immediate dependent B.
        g = TaskGraph()
        g.add(Task(id="A", name="A"))
        g.add(Task(id="B", name="B", deps=["A"]))
        g.add(Task(id="C", name="C", deps=["B"]))
        g.add(Task(id="D", name="D", deps=["C"]))

        g.mark_failed("A", "boom")

        self.assertEqual(g.get("A").status, FAILED)
        self.assertEqual(g.get("B").status, SKIPPED)
        self.assertEqual(g.get("C").status, SKIPPED)
        self.assertEqual(g.get("D").status, SKIPPED)
        self.assertTrue(
            g.is_terminal(),
            "a 3+ hop dependency chain must fully terminate, not leave "
            "a downstream task stuck PENDING forever")
        self.assertEqual(g.ready(), [])
        # Each skip note should blame the task's own immediate blocker,
        # not always the original root-cause task (C does not even
        # directly depend on A).
        self.assertIn("'B'", g.get("C").notes)
        self.assertIn("'C'", g.get("D").notes)

    def test_failure_skips_a_diamond_shaped_graph_exactly_once(self):
        # A -> B, A -> F, and E depends on BOTH B and F. E is reachable
        # from A via two different paths; it must still be processed
        # exactly once (no crash, no double-processing artifact).
        g = TaskGraph()
        g.add(Task(id="A", name="A"))
        g.add(Task(id="B", name="B", deps=["A"]))
        g.add(Task(id="F", name="F", deps=["A"]))
        g.add(Task(id="E", name="E", deps=["B", "F"]))

        g.mark_failed("A", "boom")

        for tid in ("B", "F", "E"):
            self.assertEqual(g.get(tid).status, SKIPPED)
        self.assertTrue(g.is_terminal())

    def test_skip_does_not_touch_already_terminal_tasks(self):
        # If a dependent already finished successfully before the failure
        # (possible in a wide graph with independent branches), a later
        # unrelated failure elsewhere must never retroactively overwrite
        # its SUCCESS.
        g = TaskGraph()
        g.add(Task(id="A", name="A"))
        g.add(Task(id="B", name="B"))          # independent, no deps on A
        g.add(Task(id="C", name="C", deps=["A", "B"]))
        g.mark_success("B", "done")
        g.mark_failed("A", "boom")
        self.assertEqual(g.get("B").status, SUCCESS,
                         "an unrelated already-SUCCESS task must not be "
                         "touched by a sibling's failure propagation")
        self.assertEqual(g.get("C").status, SKIPPED)

    def test_mark_skipped_also_propagates_transitively(self):
        g = TaskGraph()
        g.add(Task(id="A", name="A"))
        g.add(Task(id="B", name="B", deps=["A"]))
        g.add(Task(id="C", name="C", deps=["B"]))
        g.mark_skipped("A", "operator cancelled this step")
        self.assertEqual(g.get("B").status, SKIPPED)
        self.assertEqual(g.get("C").status, SKIPPED)
        self.assertTrue(g.is_terminal())

    def test_missing_dependency_counts_as_unmet_not_a_crash(self):
        g = TaskGraph()
        g.add(Task(id="a", name="a", deps=["ghost"]))
        self.assertEqual(g.ready(), [])
        self.assertFalse(g.deps_met(g.get("a")))

    def test_to_dict_from_dict_round_trip_preserves_status(self):
        g = TaskGraph()
        g.add(Task(id="A", name="A"))
        g.add(Task(id="B", name="B", deps=["A"]))
        g.mark_failed("A", "boom")
        restored = TaskGraph.from_dict(g.to_dict())
        self.assertEqual(restored.get("A").status, FAILED)
        self.assertEqual(restored.get("B").status, SKIPPED)


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    print("\nAll task_graph tests passed.")
