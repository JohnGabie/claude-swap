"""E0: reproduction tests for the auto-switch loops found in the real log.

Spec: docs/superpowers/specs/2026-11-15-engine-reliability-design.md,
"Evidence" and scenarios S1 to S6. Tests only: the fixes are tasks E1 to E5.

Each scenario runs the engine for 15 simulated minutes in 60 second ticks,
with strategy ``best``, threshold 98, cooldown 300 s, hysteresis 10 and
``unhealthy_ticks`` 3. A usage row is (five_hour used %, seven_day used %).
``None`` means the usage read returns nothing for that account in that tick.
The usage a tick sees can depend on the role of the account: the row of the
ACTIVE account differs from the row it has as a CANDIDATE.

A scenario that fails on the current code is ``xfail(strict=True)``: when a
later fix makes the scenario pass, the strict marker fails the run and forces
the removal of the marker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.test_autoswitch import EngineHarness, _usage7

LINE = 98.0
MINUTES = 15
TICK_SECONDS = 60
FIX_REASON = "E1-E5 fix this"

Row = tuple[float, float] | None


@dataclass
class Switch:
    minute: int
    src: int
    dst: int
    trigger: str


@dataclass
class Result:
    switches: list[Switch] = field(default_factory=list)
    per_tick: list[int] = field(default_factory=list)
    final_active: int | None = None

    def sequence(self) -> str:
        return "; ".join(
            f"(min {s.minute}, {s.src}->{s.dst}, {s.trigger})" for s in self.switches
        ) or "(no switch)"


def _row_to_usage(row: Row) -> dict | None:
    return None if row is None else _usage7(row[0], row[1])


def _build(temp_home: Path, start: int) -> EngineHarness:
    h = EngineHarness(
        temp_home,
        strategy="best",
        threshold=98.0,
        weekly_threshold=None,
        interval_seconds=60.0,
        cooldown_seconds=300.0,
        hysteresis_pct=10.0,
        unhealthy_ticks=3,
    )
    for num in range(1, 7):
        h.seed(num, f"acc{num}@example.com")
    h.make_live(f"acc{start}@example.com", start)
    data = h.switcher._get_sequence_data()
    data["activeAccountNumber"] = start
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _simulate(
    temp_home: Path,
    start: int,
    as_candidate: dict[int, Row],
    as_active: dict[int, Row] | None = None,
) -> Result:
    """Run 15 minutes. ``as_active`` overrides ``as_candidate`` for the active account."""
    as_active = as_active or {}
    h = _build(temp_home, start)
    result = Result()
    for minute in range(MINUTES + 1):
        if minute:
            h.clock.advance(TICK_SECONDS)
        active = h.active_number()
        usage = {}
        for num in range(1, 7):
            row = (
                as_active[num]
                if num == active and num in as_active
                else as_candidate[num]
            )
            usage[str(num)] = _row_to_usage(row)
        h.events.clear()
        h.tick_with_usage(usage)
        n = 0
        for e in h.events:
            if e.kind != "switch":
                continue
            d = e.to_json()
            result.switches.append(
                Switch(minute, d["from"]["number"], d["to"]["number"], d["trigger"])
            )
            n += 1
        result.per_tick.append(n)
    result.final_active = h.active_number()
    return result


def _bad_targets(result: Result, truth: dict[int, Row]) -> list[Switch]:
    """Switches to an account at or over the line while another one is under it."""

    def over(row: Row) -> bool:
        return row is not None and max(row) >= LINE

    def under(row: Row) -> bool:
        return row is not None and max(row) < LINE

    bad = []
    for s in result.switches:
        if over(truth.get(s.dst)) and any(
            under(row) for num, row in truth.items() if num != s.dst
        ):
            bad.append(s)
    return bad


# Stored numbers of the real log (percent used).
S1_ROWS: dict[int, Row] = {
    1: (15, 100),
    2: (100, 84),
    3: None,
    4: (100, 80),
    5: (0, 100),
    6: (94, 100),
}


def _with(base: dict[int, Row], **changes: Row) -> dict[int, Row]:
    out = dict(base)
    for key, value in changes.items():
        out[int(key[1:])] = value
    return out


class TestReliabilityScenarios:
    def test_s1_exact_stored_numbers_hold(self, temp_home):
        r = _simulate(temp_home, start=5, as_candidate=S1_ROWS)
        msg = f"S1 sequence: {r.sequence()}"
        assert len(r.switches) <= 1, msg
        assert not _bad_targets(r, S1_ROWS), msg
        assert r.per_tick[-5:] == [0] * 5, f"engine does not hold at the end. {msg}"

    def test_s2_unreadable_active_healthy_candidate_does_not_loop(self, temp_home):
        cand = _with(S1_ROWS, a3=(38, 5), a6=(94, 90))
        active = {3: None, 6: (94, 100)}
        r = _simulate(temp_home, start=3, as_candidate=cand, as_active=active)
        truth = _with(S1_ROWS, a3=(38, 5), a6=(94, 100))
        msg = f"S2 sequence: {r.sequence()}"
        # E3b: the fleet has no other usable account, so two switches are
        # allowed (3->6 failover, 6->3 at-limit by the only-exit release).
        assert len(r.switches) <= 2, msg
        # The failover 3->6 acts on the candidate reading (94, 90), which is
        # all the engine can know; the truth (6 at its limit) is checked for
        # the other switches.
        assert not _bad_targets(
            Result(switches=[x for x in r.switches if x.trigger != "failover"]),
            truth,
        ), msg
        assert not any(s.minute > r.switches[-1].minute for s in r.switches)
        assert r.per_tick[-5:] == [0] * 5, f"engine does not hold at the end. {msg}"

    def test_s3_readable_account_one_switch_then_stays(self, temp_home):
        rows = _with(S1_ROWS, a3=(38, 5))
        r = _simulate(temp_home, start=5, as_candidate=rows)
        msg = f"S3 sequence: {r.sequence()}"
        assert [(s.src, s.dst) for s in r.switches] == [(5, 3)], msg
        assert r.final_active == 3, msg

    def test_s4_stable_account_6_one_failover_then_stays(self, temp_home):
        rows = _with(S1_ROWS, a3=None, a6=(94, 90))
        r = _simulate(temp_home, start=3, as_candidate=rows)
        msg = f"S4 sequence: {r.sequence()}"
        assert [(s.src, s.dst, s.trigger) for s in r.switches] == [
            (3, 6, "failover")
        ], msg
        assert r.final_active == 6, msg

    def test_s5_unreadable_account_at_most_one_move_then_hold(self, temp_home):
        r = _simulate(temp_home, start=3, as_candidate=S1_ROWS)
        msg = f"S5 sequence: {r.sequence()}"
        assert len(r.switches) <= 1, msg
        assert r.per_tick[-5:] == [0] * 5, f"engine does not hold at the end. {msg}"

    def test_s6_two_unreadable_accounts_at_most_one_switch(self, temp_home):
        cand = _with(S1_ROWS, a3=(38, 5), a6=(94, 90))
        active = {3: None, 6: None}
        r = _simulate(temp_home, start=3, as_candidate=cand, as_active=active)
        msg = f"S6 sequence: {r.sequence()}"
        assert len(r.switches) <= 1, msg


class TestCharacterization:
    def test_engine_never_rotates_more_than_once_per_tick(self, temp_home):
        """Pins today's behavior on the worst loop (S6): one move per tick at most."""
        cand = _with(S1_ROWS, a3=(38, 5), a6=(94, 90))
        r = _simulate(
            temp_home, start=3, as_candidate=cand, as_active={3: None, 6: None}
        )
        assert max(r.per_tick) <= 1, f"S6 sequence: {r.sequence()}"


class TestUnreadableCounterResetOnSwitch:
    """E1 (F1): an engine switch starts the new active account at counter 0."""

    S6_CAND = _with(S1_ROWS, a3=(38, 5), a6=(94, 90))
    S6_ACTIVE: dict[int, Row] = {3: None, 6: None}

    def test_a_new_active_account_needs_its_own_unhealthy_ticks(self, temp_home):
        # E3b: the forced bar stops the second S6 switch (it was the loop), so
        # the counter of the new active account is read directly. It was
        # checked through the minute of the second switch (5) before.
        r = _simulate(
            temp_home,
            start=3,
            as_candidate=self.S6_CAND,
            as_active=self.S6_ACTIVE,
        )
        minutes = [s.minute for s in r.switches]
        msg = f"S6 sequence: {r.sequence()}"
        # unhealthy_ticks is 3: ticks at minutes 0, 1, 2 count 1, 2, 3.
        assert minutes[0] == 2, msg
        assert all(b - a >= 3 for a, b in zip(minutes, minutes[1:])), msg

    def test_a2_counter_of_the_new_active_account_starts_at_zero(self, temp_home):
        h = _build(temp_home, start=3)
        counts = []
        for _ in range(5):
            active = h.active_number()
            usage = {
                str(n): _row_to_usage(
                    None if n == active
                    else self.S6_CAND[n]
                )
                for n in range(1, 7)
            }
            h.tick_with_usage(usage)
            counts.append(h.engine._unhealthy_ticks)
            h.clock.advance(TICK_SECONDS)
        # 1, 2, 3 (switch, counter back to 0), then 1, 2 for the new account.
        assert counts == [1, 2, 0, 1, 2], counts

    def test_b_dry_run_failover_leaves_counter_unchanged(self, temp_home):
        h = _build(temp_home, start=3)
        h.engine = h._make_engine(dry_run=True)
        usage = {
            str(n): _row_to_usage(self.S6_CAND[n] if n != 3 else None)
            for n in range(1, 7)
        }
        for _ in range(3):
            h.events.clear()
            h.tick_with_usage(usage)
            h.clock.advance(TICK_SECONDS)
        switches = [e for e in h.events if e.kind == "switch"]
        assert len(switches) == 1
        assert switches[0].to_json()["dryRun"] is True
        assert h.engine._unhealthy_ticks == 3
        assert h.active_number() == 3

    def test_c_readable_tick_after_switch_still_resets_counter(self, temp_home):
        h = _build(temp_home, start=3)
        unreadable = {
            str(n): _row_to_usage(self.S6_CAND[n] if n != 3 else None)
            for n in range(1, 7)
        }
        # Switch 3 -> 6 at the third tick, counter 0 afterwards.
        for _ in range(3):
            h.tick_with_usage(unreadable)
            h.clock.advance(TICK_SECONDS)
        assert h.active_number() == 6
        # Account 6 unreadable for one tick, then readable: counter back to 0.
        unreadable6 = {
            str(n): _row_to_usage(self.S6_CAND[n] if n != 6 else None)
            for n in range(1, 7)
        }
        h.tick_with_usage(unreadable6)
        assert h.engine._unhealthy_ticks >= 1
        h.clock.advance(TICK_SECONDS)
        readable = {str(n): _row_to_usage(self.S6_CAND[n]) for n in range(1, 7)}
        h.tick_with_usage(readable)
        assert h.engine._unhealthy_ticks == 0

    def test_d_s6_switch_rate_is_at_most_one_per_unhealthy_ticks(self, temp_home):
        r = _simulate(
            temp_home,
            start=3,
            as_candidate=self.S6_CAND,
            as_active=self.S6_ACTIVE,
        )
        minutes = [s.minute for s in r.switches]
        msg = f"S6 sequence: {r.sequence()}"
        # E3b: at most one switch now, so the spacing is checked only if more
        # than one happens (it was `len(minutes) >= 2`, which needed the loop).
        assert len(minutes) <= 1, msg
        assert min((b - a for a, b in zip(minutes, minutes[1:])), default=3) >= 3, msg


# ---------------------------------------------------------------------------
# E2 (F4): an usage endpoint problem is not a dead account.
#
# How the engine classified usage errors BEFORE this change (E1 version of
# src/claude_swap/autoswitch.py, sha256 9850a3a4; line numbers are of that file
# unless another file is named):
#
# 1. An error is recorded, not classified, by the store:
#    - oauth.py:410-436 `_classify_usage_error` gives "http-<code>", "timeout",
#      "network", "bad-response" or the exception type name.
#    - oauth.py:719-724, 765-771 return `UsageOutcome(error=kind)`. An ACTIVE
#      account is never refreshed (oauth.py:719-724: `is_active` skips the 401
#      refresh retry), so its 401 is stored as "http-401".
#    - usage_store.py:1125-1152 stores `lastError`, `consecutiveFailures`,
#      `backoffUntil`; `last429At` only for "http-429". It never touches
#      `lastGood`/`fetchedAt` on a failure.
# 2. Which errors strike the token: only PERMANENT_AUTH_ERRORS
#    ({"invalid_grant", "no_refresh_token"}, usage_store.py:234) add an
#    `authDeadStrikes` strike (usage_store.py:1140-1141). AUTH_DEAD_STRIKES is 1
#    (usage_store.py:227), so one strike quarantines: switcher.py:5243-5244 turns
#    the entry into the sentinel "re-login needed". 403, 429, 5xx, timeout and
#    "http-401" never strike the token.
# 3. What makes `active_headroom` None (autoswitch.py:1434; the value comes from
#    `entry.decision_value()`, usage_store.py:392-408, through
#    `_collect_scheduled_usage` autoswitch.py:2696-2736 and
#    `_headroom_by_account` autoswitch.py:877-886):
#    - a sentinel string (re-login needed, token expired, no credentials, ...);
#    - no `last_good`;
#    - `last_good` older than STALE_OK_S (300 s, usage_store.py:57) AND
#      `trust_extended` false.
#    `trust_extended` (usage_store.py:931-948) is true when the age is inside the
#    ceiling AND (consecutive_failures > 0 OR inside nextPollAt OR a live claim OR
#    held). The ceiling is TRUST_MAX_AGE_S 3600 s for every error except
#    "http-429", whose ceiling is the earliest window reset or
#    RATE_LIMIT_TRUST_MAX_AGE_S 7200 s (usage_store.py:109, 511-548).
# 4. What the engine sees, for a good reading that is 20 minutes old:
#    - 403, 429, 500 and timeout: consecutive_failures > 0 and age < 3600 s, so
#      `trust_extended` is true, `decision_value()` is `last_good`,
#      `active_headroom` is a number, the counter resets (autoswitch.py:1436-1437)
#      and the engine holds. The store already gives F4 for these four.
#    - the same errors at 61 minutes: 403, 500 and timeout give None (ceiling
#      3600 s): counter grows, failover (autoswitch.py:1526-1541). 429 is still
#      TRUSTED until 7200 s (or the earliest reset), which is longer than F4.
#    - "http-401" and "refresh-failed" with a recent `last_good`: ALSO trusted
#      like any failure (consecutive_failures > 0), so the engine holds on a
#      refused credential. This is the gap F4 closes: nothing in the engine reads
#      `last_error`.
#    - quarantine / token-dead: sentinel "re-login needed" wins in
#      `decision_value()`: None, the counter grows, failover. Unchanged.
#    - "token expired" sentinel: idle hold (autoswitch.py:1500-1523). Unchanged.
#    - no `last_good` at all: None, the counter grows, failover. Unchanged.
#    The engine itself reads `last_error` only to show it in the poll event
#    (autoswitch.py:1404-1407).
#
# The tests below build the store entry in two ways: `trust_extended` as the
# store computes it, and `trust_extended` false (the engine must not depend on
# the store flag). Both must hold for the four endpoint errors.
# ---------------------------------------------------------------------------

from claude_swap.json_output import USAGE_RELOGIN_REQUIRED  # noqa: E402
from claude_swap.usage_store import UsageEntry  # noqa: E402
from tests.test_autoswitch import _entry_for  # noqa: E402

TRUST_S = 3600.0
ENDPOINT_ERRORS = [
    pytest.param("http-403", id="403"),
    pytest.param("http-429", id="429"),
    pytest.param("http-500", id="500"),
    pytest.param("timeout", id="timeout"),
]
TRUST_FLAGS = [
    pytest.param("store", id="store-trust"),
    pytest.param(False, id="no-store-trust"),
]


def _failed_entry(
    last_good: dict | None,
    age_s: float,
    error: str,
    trust: object = "store",
) -> UsageEntry:
    """The entry the store hands out after a failed fetch.

    ``trust="store"`` copies the store rule (usage_store.py:931-948 for a row
    with consecutive failures): trusted while the age is inside the ceiling
    (3600 s; 7200 s for 429). ``trust=False`` models an entry the store did
    not extend.
    """
    if trust == "store":
        ceiling = 7200.0 if error == "http-429" else TRUST_S
        extended = last_good is not None and age_s <= ceiling
    else:
        extended = bool(trust)
    return UsageEntry(
        last_good=last_good,
        fetched_at=None if last_good is None else 1_000_000.0 - age_s,
        age_s=None if last_good is None else age_s,
        consecutive_failures=3,
        last_error=error,
        trust_extended=extended,
    )


def _e2_harness(temp_home: Path) -> EngineHarness:
    h = EngineHarness(
        temp_home,
        strategy="best",
        threshold=98.0,
        weekly_threshold=None,
        interval_seconds=60.0,
        cooldown_seconds=300.0,
        hysteresis_pct=10.0,
        unhealthy_ticks=3,
    )
    h.seed(1, "acc1@example.com")
    h.seed(2, "acc2@example.com")
    h.make_live("acc1@example.com", 1)
    data = h.switcher._get_sequence_data()
    data["activeAccountNumber"] = 1
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _run_active_failing(
    temp_home: Path,
    last_good: dict | None,
    age_s: float,
    error: str,
    trust: object = "store",
    ticks: int = 10,
    sentinel: str | None = None,
) -> tuple[EngineHarness, list[int]]:
    """Active account 1 gets no fresh reading for ``ticks`` ticks; account 2
    is healthy and fresh. Returns the harness and the switch count per tick."""
    h = _e2_harness(temp_home)
    per_tick: list[int] = []
    start = h.clock.now
    for i in range(ticks):
        if i:
            h.clock.advance(TICK_SECONDS)
        age = age_s + (h.clock.now - start)
        if sentinel is not None:
            active = UsageEntry(sentinel=sentinel)
        else:
            active = _failed_entry(last_good, age, error, trust)
            if active.fetched_at is not None:
                active = UsageEntry(
                    **{
                        **active.__dict__,
                        "fetched_at": h.clock.now - age,
                    }
                )
        entries = {"1": active, "2": _entry_for(_usage7(10, 10), h.clock.now)}
        h.events.clear()
        h.tick_with_entries(entries)
        per_tick.append(sum(1 for e in h.events if e.kind == "switch"))
        if per_tick[-1]:
            break
    return h, per_tick


GOOD = _usage7(20, 20)


class TestEndpointTroubleIsNotADeadAccount:
    """E2 (F4): decision F4 and scenario S2 of the engine reliability spec."""

    @pytest.mark.parametrize("trust", TRUST_FLAGS)
    @pytest.mark.parametrize("error", ENDPOINT_ERRORS)
    def test_recent_good_reading_below_line_holds_for_ten_ticks(
        self, temp_home, error, trust
    ):
        h, per_tick = _run_active_failing(
            temp_home, GOOD, 20 * 60.0, error, trust
        )
        assert per_tick == [0] * 10
        assert h.active_number() == 1
        assert h.engine._unhealthy_ticks == 0

    @pytest.mark.parametrize("trust", TRUST_FLAGS)
    @pytest.mark.parametrize("error", ENDPOINT_ERRORS)
    def test_good_reading_61_minutes_old_fails_over(self, temp_home, error, trust):
        h, per_tick = _run_active_failing(
            temp_home, GOOD, 61 * 60.0, error, trust
        )
        assert per_tick == [0, 0, 1]
        assert h.active_number() == 2

    @pytest.mark.parametrize("trust", TRUST_FLAGS)
    @pytest.mark.parametrize("error", ["http-401", "refresh-failed"])
    def test_refused_credential_fails_over_with_recent_good_reading(
        self, temp_home, error, trust
    ):
        h, per_tick = _run_active_failing(
            temp_home, GOOD, 20 * 60.0, error, trust
        )
        assert per_tick == [0, 0, 1]
        assert h.active_number() == 2

    def test_refused_credential_with_a_very_fresh_reading_still_counts(
        self, temp_home
    ):
        h, per_tick = _run_active_failing(temp_home, GOOD, 30.0, "http-401")
        assert per_tick == [0, 0, 1]

    @pytest.mark.parametrize("error", ["invalid_grant", "no_refresh_token"])
    def test_permanent_auth_error_fails_over(self, temp_home, error):
        h, per_tick = _run_active_failing(temp_home, GOOD, 20 * 60.0, error)
        assert per_tick == [0, 0, 1]

    def test_quarantined_account_fails_over(self, temp_home):
        h, per_tick = _run_active_failing(
            temp_home, None, 0.0, "x", sentinel=USAGE_RELOGIN_REQUIRED
        )
        assert per_tick == [0, 0, 1]

    @pytest.mark.parametrize("error", ENDPOINT_ERRORS)
    def test_no_good_reading_at_all_fails_over(self, temp_home, error):
        h, per_tick = _run_active_failing(temp_home, None, 0.0, error)
        assert per_tick == [0, 0, 1]

    @pytest.mark.parametrize("trust", TRUST_FLAGS)
    @pytest.mark.parametrize("error", ENDPOINT_ERRORS)
    def test_counter_does_not_grow_while_endpoint_is_in_trouble(
        self, temp_home, error, trust
    ):
        h = _e2_harness(temp_home)
        h.engine._unhealthy_ticks = 0
        for _ in range(4):
            h.tick_with_entries({
                "1": _failed_entry(GOOD, 1200.0, error, trust),
                "2": _entry_for(_usage7(10, 10), h.clock.now),
            })
            h.clock.advance(TICK_SECONDS)
            assert h.engine._unhealthy_ticks == 0

    @pytest.mark.parametrize("error", ENDPOINT_ERRORS)
    def test_unreadable_good_reading_at_the_line_still_counts(
        self, temp_home, error
    ):
        """Pins the at-limit side: the store did not extend trust and the last
        reading is over the line, so the old unknown-usage counting applies."""
        h, per_tick = _run_active_failing(
            temp_home, _usage7(100, 20), 20 * 60.0, error, trust=False
        )
        assert per_tick == [0, 0, 1]

    @pytest.mark.parametrize("error", ENDPOINT_ERRORS)
    def test_unreadable_good_reading_over_one_window_line_still_counts(
        self, temp_home, error
    ):
        """Below the line in the 5h window, over it in 7d: not "below its own
        line in every window", so no hold."""
        h, per_tick = _run_active_failing(
            temp_home, _usage7(20, 99), 20 * 60.0, error, trust=False
        )
        assert per_tick == [0, 0, 1]

    @pytest.mark.parametrize(
        "reading, trigger",
        [(_usage7(100, 20), "at-limit"), (_usage7(99, 20), "proactive")],
    )
    def test_trusted_reading_at_or_over_line_plus_429_keeps_at_limit_behavior(
        self, temp_home, reading, trigger
    ):
        h = _e2_harness(temp_home)
        h.tick_with_entries({
            "1": _failed_entry(reading, 1200.0, "http-429"),
            "2": _entry_for(_usage7(10, 10), h.clock.now),
        })
        switches = [e.to_json() for e in h.events if e.kind == "switch"]
        assert [s["trigger"] for s in switches] == [trigger]
        assert h.active_number() == 2

    def test_real_store_429_after_61_minutes_fails_over(self, temp_home):
        """With the real store row: after a 429 the store still trusts a
        61 minute old reading (ceiling 7200 s), F4 does not."""
        h = _e2_harness(temp_home)
        store = h.switcher._usage_store
        ident = {"1": ("acc1@example.com", "")}
        from claude_swap.usage_store import FetchRecord

        store.record({"1": FetchRecord(usage=GOOD)}, ident)
        h.clock.advance(61 * 60.0)
        store.record({"1": FetchRecord(error="http-429")}, ident)
        assert store.entries(ident, ())["1"].decision_value() == GOOD
        per_tick = []
        for _ in range(3):
            entry = store.entries(ident, ())["1"]
            h.events.clear()
            h.tick_with_entries({
                "1": entry,
                "2": _entry_for(_usage7(10, 10), h.clock.now),
            })
            per_tick.append(sum(1 for e in h.events if e.kind == "switch"))
            h.clock.advance(TICK_SECONDS)
        assert per_tick == [0, 0, 1]

    def test_real_store_403_after_20_minutes_holds(self, temp_home):
        h = _e2_harness(temp_home)
        store = h.switcher._usage_store
        ident = {"1": ("acc1@example.com", "")}
        from claude_swap.usage_store import FetchRecord

        store.record({"1": FetchRecord(usage=GOOD)}, ident)
        h.clock.advance(20 * 60.0)
        store.record({"1": FetchRecord(error="http-403")}, ident)
        for _ in range(10):
            entry = store.entries(ident, ())["1"]
            h.events.clear()
            h.tick_with_entries({
                "1": entry,
                "2": _entry_for(_usage7(10, 10), h.clock.now),
            })
            assert not [e for e in h.events if e.kind == "switch"]
            h.clock.advance(TICK_SECONDS)
        assert h.engine._unhealthy_ticks == 0


# ---------------------------------------------------------------------------
# E3 (F2): no-return bar for the triggers at-limit and failover.
# ---------------------------------------------------------------------------

BAR_S = 900.0  # FORCED_NO_RETURN_S, written out so a changed constant shows


def _f2_harness(temp_home: Path, accounts: int = 3) -> EngineHarness:
    h = EngineHarness(
        temp_home,
        strategy="best",
        threshold=98.0,
        weekly_threshold=None,
        interval_seconds=60.0,
        cooldown_seconds=300.0,
        hysteresis_pct=10.0,
        unhealthy_ticks=3,
    )
    for num in range(1, accounts + 1):
        h.seed(num, f"acc{num}@example.com")
    h.make_live("acc1@example.com", 1)
    data = h.switcher._get_sequence_data()
    data["activeAccountNumber"] = 1
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _fresh(h: EngineHarness, used5: float, used7: float) -> UsageEntry:
    return _entry_for(_usage7(used5, used7), h.clock.now)


def _stale_optimistic(
    h: EngineHarness,
    used5: float = 5,
    used7: float = 5,
    age: float = 1200.0,
    error: str | None = "http-403",
    trust: bool = True,
) -> UsageEntry:
    """A candidate row that looks healthy but is old or came with an error."""
    return UsageEntry(
        last_good=_usage7(used5, used7),
        fetched_at=h.clock.now - age,
        age_s=age,
        consecutive_failures=3 if error else 0,
        last_error=error,
        trust_extended=trust,
    )


def _tick(h: EngineHarness, **rows: UsageEntry) -> list[dict]:
    """One tick with entries ``a1=...``; returns the switch events as JSON."""
    h.events.clear()
    h.tick_with_entries({k[1:]: v for k, v in rows.items()})
    return [e.to_json() for e in h.events if e.kind == "switch"]


def _fail_over_1_to_2(h: EngineHarness, others: dict[str, UsageEntry]) -> None:
    """Active 1 is unreadable for 3 ticks: failover to 2 (the best account)."""
    for _ in range(3):
        switches = _tick(h, a1=UsageEntry(), a2=_fresh(h, 10, 10), **others)
        h.clock.advance(TICK_SECONDS)
    assert h.active_number() == 2
    assert switches and switches[0]["trigger"] == "failover"


def _last_no_switch(h: EngineHarness) -> dict | None:
    found = [e.to_json() for e in h.events if e.kind == "no-switch"]
    return found[-1] if found else None


class TestForcedNoReturnBar:
    def test_at_limit_after_failover_goes_to_c_not_back_to_a(self, temp_home):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 50, 50)})
        # A reads healthy but only as an old row with a 403: not recovered.
        switches = _tick(
            h,
            a1=_stale_optimistic(h),
            a2=_fresh(h, 100, 100),
            a3=_fresh(h, 50, 50),
        )
        assert [(s["from"]["number"], s["to"]["number"], s["trigger"]) for s in switches] == [
            (2, 3, "at-limit")
        ]

    def test_failover_after_failover_holds_when_no_other_account_is_usable(
        self, temp_home
    ):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 100, 100)})
        # B is unreadable for an endpoint reason; its good reading is old.
        b = _stale_optimistic(h, 20, 20, age=4000.0, error="http-403", trust=False)
        for _ in range(4):
            switches = _tick(
                h, a1=_stale_optimistic(h), a2=b, a3=_fresh(h, 100, 100)
            )
            assert switches == []
            h.clock.advance(TICK_SECONDS)
        assert h.active_number() == 2
        assert _last_no_switch(h)["reason"] == "no-return"

    def test_dead_active_account_keeps_the_exit_to_a_open(self, temp_home):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 100, 100)})
        dead = UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED)
        for _ in range(3):
            switches = _tick(
                h, a1=_stale_optimistic(h), a2=dead, a3=_fresh(h, 100, 100)
            )
            h.clock.advance(TICK_SECONDS)
        assert [(s["to"]["number"], s["trigger"]) for s in switches] == [
            (1, "failover")
        ]

    def test_a_failover_exit_is_not_released_by_a_fresh_reading(self, temp_home):
        # E3b: was `test_recovered_a_is_released_inside_the_bar`, which
        # encoded the old release (fresh healthy reading lifts the bar).
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 50, 50)})
        switches = _tick(
            h, a1=_fresh(h, 5, 5), a2=_fresh(h, 100, 100), a3=_fresh(h, 50, 50)
        )
        assert [(s["to"]["number"], s["trigger"]) for s in switches] == [
            (3, "at-limit")
        ]

    @pytest.mark.parametrize(
        "age, error",
        [(0.0, "http-403"), (0.0, "http-401"), (301.0, None), (1200.0, "timeout")],
        ids=["fresh-403", "fresh-401", "old-no-error", "old-timeout"],
    )
    def test_a_stale_optimistic_row_does_not_count_as_recovered(
        self, temp_home, age, error
    ):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 50, 50)})
        switches = _tick(
            h,
            a1=_stale_optimistic(h, age=age, error=error),
            a2=_fresh(h, 100, 100),
            a3=_fresh(h, 50, 50),
        )
        assert [s["to"]["number"] for s in switches] == [3]

    def test_a_recovered_reading_over_its_own_line_is_not_recovered(
        self, temp_home
    ):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 50, 50)})
        switches = _tick(
            h, a1=_fresh(h, 99, 20), a2=_fresh(h, 100, 100), a3=_fresh(h, 50, 50)
        )
        assert [s["to"]["number"] for s in switches] == [3]

    def test_the_bar_still_holds_one_second_before_fifteen_minutes(self, temp_home):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 50, 50)})
        at = h.state()["lastSwitchAt"]
        h.clock.now = at + BAR_S - 1
        inside = _tick(
            h, a1=_stale_optimistic(h), a2=_fresh(h, 100, 100), a3=_fresh(h, 50, 50)
        )
        assert [s["to"]["number"] for s in inside] == [3]

    def test_a_bar_that_has_expired_lets_the_best_account_win(self, temp_home):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 50, 50)})
        at = h.state()["lastSwitchAt"]
        h.clock.now = at + BAR_S
        after = _tick(
            h, a1=_stale_optimistic(h), a2=_fresh(h, 100, 100), a3=_fresh(h, 50, 50)
        )
        assert [s["to"]["number"] for s in after] == [1]

    def test_two_account_fleet_keeps_the_exit_open_at_raw_zero_headroom(
        self, temp_home
    ):
        h = _f2_harness(temp_home, accounts=2)
        _fail_over_1_to_2(h, {})
        switches = _tick(h, a1=_stale_optimistic(h), a2=_fresh(h, 100, 100))
        assert [(s["to"]["number"], s["trigger"]) for s in switches] == [
            (1, "at-limit")
        ]

    def test_two_account_fleet_does_not_return_to_an_exhausted_account(
        self, temp_home
    ):
        h = _f2_harness(temp_home, accounts=2)
        _fail_over_1_to_2(h, {})
        switches = _tick(h, a1=_fresh(h, 100, 100), a2=_fresh(h, 100, 100))
        assert switches == []

    def test_a_manual_switch_clears_the_bar(self, temp_home):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 50, 50)})
        result = h.switcher.switch_to("3", json_output=True)
        assert result and result.get("switched")
        h.clock.advance(TICK_SECONDS)
        # Engine state still says 1 -> 2; the active account is 3 now.
        switches = _tick(
            h,
            a1=_stale_optimistic(h),
            a2=_fresh(h, 60, 60),
            a3=_fresh(h, 100, 100),
        )
        assert [s["to"]["number"] for s in switches] == [1]

    def test_at_limit_after_a_proactive_switch_is_barred_too(
        self, temp_home
    ):
        """The bar reads `lastSwitchFrom` whatever the earlier trigger was."""
        h = _f2_harness(temp_home)
        switches = _tick(
            h, a1=_fresh(h, 99, 20), a2=_fresh(h, 10, 10), a3=_fresh(h, 50, 50)
        )
        assert [(s["to"]["number"], s["trigger"]) for s in switches] == [
            (2, "proactive")
        ]
        h.clock.advance(TICK_SECONDS)
        switches = _tick(
            h,
            a1=_stale_optimistic(h),
            a2=_fresh(h, 100, 100),
            a3=_fresh(h, 50, 50),
        )
        assert [s["to"]["number"] for s in switches] == [3]


class TestForcedBarRelease:
    """E3b: a reading alone does not release the forced no-return bar."""

    @staticmethod
    def _set_state(h: EngineHarness, **changes) -> None:
        path = h.switcher.backup_dir / "autoswitch_state.json"
        data = json.loads(path.read_text())
        data.update(changes)
        path.write_text(json.dumps(data))

    def _left_by(self, temp_home, trigger, recovery_at="keep"):
        """Engine switched 1 -> 2; state says it left 1 by ``trigger``."""
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 50, 50)})
        changes = {}
        if trigger != "keep":
            changes["leftTrigger"] = trigger
        if recovery_at != "keep":
            changes["leftRecoveryAt"] = recovery_at
        self._set_state(h, **changes)
        return h

    def _drop_trigger(self, h):
        path = h.switcher.backup_dir / "autoswitch_state.json"
        data = json.loads(path.read_text())
        data.pop("leftTrigger", None)
        path.write_text(json.dumps(data))

    def _healthy_a(self, h):
        return dict(
            a1=_fresh(h, 5, 5), a2=_fresh(h, 100, 100), a3=_fresh(h, 50, 50)
        )

    def test_failover_fresh_healthy_reading_goes_to_the_other_account(
        self, temp_home
    ):
        h = self._left_by(temp_home, "failover")
        switches = _tick(h, **self._healthy_a(h))
        assert [s["to"]["number"] for s in switches] == [3]

    def test_failover_fresh_healthy_reading_holds_when_nothing_else_is_usable(
        self, temp_home
    ):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 100, 100)})
        b = _stale_optimistic(h, 20, 20, age=4000.0, error="http-403", trust=False)
        for _ in range(4):
            switches = _tick(h, a1=_fresh(h, 5, 5), a2=b, a3=_fresh(h, 100, 100))
            assert switches == []
            h.clock.advance(TICK_SECONDS)
        assert h.active_number() == 2
        assert _last_no_switch(h)["reason"] == "no-return"

    def test_failover_exit_still_opens_when_the_active_account_is_dead(
        self, temp_home
    ):
        h = _f2_harness(temp_home)
        _fail_over_1_to_2(h, {"a3": _fresh(h, 100, 100)})
        dead = UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED)
        for _ in range(3):
            switches = _tick(h, a1=_fresh(h, 5, 5), a2=dead, a3=_fresh(h, 100, 100))
            h.clock.advance(TICK_SECONDS)
        assert [(s["to"]["number"], s["trigger"]) for s in switches] == [
            (1, "failover")
        ]

    @pytest.mark.parametrize(
        "trigger", ["at-limit", "proactive", "consume-first", "balanced"]
    )
    def test_future_recovery_time_is_not_recovered(self, temp_home, trigger):
        h = self._left_by(temp_home, trigger)
        self._set_state(h, leftRecoveryAt=h.clock.now + 3600)
        switches = _tick(h, **self._healthy_a(h))
        assert [s["to"]["number"] for s in switches] == [3]

    @pytest.mark.parametrize(
        "trigger", ["at-limit", "proactive", "consume-first", "balanced"]
    )
    def test_past_recovery_time_with_healthy_reading_is_recovered(
        self, temp_home, trigger
    ):
        h = self._left_by(temp_home, trigger)
        self._set_state(h, leftRecoveryAt=h.clock.now - 1)
        switches = _tick(h, **self._healthy_a(h))
        assert [s["to"]["number"] for s in switches] == [1]

    def test_recovery_time_exactly_now_is_recovered(self, temp_home):
        h = self._left_by(temp_home, "at-limit")
        self._set_state(h, leftRecoveryAt=h.clock.now)
        switches = _tick(h, **self._healthy_a(h))
        assert [s["to"]["number"] for s in switches] == [1]

    def test_past_recovery_time_without_a_healthy_reading_is_not_recovered(
        self, temp_home
    ):
        h = self._left_by(temp_home, "at-limit")
        self._set_state(h, leftRecoveryAt=h.clock.now - 1)
        switches = _tick(
            h, a1=_stale_optimistic(h), a2=_fresh(h, 100, 100), a3=_fresh(h, 50, 50)
        )
        assert [s["to"]["number"] for s in switches] == [3]

    @pytest.mark.parametrize(
        "value", [None, float("nan"), "soon", float("inf"), True, "MISSING"],
        ids=["none", "nan", "string", "inf", "bool", "missing"],
    )
    @pytest.mark.parametrize("trigger", ["at-limit", "proactive"])
    def test_invalid_recovery_time_is_not_recovered(
        self, temp_home, trigger, value
    ):
        h = self._left_by(temp_home, trigger)
        path = h.switcher.backup_dir / "autoswitch_state.json"
        data = json.loads(path.read_text())
        if value == "MISSING":
            data.pop("leftRecoveryAt", None)
        else:
            data["leftRecoveryAt"] = value
        path.write_text(json.dumps(data))
        switches = _tick(h, **self._healthy_a(h))
        assert [s["to"]["number"] for s in switches] == [3]

    @pytest.mark.parametrize("unknown", ["bogus", None, "MISSING"])
    def test_unknown_or_missing_trigger_is_not_recovered(self, temp_home, unknown):
        h = self._left_by(temp_home, "at-limit")
        self._set_state(h, leftRecoveryAt=h.clock.now - 1)
        if unknown == "MISSING":
            self._drop_trigger(h)
        else:
            self._set_state(h, leftTrigger=unknown)
        switches = _tick(h, **self._healthy_a(h))
        assert [s["to"]["number"] for s in switches] == [3]

    def test_at_limit_exit_still_opens_at_zero_headroom(self, temp_home):
        h = self._left_by(temp_home, "at-limit", recovery_at=None)
        # Only A is usable and the active B is at its limit: the exit opens.
        switches = _tick(
            h, a1=_fresh(h, 5, 5), a2=_fresh(h, 100, 100), a3=_fresh(h, 100, 100)
        )
        assert [(s["to"]["number"], s["trigger"]) for s in switches] == [
            (1, "at-limit")
        ]


class TestOldBarIsUntouched:
    """The bar of proactive, consume-first and balanced stays as it was."""

    STATE = {"lastSwitchFrom": 1, "lastSwitchTo": 2, "lastSwitchAt": 1.0}

    def _call(self, h, trigger):
        settings = h.settings
        return h.engine._no_return_account(
            trigger,
            dict(self.STATE),
            {"1": 20.0, "2": 10.0},
            10.0,
            False,
            settings,
            "2",
            usage={"1": _usage7(80, 80), "2": _usage7(90, 90)},
        )

    @pytest.mark.parametrize("trigger", ["proactive", "consume-first", "balanced"])
    def test_old_scope_still_bars_the_account_we_left(self, temp_home, trigger):
        h = _f2_harness(temp_home)
        assert self._call(h, trigger) == "1"

    @pytest.mark.parametrize("trigger", ["at-limit", "failover"])
    def test_old_function_still_leaves_forced_triggers_alone(
        self, temp_home, trigger
    ):
        h = _f2_harness(temp_home)
        assert self._call(h, trigger) is None


# ---------------------------------------------------------------------------
# E4 (F3): at-limit and failover prefer a target under its own line.
# ---------------------------------------------------------------------------
# The tests read the ORDER in which the engine tries to freshen the targets.
# Every freshen fails, so the recorded list is the whole ranked list.

from contextlib import contextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402

TRIGGERS = ["at-limit", "failover"]


def _f3_harness(temp_home: Path, accounts: int = 3, **settings) -> EngineHarness:
    h = EngineHarness(
        temp_home,
        **{
            "strategy": "best",
            "threshold": 98.0,
            "weekly_threshold": None,
            "interval_seconds": 60.0,
            "cooldown_seconds": 300.0,
            "hysteresis_pct": 10.0,
            "unhealthy_ticks": 3,
            **settings,
        },
    )
    for num in range(1, accounts + 1):
        h.seed(num, f"acc{num}@example.com")
    h.make_live("acc1@example.com", 1)
    data = h.switcher._get_sequence_data()
    data["activeAccountNumber"] = 1
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


@contextmanager
def _recording_freshen(h: EngineHarness):
    attempts: list[str] = []

    def freshen(num, email):
        attempts.append(str(num))
        return "transient"

    with patch.object(h.engine, "_freshen_target", side_effect=freshen):
        yield attempts


def _rows(h: EngineHarness, rows: dict[str, tuple | dict]) -> dict[str, UsageEntry]:
    """A row is ``(used5, used7)`` or a full usage dict."""
    return {
        num: _entry_for(
            _usage7(*row) if isinstance(row, tuple) else row, h.clock.now
        )
        for num, row in rows.items()
    }


def _order(h: EngineHarness, trigger: str, rows: dict[str, tuple | dict]) -> list[str]:
    """The targets the engine tries, in order, when it must leave account 1."""
    with _recording_freshen(h) as attempts:
        if trigger == "at-limit":
            active = {"1": _entry_for(_usage7(100, 100), h.clock.now)}
            h.tick_with_entries({**active, **_rows(h, rows)})
        else:
            for _ in range(3):  # unhealthy_ticks
                h.tick_with_entries({"1": UsageEntry(), **_rows(h, rows)})
                h.clock.advance(TICK_SECONDS)
    return attempts


@pytest.mark.parametrize("trigger", TRIGGERS)
class TestForcedTargetUnderItsLine:
    def test_99_weekly_loses_to_60(self, temp_home, trigger):
        h = _f3_harness(temp_home)
        assert _order(h, trigger, {"2": (5, 99), "3": (5, 60)}) == ["3", "2"]

    def test_the_answer_does_not_depend_on_the_slot(self, temp_home, trigger):
        h = _f3_harness(temp_home)
        assert _order(h, trigger, {"2": (5, 60), "3": (5, 99)}) == ["2", "3"]

    def test_per_account_line_beats_raw_headroom(self, temp_home, trigger):
        # A: own line 50, at 60 (raw headroom 40, over). B: 80 (raw 20), under 98.
        h = _f3_harness(temp_home)
        h.switcher.set_account_threshold("2", 50.0)
        assert _order(h, trigger, {"2": (5, 60), "3": (5, 80)}) == ["3", "2"]

    def test_weekly_line_beats_raw_headroom(self, temp_home, trigger):
        # A: 7d 92 over the weekly line 90 (raw 8). B: 5h 95 under 98 (raw 5).
        h = _f3_harness(temp_home, weekly_threshold=90.0)
        assert _order(h, trigger, {"2": (5, 92), "3": (95, 5)}) == ["3", "2"]

    def test_five_hour_line_over_while_weekly_is_under(self, temp_home, trigger):
        # 5h line 80, weekly line 98. A: 5h 85 over (raw 15). B: 7d 90 under (raw 10).
        h = _f3_harness(temp_home, threshold=80.0, weekly_threshold=98.0)
        assert _order(h, trigger, {"2": (85, 5), "3": (5, 90)}) == ["3", "2"]

    def test_every_candidate_over_its_line_keeps_the_most_headroom(
        self, temp_home, trigger
    ):
        h = _f3_harness(temp_home)
        assert _order(h, trigger, {"2": (5, 99.5), "3": (5, 99)}) == ["3", "2"]

    def test_every_candidate_under_its_line_keeps_the_headroom_order(
        self, temp_home, trigger
    ):
        h = _f3_harness(temp_home, accounts=4)
        assert _order(
            h, trigger, {"2": (5, 50), "3": (5, 70), "4": (5, 30)}
        ) == ["4", "2", "3"]

    def test_per_model_window_over_its_line(self, temp_home, trigger):
        h = _f3_harness(temp_home, model="Fable")

        def usage(five_h, fable):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {"pct": 0.0},
                "scoped": [{"name": "Fable", "pct": fable}],
            }

        assert _order(h, trigger, {"2": usage(5, 99), "3": usage(60, 0)}) == ["3", "2"]

    def test_per_model_window_uses_the_weekly_line(self, temp_home, trigger):
        # Fable 92 is over the weekly line 90 (raw 8); B's 5h 95 is under 98 (raw 5).
        h = _f3_harness(temp_home, model="Fable", weekly_threshold=90.0)

        def usage(five_h, fable):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {"pct": 0.0},
                "scoped": [{"name": "Fable", "pct": fable}],
            }

        assert _order(h, trigger, {"2": usage(5, 92), "3": usage(95, 0)}) == ["3", "2"]

    def test_the_rest_of_the_list_follows_the_partition(self, temp_home, trigger):
        # Over: 2 (7d 99). Under: 3 (60) and 4 (80). After 3 fails, 4 is next, then 2.
        h = _f3_harness(temp_home, accounts=4)
        assert _order(
            h, trigger, {"2": (5, 99), "3": (5, 60), "4": (5, 80)}
        ) == ["3", "4", "2"]

    def test_the_rest_of_the_list_with_a_per_account_line(self, temp_home, trigger):
        # 2 is over its own line 50 (at 60, raw 40); 3 and 4 are under 98.
        h = _f3_harness(temp_home, accounts=4)
        h.switcher.set_account_threshold("2", 50.0)
        assert _order(
            h, trigger, {"2": (5, 60), "3": (5, 90), "4": (5, 85)}
        ) == ["4", "3", "2"]


# ---------------------------------------------------------------------------
# E5 (F6): every account exhausted: no rotation, one report, self-resume.
# ---------------------------------------------------------------------------
# The engine reports the state with the event kind `all-exhausted` (it exists
# since before E5). E5 adds `reason`, `detail` and `freesAccount` to that
# event, limits its repeats, and names only usable accounts.

from claude_swap.autoswitch import TickOutcome  # noqa: E402
from tests.test_autoswitch import _iso_at  # noqa: E402

REPEAT_S = 900.0  # ALL_EXHAUSTED_REPEAT_S, written out so a changed constant shows
CANARY = "canary"
STRATEGIES = ["best", "consume-first", "balanced"]


def _f6_harness(
    temp_home: Path, strategy: str = "best", accounts: int = 6
) -> EngineHarness:
    h = EngineHarness(
        temp_home,
        strategy=strategy,
        threshold=98.0,
        weekly_threshold=None,
        interval_seconds=60.0,
        cooldown_seconds=300.0,
        hysteresis_pct=10.0,
        unhealthy_ticks=3,
    )
    for num in range(1, accounts + 1):
        h.seed(num, f"{CANARY}{num}@example.com")
    h.make_live(f"{CANARY}1@example.com", 1)
    data = h.switcher._get_sequence_data()
    data["activeAccountNumber"] = 1
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _win(
    used5: float,
    used7: float,
    reset5: float | None = None,
    reset7: float | None = None,
) -> dict:
    """A usage row; a reset is an epoch time or None (no reset time sent)."""
    five: dict = {"pct": used5}
    seven: dict = {"pct": used7}
    if reset5 is not None:
        five["resets_at"] = _iso_at(reset5)
    if reset7 is not None:
        seven["resets_at"] = _iso_at(reset7)
    return {"five_hour": five, "seven_day": seven}


def _exhausted_fleet(t0: float) -> dict[str, dict]:
    """Six accounts, each at 100 percent in a window. Account 3 comes back
    first (40 minutes). Account 5 has a sooner 5h reset (10 minutes) but its
    weekly window stays blocked for 4 hours."""
    return {
        "1": _win(100, 40, reset5=t0 + 3 * 3600),
        "2": _win(10, 100, reset7=t0 + 2 * 3600),
        "3": _win(100, 30, reset5=t0 + 40 * 60),
        "4": _win(20, 100, reset7=t0 + 5 * 3600),
        "5": _win(100, 100, reset5=t0 + 10 * 60, reset7=t0 + 4 * 3600),
        "6": _win(100, 50, reset5=t0 + 3600),
    }


def _exhausted_events(h: EngineHarness) -> list[dict]:
    """The JSON of each all-exhausted report in ``h.events``."""
    return [
        d
        for d in (e.to_json() for e in h.events)
        if d["event"] == "all-exhausted" or d.get("reason") == "all-exhausted"
    ]


def _f6_tick(h: EngineHarness, usage: dict) -> tuple[TickOutcome, list[dict]]:
    h.events.clear()
    outcome = h.tick_with_usage(usage)
    return outcome, _exhausted_events(h)


def _run_exhausted(
    h: EngineHarness, usage: dict, minutes: int
) -> tuple[list[int], list[int]]:
    """Tick each minute from 0 to ``minutes``. Returns the minutes that had a
    report and the minutes that had a switch."""
    reported, switched = [], []
    for minute in range(minutes + 1):
        if minute:
            h.clock.advance(TICK_SECONDS)
        _, found = _f6_tick(h, usage)
        if found:
            reported.append(minute)
        if any(e.kind == "switch" for e in h.events):
            switched.append(minute)
    return reported, switched


class TestAllExhausted:
    """E5 (F6): decision F6 of the engine reliability spec."""

    def test_no_switch_in_thirty_minutes(self, temp_home):
        h = _f6_harness(temp_home)
        _, switched = _run_exhausted(h, _exhausted_fleet(h.clock.now), 30)
        assert switched == []
        assert h.active_number() == 1

    def test_first_tick_reports_the_earliest_reset_and_its_account(self, temp_home):
        h = _f6_harness(temp_home)
        t0 = h.clock.now
        outcome, found = _f6_tick(h, _exhausted_fleet(t0))
        assert outcome is TickOutcome.BLOCKED
        assert len(found) == 1
        event = found[0]
        assert event["reason"] == "all-exhausted"
        assert event["earliestResetAt"] == _iso_at(t0 + 40 * 60)
        assert event["freesAccount"] == 3
        assert _iso_at(t0 + 40 * 60) in event["detail"]
        assert "account 3" in event["detail"]

    def test_at_most_three_reports_in_thirty_minutes(self, temp_home):
        h = _f6_harness(temp_home)
        reported, _ = _run_exhausted(h, _exhausted_fleet(h.clock.now), 30)
        assert reported == [0, 15, 30]

    def test_a_quiet_tick_emits_only_the_poll_event(self, temp_home):
        h = _f6_harness(temp_home)
        usage = _exhausted_fleet(h.clock.now)
        _f6_tick(h, usage)
        h.clock.advance(TICK_SECONDS)
        outcome, found = _f6_tick(h, usage)
        assert found == []
        assert h.kinds() == ["poll"]
        assert outcome is TickOutcome.BLOCKED

    def test_the_repeat_limit_is_fifteen_minutes(self, temp_home):
        h = _f6_harness(temp_home)
        usage = _exhausted_fleet(h.clock.now)
        _f6_tick(h, usage)
        h.clock.advance(REPEAT_S - 1)
        assert _f6_tick(h, usage)[1] == []
        h.clock.advance(1)
        assert len(_f6_tick(h, usage)[1]) == 1

    def test_engine_switches_after_the_earliest_reset(self, temp_home):
        h = _f6_harness(temp_home)
        t0 = h.clock.now
        usage = _exhausted_fleet(t0)
        _, switched = _run_exhausted(h, usage, 40)
        assert switched == []
        h.clock.advance(TICK_SECONDS)  # minute 41: the 5h window of 3 reset
        usage["3"] = _win(0, 30)
        _, found = _f6_tick(h, usage)
        assert found == []
        switches = [e.to_json() for e in h.events if e.kind == "switch"]
        assert [(s["from"]["number"], s["to"]["number"]) for s in switches] == [(1, 3)]
        assert h.active_number() == 3
        # The state ended: account 3 works, and no report comes.
        h.clock.advance(TICK_SECONDS)
        usage["3"] = _win(2, 30)
        outcome, found = _f6_tick(h, usage)
        assert found == []
        assert outcome is TickOutcome.NO_ACTION
        assert not any(e.kind == "switch" for e in h.events)

    def test_a_new_exhausted_state_reports_on_its_first_tick(self, temp_home):
        """The state ends when an account has headroom. A state that starts
        again reports at once, also inside the repeat limit of the old one."""
        h = _f6_harness(temp_home)
        t0 = h.clock.now
        usage = _exhausted_fleet(t0)
        assert len(_f6_tick(h, usage)[1]) == 1
        h.clock.advance(TICK_SECONDS)
        assert _f6_tick(h, usage)[1] == []
        # Account 1 (active) has headroom for one tick: the state ended.
        h.clock.advance(TICK_SECONDS)
        assert _f6_tick(h, {**usage, "1": _win(50, 40)})[1] == []
        # All exhausted again, 3 minutes after the first report.
        h.clock.advance(TICK_SECONDS)
        assert len(_f6_tick(h, usage)[1]) == 1

    def test_one_unreadable_candidate_is_not_the_exhausted_state(self, temp_home):
        """Pin of the behavior before E5: unknown is not exhausted."""
        h = _f6_harness(temp_home)
        usage = {**_exhausted_fleet(h.clock.now), "4": None}
        for _ in range(3):
            outcome, found = _f6_tick(h, usage)
            assert found == []
            assert outcome is TickOutcome.BLOCKED
            assert _last_no_switch(h)["reason"] == "no-qualifying-candidate"
            assert not any(e.kind == "switch" for e in h.events)
            h.clock.advance(TICK_SECONDS)

    def test_quarantined_and_disabled_candidates_are_not_usable(self, temp_home):
        h = _f6_harness(temp_home)
        t0 = h.clock.now
        h.engine._quarantine("2", f"{CANARY}2@example.com", "invalid_grant")
        h.switcher.set_account_disabled("5", True)
        usage = _exhausted_fleet(t0)
        usage["2"] = _win(10, 10)  # quarantined, with headroom
        usage["5"] = _win(100, 10, reset5=t0 + 5 * 60)  # disabled, soonest reset
        outcome, found = _f6_tick(h, usage)
        assert outcome is TickOutcome.BLOCKED
        assert len(found) == 1
        assert found[0]["earliestResetAt"] == _iso_at(t0 + 40 * 60)
        assert found[0]["freesAccount"] == 3
        reported, switched = _run_exhausted(h, usage, 5)
        assert switched == []
        assert h.active_number() == 1

    def test_unknown_reset_times_say_unknown(self, temp_home):
        h = _f6_harness(temp_home)
        usage = {str(n): _win(100, 100) for n in range(1, 7)}
        outcome, found = _f6_tick(h, usage)
        assert outcome is TickOutcome.BLOCKED
        assert len(found) == 1
        assert found[0]["earliestResetAt"] is None
        assert found[0]["freesAccount"] is None
        assert "unknown" in found[0]["detail"]

    def test_no_email_in_the_report_or_the_state_file(self, temp_home):
        h = _f6_harness(temp_home)
        usage = _exhausted_fleet(h.clock.now)
        texts = []
        for _ in range(3):
            _, found = _f6_tick(h, usage)
            texts += [json.dumps(d) for d in found]
            texts += [e.human() for e in h.events if e.kind == "all-exhausted"]
            h.clock.advance(REPEAT_S)
        assert len(texts) == 6
        state_file = h.switcher.backup_dir / "autoswitch_state.json"
        if state_file.exists():
            texts.append(state_file.read_text())
        for text in texts:
            assert CANARY not in text
            assert "@" not in text

    def test_json_has_the_fields_of_a_no_switch_event(self, temp_home):
        from claude_swap.autoswitch import NoSwitchEvent

        h = _f6_harness(temp_home)
        _, found = _f6_tick(h, _exhausted_fleet(h.clock.now))
        event = found[0]
        other = NoSwitchEvent(reason="below-threshold", detail="x").to_json()
        assert set(other) <= set(event)
        for key, value in other.items():
            assert type(event[key]) is type(value), key
        assert event["schemaVersion"] == other["schemaVersion"]
        assert event["event"] == "all-exhausted"
        assert set(event) - set(other) == {"earliestResetAt", "freesAccount"}
        json.dumps(event)

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_each_strategy_reaches_the_same_state(self, temp_home, strategy):
        h = _f6_harness(temp_home, strategy=strategy)
        t0 = h.clock.now
        outcome, found = _f6_tick(h, _exhausted_fleet(t0))
        assert outcome is TickOutcome.BLOCKED
        assert len(found) == 1
        assert found[0]["reason"] == "all-exhausted"
        assert found[0]["freesAccount"] == 3
        reported, switched = _run_exhausted(h, _exhausted_fleet(t0), 10)
        assert reported == []
        assert switched == []
        assert h.active_number() == 1
