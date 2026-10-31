"""Settings for the OpenCode shim.

The server address has no default: a guessed address would report a healthy agent
while every task failed against the wrong cluster. The credential has none either,
because the API answers 401 without it and a default would only hide that until the
first call.
"""

from __future__ import annotations

from mcp_runner.config import ConfigError, env, require_url

DEFAULT_WAIT_SECONDS = 120
MAX_WAIT_SECONDS = 300


def server_url() -> str:
    return require_url("OPENCODE_SERVER_URL", "the OpenCode server").rstrip("/")


def username() -> str:
    return env("OPENCODE_SERVER_USERNAME", "opencode") or "opencode"


def password() -> str:
    value = env("OPENCODE_SERVER_PASSWORD")
    if not value:
        raise ConfigError(
            "OPENCODE_SERVER_PASSWORD is unset, so the OpenCode API cannot be called. "
            "It is the same Basic credential the web proxy injects."
        )
    return value


def workspace_root() -> str:
    """The only tree a task may start in; the shim never mounts it."""
    return env("OPENCODE_WORKSPACE_ROOT", "/workspace") or "/workspace"


def request_timeout() -> float:
    return float(env("OPENCODE_REQUEST_TIMEOUT_SECONDS", "20"))


def wait_seconds(value: int | None) -> int:
    """Clamp the caller's wait to the documented default and ceiling.

    A turn is unbounded, so the tool's wait is a polling budget, not a timeout on
    the session: exceeding it returns `running`, never an abort.
    """
    if value is None:
        return DEFAULT_WAIT_SECONDS
    return max(1, min(int(value), MAX_WAIT_SECONDS))
