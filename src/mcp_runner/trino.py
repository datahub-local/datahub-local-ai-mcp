"""The only module that talks to Trino.

Deliberately narrow. The semantic server exposes no `run_sql` and this does not
change that: every statement sent from here is composed *in code* from a
registry the server already validated, never from model output. What the model
can influence is which registered dimension it asks about, and a dimension's
`expr` is checked to be a bare column reference before it reaches a query.

That constraint is what makes the identifier interpolation below safe. Trino's
HTTP protocol has no bind parameters for identifiers, so a table or column name
has to be interpolated; `_identifier` re-checks each part rather than trusting
the caller, because the alternative is a validated registry becoming the only
thing standing between model output and a query.
"""

from __future__ import annotations

import logging
import time

import httpx

from . import config

logger = logging.getLogger(__name__)


class TrinoError(RuntimeError):
    """A statement failed. Distinct from an empty result, which is an answer."""


def _identifier(part: str) -> str:
    """One unquoted SQL identifier, or a refusal.

    Catalog, schema, table and column names all pass through here. dbt's own
    naming is a subset of this, so a name that fails is a sign the registry or
    the warehouse holds something unexpected, not that the rule is too strict.
    """
    if not part.isidentifier():
        raise TrinoError(f"refusing to interpolate {part!r}: not a bare identifier")
    return part


def qualified(table: str) -> str:
    """`catalog.schema.table`, each part checked."""
    parts = table.split(".")
    if len(parts) != 3:
        raise TrinoError(f"expected catalog.schema.table, got {table!r}")
    return ".".join(_identifier(part) for part in parts)


class Trino:
    def __init__(self, url: str | None = None, user: str | None = None, timeout: float | None = None) -> None:
        self.url = (url or config.trino_url()).rstrip("/")
        self.user = user or config.trino_user()
        self.timeout = timeout if timeout is not None else config.trino_timeout()

    def query(self, sql: str) -> list[list]:
        """Run one statement and drain every page.

        Trino answers a POST with a `nextUri` and returns rows across however
        many pages it likes, so a client that reads only the first response gets
        an empty result for a perfectly good query.
        """
        headers = {"X-Trino-User": self.user, "Content-Type": "text/plain"}
        rows: list[list] = []
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(f"{self.url}/v1/statement", content=sql.encode(), headers=headers)
                response.raise_for_status()
                payload = response.json()
                while True:
                    if payload.get("error"):
                        message = payload["error"].get("message", "statement failed")
                        raise TrinoError(f"{message}")
                    rows.extend(payload.get("data") or [])
                    following = payload.get("nextUri")
                    if not following:
                        return rows
                    time.sleep(0.05)
                    response = client.get(following, headers={"X-Trino-User": self.user})
                    response.raise_for_status()
                    payload = response.json()
            except httpx.HTTPError as exc:
                raise TrinoError(f"Trino at {self.url} did not answer: {exc}") from exc

    def columns(self, catalog: str, schema: str) -> dict[str, dict[str, str | None]]:
        """Every table in one schema, with each column's comment.

        The comment is dbt's `schema.yml` description, persisted by
        `persist_docs`. A column documented with an empty description arrives as
        `None`, indistinguishable from an undocumented one - which is why the
        dbt project requires a description on every column it lists.
        """
        rows = self.query(
            f"SELECT table_name, column_name, comment "
            f"FROM {_identifier(catalog)}.information_schema.columns "
            f"WHERE table_schema = '{_identifier(schema)}'"
        )
        tables: dict[str, dict[str, str | None]] = {}
        for table_name, column_name, comment in rows:
            tables.setdefault(table_name, {})[column_name] = comment
        return tables

    def distinct_counts(self, table: str) -> dict[str, int]:
        """Per-column distinct counts from Iceberg's own statistics.

        `SHOW STATS` reads the table metadata rather than scanning rows, so this
        costs nothing against a large table. The values are estimates Iceberg
        maintains at write time; they are used for "how many distinct values are
        there", never for a reported figure.
        """
        rows = self.query(f"SHOW STATS FOR {qualified(table)}")
        counts: dict[str, int] = {}
        for row in rows:
            # column_name, data_size, distinct_values_count, nulls_fraction, row_count, low, high
            name, distinct = row[0], row[2]
            if name is None or distinct is None:
                continue
            counts[name] = int(float(distinct))
        return counts

    def top_values(self, table: str, column: str, limit: int) -> list[str]:
        """The most frequent values of one column, most frequent first.

        Ordered by frequency because the point is near-miss matching: the value
        a person meant is far likelier to be a common one. The list is returned
        exactly as stored - a warehouse that normalises text (a `trim(upper())`
        in a transformation, say) is precisely the case where a filter typed by
        hand matches nothing, and an exact-case list is what fixes it.
        """
        rows = self.query(
            f"SELECT {_identifier(column)} FROM {qualified(table)} "
            f"WHERE {_identifier(column)} IS NOT NULL "
            f"GROUP BY {_identifier(column)} ORDER BY count(*) DESC LIMIT {int(limit)}"
        )
        return [str(row[0]) for row in rows if row and row[0] is not None]
