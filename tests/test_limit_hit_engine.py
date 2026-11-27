"""The engine and the limit-hit watcher (LH2, LH3, LH4; scenarios S2, S3, S4).

Every engine here gets a fake watcher: no test reads ``~/.claude/projects``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import autoswitch, budget_calibrate, decision_log, pace_feed, poll_policy, token_model
from claude_swap.autoswitch import TickOutcome
from claude_swap.budget_source import CALIBRATION_FILENAME
from claude_swap.limit_watch import LimitHit, LimitWatch
from claude_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, _iso_at, _usage, _usage7

# S1: the hit line of 2026-11-27 19:21:22 local (22:21:22.437Z).
S1_TS = 1795818082.437
# S2: the last switch to the active account, 22:21:20Z.
S2_SWITCH_TS = 1795818080.0
# S4: the measured 5-hour rate of account 1 and the tokens after the reading.
S4_RATE = 34794.0
S4_TOKENS = 3_500_000.0
FIVE_POINTS = 170.0  # the own 5-hour points of the calibration store below
HOUR_S = 3600.0


class FakeLimitWatch:
    """Stands in for ``LimitWatch``: hits and tokens are set by the test."""

    def __init__(self) -> None:
        self.hits: list[LimitHit] = []
        self.polls = 0
        self.tokens = 0.0
        self.since: list[float] = []
        self.poll_error: Exception | None = None
        self.tokens_error: Exception | None = None

    def poll(self) -> list[LimitHit]:
        self.polls += 1
        if self.poll_error is not None:
            error, self.poll_error = self.poll_error, None
            raise error
        hits, self.hits = self.hits, []
        return hits

    def tokens_since(self, ts: float) -> float:
        self.since.append(ts)
        if self.tokens_error is not None:
            raise self.tokens_error
        return self.tokens


def _harness(temp_home: Path, nums=(1, 2), *, now: float | None = None, **settings) -> EngineHarness:
    settings.setdefault("threshold", 99.0)
    h = EngineHarness(temp_home, **settings)
    if now is not None:
        h.clock.now = now
    h.watch = FakeLimitWatch()
    h.engine = h._make_engine(limit_watch=h.watch)
    for num in nums:
        h.seed(num, f"{chr(96 + num)}@example.com")
    h.make_live(f"{chr(96 + nums[0])}@example.com", nums[0])
    return h


def _write_state(h: EngineHarness, **state) -> None:
    path = h.switcher.backup_dir / autoswitch.STATE_FILENAME
    path.write_text(json.dumps(state))


def _switches(h: EngineHarness) -> list[tuple]:
    return [(e.trigger, e.from_ref["number"], e.to_ref["number"]) for e in h.events if e.kind == "switch"]


def _hits(h: EngineHarness) -> list[dict]:
    return [
        {"account": e.to_json()["account"], "window": e.to_json()["window"]}
        for e in h.events if e.kind == "limit-hit"
    ]


def _entries(usage: dict, now: float, **fields) -> dict[str, UsageEntry]:
    return {
        num: UsageEntry(last_good=value, fetched_at=now, age_s=0.0, **fields)
        for num, value in usage.items()
    }


def _run_loop(h: EngineHarness, entries: dict, *, delay: float = 12.0, on_wait=None, max_waits: int = 6):
    """Run ``run_loop`` on ``entries``; stop at wait ``max_waits``. Returns the wait timeouts."""
    waits: list[float] = []

    def wait(timeout):
        waits.append(timeout)
        if on_wait is not None:
            on_wait(len(waits))
        if len(waits) >= max_waits:
            h.engine.stop()
        return h.engine._stop.is_set()

    with patch.object(h.engine._wake, "wait", side_effect=wait), \
            patch.object(h.engine, "_next_delay", return_value=delay), \
            patch.object(h.switcher, "usage_entries_by_account", return_value=entries):
        assert h.engine.run_loop() == 0
    return waits


def _calibrate(h: EngineHarness, num: int, week_points: float, five_rate: float = S4_RATE) -> None:
    """A calibration store whose 5-hour rate for ``num`` is ``five_rate`` (plan pro)."""
    prior = token_model.PRIOR_POINTS["five"] * token_model.prior_rate("pro", "five")
    five_tokens = five_rate * (token_model.PRIOR_POINTS["five"] + FIVE_POINTS) - prior
    store = {
        "schemaVersion": budget_calibrate.STORE_VERSION,
        "lastPoll": None,
        "accounts": {str(num): {
            "week": {"points": week_points, "tokens": week_points * token_model.PRIOR_PER_PERCENT_PRO["week"]},
            "five": {"points": FIVE_POINTS, "tokens": five_tokens},
        }},
    }
    (h.switcher.backup_dir / CALIBRATION_FILENAME).write_text(json.dumps(store))


@pytest.fixture
def pro_plans():
    with patch.object(pace_feed, "read_plan_for", lambda *_a, **_k: "pro"):
        yield


def test_contract_numbers():
    assert autoswitch.LIMIT_WATCH_S == 5
    assert autoswitch.LIMIT_SLACK_S == 10


def test_engine_builds_a_watch_on_the_claude_projects_dir(temp_home):
    h = EngineHarness(temp_home)
    engine = autoswitch.AutoSwitchEngine(h.switcher, h.settings, h.events.append, clock=h.clock)
    assert isinstance(engine.limit_watch, LimitWatch)
    assert engine.limit_watch._root == temp_home / ".claude" / "projects"


# ---- S2 / S3: a hit switches at once ------------------------------------------------


@pytest.mark.parametrize(("offset", "counted"), [(S1_TS - S2_SWITCH_TS, False), (10.0, True)])
def test_hit_before_switch_slack_is_ignored(temp_home, offset, counted):
    # S2: the S1 line came 2.437 s after the switch to the active account.
    h = _harness(temp_home, (4, 5), now=S2_SWITCH_TS + 30)
    _write_state(h, lastSwitchAt=S2_SWITCH_TS, lastSwitchTo="4")
    entries = _entries({"4": _usage(79), "5": _usage(6)}, h.clock.now)

    def on_wait(n):
        if n == 1:
            h.watch.hits = [LimitHit(ts=S2_SWITCH_TS + offset, window="5h")]

    ticks: list[int] = []
    real_tick = h.engine.tick

    def tick():
        ticks.append(h.watch.polls)
        return real_tick()

    with patch.object(h.engine, "tick", side_effect=tick):
        _run_loop(h, entries, on_wait=on_wait, max_waits=3)
    if counted:
        assert _hits(h) == [{"account": 4, "window": "5h"}]
        assert _switches(h) == [("limit-hit", 4, 5)]
    else:
        assert _hits(h) == [] and _switches(h) == []
        assert ticks == [0]  # the loop kept waiting: no tick at once
        assert h.watch.polls == 2


def test_hit_after_a_switch_to_another_account_counts(temp_home):
    # The engine's last switch went to account 5; account 4 came back by a
    # login the state does not record, so no switch moment is known for it.
    h = _harness(temp_home, (4, 5), now=S2_SWITCH_TS + 30)
    _write_state(h, lastSwitchAt=S2_SWITCH_TS, lastSwitchTo="5")
    entries = _entries({"4": _usage(79), "5": _usage(6)}, h.clock.now)
    h.watch.hits = [LimitHit(ts=S1_TS, window="5h")]
    with patch.object(h.switcher, "usage_entries_by_account", return_value=entries):
        assert h.engine._poll_limit_watch() is True
    assert _hits(h) == [{"account": 4, "window": "5h"}]


def test_hit_with_no_active_account_is_ignored(temp_home, caplog):
    h = _harness(temp_home)
    h.watch.hits = [LimitHit(ts=h.clock.now, window="5h")]
    with patch.object(h.switcher, "current_account_number", return_value=None), \
            caplog.at_level(logging.DEBUG, logger="claude-swap"):
        assert h.engine._poll_limit_watch() is False
    assert _hits(h) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_hit_before_a_manual_switch_slack_is_ignored(temp_home):
    # The last switch to the active account was made by hand.
    h = _harness(temp_home, (4, 5), now=S2_SWITCH_TS + 30)
    _write_state(h, lastSwitchAt=S2_SWITCH_TS - 600, lastSwitchTo="5", lastManualSwitchAt=S2_SWITCH_TS)
    entries = _entries({"4": _usage(79), "5": _usage(6)}, h.clock.now)

    def on_wait(n):
        if n == 1:
            h.watch.hits = [LimitHit(ts=S1_TS, window="5h")]

    _run_loop(h, entries, on_wait=on_wait, max_waits=3)
    assert _hits(h) == [] and _switches(h) == []


def test_engine_switches_on_limit_hit(temp_home):
    # S3: account 4 reads 5h 79%, account 5 6%, cooldown running, hit 30 s after the switch to 4.
    h = _harness(temp_home, (4, 5), now=S2_SWITCH_TS + 30)
    _write_state(h, lastSwitchAt=S2_SWITCH_TS, lastSwitchTo="4")
    entries = _entries({"4": _usage(79), "5": _usage(6)}, h.clock.now)
    ticks: list[int] = []
    real_tick = h.engine.tick

    def tick():
        ticks.append(h.watch.polls)
        return real_tick()

    def on_wait(n):
        if n == 1:
            # Two agents stop on the same limit: one event.
            h.watch.hits = [LimitHit(ts=S2_SWITCH_TS + 30, window="5h")] * 2

    with patch.object(h.engine, "tick", side_effect=tick):
        waits = _run_loop(h, entries, on_wait=on_wait, max_waits=2)
    # One watcher step, one poll, then the tick at once.
    assert waits[0] <= autoswitch.LIMIT_WATCH_S
    assert ticks == [0, 1]
    assert _hits(h) == [{"account": 4, "window": "5h"}]
    assert _switches(h) == [("limit-hit", 4, 5)]
    kinds = h.kinds()
    assert kinds.index("limit-hit") < kinds.index("switch")
    assert h.active_number() == 5
    # The decision log: limit-hit before the switch, with no email.
    rows = [json.loads(line) for line in decision_log.tail(h.switcher.backup_dir)]
    events = [r["event"] for r in rows]
    hit = rows[events.index("limit-hit")]
    assert {k: hit[k] for k in ("event", "account", "window")} == {"event": "limit-hit", "account": 4, "window": "5h"}
    assert "ts" in hit and "@" not in json.dumps(hit)
    assert events.index("limit-hit") < events.index("switch")
    # The poll event of the hit tick logs the stored reading, not the mark.
    poll = [e for e in h.events if e.kind == "poll"][-1].to_json()
    assert poll["windowsPct"]["4"]["5h"] == 79.0


@pytest.mark.parametrize(("reading", "window"), [(_usage7(20, 90), "7d"), (_usage7(90, 20), "5h")])
def test_hit_without_a_window_marks_the_fullest_window(temp_home, reading, window):
    h = _harness(temp_home)
    entries = _entries({"1": reading, "2": _usage7(0, 0)}, h.clock.now)
    h.watch.hits = [LimitHit(ts=h.clock.now, window=None)]
    with patch.object(h.switcher, "usage_entries_by_account", return_value=entries):
        assert h.engine._poll_limit_watch() is True
        assert h.engine.tick() is TickOutcome.SWITCHED
    assert _hits(h) == [{"account": 1, "window": window}]
    assert _switches(h) == [("limit-hit", 1, 2)]


@pytest.mark.parametrize(("window", "reset_in", "horizon"), [
    ("5h", HOUR_S, HOUR_S),
    ("5h", None, 5 * HOUR_S),
    ("5h", -60.0, 5 * HOUR_S),  # a reset already past: the reading is old
    ("7d", None, 7 * 24 * HOUR_S),
])
def test_hit_marks_window_until_reset(temp_home, window, reset_in, horizon):
    h = _harness(temp_home)
    hit_ts = h.clock.now
    reset = _iso_at(hit_ts + reset_in) if reset_in is not None else None
    left = _usage(50, reset) if window == "5h" else _usage7(10, 50)
    h.watch.hits = [LimitHit(ts=hit_ts, window=window)]
    with patch.object(h.switcher, "usage_entries_by_account", return_value=_entries({"1": left, "2": _usage(0)}, hit_ts)):
        assert h.engine._poll_limit_watch() is True
        assert h.engine.tick() is TickOutcome.SWITCHED
    assert _switches(h) == [("limit-hit", 1, 2)]
    # Account 2 is now at its limit. Account 1 still reads 50%, but it is marked full.
    h.clock.now = hit_ts + horizon - 1
    assert h.tick_with_usage({"2": _usage(100), "1": left}) is TickOutcome.BLOCKED
    assert h.active_number() == 2
    h.clock.now = hit_ts + horizon + 1
    assert h.tick_with_usage({"2": _usage(100), "1": left}) is TickOutcome.SWITCHED
    assert _switches(h)[-1] == ("at-limit", 2, 1)


def test_hit_tick_fetches_fresh_readings(temp_home):
    # Like a switch at the line: the hit tick escalates to fresh readings first.
    h = _harness(temp_home)
    entries = _entries({"1": _usage(50), "2": _usage(0)}, h.clock.now)
    h.watch.hits = [LimitHit(ts=h.clock.now, window="5h")]
    with patch.object(h.switcher, "usage_entries_by_account", return_value=entries) as fetch:
        h.engine._poll_limit_watch()
        h.engine.tick()
    assert {"fetch": {"1", "2"}} in [call.kwargs for call in fetch.call_args_list]


def test_hit_names_one_tick_only(temp_home):
    # No account is usable on the hit tick; the switch of a later tick is the
    # engine's own one (account 1 is marked full: at its limit).
    h = _harness(temp_home)
    h.watch.hits = [LimitHit(ts=h.clock.now, window="5h")]
    with patch.object(h.switcher, "usage_entries_by_account",
                      return_value=_entries({"1": _usage(50), "2": _usage(100)}, h.clock.now)):
        assert h.engine._poll_limit_watch() is True
        assert h.engine.tick() is TickOutcome.BLOCKED
    assert _hits(h) == [{"account": 1, "window": "5h"}]
    h.clock.advance(60)
    assert h.tick_with_usage({"1": _usage(50), "2": _usage(0)}) is TickOutcome.SWITCHED
    assert _switches(h) == [("at-limit", 1, 2)]


def test_hit_of_another_account_does_not_name_the_switch(temp_home):
    h = _harness(temp_home, (1, 2, 3))
    h.watch.hits = [LimitHit(ts=h.clock.now, window="5h")]
    with patch.object(h.switcher, "usage_entries_by_account",
                      return_value=_entries({"1": _usage(50)}, h.clock.now)):
        assert h.engine._poll_limit_watch() is True
    h.make_live("b@example.com", 2)  # the user moved to account 2 by hand
    assert h.tick_with_usage({"2": _usage(100), "1": _usage(50), "3": _usage(0)}) is TickOutcome.SWITCHED
    assert _switches(h) == [("at-limit", 2, 3)]


def test_mark_on_an_unknown_reading_is_skipped(temp_home):
    h = _harness(temp_home)
    h.engine._limit_marks["2"] = {"5h": h.clock.now + HOUR_S}
    assert h.tick_with_usage({"1": _usage(10), "2": None}) is TickOutcome.NO_ACTION
    assert not any(e.kind == "error" for e in h.events)


def test_dry_run_names_the_hit(temp_home):
    h = _harness(temp_home)
    h.engine = h._make_engine(limit_watch=h.watch, dry_run=True)
    h.watch.hits = [LimitHit(ts=h.clock.now, window="5h")]
    with patch.object(h.switcher, "usage_entries_by_account",
                      return_value=_entries({"1": _usage(50), "2": _usage(0)}, h.clock.now)):
        assert h.engine._poll_limit_watch() is True
        assert h.engine.tick() is TickOutcome.SWITCHED
    switch = next(e for e in h.events if e.kind == "switch")
    assert switch.dry_run and switch.trigger == "limit-hit"
    assert h.active_number() == 1


def test_hit_asks_a_reading_of_the_old_account_on_the_next_tick(temp_home):
    h = _harness(temp_home)
    later = h.clock.now + 300
    entries = _entries({"1": _usage(50), "2": _usage(0)}, h.clock.now, next_poll_at=later)
    h.watch.hits = [LimitHit(ts=h.clock.now, window="5h")]
    with patch.object(h.switcher, "usage_entries_by_account", return_value=entries) as fetch:
        h.engine._poll_limit_watch()
        assert h.engine.tick() is TickOutcome.SWITCHED
        fetch.reset_mock()
        h.clock.advance(60)
        h.engine.tick()
        plans = [call.kwargs.get("fetch") for call in fetch.call_args_list]
        fetch.reset_mock()
        h.clock.advance(60)
        h.engine.tick()
        again = [call.kwargs.get("fetch") for call in fetch.call_args_list]
    assert {"1"} in plans  # account 1 is not due: only the hit asks for it
    assert {"1"} not in again  # one reading, not one each tick


@pytest.mark.parametrize("case", ["no hit", "idle hold"])
def test_old_account_reading_only_after_a_hit_and_outside_an_idle_hold(temp_home, case):
    h = _harness(temp_home)
    later = h.clock.now + 300
    if case == "no hit":
        entries = _entries({"1": _usage(100), "2": _usage(0)}, h.clock.now, next_poll_at=later)
    else:
        entries = _entries({"1": _usage(50), "2": _usage(0)}, h.clock.now, next_poll_at=later)
        h.watch.hits = [LimitHit(ts=h.clock.now, window="5h")]
    with patch.object(h.switcher, "usage_entries_by_account", return_value=entries) as fetch:
        h.engine._poll_limit_watch()
        assert h.engine.tick() is TickOutcome.SWITCHED
        fetch.reset_mock()
        h.clock.advance(60)
        if case == "idle hold":
            # Account 2 is in an idle hold: no candidate is read.
            _write_state(h, **h.state(), idleHoldSince=h.clock.now - 30,
                         healthAccount="2", healthTickAt=h.clock.now - 30)
        h.engine.tick()
        plans = [call.kwargs.get("fetch") for call in fetch.call_args_list]
    assert {"1"} not in plans


def test_limit_hit_departure_is_recoverable(temp_home):
    # After its window reset, an account left on a hit is recovered like one left at its limit.
    h = _harness(temp_home)
    now = h.clock.now
    state = {"lastSwitchFrom": 1, "lastSwitchTo": "2", "lastSwitchAt": now - 60,
             "leftTrigger": "limit-hit", "leftRecoveryAt": now - 1}
    entries = _entries({"1": _usage(0), "2": _usage(100)}, now)
    barred = h.engine._forced_no_return_account(
        "at-limit", state, entries, {n: e.last_good for n, e in entries.items()},
        h.settings, autoswitch.ThresholdMap({}, h.settings.threshold), "2", now,
    )
    assert barred is None


# ---- the loop ------------------------------------------------------------------------


def test_loop_polls_the_watcher_every_step(temp_home):
    h = _harness(temp_home)
    calls: list[int] = []

    def tick():
        calls.append(h.watch.polls)
        if len(calls) == 2:
            h.engine.stop()
        return TickOutcome.NO_ACTION

    with patch.object(h.engine, "tick", side_effect=tick):
        waits = _run_loop(h, {}, delay=12.0, max_waits=10)
    # 12 s between the two ticks: steps of 5, 5 and 2, a poll after each.
    assert waits[:3] == [5.0, 5.0, 2.0]
    assert calls == [0, 3]


def test_watcher_error_does_not_stop_the_loop(temp_home, caplog):
    h = _harness(temp_home)
    transcript = temp_home / ".claude" / "projects" / "x.jsonl"
    h.watch.poll_error = RuntimeError(f"a@example.com in {transcript}")
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        waits = _run_loop(h, _entries({"1": _usage(10), "2": _usage(0)}, h.clock.now), max_waits=4)
    assert len(waits) == 4 and h.watch.polls == 3
    assert not any(e.kind == "error" for e in h.events)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged
    assert "@" not in logged and str(temp_home) not in logged


def test_tokens_since_error_does_not_stop_the_tick(temp_home, pro_plans, caplog):
    h = _harness(temp_home)
    _calibrate(h, 1, week_points=150.0)
    h.watch.tokens_error = OSError(str(temp_home / ".claude" / "projects"))
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        assert h.tick_with_usage({"1": _usage(99.5), "2": _usage(0)}) is TickOutcome.SWITCHED
    assert h.watch.since, "the estimate was tried"
    assert _switches(h) == [("proactive", 1, 2)]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "OSError" in logged and str(temp_home) not in logged


# ---- S4: the estimate between readings ---------------------------------------------


def test_estimate_switches_before_the_reading(temp_home, pro_plans):
    h = _harness(temp_home)
    _calibrate(h, 1, week_points=150.0)
    fetched = h.clock.now - 120
    h.watch.tokens = S4_TOKENS  # 3,500,000 / 34794 = 100.59 points
    entries = {
        "1": UsageEntry(last_good=_usage(0), fetched_at=fetched, age_s=120.0),
        "2": UsageEntry(last_good=_usage(0), fetched_at=h.clock.now, age_s=0.0),
    }
    assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
    assert _switches(h) == [("estimate", 1, 2)]
    assert fetched in h.watch.since
    # The poll event keeps the stored reading.
    poll = next(e for e in h.events if e.kind == "poll").to_json()
    assert poll["windowsPct"]["1"]["5h"] == 0.0


def test_estimate_counts_only_tokens_after_the_switch(temp_home, pro_plans):
    h = _harness(temp_home)
    _calibrate(h, 1, week_points=150.0)
    fetched = h.clock.now - 120
    _write_state(h, lastSwitchAt=fetched + 60, lastSwitchTo="1")
    entries = {
        "1": UsageEntry(last_good=_usage(0), fetched_at=fetched, age_s=120.0),
        "2": UsageEntry(last_good=_usage(0), fetched_at=h.clock.now, age_s=0.0),
    }
    h.tick_with_entries(entries)
    assert h.watch.since and set(h.watch.since) == {fetched + 60}


@pytest.mark.parametrize(("week_points", "switches"), [(0.0, False), (20.0, False), (50.0, True), (150.0, True)])
def test_estimate_needs_confidence(temp_home, pro_plans, week_points, switches):
    h = _harness(temp_home)
    _calibrate(h, 1, week_points=week_points)
    h.watch.tokens = S4_TOKENS
    out = h.tick_with_usage({"1": _usage(0), "2": _usage(0)})
    assert out is (TickOutcome.SWITCHED if switches else TickOutcome.NO_ACTION)
    assert _switches(h) == ([("estimate", 1, 2)] if switches else [])


def test_estimate_at_the_line_switches(temp_home, pro_plans):
    h = _harness(temp_home)
    _calibrate(h, 1, week_points=150.0)
    h.watch.tokens = 99.0 * S4_RATE  # the estimate is the line exactly
    assert h.tick_with_usage({"1": _usage(0), "2": _usage(0)}) is TickOutcome.SWITCHED
    assert _switches(h) == [("estimate", 1, 2)]


def test_estimate_does_not_name_a_switch_over_the_line(temp_home, pro_plans):
    # The reading alone is over the line: the switch is the engine's own one.
    h = _harness(temp_home)
    _calibrate(h, 1, week_points=150.0)
    h.watch.tokens = S4_TOKENS
    assert h.tick_with_usage({"1": _usage(99.5), "2": _usage(0)}) is TickOutcome.SWITCHED
    assert _switches(h) == [("proactive", 1, 2)]


def test_estimate_and_balanced_read_each_plan_once_per_tick(temp_home):
    from tests.test_autoswitch_balanced import PRO_BETTER, _balanced, _spy

    h = _balanced(temp_home)
    watch = FakeLimitWatch()
    watch.tokens = 1.0
    h.engine = h._make_engine(limit_watch=watch)
    _calibrate(h, int(h.switcher.current_account_number()), week_points=150.0)
    with _spy(h, "plan_of") as calls:
        assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
    assert watch.since, "the estimate read the plan"
    assert sorted(calls) == ["1", "2", "3", "4", "5"]


@pytest.mark.parametrize(("points", "escalates"), [
    (99.0 - poll_policy.ESCALATION_MARGIN_PCT, True),
    (90.0, True),
    (99.0 - poll_policy.ESCALATION_MARGIN_PCT - 1, False),
])
def test_estimate_starts_urgent_reading(temp_home, pro_plans, points, escalates):
    h = _harness(temp_home)
    _calibrate(h, 1, week_points=150.0)
    h.watch.tokens = points * S4_RATE
    entries = _entries({"1": _usage(0), "2": _usage(0)}, h.clock.now)
    with patch.object(h.switcher, "usage_entries_by_account", return_value=entries) as fetch:
        assert h.engine.tick() is TickOutcome.NO_ACTION
    fetched = [call.kwargs for call in fetch.call_args_list]
    assert ({"fetch": {"1", "2"}} in fetched) is escalates
    assert _switches(h) == []


def test_marks_hold_after_the_consume_first_refetch(temp_home):
    # Account 1 (left on a hit) reads 10% and resets first: the fresh
    # readings of the two-phase commit must not lose its mark.
    h = _harness(temp_home, (2, 1, 3), strategy="consume-first")
    now = h.clock.now
    soon, mid, late = (_iso_at(now + d * HOUR_S) for d in (10, 20, 30))
    h.engine._limit_marks["1"] = {"5h": now + HOUR_S}
    out = h.tick_with_usage({"2": _usage7(10, 50, late), "1": _usage7(10, 10, soon), "3": _usage7(10, 10, mid)})
    assert out is TickOutcome.SWITCHED
    assert _switches(h) == [("consume-first", 2, 3)]


def test_marks_hold_after_the_balanced_refetch(temp_home, pro_plans):
    h = _harness(temp_home, (2, 1, 3), strategy="balanced")
    now = h.clock.now
    week = _iso_at(now + 48 * HOUR_S)
    h.engine._limit_marks["1"] = {"5h": now + HOUR_S}
    out = h.tick_with_usage({"2": _usage7(0, 90, week), "1": _usage7(0, 0, week), "3": _usage7(0, 10, week)})
    assert out is TickOutcome.SWITCHED
    assert _switches(h) == [("balanced", 2, 3)]


# ---- review fixes ---------------------------------------------------------------------


def test_weekly_mark_sets_only_the_seven_day_window(temp_home):
    h = _harness(temp_home)
    h.engine._limit_marks["1"] = {"7d": h.clock.now + HOUR_S}
    out = h.engine._with_limit_marks({"1": _usage7(20, 40), "2": _usage7(5, 5)})
    assert out["1"]["seven_day"]["pct"] == 100.0
    assert out["1"]["five_hour"] == {"pct": 20}
    assert out["2"] == _usage7(5, 5)


def test_corrupt_calibration_store_does_not_stop_the_tick(temp_home, pro_plans, caplog):
    # A store row that is not a dict makes budget_calibrate.rates raise
    # AttributeError: the tick goes on with no estimate.
    h = _harness(temp_home)
    store = {"schemaVersion": budget_calibrate.STORE_VERSION, "lastPoll": None,
             "accounts": {"1": ["not", "a", "row"]}}
    (h.switcher.backup_dir / CALIBRATION_FILENAME).write_text(json.dumps(store))
    h.watch.tokens = S4_TOKENS
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        assert h.tick_with_usage({"1": _usage(10), "2": _usage(0)}) is TickOutcome.NO_ACTION
    assert h.watch.since, "the estimate was tried"
    assert not any(e.kind == "error" for e in h.events)
    rate_lines = {r.getMessage() for r in caplog.records if "token rate" in r.getMessage()}
    assert rate_lines == {"limit-watch: no token rate (AttributeError)"}
    assert str(temp_home) not in "\n".join(r.getMessage() for r in caplog.records)


def test_estimate_switch_sees_the_cooldown_under_the_lock(temp_home, pro_plans):
    # Engine A decides an estimate switch on a state with no cooldown. Before
    # A takes the switch lock, engine B switches: A must see B's cooldown.
    h = _harness(temp_home, (1, 2, 3))
    _calibrate(h, 1, week_points=150.0)
    h.watch.tokens = S4_TOKENS
    other = h._make_engine(limit_watch=FakeLimitWatch())
    real_freshen = h.engine._freshen_target

    def freshen(num, email):
        if not h.state():
            other._perform("3", "c@example.com", "proactive", (None, float("inf")))
        return real_freshen(num, email)

    with patch.object(h.engine, "_freshen_target", side_effect=freshen):
        out = h.tick_with_usage({"1": _usage(0), "2": _usage(0), "3": _usage(0)})
    assert out is TickOutcome.NO_ACTION
    assert _switches(h) == [("proactive", 1, 3)]  # B's switch only
    assert [e.reason for e in h.events if e.kind == "no-switch"][-1] == "cooldown"


@pytest.mark.parametrize("reading", ["unknown", "distrusted"])
def test_hit_on_an_unread_active_account_switches_at_once(temp_home, reading):
    h = _harness(temp_home)
    h.watch.hits = [LimitHit(ts=h.clock.now, window="5h")]
    usage = {"1": None if reading == "unknown" else _usage(50), "2": _usage(0)}
    with patch.object(h.switcher, "usage_entries_by_account",
                      return_value=_entries({"1": _usage(50)}, h.clock.now)):
        assert h.engine._poll_limit_watch() is True
    verdict = "distrust" if reading == "distrusted" else None
    with patch.object(h.engine, "_endpoint_trouble_verdict", return_value=verdict):
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
    assert _switches(h) == [("limit-hit", 1, 2)]
    assert not any(e.kind == "no-switch" and e.reason == "active-usage-unknown" for e in h.events)


def test_estimate_starts_at_zero_after_the_window_reset(temp_home, pro_plans):
    # The reading says 95% but its 5-hour window reset since: the estimate
    # counts from 0 and only the tokens after the reset.
    h = _harness(temp_home)
    _calibrate(h, 1, week_points=150.0)
    now = h.clock.now
    fetched, reset = now - 120, now - 60
    h.watch.tokens = 10.0 * S4_RATE  # 10 points: far under the line from 0
    entries = {
        "1": UsageEntry(last_good=_usage(95, _iso_at(reset)), fetched_at=fetched, age_s=120.0),
        "2": UsageEntry(last_good=_usage(0), fetched_at=now, age_s=0.0),
    }
    assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
    assert _switches(h) == []
    assert reset in h.watch.since
