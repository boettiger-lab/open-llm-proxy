#!/usr/bin/env bash
# Sync logs from s3://logs-open-llm-proxy to a local directory using rclone's
# configured `nrp` remote. Subsequent DuckDB queries run against the local
# copy — no S3 secret, no per-query credential expansion.
#
# Usage:
#   ./sync-logs.sh                    # syncs to /tmp/open-llm-proxy-logs
#   ./sync-logs.sh ~/scratch/logs     # syncs to a custom path
#   LOGS_DIR=~/cache/logs ./sync-logs.sh
set -euo pipefail

DEST="${1:-${LOGS_DIR:-/tmp/open-llm-proxy-logs}}"
mkdir -p "$DEST"

rclone sync nrp:logs-open-llm-proxy "$DEST" \
  --fast-list \
  --transfers 16 \
  --checkers 16 \
  --progress

echo
echo "Logs synced to: $DEST"
echo
echo "Query with DuckDB (no credentials needed):"
echo "  duckdb -s \"SELECT ts, entry::JSON->>'user_question' AS q \\"
echo "             FROM read_parquet('$DEST/consolidated/**/*.parquet') \\"
echo "             WHERE ts > now() - INTERVAL 7 DAYS ORDER BY ts DESC LIMIT 20;\""
