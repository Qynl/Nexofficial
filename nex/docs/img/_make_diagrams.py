#!/usr/bin/env python3
"""Generate the README diagrams as SVG.

Why a generator instead of hand-drawn images: the diagrams state facts about
the system (chain order, caps, budgets). Keeping them in code means a change
in `providers.py` or `project_state.py` can be mirrored here in one place,
and the output stays sharp at any zoom with real, selectable text.

Run from this directory:  python3 _make_diagrams.py
"""

from __future__ import annotations

import os
from typing import List, Tuple

BG = "#0b0d10"
PANEL = "#141821"
PANEL_2 = "#1b2130"
LINE = "#2b3446"
TEXT = "#e8edf5"
MUTED = "#93a1b8"
NVIDIA = "#76b900"
GPT = "#10a37f"
LOCAL = "#f0a000"
RED = "#e5484d"
BLUE = "#4c8dff"
PURPLE = "#a06bff"

FONT = ("ui-sans-serif, -apple-system, 'Segoe UI', Roboto, "
        "Helvetica, Arial, sans-serif")
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


class Svg:
    def __init__(self, w: int, h: int, title: str):
        self.w, self.h = w, h
        self.parts: List[str] = []
        self.title = title

    # ---------- primitives ----------
    def rect(self, x, y, w, h, fill=PANEL, stroke=LINE, rx=10, sw=1.2,
             dash=None, opacity=1.0):
        d = ' stroke-dasharray="%s"' % dash if dash else ""
        self.parts.append(
            '<rect x="%s" y="%s" width="%s" height="%s" rx="%s" '
            'fill="%s" stroke="%s" stroke-width="%s"%s opacity="%s"/>'
            % (x, y, w, h, rx, fill, stroke, sw, d, opacity))

    def text(self, x, y, s, size=14, fill=TEXT, weight="normal", anchor="start",
             family=None, opacity=1.0, spacing=None):
        fam = family or FONT
        sp = ' letter-spacing="%s"' % spacing if spacing else ""
        self.parts.append(
            '<text x="%s" y="%s" font-family="%s" font-size="%s" '
            'font-weight="%s" fill="%s" text-anchor="%s" opacity="%s"%s>%s</text>'
            % (x, y, fam, size, weight, fill, anchor, opacity, sp, esc(s)))

    def line(self, x1, y1, x2, y2, stroke=LINE, sw=1.4, dash=None,
             marker=False, opacity=1.0):
        d = ' stroke-dasharray="%s"' % dash if dash else ""
        m = ' marker-end="url(#arrow)"' if marker else ""
        self.parts.append(
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" '
            'stroke-width="%s"%s%s opacity="%s"/>'
            % (x1, y1, x2, y2, stroke, sw, d, m, opacity))

    def path(self, d, stroke=LINE, sw=1.6, dash=None, marker=False,
             fill="none", opacity=1.0):
        da = ' stroke-dasharray="%s"' % dash if dash else ""
        m = ' marker-end="url(#arrow)"' if marker else ""
        self.parts.append(
            '<path d="%s" fill="%s" stroke="%s" stroke-width="%s"%s%s '
            'opacity="%s"/>' % (d, fill, stroke, sw, da, m, opacity))

    # ---------- composites ----------
    @staticmethod
    def _wrap(text: str, chars: int) -> List[str]:
        """Hard-wrap a mono line to the box width (no overflow, ever)."""
        out: List[str] = []
        for para in str(text).split("\n"):
            words, cur = para.split(" "), ""
            for word in words:
                cand = (cur + " " + word).strip()
                if len(cand) <= chars or not cur:
                    cur = cand
                else:
                    out.append(cur)
                    cur = word
            out.append(cur)
        return [ln for ln in out if ln != ""] or [""]

    def box(self, x, y, w, h, title, lines=(), accent=BLUE, fill=PANEL,
            tsize=15, lsize=12.5, rx=10, dash=None, tcolor=None,
            wrap=True):
        self.rect(x, y, w, h, fill=fill, stroke=accent, rx=rx, dash=dash)
        self.rect(x, y, 4, h, fill=accent, stroke=accent, rx=2, sw=0)
        self.text(x + 14, y + 24, title, size=tsize, weight="600",
                  fill=tcolor or TEXT)
        # DejaVu Sans Mono advances 0.602 em per glyph: fit the text to the
        # box instead of hoping it fits.
        chars = max(8, int((w - 30) / (lsize * 0.602)))
        yy = y + 45
        for ln in lines:
            for piece in (self._wrap(ln, chars) if wrap else [ln]):
                self.text(x + 14, yy, piece, size=lsize, fill=MUTED,
                          family=MONO)
                yy += 18
        return (x, y, w, h)

    def head(self, title: str, sub: str = "", size: int = 26) -> None:
        """Title + wrapped subtitle (so a long sentence can never run off)."""
        self.text(40, 44, title, size=size, weight="700")
        if not sub:
            return
        chars = max(40, int((self.w - 90) / (13.5 * 0.50)))
        for i, line in enumerate(self._wrap_text(sub, chars)[:2]):
            self.text(40, 70 + i * 20, line, size=13.5, fill=MUTED)

    @staticmethod
    def _wrap_text(text: str, chars: int) -> List[str]:
        out, cur = [], ""
        for word in str(text).split(" "):
            cand = (cur + " " + word).strip()
            if len(cand) <= chars or not cur:
                cur = cand
            else:
                out.append(cur)
                cur = word
        if cur:
            out.append(cur)
        return out or [""]

    def check(self) -> None:
        """Warn about any text that would run past the canvas edge."""
        import re as _re
        for part in self.parts:
            m = _re.search(r'<text x="([\d.]+)" y="[\d.]+"[^>]*font-size="([\d.]+)"'
                           r'[^>]*text-anchor="start"[^>]*>([^<]*)</text>', part)
            if not m:
                continue
            x, size, txt = float(m.group(1)), float(m.group(2)), m.group(3)
            mono = "monospace" in part
            est = len(txt) * size * (0.602 if mono else 0.52)
            if x + est > self.w - 12:
                print("  ! overflow in %s: %r (ends at %.0f of %d)"
                      % (self.title[:28], txt[:60], x + est, self.w))

    def label(self, x, y, s, accent=BLUE, size=12):
        w = max(46, int(len(s) * size * 0.62))
        self.rect(x, y - 14, w, 20, fill=PANEL_2, stroke=accent, rx=5, sw=1)
        self.text(x + w / 2, y, s, size=size, fill=accent, anchor="middle",
                  family=MONO)
        return w

    def arrow(self, x1, y1, x2, y2, color=BLUE, sw=1.8, dash=None):
        self.line(x1, y1, x2, y2, stroke=color, sw=sw, dash=dash,
                  marker=True)

    # ---------- output ----------
    def body(self) -> str:
        head = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" '
                'height="%d" viewBox="0 0 %d %d" role="img" '
                'aria-label="%s">' % (self.w, self.h, self.w, self.h,
                                      esc(self.title)),
                '<title>%s</title>' % esc(self.title),
                '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" '
                'refY="5" markerWidth="7" markerHeight="7" '
                'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" '
                'fill="context-stroke"/></marker></defs>',
                '<rect width="100%%" height="100%%" fill="%s"/>' % BG]
        return "\n".join(head + self.parts + ["</svg>", ""])


def save(name: str, svg: Svg) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, name), "w", encoding="utf-8") as fh:
        fh.write(svg.body())
    print("wrote", name, "%d bytes" % len(svg.body()))


# ===========================================================================
# 1. Hero: the face plus the chip
# ===========================================================================
def face() -> Svg:
    s = Svg(1200, 520, "NEX: two white eyes on black, with the provider chip")
    s.text(48, 52, "NEX", size=34, weight="700")
    s.text(48, 84, "a local assistant that builds games through MCP — and "
                   "shows you which model is doing it", size=15, fill=MUTED)
    s.rect(48, 108, 500, 300, fill="#000000", stroke="#222a38")
    # two eyes
    s.rect(150, 196, 92, 128, fill="#ffffff", stroke="#ffffff", rx=30)
    s.rect(258, 196, 92, 128, fill="#ffffff", stroke="#ffffff", rx=30)
    s.text(298, 384, "the resting face", size=12.5, fill=MUTED,
           anchor="middle", family=MONO)

    # chip stack
    s.text(604, 128, "provider chip (always visible)", size=13,
           fill=MUTED, family=MONO)
    rows: List[Tuple[str, str, str]] = [
        ("NVIDIA NIM · builder", "12 / 40 this minute", NVIDIA),
        ("GPT-5.1 · planner", "used sparingly", GPT),
        ("gpt-oss:20b · local", "always available", LOCAL),
        ("NIM rate-limited", "back in ~38 s - local builds meanwhile", RED),
        ("pacing (headroom)", "saving quota: 31 of 40 used, soft cap 30", BLUE),
        ("key warning", "rejected / bound to another host", RED),
    ]
    y = 152
    for left, right, col in rows:
        s.rect(604, y, 548, 38, fill=PANEL, stroke=LINE, rx=8)
        s.rect(604, y, 5, 38, fill=col, stroke=col, rx=3, sw=0)
        s.text(622, y + 24, left, size=13.5, weight="600", fill=col)
        s.text(1140, y + 24, right, size=12.5, fill=MUTED, anchor="end",
               family=MONO)
        y += 46
    s.text(48, 462, "Everything the agent does to your game goes through MCP "
                    "tools. It never gets a shell, a filesystem or the PC.",
           size=14, fill=MUTED)
    return s


# ===========================================================================
# 2. Architecture
# ===========================================================================
def architecture() -> Svg:
    s = Svg(1240, 900, "NEX architecture: browser, server, agent core, "
                       "providers, MCP boundary")
    s.head("Architecture", "one process, no dependencies beyond the Python "
                            "standard library (plus your local Ollama)")

    s.rect(30, 92, 1180, 150, fill=PANEL_2, stroke=LINE, rx=12)
    s.text(46, 116, "Browser  ·  no secrets, no logic", size=13, fill=BLUE,
           family=MONO)
    s.box(56, 130, 260, 96, "index.html + app.js",
          ["SSE event stream", "toasts, state view"], accent=BLUE)
    s.box(336, 130, 260, 96, "provider_chip.js",
          ["active provider + model", "rate-limit countdown"], accent=BLUE)
    s.box(616, 130, 260, 96, "webgl.js",
          ["the face / animations"], accent=BLUE)
    s.box(896, 130, 288, 96, "settings.html",
          ["keys + models + roles", "/api/settings/providers"], accent=BLUE)

    s.arrow(620, 242, 620, 282, color=LINE)
    s.text(632, 268, "HTTP + SSE (cookie auth, same origin)", size=12,
           fill=MUTED, family=MONO)

    s.rect(30, 282, 1180, 86, fill=PANEL_2, stroke=LINE, rx=12)
    s.text(46, 306, "server.py  ·  one process", size=13, fill=GPT,
           family=MONO)
    s.text(46, 330, "token auth · /api/* · /mcp (dev bridge) · SSE bus · "
                    "static files", size=13, fill=TEXT, family=MONO)
    s.text(46, 352, "no CORS escape hatch · keys never leave the server "
                    "(masked to the page)", size=12.5, fill=MUTED,
           family=MONO)

    s.arrow(620, 368, 620, 404, color=LINE)

    s.rect(30, 404, 1180, 300, fill=PANEL_2, stroke=LINE, rx=12)
    s.text(46, 428, "agent/  ·  the core (model-independent)", size=13,
           fill=PURPLE, family=MONO)
    core = [
        (56, 444, "design.py", ["design document", "concept, loop, pillars"]),
        (290, 444, "director.py", ["recipes -> systems", "scope envelope",
                                   "quality bar per system"]),
        (524, 444, "planner + loop", ["task graph", "policy -> registry",
                                      "-> execute"]),
        (758, 444, "observations.py", ["screenshots, logs,", "state as",
                                       "EVIDENCE"]),
        (992, 444, "critic.py", ["reviewer + tester", "rules first, then",
                                 "the model"]),
    ]
    for x, y, title, lines in core:
        s.box(x, y, 218, 122, title, lines, accent=PURPLE, tsize=13.5,
              lsize=11)
    s.box(56, 578, 420, 106, "project_state.py  ·  bounded memory",
          ["systems, criteria, quality bars, bugs,", "decisions; every map "
           "capped, oldest first"], accent=PURPLE, tsize=13.5, lsize=11.5)
    s.box(496, 578, 360, 106, "checkpoints.py",
          ["resume is a CLAIM: the world is", "re-checked before trusting "
           "it"], accent=PURPLE, tsize=13.5, lsize=11.5)
    s.box(876, 578, 334, 106, "verification.py",
          ["completion gate:", "evidence or nothing"], accent=PURPLE,
          tsize=13.5, lsize=11.5)

    s.arrow(620, 704, 620, 740, color=LINE)

    s.rect(30, 740, 1180, 130, fill=PANEL_2, stroke=LINE, rx=12)
    s.text(46, 764, "agent/providers.py  ·  who is allowed to think", size=13,
           fill=LOCAL, family=MONO)
    s.box(56, 776, 272, 78, "GPT (planner)",
          ["plans, tests, design", "called rarely"], accent=GPT, tsize=13.5,
          lsize=11.5)
    s.box(348, 776, 272, 78, "NVIDIA NIM (builder)",
          ["does the building", "~40 RPM, paced"], accent=NVIDIA, tsize=13.5,
          lsize=11.5)
    s.box(640, 776, 272, 78, "Ollama (local)",
          ["same plan, takes over", "terminal fallback"], accent=LOCAL,
          tsize=13.5, lsize=11.5)
    s.box(932, 776, 278, 78, "mcp/  ·  the action surface",
          ["policy -> capability -> registry", "no shell. no filesystem."],
          accent=RED, tsize=13.5, lsize=11.5)

    s.text(620, 160 + 76, "", size=1)
    return s


# ===========================================================================
# 3. Failover timeline
# ===========================================================================
def failover() -> Svg:
    s = Svg(1240, 668, "Failover: NVIDIA NIM hits the rate limit, Ollama "
                       "takes over the same plan, NIM resumes")
    s.head("Failover — the plan never restarts",
           "A rate limit is not a failure: it is a provider that is busy. "
           "The SAME plan continues on the next provider, and NVIDIA is "
           "tried again the moment its window has room.")

    lanes = [("NVIDIA NIM", NVIDIA, 150), ("Ollama (local)", LOCAL, 268),
             ("UI chip", BLUE, 386), ("Project plan", PURPLE, 504)]
    for name, col, y in lanes:
        s.text(40, y + 22, name, size=14, weight="600", fill=col)
        s.line(210, y + 16, 1200, y + 16, stroke=LINE, sw=1.4)

    # events (sub lines wrap inside their box: an event note never bleeds
    # into the next lane)
    def ev(x, y, w, col, title, sub, h: int = 62):
        s.rect(x, y - 44, w, h, fill=PANEL, stroke=col, rx=9)
        s.text(x + 12, y - 22, title, size=13, weight="600", fill=col)
        chars = max(10, int((w - 24) / (11.5 * 0.602)))
        for i, line in enumerate(s._wrap(str(sub), chars)[:3]):
            s.text(x + 12, y - 4 + i * 15, line, size=11.5, fill=MUTED,
                   family=MONO)

    ev(220, 166, 250, NVIDIA, "build batch", "39 -> 40 calls this minute")
    ev(496, 166, 260, NVIDIA, "HTTP 429", "Retry-After: 42 s")
    ev(782, 166, 250, NVIDIA, "cooldown until", "the RPM window has room")
    ev(1058, 166, 142, NVIDIA, "resumes", "provider.recovered")

    ev(496, 284, 260, LOCAL, "takes over", "same messages, same plan")
    ev(782, 284, 250, LOCAL, "keeps building", "plan_continues: true")

    ev(496, 402, 536, BLUE,
       "chip: NIM is rate-limited - tried again in ~42 s;",
       "gpt-oss:20b builds the same plan meanwhile", h=62)
    ev(1058, 402, 142, BLUE, "back", "info toast")

    ev(220, 520, 980, PURPLE, "one plan, one goal, no re-planning",
       "the fallback CONTINUES the task graph — it never starts over")

    s.arrow(470, 166, 496, 166, color=NVIDIA)
    s.arrow(756, 166, 782, 166, color=NVIDIA)
    s.arrow(1032, 166, 1058, 166, color=NVIDIA)
    s.arrow(626, 204, 626, 240, color=LOCAL, dash="5 4")
    s.arrow(1032, 284, 1058, 284, color=LOCAL)
    s.path("M 220 548 L 120 548 L 120 130 L 220 130", stroke=PURPLE,
           dash="5 4", marker=True)
    s.text(132, 96, "the graph keeps its position: the next batch is the next "
                    "unfinished task, whoever runs it", size=12, fill=MUTED,
           family=MONO)
    s.text(220, 596, "Detail: a 429 waits at least until the sliding "
                     "60-second window has a free slot (bounded by 120 s), "
                     "then retries automatically.", size=12.5,
           fill=MUTED, family=MONO)
    s.text(220, 618, "The streaming chat obeys the same chain, the same "
                     "budget and the same events as the build.",
           size=12.5, fill=MUTED, family=MONO)
    return s


# ===========================================================================
# 4. RPM budget / pacing
# ===========================================================================
def budget() -> Svg:
    s = Svg(1200, 620, "The 40 RPM budget: reserve, soft cap, chill, "
                       "rate-limited")
    s.head("The 40 RPM budget — calmly not using everything",
           "Free NVIDIA NIM keys allow ~40 requests/minute with no usage API. "
           "Nex counts them itself and leaves headroom, so the next minute "
           "always has budget.")

    # window bar
    x0, y0, w, h = 60, 130, 1080, 86
    s.rect(x0, y0, w, h, fill=PANEL, stroke=LINE, rx=10)
    used = 30
    for i in range(40):
        cx = x0 + 14 + i * ((w - 28) / 40)
        cw = (w - 28) / 40 - 6
        if i < used:
            fill = NVIDIA if i < 22 else LOCAL
        else:
            fill = "#232c3c"
        s.rect(cx, y0 + 20, cw, 46, fill=fill, stroke=fill, rx=4, sw=0)
    s.text(x0 + 14, y0 + 16, "requests in the current 60-second window",
           size=12, fill=MUTED, family=MONO)
    s.text(x0 + w - 14, y0 + 16, "40 = the published free-tier budget",
           size=12, fill=MUTED, anchor="end", family=MONO)
    s.text(x0 + 14, y0 + 82, "1-22 normal build traffic", size=11.5,
           fill=NVIDIA, family=MONO)
    s.text(x0 + 480, y0 + 82, "23-30 deliberate pause zone", size=11.5,
           fill=LOCAL, family=MONO)
    s.text(x0 + w - 14, y0 + 82, "31-40 reserve", size=11.5, fill=MUTED,
           anchor="end", family=MONO)

    s.text(60, 262, "reserve 25 %   soft cap = round(40 x 0.75) = 30",
           size=13, fill=LOCAL, family=MONO)
    s.text(60, 284, "min_interval_s = 1 s   max_chill_s = 8 s   "
                    "NEX_NIM_RPM overrides the budget",
           size=13, fill=MUTED, family=MONO)

    rows = [
        ("below the soft cap", "the call goes out normally", NVIDIA),
        ("inside the reserve, a fallback exists",
         "the request routes around it (paced_skips++) and the fallback "
         "serves", BLUE),
        ("inside the reserve, no fallback",
         "bounded chill (<= max_chill_s), then the call goes out", LOCAL),
        ("window completely full",
         "chill if a slot is near, else mark rate_limited and fail over",
         RED),
    ]
    y = 320
    for left, right, col in rows:
        s.rect(60, y, 1080, 52, fill=PANEL, stroke=LINE, rx=9)
        s.rect(60, y, 5, 52, fill=col, stroke=col, rx=3, sw=0)
        s.text(80, y + 32, left, size=13.5, weight="600", fill=col)
        s.text(520, y + 32, right, size=13, fill=TEXT, family=MONO)
        y += 62

    s.text(60, 588, "Every decision is visible: the chip shows the countdown "
                    "and the headroom, the log shows provider.paced / "
                    "provider.trouble / provider.recovered.",
           size=12.5, fill=MUTED, family=MONO)
    return s


# ===========================================================================
# 5. Quality path
# ===========================================================================
def quality() -> Svg:
    s = Svg(1240, 780, "The quality path: recipe library, blueprint, build, "
                       "playtest, judge, repair")
    s.head("How a game gets good (with a 20-billion-parameter brain)",
           "The model supplies the wording and the code. The ARCHITECTURE "
           "supplies the standard: proven structures, a stated quality bar, "
           "a real playtest, and a judge that must name what it saw.")

    s.box(40, 108, 300, 170, "1 · recipe library",
          ["15 systems with proven steps,", "checklists, known risks and",
           "a three-line QUALITY BAR", "(what 'good' means here)"],
          accent=PURPLE, tsize=14, lsize=11.5)
    s.box(380, 108, 300, 170, "2 · scope envelope",
          ["ONE system per round;", "objective, steps, criteria,",
           "quality bar, do-not list", "(anti-scope-creep)"],
          accent=PURPLE, tsize=14, lsize=11.5)
    s.box(720, 108, 300, 170, "3 · blueprint",
          ["deterministic document:", "steps mapped to REAL tools",
           "(play_solo, take_screenshot),", "quality checks as its own kind"],
          accent=BLUE, tsize=14, lsize=11.5)
    s.box(40, 308, 300, 170, "4 · builder (batches)",
          ["3 tool calls per model call,", "MCP tools only,",
           "halved automatically when", "the provider struggles"],
          accent=NVIDIA, tsize=14, lsize=11.5)
    s.box(380, 308, 300, 170, "5 · playtest",
          ["play_solo / pie_start runs", "the actual game. Studio's edit",
           "mode proves nothing — a", "tool call returning is not a result"],
          accent=LOCAL, tsize=14, lsize=11.5)
    s.box(720, 308, 300, 170, "6 · observe",
          ["screenshot + console/log +", "runtime state become",
           "EVIDENCE (untrusted data,", "never instructions)"],
          accent=BLUE, tsize=14, lsize=11.5)
    s.box(40, 508, 300, 170, "7 · reviewer",
          ["per criterion: proven only", "with evidence. A clean tool call",
           "is 'evidence', not 'pass'"], accent=GPT, tsize=14, lsize=11.5)
    s.box(380, 508, 300, 170, "8 · tester (adversarial)",
          ["attacks the system AND the", "quality bar, with the engine's",
           "classic defects supplied", "(character sinks, pawn won't move)"],
          accent=RED, tsize=14, lsize=11.5)
    s.box(720, 508, 300, 170, "9 · repair loop",
          ["findings -> project memory", "-> next plan fixes them.",
           "Open defects block a system", "from being called complete"],
          accent=PURPLE, tsize=14, lsize=11.5)

    # row 1: left to right
    s.arrow(340, 193, 380, 193, color=PURPLE)
    s.arrow(680, 193, 720, 193, color=PURPLE)
    # row 1 -> row 2: the snake goes back to the left column
    s.path("M 870 282 L 870 296 L 190 296 L 190 306", stroke=BLUE,
           dash="6 4", marker=True)
    s.text(430, 288, "the plan is written -> now build it", size=12,
           fill=BLUE, family=MONO)
    # row 2: left to right
    s.arrow(340, 393, 380, 393, color=NVIDIA)
    s.arrow(680, 393, 720, 393, color=LOCAL)
    # row 2 -> row 3: snake back to the left again
    s.path("M 870 482 L 870 496 L 190 496 L 190 506", stroke=GPT,
           dash="6 4", marker=True)
    s.text(430, 488, "look at it -> judge it", size=12, fill=GPT,
           family=MONO)
    # row 3: left to right
    s.arrow(340, 593, 380, 593, color=GPT)
    s.arrow(680, 593, 720, 593, color=RED)
    # the learning loop goes back to the scope envelope, outside the boxes
    s.path("M 870 682 L 870 736 L 1180 736 L 1180 86 L 530 86 L 530 106",
           stroke=PURPLE, dash="6 4", marker=True)
    s.text(560, 754, "what was learned goes back in: the goal stays, the "
                     "defects become tasks", size=12.5,
           fill=MUTED, family=MONO)

    s.text(1040, 150, "the bar is", size=13, fill=MUTED, family=MONO)
    s.text(1040, 172, "stated, not", size=13, fill=MUTED, family=MONO)
    s.text(1040, 194, "hoped for", size=13, fill=MUTED, family=MONO)
    s.rect(1032, 208, 168, 130, fill=PANEL, stroke=PURPLE, rx=9)
    s.text(1046, 232, "quality bar", size=12.5, fill=PURPLE, family=MONO)
    for i, ln in enumerate(["input responds in", "the same frame",
                            "the camera never", "clips or snaps"]):
        s.text(1046, 254 + i * 18, ln, size=11.5, fill=MUTED, family=MONO)
    s.text(1040, 366, "checked against", size=12, fill=MUTED, family=MONO)
    s.text(1040, 384, "the RUNNING game", size=12, fill=MUTED, family=MONO)
    return s


# ===========================================================================
# 6. MCP boundary
# ===========================================================================
def boundary() -> Svg:
    s = Svg(1240, 700, "The MCP-only boundary: policy, capability, registry")
    s.head("The action surface is MCP — and nothing else",
           "The agent can only act through tools that connected MCP servers "
           "expose. It has no shell, no filesystem, no arbitrary PC access.")

    s.box(40, 110, 250, 128, "model proposes",
          ["\"call roblox.play_solo\"", "plain tool name + args"],
          accent=PURPLE, tsize=14, lsize=11.5)
    s.box(330, 96, 300, 156, "policy + capability",
          ["classify: read · create ·", "modify · destructive · test ·",
           "code_exec · unknown", "unknown -> confirmation"],
          accent=RED, tsize=14, lsize=11.5)
    s.box(670, 96, 250, 156, "live registry",
          ["only tools that exist", "RIGHT NOW on a connected",
           "server; a name alone", "is never enough"],
          accent=BLUE, tsize=14, lsize=11.5)
    s.box(960, 96, 240, 156, "MCP server",
          ["Roblox Studio MCP,", "Unreal MCP, your own —",
           "the editor keeps the", "last word"],
          accent=NVIDIA, tsize=14, lsize=11.5)

    s.arrow(290, 174, 330, 174, color=PURPLE)
    s.arrow(630, 174, 670, 174, color=RED)
    s.arrow(920, 174, 960, 174, color=BLUE)

    s.text(330, 292, "refused before any server sees it", size=13,
           fill=RED, family=MONO)
    refusals = [
        "paths pointing at secrets: ~/.ssh, .env, credentials",
        "cloud metadata IPs, localhost, private ranges (SSRF)",
        "a key sent to a host it does not belong to",
        "reasoning/'thinking' smuggled back as an argument",
        "code execution on a server the operator did not allow",
    ]
    y = 312
    for r in refusals:
        s.rect(330, y, 870, 40, fill=PANEL, stroke=LINE, rx=8)
        s.text(348, y + 26, "x  " + r, size=12.5, fill=TEXT, family=MONO)
        y += 48

    s.rect(40, 312, 250, 232, fill=PANEL, stroke=LINE, rx=9)
    s.text(58, 338, "what the agent CAN do", size=13, fill=NVIDIA,
           family=MONO)
    for i, ln in enumerate(["create parts / actors", "write scripts",
                            "set properties", "run the game",
                            "take screenshots", "read the console",
                            "verify what it sees"]):
        s.text(58, 362 + i * 24, "+  " + ln, size=12.5, fill=MUTED,
               family=MONO)

    s.text(40, 606, "Two independent layers, on purpose: the policy decides "
                    "WHAT is allowed, the registry decides WHAT EXISTS.",
           size=13, fill=MUTED, family=MONO)
    s.text(40, 630, "Operators can relax a specific gate for a specific "
                    "server (NEX_ALLOW_CODE_EXECUTION) — the model cannot.",
           size=13, fill=MUTED, family=MONO)
    s.text(40, 654, "Even a fully compromised model can only use tools you "
                    "connected and the editor accepted.",
           size=13, fill=MUTED, family=MONO)
    return s


# ===========================================================================
# 7. Memory
# ===========================================================================
def memory() -> Svg:
    s = Svg(1200, 660, "Bounded memory: what is kept, how it is capped")
    s.head("Memory that cannot grow forever",
           "A local model has a small context. The project state is the "
           "memory that survives runs, and every part of it is capped — the "
           "oldest entries go first.")

    cols = [
        ("goals + systems", ["the game plan, status", "per system"], 24),
        ("criteria", ["pass/fail per system", "(max 12 per system)"], 12),
        ("quality bars", ["the standard per system", "(max 6 per system)"], 6),
        ("decisions ledger", ["locked design decisions", "conflicts are "
                              "flagged"], 30),
        ("known bugs", ["open defects with the", "observation that found "
                        "them"], 50),
        ("observations", ["screenshots, logs, state", "as evidence, "
                          "collapsed"], 30),
        ("completed / failed", ["what worked, what did", "not; the exact "
                                "count is kept"], 60),
        ("knowledge", ["facts learned about", "the project"], 40),
    ]
    x, y = 40, 108
    for i, (title, lines, cap) in enumerate(cols):
        s.box(x, y, 265, 116, title, lines, accent=PURPLE, tsize=13.5,
              lsize=11.5)
        s.text(x + 251, y + 24, "cap %d" % cap, size=11, fill=LOCAL,
               anchor="end", family=MONO)
        x += 285
        if i % 4 == 3:
            x = 40
            y += 136

    s.rect(40, 396, 1120, 92, fill=PANEL, stroke=LOCAL, rx=10)
    s.text(58, 424, "compact_memory() runs once per cycle", size=14,
           weight="600", fill=LOCAL)
    s.text(58, 448, "caps are enforced idempotently; the return value says "
                    "exactly what was dropped, so the loop can tell the user "
                    "instead of silently forgetting.", size=12.5, fill=MUTED,
           family=MONO)
    s.text(58, 470, "resume-after-restart re-checks reality (servers up? "
                    "tools still there?) before trusting a checkpoint.",
           size=12.5, fill=MUTED, family=MONO)

    s.rect(40, 508, 545, 120, fill=PANEL, stroke=BLUE, rx=10)
    s.text(58, 536, "what is NOT remembered", size=14, weight="600",
           fill=BLUE)
    for i, ln in enumerate(["the full model conversation",
                            "raw tool output beyond the evidence window",
                            "anything the model only claimed"]):
        s.text(58, 560 + i * 22, "- " + ln, size=12.5, fill=MUTED,
               family=MONO)

    s.rect(615, 508, 545, 120, fill=PANEL, stroke=NVIDIA, rx=10)
    s.text(633, 536, "why this matters with a small local model", size=14,
           weight="600", fill=NVIDIA)
    for i, ln in enumerate(["the plan, the standard and the open defects "
                            "survive",
                            "a restart — the model only has to be good",
                            "enough for one batch at a time"]):
        s.text(633, 560 + i * 22, "- " + ln, size=12.5, fill=MUTED,
               family=MONO)
    return s


def main() -> None:
    save("face.svg", face())
    save("architecture.svg", architecture())
    save("failover.svg", failover())
    save("rpm-budget.svg", budget())
    save("quality-path.svg", quality())
    save("mcp-boundary.svg", boundary())
    save("memory.svg", memory())


if __name__ == "__main__":
    main()
