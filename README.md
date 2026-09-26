# Nex

**A personal agent with a face — stdlib Python, zero build steps, and no
silent access to your computer.**

Nex is two things:

* a **presence** — a WebGL face that listens, thinks, speaks and works,
  instead of a spinner; and
* an **agent** — plan → act → observe → evaluate → adapt, with recovery
  and honest completion, over whatever MCP servers you connect.

It runs on the Python standard library (`python3 server.py`, no pip, no
node_modules), talks to a local Ollama or any OpenAI-compatible cloud
model, and its *only* way to act on the world is the MCP capability
layer you configure. There is no shell tool, no filesystem tool, no
"run this code" tool — and no flag that adds one.

```
cd nex
python3 server.py        # → http://localhost:8787
```

---

## 1. The one rule

**Nex acts only through MCP servers you connect, through one code path,
gated by policy.**

```
browser ──cookie auth + SSE──▶ server.py
                                  │
                               agent/  (plan → act → observe → evaluate → adapt)
                                  │
                               mcp/manager.call()  ← THE action path
                                  │
                               mcp/policy.authorize()  (classification + content scan)
                                  │
                               mcp/transport  ──HTTP──▶  any MCP server
                                              └─stdio─▶  any MCP server
```

What that means concretely:

* **No local tool surface.** `tools.py` does not exist. The model's
  every capability is a tool on a server *you* added. A server that
  controls a game engine, a 3D editor, or a smart home is the same
  object to Nex — it discovers the tools live and never assumes a
  vocabulary.
* **The boundary is structural, not prompt-based.** `mcp/policy.py`
  hardcodes `MCP_ONLY = True`; internal tool names are refused
  categorically; shell-shaped tool names (`run_command`, `exec`,
  `spawn_shell`) are denied even when an MCP server exposes them.
* **Arguments are scanned, not just names.** OS primitives in code
  payloads, sensitive paths (`~/.ssh`, `/etc`, `~/.nex` itself),
  traversal sequences and credential shapes (`sk-…`, `AKIA…`, private
  keys, `password=…`) are refused outright — never offered for
  approval.
* **Connected ≠ trusted.** Untrusted servers require confirmation for
  every tool and are refused outright in autonomous runs. Destructive,
  network, unknown and code-execution tools always need a human (or a
  standing, per-server operator approval that can never cover an escape
  payload).
* **Every action is audited.** `mcp/audit.py` records each call,
  refusal and confirmation with duration and context; the UI shows the
  log.
* **Subprocess spawning exists in exactly one file** — `mcp/transport.py`,
  the stdio MCP transport (that is what a stdio MCP server *is*). The
  command must be a single executable token; shell syntax is rejected
  and `NEX_STDIO_ALLOW` can pin the allowlist.

## 2. Quick start

```
cd nex
python3 server.py
```

Open the printed URL (the one-time token link sets an HttpOnly cookie;
the login card accepts the token too). Then:

1. **Connect a capability server** — Capabilities → Add server. An HTTP
   endpoint (`http://127.0.0.1:9876/mcp`) or a local command
   (`npx -y mcp-server-anything`). Nex discovers its tools live.
2. **Talk.** Plain chat uses the chat role's provider chain. When a
   message means *doing* something, Nex plans a run: the run card shows
   the plan, each step, each tool call with timing, and asks for
   approval where the policy demands it.
3. **Watch the face.** Idle, listening (real mic level), thinking,
   speaking (synced to the reply), working — it reflects the actual
   pipeline state, driven by the SSE event stream.

Without any server connected Nex is still a complete chat client with
markdown, code blocks, voice and persistence — and it says honestly
that it cannot act.

## 3. The agent loop

One run is `plan → waves of steps → evaluate → adapt`, in
`agent/loop.py`:

* **Planning is model-driven against the live catalog.** The planner
  sees a relevance-filtered tool list (huge MCP catalogs are context
  poison) and may name any tool — the plan is then validated against the
  full registry: hallucinated tools and wrong servers are dropped and
  reported, never executed. Without a model, a deterministic fallback
  plans only what lexically *is* the request, and refuses the rest
  (`blocked`, with the capability summary) rather than pretending.
* **Steps chain through `$references`** — step B can use step A's
  result (`{"id": "$Make.id"}`). A reference that cannot be resolved is
  a hard step failure; the raw `$name` string is never sent to a server.
* **Recovery is a ladder**: retry transient → fix arguments
  (deterministic first, then one model diagnosis — repair args or switch
  to an alternative tool in the same category) → honest failure. A
  failed step is reported as failed; nothing is ever narrated as done.
* **Evaluation is a separate call** with its own prompt: done / continue
  / replan / stop, judged against observed evidence, not against
  intentions. Structural failure triggers a bounded replan that carries
  what the previous attempt taught.
* **Budgets end runs, they don't hang them**: step cap, replan cap,
  wall-clock cap, approval timeout. Cancellation is immediate, even
  mid-tool-call.
* **Completion is honest.** `completed` means every step succeeded
  (or was verified); `partial` names what failed and why; `blocked`
  explains what capability was missing. Run reports contain no model
  chain-of-thought.

## 4. Providers

Two roles — **chat** (answers you) and **agent** (plans, evaluates,
repairs) — each with an explicit failover chain. Providers are abstract
(Ollama locally, NVIDIA NIM, any OpenAI-compatible endpoint); the local
model is always the terminal fallback so "the cloud is rate-limited" is
never a dead end.

* Rate limiting is paced client-side (`NEX_NIM_RPM`), failures back off
  and recover on their own, and the UI chip shows who is *really*
  answering right now — including after a failover.
* API keys live server-side (`~/.nex/providers.json`, 0600), are bound
  to the host they were entered for (point the endpoint elsewhere and
  the key stops being sent), and never reach the browser — the API
  returns them masked.

Configure in `.env` (see `nex/.env.example`) or in Settings → Model
with live model lists and a connection test.

## 5. The interface

* **The face** — one WebGL canvas (SDF rounded rectangles, no images),
  two poses: hero when the conversation is empty, compact in the topbar
  afterwards. State comes from the pipeline (SSE), never from guesses;
  speech is synced to replies; dictation drives the listening state
  with real microphone amplitude; mouse attention only while idle.
* **Run cards** — the agent's work made visible without exposing
  chain-of-thought: the goal, the phase (Planning → Executing →
  Evaluating → …), every step with status, every tool call with name,
  preview and duration, approvals inline with approve-once /
  always-allow / deny, and a final verdict.
* **Chat** — streaming markdown (sanitized DOM rendering, no raw HTML
  ever), code blocks with copy, message actions (copy / listen /
  regenerate / edit-and-resend), conversation search, auto-titles,
  SQLite persistence.
* **Capabilities** — the MCP server panel: connect, disconnect,
  reconnect, trust, remove, inspect every tool with its schema and
  policy classification, live status with errors and latency.
* **Errors are a designed surface.** User, model, MCP, network, config,
  internal and timeout errors each get a friendly name and a suggested
  next step; the technical detail is logged and shown, never swallowed.

## 6. Tests

```
cd nex
python3 tests/run_all.py
```

Nine suites, each isolated in its own process, ~4.5 s total:

| suite | covers |
| --- | --- |
| `test_architecture` | dependency direction; no local tool surface; no Minecraft; subprocess confined to the transport; `MCP_ONLY` structural |
| `test_capability` | tool classification, severity-max, confirmation gates |
| `test_transport` | NDJSON + LSP framing, fragmented reads, stdio child lifecycle, HTTP round-trips, garbage responses |
| `test_manager` | server lifecycle, validation, calls through policy, approvals, trust, audit, persistence |
| `test_agent_loop` | planning + validation, `$references`, recovery ladder, budgets, cancellation, approval flow, report safety |
| `test_store` | conversations, messages, search, regenerate truncation, persistence |
| `test_server_api` | auth, CSRF, login, static serving, path traversal, chat pipeline, SSE, real MCP server over HTTP |
| `test_escape` | the adversarial boundary: payload scans, malicious servers, prompt injection in results, config attacks, HTTP surface, no second action path |
| `test_providers` | routing, failover, pacing, key/host binding, roles |

The mandate for `test_escape.py` is the one that matters most: it tries
every route from the model to unrestricted PC capabilities it can find,
and the architecture test makes sure no new one appears.

## 7. Files

```
nex/
├── server.py              stdlib HTTP server: auth + CSRF, REST, SSE,
│                          chat pipeline, run coordination
├── store.py               SQLite conversations + messages (WAL)
├── mcp/
│   ├── manager.py         ServerManager: lifecycle + THE action path
│   ├── policy.py          MCP_ONLY, classification, argument scan, approvals
│   ├── capability.py      tool → capability heuristics (+ operator pins)
│   ├── registry.py        the live tool catalog
│   ├── transport.py       HTTP + stdio MCP transports (only subprocess user)
│   └── audit.py           the action log
├── agent/
│   ├── loop.py            AgentRun: plan → waves → evaluate → adapt
│   ├── model_planner.py   model-driven planning + validation
│   ├── planner.py         honest deterministic fallback
│   ├── task_graph.py      tasks, deps, statuses
│   ├── context.py         bounded run memory
│   ├── diagnose.py        failure diagnosis → repair decision
│   ├── prompts.py         persona, boundary, role prompts
│   ├── events.py          the event vocabulary
│   ├── jsonreply.py       robust JSON extraction
│   ├── providers.py       roles, chains, pacing, failover, key binding
│   └── mock_mcp.py        in-process MCP server for tests
├── web/                   the client: index.html, css/, js/
│                          (face engine: js/webgl.js + js/animations.js)
└── tests/                 the nine suites + tests/run_all.py
```

## 8. Honest limits

* **Voice is the browser's.** Speech synthesis and recognition use the
  Web Speech API; browsers without it simply hide the buttons. The
  quality is the platform's, not Nex's.
* **The MCP servers are third-party code you chose to trust.** Nex
  refuses what it can see (payloads, paths, credentials, unknown
  classifications), but a malicious server can lie about what a tool
  does; untrusted mode + approvals is the mitigation for that.
* **The deterministic fallback planner is deliberately minimal.**
  Without a model, Nex plans only what a request directly names. Better
  judgment is what the model is for; faking it in code produced
  confident garbage.
* **Autonomous ≠ unattended.** The default posture asks before
  anything destructive; the operator levers (`NEX_TRUSTED_SERVERS`,
  `NEX_ALLOW_CODE_EXECUTION`) exist for people who have read this
  paragraph.

---

Personal project. The face is the interface; the boundary is the
product.
