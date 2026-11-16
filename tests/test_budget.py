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
        "at": iso(NOW + 10 * H), "accountId": 2, "expiringUnits": 2.0, "hoursFromNow": 10.0,
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


def test_tokens_are_absent_without_a_measurement():
    assert build(acc(1, week=50.0))["tokens"] is None


def test_tokens_scale_by_capacity_and_say_estimate():
    b = build(acc(1, week=50.0), acc(2, "max", week=50.0),
              tokens_per_percent_pro=1000.0, tokens_measured_at="2026-11-10")
    assert b["tokens"] == {
        "estimate": True, "tokensPerPercentPro": 1000, "measuredAt": "2026-11-10",
        "remainingTokens": 300000, "usableNowTokens": 300000,
    }


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
    assert b["tokens"] is None


def test_current_budget_reads_the_calibration_file(monkeypatch, tmp_path):
    (tmp_path / "budget_calibration.json").write_text(
        json.dumps({"tokensPerPercentPro": 2000, "measuredAt": "2026-11-10"}))
    src = _fake_source(monkeypatch, tmp_path, {"accounts": [acc(1, week=50.0)]})
    assert src.current_budget(NOW)["tokens"]["remainingTokens"] == 100000
    for bad in ("{", json.dumps({"tokensPerPercentPro": -1}), json.dumps({"tokensPerPercentPro": True}), "[]"):
        (tmp_path / "budget_calibration.json").write_text(bad)
        assert src.current_budget(NOW)["tokens"] is None


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
