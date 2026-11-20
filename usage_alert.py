"""Post to Slack when one key, address or user's usage jumps (#165).

Runs as a CronJob next to the proxy's Redis (usage-alert-cronjob.yaml). It reads
the token counters the budgets already keep there (#168): output tokens per
client key, per visitor address and per signed-in user, in hourly buckets over
the budget window. A counter over its alert level in config.json `alerts` posts
one message; it posts again only when usage doubles or `repeat_hours` pass, and
once more when the counter falls back under its level.

The alert levels sit well under the budgets on purpose. A budget cuts a caller
off; an alert says to go and look while there is still room, so that real growth
(many people on one app) can be told apart from abuse (one address) before
anyone gets a 429.

Exits non-zero, and records nothing, if Redis or Slack fails, so the next run
tries again and the failed Job is visible in `kubectl get jobs`.

    python usage_alert.py            # post to SLACK_WEBHOOK_URL
    DRY_RUN=1 python usage_alert.py  # print what it would post
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

SCOPES = ("key", "client", "user")
STATE_PREFIX = "alert:usage:"
STATE_TTL_S = 14 * 24 * 3600   # Redis evicts keys with a TTL first; state outlives any incident


def load_settings(cfg: dict) -> dict:
    """The alert levels and the counter window, from config.json."""
    alerts = cfg.get("alerts") or {}
    budgets = cfg.get("budgets") or {}
    levels = {s: int(alerts[f"per_{s}"]) for s in SCOPES if alerts.get(f"per_{s}")}
    return {
        "levels": levels,
        "window_hours": int(budgets.get("window_hours", 4)),
        # Same counter ids the proxy writes: output-only counters carry `out/`.
        "prefix": "out/" if budgets.get("count", "total") == "completion" else "",
        "repeat_hours": float(alerts.get("repeat_hours", 24)),
    }


def window_usage(r, prefix: str, window_hours: int, now: float) -> dict:
    """{(scope, ident): tokens} summed over the window's hourly buckets."""
    cur = int(now // 3600)
    hours = set(range(cur - window_hours + 1, cur + 1))
    usage = {}
    keys = list(r.scan_iter(match=f"tok:{prefix}*", count=1000))
    if not keys:
        return usage
    vals = r.mget(keys)
    for k, v in zip(keys, vals):
        k = k.decode() if isinstance(k, bytes) else k
        try:
            counter, hour = k[len("tok:") + len(prefix):].rsplit(":", 1)
            scope, ident = counter.split(":", 1)
            hour = int(hour)
        except ValueError:
            continue
        if scope in SCOPES and hour in hours and v:
            usage[(scope, ident)] = usage.get((scope, ident), 0) + int(v)
    return usage


def _fmt(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


WHAT_TO_DO = (
    "Many people on one app: raise `budgets.per_key` in config.json. "
    "One address or user: add `client <hash>` or `user <hash>` to the deny-list. "
    "A leaked or misused key: revoke it. Both are in the README under Token budgets "
    "and Client keys."
)


def message(kind: str, scope: str, ident: str, used: int, level: int, settings: dict, where: str) -> str:
    w = settings["window_hours"]
    who = {"key": f"key `{ident}`", "client": f"address `client {ident}`",
           "user": f"user `user {ident}`"}[scope]
    if kind == "resolved":
        return (f":white_check_mark: open-llm-proxy ({where}): {who} is back under its alert level: "
                f"{_fmt(used)} output tokens in the last {w}h (alert at {_fmt(level)}).")
    return (f":rotating_light: open-llm-proxy ({where}): {who} used {_fmt(used)} output tokens "
            f"in the last {w}h (alert at {_fmt(level)}).\n{WHAT_TO_DO}")


def run(r, settings: dict, post, where: str, now: float = None) -> list:
    """Compare every counter with its level, post what changed, keep state in
    Redis. Returns the messages posted. `post(text)` must raise on failure."""
    now = time.time() if now is None else now
    usage = window_usage(r, settings["prefix"], settings["window_hours"], now)
    posted = []

    firing = {}
    for k in r.scan_iter(match=f"{STATE_PREFIX}*", count=1000):
        k = k.decode() if isinstance(k, bytes) else k
        raw = r.get(k)
        if raw:
            firing[k] = json.loads(raw)

    for (scope, ident), used in sorted(usage.items()):
        level = settings["levels"].get(scope)
        if not level or used < level:
            continue
        sid = f"{STATE_PREFIX}{scope}:{ident}"
        prev = firing.pop(sid, None)
        if prev and used < 2 * prev["posted_used"] and now - prev["posted_at"] < settings["repeat_hours"] * 3600:
            continue
        text = message("firing", scope, ident, used, level, settings, where)
        post(text)
        posted.append(text)
        state = {"since": (prev or {}).get("since", now), "posted_at": now, "posted_used": used}
        r.set(sid, json.dumps(state), ex=STATE_TTL_S)

    # What was firing and is no longer over its level has settled.
    for sid, prev in firing.items():
        scope, ident = sid[len(STATE_PREFIX):].split(":", 1)
        level = settings["levels"].get(scope)
        used = usage.get((scope, ident), 0)
        if level and used >= level:
            continue   # still over, quiet this run
        text = message("resolved", scope, ident, used, level or 0, settings, where)
        post(text)
        posted.append(text)
        r.delete(sid)
    return posted


def slack_poster(url: str):
    def post(text: str):
        req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Slack webhook answered HTTP {resp.status}")
    return post


def main() -> int:
    cfg = json.loads((Path(__file__).parent / "config.json").read_text())
    settings = load_settings(cfg)
    if not settings["levels"]:
        print("No `alerts` levels in config.json; nothing to check.")
        return 0
    dry = bool(os.getenv("DRY_RUN"))
    url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not dry and not url.startswith("https://hooks.slack.com/"):
        print("FATAL: SLACK_WEBHOOK_URL is unset or not a Slack incoming webhook.", file=sys.stderr)
        return 2
    post = (lambda text: print("DRY RUN would post:\n" + text)) if dry else slack_poster(url)

    import redis
    r = redis.Redis.from_url(os.environ["BUDGET_REDIS_URL"], socket_timeout=10, socket_connect_timeout=10)
    posted = run(r, settings, post, where=os.getenv("POD_NAMESPACE", "?"))
    print(f"checked {', '.join(f'{s} >= {_fmt(l)}' for s, l in settings['levels'].items())} "
          f"over {settings['window_hours']}h; posted {len(posted)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
