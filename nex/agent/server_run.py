"""Server-facing integration for the autonomous agent (STAGE 5/16/21).

Thin glue: build a live CapabilityRegistry from the running TunnelRegistry
and run the AutonomousAgent. Kept separate from server.py so the HTTP layer
stays a thin entrypoint (STAGE 22) and the agent logic is independently
testable.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional


def _registry() -> Any:
    from tunnels import get_tunnels
    from agent.registry import CapabilityRegistry
    # Use the live Upstream objects (not status-dict summaries) so the
    # registry can discover tools/resources/prompts directly.
    return CapabilityRegistry.from_upstreams(get_tunnels().raw_upstreams())


def agent_capabilities() -> Dict[str, Any]:
    """Compact capability summary of every live MCP server."""
    try:
        reg = _registry()
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc), "servers": []}
    return reg.compact_summary()


def run_agent_goal(goal: str, bus: Optional[Callable[[dict], None]] = None,
                   policy=None) -> Any:
    """Run a goal to completion against the live registry.

    Emits semantic agent events on `bus` (the server's EventBus) so the
    frontend can react. Returns a CompletionReport-like dict.
    """
    from agent.loop import AutonomousAgent
    from mcp.policy import current_policy, set_policy

    if policy is not None:
        set_policy(policy)
    reg = _registry()
    agent = AutonomousAgent(reg, bus=bus, policy=current_policy())

    # mcp-connected / disconnected bookkeeping for the frontend.
    if bus is not None:
        for s in reg.servers:
            bus(agent.agent_event if hasattr(agent, "agent_event") else None)
    return agent.run(goal)
