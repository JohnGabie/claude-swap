"""CLI tests for ``cswap pace`` — the read-only usage feed for guilda.

Spec: docs/superpowers/specs/2026-11-10-pace-command-design.md, scenarios
1, 2, 3, 7 and 9 at the CLI level, and Security requirements 4 to 8.

Every run goes through ``claude_swap.cli.main`` with stdout and stderr
captured at the file-descriptor level (``capfd``), so a write that bypasses
``sys.stdout``/``sys.stderr`` is caught too. Every test runs on fake
credentials under ``tmp_path`` (``temp_home`` sets ``HOME``); the autouse
conftest fixtures stub the Keychain and the OAuth profile fetch, and
``no_network`` refuses every socket connection.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import stat
import sys
from datetime import datetime
from pathlib import Path

import pytest

from claude_swap import cli, pace_feed, paths
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.test_pace_feed import (  # noqa: F401  (no_network is a fixture)
    EMAILS,
    Fleet,
    _access,
    _canaries,
    _refresh,
    _seed_healed_dead_token_row,
    _seed_legacy_record,
    no_network,
)

UNAVAILABLE_LINE = '{"schemaVersion":1,"error":"unavailable"}'

ACCOUNT_KEYS = {
    "id", "alias", "plan", "status", "error", "fetchedAt", "fiveHour", "sevenDay",
}
WINDOW_KEYS = {"pct", "resetsAt"}
ERROR_WORDS = {"api_key", "token_expired", "relogin_needed", "no_credentials", "unavailable"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_pace(monkeypatch, *args: str) -> int:
    """Run ``cswap <args>`` through ``cli.main`` and return the exit code."""
    monkeypatch.setattr(sys, "argv", ["cswap", *args])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    code = exc.value.code
    return 0 if code is None else code


def tree_state(root: Path) -> dict[str, tuple]:
    """Every entry under ``root``: type, mode and content hash.

    A created, removed, moved or changed file or folder changes this map.
    """
    state: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            state[rel] = ("dir", stat.S_IMODE(info.st_mode))
        elif stat.S_ISLNK(info.st_mode):
            state[rel] = ("link", str(path.readlink()))
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            state[rel] = ("file", stat.S_IMODE(info.st_mode), digest)
    return state


def assert_no_leak(text: str, home: Path) -> None:
    for value in _canaries(home):
        assert value not in text, f"identity or secret leaked: {value!r}"
    assert "Traceback" not in text


def parse_one_document(out: str) -> dict:
    """stdout holds exactly one JSON document and nothing else."""
    assert out.endswith("\n")
    assert out.count("\n") == 1, out
    return json.loads(out)


def assert_contract(payload: dict) -> None:
    assert set(payload) == {"schemaVersion", "accounts"}
    assert payload["schemaVersion"] == 1
    for account in payload["accounts"]:
        assert set(account) <= ACCOUNT_KEYS, account
        assert isinstance(account["id"], int)
        if account["status"] in {"ok", "stale"}:
            assert "error" not in account
        else:
            assert account["error"] == account["status"]
            assert account["error"] in ERROR_WORDS
            assert not {"fiveHour", "sevenDay", "fetchedAt"} & set(account)
        for key in ("fiveHour", "sevenDay"):
            if key in account:
                assert set(account[key]) <= WINDOW_KEYS


def leak_message(home: Path) -> str:
    return (
        f"failed for {EMAILS['1']} token={_access('1')} "
        f"refresh={_refresh('1')} at {home}/.claude/.credentials.json"
    )


@pytest.fixture
def fleet(temp_home: Path, no_network) -> Fleet:
    return Fleet(temp_home)


# ---------------------------------------------------------------------------
# Scenario 1 — Full read
# ---------------------------------------------------------------------------


class TestFullRead:
    def test_three_accounts_exit_0(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed()
        fleet.record_usage()
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 0
        assert err == ""
        payload = parse_one_document(out)
        assert_contract(payload)
        by_id = {a["id"]: a for a in payload["accounts"]}
        assert sorted(by_id) == [1, 2, 3]
        for account in by_id.values():
            assert account["status"] == "ok"
            assert {"plan", "fiveHour", "sevenDay", "fetchedAt"} <= set(account)
            assert account["fiveHour"]["pct"] == 41.0
            assert account["sevenDay"]["pct"] == 12.5
        assert by_id[1]["plan"] == "max"
        assert by_id[2]["plan"] == "pro"
        assert by_id[1]["alias"] == "work"

    def test_plain_pace_prints_the_same_json(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed()
        fleet.record_usage()
        capfd.readouterr()

        code_json = run_pace(monkeypatch, "pace", "--json")
        out_json, _ = capfd.readouterr()
        code_plain = run_pace(monkeypatch, "pace")
        out_plain, err_plain = capfd.readouterr()

        assert code_plain == code_json == 0
        assert err_plain == ""
        plain = parse_one_document(out_plain)
        assert_contract(plain)
        assert plain == json.loads(out_json)

    def test_zero_accounts_exit_0_empty_list(self, temp_home, no_network, monkeypatch, capfd):
        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 0
        assert err == ""
        assert parse_one_document(out) == {"schemaVersion": 1, "accounts": []}


# ---------------------------------------------------------------------------
# Scenario 2 — No identity, on stdout and stderr, in exit 0, 3 and 4
# ---------------------------------------------------------------------------


class TestNoIdentity:
    def test_exit_0(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed()
        fleet.record_usage()
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 0
        assert_no_leak(out + err, fleet.home)

    def test_exit_3(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed(missing_backup="2")
        fleet.record_usage(nums=("1", "3"))
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 3
        assert_no_leak(out + err, fleet.home)

    def test_exit_4_every_account_failed(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed()  # no usage stored: every account is unavailable
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 4
        payload = parse_one_document(out)
        assert_contract(payload)
        assert {a["status"] for a in payload["accounts"]} == {"unavailable"}
        assert_no_leak(out + err, fleet.home)

    def test_exit_4_exception_inside_snapshot(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed()
        fleet.record_usage()
        home = fleet.home

        def boom(self, *args, **kwargs):
            raise RuntimeError(leak_message(home))

        monkeypatch.setattr(ClaudeAccountSwitcher, "accounts_snapshot", boom)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 4
        assert out == UNAVAILABLE_LINE + "\n"
        assert err == ""
        assert_no_leak(out + err, home)
        assert "RuntimeError" not in out + err

    def test_exit_4_switcher_construction_fails(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed()
        fleet.record_usage()
        home = fleet.home

        def broken_switcher(*args, **kwargs):
            print(f"stderr noise {EMAILS['2']}", file=sys.stderr)
            raise RuntimeError(leak_message(home))

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", broken_switcher)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 4
        assert out == UNAVAILABLE_LINE + "\n"
        assert err == ""
        assert_no_leak(out + err, home)

    def test_logging_never_reaches_stdout_or_stderr(
        self, fleet: Fleet, monkeypatch, capfd
    ):
        """A record logged during the run, on the cswap logger or on a module
        logger with no handler, and a print to stdout or stderr, never reach
        stdout or stderr. This test passes without the root NullHandler too,
        because the stderr redirect also catches the last-resort handler;
        ``TestLastResortHandler`` proves the NullHandler."""
        fleet.seed()
        fleet.record_usage()
        home = fleet.home
        # pytest attaches capture handlers to the root logger; remove them so
        # the last-resort handler is live, as in a real process.
        monkeypatch.setattr(logging.getLogger(), "handlers", [])
        original = ClaudeAccountSwitcher.accounts_snapshot

        def noisy(self, *args, **kwargs):
            logging.getLogger("claude-swap").error(leak_message(home))
            logging.getLogger("claude_swap.process_detection").error(leak_message(home))
            print(leak_message(home))
            print(leak_message(home), file=sys.stderr)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(ClaudeAccountSwitcher, "accounts_snapshot", noisy)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 0
        assert err == ""
        assert_contract(parse_one_document(out))
        assert_no_leak(out + err, home)


# ---------------------------------------------------------------------------
# Scenario 3 — Partial data
# ---------------------------------------------------------------------------


class TestPartialData:
    def test_one_failed_account_exit_3(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed(missing_backup="2")
        fleet.record_usage(nums=("1", "3"))
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 3
        assert err == ""
        payload = parse_one_document(out)
        assert_contract(payload)
        by_id = {a["id"]: a for a in payload["accounts"]}
        assert sorted(by_id) == [1, 2, 3]
        assert by_id[1]["status"] == "ok"
        assert by_id[3]["status"] == "ok"
        assert by_id[2]["status"] not in {"ok", "stale"}
        assert by_id[2]["error"] == by_id[2]["status"]


# ---------------------------------------------------------------------------
# Scenario 7 — Own exception handler
# ---------------------------------------------------------------------------


class TestOwnExceptionHandler:
    def test_exception_outside_build_pace_gives_envelope(
        self, fleet: Fleet, monkeypatch, capfd
    ):
        fleet.seed()
        fleet.record_usage()
        home = fleet.home

        def boom(switcher):
            raise ValueError(leak_message(home))

        monkeypatch.setattr(pace_feed, "build_pace", boom)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 4
        assert out == UNAVAILABLE_LINE + "\n"
        assert err == ""
        assert "ValueError" not in out + err
        assert_no_leak(out + err, home)

    def test_claude_switch_error_does_not_use_error_envelope(
        self, fleet: Fleet, monkeypatch, capfd
    ):
        from claude_swap.exceptions import ClaudeSwitchError

        fleet.seed()
        home = fleet.home
        envelope_calls: list = []
        monkeypatch.setattr(
            cli, "error_envelope", lambda e: envelope_calls.append(e) or {}
        )

        def boom(*args, **kwargs):
            raise ClaudeSwitchError(leak_message(home))

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", boom)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 4
        assert out == UNAVAILABLE_LINE + "\n"
        assert err == ""
        assert envelope_calls == []
        assert_no_leak(out + err, home)


# ---------------------------------------------------------------------------
# Scenario 9 — Time zones and pct range
# ---------------------------------------------------------------------------


class TestTimeZones:
    def test_every_time_has_a_zone_and_pct_in_range(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed()
        fleet.record_usage()
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, _ = capfd.readouterr()
        assert code == 0
        payload = parse_one_document(out)
        times = 0
        for account in payload["accounts"]:
            parsed = datetime.fromisoformat(account["fetchedAt"])
            assert parsed.tzinfo is not None and parsed.utcoffset() is not None
            times += 1
            for key in ("fiveHour", "sevenDay"):
                window = account[key]
                assert 0.0 <= window["pct"] <= 100.0
                reset = datetime.fromisoformat(window["resetsAt"])
                assert reset.tzinfo is not None and reset.utcoffset() is not None
                times += 1
        assert times == 9


# ---------------------------------------------------------------------------
# Flags: --debug and incompatible flags are rejected by argparse (exit 2)
# ---------------------------------------------------------------------------


class TestFlags:
    @pytest.mark.parametrize(
        "argv",
        [["pace", "--debug"], ["pace", "--json", "--debug"], ["pace", "--debug", "--json"]],
        ids=["debug", "json-debug", "debug-json"],
    )
    def test_debug_is_rejected(self, temp_home, no_network, monkeypatch, capfd, argv):
        code = run_pace(monkeypatch, *argv)

        out, err = capfd.readouterr()
        assert code == 2
        assert out == ""
        assert "pace" in err.splitlines()[0]
        assert "--debug" in err
        assert "Traceback" not in err

    @pytest.mark.parametrize(
        "extra",
        [
            ["--switch"],
            ["--token-status"],
            ["--list"],
            ["--strategy", "best"],
            ["--slot", "2"],
            ["--force"],
            ["2"],
        ],
        ids=["switch", "token-status", "list", "strategy", "slot", "force", "positional"],
    )
    def test_incompatible_flags_are_rejected(
        self, temp_home, no_network, monkeypatch, capfd, extra
    ):
        code = run_pace(monkeypatch, "pace", "--json", *extra)

        out, err = capfd.readouterr()
        assert code == 2
        assert out == ""
        # The error comes from the pace parser, not from the main parser
        # rejecting an unknown "pace" word.
        assert err.splitlines()[0].startswith("usage: cswap pace")
        assert extra[0] in err
        assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Security requirements 1 and 7 at the CLI level — no writes, no network
# ---------------------------------------------------------------------------


class TestNoWrites:
    @pytest.mark.parametrize("scenario", ["exit0", "exit3", "exit4"])
    def test_whole_tree_and_stores_hash_equal(
        self, fleet: Fleet, monkeypatch, capfd, tmp_path, no_network, scenario
    ):
        if scenario == "exit0":
            fleet.seed()
            fleet.record_usage()
            expected = 0
        elif scenario == "exit3":
            fleet.seed(missing_backup="2")
            fleet.record_usage(nums=("1", "3"))
            expected = 3
        else:
            fleet.seed()
            expected = 4
        # Settings and a slot config backup exist too, so the tree covers them.
        (fleet.switcher.backup_dir / "settings.json").write_text(
            json.dumps({"autoswitch": {"threshold": 90}})
        )
        fleet.switcher._write_account_config(
            "3", EMAILS["3"], json.dumps({"oauthAccount": {"emailAddress": EMAILS["3"]}})
        )
        stores_before = fleet.hashes()
        assert any(p.name.endswith(".enc") for p in fleet.store_files())
        tree_before = tree_state(tmp_path)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        capfd.readouterr()
        assert code == expected
        assert fleet.hashes() == stores_before
        assert tree_state(tmp_path) == tree_before
        assert no_network == []

    def test_healed_dead_token_row_and_legacy_record_stay(
        self, fleet: Fleet, monkeypatch, capfd, tmp_path, no_network
    ):
        _seed_healed_dead_token_row(fleet, "2")
        _seed_legacy_record(fleet, "3")
        tree_before = tree_state(tmp_path)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert tree_state(tmp_path) == tree_before
        assert no_network == []
        assert code in {0, 3}
        assert_contract(parse_one_document(out))
        assert_no_leak(out + err, fleet.home)

    def test_pending_legacy_backup_move_is_not_run(
        self, temp_home, monkeypatch, capfd, tmp_path, no_network
    ):
        """Switcher construction moves a legacy ``~/.claude-swap-backup`` to
        the XDG root and prints the path. pace must not run that move."""
        legacy = paths.get_legacy_backup_root()
        target = paths.get_backup_root()
        assert legacy != target
        legacy.mkdir()
        (legacy / "sequence.json").write_text(json.dumps({"accounts": {}}))
        tree_before = tree_state(tmp_path)

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 4
        assert out == UNAVAILABLE_LINE + "\n"
        assert err == ""
        assert tree_state(tmp_path) == tree_before
        assert legacy.exists()
        assert not target.exists()

    def test_interrupted_migration_flag_is_not_removed(
        self, fleet: Fleet, monkeypatch, capfd, tmp_path, no_network
    ):
        fleet.seed()
        fleet.record_usage()
        flag = paths.migration_flag_for(paths.get_backup_root())
        flag.write_text("")
        tree_before = tree_state(tmp_path)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 4
        assert out == UNAVAILABLE_LINE + "\n"
        assert err == ""
        assert flag.exists()
        assert tree_state(tmp_path) == tree_before


# ---------------------------------------------------------------------------
# Migration guard — only the migration of the current platform counts
# ---------------------------------------------------------------------------

#: The migration that ``run_migrations`` records on each platform. Linux and
#: WSL have none: both registered migrations skip there and write nothing.
PLATFORM_MIGRATION = {
    Platform.MACOS: "macos_keyring_to_security",
    Platform.WINDOWS: "windows_keyring_to_files",
}
OTHER_MIGRATION = {
    Platform.MACOS: "windows_keyring_to_files",
    Platform.WINDOWS: "macos_keyring_to_security",
}


def _as_platform(monkeypatch, platform: Platform) -> None:
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: platform))


def _seed_roster(applied: list[str]) -> Path:
    """A readable ``sequence.json`` with no accounts, and ``applied`` recorded
    in ``.migrations.json`` (no state file when ``applied`` is empty)."""
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps({"accounts": {}}))
    if applied:
        (root / ".migrations.json").write_text(
            json.dumps({"version": 1, "applied": {mid: "2026-11-10T00:00:00" for mid in applied}})
        )
    return root


class TestPlatformMigrationGuard:
    @pytest.mark.parametrize("platform", [Platform.MACOS, Platform.WINDOWS], ids=["macos", "windows"])
    def test_recorded_platform_migration_runs_normally(
        self, temp_home, no_network, monkeypatch, capfd, tmp_path, platform
    ):
        """A healthy install records only its own platform's migration (the
        other one returns False and is never recorded). pace runs, exit 0."""
        _as_platform(monkeypatch, platform)
        _seed_roster([PLATFORM_MIGRATION[platform]])
        tree_before = tree_state(tmp_path)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 0
        assert err == ""
        assert parse_one_document(out) == {"schemaVersion": 1, "accounts": []}
        assert tree_state(tmp_path) == tree_before

    @pytest.mark.parametrize("platform", [Platform.MACOS, Platform.WINDOWS], ids=["macos", "windows"])
    def test_pending_platform_migration_is_not_run(
        self, temp_home, no_network, monkeypatch, capfd, tmp_path, platform
    ):
        """Only the other platform's migration is recorded: this platform's
        migration is pending and would write. pace gives exit 4."""
        _as_platform(monkeypatch, platform)
        _seed_roster([OTHER_MIGRATION[platform]])
        tree_before = tree_state(tmp_path)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 4
        assert out == UNAVAILABLE_LINE + "\n"
        assert err == ""
        assert tree_state(tmp_path) == tree_before

    @pytest.mark.parametrize(
        "platform",
        [Platform.MACOS, Platform.WINDOWS, Platform.LINUX, Platform.WSL],
        ids=["macos", "windows", "linux", "wsl"],
    )
    def test_guard_agrees_with_run_migrations(
        self, temp_home, no_network, monkeypatch, platform
    ):
        """The guard says "writes" exactly when the real ``run_migrations``
        writes the migration state, and "no write" after a real run."""
        _as_platform(monkeypatch, platform)
        root = _seed_roster([])
        state = root / ".migrations.json"

        guard_before = cli._pace_construction_writes()
        ClaudeAccountSwitcher()
        assert guard_before == state.exists()

        assert cli._pace_construction_writes() is False
        before = state.read_bytes() if state.exists() else None
        ClaudeAccountSwitcher()
        after = state.read_bytes() if state.exists() else None
        assert after == before


# ---------------------------------------------------------------------------
# Output errors and Ctrl-C
# ---------------------------------------------------------------------------


def run_pace_no_interrupt_escape(monkeypatch, *args: str) -> int:
    """Like ``run_pace``, but a KeyboardInterrupt that escapes ``cli.main``
    fails the test instead of stopping the pytest session."""
    monkeypatch.setattr(sys, "argv", ["cswap", *args])
    try:
        cli.main()
    except SystemExit as exc:
        return 0 if exc.code is None else exc.code
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped cli.main")
    pytest.fail("cli.main returned without sys.exit")


class _FailingStdout(io.TextIOBase):
    """A stdout whose write raises ``exc``; it has no file descriptor."""

    def __init__(self, exc: BaseException):
        self.exc = exc

    def writable(self) -> bool:
        return True

    def write(self, text):
        raise self.exc

    def flush(self):
        raise self.exc


def _finalizer_output(stream) -> list:
    """Finalize ``stream`` and return what Python reports as unraisable.

    A stream that is still open when it is finalized flushes first; a flush
    error there prints "Exception ignored while finalizing file" to stderr.
    """
    import gc

    seen: list = []
    old_hook = sys.unraisablehook
    sys.unraisablehook = seen.append
    try:
        stream.__del__()
        gc.collect()
    finally:
        sys.unraisablehook = old_hook
    return seen


class TestOutputErrors:
    def test_broken_pipe_prints_nothing_and_exits_4(
        self, fleet: Fleet, monkeypatch, capfd
    ):
        """stdout is a real pipe with its reader closed (as in ``| head -c 0``).
        The command exits 4, stderr stays empty, and the pending buffer goes to
        the null device: a later flush (Python flushes stdout at shutdown)
        raises nothing."""
        fleet.seed()
        fleet.record_usage()
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        pipe_stdout = open(write_fd, "w", encoding="utf-8")
        monkeypatch.setattr(sys, "stdout", pipe_stdout)
        capfd.readouterr()

        try:
            code = run_pace_no_interrupt_escape(monkeypatch, "pace", "--json")
            out, err = capfd.readouterr()
            assert code == 4
            assert err == ""
            assert out == ""
            # The shutdown flush: must not raise BrokenPipeError again.
            sys.stdout.flush()
        finally:
            if sys.stdout is not pipe_stdout:
                sys.stdout.close()
            pipe_stdout.close()

    @pytest.mark.parametrize(
        "exc", [BrokenPipeError(32, "Broken pipe"), OSError(5, "Input/output error")],
        ids=["broken-pipe", "oserror"],
    )
    def test_write_error_without_fd_prints_nothing_and_exits_4(
        self, fleet: Fleet, monkeypatch, capfd, exc
    ):
        fleet.seed()
        fleet.record_usage()
        failing = _FailingStdout(exc)
        monkeypatch.setattr(sys, "stdout", failing)
        capfd.readouterr()

        try:
            code = run_pace_no_interrupt_escape(monkeypatch, "pace", "--json")
            out, err = capfd.readouterr()
            assert code == 4
            assert err == ""
            assert "Traceback" not in out + err
            # stdout now points at the null device, so the shutdown flush is quiet.
            assert sys.stdout is not failing
            sys.stdout.write("x")
            sys.stdout.flush()
            # The old stream is already closed, so its finalizer prints nothing.
            assert _finalizer_output(failing) == []
        finally:
            if sys.stdout is not failing:
                sys.stdout.close()

    def test_ctrl_c_during_write_exits_130_silently(self, fleet: Fleet, monkeypatch, capfd):
        fleet.seed()
        fleet.record_usage()
        failing = _FailingStdout(KeyboardInterrupt())
        monkeypatch.setattr(sys, "stdout", failing)
        capfd.readouterr()

        try:
            code = run_pace_no_interrupt_escape(monkeypatch, "pace", "--json")
            out, err = capfd.readouterr()
            assert code == 130
            assert err == ""
            assert out == ""
            # The failing stream was discarded, as in the BrokenPipe case.
            assert sys.stdout is not failing
            assert _finalizer_output(failing) == []
        finally:
            if sys.stdout is not failing:
                sys.stdout.close()

    def test_stdout_none_exits_4_silently(self, fleet: Fleet, monkeypatch, capfd):
        """``cswap pace --json >&-``: Python starts with ``sys.stdout = None``."""
        fleet.seed()
        fleet.record_usage()
        monkeypatch.setattr(sys, "stdout", None)
        capfd.readouterr()

        try:
            code = run_pace_no_interrupt_escape(monkeypatch, "pace", "--json")
            out, err = capfd.readouterr()
            assert code == 4
            assert out == ""
            assert err == ""
        finally:
            if sys.stdout is not None:
                sys.stdout.close()

    def test_closed_stdout_exits_4_silently(self, fleet: Fleet, monkeypatch, capfd):
        """A closed stream raises ValueError on write, not OSError."""
        fleet.seed()
        fleet.record_usage()
        closed = io.StringIO()
        closed.close()
        monkeypatch.setattr(sys, "stdout", closed)
        capfd.readouterr()

        try:
            code = run_pace_no_interrupt_escape(monkeypatch, "pace", "--json")
            out, err = capfd.readouterr()
            assert code == 4
            assert out == ""
            assert err == ""
            assert sys.stdout is not closed
        finally:
            if sys.stdout is not closed:
                sys.stdout.close()

    def test_missing_null_device_exits_4_silently(
        self, fleet: Fleet, monkeypatch, capfd, tmp_path
    ):
        """With no null device, the discarded output goes to memory instead."""
        fleet.seed()
        fleet.record_usage()
        monkeypatch.setattr(os, "devnull", str(tmp_path / "missing" / "null"))
        failing = _FailingStdout(BrokenPipeError(32, "Broken pipe"))
        monkeypatch.setattr(sys, "stdout", failing)
        capfd.readouterr()

        try:
            code = run_pace_no_interrupt_escape(monkeypatch, "pace", "--json")
            out, err = capfd.readouterr()
            assert code == 4
            assert out == ""
            assert err == ""
            assert isinstance(sys.stdout, io.StringIO)
            sys.stdout.write("x")
            sys.stdout.flush()
        finally:
            if sys.stdout is not failing:
                sys.stdout.close()

    def test_ctrl_c_inside_build_pace_exits_130_silently(
        self, fleet: Fleet, monkeypatch, capfd
    ):
        fleet.seed()
        fleet.record_usage()

        def interrupted(switcher):
            raise KeyboardInterrupt

        monkeypatch.setattr(pace_feed, "build_pace", interrupted)
        capfd.readouterr()

        code = run_pace_no_interrupt_escape(monkeypatch, "pace", "--json")

        out, err = capfd.readouterr()
        assert code == 130
        assert out == ""
        assert err == ""


# ---------------------------------------------------------------------------
# pace must be the first word
# ---------------------------------------------------------------------------


class TestPaceIsFirstWord:
    def test_json_before_pace_is_a_usage_error(
        self, temp_home, no_network, monkeypatch, capfd
    ):
        code = run_pace(monkeypatch, "--json", "pace")

        out, err = capfd.readouterr()
        assert code == 2
        assert out == ""
        assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Root NullHandler — Python's last-resort handler never fires
# ---------------------------------------------------------------------------


class TestLastResortHandler:
    def test_last_resort_handler_never_fires(self, fleet: Fleet, monkeypatch, capfd):
        """A record on a logger with no handler reaches the root NullHandler,
        so ``logging.lastResort`` (a stderr writer) never handles it."""
        fleet.seed()
        fleet.record_usage()
        # pytest attaches capture handlers to the root logger; remove them so
        # the last-resort handler is live, as in a real process.
        monkeypatch.setattr(logging.getLogger(), "handlers", [])
        fired: list[logging.LogRecord] = []

        class Spy(logging.Handler):
            def emit(self, record):
                fired.append(record)

        monkeypatch.setattr(logging, "lastResort", Spy(logging.WARNING))
        original = ClaudeAccountSwitcher.accounts_snapshot

        def noisy(self, *args, **kwargs):
            logging.getLogger("claude_swap.process_detection").error("noise")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(ClaudeAccountSwitcher, "accounts_snapshot", noisy)
        capfd.readouterr()

        code = run_pace(monkeypatch, "pace", "--json")

        capfd.readouterr()
        assert code == 0
        assert fired == []
