"""Fleet budget: per-account quota, fleet sums, resets and the advice."""

import json
from datetime import datetime, timezone

from claude_swap import budget

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc).timestamp()
H = 3600.0
WEEK = 168 * H


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def acc(num, plan="pro", five=0.0, week=0.0, week_reset_h=84.0, five_reset_h=2.0, status="ok"):
    """An account whose weekly reset is ``week_reset_h`` hours from NOW."""
    out = {"id": num, "plan": plan, "status": status}
    if status in ("ok", "stale"):
        out["fiveHour"] = {"pct": five, "resetsAt": iso(NOW + five_reset_h * H)}
        out["sevenDay"] = {"pct": week, "resetsAt": iso(NOW + week_reset_h * H)}
    else:
        out["error"] = status
    return out


def build(*accounts, **kw):
    return budget.build_budget({"schemaVersion": 1, "accounts": list(accounts)}, NOW, **kw)


def test_one_account_at_half_week_and_half_used():
    b = build(acc(1, week=50.0))
    a = b["accounts"][0]
    assert a["usableNow"] is True and a["blockedBy"] is None
    assert a["sevenDay"]["elapsedPct"] == 50.0
    assert a["sevenDay"]["remainingUnits"] == 0.5
    assert a["sevenDay"]["projectedEndPct"] == 100.0
    assert a["sevenDay"]["expectedLossUnits"] == 0.0
    assert b["fleet"]["paceRatio"] == 1.0
    assert b["advice"] == "ok"


def test_capacity_follows_the_plan():
    b = build(acc(1, "pro", week=50.0), acc(2, "max", week=50.0), acc(3, "ultra", week=50.0))
    assert [a["capacityUnits"] for a in b["accounts"]] == [1.0, 5.0, 10.0]
    assert b["fleet"]["capacityUnits"] == 16.0
    assert b["fleet"]["remainingUnits"] == 8.0


def test_an_idle_fleet_can_speed_up_and_shows_the_loss():
    b = build(acc(1, week=10.0), acc(2, week=10.0))
    assert b["advice"] == "can_speed_up"
    assert b["fleet"]["paceRatio"] == 0.2
    # At this pace each account ends the week at 20%: 0.8 units expire each.
    assert b["accounts"][0]["sevenDay"]["projectedEndPct"] == 20.0
    assert b["fleet"]["expectedLossUnits"] == 1.6


def test_a_fleet_ahead_of_the_week_slows_down():
    b = build(acc(1, week=80.0), acc(2, week=70.0))
    assert b["advice"] == "slow_down"
    assert b["fleet"]["expectedLossUnits"] == 0.0


def test_no_usable_account_is_blocked_with_the_next_time():
    b = build(acc(1, week=100.0, week_reset_h=30.0), acc(2, five=100.0, five_reset_h=3.0, week=40.0))
    assert b["advice"] == "blocked"
    assert [a["blockedBy"] for a in b["accounts"]] == ["sevenDay", "fiveHour"]
    assert b["fleet"]["usableNow"] == 0
    assert b["fleet"]["nextUsableAt"] == iso(NOW + 3 * H)


def test_the_switch_lines_decide_what_is_usable():
    assert build(acc(1, five=92.0, week=50.0))["accounts"][0]["usableNow"] is False
    assert build(acc(1, five=92.0, week=50.0), threshold=98.0)["accounts"][0]["usableNow"] is True
    b = build(acc(1, five=10.0, week=92.0), threshold=98.0, weekly_threshold=90.0)
    assert b["accounts"][0]["blockedBy"] == "sevenDay"


def test_ultra_has_no_five_hour_gate():
    raw = acc(1, "ultra", week=50.0)
    raw["fiveHour"] = {"pct": 100.0, "resetsAt": iso(NOW + H)}
    a = build(raw)["accounts"][0]
    assert a["usableNow"] is True and "fiveHour" not in a


def test_a_passed_weekly_reset_is_a_new_week():
    a = build(acc(1, week=100.0, week_reset_h=-24.0))["accounts"][0]
    assert a["sevenDay"]["pct"] == 0.0
    assert a["usableNow"] is True
    assert a["sevenDay"]["resetsAt"] == iso(NOW - 24 * H + WEEK)


def test_resets_are_sorted_with_what_expires():
    b = build(acc(1, week=20.0, week_reset_h=100.0), acc(2, "max", week=60.0, week_reset_h=10.0))
    assert [r["accountId"] for r in b["resets"]] == [2, 1]
    assert b["resets"][0] == {
        "at": iso(NOW + 10 * H), "accountId": 2, "expiringUnits": 2.0, "refillUnits": 5.0,
        "hoursFromNow": 10.0,
    }


def test_an_unread_account_is_listed_and_not_counted():
    b = build(acc(1, week=50.0), acc(2, status="token_expired"))
    assert b["accounts"][1] == {
        "id": 2, "plan": "pro", "capacityUnits": 1.0, "status": "token_expired", "fetchedAt": None,
        "measured": False, "usableNow": False,
    }
    assert b["fleet"]["accounts"] == 2 and b["fleet"]["measuredAccounts"] == 1
    assert b["fleet"]["capacityUnits"] == 1.0


def test_a_failed_source_gives_unknown():
    for pace in ({"schemaVersion": 1, "error": "unavailable"}, {}, None, {"accounts": "x"}):
        b = budget.build_budget(pace, NOW)
        assert b["advice"] == "unknown" and b["accounts"] == []
        assert b["fleet"]["paceRatio"] is None


def test_a_new_week_gives_no_projection():
    b = build(acc(1, week=1.0, week_reset_h=167.0))
    assert b["accounts"][0]["sevenDay"]["projectedEndPct"] is None
    assert b["fleet"]["expectedLossUnits"] is None


def test_no_internal_field_and_no_identity_leaves():
    raw = acc(1, week=50.0)
    raw["email"] = "a.person@example.com"  # not part of pace; must not pass through
    text = json.dumps(build(raw))
    assert "@" not in text and '"_' not in text


def test_advice_line_is_one_line():
    line = budget.advice_line(build(acc(1, week=10.0)))
    assert line.startswith("cswap budget: can_speed_up - ")
    assert "\n" not in line and "get_budget" in line


# ---- prompt hook ------------------------------------------------------------

def test_first_line_of_a_session_needs_news():
    assert budget.hook_should_show(None, "ok", NOW) is False
    assert budget.hook_should_show(None, "unknown", NOW) is False
    assert budget.hook_should_show(None, "slow_down", NOW) is True
    assert budget.hook_should_show({}, "blocked", NOW) is True


def test_later_lines_need_a_change_and_the_dwell():
    shown = {"advice": "slow_down", "shownAt": NOW}
    assert budget.hook_should_show(shown, "slow_down", NOW + 9999) is False
    assert budget.hook_should_show(shown, "ok", NOW + 60) is False
    assert budget.hook_should_show(shown, "ok", NOW + budget.HOOK_DWELL_S) is True


def test_hook_record_keeps_the_newest_sessions():
    state = {}
    for i in range(budget.HOOK_SESSIONS_KEPT + 5):
        state = budget.hook_record(state, f"s{i}", "ok", NOW + i)
    assert len(state["sessions"]) == budget.HOOK_SESSIONS_KEPT
    assert "s0" not in state["sessions"] and f"s{budget.HOOK_SESSIONS_KEPT + 4}" in state["sessions"]


# ---- live source, CLI and MCP server ---------------------------------------

def _fake_source(monkeypatch, tmp_path, pace):
    from claude_swap import budget_source, cli, paths

    monkeypatch.setattr(paths, "get_backup_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_pace_payload", lambda: (pace, 0))
    return budget_source


def test_current_budget_uses_the_settings_lines(monkeypatch, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"autoswitch": {"threshold": 98.0}}))
    src = _fake_source(monkeypatch, tmp_path, {"accounts": [acc(1, five=95.0, week=50.0)]})
    b = src.current_budget(NOW)
    assert b["fleet"]["thresholdPct"] == 98.0
    assert b["accounts"][0]["usableNow"] is True
    # With no data of its own the account gets the prior of its plan.
    assert b["tokens"]["confidence"] == "prior"
    assert b["accounts"][0]["sevenDay"]["tokensPerPercent"] == 300_000


def test_current_budget_never_raises(monkeypatch, tmp_path):
    from claude_swap import budget_source, cli, paths

    monkeypatch.setattr(paths, "get_backup_root", lambda: tmp_path)

    def boom():
        raise RuntimeError("secret a.person@example.com")

    monkeypatch.setattr(cli, "_pace_payload", boom)
    b = budget_source.current_budget(NOW)
    assert b["advice"] == "unknown" and "@" not in json.dumps(b)


def test_hook_line_shows_once_per_change(monkeypatch, tmp_path):
    idle = {"accounts": [acc(1, week=10.0)]}
    src = _fake_source(monkeypatch, tmp_path, idle)
    first = src.hook_line("s1", NOW)
    assert first.startswith("cswap budget: can_speed_up")
    assert src.hook_line("s1", NOW + 60) is None  # same advice
    assert src.hook_line("s2", NOW + 60).startswith("cswap budget: can_speed_up")  # another session
    state = json.loads((tmp_path / "cache" / "budget_hook.json").read_text())
    assert sorted(state["sessions"]) == ["s1", "s2"]


def test_hook_line_is_silent_when_all_is_ok(monkeypatch, tmp_path):
    src = _fake_source(monkeypatch, tmp_path, {"accounts": [acc(1, week=50.0)]})
    assert src.hook_line("s1", NOW) is None
    assert not (tmp_path / "cache" / "budget_hook.json").exists()


def test_cli_budget_prints_json(monkeypatch, tmp_path, capsys):
    import pytest

    from claude_swap import cli

    _fake_source(monkeypatch, tmp_path, {"accounts": [acc(1, week=50.0)]})
    with pytest.raises(SystemExit) as e:
        cli._budget_command([])
    assert e.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schemaVersion"] == 1 and out["fleet"]["accounts"] == 1


def test_cli_budget_hook_reads_the_session_and_never_fails(monkeypatch, tmp_path, capsys):
    import io

    import pytest

    from claude_swap import cli

    _fake_source(monkeypatch, tmp_path, {"accounts": [acc(1, week=100.0, week_reset_h=30.0)]})
    for stdin, expect_line in (('{"session_id": "abc"}', True), ('{"session_id": "abc"}', False), ("not json", True)):
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        with pytest.raises(SystemExit) as e:
            cli._budget_command(["--hook"])
        assert e.value.code == 0
        out = capsys.readouterr().out
        assert out.startswith("cswap budget: blocked") if expect_line else out == ""


def test_mcp_server_class_is_found_when_the_sdk_is_installed():
    import pytest

    pytest.importorskip("mcp")
    from claude_swap import mcp_server

    assert mcp_server._server_class() is not None


def test_mcp_run_without_the_sdk_says_how_to_install(monkeypatch, capsys):
    from claude_swap import mcp_server

    monkeypatch.setattr(mcp_server, "_server_class", lambda: None)
    assert mcp_server.run() == 1
    assert "claude-swap[mcp]" in capsys.readouterr().err


# ---- horizons: the fuel for a plan ------------------------------------------

def test_horizons_add_the_refills_inside_each_window():
    # Pro, 40% left, resets in 10 h. Max, 50% left (2.5 units), resets in 100 h.
    b = build(acc(1, week=60.0, week_reset_h=10.0), acc(2, "max", week=50.0, week_reset_h=100.0))
    day, three, week = b["horizons"]
    assert [h["hours"] for h in b["horizons"]] == [24, 72, 168]
    assert day == {"hours": 24, "until": iso(NOW + 24 * H), "inTankUnits": 2.9,
                   "expiringUnits": 0.4, "refillUnits": 1.0, "availableUnits": 3.9}
    assert three["availableUnits"] == 3.9
    assert week["expiringUnits"] == 2.9 and week["refillUnits"] == 6.0 and week["availableUnits"] == 8.9


def test_an_exhausted_account_gives_fuel_only_after_its_reset():
    b = build(acc(1, week=100.0, week_reset_h=30.0))
    day, three, _week = b["horizons"]
    assert day["availableUnits"] == 0.0
    assert three["refillUnits"] == 1.0 and three["availableUnits"] == 1.0


# ---- tokens ------------------------------------------------------------------

RATE_PRO = {"weekPerPct": 1000.0, "fivePerPct": 100.0, "confidence": "low"}
RATE_MAX = {"weekPerPct": 5000.0, "fivePerPct": 500.0, "confidence": "high"}


def test_tokens_are_absent_without_rates():
    b = build(acc(1, week=50.0))
    assert b["tokens"] is None
    assert "remainingTokens" not in b["accounts"][0]["sevenDay"]
    assert "availableTokens" not in b["horizons"][0]


def test_each_account_uses_its_own_rate():
    b = build(acc(1, five=10.0, week=50.0), acc(2, "max", five=20.0, week=50.0),
              token_rates={"1": RATE_PRO, "2": RATE_MAX}, token_meta={"unit": "u"})
    one, two = b["accounts"]
    assert one["sevenDay"]["remainingTokens"] == 50_000 and one["sevenDay"]["tokensPerPercent"] == 1000
    assert two["sevenDay"]["remainingTokens"] == 250_000
    assert one["fiveHour"]["headroomTokens"] == 8_000  # (90 - 10) points * 100
    assert one["tokenConfidence"] == "low"
    assert b["tokens"] == {
        "estimate": True, "unit": "u", "confidence": "low",
        "remainingTokens": 300_000, "usableNowTokens": 300_000,
        "fiveHourHeadroomTokens": 8_000 + 35_000,
    }


def test_tokens_need_a_rate_for_every_measured_account():
    b = build(acc(1, week=50.0), acc(2, week=50.0), token_rates={"1": RATE_PRO})
    assert b["tokens"] is None and "availableTokens" not in b["horizons"][0]


def test_reachable_tokens_follow_the_five_hour_windows():
    # 80% of the week left = 80_000 tokens. One open window with 40 points of
    # headroom (4_000 tokens) that closes in 2 h, then 22 h = 5 more windows of
    # 90 points (9_000 tokens each): 49_000 tokens fit in 24 h.
    b = build(acc(1, five=50.0, five_reset_h=2.0, week=20.0, week_reset_h=200.0),
              token_rates={"1": RATE_PRO})
    day, three, week = b["horizons"]
    assert day["availableTokens"] == 80_000
    assert day["reachableTokens"] == 49_000 and day["reachablePctOfAvailable"] == 61.2
    assert three["reachableTokens"] == 80_000  # 15 windows hold more than the week has


def test_reachable_tokens_count_a_refill_inside_the_horizon():
    b = build(acc(1, five=0.0, five_reset_h=-1.0, week=100.0, week_reset_h=10.0),
              token_rates={"1": RATE_PRO})
    week = b["horizons"][2]
    assert week["availableTokens"] == 100_000 and week["reachableTokens"] == 100_000
    assert b["horizons"][0]["reachableTokens"] == 45_000  # 5 windows of 9_000 in 24 h


def test_ultra_reach_has_no_five_hour_cap():
    b = build(acc(1, "ultra", week=50.0, week_reset_h=200.0),
              token_rates={"1": {"weekPerPct": 10_000.0, "fivePerPct": 1000.0, "confidence": "prior"}})
    assert b["horizons"][0]["reachableTokens"] == 500_000


# ---- token model -----------------------------------------------------------

def test_weighted_tokens_use_type_and_model_weights():
    from claude_swap import token_model as tm

    usage = {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 50,
             "cache_read_input_tokens": 10_000, "service_tier": "x", "other": True}
    assert tm.weighted_tokens("claude-opus-5-5", usage) == 300.0  # 100 + 50 + 50 + 100
    assert tm.weighted_tokens("claude-sonnet-5-5", usage) == 180.0
    assert tm.weighted_tokens("claude-fable-5-2", usage) == 900.0
    assert tm.weighted_tokens(None, {}) == 0.0
    assert tm.model_weight("claude-fable-5-1") == 3.8 and tm.model_weight("unknown-model") == 1.0


def test_the_rate_moves_from_the_prior_to_the_account_data():
    from claude_swap import token_model as tm

    assert tm.prior_rate("pro", "week") == 300_000 and tm.prior_rate("max", "week") == 1_500_000
    assert tm.blended_rate("pro", "week", 0, 0) == 300_000
    # 10 prior points at 300k and 10 own points at 100k give 200k.
    assert tm.blended_rate("pro", "week", 10, 1_000_000) == 200_000
    assert abs(tm.blended_rate("pro", "week", 1000, 100_000_000) - 100_000) < 2_100
    assert [tm.confidence(v) for v in (0, 10, 30, 100)] == ["prior", "low", "medium", "high"]


# ---- calibration from the user's logs ---------------------------------------

def _poll(ts, active, five, week):
    return json.dumps({"event": "poll", "ts": iso(ts), "active": {"number": active},
                       "windowsPct": {str(active): {"5h": five, "7d": week}}})


def _msg(ts, mid, model="claude-opus-5-5", inp=0, out=0, create=0, read=0, text="secret text"):
    stamp = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({"timestamp": stamp, "requestId": "r" + mid, "type": "assistant",
                       "message": {"id": mid, "model": model, "content": text, "usage": {
                           "input_tokens": inp, "output_tokens": out,
                           "cache_creation_input_tokens": create, "cache_read_input_tokens": read}}})


def test_accumulate_sums_points_and_tokens_per_account():
    from claude_swap import budget_calibrate as cal

    polls = [(0.0, "1", 10.0, 20.0), (600.0, "1", 30.0, 24.0),
             (1200.0, "2", 5.0, 50.0), (1800.0, "2", 9.0, 51.0)]
    events = [(300.0, 40_000.0), (1500.0, 50_000.0), (5000.0, 999.0)]
    store = cal.accumulate(cal.empty_store(), polls, events)
    assert store["accounts"]["1"] == {"week": {"points": 4.0, "tokens": 40_000.0},
                                      "five": {"points": 20.0, "tokens": 40_000.0}}
    assert store["accounts"]["2"]["week"] == {"points": 1.0, "tokens": 50_000.0}
    assert store["lastPoll"] == [1800.0, "2", 9.0, 51.0]


def test_accumulate_continues_from_the_last_poll():
    from claude_swap import budget_calibrate as cal

    first = [(0.0, "1", 10.0, 20.0), (600.0, "1", 20.0, 22.0)]
    more = first + [(1200.0, "1", 35.0, 25.0)]
    events = [(300.0, 1000.0), (900.0, 3000.0)]
    store = cal.accumulate(cal.empty_store(), first, events)
    store = cal.accumulate(store, more, events)  # the first interval is not counted twice
    assert store["accounts"]["1"]["week"] == {"points": 5.0, "tokens": 4000.0}
    assert cal.accumulate(store, more, events) == store  # nothing new: no change


def test_accumulate_skips_gaps_resets_and_external_use():
    from claude_swap import budget_calibrate as cal

    polls = [(0.0, "1", 10.0, 20.0), (5000.0, "1", 30.0, 40.0),   # gap: the engine was off
             (5600.0, "1", 0.0, 41.0),                            # 5h reset: only the week counts
             (6200.0, "2", 5.0, 50.0), (6800.0, "2", 9.0, 53.0)]  # no token: external use
    store = cal.accumulate(cal.empty_store(), polls, [(5300.0, 700.0)])
    assert store["accounts"]["1"] == {"week": {"points": 1.0, "tokens": 700.0},
                                      "five": {"points": 0.0, "tokens": 0.0}}
    assert store["accounts"]["2"] == {"externalPoints": 3.0}


def test_rates_blend_own_data_with_the_prior():
    from claude_swap import budget_calibrate as cal

    store = cal.empty_store()
    store["accounts"]["1"] = {"week": {"points": 10.0, "tokens": 1_000_000.0},
                              "five": {"points": 30.0, "tokens": 600_000.0}}
    out = cal.rates(store, {"1": "pro", "2": "max"})
    assert out["1"]["weekPerPct"] == 200_000 and out["1"]["confidence"] == "low"
    assert out["1"]["fivePerPct"] == 25_000  # (30 * 30k + 600k) / 60
    assert out["2"] == {"weekPerPct": 1_500_000.0, "fivePerPct": 150_000.0, "confidence": "prior",
                        "ownWeekPoints": 0.0, "ownFivePoints": 0.0}
    assert cal.rates({"schemaVersion": 1, "tokensPerPercentPro": 5}, {"1": "pro"})["1"]["confidence"] == "prior"


def test_calibrate_reads_the_logs_once_per_message_and_keeps_no_text(tmp_path):
    from claude_swap import budget_calibrate as cal

    root, projects = tmp_path / "data", tmp_path / "projects" / "p1"
    root.mkdir(), projects.mkdir(parents=True)
    (root / "decisions.jsonl").write_text("\n".join([
        _poll(NOW, 1, 10.0, 20.0), json.dumps({"event": "no-switch"}), _poll(NOW + 600, 1, 30.0, 25.0)]) + "\n")
    (projects / "s.jsonl").write_text("\n".join([
        _msg(NOW + 60, "m1", inp=100, out=900, create=4000, read=50_000),   # 9_100 weighted
        _msg(NOW + 61, "m1", inp=100, out=900, create=4000, read=50_000),   # the same message again
        _msg(NOW + 120, "m2", model="claude-sonnet-5-5", out=1000),         # 3_000 weighted
        _msg(NOW - 60, "m0", out=7777),                                      # before the first poll
        "not json", json.dumps({"type": "user", "message": {"content": "hello"}}),
    ]) + "\n")
    store = cal.calibrate(root, projects_dir=tmp_path / "projects", now=NOW + 700)
    assert store["accounts"]["1"]["week"] == {"points": 5.0, "tokens": 12_100.0}
    stored = (root / "budget_calibration.json").read_text()
    assert json.loads(stored)["accounts"] == store["accounts"]
    assert "secret" not in stored and "hello" not in stored and "@" not in stored
    again = cal.calibrate(root, projects_dir=tmp_path / "projects", now=NOW + 800)
    assert again["accounts"] == store["accounts"]


def test_calibrate_with_no_data_gives_an_empty_store(tmp_path):
    from claude_swap import budget_calibrate as cal

    store = cal.calibrate(tmp_path, projects_dir=tmp_path / "none")
    assert store["accounts"] == {} and store["lastPoll"] is None


def test_current_budget_uses_the_calibration_store(monkeypatch, tmp_path):
    (tmp_path / "decisions.jsonl").write_text(
        _poll(NOW - 600, 1, 10.0, 20.0) + "\n" + _poll(NOW, 1, 30.0, 30.0) + "\n")
    projects = tmp_path / "projects" / "p"
    projects.mkdir(parents=True)
    (projects / "s.jsonl").write_text(_msg(NOW - 300, "m1", inp=1_000_000) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    src = _fake_source(monkeypatch, tmp_path, {"accounts": [acc(1, week=30.0)]})
    b = src.current_budget(NOW)
    # 10 prior points at 300k and 10 own points at 100k give 200k per point.
    assert b["accounts"][0]["sevenDay"]["tokensPerPercent"] == 200_000
    assert b["accounts"][0]["tokenConfidence"] == "low"
    assert b["tokens"]["remainingTokens"] == 14_000_000
    assert "weighted" in b["tokens"]["unit"]
