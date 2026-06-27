#!/usr/bin/env python3
"""Join judge grades + session timing/completion, write REPORT.md.
Reads the synced proxy logs + results/grades.raw; produces per-model and
per-question accuracy + timing tables. Re-runnable."""
import duckdb, os, glob, collections, statistics as st

E = os.path.dirname(os.path.abspath(__file__))
con = duckdb.connect()
con.execute(r"""CREATE VIEW orbench AS
WITH parq AS (SELECT ts::TIMESTAMP ts,type,request_id,session_id,origin,model,error,latency_ms,
   json_extract_string(entry,'$.user_question') uq, json_extract_string(entry,'$.content') content_txt
   FROM read_parquet('/tmp/open-llm-proxy-logs/consolidated/**/*.parquet') WHERE origin LIKE '%agent_runner_orbench'),
raw AS (SELECT timestamp::TIMESTAMP ts,type,request_id,session_id,origin,model,error,latency_ms,
   user_question uq, content content_txt
   FROM read_ndjson_auto('/tmp/open-llm-proxy-logs/2026-06-2*/*.jsonl',union_by_name=true) WHERE origin LIKE '%agent_runner_orbench')
SELECT * FROM parq UNION ALL SELECT r.* FROM raw r WHERE r.request_id NOT IN (SELECT request_id FROM parq WHERE request_id IS NOT NULL)""")
rows = con.execute(r"""SELECT CAST(session_id AS VARCHAR) sid, regexp_extract(any_value(origin),'https://([^.]+)',1) app,
  any_value(model) model, max(uq) uq, count(*) FILTER (WHERE type='response') turns,
  round(date_diff('millisecond',min(ts),max(ts))/1000.0,1) wall_s,
  COALESCE(bool_or(error LIKE '%budget limit%'),false) capped, COALESCE(bool_or(error IS NOT NULL AND error<>''),false) anyerr
  FROM orbench GROUP BY session_id""").fetchall()

qmap = {}
for f in glob.glob(os.path.join(E, "questions", "*.txt")):
    app = os.path.basename(f)[:-4]
    qs = [l.strip() for l in open(f) if l.strip()]
    for a in ([app] + (["wetlands-v2"] if app == "wetlands" else [])):
        for i, q in enumerate(qs, 1):
            qmap[(a, q)] = f"q{i}"
def qidx(app, uq):
    if not uq: return "?"
    u = uq.strip()
    if (app, u) in qmap: return qmap[(app, u)]
    for (a, q), v in qmap.items():
        if a == app and q[:40] == u[:40]: return v
    return "?"

SH = {"z-ai/glm-5.2": "glm-5.2", "nvidia/nemotron-3-ultra-550b-a55b": "nemotron-3-ultra",
      "minimax/minimax-m3": "minimax-m3", "moonshotai/kimi-k2.7-code": "kimi-2.7-code"}
grades = {}
for ln in open(os.path.join(E, "results", "grades.raw")):
    p, s = ln.split(); grades[p] = float(s)

sess = []
for sid, app, model, uq, turns, wall, capped, anyerr in rows:
    status = "budget_capped" if capped else ("error" if anyerr else "ok")
    sess.append(dict(app=app, model=SH.get(model, model), q=qidx(app, uq),
                     status=status, wall=wall, turns=turns, score=grades.get(sid[:8])))

models = ["glm-5.2", "minimax-m3", "kimi-2.7-code", "nemotron-3-ultra"]
L = []
L.append("# OpenRouter open-model benchmark - results\n")
L.append("Run 2026-06-26/27. 22 analytical questions x 4 models x 3 trials. "
         "Gold = independent duckdb-geo MCP queries (see gold/).\n")
L.append("> **Budget-cap caveat:** the OpenRouter account hit its monthly spend limit at "
         "00:49-00:55 UTC near the end of the run. 82 of 264 trials returned "
         "'Org member budget limit exceeded' instead of an answer. This reduces trials-per-cell "
         "in the tail but every (model x question) cell still has >=1 graded trial. Accuracy is "
         "over completed trials; complete% captures the cap.\n")
L.append("## Per-model summary\n")
L.append("| model | attempts | completed | budget-capped | complete% | accuracy | median wall | median turns |")
L.append("|---|--:|--:|--:|--:|--:|--:|--:|")
bym = collections.defaultdict(list)
for r in sess: bym[r["model"]].append(r)
for m in models:
    rs = bym[m]; ok = [r for r in rs if r["status"] == "ok"]; cap = [r for r in rs if r["status"] == "budget_capped"]
    sc = [r["score"] for r in ok if r["score"] is not None]
    acc = sum(sc) / len(sc) * 100 if sc else 0
    L.append(f"| {m} | {len(rs)} | {len(ok)} | {len(cap)} | {len(ok)/len(rs)*100:.0f}% | "
             f"**{acc:.0f}%** | {st.median([r['wall'] for r in ok]):.0f}s | {st.median([r['turns'] for r in ok]):.0f} |")
L.append("\nAccuracy = mean judge score (1 correct / 0.5 partial / 0 wrong) over completed trials.\n")

acc = collections.defaultdict(lambda: collections.defaultdict(list))
lat = collections.defaultdict(lambda: collections.defaultdict(list))
for r in sess:
    if r["status"] == "ok":
        lat[(r["app"], r["q"])][r["model"]].append(r["wall"])
        if r["score"] is not None: acc[(r["app"], r["q"])][r["model"]].append(r["score"])
L.append("## Per-question accuracy (mean score across completed trials)\n")
L.append("| app | q | glm-5.2 | minimax-m3 | kimi-2.7-code | nemotron-3-ultra |")
L.append("|---|---|--:|--:|--:|--:|")
for k in sorted(acc):
    line = f"| {k[0]} | {k[1]} |"
    for m in models:
        v = acc[k].get(m); line += f" {('%.2f' % (sum(v)/len(v)) if v else '-')} |"
    L.append(line)
L.append("\n## Per-question median wall-clock seconds (completed trials)\n")
L.append("| app | q | glm-5.2 | minimax-m3 | kimi-2.7-code | nemotron-3-ultra |")
L.append("|---|---|--:|--:|--:|--:|")
for k in sorted(lat):
    line = f"| {k[0]} | {k[1]} |"
    for m in models:
        v = lat[k].get(m); line += f" {('%.0f' % st.median(v) if v else '-')} |"
    L.append(line)
open(os.path.join(E, "results", "REPORT.md"), "w").write("\n".join(L) + "\n")
print("wrote results/REPORT.md\n")
print("\n".join(L))
