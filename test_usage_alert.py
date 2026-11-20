"""Tests for usage_alert.py (#165). Run: python test_usage_alert.py"""
import fnmatch
import json

import usage_alert as ua

H = 3600
NOW = 1_000_000 * H + 1800          # half past some hour
CUR = int(NOW // H)


class FakeRedis:
    def __init__(self):
        self.d = {}

    def scan_iter(self, match="*", count=None):
        return [k for k in list(self.d) if fnmatch.fnmatchcase(k, match)]

    def mget(self, keys):
        return [self.d.get(k) for k in keys]

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v, ex=None):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)


SETTINGS = ua.load_settings({
    "budgets": {"window_hours": 4, "count": "completion"},
    "alerts": {"per_key": 1_000_000, "per_client": 250_000, "repeat_hours": 24},
})


def _charge(r, counter, hour, n):
    k = f"tok:out/{counter}:{hour}"
    r.d[k] = str(int(r.d.get(k, 0)) + n)


def test_settings_follow_budget_counters():
    assert SETTINGS["prefix"] == "out/" and SETTINGS["window_hours"] == 4
    assert SETTINGS["levels"] == {"key": 1_000_000, "client": 250_000}
    total = ua.load_settings({"budgets": {}, "alerts": {"per_user": 5}})
    assert total["prefix"] == "" and total["levels"] == {"user": 5}


def test_window_sums_only_the_last_hours():
    r = FakeRedis()
    _charge(r, "key:app-a", CUR, 100)
    _charge(r, "key:app-a", CUR - 3, 50)
    _charge(r, "key:app-a", CUR - 4, 999)            # outside a 4h window
    _charge(r, "client:529b5c72100fa917", CUR, 7)
    r.d["tok:key:old-total-counter:%d" % CUR] = "5"  # not an output counter
    u = ua.window_usage(r, "out/", 4, NOW)
    assert u == {("key", "app-a"): 150, ("client", "529b5c72100fa917"): 7}


def test_fires_once_then_quiet_then_escalates_then_resolves():
    r, posts = FakeRedis(), []
    _charge(r, "key:app-a", CUR, 1_200_000)
    _charge(r, "key:app-b", CUR, 900_000)              # under its level
    assert len(ua.run(r, SETTINGS, posts.append, "biodiversity", NOW)) == 1
    assert "key `app-a`" in posts[0] and "1.20M" in posts[0] and "app-b" not in posts[0]

    # Still over, not doubled, within repeat_hours: silent.
    _charge(r, "key:app-a", CUR, 300_000)
    assert ua.run(r, SETTINGS, posts.append, "biodiversity", NOW + 600) == []

    # Doubled since the last post: post again.
    _charge(r, "key:app-a", CUR, 1_000_000)
    assert len(ua.run(r, SETTINGS, posts.append, "biodiversity", NOW + 1200)) == 1

    # The window rolls past the burst: resolved, state cleared.
    out = ua.run(r, SETTINGS, posts.append, "biodiversity", NOW + 5 * H)
    assert len(out) == 1 and "back under" in out[0]
    assert not [k for k in r.d if k.startswith(ua.STATE_PREFIX)]


def test_repeats_after_repeat_hours():
    r, posts = FakeRedis(), []
    _charge(r, "client:abc", CUR, 300_000)
    ua.run(r, SETTINGS, posts.append, "x", NOW)
    state = json.loads(r.d[ua.STATE_PREFIX + "client:abc"])
    state["posted_at"] -= 25 * H                       # last post a day ago
    r.d[ua.STATE_PREFIX + "client:abc"] = json.dumps(state)
    assert len(ua.run(r, SETTINGS, posts.append, "x", NOW)) == 1


def test_failed_post_records_nothing():
    r = FakeRedis()
    _charge(r, "key:app-a", CUR, 2_000_000)

    def boom(text):
        raise RuntimeError("Slack down")
    try:
        ua.run(r, SETTINGS, boom, "x", NOW)
    except RuntimeError:
        pass
    assert not [k for k in r.d if k.startswith(ua.STATE_PREFIX)], "next run must try again"


def test_shipped_config_has_alert_levels_under_the_budgets():
    cfg = json.load(open("config.json"))
    s = ua.load_settings(cfg)
    assert set(s["levels"]) == {"key", "client", "user"}
    for scope, level in s["levels"].items():
        assert all(level < b for b in cfg["budgets"][f"per_{scope}"].values()), scope


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
