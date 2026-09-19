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
        "model": "llama3.2",
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        resp_headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        try:
            return resp.status, json.loads(raw), resp_headers
        except json.JSONDecodeError:
            return resp.status, {"_raw": raw[:4000]}, resp_headers


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: float = 30.0) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
              "max_tokens", "batch_max", "note")

    def __init__(self, name: str, **kw: Any) -> None:
        base = dict(DEFAULT_PROVIDERS.get(name, {}))
        base.update({k: v for k, v in kw.items() if k in self.FIELDS})
        self.name = name
        self.label = str(base.get("label") or name)
        self.kind = str(base.get("kind") or KIND_OPENAI)
        self.base_url = str(base.get("base_url") or "").rstrip("/")
        self.model = str(base.get("model") or "")
        self.api_key = str(base.get("api_key") or "")
        self.api_key_env = str(base.get("api_key_env") or "")
        self.rpm = _as_int(base.get("rpm"), 0)
        self.timeout = _as_float(base.get("timeout"), 120.0)
        self.cooldown_s = _as_float(base.get("cooldown_s"), 20.0)
        self.temperature = _as_float(base.get("temperature"), 0.2)
        self.max_tokens = _as_int(base.get("max_tokens"), 0)
        self.batch_max = _as_int(base.get("batch_max"), 6)
        self.enabled = bool(base.get("enabled", True))
        self.note = str(base.get("note") or "")

    # -- key resolution ---------------------------------------------------
    @property
    def key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "").strip()
        return ""

    @property
    def configured(self) -> bool:
        """Ollama needs no key; OpenAI-compatible providers do."""
        if not self.enabled or not self.base_url:
            return False
        if self.kind == KIND_OPENAI and not self.key and not self._is_local_host():
            return False
        return True

    def _is_local_host(self) -> bool:
        return any(h in self.base_url for h in ("127.0.0.1", "localhost", "::1"))

    # -- urls -------------------------------------------------------------
    @property
    def chat_url(self) -> str:
        if self.kind == KIND_OLLAMA:
            return self.base_url + "/api/chat"
        return self.base_url + "/v1/chat/completions"

    @property
    def models_url(self) -> str:
        if self.kind == KIND_OLLAMA:
            return self.base_url + "/api/tags"
        return self.base_url + "/v1/models"

    def masked(self) -> Dict[str, Any]:
        return {
            "name": self.name, "label": self.label, "kind": self.kind,
            "base_url": self.base_url, "model": self.model,
            "api_key": mask_key(self.key),           # never the raw key
            "api_key_set": bool(self.key),
            "api_key_env": self.api_key_env,
            "rpm": self.rpm, "timeout": self.timeout,
            "cooldown_s": self.cooldown_s, "enabled": self.enabled,
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
        self.last_retry_after: Optional[float] = None

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
    def available(self) -> bool:
        if self.status == ST_DISABLED:
            return False
        if self.status == ST_NO_KEY:
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
            wait = retry_after if retry_after else 20.0
            self.cooldown_until = now + max(2.0, min(float(wait), 300.0))
        elif kind in (ERR_TIMEOUT, ERR_NETWORK):
            self.status = ST_COOLING
            self.cooldown_until = now + 5.0
        elif kind == ERR_SERVER:
            self.status = ST_COOLING
            self.cooldown_until = now + 10.0
        else:  # auth / bad request — do not hammer a broken config
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
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
            "total_calls": self.total_calls,
            "total_ok": self.total_ok,
            "total_failed": self.total_failed,
            "rate_limited": self.rate_limited,
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
        self.resolve_states()

    # -- configuration ----------------------------------------------------
    def resolve_states(self) -> None:
        for name, spec in self.specs.items():
            st = self.states.setdefault(name, ProviderState(name, spec.rpm,
                                                            self._clock))
            st.rpm = spec.rpm
            if not spec.enabled:
                st.status = ST_DISABLED
            elif not spec.configured:
                st.mark_missing_key()
            elif st.status in (ST_DISABLED, ST_NO_KEY):
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
        with self._lock:
            self.specs[name] = spec
            self.states[name] = ProviderState(name, spec.rpm, self._clock)
            self.resolve_states()
        return spec

    def set_role(self, role: str, provider: str,
                 model: Optional[str] = None) -> None:
        if role not in ROLES:
            return
        entry = dict(self.roles.get(role) or {})
        entry["provider"] = provider
        if model is not None:
            entry["model"] = model
        self.roles[role] = entry

    def role_model(self, role: str) -> str:
        entry = self.roles.get(role) or {}
        model = str(entry.get("model") or "").strip()
        if model:
            return model
        spec = self.specs.get(str(entry.get("provider") or ""))
        return spec.model if spec else ""

    def chain(self, role: str) -> List[str]:
        """Ordered provider names for a role: primary -> fallbacks.

        Builder:  NIM -> planner provider (GPT) -> local -> anything else usable
        Planner:  planner provider     -> NIM        -> local -> anything else
        Local always sits late but present: it is the provider that cannot be
        rate-limited or lose a key.
        """
        entry = self.roles.get(role) or {}
        primary = str(entry.get("provider") or "")
        other_role = ROLE_BUILDER if role == ROLE_PLANNER else ROLE_PLANNER
        secondary = str((self.roles.get(other_role) or {}).get("provider") or "")
        order: List[str] = []
        for name in (primary, secondary, "local"):
            if name and name in self.specs and name not in order:
                order.append(name)
        for name in self.specs:
            if name not in order and name != "local":
                order.append(name)
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
    def _call(self, spec: ProviderSpec, messages: List[Dict[str, str]],
              temperature: Optional[float] = None,
              max_tokens: Optional[int] = None,
              timeout: Optional[float] = None) -> str:
        """One provider call. Raises ProviderError (never returns garbage)."""
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
        text = _extract_text(obj, spec.kind)
        if not text:
            raise ProviderError(ERR_PARSE, "empty completion from provider "
                                           "(model=%s)" % (spec.model or "?"))
        return text

    # -- public API -------------------------------------------------------
    def chat(self, role: str, messages: List[Dict[str, str]],
             purpose: str = "", temperature: Optional[float] = None,
             max_tokens: Optional[int] = None,
             timeout: Optional[float] = None) -> str:
        """Route a chat call for `role`. Raises AllProvidersFailed if the
        whole chain is down (callers degrade honestly instead of guessing)."""
        chain = self.chain(role)
        attempts: List[Dict[str, Any]] = []
        primary = chain[0] if chain else ""
        for name in chain:
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
            try:
                text = self._call(spec, messages, temperature, max_tokens, timeout)
            except ProviderError as exc:
                state.mark_error(exc.kind, str(exc), exc.retry_after)
                attempts.append({"provider": name, "error": exc.kind,
                                 "detail": str(exc)[:200]})
                self._emit({
                    "type": "provider.status", "provider": name,
                    "role": role, "status": state.status,
                    "error_kind": exc.kind, "error": str(exc)[:200],
                    "detail": self._detail_line(), "purpose": purpose,
                    "roles": self.role_snapshot(),
                })
                continue
            state.mark_ok()
            state.total_ok += 1
            switched = self._last_served.get(role) not in (None, name)
            self._last_served[role] = name
            if name != primary:
                # The plan does NOT change — only the hands.
                self._emit({"type": "provider.fallback", "role": role,
                            "from": primary, "to": name,
                            "purpose": purpose, "plan_continues": True,
                            "roles": self.role_snapshot()})
            else:
                self._emit({"type": "provider.call", "role": role,
                            "provider": name, "model": self.role_model(role),
                            "purpose": purpose, "recovered": switched,
                            "roles": self.role_snapshot()})
            return text
        raise AllProvidersFailed(role, attempts)

    def chat_batch(self, role: str, jobs: List[Dict[str, Any]],
                   instruction: str = "",
                   temperature: Optional[float] = None) -> Dict[str, str]:
        """ONE call for N independent jobs — the cheap way to spend NIM's RPM.

        jobs: [{"key": "task-1", "prompt": "..."}]. Returns {key: text}.
        Falls back to one call per job when the batch answer is unusable, so
        batching can never lose work: it can only cost a retry.
        """
        jobs = [j for j in (jobs or []) if str(j.get("prompt") or "").strip()]
        if not jobs:
            return {}
        if len(jobs) == 1:
            key = str(jobs[0].get("key"))
            return {key: self.chat(role, [{"role": "user",
                                           "content": str(jobs[0]["prompt"])}],
                                   purpose="batch-1", temperature=temperature)}
        size = len(jobs)
        for spec in self.specs.values():
            if spec.rpm:
                size = min(size, max(1, spec.batch_max))
                break
        chunks = [jobs[i:i + size] for i in range(0, len(jobs), size)]
        out: Dict[str, str] = {}
        for chunk in chunks:
            keys = [str(j.get("key")) for j in chunk]
            body = _batch_prompt(instruction, chunk)
            text = ""
            try:
                text = self.chat(role, [{"role": "user", "content": body}],
                                 purpose="batch-%d" % len(chunk),
                                 temperature=temperature)
            except AllProvidersFailed:
                text = ""
            parsed = _parse_batch_reply(text, keys)
            if parsed is None:
                # One provider call per job — correctness over quota.
                for job in chunk:
                    k = str(job.get("key"))
                    try:
                        out[k] = self.chat(role, [{"role": "user", "content":
                                                   str(job["prompt"])}],
                                           purpose="batch-split")
                    except AllProvidersFailed:
                        continue
                continue
            out.update(parsed)
        return out

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
            out[role] = {
                "provider": str(entry.get("provider") or ""),
                "model": self.role_model(role),
                "active": active or "",
                "serving": serving or "",
                "fallback": bool(serving and serving != str(entry.get("provider") or "")),
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
            "providers": {name: spec.masked() for name, spec in self.specs.items()},
            "roles": {role: dict(self.roles.get(role) or {}) for role in ROLES},
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
        """
        store = store or SettingsStore()
        stored = store.load()
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
                        clean["api_key"] = ""
                        continue
                    clean["api_key"] = str(v).strip()
                    continue
                clean[k] = v
            # Persist (raw key only in the 0600 store, never in responses).
            entry = dict(stored.get("providers", {}).get(name) or {})
            entry.update(clean)
            stored.setdefault("providers", {})[name] = entry
            self.set_spec(name, **clean)
        for role, cfg in dict(patch.get("roles") or {}).items():
            if role not in ROLES or not isinstance(cfg, dict):
                continue
            provider = str(cfg.get("provider") or "").strip()
            model = cfg.get("model")
            if provider:
                self.set_role(role, provider,
                              None if model is None else str(model))
                stored.setdefault("roles", {})[role] = {
                    "provider": provider,
                    "model": self.roles[role].get("model", ""),
                }
        if "roles" in patch or "providers" in patch:
            store.save(stored)
        self.resolve_states()
        view = self.settings_view()
        self._emit({"type": "provider.status", "note": "settings applied",
                    "roles": self.role_snapshot()})
        return view


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_text(obj: Dict[str, Any], kind: str) -> str:
    if kind == KIND_OLLAMA:
        msg = obj.get("message") or {}
        return str(msg.get("content") or "").strip()
    choices = obj.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    text = str(msg.get("content") or "").strip()
    if not text:
        # Reasoning models may put everything into reasoning_content when
        # max_tokens cut the answer short — better that than an empty string.
        text = str(msg.get("reasoning_content") or "").strip()
    return text


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
        },
        ROLE_BUILDER: {
            "provider": builder_provider,
            "model": os.environ.get("NEX_BUILDER_MODEL", "").strip() or
                     (specs[builder_provider].model if builder_provider in specs else ""),
        },
    }
    return roles


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
    roles = _default_roles(specs)
    for role, cfg in (stored.get("roles") or {}).items():
        if role in ROLES and isinstance(cfg, dict) and cfg.get("provider") in specs:
            roles[role] = {"provider": str(cfg["provider"]),
                           "model": str(cfg.get("model") or
                                        specs[str(cfg["provider"])].model)}
    router = Router(specs, roles, bus=bus)
    router._store_path = store.path
    router.resolve_states()
    return router
