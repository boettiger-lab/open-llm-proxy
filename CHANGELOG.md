# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [Releases](README.md#releases) for how a release is cut.

## [Unreleased]

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
