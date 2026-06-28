# Gold — bosl-high-seas

## Q1. Industrial fishing displaced by designating all high-seas EBSAs as no-take MPAs
- **Answer:** ≈ **1,961,219 fishing hours/year** (2024) inside high-seas EBSAs. By gear: drifting_longlines 1,464,490 (~75%); squid_jigger 278,597 (~14%); fishing (unclassified) 133,699; trawlers 37,980; pole_and_line 27,729; tuna_purse_seines 10,890; set_longlines 3,593; fixed_gear 3,138; rest <500. Dominated by pelagic high-seas fleets. (multi-year context: 2022 2.08M, 2023 2.14M, 2024 1.96M hrs)
- **Datasets:** `gfw-fishing-effort` h6 `s3://public-gfw/gfw-fishing-effort/hex/h0=*/*.parquet`; `ebsa` h6 `s3://public-high-seas/ebsa/hex/h0=*/data_0.parquet`; `iho-maritime-boundaries` EEZ h6 `s3://public-high-seas/iho/eez/hex/h0=*/data_00.parquet`.
- **SQL:**
```sql
WITH ebsa_h6 AS (SELECT DISTINCT h0,h6 FROM read_parquet('s3://public-high-seas/ebsa/hex/h0=*/data_0.parquet')),
     eez_h6 AS (SELECT DISTINCT h6 FROM read_parquet('s3://public-high-seas/iho/eez/hex/h0=*/data_00.parquet')),
     highseas_ebsa_h6 AS (SELECT e.h0,e.h6 FROM ebsa_h6 e ANTI JOIN eez_h6 z ON e.h6=z.h6)
SELECT g.geartype, ROUND(SUM(g.fishing_hours),1) AS fishing_hours_2024
FROM read_parquet('s3://public-gfw/gfw-fishing-effort/hex/h0=*/*.parquet') g
SEMI JOIN highseas_ebsa_h6 m ON g.h0=m.h0 AND g.h6=m.h6
WHERE g.year=2024 GROUP BY g.geartype ORDER BY fishing_hours_2024 DESC;
```
- **Confidence:** HIGH method / MED absolute. Units = GFW apparent **fishing hours**, 2024. High seas = EBSA ∩ NOT-in-EEZ (67% of EBSA cells). **Caveat:** EBSA coverage incomplete (203 EBSAs, 9 of ~15 CBD workshops) → floor, not full repository.

## Q2. How many seamounts in the Sargasso Sea EBSA?
- **Answer:** **141 seamounts** intersecting (135 fully within). Confirmed two ways (H3 hex join + exact `ST_Intersects` polygon join both = 141).
- **Datasets:** `seafloor-geomorphology` polygon `s3://public-high-seas/seafloor-geomorphology.parquet` (feature_type='Seamounts'); `ebsa` polygon `s3://public-high-seas/ebsa.parquet` (GLOBAL_ID='WC_13' = Sargasso Sea).
- **SQL:**
```sql
WITH sarg AS (SELECT ST_SetCRS(geom,'OGC:CRS84') AS sgeom FROM read_parquet('s3://public-high-seas/ebsa.parquet') WHERE GLOBAL_ID='WC_13'),
     sm AS (SELECT geometry AS mgeom FROM read_parquet('s3://public-high-seas/seafloor-geomorphology.parquet') WHERE feature_type='Seamounts')
SELECT COUNT(*) AS intersecting, COUNT(*) FILTER (WHERE ST_Within(sm.mgeom,sarg.sgeom)) AS fully_within
FROM sm, sarg WHERE ST_Intersects(sm.mgeom, sarg.sgeom);
```
- **Confidence:** HIGH. Standard "features in area" = intersection = 141 (135 "within" also defensible). Excludes Guyots (flat-topped seamounts, separate class).
