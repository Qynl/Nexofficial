"""Capability registry abstraction (STAGE 4/17/26).

The agent works against a *projection* of live MCP servers, not against the
transport directly. This keeps the planner/loop testable with a mock and
lets the real ``Upstream`` objects plug in via ``from_upstreams``.

A ``CapabilityRegistry`` is LIVE MCP truth: it only contains tools that a
connected server actually exposed during discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from mcp.capability import ToolCapability, capability_for_tool


@dataclass
class ToolView:
    server: str
    name: str                       # bare tool name (no namespace)
    full_name: str                 # server.name + "." + name
    description: str
    schema: Dict[str, Any]
    capability: ToolCapability
    annotations: Dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> Dict[str, Any]:
        return {
            "server": self.server,
            "name": self.name,
            "full_name": self.full_name,
            "description": (self.description or "")[:240],
            "category": self.capability.category,
            "capability": self.capability.to_dict(),
        }


@dataclass
class ServerView:
    name: str
    client: Any                    # object with .call(tool, args) -> dict
    tools: List[ToolView] = field(default_factory=list)
    protocol: Optional[str] = None
    health: str = "ok"
    latency_ms: Optional[float] = None
    resources: int = 0
    prompts: int = 0
    last_error: Optional[str] = None

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        return self.client.call(tool, args or {})

    def by_name(self, name: str) -> Optional[ToolView]:
        for t in self.tools:
            if t.name == name or t.full_name == name:
                return t
        return None


class CapabilityRegistry:
    def __init__(self, servers: List[ServerView]) -> None:
        self.servers = list(servers)

    # ----- queries --------------------------------------------------------
    def server(self, name: str) -> Optional[ServerView]:
        for s in self.servers:
            if s.name == name:
                return s
        return None

    def all_tools(self) -> List[ToolView]:
        out: List[ToolView] = []
        for s in self.servers:
            out.extend(s.tools)
        return out

    def find_category(self, category: str) -> List[ToolView]:
        return [t for t in self.all_tools()
                if t.capability.category == category]

    def by_name(self, name: str) -> Optional[ToolView]:
        for t in self.all_tools():
            if t.name == name or t.full_name == name:
                return t
        return None

    def call(self, server: str, tool: str,
             args: Dict[str, Any]) -> Dict[str, Any]:
        s = self.server(server)
        if s is None:
            return {"error": "no such server: " + str(server)}
        return s.call(tool, args)

    # ----- compact capability summary (STAGE 26) --------------------------
    def relevant_tools(self, goal: str, limit: int = 40
                       ) -> "tuple[List[ToolView], int]":
        """Relevance-filtered view for huge MCP catalogs.

        When many servers with many tools are connected, dumping the whole
        catalog into a planning prompt both wastes context and buries the
        useful tools. This scores every tool against the goal with simple
        lexical overlap (goal words vs tool name + description + category)
        and returns the top `limit` plus the omitted count:
        (tools, omitted).

        Scoring is intentionally dumb-but-honest — it FILTERS, it never
        invents. Validation (validate_plan_deep) always runs against the
        FULL registry, so a filtered-out tool remains usable if the model
        names it anyway.
        """
        import re as _re
        words = [w for w in _re.findall(r"[a-z0-9]+", (goal or "").lower())
                 if len(w) > 2]
        stop = {"the", "and", "for", "with", "that", "this", "into", "from",
                "make", "create", "build", "game", "using", "then", "add"}
        words = [w for w in words if w not in stop] or words

        def _score(tv: ToolView) -> int:
            name = tv.name.lower()
            desc = (tv.description or "").lower()
            cat = (tv.capability.category if tv.capability else "").lower()
            score = 0
            for w in words:
                if w in name:
                    score += 3          # name hits weigh most
                elif w in name.replace("_", ""):
                    score += 2
                if w in desc:
                    score += 1
                if w == cat:
                    score += 1
            return score

        tools = self.all_tools()
        if len(tools) <= limit:
            return tools, 0
        scored = sorted(tools, key=lambda tv: (_score(tv), tv.full_name),
                        reverse=True)
        return scored[:limit], len(tools) - limit

    def compact_summary(self) -> Dict[str, Any]:
        return {
            "servers": [
                {
                    "name": s.name,
                    "health": s.health,
                    "tools": [t.to_summary() for t in s.tools],
                }
                for s in self.servers
            ]
        }

    # ----- builder from live Upstream objects -----------------------------
    @classmethod
    def from_upstreams(cls, upstreams: List[Any]) -> "CapabilityRegistry":
        servers: List[ServerView] = []
        for up in upstreams:
            tools: List[ToolView] = []
            raw = []
            try:
                raw = up.tools() or []
            except Exception:  # noqa: BLE001
                raw = []
            for t in raw:
                # Raw upstream tools may not carry our `_capability`
                # field, so classify from live annotations/fall back to the
                # heuristic. This keeps the agent's view consistent with
                # the LIVE MCP truth.
                cap = capability_for_tool(t)
                # Operator capability registry (severity-max): a local
                # pin may escalate this view; it can never downgrade.
                from mcp.capability import apply_capability_registry
                cap = apply_capability_registry(cap, up.name,
                                                t.get("name", ""))
                tools.append(ToolView(
                    server=up.name,
                    name=t.get("name", ""),
                    full_name=up.name + "." + t.get("name", ""),
                    description=t.get("description", ""),
                    schema=t.get("inputSchema", {}) or {},
                    capability=cap,
                    annotations=t.get("annotations", {}) or {},
                ))
            st = up.status() if hasattr(up, "status") else {}
            servers.append(ServerView(
                name=up.name,
                client=_UpstreamClient(up),
                tools=tools,
                protocol=st.get("protocol_version"),
                health=st.get("health", "ok"),
                latency_ms=st.get("latency_ms"),
                resources=st.get("resources_count", 0),
                prompts=st.get("prompts_count", 0),
                last_error=st.get("last_error"),
            ))
        return cls(servers)


class _UpstreamClient:
    """Adapts a real Upstream so .call() returns a normalized dict."""

    def __init__(self, up: Any) -> None:
        self.up = up

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = self.up.call(tool, args or {})
        except Exception as exc:  # noqa: BLE001
            return {"error": type(exc).__name__ + ": " + str(exc)}
        if isinstance(resp, dict) and "error" in resp:
            err = resp["error"]
            msg = err.get("message", "error") if isinstance(err, dict) else str(err)
            return {"error": msg}
        result = resp.get("result") if isinstance(resp, dict) else resp
        return {"result": result}
