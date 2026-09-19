"""Security policy + MCP-only enforcement (STAGE 4 / STAGE 18).

The policy is the architectural security boundary. It is NOT a prompt
instruction — the planner/agent calls ``authorize()`` BEFORE every
external action, and a denial is a hard stop, not a suggestion.

Trust model
  * SERVER trust is the operator's explicit act of CONNECTING a tunnel
    (token-protected settings page / built-in catalog / env). There is no
    other way for a server to enter the capability surface, so an
    allow-list of servers would only re-state that act; the narrowing
    happens at the TOOL level instead.
  * TOOL trust is the policy's job. A connected server may expose many
    tools; Nex must not trust what it cannot classify:
      - tools whose name heuristics cannot place (UNKNOWN) require
        CONFIRMATION by default (Policy.confirm_unknown) instead of
        silently passing, and
      - any external tool named after a process-execution primitive
        (PROCESS_EXECUTION) ALWAYS requires confirmation, and
      - destructive / network categories require confirmation.
    Explicit server/tool allow-lists (Policy fields) tighten further.
  * ``run_command`` is Nex's OWN shell and is NEVER authorized, on any
    server, under any configuration.
  * MCP annotations (readOnlyHint & co) are untrusted hints supplied by
    the server itself — see capability.py; they can only raise caution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from mcp.capability import (
    READ, CREATE, MODIFY, BUILD, TEST, DESTRUCTIVE, NETWORK, UNKNOWN,
    ToolCapability, apply_capability_registry,
)


# THE CAPABILITY BOUNDARY. Nex's AI can act ONLY through explicitly
# connected MCP servers (authorized via `server`). These internal names
# are MCP-protocol introspection (part of the MCP layer itself).
# EVERYTHING else internal — filesystem, shell, host scanning,
# compile/validate helpers — is NOT an AI capability, even though the
# code exists as Nex infrastructure. Not policy-gated: structurally
# absent from the boundary.
INTERNAL_ALLOWED = frozenset({
    "who_am_i", "list_platforms", "tunnel_status", "tunnel_probe",
})

# Tools that are ALWAYS treated as external/unsafe (never auto-allowed).
# NOTE: matched against the BARE tool name — a connected MCP server that
# exposes a tool literally named `run_command` is denied too.
ALWAYS_DENIED = frozenset({"run_command"})

# Process-execution tool names on EXTERNAL servers. A connected engine
# may legitimately expose console/command tools; any such tool is a
# high-risk PROCESS capability and ALWAYS requires confirmation.
# (run_command itself is Nex's internal shell and is always DENIED above
# — the two rules are deliberately separate so the policy has one
# unambiguous statement of truth per name.)
PROCESS_EXECUTION = frozenset({"execute_command", "exec"})


# THE BOUNDARY IS NOT CONFIGURABLE. There is no flag, env var, or runtime
# call that turns it off. (If a developer ever wants an unrestricted
# playground, that must be a separate, explicitly non-autonomous dev
# server — never a switch on the production agent.)
MCP_ONLY = True


@dataclass
class Policy:
    """Security policy. NOTE: the MCP-only capability boundary is a module
    constant (MCP_ONLY) and deliberately NOT a policy field."""
    server_allowlist: Optional[Set[str]] = None   # None = all servers allowed
    tool_allowlist: Dict[str, Set[str]] = field(default_factory=dict)
    require_confirm_categories: Set[str] = field(
        default_factory=lambda: {DESTRUCTIVE, NETWORK})
    max_retries: int = 3
    # External tools whose name the heuristics cannot classify (UNKNOWN):
    # require a human confirmation instead of passing silently. Never
    # "allow without question" — that is the default-deny half of the
    # design (operators can still pin exact tools via tool_allowlist).
    confirm_unknown: bool = True
    # --- the TRUSTED SERVER REGISTRY (strict mode) -------------------------
    # CONNECTED != TRUSTED. When strict_servers is enabled (the deployed
    # server enables it; see server._build_policy), an external tool call
    # is allowed only if its server is in `trusted_servers`. The registry
    # is built by the composition root from the built-in catalog + the
    # operator's NEX_TRUSTED_SERVERS — never by the model. strict_servers
    # with trusted_servers=None fails CLOSED (nothing external passes):
    # a missing registry is a configuration error, not a permission.
    strict_servers: bool = False
    trusted_servers: Optional[Set[str]] = None


@dataclass
class Decision:
    allowed: bool
    requires_confirmation: bool
    reason: str
    category: str = UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
            "category": self.category,
        }


# Module-global policy (allow-lists only — the boundary is not in here).
_CURRENT: Policy = Policy()


def current_policy() -> Policy:
    return _CURRENT


def set_policy(p: Policy) -> None:
    global _CURRENT
    _CURRENT = p


def authorize(server: Optional[str], tool: str,
              capability: Optional[ToolCapability] = None,
              policy: Optional[Policy] = None) -> Decision:
    """Authorize a single tool call. Returns a Decision.

    `server` is None for Nex-internal tools, or the upstream name for an
    MCP-exposed tool. `capability` carries the classification.
    """
    pol = policy or _CURRENT
    cap = capability or ToolCapability()
    cat = cap.category

    # 1) Always-denied tools (shell, etc.) — bare tool name, so an
    # external server exposing a tool named `run_command` is denied too.
    if tool in ALWAYS_DENIED:
        return Decision(False, False,
                        "tool '%s' is never authorized" % tool, cat)

    # 1b) Process-execution tool names on an EXTERNAL server: high-risk
    # PROCESS capability, always confirmation. (Internal tools with these
    # names fall through to the boundary below and are denied.)
    if tool in PROCESS_EXECUTION and server is not None:
        return Decision(True, True,
                        "process execution is high-risk: confirmation "
                        "required", "PROCESS")

    # 2) Internal Nex runtime tool — ONLY the MCP introspection set.
    # Everything else internal (filesystem/shell/host tools) is not an AI
    # capability, regardless of flags.
    if server is None or server == "__internal__":
        if tool in INTERNAL_ALLOWED:
            return Decision(True, False,
                            "internal Nex tool (MCP introspection)", cat)
        # THE BOUNDARY: everything else internal is infrastructure —
        # impossible, not merely discouraged.
        return Decision(False, False,
                        "boundary: '%s' is not a Nex capability" % tool, cat)

    # 3) External MCP tool.
    # Operator capability registry first: a local pin may ESCALATE the
    # classification (severity-max — it can never downgrade a tool).
    cap = apply_capability_registry(cap, server, tool)
    cat = cap.category
    # Trusted-server registry (strict mode): CONNECTED != TRUSTED.
    if pol.strict_servers and pol.trusted_servers is None:
        return Decision(False, False,
                        "strict server mode is enabled but no trusted "
                        "server registry is configured — failing closed",
                        cat)
    if (pol.strict_servers
            and server not in (pol.trusted_servers or set())):
        return Decision(False, False,
                        "server '%s' is not in the trusted server "
                        "registry (strict mode: connecting a server does "
                        "not trust it — extend NEX_TRUSTED_SERVERS)"
                        % server, cat)
    # Server allow-list.
    if pol.server_allowlist is not None and server not in pol.server_allowlist:
        return Decision(False, False,
                        "server '%s' not in allow-list" % server, cat)
    # Tool allow-list for this server.
    allowed_tools = pol.tool_allowlist.get(server)
    if allowed_tools is not None and tool not in allowed_tools:
        return Decision(False, False,
                        "tool '%s' not allowed on server '%s'" % (tool, server),
                        cat)

    # MCP-only mode: external actions are exactly what MCP is for, so they
    # are permitted (subject to confirmation). Non-MCP external paths are
    # already blocked because they have no server. So we just continue.
    requires_confirm = (
        cap.destructive
        or cat in pol.require_confirm_categories
        # Untrusted-by-default: an unclassifiable external tool is not
        # "safe, nobody looked"; it gets a human confirmation.
        or (cat == UNKNOWN and pol.confirm_unknown)
    )
    return Decision(True, requires_confirm,
                    "external MCP tool", cat)
