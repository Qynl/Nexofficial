"""Deterministic compiler/log/crash parsing (agent/debugger.py).

A failure_kind label alone ("compilation") does not tell anyone WHICH
file broke. These tests prove real, documented Unreal/UBT/MSVC/Clang/
crash-log formats are parsed into actual file/line/code/message facts
by regex — never guessed — and that unrecognized text is honestly
reported as non-deterministic instead of a confident-looking invention.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
os.environ.setdefault("NEX_HOME", "/tmp/nex-debugger-tests")

from agent.debugger import (                                    # noqa: E402
    CATEGORY_BLUEPRINT, CATEGORY_COMPILER, CATEGORY_CRASH, CATEGORY_UNKNOWN,
    diagnose_failure, parse_blueprint_errors, parse_compiler_output,
    parse_crash,
)


class CompilerParsingTests(unittest.TestCase):
    def test_msvc_style_error_extracts_file_line_code_message(self):
        text = (r"C:\Projects\MyGame\Source\MyGame\Vehicle.cpp(142): error "
               r"C2065: 'bHasEngine': undeclared identifier")
        hits = parse_compiler_output(text)
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertTrue(hit.deterministic)
        self.assertEqual(hit.category, CATEGORY_COMPILER)
        self.assertTrue(hit.file.endswith("Vehicle.cpp"))
        self.assertEqual(hit.line, 142)
        self.assertEqual(hit.code, "C2065")
        self.assertIn("bHasEngine", hit.message)

    def test_clang_gcc_style_error_extracts_file_and_line(self):
        text = ("/Users/dev/MyGame/Source/Vehicle.cpp:88:14: error: "
               "use of undeclared identifier 'Speed'")
        hits = parse_compiler_output(text)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].file, "/Users/dev/MyGame/Source/Vehicle.cpp")
        self.assertEqual(hits[0].line, 88)
        self.assertIn("Speed", hits[0].message)

    def test_linker_error_extracts_the_obj_file_and_lnk_code(self):
        text = ("Vehicle.obj : error LNK2019: unresolved external symbol "
               '"public: void __cdecl AVehicle::Honk(void)"')
        hits = parse_compiler_output(text)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].file, "Vehicle.obj")
        self.assertEqual(hits[0].code, "LNK2019")

    def test_multiple_errors_are_all_captured_in_document_order(self):
        text = (
            "Vehicle.cpp(10): error C2065: 'Foo': undeclared identifier\n"
            "Mission.cpp(20): error C2061: syntax error\n"
        )
        hits = parse_compiler_output(text)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].line, 10)
        self.assertEqual(hits[1].line, 20)

    def test_ubt_summary_line_without_a_precise_location_still_extracts_a_message(self):
        text = "LogCompile: Error: Build failed: see log for details"
        hits = parse_compiler_output(text)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].file, "")
        self.assertIn("Build failed", hits[0].message)

    def test_plain_text_with_no_known_format_yields_nothing(self):
        self.assertEqual(parse_compiler_output("the build did not work"), [])
        self.assertEqual(parse_compiler_output(""), [])


class BlueprintParsingTests(unittest.TestCase):
    def test_log_blueprint_error_is_extracted(self):
        text = ("LogBlueprint: Error: Find in Blueprints: Node "
               "BP_Vehicle.K2Node_CallFunction references an unknown "
               "function 'Honk'")
        hits = parse_blueprint_errors(text)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].category, CATEGORY_BLUEPRINT)
        self.assertIn("BP_Vehicle", hits[0].message)

    def test_compiler_bracket_tag_is_extracted(self):
        hits = parse_blueprint_errors(
            "[Compiler] Honk : Unresolved pin reference")
        self.assertEqual(len(hits), 1)
        self.assertIn("Unresolved pin", hits[0].message)


class CrashParsingTests(unittest.TestCase):
    def test_fatal_error_with_a_callstack_extracts_frames(self):
        text = (
            "Fatal error: [File:Unknown] [Line: 123]\n"
            "UnrealEditor-MyGame.dll!AVehicle::Honk() [Vehicle.cpp:77]\n"
            "UnrealEditor-Core.dll!FEngineLoop::Tick() [LaunchEngineLoop.cpp:5000]\n"
        )
        diag = parse_crash(text)
        self.assertIsNotNone(diag)
        self.assertEqual(diag.category, CATEGORY_CRASH)
        self.assertTrue(diag.frames)
        self.assertEqual(diag.frames[0]["symbol"], "AVehicle::Honk")
        self.assertEqual(diag.frames[0]["file"], "Vehicle.cpp")
        self.assertEqual(diag.frames[0]["line"], 77)
        self.assertEqual(diag.symbol, "AVehicle::Honk")

    def test_assertion_failed_is_recognized(self):
        diag = parse_crash("Assertion failed: Index < Num [File:Array.h]")
        self.assertIsNotNone(diag)
        self.assertIn("Index < Num", diag.message)

    def test_no_crash_markers_returns_none(self):
        self.assertIsNone(parse_crash("the game closed normally"))


class DiagnoseFailureDispatchTests(unittest.TestCase):
    def test_compilation_kind_uses_the_compiler_parser(self):
        diag = diagnose_failure(
            "Vehicle.cpp(1): error C2065: 'x': undeclared identifier",
            "compilation")
        self.assertTrue(diag.deterministic)
        self.assertEqual(diag.category, CATEGORY_COMPILER)
        self.assertEqual(diag.code, "C2065")

    def test_compilation_kind_falls_back_to_blueprint_parser(self):
        diag = diagnose_failure(
            "LogBlueprint: Error: broken node reference", "compilation")
        self.assertTrue(diag.deterministic)
        self.assertEqual(diag.category, CATEGORY_BLUEPRINT)

    def test_runtime_kind_uses_the_crash_parser(self):
        diag = diagnose_failure(
            "Fatal error: Access violation reading location 0x00000000",
            "runtime")
        self.assertTrue(diag.deterministic)
        self.assertEqual(diag.category, CATEGORY_CRASH)

    def test_unrecognized_text_is_honestly_non_deterministic(self):
        diag = diagnose_failure("the quest did not trigger as expected",
                                "gameplay")
        self.assertFalse(diag.deterministic)
        self.assertEqual(diag.category, "gameplay")
        self.assertIn("quest did not trigger", diag.message)

    def test_an_empty_error_is_handled_without_crashing(self):
        diag = diagnose_failure("", "unknown")
        self.assertFalse(diag.deterministic)
        diag2 = diagnose_failure(None, None)
        self.assertFalse(diag2.deterministic)

    def test_to_public_and_summary_are_bounded_and_informative(self):
        diag = diagnose_failure(
            "Vehicle.cpp(1): error C2065: 'x': undeclared identifier " * 50,
            "compilation")
        public = diag.to_public()
        self.assertTrue(public["deterministic"])
        self.assertLessEqual(len(public["message"]), 300)
        self.assertIn("Vehicle.cpp:1", diag.summary())
        self.assertIn("C2065", diag.summary())

        vague = diagnose_failure("it broke", "unknown")
        self.assertIn("no deterministic location", vague.summary())


class LoopIntegrationTests(unittest.TestCase):
    """Prove agent/loop.py actually attaches the deterministic diagnostic
    to a real failed Task and surfaces it in the final report, not just
    that the pure parser works in isolation."""

    def test_a_real_compiler_error_is_deterministically_diagnosed(self):
        import json as _json
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                    "inputSchema": {"type": "object", "properties": {}}}

        server = MockMCPServer("engine", [tool("build_project")])
        manager = FakeManager([server], fail={
            "build_project": [99,
                              "Vehicle.cpp(42): error C2065: 'Speed': "
                              "undeclared identifier"]})

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return _json.dumps({"plan": {
                    "title": "Build", "rationale": "x",
                    "steps": [{"name": "build-it", "title": "Build it",
                              "tool": "engine.build_project", "args": {}}],
                }})
            return _json.dumps({"done": False, "adjust": "stop",
                               "reason": "broken build"})

        run = AgentRun("r1", "build the project", manager, llm=llm,
                      max_steps=3, max_replans=0)
        report = run.run()
        failed_task = next(t for t in run.graph.all()
                           if t.status == "failed")
        self.assertTrue(failed_task.diagnostic["deterministic"])
        self.assertEqual(failed_task.diagnostic["file"], "Vehicle.cpp")
        self.assertEqual(failed_task.diagnostic["line"], 42)
        self.assertEqual(failed_task.diagnostic["code"], "C2065")

        report_entry = report["failed"][0]
        self.assertEqual(report_entry["diagnostic"]["code"], "C2065")

    def test_an_ambiguous_failure_is_honestly_flagged_non_deterministic(self):
        import json as _json
        from agent.loop import AgentRun
        from agent.mock_mcp import MockMCPServer
        from test_agent_loop import FakeManager

        def tool(name):
            return {"name": name, "description": "",
                    "inputSchema": {"type": "object", "properties": {}}}

        server = MockMCPServer("engine", [tool("run_mission")])
        manager = FakeManager([server], fail={
            "run_mission": [99, "the quest objective never updated"]})

        def llm(messages, purpose=None):
            if "planning mind" in messages[0]["content"]:
                return _json.dumps({"plan": {
                    "title": "Mission", "rationale": "x",
                    "steps": [{"name": "run-it", "title": "Run it",
                              "tool": "engine.run_mission", "args": {}}],
                }})
            return _json.dumps({"done": False, "adjust": "stop",
                               "reason": "mission broken"})

        run = AgentRun("r1", "run the mission", manager, llm=llm,
                      max_steps=3, max_replans=0)
        report = run.run()
        failed_task = next(t for t in run.graph.all()
                           if t.status == "failed")
        self.assertFalse(failed_task.diagnostic["deterministic"])
        self.assertIn("quest objective", failed_task.diagnostic["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
