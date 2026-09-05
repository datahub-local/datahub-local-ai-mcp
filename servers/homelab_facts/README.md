# homelab_facts

Cluster state as already-correct readings. Sixteen tools: eleven take **no
arguments at all**, and five take free text where any string is valid.

Both halves are the same rule — an argument is safe when there is no format to
get wrong. The prompts this replaced spent 2.3 KB explaining that a time
parameter was the literal word `now`, three characters, and that the quotation
marks around a JSON string are not part of the value.

| Tool                                 | What it replaces                                                                                                                                                                             |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `find_object(term)`                  | the exact namespace and name of anything, from the words a person typed — searched three ways over thirteen kinds                                                                             |
| `why_failed(term)`                   | the whole investigation: the object, its pods via owner references or selector, each container's state, the Warning events, and the log of the container that broke — including its *previous* instance when crash-looping |
| `logs(term, contains)`               | pod name, container, kubelet-or-Loki fallback, the LogQL selector                                                                                                                             |
| `endpoints(term)`                    | what `curl: (7)` actually means: ready and not-ready endpoint counts, the pods the Service's selector picks, any IngressRoute hostname                                                        |
| `alerts_snapshot()`                  | the firing-alert query **plus** the new / still-firing / resolved diff against a stored snapshot, **plus** the chronic classification                                                          |
| `volume_fill()`                      | the full `group_left` expression, already the *used* fraction, already restricted to storage classes where a percentage means anything                                                        |
| `node_fleet()`                       | the entire node table — disk, stalls, temperature, SMART, uptime, updates, UPS, kernel — with machine identity joined in the query and drift computed within hardware class                    |
| `postgres_health()`                  | the archiver **increase**, backends, database sizes, cluster objects                                                                                                                          |
| `cache_health()`                      | Valkey via `redis_*`, with no percentage because there is no ceiling to divide by                                                                                                            |
| `object_store_health()`              | Garage's per-node consensus, the **layout** capacity that is its real headroom rather than the disk under it, per-bucket size where a token allows, and one shared filesystem reported once   |
| `stream_health()`                    | Redpanda's cluster counts off the controller leader, per-broker log disk with Redpanda's own verdict, throughput and leadership churn as increases                                            |
| `metrics_store_health()`             | Prometheus's own store: history held against configured **and against uptime**, size against limit, active series, WAL and compaction integrity, dark scrape targets                          |
| `cert_expiry()`, `backup_freshness()`| dates turned into days and ages, hundreds of backup objects summarised per schedule                                                                                                          |
| `argocd_drift()`                     | sync/health state plus a consecutive-run counter                                                                                                                                             |
| `top_services()`                     | HTTP services ranked by request rate with each one's 5xx share — the `rate()` window applied server-side, grouped on the label the scrape *relabelled it to*, and the router hash stripped so a name is stable between runs |
| `workload_readiness()`               | every Deployment, StatefulSet and DaemonSet with fewer ready pods than it wants, each comparison carrying the explicit `on()` join without which the label sets miss and everything reads healthy |
| `promql(expr)`                       | arbitrary Prometheus, with the datasource, the time and the query type supplied server-side                                                                                                  |

They do **not** replace reach. A caller keeps its raw `k8s_*` tools for following
up on whatever these surface. The win is budget reallocation: the mandatory
readings drop from eight-plus calls to one or two, leaving the iteration budget
for real investigation — which is exactly where the run that motivated this
spent 13 consecutive calls hunting a namespace that does not exist.

The investigation tools extend that from *gathering* to *sequencing*.
`find_object` closed the gap between the word a person types and the string the
API needs; what remained was the chain after it — object, pods, containers,
events, logs — four calls of exact arguments in a fixed order, and the step every
failed run got wrong. Asked the question from the incident (`grafana setup job`),
`why_failed` answers in **one call and 1,651 bytes** with the Job, its pod, the
log line and a verdict; the run it replaced spent eight calls, guessed a label
key in five of them, and concluded that a running grafana did not exist.

## Data

Two keys at `/etc/mcp/homelab_facts/`, mounted as a ConfigMap. Both are
judgements no cluster can answer, which is exactly why they are config while node
names and hardware classes are derived at query time:

| Key                   | What                                                                 |
| --------------------- | -------------------------------------------------------------------- |
| `chronic_alerts.yaml` | whether an alert is noise. Names alert *rules*, never machines. A `never_suppress` list keeps a permanently-firing *real* fault from being absorbed. |
| `thresholds.yaml`     | where a reading becomes a finding. Every tool states the threshold it applied. |

Absence is **loud but never fatal**: a WARNING naming the path it tried, then all
sixteen tools served with no classification and no thresholds. Sixteen working
tools beat a pod that refuses to start over two missing files.

Example: [`deploy/examples/configmap-homelab-facts.yaml`](../../deploy/examples/configmap-homelab-facts.yaml).
Documented copies of this fleet's own answers: [`deploy/examples/homelab_facts/`](../../deploy/examples/homelab_facts/).

## Backends

`PROMETHEUS_URL` and `LOKI_URL` are **required** and have no default — see
[`docs/deployment.md`](../../docs/deployment.md). Garage is optional: with no
token the bucket section reports itself `unavailable` and nothing else changes.

## The ingress controller

`top_services()` names no controller. Which counter carries requests, and which
label on it carries the backend name, are both **required** with no default:

| Variable                        | Required | Example                                          |
| ------------------------------- | -------- | ------------------------------------------------ |
| `INGRESS_REQUESTS_METRIC`       | yes      | `traefik_service_requests_total`, `nginx_ingress_controller_requests` |
| `INGRESS_SERVICE_LABEL`         | yes      | `exported_service`, `service`                    |
| `INGRESS_STATUS_LABEL`          | no       | `code` (default), `status`                       |
| `INGRESS_SERVICE_STRIP_PATTERN` | no       | `-[0-9a-f]{16,}@[a-z]+$`                         |

The label is required rather than derived because it is not guessable even
knowing the controller. A ServiceMonitor that already owns `service` makes
Prometheus relabel the exporter's own to `exported_service` — and grouping on
the wrong one **succeeds**, returning a single plausible row carrying the whole
fleet's traffic under the scrape job's name. Verified: the same query answered
`22 services` on the right label and `1 service, traefik-metrics` on the wrong
one, with no error on either.

`INGRESS_SERVICE_STRIP_PATTERN` is cosmetic but not pointless: a Traefik router
name ends in a config hash that changes whenever the route is edited, so an
untrimmed name reads as a *new* service on the next run and breaks any
comparison a caller makes against its own memory. An invalid pattern is logged
and ignored rather than fatal.
