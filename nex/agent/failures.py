"""Failure classification taxonomy.

A single generic "error" string hides very different recovery situations:
a dropped connection is not the same problem as a missing argument, which
is not the same problem as a C++ compile error, which is not the same
problem as a crash during play. This module gives every failed task a
best-effort CATEGORY so the final report — and, eventually, a smarter
recovery strategy — can tell them apart instead of lumping everything
into one undifferentiated "failed" bucket.

This is deliberately a CLASSIFIER, not a new recovery path: it changes
nothing about what agent/loop.py's recovery ladder (retry -> fix args ->
switch tool -> honest failure) actually does. It only labels the outcome
more usefully, from the error text and the tool's own declared evidence
gates — both already-trusted-enough signals used elsewhere in this
codebase (agent/quality.py tool_gates(), the `_is_transient` regex in
agent/loop.py). Classification is heuristic and best-effort: an
ambiguous or unrecognized error honestly reports UNKNOWN rather than
guessing one of the specific categories.
"""
from __future__ import annotations

import re
from typing import Optional, Set

TRANSIENT = "transient"
SCHEMA = "schema"
ARGUMENT = "argument"
COMPILATION = "compilation"
RUNTIME = "runtime"
GAMEPLAY = "gameplay"
VISUAL = "visual"
PERFORMANCE = "performance"
ARCHITECTURE = "architecture"
MISSING_CAPABILITY = "missing_capability"
UNKNOWN = "unknown"

ALL_KINDS = (
    TRANSIENT, SCHEMA, ARGUMENT, COMPILATION, RUNTIME, GAMEPLAY, VISUAL,
    PERFORMANCE, ARCHITECTURE, MISSING_CAPABILITY, UNKNOWN,
)

_LABELS = {
    TRANSIENT: "Transient (network/timeout/rate-limit) — safe to retry",
    SCHEMA: "Schema/validation mismatch against the tool's declared contract",
    ARGUMENT: "Missing or invalid argument",
    COMPILATION: "Build/compile failure",
    RUNTIME: "Runtime crash or exception",
    GAMEPLAY: "Gameplay-system failure",
    VISUAL: "Visual/rendering failure",
    PERFORMANCE: "Performance/budget failure",
    ARCHITECTURE: "Architectural/structural failure",
    MISSING_CAPABILITY: "The connected MCP surface cannot do this",
    UNKNOWN: "Unclassified",
}

# Suggested first move per category — advisory text for a report or a
# future smarter recovery strategy, never executed automatically here.
_STRATEGY = {
    TRANSIENT: "retry the same call; it may simply have dropped once",
    SCHEMA: "re-read the tool's live schema and reshape the arguments",
    ARGUMENT: "inspect the schema for the exact missing/invalid field",
    COMPILATION: "read the compiler output, find the changed file, repair "
                "it, and compile again before doing anything else",
    RUNTIME: "inspect runtime logs for the crashing subsystem, repair it, "
            "then restart and reproduce the same scenario",
    GAMEPLAY: "re-run the specific playtest objective that exercised this "
             "system",
    VISUAL: "capture a fresh screenshot after the fix and compare it "
           "against the one that showed the defect",
    PERFORMANCE: "profile again after the fix; a claim of 'optimized' "
                "needs a new measurement, not just a code change",
    ARCHITECTURE: "pay down the specific structural issue before adding "
                 "more content on top of it",
    MISSING_CAPABILITY: "do not keep retrying; report this as unavailable "
                        "through the current MCP capability and move on",
    UNKNOWN: "treat as architecture/runtime until more evidence narrows it",
}

_TRANSIENT_RE = re.compile(
    r"(timeout|timed out|unreachable|connection reset|econnrefused|"
    r"refused|503|502|504|circuit breaker|rate limit|temporarily|"
    r"broken pipe|reset by peer)", re.IGNORECASE)

_MISSING_CAPABILITY_RE = re.compile(
    r"(no mcp servers|not connected|no tool|unknown tool|tool not found|"
    r"no server|not available through|not permitted|policy-denied|"
    r"capability (?:is )?(?:not available|unavailable)|"
    r"unsupported (?:action|tool))", re.IGNORECASE)

_SCHEMA_RE = re.compile(
    r"(schema|validation failed|does not match|additionalproperties|"
    r"required propert(?:y|ies)|type error|not of type|pattern mismatch|"
    r"enum value)", re.IGNORECASE)

_ARGUMENT_RE = re.compile(
    r"(missing (?:required )?(?:argument|parameter|field)|"
    r"required (?:argument|parameter|field)|unknown parameter|"
    r"no parameter named|invalid argument|unexpected keyword|"
    r"unrecognized argument)", re.IGNORECASE)

_COMPILATION_RE = re.compile(
    r"(compil|unrealbuildtool|\bubt\b|build failed|linker|link error|"
    r"syntax error|cs\d{4}|c\d{4}:|undefined reference|undeclared "
    r"identifier)", re.IGNORECASE)

_RUNTIME_RE = re.compile(
    r"(crash(?:ed)?|exception|stack trace|segfault|segmentation fault|"
    r"access violation|null reference|nullreferenceexception|"
    r"unhandled error|fatal error|assert(?:ion)? failed)", re.IGNORECASE)

_VISUAL_RE = re.compile(
    r"(shader|material (?:compile|error)|texture (?:missing|error)|"
    r"render(?:ing)? (?:failed|error)|screenshot failed|capture failed|"
    r"viewport (?:error|failed))", re.IGNORECASE)

_PERFORMANCE_RE = re.compile(
    r"(performance budget|frame ?time|fps dropped|memory budget|"
    r"out of memory|oom\b|draw call budget|profiler (?:error|failed))",
    re.IGNORECASE)

_ARCHITECTURE_RE = re.compile(
    r"(circular dependency|duplicate (?:system|definition)|"
    r"architecture|excessive coupling|broken reference)", re.IGNORECASE)

_GAMEPLAY_RE = re.compile(
    r"(npc (?:failed|stuck|unresponsive)|mission (?:failed|broken)|"
    r"quest (?:failed|broken)|vehicle (?:failed|stuck)|ai behavior "
    r"failed|navmesh (?:missing|invalid)|pathfinding failed)",
    re.IGNORECASE)


def classify_failure(error: Optional[str], tool_name: str = "",
                      gates: Optional[Set[str]] = None) -> str:
    """Best-effort category for one failed task.

    Order matters: the most specific/actionable signal wins first, so a
    compile error that also happens to mention a missing include file is
    reported as COMPILATION, not ARGUMENT. `gates` (the tool's own quality
    gates from agent/quality.tool_gates) breaks ties for a generic error
    using what KIND of tool failed, never upgrading a clearly-transient
    network error into something domain-specific.
    """
    text = str(error or "")
    gates = set(gates or ())

    # Most specific / most actionable signal wins first. Domain-specific
    # categories (visual, performance, architecture, gameplay) are
    # checked before the broad, generic compilation/runtime patterns so
    # e.g. "material compile error" is VISUAL, not a bare COMPILATION.
    if _MISSING_CAPABILITY_RE.search(text):
        return MISSING_CAPABILITY
    if _TRANSIENT_RE.search(text):
        return TRANSIENT
    if _SCHEMA_RE.search(text):
        return SCHEMA
    if _ARGUMENT_RE.search(text):
        return ARGUMENT
    if _VISUAL_RE.search(text):
        return VISUAL
    if _PERFORMANCE_RE.search(text):
        return PERFORMANCE
    if _ARCHITECTURE_RE.search(text):
        return ARCHITECTURE
    if _GAMEPLAY_RE.search(text):
        return GAMEPLAY
    if _COMPILATION_RE.search(text):
        return COMPILATION
    if _RUNTIME_RE.search(text):
        return RUNTIME
    # No specific textual signal at all — fall back to the tool's own
    # declared discipline, but only when exactly one unambiguous gate
    # applies; a tool with mixed gates gives no safe single answer.
    if len(gates) == 1:
        only = next(iter(gates))
        if only == "build":
            return COMPILATION
        if only == "performance":
            return PERFORMANCE
        if only in ("visual", "visual_review"):
            return VISUAL
    return UNKNOWN


def label(kind: str) -> str:
    return _LABELS.get(kind, kind)


def recovery_strategy(kind: str) -> str:
    return _STRATEGY.get(kind, _STRATEGY[UNKNOWN])
