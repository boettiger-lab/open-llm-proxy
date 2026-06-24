#!/usr/bin/env python3
"""Log consolidation + flattening (issue #31).

Single source of truth for the consolidation SQL, shared by three entrypoints
so the flattened schema and session-view shape can never drift between them:

    python consolidate.py daily      # raw YYYY-MM-DD/*.jsonl -> flat daily parquet
                                      #   + sessions/daily/YYYY-MM-DD.parquet
    python consolidate.py monthly    # roll completed month's daily parquet up
                                      #   + rebuild sessions/monthly/YYYY-MM.parquet
    python consolidate.py backfill    # rewrite pre-flatten consolidated parquet
                                      #   in place to the flat schema + session views

The daily/monthly commands are invoked by the CronJobs (consolidate-*-cronjob.yaml);
backfill is a one-off Job (flatten-historical-logs-job.yaml). All three run inside
NRP pods and reach Ceph via the internal RGW endpoint.

Two parquet trees are produced (kept separate so the documented
`consolidated/**/*.parquet` glob keeps a single, uniform schema):

  consolidated/{daily,monthly}/...  -> one row per log entry (request OR response),
        hot fields promoted to typed columns alongside the raw `entry` JSON text.
  sessions/{daily,monthly}/...      -> one row per *turn* (request+response paired),
        interleaved and keyed on session_id (heuristic fallback for old null rows).

See LOGGING.md for the full schema and query patterns.
"""
import os
import sys
import datetime

import boto3
import duckdb
from botocore.client import Config

BUCKET = os.environ.get("LOG_BUCKET", "logs-open-llm-proxy")
# Internal RGW endpoint (fast, no public-endpoint throttling) by default; the
# host (without scheme) is what DuckDB's CREATE SECRET wants.
ENDPOINT_HOST = os.environ.get("S3_ENDPOINT_HOST", "rook-ceph-rgw-nautiluss3.rook")
USE_SSL = os.environ.get("S3_USE_SSL", "false").lower() == "true"


def s3_client():
    scheme = "https" if USE_SSL else "http"
    return boto3.client(
        "s3",
        endpoint_url=f"{scheme}://{ENDPOINT_HOST}",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        config=Config(s3={"addressing_style": "path"}),
    )


def duck():
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE SECRET s3_logs (
            TYPE S3,
            KEY_ID '{os.environ["AWS_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["AWS_SECRET_ACCESS_KEY"]}',
            ENDPOINT '{ENDPOINT_HOST}',
            USE_SSL {str(USE_SSL).lower()},
            URL_STYLE 'path'
        )
        """
    )
    return con


# ── Shared SQL builders ──────────────────────────────────────────────────────
# Both operate on a relation that exposes an `entry` VARCHAR column holding the
# full original JSON record. json_extract_string / json_extract are used (never
# `entry::JSON->>`) because the latter intermittently throws "Failed to cast
# value to numerical" — see LOGGING.md's brittle-JSON caveat.

def flatten_select(from_clause, entry="entry"):
    """SELECT that promotes hot fields to typed columns, keeping raw `entry`.

    `from_clause` must yield an `entry` VARCHAR column (aliased per `entry`)."""
    e = entry
    return f"""
        SELECT
            json_extract_string({e}, '$.timestamp')::TIMESTAMPTZ        AS ts,
            json_extract_string({e}, '$.type')                          AS type,
            json_extract_string({e}, '$.request_id')                    AS request_id,
            json_extract_string({e}, '$.origin')                        AS origin,
            json_extract_string({e}, '$.session_id')                    AS session_id,
            json_extract_string({e}, '$.client')                        AS client,
            json_extract_string({e}, '$.model')                         AS model,
            json_extract_string({e}, '$.provider')                      AS provider,
            TRY_CAST(json_extract_string({e}, '$.message_count') AS INTEGER) AS message_count,
            TRY_CAST(json_extract_string({e}, '$.tools_count')   AS INTEGER) AS tools_count,
            json_extract_string({e}, '$.user_question')                 AS user_question,
            TRY_CAST(json_extract_string({e}, '$.latency_ms') AS INTEGER)    AS latency_ms,
            TRY_CAST(json_extract_string({e}, '$.has_content') AS BOOLEAN)            AS has_content,
            TRY_CAST(json_extract_string({e}, '$.has_tool_calls') AS BOOLEAN)        AS has_tool_calls,
            TRY_CAST(json_extract_string({e}, '$.has_reasoning_content') AS BOOLEAN) AS has_reasoning_content,
            TRY_CAST(json_extract_string({e}, '$.tokens.total_tokens') AS INTEGER)   AS total_tokens,
            json_extract({e}, '$.tool_calls')                           AS tool_calls,
            json_extract({e}, '$.tool_results_this_turn')               AS tool_results,
            json_extract({e}, '$.tokens')                               AS tokens,
            json_extract_string({e}, '$.error')                         AS error,
            {e}                                                         AS entry
        FROM {from_clause}
    """


def session_view(flat_sql):
    """One row per turn (request+response paired), interleaved and ordered.

    `flat_sql` is any query producing flattened rows (see flatten_select). The
    session key is the exact `session_id` when present, falling back to the
    `(origin, user_question)` heuristic for pre-session_id (null) records.
    `latest_user_message` is the last role:user message and is populated only
    when the request carries a `messages` array (LOG_CAPTURE_MODE=full); it is
    NULL in summary mode."""
    sk = "coalesce(r.session_id, r.origin || '|' || coalesce(r.user_question, ''))"
    latest_user = """
        list_last(list_transform(
            list_filter(
                CAST(json_extract(r.entry, '$.messages') AS JSON[]),
                m -> json_extract_string(m, '$.role') = 'user'),
            m -> json_extract_string(m, '$.content')))
    """
    assistant = """
        coalesce(json_extract_string(p.entry, '$.content'),
                 json_extract_string(p.entry, '$.content_preview'))
    """
    return f"""
        WITH flat AS ({flat_sql}),
             r AS (SELECT * FROM flat WHERE type = 'request'),
             p AS (SELECT * FROM flat WHERE type = 'response')
        SELECT
            {sk}                                                          AS session_key,
            r.session_id                                                  AS session_id,
            row_number() OVER (PARTITION BY {sk} ORDER BY r.ts, r.request_id) AS turn_idx,
            r.ts                                                          AS ts,
            r.origin                                                      AS origin,
            r.model                                                       AS model,
            r.provider                                                    AS provider,
            r.client                                                      AS client,
            r.message_count                                               AS message_count,
            r.user_question                                               AS user_question,
            {latest_user}                                                 AS latest_user_message,
            {assistant}                                                   AS assistant_text,
            p.tool_calls                                                  AS tool_calls,
            r.tool_results                                                AS tool_results,
            p.has_content                                                 AS has_content,
            p.has_tool_calls                                              AS has_tool_calls,
            p.latency_ms                                                  AS latency_ms,
            p.total_tokens                                                AS total_tokens,
            p.error                                                       AS error,
            r.request_id                                                  AS request_id
        FROM r LEFT JOIN p ON r.request_id = p.request_id
        ORDER BY session_key, turn_idx
    """


def copy_to(con, select_sql, key):
    con.execute(
        f"COPY ({select_sql}) TO 's3://{BUCKET}/{key}' (FORMAT PARQUET, COMPRESSION zstd)"
    )


def list_keys(s3, prefix):
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def delete_keys(s3, keys):
    objs = [{"Key": k} for k in keys]
    for i in range(0, len(objs), 1000):
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objs[i : i + 1000]})


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_daily():
    """Consolidate each completed YYYY-MM-DD/ of JSONL into flat daily parquet
    + a daily session view, then delete the raw inputs. Skips today and any day
    already consolidated (idempotent)."""
    s3 = s3_client()
    today = datetime.datetime.utcnow().date().isoformat()

    date_prefixes = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            pref = p["Prefix"].rstrip("/")
            try:
                datetime.date.fromisoformat(pref)
                date_prefixes.add(pref)
            except ValueError:
                pass

    existing = {
        k.split("/")[-1][: -len(".parquet")]
        for k in list_keys(s3, "consolidated/daily/")
        if k.endswith(".parquet")
    }

    to_do = sorted(d for d in date_prefixes if d != today and d not in existing)
    print(f"Days to consolidate ({len(to_do)}): {to_do}")
    if not to_do:
        print("Nothing to do.")
        return

    con = duck()
    for day in to_do:
        print(f"→ {day}")
        src = f"(SELECT json::VARCHAR AS entry FROM read_ndjson_objects('s3://{BUCKET}/{day}/*.jsonl'))"
        flat = flatten_select(src)
        copy_to(con, f"SELECT * FROM ({flat}) ORDER BY ts", f"consolidated/daily/{day}.parquet")
        copy_to(con, session_view(flat), f"sessions/daily/{day}.parquet")
        # Verify both outputs exist before destroying inputs.
        s3.head_object(Bucket=BUCKET, Key=f"consolidated/daily/{day}.parquet")
        s3.head_object(Bucket=BUCKET, Key=f"sessions/daily/{day}.parquet")
        raw = list_keys(s3, f"{day}/")
        delete_keys(s3, raw)
        print(f"  ✓ flat + session view written; {len(raw)} JSONL chunks removed")
    print("Done.")


def cmd_monthly():
    """Roll the just-closed month's daily parquet into one monthly file (entry +
    session view), then delete the daily inputs. Session turn_idx is recomputed
    across the full month so it is correct for cross-day sessions."""
    s3 = s3_client()
    today = datetime.datetime.utcnow().date()
    prev_month_end = today.replace(day=1) - datetime.timedelta(days=1)
    prev_month = prev_month_end.strftime("%Y-%m")
    print(f"Rolling up month: {prev_month}")

    daily_entry = [
        k for k in list_keys(s3, f"consolidated/daily/{prev_month}-") if k.endswith(".parquet")
    ]
    print(f"Daily entry files found: {len(daily_entry)}")
    if not daily_entry:
        print("Nothing to roll up.")
        return

    daily_sessions = [
        k for k in list_keys(s3, f"sessions/daily/{prev_month}-") if k.endswith(".parquet")
    ]
    entry_key = f"consolidated/monthly/{prev_month}.parquet"
    session_key = f"sessions/monthly/{prev_month}.parquet"

    con = duck()
    copy_to(
        con,
        f"SELECT * FROM read_parquet('s3://{BUCKET}/consolidated/daily/{prev_month}-*.parquet') ORDER BY ts",
        entry_key,
    )
    # Rebuild the session view from the merged month so turn_idx spans days.
    flat = f"SELECT * FROM read_parquet('s3://{BUCKET}/{entry_key}')"
    copy_to(con, session_view(flat), session_key)

    s3.head_object(Bucket=BUCKET, Key=entry_key)
    s3.head_object(Bucket=BUCKET, Key=session_key)
    delete_keys(s3, daily_entry + daily_sessions)
    print(
        f"✓ Rolled up {len(daily_entry)} daily files → {entry_key} + {session_key}; "
        f"{len(daily_entry) + len(daily_sessions)} daily files removed."
    )


def _rewrite_flat(con, s3, entry_key, session_key):
    """Rewrite one pre-flatten entry parquet in place to the flat schema and
    (re)build its session view. Idempotent: re-running on an already-flat file
    re-derives the same columns. Verifies row count is preserved before the
    atomic replace, so a partial read can never truncate the corpus."""
    src = f"read_parquet('s3://{BUCKET}/{entry_key}')"
    n_in = con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
    tmp = entry_key + ".tmp"
    flat = flatten_select(src)
    copy_to(con, f"SELECT * FROM ({flat}) ORDER BY ts", tmp)
    n_out = con.execute(f"SELECT count(*) FROM read_parquet('s3://{BUCKET}/{tmp}')").fetchone()[0]
    if n_in != n_out:
        s3.delete_object(Bucket=BUCKET, Key=tmp)
        raise RuntimeError(f"{entry_key}: row count {n_in} -> {n_out}; aborting (temp deleted)")
    s3.copy_object(Bucket=BUCKET, Key=entry_key, CopySource={"Bucket": BUCKET, "Key": tmp})
    s3.delete_object(Bucket=BUCKET, Key=tmp)
    # Session view rebuilt from the now-flat entry file.
    copy_to(con, session_view(f"read_parquet('s3://{BUCKET}/{entry_key}')"), session_key)
    s3.head_object(Bucket=BUCKET, Key=session_key)
    print(f"  ✓ {entry_key}: {n_in} rows flattened → + {session_key}")


def cmd_backfill():
    """One-off: rewrite every existing consolidated parquet (daily + monthly) to
    the flat schema and materialize its session view. Safe to re-run."""
    s3 = s3_client()
    con = duck()
    daily = sorted(k for k in list_keys(s3, "consolidated/daily/") if k.endswith(".parquet"))
    monthly = sorted(k for k in list_keys(s3, "consolidated/monthly/") if k.endswith(".parquet"))
    print(f"Backfilling {len(daily)} daily + {len(monthly)} monthly files")
    for k in daily:
        name = k.split("/")[-1]
        _rewrite_flat(con, s3, k, f"sessions/daily/{name}")
    for k in monthly:
        name = k.split("/")[-1]
        _rewrite_flat(con, s3, k, f"sessions/monthly/{name}")
    print("Backfill done.")


COMMANDS = {"daily": cmd_daily, "monthly": cmd_monthly, "backfill": cmd_backfill}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(COMMANDS)}}}")
    COMMANDS[sys.argv[1]]()
