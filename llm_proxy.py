"""
LLM Proxy Server for Kubernetes Deployment
Multi-provider proxy supporting NRP, OpenRouter, and Nimbus endpoints
Provides unified logging for all LLM requests
API keys stored in environment variables, never exposed to browser
Requires authentication token to prevent unauthorized use
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import hashlib
import httpx
import os
import json
import re
import time
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path

# --- S3 log buffer -----------------------------------------------------------
_log_buffer: List[dict] = []
_LOG_BUCKET = os.getenv("LOG_BUCKET", "logs-open-llm-proxy")
_S3_ENDPOINT = os.getenv("AWS_S3_ENDPOINT_URL", "http://rook-ceph-rgw-nautiluss3.rook")
_S3_ENABLED = bool(os.getenv("AWS_ACCESS_KEY_ID"))
_FLUSH_INTERVAL = int(os.getenv("FLUSH_INTERVAL", "60"))
# Cap the in-memory buffer so a prolonged S3 outage (entries re-queued on each
# failed flush) can't grow it without bound. Drop-oldest when exceeded, logged.
_MAX_BUFFER = int(os.getenv("LOG_MAX_BUFFER", "50000"))

# --- Logging fidelity --------------------------------------------------------
# Capture mode controls how much of each turn is logged (see LOGGING.md):
#   "summary" (default) — full response content + generously-capped inputs,
#                         but only this-turn tool results (not the whole prompt)
#   "full"              — additionally logs the entire (scrubbed) `messages`
#                         array per request for training-grade fidelity, with
#                         the large system prompt de-duplicated by hash.
_CAPTURE_MODE = os.getenv("LOG_CAPTURE_MODE", "summary").lower()

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

# Per-field length caps. 0 (or negative) means "no cap" — log the full string.
# Defaults are generous relative to the old hard-coded 200/500 limits. The final
# answer (`content`) and tool-call arguments are kept in full by default; the
# bulky reasoning trace is capped separately so you can keep full decisions/answers
# without the verbose thinking (a `*_preview` of 200 chars is always retained).
_CONTENT_MAX       = _int_env("LOG_CONTENT_MAX", 0)        # response final-answer content
_REASONING_MAX     = _int_env("LOG_REASONING_MAX", 4000)   # response reasoning_content trace
_TOOL_RESULT_MAX   = _int_env("LOG_TOOL_RESULT_MAX", 20000)
_USER_QUESTION_MAX = _int_env("LOG_USER_QUESTION_MAX", 4000)

# Per-field cap for the *stdout* (kubectl) copy only — keeps pod logs readable
# and bounds I/O while the full record still goes to S3. The full prompt
# (`messages`, full mode) is dropped from stdout entirely (message_count covers
# it). Stdout falls back to the full record only when S3 is disabled (then
# stdout is the sole sink). See LOGGING.md.
_STDOUT_MAX_FIELD = _int_env("LOG_STDOUT_MAX_FIELD", 200)

def _cap(s: Optional[str], limit: int) -> str:
    """Truncate `s` to `limit` chars; limit <= 0 means no truncation."""
    s = s or ""
    if limit and limit > 0 and len(s) > limit:
        return s[:limit]
    return s

# --- Credential scrubbing ----------------------------------------------------
# Credentials reach the logs because the geo-agent `query` MCP tool accepts
# s3_key/s3_secret in its arguments, which flow through `tool_calls`, tool
# results and the `messages` array. Scrub before anything is logged. This is
# always on, independent of capture mode — observability logs leak secrets too.
# Implementation lives in scrub.py so the live path and the historical scrub
# job (scrub-historical-logs.py) share one source of truth and never diverge.
from scrub import scrub as _scrub, scrub_text as _scrub_text, REDACTED as _REDACTED

def _emit(log_entry: dict):
    """Print log entry and add to S3 buffer."""
    _log_buffer.append(log_entry)

def _stdout_view(entry: dict) -> dict:
    """Compact copy of a log entry for kubectl/pod-stdout.

    Bounds every string field to `_STDOUT_MAX_FIELD` and drops the full
    `messages` array (full mode) — the durable, untruncated record is what gets
    buffered to S3. When S3 is disabled, callers print the full entry instead.
    """
    def shrink(v):
        if isinstance(v, str) and len(v) > _STDOUT_MAX_FIELD:
            return f"{v[:_STDOUT_MAX_FIELD]}…(+{len(v) - _STDOUT_MAX_FIELD} chars)"
        if isinstance(v, list):
            return [shrink(x) for x in v]
        if isinstance(v, dict):
            return {k: shrink(x) for k, x in v.items()}
        return v
    return {k: shrink(v) for k, v in entry.items() if k != "messages"}

# Hashes of system prompts already logged in full this process. The system
# prompt (~22k tokens, identical every turn) dominates message size, so we log
# it once and reference it by hash thereafter. Resets on restart (re-logs once).
_seen_system_hashes: set = set()

def _dedup_messages(messages: List[Dict], origin: str = None) -> List[Dict]:
    """Scrub `messages` and replace large system prompts with a hash reference.

    The first time a given system-prompt body is seen, it is emitted as a
    standalone `type: "system_prompt"` log entry; subsequent turns reference it
    by `system_sha256` so the corpus stays reconstructable without re-storing it.
    """
    out = []
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            body = m["content"]
            h = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if h not in _seen_system_hashes:
                _seen_system_hashes.add(h)
                _emit({
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "type": "system_prompt",
                    "origin": origin,
                    "system_sha256": h,
                    "content": _scrub_text(body),
                })
            out.append({"role": "system", "system_sha256": h,
                        "content_len": len(body), "_dedup": True})
        else:
            out.append(_scrub(m))
    return out

async def _flush_to_s3():
    """Write buffered log entries to S3 as a JSONL chunk file."""
    if not _log_buffer or not _S3_ENABLED:
        return
    entries, _log_buffer[:] = list(_log_buffer), []
    body = "\n".join(json.dumps(e) for e in entries) + "\n"
    now = datetime.utcnow()
    # Key must be unique per writer AND per flush. PID alone is NOT enough across
    # replicas: each pod has its own PID namespace, so two pods can share a PID
    # and overwrite each other's chunk. Include the pod hostname and a per-flush
    # UUID so any number of replicas/workers can flush concurrently without loss.
    host = os.getenv("HOSTNAME", "nohost")
    key = (f"{now.strftime('%Y-%m-%d')}/{now.strftime('%H-%M-%S')}"
           f"-{host}-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl")
    try:
        import boto3
        client = boto3.client(
            "s3",
            endpoint_url=_S3_ENDPOINT,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: client.put_object(Bucket=_LOG_BUCKET, Key=key, Body=body.encode())
        )
        print(f"✓ Flushed {len(entries)} log entries to s3://{_LOG_BUCKET}/{key}", flush=True)
    except Exception as e:
        # Re-queue the batch instead of dropping it, so a transient S3 error
        # (timeout, 5xx, throttle) doesn't permanently lose log records. New
        # entries may have arrived during the await; prepend the failed batch
        # ahead of them — we sort by ts downstream, so ordering is fine.
        _log_buffer[:0] = entries
        if len(_log_buffer) > _MAX_BUFFER:
            dropped = len(_log_buffer) - _MAX_BUFFER
            del _log_buffer[:dropped]  # drop oldest, never silently
            print(f"⚠️  Log buffer exceeded {_MAX_BUFFER} entries during outage; "
                  f"dropped {dropped} oldest", flush=True)
        print(f"⚠️  S3 flush failed: {e} — {len(entries)} entries re-queued for retry", flush=True)

async def _flush_loop():
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        await _flush_to_s3()

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_flush_loop())
    yield
    task.cancel()
    await _flush_to_s3()  # final flush on shutdown

app = FastAPI(title="Multi-Provider LLM Proxy", lifespan=lifespan)

# Enable CORS - allow requests from GitHub Pages and k8s deployment
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"https://.*\.nrp-nautilus\.io",
    allow_credentials=True,  # Required for Authorization header
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],  # Allow all headers to prevent preflight failures
)

# Load configuration from config.json
def load_config() -> dict:
    """Load provider configuration from config.json file"""
    config_path = Path(__file__).parent / "config.json"
    
    # Default configuration if config.json doesn't exist
    default_config = {
        "providers": {
            "nrp": {
                "endpoint": "https://ellm.nrp-nautilus.io/v1/chat/completions",
                "api_key_env": "NRP_API_KEY",
                "models": ["kimi", "qwen3", "glm-4.6"]
            },
            "openrouter": {
                "endpoint": "https://openrouter.ai/api/v1/chat/completions",
                "api_key_env": "OPENROUTER_KEY",
                "models": ["anthropic/", "mistralai/", "amazon/", "openai/", "qwen/"],
                "extra_headers": {
                    "HTTP-Referer": "https://wetlands.nrp-nautilus.io",
                    "X-Title": "Wetlands Chatbot"
                }
            },
            "nimbus": {
                "endpoint": "https://vllm-cirrus.carlboettiger.info/v1/chat/completions",
                "api_key_env": "NIMBUS_API_KEY",
                "models": ["cirrus"]
            }
        }
    }
    
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            print(f"✓ Loaded configuration from {config_path}")
            return config
        except Exception as e:
            print(f"⚠️  Error loading {config_path}: {e}")
            print("   Using default configuration")
            return default_config
    else:
        print(f"ℹ️  No config.json found at {config_path}, using defaults")
        return default_config

# Load config and build providers
config = load_config()
PROXY_KEY = os.getenv("PROXY_KEY")  # Key required from clients
CACHE_SALT = os.getenv("CACHE_SALT")  # Optional: isolate cached responses per deployment

# Build PROVIDERS dictionary from config
PROVIDERS = {}
for provider_name, provider_config in config["providers"].items():
    api_key_env = provider_config.get("api_key_env")
    api_key = os.getenv(api_key_env) if api_key_env else None

    PROVIDERS[provider_name] = {
        "endpoint": provider_config["endpoint"],
        "api_key": api_key,
        "models": provider_config["models"],
        "extra_headers": provider_config.get("extra_headers", {}),
        "thinking_models": provider_config.get("thinking_models", {})
    }

# Log configuration status
print("=" * 60)
print("🚀 Multi-Provider LLM Proxy Starting")
print("=" * 60)
for provider, config in PROVIDERS.items():
    has_key = bool(config["api_key"])
    status = "✓" if has_key else "✗"
    print(f"{status} {provider.upper()}: {config['endpoint']} (key: {'set' if has_key else 'MISSING'})")
if not PROXY_KEY:
    print("⚠️  WARNING: PROXY_KEY not set - proxy will reject all requests!")
if CACHE_SALT:
    print("✓ CACHE_SALT configured - responses isolated from other NRP tenants")
else:
    print("ℹ️  CACHE_SALT not set - cached responses may be shared with other NRP users")
print("=" * 60)

def get_provider_for_model(model: str) -> tuple[str, dict]:
    """Determine which provider to use based on model name"""
    # Check exact matches first (NRP and Nimbus)
    for provider_name, config in PROVIDERS.items():
        if model in config["models"]:
            return provider_name, config
    
    # Check prefix matches (OpenRouter)
    for provider_name, config in PROVIDERS.items():
        for model_prefix in config["models"]:
            if model.startswith(model_prefix):
                return provider_name, config
    
    # Default to NRP
    print(f"⚠️  Unknown model '{model}', defaulting to NRP")
    return "nrp", PROVIDERS["nrp"]

def _never_raises(fn):
    """Logging must never break request serving.

    `log_request` runs before the upstream call, so an exception here (e.g. a
    scrubbing or json.dumps edge case) would 500 the client and drop the request.
    Swallow logging errors, recording a breadcrumb instead.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # pragma: no cover - defensive
            print(f"⚠️  {fn.__name__} failed (request still served): "
                  f"{type(e).__name__}: {e}", flush=True)
    return wrapper

@_never_raises
def log_request(provider: str, model: str, messages: List[Dict], tools_count: int = 0, origin: str = None, request_id: str = None, session_id: str = None, client: str = None):
    """Log incoming request in structured JSON format"""
    # Extract the original user question (first human message, stable across all turns)
    user_question = next(
        (m.get("content", "") for m in messages if m.get("role") == "user"),
        ""
    )
    # Extract tool results added in this turn (role=tool messages at the end of history)
    # These capture both local geo-agent tool results and MCP tool results
    tool_results = []
    for m in reversed(messages):
        if m.get("role") == "tool":
            tool_results.append({
                "tool_call_id": m.get("tool_call_id"),
                "content": _scrub_text(_cap(m.get("content"), _TOOL_RESULT_MAX)),
            })
        elif m.get("role") == "assistant":
            break  # stop at the previous assistant turn

    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "type": "request",
        "request_id": request_id,
        "session_id": session_id,
        "client": client,           # e.g. "geo-agent/v3.13.1"; null until clients send X-Client
        "provider": provider,
        "model": model,
        "origin": origin,
        "message_count": len(messages),
        "tools_count": tools_count,
        "user_question": _scrub_text(_cap(user_question, _USER_QUESTION_MAX)),
        "tool_results_this_turn": list(reversed(tool_results)) if tool_results else None,
    }
    # Training-grade fidelity: capture the entire (scrubbed, system-deduped)
    # prompt so (messages -> completion) pairs can be reconstructed by request_id.
    if _CAPTURE_MODE == "full":
        log_entry["messages"] = _dedup_messages(messages, origin=origin)
    print(f"📥 REQUEST: {json.dumps(log_entry if not _S3_ENABLED else _stdout_view(log_entry))}", flush=True)
    _emit(log_entry)

@_never_raises
def log_response(provider: str, model: str, response_data: dict, latency_ms: int, error: str = None, origin: str = None, request_id: str = None, session_id: str = None, client: str = None):
    """Log response in structured JSON format"""
    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "type": "response",
        "request_id": request_id,
        "session_id": session_id,
        "client": client,
        "provider": provider,
        "model": model,
        "origin": origin,
        "latency_ms": latency_ms,
    }
    
    if error:
        log_entry["error"] = error
    else:
        # Extract response details
        if "choices" in response_data and len(response_data["choices"]) > 0:
            message = response_data["choices"][0].get("message", {})
            content = _scrub_text(message.get("content") or "")
            reasoning = _scrub_text(message.get("reasoning_content") or "")
            log_entry["has_content"] = bool(message.get("content"))
            log_entry["has_tool_calls"] = bool(message.get("tool_calls"))
            log_entry["has_reasoning_content"] = bool(message.get("reasoning_content"))
            # Full (scrubbed) response — this is the training target, no longer
            # truncated. *_preview kept for cheap kubectl/SQL scans (back-compat).
            log_entry["content"] = _cap(content, _CONTENT_MAX)
            log_entry["reasoning_content"] = _cap(reasoning, _REASONING_MAX)
            log_entry["content_preview"] = content[:200]
            log_entry["reasoning_content_preview"] = reasoning[:200]

            if message.get("tool_calls"):
                log_entry["tool_calls"] = [
                    {"name": tc["function"]["name"],
                     "arguments": _scrub(tc["function"].get("arguments", ""), _key="arguments")}
                    for tc in message["tool_calls"]
                ]
        
        # Extract token usage if available
        if "usage" in response_data:
            log_entry["tokens"] = response_data["usage"]
    
    status = "✗" if error else "✓"
    print(f"{status} RESPONSE: {json.dumps(log_entry if not _S3_ENABLED else _stdout_view(log_entry))}", flush=True)
    _emit(log_entry)

class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]  # Accept any message format from OpenAI API
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[str] = "auto"
    model: Optional[str] = "gpt-4"
    temperature: Optional[float] = 0.0
    enable_thinking: Optional[bool] = None  # None = use model default; True/False to override
    user: Optional[str] = None  # OpenAI end-user id; geo-agent sets it to its per-session UUID. Logged as session_id (not forwarded upstream).

@app.post("/v1/chat/completions")
@app.post("/chat")  # Keep for backward compatibility
async def proxy_chat(request: ChatRequest, http_request: Request, authorization: Optional[str] = Header(None)):
    """
    Multi-provider proxy for chat completions
    Routes requests to appropriate provider based on model name
    Logs all requests and responses for observability
    Requires client to provide PROXY_KEY via Authorization header
    """
    start_time = time.time()
    
    # Check client authorization
    if not PROXY_KEY:
        raise HTTPException(status_code=500, detail="PROXY_KEY not configured on server")
    
    client_key = None
    if authorization:
        client_key = authorization.replace('Bearer ', '').strip()
    
    if not client_key or client_key != PROXY_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing proxy key")
    
    # Determine provider based on model
    provider_name, provider_config = get_provider_for_model(request.model)
    endpoint = provider_config["endpoint"]
    api_key = provider_config["api_key"]
    
    if not api_key:
        error_msg = f"{provider_name.upper()} API key not configured on server"
        log_response(provider_name, request.model, {}, 0, error=error_msg)
        raise HTTPException(status_code=500, detail=error_msg)

    # Log incoming request
    request_id = uuid.uuid4().hex[:8]
    origin = http_request.headers.get("origin") or http_request.headers.get("referer")
    # Session id: prefer the OpenAI `user` body field (geo-agent already sends its
    # per-session UUID there); fall back to the X-Session-Id header for other clients.
    session_id = request.user or http_request.headers.get("x-session-id")
    client = http_request.headers.get("x-client")   # e.g. "geo-agent/v3.13.1"; null until clients send it
    log_request(provider_name, request.model, request.messages, len(request.tools or []), origin=origin, request_id=request_id, session_id=session_id, client=client)
    
    # Prepare request to LLM provider
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    # Add provider-specific extra headers if configured
    if "extra_headers" in provider_config and provider_config["extra_headers"]:
        headers.update(provider_config["extra_headers"])
    
    payload = {
        "model": request.model,
        "messages": request.messages,
        "temperature": request.temperature
    }
    
    # Add tools if provided
    if request.tools:
        payload["tools"] = request.tools
        payload["tool_choice"] = request.tool_choice

    # Cache salt: isolate this deployment's cached responses from other NRP tenants
    if CACHE_SALT and provider_name == "nrp":
        payload["cache_salt"] = CACHE_SALT

    # Thinking mode: inject per-model chat_template_kwargs if enable_thinking is set
    if request.enable_thinking is not None:
        thinking_models = provider_config.get("thinking_models", {})
        thinking_key = thinking_models.get(request.model)
        if thinking_key:
            payload["chat_template_kwargs"] = {thinking_key: request.enable_thinking}
        else:
            print(f"ℹ️  enable_thinking requested for '{request.model}' but no thinking_key configured — ignoring")
    
    # Make request to LLM provider
    async with httpx.AsyncClient(timeout=600.0) as client:
        try:
            response = await client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
            
            # Log successful response
            latency_ms = int((time.time() - start_time) * 1000)
            log_response(provider_name, request.model, result, latency_ms, origin=origin, request_id=request_id, session_id=session_id, client=client)

            return result

        except httpx.TimeoutException as e:
            latency_ms = int((time.time() - start_time) * 1000)
            error_detail = f"Request timed out after {latency_ms}ms"
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id, client=client)
            raise HTTPException(status_code=504, detail=error_detail)

        except httpx.HTTPStatusError as e:
            latency_ms = int((time.time() - start_time) * 1000)
            error_detail = f"Provider returned {e.response.status_code}: {e.response.text[:1000]}"
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id, client=client)

            # Pass through certain status codes to client
            if e.response.status_code in [400, 401, 402, 403, 429]:
                # Client errors and rate limits - pass through the original status
                raise HTTPException(status_code=e.response.status_code, detail=error_detail)
            else:
                # Other errors become 502 Bad Gateway (more accurate than 500)
                raise HTTPException(status_code=502, detail=error_detail)

        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            error_detail = f"{type(e).__name__}: {str(e)}"
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id, client=client)
            
            # Use 502 Bad Gateway for connection errors (more accurate than 500)
            # 500 should only be for internal proxy errors
            raise HTTPException(status_code=502, detail=f"Connection error: {error_detail}")

@app.options("/v1/chat/completions")
@app.options("/chat")
async def options_chat():
    """Handle CORS preflight for chat endpoints"""
    return Response(status_code=204)

@app.get("/health")
async def health_check():
    """Health check endpoint (logging suppressed)"""
    providers_status = {
        name: {"configured": bool(config["api_key"]), "endpoint": config["endpoint"]}
        for name, config in PROVIDERS.items()
    }
    return {
        "status": "healthy",
        "providers": providers_status,
        "proxy_key_configured": bool(PROXY_KEY)
    }

# Configure logging to filter out /health endpoint
import logging
from uvicorn.config import LOGGING_CONFIG

class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return '/health' not in record.getMessage()

# Apply filter to uvicorn access logger
logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Multi-Provider LLM Proxy",
        "version": "2.0",
        "providers": list(PROVIDERS.keys()),
        "endpoints": {
            "/v1/chat/completions": "POST - OpenAI-compatible chat completions",
            "/chat": "POST - Legacy chat endpoint",
            "/health": "GET - Health check with provider status"
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
