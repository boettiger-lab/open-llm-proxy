#!/usr/bin/env bash
# Create the Secrets the schmidtdse proxy needs, copied from biodiversity.
# Prints names only, never values. Safe to re-run; it overwrites the copies.
#
#   set -a; . ~/.env; set +a                # provides SCHMIDTDSE_OPENROUTER_KEY
#   ./schmidtdse/make-secrets.sh            # dry run: report what would change
#   ./schmidtdse/make-secrets.sh --apply
#
# OpenRouter: schmidtdse has its own OpenRouter key, so its spend is billed and
# capped apart from biodiversity's. It comes from SCHMIDTDSE_OPENROUTER_KEY.
#
# Client keys: one per tier for the whole namespace, not one per app. Every
# pod in a namespace can read every Secret in it, so per-app keys never kept
# apps apart. glen-ops mints them in schmidtdse/open-llm-proxy-namespace-keys
# (`public`, `private`). An app gets the private key only if it sits behind a
# login. Each app's nginx sends `X-Glen-App`, which the proxy logs as `app`.
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

# 1. OpenRouter: schmidtdse's own key, never biodiversity's.
: "${SCHMIDTDSE_OPENROUTER_KEY:?set SCHMIDTDSE_OPENROUTER_KEY (see ~/.env)}"
kubectl -n "$DST" create secret generic openrouter-key \
  --from-literal=OPENROUTER_KEY="$SCHMIDTDSE_OPENROUTER_KEY" --dry-run=client -o yaml \
  | kubectl -n "$DST" apply "${DRY[@]}" -f - >/dev/null
echo "set secret/openrouter-key from SCHMIDTDSE_OPENROUTER_KEY"

# 2. Nimbus, copied whole. (Logs use rustfs-logs-write, minted by glen-ops for
#    this bucket only; the general NRP `aws` credential is not copied.)
for s in nimbus-api-key; do
  kubectl -n "$SRC" get secret "$s" -o json | strip \
    | kubectl -n "$DST" apply "${DRY[@]}" -f - >/dev/null
  echo "copied secret/$s"
done

# 3. open-llm-proxy-secrets already exists in schmidtdse (cache-salt,
#    nrp-api-key, proxy-key). Add the keys it lacks; leave the rest alone,
#    since app nginx configs may read proxy-key from it.
for k in client-ip-salt redis-password; do
  v=$(kubectl -n "$SRC" get secret open-llm-proxy-secrets -o jsonpath="{.data.$k}")
  kubectl -n "$DST" patch secret open-llm-proxy-secrets "${DRY[@]}" --type merge \
    -p "{\"data\":{\"$k\":\"$v\"}}" >/dev/null
  echo "set open-llm-proxy-secrets/$k"
done

# 4. Client keys: the namespace's two tier keys, named public/schmidtdse and
#    private/schmidtdse so key_name in the logs shows namespace and tier.
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
kubectl -n "$DST" get secret open-llm-proxy-namespace-keys -o json \
  | python3 -c 'import json,sys,base64
d=json.load(sys.stdin)["data"]
for tier in ("public","private"):
    v=base64.b64decode(d[tier]).decode().strip()
    assert len(v)>=32 and ":" not in v and "\n" not in v, tier
    print(f"{tier}/schmidtdse:{v}")' > "$tmp/keys"
cut -d: -f1 "$tmp/keys" | sed 's/^/client key: /'

kubectl -n "$DST" create secret generic open-llm-proxy-client-keys \
  --from-file=keys="$tmp/keys" --dry-run=client -o yaml \
  | kubectl -n "$DST" apply "${DRY[@]}" -f - >/dev/null
echo "wrote open-llm-proxy-client-keys ($(wc -l < "$tmp/keys") keys)"
