# open-llm-proxy in schmidtdse

A second NRP deployment of the proxy, for the apps that run in the `schmidtdse`
namespace (tpl-ca, bosl-high-seas, bosl-high-seas-private, coasts,
wallacea-internal and the rest of `open-llm-proxy-app-keys`).

## Why it exists

Since the Nov 7 2026 security incident, pods in other namespaces cannot open
connections to pods in `biodiversity`. Cluster-level policy that this project
cannot see or change enforces the block. `schmidtdse` apps lost the LLM: their
nginx timed out connecting to `open-llm-proxy.biodiversity.svc.cluster.local`.
Outbound traffic from `schmidtdse` to the NRP S3 gateway, ellm and the internet
still works, so a proxy inside `schmidtdse` reaches every provider and logs to
the same bucket.

## Same code, same config

This directory is a kustomize overlay of the root manifests. It changes the
namespace, the DNS search domain, the redis address and the replica count, and
nothing else. The pods clone `main` and read the repo's `config.json`, the same
as the `biodiversity` deployment. Model tiers, budgets and logging are therefore
identical, and a change on `main` reaches both deployments at their next
restart. Each namespace's `rustfs-logs-write` Secret names its own log bucket,
so the logs stay apart.

| | `biodiversity` (root manifests) | `schmidtdse` (this directory) |
|---|---|---|
| Service | `open-llm-proxy.biodiversity.svc.cluster.local:8002` | `open-llm-proxy.schmidtdse.svc.cluster.local:8002` |
| Client keys | every key | the `schmidtdse` apps' keys only |
| OpenRouter key | lab key | its own key (`SCHMIDTDSE_OPENROUTER_KEY`), billed and capped separately |
| Budget counters | own redis | own redis, so the daily totals are counted separately |
| Replicas | 3 | 2 |
| Logs | rustfs `s3://logs-open-llm-proxy` | rustfs `s3://logs-open-llm-proxy-schmidtdse` |
| Log credential | `rustfs-logs-write`, scoped to its one bucket | `rustfs-logs-write`, scoped to its one bucket |

## Logging

The proxy writes raw JSONL to `logs-open-llm-proxy-schmidtdse` on rustfs
(`https://rustfs.nrp-nautilus.io`; the in-cluster rustfs Service is not
reachable from `schmidtdse`). The daily and monthly consolidation CronJobs run
here too, from the same root manifests, and take `LOG_BUCKET` and
`LOG_S3_ENDPOINT` from that Secret. They build the same `consolidated/`,
`sessions/` and `session-rollup/` tiers as in `biodiversity`.

glen-ops mints the bucket and two Secrets in `schmidtdse`: `rustfs-logs-write`
(Get/Put/List/Delete, used by the proxy and the CronJobs) and `rustfs-logs-read`
(Get/List, for analysis). Each holds `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_S3_ENDPOINT` and `BUCKET`. Rotating one means
re-minting it and restarting the proxy; nothing else in the lab shares it.

## Weekly reset

NRP culls Deployments in `schmidtdse` after two weeks. glen-ops' Sunday
`schmidtdse-refresh` job deletes the two Deployments by name and re-applies the
render (CronJobs are not culled, so they are only re-applied):

```bash
kubectl -n schmidtdse delete deploy open-llm-proxy open-llm-proxy-redis --ignore-not-found
kubectl kustomize --load-restrictor LoadRestrictionsNone schmidtdse | kubectl -n schmidtdse apply -f -
```

## Deploy

Check `kubectl config current-context` first (it should be `nautilus`).

```bash
set -a; . ~/.env; set +a                # SCHMIDTDSE_OPENROUTER_KEY
./schmidtdse/make-secrets.sh            # dry run: names only, no values
./schmidtdse/make-secrets.sh --apply    # set OpenRouter key; copy Nimbus and redis secrets; write client keys
# rustfs-logs-write must already exist (glen-ops); the pods will not start without it

kubectl kustomize --load-restrictor LoadRestrictionsNone schmidtdse \
  | kubectl -n schmidtdse apply -f -

kubectl -n schmidtdse rollout status deploy/open-llm-proxy
```

`kubectl apply -k` cannot be used, because the overlay reads files outside its
own directory.

Re-run `make-secrets.sh --apply` after issuing or revoking a `schmidtdse` app's
key in `biodiversity`. The proxy re-reads the mounted key file within about a
minute, with no restart.

## Pointing an app at it

Each app's nginx `location /api/llm/` changes its `proxy_pass` host from
`open-llm-proxy.biodiversity.svc.cluster.local` to
`open-llm-proxy.schmidtdse.svc.cluster.local`. That edit lives in each app's
repo and is rolled by glen-ops. Nothing else changes: the same key works on
both proxies.

## Credentials in this namespace

`schmidtdse` holds its own OpenRouter key and its own scoped rustfs log keys,
plus copies of the Nimbus and NRP model keys. The general NRP S3 credential is
not copied here. Anyone who can create pods
in `schmidtdse` can read them, as is already the case in `biodiversity`. See
glen-ops#219 for the longer-term plan to put the proxy in a namespace of its own.
