"""Security policy + MCP-only enforcement (STAGE 4 / STAGE 18).

The policy is the architectural security boundary. It is NOT a prompt
instruction — the planner/agent calls ``authorize()`` BEFORE every
external action, and a denial is a hard stop, not a suggestion.

Trust model
  * SERVER trust is the operator's explicit act of CONNECTING a tunnel
    (token-protected settings page / built-in catalog / env). There is no
    other way for a server to enter the capability surface, so an
    allow-list of servers would only re-state that act; the narrowing
    happens at the TOOL level instead.
  * TOOL trust is the policy's job. A connected server may expose many
    tools; Nex must not trust what it cannot classify:
      - tools whose name heuristics cannot place (UNKNOWN) require
        CONFIRMATION by default (Policy.confirm_unknown) instead of
        silently passing, and
      - any external tool named after a process-execution primitive
        (PROCESS_EXECUTION) ALWAYS requires confirmation, and
      - destructive / network categories require confirmation.
    Explicit server/tool allow-lists (Policy fields) tighten further.
  * ``run_command`` is Nex's OWN shell and is NEVER authorized, on any
    server, under any configuration.
  * MCP annotations (readOnlyHint & co) are untrusted hints supplied by
    the server itself — see capability.py; they can only raise caution.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from mcp.capability import (
    READ, CREATE, MODIFY, BUILD, TEST, CODE_EXECUTION, DESTRUCTIVE, NETWORK,
    UNKNOWN, ToolCapability, apply_capability_registry, tokenize,
)


# THE CAPABILITY BOUNDARY. Nex's AI can act ONLY through explicitly
# connected MCP servers (authorized via `server`). These internal names
# are MCP-protocol introspection (part of the MCP layer itself).
# EVERYTHING else internal — filesystem, shell, host scanning,
# compile/validate helpers — is NOT an AI capability, even though the
# code exists as Nex infrastructure. Not policy-gated: structurally
# absent from the boundary.
INTERNAL_ALLOWED = frozenset({
    "who_am_i", "list_platforms", "tunnel_status", "tunnel_probe",
})

# Tools that are ALWAYS treated as external/unsafe (never auto-allowed).
# NOTE: matched against the BARE tool name — a connected MCP server that
# exposes a tool literally named `run_command` is denied too.
ALWAYS_DENIED = frozenset({"run_command"})

# Process-execution tool names on EXTERNAL servers. A connected engine
# may legitimately expose console/command tools; any such tool is a
# high-risk PROCESS capability and ALWAYS requires confirmation.
# (run_command itself is Nex's internal shell and is always DENIED above
# — the two rules are deliberately separate so the policy has one
# unambiguous statement of truth per name.)
PROCESS_EXECUTION = frozenset({"execute_command", "exec"})


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

_SENSITIVE_PATHS = (
    ("/etc/", "system configuration"),
    ("/proc/", "kernel interfaces"),
    ("/sys/", "kernel interfaces"),
    ("/root/", "root's home"),
    ("/dev/", "device files"),
    ("/var/lib/", "system state"),
    (".ssh/", "SSH keys"),
    ("id_rsa", "SSH private key"),
    ("id_ed25519", "SSH private key"),
    ("authorized_keys", "SSH access control"),
    (".aws/", "cloud credentials"),
    (".gnupg/", "PGP keys"),
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

_MAX_SCAN = 64 * 1024


def _flatten(args: Any, prefix: str = "", out: Optional[list] = None) -> list:
    """(key, string-value) pairs from arbitrarily nested arguments."""
    if out is None:
        out = []
    if isinstance(args, dict):
        for k, v in args.items():
            _flatten(v, ("%s.%s" % (prefix, k)).strip("."), out)
    elif isinstance(args, (list, tuple)):
        for i, v in enumerate(args):
            _flatten(v, "%s[%d]" % (prefix, i), out)
    elif isinstance(args, str):
        out.append((prefix, args))
    return out


def scan_file_payload(path: str, workspace_root: str = "") -> Optional[str]:
    """Scan a file the caller is about to EXECUTE. None = clean.

    Closes the laundering hole: writing the payload into a file and then
    running `run_python(file=...)` would slip past an argument scan, so
    the referenced file is read (bounded) and checked with the same
    marker rules. Files outside the project are refused outright.
    """
    if not path:
        return None
    raw = str(path)
    root = os.path.realpath(workspace_root or os.getcwd())
    target = raw if os.path.isabs(raw) else os.path.join(root, raw)
    real = os.path.realpath(target)
    if not (real == root or real.startswith(root + os.sep)):
        return ("escape payload: %r is outside the project directory — "
                "Nex only executes code it built here" % raw)
    try:
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(_MAX_SCAN)
    except OSError:
        return None          # not a readable file: nothing to launder
    low = text.lower()
    for marker, why in _ESCAPE_MARKERS:
        if marker in low:
            return ("escape payload: the file %r contains %r (%s) — code "
                    "that reaches the operating system is never part of "
                    "building a game" % (raw, marker, why))
    return None


def code_file_argument(args: Any) -> str:
    """The file path a call is about to execute, if it names one."""
    for key, val in _flatten(args or {}):
        leaf = key.split(".")[-1].lower()
        if leaf not in _PATH_KEYS and leaf not in ("script", "code_file",
                                                   "script_file"):
            continue
        if val and not val.strip().startswith(("{", "(", "local ", "import ",
                                               "def ", "class ", "//", "#")):
            return val.strip()
    return ""


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
    code_like = bool(capability and capability.category == CODE_EXECUTION)         or (tool or "").lower() in PROCESS_EXECUTION
    for key, val in pairs:
        if budget <= 0:
            break
        text = val[:budget]
        budget -= len(text)
        low = text.lower()
        if code_like:
            for marker, why in _ESCAPE_MARKERS:
                if marker in low:
                    return ("escape payload: argument '%s' contains %r "
                            "(%s) — code that reaches the operating system "
                            "is never part of building a game"
                            % (key or "?", marker, why))
        for marker, why in _SENSITIVE_PATHS:
            if marker in low:
                return ("escape payload: argument '%s' touches %r (%s) — "
                        "outside the project, not an AI capability"
                        % (key or "?", marker, why))
        if key.split(".")[-1].lower() in _PATH_KEYS and ("../" in text
                                                         or "..\\" in text):
            return ("escape payload: argument '%s' leaves the project "
                    "directory (%r)" % (key or "?", text[:60]))
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
    # CONNECTED != TRUSTED. When strict_servers is enabled (the deployed
    # server enables it; see server._build_policy), an external tool call
    # is allowed only if its server is in `trusted_servers`. The registry
    # is built by the composition root from the built-in catalog + the
    # operator's NEX_TRUSTED_SERVERS — never by the model. strict_servers
    # with trusted_servers=None fails CLOSED (nothing external passes):
    # a missing registry is a configuration error, not a permission.
    strict_servers: bool = False
    trusted_servers: Optional[Set[str]] = None
    # --- CODE EXECUTION standing approval (operator act) -------------------
    # Code that runs on an engine (execute_luau, run_python, run_script, a
    # terminal) executes with the ENGINE's privileges. Nex therefore asks
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
              policy: Optional[Policy] = None,
              args: Any = None) -> Decision:
    """Authorize a single tool call. Returns a Decision.

    `server` is None for Nex-internal tools, or the upstream name for an
    MCP-exposed tool. `capability` carries the classification. `args` are
    the call's arguments: they are scanned for escape payloads (OS
    primitives, credentials, paths leaving the project) — the content half
    of the boundary. Passing no args keeps the old name-only behavior.
    """
    pol = policy or _CURRENT
    cap = capability or ToolCapability()
    cat = cap.category

    # 1) Always-denied tools (shell, etc.) — bare tool name, so an
    # external server exposing a tool named `run_command` is denied too.
    if tool in ALWAYS_DENIED:
        return Decision(False, False,
                        "tool '%s' is never authorized" % tool, cat)

    # 1b) Process-execution tool names on an EXTERNAL server: high-risk
    # PROCESS capability. Never silently allowed: it needs the operator's
    # standing approval (tool pin or NEX_ALLOW_CODE_EXECUTION) — and its
    # ARGUMENTS are scanned below either way, so an approval can never
    # turn into "run this shell command" for free.
    # (Internal tools with these names fall through to the boundary below
    # and are denied.)

    # 2) Internal Nex runtime tool — ONLY the MCP introspection set.
    # Everything else internal (filesystem/shell/host tools) is not an AI
    # capability, regardless of flags.
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
        cap.destructive
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
