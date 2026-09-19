# NEX

A minimal, dependency-free visual + local runtime for the personal AI
assistant **NEX**.

The resting face is two white rounded rectangles on a pure black background.
The personality comes entirely from the animation engine.

## Run

```
cd nex
python server.py
```

Then open:

```
http://localhost:8787
```

Requires only the Python standard library.

## Security

The HTTP API is **always authenticated** — there is no unauthenticated mode
and no cross-origin (`Access-Control-Allow-Origin: *`) escape hatch.

- **Token source (in order):** the `NEX_AUTH_TOKEN` environment variable,
  else a token file (default `~/.nex/server_token`, path overridable with
  `NEX_TOKEN_FILE`). On first boot a random token is created and persisted
  with `0600` permissions; it is stable across restarts.
- **Browser auth = HttpOnly cookie.** Open the banner link
  `http://localhost:8787/?nex_token=<token>` once: the server validates the
  token, sets `nex_auth` with `HttpOnly; SameSite=Strict; Path=/` (plus
  `Secure` on https origins, or always with `NEX_COOKIE_SECURE=1`) and
  302-redirects to a clean URL. Every same-origin `/api` + `/mcp` request
  (including `EventSource`) then carries the cookie automatically.
  The token is **never** serialized into served HTML/JS — no
  `window.NEX_AUTH` global — so an XSS bug cannot read the credential and
  chain into MCP/stdio.
- **CLI / programmatic clients** use the `X-Nex-Auth` request header or
  `Authorization: Bearer <token>`; the headerless legacy SSE client may
  use `?nex_auth=<token>` on `GET /api/events` only (masked in the access
  log, never honored on any other path).
- **TRUSTED SERVER REGISTRY (strict mode).** `CONNECTED != TRUSTED`: the
  deployed server starts with `strict_servers=True`, so an external tool
  call passes only if its server is in the trusted set = built-in catalog
  servers + `NEX_TRUSTED_SERVERS` (comma-separated, operator environment).
  Connecting a tunnel does not make it callable; the model can never
  extend the registry. `NEX_STRICT_SERVERS=0` restores legacy permissive
  mode (development only).
- **HTTP MCP endpoint boundary (SSRF).** `POST /api/tunnels` accepts
  `http(s)` endpoints on loopback (`127.0.0.1` / `localhost` / `::1`)
  plus hosts listed in `NEX_HTTP_ALLOW`; cloud-metadata/LAN/arbitrary
  hosts are a `400`, and a poisoned `~/.nex/tunnels.json` row is dropped
  (fail-closed) on reload.
- **Stdio MCP children are allowlisted.** `POST /api/tunnels` will only
  start a stdio MCP server whose command is a built-in editor entrypoint or
  explicitly listed in the `NEX_STDIO_ALLOW` environment variable (a
  comma-separated list). Anything else is rejected with a `400`, and a
  poisoned `~/.nex/tunnels.json` is dropped (never executed) on reload.
  The HTTP API can never widen the list at runtime.
- **MCP servers are untrusted peers.** Tool annotations
  (`readOnlyHint` etc.) are treated as *untrusted hints* — they may only
  RAISE caution, never lower it (severity-max with the name heuristic,
  so a tool named `delete_project` claiming `readOnlyHint: true` is
  still classified destructive). Unclassifiable tools (UNKNOWN) require
  confirmation by default; external tools named `execute_command` /
  `exec` always require confirmation; `run_command` (Nex's own shell)
  is never authorized on any server. Policy matching uses the bare
  tool name, so a connected server cannot slip a tool through by name
  collision (`evilserver.run_command` is still denied).
- **Operator capability registry** (`NEX_CAPABILITY_FILE`, default
  `~/.nex/capabilities.json`) pins specific tools where the heuristics +
  untrusted annotations are not enough:
  `{"server": {"tool": {"category": "destructive",
  "requires_confirmation": true}}}`. Same severity-max rule as
  annotations: a pin can only RAISE caution, never downgrade a tool. The
  file is operator infrastructure — the model never reads or writes it.
- **Three classification layers** combine into the effective capability:
  name heuristics + MCP annotations + the operator registry, with the
  MORE dangerous value winning at every step.
- **The SSE query token** (`?nex_auth=`) is only honored on
  `/api/events` — nowhere else. All token comparisons are constant-time
  (`hmac.compare_digest`).
- **`test_escape.py` — the adversarial end-to-end proof.** It makes an
  LLM-style attacker try to escape MCP through every reachable path
  (internal tool names, `__internal__` prefixes, ghost servers, REST
  shim, plan validation, LLM diagnosis, tunnel registration, SSRF URLs,
  stdio commands, untrusted-but-connected servers, cookie bypass) and
  asserts that nothing outside "authorized MCP call on a TRUSTED server"
  ever causes an effect — with canaries (a sentinel file, the mock's call
  log) and positive controls so a broken gate cannot fake a pass.
- **Confirmation gate:** destructive / network / unknown / PROCESS
  capabilities are held at a confirmation point in the agent loop
  (`approver` hook, `agent.waiting_for_confirmation` event). The
  default headless mode auto-approves (autonomous operation); wire a
  real approver for interactive use.

## The Game Director layer (small model, big engineering discipline)

Nex is built so that a *small* local model (20B-class) can still ship
sophisticated games. The model supplies reasoning; Nex supplies the
engineering discipline around it — decomposition, memory, scope and
verification are CODE, not hopes about the model.

```
GAME REQUEST -> DIRECTOR (systems + build order + checklists)
                  |
                  v
            SCOPE ENVELOPE (one objective, success criteria, DO-NOT)
                  |
                  v
              PLANNER (few steps, one system)  ->  BUILDER (MCP tools)
                  |
          BUILD -> RUN -> OBSERVE -> REVIEWER -> TESTER -> CRITIC
                  |
        proven? --no--> DEBUGGER (smallest fix) --> repair plan
             |
            yes
             v
      SYSTEM VERIFIED -> next system
```

- **Director (`agent/director.py`)** decomposes a request into game
  systems and orders them by dependency. Two sources: the **recipe
  library** (deterministic, always available) and the **model** (may
  refine, never remove). The structural roots — a player controller and a
  playable space — and every checklist survive even a model that drops
  them, and `NEX_MAX_SYSTEMS` bounds the decomposition so "make
  everything" cannot happen.
- **Recipes (`agent/recipes.py`)** are proven, engine-agnostic system
  architectures (controller, inventory, enemy AI, weapon, quest, save,
  HUD, audio, ...) with per-step *intents* and explicit **completion
  checklists**. They carry no tool names: which MCP tool implements a
  step is decided at plan time against the live registry.
- **Every recipe ends with the playtest**: `Run the game → Observe →
  Verify` are appended to each recipe centrally, and
  `NEX_MAX_STEPS_PER_SYSTEM` deliberately bounds only the BUILD steps —
  verification is not a budget item, so it can never fall off the end.
- **Scope envelope (`scope_envelope`/`scope_block`)** is the anti-"I
  improved the entire project" cage: ONE objective, the success criteria,
  a step budget for the work (`NEX_MAX_STEPS_PER_SYSTEM`), and an explicit
  DO-NOT list naming the other systems. Work that leaves the cage is named by
  the critic (`scope_creep`), never silently accepted.
- **Roles (`agent/roles.py`)** are the same model called with different,
  tightly scoped jobs: DIRECTOR, PLANNER, BUILDER, REVIEWER, TESTER,
  DEBUGGER, CRITIC. Each role sees only its own context (the Director
  never sees the tool catalog; the Debugger sees one failed step) and
  each role's output is **validated by deterministic code** before use.
- **Bounded memory (`agent/project_state.py`)** is the model's
  orientation — what exists, what works, what doesn't, what we're
  building — never a chat log. Every collection is either **upserted**
  (systems, assets, decisions, errors, failed tasks) or a **capped**
  rolling window with an honest counter (`completed_count`). Runs report
  their compaction to the UI; long autonomous sessions cannot poison
  their own context.
- **Mandatory verification.** "The tool call returned" is never proof.
  A criterion is `evidence` (the step that produces it ran) until a clean
  re-observation or the REVIEWER's verdict makes it `pass`. The
  completion gate then refuses to report `COMPLETED` while the scoped
  system still has unproven criteria — the run reports `PARTIAL` and
  names exactly which criteria lack evidence from the running game.

The UI shows the system map (planned / building / **built, not verified** /
verified / broken) with the current objective, its checklist (criteria turn
green only when proven) and the gate rows for anything unverified. A system
whose work is done but whose criteria were never confirmed is labelled
`unverified` — never `verified`.

### Campaigns: a run works the game plan, not a single system

`NEX_MAX_SYSTEMS_PER_RUN` (default 2) and `NEX_RUN_BUDGET_S` (default
900s) bound how far one run gets: after a system's work is done the
campaign scopes the next system and keeps going, announcing every step
(`agent.system_started`, `agent.campaign_finished` /
`agent.campaign_stopped` with the reason). Three things keep it honest:

* **Verification is not progress.** The campaign advances when the work
  is done, not when it is proven; unproven systems are recorded as
  `unverified` and the completion gate still names their criteria, so a
  campaign run ends `PARTIAL` rather than claiming "AAA done".
* **A quality target, not a feeling.** The critics score 1-10; `NEX_QUALITY_TARGET`
  (default 0.6) is enforced in code — a PASS below the target becomes
  another polish round (`agent.quality_gate`), bounded by
  `NEX_MAX_QUALITY_ROUNDS`.
* **The model is optional.** With no model (or a failed model call) the
  plan comes from the RECIPE library matched against the live catalog by
  deterministic rules (`agent.recipe_planned`) instead of a generic
  skeleton: a create step finds a create tool, a configure step finds a
  configure tool (never a create one), the playtest step finds the launch
  tool and the observation step finds the capture tool. Steps whose
  capability is missing are dropped and named, never invented.
* **An approved plan is binding.** When the user approves a plan (START
  BUILD), that graph is executed as approved: no campaign, no extra
  system — the run does not extend work nobody signed off on.

### Without an engine: the BLUEPRINT

"No MCP servers are connected" is honest but useless on its own, so a
blocked run now produces the **build blueprint**: every system in build
order, the proven steps, the capability each step needs and the tool that
would serve it (or "not connected"), the completion checklist, and the
test plan that would prove it. It is published as `agent.blueprint`,
persisted with the project (`state.blueprint_md`) and is explicitly a
PLAN — nothing is reported as built.

Both entry points are directed. A run started from the agent loop gets a
Director pass before planning; `START BUILD` on an approved plan
(`server_run.run_agent_goal`, the route the UI drives) keeps that plan
*exactly as approved* and still attributes it to the current system with
its checklist — so an approved plan is executed as approved AND
verifiable. Re-planning it would betray the approval; verifying it
against nothing would betray the user.

### The capability boundary: connected is not trusted, trusted is not code execution

Nex's only action surface is MCP, and three separate decisions stand
between a model output and the machine:

1. **Server trust** (`NEX_TRUSTED_SERVERS`, strict mode by default):
   connecting a server is an operator act, and it is not enough to call
   its tools.
2. **Tool classification** — every tool gets a category from its name and
   from its (untrusted) MCP annotations, severity-max, plus the operator's
   capability file. `CODE_EXECUTION` covers the tools that RUN code
   (`execute_luau`, `run_python`, `run_script`, a terminal): whatever the
   payload contains executes with the engine's privileges, so those tools
   are never silently allowed. Ordinary editor tools (`create_script`,
   `run_game`, `screenshot`, `read_console`, `save_place`) are classified
   by exact token rules and stay friction-free.
3. **The call's arguments** — `scan_arguments()` refuses OS primitives
   inside an argument (`os.execute`, `io.popen`, `subprocess`,
   `loadstring`, `dofile`, ...) and sensitive paths on *every* tool
   (`~/.ssh`, `.aws`, `/etc/`, `.nex/` — Nex's own token), and
   `scan_file_payload()` closes the laundering path where the payload is
   written to a file and the file is executed.

An autonomous run never approves anything for itself: a call that still
requires confirmation stops the run, is reported (`agent.
confirmation_required`) and appears in the UI. Two operator levers say
"yes, deliberately": `NEX_ALLOW_CODE_EXECUTION=<server>` for code/process
tools, `NEX_ALLOW_CONFIRMATIONS=<server>` for destructive/network/unknown
names, or a per-tool `{"approved": true}` pin in the capability file.
Escape payloads are refused even with every lever pulled
(`test_escape.py`, section C).

## The OBSERVE loop (build -> run -> observe -> fix -> verify)

"Build succeeded" is not "the game works". Nex judges the RUNNING game:

1. **Observe.** When an executed tool is a runtime observation (name
   semantics: `screenshot`/`capture`, `logs`/`console`, `performance`/
   `metrics`, `inspect_*`/`*_state`), its result is captured as a
   structured Observation (`agent/observations.py`) — bounded text +
   payload — stored in the project memory and published as an
   `agent.observation` event (the UI shows it in the observation pane).
   Observation text is untrusted data: judged, never obeyed.
2. **Critique.** The critic (`find_observations` in `agent/critic.py`)
   scans the LATEST observation per tool for structural defects:
   crash signals in logs/metrics, empty scenes, physics defects
   (floating, embedded, stuck, NaN positions), empty captures. Major
   findings drive a REPLAN — the improvement planner receives the open
   defects as project memory and an explicit repair-first instruction.
3. **Remember.** Defects become `known_bugs` in the project state
   (survive across runs + checkpoints). When a clean re-observation of
   the proving kind arrives, the bug is marked `fixed` — the
   re-observation is what proved the repair.
4. **Experiment safety.** Before every improvement run the workspace is
   snapshotted (tar, `agent/checkpoints.py`: `snapshot_workspace` /
   `restore_workspace`, VCS + dependency noise excluded, keep-5
   pruning). If the run makes things WORSE (more failed tasks than
   before), the files are rolled back and the decision is recorded —
   `agent.experiment_rolled_back`.

The UI's command-center strip (Build / Verify / Runtime chips) and the
collapsible observation rows track the loop live.

To script against the server, export the token, e.g.
`export NEX_AUTH_TOKEN="$(cat ~/.nex/server_token)"` and send it as
`X-Nex-Auth`.

## NEX 2.0 — Nex develops, it doesn't just generate

Nex is an AI development agent: it takes a high-level idea and turns it
into a genuinely developed project — planning, implementation, testing,
criticism, iteration, and polish are all part of the product.

**Understand before building.** `POST /api/project {goal}` runs the
design stage first: the model answers the design questions (concept,
genre, core loop, pillars, systems, world, audio, architecture,
milestones, dependencies, acceptance criteria, tests, quality gates) as
a STRUCTURED design document (`agent/design.py` — data, not Markdown),
and a draft task plan is validated against the live MCP registry.
Nothing is built until you approve.

**The Plan Page.** `agent.design_ready` renders a real page in the UI:
pillars, loop, systems, world, milestones, quality gates, honest
dependency gaps, and the draft plan — with a START BUILD button
(`POST /api/project/<id>/build`). The approved plan is PERSISTED with
the project, and START BUILD builds EXACTLY that plan — no silent
re-planning between approval and execution. If the critic later says
REPLAN, the next plan is created deliberately (Plan B), never by
accident. Nex keeps the face visible while it works; it never becomes
a soulless terminal.

**Build -> critique -> improve (bounded).** After building, the critic
(`agent/critic.py`) asks "is this actually good?" — technical
(unverified completions, stalls), design (work that contradicts the
design doc or repeats the same pattern — structural repetition mining,
never a hardcoded recipe), quality (placeholder content, open quality
gates) — optionally merged with an LLM critique. PASS -> done.
WEAK -> POLISH or REPLAN, and the agent goes back to work through the
same pipeline. Bounded by `max_critique_cycles`; Nex doesn't churn
forever, and it doesn't get a free pass after one iteration.

**Design stability.** Design decisions can be LOCKED
(`ProjectState.lock_decision`). The planner sees them; any step that
re-litigates a locked decision is skipped by the deterministic design
guard — Nex will not "helpfully" add a health bar 40 minutes after you
locked "no health bar".

**Persistent project memory.** Every project persists its design,
decisions, milestones, critique cycles and completion state
(`~/.nex/projects/<id>.json`; `GET /api/projects`,
`GET /api/project/<id>`). Close the conversation, come back tomorrow,
and Nex resumes from where the work actually stands.

The face reacts to all of it: focused while designing, proud on a PASS
verdict, confused on a WEAK one — and calm the rest of the time.

## Capability boundary (hard invariant)

Nex's AI acts ONLY through explicitly connected MCP servers. There is
no flag, env var, or runtime call that turns this off
(`mcp/policy.py` — `MCP_ONLY = True`).

- The model-visible MCP surface (`/mcp` `tools/list`) contains ONLY:
  MCP introspection tools + every connected server's tools.
  Filesystem/shell/host tools are server infrastructure and are never
  exposed to the agent.
- MCP `resources/` expose protocol metadata only (`nex://about`,
  `nex://tunnels`, per-server info, curated tool guides) — no
  filesystem, log, workspace, or host-state resources.
- **Layer separation (assistant vs. game agent).** The assistant layer
  (chat, voice, face, observer) can SPEAK but never ACT: `observer.py`
  imports no subprocess/registry/tools/MCP module and its only side
  channel is publishing `speak.*` events to the bus. The action right
  lives exclusively in the MCP gateway (`authorize()` → upstream call),
  reachable only through an authorized plan step. `test_escape.py`
  (section A7) proves it structurally (import scan) and behaviorally
  (only speak events are ever emitted).
- **The gateway order is fixed:**
  `CONNECTED → SERVER TRUST (strict registry) → server allowlist →
  tool allowlist → capability classification (heuristics + annotations
  + operator registry, severity-max) → confirmation → MCP call`.

## Files

- `server.py` — Python stdlib HTTP server, SSE stream, REST API, Ollama
  client (via `urllib`). No third-party Python packages.
- `agent/director.py` — the Game Director: request -> systems, build
  order, scope envelope (one objective, success criteria, DO-NOT).
- `agent/recipes.py` — the proven system-architecture library with
  per-system completion checklists (engine-agnostic, no tool names).
- `agent/roles.py` — the specialized roles (planner/reviewer/tester/
  debugger/critic) and their tightly scoped context builders.
- `agent/blueprint.py` — the deterministic build blueprint (systems,
  required capabilities, checklists, test plan) for runs without an
  engine.
- `agent/providers.py` — the provider layer: planner/builder roles, the
  failover chains, client-side rate budgets, `.env` + `~/.nex/providers.json`
  config, and the live status the UI reads. HTTP only — no OS surface.
- `provider_chip.js` — the "who is building right now" chip (pure label
  logic, covered by the node test harness).
- `test_providers.py` — provider-layer tests: keys, roles, failover,
  budget, batching, honesty when everything is down, MCP-only boundary.
- `.env.example` — provider keys template (copy to `nex/.env`).
- `test_director.py` — Director-layer tests: recipes, decomposition,
  scope cage, bounded memory, roles, mandatory verification.
- `mcp_engines.py` — **MCP-only** curated guide adapter for the Roblox
  Studio and Unreal Engine MCP tools. Enriches the live `tools/list`
  with rich explanations (what/when/params/examples/caveats), and
  powers the `mcp://<platform>/guide` resources + the
  `*_explain_tools` / `*_build_recipe` prompts. Does not touch the chat
  persona or sandbox tools.
- `index.html` — minimal markup, canvas + hidden debug panel.
- `style.css` — black background + minimal UI affordances.
- `webgl.js` — WebGL2 renderer. SDF-based rounded rectangles; no images,
  no SVG, no GIFs, no sprite sheets.
- `animations.js` — state machine, idle behavior library + scheduler,
  multi-phase animations, easing library.
- `app.js` — application glue: SSE, microphone (`getUserMedia`),
  `AudioContext` + `AnalyserNode`, procedural music loop, debug panel,
  keyboard shortcuts, mouse attention.

## Providers — the planner and the builder are different roles

The model layer is split in two, and the split is the point:

```
USER -> PLANNER (reasoning, architecture, decomposition)
          |
        exact build plan
          |
        BUILDER (does the work, often, in batches)
          |
        MCP only -> Roblox MCP / Unreal MCP
```

| Role | Default | Why |
| --- | --- | --- |
| **Planner** | local model (`OLLAMA_MODEL`), or **GPT** when a key is set | called a handful of times per run: design document, systems, tests |
| **Builder** | **NVIDIA NIM** (`nvidia/nemotron-3-super-120b-a12b`) | called during the build loop; NIM is fast and free, but rate-limited |

The local default is `gpt-oss:20b` — the small model the whole architecture
is built around (the model is the reasoner, never the source of quality).

Both are configurable per role (settings page → *Models & providers*), down
to the model id, the endpoint and the RPM budget. A provider list is fetched
**live** (`/v1/models` / `/api/tags`) so the model ids you see are the ones
the endpoint actually serves.

### Failover — the plan never restarts

```
NIM: 429 / timeout / 5xx / RPM exhausted
        |
        +--> GPT builds instead (same plan, same tasks)
        |
        +--> after the cooldown NIM is the builder again

NIM: 401 / 403  (key rejected — a CONFIG fault, not capacity)
        |
        +--> the same failover happens (a build is not lost to a typo)
        +--> but it is announced: provider.auth_error, a red chip mark and a
             toast, and the provider is parked for NEX_AUTH_BLOCK_S (900s)
        +--> unblocked instantly by fixing the key (or the endpoint)
```

NVIDIA publishes no usage endpoint and no per-model quota (credits were
removed in favour of unpublished rate limits, ~40 RPM is the community
baseline), so Nex counts the requests itself:

* **sliding-window budget per provider** (`NEX_NIM_RPM`, default 40) — when
  the budget is spent the router routes around the provider *before* the
  upstream 429 happens;
* **cooldown with automatic recovery** — `Retry-After` is honoured, other
  errors get 5–120s depending on the kind (timeout/server/auth);
* **live state** — `available` / `rate_limited` / `cooling` / `error` /
  `no_key`, exposed to the UI (`/api/providers`, `provider.*` SSE events);
* **fallback order** — builder: `NIM -> GPT -> local`; planner:
  `GPT -> NIM -> local`. The local model is always last and always works.
  The chain is **explicit**: only providers named in the role's `fallbacks`
  list are ever tried, so a new provider (or a new catalog entry) can never
  silently become the fallback. A configured-but-unnamed provider can still
  be *chosen* as the primary; it just never enters a chain on its own.
  Set it in the settings page or with `NEX_BUILDER_FALLBACKS` /
  `NEX_PLANNER_FALLBACKS` (comma-separated).

Failover replaces the *hands*, never the plan: the same messages are sent to
the fallback provider and the loop continues exactly where it was.

Two more honesty rules in this layer:

* **chain-of-thought is never the answer.** A reasoning model whose content
  got cut off returns its scratchpad; that is speculation and it is thrown
  away (`bad_response`) instead of being handed back to the agent as "the
  model's decision".
* **the rate budget belongs to the process, not to the settings.** Editing a
  provider (endpoint, model, RPM) keeps the sliding window and the cooldowns —
  a settings change is not a quota reset.

### Batches, not tiny calls

A rate-limited builder must not spend one request per broken step. Failures
of the same wave are collected and diagnosed in **one** call
(`agent.repair_batched`); only a single pending failure gets its own focused
call. If a batched answer is unusable, the chunk is **halved** — never split
into one call per job:

```
8 failures -> 1 batched call -> unusable? -> 2 half calls -> hard cap
             3 provider requests TOTAL (NEX_BATCH_MAX_CALLS, default 3)
```

Anything that still has no answer stays without one: the deterministic
repairs take over and the step fails honestly. Batching can cost a retry,
never a repair — and it can never multiply requests.

### Keys

Highest precedence first:

1. process environment (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, …)
2. `.env` — `nex/.env`, repo-root `.env`, `~/.nex/.env`
   (template: `nex/.env.example`)
3. settings page → `~/.nex/providers.json` (0600)

Keys are **never** sent back to the browser: the API returns them masked
(`nvapi-…9f2`). A key is also **bound to the host it was entered for**: if
the endpoint is later pointed somewhere else, the key stops being sent
(`key_mismatch`, visibly red in the UI) until it is typed again for the new
host. That closes the obvious way to steal a key: change the base URL to your
own server and wait for the Authorization header.

Provider URLs are not free-form either — they are outbound requests with a
credential attached, so:

* `http`/`https` only, no credentials inside the URL;
* cloud metadata and link-local targets are refused
  (`169.254.169.254`, `metadata.google.internal`, `fd00:ec2::254`,
  `100.100.100.200`, `fe80::/10`) — that is the classic SSRF escalation;
* a base URL pointing at Nex itself is refused (loop);
* **redirects are only followed within the same host** — otherwise a 302
  would hand the Authorization header to a third party;
* `NEX_PROVIDER_HOSTS=host1,host2` turns the whole thing into a strict
  allowlist (loopback is always allowed, so local Ollama keeps working).

And whatever the provider answers, it is text: the builder's only action
surface is the MCP registry — it cannot reach the machine.

Environment knobs:

```
NVIDIA_API_KEY=nvapi-...            # builder (build.nvidia.com)
OPENAI_API_KEY=sk-...               # planner
OLLAMA_HOST / OLLAMA_MODEL          # local fallback
NEX_PLANNER_PROVIDER=local|gpt|nim  # default: gpt if a key exists, else local
NEX_BUILDER_PROVIDER=nim|gpt|local  # default: nim if a key exists, else local
NEX_PLANNER_MODEL / NEX_BUILDER_MODEL
NEX_NIM_RPM=40                      # client-side budget for the NIM free tier
NEX_BATCH_MAX_CALLS=3               # hard cap for one batched diagnosis
NEX_AUTH_BLOCK_S=900                # park a provider whose key was rejected
NEX_PROVIDER_HOSTS=                  # optional strict allowlist for endpoints
NEX_BUILDER_FALLBACKS=gpt,local      # explicit chain (nothing implicit)
NEX_PROVIDERS_FILE=~/.nex/providers.json   # where the settings page stores
```

### The chip

Top right of the UI: which provider is **building right now**, plus the model
that is *really* answering (after a failover that is the fallback's model, not
the configured one), with the planner on a dim second line. Amber + pulse
means a failover is running, grey is the local model, red means no provider at
all. A **rejected key and a key bound to another host get their own red mark**
(`⚠key`) so a config fault cannot be mistaken for a capacity problem. The
tooltip carries the quota (`28/40 RPM`), the cooldown, the auth block and the
reason the previous provider stepped aside.

## Ollama

Optional. Configure via env:

```
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=gpt-oss:20b
```

The face animates entirely offline. Without Ollama, a chat turn surfaces a
visible ERROR state, delivers a short "can't reach the model" fallback reply
(speak.delta → speak.end), and returns to IDLE — the face never sticks in
SPEAKING.

## Backend → frontend events (SSE)

Sent as JSON over `text/event-stream`:

```
data: {"type":"state","state":"LISTENING","params":{},"ts":...}
data: {"type":"speak","text":"hello","ts":...}
```

The frontend never asks the backend for animation parameters — those are
generated locally by the behavior engine.

## Debug panel

Press `` ` `` (backtick) to toggle the developer panel. Buttons let you
force any state, force individual idle behaviors, toggle the microphone,
toggle the music loop, trigger WAKE / ERROR, and send chat messages.