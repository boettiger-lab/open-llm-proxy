#!/usr/bin/env python3
"""Roll raw JSONL log chunks up into the Parquet tiers (see LOGGING.md).

    python consolidate_logs.py --tier daily     # YYYY-MM-DD/*.jsonl -> consolidated/daily/
    python consolidate_logs.py --tier monthly   # consolidated/daily/ -> consolidated/monthly/

Both tiers also materialize the per-turn session view (`sessions/**`) and run
idempotent self-healing passes (schema upgrade of legacy files, session-view
backfill). Everything is safe to re-run: outputs are verified with head_object
before any input is deleted, and days/months that already have their artifacts
are skipped.

This is the single implementation shared by every deployment's CronJob — the
NRP one (Rook/Ceph) and the cirrus one (MinIO). The only differences are
environment:

    LOG_BUCKET          bucket holding the logs        (default logs-open-llm-proxy)
    S3_ENDPOINT_URL     S3 endpoint, scheme included   (default http://rook-ceph-rgw-nautiluss3.rook)
    S3_URL_STYLE        'path' or 'vhost'              (default path)
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY          (required)
"""

import argparse
import datetime
import os
from urllib.parse import urlparse

import boto3
import duckdb
from botocore.client import Config

BUCKET = os.getenv("LOG_BUCKET", "logs-open-llm-proxy")
ENDPOINT = os.getenv("S3_ENDPOINT_URL", "http://rook-ceph-rgw-nautiluss3.rook")
URL_STYLE = os.getenv("S3_URL_STYLE", "path")

# DuckDB's S3 secret wants the endpoint as a bare host[:port] plus a separate
# USE_SSL flag, where boto3 wants a full URL — derive both from one env var so a
# deployment can't end up with the two clients pointed at different stores.
_parsed = urlparse(ENDPOINT)
DUCKDB_ENDPOINT = _parsed.netloc or _parsed.path
DUCKDB_USE_SSL = "true" if _parsed.scheme == "https" else "false"

s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT,
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    config=Config(s3={"addressing_style": "path" if URL_STYLE == "path" else "virtual"}),
)
paginator = s3.get_paginator("list_objects_v2")


def connect():
    con = duckdb.connect()
    # In a container DuckDB sees every node core and spawns that many worker
    # threads — each with its own buffers — while CPU is throttled to the pod
    # request, so memory balloons and OOMs the pod. Pin a small thread count and
    # drop insertion-order buffering to keep COPY/sort memory bounded (the data
    # is re-sorted by ts on read anyway).
    con.execute("SET threads=2")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"""
        CREATE SECRET s3_logs (
            TYPE S3,
            KEY_ID '{os.environ["AWS_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["AWS_SECRET_ACCESS_KEY"]}',
            ENDPOINT '{DUCKDB_ENDPOINT}',
            USE_SSL {DUCKDB_USE_SSL},
            URL_STYLE '{URL_STYLE}'
        )
    """)
    return con


def _parquet_names(prefix):
    """Basenames (sans .parquet) of every Parquet file under `prefix`."""
    out = set()
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].split("/")[-1]
            if name.endswith(".parquet"):
                out.add(name[: -len(".parquet")])
    return out


def build_session_view(con, src, dest):
    """Materialize the per-turn session view from a consolidated Parquet glob.

    One row per request_id = one turn, request joined to its response. Reads only
    the raw `entry` JSON via json_extract_string / json_extract — the cast-safe
    access form — so it works on both legacy (entry-only) and new (flattened)
    consolidated files. turn_idx is assigned within `src` ordered by ts; for the
    daily tier that means within-day (a session spanning UTC midnight restarts at
    1 in the next day's file — ordering by ts is still correct, only the index
    resets). The monthly rollup recomputes it over the whole month.
    """
    con.execute(f"""
        COPY (
            WITH base AS (
                SELECT ts, type, request_id, origin,
                    json_extract_string(entry,'$.session_id')    AS session_id,
                    json_extract_string(entry,'$.client')        AS client,
                    json_extract_string(entry,'$.provider')      AS provider,
                    json_extract_string(entry,'$.model')         AS model,
                    TRY_CAST(json_extract_string(entry,'$.message_count') AS INTEGER) AS message_count,
                    TRY_CAST(json_extract_string(entry,'$.enable_thinking') AS BOOLEAN) AS enable_thinking,
                    json_extract_string(entry,'$.user_question') AS user_question,
                    json_extract_string(entry,'$.user_message_this_turn') AS user_message_this_turn,
                    json_extract(entry,'$.tool_results_this_turn') AS tool_results,
                    TRY_CAST(json_extract_string(entry,'$.latency_ms') AS BIGINT) AS latency_ms,
                    TRY_CAST(json_extract_string(entry,'$.has_tool_calls') AS BOOLEAN) AS has_tool_calls,
                    TRY_CAST(json_extract_string(entry,'$.has_content')   AS BOOLEAN) AS has_content,
                    json_extract(entry,'$.tool_calls')           AS tool_calls,
                    json_extract(entry,'$.tokens')               AS tokens,
                    json_extract_string(entry,'$.content')       AS assistant_content,
                    json_extract_string(entry,'$.reasoning_content') AS reasoning_content,
                    json_extract_string(entry,'$.error')         AS error
                FROM read_parquet('{src}')
            ),
            keyed AS (
                SELECT *, COALESCE(session_id,
                    'anon:' || md5(COALESCE(origin,'') || '|' || COALESCE(user_question,''))) AS session_key
                FROM base WHERE type='request'
            ),
            req AS (
                SELECT session_key, session_id, request_id, ts, origin, client, provider, model,
                       user_question, user_message_this_turn, message_count, enable_thinking, tool_results,
                       ROW_NUMBER() OVER (PARTITION BY session_key ORDER BY ts, request_id) AS turn_idx
                FROM keyed
            ),
            resp AS (
                SELECT request_id, ts AS response_ts, latency_ms, has_tool_calls, has_content,
                       tool_calls, tokens, assistant_content, reasoning_content, error
                FROM base WHERE type='response'
            )
            SELECT req.session_key, req.session_id, req.turn_idx,
                   req.ts AS request_ts, resp.response_ts, req.request_id,
                   req.origin, req.client, req.provider, req.model,
                   req.user_question, req.user_message_this_turn, req.message_count, req.enable_thinking, req.tool_results,
                   resp.assistant_content, resp.reasoning_content, resp.tool_calls,
                   resp.has_tool_calls, resp.has_content, resp.latency_ms, resp.tokens, resp.error
            FROM req LEFT JOIN resp USING (request_id)
            ORDER BY req.session_key, req.turn_idx
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION zstd)
    """)


def reflatten(con, path):
    """Upgrade a legacy (entry-only, 5-column) consolidated Parquet file to the
    flattened wide schema in place, reconstructing the typed columns from its own
    `entry` blob. Lossless (`entry` is preserved verbatim) and idempotent: a
    no-op once the file already has the wide schema. Keeps the whole
    consolidated/** corpus on one schema so legacy and new files compose in a
    single glob without union_by_name and without the "column not found" gap on
    the new flat columns. Materializes to a TEMP table first so the read
    completes before the COPY overwrites the source key.
    """
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()]
    # Sentinel = the newest flat column. Guarding on the latest-added column (not
    # just 'model') re-flattens wide-but-stale files when a new column lands,
    # keeping consolidated/** on one schema so the glob composes without
    # union_by_name (see the flatten note below).
    if "user_message_this_turn" in cols:
        return False
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _wide AS
        SELECT ts, type, request_id,
               json_extract_string(entry,'$.session_id')    AS session_id,
               origin,
               json_extract_string(entry,'$.client')        AS client,
               json_extract_string(entry,'$.provider')      AS provider,
               json_extract_string(entry,'$.model')         AS model,
               TRY_CAST(json_extract_string(entry,'$.message_count') AS INTEGER) AS message_count,
               TRY_CAST(json_extract_string(entry,'$.tools_count')   AS INTEGER) AS tools_count,
               TRY_CAST(json_extract_string(entry,'$.enable_thinking') AS BOOLEAN) AS enable_thinking,
               json_extract_string(entry,'$.user_question') AS user_question,
               json_extract_string(entry,'$.user_message_this_turn') AS user_message_this_turn,
               TRY_CAST(json_extract_string(entry,'$.latency_ms') AS BIGINT) AS latency_ms,
               TRY_CAST(json_extract_string(entry,'$.has_tool_calls') AS BOOLEAN) AS has_tool_calls,
               TRY_CAST(json_extract_string(entry,'$.has_content')   AS BOOLEAN) AS has_content,
               json_extract(entry, '$.tool_calls')             AS tool_calls,
               json_extract(entry, '$.tool_results_this_turn')  AS tool_results,
               json_extract(entry, '$.tokens')                  AS tokens,
               json_extract_string(entry,'$.error')         AS error,
               entry
        FROM read_parquet('{path}')
        ORDER BY ts
    """)
    con.execute(f"COPY _wide TO '{path}' (FORMAT PARQUET, COMPRESSION zstd)")
    return True


def run_daily():
    """Consolidate each completed YYYY-MM-DD/ directory of JSONL chunks into one
    consolidated/daily/YYYY-MM-DD.parquet + session view, then delete the
    originals. Always skips today (still being written)."""
    today = datetime.datetime.utcnow().date().isoformat()

    # Discover all YYYY-MM-DD/ prefixes at the bucket root
    date_prefixes = set()
    for page in paginator.paginate(Bucket=BUCKET, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            pref = p["Prefix"].rstrip("/")
            try:
                datetime.date.fromisoformat(pref)
                date_prefixes.add(pref)
            except ValueError:
                pass

    # Existing consolidated daily Parquet files, and existing session views
    existing = _parquet_names("consolidated/daily/")
    existing_sessions = _parquet_names("sessions/daily/")

    to_do = sorted(d for d in date_prefixes if d != today and d not in existing)
    # Days already consolidated but missing a session view (e.g. files written
    # before the session view existed) — backfill from Parquet.
    backfill_sessions = sorted(existing - existing_sessions - set(to_do))
    print(f"Days to consolidate ({len(to_do)}): {to_do}")
    print(f"Session views to backfill ({len(backfill_sessions)}): {backfill_sessions}")
    # Note: we always continue to the schema-upgrade pass below, which
    # re-flattens any legacy consolidated daily file — it must run even on days
    # with nothing new to consolidate or backfill.

    con = connect()

    for day in to_do:
        print(f"→ {day}")
        # Flatten the hot fields to typed columns alongside the raw `entry` blob
        # (kept verbatim for fidelity). Direct queries no longer need
        # entry::JSON-> casting; `entry` is still there.
        con.execute(f"""
            COPY (
                SELECT (json->>'timestamp')::TIMESTAMPTZ            AS ts,
                       json->>'type'                                AS type,
                       json->>'request_id'                          AS request_id,
                       json->>'session_id'                          AS session_id,
                       json->>'origin'                              AS origin,
                       json->>'client'                              AS client,
                       json->>'provider'                            AS provider,
                       json->>'model'                               AS model,
                       TRY_CAST(json->>'message_count' AS INTEGER)  AS message_count,
                       TRY_CAST(json->>'tools_count'   AS INTEGER)  AS tools_count,
                       TRY_CAST(json->>'enable_thinking' AS BOOLEAN) AS enable_thinking,
                       json->>'user_question'                       AS user_question,
                       json->>'user_message_this_turn'              AS user_message_this_turn,
                       TRY_CAST(json->>'latency_ms' AS BIGINT)      AS latency_ms,
                       TRY_CAST(json->>'has_tool_calls' AS BOOLEAN) AS has_tool_calls,
                       TRY_CAST(json->>'has_content'    AS BOOLEAN) AS has_content,
                       json_extract(json, '$.tool_calls')              AS tool_calls,
                       json_extract(json, '$.tool_results_this_turn')  AS tool_results,
                       json_extract(json, '$.tokens')                  AS tokens,
                       json->>'error'                               AS error,
                       json::VARCHAR                                AS entry
                FROM read_ndjson_objects('s3://{BUCKET}/{day}/*.jsonl')
                ORDER BY ts
            ) TO 's3://{BUCKET}/consolidated/daily/{day}.parquet'
              (FORMAT PARQUET, COMPRESSION zstd)
        """)
        # Verify Parquet exists before destroying inputs
        s3.head_object(Bucket=BUCKET, Key=f"consolidated/daily/{day}.parquet")
        # Build the per-turn session view from the just-written Parquet.
        build_session_view(con,
                           f"s3://{BUCKET}/consolidated/daily/{day}.parquet",
                           f"s3://{BUCKET}/sessions/daily/{day}.parquet")
        s3.head_object(Bucket=BUCKET, Key=f"sessions/daily/{day}.parquet")
        to_delete = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{day}/"):
            for obj in page.get("Contents", []):
                to_delete.append({"Key": obj["Key"]})
        for i in range(0, len(to_delete), 1000):
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": to_delete[i:i + 1000]})
        print(f"  ✓ {len(to_delete)} JSONL chunks removed; session view written")

    for day in backfill_sessions:
        print(f"↺ backfill session view: {day}")
        build_session_view(con,
                           f"s3://{BUCKET}/consolidated/daily/{day}.parquet",
                           f"s3://{BUCKET}/sessions/daily/{day}.parquet")

    # Schema-upgrade pass: bring any legacy-schema consolidated daily file
    # (current month — older months live in monthly files, upgraded by the
    # monthly job) up to the wide schema. Self-healing no-op once uniform.
    upgraded = 0
    for day in sorted(_parquet_names("consolidated/daily/")):
        if reflatten(con, f"s3://{BUCKET}/consolidated/daily/{day}.parquet"):
            upgraded += 1
            print(f"  ⬆ re-flattened {day} to wide schema")
    print(f"Legacy daily files upgraded to wide schema: {upgraded}")


def run_monthly():
    """Merge the just-closed month's daily Parquet files into
    consolidated/monthly/YYYY-MM.parquet (+ session view), then delete the daily
    inputs."""
    # Previous completed month (UTC)
    today = datetime.datetime.utcnow().date()
    first_of_this_month = today.replace(day=1)
    prev_month_end = first_of_this_month - datetime.timedelta(days=1)
    prev_month = prev_month_end.strftime("%Y-%m")
    print(f"Rolling up month: {prev_month}")

    # Daily files for that month
    daily_keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"consolidated/daily/{prev_month}-"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                daily_keys.append(obj["Key"])
    print(f"Daily files found: {len(daily_keys)}")
    # No early exit when there are no daily files to roll up: the schema upgrade
    # + session-view backfill passes below still need to run.

    # Daily session-view files for that month (deleted after rollup)
    session_daily_keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"sessions/daily/{prev_month}-"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                session_daily_keys.append(obj["Key"])

    monthly_key = f"consolidated/monthly/{prev_month}.parquet"
    session_monthly_key = f"sessions/monthly/{prev_month}.parquet"

    con = connect()

    if daily_keys:
        con.execute(f"""
            COPY (
                -- union_by_name tolerates a mixed month: daily files written
                -- before the flatten landed have the legacy 5-column schema;
                -- matching by name back-fills the new columns as NULL instead
                -- of mis-mapping by position.
                SELECT * FROM read_parquet('s3://{BUCKET}/consolidated/daily/{prev_month}-*.parquet',
                                           union_by_name=true)
                ORDER BY ts
            ) TO 's3://{BUCKET}/{monthly_key}' (FORMAT PARQUET, COMPRESSION zstd)
        """)
        # Verify monthly file exists before deleting daily inputs
        s3.head_object(Bucket=BUCKET, Key=monthly_key)

        # Rebuild the session view over the whole month from the monthly
        # consolidated Parquet, so turn_idx is correct across day boundaries
        # (the daily views index within-day).
        build_session_view(con,
                           f"s3://{BUCKET}/{monthly_key}",
                           f"s3://{BUCKET}/{session_monthly_key}")
        s3.head_object(Bucket=BUCKET, Key=session_monthly_key)

        delete_batch = [{"Key": k} for k in daily_keys + session_daily_keys]
        for i in range(0, len(delete_batch), 1000):
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": delete_batch[i:i + 1000]})
        print(f"✓ Rolled up {len(daily_keys)} daily files → {monthly_key}; "
              f"session view → {session_monthly_key}; "
              f"{len(daily_keys) + len(session_daily_keys)} daily files removed.")

    # Schema-upgrade pass: bring any legacy-schema monthly consolidated file up
    # to the wide schema (lossless, idempotent), so consolidated/** is uniform
    # across daily + monthly tiers.
    upgraded = 0
    for m in sorted(_parquet_names("consolidated/monthly/")):
        if reflatten(con, f"s3://{BUCKET}/consolidated/monthly/{m}.parquet"):
            upgraded += 1
            print(f"  ⬆ re-flattened {m} to wide schema")
    print(f"Legacy monthly files upgraded to wide schema: {upgraded}")

    # Backfill: any monthly consolidated file lacking a monthly session view
    # (e.g. months rolled up before the session view existed).
    backfill = sorted(_parquet_names("consolidated/monthly/") - _parquet_names("sessions/monthly/"))
    print(f"Monthly session views to backfill ({len(backfill)}): {backfill}")
    for m in backfill:
        build_session_view(con,
                           f"s3://{BUCKET}/consolidated/monthly/{m}.parquet",
                           f"s3://{BUCKET}/sessions/monthly/{m}.parquet")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", choices=("daily", "monthly"), required=True)
    args = ap.parse_args()
    print(f"tier={args.tier} bucket={BUCKET} endpoint={ENDPOINT} url_style={URL_STYLE}")
    (run_daily if args.tier == "daily" else run_monthly)()
    print("Done.")


if __name__ == "__main__":
    main()
