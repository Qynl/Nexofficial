"""Deterministic diagnostic parsing for compiler/log/crash output.

agent/failures.py answers "what KIND of failure was this" (compilation,
runtime, ...). This module goes one step further for the two categories
where Unreal/UBT/Blueprint output follows documented, parseable formats:
it extracts the actual file, line, error code, and message — ground
truth pulled out of the text with regexes, never guessed by a model.

This is the deterministic half of the OBSERVE -> LOCALIZE -> INSPECT ->
DIAGNOSE workflow: a human (or an LLM) debugging a real Unreal error
does not re-read the whole log every time, they jump straight to
"File.cpp line 123, error C2065". When the output does NOT match any
known format, this says so explicitly (`deterministic=False`) instead
of inventing a location — that is exactly the case agent/diagnose.py's
one-shot LLM step exists for: ambiguous diagnosis the parser could not
resolve, never a replacement for what the parser COULD resolve.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CATEGORY_COMPILER = "compiler"
CATEGORY_BLUEPRINT = "blueprint"
CATEGORY_CRASH = "crash"
CATEGORY_UNKNOWN = "unknown"

# MSVC: C:\Path\File.cpp(123): error C2065: 'foo': undeclared identifier
_MSVC_RE = re.compile(
    r"(?P<file>[A-Za-z]:[\\/][^\r\n:()]+?|[^\s:()]+\.(?:cpp|h|hpp|cs))"
    r"\((?P<line>\d+)\)\s*:\s*(?:fatal\s+)?error\s+"
    r"(?P<code>[A-Z]+\d{3,5}):\s*(?P<message>[^\r\n]+)", re.IGNORECASE)

# Clang/GCC: /path/File.cpp:123:45: error: message
_CLANG_RE = re.compile(
    r"(?P<file>[^\s:()]+\.(?:cpp|h|hpp|mm|m))"
    r":(?P<line>\d+):(?:\d+:)?\s*(?:fatal\s+)?error:\s*(?P<message>[^\r\n]+)",
    re.IGNORECASE)

# UnrealBuildTool / UAT high-level failure lines without a precise location.
_UBT_RE = re.compile(
    r"(?:LogCompile|UATHelper|BUILD\s+FAILED)[^\r\n]*?:\s*Error:\s*"
    r"(?P<message>[^\r\n]+)", re.IGNORECASE)

# Linker errors: file.obj : error LNK2019: unresolved external symbol ...
_LINK_RE = re.compile(
    r"(?P<file>[^\s:()]+\.(?:obj|lib|exe|dll))\s*:\s*error\s+"
    r"(?P<code>LNK\d{4}):\s*(?P<message>[^\r\n]+)", re.IGNORECASE)

# Blueprint compiler: LogBlueprint: Error: message  OR  [Compiler] message
_BLUEPRINT_RE = re.compile(
    r"(?:LogBlueprint:\s*Error:|\[Compiler\])\s*(?P<message>[^\r\n]+)",
    re.IGNORECASE)

# Runtime crash / fatal error / assertion.
_CRASH_RE = re.compile(
    r"(?P<kind>Fatal error|Assertion failed|Unhandled [Ee]xception|"
    r"Access violation|Segmentation fault)\s*[:\-]?\s*(?P<message>[^\r\n]*)",
    re.IGNORECASE)

# A callstack frame as Unreal/Windows crash reporters print it:
# UnrealEditor-Module.dll!Namespace::Class::Method() [File.cpp:123]
_FRAME_RE = re.compile(
    r"(?P<module>[\w.\-]+)!(?P<symbol>[\w:<>~,\[\] ]+?)\(\)"
    r"(?:\s*\[(?P<file>[^\]:]+):(?P<line>\d+)\])?")


@dataclass
class Diagnostic:
    """One ground-truth finding, or an honest admission there isn't one."""
    deterministic: bool
    category: str
    file: str = ""
    line: int = 0
    code: str = ""
    symbol: str = ""
    message: str = ""
    frames: List[Dict[str, Any]] = field(default_factory=list)
    match_count: int = 0

    def to_public(self) -> Dict[str, Any]:
        return {
            "deterministic": self.deterministic,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "code": self.code,
            "symbol": self.symbol,
            "message": self.message[:300],
            "frames": self.frames[:5],
            "match_count": self.match_count,
        }

    def summary(self) -> str:
        """One line for a bounded LLM context — never the full raw blob."""
        if not self.deterministic:
            return "no deterministic location extracted"
        loc = ""
        if self.file:
            loc = "%s:%d " % (self.file, self.line) if self.line else \
                self.file + " "
        code = ("[%s] " % self.code) if self.code else ""
        return "%s%s%s" % (loc, code, self.message[:200])


def parse_compiler_output(text: str) -> List[Diagnostic]:
    """Every concrete compiler/linker error found, in document order."""
    text = str(text or "")
    out: List[Diagnostic] = []
    for m in _MSVC_RE.finditer(text):
        out.append(Diagnostic(True, CATEGORY_COMPILER, file=m.group("file"),
                              line=int(m.group("line")), code=m.group("code"),
                              message=m.group("message").strip()))
    for m in _LINK_RE.finditer(text):
        out.append(Diagnostic(True, CATEGORY_COMPILER, file=m.group("file"),
                              code=m.group("code"),
                              message=m.group("message").strip()))
    for m in _CLANG_RE.finditer(text):
        out.append(Diagnostic(True, CATEGORY_COMPILER, file=m.group("file"),
                              line=int(m.group("line")),
                              message=m.group("message").strip()))
    if not out:
        for m in _UBT_RE.finditer(text):
            out.append(Diagnostic(True, CATEGORY_COMPILER,
                                  message=m.group("message").strip()))
    return out[:20]


def parse_blueprint_errors(text: str) -> List[Diagnostic]:
    text = str(text or "")
    out: List[Diagnostic] = []
    for m in _BLUEPRINT_RE.finditer(text):
        msg = (m.group("message") or "").strip()
        if msg:
            out.append(Diagnostic(True, CATEGORY_BLUEPRINT, message=msg))
    return out[:20]


def parse_crash(text: str) -> Optional[Diagnostic]:
    text = str(text or "")
    m = _CRASH_RE.search(text)
    if not m:
        return None
    frames: List[Dict[str, Any]] = []
    for fm in _FRAME_RE.finditer(text):
        frames.append({
            "module": fm.group("module"),
            "symbol": fm.group("symbol").strip(),
            "file": fm.group("file") or "",
            "line": int(fm.group("line")) if fm.group("line") else 0,
        })
    message = (m.group("message") or "").strip() or m.group("kind")
    return Diagnostic(True, CATEGORY_CRASH, message=message,
                      symbol=frames[0]["symbol"] if frames else "",
                      frames=frames, match_count=len(frames))


def diagnose_failure(error_text: str, failure_kind: str) -> Diagnostic:
    """The single entry point agent/loop.py calls on every failed task.

    Dispatches on the already-computed agent/failures.py category so the
    (more expensive, more specific) parsers only run when the category
    makes them plausible. Anything not recognized returns an honest
    `deterministic=False` record — never a confident-looking guess.
    """
    text = str(error_text or "")
    kind = str(failure_kind or "")
    if kind == "compilation":
        hits = parse_compiler_output(text)
        if hits:
            top = hits[0]
            top.match_count = len(hits)
            return top
        bp_hits = parse_blueprint_errors(text)
        if bp_hits:
            top = bp_hits[0]
            top.match_count = len(bp_hits)
            return top
    elif kind == "runtime":
        crash = parse_crash(text)
        if crash is not None:
            return crash
    elif kind == "unknown":
        # An unclassified failure might still have a recognizable shape —
        # try every parser once before giving up (bounded, cheap regexes).
        hits = parse_compiler_output(text) or parse_blueprint_errors(text)
        if hits:
            top = hits[0]
            top.match_count = len(hits)
            return top
        crash = parse_crash(text)
        if crash is not None:
            return crash
    return Diagnostic(False, kind or CATEGORY_UNKNOWN, message=text[:300])
