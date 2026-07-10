# qwen3-cirrus speed benchmark (2026-07-09)

**Model:** `qwen3-6` = `cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit` on `qwen3-cirrus.carlboettiger.info`
(OpenAI-compatible vLLM 0.24.0). Routed through the proxy as model `qwen3-6`
(provider block already on `main`; `enable_thinking` on). Same base model as nimbus's
`qwen` but **AWQ INT4** vs nimbus **NVFP4**.

**Method.** Two instruments:
1. **Overall speed for our queries** — 6 gold questions replayed through the full agent
   loop (`headless/run.js` → proxy → cirrus), thinking on. Per-call latency/tokens/tool
   rounds from transcripts.
2. **Exact prefill/decode split** — cirrus is **not** scraped by NRP Prometheus, but its
   vLLM `/metrics` is open. `cirrus_metrics.py` snapshots the cumulative counters
   before/after and diffs them (exact split over precisely the run window). `microbench.py`
   adds a controlled uncached prefill-vs-size curve + isolated decode probe, run identically
   against **nimbus** for a head-to-head.

---

## 1. Overall speed for our queries (agent loop, thinking on)

| # | app | question | wall | tool rounds | prompt tok (cum) | out tok | result |
|--|--|--|--:|--:|--:|--:|--|
| 1 | ca-30x30 | GAP 1/2 conserved acres | 156s | 2 | 105k | 1041 | ✅ 26.47M (exact) |
| 2 | ca-30x30 | top ecoregion by conserved acreage | 174s | 3 | 108k | 1119 | ✅ Mojave 7.37M (exact) |
| 3 | tpl-ca | CD-16 funders + amounts | 273s | 5 | 196k | 1969 | ✅ program table |
| 4 | tpl-ca | LWCF in Senate District 2 | **timeout @420s** | 18 | 579k | 3015 | ❌ looped, no answer |
| 5 | bosl-high-seas | seamounts in Sargasso EBSA | 112s | 3 | 83k | 971 | ✅ 141 (exact) |
| 6 | tpl | Boulder County funders (map) | 231s | 9 | 216k | 2109 | ⚠️ mapped sites, missed funding breakdown |

- **Typical completed query: ~2–4.5 min** (median ~3 min), 2–9 tool rounds.
- **5/6 completed; accuracy 4/6 solid.** Q4 looped to the 420s cap (18 tool calls) — the
  known tool-call-format instability of this base model (same failure class flagged for
  nimbus `qwen`). Q6 did the map but answered the wrong sub-question.
- Per-**turn** server e2e latency averaged **34.7s** (TTFT ~17s + decode). Wall time scales
  with tool-round count.

## 2. Exact prefill/decode split (server-side, 36 turns of the gold run)

| metric | value |
|--|--|
| prefix-cache hit | **86.6%** of prompt tokens (loop resends growing context at temp 0) |
| prefill time / decode time | 580s (47%) / 661s (**53%**) |
| effective prefill throughput | 2258 tok/s (cache-inflated) |
| **decode throughput @ ~36k ctx** | **16.5 tok/s** |
| avg TTFT (prefill latency) | 17.3 s/turn |
| avg e2e latency | 34.7 s/turn |

Decode-bound **in this workload** — but only because prefix caching removes most prefill
recompute while doing nothing for decode (every generated token still attends the full KV
cache). See §3 for why that's a cirrus-specific scaling problem, not fundamental.

## 3. cirrus vs nimbus, matched method (uncached, thinking off, single stream)

**Prefill throughput vs prompt size** — the decisive result:

| per-req prompt | cirrus tok/s | nimbus tok/s | nimbus advantage |
|--:|--:|--:|--:|
| ~3.3k | 1959 | 6381 | 3.3× |
| ~13k | 904 | 6392 | 7× |
| ~26k | 515 | 5780 | 11× |

**nimbus prefill is flat (~6.3k tok/s) across context; cirrus collapses (~quadratic).**

**Decode throughput:**

| context | cirrus tok/s | nimbus tok/s |
|--|--:|--:|
| trivial (~few hundred tok) | **114** | 100 |
| ~25k | (exceeded 100s ceiling; gold agg **16.5** @36k) | **80.9** |

- **At low context cirrus decode is *faster* than nimbus (114 vs 100).** AWQ INT4 is *not*
  the problem for decode.
- **At long context cirrus collapses on both phases** (decode 114→16.5, ~7×; prefill
  1959→515) while **nimbus holds throughput roughly flat** (decode 100→81, prefill flat).

**Why decode looked slow vs the GB10 (answer to the question):** it's **not** quantization
and **not** decode-per-se — it's **long-context attention scaling**. nimbus's GB10
(Blackwell + NVFP4 native FP4 tensor cores + an efficient/flash-style attention kernel)
keeps prefill and decode near-flat as context grows. cirrus's throughput degrades
superlinearly with context on *both* phases — the signature of an unoptimized/eager
attention kernel and/or a bandwidth/compute-limited GPU. Since our geo-agent queries run at
25k–200k cumulative context, cirrus lands in exactly the regime where it's 5–11× slower,
even though it ties/beats nimbus at short context.

## 4. Cloudflare 524 (resolved during the session)

The direct microbench hit **HTTP 524** on cold >100s prefills — Cloudflare's edge proxy
enforces a ~100s origin-response timeout. `*.carlboettiger.info` was proxied through
Cloudflare (orange cloud). The proxy path was via **external-dns + Traefik on the k3s
cluster**, with Cloudflare proxying optional. **cirrus's Cloudflare proxy was turned off**
mid-session → now resolves direct to the Berkeley origin (`128.32.85.8`, `server: uvicorn`,
no `cf-ray`); nimbus was never proxied (`169.229.53.67`). The 100s ceiling is gone for
direct calls. (The agent path via `open-llm-proxy.nrp-nautilus.io` / haproxy was never
subject to it.)

## Tooling added (this repo)
- `headless/cirrus_metrics.py` — snapshot/diff vLLM `/metrics` for exact prefill/decode
  split on endpoints Prometheus doesn't scrape (cirrus, nimbus). `snap` / `diff`.
- `headless/experiments/2026-07-09-cirrus-speed/` — `run.sh` (bracketed gold run),
  `microbench.py` (prefill curve + decode probe), transcripts, snapshots, this report.
