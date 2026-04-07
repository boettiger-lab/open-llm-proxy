# open-llm-proxy Agent Instructions

## Purpose

This is an LLM proxy service that routes chat completion requests to NRP, OpenRouter, or Nimbus providers and logs every request/response pair. The primary analysis task for agents is evaluating those logs.

## Evaluating Logs

Logs live in two places: pod stdout (ephemeral) and S3 bucket `logs-open-llm-proxy` (persisted as JSONL chunks, preferred).

**Query logs with DuckDB** (external endpoint):

```sql
CREATE SECRET (TYPE S3, ENDPOINT 's3-west.nrp-nautilus.io', USE_SSL 'TRUE', URL_STYLE 'path');
SELECT * FROM read_ndjson_auto('s3://logs-open-llm-proxy/2026-04-07/*.jsonl');
```

Each LLM call produces a `request` entry and a `response` entry linked by `request_id`. Key fields:

- **Request**: `user_question`, `tool_results_this_turn`, `model`, `origin`, `message_count`
- **Response**: `tool_calls`, `content_preview`, `tokens`, `latency_ms`, `error`

**Reconstructing a conversation**: group by `user_question`, filter by `origin`, sort by `timestamp`. The `tool_results_this_turn` on each request shows what the previous turn's tool calls returned; `tool_calls` on each response shows what the LLM called next.

See [LOGGING.md](LOGGING.md) for full field reference, SQL patterns, kubectl access, and session reconstruction examples.
