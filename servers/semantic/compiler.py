"""`SemanticQuery` -> SQL. The compiler owns every expression.

The same principle as `mcp_runner/prometheus.py`: a wrong query is not
expressible because the code assembles it. Two rules carry most of the weight.

**A ratio is `sum(num) / nullif(sum(den), 0)` at the group grain, never an
average of per-row ratios.** Measured on the live data: `avg(unit_price)` gives
2.5099 where the metric gives 2.3850, 5.2% apart. `top_products.sql` computes
the first and `price_trends.sql` the second; only the second is the metric.

**Every value from a request is a bound parameter; every identifier from a
request is allowlisted.** `order_by` is checked against the projection rather
than interpolated - the spec's own draft interpolated it one line below a rule
forbidding exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .query import SemanticQuery, resolve_window
from .registry import Metric, Registry, SemanticModel

# Trino's date_trunc units, by our grain. Named here rather than passed through
# from the request so an arbitrary unit cannot reach SQL.
_TRUNC = {
    "day": "day",
    "week": "week",
    "month": "month",
    "quarter": "quarter",
    "year": "year",
}

_OP_SQL = {"=": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}


@dataclass(frozen=True)
class Compiled:
    sql: str
    params: tuple[object, ...]
    projection: tuple[str, ...]
    table: str
    window: tuple[date, date]
    grain: str
    partial_bucket: str | None

    @property
    def is_partial_period(self) -> bool:
        return self.partial_bucket is not None


def compile_query(
    query: SemanticQuery, registry: Registry, today: date | None = None
) -> Compiled:
    """Compile a validated query. Call `query.validate` first."""
    today = today or datetime.now(UTC).date()
    metrics = [registry.metric(name) for name in query.metrics]
    model = registry.model_for(metrics[0])

    if not model.table:
        raise ValueError(
            f"semantic_model {model.name!r} has no table bound; the registry was loaded "
            f"without a dbt manifest"
        )

    time_expr = model.dimensions[model.time_dimension].expr
    bucket = f"date_trunc('{_TRUNC[query.time_range.grain]}', {time_expr})"

    select = [f"{bucket} AS period"]
    for name in query.group_by:
        dimension = model.dimensions[name]
        select.append(f"{dimension.expr} AS {_ident(dimension.name)}")
    for metric in metrics:
        select.append(f"{_render_metric(metric, registry)} AS {_ident(metric.name)}")

    where, params = _render_filters(query, model)
    start, end = resolve_window(query.time_range, today)
    where += [f"{time_expr} >= ?", f"{time_expr} < ?"]
    params += [start, end]

    # Positional, so no request-supplied name reaches the ORDER BY clause even
    # if the allowlist in `query.validate` were bypassed.
    group_positions = list(range(1, len(query.group_by) + 2))
    projection = ("period", *query.group_by, *(metric.name for metric in metrics))
    order_position = projection.index(query.order_by) + 1 if query.order_by in projection else 1
    direction = "DESC" if query.order_by and query.order_by != "period" else "ASC"

    sql = (
        f"SELECT {', '.join(select)}"
        f" FROM {model.table}"
        f" WHERE {' AND '.join(where)}"
        f" GROUP BY {', '.join(str(position) for position in group_positions)}"
        f" ORDER BY {order_position} {direction}"
        f" LIMIT {int(query.limit)}"
    )

    return Compiled(
        sql=sql,
        params=tuple(params),
        projection=projection,
        table=model.table,
        window=(start, end),
        grain=query.time_range.grain,
        partial_bucket=_partial_bucket(query.time_range.grain, end, today),
    )


def _render_metric(metric: Metric, registry: Registry) -> str:
    if metric.type == "ratio":
        numerator = registry.measure(metric, metric.numerator or "")
        denominator = registry.measure(metric, metric.denominator or "")
        # nullif, never a bare divide: one zero-quantity row would otherwise
        # fail the whole query rather than the one group.
        return _rounded(f"{numerator.render()} / nullif({denominator.render()}, 0)", 4)
    measure = registry.measure(metric, metric.measure or "")
    if measure.agg in ("count", "count_distinct"):
        return measure.render()
    return _rounded(measure.render(), 2)


def _rounded(expr: str, places: int) -> str:
    """Round in SQL, because binary floats reach Slack verbatim.

    `sum` over a DOUBLE column returns 465.0999999999997 for what is 465.10 on
    the receipts, and the prompt requires transcribing a returned value rather
    than reasoning about it - so the value has to arrive already correct. Counts
    are left alone: rounding an integer only invites a decimal point.
    """
    return f"round({expr}, {places})"


def _render_filters(query: SemanticQuery, model: SemanticModel) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []

    for item in query.filters:
        expr = model.dimensions[item.dimension].expr
        if item.op in ("in", "not_in"):
            placeholders = ", ".join("?" for _ in item.value)
            keyword = "IN" if item.op == "in" else "NOT IN"
            clauses.append(f"{expr} {keyword} ({placeholders})")
            params.extend(item.value)
        elif item.op == "contains":
            # The pattern is built here and the value stays bound, so a value
            # carrying % or _ cannot widen its own match.
            clauses.append(f"{expr} LIKE ?")
            params.append(f"%{item.value}%")
        else:
            clauses.append(f"{expr} {_OP_SQL[item.op]} ?")
            params.append(item.value)

    return clauses, params


def _partial_bucket(grain: str, end: date, today: date) -> str | None:
    """The label of the window's last bucket when it is still filling.

    Returned as a value rather than applied as a rule: the prompt requires
    printing it, because a rule the model must apply is weaker than a value it
    must transcribe.
    """
    last_day = end - timedelta(days=1)
    if last_day < today:
        return None
    start = _bucket_start(grain, last_day)
    if grain == "day":
        return None
    return _label(grain, start)


def _bucket_start(grain: str, day: date) -> date:
    if grain == "week":
        return day - timedelta(days=day.weekday())
    if grain == "month":
        return day.replace(day=1)
    if grain == "quarter":
        return day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)
    if grain == "year":
        return day.replace(month=1, day=1)
    return day


def _label(grain: str, start: date) -> str:
    if grain == "week":
        return f"week of {start.isoformat()}"
    if grain == "month":
        return start.strftime("%Y-%m")
    if grain == "quarter":
        return f"{start.year}-Q{(start.month - 1) // 3 + 1}"
    if grain == "year":
        return str(start.year)
    return start.isoformat()


def _ident(name: str) -> str:
    """Quote a registry-sourced identifier. Never called on a request value."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"refusing to quote unexpected identifier {name!r}")
    return f'"{name}"'


def inline_sql(compiled: Compiled) -> str:
    """The compiled SQL with parameters substituted, for `explain` and for logs.

    Display only - never executed. Execution always sends the parameterised
    form, so this rendering cannot become an injection path.
    """
    parts = compiled.sql.split("?")
    out = [parts[0]]
    for value, tail in zip(compiled.params, parts[1:]):
        out.append(_literal(value))
        out.append(tail)
    return "".join(out)


def _literal(value: object) -> str:
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"
