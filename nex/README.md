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
(`POST /api/project/<id>/build`). Nex keeps the face visible while it
works; it never becomes a soulless terminal.

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

Nex's AI acts ONLY through (1) explicitly connected MCP servers and
(2) the Amazon Music connector. There is no flag, env var, or runtime
call that turns this off (`mcp/policy.py` — `MCP_ONLY = True`).

- The model-visible MCP surface (`/mcp` `tools/list`) contains ONLY:
  MCP introspection tools + every connected server's tools + the 7
  Amazon Music controls. Filesystem/shell/host tools are server
  infrastructure and are never exposed to the agent.
- MCP `resources/` expose protocol metadata only (`nex://about`,
  `nex://tunnels`, per-server info, curated tool guides) — no
  filesystem, log, workspace, or host-state resources.

## Amazon Music (official Web API)

The connector (`music_amazon.py`) speaks the **official Amazon Music
Web API** (https://developer.amazon.com/docs/music/) — Login With
Amazon OAuth 2.0, `POST /v1/search/tracks`, `POST /v1/playback/sessions`,
and the mandatory `/v1/playback/event` start/stop reporting.

Setup (env):

```
NEX_AMZ_LWA_CLIENT_ID      # amzn1.application-oa2-client....
NEX_AMZ_LWA_CLIENT_SECRET
NEX_AMZ_LWA_REFRESH_TOKEN  # long-term LWA refresh token
NEX_AMZ_PROFILE_ID         # x-api-key: LWA *Security Profile ID*
                           # (amzn1.application....) — NOT the client id
NEX_AMZ_DEVICE_ID          # optional; stable id derived from hostname
NEX_AMZ_API_BASE           # default https://api.music.amazon.dev
```

NOTE: the Amazon Music Web API is in **closed beta** — the Security
Profile must be enabled by Amazon Music (developer forum / contact).
Until access exists the connector stays honest about it and you can
still drive the installed Amazon Music app:

```
NEX_MUSIC_BACKEND=auto     # default: web when credentials exist, else stub
NEX_MUSIC_BACKEND=web      # force the official Web API
NEX_MUSIC_BACKEND=link     # open amazonmusic:// deep links (desktop app)
NEX_MUSIC_BACKEND=stub     # CI/sandbox: record commands only
```

The 7 allowlisted controls (`am_play am_pause am_toggle am_next
am_previous am_volume am_search_play`) are the entire surface — on the
Web API backend they map to catalog search, playback sessions, and
event reporting; volume is client-side (the API has no volume
endpoint). DRM-protected streams are rendered by the Nex UI when the
browser can, and honestly reported as unrenderable otherwise.

## Files

- `server.py` — Python stdlib HTTP server, SSE stream, REST API, Ollama
  client (via `urllib`). No third-party Python packages.
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

## Ollama

Optional. Configure via env:

```
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.2
```

The face animates entirely offline. Without Ollama, chat attempts surface a
visible ERROR state which then RECOVERY → IDLE.

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