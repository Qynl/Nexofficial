"""RunContext — bounded working memory for a single agent run.

Covers the memory-growth guarantees: per-observation truncation, the
full/compacted evidence window, the failures cap, and the hard retention
ceiling that protects a very long (or misconfigured) run from growing
RunContext.observations without bound.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

from agent.context import (  # noqa: E402
    MAX_RETAINED_OBSERVATIONS, RunContext, result_to_text, truncate_text,
)


def expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        raise SystemExit(1)


class RunContextTests(unittest.TestCase):
    def test_observation_truncated_to_budget(self):
        ctx = RunContext("goal", result_budget=100)
        text = ctx.observe("s1", "tool", "server", "x" * 5000)
        self.assertLessEqual(len(text), 200)   # budget + marker overhead
        self.assertIn("omitted", text)

    def test_evidence_block_recent_full_older_compacted(self):
        ctx = RunContext("goal", keep_recent=2)
        for i in range(5):
            ctx.observe("step-%d" % i, "tool", "server", "result %d" % i)
        block = ctx.evidence_block()
        self.assertIn("Earlier (compacted):", block)
        self.assertIn("Recent:", block)
        # the most recent `keep_recent` are shown in full
        self.assertIn("result 4", block)
        self.assertIn("result 3", block)

    def test_failures_capped(self):
        ctx = RunContext("goal")
        for i in range(50):
            ctx.record_failure("step", "tool", "unique failure %d" % i)
        self.assertLessEqual(len(ctx.failures), ctx.max_failures)

    def test_failures_dedup(self):
        ctx = RunContext("goal")
        for _ in range(10):
            ctx.record_failure("step", "tool", "same failure")
        self.assertEqual(len(ctx.failures), 1)

    def test_observations_do_not_grow_without_bound(self):
        """A very long run (or a misconfigured NEX_MAX_STEPS) must not let
        RunContext.observations grow forever."""
        ctx = RunContext("goal")
        for i in range(5000):
            ctx.observe("step-%d" % i, "tool", "server", "r%d" % i)
        self.assertLess(len(ctx.observations), MAX_RETAINED_OBSERVATIONS + 200)

    def test_trimming_does_not_change_what_the_model_sees(self):
        """Trimming only discards entries evidence_block() could never show
        anyway — the rendered prompt must be identical with or without it."""
        ctx_small = RunContext("goal")
        ctx_big = RunContext("goal")
        # Push both well past the trim threshold; the untrimmed one is
        # simulated by disabling the cap via a huge ceiling override.
        import agent.context as context_mod
        original = context_mod.MAX_RETAINED_OBSERVATIONS
        try:
            for i in range(400):
                ctx_small.observe("step-%d" % i, "tool", "server",
                                  "result %d" % i)
            context_mod.MAX_RETAINED_OBSERVATIONS = 10 ** 9
            for i in range(400):
                ctx_big.observe("step-%d" % i, "tool", "server",
                                "result %d" % i)
        finally:
            context_mod.MAX_RETAINED_OBSERVATIONS = original
        self.assertEqual(ctx_small.evidence_block(), ctx_big.evidence_block())
        self.assertLess(len(ctx_small.observations), len(ctx_big.observations))

    def test_size_chars_reflects_retained_window(self):
        ctx = RunContext("goal", result_budget=10)
        for i in range(10):
            ctx.observe("s", "tool", "server", "x")
        self.assertGreater(ctx.size_chars(), 0)

    def test_truncate_text_preserves_head_and_tail(self):
        text = "HEAD" + ("x" * 5000) + "TAIL"
        out, truncated = truncate_text(text, budget=200)
        self.assertTrue(truncated)
        self.assertTrue(out.startswith("HEAD"))
        self.assertTrue(out.endswith("TAIL"))

    def test_result_to_text_handles_mcp_envelope(self):
        result = {"content": [{"type": "text", "text": "hello"}],
                  "isError": False}
        text, _ = result_to_text(result)
        self.assertEqual(text, "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
