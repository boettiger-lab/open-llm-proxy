# Standing baseline question set (#40)

The durable, version-controlled golden-answer set that **every guidance change is
regression-tested against** before it ships to prod. Closes the core of #40; seeded
from the open-model benchmark in
[`../experiments/2026-06-26-or-openmodel-bench/`](../experiments/2026-06-26-or-openmodel-bench).

## The gate (two-part "done" for any guidance change)
1. **The targeted trap is fixed** — the question that exposed it now passes.
2. **No regression** on this baseline — every other question scores ≥ its prior mark.

Scoring is **per-question (instance-level)**, not an aggregate — aggregate accuracy
hides the trap questions (see #42). Gold is **operator-verified** via our own
`duckdb-geo` queries, *never* model consensus (consensus can confirm a shared wrong
answer — the failure mode we explicitly guard against).

> **Targeting (critical):** validation runs MUST hit **dev** MCP
> (`dev-duckdb-mcp.nrp-nautilus.io`), which serves the candidate `:main` guidance —
> not prod. `headless/run.js` defaults to prod, so pass `--mcp-url` (or set
> `config.mcp_url`) explicitly for a validation run. Encoded in `golden.json → gate`.

## Files
- `golden.json` — the manifest: per question → `gold` (checkable answer), `accept`
  (pass rule), `trap` (the rule it guards — the #42 rule-store key), `sql_ref`
  (authoritative SQL in `gold/`), and `bench_mean_acc` (first-run difficulty).
- `gold/<app>.md` — verified answers **with authoritative SQL** (so transcripts are
  checked mechanically, not by eye).
- `questions.txt` — flat question set; `questions/<app>.txt` — per-app (matrix input).
- `build_golden.py` — regenerates `golden.json` + `questions.txt` after edits.

## Grow-on-fix
When a guidance change fixes a *new* trap, add the question that exposed it here
(with gold + SQL + a `trap` tag) so future changes can't silently reintroduce the
regression. Each `trap` tag is the link to #42's itemized rule store: one rule ↔ the
question(s) that justify it.

## Trap-rule seeds (hardest first — prime geo-agent-training targets)
From the first benchmark, mean accuracy across 4 open models:

| bench acc | question | trap(s) it guards |
|--:|---|---|
| 0.19 | wet-top10-hydrobasins-composite | normalization-method; ncp-intensity-vs-extensive; rank-sensitivity |
| 0.62 | bosl-fishing-displaced | mask-before-aggregate; units-fishing-hours; multi-year-pick |
| 0.62 | glob-least-represented-ecoregions | area-share-not-count; zero-tie; min-cell-threshold |
| 0.62 | tplca-cd-ballot-funding-2010 | **statewide-measure-attribution**; dedup-landvote-id |
| 0.69 | tplca-cd-failed-measures | statewide-measure-attribution; status-fail-filter |

The mid-pack traps (statewide-measure-attribution, multi-year-pick) are prime
candidates for #42's runtime *result-shape advisories* rather than frozen prose.

## Run
```bash
cd headless
# per app, against DEV mcp; TRIALS small (gate runs on every change)
TAG=gate TRIALS=2 GEO_AGENT_BRANCH=main \
QUESTIONS_FILE=baseline/questions/<app>.txt \
  ./run-matrix-k8s.sh boettiger-lab/<app>   # ensure the app config points at dev MCP
```
Grade each model answer against `golden.json` `gold`/`accept`; gold SQL in `gold/`.
