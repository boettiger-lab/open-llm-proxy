# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [Releases](README.md#releases) for how a release is cut.

## [Unreleased]

### Added
- **Per-session rollup tier — cross-app/cross-model stats as one `GROUP BY` (#51).**
  New `session-rollup/{daily,monthly}/` Parquet tier, one row per session, built by the
  same CronJobs that build the turn view and mirrored to rustfs alongside it. Carries
  `turns`, `tool_calls_total`, token sums, `cost_usd`, `llm_ms_total`, `wall_clock_s`,
  `status` (`ok`/`timeout`/`error`/`budget_capped`/`incomplete`), `final_answer`, `app`/`run_tag`
  parsed from the origin. Answers "across apps and models, how many LLM calls did each
  question take, how long, how much did it cost, did it complete?" directly — the
  aggregation every benchmark previously hand-rolled in its own `build_report.py`. No new
  capture: it is a `GROUP BY session_key` over `sessions/**`, so it derives entirely from
  what is already logged, and both tiers backfill existing days.
  Four things the raw aggregate would have got quietly wrong, handled explicitly:
  **(0)** a final turn with no response row has a NULL error and so reads as success,
  when it is the opposite — request logged, nothing ever returned; `status = 'incomplete'`
  is tested before `ok` and catches 259 sessions, ~5% of the previously-`ok` set, 95% of
  them real rather than synthetic-key artifacts, with `unanswered_turns` exposing the
  same failure mid-session;
  **(1)** sessions with no `session_id` share an `anon:<hash>` key that merges every
  repeat of a question — one such pseudo-session spanned 15 days — so `session_key_synthetic`
  flags them for filtering rather than letting them skew averages; **(2)** only OpenRouter
  reports a per-call cost, so `cost_usd` is `NULL` rather than `0` when nothing reported one,
  and `cost_turns` gives the coverage; **(3)** the monthly tier rebuilds from the month-wide
  turn view instead of concatenating daily rollups, which would double-count the sessions
  that cross UTC midnight (2,037 → 2,014 rows on 2026-08, turn and token totals unchanged).
  Integer aggregates are pinned to `BIGINT` — `SUM()` widens to HUGEINT, which Parquet can
  only store as `DOUBLE` — so the tier keeps one stable schema. The builder fills any column
  a legacy turn view predates (e.g. `user_message_this_turn`, #89) with `NULL` instead of
  failing the backfill on a binder error.
- **Hourly raw-JSONL mirror — log reading no longer needs the NRP key at all (#124).**
  New `logs-mirror-raw-hourly` CronJob copies *today's* `YYYY-MM-DD/` prefix to the
  rustfs mirror every hour, cutting the scoped-credential lag from **3–27h to ≤1h**
  (measured 7 min on the first run). Consolidation *structurally* excludes the current
  day (`d != today`), so an event only reached the mirror at 03:00 the next day — ~3h
  for something logged at 23:59, ~27h at 00:00, ~15h on average — and everything inside
  that window still required the one NRP credential, which carries read/write/delete on
  every NRP bucket. Running consolidation more often would **not** have helped: the
  current day is excluded by construction, not by cadence. Kept a separate CronJob
  rather than a step in the daily job, since it runs 24× more often and a failure here
  must not wedge consolidation or the rollup mirror. It owns the raw tier in the mirror
  end to end, pruning a day's raw once both its rollups are confirmed mirrored, so the
  mirror does not grow ~21 MiB / ~1,500 objects a day forever.
- **Thinking toggle for `qwen3-small` and `qwen3-4bit` (#105 follow-up).** Both honor
  `chat_template_kwargs: {"enable_thinking": false}` at the endpoint, but had no
  `thinking_models` entry — so a client's `enable_thinking` flag was silently dropped
  (`thinking_models.get(model)` missed and the proxy only logged an info line). Probed
  every NRP model that lacked an entry; results now pinned in a test:
  `qwen3-small`/`qwen3-4bit` honor `enable_thinking` (entries added); `gemma-small`,
  `gemma4-small` and `gemma4-12b` emit no `reasoning` at any setting, so there is
  nothing to toggle; `gpt-oss` and `minimax-m2` reason unconditionally and ignore both
  boolean dialects — `gpt-oss` takes an OpenAI-style `reasoning_effort` *level*, which
  the boolean `thinking_models` map cannot express. Verified end-to-end through the
  deployed proxy that `deepseek-v4-flash` now toggles as intended.
- **Declare every model NRP actually serves, including `deepseek-v4-flash` (#105).**
  `nrp.models` listed 7 ids while `ellm.nrp-nautilus.io/v1/models` serves 14. The
  undeclared ones still reached NRP, but only incidentally — `gemma4-small`,
  `gemma4-12b`, `gemma-small`, `qwen3-small`, `qwen3-4bit` and `qwen3-embedding` rode
  the broad `gemma`/`qwen3` prefixes, and `deepseek-v4-flash` matched nothing at all,
  landing on the `⚠️ Unknown model` fallback (log noise on every request, and one
  prefix edit away from silently rerouting: OpenRouter already carries `deepseek/`).
  All 14 are now declared explicitly, so the list describes what the provider serves
  and each routes by intent rather than by luck. Routing is unchanged for every model
  — the new entries are exact matches for ids that already resolved to NRP.
  `deepseek-v4-flash` also gains a `thinking_models` entry using the **`thinking`**
  dialect (as `kimi` does, not `qwen3`'s `enable_thinking`, which it ignores), so its
  reasoning can actually be toggled. Prefix matching remains provider-declaration-ordered;
  the `gemma4-nimbus` / `qwen3-cirrus` shadowing noted in #105 is left as-is.
- **Second deployment on the self-hosted k3s cluster (cirrus):
  `https://llm-proxy.carlboettiger.info`.** Manifests in `cirrus/` (namespace
  `llm-proxy`), documented in [cirrus/README.md](cirrus/README.md).
  **Config-only and NRP-inert:** it runs this repo's unmodified `llm_proxy.py`
  from `main`, and adds no file the NRP deployment reads — no application code,
  no `config.json` change, no CronJob change. Everything cirrus-specific is
  deployment config:
  - **Providers: OpenRouter + DSE-nimbus (`qwen`) only**, from a ConfigMap
    mounted over `/app/config.json` in the pod. No NRP ELLM, no Anthropic direct.
  - **Logs to the in-cluster MinIO mirror** (`minio-svc.minio.svc.cluster.local:9000`,
    bucket `logs-open-llm-proxy`), via the existing `LOG_BUCKET` /
    `AWS_S3_ENDPOINT_URL` env vars and a MinIO service account scoped to that
    bucket — not the MinIO root user. **Raw JSONL tier only:** the Parquet rollup
    and `sessions/**` view live inside the NRP CronJob manifests and can't be
    reused without extracting them, which would touch NRP. Deferred.
  - **Traefik replaces HAProxy** for ingress: CORS and the 600s backend timeouts
    become `Middleware` / `ServersTransport` CRDs (`cirrus/middleware.yaml`), with
    cert-manager + external-dns provisioning TLS and the A record.
  - No dns-cache sidecar / `dnsPolicy: None` (that works around NRP CoreDNS
    flakiness, #28); pinned to the `cirrus` node, where MinIO and Traefik live.
  - Known limitation, inherent to staying config-only: an unrouted model id
    returns `500` rather than a useful `400`, because
    `get_provider_for_model`'s fallback is a hard-coded `PROVIDERS["nrp"]` and
    cirrus has no `nrp` provider. Correct model ids are unaffected.

- **Route OpenRouter floating aliases (`~…`).** OpenRouter publishes always-latest
  aliases whose model id carries a literal leading tilde — e.g.
  `~deepseek/deepseek-v4-flash-latest` (canonical slug identical), as distinct from
  the pinned `deepseek/deepseek-v4-flash-0731`. Because `get_provider_for_model`
  matches by `str.startswith`, such an id matched *no* vendor prefix and silently
  fell back to NRP, which does not serve it. Added `~` to the OpenRouter prefix
  allowlist (`config.json` plus the in-code default), so any floating alias routes
  to OpenRouter. No other provider's model ids begin with `~`, so the broad prefix
  is unambiguous.

- **Log `user_message_this_turn` — the actual per-turn prompt (#89).** `session_id`
  persists across a whole browsing day, and `user_question` only ever held the
  *first* user message of the session, repeated verbatim on every subsequent turn —
  so distinct mid-session requests were uncountable and the follow-up prompt that
  triggered a given turn was unrecoverable from the logs (you had to reverse-engineer
  intent from tool calls). `log_request` now also captures the **last** user message
  as `user_message_this_turn`, alongside the unchanged `user_question` opener. The
  field is flattened to a typed column in the daily/monthly consolidated Parquet and
  surfaced per-turn in the session view, so "read the real sequence of what the user
  asked" is `SELECT turn_idx, user_message_this_turn … ORDER BY turn_idx`. Captured in
  the default `summary` mode (no need for `LOG_CAPTURE_MODE=full`). Back-compat: the
  reflatten schema-upgrade pass re-flattens existing consolidated files to add the
  column (`null` for pre-#89 records, whose raw `entry` never held it), keeping
  `consolidated/**` on one schema; the guard sentinel moved from `model` to
  `user_message_this_turn` so the upgrade re-triggers.
- **Print a `##BENCH-VERSIONS##` line from the headless matrix Job.** After the Job
  clones open-llm-proxy + geo-agent + the app repo, it now emits a single grep-able
  line with the exact geo-agent / app / proxy git SHAs (from the shallow clones), the
  app repo, both branches, and the app config's `mcp_url`. Lets downstream benchmark
  tooling (`boettiger-lab/geo-agent-benchmark`, olp#91) capture a reproducible
  `versions` block per run instead of guessing which stack a score was measured under.
  Purely additive logging — does not affect the matrix run.
- **Route the `deepseek/` OpenRouter family.** Added `deepseek/` to the OpenRouter
  provider's prefix allowlist so `deepseek/deepseek-v4-flash` (and other
  `deepseek/…` models) route instead of silently falling back to NRP. Enables the
  fleet-wide "DeepSeek V4 Flash (OpenRouter)" picker option.

### Added
- **`GET /v1/models` — the proxy answers what it can route (#111).** Model ids churn and
  every copy of the list goes stale independently: `config.json` (now emptied, #110), each
  app's picker, the benchmark harness. Providers already enumerate themselves, so the
  proxy now asks them and reports the union, OpenAI-shaped, annotated by provider.
  The discovered set is **filtered through `get_provider_for_model` itself**, so the
  listing cannot drift from routing — an id is advertised under a provider only if a real
  request would go there. That also narrows a provider's much larger catalog to what this
  deployment actually serves (OpenRouter returns 414 ids; 264 are routable here).
  Unreachable providers degrade rather than vanish, keeping last-known-good ids or falling
  back to declared config, with `status` reporting `ok`/`stale`/`declared` — verified live
  against `gemma4-nimbus` and `qwen3-cirrus`, both 503 at the time. Anthropic needs
  `x-api-key` plus a version header (its OpenAI-compat path answers `Invalid bearer
  token`), so per-provider `models_auth` / `models_endpoint` overrides are supported.
  Cached for `MODELS_CACHE_TTL` (default 300s), `?refresh=true` to force. Requires the
  proxy key, like the chat endpoint. Live result: nrp 14, openrouter 264, anthropic 10
  (including `claude-opus-5`/`claude-sonnet-5` — the very ids the old pinned config had
  gone stale on), nimbus 1, two degraded — 291 routable total.
  This restores the typo detection #110 gave up, lets apps stop hardcoding lists, and
  makes deployment differences visible, which is what the cirrus failover story needs.

### Added
- **Guardrails against a silent one-sided logging outage (#39, follow-up to #37).** In #37
  a variable-shadowing bug made `log_response` throw inside `@_never_raises`, so every
  response was dropped from S3 for hours while requests logged normally — no request
  failed, nothing alerted, and the corpus just quietly went lopsided. Two signals now
  surface that class of failure. **(1) Request:response balance:** each flush window the
  proxy compares how many request and response entries actually reached the buffer, and
  prints a `🚨 Logging imbalance` line when the ratio falls below `LOG_RATIO_FLOOR`
  (default 0.5) with at least `LOG_RATIO_MIN_REQUESTS` (default 20) requests — the volume
  floor stops a single turn straddling a window boundary from tripping it. Counting is
  done in `_emit`, where an entry actually lands in the buffer, *not* on entry to the log
  functions: a `log_response` that throws must go uncounted, or the check would mask the
  very failure it exists to catch. **(2) Swallowed logging exceptions** are now counted
  per function and escalate at 10/100/1000 occurrences, so a systematic fault can't hide
  among one-off serialization edge cases. Both are exposed under a new `logging` key on
  `/health` (last window, imbalance count, swallowed totals, buffer depth).
  `/health`'s `status` is deliberately **unchanged** by all of this — it backs the
  liveness, readiness *and* startup probes, and a logging fault must never restart or
  de-rotate a pod that is serving fine. `log_request`/`log_response` also now share a
  single `_log()` print+buffer path so the two sides can't drift (tidiness; it would not
  have prevented #37, which was a call-site bug). Verified by reproducing #37's shape
  through `proxy_chat` with a mocked upstream — 25 served requests, zero logged responses,
  alarm fires — and by mutation-testing the guard: disabling the alarm fails the suite.

### Changed
- **Routing declares exact ids and prefixes separately; NRP's model list is gone (#110).**
  `models` entries used to do double duty — every bare id also acted as a prefix. That was
  subtle and actively dangerous: nimbus declaring the exact id `qwen` silently claimed
  `qwen3`, `qwen3-small`, `qwen3-4bit` and `qwen3-embedding` as well, and the *only* thing
  preventing it was NRP listing each of those explicitly so the exact pass matched first.
  The "redundant" NRP entries were load-bearing as accidental shields, and deleting them —
  the obvious simplification — would have moved four production models to a different
  backend serving different weights under the requested id, with no error anywhere.
  Providers now declare `models` (exact ids) and `model_prefixes` (families) separately;
  exact still beats prefix. With that split safe, `nrp.models` drops from 14 entries to
  **none** (every id reaches NRP via `default_provider`, so a new NRP model needs no PR),
  OpenRouter's 11 vendor prefixes move to `model_prefixes` where they belong, and
  Anthropic's three pinned ids are deleted in favor of the `claude-` prefix already beside
  them — which would have routed `claude-sonnet-5` on release day instead of going stale.
  Config churn was the point: most of this file's history is model-list edits.
  A provider that omits `model_prefixes` keeps the old double-duty behavior, so cirrus's
  ConfigMap and any third-party config are unaffected (verified). The per-request
  `⚠️  Unknown model` line is gone — with `nrp.models` empty it would fire on essentially
  all production traffic. That does give up local typo detection; `GET /v1/models` (#111)
  is the intended fix, not a log line. Verified by diffing resolved providers for 54 ids
  against `origin/main`: exactly one changed, `qwenx` (a hypothetical unlisted id) from
  nimbus to nrp — which is the hazard being removed, not a regression.

### Changed
- **Log reads no longer need a privileged credential (#113).** `./sync-logs.sh` defaulted
  to rclone's `nrp:` remote — the single NRP credential, carrying read/write/delete on
  *every* NRP bucket — for a read-only task against one bucket of a few MiB. It now reads
  the rustfs mirror with a Get/List-only, single-bucket key, resolving it from
  `$LOGS_READ_KEY`/`$LOGS_READ_SECRET`, else from the `rustfs-logs-read` Secret via
  `kubectl` (so the value never lands in a dotfile or shell history), else falling back to
  `nrp:` with a warning naming what that key can do. The direct-S3 snippet in LOGGING.md,
  the AGENTS.md pointer, and the `geo-agent-training` skill all switch to the scoped key
  too. `LOG_S3_KEY`/`LOG_S3_SECRET` are now needed only for reaching the NRP source
  directly — chiefly *today's* raw JSONL, which the mirror deliberately does not carry
  (documented, with `kubectl` as the sub-minute path). Verified end to end against the
  live mirror: 36 objects / 63 MB synced with the read-only key and 38,632 session-view
  turns queryable from the local copy; the same key is refused on write.

### Fixed
- **Orphaned raw JSONL is now swept (#124).** Raw chunks were deleted only inline by the
  consolidate path, once both rollups verified. Any day that reached its rollups another
  way kept its raw JSONL *forever*: it lands in `existing`, so it can never re-enter
  `to_do`, and nothing revisited it. The session-view backfill loop is exactly such a
  path, so the 2026-08 OOM incident stranded 2026-08-07/08/12 — **2,944 objects, 41.8 MiB,
  65% of every raw object in the bucket**, for days whose rollups had been complete for a
  week. Fixed as a sweep over the raw tier rather than a delete bolted onto the backfill
  loop: a backfill-local fix would stop new orphans but never clear existing ones, since a
  day drops out of `backfill_sessions` as soon as it has a session view — the stranded days
  would have stayed stranded. Sweeping actual state is self-healing and indifferent to how
  a day was orphaned. Same verify-before-destroy rule as the rest of the pipeline (both
  rollups confirmed present; today never touched). Verified live: swept all three days,
  820 + 1341 + 783 chunks.
- **Daily consolidation had been failing for 9 days (OOM), wedged on one day.** Last
  success 2026-08-07; every run since died with
  `OutOfMemoryException: failed to allocate 256.0 KiB (819.0 MiB/819.1 MiB used)` inside
  `build_session_view`, backfilling `2026-08-07`. Self-perpetuating: the day failed, kept
  its place on the backfill list, and re-broke the job nightly. `threads=2` and
  `preserve_insertion_order=false` were already set, so the easy mitigations were spent.
  These are CronJobs, not persistent pods, so the 2Gi ceiling for long-lived workloads
  doesn't apply: daily 1Gi → 4Gi (DuckDB capped at 3GB), monthly 2Gi → 6Gi (capped at
  4GB), both with `temp_directory` set so DuckDB can spill and an `ephemeral-storage`
  request to back it. Headroom is the primary fix — on a session-view-shaped build,
  spilling turned failure into success at 500MB–1GB but not at 200MB, since DuckDB does
  not spill every operator. The monthly job is raised too although it has not failed yet:
  it runs the same code over a whole month, so it is strictly more exposed, and its next
  run (2026-09-02) rolls up an August containing the heavy benchmark sweeps.
  The backfill loop now isolates each day — one oversized day is recorded and skipped so
  the reflatten pass and the rustfs mirror still run — and the job raises at the end with
  the failed days, so it stays loud instead of silently tolerating the gap.
  Missing session views for `2026-08-07`, `08` and `12` should rebuild on the next run.

### Added
- **Mirror `consolidated/**` and `sessions/**` to rustfs from the consolidation CronJobs
  (#116).** Log analysis has required the single NRP credential, which carries
  read/write/delete on *every* NRP bucket, for a read-only task against one bucket of a
  few MiB (#113). `geo-agent-ops` has minted a scoped pair — `logs-open-llm-proxy-reader`
  (Get/List only) and `…-writer` (plus object Put/Delete, no bucket create/delete) — but
  the rustfs bucket was empty, so the reader was useless. Both CronJobs now copy the
  query-ready tiers there after the tiers are written and verified. Credentials come from
  the `rustfs-logs-write` Secret under **`RUSTFS_*`** names, deliberately not `AWS_*`:
  those are already bound to the `aws` secret for the NRP source, and reusing them would
  clobber the source credential and break the job before it mirrored anything. All four
  bindings are `optional: true`, so a cluster without the Secret still consolidates and
  reports the mirror skipped. Copy-only, never delete — an accidental source deletion
  must not propagate; re-copies when the source `LastModified` or size changes, which is
  what catches the in-place rewrites the reflatten pass performs (size alone is not a
  witness). Runs last so a mirror failure cannot cost the consolidation work, but it does
  fail the Job, because a mirror that quietly stops is a stale mirror nobody notices.
  NRP Ceph stays the system of record: rustfs shares the same rook Ceph, so this is a
  convenience copy, not a second failure domain. Consumer-side retarget of `sync-logs.sh`
  stays in #113 and deliberately does **not** land until the mirror is confirmed non-empty.

### Fixed
- **`geo-agent-training` skill: log collection was broken and over-privileged.** Its
  Step 1 selector was `app=llm-proxy`, which matches **no pods** — the label is
  `app=open-llm-proxy` — so an agent following the skill collected zero proxy logs and
  could conclude an app had no traffic. It then described a log schema three field-
  generations stale (`user_message`, since replaced by `user_question` +
  `user_message_this_turn`, #89) and listed "no `request_id`" and "responses lack
  `origin`" as known limitations, both fixed long ago (#1, #2, closed). For history it
  reached for `rclone copy nrp:logs-wetlands/` — the wrong bucket, via the broad NRP
  credential (#113). Rewritten around `./sync-logs.sh` and the `sessions/**` view, with
  the correct label, the current field list, and the credential warning. Skill 2.0 → 2.1.
- **LOGGING.md wrongly described the log-read keys as scoped (#113).** The direct-S3
  section said `LOG_S3_KEY`/`LOG_S3_SECRET` were "scoped keys for this bucket — distinct
  from your general NRP credentials." There is exactly **one** credential for NRP bucket
  access, so they are that credential: read/write/delete across every NRP bucket. The
  claim was wrong in the dangerous direction — it made an over-broad key look already
  contained, so handing it to an agent for a read-only log query looked safe. Both
  LOGGING.md and AGENTS.md now state the real scope and steer to `./sync-logs.sh`, whose
  local copy needs no secret at query time. Read-only single-bucket access is tracked in
  #113, with the rustfs mirror/mint half in `geo-agent-ops#117`.
- **Configurable `default_provider`; unroutable models get a 400, not a 500.** The
  fallback for a model id matching no provider entry was a hard-coded
  `return "nrp", PROVIDERS["nrp"]`. On NRP that is load-bearing — most of `nrp.models`
  is redundant with it, and undeclared-but-live ids have always arrived this way. On a
  deployment that doesn't serve NRP it was a latent `KeyError` *inside the request
  handler*: an opaque 500 with no body, which is what the cirrus deployment returns
  today for every bare NRP model id. The fallback is now the optional top-level
  `default_provider` key (absent → `"nrp"`, so NRP behavior is untouched), and a
  deployment with no usable default raises a typed `UnknownModelError` that the handler
  renders as a **400 listing what this deployment actually serves**, logged against a
  synthetic `unrouted` provider so the miss is visible. A `default_provider` naming an
  unconfigured provider degrades to that same 400 path with a startup note rather than
  failing at request time — which is also how a cirrus-style config (no `nrp` provider,
  no `default_provider`) now behaves, so it needs no config change to stop 500ing.
  Verified no behavior change on NRP by diffing resolved providers for all 37 ids in
  `config.json` plus unknown/empty/floating-alias cases against `origin/main`: identical.
- **Fail a matrix Job that can't establish its own provenance, and emit the versions
  line twice (#103).** A 7-Job full-tier sweep on 2026-08-01 emitted **zero**
  `##BENCH-VERSIONS##` lines, so 130 graded cells were published to the benchmark store
  with `{"geo_agent": null, "proxy": null, "app_config_sha": null}` — permanently
  unreproducible, because the SHAs can only be read from the shallow clones the Job
  made and those are long gone. Nothing anywhere raised. `matrix-job.yaml` now builds
  the line into `$BENCH_VERSIONS`, checks that each of `geo_agent`/`app_sha`/`proxy_sha`
  resolved to something other than empty-or-`unknown`, and on failure prints a
  `##BENCH-VERSIONS-MISSING##` marker naming the unresolved fields and **exits 1** —
  before `npm ci`, so an unreproducible benchmark costs nothing instead of a full
  matrix. This is the last point in the Job that can still see the clones. The line is
  also echoed a second time in the trailer beside `summary.tsv` (and before `exit $rc`,
  so a failed matrix still carries it), leaving provenance recoverable from a truncated
  or rotated log. An empty `mcp_url` warns but does not fail.

  The line now also records **which MCP build the run actually hit**, closing the last
  provenance gap: `mcp_url` is stable across MCP upgrades, so two runs with identical
  versions blocks could have queried different servers. The Job GETs `<mcp_root>/version`
  — public and auth-exempt on mcp-data-server (mcp-data-server#221) — and adds
  `mcp_server` (e.g. `mcp-data-server v0.8.15`) and `mcp_git_sha` (the running image's
  full git SHA). This resolves for any head (NRP, dev, cirrus, a mirror) with no cluster
  access or RBAC, and reports what actually *serves*, unlike a pod `imageID`, which reads
  stale on disrupted-node orphans (mcp-data-server#383). It replaces the hand-passed
  `--mcp-server 'mcp-data-server v0.8.10'`, which was exactly the kind of thing that goes
  stale. Deliberately non-fatal — it is a network call to a third party, so a transient
  failure records the explicit string `unknown` and warns rather than burning a 130-cell
  matrix. `mcp_server` fills a key `collect_run.py` already reads (falling back to it when
  `--mcp-server` is absent), and every pre-existing key is byte-identical, so nothing
  downstream needs changing; verified against that parser, including the twice-emitted
  line, which it dedupes idempotently. Root cause of the 08-01 silence itself is still
  open — this makes a recurrence loud rather than silent.
- **LOGGING.md: `get_schema` *does* reach the MCP server (#63).** The session-
  reconstruction note claimed `list_datasets` and `get_schema` are both local geo-agent
  tools whose calls "never reach the MCP server." True of `list_datasets`, wrong of
  `get_schema`: its `execute()` delegates to the MCP `get_stac_details` tool, forwarding
  the cached STAC collection inline (`geo-agent/app/map-tools.js`). Because the proxy
  logs the name the *LLM* called and the delegated request never passes through here,
  anyone counting MCP tool load from these logs undercounts `get_stac_details` badly —
  in the June 2026 corpus, 339 direct calls versus ~3,255 actual, the #2 MCP tool. The
  note now distinguishes local-only from local-delegate and says to attribute every
  `get_schema` call to `get_stac_details` when estimating server load.
- **Log client-disconnect / cancelled requests so the caller-facing nginx 502s stop
  being invisible here.** Every geo-agent app fronts this proxy with an nginx sidecar
  whose `proxy_read_timeout` is 300s (`geo-agent-template` configmap). Because the proxy
  calls upstream **non-streaming**, zero bytes flow until the whole completion is
  buffered, so any turn slower than 300s makes nginx return a `nginx/1.29.6` **502** to
  the browser — while the proxy itself either (a) eventually succeeds and logs a slow
  **200**, (b) times out at 600s and logs a **504**, or (c) is cancelled by uvicorn when
  nginx drops the connection. Case (c) raised `asyncio.CancelledError`, which is a
  `BaseException` and so slipped past the handler's `except Exception` — the request
  left only a pre-flight request row and no response/error row, which is why the 502s
  were unobservable from the proxy logs. The chat handler now catches
  `asyncio.CancelledError`, logs a response row with
  `error="Client disconnected/cancelled after …ms (upstream still pending)"`, and
  re-raises; and the success path prints a greppable `⚠️  Slow completion` marker when
  `latency_ms > 300000` (case (a) — a logged 200 the user actually saw as a 502).
  Visibility only — this does not change the request path or claim a root cause; it
  makes the currently-invisible cut requests observable so the mechanism (why the
  caller sees a 502, not a 504) can be pinned down from real data. See #82.
- **Strip leaked `<arg_key>`/`<arg_value>` (GLM) and `<parameter=…>` (qwen) tool-call
  arg dialect from responses (#85).** Some open-weight backends (`z-ai/glm-5.2`, the
  qwen family) intermittently fail to decode their own tool-call argument encoding,
  leaving raw markup inside the structured `arguments` a well-formed native
  `tool_calls` entry returns — e.g. a `value_stats` value arriving as
  `<arg_key>value_stats</arg_key> <arg_value>{…}</arg_value>` instead of the parsed
  object. The proxy now normalizes both the value-level leak (dialect inside one value
  of an otherwise-valid JSON object) and the whole-string leak (the entire `arguments`
  is raw dialect) in `_normalize_response_tool_calls`, applied to each successful
  response *before* it is returned or logged, so no downstream consumer (client or log)
  ever sees the markup. Fully defensive — any parse failure leaves the value untouched.
  The repair count is recorded as `tool_call_dialect_repaired` on the response log so
  the leak rate stays measurable. Durable server-side fix for the leak class that
  geo-agent#276 was defending against client-side.

### Added
- **Multi-key client auth — accept more than one `PROXY_KEY` so eval/dev keys are
  independently revocable.** New `PROXY_KEYS_EXTRA` env (comma-separated, wired from
  `open-llm-proxy-secrets`/`proxy-keys-extra`, `optional: true`) is folded into the
  accepted-key set alongside the primary `PROXY_KEY`; the auth check is now a set
  membership test. Backward compatible — with `PROXY_KEYS_EXTRA` unset the accepted
  set is exactly `{PROXY_KEY}`. Revoke a key by removing it from the secret value and
  restarting. Deliberately minimal: no per-key rate limits or attribution (we are not
  reinventing LiteLLM) — provider-side spend caps remain the enforcement point. Lets
  group members run the headless eval locally with a disposable key instead of the
  shared production key. Covered by `test_compute_valid_keys_multi_and_backward_compat`.
- **Baseline golden set: 4 new ca-30x30 regression questions (#40 grow-on-fix) —
  3 answer-mode + 1 clarify-mode.** Operator-verified gold + authoritative SQL +
  `trap` tags for the failures found in the 2026-07-10 proxy-log analysis of the
  ca-30x30 app (DSE-nimbus `qwen`):
  (answer) `% of California conserved` — pins the denominator to the source ecoregion
  polygon area (101.5M ac → 26.1%), trap `ca-denominator-ecoregion-source-area`;
  (answer) `CWHR13 habitat breakdown` — correct `whr13num` legend + fractional-hex area,
  traps `cwhr-code-name-from-schema-not-memory`, `cwhr13-use-fractional-hex-not-mode`;
  (answer) `% hardwood woodland conserved` — Hardwood Woodland = code 52 (13.6%), trap
  `cwhr-hardwood-woodland-is-code-52`; (clarify) `% of GAP-1 land in top-20% endemic
  richness` — genuinely ambiguous (endemic-metric + threshold-population forks), so the
  gold is to ASK, not answer; a preloaded `resolution` field carries the disambiguated
  answer (~20.8% for ACE AllTaxaEnd) to give if the model asks. `bench_mean_acc` is now
  nullable for grow-on-fix additions (not in the original 4-model benchmark), and
  clarify records gained an optional `resolution` field. First accuracy marks for
  `qwen` + `z-ai/glm-5.2` are recorded in `gold/ca-30x30.md` (both models silently
  answered the clarify question → FAIL). Related: geo-agent#303, ca-30x30#87,
  data-workflows#387, mcp-data-server#294.

### Changed
- **Re-verified the gold baseline against current NRP data vintage — no drift (#81).**
  WDPA advanced Dec-2025 → June-2026 (`WDPA_poly_Jun2026`, 306,985 features) and ca30x30's
  canonical file was re-confirmed authoritative. Re-ran the gold SQL for the four
  WDPA-dependent global-30x30 answers and both ca-30x30 GAP-status answers against
  `s3-west`: every value reproduced **exactly** (H3-hex aggregation at h8 is robust to
  WDPA's incremental additions). No expected-value edits to `gold/*.md` / `golden.json`;
  added provenance notes to the two gold headers recording the vintage they were
  re-verified against. Closes the loop from data-workflows#360.
- **Align ingress `timeout-client` with `timeout-server` (both 600s) — hygiene.**
  Added `haproxy-ingress.github.io/timeout-client: "600s"` so the client- and
  server-side idle timeouts match (the proxy calls upstream non-streaming, so a long
  completion leaves both sides idle). Does **not** resolve the ~300s ceiling on long
  single generations investigated in #82 — testing showed NRP's shared haproxy enforces
  a client timeout that this per-Ingress annotation doesn't override. #82 closed as
  resolved-by-finding: the practical answer for slow reasoning models (e.g. glm-5.2) is
  to run reasoning-OFF (consistently benchmarked as good OFF / not useful ON, #58), and
  the app exposes reasoning toggles — so the streaming/infra fix isn't worth pursuing.
- **Documented Claude prompt-caching routing (#75) — app selects the route by model id.**
  No code change: `anthropic/claude-*` already routes to OpenRouter (which maps the
  OpenAI-style `cache_control` breakpoint onto Anthropic's native param, so prompt
  caching lands), while bare `claude-*` routes to Anthropic's OpenAI-compat endpoint
  (which silently ignores `cache_control`). Verified end-to-end on
  `anthropic/claude-haiku-4.5`: a repeated ~6.8k-token cached prefix billed
  `cache_write_tokens` on the first call and `cached_tokens` on the second (~12× lower
  prefix cost). README's provider-routing section now spells out the two model ids
  side by side, the `anthropic/…` = "served by OpenRouter" naming gotcha, and the
  `"usage": {"include": true}` knob for surfacing cache accounting. Unblocks the
  geo-agent client half (per-model `prompt_cache: true`, already merged and off by
  default).
- **Nimbus (DSE) model renamed `nemotron` → `qwen`.** The `vllm-nimbus.carlboettiger.info`
  endpoint now serves `nvidia/Qwen3.6-35B-A3B-NVFP4` under the id `qwen` (was
  `nemotron`). Updated `config.json`'s `nimbus.models` to `["qwen"]` so the proxy
  (exact-match-then-prefix routing) forwards `model: "qwen"` to the nimbus endpoint;
  requests for `nemotron` no longer route anywhere. Requires a pod restart to take
  effect (config is git-synced at pod start).
- **Re-vendored `headless/mcp-client.js`** to match geo-agent upstream (#275 connect()
  race that could register zero MCP tools + reconnect-budget reset). `npm run
  check-drift` is clean again.
- **Headless runner resolves geo-agent via `GEO_AGENT_DIR` + `fresh-geoagent.sh`.**
  `run.js` now dynamic-imports the four geo-agent app modules from `GEO_AGENT_DIR`
  (default: the `../../geo-agent` sibling, so existing setups are unchanged). The
  new `headless/fresh-geoagent.sh` maintains an isolated `geo-agent@main` clone in
  a cache dir and prints its path, so `export GEO_AGENT_DIR="$(./fresh-geoagent.sh)"`
  gives a run its own pinned copy instead of depending on a shared dev checkout that
  other agents may be editing on branches. Only the app modules move; `mcp-client.js`
  stays vendored (bare-specifier resolution) and the script warns on drift.

### Added
- **New self-hosted provider `qwen3-cirrus`.** Adds the `qwen3-cirrus.carlboettiger.info`
  endpoint (qwen3 on the local k3s / cirrus host) to `config.json` under the model id
  `qwen3-cirrus` — distinct from nrp's `qwen3` and nimbus's `qwen` so the proxy's
  exact-match-then-prefix routing forwards it unambiguously. Reuses `NIMBUS_API_KEY`
  (same as the other `carlboettiger.info` vLLM endpoints) and is marked thinking-capable
  (`enable_thinking`). Requires a pod restart to take effect (config is git-synced at
  pod start).
- **Log the requested thinking mode `enable_thinking` (#64).** `log_request` now
  records `request.enable_thinking` — the mode the client **asked for** — alongside
  the existing response-side `has_reasoning_content`/`reasoning_content` (what the
  model actually **did**). Flattened to a typed `enable_thinking BOOLEAN` column
  (`null` = flag not sent / model default) in the consolidated Parquet schema and
  the per-turn session view; both cron jobs' re-flatten passes add the column to
  legacy files (as `null`) so the corpus stays on one schema. Disambiguates "reasoning
  off by request" from "model chose not to think" from "non-thinking model", making
  the effect of geo-agent's user-facing reasoning toggle (geo-agent#283) measurable
  from live traffic — not just the out-of-band headless A/B (#56/#60). See
  [LOGGING.md](LOGGING.md).
- **`ENABLE_THINKING` passthrough in the k8s matrix runner (#58).**
  `run-matrix-k8s.sh` now forwards an `ENABLE_THINKING` env (added to the export
  set, the `envsubst` allowlist, and the pod env in `matrix-job.yaml`), so a
  matrix sweep can pin reasoning on or off per pass. `run.js` (#56) turns it into
  the top-level `enable_thinking` flag, which the proxy maps to each model's
  `chat_template_kwargs` (qwen3/glm-5/kimi wired; gemma added in #57). Default
  `true` is behavior-preserving (reasoning-on is already the default; models
  without a `thinking_key` ignore it); the value is validated to `true`/`false`
  and never left empty (an empty-but-set value would read as an explicit `false`).
  Enables the two-pass reasoning ON/OFF assessment against the gold baseline (#58).

### Fixed
- **Headless runner no longer spuriously crashes slow-decode reasoning models (#61).**
  `run.js`'s fetch wrapper hard-capped every proxy call at 310s — *below* the agent's
  own 600s per-attempt budget — so a legitimately-slow reasoning call (glm-5/kimi with
  thinking ON, ~1 tok/s) was aborted mid-stream as `fetch failed`, which geo-agent
  classifies as a transient *network* error and retries on its tight 90s floor →
  `Request timed out after 90 seconds` → crash, no transcript. The wrapper cap is now
  derived from the agent budget (`llmTimeoutSec*1000 + 60s`) so it always sits above it
  and the agent's own clean timeout (full-budget retry) governs. New `--llm-timeout N`
  / `LLM_TIMEOUT_SECONDS` sets the agent's `llm_timeout_seconds` (default 600, behavior-
  preserving); `PER_FETCH_TIMEOUT_MS` overrides the wrapper backstop directly. Unblocks
  benchmarking reasoning-ON on slow models (#58). Startup banner now prints both effective
  timeouts.
- **`temperature` no longer force-sent to models that reject it.** The proxy
  unconditionally injected `temperature` (default `0.0`) into every upstream
  payload, so the newest Anthropic models — Claude Sonnet 5, Opus 4.8/4.7, Fable 5
  — returned `400 "temperature is deprecated for this model"` (they removed the
  sampling params entirely). Added a per-provider `no_sampling_params` list of
  model IDs (config-driven, matched exact-then-prefix like routing, and populated
  for the `anthropic` provider); `temperature`/`top_p` are dropped for those models
  and left untouched for everything else, so the forced `temperature: 0.0`
  determinism default (#33) still holds for open models and older Anthropic models
  (`claude-sonnet-4-6`, `claude-haiku-4-5`) that still accept it. Requires a pod
  restart to pick up the config change. (Follow-up: the `PROVIDERS` builder copies
  a fixed key whitelist from `config.json`, so `no_sampling_params` also had to be
  added there — without it the request-time lookup always saw an empty list and the
  guard never fired.)
- **nimbus `qwen` ignored `enable_thinking`; its reasoning trace wasn't logged (#66).**
  Two fixes for the direct nimbus vLLM endpoint (`nvidia/Qwen3.6-35B-A3B-NVFP4`):
  (1) added `nimbus.thinking_models = {"qwen": "enable_thinking"}` to `config.json` —
  the block had no `thinking_models`, so `proxy_chat` dropped the client's
  `enable_thinking` flag (`no thinking_key configured — ignoring`) and the endpoint
  reasoned regardless. Verified against the endpoint: `chat_template_kwargs=
  {"enable_thinking": false}` suppresses the trace, `true` restores it. (2) `log_response`
  now reads `message.reasoning_content or message.reasoning` — nimbus emits the trace
  under `reasoning` (not `reasoning_content` like NRP), so `has_reasoning_content` /
  `reasoning_content` were empty for nimbus even when it clearly reasoned. Pairs with
  the request-side `enable_thinking` column (#64) to make requested-vs-observed reasoning
  analyzable for nimbus. Requires a pod restart (config git-synced at boot).
- **gemma/gemma-small-e4b `enable_thinking` was silently ignored (#57).** These
  NRP models support disabling reasoning via `chat_template_kwargs={"enable_thinking":
  false}`, but they were absent from `config.json`'s `nrp.thinking_models`, so
  `proxy_chat` found no `thinking_key` and dropped the client's top-level
  `enable_thinking` flag (logging `no thinking_key configured — ignoring`) — the
  toggle appeared to work client-side but had no effect. Added `gemma` and
  `gemma-small-e4b` with the `enable_thinking` key. Unblocks including gemma in the
  reasoning ON/OFF assessment (#58).

### Added
- **Capture upstream response headers on the error path (#44).** On
  `httpx.HTTPStatusError`, `proxy_chat` logged only the status code and (often
  empty) body, discarding the response headers that distinguish a genuine
  rate-limit (`429` + `retry-after`/`x-ratelimit-*`) from a dead-backend gateway
  failure (naked `500`, `content-length: 0`, no `server`/`x-request-id`). That
  distinction was previously only catchable live with `curl -i` and impossible to
  recover after the fact. Now an allow-listed subset (`retry-after`,
  `x-ratelimit-{limit,remaining,reset}`, `x-request-id`, `server`, `date`,
  `content-length`) is captured into the error response log under
  `entry.upstream_headers`, queryable via `json_extract(entry,'$.upstream_headers')`.
  Allow-list only (no full header bag); values pass through the scrubber for
  defense in depth. Only the `HTTPStatusError` branch has a response to read —
  the timeout/connection branches fail without one. Not promoted to a flat
  consolidated column (entry-JSON access suffices for occasional debugging).
- **Standing baseline question set for guidance-change regression testing (#40).**
  New `headless/baseline/`: 22 analytical questions with operator-verified golden
  answers + authoritative SQL (`gold/`), seeded from the open-model benchmark
  (`headless/experiments/2026-06-26-or-openmodel-bench`). `golden.json` tags each
  question with the **trap it guards** (the #42 rule-store key), an `accept` rule,
  and first-run difficulty (`bench_mean_acc`). This is the durable set the
  MCP-server guidance-change gate regresses against (per-question/instance-level,
  not aggregate; gold is operator-verified, never model consensus). Encodes the
  dev-MCP targeting requirement (validation must hit `dev-duckdb-mcp`, not prod).
  `build_golden.py` regenerates the manifest; grow-on-fix as new traps are found.
- **Forward sampling/routing knobs instead of dropping them (#47).** `proxy_chat`
  rebuilt the upstream payload from a hard whitelist (`model`/`messages`/
  `temperature` + `tools`), so any other client field was silently dropped before
  forwarding. Added `top_p`, `seed`, `stop`, `max_tokens`, and `response_format`
  to `ChatRequest` and forward each verbatim when present (non-None) on any
  provider. The OpenRouter-isms — the `provider` routing block (`zdr`/`order`/
  `only`/`require_parameters`, ...) and top-level `usage` (`{"include": true}`) —
  are guarded to `provider_name == "openrouter"`, since a strict OpenAI-compatible
  server (e.g. vllm) may reject them. This unblocks per-request `seed`/`top_p`
  determinism (geo-agent#266), provider steering for cache/cost, and per-request
  `provider.zdr`. `cache_control` inside
  message content blocks already passed through (the `messages` array is forwarded
  verbatim); the NRP `cache_salt` path is unchanged. Relates to geo-agent#273.

### Fixed
- **`config.json`: corrected two stale NRP model ids that 404'd at the gateway.**
  `ellm.nrp-nautilus.io`'s `/v1/models` no longer serves `glm-4.7` or
  `gemma-4-e4b`; requests for them returned `404 No matching route found`. Renamed
  to the currently-served ids `glm-5` and `gemma-small-e4b` (and updated the
  `thinking_models` key `glm-4.7` → `glm-5`).

### Added
- **OpenRouter: link `z-ai/`, `minimax/`, and `moonshotai/` model families.**
  Added these three vendor prefixes to `config.json`'s OpenRouter `models` list
  (and the in-code fallback + README provider table), so ids like `z-ai/glm-5.2`,
  `minimax/minimax-m3`, and `moonshotai/kimi-k2.7-code` route to OpenRouter
  instead of falling through to the NRP default. Enables an open-model
  performance/accuracy evaluation across these families. The proxy reads
  `config.json` from a fresh `git clone` of `main` at pod boot, so this reaches
  prod on the next `rollout restart`. (Also synced the stale `glm-4.6`→`glm-5`
  and missing `nvidia/` entries in the in-code fallback default.)
- **Headless matrix: `GEO_AGENT_BRANCH` to pin the geo-agent framework clone.**
  The matrix Job hard-coded a `main` clone of `boettiger-lab/geo-agent`, which
  supplies the framework (`Agent` / `DatasetCatalog` / `ToolRegistry` /
  `createMapTools`) the runner imports — so there was no way to evaluate a
  code-level geo-agent change before it merged and shipped in a pinned release.
  `run-matrix-k8s.sh` now accepts `GEO_AGENT_BRANCH` (default `main`) and the Job
  clones that branch, mirroring the existing `APP_BRANCH`. Run the matrix once on
  a fix branch and once on `main` to A/B a change (e.g. a tool-description
  variant) across the open model collection before pinning the fleet.
- **Query-ready consolidated logs: flattened columns + a materialized session
  view (#31).** The daily consolidation now promotes the hot fields
  (`session_id`, `client`, `provider`, `model`, `message_count`, `tools_count`,
  `user_question`, `latency_ms`, `has_tool_calls`, `has_content`, `tool_calls`,
  `tool_results`, `tokens`, `error`) to typed columns alongside the verbatim
  `entry` blob, removing the `entry::JSON->>` cast traps for common queries while
  staying backward-compatible (existing `entry`-based queries still work). A new
  `sessions/{daily,monthly}/` tier materializes one row per **turn** — request
  joined to its response, keyed on `session_key` (`session_id`, or an
  `anon:<hash>` fallback) and ordered by `turn_idx` — so reconstructing a session
  ("every turn of X in order, with tool calls and results") is a single flat
  `SELECT` with no manual request/response interleaving. The daily job backfills
  session views for already-consolidated days that lack one; the monthly rollup
  rebuilds the view over the whole month (correct cross-midnight `turn_idx`) and
  reads daily files with `union_by_name=true` so a month mixing legacy
  (entry-only) and flattened daily files merges cleanly. Both jobs also run a
  **self-healing schema-upgrade pass** that re-flattens any legacy 5-column
  consolidated file in place from its preserved `entry` blob (lossless,
  idempotent), so the whole `consolidated/**` corpus converges to one schema and
  old logs gain the flat columns too — no mixed-schema barrier for analysts.
  Existing `entry`-based queries were never at risk (DuckDB name-matches common
  columns across a mixed glob). `LOGGING.md` / `AGENTS.md` document both schemas,
  the `sessions/**` ⟂ `consolidated/**` glob split, and the mixed-glob caveat.

### Fixed
- **Response logging restored (#37).** Since the #26 (X-Client) deploy, the
  `async with httpx.AsyncClient(...) as client` block shadowed the `client`
  X-Client header parameter, so `log_response(..., client=client)` passed the
  httpx client object; `json.dumps` then raised inside the `@_never_raises`
  wrapper and **every response was silently dropped from S3** (requests were
  unaffected — they log before the block). Renamed the context var to
  `http_client`. Added a handler-level regression test that drives `proxy_chat`
  with a mocked upstream and asserts a serializable `type: "response"` entry
  lands in the buffer.

## [0.1.0] - 2026-06-24

First tagged release. The proxy has run in production (`biodiversity` namespace,
`https://open-llm-proxy.nrp-nautilus.io`) since 2026-02-26; this release captures
that accumulated state as a baseline and starts tracking changes going forward.

### Added
- Multi-provider routing for `/v1/chat/completions` across NRP, OpenRouter,
  Nimbus, and a direct Anthropic (OpenAI-compatible) provider, selected by model
  name (#21).
- `session_id` is now populated from the OpenAI `user` request-body field
  (falling back to an `X-Session-Id` header), giving every log an exact
  session key instead of the lossy `(origin, user_question)` heuristic. `user`
  is logged only, never forwarded upstream (#31, #34).
- `X-Client` request header captured into logs to correlate behavior with a
  client release (#26).
- Training-grade logging: full response `content`/`reasoning_content`, full
  `tool_calls`, capture modes (`summary`/`full`), per-field caps, system-prompt
  dedup, and always-on credential scrubbing (#25).
- Tiered S3 log storage: raw JSONL → daily Parquet → monthly Parquet via
  consolidation CronJobs; `sync-logs.sh` for local-first analysis (#11, #15).
- `headless/` session-replay runner that imports geo-agent live, plus k8s Job
  driver for model × question matrix sweeps (#17, #18, #19).
- One-off `scrub-historical-logs.py` job to redact leaked credentials from
  pre-scrubbing Parquet in place (idempotent).
- `geo-agent-training` skill and `duckdb-geo` MCP server config (#22, #23).

### Changed
- Default `temperature` is `0.0` (was `0.7`) (#33).
- S3 log flush interval reduced from 300s to 60s (`FLUSH_INTERVAL`).
- Provider error truncation raised 200 → 1000 chars for debugging.
- Scaled to 3 replicas with collision-safe flush keys and HA scheduling.

### Fixed
- Re-queue buffered log entries on flush failure instead of dropping them (#27).
- DNS resilience: per-pod CoreDNS caching sidecar with `serve_stale`, plus
  `ndots`/`attempts` tuning to curb upstream `EAI_AGAIN` 502s (#29, #30).
- `boto3` added to `requirements.txt` — it was a runtime + test dependency
  missing from CI, which had been red since #27.

### Infrastructure notes
- CORS is enforced at the haproxy **ingress**, not the app — custom request
  headers must be added to `cors-allow-headers` in `ingress.yaml` (#26).
- Pods pull application code by cloning `main` at startup (init container); a
  rollout is `kubectl rollout restart`, no image build.

[Unreleased]: https://github.com/boettiger-lab/open-llm-proxy/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/boettiger-lab/open-llm-proxy/releases/tag/v0.1.0
