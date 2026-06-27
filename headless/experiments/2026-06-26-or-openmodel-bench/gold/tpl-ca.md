# Gold — tpl-ca

District attribution: Almanac/landvote carry no district id → assigned by H3 hex intersection
(sites/census res-10; landvote/census-CD res-8). Boundary-spanning features attributed in full
to each district they touch (no apportionment). All $ nominal USD. Almanac CA currency ~through 2017.

## Q1. Programs/agencies funding conservation in Congressional District 16, + how much
- **Answer (CA CD16, 2024 boundaries; total ≈ $36.1M):** State Bonds Prop 40 $11.75M; Prop 12 $7.61M; Property Tax Funds (Santa Clara Co) $6.15M; Prop 117 $3.46M; LWCF/USFWS $3.21M; LWCF/NPS $2.43M; State Parks bonds $0.80M; General Fund $0.49M; Prop 70 $0.21M.
- **Datasets:** `conservation-almanac-2024-funding` `s3://public-tpl/conservation-almanac-2024-funding.parquet`; sites hex `s3://public-tpl/conservation-almanac-2024-sites/hex/h0=*/data_0.parquet`; `census-2024-cd` hex `s3://public-census/census-2024/cd/hex/h0=*/data_0.parquet` (STATEFP='06', CD119FP='16'). Join sites→CD on h10, funding→sites on tpl_id.
- **Confidence:** HIGH. SUM(amount) safe (one row/txn). LWCF appears under two federal sponsors.

## Q2. CDs that raised the most conservation funding via ballot measures since 2010
- **Answer:** Top CD30/CD29 ~$5.161B; CD36/CD32 ~$5.023B; CD15 ~$5.005B. **Caveat:** ~$4.496B of every CD's total is 3 statewide CA props (2018 $1.796B, 2014 $1.5B, 2024 $1.2B) touching all CDs. Local-only ranking (meaningful): CD30/29 ~$665M, CD36/32 ~$527M, CD15 ~$509M.
- **Dataset:** `landvote` `s3://public-tpl/landvote.parquet` + hex; status IN ('Pass','Pass*'), year>=2010, conservation_funds_approved. Deduped by landvote_id per CD.
- **Confidence:** MED. "Raised"=funds_approved on passed. Statewide measures attributed identically to all CDs (correct but dominates). Top ranking robust.

## Q3. CDs with failed conservation ballot measures + $ at stake
- **Answer:** Essentially **all 52 CA CDs** have failed measures. ~$7.717B baseline = 6 failed statewide props touching all CDs. By total at stake: CD49 ~$9.86B, CD50 ~$9.84B, CD48 ~$9.83B, CD51/52 ~$9.82B. Local-only (meaningful): CD49 ~$2.15B, CD50 ~$2.13B, CD48 ~$2.12B (driven by a ~$2B failed San Diego County measure).
- **Dataset:** `landvote` (same), status='Fail', conservation_funds_at_stake, deduped by landvote_id.
- **Confidence:** MED. All-CD result correct but uninformative w/o separating statewide; local-only (San Diego region) is the meaningful answer.

## Q4. Assembly districts with the most Almanac protected acreage
- **Answer (acres):** AD34 563,641 (586 sites); AD36 451,429; AD02 207,738; AD47 152,443; AD75 48,747; then AD01 46,402, AD30 29,059, AD27 24,496.
- **Datasets:** `conservation-almanac-2024-sites` (acres from `s3://public-tpl/conservation-almanac-2024-sites.parquet`, geom from hex); `census-2025-sldl` hex `s3://public-census/census-2025/sldl/hex/h0=*/data_0.parquet` (STATEFP='06'). Join on h10.
- **Confidence:** HIGH ranking / MED absolute. Acres from sites GeoParquet (one row/tpl_id, SUM-safe — never SUM on hex). SLDLST zero-padded (034=AD34). Boundary-spanning sites attributed full acreage to each AD. ~79% hex coverage + ~2017 currency → absolute undercount.

## Q5. LWCF investment in State Senate District 2
- **Answer:** **≈ $285,516,180** (~$285.5M) across 48 sites. By sponsor: BLM $260.31M, USFS $16.48M, NPS $7.67M, USFWS $1.05M.
- **Datasets:** `conservation-almanac-2024-funding` (program LIKE '%LWCF%'); sites hex; `census-2025-sldu` hex `s3://public-census/census-2025/sldu/hex/h0=*/data_0.parquet` (STATEFP='06', SLDUST='002').
- **Confidence:** HIGH. Nominal USD, SUM-safe. SD2 = large North Coast seat → BLM-dominated total is consistent.
