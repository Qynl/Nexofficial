"""Architecture guard for Nex 2.0.

Enforces the invariants the security model depends on, at the SOURCE
level (AST, not runtime imports):

  1. Dependency direction:  server → agent → mcp.
     No reverse edges, no side doors.
  2. The capability boundary: NO local tool surface. tools.py must not
     exist; the model's only action path is mcp/manager.call → policy.
  3. No Minecraft: the built-in Minecraft subsystem is gone entirely.
     (A user-added MCP server that happens to control Minecraft is
     config, not code — nothing in the repo may know about it.)
  4. Process spawning happens ONLY in mcp/transport.py (the stdio MCP
     transport) — never in the agent.
  5. MCP_ONLY is not configurable.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
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
            if node.module:
                mods.add(node.module.split(".")[0])
    return mods


def uses_subprocess(path):
    """AST check: does the module import subprocess?"""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            return True
    return False


def all_py():
    for root, _dirs, files in os.walk(NEX):
        _dirs[:] = [d for d in _dirs
                    if d not in ("__pycache__", "docs", "web", ".git")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def rel(p):
    return os.path.relpath(p, NEX)


# ── 1. dependency direction ──────────────────────────────────────────────

for root, _dirs, files in os.walk(os.path.join(NEX, "mcp")):
    for f in files:
        if not f.endswith(".py"):
            continue
        path = os.path.join(root, f)
        bad = _imports(path) & {"agent", "server", "store"}
        if bad:
            failures.append("%s imports %s (mcp must stay the pure core)"
                            % (rel(path), sorted(bad)))

for root, _dirs, files in os.walk(os.path.join(NEX, "agent")):
    for f in files:
        if not f.endswith(".py"):
            continue
        path = os.path.join(root, f)
        bad = _imports(path) & {"server", "store"}
        if bad:
            failures.append("%s imports %s (agent must not reach the HTTP "
                            "layer)" % (rel(path), sorted(bad)))

# ── 2. the capability boundary ───────────────────────────────────────────

for gone in ("tools.py", "mc.py", "mc_tools.py", "minecraft_mcp.py",
             "stdio_server.py", "observer.py", "tunnels.py",
             "mcp_engines.py", "nex-minecraft-mcp", "nex-stdio",
             "upstream.py", "settings.html", "provider_chip.js"):
    if os.path.exists(os.path.join(NEX, gone)):
        failures.append("local tool surface %r still exists — the model's "
                        "action path must be MCP only" % gone)

# The manager is the only action path; the loop must not use the
# transport directly.
loop_src = open(os.path.join(NEX, "agent", "loop.py"),
                encoding="utf-8").read()
if "mcp.transport" in loop_src:
    failures.append("agent/loop.py must reach servers only through the "
                    "manager, not the transport")

# The live registry is shown to the planner.  It must remain metadata-only:
# retaining a client or exposing call() here would be a second route around
# manager.call() -> policy -> audit.
registry_tree = ast.parse(open(os.path.join(NEX, "mcp", "registry.py"),
                               encoding="utf-8").read())
for node in ast.walk(registry_tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
            and node.name == "call":
        failures.append("mcp/registry.py exposes call() — the registry must "
                        "be metadata-only")

# ── 3. no Minecraft anywhere ─────────────────────────────────────────────

MINECRAFT_WORDS = ("minecraft", "Minecraft", "MINECRAFT")
for path in all_py():
    if path.startswith(os.path.join(NEX, "tests")):
        continue    # the tests themselves assert the absence
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        continue
    for needle in MINECRAFT_WORDS:
        if needle in src:
            failures.append("%s still references %r" % (rel(path), needle))
            break

# ── 4. process spawning only in the transport ────────────────────────────

for path in all_py():
    r = rel(path)
    if r.startswith("tests"):
        continue
    if r == os.path.join("mcp", "transport.py"):
        continue    # the stdio MCP transport — the one legitimate spawner
    if uses_subprocess(path):
        failures.append("%s uses subprocess — only mcp/transport.py (the "
                        "stdio MCP transport) may spawn processes" % r)

# ── 5. the boundary is not configurable ──────────────────────────────────

policy_src = open(os.path.join(NEX, "mcp", "policy.py"),
                  encoding="utf-8").read()
if "MCP_ONLY = True" not in policy_src:
    failures.append("mcp/policy.py: MCP_ONLY must be a hardcoded True")

# ── 6. roles ─────────────────────────────────────────────────────────────

providers_src = open(os.path.join(NEX, "agent", "providers.py"),
                     encoding="utf-8").read()
if 'ROLE_CHAT = "chat"' not in providers_src \
        or 'ROLE_AGENT = "agent"' not in providers_src:
    failures.append("agent/providers.py: roles must be chat/agent")

if failures:
    for f in failures:
        print("FAIL - " + f)
    sys.exit(1)

print("ok   - mcp/* imports nothing from agent/server (pure core)")
print("ok   - agent/* free of server imports")
print("ok   - no local tool surface (tools/mc/minecraft/stdio gone)")
print("ok   - agent/loop acts only through the manager")
print("ok   - capability registry is metadata-only (no call bypass)")
print("ok   - no Minecraft references in the codebase")
print("ok   - subprocess confined to mcp/transport.py (stdio transport)")
print("ok   - MCP_ONLY is structural, not configurable")
print("\nAll architecture tests passed.")
