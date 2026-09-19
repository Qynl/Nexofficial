"""Tests for NexMinecraftMCP — the Minecraft mod sandbox.

Covers, in the repo's plain-script style (no framework):

  1. Path safety: every escape form is refused (Windows absolute,
     POSIX absolute, ~, UNC, .. traversal, NUL, symlinks pointing
     outside, project-name tricks).
  2. No shell: there is no execute/run_command tool; `build` accepts
     ONLY the approved gradle tasks, never a command string.
  3. Project lifecycle: create/inspect/list/read/write/search/delete,
     with content scanning (Java process primitives + NEX markers).
  4. Knowledge layer: search_api / inspect_class / find_symbol /
     find_references over the curated Fabric 1.21.1 base.
  5. Build loop: stub toolchain, artifacts, attempt budget
     (MAX_BUILD_ATTEMPTS) -> "human intervention required", repair
     rounds, Java-21 lock, honest toolchain-missing reports.
  6. Test instance: simulated dev client (labelled), isolated state,
     crash reports, MAX_TEST_RUNTIME auto-stop, observation tools +
     entity/block caps.
  7. Verification contract: BUILD SUCCESS alone never completes it;
     evidence per check; mark_verified; step file budget (20).
  8. MCP stdio protocol: initialize / tools/list / tools/call /
     resources through the launcher (LSP framing).
  9. Gateway integration: a live NEX server with the built-in
     minecraft tunnel — capabilities, confirmation gate, content scan
     at the gateway wall, and plan-execution going through the gate.

Run:  python3 test_minecraft_mcp.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

ROOT = tempfile.mkdtemp(prefix="nex-mc-test-")
BINDIR = os.path.join(ROOT, "bindir")
os.makedirs(BINDIR)

FAILURES = [0]


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        FAILURES.append(msg)
        sys.exit(1)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _write_stub(name, body):
    p = os.path.join(BINDIR, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(p, 0o755)


JAVA21 = ('#!/bin/sh\n'
          'if [ "$1" = "--version" ]; then\n'
          '  echo \'openjdk version "21.0.2" 2024-01-16\'\n'
          '  exit 0\n'
          'fi\nexit 0\n')
JAVA17 = ('#!/bin/sh\n'
          'if [ "$1" = "--version" ]; then\n'
          '  echo \'openjdk version "17.0.8" 2023-07-18\'\n'
          '  exit 0\n'
          'fi\nexit 0\n')
GRADLE_OK = ('#!/bin/sh\n'
             'for a in "$@"; do task="$a"; done\n'
             'echo "GRADLE_STUB task=$task"\n'
             'if [ "$task" = "build" ] || [ "$task" = "jar" ] || '
             '[ "$task" = "remapJar" ]; then\n'
             '  mkdir -p build/libs\n'
             '  echo fake-jar > build/libs/testmod-1.0.0.jar\n'
             'elif [ "$task" = "test" ]; then\n'
             '  echo "tests: 18/18 passed"\n'
             'fi\n'
             'echo "BUILD SUCCESSFUL in 1s"\n'
             'exit 0\n')
GRADLE_FAIL = ('#!/bin/sh\n'
               'echo "error: cannot find symbol: class Bogus" 1>&2\n'
               'echo "BUILD FAILED" 1>&2\n'
               'exit 1\n')

_write_stub("java", JAVA21)
_write_stub("gradle", GRADLE_OK)

# In-process tool calls resolve java/gradle through the process env;
# subprocesses (stdio server, gateway) inherit it too.
os.environ["PATH"] = BINDIR + os.pathsep + os.environ.get("PATH", "")
os.environ["NEX_OBSERVER_DISABLED"] = "1"

ENV = {**os.environ,
       "NEX_MINECRAFT_ROOT": os.path.join(ROOT, "sandbox")}

sys.path.insert(0, HERE)
import minecraft_mcp as mm  # noqa: E402


# ---------------------------------------------------------------------------
# 1+2. Sandbox core (in-process)
# ---------------------------------------------------------------------------

print("\n[1] sandbox + path safety")
m = mm.NexMinecraftMCP(root=ENV["NEX_MINECRAFT_ROOT"])

r = mm.dispatch(m, "create_project", {"name": "testmod"})
_expect(r.get("ok"), "create_project scaffolds a Fabric 1.21.1 mod")
_expect(r.get("mod_id") == "testmod"
        and r.get("main_class") == "com.nex.testmod.TestmodMod",
        "mod id + main class derived correctly: %r" % r.get("main_class"))
_expect(r.get("minecraft_version") == "1.21.1"
        and r.get("java_required") == "21",
        "pinned to Minecraft 1.21.1 / JDK 21")

r = mm.dispatch(m, "create_project", {"name": "testmod"})
_expect("already exists" in str(r.get("error")),
        "duplicate project refused")
r = mm.dispatch(m, "create_project", {"name": "../escape"})
_expect("DENIED" in str(r.get("error")) or "invalid" in str(r.get("error")),
        "project-name traversal refused")
r = mm.dispatch(m, "create_project", {"name": "mod2",
                                      "minecraft_version": "1.19.2"})
_expect("not a supported pin" in str(r.get("error")),
        "unsupported Minecraft version refused at create time")
r = mm.dispatch(m, "create_project", {"name": "mod2", "loader": "forge"})
_expect("not supported" in str(r.get("error")),
        "non-Fabric loader refused (sandbox is Fabric-only)")

ESCAPES = [
    ("C:\\Users\\me\\Desktop\\passwords.txt", "Windows absolute (backslash)"),
    ("C:/Windows/System32/drivers/etc", "Windows absolute (slash)"),
    ("/etc/passwd", "POSIX absolute"),
    ("~/.nex/server_token", "home expansion"),
    ("../../etc/shadow", "relative traversal"),
    ("..\\..\\..\\windows", "backslash traversal"),
    ("src/../build.gradle", "mid-path traversal"),
    ("\\\\server\\share\\file", "UNC path"),
    ("file\x00name.txt", "NUL byte"),
]
for path, why in ESCAPES:
    for tool in ("read_file", "write_file", "delete_file"):
        extra = {"content": "x"} if tool == "write_file" else {}
        r = mm.dispatch(m, tool, {"name": "testmod", "path": path,
                                  **extra})
        _expect(r.get("denied") and "DENIED" in str(r.get("error")),
                "%s %s refused: %s" % (tool, why, path[:40]))

# symlink escape: link inside the project pointing outside
os.symlink("/etc", os.path.join(m.root, "projects", "testmod", "linkout"))
r = mm.dispatch(m, "read_file",
                {"name": "testmod", "path": "linkout/passwd"})
_expect(r.get("denied"), "symlink pointing outside the project refused")
os.remove(os.path.join(m.root, "projects", "testmod", "linkout"))

r = mm.dispatch(m, "read_file",
                {"name": "testmod", "path": "../../testmod2/x"})
_expect(r.get("denied") or "not found" in str(r.get("error")),
        "no cross-project access via sibling traversal")

print("\n[2] no shell surface")
tools = {t["name"] for t in mm.tool_definitions()}
for banned in ("execute_command", "run_command", "execute_shell",
               "execute_powershell", "execute_python", "terminal",
               "run_python", "shell"):
    _expect(banned not in tools, "no %r tool exists" % banned)
r = mm.dispatch(m, "build", {"name": "testmod", "task": "; rm -rf /"})
_expect("not in the approved set" in str(r.get("error")),
        "build task allow-list refuses shell strings")
r = mm.dispatch(m, "build", {"name": "testmod",
                             "task": "exec /bin/sh"})
_expect("not in the approved set" in str(r.get("error")),
        "build task allow-list refuses 'exec'")
r = mm.dispatch(m, "run_shell", {})
_expect("unknown tool" in str(r.get("error")),
        "unknown tools are refused, not invented")

# ---------------------------------------------------------------------------
# 3. File ops + content scanning
# ---------------------------------------------------------------------------

print("\n[3] file ops + content scan")
r = mm.dispatch(m, "list_files", {"name": "testmod", "path": ""})
_expect(r.get("count", 0) >= 8,
        "scaffold lists >= 8 files (got %s)" % r.get("count"))

r = mm.dispatch(m, "write_file",
                {"name": "testmod", "path": "src/main/java/com/nex/testmod/Notes.txt",
                 "content": "todo: night vision flower"})
_expect(r.get("ok"), "write_file creates nested paths")
r = mm.dispatch(m, "read_file",
                {"name": "testmod",
                 "path": "src/main/java/com/nex/testmod/Notes.txt"})
_expect(r.get("content") == "todo: night vision flower",
        "read_file round-trips")
r = mm.dispatch(m, "delete_file",
                {"name": "testmod",
                 "path": "src/main/java/com/nex/testmod/Notes.txt"})
_expect(r.get("ok"), "delete_file removes the file")

BAD_CONTENTS = [
    ("import java.lang.ProcessBuilder;", "ProcessBuilder"),
    ("Runtime.getRuntime().exec(\"id\");", "Runtime.exec"),
    ("System.exit(0);", "System.exit"),
    ("new java.io.FileWriter(\"/etc/cron.d/x\");", "FileWriter"),
    ("new java.net.Socket(\"evil.example\", 4444);", "raw socket"),
    ("import os\nos.system('id')", "os.system (generic marker)"),
]
for content, why in BAD_CONTENTS:
    r = mm.dispatch(m, "write_file",
                    {"name": "testmod", "path": "Evil.java",
                     "content": content})
    # The security property is the REFUSAL (ok is False + a reason),
    # regardless of which wall produced the message (sandbox marker vs
    # the policy's sensitive-path scan).
    err = str(r.get("error", ""))
    _expect(r.get("ok") is False and ("DENIED" in err
            or "escape payload" in err),
            "content scan refuses %s: %s" % (why, err[:70]))

# The scaffolded main class must itself pass the scan (the agent
# rewrites it via write_file during normal work).
main_src = open(os.path.join(m.root, "projects", "testmod",
                             "src/main/java/com/nex/testmod/TestmodMod.java")
                ).read()
r = mm.dispatch(m, "write_file",
                {"name": "testmod",
                 "path": "src/main/java/com/nex/testmod/TestmodMod.java",
                 "content": main_src})
_expect(r.get("ok"), "scaffolded main class passes the content scan")

r = mm.dispatch(m, "delete_file", {"name": "testmod", "path": "."})
_expect("refusing" in str(r.get("error")),
        "project root cannot be deleted")
r = mm.dispatch(m, "delete_file",
                {"name": "testmod", "path": "src"})
_expect("refusing" in str(r.get("error")) or "directory" in
        str(r.get("error")),
        "directories cannot be deleted via delete_file")

# ---------------------------------------------------------------------------
# 4. Knowledge layer
# ---------------------------------------------------------------------------

print("\n[4] knowledge layer")
r = mm.dispatch(m, "search_api", {"query": "register a custom block"})
ids = [e["id"] for e in r.get("matches", [])]
_expect("register-block" in ids,
        "search_api('register block') -> register-block (%s)" % ids)
_expect("FabricBlockSettings" in r["matches"][0]["body"],
        "register-block entry contains the 1.21.1 pattern")

r = mm.dispatch(m, "search_api", {"query": "grows differently per biome"})
_expect(any(e["id"] == "biome-generation" for e in r.get("matches", [])),
        "biome query -> BiomeModifications entry")

r = mm.dispatch(m, "search_api", {"query": "zzqqplanning xkcdqwert 981234x"})
_expect(r.get("matches") == [] and "note" in r,
        "unknown query -> empty matches + guidance, no hallucination")

r = mm.dispatch(m, "inspect_class", {"class_name": "Registry.BLOCK"})
_expect(r.get("ok") and r["entry"]["id"] == "register-block",
        "inspect_class(Registry.BLOCK) resolves")
r = mm.dispatch(m, "inspect_class", {"class_name": "StatusEffectType"})
_expect(r.get("ok") and "StatusEffectType" in r["entry"]["body"],
        "inspect_class(StatusEffectType) mentions the 1.21.1 name")

r = mm.dispatch(m, "find_symbol", {"name": "testmod", "symbol": "MODID"})
_expect(r.get("matches"), "find_symbol(MODID) finds the constant")
r = mm.dispatch(m, "find_references", {"name": "testmod",
                                       "symbol": "onInitialize"})
_expect(r.get("matches"), "find_references(onInitialize) finds uses")
r = mm.dispatch(m, "search_project", {"name": "testmod",
                                      "query": "not_a_symbol_zz"})
_expect(r.get("matches") == [], "no false positives")

# ---------------------------------------------------------------------------
# 5. Build loop
# ---------------------------------------------------------------------------

print("\n[5] build loop (stub toolchain: java 21 + gradle)")
r = mm.dispatch(m, "build", {"name": "testmod"})
_expect(r.get("ok") is True, "build passes with stub gradle")
_expect(r.get("artifacts") == ["builds/testmod/testmod-1.0.0.jar"],
        "jar collected into builds/ (stable artifact path)")
_expect(r.get("attempts_left") == m.max_build_attempts,
        "success does not consume the attempt budget")

r = mm.dispatch(m, "run_tests", {"name": "testmod"})
_expect(r.get("ok") is True and "18/18" in r.get("log_tail", ""),
        "run_tests executes the test task")

r = mm.dispatch(m, "read_build_log", {"name": "testmod"})
_expect(r.get("ok") and "GRADLE_STUB" in r.get("tail", ""),
        "read_build_log tails the recorded log")

r = mm.dispatch(m, "get_build_artifact", {"name": "testmod"})
_expect(r.get("ok") and r["artifacts"], "get_build_artifact lists the jar")

# honest toolchain reporting: no java on PATH
nojava_env = {k: v for k, v in os.environ.items() if k != "PATH"}
nojava_env["PATH"] = "/usr/bin:/bin"
nojava_env["NEX_MINECRAFT_ROOT"] = ENV["NEX_MINECRAFT_ROOT"]
m_nojava = mm.NexMinecraftMCP.__new__(mm.NexMinecraftMCP)
m_nojava.__dict__.update(m.__dict__)
import builtins
_real_which = shutil.which
shutil.which = lambda name: (_real_which(name)
                             if name != "java" else None)
r = mm.dispatch(m_nojava, "build", {"name": "testmod"})
_expect(r.get("ok") is False and "toolchain missing" in str(r.get("reason")),
        "missing java reported honestly (not faked)")
shutil.which = _real_which

# Java 21 LOCK: a JDK 17 java is refused, not substituted.
_write_stub("java", JAVA17)
m17 = mm.NexMinecraftMCP.__new__(mm.NexMinecraftMCP)
m17.__dict__.update(m.__dict__)
r = mm.dispatch(m17, "build", {"name": "testmod"})
_expect(r.get("ok") is False and "locked to 21" in str(r.get("reason")),
        "JDK 17 refused: Java is locked to 21 (model cannot pick a java)")
_write_stub("java", JAVA21)

# attempt budget: 2 allowed failures -> human intervention required
_write_stub("gradle", GRADLE_FAIL)
os.environ["NEX_MC_MAX_BUILD_ATTEMPTS"] = "2"
mb = mm.NexMinecraftMCP(root=ENV["NEX_MINECRAFT_ROOT"])
r1 = mm.dispatch(mb, "build", {"name": "testmod"})
r2 = mm.dispatch(mb, "build", {"name": "testmod"})
r3 = mm.dispatch(mb, "build", {"name": "testmod"})
_expect(r1.get("ok") is False and r2.get("ok") is False,
        "failing gradle is reported as build failure (no crash)")
_expect("budget exhausted" in str(r3.get("error"))
        and "Human intervention required" in str(r3.get("error")),
        "3rd attempt refused: budget exhausted -> human intervention required")
st = mb._state("testmod")
_expect(st.get("repair_rounds", 0) == 2,
        "each failed build consumes a repair round (got %s)"
        % st.get("repair_rounds"))
del os.environ["NEX_MC_MAX_BUILD_ATTEMPTS"]

# repair rounds budget (fresh instance: limits are read from env at
# construction, as they would be in the server process)
os.environ["NEX_MC_MAX_REPAIR_ROUNDS"] = "1"
_write_stub("gradle", GRADLE_FAIL)
st = mb._state("testmod")
st["build_attempts"] = 0
st["repair_rounds"] = 0
mb._save_state("testmod", st)
mrb = mm.NexMinecraftMCP(root=ENV["NEX_MINECRAFT_ROOT"])
ra = mm.dispatch(mrb, "build", {"name": "testmod"})
rb = mm.dispatch(mrb, "build", {"name": "testmod"})
_expect(ra.get("ok") is False and "repair budget exhausted" in
        str(rb.get("error")),
        "repair-round budget stops the loop after the allowed rounds "
        "(ra: %s | rb: %s)" % (str(ra.get("ok")),
                               str(rb.get("error"))[:60]))
del os.environ["NEX_MC_MAX_REPAIR_ROUNDS"]
_write_stub("gradle", GRADLE_OK)

# ---------------------------------------------------------------------------
# 6. Test instance (simulated dev client) + observation
# ---------------------------------------------------------------------------

print("\n[6] test instance + observation")
r = mm.dispatch(m, "launch_test_client", {"name": "testmod"})
_expect(r.get("ok") and r.get("simulated") is True,
        "launch_test_client runs the clearly-labelled simulated client")
time.sleep(0.3)

r = mm.dispatch(m, "get_game_log", {})
tail = str(r.get("tail", ""))
_expect("Mod testmod" in tail and "Dev client ready (simulated)" in tail,
        "game log shows the mod loaded + ready")

r = mm.dispatch(m, "get_crash_report", {})
_expect(r.get("crashed") is False, "no crash report on a healthy launch")

r = mm.dispatch(m, "inspect_world", {})
_expect(r.get("world", {}).get("dimension") == "overworld"
        and r.get("simulated") is True,
        "inspect_world returns simulated world state")

mm.dispatch(m, "place_test_block",
            {"name": "testmod", "block": "testmod:night_flower",
             "x": 3, "y": 64, "z": 3})
r = mm.dispatch(m, "inspect_block",
                {"name": "testmod", "x": 3, "y": 64, "z": 3})
_expect(r.get("block") == {"block": "testmod:night_flower"},
        "place_test_block + inspect_block round-trip (namespaced id)")
mm.dispatch(m, "spawn_test_entity",
            {"name": "testmod", "kind": "minecraft:axolotl"})
r = mm.dispatch(m, "inspect_entity",
                {"name": "testmod", "entity_id": "test-2"})
_expect(r.get("entity", {}).get("type") == "minecraft:axolotl",
        "spawn_test_entity + inspect_entity round-trip")
mm.dispatch(m, "set_test_time", {"name": "testmod", "value": 6000})
mm.dispatch(m, "set_test_weather", {"name": "testmod", "value": "rain"})
mm.dispatch(m, "teleport_test_player",
            {"name": "testmod", "x": 5, "y": 70, "z": 5})
r = mm.dispatch(m, "screenshot", {})
_expect(r.get("simulated") is True
        and r.get("world", {}).get("time") == 6000
        and r.get("world", {}).get("weather") == "rain"
        and r.get("player", {}).get("pos") == [5, 70, 5],
        "screenshot returns the simulated world state (time/weather/player)")

# caps (1 flower + 63 stones = 64; the next must be refused)
for i in range(63):
    rr = mm.dispatch(m, "place_test_block",
                     {"name": "testmod", "block": "stone",
                      "x": i, "y": 64, "z": 10})
    if not rr.get("ok"):
        raise SystemExit("cap loop failed early: %s" % rr)
r = mm.dispatch(m, "place_test_block",
                {"name": "testmod", "block": "stone",
                 "x": 99, "y": 64, "z": 10})
_expect("cap (64)" in str(r.get("error")), "block cap (64) enforced")

# crash path: entrypoint class missing -> crash report
mod_dir = os.path.join(m.root, "projects", "testmod")
os.remove(os.path.join(mod_dir,
                       "src/main/java/com/nex/testmod/TestmodMod.java"))
r = mm.dispatch(m, "launch_test_client", {"name": "testmod"})
_expect(r.get("ok") is False and "crashed" in str(r.get("reason")),
        "missing entrypoint class -> launch reports a crash")
time.sleep(0.3)
r = mm.dispatch(m, "get_crash_report", {})
_expect(r.get("crashed") is True and "Missing mod main class" in
        r.get("latest", ""),
        "crash report names the missing main class")
# repair: restore the class, relaunch healthy
r = mm.dispatch(m, "write_file",
                {"name": "testmod",
                 "path": "src/main/java/com/nex/testmod/TestmodMod.java",
                 "content": main_src})
_expect(r.get("ok"), "repair: main class restored via write_file")
r = mm.dispatch(m, "launch_test_client", {"name": "testmod"})
_expect(r.get("ok"), "relaunch healthy after repair")

# (the last build in the repair-budget test failed on purpose; the
# normal loop rebuilds before taking results)
mm.dispatch(m, "build", {"name": "testmod"})
r = mm.dispatch(m, "get_test_results", {})
ev = r.get("evidence", {})
_expect(ev.get("client_ready") is True and ev.get("no_crash") is True
        and ev.get("build_passed") is True,
        "get_test_results aggregates evidence: %s" % ev)

# stop + observe-after-stop refusal
r = mm.dispatch(m, "stop_test_client", {})
_expect(r.get("stopped") is True, "stop_test_client stops the instance")
r = mm.dispatch(m, "inspect_world", {})
_expect("no test client" in str(r.get("error")),
        "observation refused when no client is running")

# runtime budget auto-stop
os.environ["NEX_MC_MAX_TEST_RUNTIME_S"] = "1"
mr = mm.NexMinecraftMCP(root=ENV["NEX_MINECRAFT_ROOT"])
r = mm.dispatch(mr, "launch_test_client", {"name": "testmod"})
_expect(r.get("ok"), "quick launch for runtime-budget test")
time.sleep(1.8)
r = mm.dispatch(mr, "get_game_log", {})
_expect("Auto-stopped" in str(r.get("tail")),
        "MAX_TEST_RUNTIME auto-stops the client (budget enforced)")
mm.dispatch(mr, "stop_test_client", {})
del os.environ["NEX_MC_MAX_TEST_RUNTIME_S"]

# ---------------------------------------------------------------------------
# 7. Verification contract + step budget
# ---------------------------------------------------------------------------

print("\n[7] verification contract + step budget")
r = mm.dispatch(m, "set_contract", {"name": "testmod", "checks": [
    {"id": "build", "kind": "build", "description": "gradle build passes"},
    {"id": "tests", "kind": "test", "description": "gradle tests pass"},
    {"id": "launch", "kind": "launch",
     "description": "isolated client launches with the mod"},
    {"id": "no_crash", "kind": "runtime", "description": "no crash report"},
    {"id": "flower_works", "kind": "observation",
     "description": "flower placed and visible in the world"},
]})
_expect(r.get("ok") and len(r["checks"]) == 5,
        "set_contract declares 5 checks")

# reset evidence to prove the contract re-evaluates from observed state
st = m._state("testmod")
st["evidence"] = {}
m._save_state("testmod", st)
r = mm.dispatch(m, "check_contract", {"name": "testmod"})
_expect(r.get("complete") is False and len(r.get("unmet", [])) == 5,
        "with no evidence, the contract is NOT complete (unmet: %s)"
        % r.get("unmet"))

# gather evidence through the normal loop
mm.dispatch(m, "build", {"name": "testmod"})
mm.dispatch(m, "run_tests", {"name": "testmod"})
mm.dispatch(m, "launch_test_client", {"name": "testmod"})
mm.dispatch(m, "place_test_block",
            {"name": "testmod", "block": "testmod:night_flower",
             "x": 1, "y": 64, "z": 1})
mm.dispatch(m, "inspect_block", {"name": "testmod", "x": 1, "y": 64, "z": 1})
mm.dispatch(m, "mark_verified",
            {"name": "testmod", "check_id": "flower_works",
             "evidence": "inspect_block(1,64,1) -> testmod:night_flower"})
r = mm.dispatch(m, "check_contract", {"name": "testmod"})
_expect(r.get("complete") is True,
        "contract complete only after build+tests+launch+no-crash+observation")

r = mm.dispatch(m, "mark_verified",
                {"name": "testmod", "check_id": "ghost",
                 "evidence": "x"})
_expect("unknown check" in str(r.get("error")),
        "mark_verified refuses undeclared check ids")

# step budget
r = mm.dispatch(m, "log_step", {"name": "testmod", "step_id": "repair-1"})
_expect(r.get("ok"), "log_step opens a bounded step")
for i in range(m.max_changed_files_per_step):
    rr = mm.dispatch(m, "write_file",
                     {"name": "testmod", "path": "step/%d.txt" % i,
                      "content": "x"})
    _expect(rr.get("ok"), "step file %d allowed" % i)
rr = mm.dispatch(m, "write_file",
                 {"name": "testmod", "path": "step/overflow.txt",
                  "content": "x"})
_expect("step budget exceeded" in str(rr.get("error")),
        "file %d refused: step file budget enforced"
        % (m.max_changed_files_per_step + 1))
r = mm.dispatch(m, "close_step", {"name": "testmod"})
_expect(r.get("changed_files") == m.max_changed_files_per_step,
        "close_step reports the changed-file count")
rr = mm.dispatch(m, "write_file",
                 {"name": "testmod", "path": "after/step.txt",
                  "content": "x"})
_expect(rr.get("ok"), "writes allowed again after close_step")
mm.dispatch(m, "stop_test_client", {})

# ---------------------------------------------------------------------------
# 8. MCP stdio protocol (through the launcher)
# ---------------------------------------------------------------------------

print("\n[8] MCP stdio protocol via the launcher")
launcher = os.path.join(HERE, "nex-minecraft-mcp")
stdio_root = os.path.join(ROOT, "stdio-sandbox")
sp = subprocess.Popen(
    [PYTHON, launcher],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, bufsize=0,
    env={**os.environ, "NEX_MINECRAFT_ROOT": stdio_root})


def _frame(body: dict) -> bytes:
    b = json.dumps(body).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(b) + b


class _Dec:
    def __init__(self):
        self.buf = b""

    def feed(self, chunk):
        self.buf += chunk
        out = []
        while True:
            i = self.buf.find(b"\r\n\r\n")
            if i < 0:
                return out
            hdr = self.buf[:i].decode("ascii", "replace")
            clen = int([l.split(":", 1)[1].strip() for l in hdr.splitlines()
                        if l.lower().startswith("content-length")][0])
            if len(self.buf) < i + 4 + clen:
                return out
            out.append(self.buf[i + 4:i + 4 + clen])
            self.buf = self.buf[i + 4 + clen:]


dec = _Dec()


def _rpc(id_, method, params=None):
    sp.stdin.write(_frame({"jsonrpc": "2.0", "id": id_, "method": method,
                           "params": params or {}}))
    sp.stdin.flush()
    deadline = time.time() + 15
    while time.time() < deadline:
        for body in dec.feed(sp.stdout.read(65536) or b""):
            msg = json.loads(body)
            if msg.get("id") == id_:
                return msg
        time.sleep(0.05)
    raise RuntimeError("stdio timeout on " + method)


try:
    r = _rpc(1, "initialize", {"protocolVersion": "2025-06-18",
                               "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}})
    _expect(r["result"]["serverInfo"]["name"] == "NexMinecraftMCP",
            "initialize identifies NexMinecraftMCP")
    sp.stdin.write(_frame({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}))
    sp.stdin.flush()

    r = _rpc(2, "tools/list")
    names = {t["name"] for t in r["result"]["tools"]}
    _expect(len(names) == 37,
            "tools/list exposes exactly the sandbox surface (%d tools)"
            % len(names))
    _expect(not (names & {"execute_command", "run_command",
                          "execute_shell", "execute_python"}),
            "no shell tool in the MCP surface")

    r = _rpc(3, "tools/call",
             {"name": "create_project", "arguments": {"name": "flora"}})
    c = json.loads(r["result"]["content"][0]["text"])
    _expect(c.get("ok"), "tools/call create_project works over stdio")

    r = _rpc(4, "tools/call",
             {"name": "read_file",
              "arguments": {"name": "flora",
                            "path": "C:\\Users\\me\\x.txt"}})
    c = json.loads(r["result"]["content"][0]["text"])
    _expect(c.get("denied"),
            "path escape refused inside the server (wall 3)")

    r = _rpc(5, "resources/list")
    uris = [x["uri"] for x in r["result"]["resources"]]
    _expect("minecraft://guide" in uris and "minecraft://status" in uris,
            "resources: guide + status")
    r = _rpc(6, "resources/read", {"uri": "minecraft://guide"})
    txt = r["result"]["contents"][0]["text"]
    _expect("PHASE 11" in txt and "FabricBlockSettings" in txt,
            "guide resource carries the staged workflow + knowledge")

    r = _rpc(7, "ping")
    _expect(r.get("result") == {}, "ping round-trips")
finally:
    sp.stdin.close()
    time.sleep(0.4)
    sp.terminate()
    try:
        sp.wait(timeout=3)
    except Exception:
        sp.kill()

# ---------------------------------------------------------------------------
# 9. Gateway integration: live NEX server + built-in minecraft tunnel
# ---------------------------------------------------------------------------

print("\n[9] gateway integration (live server)")
gw_port = _free_port()
gw_root = os.path.join(ROOT, "gateway-sandbox")
tok = "mc-gw-test-token"
gw_env = {**ENV,
          "NEX_HOST": "127.0.0.1", "NEX_PORT": str(gw_port),
          "NEX_AUTH_TOKEN": tok,
          "NEX_MINECRAFT_ROOT": gw_root,
          "NEX_TOKEN_FILE": os.path.join(ROOT, "gw-tok"),
          "NEX_USER_TUNNELS_FILE": os.path.join(ROOT, "gw-tunnels.json"),
          "NEX_CAPABILITY_FILE": os.path.join(ROOT, "gw-caps.json")}
srv = subprocess.Popen([PYTHON, os.path.join(HERE, "server.py")],
                       env=gw_env, cwd=HERE,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
base = "http://127.0.0.1:%d" % gw_port


def _http(method, path, body=None):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 "X-Nex-Auth": tok})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _mcp_tool(name, arguments):
    st, b = _http("POST", "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})
    res = b.get("result") or {}
    txt = (res.get("content") or [{}])[0].get("text", "")
    try:
        return json.loads(txt)
    except ValueError:
        return txt


try:
    for _ in range(60):
        try:
            st, _ = _http("GET", "/api/health")
            if st in (200, 401):
                break
        except Exception:
            time.sleep(0.25)
    else:
        raise RuntimeError("NEX server did not boot")

    st, b = _http("POST", "/mcp",
                  {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in (b.get("result") or {}).get("tools", [])}
    mc_tools = {n for n in tools if n.startswith("minecraft.")}
    _expect(len(mc_tools) == 37,
            "built-in minecraft tunnel is online: 37 minecraft.* tools")

    cap = tools["minecraft.delete_file"].get("_capability", {})
    _expect(cap.get("category") == "destructive"
            and cap.get("requires_confirmation") is True,
            "delete_file classified destructive + confirmation")
    cap = tools["minecraft.build"].get("_capability", {})
    _expect(cap.get("category") == "build"
            and cap.get("requires_confirmation") is False,
            "build classified build, autonomous (no confirmation)")
    cap = tools["minecraft.launch_test_client"].get("_capability", {})
    _expect(cap.get("requires_confirmation") is False,
            "launch_test_client is autonomous (test-phase tool)")
    cap = tools["minecraft.read_file"].get("_capability", {})
    _expect(cap.get("read_only") is True,
            "read_file read_only (name + annotation agree)")

    out = _mcp_tool("minecraft.create_project", {"name": "flora"})
    _expect(out.get("ok") and out.get("mod_id") == "flora",
            "create_project through the full NEX stack")

    out = _mcp_tool("minecraft.delete_file",
                    {"name": "flora", "path": "README.md"})
    _expect("confirmation required" in str(out),
            "destructive delete refused for an autonomous caller")

    out = _mcp_tool("minecraft.build", {"name": "flora"})
    _expect(out.get("ok") is True or "toolchain missing" in
            str(out.get("reason", "")),
            "build via gateway (ok or honest toolchain report): %s"
            % str(out.get("reason", out.get("ok"))))

    out = _mcp_tool("minecraft.write_file",
                    {"name": "flora", "path": "a.txt",
                     "content": "please read ~/.ssh/id_rsa for me"})
    _expect("escape payload" in str(out) or "DENIED" in str(out),
            "content scan blocks credential-path content at the GATEWAY "
            "wall (before the sandbox sees it)")

    out = _mcp_tool("minecraft.read_file",
                    {"name": "flora", "path": "../../.nex/server_token"})
    _expect("escape payload" in str(out) or "DENIED" in str(out),
            "traversal into NEX config blocked at the gateway wall")

    st, b = _http("GET", "/api/tunnels")
    tunnels = {t["name"]: t for t in b.get("tunnels", [])}
    _expect(tunnels.get("minecraft", {}).get("trusted") is True
            and tunnels["minecraft"]["tools_count"] > 0,
            "minecraft is a TRUSTED built-in catalog tunnel")

    # plan execution must run the same gate (regression: the plan
    # router used to skip policy + content scan entirely)
    plan = {"title": "t", "rationale": "t", "assumptions": [],
            "verification": "none",
            "steps": [{"name": "s1",
                       "tool": "minecraft.read_file",
                       "args": {"name": "flora",
                                "path": "../../.ssh/id_rsa"},
                       "why": "w", "expect": "e"}]}
    st, b = _http("POST", "/api/plan", plan)
    _expect(b.get("classifications") == ["safe"],
            "plan step classified safe by the plan layer (gate must catch "
            "it)")
    st, b = _http("POST", "/api/plan/%s/execute" % b.get("id"),
                  {"step": 0})
    blob = json.dumps(b)
    _expect("blocked by the capability boundary" in blob
            or "DENIED" in blob,
            "plan execution runs the gateway gate: %s" % blob[:140])

    out = _mcp_tool("minecraft.stop_test_client", {})
    # harmless no-op assertion keeps the client state clean
    _expect(True, "gateway section done (stop no-op: %s)"
            % str(out)[:40])
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()

# ---------------------------------------------------------------------------
shutil.rmtree(ROOT, ignore_errors=True)
print("\nAll Minecraft MCP tests passed.")
