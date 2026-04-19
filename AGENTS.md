# open-llm-proxy Agent Instructions

## Purpose

This is an LLM proxy service that routes chat completion requests to NRP, OpenRouter, or Nimbus providers and logs every request/response pair. The primary analysis task for agents is evaluating those logs.

## Evaluating Logs

Logs land in three tiers by age (see [LOGGING.md](LOGGING.md) for the full spec):

- **Today**: raw JSONL at `s3://logs-open-llm-proxy/YYYY-MM-DD/*.jsonl` (sub-minute freshness)
- **Historical**: Parquet at `s3://logs-open-llm-proxy/consolidated/**/*.parquet` — daily files roll up into monthly files on day 2 of the following month
- **kubectl**: `kubectl -n biodiversity logs deployment/open-llm-proxy -f` for live tail of the last few seconds before the next S3 flush

**Default workflow: sync once, then query locally.** The bucket is private, but `rclone` is already configured with the `nrp` remote. Running `./sync-logs.sh` pulls the whole bucket (~a few MiB, ~1s) to `/tmp/open-llm-proxy-logs/`. Subsequent DuckDB queries read local files — no `CREATE SECRET`, no credential expansion in chat, no S3 round-trip per query. Re-run `./sync-logs.sh` any time you need fresher data; it only transfers what changed.

```bash
# One-time per session (or whenever you want to refresh)
./sync-logs.sh

# Historical: consolidated Parquet (the common case)
duckdb -s "
SELECT ts, entry::JSON->>'user_question' AS q, entry::JSON->'tool_calls' AS tools
FROM read_parquet('/tmp/open-llm-proxy-logs/consolidated/**/*.parquet')
WHERE origin = 'https://tpl.nrp-nautilus.io' AND ts > now() - INTERVAL 7 DAYS
ORDER BY ts DESC;
"

# Today: raw JSONL — narrow the glob to the current hour when possible
duckdb -s "
SELECT * FROM read_ndjson_auto('/tmp/open-llm-proxy-logs/YYYY-MM-DD/*.jsonl',
                               union_by_name=true);
"
```

**Parquet schema** (same for daily and monthly tiers): `(ts TIMESTAMPTZ, type, request_id, origin, entry VARCHAR)` — `entry` is the full original log record as JSON text. Access fields with `entry::JSON->>'field'`.

For automation, one-shot CI queries, or queries that need sub-minute freshness without re-syncing, query S3 directly with `LOG_S3_KEY` / `LOG_S3_SECRET` from the shell — see [LOGGING.md](LOGGING.md#direct-s3-one-shot-queries-automation-or-inside-nrp-pods).

Each LLM call produces a `request` row and a `response` row linked by `request_id`. Key fields inside `entry` (Parquet) or as top-level columns (JSONL):

- **Request**: `user_question`, `tool_results_this_turn`, `model`, `origin`, `message_count`
- **Response**: `tool_calls`, `content_preview`, `tokens`, `latency_ms`, `error` (only on failures)

**Reconstructing a conversation**: group by `user_question`, filter by `origin`, sort by `ts` (Parquet) / `timestamp` (JSONL). The `tool_results_this_turn` on each request shows what the previous turn's tool calls returned; `tool_calls` on each response shows what the LLM called next.

**Midnight crossover caveat**: flush-time (not entry `ts`) determines the source file path. An entry with `ts = 23:59:58` may live in the next UTC day's file if it was buffered past midnight. Always filter on `ts`, not on file path, when you care about a calendar day.

See [LOGGING.md](LOGGING.md) for full field reference, SQL patterns, kubectl access, session reconstruction examples, and the CronJob details.

When analyzing geo-agent app behavior (tool-call counts, query failures, session reconstructions), invoke the `geo-agent-training` skill — it provides the full step-by-step diagnostic workflow.
