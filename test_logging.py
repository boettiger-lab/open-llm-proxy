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


def test_compute_valid_keys_multi_and_backward_compat():
    """Multi-key auth (#): PROXY_KEY plus comma-separated PROXY_KEYS_EXTRA are all
    accepted; blanks dropped; single-key setup is unchanged."""
    f = llm_proxy.compute_valid_keys
    # backward compatible: no extras -> exactly the primary
    assert f("prod", "") == frozenset({"prod"})
    assert f("prod", None) == frozenset({"prod"})
    # extras accepted alongside the primary
    assert f("prod", "eval1,eval2") == frozenset({"prod", "eval1", "eval2"})
    # whitespace and empty entries are dropped (no accidental "" accept-all)
    assert f("prod", " eval1 , , eval2 ,") == frozenset({"prod", "eval1", "eval2"})
    assert "" not in f("prod", ",, ,")
    # a random key is NOT in the set (the property the auth check relies on)
    assert "attacker" not in f("prod", "eval1")


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


def test_reasoning_field_fallback_for_nimbus():
    # #66: NRP emits `reasoning_content`; the nimbus vLLM endpoint emits `reasoning`.
    # Both must set has_reasoning_content and land in the reasoning_content column.
    p = _reload(LOG_CONTENT_MAX="0")
    # nimbus-style: trace under `reasoning`, no `reasoning_content`
    p._log_buffer.clear()
    p.log_response("nimbus", "qwen",
                   {"choices": [{"message": {"content": "answer", "reasoning": "R" * 300}}]},
                   10, request_id="n1")
    e = p._log_buffer[-1]
    assert e["has_reasoning_content"] is True
    assert e["reasoning_content"].startswith("R")
    # NRP-style still works, and reasoning_content wins if both are present
    p._log_buffer.clear()
    p.log_response("nrp", "qwen3",
                   {"choices": [{"message": {"content": "answer",
                                             "reasoning_content": "PREFERRED", "reasoning": "IGNORED"}}]},
                   10, request_id="r1")
    e = p._log_buffer[-1]
    assert e["has_reasoning_content"] is True
    assert e["reasoning_content"] == "PREFERRED"
    # No reasoning at all → flag false
    p._log_buffer.clear()
    p.log_response("nimbus", "qwen",
                   {"choices": [{"message": {"content": "answer"}}]}, 10, request_id="p1")
    assert p._log_buffer[-1]["has_reasoning_content"] is False


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


def test_user_message_this_turn_is_latest_not_opener():
    # #89: session_id persists across a whole browsing day, so `user_question`
    # (the FIRST user message) is only the session opener. `user_message_this_turn`
    # must carry the LAST user message so distinct mid-session requests are
    # countable and the actual triggering prompt is recoverable.
    p = _reload(LOG_CAPTURE_MODE="summary")
    p._log_buffer.clear()
    msgs = [
        {"role": "user", "content": "show 3d hex of US vulnerable carbon"},  # opener
        {"role": "assistant", "content": "..."},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "now re-render on a log scale"},         # this turn
    ]
    p.log_request("nrp", "qwen3", msgs, request_id="r1")
    entry = p._log_buffer[-1]
    assert entry["user_question"] == "show 3d hex of US vulnerable carbon"
    assert entry["user_message_this_turn"] == "now re-render on a log scale"


def test_user_message_this_turn_equals_opener_on_first_turn():
    # On a single-message first turn the two fields coincide (no regression for
    # the common one-shot case).
    p = _reload(LOG_CAPTURE_MODE="summary")
    p._log_buffer.clear()
    p.log_request("nrp", "qwen3", [{"role": "user", "content": "hi"}], request_id="r1")
    entry = p._log_buffer[-1]
    assert entry["user_question"] == "hi"
    assert entry["user_message_this_turn"] == "hi"


def test_request_logs_requested_enable_thinking():
    # #64: the requested thinking mode must be logged so it can be told apart from
    # observed reasoning after the fact. None (not sent) / True / False all round-trip,
    # and None serializes to JSON null rather than being dropped.
    p = _reload(LOG_CAPTURE_MODE="summary")
    for req_id, requested in (("r-none", None), ("r-on", True), ("r-off", False)):
        p._log_buffer.clear()
        p.log_request("nrp", "qwen3", [{"role": "user", "content": "hi"}],
                      request_id=req_id, enable_thinking=requested)
        entry = p._log_buffer[-1]
        assert entry["enable_thinking"] is requested
        # must survive JSON serialization as an explicit value (null, not absent)
        assert json.loads(json.dumps(entry))["enable_thinking"] == requested


def test_request_enable_thinking_defaults_to_none():
    # Callers that don't pass the flag (e.g. non-thinking clients) log null.
    p = _reload(LOG_CAPTURE_MODE="summary")
    p._log_buffer.clear()
    p.log_request("nrp", "qwen3", [{"role": "user", "content": "hi"}], request_id="r1")
    assert p._log_buffer[-1]["enable_thinking"] is None


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


def test_chat_request_parses_user_as_session_id_source():
    """The OpenAI `user` body field must survive pydantic parsing — geo-agent
    sends its per-session UUID there and the endpoint logs it as session_id."""
    p = importlib.reload(llm_proxy)
    req = p.ChatRequest(
        messages=[{"role": "user", "content": "hi"}],
        model="qwen3",
        user="b1c2d3e4-0000-4444-8888-abcdef012345",
    )
    assert req.user == "b1c2d3e4-0000-4444-8888-abcdef012345"
    # session_id resolution precedence: body `user` wins, header is the fallback.
    assert (req.user or "from-header") == "b1c2d3e4-0000-4444-8888-abcdef012345"
    assert (p.ChatRequest(messages=[], model="qwen3").user or "from-header") == "from-header"


def test_handler_emits_response_to_buffer_with_serializable_client():
    """Regression for #37: a successful turn must enqueue a `type: "response"`
    entry to the S3 buffer, and its `client` must be the X-Client *string* — not
    the httpx AsyncClient. The bug was `async with httpx.AsyncClient() as client`
    shadowing the `client` header param, so json.dumps blew up inside the
    `@_never_raises`-wrapped log_response and every response was silently dropped.

    Driving the handler (not log_response directly) is what catches it — the
    defect was at the call site, not in the function. The mocked AsyncClient is
    itself non-serializable, so pre-fix this test fails (no response buffered);
    post-fix `client` stays the header string and the entry is serializable."""
    import asyncio
    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey")
    p._log_buffer.clear()

    class _FakeResp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {
                        "content": "hello",
                        "tool_calls": [{"function": {"name": "query", "arguments": "{}"}}]}}],
                    "usage": {"total_tokens": 5}}

    class _FakeAsyncClient:  # non-serializable on purpose (mirrors httpx.AsyncClient)
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return _FakeResp()

    class _FakeRequest:
        headers = {"origin": "https://app", "x-client": "geo-agent/v9.9.9"}

    req = p.ChatRequest(model="qwen3", messages=[{"role": "user", "content": "hi"}], user="sess-1")
    with patch.object(p, "get_provider_for_model",
                      return_value=("nrp", {"endpoint": "http://upstream", "api_key": "k"})), \
         patch.object(p.httpx, "AsyncClient", _FakeAsyncClient):
        result = asyncio.run(p.proxy_chat(req, _FakeRequest(), authorization="Bearer testkey"))

    assert result["choices"][0]["message"]["content"] == "hello"
    responses = [e for e in p._log_buffer if e.get("type") == "response"]
    assert len(responses) == 1, "response was dropped from the S3 buffer (#37)"
    assert responses[0]["client"] == "geo-agent/v9.9.9"   # the header string, not an AsyncClient
    assert responses[0]["session_id"] == "sess-1"
    assert responses[0]["has_tool_calls"] is True
    json.dumps(responses[0])   # must be JSON-serializable — the crux of the bug


def _run_proxy_capture(req, provider=("nrp", {"endpoint": "http://upstream", "api_key": "k"})):
    """Drive proxy_chat with a fake client that records the forwarded payload."""
    import asyncio
    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey", CACHE_SALT="")
    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, endpoint, json=None, headers=None):
            captured["payload"] = json
            return _FakeResp()

    class _FakeRequest:
        headers = {"origin": "https://app"}

    cr = p.ChatRequest(**req)
    with patch.object(p, "get_provider_for_model", return_value=provider), \
         patch.object(p.httpx, "AsyncClient", _FakeAsyncClient):
        asyncio.run(p.proxy_chat(cr, _FakeRequest(), authorization="Bearer testkey"))
    return captured["payload"]


def test_error_path_captures_allowlisted_upstream_headers():
    """On the HTTPStatusError path, allow-listed upstream headers land in the
    buffered error response so 429-throttle vs naked-500 is queryable (#44)."""
    import asyncio
    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey")
    p._log_buffer.clear()

    # Mirror NRP's dead-backend signature: 500, empty body, content-length 0,
    # plus a couple of allow-listed correlation/rate-limit headers and one
    # disallowed header that must NOT be captured.
    upstream = p.httpx.Response(
        status_code=500,
        headers={"content-length": "0", "retry-after": "30",
                 "x-request-id": "abc123", "x-secret-internal": "leak-me"},
        request=p.httpx.Request("POST", "http://upstream"),
    )

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return upstream  # raise_for_status() below turns 500 into the error

    class _FakeRequest:
        headers = {"origin": "https://app"}

    req = p.ChatRequest(model="qwen3", messages=[{"role": "user", "content": "hi"}])
    with patch.object(p, "get_provider_for_model",
                      return_value=("nrp", {"endpoint": "http://upstream", "api_key": "k"})), \
         patch.object(p.httpx, "AsyncClient", _FakeAsyncClient):
        try:
            asyncio.run(p.proxy_chat(req, _FakeRequest(), authorization="Bearer testkey"))
            assert False, "expected HTTPException on upstream 500"
        except p.HTTPException:
            pass

    errs = [e for e in p._log_buffer if e.get("type") == "response" and e.get("error")]
    assert len(errs) == 1
    hdrs = errs[0]["upstream_headers"]
    assert hdrs["content-length"] == "0"
    assert hdrs["retry-after"] == "30"
    assert hdrs["x-request-id"] == "abc123"
    assert "x-secret-internal" not in hdrs   # allow-list only
    json.dumps(errs[0])   # must stay serializable


def test_slow_completion_warn_threshold_is_configurable_and_message_agrees():
    """The `⚠️ Slow completion` threshold is SLOW_COMPLETION_WARN_SECONDS, and the
    printed message reports the *configured* value rather than a separate literal.

    The threshold and the message used to be independent (`_CLIENT_NGINX_...= 300_000`
    vs a hardcoded "300s" in the f-string), so changing one silently made the other
    lie. That mattered because the value it named was already stale: it claimed every
    caller's nginx sidecar was 300s, while ca-30x30's is 600s.
    """
    import os as _os

    # Non-default value: threshold moves and the message follows it.
    p = _reload(SLOW_COMPLETION_WARN_SECONDS="600", PROXY_KEY="testkey")
    assert p._SLOW_COMPLETION_WARN_SECONDS == 600.0
    assert p._SLOW_COMPLETION_WARN_MS == 600_000

    # A reporting knob must never crash the proxy at import.
    for bad in ("", "   ", "abc"):
        p = _reload(SLOW_COMPLETION_WARN_SECONDS=bad, PROXY_KEY="testkey")
        assert p._SLOW_COMPLETION_WARN_SECONDS == 300.0, bad

    # Unset -> documented default of 300s (tightest known sidecar).
    _os.environ.pop("SLOW_COMPLETION_WARN_SECONDS", None)
    p = _reload(PROXY_KEY="testkey")
    assert p._SLOW_COMPLETION_WARN_SECONDS == 300.0
    assert p._SLOW_COMPLETION_WARN_MS == 300_000

    _os.environ.pop("SLOW_COMPLETION_WARN_SECONDS", None)


def test_slow_completion_warning_reports_the_configured_threshold(capsys):
    """Drive a real completion past the threshold and assert on what actually prints.

    The threshold and the printed value used to be independent, so this asserts on
    captured stdout from the real code path — not a string the test rebuilt itself,
    which would pass no matter what `llm_proxy` printed.
    """
    import asyncio, os as _os
    from unittest.mock import patch

    class _FakeResponse:
        status_code = 200
        headers = {}
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            # Non-zero so latency_ms > 0: the comparison is strictly `>`, so a 0ms
            # call would not trip even a 0s threshold.
            await asyncio.sleep(0.05)
            return _FakeResponse()

    class _FakeRequest:
        headers = {"origin": "https://app"}

    def _run(threshold):
        p = _reload(SLOW_COMPLETION_WARN_SECONDS=threshold, PROXY_KEY="testkey")
        p._log_buffer.clear()
        req = p.ChatRequest(model="qwen3", messages=[{"role": "user", "content": "hi"}])
        with patch.object(p, "get_provider_for_model",
                          return_value=("nrp", {"endpoint": "http://u", "api_key": "k"})), \
             patch.object(p.httpx, "AsyncClient", _FakeAsyncClient):
            asyncio.run(p.proxy_chat(req, _FakeRequest(), authorization="Bearer testkey"))
        return capsys.readouterr().out

    # Threshold 0 => this (fast) call is "slow", and the line names 0s, not 300s.
    out = _run("0")
    assert "Slow completion" in out
    assert "exceeds the 0s client-side warning threshold" in out
    assert "300s" not in out          # the old hardcoded literal must be gone

    # A fractional threshold is rendered without float noise.
    out = _run("0.01")
    assert "exceeds the 0.01s client-side warning threshold" in out

    # A high threshold => no warning at all for a fast call.
    out = _run("600")
    assert "Slow completion" not in out

    _os.environ.pop("SLOW_COMPLETION_WARN_SECONDS", None)


def test_client_disconnect_is_logged_and_reraised():
    """When the caller (nginx sidecar, 300s read timeout) drops the connection and
    uvicorn cancels the handler mid-upstream, we log a response row with a
    disconnect error AND re-raise CancelledError — so the previously-invisible
    client-facing nginx 502s become observable in the logs (#82)."""
    import asyncio
    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey")
    p._log_buffer.clear()

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            raise asyncio.CancelledError()  # client went away while we awaited upstream

    class _FakeRequest:
        headers = {"origin": "https://app"}

    req = p.ChatRequest(model="qwen3", messages=[{"role": "user", "content": "hi"}])
    with patch.object(p, "get_provider_for_model",
                      return_value=("nrp", {"endpoint": "http://upstream", "api_key": "k"})), \
         patch.object(p.httpx, "AsyncClient", _FakeAsyncClient):
        try:
            asyncio.run(p.proxy_chat(req, _FakeRequest(), authorization="Bearer testkey"))
            assert False, "expected CancelledError to propagate"
        except asyncio.CancelledError:
            pass  # must re-raise, not swallow

    errs = [e for e in p._log_buffer if e.get("type") == "response" and e.get("error")]
    assert len(errs) == 1
    assert "disconnected" in errs[0]["error"].lower()
    json.dumps(errs[0])   # must stay serializable


def test_passthrough_sampling_knobs_forwarded():
    """seed/top_p/stop/max_tokens/response_format reach upstream on any provider (#47)."""
    payload = _run_proxy_capture(dict(
        model="qwen3", messages=[{"role": "user", "content": "hi"}],
        seed=42, top_p=0.9, stop=["END"], max_tokens=256,
        response_format={"type": "json_object"},
    ))
    assert payload["seed"] == 42
    assert payload["top_p"] == 0.9
    assert payload["stop"] == ["END"]
    assert payload["max_tokens"] == 256
    assert payload["response_format"] == {"type": "json_object"}


def test_passthrough_omits_unset_fields():
    """Fields the client didn't send are not injected (provider defaults intact)."""
    payload = _run_proxy_capture(dict(
        model="qwen3", messages=[{"role": "user", "content": "hi"}]))
    for k in ("seed", "top_p", "stop", "max_tokens", "response_format", "usage", "provider"):
        assert k not in payload, f"{k} should not be forwarded when unset"


def test_openrouter_only_knobs():
    """`provider` routing block and top-level `usage` go to OpenRouter only (#47).

    Both are OpenRouter-isms; a strict OpenAI-compatible server (e.g. vllm) may
    400 on them, so they must never leak to non-OpenRouter providers."""
    block = {"zdr": True, "order": ["anthropic"]}
    usage = {"include": True}
    or_payload = _run_proxy_capture(
        dict(model="z-ai/glm-5.2", messages=[{"role": "user", "content": "hi"}],
             provider=block, usage=usage),
        provider=("openrouter", {"endpoint": "http://or", "api_key": "k"}))
    assert or_payload["provider"] == block
    assert or_payload["usage"] == usage

    nrp_payload = _run_proxy_capture(
        dict(model="qwen3", messages=[{"role": "user", "content": "hi"}],
             provider=block, usage=usage),
        provider=("nrp", {"endpoint": "http://upstream", "api_key": "k"}))
    assert "provider" not in nrp_payload, "provider block must not leak to non-OpenRouter"
    assert "usage" not in nrp_payload, "usage must not leak to non-OpenRouter"


# --- Tool-call arg-dialect normalization (#85) -------------------------------
# glm-5.2 (and the qwen family) intermittently leak their tool-call arg encoding
# into the structured `arguments`. Verbatim symptom from the issue: a valid outer
# JSON object whose `value_stats` value is wrapped in the GLM XML arg dialect.

def test_normalize_glm_value_level_leak():
    """#85: dialect leaked into one value of an otherwise-valid JSON object.
    The wrapper is stripped and the intended JSON payload comes back structured."""
    p = importlib.reload(llm_proxy)
    inner = {"by_res": {"2": {"max": 9.45, "min": 0.1}}}
    args = json.dumps({
        "layer_id": "hardwood",
        "value_stats": f'<arg_key>value_stats</arg_key> <arg_value>{json.dumps(inner)}</arg_value>',
    })
    out, changed = p._normalize_tool_call_arguments(args)
    assert changed
    parsed = json.loads(out)
    assert parsed["value_stats"] == inner           # structured, not a string
    assert parsed["layer_id"] == "hardwood"         # untouched
    assert "<arg_key>" not in out and "<arg_value>" not in out


def test_normalize_glm_value_level_leak_unterminated():
    """The leaked value may arrive without a closing </arg_value> tag (as the
    issue's truncated capture showed). We still recover the payload up to end."""
    p = importlib.reload(llm_proxy)
    inner = {"by_res": {"2": {"max": 9.45}}}
    args = json.dumps({
        "value_stats": f'<arg_key>value_stats</arg_key> <arg_value>{json.dumps(inner)}',
    })
    out, changed = p._normalize_tool_call_arguments(args)
    assert changed
    assert json.loads(out)["value_stats"] == inner


def test_normalize_whole_string_glm_dialect():
    """The entire `arguments` string is raw GLM dialect (no valid outer JSON)."""
    p = importlib.reload(llm_proxy)
    raw = ('<arg_key>layer_id</arg_key> <arg_value>hardwood</arg_value> '
           '<arg_key>opacity</arg_key> <arg_value>0.5</arg_value>')
    out, changed = p._normalize_tool_call_arguments(raw)
    assert changed
    parsed = json.loads(out)
    assert parsed == {"layer_id": "hardwood", "opacity": 0.5}


def test_normalize_qwen_parameter_dialect():
    """The qwen/hermes `<parameter=NAME>VALUE</parameter>` form of the same leak."""
    p = importlib.reload(llm_proxy)
    args = json.dumps({"sql": "<parameter=sql>SELECT 1</parameter>"})
    out, changed = p._normalize_tool_call_arguments(args)
    assert changed
    assert json.loads(out)["sql"] == "SELECT 1"


def test_normalize_leaves_clean_arguments_untouched():
    """No dialect markers → byte-identical passthrough, no wasted re-serialize."""
    p = importlib.reload(llm_proxy)
    args = json.dumps({"sql": "SELECT * FROM t WHERE a < 5", "n": 3})
    out, changed = p._normalize_tool_call_arguments(args)
    assert not changed
    assert out == args


def test_normalize_response_tool_calls_in_place_and_counts():
    """The response-level pass mutates result in place and returns a repair count;
    a clean sibling tool call in the same response is left alone."""
    p = importlib.reload(llm_proxy)
    result = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "add_hex_tile_layer", "arguments": json.dumps(
            {"value_stats": '<arg_value>{"by_res": {"2": {"max": 1}}}</arg_value>'})}},
        {"function": {"name": "get_schema", "arguments": '{"dataset": "ca"}'}},
    ]}}]}
    n = p._normalize_response_tool_calls(result)
    assert n == 1
    tcs = result["choices"][0]["message"]["tool_calls"]
    assert json.loads(tcs[0]["function"]["arguments"])["value_stats"] == {"by_res": {"2": {"max": 1}}}
    assert tcs[1]["function"]["arguments"] == '{"dataset": "ca"}'


def test_normalize_response_is_defensive_on_garbage():
    """Malformed shapes never raise — normalization must not break serving."""
    p = importlib.reload(llm_proxy)
    for junk in ({}, {"choices": None}, {"choices": [None]},
                 {"choices": [{"message": {"tool_calls": [{"function": None}]}}]},
                 {"choices": [{"message": {"tool_calls": "nope"}}]}):
        assert p._normalize_response_tool_calls(junk) == 0


def test_handler_repairs_dialect_and_logs_count():
    """End-to-end: a glm-5.2 response with a leaked value is repaired before it
    is returned to the client, and the repair count is recorded in the log."""
    import asyncio
    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey")
    p._log_buffer.clear()

    leaked = json.dumps({"value_stats": '<arg_key>value_stats</arg_key> <arg_value>{"by_res": {"2": {"max": 9.45}}}</arg_value>'})

    class _FakeResp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {
                        "content": None,
                        "tool_calls": [{"function": {"name": "add_hex_tile_layer",
                                                     "arguments": leaked}}]}}],
                    "usage": {"total_tokens": 5}}

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return _FakeResp()

    class _FakeRequest:
        headers = {"origin": "https://ca-30x30.nrp-nautilus.io"}

    req = p.ChatRequest(model="z-ai/glm-5.2", messages=[{"role": "user", "content": "what fraction of ca hardwood is protected?"}])
    with patch.object(p, "get_provider_for_model",
                      return_value=("openrouter", {"endpoint": "http://or", "api_key": "k"})), \
         patch.object(p.httpx, "AsyncClient", _FakeAsyncClient):
        result = asyncio.run(p.proxy_chat(req, _FakeRequest(), authorization="Bearer testkey"))

    returned = json.loads(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    assert returned["value_stats"] == {"by_res": {"2": {"max": 9.45}}}   # client gets structured data
    responses = [e for e in p._log_buffer if e.get("type") == "response"]
    assert responses[0]["tool_call_dialect_repaired"] == 1
    assert "<arg_key>" not in json.dumps(responses[0])                   # log is clean too


def test_nrp_models_route_via_default_not_declaration():
    """NRP's models are deliberately UNdeclared (#110).

    This asserts the inverse of what it used to: `nrp.models` is empty, and every
    live NRP id reaches NRP through `default_provider`. Listing them bought only a
    suppressed log line — they landed on NRP either way — while making config.json
    a maintenance treadmill (most of its history is model-list edits).

    The thing that made deleting the list unsafe, and that this test guards, is in
    `test_exact_ids_are_not_claimed_by_another_providers_prefix`.
    """
    p = importlib.reload(llm_proxy)
    assert p.PROVIDERS["nrp"]["models"] == [], "NRP ids should not be enumerated"
    assert p.DEFAULT_PROVIDER == "nrp"

    live = [
        "gpt-oss", "gemma", "kimi", "minimax-m2", "deepseek-v4-flash", "gemma-small",
        "gemma4-small", "gemma4-12b", "glm-5", "qwen3", "qwen3-embedding",
        "qwen3-small", "qwen3-4bit", "gemma-small-e4b",
    ]
    for model in live:
        assert p.get_provider_for_model(model)[0] == "nrp", model

    # A brand-new NRP model needs no config change — the point of the exercise.
    assert p.get_provider_for_model("some-model-nrp-adds-next-week")[0] == "nrp"

    # deepseek-v4-flash still honors the `thinking` dialect: per-model quirks live
    # in thinking_models and are unaffected by emptying the routing list.
    assert p.PROVIDERS["nrp"]["thinking_models"]["deepseek-v4-flash"] == "thinking"


def test_exact_ids_are_not_claimed_by_another_providers_prefix():
    """The hazard #110 exists to remove.

    nimbus declares the exact id `qwen`. Under the old semantics every bare entry
    also acted as a prefix, so `qwen` silently claimed `qwen3`, `qwen3-small`,
    `qwen3-4bit` and `qwen3-embedding` — and the *only* thing preventing that was
    NRP listing each one explicitly so the exact pass matched first. Emptying
    nrp.models without this split would have moved four production models to a
    different backend, serving different weights under the requested id, with no
    error anywhere.
    """
    p = importlib.reload(llm_proxy)
    for model in ("qwen3", "qwen3-small", "qwen3-4bit", "qwen3-embedding"):
        assert p.get_provider_for_model(model)[0] == "nrp", f"{model} escaped to nimbus"

    # Exact ids still win, so the private single-model endpoints keep their traffic.
    assert p.get_provider_for_model("qwen")[0] == "nimbus"
    assert p.get_provider_for_model("gemma4")[0] == "gemma4-nimbus"
    assert p.get_provider_for_model("qwen3-8")[0] == "vllm-cirrus"


def test_prefixes_still_route_vendor_families_and_floating_aliases():
    p = importlib.reload(llm_proxy)
    for model, expected in (
        ("z-ai/glm-5.2", "openrouter"),
        ("moonshotai/kimi-k3", "openrouter"),
        ("deepseek/deepseek-v4-flash-0731", "openrouter"),
        ("~openai/gpt-5", "openrouter"),          # floating alias (#99)
        ("qwen/qwen3.7-flash", "openrouter"),     # vendor-namespaced, not nimbus
    ):
        assert p.get_provider_for_model(model)[0] == expected, model

    # `claude-` covers the whole family, including versions never added to config
    # — which is why the three pinned claude ids were deleted rather than updated.
    assert p.PROVIDERS["anthropic"]["models"] == []
    for model in ("claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5", "claude-fable-5"):
        assert p.get_provider_for_model(model)[0] == "anthropic", model


def test_legacy_config_without_model_prefixes_keeps_double_duty():
    """Back-compat: a config predating #110 (no `model_prefixes`) must behave as
    before, since cirrus's ConfigMap and any third-party config are still that
    shape."""
    p = importlib.reload(llm_proxy)
    built = p.build_provider_entry({
        "endpoint": "https://x", "models": ["anthropic/", "qwen"],
    })
    assert built["models"] == ["anthropic/", "qwen"]
    assert built["prefixes"] == ["anthropic/", "qwen"], "legacy entries must still act as prefixes"

    # ...and declaring model_prefixes switches `models` to exact-only.
    built = p.build_provider_entry({
        "endpoint": "https://x", "models": ["qwen"], "model_prefixes": [],
    })
    assert built["prefixes"] == []


def test_nrp_thinking_dialects_match_probed_behavior():
    """The `thinking_models` map records a *probed* fact, not a guess (#105).

    Each live NRP model was sent `{"enable_thinking": false}` and `{"thinking": false}`
    as `chat_template_kwargs` and checked for a suppressed `reasoning` field. A wrong
    or missing dialect is silent — `thinking_models.get(model)` misses and the client's
    `enable_thinking` flag is dropped with only an info log — so pin the map here.
    """
    p = importlib.reload(llm_proxy)
    thinking = p.PROVIDERS["nrp"]["thinking_models"]

    # Honors `thinking`, ignores `enable_thinking`.
    for model in ["kimi", "deepseek-v4-flash"]:
        assert thinking[model] == "thinking"
    # Honors `enable_thinking`.
    for model in ["qwen3", "qwen3-small", "qwen3-4bit", "glm-5", "gemma"]:
        assert thinking[model] == "enable_thinking"
    # Emits no reasoning at any setting, so nothing to toggle. `gemma-small-e4b`
    # keeps its inert entry (harmless, predates the probe); the rest stay absent
    # so the client flag is dropped loudly rather than sent as a no-op kwarg.
    for model in ["gemma-small", "gemma4-small", "gemma4-12b"]:
        assert model not in thinking
    # Reasoning is unconditional — neither dialect suppresses it. gpt-oss instead
    # takes an OpenAI-style `reasoning_effort` *level*, which this boolean map
    # cannot express; minimax-m2 exposes no off switch at all.
    for model in ["gpt-oss", "minimax-m2"]:
        assert model not in thinking
    # Embedding model — not a chat endpoint.
    assert "qwen3-embedding" not in thinking


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


# ---------------------------------------------------------------------------
# default_provider / unroutable-model handling
# ---------------------------------------------------------------------------

CIRRUS_LIKE = {
    # No `model_prefixes` in the source config -> the builder sets prefixes ==
    # models (legacy double duty). Keeping that here means these tests also
    # cover the back-compat path for configs that predate #110.
    "openrouter": {"endpoint": "https://openrouter.ai/x", "api_key": "k",
                   "models": ["anthropic/", "z-ai/", "~"],
                   "prefixes": ["anthropic/", "z-ai/", "~"], "extra_headers": {},
                   "thinking_models": {}, "no_sampling_params": []},
    "nimbus": {"endpoint": "https://nimbus/x", "api_key": "k", "models": ["qwen"],
               "prefixes": ["qwen"],
               "extra_headers": {}, "thinking_models": {}, "no_sampling_params": []},
}


def test_default_provider_absent_key_is_nrp_backward_compat():
    """No `default_provider` in config must behave exactly as the old hard-coded
    "nrp" branch did — this is what every NRP model id relies on, since most of
    `nrp.models` is redundant with the fallback."""
    p = importlib.reload(llm_proxy)
    assert p.resolve_default_provider({}, {"nrp": {}, "openrouter": {}}) == "nrp"
    # ...and the shipped config.json still resolves to nrp
    assert p.DEFAULT_PROVIDER == "nrp"


def test_default_provider_is_configurable_and_can_be_disabled():
    p = importlib.reload(llm_proxy)
    r = p.resolve_default_provider
    assert r({"default_provider": "openrouter"}, {"nrp": {}, "openrouter": {}}) == "openrouter"
    # explicit null/"" -> no fallback, reject unroutable ids
    assert r({"default_provider": None}, {"nrp": {}}) is None
    assert r({"default_provider": ""}, {"nrp": {}}) is None


def test_default_provider_unconfigured_name_degrades_to_none_not_keyerror():
    """The cirrus case: config names (or defaults to) a provider this deployment
    doesn't serve. Previously this reached `PROVIDERS["nrp"]` and raised KeyError
    inside the request handler -> opaque 500. It must degrade to "no fallback"."""
    p = importlib.reload(llm_proxy)
    # cirrus's ConfigMap sets no default_provider at all, so the "nrp" default
    # applies to a deployment with no nrp provider -> None, not an exception.
    assert p.resolve_default_provider({}, CIRRUS_LIKE) is None
    assert p.resolve_default_provider({"default_provider": "typo"}, CIRRUS_LIKE) is None


def test_unroutable_model_raises_unknown_model_error_naming_what_is_served():
    p = importlib.reload(llm_proxy)
    p.PROVIDERS = CIRRUS_LIKE
    p.DEFAULT_PROVIDER = None

    # ids that DO route on a cirrus-like deployment still route
    assert p.get_provider_for_model("z-ai/glm-5")[0] == "openrouter"
    assert p.get_provider_for_model("qwen")[0] == "nimbus"

    # bare NRP ids have nowhere to go -> a typed error, not KeyError
    for model in ("glm-5", "kimi", "minimax-m2", "gpt-oss", "claude-sonnet-4-6"):
        try:
            p.get_provider_for_model(model)
        except p.UnknownModelError as e:
            assert model in str(e)
            assert "openrouter" in str(e) and "nimbus" in str(e)   # says what IS served
        else:
            raise AssertionError(f"{model} should not have routed anywhere")


def test_fallback_is_silent_because_it_is_the_designed_route():
    """An id matching nothing lands on the default provider, and does NOT warn.

    It used to print `⚠️  Unknown model ...` per request. Once `nrp.models` was
    emptied (#110) the fallback became the *normal* route for every NRP call, so
    that line would fire on essentially all production traffic. The tradeoff is
    real and deliberate: a typo'd id now also routes here and fails upstream
    rather than being caught locally — `GET /v1/models` (#111) is the fix for
    that, not a per-request log line.
    """
    import contextlib
    import io

    p = importlib.reload(llm_proxy)
    p.PROVIDERS = dict(CIRRUS_LIKE)
    p.DEFAULT_PROVIDER = "openrouter"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        name, _ = p.get_provider_for_model("some-new-model")
    assert name == "openrouter"
    assert "Unknown model" not in buf.getvalue()
    assert buf.getvalue() == "", "routing must not log per request"


def test_unroutable_model_returns_400_and_is_logged():
    """End-to-end: the handler turns UnknownModelError into a 400 (not the old
    KeyError-driven 500) and leaves a log row so the miss is visible."""
    import asyncio

    import pytest
    from fastapi import HTTPException
    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey")
    p._log_buffer.clear()

    class _FakeRequest:
        headers = {"origin": "https://app"}

    req = p.ChatRequest(model="glm-5", messages=[{"role": "user", "content": "hi"}])
    with patch.object(p, "PROVIDERS", CIRRUS_LIKE), \
         patch.object(p, "DEFAULT_PROVIDER", None):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(p.proxy_chat(req, _FakeRequest(), authorization="Bearer testkey"))

    assert ei.value.status_code == 400
    assert "glm-5" in ei.value.detail
    responses = [e for e in p._log_buffer if e.get("type") == "response"]
    assert len(responses) == 1 and responses[0]["provider"] == "unrouted"
    assert "glm-5" in responses[0]["error"]
    # Same requirement as the streaming rejection: identity is resolved before the
    # early return, so the miss is attributable to a caller rather than anonymous.
    assert responses[0]["origin"] == "https://app"
    assert responses[0]["request_id"] is not None


# ---------------------------------------------------------------------------
# Upstream call budget (#135)
# ---------------------------------------------------------------------------

def test_upstream_timeout_defaults_to_20min_and_only_read_is_long():
    """The long budget must apply to `read` ONLY. Reverting to a bare
    `timeout=1200.0` would also give connect/write/pool 20 minutes, so a broken
    network path would hang for 20 minutes instead of failing in 30s."""
    p = _reload(PROXY_KEY="testkey")

    assert p._UPSTREAM_READ_TIMEOUT == 1200.0
    t = p._UPSTREAM_TIMEOUT
    assert t.read == 1200.0
    assert t.connect == 30.0, "a 20-minute TCP connect is a broken path, not a slow model"
    assert t.write == 30.0
    assert t.pool == 30.0


def test_upstream_timeout_is_env_overridable():
    p = _reload(PROXY_KEY="testkey", UPSTREAM_TIMEOUT_SECONDS="1800")
    assert p._UPSTREAM_READ_TIMEOUT == 1800.0
    assert p._UPSTREAM_TIMEOUT.read == 1800.0
    # Overriding the budget must not lengthen connect.
    assert p._UPSTREAM_TIMEOUT.connect == 30.0


# ---------------------------------------------------------------------------
# Streaming is rejected explicitly, not silently ignored (#129)
# ---------------------------------------------------------------------------

def test_stream_true_returns_400_and_is_logged():
    """`stream: true` must fail loudly. Before this, Pydantic's extra="ignore"
    dropped the field and the client got HTTP 200 with a NON-streaming body,
    which an SSE-expecting OpenAI client cannot parse — a silent protocol
    violation that looked like a client bug."""
    import asyncio

    import pytest
    from fastapi import HTTPException
    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey")
    p._log_buffer.clear()

    class _FakeRequest:
        headers = {"origin": "https://app"}

    req = p.ChatRequest(
        model="qwen3-8", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    assert req.stream is True, "stream must survive validation to be rejectable"

    with patch.object(p, "PROVIDERS", CIRRUS_LIKE), \
         patch.object(p, "DEFAULT_PROVIDER", None):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(p.proxy_chat(req, _FakeRequest(), authorization="Bearer testkey"))

    assert ei.value.status_code == 400
    assert "Streaming is not supported" in ei.value.detail
    # Visible in the corpus, so "who is asking for streaming?" is answerable.
    responses = [e for e in p._log_buffer if e.get("type") == "response"]
    assert len(responses) == 1
    assert responses[0]["provider"] == "streaming-unsupported"
    assert "Streaming is not supported" in responses[0]["error"]
    # Traceable to the app that asked: a rejection row with a null origin cannot
    # answer "who is asking for streaming?", which is why it is logged at all.
    assert responses[0]["origin"] == "https://app"
    assert responses[0]["request_id"] is not None
    # Rejected before routing, so no request row was emitted.
    assert [e for e in p._log_buffer if e.get("type") == "request"] == []


def test_stream_false_and_absent_are_unaffected():
    """Only a truthy `stream` is rejected: `false` and an absent field are the
    normal path, and `stream` is never forwarded upstream either way."""
    import asyncio

    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey")

    class _FakeRequest:
        headers = {"origin": "https://app"}

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"total_tokens": 3},
            }

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, endpoint, json=None, headers=None):
            captured["payload"] = json
            return _FakeResponse()

    for stream_value in (None, False):
        p._log_buffer.clear()
        captured.clear()
        kwargs = {} if stream_value is None else {"stream": stream_value}
        req = p.ChatRequest(
            model="qwen3-8", messages=[{"role": "user", "content": "hi"}], **kwargs
        )
        with patch.object(p, "PROVIDERS", CIRRUS_LIKE), \
             patch.object(p, "DEFAULT_PROVIDER", None), \
             patch.object(p.httpx, "AsyncClient", _FakeAsyncClient):
            result = asyncio.run(
                p.proxy_chat(req, _FakeRequest(), authorization="Bearer testkey")
            )
        assert result["choices"][0]["message"]["content"] == "hi"
        assert "stream" not in captured["payload"], (
            f"stream must not be forwarded upstream (stream={stream_value!r})"
        )
        assert not [
            e for e in p._log_buffer
            if e.get("provider") == "streaming-unsupported"
        ]


# ---------------------------------------------------------------------------
# Logging guardrails (#39) — surface a one-sided pipeline failure
# ---------------------------------------------------------------------------

def _reset_log_health(p):
    p._log_buffer.clear()
    p._log_counters.update({"requests": 0, "responses": 0})
    p._log_health.update({"last_window": None, "windows_checked": 0,
                          "imbalance_windows": 0, "swallowed_exceptions": 0,
                          "swallowed_by_fn": {}})


def test_emit_counts_by_type_at_the_buffer_not_the_call_site():
    """Counting must happen where an entry actually lands in the buffer.

    If it happened on entry to log_request/log_response, a log_response that
    threw would still be counted — masking the exact failure this detects.
    """
    p = importlib.reload(llm_proxy)
    _reset_log_health(p)
    p._emit({"type": "request"})
    p._emit({"type": "response"})
    p._emit({"type": "response"})
    p._emit({"type": "something-else"})
    assert p._log_counters == {"requests": 1, "responses": 2}


def test_balance_check_is_quiet_when_healthy_and_when_idle():
    import contextlib, io
    p = importlib.reload(llm_proxy)

    _reset_log_health(p)
    assert p.check_log_balance() is None, "idle window should report nothing"

    _reset_log_health(p)
    for _ in range(50):
        p._emit({"type": "request"})
        p._emit({"type": "response"})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        window = p.check_log_balance()
    assert window == {"requests": 50, "responses": 50, "ratio": 1.0}
    assert "imbalance" not in buf.getvalue().lower()
    # counters reset for the next window
    assert p._log_counters == {"requests": 0, "responses": 0}


def test_balance_check_ignores_low_volume_windows():
    """A single in-flight turn straddling the window boundary must not alarm."""
    import contextlib, io
    p = importlib.reload(llm_proxy)
    _reset_log_health(p)
    for _ in range(3):
        p._emit({"type": "request"})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        p.check_log_balance()
    assert "imbalance" not in buf.getvalue().lower()
    assert p._log_health["imbalance_windows"] == 0


def test_one_sided_outage_is_detected_through_the_handler_path():
    """The #37 acceptance test.

    Reproduces the real shape: log_response raises inside @_never_raises, so
    responses never reach the buffer while requests keep logging normally. The
    request is still served (that is the wrapper's job) — but the imbalance must
    now be visible instead of silent for hours.

    Driven through proxy_chat with a mocked upstream, per #39's testing note: a
    direct call to log_response would not reproduce a call-site failure.
    """
    import asyncio, contextlib, io
    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey")
    _reset_log_health(p)

    class _FakeResp:
        status_code = 200
        headers = {}
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": "hi"}}]}
    class _FakeAsyncClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _FakeResp()
    class _FakeRequest:
        headers = {"origin": "https://app"}

    def _boom(*a, **k):
        raise NameError("name 'client' is not defined")   # the #37 bug, near enough

    buf = io.StringIO()
    with patch.object(p, "get_provider_for_model",
                      return_value=("nrp", {"endpoint": "http://up", "api_key": "k"})), \
         patch.object(p.httpx, "AsyncClient", _FakeAsyncClient), \
         patch.object(p, "log_response", p._never_raises(_boom)), \
         contextlib.redirect_stdout(buf):
        for i in range(25):
            req = p.ChatRequest(model="qwen3", messages=[{"role": "user", "content": "hi"}])
            asyncio.run(p.proxy_chat(req, _FakeRequest(), authorization="Bearer testkey"))
        window = p.check_log_balance()

    out = buf.getvalue()
    # requests kept flowing and were served
    assert window["requests"] == 25 and window["responses"] == 0
    assert window["ratio"] == 0.0
    # ...and the failure is now loud, on both channels
    assert "Logging imbalance" in out
    assert p._log_health["imbalance_windows"] == 1
    assert p._log_health["swallowed_exceptions"] == 25
    assert p._log_health["swallowed_by_fn"]["_boom"] == 25


def test_swallowed_logging_exceptions_are_counted_and_escalate():
    import contextlib, io
    p = importlib.reload(llm_proxy)
    _reset_log_health(p)

    @p._never_raises
    def always_fails():
        raise ValueError("nope")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for _ in range(10):
            always_fails()
    assert p._log_health["swallowed_exceptions"] == 10
    assert p._log_health["swallowed_by_fn"]["always_fails"] == 10
    # escalates at 10 — a pattern, not a one-off
    assert "persistently broken" in buf.getvalue()
    # ...and the caller never sees the exception
    assert always_fails() is None


def test_health_reports_logging_state_without_ever_degrading_status():
    """/health backs liveness, readiness AND startup probes. A logging fault must
    never restart or de-rotate a pod that is serving traffic fine."""
    import asyncio
    p = _reload(PROXY_KEY="testkey")
    _reset_log_health(p)
    for _ in range(30):
        p._emit({"type": "request"})
    p.check_log_balance()          # trips the imbalance path

    h = asyncio.run(p.health_check())
    assert h["status"] == "healthy", "logging health must not gate the probes"
    assert h["logging"]["imbalance_windows"] == 1
    assert h["logging"]["last_window"] == {"requests": 30, "responses": 0, "ratio": 0.0}
    assert "swallowed_exceptions" in h["logging"]
    assert h["logging"]["buffer_depth"] == 30


# ---------------------------------------------------------------------------
# GET /v1/models — discovery (#111)
# ---------------------------------------------------------------------------

def _fake_models_client(catalog, fail=()):
    """httpx.AsyncClient stand-in serving a {url: [ids]} catalog."""
    class _Resp:
        def __init__(self, ids): self._ids = ids
        def raise_for_status(self): pass
        def json(self): return {"data": [{"id": i} for i in self._ids]}
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, timeout=None):
            if url in fail:
                raise ConnectionError("boom")
            return _Resp(catalog.get(url, []))
    return _Client


def test_models_url_and_auth_derivation():
    p = importlib.reload(llm_proxy)
    assert p._models_url({"endpoint": "https://x/v1/chat/completions"}) == "https://x/v1/models"
    assert p._models_url({"endpoint": "https://x/v1", "models_endpoint": "https://y/list"}) == "https://y/list"
    # default is Bearer; Anthropic's compat path rejects that and needs x-api-key
    assert "Authorization" in p._models_headers({"api_key": "k"})
    h = p._models_headers({"api_key": "k", "models_auth": "x-api-key"})
    assert h["x-api-key"] == "k" and "anthropic-version" in h


def test_discovery_lists_only_what_this_deployment_would_actually_route():
    """The listing is filtered through get_provider_for_model itself, so it can
    never disagree with routing. OpenRouter's real catalog is 400+ ids while we
    claim 11 vendor prefixes — anything outside them must not be advertised."""
    import asyncio
    from unittest.mock import patch

    p = importlib.reload(llm_proxy)
    catalog = {
        "https://ellm.nrp-nautilus.io/v1/models": ["qwen3", "glm-5", "deepseek-v4-flash"],
        "https://openrouter.ai/api/v1/models": [
            "z-ai/glm-5.2",            # routes to openrouter (prefix)
            "~deepseek/x-latest",      # floating alias, also openrouter
            "google/gemini-3.7-flash", # NOT a declared prefix -> must be excluded
        ],
        "https://api.anthropic.com/v1/models": ["claude-opus-5", "claude-sonnet-5"],
        "https://vllm-nimbus.carlboettiger.info/v1/models": ["qwen"],
    }
    with patch.object(p.httpx, "AsyncClient", _fake_models_client(catalog)):
        cache = asyncio.run(p.refresh_models(force=True))

    by_provider = {}
    for m in cache["data"]:
        by_provider.setdefault(m["provider"], set()).add(m["id"])

    assert {"qwen3", "glm-5", "deepseek-v4-flash"} <= by_provider["nrp"]
    assert {"z-ai/glm-5.2", "~deepseek/x-latest"} <= by_provider["openrouter"]
    # the id we cannot route is absent everywhere, not silently filed under nrp
    assert not any(m["id"] == "google/gemini-3.7-flash" for m in cache["data"])
    # claude ids come back even though config declares no exact claude id (#110)
    assert {"claude-opus-5", "claude-sonnet-5"} <= by_provider["anthropic"]
    assert by_provider["nimbus"] == {"qwen"}


def test_unreachable_provider_degrades_instead_of_disappearing():
    """A provider that is down (gemma4-nimbus and qwen3-cirrus were both 503 when
    this was written) must keep its declared ids, then its last-known-good."""
    import asyncio
    from unittest.mock import patch

    p = importlib.reload(llm_proxy)
    dead = "https://gemma4-nimbus.carlboettiger.info/v1/models"

    with patch.object(p.httpx, "AsyncClient", _fake_models_client({}, fail={dead})):
        cache = asyncio.run(p.refresh_models(force=True))
    g = cache["providers"]["gemma4-nimbus"]
    assert g["status"] == "declared"
    assert g["count"] == 1 and "error" in g          # falls back to config's `gemma4`
    assert any(m["id"] == "gemma4" for m in cache["data"])

    # now it answers, then goes away again -> last-known-good is retained
    with patch.object(p.httpx, "AsyncClient", _fake_models_client({dead: ["gemma4"]})):
        asyncio.run(p.refresh_models(force=True))
    with patch.object(p.httpx, "AsyncClient", _fake_models_client({}, fail={dead})):
        cache = asyncio.run(p.refresh_models(force=True))
    assert cache["providers"]["gemma4-nimbus"]["status"] == "stale"
    assert cache["providers"]["gemma4-nimbus"]["count"] == 1


def test_models_endpoint_requires_the_proxy_key():
    import asyncio
    import pytest
    from fastapi import HTTPException
    from unittest.mock import patch

    p = _reload(PROXY_KEY="testkey")
    with patch.object(p.httpx, "AsyncClient", _fake_models_client({})):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(p.list_models(authorization=None))
        assert ei.value.status_code == 401
        body = asyncio.run(p.list_models(authorization="Bearer testkey"))
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    # prefixes are surfaced: `claude-` is routable even with no enumerable id
    assert body["providers"]["anthropic"]["prefixes"] == ["claude-"]
    assert "ids" not in body["providers"]["anthropic"]   # internal detail stays internal


def test_models_are_cached_and_force_refresh_bypasses():
    import asyncio
    from unittest.mock import patch

    p = importlib.reload(llm_proxy)
    calls = {"n": 0}
    base = _fake_models_client({"https://ellm.nrp-nautilus.io/v1/models": ["qwen3"]})

    class _Counting(base):
        async def get(self, url, headers=None, timeout=None):
            calls["n"] += 1
            return await super().get(url, headers=headers, timeout=timeout)

    with patch.object(p.httpx, "AsyncClient", _Counting):
        asyncio.run(p.refresh_models(force=True))
        first = calls["n"]
        asyncio.run(p.refresh_models())          # within TTL -> no refetch
        assert calls["n"] == first
        asyncio.run(p.refresh_models(force=True))
        assert calls["n"] > first


def test_models_headers_survive_none_valued_config_keys():
    """build_provider_entry stores absent discovery overrides as None, so
    `.get(key, default)` never applies the default — it returns the stored None.
    That handed httpx a None header value and made Anthropic discovery fail with
    AttributeError against the live API. Regression guard."""
    p = importlib.reload(llm_proxy)
    entry = p.build_provider_entry({"endpoint": "https://api.anthropic.com/v1/chat/completions",
                                    "models_auth": "x-api-key"})
    assert entry["anthropic_version"] is None          # the trap
    h = p._models_headers({**entry, "api_key": "k"})
    assert h["anthropic-version"] == "2023-06-01"
    assert all(v is not None for v in h.values())
