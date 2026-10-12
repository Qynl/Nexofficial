"""Shared, strict resolution of a live MCP tool view."""
from __future__ import annotations

from typing import Any, Optional


def lookup_tool(registry: Any, tool: Any,
                server: Optional[str] = None) -> Any:
    """Resolve a tool without guessing across server namespaces.

    A qualified tool name is exact. If ``server`` is also supplied, its
    prefix must agree; report/planning lookups fail closed on bad registry
    data rather than allowing lookup exceptions to escape.
    """
    if not isinstance(tool, str) or not tool:
        return None
    try:
        if server:
            server_name = str(server)
            view = registry.server(server_name)
            if view is None:
                return None
            if "." in tool:
                prefix, bare = tool.split(".", 1)
                if prefix != server_name:
                    return None
                return view.by_name(bare)
            return view.by_name(tool)
        return registry.by_name(tool)
    except Exception:  # noqa: BLE001 - lookup must fail closed
        return None
