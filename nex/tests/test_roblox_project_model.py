"""Roblox DataModel project model (roblox/project_model.py).

Proves: instances are only ever known from successful calls' own
arguments (never invented), service/script/remote/UI classification is
deterministic from ClassName, parent/child hierarchy is tracked
explicitly, and the model is bounded and cross-run mergeable.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-project-model-tests")

from roblox.project_model import (                                 # noqa: E402
    MAX_NODES, children_of, empty_model, extract_declarations,
    merge_project_model, nodes_of_kind, to_public,
)
from agent.task_graph import Task, SUCCESS, FAILED                 # noqa: E402


def task(name, tool, args=None, status=SUCCESS):
    return Task(id=name, name=name, tool=tool, args=args or {}, status=status)


class ExtractDeclarationsTests(unittest.TestCase):
    def test_no_name_declares_nothing(self):
        self.assertEqual(extract_declarations({"class_name": "Script"}), [])

    def test_a_named_instance_is_declared(self):
        decl = extract_declarations({
            "name": "PurchaseItem", "class_name": "RemoteEvent",
            "parent": "ReplicatedStorage"})
        self.assertEqual(decl, [{"name": "PurchaseItem",
                                "class_name": "RemoteEvent",
                                "parent": "ReplicatedStorage"}])

    def test_non_dict_args_are_safe(self):
        self.assertEqual(extract_declarations(None), [])
        self.assertEqual(extract_declarations("nope"), [])


class MergeProjectModelTests(unittest.TestCase):
    def test_empty_model_has_no_nodes(self):
        self.assertEqual(empty_model(), {"nodes": {}, "children": {}})

    def test_a_remote_event_is_classified_as_a_remote(self):
        model = merge_project_model(None, [
            task("t1", "create_remote_event", {
                "name": "PurchaseItem", "class_name": "RemoteEvent",
                "parent": "ReplicatedStorage"})], run_no=1)
        remotes = nodes_of_kind(model, "remote")
        self.assertEqual(len(remotes), 1)
        self.assertEqual(remotes[0]["name"], "PurchaseItem")

    def test_a_service_name_is_classified_as_a_service_even_without_class(self):
        model = merge_project_model(None, [
            task("t1", "list_instances", {"parent": "ReplicatedStorage",
                                          "name": "ReplicatedStorage"})],
            run_no=1)
        services = nodes_of_kind(model, "service")
        self.assertTrue(any(s["name"] == "ReplicatedStorage"
                           for s in services))

    def test_a_module_script_is_classified_as_a_script(self):
        model = merge_project_model(None, [
            task("t1", "create_script", {
                "name": "InventoryService", "class_name": "ModuleScript",
                "parent": "ServerScriptService"})], run_no=1)
        scripts = nodes_of_kind(model, "script")
        self.assertEqual(scripts[0]["name"], "InventoryService")

    def test_parent_child_hierarchy_is_tracked(self):
        model = merge_project_model(None, [
            task("t1", "create_script", {
                "name": "InventoryService", "class_name": "ModuleScript",
                "parent": "ServerScriptService"})], run_no=1)
        kids = children_of(model, "ServerScriptService")
        self.assertIn("ServerScriptService.InventoryService", kids)

    def test_only_successful_calls_declare_anything(self):
        model = merge_project_model(None, [
            task("t1", "create_script", {
                "name": "Broken", "class_name": "Script"},
                status=FAILED)], run_no=1)
        self.assertEqual(model["nodes"], {})

    def test_repeated_mentions_across_runs_accumulate(self):
        decl_task = task("t1", "set_property", {
            "name": "InventoryService", "class_name": "ModuleScript",
            "parent": "ServerScriptService"})
        m1 = merge_project_model(None, [decl_task], run_no=1)
        m2 = merge_project_model(m1, [decl_task], run_no=2)
        key = "ServerScriptService.InventoryService"
        self.assertEqual(m2["nodes"][key]["mentions"], 2)
        self.assertEqual(m2["nodes"][key]["first_seen_run"], 1)
        self.assertEqual(m2["nodes"][key]["last_seen_run"], 2)

    def test_node_count_is_bounded(self):
        tasks = [task("t%d" % i, "create_instance", {
            "name": "Part%d" % i, "class_name": "Part",
            "parent": "Workspace"}) for i in range(MAX_NODES + 20)]
        model = merge_project_model(None, tasks, run_no=1)
        self.assertLessEqual(len(model["nodes"]), MAX_NODES)

    def test_a_call_with_no_name_declares_nothing(self):
        model = merge_project_model(None, [
            task("t1", "list_instances", {"parent": "Workspace"})], run_no=1)
        self.assertEqual(model["nodes"], {})


class ToPublicTests(unittest.TestCase):
    def test_to_public_summarizes_by_kind_and_is_honest(self):
        model = merge_project_model(None, [
            task("t1", "create_remote_event", {
                "name": "PurchaseItem", "class_name": "RemoteEvent",
                "parent": "ReplicatedStorage"})], run_no=1)
        pub = to_public(model)
        self.assertEqual(pub["node_count"], 1)
        self.assertEqual(pub["by_kind"].get("remote"), 1)
        self.assertIn("not a full DataModel dump", pub["note"])

    def test_to_public_on_an_empty_model_is_safe(self):
        pub = to_public(empty_model())
        self.assertEqual(pub["node_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
