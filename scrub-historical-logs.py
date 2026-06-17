#!/usr/bin/env python3
"""One-off: scrub credentials from already-written logs in S3.

The live proxy only scrubs records written *after* the training-grade-logging
change (#24). Records written before it still contain real s3_key/s3_secret
values (observed: ~184 entries in consolidated Parquet). This job rewrites those
historical objects in place using the *same* scrubber as the live proxy
(scrub.py), so the corpus is safe to share/train on.

Scope (newest-leak-free-first): consolidated Parquet (daily + monthly) and any
raw JSONL day prefixes that haven't been consolidated yet. Today's prefix is
skipped (it's actively being written, and new writes are already scrubbed).

Safety / idempotency:
  - A file is only rewritten if scrubbing actually changes a byte; re-runs on
    clean files are no-ops (the redaction marker contains no secret pattern).
  - Parquet rewrites go to a temp key, are row-count-verified, then atomically
    copied over the original; the temp key is deleted. Original schema
    (ts TIMESTAMPTZ, type, request_id, origin, entry) is preserved exactly —
    only the `entry` column is scrubbed; the derived columns are kept as-is.
  - --dry-run reports what would change and writes nothing.
  - --verify re-scans every object afterward and exits non-zero if any
    credential pattern still matches.

Connection mirrors the consolidation CronJob: internal Ceph endpoint + the
namespace `aws` secret in-cluster, or LOG_S3_KEY/LOG_S3_SECRET against the
public endpoint locally. Override via S3_ENDPOINT / S3_USE_SSL.

Usage:
  python scrub-historical-logs.py --dry-run          # report only
  python scrub-historical-logs.py                    # scrub Parquet + JSONL
  python scrub-historical-logs.py --parquet-only --verify
"""
import argparse
import datetime
import io
import json
import os
import sys

import boto3
import duckdb
from botocore.client import Config

import scrub

BUCKET = os.getenv("LOG_BUCKET", "logs-open-llm-proxy")
# In-cluster default (fast, no throttling); override for local/public runs.
ENDPOINT = os.getenv("S3_ENDPOINT", "http://rook-ceph-rgw-nautiluss3.rook")
USE_SSL = os.getenv("S3_USE_SSL", "false").lower() in ("1", "true", "yes")
KEY_ID = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("LOG_S3_KEY")
SECRET = os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("LOG_S3_SECRET")


def _s3():
    return boto3.client(
        "s3", endpoint_url=ENDPOINT,
        aws_access_key_id=KEY_ID, aws_secret_access_key=SECRET,
        config=Config(s3={"addressing_style": "path"}),
    )


def _duck():
    con = duckdb.connect()
    # httpfs secret so read_parquet/COPY can hit S3 directly.
    host = ENDPOINT.split("://", 1)[-1]
    con.execute(f"""
        CREATE SECRET s3_logs (TYPE S3, KEY_ID '{KEY_ID}', SECRET '{SECRET}',
            ENDPOINT '{host}', USE_SSL {str(USE_SSL).lower()}, URL_STYLE 'path')
    """)
    return con


def _list(s3, prefix, suffix):
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(suffix):
                keys.append(obj["Key"])
    return keys


def scrub_parquet(con, s3, key, dry_run):
    """Returns (rows, changed_rows, rewrote)."""
    rows = con.execute(
        f"SELECT ts, type, request_id, origin, entry FROM read_parquet('s3://{BUCKET}/{key}')"
    ).fetchall()
    new_rows, changed = [], 0
    for ts, type_, rid, origin, entry in rows:
        scrubbed = scrub.scrub_entry(entry) if entry is not None else entry
        if scrubbed != entry:
            changed += 1
        new_rows.append((ts, type_, rid, origin, scrubbed))
    if changed == 0:
        return len(rows), 0, False
    if dry_run:
        return len(rows), changed, False

    tmp = key + ".scrubbing.tmp.parquet"
    con.execute("CREATE OR REPLACE TABLE _t (ts TIMESTAMPTZ, type VARCHAR, "
                "request_id VARCHAR, origin VARCHAR, entry VARCHAR)")
    con.executemany("INSERT INTO _t VALUES (?,?,?,?,?)", new_rows)
    con.execute(f"COPY (SELECT * FROM _t ORDER BY ts) TO 's3://{BUCKET}/{tmp}' "
                f"(FORMAT PARQUET, COMPRESSION zstd)")
    # Verify the temp file is readable and row-count matches before overwriting.
    n = con.execute(f"SELECT count(*) FROM read_parquet('s3://{BUCKET}/{tmp}')").fetchone()[0]
    if n != len(new_rows):
        raise RuntimeError(f"{key}: temp row count {n} != {len(new_rows)} — aborting, original untouched")
    s3.copy_object(Bucket=BUCKET, CopySource={"Bucket": BUCKET, "Key": tmp}, Key=key)
    s3.delete_object(Bucket=BUCKET, Key=tmp)
    return len(rows), changed, True


def scrub_jsonl(s3, key, dry_run):
    """Returns (rows, changed_rows, rewrote)."""
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    out, changed = [], 0
    for line in body.splitlines():
        if not line.strip():
            continue
        scrubbed = scrub.scrub_entry(line)
        if scrubbed != line:
            changed += 1
        out.append(scrubbed)
    if changed == 0:
        return len(out), 0, False
    if not dry_run:
        s3.put_object(Bucket=BUCKET, Key=key,
                      Body=("\n".join(out) + "\n").encode("utf-8"))
    return len(out), changed, not dry_run


def verify(con, s3, parquet_keys, jsonl_keys):
    """Re-scan everything; return count of objects still containing a secret."""
    bad = 0
    for key in parquet_keys:
        hits = con.execute(
            f"SELECT count(*) FROM read_parquet('s3://{BUCKET}/{key}') "
            f"WHERE regexp_matches(entry, '(?i)(KEY_ID|SECRET)\\s+''|Bearer\\s+\\S|"
            f"(s3[_-]?secret|s3[_-]?key|api[_-]?key|password|token)[\"'' ]?\\s*[:=]')"
        ).fetchone()[0]
        if hits:
            bad += 1
            print(f"  ✗ {key}: {hits} rows still match a secret pattern")
    for key in jsonl_keys:
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
        if any(scrub.contains_secret(l) for l in body.splitlines()):
            bad += 1
            print(f"  ✗ {key}: still matches a secret pattern")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    ap.add_argument("--parquet-only", action="store_true", help="skip raw JSONL day prefixes")
    ap.add_argument("--verify", action="store_true", help="re-scan for residual secrets after scrubbing")
    args = ap.parse_args()

    if not (KEY_ID and SECRET):
        sys.exit("No S3 credentials: set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY "
                 "(in-cluster) or LOG_S3_KEY/LOG_S3_SECRET (local).")

    s3 = _s3()
    con = _duck()
    today = datetime.datetime.utcnow().date().isoformat()

    parquet_keys = (_list(s3, "consolidated/daily/", ".parquet")
                    + _list(s3, "consolidated/monthly/", ".parquet"))

    jsonl_keys = []
    if not args.parquet_only:
        # Raw JSONL lives under YYYY-MM-DD/ prefixes; skip today's (live writes).
        prefixes = set()
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Delimiter="/"):
            for p in page.get("CommonPrefixes", []):
                pref = p["Prefix"].rstrip("/")
                try:
                    datetime.date.fromisoformat(pref)
                    if pref != today:
                        prefixes.add(pref)
                except ValueError:
                    pass
        for pref in sorted(prefixes):
            jsonl_keys += _list(s3, pref + "/", ".jsonl")

    mode = "DRY-RUN" if args.dry_run else "SCRUB"
    print(f"[{mode}] bucket={BUCKET} endpoint={ENDPOINT}")
    print(f"  {len(parquet_keys)} Parquet + {len(jsonl_keys)} JSONL objects to scan\n")

    tot_rows = tot_changed = tot_files = 0
    for key in parquet_keys:
        rows, changed, rewrote = scrub_parquet(con, s3, key, args.dry_run)
        tot_rows += rows; tot_changed += changed
        if changed:
            tot_files += 1
            verb = "would scrub" if args.dry_run else ("✓ scrubbed" if rewrote else "?")
            print(f"  [parquet] {key}: {verb} {changed}/{rows} rows")
    for key in jsonl_keys:
        rows, changed, rewrote = scrub_jsonl(s3, key, args.dry_run)
        tot_rows += rows; tot_changed += changed
        if changed:
            tot_files += 1
            verb = "would scrub" if args.dry_run else ("✓ scrubbed" if rewrote else "?")
            print(f"  [jsonl]   {key}: {verb} {changed}/{rows} rows")

    print(f"\n{mode} complete: {tot_changed} rows in {tot_files} files "
          f"(of {tot_rows} rows scanned).")

    if args.verify and not args.dry_run:
        print("\nVerifying no residual secrets...")
        bad = verify(con, s3, parquet_keys, jsonl_keys)
        if bad:
            sys.exit(f"VERIFY FAILED: {bad} object(s) still contain a secret pattern.")
        print("VERIFY OK: no credential patterns remain.")


if __name__ == "__main__":
    main()
