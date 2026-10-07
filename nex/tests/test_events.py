"""Tests for agent/events.py — the run-event taxonomy the UI renders.

emit()/run_event() are the only path from the agent loop to the frontend's
run view. The contract that matters most: emit() must never raise (a
broken bus must not break a run), and the event's own identity fields
(type/run_id/ts) must always reflect reality, even if a caller's payload
happens to carry same-named keys.
"""
import importlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

events = importlib.import_module("agent.events")

_FAILED = []


def expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        _FAILED.append(msg)


def main() -> None:
    # --- run_event() shape ---------------------------------------------
    before = time.time()
    ev = events.run_event("run-1", "run.step", step={"id": "t1"}, extra=42)
    after = time.time()
    expect(ev["type"] == "run.step", "run_event sets the event type")
    expect(ev["run_id"] == "run-1", "run_event sets the run id")
    expect(before <= ev["ts"] <= after, "run_event stamps a current timestamp")
    expect(ev["step"] == {"id": "t1"} and ev["extra"] == 42,
           "run_event forwards arbitrary payload kwargs")

    # --- regression: a payload cannot spoof the identity fields -----------
    # 'run_id' and 'event_type' are real positional parameter names, so a
    # payload carrying those collides at the call site (a clear TypeError,
    # not a silent spoof). 'type' and 'ts' are not parameter names, so they
    # DO reach **payload — these are exactly the ones that used to be able
    # to silently override the real values before the fix.
    spoofed = events.run_event("real-run", "run.step",
                                type="run.HACKED", ts=0.0, detail="x")
    expect(spoofed["type"] == "run.step",
           "a payload key named 'type' cannot override the real event type "
           "(regression: core fields must win over a same-named payload key)")
    expect(spoofed["ts"] != 0.0 and spoofed["ts"] >= before,
           "a payload key named 'ts' cannot override the real timestamp")
    expect(spoofed["run_id"] == "real-run",
           "the run id is the one actually passed in")
    expect(spoofed["detail"] == "x",
           "non-colliding payload keys are still forwarded normally")

    # --- emit() with a working bus ---------------------------------------
    received = []
    events.emit(received.append, "run-2", "run.tool", tool="demo.spawn")
    expect(len(received) == 1 and received[0]["type"] == "run.tool"
           and received[0]["tool"] == "demo.spawn",
           "emit() publishes a well-formed event on a working bus")

    # --- emit() with no bus -----------------------------------------------
    try:
        events.emit(None, "run-3", "run.step")
        ok = True
    except Exception:
        ok = False
    expect(ok, "emit() is a silent no-op when bus is None")

    # --- emit() never raises, even when the bus itself raises -------------
    def broken_bus(_event):
        raise RuntimeError("bus exploded")

    try:
        events.emit(broken_bus, "run-4", "run.step")
        ok2 = True
    except Exception:
        ok2 = False
    expect(ok2, "emit() swallows an exception raised by the bus itself — "
                "a broken subscriber must never break a run")

    # --- PHASES / status constants are the stable vocabulary the frontend
    # --- relies on -- a rename here is a breaking change for the UI ------
    expect(events.PHASES == (
        "planning", "executing", "evaluating", "adapting",
        "waiting", "finishing"),
        "the PHASES tuple is the documented, stable set the frontend expects")
    expect({events.STATUS_COMPLETED, events.STATUS_PARTIAL,
            events.STATUS_FAILED, events.STATUS_BLOCKED,
            events.STATUS_WAITING_USER, events.STATUS_CANCELLED} == {
        "completed", "partial", "failed", "blocked",
        "waiting_user", "cancelled"},
        "the run-outcome status constants match their documented string "
        "values")

    if _FAILED:
        print("\n%d failing assertions." % len(_FAILED))
        sys.exit(1)
    print("\nAll events tests passed.")


if __name__ == "__main__":
    main()
