"""Roblox test generation (roblox/test_generation.py).

Proves test plans are assembled from the real tagged catalogs (never
invented prose), only populate categories that actually apply, and
regression picks up genuinely dependent systems via agent/regression.py.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-test-generation-tests")

from roblox.test_generation import (                                # noqa: E402
    affected_systems_for_feature, generate_test_plan, plan_step_brief,
)


class GenerateTestPlanTests(unittest.TestCase):
    def test_economy_feature_gets_functional_and_multiplayer_tests(self):
        plan = generate_test_plan(["economy"])
        self.assertIn("purchase_flow", plan["functional"])
        self.assertIn("simultaneous_limited_purchase", plan["multiplayer"])

    def test_unrelated_system_yields_empty_categories(self):
        plan = generate_test_plan(["world_streaming"])
        self.assertEqual(plan["functional"], [])
        self.assertEqual(plan["multiplayer"], [])
        self.assertEqual(plan["persistence"], [])

    def test_persistence_feature_gets_persistence_cases(self):
        plan = generate_test_plan(["persistence"])
        self.assertIn("save_then_load_round_trip", plan["persistence"])

    def test_regression_lists_dependents_not_already_touched(self):
        plan = generate_test_plan(["vehicles"])
        self.assertIn("traffic", plan["regression"])
        self.assertNotIn("vehicles", plan["regression"])

    def test_performance_sensitive_systems_are_flagged(self):
        plan = generate_test_plan(["npc"])
        self.assertIn("npc", plan["performance"])

    def test_join_leave_rejoin_is_filed_under_lifecycle_not_functional(self):
        plan = generate_test_plan(["persistence", "networking"])
        self.assertIn("join_leave_rejoin", plan["lifecycle"])
        self.assertNotIn("join_leave_rejoin", plan["functional"])

    def test_no_systems_yields_a_fully_empty_plan(self):
        plan = generate_test_plan([])
        self.assertEqual(plan["functional"], [])
        self.assertEqual(plan["regression"], [])


class AffectedSystemsForFeatureTests(unittest.TestCase):
    def test_classifies_a_proposed_tool_surface(self):
        systems = affected_systems_for_feature(
            ["create_remote_event", "datastore_set"])
        self.assertIn("networking", systems)
        self.assertIn("persistence", systems)

    def test_empty_input_yields_no_systems(self):
        self.assertEqual(affected_systems_for_feature([]), set())


class PlanStepBriefTests(unittest.TestCase):
    def test_renders_catalog_grounded_lines_for_each_category(self):
        plan = generate_test_plan(["economy", "persistence"])
        lines = plan_step_brief(plan)
        joined = "\n".join(lines)
        self.assertTrue(any(l.startswith("[playtest] purchase_flow:")
                            for l in lines))
        self.assertTrue(any(l.startswith("[multiplayer] "
                                         "simultaneous_limited_purchase:")
                            for l in lines))
        self.assertIn("save_then_load_round_trip", joined)
        self.assertIn("expected:", joined)

    def test_lifecycle_ids_use_the_playtest_prefix(self):
        plan = generate_test_plan(["persistence", "networking"])
        lines = plan_step_brief(plan)
        self.assertTrue(any(l.startswith("[playtest] join_leave_rejoin:")
                            for l in lines))

    def test_empty_plan_yields_no_lines(self):
        plan = generate_test_plan([])
        self.assertEqual(plan_step_brief(plan), [])

    def test_unknown_id_is_skipped_not_fabricated(self):
        plan = generate_test_plan(["economy"])
        plan = dict(plan)
        plan["functional"] = list(plan["functional"]) + ["no_such_case"]
        lines = plan_step_brief(plan)
        self.assertFalse(any("no_such_case" in l for l in lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
