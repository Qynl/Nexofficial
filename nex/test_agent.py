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
# This section is about the DETERMINISTIC SKELETON's dependency handling
# (a failing leaf skips its dependents), so the graph is built explicitly:
# without a graph the run plans from the recipe library, which targets the
# system's own steps and would never call `create_animation` at all.
registry2, _ = _build_registry(
    animation_fail={"count": 99, "error": "missing required argument: 'rig'"})
_skeleton_agent = agent_loop.AutonomousAgent(registry2)
_skeleton_graph = _skeleton_agent.planner("Make a game.", registry2)
report2 = _skeleton_agent.run("Make a game.", graph=_skeleton_graph)
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


# ---------------------------------------------------------------------------
# 7. Quality gate: an EMPTY result is rejected, never shipped as "success".
# ---------------------------------------------------------------------------
class _EmptyMock(mock_mcp.MockMCPServer):
    def call(self, tool, args):
        return {"result": {}}     # looks like success but carries no info


empty_mock = _EmptyMock("qa", [{"name": "make_thing", "description": "x",
                                "inputSchema": {}}])
reg_qa = agent_loop.CapabilityRegistry(
    [mock_mcp.server_view("qa", empty_mock)])
g_qa = task_graph.TaskGraph()
g_qa.add(task_graph.Task(id="qa", name="make", stage="make",
                          server="qa", tool="make_thing", args={}))
rep_qa = agent_loop.AutonomousAgent(reg_qa).run("make a thing", graph=g_qa)
_expect("make" in rep_qa.failed,
        "empty result is treated as failure, not fake success: %s" % rep_qa.failed)
_expect(g_qa.get("qa").result is None or _is_empty(g_qa.get("qa").result),
        "no low-quality/empty result accepted")


def _is_empty(r):
    return r is None or (isinstance(r, dict) and len(r) == 0)


# ---------------------------------------------------------------------------
# 8. Multi-pass recovery: a failed task that can succeed on a later attempt
#    is revived and retried rather than left BLOCKED after one pass.
# ---------------------------------------------------------------------------
rec_mock = mock_mcp.MockMCPServer(
    "qa2", [{"name": "make_thing", "description": "x", "inputSchema": {}}],
    # Fails the first 3 calls globally, then succeeds (simulates a flaky /
    # eventually-ready upstream). max_attempts(3) exhausts within pass 1,
    # so only the recovery pass can get it through.
    fail={"make_thing": {"count": 3, "error": "transient blip"}})
reg_rec = agent_loop.CapabilityRegistry([mock_mcp.server_view("qa2", rec_mock)])
g_rec = task_graph.TaskGraph()
g_rec.add(task_graph.Task(id="rec", name="make", stage="make",
                          server="qa2", tool="make_thing", args={}))
rep_rec = agent_loop.AutonomousAgent(reg_rec, recovery_passes=1).run(
    "make a thing", graph=g_rec)
_expect("make" in rep_rec.completed,
        "recovery pass revives + retries a flaky task: %s" % rep_rec.status)
_expect(g_rec.get("rec").result is not None, "recovered task produced a result")


# ---------------------------------------------------------------------------
# 9. Transparency: a failed/skipped task is reported in `missing` so the user
#    knows exactly which capability/engine to connect (no silent partial).
# ---------------------------------------------------------------------------
assert "missing" in report2.to_dict(), "report exposes missing-capability list"
assert any(m["task"] == "animation" for m in report2.missing), \
    "missing list names the failed animation stage: %s" % report2.missing


# ---------------------------------------------------------------------------
# 10. Honest block: a run with NO executable tasks is BLOCKED with a
#     concrete reason — never an empty PARTIAL that reads as "we did some
#     of it" (regression: fresh installs with no engine connected used to
#     return status=PARTIAL, completed=[], failed=[], missing=[]).
# ---------------------------------------------------------------------------
from agent.events import STATUS_BLOCKED  # noqa: E402

reg_none = agent_loop.CapabilityRegistry([])
rep_none = agent_loop.AutonomousAgent(reg_none, persist=False).run(
    "make a game", graph=task_graph.TaskGraph())
_expect(rep_none.status == STATUS_BLOCKED,
        "empty registry -> status BLOCKED (was: empty PARTIAL): %s"
        % rep_none.status)
_expect(bool(rep_none.reasons) and "connect" in
        " ".join(rep_none.reasons).lower(),
        "blocked reason tells the user to connect an MCP server: %s"
        % rep_none.reasons)
_expect(bool(rep_none.missing),
        "blocked run fills the missing-capability list (transparency)")


# Also BLOCKED when the registry has servers but the plan validated down
# to zero tasks (e.g. a goal that matches no live tool).
reg_matchless = agent_loop.CapabilityRegistry(
    [mock_mcp.server_view("qa3", mock_mcp.MockMCPServer(
        "qa3", [{"name": "unrelated_tool", "description": "x",
                 "inputSchema": {}}]))])
rep_matchless = agent_loop.AutonomousAgent(reg_matchless,
                                           persist=False).run(
    "make a game", graph=task_graph.TaskGraph())
_expect(rep_matchless.status == STATUS_BLOCKED,
        "servers but no executable tasks -> BLOCKED: %s"
        % rep_matchless.status)
_expect("no executable tasks" in " ".join(rep_matchless.reasons),
        "reason distinguishes 'nothing connected' from 'plan matched "
        "nothing'")


# ---------------------------------------------------------------------------
# 11. Robustness: a dangling dependency id (corrupt checkpoint) counts as
#     unmet instead of raising KeyError and crashing the execution loop.
# ---------------------------------------------------------------------------
g_dangle = task_graph.TaskGraph()
t = task_graph.Task(id="x", name="x", stage="s", deps=["ghost_dep"])
g_dangle.add(t)
_expect(g_dangle.deps_met(t) is False,
        "dangling dep id -> deps_met False (was: KeyError)")
_expect(g_dangle.ready() == [],
        "dangling dep id -> task not ready (no crash)")


# ---------------------------------------------------------------------------
# QUALITY BARS have a path through the system (glass, not decoration)
# ---------------------------------------------------------------------------
# state -> task graph -> serialisation -> tester. If any hop drops them the
# builder and the tester never learn the standard, which is the whole point.
import agent.director as director_q  # noqa: E402
import agent.recipes as recipes_q  # noqa: E402
import agent.server_run as server_run_q  # noqa: E402

_gp_q = director_q.direct("Make a third person shooter")
_state_q = project_state.ProjectState()
for _s in _gp_q.systems:
    _state_q.set_criteria(_s.id, _s.checklist)
    _state_q.set_quality(_s.id, _s.quality)
_first_q = _gp_q.systems[0]
_expect(_state_q.quality_for(_first_q.id) == _first_q.quality,
        "the project state remembers the quality bar of a system")
_expect(_state_q.quality_for("NOT A SYSTEM") == []
        and _state_q.quality_for("") == [],
        "unknown systems have no quality bars (no accidental inheritance)")

_tg_q = task_graph.TaskGraph()
_tg_q.add(task_graph.Task(id="t1", name="build", stage="build",
                          system=_first_q.id, criteria=["player moves"]))
_scope_q = {"system": _first_q.id, "success": ["player moves"],
            "quality": list(_first_q.quality)}
server_run_q._attach_scope(_tg_q, _scope_q)
_expect(_tg_q.get("t1").quality == _first_q.quality,
        "the scope envelope hands the quality bar to the task")
_round = task_graph.TaskGraph.from_dict(_tg_q.to_dict())
_expect(_round.get("t1").quality == _first_q.quality,
        "quality bars survive graph serialisation (save / resume)")

# Memory stays bounded: the bar map is capped like every other map.
_state_big = project_state.ProjectState()
for i in range(60):
    _state_big.set_quality("sys_%d" % i, ["bar %d" % i])
_state_big.compact_memory()
_expect(len(_state_big.quality) <= 48,
        "the quality map is capped with the rest of the memory: %d"
        % len(_state_big.quality))
_expect(all(len(v) <= 6 for v in _state_big.quality.values()),
        "a single system never stores an unbounded number of bars")

# An unknown system id never crashes the library lookup.
_expect(recipes_q.quality_for("made_up") == recipes_q.GENERIC_QUALITY_BARS,
        "the library answers with generic bars for an unknown system")

# ---------------------------------------------------------------------------
# Laundering check: "write the payload to a file, then run the file"
# Regression for the scan_file_payload NameError (missing `import os` in
# mcp/policy.py) that silently disabled this whole check, and for the
# fail-closed behaviour when the scan itself raises.
# ---------------------------------------------------------------------------
import tempfile as _tempfile  # noqa: E402
import mcp.policy as _policy  # noqa: E402
from mcp.policy import Policy as _Policy  # noqa: E402

_launder_tools = [
    {"name": "run_python", "description": "run a python file",
     "inputSchema": {"type": "object",
                     "properties": {"file": {"type": "string"}}}},
]
_launder_mock = mock_mcp.MockMCPServer("exec_mcp", _launder_tools)
_launder_reg = agent_loop.CapabilityRegistry([
    mock_mcp.server_view("exec_mcp", _launder_mock),
])
_dirty = _tempfile.mkdtemp(prefix="nex-launder-")
_dirty_file = os.path.join(_dirty, "payload.py")
_launder_policy = _Policy(strict_servers=False,
                          allow_code_execution={"exec_mcp"})
_launder_agent = agent_loop.AutonomousAgent(_launder_reg,
                                            policy=_launder_policy,
                                            persist=False,
                                            workspace_root=_dirty)
with open(_dirty_file, "w", encoding="utf-8") as _f:
    _f.write("import os\nos.system('id')\n")

# 1) The scan itself works (it used to raise NameError: name 'os' is not
#    defined — the missing import made every laundering check a no-op).
_leak = _policy.scan_file_payload(_dirty_file, _dirty)
_expect(_leak is not None and "os.system" in _leak,
        "scan_file_payload detects shell execution in a file: %s"
        % (_leak or "None (NameError regression!)")[:70])
_expect(_policy.scan_file_payload("does_not_exist.py", _dirty) is None,
        "scan_file_payload: missing file is not a refusal")

# 2) A code-execution task that names a dirty file is BLOCKED.
_tg_l = task_graph.TaskGraph()
_task_l = task_graph.Task(id="l1", name="run payload", stage="build",
                          server="exec_mcp", tool="run_python",
                          args={"file": _dirty_file})
_tg_l.add(_task_l)
_launder_agent._execute_task(_task_l, project_state.ProjectState(),
                             _tg_l)
_expect(_task_l.status == task_graph.FAILED
        and _task_l.error is not None
        and "os.system" in str(_task_l.error),
        "laundered payload (write file, run file) is BLOCKED: %s"
        % str(_task_l.error)[:70])

# 3) If the scan itself blows up, the call is denied fail-closed (an old
#    version swallowed the exception and let the call through).
_real_scan = _policy.scan_file_payload
def _broken_scan(*_a, **_kw):
    raise NameError("name 'os' is not defined")
_policy.scan_file_payload = _broken_scan
try:
    _task_b = task_graph.Task(id="l2", name="run payload 2", stage="build",
                              server="exec_mcp", tool="run_python",
                              args={"file": _dirty_file})
    _tg_b = task_graph.TaskGraph()
    _tg_b.add(_task_b)
    _launder_agent._execute_task(_task_b, project_state.ProjectState(),
                                 _tg_b)
finally:
    _policy.scan_file_payload = _real_scan
_expect(_task_b.status == task_graph.FAILED
        and "fail-closed" in str(_task_b.error),
        "broken payload scan denies the call fail-closed: %s"
        % str(_task_b.error)[:80])

print("\nAll agent tests passed.")
