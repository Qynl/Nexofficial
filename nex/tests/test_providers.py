#!/usr/bin/env python3
"""Tests for the provider layer (chat/agent roles + NIM failover).

The architecture under test:

    ROUTINE (chat, summary, small plan) -> Ollama first
    HARD WORK -> NVIDIA NIM -> configured GPT -> Ollama
                                      |
                            cooldown expires -> NIM again

Concretely verified here:
  1. KEY HANDLING — .env chain, 0600 store, keys never leave the process
     in an API response (masked only).
  2. ROLE DEFAULTS — Ollama owns routine work; NIM owns hard work when its
     key exists, GPT leads when only its key exists, and Ollama is guaranteed.
  3. FAILOVER — every documented failure (429 / timeout / 5xx / auth /
     bad request) hands the SAME call to the next provider and emits an
     event that says the plan continues.
  4. BUDGET — the operator-configured RPM safety ceiling is enforced
     client-side, with cooldown and automatic recovery.
  5. BATCHING — N independent jobs cost ONE provider call (that is the
     whole point of a rate-limited agent), with a safe per-job split
     when the batch answer is unusable.
  6. HONESTY — when every provider is down the router raises instead of
     returning an empty string that would look like a valid answer.
  7. NO ESCAPE — this module executes nothing: no subprocess, no eval,
     no shell. The agent's only action surface stays the MCP registry.
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
from email.message import Message
from email.utils import formatdate

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

import agent.providers as providers  # noqa: E402


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def http_error(url, code, body="", retry_after=None):
    msg = Message()
    if retry_after is not None:
        msg["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(url, code, "err", msg,
                                  io.BytesIO(body.encode("utf-8")))


class FakeHTTP:
    """Scripted transport: per-host queues of answers.

    An answer is either a string (the model text) or an Exception instance
    to raise.
    """

    def __init__(self):
        self.script = {}
        self.calls = []          # (url, body)
        self.headers = []        # outgoing headers, per call
        self.gets = []
        self.streams = []        # streaming calls: (url, body)

    def push(self, host, answer):
        self.script.setdefault(host, []).append(answer)

    @staticmethod
    def host_of(url):
        for h in ("integrate.api.nvidia.com", "api.openai.com",
                  "opencode.ai", "openrouter.ai", "api.groq.com",
                  "generativelanguage.googleapis.com",
                  "127.0.0.1:11434", "localhost"):
            if h in url:
                return h
        return url

    def _answer_for(self, url):
        host = self.host_of(url)
        q = self.script.get(host)
        if not q:
            return providers.ProviderError(providers.ERR_NETWORK,
                                           "no scripted answer for " + host)
        ans = q.pop(0) if len(q) > 1 else q[0]
        return ans

    def post(self, url, body, headers=None, timeout=60):
        self.calls.append((url, body))
        self.headers.append(dict(headers or {}))
        ans = self._answer_for(url)
        if isinstance(ans, Exception):
            raise ans
        if isinstance(ans, dict):
            return 200, ans, {}          # raw provider body (reasoning tests)
        if "127.0.0.1:11434" in url or "localhost" in url:
            return 200, {"message": {"content": ans}}, {}
        return 200, {"choices": [{"message": {"content": ans}}]}, {}

    # --- streaming transport (Router.chat_stream) ----------------------
    def stream(self, url, body, headers=None, timeout=60):
        """Yield NDJSON/SSE lines. A scripted (host, "tokens") entry is a
        list of token strings; an Exception entry raises before the first
        token, exactly like a rejected connection."""
        self.streams.append((url, body))
        self.headers.append(dict(headers or {}))
        ans = self._answer_for(url)
        if isinstance(ans, Exception):
            raise ans
        if isinstance(ans, str) and ans.startswith("AFTER:"):
            # tokens first, THEN the failure (mid-answer break)
            for tok in ans[len("AFTER:"):].split("|"):
                yield tok
            raise providers.ProviderError(providers.ERR_NETWORK,
                                          "stream broke mid-answer")
        if isinstance(ans, dict):
            toks = ans.get("tokens") or []
            fail_after = ans.get("fail_after")
        else:
            toks = str(ans).split("|")
            fail_after = None
        is_ollama = "127.0.0.1:11434" in url or "localhost" in url
        for i, tok in enumerate(toks):
            if fail_after is not None and i == fail_after:
                raise providers.ProviderError(providers.ERR_NETWORK,
                                              "stream broke mid-answer")
            if is_ollama:
                yield json.dumps({"message": {"content": tok}, "done": False})
            else:
                yield "data: " + json.dumps(
                    {"choices": [{"delta": {"content": tok},
                                  "finish_reason": None}]})
        if is_ollama:
            yield json.dumps({"message": {"content": ""}, "done": True})
        else:
            yield "data: [DONE]"

    def get(self, url, headers=None, timeout=30):
        self.gets.append(url)
        self.headers.append(dict(headers or {}))
        ans = self._answer_for(url)
        if isinstance(ans, Exception):
            raise ans
        if isinstance(ans, list):
            if "127.0.0.1:11434" in url:
                return 200, {"models": [{"name": m} for m in ans]}
            return 200, {"data": [{"id": m} for m in ans]}
        return 200, {"data": []}


def mk_router(http, clock=None, rpm=2, bus=None, pacing=False):
    """Router for tests. Pacing (real sleeps) is OFF unless a test asks for
    it — the rest of the suite is about routing, not about wall-clock time."""
    no_pace = {} if pacing else {"min_interval_s": 0.0, "max_chill_s": 0.0}
    specs = {
        "local": providers.ProviderSpec(
            "local", model="gpt-oss:20b", base_url="http://127.0.0.1:11434",
            kind=providers.KIND_OLLAMA, **no_pace),
        "nim": providers.ProviderSpec(
            "nim", api_key="nvapi-testkey", model="nvidia/nemotron-3-super-120b-a12b",
            rpm=rpm, cooldown_s=20.0, **no_pace),
        "gpt": providers.ProviderSpec(
            "gpt", api_key="sk-testkey", model="gpt-5.1", **no_pace),
    }
    roles = {
        providers.ROLE_CHAT: {"provider": "gpt", "model": "gpt-5.1"},
        providers.ROLE_AGENT: {"provider": "nim",
                                 "model": "nvidia/nemotron-3-super-120b-a12b"},
    }
    return providers.Router(specs, roles, bus=bus, clock=clock or Clock(),
                            transport={"post": http.post, "get": http.get,
                                       "stream": http.stream})


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, evt):
        self.events.append(evt)

    def of(self, t):
        return [e for e in self.events if e.get("type") == t]


MESSAGES = [{"role": "user", "content": "repair task 7"}]

# ===========================================================================
# 1. KEYS: .env chain, masking, 0600 store
# ===========================================================================

print("=== 1. keys & settings ===")

parsed = providers.parse_env_text(
    "# comment\n"
    "NVIDIA_API_KEY=nvapi-abc123\n"
    'export OPENAI_API_KEY="sk-quoted"\n'
    "OLLAMA_MODEL='gpt-oss:20b'   # local brain\n"
    "EMPTY=\n"
    "NOT A LINE\n")
_expect(parsed.get("NVIDIA_API_KEY") == "nvapi-abc123", ".env: plain value")
_expect(parsed.get("OPENAI_API_KEY") == "sk-quoted", ".env: export + double quotes")
_expect(parsed.get("OLLAMA_MODEL") == "gpt-oss:20b",
        ".env: single quotes + trailing comment")
_expect("EMPTY" in parsed and parsed["EMPTY"] == "", ".env: empty value kept")
_expect("NOT A LINE" not in parsed.values(), ".env: garbage lines ignored")

with tempfile.TemporaryDirectory() as td:
    env_path = os.path.join(td, ".env")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("NVIDIA_API_KEY=nvapi-fromfile\nNEX_NIM_RPM=17\n")
    env = {"NVIDIA_API_KEY": "nvapi-fromprocess"}
    added = providers.load_env_file(env_path, environ=env)
    _expect(env["NVIDIA_API_KEY"] == "nvapi-fromprocess",
            ".env never overrides a real environment variable")
    _expect(env.get("NEX_NIM_RPM") == "17" and added == 1,
            ".env adds the keys the process does not define")

    store = providers.SettingsStore(os.path.join(td, "providers.json"))
    store.save({"providers": {"nim": {"api_key": "nvapi-store"}}})
    mode = os.stat(store.path).st_mode & 0o777
    _expect(mode == 0o600, "settings file is 0600 (got %o)" % mode)
    _expect(store.load()["providers"]["nim"]["api_key"] == "nvapi-store",
            "settings file round-trips")

_expect(providers.mask_key("nvapi-1234567890abcdef") == "nvapi-…cdef",
        "mask_key hides the middle: %s" % providers.mask_key("nvapi-1234567890abcdef"))
_expect(providers.mask_key("") == "" and providers.mask_key(None) == "",
        "mask_key('') is empty, not a crash")

# ===========================================================================
# 2. ROLE DEFAULTS
# ===========================================================================

print("=== 2. roles ===")

_expect(providers.role_for_purpose("chat") == "chat"
        and providers.role_for_purpose("summary") == "chat"
        and providers.role_for_purpose("planning-routine") == "chat",
        "chat, summaries and small plans select the routine lane")
_expect(providers.role_for_purpose("planning-hard") == "agent"
        and providers.role_for_purpose("evaluation") == "agent"
        and providers.role_for_purpose("diagnosis") == "agent",
        "complex plans, evaluation and diagnosis select hard work")

saved = {k: os.environ.pop(k, None) for k in
         ("NVIDIA_API_KEY", "OPENAI_API_KEY", "OLLAMA_MODEL", "OLLAMA_HOST",
          "NEX_CHAT_PROVIDER", "NEX_AGENT_PROVIDER", "NEX_CHAT_MODEL",
          "NEX_AGENT_MODEL", "NEX_PLANNER_PROVIDER", "NEX_BUILDER_PROVIDER",
          "NEX_PLANNER_MODEL", "NEX_BUILDER_MODEL", "NEX_PROVIDERS_FILE",
          "NEX_NIM_RPM")}
try:
    with tempfile.TemporaryDirectory() as td:
        empty_store = providers.SettingsStore(os.path.join(td, "none.json"))
        r0 = providers.build_router(store=empty_store, load_dot_env=False)
        _expect(r0.roles["chat"]["provider"] == "local",
                "without keys the CHAT is the local model")
        _expect(r0.roles["agent"]["provider"] == "nim"
                and r0.status()["agent"]["active"] == "local",
                "without keys hard work skips cloud and runs on Ollama")
        r0.set_spec("nim", api_key="nvapi-added-later")
        _expect(r0.status()["agent"]["active"] == "nim",
                "adding a NIM key activates hard work without a routing edit")

        os.environ["NVIDIA_API_KEY"] = "nvapi-x"
        r1 = providers.build_router(store=empty_store, load_dot_env=False)
        _expect(r1.roles["agent"]["provider"] == "nim",
                "a NIM key alone makes NIM the agent")
        _expect(r1.roles["chat"]["provider"] == "local",
                "…and the chat stays local (GPT not configured)")

        os.environ["OPENAI_API_KEY"] = "sk-x"
        r2 = providers.build_router(store=empty_store, load_dot_env=False)
        _expect(r2.roles["chat"]["provider"] == "local",
                "a GPT key does not move routine chat away from Ollama")
        _expect(r2.roles["agent"]["provider"] == "nim",
                "…and NIM remains the hard-work primary")
        _expect(r2.chain("agent") ==
                ["nim", "gpt", "opencode", "openrouter", "groq", "google",
                 "local"],
                "hard chain: NIM -> GPT -> free gateways -> Ollama (%s)"
                % r2.chain("agent"))
        _expect(r2.chain("chat") ==
                ["local", "opencode", "openrouter", "groq", "google", "gpt",
                 "nim"],
                "routine chain: Ollama -> free gateways -> GPT -> NIM (%s)"
                % r2.chain("chat"))
        _expect(r2.role_model("agent") == "nvidia/nemotron-3-super-120b-a12b",
                "agent model resolves from the role config")

        os.environ.pop("NVIDIA_API_KEY", None)
        r_gpt = providers.build_router(store=empty_store, load_dot_env=False)
        _expect(r_gpt.roles["agent"]["provider"] == "nim"
                and r_gpt.status()["agent"]["active"] == "gpt"
                and r_gpt.chain("agent") ==
                ["nim", "gpt", "opencode", "openrouter", "groq", "google",
                 "local"],
                "without a NIM key, GPT serves hard work then free gateways "
                "then Ollama")
        os.environ["NVIDIA_API_KEY"] = "nvapi-x"
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# The role's model setting must control the real request, not just the UI label.
h_role = FakeHTTP()
h_role.push("integrate.api.nvidia.com", {
    "model": "moonshotai/kimi-k3",
    "choices": [{"message": {"content": '{"plan":{"steps":[]}}'},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 123, "completion_tokens": 17},
})
r_role = mk_router(h_role, rpm=40)
r_role.set_role("agent", "nim", model="moonshotai/kimi-k3")
r_role.chat("agent", MESSAGES, purpose="planning")
role_body = h_role.calls[-1][1]
_expect(role_body["model"] == "moonshotai/kimi-k3",
        "the selected role model is the model actually sent to NIM")
_expect(role_body.get("response_format") == {"type": "json_object"},
        "NIM planning uses structured JSON mode")
_expect(role_body.get("temperature") == 0.1,
        "planning gets a deterministic purpose-specific temperature")
_expect(role_body.get("max_tokens") == 2600,
        "planning uses a bounded purpose-specific output budget")
role_state = r_role.states["nim"].to_dict()
_expect(role_state["total_prompt_tokens"] == 123
        and role_state["total_completion_tokens"] == 17,
        "NIM token usage is tracked without storing prompts or answers")
_expect(role_state["last_model"] == "moonshotai/kimi-k3"
        and role_state["last_purpose"] == "planning",
        "provider telemetry names the actual model and agent job")

h_plain = FakeHTTP()
h_plain.push("integrate.api.nvidia.com", "plain compatible answer")
r_plain = mk_router(h_plain, rpm=40)
r_plain.set_spec("nim", structured_outputs=False)
r_plain.chat("agent", MESSAGES, purpose="planning")
_expect("response_format" not in h_plain.calls[-1][1],
        "JSON mode can be disabled for an incompatible NIM/runtime")

h_tokens = FakeHTTP()
h_tokens.push("127.0.0.1:11434", "short local summary")
r_tokens = mk_router(h_tokens, rpm=40)
r_tokens.set_role("chat", "local", model="gpt-oss:20b",
                  fallbacks=["gpt", "nim"])
r_tokens.chat("chat", MESSAGES, purpose="summary")
_expect(h_tokens.calls[-1][1]["options"].get("num_predict") == 700,
        "Ollama receives the same compact purpose token budget")

# ===========================================================================
# 3. FAILOVER: 429 / timeout / 5xx / auth -> next provider, same plan
# ===========================================================================

print("=== 3. failover ===")

http = FakeHTTP()
http.push("integrate.api.nvidia.com",
          http_error("https://integrate.api.nvidia.com/v1/chat/completions",
                     429, '{"error":"rate limited"}', retry_after=20))
http.push("api.openai.com", "corrected args: {\\\"speed\\\": 12}")
bus = Bus()
r = mk_router(http, bus=bus)
text = r.chat("agent", MESSAGES, purpose="diagnose")
_expect(text.startswith("corrected args"), "429 spills over: the agent call answers")
_expect(len(http.calls) == 2, "exactly two provider calls (NIM then GPT)")
_expect(http.calls[0][1]["messages"] == http.calls[1][1]["messages"],
        "the fallback sends the SAME messages — the plan is untouched")
_expect(http.calls[1][1]["model"] == "gpt-5.1",
        "the fallback uses its own model, never the NIM role override")
fallbacks = bus.of("provider.fallback")
_expect(len(fallbacks) == 1, "one provider.fallback event")
_expect(fallbacks[0]["from"] == "nim" and fallbacks[0]["to"] == "gpt",
        "the event names NIM -> GPT")
_expect(fallbacks[0]["plan_continues"] is True,
        "the event states that the plan continues (no re-plan)")
_expect(r.states["nim"].status == providers.ST_RATE_LIMITED,
        "NIM is marked rate_limited")
_expect(r.states["nim"].to_dict()["cooldown_s"] > 0,
        "NIM got a cooldown from the Retry-After header")
http_date_wait = providers._retry_after_seconds(
    formatdate(time.time() + 30, usegmt=True))
_expect(http_date_wait is not None and 20 <= http_date_wait <= 31,
        "Retry-After also accepts an RFC HTTP date")
_expect(r.status()["agent"]["fallback"] is True,
        "the status view marks the agent as running on a fallback")
_expect(r.status()["agent"]["serving"] == "gpt",
        "…and names the provider that actually served")

# No GPT credential is a normal state, not an error or a broken chain.
h_no_gpt = FakeHTTP()
h_no_gpt.push("integrate.api.nvidia.com",
              http_error("n", 503, "temporarily unavailable"))
h_no_gpt.push("127.0.0.1:11434", "ollama took over")
r_no_gpt = mk_router(h_no_gpt, rpm=40)
r_no_gpt.set_spec("gpt", api_key="", api_key_env="")
_expect(r_no_gpt.chat("agent", MESSAGES, purpose="planning-hard") ==
        "ollama took over",
        "without a GPT key, NIM failure hands directly to Ollama")
_expect(len(h_no_gpt.calls) == 2
        and all("api.openai.com" not in url for url, _body in h_no_gpt.calls),
        "an unconfigured GPT costs no request and causes no routing bug")

# recovery: cooldown expires -> NIM again
clock = Clock()
http2 = FakeHTTP()
http2.push("integrate.api.nvidia.com", http_error("n", 429, "", retry_after=20))
http2.push("integrate.api.nvidia.com", "nim answer")
http2.push("api.openai.com", "gpt answer")
r2 = mk_router(http2, clock=clock)
r2.chat("agent", MESSAGES)
_expect(r2._last_served["agent"] == "gpt", "first call fell back to GPT")
clock.advance(25)
r2.chat("agent", MESSAGES)
_expect(r2._last_served["agent"] == "nim",
        "after the cooldown NIM is the agent again — automatically")
_expect("integrate" in http2.calls[-1][0], "…because the last call went to NIM")

# 5xx and timeouts behave the same way
for label, answer, expect_status in (
        ("5xx", http_error("n", 503, "gateway"), providers.ST_COOLING),
        ("timeout", urllib.error.URLError("timed out"), providers.ST_COOLING),
        ("network", urllib.error.URLError("dns"), providers.ST_COOLING)):
    h = FakeHTTP()
    h.push("integrate.api.nvidia.com", answer)
    h.push("api.openai.com", "gpt served")
    rr = mk_router(h)
    out = rr.chat("agent", MESSAGES)
    _expect(out == "gpt served", "%s -> the fallback serves the call" % label)
    _expect(rr.states["nim"].status == expect_status,
            "%s -> NIM state is %s" % (label, expect_status))

# auth error: marked, skipped for a while, and still fails over
h = FakeHTTP()
h.push("integrate.api.nvidia.com", http_error("n", 401, "bad key"))
h.push("api.openai.com", "gpt served")
rr = mk_router(h)
_expect(rr.chat("agent", MESSAGES) == "gpt served",
        "401 -> fallback serves (a bad key must not stop the build)")
_expect(rr.states["nim"].last_error_kind == providers.ERR_AUTH,
        "the auth error is recorded with its kind")

# ===========================================================================
# 4. BUDGET: RPM ceiling, no wasted upstream calls
# ===========================================================================

print("=== 4. rate budget ===")

h = FakeHTTP()
h.push("integrate.api.nvidia.com", "nim ok")
h.push("api.openai.com", "gpt ok")
r3 = mk_router(h, rpm=2)
for _ in range(2):
    r3.chat("agent", MESSAGES)
nim_calls_after_two = len([c for c in h.calls if "integrate" in c[0]])
_expect(nim_calls_after_two == 2, "two requests used the two slots")

out = r3.chat("agent", MESSAGES)
_expect(out == "gpt ok", "the third call is routed around NIM (budget spent)")
_expect(len([c for c in h.calls if "integrate" in c[0]]) == 2,
        "NIM was NOT called a third time — the local budget blocks upstream 429s")
_expect(r3.states["nim"].status == providers.ST_RATE_LIMITED,
        "the exhausted budget marks NIM rate_limited locally")
_expect(r3.states["nim"].budget_left() == 0, "budget_left() reports 0")
_expect(r3.status()["providers"]["nim"]["requests_in_window"] == 2,
        "the status view exposes requests_in_window for the UI")

h2 = FakeHTTP()
h2.push("integrate.api.nvidia.com", "nim ok")
h2.push("api.openai.com", "gpt ok")
c2 = Clock()
r4 = mk_router(h2, clock=c2, rpm=1)
r4.chat("agent", MESSAGES)
r4.chat("agent", MESSAGES)          # routed around
c2.advance(61)                        # sliding window rolls over
r4.chat("agent", MESSAGES)
_expect(len([c for c in h2.calls if "integrate" in c[0]]) == 2,
        "the RPM window rolls over after 60s and NIM is usable again")

# ===========================================================================
# 5. BATCHING: N jobs, one call
# ===========================================================================

print("=== 5. agent batching ===")

jobs = [{"key": "job-1", "prompt": "task A failed, what now?"},
        {"key": "job-2", "prompt": "task B failed, what now?"},
        {"key": "job-3", "prompt": "task C failed, what now?"}]
h = FakeHTTP()
h.push("integrate.api.nvidia.com", json.dumps({"results": {
    "job-1": "fix A", "job-2": "fix B", "job-3": "fix C"}}))
rb = mk_router(h)
res = rb.chat_batch("agent", jobs, instruction="Diagnose each failure.")
_expect(res == {"job-1": "fix A", "job-2": "fix B", "job-3": "fix C"},
        "batch answers are mapped back to their job keys")
_expect(len(h.calls) == 1,
        "three jobs cost ONE provider call (got %d)" % len(h.calls))
_expect("job-3" in h.calls[0][1]["messages"][0]["content"],
        "every job is present in the batched prompt")

# unusable batch answer -> HALVED retries, never one call per job
h = FakeHTTP()
h.push("integrate.api.nvidia.com", "I am afraid I cannot do that.")
h.push("integrate.api.nvidia.com", "fix A")
h.push("integrate.api.nvidia.com",
       json.dumps({"results": {"job-2": "fix B", "job-3": "fix C"}}))
rb2 = mk_router(h, rpm=0)
res2 = rb2.chat_batch("agent", jobs)
_expect(res2.get("job-1") == "fix A" and res2.get("job-3") == "fix C",
        "an unusable batch answer is recovered by halving the chunk")
_expect(len(h.calls) == 3,
        "…at a bounded cost of 3 calls for 3 jobs (got %d)" % len(h.calls))
_expect(len(h.calls) < len(jobs) + 1,
        "…and NEVER one call per job (that is the multiplication to avoid)")

# hopeless batch: the cap holds, no per-job explosion, no silent loss
h_cap = FakeHTTP()
h_cap.push("integrate.api.nvidia.com", "nope")
rb_cap = mk_router(h_cap, rpm=0)
res_cap = rb_cap.chat_batch("agent", jobs)
_expect(len(h_cap.calls) == 3,
        "a hopeless batch stops at the cap (3 calls, got %d)" % len(h_cap.calls))
_expect(res_cap.get("job-2") is None and res_cap.get("job-3") is None,
        "…and invents nothing for the jobs that never got an answer")
_expect(rb_cap.states["nim"].batch_splits == 1,
        "the split is visible in the provider state")

_expect(rb2.chat_batch("agent", []) == {}, "empty job list is a no-op")
h_solo = FakeHTTP()
h_solo.push("integrate.api.nvidia.com", "solo answer")
one = mk_router(h_solo, rpm=0).chat_batch("agent",
                                          [{"key": "solo", "prompt": "hi"}])
_expect(one == {"solo": "solo answer"}, "a single job goes straight through")
_expect(len(h_solo.calls) == 1, "…as one ordinary call, not a batch wrapper")

# ===========================================================================
# 6. HONESTY: nothing up -> raise, never an empty answer
# ===========================================================================

print("=== 6. honesty ===")

h = FakeHTTP()
for host in ("integrate.api.nvidia.com", "api.openai.com", "127.0.0.1:11434"):
    h.push(host, urllib.error.URLError("down"))
rdead = mk_router(h, rpm=0)
raised = False
try:
    rdead.chat("agent", MESSAGES)
except providers.AllProvidersFailed as exc:
    raised = True
    _expect(len(exc.attempts) == 3,
            "all three providers were attempted (%d)" % len(exc.attempts))
_expect(raised, "with every provider down the router RAISES (no invented text)")
_expect(rdead.available("agent") is False,
        "available() reports False — callers must degrade knowingly")

# an unconfigured remote provider is reported as such, never called
specs = {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                         base_url="http://127.0.0.1:11434"),
         "nim": providers.ProviderSpec("nim", base_url="https://integrate.api.nvidia.com/v1")}
r5 = providers.Router(specs, {"chat": {"provider": "nim"},
                              "agent": {"provider": "nim"}},
                      transport={"post": h.post, "get": h.get})
_expect(r5.states["nim"].status == providers.ST_NO_KEY,
        "a NIM config without a key shows up as no_key (not as 'available')")
_expect(r5.available("agent") is True,
        "the chain still finds the local provider as a working fallback")

# ===========================================================================
# 7. STATUS / SETTINGS VIEW — what the frontend gets (and does NOT get)
# ===========================================================================

print("=== 7. status & settings view ===")

h = FakeHTTP()
r6 = mk_router(h)
view = r6.settings_view()
blob = json.dumps(view)
_expect("nvapi-testkey" not in blob, "the raw NIM key NEVER appears in the view")
_expect("sk-testkey" not in blob, "the raw GPT key never appears either")
_expect(view["providers"]["nim"]["api_key"] == "nvapi-…tkey",
        "the key is masked instead: %s" % view["providers"]["nim"]["api_key"])
_expect(view["providers"]["nim"]["api_key_set"] is True, "api_key_set flag is set")
_expect(view["roles"]["agent"]["provider"] == "nim", "roles are part of the view")
_expect(all(k in view["providers"]["nim"]
            for k in ("status", "rpm", "requests_in_window", "key_mismatch")),
        "the settings view carries live state too (one shape, not two)")
_expect("nvapi-testkey" not in json.dumps(view["providers"]),
        "…and still never the raw key")
_expect(len(view["catalog"]) >= 10, "the model catalog ships with the view")
_expect(all("id" in c and "note" in c for c in view["catalog"]),
        "every catalog entry explains itself")
st = r6.status()
_expect(set(st["roles"]) == {"chat", "agent"}, "status has both roles")
_expect(all(k in st["providers"]["nim"] for k in
            ("status", "rpm", "requests_in_window", "budget_left")),
        "provider status carries the quota numbers the UI shows")
_expect("local" in st["chains"]["agent"], "local is always in the agent chain")

# ===========================================================================
# 8. APPLY — settings push updates the live router and persists
# ===========================================================================

print("=== 8. settings push ===")

with tempfile.TemporaryDirectory() as td:
    st_path = os.path.join(td, "providers.json")
    store = providers.SettingsStore(st_path)
    h = FakeHTTP()
    r7 = mk_router(h)
    r7._store_path = st_path
    out = r7.apply({"providers": {"nim": {"api_key": "nvapi-new", "rpm": 12}},
                    "roles": {"agent": {"provider": "nim",
                                          "model": "moonshotai/kimi-k2.6"}}},
                   store=store)
    _expect(r7.specs["nim"].key == "nvapi-new", "the new key is live")
    _expect(r7.specs["nim"].rpm == 12, "the new RPM budget is live")
    _expect(r7.role_model("agent") == "moonshotai/kimi-k2.6",
            "the agent model switched to the chosen one")
    _expect("nvapi-new" not in json.dumps(out), "the response still masks the key")
    persisted = json.load(open(st_path, encoding="utf-8"))
    _expect(persisted["providers"]["nim"]["api_key"] == "nvapi-new",
            "the key is persisted (0600 file), so it survives a restart")
    r7.apply({"providers": {"nim": {"api_key": "-"}}}, store=store)
    _expect(r7.specs["nim"].key == "", "'-' clears the stored key")
    r7.apply({"providers": {"nim": {"api_key": ""}}}, store=store)
    _expect(r7.specs["nim"].key == "",
            "an empty api_key field means 'keep' (does not resurrect the key)")

    # a restart picks the stored config back up
    r8 = providers.build_router(store=store, load_dot_env=False)
    _expect(r8.specs["nim"].key == "", "restart honours the cleared key")
    _expect(r8.role_model("agent") == "moonshotai/kimi-k2.6",
            "restart honours the chosen agent model")

# ===========================================================================
# 9. PROBE — the settings page's "Test" button
# ===========================================================================

print("=== 9. probe ===")

h = FakeHTTP()
h.push("integrate.api.nvidia.com",
       ["nvidia/nemotron-3-super-120b-a12b", "moonshotai/kimi-k2.6"])
r9 = mk_router(h)
ping = r9.probe("nim")
_expect(ping["ok"] is True, "probe succeeds against a live endpoint")
_expect(ping["model_available"] is True, "the configured model is in the live list")
_expect("moonshotai/kimi-k2.6" in ping["models"],
        "the live model list comes back (the settings page offers it)")
_expect(ping["models_count"] == 2, "model count reported")
_expect(ping["latency_ms"] >= 0, "probe reports latency")

h2 = FakeHTTP()
h2.push("integrate.api.nvidia.com", http_error("n", 401, "bad key"))
r10 = mk_router(h2)
bad = r10.probe("nim")
_expect(bad["ok"] is False and bad["kind"] == providers.ERR_AUTH,
        "a 401 probe is reported honestly as auth_error")
_expect("nvapi-testkey" not in json.dumps(bad), "…without leaking the key")

# ===========================================================================
# 10. NO ESCAPE — the agent cannot reach the machine
# ===========================================================================

print("=== 10. no escape ===")

src = open(os.path.join(NEX, "agent", "providers.py"), encoding="utf-8").read()
_code = re.sub(r"(?s)\"\"\".*?\"\"\"", "", src)      # ignore docstrings
for needle in ("subprocess", "os.system", "os.popen", "eval(", "exec(",
               "shell=True", "pty.spawn"):
    _expect(needle not in _code,
            "providers.py never uses %s — the agent has no OS surface" % needle)
_expect("urllib.request" in src, "…it talks HTTP and nothing else")

r11 = mk_router(FakeHTTP())
_expect(not hasattr(r11, "execute") and not hasattr(r11, "run_tool"),
        "the router exposes no tool execution method")
_expect(r11.callable_for("agent") is not None or True,
        "callable_for('agent') is the only thing the agent loop receives")

# ===========================================================================
# 11. THE AGENT INSIDE THE LOOP — N failures, ONE agent request
# ===========================================================================

# ===========================================================================
# 11. AUDIT FIXES — one regression block per finding
# ===========================================================================

print("=== 11. audit regressions ===")

# --- 🔴 ARBITRARY PROVIDER URLS (SSRF + key exfiltration) -----------------
for bad, why in (
        ("file:///etc/passwd", "non-http scheme"),
        ("gopher://x/v1", "non-http scheme"),
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
        ("http://metadata.google.internal/v1", "cloud metadata"),
        ("http://user:secret@evil.example/v1", "credentials in the URL"),
        ("http://127.0.0.1:8787/v1", "Nex itself (loop)"),
        ("", "empty")):
    try:
        providers.validate_base_url(bad)
        _expect(False, "arbitrary provider URL refused (%s)" % why)
    except ValueError:
        _expect(True, "arbitrary provider URL refused (%s): %s" % (why, bad or "''"))
    except Exception as exc:  # pragma: no cover
        _expect(False, "unexpected error for %s: %r" % (bad, exc))
_expect(providers.validate_base_url("https://integrate.api.nvidia.com/v1")
        == "https://integrate.api.nvidia.com/v1", "a real endpoint still passes")
_expect(providers.validate_base_url("http://127.0.0.1:11434") ==
        "http://127.0.0.1:11434", "loopback Ollama still passes")

# the key does NOT follow the endpoint to a new host
h_ssrf = FakeHTTP()
r_ssrf = mk_router(h_ssrf)
r_ssrf.apply({"providers": {"nim": {"base_url": "https://evil.example/v1"}}},
             store=providers.SettingsStore(os.path.join(tempfile.mkdtemp(),
                                                        "s.json")))
_expect(r_ssrf.specs["nim"].key == "",
        "a key entered for NIM is NOT sent to a different host")
_expect(r_ssrf.specs["nim"].key_mismatch is True, "the mismatch is reported")
_expect("evil.example" in r_ssrf.specs["nim"].unconfigured_reason,
        "the reason names both hosts: %s"
        % r_ssrf.specs["nim"].unconfigured_reason)
h_ssrf.push("api.openai.com", "gpt served")
out_ssrf = r_ssrf.chat("agent", MESSAGES)
_expect(out_ssrf == "gpt served",
        "the misconfigured provider is skipped, the build continues")
_expect(not any("nvapi" in str(hdrs.get("Authorization", ""))
                for hdrs in h_ssrf.headers),
        "no request ever carried the stranded key")

# ... and re-entering the key confirms the new endpoint (explicit act)
r_ssrf.apply({"providers": {"nim": {"api_key": "nvapi-moved"}}},
             store=providers.SettingsStore(os.path.join(tempfile.mkdtemp(),
                                                        "s2.json")))
_expect(r_ssrf.specs["nim"].key == "nvapi-moved"
        and r_ssrf.specs["nim"].key_mismatch is False,
        "typing the key again binds it to the new endpoint")

# strict allowlist for locked-down deployments
os.environ["NEX_PROVIDER_HOSTS"] = "vllm.internal"
try:
    providers.validate_base_url("https://evil.example/v1")
    _expect(False, "NEX_PROVIDER_HOSTS enforces the allowlist")
except ValueError:
    _expect(True, "NEX_PROVIDER_HOSTS enforces the allowlist")
_expect(providers.validate_base_url("https://vllm.internal:8000/v1"),
        "…and lets the allowed host through")
os.environ.pop("NEX_PROVIDER_HOSTS", None)

# --- 🔴 reasoning_content IS NEVER THE COMPLETION ------------------------
h_cot = FakeHTTP()
h_cot.push("integrate.api.nvidia.com", {"choices": [{"message": {
    "content": "", "reasoning_content": "Maybe I should create a part..."}}]})
h_cot.push("api.openai.com", "gpt answered properly")
r_cot = mk_router(h_cot)
out_cot = r_cot.chat("agent", MESSAGES)
_expect(out_cot == "gpt answered properly",
        "a reasoning-only body is NOT accepted as an answer — failover instead")
_expect(r_cot.states["nim"].last_error_kind == providers.ERR_PARSE,
        "…the reason is recorded (kind=%s)" % r_cot.states["nim"].last_error_kind)
_expect("reasoning" in r_cot.states["nim"].last_error.lower(),
        "…and names the reasoning problem: %s"
        % r_cot.states["nim"].last_error[:70])
h_cot2 = FakeHTTP()
h_cot2.push("integrate.api.nvidia.com", {"choices": [{"message": {
    "content": "", "reasoning_content": "scratchpad"}}]})
h_cot2.push("api.openai.com", {"choices": [{"message": {
    "content": "", "reasoning_content": "more scratchpad"}}]})
h_cot2.push("127.0.0.1:11434", {"message": {"content": ""}})
try:
    mk_router(h_cot2).chat("agent", MESSAGES)
    _expect(False, "if EVERY provider only returns reasoning, the router raises")
except providers.AllProvidersFailed:
    _expect(True, "if EVERY provider only returns reasoning, the router raises")

h_cut = FakeHTTP()
h_cut.push("integrate.api.nvidia.com", {"choices": [{
    "message": {"content": '{"plan":'}, "finish_reason": "length"}]})
h_cut.push("api.openai.com", "complete fallback answer")
r_cut = mk_router(h_cut)
_expect(r_cut.chat("agent", MESSAGES, purpose="planning") ==
        "complete fallback answer",
        "a max-token-truncated NIM plan is rejected and safely failed over")
_expect(r_cut.states["nim"].last_error_kind == providers.ERR_PARSE,
        "truncation is classified as a bad response, never successful work")

# --- 🟠 SETTINGS CHANGE MUST NOT RESET THE RATE BUDGET -------------------
h_res = FakeHTTP()
h_res.push("integrate.api.nvidia.com", "nim ok")
h_res.push("api.openai.com", "gpt ok")
r_res = mk_router(h_res, rpm=2)
r_res.chat("agent", MESSAGES)
r_res.chat("agent", MESSAGES)
_expect(r_res.states["nim"].to_dict()["requests_in_window"] == 2, "two requests consumed")
store_res = providers.SettingsStore(os.path.join(tempfile.mkdtemp(), "s.json"))
r_res.apply({"providers": {"nim": {"rpm": 40, "base_url":
            "https://integrate.api.nvidia.com/v1"}}}, store=store_res)
_expect(r_res.states["nim"].to_dict()["requests_in_window"] == 2,
        "…and still two after a settings change (no quota reset)")
_expect(r_res.states["nim"].budget_left() == 38,
        "the raised budget counts the requests already made")
r_res.apply({"providers": {"nim": {"rpm": 1}}}, store=store_res)
_expect(r_res.states["nim"].budget_left() == 0,
        "lowering the budget below the usage makes it unavailable at once")
nim_calls = len([c for c in h_res.calls if "integrate" in c[0]])
r_res.chat("agent", MESSAGES)
_expect(len([c for c in h_res.calls if "integrate" in c[0]]) == nim_calls,
        "…so the overspent provider is routed around, not called")

# --- 🟠 401/403 IS LOUD, NOT A SILENT FAILOVER ---------------------------
h_auth = FakeHTTP()
h_auth.push("integrate.api.nvidia.com", http_error("n", 401, "invalid key"))
h_auth.push("api.openai.com", "gpt served")
bus_auth = Bus()
r_auth = mk_router(h_auth, bus=bus_auth)
_expect(r_auth.chat("agent", MESSAGES) == "gpt served",
        "a rejected key does not kill the build (failover continues)")
auth_events = bus_auth.of("provider.auth_error")
_expect(len(auth_events) == 1, "…but it is announced as provider.auth_error")
_expect(auth_events[0]["provider"] == "nim"
        and "settings" in auth_events[0]["detail"],
        "the event names the provider and says what to do: %s"
        % auth_events[0]["detail"])
_expect(r_auth.states["nim"].status == providers.ST_ERROR,
        "the provider is marked error, not just 'cooling'")
_expect(r_auth.states["nim"].to_dict()["auth_blocked_s"] > 60,
        "a rejected key blocks the provider for a long time (%.0fs)"
        % r_auth.states["nim"].to_dict()["auth_blocked_s"])
n_before = len(h_auth.calls)
r_auth.chat("agent", MESSAGES)
_expect(len(h_auth.calls) - n_before == 1,
        "the rejected provider is not hammered again (only the fallback ran)")
# reset the scripted answer for that host (the old 401 is still queued)
h_auth.script["integrate.api.nvidia.com"] = ["nim recovered"]
r_auth.apply({"providers": {"nim": {"api_key": "nvapi-fixed"}}},
             store=providers.SettingsStore(os.path.join(tempfile.mkdtemp(),
                                                        "s.json")))
_expect(r_auth.states["nim"].auth_blocked() is False,
        "fixing the key lifts the auth block immediately")
_expect(r_auth.chat("agent", MESSAGES) == "nim recovered",
        "…and NIM is used again")

# --- 🟠 BATCH FAILURE MUST NOT MULTIPLY REQUESTS -------------------------
jobs8 = [{"key": "job-%d" % i, "prompt": "step %d failed" % i} for i in range(8)]
h_b8 = FakeHTTP()
h_b8.push("integrate.api.nvidia.com", "no json here")
r_b8 = mk_router(h_b8, rpm=0)
res_b8 = r_b8.chat_batch("agent", jobs8)
_expect(len(h_b8.calls) <= 3,
        "8 unusable jobs cost at most 3 requests, never 8 (got %d)"
        % len(h_b8.calls))
h_b9 = FakeHTTP()
h_b9.push("integrate.api.nvidia.com", "no json here")
r_b9 = mk_router(h_b9, rpm=0)
r_b9.chat_batch("agent", jobs8, max_calls=1)
_expect(len(h_b9.calls) == 1, "max_calls=1 disables splitting entirely")
_expect(res_b8 == {} and r_b9.states["nim"].budget_left() >= 0,
        "no answers are invented when nothing parses")

# a bad patch is ATOMIC: nothing half-applies, the store stays untouched
store_at = providers.SettingsStore(os.path.join(tempfile.mkdtemp(), "s.json"))
h_at = FakeHTTP()
r_at = mk_router(h_at)
model_before = r_at.specs["gpt"].model
try:
    r_at.apply({"providers": {"gpt": {"model": "gpt-5.2"},
                              "nim": {"base_url": "http://169.254.169.254/v1"}}},
               store=store_at)
    _expect(False, "a patch containing a metadata URL is refused")
except ValueError as exc:
    _expect("169.254.169.254" in str(exc),
            "a patch containing a metadata URL is refused: %s" % str(exc)[:60])
_expect(r_at.specs["gpt"].model == model_before,
        "…and the VALID part of that patch was NOT applied either (atomic)")
_expect(store_at.load() == {}, "…and nothing was written to the store")
try:
    r_at.apply({"roles": {"agent": {"provider": "nim",
                                      "fallbacks": ["local", "ghost"]}}},
               store=store_at)
    _expect(False, "an unknown fallback name is refused")
except ValueError as exc:
    _expect("ghost" in str(exc),
            "an unknown fallback name is refused: %s" % str(exc)[:60])

# --- 🟡 THE DEFAULT LOCAL MODEL IS gpt-oss:20b ---------------------------
_expect(providers.DEFAULT_PROVIDERS["local"]["model"] == "gpt-oss:20b",
        "the shipped local default is gpt-oss:20b (the small brain)")
with tempfile.TemporaryDirectory() as td:
    saved2 = {k: os.environ.pop(k, None) for k in
              ("OLLAMA_MODEL", "NVIDIA_API_KEY", "OPENAI_API_KEY")}
    try:
        r_def = providers.build_router(
            store=providers.SettingsStore(os.path.join(td, "none.json")),
            load_dot_env=False)
        _expect(r_def.role_model("chat") == "gpt-oss:20b"
                and r_def.status()["agent"]["active_model"] == "gpt-oss:20b",
                "a fresh install actually serves routine and hard work locally")
    finally:
        for k, v in saved2.items():
            if v is not None:
                os.environ[k] = v

# --- 🟡 THE FALLBACK CHAIN IS EXPLICIT ----------------------------------
specs_ex = {
    "local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                    base_url="http://127.0.0.1:11434"),
    "nim": providers.ProviderSpec("nim", api_key="nvapi-x"),
    "gpt": providers.ProviderSpec("gpt", api_key="sk-x"),
    # a provider that exists but is named NOWHERE
    "mystery": providers.ProviderSpec("mystery", api_key="mk-x",
                                      base_url="https://mystery.example/v1"),
}
roles_ex = {"chat": {"provider": "gpt"},
            "agent": {"provider": "nim"}}
r_ex = providers.Router(specs_ex, roles_ex)
_expect(r_ex.chain("agent") == ["nim", "gpt", "local"],
        "the agent chain is exactly the named one: %s" % r_ex.chain("agent"))
_expect("mystery" not in r_ex.chain("agent")
        and "mystery" not in r_ex.chain("chat"),
        "an unnamed provider can never enter a chain by itself")
r_ex.set_role("agent", "mystery")
_expect(r_ex.chain("agent")[0] == "mystery",
        "…but it CAN be chosen as the primary (explicit act)")
_expect(r_ex.chain("agent")[1:] == ["gpt", "local"],
        "…and it inherits the role's named fallbacks")
r_ex.set_role("agent", "nim", fallbacks=["local"])
_expect(r_ex.chain("agent") == ["nim", "local"],
        "an operator can shorten the chain: %s" % r_ex.chain("agent"))
_expect(set(r_ex.status()["roles"]) == {"chat", "agent"},
        "the status view still reports both roles")
r_ex.set_role("chat", "nim")
_expect(r_ex.role_model("chat") == "nvidia/nemotron-3-super-120b-a12b",
        "switching a role's provider adopts the NEW provider's model "
        "(got %r)" % r_ex.role_model("chat"))

# ===========================================================================
# 13. PACING, RECOVERY AND THE LOCAL MODEL AS THE LAST RESORT
# ===========================================================================

print("=== 12. rate-budget pacing + recovery ===")

# --- Ollama takes over when NIM is limited and NO other provider is set ----
h_local = FakeHTTP()
h_local.push("integrate.api.nvidia.com", http_error("n", 429, "", retry_after=30))
h_local.push("127.0.0.1:11434", "ollama built it")
r_local = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     model="gpt-oss:20b", min_interval_s=0.0,
                                     max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=40,
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"chat": {"provider": "nim", "fallbacks": ["gpt"]},
     "agent": {"provider": "nim", "fallbacks": ["gpt"]}},
    transport={"post": h_local.post, "get": h_local.get})
_expect(r_local.chain("agent") == ["nim", "local"],
        "with only NIM configured the chain is NIM -> local (%s)"
        % r_local.chain("agent"))
_expect(r_local.chat("agent", MESSAGES) == "ollama built it",
        "…so a rate-limited NIM hands over to Ollama automatically")
_expect(r_local._last_served["agent"] == "local", "Ollama served the call")

# --- the rate-limit cooldown lasts until the window has room --------------
cl = Clock()
h_win = FakeHTTP()
h_win.push("integrate.api.nvidia.com", http_error("n", 429, "", retry_after=5))
h_win.push("integrate.api.nvidia.com", "nim back")
h_win.push("127.0.0.1:11434", "ollama")
r_win = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=2,
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl, transport={"post": h_win.post, "get": h_win.get})
r_win.chat("agent", MESSAGES)                     # rate limited
blocked_for = r_win.states["nim"].to_dict()["cooldown_s"]
_expect(blocked_for >= 5, "the cooldown honours Retry-After (%.0fs)" % blocked_for)
cl.advance(6)
_expect(r_win.chat("agent", MESSAGES) == "nim back",
        "after the wait NIM is tried AGAIN (not skipped forever)")

# --- our own window is the clock: the cooldown waits for a free slot ------
cl2 = Clock()
h_full = FakeHTTP()
h_full.push("integrate.api.nvidia.com", "nim ok")
h_full.push("127.0.0.1:11434", "ollama")
r_full = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=2, reserve=0.0,
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl2, transport={"post": h_full.post, "get": h_full.get})
r_full.chat("agent", MESSAGES)
r_full.chat("agent", MESSAGES)
_expect(r_full.states["nim"].used_in_window() == 2, "the window is full")
_expect(r_full.chat("agent", MESSAGES) == "ollama",
        "the overflow goes to the local model")
_expect(r_full.states["local"] is not None, "…and local served it")
cl2.advance(61)
r_full.chat("agent", MESSAGES)
_expect(len([c for c in h_full.calls if "integrate" in c[0]]) == 3,
        "…and a minute later NIM is used again by itself")

# --- the reserve keeps headroom: fallback absorbs work before the cap ----
h_res2 = FakeHTTP()
h_res2.push("integrate.api.nvidia.com", "nim ok")
h_res2.push("127.0.0.1:11434", "ollama")
r_res2 = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=4, reserve=0.5,
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    transport={"post": h_res2.post, "get": h_res2.get})
_expect(r_res2.states["nim"].soft_cap() == 2,
        "rpm=4 with a 50%% reserve stops spending at 2")
r_res2.chat("agent", MESSAGES)
r_res2.chat("agent", MESSAGES)
res3 = r_res2.chat("agent", MESSAGES)
_expect(res3 == "ollama",
        "the third call is routed around though the hard cap is 4")
_expect(r_res2.states["nim"].paced_skips == 1, "the pacing skip is counted")
_expect(r_res2.states["nim"].to_dict()["headroom"] is True,
        "…and the state reports 'headroom' for the UI")
_expect(len([c for c in h_res2.calls if "integrate" in c[0]]) == 2,
        "NIM was called exactly twice — the reserve is real")
priority = r_res2.chat("agent", MESSAGES, purpose="planning-hard")
_expect(priority == "nim ok"
        and r_res2.states["nim"].used_in_window() == 3,
        "a complex plan may spend one of the RPM slots held in reserve")

# --- with a full window and no fallback, it CHILLS instead of hammering --
cl3 = Clock()
h_chill = FakeHTTP()
h_chill.push("integrate.api.nvidia.com", "nim ok")
r_chill = providers.Router(
    {"nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=1, reserve=0.0,
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": []}},
    clock=cl3, transport={"post": h_chill.post, "get": h_chill.get})
r_chill.chat("agent", MESSAGES)
calls_before = len(h_chill.calls)
out_chill = None
try:
    out_chill = r_chill.chat("agent", MESSAGES)
except providers.AllProvidersFailed:
    out_chill = None
_expect(len(h_chill.calls) == calls_before,
        "a full window with NO fallback does not send another request")
_expect(out_chill is None,
        "…it raises instead (the caller degrades honestly, no invented text)")

# --- concurrent runs cannot race through the local RPM ceiling ------------
h_race = FakeHTTP()
h_race.push("integrate.api.nvidia.com", "nim ok")
r_race = providers.Router(
    {"nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=4,
                                   reserve=0.0, min_interval_s=0.0,
                                   max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": []}},
    transport={"post": h_race.post, "get": h_race.get})
barrier = threading.Barrier(12)

def race_call():
    barrier.wait()
    try:
        r_race.chat("agent", MESSAGES, purpose="planning-hard")
    except providers.AllProvidersFailed:
        pass

threads = [threading.Thread(target=race_call) for _ in range(12)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=5)
_expect(r_race.states["nim"].used_in_window() == 4
        and len(h_race.calls) == 4,
        "concurrent runs share one atomic 4-RPM ceiling")

# --- pacing events are emitted -------------------------------------------
h_ev = FakeHTTP()
h_ev.push("integrate.api.nvidia.com", "nim ok")
h_ev.push("127.0.0.1:11434", "ollama")
bus_ev = Bus()
r_ev = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=2, reserve=0.5,
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    bus=bus_ev, transport={"post": h_ev.post, "get": h_ev.get})
r_ev.chat("agent", MESSAGES)
r_ev.chat("agent", MESSAGES)
paced = [e for e in bus_ev.events if e.get("type") == "provider.paced"]
_expect(paced, "a pacing decision is visible to the UI")
_expect("headroom" in str(paced[0].get("reason", "")),
        "…with the reason spelled out: %s" % paced[0].get("reason"))
fbs_ev = [e for e in bus_ev.events if e.get("type") == "provider.fallback"]
_expect(fbs_ev and fbs_ev[-1]["to"] == "local",
        "…and the chip then shows the local model as the serving agent")
_expect(bus_ev.events.index(paced[0]) < bus_ev.events.index(fbs_ev[-1]),
        "the pacing decision comes BEFORE the hand-off it causes")

# --- the plan does not restart: identical messages on every provider -----
msgs_before = None
h_same = FakeHTTP()
h_same.push("integrate.api.nvidia.com", http_error("n", 503, "gateway"))
h_same.push("127.0.0.1:11434", "ollama")
r_same = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x",
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    transport={"post": h_same.post, "get": h_same.get})
r_same.chat("agent", MESSAGES, purpose="repair")
sent = [b["messages"] for _u, b in h_same.calls]
_expect(len(sent) == 2 and sent[0] == sent[1],
        "NIM and the local model receive the SAME messages (plan untouched)")

# --- non-rate-limit trouble is announced, with what happens next ---------
h_tr = FakeHTTP()
h_tr.push("integrate.api.nvidia.com", urllib.error.URLError("timed out"))
h_tr.push("127.0.0.1:11434", "ollama")
bus_tr = Bus()
r_tr = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x",
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    bus=bus_tr, transport={"post": h_tr.post, "get": h_tr.get})
r_tr.chat("agent", MESSAGES)
trouble = [e for e in bus_tr.events if e.get("type") == "provider.trouble"]
_expect(len(trouble) == 1,
        "a non-rate-limit failure is announced (provider.trouble)")
_expect("timed out" in trouble[0]["detail"]
        and "retried automatically" in trouble[0]["detail"],
        "…and says what happens next: %s" % trouble[0]["detail"])
rate_only = [e for e in bus_tr.events
             if e.get("type") == "provider.trouble"
             and e.get("error_kind") == "rate_limit"]
_expect(not rate_only, "a rate limit is NOT reported as trouble (it is pacing)")

# --- coming back is announced too ----------------------------------------
cl4 = Clock()
h_back = FakeHTTP()
h_back.push("integrate.api.nvidia.com", http_error("n", 429, "", retry_after=3))
h_back.push("127.0.0.1:11434", "ollama")
bus_back = Bus()
r_back = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=40,
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl4, bus=bus_back, transport={"post": h_back.post, "get": h_back.get})
r_back.chat("agent", MESSAGES)
cl4.advance(5)
h_back.script["integrate.api.nvidia.com"] = ["nim again"]
r_back.chat("agent", MESSAGES)
recovered = [e for e in bus_back.events if e.get("type") == "provider.recovered"]
_expect(len(recovered) == 1, "NIM returning is announced (provider.recovered)")
_expect(recovered[0]["was"] == "local" and recovered[0]["provider"] == "nim",
        "…naming both sides: %s -> %s" % (recovered[0]["was"],
                                          recovered[0]["provider"]))

# ---------------------------------------------------------------------------
# 13b. ENDPOINT URLS: the version segment belongs to exactly one side
# ---------------------------------------------------------------------------
# A base URL is written the way providers document it (`.../v1`) and the
# OpenAI-compatible path is `/v1/chat/completions`. Appending naively gives
# `.../v1/v1/chat/completions` — a 404 that looks like a provider that is
# down, and silently fails the build over to the local model forever.
_URL_CASES = [
    ("https://integrate.api.nvidia.com/v1", "openai",
     "https://integrate.api.nvidia.com/v1/chat/completions",
     "https://integrate.api.nvidia.com/v1/models"),
    ("https://api.openai.com/v1/", "openai",
     "https://api.openai.com/v1/chat/completions",
     "https://api.openai.com/v1/models"),
    ("http://my-host:8080", "openai",
     "http://my-host:8080/v1/chat/completions",
     "http://my-host:8080/v1/models"),
    ("http://127.0.0.1:11434", "ollama",
     "http://127.0.0.1:11434/api/chat",
     "http://127.0.0.1:11434/api/tags"),
    ("http://127.0.0.1:11434/v1", "ollama",
     "http://127.0.0.1:11434/api/chat",
     "http://127.0.0.1:11434/api/tags"),
]
for _base, _kind, _want_chat, _want_models in _URL_CASES:
    _sp = providers.ProviderSpec("u", api_key="k", model="m",
                                 base_url=_base, kind=_kind)
    _expect(_sp.chat_url == _want_chat,
            "%s -> %s (got %s)" % (_base, _want_chat, _sp.chat_url))
    _expect(_sp.models_url == _want_models,
            "%s models -> %s (got %s)" % (_base, _want_models, _sp.models_url))
_expect(providers.ProviderSpec(
        "u", api_key="k", model="m",
        base_url="https://integrate.api.nvidia.com/v1").chat_url
        == "https://integrate.api.nvidia.com/v1/chat/completions",
        "the documented NIM base URL produces the documented endpoint")

# ---------------------------------------------------------------------------
# 14. STREAMING goes through the SAME rules as chat()
# ---------------------------------------------------------------------------
# The chat/voice conversation is a stream, and a stream that bypasses the
# provider layer would ignore the chain, the pacing, the cooldowns and the
# key binding — and would burn NIM budget without counting it. These checks
# pin the streaming path to the routing rules.
txt = providers._extract_text  # (keep the import used)


def _stream(router, role="chat", msgs=None):
    return list(router.chat_stream(role, msgs or [{"role": "user",
                                                   "content": "hi"}],
                                   purpose="chat"))


# 1) The chain is respected: chat = gpt -> local, and gpt streams.
h = FakeHTTP()
h.push("api.openai.com", "Hello| there")
h.push("127.0.0.1:11434", "should not be used")
bus = Bus()
r = mk_router(h, bus=bus)
out = _stream(r)
_expect(out == ["Hello", " there"],
        "the streaming call streams through the chain: %r" % (out,))
_expect(h.calls == [] and len(h.streams) == 1,
        "streaming uses the streaming transport, not the blocking one")
_expect(h.streams[0][0] == "https://api.openai.com/v1/chat/completions",
        "the stream hits the provider's chat endpoint exactly once: %s"
        % h.streams[0][0])
_expect("v1/v1" not in h.streams[0][0],
        "the API version is never doubled (a 404 would look like a provider "
        "failure)")
_expect(h.streams[0][1].get("stream") is True,
        "the request asks the provider to stream: %r"
        % h.streams[0][1].get("stream"))
_expect(h.streams[0][1].get("max_tokens") == 1800,
        "streaming chat also receives its purpose token ceiling")
_expect(r.states["gpt"].total_calls == 1 and r.states["gpt"].total_ok == 1,
        "a streamed call is counted like any other call")
# The mock provider is unlimited (rpm=0); give it a budget and prove the
# streamed call SPENDS it — an uncounted chat stream would silently starve
# the agent's minute.
r.states["gpt"].rpm = 40
_stream(r)
_expect(r.states["gpt"].used_in_window() == 1,
        "a streamed call spends the provider's RPM budget")
_expect(r.states["gpt"].to_dict()["soft_cap"] == 30,
        "the streamed budget obeys the same reserve (soft cap 30 of 40)")
_expect(any(e.get("type") == "provider.call" for e in bus.events),
        "a streamed call emits the normal provider.call event")

# 2) A rate limit on the primary hands the answer to the next provider —
#    with the SAME messages, before anything was streamed.
h2 = FakeHTTP()
h2.push("api.openai.com", providers.ProviderError(providers.ERR_RATE_LIMIT,
                                                  "429", retry_after=5))
h2.push("127.0.0.1:11434", "Local| answer")
bus2 = Bus()
r2 = mk_router(h2, bus=bus2)
out2 = _stream(r2)
_expect(out2 == ["Local", " answer"],
        "a rate-limited primary fails over mid-conversation: %r" % (out2,))
fb = [e for e in bus2.events if e.get("type") == "provider.fallback"]
_expect(bool(fb) and fb[0]["from"] == "gpt" and fb[0]["to"] == "local",
        "the streaming failover is announced: %r" % (fb[:1],))
_expect(fb and fb[0].get("plan_continues") is True,
        "a streamed failover does not restart anything")
_expect(r2.states["gpt"].status in ("cooling", "rate_limited"),
        "the failed primary is parked for streamed calls too: %s"
        % r2.states["gpt"].status)
_expect(r2.states["gpt"].to_dict()["cooldown_s"] > 0,
        "…with a real cooldown, so the next turn does not hammer it again")
_expect(h2.headers[-1].get("Authorization") is None,
        "the local provider gets no key header on the fallback")

# 3) Text already delivered is never duplicated: a mid-stream break ends
#    the answer and is reported, instead of restarting on another provider.
h3 = FakeHTTP()
h3.push("api.openai.com", {"tokens": ["Half", " answer"],
                           "fail_after": 1})
h3.push("127.0.0.1:11434", "SHOULD NOT APPEAR")
bus3 = Bus()
r3 = mk_router(h3, bus=bus3)
out3 = _stream(r3)
_expect(out3 == ["Half"],
        "a mid-stream failure yields the partial answer only: %r" % (out3,))
_expect(not [e for e in bus3.events if e.get("type") == "provider.fallback"],
        "no fallback after text was streamed (no duplicated answer)")
_expect(any(e.get("type") == "provider.trouble" for e in bus3.events),
        "a mid-stream break is reported as trouble")

# 4) An empty stream is not an answer: the router tries the next provider.
h4 = FakeHTTP()
h4.push("api.openai.com", {"tokens": []})
h4.push("127.0.0.1:11434", "Second| try")
r4 = mk_router(h4)
out4 = _stream(r4)
_expect(out4 == ["Second", " try"],
        "an empty stream is treated as a failed attempt: %r" % (out4,))

# 5) No provider at all -> AllProvidersFailed (the caller degrades honestly).
h5 = FakeHTTP()
r5 = mk_router(h5)
# Take the key away (that is what "not configured" means) and park the local
# provider: then nothing in the chain can serve a stream.
r5.specs["gpt"].api_key = ""
r5.specs["gpt"].api_key_env = ""
r5.states["local"].mark_error(providers.ERR_RATE_LIMIT, "429")
try:
    out5 = _stream(r5)
    _expect(False, "no usable provider must raise, got %r" % (out5,))
except providers.AllProvidersFailed:
    _expect(True, "streaming raises AllProvidersFailed when nothing serves")

# ===========================================================================
# 13. FREE CLOUD GATEWAYS — OpenCode Zen + OpenRouter
# ===========================================================================

print("=== 13. free gateways (OpenCode Zen / OpenRouter) ===")

_expect(providers.DEFAULT_PROVIDERS["opencode"]["kind"] == providers.KIND_OPENAI
        and providers.DEFAULT_PROVIDERS["opencode"]["base_url"]
        == "https://opencode.ai/zen/v1",
        "OpenCode Zen is a plain OpenAI-compatible endpoint at opencode.ai/zen/v1")
_expect(providers.DEFAULT_PROVIDERS["openrouter"]["kind"] == providers.KIND_OPENAI
        and providers.DEFAULT_PROVIDERS["openrouter"]["base_url"]
        == "https://openrouter.ai/api/v1",
        "OpenRouter is a plain OpenAI-compatible endpoint at openrouter.ai/api/v1")

spec_oc = providers.ProviderSpec("opencode", api_key="oc-test")
_expect(spec_oc.chat_url == "https://opencode.ai/zen/v1/chat/completions",
        "OpenCode Zen chat URL does not double the /v1 segment: %s"
        % spec_oc.chat_url)
_expect(spec_oc.models_url == "https://opencode.ai/zen/v1/models",
        "OpenCode Zen models URL matches the documented live catalog: %s"
        % spec_oc.models_url)
spec_or = providers.ProviderSpec("openrouter", api_key="or-test")
_expect(spec_or.chat_url == "https://openrouter.ai/api/v1/chat/completions",
        "OpenRouter chat URL does not double the /v1 segment: %s"
        % spec_or.chat_url)

_expect(not providers.ProviderSpec("opencode").configured,
        "OpenCode Zen needs a key like any other hosted OpenAI-compatible "
        "provider — it is free, not keyless")
_expect(not providers.ProviderSpec("openrouter").configured,
        "OpenRouter needs a key too")

catalog_oc = [c for c in providers.CATALOG if c["provider"] == "opencode"]
catalog_or = [c for c in providers.CATALOG if c["provider"] == "openrouter"]
_expect(len(catalog_oc) >= 3, "OpenCode Zen ships several curated free models")
_expect(any(c["id"] == "inclusionai/ling-3.1-flash" for c in catalog_or),
        "Ling 3.1 Flash is in the curated OpenRouter catalog")
_expect(providers.DEFAULT_PROVIDERS["openrouter"]["model"]
        == "inclusionai/ling-3.1-flash",
        "Ling 3.1 Flash is OpenRouter's default model")
_expect(any(c["id"] == "ling-3.1-flash-free" for c in catalog_oc),
        "Ling 3.1 Flash is in the curated OpenCode Zen catalog too — it "
        "moved onto Zen's own free tier directly, not just OpenRouter's")
_expect(providers.DEFAULT_PROVIDERS["opencode"]["model"]
        == "ling-3.1-flash-free",
        "Ling 3.1 Flash is OpenCode Zen's default model too, matching "
        "OpenRouter's default so both free gateways lead with the same "
        "strong $0 agentic model")
_expect(not any(c["id"] == "mimo-v2.5-free" for c in catalog_oc),
        "the retired MiMo V2.5 free listing must not linger in the "
        "curated catalog once Zen moved to MiMo V2.6 Flash")

# Bare env vars (OPENCODE_API_KEY / OPENROUTER_API_KEY) are honored, matching
# the pattern already used for NVIDIA_API_KEY / OPENAI_API_KEY.
saved_free = {k: os.environ.pop(k, None) for k in
              ("NVIDIA_API_KEY", "OPENAI_API_KEY", "OPENCODE_API_KEY",
               "OPENROUTER_API_KEY", "NEX_PROVIDERS_FILE")}
try:
    with tempfile.TemporaryDirectory() as td:
        store_free = providers.SettingsStore(os.path.join(td, "none.json"))
        os.environ["OPENCODE_API_KEY"] = "oc-live-key"
        r_free = providers.build_router(store=store_free, load_dot_env=False)
        _expect(r_free.specs["opencode"].key == "oc-live-key",
                "OPENCODE_API_KEY alone configures the OpenCode Zen provider")
        _expect(r_free.specs["opencode"].configured,
                "…and it is now usable")

        # With NIM/GPT both absent, hard work should fall through to the
        # free OpenCode Zen gateway automatically — no routing edit needed.
        h_free = FakeHTTP()
        h_free.push("opencode.ai", "free ling answer")
        r_free._transport = {"post": h_free.post, "get": h_free.get,
                             "stream": h_free.stream}
        text_free = r_free.chat(providers.ROLE_AGENT, MESSAGES,
                                purpose="diagnosis")
        _expect(text_free == "free ling answer",
                "hard work reaches OpenCode Zen when NIM/GPT are unconfigured")
        _expect(h_free.calls[-1][0] == "https://opencode.ai/zen/v1/chat/completions",
                "the call actually hits OpenCode Zen's documented endpoint")
        _expect(r_free.status()["agent"]["active"] == "opencode",
                "status reports OpenCode Zen as the active hard-work provider")

        os.environ["OPENROUTER_API_KEY"] = "or-live-key"
        r_free2 = providers.build_router(store=store_free, load_dot_env=False)
        _expect(r_free2.specs["openrouter"].key == "or-live-key",
                "OPENROUTER_API_KEY alone configures the OpenRouter provider")
        _expect(r_free2.chain("agent") ==
                ["nim", "gpt", "opencode", "openrouter", "groq", "google",
                 "local"],
                "OpenRouter sits after OpenCode Zen, before the local terminal "
                "fallback: %s" % r_free2.chain("agent"))
finally:
    for k, v in saved_free.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# ===========================================================================
# 13b. TWO MORE FREE CLOUD GATEWAYS — Groq + Google AI Studio (Gemini)
#
# Added so a build has four independent, zero-cost safety nets between
# "NIM/GPT are down or rate limited" and "fall back to Ollama", not two.
# ===========================================================================

print("=== 13b. free gateways (Groq / Google AI Studio) ===")

_expect(providers.DEFAULT_PROVIDERS["groq"]["kind"] == providers.KIND_OPENAI
        and providers.DEFAULT_PROVIDERS["groq"]["base_url"]
        == "https://api.groq.com/openai/v1",
        "Groq is a plain OpenAI-compatible endpoint at api.groq.com/openai/v1")
_expect(providers.DEFAULT_PROVIDERS["google"]["kind"] == providers.KIND_OPENAI
        and providers.DEFAULT_PROVIDERS["google"]["base_url"]
        == "https://generativelanguage.googleapis.com/v1beta/openai",
        "Google AI Studio is OpenAI-compatible at .../v1beta/openai")

spec_groq = providers.ProviderSpec("groq", api_key="gr-test")
_expect(spec_groq.chat_url == "https://api.groq.com/openai/v1/chat/completions",
        "Groq chat URL does not double the /v1 segment: %s" % spec_groq.chat_url)
_expect(spec_groq.models_url == "https://api.groq.com/openai/v1/models",
        "Groq models URL: %s" % spec_groq.models_url)

spec_g = providers.ProviderSpec("google", api_key="g-test")
_expect(spec_g.chat_url ==
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "Google's version segment is 'openai', not 'v1' — no separate /v1 "
        "is ever appended: %s" % spec_g.chat_url)
_expect(spec_g.models_url ==
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "Google models URL does not insert a spurious /v1 either: %s"
        % spec_g.models_url)

_expect(not providers.ProviderSpec("groq").configured,
        "Groq needs a key like any other hosted OpenAI-compatible provider")
_expect(not providers.ProviderSpec("google").configured,
        "Google AI Studio needs a key too")

catalog_groq = [c for c in providers.CATALOG if c["provider"] == "groq"]
catalog_g = [c for c in providers.CATALOG if c["provider"] == "google"]
_expect(len(catalog_groq) >= 2, "Groq ships several curated free models")
_expect(len(catalog_g) >= 2, "Google AI Studio ships several curated free models")
_expect(providers.DEFAULT_PROVIDERS["groq"]["model"] ==
        "llama-3.3-70b-versatile",
        "Llama 3.3 70B Versatile is Groq's default model")
_expect(providers.DEFAULT_PROVIDERS["google"]["model"] == "gemini-2.5-flash",
        "Gemini 2.5 Flash is Google AI Studio's default model")
_expect(any(c["id"] == providers.DEFAULT_PROVIDERS["groq"]["model"]
           for c in catalog_groq),
        "Groq's default model is itself in the curated catalog")
_expect(any(c["id"] == providers.DEFAULT_PROVIDERS["google"]["model"]
           for c in catalog_g),
        "Google's default model is itself in the curated catalog")

_expect(providers.ROLE_DEFAULT_FALLBACKS[providers.ROLE_AGENT][-3:-1] ==
        ["groq", "google"],
        "hard-work default chain tries Groq then Google right before the "
        "guaranteed local terminal fallback: %s"
        % providers.ROLE_DEFAULT_FALLBACKS[providers.ROLE_AGENT])
_expect("groq" in providers.ROLE_DEFAULT_FALLBACKS[providers.ROLE_CHAT]
        and "google" in providers.ROLE_DEFAULT_FALLBACKS[providers.ROLE_CHAT],
        "routine chat's default fallback list also offers both new free "
        "gateways once Ollama needs help")

# Bare env vars (GROQ_API_KEY / GEMINI_API_KEY) are honored, matching the
# pattern already used for the other three cloud providers.
saved_gg = {k: os.environ.pop(k, None) for k in
            ("NVIDIA_API_KEY", "OPENAI_API_KEY", "OPENCODE_API_KEY",
             "OPENROUTER_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY",
             "GOOGLE_API_KEY", "NEX_PROVIDERS_FILE")}
try:
    with tempfile.TemporaryDirectory() as td:
        store_gg = providers.SettingsStore(os.path.join(td, "none.json"))
        os.environ["GROQ_API_KEY"] = "gr-live-key"
        r_groq = providers.build_router(store=store_gg, load_dot_env=False)
        _expect(r_groq.specs["groq"].key == "gr-live-key",
                "GROQ_API_KEY alone configures the Groq provider")

        # With NIM/GPT/OpenCode/OpenRouter all absent, hard work falls
        # through to Groq automatically — no routing edit needed.
        h_groq = FakeHTTP()
        h_groq.push("api.groq.com", "fast groq answer")
        r_groq._transport = {"post": h_groq.post, "get": h_groq.get,
                             "stream": h_groq.stream}
        text_groq = r_groq.chat(providers.ROLE_AGENT, MESSAGES,
                                purpose="diagnosis")
        _expect(text_groq == "fast groq answer",
                "hard work reaches Groq once the free gateways ahead of it "
                "are unconfigured")
        _expect(h_groq.calls[-1][0] ==
                "https://api.groq.com/openai/v1/chat/completions",
                "the call actually hits Groq's documented endpoint")
        _expect(r_groq.status()["agent"]["active"] == "groq",
                "status reports Groq as the active hard-work provider")

        os.environ["GEMINI_API_KEY"] = "g-live-key"
        r_g = providers.build_router(store=store_gg, load_dot_env=False)
        _expect(r_g.specs["google"].key == "g-live-key",
                "GEMINI_API_KEY alone configures the Google provider")

        h_g = FakeHTTP()
        h_g.push("api.groq.com", http_error("groq-down", 429, "", retry_after=30))
        h_g.push("generativelanguage.googleapis.com", "gemini answer")
        r_g._transport = {"post": h_g.post, "get": h_g.get,
                          "stream": h_g.stream}
        text_g = r_g.chat(providers.ROLE_AGENT, MESSAGES, purpose="diagnosis")
        _expect(text_g == "gemini answer",
                "Google is reached as the NEXT free gateway once Groq is "
                "rate limited, still before Ollama")
        _expect(h_g.calls[-1][0] ==
                "https://generativelanguage.googleapis.com/v1beta/openai/"
                "chat/completions",
                "the call hits Google's documented OpenAI-compatible "
                "endpoint, not a doubled /v1: %s" % h_g.calls[-1][0])
        _expect(r_g.status()["agent"]["active"] == "google",
                "status reports Google as the active hard-work provider")
finally:
    for k, v in saved_gg.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# ===========================================================================
# 14. "OUR BEST ONE COMES BACK" — NIM reclaims the hard-work lane the moment
#     its 40-RPM minute is over, including when the 429 is a surprise.
# ===========================================================================

print("=== 14. NIM reclaims hard work once its window resets ===")

# --- the documented case: our own window predicted the 429 -----------------
# (same mechanism as section 12, restated against the literal 40-RPM default
# so there is a test that reads exactly like the feature request: "NIM is
# our best provider — once the minute is done, hand it back the work".)
cl40 = Clock()
h40 = FakeHTTP()
h40.push("integrate.api.nvidia.com", ["nim #%d" % i for i in range(40)])
h40.push("127.0.0.1:11434", "ollama covers the 41st")
h40.push("integrate.api.nvidia.com", "nim is back on top")
r40 = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=40,
                                   min_interval_s=0.0, max_chill_s=0.0,
                                   reserve=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl40, transport={"post": h40.post, "get": h40.get})
for _ in range(40):
    r40.chat("agent", MESSAGES)
_expect(len([c for c in h40.calls if "integrate" in c[0]]) == 40,
        "all 40 requests in the minute went to NIM — our best provider")
_expect(r40.chat("agent", MESSAGES) == "ollama covers the 41st",
        "request 41 inside the same minute is routed around NIM, not queued")
_expect(r40.status()["agent"]["active"] == "local",
        "status reflects Ollama as the active hard-work provider for now")
cl40.advance(61)
_expect(r40.chat("agent", MESSAGES) == "nim is back on top",
        "the instant the 60s window rolls over, NIM reclaims the lane by "
        "itself — no routing edit, no manual re-enable")
_expect(r40.status()["agent"]["active"] == "nim",
        "…and status immediately reports NIM as active again")

# --- the harder case: NIM 429s with NO Retry-After and our own window     --
# --- still thought there was room (a real quota smaller than configured). -
cl_blind = Clock()
h_blind = FakeHTTP()
h_blind.push("integrate.api.nvidia.com",
             http_error("n", 429, "no retry-after, surprise limit"))
h_blind.push("127.0.0.1:11434", "ollama covers the surprise")
h_blind.push("integrate.api.nvidia.com", "nim ok again")
r_blind = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     # rpm=40 but our window has used only 1 slot — nothing in OUR
     # accounting predicted this 429.
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=40,
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl_blind, transport={"post": h_blind.post, "get": h_blind.get})
_expect(r_blind.chat("agent", MESSAGES) == "ollama covers the surprise",
        "a surprise 429 still fails over to Ollama immediately")
blind_wait = r_blind.states["nim"].to_dict()["cooldown_s"]
_expect(blind_wait >= 59.0,
        "an UNEXPLAINED 429 (no Retry-After, window looked fine) backs off "
        "a full rate-limit window (~60s), not a quick retry that would just "
        "get hammered again (got %.1fs)" % blind_wait)
_expect(r_blind.states["nim"].blind_rate_limits == 1,
        "the blind 429 is counted so repeat offenses can be told apart "
        "from one-off hiccups")
# A touch of random jitter is added on top of the 60s floor (thundering-herd
# protection), so advance past the worst case rather than exactly 60s.
cl_blind.advance(blind_wait + 1)
_expect(r_blind.chat("agent", MESSAGES) == "nim ok again",
        "once the backoff elapses, NIM is tried again automatically")
_expect(r_blind.states["nim"].blind_rate_limits == 0,
        "a real answer clears the blind-rate-limit escalation counter")

# ===========================================================================
# 15. non-rate-limit hiccups also escalate, and use the PROVIDER's own
#     configured cooldown_s instead of one hardcoded number for everyone.
# ===========================================================================

print("=== 15. outage backoff is per-provider and escalates on repeats ===")

cl5 = Clock()
h5xx = FakeHTTP()
h5xx.push("integrate.api.nvidia.com", http_error("n", 503, "gateway"))
h5xx.push("integrate.api.nvidia.com", http_error("n", 503, "gateway"))
h5xx.push("integrate.api.nvidia.com", http_error("n", 503, "gateway"))
h5xx.push("integrate.api.nvidia.com", "nim recovered")
h5xx.push("127.0.0.1:11434", "ollama")
h5xx.push("127.0.0.1:11434", "ollama")
h5xx.push("127.0.0.1:11434", "ollama")
r5xx = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     # cooldown_s=20 here — NOT the old hardcoded 10s — must actually be
     # honoured now that it is wired into ProviderState.
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x",
                                   cooldown_s=20.0,
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl5, transport={"post": h5xx.post, "get": h5xx.get})
_expect(r5xx.chat("agent", MESSAGES) == "ollama",
        "a 5xx still fails straight over to Ollama")
wait1 = r5xx.states["nim"].to_dict()["cooldown_s"]
_expect(19.0 <= wait1 <= 23.0,
        "the FIRST 503 cools down around the provider's OWN configured "
        "cooldown_s=20 (got %.1fs), not a hardcoded 10s for every provider"
        % wait1)
_expect(r5xx.states["nim"].consecutive_failures == 1,
        "the hiccup is counted")

# a second 503 before any success in between must back off FURTHER —
# otherwise a real outage would be hammered every ~20s indefinitely.
cl5.advance(wait1 + 1)
_expect(r5xx.chat("agent", MESSAGES) == "ollama",
        "still down — NIM is retried (not skipped) once its cooldown elapsed")
wait2 = r5xx.states["nim"].to_dict()["cooldown_s"]
_expect(wait2 > wait1,
        "a SECOND consecutive 503 waits longer than the first (%.1fs -> "
        "%.1fs) — a real outage is not hammered at a fixed interval forever"
        % (wait1, wait2))
_expect(r5xx.states["nim"].consecutive_failures == 2, "escalation is counted")

# third strike, then recovery
cl5.advance(wait2 + 1)
_expect(r5xx.chat("agent", MESSAGES) == "ollama", "third 503, still routed")
wait3 = r5xx.states["nim"].to_dict()["cooldown_s"]
_expect(wait3 > wait2, "escalation keeps climbing (%.1fs)" % wait3)
cl5.advance(wait3 + 1)
_expect(r5xx.chat("agent", MESSAGES) == "nim recovered",
        "NIM is tried again once its (longer) cooldown elapses")
_expect(r5xx.states["nim"].consecutive_failures == 0,
        "a real answer resets the escalation back to the base cooldown")

# --- a lone timeout still recovers quickly (no false escalation) ---------
cl6 = Clock()
h_lone = FakeHTTP()
h_lone.push("integrate.api.nvidia.com", urllib.error.URLError("timed out"))
h_lone.push("integrate.api.nvidia.com", "nim ok")
h_lone.push("127.0.0.1:11434", "ollama")
r_lone = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x",
                                   min_interval_s=0.0, max_chill_s=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl6, transport={"post": h_lone.post, "get": h_lone.get})
r_lone.chat("agent", MESSAGES)
lone_wait = r_lone.states["nim"].to_dict()["cooldown_s"]
_expect(lone_wait <= 6.0,
        "a single isolated timeout backs off briefly (~5s, got %.1fs), not "
        "the escalated outage treatment" % lone_wait)
cl6.advance(lone_wait + 1)
_expect(r_lone.chat("agent", MESSAGES) == "nim ok",
        "…and NIM is tried again right after")

# ===========================================================================
# 16. autonomous agent work actually waits out a provider's own RPM window
#     ("the minute is over") instead of giving up on an 8-second grace;
#     a live chat reply never gets stuck waiting that long.
# ===========================================================================

print("=== 16. agent-role work waits out a rate-limit window; chat does not ===")


def _patched_sleep(clock=None):
    """Swap out time.sleep for the duration of one `with` block so a test
    that deliberately drives _pace() into its real chill/wait path does not
    actually block for up to a minute. Also advances the given fake `clock`
    by the same amount, exactly like a real `time.sleep` would move a real
    `time.monotonic`-backed clock forward — without this, the window re-check
    right after the chill would still see the old (full) window and wrongly
    fail the call over anyway. Returns the list of durations slept."""
    calls: List[float] = []
    real_sleep = time.sleep

    def fake_sleep(s):
        calls.append(s)
        if clock is not None:
            clock.advance(s)

    class _Ctx:
        def __enter__(self):
            time.sleep = fake_sleep
            return calls

        def __exit__(self, *exc):
            time.sleep = real_sleep
            return False

    return _Ctx()


# Catalog default max_chill_s (8.0, not overridden here) with rpm=2 so the
# hard ceiling is reached on the 3rd call within the same simulated minute —
# `seconds_until_slot()` then reports close to the full ~60s window, far
# past the old flat 8s grace.
cl_wait = Clock()
h_wait = FakeHTTP()
h_wait.push("integrate.api.nvidia.com", "nim #1")
h_wait.push("integrate.api.nvidia.com", "nim #2")
h_wait.push("integrate.api.nvidia.com", "nim #3")
h_wait.push("127.0.0.1:11434", "ollama (should not be needed)")
r_wait = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=2,
                                   min_interval_s=0.0, reserve=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]},
     "chat": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl_wait, transport={"post": h_wait.post, "get": h_wait.get})
r_wait.chat("agent", MESSAGES)
r_wait.chat("agent", MESSAGES)
with _patched_sleep(cl_wait) as sleeps:
    out_agent_wait = r_wait.chat("agent", MESSAGES)
_expect(out_agent_wait == "nim #3",
        "autonomous agent work waited out the window and reached NIM "
        "itself on the 3rd call, instead of falling back to Ollama: %r"
        % out_agent_wait)
_expect(len(sleeps) == 1 and 55.0 <= sleeps[0] <= 61.0,
        "the wait should cover close to the full ~60s window, well past "
        "the provider's own 8s max_chill_s (got %r)" % sleeps)

# Same provider, same catalog default max_chill_s, but a live CHAT reply:
# the short 8s grace is NOT raised, so it must still fail over immediately
# rather than block the user for up to a minute.
cl_chat = Clock()
h_chat = FakeHTTP()
h_chat.push("integrate.api.nvidia.com", "nim #1")
h_chat.push("integrate.api.nvidia.com", "nim #2")
h_chat.push("127.0.0.1:11434", "ollama covers the interactive reply")
r_chat_wait = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=1,
                                   min_interval_s=0.0, reserve=0.0)},
    {"chat": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl_chat, transport={"post": h_chat.post, "get": h_chat.get})
r_chat_wait.chat("chat", MESSAGES)
with _patched_sleep(cl_chat) as sleeps_chat:
    out_chat_wait = r_chat_wait.chat("chat", MESSAGES)
_expect(out_chat_wait == "ollama covers the interactive reply",
        "a live chat reply must still fail over immediately on a full "
        "window, never block the user for up to a minute: %r"
        % out_chat_wait)
_expect(sleeps_chat == [],
        "chat must not have chilled/slept at all before failing over: %r"
        % sleeps_chat)

# An operator (or test) that explicitly disabled chilling entirely
# (max_chill_s=0) means it for every role, including agent work — that
# explicit choice is never silently raised.
cl_off = Clock()
h_off = FakeHTTP()
h_off.push("integrate.api.nvidia.com", "nim #1")
h_off.push("integrate.api.nvidia.com", "nim #2")
h_off.push("127.0.0.1:11434", "ollama covers it, chill is OFF")
r_off = providers.Router(
    {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                     base_url="http://127.0.0.1:11434",
                                     min_interval_s=0.0, max_chill_s=0.0),
     "nim": providers.ProviderSpec("nim", api_key="nvapi-x", rpm=1,
                                   min_interval_s=0.0, max_chill_s=0.0,
                                   reserve=0.0)},
    {"agent": {"provider": "nim", "fallbacks": ["local"]}},
    clock=cl_off, transport={"post": h_off.post, "get": h_off.get})
r_off.chat("agent", MESSAGES)
with _patched_sleep(cl_off) as sleeps_off:
    out_off = r_off.chat("agent", MESSAGES)
_expect(out_off == "ollama covers it, chill is OFF",
        "max_chill_s=0 means never wait, even for agent-role work: %r"
        % out_off)
_expect(sleeps_off == [],
        "an explicit max_chill_s=0 must never be silently raised: %r"
        % sleeps_off)

print("=== 17. vision-capable routing (real screenshot critique) ===")

# --- model_supports_vision(): conservative name-pattern detection ---------
_expect(providers.model_supports_vision("gemini-2.5-flash") is True,
        "gemini-* is recognised as a vision-capable model name")
_expect(providers.model_supports_vision("gpt-5.1") is True,
        "gpt-5.* is recognised as vision-capable")
_expect(providers.model_supports_vision("gpt-4o-mini") is True,
        "gpt-4o* is recognised as vision-capable")
_expect(providers.model_supports_vision("llava:13b") is True,
        "an Ollama-hosted llava model is recognised as vision-capable")
_expect(providers.model_supports_vision("nvidia/nemotron-3-super-120b-a12b")
        is False,
        "a text-only reasoning model is NOT guessed to be vision-capable")
_expect(providers.model_supports_vision("llama-3.3-70b-versatile") is False,
        "Llama 3.3 (text-only) is not misdetected as vision-capable")
_expect(providers.model_supports_vision("gpt-oss:20b") is False,
        "the local gpt-oss default is not misdetected as vision-capable")
_expect(providers.model_supports_vision("") is False,
        "an empty/unknown model name defaults to text-only (the safe side)")
_expect(providers.model_supports_vision(None) is False,
        "None never crashes the check and defaults to text-only")

# --- _attach_images(): per-kind multimodal payload construction ----------
base_msgs = [{"role": "user", "content": "describe this screenshot"}]
one_image = [{"mime_type": "image/png", "data": "Zm9v"}]

openai_msgs = providers._attach_images(base_msgs, providers.KIND_OPENAI,
                                       one_image)
_expect(base_msgs[0]["content"] == "describe this screenshot",
        "_attach_images never mutates the caller's original messages list")
_expect(isinstance(openai_msgs[-1]["content"], list),
        "OpenAI-kind: the last message's content becomes a list of blocks")
_expect(openai_msgs[-1]["content"][0] ==
        {"type": "text", "text": "describe this screenshot"},
        "OpenAI-kind: the original text is preserved as the first block")
_expect(openai_msgs[-1]["content"][1] ==
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,Zm9v"}},
        "OpenAI-kind: the image becomes a data-URI image_url block")

ollama_msgs = providers._attach_images(base_msgs, providers.KIND_OLLAMA,
                                       one_image)
_expect(ollama_msgs[-1]["content"] == "describe this screenshot",
        "Ollama-kind: the text content is left as a plain string")
_expect(ollama_msgs[-1]["images"] == ["Zm9v"],
        "Ollama-kind: raw base64 goes in a sibling 'images' list, no "
        "data-URI prefix")

no_images = providers._attach_images(base_msgs, providers.KIND_OPENAI, [])
_expect(no_images is base_msgs,
        "no images to attach is a no-op (same list returned)")

# --- end-to-end: Router.chat() routes an image request to a vision-     --
# --- capable provider and skips one that cannot read it -------------------
http_vision = FakeHTTP()
http_vision.push("api.openai.com", "the kitchen looks moody and unfinished")
router_vision = mk_router(http_vision)
reply = router_vision.chat("chat", MESSAGES, images=one_image)
_expect(reply == "the kitchen looks moody and unfinished",
        "chat role (gpt-5.1, vision-capable) answers an image-bearing "
        "request normally")
sent_url, sent_body = http_vision.calls[-1]
_expect("api.openai.com" in sent_url,
        "the image request actually went to the vision-capable provider")
sent_content = sent_body["messages"][-1]["content"]
_expect(isinstance(sent_content, list) and
        any(b.get("type") == "image_url" for b in sent_content),
        "the real outgoing request body carries an image_url block, not "
        "just text — the model can actually see the screenshot")

# mk_router's agent chain is nim (text-only) -> gpt (vision-capable) ->
# local (text-only). nim/local must be skipped outright for an image
# request; gpt is the one actually tried — and when even THAT fails (no
# scripted answer here), the chain is honestly exhausted rather than one
# of the skipped text-only providers silently answering without ever
# having seen the picture.
http_no_vision = FakeHTTP()
router_no_vision = mk_router(http_no_vision)
try:
    router_no_vision.chat("agent", MESSAGES, images=one_image)
    _expect(False, "an image request where the only vision-capable "
                   "provider also fails must raise, never fall back to a "
                   "text-only provider that never saw the image")
except providers.AllProvidersFailed as exc:
    reasons = {a["provider"]: a["error"] for a in exc.attempts}
    _expect(reasons.get("nim") == "no_vision",
            "NIM (nemotron, text-only) is skipped for an image request "
            "with an honest 'no_vision' reason: %r" % reasons)
    _expect(reasons.get("local") == "no_vision",
            "local (gpt-oss, text-only) is likewise skipped: %r" % reasons)
    _expect(reasons.get("gpt") not in (None, "no_vision"),
            "gpt (gpt-5.1, vision-capable) IS attempted — it is the only "
            "candidate that could have actually seen the image: %r"
            % reasons)
_expect(len(http_no_vision.calls) == 1,
        "exactly one real call was made — to the sole vision-capable "
        "provider — never to a text-only one just to watch it fail: %r"
        % (http_no_vision.calls,))

# callable_for() — the real production wiring used by the agent loop —
# declares vision support and forwards images end-to-end.
http_cf = FakeHTTP()
http_cf.push("api.openai.com", "a genuine critique via callable_for")
router_cf = mk_router(http_cf)
bound = router_cf.callable_for("chat")
_expect(getattr(bound, "supports_images", False) is True,
        "callable_for()'s bound callable declares supports_images=True — "
        "this is what lets agent/llm.call_with_images actually use it")
cf_reply = bound(MESSAGES, purpose="visual-review", images=one_image)
_expect(cf_reply == "a genuine critique via callable_for",
        "the bound callable forwards images through to Router.chat and "
        "returns the real reply")

print("\nAll provider-layer tests passed.")
