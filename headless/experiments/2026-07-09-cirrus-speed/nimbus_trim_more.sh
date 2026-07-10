#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/../.."
SP=/tmp/claude-1000/-home-jovyan-boettiger-lab-open-llm-proxy/26895bd8-4c2d-4fbd-95b5-204cce10bd5c/scratchpad
OUT="experiments/2026-07-09-cirrus-speed/out/nimbus_ab"
export PROXY_KEY="$(cat /tmp/proxy_key)"
Q="Which programs and agencies have funded land conservation in Congressional District 16, and how much?"
for t in 4 5 6 7; do
  echo "--- trim trial $t ---"
  timeout 360 node run.js "$Q" --config "$SP/tpl-ca-trim.json" --system-prompt ../../tpl-ca/system-prompt.md \
    --model qwen --origin https://benchmark.nrp-nautilus.io/nimbus-trim --max-turns 14 --run-timeout 320 --trial "$t" \
    --transcript "$OUT/trim-t$t.json" --quiet 2>&1 | grep -E "^\[headless\] [0-9]|run-timeout|Error" | head -3
done
echo "DONE-MORE"
