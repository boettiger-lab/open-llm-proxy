# Full context artifact — tpl-ca CD-16, full 40-collection config

Captured via `run.js` with `DUMP_FULL=` (nimbus `qwen`, 8 LLM calls, correct answer).

## Files
- **`full-context-tpl-ca-cd16.json`** — the artifact. Shape:
  - `.tools` — the 24 tool schemas sent on **every** call (OpenAI `tools` param), untruncated.
  - `.calls[i].messages` — the **full, untruncated** messages array sent on LLM call `i`
    (system + user + assistant/tool history). Call `i+1` = call `i` + one assistant turn + its tool result(s), so the growth is visible turn over turn.
- `full-context-tpl-ca-cd16.transcript.json` — the run.js transcript (per-call `usage`, timings, tool sequence).

## Measured token breakdown (via proxy `prompt_tokens` probes, model=qwen)

Cold first-call prefill ≈ **33,937 tok**, split almost evenly:

| component | tokens | share | owner |
|--|--:|--:|--|
| System prompt (app prose + 40-collection **catalog**) | 17,793 | ~52% | **geo-agent client** (`dataset-catalog.generatePromptCatalog`) |
| Tool schemas (24 tools, `tools` param) | 16,144 | ~48% | mixed (see below) |
| &nbsp;&nbsp;— `query` tool description alone | 8,348 | ~25% | **MCP** (mcp-data-server `query` def) — affects ALL consumers |
| &nbsp;&nbsp;— other 23 tools (18 map tools + 5 MCP) | ~7,800 | ~23% | mixed: map tools = client, browse/get_* = MCP |

> ⚠️ `usage.prompt_tokens` in run.js transcripts counts the **messages only**, not the
> `tools` param — but the tools ARE sent and processed (+16,144 tok confirmed by
> with/without probe). So the true prefill is ~2× the reported `prompt_tokens` on turn 1.

Per-call `usage.prompt_tokens` (messages only): 33962 → 38507 → 40381 → 40987 → 42710 → 43374 → 43733 → 44033. The growth (~10k over 8 calls) is the accumulating `get_schema`/`query` tool-result suffix.

## Redundancy to look for in the artifact
1. **Catalog vs get_schema duplication** — each dataset's STAC `description` appears in the
   system-prompt catalog AND again in its `get_schema` result.
2. **Within a single `get_schema` result** — the full column schema + every coded-value
   enumeration is repeated across the flat-asset and hex-asset blocks (e.g. `pad-us-4.1-fee`
   lists all 56 state codes + 46 designation codes twice).
3. **`query` tool description** — ~8.3k tok of SQL guidance re-sent on every one of the 8 calls.
4. **Map-tool schemas** — 18 map tools (~8k tok) sent on an analysis-only question that never touches the map.
