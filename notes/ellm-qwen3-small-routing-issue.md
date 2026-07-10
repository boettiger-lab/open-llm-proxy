# ellm: qwen3-small latency dominated by replica scatter, not compute — needs prefix-aware routing

**Target:** NRP `ellm.nrp-nautilus.io` gateway / vLLM deployment maintainers
**Reporter context:** geo-agent apps (wyoming, global-30x30, padus, …) route all NRP traffic through `ellm.nrp-nautilus.io/v1/chat/completions`.

## Summary

The `qwen3-small` alias (`Qwen/Qwen3.6-27B`) is served by **two replicas** with **independent KV caches**, and the gateway load-balances requests across them **~50/50 with no prefix/session affinity**. Because the geo-agent workload re-sends a **byte-stable ~24k-token prefix** (system prompt + STAC catalog + 25 tool schemas) on every turn of a 9–17 call tool-use loop, the ideal case is a near-total prefix-cache hit on turns 2..N. Instead, consecutive turns scatter across the two pods, so the warm prefix on pod A is useless when the next turn lands on pod B. Result: latency for the *same* stable prefix swings **3.8s ↔ 182s**, and the recent prefix-cache hit rate sits at **65–71%** instead of the ~95%+ this workload should achieve.

## Evidence

**Client-side (open-llm-proxy logs, wyoming, 48h — 73 calls / 6 sessions):**
- Every call carries ~24k prompt tokens; output averages only **433 tokens** → prefill-bound, not generation-bound.
- Latency is **uncorrelated with prompt size** and **non-monotonic within a session**:
  - 31,831-token call → **2.1s**; 19,520-token call → **67s** (smaller prompt, 32× slower).
  - Session `7827eb9d`: turn 1 (cold, 23.7k) → 3.8s; turn 2 (24.8k) → 44s.
- A 2.1s response to a 31k-token prompt is **only physically possible with a large prefix-cache hit** — proof caching works when the request lands on a warm replica.
- The harness (`geo-agent/app/agent.js:138-187`) builds `turnMessages` once per turn (constant system prompt first) and **only appends** within the tool loop → the prefix is append-only and byte-stable. The client is doing everything right; the miss is server-side.

**Server-side (prometheus.nrp-nautilus.io):**
- Two pods serve `Qwen/Qwen3.6-27B`: `qwen3-small-vllm-inference-0` and `qwen3-27b-h200-0`.
- Traffic split ~50/50 over 48h: **12,251 vs 11,925** successful requests → active round-robin, not failover.
- Recent prefix-cache hit rate: **65.8%** and **71.3%** (lifetime 50.7% / 55.9%). With random 2-way routing, ~half of turns land on the replica that lacks the session's warm tail — matches the observed rate.
- **Load imbalance:** the weaker (non-H200) pod peaked at **33 concurrent / 13 queued** while the H200 pod sat at ~12 concurrent / ≤4 queued. The 40–180s tail (and the 600s outlier) is requests queueing on the overloaded pod.

## Root cause

1. **No prefix/session-affinity routing** across the two replicas → the stable 24k prefix can't be reused reliably → repeated cold prefill.
2. **Load imbalance** → the weaker replica absorbs bursts (33 concurrent, 13 queued) while the H200 idles → long queueing tail.

Neither is a model, prompt, or client-harness problem.

## Requested changes (NRP-side)

1. **Enable prefix-aware / session-sticky routing** at the ellm gateway for multi-replica models (route by prefix hash or session so a conversation's turns stick to one replica). Expected effect: hit rate → ~95%, collapsing the prefill tail toward the 2–8s warm-hit floor already observed.
2. **Balance by capacity/least-load**, not round-robin — the H200 pod can take a larger share; the weaker pod shouldn't hit 33 concurrent while the H200 idles.

## How to reproduce / verify

```
# per-pod prefix-cache hit rate (recent)
100*rate(vllm:prefix_cache_hits_total{model_name="Qwen/Qwen3.6-27B"}[1h])
  /rate(vllm:prefix_cache_queries_total{model_name="Qwen/Qwen3.6-27B"}[1h])
# per-pod request split / queue depth / concurrency
increase(vllm:request_success_total{model_name="Qwen/Qwen3.6-27B"}[48h])
max_over_time(vllm:num_requests_waiting{model_name="Qwen/Qwen3.6-27B"}[48h])
max_over_time(vllm:num_requests_running{model_name="Qwen/Qwen3.6-27B"}[48h])
```
Client-side latency-vs-prefix analysis: query `open-llm-proxy` logs for `origin='https://wyoming.nrp-nautilus.io'`, compare `tokens.prompt_tokens` vs `latency_ms` per turn within a `session_id`.
