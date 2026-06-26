# Headless geo-agent runner

Reproduces the browser geo-agent app's tool-use loop from the command line, for
scripted model comparisons that hit the LLM proxy the same way a real user
would.

The core framework — `Agent`, `DatasetCatalog`, `ToolRegistry`, `createMapTools`
— is imported directly from the sibling `boettiger-lab/geo-agent` repo
(`../../geo-agent/app/`), so prompt assembly, catalog injection, `get_schema`,
the tool-use loop, and the `<tool_call>` XML parser stay in sync by construction.

Three pieces are intentionally local:

1. **`mcp-client.js`** — vendored byte-for-byte from `../../geo-agent/app/mcp-client.js`
   (with a one-line banner prepended). Vendored, not imported, so the
   `@modelcontextprotocol/sdk` bare specifier resolves against this package's
   `node_modules` rather than the sibling repo's browser import map. **This is the
   drift-prone file.** Re-vendor whenever geo-agent ships an MCP transport change:
   ```bash
   npm run check-drift   # fails non-zero if it has drifted
   ```
   If drift is detected, the error message prints the exact re-vendor command.
2. **`stub-map-manager.js`** — replaces the live MapLibre map. Map tools return
   `success: true` and do nothing; analytical questions still work.
3. **`fetch` wrapper inside `run.js`** — injects the `Origin` header so proxy
   logs can tag headless runs. Node's `fetch` omits `Origin` by default.

## Matrix testing: run on the cluster, not locally

For any (model × question × trial) sweep, use **`run-matrix-k8s.sh`**. It
launches a one-shot Kubernetes Job that pulls `PROXY_KEY` from the
`open-llm-proxy-secrets` Secret already in the `biodiversity` namespace — no
local credential handling, no `/tmp/proxy_key` dance, no laptop sleep
interrupting a 20-minute run.

```bash
# 1. Write the questions to a file (one per line).
cat > runs/trails-q.txt <<'EOF'
which state has the most federal trail miles
EOF

# 2. Launch.
TAG=trails-matrix QUESTIONS_FILE=runs/trails-q.txt \
  ./run-matrix-k8s.sh boettiger-lab/geo-agent-template

# 3. Follow + analyze.
kubectl -n biodiversity logs -f job/<JOB_NAME>     # printed by step 2
./sync-logs.sh && duckdb -s "...WHERE origin='<ORIGIN>'..."  # filter the proxy logs
```

The Job clones `open-llm-proxy`, `geo-agent`, and the app repo in the
sibling layout the runner expects, `npm ci`s, and delegates to
`run_matrix.sh`. The per-cell JSON transcripts and `summary.tsv` are dumped
to the pod's stdout at the end, so `kubectl logs job/<JOB_NAME>` is
self-sufficient. Proxy logs (filtered by ORIGIN) carry the full
request/response pairs.

### Required env / flags

| Var | Notes |
|---|---|
| `APP_REPO` (positional) | `org/repo` of the geo-agent app (e.g. `boettiger-lab/geo-agent-template`) |
| `QUESTIONS_FILE` | Local file, one question per line. Read once and base64'd into the Job env. |
| `TAG` | `[a-z0-9-]+` identifier — becomes `JOB_NAME` suffix and `ORIGIN` query tag |
| `MODELS` | Optional override. Default: read from the app's `k8s/configmap.yaml` inside the pod |
| `TRIALS` / `MAX_TURNS` / `APP_BRANCH` / `NAMESPACE` | Optional; sensible defaults |
| `GEO_AGENT_BRANCH` | Optional; `boettiger-lab/geo-agent` branch to clone (default `main`). The runner imports its framework from this checkout, so set it to A/B-test a code-level geo-agent change on the open models before it ships in a pinned release. Pair with a second run on `main` for the baseline. |

## Local single-run usage (ad-hoc debugging only)

For iterating on the runner code itself, or reproducing one specific failure
once, you can run `run.js` directly against the proxy. **Do not use this for
matrix testing — use the k8s Job above.**

### Requirements

- Node 22+ (24 LTS recommended; the cluster Job uses 24)
- `boettiger-lab/geo-agent` cloned as a sibling (`../../geo-agent/`)
- A valid proxy key, via `PROXY_KEY` env var or `--api-key`

### Install

```bash
cd headless
npm install
```

### Run one question

```bash
PROXY_KEY=... node run.js "Which New Jersey municipalities have passed conservation ballot measures?" \
    --config        ../../tpl/layers-input.json \
    --system-prompt ../../tpl/system-prompt.md \
    --model         qwen3 \
    --origin        https://tpl.nrp-nautilus.io/agent_runner \
    --transcript    runs/tpl-q3-qwen3.json
```

Key flags:

| Flag | Default | Notes |
|---|---|---|
| `--config` | _(required)_ | App's `layers-input.json` — collections, MCP URL, catalog URL |
| `--system-prompt` | _(required)_ | App's `system-prompt.md` — the catalog section is appended automatically |
| `--model` | `config.llm_model` or `qwen3` | Must be a model the proxy knows |
| `--origin` | _(none)_ | Set this so logs are tagged and filterable |
| `--proxy-endpoint` | `https://open-llm-proxy.nrp-nautilus.io/v1` | |
| `--max-turns` | 20 | Mirrors browser `Agent.maxToolCalls` |
| `--transcript` | _(none)_ | Write full JSON transcript for offline comparison |
| `--quiet` | off | Suppress per-turn output |

## What this runner does vs. the browser

Matches:
- System prompt = `system-prompt.md` + `catalog.generatePromptCatalog()` + MCP `geospatial-analyst` prompt
- Local tools: `get_schema`, `list_datasets`, and all map-control tools (stubbed)
- Remote tools: whatever the MCP server advertises (incl. `register_hex_tiles`, `get_hex_tile_status`)
- Tool-use loop, 12-message context window, 4K tool-result truncation, `<tool_call>` XML fallback for models that don't emit structured calls
- MCP transport: same vendored `mcp-client.js` (10-min `callTool` timeout for hex pyramid builds, reconnect-aware tool-registry refresh wired in `run.js`)

Differs:
- Map tools return success but do nothing (no real map). Fine for analytical questions; the LLM behaves as if the map action worked.
- No `get_drawn_region` tool (no user-drawn polygon in headless mode).
- `Origin` header is injected via a `fetch` monkey-patch (Node's `fetch` omits it by default).

## Tagging runs in the proxy logs

The proxy uses the `Origin` header as the log key. Pass a distinct origin for
experimental runs so they're filterable apart from real user traffic — e.g.
`https://tpl.nrp-nautilus.io/agent_runner` is still greppable as `tpl` but
won't pollute analyses of production UI sessions.

Query your runs out of the logs (after `./sync-logs.sh`):

```sql
SELECT ts, entry::JSON->>'user_question' AS q,
       entry::JSON->'tool_calls' AS tools
FROM read_parquet('/tmp/open-llm-proxy-logs/consolidated/**/*.parquet')
WHERE origin = 'https://tpl.nrp-nautilus.io/agent_runner'
ORDER BY ts;
```
