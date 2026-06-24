# open-llm-proxy Agent Instructions

## Purpose

This is an LLM proxy service that routes chat completion requests to NRP, OpenRouter, or Nimbus providers and logs every request/response pair. The primary analysis task for agents is evaluating those logs.

> **HARD BOUNDARY — edit only this repo.** Make code edits *only* within `open-llm-proxy`. The agent runner and analysis workflows reference sibling checkouts (e.g. `boettiger-lab/geo-agent`) for context, but you must **never edit another repo's code**. When a fix belongs in a sibling repo, open a GitHub issue on that repo (for its own agents/maintainers to action) and, where relevant, handle the corresponding change on this side. Reading sibling repos and their git history for diagnosis is fine and encouraged; editing them is not.

> **Track changes.** Behavior/config/ops changes should add a [CHANGELOG.md](CHANGELOG.md) entry under `## [Unreleased]`; see [README → Releases](README.md#releases) for the (SemVer) release process.

> **CORS gotcha:** browser CORS is enforced by the **haproxy ingress** (`ingress.yaml` annotations), *not* the app's `CORSMiddleware` (which is effectively dead config). `cors-allow-headers` is an explicit list — a new custom request header (e.g. `X-Client`) must be added there or the browser preflight blocks the whole request. See the comment in `ingress.yaml`.

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

**Parquet schema** (same for daily and monthly tiers): the hot fields are flattened to typed columns — `ts, type, request_id, session_id, origin, client, provider, model, message_count, tools_count, user_question, latency_ms, has_tool_calls, has_content, tool_calls, tool_results, tokens, error` — plus `entry VARCHAR`, the full original log record as JSON text (kept for fidelity). Prefer the flat columns; for fields not promoted, use `json_extract_string(entry,'$.field')` / `json_extract(entry,'$.field')` (the `entry::JSON->>` form intermittently throws a cast error in aggregates). Legacy files predating the flatten carry only `ts/type/request_id/origin/entry`.

**Session view** (`s3://logs-open-llm-proxy/sessions/**/*.parquet`): one row per *turn*, request already joined to its response, ordered by `turn_idx` within `session_key`. This is the query-ready artifact — "show me every turn of session X in order, with tool calls and results" is one flat `SELECT`, no manual interleaving. Disjoint from the `consolidated/**` glob. See [LOGGING.md](LOGGING.md#reconstructing-a-conversation).

For automation, one-shot CI queries, or queries that need sub-minute freshness without re-syncing, query S3 directly with `LOG_S3_KEY` / `LOG_S3_SECRET` from the shell — see [LOGGING.md](LOGGING.md#direct-s3-one-shot-queries-automation-or-inside-nrp-pods).

Each LLM call produces a `request` row and a `response` row linked by `request_id`. Key fields inside `entry` (Parquet) or as top-level columns (JSONL):

- **Request**: `user_question`, `tool_results_this_turn`, `model`, `origin`, `message_count`
- **Response**: `tool_calls`, `content_preview`, `tokens`, `latency_ms`, `error` (only on failures)

**Reconstructing a conversation**: for completed days, just query the **session view** (`sessions/**`) by `session_key` and order by `turn_idx` — the interleaving is already done. To reconstruct by hand (e.g. today's raw JSONL): group by `session_id` (exact; falls back to `user_question` for pre-wiring records), filter by `origin`, sort by `ts` (Parquet) / `timestamp` (JSONL). The `tool_results_this_turn` on each request shows what the previous turn's tool calls returned; `tool_calls` on each response shows what the LLM called next.

**Midnight crossover caveat**: flush-time (not entry `ts`) determines the source file path. An entry with `ts = 23:59:58` may live in the next UTC day's file if it was buffered past midnight. Always filter on `ts`, not on file path, when you care about a calendar day.

See [LOGGING.md](LOGGING.md) for full field reference, SQL patterns, kubectl access, session reconstruction examples, and the CronJob details.

When analyzing geo-agent app behavior (tool-call counts, query failures, session reconstructions), invoke the `geo-agent-training` skill — it provides the full step-by-step diagnostic workflow.

## Reproducing geo-agent sessions from the CLI

`headless/run.js` replays a full geo-agent session (catalog load, MCP connect, prompt assembly, tool-use loop) through the proxy for scripted model comparisons and failure repros. It imports `Agent`, `DatasetCatalog`, `ToolRegistry`, and `createMapTools` directly from a sibling `boettiger-lab/geo-agent` checkout, so prompt catalog injection, `get_schema`, `<tool_call>` XML parsing, and context/result trimming match the browser by construction. Three pieces are local: `stub-map-manager.js` (stubs MapLibre), a `fetch` wrapper in `run.js` (adds the `Origin` header), and `mcp-client.js` (vendored byte-for-byte from `geo-agent/app/mcp-client.js` so the `@modelcontextprotocol/sdk` bare specifier resolves against this package's `node_modules`).

The vendored MCP client is the one thing prone to silent drift. Whenever geo-agent ships an MCP transport change (e.g. a `callTool` timeout bump, a new reconnect hook), re-vendor:

```bash
cd headless
npm run check-drift   # fails non-zero if mcp-client.js has drifted from upstream;
                      # error message prints the exact re-vendor command
```

```bash
cd headless && npm install          # one-time

PROXY_KEY=... node run.js "QUESTION" \
    --config        ../../tpl/layers-input.json \
    --system-prompt ../../tpl/system-prompt.md \
    --model qwen3 \
    --origin https://tpl.nrp-nautilus.io/agent_runner \
    --transcript runs/tpl-q1-qwen3.json
```

Tag experimental runs with a distinctive `--origin` suffix (e.g. `…/agent_runner`) so they're filterable apart from real user traffic when you later query the logs. See `headless/README.md` for all flags.

### Matrix runs (model × question grids) — run on the cluster

**Always run matrix sweeps as a Kubernetes Job, not locally.** `PROXY_KEY` lives in the `open-llm-proxy-secrets` Secret in the `biodiversity` namespace; on-cluster runs mount it directly, avoiding the local credential dance (stale `/tmp/proxy_key`, 401 sprees, laptop sleep killing a 20-minute matrix mid-run).

`headless/run-matrix-k8s.sh APP_REPO` templates `headless/k8s/matrix-job.yaml` and `kubectl create`s a one-shot Job. The Job clones `open-llm-proxy` + `geo-agent` + the app repo in the sibling layout the runner expects, `npm ci`s, runs `run_matrix.sh`, and dumps per-cell transcripts + `summary.tsv` to pod stdout. Proxy logs (filtered by `ORIGIN`) carry the full request/response pairs.

```bash
# 1. Write questions to a file (one per line, blank/#-comment lines ignored).
cat > headless/runs/log-qs.txt <<'EOF'
How much has the Land and Water Conservation Fund invested in Senate District 2?
how many miles of river have been protected by TPL projects within Tahoe National Forest
EOF

# 2. Launch. APP_REPO is org/repo; QUESTIONS_FILE is local; TAG must be [a-z0-9-]+.
TAG=logqs \
QUESTIONS_FILE=headless/runs/log-qs.txt \
MODELS="qwen3 qwen3-small glm-5 nemotron gemma" \
TRIALS=1 \
  ./headless/run-matrix-k8s.sh boettiger-lab/tpl-ca

# 3. Follow + analyze (commands printed by step 2).
kubectl -n biodiversity logs -f job/<JOB_NAME>
./sync-logs.sh && duckdb -s "... WHERE origin='<ORIGIN>' ..."
```

When asked to "test these questions across these models," reach for this — **do not** fall back to running `run_matrix.sh` locally and **do not** write a bespoke driver script per request. The local `run_matrix.sh` still exists, called by the Job; it is no longer a recommended user-facing entrypoint for matrix work.
