# OpenRouter open-model performance/accuracy benchmark — 2026-06-26

Per-question **timing** and **accuracy** evaluation of four OpenRouter-hosted open
models on the standing geo-agent example-question set (the `sweep-*.txt` snapshot),
filtered to analytical questions (map-visual-only steps dropped).

## Models under test (OpenRouter ids)

| short | OpenRouter id |
|---|---|
| glm-5.2 | `z-ai/glm-5.2` |
| nemotron-3-ultra | `nvidia/nemotron-3-ultra-550b-a55b` |
| minimax-m3 | `minimax/minimax-m3` |
| kimi-2.7-code | `moonshotai/kimi-k2.7-code` |

All four confirmed ZDR-reachable. The proxy routes them via the `z-ai/`, `nvidia/`,
`minimax/`, `moonshotai/` OpenRouter prefixes (added in open-llm-proxy PR #46). ZDR
is enforced by the OpenRouter account/key policy (not per-request).

## Ground truth (accuracy backbone)

Gold answers in [`gold/`](gold/) are computed **independently** by the orchestrating
operator via direct `duckdb-geo` MCP / SQL queries against the same S3 parquet the
apps use — **not** by consensus across the models and **not** by any single agent
run. Each model's transcript answer is graded per-question against gold. Accuracy is
scored on a separate axis from timing; a timeout/error is a timing+completion failure,
graded separately from a wrong-but-complete answer.

## Question set

`questions/<app>.txt` — analytical questions only (see [`../../runs/sweep-*.txt`](../../runs)
for the full standing set incl. dropped map-visual steps). Mixed questions
("…and show on map") keep the analytical clause; only that clause is graded.
Two capability/meta questions ("tell me about the datasets…") were dropped — no
objective gold. **22 questions across 7 apps.**

| app | APP_REPO | analytical Qs |
|---|---|--:|
| biodiversity | boettiger-lab/biodiversity | 2 |
| bosl-high-seas | boettiger-lab/bosl-high-seas | 2 |
| ca-30x30 | boettiger-lab/ca-30x30 | 2 |
| global-30x30 | boettiger-lab/global-30x30 | 4 |
| tpl-ca | boettiger-lab/tpl-ca | 5 |
| tpl | boettiger-lab/tpl | 4 |
| wetlands | boettiger-lab/wetlands-v2 | 3 |

## Parameters

- TRIALS = 3 per (model × question)
- MAX_TURNS = 20 (runner default)
- ORIGIN tag = `agent_runner_orbench` → origins `https://<app>.nrp-nautilus.io/agent_runner_orbench`
- Total runs = 22 Qs × 4 models × 3 trials = **264 agent sessions**

## Reproduce

```bash
cd open-llm-proxy/headless
E=experiments/2026-06-26-or-openmodel-bench
MODELS="z-ai/glm-5.2 nvidia/nemotron-3-ultra-550b-a55b minimax/minimax-m3 moonshotai/kimi-k2.7-code"

# one k8s Job per app (parallel on the cluster)
declare -A REPO=(
  [biodiversity]=boettiger-lab/biodiversity
  [bosl-high-seas]=boettiger-lab/bosl-high-seas
  [ca-30x30]=boettiger-lab/ca-30x30
  [global-30x30]=boettiger-lab/global-30x30
  [tpl-ca]=boettiger-lab/tpl-ca
  [tpl]=boettiger-lab/tpl
  [wetlands]=boettiger-lab/wetlands-v2
)
for app in "${!REPO[@]}"; do
  TAG=orbench MODELS="$MODELS" TRIALS=3 \
  QUESTIONS_FILE="$E/questions/$app.txt" \
    ./run-matrix-k8s.sh "${REPO[$app]}"
done
```

Then `./sync-logs.sh` and `python3 $E/analyze.py` for the per-question timing +
accuracy table.

## Files

- `questions/<app>.txt` — analytical question sets (input)
- `gold/<app>.md` — verified gold answers + the SQL/MCP queries used to derive them
- `results/` — synced analysis outputs (per-question timing + accuracy)
- `analyze.py` — pulls the `orbench` origin rows from the synced logs, computes
  per-(model × question) latency stats + tool-call counts; accuracy is merged in
  from manual grading against `gold/`.
