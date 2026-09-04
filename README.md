# datahub-local-ai-mcp

A platform for MCP servers: one shared runner, one directory per server, one
image per server, and **no deployment's data in the repository**.

Two servers ship today:

| Server          | Tools | What it answers                                            | Data it needs mounted               |
| --------------- | ----- | ---------------------------------------------------------- | ----------------------------------- |
| `homelab_facts` | 16    | Cluster state: alerts, nodes, volumes, stores, certs, logs | chronic alerts + thresholds         |
| `semantic`      | 4     | Metric definitions, and the SQL that computes them         | registry + dbt manifest (+ samples) |

## Why this exists

**Every failure in the agent fleet this was built for was a tool-loop failure,
not a writing failure.** A 4B model was being asked to be a careful API client:
assemble `100 * (1 - avail/cap)` with a `group_left` join, remember that
`increase(m[1h])` is not `m[1h]`, pass `endTime` as the literal word `now`, diff
alerts against its own memory, know that one node's kernel is not drift against
another's. Every incident added a paragraph to a prompt and a regex to a
validator, and it did not converge: prompts reached 6-12 KB and roughly 600 lines
of that validator were regexes policing English.

Both are gone. **Code gathers; the model writes.** Prompts are 4-6 KB, the checks
that survived are `tests/` assertions about the expression the server actually
sends, and the validator was deleted — because the method left the prompt and
moved into code.

The corollary is the design rule for a new server here: a tool should return one
report section's worth of already-correct readings, so the mandatory part of a
run costs one or two calls instead of eight, and the iteration budget is left for
real investigation.

## The four properties that remove failure classes structurally

**1. A wrong query is not expressible.** The expression builders in
`src/mcp_runner/prometheus.py` are the only way to ask. `used_percent()` cannot
be written as the bare ratio; `increase_()` cannot lose its wrapper;
`by_nodename()` cannot drop the join. Each was a real report: the bare ratio
called a 2%-used volume "97.9% full, write operations failing" every run for
days, and a missing join produced a node table wrong in five ways at once.

**2. Absence is a value, and it has a definition.** `unavailable` means *the
query gave no value for this node*; `n/a` means *this node has no such sensor*.
Two words, because one word absorbed a bug: `unavailable` was introduced so a
missing metric could be stated rather than invented, then silently absorbed a
broken join. A mandatory report format that cannot express absence gets filled
with invented numbers — that is how a memory reading came to be relabelled as
disk and a 5%-full disk reported as "79% (CRITICAL)".

**3. Every answer is bounded, in code.** "Fat tool" means *few calls*, never *big
answers*. A single ~16 KB tool result reproducibly ends a run with no report at
all: four calls for 24,126 result bytes produced `terminal turn had empty text`,
where five calls for 8,483 bytes the same day wrote a normal report. It is not
context overflow — cumulative input was 25,423 tokens against a 65,536 window. So
each tool declares a byte budget, truncates by whole lines, and *says* it
truncated. A full eleven-reading sweep is about 17.8 KB, and no single answer
exceeds 4 KB. `Registry.call` clamps whatever a tool returns, so a new tool
cannot forget its budget.

**4. Trends are measured, not remembered.** The server holds snapshots, so "new
since last run" is a computation. A lost snapshot degrades to "first
observation", which every tool states — it can never produce a *wrong* diff.

**A denominator is a reading too.** Three quantities here were the wrong one
before code owned them, and none is fixable by naming a metric in a prompt.
Garage's headroom is the capacity its *layout* assigns a node, not the
filesystem under it — quoting the disk overstates the store by two orders of
magnitude and shows a full store as 1% used. Prometheus holding less history
than configured is only loss once it has been up longer than it is holding, so
the verdict is computed against uptime and a restart reads as *filling* instead
of firing CRITICAL every run for a month. And Garage's three nodes describe one
shared filesystem, derived from identical capacity plus free space agreeing to
within a scrape's drift — exact byte equality called one share three separate
disks the first time it ran live.

## Nothing about a cluster is written down here

Node names, hardware classes and per-machine sensor coverage are derived at
query time (`src/mcp_runner/fleet.py`). An earlier draft carried a
`hardware_classes.yaml` naming all seven machines; that is a second copy of the
cluster, and a stale node list produces the exact failure this exists to
prevent — a row of `unavailable` for a machine whose figures were available.

- **A hardware class is the kernel flavour plus the architecture**, which is not
  an approximation of the comparability rule but *is* the rule: kernels compare
  only within one tree. A numeric difference inside a flavour is real drift; a
  different flavour is different silicon and can never converge. Adding,
  renaming or re-imaging a node needs no change here.
- **Sensor coverage is whether the sensor answered**, decided per node by a
  capability probe. Moving a disk or adding a UPS needs no change either.
- **The node inventory is the Kubernetes node list**, and the gap between it and
  what Prometheus answered is what makes a dropped join legible.

Only two kinds of file are genuinely config, and both are judgements no cluster
can answer: whether an alert is noise, and where to draw a line. Both are
**mounted, not shipped** — see [Data is mounted](#data-is-mounted). Every tool
states the threshold it applied, so a report never implies a judgement the
reader cannot check.

## Two properties worth keeping

**The server holds no credential unless one is deliberately given.** Prometheus
and Loki are queried directly rather than through Grafana, so no datasource uid
exists on either path — the value a 4B model once resolved to Loki's hex uid,
404ing every PromQL query against a prompt that stated the right one. Kubernetes
goes through the pod ServiceAccount; ArgoCD state comes from `Application` CRs
rather than the ArgoCD API; Postgres state from the CloudNativePG operator's
metrics rather than a DSN.

The single exception is opt-in and named: **per-bucket S3 usage has no
unauthenticated source.** Garage publishes no bucket label and no stored-bytes
gauge to Prometheus. With no token the bucket section reports itself
`unavailable` and nothing else changes. Where a token *is* supplied the boundary
is in the code rather than in the credential: `garage.py` exposes two `GET`s by
name with no generic request method, so a write endpoint is not expressible
whatever the token permits, and it strips each bucket's `keys` because that field
carries access key ids into a report. Scope the token to
`ListBuckets,GetBucketInfo` so the code rules are a second line, not the only
one.

**It cannot return a Secret's contents.** `kube.py` exposes `list` plus one
bounded `pod_log`, and strips `data`/`stringData` at the boundary.
`cert_expiry()` narrows with a field selector rather than filtering afterwards —
an unfiltered cluster-wide Secret list transfers every value in every namespace,
25 MB on the origin cluster, and broke the connection outright.

The log tail is the one read that can echo a value an application printed itself,
and no boundary here can prevent that — it is a property of logs, not of this
server.

## Data is mounted

**No deployment's data is in this repository, and that is the point of it.** The
code is generic and lives in the image; everything that describes one cluster
arrives as a ConfigMap. That is what makes the same image runnable against a
different cluster.

| Server          | Mount point               | Keys                                                       |
| --------------- | ------------------------- | ---------------------------------------------------------- |
| `homelab_facts` | `/etc/mcp/homelab_facts/` | `chronic_alerts.yaml`, `thresholds.yaml`                   |
| `semantic`      | `/etc/mcp/semantic/`      | `registry.yaml`, `manifest.json`, `dimension_samples.json` |

Working examples, verified to load: [`deploy/examples/`](deploy/examples/).

**The mount point is outside the code tree, and that is load-bearing.** `/app` is
WORKDIR and lands on `sys.path` for `python -m`, so a mount at `/app/semantic/`
shadows the `semantic` package and the server dies at startup with `module
'semantic' has no attribute 'register'`. The same collision breaks pytest
collection. `/etc/mcp/<server>/` cannot collide with anything. Verified by
running the container both ways.

Absence is defined per file, never guessed:

| File                     | Missing means                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------------ |
| `chronic_alerts.yaml`    | WARNING naming the path; no classification, 16 tools still served                          |
| `thresholds.yaml`        | WARNING naming the path; tools state the threshold as unset                                |
| `registry.yaml`          | **fatal** — nothing to answer from                                                          |
| `manifest.json`          | **fatal** — table names resolve from it, so answers could name a table that does not exist  |
| `dimension_samples.json` | degrades — `list_dimensions` falls back to names-only and says so                           |

A backend URL has **no default**: `PROMETHEUS_URL` and `LOKI_URL` raise a named
`ConfigError` at first use rather than pointing at one specific homelab's service
names. A guessed address reports every reading as `unavailable`, which renders a
wrong endpoint as an absent one — the failure that is hardest to see. Raising at
first use rather than at import is deliberate: the server still starts, still
answers `/healthz`, and a missing `LOKI_URL` costs `logs()` and nothing else.

Garage is the one backend whose absence is a *designed* state, so
`GARAGE_ADMIN_URL` returns `None` instead of raising — the unconfigured path is
`GarageUnconfigured`, which the caller renders as `unavailable`.

## Layout

```
src/mcp_runner/          shared: transport, registry, budgets, backend clients
servers/<name>/          one server; exposes register(registry)
tests/                   runner tests
tests/<name>/            per-server tests
deploy/examples/         ConfigMap manifests and documented config copies
scripts/                 prune_manifest.py
docs/                    adding-a-server.md, deployment.md
Dockerfile               one file, parameterised by --build-arg SERVER
```

Adding a server: [`docs/adding-a-server.md`](docs/adding-a-server.md).
Deploying one: [`docs/deployment.md`](docs/deployment.md).

## Commands

```bash
uv sync --extra dev
uv run -- pytest -q
uv run -- ruff check .

# The tool manifest, without binding a port or reaching a backend
uv run -- python -m mcp_runner --server homelab_facts --list-tools

# Serve locally against a port-forward
kubectl -n monitoring port-forward svc/prometheus 9090:9090 &
PROMETHEUS_URL=http://127.0.0.1:9090 \
  uv run -- python -m mcp_runner --server homelab_facts --port 8080
```

Diff a tool's output against hand-run PromQL before wiring an agent to it. The
tests need no cluster.

## Images

One image per server, built from one Dockerfile:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  --build-arg SERVER=homelab_facts \
  -t ghcr.io/datahub-local/mcp-homelab-facts .
```

`servers/homelab_facts` publishes as `mcp-homelab-facts` — underscores are legal
in a Python package and illegal in an image name, and `serverInfo.name` uses the
same transform so a bridge's listing matches the image.

**Multi-arch is mandatory.** Agents are scheduled onto arm64 nodes (Orange Pi),
and a server with no arm64 image is unschedulable there — which the agent
experiences as the tool simply never appearing, with nothing failing loudly.
`.github/workflows/publish.yaml` discovers `servers/*` and matrixes over them, so
a new server needs no new job.

## Deployment notes that are load-bearing

The chart that owns the `Deployment`, `Service`, `MCPServer` and the enumerated
read-only `ClusterRole` lives with the agents, not here. A kind missing from that
role is a 403, which the code keeps distinct from an empty result.

- Pods **must** carry `app.kubernetes.io/name: mcpserver`, or the
  `agent-allow-tools` NetworkPolicy blocks 8080 and every call times out with no
  useful error.
- The `MCPServer` should use the `url:` form, which stops the controller
  reconciling a deployment of its own.
- **The MCP endpoint answers on every non-health path, deliberately.** One
  server 404'd for three days with `status.ready: true` throughout because the
  discovery bridge asked for the service root while the server served `/mcp` —
  every tool was missing from every persona and nothing failed loudly. The
  bridge's path is not documented anywhere readable, so any non-health path is
  the endpoint. Health lives on `/healthz`, `/readyz`, `/livez`.
- Read `kubectl logs <run-pod> -c mcp-discover` after a deploy: it prints
  per-server tool counts, and a whole server failing is otherwise silent.
- The transport is stateless, so a bridge that does not carry a session id
  between calls works unchanged.
