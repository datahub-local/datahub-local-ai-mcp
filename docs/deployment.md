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

For `semantic`, run [`scripts/prune_manifest.py`](../scripts/prune_manifest.py)
over dbt's manifest before creating the ConfigMap: 671 KB → 3.4 KB against a
1 MiB limit, byte-identical SQL. Optional, never required.

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
