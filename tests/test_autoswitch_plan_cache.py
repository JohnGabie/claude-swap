"""Tests for ``autoswitch._PlanCache`` — the engine's in-memory plan word cache.

Spec: docs/superpowers/specs/2026-11-10-balance-strategy-design.md, item S3d.
Project rules P1 (no email, token or credential in output or logs) and P2
(read-only).

Every test runs on fake credentials under ``tmp_path``: ``temp_home`` sets
``HOME`` (the backup folder resolves beneath it), the autouse conftest
fixtures stub the Keychain and the OAuth profile fetch, and ``no_network``
refuses every socket connection.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import socket
from pathlib import Path

import pytest

from claude_swap import autoswitch, macos_keychain, pace_feed
from claude_swap.autoswitch import SwitchEvent, _PlanCache
from claude_swap.models import Platform

from tests.test_autoswitch import EngineHarness, _usage

EMAIL_A = "canary.alice@example.com"
EMAIL_B = "canary.bob@example.com"
EMAIL_C = "canary.carol@example.com"
PLAN_WORDS = ("pro", "max", "ultra", "unknown")


def _token(num: str) -> str:
    return f"sk-ant-oat01-CANARYACCESS{num}"


def _creds(num: str, plan: str | None) -> str:
    oauth: dict = {
        "accessToken": _token(num),
        "refreshToken": f"sk-ant-ort01-CANARYREFRESH{num}",
        "expiresAt": 4102444800000,
    }
    if plan is not None:
        oauth["subscriptionType"] = plan
    return json.dumps({"claudeAiOauth": oauth})


class FakeReader:
    """Stands in for ``pace_feed.read_plan_for``; records each call."""

    def __init__(self, plans: dict[str, str] | None = None, exc=None):
        self.plans = plans or {}
        self.exc = exc
        self.calls: list[tuple[str, str, bool]] = []

    def __call__(self, switcher, number, email, is_active):
        self.calls.append((number, email, is_active))
        if self.exc is not None:
            raise self.exc
        return self.plans.get(number, "unknown")


class Fleet:
    """Account number -> email, and the active account, both changeable."""

    def __init__(self):
        self.emails = {"1": EMAIL_A, "2": EMAIL_B, "3": EMAIL_C}
        self.active: str | None = "1"

    def email_of(self, num: str) -> str:
        return self.emails.get(num, "")

    def active_of(self) -> str | None:
        return self.active


def _cache(reader: FakeReader, fleet: Fleet | None = None) -> _PlanCache:
    fleet = fleet or Fleet()
    return _PlanCache(
        object(), reader=reader, email_of=fleet.email_of, active_of=fleet.active_of
    )


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the plan cache made a network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr("urllib.request.urlopen", refuse)


def _tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        for name in dirnames:
            out[str((Path(dirpath) / name).relative_to(root)) + "/"] = "dir"
    return out


# ---------------------------------------------------------------------------
# Read counts
# ---------------------------------------------------------------------------


class TestReadCounts:
    def test_many_calls_read_once(self):
        reader = FakeReader({"1": "max"})
        cache = _cache(reader)

        plans = [cache.plan_of("1") for _ in range(50)]

        assert plans == ["max"] * 50
        assert reader.calls == [("1", EMAIL_A, True)]

    def test_the_active_flag_and_the_email_reach_the_reader(self):
        reader = FakeReader({"1": "max", "2": "pro"})
        cache = _cache(reader)

        assert cache.plan_of("1") == "max"
        assert cache.plan_of("2") == "pro"
        assert reader.calls == [("1", EMAIL_A, True), ("2", EMAIL_B, False)]

    def test_a_new_account_is_read(self):
        reader = FakeReader({"1": "max", "2": "pro"})
        cache = _cache(reader)
        cache.plan_of("1")

        assert cache.plan_of("2") == "pro"
        cache.plan_of("1")
        cache.plan_of("2")
        assert [c[0] for c in reader.calls] == ["1", "2"]

    def test_a_changed_email_is_read_again(self):
        fleet = Fleet()
        reader = FakeReader({"2": "pro"})
        cache = _cache(reader, fleet)
        cache.plan_of("2")

        fleet.emails["2"] = "canary.dave@example.com"
        reader.plans["2"] = "max"

        assert cache.plan_of("2") == "max"
        assert cache.plan_of("2") == "max"
        assert reader.calls == [
            ("2", EMAIL_B, False),
            ("2", "canary.dave@example.com", False),
        ]

    def test_invalidate_reads_that_account_again_only(self):
        reader = FakeReader({"1": "max", "2": "pro"})
        cache = _cache(reader)
        cache.plan_of("1")
        cache.plan_of("2")

        reader.plans["2"] = "ultra"
        cache.invalidate("2")

        assert cache.plan_of("1") == "max"
        assert cache.plan_of("2") == "ultra"
        assert [c[0] for c in reader.calls] == ["1", "2", "2"]

    def test_invalidate_of_an_unknown_account_is_a_no_op(self):
        cache = _cache(FakeReader())
        cache.invalidate("9")
        cache.invalidate(None)

    def test_invalidate_all_reads_every_account_again(self):
        reader = FakeReader({"1": "max", "2": "pro"})
        cache = _cache(reader)
        cache.plan_of("1")
        cache.plan_of("2")

        cache.invalidate_all()
        cache.plan_of("1")
        cache.plan_of("2")
        cache.plan_of("1")

        assert [c[0] for c in reader.calls] == ["1", "2", "1", "2"]

    def test_an_int_number_is_the_same_account(self):
        reader = FakeReader({"2": "pro"})
        cache = _cache(reader)

        assert cache.plan_of(2) == "pro"
        assert cache.plan_of("2") == "pro"
        cache.invalidate(2)
        cache.plan_of("2")
        assert reader.calls == [("2", EMAIL_B, False)] * 2


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


class TestFailures:
    def test_a_reader_failure_is_unknown_and_is_not_read_again(self, capsys, caplog):
        caplog.set_level("DEBUG")
        reader = FakeReader(
            exc=OSError(f"cannot read .creds-2-{EMAIL_B}.enc token={_token('2')}")
        )
        cache = _cache(reader)

        plans = [cache.plan_of("2") for _ in range(10)]

        assert plans == ["unknown"] * 10
        assert len(reader.calls) == 1
        captured = capsys.readouterr()
        for text in (captured.out, captured.err, caplog.text):
            assert EMAIL_B not in text and _token("2") not in text
        assert caplog.records == []

        reader.exc = None
        reader.plans["2"] = "pro"
        assert cache.plan_of("2") == "unknown"
        cache.invalidate("2")
        assert cache.plan_of("2") == "pro"
        assert len(reader.calls) == 2

    def test_a_word_outside_the_plans_is_unknown(self):
        reader = FakeReader({"1": _token("1"), "2": "MAX"})
        cache = _cache(reader)

        assert cache.plan_of("1") == "unknown"
        assert cache.plan_of("2") == "unknown"
        assert cache.plan_of("1") == "unknown"
        assert len(reader.calls) == 2

    def test_a_failed_email_lookup_is_unknown_without_a_credential_read(self):
        def broken(num):
            raise RuntimeError(f"sequence unreadable {EMAIL_A}")

        reader = FakeReader({"1": "max"})
        cache = _PlanCache(object(), reader=reader, email_of=broken, active_of=lambda: "1")

        assert cache.plan_of("1") == "unknown"
        assert reader.calls == []

    def test_a_failed_active_lookup_is_unknown_and_not_cached(self):
        # Changed from test_a_failed_active_lookup_is_unknown_and_cached
        # (EXE3, Reviewer rejection): a failed active lookup is "unknown" and
        # is NOT cached, so a later call reads again.
        def broken():
            raise RuntimeError(f"config unreadable {EMAIL_A}")

        reader = FakeReader({"1": "max"})
        cache = _PlanCache(
            object(), reader=reader, email_of=Fleet().email_of, active_of=broken
        )

        assert cache.plan_of("1") == "unknown"
        assert cache.plan_of("1") == "unknown"
        assert reader.calls == []
        assert cache._entries == {}

    def test_a_temporary_active_lookup_failure_is_not_kept(self):
        state = {"fail": True}

        def flaky():
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("config unreadable")
            return "1"

        reader = FakeReader({"1": "max"})
        cache = _PlanCache(
            object(), reader=reader, email_of=Fleet().email_of, active_of=flaky
        )

        assert cache.plan_of("1") == "unknown"
        assert reader.calls == []
        assert cache.plan_of("1") == "max"
        assert reader.calls == [("1", EMAIL_A, True)]
        assert cache.plan_of("1") == "max"
        assert len(reader.calls) == 1


# ---------------------------------------------------------------------------
# No identity leaves the cache (P1)
# ---------------------------------------------------------------------------


class TestNoIdentity:
    def test_the_api_has_no_method_that_returns_the_email(self):
        reader = FakeReader({"1": "max", "2": "pro"})
        cache = _cache(reader)
        cache.plan_of("1")
        cache.plan_of("2")

        public = {n for n in dir(cache) if not n.startswith("_")}
        assert public == {"plan_of", "invalidate", "invalidate_all"}
        for text in (repr(cache), str(cache)):
            assert EMAIL_A not in text and EMAIL_B not in text
            assert "max" not in text and "pro" not in text
        # Nothing the object holds is the email: it keeps a digest only.
        assert not hasattr(cache, "__dict__")
        for slot in type(cache).__slots__:
            assert EMAIL_A not in repr(getattr(cache, slot))
            assert EMAIL_B not in repr(getattr(cache, slot))

    def test_the_cache_code_logs_writes_and_emits_nothing(self):
        tree = ast.parse(inspect.getsource(_PlanCache))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        forbidden = {
            "_logger", "logger", "logging", "print", "warning", "error", "info",
            "debug", "exception", "_emit", "on_event", "open", "write_text",
            "write_bytes", "atomic_write_json", "_write_json", "_mutate_state",
            "_write_account_credentials", "extract_oauth_data",
            "extract_access_token", "extract_refresh_token", "format_exc",
        }
        assert not names & forbidden, names & forbidden

    def test_the_cache_writes_no_file(self, temp_home, no_network):
        h = EngineHarness(temp_home)
        h.seed(1, EMAIL_A)
        h.seed(2, EMAIL_B)
        h.make_live(EMAIL_A, 1)
        h.switcher._write_account_credentials("2", EMAIL_B, _creds("2", "pro"))
        (temp_home / ".claude" / ".credentials.json").write_text(_creds("1", "max"))
        before = _tree_hashes(temp_home)

        cache = _PlanCache(h.switcher)
        plans = [cache.plan_of(n) for n in ("1", "2", "1", "2")]
        cache.invalidate_all()
        plans += [cache.plan_of(n) for n in ("1", "2")]

        assert plans == ["max", "pro", "max", "pro", "max", "pro"]
        assert _tree_hashes(temp_home) == before

    def test_state_file_and_events_never_hold_a_plan(self, temp_home, no_network):
        h = EngineHarness(temp_home)
        h.seed(1, EMAIL_A)
        h.seed(2, EMAIL_B)
        h.seed(3, EMAIL_C)
        h.make_live(EMAIL_A, 1)
        for num, email, plan in (("1", EMAIL_A, "max"), ("2", EMAIL_B, "ultra"),
                                 ("3", EMAIL_C, "pro")):
            h.switcher._write_account_credentials(num, email, _creds(num, plan))
        (temp_home / ".claude" / ".credentials.json").write_text(_creds("1", "max"))
        # The engine's own cache is warm, as S3b-2 will make it every tick.
        assert [h.engine._plan_cache.plan_of(n) for n in ("1", "2", "3")] == [
            "max", "ultra", "pro",
        ]

        h.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})

        assert any(isinstance(e, SwitchEvent) for e in h.events)
        texts = [json.dumps(e.to_json()) for e in h.events]
        texts += [e.human() for e in h.events]
        texts.append(json.dumps(h.state()))
        for text in texts:
            for word in ("ultra", '"max"', '"pro"', "subscriptionType", '"plan"'):
                assert word not in text, (word, text)
            assert _token("1") not in text and _token("2") not in text
        assert EMAIL_A not in json.dumps(h.state())
        assert EMAIL_B not in json.dumps(h.state())


# ---------------------------------------------------------------------------
# The engine: a fresh cache per engine, invalidation after a switch
# ---------------------------------------------------------------------------


class TestEngineWiring:
    def _harness(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, EMAIL_A)
        h.seed(2, EMAIL_B)
        h.seed(3, EMAIL_C)
        h.make_live(EMAIL_A, 1)
        return h

    def test_each_engine_has_its_own_empty_cache(self, temp_home, monkeypatch):
        h = self._harness(temp_home)
        reader = FakeReader({"1": "max"})
        monkeypatch.setattr(autoswitch.pace_feed, "read_plan_for", reader)
        h.engine._plan_cache.plan_of("1")

        other = h._make_engine()
        other._plan_cache.plan_of("1")

        assert other._plan_cache is not h.engine._plan_cache
        assert len(reader.calls) == 2

    def test_a_performed_switch_invalidates_the_old_and_the_new_account(
        self, temp_home, monkeypatch, no_network
    ):
        h = self._harness(temp_home)
        reader = FakeReader({"1": "max", "2": "pro", "3": "pro"})
        monkeypatch.setattr(autoswitch.pace_feed, "read_plan_for", reader)
        cache = h.engine._plan_cache
        for n in ("1", "2", "3"):
            cache.plan_of(n)
        assert reader.calls == [
            ("1", EMAIL_A, True), ("2", EMAIL_B, False), ("3", EMAIL_C, False),
        ]
        reader.calls.clear()

        h.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})
        assert h.active_number() == 2
        for n in ("1", "2", "3"):
            cache.plan_of(n)

        # 1 and 2 are read again with the new active flag; 3 is still cached.
        assert sorted(reader.calls) == [("1", EMAIL_A, False), ("2", EMAIL_B, True)]

    def test_a_dry_run_switch_invalidates_nothing(self, temp_home, monkeypatch):
        h = self._harness(temp_home)
        engine = h._make_engine(dry_run=True)
        h.engine = engine
        reader = FakeReader({"1": "max", "2": "pro", "3": "pro"})
        monkeypatch.setattr(autoswitch.pace_feed, "read_plan_for", reader)
        for n in ("1", "2", "3"):
            engine._plan_cache.plan_of(n)
        reader.calls.clear()

        h.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})
        assert any(isinstance(e, SwitchEvent) and e.dry_run for e in h.events)
        for n in ("1", "2", "3"):
            engine._plan_cache.plan_of(n)

        assert reader.calls == []

    def test_a_tick_does_not_read_a_plan(self, temp_home, monkeypatch):
        h = self._harness(temp_home)
        reader = FakeReader()
        monkeypatch.setattr(autoswitch.pace_feed, "read_plan_for", reader)

        h.tick_with_usage({"1": _usage(10), "2": _usage(10), "3": _usage(50)})
        h.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})

        assert reader.calls == []


# ---------------------------------------------------------------------------
# macOS: the active account's Keychain read happens once until invalidated
# ---------------------------------------------------------------------------


class TestMacOSActiveRead:
    def test_active_keychain_read_once_per_account(
        self, temp_home, monkeypatch, no_network, block_real_keychain
    ):
        h = EngineHarness(temp_home)
        h.seed(1, EMAIL_A)
        h.seed(2, EMAIL_B)
        h.make_live(EMAIL_A, 1)
        h.switcher._write_account_credentials("2", EMAIL_B, _creds("2", "pro"))
        (temp_home / ".claude" / ".credentials.json").write_text(_creds("1", "max"))
        h.switcher.platform = Platform.MACOS

        active_reads: list = []
        backup_reads: list = []
        keychain_reads: list = []
        real_active = h.switcher._read_active_credentials
        real_backup = h.switcher._read_account_credentials
        real_get = macos_keychain.get_password

        def count_active(*a, **k):
            active_reads.append(1)
            return real_active(*a, **k)

        def count_backup(*a, **k):
            backup_reads.append(1)
            return real_backup(*a, **k)

        def count_keychain(*a, **k):
            keychain_reads.append(1)
            return real_get(*a, **k)

        monkeypatch.setattr(h.switcher, "_read_active_credentials", count_active)
        monkeypatch.setattr(h.switcher, "_read_account_credentials", count_backup)
        monkeypatch.setattr(macos_keychain, "get_password", count_keychain)

        cache = _PlanCache(h.switcher)
        assert cache.plan_of("1") == "max"
        assert cache.plan_of("2") == "pro"
        first_keychain = len(keychain_reads)
        assert first_keychain >= 1  # the active read reached the Keychain
        for _ in range(100):
            assert cache.plan_of("1") == "max"
            assert cache.plan_of("2") == "pro"

        assert len(active_reads) == 1
        assert len(backup_reads) == 1
        assert len(keychain_reads) == first_keychain

        cache.invalidate("1")
        assert cache.plan_of("1") == "max"
        assert len(active_reads) == 2
        assert len(backup_reads) == 1
