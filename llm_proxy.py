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
import httpx
import os
import json
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

def _emit(log_entry: dict):
    """Print log entry and add to S3 buffer."""
    _log_buffer.append(log_entry)

async def _flush_to_s3():
    """Write buffered log entries to S3 as a JSONL chunk file."""
    if not _log_buffer or not _S3_ENABLED:
        return
    entries, _log_buffer[:] = list(_log_buffer), []
    body = "\n".join(json.dumps(e) for e in entries) + "\n"
    now = datetime.utcnow()
    key = f"{now.strftime('%Y-%m-%d')}/{now.strftime('%H-%M-%S')}-{os.getpid()}.jsonl"
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
        print(f"⚠️  S3 flush failed: {e} — entries remain in pod logs only", flush=True)

async def _flush_loop():
    while True:
        await asyncio.sleep(300)  # flush every 5 minutes
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

def log_request(provider: str, model: str, messages: List[Dict], tools_count: int = 0, origin: str = None, request_id: str = None, session_id: str = None):
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
                "content": (m.get("content") or "")[:500]
            })
        elif m.get("role") == "assistant":
            break  # stop at the previous assistant turn

    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "type": "request",
        "request_id": request_id,
        "session_id": session_id,
        "provider": provider,
        "model": model,
        "origin": origin,
        "message_count": len(messages),
        "tools_count": tools_count,
        "user_question": user_question[:500],
        "tool_results_this_turn": list(reversed(tool_results)) if tool_results else None,
    }
    print(f"📥 REQUEST: {json.dumps(log_entry)}", flush=True)
    _emit(log_entry)

def log_response(provider: str, model: str, response_data: dict, latency_ms: int, error: str = None, origin: str = None, request_id: str = None, session_id: str = None):
    """Log response in structured JSON format"""
    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "type": "response",
        "request_id": request_id,
        "session_id": session_id,
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
            log_entry["has_content"] = bool(message.get("content"))
            log_entry["has_tool_calls"] = bool(message.get("tool_calls"))
            log_entry["content_preview"] = (message.get("content") or "")[:200]
            
            if message.get("tool_calls"):
                log_entry["tool_calls"] = [
                    {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments", "")}
                    for tc in message["tool_calls"]
                ]
        
        # Extract token usage if available
        if "usage" in response_data:
            log_entry["tokens"] = response_data["usage"]
    
    status = "✗" if error else "✓"
    print(f"{status} RESPONSE: {json.dumps(log_entry)}", flush=True)
    _emit(log_entry)

class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]  # Accept any message format from OpenAI API
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[str] = "auto"
    model: Optional[str] = "gpt-4"
    temperature: Optional[float] = 0.7
    enable_thinking: Optional[bool] = None  # None = use model default; True/False to override

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
    session_id = http_request.headers.get("x-session-id")
    log_request(provider_name, request.model, request.messages, len(request.tools or []), origin=origin, request_id=request_id, session_id=session_id)
    
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
            log_response(provider_name, request.model, result, latency_ms, origin=origin, request_id=request_id, session_id=session_id)

            return result

        except httpx.TimeoutException as e:
            latency_ms = int((time.time() - start_time) * 1000)
            error_detail = f"Request timed out after {latency_ms}ms"
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id)
            raise HTTPException(status_code=504, detail=error_detail)

        except httpx.HTTPStatusError as e:
            latency_ms = int((time.time() - start_time) * 1000)
            error_detail = f"Provider returned {e.response.status_code}: {e.response.text[:200]}"
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id)

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
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id)
            
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
