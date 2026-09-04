"""MCP over HTTP: tool registry, JSON-RPC dispatch, health endpoints.

Two deliberate transport choices. The MCP endpoint answers on **every** non-health
path, because the discovery bridge's path is not documented anywhere readable and
guessing it wrong fails silently. And it is stateless, so a bridge that does not
carry a session id between calls works unchanged. See ../README.md.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .budget import DEFAULT_BUDGET_BYTES, clamp
from .render import ascii_only

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"

_HEALTH_PATHS = frozenset({"/healthz", "/readyz", "/livez"})


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[..., str]
    budget: int = DEFAULT_BUDGET_BYTES


@dataclass
class Registry:
    # Carried here rather than passed through `handle_rpc`/`build_app`, because
    # the registry is the one object that already reaches both. It is what
    # `initialize` reports as `serverInfo.name`, so a bridge listing several of
    # these images distinguishes them; the default keeps a bare `Registry()` in
    # a test from having to name itself.
    name: str = "mcp-server"
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(
        self,
        name: str,
        description: str,
        handler: Callable[..., str],
        *,
        schema: dict[str, Any] | None = None,
        budget: int = DEFAULT_BUDGET_BYTES,
    ) -> None:
        if name in self.tools:
            raise ValueError(f"duplicate tool {name!r}")
        self.tools[name] = Tool(
            name=name,
            description=description.strip(),
            schema=schema or {"type": "object", "properties": {}, "additionalProperties": False},
            handler=handler,
            budget=budget,
        )

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": tool.name, "description": tool.description, "inputSchema": tool.schema}
            for tool in sorted(self.tools.values(), key=lambda item: item.name)
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            raise KeyError(name)
        try:
            result = tool.handler(**(arguments or {}))
        except TypeError as exc:
            # Returned as tool content, not a protocol error: an error response
            # tends to end the run, where readable text lets the model retry.
            return f"ERROR: bad arguments for {name}: {exc}"
        except Exception as exc:
            logger.exception("tool %s failed", name)
            return f"ERROR: {name} failed: {exc}"
        # Folded and clamped whatever the tool returned, so a new tool cannot
        # forget its budget.
        return clamp(ascii_only(result), tool.budget)


# -- JSON-RPC ----------------------------------------------------------------


def handle_rpc(registry: Registry, message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. ``None`` means notification — send no reply."""
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if method is None:
        return _error(message_id, -32600, "not a request")

    # Notifications carry no id and must not be answered.
    if message_id is None:
        return None

    if method == "initialize":
        return _ok(
            message_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": registry.name, "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _ok(message_id, {})
    if method in ("tools/list", "list_tools"):
        return _ok(message_id, {"tools": registry.describe()})
    if method in ("tools/call", "call_tool"):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            return _error(message_id, -32602, "missing tool name")
        try:
            text = registry.call(name, arguments)
        except KeyError:
            return _error(message_id, -32602, f"unknown tool {name!r}")
        # `isError` is deliberately unset: the text is the useful part, and a
        # protocol error is more likely to end the run than prompt a retry.
        return _ok(message_id, {"content": [{"type": "text", "text": text}]})

    return _error(message_id, -32601, f"unknown method {method!r}")


def _ok(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


# -- ASGI --------------------------------------------------------------------


def build_app(registry: Registry) -> Callable:
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET")
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }

        if path in _HEALTH_PATHS:
            await _respond(send, 200, b'{"status":"ok"}', "application/json")
            return

        # A GET opens a server-initiated SSE stream. This server never initiates
        # anything, so declining keeps the connection from being held open.
        if method == "GET":
            await _respond(send, 405, b'{"error":"POST JSON-RPC to this path"}', "application/json")
            return
        if method != "POST":
            await _respond(send, 405, b'{"error":"method not allowed"}', "application/json")
            return

        body = await _read_body(receive)
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            await _send_rpc(send, headers, _error(None, -32700, "parse error"))
            return

        if isinstance(payload, list):
            replies = [reply for reply in (handle_rpc(registry, item) for item in payload) if reply]
            if not replies:
                await _respond(send, 202, b"", "application/json")
                return
            await _send_rpc(send, headers, replies)
            return

        reply = handle_rpc(registry, payload)
        if reply is None:
            await _respond(send, 202, b"", "application/json")
            return
        await _send_rpc(send, headers, reply)

    return app


async def _read_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body") or b"")
        if not message.get("more_body"):
            break
    return b"".join(chunks)


async def _send_rpc(send, headers: dict[str, str], payload: Any) -> None:
    """Reply as JSON, or as a single SSE event if that is all the client accepts."""
    encoded = json.dumps(payload).encode("utf-8")
    accept = headers.get("accept", "")
    if "text/event-stream" in accept and "application/json" not in accept:
        await _respond(send, 200, b"event: message\ndata: " + encoded + b"\n\n", "text/event-stream")
        return
    await _respond(send, 200, encoded, "application/json")


async def _respond(send, status: int, body: bytes, content_type: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode("latin-1")),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
