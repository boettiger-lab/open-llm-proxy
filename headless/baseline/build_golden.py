#!/usr/bin/env python3
"""Build golden.json — the #40 standing baseline manifest — from the per-app
question files + the verified gold/*.md (answers + authoritative SQL).

Each record ties a question to: the checkable gold, the authoritative SQL
(in gold/<app>.md), the *trap it guards* (the #42 rule-store key), an accept
rule, and the first-run benchmark accuracy. Re-run after editing TRAPS/ACCEPT
or adding questions. Source: headless/experiments/2026-06-26-or-openmodel-bench.
"""
import json, os
HERE = os.path.dirname(os.path.abspath(__file__))
QDIR = os.path.join(HERE, "questions")

# (app, qN) -> metadata. gold/accept kept concise + checkable; full SQL lives in
# gold/<app>.md (sql_ref). trap = the rule each question guards (rule-store key).
META = {
 ("biodiversity","q1"): dict(id="bio-ramsar-countries", trap="count-distinct-sites-not-parcels",
   gold="Top by distinct site: UK 176, Mexico 144, India 94, China 82 …",
   accept="top country = UK; COUNT(DISTINCT ramsarid), not COUNT(*) (which wrongly puts USA #1)"),
 ("biodiversity","q2"): dict(id="bio-ecoregion-vuln-carbon", trap="hex-join-include-h0;carbon-cell-is-total",
   gold="West Siberian taiga ~13.85 Gt C #1; taiga+Amazon+Congo top 10 (Mg C, vulnerable-2024)",
   accept="top ecoregion = West Siberian taiga; values ~±10%; units Mg/Gt C"),
 ("bosl-high-seas","q1"): dict(id="bosl-fishing-displaced", trap="mask-before-aggregate;units-fishing-hours;ebsa-coverage-partial",
   gold="~1.96M apparent fishing hours (2024) in high-seas EBSAs; ~75% drifting longlines, ~14% squid jigger",
   accept="~1.96M hours (2024, now specified), dominated by longlines; high-seas = EBSA∩not-EEZ",
   note="Original (no year) was ambiguous — answers split 2024 vs multi-year avg. Year now specified; the ambiguous twin is in clarify.txt."),
 ("bosl-high-seas","q2"): dict(id="bosl-sargasso-seamounts", trap="intersect-vs-within;feature_type-exact",
   gold="141 seamounts intersecting Sargasso EBSA (135 fully within)",
   accept="135–141 (intersect or within both ok)"),
 ("ca-30x30","q1"): dict(id="ca-gap12-acres", trap="use-gap-acre-columns-not-final_g_p;units-acres",
   gold="26.47M acres GAP1+2 (GAP1 17.73M, GAP2 8.74M)",
   accept="~24–28M acres; not the 52M all-units extent"),
 ("ca-30x30","q2"): dict(id="ca-ecoregion-most-conserved", trap="group-by-native-ecoregion-field",
   gold="Mojave Desert ~7.37M acres #1",
   accept="top ecoregion = Mojave Desert"),
 ("ca-30x30","q3"): dict(id="ca-pct-conserved", trap="ca-denominator-ecoregion-source-area;units-percent-not-acres",
   gold="~26.1% (26.47M ac GAP1+2 / 101.5M ac CA extent). Denominator = SUM(Shape_Area) of the source ecoregion polygons (101,498,000 ac), NOT a hex SUM (103.3M, double-counts dup rows) or count×nominal-cell-area (95.3M).",
   accept="~26% (25.5–26.6). MUST divide 26.47M GAP1+2 by the ~101.5M ecoregion extent; reject 25.6% (hex-sum denom), 27.8% (nominal-area denom), acres-only, or '% of the 30% target'.",
   note="Guidance trap added 2026-07-10 from proxy-log analysis: qwen recomputed the denominator ad hoc (25.6% vs 27.8% across runs). See notes/ca-30x30-gold-truth-vs-models.md; fixed by ca-30x30#87 (hardwired denominator)."),
 ("ca-30x30","q4"): dict(id="ca-cwhr13-pct-conserved", trap="cwhr-code-name-from-schema-not-memory;cwhr13-use-fractional-hex-not-mode",
   gold="Per whr13num (name from STAC legend, area from cwhr13-hex-fractions): Urban(80) 1.0%, Agriculture(10) 2.4%, Hardwood Woodland(52) 13.6%, Herbaceous(60) 15.8%, Water(90) 20.8%, Hardwood Forest(51) 21.3%, Conifer Forest(31) 23.5%, Shrub(70) 26.6%, Conifer Woodland(32) 28.8%, Wetland(100) 45.6%, Desert Shrub(41) 48.2%, Barren/Other(20) 51.7%, Desert Woodland(42) 56.7%.",
   accept="Class NAMES correct per whr13num legend (10=Agriculture, 31=Conifer Forest, 52=Hardwood Woodland, 80=Urban, 90=Water …) — NOT fabricated (e.g. 10=Conifer, 80=Barren is the failure). Percents ±3pt. Least protected = Urban/Agriculture; most = Desert Woodland/Barren.",
   note="Guidance trap added 2026-07-10: qwen queried cwhr13 without get_schema and fabricated 12/13 class names. See geo-agent#303, ca-30x30#87 (CWHR caveat), notes/ca-30x30-gold-truth-vs-models.md."),
 ("ca-30x30","q5"): dict(id="ca-hardwood-woodland-pct", trap="cwhr-hardwood-woodland-is-code-52;cwhr13-use-fractional-hex-not-mode",
   gold="~13.6% (Hardwood Woodland = whr13num 52; 5.87M ac total, 0.80M ac GAP1+2). Hardwood FOREST is code 51 (21.3%) — a distinct class.",
   accept="~13–14% AND uses whr13num=52. Reject answers built on code 10 (Agriculture→2.4%), 70 (Shrub→26%), 60-class oak subtypes, or 'code 51' — all wrong-code selections seen in the logs.",
   note="Guidance trap added 2026-07-10: minimax cycled through codes 71-76/70/10 (→2.4%/3.5%/26%/36%); qwen3/qwen used code 52 correctly (→13.5-13.8%). The clearest 'two numeric codes' failure. See notes/ca-30x30-gold-truth-vs-models.md."),
 # NB: the ca-30x30 endemic×GAP-1 question is ambiguous (metric-layer + threshold-population
 # forks) — it is a mode=clarify test (CLARIFY list below), NOT an answer-mode gold.
 ("global-30x30","q1"): dict(id="glob-pct-land-protected", trap="dedup-protected-hexes;land-mask-denominator",
   gold="~16.5% of global land inside WDPA",
   accept="~15–18%"),
 ("global-30x30","q2"): dict(id="glob-people-in-pas", trap="dedup-hexes-before-sum-population",
   gold="~357M people (GHS-POP 2020)",
   accept="~300–420M"),
 ("global-30x30","q3"): dict(id="glob-least-represented-ecoregions", trap="area-share-not-count;zero-tie;min-cell-threshold",
   gold="~32 ecoregions at ~0% protected; arid/steppe/desert/dry-forest (Horn of Africa, Ordos, Qaidam, Narmada, Chhota-Nagpur …)",
   accept="names any of the ~0% set or the arid/dry-forest character"),
 ("global-30x30","q4"): dict(id="glob-top5-pa-mammal-richness", trap="single-cell-richness-artifact-threshold;APP-LACKS-DATASET",
   gold="Albertine Rift/Rwenzori + Guianas/W-Amazon top (≥3-cell threshold)",
   accept="correct hotspot geography; OR graceful 'no mammal-richness layer configured' (catalog gap)"),
 ("tpl-ca","q1"): dict(id="tplca-cd16-funders", trap="hex-district-attribution;sum-amount-safe",
   gold="~$36.1M CD16: Prop40 $11.75M, Prop12 $7.61M, Property Tax $6.15M, LWCF (USFWS+NPS) ~$5.6M …",
   accept="programs incl. state bonds + LWCF; total ~$30–40M"),
 ("tpl-ca","q2"): dict(id="tplca-cd-ballot-funding-2010", trap="statewide-measure-attribution;dedup-landvote-id",
   gold="Discriminating skill = SEPARATE statewide props (jurisdiction='State', ~$4.5B baseline touching every CD) from local. Local top is the LA/coastal cluster — full-overlap: CD30/29 ~$665M, CD36/32 ~$527M, CD15 ~$509M; apportioned: CA-11 ~$170M (both defensible).",
   accept="MUST separate statewide from local measures (the graded skill). Local ranking by EITHER full-overlap OR apportioned attribution accepted; top local districts in the LA/coastal cluster. Do NOT require the full-overlap dollar figures — attribution method is unspecified by the question.",
   note="Most models self-handled the statewide trap; the 0.5 spread was full-overlap vs apportioned attribution (an unspecified, defensible fork) — an assessment-design issue, not a guidance bug. See findings/tpl-ca-q2-statewide.md."),
 ("tpl-ca","q3"): dict(id="tplca-cd-failed-measures", trap="statewide-measure-attribution;status-fail-filter",
   gold="All 52 CDs have failed measures; local-only top CD49 ~$2.15B, CD50 ~$2.13B, CD48 ~$2.12B (San Diego ~$2B driver)",
   accept="separates statewide ~$7.7B baseline; San Diego-region CDs top local"),
 ("tpl-ca","q4"): dict(id="tplca-assembly-acreage", trap="acres-from-geoparquet-not-hex;sldl-zero-pad",
   gold="AD34 563,641 ac, AD36 451,429, AD02 207,738, AD47 152,443",
   accept="AD34 top; acres from sites GeoParquet (not SUM on hex)"),
 ("tpl-ca","q5"): dict(id="tplca-lwcf-sd2", trap="program-like-lwcf;hex-district-attribution",
   gold="~$285.5M LWCF in Senate District 2 (BLM-dominated, 48 sites)",
   accept="~$250–320M; BLM-led"),
 ("tpl","q1"): dict(id="tpl-almanac-landcover", trap="mask-before-aggregate;dominant-class-per-cell;cell-count-units",
   gold="Composition by res-9 cells: herbaceous veg / needleleaf+deciduous forest / agriculture top; state leaders herb→CO, needleleaf→MT, decid→NY, ag→PA, shrubs→CA, wetland→FL",
   accept="dominant classes ~match + plausible state leaders"),
 ("tpl","q2"): dict(id="tpl-tx-cd-federal-funding", trap="sponsor_type-FED-filter;dominant-cd-assignment",
   gold="CD-34 ≈ CD-14 ~$29.5M top; CD-36 $17.0M; CD-31 $15.6M",
   accept="CD-34/CD-14 top, ~$29M"),
 ("tpl","q3"): dict(id="tpl-nj-municipalities", trap="jurisdiction-municipal-filter;dedup-landvote-id",
   gold="~261 NJ municipalities, 444 passed measures (~$1.95B); 1998–2004 wave; north-central concentration",
   accept="~261 munis / ~444 passed; 1998–2004 peak; north-central"),
 ("tpl","q4"): dict(id="tpl-boulder-funders", trap="county-name-filter;sum-amount-safe",
   gold="Boulder County sales-tax ~$321M dominant; +NRCS ~$6.3M, USFS ~$4.9M, GOCO ~$3.1M",
   accept="Boulder County local sales-tax dominant + federal/state"),
 ("wetlands","q1"): dict(id="wet-india-vuln-carbon", trap="mask-before-aggregate;dominant-class;h8-h9-join",
   gold="~1.1 Pg C (1.1e9 Mg C) total in India wetlands; top classes riverine-forested / rice paddies / temperate peatland",
   accept="~0.8–1.4e9 Mg C with by-class breakdown"),
 ("wetlands","q2"): dict(id="wet-ramsar-criterion9", trap="count-distinct-ramsarid;criterion9-nonavian-definition",
   gold="64 Ramsar sites meet Criterion 9; Criterion 9 = supports ≥1% of a population of a wetland-dependent NON-AVIAN animal species",
   accept="~60–68 sites AND correct non-avian definition (NOT waterbirds)"),
 ("wetlands","q3"): dict(id="wet-top10-hydrobasins-composite", trap="normalization-method;ncp-intensity-vs-extensive;rank-sensitivity",
   gold="L3 basin 6030007000 clear #1 (max wetland extent + carbon); top set ~as in gold (rank-sensitive tail)",
   accept="sound min-max-normalize+composite method; basin 6030007000 #1"),
}
# first-run benchmark mean accuracy (across 4 models) — the trap difficulty signal
BENCH = {"q1":{}, } # filled below from a flat dict
BENCH_FLAT = {
 "bio-ramsar-countries":0.96,"bio-ecoregion-vuln-carbon":1.0,"bosl-fishing-displaced":0.62,
 "bosl-sargasso-seamounts":1.0,"ca-gap12-acres":1.0,"ca-ecoregion-most-conserved":1.0,
 "glob-pct-land-protected":1.0,"glob-people-in-pas":0.88,"glob-least-represented-ecoregions":0.62,
 "glob-top5-pa-mammal-richness":0.75,"tplca-cd16-funders":1.0,"tplca-cd-ballot-funding-2010":0.625,
 "tplca-cd-failed-measures":0.69,"tplca-assembly-acreage":1.0,"tplca-lwcf-sd2":1.0,
 "tpl-almanac-landcover":0.94,"tpl-tx-cd-federal-funding":0.88,"tpl-nj-municipalities":0.69,
 "tpl-boulder-funders":1.0,"wet-india-vuln-carbon":0.88,"wet-ramsar-criterion9":0.69,
 "wet-top10-hydrobasins-composite":0.19,
}

def qtext(app):
    p = os.path.join(QDIR, f"{app}.txt")
    return [l.strip() for l in open(p) if l.strip()]

records = []
for app in ["biodiversity","bosl-high-seas","ca-30x30","global-30x30","tpl-ca","tpl","wetlands"]:
    qs = qtext(app)
    for i, q in enumerate(qs, 1):
        m = META[(app, f"q{i}")]
        rec = {
            "id": m["id"], "app": app, "q_idx": f"q{i}", "question": q, "mode": "answer",
            "gold": m["gold"], "accept": m["accept"],
            "trap": m["trap"].split(";"),
            "sql_ref": f"gold/{app}.md (q{i})",
            # null for grow-on-fix additions not in the original 4-model benchmark
            "bench_mean_acc": BENCH_FLAT.get(m["id"]),
        }
        if m.get("note"): rec["note"] = m["note"]
        records.append(rec)

# mode=clarify: gold response = recognize the ambiguity and ask, NOT answer.
# These are the original ambiguous phrasings of the precise answer-variants above.
CLARIFY = [
 dict(id="clarify-bosl-fishing-year", app="bosl-high-seas",
   question="How much and what type of industrial fishing would be displaced by designating all EBSAs in the high seas as MPAs, closed to fishing?",
   ambiguity="year unspecified (GFW effort spans 2012–2024; single year vs multi-year average changes the number)",
   accept="recognizes the year is unspecified and asks which year / whether to average (GFW 2012–2024); does NOT silently pick one and present a single number as definitive",
   pairs_with="bosl-fishing-displaced"),
 dict(id="clarify-tplca-attribution", app="tpl-ca",
   question="Which congressional district has raised the most conservation funding through ballot measures since 2010?",
   ambiguity="(1) statewide vs local measures; (2) how to attribute a multi-district measure's funds (full-overlap vs apportioned)",
   accept="surfaces the statewide-vs-local and/or attribution-method ambiguity and asks how to handle it; does NOT silently pick one attribution and rank",
   pairs_with="tplca-cd-ballot-funding-2010"),
 dict(id="clarify-wetlands-composite", app="wetlands",
   question="Rank the top 10 level-3 hydrobasins by wetland extent, carbon, and NCP, normalized.",
   ambiguity="normalization method and weighting unspecified (min-max vs z-score vs rank; equal vs custom weights; NCP is an intensity mixed with extensive sums)",
   accept="asks how to normalize/weight the composite (and flags the intensity-vs-extensive mismatch) before ranking; does NOT silently choose a method",
   pairs_with="wet-top10-hydrobasins-composite"),
 dict(id="clarify-ca-endemic-gap1", app="ca-30x30",
   question="What percent of GAP 1 land is in the top 20% (>=80th percentile) of endemic-species richness?",
   ambiguity="'endemic-species richness' is unspecified: (1) WHICH layer — ACE `AllTaxaEnd` (all endemic/near-endemic taxa) vs the separate rarity-weighted endemic *plant* richness layer (plants only); (2) percentile over WHICH population (all ACE hexagons → P80=4 vs nonzero-only → higher). Axis (1) dominates the answer.",
   accept="GOLD = recognizes 'endemic richness' is unspecified (at minimum the metric-layer choice) and ASKS which layer/threshold before computing. Silently answering with ANY single number — even the correct ~20.8% AllTaxaEnd value — is a FAIL; the model must ask, not guess.",
   resolution="If the user picks ACE `AllTaxaEnd`, top-20% = AllTaxaEnd ≥ P80 (=4 over distinct hexagons), GAP-1 land by sanctioned Gap1_acres/Total_Acre area-weight (reGAP=1 gives the same): **~20.8%** — ≈ the 20% expected by chance, so GAP-1 land is NOT endemic-enriched. Alternative resolutions: rarity-weighted endemic *plant* richness layer → ~25%; AllTaxaEnd with a nonzero-only percentile population / polygon-acre GAP-1 → ~30%. Operator-verified SQL in gold/ca-30x30.md (q6).",
   pairs_with=None),
]
for c in CLARIFY:
    rec = {
        "id": c["id"], "app": c["app"], "q_idx": "clarify", "question": c["question"],
        "mode": "clarify", "ambiguity": c["ambiguity"], "accept": c["accept"],
        "pairs_with": c["pairs_with"],
        "note": "Gold = identify the ambiguity and ask; NOT a data answer. Currently fails for most models (they answer anyway) — gated on a clarification-steering guidance change (geo-agent issue).",
    }
    # optional preloaded disambiguation answer(s) to provide IF the model asks
    if c.get("resolution"): rec["resolution"] = c["resolution"]
    records.append(rec)

manifest = {
    "version": "2026-06-27",
    "source_experiment": "headless/experiments/2026-06-26-or-openmodel-bench",
    "gate": {
        "mcp_target": "dev-duckdb-mcp.nrp-nautilus.io",
        "note": "Guidance-change validation runs MUST hit dev MCP (serves candidate :main guidance), not prod. The runner defaults to prod — pass --mcp-url explicitly.",
        "grading": "judge each model answer vs `gold`/`accept`; gold is operator-verified via own duckdb-geo queries (NOT model consensus).",
        "pass": "targeted trap fixed AND no regression vs this baseline (per-question, instance-level)."
    },
    "n_questions": len(records),
    "n_answer": sum(1 for r in records if r["mode"] == "answer"),
    "n_clarify": sum(1 for r in records if r["mode"] == "clarify"),
    "modes": {
        "answer": "gold = the computed answer; grade against `accept`.",
        "clarify": "gold = recognize the ambiguity and ask; grade on whether the model asks rather than silently picks. Tied to a clarification-steering guidance change.",
    },
    "questions": records,
}
out = os.path.join(HERE, "golden.json")
json.dump(manifest, open(out, "w"), indent=2)
print(f"wrote {out}: {len(records)} questions")
# also (re)write questions.txt grouped by app
with open(os.path.join(HERE, "questions.txt"), "w") as fh:
    for app in ["biodiversity","bosl-high-seas","ca-30x30","global-30x30","tpl-ca","tpl","wetlands"]:
        fh.write(f"# {app}\n")
        for q in qtext(app): fh.write(q + "\n")
        fh.write("\n")
print("wrote questions.txt")
# difficulty summary
ans = [r for r in records if r["mode"] == "answer"]
clar = [r for r in records if r["mode"] == "clarify"]
print(f"  modes: {len(ans)} answer, {len(clar)} clarify")
scored = [r for r in ans if r["bench_mean_acc"] is not None]
hard = sorted(scored, key=lambda r: r["bench_mean_acc"])[:5]
print("hardest answer-mode (trap-rule seeds):")
for r in hard: print(f"  {r['bench_mean_acc']:.2f}  {r['id']:32} traps={r['trap']}")
new = [r for r in ans if r["bench_mean_acc"] is None]
if new:
    print(f"grow-on-fix additions (no first-run bench): {len(new)}")
    for r in new: print(f"  --    {r['id']:32} traps={r['trap']}")
