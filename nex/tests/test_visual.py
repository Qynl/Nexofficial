"""Iterative visual-critique defect tracking (agent/visual.py).

The AI screenshot critique in agent/loop.py is honest but, on its own,
only prose: nothing forced an open defect to actually become future work.
This module turns a critique's own words into tracked categories and an
explicit planning objective — these tests prove that loop end to end,
not just that keywords match.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-visual-tests")

from agent.visual import (                                      # noqa: E402
    VisualIssueBoard, classify_defects,
)


class ClassifyDefectsTests(unittest.TestCase):
    def test_concrete_phrases_are_recognized(self):
        self.assertIn("placeholder_assets",
                      classify_defects("the walls are an untextured "
                                       "placeholder grey box"))
        self.assertIn("lighting",
                      classify_defects("this is just the default lighting, "
                                       "totally flat and washed out"))
        self.assertIn("seams",
                      classify_defects("there is visible z-fighting between "
                                       "the floor tiles"))

    def test_bare_severity_words_prove_nothing(self):
        # "bad" or "needs work" alone names no concrete defect; a vague
        # critique must not be hallucinated into a specific category.
        self.assertEqual(classify_defects("this looks pretty bad honestly"),
                         set())
        self.assertEqual(classify_defects(""), set())
        self.assertEqual(classify_defects(None), set())

    def test_multiple_categories_in_one_critique(self):
        cats = classify_defects(
            "Moody lighting, but the counters are an untextured "
            "placeholder grey box and the HUD overlap makes the text "
            "unreadable.")
        self.assertIn("placeholder_assets", cats)
        self.assertIn("ui_hierarchy", cats)


class VisualIssueBoardTests(unittest.TestCase):
    def test_first_mention_opens_an_issue(self):
        board = VisualIssueBoard()
        mentioned = board.record(
            "capture-kitchen",
            "the counters are an untextured placeholder grey box")
        self.assertIn("placeholder_assets", mentioned)
        open_issues = board.open_issues()
        self.assertEqual(len(open_issues), 1)
        self.assertEqual(open_issues[0].category, "placeholder_assets")
        self.assertEqual(open_issues[0].mentions, 1)
        self.assertEqual(open_issues[0].status, "open")

    def test_recurring_defect_increments_mentions_and_stays_open(self):
        board = VisualIssueBoard()
        board.record("capture-1", "default lighting, flat and washed out")
        board.record("capture-2", "still default lighting here too")
        open_issues = board.open_issues()
        self.assertEqual(len(open_issues), 1)
        self.assertEqual(open_issues[0].mentions, 2)
        self.assertEqual(open_issues[0].last_step, "capture-2")

    def test_defect_absent_from_a_later_critique_is_marked_likely_resolved(self):
        board = VisualIssueBoard()
        board.record("capture-1", "default lighting, totally flat")
        board.record("capture-2", "lighting looks great now, no more issues")
        self.assertEqual(board.open_issues(), [])
        public = board.to_public()
        self.assertEqual(len(public["likely_resolved"]), 1)
        self.assertEqual(public["likely_resolved"][0]["category"], "lighting")
        self.assertIn("heuristic", public["note"])

    def test_a_single_critique_never_marks_anything_resolved(self):
        # Nothing to compare against yet — no false "fixed" claim on the
        # very first screenshot of a run.
        board = VisualIssueBoard()
        board.record("capture-1", "default lighting, totally flat")
        self.assertEqual(board.to_public()["likely_resolved"], [])
        self.assertEqual(len(board.open_issues()), 1)

    def test_planning_note_is_empty_with_nothing_open(self):
        board = VisualIssueBoard()
        self.assertEqual(board.planning_note(), "")
        board.record("capture-1", "looks great, no notes")
        self.assertEqual(board.planning_note(), "")

    def test_planning_note_lists_open_defects_for_the_next_plan(self):
        board = VisualIssueBoard()
        board.record("capture-1", "default lighting and an untextured "
                                  "placeholder grey box")
        note = board.planning_note()
        self.assertIn("OPEN VISUAL DEFECTS", note)
        self.assertIn("Default-looking or flat lighting", note)
        self.assertIn("Placeholder/default assets still in view", note)
        self.assertIn("capture-1", note)

    def test_planning_note_prioritizes_most_recurring_defects(self):
        board = VisualIssueBoard()
        board.record("c1", "default lighting")
        board.record("c2", "still default lighting")
        board.record("c3", "default lighting again, and now a visible seam")
        # Lighting recurred 3x vs the seam's 1x — it must be the one kept
        # under a tight limit (the seam's OWN category entry is dropped,
        # even though the raw quoted sentence that proves lighting also
        # happens to mention it).
        top = sorted(board.open_issues(), key=lambda i: i.mentions,
                    reverse=True)
        self.assertEqual(top[0].category, "lighting")
        note = board.planning_note(limit=1)
        self.assertIn("lighting", note.lower())
        self.assertNotIn("visible seams, z-fighting", note.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
