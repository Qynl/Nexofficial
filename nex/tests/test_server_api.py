"""HTTP API: auth, CSRF, conversations, chat, servers, providers, runs.

Boots the real server on a loopback port with an isolated NEX_HOME and
exercises every endpoint the web client uses — plus the SSE stream.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)
sys.path.insert(0, NEX)
sys.path.insert(0, HERE)

HOME = tempfile.mkdtemp(prefix="nex-api-")
os.environ["NEX_HOME"] = HOME

from http.server import ThreadingHTTPServer          # noqa: E402
import server as server_mod                           # noqa: E402

_FAILED = []


def expect(cond, msg):
    if not cond:
        _FAILED.append(msg)
        print("FAIL - " + msg)


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        server_mod.NexHandler)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        threading.Thread(target=cls.httpd.serve_forever,
                         daemon=True).start()
        cls.token = server_mod.AUTH_TOKEN

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    # ── helpers ─────────────────────────────────────────────────

    def req(self, method, path, body=None, headers=None, auth=True,
            csrf=True):
        h = {}
        if auth:
            h["X-Nex-Auth"] = self.token
        if csrf:
            h["X-Nex"] = "1"
        if headers:
            h.update(headers)
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data,
                                   method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                payload = resp.read()
                try:
                    return resp.status, json.loads(payload)
                except ValueError:
                    return resp.status, payload
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, payload

    # ── auth & CSRF ──────────────────────────────────────────────

    def test_health_requires_auth(self):
        s, _ = self.req("GET", "/api/health", auth=False)
        expect(s == 401, "unauthenticated /api/health must 401, got %s" % s)

    def test_bad_token_rejected(self):
        s, _ = self.req("GET", "/api/health",
                        headers={"X-Nex-Auth": "wrong-token"})
        expect(s == 401, "wrong token must 401, got %s" % s)

    def test_api_rejects_query_string_credentials(self):
        s, _ = self.req("GET", "/api/health?nex_token=" + self.token,
                        auth=False)
        expect(s == 401, "API must never authenticate a secret in its URL")

    def test_post_without_csrf_rejected(self):
        s, _ = self.req("POST", "/api/conversations", csrf=False)
        expect(s == 403, "POST without X-Nex must 403, got %s" % s)

    def test_login_establishes_session(self):
        s, body = self.req("POST", "/api/auth/session",
                           body={"token": self.token}, auth=False,
                           csrf=False)
        expect(s in (200, 201),
               "login with correct token must succeed, got %s %r"
               % (s, body))
        s2, _ = self.req("POST", "/api/auth/session",
                         body={"token": "nope"}, auth=False, csrf=False)
        expect(s2 in (401, 403), "login with wrong token must fail")

    def test_token_link_exchanges_for_cookie_and_cleans_url(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        try:
            opener.open(self.base + "/?nex_token=" + self.token, timeout=10)
            self.fail("bootstrap should redirect")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 303)
            self.assertEqual(exc.headers.get("Location"), "/")
            cookie = exc.headers.get("Set-Cookie", "")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            self.assertNotIn(self.token, exc.headers.get("Location", ""))

    def test_static_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertIn(b"Nex", r.read())
            self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
            self.assertIn("default-src 'self'",
                          r.headers.get("Content-Security-Policy", ""))
        with urllib.request.urlopen(self.base + "/js/main.js",
                                    timeout=10) as r:
            self.assertEqual(r.status, 200)

    def test_static_no_directory_escape(self):
        for probe in ("/../server.py", "/js/../../server.py",
                      "/%2e%2e/server.py"):
            try:
                with urllib.request.urlopen(self.base + probe,
                                            timeout=10) as r:
                    # some servers normalize before routing; a 200 with
                    # Python content would be the real failure
                    body = r.read()
                    expect(b"AUTH_TOKEN" not in body,
                           "directory escape %r leaked source" % probe)
            except urllib.error.HTTPError as e:
                expect(e.code in (301, 302, 403, 404),
                       "escape probe %r should redirect/404, got %s"
                       % (probe, e.code))

    # ── conversations ────────────────────────────────────────────

    def test_conversation_crud(self):
        s, c = self.req("POST", "/api/conversations")
        expect(s == 200, "create conversation: %s" % s)
        cid = c["conversation"]["id"]
        s, msgs = self.req("GET", "/api/conversations/%s/messages" % cid)
        expect(s == 200 and msgs["messages"] == [],
               "new conversation must be empty")
        s, _ = self.req("PATCH", "/api/conversations/%s" % cid,
                        body={"title": "renamed"})
        expect(s == 200, "rename: %s" % s)
        s, lst = self.req("GET", "/api/conversations")
        expect(any(x["id"] == cid and x["title"] == "renamed"
                   for x in lst["conversations"]), "rename visible in list")
        s, _ = self.req("DELETE", "/api/conversations/%s" % cid)
        expect(s == 200, "delete: %s" % s)
        s, _ = self.req("DELETE", "/api/conversations/%s" % cid)
        expect(s == 404, "double delete must 404")

    def test_model_history_has_message_and_character_budget(self):
        conversation = server_mod.STORE.create_conversation("history budget")
        cid = conversation["id"]
        try:
            for i in range(30):
                server_mod.STORE.add_message(
                    cid, "user" if i % 2 == 0 else "assistant",
                    ("message-%02d " % i) + ("x" * 1980))
            history = server_mod._history_messages(cid)
            self.assertLessEqual(len(history), server_mod.MAX_HISTORY_MESSAGES)
            self.assertLessEqual(sum(len(m["content"]) for m in history),
                                 server_mod.MAX_HISTORY_CHARS)
            self.assertIn("message-29", history[-1]["content"])
        finally:
            server_mod.STORE.delete_conversation(cid)

    def test_search_filters(self):
        s, c = self.req("POST", "/api/conversations")
        cid = c["conversation"]["id"]
        self.req("POST", "/api/chat",
                 body={"conversation_id": cid,
                       "message": "the xylophone zebra query"})
        deadline = time.time() + 8
        while time.time() < deadline:
            s, msgs = self.req("GET",
                               "/api/conversations/%s/messages" % cid)
            if msgs["messages"]:
                break
            time.sleep(0.1)
        s, hits = self.req("GET", "/api/conversations?q=xylophone")
        expect(s == 200 and any(h["id"] == cid
                                for h in hits["conversations"]),
               "search must find the conversation by content: %r" % hits)
        s, none = self.req("GET", "/api/conversations?q=zzz-nothing-xyz")
        expect(not any(h["id"] == cid for h in none["conversations"]),
               "search must not match unrelated terms")

    def test_messages_of_unknown_conversation(self):
        s, body = self.req("GET", "/api/conversations/c-void/messages")
        expect(s in (200, 404), "unknown conversation handled: %s" % s)

    # ── chat pipeline ────────────────────────────────────────────

    def test_chat_accepted_and_stored(self):
        s, c = self.req("POST", "/api/conversations")
        cid = c["conversation"]["id"]
        s, body = self.req("POST", "/api/chat",
                           body={"conversation_id": cid,
                                 "message": "hello there"})
        expect(s == 200 and body.get("ok"),
               "chat POST accepted: %s %r" % (s, body))
        # wait for the pipeline (no model configured → it will error,
        # but the user message must persist)
        deadline = time.time() + 8
        while time.time() < deadline:
            s, msgs = self.req("GET",
                               "/api/conversations/%s/messages" % cid)
            if msgs["messages"]:
                break
            time.sleep(0.1)
        roles = [m["role"] for m in msgs["messages"]]
        expect("user" in roles, "user message persisted: %r" % roles)

    def test_chat_validates_body(self):
        s, body = self.req("POST", "/api/chat", body={})
        expect(s == 400, "empty chat body must 400, got %s" % s)
        s, body = self.req("POST", "/api/chat",
                           body={"conversation_id": "nope",
                                 "message": "x"})
        expect(s == 404, "unknown conversation must 404, got %s" % s)

    # ── servers ──────────────────────────────────────────────────

    def test_servers_summary_shape(self):
        s, body = self.req("GET", "/api/servers")
        expect(s == 200, "servers summary: %s" % s)
        for key in ("servers", "total", "connected", "tools"):
            expect(key in body, "summary lacks %r" % key)

    def test_production_readiness_shape(self):
        s, body = self.req("GET", "/api/production/readiness")
        expect(s == 200, "production readiness: %s" % s)
        for key in ("score", "ready_for_large_scope", "gates",
                    "disciplines", "missing", "blockers", "engines",
                    "mcp_contract", "focus"):
            expect(key in body, "production readiness lacks %r" % key)
        expect(set(body["engines"]) == {"unreal_5_8", "roblox_studio"},
               "production readiness must expose both engine profiles")

    def test_add_server_validation(self):
        s, body = self.req("POST", "/api/servers",
                           body={"name": "Bad Name",
                                 "url": "http://127.0.0.1:1"})
        expect(s == 400, "bad name must 400, got %s" % s)
        s, body = self.req("POST", "/api/servers", body={"name": "x"})
        expect(s == 400, "no transport must 400, got %s" % s)
        s, body = self.req("POST", "/api/servers",
                           body={"name": "remote",
                                 "url": "http://example.com/mcp"})
        expect(s == 400, "remote without confirm must 400, got %s" % s)

    def test_server_actions_on_unknown(self):
        s, _ = self.req("POST", "/api/servers/ghost/disconnect")
        expect(s in (400, 404), "unknown server action handled: %s" % s)
        s, _ = self.req("DELETE", "/api/servers/ghost")
        expect(s in (400, 404), "unknown server delete handled: %s" % s)

    def test_real_mcp_server_flow(self):
        """Add the echo MCP server over HTTP, verify tools + a call path."""
        from http.server import ThreadingHTTPServer as THS
        import mcp_echo_server as echo
        httpd = THS(("127.0.0.1", 0), echo.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            s, body = self.req("POST", "/api/servers",
                               body={"name": "echo",
                                     "url": "http://127.0.0.1:%d/mcp"
                                            % port})
            expect(s in (200, 201), "echo server added: %s %r" % (s, body))
            deadline = time.time() + 10
            status = None
            while time.time() < deadline:
                s, summ = self.req("GET", "/api/servers")
                row = next((x for x in summ["servers"]
                            if x["server"] == "echo"), None)
                status = row and row["status"]
                if status == "connected":
                    break
                time.sleep(0.15)
            expect(status == "connected",
                   "echo must connect (got %r)" % status)
            s, tools = self.req("GET", "/api/servers/echo/tools")
            expect(s == 200 and tools["tools"],
                   "tools listed: %s" % s)
            names = {t["name"] for t in tools["tools"]}
            expect("echo" in names and "delete_thing" in names,
                   "tool names present: %r" % names)
            create = next(t for t in tools["tools"]
                          if t["name"] == "create_thing")
            expect("id" in create.get("output_schema", {}).get(
                "properties", {}),
                "declared MCP output schema must reach the UI API")
        finally:
            httpd.shutdown()
            httpd.server_close()
            self.req("DELETE", "/api/servers/echo")

    # ── providers ────────────────────────────────────────────────

    def test_provider_view_masked(self):
        s, body = self.req("GET", "/api/providers")
        expect(s == 200, "providers view: %s" % s)
        blob = json.dumps(body)
        expect("api_key" not in blob or '"api_key": null' in blob,
               "raw api keys must not appear in the view")
        for p in body.get("providers", []):
            expect("key_masked" in p or not p.get("configured"),
                   "provider %r must expose a masked key at most" % p)
            expect("structured_outputs" in p,
                   "provider JSON-mode capability must reach settings UI")
            state = p.get("state") or {}
            expect("total_prompt_tokens" in state and "last_purpose" in state,
                   "provider flight telemetry must reach settings UI")
        expect("chat" in (body.get("roles") or {}) and
               "agent" in (body.get("roles") or {}),
               "both model-routing roles must reach settings UI")
        expect(any(item.get("provider") == "nim"
                   for item in body.get("catalog", [])),
               "curated NIM starting points must reach settings UI")
        policy = body.get("workload_policy") or {}
        expect((policy.get("routine") or {}).get("role") == "chat"
               and (policy.get("hard") or {}).get("role") == "agent",
               "routine/hard workload policy must reach settings UI")
        expect((policy.get("token_limits") or {}).get("evaluation") == 550,
               "purpose token ceilings must be visible to operators")

    def test_provider_patch_rejected_for_bad_shape(self):
        s, body = self.req("POST", "/api/providers",
                           body={"providers": "not-a-dict"})
        expect(s in (400, 500), "bad provider patch handled: %s" % s)

    # ── runs ─────────────────────────────────────────────────────

    def test_run_resolve_unknown(self):
        s, _ = self.req("POST", "/api/runs/run-void/resolve",
                        body={"approved": True})
        expect(s in (200, 404), "unknown run resolve handled: %s" % s)

    def test_run_cancel_unknown(self):
        s, _ = self.req("POST", "/api/runs/run-void/cancel")
        expect(s in (200, 404), "unknown run cancel handled: %s" % s)

    # ── events (SSE) ─────────────────────────────────────────────

    def test_events_stream_auth_and_hello(self):
        h = {"X-Nex-Auth": self.token}
        r = urllib.request.Request(self.base + "/api/events",
                                   headers=h)
        with urllib.request.urlopen(r, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream",
                          resp.headers.get("Content-Type", ""))
            # read the first event (hello) — bounded read
            buf = b""
            deadline = time.time() + 5
            while b"hello" not in buf and time.time() < deadline:
                chunk = resp.read1(4096)
                if not chunk:
                    break
                buf += chunk
            self.assertIn(b"hello", buf)
            self.assertIn(b"data:", buf)

    def test_events_requires_auth(self):
        s, _ = self.req("GET", "/api/events", auth=False)
        expect(s == 401, "SSE without auth must 401, got %s" % s)

    # ── state ────────────────────────────────────────────────────

    def test_state_shape(self):
        s, body = self.req("GET", "/api/state")
        expect(s == 200, "state: %s" % s)
        for key in ("conversations", "servers", "production_readiness",
                    "provider"):
            expect(key in body, "state lacks %r" % key)

    def test_unknown_api_404(self):
        s, body = self.req("GET", "/api/nothing-here")
        expect(s == 404, "unknown API must 404, got %s" % s)
        expect("error" in body, "404 must carry an error object")

    def test_default_bind_is_loopback_only(self):
        # A local operator console must not publish itself to the LAN
        # merely by being started.
        expect(server_mod.HOST == "127.0.0.1",
               "default NEX_HOST must be loopback, got %r" % server_mod.HOST)
        expect(server_mod._is_loopback_host("127.0.0.1"), "127.0.0.1 loopback")
        expect(server_mod._is_loopback_host("localhost"), "localhost loopback")
        expect(server_mod._is_loopback_host("::1"), "::1 loopback")
        expect(not server_mod._is_loopback_host("0.0.0.0"),
               "0.0.0.0 is not loopback")
        expect(not server_mod._is_loopback_host("192.168.1.10"),
               "LAN address is not loopback")

    def test_failed_auth_is_throttled_but_valid_auth_is_not(self):
        peer = "203.0.113.77"
        server_mod._auth_fails.pop(peer, None)
        try:
            for _ in range(server_mod._AUTH_FAIL_LIMIT):
                expect(not server_mod._auth_throttled(peer),
                       "must not throttle below the limit")
                server_mod._note_auth_failure(peer)
            expect(server_mod._auth_throttled(peer),
                   "must throttle after the failure limit")
            # Successful requests never record a failure, so a working
            # session cannot lock itself out.
            for _ in range(server_mod._AUTH_FAIL_LIMIT * 2):
                s, _b = self.req("GET", "/api/health")
                expect(s == 200, "authenticated traffic must stay 200")
        finally:
            server_mod._auth_fails.pop(peer, None)

    def test_bad_token_eventually_returns_429(self):
        server_mod._auth_fails.clear()
        try:
            codes = [self.req("GET", "/api/health",
                              headers={"X-Nex-Auth": "wrong"},
                              auth=False)[0]
                     for _ in range(server_mod._AUTH_FAIL_LIMIT + 3)]
            expect(401 in codes, "bad tokens must 401 first")
            expect(429 in codes, "sustained bad tokens must end in 429")
        finally:
            server_mod._auth_fails.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    if _FAILED:
        sys.exit(1)
    print("\nAll server API tests passed.")
