"""Small, bounded JSON-Schema validator for MCP tool arguments.

MCP tool schemas are untrusted server metadata, so this intentionally
implements the useful validation subset without resolving remote ``$ref``
values or running regexes supplied by a server.  Its purpose is to stop an
LLM typo (missing key, wrong type, unexpected property, oversized payload)
before that typo becomes an external action.

The MCP server remains authoritative and may perform stricter validation.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

MAX_ARGUMENT_BYTES = 256 * 1024
MAX_SCHEMA_DEPTH = 24
MAX_ERRORS = 12


def tool_contract_fingerprint(tool: Dict[str, Any]) -> str:
    """Stable digest of the callable MCP contract, excluding description prose."""
    contract = {
        "name": tool.get("name"),
        "inputSchema": tool.get("inputSchema") or {},
        "outputSchema": tool.get("outputSchema") or {},
        "annotations": tool.get("annotations") or {},
    }
    try:
        raw = json.dumps(contract, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raw = repr(contract)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError):
        return MAX_ARGUMENT_BYTES + 1


def _matches_type(value: Any, wanted: str) -> bool:
    if wanted == "object":
        return isinstance(value, dict)
    if wanted == "array":
        return isinstance(value, list)
    if wanted == "string":
        return isinstance(value, str)
    if wanted == "boolean":
        return isinstance(value, bool)
    if wanted == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if wanted == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if wanted == "null":
        return value is None
    return True                 # unknown extension type: server decides


def validate_arguments(schema: Any, arguments: Any) -> List[str]:
    """Return bounded human-readable validation errors (empty means valid)."""
    if not isinstance(arguments, dict):
        return ["arguments must be a JSON object"]
    size = _json_size(arguments)
    if size > MAX_ARGUMENT_BYTES:
        return ["arguments are too large (%d bytes; max %d)"
                % (size, MAX_ARGUMENT_BYTES)]
    if not isinstance(schema, dict) or not schema:
        return []

    errors: List[str] = []

    def add(message: str) -> None:
        if len(errors) < MAX_ERRORS:
            errors.append(message)

    def walk(spec: Any, value: Any, path: str, depth: int) -> None:
        if len(errors) >= MAX_ERRORS:
            return
        if depth > MAX_SCHEMA_DEPTH:
            add("%s exceeds the validation nesting limit" % path)
            return
        if isinstance(spec, bool):
            if spec is False:
                add("%s is forbidden by the tool schema" % path)
            return
        if not isinstance(spec, dict):
            return

        # allOf: every branch must hold, AND it never excludes the rest of
        # this spec (type/properties/etc. below still apply) — unlike
        # anyOf/oneOf, a branch's errors are real errors, not a dead end
        # we might recover from in another branch.
        all_of = spec.get("allOf")
        if isinstance(all_of, list):
            for branch in all_of[:16]:
                walk(branch, value, path, depth + 1)

        # Common composition keywords.  Branch errors stay private; report
        # only the useful top-level fact when no branch accepts the value.
        for keyword in ("anyOf", "oneOf"):
            branches = spec.get(keyword)
            if isinstance(branches, list) and branches:
                matches = 0
                for branch in branches[:16]:
                    before = len(errors)
                    walk(branch, value, path, depth + 1)
                    if len(errors) == before:
                        matches += 1
                    else:
                        del errors[before:]
                if (keyword == "anyOf" and matches == 0) or \
                        (keyword == "oneOf" and matches != 1):
                    add("%s does not match %s allowed schema"
                        % (path, "any" if keyword == "anyOf" else "exactly one"))
                return

        wanted = spec.get("type")
        wanted_types = ([wanted] if isinstance(wanted, str)
                        else wanted if isinstance(wanted, list) else [])
        if wanted_types and not any(_matches_type(value, t)
                                    for t in wanted_types if isinstance(t, str)):
            add("%s must be %s (got %s)" % (
                path, " or ".join(str(t) for t in wanted_types),
                type(value).__name__))
            return
        if "const" in spec and value != spec["const"]:
            add("%s must equal the schema's constant value" % path)
        enum = spec.get("enum")
        if isinstance(enum, list) and enum and value not in enum:
            add("%s must be one of the allowed values" % path)

        if isinstance(value, dict):
            props = spec.get("properties")
            props = props if isinstance(props, dict) else {}
            required = spec.get("required")
            required = required if isinstance(required, list) else []
            for key in required[:1024]:
                if isinstance(key, str) and key not in value:
                    add("%s.%s is required" % (path, key[:120]))
            additional = spec.get("additionalProperties", True)
            for key, child in list(value.items())[:2048]:
                child_path = "%s.%s" % (path, str(key)[:120])
                if key in props:
                    walk(props[key], child, child_path, depth + 1)
                elif additional is False:
                    add("%s is not allowed by the tool schema" % child_path)
                elif isinstance(additional, dict):
                    walk(additional, child, child_path, depth + 1)
            min_props = spec.get("minProperties")
            max_props = spec.get("maxProperties")
            if isinstance(min_props, int) and len(value) < min_props:
                add("%s needs at least %d properties" % (path, min_props))
            if isinstance(max_props, int) and len(value) > max_props:
                add("%s allows at most %d properties" % (path, max_props))

        elif isinstance(value, list):
            min_items = spec.get("minItems")
            max_items = spec.get("maxItems")
            if isinstance(min_items, int) and len(value) < min_items:
                add("%s needs at least %d items" % (path, min_items))
            if isinstance(max_items, int) and len(value) > max_items:
                add("%s allows at most %d items" % (path, max_items))
            item_spec = spec.get("items")
            if isinstance(item_spec, (dict, bool)):
                for i, child in enumerate(value[:4096]):
                    walk(item_spec, child, "%s[%d]" % (path, i), depth + 1)

        elif isinstance(value, str):
            min_len = spec.get("minLength")
            max_len = spec.get("maxLength")
            if isinstance(min_len, int) and len(value) < min_len:
                add("%s is shorter than minLength %d" % (path, min_len))
            if isinstance(max_len, int) and len(value) > max_len:
                add("%s is longer than maxLength %d" % (path, max_len))

        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = spec.get("minimum")
            maximum = spec.get("maximum")
            if isinstance(minimum, (int, float)) and value < minimum:
                add("%s is below minimum %s" % (path, minimum))
            if isinstance(maximum, (int, float)) and value > maximum:
                add("%s is above maximum %s" % (path, maximum))

    walk(schema, arguments, "$", 0)
    return errors


def validate_tool_output(schema: Any, result: Any) -> List[str]:
    """Validate MCP ``structuredContent`` against a tool's outputSchema.

    MCP servers are untrusted and outputSchema is a contract, not decoration.
    When a tool declares one, a successful call must return structuredContent
    that satisfies it. Text content may accompany that data, but cannot stand
    in for the declared machine-readable result.
    """
    if not isinstance(schema, dict) or not schema:
        return []
    if not isinstance(result, dict):
        return ["tool declared outputSchema but returned no result object"]
    if "structuredContent" not in result:
        return ["tool declared outputSchema but returned no structuredContent"]
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return ["structuredContent must be a JSON object"]
    errors = validate_arguments(schema, structured)
    return [e.replace("$", "$.structuredContent", 1) for e in errors]
