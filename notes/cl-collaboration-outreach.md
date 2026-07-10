# CS collaboration outreach — continual learning / agentic memory

Draft outreach emails framing our deployed geo-agent stack as a real-world testbed
for open questions in continual learning and agentic AI. Anchored to two UC Berkeley
projects funded in the **Laude Slingshots // THREE** batch (announced 2026-06-26,
https://www.laude.org/updates/slingshots-three). Slingshots fund open-source work
with bespoke support (compute, introductions, infra) — our stack (`boettiger-lab/*`)
is already open, which aligns with the program.

Background and the technical agenda these tie into: open-llm-proxy#42.

Why our example is distinctive as a testbed:
- **Deployed, real users, real stakes** (conservation / land-policy decision-makers),
  not a simulated environment (cf. their WebArena-Infinity agentic benchmark).
- **Gradient-free by necessity** — we serve open models we don't control (Qwen, GLM,
  Gemma, Kimi, …), so SFT/RL are off the table; levers are prompting, context/memory,
  and the agent harness. This is the GEPA/ACE family, in a regime their benchmarks
  don't cover (deployed, multi-tenant, can't-touch-weights).
- **Mechanically computable ground truth** — answers are queries over real data, so
  accuracy is gradeable (rare for agentic settings).
- **Genuine CL axes on real data** — "Time": dataset drift + fact updates; "Space":
  new domains/apps added over time.
- **Full observability** — every request/response logged; a replay harness reproduces
  any session and runs model × question sweeps on the cluster.

---

## Email 1 — Continual-learning group (Harrington / Bai; Darrell / Malik faculty anchors)

Target project: **"Studying Learning in Continual Learning"** (UC Berkeley) —
*"A framework that unifies LLM continual learning methods, showing that the data and
task conditions determine whether continual learning really requires learning."*

> **Subject:** Congrats on the Slingshot — a deployed agentic-AI testbed for continual learning
>
> Hi [Anne / Yutong / name],
>
> Congratulations on the Laude Slingshot for *Studying Learning in Continual Learning* — the framing (whether continual learning "really requires learning," and how data/task conditions decide) lines up closely with a system my group runs, and I think it could be a real-world anchor for some of the questions you raise.
>
> We operate a geospatial AI agent, actively deployed to conservation and land-policy decision-makers, that answers quantitative questions over large spatial datasets — protected areas, carbon, habitat, public-lands investment — through a multi-step tool-use loop. It runs on a suite of open models (Qwen, GLM, Gemma, Kimi, …) behind our own proxy, every request/response is logged, and a replay harness deterministically reproduces any session and runs model × question sweeps on our cluster. The whole stack is open source.
>
> I should be precise about what's being *learned*, because "it writes SQL" undersells it. The bottleneck isn't SQL syntax — it's whether the model correctly *understands the data*: that a value lives on a multi-resolution H3 hex grid, so summing it double-counts; that a "no-data" class is encoded as a `NaN` that will silently poison an aggregate; that a coded value means one thing in this table and another in its sibling; that carbon is stored in megagrams, so an answer phrased in gigatonnes is off by a thousand. These aren't syntax errors the model can see and retry — they're misreadings of *what the numbers are*. The same question over the same data gives a ~25× spread across models precisely because they differ in this understanding, and the failure surfaces as a confident, wrong number in front of a decision-maker. That understanding is real-world domain knowledge; it *grows and drifts* as datasets are added and updated; and since we can't touch weights, the model has to acquire and hold it in context, across a heterogeneous fleet of open models with very different capabilities.
>
> So the optimization surface is large, not a prompt: which knowledge to surface *when*, which tools to expose at which step, how to react when a result looks wrong, what to carry across steps. That puts us in the GEPA/ACE family you study, but in a regime your current benchmarks don't cover: **deployed, multi-tenant, gradient-free, with real users and real stakes**, and — unusually for agentic settings — with mechanically computable ground truth (answers are queries over real data, not eval heuristics). Our "Time axis" is genuine drift and fact updates in the underlying data; our "Space axis" is new domains and apps added over time.
>
> It feels like a live instance of a couple of questions you name — how experience compounds across multi-step tool chains, and how to manage agentic state in a deployed system — grounded in a working application rather than a simulated environment.
>
> Would you or a student be up for a short conversation about whether this could serve as a shared real-world testbed? I'm glad to share logs, the replay harness, and our evaluation setup.
>
> Best,
> Carl

---

## Email 2 — LEANN / memory-and-retrieval group (Gonzalez / Min / Zaharia; Sky Computing)

Target project: **LEANN** (UC Berkeley / Princeton) — *"A low-storage RAG system that
enables fast, accurate, and fully private retrieval directly on personal devices."*
Angle: retrieval as the substrate for agentic procedural memory at inference time.

> **Subject:** Congrats on the LEANN Slingshot — a deployed agent that needs exactly this kind of retrieval
>
> Hi [Joey / Sewon / name],
>
> Congratulations on the Laude Slingshot for LEANN. I run a deployed agentic system that's bumping into the problem LEANN attacks from the other direction, and I wonder if there's a useful overlap.
>
> We operate a geospatial AI agent, actively deployed to conservation and land-policy decision-makers, that answers quantitative questions over large spatial datasets through a multi-step tool-use loop. It runs on a suite of open models behind our own proxy, with full request/response logging and a replay harness for model × question sweeps. The whole stack is open source.
>
> Here's the retrieval problem, and it's a clean fit for LEANN. The agent writes SQL, but the bottleneck isn't text-to-SQL — it's whether the model correctly *understands* messy real-world data: that a value lives on a multi-resolution hex grid so summing double-counts, that a `NaN` is a "no-data" class that poisons aggregates, that a coded value differs between sibling tables, that the units are megagrams not gigatonnes. Getting that understanding wrong produces a confident, wrong number in front of a decision-maker — not an error anyone notices.
>
> All of that data- and domain-specific knowledge lives in two places today, both **coarse**: crammed into the system prompt, and in STAC metadata the agent can pull on demand. But the on-demand path is **all-or-none per dataset** — `get_schema` returns the whole record's columns + coded values, or nothing — and the prompt guidance is injected wholesale into every query. Neither is keyed to what the *current* question actually needs, so we're constantly trading token budget against coverage.
>
> This seems to be crying out for a finer, embedding-centric retrieval model: index the knowledge corpus at the grain of individual facts — per-column descriptions, single coded-value glosses, dataset caveats, past successful query trajectories — and retrieve just the pieces relevant to the question, rather than injecting whole documents or whole STAC records. That's RAG over a growing, self-curated agent memory that drifts as datasets change, under real latency and footprint constraints — squarely your territory, and surely a better mechanism than our current "MCP reads the whole STAC record and feeds it to the prompt."
>
> Because answers are queries over real data, we have mechanically computable ground truth, so we can actually measure whether retrieving the right knowledge at the right time improves outcomes — a cleaner signal than most agent-memory benchmarks offer.
>
> Would you or a student be interested in a short conversation about whether our deployment could be a real-world testbed for retrieval-backed agent memory? Happy to share logs, the harness, and the eval setup.
>
> Best,
> Carl

---

## Tailoring notes

- Greeting: lead authors (Harrington / Bai; Wang) are most direct; Darrell/Malik or
  Gonzalez/Zaharia if you want a PI-to-PI note.
- Bonus adjacency for a wider net: **ORLA** (Harvard, same batch) — *"a runtime adaptive
  execution engine for agentic workflows"* — is nearly a restatement of the harness
  reframe in open-llm-proxy#42; **Rules to Code** (Stanford) — codifying expertise into
  formal logic — rhymes with the attributed rule-store keystone.
- Keep the "computable ground truth" point in any follow-up; it's what makes the
  testbed gradeable, which most deployed-agent settings are not.
