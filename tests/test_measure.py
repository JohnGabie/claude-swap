"""Measurement records: tasks, measured stretches, estimates, fits."""

import json
from datetime import datetime, timezone

import pytest

from claude_swap import measure, usage_log

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc).timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def msg(ts, mid, session="s1", agent=None, model="claude-opus-5-5", inp=0, out=0, create=0, read=0):
    stamp = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    row = {"timestamp": stamp, "requestId": "r" + mid, "sessionId": session, "type": "assistant",
           "message": {"id": mid, "model": model, "content": "secret text a.person@example.com",
                       "usage": {"input_tokens": inp, "output_tokens": out,
                                 "cache_creation_input_tokens": create, "cache_read_input_tokens": read}}}
    if agent:
        row["agentId"] = agent
    return json.dumps(row)


def poll(ts, active, five, week):
    return json.dumps({"event": "poll", "ts": iso(ts), "active": {"number": active},
                       "windowsPct": {str(active): {"5h": five, "7d": week}}})


@pytest.fixture
def env(tmp_path):
    root, projects = tmp_path / "data", tmp_path / "projects"
    root.mkdir()
    (projects / "p").mkdir(parents=True)

    def logs(*lines):
        (projects / "p" / "s.jsonl").write_text("\n".join(lines) + "\n")

    def polls(*lines):
        (root / "decisions.jsonl").write_text("\n".join(lines) + "\n")

    return root, projects, logs, polls


# ---- usage log --------------------------------------------------------------

def test_read_messages_keeps_ids_and_numbers_only(env):
    _root, projects, logs, _ = env
    logs(msg(NOW + 10, "m1", agent="a1", inp=100, out=10, create=50, read=10_000),
         msg(NOW + 11, "m1", agent="a1", inp=100, out=10, create=50, read=10_000),
         msg(NOW + 20, "m2", session="s2", model="claude-sonnet-5-5", out=100),
         msg(NOW + 999, "late", out=5), "not json")
    got = usage_log.read_messages(projects, NOW, NOW + 100)
    assert [(m["session"], m["agent"], m["weighted"]) for m in got] == [("s1", "a1", 300.0), ("s2", None, 300.0)]
    assert "secret" not in json.dumps(got) and "@" not in json.dumps(got)
    summary = usage_log.summarize(got)
    assert summary["messages"] == 2 and summary["weightedTokens"] == 600
    assert summary["tokens"] == {"input": 100, "cacheWrite": 50, "cacheRead": 10_000, "output": 110}
    assert sorted(summary["models"]) == ["claude-opus-5-5", "claude-sonnet-5-5"]


def test_select_by_agent_and_by_session(env):
    _root, projects, logs, _ = env
    logs(msg(NOW + 1, "m1", agent="a1", out=1), msg(NOW + 2, "m2", out=1), msg(NOW + 3, "m3", session="s2", out=1))
    got = usage_log.read_messages(projects, NOW)
    assert len(usage_log.select(got, agents=["a1"])) == 1
    assert len(usage_log.select(got, sessions=["s1"])) == 2  # the session and its subagent
    assert len(usage_log.select(got)) == 3


# ---- stats ------------------------------------------------------------------

def test_stats_and_confidence():
    assert measure.stats([]) == {"n": 0, "median": None, "mean": None, "p25": None, "p75": None}
    assert measure.stats([10]) == {"n": 1, "median": 10, "mean": 10, "p25": 10, "p75": 10}
    assert measure.stats([10, 20, 30, 40, 100]) == {"n": 5, "median": 30, "mean": 40, "p25": 20, "p75": 40}
    assert [measure.sample_confidence(n) for n in (4, 5, 10, 30)] == ["unknown", "low", "medium", "high"]


def test_clean_removes_what_could_carry_an_email_or_a_path():
    assert measure.clean("T3-exe-7f2a") == "T3-exe-7f2a"
    assert measure.clean("a.person@example.com") == "a.personexample.com"
    assert measure.clean("/srv/someone/project") == "srvsomeoneproject"
    assert measure.clean(None) is None and measure.clean("   ") is None


# ---- measured stretches -----------------------------------------------------

def test_measure_a_clean_stretch(env):
    root, projects, logs, polls = env
    polls(poll(NOW - 60, 5, 10.0, 20.0), poll(NOW + 300, 5, 14.0, 20.0), poll(NOW + 590, 5, 20.0, 21.0))
    logs(msg(NOW + 100, "m1", inp=100_000), msg(NOW + 400, "m2", out=20_000))
    started = measure.measure_start(root, NOW, "wave 1")
    out = measure.measure_stop(root, projects, started["measureId"], NOW + 600)
    assert out["ok"] is True and out["label"] == "wave 1" and out["durationSeconds"] == 600
    assert out["usage"]["weightedTokens"] == 200_000
    assert out["percent"]["account"] == 5
    assert out["percent"]["fiveHour"] == {"start": 10.0, "end": 20.0, "delta": 10.0}
    assert out["percent"]["sevenDay"] == {"start": 20.0, "end": 21.0, "delta": 1.0}
    assert out["weightedTokensPerPoint"] == {"fiveHour": 20_000, "sevenDay": 200_000}
    assert out["contaminated"] == []
    lines = [json.loads(line) for line in (root / "measures.jsonl").read_text().splitlines()]
    assert [line["event"] for line in lines] == ["start", "stop"]
    assert all(line["schemaVersion"] == 1 and line["source"] == "passive" for line in lines)


def test_measure_reports_why_it_may_be_wrong(env):
    root, projects, logs, polls = env
    polls(poll(NOW - 60, 5, 10.0, 20.0), poll(NOW + 300, 4, 50.0, 30.0))
    logs(msg(NOW + 100, "m1", session="mine", out=1000), msg(NOW + 200, "m2", session="other", out=1000))
    started = measure.measure_start(root, NOW, sessions=["mine"])
    out = measure.measure_stop(root, projects, started["measureId"], NOW + 600)
    assert out["usage"]["weightedTokens"] == 5000 and out["otherLocalWeightedTokens"] == 5000
    assert "account-switch" in out["contaminated"] and "other-local-work" in out["contaminated"]
    assert out["weightedTokensPerPoint"] is None


def test_measure_with_no_data_and_bad_ids(env):
    root, projects, _logs, _polls = env
    started = measure.measure_start(root, NOW)
    out = measure.measure_stop(root, projects, started["measureId"], NOW + 60)
    assert out["contaminated"] == ["no-percent-readings", "no-tokens-in-the-logs"]
    assert measure.measure_stop(root, projects, started["measureId"], NOW + 61)["error"] == "already-stopped"
    assert measure.measure_stop(root, projects, "nope", NOW)["error"] == "unknown-measure-id"


def test_percent_within_resolution_and_reset_are_flagged(env):
    root, _projects, _logs, polls = env
    polls(poll(NOW, 5, 40.0, 20.0), poll(NOW + 100, 5, 0.0, 20.0), poll(NOW + 200, 5, 2.0, 20.0))
    track = measure.percent_track(root, NOW, NOW + 300)
    assert "five-hour-reset" in track["reasons"] and "weekly-within-resolution" in track["reasons"]


# ---- tasks ------------------------------------------------------------------

def task(i, role="executor", tier="master", model="claude-opus-5-5", agent=None, **kw):
    return {"role": role, "tier": tier, "model": model, "agent_id": agent or f"a{i}",
            "started_at": iso(NOW + i * 1000), "ended_at": iso(NOW + i * 1000 + 500), **kw}


def test_record_task_sums_the_tokens_of_its_agent(env):
    root, projects, logs, _ = env
    logs(msg(NOW + 100, "m1", agent="a0", out=2000), msg(NOW + 200, "m2", agent="other", out=9999),
         msg(NOW + 900, "m3", agent="a0", out=9999))  # after the task ended
    out = measure.record_task(root, projects, task(0, task_id="T3", address="T3-exe-7f2a",
                                                  project_hash="ab12", attempt=1), NOW + 600)
    assert out["ok"] and out["matchedInLogs"] and out["weightedTokens"] == 10_000
    row = json.loads((root / "tasks.jsonl").read_text())
    assert row["role"] == "executor" and row["address"] == "T3-exe-7f2a" and row["projectHash"] == "ab12"
    assert row["usage"]["weightedTokens"] == 10_000 and row["durationSeconds"] == 500
    assert row["schemaVersion"] == 1 and row["source"] == "passive"


def test_record_task_refuses_bad_input_and_keeps_no_identity(env):
    root, projects, _logs, _ = env
    assert measure.record_task(root, projects, {"role": "executor"}, NOW)["error"] == "bad-times"
    assert measure.record_task(root, projects, task(0, role=""), NOW)["error"] == "role-required"
    bad = measure.record_task(root, projects, task(0, project_hash="/srv/x/proj"), NOW)
    assert bad["error"] == "project-hash-must-be-a-hash"
    out = measure.record_task(root, projects, task(0, task_id="a.person@example.com /srv/x"), NOW)
    assert out["ok"] and out["matchedInLogs"] is False and out["weightedTokens"] is None
    stored = (root / "tasks.jsonl").read_text()
    assert "@" not in stored and "/" not in stored


def _history(root, projects, logs, sizes, **kw):
    logs(*[msg(NOW + i * 1000 + 100, f"m{i}", agent=f"a{i}", inp=size) for i, size in enumerate(sizes)])
    for i in range(len(sizes)):
        measure.record_task(root, projects, task(i, **kw), NOW)


def test_estimate_is_unknown_below_five_tasks(env):
    root, projects, logs, _ = env
    _history(root, projects, logs, [100, 200, 300, 400])
    out = measure.estimate_task(root, "executor", "master")
    assert out["confidence"] == "unknown" and out["weightedTokens"]["n"] == 4
    assert "percentOfWeek" not in out and "no estimate" in out["note"]


def test_estimate_gives_median_and_range(env):
    root, projects, logs, _ = env
    _history(root, projects, logs, [100_000, 200_000, 300_000, 400_000, 1_000_000])
    out = measure.estimate_task(root, "Executor", "Master", "claude-opus-5-5")
    assert out["confidence"] == "low" and out["matchedOn"] == "role+tier+model"
    assert out["weightedTokens"] == {"n": 5, "median": 300_000, "mean": 400_000,
                                     "p25": 200_000, "p75": 400_000}
    assert out["percentOfWeek"] == {"pro": 1.0, "max": 0.2}
    assert out["durationSeconds"]["median"] == 500


def test_estimate_widens_the_match_until_it_has_enough(env):
    root, projects, logs, _ = env
    _history(root, projects, logs, [100, 200, 300, 400, 500])
    out = measure.estimate_task(root, "executor", "journeyman", "claude-sonnet-5-5")
    assert out["matchedOn"] == "role" and out["confidence"] == "low"
    assert measure.estimate_task(root, "reviewer")["confidence"] == "unknown"


def test_failed_tasks_do_not_feed_the_estimate(env):
    root, projects, logs, _ = env
    _history(root, projects, logs, [100, 200, 300, 400, 500], outcome="failed")
    assert measure.estimate_task(root, "executor")["weightedTokens"]["n"] == 0


# ---- fits -------------------------------------------------------------------

def _budget(reachable):
    return lambda hours: {"advice": "ok", "tokens": {"confidence": "prior"},
                          "horizons": [{"hours": hours, "reachableTokens": reachable}]}


def test_fits_yes_tight_no_and_unknown(env):
    root, projects, logs, _ = env
    _history(root, projects, logs, [100_000, 200_000, 300_000, 400_000, 1_000_000])
    wave = [{"role": "executor", "tier": "master", "count": 2}]
    yes = measure.fits(root, wave, _budget(1_000_000), 24)
    assert yes["fits"] == "yes" and yes["neededTokens"] == {"median": 600_000, "p75": 800_000}
    assert measure.fits(root, wave, _budget(700_000), 24)["fits"] == "tight"
    assert measure.fits(root, wave, _budget(500_000), 24)["fits"] == "no"
    mixed = measure.fits(root, wave + [{"role": "qa"}], _budget(10**9), 24)
    assert mixed["fits"] == "unknown" and mixed["unknownTasks"] == 1
    assert measure.fits(root, [], _budget(10**9), 24)["fits"] == "unknown"
