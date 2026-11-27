"""Tests for which auto-switch events notify, and how they read."""

from __future__ import annotations

from datetime import datetime

from claude_swap.autoswitch import (
    AllExhaustedEvent,
    ConfigWarningEvent,
    ErrorEvent,
    NoSwitchEvent,
    QuarantineEvent,
    SwitchEvent,
    UnquarantineEvent,
)
from claude_swap.desktop_notify import Notifier, notification_for, toast_xml


def _switch(trigger="at-limit", **kw):
    return SwitchEvent(
        trigger=trigger,
        from_ref={"number": 1, "email": "work@example.com"},
        to_ref={"number": 2, "email": "home@example.com"},
        **kw,
    )


def test_switch_reads_naturally():
    key, title, body = notification_for(_switch())
    assert title == "Switched to home@example.com"
    assert body == "work@example.com hit its limit."


def test_proactive_switch_says_running_low():
    assert notification_for(_switch("proactive"))[2] == "work@example.com was running low."


def test_why_names_limit_hit_and_estimate():
    assert notification_for(_switch("limit-hit"))[2] == (
        "work@example.com hit its limit (reported by Claude Code)."
    )
    assert notification_for(_switch("estimate"))[2] == (
        "work@example.com reached the switch line by token estimate."
    )


def test_dry_run_switch_is_silent():
    assert notification_for(_switch(dry_run=True)) is None


def test_quarantine_explains_recovery():
    _, title, body = notification_for(QuarantineEvent(number="3", email="a@x.com", reason="refresh token revoked"))
    assert title == "Account needs re-login"
    assert "a@x.com" in body and "cswap add --slot 3" in body


def test_unquarantine_notifies():
    assert notification_for(UnquarantineEvent(number="3", email="a@x.com"))[1] == "Account back in rotation"


def test_all_exhausted_without_reset_time():
    _, title, body = notification_for(AllExhaustedEvent(earliest_reset_at=None))
    assert title == "All accounts are at their limit"
    assert body == "No reset time is known yet."


def test_all_exhausted_shows_local_reset_time():
    body = notification_for(AllExhaustedEvent(earliest_reset_at="2026-10-12T09:00:00Z"))[2]
    assert body.startswith("Earliest reset at ") and ":" in body


def test_config_warning_notifies():
    _, title, body = notification_for(ConfigWarningEvent(message="no account reports model 'x'"))
    assert title == "Auto-switch setting ignored" and "model 'x'" in body


def test_transient_error_and_routine_events_are_silent():
    assert notification_for(ErrorEvent(message="timeout", transient=True)) is None
    assert notification_for(NoSwitchEvent(reason="below-threshold")) is None


def test_fatal_error_says_auto_switch_stopped():
    assert notification_for(ErrorEvent(message="boom", transient=False))[1] == "Auto-switch stopped"


def _notifier(focused=False, now=[0.0]):
    sent = []
    n = Notifier(send=lambda t, b: sent.append((t, b)), is_focused=lambda: focused, clock=lambda: now[0])
    return n, sent


def test_same_alert_not_repeated_within_an_hour():
    now = [0.0]
    n, _ = _notifier(now=now)
    assert n.handle(_switch()) is True
    now[0] = 600
    assert n.handle(_switch()) is False
    now[0] = 3601
    assert n.handle(_switch()) is True


def test_no_notification_while_window_focused():
    n, _ = _notifier(focused=True)
    assert n.handle(_switch()) is False


def test_send_failure_is_swallowed():
    def boom(title, body):
        raise OSError("no powershell")

    n = Notifier(send=boom, clock=lambda: 0.0)
    n._safe_send("t", "b")  # must not raise


def test_toast_xml_escapes_text():
    xml = toast_xml("A & B", "<b>x</b>")
    assert "A &amp; B" in xml and "&lt;b&gt;x&lt;/b&gt;" in xml


def test_muted_kind_does_not_notify():
    from types import SimpleNamespace

    sent = []
    n = Notifier(send=lambda t, b: sent.append((t, b)))
    n.muted = {"all-exhausted"}
    ev = SimpleNamespace(kind="all-exhausted", earliest_reset_at=None)
    assert n.handle(ev) is False and sent == []


def test_clock_text_12h():
    from claude_swap.desktop_notify import clock_text
    cases = [((0, 5), "12:05 AM"), ((9, 39), "9:39 AM"), ((12, 0), "12:00 PM"), ((21, 39), "9:39 PM")]
    for (h, m), text in cases:
        assert clock_text(datetime(2026, 11, 27, h, m)) == text


def test_local_time_uses_12h_clock():
    from claude_swap.desktop_notify import local_time
    now = datetime.now().astimezone().replace(hour=21, minute=39, second=0, microsecond=0)
    assert local_time(now.isoformat()) == "9:39 PM"
    other = (now.replace(day=1) if now.day != 1 else now.replace(day=2)).replace(hour=0, minute=5)
    assert local_time(other.isoformat()) == other.strftime("%b %d") + " 12:05 AM"


def test_no_24h_clock_left():
    from pathlib import Path
    import claude_swap
    root = Path(claude_swap.__file__).parent
    for name in ("pill.py", "fleet_page.py", "desktop_notify.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "%H:%M" not in text, name
        assert "getHours()).padStart" not in text, name
        assert "pad(d.getHours())" not in text, name
    # getHours() may appear only on the lines that define clock and clockHour
    for name in ("pill.py", "fleet_page.py"):
        lines = (root / name).read_text(encoding="utf-8").splitlines()
        stray = [ln for ln in lines if "getHours()" in ln and not ln.startswith(("const clock=", "const clockHour="))]
        assert stray == [], (name, stray)
    fleet = (root / "fleet_page.py").read_text(encoding="utf-8")
    assert "textContent=`${WD[d.getDay()]} ${clockHour(d)}`" in fleet
