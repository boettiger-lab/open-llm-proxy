#!/usr/bin/env python3
"""Per-question timing + accuracy analysis for the OpenRouter open-model benchmark.

Timing/completion come from the per-app summary.tsv files saved under results/
(one row per model x question x trial, dumped by run_matrix.sh). Accuracy comes
from results/grades.tsv (app, q_idx, model, score in {1=correct,0.5=partial,0=wrong},
note) — filled by grading each model's transcript answer against gold/.

Usage:
  python3 analyze.py            # prints per-(model x question) and per-model rollups
"""
import csv, glob, os, statistics as st, collections, json

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")

def load_summaries():
    rows = []
    for f in sorted(glob.glob(os.path.join(RESULTS, "*.summary.tsv"))):
        app = os.path.basename(f).replace(".summary.tsv", "")
        with open(f) as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                r["app"] = app
                rows.append(r)
    return rows

def fnum(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def status(r):
    err = (r.get("error") or "").strip()
    if r.get("timed_out", "").lower() == "true" or "timed out" in err: return "timeout"
    if "502" in err or "500" in err: return "api_5xx"
    if err: return "error"
    return "ok"

def load_grades():
    g = {}
    p = os.path.join(RESULTS, "grades.tsv")
    if not os.path.exists(p): return g
    with open(p) as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            g[(r["app"], r["q_idx"], r["model"])] = (fnum(r["score"]), r.get("note", ""))
    return g

def main():
    rows = load_summaries()
    if not rows:
        print("No results/*.summary.tsv yet. Save each job's summary.tsv there first.")
        return
    grades = load_grades()

    # per (app, q_idx, model): aggregate trials
    cells = collections.defaultdict(list)
    for r in rows:
        cells[(r["app"], r["question"], r["model"])].append(r)

    print(f"{'app':14} {'q':4} {'model':34} {'ok/n':6} {'lat_med':8} {'lat_min':8} {'lat_max':8} {'tools':5} {'acc'}")
    print("-" * 110)
    model_ok = collections.Counter(); model_n = collections.Counter()
    model_lat = collections.defaultdict(list); model_acc = collections.defaultdict(list)
    for (app, q, model), trs in sorted(cells.items()):
        oks = [t for t in trs if status(t) == "ok"]
        lats = [fnum(t["elapsed_s"]) for t in oks if fnum(t["elapsed_s"]) is not None]
        med = f"{st.median(lats):.1f}" if lats else "-"
        lo = f"{min(lats):.1f}" if lats else "-"
        hi = f"{max(lats):.1f}" if lats else "-"
        tools = st.median([fnum(t["tool_calls"]) or 0 for t in oks]) if oks else 0
        sc, note = grades.get((app, q, model), (None, ""))
        acc = "?" if sc is None else f"{sc:g}"
        print(f"{app:14} {q:4} {model:34} {len(oks)}/{len(trs):<4} {med:8} {lo:8} {hi:8} {tools:<5g} {acc}")
        model_ok[model] += len(oks); model_n[model] += len(trs)
        model_lat[model] += lats
        if sc is not None: model_acc[model].append(sc)

    print("\n=== per-model rollup ===")
    print(f"{'model':34} {'completion':12} {'lat_med':8} {'accuracy(graded)'}")
    print("-" * 80)
    for model in sorted(model_n):
        cr = f"{model_ok[model]}/{model_n[model]}"
        med = f"{st.median(model_lat[model]):.1f}s" if model_lat[model] else "-"
        accs = model_acc[model]
        acc = f"{sum(accs)/len(accs)*100:.0f}% (n={len(accs)})" if accs else "ungraded"
        print(f"{model:34} {cr:12} {med:8} {acc}")

if __name__ == "__main__":
    main()
