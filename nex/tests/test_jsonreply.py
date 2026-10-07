"""Tests for the model-reply JSON extractor (agent/jsonreply.py).

extract_json_with_key() is how every consumer of model output (planner,
evaluator, diagnose, and critically the chat ACT directive that decides
whether an autonomous run starts) finds the structured object inside an
LLM's prose. A gap here either misses a real directive outright, or lets
pathological input burn unbounded CPU parsing it.
"""
import importlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

jsonreply = importlib.import_module("agent.jsonreply")
extract_json = jsonreply.extract_json
extract_json_with_key = jsonreply.extract_json_with_key

_FAILED = []


def expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        _FAILED.append(msg)


def main() -> None:
    # --- baseline: plain, fenced, and prose-wrapped JSON --------------------
    expect(extract_json('{"act": "go"}') == {"act": "go"},
           "a bare JSON object is parsed")
    expect(extract_json_with_key('{"act": "go"}', "act") == {"act": "go"},
           "a bare JSON object with the sought key is returned")
    expect(extract_json_with_key('{"other": 1}', "act") is None,
           "an object missing the sought key is rejected")
    expect(extract_json("no json here at all") is None,
           "prose with no JSON returns None")
    expect(extract_json("") is None and extract_json_with_key("", "act") is None,
           "an empty reply returns None")

    fenced = "Here you go:\n```json\n{\"act\": \"build\"}\n```\nthanks"
    expect(extract_json_with_key(fenced, "act") == {"act": "build"},
           "a fenced ```json block is parsed")

    prose = 'I will now proceed. {"act": "go", "reason": "ready"} Done.'
    expect(extract_json_with_key(prose, "act") == {"act": "go", "reason": "ready"},
           "a JSON object embedded in surrounding prose is found")

    # --- nesting and in-string braces ---------------------------------------
    nested_fence = (
        "```json\n{\"act\": \"go\", \"meta\": {\"a\": 1, \"b\": 2}}\n```"
    )
    expect(extract_json_with_key(nested_fence, "act") ==
           {"act": "go", "meta": {"a": 1, "b": 2}},
           "a nested object inside a fenced block is still parsed correctly "
           "(the fence regex is non-greedy and alone would truncate at the "
           "inner '}' — this must fall through to the balanced scanner)")

    in_string = '{"act": "use {curly} braces in a string", "ok": true}'
    expect(extract_json_with_key(in_string, "act") ==
           {"act": "use {curly} braces in a string", "ok": True},
           "literal braces inside a quoted string value do not confuse "
           "the balanced-brace scanner")

    # --- multiple candidate objects ------------------------------------------
    multi = 'junk {"foo": 1} more junk {"act": "go"} trailing'
    expect(extract_json_with_key(multi, "act") == {"act": "go"},
           "when several objects are present, the one carrying the sought "
           "key is returned even if it is not the first object")

    # --- regression: a stray unmatched '{' earlier in the reply must not ---
    # --- hide a perfectly valid object that follows it ----------------------
    stray_then_valid = ('Sure, I will use { as a placeholder here. '
                         'Now the real directive: {"act": "build the level"}')
    expect(extract_json_with_key(stray_then_valid, "act") ==
           {"act": "build the level"},
           "an earlier stray, never-closed '{' (e.g. mentioned in prose) "
           "does not block discovery of a valid object later in the reply "
           "(regression: the scanner used to bail out entirely on the "
           "first unbalanced '{' instead of trying the next one)")

    two_strays_then_valid = (
        "{ and another { stray brace, then the actual answer: "
        '{"act": "go"}'
    )
    expect(extract_json_with_key(two_strays_then_valid, "act") == {"act": "go"},
           "multiple stray unmatched '{' before the real object still "
           "resolve to the real object")

    truncated_then_valid = (
        'First attempt: {"act": "first", "note": "cut off right here" '
        'then the model retries: {"act": "second"}'
    )
    expect(extract_json_with_key(truncated_then_valid, "act") ==
           {"act": "second"},
           "an object truncated before its closing brace (but with all "
           "string literals properly terminated — the realistic shape of "
           "a model hitting a token limit) followed by a complete retry "
           "resolves to the complete retry")

    # --- bounded worst-case cost ---------------------------------------------
    many_small_strays = ("{x " * 50000) + '{"act": "finally"}'
    t0 = time.time()
    result = extract_json_with_key(many_small_strays, "act")
    elapsed = time.time() - t0
    expect(result == {"act": "finally"},
           "many small scattered stray braces before a valid object still "
           "resolve correctly")
    expect(elapsed < 1.0,
           "scanning thousands of scattered stray braces stays fast "
           "(%.3fs) — this used to be an accidental O(n^2) scan" % elapsed)

    huge_unmatched = "{" * 100000 + '"act": "go"}'
    t0 = time.time()
    extract_json_with_key(huge_unmatched, "act")
    elapsed2 = time.time() - t0
    expect(elapsed2 < 1.0,
           "a huge run of unmatched '{' characters is bounded work, not "
           "unbounded (%.3fs)" % elapsed2)

    # --- malformed JSON is rejected, not crashed on --------------------------
    expect(extract_json_with_key('{"act": "go",}', "act") is None,
           "trailing comma makes the object invalid JSON — returns None, "
           "does not raise")
    expect(extract_json_with_key("{not even json}", "act") is None,
           "non-JSON braces content returns None without raising")

    if _FAILED:
        print("\n%d failing assertions." % len(_FAILED))
        sys.exit(1)
    print("\nAll jsonreply tests passed.")


if __name__ == "__main__":
    main()
