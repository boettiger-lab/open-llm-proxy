"""Credential scrubbing shared by the live proxy and the historical scrub job.

Credentials reach the logs because the geo-agent `query` MCP tool accepts
s3_key/s3_secret in its arguments, which flow through `tool_calls`, tool results
and the `messages` array. The live proxy (`llm_proxy.py`) scrubs before anything
is logged; the one-off `scrub-historical-logs.py` job applies the *same* logic to
already-written S3 records. Keeping it in one module guarantees they never drift.
"""
import json
import re
from typing import Any, Optional

REDACTED = "[REDACTED]"

# Redact the *value* of any dict key whose name looks credential-bearing.
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(s3[_-]?secret|s3[_-]?key|secret[_-]?access[_-]?key|access[_-]?key[_-]?id"
    r"|aws[_-]?secret|aws[_-]?access|api[_-]?key|apikey|secret|password|passwd"
    r"|token|authorization|auth[_-]?token|bearer)"
)

# Catch secrets embedded in free text (SQL the model wrote, JSON-in-a-string
# tool arguments, DuckDB CREATE SECRET statements, Authorization headers).
TEXT_PATTERNS = [
    # key: "value" / key='value' / "s3_secret": "..." / s3_secret: bareval
    # (handles \" escaping and unquoted values; quote-optional on both sides)
    (re.compile(
        r"""(?ix)(\\?["']?(?:s3[_-]?secret|s3[_-]?key|secret[_-]?access[_-]?key
        |access[_-]?key[_-]?id|aws[_-]?secret|aws[_-]?access|api[_-]?key|apikey
        |secret|password|token)\\?["']?\s*[:=]\s*\\?["']?)([^"'\s\\,}]+)"""),
     r"\1" + REDACTED),
    # DuckDB: KEY_ID '...'  /  SECRET '...'
    (re.compile(r"(?i)\b(KEY_ID|SECRET)\s+'([^']+)'"), r"\1 '" + REDACTED + "'"),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1" + REDACTED),
]


def scrub_text(s: str) -> str:
    for pat, repl in TEXT_PATTERNS:
        s = pat.sub(repl, s)
    return s


def scrub(obj: Any, _key: Optional[str] = None) -> Any:
    """Recursively redact credentials from a JSON-serialisable structure.

    - Any dict value under a sensitive-looking key is fully redacted.
    - Remaining strings are regex-scrubbed for embedded secrets.
    - OpenAI tool-call `arguments` are a JSON *string*; parse, scrub, re-dump
      so nested s3_key/s3_secret args get key-based redaction too.
    """
    if isinstance(obj, dict):
        return {k: scrub(v, _key=k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, _key=_key) for v in obj]
    if isinstance(obj, str):
        if _key and SENSITIVE_KEY_RE.fullmatch(_key):
            return REDACTED
        # Stringified JSON (e.g. tool_call arguments): scrub structurally.
        stripped = obj.strip()
        if stripped[:1] in ("{", "[") and _key in ("arguments", "content"):
            try:
                return json.dumps(scrub(json.loads(obj)))
            except (json.JSONDecodeError, ValueError):
                pass
        return scrub_text(obj)
    return obj


def scrub_entry(entry: str) -> str:
    """Scrub one log record stored as a JSON string (the Parquet `entry` column
    or a raw JSONL line). Falls back to a text scrub if it isn't valid JSON."""
    try:
        obj = json.loads(entry)
    except (json.JSONDecodeError, ValueError):
        return scrub_text(entry)
    return json.dumps(scrub(obj))


def contains_secret(s: str) -> bool:
    """True if any credential pattern still matches — used to verify a scrub."""
    if not isinstance(s, str):
        return False
    return any(pat.search(s) for pat, _ in TEXT_PATTERNS)
