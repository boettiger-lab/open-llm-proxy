#!/bin/bash
# Full nemotron-NVFP4 matrix: 4 production apps × 2 trials, on the patched runner.
# Mirrors the marlin baseline (runs/{padus,tpl-ca,tpl,bosl-high-seas}/) so we
# can do an apples-to-apples comparison across the full real-use workload.
set -u
cd "$(dirname "$0")"

# (repo_path, deployment_hostname, runs_subdir)
APPS=(
    "../../tpl-ca|tpl-ca|tpl-ca-nvfp4"
    "../../tpl|tpl|tpl-nvfp4"
    "../../bosl-high-seas|bosl-high-seas|bosl-high-seas-nvfp4"
    "../../geo-agent-template|padus|padus-nvfp4"
)

START="$(date +%s)"
for entry in "${APPS[@]}"; do
    IFS='|' read -r app_path host runs_name <<< "$entry"
    if [ ! -d "$app_path" ]; then
        echo "[skip] $app_path not found"; continue
    fi
    mkdir -p "runs/${runs_name}"
    echo ""
    echo "########################################################################"
    echo "# ${runs_name}  (origin=${host})"
    echo "########################################################################"
    MODELS="nemotron" \
    TRIALS=2 \
    ORIGIN="https://${host}.nrp-nautilus.io/agent_runner_nvfp4" \
    RUNS_DIR="runs/${runs_name}" \
    bash run_matrix.sh "$app_path"
done

echo ""
echo "=== ALL NVFP4 APPS COMPLETE in $(( $(date +%s) - START ))s ==="
