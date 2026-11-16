"""Rotation simulator v2: a Monte Carlo test bench for account-rotation rules.

A standalone tool, not part of the product (spec: docs/superpowers/specs/
2026-11-15-adaptive-rotation-design.md, "Plan: simulator v2", task R1). It
simulates a fleet of Claude accounts over many simulated weeks with a wide
family of usage curves, and scores three simple policies. It uses a fake clock
only: it never reads a home folder, a credential, a real store or the
network, and it writes no file. The same seed and arguments always give the
same bytes, with any ``--jobs``.

    uv run python tools/rotation_sim.py --preset user_like --policy all
    uv run python tools/rotation_sim.py --preset all --weeks 4 --json
    uv run python tools/rotation_sim.py --random 2000 --policy all --jobs 16
    uv run python tools/rotation_sim.py --random 1000 --policy deficit --jobs 12 \\
        --sweep "deficit.margin_pts=2,4,8;deficit.dwell_s=900,3600"
    uv run python tools/rotation_sim.py --diagnose-steady
    uv run python tools/rotation_sim.py --random 2000 --policy all --jobs 12 --judge
    uv run python tools/rotation_sim.py --random 1000 --policy scaled_line --score gaps --jobs 12 \\
        --sweep "scaled_line.margin_pts=4,8,16;scaled_line.dwell_s=900,3600"

MODEL
-----
* Time is in 5-minute ticks. A week is 2016 ticks. Tick 0 is a Monday 00:00 of
  a fake calendar. Policies receive ``now`` in seconds since tick 0.
* Account: a capacity ``c`` (any positive number; the plans are pro 1, max 5,
  ultra 10, max20 20 weekly units), a FIXED weekly reset moment (an offset in
  the week, repeating every 7 days, used or not; at the reset the unused quota
  is lost and the account returns to 0 used), and an optional 5-hour window.
  An offset of 0 first resets at tick 2016.
* 5-hour window: it STARTS at the first use after it is closed, lasts exactly
  5 hours (60 ticks), has a cap of ``five_hour_share * c`` units, and then
  closes (while idle it has no reset time). ``five_hour_share`` is per account;
  ``None`` means the account has NO 5-hour limit. ``INHERIT`` (the default of
  an account) means the share of the run (``--five-hour-share``, default 0.12).
* An account is blocked when its weekly use reaches ``c`` or its open 5-hour
  window reaches its cap. It unblocks at the matching reset.
* One account is active at a time. Demand arrives per tick in capacity units.
  The active account serves it up to its headroom. The rest is unserved (a
  stall) and is NOT queued. The policy is called on each tick with demand, so
  an account that fills up in the middle of a tick leaves a remainder that is
  unserved (no re-choice inside a tick).
* Switch cost: the first tick with demand on a new account (not the first
  activation of the run) burns ``switch_cost_factor`` times the demand (default
  1.5, the prompt cache miss). The extra units are charged to the account and
  are not served work: served + unserved = demand always.
* Readings: the policy sees READINGS, not the truth. The reading of the active
  account is fresh on each tick. The other accounts are polled one candidate
  per tick, round robin, so their reading is as of the last poll. During a
  scripted outage no reading updates, not even the active one. Readings are
  percents rounded to 6 decimals. A policy is a pure function
  ``choose(readings, active_id, now, state) -> account_id`` (do not mutate
  ``readings``); ``state`` is its own small dict.
* An account can be disabled or added in the middle of the run. It counts in
  the metrics only for the resets at which it is enabled (enabled for ticks
  ``start <= t < end``; a reset at tick ``end`` still counts).

METRICS (a run = warm-up of 1 week, not measured, then N measured weeks)
-----------------------------------------------------------------------
* Resets measured: those with ``tick > warm-up ticks`` and ``tick <= run
  length`` of an enabled account. Demand, stall, switches and flaps are
  measured on the ticks after the warm-up.
* ``waste``: capacity units unused at each measured reset, as a percent of the
  capacity of those resets (``waste_pro``, ``waste_max``, ``waste_ultra``,
  ``waste_max20`` and ``waste_custom``: the same, for the accounts of that plan
  only; None if the fleet has none).
* Split (both as a percent of the same total capacity, so they add up to
  ``waste``): for an account-week with unused quota U, X is the unserved demand
  (fleet wide, any active account) on the ticks of that week at which the
  account still had weekly headroom, including when its own 5-hour window was
  full. ``waste_avoidable`` = min(U, X): work was waiting and this account
  could have served it by its week, or the 5-hour cap kept it from draining.
  ``waste_no_demand`` = U - min(U, X). Demand that other accounts served is NOT
  counted as avoidable (that is the price of rotation, not a stall).
* ``stall`` = unserved demand as a percent of demand; ``stall_hours`` = ticks
  with unserved demand times 5 minutes; ``served`` = served demand percent.
* ``switches_per_day``; ``flaps`` = number of pairs of consecutive switches
  at most 10 minutes (2 ticks) apart.
* ``unevenness``: for each k, the population standard deviation, across the
  accounts, of the percent used at the k-th measured reset of each account;
  the mean of these over k (needs 2 accounts). Lower is more uniform.
* ``max_used_while_low``: for each measured reset of an account that ended
  below 20 percent used, the largest weekly percent that any OTHER account
  reached since that account's previous reset; the maximum of those (None if
  no reset ended below 20 percent).

ROUND 2 (task R1b) ADDITIONS
----------------------------
* Switch cost and idle gaps: the switch cost factor applies only if the
  previous SERVED work (on any account) ended less than ``cache_ttl_s``
  (default 3600 s, ``--cache-ttl-s``) before the switch. After a longer idle
  gap the switch is free (the prompt cache is cold anyway). ``paid_switches_per_day``
  and ``free_switches_per_day`` split ``switches_per_day``.
* Volume bands: ``volume_share`` = measured demand / the weekly capacity of the
  enabled accounts (summed over the measured weeks); ``light`` below 0.5,
  ``medium`` 0.5 to 0.9, ``heavy`` 0.9 to 1.1, ``over`` above 1.1.
* ``stall_unavoidable``: per measured week, max(0, demand of the week - the
  weekly capacity of the accounts enabled at any time in that week), summed,
  as a percent of the demand; ``stall_excess`` = ``stall`` - ``stall_unavoidable``.
  It is only a lower bound of what no rule can avoid: it ignores the 5-hour
  caps and the reset timing, and it cuts the run in calendar weeks while the
  accounts reset at other moments (an account that resets inside a calendar
  week can serve more than one capacity in it). So ``stall_excess`` can be
  slightly negative and it is not a measure of the best possible rule.
* Policy parameters (``--param deficit.margin_pts=8``, repeatable), a
  ``--sweep`` grid with a train half and a held-out half, the ``pressure``
  policy, and ``--diagnose-steady`` (see ``--help``).
* A reading also carries ``five_share`` (the 5-hour share of that account, None
  for no limit): the product learns it from its own readings; here it is exact
  (``pressure.q_scale`` models an error of the belief).

R2 (THE JUDGE) ADDITIONS
------------------------
Two references per scenario, standard library only (``--judge``; ``--score gaps``
turns it on for a sweep):
* ``bound_upper(scn)``: the most demand any rule could serve when the 5-hour caps
  and the switch cost are ignored and several accounts may serve in one tick
  (earliest deadline first; optimal for that relaxation). Gives ``stall_floor``
  and ``waste_floor``. See its docstring for the proof and the warm-up rule.
* ``oracle(scn)``: an achievable clairvoyant policy (receding-horizon greedy that
  sees the truth and the future demand and obeys every rule of the engine). Its
  cost is in percent points of the measured demand (unserved) and of the measured
  capacity (waste and switch burn), so it weighs the two like ``stall + waste``.
  Parameters ``oracle.horizon_h`` (12, fine part), ``oracle.step_s`` (900) and
  ``oracle.tail_h`` (156, coarse part, so every account's next reset is seen).
  The truth lies between the oracle and the bound. A row ``oracle (ref)`` is
  listed in every table; it is not a real policy.
* Metrics ``JUDGE_METRICS``: ``stall_floor``, ``waste_floor``, ``oracle_stall``,
  ``oracle_waste`` and the gaps ``stall_gap_upper = stall - stall_floor``,
  ``stall_gap_oracle = stall - oracle_stall``, ``waste_gap_upper``,
  ``waste_gap_oracle``. A negative gap to the oracle means that the policy beat
  the oracle on that one metric by giving up the other.

SIMPLIFICATIONS: one tick is the unit; no queue; the policy cannot re-choose
inside a tick; the poll order is round robin; demand is in fleet capacity
units, so every percent metric is scale invariant.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json
import math
import multiprocessing
import os
import random
import sys
from collections import namedtuple
from dataclasses import dataclass, field
from types import MappingProxyType

# --------------------------------------------------------------------------
# Constants (named; no business number is a bare literal in the code below)
# --------------------------------------------------------------------------

TICK_S = 300
TICKS_PER_HOUR = 12
TICKS_PER_DAY = 24 * TICKS_PER_HOUR
WEEK_TICKS = 7 * TICKS_PER_DAY
WEEK_S = WEEK_TICKS * TICK_S
WINDOW_TICKS = 5 * TICKS_PER_HOUR  # the 5-hour window
FLAP_TICKS = 2  # 10 minutes
LOW_PCT = 20.0  # "reset below 20 percent"
WARMUP_WEEKS = 1
DEFAULT_WEEKS = 4
DEFAULT_FIVE_HOUR_SHARE = 0.12
DEFAULT_SWITCH_COST = 1.5
DEFAULT_SEED = 1
EPS_REL = 1e-9  # relative tolerance of "at the limit"
PCT_DECIMALS = 6
DEFAULT_CACHE_TTL_S = 3600  # the prompt cache lifetime
WINDOW_HOURS = 5.0
WEEK_HOURS = 168.0
SECONDS_PER_HOUR = 3600.0

PLAN_CAPACITY = {"pro": 1.0, "max": 5.0, "ultra": 10.0, "max20": 20.0}
CUSTOM_PLAN = "custom"

# Policy parameters (named, at the top).
USABLE_BELOW_PCT = 100.0  # an account at or over this percent is blocked
THRESHOLD_BEST_PCT = 90.0
DEFICIT_MARGIN_PTS = 4.0
DEFICIT_DWELL_S = 1800
WAIT_FOR_IDLE_S = 0  # 0 = off: do not wait for an idle gap before a switch
PRESSURE_RATIO = 1.15  # the best score must beat the active one by this factor
PRESSURE_DWELL_S = 1800
PRESSURE_Q_SCALE = 1.0  # the policy's belief of the 5-hour share / the true one
# An account with no 5-hour limit can drain 1 / floor of its week per hour: so
# large that its "can" never limits the reachable remaining quota.
PRESSURE_DWELL_FLOOR_H = 0.05

# Round 3 policies.
SCALED_F_MIN = 0.1  # lowest line height (share of the account's quota at its reset)
SCALED_F_BOOST = 1.1  # a rising demand is not capped by its own past
SCALED_GUARD = 1.0  # loss guard on (0 = off)
SCALED_GUARD_HOURS = 24.0  # an account whose reset is closer than this can be guarded
SCALED_GUARD_F = 0.9  # the guard works only at or above this f
SCALED_SPAN_FLOOR_S = 24 * 3600  # shortest span used to rate the consumption
GUARD_MIN_FRAC = 0.01  # reachable quota below this share of the capacity is not worth a guard
F_MODE_TRAILING = "trailing"  # f from the consumption observed in the trailing 7 days
F_MODE_STATELESS = "stateless"  # f from the current readings only
SCALED_F_MODE = F_MODE_TRAILING
# stateless f: below this elapsed share of the fleet week (all accounts just reset) keep the last f
SCALED_STATELESS_FLOOR = 0.01

# R2: the judge.
INF_TICK = 1 << 60  # "never": the end of an account that is never disabled
# ``oracle.*`` parameters: ``horizon_h`` is the fine part of the forward simulation, ``tail_h`` a coarse
# extension after it (so that the next reset of every account is inside the look-ahead: a week is 168 h).
ORACLE_DEFAULTS = {"horizon_h": 12.0, "step_s": 900, "tail_h": 156.0}
ORACLE_BLOCK_TICKS = 3  # step of the fine forward simulation (15 minutes)
ORACLE_TAIL_BLOCK_TICKS = 36  # step of the coarse tail (3 hours)
ORACLE_TIE_REL = 1e-9  # a cost must beat the active one by this share of the fleet capacity
ORACLE_LABEL = "oracle (ref)"  # the row of the tables: a reference, not a real policy
JUDGE_METRICS = (
    "stall_floor",
    "waste_floor",
    "oracle_stall",
    "oracle_waste",
    "stall_gap_upper",
    "stall_gap_oracle",
    "waste_gap_upper",
    "waste_gap_oracle",
)
SCORE_STALL_GAP = 1.0
SCORE_WASTE_GAP = 1.0

# Volume bands: share of the fleet capacity (upper edge inclusive only for heavy).
BANDS = ("light", "medium", "heavy", "over")
BAND_MEDIUM_FROM = 0.5
BAND_HEAVY_FROM = 0.9
BAND_OVER_ABOVE = 1.1

# Sweep: the score to minimise, a weighted sum (all weights are named).
SCORE_STALL_EXCESS = 1.0
SCORE_AVOIDABLE_WASTE = 1.0
SCORE_UNEVENNESS = 0.5
SCORE_UNEVENNESS_SCALE = 10.0
SCORE_PAID_SWITCHES = 0.2
SWEEP_TOP = 5  # rows shown, and rows re-evaluated on the held-out half
SWEEP_KEYS = (
    "stall_excess", "waste_avoidable", "unevenness", "paid_switches_per_day",
    "stall", "switches_per_day", "free_switches_per_day", "waste",
)

# The sensitivity mode: the real 5-hour share against the belief of it.
SENS_SHARES = (0.06, 0.12, 0.25)
SENS_BELIEFS = (0.5, 1.0, 2.0)
SENS_COLUMNS = ("stall_excess", "waste_avoidable", "unevenness", "paid_switches_per_day")

# The steady-state diagnosis: constant light load on a staggered fleet.
STEADY_LOAD = 0.3
STEADY_WEEKS = 12
STEADY_PLANS = ("pro", "max", "max20")
STEADY_OFFSETS = (0, 700, 1500)
STEADY_LOADS = (STEADY_LOAD, 0.9)
STEADY_POLICIES = ("deficit", "pressure", "lowest_pct", "scaled_line")
STEADY_SKIP_WEEKS = 2  # weeks of start-up left out of the steady means

# Generator ranges (every parameter of a random scenario is drawn from these).
RANGES = {
    "volume": (0.05, 1.5),  # weekly demand / fleet weekly capacity, log-uniform
    "growth": (0.3, 3.0),  # week to week factor, log-uniform
    "burst_ratio": (3.0, 30.0),  # p99 / median of the active ticks, log-uniform
    "active_prob": (0.3, 1.0),  # chance that a tick is active
    "conc_chance_per_hour": (0.0, 0.15),
    "conc_minutes": (10, 90),
    "gap_hours": (12, 72),
    "outage_minutes": (5, 120),
    "fleet_size": (2, 8),
    "custom_capacity": (0.5, 30.0),  # log-uniform
    "own_share": (0.06, 0.25),
    "idle_block_prob": (0.0, 1.0),  # chance that a day has an idle block
    "idle_block_hours": (0.0, 14.0),  # length of the idle block of a scenario
    "idle_days_per_week": (0, 3),  # whole idle days in each week
    "idle_block_anchor_h": (0.0, 24.0),  # hour of the day where the block starts
}
IDLE_JITTER_H = 2.0  # the start of the daily block moves by up to this many hours
CONC_MULTIPLIERS = (2, 3, 10)
CONC_WEIGHTS = (6, 3, 1)
REGIME_FACTORS = (10.0, 0.1)
P_GROWTH = 0.5  # chance that a scenario has growth or decay
P_GAPS = 0.4
P_REGIME = 0.2
P_OUTAGE = 0.5
P_FLEET_EVENT = 0.2  # an account disabled or added in the middle of a week
P_CLUSTERED = 0.5
P_BIG_AND_SMALL = 0.15  # one 20x account with several 1x accounts
P_MULTI_LARGE = 0.10  # two or three 20x accounts (the rest pro or max)
MULTI_LARGE_COUNTS = (2, 3)
MULTI_LARGE_REST = (("pro", 3), ("max", 1))
P_NO_FIVE = 0.15  # an account with no 5-hour limit
P_OWN_SHARE = 0.15  # an account with its own 5-hour share
PLAN_WEIGHTS = (("pro", 40), ("max", 25), ("ultra", 10), ("max20", 15), (CUSTOM_PLAN, 10))
WEEK_VOLUME_CLAMP = (0.01, 3.0)
OFF_HOURS_WEIGHT = 0.05
Z99 = 2.3263478740408408  # 99th percentile of the standard normal

DAY_SHAPES = ("flat", "weekdays_heavy", "weekend_heavy", "giant_days", "ramp_up", "ramp_down")
HOUR_SHAPES = ("flat", "work_hours", "night", "two_blocks")

INHERIT = "inherit"  # five_hour_share of an account: use the share of the run

Reading = namedtuple(
    "Reading", "week_pct five_pct reset_at capacity as_of five_share", defaults=(None,)
)
AccountSpec = namedtuple(
    "AccountSpec", "plan capacity offset start end five_hour_share", defaults=(0, None, INHERIT)
)


def account(plan, offset=0, start=0, end=None, five_hour_share=INHERIT, capacity=None):
    """Build an AccountSpec; ``capacity`` overrides the plan table (custom)."""
    cap = PLAN_CAPACITY[plan] if capacity is None else float(capacity)
    if cap <= 0:
        raise ValueError("capacity must be positive")
    return AccountSpec(plan, cap, offset, start, end, five_hour_share)


@dataclass
class Scenario:
    accounts: list
    demand: list  # capacity units per tick, length (warmup + weeks) * WEEK_TICKS
    outages: list = field(default_factory=list)  # [(start_tick, end_tick)) half open
    warmup_weeks: int = WARMUP_WEEKS
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Account state: the truth
# --------------------------------------------------------------------------


class AccountState:
    """The true state of one account. ``five_cap`` None = no 5-hour limit."""

    __slots__ = ("id", "cap", "share", "five_cap", "eps", "used", "win_start", "win_used", "next_reset")

    def __init__(self, ident, capacity, five_hour_share, next_reset=WEEK_TICKS):
        self.id = ident
        self.cap = capacity
        self.share = five_hour_share
        self.five_cap = None if five_hour_share is None else five_hour_share * capacity
        self.eps = EPS_REL * capacity
        self.used = 0.0
        self.win_start = -1
        self.win_used = 0.0
        self.next_reset = next_reset

    def window_open(self, t):
        return self.five_cap is not None and self.win_start >= 0 and t < self.win_start + WINDOW_TICKS

    def five_reset_tick(self, t):
        """Tick at which the open window closes; None while idle (no window)."""
        return self.win_start + WINDOW_TICKS if self.window_open(t) else None

    def headroom(self, t):
        room = self.cap - self.used
        if self.five_cap is not None:
            five = self.five_cap - self.win_used if self.window_open(t) else self.five_cap
            if five < room:
                room = five
        return room if room > self.eps else 0.0

    def serve(self, t, units):
        """Charge ``units`` of quota (already within headroom)."""
        if self.five_cap is not None:
            if not self.window_open(t):
                self.win_start = t
                self.win_used = 0.0
            self.win_used += units
        self.used += units

    def reset_week(self):
        self.used = 0.0
        self.next_reset += WEEK_TICKS

    def reading(self, t):
        five = 0.0
        if self.window_open(t):
            five = round(100.0 * self.win_used / self.five_cap, PCT_DECIMALS)
        return Reading(
            round(100.0 * self.used / self.cap, PCT_DECIMALS),
            five,
            self.next_reset * TICK_S,
            self.cap,
            t * TICK_S,
            self.share,
        )


# --------------------------------------------------------------------------
# Policies
# --------------------------------------------------------------------------

Policy = namedtuple("Policy", "name choose make_state")


def _blocked(r):
    return r.week_pct >= USABLE_BELOW_PCT or r.five_pct >= USABLE_BELOW_PCT


def _remaining_s(reading, now):
    """Seconds to the next weekly reset, in (0, WEEK_S]; fixed schedule."""
    return (reading.reset_at - now - 1) % WEEK_S + 1


def _headroom_pct(r):
    return 100.0 - max(r.week_pct, r.five_pct)


def threshold_best_choose(readings, active_id, now, state):
    """Stay until the active reading is at or over a threshold in any window.

    Then go to the account with the most headroom (percent). This is the
    current engine's `best`, simplified: no per-account policy, no cooldown.
    """
    threshold = state["threshold"]
    cur = readings.get(active_id)
    if cur is not None and max(cur.week_pct, cur.five_pct) < threshold:
        return active_id
    cur_room = _headroom_pct(cur) if cur is not None else -math.inf
    best, best_room = active_id, cur_room
    for ident in sorted(readings):
        room = _headroom_pct(readings[ident])
        if room > best_room:
            best, best_room = ident, room
    return best


def earliest_reset_choose(readings, active_id, now, state):
    """Use the account with headroom whose weekly reset is soonest."""
    best, best_key = None, None
    for ident in sorted(readings):
        r = readings[ident]
        if _blocked(r):
            continue
        key = (_remaining_s(r, now), ident != active_id)
        if best_key is None or key < best_key:
            best, best_key = ident, key
    if best is None:
        return active_id if active_id in readings else (min(readings) if readings else None)
    return best


def _deficit(r, now):
    elapsed_pct = 100.0 * (1.0 - _remaining_s(r, now) / WEEK_S)
    return elapsed_pct - r.week_pct


def idle_gap_s(prev_now, now):
    """Seconds between the end of the work of tick ``prev_now`` and ``now``."""
    return now - prev_now - TICK_S


def _gap_is_free(state, now):
    """True if the work stopped for at least the cache lifetime before ``now``."""
    prev = state.get("prev_now")
    if prev is None:
        return False
    return idle_gap_s(prev, now) >= state.get("cache_ttl_s", DEFAULT_CACHE_TTL_S)


def _firm_choice(readings, active_id, now, state, scores, beats):
    """The firmness layer shared by ``deficit`` and ``pressure``.

    ``scores`` maps each usable account to its score (higher is better);
    ``beats(best, current)`` says if the best score is far enough above the
    current one. Switch when the active account is blocked (at once), or when
    ``beats`` holds and the dwell is over. A switch that is not forced waits
    for an idle gap longer than the cache lifetime for at most
    ``wait_for_idle_s`` seconds (0 = do not wait).
    """
    if "since" not in state or state["last_active"] != active_id:
        state["last_active"], state["since"] = active_id, now
    cur = readings.get(active_id)
    best, best_s = None, None
    for ident in sorted(scores):
        sc = scores[ident]
        if best_s is None or sc > best_s or (sc == best_s and ident == active_id):
            best, best_s = ident, sc
    if best is None:
        choice = active_id if cur is not None else (min(readings) if readings else None)
    elif cur is None or _blocked(cur):
        choice = best
    else:
        want = (
            best != active_id
            and beats(best_s, scores[active_id])
            and now - state["since"] >= state["dwell_s"]
        )
        choice = active_id
        if not want:
            state.pop("want_since", None)
        elif state["wait_for_idle_s"] > 0 and not _gap_is_free(state, now):
            first = state.setdefault("want_since", now)
            if now - first >= state["wait_for_idle_s"]:
                choice = best
        else:
            choice = best
    if choice != active_id:
        state["last_active"], state["since"] = choice, now
        state.pop("want_since", None)
    state["prev_now"] = now
    return choice


def deficit_choose(readings, active_id, now, state):
    """Own straight line per account; take the largest deficit, with firmness.

    deficit = elapsed fraction of the account's own week * 100 - percent used
    (percent points, not capacity units). Switch only if the best deficit beats
    the active one by ``margin_pts`` and the active account has been active for
    ``dwell_s`` seconds, or when the active account is blocked.
    """
    scores = {i: _deficit(readings[i], now) for i in sorted(readings) if not _blocked(readings[i])}
    margin = state["margin_pts"]
    return _firm_choice(readings, active_id, now, state, scores, lambda best, cur: best > cur + margin)


def pressure_score(r, now, q_scale=PRESSURE_Q_SCALE, floor_h=PRESSURE_DWELL_FLOOR_H):
    """How hard an account must be drained to be used up by its reset.

    ``left`` = 1 - u (share of the week left); ``h`` = hours to the reset;
    ``can`` = q / 5 (share of the week the 5-hour window lets it deliver per
    hour; 1 / ``floor_h`` for an account with no 5-hour limit, so large that it
    never limits). The reachable remaining is min(left, can * h); ``need`` is
    reachable / h; score = need * 168 (1.0 = exactly on its straight line).
    The part that can no longer be drained is not counted.
    """
    left = max(0.0, 1.0 - r.week_pct / 100.0)
    hours = _remaining_s(r, now) / SECONDS_PER_HOUR
    if r.five_share is None:
        can = 1.0 / floor_h
    else:
        can = r.five_share * q_scale / WINDOW_HOURS
    reach = min(left, can * hours)
    return reach / hours * WEEK_HOURS


def pressure_choose(readings, active_id, now, state):
    """Pick the account with the highest pressure score; firm like ``deficit``.

    Switch when the best score exceeds the active score by the factor
    ``ratio`` and the dwell is over, or when the active account is blocked.
    """
    qs, fl = state["q_scale"], state["dwell_floor_h"]
    scores = {
        i: pressure_score(readings[i], now, qs, fl)
        for i in sorted(readings)
        if not _blocked(readings[i])
    }
    ratio = state["ratio"]
    return _firm_choice(readings, active_id, now, state, scores, lambda best, cur: best > cur * ratio)


def lowest_pct_choose(readings, active_id, now, state):
    """Use the usable account with the lowest weekly percent used; firm like ``deficit``.

    score = -percent used. The reset times play no part. Switch only if the
    best percent is lower than the active one by ``margin_pts`` and the dwell
    is over, or when the active account is blocked (at once).
    """
    scores = {i: -readings[i].week_pct for i in sorted(readings) if not _blocked(readings[i])}
    margin = state["margin_pts"]
    return _firm_choice(readings, active_id, now, state, scores, lambda best, cur: best > cur + margin)


def _scaled_observe(readings, state):
    """Add to the consumption history the rises that the readings show.

    The policy does not see the truth: for each reading newer than the last
    one it saw for that account, a rise of the weekly percent is
    ``rise / 100 * capacity`` capacity units consumed (time stamp: the reading's
    ``as_of``). A drop (a weekly reset) only sets a new baseline.
    """
    seen = state.setdefault("seen", {})
    hist = state.setdefault("hist", collections.deque())
    for ident in sorted(readings):
        r = readings[ident]
        prev = seen.get(ident)
        if prev is not None and r.as_of <= prev[0]:
            continue  # stale: the same or an older reading
        if prev is not None and r.week_pct > prev[1]:
            units = (r.week_pct - prev[1]) / 100.0 * r.capacity
            hist.append((r.as_of, units))
            state["consumed"] = state.get("consumed", 0.0) + units
        seen[ident] = (r.as_of, r.week_pct)


def _scaled_consumed(state, now):
    """Capacity units observed in the trailing 7 days (as_of > now - 7 days)."""
    hist = state.setdefault("hist", collections.deque())
    cutoff = now - WEEK_S
    while hist and hist[0][0] <= cutoff:
        state["consumed"] -= hist.popleft()[1]
    if not hist:
        state["consumed"] = 0.0
    return state.get("consumed", 0.0)


def scaled_line_f(readings, now, state):
    """The height of the line: ``f = clip(f_boost * consumed_7d / fleet_capacity_7d, f_min, 1)``.

    ``consumed_7d`` = units observed in the trailing 7 days (``_scaled_observe``);
    ``fleet_capacity_7d`` = the capacity of the accounts in ``readings`` (the
    enabled ones) times span / 7 days, with span = time since the first call,
    at least 24 hours and at most 7 days. Returns (f, consumption per hour).
    """
    t0 = state.setdefault("t0", now)
    span = min(max(now - t0, SCALED_SPAN_FLOOR_S), WEEK_S)
    capacity = sum(r.capacity for r in readings.values())
    consumed = _scaled_consumed(state, now)
    raw = consumed / (capacity * span / WEEK_S) if capacity > 0 else 0.0
    f = min(1.0, max(state["f_min"], state["f_boost"] * raw))
    return f, consumed / (span / SECONDS_PER_HOUR)


def scaled_line_f_stateless(readings, now, state):
    """The height of the line from the current readings only (``f_mode`` = stateless).

    ``f = clip(f_boost * sum(c_i * u_i) / sum(c_i * e_i), f_min, 1)`` over the
    accounts in ``readings`` (the enabled ones): ``u_i`` = used fraction of the
    current week of account i (its reading), ``e_i`` = elapsed fraction of that
    week at ``now``. It is the use of the fleet so far over what the full line
    would be so far. If ``sum(c_i * e_i)`` is below SCALED_STATELESS_FLOOR of
    the fleet capacity (all accounts just reset), f is the last f
    (``state["f"]``), or ``f_min`` at the start. No history is kept.
    A stale reading whose ``reset_at`` is not after ``now`` shows the week that
    ended: its ``u_i`` counts as 0 (the account has reset), ``e_i`` stays.
    Returns (f, consumption per hour); the consumption per hour is the ratio
    before boost and clip times the fleet capacity per hour of a week, the
    same relation as in ``scaled_line_f``; in the floor case it comes from f.
    """
    used = line = capacity = 0.0
    for ident in sorted(readings):
        r = readings[ident]
        capacity += r.capacity
        if r.reset_at > now:  # else: a reading of a week that ended; this week's use is not seen yet
            used += r.capacity * min(1.0, max(0.0, r.week_pct / 100.0))
        line += r.capacity * (1.0 - _remaining_s(r, now) / WEEK_S)
    boost = state["f_boost"]
    if capacity <= 0 or line < SCALED_STATELESS_FLOOR * capacity:
        f = state.get("f", state["f_min"])
        raw = f / boost if boost > 0 else 0.0
    else:
        raw = used / line
        f = min(1.0, max(state["f_min"], boost * raw))
    return f, raw * capacity / WEEK_HOURS


def _guard_candidates(readings, now, state, rate_per_h):
    """Usable accounts that the loss guard protects: {id: hours to the reset}.

    reachable_i = min(1 - u_i, can_i * hours_i) * capacity_i, with ``can`` the
    5-hour drain per hour as in ``pressure_score`` (no limit: never limits);
    forecast_i = rate_per_h * hours_i. The account counts if its reset is
    within ``guard_hours``, reachable_i is at least GUARD_MIN_FRAC of its
    capacity and the forecast demand is at least reachable_i.
    """
    out = {}
    for ident in sorted(readings):
        r = readings[ident]
        if _blocked(r):
            continue
        hours = _remaining_s(r, now) / SECONDS_PER_HOUR
        if hours > state["guard_hours"]:
            continue
        left = max(0.0, 1.0 - r.week_pct / 100.0)
        can = 1.0 / PRESSURE_DWELL_FLOOR_H if r.five_share is None else r.five_share / WINDOW_HOURS
        reach = min(left, can * hours) * r.capacity
        if reach >= GUARD_MIN_FRAC * r.capacity and rate_per_h * hours >= reach:
            out[ident] = hours
    return out


def scaled_line_choose(readings, active_id, now, state):
    """Straight line per account scaled to the demand; firm like ``deficit``.

    FORMULAS (all from the readings; the truth is never read)
      f = clip(f_boost * consumed_7d / fleet_capacity_7d, f_min, 1)   (``scaled_line_f``)
        or, with ``f_mode`` = stateless, from the current readings only:
      f = clip(f_boost * sum(c_i * u_i) / sum(c_i * e_i), f_min, 1)   (``scaled_line_f_stateless``)
      deficit_i = f * elapsed_fraction_i * 100 - used_pct_i           (percent points)
    The usable account with the largest deficit wins, with the firmness layer
    (``margin_pts``, ``dwell_s``, forced switch when blocked, ``wait_for_idle_s``).
    With f = 1 this is the ``deficit`` policy.

    LOSS GUARD (``guard`` on, and f >= ``guard_f``): the accounts of
    ``_guard_candidates`` take priority, the earliest reset first: their score
    is the best deficit + margin + 1 + (guard_hours - hours) / guard_hours.
    ``state["f"]`` keeps the last f (for the tests and the reports).
    """
    if state["f_mode"] == F_MODE_STATELESS:
        f, rate = scaled_line_f_stateless(readings, now, state)
    else:
        _scaled_observe(readings, state)
        f, rate = scaled_line_f(readings, now, state)
    state["f"] = f
    scores = {}
    for i in sorted(readings):
        r = readings[i]
        if not _blocked(r):
            elapsed_pct = 100.0 * (1.0 - _remaining_s(r, now) / WEEK_S)
            scores[i] = f * elapsed_pct - r.week_pct
    margin = state["margin_pts"]
    if state["guard"] and f >= state["guard_f"] and scores:
        top = max(scores.values())
        for ident, hours in _guard_candidates(readings, now, state, rate).items():
            scores[ident] = top + margin + 1.0 + (state["guard_hours"] - hours) / state["guard_hours"]
    return _firm_choice(readings, active_id, now, state, scores, lambda best, cur: best > cur + margin)


PARAM_DEFAULTS = {
    "threshold_best": {"threshold": THRESHOLD_BEST_PCT},
    "earliest_reset": {},
    "deficit": {
        "margin_pts": DEFICIT_MARGIN_PTS,
        "dwell_s": DEFICIT_DWELL_S,
        "wait_for_idle_s": WAIT_FOR_IDLE_S,
    },
    "pressure": {
        "ratio": PRESSURE_RATIO,
        "dwell_s": PRESSURE_DWELL_S,
        "wait_for_idle_s": WAIT_FOR_IDLE_S,
        "q_scale": PRESSURE_Q_SCALE,
        "dwell_floor_h": PRESSURE_DWELL_FLOOR_H,
    },
    "lowest_pct": {
        "margin_pts": DEFICIT_MARGIN_PTS,
        "dwell_s": DEFICIT_DWELL_S,
        "wait_for_idle_s": WAIT_FOR_IDLE_S,
    },
    "scaled_line": {
        "margin_pts": DEFICIT_MARGIN_PTS,
        "dwell_s": DEFICIT_DWELL_S,
        "wait_for_idle_s": WAIT_FOR_IDLE_S,
        "f_min": SCALED_F_MIN,
        "f_boost": SCALED_F_BOOST,
        "guard": SCALED_GUARD,
        "guard_hours": SCALED_GUARD_HOURS,
        "guard_f": SCALED_GUARD_F,
        "f_mode": SCALED_F_MODE,
    },
}
# The parameters whose value is a word, not a number: (policy, name) -> allowed words.
WORD_PARAMS = {("scaled_line", "f_mode"): (F_MODE_TRAILING, F_MODE_STATELESS)}


def _state_factory(name):
    defaults = PARAM_DEFAULTS[name]

    def make_state(**kw):
        unknown = set(kw) - set(defaults)
        if unknown:
            raise TypeError(f"unknown parameter for {name}: {sorted(unknown)}")
        for param, value in kw.items():
            words = WORD_PARAMS.get((name, param))
            if words is not None and value not in words:
                raise ValueError(f"unknown value {value!r} for {name}.{param} (known: {', '.join(words)})")
        return {**defaults, **kw}

    return make_state


POLICIES = {
    "threshold_best": Policy("threshold_best", threshold_best_choose, _state_factory("threshold_best")),
    "earliest_reset": Policy("earliest_reset", earliest_reset_choose, _state_factory("earliest_reset")),
    "deficit": Policy("deficit", deficit_choose, _state_factory("deficit")),
    "pressure": Policy("pressure", pressure_choose, _state_factory("pressure")),
    "lowest_pct": Policy("lowest_pct", lowest_pct_choose, _state_factory("lowest_pct")),
    "scaled_line": Policy("scaled_line", scaled_line_choose, _state_factory("scaled_line")),
}


def parse_params(items):
    """``["deficit.margin_pts=8", ...]`` -> ``{"deficit": {"margin_pts": 8.0}}``.

    Raises ValueError for a bad item, an unknown policy or an unknown name.
    """
    out = {}
    for item in items or ():
        name, sep, raw = item.partition("=")
        policy, dot, param = name.partition(".")
        if not sep or not dot or not param:
            raise ValueError(f"expected policy.name=value, got {item!r}")
        _check_param(policy, param)
        out.setdefault(policy, {})[param] = _value(policy, param, raw, item)
    return out


def _known_params():
    return {**PARAM_DEFAULTS, "oracle": ORACLE_DEFAULTS}


def _check_param(policy, param):
    known_params = _known_params()
    if policy not in known_params:
        raise ValueError(f"unknown policy {policy!r} (known: {', '.join(known_params)})")
    if param not in known_params[policy]:
        known = ", ".join(known_params[policy]) or "none"
        raise ValueError(f"unknown parameter {policy}.{param} (known: {known})")


def _value(policy, param, raw, item):
    """The value of ``policy.param``: a number, or a word for the ``WORD_PARAMS``."""
    words = WORD_PARAMS.get((policy, param))
    if words is None:
        return _number(raw, item)
    word = raw.strip()
    if word not in words:
        raise ValueError(f"unknown value in {item!r} (known: {', '.join(words)})")
    return word


def _number(raw, item):
    try:
        v = float(raw)
    except ValueError as exc:
        raise ValueError(f"not a number in {item!r}") from exc
    if not math.isfinite(v):
        raise ValueError(f"not a finite number in {item!r}")
    return v


# --------------------------------------------------------------------------
# Scenario generator
# --------------------------------------------------------------------------


def day_weights(shape, rng=None):
    """Seven weights, Monday first, normalised to sum 1."""
    if shape == "flat":
        w = [1.0] * 7
    elif shape == "weekdays_heavy":
        w = [1.5] * 5 + [0.25] * 2
    elif shape == "weekend_heavy":
        w = [0.25] * 5 + [1.5] * 2
    elif shape == "giant_days":
        rng = rng or random.Random(0)
        w = [1.0] * 7
        for d in rng.sample(range(7), rng.choice((1, 2))):
            w[d] = 8.0
    elif shape == "ramp_up":
        w = [0.3 + 1.4 * i / 6 for i in range(7)]
    elif shape == "ramp_down":
        w = [1.7 - 1.4 * i / 6 for i in range(7)]
    else:
        raise ValueError(shape)
    total = sum(w)
    return [x / total for x in w]


def hour_weights(shape):
    """24 weights, hour 0 first, normalised to sum 1."""
    if shape == "flat":
        w = [1.0] * 24
    elif shape == "work_hours":  # 09:00 to 18:00
        w = [1.0 if 9 <= h < 18 else OFF_HOURS_WEIGHT for h in range(24)]
    elif shape == "night":  # 22:00 to 06:00
        w = [1.0 if h >= 22 or h < 6 else OFF_HOURS_WEIGHT for h in range(24)]
    elif shape == "two_blocks":  # 08:00 to 12:00 and 20:00 to 24:00
        w = [1.0 if 8 <= h < 12 or h >= 20 else OFF_HOURS_WEIGHT for h in range(24)]
    else:
        raise ValueError(shape)
    total = sum(w)
    return [x / total for x in w]


def _loguniform(rng, lo, hi):
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


def idle_intervals(rng, spec, total_weeks):
    """Idle periods of a run: [(start_tick, length_ticks)], all inside the run.

    ``spec``: ``block_prob`` (chance that a day has an idle block),
    ``block_hours`` (its length), ``block_anchor_h`` (the hour where it starts,
    moved by up to IDLE_JITTER_H hours) and ``days_per_week`` (whole idle days,
    drawn without repeat in each week).
    """
    run = total_weeks * WEEK_TICKS
    block = round(spec["block_hours"] * TICKS_PER_HOUR)
    out = []
    for d in range(total_weeks * 7):
        if block > 0 and rng.random() < spec["block_prob"]:
            jitter = rng.uniform(-IDLE_JITTER_H, IDLE_JITTER_H) if spec["block_jitter_h"] else 0.0
            start = d * TICKS_PER_DAY + round((spec["block_anchor_h"] + jitter) * TICKS_PER_HOUR)
            lo, hi = max(start, 0), min(start + block, run)
            if hi > lo:
                out.append((lo, hi - lo))
    for w in range(total_weeks):
        for d in rng.sample(range(7), spec["days_per_week"]):
            out.append(((w * 7 + d) * TICKS_PER_DAY, TICKS_PER_DAY))
    return out


def _idle_mask(idle, total_weeks):
    """Per week, the tick indices (inside the week) that are idle."""
    weeks = [set() for _ in range(total_weeks)]
    for start, length in idle:
        for k in range(start, min(start + length, total_weeks * WEEK_TICKS)):
            weeks[k // WEEK_TICKS].add(k % WEEK_TICKS)
    return weeks


def build_demand(rng, p, total_capacity, weeks):
    """Demand per tick, in capacity units, for (warm-up + weeks) weeks.

    Per week: the volume (fleet capacity fraction) is split by day and hour
    weights; each tick is active with ``active_prob`` and has a lognormal
    noise with spread sigma = ln(burst_ratio) / Z99; optionally a fraction of
    the active ticks is multiplied (``double_prob``). The ticks of the idle
    periods (``p["idle"]``, round 3) are set to zero, then each week is
    rescaled to its exact volume (the same volume in fewer hours). After that
    (they change the realised volume): concurrency bursts, idle gaps and the
    regime step. A zero stays zero in all of them.
    """
    total_weeks = WARMUP_WEEKS + weeks
    days = p["day_weights"]
    hours = p["hour_weights"]
    sigma = math.log(p["burst_ratio"]) / Z99
    lo, hi = WEEK_VOLUME_CLAMP
    out = []
    idle_week = _idle_mask(p.get("idle", ()), total_weeks)
    for w in range(total_weeks):
        if p.get("week_volumes") is not None:
            vol = p["week_volumes"][min(w, len(p["week_volumes"]) - 1)]
        else:
            vol = p["volume"] * p["growth"] ** (w - WARMUP_WEEKS)
        vol = min(max(vol, lo), hi) if p.get("week_volumes") is None else vol
        raw = []
        for k in range(WEEK_TICKS):
            base = days[k // TICKS_PER_DAY] * hours[(k % TICKS_PER_DAY) // TICKS_PER_HOUR]
            if rng.random() >= p["active_prob"]:
                raw.append(0.0)
                continue
            v = base * math.exp(sigma * rng.gauss(0.0, 1.0))
            if p["double_prob"] and rng.random() < p["double_prob"]:
                v *= p["double_factor"]
            raw.append(v)
        for k in idle_week[w]:
            raw[k] = 0.0
        s = sum(raw)
        scale = vol * total_capacity / s if s > 0 else 0.0
        out.extend(x * scale for x in raw)
    n = len(out)
    # concurrency bursts
    if p["conc_chance"] > 0:
        mult = [1.0] * n
        for h in range(n // TICKS_PER_HOUR):
            if rng.random() < p["conc_chance"]:
                start = h * TICKS_PER_HOUR + rng.randrange(TICKS_PER_HOUR)
                m = rng.choices(CONC_MULTIPLIERS, CONC_WEIGHTS)[0]
                length = rng.randint(*RANGES["conc_minutes"]) // 5
                for k in range(start, min(n, start + length)):
                    mult[k] = max(mult[k], m)
        out = [x * m for x, m in zip(out, mult)]
    for start, length in p["gaps"]:
        for k in range(start, min(n, start + length)):
            out[k] = 0.0
    if p["regime"] is not None:
        tick, factor = p["regime"]
        for k in range(tick, n):
            out[k] *= factor
    return out


def _draw_fleet(rng, weeks):
    n = rng.randint(*RANGES["fleet_size"])
    run_ticks = (WARMUP_WEEKS + weeks) * WEEK_TICKS
    kind = rng.random()
    if kind < P_BIG_AND_SMALL:
        plans = ["max20"] + ["pro"] * (n - 1)
        rng.shuffle(plans)
    elif kind < P_BIG_AND_SMALL + P_MULTI_LARGE:
        k = min(n, rng.choice(MULTI_LARGE_COUNTS))
        rest = rng.choices([x for x, _ in MULTI_LARGE_REST], [w for _, w in MULTI_LARGE_REST], k=n - k)
        plans = ["max20"] * k + rest
        rng.shuffle(plans)
    else:
        names = [x for x, _ in PLAN_WEIGHTS]
        weights = [x for _, x in PLAN_WEIGHTS]
        plans = rng.choices(names, weights, k=n)
    clustered = rng.random() < P_CLUSTERED
    base = rng.randrange(WEEK_TICKS)
    accounts = []
    for plan in plans:
        offset = (base + rng.randrange(TICKS_PER_DAY)) % WEEK_TICKS if clustered else rng.randrange(WEEK_TICKS)
        cap = _loguniform(rng, *RANGES["custom_capacity"]) if plan == CUSTOM_PLAN else None
        r = rng.random()
        share = INHERIT
        if r < P_NO_FIVE:
            share = None
        elif r < P_NO_FIVE + P_OWN_SHARE:
            share = rng.uniform(*RANGES["own_share"])
        accounts.append(account(plan, offset, five_hour_share=share, capacity=cap))
    event = None
    if n >= 2 and rng.random() < P_FLEET_EVENT:
        idx = rng.randrange(1, n)  # account 0 always stays enabled
        tick = rng.randrange(WARMUP_WEEKS * WEEK_TICKS, run_ticks)
        a = accounts[idx]
        if rng.random() < 0.5:
            accounts[idx] = a._replace(end=tick)
            event = ("disable", idx, tick)
        else:
            accounts[idx] = a._replace(start=tick)
            event = ("add", idx, tick)
    return accounts, clustered, event


def draw_params(rng, weeks):
    """Draw every parameter of a random scenario from RANGES."""
    run_ticks = (WARMUP_WEEKS + weeks) * WEEK_TICKS
    accounts, clustered, event = _draw_fleet(rng, weeks)
    day_shape = rng.choice(DAY_SHAPES)
    p = {
        "accounts": accounts,
        "clustered": clustered,
        "fleet_event": event,
        "volume": _loguniform(rng, *RANGES["volume"]),
        "growth": _loguniform(rng, *RANGES["growth"]) if rng.random() < P_GROWTH else 1.0,
        "day_shape": day_shape,
        "day_weights": day_weights(day_shape, rng),
        "hour_shape": rng.choice(HOUR_SHAPES),
        "burst_ratio": _loguniform(rng, *RANGES["burst_ratio"]),
        "active_prob": rng.uniform(*RANGES["active_prob"]),
        "double_prob": 0.0,
        "double_factor": 1.0,
        "conc_chance": rng.uniform(*RANGES["conc_chance_per_hour"]),
        "gaps": [],
        "regime": None,
        "outages": [],
    }
    p["hour_weights"] = hour_weights(p["hour_shape"])
    if rng.random() < P_GAPS:
        for _ in range(rng.randint(1, 2)):
            length = rng.randint(*RANGES["gap_hours"]) * TICKS_PER_HOUR
            p["gaps"].append((rng.randrange(run_ticks), length))
    if rng.random() < P_REGIME:
        p["regime"] = (rng.randrange(WARMUP_WEEKS * WEEK_TICKS, run_ticks), rng.choice(REGIME_FACTORS))
    if rng.random() < P_OUTAGE:
        for _ in range(rng.randint(1, 3)):
            length = rng.randint(RANGES["outage_minutes"][0] // 5, RANGES["outage_minutes"][1] // 5)
            start = rng.randrange(WARMUP_WEEKS * WEEK_TICKS, run_ticks - length)
            p["outages"].append((start, start + length))
    # Round 3: real idle periods (drawn last, from their own stream, so that the
    # parameters above keep their values).
    idle_rng = random.Random(rng.random())
    p["idle_spec"] = {
        "block_prob": idle_rng.uniform(*RANGES["idle_block_prob"]),
        "block_hours": idle_rng.uniform(*RANGES["idle_block_hours"]),
        "block_anchor_h": idle_rng.uniform(*RANGES["idle_block_anchor_h"]),
        "block_jitter_h": IDLE_JITTER_H,
        "days_per_week": idle_rng.randint(*RANGES["idle_days_per_week"]),
    }
    p["idle"] = idle_intervals(idle_rng, p["idle_spec"], WARMUP_WEEKS + weeks)
    return p


def build_scenario(p, rng, weeks):
    total_capacity = sum(a.capacity for a in p["accounts"])
    demand = build_demand(rng, p, total_capacity, weeks)
    return Scenario(list(p["accounts"]), demand, list(p["outages"]), WARMUP_WEEKS, dict(p))


def generate_scenario(seed, index, weeks=DEFAULT_WEEKS):
    """Scenario number ``index`` of the random family for ``seed``."""
    rng = random.Random(f"rotation-sim:random:{seed}:{index}")
    return build_scenario(draw_params(rng, weeks), rng, weeks)


# --- presets ---------------------------------------------------------------


def _at(day, hour):
    """Tick of a weekday (Monday = 0) and hour."""
    return (day * 24 + hour) * TICKS_PER_HOUR


USER_FLEET_OFFSETS = (_at(6, 6), _at(0, 5), _at(1, 4), _at(2, 2), _at(2, 14), _at(5, 10))
USER_FLEET_PLANS = ("pro", "pro", "pro", "pro", "max", "max")
# Tuesday 23, Wednesday 20, Saturday 15, Sunday 13, Friday 11, Thursday 10, Monday 8.
USER_DAY_SPLIT = (8, 23, 20, 10, 11, 15, 13)
USER_DIP_PROB = 0.5
# Almost flat with a dip from 10:00 to 13:00.
USER_HOUR_DIP = [0.3 if 10 <= h < 13 else 1.0 for h in range(24)]
MIXED_PLANS = ("pro", "pro", "pro", "max", "max20")
MIXED_OFFSETS = (_at(0, 12), _at(1, 20), _at(3, 8), _at(4, 23), _at(6, 3))
MANY_LARGE_PLANS = ("max20", "pro", "max20", "pro", "max20")
MANY_LARGE_OFFSETS = (_at(0, 6), _at(1, 18), _at(3, 6), _at(4, 18), _at(6, 6))


def _norm(w):
    s = sum(w)
    return [x / s for x in w]


def _preset_params(accounts, **kw):
    p = {
        "accounts": accounts,
        "clustered": False,
        "fleet_event": None,
        "volume": 0.5,
        "growth": 1.0,
        "day_shape": "flat",
        "hour_shape": "flat",
        "burst_ratio": 8.0,
        "active_prob": 0.7,
        "double_prob": 0.0,
        "double_factor": 1.0,
        "conc_chance": 0.0,
        "gaps": [],
        "regime": None,
        "outages": [],
        "idle_spec": None,
        "idle": [],
    }
    p.update(kw)
    if "day_weights" not in p:
        p["day_weights"] = day_weights(p["day_shape"], random.Random(0))
    if "hour_weights" not in p:
        p["hour_weights"] = hour_weights(p["hour_shape"])
    return p


def _user_fleet():
    return [account(plan, off) for plan, off in zip(USER_FLEET_PLANS, USER_FLEET_OFFSETS)]


def preset_params(name):
    if name == "user_like":
        return _preset_params(
            _user_fleet(),
            volume=0.7,
            day_shape="user_split",
            day_weights=_norm(USER_DAY_SPLIT),
            hour_shape="flat_with_dip",
            hour_weights=_norm(USER_HOUR_DIP),
            burst_ratio=15.0,
            active_prob=0.6,
            double_prob=0.2,
            double_factor=2.0,
            # the dip from 10:00 to 13:00 is a real pause on half of the days,
            # and one whole idle day a week
            idle_spec={"block_prob": USER_DIP_PROB, "block_hours": 3.0, "block_anchor_h": 10.0,
                       "block_jitter_h": 0.0, "days_per_week": 1},
        )
    if name == "prompts_only_week_then_work":
        # Warm-up week at 0.1 of capacity (prompts only), then heavy work.
        return _preset_params(
            _user_fleet(),
            day_shape="weekdays_heavy",
            hour_shape="work_hours",
            week_volumes=[0.1, 0.9],
            volume=0.9,
            burst_ratio=10.0,
            active_prob=0.6,
        )
    if name == "light":
        return _preset_params(_user_fleet(), volume=0.15, day_shape="weekdays_heavy",
                              hour_shape="work_hours", burst_ratio=5.0)
    if name == "heavy":
        return _preset_params(_user_fleet(), volume=1.1, burst_ratio=4.0, active_prob=0.9)
    if name == "spiky":
        return _preset_params(_user_fleet(), volume=0.6, day_shape="giant_days",
                              hour_shape="two_blocks", burst_ratio=30.0, conc_chance=0.12,
                              active_prob=0.4)
    if name == "mixed_sizes":
        accounts = [account(plan, off) for plan, off in zip(MIXED_PLANS, MIXED_OFFSETS)]
        return _preset_params(accounts, volume=0.3, day_shape="weekdays_heavy",
                              hour_shape="work_hours", burst_ratio=10.0, active_prob=0.6)
    if name == "many_large":
        accounts = [account(plan, off) for plan, off in zip(MANY_LARGE_PLANS, MANY_LARGE_OFFSETS)]
        return _preset_params(accounts, volume=0.6, day_shape="weekdays_heavy",
                              hour_shape="work_hours", burst_ratio=10.0, active_prob=0.6)
    raise ValueError(name)


PRESETS = (
    "user_like",
    "prompts_only_week_then_work",
    "light",
    "heavy",
    "spiky",
    "mixed_sizes",
    "many_large",
)


def preset_scenario(name, seed=DEFAULT_SEED, weeks=DEFAULT_WEEKS):
    rng = random.Random(f"rotation-sim:preset:{name}:{seed}")
    p = preset_params(name)
    if p["idle_spec"] is not None:
        idle_rng = random.Random(f"rotation-sim:idle:{name}:{seed}")
        p["idle"] = idle_intervals(idle_rng, p["idle_spec"], WARMUP_WEEKS + weeks)
    return build_scenario(p, rng, weeks)


# --------------------------------------------------------------------------
# The simulation
# --------------------------------------------------------------------------


def band_of(share):
    """Volume band of a mean weekly volume (share of the fleet capacity)."""
    if share < BAND_MEDIUM_FROM:
        return "light"
    if share < BAND_HEAVY_FROM:
        return "medium"
    if share <= BAND_OVER_ABOVE:
        return "heavy"
    return "over"


def _week_stats(scn):
    """(unavoidable units, enabled capacity summed over weeks, measured demand).

    Measured weeks are the calendar weeks after the warm-up. An account counts
    with its full capacity in a week if it is enabled at any tick of the week.
    """
    warm = scn.warmup_weeks * WEEK_TICKS
    T = len(scn.demand)
    unavoidable = cap_total = demand_total = 0.0
    for a in range(warm, T, WEEK_TICKS):
        b = min(a + WEEK_TICKS, T)
        cap = sum(s.capacity for s in scn.accounts if s.start < b and (s.end is None or s.end > a))
        dem = sum(scn.demand[a:b])
        unavoidable += max(0.0, dem - cap)
        cap_total += cap
        demand_total += dem
    return unavoidable, cap_total, demand_total


def volume_share(scn):
    """Mean weekly demand as a share of the weekly capacity of the enabled accounts."""
    _u, cap, dem = _week_stats(scn)
    return dem / cap if cap > 0 else 0.0


def zero_demand_share(scn):
    """Percent of the measured ticks with zero demand."""
    warm = scn.warmup_weeks * WEEK_TICKS
    measured = scn.demand[warm:]
    return 100.0 * sum(1 for d in measured if d <= 0.0) / len(measured) if measured else 0.0


def scenario_band(scn):
    return band_of(volume_share(scn))


def simulate(
    scn,
    policy,
    five_hour_share=DEFAULT_FIVE_HOUR_SHARE,
    switch_cost_factor=DEFAULT_SWITCH_COST,
    trace=False,
    policy_params=None,
    cache_ttl_s=DEFAULT_CACHE_TTL_S,
    snapshots=False,
    belief_q_scale=1.0,
    truth=False,
):
    """Run ``policy`` (a name or a Policy) on ``scn``; return a result dict.

    Keys: ``metrics``, ``resets`` (account, tick, percent used, unused units,
    avoidable units; measured ones), ``switch_ticks`` (measured), ``totals``
    (whole run: demand, served, unserved), ``paid_ticks`` and ``free_ticks``
    (measured switches by cost) and, with ``trace``, ``trace`` (one tuple per
    tick with demand: tick, active, demand, served, unserved, consumed units).
    With ``snapshots``: ``snapshots``, one list per week boundary (before the
    resets of that tick) of (percent used, elapsed percent of its week) for
    each account. ``belief_q_scale`` multiplies the 5-hour share that the
    readings show to the policy; the truth (the real caps) does not change.
    ``truth`` (default False, only the oracle sets it) puts the true account
    states, the demand and the switch rules in ``state["truth"]``: the oracle
    is clairvoyant. No other policy gets it.
    """
    if isinstance(policy, str):
        policy = POLICIES[policy]
    state = policy.make_state(**(policy_params or {}))
    state["cache_ttl_s"] = cache_ttl_s
    choose = policy.choose
    specs = scn.accounts
    n = len(specs)
    demand = scn.demand
    T = len(demand)
    warm = scn.warmup_weeks * WEEK_TICKS
    W = WEEK_TICKS
    acc = []
    ev = []
    for i, s in enumerate(specs):
        share = five_hour_share if s.five_hour_share is INHERIT else s.five_hour_share
        first = s.offset if s.offset > 0 else W
        acc.append(AccountState(i, s.capacity, share, first))
        t = first
        while t <= T:
            ev.append((t, 0, i))
            t += W
        if s.start > 0:
            ev.append((s.start, 2, i))
        if s.end is not None and s.end <= T:
            ev.append((s.end, 1, i))
    ev.sort()
    enabled = [s.start <= 0 and (s.end is None or s.end > 0) for s in specs]
    ctx = None
    if truth:
        ctx = {"acc": acc, "enabled": enabled, "specs": specs, "demand": demand, "T": T,
               "switch_cost": switch_cost_factor, "cache_ttl_s": cache_ttl_s,
               "last_work": None, "last_used": None, "warm": warm}
        state["truth"] = ctx
    out = [False] * (T + 1)
    for a, b in scn.outages:
        for k in range(max(a, 0), min(b, T)):
            out[k] = True
    def read(a, t):
        r = a.reading(t)
        if belief_q_scale != 1.0 and r.five_share is not None:
            r = r._replace(five_share=r.five_share * belief_q_scale)
        return r

    readings = {}
    view = MappingProxyType(readings)
    if not out[0]:
        for i in range(n):
            if enabled[i]:
                readings[i] = read(acc[i], 0)
    xs = [0.0] * n  # unserved demand while the account had weekly headroom
    mx = [0.0] * n  # max percent of any other account since the last reset
    resets = []
    low_events = []
    switch_ticks = []
    paid_ticks = []
    free_ticks = []
    snaps = []
    last_work = None  # last tick with served work, on any account
    active = None
    last_used = None
    pend = False
    ptr = 0
    ep, nev = 0, len(ev)
    next_ev = ev[0][0] if nev else T + 1
    total_cap = sum(s.capacity for s in specs)
    un_eps = EPS_REL * total_cap
    dm = sm = um = 0.0
    dt = st = ut = 0.0
    stall_ticks = 0
    tr = []
    for t in range(T + 1):
        if snapshots and t and t % WEEK_TICKS == 0:
            snaps.append([
                (100.0 * a.used / a.cap, 100.0 * (1.0 - (a.next_reset - t) / WEEK_TICKS))
                for a in acc
            ])
        while t >= next_ev:
            tick, kind, i = ev[ep]
            ep += 1
            next_ev = ev[ep][0] if ep < nev else T + 1
            a = acc[i]
            if kind == 0:
                if enabled[i] and tick > warm:
                    unused = max(a.cap - a.used, 0.0)
                    pct = 100.0 * a.used / a.cap
                    resets.append((i, tick, pct, unused, min(unused, xs[i])))
                    if pct < LOW_PCT:
                        low_events.append(mx[i])
                a.reset_week()
                xs[i] = 0.0
                mx[i] = 0.0
            elif kind == 1:
                enabled[i] = False
                readings.pop(i, None)
                if active == i:
                    active = None
            else:
                enabled[i] = True
                if t < T and not out[t]:
                    readings[i] = read(a, t)
        if t == T:
            break
        if not out[t]:
            if active is not None:
                readings[active] = read(acc[active], t)
            for _ in range(n):
                ptr = (ptr + 1) % n
                if enabled[ptr] and ptr != active:
                    readings[ptr] = read(acc[ptr], t)
                    break
        d = demand[t]
        if d <= 0.0:
            continue
        choice = choose(view, active, t * TICK_S, state)
        if choice is not None and (not 0 <= choice < n or not enabled[choice]):
            raise ValueError(f"policy chose an unknown or disabled account: {choice!r}")
        if choice != active:
            if last_used is not None and choice is not None and choice != last_used:
                pend = True
                if t >= warm:
                    switch_ticks.append(t)
            active = choice
        measured = t >= warm
        if active is None:
            sv, un, consumed = 0.0, d, 0.0
        else:
            last_used = active
            a = acc[active]
            paid = pend and last_work is not None and idle_gap_s(last_work * TICK_S, t * TICK_S) < cache_ttl_s
            if pend and t >= warm:
                (paid_ticks if paid else free_ticks).append(t)
            mult = switch_cost_factor if paid else 1.0
            pend = False
            eff = d * mult
            room = a.headroom(t)
            if room >= eff:
                se, un = eff, 0.0
            else:
                se = room
                un = (eff - se) / mult
            if se > 0.0:
                a.serve(t, se)
                pct = 100.0 * a.used / a.cap
                for j in range(n):
                    if j != active and pct > mx[j]:
                        mx[j] = pct
            sv, consumed = d - un, se
            if se > 0.0:
                last_work = t
            if ctx is not None:
                ctx["last_used"], ctx["last_work"] = last_used, last_work
        dt += d
        st += sv
        ut += un
        if measured:
            dm += d
            sm += sv
            um += un
        if un > un_eps:
            if measured:
                stall_ticks += 1
            for j in range(n):
                if enabled[j] and acc[j].used < acc[j].cap - acc[j].eps:
                    xs[j] += un
        if trace:
            tr.append((t, active, d, sv, un, consumed))
    unavoidable = _week_stats(scn)[0]
    result = {
        "metrics": _metrics(specs, resets, low_events, switch_ticks, dm, sm, um, stall_ticks, T - warm,
                            unavoidable, len(paid_ticks), len(free_ticks)),
        "resets": resets,
        "switch_ticks": switch_ticks,
        "paid_ticks": paid_ticks,
        "free_ticks": free_ticks,
        "totals": {"demand": dt, "served": st, "unserved": ut},
    }
    if trace:
        result["trace"] = tr
    if snapshots:
        result["snapshots"] = snaps
    return result


def _plan_waste(rows, plan, specs):
    sel = [r for r in rows if specs[r[0]].plan == plan]
    cap = sum(specs[r[0]].capacity for r in sel)
    return 100.0 * sum(r[3] for r in sel) / cap if cap > 0 else None


def _metrics(specs, resets, low_events, switch_ticks, dm, sm, um, stall_ticks, measured_ticks,
             unavoidable, paid, free):
    cap = sum(specs[r[0]].capacity for r in resets)
    unused = sum(r[3] for r in resets)
    avoid = sum(r[4] for r in resets)
    pct = lambda x: 100.0 * x / cap if cap > 0 else 0.0  # noqa: E731
    by_acct = {}
    for r in resets:
        by_acct.setdefault(r[0], []).append(r[2])
    sds = []
    for k in range(max((len(v) for v in by_acct.values()), default=0)):
        vals = [v[k] for v in by_acct.values() if len(v) > k]
        if len(vals) >= 2:
            m = sum(vals) / len(vals)
            sds.append(math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals)))
    weeks = measured_ticks / WEEK_TICKS
    flaps = sum(1 for a, b in zip(switch_ticks, switch_ticks[1:]) if b - a <= FLAP_TICKS)
    m = {
        "waste": pct(unused),
        "waste_no_demand": pct(unused - avoid),
        "waste_avoidable": pct(avoid),
    }
    for plan in (*PLAN_CAPACITY, CUSTOM_PLAN):
        m[f"waste_{plan}"] = _plan_waste(resets, plan, specs)
    stall = 100.0 * um / dm if dm > 0 else 0.0
    unav = 100.0 * unavoidable / dm if dm > 0 else 0.0
    per_day = lambda n: n / (7.0 * weeks) if weeks > 0 else 0.0  # noqa: E731
    m.update(
        stall=stall,
        stall_unavoidable=unav,
        stall_excess=stall - unav,
        stall_hours=stall_ticks * TICK_S / 3600.0,
        served=100.0 * sm / dm if dm > 0 else 100.0,
        switches_per_day=per_day(len(switch_ticks)),
        paid_switches_per_day=per_day(paid),
        free_switches_per_day=per_day(free),
        flaps=float(flaps),
        unevenness=sum(sds) / len(sds) if sds else 0.0,
        max_used_while_low=max(low_events) if low_events else None,
    )
    return m


METRICS = (
    "waste",
    "waste_no_demand",
    "waste_avoidable",
    "waste_pro",
    "waste_max",
    "waste_ultra",
    "waste_max20",
    "waste_custom",
    "stall",
    "stall_unavoidable",
    "stall_excess",
    "stall_hours",
    "served",
    "switches_per_day",
    "paid_switches_per_day",
    "free_switches_per_day",
    "flaps",
    "unevenness",
    "max_used_while_low",
)


# --------------------------------------------------------------------------
# R2: the judge. Two reference results with full knowledge of the future and
# of the truth: ``bound_upper`` (an upper bound nothing can beat) and
# ``oracle`` (an achievable clairvoyant policy). The truth lies between them.
# --------------------------------------------------------------------------


def bound_upper(scn):
    """The most demand that ANY rule could serve: an UPPER bound.

    RELAXATION. Ignore the 5-hour caps and the switch cost, and let several
    accounts serve in the same tick. Then the only limit left is the weekly
    quota of each account inside its own week, and an account can serve only
    while it is enabled (``start <= t < end``).

    ALGORITHM. Process the ticks in time order. In each tick serve the demand
    from the enabled accounts with weekly headroom, in order of the earliest
    deadline (the next weekly reset, or the end of the account if that comes
    first). Quota that is not used by its reset is lost.

    WHY IT IS OPTIMAL FOR THIS RELAXATION (exchange argument). Take any plan
    that serves a unit at tick t from account j although account i, with a
    deadline not later than j's, still had headroom at t. If the plan uses that
    unit of i later, at t' < deadline(i) <= deadline(j), swap the two: serve t
    from i and t' from j. Account j is enabled and in the same week at t'
    (t' is before its deadline), so the plan stays feasible and serves the same
    amount. If the plan never uses that unit of i (it expires), serving t from i
    only frees a unit of j. Repeating the swap turns any plan into the greedy
    one without losing served demand. The ticks have no other coupling: there is
    no rate cap, so the units are interchangeable. (With the 5-hour window, a
    rate cap, earliest-deadline-first is NOT guaranteed optimal: this is why
    the bound ignores it and is only an upper bound.)

    WHY IT IS AN UPPER BOUND OF WHAT A REAL RULE CAN SERVE. A real rule is one
    feasible plan of the relaxation (one account per tick; the 5-hour caps and
    the switch burn only remove options), so it serves no more. The warm-up
    demand is NOT served in the pass that gives ``served`` and ``stall_floor``:
    serving it only uses quota, and a real rule that is blocked in the warm-up
    (a 5-hour cap) keeps quota for the measured weeks, so a bound that served the
    warm-up could be beaten. The price: an account whose week straddles the
    start of the measured period has its full quota there, so the bound is loose
    in that first partial week. ``waste_floor`` comes from a second pass that
    serves the warm-up too, because used quota is what lowers waste, and serves
    only from the accounts whose current week ends in a measured reset (quota
    used in a week that the metric never counts does not lower the measured
    waste: for example the week of an account that is disabled first).

    Returns a dict: ``served`` and ``stall_floor`` (percent of the measured
    demand, adding to 100), ``waste_floor`` (percent of the capacity of the
    measured resets, measured like the simulator does: unused quota at each
    measured reset) and the measured ``demand`` and ``unserved`` units.
    ``waste_floor`` is the waste of the greedy plan: a real rule can show a
    slightly lower waste only by burning quota on the switch cost.
    """
    _dm, um, _unused, _cap_sum = _edf(scn, serve_warmup=False)
    dm, _um, unused, cap_sum = _edf(scn, serve_warmup=True, counted_only=True)
    return {
        "served": 100.0 * (dm - um) / dm if dm > 0 else 100.0,
        "stall_floor": 100.0 * um / dm if dm > 0 else 0.0,
        "waste_floor": 100.0 * unused / cap_sum if cap_sum > 0 else 0.0,
        "demand": dm,
        "unserved": um,
    }


def _edf(scn, serve_warmup, counted_only=False):
    """Earliest deadline first over the whole run; (measured demand, measured unserved,
    unused quota at the measured resets, capacity of the measured resets).

    ``counted_only``: serve only from the accounts whose current week ends in a
    measured reset (the waste metric ignores the others), see ``bound_upper``.
    """
    specs = scn.accounts
    n = len(specs)
    demand = scn.demand
    T = len(demand)
    warm = scn.warmup_weeks * WEEK_TICKS
    cap = [s.capacity for s in specs]
    eps = [EPS_REL * c for c in cap]
    start = [s.start for s in specs]
    end = [INF_TICK if s.end is None else s.end for s in specs]
    nxt = [s.offset if s.offset > 0 else WEEK_TICKS for s in specs]
    used = [0.0] * n
    dm = um = 0.0
    unused = cap_sum = 0.0
    for t in range(T + 1):
        for i in range(n):
            if t >= nxt[i]:
                if t > warm and start[i] < t <= end[i]:
                    unused += max(cap[i] - used[i], 0.0)
                    cap_sum += cap[i]
                used[i] = 0.0
                nxt[i] += WEEK_TICKS
        if t == T:
            break
        d = demand[t]
        if d <= 0.0:
            continue
        if t < warm and not serve_warmup:
            continue
        order = sorted(
            (
                i for i in range(n)
                if start[i] <= t < end[i] and used[i] < cap[i] - eps[i]
                and (not counted_only or (warm < nxt[i] <= T and start[i] < nxt[i] <= end[i]))
            ),
            key=lambda i: (min(nxt[i], end[i]), i),
        )
        rem = d
        for i in order:
            take = min(rem, cap[i] - used[i])
            used[i] += take
            rem -= take
            if rem <= 0.0:
                break
        rem = max(rem, 0.0)
        if t >= warm:
            dm += d
            um += rem
    return dm, um, unused, cap_sum


def _oracle_static(ctx):
    """Per-run constants of the oracle, built once from the truth context."""
    specs = ctx["specs"]
    demand, T = ctx["demand"], ctx["T"]
    warm = ctx["warm"]
    dm = sum(demand[warm:])
    cap_measured = 0.0  # capacity of the measured resets: the base of the waste percent
    for s in specs:
        tick = s.offset if s.offset > 0 else WEEK_TICKS
        while tick <= T:
            if tick > warm and s.start < tick and (s.end is None or s.end >= tick):
                cap_measured += s.capacity
            tick += WEEK_TICKS
    return {
        "warm": warm,
        "w_un": 100.0 / dm if dm > 0 else 0.0,  # one unserved unit, in percent of the measured demand
        "w_waste": 100.0 / cap_measured if cap_measured > 0 else 0.0,  # one wasted unit, in percent of capacity
        "cap": [s.capacity for s in specs],
        "eps": [EPS_REL * s.capacity for s in specs],
        "start": [s.start for s in specs],
        "end": [INF_TICK if s.end is None else s.end for s in specs],
        "prefix": list(itertools.accumulate(ctx["demand"], initial=0.0)),
        "total_cap": sum(s.capacity for s in specs),
    }


def _oracle_room(i, tau, cap, eps, start, end, fcap, used, wst, wused):
    """Headroom of account ``i`` at tick ``tau`` in the forward state (0 if not enabled)."""
    if tau < start[i] or tau >= end[i]:
        return 0.0
    room = cap[i] - used[i]
    f = fcap[i]
    if f is not None:
        five = f - wused[i] if (wst[i] >= 0 and tau < wst[i] + WINDOW_TICKS) else f
        if five < room:
            room = five
    return room if room > eps[i] else 0.0


def _oracle_forward(st, ctx, snap, ev, t0, fine_end, t_end, first, bound):
    """Cost of putting ``first`` on duty at tick ``t0`` and running to ``t_end``.

    Forward simulation on a copy of the true state: the first tick exactly like
    the engine (the account serves what it can, the rest is unserved), then blocks
    of ``ORACLE_BLOCK_TICKS`` ticks in which the demand that the active account
    cannot serve goes to the continuation account (the oracle re-decides at the
    tick of a block). State (``snap``: used, window start, window used, next reset of each
    account). CONTINUATION RULE: stay on the account until it is blocked, then
    move to the enabled account with headroom whose deadline (weekly reset, or
    its end) is the earliest. The switch cost is simulated as in the engine
    (paid only if the previous work ended within the cache lifetime).
    Returns ``unserved + waste_that_expires_in_the_horizon + burn`` in capacity
    units, where ``burn`` is the extra quota that the paid switches consume
    (the switch cost; burning quota that expires anyway adds zero net cost).
    Returns ``math.inf`` as soon as the running cost exceeds ``bound``.
    """
    cap, eps, start, end, prefix = st["cap"], st["eps"], st["start"], st["end"], st["prefix"]
    warm, w_un, w_waste = st["warm"], st["w_un"], st["w_waste"]
    fcap = ctx["five_caps"]
    used = snap[0][:]
    wst = snap[1][:]
    wused = snap[2][:]
    nxt = snap[3][:]
    n = len(cap)
    mult = ctx["switch_cost"]
    ttl = ctx["cache_ttl_s"]
    cur = first
    last_used = ctx["last_used"]
    pend = last_used is not None and first != last_used
    lw = ctx["last_work"]
    un_c = waste_c = burn_c = 0.0
    ep, ne = 0, len(ev)
    tau = t0
    exact = True  # the first tick is exact, like the engine: no re-choice inside a tick
    while tau < t_end:
        B = ORACLE_BLOCK_TICKS if tau < fine_end else ORACLE_TAIL_BLOCK_TICKS
        nb = tau + 1 if exact else (tau // B + 1) * B
        if nb > t_end:
            nb = t_end
        while ep < ne and ev[ep][0] <= tau:
            tick, i = ev[ep]
            ep += 1
            if warm < tick and start[i] < tick <= end[i]:
                waste_c += cap[i] - used[i]
            used[i] = 0.0
            nxt[i] += WEEK_TICKS
        dem = prefix[nb] - prefix[tau]
        if dem > 0.0:
            rest = dem  # demand of the block not yet served
            for _ in range(n + 1):
                room = _oracle_room(cur, tau, cap, eps, start, end, fcap, used, wst, wused)
                if room <= 0.0 and exact:
                    break
                if room <= 0.0:
                    best, best_dl = -1, INF_TICK + 1
                    for i in range(n):
                        if i != cur and _oracle_room(i, tau, cap, eps, start, end, fcap, used, wst, wused) > 0.0:
                            dl = nxt[i] if nxt[i] < end[i] else end[i]
                            if dl < best_dl:
                                best, best_dl = i, dl
                    if best < 0:
                        break
                    cur = best
                    if last_used is not None and cur != last_used:
                        pend = True
                    room = _oracle_room(cur, tau, cap, eps, start, end, fcap, used, wst, wused)
                paid = pend and lw is not None and idle_gap_s(lw * TICK_S, tau * TICK_S) < ttl
                m = mult if paid else 1.0
                pend = False
                last_used = cur
                eff = rest * m
                if room >= eff:
                    se, done = eff, rest
                else:
                    se, done = room, room / m
                if fcap[cur] is not None:
                    if not (wst[cur] >= 0 and tau < wst[cur] + WINDOW_TICKS):
                        wst[cur] = tau
                        wused[cur] = 0.0
                    wused[cur] += se
                used[cur] += se
                lw = nb - 1
                burn_c += se - done
                rest -= done
                if rest <= 0.0 or exact:
                    break
            if rest > 0.0:
                un_c += rest
            if un_c * w_un + (waste_c + burn_c) * w_waste > bound:
                return math.inf
        exact = False
        tau = nb
    return un_c * w_un + (waste_c + burn_c) * w_waste


def oracle_choose(readings, active_id, now, state):
    """The clairvoyant policy: a receding-horizon greedy. It reads the TRUTH.

    It decides when the active account cannot serve the demand of the tick (it
    is blocked or too full) and otherwise every ``step_s`` seconds. At a
    decision it evaluates each usable account (enabled, with headroom) by a
    forward simulation of ``horizon_h`` hours (``_oracle_forward``: the demand
    of the future is known, no stale readings) and picks the account with the
    lowest ``unserved + waste_that_expires_in_the_horizon + switch_cost``. A tie
    (within a relative 1e-9 of the fleet capacity) stays on the active account,
    then takes the earliest deadline, then the lowest id.

    Quality: a LOWER bound of the true optimum's quality. The plan is greedy, so
    the true optimum (perfect foresight of everything) is at least as good, and
    the truth lies between this policy and ``bound_upper``. The horizon sees no
    quota beyond ``horizon_h``, and the forward step works in blocks of
    ``ORACLE_BLOCK_TICKS`` ticks; the engine that runs the choice stays exact.
    """
    ctx = state["truth"]
    st = state.get("static")
    if st is None:
        st = state["static"] = _oracle_static(ctx)
        ctx["five_caps"] = [a.five_cap for a in ctx["acc"]]
    acc, enabled, demand = ctx["acc"], ctx["enabled"], ctx["demand"]
    n = len(acc)
    t = now // TICK_S
    cur = active_id if active_id is not None and enabled[active_id] else None
    room_cur = acc[cur].headroom(t) if cur is not None else 0.0
    last = state.get("last_decision")
    if cur is not None and room_cur >= demand[t] and last is not None and now - last < state["step_s"]:
        return cur
    state["last_decision"] = now
    usable = [i for i in range(n) if enabled[i] and acc[i].headroom(t) > 0.0]
    if not usable:
        if cur is not None:
            return cur
        return next((i for i in range(n) if enabled[i]), None)
    if len(usable) == 1:
        return usable[0]
    fine = max(1, round(state["horizon_h"] * TICKS_PER_HOUR))
    t_end = min(t + fine + round(state["tail_h"] * TICKS_PER_HOUR), ctx["T"])
    fine_end = t + fine
    snap = ([a.used for a in acc], [a.win_start for a in acc], [a.win_used for a in acc],
            [a.next_reset for a in acc])
    ev = sorted((tk, i) for i in range(n) for tk in range(snap[3][i], t_end + 1, WEEK_TICKS))
    end = st["end"]
    order = sorted(usable, key=lambda i: (i != cur, min(snap[3][i], end[i]), i))
    tol = ORACLE_TIE_REL
    best, best_cost = None, math.inf
    for c in order:
        bound = best_cost - tol if best is not None else math.inf
        cost = _oracle_forward(st, ctx, snap, ev, t, fine_end, t_end, c, bound)
        if best is None or cost < best_cost - tol:
            best, best_cost = c, cost
    return best


def _oracle_state_factory(**kw):
    unknown = set(kw) - set(ORACLE_DEFAULTS)
    if unknown:
        raise TypeError(f"unknown parameter for oracle: {sorted(unknown)}")
    return {**ORACLE_DEFAULTS, **kw}


ORACLE_POLICY = Policy("oracle", oracle_choose, _oracle_state_factory)


def oracle(
    scn,
    five_hour_share=DEFAULT_FIVE_HOUR_SHARE,
    switch_cost_factor=DEFAULT_SWITCH_COST,
    trace=False,
    policy_params=None,
    cache_ttl_s=DEFAULT_CACHE_TTL_S,
):
    """Run the clairvoyant policy ``oracle_choose`` on ``scn``; return the result of ``simulate``.

    An ACHIEVABLE reference: it obeys every rule of the simulator (one active
    account, 5-hour windows, switch cost, weekly resets) because the same engine
    runs it; it only differs by seeing the truth and the future demand. It is not
    a real policy: it is listed apart (``--judge``). Parameters
    (``ORACLE_DEFAULTS``): ``horizon_h`` and ``step_s``.
    """
    return simulate(scn, ORACLE_POLICY, five_hour_share, switch_cost_factor, trace,
                    policy_params, cache_ttl_s, truth=True)


def with_gaps(m, bound, orc):
    """``m`` (a metrics dict) plus the judge numbers and the four gaps.

    ``stall_gap_upper = stall - stall_floor``, ``stall_gap_oracle = stall -
    oracle_stall``, ``waste_gap_upper = waste - waste_floor``, ``waste_gap_oracle
    = waste - oracle_waste``.
    """
    return {
        **m,
        "stall_floor": bound["stall_floor"],
        "waste_floor": bound["waste_floor"],
        "oracle_stall": orc["stall"],
        "oracle_waste": orc["waste"],
        "stall_gap_upper": m["stall"] - bound["stall_floor"],
        "stall_gap_oracle": m["stall"] - orc["stall"],
        "waste_gap_upper": m["waste"] - bound["waste_floor"],
        "waste_gap_oracle": m["waste"] - orc["waste"],
    }


def judge(scn, five_hour_share, cache_ttl_s, oracle_params=None):
    """(bound, oracle metrics with gaps) of one scenario."""
    bound = bound_upper(scn)
    orc = oracle(scn, five_hour_share, cache_ttl_s=cache_ttl_s, policy_params=oracle_params)["metrics"]
    return bound, with_gaps(orc, bound, orc)


# --------------------------------------------------------------------------
# Aggregation, formatting, CLI
# --------------------------------------------------------------------------


def percentile(values, q):
    """Linear interpolation percentile, ``q`` in [0, 100]."""
    s = sorted(values)
    if not s:
        return None
    pos = (len(s) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _summary(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"mean": None, "p10": None, "p50": None, "p90": None}
    return {
        "mean": sum(vals) / len(vals),
        "p10": percentile(vals, 10),
        "p50": percentile(vals, 50),
        "p90": percentile(vals, 90),
    }


def _task(args):
    """Worker: one scenario, every selected policy. Top level, picklable.

    With ``judge`` (the last item) it also returns ``bound`` (``bound_upper``) and
    ``oracle`` (the metrics of the oracle with the gaps) and adds the judge
    numbers and the gaps to the metrics of every policy.
    """
    kind, key, seed, weeks, share, policies, params, ttl, belief, judge_on = args
    scn = preset_scenario(key, seed, weeks) if kind == "preset" else generate_scenario(seed, key, weeks)
    metrics = {
        p: simulate(scn, p, share, policy_params=params.get(p), cache_ttl_s=ttl,
                    belief_q_scale=belief)["metrics"]
        for p in policies
    }
    out = {
        "band": scenario_band(scn),
        "share": volume_share(scn),
        "zero": zero_demand_share(scn),
        "metrics": metrics,
    }
    if judge_on:
        bound, orc = judge(scn, share, ttl, params.get("oracle"))
        out["bound"] = bound
        out["oracle"] = orc
        out["metrics"] = {p: with_gaps(m, bound, orc) for p, m in metrics.items()}
    return out


def _map(tasks, jobs, fn=_task):
    if jobs <= 1 or len(tasks) <= 1:
        return [fn(t) for t in tasks]
    try:
        ctx = multiprocessing.get_context("fork")
    except ValueError:  # no fork on this platform
        ctx = multiprocessing.get_context()
    chunk = max(1, math.ceil(len(tasks) / (jobs * 4)))
    with ctx.Pool(jobs) as pool:
        return pool.map(fn, tasks, chunksize=chunk)


def _fmt(v):
    return "-" if v is None else f"{v:.2f}"


def _table(first_header, rows, columns=METRICS):
    """rows: list of (label, {metric: value}); one column per metric."""
    width = max([len(first_header)] + [len(r[0]) for r in rows]) + 2
    cols = [max(len(m), 8) + 1 for m in columns]
    lines = [first_header.ljust(width) + "".join(m.rjust(w) for m, w in zip(columns, cols))]
    for label, vals in rows:
        lines.append(label.ljust(width) + "".join(_fmt(vals[m]).rjust(w) for m, w in zip(columns, cols)))
    return "\n".join(lines)


# --- the sweep ---------------------------------------------------------------


def score_of(m, kind="default"):
    """The sweep score of a metrics dict (lower is better).

    ``kind`` ``"gaps"``: the distance to the oracle instead of the excess stall and
    the avoidable waste (needs ``stall_gap_oracle`` and ``waste_gap_oracle``).
    """
    if kind == "gaps":
        return (
            SCORE_STALL_GAP * m["stall_gap_oracle"]
            + SCORE_WASTE_GAP * m["waste_gap_oracle"]
            + SCORE_UNEVENNESS * m["unevenness"] / SCORE_UNEVENNESS_SCALE
            + SCORE_PAID_SWITCHES * m["paid_switches_per_day"]
        )
    return (
        SCORE_STALL_EXCESS * m["stall_excess"]
        + SCORE_AVOIDABLE_WASTE * m["waste_avoidable"]
        + SCORE_UNEVENNESS * m["unevenness"] / SCORE_UNEVENNESS_SCALE
        + SCORE_PAID_SWITCHES * m["paid_switches_per_day"]
    )


SCORE_FORMULA = (
    f"score = {SCORE_STALL_EXCESS} * stall_excess + {SCORE_AVOIDABLE_WASTE} * waste_avoidable"
    f" + {SCORE_UNEVENNESS} * unevenness / {SCORE_UNEVENNESS_SCALE}"
    f" + {SCORE_PAID_SWITCHES} * paid_switches_per_day"
)
SCORE_FORMULA_GAPS = (
    f"score = {SCORE_STALL_GAP} * stall_gap_oracle + {SCORE_WASTE_GAP} * waste_gap_oracle"
    f" + {SCORE_UNEVENNESS} * unevenness / {SCORE_UNEVENNESS_SCALE}"
    f" + {SCORE_PAID_SWITCHES} * paid_switches_per_day"
)
SWEEP_KEYS_GAPS = (*SWEEP_KEYS, "stall_gap_oracle", "waste_gap_oracle")
SWEEP_COLUMNS = ("score", "stall_excess", "waste_avoidable", "unevenness", "paid_switches_per_day")
SWEEP_COLUMNS_GAPS = ("score", "stall_gap_oracle", "waste_gap_oracle", "unevenness", "paid_switches_per_day")


def parse_sweep(spec):
    """``"p.a=1,2;p.b=3,4"`` -> (policy, grid), grid = list of dicts (product, first name slowest)."""
    parts = [x.strip() for x in spec.split(";") if x.strip()]
    if not parts:
        raise ValueError("empty sweep")
    names, values, policy = [], [], None
    for part in parts:
        name, sep, raw = part.partition("=")
        pol, dot, param = name.strip().partition(".")
        if not sep or not dot or not param:
            raise ValueError(f"expected policy.name=v1,v2,..., got {part!r}")
        _check_param(pol, param)
        if policy not in (None, pol):
            raise ValueError("a sweep covers one policy only")
        policy = pol
        if param in names:
            raise ValueError(f"parameter {param} appears twice")
        items = [x.strip() for x in raw.split(",")]
        if not raw.strip() or "" in items:
            raise ValueError(f"no values for {name!r}")
        names.append(param)
        values.append([_value(pol, param, x, part) for x in items])
    return policy, [dict(zip(names, combo)) for combo in itertools.product(*values)]


def sweep_split(n):
    """Disjoint halves of the scenario indices: train first, then the held-out half."""
    half = n // 2
    return list(range(half)), list(range(half, n))


def _sweep_task(args):
    index, seed, weeks, share, policy, points, ttl, belief, gaps, oracle_params = args
    scn = generate_scenario(seed, index, weeks)
    keys = SWEEP_KEYS_GAPS if gaps else SWEEP_KEYS
    orc = oracle(scn, share, cache_ttl_s=ttl, policy_params=oracle_params)["metrics"] if gaps else None
    out = []
    for params in points:
        m = simulate(scn, policy, share, policy_params=params, cache_ttl_s=ttl,
                     belief_q_scale=belief)["metrics"]
        if gaps:
            m = {**m, "stall_gap_oracle": m["stall"] - orc["stall"], "waste_gap_oracle": m["waste"] - orc["waste"]}
        out.append({k: m[k] for k in keys})
    return out


def _eval_points(args, policy, points, indices):
    """Mean of each sweep metric over ``indices``, for each point; plus the score."""
    gaps = getattr(args, "score", "default") == "gaps"
    keys = SWEEP_KEYS_GAPS if gaps else SWEEP_KEYS
    tasks = [(i, args.seed, args.weeks, args.five_hour_share, policy, points, args.cache_ttl_s,
              args.belief_q_scale, gaps, args.params.get("oracle")) for i in indices]
    per = _map(tasks, args.jobs, _sweep_task)
    out = []
    for k in range(len(points)):
        row = {key: sum(r[k][key] for r in per) / len(per) for key in keys}
        row["score"] = score_of(row, "gaps" if gaps else "default")
        out.append(row)
    return out


def run_sweep(args, policy, grid):
    fixed = dict(args.params.get(policy, {}))
    train_idx, hold_idx = sweep_split(args.random)
    points = [fixed] + [{**fixed, **g} for g in grid]
    train = _eval_points(args, policy, points, train_idx)
    ranked = sorted(range(len(grid)), key=lambda k: (train[k + 1]["score"], k))
    top = ranked[:SWEEP_TOP]
    hold = _eval_points(args, policy, [fixed] + [points[k + 1] for k in top], hold_idx)
    rows = []
    for rank, k in enumerate(ranked):
        holdout = hold[1 + top.index(k)] if k in top else None
        rows.append({"rank": rank + 1, "params": grid[k], "train": train[k + 1], "holdout": holdout})
    best = min((r for r in rows if r["holdout"]), key=lambda r: (r["holdout"]["score"], r["rank"]))
    return {
        "policy": policy,
        "formula": SCORE_FORMULA_GAPS if getattr(args, "score", "default") == "gaps" else SCORE_FORMULA,
        "fixed": fixed,
        "train_indices": train_idx,
        "holdout_indices": hold_idx,
        "defaults": {"train": train[0], "holdout": hold[0]},
        "rows": rows,
        "best_holdout": {"params": best["params"], "rank_train": best["rank"], "holdout": best["holdout"]},
    }


def _params_label(p):
    return " ".join(f"{k}={v}" if isinstance(v, str) else f"{k}={v:g}" for k, v in p.items())


def _sweep_text(sw, head):
    cols = SWEEP_COLUMNS_GAPS if "stall_gap_oracle" in sw["formula"] else SWEEP_COLUMNS
    header = [f"{c}" for c in cols]
    names = [f"train {c}" for c in header] + [f"holdout {c}" for c in header]
    label_w = max(len(_params_label(r["params"])) for r in sw["rows"]) + 8

    def line(label, tr, ho):
        vals = [_fmt(tr[c]) for c in cols] + [_fmt(ho[c]) if ho else "-" for c in cols]
        return label.ljust(label_w) + "".join(v.rjust(len(n) + 1) for v, n in zip(vals, names))

    out = [
        f"# sweep {sw['policy']} | random {head['scenarios']} scenarios | seed {head['seed']} | weeks "
        f"{head['weeks']} | five_hour_share {head['five_hour_share']} | belief_q_scale "
        f"{head['belief_q_scale']:g} | cache_ttl_s {head['cache_ttl_s']:g}",
        f"# train = scenarios {sw['train_indices'][0]}..{sw['train_indices'][-1]} "
        f"({len(sw['train_indices'])}), holdout = {sw['holdout_indices'][0]}..{sw['holdout_indices'][-1]} "
        f"({len(sw['holdout_indices'])}); holdout only for the {SWEEP_TOP} best by train score",
        f"# {sw['formula']}  (mean over each half; lower is better)",
        "# fixed (--param): " + (_params_label(sw["fixed"]) or "none"),
        "rank params".ljust(label_w) + "".join(n.rjust(len(n) + 1) for n in names),
        line("defaults", sw["defaults"]["train"], sw["defaults"]["holdout"]),
    ]
    for r in sw["rows"][:SWEEP_TOP]:
        out.append(line(f"{r['rank']:>3} {_params_label(r['params'])}", r["train"], r["holdout"]))
    b = sw["best_holdout"]
    out.append(f"best holdout among the top {SWEEP_TOP}: {_params_label(b['params'])} "
               f"(train rank {b['rank_train']}, holdout score {b['holdout']['score']:.3f}, "
               f"defaults {sw['defaults']['holdout']['score']:.3f})")
    out.append("")
    return "\n".join(out)


# --- the steady-state diagnosis -------------------------------------------------


def steady_scenario(weeks=STEADY_WEEKS, load=STEADY_LOAD):
    """A constant load (no noise) on a staggered fleet, no warm-up."""
    accounts = [account(plan, off) for plan, off in zip(STEADY_PLANS, STEADY_OFFSETS)]
    cap = sum(a.capacity for a in accounts)
    per_tick = load * cap / WEEK_TICKS
    return Scenario(accounts, [per_tick] * (weeks * WEEK_TICKS), [], 0, {"steady": True})


def run_diagnose(args):
    weeks = args.weeks
    rows = []
    for load in STEADY_LOADS:
        scn = steady_scenario(weeks, load)
        n = len(scn.accounts)
        for name in STEADY_POLICIES:
            res = simulate(scn, name, args.five_hour_share, trace=True, snapshots=True,
                           policy_params=args.params.get(name), cache_ttl_s=args.cache_ttl_s,
                           belief_q_scale=args.belief_q_scale)
            reset = {}
            for acct, tick, pct, _u, _a in res["resets"]:
                reset[(acct, (tick - 1) // WEEK_TICKS + 1)] = pct
            work = [[0.0] * n for _ in range(weeks)]
            for t, act, _d, _sv, _un, consumed in res["trace"]:
                if act is not None:
                    work[t // WEEK_TICKS][act] += consumed
            for w in range(1, weeks + 1):
                total = sum(work[w - 1])
                for i in range(n):
                    used, elapsed = res["snapshots"][w - 1][i]
                    rows.append({
                        "policy": name, "load": load, "week": w, "account": i,
                        "capacity": scn.accounts[i].capacity,
                        "reset_pct": reset.get((i, w)),
                        "end_used_pct": used,
                        "end_deficit_pct": elapsed - used,
                        "work_share": 100.0 * work[w - 1][i] / total if total > 0 else 0.0,
                    })
    first = steady_scenario(weeks)
    return {
        "mode": "diagnose_steady", "seed": args.seed, "weeks": weeks, "load": STEADY_LOAD,
        "loads": list(STEADY_LOADS),
        "capacities": [a.capacity for a in first.accounts], "offsets": [a.offset for a in first.accounts],
        "five_hour_share": args.five_hour_share, "belief_q_scale": args.belief_q_scale,
        "cache_ttl_s": args.cache_ttl_s, "params": args.params, "rows": rows,
    }


def _diagnose_text(payload):
    weeks = payload["weeks"]
    cols = ("reset_pct", "end_used_pct", "end_deficit_pct", "work_share")
    out = [
        f"# steady-state diagnosis | loads {payload['loads']} of fleet capacity, no noise | capacities "
        f"{payload['capacities']} | reset offsets (ticks) {payload['offsets']} | weeks {weeks} | "
        f"five_hour_share {payload['five_hour_share']}",
        "# reset_pct: percent used at the reset of that account in that week (its own week, a reset",
        "#   at tick k*2016 counts in week k). end_used_pct, end_deficit_pct (own elapsed percent",
        "#   minus used percent): at the end of the calendar week, before the resets of that tick.",
        "#   work_share: percent of the units served that week that went through this account.",
    ]
    summary = []
    for load in payload["loads"]:
        out.append("")
        out.append(f"## case load {load:g}")
        for name in STEADY_POLICIES:
            mine = [r for r in payload["rows"] if r["policy"] == name and r["load"] == load]
            out.append("")
            out.append(f"{'policy':<11}{'week':>5}{'acct':>5}{'cap':>6}" + "".join(c.rjust(17) for c in cols))
            for r in mine:
                out.append(f"{r['policy']:<11}{r['week']:>5}{r['account']:>5}{r['capacity']:>6g}"
                           + "".join(_fmt(r[c]).rjust(17) for c in cols))
            spread = []
            late_spread = []
            for w in range(1, weeks + 1):
                vals = [r["reset_pct"] for r in mine if r["week"] == w and r["reset_pct"] is not None]
                sp = max(vals) - min(vals) if len(vals) > 1 else None
                spread.append("-" if sp is None else f"{sp:.1f}")
                if sp is not None and w > STEADY_SKIP_WEEKS:
                    late_spread.append(sp)
            out.append(f"{name} spread of reset_pct across accounts, by week: " + " ".join(spread))
            late = {}
            for r in mine:
                if r["week"] > STEADY_SKIP_WEEKS and r["reset_pct"] is not None:
                    late.setdefault(r["account"], []).append(r["reset_pct"])
            out.append(f"{name} mean reset_pct from week {STEADY_SKIP_WEEKS + 1} on, by account: "
                       + " ".join(f"{sum(v) / len(v):.1f}" for _k, v in sorted(late.items())))
            summary.append((load, name, sum(late_spread) / len(late_spread) if late_spread else None))
    out.append("")
    out.append(f"## summary: mean spread of reset_pct from week {STEADY_SKIP_WEEKS + 1} on")
    out.append(f"{'load':<8}{'policy':<13}{'mean spread':>12}")
    for load, name, sp in summary:
        out.append(f"{load:<8g}{name:<13}{_fmt(sp):>12}")
    out.append("")
    return "\n".join(out)


# --- CLI --------------------------------------------------------------------------


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def parse_seeds(text):
    """``"1-5"`` -> [1, 2, 3, 4, 5]; ``"2,7,9"`` -> [2, 7, 9]. ValueError if bad."""
    try:
        if "," in text:
            return [int(x) for x in text.split(",")]
        if "-" in text.strip("-"):
            lo, hi = (int(x) for x in text.split("-"))
            if hi < lo:
                raise ValueError("empty range")
            return list(range(lo, hi + 1))
        return [int(text)]
    except ValueError as exc:
        raise ValueError(f"bad --seeds {text!r}: use 1-5 or 2,7,9") from exc


def run_sensitivity(args, policies):
    """The five-hour share (truth) against the belief of it, one block each."""
    blocks = []
    for share in SENS_SHARES:
        for belief in SENS_BELIEFS:
            tasks = [("random", i, args.seed, args.weeks, share, policies, args.params, args.cache_ttl_s, belief,
                      False) for i in range(args.random)]
            per = _map(tasks, args.jobs)
            results = {p: {m: _mean([r["metrics"][p][m] for r in per]) for m in SENS_COLUMNS}
                       for p in policies}
            blocks.append({"five_hour_share": share, "belief_q_scale": belief, "results": results})
    return blocks


def run_cli(args):
    params = getattr(args, "params", {})
    args.params = params
    columns = getattr(args, "columns", None) or list(METRICS)
    belief = args.belief_q_scale
    if args.diagnose_steady:
        payload = run_diagnose(args)
        if args.json:
            return json.dumps(payload, sort_keys=True, indent=2) + "\n"
        return _diagnose_text(payload)
    policies = list(POLICIES) if args.policy == "all" else [args.policy]
    judge_on = bool(getattr(args, "judge", False))
    rows_of = [*policies, "oracle"] if judge_on else policies  # the oracle is a row, not a policy
    cols_all = [*METRICS, *JUDGE_METRICS] if judge_on else list(METRICS)
    if judge_on and not getattr(args, "columns", None):
        columns = cols_all
    label = lambda p: ORACLE_LABEL if p == "oracle" else p  # noqa: E731
    metrics_of = lambda r, p: r["oracle"] if p == "oracle" else r["metrics"][p]  # noqa: E731
    head = {
        "seed": args.seed,
        "weeks": args.weeks,
        "five_hour_share": args.five_hour_share,
        "belief_q_scale": belief,
        "switch_cost_factor": DEFAULT_SWITCH_COST,
        "cache_ttl_s": args.cache_ttl_s,
        "params": params,
        "policies": policies,
    }
    if judge_on:
        head["oracle"] = {"reference": True, "params": {**ORACLE_DEFAULTS, **params.get("oracle", {})}}
    if args.sensitivity:
        head["scenarios"] = args.random
        blocks = run_sensitivity(args, policies)
        payload = {"mode": "sensitivity", **head, "blocks": blocks}
        text = [f"# sensitivity | random {args.random} scenarios | seed {args.seed} | weeks {args.weeks} | "
                f"cache_ttl_s {args.cache_ttl_s:g} | mean over the scenarios, lower is better"]
        for blk in blocks:
            text.append("")
            text.append(f"# five_hour_share {blk['five_hour_share']:g} | belief_q_scale "
                        f"{blk['belief_q_scale']:g} | random {args.random}")
            text.append(_table("policy", [(p, blk["results"][p]) for p in policies], SENS_COLUMNS))
        text.append("")
    elif args.preset:
        names = list(PRESETS) if args.preset == "all" else [args.preset]
        seeds = args.seeds or [args.seed]
        tasks = [("preset", n, sd, args.weeks, args.five_hour_share, policies, params, args.cache_ttl_s, belief,
                  judge_on) for n in names for sd in seeds]
        per_all = _map(tasks, args.jobs)
        per = [per_all[k * len(seeds):(k + 1) * len(seeds)] for k in range(len(names))]
        results = {n: {p: {m: _mean([metrics_of(r, p)[m] for r in rs_]) for m in cols_all} for p in rows_of}
                   for n, rs_ in zip(names, per)}
        bands = {}
        for n, rs_ in zip(names, per):
            share = sum(r["share"] for r in rs_) / len(rs_)
            bands[n] = {"band": band_of(share), "volume_share": share,
                        "zero_demand_share": sum(r["zero"] for r in rs_) / len(rs_)}
        payload = {"mode": "preset", **head, "results": results, "preset_bands": bands}
        if args.seeds:
            payload["seeds"] = seeds
        which = f"seeds {args.seeds[0]}..{args.seeds[-1]} ({len(seeds)}, mean)" if args.seeds else f"seed {args.seed}"
        text = []
        for n in names:
            text.append(f"# preset {n} | band {bands[n]['band']} (volume {bands[n]['volume_share']:.2f}) | "
                        f"zero-demand ticks {bands[n]['zero_demand_share']:.1f}% | {which} | weeks {args.weeks} "
                        f"| five_hour_share {args.five_hour_share}")
            text.append(_table("policy", [(label(p), results[n][p]) for p in rows_of], columns))
            text.append("")
    elif args.sweep:
        policy, grid = parse_sweep(args.sweep)
        head["policies"] = [policy]
        head["scenarios"] = args.random
        sw = run_sweep(args, policy, grid)
        payload = {"mode": "sweep", **head, "sweep": sw}
        text = [_sweep_text(sw, head)]
    else:
        tasks = [("random", i, args.seed, args.weeks, args.five_hour_share, policies, params, args.cache_ttl_s,
                  belief, judge_on) for i in range(args.random)]
        per = _map(tasks, args.jobs)

        def summarise(sel):
            return {p: {m: _summary([metrics_of(r, p)[m] for r in sel]) for m in cols_all} for p in rows_of}

        counts = {b: sum(1 for r in per if r["band"] == b) for b in BANDS}
        results = summarise(per)
        by_band = {b: summarise([r for r in per if r["band"] == b]) for b in BANDS if counts[b]}
        zero_band = {b: sum(r["zero"] for r in per if r["band"] == b) / counts[b] for b in BANDS if counts[b]}
        zero_all = sum(r["zero"] for r in per) / len(per)
        payload = {"mode": "random", "scenarios": args.random, **head, "results": results,
                   "band_counts": counts, "bands": by_band,
                   "zero_demand_share": {"overall": zero_all, "bands": zero_band}}
        first = (f"| seed {args.seed} | weeks {args.weeks} | five_hour_share {args.five_hour_share} "
                 f"| belief_q_scale {belief:g} | cache_ttl_s {args.cache_ttl_s:g}")
        rows = []
        for p in rows_of:
            for stat in ("mean", "p10", "p50", "p90"):
                rows.append((f"{label(p)} {stat}", {m: results[p][m][stat] for m in cols_all}))
        text = [f"# overall | random {args.random} scenarios | zero-demand ticks {zero_all:.1f}% {first}",
                _table("policy stat", rows, columns), ""]
        for b in BANDS:
            if not counts[b]:
                continue
            rows = [(f"{label(p)} {stat}", {m: by_band[b][p][m][stat] for m in cols_all})
                    for p in rows_of for stat in ("mean", "p50")]
            text.append(f"# band {b} | {counts[b]} of {args.random} scenarios | "
                        f"zero-demand ticks {zero_band[b]:.1f}% {first}")
            text.append(_table("policy stat", rows, columns))
            text.append("")
    if args.json:
        return json.dumps(payload, sort_keys=True, indent=2, default=lambda o: round(o, 4)) + "\n"
    return "\n".join(text)


def _positive_int(s):
    try:
        v = int(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {s!r}") from exc
    if v < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return v


def _share(s):
    try:
        v = float(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {s!r}") from exc
    if not 0.0 < v <= 1.0:
        raise argparse.ArgumentTypeError("must be in (0, 1]")
    return v


def _scale(s):
    try:
        v = float(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {s!r}") from exc
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError("must be a number above 0")
    return v


def _seconds(s):
    try:
        v = float(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {s!r}") from exc
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError("must be a number of seconds, 0 or more")
    return v


def build_parser():
    ap = argparse.ArgumentParser(prog="rotation_sim", description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--preset", choices=[*PRESETS, "all"])
    g.add_argument("--random", type=_positive_int, metavar="N")
    g.add_argument("--diagnose-steady", action="store_true",
                   help=f"constant load {STEADY_LOAD} on capacities {STEADY_PLANS}, {STEADY_WEEKS} weeks, "
                        f"policies {STEADY_POLICIES}")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--seeds", metavar="LIST",
                    help="with --preset: average the preset over these seeds, e.g. 1-5 or 2,7,9")
    ap.add_argument("--belief-q-scale", type=_scale, default=1.0, metavar="X",
                    help="multiplies the 5-hour share that the policies believe (the truth stays)")
    ap.add_argument("--sensitivity", action="store_true",
                    help=f"with --random N: run it for the 5-hour shares {SENS_SHARES} and the belief "
                         f"scales {SENS_BELIEFS}; one compact table per pair")
    ap.add_argument("--weeks", type=_positive_int, default=None,
                    help=f"measured weeks (default {DEFAULT_WEEKS}; {STEADY_WEEKS} for --diagnose-steady)")
    ap.add_argument("--five-hour-share", type=_share, default=DEFAULT_FIVE_HOUR_SHARE)
    ap.add_argument("--cache-ttl-s", type=_seconds, default=DEFAULT_CACHE_TTL_S,
                    help="a switch is free if the work stopped at least this long before it")
    ap.add_argument("--policy", choices=[*POLICIES, "all"], default="all")
    ap.add_argument("--param", action="append", default=[], metavar="POLICY.NAME=VALUE",
                    help="policy parameter, repeatable, e.g. deficit.margin_pts=8")
    ap.add_argument("--sweep", metavar="SPEC",
                    help='grid, e.g. "deficit.margin_pts=2,4,8;deficit.dwell_s=900,1800" (needs --random)')
    ap.add_argument("--columns", metavar="LIST", help="comma separated metrics to show in tables")
    ap.add_argument("--judge", action="store_true",
                    help="add the judge: the bound and the oracle per scenario, the gap columns "
                         f"{JUDGE_METRICS} and an '{ORACLE_LABEL}' row in every table (slower)")
    ap.add_argument("--score", choices=("default", "gaps"), default="default",
                    help="with --sweep: the score to minimise; 'gaps' = distance to the oracle "
                         f"({SCORE_FORMULA_GAPS})")
    ap.add_argument("--jobs", type=_positive_int, default=os.cpu_count() or 1)
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        args.params = parse_params(args.param)
        if args.columns:
            args.columns = [c.strip() for c in args.columns.split(",")]
            bad = [c for c in args.columns if c not in (*METRICS, *JUDGE_METRICS)]
            if bad:
                raise ValueError(f"unknown metric(s): {', '.join(bad)}")
            need_judge = [c for c in args.columns if c in JUDGE_METRICS]
            if need_judge and not args.judge:
                raise ValueError(f"metric(s) {', '.join(need_judge)} need --judge")
        if args.seeds is not None:
            if not args.preset:
                raise ValueError("--seeds needs --preset")
            args.seeds = parse_seeds(args.seeds)
        if args.sensitivity:
            if not args.random:
                raise ValueError("--sensitivity needs --random N")
            if args.sweep:
                raise ValueError("--sensitivity and --sweep exclude each other")
        if args.score == "gaps" and not args.sweep:
            raise ValueError("--score gaps needs --sweep")
        if args.judge and (args.sweep or args.diagnose_steady or args.sensitivity):
            raise ValueError("--judge applies to --random and --preset tables only (--score gaps judges a sweep)")
        if args.sweep:
            if not args.random:
                raise ValueError("--sweep needs --random N")
            if args.random < 2:
                raise ValueError("--sweep needs at least 2 scenarios (a train half and a holdout half)")
            policy, _grid = parse_sweep(args.sweep)
            if args.policy not in ("all", policy):
                raise ValueError(f"--policy {args.policy} does not match the sweep of {policy}")
    except ValueError as exc:
        ap.error(str(exc))
    if args.weeks is None:
        args.weeks = STEADY_WEEKS if args.diagnose_steady else DEFAULT_WEEKS
    sys.stdout.write(run_cli(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
