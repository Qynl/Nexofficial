"""Structured project memory (STAGE 13).

Nex tracks compact structured state — NOT the entire chat history. This is
what gets sent to the model for context and what gets persisted in a
checkpoint, so it must stay small and machine-readable.

BOUNDED MEMORY (the small-model rule): this state is fed back into the
model on every planning round, so it must never grow without limit. Every
collection here is either

  * UPSERTED — keyed by identity, so re-reporting the same thing
    updates the existing entry instead of appending a duplicate
    (systems, assets, decisions, known errors, bugs, failed tasks), or
  * CAPPED — a rolling window with an honest counter for what rolled
    out (completed tasks, observations, cycles).

`compact_memory()` enforces the caps and reports what it trimmed, so a
long autonomous session cannot poison its own context with history.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- memory budget (entries per collection) --------------------------------
# These are CONTEXT limits, not disk limits: every one of these lists is
# serialized into the model's planning prompt. Keep them small.
LIMITS: Dict[str, int] = {
    "completed": 60,        # rolling window; completed_count stays exact
    "failed": 12,
    "assets": 30,
    "decisions": 25,
    "known_errors": 15,
    "pending": 40,
    "systems": 24,
    "knowledge": 40,        # durable facts learned about the project
}

# System lifecycle: what the Director promises vs. what the build proved.
SYSTEM_PLANNED = "planned"
SYSTEM_IN_PROGRESS = "in_progress"
SYSTEM_COMPLETE = "complete"
SYSTEM_BROKEN = "broken"        # was complete, then a defect was observed
# Work finished, but the criteria were NOT proven (no reviewer verdict and
# no clean observation). Distinct from "complete" on purpose: the system
# map must never show unverified work as verified.
SYSTEM_UNVERIFIED = "unverified"


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
    # Runtime observations (OBSERVE loop): [{kind, tool, text, payload,
    # empty, ts}] — bounded rolling window, the critic's evidence.
    observations: List[Dict[str, Any]] = field(default_factory=list)
    # Confirmed defects found via observation: [{kind, message, ts,
    # status: open|fixed}] — survives across runs (project memory) and is
    # fed back into the planner so the same bug is not rebuilt into place.
    known_bugs: List[Dict[str, Any]] = field(default_factory=list)
    # --- Game-Director layer: the SYSTEM MAP -------------------------------
    # Which systems this game consists of and how far each one actually
    # got. This is the small model's orientation: what exists -> what works
    # -> what doesn't -> what we are building right now. Upserted by name.
    systems: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Explicit completion criteria per system (from the recipe library /
    # the Director). The mandatory-verification gate reads this.
    criteria: Dict[str, List[str]] = field(default_factory=dict)
    # Evidence per criterion: {"system": {"criterion": "pass"|"fail"|note}}
    criteria_evidence: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Durable project knowledge the model learned (engine quirks, working
    # tool patterns): upserted by key, bounded — never a chat log.
    knowledge: Dict[str, str] = field(default_factory=dict)
    # Honest counter: `completed` is a rolling window, this does not lie.
    completed_count: int = 0
    # The system the CURRENT work is scoped to (agent.director scope
    # envelope). The critic uses it to report scope creep; the UI shows it.
    current_system: str = ""
    # The rendered BUILD BLUEPRINT (what will be built, with which
    # capabilities, and what would prove it). Bounded: it is a document,
    # not a log, and the newest one replaces the old.
    blueprint_md: str = ""
    # Lifecycle phase: PLANNING -> DESIGNING -> BUILDING -> CRITIQUING ->
    # POLISHING -> COMPLETE (or FAILED / PAUSED).
    phase: str = "PLANNING"
    project_id: str = ""

    # ----- bounded helpers --------------------------------------------------
    @staticmethod
    def _upsert(lst: List[Dict[str, Any]], entry: Dict[str, Any],
                key: str = "name", limit: int = 30) -> Dict[str, Any]:
        """Replace the entry with the same key (merge), else append; then
        enforce the cap. A same-key update keeps the ORIGINAL position so
        the model sees stable ordering."""
        for i, prev in enumerate(lst):
            if isinstance(prev, dict) and prev.get(key) == entry.get(key):
                merged = dict(prev)
                merged.update(entry)
                lst[i] = merged
                return merged
        lst.append(entry)
        del lst[:-limit]
        return entry

    def record_success(self, task_name: str) -> None:
        if task_name not in self.completed:
            self.completed.append(task_name)
            self.completed_count += 1
        if task_name in self.pending:
            self.pending.remove(task_name)
        del self.completed[:-(LIMITS["completed"])]

    def record_failure(self, task_name: str, reason: str) -> None:
        # Upsert by task: a task retried in the next cycle must not pile up
        # a new row each round (that was unbounded memory growth).
        self._upsert(self.failed, {"task": task_name, "reason": reason,
                                   "ts": time.time()},
                     key="task", limit=LIMITS["failed"])
        self.remember_error(reason)

    def remember_error(self, reason: str) -> None:
        """Deduplicated error memory (exact-string dedupe + cap)."""
        r = (reason or "").strip()[:300]
        if not r or r in self.known_errors:
            return
        self.known_errors.append(r)
        del self.known_errors[:-(LIMITS["known_errors"])]

    def record_asset(self, asset: Dict[str, Any]) -> None:
        entry = dict(asset)
        entry.setdefault("name", entry.get("id") or entry.get("type") or "?")
        self._upsert(self.assets, entry, key="name", limit=LIMITS["assets"])

    def decision(self, text: str) -> None:
        # Decisions are facts, not a log: re-stating one is a no-op.
        if text and text not in self.decisions:
            self.decisions.append(text)
            del self.decisions[:-(LIMITS["decisions"])]

    # ----- Game-Director layer: systems, criteria, knowledge ----------------
    def upsert_system(self, name: str, status: Optional[str] = None,
                      notes: str = "", recipe: str = "") -> Dict[str, Any]:
        """Register/update one system (the system map's only writer)."""
        key = (name or "").strip().lower().replace(" ", "_")
        if not key:
            return {}
        entry = self.systems.get(key) or {"name": key, "status": SYSTEM_PLANNED,
                                          "ts": time.time()}
        if status:
            entry["status"] = status
        if notes:
            entry["notes"] = notes[:280]
        if recipe:
            entry["recipe"] = recipe
        entry["ts"] = time.time()
        self.systems[key] = entry
        # Bound the map: drop the oldest PLANNED entries first, never a
        # completed one (that is the project's actual state).
        if len(self.systems) > LIMITS["systems"]:
            ordered = sorted(self.systems.items(),
                             key=lambda kv: (kv[1].get("status") != SYSTEM_PLANNED,
                                             kv[1].get("ts", 0)))
            for k, _ in ordered[:len(self.systems) - LIMITS["systems"]]:
                self.systems.pop(k, None)
        return entry

    def system_status(self, name: str) -> str:
        key = (name or "").strip().lower().replace(" ", "_")
        return (self.systems.get(key) or {}).get("status", "")

    def systems_by_status(self, status: str) -> List[str]:
        return [k for k, v in self.systems.items()
                if v.get("status") == status]

    def set_criteria(self, system: str, items: List[str]) -> None:
        key = (system or "").strip().lower().replace(" ", "_")
        if not key:
            return
        clean: List[str] = []
        for it in items or []:
            it = str(it).strip()
            if it and it not in clean:
                clean.append(it[:200])
        if clean:
            self.criteria[key] = clean[:12]

    def criteria_for(self, system: str) -> List[str]:
        key = (system or "").strip().lower().replace(" ", "_")
        return list(self.criteria.get(key) or [])

    def mark_criterion(self, system: str, criterion: str, ok: bool,
                       note: str = "") -> None:
        """Record a criterion verdict. `ok=True` means PROVEN (a reviewer
        agreed, or the running game was observed clean) — never "we tried"."""
        key = (system or "").strip().lower().replace(" ", "_")
        ev = self.criteria_evidence.setdefault(key, {})
        ev[str(criterion)[:200]] = "pass" if ok else ("fail" + (
            (": " + note[:120]) if note else ""))

    def mark_evidence(self, system: str, criterion: str,
                      note: str = "") -> None:
        """Weak signal: the step that was SUPPOSED to produce this
        criterion ran and returned a non-trivial result. Deliberately NOT
        'pass' — a successful tool call is intent, not proof. Only a clean
        re-observation or a reviewer verdict upgrades it."""
        key = (system or "").strip().lower().replace(" ", "_")
        if not key:
            return
        ev = self.criteria_evidence.setdefault(key, {})
        crit = str(criterion)[:200]
        if str(ev.get(crit, "")).startswith(("pass", "fail")):
            return                      # never downgrade a real verdict
        ev[crit] = "evidence" + ((": " + note[:100]) if note else "")

    def prove_criteria(self, system: str, note: str = "") -> int:
        """Upgrade every unproven criterion of a system to PROVEN. Called
        when the running game was observed clean this round."""
        key = (system or "").strip().lower().replace(" ", "_")
        n = 0
        ev = self.criteria_evidence.setdefault(key, {})
        for item in self.criteria_for(key):
            if not str(ev.get(item, "")).startswith("pass"):
                ev[item] = "pass" + ((": " + note[:100]) if note else "")
                n += 1
        return n

    def criterion_state(self, system: str, criterion: str) -> str:
        ev = (self.criteria_evidence.get(
            (system or "").strip().lower().replace(" ", "_")) or {})
        raw = str(ev.get(str(criterion)[:200], "") or "")
        if raw.startswith("pass"):
            return "pass"
        if raw.startswith("fail"):
            return "fail"
        if raw.startswith("evidence"):
            return "evidence"
        return "unknown"

    def unmet_criteria(self, system: Optional[str] = None) -> List[Dict[str, str]]:
        """Criteria with NO PROOF — the completion gate's input.

        `system=None` checks every system; pass a system id to gate only
        the round's objective (future systems are 'not built yet', not
        'unproven'). A system without declared criteria is never gated:
        requirements are never invented.
        """
        out: List[Dict[str, str]] = []
        keys = ([system.strip().lower().replace(" ", "_")] if system
                else list(self.criteria.keys()))
        for key in keys:
            for item in (self.criteria.get(key) or []):
                if self.criterion_state(key, item) != "pass":
                    out.append({"system": key, "criterion": item,
                                "state": self.criterion_state(key, item)})
        return out

    def remember(self, key: str, value: str) -> None:
        """Durable, upserted knowledge (bounded by key count)."""
        k = (key or "").strip()[:80]
        if not k:
            return
        self.knowledge[k] = str(value)[:280]
        if len(self.knowledge) > LIMITS["knowledge"]:
            # Drop the least recently added keys (dict order = insertion).
            for kk in list(self.knowledge.keys())[
                    :len(self.knowledge) - LIMITS["knowledge"]]:
                self.knowledge.pop(kk, None)

    # ----- memory budget --------------------------------------------------
    def memory_size(self) -> int:
        """Approximate serialized size of the model-visible memory (chars).
        The planning prompt embeds this; keep it small."""
        try:
            return len(json.dumps(self.to_dict(), default=str))
        except Exception:  # noqa: BLE001
            return -1

    def compact_memory(self) -> Dict[str, int]:
        """Enforce every cap; return what was trimmed. Safe to call often
        (idempotent) — the loop calls it once per cycle."""
        before = self.memory_size()
        trimmed: Dict[str, int] = {}
        for name, limit in (("completed", LIMITS["completed"]),
                            ("failed", LIMITS["failed"]),
                            ("assets", LIMITS["assets"]),
                            ("decisions", LIMITS["decisions"]),
                            ("known_errors", LIMITS["known_errors"]),
                            ("pending", LIMITS["pending"]),
                            ("cycles", 20), ("observations", 30),
                            ("known_bugs", 50),
                            ("decisions_ledger", 30)):
            lst = getattr(self, name, None)
            if isinstance(lst, list) and len(lst) > limit:
                trimmed[name] = len(lst) - limit
                del lst[:-limit]
        if len(self.systems) > LIMITS["systems"]:
            trimmed["systems"] = len(self.systems) - LIMITS["systems"]
        if len(self.knowledge) > LIMITS["knowledge"]:
            over = len(self.knowledge) - LIMITS["knowledge"]
            trimmed["knowledge"] = over
            for k in list(self.knowledge.keys())[:over]:
                self.knowledge.pop(k, None)
        if len(self.criteria_evidence) > LIMITS["systems"] * 2:
            trimmed["criteria_evidence"] = (
                len(self.criteria_evidence) - LIMITS["systems"] * 2)
            for k in list(self.criteria_evidence.keys())[
                    :len(self.criteria_evidence) - LIMITS["systems"] * 2]:
                self.criteria_evidence.pop(k, None)
        after = self.memory_size()
        if trimmed:
            trimmed["_bytes"] = max(0, before - after)
        return trimmed

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

    # ----- OBSERVE loop: runtime evidence + confirmed defects ------------
    def record_observation(self, obs: Dict[str, Any],
                           limit: int = 30) -> None:
        self.observations.append(dict(obs))
        del self.observations[:-limit]

    def record_bug(self, kind: str, message: str,
                   status: str = "open") -> Dict[str, Any]:
        """Register a confirmed defect (from observation critique)."""
        entry = {"kind": kind, "message": message[:300],
                 "ts": time.time(), "status": status}
        # Resolve an earlier open bug of the same kind when a new one of a
        # DIFFERENT kind appears (the game moved on); same-kind refreshes.
        for prev in self.known_bugs:
            if prev.get("kind") == kind and prev.get("status") == "open":
                prev["ts"] = entry["ts"]
                prev["message"] = entry["message"]
                return prev
        self.known_bugs.append(entry)
        del self.known_bugs[:-50]
        return entry
    def open_bugs(self) -> List[Dict[str, Any]]:
        return [b for b in self.known_bugs if b.get("status") == "open"]

    def mark_bugs_fixed(self, kinds) -> int:
        """Mark open bugs of the given kinds as fixed (re-observation
        showed them gone). Returns how many were resolved."""
        kinds = set(kinds or ())
        n = 0
        for b in self.known_bugs:
            if b.get("kind") in kinds and b.get("status") == "open":
                b["status"] = "fixed"
                n += 1
        return n

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
            "observations": self.observations,
            "known_bugs": self.known_bugs,
            "systems": self.systems,
            "criteria": self.criteria,
            "criteria_evidence": self.criteria_evidence,
            "knowledge": self.knowledge,
            "completed_count": self.completed_count,
            "current_system": self.current_system,
            "blueprint_md": (self.blueprint_md or "")[:20000],
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
        # OBSERVE loop fields (tolerant of older checkpoints without them).
        s.observations = d.get("observations", []) or []
        s.known_bugs = d.get("known_bugs", []) or []
        # Game-Director fields (tolerant of older checkpoints without them).
        s.systems = d.get("systems", {}) or {}
        s.criteria = d.get("criteria", {}) or {}
        s.criteria_evidence = d.get("criteria_evidence", {}) or {}
        s.knowledge = d.get("knowledge", {}) or {}
        s.completed_count = int(d.get("completed_count")
                                or len(s.completed))
        s.current_system = d.get("current_system", "") or ""
        s.blueprint_md = (d.get("blueprint_md", "") or "")[:20000]
        s.phase = d.get("phase", "PLANNING") or "PLANNING"
        s.project_id = d.get("project_id", "") or ""
        return s
