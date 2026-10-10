"""Remote contract system (roblox/networking.py).

Proves contracts are built from real project-model evidence, purpose/
validation fields start empty and are only ever filled by an auditable
annotate_contract() call, and the rate-limit signal requires a genuinely
separate, correlated piece of evidence rather than a guess.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-networking-tests")

from roblox.networking import (                                    # noqa: E402
    annotate_contract, build_contracts, unvalidated_contracts,
)
from roblox.project_model import merge_project_model               # noqa: E402
from agent.task_graph import Task, SUCCESS                          # noqa: E402


def task(name, tool, args=None, status=SUCCESS):
    return Task(id=name, name=name, tool=tool, args=args or {}, status=status)


class BuildContractsTests(unittest.TestCase):
    def test_no_remotes_means_no_contracts(self):
        model = merge_project_model(None, [], run_no=1)
        self.assertEqual(build_contracts(model), [])

    def test_a_remote_event_becomes_a_contract_skeleton(self):
        model = merge_project_model(None, [
            task("t1", "create_remote_event", {
                "name": "PurchaseItem", "class_name": "RemoteEvent",
                "parent": "ReplicatedStorage"})], run_no=1)
        contracts = build_contracts(model)
        self.assertEqual(len(contracts), 1)
        c = contracts[0]
        self.assertEqual(c["name"], "PurchaseItem")
        self.assertEqual(c["type"], "RemoteEvent")
        self.assertEqual(c["purpose"], "")
        self.assertEqual(c["server_validates"], [])
        self.assertIsNone(c["enrichment_source"])

    def test_remote_function_is_classified_correctly(self):
        model = merge_project_model(None, [
            task("t1", "create_remote_function", {
                "name": "GetInventory", "class_name": "RemoteFunction",
                "parent": "ReplicatedStorage"})], run_no=1)
        c = build_contracts(model)[0]
        self.assertEqual(c["type"], "RemoteFunction")

    def test_rate_limit_evidence_requires_a_correlated_separate_call(self):
        model = merge_project_model(None, [
            task("t1", "create_remote_event", {
                "name": "PurchaseItem", "class_name": "RemoteEvent",
                "parent": "ReplicatedStorage"})], run_no=1)
        no_evidence = build_contracts(model, tasks=[])
        self.assertFalse(no_evidence[0]["rate_limited"])

        rate_limit_task = task("t2", "apply_rate_limit",
                               {"target": "PurchaseItem"})
        with_evidence = build_contracts(model, tasks=[rate_limit_task])
        self.assertTrue(with_evidence[0]["rate_limited"])


class AnnotateContractTests(unittest.TestCase):
    def test_annotation_records_an_explicit_source(self):
        model = merge_project_model(None, [
            task("t1", "create_remote_event", {
                "name": "PurchaseItem", "class_name": "RemoteEvent",
                "parent": "ReplicatedStorage"})], run_no=1)
        c = build_contracts(model)[0]
        annotated = annotate_contract(
            c, purpose="Request an item purchase",
            server_validates=["item_exists", "currency_is_sufficient"],
            source="llm-diagnosis")
        self.assertEqual(annotated["purpose"], "Request an item purchase")
        self.assertEqual(annotated["enrichment_source"], "llm-diagnosis")
        # The original contract is untouched (no silent mutation).
        self.assertEqual(c["purpose"], "")

    def test_unvalidated_contracts_surfaces_the_real_gap(self):
        model = merge_project_model(None, [
            task("t1", "create_remote_event", {
                "name": "PurchaseItem", "class_name": "RemoteEvent",
                "parent": "ReplicatedStorage"})], run_no=1)
        contracts = build_contracts(model)
        self.assertEqual(unvalidated_contracts(contracts), contracts)
        annotated = [annotate_contract(
            contracts[0], server_validates=["item_exists"])]
        self.assertEqual(unvalidated_contracts(annotated), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
