"""OpenCode - the coding agent as seven MCP tools.

The agent's HTTP API has no MCP server, and Hermes and Sympozium are MCP clients.
This is the bridge: sessions in, status out, one turn at a time. It holds no state
of its own — every answer comes from the server, so a restarted shim resumes the
same session and cannot start a second writer.
"""

from __future__ import annotations

from mcp_runner.config import tool_budget
from mcp_runner.server import Registry

from . import tools


def register(registry: Registry) -> None:
    registry.add(
        "run_task",
        tools.RUN_TASK_DESCRIPTION,
        tools.run_task,
        schema=tools.RUN_TASK_SCHEMA,
        budget=tool_budget("run_task"),
    )
    registry.add(
        "get_session",
        tools.GET_SESSION_DESCRIPTION,
        tools.get_session,
        schema=tools.GET_SESSION_SCHEMA,
        budget=tool_budget("get_session"),
    )
    registry.add(
        "respond",
        tools.RESPOND_DESCRIPTION,
        tools.respond,
        schema=tools.RESPOND_SCHEMA,
        budget=tool_budget("respond"),
    )
    registry.add(
        "get_diff",
        tools.GET_DIFF_DESCRIPTION,
        tools.get_diff,
        schema=tools.GET_DIFF_SCHEMA,
        budget=tool_budget("get_diff"),
    )
    registry.add(
        "list_sessions",
        tools.LIST_SESSIONS_DESCRIPTION,
        tools.list_sessions,
        schema=tools.LIST_SESSIONS_SCHEMA,
        budget=tool_budget("list_sessions"),
    )
    registry.add(
        "get_transcript",
        tools.GET_TRANSCRIPT_DESCRIPTION,
        tools.get_transcript,
        schema=tools.GET_TRANSCRIPT_SCHEMA,
        budget=tool_budget("get_transcript"),
    )
    registry.add(
        "abort_session",
        tools.ABORT_SESSION_DESCRIPTION,
        tools.abort_session,
        schema=tools.ABORT_SESSION_SCHEMA,
        budget=tool_budget("abort_session"),
    )
