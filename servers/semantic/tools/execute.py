"""`query` - the one tool that reaches the warehouse.

It shares every step with `explain` up to the point of sending, so a query that
explains is a query that runs. What it adds is the part that cannot be checked
offline: the rows, and the three things that must travel with them.

**`excludes` rides with the number, always.** A returned figure detached from
what it leaves out is the failure this whole layer exists to prevent - the
answer reads as "spend last month" when it is "card spend on Mercadona
e-receipts last month". It is printed per metric rather than summarised, because
two metrics in one query routinely exclude different things.

**A partial bucket is stated as a value, never applied as a rule.** The compiler
already knows which bucket is still filling; printing its label is something the
model transcribes, where "drop the last row if incomplete" is something it has
to decide, and a small model decides it inconsistently.

**An empty result is an answer, not a failure.** It means the filters matched no
rows, and saying so plainly is what stops a retry loop that widens the query
until something comes back.
"""

from __future__ import annotations

import logging

from mcp_runner.budget import truncate_lines
from mcp_runner.config import scaled_budget, tool_budget
from mcp_runner.trino import Trino, TrinoError

from .. import settings
from ..compiler import compile_query, inline_sql
from ..query import QueryError, parse, validate
from .discovery import model_of

logger = logging.getLogger(__name__)

QUERY_BUDGET = tool_budget("query")

# Floor for the row section, so a query naming five metrics with long exclusions
# still returns numbers rather than only its own caveats.
_MIN_ROW_BUDGET = scaled_budget(1024)

QUERY_DESCRIPTION = """
Run a metric query and return the numbers. Name metrics from `list_metrics` and
dimensions from `list_dimensions`; filter values must match stored values
exactly. The result carries each metric's exclusions - report them with the
number, never the number alone. Use `explain` first if unsure of a query.
"""


def query(**payload) -> str:
    registry = settings.registry()
    try:
        parsed = parse(payload)
        validate(parsed, registry, settings.sample_values(model_of(payload, registry)))
        compiled = compile_query(parsed, registry)
    except QueryError as exc:
        return f"INVALID: {exc}"
    except KeyError as exc:
        return f"INVALID: {exc}"

    try:
        rows = Trino().execute(compiled.sql, compiled.params)
    except TrinoError as exc:
        # Logged with the statement, which the reply deliberately omits: the
        # model gets no SQL from this tool, so it cannot start editing one.
        logger.warning("semantic query failed: %s | %s", exc, inline_sql(compiled))
        return f"WAREHOUSE ERROR: {exc}. The query was valid; the warehouse did not answer."

    start, end = compiled.window
    header = [
        f"registry {registry.version}",
        f"window: {start.isoformat()} to {end.isoformat()} (end exclusive), by {compiled.grain}",
    ]
    if compiled.partial_bucket:
        header.append(
            f"PARTIAL PERIOD: {compiled.partial_bucket} is still filling - "
            f"its value is lower than the finished periods for that reason alone."
        )
    header.append("")

    if not rows:
        empty = (
            "0 rows: no data matched. The metric and dimensions were valid, so this is "
            "an empty result rather than an error - widen the time range or check a "
            "filter value against list_dimensions."
        )
        return "\n".join([*header, empty])

    excludes = ["", "EXCLUDES - report these with the numbers:"]
    for name in parsed.metrics:
        metric = registry.metric(name)
        excludes.append(f"  {name}: {' '.join(metric.excludes.split())}")

    # Rows are truncated against what is left after the exclusions, because
    # `truncate_lines` drops from the tail: budgeting the whole reply at once
    # would drop the caveats first and leave the bare numbers, which is the one
    # outcome this tool exists to prevent.
    body = [" | ".join(compiled.projection), *(_row(row) for row in rows)]
    reserved = sum(len(line.encode()) + 1 for line in header + excludes)
    kept = truncate_lines(body, max(QUERY_BUDGET - reserved, _MIN_ROW_BUDGET), unit="rows")

    return "\n".join(header + [kept] + excludes)


def _row(row: list) -> str:
    return " | ".join("null" if value is None else str(value) for value in row)
