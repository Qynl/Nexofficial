"""Machine-readable contracts for RemoteEvents/RemoteFunctions.

Built as a VIEW over roblox/project_model.py's already-persisted remote
nodes — there is no separate contract ledger to keep in sync, because a
contract's structural facts (name, kind, owning service) ARE the project
model's own evidence. What this module adds on top:

  * a deterministic, conservative RATE-LIMIT signal: a remote counts as
    "rate-limit evidence present" only when a DIFFERENT successful call
    this run touched something literally named for rate limiting
    alongside it — never assumed;
  * explicit, empty PURPOSE / SERVER_VALIDATES / TESTS slots. Nex cannot
    determine what a remote is FOR, or whether its handler actually
    validates arguments, from call arguments alone — that needs either
    reading the Luau body (diagnosis/LLM work) or a human description.
    Those fields start empty and UNVERIFIED on purpose, and
    annotate_contract() is the only way to fill them in, so a filled-in
    contract is always traceable to a specific source, never silently
    fabricated.

Treating client-supplied values as authoritative for important game
state is exactly the mistake this whole system exists to catch — a
contract with an empty server_validates list is a visible gap, not a
passing grade.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from roblox.project_model import nodes_of_kind

UNVERIFIED = "UNVERIFIED"
CONFIRMED = "CONFIRMED"


def _remote_kind(class_name: str) -> str:
    low = (class_name or "").lower()
    if "function" in low:
        return "RemoteFunction"
    if "unreliable" in low:
        return "UnreliableRemoteEvent"
    return "RemoteEvent"


def _has_rate_limit_evidence(name: str, tasks: Any) -> bool:
    name_low = (name or "").lower()
    for t in tasks:
        if getattr(t, "status", None) != "success":
            continue
        tool = (getattr(t, "tool", "") or "").lower()
        if "rate_limit" not in tool and "throttle" not in tool:
            continue
        args = getattr(t, "args", None) or {}
        blob = " ".join(str(v) for v in args.values() if isinstance(v, str))
        if name_low and name_low in blob.lower():
            return True
    return False


def build_contracts(model: Any, tasks: Any = ()) -> List[Dict[str, Any]]:
    """One contract skeleton per remote/bindable the project model knows
    about. `tasks` is optional — only used for the rate-limit heuristic;
    omit it to get the contract shape without that extra evidence pass.
    """
    tasks = list(tasks)
    remotes = nodes_of_kind(model, "remote") + nodes_of_kind(model, "bindable")
    contracts = []
    for node in remotes:
        contracts.append({
            "name": node["name"],
            "type": _remote_kind(node.get("class_name", "")),
            "parent": node.get("parent", ""),
            "confidence": CONFIRMED,
            "rate_limited": _has_rate_limit_evidence(node["name"], tasks),
            "purpose": "",
            "server_validates": [],
            "tests": [],
            "enrichment_source": None,
            "note": ("purpose/server_validates/tests are empty until "
                    "annotate_contract() records where that information "
                    "came from — Nex never infers remote intent from its "
                    "name alone."),
        })
    return contracts


def annotate_contract(contract: Dict[str, Any], *,
                      purpose: Optional[str] = None,
                      server_validates: Optional[List[str]] = None,
                      tests: Optional[List[str]] = None,
                      source: str = "manual") -> Dict[str, Any]:
    """Record enrichment with an explicit, auditable source.

    `source` should name WHERE this came from ("manual", "llm-diagnosis",
    "code-review"), never silently overwritten with "unknown" — a
    contract's enrichment is only as trustworthy as its declared source.
    """
    out = dict(contract)
    if purpose is not None:
        out["purpose"] = purpose
    if server_validates is not None:
        out["server_validates"] = list(server_validates)
    if tests is not None:
        out["tests"] = list(tests)
    out["enrichment_source"] = source
    return out


def unvalidated_contracts(contracts: Any) -> List[Dict[str, Any]]:
    """Remotes with no recorded server-side validation at all — the
    highest-priority multiplayer-correctness gap this model can surface."""
    return [c for c in contracts if not c.get("server_validates")]
