"""Limit hits and weighted tokens from new Claude Code transcript lines (LH1, LH3, LH4).

Reads the whole lines that Claude Code appends to ``<projects>/**/*.jsonl``
at any depth (subagent transcripts are in ``<project>/<session>/subagents/``),
by byte offset, like ``usage_history``. A line stamped more than
``FUTURE_SLACK_S`` after ``clock()`` is ignored, and a line older than
``KEEP_S`` adds no tokens. The offsets live in memory only. The
first ``poll`` starts at the end of each existing file, so old hits never
replay; a file that appears later starts at 0; a shrunk file restarts at 0.

Fields read: ``type``, ``isApiErrorMessage``, ``error``, ``apiError``,
``timestamp``, ``requestId``, ``message.id``, ``message.model``,
``message.usage`` and the text of the first content item. The text is only
searched; it is never stored or logged, and no log line holds a path.

A message repeats on several lines of a log, so ``tokens_since`` counts each
``"{message.id}|{requestId}"`` once (the ``usage_history`` key); a line with
both ids missing counts once by its timestamp.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from claude_swap import token_model, usage_history, usage_log

_logger = logging.getLogger("claude-swap")

KEEP_S = 6 * 3600
# A line stamped further than this after clock() is ignored: a clock step or a
# crafted line must not hold tokens in memory or count as a hit.
FUTURE_SLACK_S = 60
API_ERROR_LIMIT = "usage_limit_reached"
ERROR_RATE_LIMIT = "rate_limit"
LIMIT_WORD = "limit"
WINDOW_5H = "5h"
WINDOW_7D = "7d"
# First match wins.
WINDOW_WORDS = (("5-hour", WINDOW_5H), ("week", WINDOW_7D))
LINE_ERRORS = (ValueError, RecursionError, OverflowError, TypeError)


@dataclass(frozen=True)
class LimitHit:
    ts: float
    window: str | None  # "5h", "7d", or None when the text names neither


def _first_text(message: dict) -> str:
    content = message.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return ""
    text = content[0].get("text")
    return text if isinstance(text, str) else ""


def _window(text: str) -> str | None:
    lowered = text.casefold()
    for needle, window in WINDOW_WORDS:
        if needle in lowered:
            return window
    return None


def _is_hit(row: dict, text: str) -> bool:
    if row.get("type") != "assistant" or row.get("isApiErrorMessage") is not True:
        return False
    if row.get("apiError") == API_ERROR_LIMIT:
        return True
    return row.get("error") == ERROR_RATE_LIMIT and LIMIT_WORD in text.casefold()


class LimitWatch:
    """Watches transcript files; ``poll`` returns new hits, ``tokens_since`` the recent load."""

    def __init__(self, projects_dir: Path, clock=time.time) -> None:
        self._root = Path(projects_dir)
        self._clock = clock
        self._offsets: dict[Path, int] = {}
        self._started = False
        self._entries: list[tuple[float, float]] = []  # (timestamp, weighted tokens)
        self._seen: dict[str, float] = {}  # message key -> timestamp

    def poll(self) -> list[LimitHit]:
        """The limit hits in the whole lines appended since the last call."""
        try:
            paths = sorted(self._root.rglob("*.jsonl"))
        except OSError:
            paths = []
        hits: list[LimitHit] = []
        offsets: dict[Path, int] = {}
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if not self._started:
                offsets[path] = size
                continue
            offset = self._offsets.get(path, 0)
            if size < offset:
                offset = 0
            if size > offset:
                offset += self._read(path, offset, hits)
            offsets[path] = offset
        self._offsets = offsets
        self._started = True
        self._trim()
        return hits

    def tokens_since(self, ts: float) -> float:
        """Weighted tokens of the assistant lines seen with a timestamp at or after ``ts``."""
        self._trim()
        return sum(weighted for entry_ts, weighted in self._entries if entry_ts >= ts)

    def _trim(self) -> None:
        cutoff = self._clock() - KEEP_S
        self._entries = [entry for entry in self._entries if entry[0] >= cutoff]
        self._seen = {key: ts for key, ts in self._seen.items() if ts >= cutoff}

    def _read(self, path: Path, offset: int, hits: list[LimitHit]) -> int:
        """Read the whole lines after ``offset``; return the bytes consumed."""
        consumed = 0
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        break
                    consumed += len(raw)
                    try:
                        self._add_line(raw, hits)
                    except LINE_ERRORS:
                        continue
        except OSError:
            pass
        return consumed

    def _add_line(self, raw: bytes, hits: list[LimitHit]) -> None:
        row = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(row, dict):
            return
        ts = usage_log.parse_ts(row.get("timestamp"))
        message = row.get("message")
        now = self._clock()
        if ts is None or ts > now + FUTURE_SLACK_S or not isinstance(message, dict):
            return
        usage = message.get("usage")
        if row.get("type") == "assistant" and isinstance(usage, dict) and ts >= now - KEEP_S:
            key = usage_history._message_key(message, row, row.get("timestamp"))
            if key not in self._seen:
                self._seen[key] = ts
                self._entries.append((ts, token_model.weighted_tokens(message.get("model"), usage)))
        text = _first_text(message)
        if _is_hit(row, text):
            window = _window(text)
            hits.append(LimitHit(ts=ts, window=window))
            _logger.debug("limit-watch: limit hit, window %s", window or "unknown")
