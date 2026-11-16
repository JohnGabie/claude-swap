"""Learn weighted tokens per percent point, for each account, from local data.

Two local sources are joined, with no network call:

- the decision log (``decisions.jsonl``): each poll gives the active account
  and the percent of its 5-hour and weekly windows at a time;
- the Claude Code session logs (``<claude config>/projects/**/*.jsonl``):
  each assistant message gives a time, a model and its token counts.

Only the timestamp, the message and request ids, the model name and the
integer token counts of a log line are read. No message text is kept.

The result is a store of sums per account and per window (percent points
and weighted tokens, see ``token_model``). It grows with each run: only the
polls after the last one seen are read. ``rates`` turns the store into a
rate per account, pulled toward the community prior while the account has
few points of its own.

What the method cannot see:
- Use of the same account elsewhere (claude.ai, another machine). A stretch
  where the percent rose and the logs hold no token is dropped as external.
- The percent is a whole number: each reading has about one point of error.
- Headless runs move the 5-hour meter about 1.5 times faster than
  interactive work (community measurement); the stored rate is the mix of
  the user's own work.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import decision_log, token_model

STORE_VERSION = 2
# Two polls farther apart than this are not one interval (the engine was off).
MAX_GAP_S = 20 * 60.0
POLL_LINES = 200_000
WINDOWS = ("week", "five")


def _ts(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def read_token_events(projects_dir: Path, since: float) -> list[tuple[float, float]]:
    """``(time, weighted tokens)`` of each assistant message after ``since``.

    A message repeats on several lines of a log; one (message id, request id)
    counts once. Files not changed since ``since`` are skipped.
    """
    events: dict[tuple, tuple[float, float]] = {}
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
                if not isinstance(usage, dict) or ts is None or ts <= since:
                    continue
                key = (message.get("id"), row.get("requestId"))
                if key == (None, None):
                    key = (str(path), ts)
                events[key] = (ts, token_model.weighted_tokens(message.get("model"), usage))
    return sorted(events.values())


def empty_store() -> dict:
    return {"schemaVersion": STORE_VERSION, "lastPoll": None, "accounts": {}}


def _valid_store(store) -> dict:
    if not isinstance(store, dict) or store.get("schemaVersion") != STORE_VERSION:
        return empty_store()
    if not isinstance(store.get("accounts"), dict):
        return empty_store()
    return store


def accumulate(store: dict, polls, events) -> dict:
    """Add the polls after ``store['lastPoll']`` to the sums. Pure: returns a new store.

    A stretch is a run of consecutive intervals of one account. A stretch
    whose percent rose with no token in the logs is counted as external use
    and adds nothing to the sums.
    """
    store = json.loads(json.dumps(_valid_store(store)))
    last = store.get("lastPoll")
    previous = tuple(last) if isinstance(last, list) and len(last) == 4 else None
    if previous is not None:
        polls = [p for p in polls if p[0] > previous[0]]
    index = 0
    stretch: dict | None = None

    def close() -> None:
        nonlocal stretch
        if stretch is None:
            return
        row = store["accounts"].setdefault(stretch["account"], {})
        external = stretch["tokens"] <= 0 and (stretch["week"] > 0 or stretch["five"] > 0)
        if external:
            row["externalPoints"] = round(row.get("externalPoints", 0.0) + stretch["week"], 3)
        else:
            for window, key in (("week", "weekTokens"), ("five", "fiveTokens")):
                sums = row.setdefault(window, {"points": 0.0, "tokens": 0.0})
                sums["points"] = round(sums["points"] + stretch[window], 3)
                sums["tokens"] = round(sums["tokens"] + stretch[key], 1)
        stretch = None

    for poll in polls:
        if previous is not None:
            (t0, a0, f0, w0), (t1, a1, f1, w1) = previous, poll
            if a0 != a1 or t1 - t0 > MAX_GAP_S:
                close()
            else:
                while index < len(events) and events[index][0] <= t0:
                    index += 1
                tokens = 0.0
                while index < len(events) and events[index][0] <= t1:
                    tokens += events[index][1]
                    index += 1
                if stretch is None:
                    stretch = {"account": a0, "week": 0.0, "five": 0.0, "tokens": 0.0,
                               "weekTokens": 0.0, "fiveTokens": 0.0}
                stretch["tokens"] += tokens
                # A percent that fell is a reset: that window skips the interval.
                if w0 is not None and w1 is not None and w1 >= w0:
                    stretch["week"] += w1 - w0
                    stretch["weekTokens"] += tokens
                if f0 is not None and f1 is not None and f1 >= f0:
                    stretch["five"] += f1 - f0
                    stretch["fiveTokens"] += tokens
        previous = poll
    close()
    store["lastPoll"] = list(previous) if previous is not None else None
    return store


def rates(store: dict, plan_of: dict[str, str]) -> dict[str, dict]:
    """Weighted tokens per percent point for each account in ``plan_of``.

    An account with no data of its own gets the prior of its plan.
    """
    store = _valid_store(store)
    out = {}
    for account, plan in plan_of.items():
        row = store["accounts"].get(str(account)) or {}
        week = row.get("week") or {}
        five = row.get("five") or {}
        week_points = float(week.get("points") or 0.0)
        out[str(account)] = {
            "weekPerPct": token_model.blended_rate(plan, "week", week_points, float(week.get("tokens") or 0.0)),
            "fivePerPct": token_model.blended_rate(
                plan, "five", float(five.get("points") or 0.0), float(five.get("tokens") or 0.0)),
            "confidence": token_model.confidence(week_points),
            "ownWeekPoints": round(week_points, 1),
            "ownFivePoints": round(float(five.get("points") or 0.0), 1),
        }
    return out


def load_store(backup_root: Path) -> dict:
    from claude_swap.budget_source import CALIBRATION_FILENAME

    try:
        return _valid_store(json.loads((Path(backup_root) / CALIBRATION_FILENAME).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return empty_store()


def calibrate(backup_root: Path, projects_dir: Path | None = None, now: float | None = None) -> dict:
    """Read the new polls and tokens, add them to the store, write it, return it."""
    from claude_swap.budget_source import CALIBRATION_FILENAME
    from claude_swap.settings import atomic_write_json

    store = load_store(backup_root)
    polls = read_polls(backup_root)
    last = store.get("lastPoll")
    since = last[0] if isinstance(last, list) and len(last) == 4 else (polls[0][0] if polls else None)
    events = read_token_events(projects_dir or claude_projects_dir(), since) if since is not None else []
    store = accumulate(store, polls, events)
    store["updatedAt"] = _iso(time.time() if now is None else now)
    store["unit"] = token_model.UNIT
    atomic_write_json(Path(backup_root) / CALIBRATION_FILENAME, store)
    return store
