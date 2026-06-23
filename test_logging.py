"""Tests for credential scrubbing and capture-mode logging in llm_proxy.

Run: python -m pytest test_logging.py   (or: python test_logging.py)

The credential-leak fixtures below mirror real values observed in the log
bucket (issue #24): the geo-agent `query` MCP tool passes s3_key/s3_secret in
tool-call arguments, which previously reached the logs verbatim.
"""
import importlib
import json
import os

import llm_proxy


def _reload(**env):
    """Reload llm_proxy with the given env so module-level config is recomputed."""
    for k, v in env.items():
        os.environ[k] = v
    return importlib.reload(llm_proxy)


def test_scrub_redacts_tool_call_arguments():
    p = importlib.reload(llm_proxy)
    args = json.dumps({
        "sql": "SELECT * FROM read_parquet('s3://x/y.parquet')",
        "s3_key": "NRQCS0986HNYNB0HFC50",
        "s3_secret": "7cGPYdNwp24S_IyUks8HVHydA5rwMu89UjEFQ_Am",
        "s3_endpoint": "minio.example.org",
    })
    scrubbed = json.loads(p._scrub(args, _key="arguments"))
    assert scrubbed["s3_key"] == "[REDACTED]"
    assert scrubbed["s3_secret"] == "[REDACTED]"
    assert scrubbed["s3_endpoint"] == "minio.example.org"   # not a secret
    assert scrubbed["sql"].startswith("SELECT")             # query preserved


def test_scrub_text_handles_embedded_and_escaped_secrets():
    p = importlib.reload(llm_proxy)
    raw = r'... \"s3_secret\": \"7cGPYdNwp24S_IyUks8HVHydA5rwMu89UjEFQ_Am\", ...'
    out = p._scrub_text(raw)
    assert "7cGPYdNwp24S" not in out
    assert "[REDACTED]" in out

    duck = "CREATE SECRET s (TYPE S3, KEY_ID 'AKIA123', SECRET 'topsecretvalue')"
    out2 = p._scrub_text(duck)
    assert "AKIA123" not in out2 and "topsecretvalue" not in out2

    auth = "Authorization: Bearer sk-ant-abc123XYZ"
    assert "sk-ant-abc123XYZ" not in p._scrub_text(auth)


def test_scrub_is_recursive_over_messages():
    p = importlib.reload(llm_proxy)
    msg = {"role": "tool", "content": json.dumps({"api_key": "leakme", "ok": 1})}
    scrubbed = p._scrub(msg)
    assert "leakme" not in json.dumps(scrubbed)


def test_response_content_not_truncated_and_scrubbed():
    p = _reload(LOG_CONTENT_MAX="0")
    p._log_buffer.clear()
    long_content = "x" * 5000 + " s3_secret: 7cGPYdNwp24S_IyUks8HVHydA5rwMu89"
    resp = {"choices": [{"message": {"content": long_content}}]}
    p.log_response("nrp", "qwen3", resp, 10, request_id="abc")
    entry = p._log_buffer[-1]
    assert len(entry["content"]) >= 5000          # not clipped to 200
    assert "7cGPYdNwp24S" not in entry["content"]  # secret scrubbed
    assert len(entry["content_preview"]) <= 200    # preview still capped


def test_reasoning_capped_independently_of_content():
    # Middle ground: full final answer + full tool calls, but bounded reasoning.
    p = _reload(LOG_CONTENT_MAX="0", LOG_REASONING_MAX="100")
    p._log_buffer.clear()
    resp = {"choices": [{"message": {
        "content": "F" * 5000,
        "reasoning_content": "R" * 5000,
        "tool_calls": [{"function": {"name": "query", "arguments": '{"sql":"' + "S" * 5000 + '"}'}}],
    }}]}
    p.log_response("nrp", "qwen3", resp, 10, request_id="abc")
    entry = p._log_buffer[-1]
    assert len(entry["content"]) >= 5000              # final answer kept in full
    assert len(entry["reasoning_content"]) == 100     # reasoning trace bounded
    assert len(entry["reasoning_content_preview"]) <= 200
    assert len(entry["tool_calls"][0]["arguments"]) >= 5000   # tool call NOT capped


def test_summary_mode_omits_messages():
    p = _reload(LOG_CAPTURE_MODE="summary")
    p._log_buffer.clear()
    p.log_request("nrp", "qwen3", [{"role": "user", "content": "hi"}], request_id="r1")
    assert "messages" not in p._log_buffer[-1]


def test_full_mode_captures_and_dedups_system_prompt():
    p = _reload(LOG_CAPTURE_MODE="full")
    p._log_buffer.clear()
    p._seen_system_hashes.clear()
    big_system = "You are a geo agent. " * 2000
    msgs = [
        {"role": "system", "content": big_system},
        {"role": "user", "content": "How many acres?"},
        {"role": "tool", "content": json.dumps({"s3_secret": "leakme123"})},
    ]
    p.log_request("nrp", "qwen3", msgs, request_id="r1")

    sysprompt_entries = [e for e in p._log_buffer if e["type"] == "system_prompt"]
    assert len(sysprompt_entries) == 1                     # logged once, in full
    req = [e for e in p._log_buffer if e["type"] == "request"][-1]
    sys_ref = req["messages"][0]
    assert sys_ref["_dedup"] is True and "content" not in sys_ref
    assert sys_ref["system_sha256"] == sysprompt_entries[0]["system_sha256"]
    assert "leakme123" not in json.dumps(req["messages"])  # tool args scrubbed

    # Second turn with same system prompt must NOT re-log the body.
    p.log_request("nrp", "qwen3", msgs, request_id="r2")
    assert len([e for e in p._log_buffer if e["type"] == "system_prompt"]) == 1


def test_stdout_compacted_while_s3_record_stays_full(capsys=None):
    # #2: kubectl/stdout must stay readable; full fidelity goes to the S3 buffer.
    p = _reload(LOG_CAPTURE_MODE="full")
    p._S3_ENABLED = True            # pretend S3 is the durable sink
    p._log_buffer.clear()
    p._seen_system_hashes.clear()
    import io
    from contextlib import redirect_stdout
    msgs = [
        {"role": "system", "content": "SYS " * 5000},
        {"role": "user", "content": "Q " * 3000},
    ]
    buf = io.StringIO()
    with redirect_stdout(buf):
        p.log_request("nrp", "qwen3", msgs, request_id="r1")
    printed = buf.getvalue()
    # stdout line is small and omits the full messages array...
    req_line = [l for l in printed.splitlines() if l.startswith("📥 REQUEST")][0]
    assert '"messages"' not in req_line
    assert len(req_line) < 2000
    # ...but the buffered record (-> S3) keeps the full messages array.
    req = [e for e in p._log_buffer if e["type"] == "request"][-1]
    assert "messages" in req and len(req["messages"]) == 2


def test_stdout_full_when_s3_disabled():
    p = _reload(LOG_CAPTURE_MODE="summary")
    p._S3_ENABLED = False           # stdout is the only sink -> must be complete
    p._log_buffer.clear()
    import io
    from contextlib import redirect_stdout
    resp = {"choices": [{"message": {"content": "Z" * 1000}}]}
    buf = io.StringIO()
    with redirect_stdout(buf):
        p.log_response("nrp", "qwen3", resp, 10, request_id="r1")
    assert "Z" * 1000 in buf.getvalue()   # full content present on stdout


def test_logging_never_raises_on_bad_input():
    # A logging failure must never propagate into request serving (#1).
    p = importlib.reload(llm_proxy)

    class Boom:
        def __getitem__(self, k):  # not JSON-serialisable / explodes on access
            raise RuntimeError("boom")

    # Should swallow the error and return None rather than raising.
    assert p.log_response("nrp", "qwen3", {"choices": [{"message": Boom()}]}, 10) is None
    assert p.log_request("nrp", "qwen3", Boom()) is None


def test_scrub_entry_idempotent_and_detectable():
    # Powers the historical scrub job; must be lossless-idempotent + verifiable.
    import scrub
    leak = json.dumps({
        "type": "response",
        "tool_calls": [{"name": "query", "arguments": json.dumps({
            "sql": "SELECT 1", "s3_key": "NRQCS0986HNYNB0HFC50",
            "s3_secret": "7cGPYdNwp24S_IyUks8HVHydA5rwMu89UjEFQ_Am"})}],
    })
    assert scrub.contains_secret(leak)
    once = scrub.scrub_entry(leak)
    assert "7cGPYdNwp24S" not in once and "NRQCS0986HNYNB0HFC50" not in once
    assert scrub.scrub_entry(once) == once          # idempotent
    # A clean record round-trips with no semantic change.
    clean = json.dumps({"type": "request", "user_question": "How many acres?"})
    assert json.loads(scrub.scrub_entry(clean)) == json.loads(clean)


def test_flush_failure_requeues_batch():
    """A failed S3 write must re-queue the batch, not drop it (issue #27)."""
    import asyncio
    p = _reload(AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="y")
    p._log_buffer[:] = [{"type": "request", "n": i} for i in range(3)]

    class _Boom:
        def put_object(self, **kw):
            raise RuntimeError("simulated S3 5xx")

    import boto3
    orig = boto3.client
    boto3.client = lambda *a, **k: _Boom()
    try:
        asyncio.run(p._flush_to_s3())
    finally:
        boto3.client = orig

    # Entries must survive the failed flush for retry, in original order.
    assert [e["n"] for e in p._log_buffer] == [0, 1, 2]


def test_flush_failure_caps_buffer_dropping_oldest():
    """A prolonged outage must bound the buffer, dropping oldest (issue #27)."""
    import asyncio
    p = _reload(AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="y", LOG_MAX_BUFFER="5")
    p._log_buffer[:] = [{"type": "request", "n": i} for i in range(8)]

    class _Boom:
        def put_object(self, **kw):
            raise RuntimeError("simulated S3 outage")

    import boto3
    orig = boto3.client
    boto3.client = lambda *a, **k: _Boom()
    try:
        asyncio.run(p._flush_to_s3())
    finally:
        boto3.client = orig

    # Capped to 5: oldest (0,1,2) dropped, newest retained.
    assert [e["n"] for e in p._log_buffer] == [3, 4, 5, 6, 7]


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
