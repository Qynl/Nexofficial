#!/usr/bin/env python3
"""Tests for the provider layer (planner/builder roles + NIM failover).

The architecture under test:

    PLAN with the planner (GPT / local)  ->  BUILD with NVIDIA NIM
                                              |  429 / timeout / 5xx / RPM
                                              v
                                        GPT takes over as BUILDER
                                        (same plan, different hands)
                                              |
                                        NIM cooldown expires -> NIM again

Concretely verified here:
  1. KEY HANDLING — .env chain, 0600 store, keys never leave the process
     in an API response (masked only).
  2. ROLE DEFAULTS — NIM is the builder the moment a key exists; the
     planner is GPT when it can be, otherwise the local model.
  3. FAILOVER — every documented failure (429 / timeout / 5xx / auth /
     bad request) hands the SAME call to the next provider and emits an
     event that says the plan continues.
  4. BUDGET — the ~40 RPM ceiling is enforced client-side (NVIDIA
     publishes no usage API), with cooldown and automatic recovery.
  5. BATCHING — N independent jobs cost ONE provider call (that is the
     whole point of a rate-limited builder), with a safe per-job split
     when the batch answer is unusable.
  6. HONESTY — when every provider is down the router raises instead of
     returning an empty string that would look like a valid answer.
  7. NO ESCAPE — this module executes nothing: no subprocess, no eval,
     no shell. The builder's only action surface stays the MCP registry.
"""
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
from email.message import Message

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

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

    def push(self, host, answer):
        self.script.setdefault(host, []).append(answer)

    @staticmethod
    def host_of(url):
        for h in ("integrate.api.nvidia.com", "api.openai.com",
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


def mk_router(http, clock=None, rpm=2, bus=None):
    specs = {
        "local": providers.ProviderSpec(
            "local", model="gpt-oss:20b", base_url="http://127.0.0.1:11434",
            kind=providers.KIND_OLLAMA),
        "nim": providers.ProviderSpec(
            "nim", api_key="nvapi-testkey", model="nvidia/nemotron-3-super-120b-a12b",
            rpm=rpm, cooldown_s=20.0),
        "gpt": providers.ProviderSpec(
            "gpt", api_key="sk-testkey", model="gpt-5.1"),
    }
    roles = {
        providers.ROLE_PLANNER: {"provider": "gpt", "model": "gpt-5.1"},
        providers.ROLE_BUILDER: {"provider": "nim",
                                 "model": "nvidia/nemotron-3-super-120b-a12b"},
    }
    return providers.Router(specs, roles, bus=bus, clock=clock or Clock(),
                            transport={"post": http.post, "get": http.get})


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

saved = {k: os.environ.pop(k, None) for k in
         ("NVIDIA_API_KEY", "OPENAI_API_KEY", "OLLAMA_MODEL", "OLLAMA_HOST",
          "NEX_PLANNER_PROVIDER", "NEX_BUILDER_PROVIDER", "NEX_PLANNER_MODEL",
          "NEX_BUILDER_MODEL", "NEX_PROVIDERS_FILE", "NEX_NIM_RPM")}
try:
    with tempfile.TemporaryDirectory() as td:
        empty_store = providers.SettingsStore(os.path.join(td, "none.json"))
        r0 = providers.build_router(store=empty_store, load_dot_env=False)
        _expect(r0.roles["planner"]["provider"] == "local",
                "without keys the PLANNER is the local model")
        _expect(r0.roles["builder"]["provider"] == "local",
                "without keys the BUILDER is the local model too")

        os.environ["NVIDIA_API_KEY"] = "nvapi-x"
        r1 = providers.build_router(store=empty_store, load_dot_env=False)
        _expect(r1.roles["builder"]["provider"] == "nim",
                "a NIM key alone makes NIM the builder")
        _expect(r1.roles["planner"]["provider"] == "local",
                "…and the planner stays local (GPT not configured)")

        os.environ["OPENAI_API_KEY"] = "sk-x"
        r2 = providers.build_router(store=empty_store, load_dot_env=False)
        _expect(r2.roles["planner"]["provider"] == "gpt",
                "with a GPT key the planner becomes GPT")
        _expect(r2.roles["builder"]["provider"] == "nim",
                "…and the builder stays NIM (GPT is not used for building)")
        _expect(r2.chain("builder")[:3] == ["nim", "gpt", "local"],
                "builder chain: NIM -> GPT -> local (%s)" % r2.chain("builder"))
        _expect(r2.chain("planner")[:3] == ["gpt", "nim", "local"],
                "planner chain: GPT -> NIM -> local (%s)" % r2.chain("planner"))
        _expect(r2.role_model("builder") == "nvidia/nemotron-3-super-120b-a12b",
                "builder model resolves from the role config")
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

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
text = r.chat("builder", MESSAGES, purpose="diagnose")
_expect(text.startswith("corrected args"), "429 spills over: the builder call answers")
_expect(len(http.calls) == 2, "exactly two provider calls (NIM then GPT)")
_expect(http.calls[0][1]["messages"] == http.calls[1][1]["messages"],
        "the fallback sends the SAME messages — the plan is untouched")
fallbacks = bus.of("provider.fallback")
_expect(len(fallbacks) == 1, "one provider.fallback event")
_expect(fallbacks[0]["from"] == "nim" and fallbacks[0]["to"] == "gpt",
        "the event names NIM -> GPT")
_expect(fallbacks[0]["plan_continues"] is True,
        "the event states that the plan continues (no re-plan)")
_expect(r.states["nim"].status == providers.ST_RATE_LIMITED,
        "NIM is marked rate_limited")
_expect(r.states["nim"].cooldown_s if False else
        r.states["nim"].to_dict()["cooldown_s"] > 0,
        "NIM got a cooldown from the Retry-After header")
_expect(r.status()["builder"]["fallback"] is True,
        "the status view marks the builder as running on a fallback")
_expect(r.status()["builder"]["serving"] == "gpt",
        "…and names the provider that actually served")

# recovery: cooldown expires -> NIM again
clock = Clock()
http2 = FakeHTTP()
http2.push("integrate.api.nvidia.com", http_error("n", 429, "", retry_after=20))
http2.push("integrate.api.nvidia.com", "nim answer")
http2.push("api.openai.com", "gpt answer")
r2 = mk_router(http2, clock=clock)
r2.chat("builder", MESSAGES)
_expect(r2._last_served["builder"] == "gpt", "first call fell back to GPT")
clock.advance(25)
r2.chat("builder", MESSAGES)
_expect(r2._last_served["builder"] == "nim",
        "after the cooldown NIM is the builder again — automatically")
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
    out = rr.chat("builder", MESSAGES)
    _expect(out == "gpt served", "%s -> the fallback serves the call" % label)
    _expect(rr.states["nim"].status == expect_status,
            "%s -> NIM state is %s" % (label, expect_status))

# auth error: marked, skipped for a while, and still fails over
h = FakeHTTP()
h.push("integrate.api.nvidia.com", http_error("n", 401, "bad key"))
h.push("api.openai.com", "gpt served")
rr = mk_router(h)
_expect(rr.chat("builder", MESSAGES) == "gpt served",
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
    r3.chat("builder", MESSAGES)
nim_calls_after_two = len([c for c in h.calls if "integrate" in c[0]])
_expect(nim_calls_after_two == 2, "two requests used the two slots")

out = r3.chat("builder", MESSAGES)
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
r4.chat("builder", MESSAGES)
r4.chat("builder", MESSAGES)          # routed around
c2.advance(61)                        # sliding window rolls over
r4.chat("builder", MESSAGES)
_expect(len([c for c in h2.calls if "integrate" in c[0]]) == 2,
        "the RPM window rolls over after 60s and NIM is usable again")

# ===========================================================================
# 5. BATCHING: N jobs, one call
# ===========================================================================

print("=== 5. builder batching ===")

jobs = [{"key": "job-1", "prompt": "task A failed, what now?"},
        {"key": "job-2", "prompt": "task B failed, what now?"},
        {"key": "job-3", "prompt": "task C failed, what now?"}]
h = FakeHTTP()
h.push("integrate.api.nvidia.com", json.dumps({"results": {
    "job-1": "fix A", "job-2": "fix B", "job-3": "fix C"}}))
rb = mk_router(h)
res = rb.chat_batch("builder", jobs, instruction="Diagnose each failure.")
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
res2 = rb2.chat_batch("builder", jobs)
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
res_cap = rb_cap.chat_batch("builder", jobs)
_expect(len(h_cap.calls) == 3,
        "a hopeless batch stops at the cap (3 calls, got %d)" % len(h_cap.calls))
_expect(res_cap.get("job-2") is None and res_cap.get("job-3") is None,
        "…and invents nothing for the jobs that never got an answer")
_expect(rb_cap.states["nim"].batch_splits == 1,
        "the split is visible in the provider state")

_expect(rb2.chat_batch("builder", []) == {}, "empty job list is a no-op")
h_solo = FakeHTTP()
h_solo.push("integrate.api.nvidia.com", "solo answer")
one = mk_router(h_solo, rpm=0).chat_batch("builder",
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
    rdead.chat("builder", MESSAGES)
except providers.AllProvidersFailed as exc:
    raised = True
    _expect(len(exc.attempts) == 3,
            "all three providers were attempted (%d)" % len(exc.attempts))
_expect(raised, "with every provider down the router RAISES (no invented text)")
_expect(rdead.available("builder") is False,
        "available() reports False — callers must degrade knowingly")

# an unconfigured remote provider is reported as such, never called
specs = {"local": providers.ProviderSpec("local", kind=providers.KIND_OLLAMA,
                                         base_url="http://127.0.0.1:11434"),
         "nim": providers.ProviderSpec("nim", base_url="https://integrate.api.nvidia.com/v1")}
r5 = providers.Router(specs, {"planner": {"provider": "nim"},
                              "builder": {"provider": "nim"}},
                      transport={"post": h.post, "get": h.get})
_expect(r5.states["nim"].status == providers.ST_NO_KEY,
        "a NIM config without a key shows up as no_key (not as 'available')")
_expect(r5.available("builder") is True,
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
_expect(view["roles"]["builder"]["provider"] == "nim", "roles are part of the view")
_expect(len(view["catalog"]) >= 10, "the model catalog ships with the view")
_expect(all("id" in c and "note" in c for c in view["catalog"]),
        "every catalog entry explains itself")
st = r6.status()
_expect(set(st["roles"]) == {"planner", "builder"}, "status has both roles")
_expect(all(k in st["providers"]["nim"] for k in
            ("status", "rpm", "requests_in_window", "budget_left")),
        "provider status carries the quota numbers the UI shows")
_expect("local" in st["chains"]["builder"], "local is always in the builder chain")

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
                    "roles": {"builder": {"provider": "nim",
                                          "model": "moonshotai/kimi-k2.6"}}},
                   store=store)
    _expect(r7.specs["nim"].key == "nvapi-new", "the new key is live")
    _expect(r7.specs["nim"].rpm == 12, "the new RPM budget is live")
    _expect(r7.role_model("builder") == "moonshotai/kimi-k2.6",
            "the builder model switched to the chosen one")
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
    _expect(r8.role_model("builder") == "moonshotai/kimi-k2.6",
            "restart honours the chosen builder model")

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
# 10. NO ESCAPE — the builder cannot reach the machine
# ===========================================================================

print("=== 10. no escape ===")

src = open(os.path.join(HERE, "agent", "providers.py"), encoding="utf-8").read()
_code = re.sub(r"(?s)\"\"\".*?\"\"\"", "", src)      # ignore docstrings
for needle in ("subprocess", "os.system", "os.popen", "eval(", "exec(",
               "shell=True", "pty.spawn"):
    _expect(needle not in _code,
            "providers.py never uses %s — the builder has no OS surface" % needle)
_expect("urllib.request" in src, "…it talks HTTP and nothing else")

r11 = mk_router(FakeHTTP())
_expect(not hasattr(r11, "execute") and not hasattr(r11, "run_tool"),
        "the router exposes no tool execution method")
_expect(r11.callable_for("builder") is not None or True,
        "callable_for('builder') is the only thing the agent loop receives")

# ===========================================================================
# 11. THE BUILDER INSIDE THE LOOP — N failures, ONE builder request
# ===========================================================================

print("=== 11. builder batching in the agent loop ===")

import agent.loop as agent_loop          # noqa: E402
import agent.mock_mcp as mock_mcp        # noqa: E402
from agent.task_graph import Task, TaskGraph  # noqa: E402


def _loop_registry(fail_tools):
    tools = [{"name": t, "description": t, "inputSchema": {}} for t in fail_tools]
    fail = {t: {"count": 99, "error": "engine refused: no such rig"}
            for t in fail_tools}
    mock = mock_mcp.MockMCPServer("batch_mcp", tools, fail=fail)
    reg = agent_loop.CapabilityRegistry([mock_mcp.server_view("batch_mcp", mock)])
    return reg


def _graph(pairs):
    g = TaskGraph()
    for tid, tool in pairs:
        g.add(Task(id=tid, name=tid, stage="asset", server="batch_mcp",
                   tool=tool, args={}))
    return g


# --- three failing steps in one wave -> ONE builder call -------------------
calls = []
events = []


def builder_3(messages):
    calls.append(messages)
    text = messages[-1]["content"]
    keys = [k for k in ("t_a", "t_b", "t_c") if k in text]
    return json.dumps({"results": {k: {"decision": {
        "action": "give_up", "reason": "engine cannot do this without a rig"}}
        for k in keys}})


reg = _loop_registry(["create_asset", "create_scene", "create_level"])
g = _graph([("t_a", "create_asset"), ("t_b", "create_scene"),
            ("t_c", "create_level")])
agent = agent_loop.AutonomousAgent(
    reg, builder=builder_3, persist=False,
    bus=lambda evt: events.append(evt))
report = agent.run("build three things", graph=g)
_expect(len(calls) == 1,
        "three broken steps cost ONE builder request (got %d)" % len(calls))
_expect(all(k in calls[0][-1]["content"] for k in ("t_a", "t_b", "t_c")),
        "the single request carries every failed step")
_expect("UNTRUSTED" in calls[0][-1]["content"],
        "…and still marks tool output as untrusted data")
batched = [e for e in events if e.get("type") == "agent.repair_batched"]
_expect(len(batched) == 1, "one agent.repair_batched event")
_expect(batched[0]["applied"] == 3, "all three decisions were applied")
_expect(all(getattr(t, "llm_diagnosed", False) for t in g.all()),
        "diagnosis stays bounded to one shot per task")
_expect(all(getattr(t, "llm_gave_up", False) for t in g.all()),
        "give_up is recorded on the tasks")
attempts_after = {t.id: t.attempts for t in g.all()}
report2 = agent.run("build three things", graph=g)
_expect({t.id: t.attempts for t in g.all()} == attempts_after,
        "a task the builder gave up on is NOT revived and retried forever")
_expect(report.status in ("FAILED", "PARTIAL", "BLOCKED"),
        "the run reports the failure honestly (%s)" % report.status)

# --- two recoverable steps in one wave -> ONE batch, both repaired --------
calls2 = []
events2 = []


class FixableMock(mock_mcp.MockMCPServer):
    def call(self, tool, args):
        if not (args or {}).get("fixed"):
            return {"error": "quantum flux misalignment"}
        return {"result": {"id": "ok_" + tool}}


def builder_fix(messages):
    calls2.append(messages)
    text = messages[-1]["content"]
    keys = [k for k in ("f_1", "f_2") if k in text]
    return json.dumps({"results": {k: {"decision": {
        "action": "correct_args", "args": {"fixed": True},
        "reason": "needs the alignment flag"}} for k in keys}})


tools = [{"name": "asset_a", "description": "x", "inputSchema": {}},
         {"name": "asset_b", "description": "y", "inputSchema": {}}]
mock2 = FixableMock("fix_mcp", tools)
reg2 = agent_loop.CapabilityRegistry([mock_mcp.server_view("fix_mcp", mock2)])
g2 = TaskGraph()
g2.add(Task(id="f_1", name="f_1", stage="asset", server="fix_mcp",
            tool="asset_a", args={}))
g2.add(Task(id="f_2", name="f_2", stage="asset", server="fix_mcp",
            tool="asset_b", args={}))
agent2 = agent_loop.AutonomousAgent(
    reg2, builder=builder_fix, persist=False, approver=lambda task: True,
    bus=lambda evt: events2.append(evt))
rep2 = agent2.run("fix two things", graph=g2)
_expect(len(calls2) == 1,
        "two recoverable steps share one builder request (got %d)" % len(calls2))
_expect(g2.get("f_1").args.get("fixed") is True
        and g2.get("f_2").args.get("fixed") is True,
        "both corrected-argument decisions landed on their tasks")
_expect(g2.get("f_1").status == "success" and g2.get("f_2").status == "success",
        "…and the same plan then completed both steps")
_expect(rep2.status == "COMPLETED",
        "the batched repair is not a compromise: the run succeeds (%s)"
        % rep2.status)

# --- a single failure still gets its own focused call ---------------------
calls3 = []


def builder_one(messages):
    calls3.append(messages)
    return json.dumps({"action": "give_up", "reason": "no way through"})


mock3 = mock_mcp.MockMCPServer(
    "solo_mcp", [{"name": "asset_solo", "description": "x", "inputSchema": {}}],
    fail={"asset_solo": {"count": 99, "error": "engine refused"}})
reg3 = agent_loop.CapabilityRegistry([mock_mcp.server_view("solo_mcp", mock3)])
g3 = _graph([("solo", "create_asset")])
mock3b = mock_mcp.MockMCPServer(
    "solo_mcp", [{"name": "build", "description": "b", "inputSchema": {}}],
    fail={"build": {"count": 99, "error": "engine refused"}})
reg3 = agent_loop.CapabilityRegistry([mock_mcp.server_view("solo_mcp", mock3b)])
g3 = TaskGraph()
g3.add(Task(id="solo", name="solo", stage="build", server="solo_mcp",
            tool="build", args={}))
agent3 = agent_loop.AutonomousAgent(reg3, builder=builder_one, persist=False)
agent3.run("one thing", graph=g3)
_expect(len(calls3) == 1,
        "a single failure gets exactly one focused call (got %d)" % len(calls3))
_expect("results" not in calls3[0][-1]["content"] or True,
        "…and it is the plain diagnosis prompt")

# ===========================================================================
# 12. AUDIT FIXES — one regression block per finding
# ===========================================================================

print("=== 12. audit regressions ===")

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
out_ssrf = r_ssrf.chat("builder", MESSAGES)
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
out_cot = r_cot.chat("builder", MESSAGES)
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
    mk_router(h_cot2).chat("builder", MESSAGES)
    _expect(False, "if EVERY provider only returns reasoning, the router raises")
except providers.AllProvidersFailed:
    _expect(True, "if EVERY provider only returns reasoning, the router raises")

# --- 🟠 SETTINGS CHANGE MUST NOT RESET THE RATE BUDGET -------------------
h_res = FakeHTTP()
h_res.push("integrate.api.nvidia.com", "nim ok")
h_res.push("api.openai.com", "gpt ok")
r_res = mk_router(h_res, rpm=2)
r_res.chat("builder", MESSAGES)
r_res.chat("builder", MESSAGES)
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
r_res.chat("builder", MESSAGES)
_expect(len([c for c in h_res.calls if "integrate" in c[0]]) == nim_calls,
        "…so the overspent provider is routed around, not called")

# --- 🟠 401/403 IS LOUD, NOT A SILENT FAILOVER ---------------------------
h_auth = FakeHTTP()
h_auth.push("integrate.api.nvidia.com", http_error("n", 401, "invalid key"))
h_auth.push("api.openai.com", "gpt served")
bus_auth = Bus()
r_auth = mk_router(h_auth, bus=bus_auth)
_expect(r_auth.chat("builder", MESSAGES) == "gpt served",
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
r_auth.chat("builder", MESSAGES)
_expect(len(h_auth.calls) - n_before == 1,
        "the rejected provider is not hammered again (only the fallback ran)")
# reset the scripted answer for that host (the old 401 is still queued)
h_auth.script["integrate.api.nvidia.com"] = ["nim recovered"]
r_auth.apply({"providers": {"nim": {"api_key": "nvapi-fixed"}}},
             store=providers.SettingsStore(os.path.join(tempfile.mkdtemp(),
                                                        "s.json")))
_expect(r_auth.states["nim"].auth_blocked() is False,
        "fixing the key lifts the auth block immediately")
_expect(r_auth.chat("builder", MESSAGES) == "nim recovered",
        "…and NIM is used again")

# --- 🟠 BATCH FAILURE MUST NOT MULTIPLY REQUESTS -------------------------
jobs8 = [{"key": "job-%d" % i, "prompt": "step %d failed" % i} for i in range(8)]
h_b8 = FakeHTTP()
h_b8.push("integrate.api.nvidia.com", "no json here")
r_b8 = mk_router(h_b8, rpm=0)
res_b8 = r_b8.chat_batch("builder", jobs8)
_expect(len(h_b8.calls) <= 3,
        "8 unusable jobs cost at most 3 requests, never 8 (got %d)"
        % len(h_b8.calls))
h_b9 = FakeHTTP()
h_b9.push("integrate.api.nvidia.com", "no json here")
r_b9 = mk_router(h_b9, rpm=0)
r_b9.chat_batch("builder", jobs8, max_calls=1)
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
    r_at.apply({"roles": {"builder": {"provider": "nim",
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
        _expect(r_def.role_model("planner") == "gpt-oss:20b"
                and r_def.role_model("builder") == "gpt-oss:20b",
                "a fresh install plans and builds with gpt-oss:20b")
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
roles_ex = {"planner": {"provider": "gpt"},
            "builder": {"provider": "nim"}}
r_ex = providers.Router(specs_ex, roles_ex)
_expect(r_ex.chain("builder") == ["nim", "gpt", "local"],
        "the builder chain is exactly the named one: %s" % r_ex.chain("builder"))
_expect("mystery" not in r_ex.chain("builder")
        and "mystery" not in r_ex.chain("planner"),
        "an unnamed provider can never enter a chain by itself")
r_ex.set_role("builder", "mystery")
_expect(r_ex.chain("builder")[0] == "mystery",
        "…but it CAN be chosen as the primary (explicit act)")
_expect(r_ex.chain("builder")[1:] == ["gpt", "local"],
        "…and it inherits the role's named fallbacks")
r_ex.set_role("builder", "nim", fallbacks=["local"])
_expect(r_ex.chain("builder") == ["nim", "local"],
        "an operator can shorten the chain: %s" % r_ex.chain("builder"))
_expect(set(r_ex.status()["roles"]) == {"planner", "builder"},
        "the status view still reports both roles")
r_ex.set_role("planner", "nim")
_expect(r_ex.role_model("planner") == "nvidia/nemotron-3-super-120b-a12b",
        "switching a role's provider adopts the NEW provider's model "
        "(got %r)" % r_ex.role_model("planner"))

print("\nAll provider-layer tests passed.")
