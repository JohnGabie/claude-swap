"""Measure tokens per percent point from the user's own data.

Two local sources are joined, with no network call:

- the decision log (``decisions.jsonl``): each poll gives the active account
  and the percent of its windows at a time;
- the Claude Code session logs (``<claude config>/projects/**/*.jsonl``):
  each assistant message gives a time and its token counts.

Only the ``timestamp``, the message and request ids and the integer token
counts of a log line are read. No message text is kept.

Between two polls close in time with the same active account, the weekly
percent of that account rose by some points and the logs hold some tokens.
The sum of the tokens divided by the sum of the points (times the capacity
of the plan) is the tokens per percent point of a Pro account.

Limits of the measurement, all stated in the result:
- "tokens" are input + output + cache creation. Cache reads are counted
  apart: nobody outside Anthropic knows their weight in the limit.
- The percent is a whole number, so a short sample is coarse.
- Use of the same account elsewhere (claude.ai, another machine) raises the
  percent with no tokens in these logs, which lowers the result.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import balance, decision_log

# Two polls farther apart than this are not one interval (the engine was off).
MAX_GAP_S = 20 * 60.0
# Below this many Pro-equivalent weekly points the result is not written.
MIN_PRO_POINTS = 3.0
POLL_LINES = 200_000
# Pro-equivalent weekly points for each confidence word. The percent is a
# whole number, so a few points carry a large rounding error.
CONFIDENCE_STEPS = ((100.0, "high"), (30.0, "medium"), (0.0, "low"))


def confidence(pro_points: float) -> str:
    return next(word for floor, word in CONFIDENCE_STEPS if pro_points >= floor)


def _ts(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_polls(backup_root: Path) -> list[tuple[float, str, float | None, float | None]]:
    """``(time, active account, 5h percent, weekly percent)`` of each poll, oldest first."""
    polls = []
    for line in decision_log.tail(backup_root, POLL_LINES):
        if '"poll"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("event") != "poll":
            continue
        active = event.get("active")
        number = active.get("number") if isinstance(active, dict) else None
        ts = _ts(event.get("ts"))
        if number is None or ts is None:
            continue
        windows = (event.get("windowsPct") or {}).get(str(number))
        if not isinstance(windows, dict):
            continue
        five, week = windows.get("5h"), windows.get("7d")
        polls.append((
            ts, str(number),
            float(five) if isinstance(five, (int, float)) else None,
            float(week) if isinstance(week, (int, float)) else None,
        ))
    polls.sort(key=lambda p: p[0])
    return polls


def claude_projects_dir(env=None) -> Path:
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def read_token_events(projects_dir: Path, since: float) -> list[tuple[float, int, int]]:
    """``(time, tokens, cache read tokens)`` of each assistant message after ``since``.

    A message repeats on several lines of a log; one (message id, request id)
    counts once. Files not changed since ``since`` are skipped.
    """
    events: dict[tuple, tuple[float, int, int]] = {}
    try:
        files = [p for p in projects_dir.rglob("*.jsonl") if p.stat().st_mtime >= since]
    except OSError:
        return []
    for path in files:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                message = row.get("message") if isinstance(row, dict) else None
                usage = message.get("usage") if isinstance(message, dict) else None
                ts = _ts(row.get("timestamp")) if isinstance(row, dict) else None
                if not isinstance(usage, dict) or ts is None or ts < since:
                    continue

                def count(key):
                    value = usage.get(key)
                    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

                tokens = count("input_tokens") + count("output_tokens") + count("cache_creation_input_tokens")
                key = (message.get("id"), row.get("requestId"))
                if key == (None, None):
                    key = (str(path), ts)
                events[key] = (ts, tokens, count("cache_read_input_tokens"))
    return sorted(events.values())


def measure(polls, events, plan_of: dict[str, str]) -> dict:
    """Join polls and token events. Pure."""
    week_points = five_points = 0.0
    week_tokens = five_tokens = cache_read = 0
    intervals = 0
    index = 0
    per_account: dict[str, dict] = {}
    for (t0, a0, f0, w0), (t1, a1, f1, w1) in zip(polls, polls[1:]):
        if a0 != a1 or t1 <= t0 or t1 - t0 > MAX_GAP_S:
            continue
        while index < len(events) and events[index][0] <= t0:
            index += 1
        tokens = reads = 0
        scan = index
        while scan < len(events) and events[scan][0] <= t1:
            tokens += events[scan][1]
            reads += events[scan][2]
            scan += 1
        capacity = balance.plan_capacity(plan_of.get(a0))
        intervals += 1
        if w0 is not None and w1 is not None and w1 >= w0:
            week_points += (w1 - w0) * capacity
            week_tokens += tokens
            cache_read += reads
            row = per_account.setdefault(a0, {"plan": plan_of.get(a0, "unknown"), "weeklyPoints": 0.0, "tokens": 0})
            row["weeklyPoints"] += w1 - w0
            row["tokens"] += tokens
        if f0 is not None and f1 is not None and f1 >= f0:
            five_points += (f1 - f0) * capacity
            five_tokens += tokens
    enough = week_points >= MIN_PRO_POINTS
    return {
        "enough": enough,
        "confidence": confidence(week_points) if enough else None,
        "tokensPerPercentPro": round(week_tokens / week_points) if enough else None,
        "tokensPerFiveHourPercentPro": round(five_tokens / five_points) if five_points >= MIN_PRO_POINTS else None,
        "cacheReadTokensPerPercentPro": round(cache_read / week_points) if enough else None,
        "sample": {
            "intervals": intervals,
            "proWeeklyPoints": round(week_points, 1),
            "proFiveHourPoints": round(five_points, 1),
            "tokens": week_tokens,
            "from": _iso(polls[0][0]) if polls else None,
            "to": _iso(polls[-1][0]) if polls else None,
            "accounts": {k: per_account[k] for k in sorted(per_account)},
        },
        "tokenDefinition": "input + output + cache creation; cache reads are counted apart",
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def calibrate(backup_root: Path, plan_of: dict[str, str], projects_dir: Path | None = None,
              now: float | None = None) -> dict:
    """Measure, and write ``budget_calibration.json`` when the sample is enough."""
    from claude_swap.budget_source import CALIBRATION_FILENAME
    from claude_swap.settings import atomic_write_json

    polls = read_polls(backup_root)
    events = read_token_events(projects_dir or claude_projects_dir(), polls[0][0]) if polls else []
    result = measure(polls, events, plan_of)
    result["measuredAt"] = _iso(now) if now is not None else _iso(datetime.now(timezone.utc).timestamp())
    result["written"] = False
    if result["enough"] and result["tokensPerPercentPro"]:
        atomic_write_json(Path(backup_root) / CALIBRATION_FILENAME, result)
        result["written"] = True
    return result
