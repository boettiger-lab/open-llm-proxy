# CA 30x30 app — all unique user questions

Source: `open-llm-proxy` logs for `origin = https://ca-30x30.nrp-nautilus.io`
(consolidated Parquet history + today's raw JSONL), generated 2026-07-10.

**19 distinct questions** across 16 sessions (2026-06-20 → 2026-07-10),
models: `minimax-m2`, `qwen`, `qwen3`, `qwen3-small`, `gemma`.

> **Logging caveat — this list is the *first* question of each session only.**
> The proxy records one `user_question` per session (the opening prompt) and
> does not update it on follow-up turns, and the raw request payload/messages
> array is not stored. Verbatim mid-session follow-ups (e.g. "now show it on the
> map", "break that down by county") are therefore **not recoverable** from the
> logs — only inferable from the assistant's tool calls and answers. If we want
> every turn captured verbatim, the app/proxy needs to log each turn's user
> message, not just the session opener.

## Statewide protection totals (the "how much is protected" family)

- How much of California is protected?
- how much of ca is protected
- How much land is protected for 30x30 in California?
- how much is protected at 30x30?
- What percent of lands in California are protected for 30x30?
- What percent of California is gap 1 or gap 2?
- How many acres of California land are conserved at GAP status 1 or 2?

## Habitat-specific (CWHR13)

- Make me a table of the major habitats CWHR13 and how much of each is protected by lands that count for 30x30, by other protected lands, and how much of each is non-conserved, by percent of CA land
- How much of every major habitat type (CWHR13) is protected in 30x30 lands
- What percent of hardwood woodland is protected by 30x30 lands
- What percent of hardwood woodland is protected by 30x30 lands? show on the map
- What percent of hardwood woodland is protected by 30x30 lands? show only those areas on the map
- how much hardwood woodland is protected? show on the map

## Biodiversity / ACE

- Show me bird species richness across the state.
- Show me the ACE statewide biodiversity rank.
- what percentage of gap 1 land is in 80% percentile or higher of biodiversity for endemic species

## Data sources & definitions

- Can you give me the FMMP links?
- link me to the official source for FMMP data
- what does CWHR stand for

---

### Full table (verbatim, with first/last seen and models)

| Question | First seen | Last seen | Models |
|---|---|---|---|
| How many acres of California land are conserved at GAP status 1 or 2? | 2026-06-20 | 2026-07-08 | minimax-m2, qwen |
| Show me bird species richness across the state. | 2026-06-20 | 2026-07-08 | minimax-m2, qwen |
| What percent of California is gap 1 or gap 2? | 2026-06-22 | 2026-06-22 | minimax-m2 |
| what percentage of gap 1 land is in 80% percentile or higher of biodiversity for endemic species | 2026-06-22 | 2026-06-22 | minimax-m2 |
| What percent of lands in California are protected for 30x30? | 2026-06-23 | 2026-06-24 | minimax-m2 |
| Can you give me the FMMP links? | 2026-06-24 | 2026-06-24 | qwen3 |
| Make me a table of the major habitats CWHR13 and how much of each is protected by lands that count for 30x30, by other protected lands, and how much of each is non-conserved, by percent of CA land | 2026-06-24 | 2026-06-24 | qwen3 |
| What percent of hardwood woodland is protected by 30x30 lands | 2026-06-24 | 2026-06-24 | minimax-m2, qwen3 |
| What percent of hardwood woodland is protected by 30x30 lands? show on the map | 2026-06-24 | 2026-06-24 | gemma |
| What percent of hardwood woodland is protected by 30x30 lands? show only those areas on the map | 2026-06-24 | 2026-06-24 | qwen3-small |
| how much hardwood woodland is protected? show on the map | 2026-06-24 | 2026-06-24 | qwen3 |
| link me to the official source for FMMP data | 2026-06-24 | 2026-06-24 | qwen3 |
| what does CWHR stand for | 2026-06-24 | 2026-06-24 | qwen3 |
| How much land is protected for 30x30 in California? | 2026-06-30 | 2026-06-30 | qwen3 |
| How much of every major habitat type (CWHR13) is protected in 30x30 lands | 2026-07-07 | 2026-07-07 | qwen |
| how much is protected at 30x30? | 2026-07-07 | 2026-07-07 | qwen |
| Show me the ACE statewide biodiversity rank. | 2026-07-08 | 2026-07-08 | qwen |
| How much of California is protected? | 2026-07-10 | 2026-07-10 | qwen |
| how much of ca is protected | 2026-07-10 | 2026-07-10 | qwen |
