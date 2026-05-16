#!/bin/bash
# Wait for the qwen3-small matrix to finish, then run wetlands-v2 across all
# 5 of its configured models × all 5 welcome.examples × 2 trials.
set -u
cd "$(dirname "$0")"

mkdir -p runs/wetlands-v2

echo "[chain] waiting for qwen3-small matrix to finish..."
until grep -q "ALL THREE APPS COMPLETE" runs/three_apps_qwen3-small.log 2>/dev/null; do
    sleep 120
done
echo "[chain] qwen3-small matrix done. Launching wetlands-v2 matrix."

ORIGIN="https://wetlands-v2.nrp-nautilus.io/agent_runner" \
RUNS_DIR="runs/wetlands-v2" \
bash run_matrix.sh ../../wetlands-v2 2>&1 | tee runs/wetlands-v2/matrix.log
