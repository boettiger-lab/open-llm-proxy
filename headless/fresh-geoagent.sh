#!/bin/bash
# Ensure an isolated, up-to-date geo-agent@main checkout for headless runs, so a
# run never depends on the shared dev checkout (which other agents may be editing
# on branches). Idempotent: clones on first use, otherwise fetch + hard-reset to
# origin/main. Prints the checkout path on stdout so you can wire it into run.js:
#
#   export GEO_AGENT_DIR="$(./fresh-geoagent.sh)"
#   node run.js "…" --config … --system-prompt …
#   # or for a matrix: GEO_AGENT_DIR="$(./fresh-geoagent.sh)" ./run_matrix.sh …
#
# The cache lives outside the repo tree (default ~/.cache/olp-headless/geo-agent;
# override with GEO_AGENT_CACHE) so it can never collide with a sibling dev
# checkout. Only geo-agent's app/*.js (no bare imports) are consumed via
# GEO_AGENT_DIR; mcp-client.js stays vendored here (bare specifier resolution),
# so this script also warns if the vendored copy has drifted from main.
set -euo pipefail

DEST="${GEO_AGENT_CACHE:-$HOME/.cache/olp-headless/geo-agent}"
REPO="${GEO_AGENT_REPO:-https://github.com/boettiger-lab/geo-agent.git}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -d "$DEST/.git" ]; then
  git -C "$DEST" fetch --quiet origin main
  git -C "$DEST" reset --hard --quiet origin/main
else
  mkdir -p "$(dirname "$DEST")"
  git clone --quiet --branch main "$REPO" "$DEST"
fi

# Warn (don't fail) if the locally-vendored mcp-client.js has drifted from main.
if ! diff -q <(tail -n +3 "$HERE/mcp-client.js") "$DEST/app/mcp-client.js" >/dev/null 2>&1; then
  echo "WARN: vendored mcp-client.js has drifted from geo-agent@main — re-vendor with check-drift.sh" >&2
fi

echo "$DEST"
