# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [Releases](README.md#releases) for how a release is cut.

## [Unreleased]

### Added
- **Flat consolidated log schema + materialized session view (#31).** Daily/monthly
  consolidation now promotes hot fields (`model`, `provider`, `session_id`,
  `client`, `message_count`, `user_question`, `latency_ms`, `has_*`,
  `total_tokens`, `tool_calls`/`tool_results`/`tokens` as JSON, `error`) to typed
  columns alongside the raw `entry` blob — no more `entry::JSON->>` casting traps.
  A new `sessions/**` Parquet tree holds one interleaved row per turn keyed on
  `session_id` (heuristic fallback for pre-#34 nulls), so a whole conversation is
  `SELECT … WHERE session_key = ? ORDER BY turn_idx` with no manual request/response
  matching. Consolidation logic is now a single source of truth in `consolidate.py`
  (`daily`/`monthly`/`backfill`), git-cloned by the CronJobs.
- One-off `flatten-historical-logs-job.yaml` to backfill the flat schema + session
  views onto pre-#31 consolidated Parquet in place (idempotent, row-count-checked).
- `test_consolidate.py` pins the flatten/session-view SQL (schema, turn pairing,
  ordering, heuristic key fallback, full-mode `latest_user_message`).

### Changed
- Consolidation CronJobs (`consolidate-{daily,monthly}-cronjob.yaml`) switched from
  inline-heredoc Python to git-cloning the repo and running `consolidate.py`,
  matching the scrub job — keeps the flatten SQL from drifting across entrypoints.

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
