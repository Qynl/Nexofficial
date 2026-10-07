"""Security policy + MCP-only enforcement (STAGE 4 / STAGE 18).

The policy is the architectural security boundary. It is NOT a prompt
instruction — the planner/agent calls ``authorize()`` BEFORE every
external action, and a denial is a hard stop, not a suggestion.

Trust model
  * Adding a server permits discovery only; UI-added servers default to
    UNTRUSTED. ServerManager requires exact-call approval for interactive
    use and refuses untrusted servers in unattended runs.
  * TOOL trust is the policy's job. A connected server may expose many
    tools; Nex must not trust what it cannot classify:
      - tools whose name heuristics cannot place (UNKNOWN) require
        CONFIRMATION by default (Policy.confirm_unknown) instead of
        silently passing, and
      - generic shell/process/terminal tools are categorically denied,
      - language/editor code execution always needs explicit approval, and
      - destructive / network categories require confirmation.
    Explicit server/tool allow-lists (Policy fields) tighten further.
  * ``run_command`` and friends are shell primitives and are NEVER
    authorized, on any server, under any configuration.
  * MCP annotations (readOnlyHint & co) are untrusted hints supplied by
    the server itself — see capability.py; they can only raise caution.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from mcp.capability import (
    CODE_EXECUTION, DESTRUCTIVE, NETWORK,
    UNKNOWN, ToolCapability, apply_capability_registry, capability_for_tool,
    max_capability, tokenize,
)


# THE CAPABILITY BOUNDARY. Nex's AI can act ONLY through explicitly
# connected MCP servers (authorized via `server`). Nex has NO internal
# AI capabilities at all: no filesystem, no shell, no host access —
# not policy-gated, structurally absent from the boundary. A call with
# server=None (an attempt to reach "Nex's own tools") is denied here.
INTERNAL_ALLOWED: frozenset = frozenset()

# Shell/process control is not an agent capability, even when a connected
# server advertises it.  Language/editor tools such as execute_luau and
# run_script remain CODE_EXECUTION (confirmation-gated); generic command,
# terminal and process launchers are categorically refused.
ALWAYS_DENIED = frozenset({
    "run_command", "execute_command", "spawn_shell", "open_terminal",
    "terminal", "shell", "bash", "zsh", "powershell", "cmd", "cmd.exe",
    "subprocess", "popen", "exec",
})
PROCESS_EXECUTION = frozenset()  # compatibility name; shell forms are denied


def _forbidden_tool_name(tool: str) -> bool:
    raw = (tool or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if raw in ALWAYS_DENIED or normalized in ALWAYS_DENIED:
        return True
    toks = set(tokenize(raw))
    if toks & {"terminal", "shell", "bash", "zsh", "powershell", "pwsh",
               "subprocess", "popen", "pty"}:
        return True
    # command/process becomes forbidden only beside an execution verb;
    # list_commands and inspect_process remain ordinary metadata tools.
    return bool(toks & {"command", "commands", "cmd", "process"} and
                toks & {"run", "execute", "exec", "spawn", "start", "open"})


# THE BOUNDARY IS NOT CONFIGURABLE. There is no flag, env var, or runtime
# call that turns it off. (If a developer ever wants an unrestricted
# playground, that must be a separate, explicitly non-autonomous dev
# server — never a switch on the production agent.)
MCP_ONLY = True


# ---------------------------------------------------------------------------
# ARGUMENT SCANNING (the CONTENT half of the boundary)
# ---------------------------------------------------------------------------
# Name classification decides whether a tool MAY run; this decides whether
# a specific CALL is allowed to. Two deterministic rule sets, no model, no
# heuristics:
#
#   1. ESCAPE MARKERS — OS/process primitives inside an argument. Game
#      code has no business calling os.execute/io.popen/subprocess, and a
#      prompt-injected instruction ("the log says: run rm -rf ...") has to
#      get through here to become a shell command. It cannot.
#   2. SENSITIVE PATHS — credentials and system directories, checked on
#      EVERY tool, because writing into ~/.ssh is an escape no matter what
#      the tool is called.
#
# Both are refusals, not confirmations: there is no legitimate autonomous
# workflow behind "read the operator's SSH key".
_ESCAPE_MARKERS = (
    ("os.execute", "shell execution from game code"),
    ("os.popen", "shell execution from game code"),
    ("io.popen", "shell execution from game code"),
    ("os.system", "shell execution from game code"),
    ("subprocess", "process spawn from game code"),
    ("popen(", "process spawn from game code"),
    ("pty.spawn", "pseudo-terminal spawn"),
    ("package.loadlib", "native library load"),
    ("loadstring", "dynamic code evaluation"),
    ("loadfile(", "loads a file from disk as code"),
    ("dofile(", "loads a file from disk as code"),
    ("__import__", "module import from a string"),
    ("importlib", "module import from a string"),
    ("eval(", "dynamic code evaluation"),
    ("/bin/sh", "POSIX shell"),
    ("/bin/bash", "POSIX shell"),
    ("bash -c", "POSIX shell"),
    ("sh -c", "POSIX shell"),
    ("powershell -", "Windows shell"),
    ("cmd.exe /c", "Windows shell"),
    ("cmd /c", "Windows shell"),
    ("/etc/passwd", "system credential file"),
    ("curl ", "network fetch from a code payload"),
    ("wget ", "network fetch from a code payload"),
    ("base64 -d", "obfuscated payload decode"),
    ("chmod +x", "making a payload executable"),
    ("sudo ", "privilege escalation"),
)

# Credential SHAPES (value patterns, not the bare word "password"):
# an agent handing a secret to an external tool is leaking it, whether
# the tool runs code or "just" sends a message.
_CREDENTIAL_SHAPES = (
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "API key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."), "JWT"),
    (re.compile(r"(?i)(?<![a-z0-9])(api[_-]?key|secret(?:[_-]?access"
                r"[_-]?key)?|password|passwd|access[_-]?token)"
                r"[_-]*\s*[=:]\s*\S{12,}"), "credential assignment"),
    (re.compile(r"(?i)\bbearer\s+\S{15,}"), "bearer credential"),
)

_SENSITIVE_PATHS = (
    ("/etc/", "system configuration"),
    ("/proc/", "kernel interfaces"),
    ("/sys/", "kernel interfaces"),
    ("/root/", "root's home"),
    ("/dev/", "device files"),
    ("/var/lib/", "system state"),
    (".ssh/", "SSH keys"),
    (".ssh\\", "SSH keys (Windows path form)"),
    ("id_rsa", "SSH private key"),
    ("id_ed25519", "SSH private key"),
    ("authorized_keys", "SSH access control"),
    (".aws/", "cloud credentials"),
    (".aws\\", "cloud credentials (Windows path form)"),
    (".gnupg/", "PGP keys"),
    (".gnupg\\", "PGP keys (Windows path form)"),
    (".netrc", "stored credentials"),
    (".bashrc", "shell profile"),
    (".zshrc", "shell profile"),
    (".nex/", "NEX's own configuration and token"),
    (".nex\\", "NEX's own configuration and token (Windows path form)"),
    ("server_token", "NEX's own authentication token"),
    ("nex_token", "NEX's own authentication token"),
    ("/windows/system32", "operating system"),
    ("\\appdata\\", "operating system profile"),
    ("%appdata%", "operating system profile"),
    ("%userprofile%", "operating system profile"),
)

# Path-like argument KEYS: `..` traversal is only judged here, because a
# traversal in a code payload is a normal relative require, while a
# traversal in a path argument leaves the project.
_PATH_KEYS = ("path", "file", "filepath", "filename", "dir", "directory",
              "folder", "target", "destination", "dest", "source", "output",
              "input", "asset_path", "script_path", "project_path")

# Confirmation UI can show this entire bounded payload for meaningful human
# review; larger calls fail closed instead of hiding a dangerous suffix.
_MAX_SCAN = 16 * 1024


def _flatten(args: Any) -> list:
    """Bounded ``(key, string-value)`` pairs from nested JSON arguments.

    Model-generated JSON is normally acyclic, but callers are Python code;
    depth/node limits make the policy total even for hostile in-process data.
    A synthetic ``__scan_error__`` pair makes overflow fail closed.
    """
    out = []
    stack = [("", args, 0)]
    seen = set()
    nodes = 0
    while stack:
        prefix, value, depth = stack.pop()
        nodes += 1
        if nodes > 10000 or depth > 40:
            out.append(("__scan_error__", "argument structure is too complex"))
            break
        if isinstance(value, (dict, list, tuple)):
            ident = id(value)
            if ident in seen:
                out.append(("__scan_error__", "argument structure is cyclic"))
                break
            seen.add(ident)
        if isinstance(value, dict):
            for k, child in reversed(list(value.items())):
                stack.append((("%s.%s" % (prefix, k)).strip("."),
                              child, depth + 1))
        elif isinstance(value, (list, tuple)):
            for i in range(len(value) - 1, -1, -1):
                stack.append(("%s[%d]" % (prefix, i), value[i], depth + 1))
        elif isinstance(value, str):
            out.append((prefix, value))
    return out


def scan_arguments(tool: str, args: Any,
                   capability: Optional[ToolCapability] = None) -> Optional[str]:
    """Refuse a call whose ARGUMENTS carry an escape. None = clean.

    Deterministic: substring rules over the argument text, applied to
    every string value. Marker rules apply to tool calls that run code
    (CODE_EXECUTION / process names) and to every tool for the sensitive
    path rules — writing into ~/.ssh is an escape regardless of the tool.
    """
    pairs = _flatten(args or {})
    if not pairs:
        return None
    budget = _MAX_SCAN
    code_like = bool(capability and capability.category == CODE_EXECUTION) \
        or (tool or "").lower() in PROCESS_EXECUTION
    for key, val in pairs:
        if key == "__scan_error__":
            return "escape payload: arguments could not be safely inspected (%s)" % val
        if len(val) > budget:
            return ("escape payload: arguments exceed the %d-character "
                    "inspection limit; refusing rather than scanning only a prefix"
                    % _MAX_SCAN)
        budget -= len(val)
        text = unicodedata.normalize("NFKC", val).replace("\x00", "")
        # Decode common URL-encoded traversal/secret forms twice.  This does
        # not attempt to execute or interpret content; it merely prevents
        # encoding from bypassing deterministic substring rules.
        decoded = text
        for _ in range(2):
            newer = unquote(decoded)
            if newer == decoded:
                break
            decoded = newer
        low = decoded.lower()
        compact = re.sub(r"\s+", " ", low)
        if code_like:
            for marker, why in _ESCAPE_MARKERS:
                if marker in compact:
                    return ("escape payload: argument '%s' contains %r "
                            "(%s) — code that reaches the operating system "
                            "is never part of the task"
                            % (key or "?", marker, why))
        for marker, why in _SENSITIVE_PATHS:
            if marker in compact:
                return ("escape payload: argument '%s' touches %r (%s) — "
                        "outside the project, not an AI capability"
                        % (key or "?", marker, why))
        for pattern, what in _CREDENTIAL_SHAPES:
            if pattern.search(decoded):
                return ("escape payload: argument '%s' carries a %s — "
                        "secrets are never the agent's to pass along"
                        % (key or "?", what))
        key_parts = {p.lower() for p in re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                                                    key)}
        if key_parts.intersection(_PATH_KEYS) and \
                ("../" in compact or "..\\" in compact):
            return ("escape payload: argument '%s' leaves the project "
                    "directory (%r)" % (key or "?", decoded[:60]))
    return None


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
    # CONNECTED != TRUSTED. ServerManager enforces per-server interactive
    # trust. ``strict_servers`` is an additional embedding/lockdown mode:
    # an external tool call is allowed only if its server is in
    # `trusted_servers`. The registry
    # is built by the composition root from the built-in catalog + the
    # operator's NEX_TRUSTED_SERVERS — never by the model. strict_servers
    # with trusted_servers=None fails CLOSED (nothing external passes):
    # a missing registry is a configuration error, not a permission.
    strict_servers: bool = False
    trusted_servers: Optional[Set[str]] = None
    # --- CODE EXECUTION standing approval (operator act) -------------------
    # Code that runs on an engine (execute_luau, run_python, run_script)
    # executes with the ENGINE's privileges. Generic terminals are denied;
    # language execution therefore asks
    # before running it — and in an autonomous run "asking" means stopping
    # (see agent/loop.py). An operator who has read that risk may pre-
    # approve specific servers here (NEX_ALLOW_CODE_EXECUTION=a,b) or a
    # single tool in the capability file ({"approved": true}). Nothing the
    # model says can add to this set.
    allow_code_execution: Optional[Set[str]] = None
    # A coarser lever for the remaining confirmation cases (destructive /
    # network / unknown names) on servers the operator has decided to run
    # unattended: NEX_ALLOW_CONFIRMATIONS=a,b. It never covers CODE
    # EXECUTION — that needs the more specific statement above — and it
    # never covers an escape payload (the content scan is not configurable).
    allow_confirmations: Optional[Set[str]] = None


@dataclass
class Decision:
    allowed: bool
    requires_confirmation: bool
    reason: str
    category: str = UNKNOWN
    # When the call went through a generic dispatcher (Unreal MCP's
    # `call_tool`), this is the tool actually being invoked. Approvals must
    # bind to it, never to the dispatcher.
    effective_tool: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "allowed": self.allowed,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
            "category": self.category,
        }
        if self.effective_tool:
            out["effective_tool"] = self.effective_tool
        return out


# ---------------------------------------------------------------------------
# Generic tool dispatchers
#
# Unreal Engine 5.8's official MCP plugin defaults to "Enable Tool Search",
# where tools/list returns three meta-tools — list_toolsets,
# describe_toolset, and call_tool — instead of the real schemas. Every real
# action then arrives as call_tool{name: "...", arguments: {...}}.
#
# A dispatcher is a capability-confusion hazard: classifying the wrapper
# tells you nothing about the wrapped action, the shell denylist never sees
# the real name, and one "always allow" on the wrapper would silently cover
# every tool the server can reach. Nex therefore looks THROUGH a dispatcher
# and decides on the inner tool.
# ---------------------------------------------------------------------------
_DISPATCHER_NAMES = frozenset({
    "call_tool", "calltool", "invoke_tool", "run_tool", "execute_tool",
    "dispatch_tool", "tool_call", "use_tool",
})
# Argument keys that may carry the inner tool's name.
_DISPATCH_NAME_KEYS = ("name", "tool", "tool_name", "toolname", "method")


def is_dispatcher_tool(tool: str, schema: Any = None) -> bool:
    """Does this tool invoke ANOTHER tool named in its arguments?"""
    normalized = re.sub(r"[^a-z0-9]+", "_", (tool or "").strip().lower())
    if normalized.strip("_") in _DISPATCHER_NAMES:
        return True
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    if not isinstance(props, dict):
        return False
    keys = {str(k).lower() for k in props}
    names_a_tool = bool(keys & set(_DISPATCH_NAME_KEYS))
    carries_args = bool(keys & {"arguments", "args", "params",
                                "parameters", "input"})
    return names_a_tool and carries_args


def dispatched_tool_name(args: Any) -> Optional[str]:
    """The inner tool name carried by a dispatcher call, if any."""
    if not isinstance(args, dict):
        return None
    for key in _DISPATCH_NAME_KEYS:
        for actual in args:
            if str(actual).lower() != key:
                continue
            value = args[actual]
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


# Module-global policy (allow-lists only — the boundary is not in here).
_CURRENT: Policy = Policy()


def current_policy() -> Policy:
    return _CURRENT


def set_policy(p: Policy) -> None:
    global _CURRENT
    _CURRENT = p


def authorize(server: Optional[str], tool: str,
              capability: Optional[ToolCapability] = None,
              policy: Optional[Policy] = None,
              args: Any = None,
              schema: Any = None) -> Decision:
    """Authorize a single tool call. Returns a Decision.

    `server` is None for Nex-internal tools, or the upstream name for an
    MCP-exposed tool. `capability` carries the classification. `args` are
    the call's arguments: they are scanned for escape payloads (OS
    primitives, credentials, paths leaving the project) — the content half
    of the boundary. Passing no args keeps the old name-only behavior.

    A generic dispatcher (Unreal MCP's `call_tool`) is resolved to the tool
    it actually invokes BEFORE any other rule runs, so the denylist, the
    classification and the approval all bind to the real action.
    """
    if is_dispatcher_tool(tool, schema):
        cap = capability or ToolCapability()
        inner = dispatched_tool_name(args)
        if inner is None:
            return Decision(
                False, False,
                "dispatcher '%s' was called without naming the tool it "
                "invokes — Nex will not authorize an unidentified action"
                % tool, cap.category)
        if is_dispatcher_tool(inner):
            return Decision(False, False,
                            "dispatcher '%s' may not invoke another "
                            "dispatcher ('%s')" % (tool, inner),
                            cap.category)
        inner_cap = capability_for_tool({"name": inner})
        # A wrapper classified UNKNOWN carries no information — it is
        # unknown *because* it is a wrapper. Once the real tool is known,
        # that classification governs. Any other wrapper category is
        # merged severity-max so it can still escalate, never soften.
        effective_cap = (inner_cap if cap.category == UNKNOWN
                         else max_capability(cap, inner_cap))
        decision = authorize(server, inner, effective_cap, policy,
                             args=(args or {}).get("arguments")
                             if isinstance(args, dict) else None)
        reason = decision.reason
        if decision.allowed:
            reason = "%s (dispatched through %s)" % (reason, tool)
        else:
            reason = "%s%s" % (reason, "" if "dispatcher" in reason
                               else " (via dispatcher %s)" % tool)
        return Decision(decision.allowed, decision.requires_confirmation,
                        reason, decision.category, effective_tool=inner)

    pol = policy or _CURRENT
    cap = capability or ToolCapability()
    cat = cap.category

    # 1) Always-denied tools (shell, etc.) — bare tool name, so an
    # external server exposing a tool named `run_command` is denied too.
    # Checked AFTER dispatcher resolution so call_tool{run_command} is
    # denied exactly like a direct run_command.
    if _forbidden_tool_name(tool):
        return Decision(False, False,
                        "shell/process tool '%s' is never authorized"
                        % tool, cat)

    # 2) Internal Nex runtime tool — there is no model-visible set.
    # Filesystem/shell/host infrastructure is not an AI capability,
    # regardless of flags.
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

    # Content scan: the ARGUMENTS must be clean. This runs after the trust
    # checks (an untrusted server is refused for being untrusted) and
    # before any confirmation logic — a payload that reaches the OS is
    # refused outright, never offered for approval.
    refusal = scan_arguments(tool, args, cap)
    if refusal:
        return Decision(False, False, refusal, cat)

    # Code execution / process tools: allowed only with a standing
    # operator approval, otherwise it is a human decision.
    if cat == CODE_EXECUTION or tool in PROCESS_EXECUTION:
        # A process primitive is code execution by definition; report the
        # real category even when the caller passed no classification.
        cat = CODE_EXECUTION
        if not cap.requires_confirmation:
            return Decision(True, False,
                            "code execution is covered by the operator's "
                            "per-tool standing approval", cat)
        if pol.allow_code_execution is None or \
                server not in pol.allow_code_execution:
            return Decision(
                True, True,
                "code execution runs arbitrary code with the engine's "
                "privileges (%s): confirmation required — an autonomous "
                "run stops here. Pre-approve deliberately with "
                "NEX_ALLOW_CODE_EXECUTION=%s, or pin this one tool with "
                "{\"approved\": true} in the capability file"
                % (cat, server or "?"), cat)
        return Decision(True, False,
                        "operator pre-approved code execution for '%s' "
                        "(NEX_ALLOW_CODE_EXECUTION)" % server, cat)

    # MCP-only mode: external actions are exactly what MCP is for, so they
    # are permitted (subject to confirmation). Non-MCP external paths are
    # already blocked because they have no server. So we just continue.
    requires_confirm = (
        # A registry pin can explicitly escalate even a normally-safe
        # category. Baseline UNKNOWN confirmation remains controlled by
        # confirm_unknown below.
        (cap.requires_confirmation and "+registry" in cap.source)
        or cap.destructive
        or cat in pol.require_confirm_categories
        # Untrusted-by-default: an unclassifiable external tool is not
        # "safe, nobody looked"; it gets a human confirmation.
        or (cat == UNKNOWN and pol.confirm_unknown)
    )
    if requires_confirm and pol.allow_confirmations is not None \
            and server in pol.allow_confirmations:
        return Decision(True, False,
                        "external MCP tool (%s: covered by the operator's "
                        "standing approval for '%s', "
                        "NEX_ALLOW_CONFIRMATIONS)" % (cat, server), cat)
    return Decision(True, requires_confirm,
                    "external MCP tool", cat)
