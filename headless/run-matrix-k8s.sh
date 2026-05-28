#!/bin/bash
# run-matrix-k8s.sh — launch the headless matrix as a one-shot Kubernetes Job.
#
# Why this is the default: the proxy key lives in the cluster (Secret
# `open-llm-proxy-secrets`, key `PROXY_KEY`). Running on-cluster mounts it
# directly — no local credential handling, no `/tmp/proxy_key` dance.
#
# Usage:
#   QUESTIONS_FILE=... TAG=... ./run-matrix-k8s.sh APP_REPO
#
# Positional:
#   APP_REPO        org/repo of the geo-agent app (e.g. boettiger-lab/geo-agent-template)
#
# Required env:
#   QUESTIONS_FILE  path to a file with one question per line (read locally,
#                   base64-encoded into the Job env)
#   TAG             short suffix to identify this run; appears in JOB_NAME and
#                   ORIGIN. Must match [a-z0-9-]+ (e.g. trails-mileage).
#
# Optional env:
#   MODELS          space-separated model override (default: read from
#                   APP_REPO/k8s/configmap.yaml inside the pod)
#   TRIALS          trials per (model, question) (default 2)
#   MAX_TURNS       agent maxToolCalls (default 20)
#   APP_BRANCH      app repo branch to clone (default main)
#   NAMESPACE       k8s namespace (default biodiversity)
#
# After applying, the script prints the JOB_NAME and the kubectl/duckdb
# follow-up commands. The Job logs (kubectl logs job/$JOB_NAME) include the
# per-cell JSON transcripts and the summary.tsv on completion; the proxy logs
# (filterable by ORIGIN) carry the full request/response pairs for analysis.

set -euo pipefail
cd "$(dirname "$0")"

APP_REPO="${1:-}"
if [ -z "$APP_REPO" ]; then
    echo "ERROR: APP_REPO positional argument required (e.g. boettiger-lab/geo-agent-template)" >&2
    exit 2
fi
APP_NAME="$(basename "$APP_REPO")"

: "${QUESTIONS_FILE:?QUESTIONS_FILE is required}"
: "${TAG:?TAG is required (short identifier, [a-z0-9-]+)}"
[ -s "$QUESTIONS_FILE" ] || { echo "ERROR: $QUESTIONS_FILE missing or empty" >&2; exit 2; }
if ! [[ "$TAG" =~ ^[a-z0-9-]+$ ]]; then
    echo "ERROR: TAG must match [a-z0-9-]+ (got: '$TAG')" >&2
    exit 2
fi

MODELS="${MODELS:-}"
TRIALS="${TRIALS:-2}"
MAX_TURNS="${MAX_TURNS:-20}"
APP_BRANCH="${APP_BRANCH:-main}"
NAMESPACE="${NAMESPACE:-biodiversity}"

TS="$(date -u +%Y%m%d-%H%M%S)"
JOB_NAME="hmx-${APP_NAME}-${TAG}-${TS}"
JOB_NAME="${JOB_NAME:0:63}"

# ORIGIN convention: <app>.nrp-nautilus.io/agent_runner_<tag>
ORIGIN="https://${APP_NAME}.nrp-nautilus.io/agent_runner_${TAG//-/_}"

QUESTIONS_B64="$(base64 -w0 < "$QUESTIONS_FILE")"

export APP_REPO APP_BRANCH APP_NAME JOB_NAME ORIGIN TAG TRIALS MAX_TURNS MODELS QUESTIONS_B64

envsubst < k8s/matrix-job.yaml | kubectl -n "$NAMESPACE" create -f -

cat <<EOF

job:    $JOB_NAME
origin: $ORIGIN

follow:
  kubectl -n $NAMESPACE logs -f job/$JOB_NAME

filter proxy logs (after ./sync-logs.sh):
  duckdb -s "SELECT timestamp, list_transform(tool_calls, x->x.name) AS tools,
                    SUBSTR(content_preview, 1, 200) AS preview
             FROM read_ndjson_auto('/tmp/open-llm-proxy-logs/*/*.jsonl', union_by_name=true)
             WHERE type='response' AND origin='$ORIGIN'
             ORDER BY timestamp;"
EOF
