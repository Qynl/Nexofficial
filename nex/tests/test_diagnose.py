"""Tests for agent/diagnose.py — the LLM-driven failure-recovery brain.

This module is on the agent's action path: when a tool call fails and the
deterministic repairs do not apply, the model may propose correcting the
arguments, switching to a different tool, or giving up. A hallucinated or
policy-denied tool proposal MUST be rejected — this is exactly the kind of
model-proposed "escape hatch" the rest of the security model worries about.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

os.environ.setdefault("NEX_HOME", "/tmp/nex-diagnose-tests")

from agent import diagnose                                     # noqa: E402
from agent.mock_mcp import MockMCPServer, server_view          # noqa: E402
from mcp.registry import CapabilityRegistry                    # noqa: E402

_FAILED = []


def expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        _FAILED.append(msg)


def mk_registry(*mocks):
    return CapabilityRegistry([server_view(m.name, m) for m in mocks])


def read_tool(name, desc="read something"):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object",
                            "properties": {"path": {"type": "string"}}}}


class FakeTask:
    def __init__(self, name="step1", tool="read", server="srv", args=None):
        self.name = name
        self.tool = tool
        self.server = server
        self.args = args or {"path": "a.txt"}


def main() -> None:
    registry = mk_registry(MockMCPServer("srv", [read_tool("read"),
                                                  read_tool("read2")]))

    # ========================================================================
    # parse_decision
    # ========================================================================
    expect(diagnose.parse_decision(None) is None,
           "no reply at all parses to None")
    expect(diagnose.parse_decision("") is None,
           "an empty reply parses to None")
    expect(diagnose.parse_decision("not json at all") is None,
           "pure prose with no JSON parses to None")

    d1 = diagnose.parse_decision('{"action": "give_up", "reason": "no path"}')
    expect(d1 == {"action": "give_up", "reason": "no path"},
           "a well-formed give_up decision parses correctly")

    d2 = diagnose.parse_decision(
        'Here is my decision:\n```json\n{"action": "correct_args", '
        '"args": {"path": "b.txt"}}\n```')
    expect(d2 is not None and d2["action"] == "correct_args",
           "a fenced JSON decision is parsed")

    # tolerant protocol: action missing but args/tool imply one
    d3 = diagnose.parse_decision('{"args": {"path": "c.txt"}}')
    expect(d3 is not None and d3["action"] == "correct_args",
           "a missing action is inferred as correct_args when args is a dict")

    d4 = diagnose.parse_decision('{"tool": "srv.read2"}')
    expect(d4 is not None and d4["action"] == "switch_tool",
           "a missing action is inferred as switch_tool when tool is given")

    d5 = diagnose.parse_decision('{"action": "nonsense"}')
    expect(d5 is None,
           "an invalid action with no usable args/tool to infer from "
           "parses to None rather than being guessed at")

    d6 = diagnose.parse_decision('{"unrelated": "object", "no": "match"}')
    expect(d6 is None,
           "a JSON object with none of the expected keys parses to None")

    # ========================================================================
    # apply_decision: correct_args
    # ========================================================================
    task = FakeTask(args={"path": "a.txt"})

    ok = diagnose.apply_decision(
        {"action": "correct_args", "args": {"path": "b.txt"}}, task, registry)
    expect(ok == {"kind": "correct_args", "args": {"path": "b.txt"},
                  "reason": "LLM-proposed corrected arguments"},
           "a correct_args proposal with genuinely different args is accepted")

    same = diagnose.apply_decision(
        {"action": "correct_args", "args": {"path": "a.txt"}}, task, registry)
    expect(same is None,
           "a correct_args proposal identical to the current args is "
           "rejected (not actually a correction)")

    empty_args = diagnose.apply_decision(
        {"action": "correct_args", "args": {}}, task, registry)
    expect(empty_args is None,
           "an empty args dict is rejected (not a real correction)")

    non_dict_args = diagnose.apply_decision(
        {"action": "correct_args", "args": "b.txt"}, task, registry)
    expect(non_dict_args is None,
           "a non-dict args value is rejected outright")

    custom_reason = diagnose.apply_decision(
        {"action": "correct_args", "args": {"path": "c.txt"},
         "reason": "the path had a typo"}, task, registry)
    expect(custom_reason["reason"] == "the path had a typo",
           "a model-supplied reason is preserved")

    # ========================================================================
    # apply_decision: switch_tool
    # ========================================================================
    switch_ok = diagnose.apply_decision(
        {"action": "switch_tool", "tool": "srv.read2"}, task, registry)
    expect(switch_ok == {"kind": "switch_tool", "tool": "read2",
                         "server": "srv",
                         "reason": "LLM proposed 'read2' instead"},
           "switching to a real, different, policy-allowed tool succeeds")

    hallucinated = diagnose.apply_decision(
        {"action": "switch_tool", "tool": "srv.totally_made_up"},
        task, registry)
    expect(hallucinated is None,
           "a hallucinated tool name that does not exist in the live "
           "registry is rejected outright — the core escape-hatch defense")

    wrong_server = diagnose.apply_decision(
        {"action": "switch_tool", "tool": "ghost_server.read"},
        task, registry)
    expect(wrong_server is None,
           "a tool name qualified with a server that is not connected is "
           "rejected")

    not_a_switch = diagnose.apply_decision(
        {"action": "switch_tool", "tool": "srv.read"}, task, registry)
    expect(not_a_switch is None,
           "proposing the SAME tool and server the task already uses is "
           "rejected as not actually a switch")

    empty_tool = diagnose.apply_decision(
        {"action": "switch_tool", "tool": ""}, task, registry)
    expect(empty_tool is None, "an empty tool name is rejected")

    whitespace_tool = diagnose.apply_decision(
        {"action": "switch_tool", "tool": "   "}, task, registry)
    expect(whitespace_tool is None,
           "a whitespace-only tool name is rejected")

    # a bare (unqualified) name that is unambiguous across the registry
    # still resolves correctly
    bare = diagnose.apply_decision(
        {"action": "switch_tool", "tool": "read2"}, task, registry)
    expect(bare == {"kind": "switch_tool", "tool": "read2", "server": "srv",
                    "reason": "LLM proposed 'read2' instead"},
           "an unambiguous bare tool name resolves through the registry")

    # a bare name that is AMBIGUOUS across two servers must fail closed
    ambiguous_registry = mk_registry(
        MockMCPServer("srv_a", [read_tool("shared_name")]),
        MockMCPServer("srv_b", [read_tool("shared_name")]),
    )
    ambiguous_task = FakeTask(tool="other", server="srv_a")
    ambiguous = diagnose.apply_decision(
        {"action": "switch_tool", "tool": "shared_name"},
        ambiguous_task, ambiguous_registry)
    expect(ambiguous is None,
           "a bare tool name that is ambiguous across multiple connected "
           "servers fails closed rather than guessing which one was meant")

    # ========================================================================
    # apply_decision: give_up / unknown action
    # ========================================================================
    give_up = diagnose.apply_decision(
        {"action": "give_up", "reason": "no recoverable path"},
        task, registry)
    expect(give_up == {"kind": "give_up", "reason": "no recoverable path"},
           "give_up is accepted with the model's reason")

    give_up_no_reason = diagnose.apply_decision({"action": "give_up"},
                                                 task, registry)
    expect(give_up_no_reason["reason"] == "model judged the step unrecoverable",
           "give_up without a reason gets an honest default reason")

    unknown = diagnose.apply_decision({"action": "something_else"},
                                       task, registry)
    expect(unknown is None, "an unrecognized action is rejected")

    # ========================================================================
    # build_failure_context
    # ========================================================================
    ctx = diagnose.build_failure_context(task, "boom: connection refused",
                                          registry)
    expect(len(ctx) == 2 and ctx[0]["role"] == "system"
           and ctx[1]["role"] == "user",
           "build_failure_context returns one system + one user message")
    expect("srv.read" in ctx[1]["content"] or "read" in ctx[1]["content"],
           "the failing tool is named in the context")
    expect("connection refused" in ctx[1]["content"],
           "the error text is included")
    expect("UNTRUSTED_MCP_OUTPUT" in ctx[1]["content"],
           "the error (server-controlled text) is explicitly delimited as "
           "untrusted, prompt-injection-resistant data")

    huge_error = "x" * 5000
    ctx2 = diagnose.build_failure_context(task, huge_error, registry)
    expect(len(ctx2[1]["content"]) < 2000,
           "an enormous error string is bounded, not forwarded whole into "
           "the prompt")

    class ExplodingRegistry:
        def all_tools(self):
            raise RuntimeError("registry is on fire")

    ctx3 = diagnose.build_failure_context(task, "err", ExplodingRegistry())
    expect("(none)" in ctx3[1]["content"],
           "a registry that raises while listing tools degrades to an "
           "empty catalog instead of crashing the diagnosis attempt")

    # ========================================================================
    # diagnose(): end-to-end, one-shot, never raises
    # ========================================================================
    expect(diagnose.diagnose(task, "err", registry, None) is None,
           "diagnose() with no llm available returns None immediately "
           "(deterministic repairs remain the only fallback)")

    def good_llm(messages, purpose=None):
        return '{"action": "give_up", "reason": "cannot recover"}'

    result = diagnose.diagnose(task, "boom", registry, good_llm)
    expect(result == {"kind": "give_up", "reason": "cannot recover"},
           "diagnose() end-to-end: context built, model called, decision "
           "parsed and validated")

    def exploding_llm(messages, purpose=None):
        raise RuntimeError("provider is down")

    expect(diagnose.diagnose(task, "boom", registry, exploding_llm) is None,
           "diagnose() never raises even if the model call itself raises — "
           "a broken provider must not break the run")

    def garbage_llm(messages, purpose=None):
        return "I cannot help with that."

    expect(diagnose.diagnose(task, "boom", registry, garbage_llm) is None,
           "diagnose() returns None cleanly when the model's reply has no "
           "usable JSON at all")

    def hallucinating_llm(messages, purpose=None):
        return '{"action": "switch_tool", "tool": "srv.does_not_exist"}'

    expect(diagnose.diagnose(task, "boom", registry, hallucinating_llm)
           is None,
           "diagnose() end-to-end rejects a model that hallucinates a "
           "switch to a tool that does not exist")

    if _FAILED:
        print("\n%d failing assertions." % len(_FAILED))
        sys.exit(1)
    print("\nAll diagnose tests passed.")


if __name__ == "__main__":
    main()
