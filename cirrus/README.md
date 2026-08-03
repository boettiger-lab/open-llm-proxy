# open-llm-proxy on cirrus (self-hosted k3s)

A second deployment of the same proxy, on the lab's own k3s cluster instead of
NRP. Public endpoint:

    https://llm-proxy.carlboettiger.info

It is **not** a mirror of the NRP deployment — deliberately narrower:

| | NRP (`../deployment.yaml`) | cirrus (this directory) |
|---|---|---|
| Host | `open-llm-proxy.nrp-nautilus.io` | `llm-proxy.carlboettiger.info` |
| Namespace | `biodiversity` | `llm-proxy` |
| Providers | nrp ELLM, OpenRouter, Anthropic, nimbus, gemma4-nimbus, qwen3-cirrus | **OpenRouter + DSE-nimbus only** (`../config.cirrus.json`) |
| Unknown model | falls through to nrp ELLM | `400` (no `default_provider`) |
| Ingress / CORS | HAProxy annotations | Traefik `Middleware` CRD (`middleware.yaml`) |
| Log store | NRP Ceph `s3://logs-open-llm-proxy` | in-cluster MinIO `s3://logs-open-llm-proxy` |
| Replicas | 3, spread across nodes | 2, pinned to `cirrus` |

Everything else — the app, the log schema, the three-tier
JSONL → daily Parquet → monthly Parquet rollup, the `sessions/**` per-turn view
— is identical, running the same `llm_proxy.py` and `consolidate_logs.py` from
this repo.

## Models

Routing is exact-match-then-prefix over `config.cirrus.json`:

- `qwen` → DSE-nimbus (`https://vllm-nimbus.carlboettiger.info`,
  `nvidia/Qwen3.6-35B-A3B-NVFP4`), with `enable_thinking` supported.
- `anthropic/…`, `openai/…`, `qwen/…`, `deepseek/…` and the rest of the vendor
  prefixes, plus `~…` floating aliases → OpenRouter.
- anything else → `400 Unknown model` listing the routable prefixes. This is the
  point of omitting `default_provider`: a typo can't be silently forwarded to a
  billed provider.

## Deploy

```bash
kubectl apply -f cirrus/namespace.yaml
# secrets — see below, one time
kubectl apply -f cirrus/middleware.yaml
kubectl apply -f cirrus/service.yaml
kubectl apply -f cirrus/deployment.yaml
kubectl apply -f cirrus/ingress.yaml
kubectl apply -f cirrus/consolidate-daily-cronjob.yaml
kubectl apply -f cirrus/consolidate-monthly-cronjob.yaml
```

The app is **git-cloned at pod boot** (no image build), so shipping a code or
`config.cirrus.json` change to cirrus is:

```bash
kubectl -n llm-proxy rollout restart deployment/open-llm-proxy
```

Note the corollary: a pod restart picks up whatever is on `main` at that moment.

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

`DeleteObject` is required: the consolidation job deletes the raw JSONL chunks
once their Parquet file is verified.

## Querying the logs

Same schema and same three tiers as NRP (see [../LOGGING.md](../LOGGING.md)) —
only the endpoint differs. From outside the cluster, MinIO is reachable at
`https://minio.carlboettiger.info`:

```bash
export AWS_ACCESS_KEY_ID=llm-proxy-logs
export AWS_SECRET_ACCESS_KEY=...       # from the logs-s3 secret

duckdb -s "
CREATE SECRET minio (TYPE S3, KEY_ID getenv('AWS_ACCESS_KEY_ID'),
                     SECRET getenv('AWS_SECRET_ACCESS_KEY'),
                     ENDPOINT 'minio.carlboettiger.info', URL_STYLE 'path');
SELECT ts, model, user_message_this_turn, latency_ms
FROM read_parquet('s3://logs-open-llm-proxy/consolidated/**/*.parquet')
ORDER BY ts DESC LIMIT 20;
"
```

For an rclone-then-query workflow like `../sync-logs.sh`, add a remote pointing
at `https://minio.carlboettiger.info` with those credentials and sync
`logs-open-llm-proxy`.

Today's not-yet-consolidated traffic is raw JSONL under
`s3://logs-open-llm-proxy/YYYY-MM-DD/*.jsonl` (`read_ndjson_auto`,
`union_by_name=true`), or live:

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
