# LLM Proxy Logging

## Where logs live

Logs are written to two places:

1. **Pod stdout** — available immediately via `kubectl`, lost on pod restart
2. **S3 bucket `logs-open-llm-proxy`** — flushed every 60 seconds (configurable via `FLUSH_INTERVAL` env var) as JSONL chunk files, persisted indefinitely

### S3 layout (tiered rollup)

Three tiers, each holding a different age of data. A daily CronJob rolls
raw JSONL into daily Parquet; a monthly CronJob rolls completed months
of daily Parquet into one monthly Parquet. At any moment:

```
logs-open-llm-proxy/
├── 2026-04-17/                        ← today: raw JSONL (live debug tier)
│   ├── 02-00-05-39.jsonl              # flush at 02:00:05 from worker PID 39
│   └── ...
├── consolidated/
│   ├── daily/
│   │   ├── 2026-04-15.parquet         # recent completed days
│   │   └── 2026-04-16.parquet
│   └── monthly/
│       ├── 2026-02.parquet            # older completed months
│       └── 2026-03.parquet
```

| Tier | Format | When it gets written | When it gets deleted |
|---|---|---|---|
| Raw JSONL (`YYYY-MM-DD/*.jsonl`) | JSONL chunks | Proxy flushes every 60s | Next day's daily consolidation (03:00 UTC) |
| Daily (`consolidated/daily/YYYY-MM-DD.parquet`) | Parquet (zstd) | Daily cron at 03:00 UTC | Monthly rollup on day 2 (04:00 UTC) |
| Monthly (`consolidated/monthly/YYYY-MM.parquet`) | Parquet (zstd) | Monthly cron on day 2 | Never (long-term archive) |

The **Parquet schema** (identical across daily and monthly tiers):

| Column | Type | Notes |
|---|---|---|
| `ts` | `TIMESTAMPTZ` | Extracted from the `timestamp` field of the raw JSON |
| `type` | `VARCHAR` | `'request'` or `'response'` |
| `request_id` | `VARCHAR` | Correlates request ↔ response |
| `origin` | `VARCHAR` | App the traffic came from |
| `entry` | `VARCHAR` | The full original JSON record — parse with `entry::JSON` |

## Access pattern

### S3 (preferred — no kubectl needed)

The bucket is **private**. Set `LOG_S3_KEY` and `LOG_S3_SECRET` in your shell (scoped keys for this bucket — distinct from your general NRP credentials) and let the shell expand them into DuckDB's `CREATE SECRET`. Agents should use the Bash tool so shell expansion keeps the secret values out of the conversation transcript.

```bash
duckdb -s "
CREATE SECRET logs_s3 (TYPE S3, KEY_ID '$LOG_S3_KEY', SECRET '$LOG_S3_SECRET',
  ENDPOINT 's3-west.nrp-nautilus.io', USE_SSL true, URL_STYLE 'path');

-- Historical: all consolidated data in one read (daily + monthly Parquet)
SELECT ts, entry::JSON->>'user_question' AS q
FROM read_parquet('s3://logs-open-llm-proxy/consolidated/**/*.parquet')
WHERE entry::JSON->>'origin' = 'https://tpl.nrp-nautilus.io'
  AND ts > now() - INTERVAL 7 DAYS;

-- Today's live data (raw JSONL — narrow the glob to an hour when possible)
SELECT * FROM read_ndjson_auto(
  's3://logs-open-llm-proxy/2026-04-17/*.jsonl', union_by_name=true);

-- Pair requests and responses from consolidated Parquet
SELECT req.ts, req.entry::JSON->>'user_question' AS q,
       resp.entry::JSON->'tool_calls' AS tools,
       (resp.entry::JSON->>'latency_ms')::INT AS ms
FROM read_parquet('s3://logs-open-llm-proxy/consolidated/**/*.parquet') req
JOIN read_parquet('s3://logs-open-llm-proxy/consolidated/**/*.parquet') resp
  ON req.request_id = resp.request_id
WHERE req.type = 'request' AND resp.type = 'response';
"
```

For queries that span today + history, UNION raw JSONL and consolidated Parquet with a shared projection (cast JSONL rows' `timestamp` to `TIMESTAMPTZ` and wrap the full row back into JSON if needed).

**Narrow the raw JSONL glob to the current hour** (e.g. `2026-04-17/14-*.jsonl`) — do NOT scan the whole day. `union_by_name=true` handles schema drift across chunks (e.g. the `error` column appears only in some files). If the queried window has zero error responses, `error` is absent from all files — omit it from JOIN queries or use `TRY(resp.error)`.

**Temporal filter:** `timestamp` is stored as VARCHAR (ISO8601). Cast to `TIMESTAMPTZ` (not `TIMESTAMP`) when comparing against `now()`:
```sql
CAST(req.timestamp AS TIMESTAMPTZ) >= now() - INTERVAL 20 MINUTES
```
Using `TIMESTAMP` causes a type mismatch binder error because `now()` returns `TIMESTAMPTZ`.

**Log field truncation:** `tool_results_this_turn[N].content` and `content_preview` are truncated to ~200 chars in logs. A tool result that appears to contain only column names likely has full descriptions below the truncation point — verify using the STAC MCP tools directly rather than inferring from log previews.

**Transient "malformed JSON" errors on raw JSONL:** Occasionally a `read_ndjson_auto` over today's directory will fail with `unexpected control character in string` at some byte offset. This is almost always a spurious partial byte-range read between DuckDB's httpfs extension and the Ceph gateway under high-parallelism scans — not actual bad data. The proxy writes with `json.dumps` (which escapes every control char) and S3 PUTs are atomic, so malformed lines on disk are not possible from the normal write path. Retry the query, or pass `ignore_errors=true` to `read_ndjson_auto` if you want a tolerant scan:
```sql
read_ndjson_auto('s3://.../YYYY-MM-DD/*.jsonl', union_by_name=true, ignore_errors=true)
```
Do not treat this as a proxy bug unless you can point at an actual byte with a raw control character in a file that's not being actively written.

Inside NRP pods, use the internal endpoint instead (`rook-ceph-rgw-nautiluss3.rook`, `USE_SSL false`) — faster and no public-endpoint throttling.

### kubectl (live logs / last ~60s before next flush)

```bash
# Live tail
kubectl -n biodiversity logs deployment/open-llm-proxy -f

# Recent history
kubectl -n biodiversity logs deployment/open-llm-proxy --tail=500

# Filter to a specific app
kubectl -n biodiversity logs deployment/open-llm-proxy --tail=1000 \
  | grep '"origin":"https://padus.nrp-nautilus.io"'
```

## Log format

Each LLM call produces two JSON entries on stdout: a `REQUEST` line when the call arrives and a `RESPONSE` line when it completes.

### REQUEST entry

```
📥 REQUEST: {"timestamp": "...", "type": "request", ...}
```

| Field | Description |
|---|---|
| `timestamp` | UTC ISO8601 |
| `type` | `"request"` |
| `request_id` | 8-char hex — correlates this request to its response |
| `provider` | `"nrp"`, `"openrouter"`, or `"nimbus"` |
| `model` | Model name as sent by the client |
| `origin` | Origin or Referer header — identifies which app sent the request |
| `message_count` | Total messages in the conversation at this turn |
| `tools_count` | Number of tools available to the LLM |
| `user_question` | First `role: user` message in the conversation — the human's original question, stable across all turns of a tool-use loop |
| `tool_results_this_turn` | Array of `{tool_call_id, content}` for any `role: tool` messages appended since the last assistant turn. Captures results from both local geo-agent tools (e.g. `list_datasets`, `get_dataset_details`) and remote MCP tools (e.g. `query`). `null` on the first turn. |

### RESPONSE entry

```
✓ RESPONSE: {"timestamp": "...", "type": "response", ...}
✗ RESPONSE: {"timestamp": "...", "type": "response", "error": "...", ...}
```

| Field | Description |
|---|---|
| `timestamp` | UTC ISO8601 |
| `type` | `"response"` |
| `request_id` | Matches the corresponding request entry |
| `provider` | Provider that handled the request |
| `model` | Model used |
| `origin` | Same as request — identifies which app |
| `latency_ms` | End-to-end latency in milliseconds |
| `has_content` | Whether the LLM returned text content |
| `has_tool_calls` | Whether the LLM made tool calls |
| `content_preview` | First 200 chars of text response |
| `tool_calls` | Array of `{name, arguments}` — full tool call arguments including SQL query strings |
| `tokens` | Token usage object from the provider (`prompt_tokens`, `completion_tokens`, `total_tokens`) |
| `error` | Error detail string (only present on failed requests) |

## Reconstructing a conversation

Each POST to `/v1/chat/completions` is one LLM turn. A single user session produces multiple request/response pairs. To reconstruct a session:

1. Match by `origin` to isolate one app
2. Group turns by `user_question` (same question = same session)
3. Sort by `timestamp`
4. Interleave: each request's `tool_results_this_turn` shows what the previous turn's tool calls returned; each response's `tool_calls` shows what the LLM decided to call next

Use `request_id` to pair each request with its response when log lines are interleaved under concurrent load.

## Example session reconstruction

```
REQUEST  msg=2  user_question="Tell me about datasets"  tool_results=null
RESPONSE tool_calls=[{name: list_datasets, arguments: {}}]

REQUEST  msg=4  user_question="Tell me about datasets"  tool_results=[{content: "[{id: pad-us...}]"}]
RESPONSE tool_calls=[{name: get_schema, arguments: {dataset_id: "pad-us-4.1-fee"}}]

REQUEST  msg=6  user_question="Tell me about datasets"  tool_results=[{content: "path: s3://... | column | sample..."}]
RESPONSE has_content=true  content_preview="The PAD-US dataset contains..."
```

Note: `list_datasets` and `get_schema` are local geo-agent tools — their results appear in `tool_results_this_turn` on the proxy but never reach the MCP server.

## What the MCP server logs add

The DuckDB MCP server logs SQL execution separately. MCP logs are only needed for:
- SQL execution errors not visible to the LLM (failed queries that return an error string)
- Exact query timing at the database layer

For conversation-level analysis (what users asked, what the LLM decided, what tools returned), proxy logs are self-sufficient.
