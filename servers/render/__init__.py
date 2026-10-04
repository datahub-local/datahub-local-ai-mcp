"""render - turn a content spec into a short video, from an agent.

The Visual Studio workflow already renders a spec to an MP4 by calling the
visual render service directly; agents speak MCP, so this server is the same
capability behind an MCP tool. It is a thin proxy by design: it owns no
templates and does no rendering.

**It returns a reference, not the bytes.** An MP4 is hundreds of KB and every
tool answer here is clamped to a few KB (`Registry.call`), so returning the file
would be a truncated, unplayable video rather than a large one. The tool returns
an id and a URL the caller can fetch instead - the same move `homelab_facts`
makes when it returns a report section rather than the raw API response.

One thing is required and has no default: `RENDER_URL`, the base address of the
render service. A guessed address makes every render fail, which would render a
wrong endpoint as a broken renderer - the failure `require_url` exists to avoid.
"""

from __future__ import annotations

from mcp_runner.server import Registry

from .tools import render


def register(registry: Registry) -> None:
    registry.add(
        "render_asset",
        render.RENDER_DESCRIPTION,
        render.render_asset,
        schema=render.RENDER_SCHEMA,
        budget=render.RENDER_BUDGET,
    )
