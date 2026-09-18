"""Architecture guard: ONE dependency direction.

    server -> agent -> mcp (policy/capability) -> upstreams

No reverse edges, no side doors:
  * mcp/*            is the policy/capability core — imports NOTHING from
                     agent/server/mc (it must stay independently testable
                     and authoritative).
  * agent/*          must not import server or mc. (agent.server_run is the
                     documented composition root — it MAY import tunnels.)
  * mc.py            is the prompt layer; it may re-export agent.plans but
                     the plan engine must live in agent/, never duplicate.

AST-based so it checks the SOURCE, not whatever happens to be imported at
runtime.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def _imports(path):
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def rel(p):
    return os.path.relpath(p, HERE)


# 1) mcp/* — pure core.
for root, _dirs, files in os.walk(os.path.join(HERE, "mcp")):
    for f in files:
        if not f.endswith(".py"):
            continue
        path = os.path.join(root, f)
        bad = _imports(path) & {"agent", "server", "mc", "mc_tools",
                                "tunnels", "tools"}
        if bad:
            failures.append("%s imports %s (mcp must stay the pure core)"
                            % (rel(path), sorted(bad)))

# 2) agent/* — no server, no mc (server_run may import tunnels as glue).
for root, _dirs, files in os.walk(os.path.join(HERE, "agent")):
    for f in files:
        if not f.endswith(".py"):
            continue
        path = os.path.join(root, f)
        mods = _imports(path)
        bad = mods & {"server", "mc", "mc_tools"}
        if bad:
            failures.append("%s imports %s (agent must not reach the HTTP "
                            "or prompt layer)" % (rel(path), sorted(bad)))
        if os.path.basename(path) != "server_run.py" and "tunnels" in mods:
            failures.append("%s imports tunnels (only agent.server_run, the "
                            "composition root, may)" % rel(path))

# 3) The plan engine lives in agent/, not mc.py.
mc_src = open(os.path.join(HERE, "mc.py"), encoding="utf-8").read()
if "PLAN_STORE = " in mc_src or "def submit_plan(" in mc_src:
    failures.append("mc.py re-defines plan-engine internals (they live in "
                    "agent/plans.py; mc re-exports only)")
if "from agent.plans import" not in mc_src:
    failures.append("mc.py must re-export the plan engine from agent.plans")

if failures:
    for f in failures:
        print("FAIL - " + f)
    sys.exit(1)
print("ok   - mcp/* imports nothing from agent/server/mc")
print("ok   - agent/* free of server/mc imports (server_run glue excepted)")
print("ok   - plan engine canonical in agent/plans.py, mc re-exports")
print("\nAll architecture tests passed.")
