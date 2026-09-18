"""Security policy + MCP-only enforcement (STAGE 4 / STAGE 18).

The policy is the architectural security boundary. It is NOT a prompt
instruction — the planner/agent calls ``authorize()`` BEFORE every
external action, and a denial is a hard stop, not a suggestion.

Key ideas:
  * External actions must go through a connected MCP server. Nex's own
    internal runtime tools (list_files, read_file, write_file, speak,
    log_event, the MCP meta tools, the mc_tools primitives) are allowed
    because they are Nex's own operation, not arbitrary PC control.
  * ``run_command`` (shell) is NEVER authorized in MCP-only mode and is
    considered unsafe even outside it unless explicitly allow-listed.
  * Destructive / network categories require confirmation by default.
  * Server / tool allow-lists narrow the surface; absent allow-list =
    allow all (so the default deployment is permissive but still bounded
    by the MCP-only switch).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from mcp.capability import (
    READ, CREATE, MODIFY, BUILD, TEST, DESTRUCTIVE, NETWORK, UNKNOWN,
    ToolCapability,
)


# THE CAPABILITY BOUNDARY. Nex's AI can act ONLY through:
#   1. explicitly connected MCP servers (authorized via `server`), and
#   2. the explicitly implemented Amazon Music connector (server
#      "amazon-music", an allowlisted pseudo-upstream).
# These internal names are MCP-protocol introspection (part of the MCP
# layer itself). EVERYTHING else internal — filesystem, shell, host
# scanning, compile/validate helpers — is NOT an AI capability, even
# though the code exists as Nex infrastructure. Not policy-gated:
# structurally absent from the boundary.
INTERNAL_ALLOWED = frozenset({
    "who_am_i", "list_platforms", "tunnel_status", "tunnel_probe",
})

# Amazon Music controls (server "amazon-music"). The connector is a
# pseudo-upstream, so these authorize through the normal server path;
# listed here for the bare-name (unprefixed) call form.
MUSIC_ALLOWED = frozenset({
    "am_play", "am_pause", "am_toggle", "am_next", "am_previous",
    "am_volume", "am_search_play",
})

# Tools that are ALWAYS treated as external/unsafe (never auto-allowed).
ALWAYS_DENIED = frozenset({"run_command"})


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
    allow_external_unknown: bool = True


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

    # 1) Always-denied tools (shell, etc.).
    if tool in ALWAYS_DENIED:
        return Decision(False, False,
                        "tool '%s' is never authorized" % tool, cat)

    # 1b) Process execution (when the operator enables the shell) is a
    # high-risk capability: always requires explicit confirmation, even
    # though it is technically allowed.
    if tool in ("run_command", "execute_command", "exec"):
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
        if tool in MUSIC_ALLOWED:
            return Decision(True, False,
                            "Amazon Music control (explicit allowlist)", cat)
        # THE BOUNDARY: everything else internal is infrastructure —
        # impossible, not merely discouraged.
        return Decision(False, False,
                        "boundary: '%s' is not a Nex capability" % tool, cat)

    # 3) External MCP tool.
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
        cap.destructive or cat in pol.require_confirm_categories
    )
    return Decision(True, requires_confirm,
                    "external MCP tool", cat)
