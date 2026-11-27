"""Live budget source: it reads autoswitch.workDays and passes the local zone."""

import json
from datetime import datetime, timezone

import pytest

from claude_swap import budget

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc).timestamp()
H = 3600.0


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _account():
    return {
        "id": 1, "plan": "pro", "status": "ok", "fetchedAt": iso(NOW - 60),
        "fiveHour": {"pct": 0.0, "resetsAt": iso(NOW + 2 * H)},
        "sevenDay": {"pct": 50.0, "resetsAt": iso(NOW + 84 * H)},
    }


def _source(monkeypatch, tmp_path, settings):
    from claude_swap import budget_source, cli, paths

    (tmp_path / "settings.json").write_text(json.dumps(settings))
    monkeypatch.setattr(paths, "get_backup_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_pace_payload", lambda: ({"accounts": [_account()]}, 0))
    return budget_source


def test_current_budget_reads_the_work_days_setting(monkeypatch, tmp_path):
    src = _source(monkeypatch, tmp_path, {"autoswitch": {"workDays": "mon-fri"}})
    assert src.current_budget(NOW, refresh_calibration=False)["fleet"]["workDays"] == "mon-fri"


def test_current_budget_without_the_setting_is_all_days(monkeypatch, tmp_path):
    src = _source(monkeypatch, tmp_path, {})
    assert src.current_budget(NOW, refresh_calibration=False)["fleet"]["workDays"] == "all"


@pytest.mark.parametrize("value", ["fri-mon", "xx", 7, None, ""])
def test_current_budget_bad_work_days_reads_all(monkeypatch, tmp_path, value):
    src = _source(monkeypatch, tmp_path, {"autoswitch": {"workDays": value}})
    b = src.current_budget(NOW, refresh_calibration=False)
    assert b["fleet"]["workDays"] == "all"
    # The bad value must not sink the whole budget into the "unknown" fallback.
    assert b["fleet"]["measuredAccounts"] == 1 and b["advice"] == "ok"


def test_current_budget_passes_the_work_days_and_the_local_zone(monkeypatch, tmp_path):
    src = _source(monkeypatch, tmp_path, {"autoswitch": {"workDays": "mon-fri"}})
    seen = {}
    real = budget.build_budget

    def spy(pace, now, **kw):
        seen.update(kw)
        return real(pace, now, **kw)

    monkeypatch.setattr(budget, "build_budget", spy)
    src.current_budget(NOW, refresh_calibration=False)
    assert seen["work_days"] == frozenset(range(5))
    assert "tz" in seen and seen["tz"] is None  # None = the local zone, read at each call (WD3)
