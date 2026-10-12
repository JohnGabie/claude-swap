"""Tests for the pure display helpers behind the desktop window."""

from __future__ import annotations

from claude_swap.desktop import card_lines, event_line, strip_ansi
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.models import AccountSnapshot
from claude_swap.switcher import SENTINEL_NOTES
from claude_swap.usage_store import UsageEntry


def _acc(email="a@example.com", usage=None, **kw) -> AccountSnapshot:
    return AccountSnapshot(
        number="1",
        email=email,
        org_name="",
        org_uuid="",
        is_active=kw.pop("is_active", True),
        kind="oauth",
        switchable=True,
        usage=usage or UsageEntry(),
        **kw,
    )


def test_card_active_with_usage():
    usage = UsageEntry(
        last_good={"five_hour": {"pct": 62.0}, "seven_day": {"pct": 21.0}}, age_s=0
    )
    view = card_lines(_acc(usage=usage), now=0.0)
    assert view.rows[0].label == "5h"
    assert view.rows[0].pct == 62.0
    assert view.rows[1].pct == 21.0
    assert view.active is True
    assert view.message is None


def test_card_sentinel_replaces_bars():
    view = card_lines(_acc(usage=UsageEntry(sentinel=USAGE_API_KEY)), now=0.0)
    assert view.rows == ()
    assert view.message == SENTINEL_NOTES[USAGE_API_KEY]


def test_card_no_usage():
    assert card_lines(_acc(), now=0.0).message == "no usage yet"


def test_card_disabled_flag():
    assert card_lines(_acc(disabled=True), now=0.0).disabled is True


def test_card_title_truncated():
    view = card_lines(_acc(email="x" * 60 + "@example.com"), now=0.0)
    assert len(view.title) == 32
    assert view.title.endswith("…")


def test_strip_ansi():
    assert strip_ansi("\x1b[32mok\x1b[0m") == "ok"


class _Ev:
    def __init__(self, kind):
        self.kind = kind

    def human(self):
        return "something happened"


def test_event_line_switch_no_alert():
    text, alert = event_line(_Ev("switch"))
    assert alert is False
    assert text.endswith("something happened")


def test_event_line_exhausted_alerts():
    text, alert = event_line(_Ev("all-exhausted"))
    assert alert is True
    assert text.endswith("something happened")


def test_refresh_gate_replays_skipped_request():
    from claude_swap.desktop import RefreshGate

    g = RefreshGate()
    assert g.request() is True
    assert g.request() is False  # overlap refused
    assert g.done() is True  # but remembered
    assert g.request() is True
    assert g.done() is False


def test_main_replaces_none_streams(monkeypatch):
    import sys

    from claude_swap import desktop

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(desktop, "ClaudeAccountSwitcher", lambda *a, **k: object())
    monkeypatch.setattr(desktop, "run", lambda sw: 0)
    assert desktop.main() == 0
    assert sys.stdout is not None and sys.stderr is not None


def test_main_startup_error_shows_dialog(monkeypatch):
    from claude_swap import desktop
    from claude_swap.exceptions import ClaudeSwitchError

    def _boom(*a, **k):
        raise ClaudeSwitchError("boom")

    shown = []
    monkeypatch.setattr(desktop, "ClaudeAccountSwitcher", _boom)
    monkeypatch.setattr(desktop, "_show_error", shown.append)
    assert desktop.main() == 1
    assert len(shown) == 1 and "boom" in shown[0]
