# Re-run runbook (orbench2) — PREPARED, NOT YET RUN

Second pass of the OpenRouter open-model benchmark, fixing the three things that
limited the first run. **Do not launch until the preflight checklist passes.**

## Why re-run
1. First run cloned geo-agent `main` *before* #271 merged → it used the bloated
   pre-#271 tool descriptions (~35–40k prefill/turn). Current `main` includes #271
   (~9.5k/turn smaller) — this run is the clean "after".
2. First run lost 82/264 trials to the OpenRouter **monthly budget cap**.
3. Per-cell transcripts died with the pods (no durable capture). Now fixed —
   the matrix job uploads them to S3 (`CAPTURE_TRANSCRIPTS=true`, default).

## Preflight checklist (all must be true before launching)
- [ ] **OpenRouter monthly cap raised / has headroom.** Admin checks org member
      monthly $ limit + month-to-date spend (the first run cost ~$10–15; budget for
      ~2× with full trial coverage). Cap is a *dollar* limit, aggregate across models.
- [ ] **Proxy #47 deployed.** `seed`/`top_p`/`provider` passthrough is merged
      (commit 77b6f04) but reaches pods only after a rollout restart:
      `kubectl -n biodiversity rollout restart deployment/open-llm-proxy`
      then confirm the new pods are up. (Lets geo-agent's seed actually reach providers.)
- [ ] **geo-agent `main` includes #271** (it does, merged 2026-06-27 00:33) — the
      default `GEO_AGENT_BRANCH=main` picks it up. No flag needed.
- [ ] `./sync-logs.sh` works locally for post-run analysis.

## Launch (one Job per app — DO NOT run until preflight passes)
```bash
cd open-llm-proxy/headless
E=experiments/2026-06-26-or-openmodel-bench
MODELS="z-ai/glm-5.2 nvidia/nemotron-3-ultra-550b-a55b minimax/minimax-m3 moonshotai/kimi-k2.7-code"
declare -A REPO=(
  [biodiversity]=boettiger-lab/biodiversity   [bosl-high-seas]=boettiger-lab/bosl-high-seas
  [ca-30x30]=boettiger-lab/ca-30x30           [global-30x30]=boettiger-lab/global-30x30
  [tpl-ca]=boettiger-lab/tpl-ca               [tpl]=boettiger-lab/tpl
  [wetlands]=boettiger-lab/wetlands-v2 )
for app in biodiversity bosl-high-seas ca-30x30 global-30x30 tpl-ca tpl wetlands; do
  TAG=orbench2 \
  MODELS="$MODELS" \
  TRIALS=2 \
  CAPTURE_TRANSCRIPTS=true \
  QUESTIONS_FILE="$E/questions/$app.txt" \
    ./run-matrix-k8s.sh "${REPO[$app]}"
done
```
Changes from run 1: `TAG=orbench2` (new origin `agent_runner_orbench2` — disjoint from
`orbench`), **`TRIALS=2`** (run-1 analysis showed 3 wasn't worth the cost — 73% of
multi-trial cells agreed; 2 captures the variance), and durable transcript capture on.

## Capture (now durable)
Each Job uploads its per-cell transcript JSON + `summary.tsv` to
`s3://logs-open-llm-proxy/experiments/<JOB_NAME>/`. `./sync-logs.sh` pulls them to
`/tmp/open-llm-proxy-logs/experiments/`. (They're also still in `kubectl logs job/<name>`
for ~24h.) Transcripts add what the proxy logs lack: **llm-vs-tool time split,
per-tool `tool_exec_ms`, `timed_out`/`cancelled` disposition, and clean (model,q,trial)
keys.** Proxy logs add what transcripts lack: **cost ($), cached_tokens, serving provider.**

## Analyze (after `./sync-logs.sh`)
```bash
python3 $E/build_report.py            # per-model + per-question accuracy/timing/calls
# accuracy: re-grade via the per-app judge agents against gold/ (unchanged gold)
```
Then compare orbench2 vs orbench: prefill/turn should drop ~24% (the #271 win), cost
per question should fall (smaller prefill + better cache hits if #47 routing is used),
and accuracy should be ≥ run 1 with full 2-trial coverage (no budget gaps).

## Reuse
Gold answers in `gold/` are unchanged (same questions) — no need to recompute.
Grades from run 1 are in `results/grades.raw` for reference but must be re-done for
the new answers.
