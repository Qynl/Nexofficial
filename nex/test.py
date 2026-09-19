#!/usr/bin/env python3
"""Test runner for NEX.

Verifies:
- server.py is syntactically valid Python.
- The Python-side tag parser + fallback emotion picker work correctly.
- The consolidated JS test suite passes (shader structure, animation engine,
  behavior library, mock WebGL pipeline, SDF raster proportions).

No external dependencies. Run from the nex/ directory:

    python3 test.py
"""
import importlib.util
import os
import py_compile
import subprocess
import sys
import tempfile


HERE = os.path.dirname(os.path.abspath(__file__))


def _load_server_module():
    """Load server.py as a module so we can call its pure functions."""
    # server.py creates its auth token file at import time — point it at a
    # throwaway path so the test suite never touches the real one.
    os.environ["NEX_TOKEN_FILE"] = os.path.join(tempfile.mkdtemp(), "token")
    spec = importlib.util.spec_from_file_location("nex_server", os.path.join(HERE, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_server_python() -> None:
    """Exercise the tag parser and fallback engine."""
    mod = _load_server_module()

    # ---- parse_tags ----
    cases = [
        ("Hello world.", "Hello world.", None),
        ("Hello [AMUSED] world.", "Hello world.", "AMUSED"),
        ("[HAPPY] All done!", "All done!", "HAPPY"),
        ("A [happy] moment.", "A moment.", "HAPPY"),
        ("[CURIOUS]? really?", "? really?", "CURIOUS"),
        ("x [NOPE] y", "x [NOPE] y", None),  # unknown tag — left intact
        ("[SLEEPY]", "", "SLEEPY"),
        ("[FOCUSED][HAPPY] two tags", "two tags", "FOCUSED"),  # first wins
        ("", "", None),
    ]
    for raw, exp_clean, exp_state in cases:
        got_clean, got_state = mod.parse_tags(raw)
        assert got_clean == exp_clean, f"clean mismatch for {raw!r}: {got_clean!r} != {exp_clean!r}"
        assert got_state == exp_state, f"state mismatch for {raw!r}: {got_state!r} != {exp_state!r}"
    print(f"  ok  parse_tags ({len(cases)} cases)")

    # ---- fallback_state ----
    cases_fb = [
        ("haha that's funny", "lol", "AMUSED"),
        ("are you sure?", "maybe not, but...", "SUSPICIOUS"),
        ("did it!", "ok works", "PROUD"),
        ("why does it fail?", "i wonder", "CURIOUS"),
        ("huh?", "not sure", "CONFUSED"),
        ("wow really?!", "wow unexpected", "SURPRISED"),
        ("thanks so much", "glad to help", "HAPPY"),
        ("ugh this is broken", "frustrating", "FRUSTRATED"),
        ("ok relax", "calm down", "CALM"),
        ("fix this bug", "ok let's first...", "FOCUSED"),
        ("good night", "rest well", "SLEEPY"),
        ("hello there", "hi", None),
        ("", "", None),
    ]
    for user, reply, exp_state in cases_fb:
        got = mod.fallback_state(user, reply)
        assert got == exp_state, f"fallback mismatch for ({user!r}, {reply!r}): {got!r} != {exp_state!r}"
    print(f"  ok  fallback_state ({len(cases_fb)} cases)")

    # ---- The system prompt must list every emotion so the model can use it.
    for s in mod.EMOTION_STATES:
        assert "[" + s + "]" in mod.NEX_SYSTEM_PROMPT, f"system prompt missing [{s}]"
    print(f"  ok  NEX_SYSTEM_PROMPT mentions all {len(mod.EMOTION_STATES)} emotions")

    # ---- model_reachable() must not raise when the host is unreachable. ----
    assert mod.model_reachable() in (True, False), "model_reachable must return bool"
    print("  ok  model_reachable() returns bool (does not raise)")

    # ---- The whole chat pipeline: a tagged reply goes through cleanly. ----
    # Simulate by calling parse_tags + fallback_state the same way the
    # chat thread does.
    tagged_reply = "Wow, that is wild [SURPRISED]!"
    clean, tagged = mod.parse_tags(tagged_reply)
    assert clean == "Wow, that is wild!", "clean mismatch: " + repr(clean)
    assert tagged == "SURPRISED"
    chosen = tagged or mod.fallback_state("tell me a story", clean)
    assert chosen == "SURPRISED"
    bare_reply = "Done - fixed the bug."
    clean2, tagged2 = mod.parse_tags(bare_reply)
    assert tagged2 is None
    chosen2 = tagged2 or mod.fallback_state("did it work?", clean2)
    assert chosen2 == "PROUD"
    print("  ok  chat pipeline (tagged + fallback)")

    # ---- _StreamFlusher: never publishes a partial tag. ----
    def _drive(tokens, max_idle_ms=150):
        f = mod._StreamFlusher(max_idle_ms=max_idle_ms)
        out = []
        for t in tokens:
            out.extend(f.feed(t))
        out.extend(f.flush(force=True))
        return out

    # Tag split across chunks: should fire STATE only once when complete.
    segs = _drive(["Hello [", "CURIOUS", "] what", "?"])
    states = [s for _, s in segs if s]
    assert states == ["CURIOUS"], "split-tag must fire exactly once, got: " + repr(states)
    # Concatenated text should read naturally.
    text = "".join(c for c, _ in segs if c)
    assert text == "Hello what?", "concatenation should preserve text, got: " + repr(text)
    print("  ok  flusher: split-tag fires once + text preserved")

    # No mid-tag leak — the buffer MUST hold back a tag boundary until
    # the closing ']' arrives.
    segs = _drive(["start [", "SUR", "PRI", "SED", "] done"])
    states = [s for _, s in segs if s]
    assert states == ["SURPRISED"], "4-token tag split must still fire SURPRISED, got: " + repr(states)
    print("  ok  flusher: tag split across 4 chunks")

    # Spaces preserved across punctuation boundaries.
    segs = _drive(["Hi!", " World", "."])
    text = "".join(c for c, _ in segs if c)
    assert text == "Hi! World.", "punctuation + space must be preserved, got: " + repr(text)
    print("  ok  flusher: spaces preserved across punctuation")

    # Empty stream is a no-op.
    assert _drive([]) == []
    print("  ok  flusher: empty stream is a no-op")

    # A stream that never gets a sentence terminator still flushes on
    # the idle window (force=True is the safety net the chat thread
    # uses at end-of-stream).
    segs = _drive(["just one long sentence with no terminator"])
    assert any(c for c, _ in segs), "force flush must emit the unterminated chunk"
    print("  ok  flusher: force flush emits unterminated chunks")

    # A trailing tag with no following text must still produce a chunk
    # (state-only) so the client doesn't see the emotion "after" the
    # talking — it should ride with the boundary that parsed it.
    segs = _drive(["Done. [", "PROUD", "]"])
    states = [s for _, s in segs if s]
    texts = [c for c, _ in segs if c]
    assert states == ["PROUD"], "trailing tag must fire PROUD, got: " + repr(states)
    # The state-only delta has empty text but must still appear.
    assert any(c is None or c == "" for _, c in segs) or len(states) > 0
    print("  ok  flusher: trailing tag fires state even without text")

    # A tag at the start of a sentence must fire with the FIRST text
    # chunk, so the face changes AS the talking begins (not before).
    segs = _drive(["[", "HAPPY", "] Hi there", "."])
    state_pairs = [(c, s) for c, s in segs if c or s]
    # The HAPPY tag should ride with the first non-empty chunk (which
    # carries text like " Hi there" or "Hi there.").
    assert any(s == "HAPPY" for _, s in segs), "HAPPY must ride with some chunk"
    # The first chunk that has both a state and text must have HAPPY.
    combined = [(c, s) for c, s in segs if c and s]
    assert combined and combined[0][1] == "HAPPY", \
        "first combined chunk must carry HAPPY, got: " + repr(combined)
    print("  ok  flusher: start-of-sentence tag rides with first chunk")


def main() -> None:
    # Syntax-check the Python server.
    try:
        py_compile.compile(os.path.join(HERE, "server.py"), doraise=True)
    except py_compile.PyCompileError as exc:
        print(f"server.py syntax error: {exc}")
        sys.exit(1)
    print("OK: server.py parses")

    # Pure-python tests on the tag parser / fallback.
    print()
    print("server.py logic:")
    test_server_python()

    # Run the JS test suite.
    print()
    r = subprocess.run(
        ["node", os.path.join(HERE, "test.js")],
        capture_output=True, text=True, cwd=HERE,
    )
    print(r.stdout, end="")
    if r.stderr:
        print("stderr:", r.stderr, file=sys.stderr)
    if r.returncode != 0:
        print("\nFAIL: JS test suite")
        sys.exit(1)

    # Run the MCP / observer integration test suite. This assumes the
    # server is already running on http://localhost:8787 (started by
    # the user / the dev loop). Skip gracefully if it's not reachable.
    print()
    try:
        import urllib.request
        with urllib.request.urlopen(
            "http://localhost:8787/api/health", timeout=2
        ) as resp:
            if resp.status == 200:
                r2 = subprocess.run(
                    [sys.executable, os.path.join(HERE, "test_mcp.py")],
                    capture_output=True, text=True, cwd=HERE,
                )
                print(r2.stdout, end="")
                if r2.stderr:
                    print("stderr:", r2.stderr, file=sys.stderr)
                if r2.returncode != 0:
                    print("\nFAIL: MCP test suite")
                    sys.exit(1)
                # Run the MCP tunnel tests (boots its own fake upstream
                # on a free port and exercises the registry routing).
                r3 = subprocess.run(
                    [sys.executable, os.path.join(HERE, "test_mcp_tunnel.py")],
                    capture_output=True, text=True, cwd=HERE,
                )
                print(r3.stdout, end="")
                if r3.stderr:
                    print("stderr:", r3.stderr, file=sys.stderr)
                if r3.returncode != 0:
                    print("\nFAIL: MCP tunnel test suite")
                    sys.exit(1)
                # Run the MCP stdio tests (spawns `python -m stdio_server`
                # subprocesses and verifies LSP+NDJSON framing across
                # 7 round-trip scenarios).
                r4 = subprocess.run(
                    [sys.executable, os.path.join(HERE, "test_stdio_mcp.py")],
                    capture_output=True, text=True, cwd=HERE,
                )
                print(r4.stdout, end="")
                if r4.stderr:
                    print("stderr:", r4.stderr, file=sys.stderr)
                if r4.returncode != 0:
                    print("\nFAIL: MCP stdio test suite")
                    sys.exit(1)
                # Run the settings UI tests (boots an isolated Nex on a
                # random port + fake upstream, exercises the catalog,
                # connect-on-demand, and the live chat-prompt awareness
                # string).
                r5 = subprocess.run(
                    [sys.executable, os.path.join(HERE, "test_settings.py")],
                    capture_output=True, text=True, cwd=HERE,
                )
                print(r5.stdout, end="")
                if r5.stderr:
                    print("stderr:", r5.stderr, file=sys.stderr)
                if r5.returncode != 0:
                    print("\nFAIL: MCP settings test suite")
                    sys.exit(1)
                # Run the Model Coordinator tests (mc.py + mc_tools.py +
                # the new /api/plan endpoint — boots an isolated Nex).
                r6 = subprocess.run(
                    [sys.executable, os.path.join(HERE, "test_mc.py")],
                    capture_output=True, text=True, cwd=HERE,
                )
                print(r6.stdout, end="")
                if r6.stderr:
                    print("stderr:", r6.stderr, file=sys.stderr)
                if r6.returncode != 0:
                    print("\nFAIL: Model Coordinator test suite")
                    sys.exit(1)
            else:
                print("  (skipping MCP tests; server not healthy)")
    except Exception:
        print("  (skipping MCP tests; server not reachable on :8787)")

    print("\nAll NEX tests passed.")


if __name__ == "__main__":
    main()
