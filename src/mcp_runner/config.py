"""Env-driven configuration.

Nothing here carries a cluster address. An earlier version of this file defaulted
to one specific homelab's service names, which is exactly the failure mode this
repository exists to avoid: a second deployment that forgot to set `PROMETHEUS_URL`
would silently query a service that does not exist in its cluster and report every
metric `unavailable` - a wrong endpoint rendering as an absent one.

So a backend URL has **no default**. `require_url` raises at first use, naming the
variable, and the tool that needed it says so. Raising at first use rather than at
import is the deliberate half: the server must still start, still answer
`/healthz`, and still serve the tools that need no backend, so a missing Loki URL
costs `logs()` and nothing else.

Timeouts, state and config paths keep defaults - those are operational
preferences with a safe value, not claims about where anything lives.
"""

from __future__ import annotations

import os


class ConfigError(RuntimeError):
    """A required setting is unset. Raised at first use, never at import."""


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in (None, "") else default


def require_url(name: str, what: str) -> str:
    """A backend address, or a readable failure naming the variable to set."""
    value = env(name)
    if not value:
        raise ConfigError(
            f"{name} is unset, so {what} cannot be reached. Set it to the base URL of "
            f"{what} in this cluster. There is deliberately no default: a guessed "
            f"address reports every reading as unavailable instead of failing."
        )
    return value


def prometheus_url() -> str:
    """Prometheus, queried directly rather than through Grafana.

    Going direct means there is no datasource uid on this path, so none can be
    resolved to the wrong datasource - the value a 4B model resolved to Loki's
    hex uid, 404ing every query against a prompt that stated the right one.
    """
    return require_url("PROMETHEUS_URL", "Prometheus")


def prometheus_timeout() -> float:
    return float(env("PROMETHEUS_TIMEOUT_SECONDS", "20"))


def loki_url() -> str:
    """Loki, queried directly rather than through Grafana, for the same reason.

    The client expects `/loki/api/v1/*` with no auth and no tenant header, and a
    label set including `namespace`, `pod` and `container`.
    """
    return require_url("LOKI_URL", "Loki")


def loki_timeout() -> float:
    return float(env("LOKI_TIMEOUT_SECONDS", "20"))


def state_dir() -> str:
    """Where snapshots for the computed diffs live. An emptyDir is enough."""
    return env("MCP_STATE_DIR", "/tmp/mcp-state")


def trino_url() -> str:
    """Trino's HTTP endpoint. Required, same reasoning as every other backend."""
    return require_url("TRINO_URL", "Trino")


def trino_user() -> str:
    """The user Trino authorises the statement as.

    A label rather than a credential on a Trino with `access-control.name=file`
    and no authentication - but not a free-form one: the rules file lists users
    explicitly with no catch-all, so an unlisted name is denied every statement.
    Default `mcp`, which is the read-only entry.
    """
    return env("TRINO_USER", "mcp")


def trino_timeout() -> float:
    return float(env("TRINO_TIMEOUT_SECONDS", "30"))


def semantic_cache_ttl() -> float:
    """How long warehouse-derived metadata is reused before being re-read.

    Table structure and dimension values change when the pipeline runs, which is
    daily here, so a short TTL would spend queries to observe nothing. The point
    of the cache is not speed but blast radius: an expired entry that cannot be
    refreshed keeps serving the previous answer, so a Trino outage degrades
    `list_dimensions` rather than failing it.
    """
    return float(env("SEMANTIC_CACHE_TTL_SECONDS", "3600"))


def config_dir() -> str | None:
    """The mounted config directory, or ``None`` when nothing is mounted.

    This is the ConfigMap mount point, not a test hook. A server that needs it
    decides for itself whether absence is fatal or a degraded mode; see
    ``servers/homelab_facts/settings.py``, which degrades and says so.
    """
    return env("MCP_CONFIG_DIR")


def garage_admin_url() -> str | None:
    """Garage's admin API, which is the only place per-bucket usage exists.

    Prometheus carries no bucket label and no stored-bytes gauge, so bucket size
    and object count are reachable only here. Unlike Prometheus and Loki this
    endpoint needs a bearer token, so it is an optional dependency: without a
    token the bucket section reports itself unavailable and every other reading
    is unaffected.

    Hence ``None`` rather than ``require_url``. Garage is the one backend whose
    absence is a *designed* state, so raising here would convert a degraded
    bucket section into a dead ``object_store_health`` tool - `Garage()` is
    constructed by a cached accessor that runs whether or not a token exists,
    so the raise would land at construction and take the eleven Prometheus
    readings in that tool down with it. The unconfigured path is
    ``GarageUnconfigured``, which the caller already renders as `unavailable`.
    """
    return env("GARAGE_ADMIN_URL")


def garage_admin_token() -> str | None:
    """The bearer token, or ``None`` when the server is deliberately without one.

    ``None`` is the default and a supported state, not a misconfiguration.
    """
    return env("GARAGE_ADMIN_TOKEN")


def garage_timeout() -> float:
    return float(env("GARAGE_TIMEOUT_SECONDS", "10"))
