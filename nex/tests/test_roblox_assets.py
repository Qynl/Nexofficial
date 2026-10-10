"""Roblox asset intelligence (roblox/assets.py).

Proves asset refs are only tracked from real successful call evidence,
shared-usage and possibly-broken signals behave as documented, and the
module is honest that true duplicate/broken-reference detection is
unavailable.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-assets-tests")

from roblox.assets import (                                         # noqa: E402
    dependents_of_asset, empty_model, extract_asset_refs,
    merge_asset_model, possibly_broken_assets, shared_assets, to_public,
)
from agent.task_graph import Task, SUCCESS, FAILED                  # noqa: E402


def task(name, tool, args=None, status=SUCCESS):
    return Task(id=name, name=name, tool=tool, args=args or {}, status=status)


class ExtractAssetRefsTests(unittest.TestCase):
    def test_an_rbxassetid_url_is_extracted(self):
        refs = extract_asset_refs({"texture_id": "rbxassetid://123456"})
        self.assertEqual(refs, [{"asset_id": "123456", "kind": "texture"}])

    def test_a_bare_numeric_id_under_a_hinted_key_is_extracted(self):
        refs = extract_asset_refs({"sound_id": "987654"})
        self.assertEqual(refs[0]["asset_id"], "987654")
        self.assertEqual(refs[0]["kind"], "sound")

    def test_an_unhinted_numeric_value_is_not_extracted(self):
        self.assertEqual(extract_asset_refs({"count": "5"}), [])

    def test_non_dict_args_are_safe(self):
        self.assertEqual(extract_asset_refs(None), [])


class MergeAssetModelTests(unittest.TestCase):
    def test_empty_model_has_no_assets(self):
        self.assertEqual(empty_model(), {"assets": {}})

    def test_a_successful_reference_is_tracked_with_its_owner(self):
        model = merge_asset_model(None, [
            task("t1", "create_part", {
                "name": "Rock1", "mesh_id": "rbxassetid://111"})], run_no=1)
        self.assertIn("111", model["assets"])
        self.assertEqual(dependents_of_asset(model, "111"), ["Rock1"])

    def test_shared_assets_tracks_multiple_owners(self):
        model = merge_asset_model(None, [
            task("t1", "create_part", {
                "name": "Rock1", "mesh_id": "rbxassetid://111"}),
            task("t2", "create_part", {
                "name": "Rock2", "mesh_id": "rbxassetid://111"}),
        ], run_no=1)
        shared = shared_assets(model)
        self.assertEqual(len(shared), 1)
        self.assertEqual(sorted(shared[0]["referenced_by"]),
                         ["Rock1", "Rock2"])

    def test_a_single_owner_asset_is_not_shared(self):
        model = merge_asset_model(None, [
            task("t1", "create_part", {
                "name": "Rock1", "mesh_id": "rbxassetid://111"})], run_no=1)
        self.assertEqual(shared_assets(model), [])

    def test_possibly_broken_only_ever_failed(self):
        model = merge_asset_model(None, [
            task("t1", "create_part", {
                "name": "Rock1", "mesh_id": "rbxassetid://111"},
                status=FAILED)], run_no=1)
        broken = possibly_broken_assets(model)
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["asset_id"], "111")

    def test_a_later_success_clears_the_possibly_broken_flag(self):
        model = merge_asset_model(None, [
            task("t1", "create_part", {
                "name": "Rock1", "mesh_id": "rbxassetid://111"},
                status=FAILED)], run_no=1)
        model2 = merge_asset_model(model, [
            task("t2", "create_part", {
                "name": "Rock1", "mesh_id": "rbxassetid://111"})], run_no=2)
        self.assertEqual(possibly_broken_assets(model2), [])


class ToPublicTests(unittest.TestCase):
    def test_to_public_is_honest_about_what_it_cannot_detect(self):
        pub = to_public(empty_model())
        self.assertIn("unavailable through current MCP capability",
                      pub["note"])
        self.assertEqual(pub["asset_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
