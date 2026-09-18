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


if __name__ == "__main__":
    test_model_planner_basic()
    test_model_planner_drops_hallucinated()
    test_model_planner_fallback_on_garbage()
    test_judges_rule()
    test_judges_with_llm()
    print("\nAll model-planner + judge tests passed.")
