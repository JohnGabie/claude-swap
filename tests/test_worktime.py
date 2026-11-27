"""Work time: the work-days setting and the seconds that fall on work days
(claude_swap.worktime)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from claude_swap import worktime

TZ = timezone(timedelta(hours=-3))
H = 3600.0
WEEK_S = 168 * H
MON_FRI = frozenset({0, 1, 2, 3, 4})


def at(*args) -> float:
    """POSIX seconds of a wall-clock time in TZ (-03:00)."""
    return datetime(*args, tzinfo=TZ).timestamp()


# --- the setting text --------------------------------------------------------


def test_parse_and_format_days():
    parse, fmt = worktime.parse_days, worktime.format_days
    assert parse("all") is None
    assert parse(" ALL ") is None
    assert parse("mon-fri") == MON_FRI
    assert parse("Mon,Wed,Fri") == frozenset({0, 2, 4})
    assert parse("mon-wed,fri") == frozenset({0, 1, 2, 4})
    assert parse("sat") == frozenset({5})
    assert parse("fri-sun") == frozenset({4, 5, 6})
    # Seven days are all days.
    assert parse("mon-sun") is None
    assert parse("sun-sat") is None
    for bad in ("workdays", "fri-mon", "", ",", "mon,,tue", "mon-", "-fri",
                "mon-tue-wed", "sun-fri", "monday",
                # "sun-sat" is all days only as the whole text.
                "sun-sat,xyz", "sun-sat,fri-mon", "mon,sun-sat", "sun-sat,"):
        with pytest.raises(ValueError):
            parse(bad)

    assert fmt(None) == "all"
    assert fmt(frozenset(range(7))) == "all"
    assert fmt(frozenset()) == "all"
    assert fmt(MON_FRI) == "mon-fri"
    assert fmt(frozenset({0, 1, 2, 3, 4, 5})) == "mon-sat"
    assert fmt(frozenset({0, 2, 4})) == "mon,wed,fri"
    assert fmt(frozenset({0, 1, 2, 4})) == "mon-wed,fri"
    # A run of two days stays two words.
    assert fmt(frozenset({0, 1, 4})) == "mon,tue,fri"
    assert fmt(frozenset({4, 5, 6})) == "fri-sun"
    with pytest.raises(ValueError):
        fmt(frozenset({7}))
    # The normal form reads back as the same days.
    for days in (MON_FRI, frozenset({0, 2, 4}), frozenset({1, 5, 6}),
                 frozenset({0, 1, 2, 4, 5, 6}), frozenset({6})):
        assert parse(fmt(days)) == days


def test_week_work_hours():
    assert worktime.week_work_hours(None) == 168.0
    assert worktime.week_work_hours(MON_FRI) == 120.0
    assert worktime.week_work_hours(frozenset({5})) == 24.0


def test_no_day_reads_as_all_days():
    none = frozenset()
    assert worktime.week_work_hours(none) == 168.0
    now = at(2026, 11, 28, 15, 0)  # a Saturday
    assert worktime.work_seconds(now, now + 10 * H, none, TZ) == 10 * H
    # The share is the wall-clock one, float for float.
    rng = random.Random(3)
    for _ in range(500):
        reset = now + rng.uniform(-10 * H, 200 * H)
        assert worktime.work_share(reset, now, none, TZ) == worktime.work_share(
            reset, now, None, TZ)


# --- work seconds and the work share ---------------------------------------


def test_all_days_share_equals_wall_clock():
    """S1: with all days the share is today's wall-clock expression, float
    for float, in any zone."""
    rng = random.Random(7)
    for _ in range(2000):
        now = rng.uniform(1.6e9, 1.9e9)
        reset = now + rng.uniform(-10 * H, 200 * H)
        old = max(0.0, min(1.0, 1.0 - (reset - now) / WEEK_S))
        for tz in (None, TZ, timezone.utc):
            assert worktime.work_share(reset, now, None, tz) == old
        start, end = sorted((now, reset))
        assert worktime.work_seconds(start, end, None, TZ) == end - start


def test_work_share_friday_evening():
    """S2: Fri 19:06, reset Sat 10:00; the week Sat 10:00 .. Sat 10:00 has
    120 work hours and 115.1 of them have passed."""
    now = at(2026, 11, 27, 19, 6)
    reset = at(2026, 11, 28, 10, 0)
    share = worktime.work_share(reset, now, MON_FRI, TZ)
    assert share == pytest.approx(115.1 / 120, abs=1e-6)
    assert share == pytest.approx(0.959167, abs=1e-6)


def test_remaining_work_time_until_midnight():
    """S3: from Fri 19:06 to a reset on Sun 05:59 only the 4 h 54 min to
    Friday midnight are work time."""
    now = at(2026, 11, 27, 19, 6)
    reset = at(2026, 11, 29, 5, 59)
    assert worktime.work_seconds(now, reset, MON_FRI, TZ) == 17640.0


def test_work_seconds_edges():
    now = at(2026, 11, 27, 19, 6)
    # An empty or reversed span is 0.
    assert worktime.work_seconds(now, now, MON_FRI, TZ) == 0.0
    assert worktime.work_seconds(now, now - H, MON_FRI, TZ) == 0.0
    assert worktime.work_seconds(now, now - H, None, TZ) == 0.0
    # A whole calendar week holds five work days.
    mon = at(2026, 11, 23, 0, 0)
    assert worktime.work_seconds(mon, mon + WEEK_S, MON_FRI, TZ) == 120 * H
    # The weekend alone is rest time.
    sat = at(2026, 11, 28, 0, 0)
    assert worktime.work_seconds(sat, sat + 48 * H, MON_FRI, TZ) == 0.0
    # Days are read in the given zone: Sat 01:00 at -03 is Sat 04:00 UTC,
    # but Fri 23:00 at -05.
    t = at(2026, 11, 28, 1, 0)
    minus5 = timezone(timedelta(hours=-5))
    assert worktime.work_seconds(t, t + H, MON_FRI, TZ) == 0.0
    assert worktime.work_seconds(t, t + H, MON_FRI, minus5) == H


def test_work_share_with_no_work_time_left_is_one():
    # Sat 15:00, reset Sun 05:59: no work time is left before the reset.
    now = at(2026, 11, 28, 15, 0)
    reset = at(2026, 11, 29, 5, 59)
    assert worktime.work_seconds(now, reset, MON_FRI, TZ) == 0.0
    assert worktime.work_share(reset, now, MON_FRI, TZ) == 1.0
    # Five work hours left (Mon 00:00 .. 05:00): the share is below 1.
    reset_mon = at(2026, 11, 30, 5, 0)
    assert worktime.work_share(reset_mon, now, MON_FRI, TZ) == pytest.approx(115 / 120)


def test_work_share_is_clipped():
    now = at(2026, 11, 27, 19, 6)
    # A reset more than a week away: no work time of its week has passed.
    assert worktime.work_share(now + 200 * H, now, MON_FRI, TZ) == 0.0
    # A reset that has passed: the whole week has passed, and no more.
    assert worktime.work_share(now - 24 * H, now, MON_FRI, TZ) == 1.0


def test_local_zone_is_read_per_call(monkeypatch):
    # tz None follows the machine zone: TZ=UTC and TZ=-03 give other days.
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    t = at(2026, 11, 28, 1, 0)  # Sat 01:00 at -03, Sat 04:00 UTC
    try:
        monkeypatch.setenv("TZ", "UTC+5")  # POSIX sign: 5 h west of UTC
        time.tzset()
        assert worktime.work_seconds(t, t + H, MON_FRI, None) == H
        monkeypatch.setenv("TZ", "UTC0")
        time.tzset()
        assert worktime.work_seconds(t, t + H, MON_FRI, None) == 0.0
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_23_hour_day_counts_its_real_seconds():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        tz = zoneinfo.ZoneInfo("Europe/Berlin")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone data")
    # Sunday 2026-03-29 has 23 hours in Berlin (clocks go 02:00 -> 03:00).
    sun = datetime(2026, 3, 29, tzinfo=tz).timestamp()
    mon = datetime(2026, 3, 30, tzinfo=tz).timestamp()
    assert mon - sun == 23 * H
    assert worktime.work_seconds(sun - H, mon + H, frozenset({6}), tz) == 23 * H
    assert worktime.work_seconds(sun - H, mon + H, frozenset({0}), tz) == H


def test_a_23_hour_day_in_the_local_zone(monkeypatch):
    """tz None reads the offset of each instant, not one offset for all."""
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        berlin = zoneinfo.ZoneInfo("Europe/Berlin")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone data")
    sun = datetime(2026, 3, 29, tzinfo=berlin).timestamp()
    mon = datetime(2026, 3, 30, tzinfo=berlin).timestamp()
    try:
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        # The process zone took the rules (+01:00 before, +02:00 after).
        if (datetime.fromtimestamp(sun).astimezone().utcoffset(),
                datetime.fromtimestamp(mon).astimezone().utcoffset()) != (
                timedelta(hours=1), timedelta(hours=2)):
            pytest.skip("the C library has no Europe/Berlin rules")
        assert worktime.work_seconds(sun - H, mon + H, frozenset({6}), None) == 23 * H
        assert worktime.work_seconds(sun - H, mon + H, frozenset({0}), None) == H
    finally:
        monkeypatch.undo()
        time.tzset()
