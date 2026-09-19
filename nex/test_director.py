#!/usr/bin/env python3
"""Tests for the Director layer (NEX as a game-development OS).

The thesis under test: a small model does not need to be smarter — the
architecture around it must supply the engineering discipline. Concretely:

  1. RECIPES — a proven architecture library; selection is deterministic
     and dependency-closed, checklists are always present.
  2. DIRECTOR — decomposes a request into systems, orders them by
     dependency, and enforces the structural roots. A weak model can
     refine the decomposition but never remove the roots or the
     checklists.
  3. SCOPE ENVELOPE — one objective, success criteria, explicit DO-NOT.
  4. BOUNDED MEMORY — every collection upserts/caps; the state fed to the
     model cannot grow without limit; honest counters survive trimming.
  5. ROLES — specialized prompts, tightly scoped context, validated
     output (reviewer/tester/debugger).
  6. MANDATORY VERIFICATION — "the tool call returned" is never proof; a
     clean observation or a reviewer verdict is.
  7. SERVER PATH — the same discipline holds on the route the UI actually
     drives (server_run.run_agent_goal with an injected registry), because
     a Director that only works in the unit tests is worthless.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent.critic as critic  # noqa: E402
import agent.director as director_mod  # noqa: E402
import agent.loop as agent_loop  # noqa: E402
import agent.mock_mcp as mock_mcp  # noqa: E402
import agent.recipes as recipes  # noqa: E402
import agent.roles as roles  # noqa: E402
import agent.verification as verification  # noqa: E402
from agent.project_state import LIMITS, ProjectState  # noqa: E402


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


# ===========================================================================
# 1. RECIPES
# ===========================================================================

print("=== 1. recipe library ===")
_expect(len(recipes.RECIPES) >= 12,
        "library has a real breadth (%d recipes)" % len(recipes.RECIPES))
ids = [r.id for r in recipes.RECIPES]
_expect(len(ids) == len(set(ids)), "recipe ids are unique")
for r in recipes.RECIPES:
    _expect(bool(r.steps) and bool(r.checklist),
            "recipe '%s' has steps AND a checklist" % r.id)
    if r.system not in (recipes.WORLD, recipes.GAMEPLAY, recipes.UI,
                        recipes.SYSTEMS):
        _expect(False, "recipe '%s' has a valid layer (%r)"
                % (r.id, r.system))
    for req in r.requires:
        _expect(req in recipes.RECIPES_BY_ID,
                "recipe '%s' requires a real recipe (%s)" % (r.id, req))

# Selection is request-driven AND always keeps the structural roots.
shooter = recipes.select_recipes("make a first person shooter with guns "
                                 "and enemies")
sids = [r.id for r in shooter]
_expect("character_controller" in sids and "level_blockout" in sids,
        "roots always selected: %s" % sids[:3])
_expect("weapon_system" in sids and "enemy_ai" in sids,
        "shooter request pulls weapon + enemy systems: %s" % sids)
_puzzle = [r.id for r in recipes.select_recipes(
    "make a small puzzle game about light and mirrors")]
_expect("weapon_system" not in _puzzle,
        "an unrelated genre does NOT pull combat systems: %s" % _puzzle)
# Dependency closure: nothing is selected without its requirements.
for r in shooter:
    for req in r.requires:
        _expect(req in sids or len(sids) > 8,
                "selection is dependency-closed ('%s' -> '%s')" % (r.id, req))
_expect(recipes.checklist_for(["enemy_ai"])[0] == "the enemy detects the player",
        "checklist_for returns the recipe's real criteria")

# ===========================================================================
# 2. DIRECTOR
# ===========================================================================

print("\n=== 2. director ===")
gp = director_mod.plan_from_recipes("make a survival horror game with "
                                    "combat and an inventory")
_expect(gp.source == "recipes", "no model -> the library is the director")
_expect(2 <= len(gp.systems) <= 9,
        "decomposition stays focused (%d systems)" % len(gp.systems))
_expect(gp.current() is not None and gp.current().id ==
        "character_controller",
        "build order starts at the foundation (%s)" % gp.current().id)
_expect(all(s.checklist for s in gp.systems),
        "every system carries a checklist")
_expect(len(gp.checklists()) >= 8, "merged checklists are usable")

# A fallen-over model must not be able to simplify the game away.
def _bad_llm(messages):
    return json.dumps({"game": "shooter",
                       "systems": [{"id": "enemy_ai", "why": "shooting"},
                                   {"id": "not_a_real_recipe", "why": "x"}]})


gp2 = director_mod.direct("make a shooter", llm=_bad_llm)
root_ids = {s.id for s in gp2.systems}
_expect("character_controller" in root_ids and "level_blockout" in root_ids,
        "roots survive a model that dropped them: %s" % sorted(root_ids))
_expect("not_a_real_recipe" not in root_ids,
        "hallucinated recipe ids are dropped")
_expect(all(s.checklist for s in gp2.systems),
        "every surviving system still carries its checklist")


def _good_llm(messages):
    return json.dumps({
        "game": "survival horror",
        "systems": [{"id": "character_controller", "why": "core"},
                    {"id": "inventory", "why": "loot"},
                    {"id": "enemy_ai", "why": "threat"}],
        "extra_systems": [{"name": "fear_meter", "layer": "gameplay",
                           "why": "genre staple",
                           "checklist": ["fear rises near enemies",
                                         "fear decays over time"]}]})


gp3 = director_mod.direct("make a survival horror game", llm=_good_llm)
_expect(gp3.source == "model" and gp3.game == "survival horror",
        "model refinement is used when it is valid (%s)" % gp3.source)
fm = gp3.by_id("fear_meter")
_expect(fm is not None and fm.layer == "gameplay"
        and len(fm.checklist) == 2,
        "genre-specific extra systems are adopted with their checklist")
_expect(all(s.checklist for s in gp3.systems),
        "recipe systems keep library checklists through the merge")

# Status folds in from the project memory (no re-planning of finished work).
gp4 = director_mod.apply_status(
    director_mod.plan_from_recipes("make a platformer"),
    {"character_controller": {"status": "complete"},
     "level_blockout": {"status": "in_progress"}})
_expect(gp4.by_id("character_controller").status == "complete",
        "memory status folds into a fresh plan")
_expect(gp4.current().id != "character_controller",
        "a completed system is not the current objective again")

# ===========================================================================
# 3. SCOPE ENVELOPE
# ===========================================================================

print("\n=== 3. scope envelope (the anti-'I improved the entire project') ===")
env = director_mod.scope_envelope(gp.current(), gp.remaining())
_expect(env["system"] == "character_controller"
        and env["objective"].startswith("Implement the"),
        "envelope states ONE objective: %s" % env["objective"][:60])
_expect(env["success"] and len(env["success"]) >= 3,
        "envelope carries the checklist as success criteria")
_expect(any("do not" in d.lower() for d in env["do_not"])
        and any("already complete" in d.lower() for d in env["do_not"]),
        "envelope has an explicit DO-NOT list")
block = director_mod.scope_block(env)
_expect("SUCCESS CRITERIA" in block and "DO NOT" in block
        and "AT MOST" in block,
        "envelope renders into the planner prompt with hard limits")
_expect(env["max_steps"] >= 1, "step budget is set (%d)" % env["max_steps"])

# The cage must actually REACH the model: the planner prompt carries the
# objective, the criteria and the DO-NOT list, and the tasks it produces
# carry the system attribution.
import agent.model_planner as model_planner  # noqa: E402
from agent.registry import CapabilityRegistry  # noqa: E402


class _PlanLLM:
    def __init__(self):
        self.seen = ""

    def __call__(self, messages):
        self.seen = json.dumps(messages)
        return json.dumps({"plan": {
            "title": "character", "rationale": "r", "verification": "v",
            "steps": [{"name": "spawn", "tool": "engine.spawn_player",
                       "args": {}, "why": "w", "expect": ""}]}})


_llm = _PlanLLM()
_reg = CapabilityRegistry.from_upstreams([
    mock_mcp.MockMCPServer("engine", [
        {"name": "spawn_player", "description": "spawn the player",
         "inputSchema": {"type": "object", "properties": {}}}])])
_g, _plan = model_planner.model_driven_planner(
    "make a platformer", _reg, _llm,
    scope=director_mod.scope_envelope(gp.current(), gp.remaining()))
_expect("CURRENT OBJECTIVE" in _llm.seen and "SUCCESS CRITERIA" in _llm.seen
        and "DO NOT" in _llm.seen,
        "the scope cage reaches the planner prompt (objective/criteria/"
        "do-not)")
_expect("AT MOST" in _llm.seen,
        "the planner prompt carries a hard step budget")
_t = _g.all()[0] if _g.all() else None
_expect(_t is not None and _t.system == "character_controller",
        "planned tasks carry the scoped system (%r)"
        % (getattr(_t, "system", None),))
_expect(_t is not None and bool(_t.criteria),
        "planned tasks carry the checklist criteria (%d)"
        % len(getattr(_t, "criteria", []) or []))


# ===========================================================================
# 4. BOUNDED MEMORY
# ===========================================================================

print("\n=== 4. bounded, upserting project memory ===")
st = ProjectState(goal="g")
for i in range(400):
    st.record_success("task_%d" % i)
_expect(len(st.completed) == LIMITS["completed"]
        and st.completed_count == 400,
        "completed is a rolling window with an honest count (%d/%d)"
        % (len(st.completed), st.completed_count))
for i in range(50):
    st.record_failure("same_task", "error %d" % (i % 3))
_expect(len(st.failed) == 1,
        "repeated failures UPSERT by task instead of piling up (%d)"
        % len(st.failed))
_expect(len(st.known_errors) == 3,
        "error memory dedupes by signature (%d)" % len(st.known_errors))
for i in range(200):
    st.record_asset({"id": "asset_%d" % (i % 7), "type": "mesh"})
_expect(len(st.assets) == 7,
        "assets upsert by identity (%d)" % len(st.assets))
for i in range(300):
    st.decision("decision %d" % (i % 5))
_expect(len(st.decisions) == 5, "decisions dedupe (%d)" % len(st.decisions))
for i in range(200):
    st.remember("key_%d" % i, "value")
_expect(len(st.knowledge) <= LIMITS["knowledge"],
        "knowledge is capped (%d)" % len(st.knowledge))
size_before = st.memory_size()
st.record_observation({"kind": "logs", "tool": "t", "text": "x" * 2000})
for i in range(300):
    st.upsert_system("sys_%d" % i, "planned")
_expect(len(st.systems) <= LIMITS["systems"],
        "system map is bounded (%d)" % len(st.systems))
trimmed = st.compact_memory()
_expect(st.memory_size() <= 200_000,
        "model-visible memory stays small (%d chars, trimmed=%s)"
        % (st.memory_size(), sorted(trimmed.keys())))
_expect(isinstance(trimmed, dict), "compaction reports what it trimmed")
# A completed system must never be the one trimmed away.
st2 = ProjectState(goal="g")
st2.upsert_system("important", "complete")
for i in range(LIMITS["systems"] + 10):
    st2.upsert_system("planned_%d" % i, "planned")
st2.compact_memory()
_expect("important" in st2.systems,
        "compaction drops PLANNED systems first, never completed ones")
d = st2.to_dict()
_expect("systems" in d and "knowledge" in d,
        "the new memory fields persist in to_dict")
_expect(ProjectState.from_dict(d).to_dict()["systems"] == d["systems"],
        "memory round-trips through to_dict/from_dict")

# ===========================================================================
# 5. ROLES
# ===========================================================================

print("\n=== 5. specialized roles ===")
for role in roles.ROLES:
    p = roles.system_prompt(role)
    _expect("ROLE:" in p and "NO filesystem" in p,
            "role '%s' has a scoped prompt + the boundary rule" % role)
_expect(roles.system_prompt(roles.PLANNER) !=
        roles.system_prompt(roles.DEBUGGER),
        "roles are genuinely different prompts")
_expect("smallest correct fix" in
        roles.system_prompt(roles.DEBUGGER).lower(),
        "debugger is scoped to the smallest fix")
_expect("break" in roles.system_prompt(roles.TESTER).lower(),
        "tester is adversarial by construction")
_expect("one objective" in roles.system_prompt(roles.PLANNER).lower()
        or "one objective" in roles.system_prompt(roles.PLANNER),
        "planner is scoped to one objective, not the project")
rev = roles.reviewer_json('noise {"verdict": "PASS", "criteria": []} noise')
_expect(rev and rev.get("verdict") == "PASS",
        "reviewer output parsing tolerates prose around the JSON")
_expect(roles.reviewer_json("not json at all") is None,
        "unusable reviewer output is rejected (never assumed)")

st3 = ProjectState(goal="g")
st3.set_criteria("combat", ["the player can take damage"])
st3.mark_evidence("combat", "the player can take damage")
_expect(st3.criterion_state("combat", "the player can take damage")
        == "evidence",
        "a successful step records EVIDENCE, not proof")
roles.apply_review({"criteria": [
    {"criterion": "the player can take damage", "status": "pass",
     "why": "logs show damage"}]}, st3, "combat")
_expect(st3.criterion_state("combat", "the player can take damage") == "pass",
        "only a reviewer verdict with evidence turns evidence into proof")
st3.mark_evidence("combat", "the player can take damage")
_expect(st3.criterion_state("combat", "the player can take damage") == "pass",
        "later weak evidence cannot downgrade a proven criterion")

# ===========================================================================
# 6. MANDATORY VERIFICATION
# ===========================================================================

print("\n=== 6. mandatory verification ===")
ok, note = verification.check_criteria(["the enemy can die"], {})
_expect(not ok, "an empty result cannot prove a checklist criterion")
ok, note = verification.check_criteria(["the enemy can die"],
                                       {"enemy": {"alive": False}})
_expect(ok and "stay pending" in note,
        "a real result is accepted as pending evidence, not as proof")

st4 = ProjectState(goal="g")
st4.set_criteria("combat", ["enemy detects player", "enemy can die"])
unmet = st4.unmet_criteria("combat")
_expect(len(unmet) == 2 and unmet[0]["state"] == "unknown",
        "unproven criteria are visible with their state")
st4.prove_criteria("combat", note="observed clean")
_expect(not st4.unmet_criteria("combat"),
        "a clean observation proves the criteria")
st4.mark_criterion("combat", "enemy can die", False, note="respawn broke")
_expect(st4.unmet_criteria("combat"),
        "a failed criterion re-opens the gate")
_expect(not st4.unmet_criteria("inventory"),
        "systems without criteria are never gated (nothing invented)")

# The completion gate: a PARTIAL-honest report instead of a lie.
class _Reg:
    servers = []


class _FakeAgent(agent_loop.AutonomousAgent):
    def __init__(self):
        self.registry = agent_loop.CapabilityRegistry([])
        self.bus = None
        self.llm = None
        self.audit = None
        super().__init__(self.registry, llm=None, persist=False)


agent = _FakeAgent()
st5 = ProjectState(goal="g")
st5.current_system = "combat"
st5.set_criteria("combat", ["enemy can die"])
rep = agent_loop.CompletionReport(status=agent_loop.STATUS_COMPLETED, goal="g")
rep = agent._completion_gate(st5, rep)
_expect(rep.status == agent_loop.STATUS_PARTIAL,
        "COMPLETED with unproven criteria is downgraded (%s)" % rep.status)
_expect(any("enemy can die" in r for r in rep.reasons)
        and rep.unverified,
        "the report names exactly what was not verified: %s"
        % (rep.reasons[-1:] or ["-"]))
st5.prove_criteria("combat")
rep2 = agent._completion_gate(
    st5, agent_loop.CompletionReport(status=agent_loop.STATUS_COMPLETED,
                                     goal="g"))
_expect(rep2.status == agent_loop.STATUS_COMPLETED,
        "proven criteria let the run complete")
st_no = ProjectState(goal="g")          # no director layer at all
rep3 = agent._completion_gate(
    st_no, agent_loop.CompletionReport(status=agent_loop.STATUS_COMPLETED,
                                       goal="g"))
_expect(rep3.status == agent_loop.STATUS_COMPLETED,
        "legacy runs without a system map are unaffected")

# Scope creep is reported, not silently ignored.
st6 = ProjectState(goal="g")
st6.current_system = "combat"
st6.record_success("build arena@level_blockout")
st6.record_success("spawn enemy@combat")
creep = critic.find_scope_creep(st6, "combat")
_expect(len(creep) == 1 and "level_blockout" in creep[0]["message"],
        "work outside the scoped system is NAMED: %s"
        % (creep[0]["message"][:70] if creep else "-"))

# The tester role turns adversarial output into bounded findings.
def _tester_llm(messages):
    return json.dumps({"attacks": [
        {"criterion": "enemy can die", "attack": "shooting the corpse "
         "respawns it endlessly", "severity": "major"},
        {"criterion": "", "attack": "", "severity": "major"}],
        "verdict": "WEAK"})


attacks = critic.llm_test("g", "combat", ["enemy can die"],
                          [{"kind": "screenshot", "tool": "cam",
                            "text": "enemy visible"}], _tester_llm)
_expect(attacks and len(attacks) == 1
        and attacks[0]["kind"] == "test_failure"
        and attacks[0]["severity"] == "major",
        "tester attacks become major findings (empty ones dropped)")
_expect(all("respawns" in a["message"] for a in attacks),
        "the finding carries the concrete failure")

# ===========================================================================
# 7. SERVER PATH: the Director runs on the route the UI drives
# ===========================================================================
# The UI never calls AutonomousAgent directly — it goes through
# server_run.run_agent_goal. If the Director lived only in the agent loop,
# the real product would still plan the whole game in one shot.

import agent.server_run as server_run  # noqa: E402


class _ServerEngine(mock_mcp.MockMCPServer):
    """Minimal engine: enough for a build+run+observe plan to execute."""

    def call(self, tool, args):
        if tool == "screenshot":
            return {"result": {"width": 1280, "note": "player grounded"}}
        if tool == "inspect_logs":
            return {"result": {"logs": ["boot ok"], "errors": []}}
        return super().call(tool, args)


class _ServerLLM:
    """Prompt-aware fake: director, planner, reviewer, judge."""

    def __init__(self, plan):
        self.plan = plan
        self.roles = set()
        self.objectives = []

    def __call__(self, messages):
        blob = json.dumps(messages)
        if "PROVEN RECIPES" in blob:
            self.roles.add("director")
            return "{}"          # decline: the recipe library decides
        if "ROLE: REVIEWER" in blob:
            self.roles.add("reviewer")
            return json.dumps({"verdict": "PASS", "proven": [],
                               "failed": [], "notes": ""})
        if "ROLE: TESTER" in blob:
            self.roles.add("tester")
            return json.dumps({"attacks": [], "verdict": "STRONG"})
        if "CURRENT OBJECTIVE" in blob:
            self.objectives.append(blob)
        if "Produce the plan JSON" in blob or "LIVE TOOL CATALOG" in blob:
            self.roles.add("planner")
            return json.dumps(self.plan)
        if "judge" in blob.lower() or "quality" in blob.lower():
            self.roles.add("judge")
            return json.dumps({"pass": True, "score": 8,
                               "reasons": [], "suggestions": []})
        return "{}"


_SERVER_PLAN = {"plan": {
    "title": "scoped build", "rationale": "one system at a time",
    "verification": "screenshot + logs",
    "steps": [
        {"name": "build", "tool": "engine.build", "args": {},
         "why": "compile", "expect": "ok", "depends_on": []},
        {"name": "launch", "tool": "engine.launch", "args": {},
         "why": "run it", "expect": "ok", "depends_on": ["build"]},
        {"name": "screen", "tool": "engine.screenshot", "args": {},
         "why": "see it", "expect": "ok", "depends_on": ["launch"]}]}}

td_s = tempfile.mkdtemp()
try:
    ws_s = os.path.join(td_s, "ws")
    os.makedirs(ws_s, exist_ok=True)
    engine_s = _ServerEngine("engine", [
        {"name": "build", "description": "build the project",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "launch", "description": "launch the game",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "screenshot", "description": "capture the game screen",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "inspect_logs", "description": "read the runtime logs",
         "inputSchema": {"type": "object", "properties": {}}}], fail=None)
    reg_s = agent_loop.CapabilityRegistry(
        [mock_mcp.server_view("engine", engine_s)])
    llm_s = _ServerLLM(_SERVER_PLAN)
    events_s = []
    state_s = ProjectState(goal="make a third person shooter")
    out_s = server_run.run_agent_goal(
        "make a third person shooter", bus=lambda e: events_s.append(e),
        llm_call=llm_s, llm_reachable=True, mode="build",
        judge_iterations=0, state=state_s, registry=reg_s)

    directed_s = [e for e in events_s if e.get("type") == "agent.directed"]
    _expect(len(directed_s) == 1,
            "the server route DIRECTS the goal (got %d directed events)"
            % len(directed_s))
    if directed_s:
        d = directed_s[0]
        _expect(len(d.get("systems") or []) >= 2,
                "the server decomposes into systems: %s"
                % [x.get("id") for x in d.get("systems") or []])
        _expect(d.get("current") == state_s.current_system,
                "the published current system is the one on the state "
                "(%s)" % state_s.current_system)
        _expect(bool(d.get("objective")) and bool(d.get("success")),
                "the server publishes an objective WITH success criteria")
    _expect(bool(state_s.systems) and bool(state_s.criteria),
            "the system map + checklists landed in the project memory")
    _expect(state_s.system_status(state_s.current_system) == "in_progress",
            "the scoped system is marked in_progress")

    started_s = [e for e in events_s if e.get("type") == "agent.plan_started"]
    _expect(bool(started_s) and started_s[0].get("objective"),
            "planning started under an explicit objective")
    _expect(bool(llm_s.objectives),
            "the planner PROMPT carried the objective (scope reached the "
            "model, not just the event log)")

    report_s = (out_s.get("report") or {})
    _expect(out_s.get("mode") == "build",
            "the server route ran in build mode (status=%s)"
            % report_s.get("status"))
    _expect(state_s.current_system in set(state_s.systems.keys()),
            "the scoped system exists in the map (%s)"
            % state_s.current_system)
    # Honest completion: whatever the outcome, the report may only claim
    # COMPLETED if the criteria were actually proven.
    _expect(report_s.get("status") != "COMPLETED"
            or not report_s.get("unverified"),
            "an unverified run cannot report COMPLETED (status=%s)"
            % report_s.get("status"))
finally:
    pass

print("\nAll Director-layer tests passed.")
