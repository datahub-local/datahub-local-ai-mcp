"""Process-wide configuration: the registry file, and the warehouse behind it.

Exactly **one** file is mounted: `registry.yaml`, the contract that says what
each metric means. It is mounted rather than baked in because the image is
generic and the definitions belong to whichever deployment owns them;
`SEMANTIC_REGISTRY_VERSION` identifies the mounted registry.

Everything else is read from Trino. A pruned dbt manifest and a precomputed
sample sidecar used to arrive alongside it, and both described the warehouse -
which is authoritative about itself, and never stale. Their replacements live in
`warehouse.py` behind a TTL cache that serves the last good value when Trino is
unreachable, so an outage costs freshness rather than the tool.

What that shifts, and what it does not:

- the registry is still **fatal** if missing, and still fully validated at first
  use, so a registry that reaches the cluster has already passed the gate a PR
  would have applied.
- table names still come from a dbt-manifest-shaped dict, now synthesized from
  `information_schema`. This is only equivalent because dbt persists its
  `schema.yml` descriptions as column comments (`persist_docs`): the manifest
  carried documented columns only, so a NULL comment is what reproduces
  "undocumented". A column listed in `schema.yml` with a blank description
  arrives as NULL and reads as undocumented - which is why the dbt project
  requires a description on every column it lists.
- cardinality and dimension values are read live, so `list_dimensions` no longer
  has a names-only mode. It degrades to stale values instead, and says so only
  in the log.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from mcp_runner.config import tool_cap
from mcp_runner.trino import TrinoError

from .registry import Registry, bind_tables
from .registry import load as load_registry
from .warehouse import Warehouse

logger = logging.getLogger(__name__)

# Outside the code tree, and that is load-bearing rather than tidy. `/app` is
# WORKDIR and lands on `sys.path` for `python -m`, so a mount at `/app/semantic/`
# shadows the `semantic` package itself and the server dies at startup with
# "module 'semantic' has no attribute 'register'" - the same collision also
# breaks pytest collection. The monorepo dodged it by naming the directory
# `semantic_registry`; `/etc/mcp/<server>/` cannot collide at all.
DEFAULT_CONFIG_DIR = "/etc/mcp/semantic"

_DEFAULT_REGISTRY = f"{DEFAULT_CONFIG_DIR}/registry.yaml"

# Which `catalog.schema` pairs to read structure from. Required: the registry
# names models by `ref()`, which carries no catalog, so nothing in it can tell
# the server where to look. A wrong or missing scope surfaces as "ref() matches
# no dbt model" from the registry gate, which names the refs it did find.
_SCOPES_VAR = "SEMANTIC_WAREHOUSE_SCOPES"

# Held for near-miss matching, which is why the cap is this high: the dimension
# most likely to be filtered on is usually the highest-cardinality one, so a
# tidier number would drop the suggestion exactly where it is needed.
MAX_MATCH_VALUES = tool_cap("match_values")


def registry_path() -> Path:
    return Path(os.environ.get("SEMANTIC_REGISTRY_PATH", _DEFAULT_REGISTRY))


def registry_version() -> str:
    return os.environ.get("SEMANTIC_REGISTRY_VERSION", "unknown")


def scopes() -> tuple[tuple[str, str], ...]:
    """`(catalog, schema)` pairs to read table structure from.

    Configured as `<catalog>.<schema>,<catalog>.<schema>`. Raises rather than
    guessing: a guessed catalog reads an empty `information_schema` and every
    ref() then fails to resolve, which reports as a broken registry instead of
    a missing setting.
    """
    raw = os.environ.get(_SCOPES_VAR, "").strip()
    if not raw:
        raise ValueError(
            f"{_SCOPES_VAR} is unset, so there is nowhere to read table structure from. "
            f"Set it to a comma-separated list of catalog.schema pairs, e.g. "
            f"'silver.sales,gold.sales'. There is deliberately no default: a guessed "
            f"catalog resolves no models and reports as a broken registry."
        )
    parsed = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(".")
        if len(parts) != 2:
            raise ValueError(f"{_SCOPES_VAR} entry {item!r} is not catalog.schema")
        parsed.append((parts[0], parts[1]))
    if not parsed:
        raise ValueError(f"{_SCOPES_VAR} names no catalog.schema pair")
    return tuple(parsed)


@lru_cache(maxsize=1)
def warehouse() -> Warehouse:
    return Warehouse()


@lru_cache(maxsize=1)
def registry() -> Registry:
    """The registry, loaded and validated once against the live warehouse.

    Validation runs here as well as in CI: the same rules that fail a PR fail
    the server's boot, so a registry that reaches the cluster has already passed
    the gate a PR would have applied.
    """
    path = registry_path()
    manifest = warehouse().manifest(scopes())
    if not (manifest.get("nodes") or {}):
        raise ValueError(
            f"no tables found in {', '.join(f'{c}.{s}' for c, s in scopes())}: table names are "
            f"resolved from the warehouse, so the registry cannot be bound without them"
        )
    loaded = load_registry(path, version=registry_version(), manifest=manifest)
    bind_tables(loaded, manifest)
    unbound = [model.name for model in loaded.models.values() if not model.table]
    if unbound:
        raise ValueError(f"semantic_models with no table in the warehouse: {', '.join(unbound)}")
    logger.info(
        "registry loaded version=%s metrics=%d models=%d",
        loaded.version,
        len(loaded.metrics),
        len(loaded.models),
    )
    return loaded


def samples(model: str) -> dict:
    """Per-dimension `{cardinality, values}` for one model, read from Trino.

    Returns `{}` when the model is unknown or the warehouse cannot be reached
    with nothing cached, which keeps `list_dimensions` answering with names.
    """
    if not model:
        return {}
    loaded = registry()
    found = loaded.models.get(model)
    if found is None or not found.table:
        return {}

    try:
        counts = warehouse().cardinality(found.table)
    except TrinoError as exc:
        logger.warning("cardinality unavailable for %s: %s", found.table, exc)
        counts = {}

    out: dict[str, dict] = {}
    for dimension in found.dimensions.values():
        if dimension.is_time:
            continue
        cardinality = counts.get(dimension.expr)
        values: list[str] = []
        if cardinality is None or cardinality > 0:
            try:
                values = warehouse().values(found.table, dimension.expr, MAX_MATCH_VALUES)
            except TrinoError as exc:
                logger.warning(
                    "values unavailable for %s.%s: %s", found.table, dimension.expr, exc
                )
        out[dimension.name] = {
            # Iceberg's own estimate when it has one; otherwise what was read,
            # which for a dimension under the cap is the exact count.
            "cardinality": cardinality if cardinality is not None else len(values),
            "values": values,
        }
    return out


def sample_values(model: str) -> dict[str, list[str]]:
    """Just the known values per dimension, for near-miss filter validation."""
    return {
        name: detail.get("values", [])
        for name, detail in samples(model).items()
        if detail.get("values")
    }
