
---

## ⚠️ Data-access mode (headless testing during a Ceph / s3-west outage)

The primary object store (`s3-west.nrp-nautilus.io`) is **offline**, so the tools
that resolve datasets through the *top-level* STAC catalog are unavailable this
session. Follow these rules:

- **Use `get_schema(dataset_id)` for every dataset in this app.** It reads the
  catalog metadata already loaded above (served inline to the schema service), so
  it works during the outage and returns the correct `read_parquet(...)` path.
- **Do NOT call `browse_stac_catalog`, `get_stac_details`, or `get_collection`.**
  They walk the offline top-level catalog and will return "not found" / time out.
- Every dataset you need is already listed in the catalog section above — you do
  not need to discover anything. Go straight to `get_schema`, then `query`.
- Read paths resolve to the `s3://us-west-2.opendata.source.coop/...` mirror
  automatically; use the paths `get_schema` gives you verbatim.
