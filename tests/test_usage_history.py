"""usage_history: incremental cache of weighted tokens per 5-minute bucket (FP11, FP7)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone

import pytest

from claude_swap import token_model, usage_history

HOME = "/" + "home" + "/" + "u"
NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc).timestamp()
T0 = NOW - 3600  # bucket aligned: NOW is a multiple of 300


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def usage(out=100, inp=10):
    return {"input_tokens": inp, "output_tokens": out}


def line(ts, mid="m1", rid="r1", model="claude-opus-5-5", cwd=None, use=None, **extra):
    row = {"type": "assistant", "timestamp": iso(ts), "requestId": rid, "message": {"model": model}}
    if mid is not None:
        row["message"]["id"] = mid
    if use is not False:
        row["message"]["usage"] = use or usage()
    if cwd is not None:
        row["cwd"] = cwd
    row.update(extra)
    return json.dumps(row) + "\n"


def weight(model="claude-opus-5-5", use=None):
    return token_model.weighted_tokens(model, use or usage())


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "root"
    projects = tmp_path / "projects"
    projects.mkdir()
    return root, projects


def cache_path(root):
    return root / "cache" / usage_history.HISTORY_FILENAME


def total_m(history):
    return sum(sum(b["m"].values()) for b in history["buckets"].values())


def test_incremental_update_without_text(env):
    root, projects = env
    log = projects / "s.jsonl"
    secret = json.dumps({"type": "user", "timestamp": iso(T0), "message": {"content": "secret"}}) + "\n"
    log.write_text(line(T0, "a", "r1") + line(T0 + 1, "a", "r1") + secret + line(T0 + 10, "b", "r2"))
    usage_history.update(root, projects, NOW, home=HOME)
    log.write_text(log.read_text() + line(T0 + 20, "c", "r3"))
    hist = usage_history.update(root, projects, NOW, home=HOME)
    assert total_m(hist) == pytest.approx(3 * weight())
    text = cache_path(root).read_text()
    assert "secret" not in text and "/" not in text
    (entry,) = hist["files"].values()
    assert entry["offset"] == log.stat().st_size
    assert hist["schemaVersion"] == 1


def test_project_keys():
    t = ("/tmp",)
    cases = [
        (HOME + "/projects/guilda", HOME, "guilda"),
        (HOME + "/projects/" + "claude-swap" + ".worktrees/feat-x/src", HOME, "claude-swap"),
        (HOME + "/.claude/projects/x", HOME, "claude (memória)"),
        ("/tmp/lab1", HOME, "temporários"),
        (HOME, HOME, "pasta pessoal"),
        (HOME + "/projects", HOME, "pasta pessoal"),
        ("C:\\Users\\u\\projects\\glasshaus", "C:\\Users\\u", "glasshaus"),
        ("/opt/x", HOME, "outras pastas"),
        (None, HOME, "outras pastas"),
    ]
    assert [usage_history.project_key(c, h, t) for c, h, _ in cases] == [k for _, _, k in cases]
    assert usage_history.project_key("/var/tmp/z", HOME) == "temporários"
    assert usage_history.project_key(HOME + "/Downloads/x", HOME) == "pasta pessoal"
    assert usage_history.project_key("/scratch/q/a", HOME, ("/scratch/q",)) == "temporários"
    assert usage_history.project_key(HOME + "2/projects/x", HOME) == "outras pastas"


def test_update_stores_project_key_not_path(env):
    root, projects = env
    (projects / "s.jsonl").write_text(line(T0, cwd=HOME + "/projects/guilda/src"))
    hist = usage_history.update(root, projects, NOW, home=HOME)
    (bucket,) = hist["buckets"].values()
    assert bucket["p"] == {"guilda": pytest.approx(weight())}
    assert bucket["m"] == {"claude-opus-5-5": pytest.approx(weight())}
    assert "guilda/src" not in cache_path(root).read_text()


def test_shrunk_file_read_again_with_keys_cleared(env):
    root, projects = env
    log = projects / "s.jsonl"
    log.write_text(line(T0, "a", "r1") + line(T0 + 1, "b", "r2"))
    first = usage_history.update(root, projects, NOW, home=HOME)
    assert total_m(first) == pytest.approx(2 * weight())
    log.write_text(line(T0 + 5, "a", "r1"))  # shorter; key a|r1 was seen before the shrink
    second = usage_history.update(root, projects, NOW, home=HOME)
    assert total_m(second) == pytest.approx(3 * weight())
    (entry,) = second["files"].values()
    assert entry["keys"] == ["a|r1"]


def test_message_key_repeated_across_updates_counted_once(env):
    root, projects = env
    log = projects / "s.jsonl"
    log.write_text(line(T0, "a", "r1"))
    usage_history.update(root, projects, NOW, home=HOME)
    log.write_text(log.read_text() + line(T0 + 2, "a", "r1") + line(T0 + 3, "b", "r2"))
    hist = usage_history.update(root, projects, NOW, home=HOME)
    assert total_m(hist) == pytest.approx(2 * weight())


def test_message_key_without_ids_uses_timestamp(env):
    root, projects = env
    log = projects / "s.jsonl"
    row = json.loads(line(T0, None))
    row.pop("requestId", None)
    log.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    hist = usage_history.update(root, projects, NOW, home=HOME)
    assert total_m(hist) == pytest.approx(weight())
    (entry,) = hist["files"].values()
    assert entry["keys"] == ["|" + row["timestamp"]]


def test_keys_keep_last_500(env):
    root, projects = env
    log = projects / "s.jsonl"
    log.write_text("".join(line(T0 + i, f"m{i}", "r") for i in range(520)))
    hist = usage_history.update(root, projects, NOW, home=HOME)
    (entry,) = hist["files"].values()
    assert len(entry["keys"]) == 500 and entry["keys"][-1] == "m519|r"


def test_line_without_usage_skipped(env):
    root, projects = env
    log = projects / "s.jsonl"
    null_usage = json.loads(line(T0, "n"))
    null_usage["message"]["usage"] = None
    no_ts = json.loads(line(T0, "t"))
    del no_ts["timestamp"]
    log.write_text(
        line(T0, "a", use=False)
        + json.dumps(null_usage) + "\n"
        + json.dumps(no_ts) + "\n"
        + "not json \"usage\"\n"
        + line(T0 + 1, "b", "r2")
    )
    hist = usage_history.update(root, projects, NOW, home=HOME)
    assert total_m(hist) == pytest.approx(weight())


def test_partial_last_line_waits_for_newline(env):
    root, projects = env
    log = projects / "s.jsonl"
    whole = line(T0, "a", "r1")
    second = line(T0 + 1, "b", "r2")
    log.write_text(whole + second[:-1])  # no newline yet
    hist = usage_history.update(root, projects, NOW, home=HOME)
    assert total_m(hist) == pytest.approx(weight())
    (entry,) = hist["files"].values()
    assert entry["offset"] == len(whole.encode())
    log.write_text(whole + second)
    hist = usage_history.update(root, projects, NOW, home=HOME)
    assert total_m(hist) == pytest.approx(2 * weight())


def test_old_buckets_and_deleted_files_dropped(env):
    root, projects = env
    log = projects / "s.jsonl"
    gone = projects / "gone.jsonl"
    old = NOW - usage_history.KEEP_S - 3600
    log.write_text(line(old, "o", "r") + line(T0, "n", "r"))
    gone.write_text(line(T0 + 1, "g", "r"))
    first = usage_history.update(root, projects, NOW, home=HOME)
    assert len(first["files"]) == 2
    gone.unlink()
    second = usage_history.update(root, projects, NOW, home=HOME)
    assert len(second["files"]) == 1
    assert list(second["files"]) == [hashlib.sha256(str(log).encode()).hexdigest()]
    assert all(int(k) >= NOW - usage_history.KEEP_S for k in second["buckets"])
    assert total_m(second) == pytest.approx(2 * weight())  # "o" dropped; "g" stays (its bucket is recent)


def test_stored_bucket_dropped_when_it_ages_out(env):
    root, projects = env
    log = projects / "s.jsonl"
    log.write_text(line(T0, "a"))
    first = usage_history.update(root, projects, NOW, home=HOME)
    assert len(first["buckets"]) == 1
    later = NOW + usage_history.KEEP_S
    os.utime(log, (later, later))
    second = usage_history.update(root, projects, later, home=HOME)
    assert second["buckets"] == {}


def test_file_older_than_31_days_not_read(env):
    root, projects = env
    log = projects / "s.jsonl"
    log.write_text(line(T0, "a"))
    stale = NOW - usage_history.KEEP_S - 86400
    os.utime(log, (stale, stale))
    hist = usage_history.update(root, projects, NOW, home=HOME)
    assert hist["files"] == {} and hist["buckets"] == {}


def test_load_bad_file(env):
    root, _ = env
    empty = usage_history.load(root)
    assert empty["files"] == {} and empty["buckets"] == {} and empty["schemaVersion"] == 1
    path = cache_path(root)
    path.parent.mkdir(parents=True)
    for content in ("{not json", "[]", json.dumps({"schemaVersion": 2, "files": {}, "buckets": {"300": {"m": {"a": 1.0}, "p": {}}}}),
                    json.dumps({"schemaVersion": 1, "files": [], "buckets": {}})):
        path.write_text(content)
        assert usage_history.load(root) == empty
    path.unlink()
    path.mkdir()  # unreadable as a file
    assert usage_history.load(root) == empty


def test_load_round_trip(env):
    root, projects = env
    (projects / "s.jsonl").write_text(line(T0))
    written = usage_history.update(root, projects, NOW, home=HOME)
    assert usage_history.load(root) == written


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_cache_file_mode_0600(env):
    root, projects = env
    (projects / "s.jsonl").write_text(line(T0))
    usage_history.update(root, projects, NOW, home=HOME)
    assert stat.S_IMODE(cache_path(root).stat().st_mode) == 0o600


def hand_history():
    # bucket starts: 1200 (m a 300, p x 300), 1500 (m a 60, m b 120; p x 180), 3000 (m b 600; p y 600)
    base = 3_000_000
    def b(off, m, p):
        return str(base + off), {"m": m, "p": p}
    buckets = dict(
        [
            b(0, {"a": 300.0}, {"x": 300.0}),
            b(300, {"a": 60.0, "b": 120.0}, {"x": 180.0}),
            b(1800, {"b": 600.0}, {"y": 600.0}),
        ]
    )
    return {"schemaVersion": 1, "updatedAt": "x", "files": {}, "buckets": buckets}, base


def test_series():
    hist, base = hand_history()
    # step 600 s over [base, base+2400): 4 steps; weighted per minute = sum * 60 / step
    out = usage_history.series(hist, base, base + 2400, 600)
    assert out == pytest.approx([(300 + 180) * 60 / 600, 0.0, 0.0, 600 * 60 / 600])
    assert len(usage_history.series(hist, base, base + 2401, 600)) == 5
    # end exclusive, start inclusive
    assert usage_history.series(hist, base + 300, base + 600, 300) == pytest.approx([180 * 60 / 300])
    assert usage_history.series(hist, base + 1, base + 300, 300) == [0.0]


def test_totals():
    hist, base = hand_history()
    assert usage_history.totals(hist, base, base + 3000, "m") == pytest.approx({"a": 360.0, "b": 720.0})
    assert usage_history.totals(hist, base, base + 3000, "p") == pytest.approx({"x": 480.0, "y": 600.0})
    assert usage_history.totals(hist, base + 300, base + 1800, "m") == pytest.approx({"a": 60.0, "b": 120.0})
    assert usage_history.totals(hist, base + 9000, base + 9600, "p") == {}


def test_grid():
    hist, base = hand_history()
    out = usage_history.grid(hist, base, base + 2400, 1200, ["x"])
    assert out == pytest.approx({"x": [480.0, 0.0], "__other__": [0.0, 600.0]})
    out = usage_history.grid(hist, base, base + 2400, 1200, ["x", "y"])
    assert out["y"] == pytest.approx([0.0, 600.0]) and out["__other__"] == [0.0, 0.0]
