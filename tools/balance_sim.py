"""S2 simulator: compare the `best`, `consume-first` and `balanced` policies.

A standalone tool, not part of the product (spec: docs/superpowers/specs/
2026-11-10-balance-strategy-design.md, "Simulation scenarios (S2)"). It
simulates a fleet of 4 Pro + 1 Max accounts under synthetic loads, with a
fake clock only. It never reads a real store, a credential or HOME, and makes
no network call. The same seed always gives the same numbers.

    uv run python tools/balance_sim.py            # text tables, caps 8/12/16 %
    uv run python tools/balance_sim.py --cap 12   # one 5-hour cap only
    uv run python tools/balance_sim.py --json     # the same numbers as JSON
    uv run python tools/balance_sim.py --margin 5 # balanced with a 5-point margin
    uv run python tools/balance_sim.py --cap 12 --sweep-margin  # S2d sweep

The default run (no ``--cap``) also prints the S2d margin sweep.

Model
-----
* Units: "Pro weeks". A Pro account holds 1 per week, a Max account 5
  (Anthropic: Max 5x = 5 times the Pro usage; sources in the spec, Q3).
* One tick is 5 minutes. Demand arrives per tick and only the active account
  serves it. Served usage adds ``served / capacity`` to the weekly percent and
  to the 5-hour window. What the active account cannot serve in a tick is
  unserved (it is not carried to the next tick).
* 5-hour window: rolling sum of the last 5 hours of usage, as a percent of a
  5-hour cap. ASSUMPTION: the cap is a share of a Pro week for Pro, times the
  capacity for Max. Anthropic does not publish this ratio, so every scenario
  runs with each share of ``FIVE_HOUR_SHARES`` (8, 12 and 16 percent).
* Weekly clock (S2c): every account has a weekly window that resets every
  7 days whether it is used or not (the user saw a reset time on an idle
  account at 0%). The first reset of each account is staggered by scenario
  (``STAGGER``); after a reset the account is at 0% and its next reset is
  7 days later. The unused quota of each week expires at its reset, also for
  an idle account. ``resets_at`` None is kept only for tests of the
  missing-reset case: such an account never resets and wastes nothing.
* An account is blocked when any window reaches 100%. A blocked account
  serves nothing. Below 100% an account can serve; the gates of balance.py
  (10 points of 5-hour headroom, 3 points of weekly headroom) are a policy
  choice of `balanced`, not a physical limit.

Policies
--------
* ``balanced`` calls the real ``claude_swap.balance.decide`` every tick with
  its real constants, and with ``threshold`` / ``weekly_threshold`` equal to
  the engine settings that `best` and `consume-first` use here.
* ``best`` and ``consume-first`` are RE-IMPLEMENTED here, because
  ``AutoSwitchEngine._rank_candidates`` is an engine method that reads
  ``self._binding`` / ``self._models`` and the persisted state file. The pure
  function ``autoswitch._recovery_is_useful`` and the engine constants are
  reused as they are. Simplifications, each one on purpose:
    1. One switch line (``threshold`` 90) for every window; no
       ``weeklyThreshold``, no per-account thresholds, no per-model windows.
       With one line, line headroom equals ``100 - max pct`` (lines.py).
    2. Usage is always readable: no failover, no unhealthy ticks, no token
       freshen, no quarantine, no API-key accounts, no adaptive polling. The
       engine sees exact numbers every tick.
    3. The cooldown is the 300 s ``cooldown_seconds`` since the last engine
       switch, kept in memory instead of the state file.
    4. The never-undo-the-last-switch bar (``_no_return_account``) is kept
       for the proactive and consume-first triggers, while the active account
       is where the engine put it. One function (``no_return_bar``) holds it
       for every policy, `balanced` included (S2f, see below). Its release is simplified: the left
       account counts as recovered by two legs of the ordinary path of
       ``_left_account_recovered``, in the engine order: dominance (S2e,
       headroom strictly more than ``active * HORIZON_HEADROOM_RATIO +
       SPENT_HEADROOM_PCT``), then headroom (at least its departure headroom
       + ``SPENT_HEADROOM_PCT``). The recovery-time leg, the failover
       snapshot branch and the unreadable-active fallback are left out
       (usage is always readable here, simplification 2). The engine applies
       the bar and its release to every strategy, so `best` gets them too.
       A recovered account is released when it beats the active by
       ``HORIZON_HEADROOM_RATIO``, or when the bar empties the ranking.
    5. The recovery time of an account is the reset of its binding window
       (the window with the higher percent). For the rolling 5-hour window
       that is when the oldest usage leaves the window (see ``reset5``).
    6. No manual switches. ``last_manual_ts`` is always None for `balanced`.
* ``balanced`` passes ``candidate_margin`` (``--margin``, default 0) to
  ``balance.decide`` (S1g). The S2d sweep runs it with ``SWEEP_MARGINS``.
* ``balanced`` and the no-return bar (S2f). The engine applies the bar to
  every strategy, so `balanced` gets the same ``no_return_bar`` as `best`
  and `consume-first` (``BALANCED_NO_RETURN_BAR``; ``--no-bar-for-balanced``
  turns it off to measure its effect). The trigger of a `balanced` tick is:
  ``at-limit`` when the active account is blocked or has no headroom (no
  bar, as in the engine); ``proactive`` when it is at or over the line (the
  forced path of ``balance.decide``); ``BALANCED_TRIGGER`` below the line.
  ASSUMPTION: the engine scopes the bar to the below-line `balanced` trigger
  as it does to ``consume-first``; S3 must add it to the scope. The barred
  account goes to ``balance.decide`` as blocked, so it is not a candidate.
  The release follows the engine: the ratio release inside
  ``no_return_bar``, and a second ``decide`` without the bar when the
  barred ranking (``balance.rank`` at the margin, or at margin 0 for the
  forced path, as its fallback) has no other candidate and the left account
  recovered.
  Remaining simplifications for `balanced`:
    a. The engine cooldown (300 s ``cooldown_seconds``) is not applied to
       `balanced`; only the 900 s cooldown of ``balance.decide`` runs.
    b. The recovery-time leg of ``_left_account_recovered`` is left out for
       every policy (simplification 4).
    c. The engine guards on top of ``balance.decide`` (failover, unhealthy
       ticks, quarantine) are left out (simplification 2).
    d. No manual switches (simplification 6).

Metrics (S2b)
-------------
* ``pace_dev``: each hour, for each Pro account with a reset time, ahead =
  weekly percent used minus the percent expected at that point of the
  account's own week (``claude_swap.pace.compute_pace``). pace_dev is the
  population standard deviation of ahead over those accounts; an hour with
  fewer than 2 such accounts gives no sample (``pace_hours`` counts samples).
  With running clocks every hour has a sample in the fleet runs.
* ``raw_spread``: max minus min of the raw weekly percent of the 4 Pro
  accounts each hour (the S2 "spread"; idle accounts count at 0%).
* ``waste_*``: unused percent of the quota at each weekly reset in the
  measured period, idle accounts included, weighted by capacity.
* ``unused_cap``: measured fleet capacity minus served demand, as a percent
  of the measured fleet capacity. It differs from ``waste_total`` only by the
  weeks that straddle the start or the end of the measured period.
* ``reserve_at_burst_start``: bursts only; at the first working hour of each
  burst, the unused weekly quota of the fleet as a percent of its capacity.
* ``flaps`` (S2f): one rule for every policy: a switch followed by a switch
  away from the same account within ``FLAP_WINDOW_S``. It is ranked.
* ``flaps_pf`` (S2f): `balanced` only, a proactive switch followed by a
  forced switch out of the same account within ``FLAP_WINDOW_S``; "-" for
  the other policies. It is not ranked. The margin sweep and the "smallest
  margin" rule use it.
* ``peak5``: the highest 5-hour percent of any account in the measured
  period. It shows how near the fleet runs to the 5-hour cap.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import deque
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

try:
    from claude_swap import autoswitch, balance, pace
    from claude_swap.settings import AutoSwitchSettings
except ImportError:  # run outside uv: add the repo's src to the path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from claude_swap import autoswitch, balance, pace
    from claude_swap.settings import AutoSwitchSettings

# --- Parameters (every @open number lives here) ----------------------------

SEED = 20261110

TICK_S = 300  # one tick is 5 minutes
HOUR_S = 3600
DAY_S = 24 * HOUR_S
WEEK_S = 7 * DAY_S
FIVE_HOUR_S = 5 * HOUR_S
TICKS_PER_HOUR = HOUR_S // TICK_S
TICKS_PER_DAY = DAY_S // TICK_S
TICKS_PER_WEEK = WEEK_S // TICK_S

WARMUP_WEEKS = 1  # run, not measured
MEASURED_WEEKS = 4

# Fleet: ids 1-4 Pro, id 5 Max. Capacity in Pro weeks per week.
PLAN_CAPACITY = {"pro": 1.0, "max": 5.0}
FLEET_PLANS = (("pro", 1), ("pro", 2), ("pro", 3), ("pro", 4), ("max", 5))
FLEET_CAPACITY = sum(PLAN_CAPACITY[p] for p, _ in FLEET_PLANS)  # 9 Pro weeks

# ASSUMPTION (@open): the 5-hour cap as a share of the weekly capacity.
# Anthropic does not publish it; the spec (S2b) asks for 8, 12 and 16 %.
FIVE_HOUR_SHARES = (0.08, 0.12, 0.16)
FIVE_HOUR_SHARE = 0.12  # default of build_fleet / run_scenario

# @open: working day. Demand arrives only in these hours of each sim day.
WORK_START_H = 9
WORK_HOURS = 10
# @open: per-tick demand noise, uniform in [1 - NOISE, 1 + NOISE].
NOISE = 0.5

# Engine numbers, read from the real settings defaults.
_DEFAULTS = AutoSwitchSettings()
THRESHOLD_PCT = _DEFAULTS.threshold  # 90
COOLDOWN_S = _DEFAULTS.cooldown_seconds  # 300
HYSTERESIS_PCT = _DEFAULTS.hysteresis_pct  # 10
WEEKLY_THRESHOLD_PCT = _DEFAULTS.weekly_threshold  # None: threshold governs all

# The switch lines that `balanced` passes to balance.rank/decide (S1f). They
# are the same engine settings that `best` and `consume-first` use here.
BALANCED_THRESHOLD = THRESHOLD_PCT  # 90.0
BALANCED_WEEKLY_THRESHOLD = WEEKLY_THRESHOLD_PCT  # None
# S2d: candidate margins (points) of the sweep, and the default of --margin.
BALANCED_MARGIN = balance.DEFAULT_CANDIDATE_MARGIN  # 0.0
SWEEP_MARGINS = (0.0, 5.0, 10.0, 15.0)
# S2d: the columns of the sweep table.
SWEEP_METRICS = ("flaps", "flaps_pf", "switches_per_day", "blocks",
                 "unserved_pct", "pace_dev_mean", "waste_pro")
# S2f: the flap metric of the "smallest margin" rule (balanced own rule).
SWEEP_FLAP_METRIC = "flaps_pf"
# S2d: a margin "cuts the flaps" when its SWEEP_FLAP_METRIC is at most this
# share of the margin-0 value.
FLAP_CUT_SHARE = 0.5
# S2e (decided): the cost a margin may add over margin 0 and still qualify.
UNSERVED_TOLERANCE_PCT = 0.5  # percentage points of unserved demand
BLOCKS_TOLERANCE = 2  # blocks in the measured period

# S2b flap: a switch, then a switch away within this time (inclusive).
FLAP_WINDOW_S = 600
FLAP_RULE_BALANCED = "proactive-then-forced"
FLAP_RULE_ANY = "any"

# S2f: balanced gets the engine no-return bar (the engine applies it to every
# strategy). False measures balanced without it (--no-bar-for-balanced).
BALANCED_NO_RETURN_BAR = True
# S2f: the triggers that the no-return bar applies to. "proactive" and
# "consume-first" are the engine scope of `_no_return_account`;
# BALANCED_TRIGGER is the assumed trigger of a below-line balanced switch.
BALANCED_TRIGGER = "balanced"
NO_RETURN_TRIGGERS = ("proactive", "consume-first", BALANCED_TRIGGER)

# pace_dev needs at least this many Pro accounts with a running week.
PACE_MIN_ACCOUNTS = 2

EPS = 1e-9

# @open: initial weekly clocks, staggered: id -> (percent used at t=0, hours
# from t=0 to the first reset). Every account resets at that offset plus each
# whole week after it, used or not (S2c). The warm-up week washes out the
# initial percent.
STAGGER = {1: (40.0, 30.0), 2: (20.0, 70.0), 3: (60.0, 110.0), 4: (10.0, 150.0),
           5: (30.0, 90.0)}

# @open: near-reset scenario. At the first working hour of the measured
# period, account 3 is set to 40% used, and its weekly reset is 3 hours later.
NEAR_RESET = {"id": 3, "pct": 40.0, "hours": 3.0}
# Account 3's stagger offset that puts a reset NEAR_RESET hours after that
# first working hour (a whole number of weeks after the offset).
NEAR_RESET_STAGGER = {**STAGGER, NEAR_RESET["id"]: (
    STAGGER[NEAR_RESET["id"]][0], WORK_START_H + NEAR_RESET["hours"])}


def _rate_per_work_hour(share_of_fleet_week: float) -> float:
    """Pro weeks per working hour for a weekly load as a share of the fleet."""
    return share_of_fleet_week * FLEET_CAPACITY / (7 * WORK_HOURS)


# S2b bursts: share of the fleet weekly capacity used in the burst days.
BURST_SHARE = 0.40
BURST_DAYS = (4, 5, 6)

# @open: the loads are a first guess (spec, "Simulation scenarios (S2)").
SCENARIOS: dict[str, dict] = {
    "light": {
        "about": "40% of fleet capacity per week, 10 working hours a day",
        "rate": _rate_per_work_hour(0.40),
        "active_days": None,  # every day
        "stagger": STAGGER,
    },
    "heavy": {
        "about": "90% of fleet capacity per week, 10 working hours a day",
        "rate": _rate_per_work_hour(0.90),
        "active_days": None,
        "stagger": STAGGER,
    },
    "bursts": {
        # S2b: 4 idle days, then 3 days that use BURST_SHARE of the fleet
        # weekly capacity in total, over the working hours of those days.
        "about": f"4 idle days, then 3 days using {BURST_SHARE:.0%} of the "
                 "fleet weekly capacity in total",
        "rate": BURST_SHARE * FLEET_CAPACITY / (len(BURST_DAYS) * WORK_HOURS),
        "active_days": BURST_DAYS,  # day of each 7-day cycle
        "stagger": STAGGER,
        # The noise is scaled per 7-day cycle so that each cycle sums to the
        # BURST_SHARE target exactly (without it one week is 1.8% off).
        "exact_per_cycle": True,
    },
    "near-reset": {
        # At the first working hour of the measured period, account 3 is set
        # to 40% used; its staggered weekly reset is 3 hours later. @open.
        "about": "light load; account 3 at 40% resets 3 h after the first "
                 "measured working hour",
        "rate": _rate_per_work_hour(0.40),
        "active_days": None,
        "stagger": NEAR_RESET_STAGGER,
        "near_reset": NEAR_RESET,
    },
}

POLICIES = ("best", "consume-first", "balanced")

# Metric keys: the JSON keys and the table columns, in this order.
METRICS = (
    "pace_dev_mean",
    "pace_dev_max",
    "pace_hours",
    "raw_spread_mean",
    "raw_spread_max",
    "waste_pro",
    "waste_max",
    "waste_total",
    "unused_cap",
    "blocks",
    "unserved_pct",
    "switches_per_day",
    "flaps",
    "flaps_pf",
    "peak5",
    "reserve_at_burst_start",
)
# Metrics with no "best policy" in the summary: not a quality measure, the
# same for every policy when all demand is served (unused_cap), or higher is
# better (reserve_at_burst_start). waste_total (S2d): with running weekly
# windows it equals capacity minus served demand except for the weeks that
# straddle the edges of the measured period, so its ranking is edge noise.
# flaps_pf (S2f): balanced only, its own flap rule; the ranked flap metric is
# "flaps", one rule for every policy.
NOT_RANKED = ("pace_hours", "peak5", "unused_cap", "reserve_at_burst_start",
              "waste_total", "flaps_pf")
# Two values closer than this are equal in the waste identity line.
IDENTITY_TOL = 1e-6
# Lower is better for every ranked metric. Low waste_max is NOT a goal (Max
# is the reserve); the summary prints it with that note.
CLAIMS = {
    "lowest_pro_waste": ("waste_pro", "balanced has the lowest Pro waste"),
    "lowest_unserved": ("unserved_pct", "balanced has the lowest unserved demand"),
}


# --- Accounts ----------------------------------------------------------------


@dataclass
class SimAccount:
    id: int
    plan: str
    capacity: float  # Pro weeks per week
    five_hour_cap: float  # Pro weeks per rolling 5 hours
    week_used: float = 0.0  # Pro weeks used in the running week
    resets_at: float | None = None  # next weekly reset; None = missing (tests)
    recent: deque = field(default_factory=deque)  # (ts, amount) inside 5 h

    def prune(self, now: float) -> None:
        while self.recent and now - self.recent[0][0] >= FIVE_HOUR_S:
            self.recent.popleft()

    def used5(self, now: float) -> float:
        self.prune(now)
        return sum(amount for _, amount in self.recent)

    def pct5(self, now: float) -> float:
        return min(100.0, self.used5(now) / self.five_hour_cap * 100.0)

    def reset5(self, now: float) -> float | None:
        """When the oldest usage leaves the rolling window (simplification)."""
        self.prune(now)
        return self.recent[0][0] + FIVE_HOUR_S if self.recent else None

    def pct7(self) -> float:
        return min(100.0, self.week_used / self.capacity * 100.0)

    def room(self, now: float) -> float:
        return max(0.0, min(self.capacity - self.week_used,
                            self.five_hour_cap - self.used5(now)))

    def blocked(self, now: float) -> bool:
        return self.pct7() >= 100.0 - EPS or self.pct5(now) >= 100.0 - EPS


@dataclass(frozen=True)
class View:
    """What a policy sees of one account at one tick."""

    id: int
    plan: str
    pct5: float
    reset5: float | None
    pct7: float
    reset7: float | None
    blocked: bool

    @property
    def headroom(self) -> float:
        return 100.0 - max(self.pct5, self.pct7)

    @property
    def over(self) -> bool:
        return max(self.pct5, self.pct7) >= THRESHOLD_PCT

    @property
    def recovery_ts(self) -> float:
        reset = self.reset5 if self.pct5 >= self.pct7 else self.reset7
        return reset if reset is not None else math.inf


def _views(accounts: Sequence[SimAccount], now: float) -> list[View]:
    return [
        View(a.id, a.plan, a.pct5(now), a.reset5(now), a.pct7(), a.resets_at,
             a.blocked(now))
        for a in accounts
    ]


def ahead_of_pace(acc: SimAccount, now: float) -> float | None:
    """Weekly percent used minus the percent expected now (pace.py formula).

    ``None`` for an account with no reset time (the missing-reset case).
    ``compute_pace`` is pure; its 24-hour suppression is turned off so every
    running week counts, from its first hour.
    """
    if acc.resets_at is None:
        return None
    iso = datetime.fromtimestamp(acc.resets_at, tz=timezone.utc).isoformat()
    result = pace.compute_pace(
        {"pct": acc.pct7(), "resets_at": iso},
        fetched_at=now,
        period_s=float(WEEK_S),
        suppress_after_reset_s=0.0,
    )
    if result is None:
        return None
    return result.actual_pct - result.expected_pct


# --- Policies ------------------------------------------------------------------


def _dominates(left_headroom: float, active_headroom: float) -> bool:
    """The dominance leg of `autoswitch._left_account_recovered` (S2e).

    The account that the engine left counts as recovered when its headroom
    is strictly more than ``active * HORIZON_HEADROOM_RATIO +
    SPENT_HEADROOM_PCT``, with the engine constants.
    """
    return left_headroom > (
        active_headroom * autoswitch.HORIZON_HEADROOM_RATIO
        + autoswitch.SPENT_HEADROOM_PCT
    )


def no_return_bar(policy, trigger: str, active_id: int,
                  by_id: dict[int, View]) -> tuple[int | None, bool]:
    """Simplified `_no_return_account`: (barred id or None, recovered).

    One function for every policy (S2f). ``policy`` holds the last switch:
    ``last_from``, ``last_to`` and ``left_headroom`` (the headroom of the
    left account at departure).
    """
    if trigger not in NO_RETURN_TRIGGERS or policy.last_from is None:
        return None, False
    if policy.last_to != active_id:
        return None, False
    left = by_id.get(policy.last_from)
    if left is None:
        return None, False
    # `_left_account_recovered`, ordinary path: the dominance leg first,
    # then the headroom leg (the recovery leg is left out, see the module
    # docstring, simplification 4).
    recovered = _dominates(left.headroom, by_id[active_id].headroom) or (
        left.headroom >= min(
            (policy.left_headroom or 0.0) + autoswitch.SPENT_HEADROOM_PCT, 100.0
        )
    )
    if recovered and left.headroom >= (
        by_id[active_id].headroom * autoswitch.HORIZON_HEADROOM_RATIO
    ):
        return None, True
    return policy.last_from, recovered


class EnginePolicy:
    """`best` or `consume-first`, re-implemented (see the module docstring)."""

    flap_rule = FLAP_RULE_ANY

    def __init__(self, consume_first: bool) -> None:
        self.consume_first = consume_first
        self.last_reason: str | None = None
        self.last_switch_at: float | None = None
        self.last_from: int | None = None
        self.last_to: int | None = None
        self.left_headroom: float | None = None

    def choose(self, views: list[View], active_id: int, now: float) -> int | None:
        by_id = {v.id: v for v in views}
        act = by_id[active_id]
        if not act.over:
            if not self.consume_first:
                return None  # below-threshold
            trigger = "consume-first"
        else:
            trigger = "at-limit" if act.headroom <= 0 else "proactive"
        if trigger in ("proactive", "consume-first") and self._in_cooldown(now):
            return None
        cands = [v for v in views if v.id != active_id]
        if not cands:
            return None

        barred, recovered = self._no_return(trigger, active_id, by_id)
        ordered = self._rank(trigger, act, cands, barred, now)
        if not ordered and barred is not None and recovered:
            ordered = self._rank(trigger, act, cands, None, now)
        if not ordered:
            return None
        target = ordered[0]
        self.last_reason = trigger
        self.last_switch_at = now
        self.last_from = active_id
        self.last_to = target
        self.left_headroom = act.headroom
        return target

    def _in_cooldown(self, now: float) -> bool:
        return self.last_switch_at is not None and now - self.last_switch_at < COOLDOWN_S

    def _no_return(self, trigger: str, active_id: int,
                   by_id: dict[int, View]) -> tuple[int | None, bool]:
        """The shared ``no_return_bar`` (S2f)."""
        return no_return_bar(self, trigger, active_id, by_id)

    def _rank(self, trigger: str, act: View, cands: list[View],
              no_return: int | None, now: float) -> list[int]:
        """Re-implementation of `_rank_candidates` with one switch line."""
        hyst_s = autoswitch.RECOVERY_HYSTERESIS_S
        ratio = autoswitch.HORIZON_HEADROOM_RATIO
        spent = autoswitch.SPENT_HEADROOM_PCT
        h_act = act.headroom
        all_above = act.over and all(c.over for c in cands)
        best_cand_h = max((c.headroom for c in cands), default=0.0)
        active_rec = act.recovery_ts if all_above else 0.0
        qualifying: list[tuple[tuple, int]] = []
        fallback: list[tuple[tuple, int]] = []
        for c in cands:
            h = c.headroom
            if h <= 0 or c.id == no_return:
                continue
            reset_ts = c.reset7 if self.consume_first else None
            rec = c.recovery_ts if all_above else 0.0
            by_rec = False
            if trigger in ("proactive", "consume-first"):
                if c.over and not all_above:
                    continue
                if all_above:
                    by_rec = autoswitch._recovery_is_useful(
                        rec, active_rec, h_act, best_cand_h, now
                    )
                    if by_rec:
                        if rec >= active_rec - hyst_s:
                            continue
                    elif h < h_act * ratio:
                        if h_act <= spent and h >= h_act and rec < active_rec - hyst_s:
                            fallback.append(((0, rec, -h), c.id))
                        continue
                elif self.consume_first:
                    if trigger == "consume-first" and (
                        reset_ts is None or act.reset7 is None or reset_ts >= act.reset7
                    ):
                        continue
                elif h - h_act < HYSTERESIS_PCT:
                    continue
            if all_above and trigger in ("proactive", "consume-first"):
                key: tuple = (0, rec, -h) if by_rec else (1, -h, rec)
            elif self.consume_first:
                key = (reset_ts if reset_ts is not None else math.inf, -h)
            else:
                key = (-h,)
            qualifying.append((key, c.id))
        qualifying = qualifying or fallback
        qualifying.sort(key=lambda t: t[0])
        return [i for _, i in qualifying]


def balanced_trigger(act: View) -> str:
    """The engine trigger of a `balanced` tick (S2f, module docstring)."""
    if act.blocked or act.headroom <= 0:
        return "at-limit"
    if act.over:
        return "proactive"
    return BALANCED_TRIGGER


class BalancedPolicy:
    """Calls the real `balance.decide` every tick, with the no-return bar."""

    # The policy's own flap rule, for the flaps_pf column (S2f).
    flap_rule = FLAP_RULE_BALANCED

    def __init__(self, margin: float = BALANCED_MARGIN,
                 bar: bool = BALANCED_NO_RETURN_BAR) -> None:
        self.margin = margin
        self.bar = bar
        self.last_proactive_ts: float | None = None
        self.last_reason: str | None = None
        # The last switch, for the no-return bar (as in EnginePolicy).
        self.last_from: int | None = None
        self.last_to: int | None = None
        self.left_headroom: float | None = None

    def choose(self, views: list[View], active_id: int, now: float) -> int | None:
        by_id = {v.id: v for v in views}
        act = by_id[active_id]
        forced = act.blocked or act.over
        barred, recovered = (
            no_return_bar(self, balanced_trigger(act), active_id, by_id)
            if self.bar else (None, False))
        if (barred is not None and recovered
                and self._ranking_empty(views, active_id, now, forced, barred)):
            # The engine releases the bar when the barred ranking is empty
            # and the left account recovered (`_rank` in autoswitch.py). With
            # an empty barred ranking `decide` returns no switch, so the
            # release is decided first and `decide` runs once per tick.
            barred = None
        decision = self._decide(views, active_id, now, forced, barred)
        if isinstance(decision, balance.SwitchTo):
            self.last_reason = decision.reason
            if decision.reason == "proactive":
                self.last_proactive_ts = now
            self.last_from, self.last_to = active_id, decision.account_id
            self.left_headroom = act.headroom
            return decision.account_id
        return None

    @staticmethod
    def _accounts(views: list[View], barred: int | None) -> list[balance.Account]:
        """The views as balance accounts; the barred account counts as blocked."""
        return [
            balance.Account(
                id=v.id,
                plan=v.plan,
                five_hour=balance.Window(v.pct5, v.reset5),
                seven_day=balance.Window(v.pct7, v.reset7),
                blocked=v.blocked or v.id == barred,
            )
            for v in views
        ]

    def _decide(self, views: list[View], active_id: int, now: float,
                forced: bool, barred: int | None):
        return balance.decide(
            self._accounts(views, barred),
            active_id,
            now,
            self.last_proactive_ts,
            None,  # no manual switches in the simulation
            forced,
            cooldown_s=balance.BALANCED_COOLDOWN_S,
            threshold=BALANCED_THRESHOLD,
            weekly_threshold=BALANCED_WEEKLY_THRESHOLD,
            candidate_margin=self.margin,
        )

    def _ranking_empty(self, views: list[View], active_id: int, now: float,
                       forced: bool, barred: int | None) -> bool:
        """True when the barred ranking has no candidate but the active one.

        The forced path falls back to margin 0, so its ranking is at margin 0.
        """
        ids = balance.rank(self._accounts(views, barred), now,
                           threshold=BALANCED_THRESHOLD,
                           weekly_threshold=BALANCED_WEEKLY_THRESHOLD,
                           candidate_margin=0.0 if forced else self.margin)
        return not [i for i in ids if i != active_id]


def make_policy(name: str, margin: float = BALANCED_MARGIN,
                bar: bool = BALANCED_NO_RETURN_BAR):
    """A policy by name; ``margin`` and ``bar`` are used by `balanced` only."""
    if name == "best":
        return EnginePolicy(consume_first=False)
    if name == "consume-first":
        return EnginePolicy(consume_first=True)
    if name == "balanced":
        return BalancedPolicy(margin, bar)
    raise ValueError(f"unknown policy: {name}")


# --- Simulation ----------------------------------------------------------------


class Switch(NamedTuple):
    ts: float
    from_id: int
    to_id: int
    reason: str | None  # balanced: "threshold" / "proactive"; engine: trigger


def count_flaps(log: Sequence[Switch], rule: str,
                window_s: float = FLAP_WINDOW_S) -> int:
    """Count switches into an account that a switch out of it follows soon.

    ``FLAP_RULE_BALANCED``: the first switch is proactive and the second one
    is forced (``threshold``). ``FLAP_RULE_ANY``: any two switches.
    """
    flaps = 0
    for first, second in zip(log, log[1:]):
        if second.from_id != first.to_id or second.ts - first.ts > window_s:
            continue
        if rule == FLAP_RULE_ANY or (
            first.reason == "proactive" and second.reason == "threshold"
        ):
            flaps += 1
    return flaps


@dataclass
class Result:
    ticks: int = 0
    measured_ticks: int = 0
    demand: float = 0.0  # measured period
    served: float = 0.0
    unserved: float = 0.0
    total_demand: float = 0.0  # whole run, warm-up included
    total_served: float = 0.0
    total_unserved: float = 0.0
    spread_samples: list = field(default_factory=list)  # raw_spread
    pace_samples: list = field(default_factory=list)  # pace_dev
    switch_log: list = field(default_factory=list)  # measured Switch rows
    flap_rule: str = FLAP_RULE_ANY  # the policy's own rule, for flaps_pf
    peak5: float = 0.0
    # (plan, capacity, unused percent) for each week that ended
    resets: list = field(default_factory=list)
    reset_log: list = field(default_factory=list)  # (reset ts, account id)
    capacity: float = 0.0  # fleet weekly capacity, Pro weeks per week
    reserve_samples: list = field(default_factory=list)  # reserve percent
    blocks: int = 0
    switches: int = 0

    def _waste(self, plans: set[str] | None) -> float | None:
        rows = [r for r in self.resets if plans is None or r[0] in plans]
        quota = sum(cap * 100.0 for _, cap, _ in rows)
        if not quota:
            return None
        return sum(cap * unused for _, cap, unused in rows) / quota * 100.0

    def _unused_cap(self) -> float | None:
        quota = self.capacity * self.measured_ticks / TICKS_PER_WEEK
        if not quota:
            return None
        return (quota - self.served) / quota * 100.0

    def metrics(self) -> dict:
        days = self.measured_ticks / TICKS_PER_DAY
        s = self.spread_samples
        p = self.pace_samples
        values = {
            "pace_dev_mean": sum(p) / len(p) if p else None,
            "pace_dev_max": max(p) if p else None,
            "pace_hours": len(p),
            "raw_spread_mean": sum(s) / len(s) if s else None,
            "raw_spread_max": max(s) if s else None,
            "waste_pro": self._waste({"pro"}),
            "waste_max": self._waste({"max"}),
            "waste_total": self._waste(None),
            "unused_cap": self._unused_cap(),
            "blocks": self.blocks,
            "unserved_pct": self.unserved / self.demand * 100.0 if self.demand else None,
            "switches_per_day": self.switches / days if days else None,
            # S2f: one rule for every policy; flaps_pf is balanced only.
            "flaps": count_flaps(self.switch_log, FLAP_RULE_ANY),
            "flaps_pf": (count_flaps(self.switch_log, FLAP_RULE_BALANCED)
                         if self.flap_rule == FLAP_RULE_BALANCED else None),
            "peak5": self.peak5,
            "reserve_at_burst_start": (
                sum(self.reserve_samples) / len(self.reserve_samples)
                if self.reserve_samples else None),
        }
        return {k: values[k] for k in METRICS}


Event = Callable[[list[SimAccount], float], None]


def simulate(
    accounts: list[SimAccount],
    demand: Sequence[float],
    policy,
    *,
    active_id: int,
    start: float = 0.0,
    measure_from: int = 0,
    events: dict[int, Event] | None = None,
    reserve_ticks: Collection[int] | None = None,
) -> Result:
    """Run ``policy`` over ``demand`` (Pro weeks per tick). Mutates ``accounts``.

    Order inside one tick: events, weekly rollover, samples (hourly
    raw_spread and pace_dev; the reserve at ``reserve_ticks``), policy
    decision, serving by the active account.
    """
    res = Result(ticks=len(demand),
                 flap_rule=getattr(policy, "flap_rule", FLAP_RULE_ANY),
                 capacity=sum(a.capacity for a in accounts))
    pro_ids = [a.id for a in accounts if a.plan == "pro"]
    by_id = {a.id: a for a in accounts}
    for k, d in enumerate(demand):
        now = start + k * TICK_S
        measured = k >= measure_from
        if events and k in events:
            events[k](accounts, now)
        for a in accounts:
            # The window resets every 7 days, used or not (S2c).
            while a.resets_at is not None and now >= a.resets_at:
                if measured:
                    res.resets.append((a.plan, a.capacity, 100.0 - a.pct7()))
                    res.reset_log.append((a.resets_at, a.id))
                a.week_used = 0.0
                a.resets_at += WEEK_S
        if measured and (now - start) % HOUR_S == 0 and pro_ids:
            pcts = [by_id[i].pct7() for i in pro_ids]
            res.spread_samples.append(max(pcts) - min(pcts))
            aheads = [x for x in (ahead_of_pace(by_id[i], now) for i in pro_ids)
                      if x is not None]
            if len(aheads) >= PACE_MIN_ACCOUNTS:
                res.pace_samples.append(statistics.pstdev(aheads))
        if measured and reserve_ticks and k in reserve_ticks:
            left = sum(a.capacity - a.week_used for a in accounts)
            res.reserve_samples.append(left / res.capacity * 100.0)

        target = policy.choose(_views(accounts, now), active_id, now)
        if target is not None and target != active_id:
            if measured:
                res.switches += 1
                res.switch_log.append(Switch(
                    now, active_id, target, getattr(policy, "last_reason", None)))
            active_id = target

        act = by_id[active_id]
        was_blocked = act.blocked(now)
        served = 0.0 if was_blocked else min(d, act.room(now))
        if served > 0:
            act.week_used = min(act.capacity, act.week_used + served)
            act.recent.append((now, served))
            if not was_blocked and act.blocked(now) and measured:
                res.blocks += 1
            if measured:
                res.peak5 = max(res.peak5, act.pct5(now))
        unserved = d - served
        res.total_demand += d
        res.total_served += served
        res.total_unserved += unserved
        if measured:
            res.measured_ticks += 1
            res.demand += d
            res.served += served
            res.unserved += unserved
    return res


def build_fleet(scenario: dict,
                five_hour_share: float = FIVE_HOUR_SHARE) -> list[SimAccount]:
    fleet = []
    for plan, i in FLEET_PLANS:
        cap = PLAN_CAPACITY[plan]
        acc = SimAccount(id=i, plan=plan, capacity=cap,
                         five_hour_cap=five_hour_share * cap)
        initial = scenario["stagger"].get(i)
        if initial is not None:
            pct, hours = initial
            acc.week_used = pct / 100.0 * cap
            acc.resets_at = hours * HOUR_S
        fleet.append(acc)
    return fleet


def build_demand(scenario: dict, seed: int) -> list[float]:
    """Per-tick demand in Pro weeks; deterministic for a seed."""
    rng = random.Random(seed)
    n = (WARMUP_WEEKS + MEASURED_WEEKS) * TICKS_PER_WEEK
    per_tick = scenario["rate"] * TICK_S / HOUR_S
    days = scenario["active_days"]
    out = []
    for k in range(n):
        t = k * TICK_S
        day, hour = divmod(t, DAY_S)
        working = WORK_START_H * HOUR_S <= hour < (WORK_START_H + WORK_HOURS) * HOUR_S
        if days is not None and day % 7 not in days:
            working = False
        noise = rng.uniform(1.0 - NOISE, 1.0 + NOISE)  # drawn every tick
        out.append(per_tick * noise if working else 0.0)
    if scenario.get("exact_per_cycle"):
        for start in range(0, n, TICKS_PER_WEEK):
            cycle = out[start:start + TICKS_PER_WEEK]
            target = per_tick * sum(1 for d in cycle if d > 0)
            total = sum(cycle)
            if total > 0:
                out[start:start + TICKS_PER_WEEK] = [d * target / total for d in cycle]
    return out


def _near_reset_events(scenario: dict) -> dict[int, Event]:
    """Set the near-reset account's percent at the first measured working hour.

    The reset time is not moved: the stagger already puts a reset
    ``spec["hours"]`` later, so the weekly cadence stays every 7 days.
    """
    spec = scenario.get("near_reset")
    if not spec:
        return {}
    tick = WARMUP_WEEKS * TICKS_PER_WEEK + WORK_START_H * TICKS_PER_HOUR

    def event(accounts: list[SimAccount], now: float) -> None:
        acc = next(a for a in accounts if a.id == spec["id"])
        acc.week_used = spec["pct"] / 100.0 * acc.capacity
        assert acc.resets_at == now + spec["hours"] * HOUR_S, "stagger mismatch"

    return {tick: event}


def _burst_start_ticks(scenario: dict) -> set[int]:
    """The first working tick of each burst (the first active day per cycle)."""
    days = scenario.get("active_days")
    if days is None:
        return set()
    n = (WARMUP_WEEKS + MEASURED_WEEKS) * TICKS_PER_WEEK
    offset = min(days) * TICKS_PER_DAY + WORK_START_H * TICKS_PER_HOUR
    return set(range(offset, n, TICKS_PER_WEEK))


def demand_for(name: str, seed: int) -> list[float]:
    """The demand of scenario ``name`` in a run; the same for every cap."""
    index = list(SCENARIOS).index(name)
    return build_demand(SCENARIOS[name], seed * 100 + index)


def run_scenario(name: str, policy_name: str, seed: int,
                 five_hour_share: float = FIVE_HOUR_SHARE,
                 margin: float = BALANCED_MARGIN,
                 bar: bool = BALANCED_NO_RETURN_BAR) -> Result:
    scenario = SCENARIOS[name]
    return simulate(
        build_fleet(scenario, five_hour_share),
        demand_for(name, seed),
        make_policy(policy_name, margin, bar),
        active_id=FLEET_PLANS[0][1],
        measure_from=WARMUP_WEEKS * TICKS_PER_WEEK,
        events=_near_reset_events(scenario),
        reserve_ticks=_burst_start_ticks(scenario),
    )


def cap_label(share: float) -> str:
    """Percent label of a 5-hour share: 0.08 -> "8"."""
    return f"{share * 100:g}"


def _lowest(policies: dict[str, dict], key: str) -> str:
    values = {p: m[key] for p, m in policies.items() if m[key] is not None}
    if not values:
        return "none"
    low = min(values.values())
    return "/".join(p for p, v in values.items() if math.isclose(v, low, abs_tol=1e-9))


def _claim(policies: dict[str, dict], key: str) -> str:
    """'yes' (balanced alone lowest), 'tie', 'no', or 'n/a' (no value)."""
    lowest = _lowest(policies, key)
    if policies.get("balanced", {}).get(key) is None:
        return "n/a"
    names = lowest.split("/")
    if "balanced" not in names:
        return "no"
    return "yes" if names == ["balanced"] else "tie"


def summarize(scenarios: dict) -> dict:
    """Best policy per metric and the two claims, computed from the numbers.

    ``scenarios`` maps a scenario name to ``{"caps": {label: {policy:
    metrics}}}``. A claim holds across caps only when it is "yes" (balanced
    strictly lowest) at every cap; a tie or a missing value is not a win.
    """
    best: dict = {}
    claims: dict = {k: {"text": text, "metric": key, "scenarios": {}}
                    for k, (key, text) in CLAIMS.items()}
    for name, scen in scenarios.items():
        caps = scen["caps"]
        best[name] = {
            key: {cap: _lowest(policies, key) for cap, policies in caps.items()}
            for key in METRICS if key not in NOT_RANKED
        }
        for k, (key, _) in CLAIMS.items():
            per_cap = {cap: _claim(policies, key) for cap, policies in caps.items()}
            missing = sorted({p for policies in caps.values()
                              for p, m in policies.items() if m[key] is None})
            claims[k]["scenarios"][name] = {
                "per_cap": per_cap,
                "no_data": missing,
                "holds_across_caps": "yes" if all(
                    v == "yes" for v in per_cap.values()) else "no",
            }
    for claim in claims.values():
        claim["all_scenarios"] = "yes" if all(
            s["holds_across_caps"] == "yes" for s in claim["scenarios"].values()
        ) else "no"
    identity = {
        name: {cap: _waste_identity(policies)
               for cap, policies in scen["caps"].items()}
        for name, scen in scenarios.items()
    }
    return {"best_policy": best, "claims": claims, "waste_identity": identity}


def _range(values: Sequence[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return max(known) - min(known) if known else None


def _waste_identity(policies: dict[str, dict]) -> dict:
    """Total waste vs capacity minus served demand, for one scenario and cap.

    ``identical`` is "yes" when capacity minus served is the same for every
    policy (it is when no policy leaves demand unserved).
    """
    pick = lambda key: {p: m[key] for p, m in policies.items()}  # noqa: E731
    unused = pick("unused_cap")
    unserved = [v for v in pick("unserved_pct").values() if v is not None]
    spread = _range(list(unused.values()))
    return {
        "unused_cap": unused,
        "waste_total": pick("waste_total"),
        "unserved_max": max(unserved) if unserved else None,
        "unused_cap_range": spread,
        "waste_total_range": _range(list(pick("waste_total").values())),
        "identical": "yes" if spread is not None and spread <= IDENTITY_TOL
                     else "no",
    }


def margin_label(margin: float) -> str:
    """Label of a margin in points: 5.0 -> "5"."""
    return f"{margin:g}"


def smallest_margin(rows: dict[str, dict]) -> str:
    """The smallest margin > 0 that cuts the flaps without a cost, or "none".

    ``rows`` maps a margin label ("0" first) to its metrics. A margin
    qualifies when its ``SWEEP_FLAP_METRIC`` (flaps_pf, the balanced own
    rule, S2f) is at most ``FLAP_CUT_SHARE`` of the margin-0 value, its blocks are at most the margin-0 blocks + ``BLOCKS_TOLERANCE``,
    and its unserved_pct is at most the margin-0 value +
    ``UNSERVED_TOLERANCE_PCT`` (S2e). With 0 flaps at margin 0 there is
    nothing to cut. A None unserved_pct (no demand) counts as 0.
    """
    flaps = SWEEP_FLAP_METRIC
    base = rows["0"]
    if not base[flaps]:
        return "none"
    unserved = lambda m: m["unserved_pct"] or 0.0  # noqa: E731
    for label in sorted((k for k in rows if k != "0"), key=float):
        m = rows[label]
        if (m[flaps] <= base[flaps] * FLAP_CUT_SHARE
                and m["blocks"] <= base["blocks"] + BLOCKS_TOLERANCE
                and unserved(m) <= unserved(base) + UNSERVED_TOLERANCE_PCT
                + IDENTITY_TOL):
            return label
    return "none"


def run_sweep(seed: int, shares: Sequence[float], scenarios: dict,
              margin: float = BALANCED_MARGIN,
              bar: bool = BALANCED_NO_RETURN_BAR) -> dict:
    """S2d: balanced at each of ``SWEEP_MARGINS``, per scenario and cap.

    ``scenarios`` is the main run (``run_all``): best and consume-first come
    from it once, for reference, and so does the balanced row whose margin
    equals ``margin``.
    """
    pick = lambda m: {k: m[k] for k in SWEEP_METRICS}  # noqa: E731
    out: dict = {"margins": [margin_label(m) for m in SWEEP_MARGINS],
                 "scenarios": {}}
    for name in SCENARIOS:
        per_cap: dict = {}
        for share in shares:
            label = cap_label(share)
            main = scenarios[name]["caps"][label]
            balanced = {}
            for m in SWEEP_MARGINS:
                if m == margin:
                    metrics = main["balanced"]
                else:
                    metrics = run_scenario(name, "balanced", seed, share, m,
                                           bar).metrics()
                balanced[margin_label(m)] = pick(metrics)
            per_cap[label] = {
                "balanced": balanced,
                "best": pick(main["best"]),
                "consume-first": pick(main["consume-first"]),
                "smallest_margin": smallest_margin(balanced),
            }
        out["scenarios"][name] = per_cap
    return out


ASSUMPTIONS = (
    "Loads, the working day, the noise and the stagger are first guesses (@open).",
    "5-hour cap = 8, 12 or 16% of a Pro week, times capacity for Max "
    "(assumption; Anthropic does not publish it). The demand is the same at "
    "every cap.",
    "best and consume-first are re-implemented; see the tool docstring.",
    f"balanced gets threshold={BALANCED_THRESHOLD} and weekly_threshold="
    f"{BALANCED_WEEKLY_THRESHOLD}, the engine settings used for the others.",
    "pace_dev = population std of (weekly % - expected %) over the Pro "
    "accounts, each at the point of its own week, hourly; hours with fewer "
    "than 2 such accounts are skipped (pace_hours = hours sampled).",
    "Weekly windows run for idle accounts too (S2c): every account resets "
    "every 7 days at its staggered offset, used or not, and the unused quota "
    "of each week counts as waste at the reset, idle accounts included. That "
    "the window restarts by itself after a reset is an inference.",
    "Waste counts the weeks that ended in the measured period. With every "
    "window running, total waste is about capacity minus served demand, so it "
    "is the same for every policy when all demand is served.",
    "near-reset: account 3 is set to 40% at the first measured working hour "
    "(09:00 of day 7); its staggered reset is 3 hours later.",
    "reserve_at_burst_start (bursts only): unused weekly quota of the fleet at "
    "09:00 of the first burst day, % of fleet capacity; higher keeps more for "
    "the burst.",
    f"flaps: two switches within {FLAP_WINDOW_S // 60} min, the same rule "
    "for every policy (ranked). flaps_pf: balanced only, proactive then "
    "forced out of the same account (not ranked; the margin sweep uses it).",
    "balanced gets the engine no-return bar (one function for every policy); "
    "its below-line trigger is assumed to be in the bar scope. Left out for "
    "balanced: the engine 300 s cooldown, the recovery-time leg of the "
    "release, failover and unhealthy ticks, manual switches.",
    "Low waste on the Max account is NOT a goal: Max is the reserve.",
)


def run_all(seed: int = SEED,
            shares: Sequence[float] = FIVE_HOUR_SHARES,
            margin: float = BALANCED_MARGIN,
            sweep: bool = True,
            bar: bool = BALANCED_NO_RETURN_BAR) -> dict:
    """The main tables (balanced at ``margin``) and, if ``sweep``, the S2d sweep.

    ``bar`` False runs balanced without the no-return bar (S2f).
    """
    caps = [cap_label(s) for s in shares]
    out: dict = {"seed": seed, "caps": caps, "margin": margin,
                 "balanced_no_return_bar": bar,
                 "assumptions": list(ASSUMPTIONS), "scenarios": {}}
    for name, scenario in SCENARIOS.items():
        out["scenarios"][name] = {
            "about": scenario["about"],
            "caps": {
                label: {p: run_scenario(name, p, seed, share, margin,
                                        bar).metrics()
                        for p in POLICIES}
                for label, share in zip(caps, shares)
            },
        }
    out["summary"] = summarize(out["scenarios"])
    if sweep:
        out["sweep"] = run_sweep(seed, shares, out["scenarios"], margin, bar)
    return out


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value:.2f}"


def render_summary(summary: dict) -> str:
    lines = ["Summary (computed from the numbers above; lower is better)"]
    lines.append("Best policy per metric, by cap (not ranked: "
                 + ", ".join(NOT_RANKED) + "; waste_total is edge noise):")
    for name, metrics in summary["best_policy"].items():
        lines.append(f"  {name}:")
        for key, per_cap in metrics.items():
            note = "  (Max is the reserve: not a goal)" if key == "waste_max" else ""
            cells = "  ".join(f"{cap}%={p}" for cap, p in per_cap.items())
            lines.append(f"    {key:<16} {cells}{note}")
    for claim in summary["claims"].values():
        lines.append(f"Claim: {claim['text']} ({claim['metric']})")
        for name, s in claim["scenarios"].items():
            cells = " ".join(f"{cap}%={v}" for cap, v in s["per_cap"].items())
            nodata = f" [no data: {', '.join(s['no_data'])}]" if s["no_data"] else ""
            lines.append(f"  {name:<11} {cells}{nodata} -> conclusion holds "
                         f"across caps: {s['holds_across_caps']}")
        lines.append(f"  {'all':<11} -> conclusion holds across caps: "
                     f"{claim['all_scenarios']}")
    lines.append("Waste identity: total waste equals capacity minus served demand: "
                 "identical across policies when unserved is 0")
    lines.append("  (values per policy: " + " / ".join(POLICIES) + "; waste_total "
                 "differs from capacity-served only by the weeks that straddle "
                 "the edges of the measured period)")
    for name, per_cap in summary.get("waste_identity", {}).items():
        for cap, w in per_cap.items():
            cells = lambda d: "/".join(_fmt(v) for v in d.values())  # noqa: E731
            lines.append(
                f"  {name:<11}{cap}%: unserved max {_fmt(w['unserved_max'])}"
                f" -> capacity-served {cells(w['unused_cap'])}"
                f" (range {_fmt(w['unused_cap_range'])}, identical: {w['identical']});"
                f" waste_total {cells(w['waste_total'])}"
                f" (range {_fmt(w['waste_total_range'])})")
    return "\n".join(lines)


def render_sweep(sweep: dict) -> list[str]:
    """S2d: one compact table per scenario and cap."""
    lines = ["Margin sweep: balanced with candidate_margin in points; best and "
             "consume-first once, for reference"]
    widths = {k: max(len(k), 7) for k in SWEEP_METRICS}
    lw = max(len("consume-first"), len("margin")) + 1
    for name, per_cap in sweep["scenarios"].items():
        for cap, row in per_cap.items():
            lines.append(f"Sweep: {name}, 5-hour cap {cap}%")
            lines.append("  " + "margin".ljust(lw)
                         + " ".join(k.rjust(widths[k]) for k in SWEEP_METRICS))
            table = [*row["balanced"].items(), ("best", row["best"]),
                     ("consume-first", row["consume-first"])]
            for label, m in table:
                lines.append("  " + label.ljust(lw)
                             + " ".join(_fmt(m[k]).rjust(widths[k])
                                        for k in SWEEP_METRICS))
    return lines


def render_margin_choice(sweep: dict) -> list[str]:
    """S2d: the computed margin choice, one line per scenario and cap."""
    lines = [f"Margin: smallest margin that cuts {SWEEP_FLAP_METRIC} by at least "
             f"{FLAP_CUT_SHARE * 100:g} percent, with unserved at most "
             f"{UNSERVED_TOLERANCE_PCT:g} points and blocks at most "
             f"{BLOCKS_TOLERANCE} blocks above the margin-0 value (or 'none'):"]
    for name, per_cap in sweep["scenarios"].items():
        for cap, row in per_cap.items():
            lines.append(f"  smallest margin {name:<11}{cap}%: "
                         f"{row['smallest_margin']}")
    return lines


def render(data: dict) -> str:
    lines = [
        f"balance_sim  seed={data['seed']}  fleet=4 Pro + 1 Max  "
        f"warm-up={WARMUP_WEEKS} wk  measured={MEASURED_WEEKS} wk  tick={TICK_S}s"
        + ("" if data.get("balanced_no_return_bar", True)
           else "  balanced WITHOUT the no-return bar"),
        "pace_dev = std of (weekly % - expected %) among the Pro accounts "
        "(hourly); raw_spread = max-min weekly % among the 4 Pro accounts;",
        "waste = % of quota unused when a week ended (idle accounts included); "
        "unused_cap = % of fleet capacity not served;",
        "the 5-hour cap is a % of a Pro week; peak5 = highest 5-hour %; "
        "reserve_at_burst_start = % of fleet quota left at a burst start",
        "",
    ]
    widths = {k: max(len(k), 7) for k in METRICS}
    pw = max(len(p) for p in POLICIES) + 1
    for name, scen in data["scenarios"].items():
        lines.append(f"Scenario: {name} ({scen['about']})")
        lines.append("cap".ljust(5) + "policy".ljust(pw)
                     + " ".join(k.rjust(widths[k]) for k in METRICS))
        for cap, policies in scen["caps"].items():
            for policy, m in policies.items():
                lines.append(f"{cap}%".ljust(5) + policy.ljust(pw)
                             + " ".join(_fmt(m[k]).rjust(widths[k]) for k in METRICS))
        lines.append("")
    if "sweep" in data:
        lines.extend(render_sweep(data["sweep"]))
        lines.append("")
    lines.append(render_summary(data["summary"]))
    if "sweep" in data:
        lines.extend(render_margin_choice(data["sweep"]))
    lines.append("")
    lines.append("Notes:")
    lines.extend(f"- {a}" for a in data["assumptions"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument(
        "--cap", type=float, default=None, metavar="PCT",
        help="5-hour cap as a percent of a Pro week (default: run "
             + ", ".join(cap_label(s) for s in FIVE_HOUR_SHARES) + ")",
    )
    parser.add_argument(
        "--margin", type=float, default=BALANCED_MARGIN, metavar="POINTS",
        help="candidate margin of balanced, in points (default %(default)g)",
    )
    parser.add_argument(
        "--sweep-margin", action="store_true",
        help="also run balanced with margins "
             + ", ".join(margin_label(m) for m in SWEEP_MARGINS)
             + " (always on without --cap)",
    )
    parser.add_argument(
        "--no-bar-for-balanced", action="store_true",
        help="run balanced without the engine no-return bar (S2f)",
    )
    args = parser.parse_args(argv)
    if args.cap is not None and not 0 < args.cap <= 100:
        parser.error("--cap must be a percent in (0, 100]")
    if not 0.0 <= args.margin <= balance.MAX_CANDIDATE_MARGIN:
        parser.error(f"--margin must be in [0, {balance.MAX_CANDIDATE_MARGIN:g}]")
    shares = FIVE_HOUR_SHARES if args.cap is None else (args.cap / 100.0,)
    sweep = args.sweep_margin or args.cap is None
    data = run_all(args.seed, shares, args.margin, sweep,
                   bar=not args.no_bar_for_balanced)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
