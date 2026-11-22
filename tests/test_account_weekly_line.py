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
from claude_swap.autoswitch import PollEvent, SwitchEvent, TickOutcome
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
        out = balance.decide(
            accounts, 1, self.NOW, None, None, False, threshold=90.0,
            weekly_threshold=97.0,
        )
        assert not isinstance(out, balance.SwitchTo) or out.reason != "threshold"


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
        assert "  Account-2  5h –  weekly 92%" in out.splitlines()
        assert "  Account-3  5h 85%  weekly –" in out.splitlines()
        assert "Account-1" not in out
        assert "90%" in out

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
