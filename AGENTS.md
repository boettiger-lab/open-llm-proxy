# open-llm-proxy Agent Instructions

## Purpose

This is an LLM proxy service that routes chat completion requests to NRP, OpenRouter, or Nimbus providers and logs every request/response pair. The primary analysis task for agents is evaluating those logs.

## Evaluating Logs

Logs live in two places: pod stdout (ephemeral) and S3 bucket `logs-open-llm-proxy` (persisted as JSONL chunks, preferred).

The bucket is private. Agents should assume `LOG_S3_KEY` and `LOG_S3_SECRET` are pre-set in the shell and use those env vars via the Bash tool — the shell expands them at execution time so the values never appear in chat. Do not hunt for credentials in rclone config, k8s secrets, etc.

```bash
duckdb -s "
CREATE SECRET logs_s3 (TYPE S3, KEY_ID '$LOG_S3_KEY', SECRET '$LOG_S3_SECRET',
  ENDPOINT 's3-west.nrp-nautilus.io', USE_SSL true, URL_STYLE 'path');
SELECT * FROM read_ndjson_auto('s3://logs-open-llm-proxy/2026-04-07/*.jsonl', union_by_name=true);
"
```

Narrow the S3 glob to an hour (`2026-04-07/17-*.jsonl`) when you only need recent traffic.

Each LLM call produces a `request` entry and a `response` entry linked by `request_id`. Key fields:

- **Request**: `user_question`, `tool_results_this_turn`, `model`, `origin`, `message_count`
- **Response**: `tool_calls`, `content_preview`, `tokens`, `latency_ms`, `error` (sparse — only present in files that contain failures; omit from JOIN queries or use `TRY(resp.error)`)

**Reconstructing a conversation**: group by `user_question`, filter by `origin`, sort by `timestamp`. The `tool_results_this_turn` on each request shows what the previous turn's tool calls returned; `tool_calls` on each response shows what the LLM called next.

See [LOGGING.md](LOGGING.md) for full field reference, SQL patterns, kubectl access, and session reconstruction examples.

When analyzing geo-agent app behavior (tool-call counts, query failures, session reconstructions), invoke the `geo-agent-training` skill — it provides the full step-by-step diagnostic workflow.
