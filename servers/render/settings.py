"""Configuration for the render proxy.

The location of the render service is the one required setting and it has no
default, for the same reason `PROMETHEUS_URL` has none: a guessed address turns
every render into a failure, which reads as a broken renderer rather than a
missing setting. The timeout is an operational value with a safe default - a
render is Chrome capture plus encode, so it is seconds, not milliseconds.
"""

from __future__ import annotations

from mcp_runner.config import env, require_url


def render_url() -> str:
    return require_url("RENDER_URL", "the visual render service").rstrip("/")


def render_timeout() -> float:
    return float(env("RENDER_TIMEOUT_SECONDS", "240"))
