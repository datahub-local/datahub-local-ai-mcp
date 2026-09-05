# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A platform for MCP servers. One shared runner (`src/mcp_runner/`), one directory
per server (`servers/<name>/`), one image per server, and **no deployment's data
in the repository**.

Read [`README.md`](README.md) for the design rationale — it is the reasoning, not
a feature list, and most mistakes here are re-litigating something it records.
[`docs/adding-a-server.md`](docs/adding-a-server.md) is the procedure.

## The rule everything follows

**Code gathers; the model writes.** This exists because every failure in the
agent fleet it was built for was a tool-loop failure, not a writing failure. A
small model asked to assemble a PromQL join, remember a counter window, or chain
four exact calls in order gets it wrong, and the fix is never another paragraph
in a prompt — it is code that makes the wrong version inexpressible.

So when adding or changing a tool:

- If a query can be written two ways and one is wrong, **the code writes it**.
- If a caller must assemble a namespace, selector or container name, **take free
  text instead** and resolve it in code.
- If a fact can be derived at query time, **derive it** — do not write it down.
  A checked-in copy of a cluster's shape goes stale silently.

## Conventions

| What                  | Convention                        | Example                            |
| --------------------- | --------------------------------- | ---------------------------------- |
| Server directory      | `snake_case`, importable package  | `servers/homelab_facts/`           |
| Server entry point    | `register(registry)` in `__init__.py` | discovered by `--server <name>` |
| Image name            | `mcp-<name-with-hyphens>`         | `mcp-homelab-facts`                |
| Tool module           | `servers/<name>/tools/<section>.py` | `tools/alerts.py`                |
| Tests                 | `tests/<name>/`, no cluster       | `tests/semantic/`                  |
| Mount point           | `/etc/mcp/<name>/`                | never under `/app`                 |

Shared code goes in `src/mcp_runner/` only when a second server needs it.
Server-specific logic stays in the server.

## Hard-won specifics — do not undo these

- **Never default a data mount under `/app`.** WORKDIR is `/app` and lands on
  `sys.path`, so `/app/semantic/` shadows the `semantic` package and the server
  dies with `module 'semantic' has no attribute 'register'`. It also breaks
  pytest collection. Verify by running the container, not by reading.
- **Backend URLs have no default.** `config.require_url()` raises a named error
  at first use. A guessed address makes every reading `unavailable`, which
  renders a wrong endpoint as an absent one. Raising at *first use* rather than
  at import is deliberate — the server must still start and still answer
  `/healthz`.
- **Garage is the one backend whose absence is designed.** `garage_admin_url()`
  returns `None` rather than raising, because the client is constructed by a
  cached accessor before any tool knows whether a token exists. Raising there
  turns a degraded bucket section into a dead tool.
- **Absence needs a definition, not a blank.** `unavailable` (the query gave no
  value) and `n/a` (no such sensor here) are different, and one word once
  absorbed a bug. A format that cannot express absence gets filled with invented
  numbers.
- **Every tool declares a byte budget.** A single ~16 KB result reproducibly
  ends an agent run with `terminal turn had empty text`. It is not context
  overflow, so a larger window does not fix it. Keep one answer under 4 KB.
- **Every tool states the threshold it applied.**
- **A counter is not a state, and the suffix does not tell you which.**
  `cnpg_backends_total` is a *gauge*; `cnpg_pg_stat_archiver_failed_count` is a
  *counter*. Read the type from Prometheus's metadata API — a suffix rule fails
  this cluster in both directions, and `tests/test_counters.py` asserts it.
- **Tests must not read a real mount.** Build config in code with an autouse
  fixture pointing `MCP_CONFIG_DIR` at `tmp_path`, as
  `tests/homelab_facts/conftest.py` does. Three tests once passed only because
  one cluster's `chronic_alerts.yaml` happened to contain the alert they
  asserted on.
- **Assert on the expression, not only the answer.** The bugs worth guarding
  against were wrong queries returning perfectly valid numbers. This is also
  where ~600 lines of prompt-policing regexes went, and they must not come back:
  a regex asserting a prompt contains the right English passes on the prompt
  that just failed.

## No data, ever

This repository contains **no** real registry, no real metric definitions, no
node names and no cluster topology. A semantic registry names one warehouse's
models and business definitions and cannot be validated without the dbt project
that defines it — it belongs to the repository that owns that project, which
generates the ConfigMap.

Examples under `deploy/examples/` show **structure only**, and must load. One
that does not work is worse than none.

## Verifying

```bash
uv sync --extra dev
uv run -- ruff check .
uv run -- pytest -q
uv run -- python -m mcp_runner --server homelab_facts --list-tools
uv run -- python -m mcp_runner --server semantic --list-tools
```

Changing a Dockerfile, a mount path or a degradation path means **running the
container** — build it, mount data, hit `/healthz` and a `tools/list` POST, then
run it again *without* the mount and confirm the documented behaviour. The
shadowing failure and the mount-absence paths only appear at runtime.

## Style

Match the surrounding code. Comments are sparse and explain **why** — a
non-obvious constraint, an invariant, or the incident that motivated the line.
Several existing comments record a specific failure; that is the bar. Do not add
comments restating what the code says, and do not add compatibility shims,
deprecated aliases or version guards.
