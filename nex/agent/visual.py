"""Turn AI screenshot critique into tracked, actionable visual defects.

``agent/loop.py`` already asks a vision-capable model to genuinely look at
a captured screenshot (see ``_maybe_visual_critique``). Historically that
critique was pure prose: it went into the final report and nowhere else —
a model could write "this looks like an untouched default scene" on every
single screenshot for the whole run and nothing would ever change.

This module closes that gap without inventing any new tool access:

  * classify the critique's own sentences into named defect categories
    using a bounded keyword vocabulary (never trusting the model's
    self-assessment of severity, only matching concrete nouns it used);
  * track each category across the run: first raised, how many times it
    recurred, and whether a LATER critique of a LATER screenshot stopped
    mentioning it (a heuristic "likely resolved" signal — never a proof);
  * render the still-open defects as an explicit, prioritized objective
    list the planner/evaluator is shown on the next planning or replan
    pass, so "the model wrote a critique" can actually turn into
    "the model was told, in the next plan, to go fix exactly this".

Nothing here calls a tool or claims a defect is fixed. A category is only
ever marked "likely_resolved" from the absence of a mention in a later,
genuinely-performed critique — it is reported as a heuristic, not a test
result, and the public view says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Set, Tuple


# Keyword vocabulary per defect category. These intentionally match the
# concrete, observable language a vision model uses when it actually looks
# at a screenshot, not abstract quality adjectives — "bad" or "poor" alone
# proves nothing, but "placeholder cube" or "z-fighting" names a defect.
CATEGORY_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "lighting": (
        "default lighting", "flat lighting", "flat light", "harsh light",
        "overexposed", "underexposed", "no shadows", "unlit",
        "default skylight", "no mood", "washed out", "blown out",
    ),
    "composition": (
        "poor composition", "composition is weak", "off-center",
        "cluttered frame", "bad framing", "awkward angle", "camera angle",
        "unbalanced", "no focal point",
    ),
    "placeholder_assets": (
        "placeholder", "default cube", "default mesh", "grey box",
        "gray box", "blockout", "untextured", "default material",
        "starter content", "stock asset", "primitive shape",
    ),
    "repetition": (
        "repetitive", "repeated", "copy-paste", "copy paste", "tiling",
        "same asset", "monotonous", "same prop",
    ),
    "scale": (
        "wrong scale", "incorrect scale", "too small", "too large",
        "out of proportion", "scale issue", "oversized", "undersized",
    ),
    "visual_noise": (
        "visual noise", "cluttered", "too busy", "overcrowded",
        "distracting", "noisy scene",
    ),
    "empty_space": (
        "empty space", "feels empty", "barren", "sparse", "vacant",
        "nothing to look at", "void of detail",
    ),
    "ui_hierarchy": (
        "ui hierarchy", "hud is cluttered", "confusing ui", "hud overlap",
        "illegible text", "unreadable text", "poor contrast", "tiny text",
        "ui is unclear",
    ),
    "missing_vfx": (
        "missing vfx", "no particle", "lacks feedback", "no impact effect",
        "no visual feedback", "missing effects",
    ),
    "broken_materials": (
        "broken material", "missing texture", "pink texture",
        "magenta texture", "checkerboard texture", "material error",
        "shader error", "missing shader",
    ),
    "seams": (
        "seam", "z-fighting", "z fighting", "visible seam", "texture seam",
        "gap between", "clipping", "intersecting geometry",
    ),
    "animation_presentation": (
        "stiff animation", "robotic animation", "no animation",
        "t-pose", "tpose", "jerky motion", "animation looks off",
        "lacks weight", "floaty movement",
    ),
}

CATEGORY_LABELS: Dict[str, str] = {
    "lighting": "Default-looking or flat lighting",
    "composition": "Poor shot composition",
    "placeholder_assets": "Placeholder/default assets still in view",
    "repetition": "Repetitive, copy-pasted environment",
    "scale": "Incorrect object/world scale",
    "visual_noise": "Visual clutter/noise",
    "empty_space": "Empty, underdressed space",
    "ui_hierarchy": "Unclear UI/HUD hierarchy",
    "missing_vfx": "Missing VFX/feedback",
    "broken_materials": "Broken or missing materials/textures",
    "seams": "Visible seams, z-fighting, or clipping geometry",
    "animation_presentation": "Weak animation presentation",
}

_WORD_RE = re.compile(r"[a-z0-9]+(?:[ -][a-z0-9]+)*")


def _normalized(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", (text or "").lower())) + " "


def classify_defects(critique: str) -> Set[str]:
    """Bounded keyword match of a critique's text into defect categories.

    Never trusts a bare severity word; every category requires a concrete,
    specific phrase to fire. An empty or unrelated critique classifies to
    no categories rather than guessing.
    """
    if not critique:
        return set()
    blob = _normalized(critique)
    hits: Set[str] = set()
    for category, phrases in CATEGORY_KEYWORDS.items():
        for phrase in phrases:
            needle = " " + " ".join(re.findall(r"[a-z0-9]+", phrase)) + " "
            if needle in blob:
                hits.add(category)
                break
    return hits


@dataclass
class VisualIssue:
    category: str
    label: str
    first_step: str
    last_step: str
    first_text: str
    last_text: str
    mentions: int = 1
    status: str = "open"           # open | likely_resolved

    def to_public(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "label": self.label,
            "status": self.status,
            "mentions": self.mentions,
            "first_raised_at": self.first_step,
            "last_seen_at": self.last_step,
            "last_note": self.last_text[:240],
        }


@dataclass
class VisualIssueBoard:
    """Tracks defect categories across every real critique in one run."""

    issues: Dict[str, VisualIssue] = field(default_factory=dict)
    critiques_recorded: int = 0

    def record(self, step_name: str, critique_text: str) -> Set[str]:
        """Classify one genuine critique and update the issue board.

        Returns the set of categories mentioned THIS time. A category
        previously open that is absent from this critique is marked
        ``likely_resolved`` — a heuristic read of the model's own later
        opinion, never a test result, and reported as such.
        """
        self.critiques_recorded += 1
        mentioned = classify_defects(critique_text)
        for category in mentioned:
            existing = self.issues.get(category)
            if existing is None:
                self.issues[category] = VisualIssue(
                    category=category,
                    label=CATEGORY_LABELS.get(category, category),
                    first_step=step_name, last_step=step_name,
                    first_text=critique_text[:240],
                    last_text=critique_text[:240])
            else:
                existing.mentions += 1
                existing.last_step = step_name
                existing.last_text = critique_text[:240]
                existing.status = "open"
        if self.critiques_recorded > 1:
            for category, issue in self.issues.items():
                if issue.status == "open" and category not in mentioned:
                    issue.status = "likely_resolved"
        return mentioned

    def open_issues(self) -> List[VisualIssue]:
        return [issue for issue in self.issues.values()
                if issue.status == "open"]

    def to_public(self) -> Dict[str, Any]:
        open_list = self.open_issues()
        resolved = [i for i in self.issues.values()
                   if i.status == "likely_resolved"]
        return {
            "critiques_recorded": self.critiques_recorded,
            "open": [i.to_public() for i in open_list],
            "likely_resolved": [i.to_public() for i in resolved],
            "note": (
                "'likely_resolved' means a later real screenshot critique "
                "stopped mentioning that defect — a heuristic reading of "
                "the model's own later opinion, not a verified fix."
            ) if resolved else "",
        }

    def planning_note(self, limit: int = 5) -> str:
        """Actionable text for the next planning/replan/evaluation pass.

        Returns "" when there is nothing open — a run with no visual
        critique yet, or one where every raised defect stopped recurring,
        gets no extra prompt noise.
        """
        open_list = sorted(self.open_issues(),
                           key=lambda i: i.mentions, reverse=True)[:limit]
        if not open_list:
            return ""
        lines = [
            "OPEN VISUAL DEFECTS (from genuine AI critique of actual "
            "captured screenshots — fix the highest-impact ones before "
            "adding more content; do not just acknowledge them in prose):",
        ]
        for issue in open_list:
            lines.append(
                "- %s (raised %dx, last seen at '%s'): %s" %
                (issue.label, issue.mentions, issue.last_step,
                 issue.last_text))
        return "\n".join(lines)
