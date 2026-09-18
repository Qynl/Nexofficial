"""Persistent project memory (NEX 2.0 §14).

Every project gets its own durable state file so Nex can resume work
later — close the conversation, come back tomorrow, and the design
document, locked decisions, milestones, critique cycles and completion
state are all still there.

Storage: one JSON file per project under ~/.nex/projects/<id>.json.
stdlib only; thread-safe via a module lock; saves are best-effort (a
failed save never breaks a run — it is reported, not raised).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()


def projects_dir() -> str:
    base = os.environ.get("NEX_PROJECTS_DIR")
    if base:
        return base
    return os.path.join(os.path.expanduser("~"), ".nex", "projects")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return (s or "project")[:48]


def new_project_id(goal: str) -> str:
    return _slug(goal) + "-" + str(int(time.time()))


def _path(project_id: str) -> str:
    safe = _slug(project_id)
    return os.path.join(projects_dir(), safe + ".json")


def save_project(state: Any, report: Any = None,
                 graph: Any = None, plan: Any = None) -> Optional[str]:
    """Persist a ProjectState (design, decisions, cycles, completion).

    `graph` + `plan` (when given) are stored as the APPROVED plan: the
    exact Plan A the Plan Page showed. START BUILD rebuilds the graph
    from this stored form and executes it as-is — approval is binding.
    Returns the path or None on failure (never raises)."""
    try:
        pid = getattr(state, "project_id", "") or \
            new_project_id(getattr(state, "goal", ""))
        state.project_id = pid
        doc: Dict[str, Any] = {"saved_at": time.time(),
                               "state": state.to_dict()}
        if graph is not None or plan is not None:
            approved: Dict[str, Any] = {}
            if plan is not None:
                approved["plan"] = plan
            if graph is not None:
                try:
                    approved["graph"] = graph.to_dict()
                except Exception:  # noqa: BLE001
                    approved["graph"] = None
            doc["approved"] = approved
        else:
            # Preserve the persisted APPROVED plan on ordinary saves (a
            # run-end save must not clobber the plan Nex is executing).
            try:
                with _LOCK:
                    with open(_path(pid), "r", encoding="utf-8") as f:
                        prev = json.load(f)
                if isinstance(prev.get("approved"), dict):
                    doc["approved"] = prev["approved"]
            except Exception:  # noqa: BLE001
                pass
        if report is not None:
            try:
                doc["report"] = report.to_dict()
            except Exception:  # noqa: BLE001
                doc["report"] = {"status": getattr(report, "status", "")}
        d = projects_dir()
        os.makedirs(d, exist_ok=True)
        path = _path(pid)
        tmp = path + ".tmp"
        with _LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001
        return None


def load_project(project_id: str) -> Optional[Dict[str, Any]]:
    try:
        with _LOCK:
            with open(_path(project_id), "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def load_approved(project_id: str) -> Optional[Dict[str, Any]]:
    """The persisted APPROVED plan for a project:
    {"plan": <plan dict|None>, "graph": <graph dict|None>} or None."""
    doc = load_project(project_id)
    if not doc:
        return None
    approved = doc.get("approved")
    return approved if isinstance(approved, dict) else None


def load_approved_graph(project_id: str) -> Optional[Any]:
    """Rebuild the exact approved TaskGraph (Plan A) for a project."""
    import importlib
    approved = load_approved(project_id)
    if not approved or not approved.get("graph"):
        return None
    try:
        task_graph = importlib.import_module("agent.task_graph")
        return task_graph.TaskGraph.from_dict(approved["graph"])
    except Exception:  # noqa: BLE001
        return None


def load_project_state(project_id: str) -> Optional[Any]:
    """Load a persisted project back into a ProjectState (for resume)."""
    doc = load_project(project_id)
    if not doc:
        return None
    try:
        from agent.project_state import ProjectState
        return ProjectState.from_dict(doc.get("state", {}))
    except Exception:  # noqa: BLE001
        return None


def list_projects(limit: int = 20) -> List[Dict[str, Any]]:
    """Newest-first index: id, goal, phase, engine, saved_at, progress."""
    d = projects_dir()
    out: List[Dict[str, Any]] = []
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return out
    entries = []
    for n in names:
        try:
            with _LOCK:
                with open(os.path.join(d, n), "r", encoding="utf-8") as f:
                    doc = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        st = doc.get("state", {}) or {}
        design = st.get("design", {}) or {}
        milestones = design.get("milestones", []) or []
        entries.append({
            "id": st.get("project_id") or n[:-len(".json")],
            "goal": st.get("goal", ""),
            "phase": st.get("phase", "PLANNING"),
            "engine": st.get("engine"),
            "saved_at": doc.get("saved_at", 0),
            "completed": len(st.get("completed", []) or []),
            "failed": len(st.get("failed", []) or []),
            "pending": len(st.get("pending", []) or []),
            "milestones": len(milestones),
            "title": (design.get("concept")
                      or st.get("goal", ""))[:80],
            "last_activity": (st.get("current")
                              or (st.get("cycles") or [{}])[-1]
                              .get("note", "") if st.get("cycles")
                              else st.get("current")) or "",
        })
    entries.sort(key=lambda e: -float(e.get("saved_at") or 0))
    return entries[:limit]


def delete_project(project_id: str) -> bool:
    try:
        os.remove(_path(project_id))
        return True
    except OSError:
        return False
