"""Roblox playtest agent (roblox/playtest.py).

Proves: an action with no connected tool is reported, never hidden; a
scenario with no observation tool is "not_runnable" even if every action
resolves (you cannot tell whether it worked); evidence only counts
in-order, successful, resolved steps.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-playtest-tests")

from roblox.playtest import (                                       # noqa: E402
    SCENARIOS, UNAVAILABLE, playtest_review, resolve_scenario,
    scenario_evidence,
)
from agent.task_graph import Task, SUCCESS, FAILED                  # noqa: E402


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


CHECKPOINT = next(s for s in SCENARIOS if s.id == "checkpoint_respawn")


class ResolveScenarioTests(unittest.TestCase):
    def test_fully_connected_and_observable_is_runnable(self):
        registry = FakeRegistry([
            "play_solo", "move_player", "activate_prompt", "respawn",
            "studio_output"])
        res = resolve_scenario(CHECKPOINT, registry)
        self.assertTrue(res["runnable"])
        self.assertEqual(res["unavailable_actions"], [])

    def test_missing_action_is_reported_honestly(self):
        registry = FakeRegistry(["play_solo", "studio_output"])
        res = resolve_scenario(CHECKPOINT, registry)
        self.assertFalse(res["runnable"])
        self.assertIn("MOVE", res["unavailable_actions"])
        self.assertIn(UNAVAILABLE, res["note"])

    def test_resolvable_actions_without_any_observation_tool_is_not_runnable(
            self):
        registry = FakeRegistry([
            "play_solo", "move_player", "activate_prompt", "respawn"])
        res = resolve_scenario(CHECKPOINT, registry)
        self.assertFalse(res["observable"])
        self.assertFalse(res["runnable"],
                         "every action resolving is not enough without a "
                         "way to observe what happened")

    def test_no_registry_resolves_nothing(self):
        res = resolve_scenario(CHECKPOINT, None)
        self.assertFalse(res["runnable"])


class ScenarioEvidenceTests(unittest.TestCase):
    def _registry(self):
        return FakeRegistry([
            "play_solo", "move_player", "activate_prompt", "respawn",
            "studio_output"])

    def test_confirmed_when_the_whole_sequence_ran_in_order(self):
        resolution = resolve_scenario(CHECKPOINT, self._registry())
        tasks = [task("t1", "play_solo"), task("t2", "move_player"),
                task("t3", "activate_prompt"), task("t4", "respawn")]
        evidence = scenario_evidence(resolution, tasks)
        self.assertEqual(evidence["verdict"], "confirmed")

    def test_not_attempted_when_resolvable_but_never_called(self):
        resolution = resolve_scenario(CHECKPOINT, self._registry())
        evidence = scenario_evidence(resolution, [])
        self.assertEqual(evidence["verdict"], "not_attempted")

    def test_a_failed_call_never_counts_as_evidence(self):
        resolution = resolve_scenario(CHECKPOINT, self._registry())
        evidence = scenario_evidence(
            resolution, [task("t1", "play_solo", status=FAILED)])
        self.assertEqual(evidence["steps_confirmed"], 0)

    def test_out_of_order_calls_do_not_confirm_the_full_sequence(self):
        resolution = resolve_scenario(CHECKPOINT, self._registry())
        tasks = [task("t1", "respawn"), task("t2", "play_solo"),
                task("t3", "move_player"), task("t4", "activate_prompt")]
        evidence = scenario_evidence(resolution, tasks)
        self.assertNotEqual(evidence["verdict"], "confirmed")


class PlaytestReviewTests(unittest.TestCase):
    def test_scoped_scenario_ids_limit_the_review(self):
        review = playtest_review([], FakeRegistry([]),
                                 scenario_ids=["checkpoint_respawn"])
        self.assertEqual(len(review["scenarios_checked"]), 1)
        self.assertEqual(
            review["scenarios_checked"][0]["scenario"], "checkpoint_respawn")

    def test_default_reviews_every_canonical_scenario(self):
        review = playtest_review([], FakeRegistry([]))
        self.assertEqual(len(review["scenarios_checked"]), len(SCENARIOS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
