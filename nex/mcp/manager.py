"""MCP server manager — the capability layer of Nex.

An MCP server is a *capability source*: the only way the agent can act
on the world. This module owns their lifecycle:

    add → connect → (monitor → reconnect) → inspect → disconnect → remove

Design rules
------------
* CONFIG is an operator act. Servers live in ``~/.nex/servers.json``
  (0600) plus ``NEX_SERVERS`` env entries. The model can never add,
  remove, or reconfigure a server — there is no path from model output
  to this module's config.
* CONNECTED != TRUSTED. A server marked ``trusted`` (the operator's
  explicit toggle when adding it, or NEX_TRUSTED_SERVERS) may serve
  autonomous tool calls in strict mode; an untrusted one is refused by
  the policy regardless of being connected.
* Every tool call goes through ``mcp.policy.authorize`` (name
  classification + argument scanning) before it is sent, and is
  recorded in the audit log after.
* Failures are status, not exceptions: a server that is down is
  reported as ``down`` with its last error, and the rest keep working.
* The health monitor probes configured servers on a slow cadence with
  capped backoff, and reconnects them when they come back — so a
  laptop that wakes up, or an app that restarts, heals without
  operator attention.

Nothing here knows what any server *does*. A Blender bridge, a game
server, and a weather API are the same object.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from mcp.audit import AuditLog
from mcp.registry import CapabilityRegistry
from mcp.capability import capability_for_tool
from mcp.policy import authorize, current_policy, Decision
from mcp.transport import Upstream, UpstreamError

# Statuses a server can be in.
ST_CONNECTED = "connected"
ST_CONNECTING = "connecting"
ST_DISCONNECTED = "disconnected"
ST_ERROR = "error"

# A stdio command must be ONE executable token; anything from
# this set means someone is smuggling shell syntax where a
# program name belongs.
_BAD_CMD_CHARS = set(" ;|&>$`\t\n'\"\\")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _nex_dir() -> str:
    base = os.environ.get("NEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".nex")
    os.makedirs(base, exist_ok=True)
    return base


def servers_config_path() -> str:
    return os.path.join(_nex_dir(), "servers.json")


def http_allowlist() -> List[str]:
    """Hosts the operator allows beyond loopback (NEX_HTTP_ALLOW)."""
    raw = os.environ.get("NEX_HTTP_ALLOW", "")
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def stdio_allowlist() -> List[str]:
    """When NEX_STDIO_ALLOW is set it is a STRICT command allowlist —
    a lockdown lever. Empty means: operator-added commands allowed."""
    raw = os.environ.get("NEX_STDIO_ALLOW", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


def validate_server_entry(entry: Any,
                          allow_remote: bool = False) -> List[str]:
    """Validate a proposed server config. Returns rejection reasons
    (empty list = accepted).

    ``allow_remote``: non-loopback HTTP endpoints require an explicit
    operator confirmation at the call site (the UI shows the risk);
    this function only enforces hard rules.
    """
    problems: List[str] = []
    if not isinstance(entry, dict):
        return ["server entry must be an object"]
    name = str(entry.get("name") or "")
    if not _NAME_RE.match(name):
        problems.append(
            "name %r must be 1-32 chars of a-z, 0-9, '-', '_' "
            "(start with a letter or digit)" % name)
    transport = entry.get("transport")
    if entry.get("command") and entry.get("url"):
        problems.append("provide either 'url' (http) or 'command' (stdio), "
                        "not both")
    elif transport == "stdio" or (entry.get("command")
                                  and not entry.get("url")):
        cmd = str(entry.get("command") or "")
        if not cmd:
            problems.append("stdio server needs a 'command'")
        elif _BAD_CMD_CHARS.intersection(cmd) or cmd.startswith("-"):
            # The command must be ONE executable token — arguments belong
            # in 'args' (a list, never passed through a shell). A command
            # with shell metacharacters is someone trying to smuggle one.
            problems.append(
                "command %r must be a single executable (put arguments in "
                "'args'; shell syntax is never executed)" % cmd)
        allow = stdio_allowlist()
        if allow and cmd not in allow:
            problems.append(
                "stdio command %r is not in the NEX_STDIO_ALLOW allowlist "
                "(lockdown mode is active)" % cmd)
        args = entry.get("args")
        if args is not None and not isinstance(args, list):
            problems.append("'args' must be a list")
        if isinstance(args, list):
            for a in args:
                if not isinstance(a, (str, int, float)):
                    problems.append("'args' must be strings/numbers")
                    break
    else:
        url = str(entry.get("url") or "")
        if not url:
            problems.append("server needs a 'url' (http) or a 'command' (stdio)")
        elif not url.startswith(("http://", "https://")):
            problems.append("url must start with http:// or https://")
        else:
            try:
                from urllib.parse import urlparse
                p = urlparse(url)
                host = (p.hostname or "").lower()
                if not host:
                    problems.append("url has no host")
                elif host not in _LOOPBACK_HOSTS and host not in http_allowlist():
                    if not allow_remote:
                        problems.append(
                            "endpoint %r is not loopback and not in "
                            "NEX_HTTP_ALLOW — connecting requires an "
                            "explicit confirmation" % url)
            except Exception:  # noqa: BLE001
                problems.append("unparseable url")
    to = entry.get("timeout_s")
    if to is not None:
        try:
            v = float(to)
            if not (1.0 <= v <= 3600.0):
                problems.append("timeout_s must be 1..3600")
        except (TypeError, ValueError):
            problems.append("timeout_s must be a number")
    return problems


def _entry_from_env_spec(spec: str) -> Optional[Dict[str, Any]]:
    """'name=url' from NEX_SERVERS / NEX_TUNNELS env."""
    if "=" not in spec:
        return None
    name, val = spec.split("=", 1)
    name = name.strip()
    val = val.strip()
    if not name or not val:
        return None
    return {"name": name, "url": val, "transport": "http",
            "trusted": True, "source": "env"}


def env_servers() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for var in ("NEX_SERVERS",):
        raw = (os.environ.get(var) or "").strip()
        if not raw:
            continue
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            e = _entry_from_env_spec(chunk)
            if e is not None:
                out.append(e)
    return out


class ServerManager:
    """Owns every MCP server Nex knows about."""

    def __init__(self, bus: Optional[Any] = None,
                 audit: Optional[AuditLog] = None) -> None:
        self._lock = threading.RLock()
        self._bus = bus
        self.audit = audit or AuditLog()
        self._config: List[Dict[str, Any]] = []
        self._live: Dict[str, Upstream] = {}
        self._status: Dict[str, Dict[str, Any]] = {}
        self._approvals: Dict[str, Set[str]] = {}   # session tool approvals
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._load()

    # ----- events -----------------------------------------------------------

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish({"type": event_type, "ts": time.time(),
                               **payload})
        except Exception:  # noqa: BLE001
            pass

    # ----- config persistence ------------------------------------------------

    def _load(self) -> None:
        cfg: List[Dict[str, Any]] = []
        path = servers_config_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                for e in raw:
                    if isinstance(e, dict) and validate_server_entry(e):
                        cfg.append(self._normalize(e))
        except (OSError, ValueError):
            pass
        for e in env_servers():
            if validate_server_entry(e, allow_remote=True):
                cfg.append(self._normalize(e))
        self._config = cfg
        for e in cfg:
            if e.get("enabled", True):
                self._status[e["name"]] = {
                    "status": ST_DISCONNECTED, "error": None}

    def _save(self) -> None:
        path = servers_config_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            pass

    @staticmethod
    def _normalize(e: Dict[str, Any]) -> Dict[str, Any]:
        out = {
            "name": str(e.get("name") or ""),
            "transport": "stdio" if (e.get("transport") == "stdio"
                                     or (e.get("command")
                                         and not e.get("url"))) else "http",
            "enabled": bool(e.get("enabled", True)),
            "trusted": bool(e.get("trusted", True)),
        }
        if out["transport"] == "stdio":
            out["command"] = str(e.get("command") or "")
            out["args"] = [str(a) for a in (e.get("args") or [])]
        else:
            out["url"] = str(e.get("url") or "")
        if e.get("timeout_s") is not None:
            out["timeout_s"] = e["timeout_s"]
        if e.get("source"):
            out["source"] = e["source"]
        return out

    # ----- lifecycle ---------------------------------------------------------

    def add(self, entry: Dict[str, Any],
            allow_remote: bool = False,
            connect: bool = True) -> Tuple[Optional[Dict[str, Any]], str]:
        """Add a server (operator act). Returns (entry, error)."""
        problems = validate_server_entry(entry, allow_remote=allow_remote)
        if problems:
            return None, "; ".join(problems)
        norm = self._normalize(entry)
        with self._lock:
            if any(c["name"] == norm["name"] for c in self._config):
                return None, "a server named '%s' already exists" % norm["name"]
            self._config.append(norm)
            self._save()
        if connect and norm.get("enabled", True):
            err = self.connect(norm["name"])
            if err:
                return norm, ""      # added, but connection failed (reported)
        return norm, ""

    def remove(self, name: str) -> Tuple[bool, str]:
        with self._lock:
            before = len(self._config)
            self._config = [c for c in self._config if c["name"] != name]
            if len(self._config) == before:
                return False, "no server named '%s'" % name
            self._save()
            up = self._live.pop(name, None)
            self._status.pop(name, None)
            self._approvals.pop(name, None)
        if up is not None:
            try:
                up.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._emit("mcp.removed", {"server": name})
        return True, ""

    def set_trusted(self, name: str, trusted: bool) -> Tuple[bool, str]:
        with self._lock:
            for c in self._config:
                if c["name"] == name:
                    c["trusted"] = bool(trusted)
                    self._save()
                    return True, ""
            return False, "no server named '%s'" % name

    def connect(self, name: str) -> Optional[str]:
        """Connect one server. Returns an error string or None."""
        entry = self._entry(name)
        if entry is None:
            return "no server named '%s'" % name
        with self._lock:
            self._status[name] = {"status": ST_CONNECTING, "error": None}
        self._emit("mcp.status", self.server_status(name))
        up = self._build_upstream(entry)
        try:
            up.connect()
            tools = up.tools()
        except UpstreamError as exc:
            with self._lock:
                old = self._live.pop(name, None)
                self._status[name] = {"status": ST_ERROR,
                                      "error": str(exc)[:300]}
            if old is not None:
                try:
                    old.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self._emit("mcp.status", self.server_status(name))
            return str(exc)
        with self._lock:
            self._live[name] = up
            self._status[name] = {"status": ST_CONNECTED, "error": None}
        self._emit("mcp.status", self.server_status(name))
        self._emit("mcp.tools", {"server": name,
                                 "tools": self._tool_summaries(name)})
        return None

    def disconnect(self, name: str) -> Tuple[bool, str]:
        with self._lock:
            up = self._live.pop(name, None)
            if up is None and not any(c["name"] == name
                                      for c in self._config):
                return False, "no server named '%s'" % name
            self._status[name] = {"status": ST_DISCONNECTED, "error": None}
        if up is not None:
            try:
                up.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._emit("mcp.status", self.server_status(name))
        return True, ""

    def reconnect(self, name: str) -> Optional[str]:
        self.disconnect(name)
        return self.connect(name)

    def connect_all(self) -> None:
        for e in list(self._config):
            if e.get("enabled", True):
                self.connect(e["name"])

    # ----- accessors -----------------------------------------------------------

    def _entry(self, name: str) -> Optional[Dict[str, Any]]:
        for c in self._config:
            if c["name"] == name:
                return c
        return None

    def _build_upstream(self, entry: Dict[str, Any]) -> Upstream:
        if entry["transport"] == "stdio":
            up = Upstream(entry["name"], "stdio://" + entry["name"],
                          entry.get("label") or entry["name"])
            up.stdio_command = (entry["command"], list(entry.get("args") or []))
        else:
            up = Upstream(entry["name"], entry["url"],
                          entry.get("label") or entry["name"])
        to = entry.get("timeout_s")
        if to is not None:
            try:
                v = float(to)
                if 1.0 <= v <= 3600.0:
                    up.call_timeout = v
            except (TypeError, ValueError):
                pass
        return up

    def upstream(self, name: str) -> Optional[Upstream]:
        with self._lock:
            return self._live.get(name)

    def config(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(c) for c in self._config]

    def server_status(self, name: str) -> Dict[str, Any]:
        entry = self._entry(name)
        if entry is None:
            return {"server": name, "status": ST_DISCONNECTED,
                    "error": "not configured"}
        with self._lock:
            st = dict(self._status.get(name)
                      or {"status": ST_DISCONNECTED, "error": None})
            up = self._live.get(name)
        out = {
            "server": name,
            "transport": entry["transport"],
            "url": entry.get("url"),
            "command": entry.get("command"),
            "args": entry.get("args"),
            "trusted": bool(entry.get("trusted", True)),
            "enabled": bool(entry.get("enabled", True)),
            "status": st.get("status", ST_DISCONNECTED),
            "error": st.get("error"),
            "tools_count": 0,
        }
        if up is not None:
            s = up.status()
            out["tools_count"] = s.get("tools_count", 0)
            out["server_info"] = s.get("server_info", {})
            out["protocol_version"] = s.get("protocol_version")
            out["latency_ms"] = s.get("latency_ms")
            out["last_error"] = s.get("last_error")
        return out

    def status(self) -> List[Dict[str, Any]]:
        return [self.server_status(c["name"]) for c in self.config()]

    def _tool_summaries(self, name: str) -> List[Dict[str, Any]]:
        up = self.upstream(name)
        if up is None:
            return []
        try:
            raw = up.tools() or []
        except UpstreamError:
            return []
        out = []
        for t in raw:
            cap = capability_for_tool(t)
            out.append({
                "name": t.get("name", ""),
                "description": (t.get("description") or "")[:400],
                "category": cap.category,
                "requires_confirmation": cap.requires_confirmation,
                "schema": t.get("inputSchema", {}) or {},
            })
        return out

    def tools(self, name: str) -> List[Dict[str, Any]]:
        return self._tool_summaries(name)

    def registry(self) -> CapabilityRegistry:
        """A live capability registry over the connected servers."""
        with self._lock:
            ups = list(self._live.values())
        try:
            return CapabilityRegistry.from_upstreams(ups)
        except Exception:  # noqa: BLE001
            return CapabilityRegistry([])

    def summary(self) -> Dict[str, Any]:
        st = self.status()
        connected = [s for s in st if s["status"] == ST_CONNECTED]
        return {
            "servers": st,
            "total": len(st),
            "connected": len(connected),
            "tools": sum(s.get("tools_count", 0) for s in connected),
        }

    def trusted_servers(self) -> set:
        return {c["name"] for c in self._config if c.get("trusted", True)}

    # ----- the action path ------------------------------------------------------

    def approve_tool(self, server: str, tool: str) -> None:
        """Record a user approval for one tool on one server
        (session-scoped: it dies with the process)."""
        with self._lock:
            self._approvals.setdefault(server, set()).add(tool)

    def is_tool_approved(self, server: str, tool: str) -> bool:
        with self._lock:
            return tool in self._approvals.get(server, set())

    def call(self, server: str, tool: str, args: Dict[str, Any],
             audit_context: Optional[Dict[str, Any]] = None,
             autonomous: bool = False) -> Dict[str, Any]:
        """THE action path. authorize → upstream.call → audit.

        Returns a dict. On success: the tool result under 'result'.
        On refusal: {'refused': reason, 'decision': {...}}.
        On failure: {'error': message}.

        ``autonomous`` marks a call that no human is watching in the
        moment (an agent run without a live approver). Untrusted servers
        are refused outright there; interactively their tools always
        pass through a confirmation.
        """
        up = self.upstream(server)
        if up is None:
            return {"error": "server '%s' is not connected" % server}
        entry = self._entry(server) or {}
        trusted = bool(entry.get("trusted", True))
        if not trusted:
            if autonomous:
                self.audit.record("refuse", server=server, tool=tool,
                                  ok=False,
                                  detail="untrusted server in autonomous "
                                         "run",
                                  context=audit_context)
                return {
                    "refused": "server '%s' is not trusted; it cannot be "
                               "used in autonomous runs (mark it trusted "
                               "after reviewing it)" % server,
                    "decision": {"category": "untrusted_server"},
                }
            # Interactive: never silent — every tool on an untrusted
            # server goes through the human.
            if not self.is_tool_approved(server, tool):
                self.audit.record(
                    "confirm_required", server=server, tool=tool, ok=False,
                    detail="untrusted server — confirmation required",
                    context=audit_context)
                return {
                    "needs_confirmation":
                        "server '%s' is untrusted; confirm this call" % server,
                    "decision": {"category": "untrusted_server"},
                }
        try:
            raw = up.tools()
        except UpstreamError as exc:
            return {"error": "server '%s' is unreachable: %s" % (server, exc)}
        tool_def = next((t for t in raw if t.get("name") == tool), None)
        if tool_def is None:
            return {"error": "server '%s' has no tool '%s'" % (server, tool)}
        cap = capability_for_tool(tool_def)
        decision = authorize(server, tool, cap, args=args)
        if not decision.allowed:
            self.audit.record("refuse", server=server, tool=tool,
                              ok=False, detail=decision.reason,
                              context=audit_context)
            return {"refused": decision.reason,
                    "decision": decision.to_dict()}
        if decision.requires_confirmation and \
                not self.is_tool_approved(server, tool):
            self.audit.record("confirm_required", server=server, tool=tool,
                              ok=False, detail=decision.reason,
                              context=audit_context)
            return {"needs_confirmation": decision.reason,
                    "decision": decision.to_dict()}
        t0 = time.monotonic()
        try:
            resp = up.call(tool, args or {})
        except UpstreamError as exc:
            self.audit.record("call", server=server, tool=tool, ok=False,
                              duration_ms=round((time.monotonic() - t0) * 1000),
                              detail=str(exc)[:300], context=audit_context)
            return {"error": str(exc)}
        result = resp.get("result") if isinstance(resp, dict) else resp
        self.audit.record("call", server=server, tool=tool, ok=True,
                          duration_ms=round((time.monotonic() - t0) * 1000),
                          context=audit_context)
        return {"result": result}

    def audit_entries(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.audit.recent(limit)

    def close(self) -> None:
        """Disconnect every live upstream (process exit / tests)."""
        self.stop_monitor()
        with self._lock:
            ups = list(self._live.values())
            self._live.clear()
        for up in ups:
            try:
                up.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ----- health monitor ---------------------------------------------------

    def start_monitor(self, interval_s: float = 20.0) -> None:
        if self._monitor_thread is not None:
            return
        self._monitor_stop.clear()

        def _run() -> None:
            backoff = 1
            while not self._monitor_stop.wait(
                    interval_s * min(backoff, 6)):
                try:
                    changed = self._probe_all()
                    backoff = 1 if changed else backoff + 1
                except Exception:  # noqa: BLE001
                    backoff += 1
        self._monitor_thread = threading.Thread(
            target=_run, daemon=True, name="nex-mcp-monitor")
        self._monitor_thread.start()

    def stop_monitor(self) -> None:
        self._monitor_stop.set()
        self._monitor_thread = None

    def _probe_all(self) -> bool:
        """Probe every enabled server; reconnect the ones that went away
        and came back. Returns True if any status changed."""
        changed = False
        for e in list(self._config):
            if not e.get("enabled", True):
                continue
            name = e["name"]
            before = self.server_status(name).get("status")
            up = self.upstream(name)
            if up is not None:
                # Connected: light health check via cached tools refresh.
                try:
                    up.tools()
                    after = ST_CONNECTED
                    err = None
                except UpstreamError as exc:
                    after = ST_ERROR
                    err = str(exc)[:300]
                with self._lock:
                    prev = self._status.get(name) or {}
                    if after == ST_ERROR and prev.get("status") != ST_ERROR:
                        changed = True
                    if after == ST_CONNECTED and prev.get("status") != ST_CONNECTED:
                        changed = True
                    self._status[name] = {"status": after, "error": err}
            else:
                # Not connected: try to (re)connect.
                err = self.connect(name)
                if err is None:
                    changed = True
            if changed:
                self._emit("mcp.status", self.server_status(name))
        return changed


_MANAGER: Optional[ServerManager] = None


def get_manager(bus: Any = None) -> ServerManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ServerManager(bus=bus)
    return _MANAGER


def reset_manager_for_testing() -> None:
    global _MANAGER
    if _MANAGER is not None:
        _MANAGER.stop_monitor()
    _MANAGER = None
