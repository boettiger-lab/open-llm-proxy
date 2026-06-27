# Ambiguity audit of the hard benchmark questions

For each all/most-models-wrong question we asked: did models **reason correctly but
differently** (→ ambiguity, fix the question/gold), or **fail** (→ real trap / capability)?
Evidence = the `agent_runner_orbench` transcripts + judge notes.

| question | mean acc | classification | evidence | action |
|---|--:|---|---|---|
| **tpl-ca q2** ballot funding | 0.62 | **ambiguity** (attribution method) | most separated statewide; spread = full-overlap vs apportioned | precise gold/accept (done); clarify-variant |
| **bosl-high-seas q1** fishing displaced | 0.62 | **ambiguity** (year) | completed answers used 2024 / 2012–2022 avg / 2021–2024 — all defensible; gold locked 2024 | **specify year**; clarify-variant |
| **wetlands q3** hydrobasins composite | 0.19 | **hard + rank-sensitive** (mild spec-gap) | answerers converged on min-max; failures were no-answer/loops + unstable tail | specify equal-weight to remove residual DOF; stays genuinely hard |
| **global-30x30 q3** least-represented ecoregions | 0.62 | **capability + tie** | 1.0s named the 0% set; 0.5/0 were truncated/no-answer; ~32-way 0% tie | phrase the tie ("list those at ~0%") |
| **global-30x30 q4** top-5 PA mammal richness | 0.75 | **catalog gap** | app lacks a mammal-richness layer | metadata, not reasoning (separate) |

## Revisions applied (answer-variant questions → precise)
- **bosl-q1:** added "Use the most recent year (2024) of Global Fishing Watch effort, in apparent fishing hours."
- **glob-q3:** "...lowest share of their area inside a protected area? List those at approximately 0%."
- **wetlands-q3:** added "(min-max normalize each metric to [0,1] across basins, then rank by the equal-weighted mean)."
- **tpl-ca q2:** accept now grades the discriminating skill (statewide separation), accepts either attribution method (see `tpl-ca-q2-statewide.md`).

## The clarification-seeking track (new)
The *original* ambiguous phrasings are valuable as a **different test**: a good agent should
**recognize the under-specification and ask**, not silently pick. We add `mode:"clarify"`
baseline questions (the original ambiguous wordings) whose **gold answer is identifying the
ambiguity and asking a focused clarifying question** — NOT producing a data answer.

Current models almost always silently pick an interpretation, so these will *fail* until the
harness steers clarification (proposed in geo-agent — see issue link in README). That makes
them a forward-looking gate item tied to a specific guidance change, validated the same way
as any other guidance change.

## Generalizable rule (carry into assessment design)
Before treating an all-wrong question as a guidance trap: **check whether models reasoned
right but differently.** If so it's ambiguity — fix the *question* (specify the missing axis)
and grade the *discriminating skill* via an `accept` rule, not exact-match to one gold number.
And keep an ambiguous twin as a `clarify`-mode test of "ask, don't guess."
