"""Tests for the pure parts of the Windows tray app (no pystray/pywebview needed)."""

import json

from claude_swap import tray


def card(num=1, title="#1  work", active=False, disabled=False, rows=(), message=None):
    return {
        "num": num, "title": title, "active": active, "disabled": disabled,
        "rows": [{"label": l, "pct": p, "reset": None, "age": None} for l, p in rows],
        "message": message,
    }


def test_settings_default_notifications_off(tmp_path):
    s = tray.TraySettings.load(tmp_path / "missing.json")
    assert s.notifications is False and s.auto_switch is False
    assert s.startup_initialized is False


def test_settings_roundtrip_and_bad_values(tmp_path):
    path = tmp_path / "tray.json"
    tray.TraySettings(auto_switch=True, notifications=True, startup_initialized=True).save(path)
    assert tray.TraySettings.load(path) == tray.TraySettings(auto_switch=True, notifications=True, startup_initialized=True)
    path.write_text(json.dumps({"auto_switch": "yes", "notifications": True}), encoding="utf-8")
    loaded = tray.TraySettings.load(path)
    assert loaded.auto_switch is False and loaded.notifications is True
    path.write_text("not json", encoding="utf-8")
    assert tray.TraySettings.load(path) == tray.TraySettings()


def test_account_label_shows_usage_and_disabled():
    c = card(rows=(("5h", 42.4), ("7d", 18.0)))
    assert tray.account_label(c) == "#1  work  5h 42% · 7d 18%"
    assert "(disabled)" in tray.account_label(card(disabled=True, message="no usage yet"))


def test_account_label_falls_back_to_message():
    assert tray.account_label(card(message="token expired")).endswith("token expired")


def test_tooltip_uses_active_account():
    cards = [card(1, "#1  a", rows=(("5h", 10.0),)), card(2, "#2  b", active=True, rows=(("5h", 90.0), ("7d", 50.0)))]
    assert tray.tooltip(cards) == "cswap — #2  b  5h 90% · 7d 50%"


def test_tooltip_without_active_or_too_long():
    assert tray.tooltip([card()]) == "cswap"
    long = card(active=True, title="#1  " + "x" * 200, rows=(("5h", 1.0),))
    assert len(tray.tooltip([long])) == tray.TOOLTIP_MAX


def test_startup_command_prefers_windowless_python(tmp_path):
    (tmp_path / "pythonw.exe").write_text("")
    cmd = tray.startup_command(str(tmp_path / "python.exe"))
    assert cmd == f'"{tmp_path / "pythonw.exe"}" -m claude_swap.tray'
    assert tray.startup_command(str(tmp_path / "nope" / "python.exe")).endswith('python.exe" -m claude_swap.tray')


def test_muted_kinds_follow_event_toggles():
    assert tray.muted_kinds(tray.TraySettings()) == set()
    s = tray.TraySettings(notify_quarantine=False, notify_errors=False)
    assert tray.muted_kinds(s) == {"account-quarantined", "account-unquarantined", "error"}


# ---- threshold submenu and account rows ------------------------------------

def test_threshold_label_drops_a_zero_fraction():
    assert tray.threshold_label(90.0) == "90%"
    assert tray.threshold_label(92.5) == "92.5%"


def test_clamp_threshold_rounds_and_keeps_the_range():
    assert tray.clamp_threshold(98.0) == 98
    assert tray.clamp_threshold("95") == 95
    assert tray.clamp_threshold(92.4) == 92
    assert tray.clamp_threshold(10) == tray.THRESHOLD_MIN
    assert tray.clamp_threshold(100) == tray.THRESHOLD_MAX
    assert tray.clamp_threshold(None) is None
    assert tray.clamp_threshold("x") is None
    assert tray.clamp_threshold(float("nan")) is None


def test_switch_modes_cover_every_strategy_once():
    from claude_swap.settings import AUTOSWITCH_STRATEGIES

    names = [name for name, _label in tray.SWITCH_MODES]
    assert sorted(names) == sorted(AUTOSWITCH_STRATEGIES)
    assert names[:2] == ["best", "balanced"]


def test_threshold_page_has_a_slider_and_both_buttons():
    page = tray.THRESHOLD_PAGE
    assert "type=range min=50 max=99 step=1" in page
    assert "id=cancel" in page and "id=ok" in page and "__M" not in page


def test_account_rows_only_without_a_visible_pill():
    assert tray.show_account_rows(pill_available=False, pill_enabled=False) is True
    assert tray.show_account_rows(pill_available=True, pill_enabled=False) is True
    assert tray.show_account_rows(pill_available=True, pill_enabled=True) is False


def test_row_state_is_written_in_the_text():
    assert tray.toggle_label("Auto-switch", True) == "Auto-switch: On"
    assert tray.toggle_label("Auto-switch", False) == "Auto-switch: Off"
    assert tray.choice_label("Left edge", True).startswith("\u25cf")
    assert tray.choice_label("Left edge", False).startswith("\u25cb")
    assert tray.mode_label("balanced") == "Balanced"
    assert tray.mode_label("best") == "Simple"
    assert tray.mode_label("other") == "other"


# ---- pill square and card budget (fleet panel, FP3, FP10, FP12) -------------

def test_pill_payload_has_fleet_and_card_budget():
    from datetime import datetime, timezone

    from claude_swap import pill

    now = datetime(2026, 11, 21, 22, 0, 0, tzinfo=timezone.utc).timestamp()
    cards = [card(1, "#1  a", rows=(("5h", 10.0), ("7d", 20.0))),
             card(4, "#4  b", active=True, rows=(("5h", 33.0), ("7d", 95.0)))]
    for c in cards:
        c["num"] = str(c["num"])  # desktop.card_dict gives the slot number as text
    budget = {
        "fleet": {"paceRatio": 1.06, "weekUsedPct": 58.2, "weekElapsedPct": 66.9, "usableNow": 2, "accounts": 2},
        "accounts": [
            {"id": 1, "plan": "max", "fetchedAt": "2026-11-21T21:58:00Z",
             "sevenDay": {"resetsAt": "2026-11-25T05:00:00Z", "tokensPerPercent": 250000}},
            {"id": 4, "plan": "pro", "fetchedAt": "2026-11-21T21:59:00Z",
             "fiveHour": {"pct": 33.0, "resetsAt": "2026-11-22T00:29:00Z"},
             "sevenDay": {"resetsAt": "2026-11-22T05:59:00Z", "elapsedPct": 66.0, "projectedEndPct": 100.0,
                          "tokensPerPercent": 1200000}},
        ],
    }
    bucket = str(int((now - 600) // 300 * 300))
    history = {"buckets": {bucket: {"m": {"claude-opus-5-5": 2_520_000.0}, "p": {"guilda": 2_520_000.0}}}}
    shown, fleet = tray.pill_inputs(cards, budget, {"4": "work"}, history, now)
    payload = pill.pill_payload(shown, "right", "black", pill.window_layout("right", 2, (0, 0, 1920, 1080)),
                                fleet=fleet)
    assert payload["fleet"]["pace"] == 106 and payload["fleet"]["level"] == "critical"
    assert len(payload["fleet"]["sparkline24h"]) == 24 and payload["fleet"]["sparkline24h"][-1] > 0
    one, four = payload["items"]
    assert one["budget"]["plan"] == "max" and one["budget"]["nowPerMin"] is None
    assert four["budget"] == {"plan": "pro", "fiveResetAt": "2026-11-22T00:29:00Z",
                              "weekResetAt": "2026-11-22T05:59:00Z", "weekElapsedPct": 66.0,
                              "projectedEndPct": 100.0, "tokensPerPercent": 1200000,
                              "nowPerMin": 42000.0, "alias": "work", "readAgeS": 60.0}
    # without a history yet: a flat square, the cards still get their budget
    shown, fleet = tray.pill_inputs(cards, budget, {}, None, now)
    assert fleet["sparkline24h"] == [0.0] * 24 and shown[1]["budget"]["nowPerMin"] == 0.0
