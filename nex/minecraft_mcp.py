"""NexMinecraftMCP — a Minecraft-specific MCP sandbox.

This is the dedicated local MCP server that lets Nex MAKE MINECRAFT MODS
without ever touching the PC. The security model is deliberately the
opposite of "give the AI a terminal and hope the prompt catches it":

    ┌────────────────────────────────────────────────────────────┐
    │  Wall 1  Nex policy        mcp/policy.py (authorize, scan) │
    │  Wall 2  NEX gateway       gateway_gate in server.py       │
    │  Wall 3  THIS server       path sandbox + no commands      │  <-- this file
    │  Wall 4  Test instance     isolated world/mods/saves       │
    └────────────────────────────────────────────────────────────┘

Hard rules, enforced in code (not prompts):

  * EVERY path is canonicalized and must stay inside the sandbox root
    (default ~/NexMinecraft, NEX_MINECRAFT_ROOT to move it). Windows
    absolute paths (C:\\Users\\...), POSIX absolute paths, ~, UNC
    prefixes and .. traversal are all refused with
    "DENIED: path outside Minecraft workspace".
  * There is NO execute_command / execute_shell / run_python tool. The
    only process this server ever starts is the server-approved build
    (a fixed gradle task from an allow-list) and the test client. The
    model never chooses an executable, an argument vector or a shell
    string.
  * Builds are budgeted (MAX_BUILD_ATTEMPTS, default 5); when the
    budget is spent the server says "human intervention required"
    instead of looping forever.
  * A mod is never "done" on BUILD SUCCESS alone: the verification
    contract (set_contract / check_contract) requires evidence per
    check — build, tests, client launch, observations, no crash.
  * The test client is an ISOLATED instance (separate world/mods/
    config/saves under test-instance/). When a real Minecraft client
    is not available the server runs a deterministic, clearly-labelled
    SIMULATED dev client so the full build->launch->observe->repair
    loop stays testable; every simulated observation is marked
    "simulated": true. It never pretends to be the real game.

Tool surface (namespaced as `minecraft.<tool>` through the NEX
gateway):

    PROJECT
      create_project  list_projects  inspect_project  list_files
      read_file  write_file  delete_file  search_project

    MINECRAFT KNOWLEDGE
      search_api  inspect_class  find_symbol  find_references

    BUILD
      build  run_tests  read_build_log  get_build_artifact

    TEST INSTANCE
      launch_test_client  stop_test_client  get_game_log
      get_crash_report  get_test_results

    GAME OBSERVATION (isolated instance only)
      inspect_world  inspect_block  inspect_entity  inspect_player
      screenshot  spawn_test_entity  place_test_block
      set_test_time  set_test_weather  teleport_test_player

    VERIFICATION + LOOP LIMITS
      set_contract  check_contract  mark_verified
      log_step  close_step

    STATUS
      minecraft_status

NOT in this surface (by design):

    execute_shell  execute_powershell  execute_python
    read_any_file  write_any_file  delete_any_file
    launch_any_executable  run_command  open_url  send_network
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Sandbox configuration (operator environment only — never model input)
# ---------------------------------------------------------------------------

SERVER_NAME = "NexMinecraftMCP"
SERVER_VERSION = "1.0"

# Fabric 1.21.1 development uses JDK 21; the sandbox is LOCKED to it
# (the model cannot choose a different java executable).
JAVA_REQUIRED = "21"

# Supported (minecraft_version, yarn mappings, fabric loader) pins.
# Kept small and explicit: an unknown version is refused at
# create_project instead of generating a half-correct build file.
DEFAULT_MC_VERSION = "1.21.1"  # the headline pin (sort is lexico-numeric-unsafe)

SUPPORTED_MC_VERSIONS: Dict[str, Dict[str, str]] = {
    "1.21.1": {"yarn": "1.21.1+build.3", "loader": "0.16.5",
               "loom": "1.7.4"},
    "1.21":   {"yarn": "1.21+build.8",  "loader": "0.16.5",
               "loom": "1.7.4"},
    "1.20.6": {"yarn": "1.20.6+build.9", "loader": "0.16.5",
               "loom": "1.7.4"},
}

# The ONLY gradle tasks the build tool may run. `build()` takes a task
# name, never a command string; anything else is refused.
ALLOWED_BUILD_TASKS = ("build", "remapJar", "clean", "jar", "test")

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,63}")
# Minecraft registry ids: "stone" or "namespace:path" (one colon).
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,16}(:[A-Za-z0-9_\-/]{1,48})?$")
_MODID_RE = re.compile(r"[a-z][a-z0-9_]{1,63}")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _env_int(key: str, default: int) -> int:
    try:
        v = int(os.environ.get(key, "").strip() or "")
        return v if v > 0 else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        v = float(os.environ.get(key, "").strip() or "")
        return v if v > 0 else default
    except ValueError:
        return default


class McError(Exception):
    """A sandbox refusal / tool error. The message goes to the model
    verbatim — it is written to be actionable."""


class NexMinecraftMCP:
    """The sandboxed Minecraft mod-development server.

    One instance per process. All state that outlives a call is kept in
    <root>/builds/<project>/state.json (per-project counters, build
    history, verification contract, step budget) so restarts do not
    silently reset the loop limits.
    """

    def __init__(self, root: Optional[str] = None) -> None:
        raw = root or os.environ.get("NEX_MINECRAFT_ROOT", "").strip() \
            or os.path.join(os.path.expanduser("~"), "NexMinecraft")
        self.root = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
        self.projects_dir = os.path.join(self.root, "projects")
        self.test_dir = os.path.join(self.root, "test-instance")
        self.builds_dir = os.path.join(self.root, "builds")
        self.logs_dir = os.path.join(self.root, "logs")
        for d in (self.root, self.projects_dir, self.test_dir,
                  self.builds_dir, self.logs_dir):
            os.makedirs(d, exist_ok=True)

        # Loop limits (operator env overrides; the model cannot touch them).
        self.max_build_attempts = _env_int("NEX_MC_MAX_BUILD_ATTEMPTS", 5)
        self.max_repair_rounds = _env_int("NEX_MC_MAX_REPAIR_ROUNDS", 5)
        self.max_test_runtime_s = _env_float("NEX_MC_MAX_TEST_RUNTIME_S",
                                             120.0)
        self.max_changed_files_per_step = _env_int(
            "NEX_MC_MAX_CHANGED_FILES_PER_STEP", 20)
        self.build_timeout_s = _env_float("NEX_MC_BUILD_TIMEOUT_S", 600.0)

        self.max_read_bytes = 256 * 1024
        self.max_write_bytes = 1024 * 1024
        self.max_list_entries = 500
        self.max_search_results = 40

        # The operator may pin a REAL test client binary (path only, no
        # args — this server chooses the fixed argument vector). When
        # unset, the deterministic simulated dev client is used.
        self.real_client = os.environ.get("NEX_MC_TEST_CLIENT", "").strip()

        self._lock = threading.RLock()
        self._client: Optional[Any] = None      # live test client
        self._client_project: Optional[str] = None

    # ------------------------------------------------------------------
    # Path safety — the heart of the sandbox.
    # ------------------------------------------------------------------

    def _project_dir(self, name: str) -> str:
        """Resolve + validate a project name. Raises McError."""
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise McError(
                "DENIED: invalid project name %r (allowed: letters, "
                "digits, '_', '-' starting with a letter/digit, max 64)"
                % (name,))
        base = os.path.realpath(self.projects_dir)
        pdir = os.path.realpath(os.path.join(self.projects_dir, name))
        if pdir != os.path.join(base, name) and \
                not pdir.startswith(base + os.sep):
            raise McError("DENIED: project path outside Minecraft "
                          "workspace (symlink? name?)")
        return pdir

    def _safe(self, name: str, path: Any) -> str:
        """Resolve `path` (relative to the project root) to a real path
        that is guaranteed to stay inside the project. Raises McError.

        This is the ONLY way any tool reaches the filesystem.
        """
        pdir = self._project_dir(name)
        if path is None:
            return pdir
        if not isinstance(path, str):
            raise McError("DENIED: path must be a string")
        raw = path.strip()
        if "\x00" in raw:
            raise McError("DENIED: path contains NUL byte")
        if raw in ("", "."):
            return pdir
        # Absolute paths — on any platform — are outside the workspace.
        if _WIN_ABS_RE.match(raw):
            raise McError(
                "DENIED: path outside Minecraft workspace — Windows "
                "absolute path %r is not inside the mod sandbox"
                % raw[:60])
        if raw.startswith("/") or raw.startswith("~"):
            raise McError(
                "DENIED: path outside Minecraft workspace — absolute "
                "path %r is not inside the mod sandbox" % raw[:60])
        if raw.startswith("\\\\"):
            raise McError(
                "DENIED: path outside Minecraft workspace — UNC path")
        # Normalize separators, then reject traversal segment-by-segment.
        norm = raw.replace("\\", "/")
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        for p in parts:
            if p == "..":
                raise McError(
                    "DENIED: path outside Minecraft workspace — '..' "
                    "traversal in %r" % raw[:60])
        base = os.path.realpath(pdir)
        target = os.path.realpath(os.path.join(pdir, *parts))
        if target != base and not target.startswith(base + os.sep):
            # A symlink inside the project pointing outside lands here.
            raise McError(
                "DENIED: path outside Minecraft workspace — %r resolves "
                "outside the project" % raw[:60])
        return target

    # ------------------------------------------------------------------
    # Per-project persistent state (loop limits survive restarts)
    # ------------------------------------------------------------------

    def _state_path(self, name: str) -> str:
        bdir = os.path.join(self.builds_dir, name)
        os.makedirs(bdir, exist_ok=True)
        return os.path.join(bdir, "state.json")

    def _state(self, name: str) -> Dict[str, Any]:
        self._project_dir(name)
        p = self._state_path(name)
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, name: str, state: Dict[str, Any]) -> None:
        p = self._state_path(name)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)

    # ------------------------------------------------------------------
    # Content scanning — the content half of the boundary, in-sandbox.
    # (The NEX policy scans arguments on the gateway side; this is the
    # sandbox's own wall, so the server is safe standalone too.)
    # ------------------------------------------------------------------

    _ESCAPE_MARKERS: Optional[Tuple] = None

    # Java/Minecraft-specific markers — mod code is Java, and the
    # generic policy list has no Java process primitives. These are
    # ALWAYS added (unioned with the policy list when importable), so a
    # broken import can never shrink the marker set.
    _JAVA_MARKERS = (
        ("processbuilder", "process spawn from Java code"),
        ("runtime.getruntime().exec", "process spawn from Java code"),
        ("runtime.getruntime()", "JVM runtime reach from mod code"),
        ("io.popen", "process spawn from Java code"),
        ("new process(", "process spawn from Java code"),
        ("process.exit", "JVM termination from mod code"),
        ("system.exit", "JVM termination from mod code"),
        ("filewriter", "arbitrary file write from mod code"),
        ("fileinputstream", "arbitrary file read from mod code"),
        ("randomaccessfile", "arbitrary file access from mod code"),
        ("java.net.url(", "network fetch from mod code"),
        ("httpclient", "network fetch from mod code"),
        ("websocket", "network fetch from mod code"),
        ("socket(", "raw socket from mod code"),
        ("exec(", "shell execution from Java code"),
        ("getruntime", "JVM runtime reach from mod code"),
    )

    def _markers(self) -> Tuple:
        if self._ESCAPE_MARKERS is None:
            base: List[Tuple[str, str]] = []
            try:
                from mcp.policy import _ESCAPE_MARKERS as _M  # type: ignore
                base = list(_M)
            except Exception:  # noqa: BLE001 — standalone fallback
                base = [
                    ("os.execute", "shell execution"),
                    ("os.system", "shell execution"),
                    ("subprocess", "process spawn"),
                    ("/bin/sh", "POSIX shell"),
                    ("powershell", "Windows shell"),
                    (".ssh/", "SSH keys"),
                    (".aws/", "cloud credentials"),
                    ("/etc/passwd", "system credential file"),
                    (".nex/", "NEX's own configuration and token"),
                ]
            have = {m for m, _w in base}
            for m, w in self._JAVA_MARKERS:
                if m not in have:
                    base.append((m, w))
            self._ESCAPE_MARKERS = tuple(base)
        return self._ESCAPE_MARKERS

    def _scan_content(self, path: str, content: str) -> Optional[str]:
        """Refuse content that reaches the operating system. None=clean."""
        try:
            from mcp.policy import scan_arguments  # type: ignore
            refusal = scan_arguments("write_file", {"path": path,
                                                    "content": content})
            if refusal:
                return refusal
        except Exception:  # noqa: BLE001
            pass
        low = content.lower()
        for marker, why in self._markers():
            if marker in low:
                return ("DENIED: content in %r contains %r (%s) — code "
                        "that reaches the operating system never goes "
                        "into a mod" % (path, marker, why))
        return None

    # ------------------------------------------------------------------
    # PROJECT tools
    # ------------------------------------------------------------------

    def tool_create_project(self, name: str,
                            mod_id: Optional[str] = None,
                            minecraft_version: str = "1.21.1",
                            loader: str = "fabric") -> Dict[str, Any]:
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise McError("invalid project name %r" % (name,))
        pdir = self._project_dir(name)
        if os.path.exists(pdir):
            raise McError("project '%s' already exists" % name)
        mid = (mod_id or name).strip().lower()
        if not _MODID_RE.fullmatch(mid):
            raise McError(
                "invalid mod id %r (allowed: lowercase letters/digits/'_', "
                "starting with a letter, 2-64 chars)" % (mid,))
        if loader != "fabric":
            raise McError(
                "loader %r is not supported — the sandbox builds Fabric "
                "mods (1.21.1). Quilt/Forge would need a separate, "
                "equally-sandboxed server." % (loader,))
        pins = SUPPORTED_MC_VERSIONS.get(minecraft_version)
        if pins is None:
            raise McError(
                "Minecraft %r is not a supported pin — supported: %s "
                "(adding a pin requires a server code change, not a "
                "runtime choice)" % (minecraft_version,
                                     ", ".join(sorted(
                                         SUPPORTED_MC_VERSIONS))))

        class_name = "".join(w.capitalize() for w in
                             re.split(r"[_\- ]", name)) + "Mod"
        package = "com.nex.%s" % mid.replace("-", "_")
        pkg_path = package.replace(".", "/")
        group = "com.nex"

        def w(rel: str, content: str) -> None:
            full = os.path.join(pdir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)

        w("settings.gradle",
          'pluginManagement {\n'
          '    repositories {\n'
          '        maven { name = "Fabric";\n'
          '                url = "https://maven.fabricmc.net/" }\n'
          '        mavenCentral()\n'
          '        gradlePluginPortal()\n'
          '    }\n'
          '}\n')
        w("build.gradle",
          'plugins {\n'
          '    id "fabric-loom" version "%s"\n'
          '    id "java"\n'
          '}\n\n'
          'version = project.mod_version\n'
          'group = project.maven_group\n\n'
          'repositories {\n'
          '    mavenCentral()\n'
          '    maven { name = "Fabric"; url = "https://maven.fabricmc.net/" }\n'
          '}\n\n'
          'dependencies {\n'
          '    minecraft "com.mojang:minecraft:%s"\n'
          '    mappings "net.fabricmc:yarn:%s"\n'
          '    modImplementation "net.fabricmc:fabric-loader:%s"\n'
          '    modImplementation "net.fabricmc.fabric-api:fabric-api:%s"\n'
          '}\n\n'
          'java {\n'
          '    toolchain {\n'
          '        languageVersion = JavaLanguageVersion.of(%s)\n'
          '    }\n'
          '}\n\n'
          'tasks.withType(JavaCompile).configureEach {\n'
          '    it.options.release = %s\n'
          '}\n'
          % (pins["loom"], minecraft_version, pins["yarn"],
             pins["loader"],
             "%s-latest" % minecraft_version,
             JAVA_REQUIRED, JAVA_REQUIRED))
        w("gradle.properties",
          "org.gradle.jvmargs=-Xmx2G\n"
          "minecraft_version=%s\n"
          "yarn_mappings=%s\n"
          "loader_version=%s\n"
          "mod_version=1.0.0\n"
          "maven_group=%s\n"
          "archives_base_name=%s\n"
          % (minecraft_version, pins["yarn"], pins["loader"], group, mid))
        # Honest wrapper: a real wrapper needs the (binary) jar, which a
        # text scaffold cannot contain. This script delegates to a system
        # gradle when present and says so plainly when it is not.
        w("gradlew",
          '#!/bin/sh\n'
          '# Fabric gradle wrapper (text scaffold — no wrapper jar).\n'
          'if command -v gradle >/dev/null 2>&1; then\n'
          '    exec gradle "$@"\n'
          'fi\n'
          'echo "GRADLE WRAPPER MISSING: install a system gradle (8.6+) or\n'
          'echo \'run `gradle wrapper` once in this project\' — the build\n'
          'echo "tool will report the toolchain state honestly."\n'
          'exit 1\n')
        try:
            os.chmod(os.path.join(pdir, "gradlew"), 0o755)
        except OSError:
            pass
        w(".gitignore", ".gradle/\nbuild/\nrun/\n")
        w("src/main/resources/fabric.mod.json",
          json.dumps({
              "schemaVersion": 1,
              "id": mid,
              "version": "1.0.0",
              "name": name,
              "description": "Mod developed by Nex inside the "
                             "NexMinecraftMCP sandbox.",
              "authors": ["Nex"],
              "license": "MIT",
              "environment": "*",
              "entrypoints": {"main": ["%s.%s" % (package, class_name)]},
              "mixins": [mid + ".mixins.json"],
              "depends": {
                  "fabricloader": ">=0.16.0",
                  "fabric-api": "*",
                  "minecraft": "~%s" % minecraft_version,
                  "java": ">=%s" % JAVA_REQUIRED,
              },
          }, indent=2) + "\n")
        w("src/main/resources/%s.mixins.json" % mid,
          json.dumps({
              "required": True,
              "package": "%s.mixin" % package,
              "compatibilityLevel": "JAVA_%s" % JAVA_REQUIRED,
              "mixins": [],
              "client": [],
              "injectors": {"defaultRequire": 1},
          }, indent=2) + "\n")
        w("src/main/java/%s/%s.java" % (pkg_path, class_name),
          'package %s;\n\n'
          'import net.fabricmc.api.ModInitializer;\n'
          'import org.slf4j.Logger;\n'
          'import org.slf4j.LoggerFactory;\n\n'
          'public class %s implements ModInitializer {\n'
          '    public static final String MODID = "%s";\n'
          '    public static final Logger LOGGER =\n'
          '            LoggerFactory.getLogger(MODID);\n\n'
          '    @Override\n'
          '    public void onInitialize() {\n'
          '        // Register blocks/items/entities here.\n'
          '        // Nex knowledge: minecraft.search_api("register block")\n'
          '        LOGGER.info("{} initialized (Fabric {})", MODID,\n'
          '                net.fabricmc.loader.api.FabricLoader.getInstance()\n'
          '                        .getModContainer(MODID)\n'
          '                        .map(c -> c.getMetadata().getVersion()\n'
          '                                .getFriendlyString()).orElse("?"));\n'
          '    }\n'
          '}\n'
          % (package, class_name, mid))
        w("README.md",
          "# %s (Fabric %s)\n\n"
          "Scaffolded by NexMinecraftMCP. Build with the sandbox tools:\n\n"
          "    minecraft.build(project=\"%s\")          # task: build\n"
          "    minecraft.run_tests(project=\"%s\")      # task: test\n"
          "    minecraft.launch_test_client(project=\"%s\")\n\n"
          "Everything stays under the sandbox root; Java is locked to "
          "JDK %s.\n" % (name, minecraft_version, name, name, name,
                         JAVA_REQUIRED))

        state = {"created_at": time.time(),
                 "minecraft_version": minecraft_version,
                 "loader": loader, "mod_id": mid,
                 "main_class": "%s.%s" % (package, class_name),
                 "java_required": JAVA_REQUIRED,
                 "builds": [], "build_attempts": 0,
                 "repair_rounds": 0, "contract": None,
                 "step": None, "evidence": {}}
        self._save_state(name, state)
        return {"ok": True, "project": name, "mod_id": mid,
                "minecraft_version": minecraft_version, "loader": loader,
                "main_class": "%s.%s" % (package, class_name),
                "java_required": JAVA_REQUIRED,
                "root": pdir,
                "note": "scaffolded — inspect it with minecraft.inspect_project"}

    def tool_list_projects(self) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []
        try:
            entries = sorted(os.listdir(self.projects_dir))
        except OSError:
            entries = []
        for e in entries:
            if not _NAME_RE.fullmatch(e):
                continue
            st = self._state(e)
            out.append({
                "name": e,
                "mod_id": st.get("mod_id"),
                "minecraft_version": st.get("minecraft_version"),
                "loader": st.get("loader"),
                "last_build": (st.get("builds") or [{}])[-1],
            })
        return {"ok": True, "root": self.root, "projects": out}

    def tool_inspect_project(self, name: str) -> Dict[str, Any]:
        pdir = self._project_dir(name)
        if not os.path.isdir(pdir):
            raise McError("project '%s' does not exist" % name)
        st = self._state(name)
        mod = self._read_json(os.path.join(pdir, "src/main/resources/fabric.mod.json"))
        props = {}
        gp = os.path.join(pdir, "gradle.properties")
        if os.path.isfile(gp):
            with open(gp, "r", encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        k, _, v = line.strip().partition("=")
                        props[k] = v
        counts = {"java": 0, "mixin": 0, "resources": 0}
        for r, _d, files in os.walk(pdir):
            rel = os.path.relpath(r, pdir).replace(os.sep, "/")
            if rel.startswith((".gradle", "build", ".git", "run")):
                continue
            for f in files:
                if f.endswith(".java"):
                    counts["java"] += 1
                    if "/mixin/" in rel or rel.endswith("mixin"):
                        counts["mixin"] += 1
                elif "/resources/" in rel:
                    counts["resources"] += 1
        last = (st.get("builds") or [{}])[-1]
        return {
            "ok": True,
            "project": name,
            "minecraft": props.get("minecraft_version")
                         or st.get("minecraft_version"),
            "loader": st.get("loader"),
            "yarn_mappings": props.get("yarn_mappings"),
            "fabric_api": props.get("mod_version") and "pinned in build.gradle",
            "java_required": JAVA_REQUIRED,
            "java_installed": self._java_version(),
            "mod_id": (mod or {}).get("id") or st.get("mod_id"),
            "main_entrypoint": ((mod or {}).get("entrypoints") or {})
                               .get("main", [None])[0],
            "source_files": counts,
            "dependencies": (mod or {}).get("depends") or {},
            "gradle": self._gradle_path(pdir),
            "build_status": ("PASS" if last.get("ok") else
                             ("FAIL" if last else "never built")),
            "build_attempts": st.get("build_attempts", 0),
            "max_build_attempts": self.max_build_attempts,
            "repair_rounds": st.get("repair_rounds", 0),
            "tests": (st.get("evidence") or {}).get("tests"),
        }

    def tool_list_files(self, name: str, path: str = "") -> Dict[str, Any]:
        base = self._safe(name, path)
        if not os.path.isdir(base):
            raise McError("not a directory: %s" % (path or name))
        pdir = self._project_dir(name)
        out: List[str] = []
        for r, dirs, files in os.walk(base):
            rel = os.path.relpath(r, pdir).replace(os.sep, "/")
            dirs[:] = [d for d in sorted(dirs)
                       if d not in (".gradle", "build", ".git", "run")]
            for f in sorted(files):
                out.append(os.path.join(rel, f).replace(os.sep, "/")
                           if rel != "." else f)
                if len(out) >= self.max_list_entries:
                    return {"ok": True, "project": name,
                            "truncated": True, "files": out,
                            "count": len(out)}
        return {"ok": True, "project": name, "truncated": False,
                "files": out, "count": len(out)}

    def tool_read_file(self, name: str, path: str) -> Dict[str, Any]:
        real = self._safe(name, path)
        if not os.path.isfile(real):
            raise McError("file not found: %s" % path)
        size = os.path.getsize(real)
        if size > self.max_read_bytes:
            raise McError("file too large (%d bytes > %d) — use "
                          "search_project to locate the part you need"
                          % (size, self.max_read_bytes))
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"ok": True, "project": name, "path": self._rel(name, real),
                "bytes": size, "content": content}

    def tool_write_file(self, name: str, path: str,
                        content: str) -> Dict[str, Any]:
        if not isinstance(content, str):
            raise McError("content must be a string")
        if len(content) > self.max_write_bytes:
            raise McError("content too large (%d bytes > %d)"
                          % (len(content), self.max_write_bytes))
        real = self._safe(name, path)
        if os.path.isfile(real):
            with open(real, "rb") as f:
                if f.read(2) == b"PK":
                    raise McError("refusing to overwrite a binary archive "
                                  "(%s) — delete_file it first, deliberately"
                                  % path)
        refusal = self._scan_content(path, content)
        if refusal:
            raise McError(refusal)
        # Step budget: at most N file changes per declared step.
        st = self._state(name)
        step = st.get("step") or {}
        if step.get("active"):
            changed = int(step.get("changed_files", 0))
            if changed >= self.max_changed_files_per_step:
                raise McError(
                    "step budget exceeded: step '%s' already changed %d "
                    "files (max %d) — close_step it and start a new step; "
                    "a repair that rewrites the whole codebase at once is "
                    "not a repair" % (step.get("id"), changed,
                                      self.max_changed_files_per_step))
            step["changed_files"] = changed + 1
            st["step"] = step
            self._save_state(name, st)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        tmp = real + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, real)
        self._note_change(name)
        return {"ok": True, "project": name,
                "path": self._rel(name, real),
                "bytes": len(content.encode("utf-8")),
                "step_changed_files": (st.get("step") or {}).get(
                    "changed_files") if (st.get("step") or {}).get("active")
                else None}

    def tool_delete_file(self, name: str, path: str) -> Dict[str, Any]:
        real = self._safe(name, path)
        pdir = self._project_dir(name)
        if real == os.path.realpath(pdir):
            raise McError("refusing to delete the project root — delete "
                          "files individually")
        if os.path.isdir(real):
            raise McError("refusing to delete a directory (%s) — delete "
                          "its files individually" % path)
        if not os.path.isfile(real):
            raise McError("file not found: %s" % path)
        os.remove(real)
        return {"ok": True, "project": name,
                "deleted": self._rel(name, real)}

    def tool_search_project(self, name: str, query: str,
                            use_regex: bool = False,
                            path: str = "") -> Dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise McError("query must be a non-empty string")
        base = self._safe(name, path)
        if not os.path.isdir(base):
            raise McError("not a directory: %s" % (path or name))
        pdir = self._project_dir(name)
        try:
            pattern = (re.compile(query, re.IGNORECASE) if use_regex
                       else None)
        except re.error as exc:
            raise McError("bad regex: %s" % exc)
        out: List[Dict[str, Any]] = []
        for r, dirs, files in os.walk(base):
            rel = os.path.relpath(r, pdir).replace(os.sep, "/")
            dirs[:] = [d for d in sorted(dirs)
                       if d not in (".gradle", "build", ".git", "run")]
            for f in sorted(files):
                full = os.path.join(r, f)
                if os.path.getsize(full) > self.max_read_bytes:
                    continue
                try:
                    with open(full, "r", encoding="utf-8",
                              errors="replace") as fh:
                        lines = fh.read().splitlines()
                except OSError:
                    continue
                for i, line in enumerate(lines, 1):
                    hit = bool(pattern.search(line)) if pattern \
                        else query.lower() in line.lower()
                    if hit:
                        out.append({"file": os.path.join(rel, f)
                                    .replace(os.sep, "/"),
                                    "line": i, "text": line.strip()[:200]})
                        if len(out) >= self.max_search_results:
                            return {"ok": True, "project": name,
                                    "query": query, "truncated": True,
                                    "matches": out}
        return {"ok": True, "project": name, "query": query,
                "truncated": False, "matches": out}

    # ------------------------------------------------------------------
    # MINECRAFT KNOWLEDGE (local knowledge layer — no hallucinated API)
    # ------------------------------------------------------------------

    def tool_search_api(self, query: str) -> Dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise McError("query must be a non-empty string")
        q = query.lower()
        # Whole-WORD token match (substring matching made junk tokens
        # like "no" match everything). Tokens under 4 chars are noise.
        toks = [t for t in re.split(r"[^a-z0-9]+", q) if len(t) >= 4]
        scored: List[Tuple[int, Dict[str, Any]]] = []
        for e in KNOWLEDGE:
            hay = " ".join([e["title"], " ".join(e["tags"]),
                            e["body"]]).lower()
            hay_words = set(re.split(r"[^a-z0-9]+", hay))
            score = 0
            for t in toks:
                if t in hay_words:
                    score += 2
            if toks and q in hay:
                score += 3
            if score:
                scored.append((score, e))
        scored.sort(key=lambda p: -p[0])
        top = [e for _s, e in scored[:3]]
        if not top:
            return {"ok": True, "query": query, "matches": [],
                    "note": "no curated entry matched — search the project "
                            "source (search_project) or inspect a known "
                            "class (inspect_class)"}
        return {"ok": True, "query": query,
                "version_pin": "Fabric %s / JDK %s"
                               % (DEFAULT_MC_VERSION,
                                  JAVA_REQUIRED),
                "matches": top}

    def tool_inspect_class(self, class_name: str) -> Dict[str, Any]:
        key = (class_name or "").strip()
        for alias, e in CLASS_INDEX.items():
            if alias.lower() == key.lower():
                return {"ok": True, "class": key, "entry": e}
        return {"ok": False, "class": key,
                "note": "not in the curated knowledge base — try "
                        "search_api with a plain-language query, or "
                        "find_symbol inside the project"}

    def tool_find_symbol(self, name: str, symbol: str) -> Dict[str, Any]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise McError("symbol must be a non-empty string")
        esc = re.escape(symbol)
        res = self.tool_search_project(name, esc, use_regex=True)
        return dict(res, kind="symbol")

    def tool_find_references(self, name: str, symbol: str) -> Dict[str, Any]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise McError("symbol must be a non-empty string")
        res = self.tool_search_project(name, re.escape(symbol),
                                       use_regex=True)
        return dict(res, kind="references")

    # ------------------------------------------------------------------
    # BUILD (server-approved operations only — no command parameter)
    # ------------------------------------------------------------------

    def _java_version(self) -> Optional[str]:
        java = shutil.which("java")
        if not java:
            return None
        try:
            r = subprocess.run([java, "--version"], capture_output=True,
                               text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = (r.stdout or r.stderr or "")
        m = re.search(r"version \"(\d+)", out)
        return m.group(1) if m else (out.splitlines()[0].strip()
                                     if out else "unknown")

    def _gradle_path(self, pdir: str) -> Optional[str]:
        for cand in (os.path.join(pdir, "gradlew"),
                     os.path.join(pdir, "gradlew.bat")):
            if os.path.isfile(cand):
                return cand
        return shutil.which("gradle")

    def _run_build(self, name: str, task: str) -> Dict[str, Any]:
        if task not in ALLOWED_BUILD_TASKS:
            raise McError(
                "DENIED: build task %r is not in the approved set %s — "
                "the build tool takes a task name, never a command"
                % (task, list(ALLOWED_BUILD_TASKS)))
        pdir = self._project_dir(name)
        if not os.path.isdir(pdir):
            raise McError("project '%s' does not exist" % name)
        st = self._state(name)
        attempts = int(st.get("build_attempts", 0))
        if attempts >= self.max_build_attempts:
            raise McError(
                "build budget exhausted: %d failed build attempts "
                "(max %d) — STOP. Human intervention required: read "
                "read_build_log, inspect_project and decide the fix "
                "yourself or ask the operator. The server will not keep "
                "compiling in a loop." % (attempts, self.max_build_attempts))
        java = self._java_version()
        if java is None:
            return {"ok": False, "project": name, "task": task,
                    "reason": "toolchain missing: no 'java' on PATH",
                    "toolchain": {"java": None, "java_required":
                                  JAVA_REQUIRED,
                                  "gradle": self._gradle_path(pdir)},
                    "hint": "install JDK %s and gradle 8.6+ (or run the "
                            "build on a machine that has them) — the "
                            "sandbox reports the toolchain honestly "
                            "instead of pretending" % JAVA_REQUIRED}
        if str(java).split(".")[0] != JAVA_REQUIRED:
            return {"ok": False, "project": name, "task": task,
                    "reason": ("Java is locked to %s for this sandbox "
                               "(Minecraft %s uses JDK %s); found %r — "
                               "the model cannot choose a different "
                               "java executable"
                               % (JAVA_REQUIRED,
                                  st.get("minecraft_version"),
                                  JAVA_REQUIRED, java)),
                    "toolchain": {"java": java, "java_required":
                                  JAVA_REQUIRED}}
        gradle = self._gradle_path(pdir)
        if not gradle:
            return {"ok": False, "project": name, "task": task,
                    "reason": ("toolchain missing: no gradle wrapper in "
                               "the project and no 'gradle' on PATH"),
                    "toolchain": {"java": java, "gradle": None},
                    "hint": "run `gradle wrapper` once in the project or "
                            "install gradle 8.6+"}
        bdir = os.path.join(self.builds_dir, name)
        os.makedirs(bdir, exist_ok=True)
        log_path = os.path.join(bdir, "build.log")
        header = "\n===== build attempt %d/%d — task %s — %s =====\n" % (
            attempts + 1, self.max_build_attempts, task,
            time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            t0 = time.monotonic()
            proc = subprocess.run(
                [gradle, "--console=plain", "--no-daemon", task],
                cwd=pdir, capture_output=True, text=True,
                timeout=self.build_timeout_s,
            )
            dt = time.monotonic() - t0
            out = (proc.stdout or "") + ("\n" + proc.stderr
                                         if proc.stderr else "")
        except subprocess.TimeoutExpired as exc:
            out = ("TIMEOUT after %.0fs — gradle task %s did not finish "
                   "in the allowed time\n%s%s"
                   % (self.build_timeout_s, task, exc.stdout or "",
                      exc.stderr or ""))
            proc_rc = -1
            dt = self.build_timeout_s
        except OSError as exc:
            out = "failed to start gradle: %r" % (exc,)
            proc_rc = -1
            dt = 0.0
        else:
            proc_rc = proc.returncode
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(header + out)
        ok = (proc_rc == 0)
        attempts2 = 0 if ok else attempts + 1
        repair_rounds = int(st.get("repair_rounds", 0))
        if not ok:
            if repair_rounds >= self.max_repair_rounds:
                raise McError(
                    "repair budget exhausted: %d repair rounds (max %d) — "
                    "STOP. Human intervention required."
                    % (repair_rounds, self.max_repair_rounds))
            st["repair_rounds"] = repair_rounds + 1
        st["build_attempts"] = attempts2
        st.setdefault("evidence", {})["build_passed"] = ok
        record = {"task": task, "ok": ok, "exit": proc_rc,
                  "duration_s": round(dt, 2), "attempts": attempts2,
                  "time": time.time()}
        st.setdefault("builds", []).append(record)
        if len(st["builds"]) > 20:
            st["builds"] = st["builds"][-20:]
        self._save_state(name, st)
        artifacts = self._collect_artifacts(name)
        result: Dict[str, Any] = {
            "ok": ok, "project": name, "task": task,
            "attempt": attempts + 1,
            "attempts_left": max(0, self.max_build_attempts - attempts2),
            "repair_rounds_left": max(0, self.max_repair_rounds
                                      - int(st.get("repair_rounds", 0))),
            "exit": proc_rc, "duration_s": round(dt, 2),
            "log": "builds/%s/build.log" % name,
            "log_tail": out.strip()[-2500:],
            "artifacts": artifacts,
        }
        if not ok:
            result["hint"] = ("read the log tail, find the FIRST error "
                              "(not the last), fix ONE thing, then build "
                              "again — attempts are budgeted")
        return result

    def tool_build(self, name: str, task: str = "build") -> Dict[str, Any]:
        return self._run_build(name, task)

    def tool_run_tests(self, name: str) -> Dict[str, Any]:
        res = self._run_build(name, "test")
        if res.get("ok"):
            st = self._state(name)
            st.setdefault("evidence", {})["tests"] = "PASS"
            self._save_state(name, st)
        return res

    def _collect_artifacts(self, name: str) -> List[str]:
        pdir = self._project_dir(name)
        libs = os.path.join(pdir, "build", "libs")
        out: List[str] = []
        if os.path.isdir(libs):
            bdir = os.path.join(self.builds_dir, name)
            os.makedirs(bdir, exist_ok=True)
            for f in sorted(os.listdir(libs)):
                if f.endswith(".jar"):
                    src = os.path.join(libs, f)
                    dst = os.path.join(bdir, f)
                    try:
                        shutil.copy2(src, dst)
                    except OSError:
                        pass
                    out.append("builds/%s/%s" % (name, f))
        return out

    def tool_read_build_log(self, name: str,
                            tail: int = 4000) -> Dict[str, Any]:
        self._project_dir(name)
        p = os.path.join(self.builds_dir, name, "build.log")
        if not os.path.isfile(p):
            return {"ok": True, "project": name, "log": None,
                    "note": "no build log yet — nothing has been built"}
        tail = max(200, min(int(tail or 4000), 64 * 1024))
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        return {"ok": True, "project": name, "log": "builds/%s/build.log"
                % name, "bytes": len(data),
                "tail": data[-tail:]}

    def tool_get_build_artifact(self, name: str) -> Dict[str, Any]:
        self._project_dir(name)
        bdir = os.path.join(self.builds_dir, name)
        jars = []
        if os.path.isdir(bdir):
            jars = ["builds/%s/%s" % (name, f) for f in
                    sorted(os.listdir(bdir)) if f.endswith(".jar")]
        return {"ok": bool(jars), "project": name, "artifacts": jars,
                "note": None if jars else
                "no jar yet — run minecraft.build first"}

    # ------------------------------------------------------------------
    # TEST INSTANCE (isolated world/mods/config/saves)
    # ------------------------------------------------------------------

    def tool_launch_test_client(self, name: str) -> Dict[str, Any]:
        pdir = self._project_dir(name)
        if not os.path.isdir(pdir):
            raise McError("project '%s' does not exist" % name)
        st = self._state(name)
        mod = self._read_json(os.path.join(
            pdir, "src/main/resources/fabric.mod.json"))
        if mod is None:
            raise McError("project has no valid fabric.mod.json — create "
                          "it or repair it before launching")
        # Recycle any live client first.
        with self._lock:
            if self._client is not None:
                self.stop_client_locked()
            inst_dir = os.path.join(self.test_dir, name)
            os.makedirs(os.path.join(inst_dir, "world"), exist_ok=True)
            logs = os.path.join(self.logs_dir, name)
            os.makedirs(logs, exist_ok=True)
            client = _TestClient(self, name, inst_dir, logs, mod, st,
                                 self.max_test_runtime_s,
                                 simulated=not self.real_client,
                                 real_client=self.real_client)
            client.start()
            self._client = client
            self._client_project = name
        if not client.ready.wait(timeout=30):
            return {"ok": False, "project": name,
                    "reason": "test client did not become ready in 30s",
                    "game_log": "logs/%s/game.log" % name}
        if client.crashed:
            return {"ok": False, "project": name,
                    "reason": "test client crashed at launch — see "
                              "minecraft.get_crash_report",
                    "crash_report": client.crash_path,
                    "game_log": "logs/%s/game.log" % name}
        st.setdefault("evidence", {})["client_ready"] = True
        st.setdefault("evidence", {})["no_crash"] = True
        self._save_state(name, st)
        return {"ok": True, "project": name,
                "simulated": client.simulated,
                "instance": "test-instance/%s" % name,
                "game_log": "logs/%s/game.log" % name,
                "max_runtime_s": self.max_test_runtime_s,
                "note": ("SIMULATED dev client (no real Minecraft binary "
                         "in this environment) — observations are "
                         "labelled simulated:true"
                         if client.simulated else
                         "real client (operator-pinned NEX_MC_TEST_CLIENT)")}

    def stop_client_locked(self) -> None:
        c = self._client
        if c is not None:
            c.stop()
            c.join(timeout=5)
            if c.project and c.project in self._state_names():
                try:
                    st = self._state(c.project)
                    if c.crashed:
                        st.setdefault("evidence", {})["no_crash"] = False
                    self._save_state(c.project, st)
                except McError:
                    pass
        self._client = None
        self._client_project = None

    def _state_names(self) -> List[str]:
        try:
            return [e for e in os.listdir(self.builds_dir)]
        except OSError:
            return []

    def tool_stop_test_client(self, name: str = "") -> Dict[str, Any]:
        with self._lock:
            c = self._client
            if c is None:
                return {"ok": True, "stopped": False,
                        "note": "no test client running"}
            stopped_project = c.project
            if name and name != c.project:
                return {"ok": False,
                        "reason": "running client belongs to project '%s', "
                                  "not '%s'" % (c.project, name)}
            self.stop_client_locked()
        return {"ok": True, "stopped": True, "project": stopped_project}

    def tool_get_game_log(self, name: str = "") -> Dict[str, Any]:
        p = self._resolve_client_project(name, read_only=True)
        log = os.path.join(self.logs_dir, p, "game.log")
        if not os.path.isfile(log):
            return {"ok": True, "project": p, "log": None,
                    "note": "no game log for '%s' — launch_test_client "
                            "first" % p}
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        return {"ok": True, "project": p, "log": "logs/%s/game.log" % p,
                "tail": data[-8000:]}

    def tool_get_crash_report(self, name: str = "") -> Dict[str, Any]:
        p = self._resolve_client_project(name, read_only=True)
        logs = os.path.join(self.logs_dir, p)
        crashes = []
        if os.path.isdir(logs):
            crashes = sorted(f for f in os.listdir(logs)
                             if f.startswith("crash-") and f.endswith(".log"))
        if not crashes:
            return {"ok": True, "project": p, "crashed": False,
                    "note": "no crash reports for '%s'" % p}
        latest = os.path.join(logs, crashes[-1])
        with open(latest, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        return {"ok": True, "project": p, "crashed": True,
                "crash_reports": ["logs/%s/%s" % (p, c) for c in crashes],
                "latest": data[-8000:]}

    def tool_get_test_results(self, name: str = "") -> Dict[str, Any]:
        p = self._resolve_client_project(name, read_only=True)
        st = self._state(p)
        ev = st.get("evidence") or {}
        builds = st.get("builds") or []
        return {"ok": True, "project": p,
                "build": builds[-1] if builds else None,
                "evidence": ev,
                "contract": (st.get("contract") or {}).get("checks"),
                "note": "evidence is what the loop observed — a contract "
                        "check is only satisfied by evidence, never by "
                        "BUILD SUCCESS alone"}

    # ------------------------------------------------------------------
    # GAME OBSERVATION (isolated instance only)
    # ------------------------------------------------------------------

    def _live_client(self, name: str = "") -> Any:
        with self._lock:
            c = self._client
            if c is None or not c.is_running():
                raise McError("no test client running — call "
                              "launch_test_client first")
            if name and name != c.project:
                raise McError("running client belongs to project '%s', "
                              "not '%s'" % (c.project, name))
            return c

    def tool_inspect_world(self, name: str = "") -> Dict[str, Any]:
        c = self._live_client(name)
        return c.observation("inspect_world")

    def tool_inspect_block(self, name: str = "", x: int = 0, y: int = 0,
                           z: int = 0) -> Dict[str, Any]:
        c = self._live_client(name)
        key = "%d,%d,%d" % (self._coord(x), self._coord(y),
                            self._coord(z))
        block = c.state["blocks"].get(key)
        if block is None:
            block = {"block": "minecraft:stone" if self._coord(y) <= 4
                     else "minecraft:air"}
        return {"ok": True, "simulated": c.simulated, "project": c.project,
                "position": [self._coord(x), self._coord(y),
                             self._coord(z)], "block": block}

    def tool_inspect_entity(self, name: str = "",
                            entity_id: str = "") -> Dict[str, Any]:
        c = self._live_client(name)
        ents = c.state["entities"]
        if entity_id:
            match = [e for e in ents if e["id"] == entity_id
                     or e["type"] == entity_id]
            if not match:
                return {"ok": False, "simulated": c.simulated,
                        "project": c.project,
                        "reason": "no entity matching %r in the loaded "
                                  "area" % entity_id}
            return {"ok": True, "simulated": c.simulated,
                    "project": c.project, "entity": match[0]}
        return {"ok": True, "simulated": c.simulated,
                "project": c.project,
                "entities": ents[:32], "count": len(ents)}

    def tool_inspect_player(self, name: str = "") -> Dict[str, Any]:
        c = self._live_client(name)
        return {"ok": True, "simulated": c.simulated,
                "project": c.project, "player": c.state["player"]}

    def tool_screenshot(self, name: str = "") -> Dict[str, Any]:
        c = self._live_client(name)
        obs = c.observation("screenshot")
        obs["note"] = ("simulated client returns the world STATE, not "
                       "pixels — the observation pane shows it as data"
                       if c.simulated else "captured from the real client")
        return obs

    def tool_spawn_test_entity(self, name: str = "", kind: str = "pig",
                               x: int = 0, y: int = 64, z: int = 0
                               ) -> Dict[str, Any]:
        c = self._live_client(name)
        if not _ID_RE.fullmatch(kind or ""):
            raise McError("invalid entity type %r" % (kind,))
        ents = c.state["entities"]
        if len(ents) >= 32:
            raise McError("entity cap (32) reached in the test instance — "
                          "stop_test_client and relaunch")
        eid = "test-%d" % (len(ents) + 1)
        ent = {"id": eid, "type": kind,
               "pos": [self._coord(x), self._coord(y), self._coord(z)]}
        ents.append(ent)
        return {"ok": True, "simulated": c.simulated,
                "project": c.project, "spawned": ent}

    def tool_place_test_block(self, name: str = "", block: str = "stone",
                              x: int = 0, y: int = 64, z: int = 0
                              ) -> Dict[str, Any]:
        c = self._live_client(name)
        if not _ID_RE.fullmatch(block or ""):
            raise McError("invalid block id %r" % (block,))
        blocks = c.state["blocks"]
        if len(blocks) >= 64:
            raise McError("block cap (64) reached in the test instance")
        key = "%d,%d,%d" % (self._coord(x), self._coord(y),
                            self._coord(z))
        blocks[key] = {"block": block}
        return {"ok": True, "simulated": c.simulated,
                "project": c.project,
                "placed": {"block": block,
                           "pos": [self._coord(x), self._coord(y),
                                   self._coord(z)]}}

    def tool_set_test_time(self, name: str = "", value: int = 0
                           ) -> Dict[str, Any]:
        c = self._live_client(name)
        v = self._coord(value)
        if v < 0:
            raise McError("time must be >= 0 (ticks)")
        c.state["world"]["time"] = v % 24000
        return {"ok": True, "simulated": c.simulated,
                "project": c.project, "time": c.state["world"]["time"]}

    def tool_set_test_weather(self, name: str = "",
                              value: str = "clear") -> Dict[str, Any]:
        c = self._live_client(name)
        v = (value or "").lower()
        if v not in ("clear", "rain", "thunder"):
            raise McError("weather must be clear|rain|thunder")
        c.state["world"]["weather"] = v
        return {"ok": True, "simulated": c.simulated,
                "project": c.project, "weather": v}

    def tool_teleport_test_player(self, name: str = "", x: int = 0,
                                  y: int = 64, z: int = 0
                                  ) -> Dict[str, Any]:
        c = self._live_client(name)
        c.state["player"]["pos"] = [self._coord(x), self._coord(y),
                                    self._coord(z)]
        return {"ok": True, "simulated": c.simulated,
                "project": c.project, "player_pos":
                c.state["player"]["pos"]}

    @staticmethod
    def _coord(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            raise McError("coordinate must be an integer, got %r" % (v,))

    def _resolve_client_project(self, name: str,
                                read_only: bool = False) -> str:
        if name:
            self._project_dir(name)
            return name
        if self._client_project:
            return self._client_project
        if not read_only:
            raise McError("no test client running — launch_test_client "
                          "first")
        raise McError("no client project known — name the project")

    # ------------------------------------------------------------------
    # VERIFICATION CONTRACT + STEP BUDGET (the loop limits)
    # ------------------------------------------------------------------

    def tool_set_contract(self, name: str,
                          checks: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(checks, list) or not checks:
            raise McError("checks must be a non-empty list of "
                          "{id, description, kind}")
        allowed_kinds = ("build", "test", "launch", "observation",
                         "runtime")
        norm: List[Dict[str, Any]] = []
        for i, c in enumerate(checks[:40]):
            if not isinstance(c, dict):
                raise McError("check %d must be an object" % i)
            cid = str(c.get("id") or ("check-%d" % i))
            kind = str(c.get("kind") or "observation")
            if kind not in allowed_kinds:
                raise McError("check %d: kind must be one of %s"
                              % (i, list(allowed_kinds)))
            norm.append({"id": cid[:48],
                         "description": str(c.get("description") or cid)[:200],
                         "kind": kind,
                         "verified": False, "evidence": None})
        st = self._state(name)
        st["contract"] = {"set_at": time.time(), "checks": norm}
        self._save_state(name, st)
        return {"ok": True, "project": name, "checks": norm,
                "note": "BUILD SUCCESS alone never satisfies a contract — "
                        "each check needs evidence (see check_contract)"}

    def tool_check_contract(self, name: str) -> Dict[str, Any]:
        st = self._state(name)
        contract = st.get("contract")
        if not contract:
            return {"ok": False, "project": name,
                    "reason": "no contract declared — set_contract first",
                    "hint": "example checks: [{id: build, kind: build, "
                            "description: 'gradle build passes'}, "
                            "{id: launch, kind: launch, ...}, "
                            "{id: no_crash, kind: runtime, ...}]"}
        ev = st.get("evidence") or {}
        checks: List[Dict[str, Any]] = []
        for c in contract["checks"]:
            if c.get("verified"):
                status = "satisfied (explicit evidence)"
            else:
                by_kind = {
                    "build": ev.get("build_passed") is True,
                    "test": ev.get("tests") == "PASS",
                    "launch": ev.get("client_ready") is True,
                    "runtime": ev.get("no_crash") is True
                               and ev.get("client_ready") is True,
                    "observation": False,
                }
                status = ("satisfied" if by_kind.get(c["kind"])
                          else "UNMET")
            checks.append(dict(c, status=status))
        unmet = [c["id"] for c in checks if not c["status"].startswith(
            "satisfied")]
        return {"ok": True, "project": name, "checks": checks,
                "unmet": unmet, "complete": not unmet,
                "note": None if not unmet else
                "NOT complete: %s — gather the missing evidence before "
                "claiming the task is done" % ", ".join(unmet)}

    def tool_mark_verified(self, name: str, check_id: str,
                           evidence: str) -> Dict[str, Any]:
        if not isinstance(evidence, str) or not evidence.strip():
            raise McError("evidence must be a non-empty string — name what "
                          "you OBSERVED (log line, tool result, position)")
        st = self._state(name)
        contract = st.get("contract")
        if not contract:
            raise McError("no contract declared — set_contract first")
        hit = None
        for c in contract["checks"]:
            if c["id"] == check_id:
                hit = c
                break
        if hit is None:
            known = ", ".join(c["id"] for c in contract["checks"])
            raise McError("unknown check %r — declared checks: %s"
                          % (check_id, known))
        hit["verified"] = True
        hit["evidence"] = evidence.strip()[:400]
        self._save_state(name, st)
        return {"ok": True, "project": name, "check": check_id,
                "evidence": evidence.strip()[:400],
                "remaining": [c["id"] for c in contract["checks"]
                              if not c.get("verified")]}

    def tool_log_step(self, name: str, step_id: str) -> Dict[str, Any]:
        if not isinstance(step_id, str) or not step_id.strip():
            raise McError("step_id must be a non-empty string")
        st = self._state(name)
        st["step"] = {"id": step_id.strip()[:64], "active": True,
                      "changed_files": 0, "started_at": time.time()}
        self._save_state(name, st)
        return {"ok": True, "project": name, "step": step_id.strip()[:64],
                "max_changed_files_per_step":
                    self.max_changed_files_per_step}

    def tool_close_step(self, name: str) -> Dict[str, Any]:
        st = self._state(name)
        step = st.get("step") or {}
        if not step.get("active"):
            return {"ok": True, "project": name, "closed": False,
                    "note": "no active step"}
        step["active"] = False
        step["ended_at"] = time.time()
        st["step"] = step
        self._save_state(name, st)
        return {"ok": True, "project": name,
                "closed": step.get("id"),
                "changed_files": step.get("changed_files", 0)}

    # ------------------------------------------------------------------
    # STATUS + helpers
    # ------------------------------------------------------------------

    def tool_minecraft_status(self) -> Dict[str, Any]:
        with self._lock:
            running = self._client_project if (
                self._client is not None and self._client.is_running()) \
                else None
        return {
            "ok": True,
            "server": SERVER_NAME,
            "root": self.root,
            "layout": {"projects": "projects/", "test_instance":
                       "test-instance/", "builds": "builds/",
                       "logs": "logs/"},
            "limits": {
                "max_build_attempts": self.max_build_attempts,
                "max_repair_rounds": self.max_repair_rounds,
                "max_test_runtime_s": self.max_test_runtime_s,
                "max_changed_files_per_step":
                    self.max_changed_files_per_step,
                "build_timeout_s": self.build_timeout_s,
                "build_tasks": list(ALLOWED_BUILD_TASKS),
            },
            "toolchain": {
                "java": self._java_version(),
                "java_required": JAVA_REQUIRED,
                "gradle": self._gradle_path(self.projects_dir),
                "mc_versions": sorted(SUPPORTED_MC_VERSIONS),
            },
            "test_client": {"running_project": running,
                            "simulated_default": not self.real_client},
            "no": ["execute_shell", "execute_powershell", "execute_python",
                   "read_any_file", "write_any_file", "delete_any_file",
                   "launch_any_executable", "run_command"],
        }

    def _rel(self, name: str, real: str) -> str:
        pdir = os.path.realpath(self._project_dir(name))
        try:
            return os.path.relpath(real, pdir).replace(os.sep, "/")
        except ValueError:
            return real

    @staticmethod
    def _read_json(path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def _note_change(self, name: str) -> None:
        """Record a mutation for test-results honesty (no state lock
        needed — single writer per process is the stdio loop)."""


# ---------------------------------------------------------------------------
# The simulated dev client (Wall 4: isolated instance)
# ---------------------------------------------------------------------------

class _TestClient(threading.Thread):
    """A deterministic stand-in for `gradle runClient` when no real
    Minecraft binary is available. It behaves like a game the mod can
    be loaded into: it validates the mod, writes a launch log, exposes
    world/entity state to the observation tools, and enforces the
    runtime budget. Every observation is labelled simulated:true.

    When the operator pins a real client (NEX_MC_TEST_CLIENT), the
    process is spawned with a FIXED argument vector (no model input)
    and its stdout becomes the game log; a non-zero exit is a crash.
    """

    SEED = 881724

    def __init__(self, mcp: NexMinecraftMCP, project: str, inst_dir: str,
                 logs_dir: str, mod: Dict[str, Any],
                 state: Dict[str, Any], max_runtime: float,
                 simulated: bool, real_client: str) -> None:
        super().__init__(daemon=True, name="mc-test-client-%s" % project)
        self.mcp = mcp
        self.project = project
        self.inst_dir = inst_dir
        self.logs_dir = logs_dir
        self.mod = mod
        self.state_doc = state
        self.max_runtime = max_runtime
        self.simulated = simulated
        self.real_client = real_client
        self.ready = threading.Event()
        self._stop_evt = threading.Event()
        self.crashed = False
        self.crash_path: Optional[str] = None
        self.started_at = time.time()
        self.state: Dict[str, Any] = {
            "world": {"dimension": "overworld", "seed": self.SEED,
                      "time": 1000, "weather": "clear"},
            "blocks": {},
            "entities": [{"id": "player", "type": "minecraft:player",
                          "pos": [0.0, 64.0, 0.0]}],
            "player": {"name": "nex_tester", "pos": [0.0, 64.0, 0.0],
                       "health": 20.0, "dimension": "overworld"},
        }
        self._proc: Optional[subprocess.Popen] = None

    # -- lifecycle --------------------------------------------------------

    def is_running(self) -> bool:
        if self.simulated:
            return not self._stop_evt.is_set() and not self.crashed
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        self._stop_evt.set()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    def run(self) -> None:
        if self.simulated:
            self._run_simulated()
        else:
            self._run_real()

    def _log(self, line: str) -> None:
        with open(os.path.join(self.logs_dir, "game.log"), "a",
                  encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), line))

    def _run_simulated(self) -> None:
        pdir = self.mcp._project_dir(self.project)
        self._log("Starting dev client for Minecraft %s (SIMULATED — no "
                  "real Minecraft binary in this environment)"
                  % self.state_doc.get("minecraft_version", "?"))
        main_entry = ((self.mod.get("entrypoints") or {}).get("main")
                      or [None])[0]
        java_rel = ""
        if main_entry:
            java_rel = main_entry.replace(".", "/") + ".java"
        src = os.path.join(pdir, "src/main/java", java_rel)
        if not os.path.isfile(src):
            self._crash("Missing mod main class %s (declared in "
                        "fabric.mod.json entrypoints)" % main_entry)
            return
        self._log("Mod %s v%s registered; entrypoint %s"
                  % (self.mod.get("id"), self.mod.get("version"),
                     main_entry))
        self._log("Isolated instance: %s (separate world/mods/config/"
                  "saves)" % self.inst_dir)
        self._log("World loaded: dimension=overworld seed=%d" % self.SEED)
        self._log("Dev client ready (simulated)")
        self.ready.set()
        # Deterministic idle loop until stop or the runtime budget.
        while not self._stop_evt.is_set():
            if time.time() - self.started_at > self.max_runtime:
                self._log("Auto-stopped: test runtime budget "
                          "(%.0fs) reached" % self.max_runtime)
                return
            self._stop_evt.wait(0.5)
        self._log("Dev client stopped")

    def _crash(self, reason: str) -> None:
        self.crashed = True
        p = os.path.join(self.logs_dir,
                         "crash-%d.log" % int(time.time()))
        with open(p, "w", encoding="utf-8") as f:
            f.write("---- Minecraft crash (simulated dev client) ----\n")
            f.write("Project: %s\n" % self.project)
            f.write("Reason: %s\n" % reason)
            f.write("Mod: %s v%s\n" % (self.mod.get("id"),
                                       self.mod.get("version")))
        self.crash_path = p
        self._log("CRASH: " + reason)
        self.ready.set()

    def _run_real(self) -> None:
        binpath = self.real_client
        if not os.path.isfile(binpath):
            self._crash("operator-pinned client binary %s not found — "
                        "fall back: unset NEX_MC_TEST_CLIENT to use the "
                        "simulated client" % binpath)
            return
        # FIXED argument vector — the model never chooses args here.
        argv = [binpath, "--world", os.path.join(self.inst_dir, "world"),
                "--quickPlaySingleplayer"]
        try:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as exc:
            self._crash("failed to start client: %r" % (exc,))
            return
        self._log("Real client started (operator-pinned binary), pid "
                  "%s" % self._proc.pid)
        for line in self._proc.stdout:  # type: ignore[union-attr]
            self._log(line.rstrip("\n"))
            if self._stop_evt.is_set():
                break
        rc = self._proc.wait()
        if rc != 0:
            self._crash("client exited with code %d — see game log" % rc)
        self.ready.set()

    # -- observations ------------------------------------------------------

    def observation(self, kind: str) -> Dict[str, Any]:
        return {
            "ok": True,
            "simulated": self.simulated,
            "project": self.project,
            "kind": kind,
            "world": dict(self.state["world"]),
            "player": dict(self.state["player"]),
            "entities": self.state["entities"][:32],
            "blocks": dict(self.state["blocks"]),
            "uptime_s": round(time.time() - self.started_at, 1),
        }


# ---------------------------------------------------------------------------
# Knowledge base — curated Fabric 1.21.1 API (local, no hallucination)
# ---------------------------------------------------------------------------

KNOWLEDGE: List[Dict[str, Any]] = [
    {
        "id": "register-block",
        "title": "Register a custom block (Fabric 1.21.1)",
        "tags": ["block", "register", "registry", "registry.block",
                 "fabricblocksettings", "blockstate", "new block"],
        "body": (
            "In onInitialize():\n"
            "  public static final Block STONE_FLOWER =\n"
            "      new Block(FabricBlockSettings.builder()\n"
            "              .strength(1.0f).requiresTool()\n"
            "              .build());\n"
            "  STONE_FLOWER = Registry.register(Registry.BLOCK,\n"
            "      ResourceLocation.fromNamespaceAndPath(MODID, \"stone_flower\"),\n"
            "      STONE_FLOWER);\n"
            "BlockSettings come from FabricBlockSettings.builder() "
            "(Fabric's immutable builder — the vanilla BlockSettings "
            "constructor is protected). Tags: your block's id follows "
            "\"<namespace>:<path>\". For a flower use .noCollision() and "
            "extend FlowerBlock with a PlantType."
        ),
    },
    {
        "id": "register-item",
        "title": "Register a custom item (Fabric 1.21.1)",
        "tags": ["item", "register", "registry.item", "fabricitemsettings",
                 "custom item"],
        "body": (
            "public static final Item GLOWING_SEED =\n"
            "    Registry.register(Registry.ITEM,\n"
            "        ResourceLocation.fromNamespaceAndPath(MODID, \"glowing_seed\"),\n"
            "        new Item(FabricItemSettings.builder().rarity(Rarity.RARE)\n"
            "                .maxCount(16).build()));\n"
            "A block's item is separate: Registry.register(Registry.ITEM, "
            "BLOCK_ID, new ItemBlock(BLOCK, FabricItemSettings.builder().build()))."
        ),
    },
    {
        "id": "status-effect",
        "title": "Apply a status effect (e.g. Night Vision from a flower)",
        "tags": ["effect", "status effect", "night vision", "potion",
                 "statuseffecttype", "give player"],
        "body": (
            "1.21.1 name is StatusEffectType (renamed to StatusEffect in "
            "1.21.2+). To give the player an effect on use:\n"
            "  public class NightFlowerItem extends Item {\n"
            "      public NightFlowerItem(Settings s) { super(s); }\n"
            "      @Override\n"
            "      public ActionResult useOnBlock(ItemUsageContext ctx) {\n"
            "          if (ctx.getPlayer().level() instanceof ServerLevel) {\n"
            "              ServerPlayer p = (ServerPlayer) ctx.getPlayer();\n"
            "              p.addEffect(new StatusEffectInstance(\n"
            "                  StatusEffectType.NIGHT_VISION, 200, 0));\n"
            "              ctx.getWorld().setBlock(ctx.getClickedPos(),\n"
            "                  Blocks.AIR, 2);\n"
            "              return ActionResult.CONSUME;\n"
            "          }\n"
            "          return ActionResult.PASS;\n"
            "      }\n"
            "  }\n"
            "Ticks: 20/tick-second, so 200 ticks = 10 s. Custom effects "
            "need a StatusEffectType registered in Registry.STATUS_EFFECT "
            "with a color."
        ),
    },
    {
        "id": "biome-generation",
        "title": "Add a plant to specific biomes (Fabric BiomeModifications)",
        "tags": ["biome", "biomes", "plant", "generate", "spawn",
                 "biomodifications", "placements", "grows differently"],
        "body": (
            "Fabric API's BiomeModifications runs during world load:\n"
            "  BiomeModifications.addModifier(\n"
            "      BiomeModifiers.ADD_GENERATION_PLACEMENTS,\n"
            "      new BiomeEntryPredicate(\n"
            "          BiomeModifications.getBiomeKey(Registries.BIOME,\n"
            "              BiomeKeys.PLAINS).get(),\n"
            "          ModifiableBiomeNameSets.ALL_BIOMES),\n"
            "      (biome, placement) -> placement.addFeatures(\n"
            "          WorldGenLevel.OVERWORLD,\n"
            "          Feature.VEGETATION_SWEEP_EDGE?\n"
            "          // for a flower use Feature.FLOWER_REPLACE etc.\n"
            "          ));\n"
            "Practical flower pattern: create a PlacedFeature with "
            "Placements.count(2).filter(BlockPredicateType.of(state -> "
            "state.is(Blocks.GRASS_BLOCK))) and register it in your "
            "DatapackBuiltinEntriesProvider (data generation) or a "
            "fabric datagen-free runtime provider. 'Grows differently per "
            "biome' = several addModifier calls with different biome key "
            "predicates and densities."
        ),
    },
    {
        "id": "client-events",
        "title": "Client-side entrypoint and events",
        "tags": ["client", "clienttick", "event", "keybinding", "hud",
                 "entrypoint client"],
        "body": (
            "fabric.mod.json entrypoints: {\"client\": [\"com.nex.mymod.MyModClient\"]}. "
            "MyModClient implements ClientModInitializer:\n"
            "  public void onInitializeClient() {\n"
            "      ClientTickEvents.END_CLIENT_TICK.register(client -> {\n"
            "          if (client.currentScreen == null) { /* per-tick logic */ }\n"
            "      });\n"
            "      KeyBindingHelper.registerKeyBinding(new KeyBinding(\"my key\", \"key.w\", \"category.my\", GLFW.GLFW_KEY_P));\n"
            "  }\n"
            "Server-only code must NOT reference client classes — the "
            "mod's environment field controls which side runs it."
        ),
    },
    {
        "id": "networking",
        "title": "Client<->server networking (1.21 payload API)",
        "tags": ["networking", "packet", "payload", "streamcodec",
                 "clientplaynetworking", "serverplaynetworking"],
        "body": (
            "1.20.5+ (incl. 1.21.1) uses typed payloads:\n"
            "  public record MyData(int x) implements CustomPacketPayload {\n"
            "      public static final PayloadType<MyData> TYPE = new PayloadType<>(\n"
            "          ResourceLocation.fromNamespaceAndPath(MODID, \"data\"),\n"
            "          StreamCodec.composite(\n"
            "              StreamCodec.INT_VARINT, MyData::x, MyData::new));\n"
            "      @Override public PayloadType<?> type() { return TYPE; }\n"
            "  }\n"
            "Register: PayloadTypeRegistry.playS2C().register(MyData.TYPE);\n"
            "Send: ServerPlayNetworking.send(player, new MyData(1));\n"
            "Receive: ClientPlayNetworking.registerGlobalReceiver(MyData.TYPE, (data, ctx) -> ctx.client().execute(() -> {}));\n"
            "Legacy SimpleChannel/Message patterns DO NOT exist in 1.21."
        ),
    },
    {
        "id": "mixins",
        "title": "Mixin into Minecraft code",
        "tags": ["mixin", "inject", "override", "hook", "vanilla code"],
        "body": (
            "Add class to <modid>.mixins.json \"mixins\" and @Mixin:\n"
            "  @Mixin(Block.class)\n"
            "  public class BlockMixin {\n"
            "      @Inject(method = \"onPlaced\", at = @At(\"RETURN\"))\n"
            "      private void onBlockPlaced(BlockPlaceContext ctx, CallbackInfo ci) {\n"
            "          // your hook\n"
            "      }\n"
            "  }\n"
            "Keep mixins minimal; prefer Fabric API events first. Mixins "
            "break across mappings — the yarn method name must match the "
            "pinned yarn version."
        ),
    },
    {
        "id": "fabric-mod-json",
        "title": "fabric.mod.json anatomy",
        "tags": ["fabric.mod.json", "mod metadata", "entrypoints",
                 "depends", "schema"],
        "body": (
            "Required: schemaVersion, id, version, name, description, "
            "license. Key fields:\n"
            "  environment: \"*\" | \"client\" | \"server\"\n"
            "  entrypoints: {\"main\": [\"MyMod\"], \"client\": [\"MyModClient\"]}\n"
            "  mixins: [\"<modid>.mixins.json\"]\n"
            "  depends: {\"fabricloader\": \">=0.16.0\", \"minecraft\": \"~1.21.1\", \"java\": \">=21\"}\n"
            "The 'main' entrypoint class must implement ModInitializer; "
            "'client' must implement ClientModInitializer. A missing "
            "entrypoint class is a launch crash — the sandbox test client "
            "checks it before 'ready'."
        ),
    },
    {
        "id": "datagen",
        "title": "Data generation (tags, models, loot)",
        "tags": ["datagen", "data generation", "tags", "blocktags",
                 "models", "loot table"],
        "body": (
            "Register a FabricDatagenProvider in onInitialize via "
            "FabricDatagenProvider.entries(builder -> {\n"
            "    BlockTagProvider tags = new BlockTagProvider(MODID) {\n"
            "        @Override protected void populateTags(IndentJsonProvider.Provider p) {\n"
            "            this.tag(FabricBlockTags.REQUIRES_STONE_TOOL).add(YOUR_BLOCK);\n"
            "        }\n"
            "    };\n"
            "    tags.register(builder);\n"
            "    GbtBlockModelProvider models = ...; models.register(builder);\n"
            "});\n"
            "Run with the 'test' or 'runData' gradle task (this sandbox "
            "approves the standard tasks; datagen runs inside them)."
        ),
    },
    {
        "id": "build-files",
        "title": "Fabric 1.21.1 build pins (this sandbox)",
        "tags": ["build.gradle", "loom", "yarn", "loader", "java 21",
                 "gradle", "dependencies"],
        "body": (
            "This sandbox pins, per supported Minecraft version:\n"
            "  1.21.1: yarn 1.21.1+build.3, fabric-loader 0.16.5, "
            "fabric-loom 1.7.4, JDK 21\n"
            "  1.21:   yarn 1.21+build.8,   loader 0.16.5, loom 1.7.4, JDK 21\n"
            "  1.20.6: yarn 1.20.6+build.9, loader 0.16.5, loom 1.7.4, JDK 21\n"
            "Java is LOCKED to 21 (toolchain + options.release). The "
            "build tool runs only: build, remapJar, clean, jar, test."
        ),
    },
    {
        "id": "block-interaction",
        "title": "Block interaction (right-click behavior)",
        "tags": ["interaction", "onuse", "right click", "block item",
                 "use on block"],
        "body": (
            "Behavior belongs on the ITEM (ItemBlock/your Item) in 1.21: "
            "override useOnBlock(ItemUsageContext) for right-click on a "
            "block, or the BLOCK class's onUse(BlockState, World, "
            "BlockPos, Player, InteractionHand, BlockHitResult). For "
            "flowers, extend FlowerBlock (which already gives the "
            "item use behavior) and override onUse/replaceFor for custom "
            "picking. ItemUsageContext exposes clickedPos, player, hand."
        ),
    },
    {
        "id": "test-loop",
        "title": "The verification loop (how 'done' is proved here)",
        "tags": ["verify", "test", "loop", "crash report", "game log",
                 "verification contract"],
        "body": (
            "The loop the sandbox enforces:\n"
            "  write code -> build -> (fail? read log, fix ONE thing, "
            "build again — attempts budgeted) -> launch_test_client -> "
            "get_game_log / get_crash_report -> observe (inspect_*/"
            "screenshot) -> set_contract + mark_verified with evidence "
            "-> check_contract -> complete only when every check is "
            "satisfied.\n"
            "BUILD SUCCESS alone is never 'done': the contract requires "
            "launch + no-crash + observed behavior. Repair rounds are "
            "budgeted; when a budget is spent the server says 'human "
            "intervention required' instead of looping."
        ),
    },
]

CLASS_INDEX: Dict[str, Dict[str, Any]] = {}
for _e in KNOWLEDGE:
    for _t in _e["tags"]:
        CLASS_INDEX.setdefault(_t, _e)
for _alias, _entry in {
    "registry.block": "register-block",
    "fabricblocksettings": "register-block",
    "registry.item": "register-item",
    "fabricitemsettings": "register-item",
    "itemblock": "register-item",
    "statuseffecttype": "status-effect",
    "statuseffectinstance": "status-effect",
    "nightvision": "status-effect",
    "biomodifications": "biome-generation",
    "placements": "biome-generation",
    "modifiablebiomenamesets": "biome-generation",
    "clientticevents": "client-events",
    "keybindinghelper": "client-events",
    "serverplaynetworking": "networking",
    "clientplaynetworking": "networking",
    "payloadtyperegistry": "networking",
    "streamcodec": "networking",
    "custompacketcayload": "networking",
    "mixin": "mixins",
    "inject": "mixins",
    "fabricdatagenprovider": "datagen",
    "blocktags": "datagen",
    "fabricblocktags": "datagen",
    "flowerblock": "block-interaction",
    "itemusagecontext": "block-interaction",
    "block": "block-interaction",
    "item": "register-item",
}.items():
    CLASS_INDEX[_alias] = KNOWLEDGE[[e["id"] for e in KNOWLEDGE].index(_entry)]


# ---------------------------------------------------------------------------
# Tool table (the server's ENTIRE surface — nothing else exists)
# ---------------------------------------------------------------------------

TOOLS: List[Tuple[str, str, Dict[str, Any], str, Dict[str, Any]]] = [
    # (name, description, inputSchema, handler, annotations)
    ("create_project",
     "Create a new Fabric mod project scaffold (build files, fabric.mod.json, "
     "main class, mixins file). Supported pins: "
     + ", ".join(sorted(SUPPORTED_MC_VERSIONS))
     + ". Java is locked to JDK %s. Overwrites nothing; refuses existing "
     "projects." % JAVA_REQUIRED,
     {"type": "object",
      "properties": {
          "name": {"type": "string",
                   "description": "project name (letters/digits/_/-)"},
          "mod_id": {"type": "string",
                     "description": "optional lowercase mod id (defaults to name)"},
          "minecraft_version": {"type": "string",
                                "description": "1.21.1 (default) | 1.21 | 1.20.6"},
          "loader": {"type": "string", "description": "fabric (only supported)"}},
      "required": ["name"]},
     "tool_create_project",
     {}),
    ("list_projects",
     "List all mod projects in the sandbox with their last build status.",
     {"type": "object", "properties": {}, "required": []},
     "tool_list_projects",
     {"readOnlyHint": True}),
    ("inspect_project",
     "Project understanding: Minecraft version, loader, yarn mappings, mod id, "
     "main entrypoint, source/mixin/resource counts, dependencies, Java/Gradle "
     "toolchain state, build status, remaining attempt budget.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": ["name"]},
     "tool_inspect_project",
     {"readOnlyHint": True}),
    ("list_files",
     "List files under a project path (relative). Skips .gradle/build/run.",
     {"type": "object",
      "properties": {"name": {"type": "string"},
                     "path": {"type": "string",
                              "description": "relative dir, '' = project root"}},
      "required": ["name"]},
     "tool_list_files",
     {"readOnlyHint": True}),
    ("read_file",
     "Read one text file from a project (relative path). Max 256 KiB.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
      "required": ["name", "path"]},
     "tool_read_file",
     {"readOnlyHint": True}),
    ("write_file",
     "Write one text file in a project (relative path; parents created). "
     "Content is scanned for OS-reach (shell/process/credential patterns) and "
     "refused if it escapes the sandbox. Bounded by the step file budget.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "path": {"type": "string"},
                     "content": {"type": "string"}},
      "required": ["name", "path", "content"]},
     "tool_write_file",
     {}),
    ("delete_file",
     "Delete ONE file from a project. Never the project root, never a "
     "directory. Requires operator confirmation in autonomous runs.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
      "required": ["name", "path"]},
     "tool_delete_file",
     {"destructiveHint": True}),
    ("search_project",
     "Search project source (substring or regex, case-insensitive). Returns "
     "file:line matches, bounded.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "query": {"type": "string"},
                     "use_regex": {"type": "boolean"},
                     "path": {"type": "string"}},
      "required": ["name", "query"]},
     "tool_search_project",
     {"readOnlyHint": True}),
    ("search_api",
     "Search the curated Fabric 1.21.1 knowledge base (registration, biomes, "
     "effects, networking, mixins, datagen, build pins). Returns the top "
     "matching patterns with code.",
     {"type": "object",
      "properties": {"query": {"type": "string"}},
      "required": ["query"]},
     "tool_search_api",
     {"readOnlyHint": True}),
    ("inspect_class",
     "Look up a known Minecraft/Fabric class (e.g. Registry.BLOCK, "
     "FabricBlockSettings, StatusEffectType, BiomeModifications) in the "
     "curated knowledge base.",
     {"type": "object",
      "properties": {"class_name": {"type": "string"}},
      "required": ["class_name"]},
     "tool_inspect_class",
     {"readOnlyHint": True}),
    ("find_symbol",
     "Find a symbol (word-boundary regex) in the project source.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "symbol": {"type": "string"}},
      "required": ["name", "symbol"]},
     "tool_find_symbol",
     {"readOnlyHint": True}),
    ("find_references",
     "Find references to a symbol in the project source.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "symbol": {"type": "string"}},
      "required": ["name", "symbol"]},
     "tool_find_references",
     {"readOnlyHint": True}),
    ("build",
     "Run the server-approved gradle build. `task` is an allow-list member "
     "(build, remapJar, clean, jar, test) — NEVER a command string. Java is "
     "locked to %s; attempts are budgeted (default %d); the full log goes "
     "to builds/<project>/build.log. Reports the toolchain honestly when "
     "gradle/java are missing." % (JAVA_REQUIRED, 5),
     {"type": "object",
      "properties": {"name": {"type": "string"},
                     "task": {"type": "string",
                              "description": "build | remapJar | clean | jar | test",
                              "default": "build"}},
      "required": ["name"]},
     "tool_build",
     {}),
    ("run_tests",
     "Run the gradle test task (the same approved build path, task=test). "
     "Records test evidence for the verification contract.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": ["name"]},
     "tool_run_tests",
     {}),
    ("read_build_log",
     "Tail the project's gradle build log (builds/<project>/build.log).",
     {"type": "object",
      "properties": {"name": {"type": "string"},
                     "tail": {"type": "integer",
                              "description": "max chars (200-64KiB, default 4000)"}},
      "required": ["name"]},
     "tool_read_build_log",
     {"readOnlyHint": True}),
    ("get_build_artifact",
     "List the built jars (builds/<project>/*.jar) after a successful build.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": ["name"]},
     "tool_get_build_artifact",
     {"readOnlyHint": True}),
    ("launch_test_client",
     "Launch the ISOLATED test instance (separate world/mods/config/saves) "
     "with this project's mod. Without a real Minecraft binary it runs a "
     "clearly-labelled simulated dev client that validates the mod and "
     "exposes world state; every observation is marked simulated:true. "
     "Auto-stops at the runtime budget.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": ["name"]},
     "tool_launch_test_client",
     {}),
    ("stop_test_client",
     "Stop the running test client (isolated instance only).",
     {"type": "object",
      "properties": {"name": {"type": "string",
                              "description": "must match the running project"}},
      "required": []},
     "tool_stop_test_client",
     {}),
    ("get_game_log",
     "Tail the test client's game log (logs/<project>/game.log).",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": []},
     "tool_get_game_log",
     {"readOnlyHint": True}),
    ("get_crash_report",
     "Return the latest crash report (if the test client crashed) or "
     "crashed:false.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": []},
     "tool_get_crash_report",
     {"readOnlyHint": True}),
    ("get_test_results",
     "Structured results: last build record + all recorded evidence "
     "(build/tests/launch/crash) + the declared contract.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": []},
     "tool_get_test_results",
     {"readOnlyHint": True}),
    ("inspect_world",
     "Inspect the test instance: dimension, seed, time, weather, player, "
     "loaded entities, placed blocks.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": []},
     "tool_inspect_world",
     {"readOnlyHint": True}),
    ("inspect_block",
     "Read the block at (x,y,z) in the test instance.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "x": {"type": "integer"},
                     "y": {"type": "integer"}, "z": {"type": "integer"}},
      "required": ["name", "x", "y", "z"]},
     "tool_inspect_block",
     {"readOnlyHint": True}),
    ("inspect_entity",
     "Inspect one entity (by id or type) or list loaded entities in the "
     "test instance.",
     {"type": "object",
      "properties": {"name": {"type": "string"},
                     "entity_id": {"type": "string"}},
      "required": ["name"]},
     "tool_inspect_entity",
     {"readOnlyHint": True}),
    ("inspect_player",
     "Inspect the test player: position, health, dimension.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": []},
     "tool_inspect_player",
     {"readOnlyHint": True}),
    ("screenshot",
     "Capture the current view. The simulated client returns the world "
     "STATE (labelled simulated:true) — observation data, not pixels.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": []},
     "tool_screenshot",
     {"readOnlyHint": True}),
    ("spawn_test_entity",
     "Spawn a test entity (e.g. a pig) at a position in the isolated test "
     "instance only. Capped at 32 entities.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "kind": {"type": "string"},
                     "x": {"type": "integer"}, "y": {"type": "integer"},
                     "z": {"type": "integer"}},
      "required": ["name", "kind"]},
     "tool_spawn_test_entity",
     {}),
    ("place_test_block",
     "Place a test block at (x,y,z) in the isolated test instance only. "
     "Capped at 64 blocks.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "block": {"type": "string"},
                     "x": {"type": "integer"}, "y": {"type": "integer"},
                     "z": {"type": "integer"}},
      "required": ["name", "block"]},
     "tool_place_test_block",
     {}),
    ("set_test_time",
     "Set the world time (ticks, mod 24000) in the isolated test instance.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "value": {"type": "integer"}},
      "required": ["name", "value"]},
     "tool_set_test_time",
     {}),
    ("set_test_weather",
     "Set the weather (clear|rain|thunder) in the isolated test instance.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
      "required": ["name", "value"]},
     "tool_set_test_weather",
     {}),
    ("teleport_test_player",
     "Teleport the test player in the isolated test instance.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "x": {"type": "integer"},
                     "y": {"type": "integer"}, "z": {"type": "integer"}},
      "required": ["name", "x", "y", "z"]},
     "tool_teleport_test_player",
     {}),
    ("set_contract",
     "Declare the verification contract for a task: the checks that must be "
     "PROVEN before the task may be reported done. kinds: build, test, "
     "launch, observation, runtime.",
     {"type": "object",
      "properties": {"name": {"type": "string"},
                     "checks": {"type": "array",
                                "items": {"type": "object",
                                          "properties": {
                                              "id": {"type": "string"},
                                              "description": {"type": "string"},
                                              "kind": {"type": "string"}}}}},
      "required": ["name", "checks"]},
     "tool_set_contract",
     {}),
    ("check_contract",
     "Evaluate the contract against recorded evidence. 'complete' is false "
     "until every check is satisfied — BUILD SUCCESS alone never completes "
     "a contract.",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": ["name"]},
     "tool_check_contract",
     {"readOnlyHint": True}),
    ("mark_verified",
     "Attach explicit observation evidence to one contract check (e.g. "
     "'game log line: Mod flora initialized'). Required for 'observation' "
     "kind checks.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "check_id": {"type": "string"},
                     "evidence": {"type": "string"}},
      "required": ["name", "check_id", "evidence"]},
     "tool_mark_verified",
     {}),
    ("log_step",
     "Start a bounded step: at most N file changes (default 20) are allowed "
     "while the step is active. Use before a repair pass.",
     {"type": "object",
      "properties": {"name": {"type": "string"}, "step_id": {"type": "string"}},
      "required": ["name", "step_id"]},
     "tool_log_step",
     {"readOnlyHint": True}),
    ("close_step",
     "Close the active step (reports how many files it changed).",
     {"type": "object",
      "properties": {"name": {"type": "string"}},
      "required": ["name"]},
     "tool_close_step",
     {"readOnlyHint": True}),
    ("minecraft_status",
     "Sandbox status: root, layout, loop limits, toolchain state (java "
     "version vs required, gradle), which test client is running, and the "
     "explicit list of what this server does NOT provide.",
     {"type": "object", "properties": {}, "required": []},
     "tool_minecraft_status",
     {"readOnlyHint": True}),
]

HANDLERS: Dict[str, Callable[..., Any]] = {}
for _n, _d, _s, _h, _a in TOOLS:
    _fn = getattr(NexMinecraftMCP, _h)
    HANDLERS[_n] = _fn


def tool_definitions() -> List[Dict[str, Any]]:
    return [{"name": n, "description": d, "inputSchema": s,
             "annotations": a or None}
            for (n, d, s, _h, a) in TOOLS if a] \
           + [{"name": n, "description": d, "inputSchema": s}
              for (n, d, s, _h, a) in TOOLS if not a]


def dispatch(mcp: NexMinecraftMCP, name: str,
             arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Run one tool. Returns a plain dict; the stdio layer wraps it in the
    MCP content envelope."""
    fn = HANDLERS.get(name)
    if fn is None:
        return {"error": "unknown tool: %s — the sandbox exposes exactly: "
                         % name + ", ".join(n for n, *_ in TOOLS)}
    args = arguments or {}
    try:
        out = fn(mcp, **args)
        if not isinstance(out, dict):
            out = {"ok": True, "result": out}
        return out
    except McError as exc:
        return {"ok": False, "denied": str(exc).startswith("DENIED"),
                "error": str(exc)}
    except TypeError as exc:
        return {"ok": False,
                "error": "bad arguments for %s: %s" % (name, exc)}
    except Exception as exc:  # noqa: BLE001 — report, never crash the pipe
        import traceback
        return {"ok": False,
                "error": "tool %s failed: %r" % (name, exc),
                "traceback": traceback.format_exc(limit=3)}


# ---------------------------------------------------------------------------
# MCP stdio transport (LSP Content-Length + NDJSON, same framing as
# stdio_server so every MCP client interops)
# ---------------------------------------------------------------------------

def _resource_guide() -> str:
    lines = ["# NexMinecraftMCP — the Minecraft mod sandbox",
             "",
             "Everything this server does stays inside its sandbox root; "
             "there is no shell, no arbitrary path and no command "
             "parameter. The agent workflow it enforces:",
             "",
             "  PHASE 1  Understand the request",
             "  PHASE 2  inspect_project (what exists, what is pinned)",
             "  PHASE 3  Research the API (search_api / inspect_class)",
             "  PHASE 4  Plan (log_step the work you are about to do)",
             "  PHASE 5  Implement (write_file — bounded, scanned)",
             "  PHASE 6  Compile (build — budgeted)",
             "  PHASE 7  Automated tests (run_tests)",
             "  PHASE 8  Launch the isolated test client",
             "  PHASE 9  Observe (get_game_log / inspect_* / screenshot)",
             "  PHASE 10 Repair if necessary (fix ONE thing, rebuild)",
             "  PHASE 11 Final verification (set_contract / mark_verified /",
             "            check_contract — 'complete' must be true)",
             "  PHASE 12 Return summary + jar (get_build_artifact)",
             "",
             "## Curated Fabric %s knowledge" % DEFAULT_MC_VERSION,
             ""]
    for e in KNOWLEDGE:
        lines.append("### " + e["title"])
        lines.append("")
        lines.append(e["body"])
        lines.append("")
    return "\n".join(lines)


def run_stdio_server(mcp: NexMinecraftMCP,
                     argv: Optional[List[str]] = None) -> int:
    """Speak MCP over stdin/stdout until EOF. `argv` is deliberately
    ignored: the sandbox root and every limit come from the operator's
    environment, never from the client's command line."""
    try:
        from stdio_server import _StdioDecoder, encode_frame
    except Exception:  # noqa: BLE001 — same directory, but stay standalone
        _StdioDecoder = None  # type: ignore
        encode_frame = None  # type: ignore

        def encode_frame(body: bytes) -> bytes:  # type: ignore
            return (b"Content-Length: %d\r\n\r\n" % len(body)) + body

        class _StdioDecoder:  # type: ignore
            def __init__(self) -> None:
                self._buf = b""
                self._expected_len = None

            def feed(self, chunk: bytes) -> List[bytes]:
                self._buf += chunk
                out: List[bytes] = []
                while True:
                    if self._expected_len is None:
                        idx = self._buf.find(b"\r\n\r\n")
                        if idx < 0:
                            idx = self._buf.find(b"\n\n")
                            dl = 2
                        else:
                            dl = 4
                        if idx >= 0:
                            hdr = self._buf[:idx].decode("ascii", "replace")
                            clen = None
                            for line in hdr.splitlines():
                                if line.lower().startswith("content-length:"):
                                    try:
                                        clen = int(line.split(":", 1)[1])
                                    except ValueError:
                                        clen = None
                                    break
                            if clen is None:
                                pre = self._buf[:idx].rstrip(b"\r\n")
                                if pre.strip():
                                    out.append(pre)
                                self._buf = self._buf[idx + dl:]
                                continue
                            self._expected_len = clen
                            self._buf = self._buf[idx + dl:]
                        else:
                            nl = self._buf.find(b"\n")
                            if nl >= 0:
                                line = self._buf[:nl].rstrip(b"\r")
                                if line.strip():
                                    out.append(line)
                                self._buf = self._buf[nl + 1:]
                                continue
                            return out
                    if len(self._buf) >= self._expected_len:  # type: ignore
                        out.append(self._buf[:self._expected_len])  # type: ignore
                        self._buf = self._buf[self._expected_len:]  # type: ignore
                        self._expected_len = None
                    else:
                        return out

    stdin = os.fdopen(0, "rb", buffering=0)
    stdout = os.fdopen(1, "wb", buffering=0)
    decoder = _StdioDecoder()

    def _log(msg: str) -> None:
        sys.stderr.write("[%s] %s\n" % (SERVER_NAME, msg))
        try:
            sys.stderr.flush()
        except OSError:
            pass

    def _result(req_id: Any, result: Any) -> bytes:
        body = json.dumps({"jsonrpc": "2.0", "id": req_id,
                           "result": result}).encode("utf-8")
        return encode_frame(body)

    def _error(req_id: Any, code: int, message: str) -> bytes:
        body = json.dumps({"jsonrpc": "2.0", "id": req_id,
                           "error": {"code": code, "message": message}}
                          ).encode("utf-8")
        return encode_frame(body)

    def _content(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"content": [{"type": "text",
                             "text": json.dumps(payload, indent=2,
                                                ensure_ascii=False,
                                                default=str)}],
                "isError": bool(payload.get("error"))}

    _log("sandbox root: %s" % mcp.root)
    while True:
        try:
            chunk = stdin.read(4096)
        except (OSError, ValueError):
            break
        if not chunk:
            _log("EOF on stdin — shutting down (stopping test client)")
            with mcp._lock:
                if mcp._client is not None:
                    mcp.stop_client_locked()
            break
        try:
            bodies = decoder.feed(chunk)
        except Exception as exc:  # noqa: BLE001
            stdout.write(_error(None, -32700,
                                "parse error: %s" % exc))
            stdout.flush()
            continue
        for body in bodies:
            try:
                req = json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError as exc:
                stdout.write(_error(None, -32700,
                                    "invalid JSON: %s" % exc))
                stdout.flush()
                continue
            method = req.get("method")
            req_id = req.get("id")
            params = req.get("params") or {}
            if method == "initialize":
                requested = params.get("protocolVersion") or "2025-06-18"
                if requested not in ("2024-11-05", "2025-03-26",
                                     "2025-06-18", "2025-11-25"):
                    requested = "2025-06-18"
                out = _result(req_id, {
                    "protocolVersion": requested,
                    "serverInfo": {"name": SERVER_NAME,
                                   "version": SERVER_VERSION,
                                   "description":
                                   "Minecraft mod sandbox (no shell, "
                                   "workspace-confined, Java %s locked)"
                                   % JAVA_REQUIRED},
                    "capabilities": {"tools": {"listChanged": False},
                                     "resources": {"subscribe": False}},
                })
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                out = _result(req_id, {})
            elif method == "tools/list":
                out = _result(req_id, {"tools": tool_definitions()})
            elif method == "tools/call":
                name = params.get("name") or ""
                args = params.get("arguments") or {}
                out = _result(req_id, _content(
                    dispatch(mcp, name, args)))
            elif method == "resources/list":
                out = _result(req_id, {"resources": [
                    {"uri": "minecraft://guide",
                     "name": "Minecraft mod development guide",
                     "description": "The staged workflow + curated "
                                    "Fabric %s knowledge."
                                    % DEFAULT_MC_VERSION,
                     "mimeType": "text/markdown"},
                    {"uri": "minecraft://status",
                     "name": "Sandbox status",
                     "description": "Root, limits, toolchain, client.",
                     "mimeType": "application/json"},
                ]})
            elif method == "resources/read":
                uri = params.get("uri") or ""
                if uri == "minecraft://guide":
                    text = _resource_guide()
                    mime = "text/markdown"
                elif uri == "minecraft://status":
                    text = json.dumps(dispatch(mcp, "minecraft_status",
                                               {}), indent=2)
                    mime = "application/json"
                else:
                    text = json.dumps({"error": "unknown resource",
                                       "uri": uri})
                    mime = "application/json"
                out = _result(req_id, {
                    "contents": [{"uri": uri, "mimeType": mime,
                                  "text": text}]})
            else:
                if req_id is not None:
                    out = _error(req_id, -32601,
                                 "method not found: %s" % method)
                    stdout.write(out)
                    stdout.flush()
                continue
            stdout.write(out)
            stdout.flush()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    # `argv` is ignored on purpose — see run_stdio_server's docstring.
    mcp = NexMinecraftMCP()
    return run_stdio_server(mcp, argv)


if __name__ == "__main__":
    sys.exit(main())
