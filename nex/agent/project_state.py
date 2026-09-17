"""Structured project memory (STAGE 13).

Nex tracks compact structured state — NOT the entire chat history. This is
what gets sent to the model for context and what gets persisted in a
checkpoint, so it must stay small and machine-readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProjectState:
    goal: str = ""
    engine: Optional[str] = None
    version: str = "0.1"
    completed: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    current: Optional[str] = None
    failed: List[Dict[str, str]] = field(default_factory=list)
    assets: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    known_errors: List[str] = field(default_factory=list)

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
        return s
