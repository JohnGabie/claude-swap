"""limit_watch: limit hits and weighted tokens from new transcript lines (LH1, LH3, LH4, LH5).

Every transcript here is a temp file with fake lines built at run time.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from claude_swap import limit_watch
from claude_swap.limit_watch import LimitHit, LimitWatch

S1_TS_TEXT = "2026-11-27T22:21:22.437Z"
S1_TS = 1795818082.437
S1_TEXT = "You've hit your 5-hour limit · resets 8:50pm (America/Sao_Paulo)"
NOW = datetime(2026, 11, 27, 22, 30, 0, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def hit_line(text=S1_TEXT, ts=S1_TS_TEXT, *, kind="assistant", is_error=True,
             error="rate_limit", api_error="usage_limit_reached", newline=True) -> str:
    row = {
        "type": kind,
        "timestamp": ts,
        "message": {
            "model": "<synthetic>",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    if is_error is not None:
        row["isApiErrorMessage"] = is_error
    if error is not None:
        row["error"] = error
    if api_error is not None:
        row["apiError"] = api_error
    return json.dumps(row) + ("\n" if newline else "")


def usage_line(ts: float, model: str, kind: str = "assistant", mid=None, rid=None, **usage) -> str:
    row = {
        "type": kind,
        "timestamp": iso(ts),
        "message": {"model": model, "content": [{"type": "text", "text": "ok"}], "usage": usage},
    }
    if mid is not None:
        row["message"]["id"] = mid
    if rid is not None:
        row["requestId"] = rid
    return json.dumps(row) + "\n"


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    return root


def append(path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def started(projects, clock=None):
    watch = LimitWatch(projects, clock=clock or Clock())
    assert watch.poll() == []
    return watch


def windows(hits) -> list:
    return [hit.window for hit in hits]


def test_limit_hit_line_is_recognized(projects):
    log = projects / "-proj" / "s.jsonl"
    log.write_text(usage_line(NOW - 60, "claude-opus-5-5", output_tokens=1), encoding="utf-8")
    watch = started(projects)
    append(log, hit_line())
    hits = watch.poll()
    assert len(hits) == 1
    assert hits[0].ts == pytest.approx(S1_TS, abs=1e-6)
    assert hits[0].window == "5h"
    assert hits[0] == LimitHit(ts=hits[0].ts, window="5h")
    assert watch.poll() == []


def test_watch_starts_at_end_of_files(projects):
    log = projects / "-proj" / "old.jsonl"
    log.write_text(hit_line() + usage_line(NOW - 60, "claude-opus-5-5", output_tokens=100), encoding="utf-8")
    watch = LimitWatch(projects, clock=Clock())
    assert watch.poll() == []
    assert watch.poll() == []
    assert watch.tokens_since(0) == 0
    append(log, hit_line("You've hit your weekly limit"))
    assert windows(watch.poll()) == ["7d"]
    # A file that appears after the first poll is read from its start.
    (projects / "-new").mkdir()
    (projects / "-new" / "n.jsonl").write_text(hit_line(), encoding="utf-8")
    assert windows(watch.poll()) == ["5h"]


def test_subagent_transcripts_are_watched(projects):
    agent_log = projects / "-proj" / "session-1" / "subagents" / "agent-x.jsonl"
    agent_log.parent.mkdir(parents=True)
    agent_log.write_text(hit_line(), encoding="utf-8")
    watch = started(projects)
    assert watch.poll() == []
    append(agent_log, hit_line())
    assert windows(watch.poll()) == ["5h"]


def test_vanished_file_is_forgotten(projects):
    log = projects / "-proj" / "s.jsonl"
    log.write_text("{}\n", encoding="utf-8")
    watch = started(projects)
    log.unlink()
    assert watch.poll() == []
    assert log not in watch._offsets
    filler = usage_line(NOW - 60, "claude-opus-5-5", output_tokens=1)
    log.write_text(hit_line() + filler, encoding="utf-8")
    assert windows(watch.poll()) == ["5h"]


def test_future_lines_are_ignored(projects):
    clock = Clock()
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects, clock)
    assert limit_watch.FUTURE_SLACK_S == 60
    append(log, hit_line(ts=iso(NOW + 120)))
    append(log, usage_line(NOW + 120, "claude-opus-5-5", mid="m_far", rid="r_far", output_tokens=1000))
    assert watch.poll() == []
    assert watch.tokens_since(0) == 0
    append(log, hit_line(ts=iso(NOW + 30)))
    append(log, usage_line(NOW + 30, "claude-opus-5-5", mid="m_near", rid="r_near", output_tokens=100))
    append(log, usage_line(NOW + 60, "claude-opus-5-5", mid="m_edge", rid="r_edge", output_tokens=10))
    hits = watch.poll()
    assert windows(hits) == ["5h"]
    assert hits[0].ts == pytest.approx(NOW + 30, abs=1e-3)
    assert watch.tokens_since(0) == pytest.approx(550.0)


def test_limit_hit_logs_no_text_or_path(projects, caplog):
    caplog.set_level(logging.DEBUG)
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects)
    append(log, hit_line("You've hit your 5-hour limit · a@example.com"))
    append(log, "not json at all\n")
    hits = watch.poll()
    assert windows(hits) == ["5h"]
    assert "@" not in repr(hits)
    assert caplog.records, "the watcher logs each hit (window only)"
    for record in caplog.records:
        message = record.getMessage()
        assert "@" not in message
        assert str(projects) not in message
        assert ".jsonl" not in message
        assert "-proj" not in message


def test_weekly_text_maps_to_7d(projects):
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects)
    append(log, hit_line("You've hit your weekly limit · resets Dec 1"))
    append(log, hit_line("You've hit your Opus WEEK limit"))
    append(log, hit_line("You've hit your limit · resets 8:50pm"))
    assert windows(watch.poll()) == ["7d", "7d", None]


def test_rate_limit_without_api_error_counts(projects):
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects)
    append(log, hit_line(api_error=None))
    append(log, hit_line("Usage LIMIT reached", api_error=None))
    hits = watch.poll()
    assert windows(hits) == ["5h", None]
    assert hits[0].ts == pytest.approx(S1_TS, abs=1e-6)


def test_usage_limit_api_error_counts_alone(projects):
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects)
    append(log, hit_line("", error=None))
    assert windows(watch.poll()) == [None]


def test_other_api_errors_ignored(projects):
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects)
    ignored = [
        hit_line("API Error: Overloaded", error="server_error", api_error="overloaded_error"),
        hit_line("Server error, no limit hit", error="server_error", api_error=None),
        hit_line("Too many requests", api_error=None),
        hit_line(is_error=False),
        hit_line(is_error=None),
        hit_line(is_error="true"),
        hit_line(is_error=1),
        hit_line(kind="user"),
        hit_line(ts="not a time"),
        hit_line(ts=None),
        "not json\n",
        "[1, 2]\n",
        json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                    "timestamp": S1_TS_TEXT, "message": "limit"}) + "\n",
        json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                    "timestamp": S1_TS_TEXT, "message": {"content": "limit"}}) + "\n",
        json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                    "timestamp": S1_TS_TEXT, "message": {"content": [{"text": 5}]}}) + "\n",
        # Only the first content item is searched.
        json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                    "timestamp": S1_TS_TEXT,
                    "message": {"content": [{"type": "text", "text": "Request failed"},
                                            {"type": "text", "text": "You've hit your 5-hour limit"}]}}) + "\n",
    ]
    append(log, "".join(ignored))
    assert watch.poll() == []
    append(log, "".join(ignored) + hit_line())
    assert windows(watch.poll()) == ["5h"]


def test_partial_line_waits_for_newline(projects):
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects)
    line = hit_line(newline=False)
    append(log, line[:40])
    assert watch.poll() == []
    append(log, line[40:])
    assert watch.poll() == []
    append(log, "\n")
    assert windows(watch.poll()) == ["5h"]


def test_shrunk_file_restarts(projects):
    log = projects / "-proj" / "s.jsonl"
    filler = usage_line(NOW - 60, "claude-opus-5-5", output_tokens=1)
    log.write_text(filler * 10, encoding="utf-8")
    watch = started(projects)
    line = hit_line()
    assert len(line) < len(filler) * 10
    log.write_text(line, encoding="utf-8")
    assert windows(watch.poll()) == ["5h"]


def test_tokens_since_weights(projects):
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects)
    t1, t2 = NOW - 600, NOW - 300
    append(log, usage_line(t1, "claude-opus-5-5", input_tokens=10, output_tokens=100))
    append(log, usage_line(t2, "claude-sonnet-5", output_tokens=100, cache_read_input_tokens=1000))
    append(log, usage_line(t2, "claude-opus-5-5", kind="user", mid="m_user", rid="r_user", output_tokens=9999))
    append(log, hit_line(ts=iso(t2)))
    assert watch.poll() == [LimitHit(ts=pytest.approx(t2, abs=1e-3), window="5h")]
    # opus-5-5: 1.0 * (10 + 5*100) = 510; sonnet: 0.6 * (5*100 + 0.01*1000) = 306.
    assert watch.tokens_since(t1) == pytest.approx(816.0)
    assert watch.tokens_since(t1 + 1) == pytest.approx(306.0)
    assert watch.tokens_since(t2) == pytest.approx(306.0)
    assert watch.tokens_since(t2 + 1) == 0


def test_tokens_since_counts_each_message_once(projects):
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects)
    t1, t2 = NOW - 600, NOW - 300
    # The same message on 3 lines: 1.0 * 5 * 100 = 500, once.
    append(log, usage_line(t1, "claude-opus-5-5", mid="msg_1", rid="req_1", output_tokens=100) * 3)
    assert watch.poll() == []
    assert watch.tokens_since(0) == pytest.approx(500.0)
    # A repeat in a later poll still counts once; another request id is another message.
    append(log, usage_line(t1, "claude-opus-5-5", mid="msg_1", rid="req_1", output_tokens=100))
    append(log, usage_line(t1, "claude-opus-5-5", mid="msg_1", rid="req_2", output_tokens=10))
    watch.poll()
    assert watch.tokens_since(0) == pytest.approx(550.0)
    # With both ids missing, a line counts once by its timestamp.
    append(log, usage_line(t2, "claude-opus-5-5", output_tokens=1) * 2)
    append(log, usage_line(t2 + 1, "claude-opus-5-5", output_tokens=1))
    watch.poll()
    assert watch.tokens_since(t2) == pytest.approx(10.0)


def test_tokens_since_keeps_six_hours(projects):
    clock = Clock()
    log = projects / "-proj" / "s.jsonl"
    log.write_text("", encoding="utf-8")
    watch = started(projects, clock)
    assert limit_watch.KEEP_S == 6 * 3600
    append(log, usage_line(NOW - 6 * 3600 - 1, "claude-opus-5-5", output_tokens=1000))
    append(log, usage_line(NOW - 100, "claude-opus-5-5", output_tokens=100))
    watch.poll()
    assert watch.tokens_since(0) == pytest.approx(500.0)
    # poll drops the entries older than 6 h from memory.
    clock.now = NOW + 6 * 3600
    watch.poll()
    assert len(watch._entries) == 0
    assert len(watch._seen) == 0
    # tokens_since drops them too, with no poll between.
    append(log, usage_line(clock.now - 100, "claude-opus-5-5", output_tokens=100))
    watch.poll()
    assert watch.tokens_since(0) == pytest.approx(500.0)
    clock.now += 6 * 3600
    assert watch.tokens_since(0) == 0
    assert len(watch._entries) == 0


def test_unreadable_entries_are_skipped(projects):
    watch = started(projects)
    (projects / "-proj" / "broken.jsonl").symlink_to(projects / "missing-target")
    folder = projects / "-proj" / "folder.jsonl"
    folder.mkdir()
    (folder / "inner.txt").write_text("x", encoding="utf-8")
    (projects / "-proj" / "s.jsonl").write_text(hit_line(), encoding="utf-8")
    assert windows(watch.poll()) == ["5h"]
    assert watch.poll() == []
