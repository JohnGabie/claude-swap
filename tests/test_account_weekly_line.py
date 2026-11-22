"""Account weekly line: store, resolution, engine, balanced strategy, CLI, TUI.

Spec: docs/superpowers/specs/2026-11-22-pill-switch-lines-design.md, blocks
SL2 (line resolution), SL6 (data contract) and SL7 (scenarios S1-S4, S6, S7,
S9 store part).

Synthetic fixtures only: every store lives under `temp_home` / `tmp_path`.
No real account store is read or written.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from claude_swap import balance
from claude_swap import autoswitch
from claude_swap.autoswitch import (
    NoSwitchEvent,
    PollEvent,
    SwitchEvent,
    ThresholdMap,
    TickOutcome,
    _parse_reset_ts,
)
from claude_swap.usage_store import STALE_OK_S, UsageEntry
from claude_swap import lines
from claude_swap.models import AccountPolicy
from claude_swap.switcher import ClaudeAccountSwitcher

from tests.test_account_policy_cli import PolicyCliBase
from tests import test_account_policy_store as policy_store
from tests.test_account_policy_store import PolicyStoreBase
from tests.test_autoswitch import EngineHarness, _usage7

R_LATER = "2024-01-08T00:00:00Z"


# ---------------------------------------------------------------------------
# S1, S2: resolution (lines.account_lines, lines.effective_weekly)
# ---------------------------------------------------------------------------


def test_account_lines_without_weekly_key_match_today():
    """S1: general 90, weekly unset; account 2 has threshold 85, no weekly."""
    policy = AccountPolicy(threshold=85.0)
    assert lines.account_lines(policy, 90.0, None) == (85.0, None)
    assert lines.effective_weekly(85.0, None) == 85.0


def test_account_weekly_line_wins():
    """S2: general 90 / 97; account 2 has 85 / 92, account 3 has nothing."""
    two = AccountPolicy(threshold=85.0, weekly_threshold=92.0)
    three = AccountPolicy()
    assert lines.account_lines(two, 90.0, 97.0) == (85.0, 92.0)
    assert lines.account_lines(three, 90.0, 97.0) == (90.0, 97.0)
    assert lines.effective_weekly(85.0, 92.0) == 92.0


# ---------------------------------------------------------------------------
# Store: _policy_from_data, write_account_line (S6, S7, S9 store part)
# ---------------------------------------------------------------------------


class TestPolicyReadsTheWeeklyKey(PolicyStoreBase):
    def test_a_stored_weekly_line_reads_back(self, temp_home: Path):
        s = self._fleet(temp_home)
        data = s._get_sequence_data()
        data["accounts"]["2"]["weeklyThreshold"] = 92
        s._write_json(s.sequence_file, data)
        assert s.account_policies()["2"] == AccountPolicy(weekly_threshold=92.0)

    @pytest.mark.parametrize("bad", ["abc", 20, 100, True, None, [92]])
    def test_a_bad_stored_value_reads_as_not_set(self, temp_home: Path, bad):
        s = self._fleet(temp_home)
        data = s._get_sequence_data()
        data["accounts"]["2"]["weeklyThreshold"] = bad
        s._write_json(s.sequence_file, data)
        assert s.account_policies()["2"].weekly_threshold is None


class TestWriteAccountLine(PolicyStoreBase):
    def test_weekly_account_line_round_trip(self, temp_home: Path):
        """S6: set then clear; the record ends as it started."""
        s = self._fleet(temp_home)
        data = s._get_sequence_data()
        data["accounts"]["2"]["threshold"] = 85.0
        s._write_json(s.sequence_file, data)
        first = dict(self._record(s, "2"))
        assert "weeklyThreshold" not in first

        assert s.write_account_line("2", "weeklyThreshold", 92) == {
            "ok": True, "error": None,
        }
        record = self._record(s, "2")
        assert record["weeklyThreshold"] == 92.0
        assert isinstance(record["weeklyThreshold"], float)
        assert record["threshold"] == 85.0

        assert s.write_account_line("2", "weeklyThreshold", None) == {
            "ok": True, "error": None,
        }
        assert self._record(s, "2") == first
        raw = json.loads(Path(s.sequence_file).read_text())
        assert "weeklyThreshold" not in raw["accounts"]["2"]

    def test_the_five_hour_key_round_trips_too(self, temp_home: Path):
        s = self._fleet(temp_home)
        first = dict(self._record(s, "2"))
        assert s.write_account_line("2", "threshold", 80.5)["ok"] is True
        assert self._record(s, "2")["threshold"] == 80.5
        assert s.write_account_line("2", "threshold", None)["ok"] is True
        assert self._record(s, "2") == first

    def test_write_account_line_is_silent(self, temp_home: Path, capsys, caplog):
        s = self._fleet(temp_home)
        capsys.readouterr()
        with caplog.at_level(logging.DEBUG):
            s.write_account_line("2", "weeklyThreshold", 92)
            s.write_account_line("2", "threshold", 85)
            s.write_account_line("2", "weeklyThreshold", None)
            s.write_account_line("2", "weeklyThreshold", 101)
            s.write_account_line("9", "threshold", 85)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        messages = [r.getMessage() for r in caplog.records]
        assert any("weeklyThreshold" in m for m in messages), messages
        assert all("@" not in m for m in messages)

    def test_write_account_line_error_has_no_email(self, temp_home: Path, monkeypatch):
        """S9 store part: each failing write returns an error with no `@`."""
        s = self._setup(temp_home)
        self._seed(s, 1, "b@example.com")
        self._seed(s, 2, "a@example.com")
        data = s._get_sequence_data()
        for rec in data["accounts"].values():
            rec["organizationName"] = "a@example.com"
            rec["alias"] = "a@example.com"
        s._write_json(s.sequence_file, data)
        before = Path(s.sequence_file).read_bytes()

        failures = [
            s.write_account_line("2", "weeklyThreshold", 101),
            s.write_account_line("2", "weeklyThreshold", True),
            s.write_account_line("2", "weeklyThreshold", "abc"),
            s.write_account_line("2", "dailyThreshold", 80),
            s.write_account_line("9", "weeklyThreshold", 80),
            s.write_account_line("a@example.com", "weeklyThreshold", 80),
        ]

        def boom(path, payload):
            raise OSError(f"disk full writing for a@example.com at {path}")

        monkeypatch.setattr(s, "_write_json", boom)
        failures.append(s.write_account_line("2", "weeklyThreshold", 80))

        for result in failures:
            assert result["ok"] is False
            assert isinstance(result["error"], str) and result["error"]
            assert "@" not in json.dumps(result)
        assert Path(s.sequence_file).read_bytes() == before


class TestWriteAccountLineReturnPaths(PolicyStoreBase):
    """One test per return path of `write_account_line`: the exact result,
    no `@` in it, and `sequence.json` bytes unchanged on every refusal."""

    BACKDATED = "2024-01-01T00:00:00Z"

    @pytest.fixture
    def acquires(self, monkeypatch) -> list:
        """Each `FileLock.acquire` call: the checks before the lock must
        refuse without taking it."""
        from claude_swap.locking import FileLock

        calls: list = []
        real = FileLock.acquire

        def counted(self, timeout=None):
            calls.append(self.lock_path)
            return real(self, timeout)

        monkeypatch.setattr(FileLock, "acquire", counted)
        return calls

    def _refused(self, s, result: dict, error: str, before: bytes) -> None:
        assert result == {"ok": False, "error": error}
        assert "@" not in json.dumps(result)
        assert Path(s.sequence_file).read_bytes() == before

    def test_unknown_key(self, temp_home: Path, acquires):
        s = self._fleet(temp_home)
        before = Path(s.sequence_file).read_bytes()
        self._refused(
            s, s.write_account_line("2", "dailyThreshold", 80), "unknown line key", before
        )
        assert acquires == []

    @pytest.mark.parametrize("num", [True, None, 2.0, ["2"]])
    def test_a_number_that_is_not_a_slot_type(self, temp_home: Path, acquires, num):
        s = self._fleet(temp_home)
        before = Path(s.sequence_file).read_bytes()
        self._refused(
            s, s.write_account_line(num, "threshold", 80), "unknown account", before
        )
        assert acquires == []

    def test_unknown_account(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = Path(s.sequence_file).read_bytes()
        self._refused(
            s, s.write_account_line("9", "threshold", 80), "unknown account", before
        )

    @pytest.mark.parametrize("value", [101, 49.9, True, "abc", float("nan")])
    def test_out_of_range(self, temp_home: Path, acquires, value):
        s = self._fleet(temp_home)
        before = Path(s.sequence_file).read_bytes()
        self._refused(
            s, s.write_account_line("2", "weeklyThreshold", value),
            "line must be between 50 and 99.9", before,
        )
        assert acquires == []

    def test_no_accounts_managed_yet(self, temp_home: Path, acquires):
        s = ClaudeAccountSwitcher()
        assert not Path(s.sequence_file).exists()
        assert s.write_account_line("2", "weeklyThreshold", 92) == {
            "ok": False, "error": "no accounts are managed yet",
        }
        assert not Path(s.lock_file).exists()
        assert not Path(s.sequence_file).exists()
        assert acquires == []

    def test_busy_while_the_account_lock_is_held(self, temp_home: Path, monkeypatch):
        from claude_swap import switcher as switcher_module
        from claude_swap.locking import FileLock

        s = self._fleet(temp_home)
        before = Path(s.sequence_file).read_bytes()
        monkeypatch.setattr(switcher_module, "ACCOUNT_LINE_LOCK_TIMEOUT_S", 0.1)
        held = FileLock(s.lock_file, timeout=0.1)
        assert held.acquire()
        try:
            result = s.write_account_line("2", "weeklyThreshold", 92)
        finally:
            held.release()
        self._refused(s, result, "account store is busy", before)

    def test_busy_when_the_lock_cannot_be_opened(self, temp_home: Path, monkeypatch):
        from claude_swap.locking import FileLock

        s = self._fleet(temp_home)
        before = Path(s.sequence_file).read_bytes()

        def refuse(self, timeout=None):
            raise OSError("lock directory is read-only")

        monkeypatch.setattr(FileLock, "acquire", refuse)
        self._refused(
            s, s.write_account_line("2", "weeklyThreshold", 92),
            "account store is busy", before,
        )

    def test_unreadable_store(self, temp_home: Path, monkeypatch):
        from claude_swap.exceptions import ConfigError

        s = self._fleet(temp_home)
        before = Path(s.sequence_file).read_bytes()

        def torn():
            raise ConfigError("sequence.json is torn")

        monkeypatch.setattr(s, "_get_sequence_data", torn)
        self._refused(
            s, s.write_account_line("2", "weeklyThreshold", 92),
            "account store is unreadable", before,
        )

    def test_write_failed(self, temp_home: Path, monkeypatch):
        s = self._fleet(temp_home)
        before = Path(s.sequence_file).read_bytes()

        def boom(path, payload):
            raise OSError("disk full")

        monkeypatch.setattr(s, "_write_json", boom)
        self._refused(
            s, s.write_account_line("2", "weeklyThreshold", 92),
            "account store write failed", before,
        )

    def test_clearing_an_absent_line_writes_nothing(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = Path(s.sequence_file).read_bytes()
        assert s.write_account_line("2", "weeklyThreshold", None) == {
            "ok": True, "error": None,
        }
        assert Path(s.sequence_file).read_bytes() == before

    def test_setting_the_same_value_writes_nothing(self, temp_home: Path):
        s = self._fleet(temp_home)
        assert s.write_account_line("2", "weeklyThreshold", 92)["ok"]
        data = s._get_sequence_data()
        data["lastUpdated"] = self.BACKDATED
        s._write_json(s.sequence_file, data)
        before = Path(s.sequence_file).read_bytes()
        assert s.write_account_line("2", "weeklyThreshold", 92.0) == {
            "ok": True, "error": None,
        }
        assert Path(s.sequence_file).read_bytes() == before

    def test_a_write_stamps_last_updated(self, temp_home: Path):
        s = self._fleet(temp_home)
        assert s._get_sequence_data()["lastUpdated"] == self.BACKDATED
        assert s.write_account_line("2", "weeklyThreshold", 92)["ok"]
        assert s._get_sequence_data()["lastUpdated"] != self.BACKDATED

    def test_the_lock_is_released_before_the_log_line(self, temp_home: Path, monkeypatch):
        """The writer releases the account lock itself, before it logs, not
        when the lock object is garbage collected at return."""
        from claude_swap.locking import FileLock

        s = self._fleet(temp_home)
        free: list[bool] = []

        def probe(msg, *args, **kwargs):
            lock = FileLock(s.lock_file, timeout=0.0)
            got = lock.acquire()
            if got:
                lock.release()
            free.append(got)

        monkeypatch.setattr(s._logger, "info", probe)
        assert s.write_account_line("2", "weeklyThreshold", 92)["ok"]
        assert free == [True]

    def test_the_lock_is_released_after_a_write(self, temp_home: Path):
        from claude_swap.locking import FileLock

        s = self._fleet(temp_home)
        assert s.write_account_line("2", "weeklyThreshold", 92)["ok"]
        probe = FileLock(s.lock_file, timeout=0.0)
        assert probe.acquire()
        probe.release()


class TestReloginKeepsTheWeeklyLine(PolicyStoreBase):
    def test_relogin_keeps_weekly_account_line(self, temp_home: Path):
        """S7: `add --slot 2` on the same identity keeps `weeklyThreshold`."""
        s = self._fleet(temp_home, count=2)
        assert s.write_account_line("2", "weeklyThreshold", 92)["ok"] is True
        policy_store.TestPolicySurvivesSlotReadd._make_live(
            temp_home, "account-2@example.test", 2
        )

        s.add_account(slot=2)

        assert self._record(s, "2")["weeklyThreshold"] == 92.0
        assert s.account_policies()["2"].weekly_threshold == 92.0
        assert "weeklyThreshold" in ClaudeAccountSwitcher.USER_ACCOUNT_SETTINGS


# ---------------------------------------------------------------------------
# Engine: S3, S4, poll cadence
# ---------------------------------------------------------------------------


def _s3_fleet(temp_home, *, weekly_key: float | None, **settings) -> EngineHarness:
    """Accounts 2 (active) and 3. Account 2 has no `threshold`; its
    `weeklyThreshold` is `weekly_key` (None = no key)."""
    h = EngineHarness(temp_home, strategy="best", **settings)
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("b@example.com", 2)
    if weekly_key is not None:
        assert h.switcher.write_account_line("2", "weeklyThreshold", weekly_key)["ok"]
    h.engine = h._make_engine()
    return h


S3_USAGE = {"2": _usage7(10.0, 92.0), "3": _usage7(0.0, 20.0)}


def _switches(h) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


def _polls(h) -> list[PollEvent]:
    return [e for e in h.events if isinstance(e, PollEvent)]


def test_engine_switches_at_account_weekly_line(temp_home):
    """S3: general weekly line 97; account 2 leaves at its own weekly line
    92 (7d at 92). The general line alone would keep it (92 < 97)."""
    h = _s3_fleet(temp_home, weekly_key=92.0, weekly_threshold=97.0)
    outcome = h.tick_with_usage(S3_USAGE)
    assert outcome is TickOutcome.SWITCHED
    switches = _switches(h)
    assert len(switches) == 1
    payload = switches[0].to_json()
    assert payload["from"]["number"] == 2
    assert payload["to"]["number"] == 3
    assert payload["trigger"] == "proactive"
    assert _polls(h)[0].to_json()["weeklyThreshold"] == 92.0


def test_engine_holds_under_a_higher_account_weekly_line(temp_home):
    """Account weekly line 95 above the 5h line 90: 7d 92 stays."""
    h = _s3_fleet(temp_home, weekly_key=95.0)
    outcome = h.tick_with_usage(S3_USAGE)
    assert outcome is TickOutcome.NO_ACTION
    assert not _switches(h)
    reasons = [getattr(e, "reason", None) for e in h.events]
    assert "below-threshold" in reasons


def test_engine_without_account_weekly_key_is_unchanged(temp_home):
    """S4: no key, the weekly line falls back to the 5h line 90."""
    h = _s3_fleet(temp_home, weekly_key=None)
    outcome = h.tick_with_usage(S3_USAGE)
    assert outcome is TickOutcome.SWITCHED
    switches = _switches(h)
    assert len(switches) == 1
    payload = switches[0].to_json()
    assert (payload["from"]["number"], payload["to"]["number"]) == (2, 3)
    assert payload["trigger"] == "proactive"
    poll = _polls(h)[0]
    assert poll.weekly_threshold is None
    assert "weeklyThreshold" not in poll.to_json()


# ---------------------------------------------------------------------------
# Engine call sites: each one reads the account's weekly line, not the
# general one (a candidate or the active account with its own line).
# ---------------------------------------------------------------------------


def _fleet234(temp_home, **settings) -> EngineHarness:
    """Accounts 2 (active), 3 and 4."""
    h = EngineHarness(temp_home, **settings)
    for num, email in ((2, "b@example.com"), (3, "c@example.com"), (4, "d@example.com")):
        h.seed(num, email)
    h.make_live("b@example.com", 2)
    return h


def _reasons(h) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def test_landing_skips_a_candidate_over_its_own_weekly_line(temp_home):
    """`_rank_candidates` bindings. General weekly 97. Active 2 at 5h 91
    leaves. Account 3 has its own weekly line 70 and 7d 75: over it, so it
    is no landing spot. The engine lands on account 4 (5h 80)."""
    h = _fleet234(temp_home, weekly_threshold=97.0)
    assert h.switcher.write_account_line("3", "weeklyThreshold", 70)["ok"]
    h.engine = h._make_engine()
    outcome = h.tick_with_usage({
        "2": _usage7(91.0, 10.0),
        "3": _usage7(0.0, 75.0),
        "4": _usage7(80.0, 10.0),
    })
    assert outcome is TickOutcome.SWITCHED, _reasons(h)
    assert h.active_number() == 4


def test_consume_first_room_uses_the_candidates_weekly_line(temp_home):
    """`_rank_candidates` room. Consume-first, general weekly 97. Account 3
    resets first and has its own weekly line 80 at 7d 76: under the line
    (no over), but only 4 points of room, less than the hysteresis 10. The
    engine stays. On the line 97 the room would be 21 and it would move."""
    h = _fleet234(temp_home, strategy="consume-first", weekly_threshold=97.0)
    assert h.switcher.write_account_line("3", "weeklyThreshold", 80)["ok"]
    h.engine = h._make_engine()
    outcome = h.tick_with_usage({
        "2": _usage7(20.0, 20.0, "2024-01-10T00:00:00Z"),
        "3": _usage7(10.0, 76.0, "2024-01-05T00:00:00Z"),
        "4": _usage7(10.0, 10.0, "2024-01-12T00:00:00Z"),
    })
    assert outcome is TickOutcome.NO_ACTION, _reasons(h)
    assert h.active_number() == 2
    assert "already-consuming-soonest" in _reasons(h)


def _counted_tick(h, snapshot: dict):
    """One tick whose collector calls all serve `snapshot`; returns
    `(outcome, number of collector calls)`."""
    from unittest.mock import patch

    from tests.test_autoswitch import _entry_for

    entries = {n: _entry_for(v, h.clock.now) for n, v in snapshot.items()}
    with patch.object(
        h.switcher, "usage_entries_by_account", return_value=entries
    ) as mock:
        outcome = h.engine.tick()
    return outcome, mock.call_count


def test_escalation_band_uses_the_active_weekly_line(temp_home):
    """`_collect_scheduled_usage` lines. General weekly 97; active 2 has its
    own weekly line 80 and 7d 70: inside the band (70 >= 80 - 15), so the
    collector refetches every candidate (a third call). On the line 97 the
    band starts at 82 and there is no third call."""
    h = _fleet234(temp_home, weekly_threshold=97.0)
    assert h.switcher.write_account_line("2", "weeklyThreshold", 80)["ok"]
    h.engine = h._make_engine()
    outcome, calls = _counted_tick(h, {
        "2": _usage7(10.0, 70.0), "3": _usage7(10.0, 10.0), "4": _usage7(10.0, 10.0),
    })
    assert outcome is TickOutcome.NO_ACTION, _reasons(h)
    assert calls == 3


def test_endpoint_trouble_hold_reads_the_active_weekly_line(temp_home):
    """`_endpoint_trouble_verdict` of the tick. The active account's fetch
    timed out; its last good reading (10 min old) has 7d 85. Its own weekly
    line is 80, so the reading is not below every line: no F4 hold, the
    engine counts an unknown tick. On the general line 97 it would hold."""
    h = _fleet234(temp_home, weekly_threshold=97.0)
    assert h.switcher.write_account_line("2", "weeklyThreshold", 80)["ok"]
    h.engine = h._make_engine()
    now = h.clock.now
    age = 2 * STALE_OK_S
    stale = UsageEntry(
        last_good=_usage7(10.0, 85.0), fetched_at=now - age, age_s=age,
        last_error="timeout",
    )
    from tests.test_autoswitch import _entry_for

    entries = {
        "2": stale,
        "3": _entry_for(_usage7(10.0, 10.0), now),
        "4": _entry_for(_usage7(10.0, 10.0), now),
    }
    outcome = h.tick_with_entries(entries)
    assert outcome is TickOutcome.NO_ACTION
    assert _reasons(h) == ["active-usage-unknown"]


def test_departure_snapshot_records_the_weekly_binding_reset(temp_home):
    """`_binding_recovery_ts` of the departure snapshot. General weekly 97;
    active 2 has its own weekly line 92, 5h 88 (reset R5) and 7d 93 (reset
    R7). Over its weekly line, the 7d window binds, so `leftRecoveryAt` is
    R7. On the line 97 the 5h window would bind and record R5."""
    r5, r7 = "2024-01-05T00:00:00Z", "2024-01-08T00:00:00Z"
    h = _fleet234(temp_home, weekly_threshold=97.0)
    assert h.switcher.write_account_line("2", "weeklyThreshold", 92)["ok"]
    h.engine = h._make_engine()
    outcome = h.tick_with_usage({
        "2": {"five_hour": {"pct": 88.0, "resets_at": r5},
              "seven_day": {"pct": 93.0, "resets_at": r7}},
        "3": _usage7(0.0, 20.0),
        "4": _usage7(0.0, 30.0),
    })
    assert outcome is TickOutcome.SWITCHED, _reasons(h)
    assert h.state()["leftRecoveryAt"] == _parse_reset_ts(r7)


def test_no_return_bar_reads_the_barred_accounts_weekly_line(temp_home):
    """`_no_return_account`, unreadable active. Account 1 was left; its own
    weekly line is 80 and its 7d is 85: over its line, so it is no landing
    spot and the bar holds. On the general line 97 the bar would lift."""
    h = _fleet234(temp_home, weekly_threshold=97.0)
    barred = h.engine._no_return_account(
        "proactive",
        {"lastSwitchFrom": 1, "lastSwitchTo": "2"},
        {"1": 15.0},
        None,
        True,
        h.settings,
        "2",
        usage={"1": _usage7(10.0, 85.0)},
        thresholds=ThresholdMap({}, 90.0),
        weekly_lines=autoswitch.WeeklyLineMap({"1": 80.0}, 97.0),
    )
    assert barred == "1"


def test_priority_candidates_read_their_own_weekly_line(temp_home):
    """`_priority_ready` of each candidate. General weekly 97; account 3 has
    its own weekly line 70 and 7d 75: not ready, so only account 2 is
    ranked. On the line 97 account 3 would be ranked too."""
    h = _fleet234(temp_home, strategy="priority-consume-first", weekly_threshold=97.0)
    now = h.clock.now
    usage = {
        "2": _usage7(10.0, 20.0, "2024-01-08T00:00:00Z"),
        "3": _usage7(10.0, 75.0, "2024-01-05T00:00:00Z"),
    }
    ordered, any_known, _reset = h.engine._rank_priority_candidates(
        oauth_candidates=["2", "3"],
        usage=usage,
        headroom={"2": 80.0, "3": 25.0},
        current="4",
        active_enabled=False,
        settings=h.settings,
        now=now,
        thresholds=ThresholdMap({}, 90.0),
        policies={},
        weekly_lines=autoswitch.WeeklyLineMap({"3": 70.0}, 97.0),
    )
    assert ordered == ["2"]
    assert any_known is True


def test_priority_active_ready_reads_the_active_weekly_line(temp_home):
    """`priority_active_ready` of the tick. General weekly 97; active 2 has
    its own weekly line 70 and 7d 75: over it, so it is not ready, and with
    no ready candidate the tick is BLOCKED. On the line 97 the active
    account would be ready and the reason `already-consuming-priority`."""
    h = _fleet234(temp_home, strategy="priority-consume-first", weekly_threshold=97.0)
    assert h.switcher.write_account_line("2", "weeklyThreshold", 70)["ok"]
    h.engine = h._make_engine()
    outcome = h.tick_with_usage({
        "2": _usage7(10.0, 75.0),
        "3": _usage7(95.0, 10.0),
        "4": _usage7(95.0, 10.0),
    })
    assert outcome is TickOutcome.BLOCKED
    assert _reasons(h) == ["no-qualifying-candidate"]


def test_priority_active_key_reads_the_active_weekly_line(temp_home):
    """`_priority_ready` of the active account in the priority ranking.
    Active 4 has its own weekly line 70 and 7d 75: not ready, so it sets no
    bar and candidate 2 is ranked although it resets later. On the line 97
    the active account would be ready and its sooner reset would bar 2."""
    h = _fleet234(temp_home, strategy="priority-consume-first", weekly_threshold=97.0)
    ordered, _known, _reset = h.engine._rank_priority_candidates(
        oauth_candidates=["2"],
        usage={
            "2": _usage7(10.0, 20.0, "2024-01-10T00:00:00Z"),
            "4": _usage7(10.0, 75.0, "2024-01-05T00:00:00Z"),
        },
        headroom={"2": 80.0, "4": 25.0},
        current="4",
        active_enabled=True,
        settings=h.settings,
        now=h.clock.now,
        thresholds=ThresholdMap({}, 90.0),
        policies={},
        weekly_lines=autoswitch.WeeklyLineMap({"4": 70.0}, 97.0),
    )
    assert ordered == ["2"]


def test_forced_bar_reads_the_barred_accounts_weekly_line(temp_home):
    """`_forced_no_return_account`. An at-limit switch left account 1 60 s
    ago and its recovery time has passed. Its fresh reading has 7d 85, over
    its own weekly line 80: not healthy, so the bar holds. On the general
    line 97 it would be healthy and the bar would lift."""
    h = _fleet234(temp_home, weekly_threshold=97.0)
    now = h.clock.now
    barred = h.engine._forced_no_return_account(
        "at-limit",
        {
            "lastSwitchFrom": 1, "lastSwitchTo": "2", "lastSwitchAt": now - 60,
            "leftTrigger": "proactive", "leftRecoveryAt": now - 1,
        },
        {"1": UsageEntry(last_good=_usage7(10.0, 85.0), fetched_at=now, age_s=0.0)},
        {"1": _usage7(10.0, 85.0)},
        h.settings,
        ThresholdMap({}, 90.0),
        "2",
        now,
        weekly_lines=autoswitch.WeeklyLineMap({"1": 80.0}, 97.0),
    )
    assert barred == "1"


class TestLeftAccountRecoveredReadsWeeklyLines:
    """`_left_account_recovered`, one test per leg that reads a weekly
    line. General weekly 97 everywhere; the account named in
    `WeeklyLineMap` has its own weekly line 80."""

    H = 3600.0

    def _call(self, h, state, usage, headroom, active_headroom, own: dict):
        return h.engine._left_account_recovered(
            state, usage, headroom, active_headroom, h.settings, h.clock.now, "2",
            thresholds=ThresholdMap({}, 90.0),
            weekly_lines=autoswitch.WeeklyLineMap(own, 97.0),
        )

    def _split(self, h, five_h: float, week_h: float) -> dict:
        """5h 80 resetting in `five_h` hours, 7d 85 in `week_h` hours: under
        an 80 weekly line the 7d window binds, under 97 the 5h one."""
        from tests.test_autoswitch import _iso_at

        now = h.clock.now
        return {
            "five_hour": {"pct": 80.0, "resets_at": _iso_at(now + five_h * self.H)},
            "seven_day": {"pct": 85.0, "resets_at": _iso_at(now + week_h * self.H)},
        }

    def _one(self, h, pct5: float, pct7: float, reset_h: float) -> dict:
        from tests.test_autoswitch import _iso_at

        at = _iso_at(h.clock.now + reset_h * self.H)
        return {
            "five_hour": {"pct": pct5, "resets_at": at},
            "seven_day": {"pct": pct7, "resets_at": at},
        }

    FAILOVER = {
        "lastSwitchFrom": 1, "leftHeadroom": None, "leftRecoveryAt": None,
        "leftTrigger": "failover",
    }

    def test_failover_landing_leg(self, temp_home):
        """Peer 1 at 7d 85 over its line 80: no landing; no reset is known,
        so it is not recovered. On the line 97 it would land (True)."""
        h = _fleet234(temp_home, weekly_threshold=97.0)
        assert self._call(
            h, self.FAILOVER,
            {"1": _usage7(10.0, 85.0), "2": _usage7(10.0, 10.0)},
            {"1": 15.0}, 90.0, {"1": 80.0},
        ) is False

    def test_failover_peer_recovery_leg(self, temp_home):
        """Peer 1's binding window is its 7d (reset in 100 h), later than
        the active's 50 h: not recovered. On the line 97 its 5h (1 h) binds."""
        h = _fleet234(temp_home, weekly_threshold=97.0)
        assert self._call(
            h, self.FAILOVER,
            {"1": self._split(h, 1, 100), "2": self._one(h, 10.0, 10.0, 50)},
            {"1": 15.0}, 90.0, {"1": 80.0},
        ) is False

    def test_failover_active_recovery_leg(self, temp_home):
        """Active 2's binding window is its 7d (100 h); peer 1 is back in
        50 h, sooner: recovered. On the line 97 the active's 5h (1 h) binds
        and the peer is not sooner."""
        h = _fleet234(temp_home, weekly_threshold=97.0)
        assert self._call(
            h, self.FAILOVER,
            {"1": self._one(h, 95.0, 10.0, 50), "2": self._split(h, 1, 100)},
            {"1": 5.0}, 20.0, {"2": 80.0},
        ) is True

    def test_unreadable_active_landing_leg(self, temp_home):
        """Ordinary departure, unreadable active: peer 1 at 7d 85 over its
        line 80 is no landing spot; nothing else improved. On 97: True."""
        h = _fleet234(temp_home, weekly_threshold=97.0)
        state = {
            "lastSwitchFrom": 1, "leftHeadroom": 90.0, "leftRecoveryAt": None,
            "leftTrigger": "proactive",
        }
        assert self._call(
            h, state, {"1": _usage7(10.0, 85.0)}, {"1": 15.0}, None, {"1": 80.0},
        ) is False

    def test_ordinary_recovery_leg(self, temp_home):
        """Ordinary departure, left with a binding reset in 50 h. Peer 1's
        binding window is now its 7d (100 h): no nearer, not recovered. On
        the line 97 its 5h (1 h) would bind and count as recovered."""
        h = _fleet234(temp_home, weekly_threshold=97.0)
        state = {
            "lastSwitchFrom": 1, "leftHeadroom": 15.0,
            "leftRecoveryAt": h.clock.now + 50 * self.H, "leftTrigger": "proactive",
        }
        assert self._call(
            h, state, {"1": self._split(h, 1, 100)}, {"1": 15.0}, 50.0, {"1": 80.0},
        ) is False


class TestBalancedHoldQuotesTheAccountWeeklyLine:
    """The balanced below-threshold holds quote the active account's own
    weekly line 92 (general 97). An Ultra active account over its 5h line
    is still below for balanced; the detail then quotes its weekly window."""

    def _harness(self, temp_home, active_plan: str):
        from tests.test_autoswitch_balanced import _balanced

        h = _balanced(
            temp_home, plans={1: active_plan, 2: "pro"}, weekly_threshold=97.0
        )
        assert h.switcher.write_account_line("1", "weeklyThreshold", 92)["ok"]
        h.engine = h._make_engine()
        return h

    def _two_phase(self, h, first: dict, refetch: dict):
        """Serve `first` until the first balanced decision, then `refetch`."""
        from unittest.mock import patch

        from tests.test_autoswitch import _entry_for

        seen = {"decided": False}
        real_choice = h.engine._balanced_choice

        def choice(**kw):
            out = real_choice(**kw)
            seen["decided"] = True
            return out

        def collect(*args, **kwargs):
            snap = refetch if seen["decided"] else first
            return {n: _entry_for(v, h.clock.now) for n, v in snap.items()}

        with patch.object(h.switcher, "usage_entries_by_account", side_effect=collect), \
             patch.object(h.engine, "_balanced_choice", side_effect=choice):
            return h.engine.tick()

    @staticmethod
    def _details(h) -> list[str]:
        return [e.detail for e in h.events if isinstance(e, NoSwitchEvent)]

    def test_no_oauth_peer_hold(self, temp_home):
        from tests.test_autoswitch_balanced import _u

        h = self._harness(temp_home, "ultra")
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        outcome = h.tick_with_usage({"1": _u(95, 50, 100), "2": "api key"})
        assert outcome is TickOutcome.NO_ACTION
        assert self._details(h) == ["7d 50% < 92%"]

    def test_hold_after_the_refetch(self, temp_home):
        from tests.test_autoswitch_balanced import _u

        h = self._harness(temp_home, "ultra")
        outcome = self._two_phase(
            h,
            {"1": _u(95, 50, 100), "2": _u(10, 10, 20)},
            {"1": _u(95, 50, 100), "2": _u(10, 95, 20)},
        )
        assert outcome is TickOutcome.NO_ACTION
        assert self._details(h) == ["7d 50% < 92%"]

    def test_first_hold_when_the_refetch_is_unreadable(self, temp_home):
        from tests.test_autoswitch_balanced import _u

        h = self._harness(temp_home, "ultra")
        outcome = self._two_phase(
            h,
            {"1": _u(95, 50, 100), "2": _u(10, 10, 20)},
            {"1": None, "2": _u(10, 95, 20)},
        )
        assert outcome is TickOutcome.NO_ACTION
        assert self._details(h) == ["7d 50% < 92%"]

    def test_refetched_binding_quotes_the_weekly_line(self, temp_home):
        """Pro active at 7d 91 under its line 92: the refetched binding is
        the 7d window, quoted against 92."""
        from tests.test_autoswitch_balanced import _u

        h = self._harness(temp_home, "pro")
        outcome = self._two_phase(
            h,
            {"1": _u(20, 91, 100), "2": _u(10, 10, 20)},
            {"1": _u(20, 91, 100), "2": _u(10, 95, 20)},
        )
        assert outcome is TickOutcome.NO_ACTION
        assert self._details(h) == ["7d 91% < 92%"]


class TestBalancedReadsEachAccountsWeeklyLineMap:
    """The balanced builders take the whole `WeeklyLineMap`. General weekly
    97; the named account has its own weekly line."""

    def test_the_tick_trigger_reads_the_active_weekly_line(self, temp_home):
        """Active 1 at 7d 93 over its own line 92: the trigger is forced
        (`proactive`), and with no OAuth peer the tick is `no-candidates`.
        On the line 97 the trigger would be `balanced`, a below-threshold
        hold."""
        from tests.test_autoswitch_balanced import _balanced, _u

        h = _balanced(temp_home, plans={1: "pro", 2: "pro"}, weekly_threshold=97.0)
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        assert h.switcher.write_account_line("1", "weeklyThreshold", 92)["ok"]
        h.engine = h._make_engine()
        outcome = h.tick_with_usage({"1": _u(20, 93, 100), "2": "api key"})
        assert outcome is TickOutcome.BLOCKED
        assert _reasons(h) == ["no-candidates"]

    def test_the_decision_reads_the_active_weekly_line(self, temp_home):
        """Active 1 at 7d 93 over its own line 92, no usable candidate (both
        over the 5h line): the decision is at the line, so the engine reports
        `no-qualifying-candidate`, not a below-threshold hold."""
        from tests.test_autoswitch_balanced import _balanced, _u

        h = _balanced(
            temp_home, plans={1: "pro", 2: "pro", 3: "pro"}, weekly_threshold=97.0
        )
        assert h.switcher.write_account_line("1", "weeklyThreshold", 92)["ok"]
        h.engine = h._make_engine()
        outcome = h.tick_with_usage({
            "1": _u(20, 93, 100), "2": _u(95, 10, 100), "3": _u(95, 10, 100),
        })
        assert outcome is TickOutcome.BLOCKED
        assert _reasons(h) == ["no-qualifying-candidate"]

    def test_a_candidate_over_its_weekly_line_is_blocked(self, temp_home):
        """Candidate 2 at 7d 82 over its own line 80 is `blocked`, so it is
        left out of the line height `f`. The ranking is then (3, 1); with
        account 2 counted in `f` (not blocked, on the line 97) it is (1, 3)."""
        from tests.test_autoswitch_balanced import _balanced, _u

        h = _balanced(
            temp_home, plans={1: "pro", 2: "pro", 3: "pro"}, weekly_threshold=97.0
        )
        assert h.switcher.write_account_line("2", "weeklyThreshold", 80)["ok"]
        h.engine = h._make_engine()
        h.tick_with_usage({
            "1": _u(20, 10, 30), "2": _u(10, 82, 20), "3": _u(10, 0, 100),
        })
        assert h.engine.balanced_rank == (3, 1)


class TestPollCadenceWeeklyInput:
    def test_no_account_key_pins_the_general_weekly_line(self, temp_home):
        h = _s3_fleet(temp_home, weekly_key=None, weekly_threshold=97.0)
        assert h.switcher._poll_inputs_override[1] == 97.0

    def test_no_account_key_and_no_general_weekly_line_pins_none(self, temp_home):
        h = _s3_fleet(temp_home, weekly_key=None)
        assert h.switcher._poll_inputs_override[1] is None

    def test_an_account_key_pins_the_lowest_effective_weekly_line(self, temp_home):
        h = _s3_fleet(temp_home, weekly_key=92.0, weekly_threshold=97.0)
        assert h.switcher._poll_inputs_override[1] == 92.0

    def test_accounts_without_a_weekly_line_count_with_their_5h_line(self, temp_home):
        h = _s3_fleet(temp_home, weekly_key=95.0)
        assert h.switcher._poll_inputs_override[1] == 90.0

    def test_an_accounts_own_5h_line_counts_when_it_has_no_weekly_line(self, temp_home):
        """N1. Account 2 weekly 95; account 3 has its own 5-hour line 80 and
        no weekly line, so its effective weekly line is 80: cadence pins 80."""
        h = _s3_fleet(temp_home, weekly_key=95.0)
        assert h.switcher.write_account_line("3", "threshold", 80)["ok"]
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[1] == 80.0

    def test_the_general_line_is_not_in_the_minimum(self, temp_home):
        """P5. Every account has its own weekly line (92, 95); the general
        weekly line 60 belongs to no account: cadence pins 92."""
        h = _s3_fleet(temp_home, weekly_key=92.0, weekly_threshold=60.0)
        assert h.switcher.write_account_line("3", "weeklyThreshold", 95)["ok"]
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[1] == 92.0

    def test_a_disabled_account_does_not_drag_the_minimum_down(self, temp_home):
        h = _s3_fleet(temp_home, weekly_key=92.0, weekly_threshold=97.0)
        assert h.switcher.write_account_line("3", "weeklyThreshold", 60)["ok"]
        h.switcher.set_account_disabled("3", True)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[1] == 92.0

    def test_no_switchable_account_pins_the_general_line(self, temp_home):
        h = _s3_fleet(temp_home, weekly_key=92.0, weekly_threshold=97.0)
        h.switcher.set_account_disabled("2", True)
        h.switcher.set_account_disabled("3", True)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[1] == 97.0

    def test_apply_threshold_repins_the_weekly_input(self, temp_home):
        """P2. Account 2 weekly 92, general 97: after `apply_threshold(85)`
        the 5-hour input is 85 and the weekly input stays 92."""
        h = _s3_fleet(temp_home, weekly_key=92.0, weekly_threshold=97.0)
        h.switcher.set_poll_policy_inputs(90.0, None, ())
        h.engine.apply_threshold(85.0)
        assert h.switcher._poll_inputs_override[:2] == (85.0, 92.0)

    def test_apply_threshold_moves_the_5h_fallback_of_the_weekly_input(self, temp_home):
        """General weekly unset: account 3 has no line, so its weekly windows
        use the session 5-hour line. After `apply_threshold(85)` the weekly
        input is min(92, 85) = 85."""
        h = _s3_fleet(temp_home, weekly_key=92.0)
        assert h.switcher._poll_inputs_override[1] == 90.0
        h.engine.apply_threshold(85.0)
        assert h.switcher._poll_inputs_override[1] == 85.0

    def test_a_tick_reads_the_account_policies_once(self, temp_home):
        """SL2: one store read per tick resolves both lines for every
        account; the cadence pin reuses the tick's maps."""
        from unittest.mock import patch

        h = _s3_fleet(temp_home, weekly_key=92.0, weekly_threshold=97.0)
        real = h.switcher.account_policies
        with patch.object(h.switcher, "account_policies", side_effect=real) as reads:
            h.tick_with_usage({"2": _usage7(10.0, 20.0), "3": _usage7(0.0, 20.0)})
        assert reads.call_count == 1
        assert h.switcher._poll_inputs_override[1] == 92.0

    def test_a_tick_repins_after_a_store_change(self, temp_home):
        h = _s3_fleet(temp_home, weekly_key=None, weekly_threshold=97.0)
        assert h.switcher.write_account_line("3", "weeklyThreshold", 93)["ok"]
        h.tick_with_usage({"2": _usage7(10.0, 20.0), "3": _usage7(0.0, 20.0)})
        assert h.switcher._poll_inputs_override[1] == 93.0


# ---------------------------------------------------------------------------
# Balanced strategy
# ---------------------------------------------------------------------------


def test_balanced_uses_account_weekly_line(temp_home):
    """Account 2 at 7d 87 under its own weekly line 95 (general 90): usable,
    so it has a rank. Account 3 at 7d 87 with no line is not usable: a
    candidate must sit `balancedMargin` (5) points under its line, 85."""
    h = EngineHarness(temp_home, strategy="balanced")
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    assert h.switcher.write_account_line("2", "weeklyThreshold", 95)["ok"]
    h.engine = h._make_engine()
    h.tick_with_usage({
        "1": _usage7(10, 50, R_LATER),
        "2": _usage7(10, 87, R_LATER),
        "3": _usage7(10, 87, R_LATER),
    })
    assert 2 in h.engine.balanced_rank
    assert 3 not in h.engine.balanced_rank


def test_balanced_uses_account_five_line(temp_home):
    """Active account 1 has its own 5-hour line 80 and sits at 5h 85: it is
    not usable in balanced, so it has no rank. On the general line 90 it
    would be usable (85 < 90; `balancedMargin` 0)."""
    h = EngineHarness(temp_home, strategy="balanced", balanced_margin=0.0)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    assert h.switcher.write_account_line("1", "threshold", 80)["ok"]
    h.engine = h._make_engine()
    h.tick_with_usage({
        "1": _usage7(85, 10, R_LATER),
        "2": _usage7(10, 20, R_LATER),
        "3": _usage7(10, 30, R_LATER),
    })
    assert 1 not in h.engine.balanced_rank
    assert 2 in h.engine.balanced_rank and 3 in h.engine.balanced_rank


class TestBalanceModuleTakesPerAccountLines:
    NOW = 1_000_000.0

    def _acc(self, id_, pct7):
        reset = self.NOW + 24 * 3600.0
        return balance.Account(
            id_, "pro", balance.Window(10.0, None), balance.Window(pct7, reset)
        )

    def test_score_account_uses_the_accounts_own_weekly_line(self):
        accounts = [self._acc(1, 50.0), self._acc(2, 92.0), self._acc(3, 92.0)]
        own_lines = {2: (90.0, 95.0)}
        assert balance.rank(accounts, self.NOW, threshold=90.0, lines=own_lines) == [1, 2]
        assert balance.rank(accounts, self.NOW, threshold=90.0) == [1]

    def test_no_lines_mapping_is_todays_result(self):
        accounts = [self._acc(1, 50.0), self._acc(2, 92.0)]
        for weekly in (None, 95.0):
            assert balance.rank(
                accounts, self.NOW, threshold=90.0, weekly_threshold=weekly, lines={}
            ) == balance.rank(
                accounts, self.NOW, threshold=90.0, weekly_threshold=weekly
            )

    def test_decide_forces_out_an_active_over_its_own_weekly_line(self):
        accounts = [self._acc(1, 93.0), self._acc(2, 10.0)]
        out = balance.decide(
            accounts, 1, self.NOW, None, None, False, threshold=90.0,
            weekly_threshold=97.0, lines={1: (90.0, 92.0)},
        )
        assert out == balance.SwitchTo(2, "threshold")
        # Without `lines` the active account is usable (93 < 97), so the
        # decision is the proactive path: account 2's deficit is far larger.
        out = balance.decide(
            accounts, 1, self.NOW, None, None, False, threshold=90.0,
            weekly_threshold=97.0,
        )
        assert out == balance.SwitchTo(2, "proactive")

    def test_runway_reads_each_accounts_own_five_hour_line(self):
        """Runway rule with a burn: need = min(burn * MIN_RUNWAY_S, active
        room) = min(20, 40) = 20 Pro points. Account 2 sits at 5h 55 under
        its own 5-hour line 60: room 5, so it is no target and the decision
        is Stay. On the general line 90 its room would be 35."""
        reset = self.NOW + 24 * 3600.0
        accounts = [
            balance.Account(1, "pro", balance.Window(50.0, None), balance.Window(50.0, reset)),
            balance.Account(2, "pro", balance.Window(55.0, None), balance.Window(0.0, reset)),
        ]
        burn = 20.0 / balance.MIN_RUNWAY_S
        kw = dict(threshold=90.0, burn_rate=burn)
        assert balance.decide(accounts, 1, self.NOW, None, None, False, **kw) == (
            balance.SwitchTo(2, "proactive")
        )
        assert balance.decide(
            accounts, 1, self.NOW, None, None, False, lines={2: (60.0, None)}, **kw
        ) == balance.Stay()


# ---------------------------------------------------------------------------
# CLI: cswap threshold ... --weekly
# ---------------------------------------------------------------------------


class TestCliThresholdWeekly(PolicyCliBase):
    def test_cli_threshold_weekly(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)

        # set
        self._threshold(["2", "92", "--weekly"])
        record = self._record(ClaudeAccountSwitcher(), "2")
        assert record["weeklyThreshold"] == 92.0
        assert "threshold" not in record
        out = capsys.readouterr().out
        assert "Account-2" in out and "92" in out

        # list
        self._threshold(["3", "85"])
        capsys.readouterr()
        self._threshold([])
        out = capsys.readouterr().out
        assert out.splitlines() == [
            "Switch-away thresholds:",
            "  global default  90%",
            "  global weekly   –",
            "  Account-2  5h –  weekly 92%",
            "  Account-3  5h 85%  weekly –",
        ]

        # unset
        self._threshold(["2", "--unset", "--weekly"])
        record = self._record(ClaudeAccountSwitcher(), "2")
        assert "weeklyThreshold" not in record
        capsys.readouterr()
        self._threshold([])
        assert "Account-2" not in capsys.readouterr().out

        # error: --weekly with no N
        before = self._bytes(s)
        with pytest.raises(SystemExit) as exc:
            self._threshold(["--weekly"])
        assert exc.value.code == 2
        assert self._bytes(s) == before

    def test_a_bad_weekly_value_exits_1_and_writes_nothing(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        before = self._bytes(s)
        with pytest.raises(SystemExit) as exc:
            self._threshold(["2", "101", "--weekly"])
        assert exc.value.code == 1
        assert self._bytes(s) == before

    def test_weekly_accepts_an_email_and_an_alias(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        self._alias(s, "3", "dev")
        self._threshold(["account-2@example.test", "93", "--weekly"])
        self._threshold(["dev", "94", "--weekly"])
        assert self._record(ClaudeAccountSwitcher(), "2")["weeklyThreshold"] == 93.0
        assert self._record(ClaudeAccountSwitcher(), "3")["weeklyThreshold"] == 94.0

    def test_unset_weekly_without_a_line_is_a_no_op(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        before = self._bytes(s)
        self._threshold(["2", "--unset", "--weekly"])
        assert self._bytes(s) == before
        assert capsys.readouterr().out.strip() != ""


# ---------------------------------------------------------------------------
# TUI: each account's weekly line where one account's is passed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTuiUsesTheAccountWeeklyLine:
    async def test_next_best_quotes_the_account_weekly_line(self, tmp_path, fake_engine):
        import dataclasses

        from textual.widgets import Static

        from tests.test_tui import FakeSwitcher, make_account, make_app, make_entry, settle

        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1, "autoswitch": {"threshold": 90},
        }))
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                dataclasses.replace(
                    make_account(2, entry=make_entry(5.0, 93.0)),
                    policy=AccountPolicy(weekly_threshold=95.0),
                ),
                make_account(3, entry=make_entry(50.0, 10.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("g")
            await pilot.pause()
            await settle(pilot)
            plain = app.screen.query_one("#candidates", Static).render().plain
            row = next(ln for ln in plain.splitlines() if "user2@example.com" in ln)
            assert "7d  93% used (line 95%)" in row
            row3 = next(ln for ln in plain.splitlines() if "user3@example.com" in ln)
            assert "(line" not in row3

    async def test_the_card_ticks_the_account_weekly_line(self, tmp_path):
        import dataclasses

        from claude_swap.tui.widgets import _BAR_TICK, AccountsPanel
        from tests.test_tui import FakeSwitcher, make_account, make_app, make_entry, settle

        results = {}
        for name, policy in (
            ("own", AccountPolicy(weekly_threshold=97.0)),
            ("none", AccountPolicy()),
        ):
            fake = FakeSwitcher(
                [
                    dataclasses.replace(
                        make_account(1, active=True, entry=make_entry(50.0, 50.0)),
                        policy=policy,
                    ),
                    make_account(2, entry=make_entry(5.0, 10.0)),
                ],
                tmp_path,
            )
            app = make_app(fake)
            async with app.run_test(size=(100, 40)) as pilot:
                await settle(pilot)
                panel = app.screen.query_one(AccountsPanel)
                plain = panel.render().plain
                rows = {
                    ln.split()[0]: ln
                    for ln in plain.splitlines()
                    if ln.strip().startswith(("5h", "7d"))
                }
                results[name] = {k: v.index(_BAR_TICK) for k, v in rows.items()}
        assert results["own"]["7d"] > results["own"]["5h"]
        assert results["none"]["7d"] == results["none"]["5h"]


@pytest.fixture
def fake_engine(monkeypatch):
    from tests.test_tui import _FakeEngine

    _FakeEngine.instances = []
    monkeypatch.setattr("claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine)
    return _FakeEngine
