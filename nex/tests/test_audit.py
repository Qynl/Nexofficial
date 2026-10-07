"""Tests for mcp/audit.py — the structured audit log.

This is the record every operator relies on to see WHAT was called, on
WHICH server, WHETHER policy allowed it, and a redacted shape of the
arguments — never full secrets or huge payloads. Two things matter most:
secrets never leak into the log, and the log never grows unbounded.
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

audit = importlib.import_module("mcp.audit")
AuditLog = audit.AuditLog
_arg_summary = audit._arg_summary
_value_shape = audit._value_shape

_FAILED = []


def expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        _FAILED.append(msg)


def main() -> None:
    # --- value shaping never includes raw string/bytes content -------------
    expect(_value_shape("a secret string") == "<string:15 chars>",
           "a string value is described only by its length, never its content")
    expect(_value_shape(b"abcdef") == "<bytes:6>",
           "a bytes value is described only by its length")
    expect(_value_shape({"a": 1, "b": 2}) == "<object:2 keys>",
           "a nested dict is described only by its key count, never its "
           "contents (so a secret nested two levels deep still never "
           "surfaces as text)")
    expect(_value_shape([1, 2, 3]) == "<array:3 items>",
           "a list/array is described only by its length")
    expect(_value_shape(None) is None and _value_shape(True) is True
           and _value_shape(5) == 5 and _value_shape(1.5) == 1.5,
           "primitives (None/bool/int/float) pass through as-is — they "
           "cannot carry a secret payload")

    # --- secret-named keys are redacted, across casing/separator styles ---
    for key in ("password", "PASSWORD", "api_key", "API-KEY", "apikey",
                "Authorization", "cookie", "credential", "private_key",
                "auth_token", "access_token", "my_secret"):
        got = _arg_summary({key: "sensitive-value-123"})
        expect(got[key] == "<redacted>",
               "a secret-looking key %r is fully redacted (not even its "
               "length is shown)" % key)

    # --- ordinary keys keep their shape, not their value -------------------
    normal = _arg_summary({"path": "/scene/root", "count": 3, "ok": True})
    expect(normal["count"] == 3 and normal["ok"] is True,
           "non-string primitive values pass through unredacted")
    expect(normal["path"] == "<string:11 chars>",
           "an ordinary string argument is shown only as a length, never "
           "its content (the log is a shape record, not a payload dump)")

    # --- known, intentional behavior: compound keys containing a secret
    # --- word as a SUBSTRING are also redacted, even when the key is not
    # --- actually a credential (e.g. 'max_tokens' contains 'token'). This
    # --- is accepted on purpose: a redaction rule should fail toward
    # --- over-redacting (losing some log readability) rather than ever
    # --- under-redacting (leaking a real secret) --------------------------
    over_redacted = _arg_summary({"max_tokens": 500, "token_budget": 100})
    expect(over_redacted["max_tokens"] == "<redacted>"
           and over_redacted["token_budget"] == "<redacted>",
           "compound non-secret keys that merely CONTAIN a sensitive word "
           "are also redacted — an intentional fail-safe bias, not a bug")

    # --- a huge number of keys does not blow up the summary ---------------
    huge = {("k%d" % i): i for i in range(500)}
    summarized = _arg_summary(huge)
    expect(len(summarized) <= 51,
           "an argument object with hundreds of keys is capped, with an "
           "explicit 'additional keys omitted' marker, not dumped whole")
    expect("…" in summarized, "the omission is marked explicitly")

    # --- record(): args=None vs args={} are distinguishable -----------------
    log = AuditLog()
    rec_none = log.record("connect", server="s")
    rec_empty = log.record("call", server="s", tool="t", args={})
    rec_filled = log.record("call", server="s", tool="t", args={"x": 1})
    expect(rec_none["args"] is None,
           "a record with no args object at all has args=None")
    expect(rec_empty["args"] == {},
           "a record with an explicitly empty args dict keeps that "
           "distinction (regression: used to collapse to the same None as "
           "'no args at all', because of a plain truthiness check)")
    expect(rec_filled["args"] == {"x": 1},
           "a record with real args keeps its (shaped) content")

    # --- record() shape and bounded fields ----------------------------------
    rec = log.record("call", server="srv", tool="do_thing", ok=False,
                      detail="x" * 10000, duration_ms=12.5,
                      context={"conversation_id": "c1"})
    expect(rec["server"] == "srv" and rec["tool"] == "do_thing",
           "server/tool are recorded as given")
    expect(rec["ok"] is False, "the ok flag is recorded as given")
    expect(len(rec["detail"]) == 300,
           "an overlong detail string is bounded, not stored unbounded")
    expect(rec["duration_ms"] == 12.5, "duration is recorded as given")
    expect(rec["context"] == {"conversation_id": "c1"},
           "context is passed through")
    expect(isinstance(rec["ts"], float) and rec["ts"] > 0,
           "every record carries a timestamp")

    # --- bounded ring buffer: the log never grows past maxlen ---------------
    bounded = AuditLog(maxlen=5)
    for i in range(20):
        bounded.record("call", server="s", tool="t%d" % i)
    all_entries = bounded.to_list()
    expect(len(all_entries) == 5,
           "the audit log never exceeds its configured maxlen even after "
           "far more records than that have been made")
    expect(all_entries[-1]["tool"] == "t19",
           "the most recent record is kept")
    expect(all_entries[0]["tool"] == "t15",
           "the oldest records are evicted first (a true ring buffer, not "
           "a silently-growing list)")

    # --- recent(n) returns the last n, bounded by what actually exists -----
    some = AuditLog(maxlen=100)
    for i in range(3):
        some.record("call", server="s", tool="t%d" % i)
    expect(len(some.recent(50)) == 3,
           "recent(n) does not pad or error when fewer than n records exist")
    expect(some.recent(2)[-1]["tool"] == "t2",
           "recent(n) returns the most recent n records in order")

    if _FAILED:
        print("\n%d failing assertions." % len(_FAILED))
        sys.exit(1)
    print("\nAll audit tests passed.")


if __name__ == "__main__":
    main()
