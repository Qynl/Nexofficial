"""Roblox performance intelligence — measure, don't guess.

Parses real numeric measurements out of successful profiling-tool
results (never invents a number), and implements the brief's required
discipline explicitly: MEASURE -> CHANGE -> MEASURE AGAIN -> COMPARE.
Bottleneck IDENTIFICATION is left to the LLM/diagnosis layer (it needs
judgment this module cannot fake); everything here is either a parsed
number or a deterministic comparison between two already-parsed numbers.

Budgets are project-specific and must be supplied explicitly by the
caller (there is no UI yet for a user to define them — see the final
report's Known Limitations) wherever this is wired in.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

PASS = "PASS"
FAIL = "FAIL"
UNVERIFIED = "UNVERIFIED"

# metric id -> regex capturing a bare number right after a label. Deliberately
# narrow (case-insensitive label + unit) so unrelated prose never matches.
_METRIC_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("client_frame_time_ms",
     re.compile(r"(?i)client[_ ]?frame[_ ]?time\D{0,6}([\d.]+)\s*ms")),
    ("server_frame_time_ms",
     re.compile(r"(?i)server[_ ]?frame[_ ]?time\D{0,6}([\d.]+)\s*ms")),
    ("fps", re.compile(r"(?i)\bfps\D{0,6}([\d.]+)")),
    ("memory_mb", re.compile(r"(?i)memory\D{0,6}([\d.]+)\s*mb")),
    ("script_time_ms",
     re.compile(r"(?i)script[_ ]?(?:execution|time)\D{0,6}([\d.]+)\s*ms")),
    ("network_kbps", re.compile(r"(?i)network\D{0,6}([\d.]+)\s*kb")),
    ("load_time_s", re.compile(r"(?i)load[_ ]?time\D{0,6}([\d.]+)\s*s")),
)

# Metrics where a LOWER number is better. Everything not listed (fps) is
# treated as higher-is-better.
_LOWER_IS_BETTER = {"client_frame_time_ms", "server_frame_time_ms",
                    "memory_mb", "script_time_ms", "network_kbps",
                    "load_time_s"}

_PERF_TOOL_HINTS = ("microprofiler", "micro_profiler", "script_profiler",
                    "performance_stats", "memory_stats", "frame_time",
                    "frametime", "benchmark")


def extract_measurements(text: str) -> Dict[str, float]:
    """Every metric this text actually states a number for. Never a guess:
    a metric absent from the text is simply absent from the result."""
    out: Dict[str, float] = {}
    for key, pattern in _METRIC_PATTERNS:
        m = pattern.search(text or "")
        if m:
            try:
                out[key] = float(m.group(1))
            except ValueError:
                continue
    return out


def collect_evidence(tasks: Iterable[Any]) -> List[Dict[str, Any]]:
    """One entry per successful performance-flavored call that actually
    stated a parseable number, in call order."""
    out: List[Dict[str, Any]] = []
    for t in tasks:
        if getattr(t, "status", None) != "success":
            continue
        tool = (getattr(t, "tool", "") or "").lower()
        if not any(h in tool for h in _PERF_TOOL_HINTS):
            continue
        measurements = extract_measurements(str(getattr(t, "result", "") or ""))
        if measurements:
            out.append({"tool": getattr(t, "tool", ""),
                       "measurements": measurements})
    return out


def evaluate_budgets(evidence: List[Dict[str, Any]],
                     budgets: Dict[str, float]) -> Dict[str, Dict[str, Any]]:
    """Compare the MOST RECENT measurement of each budgeted metric against
    its limit. A metric never measured this run is UNVERIFIED, never
    assumed to pass."""
    results: Dict[str, Dict[str, Any]] = {}
    for metric, limit in budgets.items():
        lower_is_better = metric in _LOWER_IS_BETTER
        latest = None
        for entry in evidence:
            if metric in entry["measurements"]:
                latest = entry["measurements"][metric]
        if latest is None:
            results[metric] = {"status": UNVERIFIED, "measured": None,
                               "budget": limit}
            continue
        ok = (latest <= limit) if lower_is_better else (latest >= limit)
        results[metric] = {"status": PASS if ok else FAIL,
                           "measured": latest, "budget": limit}
    return results


def compare_before_after(before_evidence: List[Dict[str, Any]],
                         after_evidence: List[Dict[str, Any]],
                         metric: str) -> Dict[str, Any]:
    """MEASURE -> CHANGE -> MEASURE AGAIN -> COMPARE for one metric.
    Requires a real measurement on BOTH sides; never estimates a missing
    one.
    """
    before_vals = [e["measurements"][metric] for e in before_evidence
                  if metric in e["measurements"]]
    after_vals = [e["measurements"][metric] for e in after_evidence
                 if metric in e["measurements"]]
    if not before_vals or not after_vals:
        return {"metric": metric, "status": UNVERIFIED,
                "note": "missing a real measurement on one side; no "
                       "optimization claim can be made"}
    before_v, after_v = before_vals[-1], after_vals[-1]
    delta = after_v - before_v
    lower_is_better = metric in _LOWER_IS_BETTER
    improved = (delta < 0) if lower_is_better else (delta > 0)
    return {"metric": metric, "before": before_v, "after": after_v,
           "delta": delta, "improved": improved,
           "status": PASS if improved else FAIL}
