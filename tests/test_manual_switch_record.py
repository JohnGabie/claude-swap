"""S3c: a manual switch is recorded in the auto-switch state file.

Spec: docs/superpowers/specs/2026-11-10-balance-strategy-design.md, item S3c
and "Answers on the tray box, the side panel and manual switches". Project
rule P1: the state file never holds an email or an account name.

Manual call sites (all call ``autoswitch.record_manual_switch``):
``cli.py`` (``--switch``, ``--switch-to``), ``tui/app.py`` (``do_switch``,
``action_switch_best``) and ``menubar.py`` (``run_manual_switch``, used by the
two menu callbacks). ``AutoSwitchEngine._perform`` never calls it.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import autoswitch, cli, menubar
from claude_swap.exceptions import ClaudeSwitchError, LockError
from claude_swap.autoswitch import NoSwitchEvent, TickOutcome
from tests.test_autoswitch import EngineHarness
from tests.test_autoswitch_balanced import (
    PRO_BETTER,
    PRO3_BETTER,
    T0,
    _balanced,
    _reasons,
    _u,
)

STATE = "autoswitch_state.json"
CANARY = "canary.person@example.com"


def _state(root: Path) -> dict:
    return json.loads((root / STATE).read_text(encoding="utf-8"))


def _record(root: Path, now: float = 1234.5) -> None:
    autoswitch.record_manual_switch(root, clock=lambda: now)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


class TestHelper:
    def test_creates_the_file_with_only_the_key(self, tmp_path):
        _record(tmp_path, 1234.5)
        assert _state(tmp_path) == {"lastManualSwitchAt": 1234.5}

    def test_creates_a_missing_folder(self, tmp_path):
        root = tmp_path / "new" / "root"
        _record(root, 5.0)
        assert _state(root) == {"lastManualSwitchAt": 5.0}

    def test_preserves_every_other_key(self, tmp_path):
        before = {
            "schemaVersion": 1,
            "lastSwitchAt": 10.0,
            "lastSwitchFrom": 1,
            "lastSwitchTo": "2",
            "lastBalancedSwitchAt": 20.0,
            "leftTrigger": "balanced",
            "quarantine": {"3": {"email": "q@example.com", "reason": "x"}},
        }
        (tmp_path / STATE).write_text(json.dumps(before))
        _record(tmp_path, 99.0)
        after = _state(tmp_path)
        assert after.pop("lastManualSwitchAt") == 99.0
        assert after == before

    def test_overwrites_an_older_value(self, tmp_path):
        _record(tmp_path, 1.0)
        _record(tmp_path, 2.0)
        assert _state(tmp_path) == {"lastManualSwitchAt": 2.0}

    def test_writes_a_finite_float_from_the_clock(self, tmp_path):
        autoswitch.record_manual_switch(tmp_path, clock=lambda: 7)
        value = _state(tmp_path)["lastManualSwitchAt"]
        assert isinstance(value, float) and math.isfinite(value) and value == 7.0

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "x", None])
    def test_a_bad_clock_value_writes_nothing(self, tmp_path, bad):
        autoswitch.record_manual_switch(tmp_path, clock=lambda: bad)
        assert not (tmp_path / STATE).exists()

    def test_uses_time_time_by_default(self, tmp_path):
        import time

        low = time.time()
        autoswitch.record_manual_switch(tmp_path)
        assert low <= _state(tmp_path)["lastManualSwitchAt"] <= time.time()

    def test_reads_a_file_of_the_previous_version(self, tmp_path):
        """No key yet, and a junk value: both are replaced or kept safely."""
        (tmp_path / STATE).write_text(json.dumps({"lastSwitchAt": 3.0}))
        _record(tmp_path, 8.0)
        assert _state(tmp_path) == {"lastSwitchAt": 3.0, "lastManualSwitchAt": 8.0}
        (tmp_path / STATE).write_text(json.dumps({"lastManualSwitchAt": "junk"}))
        _record(tmp_path, 9.0)
        assert _state(tmp_path) == {"lastManualSwitchAt": 9.0}

    def test_swallows_a_corrupt_file_without_output(self, tmp_path, capsys):
        (tmp_path / STATE).write_text("{not json")
        _record(tmp_path, 3.0)  # must not raise
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""
        # A corrupt file reads as empty state, like the engine reads it.
        assert _state(tmp_path) == {"lastManualSwitchAt": 3.0}

    def test_swallows_a_non_object_file(self, tmp_path, capsys):
        (tmp_path / STATE).write_text("[1, 2]")
        _record(tmp_path, 3.0)
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    @pytest.mark.skipif(
        sys.platform == "win32" or (hasattr(__import__("os"), "geteuid")
                                    and __import__("os").geteuid() == 0),
        reason="needs POSIX permissions and a non-root user",
    )
    def test_swallows_a_read_only_folder(self, tmp_path, capsys):
        root = tmp_path / "ro"
        root.mkdir()
        root.chmod(0o500)
        try:
            _record(root, 3.0)  # must not raise
        finally:
            root.chmod(0o700)
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""
        assert not (root / STATE).exists()

    def test_swallows_a_write_error(self, tmp_path, capsys):
        with patch("claude_swap.autoswitch.atomic_write_json",
                   side_effect=OSError("disk full")):
            _record(tmp_path, 3.0)
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    def test_swallows_a_lock_failure(self, tmp_path, capsys):
        class Busy:
            def __init__(self, *a, **k): ...
            def __enter__(self):
                raise LockError("busy")
            def __exit__(self, *a): ...

        with patch("claude_swap.autoswitch.FileLock", Busy):
            _record(tmp_path, 3.0)
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""
        assert not (tmp_path / STATE).exists()

    def test_swallows_a_bad_root(self, capsys):
        autoswitch.record_manual_switch(object())  # type: ignore[arg-type]
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    def test_a_non_path_root_writes_nothing(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.chdir(tmp_path)
        autoswitch.record_manual_switch(MagicMock())
        assert list(tmp_path.iterdir()) == []

    def test_never_writes_an_email(self, tmp_path):
        before = {"lastSwitchAt": 1.0, "lastSwitchTo": "2"}
        (tmp_path / STATE).write_text(json.dumps(before))
        _record(tmp_path)
        raw = (tmp_path / STATE).read_text()
        assert CANARY not in raw and "@" not in raw

    def test_takes_the_state_lock(self, tmp_path):
        """The record waits for a holder of the engine's state lock."""
        from claude_swap.locking import FileLock

        lock = FileLock(tmp_path / ".autoswitch_state.lock")
        done = threading.Event()
        with lock:
            t = threading.Thread(
                target=lambda: (_record(tmp_path, 5.0), done.set())
            )
            t.start()
            assert not done.wait(0.4)  # blocked by the held lock
        t.join(5)
        assert done.is_set()
        assert _state(tmp_path) == {"lastManualSwitchAt": 5.0}

    def test_lock_timeout_is_short(self, tmp_path):
        """A stuck lock must not freeze the user's switch for 10 s."""
        seen = {}

        class Spy:
            def __init__(self, path, timeout=10.0):
                seen["timeout"] = timeout
            def __enter__(self):
                raise LockError("busy")
            def __exit__(self, *a): ...

        with patch("claude_swap.autoswitch.FileLock", Spy):
            _record(tmp_path)
        assert seen["timeout"] <= 3.0


class TestConcurrency:
    def test_engine_write_and_manual_record_keep_each_other(self, temp_home):
        h = EngineHarness(temp_home)
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=11.0))
        autoswitch.record_manual_switch(h.switcher.backup_dir, clock=lambda: 12.0)
        h.engine._mutate_state(lambda s: s.update(lastBalancedSwitchAt=13.0))
        st = h.state()
        assert st["lastSwitchAt"] == 11.0
        assert st["lastManualSwitchAt"] == 12.0
        assert st["lastBalancedSwitchAt"] == 13.0

    def test_thread_race_loses_no_key(self, tmp_path):
        from claude_swap.autoswitch import _state_ts  # noqa: F401  (import check)

        n = 12
        barrier = threading.Barrier(n)

        def manual():
            barrier.wait()
            _record(tmp_path, 50.0)

        def writer(i):
            def run():
                barrier.wait()
                eng.state_path = tmp_path / STATE
                eng._mutate_state(lambda s: s.update({f"k{i}": i}))
            return run

        eng = _Mini(tmp_path)
        threads = [threading.Thread(target=manual)] + [
            threading.Thread(target=writer(i)) for i in range(n - 1)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        st = _state(tmp_path)
        assert st["lastManualSwitchAt"] == 50.0
        assert all(st[f"k{i}"] == i for i in range(n - 1))


class _Mini:
    """The state file part of the engine, without a switcher."""

    state_path: Path

    def __init__(self, root: Path):
        self.state_path = root / STATE

    _state_lock = autoswitch.AutoSwitchEngine._state_lock
    _read_state = autoswitch.AutoSwitchEngine._read_state
    _mutate_state = autoswitch.AutoSwitchEngine._mutate_state


# ---------------------------------------------------------------------------
# The engine stays out
# ---------------------------------------------------------------------------


class TestEngineIsNotManual:
    def test_engine_switch_leaves_the_key_unchanged(self, temp_home):
        h = _balanced(temp_home)
        h.engine._mutate_state(lambda s: s.update(lastManualSwitchAt=T0 - 5000))
        assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
        assert h.state()["lastManualSwitchAt"] == T0 - 5000

    def test_engine_switch_does_not_create_the_key(self, temp_home):
        h = _balanced(temp_home)
        assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
        assert "lastManualSwitchAt" not in h.state()

    def test_perform_never_calls_the_helper(self, temp_home):
        h = _balanced(temp_home)
        with patch("claude_swap.autoswitch.record_manual_switch") as rec:
            assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
        rec.assert_not_called()
        src = inspect.getsource(autoswitch.AutoSwitchEngine)
        assert "record_manual_switch" not in src

    def test_dry_run_engine_does_not_record(self, temp_home):
        h = _balanced(temp_home)
        h.engine.dry_run = True
        with patch("claude_swap.autoswitch.record_manual_switch") as rec:
            h.tick_with_usage(PRO_BETTER)
        rec.assert_not_called()
        assert "lastManualSwitchAt" not in h.state()


# ---------------------------------------------------------------------------
# The balanced cooldown, with a recorded manual switch
# ---------------------------------------------------------------------------


class TestCooldownAfterManualSwitch:
    def test_proactive_switch_is_held_100s_after_a_manual_switch(self, temp_home):
        h = _balanced(temp_home)
        autoswitch.record_manual_switch(
            h.switcher.backup_dir, clock=lambda: h.clock.now - 100
        )
        outcome = h.tick_with_usage(PRO_BETTER)
        assert outcome is TickOutcome.NO_ACTION
        [ev] = [e for e in h.events if isinstance(e, NoSwitchEvent)]
        assert ev.reason == "cooldown"
        assert _reasons(h) == ["cooldown"]
        assert h.active_number() == 1

    def test_proactive_switch_runs_after_the_cooldown(self, temp_home):
        h = _balanced(temp_home, balanced_cooldown_seconds=900.0)
        autoswitch.record_manual_switch(
            h.switcher.backup_dir, clock=lambda: h.clock.now - 901
        )
        assert h.tick_with_usage(PRO_BETTER) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_a_forced_switch_runs_inside_the_cooldown(self, temp_home):
        h = _balanced(temp_home)
        autoswitch.record_manual_switch(
            h.switcher.backup_dir, clock=lambda: h.clock.now - 100
        )
        usage = dict(PRO_BETTER, **{"1": _u(100, 40, 100)})  # active at limit
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.active_number() != 1

    def test_the_key_works_even_when_the_strategy_was_not_balanced(self, temp_home):
        h = _balanced(temp_home, strategy="best")
        autoswitch.record_manual_switch(
            h.switcher.backup_dir, clock=lambda: h.clock.now - 100
        )
        assert h.state()["lastManualSwitchAt"] == h.clock.now - 100


# ---------------------------------------------------------------------------
# Call sites: CLI
# ---------------------------------------------------------------------------


class FakeSwitcher:
    """Stand-in for ``ClaudeAccountSwitcher`` on the CLI path."""

    def __init__(self, backup_dir: Path, *, result="switched", json_mode=False):
        self.backup_dir = backup_dir
        self.active = 1
        self.result = result  # "switched" | "noop" | "error"
        self.calls: list[tuple] = []

    def _get_sequence_data(self):
        return {"activeAccountNumber": self.active, "accounts": {}}

    def _do(self, name, json_output, target):
        self.calls.append((name,))
        if self.result == "error":
            raise ClaudeSwitchError("nope")
        moved = self.result == "switched"
        old = self.active
        if moved:
            self.active = target
        if not json_output:
            return None
        return {
            "switched": moved,
            "from": {"number": old, "email": ""},
            "to": {"number": self.active, "email": ""},
            "reason": "switched" if moved else "already-active",
        }

    def switch(self, strategy=None, json_output=False, models=(), model_source=None):
        return self._do("switch", json_output, 2)

    def switch_to(self, identifier, json_output=False, force=False):
        return self._do("switch_to", json_output, int(identifier))


def _run_cli(tmp_path: Path, argv: list[str], **kw) -> FakeSwitcher:
    fake = FakeSwitcher(tmp_path, **kw)
    with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=fake), \
         patch.object(sys, "argv", ["claude-swap", *argv]), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch("claude_swap.update_check.check_for_update", return_value=None):
        try:
            cli.main()
        except SystemExit:
            pass
    return fake


def _recorded(root: Path) -> bool:
    p = root / STATE
    return p.exists() and "lastManualSwitchAt" in json.loads(p.read_text())


class TestCliCallSite:
    @pytest.mark.parametrize("argv", [
        ["--switch"], ["--switch", "--json"],
        ["--switch-to", "2"], ["--switch-to", "2", "--json"],
    ])
    def test_records_after_a_successful_switch(self, tmp_path, argv):
        _run_cli(tmp_path, argv)
        assert _recorded(tmp_path)
        st = _state(tmp_path)
        assert isinstance(st["lastManualSwitchAt"], float)

    @pytest.mark.parametrize("argv", [
        ["--switch"], ["--switch", "--json"],
        ["--switch-to", "2"], ["--switch-to", "2", "--json"],
    ])
    def test_does_not_record_a_no_op(self, tmp_path, argv):
        _run_cli(tmp_path, argv, result="noop")
        assert not _recorded(tmp_path)

    @pytest.mark.parametrize("argv", [
        ["--switch"], ["--switch", "--json"],
        ["--switch-to", "2"], ["--switch-to", "2", "--json"],
    ])
    def test_does_not_record_a_refused_switch(self, tmp_path, argv):
        _run_cli(tmp_path, argv, result="error")
        assert not _recorded(tmp_path)

    def test_a_failing_record_does_not_fail_the_switch(self, tmp_path, capsys):
        with patch("claude_swap.cli.record_manual_switch",
                   side_effect=RuntimeError("boom"), create=True), \
             patch("claude_swap.autoswitch.record_manual_switch",
                   side_effect=RuntimeError("boom")):
            fake = _run_cli(tmp_path, ["--switch-to", "2"])
        assert fake.calls == [("switch_to",)]
        assert "boom" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Call sites: TUI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTuiCallSite:
    async def _drive(self, tmp_path, action, *, switched=True, raises=False):
        from tests.test_tui import FakeSwitcher as TuiFake, make_account, make_app, settle

        fake = TuiFake([make_account(1, active=True), make_account(2)], tmp_path)
        if not switched:
            fake.switch_to = lambda *a, **k: (
                fake.calls.append(("switch_to", "2"))
                or {"switched": False, "reason": "already-active"}
            )
        if raises:
            def boom(*a, **k):
                raise ClaudeSwitchError("refused")
            fake.switch_to = boom
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            action(app)
            await settle(pilot)
        return fake

    async def test_switch_to_records_after_success(self, tmp_path):
        await self._drive(tmp_path, lambda app: app.do_switch("2"))
        assert _recorded(tmp_path)

    async def test_switch_to_does_not_record_a_no_op(self, tmp_path):
        await self._drive(tmp_path, lambda app: app.do_switch("2"), switched=False)
        assert not _recorded(tmp_path)

    async def test_switch_to_does_not_record_a_refusal(self, tmp_path):
        await self._drive(tmp_path, lambda app: app.do_switch("2"), raises=True)
        assert not _recorded(tmp_path)

    async def test_switch_best_records_after_success(self, tmp_path):
        from tests.test_tui import FakeSwitcher as TuiFake, make_account, make_app, settle

        fake = TuiFake([make_account(1, active=True), make_account(2)], tmp_path)
        fake.switch = lambda strategy=None, json_output=False: {
            "switched": True, "from": None, "to": {"number": 2, "email": ""},
            "reason": "switched",
        }
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.action_switch_best()
            await settle(pilot)
        assert _recorded(tmp_path)

    async def test_switch_best_does_not_record_when_it_stays(self, tmp_path):
        # FakeSwitcher.switch returns switched=False.
        await self._drive(tmp_path, lambda app: app.action_switch_best())
        assert not _recorded(tmp_path)


# ---------------------------------------------------------------------------
# Call sites: menu bar (the app class needs rumps, so the callbacks call the
# module-level `run_manual_switch`, tested here)
# ---------------------------------------------------------------------------


class _MenuSwitcher:
    def __init__(self, backup_dir: Path, payload=None, error=None):
        self.backup_dir = backup_dir
        self.payload = payload
        self.error = error
        self.seen: list[dict] = []

    def switch_to(self, identifier, **kw):
        self.seen.append(kw)
        if self.error:
            raise self.error
        return self.payload

    def switch(self, strategy=None, **kw):
        self.seen.append(kw)
        if self.error:
            raise self.error
        return self.payload


class TestMenubarCallSite:
    def test_records_after_a_successful_switch_to(self, tmp_path):
        sw = _MenuSwitcher(tmp_path, {"switched": True})
        menubar.run_manual_switch(sw, lambda **kw: sw.switch_to("2", **kw))
        assert _recorded(tmp_path)
        assert sw.seen == [{"json_output": True}]

    def test_records_after_a_successful_rotation(self, tmp_path):
        sw = _MenuSwitcher(tmp_path, {"switched": True})
        menubar.run_manual_switch(sw, lambda **kw: sw.switch(strategy="best", **kw))
        assert _recorded(tmp_path)

    def test_does_not_record_a_no_op(self, tmp_path):
        sw = _MenuSwitcher(tmp_path, {"switched": False})
        menubar.run_manual_switch(sw, lambda **kw: sw.switch_to("2", **kw))
        assert not _recorded(tmp_path)

    def test_does_not_record_when_the_switch_raises(self, tmp_path):
        sw = _MenuSwitcher(tmp_path, error=ClaudeSwitchError("refused"))
        with pytest.raises(ClaudeSwitchError):
            menubar.run_manual_switch(sw, lambda **kw: sw.switch_to("2", **kw))
        assert not _recorded(tmp_path)

    def test_does_not_record_a_missing_payload(self, tmp_path):
        sw = _MenuSwitcher(tmp_path, None)
        menubar.run_manual_switch(sw, lambda **kw: sw.switch_to("2", **kw))
        assert not _recorded(tmp_path)

    def test_both_menu_callbacks_use_the_helper(self):
        """MenuBarApp needs rumps (macOS): pin the wiring in its source."""
        src = inspect.getsource(menubar.run)
        callbacks = re.findall(r"run_manual_switch\(", src)
        assert len(callbacks) == 2
        assert "self.switcher.switch_to(str(num))" not in src
        assert "self.switcher.switch(strategy=strategy)" not in src


# ---------------------------------------------------------------------------
# Rejected call sites
# ---------------------------------------------------------------------------


def test_no_other_source_calls_a_switch_for_a_user_action():
    """Evidence for the rejected sites: the only callers of the switcher's
    ``switch`` or ``switch_to`` are cli, tui/app, menubar and the engine."""
    root = Path(autoswitch.__file__).parent
    pattern = re.compile(r"\.(switch_to|switch)\b(?!_)")
    found = set()
    for path in root.rglob("*.py"):
        if path.name == "switcher.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"(switcher|self\.switcher)\.(switch_to|switch)\(|"
                     r"switcher\.(switch_to|switch),", text):
            found.add(path.relative_to(root).as_posix())
    assert found == {"autoswitch.py", "cli.py", "menubar.py", "tui/app.py"}
