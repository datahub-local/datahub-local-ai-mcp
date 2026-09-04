# Adding a server

A server is a directory under `servers/` exposing `register(registry)`. Nothing
discovers it by a path walk or a registration table: `servers/` is a packaging
root, so the directory name **is** the import name and `--server <name>` is
`importlib.import_module`. That is why the steps below touch no shared file.

## 1. Create the package

```
servers/<name>/
  __init__.py        register(registry) - the only required symbol
  settings.py        config loading and cached client handles
  tools/
    __init__.py
    <section>.py     one module per group of tools
```

Use `snake_case` for `<name>` — it is a Python package. It publishes as
`mcp-<name-with-hyphens>`.

`__init__.py` registers every tool and nothing else:

```python
"""What this server answers, and for whom."""

from __future__ import annotations

from mcp_runner.server import Registry

from .tools import inventory


def register(registry: Registry) -> None:
    registry.add(
        "list_widgets",
        inventory.list_widgets.__doc__,
        inventory.list_widgets,
        budget=2_000,
    )
```

## 2. Decide the tool surface before writing a tool

The design rule, and the reason this repository exists: **code gathers, the model
writes.** A tool that returns one report section's worth of already-correct
readings replaces eight calls of exact arguments that a small model gets wrong.

- **Few calls, never big answers.** Declare a `budget` per tool. A single ~16 KB
  result reproducibly ends an agent run with `terminal turn had empty text` —
  not context overflow, so no larger window fixes it. `Registry.call` clamps
  whatever you return, but pick the budget deliberately: keep a single answer
  under 4 KB.
- **Take free text, return exact names.** Any string should be a valid argument.
  A tool that makes the caller assemble a namespace, a label selector or a
  container name is a tool that will be called wrong.
- **Make absence expressible, with a definition.** Distinguish *the query
  returned nothing* from *this thing has no such attribute*. A format that
  cannot say "unavailable" gets filled with invented numbers.
- **State the threshold you applied**, in the output, every time.
- **Own the expression.** If a query can be written two ways and one is wrong,
  the code writes it — never the prompt. A percentage-used that could be
  computed as percentage-free is exactly this case.

## 3. Config, if it needs any

Cluster-specific data is **mounted, never shipped**. Read it from
`mcp_runner.config.config_dir()`, defaulting to `/etc/mcp/<name>/`:

```python
DEFAULT_CONFIG_DIR = Path("/etc/mcp/<name>")


def config_dir() -> Path:
    override = runner_config.config_dir()
    return Path(override) if override else DEFAULT_CONFIG_DIR
```

**Never default a mount to a path under `/app`.** `/app` is WORKDIR and lands on
`sys.path`, so `/app/<name>/` shadows the `<name>` package itself and the server
dies at startup with `module '<name>' has no attribute 'register'`. The same
collision breaks pytest collection.

Then decide, per file, what absence means, and make it explicit:

- **fatal** if answering without it would produce a confidently wrong answer
  (semantic's manifest: table names come from it);
- **degraded** if a section can honestly report itself unavailable
  (`dimension_samples.json`);
- and log a WARNING naming the path either way. A silent fallback to defaults
  reads as working.

A new backend URL gets **no default** — use `config.require_url()`, which raises
a named `ConfigError` at first use. A guessed address makes every reading
`unavailable`, which renders a wrong endpoint as an absent one.

## 4. Tests

`tests/<name>/`, needing no cluster. Fake the backends
(`tests/conftest.py` has `FakePrometheus`) and build any config in code — an
autouse fixture pointing `MCP_CONFIG_DIR` at a `tmp_path`, as
`tests/homelab_facts/conftest.py` does. Tests that read a real mount assert
against one deployment's data and break the moment it changes.

Assert on the **expression** a tool sends, not only its answer. The failures
worth guarding against were wrong queries that returned perfectly valid numbers.

## 5. Add it to CI

Two edits, both in `.github/workflows/test.yaml`:

- the `expect_tools` list, with the count your server exposes;
- the `build` matrix.

`publish.yaml` needs **no** edit — it discovers `servers/*/__init__.py`.

## 6. Verify

```bash
uv run -- ruff check .
uv run -- pytest -q
uv run -- python -m mcp_runner --server <name> --list-tools

docker build --build-arg SERVER=<name> -t mcp-<name>:test .
docker run -d --name probe -p 18080:8080 \
  -v /path/to/data:/etc/mcp/<name>:ro mcp-<name>:test
curl -s http://127.0.0.1:18080/healthz
curl -s -X POST http://127.0.0.1:18080/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Then run it **without** the mount and confirm the behaviour you documented in
step 3 actually happens. Reading the code is not enough — the `/app` shadowing
failure only appears at runtime.

Finally: diff a tool's output against the same query run by hand before wiring an
agent to it.

## 7. Ship an example ConfigMap

`deploy/examples/configmap-<name>.yaml`, showing every expected key with
structure only — **never a real deployment's data**. Verify it loads; an example
that does not work is worse than none.
