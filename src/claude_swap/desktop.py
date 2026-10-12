"""Desktop window for claude-swap (``cswap-desktop``).

A thin GUI shell over ``ClaudeAccountSwitcher`` and the core auto-switch engine
(``claude_swap.autoswitch``) — it never re-implements account, usage, or
auto-switch logic. Usage for display comes from ``switcher.accounts_snapshot()``
and is formatted with the TUI's shared helpers (``claude_swap.tui.data``).

Built on ``tkinter`` (stdlib). The pure helpers below (card and event-line
formatting) are import-safe without tkinter so they can be unit-tested headless;
``tkinter`` is imported lazily inside the app glue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from claude_swap.models import AccountSnapshot
from claude_swap.tui.data import (
    clock_stamp,
    format_age,
    sentinel_label,
    window_pct,
    window_reset_text,
)

_TITLE_MAX = 32
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WINDOWS = (("5h", "five_hour"), ("7d", "seven_day"))

ALERT_KINDS = frozenset(
    {"all-exhausted", "account-quarantined", "config-warning", "error"}
)


@dataclass(frozen=True)
class WindowRow:
    label: str
    pct: float | None
    reset: str | None
    age: str | None


@dataclass(frozen=True)
class CardView:
    title: str
    tag: str
    active: bool
    disabled: bool
    switchable: bool
    rows: tuple[WindowRow, ...]
    message: str | None


def card_lines(acc: AccountSnapshot, now: float) -> CardView:
    """Everything one account card shows, as plain data."""
    title = f"#{acc.number}  {acc.alias or acc.email}"
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1] + "…"
    rows: tuple[WindowRow, ...] = ()
    message = None
    usage = acc.usage
    if usage.sentinel:
        message = sentinel_label(usage.sentinel)
    elif not usage.last_good:
        message = "no usage yet"
    else:
        age = format_age(usage.age_s)
        rows = tuple(
            WindowRow(
                label,
                window_pct(usage.last_good, key),
                window_reset_text(usage.last_good, key, now),
                age,
            )
            for label, key in _WINDOWS
        )
    return CardView(
        title=title,
        tag=acc.display_tag,
        active=acc.is_active,
        disabled=acc.disabled,
        switchable=acc.switchable,
        rows=rows,
        message=message,
    )


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def event_line(ev) -> tuple[str, bool]:
    """Event-log text and whether it deserves alert styling."""
    return f"{clock_stamp()} {ev.human()}", ev.kind in ALERT_KINDS
