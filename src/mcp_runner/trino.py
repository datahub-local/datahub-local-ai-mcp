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
from collections.abc import Sequence
from datetime import date
from urllib.parse import quote

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


def _param_literal(value: object) -> str:
    """One value in an `EXECUTE ... USING` list.

    Trino parses these against a statement it has already planned, so a value
    lands in the parameter slot it was bound to and cannot alter the query's
    shape. Only the types the compiler produces are accepted - an unexpected
    type is a compiler bug, and rendering it via `str()` is how a value would
    quietly become something else.
    """
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise TrinoError(f"refusing to bind {type(value).__name__} parameter {value!r}")


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
        """Run one statement and drain every page."""
        return self._run(sql, {"X-Trino-User": self.user, "Content-Type": "text/plain"})

    def execute(self, sql: str, params: Sequence[object]) -> list[list]:
        """Run a `?`-placeholder statement with its values genuinely bound.

        Trino's HTTP protocol has no inline bind parameters, so the statement
        travels as an `X-Trino-Prepared-Statement` header and the body becomes
        `EXECUTE <name> USING <literals>`. The values are still parsed as a
        parameter list against an already-planned statement, so a value cannot
        become SQL structure the way an interpolated one can.

        This is the only path that executes compiler output. The compiler's
        `inline_sql` renders the same statement for display and must never be
        sent here - that is what keeps one rendering out of the injection path.
        """
        if not params:
            return self.query(sql)

        name = "semantic_query"
        headers = {
            "X-Trino-User": self.user,
            "Content-Type": "text/plain",
            # Percent-encoded: a prepared statement travels in an HTTP header,
            # and a raw newline or non-ASCII byte in one is a protocol error
            # rather than a query error.
            "X-Trino-Prepared-Statement": f"{name}={quote(sql, safe='')}",
        }
        using = ", ".join(_param_literal(value) for value in params)
        return self._run(f"EXECUTE {name} USING {using}", headers)

    def _run(self, body: str, headers: dict[str, str]) -> list[list]:
        """POST one statement and follow `nextUri` to the end.

        Trino returns rows across however many pages it likes, so a client that
        reads only the first response gets an empty result for a good query.
        """
        rows: list[list] = []
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(
                    f"{self.url}/v1/statement", content=body.encode(), headers=headers
                )
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
