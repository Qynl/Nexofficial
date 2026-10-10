"""Dependency-aware task graph.

A goal decomposes into steps. Steps have dependencies; a step is only
READY once all its dependencies SUCCEEDED. Failure propagates to
dependents (they are skipped, not re-executed), so one failure does not
restart the whole run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Step statuses.
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
    name: str                       # short human title ("Place the chest")
    slug: str = ""                  # plan machine name ("place-chest")
    server: Optional[str] = None    # MCP server name
    tool: Optional[str] = None      # tool name (bare; server carries namespace)
    args: Dict[str, Any] = field(default_factory=dict)
    deps: List[str] = field(default_factory=list)
    contract_fingerprint: str = ""       # callable schema pinned at plan time
    status: str = PENDING
    attempts: int = 0
    max_attempts: int = 3
    result: Any = None
    error: Optional[str] = None
    error_signature: Optional[str] = None
    failure_kind: str = ""        # agent/failures.py taxonomy, best-effort
    # agent/debugger.py's deterministic file/line/code/message extraction
    # (or an honest "deterministic": False record) for a FAILED task.
    diagnostic: Dict[str, Any] = field(default_factory=dict)
    tried_alts: List[str] = field(default_factory=list)
    llm_diagnosed: bool = False   # bounded: LLM repair runs at most once/task
    expect: Optional[Any] = None  # what success should look like (from plan)
    why: str = ""                 # why this step exists (from the plan)
    phase: str = ""               # production-program stage, when applicable
    notes: str = ""

    def to_public(self) -> Dict[str, Any]:
        """The step as the UI sees it (no internal repair bookkeeping)."""
        return {
            "id": self.id, "name": self.name, "status": self.status,
            "server": self.server, "tool": self.tool,
            "contract_pinned": bool(self.contract_fingerprint),
            "why": self.why, "expect": self.expect, "phase": self.phase,
            "error": self.error, "failure_kind": self.failure_kind,
            "diagnostic": self.diagnostic,
            "notes": self.notes,
        }


class TaskGraph:
    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._dependents: Dict[str, List[str]] = {}
        # Provenance / reporting metadata set by planners.
        self.plan_meta: Dict[str, Any] = {}

    # ----- mutation ---------------------------------------------------------
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

    def merge(self, other: "TaskGraph") -> None:
        """Adopt every task from `other` (used when re-planning: the new
        plan replaces the pending remainder, completed history is kept
        by the caller)."""
        for t in other.all():
            if t.id not in self._tasks:
                self.add(t)

    # ----- queries ------------------------------------------------------------
    def ready(self) -> List[Task]:
        out = []
        for t in self._tasks.values():
            if t.status != PENDING:
                continue
            if self.deps_met(t):
                out.append(t)
        return out

    def deps_met(self, task: Task) -> bool:
        """Are all of `task`'s dependencies currently SUCCESS?

        A dep id that no longer exists (a re-planned graph) counts as
        UNMET instead of raising — the task stays pending and the run
        reports it as blocked rather than crashing.
        """
        return all(
            (self._tasks.get(d) is not None
             and self._tasks[d].status == SUCCESS)
            for d in task.deps)

    def dependents(self, tid: str) -> List[str]:
        return list(self._dependents.get(tid, []))

    def is_terminal(self) -> bool:
        return all(t.status in (SUCCESS, FAILED, SKIPPED)
                   for t in self._tasks.values())

    def completed(self) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == SUCCESS]

    def failed(self) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == FAILED]

    def skipped(self) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == SKIPPED]

    def pending(self) -> List[Task]:
        return [t for t in self._tasks.values()
                if t.status in (PENDING, READY, RUNNING, WAITING)]

    # ----- transitions ---------------------------------------------------------
    def mark_success(self, tid: str, result: Any) -> None:
        t = self._tasks[tid]
        t.status = SUCCESS
        t.result = result
        t.error = None

    def mark_running(self, tid: str) -> None:
        self._tasks[tid].status = RUNNING

    def mark_waiting(self, tid: str) -> None:
        self._tasks[tid].status = WAITING

    def mark_pending(self, tid: str) -> None:
        self._tasks[tid].status = PENDING

    def mark_failed(self, tid: str, error: str,
                    signature: Optional[str] = None,
                    failure_kind: Optional[str] = None,
                    diagnostic: Optional[Dict[str, Any]] = None) -> None:
        t = self._tasks[tid]
        t.status = FAILED
        t.error = error
        t.error_signature = signature
        if failure_kind is not None:
            t.failure_kind = failure_kind
        if diagnostic is not None:
            t.diagnostic = dict(diagnostic)
        # Dependency-aware failure propagation: dependents are skipped,
        # not re-executed.
        self._propagate_skip(tid)

    def mark_skipped(self, tid: str, reason: str) -> None:
        t = self._tasks[tid]
        t.status = SKIPPED
        t.notes = reason
        self._propagate_skip(tid)

    def _propagate_skip(self, tid: str) -> None:
        """Skip every PENDING task downstream of `tid`, transitively.

        A single-hop walk is not enough: in a chain A -> B -> C, failing A
        must also skip C, not just B. Without this, C stays PENDING
        forever (deps_met() can never see B reach SUCCESS once B is
        SKIPPED), is_terminal() never becomes true, and the run's final
        report would misreport C as never having run instead of correctly
        showing it as skipped. BFS with a seen-set keeps this O(n) even
        on a diamond-shaped graph where a task has multiple paths back to
        the one that failed.
        """
        queue: List[tuple] = [(tid, dep)
                              for dep in self._dependents.get(tid, [])]
        seen = set()
        while queue:
            cause, dep = queue.pop(0)
            if dep in seen:
                continue
            seen.add(dep)
            dt = self._tasks.get(dep)
            if dt is None or dt.status != PENDING:
                continue
            dt.status = SKIPPED
            dt.notes = "skipped: dependency '%s' failed" % cause
            queue.extend((dep, nxt) for nxt in self._dependents.get(dep, []))

    # ----- serialization ----------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "tasks": [
                {
                    "id": t.id, "name": t.name, "slug": t.slug,
                    "server": t.server, "tool": t.tool, "args": t.args,
                    "deps": t.deps, "status": t.status,
                    "attempts": t.attempts, "max_attempts": t.max_attempts,
                    "result": t.result, "error": t.error,
                    "error_signature": t.error_signature,
                    "failure_kind": t.failure_kind,
                    "diagnostic": t.diagnostic,
                    "llm_diagnosed": t.llm_diagnosed,
                    "expect": t.expect, "why": t.why,
                    "phase": t.phase, "notes": t.notes,
                }
                for t in self._tasks.values()
            ]
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskGraph":
        g = cls()
        for td in d.get("tasks", []):
            g.add(Task(
                id=td["id"], name=td["name"], slug=td.get("slug", "") or "",
                server=td.get("server"), tool=td.get("tool"),
                args=td.get("args", {}), deps=td.get("deps", []),
                status=td.get("status", PENDING),
                attempts=td.get("attempts", 0),
                max_attempts=td.get("max_attempts", 3),
                result=td.get("result"), error=td.get("error"),
                error_signature=td.get("error_signature"),
                failure_kind=td.get("failure_kind", "") or "",
                diagnostic=dict(td.get("diagnostic") or {}),
                llm_diagnosed=bool(td.get("llm_diagnosed", False)),
                expect=td.get("expect"),
                why=td.get("why", "") or "",
                phase=td.get("phase", "") or "",
                notes=td.get("notes", ""),
            ))
        return g
