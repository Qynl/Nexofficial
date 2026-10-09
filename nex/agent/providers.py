"""Provider layer — local routine work, cloud heavy work, clean hand-offs.

The architecture implemented here has two operator-controlled lanes:

  * ROUTINE (the ``chat`` route, local Ollama by default): conversation,
    summaries, and small plans. These jobs do not spend hosted quota while the
    local model is healthy.
  * HARD WORK (the ``agent`` route): complex production plans, evaluations,
    failure diagnosis, and bounded batch jobs. NVIDIA NIM is preferred, GPT is
    an explicit credentialed fallback, then four FREE no-card gateways
    (OpenCode Zen, OpenRouter, Groq, Google AI Studio/Gemini) take over, and
    Ollama is always the terminal fallback. Any provider without a key (or
    without a key for THIS lane) is simply skipped — nothing in the chain
    ever blocks on a missing credential. The more of those free gateways
    have a key, the rarer a real build ever touches local compute.

Why a router instead of one model call: hosted NIM limits vary by model,
endpoint, account and current service load. Nex therefore ships a configurable
40-RPM LOCAL safety ceiling rather than pretending that number is NVIDIA's
contract. Routine work stays local, evaluations are checkpointed, output-token
budgets are purpose-specific, and scarce requests are reserved for complex
plans and repairs. Failover is explicit:

    NIM 429 / timeout / 5xx / RPM exhausted
        -> the SAME model job goes to GPT when configured
        -> otherwise the free OpenCode Zen / OpenRouter / Groq / Google
           gateways take it, in that order, for whichever have a key
        -> otherwise Ollama runs it locally
        -> the validated MCP plan is untouched: only the model changes
        -> after cooldown the router hands hard work back to NIM automatically

OpenCode Zen (https://opencode.ai/zen/v1), OpenRouter
(https://openrouter.ai/api/v1), Groq (https://api.groq.com/openai/v1), and
Google AI Studio/Gemini (https://generativelanguage.googleapis.com/v1beta/openai)
are all plain OpenAI-compatible chat/completions endpoints, so they need no
special-case code — they are DEFAULT_PROVIDERS entries like any other, free
only because the account behind the key is free. Free listings can change or
retire; Settings → Model always reads the LIVE ``/v1/models`` catalog from
the configured key, which is the authority — the CATALOG below is only a
curated starting point.

Nothing here executes a tool. A provider returns TEXT; the agent loop turns
that text into validated MCP calls. The agent therefore cannot reach the
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
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

ROLE_CHAT = "chat"
ROLE_AGENT = "agent"
ROLES = (ROLE_CHAT, ROLE_AGENT)

# Legacy 1.x role names → 2.x (settings-file migration).
_ROLE_MIGRATION = {"planner": ROLE_CHAT, "builder": ROLE_AGENT}

# EXPLICIT fallback lists. Nothing enters a chain implicitly: a provider has
# to be named here (or by the operator) to be tried, so a future provider
# added to the catalog can never silently become a fallback for a role.
#
# "opencode" (OpenCode Zen), "openrouter", "groq", and "google" are all FREE,
# no-card cloud gateways — they cost the operator nothing even without
# NIM/GPT credentials, so all four sit between the paid clouds and the
# guaranteed local model: a build keeps getting real hosted quality instead
# of dropping straight to Ollama the moment NIM/GPT are absent or rate
# limited. Order among the four free gateways follows how well-suited each
# is to agentic/tool-calling work and how generous its free tier is today
# (opencode/openrouter lead with the same strong $0 agentic model; groq adds
# very low latency; google adds the highest-volume general safety net).
ROLE_DEFAULT_FALLBACKS: Dict[str, List[str]] = {
    # Hard work: NIM -> configured GPT -> free gateways -> local, with local
    # guaranteed by Router.chain even if an operator shortens this list.
    ROLE_AGENT: ["gpt", "opencode", "openrouter", "groq", "google", "local"],
    # Routine work starts locally; the free gateways are tried before any
    # paid one, and NIM's scarce hard-work budget is the last resort.
    ROLE_CHAT: ["opencode", "openrouter", "groq", "google", "gpt", "nim"],
}

KIND_OLLAMA = "ollama"    # /api/chat, /api/tags
KIND_OPENAI = "openai"    # /v1/chat/completions, /v1/models (NIM, OpenAI, …)

# Best-effort, conservative name patterns for models documented to accept
# image input alongside text. There is no universal registry of this —
# operators can point any role at any model string — so this is inferred
# from publicly known multimodal model families, never assumed. An
# unrecognised name is treated as text-only: the safe default, because
# sending an image to a model that cannot read it would not fail loudly,
# it would just produce confident-sounding prose about pixels it never
# saw, which is worse than honestly skipping the critique.
_VISION_MODEL_PATTERNS = (
    "gemini", "gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4-mini",
    "claude-3", "claude-opus", "claude-sonnet", "claude-haiku",
    "llava", "bakllava", "pixtral", "qwen2-vl", "qwen2.5-vl", "qwen-vl",
    "llama-4-scout", "llama-4-maverick", "grok-4", "grok-vision",
    "phi-3.5-vision", "phi-4-multimodal", "moondream", "internvl",
    # Kimi: only the generations Moonshot AI has actually documented as
    # multimodal (K2 / K2 Thinking / K2.5 / K2.7 Code are text-only) —
    # "kimi-k2" bare is deliberately NOT matched here.
    "kimi-k3", "kimi-k2.6",
)


def model_supports_vision(model: str) -> bool:
    """Best-effort guess at whether `model` accepts image input.

    See `_VISION_MODEL_PATTERNS` for the rationale: false is always the
    safe default for an unrecognised model name.
    """
    name = str(model or "").lower()
    return any(pat in name for pat in _VISION_MODEL_PATTERNS)


def _attach_images(messages: List[Dict[str, Any]], kind: str,
                   images: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Return a NEW messages list with `images` attached to the last
    message, formatted the way `kind`'s API expects.

    Never mutates the input list or its dicts — the same `messages` may
    still be tried against another provider of a different `kind` if this
    one turns out to be unavailable.
    """
    if not messages or not images:
        return messages
    out = [dict(m) for m in messages]
    last = dict(out[-1])
    if kind == KIND_OLLAMA:
        # Ollama's /api/chat takes raw base64 strings in a sibling
        # "images" field on the message, not inline in "content".
        payload = [img["data"] for img in images
                  if isinstance(img, dict) and img.get("data")]
        if payload:
            last["images"] = payload
    else:
        # OpenAI-compatible chat completions: "content" becomes a list of
        # typed blocks when any non-text content is present.
        text = last.get("content", "")
        blocks: List[Dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for img in images:
            if not isinstance(img, dict):
                continue
            data = img.get("data")
            if not data:
                continue
            mime = img.get("mime_type") or "image/png"
            blocks.append({"type": "image_url",
                           "image_url": {"url": "data:%s;base64,%s"
                                         % (mime, data)}})
        if blocks:
            last["content"] = blocks
    out[-1] = last
    return out


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

# Default backoff for a "blind" 429 — a rate limit our own sliding-window
# accounting did not predict and the provider did not explain with a
# Retry-After header. Almost every provider's "requests per minute" quota
# resets on a one-minute cadence, so this is the honest assumption instead of
# a token retry that would just get hammered by the same 429 again.
_BLIND_RATE_LIMIT_BACKOFF_S = 60.0

# How fast a cooldown grows when the SAME failure keeps recurring back to
# back (consecutive 5xx/timeout/network hits, or consecutive blind 429s).
# One blip is noise; three in a row is a real outage that deserves more
# breathing room than hammering every few seconds would give it.
_BACKOFF_MULTIPLIER = 1.6

# Absolute ceiling for any single cooldown, regardless of how far the
# escalation above has climbed — a provider is always retried within two
# minutes, never parked indefinitely by this mechanism.
_MAX_COOLDOWN_S = 120.0


def _jittered(wait: float, spread: float = 0.1) -> float:
    """Pad `wait` by 0-`spread` extra, never less. Keeps providers from all
    coming back at the EXACT same instant (thundering herd) when several
    workers/processes share a key and hit the same outage or quota wall at
    once — a few hundred ms to a few seconds of spread costs nothing here
    but avoids a synchronized retry storm. Never used on top of a provider's
    own Retry-After: that figure is a promise, not a guess, and jittering it
    would risk looking like Nex ignored it.
    """
    if wait <= 0:
        return wait
    return wait + random.uniform(0.0, wait * spread)


# Machine-consumed agent jobs benefit from deterministic JSON. These purpose
# names are attached by the loop (not guessed from user text) and are visible
# in provider telemetry. Providers may opt out when a model lacks JSON mode.
STRUCTURED_PURPOSES = frozenset({
    "planning", "planning-routine", "planning-hard", "evaluation",
    "diagnosis", "batch",
})
ROUTINE_PURPOSES = frozenset({
    "chat", "summary", "planning-routine",
})
HIGH_PRIORITY_PURPOSES = frozenset({
    "planning-hard", "diagnosis",
})
_PURPOSE_TEMPERATURES = {
    "planning": 0.1,
    "planning-routine": 0.1,
    "planning-hard": 0.1,
    "evaluation": 0.0,
    "diagnosis": 0.0,
    "summary": 0.2,
}
# Output ceilings are intentionally much smaller than a provider's generic
# maximum for compact machine jobs. An explicit caller limit still wins.
PURPOSE_MAX_TOKENS: Dict[str, int] = {
    "chat": 1800,
    "summary": 700,
    "planning-routine": 1800,
    "planning-hard": 3200,
    "planning": 2600,       # backwards-compatible injected callers
    "evaluation": 550,
    "diagnosis": 850,
    "batch": 2200,
}
WORKLOAD_POLICY = {
    "routine": {
        "role": ROLE_CHAT,
        "purposes": sorted(ROUTINE_PURPOSES),
        "description": "Ollama-first: chat, summaries and small plans",
    },
    "hard": {
        "role": ROLE_AGENT,
        "purposes": ["planning-hard", "evaluation", "diagnosis", "batch-*"],
        "description": "NIM-first, then configured GPT, then free "
                       "OpenCode Zen / OpenRouter, then Ollama",
    },
    "token_limits": dict(PURPOSE_MAX_TOKENS),
}
_MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_PROVIDER_STREAM_BYTES = 16 * 1024 * 1024
_MAX_PROVIDER_STREAM_LINE_BYTES = 1024 * 1024


def _structured_purpose(purpose: str) -> bool:
    return purpose in STRUCTURED_PURPOSES or purpose.startswith("batch-")


def role_for_purpose(purpose: str, default: str = ROLE_AGENT) -> str:
    """Map trusted internal jobs onto the routine or hard-work lane."""
    return ROLE_CHAT if purpose in ROUTINE_PURPOSES else default


def purpose_token_limit(purpose: str, provider_limit: int = 0) -> int:
    """Bound output tokens by job while respecting a tighter provider cap."""
    key = "batch" if purpose.startswith("batch-") else purpose
    job_limit = PURPOSE_MAX_TOKENS.get(key, 0)
    if provider_limit and job_limit:
        return min(provider_limit, job_limit)
    return provider_limit or job_limit


def _safe_usage(value: Any) -> Dict[str, int]:
    """Keep only bounded numeric counters from untrusted provider metadata."""
    raw = value if isinstance(value, dict) else {}
    out: Dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                "reasoning_tokens"):
        if key in raw:
            out[key] = max(0, min(_as_int(raw.get(key), 0), 10 ** 9))
    return out


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
        "structured_outputs": False,
        "note": "Routine lane: private chat, summaries and small plans; terminal fallback for hard work.",
    },
    "nim": {
        "label": "NVIDIA NIM",
        "kind": KIND_OPENAI,
        "base_url": "https://integrate.api.nvidia.com/v1",
        # Kimi K3 (reviewed 2026-10 against the live NIM catalog): a
        # flagship, free-endpoint, 1M-context, NATIVELY MULTIMODAL model
        # (text + image input) — it is strong agentic/coding work AND the
        # thing that makes the real-screenshot AI critique (see
        # agent/loop.py _maybe_visual_critique) actually fire for anyone
        # with just an NVIDIA key configured, instead of only for
        # operators who also happen to have a GPT/Gemini key. Nemotron 3
        # Super (the previous default, text-only) is still in CATALOG as
        # a fast, NVIDIA-native alternative.
        "model": "moonshotai/kimi-k3",
        "api_key_env": "NVIDIA_API_KEY",
        "rpm": 40,            # Nex safety default; not a promised NIM quota
        "timeout": 240.0,
        "cooldown_s": 20.0,
        "structured_outputs": True,
        "note": "Hard-work primary. Purpose-aware JSON, quota pacing, "
                "GPT/Ollama hand-off. Default model (Kimi K3) is vision-"
                "capable, so screenshot critique works out of the box "
                "with just an NVIDIA key — no separate vision provider "
                "needed.",
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
        "structured_outputs": True,
        "note": "Credentialed hard-work fallback after NIM; skipped cleanly when no key is set.",
    },
    "opencode": {
        "label": "OpenCode Zen",
        "kind": KIND_OPENAI,
        "base_url": "https://opencode.ai/zen/v1",
        # Ling 3.1 Flash (reviewed 2026-10 against the live opencode.ai/
        # zen/v1 catalog): a named, stable model — 560B total/25B active,
        # 262K context — free directly on Zen (also OpenRouter's own
        # default below, so both free gateways lead with the same proven
        # agentic model). Big Pickle tested stronger on raw coding
        # benchmarks by community report, but it is OpenCode's rotating/
        # anonymous stealth-eval slot: the identity behind it (and
        # therefore its real quality and whether it can see images) can
        # change week to week with no warning. For a default that ships
        # to everyone, predictability wins over chasing the top score —
        # Big Pickle remains in CATALOG for anyone who wants to opt into
        # it anyway.
        "model": "ling-3.1-flash-free",
        "api_key_env": "OPENCODE_API_KEY",
        # No published per-model RPM; a conservative local ceiling avoids
        # hammering a free, no-card gateway into a hard 429/ban.
        "rpm": 20,
        "timeout": 180.0,
        "cooldown_s": 15.0,
        "structured_outputs": False,
        "note": "Free, no-card gateway (opencode.ai/auth). Several models are "
                "$0/token today — Ling 3.1 Flash (default, named and "
                "stable), Nemotron 3 Ultra/Lightning, Ling 3.0 Flash Fin, "
                "Big Pickle (rotating identity, unverifiable quality/"
                "vision) — but a free listing can change or retire; "
                "Settings → Model pulls the live catalog.",
    },

    "openrouter": {
        "label": "OpenRouter",
        "kind": KIND_OPENAI,
        "base_url": "https://openrouter.ai/api/v1",
        "model": "inclusionai/ling-3.1-flash",
        "api_key_env": "OPENROUTER_API_KEY",
        # Published free-tier default: 20 req/min, 50/day (1,000/day once the
        # account has bought $10 of credit). This is a local safety ceiling,
        # not a guarantee of OpenRouter's current policy.
        "rpm": 20,
        "timeout": 180.0,
        "cooldown_s": 20.0,
        "structured_outputs": False,
        "note": "Free, no-card aggregator (20+ ':free' models, one key). "
                "Default model is Ling 3.1 Flash (inclusionAI, 560B-MoE, "
                "free, 262K context) — widest single net for 'try another "
                "free cloud model' once NIM/GPT/OpenCode are all down.",
    },
    "groq": {
        "label": "Groq",
        "kind": KIND_OPENAI,
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "api_key_env": "GROQ_API_KEY",
        # Groq publishes per-model free-tier limits rather than one shared
        # number; llama-3.3-70b-versatile is 30 RPM / 1,000 RPD (reviewed
        # 2026-10) — this is Nex's local safety ceiling, not a guarantee.
        "rpm": 30,
        "timeout": 120.0,
        "cooldown_s": 15.0,
        "structured_outputs": True,
        "note": "Free, no-card gateway (console.groq.com/keys). Custom LPU "
                "hardware — very low latency, good for tight agent loops. "
                "Daily/token caps are tighter than the RPM number suggests "
                "on the free tier; Settings → Model pulls the live catalog.",
    },
    "google": {
        "label": "Google AI Studio (Gemini)",
        "kind": KIND_OPENAI,
        # Google's OpenAI-compatibility shim names its own version segment
        # "openai" instead of "v1" and never wants a separate "/v1" appended
        # — see ProviderSpec._endpoint(), which special-cases this ending.
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "api_key_env": "GEMINI_API_KEY",
        # Google cut free-tier limits significantly in late 2025; Gemini 2.5
        # Flash is commonly reported between 10 and 15 RPM / ~1,500 RPD
        # today (reviewed 2026-10). Conservative local ceiling, not a
        # guarantee — aistudio.google.com shows the account's real numbers.
        "rpm": 10,
        "timeout": 180.0,
        "cooldown_s": 20.0,
        "structured_outputs": False,
        "note": "Free, no-card gateway (aistudio.google.com/app/apikey). "
                "Highest raw free quality/volume of the four free gateways "
                "today, 1M-token context on Flash — the widest general "
                "safety net before local compute. Settings → Model pulls "
                "the live catalog.",
    },
}

# Curated NIM starting points (reviewed 2026-10; the live /v1/models list in
# Settings is authoritative). role hint = which job the model may fit.
CATALOG: List[Dict[str, Any]] = [
    # --- NVIDIA-native (best throughput on NIM, strong tool calling) -------
    {"provider": "nim", "id": "nvidia/nemotron-3-super-120b-a12b",
     "role": "agent", "label": "Nemotron 3 Super 120B",
     "note": "Agentic workhorse: 1M context, planning and tool calling. "
             "The previous default — still a fast, NVIDIA-native choice; "
             "text-only, so screenshot critique needs a different model."},
    {"provider": "nim", "id": "nvidia/nemotron-3.5-lightning-30b-a3b",
     "role": "agent", "label": "Nemotron 3.5 Lightning 30B",
     "note": "Fast 3B-active long-running-agent model with 1M context."},
    {"provider": "nim", "id": "nvidia/nemotron-3-ultra-550b-a55b",
     "role": "chat", "label": "Nemotron 3 Ultra 550B",
     "note": "Frontier reasoning/coding, slower — good for planning."},
    {"provider": "nim", "id": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
     "role": "agent", "label": "Llama 3.3 Nemotron Super 49B",
     "note": "Fastest native option, built for function calling."},
    {"provider": "nim", "id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
     "role": "agent", "label": "Nemotron 3 Nano Omni 30B",
     "note": "Cheap small model — good for batched, mechanical steps."},
    # --- Third-party on NIM ------------------------------------------------
    {"provider": "nim", "id": "moonshotai/kimi-k3",
     "role": "agent", "label": "Kimi K3",
     "note": "The provider default. Long-horizon multimodal coding and "
             "agentic tool use, 1M context, native vision — the model "
             "that makes real-screenshot AI critique work with just an "
             "NVIDIA key."},
    {"provider": "nim", "id": "zhipuai/glm-5.1",
     "role": "agent", "label": "GLM-5.1",
     "note": "Function calling + long-context coding."},
    {"provider": "nim", "id": "deepseek-ai/deepseek-v4-flash",
     "role": "agent", "label": "DeepSeek V4 Flash",
     "note": "Fast and cheap — batch work, small repairs."},
    {"provider": "nim", "id": "deepseek-ai/deepseek-v4-pro",
     "role": "chat", "label": "DeepSeek V4 Pro",
     "note": "Strongest coding scores, but slow (not for tight loops)."},
    {"provider": "nim", "id": "qwen/qwen3-coder-480b-a35b-instruct",
     "role": "agent", "label": "Qwen3 Coder 480B",
     "note": "Purpose-built for agentic coding."},
    {"provider": "nim", "id": "mistralai/devstral-2-123b-instruct-2512",
     "role": "agent", "label": "Devstral 2 123B",
     "note": "Dev-focused, fast tool calling."},
    {"provider": "nim", "id": "mistralai/mistral-nemotron",
     "role": "agent", "label": "Mistral Nemotron",
     "note": "Built for agentic workflows / function calling."},
    {"provider": "nim", "id": "minimaxai/minimax-m2.7",
     "role": "agent", "label": "MiniMax M2.7",
     "note": "Strong all-rounder, high latency."},
    {"provider": "nim", "id": "meta/llama-4-maverick-17b-128e-instruct",
     "role": "agent", "label": "Llama 4 Maverick",
     "note": "Popular general purpose model."},
    {"provider": "nim", "id": "google/gemma-4-31b-it",
     "role": "agent", "label": "Gemma 4 31B",
     "note": "Dense coding/reasoning model with structured-output support."},
    # --- Chat side ------------------------------------------------------
    {"provider": "gpt", "id": "gpt-5.1", "role": "chat",
     "label": "GPT-5.1", "note": "Flagship coding/agentic chat."},
    {"provider": "gpt", "id": "gpt-5-mini", "role": "chat",
     "label": "GPT-5 mini", "note": "Cheaper planning."},
    {"provider": "gpt", "id": "gpt-5.2", "role": "chat",
     "label": "GPT-5.2", "note": "Reasoning effort configurable."},
    {"provider": "gpt", "id": "gpt-5.6-terra", "role": "chat",
     "label": "GPT-5.6 Terra", "note": "Production generalist."},
    # --- OpenCode Zen (free, no card — reviewed 2026-10) -----------------
    # Only the chat/completions-protocol models are listed: Zen also serves
    # GPT/Claude/Gemini/Qwen through Responses/Messages/native endpoints this
    # router does not speak, so those are left off even when free.
    {"provider": "opencode", "id": "ling-3.1-flash-free",
     "role": "agent", "label": "Ling 3.1 Flash (OpenCode Zen, free)",
     "note": "The provider default. inclusionAI MoE, 560B total/25B active, "
             "262K context, free directly on Zen (previously OpenRouter-"
             "only) — same strong, $0 agentic model as OpenRouter's own "
             "default. Named and stable, chosen over the rotating Big "
             "Pickle slot because a default that ships to everyone should "
             "not have its real identity (and vision support) change "
             "without warning."},
    {"provider": "opencode", "id": "nemotron-3-ultra-free",
     "role": "agent", "label": "Nemotron 3 Ultra (OpenCode Zen, free)",
     "note": "Same 550B Nemotron Ultra family NIM charges for — free here. "
             "Slow; good for a careful plan, not a tight loop."},
    {"provider": "opencode", "id": "nemotron-3.5-lightning-free",
     "role": "agent", "label": "Nemotron 3.5 Lightning (OpenCode Zen, free)",
     "note": "Free mirror of NIM's fast Lightning tier — low-latency agent "
             "loops when NIM's own Lightning is rate limited."},
    {"provider": "opencode", "id": "ling-3.0-flash-fin-free",
     "role": "agent", "label": "Ling 3.0 Flash Fin (OpenCode Zen, free)",
     "note": "Ant Group / inclusionAI MoE flash model, free on Zen. An "
             "older sibling of the default Ling 3.1 Flash above — kept as "
             "an alternate in case 3.1's free listing ever rotates off."},
    {"provider": "opencode", "id": "mimo-v2.6-flash-free",
     "role": "chat", "label": "MiMo V2.6 Flash (OpenCode Zen, free)",
     "note": "Xiaomi general-purpose model; free coding/chat generalist. "
             "Supersedes the retired MiMo V2.5 free listing."},
    {"provider": "opencode", "id": "big-pickle",
     "role": "chat", "label": "Big Pickle (OpenCode Zen, free)",
     "note": "OpenCode's rotating stealth eval slot — reported by the "
             "community as the strongest free-tier coding/agentic "
             "performer on Zen, but its identity (and therefore its real "
             "quality and whether it can see images) varies by the week "
             "since it is whatever they are currently measuring. Opt in "
             "deliberately if you want to chase the top score; the "
             "provider defaults to the named, stable Ling 3.1 Flash above "
             "instead."},
    # --- OpenRouter (free, no card — 20+ ':free' models, reviewed 2026-10) -
    {"provider": "openrouter", "id": "inclusionai/ling-3.1-flash",
     "role": "agent", "label": "Ling 3.1 Flash (OpenRouter, free)",
     "note": "inclusionAI MoE, 560B total/25B active, 262K context, free. "
             "Strong agentic/tool-calling scores for a $0 model."},
    {"provider": "openrouter", "id": "deepseek/deepseek-chat-v3.1:free",
     "role": "agent", "label": "DeepSeek V3.1 (OpenRouter, free)",
     "note": "Free tier of DeepSeek's chat/coding model via OpenRouter."},
    {"provider": "openrouter", "id": "qwen/qwen3-coder:free",
     "role": "agent", "label": "Qwen3 Coder (OpenRouter, free)",
     "note": "Free agentic-coding model — tool calling tuned."},
    {"provider": "openrouter", "id": "meta-llama/llama-3.3-70b-instruct:free",
     "role": "chat", "label": "Llama 3.3 70B (OpenRouter, free)",
     "note": "Reliable free generalist; good baseline for chat/summaries."},
    {"provider": "openrouter", "id": "google/gemini-2.5-flash-lite:free",
     "role": "chat", "label": "Gemini 2.5 Flash Lite (OpenRouter, free)",
     "note": "Fast, cheap-quality free chat/summary model."},
    # --- Groq (free, no card — custom LPU hardware, reviewed 2026-10) ------
    {"provider": "groq", "id": "llama-3.3-70b-versatile",
     "role": "agent", "label": "Llama 3.3 70B Versatile (Groq, free)",
     "note": "The provider default. Extremely low latency on Groq's LPUs — "
             "good for tight agent loops; free-tier daily/token caps are "
             "tighter than the 30 RPM number alone suggests."},
    {"provider": "groq", "id": "openai/gpt-oss-120b",
     "role": "agent", "label": "GPT-OSS 120B (Groq, free)",
     "note": "Open-weight OpenAI model served on Groq; strong general "
             "agentic/coding performance, free tier."},
    {"provider": "groq", "id": "qwen/qwen3-32b",
     "role": "chat", "label": "Qwen3 32B (Groq, free)",
     "note": "Fast free chat/summary model; higher RPM than the 70B model "
             "but a smaller token-per-minute budget."},
    {"provider": "groq", "id": "moonshotai/kimi-k2-instruct",
     "role": "agent", "label": "Kimi K2 (Groq, free)",
     "note": "Long-horizon agentic/coding model, free tier on Groq."},
    # --- Google AI Studio / Gemini (free, no card, reviewed 2026-10) -------
    {"provider": "google", "id": "gemini-2.5-flash",
     "role": "agent", "label": "Gemini 2.5 Flash (Google, free)",
     "note": "The provider default. 1M-token context, hybrid reasoning, "
             "strong free-tier volume — the widest general safety net of "
             "the four free gateways before local compute."},
    {"provider": "google", "id": "gemini-2.5-flash-lite",
     "role": "chat", "label": "Gemini 2.5 Flash Lite (Google, free)",
     "note": "Higher free-tier RPM/RPD than full Flash; good for routine "
             "chat/summary load."},
    {"provider": "google", "id": "gemini-2.5-pro",
     "role": "chat", "label": "Gemini 2.5 Pro (Google, free)",
     "note": "Strongest free Gemini for careful planning; much lower free "
             "RPM/RPD than Flash — not for a tight loop."},
]

# Env -> provider field overrides (kept small and explicit).
ENV_PROVIDER_KEYS: Dict[str, Dict[str, str]] = {
    "local": {"base_url": "OLLAMA_HOST", "model": "OLLAMA_MODEL"},
    "nim": {"base_url": "NEX_NIM_BASE_URL", "model": "NEX_NIM_MODEL",
            "api_key": "NEX_NIM_API_KEY", "rpm": "NEX_NIM_RPM"},
    "gpt": {"base_url": "NEX_OPENAI_BASE_URL", "model": "NEX_OPENAI_MODEL",
            "api_key": "NEX_OPENAI_API_KEY"},
    "opencode": {"base_url": "NEX_OPENCODE_BASE_URL",
                 "model": "NEX_OPENCODE_MODEL",
                 "api_key": "NEX_OPENCODE_API_KEY",
                 "rpm": "NEX_OPENCODE_RPM"},
    "openrouter": {"base_url": "NEX_OPENROUTER_BASE_URL",
                   "model": "NEX_OPENROUTER_MODEL",
                   "api_key": "NEX_OPENROUTER_API_KEY",
                   "rpm": "NEX_OPENROUTER_RPM"},
    "groq": {"base_url": "NEX_GROQ_BASE_URL", "model": "NEX_GROQ_MODEL",
             "api_key": "NEX_GROQ_API_KEY", "rpm": "NEX_GROQ_RPM"},
    "google": {"base_url": "NEX_GOOGLE_BASE_URL", "model": "NEX_GOOGLE_MODEL",
               "api_key": "NEX_GOOGLE_API_KEY", "rpm": "NEX_GOOGLE_RPM"},
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
        payload = resp.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise OSError("provider response exceeded the 8 MiB safety limit")
        raw = payload.decode("utf-8", "replace")
        resp_headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        try:
            return resp.status, json.loads(raw), resp_headers
        except json.JSONDecodeError:
            return resp.status, {"_raw": raw[:4000]}, resp_headers


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: float = 30.0) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with _OPENER.open(req, timeout=timeout) as resp:
        payload = resp.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise OSError("provider response exceeded the 8 MiB safety limit")
        raw = payload.decode("utf-8", "replace")
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


def _retry_after_seconds(value: Any) -> Optional[float]:
    """Parse Retry-After seconds or an RFC 7231 HTTP date."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _classify_http_error(exc: urllib.error.HTTPError) -> ProviderError:
    status = getattr(exc, "code", 0) or 0
    retry_after = None
    try:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        retry_after = _retry_after_seconds(ra)
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
              "reserve", "min_interval_s", "max_chill_s",
              "structured_outputs")

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
        self.structured_outputs = bool(base.get("structured_outputs", False))
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

        Google's Gemini OpenAI-compatibility shim is the one gateway here
        that names its version segment "openai" instead of "v1"
        (``.../v1beta/openai``) and never wants a *separate* "/v1" at all —
        the real endpoint is ``.../v1beta/openai/chat/completions``, not
        ``.../v1beta/openai/v1/chat/completions``. Treat that ending the
        same way: drop the suffix's own "/v1" instead of the base's.
        """
        base = (self.base_url or "").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-len("/v1")]
        elif base.endswith("/openai"):
            suffix = suffix.replace("/v1/", "/", 1)
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
            "structured_outputs": self.structured_outputs,
            "configured": self.configured, "note": self.note,
        }


class ProviderState:
    """Live state: sliding-window RPM budget, cooldown, counters."""

    def __init__(self, name: str, rpm: int = 0,
                 clock: Callable[[], float] = time.monotonic,
                 cooldown_s: float = 20.0) -> None:
        self.name = name
        self.rpm = rpm
        self._clock = clock
        # Base cooldown for a non-rate-limit hiccup (5xx / timeout / network),
        # taken from the provider's own catalog/config entry (ProviderSpec
        # .cooldown_s) instead of one hardcoded number for every provider —
        # NIM, a free gateway and a local Ollama install do not fail the
        # same way or recover at the same pace. Kept in sync with the spec
        # by Router.resolve_states() whenever settings change. Named
        # differently from the "cooldown_s" key in to_dict() below, which
        # reports REMAINING seconds right now, not this configured base.
        self.base_cooldown_s = max(1.0, cooldown_s)
        self._lock = threading.RLock()
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
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_model = ""
        self.last_purpose = ""
        self.last_finish_reason = ""
        self.rate_limited = 0
        self.auth_failures = 0
        self.batch_splits = 0
        self.last_retry_after: Optional[float] = None
        self.reserve_ratio = 0.25
        self.last_call_clock = 0.0
        self.chills = 0
        self.paced_skips = 0
        # Consecutive 429s our OWN sliding-window accounting did not predict
        # (no Retry-After, and our tracked window still looked like it had
        # room). That means the real quota is smaller than configured, or
        # shared with another process/key — see mark_error().
        self.blind_rate_limits = 0
        # Consecutive non-rate-limit hiccups (timeout/network/5xx) with no
        # successful call in between. A single blip and a real outage look
        # identical on the first failure; this is what tells them apart so
        # the SECOND and THIRD consecutive hit back off further instead of
        # retrying at the same fixed interval forever. Reset by mark_ok().
        self.consecutive_failures = 0
        # A rejected key is a CONFIG problem: it blocks the provider for much
        # longer than a rate limit and is only lifted by fixing the config.
        self.auth_blocked_until = 0.0

    # -- budget -----------------------------------------------------------
    def _trim(self, now: float) -> None:
        while self._window and now - self._window[0] >= 60.0:
            self._window.popleft()

    def used_in_window(self) -> int:
        with self._lock:
            now = self._clock()
            self._trim(now)
            return len(self._window)

    def budget_left(self) -> int:
        with self._lock:
            if self.rpm <= 0:
                return 10 ** 6
            now = self._clock()
            self._trim(now)
            return max(0, self.rpm - len(self._window))

    def soft_cap(self) -> int:
        """Where we STOP spending and start saving (rpm minus the reserve)."""
        if self.rpm <= 0:
            return 10 ** 6
        return max(1, int(round(self.rpm * (1.0 - self.reserve_ratio))))

    def seconds_until_slot(self) -> float:
        """Seconds until the oldest request leaves the window (0 if room)."""
        with self._lock:
            now = self._clock()
            self._trim(now)
            if self.rpm <= 0 or len(self._window) < self.rpm:
                return 0.0
            return max(0.0, 60.0 - (now - self._window[0]))

    def reserve(self) -> bool:
        """Atomically take one request slot (kept for router compatibility)."""
        with self._lock:
            now = self._clock()
            if self.rpm <= 0:
                return True
            self._trim(now)
            if len(self._window) >= self.rpm:
                return False
            self._window.append(now)
            return True

    def begin_call(self) -> bool:
        """Atomically take an RPM slot and record one provider attempt."""
        with self._lock:
            now = self._clock()
            if not self.reserve():
                return False
            self.total_calls += 1
            self.last_call_ts = time.time()
            self.last_call_clock = now
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
        # A real answer proves the provider is healthy again — any
        # escalation from blind 429s or repeated hiccups no longer applies.
        self.blind_rate_limits = 0
        self.consecutive_failures = 0

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
            # Retry when it can ACTUALLY work again — in order of trust:
            #
            #   1. the moment the provider asked for (Retry-After header);
            #   2. the moment OUR OWN tracked window frees a slot, when our
            #      accounting already predicted this 429 (it thought the
            #      window was full);
            #   3. otherwise this is a BLIND 429: the provider rate-limited
            #      us even though our own sliding window still looked like
            #      it had room. That means the real quota is smaller than
            #      NEX_NIM_RPM (or similar) claims, or it is shared with
            #      another process/key. Retrying in a couple of seconds would
            #      just get hammered by the same 429 again, so back off a
            #      full rate-limit window instead — almost every provider
            #      resets per-minute — and escalate a little on repeated
            #      blind hits so a persistently wrong RPM setting does not
            #      keep probing every 60 seconds forever.
            #
            # This is what makes "after the minute is over, NIM is tried
            # again" TRUE instead of hopeful: a fixed short cooldown would
            # just hit the wall again when our own accounting is wrong.
            window_wait = self.seconds_until_slot()
            if retry_after:
                wait = max(float(retry_after), window_wait)
                self.blind_rate_limits = 0
            elif window_wait > 0:
                wait = window_wait
                self.blind_rate_limits = 0
            else:
                self.blind_rate_limits += 1
                wait = _jittered(_BLIND_RATE_LIMIT_BACKOFF_S * (
                    _BACKOFF_MULTIPLIER ** (self.blind_rate_limits - 1)))
            self.cooldown_until = now + min(max(wait, 2.0), _MAX_COOLDOWN_S)
        elif kind in (ERR_TIMEOUT, ERR_NETWORK):
            # A lone timeout is usually just the network having a bad
            # moment — retry soon. Several IN A ROW with no success between
            # them means the provider (or the path to it) is actually down,
            # so each consecutive hit waits longer instead of hammering a
            # dead endpoint every five seconds for the life of the outage.
            self.status = ST_COOLING
            self.consecutive_failures += 1
            wait = _jittered(5.0 * (
                _BACKOFF_MULTIPLIER ** (self.consecutive_failures - 1)))
            self.cooldown_until = now + min(wait, _MAX_COOLDOWN_S)
        elif kind == ERR_SERVER:
            # Base cooldown now comes from the provider's OWN configured
            # cooldown_s (catalog default, or an operator override) instead
            # of one flat number for every provider — NIM, a free gateway
            # and a local Ollama install do not recover from a 5xx at the
            # same pace. Escalates the same way as a timeout/network hit.
            self.status = ST_COOLING
            self.consecutive_failures += 1
            wait = _jittered(self.base_cooldown_s * (
                _BACKOFF_MULTIPLIER ** (self.consecutive_failures - 1)))
            self.cooldown_until = now + min(wait, _MAX_COOLDOWN_S)
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

    def record_response(self, obj: Dict[str, Any], model: str,
                        purpose: str) -> None:
        """Keep bounded operational telemetry; never store prompt/output text."""
        usage = _safe_usage(obj.get("usage") if isinstance(obj, dict) else {})
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        choices = obj.get("choices") if isinstance(obj, dict) else []
        choice = choices[0] if isinstance(choices, list) and choices \
            and isinstance(choices[0], dict) else {}
        self.last_prompt_tokens = prompt
        self.last_completion_tokens = completion
        self.total_prompt_tokens += prompt
        self.total_completion_tokens += completion
        self.last_model = str(obj.get("model") or model or "")[:200] \
            if isinstance(obj, dict) else str(model or "")[:200]
        self.last_purpose = str(purpose or "")[:80]
        self.last_finish_reason = str(choice.get("finish_reason") or "")[:80]

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
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
            "last_completion_tokens": self.last_completion_tokens,
            "last_model": self.last_model,
            "last_purpose": self.last_purpose,
            "last_finish_reason": self.last_finish_reason,
            "rate_limited": self.rate_limited,
            "blind_rate_limits": self.blind_rate_limits,
            "consecutive_failures": self.consecutive_failures,
            "base_cooldown_s": self.base_cooldown_s,
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
            name: ProviderState(name, spec.rpm, clock, spec.cooldown_s)
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
            st = self.states.setdefault(name, ProviderState(
                name, spec.rpm, self._clock, spec.cooldown_s))
            st.rpm = spec.rpm
            st.reserve_ratio = spec.reserve
            st.base_cooldown_s = max(1.0, spec.cooldown_s)
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
                state = ProviderState(name, spec.rpm, self._clock,
                                      spec.cooldown_s)
            state.rpm = spec.rpm
            state.reserve_ratio = spec.reserve
            state.base_cooldown_s = max(1.0, spec.cooldown_s)
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

    def model_for(self, role: str, provider: str) -> str:
        """Exact model to send for one provider attempt.

        A role override applies to its selected primary only. Fallbacks always
        use their own provider model; sending a NIM model id to Ollama/OpenAI
        (or silently ignoring the role override) is both broken and misleading.
        """
        entry = self.roles.get(role) or {}
        if provider == str(entry.get("provider") or ""):
            model = str(entry.get("model") or "").strip()
            if model:
                return model
        spec = self.specs.get(provider)
        return spec.model if spec else ""

    def fallbacks(self, role: str) -> List[str]:
        entry = self.roles.get(role) or {}
        listed = entry.get("fallbacks")
        if listed is None:
            listed = ROLE_DEFAULT_FALLBACKS.get(role, ["local"])
        return [str(n) for n in listed if str(n)]

    def chain(self, role: str) -> List[str]:
        """Ordered provider names for a role: primary -> named fallbacks.

        EXPLICIT only. Hard-work agent: NIM -> GPT -> local. Routine chat:
        local -> GPT -> NIM by default. A provider that is configured but not named in the role's
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
        # LOCAL cannot be removed from a chain: it is the only provider that
        # cannot lose a key or hosted quota. It is first for routine work and
        # terminal for the default hard-work route. If NIM fails and no GPT
        # key exists, Ollama therefore takes over without extra configuration.
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
                      purpose: str, primary: str, chain: List[str],
                      model: str, usage: Optional[Dict[str, Any]] = None) -> None:
        """A provider (not necessarily the primary) answered."""
        previous = self._last_served.get(role, "")
        switched = previous not in (None, "", name)
        self._last_served[role] = name
        if name != primary:
            # The plan does NOT change — only the hands.
            self._emit({"type": "provider.fallback", "role": role,
                        "from": primary, "to": name, "model": model,
                        "error_kind": (self.states[primary].last_error_kind
                                       if primary in self.states else ""),
                        "purpose": purpose, "usage": dict(usage or {}),
                        "plan_continues": True,
                        "roles": self.role_snapshot()})
            return
        self._emit({"type": "provider.call", "role": role,
                    "provider": name, "model": model,
                    "purpose": purpose, "usage": dict(usage or {}),
                    "recovered": switched,
                    "roles": self.role_snapshot()})
        if switched:
            # A rate limit is temporary by definition. When the window has
            # room again the primary is simply used again — and that is worth
            # saying out loud.
            self._emit({"type": "provider.recovered", "role": role,
                        "provider": name,
                        "model": model,
                        "was": previous,
                        "detail": "%s is available again — %s is the "
                                  "agent again"
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
            total = 0
            for raw in resp:
                total += len(raw)
                if len(raw) > _MAX_PROVIDER_STREAM_LINE_BYTES:
                    raise OSError("provider stream line exceeded 1 MiB")
                if total > _MAX_PROVIDER_STREAM_BYTES:
                    raise OSError("provider stream exceeded 16 MiB")
                yield raw.decode("utf-8", errors="replace")

    def _stream_call(self, spec: ProviderSpec,
                     messages: List[Dict[str, str]], model: str,
                     purpose: str,
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
        mt = purpose_token_limit(purpose, spec.max_tokens)
        if spec.kind == KIND_OLLAMA:
            options: Dict[str, Any] = {"temperature": temp}
            if mt:
                options["num_predict"] = mt
            body: Dict[str, Any] = {"model": model, "messages": messages,
                                    "stream": True, "options": options}
        else:
            body = {"model": model, "messages": messages,
                    "stream": True, "temperature": temp}
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
                finish = str(choices[0].get("finish_reason") or "")
                if finish:
                    if finish not in ("stop", "tool_calls"):
                        raise ProviderError(
                            ERR_PARSE,
                            "model=%s ended the stream with finish_reason=%s"
                            % (model or "?", finish))
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
            if not state.begin_call():
                state.mark_error(ERR_RATE_LIMIT,
                                 "local RPM budget exhausted (%d/min)"
                                 % spec.rpm)
                attempts.append({"provider": name, "error": "rpm_budget"})
                self._emit({"type": "provider.status",
                            "roles": self.role_snapshot(),
                            "note": "budget exhausted"})
                continue
            first: Optional[str] = None
            selected_model = self.model_for(role, name)
            try:
                stream = self._stream_call(
                    spec, messages, selected_model, purpose, temperature)
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
            state.record_response({}, selected_model, purpose)
            self._emit_serving(name, spec, role, purpose, primary, chain,
                               selected_model)
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
              model: str, purpose: str,
              temperature: Optional[float] = None,
              max_tokens: Optional[int] = None,
              timeout: Optional[float] = None
              ) -> Tuple[str, Dict[str, Any]]:
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
        if temperature is None:
            temp = _PURPOSE_TEMPERATURES.get(purpose, spec.temperature)
        else:
            temp = temperature
        mt = max_tokens or purpose_token_limit(purpose, spec.max_tokens)
        if spec.kind == KIND_OLLAMA:
            options: Dict[str, Any] = {"temperature": temp}
            if mt:
                options["num_predict"] = mt
            body: Dict[str, Any] = {
                "model": model, "messages": messages, "stream": False,
                "options": options,
            }
        else:
            body = {"model": model, "messages": messages,
                    "stream": False, "temperature": temp}
            if spec.structured_outputs and _structured_purpose(purpose):
                body["response_format"] = {"type": "json_object"}
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
        text = _extract_text(obj, spec.kind, model)
        if not text:
            raise ProviderError(ERR_PARSE, "empty completion from provider "
                                           "(model=%s)" % (model or "?"))
        return text, obj

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

    def _agent_rate_limit_chill_s(self) -> float:
        """How long autonomous agent work (planning/evaluation/diagnosis/
        batch, run with hours of budget by `_agent_llm`) will actually wait
        out a provider's OWN rate-limit window rather than immediately spend
        a request on a worse fallback. Just over 60s so it comfortably
        covers the whole sliding window: a hard-ceiling hit can need to wait
        up to (but never more than) a full minute for the oldest request to
        age out. Live interactive chat never uses this value -- see
        `_pace`.
        """
        return max(1.0, _as_float(
            os.environ.get("NEX_AGENT_RATE_LIMIT_CHILL_S"), 65.0))

    def _pace(self, role: str, chain: List[str], index: int, name: str,
              spec: ProviderSpec, state: ProviderState, purpose: str) -> str:
        """Decide whether this provider may spend a request right now.

        Hosted NIM limits vary; ``spec.rpm`` is Nex's operator-configured local
        safety ceiling, not a claim about NVIDIA's current quota. A 429 still
        wastes a request, so a limit is a ceiling to stay under:

          * a RESERVE (25% by default) is held for complex plans and repairs;
          * evaluations/batches inside that reserve route to a fallback when
            one is usable, while priority work may spend to the hard ceiling;
          * if this is the only option left, it CHILLS for a bounded moment
            instead of hammering — and only calls when the window has room;
          * consecutive calls to the same provider keep a minimum spacing.

        The chill ceiling itself depends on `role`: a live chat reply
        (ROLE_CHAT) keeps the provider's own `max_chill_s` exactly as
        configured, so a user is never left staring at a blocked reply for
        up to a minute. Autonomous agent work (ROLE_AGENT -- planning,
        evaluation, diagnosis, batch repairs) raises it to the much longer
        `_agent_rate_limit_chill_s()` instead: it has hours of run budget to
        spend, and a few dozen extra seconds actually waiting out a
        provider's 60-second RPM window is a clear win over immediately
        downgrading to a lesser fallback model. This is what makes "wait
        until the minute is over" literally true for real agent work,
        instead of an 8-second token gesture before giving up on the best
        provider in the chain. An operator (or test) that explicitly set
        `max_chill_s` to 0 meant "never make this provider wait, ever" --
        that explicit choice always wins, for every role, and is never
        raised.

        Returns "call" (go ahead, possibly after a chill) or "skip".
        """
        if state.rpm <= 0:
            return "call"
        if spec.max_chill_s <= 0.0:
            max_chill_s = 0.0
        elif role == ROLE_CHAT:
            max_chill_s = spec.max_chill_s
        else:
            max_chill_s = max(spec.max_chill_s,
                              self._agent_rate_limit_chill_s())
        used = state.used_in_window()
        if used >= state.rpm:
            # Hard ceiling reached: the next request WOULD be a 429.
            wait = state.seconds_until_slot()
            if wait <= max_chill_s:
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
        if used >= state.soft_cap() and purpose not in HIGH_PRIORITY_PURPOSES:
            # Inside the reserve: lower-priority evaluation/batch work moves to
            # a fallback. Complex plans and failure repairs may use the held
            # slots up to the hard ceiling — this is what the reserve is for.
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
            wait = min(state.seconds_until_slot(), max_chill_s)
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
             timeout: Optional[float] = None,
             images: Optional[List[Dict[str, str]]] = None) -> str:
        """Route a chat call for `role`. Raises AllProvidersFailed if the
        whole chain is down (callers degrade honestly instead of guessing).

        `images`: optional real image bytes (``[{"mime_type":...,
        "data": <base64>}]``) to attach to the last message. A provider
        whose configured model is not known to accept image input is
        skipped for this call exactly like an unconfigured one — it never
        silently gets the request text-only and invents an opinion about
        pixels it never received.
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
            if images and not model_supports_vision(
                    self.model_for(role, name)):
                attempts.append({"provider": name, "error": "no_vision"})
                continue
            if self._pace(role, chain, index, name, spec, state, purpose) \
                    == "skip":
                attempts.append({"provider": name, "error": "pacing"})
                continue
            if not state.begin_call():
                state.mark_error(ERR_RATE_LIMIT,
                                 "local RPM budget exhausted (%d/min)" % spec.rpm)
                attempts.append({"provider": name, "error": "rpm_budget"})
                self._emit({"type": "provider.status",
                            "roles": self.role_snapshot(),
                            "note": "budget exhausted"})
                continue
            selected_model = self.model_for(role, name)
            call_messages = (_attach_images(messages, spec.kind, images)
                             if images else messages)
            try:
                text, response = self._call(
                    spec, call_messages, selected_model, purpose,
                    temperature, max_tokens, timeout)
            except ProviderError as exc:
                state.mark_error(exc.kind, str(exc), exc.retry_after)
                attempts.append({"provider": name, "error": exc.kind,
                                 "detail": str(exc)[:200]})
                self._emit_failure(name, spec, state, role, purpose, exc,
                                   chain, index)
                continue
            state.mark_ok()
            state.total_ok += 1
            state.record_response(response, selected_model, purpose)
            usage = _safe_usage(
                response.get("usage") if isinstance(response, dict) else {})
            self._emit_serving(name, spec, role, purpose, primary, chain,
                               selected_model, usage)
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
        """Purpose-aware, vision-aware callable compatible with the agent
        loop."""
        def _bound(messages: List[Dict[str, str]], purpose: str = "agent",
                  images: Optional[List[Dict[str, str]]] = None) -> str:
            return self.chat(role, messages, purpose=purpose, images=images)
        _bound.supports_purpose = True  # type: ignore[attr-defined]
        _bound.supports_images = True  # type: ignore[attr-defined]
        return _bound

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
            out[role] = {
                "provider": provider,
                "model": self.role_model(role),
                "active": active or "",
                "active_model": self.model_for(role, active or ""),
                "serving": serving or "",
                "serving_model": self.model_for(role, serving or ""),
                "fallback": bool(serving and serving != provider),
            }
        return out

    def status(self) -> Dict[str, Any]:
        snap = self.role_snapshot()
        return {
            "roles": snap,
            "chat": snap.get(ROLE_CHAT, {}),
            "agent": snap.get(ROLE_AGENT, {}),
            "providers": {name: dict(spec.masked(),
                                     **self.states[name].to_dict())
                          for name, spec in self.specs.items()},
            "chains": {role: self.chain(role) for role in ROLES},
            "note": "chat decides what to build; agent does the work "
                    "through MCP tools only",
        }

    def _detail_line(self) -> str:
        try:
            st = self.status()
            return "chat=%s agent=%s" % (
                st["chat"].get("active") or "-",
                st["agent"].get("active") or "-")
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
                    "OPENAI_API_KEY": "GPT chat key",
                    "OPENCODE_API_KEY": "OpenCode Zen key, free/no-card — opencode.ai/auth",
                    "OPENROUTER_API_KEY": "OpenRouter key, free/no-card — openrouter.ai/keys",
                    "OLLAMA_HOST": "local model base URL",
                    "OLLAMA_MODEL": "local model name",
                    "NEX_PLANNER_PROVIDER": "which provider plans",
                    "NEX_BUILDER_PROVIDER": "which provider builds",
                    "NEX_NIM_RPM": "NIM requests/minute budget (default 40)",
                    "NEX_OPENCODE_RPM": "OpenCode Zen local safety ceiling (default 20)",
                    "NEX_OPENROUTER_RPM": "OpenRouter local safety ceiling (default 20)",
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
    if not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    finish = str(choice.get("finish_reason") or "")
    if finish and finish not in ("stop", "tool_calls"):
        raise ProviderError(
            ERR_PARSE,
            "model=%s returned an incomplete answer (finish_reason=%s)"
            % (model or "?", finish))
    msg = choice.get("message") or {}
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
        if name == "opencode" and not fields.get("api_key"):
            for extra in ("OPENCODE_API_KEY", "NEX_OPENCODE_API_KEY"):
                if os.environ.get(extra):
                    fields["api_key"] = os.environ[extra]
                    break
        if name == "openrouter" and not fields.get("api_key"):
            for extra in ("OPENROUTER_API_KEY", "NEX_OPENROUTER_API_KEY"):
                if os.environ.get(extra):
                    fields["api_key"] = os.environ[extra]
                    break
        if name == "groq" and not fields.get("api_key"):
            for extra in ("GROQ_API_KEY", "NEX_GROQ_API_KEY"):
                if os.environ.get(extra):
                    fields["api_key"] = os.environ[extra]
                    break
        if name == "google" and not fields.get("api_key"):
            for extra in ("GEMINI_API_KEY", "GOOGLE_API_KEY",
                          "NEX_GOOGLE_API_KEY"):
                if os.environ.get(extra):
                    fields["api_key"] = os.environ[extra]
                    break
        specs[name] = ProviderSpec(name, **{
            **{f: getattr(spec, f) for f in ProviderSpec.FIELDS}, **fields})
    return specs


def _env_first(*names: str) -> str:
    """First non-empty of the given env vars (new name wins over legacy)."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


def _default_roles(specs: Dict[str, ProviderSpec]) -> Dict[str, Dict[str, str]]:
    # 2.x names (NEX_CHAT_PROVIDER / NEX_AGENT_PROVIDER) with the 1.x
    # names (NEX_PLANNER_* / NEX_BUILDER_*) still honored.
    chat_provider = _env_first("NEX_CHAT_PROVIDER", "NEX_PLANNER_PROVIDER")
    agent_provider = _env_first("NEX_AGENT_PROVIDER", "NEX_BUILDER_PROVIDER")
    if not chat_provider:
        # Routine work should not spend hosted requests just because a key is
        # present. Operators can still select a cloud routine primary.
        chat_provider = "local"
    if not agent_provider:
        # Keep the preferred route stable even before credentials are entered.
        # Unconfigured providers cost no request and are skipped, so this is
        # NIM -> configured GPT -> Ollama today and automatically activates NIM
        # when its key is added later through Settings (no second routing edit).
        agent_provider = "nim" if "nim" in specs else (
            "gpt" if "gpt" in specs else "local")
    roles = {
        ROLE_CHAT: {
            "provider": chat_provider,
            "model": _env_first("NEX_CHAT_MODEL", "NEX_PLANNER_MODEL")
                     or (specs[chat_provider].model
                         if chat_provider in specs else ""),
            "fallbacks": _env_fallbacks(ROLE_CHAT,
                                        ROLE_DEFAULT_FALLBACKS[ROLE_CHAT]),
        },
        ROLE_AGENT: {
            "provider": agent_provider,
            "model": _env_first("NEX_AGENT_MODEL", "NEX_BUILDER_MODEL")
                     or (specs[agent_provider].model
                         if agent_provider in specs else ""),
            "fallbacks": _env_fallbacks(ROLE_AGENT,
                                        ROLE_DEFAULT_FALLBACKS[ROLE_AGENT]),
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
        role = _ROLE_MIGRATION.get(str(role), str(role))   # 1.x → 2.x
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
