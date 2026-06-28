# Gold — biodiversity

## Q1. Which countries have the most Ramsar wetland sites?
- **Answer (top 10 by distinct site count):** UK 176, Mexico 144, India 94, China 82, Spain 76, Sweden 68, Australia 67, Norway 63, Italy 61, Netherlands 58. (next: France 55, Japan 54)
- **Dataset:** `wetlands-ramsar` — `read_parquet('s3://public-wetlands/ramsar/ramsar_wetlands.parquet')`
- **SQL:**
```sql
SELECT "Country" AS country, COUNT(DISTINCT ramsarid) AS n_sites
FROM read_parquet('s3://public-wetlands/ramsar/ramsar_wetlands.parquet')
GROUP BY "Country" ORDER BY n_sites DESC LIMIT 12;
```
- **Confidence:** HIGH. **Trap:** 8,347 polygon parcels vs 2,551 distinct sites → must `COUNT(DISTINCT ramsarid)`. `COUNT(*)` wrongly puts USA on top (most parcels); correct #1 is UK. `Country` is single-valued (no transboundary double-count).

## Q2. Top 10 ecoregions by vulnerable carbon
- **Answer (Mg C, 2024):** 1 West Siberian taiga 13.85 Gt; 2 Scandinavian & Russian taiga 13.72 Gt; 3 Southwest Amazon moist forests 10.78 Gt; 4 Madeira-Tapajós moist forests 8.09 Gt; 5 Guianan lowland moist forests 7.67 Gt; 6 Uatumã-Trombetas moist forests 7.06 Gt; 7 East Siberian taiga 6.42 Gt; 8 NW Congolian lowland forests 6.42 Gt; 9 Central Congolian lowland forests 6.19 Gt; 10 NE Congolian lowland forests 6.13 Gt.
- **Datasets:** `irrecoverable-carbon` (vulnerable) `s3://public-carbon/vulnerable-carbon-2024/hex/h0=*/data_0.parquet` (carbon in Mg C, total per cell); `wwf-ecoregions-2017` `s3://public-ecoregion/ecoregion/hex/h0=*/data_0.parquet` (carries ECO_NAME, native h8).
- **SQL:**
```sql
SELECT e.ECO_NAME, ROUND(SUM(c.carbon)) AS vulnerable_carbon_MgC
FROM read_parquet('s3://public-carbon/vulnerable-carbon-2024/hex/h0=*/data_0.parquet') c
JOIN read_parquet('s3://public-ecoregion/ecoregion/hex/h0=*/data_0.parquet') e
  ON c.h0 = e.h0 AND c.h8 = e.h8
GROUP BY e.ECO_NAME ORDER BY vulnerable_carbon_MgC DESC LIMIT 10;
```
- **Confidence:** HIGH. Units Mg C (=tonnes). Used vulnerable-carbon-2024 (latest); ranking stable across years, absolute values shift. h0 included in hex join.
