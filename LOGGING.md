# LLM Proxy Logging

## Where logs live

Logs are written to two places:

1. **Pod stdout** — available immediately via `kubectl`, lost on pod restart
2. **S3 bucket `logs-open-llm-proxy`** — flushed every 60 seconds (configurable via `FLUSH_INTERVAL` env var) as JSONL chunk files, persisted indefinitely

### S3 layout

```
logs-open-llm-proxy/
├── 2026-03-31/
│   ├── 02-00-05-39.jsonl     # flush at 02:00:05 from worker PID 39
│   ├── 02-05-07-39.jsonl
│   └── ...
├── 2026-04-01/
│   └── ...
```

Each file is newline-delimited JSON (one log entry per line, mix of request and response entries).

## Access pattern

### S3 (preferred — no kubectl needed)

The bucket is **private**. Set `LOG_S3_KEY` and `LOG_S3_SECRET` in your shell (scoped keys for this bucket — distinct from your general NRP credentials) and let the shell expand them into DuckDB's `CREATE SECRET`. Agents should use the Bash tool so shell expansion keeps the secret values out of the conversation transcript.

```bash
duckdb -s "
CREATE SECRET logs_s3 (TYPE S3, KEY_ID '$LOG_S3_KEY', SECRET '$LOG_S3_SECRET',
  ENDPOINT 's3-west.nrp-nautilus.io', USE_SSL true, URL_STYLE 'path');

-- All logs for a date
SELECT * FROM read_ndjson_auto('s3://logs-open-llm-proxy/2026-03-31/*.jsonl', union_by_name=true);

-- Filter to one app
SELECT * FROM read_ndjson_auto('s3://logs-open-llm-proxy/2026-03-31/*.jsonl', union_by_name=true)
WHERE origin = 'https://padus.nrp-nautilus.io';

-- Pair requests and responses
SELECT req.user_question, req.timestamp, resp.tool_calls, resp.tokens
FROM read_ndjson_auto('s3://logs-open-llm-proxy/2026-03-31/*.jsonl', union_by_name=true) req
JOIN read_ndjson_auto('s3://logs-open-llm-proxy/2026-03-31/*.jsonl', union_by_name=true) resp
  ON req.request_id = resp.request_id
WHERE req.type = 'request' AND resp.type = 'response';
"
```

Narrow the glob to an hour (`2026-03-31/14-*.jsonl`) when you only need recent traffic. `union_by_name=true` handles schema drift across chunks (e.g. the `error` column appears only in some files).

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
RESPONSE tool_calls=[{name: get_dataset_details, arguments: {dataset_id: "pad-us-4.1-fee"}}]

REQUEST  msg=6  user_question="Tell me about datasets"  tool_results=[{content: "{columns: [...]}"}]
RESPONSE has_content=true  content_preview="The PAD-US dataset contains..."
```

Note: `list_datasets` and `get_dataset_details` are local geo-agent tools — their results appear in `tool_results_this_turn` on the proxy but never reach the MCP server.

## What the MCP server logs add

The DuckDB MCP server logs SQL execution separately. MCP logs are only needed for:
- SQL execution errors not visible to the LLM (failed queries that return an error string)
- Exact query timing at the database layer

For conversation-level analysis (what users asked, what the LLM decided, what tools returned), proxy logs are self-sufficient.
