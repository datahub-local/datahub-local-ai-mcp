# semantic

Metric definitions, and the SQL that computes them. Four tools, no execution.

Raw SQL is the wrong contract for an *analytical* question and the right one for
a structural question — which is why both paths exist and neither replaces the
other. `SHOW STATS FOR catalog.schema.table` has one right answer the database
itself knows. "How much did I spend on dairy last month" does not: a model
composing SQL has to reconstruct which mart is authoritative, which filters the
metric implies, and which of the columns named `total_amount` is the right one at
which grain. Nothing in the answer distinguishes a correct number from a
plausible one.

So the agent gets metric and dimension **names**, and never a formula, a filter
condition or a join path. Exactly the move `prometheus.py` made: `used_percent()`
cannot be the bare ratio, and here a ratio metric cannot compile to an average of
per-row ratios.

There is deliberately **no `run_sql`** — not gated, not approval-wrapped, absent.
Structural questions keep going to whatever SQL tools the caller already has.

| Tool              | Input            | Returns                                          |
| ----------------- | ---------------- | ------------------------------------------------ |
| `list_metrics`    | `search?`        | every metric with its `excludes` and dimensions   |
| `describe_metric` | `name`           | one metric in full, naming its source table       |
| `list_dimensions` | `metric`         | dimensions with sampled values                    |
| `explain`         | a `SemanticQuery`| the compiled SQL and resolved window, **not executed** |
| `query`           | a `SemanticQuery`| the rows, each metric's `excludes`, and any partial period |

`query` is the only tool that reaches the warehouse, and it accepts the same
argument as `explain` — so a query that explains is a query that runs. Its
values travel as bound parameters (`PREPARE`/`EXECUTE ... USING`); the SQL that
`explain` prints is a rendering for display and is never the thing sent.

Its reply budgets rows against what is left after the exclusions, because
truncation drops from the tail: budgeting the whole reply at once would cut the
caveats first and return bare numbers, which is the one outcome this layer
exists to prevent.

## What the code owns, and therefore cannot get wrong

- **A ratio is `sum(num) / nullif(sum(den), 0)`, never an average of per-row
  ratios.** On the warehouse this was measured against, `avg(unit_price)` gave
  2.5099 where the metric gives 2.3850 — 5.2% apart. The `nullif` is what makes
  an empty period NULL instead of a divide-by-zero.
- **Every request value is a bound parameter; every request identifier is
  allowlisted.** `order_by` compiles to a *position*, so no caller-supplied name
  reaches the `ORDER BY` clause at all.
- **`excludes` is mandatory**, and it is what makes an answer auditable. Writing
  it also forces the definitional thinking: a metric whose rows mix units (KG
  lines against EA lines) has no fixed unit, so it must declare a `grain_min`
  and say so.
- **Absence is expressible.** An unknown metric, an undeclared dimension or an
  unknown filter value returns readable text naming the closest match, never an
  empty result that reads as zero.
- **Numbers are rounded in SQL.** `sum` over a DOUBLE returns
  `465.0999999999997` for what a receipt says is `465.10`, and a prompt that
  requires transcribing a value will transcribe that one.

## Near-miss suggestions, and why no casing rule works

A warehouse may store one column normalised by a transformation
(`trim(upper(...))`, say), so a filter typed in natural case matches nothing.
But a *derived* column on the same model can hold whatever case it was written
in, so **no single casing rule is correct even within one query**. Matching is
therefore case-folded, and a miss answers `did you mean "<the stored value>"?`
— turning a silent empty result into a corrected retry.

That needs real values, which is why they are read from the warehouse rather
than shipped: it is authoritative about itself and a copy goes stale as soon as
the pipeline runs. `warehouse.py` caches them with a TTL and serves the last
good value when the warehouse is unreachable, so an outage costs freshness
rather than the tool.

## Data

**One** key at `/etc/mcp/semantic/`, mounted as a ConfigMap:

| Key             | Absence                                            |
| --------------- | -------------------------------------------------- |
| `registry.yaml` | fatal — no metric definitions without it           |

The registry is **not** baked into the image: it was, while this server lived
beside the dbt project that defines it, which made a definition change an image
build. Here the image is generic and the definitions belong to whichever
deployment owns them.

Everything else is read from the warehouse, because the warehouse is
authoritative about itself:

| What                                     | Source                                    |
| ---------------------------------------- | ----------------------------------------- |
| table name for each `ref()`              | `information_schema.columns`              |
| which columns are *documented*           | the same query's `comment`                |
| cardinality per dimension                | `SHOW STATS` (table metadata, no scan)    |
| dimension values, for near-miss matching | one grouped query per dimension           |

A pruned dbt manifest and a precomputed sample file used to be mounted
alongside. Both described the warehouse, so both were a copy that went stale
when the pipeline ran; they and the prune script are gone.

Two requirements come with that. `SEMANTIC_WAREHOUSE_SCOPES` names the
`catalog.schema` pairs to read and has **no default** — a registry names models
by `ref()`, which carries no catalog, so a guess resolves nothing and reports as
a broken registry rather than a missing setting. And the dbt project must
persist its column descriptions into the warehouse (`persist_docs` for
dbt-trino): the documentation gate reads those comments, and a column listed in
`schema.yml` with a *blank* description persists as a NULL comment, which is
indistinguishable from undocumented. Every column the registry references needs
a real description, or the gate silently weakens to "the column exists".

`SEMANTIC_REGISTRY_VERSION` is stamped at deploy time and travels on every
answer, which is what makes a number in a digest traceable to the definitions
that produced it.

The registry is loaded and validated once, so a definition change is a
ConfigMap change **plus a restart**, not a live reload. Warehouse reads are
cached with `SEMANTIC_CACHE_TTL_SECONDS` (default 3600) and a failed refresh
serves the last good value, so an outage costs freshness rather than the tool.
Only a cold cache can fail, and it fails loudly.
