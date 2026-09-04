"""Warehouse-derived metadata: table structure, cardinality, dimension values.

Everything here used to arrive as two files in a ConfigMap - a pruned dbt
manifest and a precomputed sample sidecar - refreshed by whatever built them.
Both described the warehouse, and the warehouse is authoritative about itself,
so both are read from it now. What stays a file is the registry: it is the
*contract* - what each metric means - which no warehouse can answer.

Two properties are deliberate.

**Stale beats absent.** Every entry keeps its last good value. A refresh that
fails logs and returns the previous answer, so a Trino outage costs freshness
rather than `list_dimensions`. Only a cold cache with no prior value can fail,
and it fails loudly.

**A synthesized manifest, not a new interface.** `registry.py` validates against
a dbt manifest dict and is unchanged - `manifest()` builds the same shape from
`information_schema`. The column comments dbt persists via `persist_docs` are
what make this equivalent: the manifest carried *documented* columns only, so
the gate meant "documented in schema.yml", and a NULL comment reproduces
"undocumented" exactly. A dbt project feeding this must therefore give every
column it lists a non-empty description, or the gate silently weakens to
"the column exists".
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from mcp_runner import config
from mcp_runner.trino import Trino, TrinoError

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    value: object
    fetched_at: float


@dataclass
class Warehouse:
    """Cached, code-owned reads against one Trino.

    Not a general query interface: the public methods are the four questions the
    semantic tools ask, and each composes its own SQL from validated inputs.
    """

    client: Trino | None = None
    ttl: float | None = None
    _cache: dict[str, _Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = Trino()
        if self.ttl is None:
            self.ttl = config.semantic_cache_ttl()

    def _cached(self, key: str, produce):
        """Return a fresh value, or the previous one if refreshing fails."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and (time.monotonic() - entry.fetched_at) < self.ttl:
                return entry.value
        try:
            value = produce()
        except TrinoError as exc:
            with self._lock:
                entry = self._cache.get(key)
            if entry is not None:
                logger.warning("trino refresh failed for %s, serving stale value: %s", key, exc)
                return entry.value
            logger.error("trino read failed for %s with nothing cached: %s", key, exc)
            raise
        with self._lock:
            self._cache[key] = _Entry(value=value, fetched_at=time.monotonic())
        return value

    def manifest(self, scopes) -> dict:
        """A dbt-manifest-shaped dict built from `information_schema`.

        `scopes` is an iterable of `(catalog, schema)` pairs to read. Only
        columns carrying a comment are included, because the comment is the
        persisted `schema.yml` description and its absence is what the manifest
        expressed by omitting the column.
        """
        key = "manifest:" + ",".join(f"{c}.{s}" for c, s in sorted(scopes))

        def produce() -> dict:
            nodes = {}
            for catalog, schema in sorted(scopes):
                for table, columns in self.client.columns(catalog, schema).items():
                    documented = {
                        name: {"description": comment}
                        for name, comment in columns.items()
                        if comment is not None and comment.strip()
                    }
                    nodes[f"model.{schema}.{table}"] = {
                        "name": table,
                        "resource_type": "model",
                        "database": catalog,
                        "schema": schema,
                        "alias": table,
                        "columns": documented,
                    }
            undocumented = [
                node["name"] for node in nodes.values() if not node["columns"]
            ]
            if undocumented:
                logger.warning(
                    "tables with no documented columns (persist_docs not run, or descriptions "
                    "left blank in schema.yml): %s",
                    ", ".join(sorted(undocumented)),
                )
            logger.info("manifest built from warehouse: %d table(s)", len(nodes))
            return {"nodes": nodes}

        return self._cached(key, produce)

    def cardinality(self, table: str) -> dict[str, int]:
        """Distinct counts per column, from Iceberg statistics."""
        return self._cached(f"stats:{table}", lambda: self.client.distinct_counts(table))

    def values(self, table: str, column: str, limit: int) -> list[str]:
        """Most frequent values of one column, for display and near-miss matching."""
        return self._cached(
            f"values:{table}.{column}:{limit}",
            lambda: self.client.top_values(table, column, limit),
        )

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
