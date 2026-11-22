"""Fleet view data: pure functions from budget, usage history and decision log."""

from datetime import datetime, timedelta, timezone

import pytest

from claude_swap import fleet_view
from claude_swap.fleet_view import build_fleet, model_label, square

SP = timezone(timedelta(hours=-3))  # Sao Paulo offset; a fixed zone needs no tzdata on Windows
H = 3600
NOW = datetime(2026, 11, 21, 22, 0, tzinfo=timezone.utc).timestamp()  # 19:00 in SP


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def history(*rows) -> dict:
    """rows: (epoch, model, project, weighted tokens). Bucket start must be a 300 s multiple."""
    buckets: dict = {}
    for epoch, model, project, tokens in rows:
        b = buckets.setdefault(str(int(epoch)), {"m": {}, "p": {}})
        b["m"][model] = b["m"].get(model, 0.0) + tokens
        b["p"][project] = b["p"].get(project, 0.0) + tokens
    return {"schemaVersion": 1, "updatedAt": iso(NOW), "files": {}, "buckets": buckets}


EMPTY = history()


def account(num, plan="pro", week_reset=None, remaining=None, tpp=None, remaining_pct=50.0,
            five_reset=None, five_pct=0.0):
    out = {"id": num, "plan": plan}
    if week_reset is not None:
        seven = {"pct": 100 - remaining_pct, "remainingPct": remaining_pct, "resetsAt": iso(week_reset)}
        if remaining is not None:
            seven["remainingTokens"] = remaining
        if tpp is not None:
            seven["tokensPerPercent"] = tpp
        out["sevenDay"] = seven
    if five_reset is not None:
        out["fiveHour"] = {"pct": five_pct, "resetsAt": iso(five_reset)}
    return out


def budget(accounts=(), pace=0.87, used=58.2, elapsed=66.9, usable=1, usable_tokens=None, horizons=None):
    return {
        "fleet": {"paceRatio": pace, "weekUsedPct": used, "weekElapsedPct": elapsed,
                  "usableNow": usable, "accounts": len(accounts)},
        "accounts": list(accounts),
        "horizons": horizons or [],
        "tokens": None if usable_tokens is None else {"usableNowTokens": usable_tokens},
    }


def fleet(b=None, h=EMPTY, decisions=(), period="7d", now=NOW):
    return build_fleet(b or budget(), h, list(decisions), now, period=period, tz=SP)


def test_pace_and_status_in_rhythm():
    out = fleet(budget(pace=0.87, used=58.2, elapsed=66.9))
    assert out["pace"] == 87
    assert out["status"] == {"word": "No ritmo", "level": "neutral"}
    assert out["caption"] == "Onde a semana da frota fecha nesse ritmo · 58% usado com 67% do tempo"


@pytest.mark.parametrize("pace,word,level", [
    (1.06, "Acaba antes do reset", "critical"),
    (0.74, "Vai sobrar cota", "warning"),
    (None, "Semana nova", "neutral"),
    (1.05, "No ritmo", "neutral"),
    (0.75, "No ritmo", "neutral"),
    (1.054, "Acaba antes do reset", "critical"),  # rounded 105 would read neutral
    (0.746, "Vai sobrar cota", "warning"),  # rounded 75 would read neutral
])
def test_status_borders(pace, word, level):
    out = fleet(budget(pace=pace))
    assert (out["status"]["word"], out["status"]["level"]) == (word, level)
    assert out["pace"] == (None if pace is None else round(pace * 100))


def sustainable_account(per_min=30000, minutes=600, num=1):
    return account(num, week_reset=NOW + minutes * 60, remaining=per_min * minutes, tpp=250000)


def test_now_tiles():
    h = history(
        (NOW - 3600, "claude-opus-5-5", "a", 9_000_000),   # starts at now-3600: outside
        (NOW - 3000, "claude-opus-5-5", "a", 1_000_000),
        (NOW - 300, "claude-opus-5-5", "a", 1_000_000),
        (NOW, "claude-opus-5-5", "a", 520_000),            # starts at now: inside
    )
    out = fleet(budget([sustainable_account()], usable_tokens=101_000_000), h)
    tiles = out["tiles"]
    assert tiles["nowPerMin"] == 42000.0
    assert tiles["ratio"] == 1.4
    assert round(tiles["lastsHours"], 2) == 40.08
    assert tiles["usableNowTokens"] == 101_000_000
    assert len(tiles["nowSpark24h"]) == 24


def test_now_tiles_unknowns():
    # no consumption: lastsHours None; no sustainable rate: ratio None; no tokens: lastsHours None
    out = fleet(budget([sustainable_account()], usable_tokens=1_000_000))
    assert out["tiles"]["nowPerMin"] == 0.0 and out["tiles"]["lastsHours"] is None
    h = history((NOW - 300, "m", "a", 600_000))
    out = fleet(budget([], usable_tokens=None), h)
    assert out["tiles"]["ratio"] is None and out["tiles"]["lastsHours"] is None
    assert out["tiles"]["usableNowTokens"] is None


def test_usable_tile_numbers():
    b = budget([account(1), account(2), account(3)], usable=2)
    t = fleet(b)["tiles"]
    assert (t["usable"], t["accounts"]) == (2, 3)


def test_spark24h_hourly_per_minute():
    h = history((NOW - 300, "m", "a", 600_000), (NOW - 23 * H - 600, "m", "a", 1_200_000))
    spark = fleet(h=h)["tiles"]["nowSpark24h"]
    assert spark[-1] == 10000.0          # 600000 over a 60 min bucket of 60 min
    assert spark[0] == 20000.0
    assert sum(1 for v in spark if v) == 2
    assert square(budget(), h, NOW)["sparkline24h"] == spark


def test_square():
    h = history((NOW - 300, "m", "a", 600_000))
    s = square(budget(pace=0.86), h, NOW)
    assert (s["pace"], s["level"]) == (86, "neutral")
    assert len(s["sparkline24h"]) == 24 and s["sparkline24h"][-1] == 10000.0
    assert square(budget(pace=1.2), h, NOW)["level"] == "critical"
    assert square(budget(pace=0.5), h, NOW)["level"] == "warning"
    unknown = square(budget(pace=None), EMPTY, NOW)
    assert unknown == {"sparkline24h": [0.0] * 24, "pace": None, "level": "neutral"}


def test_sustainable_rate_and_series_shape():
    accounts = [
        sustainable_account(30000, 600, 1),
        account(2, week_reset=NOW - 3600, remaining=1, tpp=10080),   # already past its reset
        account(3, week_reset=NOW + 3600, remaining=1000),            # no tokensPerPercent: adds nothing
    ]
    s = fleet(budget(accounts), period="1h")["series"]
    assert s["sustainable"] == 30000.0 + 100.0 * 10080 / 10080
    assert s["step"] == 300 and s["start"] == iso(NOW - 3600) and s["now"] == iso(NOW)
    assert len(s["past"]) == 12 and len(s["future"]) == 12


def test_sustainable_rate_floor_of_one_minute():
    b = budget([account(1, week_reset=NOW + 10, remaining=5000, tpp=100)])
    assert fleet(b, period="1h")["series"]["sustainable"] == 5000.0


def test_future_series_switches_after_reset():
    # 1d period, step 1800 s: reset at +3600 s: steps 0 and 1 before it, the rest after.
    b = budget([account(1, week_reset=NOW + 3600, remaining=600_000, tpp=10080)])
    f = fleet(b, period="1d")["series"]["future"]
    assert f[:2] == [10000.0, 10000.0]
    assert set(f[2:]) == {100.0}


@pytest.mark.parametrize("period,step,past,future", [
    ("1h", 300, 12, 12), ("6h", 300, 72, 72), ("12h", 900, 48, 48), ("1d", 1800, 48, 48),
    ("7d", 7200, 84, 36), ("15d", 21600, 60, 28), ("30d", 43200, 60, 14),
])
def test_forward_horizon_per_period(period, step, past, future):
    s = fleet(budget([sustainable_account()]), period=period)["series"]
    assert (s["step"], len(s["past"]), len(s["future"])) == (step, past, future)


def test_past_series_is_tokens_per_minute():
    h = history((NOW - 300, "m", "a", 1_500_000), (NOW - 3600, "m", "a", 300_000))
    past = fleet(h=h, period="1h")["series"]["past"]
    assert past[-1] == 300000.0 and past[0] == 60000.0
    assert sum(1 for v in past if v) == 2


HORIZONS = [
    {"hours": 24, "reachableTokens": 50_000_000},
    {"hours": 72, "reachableTokens": 90_000_000},
    {"hours": 168, "reachableTokens": 120_000_000},
]


def test_fuel_labels():
    b = budget([sustainable_account()], usable_tokens=1, horizons=HORIZONS)
    fuel = lambda p: fleet(b, period=p)["series"]["fuel"]  # noqa: E731
    assert fuel("1h") == []
    assert fuel("1d") == [{"label": "24 h", "tokens": 50_000_000, "hours": 24}]
    assert [f["label"] for f in fuel("7d")] == ["24 h", "3 dias"]
    assert fuel("30d") == [
        {"label": "24 h", "tokens": 50_000_000, "hours": 24},
        {"label": "3 dias", "tokens": 90_000_000, "hours": 72},
        {"label": "7 dias", "tokens": 120_000_000, "hours": 168},
    ]
    no_tokens = budget([sustainable_account()], usable_tokens=None, horizons=HORIZONS)
    assert fleet(no_tokens, period="30d")["series"]["fuel"] == []


def test_model_labels():
    assert model_label("claude-opus-5-5") == "Opus 5.5"
    assert model_label("claude-fable-5-2") == "Fable 5.2"
    assert model_label("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert model_label("claude-sonnet-5") == "Sonnet 5"
    assert model_label("gpt-x") == "gpt-x"
    assert model_label(None) == "unknown"


def test_period_drives_models():
    h = history(
        (NOW - 300, "claude-opus-5-5", "a", 1_000_000),
        (NOW - 3 * 86400, "claude-fable-5-2", "a", 3_000_000),
    )
    one = fleet(h=h, period="1h")["models"]
    assert [(m["label"], m["share"]) for m in one] == [("Opus 5.5", 100)]
    assert one[0]["id"] == "claude-opus-5-5" and one[0]["tokens"] == 1_000_000
    seven = fleet(h=h, period="7d")["models"]
    assert [m["label"] for m in seven] == ["Fable 5.2", "Opus 5.5"]   # sorted by tokens
    assert [m["share"] for m in seven] == [75, 25]


def test_models_drop_zero():
    h = history((NOW - 300, "claude-opus-5-5", "a", 1_000_000), (NOW - 600, "claude-haiku-4-5", "a", 0))
    assert [m["label"] for m in fleet(h=h, period="1h")["models"]] == ["Opus 5.5"]


def test_project_rows_top5_and_others():
    rows = [(NOW - 300 - 300 * i, "m", f"p{i}", 1000 * (10 - i)) for i in range(7)]
    out = fleet(h=history(*rows), period="1h")["projects"]
    keys = [r["key"] for r in out["rows"]]
    assert keys == ["p0", "p1", "p2", "p3", "p4", "outros (2)"]
    other = out["rows"][5]
    assert other["tokens"] == 1000 * 5 + 1000 * 4
    total = sum(1000 * (10 - i) for i in range(7))
    assert round(sum(r["share"] for r in out["rows"]), 6) == 100.0
    assert out["rows"][0]["share"] == pytest.approx(10000 / total * 100)
    assert len(out["columns"]) == 12 and all(len(r["cells"]) == 12 for r in out["rows"])
    assert out["rows"][0]["cells"][-1] == 10000.0


def test_project_rows_padded_with_empty():
    out = fleet(h=history((NOW - 300, "m", "p0", 5), (NOW - 300, "m", "p1", 3)), period="1h")["projects"]
    assert [r["key"] for r in out["rows"]] == ["p0", "p1", "", "", "", ""]
    assert out["rows"][5] == {"key": "", "cells": [], "tokens": 0, "share": 0}
    empty = fleet(period="1h")["projects"]
    assert len(empty["rows"]) == 6 and all(r["key"] == "" for r in empty["rows"])


def test_project_columns_by_local_day():
    # 2026-11-21T02:00Z is 23:00 on the 20th in Sao Paulo; the 21st starts at 03:00Z.
    h = history((ts("2026-11-21T02:00:00Z"), "m", "p0", 7), (ts("2026-11-21T04:00:00Z"), "m", "p0", 11))
    cols = fleet(h=h, period="7d")["projects"]["columns"]
    assert cols[0] == "2026-11-14T03:00:00Z" and cols[-1] == "2026-11-21T03:00:00Z" and len(cols) == 8
    cells = fleet(h=h, period="7d")["projects"]["rows"][0]["cells"]
    assert cells[-2] == 7.0 and cells[-1] == 11.0


def test_project_columns_by_period_step():
    assert len(fleet(period="6h")["projects"]["columns"]) == 24   # 15 min
    assert len(fleet(period="12h")["projects"]["columns"]) == 12  # 1 h
    assert len(fleet(period="1d")["projects"]["columns"]) == 24   # 1 h


def test_events_72h():
    accounts = [
        account(1, five_reset=NOW + 2 * H, five_pct=40, week_reset=NOW + 10 * H, remaining_pct=12.4),
        account(2, five_reset=NOW + 3 * H, five_pct=0),                    # nothing used: no event
        account(3, five_reset=NOW + 80 * H, five_pct=10, week_reset=NOW + 50 * H, remaining_pct=30),
        account(4, week_reset=NOW + 100 * H),                              # beyond 72 h
        account(5, five_reset=NOW + 45 * 60, five_pct=5),
        account(6, five_reset=NOW - 60, five_pct=5),                       # already past
    ]
    ev = fleet(budget(accounts))["events72h"]
    assert ev == [
        {"kind": "five", "account": 5, "at": iso(NOW + 45 * 60), "text": "conta 5 · janela de 5 h volta em 45 min"},
        {"kind": "five", "account": 1, "at": iso(NOW + 2 * H), "text": "conta 1 · janela de 5 h volta em 2 h"},
        {"kind": "week", "account": 1, "at": iso(NOW + 10 * H),
         "text": "conta 1 · semana renova · sobra 12% em 10 h"},
        {"kind": "week", "account": 3, "at": iso(NOW + 50 * H),
         "text": "conta 3 · semana renova · sobra 30% em 2 d"},
    ]


def poll(t, windows):
    return {"event": "poll", "ts": t, "active": {"number": 1}, "windowsPct": windows}


def switch(t, dry=False, trigger="proactive"):
    return {"event": "switch", "ts": t, "trigger": trigger, "dryRun": dry}


def test_switch_count_ignores_dry_run_and_other_events():
    dec = [
        poll("2026-11-16T16:51:44Z", {}),
        switch("2026-11-21T21:00:00Z"),             # today (SP), in period
        switch("2026-11-21T17:00:00Z", dry=True),   # dry run: not counted
        switch("2026-11-21T17:00:00Z"),
        switch("2026-11-21T01:00:00Z"),             # 22:00 on the 20th in SP: not today
        switch("2026-11-17T01:00:00Z"),             # before the period
    ]
    s = fleet(decisions=dec, period="1d")["switches"]
    assert s["count"] == 3 and s["today"] == 2
    assert s["logSince"] == "2026-11-16T16:51:44Z"
    assert len(s["bars"]) == 6
    assert [b["count"] for b in s["bars"]] == [1, 0, 0, 0, 1, 1]
    assert s["bars"][0]["start"] == iso(NOW - 86400) and s["bars"][1]["start"] == iso(NOW - 86400 + 4 * H)


def test_switch_bars_per_local_day_and_no_log():
    dec = [switch("2026-11-21T04:00:00Z"), switch("2026-11-21T05:00:00Z"), switch("2026-11-15T10:00:00Z")]
    s = fleet(decisions=dec, period="7d")["switches"]
    assert len(s["bars"]) == 8
    assert s["bars"][-1] == {"start": "2026-11-21T03:00:00Z", "count": 2}
    assert s["bars"][1] == {"start": "2026-11-15T03:00:00Z", "count": 1}
    assert s["count"] == 3
    none = fleet(period="7d")["switches"]
    assert none["logSince"] is None and none["count"] == 0 and none["today"] == 0


def unused_decisions():
    return [
        poll("2026-11-16T16:51:44Z", {}),
        poll("2026-11-17T21:58:00Z", {"2": {"5h": 3.0, "7d": 22.0}}),
        poll("2026-11-21T12:55:00Z", {"3": {"5h": 3.0, "7d": 95.0}}),
        poll("2026-11-21T23:00:00Z", {"2": {"7d": 1.0}}),
    ]


def unused_budget(plan3="pro"):
    return budget([
        account(3, plan=plan3, week_reset=ts("2026-11-28T13:00:00Z"), tpp=250000),
        account(2, plan="pro", week_reset=ts("2026-11-25T05:00:00Z"), tpp=250000),
    ])


def test_unused_quota():
    u = fleet(unused_budget(), decisions=unused_decisions(), period="7d")["unused"]
    assert u["items"] == [
        {"account": 2, "pct": 78, "usd": 3.6, "tokens": 19_500_000, "noReading": True, "at": "2026-11-18T05:00:00Z"},
        {"account": 3, "pct": 5, "usd": 0.23, "tokens": 1_250_000, "noReading": False, "at": "2026-11-21T13:00:00Z"},
    ]
    assert u["usd"] == 3.83
    assert u["tokens"] == 20_750_000


def test_unused_quota_window_and_log_limits():
    dec = unused_decisions()
    # 1d period: only the reset of 11-21T13:00Z is inside.
    one = fleet(unused_budget(), decisions=dec, period="1d")["unused"]["items"]
    assert [i["account"] for i in one] == [3]
    # a log that starts after the account-2 reset leaves that reset out
    late = [d for d in dec if d["ts"] >= "2026-11-21"]
    late.insert(0, poll("2026-11-19T00:00:00Z", {}))
    items = fleet(unused_budget(), decisions=late, period="7d")["unused"]["items"]
    assert [i["account"] for i in items] == [3]
    # nothing at all: no items
    assert fleet(unused_budget(), period="7d")["unused"] == {"usd": 0.0, "tokens": 0, "items": []}


def test_unused_quota_picks_last_poll_before_reset_and_skips_full():
    dec = [
        poll("2026-11-16T16:51:44Z", {}),
        poll("2026-11-21T10:00:00Z", {"3": {"7d": 90.0}}),
        poll("2026-11-21T12:59:00Z", {"3": {"7d": 100.0}}),   # last before reset: used it all
        poll("2026-11-21T13:01:00Z", {"3": {"7d": 0.0}}),     # after the reset: ignored
    ]
    assert fleet(unused_budget(), decisions=dec, period="1d")["unused"]["items"] == []


def test_unused_quota_unknown_plan_and_tokens():
    b = budget([
        account(3, plan="team", week_reset=ts("2026-11-28T13:00:00Z"), tpp=250000),
        account(2, plan="max", week_reset=ts("2026-11-25T05:00:00Z")),   # tokensPerPercent unknown
    ])
    u = fleet(b, decisions=unused_decisions(), period="7d")["unused"]
    by = {i["account"]: i for i in u["items"]}
    assert by[3]["usd"] is None and by[3]["tokens"] == 1_250_000
    assert by[2]["tokens"] is None and by[2]["usd"] == pytest.approx(78 / 100 * 100 * 12 / 52, abs=0.005)
    assert u["usd"] == round(78 / 100 * 100 * 12 / 52, 2)      # the unknown plan is not in the money total
    assert u["tokens"] == 1_250_000


def test_prices_table():
    assert fleet_view.PRICES_USD_MONTH == {"pro": 20.0, "max": 100.0, "ultra": 200.0}
    assert fleet_view.PERIODS["7d"] == (604800, 7200, 259200, 86400)


def test_payload_keys_and_unknown_period():
    out = fleet()
    assert set(out) == {"period", "pace", "status", "caption", "tiles", "series", "models",
                        "projects", "events72h", "switches", "unused"}
    assert out["period"] == "7d"
    with pytest.raises(ValueError):
        fleet(period="2d")


def test_switches_today_counts_since_local_midnight_in_every_period():
    # local midnight (-03) of the 21st is 03:00Z; now is 22:00Z, so 10:00Z and 21:30Z are today,
    # and 10:00Z is before the start of the 1h and 6h periods.
    dec = [poll("2026-11-16T16:51:44Z", {}), switch("2026-11-21T10:00:00Z"), switch("2026-11-21T21:30:00Z"),
           switch("2026-11-21T10:30:00Z", dry=True), switch("2026-11-21T02:00:00Z")]
    for period in ("1h", "6h"):
        s = fleet(decisions=dec, period=period)["switches"]
        assert s["today"] == 2, period
    assert fleet(decisions=dec, period="1h")["switches"]["count"] == 1
    assert fleet(decisions=dec, period="6h")["switches"]["count"] == 1


def test_unused_ignores_malformed_windows():
    dec = unused_decisions() + [
        poll("2026-11-21T12:56:00Z", ["not", "a", "dict"]),
        poll("2026-11-21T12:57:00Z", {"3": "x", "2": None}),
        {"event": "poll", "ts": "2026-11-21T12:58:00Z", "windowsPct": 7},
    ]
    u = fleet(unused_budget(), decisions=dec, period="7d")["unused"]
    assert [i["account"] for i in u["items"]] == [2, 3] and u["items"][1]["pct"] == 5
