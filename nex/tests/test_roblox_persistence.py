"""Roblox persistence testing (roblox/persistence.py).

Proves: a save-only run never counts as a confirmed round-trip, an
unresolved action is reported honestly, and only in-order successful
evidence can confirm a case.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-persistence-tests")

from roblox.persistence import (                                    # noqa: E402
    CASES, UNAVAILABLE, case_evidence, persistence_review, resolve_case,
)
from agent.task_graph import Task, SUCCESS                           # noqa: E402


def task(name, tool, status=SUCCESS):
    return Task(id=name, name=name, tool=tool, status=status)


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeRegistry:
    def __init__(self, tool_names):
        self._tools = [FakeTool(n) for n in tool_names]

    def all_tools(self):
        return self._tools


ROUND_TRIP = next(c for c in CASES if c.id == "save_then_load_round_trip")


class ResolveCaseTests(unittest.TestCase):
    def test_fully_connected_registry_is_runnable(self):
        registry = FakeRegistry(["datastore_set", "datastore_get"])
        res = resolve_case(ROUND_TRIP, registry)
        self.assertTrue(res["runnable"])

    def test_missing_action_is_reported_honestly(self):
        registry = FakeRegistry(["datastore_set"])
        res = resolve_case(ROUND_TRIP, registry)
        self.assertFalse(res["runnable"])
        self.assertIn("LOAD", res["unavailable_actions"])
        self.assertIn(UNAVAILABLE, res["note"])


class CaseEvidenceTests(unittest.TestCase):
    def test_a_save_without_a_load_is_not_confirmed(self):
        registry = FakeRegistry(["datastore_set", "datastore_get"])
        resolution = resolve_case(ROUND_TRIP, registry)
        evidence = case_evidence(
            resolution, [task("t1", "datastore_set")])
        self.assertNotEqual(evidence["verdict"], "confirmed")

    def test_save_then_load_in_order_is_confirmed(self):
        registry = FakeRegistry(["datastore_set", "datastore_get"])
        resolution = resolve_case(ROUND_TRIP, registry)
        evidence = case_evidence(resolution, [
            task("t1", "datastore_set"), task("t2", "datastore_get")])
        self.assertEqual(evidence["verdict"], "confirmed")

    def test_not_runnable_when_a_tool_is_missing(self):
        registry = FakeRegistry(["datastore_set"])
        resolution = resolve_case(ROUND_TRIP, registry)
        evidence = case_evidence(resolution, [])
        self.assertEqual(evidence["verdict"], "not_runnable")


class PersistenceReviewTests(unittest.TestCase):
    def test_review_covers_every_case_by_default(self):
        review = persistence_review([], FakeRegistry([]))
        self.assertEqual(len(review["cases_checked"]), len(CASES))

    def test_scoped_case_ids_limit_the_review(self):
        review = persistence_review(
            [], FakeRegistry([]),
            case_ids=["save_then_load_round_trip"])
        self.assertEqual(len(review["cases_checked"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
