# open-llm-proxy

OpenAI-compatible LLM proxy for NRP Nautilus. Routes requests to LLM providers, authenticates clients with a proxy key (keeping provider API keys off the browser), and logs every request/response pair to S3.

The canonical deployment lives at `https://open-llm-proxy.nrp-nautilus.io` in the `biodiversity` namespace, but the proxy is designed to be deployed independently into any namespace — each instance writes logs to its own namespace-scoped S3 bucket.

## Architecture

```
your-namespace
┌──────────────────────────────────────────────────────┐
│  geo-agent app(s)                                    │
│       │                                              │
│       ├── POST /v1/chat/completions ──▶ open-llm-proxy ──▶ NRP / OpenRouter / Nimbus
│       │         (PROXY_KEY auth)            │         │
│       │                                     ▼         │
│       │                            s3://logs-<ns>/    │
│       │                                              │
│       └── MCP queries ──▶ mcp-data-server (shared or local)
└──────────────────────────────────────────────────────┘
```

Each namespace runs its own open-llm-proxy. The MCP data server can be shared across namespaces (it serves read-only data and doesn't need per-team isolation). Logs stay within the namespace's S3 bucket, so anyone with access to the namespace can query their logs but not other teams'.

## How it works

Clients send standard `/v1/chat/completions` requests with a `PROXY_KEY` in the `Authorization` header. The proxy:

1. Authenticates the client against `PROXY_KEY`
2. Routes to the correct provider based on model name
3. Logs the request and response to stdout and buffers for S3
4. Returns the provider response unmodified

### Provider routing

Configured in `config.json`. Most deployments only need NRP:

| Provider | Models | Notes |
|---|---|---|
| **NRP** (`ellm.nrp-nautilus.io`) | `kimi`, `qwen3`, `glm-4.7`, `minimax-m2`, `gpt-oss`, `gemma` | Default; supports `enable_thinking` for applicable models |
| **OpenRouter** | `anthropic/…`, `mistralai/…`, `openai/…`, `qwen/…`, `nvidia/…`, `amazon/…` | Prefix match; requires separate API key |
| **Nimbus** | `nemotron` | Private vLLM instance; requires separate API key |

Unknown models fall back to NRP. To customize the model list, edit `config.json` — no code change needed.

### Thinking mode

Set `"enable_thinking": true` in the request body to activate extended reasoning on supported models (`kimi`, `qwen3`, `glm-4.7`). The proxy injects the correct provider-specific parameter based on `config.json`.

## Logging

Every LLM call produces two JSONL log entries (a `request` on arrival and a `response` on completion) linked by `request_id`.

Logs are written to **pod stdout** immediately and flushed to **S3** every 5 minutes as dated JSONL chunk files. The target bucket is set by the `LOG_BUCKET` env var (e.g. `logs-cacao` for the cacao namespace). If no S3 credentials are present, logs go to stdout only.

```
logs-<ns>/
├── 2026-04-07/
│   ├── 02-00-05-39.jsonl
│   └── ...
```

Query with DuckDB:

```sql
CREATE SECRET (TYPE S3, ENDPOINT 's3-west.nrp-nautilus.io', USE_SSL 'TRUE', URL_STYLE 'path');
SELECT * FROM read_ndjson_auto('s3://logs-<ns>/2026-04-07/*.jsonl');
```

See [LOGGING.md](LOGGING.md) for the full field reference, conversation reconstruction patterns, and kubectl access.

## Deploy to your namespace

### 1. Create secrets

You need at minimum an NRP API key and a proxy key:

```bash
kubectl create secret generic open-llm-proxy-secrets \
  --from-literal=nrp-api-key='your-nrp-api-key' \
  --from-literal=proxy-key='your-proxy-key' \
  -n <your-namespace>
```

If your namespace has an `aws` secret with S3 credentials, logs will be flushed to S3 automatically. See `secrets.yaml.example` for the full secret structure and optional providers.

### 2. Configure

Copy `config.json` or use it as-is. To use only NRP (the common case), you can trim it to:

```json
{
  "providers": {
    "nrp": {
      "endpoint": "https://ellm.nrp-nautilus.io/v1/chat/completions",
      "api_key_env": "NRP_API_KEY",
      "models": ["kimi", "qwen3", "glm-4.7"]
    }
  }
}
```

### 3. Set your log bucket

In `deployment.yaml`, set `LOG_BUCKET` to a bucket in your namespace:

```yaml
- name: LOG_BUCKET
  value: "logs-<your-namespace>"
```

If you don't have S3 credentials or don't set this, logs still go to pod stdout.

### 4. Apply manifests

```bash
kubectl apply -f service.yaml -n <your-namespace>
kubectl apply -f ingress.yaml -n <your-namespace>
kubectl apply -f deployment.yaml -n <your-namespace>
kubectl rollout status deployment/open-llm-proxy -n <your-namespace>
```

Update the `host` in `ingress.yaml` to match your namespace's desired hostname.

## Update

Push changes to `main` (or your fork), then:

```bash
kubectl rollout restart deployment/open-llm-proxy -n <your-namespace>
```

## Testing with agent_runner.py

`agent_runner.py` replays a geo-agent-style session from the command line: it connects to an MCP server for tools, sends requests through the proxy, and drives the tool-use loop to completion. Unlike calling MCP tools directly, this exercises the full proxy pipeline and generates real log entries.

```bash
pip install openai mcp httpx

# Basic query using defaults (nemotron model, canonical proxy + MCP endpoints)
export OPENAI_API_KEY='your-proxy-key'
python agent_runner.py "Rank states by fraction that is GAP 1+2"

# Reproduce a specific app's behavior with its system prompt
python agent_runner.py "How many MPAs have IUCN category II?" \
    --model anthropic/claude-sonnet-4-5 \
    --origin https://bosl-high-seas.nrp-nautilus.io \
    --system-prompt ~/repos/bosl-high-seas/system-prompt.md
```

Use `--origin` to tag the log entries with a specific app, and `--system-prompt` to load the actual app's system prompt (path or URL) so the model behaves the same way it would in production.

## Health check

```
GET https://<your-host>/health
```

Returns provider configuration status and whether `PROXY_KEY` is set.
