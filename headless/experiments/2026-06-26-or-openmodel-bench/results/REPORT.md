# OpenRouter open-model benchmark - results

Run 2026-06-26/27. 22 analytical questions x 4 models x 3 trials. Gold = independent duckdb-geo MCP queries (see gold/).

> **Budget-cap caveat:** the OpenRouter account hit its monthly spend limit at 00:49-00:55 UTC near the end of the run. 82 of 264 trials returned 'Org member budget limit exceeded' instead of an answer. This reduces trials-per-cell in the tail but every (model x question) cell still has >=1 graded trial. Accuracy is over completed trials; complete% captures the cap.

## Per-model summary

| model | attempts | completed | budget-capped | complete% | accuracy | median wall | median turns |
|---|--:|--:|--:|--:|--:|--:|--:|
| glm-5.2 | 65 | 47 | 18 | 72% | **86%** | 145s | 4 |
| minimax-m3 | 66 | 45 | 21 | 68% | **77%** | 88s | 4 |
| kimi-2.7-code | 65 | 43 | 22 | 66% | **90%** | 123s | 6 |
| nemotron-3-ultra | 66 | 45 | 21 | 68% | **92%** | 68s | 5 |

Accuracy = mean judge score (1 correct / 0.5 partial / 0 wrong) over completed trials.

## Per-question accuracy (mean score across completed trials)

| app | q | glm-5.2 | minimax-m3 | kimi-2.7-code | nemotron-3-ultra |
|---|---|--:|--:|--:|--:|
| biodiversity | q1 | 1.00 | 1.00 | 1.00 | 0.83 |
| biodiversity | q2 | 1.00 | 1.00 | 1.00 | 1.00 |
| bosl-high-seas | q1 | 0.33 | 0.50 | 0.83 | 0.83 |
| bosl-high-seas | q2 | 1.00 | 1.00 | 1.00 | 1.00 |
| ca-30x30 | q1 | 1.00 | 1.00 | 1.00 | 1.00 |
| ca-30x30 | q2 | 1.00 | 1.00 | 1.00 | 1.00 |
| global-30x30 | q1 | 1.00 | 1.00 | 1.00 | 1.00 |
| global-30x30 | q2 | 1.00 | 1.00 | 0.50 | 1.00 |
| global-30x30 | q3 | 1.00 | 0.50 | 0.00 | 1.00 |
| global-30x30 | q4 | 0.50 | 0.50 | 1.00 | 1.00 |
| tpl | q1 | 1.00 | 0.75 | 1.00 | 1.00 |
| tpl | q2 | 1.00 | 0.50 | 1.00 | 1.00 |
| tpl | q3 | 0.25 | 0.50 | 1.00 | 1.00 |
| tpl | q4 | 1.00 | 1.00 | 1.00 | 1.00 |
| tpl-ca | q1 | 1.00 | 1.00 | 1.00 | 1.00 |
| tpl-ca | q2 | 0.50 | 0.50 | 0.75 | 0.75 |
| tpl-ca | q3 | 1.00 | 0.75 | 1.00 | 0.50 |
| tpl-ca | q4 | 1.00 | 1.00 | 1.00 | 1.00 |
| tpl-ca | q5 | 1.00 | 1.00 | 1.00 | 1.00 |
| wetlands-v2 | q1 | 1.00 | 0.50 | 0.50 | 1.00 |
| wetlands-v2 | q2 | 1.00 | 0.00 | 1.00 | 0.75 |
| wetlands-v2 | q3 | 0.25 | 0.00 | 0.00 | 0.50 |

## Per-question median wall-clock seconds (completed trials)

| app | q | glm-5.2 | minimax-m3 | kimi-2.7-code | nemotron-3-ultra |
|---|---|--:|--:|--:|--:|
| biodiversity | q1 | 38 | 34 | 30 | 54 |
| biodiversity | q2 | 264 | 135 | 240 | 138 |
| bosl-high-seas | q1 | 580 | 270 | 308 | 195 |
| bosl-high-seas | q2 | 132 | 56 | 135 | 33 |
| ca-30x30 | q1 | 28 | 20 | 18 | 33 |
| ca-30x30 | q2 | 59 | 18 | 44 | 27 |
| global-30x30 | q1 | 388 | 111 | 284 | 368 |
| global-30x30 | q2 | 413 | 225 | 869 | 164 |
| global-30x30 | q3 | 156 | 112 | 714 | 164 |
| global-30x30 | q4 | 13 | 196 | 200 | 108 |
| tpl | q1 | 356 | 138 | 298 | 205 |
| tpl | q2 | 92 | 616 | 165 | 65 |
| tpl | q3 | 137 | 87 | 144 | 54 |
| tpl | q4 | 68 | 117 | 72 | 42 |
| tpl-ca | q1 | 103 | 55 | 91 | 62 |
| tpl-ca | q2 | 216 | 118 | 212 | 105 |
| tpl-ca | q3 | 276 | 110 | 147 | 142 |
| tpl-ca | q4 | 192 | 105 | 130 | 29 |
| tpl-ca | q5 | 153 | 73 | 42 | 65 |
| wetlands-v2 | q1 | 265 | 83 | 123 | 127 |
| wetlands-v2 | q2 | 76 | 68 | 51 | 43 |
| wetlands-v2 | q3 | 451 | 778 | 836 | 449 |
