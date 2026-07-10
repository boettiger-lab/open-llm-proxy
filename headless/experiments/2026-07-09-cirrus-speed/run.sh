#!/usr/bin/env bash
# Benchmark qwen3-6 (qwen3-cirrus.carlboettiger.info) speed on our gold questions.
# Runs each gold question through the full agent loop (run.js -> proxy -> cirrus),
# bracketed by server-side vLLM /metrics snapshots so we get the EXACT prefill/decode
# split over precisely this window (cirrus is not scraped by NRP Prometheus).
set -uo pipefail
cd "$(dirname "$0")/../.."          # -> headless/
EXP="experiments/2026-07-09-cirrus-speed"
OUT="$EXP/out"; mkdir -p "$OUT"
export PROXY_KEY="$(cat /tmp/proxy_key)"
ORIGIN="https://benchmark.nrp-nautilus.io/cirrus-goldspeed"
MODEL=qwen3-6
SIB=../..

# app|question
QS=(
"ca-30x30|How many acres of California land are conserved at GAP status 1 or 2?"
"ca-30x30|Which ecoregion has the most conserved acreage?"
"tpl-ca|Which programs and agencies have funded land conservation in Congressional District 16, and how much?"
"tpl-ca|How much has the Land and Water Conservation Fund invested in Senate District 2?"
"bosl-high-seas|How many seamounts are there in the Sargasso Sea \"Ecologically or Biologically Significant Marine Area\" that was designated on the high seas?"
"tpl|Who has funded land conservation in Boulder County, Colorado? show areas on the map."
)

echo "=== metrics snapshot BEFORE ==="
python3 cirrus_metrics.py snap "$OUT/snap_before.json"

i=0
for entry in "${QS[@]}"; do
  i=$((i+1))
  app="${entry%%|*}"; q="${entry#*|}"
  tag=$(printf "q%02d-%s" "$i" "$app")
  echo ""
  echo "===================== [$i/${#QS[@]}] $app ====================="
  echo "Q: $q"
  timeout 480 node run.js "$q" \
    --config        "$SIB/$app/layers-input.json" \
    --system-prompt "$SIB/$app/system-prompt.md" \
    --model "$MODEL" --origin "$ORIGIN" \
    --max-turns 14 --run-timeout 420 \
    --transcript "$OUT/$tag.json" --quiet 2>&1 \
    | grep -E "^\[headless\] [0-9]|ANSWER|run-timeout|Error|error:" | head -8
done

echo ""
echo "=== metrics snapshot AFTER ==="
python3 cirrus_metrics.py snap "$OUT/snap_after.json"
echo ""
python3 cirrus_metrics.py diff "$OUT/snap_before.json" "$OUT/snap_after.json" | tee "$OUT/split.txt"
echo ""
echo "DONE. transcripts in $OUT/"
