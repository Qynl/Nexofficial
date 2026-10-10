"""HTTP API: auth, CSRF, conversations, chat, servers, providers, runs.

Boots the real server on a loopback port with an isolated NEX_HOME and
exercises every endpoint the web client uses — plus the SSE stream.
"""
import json
import os
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

    def test_health_reports_storage_retention(self):
        """/api/health surfaces the conversation store's size and its
        retention limits, so an operator can see memory is bounded."""
        s, body = self.req("GET", "/api/health")
        expect(s == 200, "authenticated /api/health must 200, got %s" % s)
        storage = body.get("storage") or {}
        expect("conversations" in storage and "messages" in storage,
               "storage stats expose conversation/message counts")
        limits = storage.get("retention") or {}
        expect("max_conversations" in limits
               and "max_messages_per_conversation" in limits,
               "storage stats expose the active retention limits")

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

    def test_run_summary_persists_project_memory(self):
        # The server-layer hook, not agent/loop.py directly (agent/ must
        # not import store.py — tests/test_architecture.py enforces that),
        # is what turns a run's structured memory into something the NEXT
        # run in the same conversation can see.
        cid = server_mod.STORE.create_conversation()["id"]
        self.assertIsNone(server_mod.STORE.get_project_memory(cid))

        class FakeRun:
            conversation_id = cid
            run_id = "run-fake-memory-test"

        report = {"status": "completed",
                 "project_memory": {"runs_recorded": 1,
                                    "milestones_done": ["discovery"],
                                    "risks": []}}
        server_mod._on_run_summary(FakeRun(), "done", report)
        mem = server_mod.STORE.get_project_memory(cid)
        expect(mem is not None and mem.get("runs_recorded") == 1,
              "project memory persisted after a run: %r" % mem)

    def test_start_run_loads_prior_project_memory_for_the_new_run(self):
        cid = server_mod.STORE.create_conversation()["id"]
        server_mod.STORE.save_project_memory(
            cid, {"runs_recorded": 1, "milestones_done": ["discovery"]})
        captured = {}
        real_start = server_mod.RUNS.start

        def fake_start(goal, conversation_id=None, **opts):
            captured["project_memory"] = opts.get("project_memory")
            return "run-not-really-started"

        server_mod.RUNS.start = fake_start
        try:
            server_mod._start_run(cid, "a test goal", "")
        finally:
            server_mod.RUNS.start = real_start
        expect(captured.get("project_memory", {}).get("runs_recorded") == 1,
              "prior project memory reached RunCoordinator.start: %r"
              % captured)

    def test_run_summary_persists_regression_state(self):
        cid = server_mod.STORE.create_conversation()["id"]
        self.assertIsNone(server_mod.STORE.get_regression_state(cid))

        class FakeRun:
            conversation_id = cid
            run_id = "run-fake-regression-test"

        report = {"status": "completed",
                 "regression_state": {"systems": {
                     "vehicles": {"since_run": 1,
                                 "unverified_dependents": ["traffic"]}}}}
        server_mod._on_run_summary(FakeRun(), "done", report)
        state = server_mod.STORE.get_regression_state(cid)
        expect(state is not None
              and state["systems"]["vehicles"]["since_run"] == 1,
              "regression state persisted after a run: %r" % state)

    def test_start_run_loads_prior_regression_state_for_the_new_run(self):
        cid = server_mod.STORE.create_conversation()["id"]
        server_mod.STORE.save_regression_state(
            cid, {"systems": {"missions": {"since_run": 2}}})
        captured = {}
        real_start = server_mod.RUNS.start

        def fake_start(goal, conversation_id=None, **opts):
            captured["regression_state"] = opts.get("regression_state")
            return "run-not-really-started"

        server_mod.RUNS.start = fake_start
        try:
            server_mod._start_run(cid, "a test goal", "")
        finally:
            server_mod.RUNS.start = real_start
        expect(captured.get("regression_state", {})
              .get("systems", {}).get("missions", {}).get("since_run") == 2,
              "prior regression state reached RunCoordinator.start: %r"
              % captured)

    def test_run_summary_persists_project_graph(self):
        cid = server_mod.STORE.create_conversation()["id"]
        self.assertIsNone(server_mod.STORE.get_project_graph(cid))

        class FakeRun:
            conversation_id = cid
            run_id = "run-fake-graph-test"

        report = {"status": "completed",
                 "project_graph": {"nodes": {
                     "BP_Car": {"systems": ["vehicles"], "mentions": 1}},
                     "edges": {}}}
        server_mod._on_run_summary(FakeRun(), "done", report)
        graph = server_mod.STORE.get_project_graph(cid)
        expect(graph is not None
              and graph["nodes"]["BP_Car"]["mentions"] == 1,
              "project graph persisted after a run: %r" % graph)

    def test_start_run_loads_prior_project_graph_for_the_new_run(self):
        cid = server_mod.STORE.create_conversation()["id"]
        server_mod.STORE.save_project_graph(
            cid, {"nodes": {"BP_Car": {"mentions": 3}}, "edges": {}})
        captured = {}
        real_start = server_mod.RUNS.start

        def fake_start(goal, conversation_id=None, **opts):
            captured["project_graph"] = opts.get("project_graph")
            return "run-not-really-started"

        server_mod.RUNS.start = fake_start
        try:
            server_mod._start_run(cid, "a test goal", "")
        finally:
            server_mod.RUNS.start = real_start
        expect(captured.get("project_graph", {})
              .get("nodes", {}).get("BP_Car", {}).get("mentions") == 3,
              "prior project graph reached RunCoordinator.start: %r"
              % captured)

    def test_run_summary_persists_roblox_project_model(self):
        cid = server_mod.STORE.create_conversation()["id"]
        self.assertIsNone(server_mod.STORE.get_roblox_project_model(cid))

        class FakeRun:
            conversation_id = cid
            run_id = "run-fake-roblox-model-test"

        report = {"status": "completed",
                 "roblox_project_model": {"nodes": {
                     "ReplicatedStorage.PurchaseItem": {"kind": "remote"}},
                     "children": {}}}
        server_mod._on_run_summary(FakeRun(), "done", report)
        model = server_mod.STORE.get_roblox_project_model(cid)
        expect(model is not None
              and model["nodes"]["ReplicatedStorage.PurchaseItem"]["kind"]
              == "remote",
              "roblox project model persisted after a run: %r" % model)

    def test_a_non_roblox_run_never_overwrites_a_prior_roblox_model(self):
        cid = server_mod.STORE.create_conversation()["id"]
        server_mod.STORE.save_roblox_project_model(
            cid, {"nodes": {"Workspace.Rock": {"kind": "instance"}},
                 "children": {}})

        class FakeRun:
            conversation_id = cid
            run_id = "run-fake-non-roblox-test"

        # roblox_project_model is None (not computed), exactly what
        # agent/loop.py's _roblox_report() returns for a non-Roblox run.
        report = {"status": "completed", "roblox_project_model": None}
        server_mod._on_run_summary(FakeRun(), "done", report)
        model = server_mod.STORE.get_roblox_project_model(cid)
        expect(model is not None and "Workspace.Rock" in model["nodes"],
              "a non-Roblox run must not erase prior Roblox state: %r"
              % model)

    def test_start_run_loads_prior_roblox_project_model_for_the_new_run(self):
        cid = server_mod.STORE.create_conversation()["id"]
        server_mod.STORE.save_roblox_project_model(
            cid, {"nodes": {"Workspace.Rock": {"kind": "instance"}},
                 "children": {}})
        captured = {}
        real_start = server_mod.RUNS.start

        def fake_start(goal, conversation_id=None, **opts):
            captured["roblox_project_model"] = opts.get(
                "roblox_project_model")
            return "run-not-really-started"

        server_mod.RUNS.start = fake_start
        try:
            server_mod._start_run(cid, "a test goal", "")
        finally:
            server_mod.RUNS.start = real_start
        expect("Workspace.Rock" in
              (captured.get("roblox_project_model") or {}).get("nodes", {}),
              "prior roblox project model reached RunCoordinator.start: %r"
              % captured)

    def test_run_summary_persists_a_checkpoint(self):
        cid = server_mod.STORE.create_conversation()["id"]
        self.assertEqual(server_mod.STORE.list_checkpoints(cid), [])

        class FakeRun:
            conversation_id = cid
            run_id = "run-fake-checkpoint-test"

        report = {"status": "completed",
                 "checkpoint": {"run_id": "run-fake-checkpoint-test",
                               "run_no": 1, "mutations": 2,
                               "reversible": 1, "steps": []}}
        server_mod._on_run_summary(FakeRun(), "done", report)
        checkpoints = server_mod.STORE.list_checkpoints(cid)
        expect(len(checkpoints) == 1 and checkpoints[0]["mutations"] == 2,
              "checkpoint persisted after a run: %r" % checkpoints)

    def test_checkpoints_endpoint_lists_compact_summaries(self):
        s, c = self.req("POST", "/api/conversations")
        cid = c["conversation"]["id"]
        server_mod.STORE.save_checkpoint(cid, 1, {
            "run_id": "r1", "run_no": 1, "mutations": 3, "reversible": 2,
            "irreversible": 1, "coverage_pct": 67,
            "steps": [{"tool": "destroy_actor"}]})
        s, body = self.req("GET", "/api/conversations/%s/checkpoints" % cid)
        expect(s == 200, "checkpoints endpoint: %s" % s)
        expect(len(body["checkpoints"]) == 1, "one checkpoint listed")
        expect("steps" not in body["checkpoints"][0],
              "checkpoint list view must not expose raw steps")
        expect(body["checkpoints"][0]["run_no"] == 1, "run_no present")

    def test_checkpoints_endpoint_unknown_conversation_404s(self):
        s, _ = self.req("GET", "/api/conversations/no-such-id/checkpoints")
        expect(s == 404, "unknown conversation checkpoints: %s" % s)

    def test_rollback_plan_endpoint_combines_checkpoints(self):
        s, c = self.req("POST", "/api/conversations")
        cid = c["conversation"]["id"]
        server_mod.STORE.save_checkpoint(cid, 1, {
            "run_id": "r1", "run_no": 1, "mutations": 1, "reversible": 1,
            "irreversible": 0, "coverage_pct": 100,
            "steps": [{"tool": "destroy_a"}], "blocked": []})
        server_mod.STORE.save_checkpoint(cid, 2, {
            "run_id": "r2", "run_no": 2, "mutations": 1, "reversible": 1,
            "irreversible": 0, "coverage_pct": 100,
            "steps": [{"tool": "destroy_b"}], "blocked": []})
        s, body = self.req(
            "POST", "/api/conversations/%s/checkpoints/rollback-plan" % cid,
            body={})
        expect(s == 200, "rollback plan endpoint: %s" % s)
        plan = body["rollback_plan"]
        expect(plan["runs_included"] == [2, 1], "newest run first: %r"
              % plan["runs_included"])
        expect([step["tool"] for step in plan["steps"]]
              == ["destroy_b", "destroy_a"], "steps in LIFO order: %r"
              % plan["steps"])

    def test_rollback_plan_endpoint_honors_since_run_no(self):
        s, c = self.req("POST", "/api/conversations")
        cid = c["conversation"]["id"]
        server_mod.STORE.save_checkpoint(cid, 1, {
            "run_id": "r1", "run_no": 1, "mutations": 1, "reversible": 1,
            "irreversible": 0, "coverage_pct": 100, "steps": [], "blocked": []})
        server_mod.STORE.save_checkpoint(cid, 2, {
            "run_id": "r2", "run_no": 2, "mutations": 1, "reversible": 1,
            "irreversible": 0, "coverage_pct": 100, "steps": [], "blocked": []})
        s, body = self.req(
            "POST", "/api/conversations/%s/checkpoints/rollback-plan" % cid,
            body={"since_run_no": 1})
        expect(body["rollback_plan"]["runs_included"] == [2],
              "since_run_no excludes the older checkpoint: %r"
              % body["rollback_plan"]["runs_included"])

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

    def test_rate_limiter_blocks_a_runaway_peer_but_not_normal_use(self):
        peer = "203.0.113.88"
        server_mod._rate_hits.pop(peer, None)
        try:
            for _ in range(server_mod._RATE_LIMIT):
                expect(not server_mod._rate_limited(peer),
                       "must not rate-limit a peer below the ceiling")
            expect(server_mod._rate_limited(peer),
                   "must rate-limit once a single peer exceeds the "
                   "ceiling within the window (guards against a buggy "
                   "retry loop running up cost against a metered "
                   "provider)")
        finally:
            server_mod._rate_hits.pop(peer, None)

    def test_rate_limiter_returns_429_over_real_http_once_tripped(self):
        # Exercise the real dispatch path (_auth_gate), not just the pure
        # counting function, so a wiring mistake would be caught too.
        peer = "127.0.0.1"
        server_mod._rate_hits.pop(peer, None)
        try:
            now = time.time()
            server_mod._rate_hits[peer] = [now] * server_mod._RATE_LIMIT
            s, body = self.req("GET", "/api/health")
            expect(s == 429, "a request past the ceiling must get 429, "
                             "got %r" % s)
            expect(body.get("error", {}).get("code") == server_mod.ERR_USER,
                  "rate-limit errors must use the user error code")
        finally:
            server_mod._rate_hits.pop(peer, None)
        # Must recover immediately once the window's hits are cleared —
        # this is a sliding window, not a lockout.
        s, _b = self.req("GET", "/api/health")
        expect(s == 200, "must recover once old hits age out/are cleared")

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
