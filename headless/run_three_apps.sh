#!/bin/bash
# Wrapper: run nemotron + qwen3 across the three TPL/BOSL apps in sequence.
set -u
cd "$(dirname "$0")"

APPS=(tpl-ca tpl bosl-high-seas)
MATRIX_START="$(date +%s)"

for app in "${APPS[@]}"; do
    if [ ! -d "../../${app}" ]; then
        echo "[skip] ../../$app not found"
        continue
    fi
    echo ""
    echo "########################################################################"
    echo "# ${app}"
    echo "########################################################################"
    MODELS="${MODELS:-nemotron qwen3}" \
    ORIGIN="https://${app}.nrp-nautilus.io/agent_runner" \
    RUNS_DIR="runs/${app}" \
    bash run_matrix.sh "../../${app}"
done

echo ""
echo "=== ALL THREE APPS COMPLETE in $(( $(date +%s) - MATRIX_START ))s ==="
