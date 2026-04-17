# open-llm-proxy Agent Instructions

## Purpose

This is an LLM proxy service that routes chat completion requests to NRP, OpenRouter, or Nimbus providers and logs every request/response pair. The primary analysis task for agents is evaluating those logs.

## Evaluating Logs

Logs live in three surfaces: **Postgres** (primary, 1-year retention, sub-second freshness), **pod stdout** (live tail / fallback), and **S3 Parquet** (monthly cold archive, forever). Query Postgres for anything current; reach for S3 Parquet only when spanning more than a year back.

Agents should assume `LOG_DB_READ_URL` is pre-set in the shell (DSN for the read-only role) and use it via the Bash tool — the shell expands it at execution time so the password never appears in chat. Do not hunt for credentials in k8s Secrets.

```bash
psql "$LOG_DB_READ_URL" -c "
  SELECT ts, entry->>'user_question' AS q, entry->'tool_calls' AS tools
  FROM logs
  WHERE origin='https://tpl.nrp-nautilus.io' AND ts > now() - INTERVAL '1 hour'
  ORDER BY ts DESC;
"
```

Or with DuckDB when you need joins / analytical queries:

```bash
duckdb -s "
INSTALL postgres; LOAD postgres;
ATTACH '$LOG_DB_READ_URL' AS pg (TYPE postgres, READ_ONLY);
SELECT req.ts, req.entry->>'user_question' AS q, resp.entry->'tool_calls' AS tools
FROM pg.public.logs req
JOIN pg.public.logs resp ON req.request_id = resp.request_id
WHERE req.type='request' AND resp.type='response'
  AND req.ts > now() - INTERVAL 20 MINUTES;
"
```

Each LLM call produces a `request` row and a `response` row linked by `request_id`. The full payload lives in `entry` (JSONB). Key fields inside `entry`:

- **Request**: `user_question`, `tool_results_this_turn`, `model`, `message_count`
- **Response**: `tool_calls`, `content_preview`, `tokens`, `latency_ms`, `error` (only on failures)

**Reconstructing a conversation**: group by `entry->>'user_question'`, filter by `origin`, sort by `ts`. The `tool_results_this_turn` on each request shows what the previous turn's tool calls returned; `tool_calls` on each response shows what the LLM called next.

See [LOGGING.md](LOGGING.md) for full field reference, SQL patterns, kubectl access, S3 Parquet archive format, and session reconstruction examples.

When analyzing geo-agent app behavior (tool-call counts, query failures, session reconstructions), invoke the `geo-agent-training` skill — it provides the full step-by-step diagnostic workflow.
