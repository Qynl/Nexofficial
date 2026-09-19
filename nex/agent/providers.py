"""Provider layer — who PLANS, who BUILDS, and what happens when one is down.

The architecture this module implements:

        USER
         |
         v
    [ PLANNER ]  reasoning / architecture / decomposition   (rare, big calls)
         |
    exact build plan
         |
         v
    [ BUILDER ]  executes the plan through MCP tools         (frequent calls)
         |
      MCP only  ->  Roblox MCP / Unreal MCP

Roles are separate on purpose:

  * PLANNER  (default: the local model, or GPT when an OpenAI key is present)
    is called a handful of times per run: design document, systems, tests.
  * BUILDER  (default: NVIDIA NIM) is called during the build loop —
    failure diagnosis, repair decisions — and nothing else.

Why a router instead of one model call: NIM's free tier is ~40 requests per
minute with *undocumented, per-model* limits and no usage endpoint (NVIDIA
removed credits and never published a quota API). So the budget has to live
client-side and the failover has to be explicit:

    NIM 429 / timeout / 5xx / RPM exhausted
        -> the SAME call is retried on the fallback provider (GPT, then local)
        -> the build plan is untouched: only the hands change, not the plan
        -> after cooldown the router hands back to NIM automatically

Nothing here executes a tool. A provider returns TEXT; the agent loop turns
that text into validated MCP calls. The builder therefore cannot reach the
machine — the only action surface remains the MCP registry.

Configuration precedence (highest first):
    1. explicit settings push (POST /api/settings/providers)
    2. process environment
    3. .env files  (nex/.env, repo-root .env, ~/.nex/.env)
    4. ~/.nex/providers.json
    5. built-in defaults (see DEFAULT_PROVIDERS)

stdlib only. No subprocess, no shell, no eval — this module makes HTTP calls
and nothing else.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

ROLE_PLANNER = "planner"
ROLE_BUILDER = "builder"
ROLES = (ROLE_PLANNER, ROLE_BUILDER)

# EXPLICIT fallback lists. Nothing enters a chain implicitly: a provider has
# to be named here (or by the operator) to be tried, so a future provider
# added to the catalog can never silently become a fallback for a role.
ROLE_DEFAULT_FALLBACKS: Dict[str, List[str]] = {
    ROLE_BUILDER: ["gpt", "local"],
    ROLE_PLANNER: ["nim", "local"],
}

KIND_OLLAMA = "ollama"    # /api/chat, /api/tags
KIND_OPENAI = "openai"    # /v1/chat/completions, /v1/models (NIM, OpenAI, …)

# Provider states surfaced to the UI.
ST_AVAILABLE = "available"
ST_RATE_LIMITED = "rate_limited"
ST_COOLING = "cooling"
ST_ERROR = "error"
ST_NO_KEY = "no_key"
ST_DISABLED = "disabled"

# Error kinds (used for state transitions + event payloads).
ERR_RATE_LIMIT = "rate_limit"
ERR_TIMEOUT = "timeout"
ERR_SERVER = "server_error"
ERR_AUTH = "auth_error"
ERR_BAD_REQUEST = "bad_request"
ERR_NETWORK = "network_error"
ERR_PARSE = "bad_response"
ERR_CONFIG = "config_error"

# Which errors are worth retrying on another provider (everything except a
# malformed request that would fail identically everywhere… and even that one
# we hand over, because providers differ in what they accept).
FAILOVER_KINDS = (ERR_RATE_LIMIT, ERR_TIMEOUT, ERR_SERVER, ERR_AUTH,
                  ERR_NETWORK, ERR_PARSE, ERR_BAD_REQUEST)

NEX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".nex",
                                     "providers.json")

# ---------------------------------------------------------------------------
# Built-in providers + a curated model catalog
#
# Model ids move; the settings page can pull the LIVE list from any provider
# (/v1/models or /api/tags), which always wins. The catalog below is a
# starting point with a short "why", so a fresh install has a good default.
# ---------------------------------------------------------------------------

DEFAULT_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "local": {
        "label": "Local model",
        "kind": KIND_OLLAMA,
        "base_url": "http://127.0.0.1:11434",
        "model": "gpt-oss:20b",
        "api_key_env": "",
        "rpm": 0,             # local: no quota
        "timeout": 120.0,
        "cooldown_s": 5.0,
        "note": "Private, free, no quota — the fallback that always works.",
    },
    "nim": {
        "label": "NVIDIA NIM",
        "kind": KIND_OPENAI,
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/nemotron-3-super-120b-a12b",
        "api_key_env": "NVIDIA_API_KEY",
        "rpm": 40,            # community baseline for the free tier
        "timeout": 240.0,
        "cooldown_s": 20.0,
        "note": "Builder. ~40 RPM, limits are per-model and unpublished.",
    },
    "gpt": {
        "label": "GPT (OpenAI-compatible)",
        "kind": KIND_OPENAI,
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.1",
        "api_key_env": "OPENAI_API_KEY",
        "rpm": 0,
        "timeout": 240.0,
        "cooldown_s": 15.0,
        "note": "Planner. Takes over as builder when NIM is rate-limited.",
    },
}

# Curated NIM picks (as of 2026-09; the live list in the settings page is
# authoritative). role hint = which role the model is a good fit for.
CATALOG: List[Dict[str, Any]] = [
    # --- NVIDIA-native (best throughput on NIM, strong tool calling) -------
    {"provider": "nim", "id": "nvidia/nemotron-3-super-120b-a12b",
     "role": "builder", "label": "Nemotron 3 Super 120B",
     "note": "Agentic workhorse: 1M context, strong SWE-bench, tool calling."},
    {"provider": "nim", "id": "nvidia/nemotron-3-ultra-550b-a55b",
     "role": "planner", "label": "Nemotron 3 Ultra 550B",
     "note": "Frontier reasoning/coding, slower — good for planning."},
    {"provider": "nim", "id": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
     "role": "builder", "label": "Llama 3.3 Nemotron Super 49B",
     "note": "Fastest native option, built for function calling."},
    {"provider": "nim", "id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
     "role": "builder", "label": "Nemotron 3 Nano Omni 30B",
     "note": "Cheap small model — good for batched, mechanical steps."},
    # --- Third-party on NIM ------------------------------------------------
    {"provider": "nim", "id": "moonshotai/kimi-k2.6",
     "role": "builder", "label": "Kimi K2.6",
     "note": "Long-horizon agentic coding, 1M+ context."},
    {"provider": "nim", "id": "zhipuai/glm-5.1",
     "role": "builder", "label": "GLM-5.1",
     "note": "Function calling + long-context coding."},
    {"provider": "nim", "id": "deepseek-ai/deepseek-v4-flash",
     "role": "builder", "label": "DeepSeek V4 Flash",
     "note": "Fast and cheap — batch work, small repairs."},
    {"provider": "nim", "id": "deepseek-ai/deepseek-v4-pro",
     "role": "planner", "label": "DeepSeek V4 Pro",
     "note": "Strongest coding scores, but slow (not for tight loops)."},
    {"provider": "nim", "id": "qwen/qwen3-coder-480b-a35b-instruct",
     "role": "builder", "label": "Qwen3 Coder 480B",
     "note": "Purpose-built for agentic coding."},
    {"provider": "nim", "id": "mistralai/devstral-2-123b-instruct-2512",
     "role": "builder", "label": "Devstral 2 123B",
     "note": "Dev-focused, fast tool calling."},
    {"provider": "nim", "id": "mistralai/mistral-nemotron",
     "role": "builder", "label": "Mistral Nemotron",
     "note": "Built for agentic workflows / function calling."},
    {"provider": "nim", "id": "minimaxai/minimax-m2.7",
     "role": "builder", "label": "MiniMax M2.7",
     "note": "Strong all-rounder, high latency."},
    {"provider": "nim", "id": "meta/llama-4-maverick-17b-128e-instruct",
     "role": "builder", "label": "Llama 4 Maverick",
     "note": "Popular general purpose model."},
    # --- Planner side ------------------------------------------------------
    {"provider": "gpt", "id": "gpt-5.1", "role": "planner",
     "label": "GPT-5.1", "note": "Flagship coding/agentic planner."},
    {"provider": "gpt", "id": "gpt-5-mini", "role": "planner",
     "label": "GPT-5 mini", "note": "Cheaper planning."},
    {"provider": "gpt", "id": "gpt-5.2", "role": "planner",
     "label": "GPT-5.2", "note": "Reasoning effort configurable."},
    {"provider": "gpt", "id": "gpt-5.6-terra", "role": "planner",
     "label": "GPT-5.6 Terra", "note": "Production generalist."},
]

# Env -> provider field overrides (kept small and explicit).
ENV_PROVIDER_KEYS: Dict[str, Dict[str, str]] = {
    "local": {"base_url": "OLLAMA_HOST", "model": "OLLAMA_MODEL"},
    "nim": {"base_url": "NEX_NIM_BASE_URL", "model": "NEX_NIM_MODEL",
            "api_key": "NEX_NIM_API_KEY", "rpm": "NEX_NIM_RPM"},
    "gpt": {"base_url": "NEX_OPENAI_BASE_URL", "model": "NEX_OPENAI_MODEL",
            "api_key": "NEX_OPENAI_API_KEY"},
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def mask_key(key: Optional[str]) -> str:
    """Never hand a key to the browser. 'nvapi-abc…9f2' or '' when unset."""
    if not key:
        return ""
    s = str(key)
    if len(s) <= 8:
        return "…" + s[-2:]
    return s[:6] + "…" + s[-4:]


# --- provider URLs -----------------------------------------------------------
#
# A provider URL is an OUTBOUND REQUEST made by this server with a credential
# attached, so it is not a free-form setting:
#
#   * http/https only (no file://, no gopher://, no userinfo tricks),
#   * cloud metadata + link-local targets are refused outright (the classic
#     SSRF escalation: 169.254.169.254, fd00:ec2::254, metadata.google.internal),
#   * a base URL pointing at Nex itself is refused (loop),
#   * redirects are only followed to the SAME host — otherwise a redirect
#     would hand the Authorization header to a third party,
#   * and a key is BOUND to the host it was entered for: change the endpoint
#     and the key goes dark until it is re-entered (see ProviderSpec.key).
#
# NEX_PROVIDER_HOSTS adds a strict allowlist (comma-separated). When set, only
# those hosts (plus loopback) may be configured — that is the switch for a
# locked-down deployment.
_METADATA_HOSTS = frozenset({
    "metadata.google.internal", "metadata.goog",
    "169.254.169.254", "169.254.170.2", "100.100.100.200",
    "fd00:ec2::254", "instance-data",
})


def allowed_provider_hosts() -> List[str]:
    raw = os.environ.get("NEX_PROVIDER_HOSTS", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _norm_host(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h


def _is_loopback(host: str) -> bool:
    h = _norm_host(host)
    return h in ("127.0.0.1", "localhost", "::1") or h.startswith("127.")


def _is_link_local(host: str) -> bool:
    h = _norm_host(host)
    if h.startswith("169.254.") or h.startswith("fe80:"):
        return True
    if h in _METADATA_HOSTS:
        return True
    # IPv4-mapped forms arrive as ::ffff:169.254.169.254
    return ":169.254." in h or h.endswith(":169.254.169.254")


def validate_base_url(url: str) -> str:
    """Return the normalized base URL or raise ValueError with the reason."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("empty base URL")
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("only http:// and https:// endpoints are allowed "
                         "(got %r)" % (parts.scheme or "no scheme"))
    if "@" in (parts.netloc or ""):
        raise ValueError("credentials inside the URL are not allowed")
    host = _norm_host(parts.hostname or "")
    if not host:
        raise ValueError("no host in base URL")
    if _is_link_local(host):
        raise ValueError("refusing a link-local/metadata address (%s) — that "
                         "is an SSRF target, not a model endpoint" % host)
    if parts.query or parts.fragment:
        raise ValueError("base URL must not carry a query or fragment")
    allow = allowed_provider_hosts()
    if allow and not _is_loopback(host) and host not in allow:
        raise ValueError("host %r is not in NEX_PROVIDER_HOSTS" % host)
    if _is_loopback(host):
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise ValueError("invalid port") from None
        if port == int(os.environ.get("NEX_PORT", "8787") or 8787):
            raise ValueError("refusing to point a provider at Nex itself "
                             "(port %d)" % port)
    path = (parts.path or "").rstrip("/")
    netloc = parts.netloc
    return "%s://%s%s" % (parts.scheme.lower(), netloc, path)


def _host_of(url: str) -> str:
    try:
        return _norm_host(urllib.parse.urlsplit(url or "").hostname or "")
    except ValueError:
        return ""


def _next_candidate(chain: List[str], index: int) -> str:
    """Human sentence fragment for "who takes over"."""
    rest = [n for n in chain[index + 1:]]
    if not rest:
        return "no fallback is configured"
    if len(rest) == 1:
        return "%s continues the build" % rest[0]
    return "%s or %s continues the build" % (rest[0], rest[-1])


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within the same host.

    urllib re-sends the original request headers (including a bearer token)
    when it follows a redirect. A model endpoint that answers 302 to another
    host would therefore exfiltrate the operator's key — so cross-host
    redirects are refused here, loudly.
    """

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        src = _host_of(req.full_url)
        dst = _host_of(newurl)
        if not dst or dst != src:
            raise urllib.error.HTTPError(
                req.full_url, code,
                "refused a cross-host redirect to %s (would leak the "
                "Authorization header)" % (dst or "?"), headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirectHandler())


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# .env loading — the operator path for keys
# ---------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse a .env body. Supports `export `, quotes and #-comments."""
    out: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        # Strip an unquoted trailing comment.
        if val[:1] not in ('"', "'"):
            val = val.split(" #", 1)[0].strip()
        else:
            quote = val[0]
            end = val.find(quote, 1)
            val = val[1:end] if end > 0 else val[1:]
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        out[key] = val.strip()
    return out


def env_file_paths(explicit: Optional[Iterable[str]] = None) -> List[str]:
    if explicit is not None:
        return [p for p in explicit if p]
    return [
        os.path.join(NEX_DIR, ".env"),                     # nex/.env
        os.path.join(os.path.dirname(NEX_DIR), ".env"),    # repo-root .env
        os.path.join(os.path.expanduser("~"), ".nex", ".env"),
    ]


def load_env_file(path: str, environ: Optional[Dict[str, str]] = None) -> int:
    """Load one .env file. Existing process env WINS (operator override).

    Returns the number of newly set keys.
    """
    env = environ if environ is not None else os.environ
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0
    added = 0
    for key, val in parse_env_text(text).items():
        if key not in env or env.get(key) in (None, ""):
            env[key] = val
            added += 1
    return added


def load_env(paths: Optional[Iterable[str]] = None) -> int:
    """Load the default .env chain (nex/.env, repo .env, ~/.nex/.env)."""
    total = 0
    for p in env_file_paths(paths):
        total += load_env_file(p)
    return total


# ---------------------------------------------------------------------------
# Settings store — ~/.nex/providers.json (0600)
# ---------------------------------------------------------------------------

class SettingsStore:
    """Tiny JSON store for provider config. Never logs or returns raw keys."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.environ.get("NEX_PROVIDERS_FILE") \
            or DEFAULT_SETTINGS_PATH

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, data: Dict[str, Any]) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def patch(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        data = self.load()
        for k, v in (patch or {}).items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
        self.save(data)
        return data


# ---------------------------------------------------------------------------
# HTTP transport (injectable for tests)
# ---------------------------------------------------------------------------

def _http_post_json(url: str, body: Dict[str, Any],
                    headers: Optional[Dict[str, str]] = None,
                    timeout: float = 60.0) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    """POST JSON. Returns (status, parsed_body, response_headers). Raises on
    transport errors so the caller can classify them."""
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        resp_headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        try:
            return resp.status, json.loads(raw), resp_headers
        except json.JSONDecodeError:
            return resp.status, {"_raw": raw[:4000]}, resp_headers


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: float = 30.0) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            return resp.status, {"_raw": raw[:4000]}


class ProviderError(Exception):
    """A provider call failed in a way the router can act on."""

    def __init__(self, kind: str, message: str,
                 retry_after: Optional[float] = None,
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.status = status

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return "%s: %s" % (self.kind, super().__str__())


class AllProvidersFailed(RuntimeError):
    """Every provider in the chain failed. The caller must degrade honestly —
    never invent an answer."""

    def __init__(self, role: str, attempts: List[Dict[str, Any]]) -> None:
        self.role = role
        self.attempts = attempts
        detail = "; ".join("%s=%s" % (a.get("provider"), a.get("error"))
                           for a in attempts) or "no provider configured"
        super().__init__("no provider could serve role '%s' (%s)" % (role, detail))


def _classify_http_error(exc: urllib.error.HTTPError) -> ProviderError:
    status = getattr(exc, "code", 0) or 0
    retry_after = None
    try:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        if ra:
            retry_after = _as_float(ra, 0.0) or None
    except Exception:  # noqa: BLE001
        retry_after = None
    body = ""
    try:
        body = exc.read().decode("utf-8", "replace")[:400]
    except Exception:  # noqa: BLE001
        body = ""
    if status == 429:
        return ProviderError(ERR_RATE_LIMIT, "rate limited (429) %s" % body,
                             retry_after=retry_after, status=status)
    if status in (401, 403):
        return ProviderError(ERR_AUTH, "auth rejected (%d) %s" % (status, body),
                             status=status)
    if 500 <= status < 600:
        return ProviderError(ERR_SERVER, "server error (%d)" % status,
                             status=status)
    return ProviderError(ERR_BAD_REQUEST, "request rejected (%d) %s"
                         % (status, body), status=status)


# ---------------------------------------------------------------------------
# Provider spec + live state
# ---------------------------------------------------------------------------

class ProviderSpec:
    """A configured provider endpoint. Plain data + URL/key resolution."""

    FIELDS = ("kind", "base_url", "model", "api_key", "api_key_env", "rpm",
              "timeout", "cooldown_s", "label", "enabled", "temperature",
              "max_tokens", "batch_max", "note", "key_host",
              # pacing: how much of the RPM window is held back for
              # interactive/manual calls, how close two requests may sit,
              # and how long a single call may WAIT instead of spending quota.
              "reserve", "min_interval_s", "max_chill_s")

    def __init__(self, name: str, **kw: Any) -> None:
        base = dict(DEFAULT_PROVIDERS.get(name, {}))
        base.update({k: v for k, v in kw.items() if k in self.FIELDS})
        self.name = name
        self.label = str(base.get("label") or name)
        self.kind = str(base.get("kind") or KIND_OPENAI)
        self.base_url = validate_base_url(str(base.get("base_url") or ""))\
            if base.get("base_url") else ""
        if not self.base_url:
            raise ValueError("provider %s has no base URL" % name)
        self.model = str(base.get("model") or "")
        self.api_key = str(base.get("api_key") or "")
        self.api_key_env = str(base.get("api_key_env") or "")
        # The host this key was issued for. A key without a binding is legacy
        # (or a test) and still works; a key WITH a binding that no longer
        # matches the endpoint goes DARK instead of travelling to a new host.
        self.key_host = str(base.get("key_host") or "")
        self.rpm = _as_int(base.get("rpm"), 0)
        self.timeout = _as_float(base.get("timeout"), 120.0)
        self.cooldown_s = _as_float(base.get("cooldown_s"), 20.0)
        self.temperature = _as_float(base.get("temperature"), 0.2)
        self.max_tokens = _as_int(base.get("max_tokens"), 0)
        self.batch_max = _as_int(base.get("batch_max"), 6)
        # Keep 25% of a limited window in reserve: a build can always be
        # paused, but the 41st request in a minute cannot be taken back.
        self.reserve = min(0.9, max(0.0, _as_float(base.get("reserve"), 0.25)))
        self.min_interval_s = max(0.0, _as_float(base.get("min_interval_s"), 1.0))
        self.max_chill_s = max(0.0, _as_float(base.get("max_chill_s"), 8.0))
        self.enabled = bool(base.get("enabled", True))
        self.note = str(base.get("note") or "")

    # -- key resolution ---------------------------------------------------
    @property
    def raw_key(self) -> str:
        """The stored/env key, regardless of the endpoint it is bound to."""
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "").strip()
        return ""

    @property
    def key_mismatch(self) -> bool:
        """True when a key exists but belongs to a DIFFERENT host."""
        if not self.raw_key or not self.key_host:
            return False
        return _host_of(self.base_url) != _norm_host(self.key_host)

    @property
    def key(self) -> str:
        """The key actually sent — empty when it does not match the host."""
        raw = self.raw_key
        if not raw or self.key_mismatch:
            return ""
        return raw

    def bind_key(self, host: Optional[str] = None) -> None:
        self.key_host = _norm_host(host) if host else _host_of(self.base_url)

    @property
    def configured(self) -> bool:
        """Ollama needs no key; OpenAI-compatible providers do."""
        if not self.enabled or not self.base_url:
            return False
        if self.kind == KIND_OPENAI and not self.key and not self._is_local_host():
            return False
        return True

    @property
    def unconfigured_reason(self) -> str:
        if self.key_mismatch:
            return ("the key belongs to %s but the endpoint is %s — the key "
                    "was NOT sent; re-enter it to confirm this endpoint"
                    % (self.key_host, _host_of(self.base_url) or "?"))
        if not self.enabled:
            return "disabled"
        if self.kind == KIND_OPENAI and not self.raw_key and not self._is_local_host():
            return "no API key configured"
        return ""

    def _is_local_host(self) -> bool:
        return any(h in self.base_url for h in ("127.0.0.1", "localhost", "::1"))

    # -- urls -------------------------------------------------------------
    def _endpoint(self, suffix: str) -> str:
        """Base URL + endpoint, WITHOUT doubling the API version.

        A base URL is written the way every provider documents it —
        ``https://integrate.api.nvidia.com/v1`` — and the OpenAI-compatible
        endpoint is ``/v1/chat/completions``, so appending naively produces
        ``/v1/v1/chat/completions`` and a 404 that looks like a provider
        failure. The version segment belongs to exactly one of the two.
        """
        base = (self.base_url or "").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-len("/v1")]
        return base + suffix

    @property
    def chat_url(self) -> str:
        if self.kind == KIND_OLLAMA:
            return self._endpoint("/api/chat")
        return self._endpoint("/v1/chat/completions")

    @property
    def models_url(self) -> str:
        if self.kind == KIND_OLLAMA:
            return self._endpoint("/api/tags")
        return self._endpoint("/v1/models")

    def masked(self) -> Dict[str, Any]:
        return {
            "name": self.name, "label": self.label, "kind": self.kind,
            "base_url": self.base_url, "model": self.model,
            "api_key": mask_key(self.key),           # never the raw key
            "api_key_set": bool(self.key),
            "api_key_stored": bool(self.raw_key),
            "api_key_env": self.api_key_env,
            "key_host": self.key_host,
            "key_mismatch": self.key_mismatch,
            "unconfigured_reason": self.unconfigured_reason,
            "rpm": self.rpm, "timeout": self.timeout,
            "cooldown_s": self.cooldown_s, "enabled": self.enabled,
            "reserve": self.reserve, "min_interval_s": self.min_interval_s,
            "configured": self.configured, "note": self.note,
        }


class ProviderState:
    """Live state: sliding-window RPM budget, cooldown, counters."""

    def __init__(self, name: str, rpm: int = 0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.rpm = rpm
        self._clock = clock
        self._window: Deque[float] = deque()
        self.cooldown_until = 0.0
        self.status = ST_AVAILABLE
        self.last_error = ""
        self.last_error_kind = ""
        self.last_ok_ts = 0.0
        self.last_call_ts = 0.0
        self.total_calls = 0
        self.total_ok = 0
        self.total_failed = 0
        self.rate_limited = 0
        self.auth_failures = 0
        self.batch_splits = 0
        self.last_retry_after: Optional[float] = None
        self.reserve_ratio = 0.25
        self.last_call_clock = 0.0
        self.chills = 0
        self.paced_skips = 0
        # A rejected key is a CONFIG problem: it blocks the provider for much
        # longer than a rate limit and is only lifted by fixing the config.
        self.auth_blocked_until = 0.0

    # -- budget -----------------------------------------------------------
    def _trim(self, now: float) -> None:
        while self._window and now - self._window[0] >= 60.0:
            self._window.popleft()

    def used_in_window(self) -> int:
        now = self._clock()
        self._trim(now)
        return len(self._window)

    def budget_left(self) -> int:
        if self.rpm <= 0:
            return 10 ** 6
        return max(0, self.rpm - self.used_in_window())

    def soft_cap(self) -> int:
        """Where we STOP spending and start saving (rpm minus the reserve)."""
        if self.rpm <= 0:
            return 10 ** 6
        return max(1, int(round(self.rpm * (1.0 - self.reserve_ratio))))

    def seconds_until_slot(self) -> float:
        """Seconds until the oldest request leaves the window (0 if room)."""
        now = self._clock()
        self._trim(now)
        if self.rpm <= 0 or len(self._window) < self.rpm:
            return 0.0
        return max(0.0, 60.0 - (now - self._window[0]))

    def reserve(self) -> bool:
        """Take one request slot. False = local budget exhausted."""
        if self.rpm <= 0:
            return True
        now = self._clock()
        self._trim(now)
        if len(self._window) >= self.rpm:
            return False
        self._window.append(now)
        return True

    # -- state ------------------------------------------------------------
    def auth_block_s(self) -> float:
        return _as_float(os.environ.get("NEX_AUTH_BLOCK_S"), 900.0)

    def auth_blocked(self) -> bool:
        return bool(self.auth_blocked_until) \
            and self._clock() < self.auth_blocked_until

    def clear_auth_block(self) -> None:
        self.auth_blocked_until = 0.0
        if self.status == ST_ERROR and self.last_error_kind == ERR_AUTH:
            self.status = ST_AVAILABLE
            self.last_error = ""
            self.last_error_kind = ""

    def available(self) -> bool:
        if self.status == ST_DISABLED:
            return False
        if self.status == ST_NO_KEY:
            return False
        if self.auth_blocked():
            self.status = ST_ERROR
            return False
        if self.cooldown_until and self._clock() < self.cooldown_until:
            if self.status not in (ST_RATE_LIMITED, ST_COOLING, ST_ERROR):
                self.status = ST_COOLING
            return False
        # cooldown elapsed -> back in the pool
        if self.status in (ST_RATE_LIMITED, ST_COOLING, ST_ERROR):
            self.status = ST_AVAILABLE
            self.last_error = ""
            self.last_error_kind = ""
            self.cooldown_until = 0.0
        return True

    def mark_ok(self) -> None:
        self.status = ST_AVAILABLE
        self.cooldown_until = 0.0
        self.last_error = ""
        self.last_error_kind = ""
        self.last_ok_ts = time.time()

    def mark_error(self, kind: str, message: str,
                   retry_after: Optional[float] = None) -> None:
        now = self._clock()
        self.last_error = message[:300]
        self.last_error_kind = kind
        self.total_failed += 1
        if kind == ERR_RATE_LIMIT:
            self.rate_limited += 1
            self.status = ST_RATE_LIMITED
            self.last_retry_after = retry_after
            # Retry when it can ACTUALLY work again: either the moment the
            # provider asked for (Retry-After) or the moment our own window
            # frees a slot — whichever is later. That is what makes "after
            # the minute is over, NIM is tried again" true instead of
            # hopeful: a fixed 20s cooldown would just hit the wall again.
            window_wait = self.seconds_until_slot()
            wait = max(float(retry_after or 0.0), window_wait, 2.0)
            self.cooldown_until = now + min(wait, 120.0)
        elif kind in (ERR_TIMEOUT, ERR_NETWORK):
            self.status = ST_COOLING
            self.cooldown_until = now + 5.0
        elif kind == ERR_SERVER:
            self.status = ST_COOLING
            self.cooldown_until = now + 10.0
        elif kind == ERR_AUTH:
            # Not a hiccup — a wrong/expired key. Stop hammering it entirely
            # until the configuration changes (clear_auth_block) and let the
            # router work with a provider that actually has credentials.
            self.auth_failures += 1
            self.status = ST_ERROR
            self.cooldown_until = 0.0
            self.auth_blocked_until = now + self.auth_block_s()
        else:  # malformed request / bad response — do not hammer it either
            self.status = ST_ERROR
            self.cooldown_until = now + 120.0

    def mark_missing_key(self) -> None:
        self.status = ST_NO_KEY
        self.last_error = "no API key configured"
        self.last_error_kind = ERR_AUTH

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "available": self.available(),
            "rpm": self.rpm,
            "requests_in_window": self.used_in_window(),
            "budget_left": None if self.rpm <= 0 else self.budget_left(),
            "cooldown_s": round(max(0.0, self.cooldown_until - self._clock()), 1),
            "auth_blocked_s": round(max(0.0, self.auth_blocked_until
                                        - self._clock()), 1),
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
            "total_calls": self.total_calls,
            "total_ok": self.total_ok,
            "total_failed": self.total_failed,
            "rate_limited": self.rate_limited,
            "auth_failures": self.auth_failures,
            "batch_splits": self.batch_splits,
            "soft_cap": self.soft_cap(),
            "chills": self.chills,
            "paced_skips": self.paced_skips,
            "headroom": self.rpm > 0 and self.used_in_window() >= self.soft_cap(),
        }


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

class Router:
    """Role-aware provider routing with failover.

    `chat(role, messages)` walks the role's chain and returns the first
    provider answer. Failures put a provider into cooldown; the next call
    tries it again once the cooldown is over — so NIM resumes by itself.
    """

    def __init__(self, specs: Dict[str, ProviderSpec],
                 roles: Dict[str, Dict[str, str]],
                 bus: Any = None,
                 clock: Callable[[], float] = time.monotonic,
                 transport: Optional[Dict[str, Callable[..., Any]]] = None) -> None:
        self._lock = threading.RLock()
        self.specs = specs
        self.roles = roles
        self.bus = bus
        self._clock = clock
        self.states: Dict[str, ProviderState] = {
            name: ProviderState(name, spec.rpm, clock)
            for name, spec in specs.items()
        }
        self._last_served: Dict[str, str] = {}
        self._transport = {
            "post": _http_post_json, "get": _http_get_json,
        }
        if transport:
            self._transport.update(transport)
        for spec in self.specs.values():
            # A key always belongs to SOME host. Binding here means a later
            # endpoint change cannot silently carry the secret along.
            if spec.raw_key and not spec.key_host:
                spec.bind_key()
        self.resolve_states()

    # -- configuration ----------------------------------------------------
    def resolve_states(self) -> None:
        for name, spec in self.specs.items():
            st = self.states.setdefault(name, ProviderState(name, spec.rpm,
                                                            self._clock))
            st.rpm = spec.rpm
            st.reserve_ratio = spec.reserve
            if not spec.enabled:
                st.status = ST_DISABLED
            elif not spec.configured:
                st.mark_missing_key()
                if spec.unconfigured_reason:
                    st.last_error = spec.unconfigured_reason
            elif st.status in (ST_DISABLED, ST_NO_KEY) and not st.auth_blocked():
                st.status = ST_AVAILABLE
                st.last_error = ""
                st.last_error_kind = ""

    def set_spec(self, name: str, **fields: Any) -> ProviderSpec:
        current = self.specs.get(name)
        merged: Dict[str, Any] = {}
        if current is not None:
            for f in ProviderSpec.FIELDS:
                merged[f] = getattr(current, f)
        merged.update({k: v for k, v in fields.items()
                       if k in ProviderSpec.FIELDS and v is not None})
        spec = ProviderSpec(name, **merged)
        if spec.raw_key and not spec.key_host:
            spec.bind_key()
        with self._lock:
            # The rate budget, the sliding window and the cooldowns belong to
            # the PROCESS, not to the config: editing a setting must never
            # hand back a fresh 40-RPM allowance (that would be a trivial
            # quota reset). Only a real identity change (endpoint or key)
            # lifts an AUTH block, because that is a new credential.
            state = self.states.get(name)
            if state is None:
                state = ProviderState(name, spec.rpm, self._clock)
            state.rpm = spec.rpm
            state.reserve_ratio = spec.reserve
            if current is not None and (
                    current.base_url != spec.base_url
                    or current.api_key != spec.api_key
                    or current.key_host != spec.key_host
                    or current.api_key_env != spec.api_key_env):
                state.clear_auth_block()
            self.specs[name] = spec
            self.states[name] = state
            self.resolve_states()
        return spec

    def set_role(self, role: str, provider: str,
                 model: Optional[str] = None,
                 fallbacks: Optional[List[str]] = None) -> None:
        if role not in ROLES:
            return
        entry = dict(self.roles.get(role) or {})
        previous = str(entry.get("provider") or "")
        entry["provider"] = provider
        if model is not None:
            entry["model"] = model
        elif previous and previous != provider:
            # Switching the provider without naming a model: the old model
            # name belongs to the OLD provider (gpt-oss:20b is not an OpenAI
            # model). Clear it so the role adopts the new provider's model.
            entry["model"] = ""
        if fallbacks is not None:
            entry["fallbacks"] = [str(f) for f in fallbacks if str(f)]
        self.roles[role] = entry

    def role_model(self, role: str) -> str:
        entry = self.roles.get(role) or {}
        model = str(entry.get("model") or "").strip()
        if model:
            return model
        spec = self.specs.get(str(entry.get("provider") or ""))
        return spec.model if spec else ""

    def fallbacks(self, role: str) -> List[str]:
        entry = self.roles.get(role) or {}
        listed = entry.get("fallbacks")
        if listed is None:
            listed = ROLE_DEFAULT_FALLBACKS.get(role, ["local"])
        return [str(n) for n in listed if str(n)]

    def chain(self, role: str) -> List[str]:
        """Ordered provider names for a role: primary -> named fallbacks.

        EXPLICIT only. Builder: NIM -> gpt -> local. Planner: gpt -> nim ->
        local. A provider that is configured but not named in the role's
        fallback list can still be SELECTED as the primary — it just never
        appears in a chain by itself, so adding a provider (or a catalog
        entry) can never silently change who builds.
        """
        entry = self.roles.get(role) or {}
        primary = str(entry.get("provider") or "")
        order: List[str] = []
        if primary and primary in self.specs:
            order.append(primary)
        for name in self.fallbacks(role):
            if name in self.specs and name not in order:
                order.append(name)
        # The LOCAL model is the terminal fallback and cannot be removed from
        # a chain: it is the only provider that cannot lose a key, run out of
        # quota or answer with a 5xx. Everything else is explicit — but if
        # NIM is rate-limited and no GPT is configured, Ollama takes over
        # without anyone having to configure that.
        if "local" in self.specs and "local" not in order:
            order.append("local")
        return order

    # -- events -----------------------------------------------------------
    def _emit(self, event: Dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            payload = dict(event)
            payload.setdefault("ts", time.time())
            self.bus.publish(payload)
        except Exception:  # noqa: BLE001 — events must never break a call
            pass

    # -- transport --------------------------------------------------------
    # -- event helpers (shared by chat() and chat_stream()) ---------------
    def _emit_failure(self, name: str, spec: ProviderSpec, state: ProviderState,
                      role: str, purpose: str, exc: ProviderError,
                      chain: List[str], index: int) -> None:
        """One failed attempt: status + (when it is not the quota) trouble."""
        self._emit({
            "type": "provider.status", "provider": name,
            "role": role, "status": state.status,
            "error_kind": exc.kind, "error": str(exc)[:200],
            "detail": self._detail_line(), "purpose": purpose,
            "roles": self.role_snapshot(),
        })
        if exc.kind != ERR_RATE_LIMIT:
            # "if it's not a rate limit error it tells me" — a timeout, a
            # 5xx or an unusable answer is NOT the quota and the operator
            # should hear about it, even though the work continues on the
            # next provider.
            human = {
                ERR_TIMEOUT: "timed out",
                ERR_SERVER: "answered with a server error (5xx)",
                ERR_NETWORK: "is unreachable",
                ERR_PARSE: "returned an unusable answer",
                ERR_CONFIG: "is misconfigured",
                ERR_BAD_REQUEST: "rejected the request",
            }.get(exc.kind, exc.kind)
            self._emit({
                "type": "provider.trouble", "provider": name,
                "role": role, "error_kind": exc.kind,
                "error": str(exc)[:200], "purpose": purpose,
                "retry_in_s": round(max(0.0, state.cooldown_until
                                        - self._clock()), 1),
                "detail": "%s %s — %s; %s is retried automatically"
                          % (spec.label or name, human,
                             _next_candidate(chain, index),
                             spec.label or name),
                "roles": self.role_snapshot(),
            })
        if exc.kind == ERR_AUTH:
            # NOT a quiet failover: the key is wrong/expired and the operator
            # has to know. The call still continues on the next provider so a
            # build is not lost to a config mistake.
            self._emit({
                "type": "provider.auth_error", "provider": name,
                "role": role, "status": state.status,
                "auth_blocked_s": state.to_dict()["auth_blocked_s"],
                "error": str(exc)[:200], "purpose": purpose,
                "detail": ("key rejected — fix %s in the settings; "
                           "Nex continues on the fallback provider"
                           % (spec.label or name)),
                "roles": self.role_snapshot(),
            })

    def _emit_serving(self, name: str, spec: ProviderSpec, role: str,
                      purpose: str, primary: str, chain: List[str]) -> None:
        """A provider (not necessarily the primary) answered."""
        previous = self._last_served.get(role, "")
        switched = previous not in (None, "", name)
        self._last_served[role] = name
        if name != primary:
            # The plan does NOT change — only the hands.
            self._emit({"type": "provider.fallback", "role": role,
                        "from": primary, "to": name,
                        "error_kind": (self.states[primary].last_error_kind
                                       if primary in self.states else ""),
                        "purpose": purpose, "plan_continues": True,
                        "roles": self.role_snapshot()})
            return
        self._emit({"type": "provider.call", "role": role,
                    "provider": name, "model": self.role_model(role),
                    "purpose": purpose, "recovered": switched,
                    "roles": self.role_snapshot()})
        if switched:
            # A rate limit is temporary by definition. When the window has
            # room again the primary is simply used again — and that is worth
            # saying out loud.
            self._emit({"type": "provider.recovered", "role": role,
                        "provider": name,
                        "model": self.role_model(role),
                        "was": previous,
                        "detail": "%s is available again — %s is the "
                                  "builder again"
                                  % (spec.label or name, purpose or role),
                        "roles": self.role_snapshot()})

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------
    def _stream_http(self, url: str, body: Dict[str, Any],
                     headers: Dict[str, str], timeout: float):
        """Default streaming transport: POST, yield decoded lines.

        Kept as a thin, injectable seam (``_transport["stream"]``) so tests
        never open a socket — the same discipline as the non-streaming
        transport.
        """
        data = json.dumps(body).encode("utf-8")
        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs,
                                     method="POST")
        with _OPENER.open(req, timeout=timeout) as resp:
            for raw in resp:
                yield raw.decode("utf-8", errors="replace")

    def _stream_call(self, spec: ProviderSpec,
                     messages: List[Dict[str, str]],
                     temperature: Optional[float] = None):
        """Stream one provider call. Raises ProviderError before the first
        token if the provider cannot serve it at all."""
        try:
            validate_base_url(spec.base_url)
        except ValueError as exc:
            raise ProviderError(ERR_CONFIG, str(exc)) from None
        if spec.key_mismatch:
            raise ProviderError(ERR_CONFIG,
                                "key/endpoint mismatch: "
                                + spec.unconfigured_reason)
        headers: Dict[str, str] = {}
        if spec.key:
            headers["Authorization"] = "Bearer " + spec.key
        temp = spec.temperature if temperature is None else temperature
        if spec.kind == KIND_OLLAMA:
            body: Dict[str, Any] = {"model": spec.model, "messages": messages,
                                    "stream": True,
                                    "options": {"temperature": temp}}
        else:
            body = {"model": spec.model, "messages": messages,
                    "stream": True, "temperature": temp}
            mt = spec.max_tokens
            if mt:
                body["max_tokens"] = mt
        stream = self._transport.get("stream") or self._stream_http
        try:
            lines = stream(spec.chat_url, body, headers, spec.timeout)
            for raw in lines:
                line = (raw or "").strip()
                if not line:
                    continue
                if spec.kind == KIND_OLLAMA:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    chunk = (obj.get("message") or {}).get("content")
                    if chunk:
                        yield chunk
                    if obj.get("done"):
                        return
                    continue
                if line.startswith("data:"):
                    line = line[len("data:"):].strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        return
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                chunk = delta.get("content")
                if chunk:
                    yield chunk
                if choices[0].get("finish_reason"):
                    return
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            kind = ERR_TIMEOUT if "timed out" in repr(exc).lower() else ERR_NETWORK
            raise ProviderError(kind, "unreachable: %r" % (exc,)) from None

    def chat_stream(self, role: str, messages: List[Dict[str, str]],
                    purpose: str = "",
                    temperature: Optional[float] = None):
        """Stream a chat answer for `role` through the SAME chain as chat().

        Yields content tokens. The routing rules are not duplicated: the
        chain order, the pacing/reserve check, the cooldown and the key
        binding are the ones the non-streaming call uses, and the same
        events are emitted.

        One deliberate difference: a provider is only swapped while NOTHING
        has been streamed yet. Once tokens reached the caller a second
        provider would repeat the visible answer, so the stream ends there
        and the failure is reported instead (an honest partial answer beats
        a duplicated one).
        """
        chain = self.chain(role)
        attempts: List[Dict[str, Any]] = []
        primary = chain[0] if chain else ""
        for index, name in enumerate(chain):
            spec = self.specs.get(name)
            state = self.states.get(name)
            if spec is None or state is None:
                continue
            if not spec.configured:
                state.mark_missing_key()
                attempts.append({"provider": name, "error": "not configured"})
                continue
            if not state.available():
                attempts.append({"provider": name, "error": state.status})
                continue
            if self._pace(role, chain, index, name, spec, state, purpose) \
                    == "skip":
                attempts.append({"provider": name, "error": "pacing"})
                continue
            if not state.reserve():
                state.mark_error(ERR_RATE_LIMIT,
                                 "local RPM budget exhausted (%d/min)"
                                 % spec.rpm)
                attempts.append({"provider": name, "error": "rpm_budget"})
                self._emit({"type": "provider.status",
                            "roles": self.role_snapshot(),
                            "note": "budget exhausted"})
                continue
            state.total_calls += 1
            state.last_call_ts = time.time()
            state.last_call_clock = self._clock()
            first: Optional[str] = None
            try:
                stream = self._stream_call(spec, messages, temperature)
                for token in stream:
                    first = token
                    break
            except ProviderError as exc:
                state.mark_error(exc.kind, str(exc), exc.retry_after)
                attempts.append({"provider": name, "error": exc.kind,
                                 "detail": str(exc)[:200]})
                self._emit_failure(name, spec, state, role, purpose, exc,
                                   chain, index)
                continue
            except Exception as exc:  # noqa: BLE001 — transport surprises
                state.mark_error(ERR_NETWORK, repr(exc))
                attempts.append({"provider": name, "error": ERR_NETWORK,
                                 "detail": repr(exc)[:200]})
                self._emit_failure(name, spec, state, role, purpose,
                                   ProviderError(ERR_NETWORK, repr(exc)),
                                   chain, index)
                continue
            if not first:
                # A connection that opens and says nothing is not an answer.
                exc = ProviderError(ERR_PARSE, "empty stream from provider")
                state.mark_error(exc.kind, str(exc))
                attempts.append({"provider": name, "error": exc.kind,
                                 "detail": str(exc)[:200]})
                self._emit_failure(name, spec, state, role, purpose, exc,
                                   chain, index)
                continue
            state.mark_ok()
            state.total_ok += 1
            self._emit_serving(name, spec, role, purpose, primary, chain)
            yield first
            try:
                for token in stream:
                    yield token
            except ProviderError as exc:
                # Mid-answer failure: the caller already has text. Do NOT
                # restart on another provider (that would duplicate it) —
                # record it and let the stream end.
                state.mark_error(exc.kind, str(exc), exc.retry_after)
                self._emit_failure(name, spec, state, role, purpose, exc,
                                   chain, index)
            except Exception as exc:  # noqa: BLE001
                state.mark_error(ERR_NETWORK, repr(exc))
                self._emit_failure(name, spec, state, role, purpose,
                                   ProviderError(ERR_NETWORK, repr(exc)),
                                   chain, index)
            return
        raise AllProvidersFailed(role, attempts)

    def _call(self, spec: ProviderSpec, messages: List[Dict[str, str]],
              temperature: Optional[float] = None,
              max_tokens: Optional[int] = None,
              timeout: Optional[float] = None) -> str:
        """One provider call. Raises ProviderError (never returns garbage)."""
        # Belt and braces: the URL was validated when it was configured, and
        # it is validated again here, because this is the line that attaches
        # the operator's key to an outbound request.
        try:
            validate_base_url(spec.base_url)
        except ValueError as exc:
            raise ProviderError(ERR_CONFIG, str(exc)) from None
        if spec.key_mismatch:
            raise ProviderError(ERR_CONFIG,
                                "key/endpoint mismatch: " + spec.unconfigured_reason)
        headers: Dict[str, str] = {}
        if spec.key:
            headers["Authorization"] = "Bearer " + spec.key
        temp = spec.temperature if temperature is None else temperature
        if spec.kind == KIND_OLLAMA:
            body: Dict[str, Any] = {
                "model": spec.model, "messages": messages, "stream": False,
                "options": {"temperature": temp},
            }
        else:
            body = {"model": spec.model, "messages": messages,
                    "stream": False, "temperature": temp}
            mt = max_tokens or spec.max_tokens
            if mt:
                body["max_tokens"] = mt
        try:
            status, obj, headers_out = self._transport["post"](
                spec.chat_url, body, headers,
                spec.timeout if timeout is None else timeout)
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            kind = ERR_TIMEOUT if "timed out" in repr(exc).lower() else ERR_NETWORK
            raise ProviderError(kind, "unreachable: %r" % (exc,)) from None
        if not isinstance(obj, dict):
            raise ProviderError(ERR_PARSE, "provider returned non-JSON body")
        text = _extract_text(obj, spec.kind, spec.model)
        if not text:
            raise ProviderError(ERR_PARSE, "empty completion from provider "
                                           "(model=%s)" % (spec.model or "?"))
        return text

    # -- public API -------------------------------------------------------
    # -- pacing: spend the rate budget on purpose, not by accident --------
    def _has_candidate(self, chain: List[str], after: int) -> bool:
        """Is there a usable provider further down the chain?"""
        for name in chain[after + 1:]:
            spec = self.specs.get(name)
            state = self.states.get(name)
            if spec and state and spec.configured and state.available():
                return True
        return False

    def _chill(self, seconds: float, name: str, state: ProviderState,
               purpose: str, reason: str) -> None:
        """Wait a bounded moment instead of spending quota we do not have."""
        if seconds <= 0:
            return
        state.chills += 1
        self._emit({"type": "provider.paced", "provider": name,
                    "reason": reason, "chill_s": round(seconds, 1),
                    "purpose": purpose, "roles": self.role_snapshot()})
        time.sleep(seconds)

    def _pace(self, role: str, chain: List[str], index: int, name: str,
              spec: ProviderSpec, state: ProviderState, purpose: str) -> str:
        """Decide whether this provider may spend a request right now.

        The NIM free tier is ~40 requests/minute with unpublished per-model
        limits, and going over is not recoverable — a 429 is a wasted request.
        So a limit is not a goal to reach, it is a ceiling to stay under:

          * a RESERVE (25% by default) is held back, so a build never eats
            the entire allowance of the minute;
          * while inside the reserve, a request is routed around to the
            fallback if one is usable (the plan continues either way);
          * if this is the only option left, it CHILLS for a bounded moment
            instead of hammering — and only calls when the window has room;
          * consecutive calls to the same provider keep a minimum spacing.

        Returns "call" (go ahead, possibly after a short chill) or "skip".
        """
        if state.rpm <= 0:
            return "call"
        used = state.used_in_window()
        if used >= state.rpm:
            # Hard ceiling reached: the next request WOULD be a 429.
            wait = state.seconds_until_slot()
            if wait <= spec.max_chill_s:
                self._chill(wait, name, state, purpose, "window full")
                return "call"
            state.mark_error(ERR_RATE_LIMIT,
                             "RPM window full (%d/min) — next slot in %.0fs" %
                             (state.rpm, wait))
            self._emit({"type": "provider.paced", "provider": name,
                        "reason": "window full",
                        "chill_s": round(wait, 1), "purpose": purpose,
                        "budget_left": 0, "roles": self.role_snapshot()})
            return "skip"
        if used >= state.soft_cap():
            # Inside the reserve: let a fallback absorb the work and keep the
            # remaining allowance for whatever comes next.
            if self._has_candidate(chain, index):
                state.paced_skips += 1
                self._emit({"type": "provider.paced", "provider": name,
                            "reason": "headroom reserve (%d of %d used, "
                                      "soft cap %d)" % (used, state.rpm,
                                                        state.soft_cap()),
                            "chill_s": 0, "purpose": purpose,
                            "budget_left": state.rpm - used,
                            "roles": self.role_snapshot()})
                return "skip"
            wait = min(state.seconds_until_slot(), spec.max_chill_s)
            self._chill(wait, name, state, purpose, "headroom, no fallback")
        # Minimum spacing between two calls to the same provider.
        gap = self._clock() - state.last_call_clock
        if spec.min_interval_s and gap < spec.min_interval_s:
            self._chill(spec.min_interval_s - gap, name, state, purpose,
                        "pacing")
        return "call"

    def chat(self, role: str, messages: List[Dict[str, str]],
             purpose: str = "", temperature: Optional[float] = None,
             max_tokens: Optional[int] = None,
             timeout: Optional[float] = None) -> str:
        """Route a chat call for `role`. Raises AllProvidersFailed if the
        whole chain is down (callers degrade honestly instead of guessing)."""
        chain = self.chain(role)
        attempts: List[Dict[str, Any]] = []
        primary = chain[0] if chain else ""
        for index, name in enumerate(chain):
            spec = self.specs.get(name)
            state = self.states.get(name)
            if spec is None or state is None:
                continue
            if not spec.configured:
                state.mark_missing_key()
                attempts.append({"provider": name, "error": "not configured"})
                continue
            if not state.available():
                attempts.append({"provider": name, "error": state.status})
                continue
            if self._pace(role, chain, index, name, spec, state, purpose) \
                    == "skip":
                attempts.append({"provider": name, "error": "pacing"})
                continue
            if not state.reserve():
                state.mark_error(ERR_RATE_LIMIT,
                                 "local RPM budget exhausted (%d/min)" % spec.rpm)
                attempts.append({"provider": name, "error": "rpm_budget"})
                self._emit({"type": "provider.status",
                            "roles": self.role_snapshot(),
                            "note": "budget exhausted"})
                continue
            state.total_calls += 1
            state.last_call_ts = time.time()
            state.last_call_clock = self._clock()
            try:
                text = self._call(spec, messages, temperature, max_tokens, timeout)
            except ProviderError as exc:
                state.mark_error(exc.kind, str(exc), exc.retry_after)
                attempts.append({"provider": name, "error": exc.kind,
                                 "detail": str(exc)[:200]})
                self._emit_failure(name, spec, state, role, purpose, exc,
                                   chain, index)
                continue
            state.mark_ok()
            state.total_ok += 1
            self._emit_serving(name, spec, role, purpose, primary, chain)
            return text
        raise AllProvidersFailed(role, attempts)

    def chat_batch(self, role: str, jobs: List[Dict[str, Any]],
                   instruction: str = "",
                   temperature: Optional[float] = None,
                   max_calls: Optional[int] = None) -> Dict[str, str]:
        """ONE call for N independent jobs — the cheap way to spend NIM's RPM.

        jobs: [{"key": "task-1", "prompt": "..."}]. Returns {key: text}.

        An unusable batch answer is retried by HALVING the chunk, never by
        falling back to one call per job: an 8-job batch that fails costs 1
        (batch) + 2 (halves) = 3 requests, not 8. The hard cap is
        `max_calls` (default NEX_BATCH_MAX_CALLS=3) per batch call, and a
        split only happens while the provider still has rate budget left.
        Jobs that never got an answer are simply missing from the result —
        the caller decides how to degrade.
        """
        jobs = [j for j in (jobs or []) if str(j.get("prompt") or "").strip()]
        if not jobs:
            return {}
        if len(jobs) == 1:
            key = str(jobs[0].get("key"))
            return {key: self.chat(role, [{"role": "user",
                                           "content": str(jobs[0]["prompt"])}],
                                   purpose="batch-1", temperature=temperature)}
        if max_calls is None:
            max_calls = _as_int(os.environ.get("NEX_BATCH_MAX_CALLS"), 3)
        max_calls = max(1, max_calls)
        size = len(jobs)
        for spec in self.specs.values():
            if spec.rpm:
                size = min(size, max(1, spec.batch_max))
                break
        chunks = [jobs[i:i + size] for i in range(0, len(jobs), size)]
        out: Dict[str, str] = {}
        used = {"n": 0}

        def budget_ok() -> bool:
            if used["n"] >= max_calls:
                return False
            # Never spend a split when every usable provider is empty.
            for name in self.chain(role):
                spec = self.specs.get(name)
                state = self.states.get(name)
                if spec and state and spec.configured and state.available() \
                        and (state.rpm <= 0 or state.budget_left() > 0):
                    return True
            return False

        def run(chunk: List[Dict[str, Any]]) -> None:
            if used["n"] >= max_calls:
                return                      # hard cap, also for the recursion
            keys = [str(j.get("key")) for j in chunk]
            body = _batch_prompt(instruction, chunk)
            used["n"] += 1
            try:
                text = self.chat(role, [{"role": "user", "content": body}],
                                 purpose="batch-%d" % len(chunk),
                                 temperature=temperature)
            except AllProvidersFailed:
                return
            parsed = _parse_batch_reply(text, keys)
            if parsed is not None:
                out.update(parsed)
                return
            if len(chunk) == 1:
                # A one-job chunk IS a normal single call: plain text is a
                # perfectly good answer for one prompt.
                if text.strip():
                    out[keys[0]] = text.strip()
                return
            if not budget_ok():
                return                      # capped: no per-job explosion
            self._count_split(role)
            mid = len(chunk) // 2
            run(chunk[:mid])
            run(chunk[mid:])

        for chunk in chunks:
            if used["n"] >= max_calls:
                break
            run(chunk)
        return out

    def _count_split(self, role: str) -> None:
        for name in self.chain(role):
            st = self.states.get(name)
            if st is not None:
                st.batch_splits += 1
                break

    def callable_for(self, role: str
                     ) -> Callable[[List[Dict[str, str]]], str]:
        """`llm(messages) -> str` bound to a role (what the agent loop takes)."""
        def _call(messages: List[Dict[str, str]]) -> str:
            return self.chat(role, messages)
        return _call

    def available(self, role: str) -> bool:
        for name in self.chain(role):
            spec = self.specs.get(name)
            state = self.states.get(name)
            if spec and state and spec.configured and state.available():
                return True
        return False

    def probe(self, name: str, live: bool = True) -> Dict[str, Any]:
        """Reachability test for the UI: does it answer, how fast, how many
        models does it offer."""
        spec = self.specs.get(name)
        state = self.states.get(name)
        if spec is None or state is None:
            return {"ok": False, "error": "unknown provider"}
        if not spec.enabled:
            return {"ok": False, "error": "disabled"}
        started = time.time()
        headers: Dict[str, str] = {}
        if spec.key:
            headers["Authorization"] = "Bearer " + spec.key
        try:
            status, obj = self._transport["get"](spec.models_url, headers,
                                                  min(spec.timeout, 30.0))
        except urllib.error.HTTPError as exc:
            err = _classify_http_error(exc)
            state.mark_error(err.kind, str(err), err.retry_after)
            return {"ok": False, "provider": name, "error": str(err)[:300],
                    "kind": err.kind, "status": spec.masked(), "live": {}}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            state.mark_error(ERR_NETWORK, repr(exc))
            return {"ok": False, "provider": name, "error": repr(exc)[:300],
                    "kind": ERR_NETWORK, "status": spec.masked(), "live": {}}
        models = _extract_models(obj, spec.kind)
        latency = int((time.time() - started) * 1000)
        state.mark_ok()
        return {
            "ok": True, "provider": name, "latency_ms": latency,
            "model_available": (not spec.model) or any(
                m == spec.model or m.split(":")[0] == spec.model.split(":")[0]
                for m in models) or not models,
            "models": models[:400] if live else models[:50],
            "models_count": len(models),
            "status": spec.masked(),
        }

    def list_models(self, name: str) -> List[str]:
        res = self.probe(name)
        return list(res.get("models") or [])

    # -- views ------------------------------------------------------------
    def role_snapshot(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for role in ROLES:
            chain = self.chain(role)
            entry = self.roles.get(role) or {}
            active = None
            for name in chain:
                spec = self.specs.get(name)
                state = self.states.get(name)
                if spec and state and spec.configured and state.available():
                    active = name
                    break
            serving = self._last_served.get(role)
            provider = str(entry.get("provider") or "")
            # Which MODEL is actually answering matters as much as which
            # provider: after a failover the role's configured model name is
            # no longer the truth (NIM's model name is not what GPT runs).
            def _model_for(name: str) -> str:
                if not name:
                    return ""
                if name == provider:
                    return self.role_model(role)
                spec = self.specs.get(name)
                return spec.model if spec else ""

            out[role] = {
                "provider": provider,
                "model": self.role_model(role),
                "active": active or "",
                "active_model": _model_for(active or ""),
                "serving": serving or "",
                "serving_model": _model_for(serving or ""),
                "fallback": bool(serving and serving != provider),
            }
        return out

    def status(self) -> Dict[str, Any]:
        snap = self.role_snapshot()
        return {
            "roles": snap,
            "planner": snap.get(ROLE_PLANNER, {}),
            "builder": snap.get(ROLE_BUILDER, {}),
            "providers": {name: dict(spec.masked(),
                                     **self.states[name].to_dict())
                          for name, spec in self.specs.items()},
            "chains": {role: self.chain(role) for role in ROLES},
            "note": "planner decides what to build; builder does the work "
                    "through MCP tools only",
        }

    def _detail_line(self) -> str:
        try:
            st = self.status()
            return "planner=%s builder=%s" % (
                st["planner"].get("active") or "-",
                st["builder"].get("active") or "-")
        except Exception:  # noqa: BLE001
            return ""

    # -- settings ---------------------------------------------------------
    def settings_view(self) -> Dict[str, Any]:
        return {
            "ok": True,
            # Masked spec + LIVE state, the same shape as status()["providers"]
            # — a caller should not have to know that there are two views.
            "providers": {name: dict(spec.masked(),
                                     **self.states[name].to_dict())
                          for name, spec in self.specs.items()},
            "roles": {role: dict(self.roles.get(role) or {},
                                 model=self.role_model(role),
                                 fallbacks=self.fallbacks(role))
                      for role in ROLES},
            "chains": {role: self.chain(role) for role in ROLES},
            "allowed_hosts": allowed_provider_hosts(),
            "role_models": {role: self.role_model(role) for role in ROLES},
            "status": self.status(),
            "catalog": CATALOG,
            "env": {
                "files": env_file_paths(),
                "nice_to_know": {
                    "NVIDIA_API_KEY": "NIM API key (nvapi-…) — build.nvidia.com",
                    "OPENAI_API_KEY": "GPT planner key",
                    "OLLAMA_HOST": "local model base URL",
                    "OLLAMA_MODEL": "local model name",
                    "NEX_PLANNER_PROVIDER": "which provider plans",
                    "NEX_BUILDER_PROVIDER": "which provider builds",
                    "NEX_NIM_RPM": "NIM requests/minute budget (default 40)",
                },
            },
            "settings_file": self.store_path(),
        }

    def store_path(self) -> str:
        return getattr(self, "_store_path", "") or os.environ.get(
            "NEX_PROVIDERS_FILE") or DEFAULT_SETTINGS_PATH

    def apply(self, patch: Dict[str, Any], store: Optional[SettingsStore] = None
              ) -> Dict[str, Any]:
        """Live-update from the settings page. Returns the new view.

        `api_key` values: "" keeps the stored key, "-" clears it.

        ATOMIC: the whole patch is validated first (URLs, hosts, roles) and
        nothing is touched if any provider in it is invalid — a settings form
        must not half-apply and leave the live config and the stored file
        disagreeing.
        """
        store = store or SettingsStore()
        stored = store.load()

        # ---- pass 1: validate, build the candidate specs -----------------
        planned: Dict[str, Dict[str, Any]] = {}
        for name, fields in dict(patch.get("providers") or {}).items():
            if not isinstance(fields, dict):
                continue
            clean: Dict[str, Any] = {}
            for k, v in fields.items():
                if k not in ProviderSpec.FIELDS:
                    continue
                if k == "api_key":
                    if v == "" or v is None:
                        continue          # keep what we have
                    if v == "-":
                        # Clearing the key also clears its binding.
                        clean["api_key"] = ""
                        clean["key_host"] = ""
                        continue
                    # A key typed HERE is bound to the endpoint that is in
                    # effect now (or the one in the same request): if the URL
                    # is later pointed elsewhere, the key goes dark instead of
                    # travelling to the new host.
                    clean["api_key"] = str(v).strip()
                    clean["key_host"] = _host_of(
                        str(fields.get("base_url")
                            or getattr(self.specs.get(name), "base_url", "")))
                    continue
                clean[k] = v
            planned[name] = clean
        current = self.specs
        for name, clean in planned.items():
            base_spec = current.get(name)
            merged = ({f: getattr(base_spec, f) for f in ProviderSpec.FIELDS}
                      if base_spec is not None else {})
            merged.update(clean)
            try:
                candidate = ProviderSpec(name, **merged)
                if candidate.raw_key and not candidate.key_host:
                    candidate.bind_key()
            except ValueError as exc:
                raise ValueError("provider %r rejected: %s" % (name, exc)) from None
        planned_roles: Dict[str, Dict[str, Any]] = {}
        for role, cfg in dict(patch.get("roles") or {}).items():
            if role not in ROLES or not isinstance(cfg, dict):
                continue
            provider = str(cfg.get("provider") or "").strip()
            if not provider:
                continue
            if provider not in planned and provider not in self.specs:
                raise ValueError("role %r names unknown provider %r"
                                 % (role, provider))
            falls = cfg.get("fallbacks")
            if isinstance(falls, str):
                falls = [f.strip() for f in falls.split(",") if f.strip()]
            if falls is not None:
                unknown = [f for f in falls
                           if f not in planned and f not in self.specs]
                if unknown:
                    raise ValueError("role %r names unknown fallback(s): %s"
                                     % (role, ", ".join(unknown)))
            planned_roles[role] = {"provider": provider,
                                   "model": cfg.get("model"),
                                   "fallbacks": falls}

        # ---- pass 2: apply (nothing above has mutated anything) ----------
        for name, clean in planned.items():
            entry = dict(stored.get("providers", {}).get(name) or {})
            entry.update(clean)
            stored.setdefault("providers", {})[name] = entry
            self.set_spec(name, **clean)
        for role, cfg in planned_roles.items():
            model = cfg.get("model")
            self.set_role(role, cfg["provider"],
                          None if model is None else str(model),
                          cfg.get("fallbacks"))
            stored.setdefault("roles", {})[role] = {
                "provider": cfg["provider"],
                "model": self.roles[role].get("model", ""),
                "fallbacks": self.fallbacks(role),
            }
        if planned or planned_roles:
            store.save(stored)
        self.resolve_states()
        view = self.settings_view()
        self._emit({"type": "provider.status", "note": "settings applied",
                    "roles": self.role_snapshot()})
        return view


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_text(obj: Dict[str, Any], kind: str, model: str = "") -> str:
    if kind == KIND_OLLAMA:
        msg = obj.get("message") or {}
        return str(msg.get("content") or "").strip()
    choices = obj.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    text = str(msg.get("content") or "").strip()
    if text:
        return text
    # NEVER promote chain-of-thought to the answer. A reasoning model whose
    # content got cut off returns its scratchpad — speculation, restatements
    # of the prompt and whatever text it was fed. Handing that back as "the
    # model's answer" to an autonomous agent is how a plan turns into noise,
    # so this is an honest failure (with the reason) instead.
    if str(msg.get("reasoning_content") or "").strip() or \
            str(msg.get("reasoning") or "").strip():
        raise ProviderError(
            ERR_PARSE,
            "model=%s returned reasoning without an answer (likely cut off by "
            "max_tokens) — raise max_tokens or pick a non-reasoning model; "
            "chain-of-thought is never used as a result" % (model or "?"))
    return ""


def _extract_models(obj: Dict[str, Any], kind: str) -> List[str]:
    if kind == KIND_OLLAMA:
        return sorted(str(m.get("name") or "") for m in (obj.get("models") or [])
                      if m.get("name"))
    return sorted(str(m.get("id") or "") for m in (obj.get("data") or [])
                  if m.get("id"))


def _batch_prompt(instruction: str, jobs: List[Dict[str, Any]]) -> str:
    lines = [
        instruction or ("You are responding to several independent jobs at "
                        "once. Answer EACH job separately and concisely."),
        "",
        "Reply with ONLY a JSON object of this exact shape:",
        '{"results": {"<job key>": "<answer for that job>"}}',
        "No prose outside the JSON. Keep every answer self-contained.",
        "",
    ]
    for job in jobs:
        lines.append("### job %s" % job.get("key"))
        lines.append(str(job.get("prompt") or "").strip())
        lines.append("")
    return "\n".join(lines)


def _parse_batch_reply(text: str, keys: List[str]) -> Optional[Dict[str, str]]:
    """Parse the batched answer. None = unusable, caller splits the batch."""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw).strip()
    obj: Any = None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict):
        return None
    results = obj.get("results")
    if not isinstance(results, dict):
        # Tolerate {"key": "answer"} shapes.
        results = {k: v for k, v in obj.items() if isinstance(v, str)}
    if not results:
        return None
    out: Dict[str, str] = {}
    for key in keys:
        val = results.get(key)
        if val is None:
            # Tolerate "job-1"/"job 1"/"1" style keys.
            for cand in (key.replace("job-", ""), key.replace("job_", ""),
                         key.split("-")[-1]):
                if cand in results:
                    val = results[cand]
                    break
        if val is None:
            continue
        out[key] = val if isinstance(val, str) else json.dumps(val)
    return out or None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def _specs_from_env(base: Dict[str, ProviderSpec]) -> Dict[str, ProviderSpec]:
    specs: Dict[str, ProviderSpec] = {}
    for name, spec in base.items():
        fields: Dict[str, Any] = {}
        for field, env_key in ENV_PROVIDER_KEYS.get(name, {}).items():
            val = os.environ.get(env_key, "")
            if val:
                fields[field] = val
        if name == "nim" and not fields.get("api_key"):
            for extra in ("NVIDIA_API_KEY", "NIM_API_KEY", "NEX_NIM_KEY"):
                if os.environ.get(extra):
                    fields["api_key"] = os.environ[extra]
                    break
        if name == "gpt" and not fields.get("api_key"):
            for extra in ("OPENAI_API_KEY", "NEX_OPENAI_API_KEY"):
                if os.environ.get(extra):
                    fields["api_key"] = os.environ[extra]
                    break
        specs[name] = ProviderSpec(name, **{
            **{f: getattr(spec, f) for f in ProviderSpec.FIELDS}, **fields})
    return specs


def _default_roles(specs: Dict[str, ProviderSpec]) -> Dict[str, Dict[str, str]]:
    planner_provider = os.environ.get("NEX_PLANNER_PROVIDER", "").strip()
    builder_provider = os.environ.get("NEX_BUILDER_PROVIDER", "").strip()
    if not planner_provider:
        planner_provider = "gpt" if (specs.get("gpt") and
                                     specs["gpt"].configured and
                                     specs["gpt"].key) else "local"
    if not builder_provider:
        builder_provider = "nim" if (specs.get("nim") and specs["nim"].key) \
            else "local"
    roles = {
        ROLE_PLANNER: {
            "provider": planner_provider,
            "model": os.environ.get("NEX_PLANNER_MODEL", "").strip() or
                     (specs[planner_provider].model if planner_provider in specs else ""),
            "fallbacks": _env_fallbacks(ROLE_PLANNER,
                                        ROLE_DEFAULT_FALLBACKS[ROLE_PLANNER]),
        },
        ROLE_BUILDER: {
            "provider": builder_provider,
            "model": os.environ.get("NEX_BUILDER_MODEL", "").strip() or
                     (specs[builder_provider].model if builder_provider in specs else ""),
            "fallbacks": _env_fallbacks(ROLE_BUILDER,
                                        ROLE_DEFAULT_FALLBACKS[ROLE_BUILDER]),
        },
    }
    return roles


def _env_fallbacks(role: str, default: List[str]) -> List[str]:
    """NEX_BUILDER_FALLBACKS / NEX_PLANNER_FALLBACKS override the explicit
    chain (comma-separated provider names)."""
    env = os.environ.get("NEX_%s_FALLBACKS" % role.upper(), "").strip()
    if not env:
        return list(default)
    return [n.strip() for n in env.split(",") if n.strip()]


def build_router(bus: Any = None, store: Optional[SettingsStore] = None,
                 load_dot_env: bool = True) -> Router:
    """Build the process-wide router from .env + settings + environment."""
    if load_dot_env:
        load_env()
    store = store or SettingsStore()
    stored = store.load()
    base = {name: ProviderSpec(name) for name in DEFAULT_PROVIDERS}
    for name, fields in (stored.get("providers") or {}).items():
        if name not in base:
            base[name] = ProviderSpec(name)
        if isinstance(fields, dict):
            base[name] = ProviderSpec(name, **{
                **{f: getattr(base[name], f) for f in ProviderSpec.FIELDS},
                **{k: v for k, v in fields.items()
                   if k in ProviderSpec.FIELDS and v is not None}})
    specs = _specs_from_env(base)
    for name, spec in specs.items():
        # An env/store key is bound to the endpoint that is in effect for it
        # right now. If a later settings push moves the endpoint, the key
        # stops being sent until the operator confirms the new host.
        if spec.raw_key and not spec.key_host:
            spec.bind_key()
    roles = _default_roles(specs)
    for role, cfg in (stored.get("roles") or {}).items():
        if role in ROLES and isinstance(cfg, dict) and cfg.get("provider") in specs:
            falls = cfg.get("fallbacks")
            if not isinstance(falls, list):
                falls = ROLE_DEFAULT_FALLBACKS.get(role, ["local"])
            roles[role] = {"provider": str(cfg["provider"]),
                           "model": str(cfg.get("model") or
                                        specs[str(cfg["provider"])].model),
                           "fallbacks": [str(f) for f in falls if str(f)]}
    router = Router(specs, roles, bus=bus)
    router._store_path = store.path
    router.resolve_states()
    return router
