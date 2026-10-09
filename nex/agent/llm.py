"""Small compatibility boundary for purpose-aware model calls.

The agent core accepts ordinary ``llm(messages)`` callables in tests and custom
integrations. Nex's provider router additionally accepts a trusted purpose tag
so it can apply deterministic temperatures, structured-output mode and useful
telemetry without inferring intent from untrusted prompt text.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


def call(llm: Callable, messages: List[Dict[str, str]], purpose: str) -> Any:
    """Invoke an LLM with a purpose when it explicitly supports that contract."""
    if getattr(llm, "supports_purpose", False):
        return llm(messages, purpose=purpose)
    return llm(messages)


def call_with_images(llm: Callable, messages: List[Dict[str, str]],
                     purpose: str, images: List[Dict[str, str]]
                     ) -> Optional[str]:
    """Invoke an LLM with real image bytes, but ONLY when it explicitly
    declares it can read them (``supports_images`` truthy).

    Most test doubles and some configured providers/models are text-only.
    Sending one a "critique this screenshot" prompt it never received the
    picture for would just produce confident-sounding fiction about
    pixels it never saw — worse than no critique at all. Returning
    ``None`` means "no genuine visual critique is possible right now";
    callers must treat that as a skip, never as a verdict one way or the
    other.
    """
    if not images or not getattr(llm, "supports_images", False):
        return None
    try:
        return llm(messages, purpose=purpose, images=images)
    except TypeError:
        # A callable that declared supports_images but has an
        # incompatible signature must not crash the run over an
        # auxiliary, additive check.
        return None
