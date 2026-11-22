"""Fleet panel data: pure functions from the budget, the usage history and the decision log.

No I/O and no clock: the caller passes ``now`` (epoch seconds) and, for day columns,
``tz`` (default: the process's local zone). Token values are weighted tokens.
Spec: fleet-panel-design FP3-FP9, FP12.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone, tzinfo

from . import budget as budget_mod

# key: (span s, chart step s, forward horizon s, project column s)
PERIODS = {
    "1h": (3600, 300, 3600, 300),
    "6h": (21600, 300, 21600, 900),
    "12h": (43200, 900, 43200, 3600),
    "1d": (86400, 1800, 86400, 3600),
    "7d": (604800, 7200, 259200, 86400),
    "15d": (1296000, 21600, 604800, 86400),
    "30d": (2592000, 43200, 604800, 86400),
}
PRICES_USD_MONTH = {"pro": 20.0, "max": 100.0, "ultra": 200.0}

DAY_S = 86400
WEEK_S = 7 * DAY_S
WEEK_MINUTES = 10080
SPARK_BUCKETS = 24
SPARK_STEP_S = 3600
NOW_WINDOW_S = 3600
EVENTS_HORIZON_S = 72 * 3600
SWITCH_SLICES = 6
PROJECT_ROWS = 6
PROJECT_TOP = PROJECT_ROWS - 1
NO_READING_GAP_S = 2 * 3600
WEEKS_PER_YEAR = 52
MONTHS_PER_YEAR = 12
FUEL_LABELS = {24: "24 h", 72: "3 dias", 168: "7 dias"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _buckets(history: dict):
    """(start epoch, bucket dict) for each well-formed bucket."""
    raw = history.get("buckets") if isinstance(history, dict) else None
    for key, value in (raw or {}).items():
        try:
            start = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            yield start, value


def _bucket_total(bucket: dict) -> float:
    return sum(v for v in (bucket.get("m") or {}).values() if isinstance(v, (int, float)))


def _series(history: dict, start: float, end: float, step: int) -> list[float]:
    """Weighted tokens per minute per step; bucket b is in [start, end)."""
    count = max(math.ceil((end - start) / step), 0)
    sums = [0.0] * count
    for b, bucket in _buckets(history):
        if start <= b < end:
            sums[int((b - start) // step)] += _bucket_total(bucket)
    return [v / (step / 60.0) for v in sums]


def _totals(history: dict, start: float, end: float, field: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for b, bucket in _buckets(history):
        if start <= b < end:
            for key, value in (bucket.get(field) or {}).items():
                if isinstance(value, (int, float)):
                    out[key] = out.get(key, 0.0) + value
    return out


def _local_midnight(t: float, tz: tzinfo | None) -> float:
    d = datetime.fromtimestamp(t, tz=tz).date()
    return datetime.combine(d, time.min, tzinfo=tz).timestamp() if tz else datetime.combine(d, time.min).timestamp()


def _day_starts(start: float, end: float, tz: tzinfo | None) -> list[float]:
    """Local midnights from the one at or before ``start`` to the one at or before ``end``."""
    d = datetime.fromtimestamp(start, tz=tz).date()
    last = datetime.fromtimestamp(end, tz=tz).date()
    out = []
    while d <= last:
        midnight = datetime.combine(d, time.min, tzinfo=tz) if tz else datetime.combine(d, time.min)
        out.append(midnight.timestamp())
        d += timedelta(days=1)
    return out


def _columns(span: int, col: int, now: float, tz: tzinfo | None) -> list[float]:
    start = now - span
    if col >= DAY_S:
        return _day_starts(start, now, tz)
    return [start + i * col for i in range(math.ceil(span / col))]


def _column_index(starts: list[float], t: float) -> int | None:
    """Index of the column that holds ``t``; the last column runs to the end."""
    if not starts or t < starts[0]:
        return None
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _dur(seconds: float) -> str:
    """Under 60 min ``45 min``, under 48 h ``10 h``, else ``2 d``."""
    seconds = max(seconds, 0.0)
    minutes = _half_up(seconds / 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    if seconds < 48 * 3600:
        return f"{_half_up(seconds / 3600)} h"
    return f"{_half_up(seconds / DAY_S)} d"


# ---------------------------------------------------------------------------
# labels and status
# ---------------------------------------------------------------------------


def model_label(model_id: str | None) -> str:
    """``claude-opus-5-5`` -> ``Opus 5.5``; an id without ``claude-`` stays as it is."""
    if not model_id:
        return "unknown"
    if not model_id.startswith("claude-"):
        return model_id
    parts = model_id[len("claude-"):].split("-")
    if len(parts) > 1 and len(parts[-1]) == 8 and parts[-1].isdigit():
        parts = parts[:-1]
    family = next((p for p in parts if not p.isdigit()), None)
    if family is None:
        return model_id
    versions = [p for p in parts if p.isdigit()]
    name = family.capitalize()
    return f"{name} {'.'.join(versions)}" if versions else name


def _status(pace) -> tuple[str, str]:
    """Raw ratio against the borders: the rounded percent would move the borders."""
    if pace is None:
        return "Semana nova", "neutral"
    if pace > budget_mod.SLOW_DOWN_RATIO:
        return "Acaba antes do reset", "critical"
    if pace < budget_mod.SPEED_UP_RATIO:
        return "Vai sobrar cota", "warning"
    return "No ritmo", "neutral"


def _fleet(budget: dict) -> dict:
    fleet = budget.get("fleet") if isinstance(budget, dict) else None
    return fleet if isinstance(fleet, dict) else {}


def _accounts(budget: dict) -> list[dict]:
    accounts = budget.get("accounts") if isinstance(budget, dict) else None
    return [a for a in (accounts or []) if isinstance(a, dict)]


def square(budget: dict, history: dict, now: float) -> dict:
    pace = _num(_fleet(budget).get("paceRatio"))
    return {
        "sparkline24h": _series(history, now - SPARK_BUCKETS * SPARK_STEP_S, now, SPARK_STEP_S),
        "pace": None if pace is None else _half_up(pace * 100),
        "level": _status(pace)[1],
    }


# ---------------------------------------------------------------------------
# sustainable rate
# ---------------------------------------------------------------------------


def _week_inputs(budget: dict) -> list[tuple[float, float, float]]:
    """(remainingTokens, tokensPerPercent, weekly reset epoch) of each account that has all three."""
    out = []
    for a in _accounts(budget):
        seven = a.get("sevenDay")
        if not isinstance(seven, dict):
            continue
        remaining = _num(seven.get("remainingTokens"))
        per_pct = _num(seven.get("tokensPerPercent"))
        reset = _ts(seven.get("resetsAt"))
        if remaining is not None and per_pct is not None and reset is not None:
            out.append((remaining, per_pct, reset))
    return out


def _sustainable_at(inputs, now: float, at: float) -> float:
    """Before an account's weekly reset: what is left spread over the time to the reset
    (measured from ``now``); after it: a whole new week, ``100 * tokensPerPercent / 10080``."""
    total = 0.0
    for remaining, per_pct, reset in inputs:
        if at < reset:
            total += remaining / max((reset - now) / 60.0, 1.0)
        else:
            total += 100.0 * per_pct / WEEK_MINUTES
    return total


def _fuel(budget: dict, forward: int) -> list[dict]:
    tokens = budget.get("tokens") if isinstance(budget, dict) else None
    if not isinstance(tokens, dict):
        return []
    out = []
    for row in budget.get("horizons") or []:
        hours, reach = (row.get("hours"), row.get("reachableTokens")) if isinstance(row, dict) else (None, None)
        if hours is None or reach is None or hours * 3600 > forward:
            continue
        out.append({"label": FUEL_LABELS.get(hours, f"{hours} h"), "tokens": reach, "hours": hours})
    return out


# ---------------------------------------------------------------------------
# payload parts
# ---------------------------------------------------------------------------


def _models(history: dict, start: float, now: float) -> list[dict]:
    totals = {k: v for k, v in _totals(history, start, now, "m").items() if v > 0}
    grand = sum(totals.values())
    rows = sorted(totals.items(), key=lambda kv: -kv[1])
    return [
        {"id": k, "label": model_label(k), "tokens": v, "share": round(100.0 * v / grand, 2)}
        for k, v in rows
    ]


def _projects(history: dict, start: float, now: float, col: int, tz) -> dict:
    cols = _columns(int(now - start), col, now, tz)
    totals = {k: v for k, v in _totals(history, start, now, "p").items() if v > 0}
    grand = sum(totals.values())
    ranked = sorted(totals, key=lambda k: (-totals[k], k))
    top = ranked[:PROJECT_TOP]
    rest = ranked[PROJECT_TOP:]
    cells = {k: [0.0] * len(cols) for k in totals}
    for b, bucket in _buckets(history):
        if not start <= b < now:
            continue
        i = _column_index(cols, b)
        if i is None:
            continue
        for key, value in (bucket.get("p") or {}).items():
            if key in cells and isinstance(value, (int, float)):
                cells[key][i] += value
    rows = []
    for k in top:
        rows.append({"key": k, "cells": cells[k], "tokens": totals[k], "share": 100.0 * totals[k] / grand})
    if rest:
        other_cells = [sum(cells[k][i] for k in rest) for i in range(len(cols))]
        other = sum(totals[k] for k in rest)
        rows.append({"key": f"outros ({len(rest)})", "cells": other_cells, "tokens": other,
                     "share": 100.0 * other / grand})
    while len(rows) < PROJECT_ROWS:
        rows.append({"key": "", "cells": [], "tokens": 0, "share": 0})
    return {"columns": [_iso(c) for c in cols], "rows": rows}


def _events72h(budget: dict, now: float) -> list[dict]:
    events = []
    for a in _accounts(budget):
        num = a.get("id")
        five = a.get("fiveHour")
        if isinstance(five, dict):
            at = _ts(five.get("resetsAt"))
            pct = _num(five.get("pct"))
            if at is not None and now < at <= now + EVENTS_HORIZON_S and pct is not None and pct > 0:
                events.append({"kind": "five", "account": num, "at": _iso(at),
                               "text": f"conta {num} · janela de 5 h volta em {_dur(at - now)}",
                               "_t": at})
        seven = a.get("sevenDay")
        if isinstance(seven, dict):
            at = _ts(seven.get("resetsAt"))
            if at is not None and now < at <= now + EVENTS_HORIZON_S:
                left = _num(seven.get("remainingPct")) or 0.0
                events.append({"kind": "week", "account": num, "at": _iso(at),
                               "text": f"conta {num} · semana renova · sobra {_half_up(left)}% em {_dur(at - now)}",
                               "_t": at})
    events.sort(key=lambda e: e["_t"])
    for e in events:
        del e["_t"]
    return events


def _event_times(decisions: list[dict], kind: str) -> list[tuple[float, dict]]:
    out = []
    for d in decisions:
        if isinstance(d, dict) and d.get("event") == kind:
            t = _ts(d.get("ts"))
            if t is not None:
                out.append((t, d))
    return out


def _log_since(decisions: list[dict]) -> float | None:
    times = [t for d in decisions if isinstance(d, dict) and (t := _ts(d.get("ts"))) is not None]
    return min(times) if times else None


def _switches(decisions: list[dict], start: float, now: float, col: int, tz, log_since) -> dict:
    if col >= DAY_S:
        starts = _day_starts(start, now, tz)
    else:
        width = (now - start) / SWITCH_SLICES
        starts = [start + i * width for i in range(SWITCH_SLICES)]
    midnight = _local_midnight(now, tz)
    bars = [0] * len(starts)
    count = today = 0
    for t, d in _event_times(decisions, "switch"):
        if d.get("dryRun") is True or not start <= t <= now:
            continue
        count += 1
        if t >= midnight:
            today += 1
        i = _column_index(starts, t)
        if i is not None:
            bars[i] += 1
    return {
        "count": count,
        "today": today,
        "bars": [{"start": _iso(s), "count": c} for s, c in zip(starts, bars)],
        "logSince": None if log_since is None else _iso(log_since),
    }


def _unused(budget: dict, decisions: list[dict], start: float, now: float, log_since) -> dict:
    polls = sorted(_event_times(decisions, "poll"), key=lambda p: p[0])
    items = []
    for a in _accounts(budget):
        seven = a.get("sevenDay")
        reset0 = _ts(seven.get("resetsAt")) if isinstance(seven, dict) else None
        if reset0 is None or log_since is None:
            continue
        num = a.get("id")
        per_pct = _num(seven.get("tokensPerPercent"))
        price = PRICES_USD_MONTH.get(str(a.get("plan", "")).lower())
        k = 1
        while reset0 - k * WEEK_S >= start:
            reset = reset0 - k * WEEK_S
            k += 1
            if reset > now or reset <= log_since:
                continue
            reading = None
            for t, d in polls:
                if t >= reset:
                    break
                win = (d.get("windowsPct") or {}).get(str(num))
                pct = _num(win.get("7d")) if isinstance(win, dict) else None
                if pct is not None:
                    reading = (t, pct)
            if reading is None:
                continue
            unused = _half_up(100.0 - reading[1])
            if unused <= 0:
                continue
            items.append({
                "account": num,
                "pct": unused,
                "usd": None if price is None else unused / 100.0 * price * MONTHS_PER_YEAR / WEEKS_PER_YEAR,
                "tokens": None if per_pct is None else _half_up(unused * per_pct),
                "noReading": reset - reading[0] > NO_READING_GAP_S,
                "at": _iso(reset),
                "_t": reset,
            })
    items.sort(key=lambda i: (i["_t"], i["account"] if isinstance(i["account"], int) else 0))
    usd = sum(i["usd"] for i in items if i["usd"] is not None)
    tokens = sum(i["tokens"] for i in items if i["tokens"] is not None)
    for i in items:
        del i["_t"]
        if i["usd"] is not None:
            i["usd"] = round(i["usd"], 2)
    return {"usd": round(usd, 2), "tokens": tokens, "items": items}


# ---------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------


def build_fleet(budget: dict, history: dict, decisions: list[dict], now: float,
                period: str = "7d", tz: tzinfo | None = None) -> dict:
    if period not in PERIODS:
        raise ValueError(f"unknown period: {period!r}")
    span, step, forward, col = PERIODS[period]
    start = now - span
    fleet = _fleet(budget)
    pace = _num(fleet.get("paceRatio"))
    word, level = _status(pace)
    used, elapsed = _num(fleet.get("weekUsedPct")), _num(fleet.get("weekElapsedPct"))
    caption = "Onde a semana da frota fecha nesse ritmo"
    if used is not None and elapsed is not None:
        caption += f" · {_half_up(used)}% usado com {_half_up(elapsed)}% do tempo"

    inputs = _week_inputs(budget)
    sustainable = _sustainable_at(inputs, now, now)
    now_per_min = sum(
        _bucket_total(b) for s, b in _buckets(history) if now - NOW_WINDOW_S < s <= now
    ) / 60.0
    tokens = budget.get("tokens") if isinstance(budget, dict) else None
    usable_tokens = _num(tokens.get("usableNowTokens")) if isinstance(tokens, dict) else None
    lasts = usable_tokens / now_per_min / 60.0 if usable_tokens and now_per_min else None
    tiles = {
        "nowPerMin": now_per_min,
        "nowSpark24h": _series(history, now - SPARK_BUCKETS * SPARK_STEP_S, now, SPARK_STEP_S),
        "ratio": round(now_per_min / sustainable, 1) if sustainable else None,
        "lastsHours": lasts,
        "usableNowTokens": None if usable_tokens is None else int(usable_tokens),
        "usable": fleet.get("usableNow", 0),
        "accounts": fleet.get("accounts", len(_accounts(budget))),
    }
    series = {
        "start": _iso(start),
        "step": step,
        "now": _iso(now),
        "past": _series(history, start, now, step),
        "future": [_sustainable_at(inputs, now, now + i * step) for i in range(forward // step)],
        "sustainable": sustainable,
        "fuel": _fuel(budget, forward),
    }
    decisions = [d for d in decisions if isinstance(d, dict)]
    log_since = _log_since(decisions)
    return {
        "period": period,
        "pace": None if pace is None else _half_up(pace * 100),
        "status": {"word": word, "level": level},
        "caption": caption,
        "tiles": tiles,
        "series": series,
        "models": _models(history, start, now),
        "projects": _projects(history, start, now, col, tz),
        "events72h": _events72h(budget, now),
        "switches": _switches(decisions, start, now, col, tz, log_since),
        "unused": _unused(budget, decisions, start, now, log_since),
    }
