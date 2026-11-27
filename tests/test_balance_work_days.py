"""The balanced strategy on work time, and the adaptive loss guard
(claude_swap.balance, spec 2026-11-27 work days, WD3 and WD4)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from claude_swap import balance
from claude_swap.balance import Account, Hold, Stay, SwitchTo, Window

TZ = timezone(timedelta(hours=-3))
H = 3600.0
WEEK_S = 168 * H
MON_FRI = frozenset({0, 1, 2, 3, 4})


@pytest.fixture(autouse=True)
def utc_process_zone(monkeypatch):
    """The process zone is UTC, not the -03:00 of ``TZ``: a ``tz`` that
    falls back to the local zone gives other day borders and fails."""
    import time

    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    monkeypatch.setenv("TZ", "UTC0")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(*args) -> float:
    return datetime(*args, tzinfo=TZ).timestamp()


FRI = at(2026, 11, 27, 19, 6)
SAT = at(2026, 11, 28, 15, 0)

# The fleet of Fri 2026-11-27 19:06 -03: (id, plan, weekly percent, reset).
FLEET = (
    (1, "pro", 39.0, at(2026, 12, 1, 4, 0)),
    (2, "pro", 26.0, at(2026, 12, 2, 1, 59)),
    (3, "pro", 77.0, at(2026, 11, 28, 10, 0)),
    (4, "pro", 64.0, at(2026, 11, 29, 5, 59)),
    (5, "max", 52.0, at(2026, 11, 30, 5, 0)),
    (6, "pro", 23.0, at(2026, 12, 2, 13, 59)),
)


def fleet(now: float, overrides=None) -> list[Account]:
    overrides = overrides or {}
    out = []
    for id_, plan, p7, reset in FLEET:
        p7, reset = overrides.get(id_, (p7, reset))
        out.append(Account(id_, plan, Window(0.0, now + H), Window(p7, reset)))
    return out


def scores(accounts, now, **kw):
    kw.setdefault("threshold", 99.0)
    return balance._ranked(accounts, now, weekly_threshold=None,
                           candidate_margin=0.0, work_days=MON_FRI, tz=TZ, **kw)


# --- S1: all days keeps today's floats ---------------------------------------


def _old_elapsed(account: Account, now: float) -> float:
    reset = account.seven_day.resets_at
    if reset <= now:
        return ((now - reset) % WEEK_S) / WEEK_S
    return max(0.0, min(1.0, 1.0 - (reset - now) / WEEK_S))


def test_all_days_share_equals_wall_clock():
    rng = random.Random(11)
    for _ in range(3000):
        now = rng.uniform(1.6e9, 1.9e9)
        reset = now + rng.uniform(-400 * H, 200 * H)
        a = Account(1, "pro", Window(10.0, now + H), Window(rng.uniform(0, 100), reset))
        old = _old_elapsed(a, now)
        for kw in ({}, {"work_days": None, "tz": TZ}, {"work_days": None}):
            assert balance._weekly(a, now, **kw)[1] == old
    # The engine fleets of the existing tests: the same f and the same ranks.
    accounts = fleet(FRI)
    assert balance.line_height(accounts, FRI) == balance.line_height(
        accounts, FRI, work_days=None, tz=TZ)


# --- S4 and S5: the WD0 fleet ------------------------------------------------


def test_friday_ranks_expiring_accounts_first():
    accounts = fleet(FRI)
    ranked = scores(accounts, FRI)
    assert [s.id for s in ranked[:3]] == [3, 4, 5]
    assert all(s.guard and s.reason == "loss guard" for s in ranked[:3])
    assert not any(s.guard for s in ranked[3:])
    # The others by deficit, on the work share and its line height
    # (f = 0.6545): #2 has 0.6545 * 54.26 - 26 = 9.52.
    assert [s.id for s in ranked[3:]] == [2, 1, 6]
    assert ranked[3].score == pytest.approx(9.52, abs=0.01)
    assert balance.rank(accounts, FRI, threshold=99.0, work_days=MON_FRI,
                        tz=TZ)[:3] == [3, 4, 5]
    # The work share of #3: 115.1 of its 120 work hours have passed.
    assert balance._weekly(accounts[2], FRI, MON_FRI, TZ)[1] == pytest.approx(
        115.1 / 120)


def test_rest_time_use_spends_expiring_quota():
    # Saturday 15:00: the week of #3 reset at 10:00 (0% of a new week).
    accounts = fleet(SAT)
    ranked = scores(accounts, SAT)
    assert [s.id for s in ranked[:2]] == [4, 5]
    assert ranked[0].guard and ranked[1].guard
    # #4 has no work time left before its reset on Sunday 05:59.
    assert balance.remaining_work_hours(accounts[3], SAT, MON_FRI, TZ) == 0.0
    assert balance._weekly(accounts[3], SAT, MON_FRI, TZ)[1] == 1.0
    # #3 starts a new week: no work time of it has passed, no guard.
    by_id = {s.id: s for s in ranked}
    assert balance._weekly(accounts[2], SAT, MON_FRI, TZ)[:2] == (0.0, 0.0)
    assert not by_id[3].guard


# --- the need of an account and the guard margin ----------------------------


def test_guard_needs_and_margin():
    assert balance.RATE_FLOOR == 0.5
    assert balance.GUARD_MARGIN == 1.25
    assert balance.GUARD_CAP_WORK_H == 24.0
    assert not hasattr(balance, "GUARD_F")
    assert not hasattr(balance, "GUARD_HOURS")

    # need = c * (line - percent) / 100 / R_w work hours. Pro at 50% of a
    # 90% line at R_w = 0.04 units per hour: 10 h; the guard holds at
    # 1.25 * 10 = 12.5 h of remaining work time.
    def guard(p7=50.0, hours=12.5, plan="pro", rate=0.04, **kw):
        a = Account(1, plan, Window(0.0, FRI + H), Window(p7, FRI + hours * H))
        return balance.score_account(a, FRI, work_rate=rate, **kw).guard

    assert guard(hours=12.5)
    assert not guard(hours=12.6)
    # The need runs to the real weekly line, not to the candidate margin
    # (7.5 h, so 9.4 h).
    assert guard(hours=11.0, candidate_margin=10.0)
    # The weekly line is the weekly threshold when it is set.
    assert guard(hours=11.0, threshold=60.0, weekly_threshold=90.0)
    assert not guard(hours=11.0, threshold=90.0, weekly_threshold=70.0)
    # No fleet rate (a lone score, or no account counts): no guard.
    assert not guard(rate=None)
    assert not guard(rate=0.0)
    # The rate floor: a slow fleet (rho = 0.3) counts at RATE_FLOOR.
    slow = [Account(i, "pro", Window(0.0, FRI + H), Window(p7, FRI + 84 * H))
            for i, p7 in ((1, 10.0), (2, 20.0))]
    assert balance.work_rate(slow, FRI) == pytest.approx(0.5 * 2 / 168)
    # Remaining work time: with mon-fri, a reset on Sunday morning has only
    # the 4.9 h to Friday midnight left, so the need decides at that time.
    a = Account(1, "pro", Window(0.0, FRI + H), Window(95.0, at(2026, 11, 29, 5, 59)))
    # need = 0.04 / 0.01 = 4 h; 1.25 * 4 = 5 >= 4.9 h.
    assert balance.score_account(a, FRI, threshold=99.0, work_rate=0.01,
                                 work_days=MON_FRI, tz=TZ).guard
    # Wall clock: 34.9 h left, no guard.
    assert not balance.score_account(a, FRI, threshold=99.0, work_rate=0.01).guard
    # No weekly window or no reset time: no remaining work time.
    assert balance.remaining_work_hours(Account(1, "pro", None, None), FRI) is None
    no_reset = Account(1, "pro", Window(0.0, FRI + H), Window(10.0, None))
    assert balance.remaining_work_hours(no_reset, FRI) is None


def test_guard_cap():
    # A Max account needs five times longer than a Pro (50 h here, so
    # 62.5 h), but the guard acts only below 24 remaining work hours.
    def guard(hours, plan="max"):
        a = Account(1, plan, Window(0.0, FRI + H), Window(50.0, FRI + hours * H))
        return balance.score_account(a, FRI, work_rate=0.04).guard

    assert guard(23.9)
    # Exactly 24.0 h is not below the cap.
    assert not guard(24.0)
    assert not guard(24.5)
    assert not guard(23.9, plan="pro")
    # Early in the week, at a slow pace, a Max with staggered resets is not
    # a guard account: the strategy keeps spreading use.
    week = [
        Account(1, "max", Window(0.0, FRI + H), Window(5.0, FRI + 150 * H)),
        Account(2, "pro", Window(0.0, FRI + H), Window(10.0, FRI + 100 * H)),
        Account(3, "pro", Window(0.0, FRI + H), Window(20.0, FRI + 60 * H)),
    ]
    assert not any(s.guard for s in balance._ranked(
        week, FRI, threshold=90.0, weekly_threshold=None, candidate_margin=0.0))


def test_guard_ties_go_by_deficit():
    # Two guard accounts with the same next reset: the larger deficit first.
    sat10 = at(2026, 11, 28, 10, 0)
    pair = [*fleet(FRI, {3: (60.0, sat10)}),
            Account(7, "pro", Window(0.0, FRI + H), Window(50.0, sat10))]
    ranked = scores(pair, FRI)
    assert [(s.id, s.guard) for s in ranked[:2]] == [(7, True), (3, True)]
    assert ranked[0].score > ranked[1].score
    assert balance.rank(list(reversed(pair)), FRI, threshold=99.0,
                        work_days=MON_FRI, tz=TZ)[:2] == [7, 3]


def test_fleet_work_rate():
    # R_w = max(rho, RATE_FLOOR) * sum(c) / W_h. Two Pro accounts half way
    # through their weeks at 20% and 40%: rho = 0.6 / 1.0.
    def acc(id_, p7, hours, plan="pro"):
        return Account(id_, plan, Window(0.0, FRI + H), Window(p7, FRI + hours * H))

    fast = [acc(1, 20.0, 84), acc(2, 40.0, 84)]
    assert balance.work_rate(fast, FRI) == pytest.approx(0.6 * 2 / 168)
    # The capacity counts: a Max adds 5 units (rho = 2.6 / 3.5).
    with_max = [*fast, acc(3, 40.0, 84, plan="max")]
    assert balance.work_rate(with_max, FRI) == pytest.approx(2.6 / 3.5 * 7 / 168)
    # rho = 0.3 is below the floor: R_w uses 0.5.
    slow = [acc(1, 10.0, 84), acc(2, 20.0, 84)]
    assert balance.work_rate(slow, FRI) == pytest.approx(0.5 * 2 / 168)
    # A blocked account and one with no reset time do not count.
    blocked = Account(3, "max", Window(0.0, FRI + H), Window(99.0, FRI + 84 * H),
                      blocked=True)
    idle = Account(4, "max", Window(0.0, FRI + H), Window(99.0, None))
    assert balance.work_rate([*fast, blocked, idle], FRI) == pytest.approx(0.6 * 2 / 168)
    # Work hours of the week: 120 for mon-fri.
    assert balance.work_rate(fast, FRI, work_days=MON_FRI, tz=TZ) > 0
    rho = balance.fleet_pace(fast, FRI, work_days=MON_FRI, tz=TZ)
    assert balance.work_rate(fast, FRI, work_days=MON_FRI, tz=TZ) == pytest.approx(
        max(rho, 0.5) * 2 / 120)
    # Every week just started (sum of c * e below F_FLOOR): the floor.
    fresh = [acc(1, 60.0, 168 - 0.01)]
    assert balance.fleet_pace(fresh, FRI) is None
    assert balance.work_rate(fresh, FRI) == pytest.approx(0.5 / 168)
    # No account counts: no rate.
    assert balance.work_rate([idle], FRI) == 0.0


# --- decide: two guard accounts ------------------------------------------------


def _decide(accounts, active, now=FRI, last_proactive=None):
    return balance.decide(accounts, active, now, last_proactive, None, False,
                          threshold=99.0, work_days=MON_FRI, tz=TZ)


def test_guard_active_moves_to_a_guard_with_an_earlier_reset():
    accounts = fleet(FRI)
    # #5 (guard, reset Monday) leaves for #3 (guard, reset Saturday).
    assert _decide(accounts, 5) == SwitchTo(3, "proactive")
    assert _decide(accounts, 4) == SwitchTo(3, "proactive")
    assert _decide(accounts, 5, last_proactive=FRI - 10) == Hold("cooldown")
    # The earliest reset stays: the other guard accounts reset later.
    assert _decide(accounts, 3) == Stay()
    # A guard candidate with the same reset does not move a guard account.
    sat10 = at(2026, 11, 28, 10, 0)
    twin = Account(7, "pro", Window(0.0, FRI + H), Window(60.0, sat10))
    pair = [*fleet(FRI, {3: (60.0, sat10)}), twin]
    assert [(s.id, s.guard) for s in scores(pair, FRI)[:2]] == [(3, True), (7, True)]
    assert _decide(pair, 3) == Stay()
    assert _decide(pair, 7) == Stay()
    # A non-guard active account moves to the first guard account.
    assert _decide(accounts, 2) == SwitchTo(3, "proactive")


def test_decide_reads_the_work_rate_and_the_work_line():
    r3, r4 = at(2026, 11, 28, 10, 0), at(2026, 11, 29, 5, 59)
    # #3 is over its line. #4 at 79% needs less than 4.9 / 1.25 h at the
    # work rate (120 work hours a week), so it is no guard account; #5 is.
    accounts = fleet(FRI, {3: (99.5, r3), 4: (79.0, r4)})
    by_id = {s.id: s for s in scores(accounts, FRI)}
    assert not by_id[4].guard and by_id[5].guard
    assert _decide(accounts, 4) == SwitchTo(5, "proactive")
    # At the wall-clock rate (168 hours a week) #4 would be a guard account.
    assert balance.score_account(accounts[3], FRI, threshold=99.0,
                                 work_rate=balance.work_rate(accounts, FRI),
                                 work_days=MON_FRI, tz=TZ).guard
    # No guard account: the deficits on the work line (f = 0.566) are 2.1,
    # 4.7 and 2.0, inside DEFICIT_MARGIN_PTS. The wall-clock f (0.792)
    # would give #1 a lead of 6.4 points over #6.
    calm = [a for a in fleet(FRI) if a.id in (1, 2, 6)]
    assert _decide(calm, 6) == Stay()


def test_engine_numbers_pass_through_decide_and_rank():
    """decide and rank take the same work days as the ranking helpers."""
    accounts = fleet(FRI)
    assert balance.rank(accounts, FRI, threshold=99.0, work_days=MON_FRI,
                        tz=TZ) == [3, 4, 5, 2, 1, 6]
    # Wall clock: #3 has 14.9 h left, more than 1.25 times its need, so no
    # account is a guard account, and the deficits lead.
    assert not any(s.guard for s in balance._ranked(
        accounts, FRI, threshold=99.0, weekly_threshold=None, candidate_margin=0.0))
    assert balance.rank(accounts, FRI, threshold=99.0) == [2, 1, 5, 4, 6, 3]
    # decide from the active #6: on work days to the first guard account;
    # on the wall clock the lead of #2 is inside DEFICIT_MARGIN_PTS.
    assert _decide(accounts, 6) == SwitchTo(3, "proactive")
    assert balance.decide(accounts, 6, FRI, None, None, False,
                          threshold=99.0) == Stay()
