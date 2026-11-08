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

# Allow-list of upstream response headers to capture on the error path (#44).
# These let us distinguish a genuine rate-limit (429 + retry-after/x-ratelimit-*)
# from a dead-backend gateway failure (naked 500, content-length: 0, no
# server/x-request-id) — a distinction otherwise only catchable live with
# `curl -i`. Kept an explicit allow-list (not the full header bag) so nothing
# sensitive is logged; values still pass through the scrubber for defense in
# depth. Lower-cased for case-insensitive lookup against httpx.Headers.
_UPSTREAM_HEADER_ALLOWLIST = (
    "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-request-id", "server", "date", "content-length",
)

def _capture_upstream_headers(headers) -> dict:
    """Pull the allow-listed subset of an upstream response's headers, scrubbed.
    Returns None when none are present so the field is omitted from the log."""
    if not headers:
        return None
    captured = {
        name: _scrub_text(headers[name])
        for name in _UPSTREAM_HEADER_ALLOWLIST
        if name in headers
    }
    return captured or None

def _cap(s: Optional[str], limit: int) -> str:
    """Truncate `s` to `limit` chars; limit <= 0 means no truncation."""
    s = s or ""
    if limit and limit > 0 and len(s) > limit:
        return s[:limit]
    return s

# --- Tool-call arg-dialect normalization (#85) -------------------------------
# Some open-weight backends intermittently fail to decode their own tool-call
# argument encoding, leaving raw markup inside the structured `arguments` string
# that an otherwise well-formed native `tool_calls` entry hands back:
#   GLM (z-ai/glm-5.2):  <arg_key>NAME</arg_key> <arg_value>VALUE</arg_value>
#   qwen / hermes:       <parameter=NAME>VALUE</parameter>
# The intended payload is intact *inside* the wrapper, so this is a
# serialization/parse gap, not lost data (#85; qwen precedent geo-agent#276).
# We repair it here — before the response is returned or logged — so no
# downstream consumer (client or log) ever sees the dialect. Fully defensive:
# any parse failure leaves the value untouched.
_ARG_DIALECT_MARKERS = ("<arg_key>", "<arg_value>", "<parameter=")
_GLM_ARG_PAIR_RE   = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)(?:</arg_value>|$)", re.DOTALL)
_QWEN_ARG_PAIR_RE  = re.compile(r"<parameter=(.*?)>(.*?)(?:</parameter>|$)", re.DOTALL)
_GLM_ARG_VALUE_RE  = re.compile(r"<arg_value>(.*?)(?:</arg_value>|$)", re.DOTALL)
_QWEN_ARG_VALUE_RE = re.compile(r"<parameter=[^>]*>(.*?)(?:</parameter>|$)", re.DOTALL)


def _coerce_json(s: str):
    """Parse `s` as JSON when it is structured data, else return it stripped.
    Lets an unwrapped `{...}`/`[...]`/number value come back structured while a
    bare scalar string stays a string."""
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        return s


def _unwrap_dialect_value(val):
    """If a single tool-call argument *value* carries leaked arg-dialect markup,
    extract the real payload from inside the <arg_value>/<parameter=…> wrapper.
    Returns (value, changed)."""
    if not isinstance(val, str) or not any(m in val for m in _ARG_DIALECT_MARKERS):
        return val, False
    m = _GLM_ARG_VALUE_RE.search(val) or _QWEN_ARG_VALUE_RE.search(val)
    if not m:
        return val, False
    return _coerce_json(m.group(1)), True


def _normalize_tool_call_arguments(arguments):
    """Strip leaked arg-dialect markup from a tool call's `arguments` string.
    Handles both the value-level leak (dialect inside one value of an otherwise
    valid JSON object, the #85 symptom) and the whole-string leak (the entire
    `arguments` is raw dialect). Returns (arguments_string, changed)."""
    if not isinstance(arguments, str) or not any(m in arguments for m in _ARG_DIALECT_MARKERS):
        return arguments, False
    # Case 1: arguments is valid JSON; dialect leaked into individual values.
    try:
        obj = json.loads(arguments)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        changed = False
        for k, v in list(obj.items()):
            new_v, ch = _unwrap_dialect_value(v)
            if ch:
                obj[k] = new_v
                changed = True
        return (json.dumps(obj), True) if changed else (arguments, False)
    # Case 2: the whole arguments string is raw dialect — rebuild the object by
    # pairing each key tag with the value tag that follows it.
    pairs = _GLM_ARG_PAIR_RE.findall(arguments) or _QWEN_ARG_PAIR_RE.findall(arguments)
    if pairs:
        return json.dumps({k.strip(): _coerce_json(v) for k, v in pairs}), True
    return arguments, False


def _normalize_response_tool_calls(result) -> int:
    """Repair leaked tool-call arg dialect (#85) in an upstream response, in
    place. Returns the number of tool-call `arguments` repaired. Fully
    defensive: any error leaves `result` untouched and returns 0 —
    normalization must never corrupt a response or break serving."""
    repaired = 0
    try:
        for choice in result.get("choices") or []:
            message = (choice or {}).get("message") or {}
            for tc in message.get("tool_calls") or []:
                fn = (tc or {}).get("function")
                if not isinstance(fn, dict):
                    continue
                new_args, changed = _normalize_tool_call_arguments(fn.get("arguments"))
                if changed:
                    fn["arguments"] = new_args
                    repaired += 1
    except Exception as e:  # pragma: no cover - defensive
        print(f"⚠️  tool-call dialect normalization skipped: "
              f"{type(e).__name__}: {e}", flush=True)
        return 0
    return repaired

# --- Credential scrubbing ----------------------------------------------------
# Credentials reach the logs because the geo-agent `query` MCP tool accepts
# s3_key/s3_secret in its arguments, which flow through `tool_calls`, tool
# results and the `messages` array. Scrub before anything is logged. This is
# always on, independent of capture mode — observability logs leak secrets too.
# Implementation lives in scrub.py so the live path and the historical scrub
# job (scrub-historical-logs.py) share one source of truth and never diverge.
from scrub import scrub as _scrub, scrub_text as _scrub_text, REDACTED as _REDACTED

# --- Logging health (#39) -------------------------------------------------
# A *one-sided* pipeline failure is invisible from the outside: the corpus just
# quietly goes lopsided. In #37 a variable-shadowing bug made `log_response`
# throw inside `@_never_raises`, so every response was dropped from S3 for hours
# while requests logged normally, and nobody noticed until the data looked odd.
#
# Both guardrails below are observability only — they never change what is
# served, and never change /health's `status` field, which backs the liveness,
# readiness *and* startup probes. A logging fault must not restart a pod that is
# happily serving traffic.
_LOG_RATIO_MIN_REQUESTS = int(os.getenv("LOG_RATIO_MIN_REQUESTS", "20"))
_LOG_RATIO_FLOOR = float(os.getenv("LOG_RATIO_FLOOR", "0.5"))

_log_counters = {"requests": 0, "responses": 0}      # current window, reset per flush
_log_health = {
    "last_window": None,
    "windows_checked": 0,
    "imbalance_windows": 0,
    "swallowed_exceptions": 0,
    "swallowed_by_fn": {},
}


def _emit(log_entry: dict):
    """Print log entry and add to S3 buffer."""
    _log_buffer.append(log_entry)
    # Count here, not at the call sites. This is the one point where an entry
    # actually lands in the buffer, so a `log_response` that throws *before*
    # reaching it goes uncounted — which is precisely the #37 signature we want
    # the ratio to expose. Counting on entry to the log functions would mask it.
    kind = log_entry.get("type")
    if kind == "request":
        _log_counters["requests"] += 1
    elif kind == "response":
        _log_counters["responses"] += 1


def _log(prefix: str, log_entry: dict):
    """Single print + buffer path for both log_request and log_response (#39).

    They previously did this independently, which let the two sides drift.
    Note this would NOT have prevented #37 — that bug was at the call site,
    passing a bad `client` — so it is robustness, not a fix for that failure.
    """
    print(f"{prefix}: {json.dumps(log_entry if not _S3_ENABLED else _stdout_view(log_entry))}",
          flush=True)
    _emit(log_entry)


def check_log_balance():
    """Evaluate request:response balance for the window just elapsed (#39).

    Healthy traffic emits ~1 response per request. A sustained shortfall means
    one side stopped writing. Returns the window summary (or None if idle), and
    resets the counters.
    """
    reqs, resps = _log_counters["requests"], _log_counters["responses"]
    _log_counters["requests"] = _log_counters["responses"] = 0

    if not reqs and not resps:
        return None

    ratio = (resps / reqs) if reqs else None
    window = {"requests": reqs, "responses": resps,
              "ratio": round(ratio, 3) if ratio is not None else None}
    _log_health["last_window"] = window
    _log_health["windows_checked"] += 1

    # Require a floor of volume: with a handful of requests a single in-flight
    # turn straddling the window boundary would trip a naive ratio check.
    if reqs >= _LOG_RATIO_MIN_REQUESTS and ratio is not None and ratio < _LOG_RATIO_FLOOR:
        _log_health["imbalance_windows"] += 1
        print(f"🚨 Logging imbalance: {resps} responses for {reqs} requests "
              f"(ratio {ratio:.2f} < {_LOG_RATIO_FLOOR}) in the last "
              f"{_FLUSH_INTERVAL}s — one side of the logging pipeline may have "
              f"stopped writing. See #37/#39.", flush=True)
    return window

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
        # Outside _flush_to_s3 deliberately: that returns early when S3 is
        # disabled, and the balance check must run regardless of where logs go.
        check_log_balance()

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_flush_loop())
    yield
    task.cancel()
    await _flush_to_s3()  # final flush on shutdown

# No /docs, /redoc or /openapi.json: the API is documented in the README, and the
# schema only helped the November 2026 prober map the service.
app = FastAPI(title="Multi-Provider LLM Proxy", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

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
                "models": ["kimi", "qwen3", "glm-5"]
            },
            "openrouter": {
                "endpoint": "https://openrouter.ai/api/v1/chat/completions",
                "api_key_env": "OPENROUTER_KEY",
                "models": ["anthropic/", "mistralai/", "amazon/", "openai/", "qwen/", "nvidia/", "z-ai/", "minimax/", "moonshotai/", "deepseek/", "xiaomi/", "~"],
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
PROXY_KEY = os.getenv("PROXY_KEY")  # Primary client key (prod apps)
CACHE_SALT = os.getenv("CACHE_SALT")  # Optional: isolate cached responses per deployment


# Client keys and their names (#162). Every request is attributed to the name of
# the key that sent it, so one app's or one person's usage can be measured,
# limited and cut off without touching the others. The name is logged; the key
# never is.
#
# Sources, merged:
#   PROXY_KEY          the legacy shared key, named "primary".
#   PROXY_KEYS_EXTRA   comma-separated; each entry `name:key`, or a bare `key`
#                      (named "unnamed-<sha256 prefix>" so it is still traceable).
#   PROXY_KEYS_FILE    one `tier/name:key` (or `name:key`) per line, `#` comments
#                      allowed. Meant to be a
#                      mounted Secret: kubelet refreshes it in place, and the proxy
#                      re-reads it when its mtime changes, so issuing or revoking a
#                      key needs no restart.
# A revoked key gets the same 401 as an unknown one.
_KEY_NAME_RE = re.compile(r"^(?:[a-z][a-z0-9-]{0,31}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _parse_key_entry(entry: str, where: str):
    """`[tier/]name:key` or bare `key` -> (key, "[tier/]name"), or None."""
    entry = entry.strip()
    if not entry or entry.startswith("#"):
        return None
    name, sep, key = entry.partition(":")
    if not sep:
        return entry, f"unnamed-{_key_fingerprint(entry)}"
    name, key = name.strip(), key.strip()
    if not key or not _KEY_NAME_RE.match(name):
        print(f"⚠️  Ignoring malformed client key entry in {where} (name {name!r})")
        return None
    return key, name


def parse_client_keys(primary, extra, file_text=None) -> dict:
    """Map of accepted client key -> its name. Later sources win on a duplicate key."""
    keys = {}
    if primary and primary.strip():
        keys[primary.strip()] = "primary"
    for entry in (extra or "").split(","):
        parsed = _parse_key_entry(entry, "PROXY_KEYS_EXTRA")
        if parsed:
            keys[parsed[0]] = parsed[1]
    for line in (file_text or "").splitlines():
        parsed = _parse_key_entry(line, "PROXY_KEYS_FILE")
        if parsed:
            keys[parsed[0]] = parsed[1]
    return keys


def compute_valid_keys(primary, extra):
    """Set of accepted client keys from PROXY_KEY and PROXY_KEYS_EXTRA."""
    return frozenset(parse_client_keys(primary, extra))


PROXY_KEYS_FILE = os.getenv("PROXY_KEYS_FILE")
_client_keys = {"mtime": None, "keys": {}}


def client_keys() -> dict:
    """Current key -> name map, re-reading PROXY_KEYS_FILE if it changed."""
    mtime, text = None, None
    if PROXY_KEYS_FILE:
        try:
            mtime = os.stat(PROXY_KEYS_FILE).st_mtime
        except OSError:
            mtime = None
    if _client_keys["keys"] and mtime == _client_keys["mtime"]:
        return _client_keys["keys"]
    if mtime is not None:
        try:
            with open(PROXY_KEYS_FILE) as f:
                text = f.read()
        except OSError as e:
            print(f"⚠️  Could not read PROXY_KEYS_FILE: {e}")
    keys = parse_client_keys(PROXY_KEY, os.getenv("PROXY_KEYS_EXTRA", ""), text)
    if _client_keys["mtime"] is not None or mtime is not None:
        print(f"🔑 Client keys loaded: {len(keys)} ({', '.join(sorted(set(keys.values())))})")
    _client_keys.update(mtime=mtime, keys=keys)
    return keys


def key_identity(authorization):
    """(name, tier) of the client key in an `Authorization: Bearer` header, or None.

    The tier comes from the key's `tier/` prefix in the keys file. Without one,
    the legacy shared key gets `primary_key_tier` and any other key gets
    `default_key_tier`, which is "public": an unmarked key never gets more
    than the public list."""
    if not authorization:
        return None
    key = authorization.replace("Bearer ", "", 1).strip()
    raw = client_keys().get(key) if key else None
    if not raw:
        return None
    tier, _, name = raw.rpartition("/")
    if not tier:
        tier = PRIMARY_KEY_TIER if name == "primary" else DEFAULT_KEY_TIER
    return name, tier


def key_name_for(authorization) -> Optional[str]:
    """Name of the client key in an `Authorization: Bearer` header, or None."""
    ident = key_identity(authorization)
    return ident[0] if ident else None


# End-user address as forwarded by the app's nginx (`X-Real-IP`). Only as
# trustworthy as the caller that set it: the apps derive it from HAProxy's
# X-Forwarded-For, but a direct in-cluster caller can send anything.
#   CLIENT_IP_LOG=hash (default)  salted sha256 prefix; needs CLIENT_IP_SALT,
#                                 and logs nothing without one.
#   CLIENT_IP_LOG=raw             the address itself.
#   CLIENT_IP_LOG=off             nothing.
CLIENT_IP_LOG = (os.getenv("CLIENT_IP_LOG") or "hash").strip().lower()
CLIENT_IP_SALT = os.getenv("CLIENT_IP_SALT")


def client_ip_for_log(headers) -> Optional[str]:
    ip = (headers.get("x-real-ip") or "").strip()
    if not ip or CLIENT_IP_LOG == "off":
        return None
    if CLIENT_IP_LOG == "raw":
        return ip
    if not CLIENT_IP_SALT:
        return None
    return hashlib.sha256(f"{CLIENT_IP_SALT}:{ip}".encode()).hexdigest()[:16]


# Build PROVIDERS dictionary from config
def build_provider_entry(provider_config: dict) -> dict:
    """Normalize one provider's config into the dict used at request time."""
    api_key_env = provider_config.get("api_key_env")
    api_key = os.getenv(api_key_env) if api_key_env else None

    return {
        "endpoint": provider_config["endpoint"],
        "api_key": api_key,
        # Exact model ids this provider serves.
        "models": provider_config.get("models", []),
        # Prefixes it claims, declared separately (#110). Historically `models`
        # did double duty — every bare entry also acted as a prefix — which is
        # subtle and was actively dangerous: nimbus declaring the exact id
        # `qwen` silently claimed `qwen3`, `qwen3-small`, `qwen3-4bit` and
        # `qwen3-embedding` too, and the only thing stopping it was NRP listing
        # each of those explicitly so the exact pass matched first. Deleting
        # those "redundant" NRP entries would have rerouted four production
        # models to a different backend, with no error.
        #
        # A provider that declares `model_prefixes` gets the new semantics:
        # `models` is exact-only. One that doesn't keeps the old double duty,
        # so third-party/legacy configs (and cirrus's ConfigMap) are unaffected.
        "prefixes": provider_config.get("model_prefixes", provider_config.get("models", [])),
        "extra_headers": provider_config.get("extra_headers", {}),
        "thinking_models": provider_config.get("thinking_models", {}),
        # Models that 400 on sampling params (temperature/top_p) — e.g. the newest
        # Anthropic models. Must be carried through here: this rebuilt dict (not the
        # raw config) is what get_provider_for_model returns at request time.
        "no_sampling_params": provider_config.get("no_sampling_params", []),
        # Model-discovery overrides (#111): most providers expose an
        # OpenAI-style /v1/models taking a Bearer token, derived from the chat
        # endpoint. Anthropic needs x-api-key; some may need an explicit URL.
        "models_auth": provider_config.get("models_auth"),
        "models_endpoint": provider_config.get("models_endpoint"),
        "anthropic_version": provider_config.get("anthropic_version"),
        # Output-token bounds (#150). `default_max_tokens` is sent when the client
        # sends none; `max_tokens_ceiling` clamps what a client may ask for. Set per
        # provider, not globally: vLLM (NRP, nimbus) 400s when prompt + max_tokens
        # exceeds the model's context, so a blanket default would break the
        # small-context models, while the paid providers are where a runaway
        # costs money.
        "default_max_tokens": provider_config.get("default_max_tokens"),
        "max_tokens_ceiling": provider_config.get("max_tokens_ceiling"),
    }


PROVIDERS = {
    name: build_provider_entry(cfg) for name, cfg in config["providers"].items()
}

# Provider that serves a model id matching no explicit entry.
#
# This was a hard-coded "nrp" for the life of the proxy, which is load-bearing on
# the NRP deployment: most of `nrp.models` is redundant with this fallback, and
# undeclared-but-live ids have always reached NRP through it. Keeping "nrp" as the
# default preserves that behavior exactly.
#
# It also *only* worked on a deployment that serves NRP. Anywhere else the lookup
# raised KeyError inside the request handler — a 500 with no useful body — which is
# what the cirrus deployment hits for every bare NRP model id. Deployments that
# don't serve NRP now name their own fallback, or set it to null/"" to reject
# unroutable ids outright with a 400 that says what *is* served.
def resolve_default_provider(cfg: dict, providers: dict):
    """Pick the fallback provider, or None to reject unroutable ids with a 400.

    Absent key -> "nrp", preserving the historical hard-coded behavior on NRP.
    A named-but-unconfigured provider degrades to None with a warning rather than
    blowing up at request time — which is exactly how a deployment that doesn't
    serve NRP (cirrus) behaves with no `default_provider` set at all.
    """
    name = cfg.get("default_provider", "nrp")
    if not name:
        return None
    if name not in providers:
        print(f"ℹ️  default_provider '{name}' is not configured here — "
              f"unroutable model ids will be rejected with 400")
        return None
    return name


DEFAULT_PROVIDER = resolve_default_provider(config, PROVIDERS)


# Provider for a *namespaced* id (`vendor/model`) that matches no explicit entry
# (#154). Bare ids and namespaced ids fail in opposite directions under a single
# fallback: NRP publishes bare ids and never a slash-namespaced vendor id, while
# OpenRouter's catalog is the authority for `vendor/model` and grows weekly. With
# only `default_provider`, every new OpenRouter vendor needed a hand-added prefix,
# and a forgotten one (`reflection/`) silently went to NRP instead.
#
# Absent key -> "openrouter" when this deployment configures it, else no
# fallback: a namespaced id that routes nowhere is a 400, never a quiet trip to
# `default_provider`. Set it to null/"" to require declared prefixes again.
def resolve_namespaced_default_provider(cfg: dict, providers: dict):
    """Pick the fallback for unmatched `vendor/model` ids, or None for a 400."""
    name = cfg.get("namespaced_default_provider", "openrouter")
    if not name or name not in providers:
        return None
    return name


NAMESPACED_DEFAULT_PROVIDER = resolve_namespaced_default_provider(config, PROVIDERS)


# Model tiers (#163). The proxy keeps two curated model lists, one for public
# apps and one for private (signed-in) apps, and nothing per app. Every client
# key belongs to a tier, so the list an app offers and the models it may call
# are set here, once, for every app of that kind. Public apps are open relays
# by construction — anyone on the internet can POST to their /api/llm/ — so the
# public list holds only lab- and NRP-hosted models.
#
#   "tiers": {"public":  {"default_model": "deepseek-v4-flash", "models": [...]},
#             "private": {"default_model": "deepseek-v4-flash", "models": [...]}}
#
# `models` entries are exact ids, or a prefix ending in `*`. A model outside the
# caller's tier gets a 403; `GET /v1/models` returns the tier's list, in order,
# with the default flagged. No `tiers` key -> every routable model is allowed
# (cirrus and other deployments keep their behavior).
def resolve_tiers(cfg: dict):
    """Map of tier -> {"default_model", "models"}, or None when unconfigured."""
    tiers = cfg.get("tiers")
    if not tiers:
        return None
    out = {}
    for name, spec in tiers.items():
        models = tuple(spec.get("models") or [])
        default = spec.get("default_model") or next((m for m in models if not m.endswith("*")), None)
        if default and default not in models:
            print(f"⚠️  tiers.{name}.default_model {default!r} is not in its models list")
        out[name] = {"default_model": default, "models": models}
    return out


TIERS = resolve_tiers(config)
DEFAULT_KEY_TIER = config.get("default_key_tier", "public")
# The legacy shared PROXY_KEY is still what every app sends until each moves to
# its own key (#162). It stays "private" for that migration only, so private
# apps keep working; set it to "public" (or revoke the key) once they have moved.
PRIMARY_KEY_TIER = config.get("primary_key_tier", "private")


def model_in_tier(model: str, tier) -> bool:
    """True if `tier` may use `model`. Unknown tier -> False."""
    if TIERS is None:
        return True
    spec = TIERS.get(tier)
    if spec is None:
        return False
    for pat in spec["models"]:
        if model == pat or (pat.endswith("*") and model.startswith(pat[:-1])):
            return True
    return False


def tier_model_list(tier) -> list:
    """The tier's listable models (exact ids that route), in configured order."""
    spec = (TIERS or {}).get(tier) or {"models": (), "default_model": None}
    out = []
    for mid in spec["models"]:
        if mid.endswith("*"):
            continue
        try:
            provider = get_provider_for_model(mid)[0]
        except UnknownModelError:
            continue
        out.append({"id": mid, "object": "model", "owned_by": provider,
                    "provider": provider, "default": mid == spec["default_model"]})
    return out


# Daily token budgets and a deny-list (#168). Alerts (#165) say when to look;
# these stop the spend. Counted per client key, per end-user address (the app
# nginx's X-Real-IP) and per signed-in user (X-Forwarded-Email), each against its
# tier's limit over a rolling window:
#
#   "budgets": {"window_hours": 24,
#               "per_key":    {"public": 10000000, "private": 50000000},
#               "per_client": {"public": 3000000,  "private": 10000000},
#               "per_user":   {"public": 3000000,  "private": 10000000}}
#
# A tier missing from a scope has no limit there. Address and user are counted
# separately, so a caller who rotates a forged X-Forwarded-Email still hits the
# address budget. Tokens are prompt + completion as the provider reports them.
#
# The counters must be shared: the NRP deployment runs 3 pods x 4 workers, and
# counters kept in each process would let a caller spend 12x the budget. So they
# live in Redis (BUDGET_REDIS_URL). Without it they fall back to process memory,
# with a warning. If Redis errors mid-flight the request is allowed (fail open)
# and the error logged: a budget outage should not take the apps down.
def resolve_budgets(cfg: dict):
    b = cfg.get("budgets")
    if not b:
        return None
    return {"window_hours": int(b.get("window_hours", 24)),
            "per_key": dict(b.get("per_key") or {}),
            "per_client": dict(b.get("per_client") or {}),
            "per_user": dict(b.get("per_user") or {})}


BUDGETS = resolve_budgets(config)
BUDGET_REDIS_URL = os.getenv("BUDGET_REDIS_URL")


class MemoryBudgetStore:
    """Per-process hourly buckets. Exact in tests; a 12x-loose cap in production."""

    def __init__(self):
        self.buckets = {}

    async def window(self, counters, hours):
        return [self.buckets.get((c, h), 0) for c in counters for h in hours]

    async def charge(self, counters, hour, tokens, ttl_s):
        for c in counters:
            self.buckets[(c, hour)] = self.buckets.get((c, hour), 0) + tokens


class RedisBudgetStore:
    def __init__(self, url):
        import redis.asyncio as aioredis  # imported lazily: only needed when configured
        self.r = aioredis.from_url(url, socket_timeout=2, socket_connect_timeout=2)

    async def window(self, counters, hours):
        vals = await self.r.mget([f"tok:{c}:{h}" for c in counters for h in hours])
        return [int(v) if v else 0 for v in vals]

    async def charge(self, counters, hour, tokens, ttl_s):
        pipe = self.r.pipeline(transaction=False)
        for c in counters:
            pipe.incrby(f"tok:{c}:{hour}", tokens)
            pipe.expire(f"tok:{c}:{hour}", ttl_s)
        await pipe.execute()


_budget_store = None
_budget_errors = 0


def budget_store():
    global _budget_store
    if _budget_store is None:
        if BUDGET_REDIS_URL:
            _budget_store = RedisBudgetStore(BUDGET_REDIS_URL)
        else:
            _budget_store = MemoryBudgetStore()
    return _budget_store


def budget_counters(tier, key_name, client_ip, user):
    """[(counter id, limit, label)] that apply to this caller."""
    if not BUDGETS:
        return []
    out = []
    for scope, ident in (("key", key_name), ("client", client_ip), ("user", user)):
        limit = BUDGETS[f"per_{scope}"].get(tier)
        if ident and limit:
            out.append((f"{scope}:{ident}", int(limit), scope))
    return out


async def check_budget(counters, now=None):
    """None if within budget, else (label, used, limit, reset_epoch)."""
    global _budget_errors
    if not counters:
        return None
    now = time.time() if now is None else now
    cur = int(now // 3600)
    hours = list(range(cur - BUDGETS["window_hours"] + 1, cur + 1))
    try:
        vals = await budget_store().window([c for c, _, _ in counters], hours)
    except Exception as e:  # fail open
        _budget_errors += 1
        if _budget_errors in (1, 10, 100) or _budget_errors % 1000 == 0:
            print(f"⚠️  budget store unavailable ({_budget_errors}x), not enforcing: {type(e).__name__}: {e}", flush=True)
        return None
    n = len(hours)
    for i, (_, limit, label) in enumerate(counters):
        row = vals[i * n:(i + 1) * n]
        used = sum(row)
        if used >= limit:
            first = next(h for h, v in zip(hours, row) if v)
            return label, used, limit, (first + BUDGETS["window_hours"]) * 3600
    return None


async def charge_budget(counters, tokens, now=None):
    global _budget_errors
    if not counters or not tokens:
        return
    now = time.time() if now is None else now
    try:
        await budget_store().charge([c for c, _, _ in counters], int(now // 3600), int(tokens),
                                    (BUDGETS["window_hours"] + 1) * 3600)
    except Exception as e:
        _budget_errors += 1
        if _budget_errors in (1, 10, 100) or _budget_errors % 1000 == 0:
            print(f"⚠️  budget charge failed ({_budget_errors}x): {type(e).__name__}: {e}", flush=True)


# Deny-list: a mounted ConfigMap (DENYLIST_FILE), re-read when it changes, so one
# key, address or user can be blocked in about a minute without a restart or a
# key rotation. One entry per line, `#` comments allowed:
#   key tpl                    a client key, by name
#   client 529b5c72100fa917    an address, as logged in `client_ip`
#   user 3f0c...               a signed-in user, as logged in `user`
DENYLIST_FILE = os.getenv("DENYLIST_FILE")
_denylist = {"mtime": None, "entries": frozenset()}


def denylist() -> frozenset:
    if not DENYLIST_FILE:
        return frozenset()
    try:
        mtime = os.stat(DENYLIST_FILE).st_mtime
    except OSError:
        return frozenset()
    if mtime != _denylist["mtime"]:
        entries = set()
        try:
            with open(DENYLIST_FILE) as f:
                for line in f:
                    parts = line.split("#", 1)[0].split()
                    if len(parts) == 2 and parts[0] in ("key", "client", "user"):
                        entries.add((parts[0], parts[1]))
        except OSError as e:
            print(f"⚠️  Could not read DENYLIST_FILE: {e}")
        _denylist.update(mtime=mtime, entries=frozenset(entries))
        print(f"🚫 Deny-list loaded: {len(entries)} entries")
    return _denylist["entries"]


def is_denied(key_name, client_ip, user) -> bool:
    d = denylist()
    return bool(d) and (("key", key_name) in d or ("client", client_ip) in d or ("user", user) in d)


def user_for_log(headers) -> Optional[str]:
    """Signed-in user (oauth2-proxy's X-Forwarded-Email via the app nginx),
    salted-hashed like the address. Only trustworthy where that nginx strips a
    client-supplied value; budgets count address and user separately for that
    reason."""
    email = (headers.get("x-forwarded-email") or "").strip().lower()
    if not email or not CLIENT_IP_SALT:
        return None
    return hashlib.sha256(f"{CLIENT_IP_SALT}:user:{email}".encode()).hexdigest()[:16]


def effective_max_tokens(requested, provider_config: dict):
    """Client value clamped to the provider's ceiling, else the provider default."""
    ceiling = provider_config.get("max_tokens_ceiling")
    value = requested if requested is not None else provider_config.get("default_max_tokens")
    if value is not None and ceiling is not None:
        value = min(value, ceiling)
    return value



class UnknownModelError(Exception):
    """No provider matches this model id, and the deployment has no usable default.

    Carried as an exception rather than a sentinel return so the request handler can
    turn it into a 400 (client asked for something we don't serve) instead of the
    KeyError-driven 500 this used to be.
    """

    def __init__(self, model: str):
        self.model = model
        served = "; ".join(
            f"{name} ({', '.join(cfg['models'][:6])}{'…' if len(cfg['models']) > 6 else ''})"
            for name, cfg in PROVIDERS.items()
        )
        hint = (" Namespaced `vendor/model` ids route to `namespaced_default_provider`,"
                " which this deployment does not configure."
                if "/" in model else "")
        super().__init__(
            f"Unknown model '{model}'. This deployment serves: {served}.{hint}"
        )


# Log configuration status
print("=" * 60)
print("🚀 Multi-Provider LLM Proxy Starting")
print("=" * 60)
for provider, config in PROVIDERS.items():
    has_key = bool(config["api_key"])
    status = "✓" if has_key else "✗"
    print(f"{status} {provider.upper()}: {config['endpoint']} (key: {'set' if has_key else 'MISSING'})")
if not client_keys():
    print("⚠️  WARNING: no client keys (PROXY_KEY, PROXY_KEYS_EXTRA, PROXY_KEYS_FILE) - proxy will reject all requests!")
else:
    _names = sorted(set(client_keys().values()))
    print(f"✓ Accepting {len(client_keys())} client keys: {', '.join(_names)}")
if CLIENT_IP_LOG == "hash" and not CLIENT_IP_SALT:
    print("ℹ️  CLIENT_IP_LOG=hash but CLIENT_IP_SALT is unset - client addresses are not logged")
if TIERS is None:
    print("ℹ️  No model tiers configured: every routable model is allowed")
else:
    print("✓ Model tiers: " + "; ".join(
        f"{t} ({len(v['models'])} models, default {v['default_model']})" for t, v in TIERS.items())
        + f". Unmarked keys: {DEFAULT_KEY_TIER}; shared PROXY_KEY: {PRIMARY_KEY_TIER}")
if BUDGETS:
    print(f"✓ Token budgets over {BUDGETS['window_hours']}h: key {BUDGETS['per_key']}, "
          f"client {BUDGETS['per_client']}, user {BUDGETS['per_user']}; store: "
          + ("redis" if BUDGET_REDIS_URL else "PROCESS MEMORY (limits are per worker, not shared)"))
if CACHE_SALT:
    print("✓ CACHE_SALT configured - responses isolated from other NRP tenants")
else:
    print("ℹ️  CACHE_SALT not set - cached responses may be shared with other NRP users")
print("=" * 60)

def get_provider_for_model(model: str) -> tuple[str, dict]:
    """Route a model id to a provider: exact ids first, then declared prefixes,
    then the namespaced fallback for `vendor/model` ids (#154), then the
    deployment's default provider for bare ids (#110)."""
    # Exact ids win over any prefix — this is what keeps a private endpoint
    # (`qwen` on nimbus, `gemma4`, `qwen3-6`) from being swallowed by a broader
    # vendor prefix declared elsewhere.
    for provider_name, config in PROVIDERS.items():
        if model in config["models"]:
            return provider_name, config

    # Then declared prefixes — vendor families like `anthropic/`, `z-ai/`, `~`
    # for OpenRouter's floating aliases, and `claude-` for direct Anthropic.
    for provider_name, config in PROVIDERS.items():
        for model_prefix in config["prefixes"]:
            if model.startswith(model_prefix):
                return provider_name, config

    # A namespaced id goes to the namespaced fallback, or nowhere — never to
    # `default_provider`, which serves bare ids (#154).
    if "/" in model:
        if NAMESPACED_DEFAULT_PROVIDER:
            return NAMESPACED_DEFAULT_PROVIDER, PROVIDERS[NAMESPACED_DEFAULT_PROVIDER]
        raise UnknownModelError(model)

    # Fall back to the deployment's default provider. This is the *designed*
    # route for the default provider's own models, not an anomaly: `nrp.models`
    # is deliberately empty, because listing NRP's ids bought nothing (they all
    # land here anyway) while making the config a maintenance treadmill — most
    # of the churn in this file's history was model-list edits. So no per-request
    # warning: it would fire on every single NRP call.
    #
    # The cost is that a typo'd id also lands here and fails upstream instead of
    # being caught locally. `GET /v1/models` (#111) is the real fix for that.
    if DEFAULT_PROVIDER:
        return DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER]
    raise UnknownModelError(model)

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
            n = _log_health["swallowed_by_fn"].get(fn.__name__, 0) + 1
            _log_health["swallowed_by_fn"][fn.__name__] = n
            _log_health["swallowed_exceptions"] += 1
            print(f"⚠️  {fn.__name__} failed (request still served): "
                  f"{type(e).__name__}: {e}", flush=True)
            # One failure is a serialization edge case; a pattern is a bug
            # silently eating the corpus (#37). Escalate on a log scale so a
            # persistent fault stays loud without flooding a busy log.
            if n in (10, 100, 1000) or (n > 1000 and n % 1000 == 0):
                print(f"🚨 {fn.__name__} has now failed {n} times — logging is "
                      f"persistently broken, not a one-off. See #39.", flush=True)
    return wrapper

@_never_raises
def log_request(provider: str, model: str, messages: List[Dict], tools_count: int = 0, origin: str = None, request_id: str = None, session_id: str = None, client: str = None, enable_thinking: bool = None, key_name: str = None, client_ip: str = None, tier: str = None, user: str = None):
    """Log incoming request in structured JSON format"""
    # Extract the original user question (first human message, stable across all turns)
    user_question = next(
        (m.get("content", "") for m in messages if m.get("role") == "user"),
        ""
    )
    # Extract THIS turn's user message (the LAST human message). session_id persists
    # across a whole browsing day, so `user_question` above is only the session
    # opener, repeated on every subsequent turn; this field carries the actual
    # prompt that triggered the current turn, making distinct mid-session requests
    # countable and readable from the logs (#89).
    user_message_this_turn = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
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
        "key_name": key_name,       # name of the client key (#162); never the key
        "client_ip": client_ip,     # X-Real-IP, hashed unless CLIENT_IP_LOG=raw
        "tier": tier,               # model tier of the key: public / private (#163)
        "user": user,               # signed-in user (X-Forwarded-Email), salted hash
        "provider": provider,
        "model": model,
        "origin": origin,
        "message_count": len(messages),
        "tools_count": tools_count,
        # Requested thinking mode (what the client asked for), distinct from the
        # observed reasoning trace on the response side. null = client didn't send
        # the flag / model default; True/False = explicit request override.
        "enable_thinking": enable_thinking,
        "user_question": _scrub_text(_cap(user_question, _USER_QUESTION_MAX)),
        "user_message_this_turn": _scrub_text(_cap(user_message_this_turn, _USER_QUESTION_MAX)),
        "tool_results_this_turn": list(reversed(tool_results)) if tool_results else None,
    }
    # Training-grade fidelity: capture the entire (scrubbed, system-deduped)
    # prompt so (messages -> completion) pairs can be reconstructed by request_id.
    if _CAPTURE_MODE == "full":
        log_entry["messages"] = _dedup_messages(messages, origin=origin)
    _log("📥 REQUEST", log_entry)

@_never_raises
def log_response(provider: str, model: str, response_data: dict, latency_ms: int, error: str = None, origin: str = None, request_id: str = None, session_id: str = None, client: str = None, upstream_headers: dict = None, dialect_repaired: int = 0, key_name: str = None, client_ip: str = None, tier: str = None, user: str = None):
    """Log response in structured JSON format"""
    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "type": "response",
        "request_id": request_id,
        "session_id": session_id,
        "client": client,
        "key_name": key_name,
        "client_ip": client_ip,
        "tier": tier,
        "user": user,
        "provider": provider,
        "model": model,
        "origin": origin,
        "latency_ms": latency_ms,
    }
    
    if error:
        log_entry["error"] = error
        # Allow-listed upstream response headers (#44) — present only on the
        # HTTPStatusError path, where the upstream actually returned a response.
        if upstream_headers:
            log_entry["upstream_headers"] = upstream_headers
    else:
        # Extract response details
        if "choices" in response_data and len(response_data["choices"]) > 0:
            message = response_data["choices"][0].get("message", {})
            content = _scrub_text(message.get("content") or "")
            # Reasoning trace field name is provider-dependent: NRP ellm (qwen3 etc.)
            # emits `reasoning_content`; the nimbus vLLM endpoint emits `reasoning`.
            # Prefer `reasoning_content`, fall back to `reasoning`, so the trace is
            # captured (and `has_reasoning_content` is accurate) for both (#66).
            raw_reasoning = message.get("reasoning_content") or message.get("reasoning")
            reasoning = _scrub_text(raw_reasoning or "")
            log_entry["has_content"] = bool(message.get("content"))
            log_entry["has_tool_calls"] = bool(message.get("tool_calls"))
            log_entry["has_reasoning_content"] = bool(raw_reasoning)
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
        
        # How many tool-call arguments were repaired of leaked arg dialect (#85).
        # Kept queryable so the leak rate stays measurable even though the markup
        # itself no longer reaches the logs.
        if dialect_repaired:
            log_entry["tool_call_dialect_repaired"] = dialect_repaired

        # The upstream host that actually served the call. OpenRouter fans one
        # model id out across many providers (DeepInfra, Novita, ...) and names
        # the one it picked in the body's top-level `provider`; `provider` above
        # is only our route ("openrouter"). Absent for NRP/nimbus.
        if response_data.get("provider"):
            log_entry["upstream_provider"] = response_data["provider"]

        # Extract token usage if available
        if "usage" in response_data:
            log_entry["tokens"] = response_data["usage"]
    
    status = "✗" if error else "✓"
    _log(f"{status} RESPONSE", log_entry)

# Per-request budget for the upstream provider call. 20 minutes (#135), raised
# from a hardcoded 600s: 408 requests died at *exactly* ~600s in the last 60 days
# (nrp 259, nimbus 143, vllm-cirrus 6), which is this timeout, not the provider.
#
# Only `read` gets the long budget. httpx's `read` is the max wait for a *chunk*
# of data, so for a self-hosted vLLM that emits nothing until generation
# completes it behaves as a hard total cap — which is why those 408 died here,
# while OpenRouter (which sends keepalive bytes that keep resetting the read
# clock) has logged successes out past 1700s. connect/write/pool stay short: a
# TCP connect that takes 20 minutes is a broken path, not a slow model.
#
# NOTE the ceiling is only as high as the lowest hop. For nimbus- and
# cirrus-backed models, Traefik's `responseHeaderTimeout: 600s` still binds
# (boettiger-lab/k8s#42) until that is raised too.
_UPSTREAM_READ_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "1200"))
_UPSTREAM_TIMEOUT = httpx.Timeout(
    _UPSTREAM_READ_TIMEOUT, connect=30.0, write=30.0, pool=30.0
)

# Threshold for the "slow completion" warning below (#82). This is *not* a cap
# this proxy controls or enforces — it is a guess at the tightest deadline a
# caller is likely to have, used only to flag a logged 200 the client may never
# have received.
#
# There is no single right value: each app fronts this proxy with its own nginx
# sidecar, and they differ (`geo-agent-template` 300s; `ca-30x30` 600s), while
# geo-agent's browser client aborts on its own `llm_timeout_seconds` (600s
# default) regardless of the sidecar. The effective deadline is the minimum of
# the two, per app. Default to the tightest known sidecar so the warning
# over-reports rather than misses; override per deployment to match its own
# chain.
#
# Parsed defensively: this knob only decides whether a log line prints, so a
# typo'd or empty value must not take the proxy down at import. (An empty string
# is a common manifest idiom — `value: ""` — and float("") raises.)
def _slow_completion_warn_seconds() -> float:
    raw = (os.getenv("SLOW_COMPLETION_WARN_SECONDS") or "").strip()
    if not raw:
        return 300.0
    try:
        return float(raw)
    except ValueError:
        print(f"⚠️  Ignoring non-numeric SLOW_COMPLETION_WARN_SECONDS={raw!r} — "
              f"falling back to 300s", flush=True)
        return 300.0


_SLOW_COMPLETION_WARN_SECONDS = _slow_completion_warn_seconds()
_SLOW_COMPLETION_WARN_MS = _SLOW_COMPLETION_WARN_SECONDS * 1000

class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]  # Accept any message format from OpenAI API
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[str] = "auto"
    model: Optional[str] = "gpt-4"
    temperature: Optional[float] = 0.0
    enable_thinking: Optional[bool] = None  # None = use model default; True/False to override
    user: Optional[str] = None  # OpenAI end-user id; geo-agent sets it to its per-session UUID. Logged as session_id (not forwarded upstream).
    # Declared only so it can be REJECTED explicitly (#129). This proxy is
    # deliberately non-streaming: it buffers the upstream completion and rebuilds
    # the log record from the finished JSON body. Pydantic's default is
    # extra="ignore", so before this field existed a client sending `stream: true`
    # got HTTP 200 with a non-streaming body — an OpenAI client waiting for SSE
    # cannot parse that, and the failure looked like a client bug. Fail loudly
    # instead. See proxy_chat() for the rejection.
    stream: Optional[bool] = None
    # Known-safe sampling/routing knobs forwarded verbatim when present (#47).
    # Without these, anything outside the whitelist below was silently dropped:
    # `seed`/`top_p` (determinism), `stop`/`max_tokens`/`response_format` (output
    # shaping), `usage` + OpenRouter `provider` (cost/cache/ZDR routing).
    top_p: Optional[float] = None
    seed: Optional[int] = None
    stop: Optional[Any] = None  # str or list[str] per OpenAI spec
    max_tokens: Optional[int] = None
    response_format: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None  # e.g. OpenRouter {"include": true}
    provider: Optional[Dict[str, Any]] = None  # OpenRouter routing block (zdr/order/only/...)

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
    if not client_keys():
        raise HTTPException(status_code=500, detail="No client keys configured on server")

    ident = key_identity(authorization)
    if not ident:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing proxy key")
    key_name, tier = ident
    client_ip = client_ip_for_log(http_request.headers)
    user = user_for_log(http_request.headers)
    request_id = uuid.uuid4().hex[:8]
    if is_denied(key_name, client_ip, user):
        log_response("blocked", request.model, {}, 0, error="deny-list",
                     origin=http_request.headers.get("origin") or http_request.headers.get("referer"),
                     request_id=request_id, key_name=key_name, client_ip=client_ip, tier=tier, user=user)
        raise HTTPException(status_code=403, detail="Forbidden")

    # Caller identity, derived before any early return: the three rejections below
    # (streaming, unroutable model, missing provider key) each log a response row,
    # and a row without `origin` cannot be traced to the app that sent it — which is
    # the whole point of logging the rejection. This used to be computed after
    # routing, so those rows carried "origin": null.
    origin = http_request.headers.get("origin") or http_request.headers.get("referer")
    # Session id: prefer the OpenAI `user` body field (geo-agent already sends its
    # per-session UUID there); fall back to the X-Session-Id header for other clients.
    session_id = request.user or http_request.headers.get("x-session-id")
    client = http_request.headers.get("x-client")   # e.g. "geo-agent/v3.13.1"; null until clients send it

    # Streaming is not supported, and saying so is better than pretending (#129).
    # Logged against a synthetic provider — as with unrouted models — because the
    # request never reaches log_request, and "who is asking for streaming?" is
    # otherwise unanswerable from the corpus.
    if request.stream:
        error_msg = (
            "Streaming is not supported by this proxy: it buffers each completion to "
            "log the request/response pair. Omit `stream` or set it to false."
        )
        log_response("streaming-unsupported", request.model, {}, 0, error=error_msg,
                     origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user)
        raise HTTPException(status_code=400, detail=error_msg)

    # Determine provider based on model
    try:
        provider_name, provider_config = get_provider_for_model(request.model)
    except UnknownModelError as e:
        # Client asked for something this deployment doesn't serve. Log it against a
        # synthetic provider so the miss is visible in the logs (the request never
        # reaches log_request, which runs after routing succeeds).
        log_response("unrouted", request.model, {}, 0, error=str(e),
                     origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user)
        raise HTTPException(status_code=400, detail=str(e))

    if not model_in_tier(request.model, tier):
        listed = ", ".join(m["id"] for m in tier_model_list(tier)) or "none"
        error_msg = (f"Model '{request.model}' is not available to {tier} apps. "
                     f"Available: {listed}.")
        log_response("disallowed", request.model, {}, 0, error=error_msg,
                     origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user)
        raise HTTPException(status_code=403, detail=error_msg)

    counters = budget_counters(tier, key_name, client_ip, user)
    over = await check_budget(counters)
    if over:
        label, used, limit, reset = over
        resets = datetime.utcfromtimestamp(reset).strftime("%Y-%m-%d %H:%M UTC")
        error_msg = (f"Daily token budget exceeded for this {label} "
                     f"({used:,} of {limit:,} tokens in {BUDGETS['window_hours']}h). "
                     f"It starts freeing up at {resets}.")
        log_response("budget-exceeded", request.model, {}, 0, error=error_msg,
                     origin=origin, request_id=request_id, session_id=session_id, client=client,
                     key_name=key_name, client_ip=client_ip, tier=tier, user=user)
        raise HTTPException(status_code=429, detail=error_msg,
                            headers={"Retry-After": str(max(60, int(reset - time.time())))})
    endpoint = provider_config["endpoint"]
    api_key = provider_config["api_key"]
    
    if not api_key:
        error_msg = f"{provider_name.upper()} API key not configured on server"
        log_response(provider_name, request.model, {}, 0, error=error_msg,
                     origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user)
        raise HTTPException(status_code=500, detail=error_msg)

    # Log incoming request
    log_request(provider_name, request.model, request.messages, len(request.tools or []), origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user, enable_thinking=request.enable_thinking)
    
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
    }

    # Sampling params: the newest Anthropic models (Sonnet 5, Opus 4.8/4.7, Fable 5,
    # ...) reject `temperature`/`top_p`/`top_k` with a 400 — they were removed, not
    # deprecated. `no_sampling_params` lists (per provider) the model IDs that must
    # not receive them; matched exact-then-prefix like model routing. Everything else
    # keeps the forced `temperature: 0.0` default (#33) for eval determinism.
    no_sampling = provider_config.get("no_sampling_params", [])
    rejects_sampling = request.model in no_sampling or any(
        request.model.startswith(p) for p in no_sampling
    )
    if not rejects_sampling:
        payload["temperature"] = request.temperature

    # Add tools if provided
    if request.tools:
        payload["tools"] = request.tools
        payload["tool_choice"] = request.tool_choice

    # Forward known-safe sampling/output knobs when the client sends them (#47).
    # Only set keys that are present (non-None) so provider defaults are untouched.
    max_tokens = effective_max_tokens(request.max_tokens, provider_config)
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    for field in ("top_p", "seed", "stop", "response_format"):
        value = getattr(request, field)
        if value is not None:
            if field == "top_p" and rejects_sampling:
                continue  # same 400 as temperature on these models
            payload[field] = value

    # OpenRouter-only knobs: the `provider` routing block (zdr / order / only /
    # require_parameters, ...) and top-level `usage` ({"include": true}). Both are
    # OpenRouter-isms — meaningless, and potentially rejected by strict
    # OpenAI-compatible servers (e.g. vllm), elsewhere — so guard by provider.
    if provider_name == "openrouter":
        if request.provider is not None:
            payload["provider"] = request.provider
        if request.usage is not None:
            payload["usage"] = request.usage

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
    
    # Make request to LLM provider. NB: name this `http_client`, NOT `client` —
    # `client` is the X-Client header value passed to log_response() below; an
    # `as client` here shadowed it with the AsyncClient object, so json.dumps in
    # log_response raised and every response was silently dropped from S3 (#37).
    async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as http_client:
        try:
            response = await http_client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()

            # Repair leaked tool-call arg dialect (#85) before returning OR
            # logging, so neither the client nor the log ever sees the markup.
            dialect_repaired = _normalize_response_tool_calls(result)
            if dialect_repaired:
                print(f"🧹 Normalized {dialect_repaired} tool-call argument(s) with "
                      f"leaked arg dialect (model={request.model}, request_id={request_id})",
                      flush=True)

            # Log successful response
            latency_ms = int((time.time() - start_time) * 1000)
            # Because we call upstream non-streaming, zero bytes flow until the whole
            # completion is buffered — so a turn slower than the caller's own deadline
            # (its nginx sidecar's proxy_read_timeout, or geo-agent's client-side
            # llm_timeout_seconds, whichever is tighter) has already failed for the
            # user even though *we* succeed. Flag it so a logged 200 the client may
            # never have received is greppable in pod logs (#82). "may" is load-bearing:
            # the threshold is a fleet-wide guess, not this caller's actual deadline,
            # and latency_ms is proxy-side either way — see the constant above.
            if latency_ms > _SLOW_COMPLETION_WARN_MS:
                print(f"⚠️  Slow completion: {latency_ms}ms exceeds the "
                      f"{_SLOW_COMPLETION_WARN_SECONDS:g}s client-side warning threshold "
                      f"(model={request.model}, request_id={request_id}) — the caller may "
                      f"already have given up on this turn and seen a gateway 502/504",
                      flush=True)
            log_response(provider_name, request.model, result, latency_ms, origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user, dialect_repaired=dialect_repaired)

            usage = result.get("usage") or {}
            await charge_budget(counters, (usage.get("prompt_tokens") or 0)
                                + (usage.get("completion_tokens") or 0))
            return result

        except asyncio.CancelledError:
            # The caller (typically the app's nginx sidecar, on its own
            # proxy_read_timeout) gave up and closed the connection while we were
            # still awaiting upstream, so uvicorn cancelled this handler. CancelledError is a BaseException, so
            # the `except Exception` below never caught it — the request vanished
            # from S3 (only the pre-flight request row remained), which is exactly
            # why the client-facing nginx 502s were invisible here (#82). Log it,
            # then re-raise so the cancellation still propagates correctly.
            latency_ms = int((time.time() - start_time) * 1000)
            error_detail = f"Client disconnected/cancelled after {latency_ms}ms (upstream still pending)"
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user)
            raise

        except httpx.TimeoutException as e:
            latency_ms = int((time.time() - start_time) * 1000)
            error_detail = f"Request timed out after {latency_ms}ms"
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user)
            raise HTTPException(status_code=504, detail=error_detail)

        except httpx.HTTPStatusError as e:
            latency_ms = int((time.time() - start_time) * 1000)
            error_detail = f"Provider returned {e.response.status_code}: {e.response.text[:1000]}"
            # Capture allow-listed upstream headers so the rate-limit (429 +
            # retry-after/x-ratelimit-*) vs dead-backend (naked 500, no
            # server/x-request-id) distinction is queryable from logs (#44).
            upstream_headers = _capture_upstream_headers(e.response.headers)
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user, upstream_headers=upstream_headers)

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
            log_response(provider_name, request.model, {}, latency_ms, error=error_detail, origin=origin, request_id=request_id, session_id=session_id, client=client, key_name=key_name, client_ip=client_ip, tier=tier, user=user)
            
            # Use 502 Bad Gateway for connection errors (more accurate than 500)
            # 500 should only be for internal proxy errors
            raise HTTPException(status_code=502, detail=f"Connection error: {error_detail}")

@app.options("/v1/chat/completions")
@app.options("/chat")
async def options_chat():
    """Handle CORS preflight for chat endpoints"""
    return Response(status_code=204)

# --- Model discovery (#111) --------------------------------------------------
# Providers already enumerate themselves, so the proxy should not keep a list —
# #110 deleted the ones it had. This asks each configured provider what it serves
# and reports the union, annotated by provider.
#
# The routable set is filtered *through `get_provider_for_model` itself*, so the
# listing cannot drift from actual routing: an id appears under a provider only
# if the router would really send it there. A provider's own catalog may be far
# larger than what we route, and that filter is what keeps the two honest. (Since
# #154 every namespaced OpenRouter id routes there, so its whole catalog lists.)
_MODELS_TTL = int(os.getenv("MODELS_CACHE_TTL", "300"))
_MODELS_TIMEOUT = float(os.getenv("MODELS_FETCH_TIMEOUT", "8"))
_models_cache = {"data": [], "providers": {}, "fetched_at": 0.0, "fetched_iso": None}
_models_lock = asyncio.Lock()


def _models_url(provider_config: dict) -> str:
    """Derive the provider's model-listing URL from its chat endpoint."""
    explicit = provider_config.get("models_endpoint")
    if explicit:
        return explicit
    ep = provider_config["endpoint"]
    if "/chat/completions" in ep:
        return ep.replace("/chat/completions", "/models")
    return ep.rstrip("/") + "/models"


def _models_headers(provider_config: dict) -> dict:
    """Auth for the listing call. Most providers take a Bearer token; Anthropic's
    OpenAI-compat path rejects it (`Invalid bearer token`) and its native listing
    wants `x-api-key` plus a version header — verified against the live API."""
    key = provider_config.get("api_key") or ""
    if provider_config.get("models_auth") == "x-api-key":
        # `or` not `.get(default)`: build_provider_entry stores the key as None
        # when unset, so a default argument would never apply and httpx would be
        # handed a None header value. Caught by live verification, not unit tests.
        version = provider_config.get("anthropic_version") or "2023-06-01"
        return {"x-api-key": key, "anthropic-version": version}
    return {"Authorization": f"Bearer {key}"} if key else {}


async def _fetch_provider_models(cfg: dict, client) -> tuple:
    """Return (ids, error). Never raises — an unreachable provider must degrade,
    not break the endpoint."""
    try:
        r = await client.get(_models_url(cfg), headers=_models_headers(cfg),
                             timeout=_MODELS_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        ids = [m.get("id") for m in payload.get("data", [])
               if isinstance(m, dict) and m.get("id")]
        return ids, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:200]


async def refresh_models(force: bool = False) -> dict:
    """Refresh the model cache if stale. Returns the cache."""
    now = time.monotonic()
    if not force and _models_cache["data"] and (now - _models_cache["fetched_at"]) < _MODELS_TTL:
        return _models_cache

    async with _models_lock:
        now = time.monotonic()
        if not force and _models_cache["data"] and (now - _models_cache["fetched_at"]) < _MODELS_TTL:
            return _models_cache

        names = list(PROVIDERS)
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *(_fetch_provider_models(PROVIDERS[n], client) for n in names)
            )

        data, provider_status = [], {}
        for name, (ids, error) in zip(names, results):
            cfg = PROVIDERS[name]
            previous = _models_cache["providers"].get(name, {})
            if ids is None:
                # Unreachable: keep the last good answer if we have one, else fall
                # back to whatever the config declares. Never drop the provider —
                # a down endpoint is still a routing target once it returns.
                ids = previous.get("ids") or list(cfg.get("models", []))
                status = "stale" if previous.get("ids") else "declared"
            else:
                status = "ok"
            # Only keep ids this deployment would actually route *here*.
            routable = []
            for mid in ids:
                try:
                    if get_provider_for_model(mid)[0] == name:
                        routable.append(mid)
                except UnknownModelError:
                    continue
            provider_status[name] = {
                "status": status,
                "count": len(routable),
                "prefixes": list(cfg.get("prefixes", [])),
                "ids": routable,
            }
            if error:
                provider_status[name]["error"] = error
            data.extend({"id": mid, "object": "model", "owned_by": name,
                         "provider": name} for mid in routable)

        _models_cache.update({
            "data": data,
            "providers": provider_status,
            "fetched_at": time.monotonic(),
            "fetched_iso": datetime.utcnow().isoformat() + "Z",
        })
        return _models_cache


@app.get("/v1/models")
async def list_models(authorization: str = Header(None), refresh: bool = False):
    """OpenAI-shaped list of everything this deployment can route.

    Lets a client populate a model picker from the proxy instead of hardcoding a
    list that goes stale independently — and makes the *deployment's* reachable
    set visible, which is what the cirrus failover story needs (#102): the same
    app pointed at a narrower deployment sees a narrower list, rather than
    offering ids that will 400.
    """
    if not client_keys():
        raise HTTPException(status_code=500, detail="PROXY_KEY not configured on server")
    ident = key_identity(authorization)
    if not ident:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing proxy key")

    if TIERS is not None:
        # The tier's curated list *is* the answer: an app's picker shows exactly
        # what its tier may call, default first-class. No upstream catalog fetch,
        # so callers can't make the proxy call every provider (`?refresh=true`).
        data = tier_model_list(ident[1])
        return {"object": "list", "data": data, "tier": ident[1],
                "default_model": next((m["id"] for m in data if m["default"]), None)}

    cache = await refresh_models(force=refresh)
    return {
        "object": "list",
        "data": cache["data"],
        # Non-standard, additive: OpenAI clients read `data` and ignore the rest.
        # `prefixes` matters because a family like `claude-` is routable without
        # any concrete id being enumerable.
        "providers": {
            name: {k: v for k, v in info.items() if k != "ids"}
            for name, info in cache["providers"].items()
        },
        "fetched_at": cache["fetched_iso"],
    }


@app.get("/health")
async def health_check():
    """Health check endpoint (logging suppressed)"""
    providers_status = {
        name: {"configured": bool(config["api_key"]), "endpoint": config["endpoint"]}
        for name, config in PROVIDERS.items()
    }
    return {
        # `status` is deliberately untouched by logging health: this endpoint
        # backs the liveness, readiness AND startup probes, and a logging fault
        # must never restart or de-rotate a pod that is serving fine (#39).
        "status": "healthy",
        "providers": providers_status,
        "proxy_key_configured": bool(PROXY_KEY),
        "logging": {
            "last_window": _log_health["last_window"],
            "windows_checked": _log_health["windows_checked"],
            "imbalance_windows": _log_health["imbalance_windows"],
            "swallowed_exceptions": _log_health["swallowed_exceptions"],
            "swallowed_by_fn": _log_health["swallowed_by_fn"],
            "buffer_depth": len(_log_buffer),
        },
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
