# LLM Proxy Logging

## Where logs live

Logs are written to two places:

1. **Pod stdout** — available immediately via `kubectl`, lost on pod restart
2. **S3 bucket `logs-open-llm-proxy`** — flushed every 60 seconds (configurable via `FLUSH_INTERVAL` env var) as JSONL chunk files, persisted indefinitely

> This document describes the **NRP** deployment (bucket on NRP Ceph). The cirrus
> deployment writes the same record format to a same-named bucket on the
> in-cluster MinIO mirror, but has **only the raw JSONL tier** — the Parquet
> rollup, `sessions/**` view and `session-rollup/**` tier below are NRP-only for
> now. See
> [cirrus/README.md](cirrus/README.md).

### S3 layout (tiered rollup)

Three tiers, each holding a different age of data. A daily CronJob rolls
raw JSONL into daily Parquet; a monthly CronJob rolls completed months
of daily Parquet into one monthly Parquet. At any moment:

```
logs-open-llm-proxy/
├── 2026-04-17/                        ← today: raw JSONL (live debug tier)
│   ├── 02-00-05-39.jsonl              # flush at 02:00:05 from worker PID 39
│   └── ...
├── consolidated/                      ← one row per log entry (request OR response)
│   ├── daily/
│   │   ├── 2026-04-15.parquet         # recent completed days
│   │   └── 2026-04-16.parquet
│   └── monthly/
│       ├── 2026-02.parquet            # older completed months
│       └── 2026-03.parquet
├── sessions/                          ← one row per *turn* (request joined to its response)
│   ├── daily/
│   │   ├── 2026-04-15.parquet
│   │   └── 2026-04-16.parquet
│   └── monthly/
│       └── 2026-03.parquet
└── session-rollup/                    ← one row per *session* (mechanical stats)
    ├── daily/
    │   ├── 2026-04-15.parquet
    │   └── 2026-04-16.parquet
    └── monthly/
        └── 2026-03.parquet
```

| Tier | Format | When it gets written | When it gets deleted |
|---|---|---|---|
| Raw JSONL (`YYYY-MM-DD/*.jsonl`) | JSONL chunks | Proxy flushes every 60s | Next day's daily consolidation (03:00 UTC) |
| Daily (`consolidated/daily/YYYY-MM-DD.parquet`) | Parquet (zstd) | Daily cron at 03:00 UTC | Monthly rollup on day 2 (04:00 UTC) |
| Monthly (`consolidated/monthly/YYYY-MM.parquet`) | Parquet (zstd) | Monthly cron on day 2 | Never (long-term archive) |
| Session daily (`sessions/daily/YYYY-MM-DD.parquet`) | Parquet (zstd) | Daily cron, derived from that day's consolidated file | Monthly rollup on day 2 |
| Session monthly (`sessions/monthly/YYYY-MM.parquet`) | Parquet (zstd) | Monthly cron on day 2 | Never |
| Rollup daily (`session-rollup/daily/YYYY-MM-DD.parquet`) | Parquet (zstd) | Daily cron, derived from that day's session view | Monthly rollup on day 2 |
| Rollup monthly (`session-rollup/monthly/YYYY-MM.parquet`) | Parquet (zstd) | Monthly cron on day 2 | Never |

The **consolidated Parquet schema** (identical across daily and monthly tiers). The
hot fields are flattened to typed columns; the raw JSON is kept verbatim in `entry`
for fidelity, so old `entry::JSON->>'…'` / `json_extract_string` queries still work:

| Column | Type | Notes |
|---|---|---|
| `ts` | `TIMESTAMPTZ` | Extracted from the `timestamp` field of the raw JSON |
| `type` | `VARCHAR` | `'request'` or `'response'` |
| `request_id` | `VARCHAR` | Correlates request ↔ response |
| `session_id` | `VARCHAR` | Per-session id (see field note); `null` in pre-wiring records |
| `origin` | `VARCHAR` | App the traffic came from |
| `client` | `VARCHAR` | e.g. `geo-agent/v3.13.1`; `null` until clients send `X-Client` |
| `provider` | `VARCHAR` | `nrp` / `openrouter` / `nimbus` |
| `model` | `VARCHAR` | Model id |
| `message_count` | `INTEGER` | Request only — messages in the prompt |
| `tools_count` | `INTEGER` | Request only — tools offered |
| `enable_thinking` | `BOOLEAN` | Request only — the thinking mode the client **asked for** (`request.enable_thinking`). `null` = flag not sent / model default; `true`/`false` = explicit override. Distinct from the response-side `has_reasoning_content` (what the model actually **did**) — together they disambiguate "reasoning off by request" from "model chose not to think" from "non-thinking model". |
| `user_question` | `VARCHAR` | Request only — **first** user message (see trap below) |
| `user_message_this_turn` | `VARCHAR` | Request only — the **last** user message, i.e. the actual prompt that triggered *this* turn. Use this (not `user_question`) to count or read distinct requests within a session. `null` on records written before #89. |
| `latency_ms` | `BIGINT` | Response only |
| `has_tool_calls` | `BOOLEAN` | Response only |
| `has_content` | `BOOLEAN` | Response only |
| `tool_calls` | `JSON` | Response only — `[{name, arguments}]` |
| `tool_results` | `JSON` | Request only — `tool_results_this_turn` (prior turn's tool outputs) |
| `tokens` | `JSON` | Response only — usage object |
| `error` | `VARCHAR` | Response only — set on failures |
| `entry` | `VARCHAR` | The full original JSON record — parse with `json_extract_string`/`json_extract` |

Columns not applicable to a row's `type` are `null` (e.g. `latency_ms` on a request).
Files written before the flatten landed carried only the legacy 5 columns
(`ts`/`type`/`request_id`/`origin`/`entry`); both cron jobs run a **self-healing
schema-upgrade pass** that re-flattens any such file in place from its preserved
`entry` blob (lossless, idempotent), so the whole `consolidated/**` corpus converges
to one schema. Two safety nets cover the transient window before that completes:
existing `entry`-based queries are unaffected (DuckDB name-matches the common
columns across a mixed glob — no corruption), and the monthly rollup reads daily
files with `union_by_name=true`. The one mixed-glob gotcha: selecting a *new* flat
column (e.g. `model`) over a glob that still contains a legacy file raises
`column not found` — add `union_by_name=true` to `read_parquet(...)`, or just wait
for the upgrade pass.

The **session Parquet schema** (`sessions/**`) — one row per turn, request already
joined to its response, so "show me every turn of session X in order, with tool
calls and results" is a single flat `SELECT` with no JSON gymnastics and no manual
request/response interleaving:

| Column | Type | Notes |
|---|---|---|
| `session_key` | `VARCHAR` | `session_id` when present, else `anon:<md5(origin\|user_question)>` so every turn still groups |
| `session_id` | `VARCHAR` | Raw id (`null` if the heuristic key was used) |
| `turn_idx` | `BIGINT` | 1-based turn order within the session. **Daily files index within-day** (a session crossing UTC midnight restarts at 1 in the next day's file); the monthly view recomputes it over the whole month. Ordering by `request_ts` is always correct regardless. |
| `request_ts` / `response_ts` | `TIMESTAMPTZ` | Turn start / completion |
| `request_id` | `VARCHAR` | |
| `origin`, `client`, `provider`, `model` | `VARCHAR` | |
| `user_question` | `VARCHAR` | Opening question (same first-message-only caveat) |
| `user_message_this_turn` | `VARCHAR` | The actual prompt for this turn (last user message). This is the field to read per-turn — `SELECT turn_idx, user_message_this_turn` gives the real sequence of what the user asked. `null` before #89. |
| `message_count` | `INTEGER` | |
| `enable_thinking` | `BOOLEAN` | Requested thinking mode this turn (`null` = default / not sent). Pair with `reasoning_content` to compare requested-vs-observed reasoning per turn. |
| `tool_results` | `JSON` | Tool outputs that came *into* this turn |
| `assistant_content` / `reasoning_content` | `VARCHAR` | The model's reply this turn |
| `tool_calls` | `JSON` | Tools the model called *out* this turn |
| `has_tool_calls`, `has_content` | `BOOLEAN` | |
| `latency_ms`, `tokens`, `error` | `BIGINT`/`JSON`/`VARCHAR` | |

> Note: `consolidated/**`, `sessions/**` and `session-rollup/**` are disjoint
> top-level prefixes — a `read_parquet('…/consolidated/**/*.parquet')` never picks up
> session or rollup rows, so existing queries are unaffected.

The **session rollup schema** (`session-rollup/**`) — one row per *session* (#51).
Where `sessions/**` answers "what happened turn by turn", this answers "across apps
and models, how many LLM calls did each question take, how long, how much did it
cost, did it complete?" — the aggregation every benchmark used to hand-roll in its
own `build_report.py`. It is a `GROUP BY session_key` over the turn view, so it adds
no new capture; everything here is already in the logs.

| Column | Type | Notes |
|---|---|---|
| `session_key` | `VARCHAR` | Same key as the turn view |
| `session_id` | `VARCHAR` | Raw id (`null` when the heuristic key was used) |
| `session_key_synthetic` | `BOOLEAN` | **Read this before averaging anything.** `true` when the key is the `anon:<hash>` fallback, which groups *every* repeat of the same question from the same origin into one pseudo-session. Harmless per-turn; at session grain it inflates `turns` and stretches `wall_clock_s` across weeks. Filter with `WHERE NOT session_key_synthetic` for any per-session statistic. Near-universal before June 2026, ~0.7% of August 2026 sessions. |
| `origin`, `app`, `run_tag`, `client`, `model`, `provider` | `VARCHAR` | Taken from the **first** turn — for an experiment, "what the run was launched with". `app` is the origin host's first label (`tpl-ca`); `run_tag` is the origin path (`/agent_runner_bench2`), which is how matrix runs are tagged apart from real traffic. |
| `models_used` | `BIGINT` | Distinct models across the session. `> 1` means a fallback ladder switched mid-session (~3% of sessions), so the single `model` column above does not tell the whole story. |
| `question` | `VARCHAR` | The first turn's prompt |
| `turns` | `BIGINT` | LLM calls in the session |
| `tool_calls_total` | `BIGINT` | Tool calls summed across turns |
| `prompt_tokens`, `completion_tokens`, `cached_tokens`, `total_tokens` | `BIGINT` | Summed across **all** turns |
| `cost_usd` | `DOUBLE` | Summed across turns. **Only OpenRouter reports a cost** — NRP and nimbus are self-hosted and Anthropic-direct does not return one — so this is `NULL`, not `0`, where nothing reported one: "no price data" must not read as "free". |
| `cost_turns` | `BIGINT` | How many turns carried a cost, so partial coverage is detectable |
| `llm_ms_total` | `BIGINT` | Sum of per-turn `latency_ms` — time actually spent in the model |
| `wall_clock_s` | `DOUBLE` | Last activity − first activity. Exceeds `llm_ms_total` by however long the client spent thinking, running tools, or idle |
| `error_turns` | `BIGINT` | Turns that carried an error. Non-zero with `status = 'ok'` means the session recovered |
| `unanswered_turns` | `BIGINT` | Turns with a request but **no response row at all** — the proxy died, the client hung up, or one side of the pair was dropped (#39/#121). Distinct from `error_turns`: an error *is* a response |
| `status` | `VARCHAR` | How the session **ended**: `ok` / `timeout` / `error` / `budget_capped` / `incomplete`. `incomplete` means the last turn never got a response — measured at ~5% of what would otherwise read as `ok`, and 95% of those are real (non-synthetic) sessions, so do not treat it as noise. Anything finer than these five (provider 5xx vs 4xx, never-reached-provider vs rejected) is a `LIKE` on `final_error`, which is kept verbatim for exactly that |
| `final_error` | `VARCHAR` | The last turn's error string, if any |
| `final_answer` | `VARCHAR` | Last non-empty assistant content |
| `started_at`, `finished_at` | `TIMESTAMPTZ` | Session span |

Two caveats worth knowing before trusting a number:

- **Daily rollups index within-day**, like the turn view. A session crossing UTC
  midnight appears as two rows across two daily files; the monthly job rebuilds the
  rollup from the whole-month turn view, collapsing it back to one. (Measured on
  2026-08: 2,037 daily rows → 2,014 month-wide, with turn and token totals
  identical.) Summing `turns` across a mixed daily glob is always correct; counting
  *sessions* is only exact within one tier.
- **`provider` labels the first turn, `cost_usd` sums all of them.** A session that
  starts on NRP and falls back to OpenRouter is attributed to `nrp` but carries the
  OpenRouter cost. `models_used > 1` is the flag for it.

```sql
-- Cross-model stats: calls, latency, cost and completion rate per question.
SELECT model,
       count(*)                       AS sessions,
       round(avg(turns), 1)           AS calls_per_question,
       round(avg(wall_clock_s), 1)    AS avg_seconds,
       round(sum(cost_usd), 2)        AS usd,
       round(100.0 * count(*) FILTER (WHERE status = 'ok') / count(*), 1) AS pct_ok
FROM read_parquet('/tmp/open-llm-proxy-logs/session-rollup/**/*.parquet')
WHERE NOT session_key_synthetic
  AND started_at > now() - INTERVAL 14 DAYS
GROUP BY 1 ORDER BY sessions DESC;

-- One experiment's grid, isolated by the origin tag the matrix run used.
SELECT run_tag, model, count(*) AS sessions, round(avg(turns),1) AS calls,
       count(*) FILTER (WHERE status <> 'ok') AS failures
FROM read_parquet('/tmp/open-llm-proxy-logs/session-rollup/**/*.parquet')
WHERE run_tag IS NOT NULL
GROUP BY 1, 2 ORDER BY run_tag, sessions DESC;
```

## Access pattern

> ✅ **Reading logs no longer needs a privileged credential (#113).** `./sync-logs.sh`
> reads the **rustfs mirror** with a credential scoped to this one bucket, Get/List only.
> The old default was rclone's `nrp:` remote — the single NRP credential, which carries
> read/write/delete on *every* NRP bucket, for a read-only task on a few MiB of logs.
>
> NRP Ceph remains the system of record; the consolidation CronJobs copy `consolidated/**`
> and `sessions/**` to rustfs after each run. rustfs shares the same rook Ceph, so the
> mirror is a convenience copy, **not** a second failure domain, and must never hold the
> only copy.

### Local sync (recommended for interactive analysis)

Sync once per session, then query the local files — no S3 secret at query time, and orders of magnitude faster iteration:

```bash
./sync-logs.sh                   # syncs to /tmp/open-llm-proxy-logs
./sync-logs.sh ~/scratch/logs    # or pick your own path
```

It resolves a read-only credential in this order:

1. `$LOGS_READ_KEY` / `$LOGS_READ_SECRET` from the environment;
2. the **`rustfs-logs-read` Secret** in `biodiversity`, fetched via `kubectl` — so the
   value never lands in a dotfile or your shell history. This is the usual path: if you
   can reach the cluster, `./sync-logs.sh` just works;
3. rclone's `nrp:` remote — the broad NRP credential. Legacy fallback only; it prints a
   warning naming what that key can do.

Reference the Secret by **name**, never by value, so rotation touches nothing here.

Full sync is a few seconds (~63 MB). Re-syncs during the same session are near-instant — rclone only transfers what changed.

> **Mirror freshness: ~1h.** It holds the query-ready tiers — `consolidated/**` and
> `sessions/**`, refreshed by the consolidation CronJob at 03:00 UTC — **and today's raw
> JSONL** under `YYYY-MM-DD/`, refreshed hourly by `logs-mirror-raw-hourly` (#124).
> For fresher than that use [`kubectl`](#kubectl-live-logs--last-60s-before-next-flush).
>
> This closed a 3–27h hole. Consolidation *structurally* excludes the current day, so
> before #124 an event only reached the mirror at 03:00 the next day — ~3h for something
> logged at 23:59, ~27h at 00:00, ~15h on average — and everything inside that window
> needed the omnipotent NRP key. Running consolidation more often would not have helped:
> the current day is excluded by construction, not by cadence.

Then query the local path — no `CREATE SECRET` needed:

```bash
duckdb -s "
-- Historical: all consolidated data in one read (daily + monthly Parquet)
SELECT ts, entry::JSON->>'user_question' AS q
FROM read_parquet('/tmp/open-llm-proxy-logs/consolidated/**/*.parquet')
WHERE entry::JSON->>'origin' = 'https://tpl.nrp-nautilus.io'
  AND ts > now() - INTERVAL 7 DAYS;

-- Today's live data (raw JSONL — narrow the glob to an hour when possible)
SELECT * FROM read_ndjson_auto(
  '/tmp/open-llm-proxy-logs/2026-04-17/*.jsonl', union_by_name=true);

-- Pair requests and responses from consolidated Parquet
SELECT req.ts, req.entry::JSON->>'user_question' AS q,
       resp.entry::JSON->'tool_calls' AS tools,
       (resp.entry::JSON->>'latency_ms')::INT AS ms
FROM read_parquet('/tmp/open-llm-proxy-logs/consolidated/**/*.parquet') req
JOIN read_parquet('/tmp/open-llm-proxy-logs/consolidated/**/*.parquet') resp
  ON req.request_id = resp.request_id
WHERE req.type = 'request' AND resp.type = 'response';
"
```

Live data caveat: raw JSONL for today is only as fresh as the last `./sync-logs.sh`. Re-run it before querying if you care about the last few minutes. For sub-minute freshness, use `kubectl` (below) or the direct-S3 path.

For queries that span today + history, UNION raw JSONL and consolidated Parquet with a shared projection (cast JSONL rows' `timestamp` to `TIMESTAMPTZ` and wrap the full row back into JSON if needed).

### Direct S3 (one-shot queries, automation, or inside NRP pods)

When you don't want a local copy — e.g. a single CI query, a k8s job, or always-current reads inside a pod — query directly with a DuckDB secret. **Use the scoped read-only credential**, and let the shell expand it so the value stays out of the transcript:

```bash
export LOGS_READ_KEY=$(kubectl -n biodiversity get secret rustfs-logs-read \
  -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d)
export LOGS_READ_SECRET=$(kubectl -n biodiversity get secret rustfs-logs-read \
  -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d)

duckdb -s "
CREATE SECRET logs_ro (TYPE S3, KEY_ID '$LOGS_READ_KEY', SECRET '$LOGS_READ_SECRET',
  ENDPOINT 'rustfs.nrp-nautilus.io', USE_SSL true, URL_STYLE 'path');

SELECT session_key, turn_idx, user_message_this_turn
FROM read_parquet('s3://logs-open-llm-proxy/sessions/**/*.parquet')
ORDER BY session_key, turn_idx LIMIT 20;
"
```

This key is **Get/List on `logs-open-llm-proxy` only** — it cannot write or delete anything, here or elsewhere. Agents should use the Bash tool so shell expansion keeps the values out of the conversation transcript.

> ⚠️ **`LOG_S3_KEY` / `LOG_S3_SECRET` are a different thing — don't use them for reads.**
> There is exactly **one** credential for NRP bucket access, so those variables hold it:
> **read/write/delete across every NRP bucket**. They remain necessary only for reaching
> the NRP source directly — chiefly *today's* raw JSONL, which the mirror doesn't carry.
> Everything else should go through the scoped key above. (An older version of this note
> called them "scoped keys for this bucket"; that was wrong, and wrong in the direction
> that hides risk.)

```bash
duckdb -s "
CREATE SECRET logs_s3 (TYPE S3, KEY_ID '$LOG_S3_KEY', SECRET '$LOG_S3_SECRET',
  ENDPOINT 's3-west.nrp-nautilus.io', USE_SSL true, URL_STYLE 'path');

SELECT ts, entry::JSON->>'user_question' AS q
FROM read_parquet('s3://logs-open-llm-proxy/consolidated/**/*.parquet')
WHERE ts > now() - INTERVAL 7 DAYS;
"
```

**Narrow the raw JSONL glob to the current hour** (e.g. `2026-04-17/14-*.jsonl`) — do NOT scan the whole day. `union_by_name=true` handles schema drift across chunks (e.g. the `error` column appears only in some files). If the queried window has zero error responses, `error` is absent from all files — omit it from JOIN queries or use `TRY(resp.error)`.

**Temporal filter:** `timestamp` is stored as VARCHAR (ISO8601). Cast to `TIMESTAMPTZ` (not `TIMESTAMP`) when comparing against `now()`:
```sql
CAST(req.timestamp AS TIMESTAMPTZ) >= now() - INTERVAL 20 MINUTES
```
Using `TIMESTAMP` causes a type mismatch binder error because `now()` returns `TIMESTAMPTZ`.

**Log field truncation:** As of the training-grade logging change, the response `content`/`reasoning_content` are captured **in full** (use them, not the 200-char `*_preview` fields), and `tool_results_this_turn[N].content` is capped at 20000 chars (`LOG_TOOL_RESULT_MAX`). **Records written before that change** still have only a 200-char `content_preview` and 500-char tool results — for those, a tool result that appears to contain only column names likely has full descriptions below the truncation point; verify using the STAC MCP tools directly rather than inferring from old previews.

**Transient "malformed JSON" errors on raw JSONL:** Occasionally a `read_ndjson_auto` over today's directory will fail with `unexpected control character in string` at some byte offset. This is almost always a spurious partial byte-range read between DuckDB's httpfs extension and the Ceph gateway under high-parallelism scans — not actual bad data. The proxy writes with `json.dumps` (which escapes every control char) and S3 PUTs are atomic, so malformed lines on disk are not possible from the normal write path. The local-sync workflow avoids this entirely (it's only observed against `s3://` reads). If you're on the direct-S3 path: retry the query, or pass `ignore_errors=true` to `read_ndjson_auto` if you want a tolerant scan:
```sql
read_ndjson_auto('s3://.../YYYY-MM-DD/*.jsonl', union_by_name=true, ignore_errors=true)
```
Do not treat this as a proxy bug unless you can point at an actual byte with a raw control character in a file that's not being actively written.

Inside NRP pods, use the internal endpoint instead (`rook-ceph-rgw-nautiluss3.rook`, `USE_SSL false`) — faster and no public-endpoint throttling.

### kubectl (live logs / last ~60s before next flush)

Pod stdout is **compacted**: each field is bounded to ~200 chars
(`LOG_STDOUT_MAX_FIELD`) and the full `messages` array (full mode) is omitted —
so `kubectl logs` stays readable and isn't flooded by large prompts/responses.
The **complete, untruncated** record is in S3; use that for anything beyond a
live glance. (When S3 is disabled, stdout falls back to the full record since
it's then the only sink.) `origin`/`request_id`/`type` are never shrunk, so the
grep filters below still work.

```bash
# Live tail
kubectl -n biodiversity logs deployment/open-llm-proxy -f

# Recent history
kubectl -n biodiversity logs deployment/open-llm-proxy --tail=500

# Filter to a specific app
kubectl -n biodiversity logs deployment/open-llm-proxy --tail=1000 \
  | grep '"origin":"https://padus.nrp-nautilus.io"'
```

## Is the logging pipeline actually healthy? (#39)

A *one-sided* failure is the dangerous one: if responses stop being written while
requests keep flowing, nothing breaks and no request fails — the corpus just quietly
goes lopsided. That happened in #37 (a variable-shadowing bug made `log_response` throw
inside `@_never_raises`) and was invisible for hours.

Two signals now make it observable. Both are **observability only** — they never change
what is served.

**1. Request:response balance.** Every flush window, the proxy compares how many request
and response entries actually reached the buffer. A sustained shortfall prints:

```
🚨 Logging imbalance: 0 responses for 25 requests (ratio 0.00 < 0.5) in the
   last 60s — one side of the logging pipeline may have stopped writing.
```

Tunable via `LOG_RATIO_FLOOR` (default `0.5`) and `LOG_RATIO_MIN_REQUESTS` (default `20`,
so a single turn straddling a window boundary can't trip it). Counting happens where an
entry lands in the buffer, not on entry to the log functions — so a `log_response` that
*throws* goes uncounted, which is the whole point.

**2. Swallowed logging exceptions.** `@_never_raises` still keeps logging faults from
breaking request serving, but now counts them per function and escalates at 10 / 100 /
1000 occurrences (`🚨 … has now failed N times — logging is persistently broken, not a
one-off`), so a systematic fault can't hide among one-off serialization edge cases.

Both are exposed on `/health`:

```jsonc
"logging": {
  "last_window":  { "requests": 120, "responses": 119, "ratio": 0.992 },
  "windows_checked": 340,
  "imbalance_windows": 0,          // windows that tripped the floor
  "swallowed_exceptions": 0,
  "swallowed_by_fn": {},           // e.g. {"log_response": 25}
  "buffer_depth": 12               // entries awaiting S3 flush
}
```

> ⚠️ `/health`'s **`status` field is deliberately unaffected** by any of this. That
> endpoint backs the liveness, readiness *and* startup probes, so degrading it on a
> logging fault would restart or de-rotate a pod that is serving traffic perfectly well.
> Alert on the `logging` fields, never on `status`.

## Logging fidelity & credential scrubbing

The proxy logs are used as an evaluation/training corpus (see the `agent_runner_*`
origins), so capture is **training-grade**, controlled by `LOG_CAPTURE_MODE`:

| Mode | What's captured | Use |
|---|---|---|
| `summary` (default) | Full response `content`/`reasoning_content`, generously-capped `user_question` **and per-turn `user_message_this_turn`** and tool results, full `tool_calls`. **No** raw prompt (the full `messages` array). | Observability + most analysis; the response target is faithful, and follow-up prompts are captured per turn. |
| `full` | Everything in `summary`, **plus** the entire `messages` array per request (system prompt de-duplicated by hash). | Reconstructing exact `(messages → completion)` training pairs. |

Caps are tunable per field via env vars (`0` = uncapped): `LOG_CONTENT_MAX`
(final answer, default 0), `LOG_REASONING_MAX` (thinking trace, default 4000),
`LOG_TOOL_RESULT_MAX` (default 20000), `LOG_USER_QUESTION_MAX` (default 4000).
Capture mode and per-field caps are **orthogonal**: capture mode controls how
much of the *prompt* (`messages`) is kept; caps bound each *output field*.
`tool_calls` arguments (the SQL the model wrote) are always kept in full. The
default config therefore keeps full tool calls + full final answer but bounds
the bulky reasoning trace — set `LOG_REASONING_MAX=0` to capture it in full.

Logging is wrapped so a failure (e.g. a scrubbing/serialisation edge case) can
never break request serving — it logs a `⚠️ … failed (request still served)`
breadcrumb to stdout and the upstream call proceeds normally.

**Credential scrubbing is always on, in both modes.** The geo-agent `query` MCP
tool takes `s3_key`/`s3_secret` in its arguments, which flow through `tool_calls`,
tool results, and (in `full` mode) `messages`. Before anything is logged the proxy
redacts: values under credential-looking keys (`s3_secret`, `s3_key`, `api_key`,
`password`, `token`, …), DuckDB `KEY_ID '…'` / `SECRET '…'` literals, and
`Authorization: Bearer …` tokens — replacing them with `[REDACTED]`. This is
exercised by `test_logging.py`. **Note:** the live scrubber only protects records
written *after* this change; older Parquet still contains leaked secrets. The
one-off **`scrub-historical-logs.py`** job (manifest: `scrub-historical-logs-job.yaml`)
rewrites those historical S3 objects in place using the *same* `scrub.py` logic,
so the corpus is safe to share. It is idempotent (re-runs are no-ops), preserves
JSON semantics (only the `entry` `tool_calls`/tool-result fields change), and
rewrites Parquet via a temp key + row-count check + atomic copy. Run `--dry-run`
first, then `--verify` for the real pass. Validated against the current bucket:
184 leaking rows → 0, with zero data loss on the rest.

**Size note:** `summary` mode is comparable to the old format (a few MiB/year; the
extra full-content bytes compress well under Parquet zstd). `full` mode is far
larger — each turn carries the whole conversation (~23k prompt tokens avg), so the
system-prompt dedup (which removes the dominant repeated ~22k-token blob) is what
keeps it tractable. With multiple uvicorn workers the dedup set is per-process, so
each distinct system prompt is logged once per worker.

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
| `client` | `X-Client` header — client app + version, e.g. `geo-agent/v3.13.1`. `null` until the client sends it. Correlates logged behavior with a specific release. |
| `session_id` | Stable per-browser-session id that ties together every turn **and every follow-up question** of one user session. Sourced from the request body's OpenAI `user` field (geo-agent already sends its per-session UUID there), falling back to the `X-Session-Id` header. **`null` in records written before this was wired up** (see wiring note below) — for those, fall back to the `(origin, user_question)` heuristic and mind the `user_question` caveat. |
| `message_count` | Total messages in the conversation at this turn |
| `tools_count` | Number of tools available to the LLM |
| `enable_thinking` | The thinking mode the client **requested** for this turn (`request.enable_thinking`): `null` when the flag wasn't sent (model default), `true`/`false` on an explicit override. The request-side counterpart to the response's `has_reasoning_content`/`reasoning_content` (what the model actually did). Lets you tell "reasoning off by request" apart from "model chose not to think" and from "non-thinking model" — needed to evaluate a user-facing reasoning toggle from live traffic. |
| `user_question` | First `role: user` message in the conversation. ⚠️ **First-message-only — this is a trap.** It is the human's *original* opening question and is stable across all turns of a tool-use loop, but it does **not** update when the user asks a follow-up in the same session. A session where the user opens with "Tell me about datasets" and later asks "now map the hardwood woodland" logs **both** turns under `user_question = "Tell me about datasets"`. **To read the actual per-turn prompt, use `user_message_this_turn` instead** (below) — the trap only applies if you filter on `user_question`. Capped at `LOG_USER_QUESTION_MAX` chars (default 4000). |
| `user_message_this_turn` | Last `role: user` message in the conversation — the actual prompt that triggered **this** turn (#89). This is the field to group/count/read distinct requests within a session, since `session_id` persists across a whole browsing day. On a one-shot first turn it equals `user_question`; on a follow-up it carries the new prompt ("now map the hardwood woodland") that `user_question` misses. `null` on records written before #89. Capped at `LOG_USER_QUESTION_MAX` chars. |
| `tool_results_this_turn` | Array of `{tool_call_id, content}` for any `role: tool` messages appended since the last assistant turn. Captures results from both local geo-agent tools (e.g. `list_datasets`, `get_dataset_details`) and remote MCP tools (e.g. `query`). Each `content` is capped at `LOG_TOOL_RESULT_MAX` chars (default 20000). `null` on the first turn. |
| `messages` | **Only when `LOG_CAPTURE_MODE=full`.** The entire scrubbed `messages` array sent to the provider — training-grade fidelity. The large system prompt is de-duplicated: each `role: system` message is replaced by `{role, system_sha256, content_len, _dedup: true}` and the full body is emitted once (per worker) as a separate `type: "system_prompt"` entry. Join `messages[].system_sha256` → the `system_prompt` entry to rehydrate. |

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
| `client` | Same as request — `X-Client` header (client app + version), `null` until sent |
| `session_id` | Same as request — per-session id from the `user` body field / `X-Session-Id` header |
| `latency_ms` | End-to-end latency in milliseconds |
| `has_content` | Whether the LLM returned text content |
| `has_tool_calls` | Whether the LLM made tool calls |
| `has_reasoning_content` | Whether the LLM returned a separate `reasoning_content` field (qwen3 thinking-mode and similar). A response with `has_content=false` and `has_reasoning_content=true` means the model spent its budget reasoning but never emitted a final answer — diagnostic for degenerate-200 cases. |
| `content` | **Full** text response (the training target), scrubbed of credentials. Capped only if `LOG_CONTENT_MAX > 0` (default 0 = uncapped). |
| `reasoning_content` | **Full** `reasoning_content` (qwen3 thinking-mode etc.), scrubbed. Empty for non-thinking models. |
| `content_preview` | First 200 chars of `content` — kept for cheap kubectl/SQL scans. |
| `reasoning_content_preview` | First 200 chars of `reasoning_content`. |
| `tool_calls` | Array of `{name, arguments}` — full tool call arguments including SQL query strings. Credential args (`s3_key`/`s3_secret`/…) are redacted. |
| `tokens` | Token usage object from the provider (`prompt_tokens`, `completion_tokens`, `total_tokens`) |
| `error` | Error detail string (only present on failed requests) |
| `upstream_headers` | Allow-listed upstream response headers, captured only when the upstream returned an HTTP error response (#44). Tells a real rate-limit (`retry-after`/`x-ratelimit-*`) apart from a dead-backend gateway failure (naked `500`, `content-length: 0`, no `server`/`x-request-id`). Not a flat column — query via `json_extract(entry,'$.upstream_headers')`. |

### Wiring up `session_id`

`session_id` is populated server-side from two sources, in priority order:

1. **The OpenAI `user` request-body field (primary).** geo-agent's `Agent` mints
   one `crypto.randomUUID()` per instance (`this.sessionId`) and already sends it
   as `user` on every `/chat/completions` POST. The proxy reads `request.user`
   into `session_id` (`llm_proxy.py`) and logs it — but does **not** forward `user`
   upstream, so provider-side caching/abuse-monitoring is unaffected. This required
   no client change and lights up all existing geo-agent traffic.
2. **The `X-Session-Id` header (fallback).** For non-geo-agent clients that prefer
   a header. It is in the ingress `cors-allow-headers` allow-list (`ingress.yaml`);
   CORS is enforced at the ingress, not the app, so any custom header must be
   listed there or the browser preflight blocks the whole request (same gotcha as
   `X-Client` in #26).

Records written before this wiring have `session_id = null`; group those by the
`(origin, user_question)` heuristic instead. For newer records, group by
`session_id` for exact session reconstruction — it is immune to the
`user_question` first-message-only trap and survives follow-up questions.

## Reconstructing a conversation

> ✅ **Easiest path: the session view (`sessions/**`).** For completed days/months,
> the turn-level reconstruction below is already materialized — one row per turn with
> request and response joined, `tool_results` (in) and `tool_calls` (out) interleaved,
> ordered by `turn_idx`. No manual pairing, no JSON casting:
>
> ```sql
> SELECT turn_idx, model, user_message_this_turn,   -- the real prompt each turn (not the stale opener)
>        json_array_length(tool_results) AS results_in,
>        json_array_length(tool_calls)   AS calls_out,
>        assistant_content, latency_ms
> FROM read_parquet('/tmp/open-llm-proxy-logs/sessions/**/*.parquet')
> WHERE session_key = '…'          -- session_id, or anon:<hash> for pre-wiring rows
> ORDER BY turn_idx;
> ```
>
> Find a `session_key` by filtering on `origin`/`user_question`/`ts` first. The manual
> recipe below still applies to **today's** raw JSONL (not yet consolidated) and is
> what the session view itself is built from.

Each POST to `/v1/chat/completions` is one LLM turn. A single user session produces multiple request/response pairs. To reconstruct a session:

1. Match by `origin` to isolate one app
2. Group turns by `user_question` (same question = same session)
3. Sort by `timestamp`
4. Interleave: each request's `tool_results_this_turn` shows what the previous turn's tool calls returned; each response's `tool_calls` shows what the LLM decided to call next

Use `request_id` to pair each request with its response when log lines are interleaved under concurrent load.

> ⚠️ **`user_question` groups by *opening* question, not by session.** Step 2 is a heuristic with two failure modes: (a) two different users who happen to open with the same question collapse into one apparent session, and (b) a single user's **follow-up questions never get their own group** — they stay pinned to the opening `user_question` (see the field note above). When `session_id` is populated, group by it instead — it is exact and survives follow-ups. Until then, treat a single `user_question` group as "one opening question and everything that followed it," not "one atomic question." **To recover the distinct user requests *within* a session, read/segment on `user_message_this_turn` (#89), which carries each turn's actual prompt** — `user_question` cannot distinguish them.

> 📋 **Brittle-JSON caveat (consolidated Parquet).** `entry::JSON->>'field'` intermittently throws `Conversion Error: Failed to cast value to numerical` — DuckDB mis-infers an all-`null` or numeric-looking JSON path and tries to cast the whole `entry` blob. It is load-bearing-flaky: the same expression works in a bare `SELECT` but fails inside a `GROUP BY`/aggregate. Use `json_extract_string(entry, '$.field')` (for text) and `json_extract(entry, '$.field')` (for JSON) instead — they never throw.

## Example session reconstruction

```
REQUEST  msg=2  user_question="Tell me about datasets"  tool_results=null
RESPONSE tool_calls=[{name: list_datasets, arguments: {}}]

REQUEST  msg=4  user_question="Tell me about datasets"  tool_results=[{content: "[{id: pad-us...}]"}]
RESPONSE tool_calls=[{name: get_schema, arguments: {dataset_id: "pad-us-4.1-fee"}}]

REQUEST  msg=6  user_question="Tell me about datasets"  tool_results=[{content: "path: s3://... | column | sample..."}]
RESPONSE has_content=true  content_preview="The PAD-US dataset contains..."
```

Note: `list_datasets` and `get_schema` are both client-side geo-agent tools, but only one of them stays client-side.

- **`list_datasets` is local-only.** It is answered from the in-browser catalog and never reaches the MCP server.
- **`get_schema` is a local *delegate*.** Its `execute()` forwards to the MCP server as a **`get_stac_details`** call, passing the cached STAC collection inline (`geo-agent/app/map-tools.js`, `callTool('get_stac_details', …)`).

> ⚠️ **Counting MCP tool load from proxy logs undercounts `get_stac_details`.** The proxy logs the name the *LLM* called (`get_schema`); the delegated MCP request does not pass through this proxy, so it is invisible here. To estimate real MCP-server load, attribute every `get_schema` tool-call to `get_stac_details` as well. In the June 2026 corpus that is the difference between 339 direct `get_stac_details` calls and ~3,255 actual ones (~2,916 arriving via `get_schema`) — i.e. the #2 MCP tool, not a minor one.

## What the MCP server logs add

The DuckDB MCP server logs SQL execution separately. MCP logs are only needed for:
- SQL execution errors not visible to the LLM (failed queries that return an error string)
- Exact query timing at the database layer

For conversation-level analysis (what users asked, what the LLM decided, what tools returned), proxy logs are self-sufficient.
