"""semantic - metric definitions as the agent's tool surface, instead of SQL.

Raw SQL is the wrong contract for an analytical question. Asked "how much did I
spend on dairy last month", a model composing SQL has to reconstruct which mart
is authoritative, which filters the metric implies, and which of five columns
named total_amount is the right one at which grain - and nothing in the answer
distinguishes a correct number from a plausible one.

So the agent gets metric and dimension *names* and never a formula, a filter
condition or a join path. Same move `homelab_facts` made for Prometheus:
`used_percent()` cannot be the bare ratio because the code owns the expression.

There is deliberately **no run_sql** - not gated, not approval-wrapped, absent.
Structural questions ("what tables exist", "how many rows") keep going to the
existing `trino_*` tools, where they are already correct. See ../../README.md.
"""

from __future__ import annotations

from mcp_runner.server import Registry

from .tools import discovery, execute


def register(registry: Registry) -> None:
    registry.add(
        "list_metrics",
        discovery.LIST_DESCRIPTION,
        discovery.list_metrics,
        schema=discovery.LIST_SCHEMA,
        budget=discovery.LIST_BUDGET,
    )
    registry.add(
        "describe_metric",
        discovery.DESCRIBE_DESCRIPTION,
        discovery.describe_metric,
        schema=discovery.DESCRIBE_SCHEMA,
        budget=discovery.DESCRIBE_BUDGET,
    )
    registry.add(
        "list_dimensions",
        discovery.DIMENSIONS_DESCRIPTION,
        discovery.list_dimensions,
        schema=discovery.DIMENSIONS_SCHEMA,
        budget=discovery.DIMENSIONS_BUDGET,
    )
    registry.add(
        "explain",
        discovery.EXPLAIN_DESCRIPTION,
        discovery.explain,
        schema=discovery.QUERY_SCHEMA,
        budget=discovery.EXPLAIN_BUDGET,
    )
    registry.add(
        "query",
        execute.QUERY_DESCRIPTION,
        execute.query,
        schema=discovery.QUERY_SCHEMA,
        budget=execute.QUERY_BUDGET,
    )
