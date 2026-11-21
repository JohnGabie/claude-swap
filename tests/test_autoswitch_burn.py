"""Burn lookahead and the runway rule: the incident of 2026-11-16.

The engine moved from a Max account at 80% to a Pro account at 29% under a
heavy load. The Pro account read 29, 48, 82, 84 percent on readings 1 to 4
minutes apart and was full before the next one: the work stopped.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from claude_swap import autoswitch, pace_feed
from claude_swap.autoswitch import TickOutcome
from tests.test_autoswitch import EngineHarness, _usage


def _fleet(temp_home: Path, strategy: str, threshold: float = 99.0) -> EngineHarness:
    h = EngineHarness(temp_home, strategy=strategy, threshold=threshold)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    return h


def _switches(h) -> list[tuple]:
    return [(e.trigger, e.from_ref["number"], e.to_ref["number"]) for e in h.events if e.kind == "switch"]


class TestBurnLookahead:
    def test_a_fast_burn_leaves_before_the_line(self, temp_home):
        h = _fleet(temp_home, "best")
        assert h.tick_with_usage({"1": _usage(48), "2": _usage(0)}) is TickOutcome.NO_ACTION
        h.clock.advance(231)  # 19:28:35 -> 19:32:26 in the incident
        # 34 points in 231 s; at 82% the next reading would be over the line.
        assert h.tick_with_usage({"1": _usage(82), "2": _usage(0)}) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_a_slow_burn_stays_until_the_line(self, temp_home):
        h = _fleet(temp_home, "best")
        h.tick_with_usage({"1": _usage(80), "2": _usage(0)})
        h.clock.advance(300)
        # 1 point in 5 minutes: about 1 more before the next reading.
        assert h.tick_with_usage({"1": _usage(81), "2": _usage(0)}) is TickOutcome.NO_ACTION
        h.clock.advance(300)
        assert h.tick_with_usage({"1": _usage(96), "2": _usage(0)}) is TickOutcome.SWITCHED

    def test_the_lookahead_is_capped(self, temp_home):
        h = _fleet(temp_home, "best")
        h.tick_with_usage({"1": _usage(0), "2": _usage(0)})
        h.clock.advance(60)
        # A jump of 55 points in a minute lowers the line by 40 at most: 99 - 40 = 59.
        assert h.tick_with_usage({"1": _usage(55), "2": _usage(0)}) is TickOutcome.NO_ACTION
        assert h.engine._burn_rate > 0

    def test_a_window_reset_clears_the_rate(self, temp_home):
        h = _fleet(temp_home, "best")
        h.tick_with_usage({"1": _usage(60), "2": _usage(0)})
        h.clock.advance(60)
        h.tick_with_usage({"1": _usage(5), "2": _usage(0)})
        assert h.engine._burn_rate == 0.0

    def test_the_same_reading_twice_keeps_the_rate(self, temp_home):
        h = _fleet(temp_home, "best")
        entries = lambda pct, at: {  # noqa: E731
            "1": autoswitch.UsageEntry(last_good=_usage(pct), fetched_at=at, age_s=0.0),
            "2": autoswitch.UsageEntry(last_good=_usage(0), fetched_at=at, age_s=0.0),
        }
        t0 = h.clock.now
        h.tick_with_entries(entries(10, t0))
        h.clock.advance(100)
        h.tick_with_entries(entries(20, t0 + 100))
        rate = h.engine._burn_rate
        assert rate == 0.1
        h.clock.advance(60)
        h.tick_with_entries(entries(20, t0 + 100))  # no new fetch: not a rate of zero
        assert h.engine._burn_rate == rate

    def test_a_new_active_account_starts_with_no_rate(self, temp_home):
        h = _fleet(temp_home, "best")
        h.tick_with_usage({"1": _usage(48), "2": _usage(0)})
        h.clock.advance(231)
        h.tick_with_usage({"1": _usage(82), "2": _usage(0)})  # switches to 2
        h.clock.advance(60)
        h.tick_with_usage({"2": _usage(1), "1": _usage(90)})
        assert h.engine._burn_rate == 0.0


class TestRunwayRuleInTheEngine:
    def _plans(self, plans):
        return patch.object(pace_feed, "read_plan_for", lambda _sw, num, _email, _active: plans[num])

    def test_the_incident_stays_on_the_max_account(self, temp_home):
        # Account 1 is Max; its readings are the ones of the incident
        # (19:16:40 37%, 19:19:43 55%, 19:22:46 66%, 19:26:35 80%).
        # Account 2 is Pro at 29%: less 5-hour room than the Max has left.
        h = _fleet(temp_home, "balanced")
        with self._plans({"1": "max", "2": "pro"}):
            for wait, pct in ((0, 37), (183, 55), (183, 66), (229, 80)):
                h.clock.advance(wait)
                h.tick_with_usage({"1": _usage(pct), "2": _usage(29)})
        assert _switches(h) == [] and h.active_number() == 1

    def test_without_a_burn_the_balanced_move_still_happens(self, temp_home):
        h = _fleet(temp_home, "balanced")
        with self._plans({"1": "max", "2": "pro"}):
            from tests.test_autoswitch import _usage7  # noqa: PLC0415

            later = "2099-01-08T00:00:00Z"
            h.tick_with_usage({"1": _usage7(80, 60, later), "2": _usage7(29, 0, later)})
        assert _switches(h) == [("balanced", 1, 2)]


class TestUrgentPollingByRate:
    def _plan(self, prev, new, threshold=99.0):
        from claude_swap import poll_policy

        return poll_policy.plan_after_fetch(
            prev_interval_s=180.0, prev_usage=_usage(prev), new_usage=_usage(new),
            is_active=True, threshold=threshold, models=(), recent_429=False,
            now=0.0, rng=lambda: 0.5,
        )[1]

    def test_a_fast_rise_outside_the_band_is_urgent(self):
        # The incident: 29% -> 48% is far from the 99% line, but 3 more such polls cross it.
        assert self._plan(29, 48) == 60.0

    def test_a_slow_rise_outside_the_band_keeps_the_normal_cadence(self):
        assert self._plan(40, 44) == 180.0

    def test_the_band_is_still_urgent(self):
        assert self._plan(88, 90) == 60.0


# ---- week of 2026-11-16: findings of the data review ------------------------

from claude_swap import balance  # noqa: E402
from tests.test_autoswitch import _usage7  # noqa: E402


class TestLookaheadIsFiveHourOnly:
    def test_a_weekly_percent_near_the_line_does_not_force_a_switch(self, temp_home):
        # 2026-11-18 06:30: 5h at 16%, 7d at 87%, the lowered line was 83%.
        h = _fleet(temp_home, "best")
        h.tick_with_usage({"1": _usage7(10, 87), "2": _usage7(0, 10)})
        h.clock.advance(60)
        h.events.clear()
        assert h.tick_with_usage({"1": _usage7(30, 87), "2": _usage7(0, 10)}) is TickOutcome.NO_ACTION
        assert _switches(h) == []

    def test_the_poll_event_logs_the_lookahead_and_raw_percents(self, temp_home):
        h = _fleet(temp_home, "best")
        h.tick_with_usage({"1": _usage(10), "2": _usage(0)})
        h.clock.advance(60)
        h.events.clear()
        h.tick_with_usage({"1": _usage(30), "2": _usage(0)})
        poll = next(e for e in h.events if e.kind == "poll").to_json()
        assert poll["threshold"] == 99.0
        assert poll["windowsPct"]["1"]["5h"] == 30.0
        assert poll["fiveHourLookaheadPct"] == 20.0

    def test_project_five_hour_copies_and_caps(self):
        usage = {"1": {"five_hour": {"pct": 90.0, "resets_at": "x"}, "seven_day": {"pct": 50.0}}, "2": None}
        out = autoswitch._project_five_hour(usage, "1", 25.0)
        assert out["1"]["five_hour"] == {"pct": 100.0, "resets_at": "x"}
        assert out["1"]["seven_day"] == {"pct": 50.0}
        assert usage["1"]["five_hour"]["pct"] == 90.0  # not mutated
        assert autoswitch._project_five_hour(usage, "1", 0.0) == usage
        assert autoswitch._project_five_hour(usage, "2", 10.0) == usage


class TestBurstAndPollPlan:
    def _entries(self, pct, at, next_at=None):
        return {
            "1": autoswitch.UsageEntry(last_good=_usage(pct), fetched_at=at, age_s=0.0, next_poll_at=next_at),
            "2": autoswitch.UsageEntry(last_good=_usage(0), fetched_at=at, age_s=0.0),
        }

    def test_a_burst_then_a_slow_interval_keeps_the_burst_rate(self, temp_home):
        # 2026-11-17 04:36-04:42: 64% -> 77% -> 79%, next reading 3 minutes later, then 100%.
        h = _fleet(temp_home, "best")
        t0 = h.clock.now
        h.tick_with_entries(self._entries(64, t0))
        h.clock.advance(60)
        h.tick_with_entries(self._entries(77, t0 + 60))
        h.clock.advance(60)
        h.events.clear()
        out = h.tick_with_entries(self._entries(79, t0 + 120, next_at=t0 + 300))
        assert out is TickOutcome.SWITCHED and h.active_number() == 2


class TestForcedSwitchLastFallback:
    NOW = 1_000_000.0
    WEEK_END = NOW + 84 * 3600.0

    def _acc(self, num, plan, five, week):
        return balance.Account(num, plan, balance.Window(five, self.NOW + 3600.0),
                               balance.Window(week, self.WEEK_END))

    def test_the_2026_11_16_stop_moves_to_the_max_at_91(self):
        accounts = [self._acc(3, "pro", 100.0, 65.0), self._acc(4, "pro", 100.0, 36.0),
                    self._acc(5, "max", 91.0, 19.0), self._acc(1, "pro", 0.0, 100.0)]
        out = balance.decide(accounts, 3, self.NOW, None, None, True, threshold=99.0,
                             candidate_margin=5.0)
        assert isinstance(out, balance.SwitchTo) and out.account_id == 5 and out.reason == "threshold"

    def test_an_account_at_or_over_the_line_is_still_refused(self):
        accounts = [self._acc(3, "pro", 100.0, 65.0), self._acc(5, "max", 99.0, 19.0)]
        out = balance.decide(accounts, 3, self.NOW, None, None, True, threshold=99.0)
        assert isinstance(out, balance.Stay)

    def test_a_proactive_move_keeps_the_headroom_gate(self):
        accounts = [self._acc(3, "pro", 50.0, 90.0), self._acc(5, "max", 92.0, 0.0)]
        out = balance.decide(accounts, 3, self.NOW, None, None, False, threshold=99.0)
        assert not (isinstance(out, balance.SwitchTo) and out.account_id == 5)

    def test_the_engine_moves_instead_of_staying_at_the_limit(self, temp_home):
        h = _fleet(temp_home, "balanced")
        with patch.object(pace_feed, "read_plan_for", lambda _sw, num, _e, _a: {"1": "pro", "2": "max"}[num]):
            h.events.clear()
            out = h.tick_with_usage({"1": _usage7(100, 65), "2": _usage7(91, 19)})
        assert out is TickOutcome.SWITCHED and h.active_number() == 2
