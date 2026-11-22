"""Tests for the pure parts of the Windows tray app (no pystray/pywebview needed)."""

import json
import logging
import sys
import threading
import types
from pathlib import Path

import pytest

from claude_swap import budget_source, desktop, fleet_page, lines_page, pill, tray
from claude_swap.settings import AutoSwitchSettings, load_settings, set_setting

from tests.test_account_policy_store import PolicyStoreBase


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
    # Buckets on the edges of the square's hourly steps, with distinct values, so that moving the square's
    # "now" by one bucket (300 s) or more either way changes the exact list:
    # - now-300 is the newest bucket: a "now" 300 s earlier or more leaves it out;
    # - now-3600 starts the last hour: a "now" 300 s later or more moves it to the hour before;
    # - now-600 makes the last hour hold 2 520 000 (the active card's 42 000/min);
    # - now-3*3600-600 sits in hour 20 alone.
    weights = {300: 1_800_000.0, 600: 720_000.0, 3600: 600_000.0, 3 * 3600 + 600: 300_000.0}
    assert now % 300 == 0
    history = {"buckets": {str(int(now - age)): {"m": {"claude-opus-5-5": w}, "p": {"guilda": w}}
                           for age, w in weights.items()}}
    shown, fleet = tray.pill_inputs(cards, budget, {"4": "work"}, history, now)
    payload = pill.pill_payload(shown, "right", "black", pill.window_layout("right", 2, (0, 0, 1920, 1080)),
                                fleet=fleet)
    assert payload["fleet"]["pace"] == 106 and payload["fleet"]["level"] == "critical"
    # weighted tokens per minute of each hour: hour 20 holds 300 000, hour 23 holds 1 800 000 + 720 000 + 600 000
    assert payload["fleet"]["sparkline24h"] == [0.0] * 20 + [5000.0, 0.0, 0.0, 52000.0]
    one, four = payload["items"]
    assert one["budget"]["plan"] == "max" and one["budget"]["nowPerMin"] is None
    assert four["budget"] == {"plan": "pro", "fiveResetAt": "2026-11-22T00:29:00Z",
                              "weekResetAt": "2026-11-22T05:59:00Z", "weekElapsedPct": 66.0,
                              "projectedEndPct": 100.0, "tokensPerPercent": 1200000,
                              "nowPerMin": 42000.0, "alias": "work", "readAgeS": 60.0}
    # without a history yet: a flat square, the cards still get their budget
    shown, fleet = tray.pill_inputs(cards, budget, {}, None, now)
    assert fleet["sparkline24h"] == [0.0] * 24 and shown[1]["budget"]["nowPerMin"] == 0.0


SECRET_PATH = "/" + "/".join(("home", "u", "secret"))  # a fake path, built so no path literal sits in the file


def test_refresh_pill_survives_a_failure_and_logs_the_type_only(caplog, monkeypatch):
    import logging

    class FakePill:
        def __init__(self):
            self.calls, self.general = [], []

        def update(self, cards, fleet=None, general_lines=None):
            self.calls.append((cards, fleet))
            self.general.append(general_lines)

    cards = [card(1, "#1  me@example.com", rows=(("5h", 10.0),))]
    cards[0]["num"] = "1"

    def bad_budget(_now):
        raise ValueError(SECRET_PATH + " me@example.com")

    pill, latest = FakePill(), {}
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        tray.refresh_pill(pill, cards, bad_budget, {}, None, 1_000_000.0, latest)
    assert pill.calls == [(cards, None)]  # the pill keeps today's rows; the loop goes on

    def bad_inputs(*_a):
        raise RecursionError(SECRET_PATH)

    monkeypatch.setattr(tray, "pill_inputs", bad_inputs)
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        tray.refresh_pill(pill, cards, lambda _now: {"accounts": []}, {}, None, 1_000_000.0, latest)
    assert pill.calls[-1] == (cards, None) and latest["budget"] == {"accounts": []}
    assert "ValueError" in caplog.text and "RecursionError" in caplog.text
    assert "secret" not in caplog.text and "@" not in caplog.text

    monkeypatch.undo()
    tray.refresh_pill(pill, cards, lambda _now: {"accounts": [{"id": 1, "plan": "pro"}]}, {}, None, 1_000_000.0, latest)
    shown, square = pill.calls[-1]
    assert shown[0]["budget"]["plan"] == "pro" and square["sparkline24h"] == [0.0] * 24
    # the general lines reach the pill, also when the budget fails
    general = {"five": 90.0, "week": 97.0}
    tray.refresh_pill(pill, cards, bad_budget, {}, None, 1_000_000.0, latest, general_lines=general)
    assert pill.general == [None] * 3 + [general] and pill.calls[-1] == (cards, None)


# ---- switch lines: on_lines, card lines, card section state (SL3, SL4, SL6, S8, S9) ----------

AREA = (0, 0, 1920, 1080)
OK = {"ok": True, "error": None}


def refused() -> dict:
    return {"ok": False, "error": tray.LINES_REFUSED}


def store(temp_home, count=6):
    """A synthetic store with ``count`` accounts in a temp home (never the real one)."""
    return PolicyStoreBase()._fleet(temp_home, count)


def pill_cards(nums):
    out = []
    for n in nums:
        c = card(n, f"#{n}  account-{n}@example.test", rows=(("5h", 10.0 * n), ("7d", 5.0 * n)))
        c["num"] = str(n)  # desktop.card_dict gives the slot number as text
        c["tag"] = f"account-{n}@example.test's Organization"
        out.append(c)
    return out


class Lines:
    """The tray's lines wiring with fakes for the window and the refresh."""

    def __init__(self, switcher, tmp_path, nums=range(1, 7), settings=None):
        self.settings = settings or tray.TraySettings()
        self.path = tmp_path / "tray_settings.json"
        self.changes, self.view = [], FakeView()
        self.window = lines_page.LinesWindow(lambda fn: fn(self.view), lambda: ANCHOR)
        self.ctl = pill.PillController(None, None, self.settings, lambda: None, lambda *a: None,
                                       on_lines=None)
        self.ctl.cards = pill_cards(nums)
        self.actions = tray.LinesActions(switcher.write_account_line, self.settings, self.path,
                                         lambda: [c["num"] for c in self.ctl.cards], self.window.toggle,
                                         lambda: self.changes.append(1))
        self.ctl._on_lines = self.actions
        self.bridge = pill.PillBridge(self.ctl)


class FakeView:
    def __init__(self):
        self.log = []
        view = self

        class W:
            def __getattr__(self, name):
                return lambda *a: view.log.append((name, *a))

        class V:
            def run_javascript(self, script, *_a):
                view.log.append(("js", script))

        self.window, self.webview = W(), V()


ANCHOR = {"edge": "right", "square": [1880, 700, 30, 30], "area": list(AREA)}


def test_bridge_refuses_bad_line_input(temp_home, tmp_path):
    # S8: each bad input writes nothing and returns {"ok": false}
    s = store(temp_home)
    lines = Lines(s, tmp_path)
    before = Path(s.sequence_file).read_bytes()
    bad = [("9", "five", 80), ("2", "day", 80), ("2", "five", True), ("2", "five", 101), ("2; rm", "five", 80),
           (2, "five", 80), ("02", "five", 80), ("٢", "five", 80), (None, "five", 80),
           ("2", None, 80), ("2", "threshold", 80), ("2", "five", "80"), ("2", "week", False),
           ("2", "five", 49.9), ("2", "week", 100), ("2", "five", float("nan")), ("2", "five", float("inf")),
           ("2", "five", [80])]
    for num, kind, value in bad:
        assert lines.bridge.set_account_line(num, kind, value) == refused(), (num, kind, value)
    assert Path(s.sequence_file).read_bytes() == before
    assert lines.changes == []
    # the edges of the range are accepted
    assert lines.bridge.set_account_line("2", "five", 50) == OK
    assert lines.bridge.set_account_line("2", "week", 99.9) == OK


def test_bridge_refuses_a_pill_num_that_is_not_an_ascii_digit_string(temp_home, tmp_path):
    # the num check does not trust the pill's own list: a card num that is not an ASCII digit string is refused
    s = store(temp_home)
    lines = Lines(s, tmp_path)
    before = Path(s.sequence_file).read_bytes()
    for num in ("x", "\u0662", " 2", ""):
        lines.ctl.cards[0]["num"] = num
        assert lines.bridge.set_account_line(num, "five", 80) == refused(), num
    assert lines.changes == [] and Path(s.sequence_file).read_bytes() == before


def test_bridge_writes_account_line(temp_home, tmp_path):
    s = store(temp_home)
    lines = Lines(s, tmp_path)
    record = lambda: json.loads(Path(s.sequence_file).read_text())["accounts"]["5"]  # noqa: E731
    assert lines.bridge.set_account_line("5", "five", 80) == OK
    assert record()["threshold"] == 80.0 and "weeklyThreshold" not in record()
    assert lines.bridge.set_account_line("5", "week", 92.5) == OK
    assert record()["weeklyThreshold"] == 92.5 and record()["threshold"] == 80.0
    assert lines.bridge.set_account_line("5", "week", None) == OK
    assert "weeklyThreshold" not in record() and record()["threshold"] == 80.0
    assert lines.bridge.set_account_line("5", "five", None) == OK
    assert "threshold" not in record()
    assert lines.changes == [1] * 4  # each write asks the tray for a refresh
    # the writer's own refusal comes back as it is, with no refresh
    calls = []
    failing = tray.LinesActions(lambda num, key, value: calls.append((num, key, value)) or {"ok": False, "error": "busy"},
                                tray.TraySettings(), tmp_path / "t.json", lambda: ["5"], lambda: None,
                                lambda: calls.append("refresh"))
    assert failing("set_account_line", {"num": "5", "kind": "week", "value": 90}) == {"ok": False, "error": "busy"}
    assert calls == [("5", "weeklyThreshold", 90)]


def test_on_lines_actions(temp_home, tmp_path):
    lines = Lines(store(temp_home, 2), tmp_path, nums=(1, 2))
    assert lines.bridge.lines_window() == OK
    x, y = fleet_page.panel_position(ANCHOR, size=(320, 520))
    assert lines.view.log == [("show_all",), ("deiconify",), ("present",), ("move", x, y),
                              ("js", "window.linesOpen&&window.linesOpen()")]
    lines.view.log.clear()
    assert lines.bridge.lines_window() == OK  # a second gear click closes it
    assert lines.view.log == [("js", "window.linesClosed&&window.linesClosed()"), ("hide",)]
    assert lines.actions("unknown", {}) == refused()
    assert lines.actions("set_account_line", None) == refused()
    assert lines.actions("set_account_line", {"num": "1", "kind": "five"}) == refused()  # no value key


def test_card_open_saved(tmp_path):
    settings = tray.TraySettings()
    assert settings.pill_card_limits_open is False
    path = tmp_path / "tray_settings.json"
    changes = []
    actions = tray.LinesActions(lambda *a: {"ok": True, "error": None}, settings, path, lambda: [], lambda: None,
                                lambda: changes.append(1))
    assert actions("set_card_open", {"open": True}) == OK
    assert settings.pill_card_limits_open is True
    assert tray.TraySettings.load(path).pill_card_limits_open is True
    assert actions("set_card_open", {"open": False}) == OK
    assert tray.TraySettings.load(path).pill_card_limits_open is False
    saved = path.read_bytes()
    for bad in ("yes", 1, None, [True]):
        assert actions("set_card_open", {"open": bad}) == refused()
    assert actions("set_card_open", {}) == refused()
    assert path.read_bytes() == saved and settings.pill_card_limits_open is False
    assert changes == []
    # a bad stored value loads as the default
    path.write_text(json.dumps({"pill_card_limits_open": "yes"}), encoding="utf-8")
    assert tray.TraySettings.load(path).pill_card_limits_open is False


def test_on_lines_catches_errors(tmp_path, caplog):
    def boom(*_a):
        raise ValueError("a@example.com sk-ant-token")

    actions = tray.LinesActions(boom, tray.TraySettings(), tmp_path / "t.json", lambda: ["2"], boom, boom)
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        results = [actions("set_account_line", {"num": "2", "kind": "five", "value": 80}),
                   actions("toggle_window", {})]
        broken_nums = tray.LinesActions(boom, tray.TraySettings(), tmp_path / "t.json", boom, boom, boom)
        results.append(broken_nums("set_account_line", {"num": "2", "kind": "five", "value": 80}))
        unsaved = tray.TraySettings()
        unsaved.save = boom
        results.append(tray.LinesActions(boom, unsaved, tmp_path / "t.json", boom, boom, boom)(
            "set_card_open", {"open": True}))
    assert results == [{"ok": False, "error": tray.LINES_FAILED}] * 4
    assert caplog.text.count("ValueError") == 4
    assert "@" not in caplog.text and "sk-ant" not in caplog.text


def test_tray_card_lines_from_policies(temp_home, caplog):
    s = store(temp_home, 4)
    assert s.write_account_line("2", "threshold", 85)["ok"]
    assert s.write_account_line("2", "weeklyThreshold", 92)["ok"]
    assert s.write_account_line("3", "weeklyThreshold", 93.5)["ok"]
    assert s.write_account_line("4", "threshold", 80)["ok"]
    cards = pill_cards(range(1, 5))
    shown, general = tray.lines_inputs(cards, s.account_policies,
                                       lambda: AutoSwitchSettings(threshold=90.0, weekly_threshold=97.0))
    assert [c["lines"] for c in shown] == [
        {"five": 90.0, "week": 97.0, "own5": False, "ownWeek": False},
        {"five": 85.0, "week": 92.0, "own5": True, "ownWeek": True},
        {"five": 90.0, "week": 93.5, "own5": False, "ownWeek": True},
        {"five": 80.0, "week": 97.0, "own5": True, "ownWeek": False},
    ]
    assert general == {"five": 90.0, "week": 97.0}
    assert all("lines" not in c for c in cards)
    # no general weekly line: each effective weekly line is the account's 5-hour line
    shown, general = tray.lines_inputs(cards, s.account_policies, lambda: AutoSwitchSettings(threshold=88.0))
    assert [(c["lines"]["five"], c["lines"]["week"]) for c in shown] == [(88.0, 88.0), (85.0, 92.0), (88.0, 93.5),
                                                                         (80.0, 80.0)]
    assert general == {"five": 88.0, "week": 88.0}

    def boom():
        raise RuntimeError(SECRET_PATH + " a@example.com")

    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        assert tray.lines_inputs(cards, boom, lambda: AutoSwitchSettings()) == (cards, None)
        assert tray.lines_inputs(cards, s.account_policies, boom) == (cards, None)
    assert caplog.text.count("RuntimeError") == 2 and "secret" not in caplog.text and "@" not in caplog.text


def test_pill_payload_has_lines(temp_home, monkeypatch):
    s = store(temp_home, 2)
    assert s.write_account_line("2", "weeklyThreshold", 92)["ok"]
    monkeypatch.setattr(pill, "work_area", lambda: AREA)
    for open_ in (True, False):
        settings = tray.TraySettings(pill_card_limits_open=open_)
        ctl = pill.PillController(None, None, settings, lambda: None, lambda *a: None)
        sent = []
        ctl._run_js = sent.append
        ctl._loaded.set()
        ctl._visible = True
        ctl.cards = pill_cards((1, 2))  # the same count and layout: update() pushes, it does not relayout
        ctl._placed = ctl._layout()
        shown, general = tray.lines_inputs(pill_cards((1, 2)), s.account_policies,
                                           lambda: AutoSwitchSettings(threshold=90.0))
        tray.refresh_pill(ctl, shown, lambda _now: {"accounts": []}, {}, None, 1_000_000.0, {},
                          general_lines=general)
        payload = json.loads(sent[-1][len("render("):-1])
        assert payload["cardLimitsOpen"] is open_
        assert payload["generalLines"] == {"five": 90.0, "week": 90.0}
        assert [i["lines"] for i in payload["items"]] == [
            {"five": 90.0, "week": 90.0, "own5": False, "ownWeek": False},
            {"five": 90.0, "week": 92.0, "own5": False, "ownWeek": True}]


def test_lines_payloads_have_no_identity(temp_home, tmp_path, monkeypatch):
    # S9 tray and window part: email, org name and alias all hold a@example.com
    s = store(temp_home, 3)
    data = s._get_sequence_data()
    for rec in data["accounts"].values():
        rec["email"] = "a@example.com"
        rec["organizationName"] = "a@example.com"
        rec["alias"] = "a@example.com"
    data["accounts"]["2"]["weeklyThreshold"] = 92.0
    s._write_json(s.sequence_file, data)
    cards = pill_cards((1, 2, 3))
    for c in cards:
        c["title"], c["tag"] = "#%s  a@example.com" % c["num"], "a@example.com"
    core = lambda: AutoSwitchSettings(threshold=90.0, weekly_threshold=97.0)  # noqa: E731
    shown, general = tray.lines_inputs(cards, s.account_policies, core)
    monkeypatch.setattr(pill, "work_area", lambda: AREA)
    payload = pill.pill_payload(shown, "right", "black", pill.window_layout("right", 3, AREA),
                                general_lines=general, card_limits_open=True)
    window = lines_page.LinesWindowApi(core, s.account_policies, lambda: shown, lambda: True,
                                       lambda c, a: {"ok": False, "output": "bad a@example.com"},
                                       lambda: None, lambda: None)
    lines = Lines(s, tmp_path, nums=(1, 2, 3))
    out = [payload, window.get(), window.set("five", 80), window.close(),
           lines.bridge.set_account_line("2", "week", 101), lines.bridge.set_account_line("7", "week", 90)]
    assert out[1]["accounts"][1]["week"] == 92.0  # the window did read the store
    for o in out:
        assert "@" not in json.dumps(o), o


def test_on_lines_refuses_a_kind_of_the_wrong_type_without_a_warning(temp_home, tmp_path, caplog):
    s = store(temp_home)
    lines = Lines(s, tmp_path)
    before = Path(s.sequence_file).read_bytes()
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        for kind in (["five"], {"five": 1}, 5):
            assert lines.bridge.set_account_line("2", kind, 80) == refused(), kind
    assert caplog.records == []  # a refusal, not a failure
    assert Path(s.sequence_file).read_bytes() == before and lines.changes == []


# ---- tray.run() wiring of the lines window and on_lines (SL3, SL6) --------------------------------
# run() with fake webview, pystray, gi and PIL modules, in a temp home: no GUI starts and no real store is used.


class _Hook:
    def __init__(self):
        self.fns = []

    def __iadd__(self, fn):
        self.fns.append(fn)
        return self


class _Win:
    def __init__(self, title, kw, log):
        self.title, self.kw, self.uid, self._log = title, kw, "uid-" + title, log
        self.events = types.SimpleNamespace(closing=_Hook(), loaded=_Hook())

    def destroy(self):
        self._log.append(("destroy", self.title))


class _Stop(Exception):
    """Ends the refresh loop after one pass."""


class _Thread:
    """Records the threads run() starts; the test runs their targets itself."""

    def __init__(self, target=None, args=(), daemon=None, **_kw):
        self.target, self.args = target, args

    def start(self):
        _Thread.started.append(self)


class TrayRun:
    def __init__(self, temp_home, tmp_path, monkeypatch):
        for name in ("run", "cfg"):
            (tmp_path / name).mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))  # the single-instance lock
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))  # the autostart entry
        monkeypatch.setenv("GDK_BACKEND", "x11")  # run() sets these two with setdefault
        monkeypatch.setenv("WEBKIT_DMABUF_RENDERER_FORCE_SHM", "1")
        self.windows, self.log, self.views, self.icons, self.ready = {}, [], {}, [], []
        run = self

        webview = types.ModuleType("webview")
        webview.create_window = lambda title, **kw: run.windows.setdefault(title, _Win(title, kw, run.log))
        webview.start = lambda on_ready, icon=None: run.ready.append(on_ready)
        webview.screens = [types.SimpleNamespace(x=0, y=0, width=1920, height=1080)]
        webview.FileDialog = types.SimpleNamespace(SAVE=1, OPEN=0)
        wgtk = types.ModuleType("webview.platforms.gtk")
        wgtk.BrowserView = types.SimpleNamespace(instances=self.views)
        gir = types.ModuleType("gi.repository")
        gir.GLib = types.SimpleNamespace(idle_add=lambda fn: fn())

        class Icon:
            def __init__(self, name, image, title, menu):
                self.menu, self.title = menu, title
                run.icons.append(self)

            def update_menu(self):
                pass

            def run_detached(self):
                pass

            def notify(self, *_a):
                pass

            def stop(self):
                run.log.append(("stop", "icon"))

        pystray = types.ModuleType("pystray")
        pystray.Icon = Icon
        pystray.Menu = type("Menu", (), {"SEPARATOR": None, "__init__": lambda self, *items: setattr(self, "items", items)})
        pystray.MenuItem = lambda label, action=None, **_kw: (label, action)
        image = types.SimpleNamespace(save=lambda *_a: None)
        pil = types.ModuleType("PIL")
        pil.Image = types.SimpleNamespace(open=lambda _p: image, new=lambda *_a: image)
        for name, module in {"webview": webview, "webview.platforms": types.ModuleType("webview.platforms"),
                             "webview.platforms.gtk": wgtk, "gi": types.ModuleType("gi"), "gi.repository": gir,
                             "pystray": pystray, "PIL": pil}.items():
            monkeypatch.setitem(sys.modules, name, module)

        _Thread.started = []
        monkeypatch.setattr(tray, "threading", types.SimpleNamespace(Event=threading.Event, Thread=_Thread))
        monkeypatch.setattr(tray, "REFRESH_INTERVAL_S", 0)
        monkeypatch.setattr(pill, "work_area", lambda: AREA)
        monkeypatch.setattr(budget_source, "current_budget", lambda *_a, **_k: {"accounts": []})

        # The store has accounts 1-3 with an own weekly line on 2; the pill shows accounts 1 and 2 only.
        self.switcher = store(temp_home, 3)
        assert self.switcher.write_account_line("2", "weeklyThreshold", 92)["ok"]
        set_setting(Path(self.switcher.backup_dir), "autoswitch.weeklyThreshold", "97")
        self.cards = pill_cards((1, 2))
        snaps = iter([{"accounts": self.cards}])

        def snapshot(_api):
            try:
                return next(snaps)
            except StopIteration:
                raise _Stop from None

        monkeypatch.setattr(desktop.DesktopApi, "snapshot", snapshot)

    def start(self):
        assert tray.run(self.switcher) == 0
        self.ready[0]()  # the webview ready callback: it starts the threads
        refresh = next(t.target for t in _Thread.started if t.target.__name__ == "refresh_loop")
        with pytest.raises(_Stop):
            refresh()  # one pass, then the second snapshot ends the loop
        self.ctl = self.windows["cswap pill"].kw["js_api"]._c
        self.bridge = self.windows["cswap pill"].kw["js_api"]
        self.lines = self.windows["cswap lines"]
        self.view = self.views.setdefault(self.lines.uid, FakeView())
        return self

    def menu_action(self, label):
        items = [i for i in self.icons[0].menu.items[0]() if i is not None]  # None: a separator
        return next(action for text, action in items if text == label)


@pytest.fixture
def tray_run(temp_home, tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("the pill and the lines window are Linux only")
    return TrayRun(temp_home, tmp_path, monkeypatch).start()


def test_run_gives_the_pill_the_card_lines_and_the_general_lines(tray_run):
    ctl = tray_run.ctl
    assert [c["num"] for c in ctl.cards] == ["1", "2"]
    assert [c["lines"] for c in ctl.cards] == [
        {"five": 90.0, "week": 97.0, "own5": False, "ownWeek": False},
        {"five": 90.0, "week": 92.0, "own5": False, "ownWeek": True}]
    assert ctl.general_lines == {"five": 90.0, "week": 97.0}


def test_run_creates_the_lines_window(tray_run):
    kw = tray_run.lines.kw
    assert {k: v for k, v in kw.items() if k not in ("html", "js_api")} == {
        "width": 320, "height": 520, "resizable": False, "background_color": "#000000", "hidden": True}
    assert kw["html"] == lines_page.LINES_PAGE and isinstance(kw["js_api"], lines_page.LinesWindowApi)


def test_run_gear_toggles_the_lines_window(tray_run):
    assert tray_run.bridge.lines_window() == OK  # the pill got on_lines: the first gear click opens
    x, y = fleet_page.panel_position(tray_run.ctl.gear_anchor(), size=(320, 520))
    assert tray_run.view.log == [("show_all",), ("deiconify",), ("present",), ("move", x, y),
                                 ("js", "window.linesOpen&&window.linesOpen()")]
    tray_run.view.log.clear()
    assert tray_run.bridge.lines_window() == OK  # the second closes
    assert tray_run.view.log == [("js", "window.linesClosed&&window.linesClosed()"), ("hide",)]


def test_run_on_lines_checks_the_num_against_the_pill(tray_run):
    s = tray_run.switcher
    before = Path(s.sequence_file).read_bytes()
    assert tray_run.bridge.set_account_line("3", "five", 80) == refused()  # in the store, not in the pill
    assert Path(s.sequence_file).read_bytes() == before
    assert tray_run.bridge.set_account_line("1", "five", 80) == OK
    assert s.account_policies()["1"].threshold == 80.0
    assert tray_run.bridge.set_card_open(True) == OK and tray_run.ctl.settings.pill_card_limits_open is True


def test_run_lines_window_api_order_of_args(tray_run):
    api = tray_run.lines.kw["js_api"]
    got = api.get()  # core settings, policies, the tray's cards and the auto-switch state, each in its place
    assert {k: got[k] for k in ("five", "week", "autoSwitch")} == {"five": 90.0, "week": 97.0, "autoSwitch": False}
    assert got["accounts"] == [
        {"num": "1", "five": 90.0, "week": 97.0, "own5": False, "ownWeek": False, "use5": 10.0, "useWeek": 5.0},
        {"num": "2", "five": 90.0, "week": 92.0, "own5": False, "ownWeek": True, "use5": 20.0, "useWeek": 10.0}]
    tray_run.ctl.settings.auto_switch = True
    assert api.get()["autoSwitch"] is True
    assert api.set("five", 85) == OK and tray_run.view.log == []  # save_settings writes; nothing hides
    assert load_settings(Path(tray_run.switcher.backup_dir)).threshold == 85.0
    assert api.close() == OK and tray_run.view.log[-1] == ("hide",)  # close() hides the window


def test_run_lines_window_closing_hides_and_quit_destroys_it(tray_run):
    closing = tray_run.lines.events.closing.fns
    assert len(closing) == 1
    assert closing[0]() is False  # the ✕ of the window manager hides it; the close is cancelled
    assert tray_run.view.log == [("js", "window.linesClosed&&window.linesClosed()"), ("hide",)]
    tray_run.menu_action("Quit")()
    assert ("destroy", "cswap lines") in tray_run.log
    tray_run.view.log.clear()
    assert closing[0]() is None and tray_run.view.log == []  # while quitting, the window may close
