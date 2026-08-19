#!/usr/bin/env bash
# Fail if headless/mcp-client.js has drifted from geo-agent/app/mcp-client.js.
# This file is a vendored copy (see README for why); whenever upstream ships an
# MCP transport change, re-vendor with `npm run check-drift -- --revendor`.
#
# ONE divergence is intentional and permanent (geo-agent#343/#346): upstream is
# served straight from jsDelivr with no bundler, so it imports a pre-bundled SDK
# by relative path (`./vendor/mcp-sdk.js`) because a static import of an
# unreachable third-party ESM host white-screens the whole app. This runner has
# a node_modules, so it imports the SDK by bare specifier instead. Diffing the
# files verbatim therefore reported drift on every run *forever*, and — worse —
# the re-vendor command it printed would have copied upstream's relative import
# into a package with no `vendor/` directory, breaking the runner outright.
#
# So both sides are normalized before diffing: the SDK import (and any comment
# block attached to it) collapses to a single marker. Every other line, including
# any *other* import upstream adds, is still compared byte-for-byte, so a real
# transport change — a callTool timeout bump, a new reconnect hook, a new
# dependency — still fails this check.

set -euo pipefail

cd "$(dirname "$0")"

# GEO_AGENT_DIR mirrors run.js: point at an isolated checkout instead of the
# shared sibling one (which may be mid-edit on some branch).
GEO_AGENT_DIR="${GEO_AGENT_DIR:-../../geo-agent}"
UPSTREAM="$GEO_AGENT_DIR/app/mcp-client.js"
LOCAL="mcp-client.js"
BANNER_LINES=2  # leading vendor banner (one comment line + one blank) to strip before diffing

# The bare-specifier imports this copy must keep, whatever upstream does.
LOCAL_SDK_IMPORTS=$'import { Client } from \'@modelcontextprotocol/sdk/client/index.js\';\nimport { StreamableHTTPClientTransport } from \'@modelcontextprotocol/sdk/client/streamableHttp.js\';'

if [ ! -f "$UPSTREAM" ]; then
  echo "ERROR: $UPSTREAM not found. Clone boettiger-lab/geo-agent as a sibling of" >&2
  echo "       open-llm-proxy, or set GEO_AGENT_DIR to a checkout." >&2
  exit 2
fi

# The module PATH is allowed to diverge; the set of symbols imported from the SDK
# is not. Compared separately so upstream importing a NEW SDK export still fails
# this check, even though the import statements have different shapes (upstream:
# one combined import from a relative path; here: two bare-specifier imports).
sdk_symbols() {
  grep -E "^import .*(@modelcontextprotocol/sdk|vendor/mcp-sdk)" \
    | sed -e 's/^import[[:space:]]*//' -e 's/[[:space:]]*from.*$//' -e 's/[{}]//g' \
    | tr ',' '\n' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    | grep -v '^$' \
    | sort -u
}

# Collapse the SDK import to a marker, dropping any comment block directly above
# it (the comment explains the import, so it travels with it). Comment lines not
# attached to an SDK import are flushed through and still compared.
normalize() {
  awk '
    /^[[:space:]]*\/\// { pending[++n] = $0; next }
    {
      is_sdk_import = ($0 ~ /^import .*(@modelcontextprotocol\/sdk|vendor\/mcp-sdk)/)
      if (is_sdk_import) {
        n = 0                                  # discard the attached comment block
        if (!marker_emitted) { print "//__MCP_SDK_IMPORT__"; marker_emitted = 1 }
        next
      }
      for (i = 1; i <= n; i++) print pending[i]
      n = 0
      print
    }
    END { for (i = 1; i <= n; i++) print pending[i] }
  '
}

if [ "${1:-}" = "--revendor" ]; then
  # Rebuild the vendored copy: upstream's body, but with OUR import lines.
  {
    head -n "$BANNER_LINES" "$LOCAL"
    normalize < "$UPSTREAM" | awk -v repl="$LOCAL_SDK_IMPORTS" '
      $0 == "//__MCP_SDK_IMPORT__" { print repl; next } { print }
    '
  } > "$LOCAL.new"
  mv "$LOCAL.new" "$LOCAL"
  echo "Re-vendored $LOCAL from $UPSTREAM (kept bare-specifier SDK imports)."
  echo "Review the diff, then commit."
  exit 0
fi

drift=0

if ! diff -u <(sdk_symbols < "$LOCAL") <(sdk_symbols < "$UPSTREAM"); then
  echo
  echo "DRIFT: the set of symbols imported from the MCP SDK differs."
  echo "       Only the module PATH may diverge, not what is imported from it —"
  echo "       --revendor will not fix this, it needs a look by hand."
  drift=1
fi

if ! diff -u <(normalize < <(tail -n +$((BANNER_LINES + 1)) "$LOCAL")) \
             <(normalize < "$UPSTREAM"); then
  echo
  echo "DRIFT: $LOCAL no longer matches $UPSTREAM"
  echo "Re-vendor with:"
  echo "  cd headless && npm run check-drift -- --revendor"
  drift=1
fi

if [ "$drift" -eq 0 ]; then
  echo "OK: $LOCAL is in sync with $UPSTREAM (SDK import path divergence is expected)"
fi
exit "$drift"
