"""Checkpoints (STAGE 14).

After meaningful milestones, Nex saves a checkpoint containing the project
state + task graph. If Nex restarts, it recovers from the latest valid
checkpoint instead of starting over.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from agent.project_state import ProjectState
from agent.task_graph import TaskGraph


def save_checkpoint(path: str, goal: str, state: ProjectState,
                    graph: TaskGraph, extra: Optional[Dict[str, Any]] = None) -> None:
    """Persist a checkpoint atomically-ish (write-then-rename)."""
    payload: Dict[str, Any] = {
        "goal": goal,
        "state": state.to_dict(),
        "graph": graph.to_dict(),
        "extra": extra or {},
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load_checkpoint(path: str) -> Optional[Tuple[str, ProjectState, TaskGraph, Dict[str, Any]]]:
    """Load a checkpoint. Returns None if missing/unreadable."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    state = ProjectState.from_dict(data.get("state", {}))
    graph = TaskGraph.from_dict(data.get("graph", {}))
    return data.get("goal", ""), state, graph, data.get("extra", {})


def is_resumable(path: str) -> bool:
    return load_checkpoint(path) is not None
