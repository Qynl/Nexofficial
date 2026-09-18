"""The Critic — Nex becomes its own critic (NEX 2.0 core).

"Did my tool call return success?" and "is this actually good?" are
completely different questions. The executor answers the first
(verification.py). THE CRITIC answers the second.

Three layers of verification, per the project vision:

  technical  — build/runtime reality: failed MCP operations, unverified
               completions, tasks that stalled repeatedly.
  design     — does the WORK match the DESIGN DOCUMENT? Repetition that
               contradicts the intended experience, steps that re-litigate
               LOCKED design decisions.
  quality    — placeholder/generic content, missing quality gates.

The deterministic detectors are STRUCTURAL, not content rules: repetition
mining over the task stream, placeholder-language scan, unverified-
completion scan, stall detection. They never encode a particular game
recipe. The optional LLM layer (llm callable, same convention as
judges.py) adds design/quality judgment ON TOP; when it fails, the
deterministic findings stand alone — honest either way.

Output: CritiqueReport {verdict PASS|WEAK, findings, action
COMPLETE|POLISH|REPLAN}. The executor loop uses `action` to decide
polish vs. replan vs. done — Nex doesn't get a free pass after one
iteration, but it also doesn't churn forever (bounded by the caller).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

VERDICT_PASS = "PASS"
VERDICT_WEAK = "WEAK"

ACTION_COMPLETE = "COMPLETE"
ACTION_POLISH = "POLISH"
ACTION_REPLAN = "REPLAN"

# Severity tiers per finding kind. Structural breaks (repetition of the
# same experience loop, repeated stalls) are major; cosmetic residue
# (placeholder naming) is minor.
_SEVERITY = {
    "repetition": "major",
    "stall": "major",
    "locked_conflict": "major",
    "placeholder": "minor",
    "unverified": "minor",
    "gates_open": "minor",
    "llm_design": "minor",
    "llm_quality": "minor",
}

_PLACEHOLDER_RE = re.compile(
    r"(?<![a-z0-9])(placeholder|generic[-_ ]?\d*|default[-_ ]?\d*|"
    r"untitled|todo|temp(orary)?[-_ ]?\d*|wip|unnamed|lorem)"
    r"(?![a-z0-9])", re.I)


# ---------------------------------------------------------------------------
# Deterministic detectors (structural only)
# ---------------------------------------------------------------------------

def _name_tokens(name: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]{3,}", (name or "").lower())]


def find_repetition(completed: List[str], window: int = 3,
                    threshold: int = 3) -> List[Dict[str, Any]]:
    """Detect an ordered task n-gram that keeps recurring in the completed
    stream — the "corridor -> noise -> entity -> escape, x5" pattern,
    generically: the same short sequence of work done over and over."""
    seq = [_name_tokens(n) for n in completed]
    seq = [s for s in seq if s]
    if len(seq) < window * threshold:
        return []
    grams: Dict[tuple, List[int]] = {}
    for i in range(len(seq) - window + 1):
        gram = tuple(tuple(s) for s in seq[i:i + window])
        grams.setdefault(gram, []).append(i)
    out = []
    for gram, idxs in grams.items():
        if len(idxs) >= threshold:
            # Spread check: occurrences must not all be adjacent dupes of
            # a legit multi-part task name.
            spread = idxs[-1] - idxs[0]
            if spread >= max(2, len(seq) // 4):
                out.append({
                    "kind": "repetition",
                    "severity": _SEVERITY["repetition"],
                    "message": ("repeating work pattern detected: %s "
                                "(occurrences: %d)"
                                % (" -> ".join(" ".join(g) for g in gram),
                                   len(idxs))),
                    "evidence": {"pattern": [list(g) for g in gram],
                                 "occurrences": len(idxs)},
                })
    return out


def find_placeholder(texts: List[str],
                     limit: int = 5) -> List[Dict[str, Any]]:
    """Generic/placeholder content still present in completed work."""
    hits = []
    for t in texts:
        if _PLACEHOLDER_RE.search(t or ""):
            hits.append(t)
    if not hits:
        return []
    return [{
        "kind": "placeholder",
        "severity": _SEVERITY["placeholder"],
        "message": ("%d completed item(s) still carry placeholder/generic "
                    "naming or content" % len(hits)),
        "evidence": {"items": hits[:limit]},
    }]


def find_unverified(completed: List[Dict[str, Any]],
                    limit: int = 5) -> List[Dict[str, Any]]:
    """Tasks marked complete without any verification evidence."""
    un = [c.get("name", "?") for c in completed if not c.get("verified")]
    if not un:
        return []
    return [{
        "kind": "unverified",
        "severity": _SEVERITY["unverified"],
        "message": ("%d completed task(s) have no verification evidence "
                    "(claimed, not checked)" % len(un)),
        "evidence": {"tasks": un[:limit]},
    }]


def find_stall(failed: List[Dict[str, Any]], max_same: int = 3
               ) -> List[Dict[str, Any]]:
    """The same task failing over and over — something structural is
    wrong and more retries won't fix it."""
    counts: Dict[str, int] = {}
    for f in failed:
        name = f.get("task", "?") if isinstance(f, dict) else str(f)
        counts[name] = counts.get(name, 0) + 1
    out = []
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n >= max_same:
            out.append({
                "kind": "stall",
                "severity": _SEVERITY["stall"],
                "message": ("'%s' failed %d times — diagnose before more "
                            "attempts" % (name, n)),
                "evidence": {"task": name, "failures": n},
            })
    return out


def find_open_gates(design: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Quality gates the design promised that nothing has checked yet."""
    gates = (design or {}).get("quality_gates") or []
    open_gates = [g.get("name", "?") for g in gates
                  if isinstance(g, dict) and not g.get("done")]
    if not open_gates:
        return []
    return [{
        "kind": "gates_open",
        "severity": _SEVERITY["gates_open"],
        "message": ("%d quality gate(s) not yet demonstrated: %s"
                    % (len(open_gates), ", ".join(open_gates[:5]))),
        "evidence": {"gates": open_gates},
    }]


def find_locked_conflicts(items: List[str],
                          locked_decisions: List[str]
                          ) -> List[Dict[str, Any]]:
    """Work that re-litigates a LOCKED design decision (design
    stability). Uses design.conflicts_locked's conservative heuristic."""
    if not locked_decisions:
        return []
    try:
        from agent.design import conflicts_locked
    except ImportError:  # pragma: no cover
        return []
    out = []
    seen = set()
    for item in items:
        dec = conflicts_locked(item, locked_decisions)
        if dec and dec not in seen:
            seen.add(dec)
            out.append({
                "kind": "locked_conflict",
                "severity": _SEVERITY["locked_conflict"],
                "message": ("work conflicts with a LOCKED design decision "
                            "('%s')" % dec),
                "evidence": {"item": item, "locked": dec},
            })
    return out


# ---------------------------------------------------------------------------
# LLM layer — design/quality judgment ON TOP of the deterministic findings
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def critique_prompt(goal: str, design_summary_text: str,
                    evidence_text: str) -> str:
    return (
        "You are the quality critic reviewing finished (or partially "
        "finished) development work. Judge the WORK, not the process.\n\n"
        "GOAL: " + goal + "\n\n"
        + ("DESIGN DOCUMENT:\n" + design_summary_text + "\n\n"
           if design_summary_text else "")
        + "EVIDENCE (completed tasks, failures, tool results):\n"
        + evidence_text + "\n\n"
        "Answer as ONE JSON object:\n"
        '{"technical": <0-10>, "design": <0-10>, "quality": <0-10>,\n'
        ' "findings": [{"kind": "repetition"|"placeholder"|"design_drift"'
        '|"quality"|"technical", "message": "...", "severity":'
        ' "major"|"minor"}],\n'
        ' "verdict": "PASS"|"WEAK",\n'
        ' "note": "one-sentence honest summary"}\n\n'
        "Ask yourself: does the core loop work? is there pointless or "
        "repetitive content? are there placeholder systems? is the "
        "atmosphere/direction coherent? would a stranger call this good?"
    )


def llm_critique(goal: str, state: Any, report: Any,
                 llm: Callable[[List[Dict[str, str]]], str],
                 max_evidence: int = 24) -> Optional[Dict[str, Any]]:
    """Run the LLM critic; returns parsed JSON or None (never raises)."""
    from agent.design import design_summary
    try:
        completed = getattr(state, "completed", []) or []
        evidence_lines = []
        for name in completed[:max_evidence]:
            evidence_lines.append("- done: " + str(name))
        for f in (getattr(state, "failed", []) or [])[:max_evidence // 2]:
            if isinstance(f, dict):
                evidence_lines.append("- FAILED: %s (%s)"
                                      % (f.get("task"), f.get("reason")))
        assets = getattr(state, "assets", []) or []
        for a in assets[:max_evidence // 2]:
            evidence_lines.append("- asset: " + json.dumps(a)[:160])
        for r in (getattr(report, "reasons", None) or [])[:10]:
            evidence_lines.append("- report: " + str(r)[:160])
        prompt = critique_prompt(
            goal, design_summary(getattr(state, "design", {}) or {}),
            "\n".join(evidence_lines) or "- (no evidence)")
        reply = llm([{"role": "user", "content": prompt}])
        return _extract_json(reply)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# The CritiqueReport
# ---------------------------------------------------------------------------

@dataclass
class CritiqueReport:
    verdict: str = VERDICT_PASS
    action: str = ACTION_COMPLETE
    findings: List[Dict[str, Any]] = field(default_factory=list)
    scores: Dict[str, Any] = field(default_factory=dict)
    note: str = ""
    cycle: int = 1
    llm_used: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "action": self.action,
                "findings": self.findings, "scores": self.scores,
                "note": self.note, "cycle": self.cycle,
                "llm_used": self.llm_used}


def critique(goal: str, state: Any, report: Any,
             llm: Optional[Callable] = None, cycle: int = 1,
             completed_verified: Optional[List[Dict[str, Any]]] = None
             ) -> CritiqueReport:
    """Full critique: deterministic findings + optional LLM judgment.

    `state` is a ProjectState (uses completed/failed/assets/design/
    decisions_ledger). `completed_verified` optionally supplies
    [{name, verified}] — when omitted, unverified detection is skipped
    (no fabricated evidence). `report` is a CompletionReport.
    """
    findings: List[Dict[str, Any]] = []
    completed = list(getattr(state, "completed", []) or [])
    failed = list(getattr(state, "failed", []) or [])
    assets = [json.dumps(a) for a in (getattr(state, "assets", []) or [])]
    design = getattr(state, "design", {}) or {}

    # Locked-decision guard first — design stability.
    locked = []
    for d in (getattr(state, "decisions_ledger", []) or []):
        if isinstance(d, dict) and d.get("status") == "LOCKED":
            locked.append(d.get("decision", ""))
    locked = [d for d in locked if d]
    if locked:
        findings += find_locked_conflicts(completed + assets, locked)

    findings += find_repetition(completed)
    findings += find_stall(failed)
    findings += find_placeholder([c for c in completed] + assets)
    if completed_verified is not None:
        findings += find_unverified(completed_verified)
    if design:
        findings += find_open_gates(design)

    scores: Dict[str, Any] = {}
    note = ""
    llm_used = False
    llm_verdict = None
    if llm is not None:
        llm_out = llm_critique(goal, state, report, llm)
        if llm_out:
            llm_used = True
            for k in ("technical", "design", "quality"):
                v = llm_out.get(k)
                if isinstance(v, (int, float)):
                    scores[k] = max(0, min(10, round(float(v), 1)))
            note = str(llm_out.get("note") or "")[:400]
            llm_verdict = str(llm_out.get("verdict") or "").upper()
            for f in llm_out.get("findings", []) or []:
                if not isinstance(f, dict) or not f.get("message"):
                    continue
                kind = str(f.get("kind") or "quality")
                findings.append({
                    "kind": ("llm_" + kind) if kind in
                            ("design", "quality") else kind,
                    "severity": ("major" if str(f.get("severity")) ==
                                 "major" else "minor"),
                    "message": str(f["message"])[:400],
                    "evidence": {"source": "llm"},
                })

    has_major = any(f.get("severity") == "major" for f in findings)
    if llm_verdict == "WEAK":
        verdict = VERDICT_WEAK
    elif llm_verdict == "PASS" and not has_major:
        verdict = VERDICT_PASS
    else:
        verdict = VERDICT_WEAK if (has_major or findings) else VERDICT_PASS

    # Action: structural problems (any MAJOR finding — repetition, stalls,
    # locked-decision conflicts, a major design critique) -> replan;
    # residue (placeholders, unverified claims, open gates) -> polish;
    # clean -> done.
    if verdict == VERDICT_PASS:
        action = ACTION_COMPLETE
    elif has_major:
        action = ACTION_REPLAN
    else:
        action = ACTION_POLISH

    return CritiqueReport(verdict=verdict, action=action,
                          findings=findings, scores=scores, note=note,
                          cycle=cycle, llm_used=llm_used)
