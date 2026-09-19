"""Checkpoints (STAGE 14).

After meaningful milestones, Nex saves a checkpoint containing the project
state + task graph. If Nex restarts, it recovers from the latest valid
checkpoint instead of starting over.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

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


def reconcile_on_resume(path: str, registry,
                        bus=None) -> Optional[Tuple[str, ProjectState, TaskGraph, Dict[str, Any]]]:
    """Reconcile a checkpoint against CURRENT reality before resuming.

    A checkpoint is a claim about the world; the world moved on while Nex
    was down. Before trusting it we:

      1. re-discover capabilities (the caller passes a FRESH registry built
         from live MCP upstreams — servers may have connected/disconnected),
      2. check every task's tool still exists; pending tasks whose tool
         vanished are marked FAILED with a note instead of blowing up
         mid-run,
      3. flag required-args drift between the saved args and the live
         inputSchema,
      4. RE-VERIFY completed tasks that have a verify_tool still available:
         if verification now fails, the task is demoted to pending so the
         loop genuinely redoes it (honest resume, not blind resume).

    Step 4 goes through agent.verification.verify_task — the SAME canonical
    verifier the execution loop uses — against the freshly reconnected
    registry.

    Returns (goal, state, graph, reconciliation_report) or None.
    """
    from agent.task_graph import FAILED, PENDING, SUCCESS

    loaded = load_checkpoint(path)
    if loaded is None:
        return None
    goal, state, graph, extra = loaded

    report: Dict[str, Any] = {
        "goal": goal,
        "tasks_checked": 0,
        "missing_tools": [],
        "args_drift": [],
        "reverified": 0,
        "reverify_failed": [],
    }

    def _tv(tool: str):
        tv = registry.by_name(tool)
        if tv is None and "." in tool:
            tv = registry.by_name(tool.split(".", 1)[-1])
        return tv

    for t in graph.all():
        report["tasks_checked"] += 1
        tv = _tv(t.tool)
        if tv is None:
            report["missing_tools"].append(t.tool)
            if t.status != SUCCESS:
                t.status = FAILED
                t.error = ("tool '%s' unavailable since checkpoint" % t.tool)
                t.notes = (t.notes + " | reconciled: tool vanished").strip(" |")
                if bus is not None:
                    bus({"type": "agent.checkpoint_reconciled", "task": t.id,
                         "issue": "missing_tool", "tool": t.tool})
                continue
        # Required-args drift vs the live schema.
        schema = tv.schema if isinstance(tv.schema, dict) else {}
        for req in (schema.get("required", []) or []):
            if req not in (t.args or {}):
                report["args_drift"].append(
                    {"task": t.id, "tool": t.tool, "missing_arg": req})
        # Re-verify completed work whose verifier still exists. This is the
        # canonical verifier: it performs the live registry call itself.
        if t.status == SUCCESS and t.verify_tool:
            vtv = _tv(t.verify_tool)
            if vtv is not None:
                try:
                    from agent.verification import verify_task
                    vres = verify_task(t, registry,
                                       t.result if isinstance(t.result, dict) else None)
                    report["reverified"] += 1
                    if not vres.ok:
                        t.status = PENDING
                        t.notes = (t.notes +
                                   " | reconciled: re-verify failed, redo"
                                   ).strip(" |")
                        report["reverify_failed"].append(t.id)
                        if bus is not None:
                            bus({"type": "agent.checkpoint_reconciled",
                                 "task": t.id, "issue": "reverify_failed",
                                 "note": vres.note})
                except Exception as exc:  # noqa: BLE001
                    report["args_drift"].append(
                        {"task": t.id, "tool": t.verify_tool,
                         "reverify_error": repr(exc)})

    if bus is not None:
        bus({"type": "agent.checkpoint_resumed", "goal": goal,
             "report": report})
    return goal, state, graph, report


# ---------------------------------------------------------------------------
# Workspace snapshots (file-level checkpoints — the audit's
# "checkpoint before a big change, rollback after a failed experiment").
#
# A state checkpoint (above) remembers the PLAN. A workspace snapshot
# remembers the actual FILES the agent has written so far. Together they
# let the agent experiment aggressively: snapshot -> try -> if it makes
# things worse, restore the files and keep the plan.
#
# Implemented with tar (stdlib) so it works with no external deps and is
# atomic enough for the agent's use (we never edit a snapshot after
# writing it). Snapshots live under <root>/.nex_snapshots/.
# ---------------------------------------------------------------------------

import tarfile
import time


def _snapshot_dir(root: str) -> str:
    return os.path.join(root, ".nex_snapshots")


def snapshot_workspace(root: str, label: str = "") -> Optional[str]:
    """Snapshot the files under ``root`` (excluding the snapshot dir and
    VCS/dep noise). Returns the snapshot path, or None on failure.

    This is what the agent calls before a risky change so it can roll the
    files back if the change makes verification/observation worse.
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return None
    snap_dir = _snapshot_dir(root)
    try:
        os.makedirs(snap_dir, exist_ok=True)
        slug = (label or "snap").replace(os.sep, "_")
        safe = "".join(c for c in slug if c.isalnum() or c in "-_")[:40] or "snap"
        path = os.path.join(snap_dir, "%d_%s.tar" % (int(time.time() * 1000), safe))
        # tar members are named "<root-basename>/..."; skip the snapshot
        # dir itself, VCS metadata, and dependency bulk that never needs
        # rolling back (any path component in `skip` excludes the member).
        skip = {".nex_snapshots", ".git", ".hg", ".svn", "node_modules",
                ".venv", "__pycache__", ".mypy_cache", ".pytest_cache"}

        def _filter(member: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
            if any(part in skip for part in member.name.split("/")):
                return None
            return member

        # arcname="." -> members are "./file" so that extractall(root)
        # (see restore_workspace) lands files exactly where they came
        # from — no double-nested root directory.
        with tarfile.open(path, "w") as tf:
            tf.add(root, arcname=".", filter=_filter, recursive=True)
        return path
    except (OSError, tarfile.TarError):
        return None


def restore_workspace(root: str, snapshot_path: str) -> bool:
    """Restore the files under ``root`` from a snapshot made by
    ``snapshot_workspace``. Best-effort: overwrites matching files, leaves
    others alone. Returns True on success."""
    root = os.path.abspath(root)
    try:
        with tarfile.open(snapshot_path, "r") as tf:
            tf.extractall(root)  # noqa: S202 — snapshot is our own file
        return True
    except (OSError, tarfile.TarError):
        return False


def list_snapshots(root: str) -> List[str]:
    """Newest-first list of snapshot paths under ``root``."""
    snap_dir = _snapshot_dir(os.path.abspath(root))
    if not os.path.isdir(snap_dir):
        return []
    out = []
    for name in os.listdir(snap_dir):
        if name.endswith(".tar"):
            out.append(os.path.join(snap_dir, name))
    return sorted(out, reverse=True)


def prune_snapshots(root: str, keep: int = 5) -> int:
    """Keep only the newest ``keep`` snapshots. Returns # removed."""
    snaps = list_snapshots(root)
    removed = 0
    for path in snaps[keep:]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed
