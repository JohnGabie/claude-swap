"""Scenarios for the `balanced` ranking and decision (claude_swap.balance).

Time is a fake clock: every timestamp is NOW plus a number of hours.
"""

import math
import random

import pytest

from claude_swap import balance
from claude_swap.balance import (
    BALANCED_COOLDOWN_S,
    DEFAULT_CANDIDATE_MARGIN,
    DEFAULT_THRESHOLD,
    MIN_HEADROOM_5H,
    MIN_HEADROOM_7D,
    Account,
    Hold,
    Stay,
    SwitchTo,
    Window,
    decide,
    rank,
    score_account,
)

NOW = 1_000_000.0
H = 3600.0


def acct(id_, plan="pro", p5=0.0, r5=3, p7=0.0, r7=None, blocked=False):
    """Account with times in hours from NOW; r7=None means no running week."""
    return Account(
        id=id_,
        plan=plan,
        five_hour=Window(p5, NOW + r5 * H if r5 is not None else None),
        seven_day=Window(p7, NOW + r7 * H if r7 is not None else None),
        blocked=blocked,
    )


def run(accounts, active, *, now=NOW, last_proactive=None, last_manual=None,
        at_threshold=False):
    return decide(
        accounts,
        active,
        now,
        last_proactive_ts=last_proactive,
        last_manual_ts=last_manual,
        active_at_threshold=at_threshold,
    )


# --- constants the user decided ------------------------------------------


def test_decided_constants():
    assert MIN_HEADROOM_5H == 10.0
    assert MIN_HEADROOM_7D == 3.0
    assert BALANCED_COOLDOWN_S == 900.0
    # Changed in B1 (was NEAR_RESET_S == 6 h): the scaled-line constants.
    assert balance.PLAN_CAPACITY == {"pro": 1.0, "max": 5.0, "ultra": 10.0}
    assert balance.DEFAULT_CAPACITY == 1.0
    assert (balance.F_BOOST, balance.F_MIN, balance.F_FLOOR) == (1.1, 0.1, 0.01)
    assert (balance.GUARD_F, balance.GUARD_HOURS) == (0.9, 12.0)
    assert balance.DEFICIT_MARGIN_PTS == 4.0


# --- the height of the line f (B1) -----------------------------------------
# f = clip(F_BOOST * sum(c_i * u_i) / sum(c_i * e_i), F_MIN, 1.0)


def _fleet_1_5_10():
    # Capacity 1, 5 and 10; 0.5, 0.25 and 0.75 of each week has elapsed.
    return [
        acct(1, "pro", p7=20, r7=84),
        acct(2, "max", p7=40, r7=126),
        acct(3, "ultra", p7=10, r7=42),
    ]


def test_f_formula_on_a_hand_built_fleet():
    used = 1 * 0.20 + 5 * 0.40 + 10 * 0.10  # 3.2 capacity units
    line = 1 * 0.50 + 5 * 0.25 + 10 * 0.75  # 9.25 capacity units
    f = balance.line_height(_fleet_1_5_10(), NOW)
    assert f == pytest.approx(1.1 * used / line)
    assert f == pytest.approx(0.38054, abs=1e-5)


def test_f_is_clipped_into_f_min_to_one():
    heavy = [acct(1, p7=80, r7=84), acct(2, p7=70, r7=84)]  # 1.1 * 1.5 / 1.0
    assert balance.line_height(heavy, NOW) == 1.0
    unused = [acct(1, p7=0, r7=84), acct(2, p7=1, r7=84)]  # 1.1 * 0.01 / 1.0
    assert balance.line_height(unused, NOW) == 0.1
    assert balance.line_height([], NOW) == 0.1


def test_f_is_f_min_when_the_fleet_week_has_just_started():
    # Elapsed 0.5 h of 168 h: the denominator 0.00298 is below F_FLOOR.
    just_reset = [acct(1, p7=50, r7=167.5)]
    assert balance.line_height(just_reset, NOW) == 0.1
    # A reset more than one week away clips the elapsed share to 0.
    assert balance.line_height([acct(1, p7=50, r7=400)], NOW) == 0.1
    # Elapsed 0.02 of the week is above the floor: 1.1 * 0.5 / 0.02 clips to 1.
    assert balance.line_height([acct(1, p7=50, r7=168 * 0.98)], NOW) == 1.0


def test_f_ignores_blocked_and_unmeasured_accounts():
    fleet = _fleet_1_5_10()
    f = balance.line_height(fleet, NOW)
    extra = [
        acct(7, "ultra", p7=95, r7=10, blocked=True),
        Account(8, "max", None, Window(95, NOW + 10 * H)),
        Account(9, "max", Window(0, NOW + H), Window(math.nan, NOW + 10 * H)),
    ]
    assert balance.line_height([*fleet, *extra], NOW) == f


def test_same_plan_word_on_every_account_gives_equal_weights():
    def fleet(*plans):
        return [acct(1, plans[0], p7=20, r7=84), acct(2, plans[1], p7=40, r7=126),
                acct(3, plans[2], p7=10, r7=42)]

    same = [fleet(word, word, word) for word in ("pro", "max", "ultra", "team")]
    heights = [balance.line_height(f, NOW) for f in same]
    # Equal weights: (0.2 + 0.4 + 0.1) / (0.5 + 0.25 + 0.75).
    assert heights == pytest.approx([1.1 * 0.7 / 1.5] * 4)
    assert len({tuple(rank(f, NOW)) for f in same}) == 1
    # Different words give different weights.
    mixed = balance.line_height(fleet("pro", "max", "ultra"), NOW)
    assert mixed != pytest.approx(heights[0])


def test_unknown_plan_word_has_the_capacity_of_pro():
    # Changed in B1 (was "unknown acts like Pro" in the class rule).
    for word in ("unknown", "some-new-tier", "", "PRO "):
        assert balance.plan_capacity(word) == 1.0, word
    assert balance.plan_capacity(None) == 1.0
    assert balance.plan_capacity("Max") == 5.0
    assert balance.plan_capacity("ultra") == 10.0


# --- deficit order (B1) -----------------------------------------------------
# deficit_i = f * e_i * 100 - weekly percent; the largest deficit goes first.


def test_deficit_order_with_capacities_1_5_10_and_staggered_resets():
    fleet = _fleet_1_5_10()
    f = balance.line_height(fleet, NOW)
    got = [balance.deficit(a, NOW, f) for a in fleet]
    assert got == pytest.approx([f * 50 - 20, f * 25 - 40, f * 75 - 10])
    assert got == pytest.approx([-0.97, -30.49, 18.54], abs=0.01)
    assert rank(fleet, NOW) == [3, 1, 2]
    assert rank(list(reversed(fleet)), NOW) == [3, 1, 2]


def test_capacity_and_f_decide_the_order():
    # f = 1.1 * (0.335 + 5 * 0.10) / (0.75 + 5 * 0.25) = 0.45925:
    # the Pro has 0.45925 * 75 - 33.5 = 0.94, the Max 0.45925 * 25 - 10 = 1.48.
    # With equal capacities (f = 0.4785) or with f = 1 the Pro is first.
    pro = acct(1, "pro", p7=33.5, r7=42)
    mx = acct(2, "max", p7=10, r7=126)
    f = balance.line_height([pro, mx], NOW)
    assert f == pytest.approx(0.45925)
    assert balance.deficit(pro, NOW, f) == pytest.approx(0.944, abs=1e-3)
    assert balance.deficit(mx, NOW, f) == pytest.approx(1.481, abs=1e-3)
    assert rank([pro, mx], NOW) == [2, 1]


def test_light_fleet_use_grows_evenly_over_three_pro_and_one_max():
    # 200 decisions, 30 minutes apart, a constant 0.012 capacity units each
    # (2.4 of the 8 units of the fleet: 30 percent), aligned weekly resets.
    capacity = {1: 1.0, 2: 1.0, 3: 1.0, 4: 5.0}
    plan = {1: "pro", 2: "pro", 3: "pro", 4: "max"}
    pct = dict.fromkeys(capacity, 0.0)
    reset = NOW + 150 * H
    now, active, last, switches = NOW, 1, None, 0
    for _ in range(200):
        fleet = [Account(i, plan[i], Window(0.0, now + H), Window(pct[i], reset))
                 for i in sorted(capacity)]
        got = decide(fleet, active, now, last_proactive_ts=last,
                     last_manual_ts=None, active_at_threshold=False)
        if isinstance(got, SwitchTo):
            assert got.reason == "proactive"
            active, last, switches = got.account_id, now, switches + 1
        pct[active] += 0.012 / capacity[active] * 100.0
        now += 1800.0
    assert max(pct.values()) - min(pct.values()) < 10.0, pct
    assert min(pct.values()) > 20.0, pct
    assert switches < 100  # the margin and the cooldown keep the choice firm


def test_expiring_quota_beats_a_fresh_week():
    # Light user, 4 Pro. Account 2 resets tomorrow with 80% unused.
    accounts = [
        acct(1, p7=5, r7=144),
        acct(2, p7=20, r7=24),
        acct(3, p7=0, r7=None),
        acct(4, p7=10, r7=100),
    ]
    assert rank(accounts, NOW)[0] == 2
    assert run(accounts, 1) == SwitchTo(2, "proactive")


def test_within_pro_lower_use_and_nearer_reset_go_first():
    near_low = acct(1, p7=20, r7=24)
    far_high = acct(2, p7=50, r7=100)
    assert rank([far_high, near_low], NOW) == [1, 2]


def test_expired_weekly_reading_counts_as_zero_used():
    # Changed in B1b (was e = 1, deficit f * 100, rank [1, 2]): the week that
    # the 80% reading describes has ended, and a new week started 60 seconds
    # ago: u = 0 and e = 60 s / 168 h.
    stale = Account(1, "pro", Window(0, NOW + H), Window(80, NOW - 60))
    live = acct(2, p7=30, r7=84)
    e_stale = 60.0 / (168 * H)
    f = balance.line_height([stale, live], NOW)
    assert f == pytest.approx(1.1 * (0.0 + 0.30) / (e_stale + 0.5))
    assert balance.deficit(stale, NOW, f) == pytest.approx(f * e_stale * 100.0)
    assert balance.deficit(live, NOW, f) == pytest.approx(f * 50.0 - 30.0)
    assert rank([live, stale], NOW) == [2, 1]


# --- a weekly reset that has passed starts a new week (B1b) ------------------
# u_i = 0 and e_i = ((now - old reset) mod 168 h) / 168 h. The loss guard uses
# the first reset after now: old reset + k * 168 h.


def _passed(id_, hours_ago, p7=80.0, plan="pro"):
    """Account whose weekly reset passed ``hours_ago`` hours before NOW."""
    return Account(id_, plan, Window(0, NOW + H), Window(p7, NOW - hours_ago * H))


def _elapsed(account, now=NOW):
    # With f = 1 and u = 0 the deficit is e * 100.
    return balance.deficit(account, now, 1.0) / 100.0


def test_reset_passed_one_hour_ago_starts_a_new_week():
    fresh = _passed(1, 1)
    behind = acct(2, p7=10, r7=84)
    ahead = acct(3, p7=60, r7=84)
    fleet = [fresh, behind, ahead]
    assert _elapsed(fresh) == pytest.approx(1 / 168)
    f = balance.line_height(fleet, NOW)
    assert f == pytest.approx(1.1 * (0.0 + 0.10 + 0.60) / (1 / 168 + 0.5 + 0.5))
    # The deficit of the new week is near 0 (0.46); the account behind its
    # line has 28.3 and goes first.
    assert balance.deficit(fresh, NOW, f) == pytest.approx(f * 100 / 168)
    assert 0 < balance.deficit(fresh, NOW, f) < 1.0
    assert balance.deficit(behind, NOW, f) == pytest.approx(f * 50 - 10)
    assert rank(fleet, NOW) == [2, 1, 3]
    assert rank(list(reversed(fleet)), NOW) == [2, 1, 3]
    assert run(fleet, 2) == Stay()
    assert run(fleet, 1) == SwitchTo(2, "proactive")


def test_reset_passed_eight_days_ago_counts_modulo_one_week():
    old = _passed(1, 8 * 24)
    assert _elapsed(old) == pytest.approx(24 / 168)
    live = acct(2, p7=30, r7=84)
    f = balance.line_height([old, live], NOW)
    assert f == pytest.approx(1.1 * 0.30 / (24 / 168 + 0.5))
    # Two whole weeks and 5 hours.
    assert _elapsed(_passed(3, 2 * 168 + 5)) == pytest.approx(5 / 168)


def test_reset_exactly_at_now_has_zero_elapsed():
    at_now = _passed(1, 0)
    assert _elapsed(at_now) == 0.0
    s = score_account(at_now, NOW, f=1.0)
    assert s.usable and s.score == 0.0 and s.headroom_7d == 100.0
    # The next reset is one whole week away: no loss guard.
    assert not s.guard
    # One whole week later the same reading is at e = 0 again.
    assert _elapsed(_passed(2, 168)) == 0.0


def _passed_guard_fleet(hours_ago=160, third_p7=80.0):
    # hours_ago=160: e = 160/168; f = 1.1 * 1.65 / 1.857 = 0.977 (guard on).
    # third_p7=20: f = 1.1 * 1.05 / 1.857 = 0.622 (guard off).
    return [_passed(1, hours_ago, p7=40.0), acct(2, p7=85, r7=100),
            acct(3, p7=third_p7, r7=84)]


def test_loss_guard_uses_the_next_real_reset_of_a_passed_reading():
    fleet = _passed_guard_fleet()
    f = balance.line_height(fleet, NOW)
    assert f >= 0.9
    # The old reset passed 160 hours ago: the next one is 8 hours away.
    s = score_account(fleet[0], NOW, f=f)
    assert s.usable and s.headroom_7d == 100.0
    assert s.guard and s.reason == "loss guard"
    assert s.reset_7d == pytest.approx(NOW + 8 * H)
    assert rank(fleet, NOW)[0] == 1
    assert run(fleet, 1) == Stay()
    # A reading older than one week: 160 h + 168 h ago, the same next reset.
    older = score_account(_passed(1, 160 + 168, p7=40.0), NOW, f=f)
    assert older.guard and older.reset_7d == pytest.approx(NOW + 8 * H)
    # Two guard accounts: the earlier next reset goes first.
    # (f = 1.1 * 2.5 / 2.797 = 0.983, and 0.975 with the reset in 6 hours.)
    two = [*fleet, acct(4, p7=85, r7=10)]
    assert balance.line_height(two, NOW) >= 0.9
    assert rank(two, NOW)[:2] == [1, 4]
    sooner = [*fleet, acct(4, p7=85, r7=6)]
    assert balance.line_height(sooner, NOW) >= 0.9
    assert rank(sooner, NOW)[:2] == [4, 1]


def test_loss_guard_of_a_passed_reading_keeps_its_limits():
    # 13 hours before the next reset: outside GUARD_HOURS.
    far = _passed_guard_fleet(hours_ago=155)
    f = balance.line_height(far, NOW)
    assert f >= 0.9
    assert not score_account(far[0], NOW, f=f).guard
    # A low f: no guard.
    low = _passed_guard_fleet(third_p7=20.0)
    f = balance.line_height(low, NOW)
    assert f < 0.9
    s = score_account(low[0], NOW, f=f)
    assert s.usable and not s.guard and s.reason == "deficit"


def test_account_with_no_weekly_reset_time():
    # No reset time: e = 0 and u = 0 in the fleet sums; deficit = -percent.
    idle = acct(1, p7=7, r7=None)
    fleet = _fleet_1_5_10()
    f = balance.line_height(fleet, NOW)
    assert balance.line_height([idle, *fleet], NOW) == f
    assert balance.deficit(idle, NOW, f) == -7.0
    assert balance.deficit(idle, NOW, 1.0) == -7.0
    # Deficits: account 3 has 18.54, account 1 has -0.97, the idle one -7.
    assert rank([acct(4, p7=7, r7=None), *fleet], NOW) == [3, 1, 4, 2]
    # A fleet with no reset time at all: f is F_MIN, less used first.
    none = [acct(1, p7=30, r7=None), acct(2, p7=10, r7=None)]
    assert balance.line_height(none, NOW) == 0.1
    assert rank(none, NOW) == [2, 1]


def test_ties_go_to_the_less_used_account_then_to_the_lower_id():
    # Changed in B1 (was a tie of urgency inside the Pro class).
    # f is F_MIN (1.1 * 0.05 / 1.0 is below it). Account 5 sits on its line:
    # 0.1 * 0.5 * 100 - 5 = 0, the deficit of the unused account 9.
    on_line = acct(5, p7=5, r7=84)
    unused = acct(9, p7=0, r7=None)
    ahead = acct(2, p7=0, r7=84)
    fleet = [on_line, unused, ahead]
    f = balance.line_height(fleet, NOW)
    assert f == 0.1
    assert balance.deficit(on_line, NOW, f) == balance.deficit(unused, NOW, f) == 0.0
    assert rank(fleet, NOW) == [2, 9, 5]
    # Equal deficit and equal percent: the lower id, in any input order.
    twins = [acct(7, p7=10, r7=None), acct(3, p7=10, r7=None)]
    assert rank(twins, NOW) == [3, 7]
    assert rank(list(reversed(twins)), NOW) == [3, 7]


# --- the loss guard (B1) ----------------------------------------------------
# With f >= GUARD_F, a usable account whose weekly reset is within
# GUARD_HOURS goes first, the earliest reset first.


def _guard_fleet(third_p7=80.0, guard_r7=6):
    # third_p7=80: f = 1.1 * 1.65 / 1.869 = 0.971 (guard on); deficits 8.6
    # for account 1, 39.3 for account 2, -31.4 for account 3.
    # third_p7=60: f = 1.1 * 1.45 / 1.869 = 0.853 (guard off); deficits -2.7,
    # 34.5 and -17.3.
    return [acct(1, p7=85, r7=guard_r7), acct(2, p7=0, r7=100),
            acct(3, p7=third_p7, r7=84)]


def test_loss_guard_on_puts_the_expiring_account_first():
    fleet = _guard_fleet()
    f = balance.line_height(fleet, NOW)
    assert f >= 0.9
    assert balance.deficit(fleet[0], NOW, f) < balance.deficit(fleet[1], NOW, f)
    assert rank(fleet, NOW) == [1, 2, 3]
    # A non-guard active account moves to the guard account: its deficit is
    # the lower one, so only the guard explains the move.
    assert run(fleet, 2) == SwitchTo(1, "proactive")
    assert run(fleet, 3) == SwitchTo(1, "proactive")
    assert run(fleet, 2, last_proactive=NOW - 10) == Hold("cooldown")
    # The guard account itself stays, also against a far larger deficit.
    assert run(fleet, 1) == Stay()


def test_loss_guard_off_below_guard_f():
    fleet = _guard_fleet(third_p7=60.0)
    assert 0.8 < balance.line_height(fleet, NOW) < 0.9
    assert rank(fleet, NOW) == [2, 1, 3]
    assert run(fleet, 2) == Stay()
    # No guard: the account near its reset leaves for the larger deficit.
    assert run(fleet, 1) == SwitchTo(2, "proactive")


def test_loss_guard_window_is_twelve_hours():
    assert rank(_guard_fleet(guard_r7=12), NOW) == [1, 2, 3]
    assert balance.line_height(_guard_fleet(guard_r7=12.5), NOW) >= 0.9
    assert rank(_guard_fleet(guard_r7=12.5), NOW) == [2, 1, 3]


def test_loss_guard_orders_by_the_earliest_reset():
    # Account 4 resets first and has the smaller deficit of the two guards.
    fleet = [*_guard_fleet(), acct(4, p7=88, r7=3)]
    f = balance.line_height(fleet, NOW)
    assert f >= 0.9
    assert balance.deficit(fleet[3], NOW, f) < balance.deficit(fleet[0], NOW, f)
    assert rank(fleet, NOW) == [4, 1, 2, 3]
    # One guard account does not leave for another guard account.
    assert run(fleet, 1) == Stay()
    assert run(fleet, 4) == Stay()


def test_loss_guard_needs_a_usable_account():
    # The expiring account is over its weekly line: no guard, not a candidate.
    fleet = [acct(1, p7=92, r7=6), acct(2, p7=0, r7=100), acct(3, p7=80, r7=84)]
    assert balance.line_height(fleet, NOW) >= 0.9
    assert rank(fleet, NOW) == [2, 3]
    assert run(fleet, 2) == Stay()


# --- gates and the 5-hour discount ----------------------------------------


def test_five_hour_gate_at_ten_points():
    # Changed in S1f: the default threshold (90) is stricter than the gate,
    # so the gate is tested with the highest threshold the settings allow.
    line = {"threshold": 99.9}
    assert score_account(acct(1, p5=90, p7=10, r7=24), NOW, **line).usable
    gated = score_account(acct(2, p5=90.5, r5=1, p7=10, r7=24), NOW, **line)
    assert not gated.usable and gated.reason == "5h spent"
    assert gated.blocked_until == NOW + 1 * H
    assert rank([acct(2, p5=90.5, p7=10, r7=24), acct(3, p7=60, r7=100)], NOW) == [3]


def test_weekly_gate_at_three_points():
    # Changed in S1f: tested with the highest weekly threshold the settings
    # allow, because the default threshold (90) is stricter than the gate.
    line = {"weekly_threshold": 99.9}
    assert score_account(acct(1, p7=97, r7=30), NOW, **line).usable
    gated = score_account(acct(2, p7=97.5, r7=30), NOW, **line)
    assert not gated.usable and gated.reason == "weekly spent"
    assert gated.blocked_until == NOW + 30 * H


def test_five_hour_use_below_the_gate_does_not_change_the_order():
    # Changed in B1 (was "a partly spent 5-hour window lowers the score"):
    # the deficit reads the weekly window only.
    fresh = acct(1, p5=0, p7=20, r7=48)
    tired = acct(2, p5=70, p7=20, r7=48)
    assert score_account(tired, NOW).score == score_account(fresh, NOW).score
    assert rank([tired, fresh], NOW) == [1, 2]


def test_ultra_has_no_five_hour_gate():
    ultra = acct(1, "ultra", p5=100, p7=10, r7=48)
    assert score_account(ultra, NOW).usable
    assert rank([ultra], NOW) == [1]
    mx = acct(2, "max", p5=100, p7=10, r7=48)
    assert not score_account(mx, NOW).usable


def test_rolled_over_window_counts_as_empty():
    # The 5-hour reset already passed: the 95% reading is old.
    stale5 = Account(1, "pro", Window(95, NOW - 60), Window(10, NOW + 48 * H))
    assert score_account(stale5, NOW).usable
    # The weekly reset passed: 99% becomes 0% used.
    # Changed in B1 (was reason == "idle reserve"): the reason words changed.
    stale7 = Account(2, "pro", Window(0, NOW + H), Window(99, NOW - 60))
    s = score_account(stale7, NOW)
    assert s.usable and s.headroom_7d == 100.0


def test_unmeasured_account_is_not_usable():
    s = score_account(Account(1, "pro", None, Window(10, NOW + H)), NOW)
    assert not s.usable and s.reason == "unmeasured"


def test_blocked_account_is_excluded():
    a = acct(1, p7=0, r7=10, blocked=True)
    b = acct(2, p7=50, r7=100)
    assert not score_account(a, NOW).usable
    assert rank([a, b], NOW) == [2]


# --- ranking output -------------------------------------------------------


def test_ranking_four_pro_one_max():
    # Changed in B1 (was [1, 4, 2, 3, 10], Pro first). f = 1.1 * 1.0 / 3.0476
    # = 0.361, so the guard is off.
    accounts = [
        acct(10, "max", p7=5, r7=120),  # 0.361 * 28.6 - 5 = 5.3
        acct(1, p7=40, r7=12),  # 0.361 * 92.9 - 40 = -6.5
        acct(2, p7=10, r7=130),  # 0.361 * 22.6 - 10 = -1.8
        acct(3, p7=0, r7=None),  # 0
        acct(4, p7=25, r7=90),  # 0.361 * 46.4 - 25 = -8.2
    ]
    assert rank(accounts, NOW) == [10, 3, 2, 1, 4]


def test_ranking_four_pro_one_max_near_reset():
    accounts = [
        acct(10, "max", p7=40, r7=4),
        acct(1, p7=40, r7=12),
        acct(2, p7=10, r7=130),
        acct(3, p5=95, p7=0, r7=None),  # 5-hour gate
        acct(4, p7=25, r7=90, blocked=True),
    ]
    assert rank(accounts, NOW) == [10, 1, 2]


# --- decide: edge cases ---------------------------------------------------


def test_all_blocked_returns_stay():
    accounts = [
        acct(1, p5=96, r5=2, p7=10, r7=50),
        acct(2, p5=97, r5=1, p7=10, r7=50),
        acct(3, p7=10, r7=50, blocked=True),
    ]
    assert rank(accounts, NOW) == []
    assert run(accounts, 1) == Stay()
    assert run(accounts, 1, at_threshold=True) == Stay()


def test_one_account_only_stays():
    only = [acct(1, p7=50, r7=10)]
    assert run(only, 1) == Stay()
    assert run(only, 1, at_threshold=True) == Stay()


def test_active_blocked_moves_to_the_best_account():
    a = acct(1, p5=96, r5=2, p7=10, r7=48)
    b = acct(2, p7=40, r7=100)
    # Changed in S1b (was "proactive"): a blocked or gated active account is a
    # forced switch, so the reason is "threshold".
    assert run([a, b], 1) == SwitchTo(2, "threshold")


# --- decide: unusable active account (S1b) ---------------------------------


def _unusable_active(kind):
    if kind == "blocked":
        a = acct(1, p7=10, r7=48, blocked=True)
    elif kind == "5h":
        a = acct(1, p5=95, r5=2, p7=10, r7=48)
    else:
        a = acct(1, p7=98, r7=48)
    return [a, acct(2, p7=40, r7=100)]


def test_active_blocked_switches_at_once_inside_a_proactive_cooldown():
    got = run(_unusable_active("blocked"), 1, last_proactive=NOW - 10)
    assert got == SwitchTo(2, "threshold")


def test_active_blocked_switches_at_once_inside_a_manual_cooldown():
    got = run(_unusable_active("blocked"), 1, last_manual=NOW - 10)
    assert got == SwitchTo(2, "threshold")


def test_active_below_the_five_hour_gate_switches_at_once():
    got = run(_unusable_active("5h"), 1, last_proactive=NOW - 10,
              last_manual=NOW - 10)
    assert got == SwitchTo(2, "threshold")


def test_active_below_the_weekly_gate_switches_at_once():
    got = run(_unusable_active("7d"), 1, last_proactive=NOW - 10,
              last_manual=NOW - 10)
    assert got == SwitchTo(2, "threshold")


def test_usable_active_inside_the_cooldown_still_holds():
    got = run(_proactive_pair(), 1, last_proactive=NOW - 10)
    assert got == Hold("cooldown")


def test_unusable_active_with_no_usable_candidate_stays():
    accounts = [
        acct(1, p7=10, r7=48, blocked=True),
        acct(2, p5=96, r5=2, p7=10, r7=48),
    ]
    assert run(accounts, 1, last_proactive=NOW - 10) == Stay()


# --- decide: threshold and cooldowns --------------------------------------


def test_threshold_switch_ignores_every_cooldown():
    a = acct(1, p7=94, r7=48)
    b = acct(2, p7=40, r7=100)
    c = acct(3, p7=60, r7=100)
    got = run([a, b, c], 1, now=NOW, last_proactive=NOW - 10,
              last_manual=NOW - 10, at_threshold=True)
    assert got == SwitchTo(2, "threshold")


def test_threshold_switch_skips_the_active_account_even_if_it_ranks_first():
    a = acct(1, p7=80, r7=5)  # still above the gates, ranks first
    b = acct(2, p7=40, r7=100)
    assert rank([a, b], NOW)[0] == 1
    assert run([a, b], 1, at_threshold=True) == SwitchTo(2, "threshold")


def _proactive_pair():
    # Account 2 clearly beats account 1: 80/24 h against 50/100 h.
    return [acct(1, p7=50, r7=100), acct(2, p7=20, r7=24)]


def test_proactive_switch_held_inside_cooldown_after_a_proactive_switch():
    got = run(_proactive_pair(), 1, last_proactive=NOW - 899)
    assert got == Hold("cooldown")


def test_proactive_switch_runs_after_the_cooldown():
    got = run(_proactive_pair(), 1, last_proactive=NOW - 900)
    assert got == SwitchTo(2, "proactive")


def test_proactive_switch_held_inside_cooldown_after_a_manual_switch():
    got = run(_proactive_pair(), 1, last_manual=NOW - 60)
    assert got == Hold("cooldown")


def test_proactive_switch_runs_after_the_manual_cooldown():
    got = run(_proactive_pair(), 1, last_manual=NOW - 901)
    assert got == SwitchTo(2, "proactive")


def test_cooldown_length_is_a_parameter():
    got = decide(_proactive_pair(), 1, NOW, last_proactive_ts=NOW - 100,
                 last_manual_ts=None, active_at_threshold=False, cooldown_s=60.0)
    assert got == SwitchTo(2, "proactive")


def test_no_hold_when_there_is_nothing_to_switch_to():
    # Active is already the best: Stay, not Hold, even inside the cooldown.
    got = run(_proactive_pair(), 2, last_proactive=NOW - 10)
    assert got == Stay()


# --- decide: the deficit margin (B1) ----------------------------------------
# Changed in B1: the margin of DEFICIT_MARGIN_PTS replaces the hysteresis of
# STICKY_RATIO and the class change.


def _margin_pair(best_p7):
    # No weekly reset time: the deficit is minus the weekly percent.
    return [acct(1, p7=20.0, r7=None), acct(2, p7=best_p7, r7=None)]


def test_deficit_margin_of_3_9_points_stays():
    pair = _margin_pair(16.1)
    assert rank(pair, NOW)[0] == 2
    assert run(pair, 1) == Stay()
    assert run(pair, 1, last_proactive=NOW - 10) == Stay()


def test_deficit_margin_of_exactly_4_points_stays():
    assert run(_margin_pair(16.0), 1) == Stay()


def test_deficit_margin_of_4_1_points_switches_when_the_cooldown_allows():
    pair = _margin_pair(15.9)
    assert run(pair, 1) == SwitchTo(2, "proactive")
    assert run(pair, 1, last_proactive=NOW - 900) == SwitchTo(2, "proactive")
    assert run(pair, 1, last_proactive=NOW - 899) == Hold("cooldown")
    assert run(pair, 1, last_manual=NOW - 899) == Hold("cooldown")


def test_deficit_margin_compares_with_the_line_of_each_account():
    # f = 1.1 * 0.6 / 1.0 = 0.66. Account 1: 0.66 * 50 - 30 = 3.0.
    # Account 2: 0.66 * 50 - 30 + delta. Only the difference counts.
    def pair(p7):
        return [acct(1, p7=30.0, r7=84), acct(2, p7=p7, r7=84)]

    assert run(pair(26.5), 1) == Stay()
    assert run(pair(25.5), 1) == SwitchTo(2, "proactive")


# --- decide: unmeasured and unknown active account (S1c) -------------------


def _unmeasured_active():
    # Active account 1 has no 5-hour window; account 2 is a clear winner.
    one = Account(1, "pro", None, Window(10, NOW + 100 * H))
    return [one, acct(2, p7=20, r7=24)]


def test_unmeasured_active_inside_a_cooldown_stays():
    got = run(_unmeasured_active(), 1, last_proactive=NOW - 10,
              last_manual=NOW - 10)
    assert got == Stay()


def test_unmeasured_active_outside_any_cooldown_stays():
    assert run(_unmeasured_active(), 1) == Stay()
    assert run(_unmeasured_active(), 1, last_proactive=NOW - 5000,
               last_manual=NOW - 5000) == Stay()


def test_non_finite_active_stays():
    for bad in (
        Account(1, "pro", Window(math.nan, NOW + H), Window(10, NOW + 100 * H)),
        Account(1, "pro", Window(0, NOW + H), Window(math.inf, NOW + 100 * H)),
        Account(1, "pro", Window(0, NOW + H), Window(10, math.nan)),
    ):
        assert run([bad, acct(2, p7=20, r7=24)], 1) == Stay()


def test_unmeasured_active_at_threshold_switches_inside_a_cooldown():
    got = run(_unmeasured_active(), 1, last_proactive=NOW - 10,
              last_manual=NOW - 10, at_threshold=True)
    assert got == SwitchTo(2, "threshold")


def test_non_finite_active_at_threshold_switches_inside_a_cooldown():
    for bad in (
        Account(1, "pro", Window(math.nan, NOW + H), Window(10, NOW + 100 * H)),
        Account(1, "pro", Window(0, NOW + H), Window(math.inf, NOW + 100 * H)),
        Account(1, "pro", Window(0, NOW + H), Window(10, math.nan)),
    ):
        got = run([bad, acct(2, p7=20, r7=24)], 1, last_proactive=NOW - 10,
                  at_threshold=True)
        assert got == SwitchTo(2, "threshold")


def test_unmeasured_active_at_threshold_with_no_usable_candidate_stays():
    one = Account(1, "pro", None, Window(10, NOW + 100 * H))
    other = acct(2, p7=20, r7=24, blocked=True)
    assert run([one, other], 1, at_threshold=True) == Stay()


def _blocked_unmeasured_active():
    # Blocked wins over unmeasured: the active account is forced out.
    one = Account(1, "pro", None, None, blocked=True)
    return [one, acct(2, p7=20, r7=24)]


def test_blocked_unmeasured_active_switches_inside_a_proactive_cooldown():
    got = run(_blocked_unmeasured_active(), 1, last_proactive=NOW - 10,
              at_threshold=False)
    assert got == SwitchTo(2, "threshold")


def test_blocked_unmeasured_active_switches_inside_a_manual_cooldown():
    got = run(_blocked_unmeasured_active(), 1, last_manual=NOW - 10,
              at_threshold=False)
    assert got == SwitchTo(2, "threshold")


def test_active_id_not_in_the_list_holds_inside_the_cooldown():
    accounts = [acct(2, p7=20, r7=24), acct(3, p7=50, r7=100)]
    assert run(accounts, 99, last_proactive=NOW - 10) == Hold("cooldown")


def test_active_id_not_in_the_list_switches_after_the_cooldown():
    accounts = [acct(2, p7=20, r7=24), acct(3, p7=50, r7=100)]
    assert run(accounts, 99) == SwitchTo(2, "proactive")
    assert run(accounts, 99, last_manual=NOW - 901) == SwitchTo(2, "proactive")


# --- non-finite input is unmeasured (S1c) ----------------------------------


def _bad_accounts():
    good = acct(9, p7=30, r7=50)
    return good, {
        "nan resets_at": Account(1, "pro", Window(0, NOW + H),
                                 Window(10, math.nan)),
        "nan pct": Account(1, "pro", Window(math.nan, NOW + H),
                           Window(10, NOW + 50 * H)),
        "inf pct": Account(1, "pro", Window(0, NOW + H),
                           Window(math.inf, NOW + 50 * H)),
        "inf resets_at": Account(1, "pro", Window(0, NOW + H),
                                 Window(10, math.inf)),
    }


def test_non_finite_values_make_the_account_unmeasured():
    good, bad = _bad_accounts()
    for name, a in bad.items():
        s = score_account(a, NOW)
        assert not s.usable and s.reason == "unmeasured", name
        assert rank([a, good], NOW) == [9], name


def test_rank_does_not_depend_on_input_order_with_non_finite_input():
    good, bad = _bad_accounts()
    others = [acct(2, p7=10, r7=40), acct(3, p7=20, r7=80)]
    for name, a in bad.items():
        first = rank([a, *others, good], NOW)
        second = rank([good, *reversed(others), a], NOW)
        assert 1 not in first and first == second, name


# --- the engine threshold (S1f) --------------------------------------------
# A candidate must sit strictly below the engine's switch line in every
# window, or the engine forces it out on the next tick (the S2 flap).


def test_weekly_at_the_threshold_is_not_a_candidate():
    at = acct(1, p7=90.0, r7=24)
    below = acct(2, p7=89.9, r7=24)
    assert rank([at], NOW) == []
    assert rank([below], NOW) == [2]
    assert DEFAULT_THRESHOLD == 90.0


def test_weekly_threshold_has_its_own_line():
    weekly92 = acct(1, p7=92.0, r7=24)
    assert rank([weekly92], NOW) == []
    assert rank([weekly92], NOW, weekly_threshold=95.0) == [1]
    # The 5-hour window keeps `threshold` (90) as its line.
    five90 = acct(2, p5=90.0, p7=10, r7=24)
    assert rank([five90], NOW, weekly_threshold=95.0) == []
    five89 = acct(3, p5=89.9, p7=10, r7=24)
    assert rank([five89], NOW, weekly_threshold=95.0) == [3]


def test_threshold_is_a_parameter():
    a = acct(1, p5=50, p7=80, r7=24)
    assert rank([a], NOW) == [1]
    assert rank([a], NOW, threshold=80.0) == []
    assert rank([a], NOW, threshold=50.0, weekly_threshold=85.0) == []
    assert rank([a], NOW, threshold=50.1, weekly_threshold=85.0) == [1]


def test_five_hour_threshold_applies_to_pro_and_max_not_ultra():
    for plan in ("pro", "max"):
        assert rank([acct(1, plan, p5=90.0, p7=10, r7=48)], NOW) == [], plan
        assert rank([acct(1, plan, p5=89.9, p7=10, r7=48)], NOW) == [1], plan
    assert rank([acct(1, "ultra", p5=95.0, p7=10, r7=48)], NOW) == [1]
    # The weekly line still applies to Ultra.
    assert rank([acct(1, "ultra", p5=0, p7=90.0, r7=48)], NOW) == []


def test_rolled_over_window_is_below_the_threshold():
    stale5 = Account(1, "pro", Window(95, NOW - 60), Window(10, NOW + 48 * H))
    stale7 = Account(2, "pro", Window(0, NOW + H), Window(95, NOW - 60))
    # Changed in B1b (was [2, 1] with e = 1 for the expired week): the new
    # week of account 2 started 60 seconds ago, so its deficit is near 0;
    # f = 1.1 * 0.10 / 0.7144 = 0.154 and account 1 has 0.154 * 71.4 - 10 = 1.0.
    assert rank([stale5, stale7], NOW) == [1, 2]


def test_active_at_the_threshold_is_a_forced_switch_without_cooldown():
    # The caller flag is False: the module itself sees the active account at
    # the line and forces the switch, inside both cooldowns.
    for active in (acct(1, p7=90.0, r7=48), acct(1, p5=90.0, p7=10, r7=48)):
        got = decide([active, acct(2, p7=40, r7=100)], 1, NOW,
                     last_proactive_ts=NOW - 10, last_manual_ts=NOW - 10,
                     active_at_threshold=False)
        assert got == SwitchTo(2, "threshold")


def test_active_at_the_threshold_with_no_candidate_below_it_stays():
    accounts = [acct(1, p7=92, r7=48), acct(2, p7=91, r7=100)]
    assert run(accounts, 1) == Stay()


def test_decide_passes_the_thresholds_on():
    accounts = [acct(1, p7=50, r7=100), acct(2, p7=92, r7=6)]
    assert run(accounts, 1) == Stay()
    got = decide(accounts, 1, NOW, last_proactive_ts=None, last_manual_ts=None,
                 active_at_threshold=False, weekly_threshold=95.0)
    assert got == SwitchTo(2, "proactive")


_GRID_PCTS = (0.0, 40.0, 70.0, 85.0, 89.9, 90.0, 90.1, 92.0, 95.0, 97.0, 100.0)
_GRID_PLANS = ("pro", "pro", "max", "ultra", "unknown")
_GRID_LINES = ((90.0, None), (85.0, 95.0), (90.0, 92.0))


def test_decide_never_moves_to_an_account_at_or_over_its_lines():
    rng = random.Random(20261110)
    proactive = 0
    for case in range(200):
        threshold, weekly = _GRID_LINES[case % len(_GRID_LINES)]
        line7 = weekly if weekly is not None else threshold
        accounts = [
            acct(i, rng.choice(_GRID_PLANS), p5=rng.choice(_GRID_PCTS),
                 p7=rng.choice(_GRID_PCTS), r7=rng.choice((None, 4, 24, 100)))
            for i in (1, 2, 3)
        ]
        by_id = {a.id: a for a in accounts}
        got = decide(accounts, rng.choice((1, 2, 3)), NOW,
                     last_proactive_ts=None, last_manual_ts=None,
                     active_at_threshold=False, threshold=threshold,
                     weekly_threshold=weekly)
        if not isinstance(got, SwitchTo):
            continue
        proactive += got.reason == "proactive"
        target = by_id[got.account_id]
        assert target.seven_day.pct < line7, (case, accounts, got)
        if target.plan != "ultra":
            assert target.five_hour.pct < threshold, (case, accounts, got)
    assert proactive >= 20


# --- the candidate margin (S1g) --------------------------------------------
# A CANDIDATE must sit below `line - candidate_margin` in every window. The
# active account is judged at the real line (margin 0).


def test_default_candidate_margin_is_zero():
    assert DEFAULT_CANDIDATE_MARGIN == 0.0
    a = acct(1, p7=89.9, r7=24)
    assert score_account(a, NOW).usable
    assert score_account(a, NOW, candidate_margin=0.0).usable


def test_candidate_at_85_weekly_is_usable_with_margin_0_not_10():
    cand = acct(2, p7=85.0, r7=24)
    assert rank([cand], NOW, threshold=90.0) == [2]
    assert rank([cand], NOW, threshold=90.0, candidate_margin=0.0) == [2]
    assert rank([cand], NOW, threshold=90.0, candidate_margin=10.0) == []
    s = score_account(cand, NOW, threshold=90.0, candidate_margin=10.0)
    assert not s.usable and s.reason == "weekly threshold"
    # The line is strict: 79.9 is below 90 - 10, 80.0 is not.
    assert rank([acct(3, p7=79.9, r7=24)], NOW, candidate_margin=10.0) == [3]
    assert rank([acct(3, p7=80.0, r7=24)], NOW, candidate_margin=10.0) == []


def test_margin_applies_to_the_weekly_threshold_when_set():
    cand = acct(2, p7=88.0, r7=24)
    assert rank([cand], NOW, weekly_threshold=95.0, candidate_margin=5.0) == [2]
    assert rank([cand], NOW, weekly_threshold=95.0, candidate_margin=8.0) == []


def test_margin_applies_to_the_five_hour_window():
    cand = acct(2, p5=82.0, p7=10, r7=24)
    assert rank([cand], NOW, candidate_margin=5.0) == [2]
    assert rank([cand], NOW, candidate_margin=10.0) == []
    s = score_account(cand, NOW, candidate_margin=10.0)
    assert not s.usable and s.reason == "5h threshold"


def test_ultra_ignores_the_five_hour_margin():
    ultra = acct(1, "ultra", p5=95.0, p7=10, r7=48)
    assert rank([ultra], NOW, candidate_margin=10.0) == [1]
    assert rank([ultra], NOW, candidate_margin=50.0) == [1]
    # The weekly margin still applies to Ultra.
    assert rank([acct(1, "ultra", p7=85.0, r7=48)], NOW, candidate_margin=10.0) == []


def test_candidate_margin_clamps_into_0_to_50():
    a = acct(1, p7=45.0, r7=24)
    # A negative margin is clamped to 0: no line above the real one.
    assert rank([acct(2, p7=89.9, r7=24)], NOW, candidate_margin=-20.0) == [2]
    assert rank([acct(2, p7=90.0, r7=24)], NOW, candidate_margin=-20.0) == []
    # A margin over 50 is clamped to 50: the weekly line is 40.
    assert rank([a], NOW, candidate_margin=50.0) == []
    assert rank([acct(3, p7=39.9, r7=24)], NOW, candidate_margin=500.0) == [3]
    assert rank([a], NOW, candidate_margin=500.0) == []
    # A non-finite margin is clamped too, not passed through.
    assert rank([acct(3, p7=39.9, r7=24)], NOW, candidate_margin=math.inf) == [3]
    assert rank([acct(2, p7=89.9, r7=24)], NOW, candidate_margin=-math.inf) == [2]
    assert rank([acct(2, p7=89.9, r7=24)], NOW, candidate_margin=math.nan) == [2]


def _margin_decide(accounts, active, margin, **kw):
    return decide(accounts, active, NOW, last_proactive_ts=kw.get("last_proactive"),
                  last_manual_ts=None, active_at_threshold=False,
                  threshold=90.0, candidate_margin=margin)


def test_active_inside_the_margin_is_not_forced_out():
    # Active at 85% weekly: inside the margin of 10, under the threshold 90.
    active = acct(1, p7=85.0, r7=48)
    other = acct(2, p7=10.0, r7=100)
    got = _margin_decide([active, other], 1, 10.0, last_proactive=NOW - 10)
    assert not isinstance(got, SwitchTo), got
    assert got == Hold("cooldown")
    # Without a cooldown the move is proactive, never a forced switch.
    got = _margin_decide([active, other], 1, 10.0)
    assert got != SwitchTo(2, "threshold")


def test_active_inside_the_margin_keeps_its_hysteresis():
    # The active account is scored at margin 0, so a close candidate does not
    # take over: the same Stay as without a margin.
    # Changed in B1 (was a STICKY_RATIO check): the two deficits are 3 points
    # apart, below DEFICIT_MARGIN_PTS.
    a = acct(1, p7=82.0, r7=48)
    b = acct(2, p7=79.0, r7=48)
    f = balance.line_height([a, b], NOW)
    gap = balance.deficit(b, NOW, f) - balance.deficit(a, NOW, f)
    assert 0 < gap < balance.DEFICIT_MARGIN_PTS
    assert _margin_decide([a, b], 1, 0.0) == Stay()
    assert _margin_decide([a, b], 1, 10.0) == Stay()


def test_active_at_the_threshold_is_forced_out_with_any_margin():
    for margin in (0.0, 5.0, 10.0, 15.0, 50.0):
        active = acct(1, p7=90.0, r7=48)
        other = acct(2, p7=10.0, r7=100)
        got = _margin_decide([active, other], 1, margin, last_proactive=NOW - 10)
        assert got == SwitchTo(2, "threshold"), margin


def test_forced_switch_uses_the_margin_for_its_candidates():
    # Changed in S1h (was Stay at margin 10): the only other account sits
    # inside the margin, so the forced switch falls back to margin 0.
    active = acct(1, p7=90.0, r7=48)
    near = acct(2, p7=85.0, r7=100)
    assert _margin_decide([active, near], 1, 0.0) == SwitchTo(2, "threshold")
    assert _margin_decide([active, near], 1, 10.0) == SwitchTo(2, "threshold")


# --- the forced-switch fallback to margin 0 (S1h) ----------------------------
# A forced switch that finds no candidate below the margin uses margin 0 for
# that decision. The proactive path keeps the margin.


def _inside_margin_fleet(active):
    # Two candidates at 85% weekly: inside a margin of 10, below the line 90.
    # Account 3 resets sooner, so it ranks first at margin 0.
    return [active, acct(2, p7=85.0, r7=100), acct(3, p7=85.0, r7=20)]


def test_forced_switch_with_every_candidate_inside_the_margin_falls_back():
    fleet = _inside_margin_fleet(acct(1, p7=92.0, r7=48))  # over the line
    assert rank(fleet, NOW, candidate_margin=10.0) == []
    assert rank(fleet, NOW)[0] == 3
    # Over the line, inside both cooldowns: the best usable account at margin 0.
    got = _margin_decide(fleet, 1, 10.0, last_proactive=NOW - 10)
    assert got == SwitchTo(3, "threshold")
    # The caller's threshold flag falls back the same way.
    usable_active = acct(1, p7=50.0, r7=48)
    got = decide(_inside_margin_fleet(usable_active), 1, NOW,
                 last_proactive_ts=None, last_manual_ts=None,
                 active_at_threshold=True, candidate_margin=10.0)
    assert got == SwitchTo(3, "threshold")
    # A blocked active account and a gated one fall back too.
    for active in (acct(1, p7=10.0, r7=48, blocked=True),
                   acct(1, p5=95.0, r5=2, p7=10.0, r7=48)):
        got = _margin_decide(_inside_margin_fleet(active), 1, 10.0)
        assert got == SwitchTo(3, "threshold"), active


def test_forced_switch_prefers_a_candidate_below_the_margin():
    # Account 3 inside the margin ranks first at margin 0, but account 4 is
    # below the margin: the fallback is not used, account 4 wins.
    fleet = [*_inside_margin_fleet(acct(1, p7=92.0, r7=48)),
             acct(4, p7=60.0, r7=150)]
    assert rank(fleet, NOW)[0] == 3
    assert rank(fleet, NOW, candidate_margin=10.0) == [4]
    assert _margin_decide(fleet, 1, 10.0) == SwitchTo(4, "threshold")


def test_proactive_path_with_candidates_inside_the_margin_stays():
    # The same candidates, with a usable active account: the margin holds.
    fleet = _inside_margin_fleet(acct(1, p7=50.0, r7=160))
    assert _margin_decide(fleet, 1, 0.0) == SwitchTo(3, "proactive")
    assert _margin_decide(fleet, 1, 10.0) == Stay()
    assert _margin_decide(fleet, 1, 10.0, last_proactive=NOW - 10) == Stay()


def test_forced_switch_with_no_usable_candidate_at_all_stays():
    # Every other account is over the line, blocked or gated: no fallback
    # target exists at margin 0 either.
    fleet = [acct(1, p7=92.0, r7=48), acct(2, p7=91.0, r7=100),
             acct(3, p7=10.0, r7=50, blocked=True),
             acct(4, p5=95.0, r5=2, p7=10.0, r7=50)]
    assert rank(fleet, NOW) == []
    for margin in (0.0, 10.0):
        assert _margin_decide(fleet, 1, margin) == Stay(), margin


_MARGIN_GRID_PCTS = (0.0, 40.0, 70.0, 75.0, 79.9, 80.0, 84.9, 85.0, 87.0,
                     89.9, 90.0, 92.0, 97.0, 100.0)


def test_decide_never_moves_to_an_account_inside_the_margin():
    # Changed in S1h: a forced SwitchTo may fall back to margin 0, so it is
    # checked against the real lines; a proactive one against line - margin.
    fallbacks = 0
    for margin in (0.0, 5.0, 10.0):
        rng = random.Random(20261110 + int(margin))
        proactive = 0
        for case in range(200):
            threshold, weekly = _GRID_LINES[case % len(_GRID_LINES)]
            real7 = weekly if weekly is not None else threshold
            real5 = threshold
            accounts = [
                acct(i, rng.choice(_GRID_PLANS), p5=rng.choice(_MARGIN_GRID_PCTS),
                     p7=rng.choice(_MARGIN_GRID_PCTS),
                     r7=rng.choice((None, 4, 24, 100)))
                for i in (1, 2, 3)
            ]
            by_id = {a.id: a for a in accounts}
            got = decide(accounts, rng.choice((1, 2, 3)), NOW,
                         last_proactive_ts=None, last_manual_ts=None,
                         active_at_threshold=False, threshold=threshold,
                         weekly_threshold=weekly, candidate_margin=margin)
            if not isinstance(got, SwitchTo):
                continue
            proactive += got.reason == "proactive"
            cut = margin if got.reason == "proactive" else 0.0
            line7, line5 = real7 - cut, real5 - cut
            target = by_id[got.account_id]
            assert target.seven_day.pct < line7, (margin, case, accounts, got)
            if target.plan != "ultra":
                assert target.five_hour.pct < line5, (margin, case, accounts, got)
            inside = not (target.seven_day.pct < real7 - margin and (
                target.plan == "ultra" or target.five_hour.pct < real5 - margin))
            fallbacks += got.reason == "threshold" and inside
        assert proactive >= 20, margin
    # The grid reaches the fallback: some forced targets sit inside a margin.
    assert fallbacks >= 1


# --- the proactive path with a margin (B1; replaces the tier rule of S1i) ----
# Changed in B1: the classes are gone. A usable active account inside the
# candidate margin is compared by deficit with the best candidate.


def _reviewer_fleet_weekly(active_p7=85.0):
    # The frozen review: Pro 1 active at 85% weekly, Pro 2 at 86%, Max at 5%.
    return [acct(1, "pro", p7=active_p7, r7=48), acct(2, "pro", p7=86.0, r7=100),
            acct(3, "max", p7=5.0, r7=120)]


def _reviewer_fleet_five_hour(active_p5=85.0):
    # The same fleet in the 5-hour window, with a low weekly use.
    return [acct(1, "pro", p5=active_p5, p7=10.0, r7=48),
            acct(2, "pro", p5=86.0, p7=10.0, r7=100),
            acct(3, "max", p5=5.0, p7=10.0, r7=120)]


def test_reviewer_example_weekly_moves_to_the_underused_max():
    # Changed in B1 (was Stay, "never a worse tier"): f = 0.846; the Max has
    # a deficit of 19.2 and the active Pro -24.6, so the use moves to the Max.
    fleet = _reviewer_fleet_weekly()
    assert _margin_decide(fleet, 1, 0.0) == SwitchTo(3, "proactive")
    assert rank(fleet, NOW, candidate_margin=10.0) == [3]
    assert _margin_decide(fleet, 1, 10.0) == SwitchTo(3, "proactive")
    assert _margin_decide(fleet, 1, 10.0, last_proactive=NOW - 10) == Hold("cooldown")


def test_reviewer_example_five_hour_stays_at_margin_10():
    # The active Pro has the largest deficit (11.6 against -1.4 for the Max).
    fleet = _reviewer_fleet_five_hour()
    assert _margin_decide(fleet, 1, 0.0) == Stay()
    assert rank(fleet, NOW, candidate_margin=10.0) == [3]
    assert _margin_decide(fleet, 1, 10.0) == Stay()


def test_reviewer_example_over_the_line_is_forced_to_the_max():
    # The forced path may still move to any class.
    for fleet in (_reviewer_fleet_weekly(active_p7=91.0),
                  _reviewer_fleet_five_hour(active_p5=91.0)):
        assert _margin_decide(fleet, 1, 10.0) == SwitchTo(3, "threshold")
        assert _margin_decide(fleet, 1, 10.0, last_proactive=NOW - 10) == \
            SwitchTo(3, "threshold")


def test_pro_inside_the_margin_moves_to_a_far_better_pro():
    # Changed in B1 (was a STICKY_RATIO check): deficits 32 against -65.
    active = acct(1, "pro", p7=85.0, r7=100)
    better = acct(2, "pro", p7=10.0, r7=24)
    fleet = [active, better, acct(3, "max", p7=5.0, r7=120)]
    assert _margin_decide(fleet, 1, 10.0) == SwitchTo(2, "proactive")


def test_proactive_switch_needs_the_deficit_margin_or_the_loss_guard():
    # Changed in B1 (was "never to a worse tier"): every proactive move from
    # a usable active account beats it by DEFICIT_MARGIN_PTS, or goes to a
    # loss-guard account while the active account is not one.
    seen_inside = by_margin = by_guard = 0
    for margin in (0.0, 5.0, 10.0):
        rng = random.Random(20261111 + int(margin))
        for case in range(400):
            threshold, weekly = _GRID_LINES[case % len(_GRID_LINES)]
            accounts = [
                acct(i, rng.choice(_GRID_PLANS), p5=rng.choice(_MARGIN_GRID_PCTS),
                     p7=rng.choice(_MARGIN_GRID_PCTS),
                     r7=rng.choice((None, 4, 24, 100)))
                for i in (1, 2, 3)
            ]
            active_id = rng.choice((1, 2, 3))
            by_id = {a.id: a for a in accounts}
            lines = {"threshold": threshold, "weekly_threshold": weekly}
            if not score_account(by_id[active_id], NOW, **lines).usable:
                continue
            inside = not score_account(by_id[active_id], NOW, **lines,
                                       candidate_margin=margin).usable
            seen_inside += inside
            got = decide(accounts, active_id, NOW, last_proactive_ts=None,
                         last_manual_ts=None, active_at_threshold=False,
                         candidate_margin=margin, **lines)
            if not (isinstance(got, SwitchTo) and got.reason == "proactive"):
                continue
            f = balance.line_height(accounts, NOW)
            target, active = by_id[got.account_id], by_id[active_id]
            gap = balance.deficit(target, NOW, f) - balance.deficit(active, NOW, f)

            def guarded(a, f=f):
                reset = a.seven_day.resets_at
                return f >= 0.9 and reset is not None and 0 < reset - NOW <= 12 * H

            if gap > balance.DEFICIT_MARGIN_PTS:
                by_margin += 1
            else:
                assert guarded(target) and not guarded(active), (
                    margin, case, accounts, got)
                by_guard += 1
    # The grid reaches usable active accounts inside a margin, and both
    # reasons for a proactive move.
    assert seen_inside >= 20
    assert by_margin >= 20 and by_guard >= 1


class TestRunwayRule:
    """A proactive move never goes to an account with less 5-hour room."""

    NOW = 1_000_000.0
    WEEK_END = NOW + 84 * 3600.0

    def _acc(self, num, plan, five, week):
        return balance.Account(num, plan, balance.Window(five, self.NOW + 3600.0),
                               balance.Window(week, self.WEEK_END))

    # The incident: the Max 5-hour percent rose 4.3 points a minute = 21.5 Pro points.
    BURN = 21.5 / 60.0

    def _decide(self, accounts, active, at_threshold=False, burn=BURN):
        return balance.decide(accounts, active, self.NOW, None, None, at_threshold,
                              threshold=99.0, burn_rate=burn)

    def test_five_hour_room_is_capacity_times_points_below_the_line(self):
        assert balance.five_hour_room(self._acc(5, "max", 80.0, 18.0), self.NOW, 99.0) == 95.0
        assert balance.five_hour_room(self._acc(4, "pro", 29.0, 27.0), self.NOW, 99.0) == 70.0
        assert balance.five_hour_room(self._acc(9, "ultra", 99.0, 10.0), self.NOW, 99.0) == float("inf")
        unmeasured = balance.Account(1, "pro", None, balance.Window(10.0, self.WEEK_END))
        assert balance.five_hour_room(unmeasured, self.NOW, 99.0) == 0.0

    def test_the_incident_max_at_80_does_not_move_to_pro_at_29(self):
        # 2026-11-16: the active Max had more 5-hour room than the Pro target.
        accounts = [self._acc(5, "max", 80.0, 18.0), self._acc(4, "pro", 29.0, 0.0)]
        assert isinstance(self._decide(accounts, 5), balance.Stay)

    def test_with_no_burn_the_rule_changes_nothing(self):
        accounts = [self._acc(5, "max", 80.0, 18.0), self._acc(4, "pro", 29.0, 0.0)]
        out = self._decide(accounts, 5, burn=0.0)
        assert isinstance(out, balance.SwitchTo) and out.account_id == 4

    def test_a_light_burn_lets_the_move_happen(self):
        # 1 Pro point a minute: 30 points last half an hour; the Pro has 70.
        accounts = [self._acc(5, "max", 80.0, 18.0), self._acc(4, "pro", 29.0, 0.0)]
        out = self._decide(accounts, 5, burn=1.0 / 60.0)
        assert isinstance(out, balance.SwitchTo) and out.account_id == 4

    def test_a_target_with_more_room_than_the_active_is_still_chosen(self):
        accounts = [self._acc(5, "max", 90.0, 60.0), self._acc(4, "pro", 0.0, 0.0)]
        out = self._decide(accounts, 5)  # active room 45, target room 99
        assert isinstance(out, balance.SwitchTo) and out.account_id == 4 and out.reason == "proactive"

    def test_the_next_candidate_with_enough_room_is_used(self):
        accounts = [self._acc(5, "max", 60.0, 60.0), self._acc(4, "pro", 0.0, 0.0),
                    self._acc(6, "max", 0.0, 10.0)]
        out = self._decide(accounts, 5)  # active room 195: only the other Max has more
        assert isinstance(out, balance.SwitchTo) and out.account_id == 6

    def test_a_forced_switch_is_not_limited_by_the_rule(self):
        accounts = [self._acc(5, "max", 99.0, 18.0), self._acc(4, "pro", 29.0, 27.0)]
        out = self._decide(accounts, 5, at_threshold=True)
        assert isinstance(out, balance.SwitchTo) and out.account_id == 4 and out.reason == "threshold"


def test_the_active_account_is_not_forced_out_below_the_user_line():
    # Week of 2026-11-16: with a 99% line the balanced strategy left the
    # active account at 91% because of the 10-point headroom gate.
    now = 1_000_000.0
    end = now + 84 * 3600.0
    accounts = [balance.Account(1, "pro", balance.Window(91.0, now + 3600.0), balance.Window(30.0, end)),
                balance.Account(2, "pro", balance.Window(0.0, now + 3600.0), balance.Window(30.0, end))]
    out = balance.decide(accounts, 1, now, None, None, False, threshold=99.0)
    assert isinstance(out, balance.Stay)
    accounts[0] = balance.Account(1, "pro", balance.Window(99.0, now + 3600.0), balance.Window(30.0, end))
    out = balance.decide(accounts, 1, now, None, None, False, threshold=99.0)
    assert isinstance(out, balance.SwitchTo) and out.reason == "threshold"
