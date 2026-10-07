# Nex — whole-codebase improvement plan

Produced from a deep, repo-wide audit (Oct 2026): code hygiene, test-coverage
gaps, the two largest hand-written modules, the web frontend, CI/packaging,
and the server's security model. One real bug was found and fixed along the
way (see Tier 0). Everything else below is a prioritized backlog, not a
list of confirmed bugs — most of it is "we checked and found no dedicated
test", not "we checked and found broken code".

## Tier 0 — done this pass

- **`nex/agent/jsonreply.py` had zero test coverage anywhere in the repo**,
  despite being the single chokepoint every consumer of model output goes
  through (planner, evaluator, diagnose, and the chat **ACT directive** that
  decides whether an autonomous run starts). Deep-testing it surfaced a real
  bug: the balanced-brace scanner bailed out of its *entire* search the
  moment any one `{` failed to find a match, instead of trying the next
  `{`. A single harmless stray brace earlier in a reply (a model saying
  "I'll use `{` as a placeholder" before its real answer) could silently
  hide a valid ACT directive that followed it — an autonomous run the model
  correctly signalled could simply never start, with no error anywhere.
  Fixed with a proper single-pass, stack-based scan (closing `}` resolves
  the innermost open `{`): correct *and* strictly O(n), immune to the
  O(n²) blowup a naive "retry from every stray brace" fix would introduce.
  Added `nex/tests/test_jsonreply.py` (18 assertions), wired into
  `run_all.py`. Committed `465e5d4`, pushed, 17/17 suites green.

## Tier 1 — DONE

Both items below are complete (commit `b40111d`): CI now runs on every push,
and `audit.py`/`diagnose.py`/`events.py`/`llm.py`/`prompts.py` all have
dedicated tests. Two more small, real issues turned up and were fixed along
the way: `audit.py` was recording a tool called with legitimately empty
arguments (`{}`) identically to "no arguments were ever given" (`None`),
losing a real distinction; and `events.py` built each event with the core
`type`/`run_id`/`ts` fields before the payload spread, so a payload carrying
a same-named key could have silently overridden them (no current caller
does, but it was a live footgun in the one place that is the UI's source of
truth for run state). `diagnose.py` and `prompts.py` were already solid —
their new tests lock in existing correct behavior (hallucinated-tool
rejection, policy re-authorization on switch_tool, bounded prompt
construction against hostile MCP metadata) with no code changes needed.
22/22 suites pass.

## Tier 1 (original text, for reference)

1. **Dedicated unit tests for the remaining untested modules.** Of 25
   non-test modules in `nex/agent` + `nex/mcp`, these have no dedicated test
   file *and* no genuine indirect exercise found (verified by checking for
   real imports, not just incidental name matches — e.g. `policy.py`,
   `registry.py`, `task_graph.py`, `model_planner.py`, `workload.py`, and
   `planner.py` all turned out to have real, substantive coverage once
   checked properly):
   - `agent/audit.py` — the run's audit trail; if this has bugs they hide
     evidence of what actually happened, which is the thing Nex leans on
     most heavily for trust.
   - `agent/diagnose.py` — failure diagnosis / retry classification logic.
   - `agent/events.py` — the event bus other components rely on for
     notifications.
   - `agent/llm.py` — a model-call helper layer.
   - `agent/prompts.py` — prompt construction; subtle bugs here silently
     degrade model quality rather than crashing, so they're easy to miss.

   `jsonreply.py` was in exactly this bucket and had a real, user-facing bug
   hiding in it. The same "write tests, deliberately try to break it"
   exercise is the highest-value next step, roughly in the order above
   (audit and diagnose look the most consequential; llm/events/prompts are
   lower but still worth an hour each).

2. **Add a CI workflow.** There is no `.github/workflows` at all — zero
   automated gate on any push, so every regression check for the last dozen
   improvements in this project happened because an agent remembered to run
   `python3 nex/tests/run_all.py` by hand. The whole suite is dependency-free
   (stdlib only) and finishes in ~11s, so this is a 15-minute, zero-risk
   addition: a workflow that checks out the repo, runs `python3
   nex/tests/run_all.py` on every push and PR to this branch, and fails the
   check on nonzero exit. No build step, no external services, nothing to
   maintain.

## Tier 2/3 — ALL DONE (items 3, 4, 5, 6)

Items 3, 4, and 6 (commit `81f485c` + follow-up): frontend smoke tests, API
rate limiting, and a minimal `pyproject.toml`. Item 5 (structured `logging`,
commit `3615631`) was initially skipped as discretionary/"not recommended as
current work" per the plan's own framing, then done anyway once explicitly
requested — see Tier 3 below for what changed.

Writing the very first frontend tests immediately found two real, severe
bugs, not just gaps in coverage:

- `chat.js` and `composer.js` both imported named exports (`RunCard`,
  `face`) that did not exist in `runview.js`/`face.js`. ES module named
  imports are resolved at *link time*, before any code runs — this was a
  hard `SyntaxError` in any real browser, meaning the entire chat UI module
  graph could never load. Fixed by correcting the `RunCard` import to the
  `buildRunCard` function actually used, and by giving `face.js` a real
  exported singleton (`initFace()` + a null-safe `face` proxy) instead of
  main.js's unexported local variable. Locked in by
  `nex/web/tests/imports.test.mjs` (checks every named import across all of
  `nex/web/js` resolves to a real export) and `nex/web/tests/boot.test.mjs`
  (loads the real `index.html` + `main.js` in jsdom and asserts it boots).
- `markdown.js`'s fenced-code-block handling could never recognize a
  *closing* fence (the per-line regex needed an embedded newline no single
  line could contain), so any reply with a code block followed by more text
  — an extremely common model-output shape — had everything after the
  opening fence, including the real closing fence, swallowed into the code
  block. Fixed with proper forward-scanning for the matching closing fence.
- `markdown.js`'s table-separator regex used the character class
  `[\s:-|]`, parsed as `\s` plus the *range* `':'`–`'|'` (0x3A–0x7C) —
  covering most letters and punctuation while *excluding* the literal `-`
  character outright. A standard `|---|---|` separator row never matched,
  so markdown tables never rendered as tables at all. Fixed by moving `-`
  to the end of the class so it's literal, not a range endpoint.

The 18-test suite (`nex/web/tests/`, Node's built-in test runner + jsdom,
`npm test` from `nex/web/`) runs in CI alongside the Python suite.

Rate limiting: `server.py` now applies a generous (600 requests / 60s),
per-peer sliding-window limit to all authenticated `/api/*` traffic, wired
into the existing `_auth_gate` chokepoint right after the auth check. It's
defense-in-depth against a buggy client-side retry loop running up cost
against a real, metered provider, not an anti-abuse measure (the 256-bit
token already keeps strangers out). Covered by two new tests in
`test_server_api.py`, including one that trips the limiter over real HTTP
and confirms it's a sliding window, not a lockout.

`pyproject.toml`: metadata-only, zero dependencies, verified end-to-end with
a real `pip install -e .` into a throwaway venv and importing `agent`,
`mcp`, `server`, and `store` from an unrelated working directory. Nex's
internal imports assume `nex/` itself (not the repo root) is the import
root, with `agent`/`mcp` as namespace packages (no `__init__.py`) — the
`[tool.setuptools]` mapping mirrors that layout exactly, so installing the
package required zero changes to any existing import statement.

## Tier 2 — medium value (original text, for reference)

3. **Some minimal frontend testing.** `nex/web/` is 8,653 lines across 13 JS
   files, CSS, and HTML, with zero automated coverage of any kind. A spot
   read-through found the frontend already does the right thing
   security-wise (chat/server/settings content is rendered via
   `.textContent`, never interpolated into `innerHTML` — no XSS gap found),
   but there's no safety net against a future regression (e.g. someone
   switching a `.textContent` to a template-literal `.innerHTML` without
   noticing the risk). Given the backend's stdlib-only philosophy, the
   lightest-weight option is probably a small Node+jsdom harness (no browser
   automation, no bundler) that imports the pure/DOM-manipulation functions
   and asserts on the resulting DOM for a handful of critical paths: chat
   message rendering, the server list, and the settings form. Scope this
   modestly — full coverage of 8.6k lines of UI code is not worth the
   investment, but smoke coverage of the chat bubble rendering and the
   add-server dialog would catch the highest-impact regressions.

4. **Light rate limiting on authenticated API endpoints.** The server
   already throttles *failed authentication* attempts, uses HttpOnly +
   SameSite=Strict cookies, and requires a custom CSRF header on mutating
   requests — a solid, deliberate security model for a local-first,
   loopback-by-default tool. There's no general rate limit on
   *authenticated* requests (e.g. hammering `/api/chat`), which matters less
   here than in a multi-tenant server but is still a cheap defense-in-depth
   addition (and guards against a buggy client script looping a call
   against a real, metered provider API and running up cost). Lower
   priority than Tier 1, but cheap if picked up alongside other server.py
   work.

## Tier 3 — low priority / discretionary

5. **DONE** (commit `3615631`). **Structured `logging` instead of
   `print`/`stderr.write`.** No `logging` module usage anywhere in the
   codebase — only ~6 legitimate CLI startup-banner `print()`s and a
   handful of `stderr.write()` calls for error reporting (`capability.py`,
   `transport.py`, `server.py`). This was a deliberate-looking simplicity
   choice for a small, local-first, stdlib-only tool, not a bug — the
   maintainer asked for it anyway, so it's done: `server.py` now
   configures `logging.basicConfig()` once at import time, respecting a
   new `NEX_LOG_LEVEL` env var (default INFO, invalid values fall back to
   INFO), with three per-area loggers (`server.http`/`server.chat`/
   `server.api`) replacing the old `[http]`/`[chat]`/`[api]` bracket-tag
   stderr writes; `capability.py`'s registry-corruption warning does the
   same. The ~6 startup-banner prints and `transport.py`'s raw child-MCP-
   stderr passthrough were deliberately left as-is (see the inline
   comments at each site for why) — they are not log records this process
   produces. Covered by `nex/tests/test_logging.py` (subprocess-based:
   level filtering, format, invalid-value fallback) and a new test in
   `test_capability.py`. 24/24 suites pass.
6. **A minimal `pyproject.toml`** purely for metadata/versioning (no
   dependencies) if `pip install -e .`-style ergonomics are ever wanted.
   Fully optional — the project explicitly has no packaging today and that
   is consistent with its "nothing but the standard library" design.

## Explicitly not on this list

The README's "Honest limits" section already documents ~9 known
architectural boundaries (e.g. Nex orchestrates capabilities rather than
containing a game engine; the quality score is evidence-coverage, not an
art review; reverting is compensation, not true rollback). Those are
deliberate, self-aware tradeoffs the maintainers already chose — this plan
does not re-flag them as bugs or suggest revisiting them.

## Suggested order of attack

1. CI workflow (Tier 1.2) — trivial, immediately pays for itself.
2. `audit.py` and `diagnose.py` tests (Tier 1.1) — most consequential of the
   untested modules.
3. `events.py`, `llm.py`, `prompts.py` tests (Tier 1.1) — same pattern,
   lower individual risk.
4. Frontend smoke tests (Tier 2.3) and API rate limiting (Tier 2.4), as
   time allows.
5. Tier 3 items only if/when the maintainers specifically want them.
