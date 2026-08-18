# open-llm-proxy on cirrus (self-hosted k3s)

A second deployment of the same proxy, on the lab's own k3s cluster instead of
NRP. Public endpoint:

    https://llm-proxy.carlboettiger.info

## Config-only, by design

**This directory contains no application code and changes nothing the NRP
deployment reads.** The pods clone `main` and run the *same* `llm_proxy.py` as
NRP; everything cirrus-specific is expressed as deployment config:

- the provider set comes from a **ConfigMap mounted over `/app/config.json`**
  (`config-configmap.yaml`) — no `config.json` edit, no new config file in the
  repo, no code change;
- the log destination comes from `LOG_BUCKET` + `AWS_S3_ENDPOINT_URL`, env vars
  the app already supported;
- ingress/CORS/timeouts are Traefik CRDs, entirely outside the app.

The upside is that **there is nothing to diverge**: cirrus cannot drift from
NRP's code, because it has none of its own, and a change made for cirrus cannot
regress NRP, because there is nothing to change. The cost is that cirrus gets
only what upstream `llm_proxy.py` already does — see *Known limitations* below.
That trade is deliberate: features cirrus wants should land in the shared app
through the normal release cycle, not as a cirrus fork.

Keep it that way. If a future cirrus need seems to require an app change, the
right move is to propose that change on its own merits for both deployments —
not to point these manifests at a branch.

## How it differs from NRP

| | NRP (`../deployment.yaml`) | cirrus (this directory) |
|---|---|---|
| Host | `open-llm-proxy.nrp-nautilus.io` | `llm-proxy.carlboettiger.info` |
| Namespace | `biodiversity` | `llm-proxy` |
| Providers | nrp ELLM, OpenRouter, Anthropic, nimbus, gemma4-nimbus, vllm-cirrus | **OpenRouter + DSE-nimbus only** |
| Ingress / CORS | HAProxy annotations | Traefik `Middleware` CRD (`middleware.yaml`) |
| Log store | NRP Ceph `s3://logs-open-llm-proxy` | in-cluster MinIO `s3://logs-open-llm-proxy` |
| Log tiers | raw JSONL → daily Parquet → monthly Parquet + `sessions/**` | **raw JSONL only** (see below) |
| Replicas | 3, spread across nodes | 2, pinned to `cirrus` |

## Models

Routing is exact-match-then-prefix over the mounted config:

- `qwen` → DSE-nimbus (`https://vllm-nimbus.carlboettiger.info`,
  `nvidia/Qwen3.6-35B-A3B-NVFP4`), with `enable_thinking` supported.
- `anthropic/…`, `openai/…`, `qwen/…`, `deepseek/…` and the other vendor
  prefixes, plus `~…` floating aliases → OpenRouter.

## Known limitations

Both follow directly from the config-only rule, and both are fixable upstream
whenever the shared app is touched for other reasons:

1. **An unrecognized model id returns `500`, not a helpful `400`.** Upstream
   `get_provider_for_model` falls back to `PROVIDERS["nrp"]` unconditionally,
   and cirrus has no `nrp` provider, so the lookup raises. It only affects
   typo'd/unrouted model names; correct ids are unaffected. (Fixing it properly
   means making the fallback provider configurable in the shared app.)
2. **No Parquet consolidation.** The daily/monthly rollup, the `sessions/**`
   per-turn view and the `session-rollup/**` per-session tier are implemented as
   ~650 lines of Python embedded in the NRP
   CronJob manifests, which cannot be reused without extracting them into the
   repo — an NRP-affecting change. So cirrus keeps **raw JSONL indefinitely**:
   nothing is lost, queries just get slower as volume grows, and the
   query-ready session view and rollup aren't available. Revisit when that
   extraction happens upstream.

## Deploy

```bash
kubectl apply -f cirrus/namespace.yaml
# secrets — see below, one time
kubectl apply -f cirrus/config-configmap.yaml
kubectl apply -f cirrus/middleware.yaml
kubectl apply -f cirrus/service.yaml
kubectl apply -f cirrus/deployment.yaml
kubectl apply -f cirrus/ingress.yaml
```

The app is **git-cloned at pod boot** (no image build), so shipping a change is:

```bash
kubectl -n llm-proxy rollout restart deployment/open-llm-proxy
```

That picks up whatever is on `main` — the same code NRP runs. A ConfigMap edit
also needs a restart (subPath mounts don't live-update).

## Secrets (one time)

Four secrets in the `llm-proxy` namespace:

```bash
# Client auth. Generate: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
kubectl -n llm-proxy create secret generic open-llm-proxy-secrets \
  --from-literal=proxy-key='...'
# optional additional revocable keys, comma-separated:
#   --from-literal=proxy-keys-extra='key1,key2'

kubectl -n llm-proxy create secret generic openrouter-key \
  --from-literal=OPENROUTER_KEY='sk-or-v1-...'

# Same vLLM API key the DSE-nimbus endpoint uses (also in vllm/vllm-api-key)
kubectl -n llm-proxy create secret generic nimbus-api-key \
  --from-literal=NIMBUS_API_KEY='...'

# MinIO service account for the log bucket (see next section)
kubectl -n llm-proxy create secret generic logs-s3 \
  --from-literal=AWS_ACCESS_KEY_ID='llm-proxy-logs' \
  --from-literal=AWS_SECRET_ACCESS_KEY='...'
```

Read the current client key back with:

```bash
kubectl -n llm-proxy get secret open-llm-proxy-secrets \
  -o jsonpath='{.data.proxy-key}' | base64 -d; echo
```

## Log bucket + MinIO service account (one time)

Logs go to the in-cluster MinIO mirror, in a **private** bucket, written by a
service account scoped to that bucket alone — not the MinIO root user. Created
via `mc` inside the MinIO pod:

```bash
POD=$(kubectl -n minio get pod -l k8s-app=minio-app -o name | head -1)
kubectl -n minio exec "$POD" -- sh -c '
  mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
  mc mb --ignore-existing local/logs-open-llm-proxy
  mc anonymous set none local/logs-open-llm-proxy
  cat > /tmp/p.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],
  "Resource":["arn:aws:s3:::logs-open-llm-proxy"]},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],
  "Resource":["arn:aws:s3:::logs-open-llm-proxy/*"]}]}
EOF
  mc admin policy create local llm-proxy-logs-rw /tmp/p.json
  mc admin user add local llm-proxy-logs "<SECRET>"
  mc admin policy attach local llm-proxy-logs-rw --user llm-proxy-logs
'
```

(`DeleteObject` is not needed today — nothing prunes the raw tier — but is
included so a future consolidation job can delete chunks after rolling them up.)

## Querying the logs

Raw JSONL only, same record format as NRP (see [../LOGGING.md](../LOGGING.md)):

```
logs-open-llm-proxy/
└── 2026-08-06/
    ├── 02-08-45-<pod>-<pid>-<uuid>.jsonl
    └── ...
```

MinIO is reachable at `https://minio.carlboettiger.info` from outside the
cluster:

```bash
export AWS_ACCESS_KEY_ID=llm-proxy-logs
export AWS_SECRET_ACCESS_KEY=...       # from the logs-s3 secret

duckdb -s "
CREATE SECRET minio (TYPE S3, KEY_ID getenv('AWS_ACCESS_KEY_ID'),
                     SECRET getenv('AWS_SECRET_ACCESS_KEY'),
                     ENDPOINT 'minio.carlboettiger.info', URL_STYLE 'path');
SELECT timestamp, model, user_message_this_turn, latency_ms
FROM read_ndjson_auto('s3://logs-open-llm-proxy/2026-*/*.jsonl', union_by_name=true)
ORDER BY timestamp DESC LIMIT 20;
"
```

`union_by_name=true` matters here: request and response records have different
field sets, and the raw tier never went through the schema-flattening step.

Live tail:

```bash
kubectl -n llm-proxy logs deployment/open-llm-proxy -f
```

## Smoke test

```bash
KEY=$(kubectl -n llm-proxy get secret open-llm-proxy-secrets \
        -o jsonpath='{.data.proxy-key}' | base64 -d)

curl -s https://llm-proxy.carlboettiger.info/health | jq
curl -s https://llm-proxy.carlboettiger.info/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"Say hi."}]}' | jq -r '.choices[0].message.content'
```
