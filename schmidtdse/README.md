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
restart.

| | `biodiversity` (root manifests) | `schmidtdse` (this directory) |
|---|---|---|
| Service | `open-llm-proxy.biodiversity.svc.cluster.local:8002` | `open-llm-proxy.schmidtdse.svc.cluster.local:8002` |
| Client keys | every key | the `schmidtdse` apps' keys only |
| Budget counters | own redis | own redis, so the daily totals are counted separately |
| Replicas | 3 | 2 |
| Logs | `s3://logs-open-llm-proxy` | the same bucket and the same consolidation CronJobs |

Log records carry `key_name`, so the two deployments' traffic stays separable in
one corpus. The consolidation and mirror CronJobs run only in `biodiversity`.
They read the bucket, not the proxy, so this deployment needs no copy of them.

## Deploy

Check `kubectl config current-context` first (it should be `nautilus`).

```bash
./schmidtdse/make-secrets.sh            # dry run: names only, no values
./schmidtdse/make-secrets.sh --apply    # copy provider, S3 and redis secrets; write client keys

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

`make-secrets.sh` copies the OpenRouter, Anthropic, Nimbus and NRP keys and the
general NRP S3 credential (`aws`) into `schmidtdse`. Anyone who can create pods
in `schmidtdse` can read them, as is already the case in `biodiversity`. See
glen-ops#219 for the longer-term plan to put the proxy in a namespace of its own.
