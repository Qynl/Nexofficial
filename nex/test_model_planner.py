"""Tests for model-driven planning + judges (no Ollama required).

A fake `llm(messages) -> str` stands in for the model server. The planner
must turn a goal-specific plan into a validated TaskGraph, drop hallucinated
tools, and fall back to the skeleton on garbage. Judges must produce a
verdict with scores + suggestions.
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent.mock_mcp as mock_mcp
import agent.loop as agent_loop
from agent.registry import CapabilityRegistry
from agent.events import STATUS_COMPLETED, STATUS_PARTIAL


def _registry():
    asset_tools = [
        {"name": "create_asset", "description": "x", "inputSchema": {}},
        {"name": "import_asset", "description": "x", "inputSchema": {}},
        {"name": "verify_asset", "description": "x", "inputSchema": {}},
    ]
    game_tools = [
        {"name": "create_project", "description": "x", "inputSchema": {}},
        {"name": "create_animation", "description": "x", "inputSchema": {}},
        {"name": "create_script", "description": "x", "inputSchema": {}},
        {"name": "build", "description": "x", "inputSchema": {}},
        {"name": "run_game", "description": "x", "inputSchema": {}},
        {"name": "inspect_logs", "description": "x", "inputSchema": {}},
        {"name": "verify_game", "description": "x", "inputSchema": {}},
    ]
    asset = mock_mcp.MockMCPServer("asset_mcp", asset_tools)
    game = mock_mcp.MockMCPServer("game_mcp", game_tools)
    return CapabilityRegistry([
        mock_mcp.server_view("asset_mcp", asset),
        mock_mcp.server_view("game_mcp", game),
    ])


def _expect(cond, msg):
    if not cond:
        print("FAIL -", msg)
        sys.exit(1)
    print("ok   -", msg)


GOOD_PLAN = {
    "title": "demo",
    "rationale": "demo",
    "verification": "demo",
    "steps": [
        {"name": "asset", "tool": "create_asset", "args": {},
         "why": "a", "expect": "a"},
        {"name": "anim", "tool": "create_animation", "args": {},
         "why": "a", "expect": "a", "depends_on": ["asset"]},
        {"name": "build", "tool": "build", "args": {},
         "why": "a", "expect": "a", "depends_on": ["anim"]},
    ],
}


def _fake_llm(messages):
    # Echo: if the last user message mentions "bad", return garbage.
    last = messages[-1]["content"]
    if "garbage" in last:
        return "I am not a plan."
    if "hallucinate" in last:
        return json.dumps({"plan": {"title": "x", "rationale": "",
                                   "verification": "",
                                   "steps": [
                                       {"name": "a", "tool": "create_asset",
                                        "args": {}, "why": "", "expect": ""},
                                       {"name": "b", "tool": "bogus_tool_xyz",
                                        "args": {}, "why": "", "expect": "",
                                        "depends_on": ["a"]},
                                   ]}})
    return json.dumps({"plan": GOOD_PLAN})


def test_model_planner_basic():
    from agent.model_planner import model_driven_planner
    reg = _registry()
    graph, plan = model_driven_planner("build a game", reg, _fake_llm)
    _expect(plan is not None, "model plan parsed")
    tasks = {t.name: t for t in graph.all()}
    _expect(len(tasks) == 3, "three valid tasks created")
    _expect("asset" in tasks and "anim" in tasks and "build" in tasks,
            "expected task names present")
    # dependency wiring
    _expect(tasks["anim"].deps == [tasks["asset"].id], "anim depends on asset")
    _expect(tasks["build"].deps == [tasks["anim"].id], "build depends on anim")
    # servers resolved from registry
    _expect(tasks["asset"].server == "asset_mcp", "asset tool resolved to asset_mcp")
    _expect(tasks["build"].server == "game_mcp", "build tool resolved to game_mcp")


def test_model_planner_drops_hallucinated():
    from agent.model_planner import model_driven_planner
    reg = _registry()
    graph, plan = model_driven_planner(
        "build a game that hallucinates", reg, _fake_llm)
    # The hallucinated tool is dropped; the real one is kept.
    names = [t.name for t in graph.all()]
    _expect("a" in names and "b" not in names,
            "hallucinated tool dropped, real tool kept: %s" % names)
    _expect(plan is not None, "partially valid plan still used")


def test_model_planner_fallback_on_garbage():
    from agent.model_planner import model_driven_planner
    reg = _registry()
    graph, plan = model_driven_planner(
        "build a game that is garbage", reg, _fake_llm)
    _expect(plan is None, "garbage reply -> fallback (plan None)")
    _expect(len(graph.all()) > 0, "fallback skeleton produced tasks")


def test_judges_rule():
    from agent.judges import judge
    from agent.task_graph import TaskGraph, Task
    reg = _registry()
    g = TaskGraph()
    g.add(Task(id="1", name="core", stage="script", server="game_mcp",
               tool="create_script", args={}))
    g.add(Task(id="2", name="art", stage="create", server="asset_mcp",
               tool="create_asset", args={}, deps=["1"]))
    g.mark_success("1", {"id": "s"})
    g.mark_success("2", {"id": "a"})
    # Build a minimal report-like object.
    class R:
        status = STATUS_COMPLETED
        completed = ["core", "art"]
        failed = []
        skipped = []
        missing = []
    verdict = judge("make a fun赛车 game", R(), GOOD_PLAN, llm=None)
    _expect("score" in verdict and verdict["score"] >= 0.0, "verdict has score")
    _expect("suggestions" in verdict, "verdict has suggestions")
    _expect(isinstance(verdict.get("dimensions"), dict), "verdict has dimensions")


def test_judges_with_llm():
    from agent.judges import judge

    def llm(messages):
        return json.dumps({"fun": 9, "quality": 8, "playability": 7,
                           "suggestions": ["add juice", "add audio"]})

    class R:
        status = STATUS_PARTIAL
        completed = ["core"]
        failed = ["art"]
        skipped = []
        missing = [{"task": "art", "server": "asset_mcp",
                    "tool": "create_asset", "reason": "unavailable"}]
    verdict = judge("make a game", R(), GOOD_PLAN, llm=llm)
    _expect(verdict["judges"].get("llm") is not None, "LLM judge ran")
    _expect(verdict["judges"]["llm"]["fun"] == 9, "LLM fun score parsed")
    _expect(len(verdict["suggestions"]) >= 1, "suggestions merged")


# ---------------------------------------------------------------------------
# Unification: ONE planning + execution pipeline
# ---------------------------------------------------------------------------

def test_validate_plan_deep():
    from agent.model_planner import validate_plan_deep
    reg = _registry()
    good = {"steps": [{"name": "a", "tool": "create_asset", "args": {}}]}
    errs, warns = validate_plan_deep(good, reg)
    _expect(errs == [], "good plan has no errors, got %r" % errs)

    missing = {"steps": [{"name": "a", "tool": "no_such_tool", "args": {}}]}
    errs, _ = validate_plan_deep(missing, reg)
    _expect(len(errs) == 1 and "not found" in errs[0], "unknown tool -> error")

    shell_tools = [{"name": "run_command", "description": "x",
                    "inputSchema": {}}]
    shell = CapabilityRegistry([mock_mcp.server_view(
        "shell_mcp", mock_mcp.MockMCPServer("shell_mcp", shell_tools))])
    denied = {"steps": [{"name": "sh", "tool": "run_command",
                         "args": {"cmd": "ls"}}]}
    errs, _ = validate_plan_deep(denied, shell)
    _expect(len(errs) == 1 and "denied by policy" in errs[0],
            "policy-denied tool -> error, got %r" % errs)

    # Required-arg enforcement from the LIVE schema.
    strict_tools = [{"name": "save_thing", "description": "x",
                     "inputSchema": {"type": "object",
                                     "required": ["path"],
                                     "properties": {"path": {"type": "string"}}}}]
    strict = CapabilityRegistry([mock_mcp.server_view(
        "strict_mcp", mock_mcp.MockMCPServer("strict_mcp", strict_tools))])
    bad_args = {"steps": [{"name": "s", "tool": "save_thing", "args": {}}]}
    errs, _ = validate_plan_deep(bad_args, strict)
    _expect(len(errs) == 1 and "missing required arg 'path'" in errs[0],
            "missing required arg -> error, got %r" % errs)
    unknown = {"steps": [{"name": "s", "tool": "save_thing",
                          "args": {"path": "x", "bogus": 1}}]}
    errs, warns = validate_plan_deep(unknown, strict)
    _expect(errs == [] and len(warns) == 1 and "not in live schema" in warns[0],
            "unknown arg -> warning only")


def test_agent_uses_llm_planner_canonically():
    """With an llm attached, run() plans via the model, not the skeleton."""
    import json as _json
    reg = _registry()
    llm_plan = {"plan": {"title": "LLM plan", "rationale": "r",
                         "verification": "v",
                         "steps": [
                             {"name": "llm_project", "tool": "create_project",
                              "args": {"name": "llmgame"}, "why": "w",
                              "expect": "e"},
                             {"name": "llm_asset", "tool": "create_asset",
                              "args": {"type": "mesh"}, "why": "w",
                              "expect": "e", "depends_on": ["llm_project"]},
                         ]}}

    def plan_llm(messages):
        return _json.dumps(llm_plan)

    agent = agent_loop.AutonomousAgent(reg, llm=plan_llm)
    report = agent.run("make the llm thing")
    names = [t["name"] for t in report.to_dict().get("tasks", [])] \
        if hasattr(report, "to_dict") else []
    _expect(agent.last_plan is not None,
            "canonical planning used the model (last_plan set)")
    _expect(report.status == STATUS_COMPLETED,
            "LLM-planned graph executes to completion, got %r" % report.status)


def test_llm_repair_used_when_regex_fails():
    """A failure that matches no regex falls through to genuine LLM repair,
    bounded to one attempt per task."""
    import json as _json

    class RepairMock(mock_mcp.MockMCPServer):
        def call(self, tool, args):
            if tool == "flaky" and not args.get("fixed"):
                return {"error": "quantum flux misalignment"}  # no regex hit
            return {"result": {"id": "ok", "fixed": bool(args.get("fixed"))}}

    tools = [{"name": "flaky", "description": "x", "inputSchema": {}}]
    reg = CapabilityRegistry([mock_mcp.server_view(
        "repair_mcp", RepairMock("repair_mcp", tools))])

    repair_prompts = []

    def repair_llm(messages):
        repair_prompts.append(list(messages))
        return _json.dumps({"args": {"fixed": True}})

    from agent.task_graph import TaskGraph, Task
    g = TaskGraph()
    g.add(Task(id="t1", name="flaky_step", stage="asset",
               server="repair_mcp", tool="flaky", args={}))
    agent = agent_loop.AutonomousAgent(reg, llm=repair_llm)
    report = agent.run("repair me", graph=g)
    _expect(report.status == STATUS_COMPLETED,
            "LLM repair recovers the task, got %r" % report.status)
    _expect(len(repair_prompts) == 1,
            "LLM repair bounded to one attempt, ran %d" % len(repair_prompts))
    _expect("failure-diagnosis agent" in repair_prompts[0][0]["content"],
            "repair prompt is a diagnosis prompt")
    # llm_diagnosed flag recorded on the task
    _expect(g.get("t1").llm_diagnosed is True, "llm_diagnosed flag recorded")
    _expect(g.get("t1").args.get("fixed") is True, "corrected args applied")


def test_reconcile_on_resume():
    """Resume reconciles the checkpoint against live reality."""
    import os, tempfile
    from agent.project_state import ProjectState
    from agent.checkpoints import save_checkpoint, reconcile_on_resume

    reg = _registry()
    from agent.task_graph import TaskGraph, Task, PENDING, FAILED, SUCCESS
    g = TaskGraph()
    g.add(Task(id="done", name="done_step", stage="asset",
               server="asset_mcp", tool="create_asset", args={},
               verify_tool="verify_asset"))
    g.add(Task(id="todo", name="todo_step", stage="asset",
               server="asset_mcp", tool="ghost_tool", args={}))
    g.mark_success("done", {"id": "asset_1"})

    state = ProjectState(goal="resume me")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "ckpt.json")
        save_checkpoint(path, "resume me", state, g)

        # 1. Availability-only reconcile: ghost_tool vanished -> FAILED.
        out = reconcile_on_resume(path, reg)
        _expect(out is not None, "checkpoint loads")
        _, s2, g2, rep = out
        _expect("ghost_tool" in rep["missing_tools"], "vanished tool reported")
        _expect(g2.get("todo").status == FAILED,
                "pending task with vanished tool marked FAILED")
        _expect(g2.get("done").status == SUCCESS,
                "completed task untouched without call_tool")

        # 2. Reconcile with re-verification against a registry whose
        #    verifier now errors (artifact really gone).
        class BrokenVerifyMock(mock_mcp.MockMCPServer):
            def call(self, tool, args):
                if tool == "verify_asset":
                    return {"error": "artifact no longer exists"}
                return super().call(tool, args)
        broken_reg = CapabilityRegistry([mock_mcp.server_view(
            "asset_mcp", BrokenVerifyMock("asset_mcp", [
                {"name": "create_asset", "description": "x", "inputSchema": {}},
                {"name": "import_asset", "description": "x", "inputSchema": {}},
                {"name": "verify_asset", "description": "x", "inputSchema": {}},
            ]))])
        _, _, g3, rep3 = reconcile_on_resume(path, broken_reg)
        _expect(rep3["reverified"] == 1, "completed task reverified")
        _expect(g3.get("done").status == PENDING,
                "failed re-verify demotes completed task to pending/redo")


def test_diagnose_decisions():
    """Structured LLM failure diagnosis: parse + registry-validate."""
    from agent.diagnose import parse_decision, apply_decision, diagnose
    reg = _registry()

    # Minimal legacy protocol still works.
    d = parse_decision('{"args": {"path": "x"}}')
    _expect(d and d["action"] == "correct_args", "legacy args-only parsed")
    out = apply_decision(d, type("T", (), {"args": {}, "tool": "create_asset"})(), reg)
    _expect(out and out["kind"] == "correct_args", "legacy decision applied")

    # Structured: switch_tool must exist in the live registry.
    d2 = parse_decision('{"action": "switch_tool", "tool": "import_asset", "reason": "create failed"}')
    t = type("T", (), {"args": {}, "tool": "create_asset"})()
    out2 = apply_decision(d2, t, reg)
    _expect(out2 and out2["kind"] == "switch_tool" and out2["tool"] == "import_asset",
            "switch_tool to a live tool accepted")

    # Hallucinated escape hatch rejected.
    d3 = parse_decision('{"action": "switch_tool", "tool": "magic_fix_all"}')
    _expect(apply_decision(d3, t, reg) is None,
            "switch_tool to a hallucinated tool rejected")

    # give_up passes through with its reason.
    d4 = parse_decision('{"action": "give_up", "reason": "editor offline"}')
    out4 = apply_decision(d4, t, reg)
    _expect(out4 and out4["kind"] == "give_up" and out4["reason"] == "editor offline",
            "give_up carries the model's reason")

    # Garbage -> None.
    _expect(parse_decision("I am not json") is None, "garbage reply rejected")

    # End-to-end bounded diagnose() with a fake llm.
    calls = []
    def llm(messages):
        calls.append(messages)
        return '{"action": "correct_args", "args": {"name": "renamed"}}'
    task = type("T", (), {"args": {}, "tool": "create_asset",
                          "server": "asset_mcp", "name": "t"})()
    dec = diagnose(task, "missing name", reg, llm)
    _expect(dec and dec["args"] == {"name": "renamed"}, "diagnose end-to-end")
    _expect(diagnose(task, "x", reg, lambda m: "garbage") is None,
            "garbage llm -> None")


def test_llm_switch_tool_recovery():
    """When args can't be fixed, the model can switch to a live tool."""
    import json as _json

    class StrictMock(mock_mcp.MockMCPServer):
        def call(self, tool, args):
            if tool == "create_animation" and "rig" not in (args or {}):
                return {"error": "absolute requirement missing"}
            return {"result": {"id": "ok_%s" % tool}}

    tools = [
        {"name": "create_animation", "description": "needs rig",
         "inputSchema": {}},
        {"name": "import_animation", "description": "import instead",
         "inputSchema": {}},
    ]
    reg = CapabilityRegistry([mock_mcp.server_view(
        "anim_mcp", StrictMock("anim_mcp", tools))])

    def llm(messages):
        return _json.dumps({"action": "switch_tool",
                            "tool": "import_animation",
                            "reason": "creation unsupported, import instead"})

    from agent.task_graph import TaskGraph, Task
    g = TaskGraph()
    g.add(Task(id="a1", name="anim", stage="animation",
               server="anim_mcp", tool="create_animation", args={}))
    agent = agent_loop.AutonomousAgent(reg, llm=llm)
    report = agent.run("animate", graph=g)
    _expect(report.status == STATUS_COMPLETED,
            "switch_tool recovers, got %r" % report.status)
    t = g.get("a1")
    _expect(t.tool == "import_animation",
            "task now uses the switched tool, got %r" % t.tool)
    _expect(t.llm_diagnosed is True, "diagnosis bounded to one shot")


def test_verification_expect_criteria():
    """Rich acceptance criteria gate results before success is claimed."""
    from agent.verification import check_expect, _is_empty_result
    _expect(_is_empty_result([]) is True, "empty list is trivial")
    _expect(_is_empty_result([{}, ""]) is True, "list of empties is trivial")
    _expect(_is_empty_result({"result": None}) is True, "envelope-of-nothing")
    _expect(_is_empty_result({"ok": True, "id": "a1"}) is False,
            "payload-bearing dict is non-trivial")

    ok, _ = check_expect({"id": "a1", "name": "x"},
                         {"keys": ["id", "name"]})
    _expect(ok, "keys criteria pass")
    ok, note = check_expect({"id": "a1"}, {"keys": ["id", "sprite"]})
    _expect(not ok and "sprite" in note, "missing key fails with note")
    ok, _ = check_expect("path/to/thing.png", {"contains": ".png"})
    _expect(ok, "contains criteria pass")
    ok, _ = check_expect(["a", "b", "c"], {"min_len": 3})
    _expect(ok, "min_len criteria pass")
    ok, _ = check_expect([1], {"min_len": 3})
    _expect(not ok, "min_len criteria fail")
    ok, _ = check_expect({"v": 1}, {"is": {"v": 1}})
    _expect(ok, "equals criteria pass")
    ok, _ = check_expect("", "a screenshot of the scene")
    _expect(not ok, "string expect still rejects trivial results")

    # verify_task honors expect before the verifier runs.
    from agent.verification import verify_task
    reg = _registry()

    class T:
        verify_tool = None
        expect = {"keys": ["id"]}
    res = verify_task(T(), reg, {"id": "a1"})
    _expect(res.ok, "expect met -> ok without verifier")
    res = verify_task(T(), reg, {"nope": 1})
    _expect(not res.ok, "expect unmet -> rejected even though non-empty")


def test_relevance_filtering():
    """Huge catalogs are relevance-filtered for the planning prompt."""
    reg = _registry()
    tools = [{"name": t, "description": "does %s things" % t.split("_")[0],
              "inputSchema": {}}
             for t in ["animate_walk", "bake_light", "build_mesh",
                       "paint_texture", "rig_bones", "render_frame",
                       "export_fbx", "import_fbx"]]
    tools += [{"name": "filler_%d" % i, "description": "misc",
               "inputSchema": {}} for i in range(60)]
    big = CapabilityRegistry([mock_mcp.server_view(
        "big_mcp", mock_mcp.MockMCPServer("big_mcp", tools))])
    sel, omitted = big.relevant_tools(
        "animate a walk cycle for the character", limit=10)
    names = [t.name for t in sel]
    _expect(omitted == 58, "58 tools omitted, got %d" % omitted)
    _expect(names[0] == "animate_walk",
            "most relevant tool ranked first, got %s" % names[0])
    # Small catalogs are returned unfiltered.
    sel2, omitted2 = reg.relevant_tools("anything", limit=40)
    _expect(omitted2 == 0, "small catalog unfiltered")
    # Planner prompt uses the filtered catalog with an honest trailer.
    from agent.model_planner import _catalog
    cat = _catalog(big, goal="animate a walk cycle", max_catalog=10)
    _expect("(+58 more tools available" in cat, "catalog trailer present")
    _expect("animate_walk" in cat, "relevant tool present in catalog")


def test_campaign_runs_milestones_and_resumes():
    """Campaign: LLM roadmap -> per-milestone canonical runs -> checkpoint
    -> resume skips completed milestones."""
    import agent.campaign as campaign
    from agent.events import STATUS_COMPLETED

    roadmap_llm = json.dumps({"milestones": [
        {"title": "m1 blocks", "goal": "make blockout",
         "done_when": "a project exists"},
        {"title": "m2 animate", "goal": "add animation",
         "done_when": "an animation exists"},
    ]})

    def roadmap_only_llm(messages):
        return roadmap_llm

    events = []
    ran_goals = []

    def fake_run_agent_goal(mgoal, bus=None, llm_call=None,
                            llm_reachable=False, mode="build", **kw):
        ran_goals.append(mgoal)
        if bus:
            bus({"type": "agent.plan_ready", "goal": mgoal})
        return {"mode": "build", "report": {"status": STATUS_COMPLETED,
                                            "missing": []}}

    orig = campaign.run_campaign  # keep
    import agent.server_run as sr
    patched = sr.run_agent_goal
    sr.run_agent_goal = fake_run_agent_goal
    try:
        camp_file = campaign.CHECKPOINT_FILE
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            campaign.CHECKPOINT_FILE = os.path.join(td, "camp.json")
            summary = campaign.run_campaign(
                "make a tiny game", bus=events.append,
                llm_call=roadmap_only_llm, llm_reachable=True)
            _expect(summary["milestones_total"] == 2, "2 milestones planned")
            _expect(summary["status"] == "COMPLETED", "both completed")
            _expect(len(ran_goals) == 2, "each milestone ran the pipeline")
            _expect(any(e["type"] == "agent.campaign_started" for e in events),
                    "campaign_started emitted")
            _expect(sum(1 for e in events
                        if e["type"] == "agent.milestone_completed") == 2,
                    "milestone_completed emitted twice")

            # Resume: fresh call, same goal, milestone results loaded from
            # the checkpoint -> nothing re-runs.
            ran_goals.clear()
            events.clear()
            summary2 = campaign.run_campaign(
                "make a tiny game", bus=events.append,
                llm_call=roadmap_only_llm, llm_reachable=True, resume=True)
            _expect(len(ran_goals) == 0, "resume re-runs nothing")
            _expect(summary2["status"] == "COMPLETED", "resume still complete")
            _expect(any(e.get("resumed") for e in events
                        if e["type"] == "agent.milestone_completed"),
                    "resumed milestones flagged")
    finally:
        sr.run_agent_goal = patched
        campaign.run_campaign = orig


if __name__ == "__main__":
    test_model_planner_basic()
    test_model_planner_drops_hallucinated()
    test_model_planner_fallback_on_garbage()
    test_judges_rule()
    test_judges_with_llm()
    test_validate_plan_deep()
    test_agent_uses_llm_planner_canonically()
    test_llm_repair_used_when_regex_fails()
    test_reconcile_on_resume()
    test_diagnose_decisions()
    test_llm_switch_tool_recovery()
    test_verification_expect_criteria()
    test_relevance_filtering()
    test_campaign_runs_milestones_and_resumes()
    print("\nAll model-planner + judge tests passed.")
