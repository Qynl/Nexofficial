"""Small compatibility boundary for purpose-aware model calls.

The agent core accepts ordinary ``llm(messages)`` callables in tests and custom
integrations. Nex's provider router additionally accepts a trusted purpose tag
so it can apply deterministic temperatures, structured-output mode and useful
telemetry without inferring intent from untrusted prompt text.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List


def call(llm: Callable, messages: List[Dict[str, str]], purpose: str) -> Any:
    """Invoke an LLM with a purpose when it explicitly supports that contract."""
    if getattr(llm, "supports_purpose", False):
        return llm(messages, purpose=purpose)
    return llm(messages)
