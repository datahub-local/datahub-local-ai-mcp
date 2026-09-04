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

A `query` tool that executes is not built. Nothing here reaches a warehouse.

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

A warehouse may store one column as `trim(upper(...))`, so a filter on
`"Leche Entera"` matches nothing. But a *derived* column on the same model can be
LLM-written mixed case (`"Whole milk"`), so **no single casing rule is correct
even within one query**. Matching is therefore case-folded, and a miss answers
`did you mean "LECHE ENTERA P6"?` — turning a silent empty result into a
corrected retry.

That needs real values, which is the one thing here that cannot be computed
offline. They live in the separate `dimension_samples.json`, and their absence
degrades `list_dimensions` to names-only rather than failing anything.

## Data

Three keys at `/etc/mcp/semantic/`, mounted as a ConfigMap. The registry is
**not** baked into the image: it was, while this server lived beside the dbt
project that defines it, which made a definition change an image build. Here the
image is generic and the definitions belong to whichever deployment owns them.

| Key                      | Absence                                          |
| ------------------------ | ------------------------------------------------ |
| `registry.yaml`          | fatal                                            |
| `manifest.json`          | fatal — table names resolve from it              |
| `dimension_samples.json` | degrades to names-only                           |

`SEMANTIC_REGISTRY_VERSION` is stamped at deploy time and travels on every
answer, which is what makes a number in a digest traceable to the definitions
that produced it.

Loading is cached, so a definition change is a ConfigMap change **plus a
restart**, not a live reload.

Run [`scripts/prune_manifest.py`](../../scripts/prune_manifest.py) over dbt's
manifest first: a full one is ~671 KB against a 1 MiB ConfigMap limit, and only
six fields per model are read. Pruned it is ~3.4 KB and produces byte-identical
SQL — verified. The server works against either, so pruning is never a
dependency.

Example: [`deploy/examples/configmap-semantic.yaml`](../../deploy/examples/configmap-semantic.yaml).
