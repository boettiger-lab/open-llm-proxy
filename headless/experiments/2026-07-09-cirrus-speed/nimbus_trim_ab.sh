#!/usr/bin/env bash
# A/B: does trimming the injected catalog hurt/help NIMBUS (model=qwen, NVFP4)?
# Same gold question, full (40-collection) vs trimmed (3-collection) tpl-ca config,
# N trials each (nimbus tool-call format is stochastic -> trials matter).
set -uo pipefail
cd "$(dirname "$0")/../.."          # -> headless/
SP=/tmp/claude-1000/-home-jovyan-boettiger-lab-open-llm-proxy/26895bd8-4c2d-4fbd-95b5-204cce10bd5c/scratchpad
OUT="experiments/2026-07-09-cirrus-speed/out/nimbus_ab"; mkdir -p "$OUT"
export PROXY_KEY="$(cat /tmp/proxy_key)"
Q="Which programs and agencies have funded land conservation in Congressional District 16, and how much?"
MODEL=qwen
TRIALS=3

run () {  # $1=label $2=config
  for t in $(seq 1 $TRIALS); do
    echo "--- $1 trial $t ---"
    timeout 360 node run.js "$Q" \
      --config "$2" --system-prompt ../../tpl-ca/system-prompt.md \
      --model "$MODEL" --origin "https://benchmark.nrp-nautilus.io/nimbus-$1" \
      --max-turns 14 --run-timeout 320 --trial "$t" \
      --transcript "$OUT/$1-t$t.json" --quiet 2>&1 \
      | grep -E "^\[headless\] [0-9]|run-timeout|Error|error:" | head -4
  done
}

run full "../../tpl-ca/layers-input.json"
run trim "$SP/tpl-ca-trim.json"
echo "DONE-NIMBUS-AB"
