"""Tests for tools/rotation_sim.py, the rotation simulator v2 (task R1).

The tool lives outside ``src``; it is loaded from its file with importlib.
All time is a fake clock (ticks of 5 minutes). Expected values of the small
cases are derived by hand in the comments.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import os
import random
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "rotation_sim.py"


def _load():
    spec = importlib.util.spec_from_file_location("rotation_sim", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rotation_sim"] = module  # a forked pool pickles by module name
    spec.loader.exec_module(module)
    return module


rs = _load()
W = rs.WEEK_TICKS


def scn(accounts, demand, outages=(), warm=0):
    return rs.Scenario(list(accounts), list(demand), list(outages), warm, {})


def scripted(fn):
    """A policy that picks the account from the tick number: fn(tick) -> id."""
    return rs.Policy("scripted", lambda r, active, now, st: fn(now // rs.TICK_S), dict)


def sim(s, policy, share=1.0, factor=1.0, **kw):
    return rs.simulate(s, policy, five_hour_share=share, switch_cost_factor=factor, trace=True, **kw)


def seq(res):
    return [row[1] for row in res["trace"]]


# --- the account: weekly reset and 5-hour window ---------------------------


def test_weekly_reset_is_fixed_and_used_or_not():
    # Offset 50. 0.01 per tick for ticks 0..49 uses 0.5. Resets at 50 and 2066
    # whether the account is used or not; after the reset it serves a full week.
    demand = [0.01] * 50 + [0.0] * 10 + [0.01] * 10 + [0.0] * (W + 100 - 70)
    res = sim(scn([rs.account("pro", 50)], demand), "earliest_reset")
    assert [r[1] for r in res["resets"]] == [50, 50 + W]
    assert res["resets"][0][2] == pytest.approx(50.0)
    assert res["resets"][1][2] == pytest.approx(10.0)  # 10 ticks of 0.01 after the first reset


def test_idle_account_at_its_reset_wastes_everything():
    res = sim(scn([rs.account("pro", 100)], [0.0] * (2 * W)), "earliest_reset")
    assert [r[1] for r in res["resets"]] == [100, 100 + W]
    assert res["metrics"]["waste"] == pytest.approx(100.0)
    assert res["metrics"]["waste_no_demand"] == pytest.approx(100.0)
    assert res["metrics"]["waste_avoidable"] == 0.0


def test_offset_zero_first_resets_after_a_week():
    res = sim(scn([rs.account("pro", 0)], [0.0] * W), "earliest_reset")
    assert [r[1] for r in res["resets"]] == [W]


def test_five_hour_window_starts_at_first_use_blocks_closes_and_has_no_idle_reset():
    a = rs.AccountState(0, 1.0, 0.12)  # cap 0.12
    assert a.five_reset_tick(0) is None and not a.window_open(0)  # idle: no reset time
    assert a.headroom(0) == pytest.approx(0.12)
    a.serve(5, 0.05)  # window starts at tick 5
    assert a.five_reset_tick(6) == 5 + rs.WINDOW_TICKS
    a.serve(6, 0.07)  # reaches the cap
    assert a.headroom(7) == 0.0  # blocked
    assert a.headroom(5 + rs.WINDOW_TICKS - 1) == 0.0  # still blocked one tick before
    assert not a.window_open(5 + rs.WINDOW_TICKS)  # closed after exactly 5 hours
    assert a.headroom(5 + rs.WINDOW_TICKS) == pytest.approx(0.12)
    assert a.five_reset_tick(5 + rs.WINDOW_TICKS + 10) is None  # idle again
    a.serve(100, 0.01)  # a new window starts at the next use, not at tick 65
    assert a.five_reset_tick(101) == 100 + rs.WINDOW_TICKS


def test_account_never_served_at_its_limits_in_a_run():
    # 0.05 per tick, cap 0.12 per window: ticks 0,1 serve 0.05; tick 2 serves
    # 0.02 and 0.03 is unserved; nothing is served until tick 60 (new window).
    res = sim(scn([rs.account("pro", 1000)], [0.05] * 70), "earliest_reset", share=0.12)
    served = [row[3] for row in res["trace"]]
    assert served[:3] == pytest.approx([0.05, 0.05, 0.02])
    assert all(x == 0.0 for x in served[3:60])
    assert served[60:63] == pytest.approx([0.05, 0.05, 0.02])
    # weekly limit: 0.3 per tick, share 1.0: 0.3, 0.3, 0.3, 0.1, then nothing
    res = sim(scn([rs.account("pro", 1000)], [0.3] * 8), "earliest_reset", share=1.0)
    assert [row[3] for row in res["trace"]] == pytest.approx([0.3, 0.3, 0.3, 0.1, 0, 0, 0, 0])
    assert sum(row[5] for row in res["trace"]) <= 1.0 + 1e-9


def test_account_without_five_hour_limit_is_never_blocked_by_a_window():
    demand = [0.2] * 10
    free = sim(scn([rs.account("pro", 1000, five_hour_share=None)], demand), "earliest_reset", share=0.12)
    assert [row[3] for row in free["trace"]] == pytest.approx([0.2] * 5 + [0.0] * 5)  # only the week limits
    capped = sim(scn([rs.account("pro", 1000)], demand), "earliest_reset", share=0.12)
    assert sum(row[3] for row in capped["trace"]) == pytest.approx(0.12)
    a = rs.AccountState(0, 1.0, None)
    a.serve(0, 0.5)
    assert a.five_reset_tick(1) is None and a.reading(1).five_pct == 0.0


def test_per_account_share_overrides_the_run_share():
    demand = [0.05] * 10
    s = scn([rs.account("pro", 1000, five_hour_share=0.06)], demand)
    res = sim(s, "earliest_reset", share=0.5)
    assert sum(row[3] for row in res["trace"]) == pytest.approx(0.06)


def test_capacity_is_any_positive_number_and_plans_exist():
    assert rs.PLAN_CAPACITY == {"pro": 1.0, "max": 5.0, "ultra": 10.0, "max20": 20.0}
    assert rs.account("custom", capacity=3.7).capacity == 3.7
    with pytest.raises(ValueError):
        rs.account("custom", capacity=0)


# --- conservation, switch cost ---------------------------------------------


@pytest.mark.parametrize("policy", list(rs.POLICIES))
def test_conservation_served_plus_unserved_equals_demand(policy):
    s = rs.preset_scenario("heavy", seed=3, weeks=1)
    res = sim(s, policy, share=0.12, factor=1.5)
    assert res["totals"]["served"] + res["totals"]["unserved"] == pytest.approx(sum(s.demand), rel=1e-9)
    for _t, _a, d, sv, un, _c in res["trace"]:
        assert sv + un == pytest.approx(d, rel=1e-9, abs=1e-12)
        assert un >= 0 and sv >= 0
    assert res["totals"]["unserved"] > 0  # the case does stress the limits


def test_switch_cost_applies_once_per_switch():
    # accounts 0,1; account 0 on ticks 0-2, 1 on 3-5, 0 on 6-8; factor 2.
    pol = scripted(lambda t: 0 if t < 3 or t >= 6 else 1)
    res = sim(scn([rs.account("pro", 1000), rs.account("pro", 1000)], [0.01] * 9), pol, factor=2.0)
    consumed = [row[5] for row in res["trace"]]
    # tick 0 is the first activation (free); ticks 3 and 6 are switches.
    assert consumed == pytest.approx([0.01, 0.01, 0.01, 0.02, 0.01, 0.01, 0.02, 0.01, 0.01])
    assert res["totals"]["served"] == pytest.approx(0.09)  # the overhead is not served work
    assert len(res["switch_ticks"]) == 2


# --- policies on tiny hand-checked cases ------------------------------------


def two_pro(offsets=(0, 0)):
    return [rs.account("pro", o) for o in offsets]


def test_threshold_best_sequence():
    # 0.45 per tick, threshold 90. A: 45, 90 -> at tick 2 the reading is 90, so
    # go to B (headroom 100 > 10). B: 45, 90 (tick 4: 90 but A has 10, not
    # more than B's 10: stay); B serves the last 0.1; tick 5 B is at 100 and A
    # has 10 > 0: back to A.
    res = sim(scn(two_pro(), [0.45] * 6), "threshold_best")
    assert seq(res) == [0, 0, 1, 1, 1, 0]


def test_earliest_reset_sequence():
    # A resets at tick 10, B at tick 5; 0.3 per tick. B first (soonest): 0.3,
    # 0.6, 0.9, 0.1 (tick 3); at tick 4 B reads 100 so A; at tick 5 B resets
    # but its next reset is a week away, so A (soonest 10) keeps the work.
    res = sim(scn([rs.account("pro", 10), rs.account("pro", 5)], [0.3] * 8), "earliest_reset")
    assert seq(res) == [1, 1, 1, 1, 0, 0, 0, 0]


def test_deficit_sequence_margin_and_dwell():
    # Both reset together, 0.01 per tick = 1 point per served tick; margin 4,
    # dwell 1800 s = 6 ticks. A: ticks 0-5 (at tick 5 the gap is 5 > 4 but the
    # dwell is 1500 s). Tick 6: gap 6, dwell 1800: B. B stays until its use
    # beats A's by more than 4: 11 points at tick 17 (gap 5, dwell 3300): A.
    res = sim(scn(two_pro(), [0.01] * 18), "deficit")
    assert seq(res) == [0] * 6 + [1] * 11 + [0]


def test_deficit_switches_at_once_when_the_active_account_is_blocked():
    # A has 5-hour cap 0.12; 0.07 per tick: tick 0 serves, tick 1 fills it
    # (0.05 served). At tick 2 A reads 100: leave at once, with no dwell.
    res = sim(scn(two_pro(), [0.07] * 3), "deficit", share=0.12)
    assert seq(res) == [0, 0, 1]


def test_deficit_equal_percent_across_sizes_while_threshold_best_is_not():
    # Constant light load: 0.1 of the fleet weekly capacity, one week.
    # Resets aligned (the lines coincide): equal percent within 5 points.
    # Resets staggered: the rule also drains the account nearest its reset, so
    # the percent is not equal, but it is still more uniform than threshold_best.
    for offsets, equal in (((0, 0, 0), True), ((0, 700, 1500), False)):
        accounts = [
            rs.account("pro", offsets[0]),
            rs.account("max", offsets[1]),
            rs.account("max20", offsets[2]),
        ]
        per_tick = 0.1 * 26.0 / W
        s = scn(accounts, [per_tick] * (2 * W), warm=1)
        res = {p: sim(s, p, share=0.12, factor=1.5) for p in ("deficit", "threshold_best")}
        pcts = {p: [r[2] for r in res[p]["resets"]] for p in res}
        assert len(pcts["deficit"]) == 3
        if equal:
            assert max(pcts["deficit"]) - min(pcts["deficit"]) < 5.0
            assert max(pcts["threshold_best"]) - min(pcts["threshold_best"]) > 20.0
        assert res["deficit"]["metrics"]["unevenness"] < res["threshold_best"]["metrics"]["unevenness"]


def test_prompts_only_week_threshold_best_piles_on_one_account():
    s = rs.preset_scenario("prompts_only_week_then_work", seed=1, weeks=1)
    s = scn(s.accounts, s.demand[:W], warm=0)  # the light week alone
    thr = sim(s, "threshold_best", share=0.12)
    dfc = sim(s, "deficit", share=0.12)
    # the story: everything on the first account, the other five untouched
    assert sorted(r[2] for r in thr["resets"])[:5] == [0.0] * 5
    assert thr["metrics"]["unevenness"] > 2 * dfc["metrics"]["unevenness"]


# --- readings: stale candidates and outages ---------------------------------


def test_policy_sees_readings_not_the_truth_stale_and_outage():
    calls = []

    def spy(readings, active, now, state):
        calls.append((now // rs.TICK_S, active, dict(readings)))
        return 0

    pol = rs.Policy("spy", spy, dict)
    s = scn([rs.account("pro", 1000)] * 3, [0.001] * 60, outages=[(20, 40)])
    sim(s, pol)
    by_tick = {t: (act, r) for t, act, r in calls}
    assert all(isinstance(x, rs.Reading) for x in by_tick[5][1].values())
    assert by_tick[0][1][0].as_of == 0
    for t in range(1, 20):  # round robin: account 1 polled on even ticks, 2 on odd ticks
        r = by_tick[t][1]
        assert r[0].as_of == t * rs.TICK_S
        assert r[0].week_pct == pytest.approx(0.1 * t)  # fresh truth of the active account
        assert r[1].as_of == (t if t % 2 == 0 else t - 1) * rs.TICK_S
        assert r[2].as_of == (t if t % 2 else t - 1) * rs.TICK_S
    for t in range(20, 40):  # outage: nothing updates, the truth moves away
        r = by_tick[t][1]
        assert r[0].as_of == 19 * rs.TICK_S
        assert r[0].week_pct == pytest.approx(1.9)
        assert r[0].week_pct < 0.1 * t  # the policy cannot see the truth
        assert r[1].as_of == 18 * rs.TICK_S and r[2].as_of == 19 * rs.TICK_S
    assert by_tick[40][1][0].as_of == 40 * rs.TICK_S  # fresh again


def test_outage_makes_a_policy_keep_a_full_account():
    # Account 0 fills up during the outage; the policy still reads it as free.
    s = scn(two_pro((1000, 1000)), [0.1] * 30, outages=[(2, 20)])
    res = sim(s, "earliest_reset", share=1.0)
    assert set(seq(res)[2:20]) == {0}
    assert sum(row[3] for row in res["trace"][2:20]) < 0.1 * 18  # stalls while it believes it is free


# --- metrics hand checks ------------------------------------------------------


def test_waste_split_no_demand_and_unevenness_and_low_reset():
    # A is used on ticks 0-59 (0.6), B on 60-69 (0.1); both reset at tick W.
    demand = [0.01] * 70 + [0.0] * (W - 70)
    res = sim(scn(two_pro(), demand), scripted(lambda t: 0 if t < 60 else 1))
    m = res["metrics"]
    assert m["waste"] == pytest.approx(100 * (0.4 + 0.9) / 2)  # 65
    assert m["waste_no_demand"] == pytest.approx(65.0)
    assert m["waste_avoidable"] == 0.0
    assert m["waste_pro"] == pytest.approx(65.0) and m["waste_max"] is None
    assert m["unevenness"] == pytest.approx(25.0)  # std of [60, 10]
    assert m["max_used_while_low"] == pytest.approx(60.0)  # B reset at 10 < 20 while A reached 60
    assert m["stall"] == 0.0 and m["served"] == pytest.approx(100.0)


def test_waste_split_avoidable_with_the_five_hour_cap():
    # One account, cap 0.12, 0.05 per tick for 10 ticks: served 0.12, unserved
    # 0.03 + 7 * 0.05 = 0.38 of 0.5 demand. Unused 0.88 = 0.38 avoidable + 0.50.
    demand = [0.05] * 10 + [0.0] * (W - 10)
    res = sim(scn([rs.account("pro", 0)], demand), "earliest_reset", share=0.12)
    m = res["metrics"]
    assert m["waste"] == pytest.approx(88.0)
    assert m["waste_avoidable"] == pytest.approx(38.0)
    assert m["waste_no_demand"] == pytest.approx(50.0)
    assert m["stall"] == pytest.approx(76.0)
    assert m["served"] == pytest.approx(24.0)
    assert m["stall_hours"] == pytest.approx(8 * 5 / 60)


def test_waste_by_plan_uses_each_plans_own_capacity():
    s = scn([rs.account("pro", 0), rs.account("max", 0), rs.account("max20", 0)], [0.0] * W)
    m = sim(s, "earliest_reset")["metrics"]
    assert m["waste_pro"] == m["waste_max"] == m["waste_max20"] == pytest.approx(100.0)
    assert m["waste_ultra"] is None


def test_flaps_and_switches_per_day_hand_checked():
    # Switches at ticks 10, 11, 20, 30, 31, 32: pairs within 2 ticks (10 min):
    # (10,11), (30,31), (31,32) = 3 flaps; 6 switches over 7 days.
    marks = [10, 11, 20, 30, 31, 32]
    pol = scripted(lambda t: sum(1 for m in marks if m <= t) % 2)
    res = sim(scn(two_pro((1000, 1000)), [0.0001] * W), pol)
    assert res["switch_ticks"] == marks
    assert res["metrics"]["flaps"] == 3
    assert res["metrics"]["switches_per_day"] == pytest.approx(6 / 7)


# --- scale invariance -----------------------------------------------------------


def _scaled(s, k):
    accounts = [a._replace(capacity=a.capacity * k) for a in s.accounts]
    return rs.Scenario(accounts, [d * k for d in s.demand], s.outages, s.warmup_weeks, s.meta)


@pytest.mark.parametrize("policy", list(rs.POLICIES))
@pytest.mark.parametrize("name", ["user_like", "mixed_sizes", "spiky"])
def test_scale_invariance_same_decisions_and_percent_metrics(policy, name):
    s = rs.preset_scenario(name, seed=2, weeks=1)
    a = sim(s, policy, share=0.12, factor=1.5)
    b = sim(_scaled(s, 7.0), policy, share=0.12, factor=1.5)
    assert seq(a) == seq(b)
    assert a["metrics"].keys() == b["metrics"].keys()
    for key, va in a["metrics"].items():
        vb = b["metrics"][key]
        assert (va is None) == (vb is None), key
        if va is not None:
            assert vb == pytest.approx(va, rel=1e-6, abs=1e-9), key
    assert len(set(seq(a))) > 1  # decisions were not trivial


# --- generator and presets ----------------------------------------------------------


def test_generator_stays_inside_its_documented_ranges_and_covers_shapes():
    R = rs.RANGES
    days, hours, big_small, plans = set(), set(), 0, set()
    for i in range(500):
        p = rs.draw_params(random.Random(f"t:{i}"), weeks=4)
        days.add(p["day_shape"])
        hours.add(p["hour_shape"])
        assert R["fleet_size"][0] <= len(p["accounts"]) <= R["fleet_size"][1]
        assert R["volume"][0] <= p["volume"] <= R["volume"][1]
        assert p["growth"] == 1.0 or R["growth"][0] <= p["growth"] <= R["growth"][1]
        assert R["burst_ratio"][0] <= p["burst_ratio"] <= R["burst_ratio"][1]
        assert R["active_prob"][0] <= p["active_prob"] <= R["active_prob"][1]
        assert R["conc_chance_per_hour"][0] <= p["conc_chance"] <= R["conc_chance_per_hour"][1]
        assert sum(p["day_weights"]) == pytest.approx(1.0) and len(p["day_weights"]) == 7
        assert sum(p["hour_weights"]) == pytest.approx(1.0) and len(p["hour_weights"]) == 24
        for _start, length in p["gaps"]:
            assert R["gap_hours"][0] * 12 <= length <= R["gap_hours"][1] * 12
        if p["regime"] is not None:
            assert p["regime"][1] in rs.REGIME_FACTORS
        for a, b in p["outages"]:
            assert R["outage_minutes"][0] <= (b - a) * 5 <= R["outage_minutes"][1]
        for a in p["accounts"]:
            plans.add(a.plan)
            assert a.capacity > 0 and 0 <= a.offset < W
            assert a.five_hour_share in (rs.INHERIT, None) or R["own_share"][0] <= a.five_hour_share <= R["own_share"][1]
            if a.plan == "custom":
                assert R["custom_capacity"][0] <= a.capacity <= R["custom_capacity"][1]
            else:
                assert a.capacity == rs.PLAN_CAPACITY[a.plan]
        if p["clustered"]:
            offs = sorted(a.offset for a in p["accounts"])
            gaps = [(offs[(k + 1) % len(offs)] - offs[k]) % W for k in range(len(offs))]
            assert W - max(gaps) <= rs.TICKS_PER_DAY * 2  # inside a 24 h start window plus the draw
        if p["fleet_event"]:
            kind, idx, tick = p["fleet_event"]
            assert idx >= 1 and W <= tick < 5 * W
        kinds = [a.plan for a in p["accounts"]]
        if kinds.count("max20") == 1 and kinds.count("pro") >= 3 and len(kinds) == kinds.count("pro") + 1:
            big_small += 1
    assert days == set(rs.DAY_SHAPES) and hours == set(rs.HOUR_SHAPES)
    assert plans == {*rs.PLAN_CAPACITY, "custom"}
    assert big_small > 0  # one 20x account with several 1x accounts


def test_generated_scenario_shape_and_volume_normalisation():
    s = rs.generate_scenario(5, 3, weeks=2)
    assert len(s.demand) == 3 * W and all(d >= 0 for d in s.demand)
    light = rs.preset_scenario("light", seed=1, weeks=2)  # no bursts, gaps or regime
    cap = sum(a.capacity for a in light.accounts)
    for w in range(3):
        assert sum(light.demand[w * W : (w + 1) * W]) == pytest.approx(0.15 * cap)


def test_presets_exist_and_user_like_has_the_six_reset_offsets():
    assert {"user_like", "prompts_only_week_then_work", "light", "heavy", "spiky", "mixed_sizes"} <= set(rs.PRESETS)
    u = rs.preset_scenario("user_like", seed=1, weeks=1)
    hours = [a.offset / rs.TICKS_PER_HOUR for a in u.accounts]
    # Sun 06:00, Mon 05:00, Tue 04:00, Wed 02:00, Wed 14:00, Sat 10:00 (Monday 00:00 = 0)
    assert hours == [150, 5, 28, 50, 62, 130]
    assert [a.plan for a in u.accounts].count("pro") == 4 and [a.plan for a in u.accounts].count("max") == 2
    # R1c: the preset now has an idle day a week, which empties one day of this
    # split; the split is checked on the same scenario without the idle periods.
    plain = {**rs.preset_params("user_like"), "idle_spec": None, "idle": []}
    u = rs.build_scenario(plain, random.Random("rotation-sim:preset:user_like:1"), 1)
    day = [sum(u.demand[W + d * 288 : W + (d + 1) * 288]) for d in range(7)]
    total = sum(day)
    # the weights are exact; the realised split carries the per-tick noise
    assert [round(100 * w) for w in rs.preset_params("user_like")["day_weights"]] == [8, 23, 20, 10, 11, 15, 13]
    for got, want in zip(day, [8, 23, 20, 10, 11, 15, 13]):
        assert abs(100 * got / total - want) < 3.0


def test_mixed_sizes_preset_fleet():
    m = rs.preset_scenario("mixed_sizes", seed=1, weeks=1)
    assert sorted(a.capacity for a in m.accounts) == [1.0, 1.0, 1.0, 5.0, 20.0]
    assert len({a.offset for a in m.accounts}) == 5


def test_prompts_only_preset_has_a_light_warmup_week():
    s = rs.preset_scenario("prompts_only_week_then_work", seed=1, weeks=2)
    cap = sum(a.capacity for a in s.accounts)
    assert sum(s.demand[:W]) == pytest.approx(0.1 * cap)
    assert sum(s.demand[W : 2 * W]) == pytest.approx(0.9 * cap)


# --- CLI ----------------------------------------------------------------------------------


def cli(capsys, *argv):
    assert rs.main(list(argv)) == 0
    return capsys.readouterr().out


def test_determinism_and_jobs_equality(capsys):
    base = ["--random", "10", "--weeks", "1", "--seed", "4", "--policy", "all"]
    one = cli(capsys, *base, "--jobs", "1")
    four = cli(capsys, *base, "--jobs", "4")
    again = cli(capsys, *base, "--jobs", "1")
    assert one == four == again
    assert cli(capsys, *base, "--jobs", "1", "--seed", "5") != one
    jone = cli(capsys, *base, "--jobs", "1", "--json")
    jfour = cli(capsys, *base, "--jobs", "4", "--json")
    assert jone == jfour


def test_preset_table_and_json_have_the_same_keys(capsys):
    argv = ["--preset", "light", "--weeks", "1", "--jobs", "1"]
    table = cli(capsys, *argv)
    header = [ln for ln in table.splitlines() if ln.startswith("policy")][0].split()
    data = json.loads(cli(capsys, *argv, "--json"))
    assert header[1:] == list(rs.METRICS)
    assert set(data["results"]["light"]) == set(rs.POLICIES)
    for metrics in data["results"]["light"].values():
        assert set(metrics) == set(header[1:])
    for name in rs.POLICIES:
        assert name in table


def test_random_table_and_json_have_the_same_keys(capsys):
    argv = ["--random", "4", "--weeks", "1", "--jobs", "1", "--policy", "deficit"]
    table = cli(capsys, *argv)
    header = [ln for ln in table.splitlines() if ln.startswith("policy stat")][0].split()
    data = json.loads(cli(capsys, *argv, "--json"))
    assert header[2:] == list(rs.METRICS)
    assert set(data["results"]["deficit"]) == set(header[2:])
    assert set(data["results"]["deficit"]["waste"]) == {"mean", "p10", "p50", "p90"}
    assert "deficit mean" in table and "deficit p90" in table


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--preset", "light", "--random", "3"],
        ["--preset", "nope"],
        ["--random", "0"],
        ["--random", "x"],
        ["--random", "3", "--weeks", "0"],
        ["--random", "3", "--five-hour-share", "0"],
        ["--random", "3", "--five-hour-share", "1.5"],
        ["--random", "3", "--policy", "bogus"],
        ["--random", "3", "--jobs", "0"],
        ["--random", "3", "--seed", "abc"],
    ],
)
def test_bad_arguments_exit_with_code_2(argv):
    with pytest.raises(SystemExit) as exc:
        rs.main(argv)
    assert exc.value.code == 2


def test_tool_touches_no_file_and_no_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    def refuse(*a, **k):
        raise AssertionError("the simulator must not open files or sockets")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(builtins, "open", refuse)
    cli(capsys, "--preset", "light", "--weeks", "1", "--jobs", "1")
    cli(capsys, "--random", "2", "--weeks", "1", "--jobs", "1", "--json")
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []


def test_script_entry_runs_with_a_clean_home(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, str(TOOL), "--preset", "mixed_sizes", "--weeks", "1", "--jobs", "1"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "mixed_sizes" in done.stdout and "deficit" in done.stdout
    assert list(tmp_path.iterdir()) == []
    bad = subprocess.run([sys.executable, str(TOOL), "--random", "0"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2


# ==========================================================================
# Round 2 (task R1b): bands, unavoidable stall, parameters, pressure,
# idle-gap switches, sweep, steady-state diagnosis. Written before the code.
# ==========================================================================


def rd(week_pct, remaining_s, five_pct=0.0, five_share=0.12, now=100 * 300):
    """A reading as the policy sees it: ``remaining_s`` seconds to the reset."""
    return rs.Reading(week_pct, five_pct, now + remaining_s, 1.0, now, five_share)


HOUR = 3600


# --- volume bands -------------------------------------------------------------


@pytest.mark.parametrize(
    "share, band",
    [
        (0.0, "light"),
        (0.4999, "light"),
        (0.5, "medium"),
        (0.8999, "medium"),
        (0.9, "heavy"),
        (1.0, "heavy"),
        (1.1, "heavy"),
        (1.1001, "over"),
        (3.0, "over"),
    ],
)
def test_band_assignment_at_the_edges(share, band):
    assert rs.band_of(share) == band
    assert rs.BANDS == ("light", "medium", "heavy", "over")


def test_volume_share_uses_the_measured_weeks_and_the_enabled_capacity():
    # one pro, two measured weeks of demand 0.4 and 0.6: mean 0.5 of capacity
    s = scn([rs.account("pro", 0)], [0.4 / W] * W + [0.6 / W] * W)
    assert rs.volume_share(s) == pytest.approx(0.5)
    assert rs.scenario_band(s) == "medium"
    # the warm-up week is not measured
    s2 = scn([rs.account("pro", 0)], [9.0 / W] * W + [0.2 / W] * W, warm=1)
    assert rs.volume_share(s2) == pytest.approx(0.2)
    assert rs.scenario_band(s2) == "light"


def test_random_output_reports_each_metric_per_band_and_overall(capsys):
    data = json.loads(cli(capsys, "--random", "12", "--weeks", "1", "--jobs", "1",
                          "--policy", "deficit", "--json"))
    assert sum(data["band_counts"].values()) == 12
    assert set(data["band_counts"]) <= set(rs.BANDS)
    assert set(data["bands"]) == {b for b, n in data["band_counts"].items() if n}
    for band, per_policy in data["bands"].items():
        assert set(per_policy["deficit"]) == set(rs.METRICS)
        assert set(per_policy["deficit"]["stall"]) == {"mean", "p10", "p50", "p90"}
    assert set(data["results"]["deficit"]) == set(rs.METRICS)  # overall stays
    table = cli(capsys, "--random", "12", "--weeks", "1", "--jobs", "1", "--policy", "deficit")
    for band in data["bands"]:
        assert f"band {band}" in table
    assert "overall" in table


# --- unavoidable stall ----------------------------------------------------------


def test_stall_unavoidable_is_a_third_when_demand_is_one_and_a_half_times_capacity():
    # One pro (capacity 1), one week, demand 1.5: 0.5 cannot be served by any rule.
    s = scn([rs.account("pro", 0)], [1.5 / W] * W)
    m = sim(s, "earliest_reset", share=1.0)["metrics"]
    assert m["stall_unavoidable"] == pytest.approx(100.0 / 3.0)
    assert m["stall"] == pytest.approx(100.0 / 3.0)
    assert m["stall_excess"] == pytest.approx(0.0, abs=1e-9)


def test_stall_unavoidable_is_summed_per_week_and_zero_below_capacity():
    # week 1: 1.5 (0.5 over), week 2: 0.5 (nothing over, no carry): 0.5 / 2.0
    s = scn([rs.account("pro", 0)], [1.5 / W] * W + [0.5 / W] * W)
    m = sim(s, "earliest_reset", share=1.0)["metrics"]
    assert m["stall_unavoidable"] == pytest.approx(25.0)
    low = sim(scn([rs.account("pro", 0)], [0.8 / W] * W), "earliest_reset", share=1.0)["metrics"]
    assert low["stall_unavoidable"] == 0.0


def test_stall_unavoidable_counts_only_enabled_accounts_and_skips_the_warmup():
    # B is disabled before week 2 starts: week 2 has the capacity of A only.
    accounts = [rs.account("pro", 0), rs.account("pro", 0, end=W // 2)]
    s = scn(accounts, [0.0] * W + [1.5 / W] * W, warm=1)
    m = sim(s, "earliest_reset", share=1.0)["metrics"]
    assert m["stall_unavoidable"] == pytest.approx(100.0 / 3.0)
    both = scn([rs.account("pro", 0), rs.account("pro", 0)], [0.0] * W + [1.5 / W] * W, warm=1)
    assert sim(both, "earliest_reset", share=1.0)["metrics"]["stall_unavoidable"] == 0.0


# --- policy parameters ---------------------------------------------------------------


def test_parse_params_sets_values_and_rejects_unknown_names():
    got = rs.parse_params(["deficit.margin_pts=8", "deficit.dwell_s=900", "pressure.ratio=1.3"])
    assert got == {"deficit": {"margin_pts": 8.0, "dwell_s": 900.0}, "pressure": {"ratio": 1.3}}
    assert rs.parse_params([]) == {}
    for bad in ("deficit.nope=1", "bogus.margin_pts=1", "margin_pts=1", "deficit.margin_pts",
                "deficit.margin_pts=abc", "earliest_reset.x=1", "deficit.margin_pts=nan"):
        with pytest.raises(ValueError):
            rs.parse_params([bad])


@pytest.mark.parametrize("bad", ["deficit.nope=1", "bogus.x=1", "deficit.margin_pts=abc", "noequals"])
def test_cli_unknown_or_bad_param_exits_2(bad):
    with pytest.raises(SystemExit) as exc:
        rs.main(["--random", "2", "--weeks", "1", "--param", bad])
    assert exc.value.code == 2


def test_cli_param_changes_the_policy_and_is_echoed(capsys):
    base = ["--random", "6", "--weeks", "1", "--jobs", "1", "--policy", "deficit", "--json"]
    default = json.loads(cli(capsys, *base))
    wide = json.loads(cli(capsys, *base, "--param", "deficit.margin_pts=1000"))
    assert wide["params"] == {"deficit": {"margin_pts": 1000.0}}
    assert default["params"] == {}
    # a margin that large allows only forced switches: fewer switches
    assert wide["results"]["deficit"]["switches_per_day"]["mean"] < default["results"]["deficit"]["switches_per_day"]["mean"]


def test_param_defaults_are_named_and_complete():
    assert set(rs.PARAM_DEFAULTS) == set(rs.POLICIES)
    assert set(rs.PARAM_DEFAULTS["deficit"]) == {"margin_pts", "dwell_s", "wait_for_idle_s"}
    assert set(rs.PARAM_DEFAULTS["pressure"]) >= {"ratio", "dwell_s", "wait_for_idle_s"}
    assert rs.PARAM_DEFAULTS["deficit"]["wait_for_idle_s"] == 0
    assert rs.PARAM_DEFAULTS["pressure"]["ratio"] == 1.15


# --- the pressure policy -----------------------------------------------------------------


def test_pressure_score_hand_checked():
    # q = 0.12 so can = 0.12 / 5 = 0.024 of the week per hour.
    # A: 50 % left, 10 h to reset: reachable min(0.5, 0.024 * 10 = 0.24) = 0.24,
    #    need 0.24 / 10 = 0.024 per hour, score 0.024 * 168 = 4.032.
    now = 100 * 300
    assert rs.pressure_score(rd(50.0, 10 * HOUR), now) == pytest.approx(4.032)
    # B: nothing used, 100 h to reset: reachable min(1, 2.4) = 1, need 0.01, score 1.68.
    assert rs.pressure_score(rd(0.0, 100 * HOUR), now) == pytest.approx(1.68)
    # a fresh account with a full week to go is exactly on its line: score 1
    assert rs.pressure_score(rd(0.0, 168 * HOUR), now) == pytest.approx(1.0)
    # no 5-hour limit: nothing is unreachable. 50 % left, 20 h: 0.025 per hour, 4.2
    assert rs.pressure_score(rd(50.0, 20 * HOUR, five_share=None), now) == pytest.approx(4.2)


def test_pressure_picks_the_account_near_its_reset_with_quota_left():
    now = 100 * 300
    readings = {0: rd(0.0, 100 * HOUR), 1: rd(50.0, 10 * HOUR)}
    st = rs.POLICIES["pressure"].make_state()
    assert rs.POLICIES["pressure"].choose(readings, None, now, st) == 1


def test_pressure_does_not_count_the_part_that_cannot_be_drained():
    # A: 90 % left, 2 h to reset. Naive need 0.45 per hour (score 75.6) would
    # win, but at 0.024 per hour only 0.048 can be drained: score 4.032.
    # C has no 5-hour limit, 50 % left, 20 h: score 4.2. C outranks A.
    now = 100 * 300
    a, c = rd(10.0, 2 * HOUR), rd(50.0, 20 * HOUR, five_share=None)
    assert rs.pressure_score(a, now) == pytest.approx(4.032)
    st = rs.POLICIES["pressure"].make_state()
    assert rs.POLICIES["pressure"].choose({0: a, 1: c}, None, now, st) == 1


def test_pressure_ratio_dwell_and_forced_switch():
    pol = rs.POLICIES["pressure"]
    now = 100000
    a, c = rd(10.0, 2 * HOUR, now=now), rd(50.0, 20 * HOUR, five_share=None, now=now)  # 4.032 vs 4.2

    def fresh(**kw):
        st = pol.make_state(**kw)
        st.update(last_active=0, since=0)  # active since long ago: dwell has passed
        return st

    assert pol.choose({0: a, 1: c}, 0, now, fresh()) == 0  # 4.2 < 4.032 * 1.15: stay
    assert pol.choose({0: a, 1: c}, 0, now, fresh(ratio=1.02)) == 1  # 4.2 > 4.1126
    young = fresh(ratio=1.02)
    young["since"] = now - 100  # dwell of 1800 s has not passed
    assert pol.choose({0: a, 1: c}, 0, now, young) == 0
    full = rd(100.0, 2 * HOUR, now=now)  # blocked
    assert pol.choose({0: full, 1: c}, 0, now, young) == 1  # forced: at once


def test_pressure_rotates_in_an_end_to_end_run():
    s = rs.preset_scenario("heavy", seed=3, weeks=1)
    res = sim(s, "pressure", share=0.12, factor=1.5)
    assert len(set(seq(res))) > 1


# --- idle gaps: free switches and waiting for a gap ----------------------------------------------


def _gap_case(switch_tick, **kw):
    # account 0 works on ticks 0-9; account 1 takes the first work at switch_tick
    demand = [0.01] * 10 + [0.0] * (switch_tick - 10) + [0.01]
    pol = scripted(lambda t: 0 if t < switch_tick else 1)
    accounts = [rs.account("pro", 1000), rs.account("pro", 1000)]
    return sim(scn(accounts, demand), pol, factor=2.0, **kw)


def test_switch_inside_the_cache_lifetime_is_paid_and_after_it_is_free():
    # last work at tick 9. Gap in ticks = switch_tick - 10. Paid if the gap is
    # under 3600 s = 12 ticks: tick 21 (gap 11) is paid, tick 22 (gap 12) is free.
    paid = _gap_case(21)
    assert paid["trace"][-1][5] == pytest.approx(0.02)
    assert paid["paid_ticks"] == [21] and paid["free_ticks"] == []
    free = _gap_case(22)
    assert free["trace"][-1][5] == pytest.approx(0.01)
    assert free["free_ticks"] == [22] and free["paid_ticks"] == []
    # both are switches
    assert len(paid["switch_ticks"]) == len(free["switch_ticks"]) == 1
    assert free["metrics"]["free_switches_per_day"] > 0 and free["metrics"]["paid_switches_per_day"] == 0
    assert paid["metrics"]["paid_switches_per_day"] > 0 and paid["metrics"]["free_switches_per_day"] == 0
    assert free["metrics"]["switches_per_day"] == pytest.approx(
        free["metrics"]["paid_switches_per_day"] + free["metrics"]["free_switches_per_day"])


def test_cache_ttl_is_an_option_of_the_run():
    short = _gap_case(21, cache_ttl_s=600)  # gap 11 ticks = 3300 s is over 600 s: free
    assert short["free_ticks"] == [21]
    assert short["trace"][-1][5] == pytest.approx(0.01)
    zero = _gap_case(11, cache_ttl_s=0)  # a gap of one tick is still not under 0 s
    assert zero["free_ticks"] == [11]
    assert rs.DEFAULT_CACHE_TTL_S == 3600


def test_the_gap_counts_from_served_work_on_any_account():
    # work on account 0 until tick 9, then account 1 works on ticks 30-31 (gap
    # 20 ticks, over the lifetime: free switch), then back to 0 at tick 33 (gap 1: paid)
    marks = {30: 1, 33: 0}

    def pick(t):
        cur = 0
        for k, v in sorted(marks.items()):
            if t >= k:
                cur = v
        return cur

    demand = [0.01] * 10 + [0.0] * 20 + [0.01, 0.01, 0.0, 0.01]
    accounts = [rs.account("pro", 1000), rs.account("pro", 1000)]
    res = sim(scn(accounts, demand), scripted(pick), factor=2.0)
    assert res["free_ticks"] == [30] and res["paid_ticks"] == [33]


def _deficit_readings(now, a_pct, b_pct):
    # both accounts half way through their week; B is far below A: switch wanted
    half = WEEK_S_ // 2
    return {0: rd(a_pct, half, now=now), 1: rd(b_pct, half, now=now)}


WEEK_S_ = rs.WEEK_S


@pytest.mark.parametrize("name", ["deficit", "pressure"])
def test_wait_for_idle_delays_a_non_forced_switch_until_a_gap_or_the_limit(name):
    pol = rs.POLICIES[name]
    n0 = 1000 * rs.TICK_S

    def state(wait):
        st = pol.make_state(dwell_s=0, wait_for_idle_s=wait)
        st["cache_ttl_s"] = HOUR
        return st

    def readings(now):
        if name == "deficit":
            return _deficit_readings(now, 40.0, 10.0)
        # pressure: A has used much, B little, both 50 % through the week: B has more need
        half = WEEK_S_ // 2
        return {0: rd(95.0, half, now=now), 1: rd(10.0, half, now=now)}

    # off: the switch happens at once
    st = state(0)
    assert pol.choose(readings(n0), 0, n0, st) == 1

    # waiting 2 h: the work is continuous, so the policy keeps account 0 ...
    st = state(2 * HOUR)
    st.update(last_active=0, since=0)
    for k in range(5):
        now = n0 + k * rs.TICK_S
        assert pol.choose(readings(now), 0, now, st) == 0
    # ... until an idle gap longer than the cache lifetime shows up: free switch.
    # previous call at n0 + 4 ticks; next call a gap of 3600 s after its end.
    prev = n0 + 4 * rs.TICK_S
    now = prev + rs.TICK_S + HOUR
    assert now - (2 * HOUR) < n0 + 4 * rs.TICK_S + HOUR  # the wait limit is not what triggers it
    assert pol.choose(readings(now), 0, now, st) == 1

    # a gap shorter than the lifetime does not count
    st = state(4 * HOUR)
    st.update(last_active=0, since=0)
    assert pol.choose(readings(n0), 0, n0, st) == 0
    now = n0 + rs.TICK_S + HOUR - rs.TICK_S  # gap of 3600 - 300 s
    assert pol.choose(readings(now), 0, now, st) == 0

    # the wait is bounded by wait_for_idle_s
    st = state(600)
    st.update(last_active=0, since=0)
    got, active = [], 0
    for k in range(4):
        active = pol.choose(readings(n0 + k * rs.TICK_S), active, n0 + k * rs.TICK_S, st)
        got.append(active)
    assert got == [0, 0, 1, 1]  # want since n0; at n0 + 600 s the wait is over


@pytest.mark.parametrize("name", ["deficit", "pressure"])
def test_wait_for_idle_never_delays_a_forced_switch(name):
    pol = rs.POLICIES[name]
    now = 1000 * rs.TICK_S
    st = pol.make_state(dwell_s=0, wait_for_idle_s=10 * HOUR)
    st["cache_ttl_s"] = HOUR
    half = rs.WEEK_S // 2
    full = rd(100.0, half, now=now)  # blocked at the weekly limit
    other = rd(10.0, half, now=now)
    assert pol.choose({0: full, 1: other}, 0, now, st) == 1
    st = pol.make_state(dwell_s=0, wait_for_idle_s=10 * HOUR)
    st["cache_ttl_s"] = HOUR
    five_full = rd(10.0, half, five_pct=100.0, now=now)  # blocked by its 5-hour window
    assert pol.choose({0: five_full, 1: other}, 0, now, st) == 1


def test_waiting_for_idle_gaps_lowers_paid_switches_in_a_run():
    s = rs.preset_scenario("user_like", seed=1, weeks=2)
    base = rs.simulate(s, "deficit", 0.12)["metrics"]
    wait = rs.simulate(s, "deficit", 0.12, policy_params={"wait_for_idle_s": 6 * HOUR})["metrics"]
    assert wait["paid_switches_per_day"] < base["paid_switches_per_day"]


# --- scale invariance for the new policy and the new fleets -----------------------------------------


@pytest.mark.parametrize("policy", ["pressure", "deficit"])
def test_scale_invariance_with_idle_waiting_and_many_large(policy):
    s = rs.preset_scenario("many_large", seed=2, weeks=1)
    params = {"wait_for_idle_s": 1800}
    a = sim(s, policy, share=0.12, factor=1.5, policy_params=params)
    b = sim(_scaled(s, 7.0), policy, share=0.12, factor=1.5, policy_params=params)
    assert seq(a) == seq(b)
    assert a["paid_ticks"] == b["paid_ticks"] and a["free_ticks"] == b["free_ticks"]
    for key, va in a["metrics"].items():
        vb = b["metrics"][key]
        assert (va is None) == (vb is None), key
        if va is not None:
            assert vb == pytest.approx(va, rel=1e-6, abs=1e-9), key


# --- generator and presets, round 2 -----------------------------------------------------------------------


def test_many_large_preset_and_multi_large_fleets_exist():
    m = rs.preset_scenario("many_large", seed=1, weeks=1)
    assert sorted(a.capacity for a in m.accounts) == [1.0, 1.0, 20.0, 20.0, 20.0]
    assert len({a.offset for a in m.accounts}) == 5
    assert "many_large" in rs.PRESETS
    assert rs.preset_params("many_large")["volume"] == 0.6
    assert 0 < rs.P_MULTI_LARGE < 1
    multi = 0
    for i in range(400):
        p = rs.draw_params(random.Random(f"m:{i}"), weeks=4)
        if [a.plan for a in p["accounts"]].count("max20") >= 2:
            multi += 1
    # at least the explicit probability (other draws can add more of them)
    assert multi >= 0.6 * rs.P_MULTI_LARGE * 400


# --- the sweep -----------------------------------------------------------------------------------------------------


SWEEP = "deficit.margin_pts=2,8;deficit.dwell_s=900,3600"


def _sweep(capsys, jobs="1", extra=(), n="6"):
    return cli(capsys, "--random", n, "--weeks", "1", "--jobs", jobs, "--policy", "deficit",
               "--sweep", SWEEP, "--json", *extra)


def test_parse_sweep_builds_the_grid_in_order():
    pol, grid = rs.parse_sweep("deficit.margin_pts=2,8;deficit.dwell_s=900,3600")
    assert pol == "deficit"
    assert grid == [
        {"margin_pts": 2.0, "dwell_s": 900.0},
        {"margin_pts": 2.0, "dwell_s": 3600.0},
        {"margin_pts": 8.0, "dwell_s": 900.0},
        {"margin_pts": 8.0, "dwell_s": 3600.0},
    ]
    for bad in ("", "deficit.nope=1,2", "deficit.margin_pts=2;pressure.ratio=1.1", "deficit.margin_pts=",
                "deficit.margin_pts=1,x", "deficit.margin_pts=1;deficit.margin_pts=2"):
        with pytest.raises(ValueError):
            rs.parse_sweep(bad)


def test_sweep_split_is_two_disjoint_halves():
    train, hold = rs.sweep_split(7)
    assert set(train).isdisjoint(hold) and sorted([*train, *hold]) == list(range(7))
    assert len(train) == 3 and len(hold) == 4 or len(train) == 4 and len(hold) == 3


def test_sweep_evaluates_train_and_holdout_on_disjoint_scenarios(capsys):
    data = json.loads(_sweep(capsys))["sweep"]
    assert set(data["train_indices"]).isdisjoint(data["holdout_indices"])
    assert sorted([*data["train_indices"], *data["holdout_indices"]]) == list(range(6))
    rows = data["rows"]
    assert len(rows) == 4
    scores = [r["train"]["score"] for r in rows]
    assert scores == sorted(scores)  # ranked by train score
    assert all(r["holdout"] is not None for r in rows)  # 4 <= 5 best
    for key in ("score", "stall_excess", "waste_avoidable", "unevenness", "paid_switches_per_day"):
        assert key in rows[0]["train"] and key in rows[0]["holdout"]
    assert data["defaults"]["train"]["score"] is not None
    assert "formula" in data and "stall_excess" in data["formula"]


def test_sweep_score_is_the_documented_formula_on_the_train_half(capsys):
    data = json.loads(_sweep(capsys))["sweep"]
    row = data["rows"][0]
    idx = data["train_indices"]
    metrics = [
        rs.simulate(rs.generate_scenario(1, i, 1), "deficit", 0.12, policy_params=row["params"])["metrics"]
        for i in idx
    ]
    expect = sum(rs.score_of(m) for m in metrics) / len(metrics)
    assert row["train"]["score"] == pytest.approx(expect, rel=1e-9)
    m = metrics[0]
    by_hand = (rs.SCORE_STALL_EXCESS * m["stall_excess"] + rs.SCORE_AVOIDABLE_WASTE * m["waste_avoidable"]
               + rs.SCORE_UNEVENNESS * m["unevenness"] / rs.SCORE_UNEVENNESS_SCALE
               + rs.SCORE_PAID_SWITCHES * m["paid_switches_per_day"])
    assert rs.score_of(m) == pytest.approx(by_hand)
    assert (rs.SCORE_STALL_EXCESS, rs.SCORE_AVOIDABLE_WASTE, rs.SCORE_UNEVENNESS,
            rs.SCORE_UNEVENNESS_SCALE, rs.SCORE_PAID_SWITCHES) == (1.0, 1.0, 0.5, 10.0, 0.2)


def test_sweep_ranking_is_deterministic_across_jobs_and_text_prints_the_formula(capsys):
    one = _sweep(capsys, "1")
    four = _sweep(capsys, "4")
    assert one == four
    text = cli(capsys, "--random", "6", "--weeks", "1", "--jobs", "1", "--policy", "deficit", "--sweep", SWEEP)
    assert "score = " in text and "paid_switches_per_day" in text
    assert "train" in text and "holdout" in text


def test_sweep_fixed_params_apply_under_the_grid(capsys):
    plain = json.loads(_sweep(capsys))["sweep"]["rows"]
    fixed = json.loads(_sweep(capsys, extra=("--param", "deficit.wait_for_idle_s=7200")))["sweep"]["rows"]
    assert {json.dumps(r["params"], sort_keys=True) for r in plain} == \
           {json.dumps(r["params"], sort_keys=True) for r in fixed}
    assert [r["train"]["score"] for r in plain] != [r["train"]["score"] for r in fixed]


@pytest.mark.parametrize(
    "argv",
    [
        ["--random", "4", "--sweep", "deficit.nope=1,2"],
        ["--random", "4", "--sweep", "deficit.margin_pts=2;pressure.ratio=1.1"],
        ["--random", "4", "--policy", "pressure", "--sweep", "deficit.margin_pts=2,4"],
        ["--preset", "light", "--sweep", "deficit.margin_pts=2,4"],
        ["--random", "1", "--sweep", "deficit.margin_pts=2,4"],
        ["--diagnose-steady", "--sweep", "deficit.margin_pts=2,4"],
    ],
)
def test_bad_sweeps_exit_with_code_2(argv):
    with pytest.raises(SystemExit) as exc:
        rs.main(argv)
    assert exc.value.code == 2


# --- steady-state diagnosis -----------------------------------------------------------------------------------------------


def test_diagnose_steady_prints_one_row_per_account_per_week(capsys):
    argv = ["--diagnose-steady", "--weeks", "3", "--jobs", "1"]
    text = cli(capsys, *argv)
    rows = [ln.split() for ln in text.splitlines() if ln.split()[:1] in (["deficit"], ["pressure"])
            and len(ln.split()) > 2 and ln.split()[1].isdigit()]
    # R1c: two loads (0.3 and 0.9) and four policies; the rows of deficit and
    # pressure are counted for both loads.
    assert len(rows) == 2 * 2 * 3 * 3
    for name in ("deficit", "pressure"):
        mine = [r for r in rows if r[0] == name]
        assert sorted({int(r[1]) for r in mine}) == [1, 2, 3]
        assert all(sum(1 for r in mine if int(r[1]) == w) == 3 * 2 for w in (1, 2, 3))
    data = json.loads(cli(capsys, *argv, "--json"))
    assert data["mode"] == "diagnose_steady"
    assert data["capacities"] == [1.0, 5.0, 20.0] and data["load"] == 0.3
    assert len(data["rows"]) == 4 * 2 * 3 * 3
    row = data["rows"][0]
    assert set(row) >= {"policy", "week", "account", "capacity", "reset_pct", "end_used_pct",
                        "end_deficit_pct", "work_share"}
    for name in ("deficit", "pressure"):
        for w in (1, 2, 3):
            for load in (0.3, 0.9):
                shares = [r["work_share"] for r in data["rows"]
                          if r["policy"] == name and r["week"] == w and r["load"] == load]
                assert sum(shares) == pytest.approx(100.0, abs=1e-6)


def test_diagnose_steady_reset_percent_matches_the_simulation(capsys):
    data = json.loads(cli(capsys, "--diagnose-steady", "--weeks", "2", "--jobs", "1", "--json"))
    scn_ = rs.steady_scenario(2)
    res = rs.simulate(scn_, "deficit", rs.DEFAULT_FIVE_HOUR_SHARE)
    by = {}
    for acct, tick, pct, _u, _a in res["resets"]:
        by.setdefault(acct, []).append(pct)
    for r in data["rows"]:
        if r["policy"] == "deficit" and r["load"] == 0.3:
            assert r["reset_pct"] == pytest.approx(by[r["account"]][r["week"] - 1], rel=1e-6)
    # constant load 0.3 of capacity and no noise
    cap = sum(a.capacity for a in scn_.accounts)
    assert len(set(scn_.demand)) == 1 and sum(scn_.demand[:W]) == pytest.approx(0.3 * cap)


def test_diagnose_steady_is_deterministic_across_jobs(capsys):
    a = cli(capsys, "--diagnose-steady", "--weeks", "2", "--jobs", "1", "--json")
    b = cli(capsys, "--diagnose-steady", "--weeks", "2", "--jobs", "4", "--json")
    assert a == b


# --- the whole CLI with the new options --------------------------------------------------------------------------------------


def test_jobs_1_equals_jobs_4_with_params_ttl_and_pressure(capsys):
    base = ["--random", "10", "--weeks", "1", "--seed", "4", "--policy", "all",
            "--param", "pressure.ratio=1.3", "--param", "deficit.wait_for_idle_s=1800", "--cache-ttl-s", "1800"]
    one = cli(capsys, *base, "--jobs", "1")
    four = cli(capsys, *base, "--jobs", "4")
    assert one == four
    assert cli(capsys, *base, "--jobs", "1", "--json") == cli(capsys, *base, "--jobs", "4", "--json")
    assert "pressure mean" in one


def test_cache_ttl_option_changes_paid_and_free_switches(capsys):
    base = ["--preset", "user_like", "--weeks", "2", "--jobs", "1", "--policy", "deficit", "--json"]
    never = json.loads(cli(capsys, *base, "--cache-ttl-s", "0"))["results"]["user_like"]["deficit"]
    default = json.loads(cli(capsys, *base))["results"]["user_like"]["deficit"]
    assert never["paid_switches_per_day"] == 0.0
    assert default["paid_switches_per_day"] > 0.0
    assert never["switches_per_day"] == pytest.approx(default["switches_per_day"], rel=1.0)
    with pytest.raises(SystemExit) as exc:
        rs.main(["--random", "2", "--cache-ttl-s", "-1"])
    assert exc.value.code == 2


# ==========================================================================
# Round 3 (task R1c): lowest_pct, scaled_line, real idle periods, belief of
# the 5-hour share, sensitivity, seeds. Written before the code.
# ==========================================================================

NEW_POLICIES = ["lowest_pct", "scaled_line"]
T0 = 1000 * rs.TICK_S


def rdc(pct, remaining_s, cap=1.0, now=T0, five_pct=0.0, five_share=0.12):
    """A reading of an account with capacity ``cap`` as the policy sees it."""
    return rs.Reading(pct, five_pct, now + remaining_s, cap, now, five_share)


def scaled(**kw):
    """(policy, state) of scaled_line with the guard off unless asked."""
    params = {"dwell_s": 0, "guard": 0.0, **kw}
    pol = rs.POLICIES["scaled_line"]
    st = pol.make_state(**params)
    st["cache_ttl_s"] = HOUR
    return pol, st


def feed(pol, st, steps, active=0):
    """Call the policy for each (now, {id: reading}); return the last f."""
    for now, readings in steps:
        pol.choose(readings, active, now, st)
    return st["f"]


def two_steps(rise_pct, after_s=24 * HOUR, caps=(1.0, 1.0)):
    """Baseline at T0 with all at 0, then account 0 at ``rise_pct`` after ``after_s``."""
    t1 = T0 + after_s
    return [
        (T0, {i: rdc(0.0, 100 * HOUR, c, now=T0) for i, c in enumerate(caps)}),
        (t1, {i: rdc(rise_pct if i == 0 else 0.0, 100 * HOUR, c, now=t1) for i, c in enumerate(caps)}),
    ]


def test_new_policies_are_registered_with_their_named_parameters():
    assert set(rs.POLICIES) >= {"threshold_best", "earliest_reset", "deficit", "pressure", *NEW_POLICIES}
    assert set(rs.PARAM_DEFAULTS["lowest_pct"]) == {"margin_pts", "dwell_s", "wait_for_idle_s"}
    d = rs.PARAM_DEFAULTS["scaled_line"]
    assert set(d) >= {"margin_pts", "dwell_s", "wait_for_idle_s", "f_min", "f_boost", "guard",
                      "guard_hours", "guard_f"}
    assert (d["f_min"], d["f_boost"], d["guard"], d["guard_hours"], d["guard_f"]) == (0.1, 1.1, 1.0, 24.0, 0.9)
    assert rs.PARAM_DEFAULTS["lowest_pct"]["margin_pts"] == rs.PARAM_DEFAULTS["deficit"]["margin_pts"]
    assert rs.parse_params(["scaled_line.f_boost=1.3"]) == {"scaled_line": {"f_boost": 1.3}}


# --- lowest_pct ----------------------------------------------------------------


def test_lowest_pct_picks_the_lowest_percent_and_ignores_the_reset_times():
    pol = rs.POLICIES["lowest_pct"]
    st = pol.make_state(dwell_s=0)
    # B resets in 1 h (deficit would love it) but A has the lower percent used
    readings = {0: rdc(10.0, 100 * HOUR), 1: rdc(30.0, 1 * HOUR), 2: rdc(100.0, 50 * HOUR)}
    assert pol.choose(readings, None, T0, st) == 0
    # blocked accounts are never picked, even with the lowest percent
    readings = {0: rdc(5.0, 50 * HOUR, five_pct=100.0), 1: rdc(30.0, 1 * HOUR), 2: rdc(40.0, 5 * HOUR)}
    assert pol.choose(readings, None, T0, pol.make_state()) == 1


def test_lowest_pct_sequence_hand_checked():
    # A is a pro (capacity 1), B has capacity 2; 0.02 per tick is 2 points a
    # tick on A and 1 point on B. Margin 4, dwell 0. Tie at 0: A. A reads 2k at
    # tick k; at tick 3 it reads 6 > 0 + 4: B (ticks 3..13, B reads k - 3).
    # At tick 14 B reads 11 and A reads 6: 6 + 4 < 11: A. A reads 6 + 2j at
    # tick 14 + j and beats B (11) by more than 4 when 6 + 2j > 15: j = 5, tick 19: B.
    accounts = [rs.account("pro", 0), rs.account("custom", 0, capacity=2.0)]
    res = sim(scn(accounts, [0.02] * 20), "lowest_pct", policy_params={"dwell_s": 0})
    assert seq(res) == [0] * 3 + [1] * 11 + [0] * 5 + [1]


@pytest.mark.parametrize("name", NEW_POLICIES)
def test_new_policy_firmness_margin_dwell_and_forced_switch(name):
    extra = {"f_min": 1.0, "guard": 0.0} if name == "scaled_line" else {}
    pol = rs.POLICIES[name]
    half = rs.WEEK_S // 2
    far = {0: rdc(40.0, half), 1: rdc(10.0, half)}
    young = pol.make_state(dwell_s=1800, **extra)
    young.update(last_active=0, since=T0 - 100)
    assert pol.choose(far, 0, T0, young) == 0  # dwell not over
    old = pol.make_state(dwell_s=1800, **extra)
    old.update(last_active=0, since=0)
    assert pol.choose(far, 0, T0, old) == 1  # dwell over, gap 30 > margin 4
    close = {0: rdc(14.0, half), 1: rdc(10.0, half)}
    keep = pol.make_state(dwell_s=0, margin_pts=4.0, **extra)
    keep.update(last_active=0, since=0)
    assert pol.choose(close, 0, T0, keep) == 0  # 4 is not more than the margin
    full = {0: rdc(100.0, half), 1: rdc(10.0, half)}
    assert pol.choose(full, 0, T0, young) == 1  # forced: at once
    five = {0: rdc(10.0, half, five_pct=100.0), 1: rdc(10.0, half)}
    assert pol.choose(five, 0, T0, young) == 1


@pytest.mark.parametrize("name", NEW_POLICIES)
def test_new_policy_waits_for_an_idle_gap_but_never_for_a_forced_switch(name):
    extra = {"f_min": 1.0, "guard": 0.0} if name == "scaled_line" else {}
    pol = rs.POLICIES[name]
    half = rs.WEEK_S // 2

    def state(wait):
        st = pol.make_state(dwell_s=0, wait_for_idle_s=wait, **extra)
        st["cache_ttl_s"] = HOUR
        st.update(last_active=0, since=0)
        return st

    def readings(now):
        return {0: rdc(40.0, half, now=now), 1: rdc(10.0, half, now=now)}

    assert pol.choose(readings(T0), 0, T0, state(0)) == 1  # off: at once
    st = state(2 * HOUR)
    for k in range(5):  # continuous work: keep account 0
        now = T0 + k * rs.TICK_S
        assert pol.choose(readings(now), 0, now, st) == 0
    now = T0 + 4 * rs.TICK_S + rs.TICK_S + HOUR  # a gap of one cache lifetime: free switch
    assert pol.choose(readings(now), 0, now, st) == 1
    st = state(600)  # bounded by the wait limit
    got, active = [], 0
    for k in range(4):
        now = T0 + k * rs.TICK_S
        active = pol.choose(readings(now), active, now, st)
        got.append(active)
    assert got == [0, 0, 1, 1]
    full = {0: rdc(100.0, half), 1: rdc(10.0, half)}
    assert pol.choose(full, 0, T0, state(10 * HOUR)) == 1


# --- scaled_line: f ----------------------------------------------------------------


def test_f_is_the_observed_consumption_over_the_fleet_capacity_of_the_span():
    # Two accounts of capacity 1; account 0 rises 20 points in 24 h: 0.2 units.
    # The fleet capacity of 24 h is 2 * 24 / 168 = 0.285714: f = 0.7 (boost 1).
    pol, st = scaled(f_boost=1.0)
    assert feed(pol, st, two_steps(20.0)) == pytest.approx(0.7)
    pol, st = scaled()  # default boost 1.1
    assert feed(pol, st, two_steps(20.0)) == pytest.approx(0.77)
    # capacity weights the rise: 10 points of a capacity 5 account is 0.5 unit
    pol, st = scaled(f_boost=1.0)
    assert feed(pol, st, two_steps(10.0, caps=(5.0, 1.0))) == pytest.approx(0.5 / (6.0 * 24 / 168))


def test_f_uses_the_elapsed_span_with_a_24_hour_floor_and_clips():
    pol, st = scaled(f_boost=1.0)
    # 5 points after 6 h: the span is floored to 24 h: 0.05 / 0.285714 = 0.175
    assert feed(pol, st, two_steps(5.0, after_s=6 * HOUR)) == pytest.approx(0.175)
    pol, st = scaled(f_boost=1.0)
    assert feed(pol, st, two_steps(1.0)) == pytest.approx(0.1)  # 0.0350 < f_min: clip at 0.1
    pol, st = scaled(f_boost=1.0, f_min=0.2)
    assert feed(pol, st, two_steps(1.0)) == pytest.approx(0.2)
    pol, st = scaled(f_boost=1.0)
    assert feed(pol, st, two_steps(90.0)) == 1.0  # 0.9 / 0.2857 = 3.15: clip at 1
    # a later call keeps the same consumption over a longer span: 25 h
    pol, st = scaled(f_boost=1.0)
    steps = two_steps(20.0)
    t2 = T0 + 25 * HOUR
    steps.append((t2, {0: rdc(20.0, 100 * HOUR, now=t2), 1: rdc(0.0, 100 * HOUR, now=t2)}))
    assert feed(pol, st, steps) == pytest.approx(0.2 / (2.0 * 25 / 168))


def test_f_forgets_consumption_older_than_seven_days_and_ignores_resets():
    pol, st = scaled(f_boost=1.0)
    steps = two_steps(20.0)
    t2 = T0 + 9 * 24 * HOUR  # 8 days after the rise: out of the trailing 7 days
    steps.append((t2, {0: rdc(60.0, 100 * HOUR, now=t2), 1: rdc(0.0, 100 * HOUR, now=t2)}))
    # only the 40 points (0.4 units) of the last call are in the window of 7 days
    assert feed(pol, st, steps) == pytest.approx(0.4 / 2.0)
    pol, st = scaled(f_boost=1.0)
    steps = two_steps(20.0)
    t2, t3 = T0 + 25 * HOUR, T0 + 26 * HOUR
    steps.append((t2, {0: rdc(5.0, 100 * HOUR, now=t2), 1: rdc(0.0, 100 * HOUR, now=t2)}))  # a reset
    steps.append((t3, {0: rdc(15.0, 100 * HOUR, now=t3), 1: rdc(0.0, 100 * HOUR, now=t3)}))
    # 0.2 before, nothing for the drop to 5, 0.1 after: 0.3 over a 26 h span
    assert feed(pol, st, steps) == pytest.approx(0.3 / (2.0 * 26 / 168))


def test_f_does_not_count_a_stale_reading_twice():
    pol, st = scaled(f_boost=1.0)
    steps = two_steps(20.0)
    t2 = T0 + 25 * HOUR
    stale = rs.Reading(40.0, 0.0, T0 + 100 * HOUR, 1.0, T0 + 24 * HOUR, 0.12)  # as of before t1
    steps.append((t2, {0: stale, 1: rdc(0.0, 100 * HOUR, now=t2)}))
    assert feed(pol, st, steps) == pytest.approx(0.2 / (2.0 * 25 / 168))


def _wrapped_run(scenario, **params):
    """Run scaled_line through simulate and log (tick, f) at each call."""
    log = []

    def choose(readings, active, now, state):
        res = rs.scaled_line_choose(readings, active, now, state)
        log.append((now // rs.TICK_S, state["f"]))
        return res

    pol = rs.Policy("spy", choose, rs.POLICIES["scaled_line"].make_state)
    rs.simulate(scenario, pol, 0.12, policy_params=params)
    return log


def _step_scenario(first, second, switch_week=2, weeks=5):
    accounts = [rs.account("pro", 0), rs.account("pro", 672), rs.account("pro", 1344)]
    cap = 3.0
    demand = []
    for w in range(weeks):
        demand += [(first if w < switch_week else second) * cap / W] * W
    return scn(accounts, demand)


def test_f_follows_a_tenfold_rise_to_the_clip_within_seven_days():
    log = _wrapped_run(_step_scenario(0.1, 1.0), guard=0.0)
    step = 2 * W
    before = [f for t, f in log if step - 100 <= t < step]
    after = {t: f for t, f in log}
    assert max(before) < 0.2
    assert after[step + 7 * rs.TICKS_PER_DAY] == 1.0
    assert max(f for t, f in log if t < step + 7 * rs.TICKS_PER_DAY) <= 1.0


def test_f_decays_after_a_tenfold_fall():
    log = _wrapped_run(_step_scenario(1.0, 0.1), guard=0.0)
    step = 2 * W
    f = {t: v for t, v in log}
    assert f[step - 1] >= 0.99  # near the clip before
    day3 = f[step + 3 * rs.TICKS_PER_DAY]
    day7 = f[step + 7 * rs.TICKS_PER_DAY]
    assert day7 < day3 < f[step - 1]
    assert day7 < 0.2  # 1.1 * 0.1 = 0.11 plus the bursts of the 5-hour caps


# --- scaled_line: the choice -------------------------------------------------------


def test_scaled_line_sequence_hand_checked():
    # f is fixed at 0.5 (f_min 0.5, boost 0). A resets at tick 2016, B at 1008.
    # deficit_A = 0.5 * 100 * t / 2016 - used, deficit_B = 0.5 * 100 * (0.5 + t / 2016) - used.
    # 1 point per served tick, margin 4.5, dwell 0. B first (25 against 0). B
    # reads k at tick k: A - B = k - 25, above 4.5 at k = 30: A (ticks 30..39,
    # A reads j, B stays at 30: B - A = j - 5 > 4.5 at j = 10): B at tick 40.
    accounts = [rs.account("pro", 0), rs.account("pro", 1008)]
    res = sim(scn(accounts, [0.01] * 41), "scaled_line",
              policy_params={"f_min": 0.5, "f_boost": 0.0, "margin_pts": 4.5, "dwell_s": 0, "guard": 0.0})
    assert seq(res) == [1] * 30 + [0] * 10 + [1]


@pytest.mark.parametrize("name", ["user_like", "heavy", "mixed_sizes", "spiky"])
def test_scaled_line_with_f_forced_to_one_makes_the_same_choices_as_deficit(name):
    s = rs.preset_scenario(name, seed=3, weeks=1)
    a = sim(s, "deficit", share=0.12, factor=1.5)
    b = sim(s, "scaled_line", share=0.12, factor=1.5, policy_params={"f_min": 1.0, "guard": 0.0})
    assert seq(a) == seq(b)
    assert len(set(seq(a))) > 1
    assert a["metrics"]["unevenness"] == pytest.approx(b["metrics"]["unevenness"])


def _guard_case(guard=1.0, extra_cap=20.0, **kw):
    """A near its reset with 15 % left, B far ahead on the line, C and D."""
    pol, st = scaled(f_boost=1.0, guard=guard, **kw)
    caps = {0: 1.0, 1: 1.0, 2: 1.0, 3: extra_cap}
    t1 = T0 + 24 * HOUR

    def at(now, pcts):
        remaining = {0: 10 * HOUR, 1: 100 * HOUR, 2: 150 * HOUR, 3: 160 * HOUR}
        return {i: rdc(pcts[i], remaining[i], caps[i], now=now) for i in caps}

    first = at(T0, {0: 85.0, 1: 0.0, 2: 0.0, 3: 0.0})
    second = at(t1, {0: 85.0, 1: 0.0, 2: 60.0, 3: 0.0})  # C used 0.6 units in 24 h
    pol.choose(first, None, T0, st)
    choice = pol.choose(second, 2, t1, st)
    return choice, st


def test_loss_guard_picks_the_near_reset_account_only_when_f_is_high():
    # fleet capacity 3 (account 3 absent): f = 0.6 / (3 * 24 / 168) = 1.4 -> 1.
    # A: 15 % left, 10 h to reset, reachable min(0.15, 0.024 * 10) = 0.15 unit;
    # the forecast is 0.6 / 24 * 10 = 0.25 unit >= 0.15, reset within 24 h.
    # Without the guard the deficit rule prefers B (40.5 against 94 - 85 = 9).
    choice, st = _guard_case(extra_cap=0.0001)
    assert st["f"] == 1.0 and choice == 0
    choice, _ = _guard_case(guard=0.0, extra_cap=0.0001)
    assert choice == 1


def test_loss_guard_is_blocked_by_a_low_f_and_opens_with_a_lower_guard_f():
    # account 3 has capacity 20: f = 0.6 / (23 * 24 / 168) = 0.18 < 0.9
    choice, st = _guard_case()
    assert st["f"] < 0.9 and choice != 0
    choice, _ = _guard_case(guard_f=0.1)
    assert choice == 0


def test_loss_guard_needs_the_reset_within_guard_hours_and_a_forecast_that_covers_it():
    choice, _ = _guard_case(extra_cap=0.0001, guard_hours=5.0)  # reset in 10 h > 5 h
    assert choice == 1
    # a trailing demand too small to consume the reachable quota: no priority.
    pol, st = scaled(f_boost=1.0, guard=1.0, f_min=1.0)  # f = 1 by force
    t1 = T0 + 24 * HOUR
    caps = {0: 1.0, 1: 1.0, 2: 1.0}

    def at(now, pcts):
        remaining = {0: 10 * HOUR, 1: 100 * HOUR, 2: 150 * HOUR}
        return {i: rdc(pcts[i], remaining[i], caps[i], now=now) for i in caps}

    pol.choose(at(T0, {0: 85.0, 1: 0.0, 2: 0.0}), None, T0, st)
    # 3 points of use: forecast 0.03 / 24 * 10 = 0.0125 < reachable 0.15
    assert pol.choose(at(t1, {0: 85.0, 1: 0.0, 2: 3.0}), 2, t1, st) == 1


def test_loss_guard_takes_the_earliest_reset_first():
    pol, st = scaled(f_boost=1.0, guard=1.0)
    t1 = T0 + 24 * HOUR
    caps = {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.0001}
    remaining = {0: 12 * HOUR, 1: 6 * HOUR, 2: 150 * HOUR, 3: 160 * HOUR}

    def at(now, pcts):
        return {i: rdc(pcts[i], remaining[i], caps[i], now=now) for i in caps}

    # A (12 h, 80 %) has the larger deficit; B (6 h, 85 %) resets first and wins the guard
    pol.choose(at(T0, {0: 80.0, 1: 85.0, 2: 0.0, 3: 0.0}), None, T0, st)
    assert pol.choose(at(t1, {0: 80.0, 1: 85.0, 2: 90.0, 3: 0.0}), 2, t1, st) == 1


# --- scaled_line: readings only -----------------------------------------------------


def test_the_policy_gets_only_readings_and_never_reads_the_truth(monkeypatch):
    inside = {"on": False, "reads": 0}
    truth = rs.AccountState

    class Spy(truth):
        __slots__ = ()

        def __getattribute__(self, name):
            if inside["on"] and name in ("used", "win_used", "cap", "five_cap", "headroom"):
                inside["reads"] += 1
            return truth.__getattribute__(self, name)

    seen = []

    def choose(readings, active, now, state):
        inside["on"] = True
        try:
            seen.append({type(v) for v in readings.values()})
            return rs.scaled_line_choose(readings, active, now, state)
        finally:
            inside["on"] = False

    monkeypatch.setattr(rs, "AccountState", Spy)
    pol = rs.Policy("spy", choose, rs.POLICIES["scaled_line"].make_state)
    s = rs.preset_scenario("user_like", seed=1, weeks=1)
    rs.simulate(scn(s.accounts, s.demand[:W]), pol, 0.12)
    assert seen and all(kinds == {rs.Reading} for kinds in seen)
    assert inside["reads"] == 0


def test_scaled_line_state_is_per_run_and_the_input_is_not_mutated():
    pol = rs.POLICIES["scaled_line"]
    a, b = pol.make_state(), pol.make_state()
    pol.choose({0: rdc(0.0, 100 * HOUR)}, None, T0, a)
    assert b.get("hist", []) == [] and a is not b
    readings = {0: rdc(10.0, 100 * HOUR), 1: rdc(0.0, 50 * HOUR)}
    frozen = rs.MappingProxyType(dict(readings))
    pol.choose(frozen, 0, T0, pol.make_state())  # a read-only mapping works


# --- steady state: the round 2 finding -----------------------------------------------


def _spread_by_week(res, n):
    by = {}
    for acct, tick, pct, *_ in res["resets"]:
        by.setdefault((tick - 1) // W + 1, []).append(pct)
    return {w: max(v) - min(v) for w, v in by.items() if len(v) == n}


def test_scaled_line_evens_the_weekly_percent_where_deficit_does_not():
    s = rs.steady_scenario(8)
    spread = {p: _spread_by_week(rs.simulate(s, p, 0.12), 3) for p in ("scaled_line", "deficit", "lowest_pct")}
    for week in range(rs.STEADY_SKIP_WEEKS + 1, 9):
        assert spread["scaled_line"][week] < 10.0, (week, spread["scaled_line"])
        assert spread["deficit"][week] > 30.0, (week, spread["deficit"])


# --- scale invariance and determinism of the new policies ----------------------------------------------------


@pytest.mark.parametrize("policy", NEW_POLICIES)
@pytest.mark.parametrize("name", ["user_like", "mixed_sizes", "many_large"])
def test_new_policies_are_scale_invariant(policy, name):
    s = rs.preset_scenario(name, seed=2, weeks=1)
    params = {"wait_for_idle_s": 1800}
    a = sim(s, policy, share=0.12, factor=1.5, policy_params=params)
    b = sim(_scaled(s, 7.0), policy, share=0.12, factor=1.5, policy_params=params)
    assert seq(a) == seq(b)
    assert len(set(seq(a))) > 1
    for key, va in a["metrics"].items():
        vb = b["metrics"][key]
        assert (va is None) == (vb is None), key
        if va is not None:
            assert vb == pytest.approx(va, rel=1e-6, abs=1e-9), key


@pytest.mark.parametrize("policy", NEW_POLICIES)
def test_new_policies_are_deterministic_and_conserve_demand(policy):
    s = rs.generate_scenario(3, 5, weeks=1)
    a = sim(s, policy, share=0.12, factor=1.5)
    b = sim(s, policy, share=0.12, factor=1.5)
    assert seq(a) == seq(b) and a["metrics"] == b["metrics"]
    t = a["totals"]
    assert t["served"] + t["unserved"] == pytest.approx(t["demand"])


def test_jobs_1_equals_jobs_4_for_the_new_policies(capsys):
    base = ["--random", "8", "--weeks", "1", "--seed", "6", "--policy", "scaled_line",
            "--param", "scaled_line.f_boost=1.3", "--param", "scaled_line.wait_for_idle_s=1800"]
    assert cli(capsys, *base, "--jobs", "1") == cli(capsys, *base, "--jobs", "4")
    lows = ["--random", "8", "--weeks", "1", "--seed", "6", "--policy", "lowest_pct", "--json"]
    assert cli(capsys, *lows, "--jobs", "1") == cli(capsys, *lows, "--jobs", "4")


# --- generator: real idle periods ---------------------------------------------------------


def test_idle_spec_is_drawn_inside_its_ranges_and_covers_them():
    R = rs.RANGES
    assert R["idle_block_hours"] == (0.0, 14.0) and R["idle_days_per_week"] == (0, 3)
    probs, hours, days = [], [], set()
    for i in range(500):
        p = rs.draw_params(random.Random(f"idle:{i}"), weeks=4)
        spec = p["idle_spec"]
        assert R["idle_block_prob"][0] <= spec["block_prob"] <= R["idle_block_prob"][1]
        assert R["idle_block_hours"][0] <= spec["block_hours"] <= R["idle_block_hours"][1]
        assert spec["days_per_week"] in range(R["idle_days_per_week"][0], R["idle_days_per_week"][1] + 1)
        probs.append(spec["block_prob"])
        hours.append(spec["block_hours"])
        days.add(spec["days_per_week"])
        total_weeks = rs.WARMUP_WEEKS + 4
        run = total_weeks * W
        block = round(spec["block_hours"] * rs.TICKS_PER_HOUR)
        full = [iv for iv in p["idle"] if iv[1] == rs.TICKS_PER_DAY and iv[0] % rs.TICKS_PER_DAY == 0]
        assert len(full) >= spec["days_per_week"] * total_weeks  # a block of 24 h cannot occur
        for start, length in p["idle"]:
            assert 0 <= start < run and 0 < length <= rs.TICKS_PER_DAY
            assert length == rs.TICKS_PER_DAY or length <= block
    assert min(probs) < 0.1 and max(probs) > 0.9
    assert min(hours) < 1.0 and max(hours) > 13.0
    assert days == {0, 1, 2, 3}


def test_idle_blocks_have_exactly_zero_demand_even_after_bursts_and_regime():
    checked = nonempty = 0
    for i in range(60):
        s = rs.generate_scenario(7, i, weeks=2)
        idle = s.meta["idle"]
        for start, length in idle:
            assert all(d == 0.0 for d in s.demand[start : start + length])
        if idle:
            nonempty += 1
        checked += 1
    assert checked == 60 and nonempty > 30


def test_idle_days_per_week_are_whole_days_of_zero_demand():
    seen = 0
    for i in range(120):
        s = rs.generate_scenario(8, i, weeks=2)
        if s.meta["gaps"]:
            continue  # a gap of 12 to 72 hours can also empty a day
        want = s.meta["idle_spec"]["days_per_week"]
        for w in range(3):
            days = [sum(s.demand[(w * 7 + d) * 288 : (w * 7 + d + 1) * 288]) for d in range(7)]
            assert sum(1 for x in days if x == 0.0) == want, (i, w)
            seen += 1
    assert seen > 50


def test_the_week_volume_is_kept_when_idle_periods_are_added():
    s = rs.generate_scenario(9, 4, weeks=2)
    assert s.meta["idle"]
    # the idle ticks hold no demand, the rest of the week holds all of it
    assert all(sum(s.demand[w * W : (w + 1) * W]) > 0 for w in range(3))


def test_user_like_preset_has_a_midday_dip_on_half_of_the_days_and_one_idle_day_a_week():
    spec = rs.preset_params("user_like")["idle_spec"]
    assert spec["block_prob"] == 0.5 and spec["days_per_week"] == 1
    assert spec["block_anchor_h"] == 10.0 and spec["block_hours"] == 3.0
    dips = non_idle = 0
    for seed in range(1, 6):
        s = rs.preset_scenario("user_like", seed=seed, weeks=4)
        for w in range(5):
            days = []
            for d in range(7):
                base = (w * 7 + d) * 288
                days.append((sum(s.demand[base : base + 288]), s.demand[base + 120 : base + 156]))
            assert sum(1 for tot, _ in days if tot == 0.0) == 1  # one idle day a week
            for tot, block in days:
                if tot > 0.0:
                    non_idle += 1
                    dips += all(x == 0.0 for x in block)
    assert 0.35 < dips / non_idle < 0.65
    # other presets keep no idle period
    assert not rs.preset_scenario("light", seed=1, weeks=1).meta.get("idle")


def test_zero_demand_share_counts_the_measured_ticks_only():
    s = scn([rs.account("pro", 0)], [0.0] * 100 + [0.1] * 50 + [0.0] * 50, warm=0)
    assert rs.zero_demand_share(s) == pytest.approx(75.0)
    w = rs.Scenario([rs.account("pro", 0)], [0.0] * W + [0.1] * (W // 2) + [0.0] * (W // 2), [], 1, {})
    assert rs.zero_demand_share(w) == pytest.approx(50.0)  # the warm-up week is left out


def test_random_output_reports_the_zero_demand_share_per_band(capsys):
    data = json.loads(cli(capsys, "--random", "12", "--weeks", "1", "--jobs", "1",
                          "--policy", "deficit", "--json"))
    z = data["zero_demand_share"]
    assert 0.0 <= z["overall"] <= 100.0
    assert set(z["bands"]) == set(data["bands"])
    assert all(0.0 <= v <= 100.0 for v in z["bands"].values())
    table = cli(capsys, "--random", "12", "--weeks", "1", "--jobs", "1", "--policy", "deficit")
    heads = [ln for ln in table.splitlines() if ln.startswith("# band") or ln.startswith("# overall")]
    assert heads and all("zero-demand ticks" in ln for ln in heads)
    pre = json.loads(cli(capsys, "--preset", "user_like", "--weeks", "1", "--jobs", "1", "--json"))
    assert pre["preset_bands"]["user_like"]["zero_demand_share"] > 0.0


# --- the belief of the 5-hour share ------------------------------------------------------------------------------


def test_belief_scales_the_share_in_the_readings_and_leaves_the_truth():
    seen = []

    def spy(readings, active, now, state):
        seen.append({i: (r.five_share, r.five_pct) for i, r in readings.items()})
        return 0

    pol = rs.Policy("spy", spy, dict)
    accounts = [rs.account("pro", 1000), rs.account("pro", 1000, five_hour_share=None)]
    s = scn(accounts, [0.05] * 4)
    runs = {b: rs.simulate(s, pol, 0.12, belief_q_scale=b, trace=True) for b in (1.0, 0.5, 2.0)}
    # the trace served is the truth: same for any belief (5-hour cap 0.12 on account 0)
    served = {b: [row[3] for row in r["trace"]] for b, r in runs.items()}
    assert served[0.5] == served[1.0] == served[2.0]
    assert sum(served[1.0]) == pytest.approx(0.12 + 0.0)  # 0.05, 0.05, 0.02, then blocked
    n = len(seen) // 3
    one, half, two = seen[:n], seen[n : 2 * n], seen[2 * n :]
    assert one[-1][0][0] == pytest.approx(0.12) and half[-1][0][0] == pytest.approx(0.06)
    assert two[-1][0][0] == pytest.approx(0.24)
    assert one[-1][1][0] is None and half[-1][1][0] is None  # no limit stays no limit
    assert [x[0][1] for x in one] == [x[0][1] for x in half]  # the 5-hour percent is not scaled


def test_belief_changes_pressure_and_scaled_line_guard_but_not_deficit():
    s = rs.preset_scenario("heavy", seed=3, weeks=1)
    base = {p: seq(sim(s, p, share=0.12, factor=1.5)) for p in ("pressure", "deficit", "lowest_pct")}
    low = {p: seq(sim(s, p, share=0.12, factor=1.5, belief_q_scale=0.4)) for p in base}
    assert low["deficit"] == base["deficit"] and low["lowest_pct"] == base["lowest_pct"]
    assert low["pressure"] != base["pressure"]


def test_belief_option_one_equals_no_option_and_a_bad_value_exits_2(capsys):
    base = ["--random", "6", "--weeks", "1", "--jobs", "1", "--policy", "all", "--json"]
    plain = cli(capsys, *base)
    assert cli(capsys, *base, "--belief-q-scale", "1.0") == plain
    other = cli(capsys, *base, "--belief-q-scale", "0.4")
    assert other != plain
    assert json.loads(other)["belief_q_scale"] == 0.4 and json.loads(plain)["belief_q_scale"] == 1.0
    for bad in ("0", "-1", "x"):
        with pytest.raises(SystemExit) as exc:
            rs.main(["--random", "3", "--belief-q-scale", bad])
        assert exc.value.code == 2


# --- sensitivity ------------------------------------------------------------------------------------------------------


def test_sensitivity_prints_nine_blocks_with_every_policy_and_four_columns(capsys):
    text = cli(capsys, "--random", "4", "--weeks", "1", "--jobs", "1", "--sensitivity")
    heads = [ln for ln in text.splitlines() if ln.startswith("# five_hour_share")]
    assert len(heads) == 9
    combos = [(s, b) for s in rs.SENS_SHARES for b in rs.SENS_BELIEFS]
    assert rs.SENS_SHARES == (0.06, 0.12, 0.25) and rs.SENS_BELIEFS == (0.5, 1.0, 2.0)
    for head, (share, belief) in zip(heads, combos):
        assert f"{share:g}" in head and f"belief_q_scale {belief:g}" in head
    for col in ("stall_excess", "waste_avoidable", "unevenness", "paid_switches_per_day"):
        assert text.count(col) == 9
    for name in rs.POLICIES:
        assert sum(1 for ln in text.splitlines() if ln.startswith(name + " ")) == 9
    data = json.loads(cli(capsys, "--random", "4", "--weeks", "1", "--jobs", "1", "--sensitivity", "--json"))
    assert data["mode"] == "sensitivity" and len(data["blocks"]) == 9
    first = data["blocks"][0]
    assert (first["five_hour_share"], first["belief_q_scale"]) == (0.06, 0.5)
    assert set(first["results"]) == set(rs.POLICIES)
    assert set(first["results"]["deficit"]) == set(rs.SENS_COLUMNS)
    shares = {b["five_hour_share"]: b["results"]["deficit"]["stall_excess"] for b in data["blocks"]}
    assert len(set(shares.values())) > 1  # the share is a real input


def test_sensitivity_is_deterministic_across_jobs_and_needs_random(capsys):
    argv = ["--random", "4", "--weeks", "1", "--sensitivity", "--json"]
    assert cli(capsys, *argv, "--jobs", "1") == cli(capsys, *argv, "--jobs", "4")
    for bad in (["--preset", "light", "--sensitivity"], ["--diagnose-steady", "--sensitivity"]):
        with pytest.raises(SystemExit) as exc:
            rs.main(bad)
        assert exc.value.code == 2


# --- seeds -------------------------------------------------------------------------------------------------------------------


def test_parse_seeds_accepts_ranges_and_lists():
    assert rs.parse_seeds("1-5") == [1, 2, 3, 4, 5]
    assert rs.parse_seeds("2,7,9") == [2, 7, 9]
    assert rs.parse_seeds("3") == [3]
    for bad in ("", "5-1", "a", "1-", "1,,2"):
        with pytest.raises(ValueError):
            rs.parse_seeds(bad)


def test_seeds_average_the_preset_over_the_seeds_and_need_a_preset(capsys):
    base = ["--preset", "light", "--weeks", "1", "--jobs", "1", "--policy", "deficit", "--json"]
    per = [json.loads(cli(capsys, *base, "--seed", str(s)))["results"]["light"]["deficit"] for s in (1, 2, 3)]
    avg = json.loads(cli(capsys, *base, "--seeds", "1-3"))
    assert avg["seeds"] == [1, 2, 3]
    got = avg["results"]["light"]["deficit"]
    for key in ("waste", "stall", "unevenness", "switches_per_day"):
        assert got[key] == pytest.approx(sum(p[key] for p in per) / 3, abs=1e-3)
    for bad in (["--random", "3", "--seeds", "1-2"], ["--preset", "light", "--seeds", "5-1"]):
        with pytest.raises(SystemExit) as exc:
            rs.main(bad)
        assert exc.value.code == 2


# --- diagnose-steady, round 3 ---------------------------------------------------------------------------------------


def test_diagnose_steady_runs_four_policies_on_two_loads(capsys):
    assert rs.STEADY_POLICIES == ("deficit", "pressure", "lowest_pct", "scaled_line")
    assert rs.STEADY_LOADS == (0.3, 0.9)
    data = json.loads(cli(capsys, "--diagnose-steady", "--weeks", "3", "--jobs", "1", "--json"))
    assert data["loads"] == [0.3, 0.9] and data["load"] == 0.3
    assert len(data["rows"]) == 4 * 2 * 3 * 3
    assert {(r["policy"], r["load"]) for r in data["rows"]} == {
        (p, l) for p in rs.STEADY_POLICIES for l in rs.STEADY_LOADS
    }
    text = cli(capsys, "--diagnose-steady", "--weeks", "3", "--jobs", "1")
    for name in rs.STEADY_POLICIES:
        assert text.count(f"{name} spread of reset_pct across accounts, by week:") == 2
    assert "load 0.9" in text and "load 0.3" in text
    # the volume 0.9 case is the same fleet at 0.9 of the capacity, no noise
    s9 = rs.steady_scenario(2, load=0.9)
    assert sum(s9.demand[:W]) == pytest.approx(0.9 * 26.0)


# --- R2: the judge (clairvoyant bounds) ------------------------------------------------------------------------------------
#
# bound_upper: earliest deadline first, 5-hour caps and switch cost ignored, several accounts per tick.
# oracle: a receding-horizon clairvoyant policy that obeys every rule of the simulator.


def _bound(accounts, demand, outages=(), warm=0):
    return rs.bound_upper(scn(accounts, demand, outages, warm))


def test_bound_one_account_demand_below_capacity():
    # Capacity 1, resets at tick 100 (inside the run of 200 ticks). Demand 0.4 in all: everything is served,
    # the reset at 100 finds 1.0 - 0.4 = 0.6 unused: waste = 60 percent of that one capacity.
    demand = [0.04] * 10 + [0.0] * 190
    b = _bound([rs.account("pro", 100)], demand)
    assert b["served"] == pytest.approx(100.0)
    assert b["stall_floor"] == pytest.approx(0.0)
    assert b["waste_floor"] == pytest.approx(60.0)


def test_bound_one_account_demand_above_capacity():
    # Demand 1.5 in ticks 0..49: only the capacity 1.0 can be served, 0.5 is lost: stall 0.5 / 1.5.
    demand = [0.03] * 50 + [0.0] * 150
    b = _bound([rs.account("pro", 100)], demand)
    assert b["stall_floor"] == pytest.approx(100.0 * 0.5 / 1.5)
    assert b["served"] == pytest.approx(100.0 * 1.0 / 1.5)
    assert b["waste_floor"] == pytest.approx(0.0)


def test_bound_serves_the_earliest_reset_first_and_avoids_the_loss_of_the_other_order():
    # A (id 0) resets at tick 30, B (id 1) at tick 60, both capacity 1. Demand 0.05 per tick, ticks 0..59 (3.0).
    # Earliest reset first: ticks 0..19 from A (1.0), 20..29 from B (0.5); A resets full. Ticks 30..39 from B (0.5,
    # B full), 40..59 from A (1.0). All 3.0 served and both accounts are empty at their resets: waste 0.
    # The other order (B first) serves 1.0 from B in ticks 0..19, 0.5 from A in 20..29; A resets with 0.5 unused;
    # then B is full, A serves 1.0 in 30..49 and 0.5 is unserved in 50..59.
    a = rs.account("pro", 30, five_hour_share=None)
    b_ = rs.account("pro", 60, five_hour_share=None)
    demand = [0.05] * 60 + [0.0] * 40
    s = scn([a, b_], demand)
    bound = rs.bound_upper(s)
    assert bound["served"] == pytest.approx(100.0)
    assert bound["waste_floor"] == pytest.approx(0.0, abs=1e-6)
    wrong = sim(s, scripted(lambda t: 1 if t < 20 else 0))
    assert wrong["metrics"]["served"] == pytest.approx(100.0 * 2.5 / 3.0)
    assert wrong["metrics"]["waste"] > 0.0


def test_bound_demand_after_a_reset_cannot_use_the_expired_quota():
    # One account, capacity 1, resets at tick 50. Demand 1.5 arrives at ticks 50..59, after the reset: only the
    # fresh week (1.0) is usable. The first week (1.0) expired unused: waste 100 percent of that capacity.
    demand = [0.0] * 50 + [0.15] * 10 + [0.0] * 20
    b = _bound([rs.account("pro", 50)], demand)
    assert b["stall_floor"] == pytest.approx(100.0 * 0.5 / 1.5)
    assert b["waste_floor"] == pytest.approx(100.0)


def test_bound_lets_several_accounts_serve_in_the_same_tick_and_ignores_the_five_hour_cap():
    # One tick of demand 1.5 with two capacity-1 accounts: the relaxation serves it all, a real rule serves only
    # one account's room (1.0) in a tick. The 5-hour share 0.12 would cap one account at 0.12 per window.
    demand = [1.5] + [0.0] * 9
    s = scn([rs.account("pro", 1000), rs.account("pro", 2000)], demand)
    assert rs.bound_upper(s)["stall_floor"] == pytest.approx(0.0)
    real = sim(s, "earliest_reset", share=0.12, factor=1.5)
    assert real["metrics"]["stall"] > 0.0


def test_bound_ignores_the_switch_cost_and_the_outages():
    demand = [0.1] * 20
    s = scn([rs.account("pro", 1000), rs.account("pro", 1001)], demand, outages=[(0, 20)])
    assert rs.bound_upper(s)["served"] == pytest.approx(100.0)


def test_bound_does_not_use_an_account_before_it_starts_or_after_it_ends():
    # B (capacity 1) is enabled for ticks 10 <= t < 20 only. A (capacity 0.5) always. Demand 0.1 per tick for 30
    # ticks (3.0): A gives 0.5, B gives at most 1.0 inside its window (ticks 10..19 serve 1.0 of 1.0 from B plus
    # whatever A has). Total servable = 1.5, so stall = 1.5 / 3.0.
    a = rs.account("custom", 1000, capacity=0.5)
    b = rs.account("pro", 1001, start=10, end=20)
    s = scn([a, b], [0.1] * 30 + [0.0] * 10)
    assert rs.bound_upper(s)["stall_floor"] == pytest.approx(100.0 * 1.5 / 3.0)


def test_bound_counts_the_stall_only_after_the_warm_up():
    # Warm-up of one week. Demand 0.3 per tick in the first 10 ticks (warm-up: 3.0 asked, about 2.0 unserved, not
    # measured) and 0.01 per tick in ticks W+10..W+19 (measured, after the reset at W+5): measured stall is 0.
    demand = [0.3] * 10 + [0.0] * W + [0.01] * 10 + [0.0] * 50
    b = _bound([rs.account("pro", 5)], demand, warm=1)
    assert b["stall_floor"] == pytest.approx(0.0)


# --- bound_upper against every policy and the oracle: a property over random scenarios ------------------------------------

JUDGE_SEED = 11
JUDGE_WEEKS = 1
JUDGE_N = 200


@pytest.fixture(scope="module")
def judged():
    """200 random one-week scenarios: every policy, the oracle and the bound (computed once, 4 workers)."""
    policies = list(rs.POLICIES)
    tasks = [
        ("random", i, JUDGE_SEED, JUDGE_WEEKS, rs.DEFAULT_FIVE_HOUR_SHARE, policies, {}, rs.DEFAULT_CACHE_TTL_S,
         1.0, True)
        for i in range(JUDGE_N)
    ]
    return rs._map(tasks, 4)


def test_bound_serves_at_least_as_much_as_every_policy_and_the_oracle(judged):
    assert len(judged) == JUDGE_N
    for r in judged:
        floor = r["bound"]["stall_floor"]
        for name, m in r["metrics"].items():
            assert floor <= m["stall"] + 1e-6, name
        assert floor <= r["oracle"]["stall"] + 1e-6


def test_oracle_is_at_least_as_good_as_threshold_best_on_unserved_plus_waste_in_90_percent(judged):
    better = 0
    for r in judged:
        o = r["oracle"]["stall"] + r["oracle"]["waste"]
        t = r["metrics"]["threshold_best"]["stall"] + r["metrics"]["threshold_best"]["waste"]
        better += o <= t + 1e-9
    assert better >= 0.9 * JUDGE_N, f"oracle at least as good in {better} of {JUDGE_N}"


def test_gaps_are_computed_as_defined(judged):
    for r in judged[:60]:
        b, o = r["bound"], r["oracle"]
        for name, m in r["metrics"].items():
            assert m["stall_floor"] == pytest.approx(b["stall_floor"])
            assert m["waste_floor"] == pytest.approx(b["waste_floor"])
            assert m["oracle_stall"] == pytest.approx(o["stall"])
            assert m["oracle_waste"] == pytest.approx(o["waste"])
            assert m["stall_gap_upper"] == pytest.approx(m["stall"] - b["stall_floor"])
            assert m["stall_gap_oracle"] == pytest.approx(m["stall"] - o["stall"])
            assert m["waste_gap_upper"] == pytest.approx(m["waste"] - b["waste_floor"])
            assert m["waste_gap_oracle"] == pytest.approx(m["waste"] - o["waste"])
        # the oracle row compares with itself: no gap to the oracle, a gap to the bound
        assert o["stall_gap_oracle"] == pytest.approx(0.0) and o["waste_gap_oracle"] == pytest.approx(0.0)
        assert o["stall_gap_upper"] == pytest.approx(o["stall"] - b["stall_floor"])
        assert o["waste_gap_upper"] == pytest.approx(o["waste"] - b["waste_floor"])


def test_bound_upper_returns_served_and_stall_floor_that_add_to_100():
    b = _bound([rs.account("pro", 100)], [0.03] * 50 + [0.0] * 150)
    assert b["served"] + b["stall_floor"] == pytest.approx(100.0)


# --- the oracle ----------------------------------------------------------------------------------------------------------------


def _oracle_run(s, factor=1.0, share=1.0, **kw):
    return rs.oracle(s, five_hour_share=share, switch_cost_factor=factor, trace=True, **kw)


def test_oracle_defaults_are_named_and_the_oracle_is_not_a_real_policy():
    assert rs.ORACLE_DEFAULTS == {"horizon_h": 12.0, "step_s": 900, "tail_h": 156.0}
    assert "oracle" not in rs.POLICIES and "oracle" not in rs.PARAM_DEFAULTS
    assert rs.parse_params(["oracle.horizon_h=6", "oracle.step_s=1800", "oracle.tail_h=0"]) == {
        "oracle": {"horizon_h": 6.0, "step_s": 1800.0, "tail_h": 0.0}
    }
    with pytest.raises(ValueError):
        rs.parse_params(["oracle.nope=1"])


def test_oracle_hand_checked_drains_the_account_that_expires_first():
    # A (id 0, capacity 1) resets at tick 15. B (id 1, capacity 1) resets after the run. No 5-hour cap, no switch
    # cost. Demand 0.1 per tick for ticks 0..14 (1.5). Best: A for 10 ticks (A is then full, it resets at 15 with
    # nothing lost), then B for 5 ticks. B first would leave 0.5 of A unused at its reset (waste 50 percent).
    a = rs.account("pro", 15, five_hour_share=None)
    b = rs.account("pro", 150, five_hour_share=None)
    s = scn([a, b], [0.1] * 15 + [0.0] * 85)
    res = _oracle_run(s)
    assert seq(res) == [0] * 10 + [1] * 5
    assert res["metrics"]["stall"] == pytest.approx(0.0)
    assert res["metrics"]["waste"] == pytest.approx(0.0, abs=1e-6)
    wrong = sim(s, scripted(lambda t: 1 if t < 10 else 0))
    assert wrong["metrics"]["waste"] == pytest.approx(50.0, abs=1e-6)  # A resets with 0.5 of 1.0 unused


def test_oracle_does_not_switch_without_a_reason_and_pays_the_switch_cost_when_it_must():
    # Both accounts reset at tick 200 (run 250 ticks). Demand 0.01 per tick for 30 ticks.
    big = rs.account("pro", 200, five_hour_share=None)
    other = rs.account("pro", 200, five_hour_share=None)
    quiet = _oracle_run(scn([big, other], [0.01] * 30 + [0.0] * 220), factor=2.0)
    assert seq(quiet) == [0] * 30 and quiet["metrics"]["switches_per_day"] == 0.0
    # A (capacity 0.2) resets at tick 25, B (capacity 1) at tick 200. Using A first loses nothing (A would
    # expire with 0.2 unused) and costs one paid switch of 0.01; B alone would waste 0.2. So: A for the 20
    # ticks that fill it, then B for the last 10. The first tick on B burns 2 x 0.01.
    small = rs.account("custom", 25, five_hour_share=None, capacity=0.2)
    res = _oracle_run(scn([small, other], [0.01] * 30 + [0.0] * 220), factor=2.0)
    assert seq(res) == [0] * 20 + [1] * 10
    consumed = [row[5] for row in res["trace"]]
    assert consumed[20] == pytest.approx(0.02) and consumed[21] == pytest.approx(0.01)
    assert res["paid_ticks"] == [20]


def test_oracle_obeys_the_rules_on_random_scenarios():
    for i in range(6):
        s = rs.generate_scenario(21, i, 1)
        res = rs.oracle(s, five_hour_share=0.12, switch_cost_factor=1.5, trace=True)
        t = res["totals"]
        assert t["served"] + t["unserved"] == pytest.approx(t["demand"])  # conservation
        for tick, active, d, sv, un, consumed in res["trace"]:
            assert sv + un == pytest.approx(d)  # nothing is queued, nothing is invented
            assert consumed >= sv - 1e-12  # burn only adds
            if active is None:
                assert sv == 0.0
        paid = set(res["paid_ticks"])
        for tick, active, d, sv, un, consumed in res["trace"]:
            if tick in paid and un < 1e-12:
                assert consumed == pytest.approx(1.5 * d)  # the switch cost is paid
        # per account and per own week, never above the capacity
        used = {}
        for tick, active, d, sv, un, consumed in res["trace"]:
            if active is None:
                continue
            spec = s.accounts[active]
            first = spec.offset if spec.offset > 0 else W
            wk = 0 if tick < first else 1 + (tick - first) // W
            used[(active, wk)] = used.get((active, wk), 0.0) + consumed
        for (acct, _wk), u in used.items():
            assert u <= s.accounts[acct].capacity * (1 + 1e-9)
        assert rs.oracle(s, five_hour_share=0.12, switch_cost_factor=1.5)["metrics"] == res["metrics"]


def test_the_oracle_sees_the_truth_and_no_other_policy_does():
    seen = {}

    def choose(readings, active, now, state):
        seen["keys"] = set(state)
        return min(readings) if active is None else active

    pol = rs.Policy("spy", choose, dict)
    rs.simulate(scn([rs.account("pro", 100)], [0.01] * 5), pol, 1.0)
    assert "truth" not in seen["keys"]


def test_oracle_options_change_the_plan_and_are_validated(capsys):
    s = rs.generate_scenario(21, 3, 1)
    base = rs.oracle(s, five_hour_share=0.12)["metrics"]
    short = rs.oracle(s, five_hour_share=0.12, policy_params={"horizon_h": 1.0, "step_s": 3600})["metrics"]
    assert set(base) == set(short)  # same shape; the plan can differ
    with pytest.raises(TypeError):
        rs.oracle(s, policy_params={"nope": 1})


def test_oracle_run_time_for_a_five_week_scenario_is_under_two_seconds():
    import time

    worst = 0.0
    for name in ("user_like", "heavy", "many_large"):
        s = rs.preset_scenario(name, seed=1, weeks=4)  # 1 warm-up week + 4 measured weeks
        t0 = time.perf_counter()
        rs.oracle(s, five_hour_share=0.12)
        worst = max(worst, time.perf_counter() - t0)
    assert worst < ORACLE_TIME_LIMIT_S, f"{worst:.2f} s"


ORACLE_TIME_LIMIT_S = 2.0


# --- the CLI with the judge ----------------------------------------------------------------------------------------------------


def test_judge_metrics_are_named_and_the_default_output_does_not_change(capsys):
    assert rs.JUDGE_METRICS == (
        "stall_floor", "waste_floor", "oracle_stall", "oracle_waste",
        "stall_gap_upper", "stall_gap_oracle", "waste_gap_upper", "waste_gap_oracle",
    )
    plain = json.loads(cli(capsys, "--random", "3", "--weeks", "1", "--jobs", "1", "--policy", "deficit", "--json"))
    assert "oracle" not in plain["results"]
    assert set(plain["results"]["deficit"]) == set(rs.METRICS)


def _judge_args(*extra):
    return ["--random", "4", "--weeks", "1", "--jobs", "1", "--policy", "deficit", "--judge", *extra]


def test_judge_adds_an_oracle_row_and_the_gap_columns_to_every_table(capsys):
    table = cli(capsys, *_judge_args())
    header = [ln for ln in table.splitlines() if ln.startswith("policy stat")][0].split()
    assert header[2:] == [*rs.METRICS, *rs.JUDGE_METRICS]
    assert "oracle (ref) mean" in table and "deficit mean" in table
    data = json.loads(cli(capsys, *_judge_args("--json")))
    assert set(data["results"]) == {"deficit", "oracle"}
    assert set(data["results"]["oracle"]) == {*rs.METRICS, *rs.JUDGE_METRICS}
    assert all("oracle" in blk for blk in data["bands"].values())
    assert data["oracle"]["reference"] is True and data["oracle"]["params"] == rs.ORACLE_DEFAULTS
    pre = cli(capsys, "--preset", "light", "--weeks", "1", "--jobs", "1", "--policy", "deficit", "--judge")
    assert "oracle (ref)" in pre and "stall_gap_oracle" in pre


def test_judge_with_seeds_and_the_oracle_params(capsys):
    argv = ["--preset", "light", "--seeds", "1-2", "--weeks", "1", "--jobs", "1", "--policy", "deficit", "--judge",
            "--json"]
    data = json.loads(cli(capsys, *argv))
    assert data["seeds"] == [1, 2]
    assert set(data["results"]["light"]) == {"deficit", "oracle"}
    fast = json.loads(cli(capsys, *argv, "--param", "oracle.horizon_h=2", "--param", "oracle.step_s=1800"))
    assert fast["params"]["oracle"] == {"horizon_h": 2.0, "step_s": 1800.0}
    assert fast["oracle"]["params"] == {"horizon_h": 2.0, "step_s": 1800.0, "tail_h": 156.0}


def test_judge_is_deterministic_and_jobs_1_equals_jobs_4(capsys):
    base = ["--random", "6", "--weeks", "1", "--seed", "3", "--policy", "all", "--judge"]
    one = cli(capsys, *base, "--jobs", "1")
    four = cli(capsys, *base, "--jobs", "4")
    assert one == four == cli(capsys, *base, "--jobs", "1")
    assert cli(capsys, *base, "--jobs", "1", "--json") == cli(capsys, *base, "--jobs", "4", "--json")
    pre = ["--preset", "light", "--seeds", "1-2", "--weeks", "1", "--policy", "deficit", "--judge", "--json"]
    assert cli(capsys, *pre, "--jobs", "1") == cli(capsys, *pre, "--jobs", "4")


GAPS_SWEEP = "deficit.margin_pts=2,8"


def _gaps(capsys, *extra, jobs="1"):
    return cli(capsys, "--random", "6", "--weeks", "1", "--jobs", jobs, "--policy", "deficit",
               "--sweep", GAPS_SWEEP, "--score", "gaps", *extra)


def test_score_gaps_changes_the_formula_and_the_ranking_score(capsys):
    default = json.loads(cli(capsys, "--random", "6", "--weeks", "1", "--jobs", "1", "--policy", "deficit",
                             "--sweep", GAPS_SWEEP, "--json"))["sweep"]
    gaps = json.loads(_gaps(capsys, "--json"))["sweep"]
    assert "stall_excess" in default["formula"]
    assert gaps["formula"] == (
        "score = 1.0 * stall_gap_oracle + 1.0 * waste_gap_oracle + 0.5 * unevenness / 10.0"
        " + 0.2 * paid_switches_per_day"
    )
    assert default["rows"][0]["train"]["score"] != gaps["rows"][0]["train"]["score"]
    text = _gaps(capsys)
    assert "stall_gap_oracle" in text and "score = " in text
    row = gaps["rows"][0]
    for key in ("score", "stall_gap_oracle", "waste_gap_oracle", "unevenness", "paid_switches_per_day"):
        assert key in row["train"] and key in row["holdout"]


def test_score_gaps_is_the_documented_formula_on_the_train_half(capsys):
    data = json.loads(_gaps(capsys, "--json"))["sweep"]
    row = data["rows"][0]
    metrics = []
    for i in data["train_indices"]:
        s = rs.generate_scenario(1, i, 1)
        m = rs.simulate(s, "deficit", 0.12, policy_params=row["params"])["metrics"]
        o = rs.oracle(s, five_hour_share=0.12)["metrics"]
        m = {**m, "stall_gap_oracle": m["stall"] - o["stall"], "waste_gap_oracle": m["waste"] - o["waste"]}
        metrics.append(m)
    expect = sum(rs.score_of(m, "gaps") for m in metrics) / len(metrics)
    assert row["train"]["score"] == pytest.approx(expect, rel=1e-9)
    m = metrics[0]
    by_hand = (m["stall_gap_oracle"] + m["waste_gap_oracle"] + 0.5 * m["unevenness"] / 10.0
               + 0.2 * m["paid_switches_per_day"])
    assert rs.score_of(m, "gaps") == pytest.approx(by_hand)


def test_score_gaps_is_deterministic_across_jobs(capsys):
    assert _gaps(capsys, "--json", jobs="1") == _gaps(capsys, "--json", jobs="4")


@pytest.mark.parametrize(
    "argv",
    [
        ["--random", "4", "--score", "gaps"],  # a score belongs to a sweep
        ["--random", "4", "--sweep", "deficit.margin_pts=2,4", "--score", "nope"],
        ["--preset", "light", "--judge", "--sweep", "deficit.margin_pts=2,4"],
        ["--diagnose-steady", "--judge"],
        ["--random", "4", "--sensitivity", "--judge"],
    ],
)
def test_bad_judge_options_exit_with_code_2(argv):
    with pytest.raises(SystemExit) as exc:
        rs.main(argv)
    assert exc.value.code == 2


def test_the_judge_never_reads_a_home_folder_or_the_network(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("I/O outside the repo")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(builtins, "open", boom)
    out = cli(capsys, *_judge_args())
    assert "oracle (ref)" in out


# ==========================================================================
# Round 5 (task R3): a stateless f for scaled_line (``f_mode``)
#
#   f = clip(f_boost * sum(c_i * u_i) / sum(c_i * e_i), f_min, 1)
# u_i = used fraction of the current week (from the reading), e_i = elapsed
# fraction of that week. No history except the last f.
# ==========================================================================

STATELESS = {"f_mode": "stateless"}
WEEK_H = 168


def _three(now=T0, pcts=(30.0, 20.0, 10.0), elapsed=(0.5, 0.25, 0.75), caps=(1.0, 5.0, 20.0)):
    """Readings of three accounts with known used percent and elapsed fraction."""
    return {i: rdc(p, round((1.0 - e) * WEEK_H) * HOUR, c, now=now)
            for i, (p, e, c) in enumerate(zip(pcts, elapsed, caps))}


def test_f_mode_is_a_named_parameter_with_trailing_as_the_default():
    assert rs.PARAM_DEFAULTS["scaled_line"]["f_mode"] == "trailing"
    assert rs.parse_params(["scaled_line.f_mode=stateless"]) == {"scaled_line": {"f_mode": "stateless"}}
    assert rs.parse_params(["scaled_line.f_mode=trailing"]) == {"scaled_line": {"f_mode": "trailing"}}
    for bad in ("scaled_line.f_mode=bogus", "scaled_line.f_mode=1", "scaled_line.f_mode=",
                "deficit.f_mode=stateless", "scaled_line.f_boost=stateless"):
        with pytest.raises(ValueError):
            rs.parse_params([bad])


def test_f_mode_trailing_is_byte_identical_to_no_parameter(capsys):
    for name in ("user_like", "mixed_sizes"):
        s = rs.preset_scenario(name, seed=2, weeks=2)
        a = sim(s, "scaled_line", share=0.12, factor=1.5)
        b = sim(s, "scaled_line", share=0.12, factor=1.5, policy_params={"f_mode": "trailing"})
        assert a["trace"] == b["trace"]
        assert repr(a["metrics"]) == repr(b["metrics"])
        assert a["resets"] == b["resets"] and a["switch_ticks"] == b["switch_ticks"]
    base = ["--random", "8", "--weeks", "1", "--seed", "6", "--policy", "scaled_line", "--jobs", "1"]
    assert cli(capsys, *base) == cli(capsys, *base, "--param", "scaled_line.f_mode=trailing")
    pre = ["--preset", "user_like", "--weeks", "1", "--policy", "scaled_line", "--jobs", "1", "--judge"]
    assert cli(capsys, *pre) == cli(capsys, *pre, "--param", "scaled_line.f_mode=trailing")
    plain = json.loads(cli(capsys, *base, "--json"))
    named = json.loads(cli(capsys, *base, "--json", "--param", "scaled_line.f_mode=trailing"))
    assert named.pop("params") == {"scaled_line": {"f_mode": "trailing"}} and plain.pop("params") == {}
    assert plain == named
    # the two modes are different rules
    assert cli(capsys, *base) != cli(capsys, *base, "--param", "scaled_line.f_mode=stateless")


def test_stateless_f_hand_checked_on_three_accounts():
    # capacities 1, 5, 20; used 0.30, 0.20, 0.10; elapsed 0.5, 0.25, 0.75
    # sum(c * u) = 0.3 + 1.0 + 2.0 = 3.3; sum(c * e) = 0.5 + 1.25 + 15.0 = 16.75
    raw = 3.3 / 16.75
    pol, st = scaled(f_boost=1.0, **STATELESS)
    assert feed(pol, st, [(T0, _three())]) == pytest.approx(raw)
    assert raw == pytest.approx(0.197015, abs=1e-6)
    pol, st = scaled(**STATELESS)  # default boost 1.1
    assert feed(pol, st, [(T0, _three())]) == pytest.approx(1.1 * raw)
    # the clip: low at f_min, high at 1
    pol, st = scaled(f_boost=1.0, **STATELESS)
    assert feed(pol, st, [(T0, _three(pcts=(1.0, 1.0, 1.0)))]) == pytest.approx(0.1)
    pol, st = scaled(f_boost=1.0, f_min=0.3, **STATELESS)
    assert feed(pol, st, [(T0, _three())]) == pytest.approx(0.3)
    pol, st = scaled(f_boost=1.0, **STATELESS)
    assert feed(pol, st, [(T0, _three(pcts=(90.0, 90.0, 90.0)))]) == 1.0


def test_stateless_f_keeps_no_history_and_depends_on_the_current_readings_only():
    pol, st = scaled(f_boost=1.0, **STATELESS)
    t1 = T0 + 6 * HOUR
    noise = _three(pcts=(80.0, 5.0, 60.0), elapsed=(0.9, 0.1, 0.6))
    after_noise = feed(pol, st, [(T0, noise), (t1, _three(now=t1))])
    pol2, st2 = scaled(f_boost=1.0, **STATELESS)
    assert after_noise == feed(pol2, st2, [(t1, _three(now=t1))]) == pytest.approx(3.3 / 16.75)
    assert not st.get("hist") and not st.get("seen") and "consumed" not in st


def test_stateless_f_floor_uses_the_previous_f_or_f_min_at_the_start():
    # every account reset 5 minutes ago: elapsed 300 / 604800, below the floor
    fresh = {i: rs.Reading(p, 0.0, T0 + rs.WEEK_S - rs.TICK_S, c, T0, 0.12)
             for i, (p, c) in enumerate(((0.0, 1.0), (0.01, 5.0), (0.0, 20.0)))}
    pol, st = scaled(f_boost=1.0, **STATELESS)
    assert feed(pol, st, [(T0, fresh)]) == 0.1  # the start: f_min
    pol, st = scaled(f_boost=1.0, f_min=0.25, **STATELESS)
    assert feed(pol, st, [(T0, fresh)]) == 0.25
    # after a normal call, the floor case keeps the previous f
    pol, st = scaled(f_boost=1.0, **STATELESS)
    t0 = T0 - HOUR
    prev = feed(pol, st, [(t0, _three(now=t0))])
    assert prev == pytest.approx(3.3 / 16.75)
    assert feed(pol, st, [(T0, fresh)]) == prev
    assert rs.SCALED_STATELESS_FLOOR > 300 / rs.WEEK_S  # the named floor covers this case


def test_stateless_f_ignores_the_used_percent_of_a_reading_from_a_week_that_ended():
    # account 0: a reading taken 10 minutes ago, 5 minutes before its reset; the
    # 80 percent belongs to the week that ended, so u_0 = 0 and e_0 = 300 / WEEK_S.
    t_read = T0 - 2 * rs.TICK_S
    stale = rs.Reading(80.0, 0.0, T0 - rs.TICK_S, 1.0, t_read, 0.12)
    readings = {0: stale, 1: rdc(20.0, 84 * HOUR, 1.0)}
    pol, st = scaled(f_boost=1.0, **STATELESS)
    assert feed(pol, st, [(T0, readings)]) == pytest.approx(0.2 / (0.5 + rs.TICK_S / rs.WEEK_S))
    # the same reading before its reset counts in full
    pol, st = scaled(f_boost=1.0, **STATELESS)
    live = {0: rs.Reading(80.0, 0.0, T0 - rs.TICK_S, 1.0, t_read, 0.12), 1: rdc(20.0, 84 * HOUR, 1.0, now=t_read)}
    e0 = 1.0 - rs.TICK_S / rs.WEEK_S
    assert feed(pol, st, [(t_read, live)]) == pytest.approx(1.0 / (e0 + 0.5))


def test_stateless_evens_the_weekly_percent_under_the_steady_light_load():
    s = rs.steady_scenario(8)
    assert [a.capacity for a in s.accounts] == [1.0, 5.0, 20.0] and rs.STEADY_LOAD == 0.3
    res = rs.simulate(s, "scaled_line", 0.12, policy_params=STATELESS)
    spread = _spread_by_week(res, 3)
    for week in range(rs.STEADY_SKIP_WEEKS + 1, 9):
        assert spread[week] < 10.0, (week, spread)


def test_stateless_f_follows_a_tenfold_rise():
    log = _wrapped_run(_step_scenario(0.1, 1.0), guard=0.0, **STATELESS)
    step = 2 * W
    f = {t: v for t, v in log}
    assert max(v for t, v in log if step - 100 <= t < step) < 0.2
    assert f[step + 7 * rs.TICKS_PER_DAY] >= 0.9
    assert f[step + 3 * rs.TICKS_PER_DAY] > 2 * f[step - 1]
    assert max(v for _t, v in log) <= 1.0


def test_stateless_f_follows_a_tenfold_fall():
    log = _wrapped_run(_step_scenario(1.0, 0.1), guard=0.0, **STATELESS)
    step = 2 * W
    f = {t: v for t, v in log}
    assert f[step - 1] >= 0.9
    day3 = f[step + 3 * rs.TICKS_PER_DAY]
    day7 = f[step + 7 * rs.TICKS_PER_DAY]
    assert day7 < day3 < f[step - 1]
    assert day7 < 0.2


def test_the_stateless_policy_gets_only_readings_and_never_reads_the_truth(monkeypatch):
    inside = {"on": False, "reads": 0}
    truth = rs.AccountState

    class Spy(truth):
        __slots__ = ()

        def __getattribute__(self, name):
            if inside["on"] and name in ("used", "win_used", "cap", "five_cap", "headroom"):
                inside["reads"] += 1
            return truth.__getattribute__(self, name)

    seen, modes = [], set()

    def choose(readings, active, now, state):
        inside["on"] = True
        try:
            seen.append({type(v) for v in readings.values()})
            modes.add(state["f_mode"])
            return rs.scaled_line_choose(readings, active, now, state)
        finally:
            inside["on"] = False

    monkeypatch.setattr(rs, "AccountState", Spy)
    pol = rs.Policy("spy", choose, rs.POLICIES["scaled_line"].make_state)
    s = rs.preset_scenario("user_like", seed=1, weeks=1)
    res = rs.simulate(scn(s.accounts, s.demand[:W]), pol, 0.12, policy_params=STATELESS, trace=True)
    assert modes == {"stateless"}
    assert seen and all(kinds == {rs.Reading} for kinds in seen)
    assert inside["reads"] == 0
    assert len(set(seq(res))) > 1


@pytest.mark.parametrize("name", ["user_like", "mixed_sizes", "many_large"])
def test_stateless_is_scale_invariant(name):
    s = rs.preset_scenario(name, seed=2, weeks=1)
    a = sim(s, "scaled_line", share=0.12, factor=1.5, policy_params=STATELESS)
    b = sim(_scaled(s, 7.0), "scaled_line", share=0.12, factor=1.5, policy_params=STATELESS)
    assert seq(a) == seq(b)
    assert len(set(seq(a))) > 1
    for key, va in a["metrics"].items():
        vb = b["metrics"][key]
        assert (va is None) == (vb is None), key
        if va is not None:
            assert vb == pytest.approx(va, rel=1e-6, abs=1e-9), key


def test_stateless_is_deterministic_and_conserves_demand(capsys):
    s = rs.generate_scenario(3, 5, weeks=1)
    a = sim(s, "scaled_line", share=0.12, factor=1.5, policy_params=STATELESS)
    b = sim(s, "scaled_line", share=0.12, factor=1.5, policy_params=STATELESS)
    assert seq(a) == seq(b) and a["metrics"] == b["metrics"]
    t = a["totals"]
    assert t["served"] + t["unserved"] == pytest.approx(t["demand"])
    base = ["--random", "8", "--weeks", "1", "--seed", "6", "--policy", "scaled_line",
            "--param", "scaled_line.f_mode=stateless"]
    one = cli(capsys, *base, "--jobs", "1")
    assert one == cli(capsys, *base, "--jobs", "4") == cli(capsys, *base, "--jobs", "1")
    data = json.loads(cli(capsys, *base, "--jobs", "1", "--json"))
    assert data["params"] == {"scaled_line": {"f_mode": "stateless"}}


@pytest.mark.parametrize(
    "argv",
    [
        ["--random", "2", "--weeks", "1", "--param", "scaled_line.f_mode=bogus"],
        ["--random", "2", "--weeks", "1", "--param", "scaled_line.f_mode=2"],
        ["--preset", "light", "--param", "scaled_line.f_mode=Stateless"],
        ["--diagnose-steady", "--weeks", "2", "--param", "scaled_line.f_mode=nope"],
        ["--random", "4", "--weeks", "1", "--sweep", "scaled_line.f_mode=trailing,bogus"],
    ],
)
def test_an_unknown_f_mode_exits_with_code_2(argv):
    with pytest.raises(SystemExit) as exc:
        rs.main(argv)
    assert exc.value.code == 2


def test_an_unknown_f_mode_given_to_simulate_is_an_error():
    s = rs.preset_scenario("light", seed=1, weeks=1)
    with pytest.raises(ValueError):
        rs.simulate(s, "scaled_line", 0.12, policy_params={"f_mode": "bogus"})


def test_f_mode_works_in_a_sweep_and_in_the_steady_diagnosis(capsys):
    argv = ["--random", "6", "--weeks", "1", "--jobs", "1", "--seed", "3",
            "--sweep", "scaled_line.f_mode=trailing,stateless;scaled_line.margin_pts=4,8"]
    data = json.loads(cli(capsys, *argv, "--json"))
    points = [r["params"] for r in data["sweep"]["rows"]]
    assert sorted((p["f_mode"], p["margin_pts"]) for p in points) == [
        ("stateless", 4.0), ("stateless", 8.0), ("trailing", 4.0), ("trailing", 8.0)]
    text = cli(capsys, *argv)
    assert "f_mode=stateless" in text and "f_mode=trailing" in text and "margin_pts=4" in text
    fixed = cli(capsys, "--random", "6", "--weeks", "1", "--jobs", "1", "--seed", "3",
                "--param", "scaled_line.f_mode=stateless", "--sweep", "scaled_line.margin_pts=4,8")
    assert "# fixed (--param): f_mode=stateless" in fixed
    base = ["--diagnose-steady", "--weeks", "3", "--jobs", "1", "--json"]
    trailing = json.loads(cli(capsys, *base))
    stateless = json.loads(cli(capsys, *base, "--param", "scaled_line.f_mode=stateless"))
    pick = lambda d, p: [r for r in d["rows"] if r["policy"] == p]  # noqa: E731
    assert pick(trailing, "scaled_line") != pick(stateless, "scaled_line")
    assert pick(trailing, "deficit") == pick(stateless, "deficit")
