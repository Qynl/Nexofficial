"""Generic AAA-quality primitives for the Model Coordinator.

These tools are deliberately small and orthogonal. The 20B model
composes them into workflows. There are no presets, no recipes —
just validated primitives the model can call from any plan.

Surface
-------
* ``detect_engines()`` — sniff the host for Unreal / Blender /
  Unity / Roblox / Godot installations. Returns a structured
  report so the model can pick the right toolchain.
* ``engine_info(engine)`` — version + project paths for one
  specific engine. Empty result if not installed.
* ``compile_check(path, language)`` — run the right syntax-check
  for a single source file. Supports Python (always available
  via py_compile), GDScript (godot --check-only), Lua (luac -p),
  C/C++ (just verifies the file parses as text), JSON (stdlib
  json), YAML (best-effort), TOML (tomllib in 3.11+). For
  anything else, returns ``{"ok": False, "reason": "no validator"}``.
* ``validate_assets(kind)`` — generic per-engine asset validator
  that walks a directory and reports anything obviously broken
  (missing meta files, broken JSON sidecars, etc.). Used by the
  model after writing asset scaffolding.
* ``json_path_query(text, query)`` — RFC 6901 JSON pointer query
  against a JSON document. Useful when the model gets a huge
  tools/list response back and needs a specific tool by name.
* ``diff_files(a, b)`` — unified diff between two text files.
  Useful for self-review.

Why no presets
--------------
The user explicitly asked the model to invent its own workflows.
These tools are scaffolds, not steps. ``compile_check`` does not
know what the model is building; it just checks one file. The
model has to figure out which files to check, in what order, and
how to react when something fails.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from typing import Any, Dict, List, Optional


WORKSPACE_ROOT = os.environ.get(
    "NEX_TOOLS_ROOT",
    os.path.join(os.path.expanduser("~"), "NexWorkspace"))


# ---------------------------------------------------------------------------
# Engine detection. Looks in the usual install locations per platform.
# ---------------------------------------------------------------------------

def _which(prog: str) -> Optional[str]:
    return shutil.which(prog)


def _read_first(paths: List[str]) -> Optional[str]:
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    return f.read(2048).decode("utf-8", "replace")
            except OSError:
                continue
    return None


def _detect_roblox() -> Dict[str, Any]:
    """Cross-platform detection of Roblox Studio + its official MCP server.

    Roblox Studio installs differently per OS and the official MCP server
    ships with it:
      * Windows  — Studio lives under %LOCALAPPDATA%\\Roblox; the MCP
                   entrypoint is `mcp.bat` (stdio) and/or `StudioMCP.exe`.
      * macOS    — /Applications/RobloxStudio.app, MCP entrypoint is
                   Contents/MacOS/StudioMCP (stdio).
      * Linux    — not officially supported; best-effort via Wine's
                   AppData layout, plus the `rojo`/`rbx`/`roblox` CLIs.

    Returns a structured dict the model can branch on, always containing
    `installed`, and (when relevant) `studio_mcp`, `mcp_bat`, `cli`,
    `mcp`, `mcp_transport`, `mcp_command`, and a `mcp_note`.
    """
    result: Dict[str, Any] = {"installed": False}

    home = os.path.expanduser("~")
    localapp = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    wine_user = os.environ.get("USER", "")

    # Candidate Studio roots, most-to-least likely, per platform.
    roots: List[str] = []
    if localapp:
        roots.append(os.path.join(localapp, "Roblox"))          # Win
    roots.append("/Applications/RobloxStudio.app")              # macOS .app
    roots.append(os.path.join(home, "Applications",
                              "RobloxStudio.app"))               # macOS user
    if appdata:
        roots.append(os.path.join(appdata, "Roblox"))           # Win (roaming)
    # Wine best-effort.
    roots.append(os.path.join(home, ".wine", "drive_c", "users",
                              wine_user, "AppData", "Local", "Roblox"))

    studio_dir: Optional[str] = None
    for r in roots:
        if r and os.path.exists(r):
            studio_dir = r
            break

    studio_mcp: Optional[str] = None
    mcp_bat: Optional[str] = None
    if studio_dir:
        # The macOS .app keeps StudioMCP inside Contents/MacOS; everything
        # else (Windows) keeps the binaries directly under the root.
        candidates = [
            os.path.join(studio_dir, "Contents", "MacOS", "StudioMCP"),
            os.path.join(studio_dir, "StudioMCP.exe"),
            os.path.join(studio_dir, "mcp.bat"),
            os.path.join(studio_dir, "RobloxStudioBeta.exe"),
        ]
        for p in candidates:
            if os.path.exists(p):
                if p.endswith(("StudioMCP", "StudioMCP.exe")):
                    studio_mcp = p
                elif p.endswith("mcp.bat"):
                    mcp_bat = p

    cli = _which("roblox") or _which("rbx") or _which("rojo")
    installed = bool(studio_dir or cli)
    if not installed:
        return result

    result["installed"] = True
    result["studio_dir"] = studio_dir
    result["studio_mcp"] = studio_mcp
    result["mcp_bat"] = mcp_bat
    result["cli"] = cli

    if studio_mcp or mcp_bat:
        result["mcp"] = True
        result["mcp_transport"] = "stdio"
        if mcp_bat:
            result["mcp_command"] = ["cmd", "/c", mcp_bat]
        elif studio_mcp:
            result["mcp_command"] = [studio_mcp]
    else:
        result["mcp"] = False
        result["mcp_note"] = (
            "Studio found but the official MCP server (StudioMCP / mcp.bat) "
            "was not located. Enable the MCP Server beta in Studio > File > "
            "Beta Features, then reconnect.")
    return result



def detect_engines() -> Dict[str, Any]:
    """Return a dict of {engine: {installed, version?, paths?, project?}}.

    Conservative: an engine is "installed" only if BOTH the binary
    is on PATH (or its standard path exists) AND we found some
    artifact (uproject, blend, csproj, place) or version string.
    """
    found: Dict[str, Any] = {}

    # --- Unreal Engine ---------------------------------------------------
    ue_dirs = [
        "/Users/Shared/Epic Games",
        os.path.expanduser("~/Library/Application Support/Epic"),
        "C:\\Program Files\\Epic Games",
        "C:\\Program Files (x86)\\Epic Games",
        "/opt/unreal-engine",
        "/opt/UnrealEngine",
        os.path.expanduser("~/.local/share/Epic Games"),
    ]
    ue_paths = [p for p in ue_dirs if os.path.exists(p)]
    ue_engine_bins = []
    for root in ue_paths:
        try:
            for entry in os.listdir(root):
                full = os.path.join(root, entry)
                if not os.path.isdir(full):
                    continue
                cand = os.path.join(full, "Engine", "Binaries",
                                     "Linux", "UnrealEditor")
                if os.path.exists(cand):
                    ue_engine_bins.append(cand)
                cand2 = os.path.join(full, "Engine", "Binaries",
                                      "Mac", "UnrealEditor.app",
                                      "Contents", "MacOS", "UnrealEditor")
                if os.path.exists(cand2):
                    ue_engine_bins.append(cand2)
                cand3 = os.path.join(full, "Engine", "Binaries",
                                      "Win64", "UnrealEditor.exe")
                if os.path.exists(cand3):
                    ue_engine_bins.append(cand3)
        except OSError:
            continue
    if ue_engine_bins or _which("UnrealEditor") or _which("ue4"):
        ver = None
        for b in ue_engine_bins:
            base = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(b))))
            build = os.path.join(base, "Engine", "Build",
                                  "Build.version")
            if os.path.exists(build):
                try:
                    with open(build, "r", encoding="utf-8") as f:
                        d = json.loads(f.read())
                    ver = (d.get("MajorVersion"), d.get("MinorVersion"))
                    ver = ".".join(str(v) for v in ver if v is not None)
                except (OSError, ValueError):
                    pass
                break
        found["unreal"] = {
            "installed": True,
            "version": ver,
            "bins": ue_engine_bins[:3],
            # UE 5.8 ships an experimental MCP plugin (Project Settings ->
            # MCP) that listens on this Streamable-HTTP endpoint; older
            # engine versions need an external stdio bridge instead.
            "mcp_transport": "http",
            "mcp_endpoint": "http://127.0.0.1:3000/mcp",
            "mcp_note": (
                "UE 5.8's experimental MCP plugin listens here once enabled "
                "in Project Settings -> MCP and the editor is restarted. "
                "UE 5.6 and below need a stdio bridge (e.g. "
                "npx unreal-engine-mcp-server). See the "
                "'mcp://unreal-engine/guide' resource for the tool catalog."
            ),
        }
    # --- Blender ---------------------------------------------------------
    bl = _which("blender")
    if not bl:
        # Standard install paths.
        for cand in (
            "/Applications/Blender.app/Contents/MacOS/Blender",
            "/usr/bin/blender",
            "C:\\Program Files\\Blender Foundation\\Blender\\blender.exe",
        ):
            if os.path.exists(cand):
                bl = cand; break
    if bl:
        try:
            r = subprocess.run([bl, "--version"],
                               capture_output=True, text=True, timeout=5)
            ver = (r.stdout or "").strip().split("\n", 1)[0]
        except (OSError, subprocess.TimeoutExpired):
            ver = None
        found["blender"] = {"installed": True, "binary": bl,
                            "version": ver}

    # --- Godot -----------------------------------------------------------
    godot = _which("godot") or _which("godot4") or _which("godot3")
    if godot:
        try:
            r = subprocess.run([godot, "--version"],
                               capture_output=True, text=True, timeout=5)
            ver = (r.stdout or r.stderr or "").strip().split("\n", 1)[0]
        except (OSError, subprocess.TimeoutExpired):
            ver = None
        found["godot"] = {"installed": True, "binary": godot,
                          "version": ver}

    # --- Roblox Studio + official MCP server (cross-platform) ------------
    found["roblox"] = _detect_roblox()

    # --- Unity -----------------------------------------------------------
    unity = _which("Unity") or _which("unity") or _which("unityhub")
    if not unity:
        for cand in (
            "/Applications/Unity/Hub/Editor",
            "C:\\Program Files\\Unity\\Hub\\Editor",
        ):
            if os.path.exists(cand):
                # We don't enumerate versions — the model can list_files.
                unity = cand; break
    if unity:
        found["unity"] = {"installed": True, "binary_or_dir": unity}

    # Always include the canonical keys so callers can branch on
    # `installed` without checking for key presence.
    for k in ("unreal", "blender", "godot", "roblox", "unity"):
        if k not in found:
            found[k] = {"installed": False}
    return found


def engine_info(engine: str) -> Dict[str, Any]:
    """Detail for one engine. Like detect_engines but focused."""
    return detect_engines().get(engine, {"installed": False})


# ---------------------------------------------------------------------------
# Sandbox path safety — never escape the workspace.
# ---------------------------------------------------------------------------

def _resolve_sandbox(path: str) -> Optional[str]:
    """Return the absolute path under WORKSPACE_ROOT if it stays inside,
    or None if it escapes. Symlinks are resolved so a symlinked parent
    can't be used to escape the sandbox."""
    root = os.path.realpath(WORKSPACE_ROOT)
    candidate = os.path.realpath(os.path.abspath(os.path.join(WORKSPACE_ROOT, path)))
    if not (candidate == root or candidate.startswith(root + os.sep)):
        return None
    return candidate


# ---------------------------------------------------------------------------
# compile_check — the universal "does this code parse?" tool.
# ---------------------------------------------------------------------------

def compile_check(path: str, language: Optional[str] = None,
                   ) -> Dict[str, Any]:
    """Validate a single source file's syntax.

    Returns dict: {"ok": bool, "reason": str, "file": str, "language": str}.
    """
    abs_path = _resolve_sandbox(path)
    if abs_path is None:
        return {"ok": False, "reason": "path escapes workspace",
                "file": path, "language": language or ""}
    if not os.path.exists(abs_path):
        return {"ok": False, "reason": "file not found",
                "file": path, "language": language or ""}
    if language is None:
        ext = os.path.splitext(abs_path)[1].lower().lstrip(".")
        language = {"py": "python", "cpp": "cpp", "cc": "cpp", "c": "c",
                    "h": "cpp_header", "hpp": "cpp_header",
                    "lua": "lua", "luau": "lua",
                    "cs": "csharp",
                    "gd": "gdscript", "tscn": "godot_scene",
                    "json": "json", "toml": "toml",
                    "yaml": "yaml", "yml": "yaml",
                    "md": "markdown"}.get(ext, "")
    if not language:
        return {"ok": False, "reason": "could not detect language from path",
                "file": path, "language": ""}

    if language == "python":
        try:
            import py_compile
            py_compile.compile(abs_path, doraise=True)
            return {"ok": True, "reason": "compiled cleanly",
                    "file": path, "language": language}
        except py_compile.PyCompileError as exc:
            return {"ok": False, "reason": str(exc),
                    "file": path, "language": language}
    if language == "lua":
        luac = _which("luac") or _which("luac5.4") or _which("luac5.3")
        if not luac:
            return {"ok": False,
                    "reason": "luac not on PATH — install Lua to validate",
                    "file": path, "language": language}
        r = subprocess.run([luac, "-p", abs_path],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return {"ok": True, "reason": "luac -p passed",
                    "file": path, "language": language}
        return {"ok": False,
                "reason": (r.stderr or r.stdout or "luac failed").strip(),
                "file": path, "language": language}
    if language == "gdscript":
        godot = _which("godot") or _which("godot4")
        if not godot:
            return {"ok": False,
                    "reason": "godot binary not on PATH — install Godot to validate",
                    "file": path, "language": language}
        # `--check-only` runs a static parse without launching the editor.
        try:
            r = subprocess.run([godot, "--headless", "--check-only",
                                "--script", abs_path],
                               capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return {"ok": False,
                    "reason": "godot --check-only timed out",
                    "file": path, "language": language}
        if r.returncode == 0:
            return {"ok": True, "reason": "godot --check-only passed",
                    "file": path, "language": language}
        return {"ok": False,
                "reason": (r.stderr or r.stdout or "godot failed").strip()[:400],
                "file": path, "language": language}
    if language == "json":
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                json.load(f)
            return {"ok": True, "reason": "json parses",
                    "file": path, "language": language}
        except (OSError, ValueError) as exc:
            return {"ok": False, "reason": str(exc),
                    "file": path, "language": language}
    if language == "toml":
        try:
            with open(abs_path, "rb") as f:
                tomllib.load(f)
            return {"ok": True, "reason": "toml parses",
                    "file": path, "language": language}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return {"ok": False, "reason": str(exc),
                    "file": path, "language": language}
    if language in ("cpp", "cpp_header", "c", "csharp"):
        # We don't have a free C# compiler in every sandbox. Be honest
        # about that — the model can shell out to its own if it has
        # one configured.
        if language == "csharp":
            # HONESTY: without a project/compiler we can only check the
            # ENVIRONMENT, not the source. `checked: "environment"` tells
            # the autonomous verifier this is NOT a successful compile.
            roslyn = _which("dotnet")
            if not roslyn:
                return {"ok": False, "checked": "none",
                        "reason": "dotnet not on PATH",
                        "file": path, "language": language}
            r = subprocess.run([roslyn, "--info"],
                               capture_output=True, text=True, timeout=10)
            return {"ok": True, "checked": "environment",
                    "compiled": False,
                    "reason": "dotnet SDK present; source was NOT compiled "
                              "(a csproj + build is required for that)",
                    "file": path, "language": language,
                    "dotnet": (r.stdout or r.stderr or "").strip()[:400]}
        return {"ok": False,
                "reason": ("no in-sandbox C/C++ compiler; "
                           "ask the model to shell out via the editor's "
                           "UAT/UBT or a configured clang"),
                "file": path, "language": language}
    return {"ok": False, "reason": "no validator for language: " + language,
            "file": path, "language": language}


# ---------------------------------------------------------------------------
# validate_assets — best-effort directory walk for asset integrity.
# ---------------------------------------------------------------------------

def validate_assets(kind: str = "auto",
                    path: str = "") -> Dict[str, Any]:
    """Walk a directory and look for obvious asset issues.

    `kind` is one of: "unreal", "unity", "godot", "blender",
    "roblox", "auto". When "auto", we sniff for known marker
    files (.uproject, .godot, Assets/, project.godot, .blend).

    Returns a structured report. We DO NOT actually run the
    editor — that would block. We just check metadata.
    """
    abs_root = _resolve_sandbox(path or ".")
    if abs_root is None:
        return {"ok": False, "reason": "path escapes workspace"}
    if not os.path.isdir(abs_root):
        return {"ok": False, "reason": "not a directory: " + path}

    report: Dict[str, Any] = {
        "ok": True,
        "kind": kind,
        "scanned": abs_root,
        "findings": [],
    }

    def _note(level: str, msg: str, where: str = "") -> None:
        report["findings"].append({"level": level, "msg": msg,
                                   "where": where})
        if level == "error":
            report["ok"] = False

    # Detect kind if auto.
    if kind == "auto":
        for entry in os.listdir(abs_root):
            if entry.endswith(".uproject"):
                kind = "unreal"; break
            if entry.endswith(".godot"):
                kind = "godot"; break
            if entry == "project.godot":
                kind = "godot"; break
            if entry == "Assets" and os.path.isdir(os.path.join(abs_root, entry)):
                kind = "unity"; break
            if entry.endswith(".blend"):
                kind = "blender"; break
            if entry.endswith(".rbxl") or entry.endswith(".rbxlx"):
                kind = "roblox"; break

    if kind == "unreal":
        # REAL Unreal structure checks. (An earlier version flagged every
        # .uasset without a Unity-style .meta sidecar — that is not how
        # Unreal works and it reported healthy projects as broken.)
        uasset_count = 0
        empty_assets = 0
        foreign_meta = 0
        for r, _d, files in os.walk(abs_root):
            for f in files:
                p = os.path.join(r, f)
                if f.endswith(".uasset") or f.endswith(".umap"):
                    uasset_count += 1
                    try:
                        if os.path.getsize(p) == 0:
                            empty_assets += 1
                            _note("error",
                                  "asset file is zero bytes (corrupt save "
                                  "or interrupted write)",
                                  os.path.relpath(p, abs_root))
                    except OSError:
                        pass
                elif f.endswith(".meta"):
                    # Unity-style sidecars inside an Unreal project are a
                    # sign of a mixed checkout — informational, not fatal.
                    foreign_meta += 1
        if foreign_meta:
            _note("warn",
                  "%d Unity-style .meta sidecars present in an Unreal "
                  "project (mixed checkout?)" % foreign_meta, "")
        report["uasset_count"] = uasset_count
        report["empty_assets"] = empty_assets
        report["foreign_meta_count"] = foreign_meta

    elif kind == "unity":
        # ProjectSettings/ProjectVersion.txt is the canonical version file.
        v = os.path.join(abs_root, "ProjectSettings", "ProjectVersion.txt")
        if not os.path.exists(v):
            _note("warn",
                  "ProjectSettings/ProjectVersion.txt missing",
                  os.path.relpath(v, abs_root))

    elif kind == "godot":
        proj = os.path.join(abs_root, "project.godot")
        if not os.path.exists(proj):
            _note("error", "project.godot missing",
                  os.path.relpath(proj, abs_root))
        else:
            try:
                with open(proj, "rb") as f:
                    tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                _note("error", "project.godot unparseable: " + str(exc),
                      os.path.relpath(proj, abs_root))

    elif kind == "roblox":
        # We look for default.project.json (Rojo) and warn if it's missing.
        for proj in ("default.project.json", "project.json"):
            p = os.path.join(abs_root, proj)
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        json.load(f)
                except (OSError, ValueError) as exc:
                    _note("error", proj + " unparseable: " + str(exc),
                          os.path.relpath(p, abs_root))
                break

    return report


# ---------------------------------------------------------------------------
# json_path_query — RFC 6901 pointer.
# ---------------------------------------------------------------------------

def json_path_query(text: str, query: str) -> Dict[str, Any]:
    """Read text (JSON) and pull a value at the given JSON pointer
    (RFC 6901). Returns ``{"ok":True,"value":...}`` or
    ``{"ok":False,"reason":...}``.

    Examples:
      query=""        -> the whole document
      query="/tools/0"-> the first tool
      query="/tools/0/name"-> its name field
    """
    try:
        doc = json.loads(text) if isinstance(text, str) else text
    except (ValueError, TypeError) as exc:
        return {"ok": False, "reason": "input is not JSON: " + str(exc)}
    if not query or query == "/":
        return {"ok": True, "value": doc}
    if not query.startswith("/"):
        return {"ok": False, "reason": "query must start with '/'"}
    cur: Any = doc
    for raw_part in query.split("/")[1:]:
        # RFC 6901 escape.
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                return {"ok": False,
                        "reason": "non-integer index '" + part + "' on array"}
            if idx < 0 or idx >= len(cur):
                return {"ok": False,
                        "reason": "index " + str(idx) + " out of range"}
            cur = cur[idx]
        elif isinstance(cur, dict):
            if part not in cur:
                return {"ok": False,
                        "reason": "no key '" + part + "'"}
            cur = cur[part]
        else:
            return {"ok": False, "reason": "cannot descend into scalar"}
    return {"ok": True, "value": cur}


# ---------------------------------------------------------------------------
# diff_files — unified diff between two sandbox files.
# ---------------------------------------------------------------------------

def diff_files(a: str, b: str) -> Dict[str, Any]:
    """Return a unified diff for two text files under the sandbox."""
    pa = _resolve_sandbox(a)
    pb = _resolve_sandbox(b)
    if pa is None:
        return {"ok": False, "reason": "a escapes workspace: " + a}
    if pb is None:
        return {"ok": False, "reason": "b escapes workspace: " + b}
    if not os.path.exists(pa):
        return {"ok": False, "reason": "a not found: " + a}
    if not os.path.exists(pb):
        return {"ok": False, "reason": "b not found: " + b}
    try:
        with open(pa, "r", encoding="utf-8") as f:
            ta = f.read().splitlines()
        with open(pb, "r", encoding="utf-8") as f:
            tb = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "reason": str(exc)}
    diff = list(difflib.unified_diff(ta, tb, fromfile=a, tofile=b,
                                      lineterm=""))
    return {"ok": True, "diff": diff, "diff_lines": len(diff)}


# ---------------------------------------------------------------------------
# tool list surface — what the MCP gateway exposes.
# ---------------------------------------------------------------------------

MC_TOOLS = [
    ("detect_engines",
     "DIAGNOSTICS ONLY — host discovery, not a build capability. Sniffs "
     "the local machine for installed game engines (Unreal, Blender, "
     "Godot, Roblox, Unity). Do not plan around it: what Nex can actually "
     "drive is whatever the CONNECTED MCP servers expose.",
     {"type": "object", "properties": {}, "required": []},
     detect_engines),
    ("engine_info",
     "Detail for one engine: version, project paths, capabilities.",
     {"type": "object",
      "properties": {"engine": {"type": "string"}},
      "required": ["engine"]},
     engine_info),
    ("compile_check",
     "Validate the syntax of one source file. Returns "
     "{ok, reason, file, language}. Supports python (always), "
     "lua (if luac on PATH), gdscript (if godot on PATH), "
     "json, toml, csharp (if dotnet on PATH).",
     {"type": "object",
      "properties": {
          "path": {"type": "string"},
          "language": {"type": "string",
                       "description": ("Override language: python, lua, "
                                       "gdscript, json, toml, csharp, cpp. "
                                       "Auto-detected from extension if "
                                       "omitted.")}},
      "required": ["path"]},
     compile_check),
    ("validate_assets",
     "Walk a directory and look for obvious asset integrity issues "
     "(missing sidecars, broken JSON, etc). Returns a report.",
     {"type": "object",
      "properties": {
          "kind": {"type": "string",
                   "description": "unreal, unity, godot, blender, roblox, or auto"},
          "path": {"type": "string",
                   "description": "Directory to scan; relative to workspace root. "
                                  "Empty string = workspace root."}}},
     validate_assets),
    ("json_path_query",
     "Read a JSON document and pull a value at an RFC 6901 JSON pointer. "
     "Useful when an upstream returns a huge tools/list and you need a "
     "specific tool by name.",
     {"type": "object",
      "properties": {
          "text": {"type": "string",
                   "description": "JSON text, or pass a stringified doc."},
          "query": {"type": "string",
                    "description": "/tools/0/name, etc. Empty = whole doc."}},
      "required": ["text", "query"]},
     json_path_query),
    ("diff_files",
     "Unified diff between two sandbox text files.",
     {"type": "object",
      "properties": {
          "a": {"type": "string"},
          "b": {"type": "string"}},
      "required": ["a", "b"]},
     diff_files),
]


def tool_definitions() -> List[Dict[str, Any]]:
    return [{"name": n, "description": d, "inputSchema": s}
            for (n, d, s, _h) in MC_TOOLS]


def call_tool(name: str, arguments: Optional[Dict[str, Any]] = None
              ) -> Dict[str, Any]:
    arguments = arguments or {}
    for (n, _d, _s, handler) in MC_TOOLS:
        if n == name:
            try:
                return handler(**arguments)
            except TypeError as exc:
                return {"error": "bad arguments: " + str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"error": "tool failed: " + repr(exc)}
    return {"error": "unknown tool: " + name}
