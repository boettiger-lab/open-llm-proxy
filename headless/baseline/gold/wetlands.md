# Gold — wetlands (wetlands-v2)

## Q1. Vulnerable carbon stored in different wetlands of India
- **Answer:** **≈ 1.095 × 10⁹ Mg C (~1.10 Pg C)** total vulnerable carbon (2024) in Indian wetlands. Top GLWD-v2 classes (Mg C): Riverine seasonally-saturated forested (14) 2.07e8; Rice paddies (33) 1.75e8; Temperate peatland forested (24) 1.72e8; Small streams (7) 8.75e7; Riverine seasonally-saturated non-forested (15) 8.43e7; Mangrove (28) 6.85e7; Salt pan/saline (32) 5.03e7; Other coastal (31) 3.83e7; Tropical peatland forested (26) 3.12e7; Temperate peatland non-forested (25) 2.35e7.
- **Datasets:** `irrecoverable-carbon` `s3://public-carbon/vulnerable-carbon-2024/hex/h0=*/data_0.parquet`; `wetlands-glwd-v2` `s3://public-wetlands/glwd/hex/h0=*/data_0.parquet` (class Z, codes 1–33); `overture-divisions-countries` `s3://public-overturemaps/2026-02-18.0/countries/hex/h0=*/data_0.parquet` (country='IN'). SEMI JOIN carbon onto India h8 mask BEFORE aggregating, then join GLWD class on h8.
- **Confidence:** MED-HIGH. Units Mg C. **Trap handled:** mask-before-aggregate (DPP). Wetland = GLWD dominant class per h8 (mode). vulnerable-carbon-2024 (latest).

## Q2. Ramsar sites meeting Criterion 9 + explain the criterion
- **Answer:** **64 Ramsar sites** meet Criterion 9 (of 2,551 distinct). Examples: Aldabra Atoll (SC), Bas Ogooué & Akanda (GA), Elkhorn Slough & Corkscrew Swamp & Cache River (US), Dafeng NNR (CN), Dojran Lake (MK), Basse-Mana (FR). **Criterion 9 (definition):** a wetland is internationally important if it regularly supports **1% of the individuals in a population of one species/subspecies of wetland-dependent NON-AVIAN animal** (the non-avian analogue of Criterion 6 for waterbirds; Group B species criterion).
- **Dataset:** `wetlands-ramsar` `s3://public-wetlands/ramsar/ramsar_wetlands.parquet` (boolean `Criterion9`).
- **SQL:**
```sql
SELECT COUNT(*) FILTER (WHERE "Criterion9") AS crit9, COUNT(*) AS total
FROM (SELECT DISTINCT ramsarid,"Criterion9" FROM read_parquet('s3://public-wetlands/ramsar/ramsar_wetlands.parquet'));
```
- **Confidence:** HIGH. `COUNT(DISTINCT ramsarid)` (parcels vs sites trap). Definition matches official Ramsar criteria.

## Q3. Top 10 level-3 hydrobasins by wetland extent, carbon, NCP (normalized)
- **Answer (equal-weight mean of min-max-normalized extent/carbon/NCP):** 1 `6030007000` (0.866, max on extent & carbon); 2 `3030001840` 0.452; 3 `1030020040` 0.407; 4 `4030025450` 0.386; 5 `5030040360` 0.382; 6–9 `1030040050`/`5030055010`/`5030087540`/`6030040020` ~0.333 (NCP=max-tied, near-0 extent/carbon); 10 `5030054880` 0.331. Basin 6030007000 is the clear #1.
- **Datasets:** `hydrobasins-v1c` L3 `s3://public-hydrobasins/L3/**`; `wetlands-glwd-v2` area `s3://public-wetlands/glwd/area-hex/h0=*/data_0.parquet` (area_ha_x10/10); `irrecoverable-carbon` vulnerable-2024; `ncp-biodiversity` `s3://public-ncp/hex/ncp_biod_nathab/h0=*/data_0.parquet` (AVG, h7). Min-max normalize each metric across basins, composite = unweighted mean.
- **Confidence:** MED. Method sound + reproducible but **rank-sensitive**: #1 and top-5 robust; ranks 6–9 driven entirely by NCP=20 tie (small basins, ~0 extent/carbon). Different normalization/weighting reorders the NCP-saturated tail. NCP is an intensity (mean) combined with extensive (summed) metrics — inherent to the "normalized composite" framing.
