#!/usr/bin/env bash
# Create the Secrets the schmidtdse proxy needs, copied from biodiversity.
# Prints names only, never values. Safe to re-run; it overwrites the copies.
#
#   ./schmidtdse/make-secrets.sh            # dry run: report what would change
#   ./schmidtdse/make-secrets.sh --apply
#
# Client keys: the schmidtdse proxy accepts only the apps that run in
# schmidtdse. Their names come from schmidtdse/open-llm-proxy-app-keys (the
# Secret each app's nginx reads); their `tier/name:key` lines, tier included,
# are taken from biodiversity/open-llm-proxy-client-keys so both proxies agree.
set -euo pipefail

SRC=biodiversity
DST=schmidtdse
APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true
DRY=(--dry-run=server)
$APPLY && DRY=()

ctx=$(kubectl config current-context)
echo "kubectl context: $ctx   $SRC -> $DST   apply=$APPLY"

# Keep only what defines the Secret; drop namespace and server-set metadata.
strip() {
  python3 -c 'import json,sys
o=json.load(sys.stdin)
print(json.dumps({"apiVersion":"v1","kind":"Secret","type":o.get("type","Opaque"),
                  "metadata":{"name":o["metadata"]["name"]},"data":o["data"]}))'
}

# 1. Provider and S3 credentials, copied whole.
for s in openrouter-key nimbus-api-key anthropic-api-key aws; do
  kubectl -n "$SRC" get secret "$s" -o json | strip \
    | kubectl -n "$DST" apply "${DRY[@]}" -f - >/dev/null
  echo "copied secret/$s"
done

# 2. open-llm-proxy-secrets already exists in schmidtdse (cache-salt,
#    nrp-api-key, proxy-key). Add the keys it lacks; leave the rest alone,
#    since app nginx configs may read proxy-key from it.
for k in client-ip-salt redis-password; do
  v=$(kubectl -n "$SRC" get secret open-llm-proxy-secrets -o jsonpath="{.data.$k}")
  kubectl -n "$DST" patch secret open-llm-proxy-secrets "${DRY[@]}" --type merge \
    -p "{\"data\":{\"$k\":\"$v\"}}" >/dev/null
  echo "set open-llm-proxy-secrets/$k"
done

# 3. Client keys for the schmidtdse apps only.
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
kubectl -n "$SRC" get secret open-llm-proxy-client-keys -o jsonpath='{.data.keys}' | base64 -d > "$tmp/all"
kubectl -n "$DST" get secret open-llm-proxy-app-keys -o json \
  | python3 -c 'import json,sys,base64
for k,v in json.load(sys.stdin)["data"].items(): print(k, base64.b64decode(v).decode().strip())' > "$tmp/apps"

: > "$tmp/keys"
missing=0
while read -r name key; do
  # Match the line by key, so a tier prefix or a renamed entry cannot slip through.
  line=$(grep -v '^[[:space:]]*#' "$tmp/all" | grep -F ":$key" | head -1 || true)
  if [[ -z "$line" ]]; then
    echo "MISSING: app key for '$name' is not in $SRC/open-llm-proxy-client-keys"
    missing=1
  else
    echo "$line" >> "$tmp/keys"
    echo "client key: ${line%%:*}"
  fi
done < "$tmp/apps"
[[ $missing == 0 ]] || { echo "Refusing to continue: fix the missing keys first."; exit 1; }

kubectl -n "$DST" create secret generic open-llm-proxy-client-keys \
  --from-file=keys="$tmp/keys" --dry-run=client -o yaml \
  | kubectl -n "$DST" apply "${DRY[@]}" -f - >/dev/null
echo "wrote open-llm-proxy-client-keys ($(wc -l < "$tmp/keys") keys)"
