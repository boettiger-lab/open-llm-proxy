#!/usr/bin/env python3
"""Controlled prefill-vs-size curve + isolated decode probe for qwen3-6 (cirrus).

Uses server-side vLLM /metrics deltas (via cirrus_metrics.parse) for the exact
prefill/decode split at each size. Thinking is OFF so prefill (short output) and
decode (long output, tiny prompt) are cleanly isolated. Hits the endpoint DIRECTLY
(nimbus key) -- run only when nothing else is loading cirrus.

Env: NIMBUS_KEY (or /tmp/nimbus_key), MODEL_NAME (default qwen3-6),
     CHAT_URL, METRICS_URL.
"""
import os, sys, json, time, uuid, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import cirrus_metrics as cm  # parse(), URL, MODEL

KEY = os.environ.get("NIMBUS_KEY") or open("/tmp/nimbus_key").read().strip()
MODEL = os.environ.get("MODEL_NAME", "qwen3-6")
CHAT = os.environ.get("CHAT_URL", "https://qwen3-cirrus.carlboettiger.info/v1/chat/completions")

FILLER = ("The quick brown fox jumps over the lazy dog. Sphinx of black quartz, "
          "judge my vow. Pack my box with five dozen liquor jugs. ")

def make_prompt(target_tokens):
    words_per = len(FILLER.split())
    repeats = max(1, (int(target_tokens / 0.75) + words_per - 1) // words_per)
    return f"[nonce {uuid.uuid4()}] " + FILLER * repeats

def snap():
    req = urllib.request.Request(cm.URL, headers={"User-Agent": "curl/8.0 microbench"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return cm.parse(r.read().decode("utf-8", "replace"))

def chat(prompt, max_tokens):
    body = json.dumps({
        "model": MODEL, "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(CHAT, data=body, method="POST", headers={
        "Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
        "User-Agent": "curl/8.0 microbench"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
        return time.monotonic() - t0, d.get("usage", {})
    except Exception as e:                       # 524 CF-timeout on >100s prefill, etc.
        return time.monotonic() - t0, {"error": str(e)}

def delta(b, a, k):
    return b.get(k, 0.0) - a.get(k, 0.0)

def window(fn, label):
    a = snap()
    fn()
    b = snap()
    ptok = delta(b, a, "vllm:prompt_tokens_total")
    gtok = delta(b, a, "vllm:generation_tokens_total")
    pref = delta(b, a, "vllm:request_prefill_time_seconds_sum")
    dec  = delta(b, a, "vllm:request_decode_time_seconds_sum")
    n    = delta(b, a, "vllm:request_prefill_time_seconds_count")
    cached = delta(b, a, "vllm:prompt_tokens_cached_total")
    return {"label": label, "reqs": n, "prompt_tok": ptok, "gen_tok": gtok,
            "prefill_s": pref, "decode_s": dec, "cached_tok": cached,
            "prefill_tok_s": ptok/pref if pref > 0 else 0,
            "decode_tok_s": gtok/dec if dec > 0 else 0}

def main():
    print(f"microbench {MODEL} @ {CHAT}  (thinking OFF)\n")
    # warm-up so first-request cold effects don't skew size-2000
    chat(make_prompt(500), 4)

    print("== PREFILL curve (short output, vary prompt size) ==")
    print(f"{'size':>7} {'reqs':>5} {'prompt_tok':>11} {'prefill_s':>10} {'prefill_tok/s':>14} {'wall_s(avg)':>12}")
    # keep single-shot prompts under the ~100s Cloudflare edge timeout (else HTTP 524)
    sizes = [int(x) for x in os.environ.get("SIZES", "2000,8000,16000").split(",")]
    trials = int(os.environ.get("TRIALS", "3"))
    rows = []
    for size in sizes:
        walls = []
        def fire(size=size, walls=walls):
            for _ in range(trials):
                w, u = chat(make_prompt(size), 8)
                walls.append(w)
        r = window(fire, f"prefill~{size}")
        r["wall_avg"] = sum(walls)/len(walls) if walls else 0
        rows.append(r)
        print(f"{size:>7} {r['reqs']:>5.0f} {r['prompt_tok']:>11.0f} "
              f"{r['prefill_s']:>10.2f} {r['prefill_tok_s']:>14.0f} {r['wall_avg']:>12.2f}")

    print("\n== DECODE probe (tiny prompt, long output x3) ==")
    def fire_dec():
        for _ in range(3):
            chat("Write a long detailed essay about the ocean. Keep going.", 512)
    dr = window(fire_dec, "decode")
    print(f"reqs={dr['reqs']:.0f}  gen_tok={dr['gen_tok']:.0f}  decode_s={dr['decode_s']:.2f}  "
          f"DECODE={dr['decode_tok_s']:.1f} tok/s")

    out = os.path.join(HERE, "out", "microbench.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump({"model": MODEL, "prefill_curve": rows, "decode": dr}, open(out, "w"), indent=2)
    print(f"\n-> {out}")

if __name__ == "__main__":
    main()
