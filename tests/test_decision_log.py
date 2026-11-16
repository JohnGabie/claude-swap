"""Decision log: scrubbed lines, rotation, and the bug report text."""

import json

from claude_swap import decision_log
from claude_swap.decision_log import DecisionLog, build_report, scrub, tail


class FakeEvent:
    def __init__(self, payload):
        self._payload = payload

    def to_json(self):
        return self._payload


SWITCH = {
    "schemaVersion": 1,
    "event": "switch",
    "ts": "2026-11-16T12:00:00+00:00",
    "trigger": "balanced",
    "from": {"number": 3, "email": "a.person@example.com", "organizationName": "Acme"},
    "to": {"number": 6, "email": "other@example.org"},
    "warnings": ["token of other@example.org is near expiry"],
}


def test_scrub_drops_private_keys_and_masks_emails():
    out = scrub(SWITCH)
    assert out["from"] == {"number": 3}
    assert out["to"] == {"number": 6}
    assert out["warnings"] == ["token of <email> is near expiry"]
    assert out["trigger"] == "balanced"
    assert "@" not in json.dumps(out)


def test_scrub_masks_an_email_used_as_a_key():
    assert scrub({"a@b.co": 1}) == {"<email>": 1}


def test_write_appends_one_scrubbed_json_line(tmp_path):
    log = DecisionLog(tmp_path)
    log.write(FakeEvent(SWITCH))
    log.write(FakeEvent({"event": "no-switch", "reason": "no-return"}))
    lines = (tmp_path / "decisions.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["to"] == {"number": 6}
    assert json.loads(lines[1])["reason"] == "no-return"
    assert "@" not in "".join(lines)


def test_write_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    DecisionLog(blocker / "sub").write(FakeEvent(SWITCH))  # parent is a file

    class Broken:
        def to_json(self):
            raise RuntimeError("boom")

    DecisionLog(tmp_path).write(Broken())


def test_rotation_keeps_a_bounded_number_of_files(tmp_path):
    log = DecisionLog(tmp_path, max_bytes=200, backups=2)
    for i in range(60):
        log.write(FakeEvent({"event": "poll", "i": i}))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["decisions.jsonl", "decisions.jsonl.1", "decisions.jsonl.2"]
    assert all(p.stat().st_size <= 200 for p in tmp_path.iterdir())


def test_tail_reads_across_rotated_files_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(decision_log, "BACKUPS", 2)
    log = DecisionLog(tmp_path, max_bytes=200, backups=2)
    for i in range(60):
        log.write(FakeEvent({"event": "poll", "i": i}))
    got = [json.loads(line)["i"] for line in tail(tmp_path, 12)]
    assert got == list(range(48, 60))


def test_report_has_no_email_and_shows_every_part(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"autoswitch": {"threshold": 98.0}}))
    (tmp_path / "autoswitch_state.json").write_text(
        json.dumps({"lastSwitchAt": 1.0, "accounts": {"3": {"email": "a.person@example.com", "pct": 40}}})
    )
    DecisionLog(tmp_path).write(FakeEvent(SWITCH))
    text = build_report(tmp_path, version="9.9")
    assert "version 9.9" in text
    assert '"threshold": 98.0' in text
    assert '"pct": 40' in text
    assert '"trigger":"balanced"' in text
    assert "@" not in text


def test_report_on_an_empty_folder(tmp_path):
    text = build_report(tmp_path)
    assert "(no file)" in text
    assert "(no decisions logged)" in text


# ---- engine wiring and the CLI command -------------------------------------

def test_engine_emit_writes_the_log_before_the_frontend(tmp_path):
    from claude_swap.autoswitch import AutoSwitchEngine, NoSwitchEvent

    seen = []
    engine = AutoSwitchEngine.__new__(AutoSwitchEngine)
    engine._decision_log = DecisionLog(tmp_path)
    engine.on_event = lambda ev: seen.append((tmp_path / "decisions.jsonl").read_text())
    engine._emit(NoSwitchEvent(reason="no-return", detail="held for a.person@example.com"))
    assert len(seen) == 1
    line = json.loads(seen[0])
    assert line["event"] == "no-switch" and line["reason"] == "no-return"
    assert "@" not in seen[0]


def test_debug_report_command_prints_a_scrubbed_report(tmp_path, monkeypatch, capsys):
    import pytest

    from claude_swap import cli, paths

    (tmp_path / "settings.json").write_text(json.dumps({"autoswitch": {"threshold": 95.0}}))
    DecisionLog(tmp_path).write(FakeEvent(SWITCH))
    monkeypatch.setattr(paths, "get_backup_root", lambda: tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        cli._debug_report_command([])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert '"threshold": 95.0' in out and '"trigger":"balanced"' in out
    assert "@" not in out


def test_debug_report_command_writes_a_file(tmp_path, monkeypatch, capsys):
    import pytest

    from claude_swap import cli, paths

    monkeypatch.setattr(paths, "get_backup_root", lambda: tmp_path)
    target = tmp_path / "report.txt"
    with pytest.raises(SystemExit):
        cli._debug_report_command(["-o", str(target)])
    assert "(no decisions logged)" in target.read_text()
