# open-llm-proxy Agent Instructions

## Purpose

This is an LLM proxy service that routes chat completion requests to NRP, OpenRouter, or Nimbus providers and logs every request/response pair. The primary analysis task for agents is evaluating those logs.

> **HARD BOUNDARY — edit only this repo.** Make code edits *only* within `open-llm-proxy`. The agent runner and analysis workflows reference sibling checkouts (e.g. `boettiger-lab/geo-agent`) for context, but you must **never edit another repo's code**. When a fix belongs in a sibling repo, open a GitHub issue on that repo (for its own agents/maintainers to action) and, where relevant, handle the corresponding change on this side. Reading sibling repos and their git history for diagnosis is fine and encouraged; editing them is not.

> **Track changes.** Behavior/config/ops changes should add a [CHANGELOG.md](CHANGELOG.md) entry under `## [Unreleased]`; see [README → Releases](README.md#releases) for the (SemVer) release process.

> **Use a git worktree — never build on top of another branch's active work.** The primary checkout is frequently mid-edit on some feature branch (uncommitted changes, a branch based on a stale `main`). Do **not** make your edits there: you'll tangle your change with unrelated work, and if that branch is behind `main` you can silently *revert* already-merged commits when you ship. For any change, start clean: `git worktree add <path> -b <branch> origin/main` (fetch first), edit there, PR to `main`. This keeps your diff minimal and based on current `main`.

> **No public ingress — keep it that way.** Since 2026-11-07 (#162) the NRP proxy is reachable only in-cluster (`http://open-llm-proxy.biodiversity.svc.cluster.local:8002/v1`). Browsers reach it same-origin through each app's nginx `/api/llm/`, which adds that app's key; Jobs call the Service. So there is no CORS layer to configure (the app's `CORSMiddleware` is dead config), and a new request header only has to pass the app's nginx. Do not re-apply `archive/ingress.yaml`; its header lists what must be true first.

> **Non-streaming by design — never "fix" a slow turn by adding streaming.** The proxy buffers every completion, and that buffer is what produces the request/response log pair, the tool-call dialect repair, and the token accounting — i.e. the very corpus this file tells you to analyze. `stream: true` is refused with a **400** (`llm_proxy.py`, #129); streaming was rejected as *a change to the primary analysis artifact*, not for lack of demand. geo-agent is a **caching**-agentic design, not a streaming one: the browser `Agent` awaits a complete response and there is no SSE consumer anywhere in its `app/`, and prompt caching — not time-to-first-token — is the cost lever (cold-cache first turns cost *more* per call than deep ones). Consequence to expect when reading logs: every per-call deadline covers the whole generation, so a slow turn can reach the user as a sidecar 502/504 while the proxy logs a **200** — `latency_ms` is proxy-side and does not prove the client was still listening — a caveat that outlives the issues it was raised in (#82 closed, #129 closed as *not planned* — the latter is the decision not to stream, not a pending fix). Per-call ceilings are a *chain* (`UPSTREAM_TIMEOUT_SECONDS`, each app's nginx sidecar and its ingress, geo-agent's `llm_timeout_seconds`); raise them together or the cap just relocates to the next hop (#136). See [README → No streaming (by design)](README.md#no-streaming-by-design).

## Deployments

The canonical deployment is on **NRP** (namespace `biodiversity`, in-cluster
only, no public hostname) — that is what the manifests at the repo root, and
everything below, describe. A second, narrower deployment runs on the lab's
**self-hosted k3s cluster** (`llm-proxy.carlboettiger.info`, namespace
`llm-proxy`): OpenRouter + DSE-nimbus only, raw-JSONL logs in the in-cluster
MinIO mirror, Traefik instead of HAProxy. See [cirrus/](cirrus/README.md).

Two rules for agents working on it:

1. **cirrus is config-only.** Everything cirrus-specific lives in `cirrus/` as
   deployment config (a ConfigMap mounted over `config.json`, env vars, Traefik
   CRDs). It runs this repo's unmodified code. Do **not** make an app change
   "for cirrus" and do **not** point its manifests at a branch — both deployments
   git-clone `main` at pod boot, so a shared-code edit reaches NRP at its next
   restart whether or not that was the intent. If cirrus genuinely needs an app
   change, propose it on its own merits for both deployments.
2. **Check which cluster `kubectl` is pointed at** (`kubectl config
   current-context`) before touching anything — a laptop configured for cirrus
   generally cannot reach NRP, and vice versa.

A third, on NRP in namespace **`schmidtdse`**, serves the apps that run there,
because pods outside `biodiversity` cannot connect into it since the Nov 7 2026
incident. It is a kustomize overlay of the root manifests (same code and
config; own Service, redis and client keys) and logs to the same bucket. See
[schmidtdse/](schmidtdse/README.md).

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

**Parquet schema** (same for daily and monthly tiers): the hot fields are flattened to typed columns — `ts, type, request_id, session_id, origin, client, provider, model, message_count, tools_count, enable_thinking, user_question, user_message_this_turn, latency_ms, has_tool_calls, has_content, tool_calls, tool_results, tokens, error` — plus `entry VARCHAR`, the full original log record as JSON text (kept for fidelity). Prefer the flat columns; for fields not promoted, use `json_extract_string(entry,'$.field')` / `json_extract(entry,'$.field')` (the `entry::JSON->>` form intermittently throws a cast error in aggregates). Legacy files predating the flatten carry only `ts/type/request_id/origin/entry`.

**Session view** (`s3://logs-open-llm-proxy/sessions/**/*.parquet`): one row per *turn*, request already joined to its response, ordered by `turn_idx` within `session_key`. This is the query-ready artifact — "show me every turn of session X in order, with tool calls and results" is one flat `SELECT`, no manual interleaving. Disjoint from the `consolidated/**` glob. See [LOGGING.md](LOGGING.md#reconstructing-a-conversation).

**Session rollup** (`s3://logs-open-llm-proxy/session-rollup/**/*.parquet`): one row per *session* — `turns`, `tool_calls_total`, token sums, `cost_usd`, `wall_clock_s`, `status` (`ok`/`timeout`/`error`/`budget_capped`/`incomplete`), `final_answer`, plus `app`/`run_tag` parsed from the origin. Reach for this for anything cross-app or cross-model ("calls per question", "cost per model", "completion rate") — it is a single `GROUP BY` instead of a bespoke report script. Disjoint from both globs above.

> ⚠️ Always filter `WHERE NOT session_key_synthetic` before averaging. Sessions with no `session_id` fall back to an `anon:<hash>` key that merges every repeat of the same question, which inflates `turns` and can stretch `wall_clock_s` over weeks. Near-universal before June 2026; ~0.7% of August 2026 sessions.

For automation, one-shot CI queries, or reads from inside a pod, query directly with the **scoped read-only credential** in the `rustfs-logs-read` Secret — Get/List on this one bucket, nothing else. See [LOGGING.md](LOGGING.md#direct-s3-one-shot-queries-automation-or-inside-nrp-pods) for the DuckDB snippet.

> ⚠️ `LOG_S3_KEY` / `LOG_S3_SECRET` are **the general NRP credentials** — read/write/delete on every bucket, and there is only one. **Reading logs does not need them at all.** `./sync-logs.sh` and the direct-S3 snippet use the scoped key (#113), and since #178 the logs live on rustfs, so the whole corpus from 2026-08-16 on is reachable with the scoped key. Reach for the general credentials only to write to NRP, or to touch a bucket other than this one.

Each LLM call produces a `request` row and a `response` row linked by `request_id`. Key fields inside `entry` (Parquet) or as top-level columns (JSONL):

- **Request**: `user_question` (session opener), `user_message_this_turn` (the actual prompt for *this* turn — use this to count/read distinct follow-up requests within a session), `tool_results_this_turn`, `model`, `origin`, `message_count`
- **Response**: `tool_calls`, `content_preview`, `tokens`, `latency_ms`, `error` (only on failures)

**Reconstructing a conversation**: for completed days, just query the **session view** (`sessions/**`) by `session_key` and order by `turn_idx` — the interleaving is already done. To reconstruct by hand (e.g. today's raw JSONL): group by `session_id` (exact; falls back to `user_question` for pre-wiring records), filter by `origin`, sort by `ts` (Parquet) / `timestamp` (JSONL). The `tool_results_this_turn` on each request shows what the previous turn's tool calls returned; `tool_calls` on each response shows what the LLM called next.

**Midnight crossover caveat**: flush-time (not entry `ts`) determines the source file path. An entry with `ts = 23:59:58` may live in the next UTC day's file if it was buffered past midnight. Always filter on `ts`, not on file path, when you care about a calendar day.

See [LOGGING.md](LOGGING.md) for full field reference, SQL patterns, kubectl access, session reconstruction examples, and the CronJob details.

When analyzing geo-agent app behavior (tool-call counts, query failures, session reconstructions), invoke the `geo-agent-training` skill — it provides the full step-by-step diagnostic workflow.

## Benchmarks live elsewhere

Benchmarking is not this repo's job. The headless runner, matrix Jobs, question banks,
gold answers and the `matrix-archiver` CronJob moved to
[boettiger-lab/glen-benchmark](https://github.com/boettiger-lab/glen-benchmark) on
2026-11-07, and that repo looks after them. Benchmark runs reach the proxy like any other
client, with their own key (`key_name`) and origin, so they can be filtered out of, or
into, a log query.
