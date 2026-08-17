# open-llm-proxy

OpenAI-compatible LLM proxy for NRP Nautilus. Routes requests to LLM providers, authenticates clients with a proxy key (keeping provider API keys off the browser), and logs every request/response pair to S3.

The canonical deployment lives at `https://open-llm-proxy.nrp-nautilus.io` in the `biodiversity` namespace, but the proxy is designed to be deployed independently into any namespace — each instance writes logs to its own namespace-scoped S3 bucket.

A second deployment runs on the lab's self-hosted k3s cluster at `https://llm-proxy.carlboettiger.info` (OpenRouter + DSE-nimbus only, logging to the in-cluster MinIO mirror). It is **config-only** — it runs this repo's unmodified code from `main` with a ConfigMap mounted over `config.json`, and changes nothing the NRP deployment reads. Manifests and operations: [cirrus/](cirrus/README.md).

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
| **NRP** (`ellm.nrp-nautilus.io`) | anything unmatched — e.g. `kimi`, `qwen3`, `glm-5`, `minimax-m2`, `deepseek-v4-flash`, `gemma…` | The `default_provider`, so its ids are **not** listed in `config.json`; ask the endpoint (`/v1/models`) for the live set rather than trusting this table. Supports `enable_thinking` for applicable models |
| **OpenRouter** | `anthropic/…`, `mistralai/…`, `openai/…`, `qwen/…`, `nvidia/…`, `amazon/…`, `z-ai/…`, `minimax/…`, `moonshotai/…`, `deepseek/…`, `~…` | Prefix match; requires separate API key. The `~` prefix catches OpenRouter's *floating aliases* (e.g. `~deepseek/deepseek-v4-flash-latest`), whose ids are literally `~`-prefixed and so never match a vendor prefix |
| **Anthropic** | `claude-…` — the whole family by prefix, including releases newer than this table | Direct via Anthropic's OpenAI-compatible `/v1/chat/completions`; prefix match. Bills the Developer Platform API (not the Claude.ai Team plan) — set `ANTHROPIC_API_KEY`. The default model is chosen app-side (`llm_model`); the proxy just routes whatever `claude-*` it receives. **No prompt caching** — see below |
| **Nimbus** | `qwen` (exact id only) | Private vLLM instance; requires separate API key. Exact-match so the broader `qwen…` family still goes to NRP |

Unknown models fall back to NRP (`default_provider`). NRP's own ids are deliberately **not** enumerated in
`config.json` — they all reach NRP through that fallback, so a new NRP model needs no config change.
You only list a model to route it somewhere *other* than the default. See
[`models` vs `model_prefixes`](#models-vs-model_prefixes).

#### Claude: direct vs. OpenRouter (prompt caching)

The **app picks the route by model id** — there are two ways to reach Claude, and they behave differently for [prompt caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching):

| Send `model:` | Routes to | Prompt caching |
|---|---|---|
| `claude-sonnet-4-6` (bare `claude-*`) | Anthropic direct | ❌ **ignored** |
| `anthropic/claude-sonnet-4.5` (OpenRouter slug) | OpenRouter → Anthropic | ✅ **works** |

Anthropic's OpenAI-compatibility endpoint (`/v1/chat/completions`) silently ignores message-embedded `cache_control` — prompt caching is a native Messages-API (`/v1/messages`) feature (geo-agent [#273](https://github.com/boettiger-lab/geo-agent/issues/273), proxy [#75](https://github.com/boettiger-lab/open-llm-proxy/issues/75)). OpenRouter maps the OpenAI-style `cache_control` breakpoint onto Anthropic's native param, so the same request caches there.

> **Naming gotcha:** `anthropic/…` is OpenRouter's *vendor namespace* ("the Anthropic-brand model **served by OpenRouter**"), **not** "route to Anthropic direct." Bare `claude-*` is the direct route. OpenRouter also uses its own version formatting (`claude-sonnet-4.5`, not `claude-sonnet-4-6`) — the app owns the exact slug.

To get the savings, the app (client-side) must (1) send the system prompt as content parts with a `cache_control: {"type":"ephemeral"}` breakpoint, and (2) use the `anthropic/…` model id. Add `"usage": {"include": true}` to the request body to see the cache accounting (`cached_tokens` / `cache_write_tokens`) in the response and logs. Verified on `anthropic/claude-haiku-4.5`: a repeated ~6.8k-token prefix billed `cache_write_tokens` on the first call and `cached_tokens` on the second (~12× lower prefix cost).

### Thinking mode

Set `"enable_thinking": true` in the request body to activate extended reasoning on supported models (`kimi`, `qwen3`, `glm-5`). The proxy injects the correct provider-specific parameter based on `config.json`.

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
  "default_provider": "nrp",
  "providers": {
    "nrp": {
      "endpoint": "https://ellm.nrp-nautilus.io/v1/chat/completions",
      "api_key_env": "NRP_API_KEY"
    }
  }
}
```

Note there is no model list: everything routes to `default_provider`. You only
enumerate models to send *specific* ids somewhere other than the default.

#### Ask the proxy what it serves: `GET /v1/models`

Rather than hardcoding a model list, clients can query the deployment:

```bash
curl -H "Authorization: Bearer $PROXY_KEY" https://open-llm-proxy.nrp-nautilus.io/v1/models
```

OpenAI-shaped (`{"object": "list", "data": [...]}`), with each entry annotated by
the provider that would serve it, plus a non-standard `providers` block reporting
per-provider status and prefixes. Results are cached (`MODELS_CACHE_TTL`, default
300s); `?refresh=true` forces a re-fetch.

The set is discovered from each provider's own `/v1/models` and then **filtered
through the router itself** — an id is listed under a provider only if a real
request for it would go there. So the listing cannot drift from routing, and a
provider's much larger catalog (OpenRouter serves 400+ ids; this deployment
routes 264 of them) is narrowed to what actually works here.

A provider that is unreachable degrades rather than disappearing: it keeps its
last-known-good ids, or falls back to whatever `config.json` declares, and says
so via `status` (`ok` / `stale` / `declared`).

This is also what makes deployment differences visible: the same app pointed at a
narrower deployment (see [cirrus/](cirrus/README.md)) sees a shorter list instead
of offering ids that would 400.

#### `models` vs `model_prefixes`

Routing tries **exact ids first, then prefixes**, and the two are declared
separately:

```json
"nimbus":     { "models": ["qwen"], "model_prefixes": [] },
"openrouter": { "models": [], "model_prefixes": ["z-ai/", "deepseek/", "~"] },
"anthropic":  { "models": [], "model_prefixes": ["claude-"] }
```

- **`models`** — exact ids only. Use for a single-model endpoint.
- **`model_prefixes`** — vendor families (`z-ai/`, `deepseek/`), OpenRouter's
  floating aliases (`~`), or a version family (`claude-`, which routes
  `claude-sonnet-5` and every future release without a config edit).

Exact beats prefix, so a private single-model endpoint is never swallowed by a
broader family declared elsewhere.

> ⚠️ **A provider that omits `model_prefixes` keeps the legacy behavior**, where
> every `models` entry *also* acts as a prefix. That's kept for back-compat, but
> it is a trap worth knowing: nimbus's exact id `qwen` would then also claim
> `qwen3`, `qwen3-small` and friends. Declare `model_prefixes` (even as `[]`) to
> opt into exact-only matching.

#### `default_provider` (optional)

A model id matching no provider entry goes to the **default provider**, which is
`nrp` unless you say otherwise:

```json
{
  "default_provider": "openrouter",
  "providers": { "…": {} }
}
```

This is why an NRP model id works whether or not it appears in `nrp.models` —
the fallback catches it. Two other values are useful:

- **another provider name** — for a deployment that doesn't serve NRP at all
  (e.g. `cirrus/`), so unlisted ids land somewhere that exists;
- **`null`** — no fallback. An unroutable id is rejected with a **400** naming
  what the deployment *does* serve, instead of being forwarded to a provider
  that will reject it less clearly.

A `default_provider` naming a provider this deployment doesn't configure is not
fatal: it logs a note at startup and behaves as `null`. That is also what
happens on a deployment with no `nrp` provider and no `default_provider` set —
it gets clear 400s rather than an opaque 500.

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

## Releases

Notable changes are tracked in [CHANGELOG.md](CHANGELOG.md)
([Keep a Changelog](https://keepachangelog.com/) format) and the repo follows
[Semantic Versioning](https://semver.org/) (`vMAJOR.MINOR.PATCH`). Every PR that
changes behavior, config, or ops should add an entry under `## [Unreleased]`.

To cut a release once `main` is green:

```bash
VERSION=0.1.0            # bump per semver

# 1. Move the [Unreleased] entries into a new dated section in CHANGELOG.md,
#    update the compare/tag links at the bottom, and commit (or merge a PR).
# 2. Tag and create the GitHub release with notes from the changelog section:
git tag -a "v$VERSION" -m "v$VERSION"
git push origin "v$VERSION"
gh release create "v$VERSION" --title "v$VERSION" --notes "<changelog section>"
```

Tags are descriptive only — deployment still tracks `main` (see [Update](#update)).

## Headless agent runner

`headless/run.js` replays a geo-agent session from the command line: it loads the app's STAC catalog, connects to the MCP server, assembles the exact system prompt the browser sees, and drives the tool-use loop through the proxy. Unlike calling MCP tools directly, this exercises the full proxy pipeline and writes real log entries — useful for scripted model comparisons or reproducing failures.

The runner imports the live framework modules (`Agent`, `DatasetCatalog`, `ToolRegistry`, `createMapTools`) directly from a sibling `boettiger-lab/geo-agent` checkout rather than reimplementing them, so it stays in sync with production behavior by construction. Map tools are stubbed (no live map); everything else — prompt catalog injection, `get_schema`, `<tool_call>` XML parsing, context trimming, tool-result truncation — matches the browser.

```bash
cd headless
npm install

PROXY_KEY='your-proxy-key' node run.js "Which New Jersey municipalities have passed conservation ballot measures?" \
    --config        ../../tpl/layers-input.json \
    --system-prompt ../../tpl/system-prompt.md \
    --model         qwen3 \
    --origin        https://tpl.nrp-nautilus.io/agent_runner \
    --transcript    runs/tpl-q3-qwen3.json
```

Use `--origin` with a distinctive suffix (e.g. `.../agent_runner`) so experimental runs are filterable apart from production user traffic. See `headless/README.md` for all flags and the list of fidelity caveats.

## Health check

```
GET https://<your-host>/health
```

Returns provider configuration status and whether `PROXY_KEY` is set.
