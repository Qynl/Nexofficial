"""Dependency-aware task graph (STAGE 6 / STAGE 9).

A goal decomposes into tasks. Tasks have dependencies; a task is only
READY once all its dependencies SUCCEEDED. Failure propagates to
dependents so we never restart the whole project after one failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Task statuses.
PENDING = "pending"
READY = "ready"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
SKIPPED = "skipped"          # dependency failed -> not executed
WAITING = "waiting"          # needs user confirmation


@dataclass
class Task:
    id: str
    name: str
    stage: str                       # coarse stage label (asset, build, ...)
    server: Optional[str] = None     # MCP server name, or None for internal
    tool: Optional[str] = None       # tool name (bare; server carries namespace)
    args: Dict[str, Any] = field(default_factory=dict)
    deps: List[str] = field(default_factory=list)
    verify_tool: Optional[str] = None
    verify_args: Dict[str, Any] = field(default_factory=dict)
    status: str = PENDING
    attempts: int = 0
    max_attempts: int = 3
    result: Any = None
    error: Optional[str] = None
    error_signature: Optional[str] = None
    tried_alts: List[str] = field(default_factory=list)
    notes: str = ""


class TaskGraph:
    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._dependents: Dict[str, List[str]] = {}

    # ----- mutation -------------------------------------------------------
    def add(self, task: Task) -> Task:
        if task.id in self._tasks:
            raise ValueError("duplicate task id: " + task.id)
        self._tasks[task.id] = task
        for d in task.deps:
            self._dependents.setdefault(d, []).append(task.id)
        return task

    def get(self, tid: str) -> Optional[Task]:
        return self._tasks.get(tid)

    def all(self) -> List[Task]:
        return list(self._tasks.values())

    # ----- queries --------------------------------------------------------
    def ready(self) -> List[Task]:
        out = []
        for t in self._tasks.values():
            if t.status != PENDING:
                continue
            if self.deps_met(t):
                out.append(t)
        return out

    def deps_met(self, task: Task) -> bool:
        """Are all of `task`'s dependencies currently SUCCESS?"""
        return all(self._tasks[d].status == SUCCESS for d in task.deps)

    def dependents(self, tid: str) -> List[str]:
        return list(self._dependents.get(tid, []))

    def is_terminal(self) -> bool:
        return all(t.status in (SUCCESS, FAILED, SKIPPED) for t in self._tasks.values())

    def completed(self) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == SUCCESS]

    def failed(self) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == FAILED]

    def skipped(self) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == SKIPPED]

    # ----- transitions ----------------------------------------------------
    def mark_success(self, tid: str, result: Any) -> None:
        t = self._tasks[tid]
        t.status = SUCCESS
        t.result = result
        t.error = None

    def mark_running(self, tid: str) -> None:
        self._tasks[tid].status = RUNNING

    def mark_failed(self, tid: str, error: str,
                    signature: Optional[str] = None) -> None:
        t = self._tasks[tid]
        t.status = FAILED
        t.error = error
        t.error_signature = signature
        # Dependency-aware failure propagation: dependents are skipped, not
        # re-executed, so a single failure doesn't cascade into retries.
        for dep in self._dependents.get(tid, []):
            dt = self._tasks[dep]
            if dt.status == PENDING:
                dt.status = SKIPPED
                dt.notes = "skipped: dependency '%s' failed" % tid

    def mark_skipped(self, tid: str, reason: str) -> None:
        t = self._tasks[tid]
        t.status = SKIPPED
        t.notes = reason

    # ----- serialization (for checkpoints) --------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "tasks": [
                {
                    "id": t.id, "name": t.name, "stage": t.stage,
                    "server": t.server, "tool": t.tool, "args": t.args,
                    "deps": t.deps, "verify_tool": t.verify_tool,
                    "verify_args": t.verify_args, "status": t.status,
                    "attempts": t.attempts, "max_attempts": t.max_attempts,
                    "result": t.result, "error": t.error,
                    "error_signature": t.error_signature, "notes": t.notes,
                }
                for t in self._tasks.values()
            ]
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskGraph":
        g = cls()
        for td in d.get("tasks", []):
            g.add(Task(
                id=td["id"], name=td["name"], stage=td["stage"],
                server=td.get("server"), tool=td.get("tool"),
                args=td.get("args", {}), deps=td.get("deps", []),
                verify_tool=td.get("verify_tool"),
                verify_args=td.get("verify_args", {}),
                status=td.get("status", PENDING),
                attempts=td.get("attempts", 0),
                max_attempts=td.get("max_attempts", 3),
                result=td.get("result"), error=td.get("error"),
                error_signature=td.get("error_signature"),
                notes=td.get("notes", ""),
            ))
        return g
