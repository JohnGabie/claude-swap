"""S3b-2: the ``balanced`` strategy in the auto-switch engine.

Spec: docs/superpowers/specs/2026-11-10-balance-strategy-design.md, items
S3b-2, "Cooldown of balanced", "Decisions after S1" and "Result after S1i
and S2f". Project rules P1 (no email or plan in output), P3 (additive JSON).

The first class pins the old paths (``best`` and ``consume-first``) on a
fixed fleet: these tests were recorded on the code before S3b-2 and must
pass before and after the change.
"""

from __future__ import annotations

import json
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.autoswitch import (
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    _balance_accounts,
    _PlanCache,
    _state_ts,
)
from claude_swap.usage_store import UsageEntry
from tests.test_autoswitch import (
    EngineHarness,
    _entry_for,
    _iso_at,
    _usage,
    _usage7,
)

R_SOON = "2024-01-05T00:00:00Z"
R_LATER = "2024-01-08T00:00:00Z"
R_LATEST = "2024-01-10T00:00:00Z"


def _shape(events) -> list[dict]:
    out = []
    for e in events:
        d = e.to_json()
        d.pop("ts")
        out.append(d)
    return out


def _fleet(temp_home: Path, strategy: str, **kw) -> EngineHarness:
    h = EngineHarness(temp_home, strategy=strategy, **kw)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _compact(event: dict) -> tuple:
    if event["event"] == "poll":
        return ("poll", event["active"]["number"])
    if event["event"] == "switch":
        return ("switch", event["trigger"], event["from"]["number"],
                event["to"]["number"])
    return (event["event"], event["reason"], event["detail"])


def _run(h: EngineHarness, ticks) -> list:
    """Per tick: outcome, compact events, the three switch keys, state keys."""
    rows = []
    for advance, usage in ticks:
        h.clock.advance(advance)
        h.events.clear()
        outcome = h.tick_with_usage(usage)
        st = h.state()
        rows.append((
            outcome.name,
            [_compact(e) for e in _shape(h.events)],
            {k: st.get(k) for k in SWITCH_KEYS},
            sorted(st),
        ))
    return rows


SWITCH_KEYS = ("lastSwitchAt", "lastSwitchFrom", "lastSwitchTo")
STATE_AFTER_SWITCH = [
    "lastSwitchAt", "lastSwitchFrom", "lastSwitchTo", "leftHeadroom",
    "leftRecoveryAt", "leftTrigger", "schemaVersion",
]
NO_SWITCH_STATE = {"lastSwitchAt": None, "lastSwitchFrom": None, "lastSwitchTo": None}
T0 = 1_000_000.0


AT_LIMIT_TICKS = [
    (0, {"1": _usage(100), "2": _usage(30), "3": _usage(50)}),
    (60, {"2": _usage(40), "1": _usage(100), "3": _usage(50)}),
    (60, {"2": _usage(100), "1": _usage(20), "3": _usage(50)}),
]
PROACTIVE_TICKS = [
    (0, {"1": _usage(50), "2": _usage(0), "3": _usage(0)}),
    (60, {"1": _usage(95), "2": _usage(20), "3": _usage(60)}),
    (100, {"2": _usage(95), "1": _usage(10), "3": _usage(60)}),
    (400, {"2": _usage(95), "1": _usage(10), "3": _usage(60)}),
]
FAILOVER_TICKS = [
    (0, {"1": None, "2": _usage(30), "3": _usage(20)}),
    (60, {"1": None, "2": _usage(30), "3": _usage(20)}),
    (60, {"1": None, "2": _usage(30), "3": _usage(20)}),
]
CONSUME_FIRST_TICKS = [
    (0, {"1": _usage7(20, 20, R_LATER), "2": _usage7(10, 10, R_SOON),
         "3": _usage7(10, 10, R_LATEST)}),
    (60, {"2": _usage7(20, 20, R_LATER), "1": _usage7(10, 10, R_LATEST),
          "3": _usage7(10, 10, R_SOON)}),
    (400, {"2": _usage7(20, 20, R_LATER), "1": _usage7(10, 10, R_LATEST),
           "3": _usage7(10, 10, R_SOON)}),
]


AT_LIMIT_EXPECTED = [
    ("SWITCHED", [("poll", 1), ("switch", "at-limit", 1, 2)],
     {"lastSwitchAt": T0, "lastSwitchFrom": 1, "lastSwitchTo": "2"},
     STATE_AFTER_SWITCH),
    ("NO_ACTION", [("poll", 2), ("no-switch", "below-threshold", "5h 40% < 90%")],
     {"lastSwitchAt": T0, "lastSwitchFrom": 1, "lastSwitchTo": "2"},
     STATE_AFTER_SWITCH),
    # Changed on purpose by E3b (engine reliability, F2): account 1 was left
    # at its limit 120 s ago and its window has not reset (no leftRecoveryAt),
    # so the forced no-return bar sends the engine to account 3, not back to 1.
    ("SWITCHED", [("poll", 2), ("switch", "at-limit", 2, 3)],
     {"lastSwitchAt": T0 + 120, "lastSwitchFrom": 2, "lastSwitchTo": "3"},
     STATE_AFTER_SWITCH),
]
PROACTIVE_EXPECTED = [
    ("NO_ACTION", [("poll", 1), ("no-switch", "below-threshold", "5h 50% < 90%")],
     NO_SWITCH_STATE, []),
    ("SWITCHED", [("poll", 1), ("switch", "proactive", 1, 2)],
     {"lastSwitchAt": T0 + 60, "lastSwitchFrom": 1, "lastSwitchTo": "2"},
     STATE_AFTER_SWITCH),
    ("NO_ACTION", [("poll", 2), ("no-switch", "cooldown", "")],
     {"lastSwitchAt": T0 + 60, "lastSwitchFrom": 1, "lastSwitchTo": "2"},
     STATE_AFTER_SWITCH),
    ("SWITCHED", [("poll", 2), ("switch", "proactive", 2, 1)],
     {"lastSwitchAt": T0 + 560, "lastSwitchFrom": 2, "lastSwitchTo": "1"},
     STATE_AFTER_SWITCH),
]
# U1 (merge of upstream v1.2.0b1): upstream #477 persists the unreadable-active
# counters, so an unreadable tick now writes these state keys. The switch
# clears them (F1 resets the counter, and the persist step then drops the keys).
HEALTH_STATE = [
    "healthAccount", "healthTickAt", "idleHoldSince", "schemaVersion",
    "unhealthyTicks",
]
FAILOVER_EXPECTED = [
    ("NO_ACTION",
     [("poll", 1), ("no-switch", "active-usage-unknown", "1/3 before failover")],
     NO_SWITCH_STATE, HEALTH_STATE),
    ("NO_ACTION",
     [("poll", 1), ("no-switch", "active-usage-unknown", "2/3 before failover")],
     NO_SWITCH_STATE, HEALTH_STATE),
    ("SWITCHED", [("poll", 1), ("switch", "failover", 1, 3)],
     {"lastSwitchAt": T0 + 120, "lastSwitchFrom": 1, "lastSwitchTo": "3"},
     STATE_AFTER_SWITCH),
]
CONSUME_FIRST_EXPECTED = [
    ("SWITCHED", [("poll", 1), ("switch", "consume-first", 1, 2)],
     {"lastSwitchAt": T0, "lastSwitchFrom": 1, "lastSwitchTo": "2"},
     STATE_AFTER_SWITCH),
    ("NO_ACTION", [("poll", 2), ("no-switch", "cooldown", "")],
     {"lastSwitchAt": T0, "lastSwitchFrom": 1, "lastSwitchTo": "2"},
     STATE_AFTER_SWITCH),
    ("SWITCHED", [("poll", 2), ("switch", "consume-first", 2, 3)],
     {"lastSwitchAt": T0 + 460, "lastSwitchFrom": 2, "lastSwitchTo": "3"},
     STATE_AFTER_SWITCH),
]


# Fetch calls of the usage collector, recorded on the old code: a
# below-threshold `best` tick reads the store once and fetches one due
# candidate; the consume-first two-phase commit adds ONE refetch of all.
BEST_BELOW_FETCHES = [{"fetch": set()}, {"fetch": {"2"}, "scheduled": True}]
CONSUME_FIRST_FETCHES = [
    {"fetch": set()}, {"fetch": {"2"}, "scheduled": True}, {"fetch": {"1", "2", "3"}},
]


class TestOldPathsUnchanged:
    """Characterization: recorded on the code before S3b-2 (EXE2: these pass
    before and after the change)."""

    @pytest.mark.parametrize(
        "strategy, ticks, expected",
        [
            ("best", AT_LIMIT_TICKS, AT_LIMIT_EXPECTED),
            ("best", PROACTIVE_TICKS, PROACTIVE_EXPECTED),
            ("best", FAILOVER_TICKS, FAILOVER_EXPECTED),
            ("consume-first", CONSUME_FIRST_TICKS, CONSUME_FIRST_EXPECTED),
        ],
        ids=["at-limit", "proactive", "failover", "consume-first"],
    )
    def test_fixed_fleet_sequence(self, temp_home, strategy, ticks, expected):
        h = _fleet(temp_home, strategy)
        assert _run(h, ticks) == expected
        assert "lastBalancedSwitchAt" not in h.state()

    def test_switch_event_full_shape(self, temp_home):
        h = _fleet(temp_home, "best")
        h.tick_with_usage(AT_LIMIT_TICKS[0][1])
        assert _shape(h.events) == [
            {"schemaVersion": 1, "event": "poll",
             "active": {"number": 1, "email": "a@example.com"},
             "headroomPct": {"1": 0.0, "2": 70.0, "3": 50.0},
             "threshold": 90.0,
             "windowsPct": {"1": {"5h": 100.0, "7d": 0.0},
                            "2": {"5h": 30.0, "7d": 0.0},
                            "3": {"5h": 50.0, "7d": 0.0}}},
            {"schemaVersion": 1, "event": "switch", "trigger": "at-limit",
             "from": {"number": 1, "email": "a@example.com"},
             "to": {"number": 2, "email": "b@example.com"},
             "warnings": [], "dryRun": False},
        ]

    @staticmethod
    def _counting(h: EngineHarness, usage: dict) -> MagicMock:
        entries = {n: _entry_for(v, h.clock.now) for n, v in usage.items()}
        spy = MagicMock(return_value=entries)
        with patch.object(h.switcher, "usage_entries_by_account", spy):
            h.engine.tick()
        return spy

    def test_best_below_threshold_fetch_calls(self, temp_home):
        h = _fleet(temp_home, "best")
        spy = self._counting(h, {"1": _usage(20), "2": _usage(10), "3": _usage(0)})
        assert [c.kwargs for c in spy.call_args_list] == BEST_BELOW_FETCHES
        assert h.kinds() == ["poll", "no-switch"]

    def test_consume_first_two_phase_refetch_calls(self, temp_home):
        h = _fleet(temp_home, "consume-first")
        spy = self._counting(h, CONSUME_FIRST_TICKS[0][1])
        assert [c.kwargs for c in spy.call_args_list] == CONSUME_FIRST_FETCHES
        assert h.active_number() == 2


# ---------------------------------------------------------------------------
# The balanced strategy
# ---------------------------------------------------------------------------

PLANS = {1: "pro", 2: "pro", 3: "pro", 4: "pro", 5: "max"}
PLAN_WORD_RE = re.compile(r"\b(pro|max|ultra)\b")


def _email(num: int) -> str:
    return f"canary.balanced{num}@example.com"


def _creds(num: int, plan: str | None) -> str:
    blob: dict = {"accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"}
    if plan is not None:
        blob["subscriptionType"] = plan
    return json.dumps({"claudeAiOauth": blob})


def seed(h: EngineHarness, num: int, *, plan: str | None = None) -> None:
    """``EngineHarness.seed`` plus a stored plan word (``subscriptionType``)."""
    h.seed(num, _email(num))
    h.switcher._write_account_credentials(str(num), _email(num), _creds(num, plan))


def make_live(h: EngineHarness, num: int, *, plan: str | None = None) -> None:
    """``EngineHarness.make_live`` plus the plan word in the live store."""
    h.make_live(_email(num), num)
    (h.temp_home / ".claude" / ".credentials.json").write_text(_creds(num, plan))


def _at(hours: float) -> str:
    """ISO reset time ``hours`` after the harness start time."""
    return _iso_at(T0 + hours * 3600)


def _u(p5: float, p7: float, reset7_h: float | None) -> dict:
    return _usage7(p5, p7, _at(reset7_h) if reset7_h is not None else None)


def _balanced(temp_home: Path, plans: dict[int, str] = PLANS, active: int = 1,
              **kw) -> EngineHarness:
    kw.setdefault("strategy", "balanced")
    h = EngineHarness(temp_home, **kw)
    for num, plan in plans.items():
        seed(h, num, plan=plan)
    make_live(h, active, plan=plans[active])
    data = h.switcher._get_sequence_data()
    data["activeAccountNumber"] = active
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


# Active Pro 1 is below the threshold. Pro 2 has the largest deficit (10% used,
# reset in 20 h), so it beats the active Pro (40% used, reset in 100 h) by
# more than DEFICIT_MARGIN_PTS. (Comment changed in B1: was STICKY_RATIO.)
PRO_BETTER = {
    "1": _u(20, 40, 100), "2": _u(10, 10, 20), "3": _u(10, 50, 150),
    "4": _u(10, 50, 150), "5": _u(0, 0, 100),
}
# Same fleet, but every other Pro is worse than the active one; only the Max
# account is fresher (0% used, 40% of its week gone).
ONLY_MAX_BETTER = {
    "1": _u(20, 40, 100), "2": _u(10, 80, 160), "3": _u(10, 80, 160),
    "4": _u(10, 80, 160), "5": _u(0, 0, 100),
}
# Added in B1: the Max is used as much as the active Pro (40%, same reset), so
# no account beats the active one. f = 1; the deficits of 1 and 5 are equal.
NOTHING_BETTER = dict(ONLY_MAX_BETTER, **{"5": _u(0, 40, 100)})


class TestBalancedProFirst:
    def test_better_pro_gives_a_balanced_switch(self, temp_home):
        h = _balanced(temp_home)
        outcome = h.tick_with_usage(PRO_BETTER)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        [sw] = _switches(h)
        assert sw.trigger == "balanced"
        assert sw.from_ref["number"] == 1 and sw.to_ref["number"] == 2
        st = h.state()
        assert st["lastBalancedSwitchAt"] == T0
        assert st["lastSwitchAt"] == T0
        assert st["lastSwitchFrom"] == 1 and st["lastSwitchTo"] == "2"
        assert st["leftTrigger"] == "balanced"

    def test_only_the_max_underused_moves_to_the_max(self, temp_home):
        # Changed in B1 (was test_only_the_max_better_stays, NO_ACTION under
        # the Pro-first rule): uniform use moves to the unused Max.
        h = _balanced(temp_home)
        outcome = h.tick_with_usage(ONLY_MAX_BETTER)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 5
        assert [s.trigger for s in _switches(h)] == ["balanced"]
        assert h.state()["lastBalancedSwitchAt"] == T0

    def test_no_account_ahead_of_the_active_one_stays(self, temp_home):
        # Changed in B1: the NO_ACTION half of the old test, on a fleet where
        # the Max sits on the same line as the active Pro.
        h = _balanced(temp_home)
        outcome = h.tick_with_usage(NOTHING_BETTER)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _reasons(h) == ["below-threshold"]
        assert h.state() == {}

    def test_balanced_is_not_best(self, temp_home):
        """`best` stays below the threshold on the same fleet."""
        h = _balanced(temp_home, strategy="best")
        assert h.tick_with_usage(PRO_BETTER) is TickOutcome.NO_ACTION
        assert h.active_number() == 1


def _cooldown_fleet(temp_home: Path) -> EngineHarness:
    h = _balanced(temp_home)
    assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
    assert h.active_number() == 2
    h.events.clear()
    return h


# From active Pro 2: Pro 3 is now much more urgent. Account 1 (the account
# just left) is unchanged and worse, so the no-return bar plays no part.
PRO3_BETTER = {
    "2": _u(20, 40, 100), "1": _u(20, 40, 100), "3": _u(10, 10, 20),
    "4": _u(10, 50, 150), "5": _u(0, 0, 100),
}


class TestBalancedCooldown:
    def test_proactive_switch_is_held_inside_the_cooldown(self, temp_home):
        h = _cooldown_fleet(temp_home)
        # Past the engine cooldown (300 s), inside the balanced one (900 s).
        h.clock.advance(400)
        outcome = h.tick_with_usage(PRO3_BETTER)
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["cooldown"]
        assert h.active_number() == 2

    def test_proactive_switch_runs_after_the_cooldown(self, temp_home):
        h = _cooldown_fleet(temp_home)
        h.clock.advance(901)
        outcome = h.tick_with_usage(PRO3_BETTER)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert [s.trigger for s in _switches(h)] == ["balanced"]
        assert h.state()["lastBalancedSwitchAt"] == T0 + 901

    def test_cooldown_uses_the_balanced_setting(self, temp_home):
        h = _balanced(temp_home, balanced_cooldown_seconds=100)
        assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
        h.clock.advance(101)
        assert h.tick_with_usage(PRO3_BETTER) is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_manual_switch_timestamp_holds_a_proactive_switch(self, temp_home):
        """S3c writes `lastManualSwitchAt`; S3b-2 only reads it."""
        h = _balanced(temp_home)
        h.engine._mutate_state(lambda s: s.update(lastManualSwitchAt=T0 - 10))
        outcome = h.tick_with_usage(PRO_BETTER)
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["cooldown"]
        assert "lastBalancedSwitchAt" not in h.state()


class TestBalancedForcedSwitch:
    def test_at_limit_ignores_both_cooldowns(self, temp_home):
        h = _cooldown_fleet(temp_home)
        h.clock.advance(60)
        usage = dict(PRO3_BETTER, **{"2": _u(100, 40, 100)})
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert [s.trigger for s in _switches(h)] == ["at-limit"]
        # A forced switch is not a balanced switch.
        assert h.state()["lastBalancedSwitchAt"] == T0
        assert h.state()["lastSwitchAt"] == T0 + 60

    def test_over_the_line_ignores_the_balanced_cooldown(self, temp_home):
        h = _cooldown_fleet(temp_home)
        h.clock.advance(400)
        usage = dict(PRO3_BETTER, **{"2": _u(95, 40, 100)})
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert [s.trigger for s in _switches(h)] == ["proactive"]
        assert h.state()["lastBalancedSwitchAt"] == T0

    def test_active_below_the_line_is_not_forced_by_the_headroom_gate(
        self, temp_home
    ):
        """Threshold 95: the active 5h window at 92 is below the user's line.
        The 10-point headroom gate of balance is for candidates only, so the
        active account stays (week of 2026-11-16: with a 99% line it was
        forced out at 90-92%)."""
        h = _balanced(temp_home, threshold=95.0)
        assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
        h.events.clear()
        h.clock.advance(400)
        usage = {
            "2": _u(92, 40, 100), "1": _u(20, 40, 100), "3": _u(10, 50, 150),
            "4": _u(10, 50, 150), "5": _u(0, 0, 100),
        }
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.NO_ACTION
        assert _switches(h) == [] and h.active_number() == 2

    def test_forced_with_nothing_usable_keeps_the_engine_reasons(self, temp_home):
        h = _balanced(temp_home)
        usage = {
            "1": _u(95, 40, 100), "2": _u(95, 10, 20), "3": _u(95, 10, 20),
            "4": _u(95, 10, 20), "5": _u(95, 0, 100),
        }
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.BLOCKED
        assert _reasons(h) == ["no-qualifying-candidate"]
        assert h.active_number() == 1


class TestBalancedLockedRecheck:
    def _loser_tick(self, h: EngineHarness, loser, usage: dict):
        real_read = loser._read_state
        calls: list[bool] = []

        def racing_read() -> dict:
            calls.append(True)
            return {} if len(calls) == 1 else real_read()

        entries = {n: _entry_for(v, h.clock.now) for n, v in usage.items()}
        with patch.object(loser, "_read_state", side_effect=racing_read), \
             patch.object(h.switcher, "usage_entries_by_account",
                          return_value=entries):
            return loser.tick()

    def test_recheck_under_the_lock_reads_last_balanced_switch(self, temp_home):
        """The loser read the state before the winner's switch. 400 s is past
        `cooldownSeconds` (300) and inside `balancedCooldownSeconds` (900):
        only a check of `lastBalancedSwitchAt` against 900 holds it."""
        h = _cooldown_fleet(temp_home)
        loser = h._make_engine()
        h.clock.advance(400)
        outcome = self._loser_tick(h, loser, PRO3_BETTER)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert _reasons(h) == ["cooldown"]

    def test_recheck_ignores_a_recent_forced_switch(self, temp_home):
        """`lastSwitchAt` is 60 s old (an at-limit switch), and no balanced
        switch ran: the recheck under the lock must let it through."""
        h = _balanced(temp_home)
        at_limit = dict(PRO_BETTER, **{"1": _u(100, 40, 100)})
        assert h.tick_with_usage(at_limit) is TickOutcome.SWITCHED
        assert [s.trigger for s in _switches(h)] == ["at-limit"]
        assert h.active_number() == 2
        assert "lastBalancedSwitchAt" not in h.state()
        h.events.clear()
        h.clock.advance(60)
        outcome = h.tick_with_usage(PRO3_BETTER)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert [s.trigger for s in _switches(h)] == ["balanced"]


# Account 1 is left at T0 with 60 points (7d 40%, reset in 30 h). Account 2
# then burns its week and its 5h window; balance would now move back to 1.
# Changed in B1 (fleet numbers only): the Max was at 0% weekly, which the
# scaled line picks first; at 40% it sits on its line and plays no part.
NO_RETURN_FIRST = {
    "1": _u(20, 40, 30), "2": _u(10, 10, 20), "3": _u(10, 50, 150),
    "4": _u(10, 50, 150), "5": _u(0, 40, 100),
}
NO_RETURN_LATER = {
    "2": _u(70, 60, 20), "1": _u(20, 40, 30), "3": _u(10, 50, 150),
    "4": _u(10, 50, 150), "5": _u(0, 40, 100),
}
# Account 1 recovered by 4 points (>= SPENT_HEADROOM_PCT), but the active
# still has 40 points, so the ratio leg of the bar keeps it barred: only the
# `_rank`-style release (Stay, then decide again unbarred) frees it.
NO_RETURN_RECOVERED = {
    "2": _u(60, 60, 20), "1": _u(20, 36, 30), "3": _u(10, 50, 150),
    "4": _u(10, 50, 150), "5": _u(0, 40, 100),
}


class TestBalancedNoReturnBar:
    def _left_one(self, temp_home: Path) -> EngineHarness:
        h = _balanced(temp_home)
        assert h.tick_with_usage(NO_RETURN_FIRST) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.events.clear()
        h.clock.advance(1000)
        return h

    def test_bar_stops_the_return_to_the_account_just_left(self, temp_home):
        h = self._left_one(temp_home)
        outcome = h.tick_with_usage(NO_RETURN_LATER)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert _reasons(h) == ["below-threshold"]

    def test_bar_releases_like_rank_when_the_account_recovered(self, temp_home):
        h = self._left_one(temp_home)
        outcome = h.tick_with_usage(NO_RETURN_RECOVERED)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 1
        assert [s.trigger for s in _switches(h)] == ["balanced"]

    def test_bar_does_not_apply_after_a_manual_switch(self, temp_home):
        """`lastSwitchTo` no longer matches the active account: no bar."""
        h = self._left_one(temp_home)
        h.engine._mutate_state(lambda s: s.update(lastSwitchTo="9"))
        outcome = h.tick_with_usage(NO_RETURN_LATER)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 1


class TestBalancedMeasurement:
    def test_unmeasured_active_gives_no_switch(self, temp_home):
        h = _balanced(temp_home)
        usage = dict(PRO_BETTER, **{"1": {"five_hour": {"pct": 20.0}}})
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _reasons(h) == ["below-threshold"]

    def test_measured_control_switches(self, temp_home):
        h = _balanced(temp_home)
        assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED

    def test_quarantined_candidate_is_never_a_target(self, temp_home):
        h = _balanced(temp_home)
        h.engine._mutate_state(lambda s: s.setdefault("quarantine", {}).update(
            {"2": {"email": _email(2), "reason": "invalid_grant", "at": "x",
                   "refreshTokenFingerprint": oauth_fingerprint(h, 2)}}
        ))
        # Changed in B1 (fleet numbers only): the Max at 40% weekly sits on
        # its line, so the quarantined Pro 2 is the only better account.
        usage = dict(PRO_BETTER, **{"5": _u(0, 40, 100)})
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1


def oauth_fingerprint(h: EngineHarness, num: int):
    from claude_swap import oauth

    creds = h.switcher.read_account_credentials(str(num), _email(num))
    return oauth.credential_fingerprint(creds) if creds else None


class TestBalancedUltra:
    def test_ultra_candidate_is_not_blocked_by_the_5h_line(self):
        usage = {"1": _u(95, 40, 100), "2": _u(95, 10, 20), "3": _u(95, 10, 20)}
        plans = {"1": "pro", "2": "ultra", "3": "pro"}
        accounts = _balance_accounts(
            "1", ["2", "3"], usage, {"1": 90.0, "2": 90.0, "3": 90.0}, None, (),
            plans.get,
        )
        blocked = {a.id: a.blocked for a in accounts}
        assert blocked == {1: False, 2: False, 3: True}

    def test_ultra_weekly_line_still_blocks(self):
        usage = {"1": _u(20, 40, 100), "2": _u(10, 95, 20)}
        accounts = _balance_accounts(
            "1", ["2"], usage, {"1": 90.0, "2": 90.0}, None, (),
            {"1": "pro", "2": "ultra"}.get,
        )
        assert accounts[1].blocked is True

    def test_engine_moves_to_an_ultra_with_a_spent_5h_window(self, temp_home):
        h = _balanced(temp_home, plans={1: "pro", 2: "ultra", 3: "pro"})
        usage = {"1": _u(95, 40, 100), "2": _u(95, 10, 20), "3": _u(95, 10, 20)}
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2


# Pro 2 is the most urgent account (reset in 2 h, the loss guard at f = 1)
# but sits at 86% weekly: inside the 5-point margin under the 90% line.
# Changed in B1 (fleet numbers only): the Max at 40% weekly (was 0%) sits on
# its line, so it is no proactive target.
MARGIN_FLEET = {
    "1": _u(20, 40, 100), "2": _u(10, 86, 2), "3": _u(10, 80, 160),
    "4": _u(10, 80, 160), "5": _u(0, 40, 100),
}


class TestBalancedMargin:
    def test_candidate_inside_the_margin_is_not_chosen_proactively(self, temp_home):
        h = _balanced(temp_home)
        assert h.tick_with_usage(MARGIN_FLEET) is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_margin_zero_control(self, temp_home):
        h = _balanced(temp_home, balanced_margin=0.0)
        assert h.tick_with_usage(MARGIN_FLEET) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_forced_switch_falls_back_to_margin_zero(self, temp_home):
        h = _balanced(temp_home)
        usage = {
            "1": _u(95, 40, 100), "2": _u(10, 86, 2), "3": _u(95, 10, 160),
            "4": _u(95, 10, 160), "5": _u(95, 0, 100),
        }
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert [s.trigger for s in _switches(h)] == ["proactive"]


@contextmanager
def _spy(h: EngineHarness, name: str):
    """Record the account argument of ``_PlanCache.<name>`` calls on the
    engine's own cache (the class has ``__slots__``: patch the class)."""
    calls: list[str] = []
    real = getattr(_PlanCache, name)
    cache = h.engine._plan_cache

    def wrapper(self, num):
        if self is cache:
            calls.append(str(num))
        return real(self, num)

    with patch.object(_PlanCache, name, wrapper):
        yield calls


class TestBalancedPlanCache:
    def test_plan_of_runs_at_most_once_per_account_per_tick(self, temp_home):
        h = _balanced(temp_home)
        with _spy(h, "plan_of") as calls:
            assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
        # The tick ran `decide` twice (two-phase commit) and read each plan once.
        assert sorted(calls) == ["1", "2", "3", "4", "5"]

    def test_manual_switch_between_ticks_invalidates_both_accounts(
        self, temp_home
    ):
        # Changed in B1 (fleet only): NOTHING_BETTER is the no-switch fleet.
        h = _balanced(temp_home)
        assert h.tick_with_usage(NOTHING_BETTER) is TickOutcome.NO_ACTION
        h.switcher.switch_to("3", json_output=True)  # by hand
        assert h.active_number() == 3
        usage = dict(NOTHING_BETTER, **{"3": _u(20, 40, 100), "1": _u(10, 80, 160)})
        with _spy(h, "invalidate") as calls:
            h.tick_with_usage(usage)
        assert sorted(calls) == ["1", "3"]

    def test_manual_switch_rereads_the_plan(self, temp_home):
        """The live store of a new active account can hold another plan."""
        # Changed in B1 (fleet only): NOTHING_BETTER is the no-switch fleet.
        h = _balanced(temp_home)
        assert h.tick_with_usage(NOTHING_BETTER) is TickOutcome.NO_ACTION
        assert h.engine._plan_cache.plan_of("1") == "pro"
        h.switcher.switch_to("5", json_output=True)
        before = h.engine._plan_cache._entries.get("5")
        assert before is not None and before[1] == "max"
        # Same email, other stored plan: only an invalidate reads it again.
        h.switcher._write_account_credentials("5", _email(5), _creds(5, "pro"))
        (h.temp_home / ".claude" / ".credentials.json").write_text(_creds(5, "pro"))
        h.tick_with_usage(dict(NOTHING_BETTER, **{"5": _u(20, 40, 100)}))
        assert h.engine._plan_cache._entries["5"][1] == "pro"

    def test_engine_switch_needs_no_extra_invalidate(self, temp_home):
        h = _balanced(temp_home)
        assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
        h.clock.advance(60)
        with _spy(h, "invalidate") as calls:
            h.tick_with_usage(PRO3_BETTER)
        assert calls == []


class TestBalancedDryRun:
    def test_dry_run_writes_nothing(self, temp_home):
        h = _balanced(temp_home)
        h.engine = h._make_engine(dry_run=True)
        outcome = h.tick_with_usage(PRO_BETTER)
        assert outcome is TickOutcome.SWITCHED
        [sw] = _switches(h)
        assert sw.dry_run is True and sw.trigger == "balanced"
        assert h.active_number() == 1
        assert not (h.switcher.backup_dir / "autoswitch_state.json").exists()
        assert h.engine._plan_cache._entries == {}


def _scan(payload: str) -> None:
    for num in PLANS:
        assert _email(num) not in payload, payload
    assert not PLAN_WORD_RE.search(payload), payload
    assert '"unknown"' not in payload, payload
    assert "plan" not in payload.lower(), payload


class TestBalancedNoIdentityInOutput:
    def test_no_email_or_plan_outside_the_account_refs(self, temp_home):
        """P1. The `active`, `from` and `to` refs carry the email today (the
        shape is unchanged); every other part of every event, and the state
        file, has no email and no plan word."""
        h = _balanced(temp_home)
        h.tick_with_usage(PRO_BETTER)
        h.clock.advance(400)
        h.tick_with_usage(PRO3_BETTER)
        h.clock.advance(60)
        h.tick_with_usage(dict(PRO3_BETTER, **{"2": _u(100, 40, 100)}))
        assert {"switch", "no-switch", "poll"} <= set(h.kinds())
        for event in h.events:
            data = event.to_json()
            for key in ("active", "from", "to"):
                ref = data.pop(key, None)
                if ref is not None:
                    assert set(ref) == {"number", "email"}
            _scan(json.dumps(data))
        _scan((h.switcher.backup_dir / "autoswitch_state.json").read_text())


class TestAutoJsonCommand:
    def test_auto_once_json_emits_the_balanced_trigger(self, temp_home, capsys):
        from claude_swap import cli

        h = _balanced(temp_home)
        now = time.time()
        later = lambda hours: _iso_at(now + hours * 3600)  # noqa: E731
        usage = {
            "1": _usage7(20, 40, later(100)), "2": _usage7(10, 10, later(20)),
            "3": _usage7(10, 50, later(150)), "4": _usage7(10, 50, later(150)),
            "5": _usage7(0, 0, later(100)),
        }
        entries = {n: _entry_for(v, now) for n, v in usage.items()}
        h.switcher._usage_store.clock = time.time
        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=h.switcher), \
             patch.object(h.switcher, "usage_entries_by_account",
                          return_value=entries), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "auto", "--once", "--json",
                                        "--strategy", "balanced"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == TickOutcome.SWITCHED.value
        lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()
                 if ln.strip()]
        [switch] = [ln for ln in lines if ln["event"] == "switch"]
        assert switch["trigger"] == "balanced"
        assert set(switch) == {"schemaVersion", "event", "ts", "trigger", "from",
                               "to", "warnings", "dryRun"}
        assert switch["schemaVersion"] == 1
        assert switch["from"] == {"number": 1, "email": _email(1)}
        assert switch["to"] == {"number": 2, "email": _email(2)}
        assert switch["warnings"] == [] and switch["dryRun"] is False
        assert [ln["event"] for ln in lines] == ["poll", "switch"]


class TestBalancedTwoPhase:
    @staticmethod
    def _phased(h: EngineHarness, first: dict, second: dict):
        """The collector serves ``first`` and the refetch serves ``second``."""
        calls: list[dict] = []

        def fetch(**kw):
            calls.append(kw)
            return second if len(calls) > 2 else first

        with patch.object(h.switcher, "usage_entries_by_account", side_effect=fetch):
            outcome = h.engine.tick()
        return outcome, calls

    def _entries(self, h: EngineHarness, usage: dict) -> dict:
        return {n: _entry_for(v, h.clock.now) for n, v in usage.items()}

    def test_balanced_switch_refetches_all_and_decides_again(self, temp_home):
        # Changed in B1 (fleet only): the refetch shows the no-switch fleet.
        h = _balanced(temp_home)
        fresh = self._entries(h, NOTHING_BETTER)
        outcome, calls = self._phased(h, self._entries(h, PRO_BETTER), fresh)
        assert calls[-1] == {"fetch": {"1", "2", "3", "4", "5"}}
        assert len(calls) == 3
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert h.active_number() == 1

    def test_stale_target_after_the_refetch_holds(self, temp_home):
        h = _balanced(temp_home)
        fresh = self._entries(h, PRO_BETTER)
        # 200 s old: still trusted for a decision (STALE_OK_S 300) but not
        # fresh (SERVE_TTL_S 180), so only the stale gate can stop it.
        fresh["2"] = UsageEntry(
            last_good=PRO_BETTER["2"], fetched_at=h.clock.now - 200, age_s=200.0
        )
        outcome, _ = self._phased(h, self._entries(h, PRO_BETTER), fresh)
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["stale-usage"]
        assert h.active_number() == 1

    def test_unreadable_active_after_the_refetch_stays(self, temp_home):
        h = _balanced(temp_home)
        fresh = self._entries(h, dict(PRO_BETTER, **{"1": None}))
        outcome, _ = self._phased(h, self._entries(h, PRO_BETTER), fresh)
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert [e.detail for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "7d 40% < 90%"
        ]

    def test_forced_switch_does_not_refetch(self, temp_home):
        """Over the line the collector already escalated: no extra fetch."""
        h = _balanced(temp_home)
        usage = dict(PRO_BETTER, **{"1": _u(100, 40, 100)})
        outcome, calls = self._phased(
            h, self._entries(h, usage), self._entries(h, usage)
        )
        assert outcome is TickOutcome.SWITCHED
        assert [s.trigger for s in _switches(h)] == ["at-limit"]
        assert len(calls) == 3  # store read, baseline fetch, escalation fetch


# ---------------------------------------------------------------------------
# S3b-2c: the Reviewer's REJECTED-SMALL on S3b-2.
# ---------------------------------------------------------------------------

HUGE = 10**400  # an int that no float can hold: float(HUGE) raises


class TestStateTimestamp:
    @pytest.mark.parametrize(
        "value",
        [HUGE, -HUGE, True, False, float("nan"), float("inf"), float("-inf"),
         "x", "1500000000", [1], {"a": 1}, None, -5, -0.5],
        ids=["huge", "neg-huge", "true", "false", "nan", "inf", "neg-inf",
             "str", "numeric-str", "list", "dict", "none", "neg-int",
             "neg-float"],
    )
    def test_invalid_value_gives_none(self, value):
        assert _state_ts({"k": value}, "k") is None

    def test_missing_key_gives_none(self):
        assert _state_ts({}, "k") is None

    @pytest.mark.parametrize("value, expected", [
        (0, 0.0), (1.5e9, 1.5e9), (1_500_000_000, 1.5e9), (T0, T0),
    ])
    def test_finite_epoch_is_kept(self, value, expected):
        result = _state_ts({"k": value}, "k")
        assert result == expected and type(result) is float


class TestBalancedOverflowTimestamps:
    """A hand-edited or corrupt state file must not stop a switch."""

    def _corrupt(self, temp_home: Path) -> EngineHarness:
        h = _balanced(temp_home)
        h.engine._mutate_state(lambda s: s.update(
            lastManualSwitchAt=HUGE, lastBalancedSwitchAt=HUGE,
        ))
        return h

    def test_at_limit_switches(self, temp_home):
        h = self._corrupt(temp_home)
        outcome = h.tick_with_usage(dict(PRO_BETTER, **{"1": _u(100, 40, 100)}))
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert [s.trigger for s in _switches(h)] == ["at-limit"]

    def test_proactive_balanced_switches(self, temp_home):
        h = self._corrupt(temp_home)
        outcome = h.tick_with_usage(PRO_BETTER)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert [s.trigger for s in _switches(h)] == ["balanced"]


@contextmanager
def _failing_freshen(h: EngineHarness, ok: frozenset[str] = frozenset()):
    """Record each ``_freshen_target`` account; every account not in ``ok``
    fails with ``transient``."""
    attempts: list[str] = []

    def freshen(num, email):
        attempts.append(str(num))
        return "ok" if str(num) in ok else "transient"

    with patch.object(h.engine, "_freshen_target", side_effect=freshen):
        yield attempts


class TestBalancedOrdered:
    def test_proactive_failed_freshen_tries_only_the_target(self, temp_home):
        """PRO_BETTER: Pro 3 and Pro 4 and the Max 5 are all usable, so a
        longer `ordered` would try them."""
        h = _balanced(temp_home)
        with _failing_freshen(h) as attempts:
            outcome = h.tick_with_usage(PRO_BETTER)
        assert attempts == ["2"]
        assert outcome is TickOutcome.ERROR
        assert h.active_number() == 1
        assert _switches(h) == []

    def test_forced_margin_zero_fallback_builds_the_rest_at_margin_zero(
        self, temp_home
    ):
        """Margin 10: every candidate (85% weekly, line 90) is inside the
        margin. The active is over its 5h line: the forced switch takes the
        best one, and the freshen loop then tries the others."""
        h = _balanced(temp_home, plans={1: "pro", 2: "pro", 3: "pro", 4: "pro"},
                      balanced_margin=10.0)
        usage = {
            "1": _u(95, 40, 100), "2": _u(10, 85, 2), "3": _u(10, 85, 50),
            "4": _u(10, 85, 100),
        }
        with _failing_freshen(h) as attempts:
            outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.ERROR
        assert attempts[0] == "2"
        assert sorted(attempts[1:]) == ["3", "4"]

    def test_forced_margin_zero_fallback_switches_to_the_best(self, temp_home):
        h = _balanced(temp_home, plans={1: "pro", 2: "pro", 3: "pro", 4: "pro"},
                      balanced_margin=10.0)
        usage = {
            "1": _u(95, 40, 100), "2": _u(10, 85, 2), "3": _u(10, 85, 50),
            "4": _u(10, 85, 100),
        }
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert [s.trigger for s in _switches(h)] == ["proactive"]

    def test_active_over_its_own_line_is_not_in_ordered(self, temp_home):
        """Per-account line 50, active at 60: over its line, but under the
        global 90 that `balance.rank` measures with."""
        h = _balanced(temp_home)
        h.switcher.set_account_threshold("1", 50.0)
        usage = dict(PRO_BETTER, **{"1": _u(60, 40, 100)})
        with _failing_freshen(h) as attempts:
            outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.ERROR
        assert attempts and attempts[0] == "2"
        assert "1" not in attempts
        assert len(attempts) == len(set(attempts))


class TestBalancedFailover:
    def test_unreadable_active_fails_over_on_the_third_tick(self, temp_home):
        h = _balanced(temp_home)
        usage = dict(PRO_BETTER, **{"1": None})
        outcomes = []
        for i in range(3):
            if i:
                h.clock.advance(60)
            outcomes.append(h.tick_with_usage(usage))
        assert outcomes == [
            TickOutcome.NO_ACTION, TickOutcome.NO_ACTION, TickOutcome.SWITCHED,
        ]
        assert _reasons(h) == ["active-usage-unknown", "active-usage-unknown"]
        assert [s.trigger for s in _switches(h)] == ["failover"]
        assert h.active_number() != 1
        assert "lastBalancedSwitchAt" not in h.state()


class TestBalancedNoOauthPeer:
    def test_only_excluded_api_key_peers_is_below_threshold(self, temp_home):
        h = _balanced(temp_home, plans={1: "pro", 2: "pro"})
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        outcome = h.tick_with_usage({"1": _u(20, 40, 100), "2": "api key"})
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert h.active_number() == 1


class TestBalancedRank:
    """``engine.balanced_rank``: what the pill shows as gold, silver, bronze."""

    def test_rank_lists_usable_accounts_best_first(self, temp_home):
        h = _fleet(temp_home, "balanced")
        h.tick_with_usage({"1": _usage7(10, 50, R_LATER),
                           "2": _usage7(10, 10, R_LATER),
                           "3": _usage7(10, 30, R_LATER)})
        # Equal plans, equal reset: the least weekly use leads.
        assert h.engine.balanced_rank == (2, 3, 1)

    def test_an_account_at_its_limit_has_no_rank(self, temp_home):
        h = _fleet(temp_home, "balanced")
        h.tick_with_usage({"1": _usage7(10, 50, R_LATER),
                           "2": _usage7(100, 10, R_LATER),
                           "3": _usage7(10, 30, R_LATER)})
        assert h.engine.balanced_rank == (3, 1)

    def test_other_strategies_keep_no_rank(self, temp_home):
        h = _fleet(temp_home, "best")
        h.tick_with_usage({"1": _usage7(10, 50, R_LATER),
                           "2": _usage7(10, 10, R_LATER),
                           "3": _usage7(10, 30, R_LATER)})
        assert h.engine.balanced_rank == ()


@pytest.fixture
def utc_zone(monkeypatch):
    """The machine zone is UTC for the test (the engine reads the local
    zone for work days)."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    monkeypatch.setenv("TZ", "UTC0")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class TestBalancedWorkDays:
    """``autoswitch.workDays`` reaches ``balance.decide`` and every
    ``balance.rank`` of the tick (WD2, WD4)."""

    # The fake clock starts on Monday 1970-01-12 13:46:40 UTC. Account 2
    # resets at 21:46 the same day with 2 points left under its line: a
    # small need (2.3 h), so with all days its 8 hours are no guard. With
    # Monday a rest day it has no work time left: a guard account.
    USAGE = {
        "1": _usage7(10, 40, _iso_at(T0 + 100 * 3600)),
        "2": _usage7(10, 88, _iso_at(T0 + 8 * 3600)),
        "3": _usage7(10, 20, _iso_at(T0 + 120 * 3600)),
    }

    def _tick(self, temp_home, **kw):
        h = _fleet(temp_home, "balanced", balanced_margin=0.0, **kw)
        h.tick_with_usage(self.USAGE)
        return h

    def test_auto_work_days_flag(self, temp_home, utc_zone):
        h = self._tick(temp_home, work_days="tue-sun")
        assert h.engine.balanced_rank[0] == 2
        assert [s.trigger for s in _switches(h)] == ["balanced"]
        assert h.active_number() == 2

    def test_all_days_keeps_the_wall_clock(self, temp_home, utc_zone):
        h = self._tick(temp_home)
        assert h.engine.balanced_rank[0] != 2
        assert h.active_number() != 2

    def test_a_bad_work_days_text_counts_all_days(self, temp_home, utc_zone):
        assert self._tick(temp_home, work_days="fri-mon").engine.balanced_rank == (
            self._tick(temp_home).engine.balanced_rank)

    def test_a_forced_switch_ranks_on_work_days(self, temp_home, utc_zone):
        # Account 1 is at its 5-hour line: a forced switch, also to account 2.
        h = _fleet(temp_home, "balanced", balanced_margin=0.0, work_days="tue-sun")
        h.tick_with_usage(dict(self.USAGE, **{"1": _usage7(95, 40, _iso_at(T0 + 100 * 3600))}))
        assert h.active_number() == 2
        # A forced switch below 100%: the engine's "proactive" trigger.
        assert [s.trigger for s in _switches(h)] == ["proactive"]
