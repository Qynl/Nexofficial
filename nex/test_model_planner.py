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
    _expect("tool-repair agent" in repair_prompts[0][0]["content"],
            "repair prompt is a tool-repair prompt")
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
    print("\nAll model-planner + judge tests passed.")
