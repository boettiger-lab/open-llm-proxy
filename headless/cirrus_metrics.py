#!/usr/bin/env python3
"""Exact prefill/decode split for the Carl-hosted vLLM endpoints (cirrus, nimbus)
from their OPEN /metrics scrape endpoint (no auth, no Prometheus server needed).

Those endpoints are NOT scraped by prometheus.nrp-nautilus.io, so prom_prefill_decode.py
can't see them. But vLLM exposes cumulative counters at /metrics; snapshot before and
after a benchmark run and diff to get the exact split over precisely that window.

Usage:
  python3 cirrus_metrics.py snap  OUT.json            # write a snapshot
  python3 cirrus_metrics.py diff  A.json B.json        # derive split over [A,B]

Env:
  METRICS_URL   default https://qwen3-cirrus.carlboettiger.info/metrics
  MODEL_NAME    default qwen3-6   (vLLM's model_name label; 'qwen' for nimbus)
"""
import sys, os, json, re, urllib.request

URL   = os.environ.get("METRICS_URL", "https://qwen3-cirrus.carlboettiger.info/metrics")
MODEL = os.environ.get("MODEL_NAME", "qwen3-6")

# metric name -> whether it's a histogram (we grab _sum and _count) or a plain counter/gauge
HIST = [
    "vllm:request_prefill_time_seconds",
    "vllm:request_decode_time_seconds",
    "vllm:time_to_first_token_seconds",
    "vllm:inter_token_latency_seconds",
    "vllm:e2e_request_latency_seconds",
    "vllm:request_queue_time_seconds",
    "vllm:request_inference_time_seconds",
    "vllm:request_prompt_tokens",
    "vllm:request_generation_tokens",
]
COUNTER = [
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:prompt_tokens_cached_total",
    "vllm:prefix_cache_queries_total",
    "vllm:prefix_cache_hits_total",
]
GAUGE = ["vllm:num_requests_running", "vllm:num_requests_waiting", "vllm:kv_cache_usage_perc"]


def parse(text):
    """Return {metric_key: value} for our MODEL only. Histograms -> name_sum/name_count."""
    out = {}
    want_line = {}
    for m in HIST:
        want_line[m + "_sum"] = m + "_sum"
        want_line[m + "_count"] = m + "_count"
    for m in COUNTER + GAUGE:
        want_line[m] = m
    # success_total broken out by finished_reason
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        # name{labels} value   OR   name value
        mobj = re.match(r'^([a-zA-Z_:][^ {]*)(\{[^}]*\})?\s+(\S+)$', line)
        if not mobj:
            continue
        name, labels, val = mobj.group(1), mobj.group(2) or "", mobj.group(3)
        if 'model_name="%s"' % MODEL not in labels and labels != "":
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        if name in want_line:
            out[want_line[name]] = v
        elif name == "vllm:request_success_total":
            r = re.search(r'finished_reason="([^"]+)"', labels)
            if r:
                out["success:" + r.group(1)] = v
    return out


def snap(path):
    req = urllib.request.Request(URL, headers={"User-Agent": "curl/8.0 cirrus-metrics"})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    data = parse(text)
    data["_url"] = URL
    data["_model"] = MODEL
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    running = data.get("vllm:num_requests_running", 0)
    waiting = data.get("vllm:num_requests_waiting", 0)
    print(f"snapshot -> {path}  (running={running:.0f} waiting={waiting:.0f}, "
          f"prompt_tok={data.get('vllm:prompt_tokens_total',0):.0f} "
          f"gen_tok={data.get('vllm:generation_tokens_total',0):.0f})")


def d(b, a, k):
    return b.get(k, 0.0) - a.get(k, 0.0)


def diff(pa, pb):
    a = json.load(open(pa)); b = json.load(open(pb))
    reqs_pre = d(b, a, "vllm:request_prefill_time_seconds_count")
    reqs_dec = d(b, a, "vllm:request_decode_time_seconds_count")
    reqs = max(reqs_pre, reqs_dec)
    pref_s = d(b, a, "vllm:request_prefill_time_seconds_sum")
    dec_s  = d(b, a, "vllm:request_decode_time_seconds_sum")
    ttft_s = d(b, a, "vllm:time_to_first_token_seconds_sum")
    ttft_n = d(b, a, "vllm:time_to_first_token_seconds_count")
    itl_s  = d(b, a, "vllm:inter_token_latency_seconds_sum")
    itl_n  = d(b, a, "vllm:inter_token_latency_seconds_count")
    e2e_s  = d(b, a, "vllm:e2e_request_latency_seconds_sum")
    q_s    = d(b, a, "vllm:request_queue_time_seconds_sum")
    ptok   = d(b, a, "vllm:prompt_tokens_total")
    gtok   = d(b, a, "vllm:generation_tokens_total")
    pcache = d(b, a, "vllm:prompt_tokens_cached_total")
    chit   = d(b, a, "vllm:prefix_cache_hits_total")
    cqry   = d(b, a, "vllm:prefix_cache_queries_total")

    def rate(tok, sec):
        return tok / sec if sec > 0 else 0.0

    pre_rate = rate(ptok, pref_s)
    dec_rate = rate(gtok, dec_s)
    print(f"\nExact prefill/decode split over window  (model={a.get('_model')}, {a.get('_url')})")
    print(f"requests in window:        {reqs:.0f}")
    print(f"prompt tokens (prefill):   {ptok:,.0f}   ({pcache:,.0f} cached, "
          f"{100*pcache/ptok if ptok else 0:.0f}% of prompt)")
    print(f"generation tokens (decode):{gtok:,.0f}")
    print(f"prefix-cache hit rate:     {100*chit/cqry if cqry else 0:.1f}%  ({chit:,.0f}/{cqry:,.0f} tokens)")
    print("-" * 62)
    print(f"prefill time  (sum):       {pref_s:8.2f} s   avg {pref_s/reqs if reqs else 0:6.3f} s/req")
    print(f"decode  time  (sum):       {dec_s:8.2f} s   avg {dec_s/reqs if reqs else 0:6.3f} s/req")
    print(f"queue   time  (sum):       {q_s:8.2f} s")
    print(f"e2e latency   (sum):       {e2e_s:8.2f} s   avg {e2e_s/reqs if reqs else 0:6.3f} s/req")
    print(f"decode share of infer time:{100*dec_s/(pref_s+dec_s) if (pref_s+dec_s) else 0:5.0f}%")
    print("-" * 62)
    print(f"PREFILL throughput:        {pre_rate:8.0f} tok/s")
    print(f"DECODE  throughput:        {dec_rate:8.1f} tok/s")
    print(f"prefill/decode rate ratio: {pre_rate/dec_rate if dec_rate else 0:8.1f}x")
    print("-" * 62)
    print(f"avg TTFT (prefill latency):{ttft_s/ttft_n if ttft_n else 0:8.3f} s/req  (n={ttft_n:.0f})")
    print(f"avg inter-token latency:   {1000*itl_s/itl_n if itl_n else 0:8.1f} ms/tok "
          f"-> {itl_n/itl_s if itl_s else 0:6.1f} tok/s per-stream decode")
    fr = {k[8:]: d(b, a, k) for k in set(a) | set(b) if k.startswith("success:")}
    if any(fr.values()):
        print("finished_reason:          ", {k: int(v) for k, v in fr.items() if v})


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "snap":
        snap(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "diff":
        diff(sys.argv[2], sys.argv[3])
    else:
        print(__doc__); sys.exit(2)
