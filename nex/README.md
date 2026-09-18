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