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


def run(env, text, binary=False):
    root, projects = env
    log = projects / "s.jsonl"
    if binary:
        log.write_bytes(text)
    else:
        log.write_text(text, encoding="utf-8")
    return usage_history.update(root, projects, NOW, home=HOME), log


def test_line_separators_inside_text_do_not_split_a_message(env):
    row = json.loads(line(T0, "a", "r1"))
    row["message"]["content"] = "x y\u0085z\x0b\x0c\x1c\x1d\x1e"
    hist, _ = run(env, json.dumps(row, ensure_ascii=False) + "\n")
    assert total_m(hist) == pytest.approx(weight())


def test_bucket_key_is_exact(env):
    hist, _ = run(env, line(T0 + 10, "a", "r1"))
    assert list(hist["buckets"]) == [str(int(T0))]


def test_updated_at_is_exact(env):
    hist, _ = run(env, line(T0))
    assert hist["updatedAt"] == "2026-11-21T12:00:00Z"
    assert usage_history.load(env[0])["updatedAt"] == "2026-11-21T12:00:00Z"


def test_message_key_with_one_id_missing(env):
    only_rid = json.loads(line(T0, None, "r9"))
    only_mid = json.loads(line(T0, "m9", None))
    del only_mid["requestId"]
    hist, _ = run(env, json.dumps(only_rid) + "\n" + json.dumps(only_mid) + "\n")
    (entry,) = hist["files"].values()
    assert entry["keys"] == ["|r9", "m9|"]
    assert total_m(hist) == pytest.approx(2 * weight())


@pytest.mark.parametrize("name", ["TMPDIR", "TEMP", "TMP"])
def test_temp_dirs_read_environment(env, monkeypatch, name):
    for other in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "/scratch/fake-tmp")
    assert "/scratch/fake-tmp" in usage_history._temp_dirs()
    hist, _ = run(env, line(T0, cwd="/scratch/fake-tmp/job"))
    (bucket,) = hist["buckets"].values()
    assert list(bucket["p"]) == ["temporários"]


def test_zero_usage_adds_no_bucket_but_keeps_key(env):
    zero = {"input_tokens": 0, "output_tokens": 0}
    hist, _ = run(env, line(T0, "z", "r", use=zero))
    assert hist["buckets"] == {}
    (entry,) = hist["files"].values()
    assert entry["keys"] == ["z|r"]
    both = line(T0, "z", "r", use=zero) + line(T0 + 1, "y", "r")
    hist, _ = run(env, both)
    (bucket,) = hist["buckets"].values()
    assert bucket["m"] == {"claude-opus-5-5": pytest.approx(weight())}


def test_missing_model_is_unknown(env):
    row = json.loads(line(T0))
    del row["message"]["model"]
    hist, _ = run(env, json.dumps(row) + "\n")
    (bucket,) = hist["buckets"].values()
    assert list(bucket["m"]) == ["unknown"]


def test_project_key_with_at_sign_is_personal():
    assert usage_history.project_key(HOME + "/projects/x@y.com", HOME) == "pasta pessoal"
    assert usage_history.project_key(HOME + "/projects/x@y.com/src", HOME) == "pasta pessoal"
    assert usage_history.project_key(HOME + "/projects/ok", HOME) == "ok"


def test_load_drops_non_numeric_values(env):
    root, _ = env
    path = cache_path(root)
    path.parent.mkdir(parents=True)
    data = {
        "schemaVersion": 1, "updatedAt": "x", "files": {},
        "buckets": {"300": {"m": {"a": 1.5, "b": "x", "c": None, "d": True, "e": 2}, "p": {"x": [1], "y": 3.0}}},
    }
    path.write_text(json.dumps(data))
    out = usage_history.load(root)
    assert out["buckets"]["300"] == {"m": {"a": 1.5, "e": 2}, "p": {"y": 3.0}}


def test_deep_nesting_and_huge_number_do_not_stop_the_update(env):
    deep = '{"usage": ' + "[" * 100000 + "\n"
    huge = json.loads(line(T0 + 1, "h", "r"))
    huge["message"]["usage"]["output_tokens"] = 10**400
    good = line(T0 + 2, "g", "r")
    text = line(T0, "a", "r") + deep + json.dumps(huge) + "\n" + good
    hist, log = run(env, text)
    assert total_m(hist) == pytest.approx(2 * weight())
    (entry,) = hist["files"].values()
    assert entry["offset"] == log.stat().st_size
    assert cache_path(env[0]).exists()


def test_ids_and_model_limited_to_200_chars(env):
    long_id = "i" * 201
    ok_id = "i" * 200
    rows = [json.loads(line(T0, long_id, "r1", model="m" * 201)), json.loads(line(T0 + 1, ok_id, "r2"))]
    rows[0]["message"]["usage"] = usage()
    nonstr = json.loads(line(T0 + 2, 5, 7))
    hist, _ = run(env, "".join(json.dumps(r) + "\n" for r in rows + [nonstr]))
    (entry,) = hist["files"].values()
    assert entry["keys"] == ["|r1", f"{ok_id}|r2", "|" + nonstr["timestamp"]]
    models = {m for b in hist["buckets"].values() for m in b["m"]}
    assert "m" * 201 not in models and "unknown" in models


def test_assistant_line_stores_only_key_and_counts(env):
    row = json.loads(line(T0, "a", "r1", cwd=HOME + "/projects/who@mail.example"))
    row["message"]["content"] = [{"type": "text", "text": "secret-text"}]
    hist, _ = run(env, json.dumps(row) + "\n")
    text = cache_path(env[0]).read_text()
    assert "secret-text" not in text and "@" not in text and "/" not in text
    (bucket,) = hist["buckets"].values()
    assert list(bucket["p"]) == ["pasta pessoal"]


def test_update_reads_line_by_line_not_whole_tail(env, monkeypatch):
    root, projects = env
    log = projects / "s.jsonl"
    log.write_text("".join(line(T0 + i, f"m{i}", "r") for i in range(50)))
    reads = []
    real = type(log).open

    def spy(self, *a, **k):
        handle = real(self, *a, **k)
        if self.name != "s.jsonl":
            return handle
        orig = handle.read

        def spy_read(*args):
            reads.append(args)
            return orig(*args)

        class Wrap:
            def __getattr__(self, n):
                return getattr(handle, n)
            def __enter__(self):
                handle.__enter__()
                return self
            def __exit__(self, *e):
                return handle.__exit__(*e)
            def __iter__(self):
                return iter(handle)
            read = staticmethod(spy_read)

        return Wrap()

    monkeypatch.setattr(type(log), "open", spy)
    hist = usage_history.update(root, projects, NOW, home=HOME)
    assert total_m(hist) == pytest.approx(50 * weight())
    assert reads == []
