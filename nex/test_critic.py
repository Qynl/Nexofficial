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
    return "```json\n" + json.dumps(PLAN_JSON) + "\n```"


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
    return "```json\n" + json.dumps(PLAN_JSON) + "\n```"


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
    events = []
    agent = agent_loop.AutonomousAgent(
        registry, llm=llm_pipeline, bus=events.append,
        max_critique_cycles=1, persist=True)
    report = agent.run("mechanism puzzle box")

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
    return "```json\n" + json.dumps(PLAN_JSON) + "\n```"


servers2 = [
    mock_mcp.server_view("asset_mcp", mock_mcp.MockMCPServer(
        "asset_mcp", asset_tools)),
    mock_mcp.server_view("game_mcp", mock_mcp.MockMCPServer(
        "game_mcp", build_tools)),
]
registry2 = agent_loop.CapabilityRegistry(servers2)
events2 = []
agent2 = agent_loop.AutonomousAgent(
    registry2, llm=llm_improve, bus=events2.append,
    max_critique_cycles=2, persist=False)
report2 = agent2.run("mechanism puzzle box")
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
    return "```json\n" + json.dumps(PLAN_JSON) + "\n```"


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


print("\nAll critic + improve-loop tests passed.")
