#!/usr/bin/env python3
"""A tiny stdlib MCP server for tests.

Speaks Streamable HTTP MCP on /mcp: initialize, tools/list, tools/call.
Tools:
  * echo(text)             — returns the text (READ)
  * add(a, b)              — returns the sum (READ)
  * create_thing(name)     — returns {id, name} (CREATE)
  * delete_thing(id)       — "deleted" (DESTRUCTIVE — needs approval)
  * fail_always()          — always errors (for recovery tests)

Run: python3 tests/mcp_echo_server.py [port]
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "echo", "description": "Echo the given text back.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"},
                                    "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "create_thing", "description": "Create a named thing.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"}},
                     "required": ["name"]},
     "outputSchema": {"type": "object",
                      "properties": {"id": {"type": "string"},
                                     "name": {"type": "string"}},
                      "required": ["id", "name"],
                      "additionalProperties": False}},

    {"name": "delete_thing", "description": "Delete a thing by id.",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "string"}},
                     "required": ["id"]}},
    {"name": "fail_always", "description": "Always fails.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_broken_contract",
     "description": "Test fixture that violates its declared output schema.",
     "inputSchema": {"type": "object", "properties": {}},
     "outputSchema": {"type": "object",
                      "properties": {"ok": {"type": "boolean"}},
                      "required": ["ok"]}},
]


RESOURCES = [
    {"uri": "project://level/overview",
     "name": "level overview",
     "description": "current level layout and scene budget",
     "mimeType": "text/plain",
     "_text": "level: greybox_01\nactors: 42\nlightmap: unbuilt\n"},
    {"uri": "project://notes/ignore-me",
     "name": "scratch",
     "description": "unrelated scratch data",
     "mimeType": "application/octet-stream",
     "_text": "noise"},
]

PROMPTS = [
    {"name": "level_review", "description": "review a level for quality"},
    {"name": "perf_pass", "description": "walk a performance pass"},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        method = body.get("method")
        rid = body.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18",
                      "capabilities": {"tools": {"listChanged": True},
                                       "resources": {},
                                       "prompts": {}},
                      "serverInfo": {"name": "echo-server",
                                     "version": "1.0"}}
        elif method == "notifications/initialized":
            self._send(202, {})
            return
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "resources/list":
            result = {"resources": RESOURCES}
        elif method == "resources/read":
            uri = (body.get("params") or {}).get("uri")
            match = next((r for r in RESOURCES if r["uri"] == uri), None)
            if match is None:
                self._send(200, {"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": -32002,
                                           "message": "no such resource"}})
                return
            result = {"contents": [{"uri": uri,
                                    "mimeType": match["mimeType"],
                                    "text": match["_text"]}]}
        elif method == "prompts/list":
            result = {"prompts": PROMPTS}
        elif method == "prompts/get":
            result = {"description": "echo prompt",
                      "messages": [{"role": "user",
                                    "content": {"type": "text",
                                                "text": "inspect first"}}]}
        elif method == "tools/call":
            name = (body.get("params") or {}).get("name")
            args = (body.get("params") or {}).get("arguments") or {}
            result = self._call(name, args)
        else:
            self._send(404, {"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32601,
                                       "message": "unknown " + str(method)}})
            return
        self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result})

    def _call(self, name, args):
        if name == "echo":
            text = {"content": [{"type": "text",
                                 "text": str(args.get("text", ""))}]}
            return text
        if name == "add":
            return {"content": [{"type": "text",
                                 "text": str(args.get("a", 0)
                                             + args.get("b", 0))}]}
        if name == "create_thing":
            structured = {"id": "thing_1", "name": args.get("name", "?")}
            return {"content": [{"type": "text",
                                 "text": json.dumps(structured)}],
                    "structuredContent": structured}
        if name == "delete_thing":
            return {"content": [{"type": "text",
                                 "text": "deleted " + str(args.get("id"))}]}
        if name == "fail_always":
            return {"isError": True,
                    "content": [{"type": "text", "text": "boom: nope"}]}
        if name == "read_broken_contract":
            return {"content": [{"type": "text", "text": "looks fine"}]}
        return {"isError": True,
                "content": [{"type": "text", "text": "unknown tool"}]}

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9871
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("echo MCP server on http://127.0.0.1:%d/mcp" % port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
