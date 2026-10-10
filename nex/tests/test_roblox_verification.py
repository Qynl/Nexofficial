"""Roblox per-system verification (roblox/verification.py).

Proves the two new dimensions are NOT_APPLICABLE when no canonical case
exists for a system, PASS only when a real catalog scenario confirmed,
and never silently fabricate multiplayer/persistence proof for a system
no catalog entry covers.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-verification-tests")

from roblox.verification import NOT_APPLICABLE, roblox_verify_systems   # noqa: E402
from agent.verification import PASS, UNPROVEN                          # noqa: E402
from agent.task_graph import Task, SUCCESS                             # noqa: E402


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

    def by_name(self, name):
        for t in self._tools:
            if t.name == name:
                return t
        return None


class RobloxVerifySystemsTests(unittest.TestCase):
    def test_a_system_with_no_catalog_entry_is_not_applicable(self):
        registry = FakeRegistry(["navmesh_bake"])
        result = roblox_verify_systems(
            [task("t1", "navmesh_bake")], registry)
        self.assertEqual(result["navigation"]["persistence_tested"],
                         NOT_APPLICABLE)
        self.assertEqual(result["navigation"]["multiplayer_tested"],
                         NOT_APPLICABLE)

    def test_economy_with_confirmed_purchase_scenario_passes_multiplayer(
            self):
        registry = FakeRegistry(["shop_purchase_item", "start_client",
                                 "fire_remote", "studio_output"])
        tasks = [
            task("t1", "shop_purchase_item"),
            task("t2", "start_client"), task("t3", "start_client"),
            task("t4", "fire_remote"), task("t5", "fire_remote"),
        ]
        result = roblox_verify_systems(tasks, registry)
        self.assertEqual(result["economy"]["multiplayer_tested"], PASS)

    def test_economy_with_no_multiplayer_evidence_is_unproven_not_passing(
            self):
        registry = FakeRegistry(["shop_purchase_item"])
        result = roblox_verify_systems(
            [task("t1", "shop_purchase_item")], registry)
        self.assertEqual(result["economy"]["multiplayer_tested"], UNPROVEN)

    def test_base_dimensions_are_still_present(self):
        registry = FakeRegistry(["shop_purchase_item"])
        result = roblox_verify_systems(
            [task("t1", "shop_purchase_item")], registry)
        self.assertIn("implemented", result["economy"])
        self.assertIn("overall", result["economy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
