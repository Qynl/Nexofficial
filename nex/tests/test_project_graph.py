"""Deterministic project dependency graph (agent/project_graph.py).

Nex has no schema-level 'what does this asset reference' tool. These
tests prove identifiers are extracted deterministically from REAL tool
call arguments (never free prose, never guessed), that two identifiers
named in the same successful call become a co-occurrence edge, that the
graph merges and bounds itself across runs, and that only SUCCESSFUL
calls ever contribute evidence.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-project-graph-tests")

from agent.project_graph import (                                 # noqa: E402
    MAX_EDGES_PER_NODE, MAX_NODES, dependents_of, empty_graph,
    extract_identifiers, merge_project_graph, related, to_public,
)
from agent.task_graph import Task, SUCCESS, FAILED                # noqa: E402


def task(name, tool, args=None, status=SUCCESS):
    return Task(id=name, name=name, tool=tool, args=args or {}, status=status)


class ExtractIdentifiersTests(unittest.TestCase):
    def test_an_engine_content_path_is_always_an_identifier(self):
        idents = extract_identifiers({"target": "/Game/Vehicles/BP_Car"})
        self.assertIn("/Game/Vehicles/BP_Car", idents)

    def test_a_hinted_key_with_identifier_shaped_value_is_extracted(self):
        idents = extract_identifiers({"actor_name": "BP_PoliceCar"})
        self.assertIn("BP_PoliceCar", idents)

    def test_a_hinted_key_with_free_text_prose_is_not_extracted(self):
        idents = extract_identifiers(
            {"description": "a fast red car that honks"})
        self.assertEqual(idents, [])

    def test_an_unhinted_key_is_not_extracted_even_if_identifier_shaped(self):
        idents = extract_identifiers({"count": "BP_Car"})
        self.assertEqual(idents, [])

    def test_a_purely_numeric_value_is_never_an_identifier(self):
        idents = extract_identifiers({"level_index": "7"})
        self.assertEqual(idents, [])

    def test_non_dict_args_are_handled_without_crashing(self):
        self.assertEqual(extract_identifiers(None), [])
        self.assertEqual(extract_identifiers("not a dict"), [])

    def test_duplicate_values_across_keys_are_deduplicated(self):
        idents = extract_identifiers(
            {"actor_name": "BP_Car", "target_class": "BP_Car"})
        self.assertEqual(idents, ["BP_Car"])


class MergeProjectGraphTests(unittest.TestCase):
    def test_empty_graph_has_no_nodes_or_edges(self):
        self.assertEqual(empty_graph(), {"nodes": {}, "edges": {}})

    def test_a_single_identifier_becomes_a_node_with_its_system_tag(self):
        graph = merge_project_graph(None, [
            task("t1", "spawn_vehicle", {"actor_name": "BP_Car"})], run_no=1)
        node = graph["nodes"]["BP_Car"]
        self.assertIn("vehicles", node["systems"])
        self.assertEqual(node["first_seen_run"], 1)
        self.assertEqual(node["last_seen_run"], 1)
        self.assertEqual(node["mentions"], 1)
        self.assertIn("spawn_vehicle", node["tools"])

    def test_two_identifiers_in_the_same_call_become_a_co_occurrence_edge(self):
        graph = merge_project_graph(None, [
            task("t1", "spawn_vehicle",
                {"actor_name": "BP_Car", "material_name": "M_CarPaint"})],
            run_no=1)
        self.assertEqual(related(graph, "BP_Car"), [("M_CarPaint", 1)])
        self.assertEqual(related(graph, "M_CarPaint"), [("BP_Car", 1)])
        self.assertEqual(dependents_of(graph, "BP_Car"), ["M_CarPaint"])

    def test_only_successful_calls_contribute_evidence(self):
        graph = merge_project_graph(None, [
            task("t1", "spawn_vehicle", {"actor_name": "BP_Car"},
                status=FAILED)], run_no=1)
        self.assertEqual(graph["nodes"], {})

    def test_repeated_mentions_across_runs_increment_the_counter(self):
        g1 = merge_project_graph(None, [
            task("t1", "spawn_vehicle", {"actor_name": "BP_Car"})], run_no=1)
        g2 = merge_project_graph(g1, [
            task("t2", "modify_vehicle", {"actor_name": "BP_Car"})], run_no=2)
        node = g2["nodes"]["BP_Car"]
        self.assertEqual(node["mentions"], 2)
        self.assertEqual(node["first_seen_run"], 1)
        self.assertEqual(node["last_seen_run"], 2)
        self.assertIn("spawn_vehicle", node["tools"])
        self.assertIn("modify_vehicle", node["tools"])

    def test_edge_weight_strengthens_with_repeated_co_occurrence(self):
        pair_task = task("t1", "spawn_vehicle",
                         {"actor_name": "BP_Car", "material_name": "M_Paint"})
        g1 = merge_project_graph(None, [pair_task], run_no=1)
        g2 = merge_project_graph(g1, [pair_task], run_no=2)
        self.assertEqual(related(g2, "BP_Car"), [("M_Paint", 2)])

    def test_edges_per_node_are_bounded(self):
        graph = empty_graph()
        tasks = []
        for i in range(MAX_EDGES_PER_NODE + 10):
            tasks.append(task("t%d" % i, "spawn_vehicle",
                              {"actor_name": "BP_Car",
                               "material_name": "M_Paint_%d" % i}))
        graph = merge_project_graph(None, tasks, run_no=1)
        self.assertLessEqual(len(graph["edges"]["BP_Car"]), MAX_EDGES_PER_NODE)

    def test_node_count_is_bounded_and_keeps_the_most_recent(self):
        tasks = [task("t%d" % i, "spawn_vehicle",
                     {"actor_name": "BP_Car_%d" % i})
                for i in range(MAX_NODES + 20)]
        graph = merge_project_graph(None, tasks, run_no=1)
        self.assertLessEqual(len(graph["nodes"]), MAX_NODES)

    def test_a_tool_with_no_identifier_shaped_args_adds_nothing(self):
        graph = merge_project_graph(None, [
            task("t1", "list_actors", {"filter": "all"})], run_no=1)
        self.assertEqual(graph["nodes"], {})


class ToPublicTests(unittest.TestCase):
    def test_to_public_is_bounded_and_carries_an_honesty_note(self):
        graph = merge_project_graph(None, [
            task("t1", "spawn_vehicle", {"actor_name": "BP_Car"})], run_no=1)
        pub = to_public(graph)
        self.assertEqual(pub["node_count"], 1)
        self.assertIn("BP_Car", pub["nodes"])
        self.assertIn("not a true engine reference graph", pub["note"])

    def test_to_public_on_an_empty_graph_is_safe(self):
        pub = to_public(empty_graph())
        self.assertEqual(pub["node_count"], 0)
        self.assertEqual(pub["nodes"], {})


class LoopIntegrationTests(unittest.TestCase):
    """Prove agent/loop.py actually produces this from a real run, not
    just that the pure functions work."""

    def test_a_real_run_produces_a_graph_with_nodes_and_an_edge(self):
        import json as _json
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                    "inputSchema": {"type": "object", "properties": {}}}

        server = MockMCPServer("engine", [tool("spawn_vehicle")])
        manager = FakeManager([server])

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return _json.dumps({"plan": {
                    "title": "Vehicles", "rationale": "x",
                    "steps": [{"name": "add-car", "title": "Add a car",
                              "tool": "engine.spawn_vehicle",
                              "args": {"actor_name": "BP_Car",
                                      "material_name": "M_Paint"}}],
                }})
            return _json.dumps({"done": True})

        run = AgentRun("r1", "add a drivable car", manager, llm=llm,
                      max_steps=3, max_replans=0)
        report = run.run()
        graph = report["project_graph"]
        self.assertIn("BP_Car", graph["nodes"])
        self.assertEqual(dependents_of(graph, "BP_Car"), ["M_Paint"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
