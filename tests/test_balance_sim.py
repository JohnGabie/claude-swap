"""Tests for tools/balance_sim.py, the S2 policy simulator.

The tool lives outside ``src``, so pyproject does not put it on the path; it
is loaded from its file with importlib.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from claude_swap import balance

TOOL = Path(__file__).resolve().parents[1] / "tools" / "balance_sim.py"
WEEK_S = 7 * 24 * 3600


def _load():
    spec = importlib.util.spec_from_file_location("balance_sim", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["balance_sim"] = module
    spec.loader.exec_module(module)
    return module


sim = _load()


@pytest.fixture(scope="module")
def full_run():
    return sim.run_all(sim.SEED)


def _pro(i, **kw):
    return sim.SimAccount(id=i, plan="pro", capacity=1.0, five_hour_cap=1.0, **kw)


def _max(i, **kw):
    return sim.SimAccount(id=i, plan="max", capacity=5.0, five_hour_cap=5.0, **kw)


def _policy(name):
    return sim.make_policy(name)


# --- determinism and conservation -----------------------------------------


def test_same_seed_gives_same_output_twice(full_run, monkeypatch):
    # The fake clock is the only clock: a wall-clock read fails the run.
    def _no_wall_clock():
        raise AssertionError("the simulator read the wall clock")

    monkeypatch.setattr(time, "time", _no_wall_clock)
    again = sim.run_all(sim.SEED)
    assert json.dumps(again, sort_keys=True) == json.dumps(full_run, sort_keys=True)


def test_other_seed_changes_the_demand():
    a = sim.build_demand(sim.SCENARIOS["light"], sim.SEED)
    b = sim.build_demand(sim.SCENARIOS["light"], sim.SEED + 1)
    assert a != b
    assert a == sim.build_demand(sim.SCENARIOS["light"], sim.SEED)


@pytest.mark.parametrize("scenario", list(sim.SCENARIOS))
@pytest.mark.parametrize("policy", sim.POLICIES)
def test_served_plus_unserved_equals_demand(scenario, policy):
    result = sim.run_scenario(scenario, policy, sim.SEED)
    assert result.served + result.unserved == pytest.approx(result.demand, abs=1e-9)
    assert result.total_served + result.total_unserved == pytest.approx(
        result.total_demand, abs=1e-9
    )
    assert result.demand > 0


# --- hand-checked metrics on tiny fleets -----------------------------------


def test_raw_spread_unchanged_on_old_case():
    # S2b: the old "spread" is now the "raw_spread" column; same numbers.
    # Tick 0 serves 0.1 Pro week on account 1 (10%). Samples at t=0 (spread
    # 0, before serving) and t=3600 (spread 10). Mean 5, max 10.
    demand = [0.1] + [0.0] * 23
    result = sim.simulate([_pro(1), _pro(2)], demand, _policy("best"), active_id=1)
    m = result.metrics()
    assert result.spread_samples == [0.0, pytest.approx(10.0)]
    assert m["raw_spread_mean"] == pytest.approx(5.0)
    assert m["raw_spread_max"] == pytest.approx(10.0)


def test_waste_at_reset_hand_checked():
    # Pro at 30% and Max at 80% both reset at t=3600, with no demand.
    # Pro waste 70%. Max waste 20%. Total by quota: (0.7*1 + 0.2*5) / 6.
    accounts = [
        _pro(1, week_used=0.3, resets_at=3600.0),
        _max(2, week_used=4.0, resets_at=3600.0),
    ]
    result = sim.simulate(accounts, [0.0] * 13, _policy("best"), active_id=1)
    m = result.metrics()
    assert m["waste_pro"] == pytest.approx(70.0)
    assert m["waste_max"] == pytest.approx(20.0)
    assert m["waste_total"] == pytest.approx(170.0 / 6.0)


def test_waste_is_none_for_a_class_with_no_reset():
    accounts = [_pro(1, week_used=0.3, resets_at=3600.0), _pro(2)]
    m = sim.simulate(accounts, [0.0] * 13, _policy("best"), active_id=1).metrics()
    assert m["waste_pro"] == pytest.approx(70.0)
    assert m["waste_max"] is None


def test_block_count_hand_checked():
    # 1.2 Pro weeks at tick 0: account 1 fills to 100% (one block), 0.2 is
    # unserved. Tick 1: at-limit switch to account 2, which serves 0.5.
    demand = [1.2, 0.5, 0.0]
    result = sim.simulate([_pro(1), _pro(2)], demand, _policy("best"), active_id=1)
    m = result.metrics()
    assert m["blocks"] == 1
    assert result.switches == 1
    assert result.unserved == pytest.approx(0.2)
    assert m["unserved_pct"] == pytest.approx(0.2 / 1.7 * 100.0)


def test_switches_per_day_hand_checked():
    # Account 1 goes to 95% at tick 0. Tick 1: best switches to account 2.
    one_day = sim.TICKS_PER_DAY
    demand = [0.95] + [0.0] * (2 * one_day - 1)
    result = sim.simulate([_pro(1), _pro(2)], demand, _policy("best"), active_id=1)
    assert result.switches == 1
    assert result.metrics()["switches_per_day"] == pytest.approx(0.5)

    demand = [0.95] + [0.0] * (one_day - 1)
    result = sim.simulate([_pro(1), _pro(2)], demand, _policy("best"), active_id=1)
    assert result.metrics()["switches_per_day"] == pytest.approx(1.0)


def test_warm_up_ticks_are_not_measured():
    demand = [1.2, 0.0, 0.0]
    result = sim.simulate(
        [_pro(1), _pro(2)], demand, _policy("best"), active_id=1, measure_from=1
    )
    assert result.metrics()["blocks"] == 0
    assert result.demand == 0.0
    assert result.total_demand == pytest.approx(1.2)


# --- weekly clock -----------------------------------------------------------


def test_weekly_clock_runs_with_no_use_and_resets_every_7_days():
    # S2c (replaces the S2 test "starts at first use"; the user saw a reset
    # time on an idle account, so the old rule no longer holds). Account 1 is
    # used once (20%); account 2 is never used and sits at 0%. Both windows
    # reset after 7 days with no use, and keep a reset time after the reset.
    tick = sim.TICK_S
    n = 2 * WEEK_S // tick + 10
    demand = [0.2] + [0.0] * (n - 1)
    accounts = [_pro(1, resets_at=3600.0 * 2), _pro(2, resets_at=3600.0)]
    result = sim.simulate(accounts, demand, _policy("best"), active_id=1)
    a1, a2 = accounts
    # resets at 2 h and 2 h + 7 d (account 1); 1 h and 1 h + 7 d (account 2)
    assert a1.resets_at == pytest.approx(7200.0 + 2 * WEEK_S)
    assert a2.resets_at == pytest.approx(3600.0 + 2 * WEEK_S)
    assert result.reset_log == [
        (3600.0, 2), (7200.0, 1), (3600.0 + WEEK_S, 2), (7200.0 + WEEK_S, 1)]
    # an idle account at 0% has a reset time
    assert a2.pct7() == 0.0 and a2.resets_at is not None
    # waste counts for the idle account too: 4 resets, 80 + 100 + 100 + 100
    assert result.metrics()["waste_pro"] == pytest.approx(380.0 / 4)


def test_idle_account_wastes_100_percent_at_its_reset():
    acc = _pro(1, resets_at=3600.0)
    result = sim.simulate([acc], [0.0] * 13, _policy("best"), active_id=1)
    assert result.reset_log == [(3600.0, 1)]
    assert result.metrics()["waste_pro"] == pytest.approx(100.0)
    assert acc.resets_at == pytest.approx(3600.0 + WEEK_S)


def test_used_account_wastes_only_the_unused_part():
    # 0.25 Pro week served at tick 0, reset at 1 h: 75% wasted, then the
    # next week starts at 0% with a reset time 7 days later.
    acc = _pro(1, resets_at=3600.0)
    result = sim.simulate([acc], [0.25] + [0.0] * 12, _policy("best"), active_id=1)
    assert result.metrics()["waste_pro"] == pytest.approx(75.0)
    assert acc.pct7() == 0.0
    assert acc.resets_at == pytest.approx(3600.0 + WEEK_S)


def test_missing_reset_time_never_starts_a_clock():
    # The missing-reset case (resets_at None) is only for tests: no clock
    # starts at the first use, so no reset and no waste is counted.
    acc = _pro(1)
    result = sim.simulate([acc], [0.2, 0.0, 0.0], _policy("best"), active_id=1)
    assert acc.resets_at is None
    assert acc.pct7() == pytest.approx(20.0)
    assert result.reset_log == []
    assert result.metrics()["waste_pro"] is None


@pytest.mark.parametrize("scenario", list(sim.SCENARIOS))
def test_staggered_resets_happen_at_the_configured_offsets(scenario):
    result = sim.run_scenario(scenario, "best", sim.SEED)
    stagger = sim.SCENARIOS[scenario]["stagger"]
    start = sim.WARMUP_WEEKS * WEEK_S
    end = start + sim.MEASURED_WEEKS * WEEK_S
    for plan, i in sim.FLEET_PLANS:
        first = stagger[i][1] * 3600.0
        expected = [first + k * WEEK_S for k in range(sim.WARMUP_WEEKS
                                                      + sim.MEASURED_WEEKS + 1)]
        expected = [t for t in expected if start <= t < end]
        assert [t for t, a in result.reset_log if a == i] == expected
        assert len(expected) == sim.MEASURED_WEEKS  # one reset per week
    # every account resets once per measured week, idle or not
    assert len(result.reset_log) == len(sim.FLEET_PLANS) * sim.MEASURED_WEEKS


def test_near_reset_account_3_resets_3_hours_after_the_measured_start():
    # The measured start of the near-reset scenario is its first working
    # hour (the event sets account 3 to 40% there); the reset is 3 h later.
    near = sim.SCENARIOS["near-reset"]["near_reset"]
    start = sim.WARMUP_WEEKS * WEEK_S + sim.WORK_START_H * 3600.0
    result = sim.run_scenario("near-reset", "best", sim.SEED)
    first = min(t for t, a in result.reset_log if a == near["id"])
    assert first == pytest.approx(start + near["hours"] * 3600.0)
    assert near == {"id": 3, "pct": 40.0, "hours": 3.0}


@pytest.mark.parametrize("scenario", list(sim.SCENARIOS))
def test_resets_at_is_never_none_during_a_run(scenario, monkeypatch):
    real_decide = balance.decide
    seen = {"windows": 0}

    def spy(accounts, *args, **kwargs):
        for a in accounts:
            assert a.seven_day.resets_at is not None, a
            assert a.seven_day.resets_at > args[1]  # after `now`
            seen["windows"] += 1
        return real_decide(accounts, *args, **kwargs)

    monkeypatch.setattr(balance, "decide", spy)
    result = sim.run_scenario(scenario, "balanced", sim.SEED)
    assert seen["windows"] == result.ticks * len(sim.FLEET_PLANS)


def test_five_hour_window_is_a_rolling_sum():
    tick = sim.TICK_S
    acc = sim.SimAccount(id=1, plan="pro", capacity=1.0, five_hour_cap=0.1)
    n = (5 * 3600) // tick + 2
    sim.simulate([acc], [0.05] + [0.0] * (n - 1), _policy("best"), active_id=1)
    assert acc.pct5(n * tick) == 0.0
    acc = sim.SimAccount(id=1, plan="pro", capacity=1.0, five_hour_cap=0.1)
    sim.simulate([acc], [0.05, 0.0], _policy("best"), active_id=1)
    assert acc.pct5(2 * tick) == pytest.approx(50.0)


def test_default_five_hour_cap_scales_with_capacity():
    fleet = sim.build_fleet(sim.SCENARIOS["light"])
    by_id = {a.id: a for a in fleet}
    assert [a.plan for a in fleet] == ["pro"] * 4 + ["max"]
    assert by_id[1].capacity == 1.0 and by_id[5].capacity == 5.0
    assert by_id[1].five_hour_cap == pytest.approx(sim.FIVE_HOUR_SHARE)
    assert by_id[5].five_hour_cap == pytest.approx(sim.FIVE_HOUR_SHARE * 5.0)
    fleet = sim.build_fleet(sim.SCENARIOS["light"], five_hour_share=0.08)
    by_id = {a.id: a for a in fleet}
    assert by_id[1].five_hour_cap == pytest.approx(0.08)
    assert by_id[5].five_hour_cap == pytest.approx(0.40)


# --- blocked accounts --------------------------------------------------------


@pytest.mark.parametrize("policy", sim.POLICIES)
def test_account_at_100_percent_is_not_served(policy):
    acc = _pro(1, week_used=1.0, resets_at=10 * WEEK_S)
    result = sim.simulate([acc], [0.1, 0.1], _policy(policy), active_id=1)
    assert result.served == 0.0
    assert result.unserved == pytest.approx(0.2)
    assert acc.week_used == 1.0


@pytest.mark.parametrize("policy", sim.POLICIES)
def test_all_accounts_blocked_leaves_demand_unserved(policy):
    accounts = [
        _pro(1, week_used=1.0, resets_at=10 * WEEK_S),
        _pro(2, week_used=1.0, resets_at=10 * WEEK_S),
        _max(5, week_used=5.0, resets_at=10 * WEEK_S),
    ]
    result = sim.simulate(accounts, [0.3] * 4, _policy(policy), active_id=1)
    assert result.served == 0.0
    assert result.unserved == pytest.approx(1.2)
    assert result.metrics()["unserved_pct"] == pytest.approx(100.0)


# --- balanced uses the real module ------------------------------------------


def test_balanced_calls_decide_with_real_balance_types(monkeypatch):
    real_decide = balance.decide
    calls = []

    def spy(accounts, active_id, now, *args, **kwargs):
        assert accounts, "decide got no accounts"
        for a in accounts:
            assert type(a) is balance.Account
            for w in (a.five_hour, a.seven_day):
                assert type(w) is balance.Window
        calls.append(kwargs.get("cooldown_s"))
        return real_decide(accounts, active_id, now, *args, **kwargs)

    monkeypatch.setattr(balance, "decide", spy)
    result = sim.run_scenario("light", "balanced", sim.SEED)
    assert len(calls) == result.ticks
    assert set(calls) == {balance.BALANCED_COOLDOWN_S}


def test_balanced_proactive_cooldown_is_tracked():
    # Account 1 is less urgent (reset 6 days out) than account 2 (reset in
    # 10 hours with headroom): balanced moves proactively to account 2.
    accounts = [
        _pro(1, week_used=0.1, resets_at=6 * 24 * 3600.0),
        _pro(2, week_used=0.1, resets_at=10 * 3600.0),
    ]
    policy = _policy("balanced")
    result = sim.simulate(accounts, [0.0] * 3, policy, active_id=1)
    assert result.switches == 1
    assert policy.last_proactive_ts == 0.0


# --- isolation and output ----------------------------------------------------


def test_tool_never_touches_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    with redirect_stdout(io.StringIO()):
        assert sim.main([]) == 0
        assert sim.main(["--json", "--cap", "12"]) == 0
    assert list(tmp_path.rglob("*")) == []


@pytest.fixture(scope="module")
def default_text(full_run):
    return sim.render(full_run)


def test_json_parses_and_has_the_table_keys(default_text):
    out = io.StringIO()
    with redirect_stdout(out):
        assert sim.main(["--json"]) == 0
    data = json.loads(out.getvalue())

    # S2b: one table per scenario, rows = cap x policy, header "cap policy ...".
    headers = [
        line.split()[2:]
        for line in default_text.splitlines()
        if line.startswith("cap ")
    ]
    assert len(headers) == len(sim.SCENARIOS)
    assert data["caps"] == [sim.cap_label(s) for s in sim.FIVE_HOUR_SHARES]
    assert data["caps"] == ["8", "12", "16"]

    assert set(data["scenarios"]) == set(sim.SCENARIOS)
    for header, (name, scen) in zip(headers, data["scenarios"].items()):
        assert list(scen["caps"]) == data["caps"]
        for policies in scen["caps"].values():
            assert set(policies) == set(sim.POLICIES)
            for metrics in policies.values():
                assert list(metrics) == header == list(sim.METRICS)
    # S2c: the summary also holds the waste identity (was: only 2 keys).
    assert set(data["summary"]) == {"best_policy", "claims", "waste_identity"}
    assert set(data["summary"]["claims"]) == {"lowest_pro_waste", "lowest_unserved"}


def test_table_names_policies_and_summary(default_text):
    text = default_text
    for p in sim.POLICIES:
        assert p in text
    assert text.count("Summary") == 1
    # one line per scenario and one for all scenarios, for each of 2 claims
    assert text.count("conclusion holds across caps:") == 2 * (len(sim.SCENARIOS) + 1)
    assert "Max" in text and "reserve" in text


# --- S2b: thresholds of balanced -----------------------------------------------


def test_balanced_passes_the_engine_thresholds(monkeypatch):
    real_decide = balance.decide
    seen = []

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real_decide(*args, **kwargs)

    monkeypatch.setattr(balance, "decide", spy)
    accounts = [_pro(1, week_used=0.5, resets_at=WEEK_S), _pro(2)]
    sim.simulate(accounts, [0.01] * 4, _policy("balanced"), active_id=1)
    assert len(seen) == 4
    assert sim.BALANCED_THRESHOLD == 90.0 == sim.THRESHOLD_PCT
    assert sim.BALANCED_WEEKLY_THRESHOLD is None
    for kwargs in seen:
        assert kwargs["threshold"] == 90.0
        assert "weekly_threshold" in kwargs and kwargs["weekly_threshold"] is None


# --- S2b: pace_dev -------------------------------------------------------------


DAY_S = 24 * 3600


def test_ahead_of_pace_uses_the_account_own_week():
    # 3.5 days into its week: expected 50%. 1.75 days in: expected 25%.
    a = _pro(1, week_used=0.7, resets_at=3.5 * DAY_S)
    b = _pro(2, week_used=0.05, resets_at=5.25 * DAY_S)
    assert sim.ahead_of_pace(a, 0.0) == pytest.approx(20.0)
    assert sim.ahead_of_pace(b, 0.0) == pytest.approx(-20.0)
    assert sim.ahead_of_pace(_pro(3), 0.0) is None  # missing reset time
    # S2c: an idle account at 0% has a running week and is behind pace.
    assert sim.ahead_of_pace(_pro(4, resets_at=3.5 * DAY_S), 0.0) == pytest.approx(-50.0)


def test_pace_dev_hand_checked():
    # Account 1 is 20 points ahead of pace, account 2 is 20 points behind.
    # Population standard deviation of (+20, -20) = 20. Account 3 has no
    # reset time (the missing-reset case, kept for tests only) and account 5
    # is Max: neither counts in pace_dev. The raw spread (70 - 0, account 3
    # included) differs on purpose.
    accounts = [
        _pro(1, week_used=0.7, resets_at=3.5 * DAY_S),
        _pro(2, week_used=0.05, resets_at=5.25 * DAY_S),
        _pro(3),
        _max(5, week_used=1.0, resets_at=DAY_S),
    ]
    result = sim.simulate(accounts, [0.0], _policy("best"), active_id=1)
    m = result.metrics()
    assert result.pace_samples == [pytest.approx(20.0)]
    assert m["pace_dev_mean"] == pytest.approx(20.0)
    assert m["pace_dev_max"] == pytest.approx(20.0)
    assert m["pace_hours"] == 1
    assert m["raw_spread_mean"] == pytest.approx(70.0)


def test_pace_dev_mean_and_max_over_hours():
    # Hour 0: account 1 is 1 hour before its reset, account 2 is 20 behind.
    # S2c (was: account 1 goes idle at t=3600 and hour 1 has no sample): at
    # t=3600 account 1 resets and its new week starts at 0% with 0% expected
    # (ahead 0), so hour 1 has a sample too. Two accounts: pstdev = |a-b|/2.
    accounts = [
        _pro(1, week_used=0.99, resets_at=3600.0),
        _pro(2, week_used=0.05, resets_at=5.25 * DAY_S),
    ]
    h0 = abs((99.0 - (WEEK_S - 3600.0) / WEEK_S * 100.0) - (-20.0)) / 2.0
    a2_h1 = 5.0 - (1.75 * DAY_S + 3600.0) / WEEK_S * 100.0
    h1 = abs(0.0 - a2_h1) / 2.0
    result = sim.simulate(accounts, [0.0] * 13, _policy("best"), active_id=2)
    assert result.pace_samples == [pytest.approx(h0), pytest.approx(h1)]
    m = result.metrics()
    assert m["pace_dev_max"] == pytest.approx(max(h0, h1))
    assert m["pace_dev_mean"] == pytest.approx((h0 + h1) / 2.0)


def test_pace_dev_hand_checked_with_all_4_pro_accounts_running():
    # S2c: all 4 Pro accounts have a running week, the idle one included.
    # Aheads at t=0: +20 (70% at 3.5 d left), -20 (5% at 5.25 d left),
    # -50 (idle, 0% at 3.5 d left), +10 (60% at 3.5 d left). Mean -10;
    # squared deviations 900, 100, 1600, 400; pstdev = sqrt(3000 / 4).
    accounts = [
        _pro(1, week_used=0.7, resets_at=3.5 * DAY_S),
        _pro(2, week_used=0.05, resets_at=5.25 * DAY_S),
        _pro(3, resets_at=3.5 * DAY_S),
        _pro(4, week_used=0.6, resets_at=3.5 * DAY_S),
        _max(5, week_used=1.0, resets_at=DAY_S),
    ]
    result = sim.simulate(accounts, [0.0], _policy("best"), active_id=1)
    assert result.pace_samples == [pytest.approx((3000.0 / 4) ** 0.5)]


@pytest.mark.parametrize("scenario", list(sim.SCENARIOS))
def test_every_measured_hour_has_a_pace_sample(full_run, scenario):
    # S2c: with running clocks, `best` no longer shows "-" for pace_dev.
    hours = sim.MEASURED_WEEKS * 7 * 24
    for policies in full_run["scenarios"][scenario]["caps"].values():
        for m in policies.values():
            assert m["pace_hours"] == hours
            assert m["pace_dev_mean"] is not None


# --- S2b: flaps ----------------------------------------------------------------


def test_flaps_hand_checked():
    S = sim.Switch
    rule = sim.FLAP_RULE_BALANCED
    assert sim.FLAP_WINDOW_S == 600
    # balanced: a proactive switch, then a forced switch out of the same
    # account within 10 minutes.
    assert sim.count_flaps([S(0.0, 1, 2, "proactive"), S(540.0, 2, 3, "threshold")], rule) == 1
    assert sim.count_flaps([S(0.0, 1, 2, "proactive"), S(660.0, 2, 3, "threshold")], rule) == 0
    assert sim.count_flaps([S(0.0, 1, 2, "proactive"), S(600.0, 2, 3, "threshold")], rule) == 1
    assert sim.count_flaps([S(0.0, 1, 2, "proactive"), S(540.0, 2, 3, "proactive")], rule) == 0
    assert sim.count_flaps([S(0.0, 1, 2, "threshold"), S(540.0, 2, 3, "threshold")], rule) == 0
    # the second switch must leave the account the first one went to
    assert sim.count_flaps([S(0.0, 1, 2, "proactive"), S(540.0, 4, 3, "threshold")], rule) == 0
    # best / consume-first: any switch followed by a switch away in 10 min
    rule = sim.FLAP_RULE_ANY
    assert sim.count_flaps([S(0.0, 1, 2, "proactive"), S(540.0, 2, 1, "at-limit")], rule) == 1
    assert sim.count_flaps([S(0.0, 1, 2, "proactive"), S(660.0, 2, 1, "at-limit")], rule) == 0
    three = [S(0.0, 1, 2, "a"), S(300.0, 2, 3, "b"), S(600.0, 3, 1, "c")]
    assert sim.count_flaps(three, rule) == 2


def test_simulate_logs_switch_reasons_and_flaps():
    demand = [1.2, 0.5, 0.0]
    result = sim.simulate([_pro(1), _pro(2)], demand, _policy("best"), active_id=1)
    assert result.switch_log == [sim.Switch(300.0, 1, 2, "at-limit")]
    assert result.metrics()["flaps"] == 0

    accounts = [
        _pro(1, week_used=0.1, resets_at=6 * 24 * 3600.0),
        _pro(2, week_used=0.1, resets_at=10 * 3600.0),
    ]
    result = sim.simulate(accounts, [0.0] * 3, _policy("balanced"), active_id=1)
    assert result.switch_log == [sim.Switch(0.0, 1, 2, "proactive")]


# --- S2b: bursts scenario ------------------------------------------------------


def test_bursts_use_40_percent_of_fleet_week_in_3_days():
    scen = sim.SCENARIOS["bursts"]
    demand = sim.demand_for("bursts", sim.SEED)
    tpw, tpd = sim.TICKS_PER_WEEK, sim.TICKS_PER_DAY
    target = 0.40 * sim.FLEET_CAPACITY
    weeks = sim.WARMUP_WEEKS + sim.MEASURED_WEEKS
    assert len(demand) == weeks * tpw
    for w in range(weeks):
        week = demand[w * tpw:(w + 1) * tpw]
        assert sum(week[:4 * tpd]) == 0.0  # 4 idle days
        for d in range(4, 7):
            assert sum(week[d * tpd:(d + 1) * tpd]) > 0.0
        assert sum(week) == pytest.approx(target, rel=0.01)
    assert scen["active_days"] == (4, 5, 6)


# --- S2b: 5-hour cap sensitivity -----------------------------------------------


TINY = {
    "about": "tiny load, far below every 5-hour cap",
    "rate": 0.001,
    "active_days": None,
    "stagger": sim.STAGGER,
}


def test_cap_option_changes_only_the_five_hour_numbers(monkeypatch):
    monkeypatch.setattr(sim, "SCENARIOS", {"tiny": TINY})
    runs = {}
    for cap in ("8", "16"):
        out = io.StringIO()
        with redirect_stdout(out):
            assert sim.main(["--json", "--cap", cap]) == 0
        data = json.loads(out.getvalue())
        assert data["caps"] == [cap]
        runs[cap] = data["scenarios"]["tiny"]["caps"][cap]
    for policy in sim.POLICIES:
        m8, m16 = dict(runs["8"][policy]), dict(runs["16"][policy])
        p8, p16 = m8.pop("peak5"), m16.pop("peak5")
        assert 0.0 < p8 < 50.0  # no 5-hour window is reached
        assert p8 == pytest.approx(2.0 * p16)
        assert m8 == m16  # every weekly number is equal


def test_default_run_prints_all_three_caps(default_text):
    for cap in ("8", "12", "16"):
        assert any(line.startswith(f"{cap}% ") for line in default_text.splitlines())


# --- S2b: summary --------------------------------------------------------------


def _metrics(**values):
    m = {k: None for k in sim.METRICS}
    m.update(values)
    return m


def _fake_scenarios(balanced_waste_at_16):
    caps = {}
    for cap, bal in (("8", 1.0), ("12", 2.0), ("16", balanced_waste_at_16)):
        caps[cap] = {
            "best": _metrics(waste_pro=5.0, unserved_pct=0.5),
            "consume-first": _metrics(waste_pro=6.0, unserved_pct=0.7),
            "balanced": _metrics(waste_pro=bal, unserved_pct=0.1),
        }
    return {"fake": {"about": "", "caps": caps}}


def test_summary_claims_are_computed_from_the_data():
    holds = sim.summarize(_fake_scenarios(3.0))
    flip = sim.summarize(_fake_scenarios(9.0))

    waste = holds["claims"]["lowest_pro_waste"]["scenarios"]["fake"]
    assert waste["per_cap"] == {"8": "yes", "12": "yes", "16": "yes"}
    assert waste["holds_across_caps"] == "yes"

    waste = flip["claims"]["lowest_pro_waste"]["scenarios"]["fake"]
    assert waste["per_cap"] == {"8": "yes", "12": "yes", "16": "no"}
    assert waste["holds_across_caps"] == "no"
    assert flip["claims"]["lowest_pro_waste"]["all_scenarios"] == "no"
    assert flip["best_policy"]["fake"]["waste_pro"] == {
        "8": "balanced", "12": "balanced", "16": "best"}

    unserved = flip["claims"]["lowest_unserved"]["scenarios"]["fake"]
    assert unserved["holds_across_caps"] == "yes"

    text = sim.render_summary(flip)
    assert "conclusion holds across caps: no" in text
    assert "conclusion holds across caps: yes" in text


def test_unused_capacity_is_capacity_minus_served():
    # Two Pro accounts for one day: capacity 2 * 1/7 Pro week. 0.1 served.
    n = sim.TICKS_PER_DAY
    accounts = [_pro(1, resets_at=WEEK_S), _pro(2, resets_at=WEEK_S)]
    result = sim.simulate(accounts, [0.1] + [0.0] * (n - 1), _policy("best"),
                          active_id=1)
    cap = 2.0 / 7.0
    assert result.metrics()["unused_cap"] == pytest.approx((cap - 0.1) / cap * 100.0)


def test_waste_identity_is_computed_from_the_data():
    def caps(waste, unused, unserved):
        return {"12": {
            p: _metrics(waste_total=w, unused_cap=u, unserved_pct=s)
            for p, w, u, s in zip(sim.POLICIES, waste, unused, unserved)}}

    scen = {
        "even": {"about": "", "caps": caps((60.0, 61.0, 60.5), (60.2,) * 3,
                                           (0.0,) * 3)},
        "short": {"about": "", "caps": caps((5.0, 4.0, 3.0), (3.0, 2.5, 2.0),
                                            (1.0, 0.5, 0.0))},
    }
    ident = sim.summarize(scen)["waste_identity"]
    even = ident["even"]["12"]
    assert even["unserved_max"] == 0.0
    assert even["unused_cap_range"] == pytest.approx(0.0)
    assert even["waste_total_range"] == pytest.approx(1.0)
    assert even["identical"] == "yes"
    short = ident["short"]["12"]
    assert short["unserved_max"] == pytest.approx(1.0)
    assert short["unused_cap_range"] == pytest.approx(1.0)
    assert short["identical"] == "no"
    text = sim.render_summary(sim.summarize(scen))
    assert ("total waste equals capacity minus served demand: identical "
            "across policies when unserved is 0") in text
    assert "60.20" in text


def test_default_summary_prints_the_waste_identity(default_text):
    assert default_text.count("total waste equals capacity minus served demand") == 1
    for name in sim.SCENARIOS:
        assert any(line.strip().startswith(f"{name} ") and "capacity-served" in line
                   for line in default_text.splitlines())


def test_reserve_at_burst_start_hand_checked():
    # Sampled at the given ticks: unused weekly quota of the fleet as a percent
    # of the fleet capacity. Tick 1: Pro at 50%, Max at 80% -> (0.5 + 1) / 6.
    accounts = [_pro(1, week_used=0.5, resets_at=WEEK_S),
                _max(5, week_used=4.0, resets_at=WEEK_S)]
    result = sim.simulate(accounts, [0.0] * 3, _policy("best"), active_id=1,
                          reserve_ticks={1})
    assert result.metrics()["reserve_at_burst_start"] == pytest.approx(1.5 / 6 * 100.0)
    none = sim.simulate([_pro(1, resets_at=WEEK_S)], [0.0], _policy("best"),
                        active_id=1).metrics()
    assert none["reserve_at_burst_start"] is None


def test_bursts_scenario_samples_the_reserve_once_per_measured_burst():
    result = sim.run_scenario("bursts", "best", sim.SEED)
    assert len(result.reserve_samples) == sim.MEASURED_WEEKS
    assert sim.run_scenario("light", "best", sim.SEED).reserve_samples == []


def test_summary_tie_is_not_a_win():
    scen = _fake_scenarios(3.0)
    for policies in scen["fake"]["caps"].values():
        for m in policies.values():
            m["unserved_pct"] = 0.0
    s = sim.summarize(scen)["claims"]["lowest_unserved"]["scenarios"]["fake"]
    assert s["per_cap"] == {"8": "tie", "12": "tie", "16": "tie"}
    assert s["holds_across_caps"] == "no"


# --- S2d: candidate margin sweep ---------------------------------------------


def test_margin_option_is_passed_to_balance(monkeypatch):
    real_decide = balance.decide
    seen = []

    def spy(*args, **kwargs):
        seen.append(kwargs.get("candidate_margin"))
        return real_decide(*args, **kwargs)

    monkeypatch.setattr(balance, "decide", spy)
    monkeypatch.setattr(sim, "SCENARIOS", {"tiny": TINY})
    with redirect_stdout(io.StringIO()):
        assert sim.main(["--json", "--cap", "12", "--margin", "7.5"]) == 0
    assert seen and set(seen) == {7.5}
    # The default margin is 0, and balanced always passes it on.
    seen.clear()
    accounts = [_pro(1, week_used=0.5, resets_at=WEEK_S), _pro(2)]
    sim.simulate(accounts, [0.01] * 4, _policy("balanced"), active_id=1)
    assert seen == [0.0] * 4
    seen.clear()
    sim.simulate([_pro(1), _pro(2)], [0.01] * 2, sim.make_policy("balanced", 10.0),
                 active_id=1)
    assert seen == [10.0] * 2


def test_margin_option_rejects_a_value_out_of_range():
    for bad in ("-1", "50.5", "nan"):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err), \
                pytest.raises(SystemExit):
            sim.main(["--cap", "12", "--margin", bad])
        assert "--margin must be" in err.getvalue(), bad


def test_sweep_runs_the_four_margins(monkeypatch):
    real_decide = balance.decide
    seen = set()

    def spy(*args, **kwargs):
        seen.add(kwargs.get("candidate_margin"))
        return real_decide(*args, **kwargs)

    monkeypatch.setattr(balance, "decide", spy)
    monkeypatch.setattr(sim, "SCENARIOS", {"tiny": TINY})
    out = io.StringIO()
    with redirect_stdout(out):
        assert sim.main(["--cap", "12", "--sweep-margin"]) == 0
    assert seen == {0.0, 5.0, 10.0, 15.0}
    assert sim.SWEEP_MARGINS == (0.0, 5.0, 10.0, 15.0)
    text = out.getvalue()
    rows = [line.split()[0] for line in text.splitlines()
            if line.startswith("  ") and line.split()
            and line.split()[0] in {"0", "5", "10", "15", "best", "consume-first"}]
    assert rows == ["0", "5", "10", "15", "best", "consume-first"]
    header = next(line for line in text.splitlines()
                  if line.strip().startswith("margin "))
    assert header.split() == ["margin", *sim.SWEEP_METRICS]
    # Changed in S2f (flaps_pf added): the sweep shows both flap columns.
    assert sim.SWEEP_METRICS == ("flaps", "flaps_pf", "switches_per_day", "blocks",
                                 "unserved_pct", "pace_dev_mean", "waste_pro")


def test_cap_alone_runs_no_sweep():
    data = sim.run_all(sim.SEED, (0.12,), sweep=False)
    assert "sweep" not in data


def test_default_run_prints_the_sweep(full_run, default_text):
    sweep = full_run["sweep"]
    assert sweep["margins"] == ["0", "5", "10", "15"]
    assert set(sweep["scenarios"]) == set(sim.SCENARIOS)
    for per_cap in sweep["scenarios"].values():
        assert list(per_cap) == full_run["caps"]
        for row in per_cap.values():
            assert list(row["balanced"]) == sweep["margins"]
            # margin 0 is the main balanced run (default --margin 0)
    for name in sim.SCENARIOS:
        for cap in full_run["caps"]:
            assert sweep["scenarios"][name][cap]["balanced"]["0"] == {
                k: full_run["scenarios"][name]["caps"][cap]["balanced"][k]
                for k in sim.SWEEP_METRICS}
    lines = [line for line in default_text.splitlines()
             if line.strip().startswith("smallest margin")]
    assert len(lines) == len(sim.SCENARIOS) * len(full_run["caps"])


def _sweep_rows(base, by_margin):
    """Fake sweep rows: margin label -> metrics, from (flaps_pf, blocks, unserved).

    Changed in S2f (was "flaps"): the "smallest margin" rule reads flaps_pf.
    """
    keys = ("flaps_pf", "blocks", "unserved_pct")
    rows = {"0": dict(zip(keys, base))}
    for label, values in by_margin.items():
        rows[label] = dict(zip(keys, values))
    return rows


def test_smallest_margin_is_computed_from_the_data():
    # Margin 5 halves the flaps (40 -> 20) with equal blocks and unserved.
    five = _sweep_rows((40, 3, 1.0), {"5": (20, 3, 1.0), "10": (5, 2, 0.5),
                                      "15": (0, 2, 0.5)})
    assert sim.smallest_margin(five) == "5"
    # Margin 5 cuts too little (40 -> 21); 10 cuts but raises blocks past the
    # tolerance; 15 raises unserved past the tolerance: none qualifies.
    # Changed in S2e (was blocks 4 and unserved 1.2, now inside the tolerance).
    none = _sweep_rows((40, 3, 1.0), {"5": (21, 3, 1.0), "10": (10, 6, 1.0),
                                      "15": (0, 3, 1.6)})
    assert sim.smallest_margin(none) == "none"
    # No flaps at margin 0: nothing to cut.
    assert sim.smallest_margin(_sweep_rows((0, 0, 0.0), {"5": (0, 0, 0.0)})) == "none"
    # A None unserved (no demand) counts as 0.
    assert sim.smallest_margin(_sweep_rows((4, 0, None), {"5": (2, 0, None)})) == "5"

    sweep = {"margins": ["0", "5", "10", "15"], "scenarios": {
        "fake": {"12": {"balanced": five, "smallest_margin": "5"},
                 "16": {"balanced": none, "smallest_margin": "none"}}}}
    text = "\n".join(sim.render_margin_choice(sweep))
    assert "fake" in text and "12%: 5" in text and "16%: none" in text
    assert "at least 50 percent" in text


def test_smallest_margin_tolerates_a_small_cost():
    assert sim.UNSERVED_TOLERANCE_PCT == 0.5
    assert sim.BLOCKS_TOLERANCE == 2
    # Passes only because of the tolerance: +2 blocks and +0.5 unserved.
    edge = _sweep_rows((40, 3, 1.0), {"5": (20, 5, 1.5)})
    assert sim.smallest_margin(edge) == "5"
    # Fails because of the tolerance: one block more, or unserved just over.
    assert sim.smallest_margin(_sweep_rows((40, 3, 1.0), {"5": (20, 6, 1.0)})) == "none"
    assert sim.smallest_margin(_sweep_rows((40, 3, 1.0), {"5": (20, 3, 1.51)})) == "none"
    # The flap cut is not tolerated: 21 of 40 is not a cut of 50 percent.
    assert sim.smallest_margin(_sweep_rows((40, 3, 1.0), {"5": (21, 3, 1.0)})) == "none"
    # The smallest qualifying margin wins: 5 fails, 10 passes in the tolerance.
    rows = _sweep_rows((40, 3, 1.0), {"5": (20, 6, 1.0), "10": (8, 4, 1.3),
                                      "15": (0, 3, 1.0)})
    assert sim.smallest_margin(rows) == "10"
    sweep = {"margins": ["0", "5"], "scenarios": {"fake": {"12": {
        "balanced": edge, "smallest_margin": "5"}}}}
    text = "\n".join(sim.render_margin_choice(sweep))
    assert "0.5" in text and "2 blocks" in text


# --- S2e: the dominance leg of the no-return release ---------------------------


def _view(i, pct7, reset7, pct5=0.0):
    return sim.View(i, "pro", pct5, None, pct7, reset7, False)


def _barred_policy(left_headroom):
    """consume-first that moved 1 -> 2 with account 1 at ``left_headroom``."""
    policy = sim.EnginePolicy(consume_first=True)
    policy.last_from, policy.last_to = 1, 2
    policy.left_headroom = left_headroom
    policy.last_switch_at = -10_000.0  # no cooldown at now = 0
    return policy


def test_dominance_leg_releases_the_no_return_bar():
    from claude_swap import autoswitch
    assert autoswitch.HORIZON_HEADROOM_RATIO == 2.0
    assert autoswitch.SPENT_HEADROOM_PCT == 3.0
    # Account 1 was left at 60 headroom and still has 60: no headroom leg.
    # Active 2 has 15 headroom. By hand, the engine formula
    # h > active * 2.0 + 3.0 is 60 > 33: True, so the bar is released
    # (and the ratio release 60 >= 15 * 2 holds).
    left, active = _view(1, 40.0, 10 * 3600.0), _view(2, 85.0, 100 * 3600.0)
    by_id = {1: left, 2: active}
    policy = _barred_policy(60.0)
    assert sim._dominates(60.0, 15.0) is True
    assert policy._no_return("consume-first", 2, by_id) == (None, True)
    # consume-first moves back to account 1, the sooner reset.
    assert policy.choose([left, active], 2, 0.0) == 1


def test_dominance_leg_does_not_release_when_the_active_is_close():
    # Active 2 has 30 headroom. By hand: 60 > 30 * 2.0 + 3.0 = 63 is False,
    # and 60 >= 60 + 3.0 is False: account 1 stays barred.
    left, active = _view(1, 40.0, 10 * 3600.0), _view(2, 70.0, 100 * 3600.0)
    by_id = {1: left, 2: active}
    policy = _barred_policy(60.0)
    assert sim._dominates(60.0, 30.0) is False
    assert policy._no_return("consume-first", 2, by_id) == (1, False)
    assert policy.choose([left, active], 2, 0.0) is None
    # The boundary is strict, as in the engine: 63 > 63 is False.
    assert sim._dominates(63.0, 30.0) is False
    assert sim._dominates(63.01, 30.0) is True


def test_dominance_leg_changes_consume_first_in_the_expected_direction(monkeypatch):
    # The leg releases the bar onto an account that dominates the active, so
    # consume-first serves more: unserved demand never rises and falls in the
    # heavy scenario. best does not change (its runs never reach the leg with
    # a different outcome).
    new = {(n, s): sim.run_scenario(n, p, sim.SEED, s).metrics()
           for n in ("heavy", "light") for s in (0.08, 0.12)
           for p in ("consume-first",)}
    best_new = sim.run_scenario("heavy", "best", sim.SEED, 0.12).metrics()
    monkeypatch.setattr(sim, "_dominates", lambda *_: False)
    old = {(n, s): sim.run_scenario(n, "consume-first", sim.SEED, s).metrics()
           for n, s in new}
    best_old = sim.run_scenario("heavy", "best", sim.SEED, 0.12).metrics()
    for key in new:
        assert new[key]["unserved_pct"] <= old[key]["unserved_pct"], key
        assert new[key] != old[key], key
    for s in (0.08, 0.12):
        assert new["heavy", s]["unserved_pct"] < old["heavy", s]["unserved_pct"]
        assert new["heavy", s]["blocks"] < old["heavy", s]["blocks"]
    assert best_new == best_old


def test_waste_total_is_not_ranked(full_run):
    assert "waste_total" in sim.NOT_RANKED
    assert "waste_total" in sim.METRICS  # still in the table
    for metrics in full_run["summary"]["best_policy"].values():
        assert "waste_total" not in metrics
        assert "waste_pro" in metrics
    text = sim.render_summary(full_run["summary"])
    assert "edge noise" in text


# --- S2f: one flap rule for every policy; flaps_pf for balanced ---------------


def _flap_log():
    S = sim.Switch
    # 1 -> 2 proactive, then forced out of 2 within 10 min (one flap by both
    # rules); 3 -> 1 at 2000 s, then 1 -> 2 at 2300 s (an "any" flap only).
    return [S(0.0, 1, 2, "proactive"), S(300.0, 2, 3, "threshold"),
            S(2000.0, 3, 1, "proactive"), S(2300.0, 1, 2, "proactive")]


def test_main_flap_metric_uses_one_rule_for_every_policy():
    counts = {}
    pf = {}
    for name in sim.POLICIES:
        policy = sim.make_policy(name)
        result = sim.Result(switch_log=_flap_log(), flap_rule=policy.flap_rule)
        m = result.metrics()
        counts[name], pf[name] = m["flaps"], m["flaps_pf"]
    assert counts == {p: 2 for p in sim.POLICIES}
    assert counts["balanced"] == sim.count_flaps(_flap_log(), sim.FLAP_RULE_ANY)
    # flaps_pf: balanced only, proactive then forced out of the same account.
    assert pf == {"best": None, "consume-first": None, "balanced": 1}


def test_flaps_pf_counts_only_proactive_then_forced():
    S = sim.Switch
    policy = sim.make_policy("balanced")
    for log, want in (
        ([S(0.0, 1, 2, "proactive"), S(600.0, 2, 3, "threshold")], 1),
        ([S(0.0, 1, 2, "proactive"), S(300.0, 2, 3, "proactive")], 0),
        ([S(0.0, 1, 2, "threshold"), S(300.0, 2, 3, "threshold")], 0),
        ([S(0.0, 1, 2, "proactive"), S(900.0, 2, 3, "threshold")], 0),
    ):
        m = sim.Result(switch_log=log, flap_rule=policy.flap_rule).metrics()
        assert m["flaps_pf"] == want, log
    # The column is "-" in the table for the other policies.
    assert sim._fmt(None) == "-"


def test_flaps_pf_is_not_ranked(full_run, default_text):
    assert "flaps_pf" in sim.NOT_RANKED
    assert "flaps_pf" in sim.METRICS and "flaps" not in sim.NOT_RANKED
    for metrics in full_run["summary"]["best_policy"].values():
        assert "flaps_pf" not in metrics
        assert "flaps" in metrics
    for policies in full_run["scenarios"]["light"]["caps"].values():
        assert policies["best"]["flaps_pf"] is None
        assert policies["consume-first"]["flaps_pf"] is None
        assert policies["balanced"]["flaps_pf"] is not None
    assert sim.SWEEP_FLAP_METRIC == "flaps_pf"
    assert "cuts flaps_pf by at least" in default_text


def test_smallest_margin_reads_flaps_pf_not_flaps():
    rows = {"0": {"flaps": 0, "flaps_pf": 10, "blocks": 0, "unserved_pct": 0.0},
            "5": {"flaps": 50, "flaps_pf": 4, "blocks": 0, "unserved_pct": 0.0}}
    assert sim.smallest_margin(rows) == "5"


# --- S2f: the no-return bar applies to balanced --------------------------------


def _bar_case(policy, left_pct7, active_pct7, left_headroom):
    """balanced moved 1 -> 2 with account 1 at ``left_headroom``; now = 0."""
    policy.last_from, policy.last_to = 1, 2
    policy.left_headroom = left_headroom
    left = _view(1, left_pct7, 10 * 3600.0)
    active = _view(2, active_pct7, 100 * 3600.0)
    return [left, active]


def test_no_return_bar_blocks_balanced_switching_back():
    # Active 2 is over the line (91%, 9 points of headroom): a forced switch
    # with trigger "proactive". Account 1 has 20 points, as at departure.
    # By hand: dominance 20 > 9 * 2 + 3 = 21 is False; headroom leg 20 >= 23
    # is False: not recovered, account 1 stays barred.
    barred = sim.BalancedPolicy(10.0)
    views = _bar_case(barred, 80.0, 91.0, 20.0)
    by_id = {v.id: v for v in views}
    assert sim.balanced_trigger(by_id[2]) == "proactive"
    assert sim.no_return_bar(barred, "proactive", 2, by_id) == (1, False)
    assert barred.choose(views, 2, 0.0) is None
    # Without the bar the same case switches back at once.
    free = sim.BalancedPolicy(10.0, bar=False)
    views = _bar_case(free, 80.0, 91.0, 20.0)
    assert free.choose(views, 2, 0.0) == 1


def test_no_return_bar_blocks_a_below_line_balanced_return():
    # Active 2 is usable (50%, reset 100 h out); account 1 (60%, reset 10 h
    # out) is far more urgent, so balanced would move back proactively.
    # 40 > 50 * 2 + 3 is False and 40 >= 43 is False: barred.
    policy = sim.BalancedPolicy(0.0, bar=False)
    views = _bar_case(policy, 60.0, 50.0, 40.0)
    assert sim.balanced_trigger(views[1]) == sim.BALANCED_TRIGGER
    assert policy.choose(views, 2, 0.0) == 1
    policy = sim.BalancedPolicy(0.0)
    views = _bar_case(policy, 60.0, 50.0, 40.0)
    assert policy.choose(views, 2, 0.0) is None


def test_no_return_bar_releases_for_balanced_like_the_engine():
    # Ratio release: dominance 20 > 8 * 2 + 3 = 19, and 20 >= 8 * 2.
    policy = sim.BalancedPolicy(10.0)
    views = _bar_case(policy, 80.0, 92.0, 20.0)
    assert sim.no_return_bar(policy, "proactive", 2,
                             {v.id: v for v in views}) == (None, True)
    assert policy.choose(views, 2, 0.0) == 1
    # Empty-ranking release: headroom leg 17 >= 14 + 3, but 17 < 9 * 2, so
    # the bar holds; the ranking without account 1 is empty and account 1
    # recovered, so the bar is released and balanced moves back.
    policy = sim.BalancedPolicy(10.0)
    views = _bar_case(policy, 83.0, 91.0, 14.0)
    assert sim.no_return_bar(policy, "proactive", 2,
                             {v.id: v for v in views}) == (1, True)
    assert policy.choose(views, 2, 0.0) == 1
    # The same with a third account below the line: the ranking is not
    # empty, so the bar holds and balanced goes to account 3.
    policy = sim.BalancedPolicy(10.0)
    views = [*_bar_case(policy, 83.0, 91.0, 14.0), _view(3, 50.0, 150 * 3600.0)]
    assert policy.choose(views, 2, 0.0) == 3
    # At the limit (blocked) the bar does not apply, as in the engine.
    policy = sim.BalancedPolicy(10.0)
    left = _view(1, 80.0, 10 * 3600.0)
    blocked = sim.View(2, "pro", 0.0, None, 100.0, 100 * 3600.0, True)
    policy.last_from, policy.last_to, policy.left_headroom = 1, 2, 20.0
    assert sim.balanced_trigger(blocked) == "at-limit"
    assert policy.choose([left, blocked], 2, 0.0) == 1


def test_no_return_bar_is_the_same_function_for_every_policy(monkeypatch):
    real = sim.no_return_bar
    seen = set()

    def spy(policy, *args):
        seen.add(type(policy).__name__)
        return real(policy, *args)

    monkeypatch.setattr(sim, "no_return_bar", spy)
    for name in sim.POLICIES:
        sim.run_scenario("heavy", name, sim.SEED, 0.12)
    assert seen == {"EnginePolicy", "BalancedPolicy"}


def test_balanced_records_the_last_switch_for_the_bar():
    accounts = [
        _pro(1, week_used=0.1, resets_at=6 * 24 * 3600.0),
        _pro(2, week_used=0.1, resets_at=10 * 3600.0),
    ]
    policy = _policy("balanced")
    sim.simulate(accounts, [0.0] * 3, policy, active_id=1)
    assert (policy.last_from, policy.last_to) == (1, 2)
    assert policy.left_headroom == pytest.approx(90.0)


def test_no_bar_option_reaches_balanced(monkeypatch):
    monkeypatch.setattr(sim, "SCENARIOS", {"tiny": TINY})
    seen = []
    real = sim.BalancedPolicy.__init__

    def spy(self, *args, **kwargs):
        real(self, *args, **kwargs)
        seen.append(self.bar)

    monkeypatch.setattr(sim.BalancedPolicy, "__init__", spy)
    out = io.StringIO()
    with redirect_stdout(out):
        assert sim.main(["--json", "--cap", "12", "--no-bar-for-balanced"]) == 0
    assert seen and set(seen) == {False}
    assert json.loads(out.getvalue())["balanced_no_return_bar"] is False
    seen.clear()
    with redirect_stdout(io.StringIO()):
        assert sim.main(["--json", "--cap", "12"]) == 0
    assert seen and set(seen) == {True}
    assert sim.BALANCED_NO_RETURN_BAR is True
