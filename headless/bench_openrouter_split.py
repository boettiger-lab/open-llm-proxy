#!/usr/bin/env python3
"""Per-call prefill/decode split from OpenRouter generation stats (geo-agent#282).

Our vLLM Prometheus metrics (`prom_prefill_decode.py`) give the fleet split, but
only in aggregate and only for models on our own serving stack. This script gets
a **clean per-call** split for any OpenRouter-hosted model, and — unlike vLLM
Prometheus — breaks out **reasoning tokens**, the largest decode component:

  latency                  -> time to first token   (prefill + queue)
  generation_time          -> decode wall time
  native_tokens_reasoning  -> hidden reasoning tokens (the #283 lever)
  native_tokens_cached     -> prompt-cache hit

It sends the real geo-agent system prompt + a few real analytical questions, then
reads OpenRouter's `/api/v1/generation?id=` for each. Also runs one question with
reasoning ON vs OFF to size the #283 lever (note: the generic
`reasoning:{enabled:false}` flag is provider-dependent and may not disable on all
models — verify per model).

COST: this hits OpenRouter (paid). Keep the question list short; each call is
~$0.001-0.01 on glm-5.2. Skips nothing silently.

Env:
  OPENROUTER_KEY   required. On-cluster: mounted from the `openrouter-key` secret.
  SYS_PROMPT       optional path to a system prompt (default: ../../geo-agent/app/system-prompt.md).
  MAX_TOKENS       optional completion cap (default 4000; raise to avoid truncating reasoning).

Usage:  OPENROUTER_KEY=... python3 bench_openrouter_split.py [model]   # default z-ai/glm-5.2
"""
import os, sys, json, time, urllib.request, urllib.error

KEY = os.environ.get("OPENROUTER_KEY")
if not KEY:
    print("OPENROUTER_KEY required (on-cluster: from the `openrouter-key` secret)", file=sys.stderr)
    sys.exit(2)
MODEL = sys.argv[1] if len(sys.argv) > 1 else "z-ai/glm-5.2"
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4000"))
BASE = "https://openrouter.ai/api/v1"

_here = os.path.dirname(os.path.abspath(__file__))
_sys_path = os.environ.get("SYS_PROMPT", os.path.join(_here, "..", "..", "geo-agent", "app", "system-prompt.md"))
try:
    SYS = open(_sys_path).read()
except OSError:
    SYS = "You are a geospatial data analyst agent."  # fallback so the script still runs

QUESTIONS = [
    "How many acres of California land are conserved at GAP status 1 or 2?",
    "Which ecoregion has the most conserved acreage?",
    "Rank the top hydrobasins by species richness, normalized by basin area.",
]

def post(messages, reasoning=None):
    body = {"model": MODEL, "messages": messages, "temperature": 0,
            "max_tokens": MAX_TOKENS, "usage": {"include": True}}
    if reasoning is not None:
        body["reasoning"] = reasoning
    req = urllib.request.Request(f"{BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    return d, (time.time() - t0) * 1000

def gen_stats(gid):
    time.sleep(1.5)                    # let OpenRouter finalize the record
    for _ in range(12):
        try:
            req = urllib.request.Request(f"{BASE}/generation?id={gid}",
                headers={"Authorization": f"Bearer {KEY}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r).get("data", {})
        except urllib.error.HTTPError as e:
            if e.code in (404, 429):   # not populated yet / rate-limited
                time.sleep(3); continue
            raise
    return {}

def run(label, messages, reasoning=None):
    d, wall = post(messages, reasoning)
    s = gen_stats(d["id"]) if d.get("id") else {}
    lat = s.get("latency"); gt = s.get("generation_time")
    pt = s.get("native_tokens_prompt") or s.get("tokens_prompt")
    ct = s.get("native_tokens_completion") or s.get("tokens_completion")
    rt = s.get("native_tokens_reasoning")
    return {
        "label": label, "wall_ms": round(wall), "prefill_ms": lat, "decode_ms": gt,
        "decode_%time": round(100*gt/(lat+gt)) if (lat and gt) else None,
        "prompt_tok": pt, "completion_tok": ct, "reasoning_tok": rt,
        "reasoning_%out": round(100*rt/ct) if (rt and ct) else None,
        "cached_tok": s.get("native_tokens_cached"),
        "decode_tok/s": round(ct/(gt/1000), 1) if (ct and gt) else None,
        "prefill_tok/s": round(pt/(lat/1000)) if (pt and lat) else None,
        "cost": s.get("total_cost"),
    }

def main():
    print(f"model: {MODEL}   max_tokens: {MAX_TOKENS}\n")
    rows = [run(f"q{i+1}", [{"role":"system","content":SYS},{"role":"user","content":q}])
            for i, q in enumerate(QUESTIONS)]
    hard = [{"role":"system","content":SYS},{"role":"user","content":QUESTIONS[-1]}]
    rows.append(run("q3-reasoning-off", hard, reasoning={"enabled": False}))

    cols = ["label","prefill_ms","decode_ms","decode_%time","prompt_tok","completion_tok",
            "reasoning_tok","reasoning_%out","cached_tok","decode_tok/s","prefill_tok/s","cost"]
    w = {c: max(len(c), 10) for c in cols}
    print("  ".join(c.rjust(w[c]) for c in cols))
    print("  ".join("-"*w[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).rjust(w[c]) for c in cols))

if __name__ == "__main__":
    main()
