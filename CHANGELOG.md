# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [Releases](README.md#releases) for how a release is cut.

## [Unreleased]

### Changed
- **Documented Claude prompt-caching routing (#75) — app selects the route by model id.**
  No code change: `anthropic/claude-*` already routes to OpenRouter (which maps the
  OpenAI-style `cache_control` breakpoint onto Anthropic's native param, so prompt
  caching lands), while bare `claude-*` routes to Anthropic's OpenAI-compat endpoint
  (which silently ignores `cache_control`). Verified end-to-end on
  `anthropic/claude-haiku-4.5`: a repeated ~6.8k-token cached prefix billed
  `cache_write_tokens` on the first call and `cached_tokens` on the second (~12× lower
  prefix cost). README's provider-routing section now spells out the two model ids
  side by side, the `anthropic/…` = "served by OpenRouter" naming gotcha, and the
  `"usage": {"include": true}` knob for surfacing cache accounting. Unblocks the
  geo-agent client half (per-model `prompt_cache: true`, already merged and off by
  default).
- **Nimbus (DSE) model renamed `nemotron` → `qwen`.** The `vllm-nimbus.carlboettiger.info`
  endpoint now serves `nvidia/Qwen3.6-35B-A3B-NVFP4` under the id `qwen` (was
  `nemotron`). Updated `config.json`'s `nimbus.models` to `["qwen"]` so the proxy
  (exact-match-then-prefix routing) forwards `model: "qwen"` to the nimbus endpoint;
  requests for `nemotron` no longer route anywhere. Requires a pod restart to take
  effect (config is git-synced at pod start).
- **Re-vendored `headless/mcp-client.js`** to match geo-agent upstream (#275 connect()
  race that could register zero MCP tools + reconnect-budget reset). `npm run
  check-drift` is clean again.
- **Headless runner resolves geo-agent via `GEO_AGENT_DIR` + `fresh-geoagent.sh`.**
  `run.js` now dynamic-imports the four geo-agent app modules from `GEO_AGENT_DIR`
  (default: the `../../geo-agent` sibling, so existing setups are unchanged). The
  new `headless/fresh-geoagent.sh` maintains an isolated `geo-agent@main` clone in
  a cache dir and prints its path, so `export GEO_AGENT_DIR="$(./fresh-geoagent.sh)"`
  gives a run its own pinned copy instead of depending on a shared dev checkout that
  other agents may be editing on branches. Only the app modules move; `mcp-client.js`
  stays vendored (bare-specifier resolution) and the script warns on drift.

### Added
- **New self-hosted provider `qwen3-cirrus`.** Adds the `qwen3-cirrus.carlboettiger.info`
  endpoint (qwen3 on the local k3s / cirrus host) to `config.json` under the model id
  `qwen3-cirrus` — distinct from nrp's `qwen3` and nimbus's `qwen` so the proxy's
  exact-match-then-prefix routing forwards it unambiguously. Reuses `NIMBUS_API_KEY`
  (same as the other `carlboettiger.info` vLLM endpoints) and is marked thinking-capable
  (`enable_thinking`). Requires a pod restart to take effect (config is git-synced at
  pod start).
- **Log the requested thinking mode `enable_thinking` (#64).** `log_request` now
  records `request.enable_thinking` — the mode the client **asked for** — alongside
  the existing response-side `has_reasoning_content`/`reasoning_content` (what the
  model actually **did**). Flattened to a typed `enable_thinking BOOLEAN` column
  (`null` = flag not sent / model default) in the consolidated Parquet schema and
  the per-turn session view; both cron jobs' re-flatten passes add the column to
  legacy files (as `null`) so the corpus stays on one schema. Disambiguates "reasoning
  off by request" from "model chose not to think" from "non-thinking model", making
  the effect of geo-agent's user-facing reasoning toggle (geo-agent#283) measurable
  from live traffic — not just the out-of-band headless A/B (#56/#60). See
  [LOGGING.md](LOGGING.md).
- **`ENABLE_THINKING` passthrough in the k8s matrix runner (#58).**
  `run-matrix-k8s.sh` now forwards an `ENABLE_THINKING` env (added to the export
  set, the `envsubst` allowlist, and the pod env in `matrix-job.yaml`), so a
  matrix sweep can pin reasoning on or off per pass. `run.js` (#56) turns it into
  the top-level `enable_thinking` flag, which the proxy maps to each model's
  `chat_template_kwargs` (qwen3/glm-5/kimi wired; gemma added in #57). Default
  `true` is behavior-preserving (reasoning-on is already the default; models
  without a `thinking_key` ignore it); the value is validated to `true`/`false`
  and never left empty (an empty-but-set value would read as an explicit `false`).
  Enables the two-pass reasoning ON/OFF assessment against the gold baseline (#58).

### Fixed
- **`temperature` no longer force-sent to models that reject it.** The proxy
  unconditionally injected `temperature` (default `0.0`) into every upstream
  payload, so the newest Anthropic models — Claude Sonnet 5, Opus 4.8/4.7, Fable 5
  — returned `400 "temperature is deprecated for this model"` (they removed the
  sampling params entirely). Added a per-provider `no_sampling_params` list of
  model IDs (config-driven, matched exact-then-prefix like routing, and populated
  for the `anthropic` provider); `temperature`/`top_p` are dropped for those models
  and left untouched for everything else, so the forced `temperature: 0.0`
  determinism default (#33) still holds for open models and older Anthropic models
  (`claude-sonnet-4-6`, `claude-haiku-4-5`) that still accept it. Requires a pod
  restart to pick up the config change. (Follow-up: the `PROVIDERS` builder copies
  a fixed key whitelist from `config.json`, so `no_sampling_params` also had to be
  added there — without it the request-time lookup always saw an empty list and the
  guard never fired.)
- **nimbus `qwen` ignored `enable_thinking`; its reasoning trace wasn't logged (#66).**
  Two fixes for the direct nimbus vLLM endpoint (`nvidia/Qwen3.6-35B-A3B-NVFP4`):
  (1) added `nimbus.thinking_models = {"qwen": "enable_thinking"}` to `config.json` —
  the block had no `thinking_models`, so `proxy_chat` dropped the client's
  `enable_thinking` flag (`no thinking_key configured — ignoring`) and the endpoint
  reasoned regardless. Verified against the endpoint: `chat_template_kwargs=
  {"enable_thinking": false}` suppresses the trace, `true` restores it. (2) `log_response`
  now reads `message.reasoning_content or message.reasoning` — nimbus emits the trace
  under `reasoning` (not `reasoning_content` like NRP), so `has_reasoning_content` /
  `reasoning_content` were empty for nimbus even when it clearly reasoned. Pairs with
  the request-side `enable_thinking` column (#64) to make requested-vs-observed reasoning
  analyzable for nimbus. Requires a pod restart (config git-synced at boot).
- **gemma/gemma-small-e4b `enable_thinking` was silently ignored (#57).** These
  NRP models support disabling reasoning via `chat_template_kwargs={"enable_thinking":
  false}`, but they were absent from `config.json`'s `nrp.thinking_models`, so
  `proxy_chat` found no `thinking_key` and dropped the client's top-level
  `enable_thinking` flag (logging `no thinking_key configured — ignoring`) — the
  toggle appeared to work client-side but had no effect. Added `gemma` and
  `gemma-small-e4b` with the `enable_thinking` key. Unblocks including gemma in the
  reasoning ON/OFF assessment (#58).

### Added
- **Capture upstream response headers on the error path (#44).** On
  `httpx.HTTPStatusError`, `proxy_chat` logged only the status code and (often
  empty) body, discarding the response headers that distinguish a genuine
  rate-limit (`429` + `retry-after`/`x-ratelimit-*`) from a dead-backend gateway
  failure (naked `500`, `content-length: 0`, no `server`/`x-request-id`). That
  distinction was previously only catchable live with `curl -i` and impossible to
  recover after the fact. Now an allow-listed subset (`retry-after`,
  `x-ratelimit-{limit,remaining,reset}`, `x-request-id`, `server`, `date`,
  `content-length`) is captured into the error response log under
  `entry.upstream_headers`, queryable via `json_extract(entry,'$.upstream_headers')`.
  Allow-list only (no full header bag); values pass through the scrubber for
  defense in depth. Only the `HTTPStatusError` branch has a response to read —
  the timeout/connection branches fail without one. Not promoted to a flat
  consolidated column (entry-JSON access suffices for occasional debugging).
- **Standing baseline question set for guidance-change regression testing (#40).**
  New `headless/baseline/`: 22 analytical questions with operator-verified golden
  answers + authoritative SQL (`gold/`), seeded from the open-model benchmark
  (`headless/experiments/2026-06-26-or-openmodel-bench`). `golden.json` tags each
  question with the **trap it guards** (the #42 rule-store key), an `accept` rule,
  and first-run difficulty (`bench_mean_acc`). This is the durable set the
  MCP-server guidance-change gate regresses against (per-question/instance-level,
  not aggregate; gold is operator-verified, never model consensus). Encodes the
  dev-MCP targeting requirement (validation must hit `dev-duckdb-mcp`, not prod).
  `build_golden.py` regenerates the manifest; grow-on-fix as new traps are found.
- **Forward sampling/routing knobs instead of dropping them (#47).** `proxy_chat`
  rebuilt the upstream payload from a hard whitelist (`model`/`messages`/
  `temperature` + `tools`), so any other client field was silently dropped before
  forwarding. Added `top_p`, `seed`, `stop`, `max_tokens`, and `response_format`
  to `ChatRequest` and forward each verbatim when present (non-None) on any
  provider. The OpenRouter-isms — the `provider` routing block (`zdr`/`order`/
  `only`/`require_parameters`, ...) and top-level `usage` (`{"include": true}`) —
  are guarded to `provider_name == "openrouter"`, since a strict OpenAI-compatible
  server (e.g. vllm) may reject them. This unblocks per-request `seed`/`top_p`
  determinism (geo-agent#266), provider steering for cache/cost, and per-request
  `provider.zdr`. `cache_control` inside
  message content blocks already passed through (the `messages` array is forwarded
  verbatim); the NRP `cache_salt` path is unchanged. Relates to geo-agent#273.

### Fixed
- **`config.json`: corrected two stale NRP model ids that 404'd at the gateway.**
  `ellm.nrp-nautilus.io`'s `/v1/models` no longer serves `glm-4.7` or
  `gemma-4-e4b`; requests for them returned `404 No matching route found`. Renamed
  to the currently-served ids `glm-5` and `gemma-small-e4b` (and updated the
  `thinking_models` key `glm-4.7` → `glm-5`).

### Added
- **OpenRouter: link `z-ai/`, `minimax/`, and `moonshotai/` model families.**
  Added these three vendor prefixes to `config.json`'s OpenRouter `models` list
  (and the in-code fallback + README provider table), so ids like `z-ai/glm-5.2`,
  `minimax/minimax-m3`, and `moonshotai/kimi-k2.7-code` route to OpenRouter
  instead of falling through to the NRP default. Enables an open-model
  performance/accuracy evaluation across these families. The proxy reads
  `config.json` from a fresh `git clone` of `main` at pod boot, so this reaches
  prod on the next `rollout restart`. (Also synced the stale `glm-4.6`→`glm-5`
  and missing `nvidia/` entries in the in-code fallback default.)
- **Headless matrix: `GEO_AGENT_BRANCH` to pin the geo-agent framework clone.**
  The matrix Job hard-coded a `main` clone of `boettiger-lab/geo-agent`, which
  supplies the framework (`Agent` / `DatasetCatalog` / `ToolRegistry` /
  `createMapTools`) the runner imports — so there was no way to evaluate a
  code-level geo-agent change before it merged and shipped in a pinned release.
  `run-matrix-k8s.sh` now accepts `GEO_AGENT_BRANCH` (default `main`) and the Job
  clones that branch, mirroring the existing `APP_BRANCH`. Run the matrix once on
  a fix branch and once on `main` to A/B a change (e.g. a tool-description
  variant) across the open model collection before pinning the fleet.
- **Query-ready consolidated logs: flattened columns + a materialized session
  view (#31).** The daily consolidation now promotes the hot fields
  (`session_id`, `client`, `provider`, `model`, `message_count`, `tools_count`,
  `user_question`, `latency_ms`, `has_tool_calls`, `has_content`, `tool_calls`,
  `tool_results`, `tokens`, `error`) to typed columns alongside the verbatim
  `entry` blob, removing the `entry::JSON->>` cast traps for common queries while
  staying backward-compatible (existing `entry`-based queries still work). A new
  `sessions/{daily,monthly}/` tier materializes one row per **turn** — request
  joined to its response, keyed on `session_key` (`session_id`, or an
  `anon:<hash>` fallback) and ordered by `turn_idx` — so reconstructing a session
  ("every turn of X in order, with tool calls and results") is a single flat
  `SELECT` with no manual request/response interleaving. The daily job backfills
  session views for already-consolidated days that lack one; the monthly rollup
  rebuilds the view over the whole month (correct cross-midnight `turn_idx`) and
  reads daily files with `union_by_name=true` so a month mixing legacy
  (entry-only) and flattened daily files merges cleanly. Both jobs also run a
  **self-healing schema-upgrade pass** that re-flattens any legacy 5-column
  consolidated file in place from its preserved `entry` blob (lossless,
  idempotent), so the whole `consolidated/**` corpus converges to one schema and
  old logs gain the flat columns too — no mixed-schema barrier for analysts.
  Existing `entry`-based queries were never at risk (DuckDB name-matches common
  columns across a mixed glob). `LOGGING.md` / `AGENTS.md` document both schemas,
  the `sessions/**` ⟂ `consolidated/**` glob split, and the mixed-glob caveat.

### Fixed
- **Response logging restored (#37).** Since the #26 (X-Client) deploy, the
  `async with httpx.AsyncClient(...) as client` block shadowed the `client`
  X-Client header parameter, so `log_response(..., client=client)` passed the
  httpx client object; `json.dumps` then raised inside the `@_never_raises`
  wrapper and **every response was silently dropped from S3** (requests were
  unaffected — they log before the block). Renamed the context var to
  `http_client`. Added a handler-level regression test that drives `proxy_chat`
  with a mocked upstream and asserts a serializable `type: "response"` entry
  lands in the buffer.

## [0.1.0] - 2026-06-24

First tagged release. The proxy has run in production (`biodiversity` namespace,
`https://open-llm-proxy.nrp-nautilus.io`) since 2026-02-26; this release captures
that accumulated state as a baseline and starts tracking changes going forward.

### Added
- Multi-provider routing for `/v1/chat/completions` across NRP, OpenRouter,
  Nimbus, and a direct Anthropic (OpenAI-compatible) provider, selected by model
  name (#21).
- `session_id` is now populated from the OpenAI `user` request-body field
  (falling back to an `X-Session-Id` header), giving every log an exact
  session key instead of the lossy `(origin, user_question)` heuristic. `user`
  is logged only, never forwarded upstream (#31, #34).
- `X-Client` request header captured into logs to correlate behavior with a
  client release (#26).
- Training-grade logging: full response `content`/`reasoning_content`, full
  `tool_calls`, capture modes (`summary`/`full`), per-field caps, system-prompt
  dedup, and always-on credential scrubbing (#25).
- Tiered S3 log storage: raw JSONL → daily Parquet → monthly Parquet via
  consolidation CronJobs; `sync-logs.sh` for local-first analysis (#11, #15).
- `headless/` session-replay runner that imports geo-agent live, plus k8s Job
  driver for model × question matrix sweeps (#17, #18, #19).
- One-off `scrub-historical-logs.py` job to redact leaked credentials from
  pre-scrubbing Parquet in place (idempotent).
- `geo-agent-training` skill and `duckdb-geo` MCP server config (#22, #23).

### Changed
- Default `temperature` is `0.0` (was `0.7`) (#33).
- S3 log flush interval reduced from 300s to 60s (`FLUSH_INTERVAL`).
- Provider error truncation raised 200 → 1000 chars for debugging.
- Scaled to 3 replicas with collision-safe flush keys and HA scheduling.

### Fixed
- Re-queue buffered log entries on flush failure instead of dropping them (#27).
- DNS resilience: per-pod CoreDNS caching sidecar with `serve_stale`, plus
  `ndots`/`attempts` tuning to curb upstream `EAI_AGAIN` 502s (#29, #30).
- `boto3` added to `requirements.txt` — it was a runtime + test dependency
  missing from CI, which had been red since #27.

### Infrastructure notes
- CORS is enforced at the haproxy **ingress**, not the app — custom request
  headers must be added to `cors-allow-headers` in `ingress.yaml` (#26).
- Pods pull application code by cloning `main` at startup (init container); a
  rollout is `kubectl rollout restart`, no image build.

[Unreleased]: https://github.com/boettiger-lab/open-llm-proxy/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/boettiger-lab/open-llm-proxy/releases/tag/v0.1.0
