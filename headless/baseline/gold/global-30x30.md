# Gold — global-30x30

<!-- Provenance: WDPA-dependent answers (Q1–Q4) re-verified 2026-07-13 against current NRP
     (WDPA_poly_Jun2026, 306,985 features — advanced past the Dec-2025 vintage they were first
     computed on). All values reproduced exactly; no changes. See open-llm-proxy#81. -->

## Q1. What % of global land is inside a designated protected area?
- **Answer:** **16.48%** (29,263,682 protected land h8 cells / 177,521,782 total land h8 cells). Matches Protected Planet ~16–17% terrestrial.
- **Datasets:** `wdpa` `s3://public-wdpa/wdpa/hex/h0=*/data_0.parquet`; land mask `cgls-lc100-2019` `s3://public-land-cover/cgls-lc100-2019/hex/h0=*/data_0.parquet` (land = lc_class NOT IN (0,80,200)).
- **SQL:**
```sql
WITH wdpa AS (SELECT DISTINCT h8 FROM read_parquet('s3://public-wdpa/wdpa/hex/h0=*/data_0.parquet')),
     land AS (SELECT DISTINCT h8 FROM read_parquet('s3://public-land-cover/cgls-lc100-2019/hex/h0=*/data_0.parquet') WHERE lc_class NOT IN (0,80,200))
SELECT 100.0*COUNT(*)/(SELECT COUNT(*) FROM land) FROM land WHERE h8 IN (SELECT h8 FROM wdpa);
```
- **Confidence:** HIGH. Deduped at h8 (PA overlaps not double-counted). Land-only as asked.

## Q2. How many people live inside existing protected areas?
- **Answer:** **≈ 357 million** (357,331,494; GHS-POP 2020). ~4.6% of world population.
- **Datasets:** `ghs-pop-2020` `s3://public-population/ghs-pop-2020/hex/h0=*/data_0.parquet`; `wdpa` hex.
- **SQL:**
```sql
WITH wdpa AS (SELECT DISTINCT h8 FROM read_parquet('s3://public-wdpa/wdpa/hex/h0=*/data_0.parquet'))
SELECT CAST(ROUND(SUM(p.population)) AS BIGINT)
FROM read_parquet('s3://public-population/ghs-pop-2020/hex/h0=*/data_0.parquet') p
SEMI JOIN wdpa ON p.h8=wdpa.h8;
```
- **Confidence:** MED-HIGH. Persons (2020). WDPA deduped before join. h8 matching slightly over-attributes at PA edges → true value modestly lower.

## Q3. Which ecoregions are least represented in the protected-area network?
- **Answer:** **~32 ecoregions at ~0% protected** (a tie, not a clean bottom-10). Largest 0%: Horn of Africa xeric bushlands, Ordos Plateau steppe, Qaidam Basin semi-desert, Narmada Valley dry deciduous forests, Chhota-Nagpur dry deciduous forests, N. Anatolian conifer/deciduous forests, Somali montane xeric woodlands, E. Anatolian deciduous forests, Tarim Basin deciduous forests & steppe, Helanshan montane conifer forests — all 0.000%. Just above 0: Central Deccan Plateau 0.004%, Yarlung Zanbo arid steppe 0.010%, Central Anatolian steppe 0.013%. Pattern: arid steppe/desert/dry-deciduous forest, mostly Palearctic + Indomalayan + Horn of Africa.
- **Datasets:** `wwf-ecoregions-2017` hex + names `s3://public-ecoregion/ecoregion.parquet` (join `_cng_fid=OBJECTID`); `wdpa` hex.
- **SQL:** % = protected h8 / total h8 per ecoregion, filter total_cells>=50. (full query in run log)
- **Confidence:** HIGH. Area-weighted overall = 16.69% (consistent w/ Q1). Truthful structure is a 0% tie of ~32 ecoregions; a single ordered bottom-10 is acceptable but the tie is the real answer.

## Q4. Top 5 protected areas by mean mammal richness
- **Answer (≥3 h5 cells, defensible gold):** 1 Rwenzori Mountains, Uganda 210.0; 2 Estuaire du fleuve Sinnamary, FGuiana 201.5; 3 Crique et Pripri Yiyi, FGuiana 201.3; 4 Galibi, Suriname 200.3; 5 Basse-Mana, FGuiana 199.8. Geography = Albertine Rift + Guianas/W-Amazon (known mammal hotspots).
- **Datasets:** `iucn-richness-2025` mammals `s3://public-iucn/hex/mammals_sr/h0=*/data_0.parquet` (native h5); `wdpa` hex (h8→h5); names `s3://public-wdpa/wdpa.parquet`.
- **SQL:** AVG(mammals_sr) per WDPA `_cng_fid` over h5 cells, filter n_h5_cells>=3. (full query in run log)
- **Confidence:** MED. **Trap:** without a min-cell threshold, top is 3 single-cell tiny PAs reading exactly 213 (one PA on the global hotspot cell) — an artifact. ≥3-cell threshold gives the meaningful answer. Either accepted, thresholded is gold. **App-coverage caveat:** global-30x30's configured layers don't include a mammal-richness dataset — models may not have had this data.

## Q5. How much irrecoverable carbon (2024 layer) is stored in California?
- **Answer:** **≈ 400 Mt C** (3.999e8 Mg C) irrecoverable carbon (2024) within California. Cross-check: Florida **≈ 466 Mt C** (4.658e8).
- **Trap (`h0-not-a-boundary`):** `h0` is the res-0 hive **partition key** (~4.35M km² per base cell), NOT a boundary. Filtering carbon by California's `h0` set alone returns **2,543 Mt C** (2.543e9) — the two whole base cells CA touches, 6.4× too high; Florida (1 base cell) → **1,934 Mt** vs 466 correct (4.2×). Clip via the census state hex mask at the **finest shared resolution**: carbon is native h9 (parents h8…h5), census-state is native h8, so join/filter on `h8` (and `h0` for partition pruning).
- **Datasets:** `irrecoverable-carbon` 2024 `s3://public-carbon/irrecoverable-carbon-2024/hex/h0=*/data_0.parquet` (col `carbon` Mg C, one row per h9 — SUM directly, no dedup); mask `census-2024-state` `s3://public-census/census-2024/state/hex/h0=*/data_0.parquet` (native h8, `STUSPS`). census not in global-30x30's configured layers → discovered via `browse_stac_catalog`.
- **SQL:**
```sql
SELECT SUM(c.carbon) AS carbon_mg_c
FROM read_parquet('s3://public-carbon/irrecoverable-carbon-2024/hex/h0=*/data_0.parquet') c
WHERE c.h0 IN (SELECT DISTINCT h0 FROM read_parquet('s3://public-census/census-2024/state/hex/h0=*/data_0.parquet') WHERE STUSPS='CA')
  AND c.h8 IN (SELECT DISTINCT h8 FROM read_parquet('s3://public-census/census-2024/state/hex/h0=*/data_0.parquet') WHERE STUSPS='CA');
```
- **Confidence:** HIGH. Operator-verified 2026-07-15 (duckdb-geo, v0.8.7): CA correct 3.999e8, FL correct 4.658e8; h0-only trap CA 2.543e9, FL 1.934e9. Guards the geo-agent #322/#325 region-subset guidance (`h0` is not a boundary; lead with the `hN IN (SELECT hN FROM <mask> WHERE <attr>)` form).
