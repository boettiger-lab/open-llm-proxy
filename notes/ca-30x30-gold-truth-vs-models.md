# CA 30x30 — gold truth vs. what the open models answered

Generated 2026-07-10. Gold truth computed directly against the `duckdb-geo` MCP
(public `s3://public-ca30x30` + `s3://public-cdfw` parquet), following the
sanctioned aggregation rules in each dataset's STAC metadata. Model answers are
from the open-llm-proxy logs for `origin=https://ca-30x30.nrp-nautilus.io`
(all on the DSE-nimbus `qwen`, plus historical `qwen3`, `qwen3-small`,
`gemma`, `minimax-m2`).

**Does the earlier questions table cover everything?** It captured all 19
*session-opening* questions. The proxy only logs each session's first question,
so verbatim mid-session follow-ups aren't recoverable — but every theme
(statewide protection, CWHR habitat, hardwood woodland, ACE biodiversity, FMMP,
definitions) appears as some session's opener, so the coverage below is complete
by theme. Questions collapse into ~5 computable buckets + 3 informational.

Gold-truth method notes: statewide numerator from the polygon geoparquet
(one row/feature, no dedup); CA denominator = 101.5M ac (source ecoregion
polygons, verified earlier). Habitat overlay uses the `cwhr13-hex-fractions`
asset (`SUM(frac·cell_area)`) weighted by each conserved unit's GAP1+2 share
`Acres/Total_Acre` (the STAC-sanctioned feature-overlay weight), not the biased
`mode` asset. ACE joins at res-8 with dedup by `Hex_ID`.

---

## Bucket A — "how much of California is protected?" (7 phrasings)

**GOLD:** GAP1+2 = **26.47M ac** (GAP1 17.73 + GAP2 8.74) = **26.1% of California**.
GAP3+4 ("other protected") = 25.90M. Total protected (all GAP) = 52.38M = 51.6%.
Denominator = 101.5M ac.

| model / date | answered | verdict |
|---|---|---|
| qwen 2026-07-10 | 25.6% (denom 103.3M — hex `SUM`, double-counts dup rows) | numerator ✓, **% low** |
| qwen 2026-07-08 | 27.8% (denom 95.3M — distinct cells × *nominal* area) | numerator ✓, **% high** |
| qwen 2026-07-10 (2nd session) | 26.47M ac, **no %**, no query at all (answered from context) | acres ✓, incomplete |
| qwen 2026-07-07 | 26.47M ac + "85% of the 30% target" | reframed, no "% of CA" |
| minimax-m2 (various) | 25.1 / 26.0 / 30 % | scattered |

The **numerator (26.47M) is rock-solid every time**; only the denominator/framing
drifts. Fixed by hardwiring 101.5M → 26.1% in ca-30x30#87.

---

## Bucket B — "how much of every CWHR13 habitat is protected?" (habitat table)

**GOLD** (% of each class that is GAP1+2 conserved):

| code | habitat | total (M ac) | GAP1+2 (M ac) | % protected |
|--:|---|--:|--:|--:|
| 80 | Urban | 4.78 | 0.05 | 1.0 |
| 10 | Agriculture | 10.25 | 0.25 | 2.4 |
| 52 | **Hardwood Woodland** | 5.87 | 0.80 | **13.6** |
| 60 | Herbaceous | 10.97 | 1.74 | 15.8 |
| 90 | Water | 1.85 | 0.39 | 20.8 |
| 51 | Hardwood Forest | 5.21 | 1.11 | 21.3 |
| 31 | Conifer Forest | 17.31 | 4.08 | 23.5 |
| 70 | Shrub | 12.97 | 3.44 | 26.6 |
| 32 | Conifer Woodland | 3.22 | 0.93 | 28.8 |
| 100 | Wetland | 0.85 | 0.39 | 45.6 |
| 41 | Desert Shrub | 19.26 | 9.27 | 48.2 |
| 20 | Barren/Other | 2.88 | 1.49 | 51.7 |
| 42 | Desert Woodland | 0.96 | 0.55 | 56.7 |

**Model behavior:**
- **qwen 2026-07-10:** fabricated the code→name legend — **12 of 13 names wrong**
  (called 10 "Conifer Forest" [=Agriculture], 41 "Chaparral" [=Desert Shrub],
  80 "Barren" [=Urban], 51 "Pinyon-Juniper" [=Hardwood Forest], …). Never called
  `get_schema(cwhr13)`; no name column exists in the hex data so it hallucinated.
  Root cause: geo-agent#303.
- **qwen 2026-07-07:** correct *names* (Agriculture, Barren/Other…) but the *areas*
  were far off (Agriculture "961,589 ac / 4.6%" vs gold 10.25M / 2.4%; Barren
  "79.8%" vs gold 51.7%) — a restrictive/mode-based join undercounting class totals.

So the habitat table fails on **names** (hallucinated legend) and, separately, on
**areas** (method-dependent) — both need to be right and neither reliably was.

---

## Bucket C — "what % of hardwood woodland is protected?" (5+ phrasings)

**GOLD:** Hardwood Woodland = `whr13num` **code 52**, total 5.87M ac,
GAP1+2 = 0.80M ac → **13.6%**. (Hardwood *Forest* = code 51 = 21.3%, a distinct class.)

This question is the clearest window into the "two numeric codes" confusion — the
answer depended entirely on which code the model picked:

| model / date | code it used | answer | verdict |
|---|---|---|---|
| qwen3 2026-06-24 | **52** (correct) | 5.81M total, **13.5%** | ✅ correct |
| qwen 2026-07-07 | **52** (correct) | 5.81M, 802,430 ac, **13.8%** | ✅ correct |
| minimax-m2 2026-06-22 | 71–76 (60-class oak types) | 3.14M, **3.5%** | ❌ wrong code |
| minimax-m2 2026-06-22 | 70 ("Hardwood") | 13.0M, **26.0%** | ❌ wrong code |
| minimax-m2 2026-06-23 | 10 ("Hardwood") | **2.4%** | ❌ (2.4% is Agriculture) |
| minimax-m2 2026-06-23 | 60-class "hardwood-dominated" | **36.2%** | ❌ wrong code |
| minimax-m2 2026-06-24 | 52 (right code) but total 19.3M | **14.0%** | ⚠️ right code, wrong total |

`qwen3` and `qwen` (when they read the schema) nailed it at ~13.5–13.8% ≈ gold
13.6%. `minimax-m2` cycled through codes 71–76, 70, 10, 9, 60-class subsets before
landing on 52 — every wrong code produced a confidently-stated wrong percentage
(2.4%, 3.5%, 26%, 36%). Same failure family as Bucket B: no reliable code→name step.

---

## Bucket D — ACE biodiversity (3 questions)

**D1. "what % of GAP-1 land is in ≥80th-percentile endemic biodiversity?"**
GOLD: with P80 of `AllTaxaEnd` = 4 endemic taxa, **20.7%** of GAP-1 land (103,900
res-8 cells) falls in the top-20% endemic cells — i.e. GAP-1 land is *not*
enriched for endemic biodiversity (≈ the 20% expected by chance).
- **minimax-m2 2026-06-22:** answered **20.52%** (104,889 GAP-1 cells, threshold P80).
  ✅ Essentially correct — matches gold to 0.2 pt. The model got this one right.

**D2. "Show me bird species richness across the state"** — map request. Correct
field is `NtvBird` (native bird models per hexagon; range 0–242, statewide mean
116.7, highest in Marin). Gold is a rendered gradient, not a number.

**D3. "Show me the ACE statewide biodiversity rank"** — map request. Correct field
is `BioRankSW` (1–5 statewide quintile; 12,778 of 63,890 hexagons are rank 5).
Rendered layer, not a number.

---

## Bucket E — informational (3 questions, no numeric gold)

- "Can you give me the FMMP links?" / "link me to the official source for FMMP data" —
  answer is a citation to CA FMMP (`fmmp-2022` dataset / CA Dept. of Conservation),
  not a computation.
- "what does CWHR stand for" — California Wildlife Habitat Relationships.

---

## Scorecard & where the errors come from

| question bucket | model got it right? | failure mode | fix |
|---|---|---|---|
| A. statewide % | numerator always ✓, % drifted 25.6–27.8 | denominator recomputed ad hoc | ca-30x30#87 (hardwire 101.5M) |
| B. habitat table | ✗ (names hallucinated; areas method-dependent) | no code→name step; wrong asset for area | geo-agent#303; #301 |
| C. hardwood woodland | ✓ *iff* it used code 52 (qwen/qwen3), ✗ for minimax | wrong CWHR code picked from memory | geo-agent#303; ca-30x30#87 caveat |
| D1. endemic × GAP1 | ✓ (20.5% ≈ gold 20.7%) | — | — |
| D2/D3. maps | n/a (display) | — | — |

**Bottom line:** the models are reliable on well-scoped numeric joins when they read
the schema (Bucket A numerator, C-with-code-52, D1). They fail exactly where a
**coded value must be resolved** — the CWHR habitat legend — because there's no name
column in the hex data and nothing forces `get_schema`, so they fill the gap from
memory. That single gap explains both the mislabeled habitat table (B) and the
hardwood-woodland scatter (C). Fixes already filed: geo-agent#303 (reactive
schema-on-error + inline small legends), ca-30x30#87 (hardwire denominator + CWHR
code caveat), data-workflows#387 (dup rows), mcp-data-server#294 (exact H3 area).
