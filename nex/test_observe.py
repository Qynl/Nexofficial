"""OBSERVE loop: BUILD -> RUN -> OBSERVE -> CRITIQUE -> FIX -> RE-OBSERVE.

The audit's biggest functional gap: Nex could say "build succeeded" but
could not SEE the running game. This suite exercises the full loop with an
in-process mock engine:

  A. defect detected: first screenshot shows the player floating, logs
     show a crash -> critic (major findings) -> repair plan -> re-observe
     clean -> bugs marked fixed, report honest.
  B. experiment safety: a repair run that makes things WORSE rolls the
     workspace files back to the pre-change snapshot.
  C. workspace checkpoints: snapshot -> mutate -> restore round-trip.
"""

import importlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent.loop as agent_loop            # noqa: E402
import agent.mock_mcp as mock_mcp          # noqa: E402
from agent.events import STATUS_COMPLETED  # noqa: E402
from agent.project_state import ProjectState  # noqa: E402


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Mock engine: a tiny MCP server with build/launch/observation tools and
# DETERMINISTIC evidence: first screenshot + logs show a defect, the
# re-observation after the fix is clean.
# ---------------------------------------------------------------------------

ENGINE_TOOLS = [
    {"name": "build", "description": "build the project",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "launch", "description": "launch the game",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "screenshot", "description": "capture the game screen",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_logs", "description": "read the runtime logs",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fix_player_controller",
     "description": "repair the player controller physics",
     "inputSchema": {"type": "object", "properties": {}}},
]


class FlakyEngine(mock_mcp.MockMCPServer):
    """Engine whose FIRST run has a real defect; after the fix it is clean.
    `launch` writes a version stamp file so rollback can be observed."""

    def __init__(self, workspace):
        super().__init__("engine", ENGINE_TOOLS,
                         fail=None)
        self.workspace = workspace
        self.screens = 0

    def call(self, tool, args):
        if tool == "launch":
            n = 1 + len([f for f in os.listdir(self.workspace)
                         if f.startswith("run_")]) if os.path.isdir(
                             self.workspace) else 1
            try:
                os.makedirs(self.workspace, exist_ok=True)
                with open(os.path.join(self.workspace,
                                       "run_%d_world.bin" % n), "w") as f:
                    f.write("world-v%d" % n)
            except OSError:
                pass
            return {"result": {"id": "run_%d" % n, "ok": True}}
        if tool == "screenshot":
            self.screens += 1
            if self.screens == 1:
                return {"result": {"width": 1280,
                                   "note": "player floating 30cm above "
                                           "the ground"}}
            return {"result": {"width": 1280,
                               "note": "player grounded and centered"}}
        if tool == "inspect_logs":
            if self.screens == 1:
                return {"result": {"logs": ["boot ok",
                                            "Exception: NullReference"
                                            "Exception in PlayerController"],
                                   "errors": ["NullReferenceException"]}}
            return {"result": {"logs": ["boot ok", "no errors"],
                               "errors": []}}
        return super().call(tool, args)


# A fake LLM that produces exactly two plans:
#   call 1 (initial): build -> launch -> observe (defect will be found)
#   call 2 (improve): fix -> launch -> re-observe (clean)
PLAN_A = {
    "plan": {
        "title": "platformer v1",
        "rationale": "build then observe",
        "verification": "screenshot + logs",
        "steps": [
            {"name": "build", "tool": "engine.build", "args": {},
             "why": "compile", "expect": "ok", "depends_on": []},
            {"name": "launch", "tool": "engine.launch", "args": {},
             "why": "run the game", "expect": "ok",
             "depends_on": ["build"]},
            {"name": "screen", "tool": "engine.screenshot", "args": {},
             "why": "observe the running game", "expect": "ok",
             "depends_on": ["launch"]},
            {"name": "logs", "tool": "engine.inspect_logs", "args": {},
             "why": "observe runtime logs", "expect": "ok",
             "depends_on": ["launch"]},
        ],
    }
}
PLAN_B = {
    "plan": {
        "title": "platformer v1 (repair)",
        "rationale": "fix the floating player, then prove it",
        "verification": "re-observe screenshot + logs",
        "steps": [
            {"name": "fix", "tool": "engine.fix_player_controller",
             "args": {}, "why": "repair physics", "expect": "ok",
             "depends_on": []},
            {"name": "launch2", "tool": "engine.launch", "args": {},
             "why": "re-run after repair", "expect": "ok",
             "depends_on": ["fix"]},
            {"name": "screen2", "tool": "engine.screenshot", "args": {},
             "why": "prove the repair", "expect": "ok",
             "depends_on": ["launch2"]},
            {"name": "logs2", "tool": "engine.inspect_logs", "args": {},
             "why": "confirm no crash", "expect": "ok",
             "depends_on": ["launch2"]},
        ],
    }
}


class FakeLLM:
    """Prompt-aware fake model: planning prompts get the next plan JSON;
    the critic's prompt gets "{}" (no LLM critique — the deterministic
    findings stand, which is exactly what we want to test here)."""

    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = []

    def __call__(self, messages):
        import json
        prompt = json.dumps(messages)
        self.calls.append(messages)
        is_planning = ("LIVE TOOL CATALOG" in prompt
                       or "Produce the plan JSON" in prompt)
        if is_planning and self.plans:
            return json.dumps(self.plans.pop(0))
        return "{}"


def _find(events, etype):
    return [e for e in events if e.get("type") == etype]


# ===========================================================================
# A. defect -> critique -> repair -> clean re-observation
# ===========================================================================

td = tempfile.mkdtemp()
try:
    engine = FlakyEngine(os.path.join(td, "ws"))
    reg = agent_loop.CapabilityRegistry([mock_mcp.server_view("engine", engine)])
    llm = FakeLLM([PLAN_A, PLAN_B])
    events = []
    state = ProjectState(goal="make a platformer")
    state.design = {"concept": "a platformer", "quality_gates": []}

    # max_critique_cycles=2: round 1 finds the defect -> repair; round 2
    # judges the RE-OBSERVATION (clean) -> marks the bugs fixed, COMPLETE.
    agent = agent_loop.AutonomousAgent(reg, llm=llm, persist=False,
                                       bus=lambda e: events.append(e),
                                       workspace_root=os.path.join(td, "ws"),
                                       max_critique_cycles=2)
    report = agent.run("make a platformer", state=state)

    # 1) The run finished with work done.
    _expect(report.status == STATUS_COMPLETED,
            "observe: full loop ends COMPLETED (was %s: %s)"
            % (report.status, report.reasons))

    # 2) Observations were captured from the runtime tools.
    obs = state.observations
    _expect(len(obs) >= 4,
            "observe: runtime observations captured (%d, want >=4)"
            % len(obs))
    kinds = [o.get("kind") for o in obs]
    _expect("screenshot" in kinds and "logs" in kinds,
            "observe: screenshot + logs evidence captured: %s" % kinds)
    shots = [o for o in obs if o.get("kind") == "screenshot"]
    _expect(any("floating" in o.get("text", "") for o in shots),
            "observe: the FIRST screenshot carries the defect evidence")
    _expect("floating" not in shots[-1].get("text", ""),
            "observe: the re-observation after the repair is clean")

    # 3) The critic saw the defect (major findings) and drove a repair.
    crits = _find(events, "agent.critique")
    _expect(len(crits) >= 1, "observe: critic ran on the observations")
    c1 = crits[0].get("findings", [])
    c1_kinds = {f.get("kind") for f in c1 if isinstance(f, dict)}
    _expect("physics_defect" in c1_kinds,
            "observe: critic flagged the floating player: %s" % c1_kinds)
    _expect("crash" in c1_kinds,
            "observe: critic flagged the runtime crash: %s" % c1_kinds)
    _expect(crits[0].get("action") == "REPLAN",
            "observe: major findings drove a repair (REPLAN), got %s"
            % crits[0].get("action"))

    # 4) The repair plan was actually executed (fix + re-observe).
    _expect(engine.screens == 2,
            "observe: the game was re-launched and re-observed "
            "(screenshots: %d)" % engine.screens)
    _expect(any(e.get("tool") == "fix_player_controller"
                for e in _find(events, "agent.tool_succeeded")),
            "observe: the repair step ran (fix_player_controller)")

    # 5) Project memory: defects recorded as open, then FIXED by the
    #    clean re-observation.
    _expect(bool(state.known_bugs),
            "observe: confirmed defects entered project memory")
    _expect(all(b.get("status") == "fixed" for b in state.known_bugs),
            "observe: re-observation proved the fix (bugs -> fixed): %s"
            % [(b.get("kind"), b.get("status")) for b in state.known_bugs])

    # 6) The planner got the project memory on the repair round.
    plan_calls = [c for c in llm.calls
                  if "LIVE TOOL CATALOG" in json.dumps(c)]
    # Two rounds for THIS system (initial + repair) — plus, since round 3
    # of the work, one more if the campaign moved on to the next system.
    # The repair round is what this suite is about; the campaign has its
    # own section in test_director.py.
    _expect(len(plan_calls) >= 2,
            "observe: the initial + repair planning rounds both happened, "
            "got %d" % len(plan_calls))
    campaign_started = [e for e in events
                        if e.get("type") == "agent.system_started"]
    _expect(len(plan_calls) == 2 + len(campaign_started),
            "observe: exactly one planning round per system (initial + "
            "repair, plus the campaign's next system): %d rounds, %d "
            "campaign systems" % (len(plan_calls), len(campaign_started)))
    second = json.dumps(plan_calls[1])
    _expect("CONFIRMED DEFECTS" in second,
            "observe: repair plan prompt carries the open-bug memory")
    _expect("OBSERVE RULE" in json.dumps(plan_calls[0]),
            "observe: planner told to include observation steps")

    # 7) The UI events exist (observation rows in the panel).
    _expect(len(_find(events, "agent.observation")) >= 4,
            "observe: agent.observation events published for the UI")
finally:
    shutil.rmtree(td, ignore_errors=True)


# ===========================================================================
# B. experiment safety: a repair run that makes things WORSE -> rollback
# ===========================================================================

td2 = tempfile.mkdtemp()
try:
    ws2 = os.path.join(td2, "ws")
    os.makedirs(ws2, exist_ok=True)
    world = os.path.join(ws2, "world.bin")
    with open(world, "w") as f:
        f.write("world-v1")

    class BreakingEngine(FlakyEngine):
        def call(self, tool, args):
            if tool == "fix_player_controller":
                # The "fix" corrupts the world before failing.
                try:
                    with open(os.path.join(self.workspace, "world.bin"),
                              "w") as f:
                        f.write("CORRUPTED")
                except OSError:
                    pass
                return {"error": "fix made the build worse"}
            return super().call(tool, args)

    engine2 = BreakingEngine(ws2)
    reg2 = agent_loop.CapabilityRegistry(
        [mock_mcp.server_view("engine", engine2)])
    llm2 = FakeLLM([PLAN_A, PLAN_B])
    events2 = []
    state2 = ProjectState(goal="make a platformer")
    state2.design = {"concept": "a platformer", "quality_gates": []}

    agent2 = agent_loop.AutonomousAgent(
        reg2, llm=llm2, persist=False,
        bus=lambda e: events2.append(e), workspace_root=ws2)
    report2 = agent2.run("make a platformer", state=state2)

    rolled = _find(events2, "agent.experiment_rolled_back")
    _expect(bool(rolled),
            "rollback: a worse repair run triggered experiment_rolled_back")
    with open(world, "r") as f:
        content = f.read()
    _expect(content == "world-v1",
            "rollback: workspace restored to the pre-change snapshot "
            "(file content: %r)" % content)
    _expect(report2.status in ("PARTIAL", "FAILED", "BLOCKED"),
            "rollback: the report is honest about the failed experiment "
            "(%s)" % report2.status)
finally:
    shutil.rmtree(td2, ignore_errors=True)


# ===========================================================================
# C. workspace checkpoints: snapshot -> mutate -> restore
# ===========================================================================

from agent.checkpoints import (list_snapshots, restore_workspace,  # noqa: E402
                               snapshot_workspace)

td3 = tempfile.mkdtemp()
try:
    ws3 = os.path.join(td3, "ws")
    os.makedirs(ws3)
    p = os.path.join(ws3, "level.umap")
    with open(p, "w") as f:
        f.write("original")

    snap = snapshot_workspace(ws3, label="before_experiment")
    _expect(snap is not None and os.path.exists(snap),
            "checkpoint: workspace snapshot created")

    with open(p, "w") as f:
        f.write("mutated")
    _expect(restore_workspace(ws3, snap), "checkpoint: restore returned ok")
    with open(p, "r") as f:
        _expect(f.read() == "original",
                "checkpoint: file restored to its pre-experiment content")
    _expect(len(list_snapshots(ws3)) == 1,
            "checkpoint: snapshot listed")
finally:
    shutil.rmtree(td3, ignore_errors=True)





# ===========================================================================
# D. Director layer end-to-end: the agent works INSIDE the scope cage and
#    only reports COMPLETED after the checklist is PROVEN by observation.
# ===========================================================================
print("\n=== D. director scoping + mandatory verification ===")

DIRECTOR_JSON = json.dumps({
    "game": "platformer",
    "systems": [{"id": "character_controller", "why": "movement first"},
                {"id": "core_loop", "why": "a reason to play"}],
    "extra_systems": [],
})

REVIEW_PASS = json.dumps({
    "criteria": [{"criterion": c, "status": "pass",
                  "why": "the clean re-observation shows it"}
                 for c in ["player moves on input",
                           "camera follows without clipping through geometry",
                           "the character rests on the ground (does not "
                           "float or sink)",
                           "no runtime errors in the movement logs"]],
    "verdict": "PASS",
})

TESTER_JSON = json.dumps({"attacks": [], "verdict": "PASS"})


class DirectorLLM:
    """Prompt-aware fake model: answers EACH ROLE's prompt with the right
    shape. This is what the real architecture looks like — one model,
    several scoped jobs."""

    def __init__(self, plans):
        self.plans = list(plans)
        self.roles_seen = set()

    def __call__(self, messages):
        blob = json.dumps(messages)
        if "PROVEN RECIPES" in blob:
            self.roles_seen.add("director")
            return DIRECTOR_JSON
        if "ROLE: REVIEWER" in blob:
            self.roles_seen.add("reviewer")
            return REVIEW_PASS
        if "ROLE: TESTER" in blob:
            self.roles_seen.add("tester")
            return TESTER_JSON
        if "LIVE TOOL CATALOG" in blob or "Produce the plan JSON" in blob:
            self.roles_seen.add("planner")
            if self.plans:
                return json.dumps(self.plans.pop(0))
            return "{}"
        return "{}"


td_d = tempfile.mkdtemp()
try:
    engine_d = FlakyEngine(os.path.join(td_d, "ws"))
    reg_d = agent_loop.CapabilityRegistry(
        [mock_mcp.server_view("engine", engine_d)])
    llm_d = DirectorLLM([PLAN_A, PLAN_B])
    events_d = []
    state_d = ProjectState(goal="make a platformer")
    state_d.design = {"concept": "a platformer", "quality_gates": []}
    agent_d = agent_loop.AutonomousAgent(
        reg_d, llm=llm_d, persist=False,
        bus=lambda e: events_d.append(e), workspace_root=os.path.join(td_d, "ws"),
        max_critique_cycles=2)
    report_d = agent_d.run("make a platformer", state=state_d)

    directed = _find(events_d, "agent.directed")
    _expect(bool(directed), "director ran and published the system map")
    if directed:
        _expect(directed[0].get("source") in ("model", "recipes"),
                "decomposition has a provenance (%s)" % directed[0].get("source"))
        _expect(len(directed[0].get("systems") or []) >= 2,
                "systems were decomposed: %s"
                % [s.get("id") for s in directed[0].get("systems") or []])
        _expect(directed[0].get("objective"),
                "a single objective was set: %s"
                % str(directed[0].get("objective"))[:70])
        _expect(bool(directed[0].get("success")),
                "the objective carries SUCCESS CRITERIA")
    _expect("director" in llm_d.roles_seen and "planner" in llm_d.roles_seen,
            "the model was used in several ROLES: %s" % sorted(llm_d.roles_seen))

    scoped = [e for e in events_d if e.get("type") == "agent.plan_started"]
    _expect(bool(scoped), "planning started")
    # The scope reached EXECUTION: the tasks carried the scoped system and
    # its criteria (visible as criterion evidence in the project memory).
    sid = state_d.current_system
    ev = (state_d.criteria_evidence or {}).get(sid) or {}
    _expect(bool(ev),
            "executed tasks carried system+criteria into the memory (%s: %s)"
            % (sid, list(ev.items())[:2]))

    _expect(report_d.status == STATUS_COMPLETED,
            "run completes when the criteria are proven (%s)" % report_d.status)
    _expect(not report_d.unverified,
            "nothing unverified is left in the report")
    _expect(state_d.system_status("character_controller") == "complete",
            "the scoped system is marked COMPLETE (got %r)"
            % state_d.system_status("character_controller"))
    _expect(not state_d.unmet_criteria("character_controller"),
            "every criterion of the scoped system is PROVEN")
    verified_events = _find(events_d, "agent.system_verified")
    _expect(bool(verified_events),
            "the verification event is published for the UI")
    _expect("tester" in llm_d.roles_seen,
            "the TESTER role ran against the system's criteria")
    _expect("reviewer" in llm_d.roles_seen,
            "the REVIEWER role judged the criteria from the evidence")

    # Scope creep is impossible to hide: a plan that touches ANOTHER system
    # is reported (the work is not blocked — it is named).
    state_d.record_success("side_quest_system@core_loop")
    creep = __import__("agent.critic", fromlist=["critic"]).find_scope_creep(
        state_d, "character_controller")
    _expect(bool(creep),
            "work outside the scoped system is reported (scope_creep)")
finally:
    shutil.rmtree(td_d, ignore_errors=True)

print("\nAll observe-loop tests passed.")
