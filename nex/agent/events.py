"""Semantic agent events + states (STAGE 21 / STAGE 20).

The frontend does NOT need to understand MCP internals — it only reacts
to these semantic events. The agent publishes them on the shared EventBus
(server.BUS) and the front-end maps them to the existing face states.
"""

from __future__ import annotations

# Coarse agent lifecycle states. These are NOT emotion states; the
# frontend maps each to an existing EMOTION state (see app.js).
AGENT_STATES = (
    "PLANNING", "OBSERVING", "EXECUTING", "VERIFYING",
    "REPAIRING", "WAITING", "COMPLETED", "BLOCKED", "ERROR",
)

# Event type strings the agent emits. Convention: type starts with
# "agent." so the SSE handler / frontend can route them.
EVENT_PLAN_STARTED = "agent.plan_started"
EVENT_PLAN_UPDATED = "agent.plan_updated"
EVENT_TASK_STARTED = "agent.task_started"
EVENT_TOOL_CALLED = "agent.tool_called"
EVENT_TOOL_SUCCEEDED = "agent.tool_succeeded"
EVENT_TOOL_FAILED = "agent.tool_failed"
EVENT_VERIFICATION_STARTED = "agent.verification_started"
EVENT_VERIFICATION_PASSED = "agent.verification_passed"
EVENT_VERIFICATION_FAILED = "agent.verification_failed"
EVENT_REPAIR_STARTED = "agent.repair_started"
EVENT_REPAIR_SUCCEEDED = "agent.repair_succeeded"
EVENT_WAITING_FOR_CONFIRMATION = "agent.waiting_for_confirmation"
EVENT_MCP_CONNECTED = "agent.mcp_connected"
EVENT_MCP_DISCONNECTED = "agent.mcp_disconnected"
EVENT_PROJECT_COMPLETED = "agent.project_completed"
EVENT_PROJECT_BLOCKED = "agent.project_blocked"

# Completion status vocabulary (STAGE 28).
STATUS_COMPLETED = "COMPLETED"
STATUS_PARTIAL = "PARTIAL"
STATUS_BLOCKED = "BLOCKED"
STATUS_FAILED = "FAILED"
STATUS_WAITING_USER = "WAITING_FOR_USER"


def agent_event(agent_state=None, event_type=None, **payload) -> dict:
    """Build a semantic agent event dict for the EventBus."""
    evt: dict = {"type": event_type or "agent"}
    if agent_state:
        evt["agentState"] = agent_state
    evt.update(payload)
    return evt
