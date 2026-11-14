# Deployment

The images are generic; everything cluster-specific is a ConfigMap. The
`Deployment`, `Service`, `MCPServer` and `ClusterRole` live in the chart that
owns the agents, not here.

## Images

```
ghcr.io/datahub-local/mcp-homelab-facts:main
ghcr.io/datahub-local/mcp-semantic:main
```

Also tagged with the commit sha. Both are `linux/amd64,linux/arm64`.

## Environment

| Variable                    | Server        | Required | Notes                                       |
| --------------------------- | ------------- | -------- | ------------------------------------------- |
| `PROMETHEUS_URL`            | homelab_facts | **yes**  | no default; raises a named error at first use |
| `LOKI_URL`                  | homelab_facts | **yes**  | as above; missing costs `logs()` only        |
| `MCP_CONFIG_DIR`            | both          | no       | overrides `/etc/mcp/<server>/`               |
| `MCP_STATE_DIR`             | homelab_facts | no       | snapshots for computed diffs; emptyDir is enough |
| `GARAGE_ADMIN_URL`          | homelab_facts | no       | absent = bucket section `unavailable`        |
| `GARAGE_ADMIN_TOKEN`        | homelab_facts | no       | as above; scope to `ListBuckets,GetBucketInfo` |
| `SEMANTIC_REGISTRY_VERSION` | semantic      | no       | stamped onto every answer                    |
| `PROMETHEUS_TIMEOUT_SECONDS` | homelab_facts | no      | default 20                                   |
| `LOKI_TIMEOUT_SECONDS`      | homelab_facts | no       | default 20                                   |
| `GARAGE_TIMEOUT_SECONDS`    | homelab_facts | no       | default 10                                   |

A backend URL has no default deliberately. Pointing at one specific homelab's
service names means a second deployment that forgets to set it queries a service
that does not exist and reports every metric `unavailable` — a wrong endpoint
rendering as an absent one, which is the failure hardest to see. Garage is the
exception: its absence is a designed state, so it returns `None` and the caller
says `unavailable`.

## Timeouts

Four ceilings bound one tool call: the server's own backend timeout, the bridge
timeout the Sympozium controller applies, the Hermes client's per-server `timeout`,
and the Pod probe that decides whether the process survives. The bridge value is
the persona's `mcpServers[].timeout`; `MCPServer.spec.timeout` is inherited only
when that entry omits one (`resolveMCPServerURLs`, v0.13.0), so a catalog value
alone cannot bound an agent. The rule is **server-internal < bridge < Hermes
client**, with at least 10s of headroom at each step, and the bridge must also sit
10s above the tool's measured p-max. A server with no internal HTTP ceiling is
bounded by its bridge. Hermes connects directly and does not see the catalog
value; the bridge is what Sympozium agents get. `opencode` pairs a 600s bridge
with a 900s client because the shim's own `run_task` wait budget is 300s.

These are the effective values in this homelab. The sources are
`datahub-local-core/releases/automation/values/{mcp,hermes,opencode}.yaml.gotmpl`
for the catalog, the gateway and OpenCode, and
`datahub-local-ai/agents/sympozium/values/default.yaml.gotmpl` for the four
servers this repository owns; the personas that mount them state the same value in
`agents/sympozium/projects/*/agents/*.yaml`.

| Server | Server-internal | `spec.timeout` | Hermes `timeout` | Pod probe |
| --- | --- | --- | --- | --- |
| `k8s` | none | 30 | 60 | readiness `/healthz` 5s |
| `github` | none | 30 | 60 | readiness `/healthz` 5s; liveness TCP 30s |
| `grafana` | none | 30 | 60 | controller default |
| `postgres` | none | 60 | 120 | controller default; see below |
| `argocd` | none | 30 | 60 | controller default |
| `slack` | none | 30 | 60 | controller default |
| `trino` | 30 | 60 | 120 | readiness TCP 5s; liveness TCP 15s |
| `firecrawl` | Firecrawl API default | 60 | 300 | readiness `/health` 5s; liveness `/health` 15s |
| `n8n` | none | not catalogued | 60 | readiness `/health` 10s; liveness `/health` 15s |
| `opencode` | 20 per HTTP call; 300 per `run_task` wait | 600 | 900 | readiness `/healthz` 10s; liveness `/healthz` 30s |
| `homelab-facts` | Kubernetes lists unbounded; Prometheus 20, Loki 20, Garage 10 | 90 | not registered | liveness `/healthz` 30s, 10s timeout, 6 failures; readiness 10s |
| `semantic` | Trino 30 | 60 | not registered | as `homelab-facts` |
| `semantic-finance` | Trino 30 | 60 | not registered | as `homelab-facts` |
| `utils` | fetch 120 | 130 | not registered | as `homelab-facts` |

`homelab-facts` is 90, not 60. Its name-resolving tools list up to thirteen kinds
from the Kubernetes API; measured on 2026-11-14 at a 180s client ceiling,
`find_object` was 35.6s p50 and 40.7s max, `logs` 33.4 and 52.3, `why_failed` 42.8
and 60.2 over 22 calls. The 20s Prometheus and Loki ceilings do not bind those
calls: none of the three queries Prometheus, and Loki appears only on fallback,
where `why_failed` on a Job reached 60.2s. The server also runs every JSON-RPC
request on one worker, so a call the bridge abandons keeps the worker and a retry
measured 64.9s. 90s covers the measured p-max with 29.8s of headroom; shortening
the fan-out or widening the executor is a separate server change.

`controller default` is the Sympozium controller's probe on a managed stdio
`MCPServer`, read from the live Deployments on 2026-11-11: liveness `/healthz`
every 15s, 5s timeout, 5 failures, 30s initial delay; readiness `/readyz` every
10s, 5s timeout, 5 failures, 20s initial delay.

Three cases are recorded here because the numbers alone mislead:

- **firecrawl's 300s timeouts were not a short timeout.** On 2026-10-31 two
  `firecrawl_scrape` calls ended at exactly 300s while the same server's
  keepalive failed. The stdio bridge shared one child across callers and misaligned
  Firecrawl's replies; the stateless HTTP transport removed the hang on 2026-11-02.
  Raising the client would only have made each failure slower. The 300s value stays
  explicit, above the 60s bridge, because the Firecrawl API can legitimately run
  past the bridge. Its `keepalive_interval: 3600` stays too: the stateless
  transport has no session to refresh and closes the GET event stream, so the
  default 180s probe would tear the transport down mid-call.
- **postgres' park loop was not a tool timeout.** The stdio adapter shares one
  child with `/healthz`, so a request sent before `initialize` blocked the health
  handler; the liveness probe killed the container after its 30s initial delay
  plus five 15s periods. The CRD exposes no probe fields, so these numbers are the
  controller's and cannot be sized from any repository here. Widening the Hermes
  timeout would have changed nothing.
- **`utils`' 120s fetch belongs to n8n, not the catalog.** n8n calls the Service
  directly for `temp_store`; no agent mounts it. Its bridge is 130s so the rule
  holds for any future catalog caller, and a slow fetch of a rendered video is not
  cut short.

Two wiring rules for the Garage secret, both learned the hard way:

- make the refs `optional: true` — an unresolvable `secretKeyRef` holds the pod
  in `CreateContainerConfigError` and takes down all sixteen tools rather than
  the one section that needs it;
- name each key individually rather than using `envFrom` — the Secret carrying
  the admin token may also carry `AWS_SECRET_ACCESS_KEY`, which is a write
  credential a read-only reporter must not hold.

## ConfigMaps

See [`deploy/examples/`](../deploy/examples/). Mount at `/etc/mcp/<server>/`.

**Never mount under `/app`.** WORKDIR is `/app` and lands on `sys.path`, so
`/app/semantic/` shadows the `semantic` package and the server dies at startup
with `module 'semantic' has no attribute 'register'`. Verified by running the
container both ways.

```yaml
volumeMounts:
  - name: config
    mountPath: /etc/mcp/homelab_facts
    readOnly: true
volumes:
  - name: config
    configMap:
      name: mcp-homelab-facts
```

Config is cached at first read, so a ConfigMap update needs a pod restart.

`semantic` mounts only `registry.yaml`; it reads table structure, cardinality
and dimension values from the warehouse. That needs `TRINO_URL` and
`SEMANTIC_WAREHOUSE_SCOPES` (comma-separated `catalog.schema` pairs, no
default), and the dbt project must persist column descriptions into the
warehouse — a blank description becomes a NULL comment, which the documentation
gate cannot tell from an undocumented column.

## Pod requirements

- **`app.kubernetes.io/name: mcpserver`** on the pod, or the `agent-allow-tools`
  NetworkPolicy blocks 8080 and every call times out with no useful error.
- Runs as uid 65532, non-root. It needs no write access to anything except
  `MCP_STATE_DIR`.
- Port 8080. Health on `/healthz`, `/readyz`, `/livez`.

## MCPServer

Use the `url:` form — it stops the controller reconciling a deployment of its
own.

**The MCP endpoint answers on every non-health path, deliberately.** One server
404'd for three days with `status.ready: true` throughout because the discovery
bridge asked for the service root while the server served `/mcp` — every tool
was missing from every persona and nothing failed loudly. The bridge's path is
not documented anywhere readable, so any non-health path is the endpoint.

After any deploy, transport or image change, read the discovery log: it prints
per-server tool counts, and a whole server failing is otherwise silent.

```bash
kubectl logs <run-pod> -c mcp-discover
```

Expect 16 tools for `homelab_facts` and 4 for `semantic`.

## Verifying by hand

```bash
kubectl -n automation port-forward svc/mcp-homelab-facts 8080:8080 &
curl -s http://127.0.0.1:8080/healthz
curl -s -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
```

`initialize` reports `serverInfo.name` as `mcp-<server>`, which is how a bridge
listing several of these distinguishes them.

## Agent-side notes

These are properties of the agent runtime, not of this repository, but they
decide whether the tools are usable:

- **`toolsAllow` is the prompt budget, not just permission.** Every tool a
  server exposes is registered and its schema injected, whatever a tool policy
  filters at the LLM request. At roughly 670 tokens per schema, an unfiltered
  set costs ~40 KB of prompt on *every* call in the loop. Pin the allowlist per
  persona to exactly the tools it needs.
- A tool name that does not exist fails **silently** — it simply never appears,
  and the agent writes a blander report. Verify names with a `tools/list` call
  rather than inferring them.
