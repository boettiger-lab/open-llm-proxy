"""Tests for the consolidation SQL builders (consolidate.py, issue #31).

Exercises the pure flatten_select / session_view SQL against synthetic fixtures
and (when present) a locally-synced copy of the real corpus, so the flattened
schema and session-view shape are pinned without needing S3.

Runnable standalone (`python test_consolidate.py`, the CI entrypoint) or under
pytest — same dual style as test_logging.py. Uses only duckdb (no pandas/pytest).
"""
import json
import os

import duckdb

import consolidate

LOCAL_LOGS = "/tmp/open-llm-proxy-logs"

EXPECTED_FLAT_COLS = {
    "ts", "type", "request_id", "origin", "session_id", "client", "model",
    "provider", "message_count", "tools_count", "user_question", "latency_ms",
    "has_content", "has_tool_calls", "has_reasoning_content", "total_tokens",
    "tool_calls", "tool_results", "tokens", "error", "entry",
}
EXPECTED_SESSION_COLS = {
    "session_key", "session_id", "turn_idx", "ts", "origin", "model", "provider",
    "client", "message_count", "user_question", "latest_user_message",
    "assistant_text", "tool_calls", "tool_results", "has_content",
    "has_tool_calls", "latency_ms", "total_tokens", "error", "request_id",
}


def _rel(con, records):
    """Register a relation exposing `entry` VARCHAR, one row per record dict."""
    con.execute("CREATE TABLE raw(entry VARCHAR)")
    con.executemany("INSERT INTO raw VALUES (?)", [[json.dumps(r)] for r in records])
    return "raw"


def _rows(con, sql):
    """Run sql, return (column_names_set, list_of_dict_rows)."""
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return set(cols), [dict(zip(cols, r)) for r in cur.fetchall()]


def test_flatten_schema_and_types():
    con = duckdb.connect()
    rel = _rel(con, [
        {"timestamp": "2026-06-01T00:00:00+00:00", "type": "request",
         "request_id": "abc", "origin": "o", "session_id": "s1", "model": "qwen3",
         "provider": "nrp", "message_count": 2, "tools_count": 9,
         "user_question": "hello?", "tool_results_this_turn": None},
        {"timestamp": "2026-06-01T00:00:01+00:00", "type": "response",
         "request_id": "abc", "origin": "o", "session_id": "s1", "model": "qwen3",
         "provider": "nrp", "latency_ms": 1234, "has_content": True,
         "has_tool_calls": True, "tokens": {"total_tokens": 50},
         "tool_calls": [{"name": "query", "arguments": {"sql": "SELECT 1"}}]},
    ])
    cols, rows = _rows(con, consolidate.flatten_select(rel))
    assert cols == EXPECTED_FLAT_COLS, cols ^ EXPECTED_FLAT_COLS
    req = next(r for r in rows if r["type"] == "request")
    resp = next(r for r in rows if r["type"] == "response")
    assert req["message_count"] == 2 and req["tools_count"] == 9
    assert resp["latency_ms"] == 1234 and resp["has_tool_calls"] is True
    assert resp["total_tokens"] == 50
    assert json.loads(req["entry"])["user_question"] == "hello?"


def test_session_view_pairs_and_orders_turns():
    con = duckdb.connect()
    rel = _rel(con, [
        {"timestamp": "2026-06-01T00:00:00+00:00", "type": "request", "request_id": "r1",
         "origin": "o", "session_id": "S", "user_question": "open Q", "message_count": 2},
        {"timestamp": "2026-06-01T00:00:01+00:00", "type": "response", "request_id": "r1",
         "origin": "o", "session_id": "S", "content": "thinking",
         "tool_calls": [{"name": "list_datasets", "arguments": {}}]},
        {"timestamp": "2026-06-01T00:00:05+00:00", "type": "request", "request_id": "r2",
         "origin": "o", "session_id": "S", "user_question": "open Q", "message_count": 4,
         "tool_results_this_turn": [{"tool_call_id": "t", "content": "[...]"}]},
        {"timestamp": "2026-06-01T00:00:06+00:00", "type": "response", "request_id": "r2",
         "origin": "o", "session_id": "S", "content_preview": "final answer"},
    ])
    cols, rows = _rows(con, consolidate.session_view(consolidate.flatten_select(rel)))
    assert cols == EXPECTED_SESSION_COLS, cols ^ EXPECTED_SESSION_COLS
    assert [r["turn_idx"] for r in rows] == [1, 2]
    assert {r["session_key"] for r in rows} == {"S"}
    t1, t2 = rows
    assert t1["assistant_text"] == "thinking"          # full content preferred
    assert t2["assistant_text"] == "final answer"      # falls back to preview
    assert json.loads(t1["tool_calls"])[0]["name"] == "list_datasets"
    assert json.loads(t2["tool_results"])[0]["content"] == "[...]"


def test_session_key_heuristic_fallback_when_session_id_null():
    con = duckdb.connect()
    rel = _rel(con, [
        {"timestamp": "2026-06-01T00:00:00+00:00", "type": "request", "request_id": "r1",
         "origin": "app-a", "user_question": "Q1"},
        {"timestamp": "2026-06-01T00:00:01+00:00", "type": "request", "request_id": "r2",
         "origin": "app-a", "user_question": "Q2"},
    ])
    _, rows = _rows(con, consolidate.session_view(consolidate.flatten_select(rel)))
    assert {r["session_key"] for r in rows} == {"app-a|Q1", "app-a|Q2"}
    assert all(r["session_id"] is None for r in rows)


def test_latest_user_message_full_mode_only():
    con = duckdb.connect()
    rel = _rel(con, [
        {"timestamp": "2026-06-01T00:00:00+00:00", "type": "request", "request_id": "r1",
         "origin": "o", "session_id": "S", "user_question": "first Q", "messages": [
             {"role": "system", "content": "sys"},
             {"role": "user", "content": "first Q"},
             {"role": "assistant", "content": "a"},
             {"role": "user", "content": "follow up Q"}]},
        {"timestamp": "2026-06-02T00:00:00+00:00", "type": "request", "request_id": "r2",
         "origin": "o", "session_id": "T", "user_question": "lone Q"},
    ])
    _, rows = _rows(con, consolidate.session_view(consolidate.flatten_select(rel)))
    by_key = {r["session_key"]: r for r in rows}
    assert by_key["S"]["latest_user_message"] == "follow up Q"
    assert by_key["T"]["latest_user_message"] is None


def test_against_real_corpus_if_synced():
    """Flatten + session view run cleanly over the real consolidated Parquet and
    preserve row counts, with dense 1-based turn_idx per session. Skips when no
    local corpus is synced (e.g. in CI)."""
    if not os.path.isdir(os.path.join(LOCAL_LOGS, "consolidated")):
        print("  (skipped: no locally-synced corpus — run ./sync-logs.sh)")
        return
    con = duckdb.connect()
    src = f"read_parquet('{LOCAL_LOGS}/consolidated/**/*.parquet')"
    n_raw = con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
    flat_sql = consolidate.flatten_select(src)
    assert con.execute(f"SELECT count(*) FROM ({flat_sql})").fetchone()[0] == n_raw
    cols, _ = _rows(con, consolidate.session_view(flat_sql) + " LIMIT 0")
    assert cols == EXPECTED_SESSION_COLS, cols ^ EXPECTED_SESSION_COLS
    con.execute("CREATE VIEW sv AS " + consolidate.session_view(flat_sql))
    n_req = con.execute(f"SELECT count(*) FROM ({flat_sql}) WHERE type='request'").fetchone()[0]
    assert con.execute("SELECT count(*) FROM sv").fetchone()[0] == n_req
    bad = con.execute(
        "SELECT count(*) FROM (SELECT session_key, min(turn_idx) lo, max(turn_idx) hi, "
        "count(*) n FROM sv GROUP BY 1) WHERE lo != 1 OR hi != n"
    ).fetchone()[0]
    assert bad == 0


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
