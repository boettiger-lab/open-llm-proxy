# LLM Proxy Logging

## Where logs live

Every LLM request/response pair is written to **three** surfaces. Each serves a distinct purpose:

| Surface | Purpose | Latency | Retention |
|---|---|---|---|
| **Postgres `logs` table** | Primary durable store; queryable | Sub-second | 1 year |
| **Pod stdout** | Live tail for active debugging | Real-time | Until pod restart |
| **S3 `archive/YYYY-MM.parquet`** | Cold archive; monthly rollup | End of month + ~3h | Forever |

The Postgres row **is** the log. Stdout is a realtime view and a safety net if Postgres is unreachable. S3 Parquet is the long-term archive so the Postgres PVC stays bounded.

## Postgres (primary access pattern)

### Credentials

There are two roles, both created at Postgres first-init:

- `log_writer` — `INSERT` only, used by the proxy itself (`LOG_DB_URL` secret)
- `log_reader` — `SELECT` only, used by developers / cron archive job

Agents and developers should have **`LOG_DB_READ_URL`** pre-set in their shell, pointing at the reader role:

```
postgresql://log_reader:<password>@<host>:5432/logs
```

Use the env var via the Bash tool so the password never appears in chat. **Do not** look up or copy the password out of `postgres-logs-auth` or any k8s Secret.

### Schema

```sql
CREATE TABLE logs (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    type       TEXT NOT NULL,            -- 'request' | 'response'
    request_id TEXT,
    origin     TEXT,
    entry      JSONB NOT NULL            -- the full payload
);
```

`entry` is the full JSON object — query into it with `entry->>'field'` (text) or `entry->'field'` (jsonb).

### psql (simple one-offs)

```bash
psql "$LOG_DB_READ_URL" -c "
  SELECT ts, entry->>'user_question' AS q, entry->'tool_calls' AS tools
  FROM logs
  WHERE type='response' AND origin='https://tpl.nrp-nautilus.io'
    AND ts > now() - INTERVAL '1 hour'
  ORDER BY ts DESC;
"
```

### DuckDB (joins, Parquet spillover, richer analysis)

DuckDB has a `postgres` extension that can query the live table directly:

```bash
duckdb -s "
INSTALL postgres; LOAD postgres;
ATTACH '$LOG_DB_READ_URL' AS pg (TYPE postgres, READ_ONLY);

-- Pair requests and responses
SELECT req.ts, req.entry->>'user_question' AS q,
       resp.entry->'tool_calls' AS tools, (resp.entry->>'latency_ms')::INT AS ms
FROM pg.public.logs req
JOIN pg.public.logs resp ON req.request_id = resp.request_id
WHERE req.type='request' AND resp.type='response'
  AND req.origin='https://tpl.nrp-nautilus.io'
  AND req.ts > now() - INTERVAL 20 MINUTES
ORDER BY req.ts DESC;
"
```

## Stdout (live tail, fallback)

Same JSON payload as Postgres, on every pod. Use `kubectl` for real-time inspection during active debugging:

```bash
kubectl -n biodiversity logs deployment/open-llm-proxy -f \
  | grep '"origin":"https://tpl.nrp-nautilus.io"'
```

If the Postgres pod is unavailable, log rows will **only** exist in stdout until the pod restarts. That's the safety net.

## S3 Parquet archive (cold storage)

On the first day of each month, a CronJob dumps the previous month's rows to `s3://logs-open-llm-proxy/archive/YYYY-MM.parquet` (zstd-compressed). The archive format keeps `entry` as a JSON-encoded `VARCHAR` column so DuckDB can still parse into it:

```bash
duckdb -s "
CREATE SECRET logs_s3 (TYPE S3, KEY_ID '$LOG_S3_KEY', SECRET '$LOG_S3_SECRET',
  ENDPOINT 's3-west.nrp-nautilus.io', USE_SSL true, URL_STYLE 'path');

SELECT ts, entry::JSON->>'user_question' AS q
FROM read_parquet('s3://logs-open-llm-proxy/archive/2026-03.parquet')
WHERE entry::JSON->>'origin' = 'https://tpl.nrp-nautilus.io';
"
```

To query live + archive as one set, UNION the Postgres table and the Parquet files in DuckDB.

## Log fields

### REQUEST entry (`type='request'`)

| Field | Description |
|---|---|
| `timestamp` | UTC ISO8601 (also stored as `ts TIMESTAMPTZ`) |
| `request_id` | 8-char hex — correlates this request to its response |
| `provider` | `"nrp"`, `"openrouter"`, or `"nimbus"` |
| `model` | Model name as sent by the client |
| `origin` | Origin or Referer header — identifies which app sent the request |
| `message_count` | Total messages in the conversation at this turn |
| `tools_count` | Number of tools available to the LLM |
| `user_question` | First `role: user` message — stable across all turns of a tool-use loop |
| `tool_results_this_turn` | Array of `{tool_call_id, content}` for any `role: tool` messages appended since the last assistant turn. `null` on the first turn. Content truncated to ~500 chars. |

### RESPONSE entry (`type='response'`)

| Field | Description |
|---|---|
| `timestamp`, `request_id`, `provider`, `model`, `origin` | Same semantics as request |
| `latency_ms` | End-to-end latency in milliseconds |
| `has_content`, `has_tool_calls` | Booleans |
| `content_preview` | First 200 chars of text response |
| `tool_calls` | Array of `{name, arguments}` — full tool call arguments including SQL query strings |
| `tokens` | Token usage from the provider (`prompt_tokens`, `completion_tokens`, `total_tokens`) |
| `error` | Error detail string (only present on failed requests) |

## Reconstructing a conversation

Each POST to `/v1/chat/completions` is one LLM turn. To reconstruct a session:

1. Filter by `origin` to isolate one app
2. Group turns by `entry->>'user_question'` (same question = same session)
3. Sort by `ts`
4. Pair each request with its response via `request_id`. Each request's `tool_results_this_turn` shows what the previous turn's tool calls returned; each response's `tool_calls` shows what the LLM decided to call next.

## Bootstrap (one-time)

Before the proxy can log to Postgres, the cluster needs:

1. **Secret `postgres-logs-auth`** — three passwords (superuser, writer, reader). Generate and apply manually (not committed).
   ```bash
   kubectl -n biodiversity create secret generic postgres-logs-auth \
     --from-literal=POSTGRES_PASSWORD="$(openssl rand -base64 32)" \
     --from-literal=LOG_WRITER_PASSWORD="$(openssl rand -base64 32)" \
     --from-literal=LOG_READER_PASSWORD="$(openssl rand -base64 32)"
   ```
2. **`log-db-url` key in `open-llm-proxy-secrets`** — writer DSN used by the proxy pod:
   ```
   postgresql://log_writer:<LOG_WRITER_PASSWORD>@postgres-logs:5432/logs
   ```
3. **Apply manifests in order**:
   ```bash
   kubectl apply -f postgres-initdb-configmap.yaml
   kubectl apply -f postgres-statefulset.yaml
   kubectl apply -f postgres-service.yaml
   # wait for LoadBalancer IP to be assigned, verify external 5432 reachable
   kubectl apply -f archive-cronjob.yaml
   kubectl apply -f retention-cronjob.yaml
   # finally, roll the proxy
   kubectl -n biodiversity rollout restart deployment/open-llm-proxy
   ```
