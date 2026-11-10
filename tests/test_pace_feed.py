"""Tests for ``claude_swap.pace_feed`` — the read-only ``cswap pace --json`` feed.

Spec: docs/superpowers/specs/2026-11-10-pace-command-design.md, scenarios
1-6, 8 and 9 at the module level, and Security requirements 1-4, 6 and 7.

Every test runs on fake credentials under ``tmp_path`` (``temp_home`` sets
``HOME``, so the backup folder resolves beneath it too). The autouse conftest
fixtures stub the Keychain and the OAuth profile fetch, and ``no_network``
below refuses every socket connection, so no test reads a real credential or
reaches the network.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_swap import pace_feed
from claude_swap.models import AccountSnapshot, AccountsSnapshot, Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import (
    AUTH_DEAD_STRIKES,
    FetchRecord,
    UsageEntry,
    UsageStore,
)

# ---------------------------------------------------------------------------
# Canary identity and secrets: none of these may ever appear in pace output.
# ---------------------------------------------------------------------------

EMAILS = {
    "1": "canary.alice@example.com",
    "2": "canary.bob@example.com",
    "3": "canary.carol@example.com",
}
ORG_NAMES = {"1": "Canary Org Alpha", "2": "Canary Org Beta", "3": ""}
ORG_UUIDS = {
    "1": "org-uuid-canary-1111",
    "2": "org-uuid-canary-2222",
    "3": "org-uuid-canary-3333",
}
ACCOUNT_UUIDS = {
    "1": "acct-uuid-canary-aaaa",
    "2": "acct-uuid-canary-bbbb",
    "3": "acct-uuid-canary-cccc",
}
API_KEY = "sk-ant-api03-CANARYAPIKEY" + "x" * 80 + "-AAAAAAAA"


def _access(num: str) -> str:
    return f"sk-ant-oat01-CANARYACCESS{num}"


def _refresh(num: str) -> str:
    return f"sk-ant-ort01-CANARYREFRESH{num}"


def _oauth_creds(num: str, plan: str | None = "max") -> str:
    oauth: dict = {
        "accessToken": _access(num),
        "refreshToken": _refresh(num),
        # Fixed far-future expiry (2100-01-01): a valid token, and stable
        # parametrize ids across xdist workers.
        "expiresAt": 4102444800000,
        "scopes": ["user:inference", "user:profile"],
    }
    if plan is not None:
        oauth["subscriptionType"] = plan
    return json.dumps({"claudeAiOauth": oauth})


def _usage(pct5: float = 41.0, pct7: float = 12.5, reset5=None, reset7=None) -> dict:
    now = datetime.now(timezone.utc)
    r5 = reset5 or (now + timedelta(hours=2)).isoformat()
    r7 = reset7 or (now + timedelta(days=3)).isoformat()
    return {
        "five_hour": {"pct": pct5, "resets_at": r5, "countdown": "2h", "clock": "14:00"},
        "seven_day": {"pct": pct7, "resets_at": r7, "countdown": "3d", "clock": "Fri"},
    }


def _canaries(home: Path) -> list[str]:
    values = [API_KEY, str(home)]
    for num in ("1", "2", "3"):
        values += [
            EMAILS[num],
            ORG_UUIDS[num],
            ACCOUNT_UUIDS[num],
            _access(num),
            _refresh(num),
        ]
        if ORG_NAMES[num]:
            values.append(ORG_NAMES[num])
    return values


def _assert_no_canary(text: str, home: Path) -> None:
    for value in _canaries(home):
        assert value not in text, f"identity or secret leaked: {value!r}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch):
    """Refuse and record every outbound connection."""
    attempts: list = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("pace made a network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    return attempts


@pytest.fixture
def no_dead_token_clear(monkeypatch):
    """Record every ``clear_dead_token`` call (it writes the usage store)."""
    calls: list = []
    original = UsageStore.clear_dead_token

    def spy(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(UsageStore, "clear_dead_token", spy)
    return calls


class Fleet:
    """Three stored accounts on a Linux switcher under ``temp_home``."""

    def __init__(self, home: Path):
        self.home = home
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        self.switcher = s

    def seed(
        self,
        plans: dict[str, str | None] | None = None,
        api_key_slot: str | None = None,
        missing_backup: str | None = None,
        alias: dict[str, str] | None = None,
    ) -> "Fleet":
        s = self.switcher
        plans = plans or {"1": "max", "2": "pro", "3": "max"}
        alias = alias if alias is not None else {"1": "work"}
        data = s._get_sequence_data()
        data["sequence"] = [1, 2, 3]
        data["activeAccountNumber"] = 1
        data["accounts"] = {}
        for num in ("1", "2", "3"):
            record = {
                "email": EMAILS[num],
                "uuid": ACCOUNT_UUIDS[num],
                "organizationUuid": ORG_UUIDS[num],
                "organizationName": ORG_NAMES[num],
                "added": "2026-01-01T00:00:00Z",
            }
            if num in alias:
                record["alias"] = alias[num]
            if num == api_key_slot:
                record["kind"] = "api_key"
            data["accounts"][num] = record
        s._write_json(s.sequence_file, data)

        # The live identity and credential of the active slot (1).
        (self.home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": EMAILS["1"],
                        "accountUuid": ACCOUNT_UUIDS["1"],
                        "organizationUuid": ORG_UUIDS["1"],
                        "organizationName": ORG_NAMES["1"],
                    }
                }
            )
        )
        live = _oauth_creds("1", plans.get("1"))
        (self.home / ".claude" / ".credentials.json").write_text(live)

        for num in ("1", "2", "3"):
            if num == missing_backup:
                continue
            creds = API_KEY if num == api_key_slot else _oauth_creds(num, plans.get(num))
            s._write_account_credentials(num, EMAILS[num], creds)
        return self

    def identities(self) -> dict[str, tuple[str, str]]:
        return {num: (EMAILS[num], ORG_UUIDS[num]) for num in ("1", "2", "3")}

    def record_usage(self, nums=("1", "2", "3"), usage: dict | None = None) -> None:
        store = self.switcher._usage_store
        ids = self.identities()
        store.record(
            {num: FetchRecord(usage=usage or _usage()) for num in nums},
            {num: ids[num] for num in nums},
        )

    def store_files(self) -> list[Path]:
        s = self.switcher
        files = [
            self.home / ".claude" / ".credentials.json",
            self.home / ".claude.json",
            s.sequence_file,
            s._usage_store.path,
        ]
        files += sorted(s.credentials_dir.glob("*"))
        return files

    def hashes(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for path in self.store_files():
            out[str(path)] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            )
        return out


@pytest.fixture
def fleet(temp_home: Path, no_network) -> Fleet:
    return Fleet(temp_home)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find_row(rows: dict, num: str) -> dict:
    """One stored usage row, whatever the store's top-level layout."""
    if num in rows and isinstance(rows[num], dict):
        return rows[num]
    for value in rows.values():
        if isinstance(value, dict) and isinstance(value.get(num), dict):
            return value[num]
    raise KeyError(num)


def _seed_healed_dead_token_row(fleet: Fleet, num: str) -> None:
    """A struck usage row whose fingerprint no stored credential matches.

    The collector reads this as healed and, on the default path, calls
    ``clear_dead_token`` (a usage-store write).
    """
    fleet.seed()
    fleet.record_usage()
    store = fleet.switcher._usage_store
    store._mutate(
        {num: fleet.identities()[num]},
        [num],
        lambda _n, row: row.update(
            authDeadStrikes=AUTH_DEAD_STRIKES,
            struckFingerprint="condemned-generation-no-longer-stored",
        ),
    )
    entry = store.entries({num: fleet.identities()[num]})[num]
    assert entry.auth_dead_strikes >= AUTH_DEAD_STRIKES
    assert entry.token_dead()


def _seed_legacy_record(fleet: Fleet, num: str) -> None:
    """A sequence record added before org support: no ``organizationUuid``.

    On the default path ``_get_sequence_data_migrated`` rewrites
    ``sequence.json``. The slot's config backup carries the org, so the
    migration has a value to write.
    """
    s = fleet.switcher
    if not s.sequence_file.exists() or not (s._get_sequence_data() or {}).get(
        "accounts"
    ):
        fleet.seed()
        fleet.record_usage()
    s._write_account_config(
        num,
        EMAILS[num],
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": EMAILS[num],
                    "accountUuid": ACCOUNT_UUIDS[num],
                    "organizationUuid": ORG_UUIDS[num],
                    "organizationName": ORG_NAMES[num],
                }
            }
        ),
    )
    data = s._get_sequence_data()
    del data["accounts"][num]["organizationUuid"]
    del data["accounts"][num]["organizationName"]
    s._write_json(s.sequence_file, data)


# ---------------------------------------------------------------------------
# Scenario 1 — Full read
# ---------------------------------------------------------------------------


class TestFullRead:
    def test_three_accounts_all_ok_exit_0(self, fleet: Fleet):
        fleet.seed()
        fleet.record_usage()

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 0
        assert payload["schemaVersion"] == 1
        assert [a["id"] for a in payload["accounts"]] == [1, 2, 3]
        for account in payload["accounts"]:
            assert account["status"] == "ok"
            assert "error" not in account
            assert account["plan"] in ("pro", "max")
            assert set(account["fiveHour"]) == {"pct", "resetsAt"}
            assert set(account["sevenDay"]) == {"pct", "resetsAt"}
            assert account["fiveHour"]["pct"] == 41.0
            assert account["sevenDay"]["pct"] == 12.5
            assert account["fetchedAt"]

    def test_alias_only_when_set(self, fleet: Fleet):
        fleet.seed(alias={"1": "work"})
        fleet.record_usage()

        payload, _ = pace_feed.build_pace(fleet.switcher)

        by_id = {a["id"]: a for a in payload["accounts"]}
        assert by_id[1]["alias"] == "work"
        assert "alias" not in by_id[2]
        assert "alias" not in by_id[3]

    def test_snapshot_called_with_empty_fetch_set(self, fleet: Fleet, monkeypatch):
        fleet.seed()
        fleet.record_usage()
        calls: list = []
        original = fleet.switcher.accounts_snapshot

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        monkeypatch.setattr(fleet.switcher, "accounts_snapshot", spy)
        pace_feed.build_pace(fleet.switcher)

        assert calls == [((), {"fetch": set(), "read_only": True})]


# ---------------------------------------------------------------------------
# Scenario 2 — No identity
# ---------------------------------------------------------------------------


class TestNoIdentity:
    def test_exit_0_output_has_no_identity(self, fleet: Fleet, capsys):
        fleet.seed()
        fleet.record_usage()

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 0
        captured = capsys.readouterr()
        _assert_no_canary(json.dumps(payload), fleet.home)
        _assert_no_canary(captured.out + captured.err, fleet.home)

    def test_exit_3_output_has_no_identity(self, fleet: Fleet, capsys):
        fleet.seed(missing_backup="2")
        fleet.record_usage(nums=("1", "3"))

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 3
        captured = capsys.readouterr()
        _assert_no_canary(json.dumps(payload), fleet.home)
        _assert_no_canary(captured.out + captured.err, fleet.home)

    def test_exit_4_injected_exception_has_no_identity(
        self, fleet: Fleet, monkeypatch, capsys
    ):
        fleet.seed()

        def boom(*args, **kwargs):
            raise RuntimeError(
                f"failed for {EMAILS['1']} token={_access('1')} "
                f"refresh={_refresh('1')} at {fleet.home}/.claude/.credentials.json"
            )

        monkeypatch.setattr(fleet.switcher, "accounts_snapshot", boom)

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 4
        assert payload == {"schemaVersion": 1, "error": "unavailable"}
        captured = capsys.readouterr()
        _assert_no_canary(json.dumps(payload), fleet.home)
        _assert_no_canary(captured.out + captured.err, fleet.home)
        assert "RuntimeError" not in json.dumps(payload) + captured.out + captured.err

    def test_exception_inside_plan_read_has_no_identity(
        self, fleet: Fleet, monkeypatch, capsys
    ):
        fleet.seed()
        fleet.record_usage()

        def boom(*args, **kwargs):
            raise OSError(f"cannot read {fleet.home}/x for {EMAILS['2']} {_access('2')}")

        # The snapshot pass reads the backups too; break the backup reader
        # only after the snapshot exists, so only the plan read fails.
        original = fleet.switcher.accounts_snapshot

        def snapshot_then_break(*args, **kwargs):
            snap = original(*args, **kwargs)
            monkeypatch.setattr(fleet.switcher, "_read_account_credentials", boom)
            return snap

        monkeypatch.setattr(fleet.switcher, "accounts_snapshot", snapshot_then_break)

        payload, code = pace_feed.build_pace(fleet.switcher)

        captured = capsys.readouterr()
        _assert_no_canary(json.dumps(payload), fleet.home)
        _assert_no_canary(captured.out + captured.err, fleet.home)
        by_id = {a["id"]: a for a in payload["accounts"]}
        assert by_id[2]["plan"] == "unknown"
        assert code == 0

    def test_last_error_is_never_printed(self, fleet: Fleet):
        fleet.seed()
        fleet.record_usage()
        store = fleet.switcher._usage_store
        store.record(
            {"2": FetchRecord(error=f"timeout {EMAILS['2']} CANARY-LAST-ERROR")},
            {"2": fleet.identities()["2"]},
        )

        payload, _ = pace_feed.build_pace(fleet.switcher)

        text = json.dumps(payload)
        assert "CANARY-LAST-ERROR" not in text
        assert "timeout" not in text
        _assert_no_canary(text, fleet.home)


# ---------------------------------------------------------------------------
# Scenario 3 — Partial data
# ---------------------------------------------------------------------------


class TestPartialData:
    def test_one_failed_account_keeps_the_others_exit_3(self, fleet: Fleet):
        fleet.seed(missing_backup="2")
        fleet.record_usage(nums=("1", "3"))

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 3
        by_id = {a["id"]: a for a in payload["accounts"]}
        assert set(by_id) == {1, 2, 3}
        assert by_id[1]["status"] == "ok" and "fiveHour" in by_id[1]
        assert by_id[3]["status"] == "ok" and "sevenDay" in by_id[3]
        failed = by_id[2]
        assert failed["status"] == "no_credentials"
        assert failed["error"] == "no_credentials"
        assert "fiveHour" not in failed and "sevenDay" not in failed

    def test_no_stored_usage_is_unavailable_exit_3(self, fleet: Fleet):
        fleet.seed()
        fleet.record_usage(nums=("1", "2"))

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 3
        by_id = {a["id"]: a for a in payload["accounts"]}
        assert by_id[3]["status"] == "unavailable"
        assert by_id[3]["error"] == "unavailable"
        assert "fiveHour" not in by_id[3] and "fetchedAt" not in by_id[3]

    def test_stale_only_fleet_keeps_windows_and_gives_exit_0(self, fleet: Fleet):
        fleet.seed()
        fleet.record_usage()
        store = fleet.switcher._usage_store
        real_clock = store.clock
        store.clock = lambda: real_clock() + 2 * 3600.0

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 0
        for account in payload["accounts"]:
            assert account["status"] == "stale"
            assert "error" not in account
            assert account["fiveHour"]["pct"] == 41.0
            assert account["fetchedAt"]

    def test_no_readable_account_gives_exit_4(self, fleet: Fleet):
        fleet.seed()  # credentials, but no stored usage at all

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 4
        assert payload["schemaVersion"] == 1
        assert [a["id"] for a in payload["accounts"]] == [1, 2, 3]
        assert all(a["status"] == "unavailable" for a in payload["accounts"])

    def test_no_accounts_gives_exit_0(self, temp_home: Path, no_network):
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()

        payload, code = pace_feed.build_pace(s)

        assert code == 0
        assert payload == {"schemaVersion": 1, "accounts": []}

    def test_api_key_plus_ok_gives_exit_0(self):
        now = time.time()
        ok = UsageEntry(last_good=_usage(), fetched_at=now, age_s=0.0)
        fake = _FakeSwitcher(
            [_snap("1", ok), _snap("3", UsageEntry(), kind="api_key")],
            {"1": _oauth_creds("1"), "3": API_KEY},
        )

        payload, code = pace_feed.build_pace(fake)

        assert code == 0
        assert [a["status"] for a in payload["accounts"]] == ["ok", "api_key"]

    def test_ok_plus_token_expired_gives_exit_3(self):
        now = time.time()
        ok = UsageEntry(last_good=_usage(), fetched_at=now, age_s=0.0)
        expired = UsageEntry(sentinel="token expired")
        fake = _FakeSwitcher(
            [_snap("1", ok), _snap("2", expired)],
            {"1": _oauth_creds("1"), "2": _oauth_creds("2")},
        )

        payload, code = pace_feed.build_pace(fake)

        assert code == 3
        by_id = {a["id"]: a for a in payload["accounts"]}
        assert by_id[1]["status"] == "ok"
        assert by_id[2]["status"] == "token_expired"
        assert by_id[2]["error"] == "token_expired"

    def test_every_account_failed_gives_exit_4_with_accounts(self):
        fake = _FakeSwitcher(
            [
                _snap("1", UsageEntry(sentinel="token expired")),
                _snap("2", UsageEntry(sentinel="re-login needed")),
                _snap("3", UsageEntry(sentinel="no credentials")),
            ],
            {},
        )

        payload, code = pace_feed.build_pace(fake)

        assert code == 4
        _assert_contract(payload)
        assert [(a["id"], a["status"]) for a in payload["accounts"]] == [
            (1, "token_expired"),
            (2, "relogin_needed"),
            (3, "no_credentials"),
        ]


# ---------------------------------------------------------------------------
# Scenario 4 — Plan type
# ---------------------------------------------------------------------------


class TestPlanType:
    def test_max_pro_and_missing_field(self, fleet: Fleet):
        fleet.seed(plans={"1": "max", "2": "pro", "3": None})
        fleet.record_usage()

        payload, _ = pace_feed.build_pace(fleet.switcher)

        by_id = {a["id"]: a for a in payload["accounts"]}
        assert by_id[1]["plan"] == "max"
        assert by_id[2]["plan"] == "pro"
        assert by_id[3]["plan"] == "unknown"

    def test_active_plan_comes_from_the_live_credential(self, fleet: Fleet):
        fleet.seed(plans={"1": "pro", "2": "pro", "3": "pro"})
        fleet.record_usage()
        # The live store says max; the slot-1 backup still says pro.
        (fleet.home / ".claude" / ".credentials.json").write_text(_oauth_creds("1", "max"))

        payload, _ = pace_feed.build_pace(fleet.switcher)

        assert payload["accounts"][0]["plan"] == "max"

    @pytest.mark.parametrize(
        ("blob", "expected"),
        [
            (_oauth_creds("1", "max"), "max"),
            (_oauth_creds("1", "pro"), "pro"),
            (_oauth_creds("1", None), "unknown"),
            (_oauth_creds("1", "enterprise"), "unknown"),
            (_oauth_creds("1", "MAX"), "unknown"),
            (json.dumps({"claudeAiOauth": {"subscriptionType": None}}), "unknown"),
            (json.dumps({"claudeAiOauth": {"subscriptionType": 3}}), "unknown"),
            (json.dumps({"claudeAiOauth": "max"}), "unknown"),
            (json.dumps({"subscriptionType": "max"}), "unknown"),
            (json.dumps(["max"]), "unknown"),
            ("not json {{{", "unknown"),
            (API_KEY, "unknown"),
            ("", "unknown"),
            (None, "unknown"),
        ],
    )
    def test_plan_from_credential(self, blob, expected):
        assert pace_feed.plan_from_credential(blob) == expected

    def test_plan_helper_returns_only_the_plan_word(self, fleet: Fleet, caplog):
        fleet.seed()
        caplog.set_level("DEBUG")
        snapshot = fleet.switcher.accounts_snapshot(fetch=set())
        caplog.clear()

        results = [pace_feed.read_plan(fleet.switcher, acc) for acc in snapshot.accounts]

        assert results == ["max", "pro", "max"]
        assert all(isinstance(r, str) for r in results)
        _assert_no_canary(caplog.text, fleet.home)


# ---------------------------------------------------------------------------
# Scenario 5 — No network, no writes
# ---------------------------------------------------------------------------


class TestNoNetworkNoWrites:
    def test_stores_are_hash_equal_and_no_network(
        self, fleet: Fleet, no_network, no_dead_token_clear
    ):
        fleet.seed(missing_backup="3")
        fleet.record_usage(nums=("1", "2"))
        before = fleet.hashes()
        # Each slot backup that exists is in the hash set.
        assert sum(1 for p in fleet.store_files() if p.name.endswith(".enc")) == 2
        assert all(v is not None for v in before.values())

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 3
        assert fleet.hashes() == before
        assert no_network == []
        assert no_dead_token_clear == []

    def test_stale_and_expired_rows_write_nothing(
        self, fleet: Fleet, no_network, no_dead_token_clear
    ):
        fleet.seed()
        fleet.record_usage()
        store = fleet.switcher._usage_store
        real_clock = store.clock
        store.clock = lambda: real_clock() + 3 * 3600.0
        before = fleet.hashes()

        pace_feed.build_pace(fleet.switcher)

        assert fleet.hashes() == before
        assert no_network == []
        assert no_dead_token_clear == []

    def test_healed_dead_token_row_is_not_cleared(
        self, fleet: Fleet, no_network, no_dead_token_clear
    ):
        _seed_healed_dead_token_row(fleet, "2")
        usage_path = fleet.switcher._usage_store.path
        before = fleet.hashes()

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert no_dead_token_clear == []
        assert _sha(usage_path) == before[str(usage_path)]
        assert fleet.hashes() == before
        assert code == 0
        assert {a["id"]: a for a in payload["accounts"]}[2]["status"] == "ok"

    def test_legacy_record_without_org_uuid_is_not_migrated(
        self, fleet: Fleet, no_network, no_dead_token_clear
    ):
        _seed_legacy_record(fleet, "2")
        seq_path = fleet.switcher.sequence_file
        before = fleet.hashes()

        payload, _code = pace_feed.build_pace(fleet.switcher)

        assert _sha(seq_path) == before[str(seq_path)]
        assert fleet.hashes() == before
        snapshot = fleet.switcher.accounts_snapshot(fetch=set(), read_only=True)
        assert fleet.hashes() == before
        assert {a.number: a.org_uuid for a in snapshot.accounts}["2"] == ""
        assert "organizationUuid" not in json.loads(seq_path.read_text())[
            "accounts"
        ]["2"]
        _assert_contract(payload)

    @pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
    def test_default_snapshot_still_migrates_and_clears(
        self, fleet: Fleet, no_network, no_dead_token_clear, explicit
    ):
        """Existing callers (read_only False) keep the two writes."""
        _seed_healed_dead_token_row(fleet, "2")
        _seed_legacy_record(fleet, "3")
        seq_path = fleet.switcher.sequence_file
        before = fleet.hashes()

        if explicit:
            fleet.switcher.accounts_snapshot(fetch=set(), read_only=False)
        else:
            fleet.switcher.accounts_snapshot(fetch=set())

        assert [args[0] for args in no_dead_token_clear] == [["2"]]
        record = json.loads(seq_path.read_text())["accounts"]["3"]
        assert record["organizationUuid"] == ORG_UUIDS["3"]
        assert _sha(seq_path) != before[str(seq_path)]
        rows = json.loads(fleet.switcher._usage_store.path.read_text())
        assert _find_row(rows, "2")["authDeadStrikes"] == 0

    def test_module_imports_no_token_accessor_and_calls_no_writer(self):
        """Security requirements 2 and 7, checked on the code (not the
        docstrings): no token accessor, no oauth module, no writer, no
        logging, no exception text."""
        tree = ast.parse(inspect.getsource(pace_feed))
        imported: set[str] = set()
        names: set[str] = set()
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)

        forbidden_imports = {
            "claude_swap.oauth", "oauth", "logging", "urllib", "urllib.request",
            "socket", "http", "http.client", "extract_oauth_data",
            "extract_access_token", "extract_refresh_token",
        }
        assert not imported & forbidden_imports, imported & forbidden_imports
        forbidden_names = {
            "oauth", "extract_oauth_data", "extract_access_token",
            "extract_refresh_token", "clear_dead_token", "record", "reserve",
            "adopt", "write_text", "write_bytes", "atomic_write_text",
            "atomic_write_json", "_write_json", "_write_account_credentials",
            "_write_credentials", "urlopen", "_logger", "logger", "print",
            "repr", "format_exc", "traceback",
        }
        assert not names & forbidden_names, names & forbidden_names
        # `str` is allowed as a type (isinstance) but never called: a str()
        # call is how exception text reaches a payload.
        assert not called & {"str", "repr", "print", "format"}, called


# ---------------------------------------------------------------------------
# Scenario 6 — Allowed keys and error words
# ---------------------------------------------------------------------------

ACCOUNT_KEYS = {
    "id", "alias", "plan", "status", "error", "fetchedAt", "fiveHour", "sevenDay",
}
WINDOW_KEYS = {"pct", "resetsAt"}
STATUS_WORDS = {
    "ok", "stale", "api_key", "token_expired", "relogin_needed",
    "no_credentials", "unavailable",
}
ERROR_WORDS = STATUS_WORDS - {"ok", "stale"}


def _assert_contract(payload: dict) -> None:
    assert set(payload) == {"schemaVersion", "accounts"}
    assert payload["schemaVersion"] == 1
    for account in payload["accounts"]:
        assert set(account) <= ACCOUNT_KEYS, set(account) - ACCOUNT_KEYS
        assert isinstance(account["id"], int) and not isinstance(account["id"], bool)
        assert account["plan"] in {"pro", "max", "unknown"}
        assert account["status"] in STATUS_WORDS
        if "alias" in account:
            assert isinstance(account["alias"], str) and account["alias"]
        if account["status"] in ERROR_WORDS:
            assert account["error"] == account["status"]
            assert "fiveHour" not in account and "sevenDay" not in account
            assert "fetchedAt" not in account
        else:
            assert "error" not in account
        for window in ("fiveHour", "sevenDay"):
            if window in account:
                assert set(account[window]) <= WINDOW_KEYS
                assert "pct" in account[window]


class _FakeSwitcher:
    """A switcher stand-in that serves one prepared snapshot."""

    def __init__(self, accounts: list[AccountSnapshot], creds: dict[str, str]):
        self._snapshot = AccountsSnapshot(
            active_number=None, accounts=tuple(accounts), taken_at=time.time()
        )
        self._creds = creds

    def accounts_snapshot(self, fetch=None, *, read_only=False):
        assert fetch == set()
        assert read_only is True
        return self._snapshot

    def _read_account_credentials(self, account_num, email):
        return self._creds.get(account_num, "")

    def _read_active_credentials(self):
        raise AssertionError("no active account in this fake")


def _snap(num: str, entry: UsageEntry, kind: str = "oauth", alias: str = "") -> AccountSnapshot:
    return AccountSnapshot(
        number=num,
        email=EMAILS.get(num, "canary.x@example.com"),
        org_name="Canary Org Alpha",
        org_uuid=ORG_UUIDS.get(num, "org-uuid-canary-1111"),
        is_active=False,
        kind=kind,
        switchable=True,
        usage=entry,
        alias=alias,
    )


class TestAllowedKeys:
    def test_real_fleet_payload_has_only_contract_keys(self, fleet: Fleet):
        fleet.seed(api_key_slot="3", missing_backup="2")
        fleet.record_usage(nums=("1",))

        payload, _ = pace_feed.build_pace(fleet.switcher)

        _assert_contract(payload)

    @pytest.mark.parametrize(
        ("sentinel", "word", "exit_code"),
        [
            ("token expired", "token_expired", 4),
            ("re-login needed", "relogin_needed", 4),
            ("no credentials", "no_credentials", 4),
            ("api key", "api_key", 0),
            ("keychain unavailable", "unavailable", 4),
            ("foreign credential", "unavailable", 4),
            ("some future sentinel", "unavailable", 4),
        ],
    )
    def test_each_sentinel_maps_to_one_status_word(self, sentinel, word, exit_code):
        now = time.time()
        entry = UsageEntry(
            sentinel=sentinel,
            last_good=_usage(),
            fetched_at=now,
            age_s=0.0,
            last_error="http-429 CANARY-LAST-ERROR",
        )
        fake = _FakeSwitcher([_snap("2", entry)], {"2": _oauth_creds("2", "pro")})

        payload, code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        account = payload["accounts"][0]
        assert account["status"] == word
        assert account["error"] == word
        assert code == exit_code
        assert "CANARY-LAST-ERROR" not in json.dumps(payload)

    def test_extra_snapshot_fields_are_not_copied(self):
        now = time.time()
        usage = _usage()
        usage["spend"] = {"used": 1.0, "limit": 2.0, "pct": 50.0, "currency": "USD"}
        usage["scoped"] = [{"name": "Fable", "pct": 3.0}]
        usage["five_hour"]["email"] = EMAILS["1"]
        usage["canary_extra"] = EMAILS["1"]
        entry = UsageEntry(last_good=usage, fetched_at=now, age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry, alias="work")], {"1": _oauth_creds("1")})

        payload, code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        assert code == 0
        text = json.dumps(payload)
        assert "spend" not in text and "Fable" not in text and "scoped" not in text
        assert "countdown" not in text and "clock" not in text
        _assert_no_canary(text, Path("/nonexistent-home-canary"))


# ---------------------------------------------------------------------------
# Scenario 8 — API-key account
# ---------------------------------------------------------------------------


class TestApiKeyAccount:
    def test_api_key_account_has_status_and_no_windows(self, fleet: Fleet):
        fleet.seed(api_key_slot="3")
        fleet.record_usage(nums=("1", "2"))

        payload, code = pace_feed.build_pace(fleet.switcher)

        account = {a["id"]: a for a in payload["accounts"]}[3]
        assert account["status"] == "api_key"
        assert account["error"] == "api_key"
        assert account["plan"] == "unknown"
        assert "fiveHour" not in account and "sevenDay" not in account
        assert code == 0
        _assert_no_canary(json.dumps(payload), fleet.home)

    def test_api_key_kind_wins_even_with_stored_usage(self):
        entry = UsageEntry(last_good=_usage(), fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher([_snap("3", entry, kind="api_key")], {"3": API_KEY})

        payload, _ = pace_feed.build_pace(fake)

        account = payload["accounts"][0]
        assert account["status"] == "api_key"
        assert "fiveHour" not in account


# ---------------------------------------------------------------------------
# Scenario 9 — Time zones and pct range
# ---------------------------------------------------------------------------


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f"time without a time zone: {value!r}"
    assert parsed.utcoffset() is not None
    return parsed


class TestTimeZones:
    def test_every_time_has_a_time_zone_and_pct_in_range(self, fleet: Fleet):
        fleet.seed()
        fleet.record_usage()

        payload, _ = pace_feed.build_pace(fleet.switcher)

        for account in payload["accounts"]:
            _aware(account["fetchedAt"])
            for window in ("fiveHour", "sevenDay"):
                _aware(account[window]["resetsAt"])
                assert 0 <= account[window]["pct"] <= 100

    def test_fetched_at_matches_the_store_time(self):
        fetched = datetime(2026, 11, 10, 11, 58, 12, tzinfo=timezone.utc).timestamp()
        entry = UsageEntry(last_good=_usage(), fetched_at=fetched, age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        assert payload["accounts"][0]["fetchedAt"] == "2026-11-10T11:58:12Z"

    def test_offset_reset_time_is_kept_with_its_instant(self):
        usage = _usage(reset5="2026-11-10T16:00:00.123456+02:00")
        entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        reset = payload["accounts"][0]["fiveHour"]["resetsAt"]
        assert reset == "2026-11-10T14:00:00Z"
        assert _aware(reset) == datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)

    def test_reset_time_microseconds_are_cut_not_rounded(self):
        usage = _usage(reset5="2026-11-10T14:00:00.999999+00:00")
        entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        assert payload["accounts"][0]["fiveHour"]["resetsAt"] == "2026-11-10T14:00:00Z"

    def test_reset_time_qa_value_has_whole_seconds(self):
        usage = _usage(reset5="2026-11-10T14:00:00.123456+00:00")
        entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        assert payload["accounts"][0]["fiveHour"]["resetsAt"] == "2026-11-10T14:00:00Z"

    def test_reset_time_negative_offset_is_converted_to_z(self):
        usage = _usage(reset5="2026-11-10T11:00:00.500000-03:00")
        entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        assert payload["accounts"][0]["fiveHour"]["resetsAt"] == "2026-11-10T14:00:00Z"

    def test_every_time_field_matches_whole_second_utc_format(self, fleet: Fleet):
        fleet.seed()
        fleet.record_usage()

        payload, _ = pace_feed.build_pace(fleet.switcher)

        pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        for account in payload["accounts"]:
            assert pattern.fullmatch(account["fetchedAt"]), account["fetchedAt"]
            for window in ("fiveHour", "sevenDay"):
                value = account[window]["resetsAt"]
                assert pattern.fullmatch(value), value

    def test_fetched_at_with_fraction_keeps_whole_second_format(self):
        fetched = datetime(2026, 11, 10, 11, 58, 12, 987654, tzinfo=timezone.utc).timestamp()
        entry = UsageEntry(last_good=_usage(), fetched_at=fetched, age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        assert payload["accounts"][0]["fetchedAt"] == "2026-11-10T11:58:12Z"

    def test_reset_time_without_zone_drops_only_reset_with_fraction(self):
        usage = _usage(reset7="2026-11-14T09:00:00.123456+00:00")
        usage["five_hour"]["resets_at"] = "2026-11-10T14:00:00.123456"
        entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        account = payload["accounts"][0]
        assert account["fiveHour"] == {"pct": 41.0}
        assert account["sevenDay"] == {"pct": 12.5, "resetsAt": "2026-11-14T09:00:00Z"}

    @pytest.mark.parametrize("bad_reset", ["2026-11-10T14:00:00", "not a time", 12345])
    def test_reset_time_without_zone_is_omitted(self, bad_reset):
        usage = _usage()
        usage["five_hour"]["resets_at"] = bad_reset
        entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        window = payload["accounts"][0]["fiveHour"]
        assert "resetsAt" not in window
        assert window["pct"] == 41.0

    @pytest.mark.parametrize("bad_pct", [-1.0, 100.5, float("nan"), float("inf"), "41", None, True])
    def test_pct_out_of_range_drops_the_window(self, bad_pct):
        usage = _usage()
        usage["five_hour"]["pct"] = bad_pct
        entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        account = payload["accounts"][0]
        assert "fiveHour" not in account
        assert account["sevenDay"]["pct"] == 12.5

    def test_pct_too_large_for_float_drops_only_its_window(self):
        usage = _usage()
        usage["five_hour"]["pct"] = 10**400
        entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        good = UsageEntry(last_good=_usage(), fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher(
            [_snap("1", entry), _snap("2", good)],
            {"1": _oauth_creds("1"), "2": _oauth_creds("2", "pro")},
        )

        payload, code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        assert code == 0
        first, second = payload["accounts"]
        assert first["status"] == "ok"
        assert "fiveHour" not in first
        assert first["sevenDay"]["pct"] == 12.5
        assert "fetchedAt" in first
        assert second["status"] == "ok"
        assert second["fiveHour"]["pct"] == 41.0
        assert str(10**400)[:20] not in json.dumps(payload, allow_nan=False)

    @pytest.mark.parametrize("edge", [0, 0.0, 100, 100.0])
    def test_pct_edges_are_kept(self, edge):
        usage = _usage(pct5=edge)
        entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, _ = pace_feed.build_pace(fake)

        assert payload["accounts"][0]["fiveHour"]["pct"] == float(edge)

    # --- T3b: a reset time that cannot be converted drops only ``resetsAt`` ---

    @staticmethod
    def _two_accounts(bad_reset: object, second: UsageEntry | None = None):
        """Account 1 holds ``bad_reset`` in fiveHour; account 2 is healthy
        unless ``second`` replaces it."""
        usage = _usage(reset7="2026-11-14T09:00:00+00:00")
        usage["five_hour"]["resets_at"] = bad_reset
        bad = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)
        good = second or UsageEntry(
            last_good=_usage(
                reset5="2026-11-10T14:00:00+00:00", reset7="2026-11-14T09:00:00+00:00"
            ),
            fetched_at=time.time(),
            age_s=0.0,
        )
        return _FakeSwitcher(
            [_snap("1", bad), _snap("2", good)],
            {"1": _oauth_creds("1"), "2": _oauth_creds("2", "pro")},
        )

    @pytest.mark.parametrize(
        "edge", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
    )
    def test_out_of_range_reset_drops_only_reset(self, edge):
        fake = self._two_accounts(edge)

        payload, code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        assert code == 0
        first, second = payload["accounts"]
        assert first["status"] == "ok"
        assert first["fiveHour"] == {"pct": 41.0}
        assert first["sevenDay"] == {"pct": 12.5, "resetsAt": "2026-11-14T09:00:00Z"}
        assert second["status"] == "ok"
        assert second["fiveHour"] == {"pct": 41.0, "resetsAt": "2026-11-10T14:00:00Z"}
        assert second["sevenDay"] == {"pct": 12.5, "resetsAt": "2026-11-14T09:00:00Z"}

    @pytest.mark.parametrize(
        "edge", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
    )
    def test_out_of_range_reset_keeps_partial_exit_code(self, edge):
        fake = self._two_accounts(edge, second=UsageEntry(sentinel="token expired"))

        payload, code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        assert code == 3
        first, second = payload["accounts"]
        assert first["status"] == "ok"
        assert first["fiveHour"] == {"pct": 41.0}
        assert second["status"] == "token_expired"

    def test_half_hour_offset_gives_the_right_instant(self):
        fake = self._two_accounts("2026-11-10T19:30:00+05:30")

        payload, _ = pace_feed.build_pace(fake)

        reset = payload["accounts"][0]["fiveHour"]["resetsAt"]
        assert reset == "2026-11-10T14:00:00Z"
        assert _aware(reset) == datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)

    def test_stored_z_suffix_is_read(self):
        fake = self._two_accounts("2026-11-10T14:00:00Z")

        payload, _ = pace_feed.build_pace(fake)

        assert payload["accounts"][0]["fiveHour"]["resetsAt"] == "2026-11-10T14:00:00Z"

    def test_fraction_before_midnight_truncates_without_date_roll(self):
        fake = self._two_accounts("2026-11-10T23:59:59.9+00:00")

        payload, _ = pace_feed.build_pace(fake)

        assert payload["accounts"][0]["fiveHour"]["resetsAt"] == "2026-11-10T23:59:59Z"

    @pytest.mark.parametrize(
        ("hostile", "fragments"),
        [
            ("Q" * 10000, ["QQQQ"]),
            (
                "2026-11-10T14:00:00\x00\x07\x1b[31mCANARY-RESET\ud800+00:00",
                ["CANARY-RESET", "\\u001b", "\\u0000", "\\u0007", "\\ud800", "[31m"],
            ),
        ],
        ids=["10000-chars", "control-chars-and-lone-surrogate"],
    )
    def test_hostile_reset_drops_only_reset_and_prints_no_stored_text(
        self, hostile, fragments
    ):
        fake = self._two_accounts(hostile)

        payload, code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        assert code == 0
        first, second = payload["accounts"]
        assert first["fiveHour"] == {"pct": 41.0}
        assert first["sevenDay"] == {"pct": 12.5, "resetsAt": "2026-11-14T09:00:00Z"}
        assert second["fiveHour"]["resetsAt"] == "2026-11-10T14:00:00Z"
        text = json.dumps(payload)
        # The payload encodes as strict UTF-8: no lone surrogate survives.
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for fragment in fragments:
            assert fragment not in text, fragment


# --- T3c: a stored fetched_at that cannot be converted fails only its account ---

#: Stored fetched_at values that cannot become a time, and the JSON forms that
#: each value would take if it leaked into the payload.
_BAD_FETCHED_AT = [
    (1e20, ["1e+20", "100000000000000000000"]),
    (float("inf"), ["Infinity", "inf"]),
    (float("nan"), ["NaN"]),
    (253402300800.0, ["253402300800"]),
    (-1e20, ["-1e+20", "-100000000000000000000"]),
    (True, ["true"]),
]
_BAD_FETCHED_AT_IDS = ["1e20", "inf", "nan", "year-10000", "minus-1e20", "bool"]


class TestBadFetchedAt:
    @staticmethod
    def _entry(fetched_at: object) -> UsageEntry:
        return UsageEntry(last_good=_usage(), fetched_at=fetched_at, age_s=0.0)

    @staticmethod
    def _assert_no_stored_value(payload: dict, fragments: list[str]) -> None:
        # Strict JSON: NaN or Infinity in the payload would raise here.
        text = json.dumps(payload, allow_nan=False)
        for fragment in fragments:
            assert fragment not in text, fragment

    @pytest.mark.parametrize(
        ("bad", "fragments"), _BAD_FETCHED_AT, ids=_BAD_FETCHED_AT_IDS
    )
    def test_bad_fetched_at_fails_only_its_account(self, bad, fragments):
        fake = _FakeSwitcher(
            [
                _snap("1", self._entry(bad), alias="work"),
                _snap("2", self._entry(time.time())),
                _snap("3", self._entry(time.time())),
            ],
            {
                "1": _oauth_creds("1"),
                "2": _oauth_creds("2", "pro"),
                "3": _oauth_creds("3"),
            },
        )

        payload, code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        assert code == 3
        first, second, third = payload["accounts"]
        assert first == {
            "id": 1,
            "alias": "work",
            "plan": "max",
            "status": "unavailable",
            "error": "unavailable",
        }
        for account in (second, third):
            assert account["status"] == "ok"
            assert set(account) == {"id", "plan", "status", "fetchedAt", "fiveHour", "sevenDay"}
        assert second["plan"] == "pro"
        self._assert_no_stored_value(payload, fragments)

    def test_bad_fetched_at_without_alias_has_no_alias_key(self):
        fake = _FakeSwitcher(
            [_snap("1", self._entry(float("nan"))), _snap("2", self._entry(time.time()))],
            {"1": _oauth_creds("1", "pro"), "2": _oauth_creds("2")},
        )

        payload, code = pace_feed.build_pace(fake)

        assert code == 3
        assert payload["accounts"][0] == {
            "id": 1,
            "plan": "pro",
            "status": "unavailable",
            "error": "unavailable",
        }

    def test_every_account_bad_gives_exit_4_with_accounts(self):
        bads = [value for value, _ in _BAD_FETCHED_AT]
        nums = [str(i) for i in range(1, len(bads) + 1)]
        fake = _FakeSwitcher(
            [_snap(num, self._entry(bad)) for num, bad in zip(nums, bads)],
            {num: _oauth_creds(num) for num in nums},
        )

        payload, code = pace_feed.build_pace(fake)

        assert code == 4
        assert "error" not in payload
        _assert_contract(payload)
        assert [a["id"] for a in payload["accounts"]] == [int(n) for n in nums]
        for account in payload["accounts"]:
            assert account == {
                "id": account["id"],
                "plan": "max",
                "status": "unavailable",
                "error": "unavailable",
            }
        self._assert_no_stored_value(
            payload, [f for _, frags in _BAD_FETCHED_AT for f in frags]
        )

    @pytest.mark.parametrize(
        "bad", ["1731243600", b"1731243600", [1731243600], 10**400],
        ids=["str", "bytes", "list", "huge-int-pace-half-only"],
    )
    def test_wrong_type_or_huge_int_fails_only_its_account(self, bad):
        """Injected entries: this covers only the pace_feed half.

        The usage store never hands pace an integer like ``10**400``: it
        fails first (see
        ``test_store_huge_int_fetched_at_fails_closed_with_envelope``).
        """
        fake = _FakeSwitcher(
            [_snap("1", self._entry(bad)), _snap("2", self._entry(time.time()))],
            {"1": _oauth_creds("1"), "2": _oauth_creds("2")},
        )

        payload, code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        assert code == 3
        first, second = payload["accounts"]
        assert first["status"] == "unavailable"
        assert set(first) == {"id", "plan", "status", "error"}
        assert second["status"] == "ok"
        assert "1731243600" not in json.dumps(payload, allow_nan=False)

    def test_store_huge_int_fetched_at_fails_closed_with_envelope(
        self, fleet: Fleet, monkeypatch, capfd
    ):
        """A hand-edited usage.json with ``fetchedAt`` 10**400: the store
        raises before pace reads the row. Accepted: fail closed, exit 4."""
        from claude_swap import cli
        import sys

        fleet.seed()
        fleet.record_usage()
        path = fleet.switcher._usage_store.path
        rows = json.loads(path.read_text())
        _find_row(rows, "1")["fetchedAt"] = 10**400
        path.write_text(json.dumps(rows))
        huge = str(10**400)
        capfd.readouterr()

        payload, code = pace_feed.build_pace(fleet.switcher)

        assert code == 4
        assert payload == {"schemaVersion": 1, "error": "unavailable"}

        monkeypatch.setattr(sys, "argv", ["cswap", "pace", "--json"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        out, err = capfd.readouterr()
        assert exc.value.code == 4
        assert out == '{"schemaVersion":1,"error":"unavailable"}\n'
        assert err == ""
        assert huge[:20] not in out + err
        _assert_no_canary(out + err, fleet.home)

    def test_none_fetched_at_keeps_its_current_status(self):
        fake = _FakeSwitcher(
            [_snap("1", self._entry(None)), _snap("2", self._entry(time.time()))],
            {"1": _oauth_creds("1"), "2": _oauth_creds("2")},
        )

        payload, code = pace_feed.build_pace(fake)

        assert code == 3
        assert payload["accounts"][0] == {
            "id": 1,
            "plan": "max",
            "status": "unavailable",
            "error": "unavailable",
        }

    @pytest.mark.parametrize(
        "bad", [float("nan"), 1e20, True], ids=["nan", "1e20", "bool"]
    )
    def test_account_status_comes_before_bad_fetched_at(self, bad):
        """The status of the account itself wins over the bad-fetched_at rule:
        the rule applies only to accounts that would be ok or stale."""
        fake = _FakeSwitcher(
            [
                _snap("1", self._entry(bad), kind="api_key"),
                _snap(
                    "2",
                    UsageEntry(
                        sentinel="token expired",
                        last_good=_usage(),
                        fetched_at=bad,
                        age_s=0.0,
                    ),
                ),
                _snap(
                    "3",
                    UsageEntry(
                        sentinel="re-login needed",
                        last_good=_usage(),
                        fetched_at=bad,
                        age_s=0.0,
                    ),
                ),
                _snap(
                    "4",
                    UsageEntry(
                        sentinel="no credentials",
                        last_good=_usage(),
                        fetched_at=bad,
                        age_s=0.0,
                    ),
                ),
            ],
            {
                "1": API_KEY,
                "2": _oauth_creds("2"),
                "3": _oauth_creds("3"),
                "4": _oauth_creds("4"),
            },
        )

        payload, code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        # api_key is a read account, the three sentinels are failed: partial.
        assert code == 3
        assert payload["accounts"] == [
            {"id": 1, "plan": "unknown", "status": "api_key", "error": "api_key"},
            {"id": 2, "plan": "max", "status": "token_expired", "error": "token_expired"},
            {"id": 3, "plan": "max", "status": "relogin_needed", "error": "relogin_needed"},
            {"id": 4, "plan": "max", "status": "no_credentials", "error": "no_credentials"},
        ]

    @pytest.mark.parametrize(
        ("stale", "status", "code"),
        [(False, "ok", 0), (True, "stale", 0)],
        ids=["fresh-ok", "old-stale"],
    )
    def test_negative_in_range_fetched_at_converts_as_usual(self, stale, status, code):
        from claude_swap.usage_store import STALE_OK_S

        age_s = STALE_OK_S + 1.0 if stale else 0.0
        usage = _usage(
            reset5="2026-11-10T14:00:00+00:00", reset7="2026-11-14T09:00:00+00:00"
        )
        entry = UsageEntry(last_good=usage, fetched_at=-1.0, age_s=age_s)
        fake = _FakeSwitcher([_snap("1", entry)], {"1": _oauth_creds("1")})

        payload, exit_code = pace_feed.build_pace(fake)

        _assert_contract(payload)
        assert exit_code == code
        assert payload["accounts"] == [
            {
                "id": 1,
                "plan": "max",
                "status": status,
                "fetchedAt": "1969-12-31T23:59:59Z",
                "fiveHour": {"pct": 41.0, "resetsAt": "2026-11-10T14:00:00Z"},
                "sevenDay": {"pct": 12.5, "resetsAt": "2026-11-14T09:00:00Z"},
            }
        ]
