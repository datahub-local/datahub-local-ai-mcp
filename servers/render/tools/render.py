"""`render_asset` and the reference it returns.

The caller sends a content spec (the same typed spec the Visual Studio workflow
authors), the service renders it and stores it, and this tool hands back the id,
URL, duration, frame count and dimensions. Fetching the file is the caller's
job, which is what keeps this answer inside a few KB.
"""

from __future__ import annotations

import json
import logging

import httpx

from mcp_runner.config import tool_budget

from .. import settings

logger = logging.getLogger(__name__)

RENDER_BUDGET = tool_budget("render_asset")

RENDER_DESCRIPTION = """
Render a content spec to a short MP4 motion graphic and return a reference to it.
The spec is a JSON object: `title`, 1-8 `blocks` of `{label, value}` (each may
carry an `icon`), an `accent` hex colour, and an optional `layout` of `stats`,
`flow`, `timeline`, `comparison` or `bars`. You get back an id and a URL - fetch
the URL for the video; the bytes are not returned here.
"""

RENDER_SCHEMA = {
    "type": "object",
    "properties": {
        "spec": {
            "type": "string",
            "description": "The content spec as a JSON object string.",
        }
    },
    "required": ["spec"],
    "additionalProperties": False,
}


def render_asset(spec: str, **_: object) -> str:
    try:
        parsed = json.loads(spec) if isinstance(spec, str) else spec
    except (TypeError, ValueError) as exc:
        return f"INVALID: spec is not JSON: {exc}"
    if not isinstance(parsed, dict):
        return "INVALID: spec must be a JSON object"

    base = settings.render_url()  # raises ConfigError, naming RENDER_URL
    try:
        response = httpx.post(
            f"{base}/render",
            json={"spec": parsed, "store": True},
            timeout=settings.render_timeout(),
        )
    except httpx.HTTPError as exc:
        return f"RENDER SERVICE ERROR: {exc}. The service is configured but did not answer."

    if response.status_code != 200:
        return f"RENDER FAILED: {_reason(response)}"

    try:
        data = response.json()
    except ValueError:
        return "RENDER FAILED: the service returned a non-JSON body"

    return _summary(base, data)


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300]}"
    return str(body.get("error") or f"HTTP {response.status_code}")


def _summary(base: str, data: dict) -> str:
    duration_ms = _int(data.get("durationMs"))
    fps = data.get("fps") or ""
    lines = [
        "rendered mp4",
        f"id: {data.get('id', '')}",
        f"url: {base}{data.get('url', '')}",
        f"duration: {duration_ms / 1000:.2f}s ({_int(data.get('frames'))} frames, {fps})",
        f"size: {data.get('width', '?')}x{data.get('height', '?')}",
    ]
    return "\n".join(lines)


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
