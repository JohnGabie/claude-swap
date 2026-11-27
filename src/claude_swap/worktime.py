# guilda:consolidate worktime — provisional copy; W1's file replaces it at merge
"""Work time: the seconds of a span that fall on a work day of the local calendar.

Pure functions (spec work-days WD3, WD8). ``days`` is ``None`` for all days, or
a frozenset of weekday numbers (0 = Monday). ``tz`` is a ``tzinfo``; ``None``
means the machine's local zone, read at each call.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, tzinfo

WEEK_S = 7 * 24 * 3600.0
_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_days(text: str) -> frozenset[int] | None:
    """``None`` for all days, else the set of weekdays. A bad text raises ``ValueError``."""
    if not isinstance(text, str):
        raise ValueError("work days must be text")
    words = text.strip().lower()
    if words == "all":
        return None
    days: set[int] = set()
    for part in words.split(","):
        part = part.strip()
        if "-" in part:
            first, _, last = part.partition("-")
            first, last = first.strip(), last.strip()
            if first not in _NAMES or last not in _NAMES:
                raise ValueError(f"bad day range: {part!r}")
            a, b = _NAMES.index(first), _NAMES.index(last)
            if a > b:
                raise ValueError(f"a range does not wrap the week: {part!r}")
            days.update(range(a, b + 1))
        elif part in _NAMES:
            days.add(_NAMES.index(part))
        else:
            raise ValueError(f"bad day: {part!r}")
    return None if len(days) == 7 else frozenset(days)


def format_days(days: frozenset[int] | None) -> str:
    """The normal form: ``all``, or runs of 3+ days as ``a-b`` and other days as words."""
    if days is None or len(days) >= 7:
        return "all"
    ordered = sorted(days)
    parts, i = [], 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        if j - i + 1 >= 3:
            parts.append(f"{_NAMES[ordered[i]]}-{_NAMES[ordered[j]]}")
        else:
            parts.extend(_NAMES[d] for d in ordered[i:j + 1])
        i = j + 1
    return ",".join(parts)


def _local(t: float, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(t, tz=tz) if tz is not None else datetime.fromtimestamp(t).astimezone()


def _next_midnight(moment: datetime, tz: tzinfo | None) -> float:
    day = moment.date() + timedelta(days=1)
    if tz is not None:
        return datetime.combine(day, time.min, tzinfo=tz).timestamp()
    return datetime.combine(day, time.min).timestamp()  # naive: the machine's zone


def work_seconds(start: float, end: float, days: frozenset[int] | None, tz: tzinfo | None) -> float:
    """Seconds of ``[start, end)`` on a local calendar day in ``days``."""
    if end <= start:
        return 0.0
    if days is None:
        return end - start
    total, cur = 0.0, start
    while cur < end:
        moment = _local(cur, tz)
        stop = min(_next_midnight(moment, tz), end)
        if stop <= cur:  # a zone quirk must not loop forever
            stop = end
        if moment.weekday() in days:
            total += stop - cur
        cur = stop
    return total


def week_work_hours(days: frozenset[int] | None) -> float:
    """Work hours of one full week."""
    return 168.0 if days is None else 24.0 * len(days)


def work_share(reset: float, now: float, days: frozenset[int] | None, tz: tzinfo | None) -> float:
    """Share (0..1) of the work time of the week ``[reset - 168 h, reset)`` that has passed.

    With no work time left before ``reset`` (and ``reset`` after ``now``) the share is 1."""
    if days is None:
        return max(0.0, min(1.0, 1.0 - (reset - now) / WEEK_S))
    start = reset - WEEK_S
    week = work_seconds(start, reset, days, tz)
    if week <= 0:
        return 1.0
    if reset > now and work_seconds(now, reset, days, tz) <= 0:
        return 1.0
    return max(0.0, min(1.0, work_seconds(start, min(now, reset), days, tz) / week))
