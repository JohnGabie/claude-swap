# guilda:consolidate worktime — provisional copy; W1's file replaces it at merge
"""Work days (WD2, WD8): parse and format the ``autoswitch.workDays`` text. Provisional: only these two."""

from __future__ import annotations

DAY_WORDS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # 0 = Monday
_RUN_MIN = 3  # a run of this many consecutive days is written as ``a-b``


def parse_days(text: str) -> frozenset[int] | None:
    """``None`` for all days, else the set of 0..6 (0 = Monday). A bad text raises ``ValueError``."""
    if not isinstance(text, str):
        raise ValueError("work days must be text")
    low = text.lower()
    if low == "all":
        return None
    days: set[int] = set()
    for part in low.split(","):
        if part == "sun-sat":  # the Sunday-first week, all seven days (S7 of the spec)
            days.update(range(7))
            continue
        first, dash, last = part.partition("-")
        if first not in DAY_WORDS or (dash and last not in DAY_WORDS):
            raise ValueError(f"bad work days: {part!r}")
        start = DAY_WORDS.index(first)
        end = DAY_WORDS.index(last) if dash else start
        if end < start:
            raise ValueError("a range does not wrap the week")
        days.update(range(start, end + 1))
    return None if len(days) == 7 else frozenset(days)


def format_days(days: frozenset[int] | None) -> str:
    """The normal form: ``all``, or the days Mon..Sun with runs of 3 or more as ``a-b``."""
    if days is None or len(days) in (0, 7):
        return "all"
    ordered = sorted(days)
    out, i = [], 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        if j - i + 1 >= _RUN_MIN:
            out.append(f"{DAY_WORDS[ordered[i]]}-{DAY_WORDS[ordered[j]]}")
        else:
            out.extend(DAY_WORDS[d] for d in ordered[i:j + 1])
        i = j + 1
    return ",".join(out)
