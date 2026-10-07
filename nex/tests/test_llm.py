"""Tests for agent/llm.py — the purpose-aware model-call boundary.

call() is the one place that decides whether an llm callable receives the
trusted `purpose` tag (temperatures, structured-output mode, telemetry) or
is treated as a plain `llm(messages)` callable (tests, custom integrations).
Getting the branch wrong either silently drops purpose-aware behavior for a
provider that supports it, or crashes a plain callable that does not.
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

llm_mod = importlib.import_module("agent.llm")
call = llm_mod.call

_FAILED = []


def expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        _FAILED.append(msg)


class PlainLLM:
    """No supports_purpose attribute at all — the common/simple case."""
    def __init__(self):
        self.calls = []

    def __call__(self, messages):
        self.calls.append(("plain", messages))
        return "plain reply"


class PurposeAwareLLM:
    def __init__(self):
        self.calls = []
        self.supports_purpose = True

    def __call__(self, messages, purpose=None):
        self.calls.append((messages, purpose))
        return "purpose reply: %s" % purpose


class ExplicitlyUnawareLLM:
    """supports_purpose explicitly False, not just absent."""
    def __init__(self):
        self.calls = []
        self.supports_purpose = False

    def __call__(self, messages):
        self.calls.append(messages)
        return "unaware reply"


def main() -> None:
    msgs = [{"role": "user", "content": "hi"}]

    plain = PlainLLM()
    result = call(plain, msgs, "chat")
    expect(result == "plain reply",
           "a callable with no supports_purpose attribute is called plainly")
    expect(plain.calls == [("plain", msgs)],
           "the plain callable receives only messages, not purpose")

    aware = PurposeAwareLLM()
    result2 = call(aware, msgs, "planning")
    expect(result2 == "purpose reply: planning",
           "a callable with supports_purpose=True receives the purpose")
    expect(aware.calls == [(msgs, "planning")],
           "the purpose-aware callable is invoked with purpose as a kwarg")

    unaware = ExplicitlyUnawareLLM()
    result3 = call(unaware, msgs, "evaluation")
    expect(result3 == "unaware reply",
           "a callable with supports_purpose=False explicitly set is still "
           "called plainly (not just when the attribute is absent)")

    # A truthy-but-non-bool supports_purpose still takes the purpose branch —
    # getattr() only cares about truthiness, matching the documented contract.
    class TruthyAware:
        supports_purpose = "yes"

        def __call__(self, messages, purpose=None):
            return (messages, purpose)

    truthy = TruthyAware()
    result4 = call(truthy, msgs, "diagnosis")
    expect(result4 == (msgs, "diagnosis"),
           "a truthy non-bool supports_purpose still takes the purpose branch")

    # Different purposes for the same purpose-aware callable are forwarded
    # faithfully each time (no caching/staleness across calls).
    aware2 = PurposeAwareLLM()
    call(aware2, msgs, "chat")
    call(aware2, msgs, "planning")
    call(aware2, msgs, "evaluation")
    expect([p for _, p in aware2.calls] == ["chat", "planning", "evaluation"],
           "successive calls with different purposes are each forwarded "
           "correctly, independent of prior calls")

    if _FAILED:
        print("\n%d failing assertions." % len(_FAILED))
        sys.exit(1)
    print("\nAll llm tests passed.")


if __name__ == "__main__":
    main()
