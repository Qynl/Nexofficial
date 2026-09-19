"""Tests for the NEX 2.0 Critic (agent/critic.py) + the
build -> critique -> improve loop in the executor.

"Did my tool call return success?" is verification. "Is this actually
good?" is the critic. These tests pin the deterministic detectors
(structural, never a game recipe), the verdict/action logic, the honest
LLM-failure path, and the bounded improve cycle inside AutonomousAgent.
"""
import importlib
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

critic = importlib.import_module("agent.critic")
design_mod = importlib.import_module("agent.design")
project_state = importlib.import_module("agent.project_state")
agent_loop = importlib.import_module("agent.loop")
mock_mcp = importlib.import_module("agent.mock_mcp")
plans = importlib.import_module("agent.plans")
task_graph = importlib.import_module("agent.task_graph")


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------------------
# 1. repetition detector — structural pattern mining, no game recipe
# ---------------------------------------------------------------------------

stream = []
for round_i in range(4):
    stream += ["spawn corridor segment", "play noise cue",
               "spawn entity", "play escape sting"]
stream += ["final polish pass"]
found = critic.find_repetition(stream, window=3, threshold=3)
_expect(bool(found) and found[0]["kind"] == "repetition"
        and found[0]["severity"] == "major",
        "repeated 3-task cycle detected (major)")

unique = ["build dock", "wire lights", "add fog volume",
          "tune reverb", "paint trim", "place props",
          "light hallway", "mirror room", "atrium glass",
          "service stairs", "pump room", "flood doors",
          "loader bay", "cold storage"]
_expect(critic.find_repetition(unique) == [],
        "varied work is not flagged as repetition")
_expect(critic.find_repetition([]) == [], "empty stream -> no findings")


# ---------------------------------------------------------------------------
# 2. placeholder / unverified / stall / open gates
# ---------------------------------------------------------------------------

ph = critic.find_placeholder(["material_generic_2 applied",
                              "build dock"])
_expect(bool(ph) and ph[0]["kind"] == "placeholder"
        and ph[0]["severity"] == "minor",
        "placeholder/generic content flagged (minor)")
_expect(critic.find_placeholder(["weathered brass fittings"]) == [],
        "real content passes")

uv = critic.find_unverified([{"name": "build dock", "verified": True},
                             {"name": "add fog", "verified": False}])
_expect(bool(uv) and "1 completed task" in uv[0]["message"],
        "unverified completions surfaced")

stall = critic.find_stall([{"task": "bake nav"},
                           {"task": "bake nav"},
                           {"task": "bake nav"}])
_expect(bool(stall) and stall[0]["kind"] == "stall",
        "same task failing 3x -> stall finding")
_expect(critic.find_stall([{"task": "bake nav"},
                           {"task": "bake nav"}]) == [],
        "two failures -> not yet a stall")

gates = critic.find_open_gates(
    {"quality_gates": [{"name": "atmosphere works", "done": False},
                       {"name": "no broken interactions", "done": True}]})
_expect(bool(gates) and gates[0]["evidence"]["gates"]
        == ["atmosphere works"],
        "open quality gates reported (done gates excluded)")
_expect(critic.find_open_gates({}) == [], "no design -> no gate findings")


# ---------------------------------------------------------------------------
# 3. locked-decision conflicts surface in the critic too
# ---------------------------------------------------------------------------

lk = critic.find_locked_conflicts(
    ["add health bar", "build dock lighting"],
    ["No conventional health bar."])
_expect(bool(lk) and lk[0]["kind"] == "locked_conflict"
        and lk[0]["severity"] == "major",
        "critic flags work that re-litigates a LOCKED decision")


# ---------------------------------------------------------------------------
# 4. critique(): verdict + action logic (deterministic layer)
# ---------------------------------------------------------------------------

def _state(goal="g", design=None):
    st = project_state.ProjectState(goal=goal)
    st.design = design or {}
    return st


class _Report:
    def __init__(self):
        self.status = "completed"
        self.completed = []
        self.failed = []
        self.skipped = []
        self.reasons = []
        self.missing = []


st = _state()
rep = _Report()
c = critic.critique("g", st, rep, llm=None, cycle=1)
_expect(c.verdict == "PASS" and c.action == "COMPLETE"
        and c.llm_used is False,
        "clean run: PASS / COMPLETE without an LLM")

st.completed = ["spawn corridor segment", "play noise cue",
                "spawn entity", "play escape sting"] * 3 + ["final polish"]
c = critic.critique("g", st, rep, llm=None)
_expect(c.verdict == "WEAK" and c.action == "REPLAN",
        "repetition -> WEAK / REPLAN (structural)")

st2 = _state()
st2.completed = ["apply material_generic_1", "build dock"]
c = critic.critique("g", st2, rep, llm=None)
_expect(c.verdict == "WEAK" and c.action == "POLISH",
        "placeholder only -> WEAK / POLISH (residue)")

st3 = _state(design={"quality_gates": [{"name": "loop works",
                                        "done": False}]})
c = critic.critique("g", st3, rep, llm=None)
_expect(c.action == "POLISH"
        and any(f["kind"] == "gates_open" for f in c.findings),
        "open gates -> POLISH")

st4 = _state()
st4.lock_decision("No conventional health bar.", "tension design")
st4.completed = ["add health bar"]
c = critic.critique("g", st4, rep, llm=None)
_expect(c.action == "REPLAN"
        and any(f["kind"] == "locked_conflict" for f in c.findings),
        "locked-decision conflict -> REPLAN (design stability)")


# ---------------------------------------------------------------------------
# 5. LLM critic layer: used when it works, honest when it fails
# ---------------------------------------------------------------------------

def llm_weak(messages):
    return json.dumps({
        "technical": 7, "design": 4, "quality": 5,
        "findings": [{"kind": "design",
                      "message": "the second area contradicts the "
                                 "isolation pillar",
                      "severity": "major"}],
        "verdict": "WEAK",
        "note": "the core loop works but the pacing collapses in act two"})


c = critic.critique("g", _state(), _Report(), llm=llm_weak)
_expect(c.llm_used is True and c.verdict == "WEAK"
        and c.action == "REPLAN"
        and c.scores.get("design") == 4
        and any(f.get("evidence", {}).get("source") == "llm"
                for f in c.findings),
        "LLM critique merges in (scores, findings, verdict)")


def llm_pass(messages):
    return json.dumps({"technical": 9, "design": 8, "quality": 9,
                       "findings": [], "verdict": "PASS",
                       "note": "solid"})


st_clean = _state()
st_clean.completed = ["build dock", "wire lights"]
c = critic.critique("g", st_clean, _Report(), llm=llm_pass)
_expect(c.verdict == "PASS" and c.action == "COMPLETE",
        "LLM PASS with no major findings -> COMPLETE")


def llm_broken(messages):
    raise RuntimeError("model down")


c = critic.critique("g", _state(), _Report(), llm=llm_broken)
_expect(c.llm_used is False and c.verdict == "PASS"
        and c.action == "COMPLETE",
        "LLM failure is tolerated (deterministic findings stand)")

c = critic.critique("g", _state(), _Report(),
                    llm=lambda m: "not json at all")
_expect(c.llm_used is False,
        "unparseable LLM reply -> deterministic-only critique")


# ---------------------------------------------------------------------------
# 6. Full loop integration: design -> plan -> build -> critique (mocked)
# ---------------------------------------------------------------------------

DESIGN_JSON = {
    "concept": "a small mechanism puzzle box",
    "genre": "puzzle",
    "gameplay_loop": "observe -> configure -> test -> advance",
    "design_pillars": ["clarity", "tactility"],
    "milestones": [{"name": "Foundation", "tasks": [
        "create_asset the frame", "import_asset gears"]},
        {"name": "Loop", "tasks": ["create_script logic",
                                   "build the box",
                                   "verify_game runs"]}],
    "quality_gates": [{"name": "puzzle solvable", "done": False}],
}

PLAN_JSON = {
    "title": "mechanism puzzle box",
    "rationale": "foundation then loop",
    "steps": [
        {"name": "create_asset frame", "tool": "create_asset",
         "args": {}, "why": "frame", "expect": "asset exists"},
        {"name": "import_asset gears", "tool": "import_asset",
         "args": {}, "why": "gears", "expect": "imported",
         "depends": ["create_asset frame"]},
        {"name": "build the box", "tool": "build", "args": {},
         "why": "assemble", "expect": "build ok",
         "depends": ["import_asset gears"]},
    ],
}

CALLS = {"design": 0, "plan": 0, "critique": 0}


def llm_pipeline(messages):
    prompt = messages[-1]["content"]
    if "quality critic" in prompt.lower():
        CALLS["critique"] += 1
        return json.dumps({"technical": 9, "design": 8, "quality": 9,
                           "findings": [], "verdict": "PASS",
                           "note": "coherent"})
    if "design lead" in prompt.lower():
        CALLS["design"] += 1
        return "```json\n" + json.dumps(DESIGN_JSON) + "\n```"
    CALLS["plan"] += 1
    return "```json\n" + json.dumps({"plan": PLAN_JSON}) + "\n```"


def _llm_pipeline_old(messages):
    prompt = messages[-1]["content"]
    if "design document" in prompt.lower():
        CALLS["design"] += 1
        return "```json\n" + json.dumps(DESIGN_JSON) + "\n```"
    if "quality critic" in prompt.lower():
        CALLS["critique"] += 1
        return json.dumps({"technical": 9, "design": 8, "quality": 9,
                           "findings": [], "verdict": "PASS",
                           "note": "coherent"})
    CALLS["plan"] += 1
    return "```json\n" + json.dumps({"plan": PLAN_JSON}) + "\n```"


asset_tools = [
    {"name": "create_asset", "description": "create an asset",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "import_asset", "description": "import an asset",
     "inputSchema": {"type": "object", "properties": {}}},
]
build_tools = [
    {"name": "build", "description": "build",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "verify_game", "description": "verify",
     "inputSchema": {"type": "object", "properties": {}}},
]
servers = [
    mock_mcp.server_view("asset_mcp", mock_mcp.MockMCPServer(
        "asset_mcp", asset_tools)),
    mock_mcp.server_view("game_mcp", mock_mcp.MockMCPServer(
        "game_mcp", build_tools)),
]
registry = agent_loop.CapabilityRegistry(servers)

with tempfile.TemporaryDirectory() as td:
    os.environ["NEX_PROJECTS_DIR"] = td
    # This test is about the ORDER (design -> plan -> build -> critique),
    # not about the campaign: pin the campaign to one system so the
    # assertions count a single round. The multi-system campaign has its
    # own section in test_director.py.
    os.environ["NEX_MAX_SYSTEMS_PER_RUN"] = "1"
    events = []
    agent = agent_loop.AutonomousAgent(
        registry, llm=llm_pipeline, bus=events.append,
        max_critique_cycles=1, persist=True)
    report = agent.run("mechanism puzzle box")
    os.environ.pop("NEX_MAX_SYSTEMS_PER_RUN", None)

    _expect(CALLS["design"] == 1,
            "design stage ran exactly once before planning")
    _expect(CALLS["plan"] >= 1, "planner produced the task plan")
    _expect(CALLS["critique"] == 1, "critic ran after the build")
    _expect(report.status == "COMPLETED",
            "mocked project completes")
    _expect(agent.last_critique is not None
            and agent.last_critique.verdict == "PASS"
            and agent.last_critique.cycle == 1,
            "critique verdict recorded on the agent")

    kinds = [e.get("type") for e in events]
    _expect("agent.design_started" in kinds
            and "agent.design_ready" in kinds,
            "design events emitted (started + ready)")
    _expect("agent.critique_started" in kinds
            and "agent.critique" in kinds,
            "critique events emitted")
    crit_ev = [e for e in events if e.get("type") == "agent.critique"][0]
    _expect(crit_ev.get("verdict") == "PASS"
            and crit_ev.get("action") == "COMPLETE",
            "critique event carries verdict + action")

    from agent.projects_store import list_projects, load_project
    projs = list_projects()
    _expect(len(projs) == 1 and projs[0]["phase"] == "COMPLETE",
            "project persisted with phase COMPLETE")
    doc = load_project(projs[0]["id"])
    _expect(doc and doc["state"].get("design", {}).get("concept")
            == "a small mechanism puzzle box",
            "design document persisted with the project")
    _expect(doc["state"].get("cycles"), "critique cycle persisted")
    os.environ.pop("NEX_PROJECTS_DIR", None)

# --- critique-driven improvement (mocked critic says WEAK once) -----------
STATES = {"first": True}


def llm_improve(messages):
    prompt = messages[-1]["content"]
    if "design lead" in prompt.lower():
        return "```json\n" + json.dumps(DESIGN_JSON) + "\n```"
    if "quality critic" in prompt.lower():
        if STATES["first"]:
            STATES["first"] = False
            return json.dumps({
                "technical": 6, "design": 4, "quality": 5,
                "findings": [{"kind": "quality",
                              "message": "frame reads as generic",
                              "severity": "minor"}],
                "verdict": "WEAK", "note": "needs a polish pass"})
        return json.dumps({"technical": 9, "design": 8, "quality": 9,
                           "findings": [], "verdict": "PASS",
                           "note": "good now"})
    return "```json\n" + json.dumps({"plan": PLAN_JSON}) + "\n```"


servers2 = [
    mock_mcp.server_view("asset_mcp", mock_mcp.MockMCPServer(
        "asset_mcp", asset_tools)),
    mock_mcp.server_view("game_mcp", mock_mcp.MockMCPServer(
        "game_mcp", build_tools)),
]
registry2 = agent_loop.CapabilityRegistry(servers2)
events2 = []
# One system: this test counts critique CYCLES (WEAK -> improve -> PASS),
# and a campaign would legitimately add cycles for the next system.
os.environ["NEX_MAX_SYSTEMS_PER_RUN"] = "1"
agent2 = agent_loop.AutonomousAgent(
    registry2, llm=llm_improve, bus=events2.append,
    max_critique_cycles=2, persist=False)
report2 = agent2.run("mechanism puzzle box")
os.environ.pop("NEX_MAX_SYSTEMS_PER_RUN", None)
kinds2 = [e.get("type") for e in events2]
_expect("agent.improve_started" in kinds2,
        "WEAK critique triggers a bounded improvement run")
_expect(agent2.last_critique is not None
        and agent2.last_critique.cycle == 2
        and agent2.last_critique.verdict == "PASS",
        "second critique cycle passes (build -> critique -> improve)")
_expect(report2.status == "COMPLETED",
        "improved run completes")
# Bounded: cycle 2 is the last even if the critic kept saying WEAK.
STATES["first"] = True
STATES["count"] = 0


def llm_always_weak(messages):
    prompt = messages[-1]["content"]
    if "design lead" in prompt.lower():
        return "```json\n" + json.dumps(DESIGN_JSON) + "\n```"
    if "quality critic" in prompt.lower():
        return json.dumps({"technical": 3, "design": 3, "quality": 3,
                           "findings": [{"kind": "quality",
                                         "message": "still weak",
                                         "severity": "minor"}],
                           "verdict": "WEAK", "note": "never good"})
    return "```json\n" + json.dumps({"plan": PLAN_JSON}) + "\n```"


servers3 = [
    mock_mcp.server_view("asset_mcp", mock_mcp.MockMCPServer(
        "asset_mcp", asset_tools)),
    mock_mcp.server_view("game_mcp", mock_mcp.MockMCPServer(
        "game_mcp", build_tools)),
]
registry3 = agent_loop.CapabilityRegistry(servers3)
agent3 = agent_loop.AutonomousAgent(
    registry3, llm=llm_always_weak, bus=None,
    max_critique_cycles=2, persist=False)
report3 = agent3.run("mechanism puzzle box")
_expect(agent3.last_critique.cycle == 2,
        "critique loops are BOUNDED (stops at max_critique_cycles)")


# ---------------------------------------------------------------------------
# 7b. EXACT-PLAN APPROVAL: design persists Plan A; build executes Plan A
# exactly (no silent re-planning); REPLAN persists Plan B deliberately.
# ---------------------------------------------------------------------------

from agent.server_run import run_agent_goal  # noqa: E402
from agent.projects_store import (load_approved,   # noqa: E402
                                  load_approved_graph,
                                  load_project_state)

PLAN_B_JSON = {
    "title": "mechanism puzzle box — plan B",
    "rationale": "polish pass",
    "steps": [
        {"name": "polish frame", "tool": "create_asset",
         "args": {}, "why": "finish", "expect": "polished"},
    ],
}

plan_calls = {"build": 0, "pre_critique": 0}
STATES_B = {"critiques": 0}


def llm_design_critique_only(messages):
    prompt = messages[-1]["content"]
    if "PROVEN RECIPES" in prompt or "ROLE:" in prompt:
        # The GAME DIRECTOR ("PROVEN RECIPES") and the specialized ROLES
        # (reviewer/tester/debugger) are not planners. They never produce a
        # task plan, so they must not count as a plan call. Decline with a
        # valid empty answer so each role keeps its deterministic fallback.
        return "{}"
    if "quality critic" in prompt.lower():
        STATES_B["critiques"] += 1
        STATES_B["critique_seen"] = True
        if STATES_B["critiques"] == 1:
            return json.dumps({"technical": 6, "design": 5, "quality": 5,
                               "findings": [{"kind": "quality",
                                             "message": "rough edges",
                                             "severity": "minor"}],
                               "verdict": "WEAK", "note": "polish"})
        return json.dumps({"technical": 9, "design": 8, "quality": 9,
                           "findings": [], "verdict": "PASS",
                           "note": "good"})
    if "design lead" in prompt.lower():
        return "```json\n" + json.dumps(DESIGN_JSON) + "\n```"
    if "review it now" in prompt.lower():
        # The post-build quality judge (pre-existing behavior) — not the
        # planner. Give it a passing review in the judge's own schema.
        return json.dumps({"fun": 9, "quality": 9, "playability": 9,
                           "suggestions": []})
    # The PLANNER must never be called BEFORE the first critique (that
    # would mean the build is not executing the approved plan). A planner
    # call AFTER a WEAK critique is the deliberate Plan B. Plan A is
    # served before any critique (the design draft); Plan B after.
    plan_calls["build"] += 1
    if not STATES_B.get("critique_seen"):
        plan_calls["pre_critique"] += 1
        return "```json\n" + json.dumps({"plan": PLAN_JSON}) + "\n```"
    return "```json\n" + json.dumps({"plan": PLAN_B_JSON}) + "\n```"


with tempfile.TemporaryDirectory() as td:
    os.environ["NEX_PROJECTS_DIR"] = td
    # --- design phase: persist Plan A ---
    res = run_agent_goal("mechanism puzzle box", bus=None,
                         llm_call=llm_design_critique_only,
                         llm_reachable=True, mode="design",
                         registry=registry)
    _expect(res.get("ok") is True and res.get("project_id"),
            "design phase returns a persisted project id")
    pid = res["project_id"]

    # Reset counters: only the BUILD phase is under scrutiny now. (The
    # design phase legitimately drafted Plan A once.)
    plan_calls["build"] = 0
    plan_calls["pre_critique"] = 0
    STATES_B["critique_seen"] = False

    approved = load_approved(pid)
    _expect(approved and approved.get("plan", {}).get("title")
            == "mechanism puzzle box",
            "Plan A (the plan the Plan Page showed) is persisted")
    g_a = load_approved_graph(pid)
    _expect(g_a is not None
            and [t.name for t in g_a.all()]
            == [s["name"] for s in PLAN_JSON["steps"]],
            "approved TaskGraph rebuilds to the exact approved steps")

    # --- build phase: EXACTLY Plan A, no re-planning ---
    st_a = load_project_state(pid)
    events_b = []
    agent_b_runs = run_agent_goal("mechanism puzzle box", bus=events_b.append,
                                  llm_call=llm_design_critique_only,
                                  llm_reachable=True, mode="build",
                                  state=st_a, graph=g_a,
                                  registry=registry)
    _expect(plan_calls["pre_critique"] == 0,
            "START BUILD executes Plan A — no planning before the critic")
    # ...while the Director still ran: an approved plan is executed as
    # approved, but it is now attributed to a system with criteria, which
    # is what the reviewer and the completion gate verify against.
    directed_b = [e for e in events_b if e.get("type") == "agent.directed"]
    _expect(len(directed_b) == 1,
            "START BUILD is directed into ONE system (%d directed events)"
            % len(directed_b))
    _expect(bool(st_a.current_system) and bool(st_a.criteria_for(
        st_a.current_system)),
        "the executed plan carries a system with success criteria (%s)"
        % st_a.current_system)
    executed = agent_b_runs["report"]["completed"]
    _expect(executed[:len(PLAN_JSON["steps"])]
            == [s["name"] for s in PLAN_JSON["steps"]],
            "executed tasks are exactly the approved Plan A steps")
    _expect(plan_calls["build"] == 1,
            "exactly one deliberate re-plan (the improve round)")
    _expect("polish frame" in agent_b_runs["report"]["completed"],
            "Plan B's step executed after the WEAK critique")
    _expect(STATES_B["critiques"] >= 2
            and agent_b_runs["report"]["status"] == "COMPLETED",
            "critic ran; WEAK -> improve -> re-critique PASS (bounded)")
    # COMPLETED is only allowed if the scoped criteria were actually
    # proven — the verification gate must not have been bypassed.
    _expect(not agent_b_runs["report"].get("unverified"),
            "COMPLETED carries no unverified criteria: %s"
            % (agent_b_runs["report"].get("unverified") or [])[:2])
    # Plan B persisted deliberately after the improve round.
    approved2 = load_approved(pid)
    if not (approved2 and (approved2.get("plan") or {}).get("title")
            == "mechanism puzzle box — plan B"):
        print("DEBUG approved2:", json.dumps(approved2)[:200])
    _expect(approved2 and (approved2.get("plan") or {}).get("title")
            == "mechanism puzzle box — plan B",
            "REPLAN/POLISH round persists Plan B as the current plan")
    os.environ.pop("NEX_PROJECTS_DIR", None)


# ---------------------------------------------------------------------------
# 7. skeleton runs stay untouched (no design, no LLM -> no critique noise)
# ---------------------------------------------------------------------------

servers4 = [
    mock_mcp.server_view("asset_mcp", mock_mcp.MockMCPServer(
        "asset_mcp", asset_tools)),
]
registry4 = agent_loop.CapabilityRegistry(servers4)
agent4 = agent_loop.AutonomousAgent(registry4, llm=None, bus=None,
                                    persist=False)
report4 = agent4.run("make a small thing")
_expect(agent4.last_critique is None,
        "bare skeleton run: no critique cycle (nothing to critique against)")


# ---------------------------------------------------------------------------
# 9. OBSERVE-loop detector — runtime evidence (untrusted engine output)
# ---------------------------------------------------------------------------

def _obs(kind, tool, text, empty=False):
    return {"kind": kind, "tool": tool, "text": text, "empty": empty,
            "ts": time.time()}

# Defects are caught.
f = critic.find_observations([
    _obs("screenshot", "engine.screenshot",
         '{"note": "player floating 30cm above the ground"}'),
    _obs("logs", "engine.inspect_logs",
         '{"logs": ["Exception: NullReferenceException in PlayerController"]},')
])
fk = {x["kind"] for x in f}
_expect("physics_defect" in fk, "obs: floating player -> physics_defect")
_expect("crash" in fk, "obs: crash log -> crash finding")
_expect(all(x["severity"] == "major" for x in f),
        "obs: runtime defects are major (drive repair)")

# A crash in METRICS counts too; clean evidence produces nothing.
f = critic.find_observations([
    _obs("metrics", "engine.performance",
         '{"fps": 3, "error": "fatal error: out of memory"}'),
    _obs("screenshot", "engine.screenshot",
         '{"note": "player grounded and centered"}'),
    _obs("logs", "engine.inspect_logs", '{"logs": ["boot ok", "no errors"]}'),
])
fk = {x["kind"] for x in f}
_expect("crash" in fk, "obs: fatal error in metrics -> crash")
_expect("physics_defect" not in fk, "obs: clean re-observation is NOT a defect")

# Empty captures + empty scenes.
f = critic.find_observations([
    _obs("screenshot", "engine.screenshot", "", empty=True),
    _obs("state", "engine.inspect_runtime", '{"actors_count": 0, "note": "empty scene"}'),
])
fk = {x["kind"] for x in f}
_expect("empty_capture" in fk and "empty_scene" in fk,
        "obs: empty capture + empty scene detected")

# The LATEST observation per tool wins: a repaired defect that was
# re-observed clean must not re-trigger (full critique() semantics).
st = project_state.ProjectState(goal="g")
st.design = {"concept": "x", "quality_gates": []}
st.record_observation(_obs("screenshot", "engine.screenshot",
                           '{"note": "player floating above ground"}'))
c1 = critic.critique("g", st, report4, llm=None, cycle=1)
_expect(any(x["kind"] == "physics_defect" for x in c1.findings),
        "obs: stale defect observed -> finding")
st.record_observation(_obs("screenshot", "engine.screenshot",
                           '{"note": "player grounded and centered"}'))
c2 = critic.critique("g", st, report4, llm=None, cycle=2)
_expect(not any(x.get("source") == "observation" for x in c2.findings),
        "obs: clean RE-observation clears the defect (latest wins)")

# Bug memory: defect recorded open, then marked fixed by the clean
# re-observation.
st2 = project_state.ProjectState(goal="g")
st2.design = {"concept": "x", "quality_gates": []}
st2.record_observation(_obs("screenshot", "engine.screenshot",
                            '{"note": "player floating above ground"}'))
critic.critique("g", st2, report4, llm=None, cycle=1)
_expect(st2.open_bugs() and st2.open_bugs()[0]["status"] == "open",
        "obs: defect recorded as open bug in project memory")
st2.record_observation(_obs("screenshot", "engine.screenshot",
                            '{"note": "player grounded and centered"}'))
critic.critique("g", st2, report4, llm=None, cycle=2)
_expect(not st2.open_bugs(),
        "obs: re-observation marks the bug fixed (memory hygiene)")


# ---------------------------------------------------------------------------
# 10. LLM observation judge — the second pair of eyes on runtime evidence
# ---------------------------------------------------------------------------

class _JudgeLLM:
    """Prompt-aware fake: answers the observation-judge prompt with a
    fixed JSON payload; anything else -> '{}'."""

    def __init__(self, findings, verdict="WEAK"):
        self.findings = findings
        self.verdict = verdict
        self.seen_prompts = []

    def __call__(self, messages):
        blob = json.dumps(messages)
        self.seen_prompts.append(blob)
        if "runtime observation judge" in blob:
            return json.dumps({"findings": self.findings,
                               "verdict": self.verdict})
        return "{}"


# A defect the RULES cannot name ("the level has no exit") is caught by
# the judge and drives a repair.
stj = project_state.ProjectState(goal="g")
stj.design = {"concept": "x", "quality_gates": []}
stj.record_observation(_obs("screenshot", "engine.screenshot",
                            '{"note": "a corridor with no exit anywhere"}'))
jllm = _JudgeLLM([{"kind": "playability", "severity": "major",
                   "message": "the level has no exit; the player cannot "
                              "finish"}])
cj = critic.critique("g", stj, report4, llm=jllm, cycle=1)
pj = [f for f in cj.findings if f.get("severity") == "major"]
_expect(any("no exit" in f.get("message", "") for f in pj),
        "judge: an unnameable defect from the evidence drives a finding")
_expect(cj.action == "REPLAN",
        "judge: a major judged defect -> REPLAN (got %s)" % cj.action)
_expect(any("live tool catalog" not in p.lower() for p in jllm.seen_prompts)
        and any("RUNTIME EVIDENCE" in p for p in jllm.seen_prompts),
        "judge: the evidence is framed as untrusted data in the prompt")
_expect(stj.open_bugs(), "judge: the judged defect enters project memory")

# Failure-safety: a model that explodes / returns garbage adds nothing
# and never breaks the critique (rules still stand).
def _boom(messages):
    if "runtime observation judge" in json.dumps(messages):
        raise RuntimeError("judge exploded")
    return "{}"


stj2 = project_state.ProjectState(goal="g")
stj2.design = {"concept": "x", "quality_gates": []}
stj2.record_observation(_obs("logs", "engine.inspect_logs",
                             '{"logs": ["NullReferenceException"]}'))
cj2 = critic.critique("g", stj2, report4, llm=_boom, cycle=1)
_expect(any(f.get("kind") == "crash" for f in cj2.findings),
        "judge: a broken judge keeps the deterministic findings intact")

# Garbage output is ignored (no fabricated defects).
stj3 = project_state.ProjectState(goal="g")
stj3.design = {"concept": "x", "quality_gates": []}
stj3.record_observation(_obs("screenshot", "engine.screenshot",
                             '{"note": "clean"}'))
cj3 = critic.critique("g", stj3, report4,
                      llm=_JudgeLLM([{"message": "x"}], ), cycle=1)
# (kind missing -> canonicalized to playability; the finding is still
# traceable and bounded, never free-form)
kinds3 = [f.get("kind") for f in cj3.findings]
_expect(all(k in critic._JUDGE_KINDS or k in critic._SEVERITY
            for k in kinds3),
        "judge: findings carry canonical kinds only: %s" % kinds3)

# No duplicate: a defect the rules already flagged is not double-counted.
stj4 = project_state.ProjectState(goal="g")
stj4.design = {"concept": "x", "quality_gates": []}
stj4.record_observation(_obs("logs", "engine.inspect_logs",
                             '{"logs": ["NullReferenceException"]}'))
cj4 = critic.critique("g", stj4, report4,
                      llm=_JudgeLLM([{"kind": "crash", "severity": "major",
                                      "message": "a crash in the logs"}],
                                    verdict="WEAK"), cycle=1)
crashes = [f for f in cj4.findings if f.get("kind") == "crash"]
_expect(len(crashes) == 1,
        "judge: a rule-flagged defect is not double-counted (%d)"
        % len(crashes))

# Clean evidence + a quiet judge = no observation findings at all.
stj5 = project_state.ProjectState(goal="g")
stj5.design = {"concept": "x", "quality_gates": []}
stj5.record_observation(_obs("screenshot", "engine.screenshot",
                             '{"note": "player grounded, level exit visible"}'))
cj5 = critic.critique("g", stj5, report4,
                      llm=_JudgeLLM([], verdict="PASS"), cycle=1)
_expect(not [f for f in cj5.findings if f.get("source") == "observation"],
        "judge: clean evidence + quiet judge -> no defect findings")
_expect(stj5.open_bugs() == [],
        "judge: a healthy re-observation leaves no open bug")

# ---------------------------------------------------------------------------
# QUALITY BARS reach the TESTER (and stay labelled as quality)
# ---------------------------------------------------------------------------
# The tester is the adversarial judge of "is this any good?". It must be
# handed the quality bar, and when it attacks a bar the finding must say so
# — the label decides whether a repair chases "works" or "feels right".
_seen_prompts = []

def _llm_attacks(msgs):
    _seen_prompts.append(msgs[-1]["content"])
    import json as _json
    return _json.dumps({"verdict": "WEAK", "attacks": [
        {"criterion": "quality: the camera never snaps",
         "attack": "camera teleports behind the player at the level edge",
         "severity": "major"},
        {"criterion": "player moves on input",
         "attack": "holding both keys freezes the character",
         "severity": "major"}]})

_atk = critic.llm_test("make a shooter", "character_controller",
                       ["player moves on input"], [],
                       _llm_attacks,
                       quality=["the camera never snaps",
                                "acceleration is eased"])
_expect(bool(_atk) and len(_atk) == 2, "both attacks become findings")
_expect(_atk[0]["evidence"].get("quality_bar") == "the camera never snaps",
        "an attack on a quality bar is labelled with that bar")
_expect("quality_bar" not in _atk[1]["evidence"],
        "a plain criterion attack carries no quality label")
_expect(all(f["kind"] == "test_failure" for f in _atk),
        "quality attacks stay test_failure findings (existing repair path)")
_expect("QUALITY BAR" in _seen_prompts[0]
        and "the camera never snaps" in _seen_prompts[0],
        "the tester prompt contains the quality bar it must attack")

# Without a bar the prompt stays exactly as before (no empty heading).
_seen_prompts.clear()
critic.llm_test("g", "s", ["c"], [], _llm_attacks)
_expect("QUALITY BAR (judge the evidence against these too"
        not in _seen_prompts[0],
        "no quality bars -> no quality block in the tester prompt")

print("\nAll critic + improve-loop tests passed.")
