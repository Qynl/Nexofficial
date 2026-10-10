"""Granular Roblox capability model (roblox/capabilities.py).

Proves the core claim of the brief: a tool existing never promotes a
capability past PARTIAL on its own; AVAILABLE requires this run's own
successful-call evidence, and multi-signal capabilities (REMOTE_VALIDATION,
RUNTIME_PLAYTEST, MULTI_CLIENT_PLAYTEST, PERSISTENCE_TESTING) need every
required kind of evidence, not just one.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-roblox-capabilities-tests")

from roblox.capabilities import (                                  # noqa: E402
    AVAILABLE, CONFIRMED, INFERRED, PARTIAL, UNAVAILABLE, UNKNOWN,
    UNVERIFIED, assess_capability, capability, capability_report,
)
from agent.task_graph import Task, SUCCESS, FAILED                 # noqa: E402


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


class NoRegistryTests(unittest.TestCase):
    def test_every_capability_is_unknown_without_a_registry(self):
        report = capability_report(None, [])
        self.assertTrue(report)
        self.assertTrue(all(c["state"] == UNKNOWN for c in report.values()))
        self.assertTrue(all(c["confidence"] == UNVERIFIED
                           for c in report.values()))


class SimpleCapabilityTests(unittest.TestCase):
    def test_no_matching_tool_is_unavailable(self):
        cap = capability("DATAMODEL_INSPECTION")
        result = assess_capability(cap, FakeRegistry(["unrelated_tool"]), [])
        self.assertEqual(result["state"], UNAVAILABLE)
        self.assertEqual(result["confidence"], UNVERIFIED)

    def test_a_connected_but_unused_tool_is_only_partial(self):
        cap = capability("DATAMODEL_INSPECTION")
        result = assess_capability(cap, FakeRegistry(["list_instances"]), [])
        self.assertEqual(result["state"], PARTIAL)
        self.assertEqual(result["confidence"], INFERRED)

    def test_a_successful_call_this_run_makes_it_available(self):
        cap = capability("DATAMODEL_INSPECTION")
        registry = FakeRegistry(["list_instances"])
        result = assess_capability(
            cap, registry, [task("t1", "list_instances")])
        self.assertEqual(result["state"], AVAILABLE)
        self.assertEqual(result["confidence"], CONFIRMED)

    def test_a_failed_call_never_counts_as_proof(self):
        cap = capability("DATAMODEL_INSPECTION")
        registry = FakeRegistry(["list_instances"])
        result = assess_capability(
            cap, registry, [task("t1", "list_instances", status=FAILED)])
        self.assertEqual(result["state"], PARTIAL)


class MultiSignalCapabilityTests(unittest.TestCase):
    def test_remote_validation_needs_a_remote_and_a_server_script(self):
        cap = capability("REMOTE_VALIDATION")
        registry = FakeRegistry(["create_remote_event", "edit_server_script"])
        # Only the remote was actually called successfully -> PARTIAL.
        partial = assess_capability(
            cap, registry, [task("t1", "create_remote_event")])
        self.assertEqual(partial["state"], PARTIAL)
        # Both kinds of evidence present -> AVAILABLE.
        full = assess_capability(cap, registry, [
            task("t1", "create_remote_event"),
            task("t2", "edit_server_script"),
        ])
        self.assertEqual(full["state"], AVAILABLE)
        self.assertEqual(full["confidence"], CONFIRMED)

    def test_a_tool_existing_never_alone_proves_runtime_playtest(self):
        cap = capability("RUNTIME_PLAYTEST")
        registry = FakeRegistry(["start_play", "studio_output"])
        only_started = assess_capability(
            cap, registry, [task("t1", "start_play")])
        self.assertEqual(only_started["state"], PARTIAL,
                         "starting a session alone must never be AVAILABLE")
        started_and_observed = assess_capability(cap, registry, [
            task("t1", "start_play"), task("t2", "studio_output"),
        ])
        self.assertEqual(started_and_observed["state"], AVAILABLE)

    def test_multi_client_requires_more_than_one_client_session(self):
        cap = capability("MULTI_CLIENT_PLAYTEST")
        registry = FakeRegistry(["start_client"])
        one_client = assess_capability(
            cap, registry, [task("t1", "start_client")])
        self.assertEqual(one_client["state"], PARTIAL)
        two_clients = assess_capability(cap, registry, [
            task("t1", "start_client"), task("t2", "start_client"),
        ])
        self.assertEqual(two_clients["state"], AVAILABLE)

    def test_persistence_needs_two_calls_not_one(self):
        cap = capability("PERSISTENCE_TESTING")
        registry = FakeRegistry(["datastore_get", "datastore_set"])
        one_call = assess_capability(
            cap, registry, [task("t1", "datastore_get")])
        self.assertEqual(one_call["state"], PARTIAL,
                         "one persistence call only proves a save OR a "
                         "load, never a round-trip")
        two_calls = assess_capability(cap, registry, [
            task("t1", "datastore_set"), task("t2", "datastore_get"),
        ])
        self.assertEqual(two_calls["state"], AVAILABLE)


class CapabilityReportTests(unittest.TestCase):
    def test_report_covers_every_defined_capability(self):
        from roblox.capabilities import CAPABILITIES
        report = capability_report(FakeRegistry([]), [])
        self.assertEqual(set(report), {c.id for c in CAPABILITIES})

    def test_unknown_capability_id_returns_none(self):
        self.assertIsNone(capability("NOT_A_REAL_CAPABILITY"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
