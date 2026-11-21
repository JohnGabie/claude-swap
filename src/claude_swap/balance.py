"""Usage balancing for the ``balanced`` strategy: which account to spend now.

Pure functions: no I/O, no clock reads, no engine state. The caller passes
the measured windows, the plan string, the engine's ``blocked`` flag and the
time ``now`` (POSIX seconds). This module returns a ranking and a decision.

The rule is the scaled line (spec:
docs/superpowers/specs/2026-11-15-adaptive-rotation-design.md, "Simulation
round 5 (stateless ``f``) and the port to the engine"). It replaces the old
class rule (Pro first). Every account is used in proportion to its capacity,
so that the weekly percent used is about the same on every account.

1. Usable accounts (unchanged). An account is usable when it is not blocked,
   is measured with finite values, has at least ``MIN_HEADROOM_5H`` points
   free in the 5-hour window and ``MIN_HEADROOM_7D`` in the week, and sits
   strictly below each switch line: ``threshold`` for the 5-hour window, and
   ``weekly_threshold`` (or ``threshold`` when it is ``None``) for the week.
   Plans in ``NO_FIVE_HOUR_LIMIT_PLANS`` have no 5-hour gate and no 5-hour
   line. A window whose reset time has passed counts as 0% used.
   Candidate margin (S1g). A CANDIDATE must also sit ``candidate_margin``
   points below each line (clamped into 0..``MAX_CANDIDATE_MARGIN``). The
   active account is judged at the real line (margin 0).
2. The line. For each measured, not blocked account ``i``:

       c_i = PLAN_CAPACITY[plan]            (DEFAULT_CAPACITY for another word)
       e_i = clip(1 - (weekly reset - now) / 168 h, 0, 1)
       u_i = weekly percent / 100

   A weekly reset time that has passed (``weekly reset <= now``) describes
   a week that ended. The weekly reset is fixed, so the new week started at
   that reset time:

       e_i = ((now - weekly reset) mod 168 h) / 168 h
       u_i = 0

   The next weekly reset of such an account is the first one after ``now``:
   ``weekly reset + k * 168 h``.
       f   = clip(F_BOOST * sum(c_i * u_i) / sum(c_i * e_i), F_MIN, 1)
       deficit_i = f * e_i * 100 - weekly percent

   ``f`` is the height of the line: the share of its quota that each account
   aims at by its own reset. It is the use of the fleet so far divided by
   what the full line would be so far. When ``sum(c_i * e_i)`` is below
   ``F_FLOOR``, ``f`` is ``F_MIN``. An account with no weekly reset time has
   ``e_i = 0`` and ``u_i = 0`` in the sums, and its deficit is minus its
   weekly percent.
3. Order of the usable accounts: the largest deficit first. Ties: less
   weekly percent used, then the lower id.
   Loss guard: when ``f >= GUARD_F``, the usable accounts whose next weekly
   reset is within ``GUARD_HOURS`` go first, the earliest reset first. The
   others follow by deficit.
4. ``decide`` says when to leave the active account, in this order:
   a. A forced switch comes first: the caller passes the threshold, or the
      active account is blocked, gated or at a switch line. It moves to the
      best other usable account (reason ``"threshold"``, no cooldown, no
      deficit margin). With no candidate below the margin it falls back to
      margin 0 (S1h). The threshold from the caller wins over an unmeasured
      active account.
   b. An unmeasured active account that is not at the threshold returns
      ``Stay``.
   c. An ``active_id`` missing from the list follows the proactive path.
   d. The proactive path takes the first account of the ranking other than
      the active one. It moves only when that deficit exceeds the deficit of
      the active account by more than ``DEFICIT_MARGIN_PTS``, or when that
      account is a loss-guard account and the active account is not. A
      loss-guard active account stays. Then the cooldowns apply
      (``cooldown_s`` after the last proactive switch and after the last
      manual switch): a running cooldown gives ``Hold``.

The engine in ``autoswitch.py`` keeps its own guards on top of this module.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# --- Tuning table --------------------------------------------------------
# Every number of the strategy lives here. Source of the scaled-line values:
# the simulation rounds in the spec of 2026-11-15 (rounds 3 to 5, and the
# user's decisions after round 4). Change them only with the user.

# Capacity of a plan in units of one Pro week (spec, "Simulation round 5":
# pro 1, max 5, ultra 10, any other word 1). Measured capacity comes later.
PLAN_CAPACITY = {"pro": 1.0, "max": 5.0, "ultra": 10.0}
DEFAULT_CAPACITY = 1.0
# Round 3: a rising demand is not capped by its own past.
F_BOOST = 1.1
# Round 3: the lowest height of the line.
F_MIN = 0.1
# Round 5 (`SCALED_STATELESS_FLOOR`): below this value of sum(c_i * e_i)
# the ratio is not reliable (every account has just reset); f is F_MIN.
F_FLOOR = 0.01
# Round 3 (`guard_f`): the loss guard works only at or above this f.
GUARD_F = 0.9
# Round 4: a guard of 12 hours scored better than 24 hours.
GUARD_HOURS = 12.0
# Rounds 3 and 4, and the user's decision: a proactive switch needs the
# best deficit to exceed the active deficit by more than this many points.
DEFICIT_MARGIN_PTS = 4.0

# decided: gates, in headroom points (100 - percent used).
MIN_HEADROOM_5H = 10.0
MIN_HEADROOM_7D = 3.0
# decided: cooldown (dwell) after a proactive `balanced` switch, and the
# same after a manual switch. The engine passes `balancedCooldownSeconds`.
BALANCED_COOLDOWN_S = 900.0
# Runway rule: a proactive target must last this long at the measured burn.
MIN_RUNWAY_S = 1800.0

# decided: the engine's switch line. It mirrors `AutoSwitchSettings.threshold`
# (settings.py). A candidate must sit strictly below it in every window, and
# below `weekly_threshold` (when set) in the weekly window; else the engine
# forces it out on the next tick. The gates above stay as extra floors.
DEFAULT_THRESHOLD = 90.0

# proposed (S1g): a CANDIDATE must sit this many points below each switch
# line, so that the next tick of work does not push it over the line. The
# active account is judged at the real line. Clamped into
# 0..MAX_CANDIDATE_MARGIN. The engine passes `balancedMargin`.
DEFAULT_CANDIDATE_MARGIN = 0.0
MAX_CANDIDATE_MARGIN = 50.0

# Length of the weekly window: a fact, not a tuning value.
WEEK_H = 168.0
WEEK_S = WEEK_H * 3600.0
HOUR_S = 3600.0

# Per Anthropic (read 2026-11-10), verified Ultra accounts have no 5-hour
# limit:
#   https://support.claude.com/en/articles/17728952-what-is-the-ultra-plan
NO_FIVE_HOUR_LIMIT_PLANS = frozenset({"ultra"})


# --- Input ---------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """One usage window: percent used (0-100) and reset time, if known."""

    pct: float
    resets_at: float | None


@dataclass(frozen=True)
class Account:
    """One account as the caller measured it. A ``None`` window is unmeasured."""

    id: int
    plan: str
    five_hour: Window | None
    seven_day: Window | None
    blocked: bool = False


# --- Output --------------------------------------------------------------


@dataclass(frozen=True)
class Score:
    id: int
    usable: bool
    # The deficit in percent points; only meaningful when usable.
    score: float
    reason: str
    # True when the loss guard puts this usable account first.
    guard: bool = False
    # Weekly headroom points after rollover; 100 minus it breaks a tie.
    headroom_7d: float = 0.0
    # Weekly reset time (POSIX seconds) of a loss-guard account.
    reset_7d: float | None = None
    # When a gated account is usable again (POSIX seconds), if known.
    blocked_until: float | None = None


@dataclass(frozen=True)
class Stay:
    """Keep the active account."""


@dataclass(frozen=True)
class SwitchTo:
    account_id: int
    reason: Literal["threshold", "proactive"]


@dataclass(frozen=True)
class Hold:
    """A proactive switch would run, but a cooldown holds it back."""

    reason: Literal["cooldown"] = "cooldown"


Decision = Stay | SwitchTo | Hold


# --- Scoring -------------------------------------------------------------


def _plan_key(plan: str | None) -> str:
    return (plan or "").strip().lower()


def plan_capacity(plan: str | None) -> float:
    """Capacity of a plan word; ``DEFAULT_CAPACITY`` for an unknown word."""
    return PLAN_CAPACITY.get(_plan_key(plan), DEFAULT_CAPACITY)


def _window(window: Window | None, now: float) -> tuple[float | None, float | None]:
    """Clamp a window; a reset in the past means the window rolled over."""
    if window is None or not math.isfinite(window.pct):
        return None, None
    if window.resets_at is not None and not math.isfinite(window.resets_at):
        return None, None
    if window.resets_at is not None and window.resets_at <= now:
        return 0.0, None
    return max(0.0, min(100.0, float(window.pct))), window.resets_at


def five_hour_room(account: Account, now: float, threshold: float = DEFAULT_THRESHOLD) -> float:
    """Room left in the 5-hour window, in Pro percent points: capacity times
    the points below the switch line. Infinite for a plan with no 5-hour
    limit; 0.0 for an unmeasured window."""
    if _plan_key(account.plan) in NO_FIVE_HOUR_LIMIT_PLANS:
        return math.inf
    pct5, _ = _window(account.five_hour, now)
    if pct5 is None:
        return 0.0
    return plan_capacity(account.plan) * max(0.0, threshold - pct5)


def clamp_margin(candidate_margin: float) -> float:
    """Clamp a margin into 0..MAX_CANDIDATE_MARGIN; NaN becomes the default."""
    if math.isnan(candidate_margin):
        return DEFAULT_CANDIDATE_MARGIN
    return max(0.0, min(MAX_CANDIDATE_MARGIN, float(candidate_margin)))


def _weekly(account: Account, now: float) -> tuple[float, float, bool] | None:
    """``(weekly percent, elapsed share e, counts in the fleet sums)``.

    ``None`` for an unmeasured account (a missing window or a non-finite
    value; the 5-hour window does not count for ``NO_FIVE_HOUR_LIMIT_PLANS``).
    When the weekly reset has passed, a new week started at that reset: the
    percent is 0 and ``e`` is the share of the new week that has elapsed.
    ``e`` is 0 with no weekly reset time, and such an account does not count
    in the sums.
    """
    pct7, _ = _window(account.seven_day, now)
    if pct7 is None:
        return None
    if _plan_key(account.plan) not in NO_FIVE_HOUR_LIMIT_PLANS:
        if _window(account.five_hour, now)[0] is None:
            return None
    reset = account.seven_day.resets_at
    if reset is None:
        return pct7, 0.0, False
    if reset <= now:
        elapsed = ((now - reset) % WEEK_S) / WEEK_S
    else:
        elapsed = max(0.0, min(1.0, 1.0 - (reset - now) / WEEK_S))
    return pct7, elapsed, True


def _next_weekly_reset(window: Window, now: float) -> float | None:
    """The first weekly reset after ``now``; ``None`` with no reset time.

    A reset time that has passed repeats every week: the result is
    ``resets_at + k * 168 h`` for the smallest ``k`` that puts it after ``now``.
    """
    reset = window.resets_at
    if reset is None or reset > now:
        return reset
    return reset + (math.floor((now - reset) / WEEK_S) + 1) * WEEK_S


def line_height(accounts: Sequence[Account], now: float) -> float:
    """The height ``f`` of the line, from the current readings only.

    ``f = clip(F_BOOST * sum(c_i * u_i) / sum(c_i * e_i), F_MIN, 1.0)`` over
    the measured, not blocked accounts. ``f`` is ``F_MIN`` when the
    denominator is below ``F_FLOOR``.
    """
    used = line = 0.0
    for account in accounts:
        if account.blocked:
            continue
        weekly = _weekly(account, now)
        if weekly is None or not weekly[2]:
            continue
        pct7, elapsed, _ = weekly
        capacity = plan_capacity(account.plan)
        used += capacity * pct7 / 100.0
        line += capacity * elapsed
    if line < F_FLOOR:
        return F_MIN
    return max(F_MIN, min(1.0, F_BOOST * used / line))


def deficit(account: Account, now: float, f: float) -> float:
    """``f * e_i * 100 - weekly percent``; 0.0 for an unmeasured account."""
    weekly = _weekly(account, now)
    if weekly is None:
        return 0.0
    pct7, elapsed, _ = weekly
    return f * elapsed * 100.0 - pct7


def score_account(
    account: Account,
    now: float,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    weekly_threshold: float | None = None,
    candidate_margin: float = DEFAULT_CANDIDATE_MARGIN,
    f: float = 1.0,
    min_headroom: bool = True,
) -> Score:
    """Score one account. ``usable`` is False at or over a switch line.

    The weekly line is ``weekly_threshold``, or ``threshold`` when it is
    ``None``. The 5-hour line is ``threshold``, except for plans in
    ``NO_FIVE_HOUR_LIMIT_PLANS``. Each line is lowered by
    ``candidate_margin`` (clamped into 0..``MAX_CANDIDATE_MARGIN``).

    ``f`` is the height of the line of the fleet (``line_height``); the
    default 1.0 is the full line. ``score`` is the deficit at that ``f``.

    ``min_headroom=False`` drops the ``MIN_HEADROOM_5H`` / ``MIN_HEADROOM_7D``
    gates: the account then only needs to be below its lines. The forced
    switch uses it as its last fallback.
    """
    margin = clamp_margin(candidate_margin)
    line7 = (weekly_threshold if weekly_threshold is not None else threshold) - margin
    line5 = threshold - margin
    if account.blocked:
        return Score(account.id, False, 0.0, "blocked")
    pct5, reset5 = _window(account.five_hour, now)
    pct7, reset7 = _window(account.seven_day, now)
    has_5h_limit = _plan_key(account.plan) not in NO_FIVE_HOUR_LIMIT_PLANS
    if pct7 is None or (has_5h_limit and pct5 is None):
        return Score(account.id, False, 0.0, "unmeasured")

    head7 = 100.0 - pct7
    if min_headroom and head7 < MIN_HEADROOM_7D:
        return Score(account.id, False, 0.0, "weekly spent", headroom_7d=head7,
                     blocked_until=reset7)
    if not pct7 < line7:
        return Score(account.id, False, 0.0, "weekly threshold",
                     headroom_7d=head7, blocked_until=reset7)
    if has_5h_limit and pct5 is not None:
        head5 = 100.0 - pct5
        if min_headroom and head5 < MIN_HEADROOM_5H:
            return Score(account.id, False, 0.0, "5h spent", headroom_7d=head7,
                         blocked_until=reset5)
        if not pct5 < line5:
            return Score(account.id, False, 0.0, "5h threshold",
                         headroom_7d=head7, blocked_until=reset5)

    # Loss guard: the week ends within GUARD_HOURS, at a high f. A reset
    # time that has passed counts by its next weekly reset.
    next7 = _next_weekly_reset(account.seven_day, now)
    guard = (
        f >= GUARD_F
        and next7 is not None
        and next7 - now <= GUARD_HOURS * HOUR_S
    )
    reason = "loss guard" if guard else "deficit"
    return Score(account.id, True, deficit(account, now, f), reason, guard,
                 head7, reset_7d=next7)


def _rank_key(s: Score) -> tuple:
    """Guard accounts by the earliest reset; then deficit, less used, id."""
    if s.guard:
        return (0, s.reset_7d, -s.score, -s.headroom_7d, s.id)
    return (1, 0.0, -s.score, -s.headroom_7d, s.id)


def _ranked(accounts: Sequence[Account], now: float, *, threshold: float,
            weekly_threshold: float | None,
            candidate_margin: float, f: float | None = None,
            min_headroom: bool = True) -> list[Score]:
    """Usable accounts, best first (see ``_rank_key``)."""
    if f is None:
        f = line_height(accounts, now)
    scores = (
        score_account(a, now, threshold=threshold,
                      weekly_threshold=weekly_threshold,
                      candidate_margin=candidate_margin, f=f,
                      min_headroom=min_headroom)
        for a in accounts
    )
    return sorted((s for s in scores if s.usable), key=_rank_key)


def rank(
    accounts: Sequence[Account],
    now: float,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    weekly_threshold: float | None = None,
    candidate_margin: float = DEFAULT_CANDIDATE_MARGIN,
) -> list[int]:
    """Return the ids of usable accounts, best first.

    A usable account is strictly below each of its switch lines minus
    ``candidate_margin`` (see ``score_account``) and passes the headroom
    gates. The order is the loss guard, then the largest deficit, then less
    weekly percent used, then the lower id.
    """
    ranked = _ranked(accounts, now, threshold=threshold,
                     weekly_threshold=weekly_threshold,
                     candidate_margin=candidate_margin)
    return [s.id for s in ranked]


# --- Decision ------------------------------------------------------------


def _cooldown_running(last_ts: float | None, now: float, cooldown_s: float) -> bool:
    return last_ts is not None and now - last_ts < cooldown_s


def _active_score(accounts: Sequence[Account], active_id: int | None,
                  now: float, *, threshold: float,
                  weekly_threshold: float | None, f: float) -> Score | None:
    """Score of the active account, or ``None`` when it is not in the list."""
    # The active account is judged against the user's lines only: the
    # MIN_HEADROOM gates keep a move from landing on an almost full account,
    # they are not a reason to leave one (week of 2026-11-16: with a 99% line
    # the active account was forced out at 90-92% eight times).
    for a in accounts:
        if a.id == active_id:
            return score_account(a, now, threshold=threshold,
                                 weekly_threshold=weekly_threshold, f=f,
                                 min_headroom=False)
    return None


def _forced_out(active_score) -> bool:
    """True when the active account is measured but unusable."""
    return (
        active_score is not None
        and not active_score.usable
        and active_score.reason != "unmeasured"
    )


def decide(
    accounts: Sequence[Account],
    active_id: int | None,
    now: float,
    last_proactive_ts: float | None,
    last_manual_ts: float | None,
    active_at_threshold: bool,
    cooldown_s: float = BALANCED_COOLDOWN_S,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    weekly_threshold: float | None = None,
    candidate_margin: float = DEFAULT_CANDIDATE_MARGIN,
    burn_rate: float = 0.0,
) -> Decision:
    """Decide whether to leave the active account, and for which account.

    Order of the rules:

    1. A forced switch happens when ``active_at_threshold`` is True, or the
       active account is unusable (blocked, below the 5-hour or weekly
       gate, or at or over a switch line: ``threshold``, and
       ``weekly_threshold`` for the weekly window when it is set). The
       caller's threshold wins even if the active account is unmeasured. The result is ``SwitchTo(..., reason="threshold")``. It
       ignores the cooldown. With no usable candidate it returns ``Stay``.
    2. An unmeasured active account (a ``None`` window or a non-finite
       value), not blocked and not at the threshold, returns ``Stay``.
    3. An ``active_id`` that is not in the list follows the proactive path.
    4. The proactive path applies the deficit margin, the loss guard and
       the cooldown. ``best`` is the first account of the ranking other than
       the active account. With a usable active account (measured, not
       blocked, not gated, below its real lines):

       * an active account that the loss guard protects returns ``Stay``;
       * a ``best`` that the loss guard protects moves without the margin;
       * else the move needs ``deficit(best) - deficit(active)`` above
         ``DEFICIT_MARGIN_PTS``.

       A move then waits for the cooldowns: ``Hold`` while one runs.

       Runway rule: ``burn_rate`` is the measured use of the active
       account in Pro percent points of the 5-hour window per second (0.0
       when unknown). With a burn, a proactive target needs a 5-hour room
       (``five_hour_room``: capacity times the points below the line) that
       lasts ``MIN_RUNWAY_S`` at that burn, or at least the room the active
       account has left. With no burn the rule changes nothing.

    ``candidate_margin`` (S1g) applies to the candidate list only. The
    active account is scored at margin 0, for the forced switch and for the
    deficit margin: an active account inside the margin but below its lines is
    not forced out.

    Forced-switch fallback (S1h): when the forced switch finds no candidate
    below the margin, it ranks the candidates again at margin 0 for this
    decision and moves to the best of them. The target is then only below
    the real lines. With no candidate at margin 0 either, it returns
    ``Stay``. The proactive path keeps the margin and has no fallback. With
    margin 0 the fallback changes nothing.

    With no usable target this returns ``Stay``; the caller handles the case
    where every account is blocked.
    """
    lines = {"threshold": threshold, "weekly_threshold": weekly_threshold}
    # One line height for the whole decision: it does not depend on the margin.
    f = line_height(accounts, now)
    ranked = _ranked(accounts, now, **lines, candidate_margin=candidate_margin,
                     f=f)
    # The active account is judged at the real lines (margin 0).
    active_score = _active_score(accounts, active_id, now, **lines, f=f)

    if active_at_threshold or _forced_out(active_score):
        # Forced switch: no cooldown and no deficit margin apply. With no
        # candidate below the margin, fall back to margin 0 (S1h).
        targets = [s for s in ranked if s.id != active_id]
        if not targets:
            targets = [s for s in _ranked(accounts, now, **lines,
                                          candidate_margin=0.0, f=f)
                       if s.id != active_id]
        if not targets:
            # Last fallback: any account below the user's own lines, even
            # with less than MIN_HEADROOM_5H points of room. Staying on an
            # account at its limit stops the work (2026-11-16 17:50-18:51
            # and 2026-11-18 05:55-06:20: a Max account at 91% of a 99% line
            # was refused for its 9 points of room while the active account
            # sat at 100%).
            targets = [s for s in _ranked(accounts, now, **lines,
                                          candidate_margin=0.0, f=f,
                                          min_headroom=False)
                       if s.id != active_id]
        if targets:
            return SwitchTo(targets[0].id, "threshold")
        return Stay()

    # An unmeasured active account (not at threshold) gives no reason to
    # leave it. An active_id that is not in the list follows the proactive
    # path below.
    if active_score is not None and active_score.reason == "unmeasured":
        return Stay()

    others = [s for s in ranked if s.id != active_id]
    # Runway rule: under a measured burn, a proactive move needs a target
    # whose 5-hour room lasts MIN_RUNWAY_S at that burn, or that has at least
    # the room the active account has left.
    # Incident 2026-11-16: under a heavy load the rule moved from a Max
    # account at 80% (room of 5 * 19 = 95 Pro points) to a Pro account at 29%
    # (room of 70 Pro points); the Pro window was full 9 minutes later and
    # the work stopped. The forced switch (rule 1) is not limited by this.
    if burn_rate > 0.0 and math.isfinite(burn_rate):
        by_id = {a.id: a for a in accounts}
        active_account = by_id.get(active_id)
        need = burn_rate * MIN_RUNWAY_S
        if active_account is not None:
            need = min(need, five_hour_room(active_account, now, threshold))
        others = [s for s in others if five_hour_room(by_id[s.id], now, threshold) >= need]
    if not others:
        return Stay()
    best = others[0]
    # The active account is compared at margin 0. With margin 0 this is the
    # same Score as its entry in ``ranked``.
    active = active_score if active_score is not None and active_score.usable else None
    if active is not None:
        # A loss-guard active account keeps spending its expiring quota.
        if active.guard:
            return Stay()
        if not best.guard and not best.score - active.score > DEFICIT_MARGIN_PTS:
            return Stay()

    if _cooldown_running(last_proactive_ts, now, cooldown_s) or _cooldown_running(
        last_manual_ts, now, cooldown_s
    ):
        return Hold("cooldown")
    return SwitchTo(best.id, "proactive")
