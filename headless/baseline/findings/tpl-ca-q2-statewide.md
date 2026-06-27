# Root-cause: tpl-ca q2 — "CDs that raised the most conservation funding via ballot measures since 2010"

Diagnosed via the `geo-agent-training` workflow against the `agent_runner_orbench`
sessions (12 sessions, 37 `query` calls across 4 models).

## What we expected (the hypothesis)
Models attribute *statewide* CA propositions (`jurisdiction='State'`, which touch every
congressional district) fully to each CD, flattening the ranking. → a guidance/metadata
trap about the landvote `jurisdiction` field.

## What the logs actually show
**Most models found the statewide trap on their own.** Queries referencing
`jurisdiction != 'State'` / statewide: glm 3/4, kimi 14/20, minimax 3/3, nemotron 8/10.
They explored (`get_schema landvote` → query jurisdiction) and self-corrected. The
statewide trap is *real but largely self-handled* — it is **not** the main source of the
0.5–0.75 scores.

The real discriminator is a **second, genuinely ambiguous methodology fork the question
doesn't specify: how to attribute a multi-district measure's funding.**
- **Full-overlap** (gold's choice): attribute each measure's *full* `conservation_funds_approved`
  to every CD it overlaps. → CD30/29 ~$665M top. Scored 1.0 (matches gold).
- **Apportioned**: divide a measure's funds across the districts/cells it spans (area- or
  cell-weighted). → e.g. CA-11 ~$169–179M top. Scored 0.5 ("wrong magnitude/ranking").

Apportionment is arguably the *more* correct interpretation of "which district raised the
funding," yet it was penalized because the gold locked full-overlap. A few answers also
reframed `approved` vs `at_stake` — another unspecified axis.

## Layer attribution
- **Primary cause: the assessment, not any of the 4 geo-agent layers.** The question is
  under-specified on attribution method, and the gold over-specified it — penalizing
  defensible answers. This is an **assessment-design** problem (baseline question/gold),
  fixable in *this* repo.
- **Secondary (real but minor): landvote `jurisdiction` semantics.** The minority of
  queries that didn't separate statewide measures would benefit from the dataset
  documenting that `jurisdiction='State'` measures span all districts. Canonical home =
  **STAC dataset description (Layer 1, data-workflows)**. Per geo-agent#42, the
  higher-leverage version is a **runtime result-shape advisory**: when landvote rows with
  `jurisdiction='State'` are being joined to districts, attach a note — "statewide measures
  touch every district; separate them or apportion." → geo-agent framework (surface #1).

## Fixes applied / proposed
1. **Assessment (done here):** the baseline `accept` for this question now tests the *real*
   skill — **did the model separate statewide from local measures?** — and accepts *either*
   full-overlap or apportioned local attribution, as long as statewide is separated and the
   top local districts land in the LA/coastal cluster. `gold` documents both methods.
2. **Layer 1 (issue):** data-workflows — add `jurisdiction` field semantics to the landvote
   STAC description (State vs County vs Municipal; statewide measures span all districts).
3. **#42 (tracked):** a runtime statewide-measure advisory is the higher-leverage alternative
   to prose; logged against geo-agent#42's surface-#1 list.

## Generalizable lesson for training/assessment
Some "hard questions" are hard because they are **ambiguous and the gold locks one
defensible interpretation**. Before treating an all/most-models-wrong question as a guidance
trap, check whether the models *reasoned correctly but differently*. The gate's `accept`
rule should test the **discriminating skill** (here: statewide separation), not an arbitrary
modeling choice (here: attribution method). This is why the baseline grades on
`accept`-rules, not exact-match to a single gold number.
