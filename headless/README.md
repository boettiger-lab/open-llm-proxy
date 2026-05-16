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

## Requirements

- Node 20+
- `boettiger-lab/geo-agent` cloned as a sibling (`../../geo-agent/`)
- A valid proxy key (same one the app uses), via `PROXY_KEY` env var or `--api-key`

## Install

```bash
cd headless
npm install
```

## Usage

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
