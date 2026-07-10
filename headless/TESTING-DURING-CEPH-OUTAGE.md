# Running headless test queries during the Ceph (s3-west) outage

**Status: `s3-west.nrp-nautilus.io` (NRP Ceph) is DOWN (503 on everything).** The
normal catalog + parquet reads fail, so any config pointing at s3-west crashes at
boot (`DatasetCatalog.load` → `HTTP 503`, see geo-agent#287). Two changes make
headless runs work anyway, **entirely off the public source.coop mirror**:

1. **Data comes from source.coop**, not Ceph — via per-collection `collection_url`s.
2. **Use the DEV MCP server**, which has the source.coop rewrite (mcp-data-server#261):
   `https://dev-duckdb-mcp.nrp-nautilus.io/mcp`
   (prod `duckdb-mcp.nrp-nautilus.io` is pinned to v0.7.8 and does **not** have #261.)

## What #261 does (why the dev server is required)

Pass a source.coop `stac-collection.json` to the STAC tools and it rewrites
`https://data.source.coop/...` asset hrefs to `s3://us-west-2.opendata.source.coop/...`
so DuckDB can glob-expand `h0=*` (the HTTPS gateway can't list), and avoids a
network `get_root()` that would otherwise crash on an inline source.coop
collection during the outage. No client/MCP reconfig beyond pointing at the dev URL.

## Config: point everything at source.coop

Two rules:
- **`mcp_url`** → the dev server.
- **`catalog` + every collection's `collection_url`** → `data.source.coop`, NOT s3-west.
  - `load()` fetches the top-level `catalog` URL **unconditionally** — it must be a
    reachable 200 (any source.coop collection JSON works as a harmless root). If you
    leave it as s3-west it hard-crashes boot (geo-agent#287).
  - Collections with `collection_url` are fetched **directly** (no catalog walk); a
    404 `collection_url` just warns-and-skips, so partial coverage is fine.

### href remapping rule
s3-west `…/public-<X>/<path>/stac-collection.json`
→ source.coop `https://data.source.coop/cboettig/<X>/<path>/stac-collection.json`

Naming is **not** always 1:1 and coverage is per-collection — verify with
`curl -sI <url>` (200 vs 404) before relying on it. Confirmed present:
`cboettig/gfw`, `cboettig/high-seas/{ebsa,seafloor-geomorphology,ecs,iho,meow}`,
`cboettig/carbon`. Known gaps on first guess: `wdpa`, `iucn` (different paths).
`public-output` (tile/style outputs) is **not** mirrored at all.

### Example (bosl-high-seas, source.coop + dev MCP)
```json
{
  "catalog": "https://data.source.coop/cboettig/gfw/stac-collection.json",
  "mcp_url": "https://dev-duckdb-mcp.nrp-nautilus.io/mcp",
  "collections": [
    {"collection_id": "gfw-fishing-effort", "collection_url": "https://data.source.coop/cboettig/gfw/stac-collection.json"},
    {"collection_id": "ebsa", "collection_url": "https://data.source.coop/cboettig/high-seas/ebsa/stac-collection.json"},
    {"collection_id": "seafloor-geomorphology", "collection_url": "https://data.source.coop/cboettig/high-seas/seafloor-geomorphology/stac-collection.json"}
  ]
}
```
A copy of this lives at `headless/TESTING-DURING-CEPH-OUTAGE.bosl.json`.

### Reliability add-on 1: cache the small JSONs locally (dodge gateway 500s)

`data.source.coop` (the Cloudflare-fronted HTTPS gateway) throws **intermittent
500s** on the small `stac-collection.json` fetches. That's the only way the
*client* can read them — `us-west-2.opendata.source.coop` is an S3-protocol
endpoint the client's plain `fetch()` can't speak (only the MCP server reaches it,
via DuckDB's S3 client, for the heavy parquet). A 500 on the `catalog` root
hard-crashes boot; a 500 on a needed collection silently drops it.

Fix: pre-fetch each collection JSON once (retry through any 500 burst), serve them
from `localhost`, and point `catalog` + every `collection_url` at
`http://127.0.0.1:PORT/<id>.json`. This is safe because `get_schema` forwards the
cached STAC content **inline** to MCP (`map-tools.js` — "so MCP doesn't re-fetch"),
so the server never fetches your localhost URL; it only matters for the client's
initial load. Heavy reads still go direct-to-AWS. Eliminates the flakiness entirely.

### Reliability add-on 2: steer weak models with an outage system-prompt addendum

Smaller/quantized models tend to reach for `get_stac_details` / `browse_stac_catalog`
(which resolve through the offline top-level catalog and fail) instead of
`get_schema` (inline, works). Append `headless/outage-systemprompt-addendum.md` to
the app's base `system-prompt.md` and pass the combined file via `--system-prompt`:

```bash
cat ../../<app>/system-prompt.md outage-systemprompt-addendum.md > /tmp/sp.outage.md
node run.js "..." --system-prompt /tmp/sp.outage.md ...
```

This does NOT fix malformed tool-call *syntax* (a model-side issue — see geo-agent#288);
it only fixes tool *choice*.

## Running a query

```bash
export PROXY_KEY=$(kubectl -n biodiversity get secret open-llm-proxy-secrets \
  -o jsonpath="{.data['proxy-key']}" | base64 -d)

node run.js "How many seamounts are in the Sargasso Sea EBSA?" \
  --config       TESTING-DURING-CEPH-OUTAGE.bosl.json \
  --system-prompt ../../bosl-high-seas/system-prompt.md \
  --model         qwen3 \
  --transcript    runs/test.json
```
(`mcp_url` is read from the config; you can also override with `--mcp-url`.)

### Gotchas
- **`run.js` imports the sibling `../../geo-agent`** — run it from a `headless/`
  whose `../../` is `boettiger-lab/` (the real checkout, or a copy placed as a
  sibling of `geo-agent`). A git *worktree* of open-llm-proxy will NOT resolve the
  import. If you copy `headless/` elsewhere, symlink `node_modules` back to the real
  checkout.
- **Concurrent work:** if multiple agents share the checkout, run from an isolated
  copy so someone editing `mcp-client.js`/`run.js` mid-run doesn't break you.
- **Reasoning on/off:** set `ENABLE_THINKING=true|false` (proxy translates per-model;
  only `qwen3`/`glm-5`/`kimi` have a thinking_key). Unset = model default.

## Cluster matrix (`run-matrix-k8s.sh`)
The k8s runner **clones each app's committed config**, which points at s3-west — so
it will crash until either Ceph is back or the runner grows a config-injection path
to feed a source.coop config. For now, prefer **local** runs (above) with source.coop
configs during the outage.

Refs: mcp-data-server#261 (source.coop fallback), geo-agent#287 (boot-crash on 5xx),
geo-agent#288 (tool-call parser should recover from malformed calls),
open-llm-proxy#58 (reasoning on/off assessment this was built for).
