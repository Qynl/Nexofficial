"""NEX MCP tunnel registry.

An MCP tunnel routes `tools/call` from Nex clients to whichever
upstream (Roblox Studio plugin, Unreal Remote Control bridge,
Blender addon bridge, etc.) actually owns the requested tool.

Design:

  * Each tunnel wraps an `Upstream`. They start cold; first call
    runs the MCP `initialize` handshake.
  * `aggregated_tools()` returns ONE list, where every upstream
    tool is namespaced as `<upstream>.<tool>` plus Nex's own
    unprefixed tools.
  * `route(name, args)` parses the namespace and forwards. Names
    without a prefix stay local (Nex's own `tool_*` calls).
  * If a tunnel is down, `aggregated_tools()` simply skips it; if
    a client calls a tool whose upstream is offline we return a
    JSON-RPC error saying the upstream isn't connected.

Thread-safety: tools/list is read-mostly and Nex's HTTP handler is
already serialised (BaseHTTPRequestThread per request), so we keep
this simple without a lock. The internal `Upstream` object pools
its own connection state per call.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from upstream import (
    Upstream, UpstreamError, default_registry, DEFAULT_TUNNELS,
    _parse_extra_tunnels,
)


# Max bytes for a tool description we expose to clients. The MCP 2025
# spec caps a single tool description at 2^16 chars; we cap at 8KB to
# keep token costs modest when aggregating many upstreams.
_DESC_CAP = 8 * 1024


class TunnelRegistry:
    """Owns Upstream instances and aggregates their tool catalogs."""

    def __init__(self, upstreams: Optional[List[Upstream]] = None) -> None:
        # Copy so callers can't mutate our state.
        self._upstreams: List[Upstream] = list(upstreams or default_registry())
        # Reentrant: reads that call other reads (summary -> list_upstreams).
        # Snapshot reads: we copy the list under the lock, then touch the
        # upstreams OUTSIDE it so slow network I/O never serializes the
        # registry against itself under ThreadingHTTPServer concurrency.
        self._lock = threading.RLock()
        self._local_tools_fn: Optional[Callable[[], List[Dict[str, Any]]]] = None
        self._local_router: Optional[Callable[[str, Dict[str, Any]],
                                              Dict[str, Any]]] = None
        # Tool prefix characters we accept on the wire.
        self._prefix_re = re.compile(r"^[a-zA-Z0-9_-]{1,32}\.(.*)$")

    # ----- wiring ---------------------------------------------------------

    def bind_local(self, *,
                   tool_provider: Callable[[], List[Dict[str, Any]]],
                   tool_router: Callable[[str, Dict[str, Any]],
                                         Dict[str, Any]]) -> None:
        """Hook the registry up to Nex's own sandbox tool set.

        `tool_provider` returns the local tool schemas (no prefix).
        `tool_router(name, args)` invokes a local tool by name and
        returns the MCP-style result envelope.
        """
        with self._lock:
            self._local_tools_fn = tool_provider
            self._local_router = tool_router

    # ----- introspection --------------------------------------------------

    def list_upstreams(self) -> List[Dict[str, Any]]:
        with self._lock:
            snapshot = list(self._upstreams)
        return [u.status() for u in snapshot]

    def raw_upstreams(self) -> List[Upstream]:
        """Return the live ``Upstream`` objects (not status summaries).

        The agent layer uses this to build a capability registry straight
        from the connected servers rather than from cached status dicts.
        """
        with self._lock:
            return list(self._upstreams)

    def summary(self) -> Dict[str, Any]:
        statuses = self.list_upstreams()
        online = sum(1 for s in statuses if not s["last_error"]
                     or (s.get("tools_count", 0) > 0))
        # TRUST state (CONNECTED != TRUSTED): in strict server mode only
        # servers in the trusted registry may be called (mcp.policy). The
        # UI shows this; the gateway enforces it.
        strict = False
        trusted: set = set()
        try:
            from mcp.policy import current_policy
            pol = current_policy()
            strict = bool(getattr(pol, "strict_servers", False))
            trusted = set(getattr(pol, "trusted_servers", None) or set())
        except Exception:  # noqa: BLE001
            pass
        for s in statuses:
            s["trusted"] = (not strict) or (s.get("name") in trusted)
        return {
            "tunnels": statuses,
            "online": online,
            "total": len(statuses),
            "platforms": [s["label"] for s in statuses],
            "names":    [s["name"]  for s in statuses],
            "strict": strict,
            "trusted_total": len(trusted),
            "trusted_online": sum(1 for s in statuses if s.get("trusted")),
        }

    # ----- aggregates -----------------------------------------------------

    def aggregated_tools(self) -> List[Dict[str, Any]]:
        """Return the merged tool list as MCP clients see it.

        Local tools are unprefixed. Upstream tools are prefixed with
        `<upstream_name>.`. The upstream's own description is
        preserved (truncated to 8KB) and — for the engines Nex curates
        (Roblox Studio, Unreal Engine) — enriched with a curated,
        model-friendly explanation from ``mcp_engines`` so the AI
        assistant can actually use the tool well. Every entry is
        annotated with the platform it belongs to.
        """
        # Lazily import the MCP-only engine adapter so tunnels.py stays
        # usable even if that module is absent, and to avoid a top-level
        # import cycle.
        try:
            from mcp_engines import enrich_upstream_tools
            _enrich = enrich_upstream_tools
        except Exception:  # noqa: BLE001
            _enrich = None
        out: List[Dict[str, Any]] = []
        try:
            from mcp.capability import capability_for_tool
            _cap = capability_for_tool
        except Exception:  # noqa: BLE001
            _cap = None
        if self._local_tools_fn is not None:
            for t in self._local_tools_fn() or []:
                e = dict(t)
                if _cap is not None:
                    e["_capability"] = _cap(t).to_dict()
                out.append(e)
        with self._lock:
            snapshot = list(self._upstreams)
        for u in snapshot:
            try:
                tools = u.tools()
            except UpstreamError:
                # Cold upstream; keep going.
                continue
            if not tools:
                continue
            label = u.label or u.name
            for t in tools:
                # Enrich (curated explanation) before prefixing + capping.
                if _enrich is not None:
                    try:
                        t = _enrich(u.name, t)
                    except Exception:  # noqa: BLE001
                        t = dict(t)
                schema = t.get("inputSchema") or {"type": "object",
                                                  "properties": {}}
                desc = (t.get("description") or "").strip()
                if len(desc) > _DESC_CAP:
                    desc = desc[:_DESC_CAP] + "…"
                entry = {
                    "name": u.name + "." + (t.get("name") or "tool"),
                    "description": ("[" + label + "] " + desc
                                    if desc
                                    else "[" + label + "] tool"),
                    "inputSchema": schema,
                }
                if _cap is not None:
                    entry["_capability"] = _cap(t).to_dict()
                out.append(entry)
        return out

    # ----- live discovery (STAGE 3/4/26) --------------------------------

    def discover_all(self) -> List[Dict[str, Any]]:
        """Discover tools/resources/prompts for every upstream (live truth).

        Each entry is the actual server response — we never pretend a tool
        exists that the live server did not expose. Failures are reported
        per-server rather than aborting the whole sweep.
        """
        try:
            from mcp.discovery import discover_server
        except Exception:  # noqa: BLE001
            discover_server = None
        out: List[Dict[str, Any]] = []
        for u in self._upstreams:
            if discover_server is not None:
                snap = discover_server(u)
            else:
                try:
                    snap = {"connected": True, "tools": u.tools(),
                            "resources": [], "prompts": []}
                except Exception:  # noqa: BLE001
                    snap = {"connected": False, "tools": []}
            snap["name"] = u.name
            snap["label"] = u.label or u.name
            out.append(snap)
        return out

    def server_snapshot(self) -> List[Dict[str, Any]]:
        """Compact per-server health + capability summary for the registry."""
        out: List[Dict[str, Any]] = []
        for u in self._upstreams:
            st = u.status()
            out.append({
                "name": st.get("name"),
                "label": st.get("label"),
                "health": st.get("health"),
                "connected": st.get("initialized"),
                "protocol_version": st.get("protocol_version"),
                "latency_ms": st.get("latency_ms"),
                "tools_count": st.get("tools_count"),
                "resources_count": st.get("resources_count"),
                "prompts_count": st.get("prompts_count"),
                "last_error": st.get("last_error"),
                "circuit_open_seconds": st.get("circuit_open_seconds"),
            })
        return out

    # ----- routing --------------------------------------------------------

    def route(self, tool_name: str,
              arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Route `tool_name` to its owner and return the MCP envelope.

        Returns one of:
          * {"content": [...], "isError": False}  — successful call
          * {"error": "..."}                     — handled locally
          * {"isError": True, "content": [...]}  — upstream rejected
        """
        m = self._prefix_re.match(tool_name) if "." in tool_name else None
        if m:
            upstream_name = tool_name.split(".", 1)[0]
            inner = tool_name.split(".", 1)[1]
            u = self._find(upstream_name)
            if u is None:
                return self._err("unknown tunnel: " + upstream_name)
            return self._call_upstream(u, inner, arguments)
        # Local tool.
        if self._local_router is None:
            return self._err("no local tool router registered")
        return self._local_router(tool_name, arguments or {})

    # ----- primitives -----------------------------------------------------

    def _find(self, name: str) -> Optional[Upstream]:
        with self._lock:
            for u in self._upstreams:
                if u.name == name:
                    return u
        return None

    def _call_upstream(self, u: Upstream, tool: str,
                       args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = u.call(tool, args or {})
        except UpstreamError as exc:
            return self._err("upstream " + u.name + " not reachable: "
                             + str(exc))
        result = resp.get("result", {})
        # Re-shape so the LLM gets the MCP "content" envelope.
        content = result.get("content")
        if isinstance(content, list):
            return {
                "content": content,
                "isError": bool(result.get("isError", False)),
            }
        # Upstream returned something else (string, dict) — wrap it.
        return {
            "content": [{"type": "text", "text": json_dumps(result)}],
            "isError": False,
        }

    def _err(self, msg: str) -> Dict[str, Any]:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "Tunnel error: " + msg}],
        }


def json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2,
                      sort_keys=True)[:_DESC_CAP]


# ---------------------------------------------------------------------------
# Singleton wiring. server.py imports this and binds the local tool
# provider at boot; the rest of the system reads TUNNELS.
# ---------------------------------------------------------------------------

_TUNNELS: Optional[TunnelRegistry] = None

# ---------------------------------------------------------------------------
# User-added MCP servers persist across restarts (~/.nex/tunnels.json).
# The + Add MCP Server UI writes here via POST /api/tunnels; DELETE
# /api/tunnels/<name> removes from here. Env-derived defaults always load
# too; saved user servers are appended on every (re)load.
# ---------------------------------------------------------------------------


def _user_tunnels_path() -> str:
    import os
    base = os.environ.get("NEX_USER_TUNNELS_FILE")
    if base:
        return base
    return os.path.join(os.path.expanduser("~"), ".nex", "tunnels.json")


def load_user_tunnels() -> List[Dict[str, Any]]:
    """Saved user-added MCP server configs (never the env defaults)."""
    import json as _json
    try:
        with open(_user_tunnels_path(), "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict) and c.get("name")]
    except (OSError, ValueError):
        pass
    return []


def save_user_tunnels(entries: List[Dict[str, Any]]) -> bool:
    import json as _json
    import os
    path = _user_tunnels_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(entries, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def add_user_tunnel(entry: Dict[str, Any]) -> bool:
    """Persist one user-added server (replaces same-name entry)."""
    entries = [c for c in load_user_tunnels()
               if c.get("name") != entry.get("name")]
    entries.append(entry)
    return save_user_tunnels(entries)


def remove_user_tunnel(name: str) -> bool:
    """Drop a saved server by name. Returns True if it was saved here."""
    entries = load_user_tunnels()
    kept = [c for c in entries if c.get("name") != name]
    if len(kept) == len(entries):
        return False
    return save_user_tunnels(kept)


def get_tunnels() -> TunnelRegistry:
    global _TUNNELS
    if _TUNNELS is None:
        _TUNNELS = TunnelRegistry()
    return _TUNNELS


def reload_tunnels(extra: Optional[List[Dict[str, Any]]] = None,
                    replace: bool = False) -> TunnelRegistry:
    """Re-read NEX_TUNNELS and rebuild the registry.

    `extra` is appended to the env-derived config; if `replace` is True,
    only `extra` is used (env is ignored). Used by:
      * /api/tunnels/reload — re-reads env.
      * POST /api/tunnels with a JSON body — installs `extra` and
        (by default) keeps the env-derived ones too.

    Entries may declare either:
      * ``url`` for HTTP, OR
      * ``transport="stdio"`` + ``command`` + ``args`` (list) for
        stdio MCP children (e.g. Roblox Studio's ``mcp.bat``).
    """
    global _TUNNELS
    # SECURITY: stdio entries mean "spawn this process" — reject anything
    # not on the operator allowlist (built-in catalog + NEX_STDIO_ALLOW).
    # The HTTP API returns 400 before it gets here; this is the
    # defense-in-depth net for every other caller.
    problems = validate_extra_entries(list(extra or []))
    if problems:
        raise ValueError("unsafe tunnel entries rejected: "
                         + "; ".join(problems))
    if replace or extra:
        cfg: List[Dict[str, Any]] = []
        if not replace:
            cfg += DEFAULT_TUNNELS
        cfg += _parse_extra_tunnels()
        cfg += load_user_tunnels()
        cfg += list(extra or [])
    else:
        cfg = (list(DEFAULT_TUNNELS) + _parse_extra_tunnels()
               + load_user_tunnels())
    # Fail-closed on persisted/env rows too: an old (pre-allowlist)
    # ~/.nex/tunnels.json could still carry a command no one vetted —
    # drop it from the live registry and say so, instead of spawning it.
    allow = stdio_allowlist()
    kept: List[Dict[str, Any]] = []
    for c in cfg:
        if _is_stdio_entry(c) and str(c.get("command") or "") not in allow:
            import sys as _sys
            _sys.stderr.write(
                "[nex] dropped non-allowlisted stdio tunnel %r\n"
                % (c.get("name"),))
            continue
        if not _is_stdio_entry(c):
            url = str(c.get("url") or "")
            if url and not url.startswith("stdio://"):
                ok, why = http_url_allowed(url)
                if not ok:
                    import sys as _sys
                    _sys.stderr.write(
                        "[nex] dropped non-allowlisted http tunnel %r: %s\n"
                        % (c.get("name"), why))
                    continue
        kept.append(c)
    cfg = kept
    from upstream import _apply_call_timeout
    upstreams = []
    for c in cfg:
        url = c.get("url") or _stdio_url_for(c)
        u = Upstream(c["name"], url,
                     c.get("label") or c["name"])
        _apply_call_timeout(u, c)
        if _is_stdio_entry(c):
            u._stdio_command = (
                c["command"], list(c.get("args") or []),
            )
        upstreams.append(u)
    fresh = TunnelRegistry(upstreams)
    if _TUNNELS is not None and _TUNNELS._local_tools_fn is not None:
        fresh.bind_local(
            tool_provider=_TUNNELS._local_tools_fn,
            tool_router=_TUNNELS._local_router,
        )
    _TUNNELS = fresh
    return _TUNNELS


def _is_stdio_entry(c: Dict[str, Any]) -> bool:
    if c.get("transport") == "stdio":
        return True
    if c.get("command") and not c.get("url"):
        return True
    return False


def _stdio_url_for(c: Dict[str, Any]) -> str:
    """URL sentinel for a stdio upstream row (Upstream.__init__
    refuses ``None`` — the sentinel lets it carry the row)."""
    return "stdio://" + c["name"]


# ---------------------------------------------------------------------------
# Stdio command allowlist.
#
# A stdio tunnel entry means "spawn this process and speak MCP to it" —
# i.e. arbitrary process execution as the server user. The HTTP API must
# never be able to spawn an arbitrary command (it was a remote code
# execution: POST /api/tunnels + probe ran any command).
#
# What IS allowed:
#   * commands that appear in DEFAULT_TUNNELS (the built-in editor
#     entrypoints — Roblox Studio MCP, etc.), and
#   * commands the operator explicitly allowlisted in NEX_STDIO_ALLOW
#     (comma-separated exact `command` strings, set in the server's
#     environment — not via the HTTP API).
# Everything else is rejected BEFORE it is registered or persisted.
# ---------------------------------------------------------------------------

def stdio_allowlist() -> List[str]:
    raw = os.environ.get("NEX_STDIO_ALLOW", "")
    allowed = [a.strip() for a in raw.split(",") if a.strip()]
    for c in DEFAULT_TUNNELS:
        if c.get("command"):
            allowed.append(c["command"])
    return allowed


def validate_extra_entries(entries: List[Any]) -> List[str]:
    """Return a list of human-readable rejection reasons for unsafe
    entries (empty list = all safe). Stdio entries must be on the
    command allowlist; HTTP url entries must point at loopback or an
    operator-allowlisted host (SSRF boundary)."""
    problems: List[str] = []
    allow = stdio_allowlist()
    for i, e in enumerate(entries or []):
        if not isinstance(e, dict):
            problems.append("entry %d: must be an object" % i)
            continue
        if not e.get("name"):
            problems.append("entry %d: missing 'name'" % i)
            continue
        if _is_stdio_entry(e):
            cmd = str(e.get("command") or "")
            if cmd not in allow:
                problems.append(
                    "entry %d ('%s'): stdio command %r is not allowed — "
                    "stdio MCP children must be a built-in editor "
                    "entrypoint or listed in NEX_STDIO_ALLOW (operator "
                    "environment; the HTTP API cannot extend the list)"
                    % (i, e.get("name"), cmd))
        else:
            url = str(e.get("url") or "")
            if url and not url.startswith("stdio://"):
                ok, why = http_url_allowed(url)
                if not ok:
                    problems.append("entry %d ('%s'): %s"
                                    % (i, e.get("name"), why))
    return problems


def reset_tunnels_for_testing() -> None:
    """Drop the cached registry. Only used by the test harness."""
    global _TUNNELS
    _TUNNELS = None


# ---------------------------------------------------------------------------
# HTTP MCP endpoint trust boundary (SSRF defense).
#
# An http tunnel tells the server "connect to this URL and speak MCP
# there". An unrestricted URL is a classic SSRF primitive: internal
# services, cloud metadata endpoints, other LAN machines — reachable
# with the server's privileges. The LLM can never choose a URL (only
# the operator configures tunnels), but even an operator mistake — or
# a poisoned tunnels.json — must fail closed.
#
# What IS allowed:
#   * loopback endpoints (127.0.0.1 / localhost / ::1, any port) — the
#     engine's local MCP servers, and
#   * hosts the operator explicitly allowlisted in NEX_HTTP_ALLOW
#     (comma-separated hostnames, e.g. "gamebox.lan,10.0.0.5").
# Everything else is rejected BEFORE registration / dropped on reload.
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def http_allowlist() -> List[str]:
    raw = os.environ.get("NEX_HTTP_ALLOW", "")
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def http_url_allowed(url: str) -> tuple:
    """(allowed, reason) for an http(s) MCP endpoint URL."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, "not an http(s) URL"
        host = (p.hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False, "unparseable URL"
    if not host:
        return False, "URL has no host"
    if host in _LOOPBACK_HOSTS or host in http_allowlist():
        return True, ""
    return False, (
        "http MCP endpoint %r is not allowed — only loopback "
        "(127.0.0.1/localhost) or hosts listed in NEX_HTTP_ALLOW "
        "(operator environment; the HTTP API cannot extend the list)"
        % url)
