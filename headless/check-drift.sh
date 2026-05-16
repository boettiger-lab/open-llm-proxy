#!/usr/bin/env bash
# Fail if headless/mcp-client.js has drifted from geo-agent/app/mcp-client.js.
# This file is a vendored copy (see README for why); whenever upstream ships an
# MCP transport change, re-vendor and update this script's banner-line count if
# the leading banner block changes shape.

set -euo pipefail

UPSTREAM="../../geo-agent/app/mcp-client.js"
LOCAL="mcp-client.js"
BANNER_LINES=2  # leading vendor banner (one comment line + one blank) to strip before diffing

cd "$(dirname "$0")"

if [ ! -f "$UPSTREAM" ]; then
  echo "ERROR: $UPSTREAM not found. Clone boettiger-lab/geo-agent as a sibling of open-llm-proxy." >&2
  exit 2
fi

if diff -u <(tail -n +$((BANNER_LINES + 1)) "$LOCAL") "$UPSTREAM"; then
  echo "OK: $LOCAL is in sync with $UPSTREAM"
  exit 0
else
  echo
  echo "DRIFT: $LOCAL no longer matches $UPSTREAM"
  echo "Re-vendor with:"
  echo "  cd headless && { head -n $BANNER_LINES $LOCAL; cat $UPSTREAM; } > $LOCAL.new && mv $LOCAL.new $LOCAL"
  exit 1
fi
