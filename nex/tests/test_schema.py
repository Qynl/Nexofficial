"""Tests for the bounded JSON-Schema argument validator (mcp/schema.py).

validate_arguments() is the last line of defense between an LLM's typo /
a hallucinated argument and a real external tool call reaching a live MCP
server. These tests focus on the composition keywords (allOf/anyOf/oneOf)
since a gap there means a declared constraint silently never gets checked.
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)

schema = importlib.import_module("mcp.schema")
validate_arguments = schema.validate_arguments

_FAILED = []


def expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        _FAILED.append(msg)


def main() -> None:
    # --- baseline: a plain object schema still works ----------------------
    basic = {"type": "object", "properties": {"n": {"type": "integer"}},
             "required": ["n"]}
    expect(validate_arguments(basic, {"n": 1}) == [],
           "a valid plain object passes")
    expect(validate_arguments(basic, {}) != [],
           "a missing required field is caught")

    # --- allOf: every branch must hold -------------------------------------
    all_of_schema = {
        "allOf": [
            {"type": "object", "required": ["name"]},
            {"properties": {"age": {"type": "integer", "minimum": 0}}},
        ]
    }
    expect(validate_arguments(all_of_schema, {"name": "a", "age": 5}) == [],
           "allOf: an argument satisfying every branch passes")
    errs_missing = validate_arguments(all_of_schema, {"age": 5})
    expect(errs_missing != [],
           "allOf: violating ONE branch (missing 'name') must still be "
           "reported — this is the gap that existed before allOf support "
           "was added (branches used to be silently ignored)")
    errs_bad_age = validate_arguments(all_of_schema, {"name": "a", "age": -1})
    expect(errs_bad_age != [],
           "allOf: a branch-level constraint (age minimum) must be "
           "enforced: %r" % errs_bad_age)

    # --- allOf does not exclude sibling keywords on the same spec ---------
    all_of_with_siblings = {
        "type": "object",
        "required": ["id"],
        "allOf": [{"properties": {"id": {"type": "string", "minLength": 3}}}],
    }
    expect(validate_arguments(all_of_with_siblings, {"id": "abc"}) == [],
           "allOf siblings: a valid value against both the outer spec and "
           "the allOf branch passes")
    expect(validate_arguments(all_of_with_siblings, {"id": "ab"}) != [],
           "allOf siblings: the allOf branch's minLength is still enforced "
           "alongside the outer 'required'")
    expect(validate_arguments(all_of_with_siblings, {}) != [],
           "allOf siblings: the OUTER required is still enforced too")

    # --- anyOf / oneOf keep working as before (regression guard) ----------
    any_of_prop = {"type": "object",
                   "properties": {"v": {"anyOf": [{"type": "string"},
                                                  {"type": "integer"}]}}}
    expect(validate_arguments(any_of_prop, {"v": "s"}) == [],
           "anyOf: a string branch match passes")
    expect(validate_arguments(any_of_prop, {"v": 3}) == [],
           "anyOf: an integer branch match passes")
    expect(validate_arguments(any_of_prop, {"v": []}) != [],
           "anyOf: neither branch matching is an error")

    one_of_prop = {"type": "object",
                   "properties": {"v": {"oneOf": [
                       {"type": "integer", "multipleOf": 2},
                       {"type": "integer", "multipleOf": 3},
                   ]}}}
    # multipleOf isn't implemented by this validator (out of scope), so
    # both branches "match" any integer — oneOf must then report the
    # ambiguity (more than one branch matched) rather than silently pick
    # one. This documents current behavior, not a new guarantee.
    result = validate_arguments(one_of_prop, {"v": 6})
    expect(isinstance(result, list), "oneOf: validator returns a list: %r"
           % result)

    # --- nested allOf inside a property --------------------------------
    nested = {"type": "object", "properties": {
        "p": {"allOf": [{"type": "object", "required": ["a"]},
                        {"type": "object", "required": ["b"]}]}}}
    expect(validate_arguments(nested, {"p": {"a": 1, "b": 2}}) == [],
           "nested allOf: both required keys present passes")
    expect(validate_arguments(nested, {"p": {"a": 1}}) != [],
           "nested allOf: a missing key from either branch is caught")

    if _FAILED:
        print("\n%d failing assertions." % len(_FAILED))
        sys.exit(1)
    print("\nAll schema tests passed.")


if __name__ == "__main__":
    main()
