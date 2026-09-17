"""Tests for the autonomous agent layer (STAGE 6/9/13/14/24/25).

Covers: dependency-aware task graph, project state, checkpoints, and a full
mocked autonomous run where one operation fails and is repaired. No real
Unreal/Blender/Roblox required.
"""

import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

task_graph = importlib.import_module("agent.task_graph")
project_state = importlib.import_module("agent.project_state")
checkpoints = importlib.import_module("agent.checkpoints")
agent_loop = importlib.import_module("agent.loop")
mock_mcp = importlib.import_module("agent.mock_mcp")
capability = importlib.import_module("mcp.capability")


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


def _build_registry(animation_fail=None):
    asset_tools = [
        {"name": "create_asset", "description": "create an asset",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "import_asset", "description": "import an asset",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "verify_asset", "description": "verify an asset",
         "inputSchema": {"type": "object", "properties": {}}},
    ]
    game_tools = [
        {"name": "create_project", "description": "create project",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "create_animation", "description": "create animation",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "create_script", "description": "create script",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "build", "description": "build the game",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "run_game", "description": "run the game",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "inspect_logs", "description": "inspect logs",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "verify_game", "description": "verify game",
         "inputSchema": {"type": "object", "properties": {}}},
    ]
    asset_mock = mock_mcp.MockMCPServer("asset_mcp", asset_tools)
    game_mock = mock_mcp.MockMCPServer("game_mcp", game_tools,
                                       fail={"create_animation": animation_fail}
                                       if animation_fail else None)
    servers = [
        mock_mcp.server_view("asset_mcp", asset_mock),
        mock_mcp.server_view("game_mcp", game_mock),
    ]
    registry = agent_loop.CapabilityRegistry(servers)
    return registry, game_mock


# ---------------------------------------------------------------------------
# 1. Task graph: dependency readiness + failure propagation
# ---------------------------------------------------------------------------
g = task_graph.TaskGraph()
g.add(task_graph.Task(id="a", name="a", stage="asset", status="success"))
g.add(task_graph.Task(id="b", name="b", stage="import", deps=["a"]))
g.add(task_graph.Task(id="c", name="c", stage="build", deps=["b"]))
_expect([t.id for t in g.ready()] == ["b"], "only dependency-satisfied task is ready")
g.mark_failed("b", "boom")
_expect(g.get("c").status == task_graph.SKIPPED, "failed dep -> dependent skipped")
_expect(g.is_terminal(), "graph terminal after failure propagation")

# ---------------------------------------------------------------------------
# 2. Project state + checkpoints round-trip
# ---------------------------------------------------------------------------
st = project_state.ProjectState(goal="test")
st.record_success("a")
st.record_failure("b", "boom")
_expect(st.completed == ["a"], "project state records completion")
_expect(len(st.failed) == 1, "project state records failure")

import tempfile, json
tmp = tempfile.mkdtemp()
cp = os.path.join(tmp, "cp.json")
graph2 = task_graph.TaskGraph()
graph2.add(task_graph.Task(id="x", name="x", stage="asset", status="success",
                           result={"id": "1"}))
checkpoints.save_checkpoint(cp, "my goal", st, graph2)
loaded = checkpoints.load_checkpoint(cp)
_expect(loaded is not None, "checkpoint loads")
_goal, _st, _g, _extra = loaded
_expect(_goal == "my goal", "checkpoint preserves goal")
_expect(_g.get("x").result == {"id": "1"}, "checkpoint preserves task result")


# ---------------------------------------------------------------------------
# 3. Autonomous simulation: one failure, repaired, continued
# ---------------------------------------------------------------------------
registry, game_mock = _build_registry(
    animation_fail={"count": 1, "error": "missing required argument: 'rig'"})

# Snapshot the discovered capability set to prove the planner reacts to it.
tool_names = sorted(t.full_name for t in registry.all_tools())
_expect("game_mcp.create_animation" in tool_names, "planner sees discovered tools")

# The graph must be dependency-aware: build depends on import/asset/script/anim.
agent = agent_loop.AutonomousAgent(registry)
state = project_state.ProjectState(goal="Create a small playable game.")
graph = agent.planner("Create a small playable game.", registry)
_expect("stage_build" in [t.id for t in graph.all()], "graph has build stage")
build_task = graph.get("stage_build")
_expect(set(build_task.deps) >= {"stage_import", "stage_asset", "stage_script",
                                 "stage_animation"},
        "build depends on asset/import/script/animation (dependency-aware)")
run_task = graph.get("stage_run")
_expect(run_task.deps == ["stage_build"], "run depends on build")

# Run the loop for real against the mock (with injected animation failure).
# Pass the graph we built so we can inspect the executed instance afterward.
graph = agent.planner("Create a small playable game.", registry)
report = agent_loop.AutonomousAgent(registry).run("Create a small playable game.",
                                                graph=graph)
_expect(report.status == agent_loop.STATUS_COMPLETED,
        "simulation completes after repairing the animation failure: %s" % report.status)
_expect("animation" in report.completed, "animation task completed (repaired)")
anim_task = graph.get("stage_animation")
_expect(anim_task.attempts >= 2, "animation was retried after repair")
_expect(anim_task.result is not None, "animation produced a result after repair")
_expect(not report.failed, "no failed tasks in repaired run: %s" % report.failed)


# ---------------------------------------------------------------------------
# 4. Autonomous simulation: persistent failure -> dependency-aware skip
# ---------------------------------------------------------------------------
registry2, _ = _build_registry(
    animation_fail={"count": 99, "error": "missing required argument: 'rig'"})
report2 = agent_loop.AutonomousAgent(registry2).run("Make a game.")
_expect("animation" in report2.failed, "persistent failure recorded")
_expect(len(report2.skipped) > 0, "dependents skipped on hard failure")
_expect(report2.status in (agent_loop.STATUS_PARTIAL,
                            agent_loop.STATUS_BLOCKED),
        "partial/blocked status when a stage cannot be completed")


# ---------------------------------------------------------------------------
# 5. Recovery option C: switch to the SAME tool on another MCP server
#    (don't stop at the first server's failure).
# ---------------------------------------------------------------------------
mock_a = mock_mcp.MockMCPServer(
    "s1", [{"name": "create_animation", "description": "x", "inputSchema": {}}],
    fail={"create_animation": {"count": 99, "error": "studio 1 crashed"}})
mock_b = mock_mcp.MockMCPServer(
    "s2", [{"name": "create_animation", "description": "x", "inputSchema": {}}])
reg_alt = agent_loop.CapabilityRegistry([
    mock_mcp.server_view("s1", mock_a),
    mock_mcp.server_view("s2", mock_b),
])
g_alt = task_graph.TaskGraph()
g_alt.add(task_graph.Task(id="anim", name="animation", stage="animation",
                          server="s1", tool="create_animation", args={}))
rep_alt = agent_loop.AutonomousAgent(reg_alt).run("make anim", graph=g_alt)
_expect("animation" in rep_alt.completed,
        "agent recovers by switching to the same tool on another server: %s"
        % rep_alt.status)
_expect(g_alt.get("anim").result is not None,
        "switched server produced a result")


# ---------------------------------------------------------------------------
# 6. Unknown tool is classified conservatively in the registry view
# ---------------------------------------------------------------------------
unknown = registry.by_name("create_animation")
_expect(unknown.capability.source in ("heuristic", "conservative"),
        "discovered tool capability is classified")


print("\nAll agent tests passed.")
