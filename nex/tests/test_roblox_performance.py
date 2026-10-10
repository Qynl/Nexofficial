"""Roblox performance intelligence (roblox/performance.py).

Proves measurements are only real parsed numbers (never guessed), budget
evaluation is honestly UNVERIFIED without a measurement, and before/after
comparison requires a measurement on both sides.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-performance-tests")

from roblox.performance import (                                    # noqa: E402
    FAIL, PASS, UNVERIFIED, collect_evidence, compare_before_after,
    evaluate_budgets, extract_measurements,
)
from agent.task_graph import Task, SUCCESS, FAILED                  # noqa: E402


def task(name, tool, result="", status=SUCCESS):
    return Task(id=name, name=name, tool=tool, result=result, status=status)


class ExtractMeasurementsTests(unittest.TestCase):
    def test_extracts_a_stated_number(self):
        self.assertEqual(
            extract_measurements("client frame time: 16.2ms")
            ["client_frame_time_ms"], 16.2)

    def test_no_number_means_no_entry(self):
        self.assertEqual(extract_measurements("everything looks fine"), {})

    def test_multiple_metrics_in_one_result(self):
        m = extract_measurements("fps: 58, memory: 512MB")
        self.assertEqual(m["fps"], 58.0)
        self.assertEqual(m["memory_mb"], 512.0)


class CollectEvidenceTests(unittest.TestCase):
    def test_only_performance_flavored_successful_calls_count(self):
        tasks = [
            task("t1", "microprofiler_snapshot", "client frame time: 10ms"),
            task("t2", "create_part", "fps: 999"),   # not perf-flavored tool
            task("t3", "microprofiler_snapshot", "no numbers here"),
            task("t4", "microprofiler_snapshot", "fps: 30", status=FAILED),
        ]
        evidence = collect_evidence(tasks)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["measurements"]["client_frame_time_ms"],
                         10.0)


class EvaluateBudgetsTests(unittest.TestCase):
    def test_unmeasured_metric_is_unverified_not_assumed_passing(self):
        results = evaluate_budgets([], {"client_frame_time_ms": 16.6})
        self.assertEqual(results["client_frame_time_ms"]["status"], UNVERIFIED)

    def test_under_budget_lower_is_better_metric_passes(self):
        evidence = [{"tool": "x", "measurements": {"client_frame_time_ms": 12.0}}]
        results = evaluate_budgets(evidence, {"client_frame_time_ms": 16.6})
        self.assertEqual(results["client_frame_time_ms"]["status"], PASS)

    def test_over_budget_fails(self):
        evidence = [{"tool": "x", "measurements": {"client_frame_time_ms": 25.0}}]
        results = evaluate_budgets(evidence, {"client_frame_time_ms": 16.6})
        self.assertEqual(results["client_frame_time_ms"]["status"], FAIL)

    def test_fps_is_higher_is_better(self):
        evidence = [{"tool": "x", "measurements": {"fps": 60.0}}]
        results = evaluate_budgets(evidence, {"fps": 30.0})
        self.assertEqual(results["fps"]["status"], PASS)
        low_fps = [{"tool": "x", "measurements": {"fps": 10.0}}]
        results2 = evaluate_budgets(low_fps, {"fps": 30.0})
        self.assertEqual(results2["fps"]["status"], FAIL)


class CompareBeforeAfterTests(unittest.TestCase):
    def test_missing_either_side_is_unverified(self):
        result = compare_before_after([], [], "client_frame_time_ms")
        self.assertEqual(result["status"], UNVERIFIED)

    def test_an_improvement_is_detected_for_lower_is_better_metrics(self):
        before = [{"tool": "x", "measurements": {"client_frame_time_ms": 20.0}}]
        after = [{"tool": "x", "measurements": {"client_frame_time_ms": 12.0}}]
        result = compare_before_after(before, after, "client_frame_time_ms")
        self.assertTrue(result["improved"])
        self.assertEqual(result["status"], PASS)

    def test_a_regression_is_detected_not_hidden(self):
        before = [{"tool": "x", "measurements": {"client_frame_time_ms": 10.0}}]
        after = [{"tool": "x", "measurements": {"client_frame_time_ms": 22.0}}]
        result = compare_before_after(before, after, "client_frame_time_ms")
        self.assertFalse(result["improved"])
        self.assertEqual(result["status"], FAIL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
