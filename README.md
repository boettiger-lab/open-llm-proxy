# open-llm-proxy

OpenAI-compatible LLM proxy for NRP Nautilus. Routes requests to LLM providers, authenticates clients with a proxy key (keeping provider API keys off the browser), and logs every request/response pair to S3.

The canonical deployment runs in the `biodiversity` namespace on NRP and is reachable **only inside the cluster**, at `http://open-llm-proxy.biodiversity.svc.cluster.local:8002/v1`. It has had no public ingress since 2026-11-07 (#162): browsers reach it through each app's nginx at `/api/llm/`, which adds that app's key, and batch work runs as cluster Jobs. The proxy is designed to be deployed independently into any namespace, but the proxy is designed to be deployed independently into any namespace — each instance writes logs to its own namespace-scoped S3 bucket.

A second deployment is defined for the lab's self-hosted k3s cluster (OpenRouter + DSE-nimbus only, logging to the in-cluster MinIO mirror). It is parked: `replicas: 0`, and its public hostname `llm-proxy.carlboettiger.info` is gone. It is **config-only** — it runs this repo's unmodified code from `main` with a ConfigMap mounted over `config.json`, and changes nothing the NRP deployment reads. Manifests and operations: [cirrus/](cirrus/README.md).

## Architecture

```
your-namespace
┌──────────────────────────────────────────────────────┐
│  geo-agent app(s)                                    │
│       │                                              │
│       ├── POST /v1/chat/completions ──▶ open-llm-proxy ──▶ NRP / OpenRouter / Nimbus
│       │       (client key auth)             │         │
│       │                                     ▼         │
│       │                            s3://logs-<ns>/    │
│       │                                              │
│       └── MCP queries ──▶ mcp-data-server (shared or local)
└──────────────────────────────────────────────────────┘
```

Each namespace runs its own open-llm-proxy. The MCP data server can be shared across namespaces (it serves read-only data and doesn't need per-team isolation). Logs stay within the namespace's S3 bucket, so anyone with access to the namespace can query their logs but not other teams'.

## How it works

Clients send standard `/v1/chat/completions` requests with their own client key in the `Authorization` header. The proxy:

1. Authenticates the client against its key list (see [Client keys](#client-keys))
2. Routes to the correct provider based on model name
3. Logs the request and response to stdout and buffers for S3
4. Returns the provider response unmodified

### Provider routing

Configured in `config.json`. Most deployments only need NRP:

| Provider | Models | Notes |
|---|---|---|
| **NRP** (`ellm.nrp-nautilus.io`) | anything unmatched — e.g. `kimi`, `qwen3`, `glm-5`, `minimax-m2`, `deepseek-v4-flash`, `gemma…` | The `default_provider`, so its ids are **not** listed in `config.json`; ask the endpoint (`/v1/models`) for the live set rather than trusting this table. Supports `enable_thinking` for applicable models |
| **OpenRouter** | any namespaced `vendor/model` id, e.g. `z-ai/…`, `deepseek/…`, `reflection/…`, plus `~…` | The `namespaced_default_provider`, so a new OpenRouter vendor needs no config change; the declared `model_prefixes` are now documentation more than routing. Requires separate API key. The `~` prefix catches OpenRouter's *floating aliases* (e.g. `~deepseek/deepseek-v4-flash-latest`), whose ids are literally `~`-prefixed and so never match a vendor prefix |
| **Nimbus** | `qwen` (exact id only) | Private vLLM instance; requires separate API key. Exact-match so the broader `qwen…` family still goes to NRP |

Unknown *bare* ids fall back to NRP (`default_provider`); unknown *namespaced* ids (containing `/`)
fall back to OpenRouter (`namespaced_default_provider`) and never to NRP, which serves no
slash-namespaced ids. NRP's own ids are deliberately **not** enumerated in
`config.json` — they all reach NRP through that fallback, so a new NRP model needs no config change.
You only list a model to route it somewhere *other* than the default. See
[`models` vs `model_prefixes`](#models-vs-model_prefixes).

#### Claude goes through OpenRouter

There is no direct Anthropic provider (removed 2026-11-09, unused since September). Send the
OpenRouter id, e.g. `anthropic/claude-sonnet-5`. OpenRouter maps an OpenAI-style
`cache_control` breakpoint onto Anthropic's native prompt caching: send the system prompt as
content parts with `cache_control: {"type":"ephemeral"}`, and add `"usage": {"include": true}`
to see `cached_tokens` / `cache_write_tokens` in the response and logs. A bare `claude-*` id
falls back to NRP, which does not serve it.

### Model tiers and model settings

The proxy keeps curated model lists in `config.json`, one per tier: public apps, private (signed-in) apps, and the benchmark. Nothing is configured per app.

```json
"tiers": {
  "public":    {"default_model": "deepseek-v4-flash", "models": ["deepseek-v4-flash", "qwen3", ...]},
  "private":   {"default_model": "deepseek/deepseek-v4.1-flash-1017",
                "models": ["deepseek/deepseek-v4.1-flash-1017", "deepseek-v4-flash", ...]},
  "benchmark-free": {"models": ["deepseek-v4-flash", "glm-5", ...], "auto": ["nimbus", ...]},
  "benchmark-paid": {"models": ["z-ai/glm-5.3-flash", ...],
                     "provider_overrides": {"openrouter": "openrouter-benchmark"}}
},
"models": {
  "deepseek-v4-flash": {"label": "NRP DeepSeek V4 Flash"},
  "z-ai/glm-5.2":      {"label": "GLM-5.2"}
}
```

- **How a key gets its tier.** Each client key (see [Client keys](#client-keys)) carries its tier as a prefix in the keys file: `public/tpl:<key>` or `private/tpl-internal:<key>`.
  - A key with no prefix is `public` (`default_key_tier`).
  - The legacy shared `PROXY_KEY` was retired on 2026-11-27 (#204): the NRP deployments no longer set it. A deployment that still sets it gives it `primary_key_tier`.
- **Requests.** A model outside the caller's tier gets a **403** that lists what the tier offers, and is logged with `provider: "disallowed"` and `tier`.
- **Request size limits (#164).** A tier's `limits` cap `body_bytes` (the request body), `input_chars` (text in `messages`, as logged; audio does not count), `messages` and `tools`. A tier inherits its parents' limits and can override each one; a limit no tier sets is not enforced. The body is checked before it is parsed, from `Content-Length` or, without one, while it is read. A request over a limit gets a **413** that names the limit and the tier, and is logged with `provider: "too-large"`. The numbers come from measured traffic (see #164): public 8 MB / 1M characters / 200 messages / 64 tools; private 8 MB / 3M / 400 / 64; benchmark tiers 16 MB / 4M / 1,500 / 64. The body limits leave room for about 3 minutes of voice input, which arrives as base64 WAV in the body.
- **The model list.** `GET /v1/models` returns the caller's tier list in order, flagging `default` on one entry and adding `default_model`. Entries carry `settings` from the `models` map. It doesn't fetch the upstream catalogs.
- **Context windows (#209).** Each list entry carries `context_length` (tokens) when it is known, outside `settings`. A model on a provider with `discover_models` takes it from the server's `max_model_len` (vLLM reports it); a `context_length` in the `models` map overrides that, and is the only source for NRP (its gateway lists ids only) and OpenRouter (its catalog is not fetched). Unknown windows are left out, not guessed. The NRP values were read on 2026-11-26 from the 400 that vLLM returns for an oversized `max_tokens`; `gpt-oss` accepted that request and has none.
- **The transcription model (#207).** A tier's `transcription_model` is the model a client sends voice input to. `GET /v1/models` names it beside `default_model`. The tier may call it even when it is not in `models`, and it is not listed among the chat models. It is inherited through `extends`.
- **Speech-to-text providers (#207).** A provider with `"api": "audio_transcriptions"` is an OpenAI-style `/v1/audio/transcriptions` server (whisper-cirrus runs speaches). A client calls it like any chat model, with the audio as an `input_audio` content part; the proxy uploads the audio and answers with the transcript as a chat completion. A call with no audio gets a 400. The recording is never logged: request rows carry `[input_audio wav, N bytes]` in its place.
- **Why public is mostly lab-hosted.** Public apps are open relays: anyone can POST to their `/api/llm/`. That's how the November 2026 prober reached the proxy. So the public list holds NRP- and DSE-hosted models, plus one paid model, `deepseek-or` (OpenRouter's DeepSeek V4 Flash 1017 checkpoint), which runs on its own OpenRouter key with a daily credit limit (`openrouter-public`, see [Fallback models](#fallback-models)). Once the limit is spent, or on a deployment without that key, its calls go to NRP's `deepseek-v4-flash` and then DSE's. Never put a model on the main `OPENROUTER_KEY` in the public tier.
- **The private list is short on purpose.** Private apps get OpenRouter's DeepSeek V4 Flash 1017 checkpoint (the default, with OpenRouter's default price-weighted provider routing), the same model with `:nitro` (OpenRouter sorts its providers by throughput, at a higher price), and NRP's `deepseek-v4-flash` and `gemma` and DSE's `deepseek-nimbus`. It does not extend public.
- **`extends`.** A tier that extends another (or a list of others) gets the parent's models first, in order, then its own. It inherits the parent's `default_model` and `transcription_model` unless it sets its own. Unknown parents and cycles stop the proxy at startup.
- **Adding a model.** To offer a new model to every private app, add one line to `tiers.private.models`. Private does not inherit public, and the benchmark tiers inherit neither.
- **`provider_overrides`.** A tier can map a provider to another for its own calls, e.g. `{"openrouter": "openrouter-benchmark"}`: the same model ids, billed to a different key. Routing is otherwise by model id alone, so this is the only way for two tiers to reach one id on different keys. It is not inherited through `extends`.
- **`upstream_timeout_seconds`.** A tier can set its own budget for each upstream call, in place of the deployment's `UPSTREAM_TIMEOUT_SECONDS` (see [No streaming](#no-streaming-by-design)). Both benchmark tiers set 14400 (4 hours, #190) to match glen-benchmark's per-call budget; apps cap their own calls far lower, so they keep the 20-minute default. It is inherited through `extends`, and only `read` changes.
- **Adding a tier.** Add a block to `tiers`, usually extending an existing one, and give a key the prefix `<tier>/` in the keys file. Add budget limits for it under `budgets` if it needs any; a tier with none has no limit.
- **Model settings.** The top-level `models` map holds settings for each model id that a client applies: `label`, `temperature`, `top_p`, `seed`, `max_tokens`, `llm_timeout_seconds`, `reasoning_toggle`, `reasoning_default` and `prompt_cache`, with the meanings of glen's `llm_models` fields. `GET /v1/models` returns them as `settings` on each entry. They are defaults for the client, not limits: an app's own `llm_models` entry overrides them, and the proxy enforces nothing from them. Other keys pass through with a startup warning. Limits the proxy does enforce stay on the provider, as below.
- **The benchmark tiers (#175).** glen-benchmark runs free and paid models on separate keys, so a run's spend is always deliberate and attributable.
  - `benchmark-free` holds only NRP and DSE models, including the NRP judges. The `benchmark-free/glen-benchmark` key uses it.
  - `benchmark-paid` holds the paid OpenRouter models, for the `benchmark-paid/glen-benchmark-paid` key. Its `provider_overrides` sends its OpenRouter calls to `openrouter-benchmark`, a second OpenRouter key with its own credit limit (`OPENROUTER_BENCHMARK_KEY`, optional Secret `openrouter-benchmark-key`). The same ids called from private still use the main key. Without the benchmark key, its calls fail with a 500 that names the missing key; they never reach the main key.
  - glen-benchmark's registry is `suite/models.yaml` and `suite/judges.yaml` in that repo, which can't edit this one, so screening a new candidate is a one-line PR adding it to `tiers.benchmark-free.models` or `tiers.benchmark-paid.models`. That does not offer it to any app. Neither tier has budget limits, because a 429 midway through a matrix loses cells and the keys work only in-cluster.
- **Other deployments.** With no `tiers` key, every routable model is allowed. cirrus mounts its own config and is unaffected.

**Served-model discovery and automatic tiers (#194).** Our own vLLM servers change which model they serve. A provider with `"discover_models": true` is asked what it serves every `DISCOVERY_INTERVAL_SECONDS` (default 60), and once before the proxy takes traffic. It is on for NRP and the DSE servers, and must never be on for OpenRouter or another paid aggregator.

```json
"tiers": {
  "public": {
    "models": ["deepseek-v4-flash", "glm-5", "kimi", "deepseek-nimbus"],
    "auto": ["nimbus", "nimbus2", "gemma4-nimbus"]
  }
}
```

- **Automatic or manual, per provider.** A tier's `auto` providers contribute whatever they serve right now; when vllm-nimbus switches models, the tier follows with no config edit. Any other provider is manual: the tier gets only the ids in `models`, which is how public takes a subset of NRP. `auto` may name only providers with `discover_models` on, or the proxy stops at startup. It is inherited through `extends`.
- **Routing.** An id a discovering provider serves routes to it, after the exact ids in `config.json` and before prefixes. If two serve the same id, the first in config order gets it and the startup log says so; keep them apart with `model_aliases`.
- **Models not being served.** A listed model that its provider is known not to serve right now is left out of `GET /v1/models`, unless one of its `fallbacks` is served. A call for it goes straight to that fallback (logged with `fallback_from` and `"error": "not currently served"`), or gets a **404** that names what the provider does serve, logged with `provider: "unavailable"`.
- **Never answered.** Until a provider's list has been fetched once, nothing about it is hidden or refused; if it stops answering, its last list is kept. `GET /health` shows each provider's current list as `serving`.
- **Reasoning levels (#214).** Clients may send `reasoning_level`: `none`, `low`, `medium` or `high`, the same words for every model. Each provider's `reasoning_levels` table, next to `thinking_models`, says what each level means for each model it serves, as `reasoning_effort` and/or `enable_thinking`. `/v1/models` lists `settings.reasoning_levels` from the listed route's table, and the default `settings.reasoning_level` from the `models` map when that route can translate it; a model with levels has no `reasoning_toggle`. Precedence: a client's level is translated; a raw `reasoning_effort` or `enable_thinking` overrides the translation and turns the default off; the model's default applies only when a client sends none of the three. A level the model's route can't translate is a **400** that lists its levels (logged as `bad-reasoning-level`); a fallback that can't translate it is skipped. Every table ships empty until the owner approves entries from the reasoning tests.
- **Settings.** A discovered model has no `thinking_models` entry or `models` settings until one is added, so add those when a new model arrives that needs a thinking switch or a label.

**Output caps.** Paid providers also cap output tokens (#150). `default_max_tokens` is sent when the client sends no `max_tokens`, and `max_tokens_ceiling` clamps a larger client value. Both are set per provider (32,768 on `openrouter` and `anthropic`). They are not set on NRP or nimbus, because vLLM rejects a request whose prompt plus `max_tokens` exceeds the model's context.

### Token budgets and the deny-list

**Budgets (#168).** The proxy counts output (completion) tokens over a rolling 4-hour window. Each counter is checked against its tier's limit in `config.json` `budgets`. A tier with no entry, such as `benchmark-free`, has no limit:

| Counter | Identifies | Public | Private |
|---|---|---|---|
| `per_key` | an app or job (a named client key, #169) | 4M | 4M |
| `per_client` | a visitor's address (`X-Real-IP`) | 1M | 1M |
| `per_user` | a signed-in user (`X-Forwarded-Email`) | 1M | 1M |

- **Output tokens only** (`"count": "completion"`). Prompt tokens mostly measure how long a conversation has run, because geo-agent resends the whole history on every call, while output is what loads NRP. Set `"count": "total"` to charge prompt + completion, which is the default when `count` is absent.
- **The window** is 4 hours (`window_hours`), counted in hourly buckets.
- **Over budget:** the request gets a **429** with `Retry-After`, saying which budget was hit and when it starts freeing up. It is logged with `provider: "budget-exceeded"`.
- **Address and user are counted separately,** so a forged email header can't escape the address limit.
- **The numbers** come from 2026-11-10, when one person on tpl and tpl-ca hit the old 3M total-token limit in 48 minutes: 2.94M prompt tokens but only 71k output tokens over 5 sessions. The public limits were first set at about 3 times that person's output. On 2026-11-16 they were raised 4-fold, to the private limits, after testing glen v4 from one JupyterHub pod spent 267k output tokens in 4 hours on NRP's deepseek-v4-flash. Every public-tier model runs on NRP or nimbus at no cost, so the public budgets guard NRP's capacity against a runaway client, not spending. Revisit them with more data.

**The counter store.** Counters live in Redis (`redis.yaml`, password `redis-password` in `open-llm-proxy-secrets`), because 3 pods × 4 workers must count against one total.
- **Without `BUDGET_REDIS_URL`,** each process keeps its own counters, which would allow 12 times the budget. A warning is printed at startup.
- **If Redis errors,** requests are allowed, so a budget outage never takes the apps down, and the error is logged.

**The deny-list.** ConfigMap `open-llm-proxy-denylist`, key `denylist`, one entry per line:

```
key tpl                    # a client key, by name
client 529b5c72100fa917    # an address, as logged in client_ip
user 3f0c9a1b2c3d4e5f      # a signed-in user, as logged in user
```

```bash
kubectl -n biodiversity create configmap open-llm-proxy-denylist \
  --from-file=denylist=denylist.txt --dry-run=client -o yaml | kubectl apply -f -
```

- **Taking effect:** the proxy re-reads the file when it changes, usually within a minute, with no restart.
- **What a blocked caller sees:** a bare 403, logged with `provider: "blocked"`.
- **Finding an entry:** use the hashes as they appear in the logs. The same salt hashes the address and the user, so the raw values are never needed.

### Fallback models

When the backend for a model refuses a call, the proxy retries it on substitutes named in `config.json` `fallbacks`, in order. A value is one model or a list:

```json
"fallbacks": {
  "deepseek-v4-flash": "deepseek-nimbus",
  "deepseek-or": ["deepseek-v4-flash", "deepseek-nimbus"]
}
```

- **What moves on:** a status in the refusing provider's `fallback_statuses` (default 429, 500, 502, 503 and 504), or no connection at all. Any other 4xx is the request's own fault and is returned. A read timeout is returned too, because the caller has already waited.
- **The list is the whole chain.** A substitute's own `fallbacks` entry is not followed.
- **Skipped substitutes:** one outside the caller's tier, one that routes nowhere, or one whose provider has no API key.
- **Spending caps.** An OpenRouter key with a credit limit refuses calls once the limit is spent, with a 402 or 403. Give that key its own provider and add those statuses to its `fallback_statuses`, and callers move on to NRP or DSE instead of seeing the error. The shipped config does this for the public tier's DeepSeek:

  ```json
  "openrouter-public": {
    "endpoint": "https://openrouter.ai/api/v1/chat/completions",
    "api_key_env": "OPENROUTER_PUBLIC_KEY",
    "models": ["deepseek-or"],
    "model_aliases": {"deepseek-or": "deepseek/deepseek-v4.1-flash-1017"},
    "model_prefixes": [],
    "fallback_statuses": [402, 403, 429, 500, 502, 503, 504],
    "max_tokens_ceiling": 32768
  }
  ```

  The alias keeps this key apart from the private tier's `deepseek/deepseek-v4.1-flash-1017`, which stays on `OPENROUTER_KEY`. The cap is set on the key at OpenRouter (a $5 limit that resets daily); the proxy's token budgets still apply per tier, whichever backend answers. The key lives in the optional Secret `openrouter-public-key` (field `OPENROUTER_PUBLIC_KEY`). Without it, a model whose provider has no key goes straight to its fallbacks, logged with `fallback_from.error: "no API key"`; a keyless model with no fallback still gets a 500 that names the missing key.
- **Logs:** the response row names the backend that answered. `fallback_from` holds the first refused attempt, and `fallback_attempts` lists every refused attempt when there was more than one.

### Thinking mode

Set `"enable_thinking": true` in the request body to activate extended reasoning on supported models (`kimi`, `qwen3`, `glm-5`). The proxy injects the correct provider-specific parameter based on `config.json`.

### No streaming (by design)

The proxy is **non-streaming**: it buffers each upstream completion so it can log the
request/response pair, repair leaked tool-call dialect, and account tokens from the
finished body. `"stream": true` is rejected with a **400** rather than silently ignored
(#129) — previously the field was dropped and the client got a 200 carrying a
*non-streaming* body, which an SSE-expecting OpenAI client cannot parse.

Because nothing streams, every per-call deadline in the chain has to cover the whole
generation, not just time-to-first-token. A caller fronted by an nginx sidecar will see a
502 on turns slower than *its own* `proxy_read_timeout` even when the proxy eventually
succeeds. That deadline is per-app, not a single fleet-wide number — `geo-agent-template`
runs 300s, `ca-30x30` 600s — and geo-agent's browser client aborts on its own
`llm_timeout_seconds` (600s default) regardless of the sidecar, so the effective deadline
is the tighter of the two.

Turns past that are flagged in pod logs as `⚠️ Slow completion` (#82), using
**`SLOW_COMPLETION_WARN_SECONDS`, default 300** — the tightest known sidecar, so the
warning over-reports rather than misses. Raise it per deployment to match that
deployment's own chain. It is only a reporting threshold: it caps nothing, and because
`latency_ms` is measured proxy-side, a flagged 200 means the client *may* have stopped
listening, not that it did — the warning narrows where to look, it does not confirm a
user-visible failure. The benchmark runner in
[glen-benchmark](https://github.com/boettiger-lab/glen-benchmark) has the same exposure.

The proxy's own budget for the upstream call is **`UPSTREAM_TIMEOUT_SECONDS`, default
1200** (20 minutes, #135). It was a hardcoded 600s, and that was the hop that actually
bit: over 60 days, 408 requests died at *exactly* ~600s (`nrp` 259, `nimbus` 143,
`vllm-cirrus` 6). Only the `read` phase gets the long budget — connect/write/pool stay at
30s, since a 20-minute TCP connect is a broken path, not a slow model.

One asymmetry worth knowing: httpx's `read` timeout bounds the wait for a *chunk*, not the
whole response. A self-hosted vLLM emits nothing until generation completes, so the budget
acts as a hard total cap there; OpenRouter sends keepalive bytes that keep resetting it,
which is why only OpenRouter has logged successes past 1700s.

Raising it does not lift every ceiling — the lowest hop wins. For nimbus- and
cirrus-backed models, Traefik's `responseHeaderTimeout: 600s` in `boettiger-lab/k8s` is
still the binding cap (boettiger-lab/k8s#42).

A tier can replace the budget for its own calls with `upstream_timeout_seconds`. The two
benchmark tiers set 4 hours (#190), since glen-benchmark's runner gives each call that long
and calls the Service directly, with no app sidecar in between. The provider-side hops
above still apply to those calls.

## Logging

Every LLM call produces two JSONL log entries (a `request` on arrival and a `response` on completion) linked by `request_id`.

Logs are written to **pod stdout** immediately and flushed to **S3** every 60 seconds (`FLUSH_INTERVAL`) as dated JSONL chunk files, plus a final flush on shutdown so a graceful restart does not drop the buffer. The NRP deployments take the bucket, endpoint and a key scoped to that bucket from the `rustfs-logs-write` Secret in their namespace (`LOG_BUCKET`, `AWS_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`). If no S3 credentials are present, logs go to stdout only.

```
logs-<ns>/
├── 2026-04-07/
│   ├── 02-00-05-39.jsonl
│   └── ...
```

Sync them with `./sync-logs.sh`, which uses the read-only `rustfs-logs-read` key, and query the local copy with DuckDB.

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

#### Client keys

Every caller should have its own named key: one per app, and one per person or job for out-of-cluster use (benchmarks, laptops). The proxy logs the key's **name** as `key_name` on every request and response row. It never logs the key. With named keys, one app's usage can be measured and one caller can be cut off without affecting the others.

Keys live in a separate Secret, one `name:key` per line:

```bash
# issue a key: append a line and re-apply
NEW=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
kubectl -n biodiversity get secret open-llm-proxy-client-keys -o jsonpath='{.data.keys}' | base64 -d > /tmp/keys 2>/dev/null || : > /tmp/keys
echo "public/tpl:$NEW" >> /tmp/keys      # tier/name:key
kubectl -n biodiversity create secret generic open-llm-proxy-client-keys \
  --from-file=keys=/tmp/keys --dry-run=client -o yaml | kubectl apply -f -
shred -u /tmp/keys
```

- **Revoking a key:** delete its line and re-apply the same way. The Secret is mounted as a file, and kubelet refreshes it in place, usually within a minute. The proxy re-reads the file when it changes, so no restart is needed.
- **Unknown or revoked keys:** both get the same 401.
- **Key names:** letters, digits, `.`, `_` and `-`. The optional `tier/` prefix sets the model tier.
- **Older forms:** `PROXY_KEY` (logged as `primary`; no longer set on NRP, #204) and `PROXY_KEYS_EXTRA` still work, as comma-separated `name:key` entries or bare keys. A bare key is logged as `unnamed-<hash prefix>`. Both need a restart to change.

The app's nginx forwards the visitor's address as `X-Real-IP`, and the proxy logs it as `client_ip`. By default it is a salted hash (`CLIENT_IP_LOG=hash`, salt from the `client-ip-salt` key of `open-llm-proxy-secrets`), which is enough to count and block one visitor without storing the address itself. With no salt set, nothing is logged. Set `CLIENT_IP_LOG=raw` to log the address itself, or `off` to log nothing.

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
# from a pod in the cluster (there is no public endpoint)
curl -H "Authorization: Bearer $CLIENT_KEY" http://open-llm-proxy.biodiversity.svc.cluster.local:8002/v1/models
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
"openrouter": { "models": [], "model_prefixes": ["z-ai/", "deepseek/", "~"] }
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

#### `namespaced_default_provider` (optional)

A `vendor/model` id matching no exact id or declared prefix goes here instead
of to `default_provider` (#154). It defaults to `openrouter` when that provider
is configured. OpenRouter's catalog is the authority for namespaced ids and
changes weekly, so curating a prefix list for it only produced silent misroutes
when a vendor was missing. Set it to `null` to require declared prefixes again.
With no usable value, an unmatched namespaced id is a **400**. It never falls
back to `default_provider`.

The upstream still decides whether the id exists: an id OpenRouter doesn't
serve comes back as OpenRouter's own error, relayed by the proxy.

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
kubectl apply -f deployment.yaml -n <your-namespace>
kubectl rollout status deployment/open-llm-proxy -n <your-namespace>
```

There is deliberately no Ingress. Apps in the cluster call the Service, and a browser never talks to the proxy directly. If you must expose one, `archive/ingress.yaml` is the old NRP manifest; read its header first.

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

## Health check

```
GET https://<your-host>/health
```

Returns provider configuration status, the number of accepted client keys (`client_keys`), and whether the legacy `PROXY_KEY` is set.
