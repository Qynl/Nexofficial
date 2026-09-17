"""In-process mock MCP server for tests (STAGE 10 / STAGE 24 / STAGE 25).

Implements the same surface the agent expects from a real upstream:
``connect()``, ``tools()``, ``resources()``, ``prompts()``, ``call()``.
No external engine required. Supports deterministic failure injection so the
autonomous loop's repair/retry path can be exercised.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from agent.registry import ServerView, ToolView
from mcp.capability import capability_for_tool


class MockMCPServer:
    def __init__(self, name: str, tools: List[Dict[str, Any]],
                 fail: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.name = name
        self._tools = [dict(t) for t in tools]
        # fail: {tool_name: {"count": N, "error": "..."}} -> first N calls fail.
        self._fail = {k: dict(v) for k, v in (fail or {}).items()}

    def connect(self) -> None:
        pass

    def tools(self) -> List[Dict[str, Any]]:
        return [dict(t) for t in self._tools]

    def resources(self) -> List[Dict[str, Any]]:
        return []

    def prompts(self) -> List[Dict[str, Any]]:
        return []

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        # Failure injection.
        spec = self._fail.get(tool)
        if spec and spec.get("count", 0) > 0:
            spec["count"] -= 1
            return {"error": spec.get("error", "injected failure")}

        result: Dict[str, Any] = self._synthetic(tool, args or {})
        return {"result": result}

    def _synthetic(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool == "create_project":
            return {"id": "proj_1", "name": args.get("name", "game")}
        if tool == "create_asset":
            return {"id": "asset_1", "type": args.get("type", "asset"),
                    "name": args.get("name", "asset")}
        if tool == "import_asset":
            return {"id": "imported_1", "asset": args.get("asset", "asset_1")}
        if tool == "create_animation":
            return {"id": "anim_1", "name": args.get("name", "anim"),
                    "rig": args.get("rig")}
        if tool == "create_script":
            return {"id": "script_1", "name": args.get("name", "script")}
        if tool == "build":
            return {"id": "build_1", "ok": True}
        if tool == "run_game":
            return {"id": "run_1", "ok": True}
        if tool == "inspect_logs":
            return {"logs": ["boot ok"], "errors": []}
        if tool == "verify_asset":
            if args.get("asset"):
                return {"valid": True, "asset": args["asset"]}
            return {"valid": False}
        if tool == "verify_game":
            if args.get("build") or args.get("id"):
                return {"playable": True}
            return {"playable": False}
        # Generic fallback.
        return {"ok": True, "tool": tool}


def server_view(name: str, mock: MockMCPServer,
                protocol: str = "2025-06-18") -> ServerView:
    """Build a ServerView (with classified capabilities) from a mock server."""
    tools: List[ToolView] = []
    for t in mock.tools():
        cap = capability_for_tool(t)
        tools.append(ToolView(
            server=name, name=t.get("name", ""), full_name=name + "." + t.get("name", ""),
            description=t.get("description", ""), schema=t.get("inputSchema", {}) or {},
            capability=cap, annotations=t.get("annotations", {}) or {},
        ))
    return ServerView(name=name, client=mock, tools=tools, protocol=protocol,
                      health="ok")
