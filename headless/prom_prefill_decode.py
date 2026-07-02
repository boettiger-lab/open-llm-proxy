#!/usr/bin/env python3
"""Issue #282: per-model prefill-vs-decode TIME split from vLLM Prometheus metrics.

Pulls, per model_name over a window, the summed prefill/decode time, prompt/gen
tokens, and prefix-cache hits/queries, then derives the time split and the
prefill-vs-decode *rate* ratio (the 10-100x hypothesis) and cache-hit rate.

No PROXY_KEY needed — reads production serving metrics directly.
"""
import sys, json, urllib.parse, urllib.request

PROM = "https://prometheus.nrp-nautilus.io/api/v1/query"
WINDOW = sys.argv[1] if len(sys.argv) > 1 else "24h"

def q(expr):
    url = PROM + "?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=30) as r:
        d = json.load(r)
    out = {}
    for res in d.get("data", {}).get("result", []):
        m = res["metric"].get("model_name", "?")
        try:
            out[m] = float(res["value"][1])
        except (ValueError, TypeError):
            pass
    return out

def series(metric):
    return q(f'sum by (model_name)(increase({metric}{{}}[{WINDOW}]))')

prefill_s   = series("vllm:request_prefill_time_seconds_sum")
prefill_n   = series("vllm:request_prefill_time_seconds_count")
decode_s    = series("vllm:request_decode_time_seconds_sum")
decode_n    = series("vllm:request_decode_time_seconds_count")
prompt_tok  = series("vllm:prompt_tokens_total")
gen_tok     = series("vllm:generation_tokens_total")
cache_hit   = series("vllm:prefix_cache_hits_total")
cache_qry   = series("vllm:prefix_cache_queries_total")

models = sorted(set(prefill_s) | set(decode_s), key=lambda m: -(decode_s.get(m, 0)))

def g(d, m):
    return d.get(m, 0.0)

hdr = ("model", "reqs", "prefill_s", "decode_s", "dec/pre",
       "prefill_tok/s", "decode_tok/s", "rate_x", "decode_%time", "cache_hit%")
w = [34, 6, 9, 9, 7, 13, 12, 7, 12, 10]
def row(vals):
    print("  ".join(str(v).rjust(w[i]) for i, v in enumerate(vals)))

print(f"\nvLLM prefill-vs-decode split — window={WINDOW}  (source: prometheus.nrp-nautilus.io)\n")
row(hdr)
row(["-"*x for x in w])
for m in models:
    ps, pn = g(prefill_s, m), g(prefill_n, m)
    ds, dn = g(decode_s, m), g(decode_n, m)
    pt, gt = g(prompt_tok, m), g(gen_tok, m)
    ch, cq = g(cache_hit, m), g(cache_qry, m)
    if pn < 1 and dn < 1:
        continue
    pre_avg = ps/pn if pn else 0
    dec_avg = ds/dn if dn else 0
    pre_rate = pt/ps if ps else 0          # prompt tokens processed / prefill sec
    dec_rate = gt/ds if ds else 0          # generated tokens / decode sec
    rate_x = pre_rate/dec_rate if dec_rate else 0
    dec_time_pct = 100*ds/(ps+ds) if (ps+ds) else 0
    hit = 100*ch/cq if cq else 0
    dpr = dec_avg/pre_avg if pre_avg else 0
    row([m[:34], int(max(pn, dn)),
         f"{pre_avg:.2f}", f"{dec_avg:.2f}", f"{dpr:.1f}",
         f"{pre_rate:.0f}", f"{dec_rate:.1f}", f"{rate_x:.0f}",
         f"{dec_time_pct:.0f}%", f"{hit:.0f}%"])

print("""
Legend:
  prefill_s / decode_s = avg seconds per request in each phase
  dec/pre              = decode time ÷ prefill time (how decode-bound per request)
  prefill_tok/s        = prompt tokens ÷ total prefill seconds (aggregate prefill throughput)
  decode_tok/s         = generated tokens ÷ total decode seconds (aggregate decode throughput)
  rate_x               = prefill_tok/s ÷ decode_tok/s  (the "10-100x" hypothesis)
  decode_%time         = share of LLM compute time spent decoding
  cache_hit%           = vLLM prefix-cache hit rate (prefill work we already avoid)
""")
