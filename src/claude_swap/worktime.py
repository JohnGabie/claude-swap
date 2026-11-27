"""Work time: the days of the week the user works, and the seconds of a span
that fall on them.

Pure functions: no I/O and no clock reads. The caller passes POSIX seconds
and a ``tzinfo``. ``tz`` None means the machine's local zone, read again for
each instant (``datetime.fromtimestamp(t)``), so a change of offset (summer
time) moves the day borders where the calendar puts them.

``days`` is a frozenset of weekday numbers (0 = Monday .. 6 = Sunday), or
``None`` for all days. The setting ``autoswitch.workDays`` holds the days as
text; ``parse_days`` reads it and ``format_days`` writes its normal form:
``"all"`` for seven days, else the days in order Monday to Sunday, a run of
three or more consecutive days as ``a-b`` and other days as single words,
joined by ``,`` (``mon-fri``, ``mon,wed,fri``, ``mon-wed,fri``).

A work share is the part of an account's week that has passed, counted in
work time only. The week of an account with next weekly reset ``R`` is
``[R - 168 h, R)``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

DAY_WORDS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALL_DAYS = "all"
WEEK_S = 168 * 3600.0
DAY_H = 24.0
# A run of this many consecutive days or more is written as a range.
MIN_RANGE_RUN = 3


def _day(word: str, text: str) -> int:
    try:
        return DAY_WORDS.index(word.strip())
    except ValueError:
        raise ValueError(f"unknown day {word.strip()!r} in {text!r}") from None


def parse_days(text: str) -> frozenset[int] | None:
    """Read a work-days text. ``None`` means all days.

    Forms: ``all``, a range ``mon-fri``, a list ``mon,wed,fri`` or a mix
    ``mon-wed,fri``; case and spaces around words are ignored. A range runs
    forward from Monday to Sunday and does not wrap (``fri-mon`` is refused);
    ``sun-sat``, the whole week in Sunday-first order, is the one exception.
    Seven days read as ``None``. A bad text raises ``ValueError``.
    """
    lowered = text.strip().lower()
    if lowered == ALL_DAYS:
        return None
    days: set[int] = set()
    for part in lowered.split(","):
        ends = part.split("-")
        if len(ends) == 1:
            days.add(_day(ends[0], text))
        elif len(ends) == 2:
            first, last = _day(ends[0], text), _day(ends[1], text)
            if (first, last) == (6, 5):
                return None
            if first > last:
                raise ValueError(f"the range {part.strip()!r} wraps the week")
            days.update(range(first, last + 1))
        else:
            raise ValueError(f"bad range {part.strip()!r} in {text!r}")
    return None if len(days) == len(DAY_WORDS) else frozenset(days)


def format_days(days: frozenset[int] | None) -> str:
    """The normal form of ``days``; ``"all"`` for None, seven days or none."""
    if not days or len(days) == len(DAY_WORDS):
        return ALL_DAYS
    if not days <= set(range(len(DAY_WORDS))):
        raise ValueError(f"days must be 0..6, got {sorted(days)}")
    ordered = sorted(days)
    runs: list[list[int]] = []
    for day in ordered:
        if runs and day == runs[-1][-1] + 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    words = []
    for run in runs:
        if len(run) >= MIN_RANGE_RUN:
            words.append(f"{DAY_WORDS[run[0]]}-{DAY_WORDS[run[-1]]}")
        else:
            words.extend(DAY_WORDS[d] for d in run)
    return ",".join(words)


def week_work_hours(days: frozenset[int] | None) -> float:
    """Work hours of one full week: 168 for all days, 120 for mon-fri."""
    return len(DAY_WORDS) * DAY_H if days is None else len(days) * DAY_H


def _next_midnight(t: float, tz: tzinfo | None) -> tuple[float, int]:
    """``(the first local midnight after t, the weekday of t)``."""
    here = datetime.fromtimestamp(t, tz)
    day = here.date() + timedelta(days=1)
    # A naive datetime (tz None) is local time to .timestamp().
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()
    return midnight, here.weekday()


def work_seconds(start: float, end: float, days: frozenset[int] | None,
                 tz: tzinfo | None) -> float:
    """The seconds of ``[start, end)`` that fall on a local day in ``days``.

    ``end - start`` for ``days`` None; 0.0 when ``end <= start``.
    """
    if end <= start:
        return 0.0
    if days is None:
        return end - start
    total = 0.0
    cur = start
    while cur < end:
        midnight, weekday = _next_midnight(cur, tz)
        stop = min(midnight, end)
        if weekday in days:
            total += stop - cur
        cur = stop
    return total


def work_share(reset: float, now: float, days: frozenset[int] | None,
               tz: tzinfo | None) -> float:
    """The share of the week before ``reset`` that has passed at ``now``,
    in work time, clipped to 0..1.

    ``reset`` is the next weekly reset. For ``days`` None this is
    ``1 - (reset - now) / 168 h``, the wall-clock share. When no work time
    is left before the reset, the share is 1: the passed part and the whole
    week sum the same day pieces.
    """
    if days is None:
        return max(0.0, min(1.0, 1.0 - (reset - now) / WEEK_S))
    week = work_seconds(reset - WEEK_S, reset, days, tz)
    passed = work_seconds(reset - WEEK_S, now, days, tz)
    return max(0.0, min(1.0, passed / week))
