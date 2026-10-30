"""The MCP transport and the tool boundary.

Core's `mcp-k8s` 404'd for three days with `MCPServer.status.ready: true`
throughout, because the discovery bridge asked for the service root and the
server served `/mcp`. Every `k8s_*` tool was missing from every persona for that
whole time and nothing failed loudly. Hence the path-agnostic tests below.
"""

from __future__ import annotations

import json

from mcp_runner.server import PROTOCOL_VERSION, Registry, build_app, handle_rpc


def registry_with(handler=None, budget=4096) -> Registry:
    registry = Registry()
    registry.add(
        "probe",
        "A probe tool.",
        handler or (lambda: "pong"),
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        budget=budget,
    )
    return registry


class TestJsonRpc:
    def test_initialize_reports_the_protocol_version(self):
        reply = handle_rpc(registry_with(), {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION

    def test_tools_list_returns_name_description_and_schema(self):
        reply = handle_rpc(registry_with(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool = reply["result"]["tools"][0]
        assert set(tool) == {"name", "description", "inputSchema"}

    def test_tools_call_returns_text_content(self):
        reply = handle_rpc(
            registry_with(),
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "probe"}},
        )
        assert reply["result"]["content"] == [{"type": "text", "text": "pong"}]

    def test_a_notification_gets_no_reply(self):
        # Answering a notification is a protocol violation and some bridges hang.
        assert handle_rpc(registry_with(), {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_an_unknown_tool_is_an_error_not_a_crash(self):
        reply = handle_rpc(
            registry_with(),
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},
        )
        assert reply["error"]["code"] == -32602

    def test_an_unknown_method_is_reported(self):
        reply = handle_rpc(registry_with(), {"jsonrpc": "2.0", "id": 5, "method": "wat"})
        assert reply["error"]["code"] == -32601

    def test_ping_is_answered(self):
        reply = handle_rpc(registry_with(), {"jsonrpc": "2.0", "id": 6, "method": "ping"})
        assert reply["result"] == {}


class TestToolBoundary:
    def test_a_raising_tool_becomes_readable_text_not_a_protocol_error(self):
        def boom():
            raise RuntimeError("kaboom")

        reply = handle_rpc(
            registry_with(boom),
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "probe"}},
        )
        # A protocol error tends to end the run; readable text lets the model retry.
        assert "ERROR" in reply["result"]["content"][0]["text"]
        assert "kaboom" in reply["result"]["content"][0]["text"]

    def test_bad_arguments_are_explained_rather_than_raised(self):
        registry = registry_with(lambda: "pong")
        out = registry.call("probe", {"unexpected": 1})
        assert out.startswith("ERROR: bad arguments")

    def test_the_budget_is_enforced_at_the_boundary_too(self):
        # Belt and braces: a new tool that forgets to truncate cannot blow the
        # budget, because the registry clamps every result.
        registry = registry_with(lambda: "x" * 10_000, budget=1024)
        out = registry.call("probe", {})
        assert len(out.encode()) <= 1024

    def test_non_ascii_is_folded_at_the_boundary(self):
        # `status.result` is dropped whenever the reply carries invalid UTF-8, and
        # the old delivery header told the model to echo a middot verbatim.
        registry = registry_with(lambda: "a · b — c")
        out = registry.call("probe", {})
        out.encode("ascii")
        assert "-" in out

    def test_duplicate_tool_names_are_rejected_at_registration(self):
        registry = registry_with()
        try:
            registry.add("probe", "again", lambda: "x")
        except ValueError:
            return
        raise AssertionError("a duplicate tool name must not be accepted")


class TestTransport:
    """The MCP endpoint answers on every path, because the bridge's path is unknown."""

    def _post(self, app, path, body, accept="application/json"):
        import asyncio

        messages = []

        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"accept", accept.encode())],
        }
        asyncio.run(app(scope, receive, send))
        return messages

    def _get(self, app, path):
        import asyncio

        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(app({"type": "http", "method": "GET", "path": path, "headers": []}, receive, send))
        return messages

    def test_the_service_root_serves_mcp(self):
        app = build_app(registry_with())
        out = self._post(app, "/", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert out[0]["status"] == 200
        assert b"probe" in out[1]["body"]

    def test_the_mcp_path_serves_mcp(self):
        # kubernetes-mcp-server serves /mcp and core's bridge asked for /. Both work here.
        app = build_app(registry_with())
        out = self._post(app, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert out[0]["status"] == 200

    def test_an_arbitrary_path_still_serves_mcp(self):
        app = build_app(registry_with())
        for path in ("/rpc", "/api/v1/mcp", "/anything/at/all"):
            out = self._post(app, path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert out[0]["status"] == 200, path

    def test_healthz_is_not_swallowed_by_the_catch_all(self):
        app = build_app(registry_with())
        out = self._get(app, "/healthz")
        assert out[0]["status"] == 200
        assert b"ok" in out[1]["body"]

    def test_an_sse_only_client_gets_an_sse_frame(self):
        app = build_app(registry_with())
        out = self._post(
            app, "/", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, accept="text/event-stream"
        )
        assert b"event: message" in out[1]["body"]

    def test_a_json_client_gets_plain_json(self):
        app = build_app(registry_with())
        out = self._post(app, "/", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert json.loads(out[1]["body"])["result"] == {}

    def test_malformed_json_is_a_parse_error_not_a_crash(self):
        import asyncio

        app = build_app(registry_with())
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"{not json", "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(
            app({"type": "http", "method": "POST", "path": "/", "headers": []}, receive, send)
        )
        assert json.loads(messages[1]["body"])["error"]["code"] == -32700

    def test_no_session_id_is_required(self):
        # A bridge that does not carry Mcp-Session-Id between calls must still work.
        app = build_app(registry_with())
        out = self._post(app, "/", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        headers = dict(out[0]["headers"])
        assert b"mcp-session-id" not in {key.lower() for key in headers}


class TestTheLoopStaysFreeWhileAToolRuns:
    """A probe is answered by the event loop, so a tool must not occupy it.

    Measured 2026-10-30: `/healthz` waited for the whole tool call, so a report
    of dozens of sequential Prometheus queries timed the probes out for minutes
    — first dropping the pod out of its Service (callers hung until their own
    deadline) and then dying on the liveness threshold with the call in flight.
    """

    def _request(self, app, method, path, body=None):
        messages = []

        async def receive():
            return {"type": "http.request", "body": body or b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "method": method, "path": path, "headers": []}
        return app(scope, receive, send), messages

    def _call(self, name="probe"):
        return json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}
        ).encode()

    def test_healthz_answers_while_a_tool_is_still_running(self):
        import asyncio
        import threading
        import time

        released = threading.Event()
        started = threading.Event()

        def blocking_tool():
            started.set()
            released.wait(timeout=5)
            return "blocked tool done"

        app = build_app(registry_with(blocking_tool))

        async def scenario():
            tool, tool_messages = self._request(app, "POST", "/", self._call())
            running = asyncio.create_task(tool)
            # Without the fix this loop cannot be scheduled at all while the tool
            # blocks, so the watchdog below is what fails the test, not the await.
            for _ in range(500):
                await asyncio.sleep(0.01)
                if started.is_set():
                    break

            probe, probe_messages = self._request(app, "GET", "/healthz")
            issued = time.monotonic()
            await asyncio.wait_for(probe, timeout=1.0)
            waited = time.monotonic() - issued

            assert not released.is_set(), "the probe answered only once the tool had finished"
            released.set()
            await running
            return waited, probe_messages, tool_messages

        watchdog = threading.Timer(2.0, released.set)
        watchdog.start()
        try:
            waited, probe_messages, tool_messages = asyncio.run(scenario())
        finally:
            watchdog.cancel()
            released.set()

        assert probe_messages[0]["status"] == 200
        assert waited < 0.5, f"/healthz waited {waited:.2f}s for the tool"
        assert b"blocked tool done" in tool_messages[1]["body"]

    def test_the_tool_runs_off_the_event_loop_thread(self):
        import asyncio
        import threading

        threads = {}

        def tool():
            threads["tool"] = threading.get_ident()
            return "ok"

        app = build_app(registry_with(tool))

        async def scenario():
            threads["loop"] = threading.get_ident()
            post, _ = self._request(app, "POST", "/", self._call())
            await post

        asyncio.run(scenario())
        assert threads["tool"] != threads["loop"]

    def test_tool_calls_do_not_overlap(self):
        # One worker, deliberately: the tools share the snapshot directory, and a
        # pool would let two diff-based readings of the same key interleave.
        import asyncio
        import time

        active = 0
        peak = 0

        def tool():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.1)
            active -= 1
            return "ok"

        app = build_app(registry_with(tool))

        async def scenario():
            first, _ = self._request(app, "POST", "/", self._call())
            second, _ = self._request(app, "POST", "/", self._call())
            await asyncio.gather(first, second)

        asyncio.run(scenario())
        assert peak == 1

