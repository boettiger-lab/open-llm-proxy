# Gold — tpl (national)

All $ nominal USD. Almanac currency ~through 2017. Sites hex covers ~79% of sites
(small/complex geometries dropped) → site-count totals slightly undercount.

## Q1. Land-cover composition of Almanac protected areas nationwide + states leading each cover type
- **Answer:** Composition by H3 res-9 cell (≈0.105 km²/cell; dominant class per cell, land classes only). Top: Herbaceous veg (30) 173,997; Closed evergreen-needleleaf forest (111) 144,604; Closed deciduous-broadleaf (114) 139,474; Agriculture (40) 82,039; Open forest unknown (126) 65,220; Closed forest unknown (116) 55,346; Closed mixed (115) 49,873; Shrubs (20) 37,481; Herbaceous wetland (90) 16,907. **State leaders:** Herbaceous veg→CO; Closed needleleaf→MT; Closed mixed forest→ME; Shrubs→CA; Deciduous-broadleaf→NY; Agriculture→PA; Bare/sparse→CA; Closed unknown forest→FL; Herbaceous wetland→FL; Permanent water→ND; Urban→FL; Snow/ice→AK.
- **Datasets:** `conservation-almanac-2024-sites` hex (h9 mask + state_id); `cgls-lc100-2019` `s3://public-land-cover/cgls-lc100-2019/hex/h0=*/data_0.parquet`. SEMI JOIN land cover onto site-hex mask, group by lc_class; per-state via ROW_NUMBER over (state, class).
- **Confidence:** MED. Units = res-9 **cell counts** (×0.105 km² for area), dominant-class (within-cell mixes discarded). ~79% site coverage. Border cells counted for both states (minor). Leaders robust given margins.

## Q2. Texas CDs receiving the most federal conservation funding
- **Answer:** CD-34 ~$29.5M (71 sites); CD-14 ~$29.5M (68); CD-36 ~$17.0M (45); CD-31 ~$15.6M (25); CD-10 ~$8.9M (20); CD-6 ~$7.1M. (TX federal total ~$126.8M/312 sites; ~$124M placed.)
- **Datasets:** `conservation-almanac-2024-funding` (sponsor_type='FED', amount>0); sites + hex; `census-2024-cd` hex (STATEFP='48'). Site→dominant-CD by max hex-cell count.
- **Confidence:** MED. USD. 119th-Congress CDs vs ~2017 sites. Spatial assignment via res-10; ~$3M unplaced. CD-34 ≈ CD-14 (tied top).

## Q3. New Jersey municipalities that passed conservation ballot measures + pattern
- **Answer:** **261 distinct NJ municipalities**, 444 passed municipal measures (1988–2025, ~$1.95B approved). **Temporal:** sharp wave 1998–2004 (peak 47 in 1998; Garden State Preservation Trust era), lull 2009–2015, modest 2016–2025 resurgence. **Spatial:** concentrated in north-central suburban/exurban corridor — Bergen (30 munis), Burlington (22, ~$307M highest $), Monmouth (23), Morris (27), Hunterdon, Warren, Essex; sparse in south + urban core.
- **Dataset:** `landvote` `s3://public-tpl/landvote.parquet`; state='NJ', status IN ('Pass','Pass*'), jurisdiction='Municipal', municipal<>'nan'. Dedup by landvote_id.
- **Confidence:** HIGH.

## Q4. Who has funded land conservation in Boulder County, Colorado?
- **Answer:** **Boulder County, CO (LOC) ~$321.2M** across 530 sites — almost all its **Sales Tax Funds** ($321.3M). Federal: NRCS ~$6.30M (Farm & Ranch Lands Protection), USFS ~$4.9M (Forest Legacy). State: Colorado/GOCO ~$3.13M. Local: Longmont ~$2.10M. Private ~$1.0M.
- **Datasets:** `conservation-almanac-2024-funding` + `conservation-almanac-2024-sites` (state_id='CO', county='Boulder County'). Join on tpl_id.
- **Confidence:** HIGH. USD, SUM-safe. Dominated by Boulder County's local sales-tax open-space program.
