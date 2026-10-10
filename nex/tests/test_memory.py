"""Persistent structured project memory (agent/memory.py).

A long production is not one run — it's a conversation resumed across
many runs. These tests prove memory is built only from real structural
evidence (never invented), stays bounded, and genuinely carries bugs,
milestones, visual defects, and recurring risks forward between runs
without ever requiring a raw history dump into the prompt.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-memory-tests")

from agent.memory import (                                          # noqa: E402
    MEMORY_CONFIRMED, MEMORY_INFERRED, MEMORY_STALE, MEMORY_UNVERIFIED,
    STALE_AFTER_RUNS, empty_memory, merge_from_run, to_prompt_block,
)


class EmptyAndFirstRunTests(unittest.TestCase):
    def test_empty_memory_renders_nothing(self):
        self.assertEqual(to_prompt_block(None), "")
        self.assertEqual(to_prompt_block(empty_memory("c1")), "")

    def test_first_run_establishes_baseline(self):
        mem = merge_from_run(
            None, conversation_id="c1",
            milestones_done=["discovery", "foundation"],
            milestones_open=["systems", "world_content"],
            failed=[{"tool": "build_project", "error": "build failed",
                    "failure_kind": "compilation"}],
            tool_outcomes=[{"tool": "build_project", "ok": False},
                          {"tool": "inspect_uproject", "ok": True}],
            visual_public={"open": [{"category": "lighting",
                                     "label": "Flat lighting"}],
                          "likely_resolved": []},
            performance_evidence=["60fps on empty level"],
        )
        self.assertEqual(mem["runs_recorded"], 1)
        self.assertEqual(mem["milestones_done"], ["discovery", "foundation"])
        self.assertEqual(mem["milestones_open"], ["systems", "world_content"])
        self.assertEqual(len(mem["known_bugs"]), 1)
        self.assertEqual(mem["known_bugs"][0]["occurrences"], 1)
        self.assertEqual(mem["tool_failure_counts"]["build_project"], 1)
        self.assertEqual(mem["tool_success_counts"]["inspect_uproject"], 1)
        self.assertEqual(len(mem["visual_issues_open"]), 1)

        block = to_prompt_block(mem)
        self.assertIn("1 earlier run", block)
        self.assertIn("discovery", block)
        self.assertIn("systems", block)
        self.assertIn("build_project", block)
        self.assertIn("Flat lighting", block)


class MultiRunEvolutionTests(unittest.TestCase):
    def test_a_recurring_bug_increments_and_becomes_a_risk(self):
        mem = merge_from_run(
            None, conversation_id="c1",
            failed=[{"tool": "compile_blueprint", "error": "syntax error",
                    "failure_kind": "compilation"}],
            tool_outcomes=[{"tool": "compile_blueprint", "ok": False}],
        )
        self.assertEqual(mem["risks"], [])   # one occurrence is not a risk

        mem2 = merge_from_run(
            mem, conversation_id="c1",
            failed=[{"tool": "compile_blueprint", "error": "syntax error #2",
                    "failure_kind": "compilation"}],
            tool_outcomes=[{"tool": "compile_blueprint", "ok": False}],
        )
        self.assertEqual(mem2["known_bugs"][0]["occurrences"], 2)
        self.assertEqual(mem2["known_bugs"][0]["last_seen_run"], 2)
        self.assertTrue(any("compile_blueprint" in r for r in mem2["risks"]))

    def test_a_bug_is_only_marked_resolved_after_the_tool_later_succeeds(self):
        mem = merge_from_run(
            None, conversation_id="c1",
            failed=[{"tool": "package_project", "error": "cook failed",
                    "failure_kind": "compilation"}],
            tool_outcomes=[{"tool": "package_project", "ok": False}],
        )
        self.assertEqual(len(mem["known_bugs"]), 1)
        self.assertEqual(mem["resolved_bugs"], [])

        # Next run: the bug isn't mentioned in `failed` AND the same tool
        # has NOT succeeded yet — it must stay open, not vanish.
        mem_silent = merge_from_run(
            mem, conversation_id="c1", failed=[], tool_outcomes=[])
        self.assertEqual(len(mem_silent["known_bugs"]), 1)
        self.assertEqual(mem_silent["resolved_bugs"], [])

        # Next run: the same tool now succeeds — only now is it resolved.
        mem_fixed = merge_from_run(
            mem_silent, conversation_id="c1", failed=[],
            tool_outcomes=[{"tool": "package_project", "ok": True}])
        self.assertEqual(mem_fixed["known_bugs"], [])
        self.assertEqual(len(mem_fixed["resolved_bugs"]), 1)
        self.assertEqual(mem_fixed["resolved_bugs"][0]["tool"],
                         "package_project")

    def test_milestones_accumulate_and_never_double_count(self):
        mem = merge_from_run(
            None, conversation_id="c1",
            milestones_done=["discovery"],
            milestones_open=["foundation", "systems"])
        mem2 = merge_from_run(
            mem, conversation_id="c1",
            milestones_done=["discovery", "foundation"],
            milestones_open=["systems"])
        self.assertEqual(mem2["milestones_done"], ["discovery", "foundation"])
        self.assertEqual(mem2["milestones_open"], ["systems"])

    def test_tool_tallies_accumulate_across_runs(self):
        mem = merge_from_run(
            None, conversation_id="c1",
            tool_outcomes=[{"tool": "spawn_actor", "ok": True}])
        mem2 = merge_from_run(
            mem, conversation_id="c1",
            tool_outcomes=[{"tool": "spawn_actor", "ok": True},
                          {"tool": "spawn_actor", "ok": False}])
        self.assertEqual(mem2["tool_success_counts"]["spawn_actor"], 2)
        self.assertEqual(mem2["tool_failure_counts"]["spawn_actor"], 1)

    def test_lists_stay_bounded_across_many_runs(self):
        mem = None
        for i in range(30):
            mem = merge_from_run(
                mem, conversation_id="c1",
                milestones_done=["stage-%d" % i],
                failed=[{"tool": "tool-%d" % i, "error": "x",
                        "failure_kind": "unknown"}],
                tool_outcomes=[{"tool": "tool-%d" % i, "ok": False}])
        self.assertLessEqual(len(mem["milestones_done"]), 20)
        self.assertLessEqual(len(mem["known_bugs"]), 20)

    def test_prompt_block_is_bounded_even_with_many_findings(self):
        mem = None
        for i in range(10):
            mem = merge_from_run(
                mem, conversation_id="c1",
                failed=[{"tool": "t%d" % i, "error": "e" * 300,
                        "failure_kind": "runtime"}],
                tool_outcomes=[{"tool": "t%d" % i, "ok": False}])
        block = to_prompt_block(mem, char_budget=500)
        self.assertLessEqual(len(block), 540)   # budget + truncation marker


class LoopIntegrationTests(unittest.TestCase):
    """Prove the wiring inside agent/loop.py itself, not just the pure
    merge logic above: a real AgentRun must emit project_memory in its
    final report, and a SECOND AgentRun seeded with the first run's
    memory must actually see it in its planning prompt."""

    def _llm_factory(self, fail_tool, step_name="build-it"):
        import json as _json

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                self._seen_prompt = messages[-1]["content"]
                return _json.dumps({"plan": {
                    "title": "Build", "rationale": "x",
                    "steps": [{"name": step_name, "title": "Build it",
                              "tool": "engine." + fail_tool, "args": {}}],
                }})
            return _json.dumps({"done": False, "adjust": "stop",
                               "reason": "it is broken"})
        return llm

    def test_a_runs_project_memory_reaches_the_next_runs_planning_prompt(self):
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                    "inputSchema": {"type": "object", "properties": {}}}

        server = MockMCPServer("engine", [tool("build_project")])
        manager = FakeManager([server], fail={
            "build_project": [99, "UnrealBuildTool: build failed"]})

        run1 = AgentRun("r1", "build the project", manager,
                       llm=self._llm_factory("build_project"),
                       max_steps=3, max_replans=0)
        report1 = run1.run()
        mem1 = report1.get("project_memory")
        self.assertIsNotNone(mem1)
        self.assertEqual(mem1["runs_recorded"], 1)
        self.assertEqual(len(mem1["known_bugs"]), 1)
        self.assertEqual(mem1["known_bugs"][0]["tool"], "build_project")

        # A second run in the SAME conversation, seeded with run1's memory,
        # must actually plan with it visible — not rediscover from scratch.
        run2 = AgentRun("r2", "build the project", manager,
                       llm=self._llm_factory("build_project",
                                            step_name="build-it-2"),
                       max_steps=3, max_replans=0,
                       project_memory=mem1)
        report2 = run2.run()
        self.assertIn("PROJECT MEMORY", self._seen_prompt)
        self.assertIn("build_project", self._seen_prompt)
        mem2 = report2.get("project_memory")
        self.assertEqual(mem2["runs_recorded"], 2)
        self.assertEqual(mem2["known_bugs"][0]["occurrences"], 2)
        self.assertTrue(any("build_project" in r for r in mem2["risks"]))


class ConfidenceStateTests(unittest.TestCase):
    """Point 13: every fact carries CONFIRMED/INFERRED/UNVERIFIED/STALE,
    derived purely from when real evidence last touched it — never from
    an LLM's self-report."""

    def test_a_milestone_reaffirmed_this_run_is_confirmed(self):
        mem = merge_from_run(None, conversation_id="c1",
                             milestones_done=["discovery"])
        self.assertEqual(
            mem["milestone_confidence"]["discovery"]["state"],
            MEMORY_CONFIRMED)

    def test_a_milestone_not_mentioned_again_is_unverified_not_confirmed(
            self):
        mem = merge_from_run(None, conversation_id="c1",
                             milestones_done=["discovery"])
        mem2 = merge_from_run(mem, conversation_id="c1", milestones_done=[])
        self.assertEqual(
            mem2["milestone_confidence"]["discovery"]["state"],
            MEMORY_UNVERIFIED)

    def test_a_milestone_untouched_for_enough_runs_goes_stale(self):
        mem = merge_from_run(None, conversation_id="c1",
                             milestones_done=["discovery"])
        for _ in range(STALE_AFTER_RUNS + 1):
            mem = merge_from_run(mem, conversation_id="c1",
                                 milestones_done=[])
        self.assertEqual(
            mem["milestone_confidence"]["discovery"]["state"], MEMORY_STALE)

    def test_a_bug_failing_again_this_run_is_confirmed(self):
        mem = merge_from_run(
            None, conversation_id="c1",
            failed=[{"tool": "compile_blueprint", "error": "x",
                    "failure_kind": "compilation"}],
            tool_outcomes=[{"tool": "compile_blueprint", "ok": False}])
        self.assertEqual(mem["known_bugs"][0]["confidence"], MEMORY_CONFIRMED)

    def test_a_bug_silently_carried_forward_is_unverified_not_confirmed(
            self):
        mem = merge_from_run(
            None, conversation_id="c1",
            failed=[{"tool": "compile_blueprint", "error": "x",
                    "failure_kind": "compilation"}],
            tool_outcomes=[{"tool": "compile_blueprint", "ok": False}])
        mem2 = merge_from_run(mem, conversation_id="c1", failed=[],
                              tool_outcomes=[])
        self.assertEqual(mem2["known_bugs"][0]["confidence"],
                         MEMORY_UNVERIFIED)

    def test_a_stale_milestone_is_flagged_in_the_rendered_prompt_block(self):
        mem = merge_from_run(None, conversation_id="c1",
                             milestones_done=["discovery"])
        for _ in range(STALE_AFTER_RUNS + 1):
            mem = merge_from_run(mem, conversation_id="c1",
                                 milestones_done=[])
        block = to_prompt_block(mem)
        self.assertIn("discovery (STALE", block)

    def test_a_resolved_bug_is_always_inferred_never_confirmed(self):
        mem = merge_from_run(
            None, conversation_id="c1",
            failed=[{"tool": "package_project", "error": "x",
                    "failure_kind": "compilation"}],
            tool_outcomes=[{"tool": "package_project", "ok": False}])
        mem2 = merge_from_run(
            mem, conversation_id="c1", failed=[],
            tool_outcomes=[{"tool": "package_project", "ok": True}])
        self.assertEqual(mem2["resolved_bugs"][0]["confidence"],
                         MEMORY_INFERRED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
