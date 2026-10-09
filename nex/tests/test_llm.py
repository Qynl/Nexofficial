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
call_with_images = llm_mod.call_with_images

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

    # --- call_with_images: the genuine-visual-critique boundary -----------
    IMAGES = [{"mime_type": "image/png", "data": "Zm9v"}]

    class VisionAwareLLM:
        def __init__(self):
            self.calls = []
            self.supports_purpose = True
            self.supports_images = True

        def __call__(self, messages, purpose=None, images=None):
            self.calls.append((messages, purpose, images))
            return "critique: %d image(s)" % len(images or [])

    vision = VisionAwareLLM()
    result5 = call_with_images(vision, msgs, "visual-review", IMAGES)
    expect(result5 == "critique: 1 image(s)",
           "a callable that declares supports_images=True is actually "
           "sent the images and its real reply is returned")
    expect(vision.calls == [(msgs, "visual-review", IMAGES)],
           "the vision-aware callable receives messages, purpose, and "
           "images exactly as given")

    # A plain callable (the common case — most test doubles and any
    # text-only configured model) has no supports_images attribute at
    # all. It must NEVER be handed images it cannot read.
    plain_no_vision = PlainLLM()
    result6 = call_with_images(plain_no_vision, msgs, "visual-review", IMAGES)
    expect(result6 is None,
           "a callable with no supports_images attribute is never called "
           "with images — None means 'no genuine critique possible', not "
           "a fabricated one")
    expect(plain_no_vision.calls == [],
           "the text-only callable is not invoked at all when a visual "
           "critique is requested (it would have no way to honor it)")

    # supports_images explicitly False is honored exactly like absence.
    class ExplicitlyNoVision:
        supports_purpose = True
        supports_images = False

        def __call__(self, messages, purpose=None):
            return "should never be reached"

    result7 = call_with_images(ExplicitlyNoVision(), msgs, "visual-review",
                               IMAGES)
    expect(result7 is None,
           "supports_images=False explicitly set is honored exactly like "
           "an absent attribute — no call is made")

    # No images to attach means there is nothing to critique — skip
    # cleanly even for a vision-capable callable, never call with an
    # empty image list.
    vision2 = VisionAwareLLM()
    result8 = call_with_images(vision2, msgs, "visual-review", [])
    expect(result8 is None,
           "an empty images list skips the call even for a vision-aware "
           "callable — there is nothing to look at")
    expect(vision2.calls == [],
           "a vision-aware callable is not invoked when there are no "
           "images to send it")

    # The guard must be the supports_images ATTRIBUTE, never merely
    # "does this callable's signature happen to accept images=...".  A
    # callable that COULD technically accept images but never declared
    # support for them must still never receive any — otherwise a
    # text-only model with a permissive *args/**kwargs signature would
    # silently be asked to critique pixels it cannot read.
    class SignatureAcceptsImagesButDoesNotDeclareSupport:
        supports_purpose = True
        # deliberately no supports_images attribute at all

        def __init__(self):
            self.calls = []

        def __call__(self, messages, purpose=None, images=None):
            self.calls.append((messages, purpose, images))
            return "should never be reached"

    permissive = SignatureAcceptsImagesButDoesNotDeclareSupport()
    result10 = call_with_images(permissive, msgs, "visual-review", IMAGES)
    expect(result10 is None,
           "a callable whose signature could accept images, but which "
           "never declared supports_images, is still never called — the "
           "attribute is the only thing that grants vision use, not an "
           "accommodating signature")
    expect(permissive.calls == [],
           "the permissive-signature callable is not invoked at all")

    # A callable that claims vision support but has an incompatible
    # signature must not crash the run over this auxiliary check.
    class BrokenVisionLLM:
        supports_images = True

        def __call__(self, messages):  # no purpose/images kwargs
            return "unreachable"

    result9 = call_with_images(BrokenVisionLLM(), msgs, "visual-review",
                               IMAGES)
    expect(result9 is None,
           "a vision-aware callable with an incompatible signature fails "
           "closed (None), it does not raise and crash the run")

    if _FAILED:
        print("\n%d failing assertions." % len(_FAILED))
        sys.exit(1)
    print("\nAll llm tests passed.")


if __name__ == "__main__":
    main()
