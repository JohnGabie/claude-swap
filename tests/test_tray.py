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
