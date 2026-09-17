"""MCP discovery helpers (STAGE 3 / STAGE 5).

These operate on an *upstream-like* object that exposes:
    connect()                       -> perform initialize handshake
    tools()                        -> list of tool dicts
    resources()  (optional)        -> list of resource dicts
    prompts()   (optional)         -> list of prompt dicts
    call(method, params)           -> raw JSON-RPC (for advanced discovery)

The real ``Upstream`` in upstream.py satisfies this; mock servers in the
tests satisfy it too, so discovery is testable without real engines.

The result is the LIVE MCP truth: what the server actually exposes. We
never invent tools that aren't discovered here.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from mcp.capability import capability_for_tool, ToolCapability


def discover_server(up) -> Dict[str, Any]:
    """Connect + enumerate a server, returning a snapshot.

    Returns a dict with: connected, protocol_version, tools (each enriched
    with `_capability`), resources, prompts, latency_ms, health, error.
    Failures are reported in the snapshot rather than raised, so a single
    dead server never aborts discovery of the others.
    """
    t0 = time.monotonic()
    snapshot: Dict[str, Any] = {
        "connected": False,
        "protocol_version": None,
        "tools": [],
        "resources": [],
        "prompts": [],
        "latency_ms": None,
        "health": "down",
        "error": None,
    }
    try:
        up.connect()
    except Exception as exc:  # noqa: BLE001
        snapshot["error"] = _safe_err(exc)
        snapshot["health"] = "down"
        return snapshot

    snapshot["connected"] = True
    snapshot["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)

    # Tools (required by spec).
    try:
        raw_tools = up.tools()
    except Exception as exc:  # noqa: BLE001
        snapshot["error"] = _safe_err(exc)
        snapshot["health"] = "degraded"
        raw_tools = []
    enriched = []
    for t in (raw_tools or []):
        cap = capability_for_tool(t)  # type: ToolCapability
        et = dict(t)
        et["_capability"] = cap.to_dict()
        enriched.append(et)
    snapshot["tools"] = enriched
    if raw_tools:
        snapshot["health"] = "ok"

    # Resources (optional).
    if hasattr(up, "resources"):
        try:
            snapshot["resources"] = list(up.resources() or [])
        except Exception:  # noqa: BLE001
            snapshot["resources"] = []
    # Prompts (optional).
    if hasattr(up, "prompts"):
        try:
            snapshot["prompts"] = list(up.prompts() or [])
        except Exception:  # noqa: BLE001
            snapshot["prompts"] = []

    return snapshot


def _safe_err(exc: Exception) -> str:
    return (type(exc).__name__ + ": " + str(exc))[:300]
