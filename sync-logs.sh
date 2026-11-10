#!/usr/bin/env bash
# Sync the LLM proxy logs to a local directory, then query the local copy with
# DuckDB — no credential expansion per query, and orders of magnitude faster
# iteration than round-tripping S3.
#
# Reads the rustfs log bucket using a credential scoped to this one bucket,
# Get/List only (#113). That replaces the previous default of rclone's `nrp:`
# remote, which is the single NRP credential and carries read/write/delete on
# *every* NRP bucket — far beyond what reading a few MiB of logs needs.
#
# Credential: $LOGS_READ_KEY / $LOGS_READ_SECRET from the environment, or else
# the `rustfs-logs-read` Secret in $LOGS_SECRET_NAMESPACE (default biodiversity),
# via kubectl. The Secret also names the bucket and endpoint. There is no
# fallback to rclone's `nrp:` remote: that is the general NRP key, and since #178
# the NRP Ceph bucket is a frozen copy that is no longer written.
#
# Usage:
#   ./sync-logs.sh                    # syncs to /tmp/open-llm-proxy-logs
#   ./sync-logs.sh ~/scratch/logs     # syncs to a custom path
#   LOGS_DIR=~/cache/logs ./sync-logs.sh
#   LOGS_SECRET_NAMESPACE=schmidtdse ./sync-logs.sh
#                                     # schmidtdse's bucket, to /tmp/open-llm-proxy-logs-schmidtdse
#
# FRESHNESS: since #178 the proxy writes raw JSONL straight to this bucket every
# minute, and the 03:00 UTC CronJob builds the Parquet tiers. Nothing here needs
# the NRP key.
set -euo pipefail

NAMESPACE="${LOGS_SECRET_NAMESPACE:-biodiversity}"
# A separate default directory per namespace: `rclone sync` deletes local files
# the bucket lacks, so two buckets must never share one directory.
default_dest=/tmp/open-llm-proxy-logs
[ "$NAMESPACE" != biodiversity ] && default_dest="$default_dest-$NAMESPACE"
DEST="${1:-${LOGS_DIR:-$default_dest}}"
BUCKET="${LOGS_BUCKET:-logs-open-llm-proxy}"
ENDPOINT="${LOGS_READ_ENDPOINT:-https://rustfs.nrp-nautilus.io}"
SECRET="${LOGS_READ_SECRET_NAME:-rustfs-logs-read}"

mkdir -p "$DEST"

# 2. Pull the scoped credential from the cluster if it wasn't supplied. Keeps it
#    out of dotfiles and shell history — it only ever lives in this process.
if [ -z "${LOGS_READ_KEY:-}" ] && command -v kubectl >/dev/null 2>&1; then
    if k=$(kubectl -n "$NAMESPACE" get secret "$SECRET" \
             -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' 2>/dev/null | base64 -d 2>/dev/null) && [ -n "$k" ]; then
        LOGS_READ_KEY="$k"
        LOGS_READ_SECRET=$(kubectl -n "$NAMESPACE" get secret "$SECRET" \
             -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' 2>/dev/null | base64 -d)
        ep=$(kubectl -n "$NAMESPACE" get secret "$SECRET" \
             -o jsonpath='{.data.AWS_S3_ENDPOINT}' 2>/dev/null | base64 -d 2>/dev/null || true)
        [ -n "$ep" ] && ENDPOINT="$ep"
        b=$(kubectl -n "$NAMESPACE" get secret "$SECRET" \
             -o jsonpath='{.data.BUCKET}' 2>/dev/null | base64 -d 2>/dev/null || true)
        [ -n "$b" ] && [ -z "${LOGS_BUCKET:-}" ] && BUCKET="$b"
        echo "→ using read-only credential from secret/$SECRET in $NAMESPACE"
    fi
fi

if [ -n "${LOGS_READ_KEY:-}" ]; then
    echo "→ source: $ENDPOINT/$BUCKET (read-only, single-bucket)"
    # Define the remote entirely through env vars so no rclone.conf edit is
    # needed and the secret never touches disk.
    RCLONE_CONFIG_LOGS_TYPE=s3 \
    RCLONE_CONFIG_LOGS_PROVIDER=Other \
    RCLONE_CONFIG_LOGS_ENDPOINT="$ENDPOINT" \
    RCLONE_CONFIG_LOGS_ACCESS_KEY_ID="$LOGS_READ_KEY" \
    RCLONE_CONFIG_LOGS_SECRET_ACCESS_KEY="$LOGS_READ_SECRET" \
    RCLONE_CONFIG_LOGS_FORCE_PATH_STYLE=true \
      rclone sync "logs:$BUCKET" "$DEST" \
        --fast-list --transfers 16 --checkers 16 --progress
else
    echo "✗ No scoped credential found. Set LOGS_READ_KEY/LOGS_READ_SECRET, or get"
    echo "  read access to secret/$SECRET in $NAMESPACE." >&2
    exit 1
fi

echo
echo "Logs synced to: $DEST"
echo
echo "Query with DuckDB (no credentials needed):"
echo "  duckdb -s \"SELECT session_key, turn_idx, user_message_this_turn \\"
echo "             FROM read_parquet('$DEST/sessions/**/*.parquet') \\"
echo "             ORDER BY session_key, turn_idx LIMIT 20;\""
