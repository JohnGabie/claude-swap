"""S3b-1: pure builders of ``balance.Account`` objects from engine data."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from claude_swap import balance
from claude_swap.autoswitch import _balance_accounts, _balance_active_at_threshold

T = 90.0  # global line used by these tests


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _u(five, seven, r5=None, r7=None, scoped=None) -> dict:
    d: dict = {
        "five_hour": {"pct": five, **({"resets_at": _iso(r5)} if r5 else {})},
        "seven_day": {"pct": seven, **({"resets_at": _iso(r7)} if r7 else {})},
    }
    if scoped is not None:
        d["scoped"] = scoped
    return d


def _plans(mapping):
    return lambda num: mapping.get(num, "unknown")


def _build(current, candidates, usage, *, thresholds=None, weekly=None,
           models=(), plans=None):
    nums = [current, *candidates]
    th = thresholds or {n: T for n in nums}
    return _balance_accounts(
        current, candidates, usage, th, weekly, models, _plans(plans or {})
    )


def _by_id(accounts):
    return {a.id: a for a in accounts}


class TestWindows:
    def test_plain_fleet_windows_and_plan(self):
        usage = {
            "1": _u(10, 20, 5000, 9000),
            "2": _u(30, 40, 6000, 9500),
        }
        accs = _by_id(
            _build("1", ["2"], usage, plans={"1": "max", "2": "pro"})
        )
        assert accs[1].five_hour == balance.Window(10.0, 5000.0)
        assert accs[1].seven_day == balance.Window(20.0, 9000.0)
        assert accs[1].plan == "max"
        assert accs[2].five_hour == balance.Window(30.0, 6000.0)
        assert accs[2].seven_day == balance.Window(40.0, 9500.0)
        assert accs[2].plan == "pro"

    def test_missing_reset_is_none(self):
        accs = _by_id(_build("1", [], {"1": _u(10, 20)}))
        assert accs[1].five_hour == balance.Window(10.0, None)
        assert accs[1].seven_day == balance.Window(20.0, None)

    def test_weekly_is_highest_of_7d_and_scoped_with_its_reset(self):
        scoped = [{"name": "Fable", "pct": 70, "resets_at": _iso(7777)}]
        usage = {"1": _u(10, 20, 5000, 9000, scoped)}
        accs = _by_id(_build("1", [], usage, models=("Fable",)))
        assert accs[1].seven_day == balance.Window(70.0, 7777.0)

    def test_7d_wins_when_higher_than_scoped(self):
        scoped = [{"name": "Fable", "pct": 10, "resets_at": _iso(7777)}]
        usage = {"1": _u(10, 20, 5000, 9000, scoped)}
        accs = _by_id(_build("1", [], usage, models=("Fable",)))
        assert accs[1].seven_day == balance.Window(20.0, 9000.0)

    def test_no_scoped_window_when_models_empty(self):
        scoped = [{"name": "Fable", "pct": 70, "resets_at": _iso(7777)}]
        usage = {"1": _u(10, 20, 5000, 9000, scoped)}
        accs = _by_id(_build("1", [], usage, models=()))
        assert accs[1].seven_day == balance.Window(20.0, 9000.0)

    def test_sentinel_and_none_are_unmeasured(self):
        usage = {"1": _u(10, 20), "2": "token-expired", "3": None}
        accs = _by_id(_build("1", ["2", "3", "4"], usage))
        for n in (2, 3, 4):  # 4: no entry at all
            assert accs[n].five_hour is None
            assert accs[n].seven_day is None
            assert accs[n].blocked is False

    def test_id_is_int_and_order_is_current_first(self):
        accs = _build("1", ["2", "3"], {"1": _u(1, 1), "2": _u(1, 1), "3": _u(1, 1)})
        assert [a.id for a in accs] == [1, 2, 3]
        assert all(isinstance(a.id, int) for a in accs)


class TestBlocked:
    def test_over_own_line_is_blocked_under_is_not(self):
        usage = {"1": _u(0, 0), "2": _u(50, 50), "3": _u(50, 50)}
        th = {"1": T, "2": 40.0, "3": 60.0}
        accs = _by_id(_build("1", ["2", "3"], usage, thresholds=th))
        assert accs[2].blocked is True
        assert accs[3].blocked is False

    def test_weekly_line_applies_to_weekly_window(self):
        usage = {"1": _u(0, 0), "2": _u(10, 85)}
        accs = _by_id(_build("1", ["2"], usage, weekly=80.0))
        assert accs[2].blocked is True

    def test_scoped_window_over_line_blocks(self):
        scoped = [{"name": "Fable", "pct": 95}]
        usage = {"1": _u(0, 0), "2": _u(10, 10, scoped=scoped)}
        accs = _by_id(_build("1", ["2"], usage, models=("Fable",)))
        assert accs[2].blocked is True

    def test_candidate_without_a_threshold_entry_is_not_blocked(self):
        usage = {"1": _u(0, 0), "2": _u(99, 99)}
        accs = _by_id(_build("1", ["2"], usage, thresholds={"1": T}))
        assert accs[2].blocked is False

    def test_active_is_never_blocked(self):
        accs = _by_id(_build("1", ["2"], {"1": _u(99, 99), "2": _u(1, 1)}))
        assert accs[1].blocked is False


class TestActiveAtThreshold:
    def _call(self, usage, plan, **kw):
        return _balance_active_at_threshold(
            "1", usage, {"1": T}, kw.get("weekly"), kw.get("models", ()),
            _plans({"1": plan}),
        )

    def test_five_hour_at_line(self):
        assert self._call(_u(90, 10), "pro") is True

    def test_weekly_at_line(self):
        assert self._call(_u(10, 90), "pro") is True

    def test_weekly_threshold_sets_the_weekly_line(self):
        usage = _u(10, 85)
        assert self._call(usage, "pro", weekly=80.0) is True
        assert self._call(usage, "pro", weekly=None) is False

    def test_below_both(self):
        assert self._call(_u(89, 89), "pro") is False

    def test_scoped_at_line(self):
        scoped = [{"name": "Fable", "pct": 95}]
        assert self._call(_u(10, 10, scoped=scoped), "pro", models=("Fable",)) is True

    def test_ultra_ignores_five_hour(self):
        assert self._call(_u(95, 10), "ultra") is False

    def test_ultra_weekly_counts(self):
        assert self._call(_u(10, 95), "ultra") is True

    def test_max_keeps_five_hour(self):
        assert self._call(_u(95, 10), "max") is True

    def test_sentinel_or_none_is_false(self):
        assert self._call("expired", "pro") is False
        assert self._call(None, "pro") is False


class TestPurity:
    def test_inputs_not_mutated_and_repeatable(self):
        scoped = [{"name": "Fable", "pct": 70, "resets_at": _iso(7777)}]
        usage = {"1": _u(10, 20, 5000, 9000, scoped), "2": "expired"}
        th = {"1": T, "2": T}
        before = copy.deepcopy((usage, th))
        a = _balance_accounts("1", ["2"], usage, th, 80.0, ("Fable",), _plans({}))
        b = _balance_accounts("1", ["2"], usage, th, 80.0, ("Fable",), _plans({}))
        assert a == b
        assert (usage, th) == before
