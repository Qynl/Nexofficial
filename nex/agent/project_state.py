"""Structured project memory (STAGE 13).

Nex tracks compact structured state — NOT the entire chat history. This is
what gets sent to the model for context and what gets persisted in a
checkpoint, so it must stay small and machine-readable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProjectState:
    goal: str = ""
    engine: Optional[str] = None
    version: str = "0.2"
    completed: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    current: Optional[str] = None
    failed: List[Dict[str, str]] = field(default_factory=list)
    assets: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    known_errors: List[str] = field(default_factory=list)
    # --- NEX 2.0: structured project memory --------------------------------
    # The Design Document (agent.design): concept, pillars, loop,
    # milestones, quality gates — structured data, not Markdown.
    design: Dict[str, Any] = field(default_factory=dict)
    # Design decisions ledger. A decision may be LOCKED: later work must
    # not re-litigate it (design stability).
    decisions_ledger: List[Dict[str, Any]] = field(default_factory=list)
    # Critique cycles: [{cycle, verdict, action, findings, scores, note}]
    cycles: List[Dict[str, Any]] = field(default_factory=list)
    # Lifecycle phase: PLANNING -> DESIGNING -> BUILDING -> CRITIQUING ->
    # POLISHING -> COMPLETE (or FAILED / PAUSED).
    phase: str = "PLANNING"
    project_id: str = ""

    def record_success(self, task_name: str) -> None:
        if task_name not in self.completed:
            self.completed.append(task_name)
        if task_name in self.pending:
            self.pending.remove(task_name)

    def record_failure(self, task_name: str, reason: str) -> None:
        self.failed.append({"task": task_name, "reason": reason})
        if task_name not in self.known_errors:
            self.known_errors.append(reason)

    def record_asset(self, asset: Dict[str, Any]) -> None:
        self.assets.append(asset)

    def decision(self, text: str) -> None:
        self.decisions.append(text)

    # ----- NEX 2.0: design stability + critique memory ----------------------
    def lock_decision(self, decision: str, reason: str = "") -> Dict[str, Any]:
        """Record a design decision as LOCKED: later work must respect it
        (the planner sees it; the critic flags contradictions)."""
        entry = {"decision": decision, "reason": reason,
                 "status": "LOCKED", "ts": time.time()}
        self.decisions_ledger.append(entry)
        self.decisions.append("LOCKED: " + decision)
        return entry

    def note_decision(self, decision: str, reason: str = "") -> Dict[str, Any]:
        entry = {"decision": decision, "reason": reason,
                 "status": "OPEN", "ts": time.time()}
        self.decisions_ledger.append(entry)
        self.decisions.append(decision)
        return entry

    def locked_decisions(self) -> List[str]:
        return [d["decision"] for d in self.decisions_ledger
                if d.get("status") == "LOCKED" and d.get("decision")]

    def record_cycle(self, cycle: Dict[str, Any]) -> None:
        self.cycles.append(dict(cycle))
        del self.cycles[:-20]

    def gate_done(self, gate_name: str) -> bool:
        for g in (self.design or {}).get("quality_gates", []):
            if isinstance(g, dict) and g.get("name") == gate_name:
                return bool(g.get("done"))
        return False

    def complete_gate(self, gate_name: str) -> bool:
        for g in (self.design or {}).get("quality_gates", []):
            if isinstance(g, dict) and g.get("name") == gate_name:
                g["done"] = True
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "engine": self.engine,
            "version": self.version,
            "completed": self.completed,
            "pending": self.pending,
            "current": self.current,
            "failed": self.failed,
            "assets": self.assets,
            "decisions": self.decisions,
            "known_errors": self.known_errors,
            "design": self.design,
            "decisions_ledger": self.decisions_ledger,
            "cycles": self.cycles,
            "phase": self.phase,
            "project_id": self.project_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProjectState":
        s = cls()
        s.goal = d.get("goal", "")
        s.engine = d.get("engine")
        s.version = d.get("version", "0.1")
        s.completed = d.get("completed", []) or []
        s.pending = d.get("pending", []) or []
        s.current = d.get("current")
        s.failed = d.get("failed", []) or []
        s.assets = d.get("assets", []) or []
        s.decisions = d.get("decisions", []) or []
        s.known_errors = d.get("known_errors", []) or []
        # NEX 2.0 fields (tolerant of v0.1 files without them).
        s.design = d.get("design", {}) or {}
        s.decisions_ledger = d.get("decisions_ledger", []) or []
        s.cycles = d.get("cycles", []) or []
        s.phase = d.get("phase", "PLANNING") or "PLANNING"
        s.project_id = d.get("project_id", "") or ""
        return s
