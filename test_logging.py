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
