"""Fleet budget for planning: how much quota exists, where, and until when.

``build_budget`` is a pure function of the ``cswap pace`` payload, so it
holds what that payload holds: account numbers and plan words, no email,
no organization or account name, no token. It makes no network call.

Units. One capacity unit is the weekly quota of one Pro account
(``balance.PLAN_CAPACITY``). Anthropic does not publish the size of a plan
in tokens, so every token figure is an estimate and says so.

Advice. ``paceRatio`` is the used share of the fleet week divided by the
elapsed share (both weighted by capacity). 1.0 uses the whole week exactly.

    blocked        no account is usable now
    slow_down      paceRatio > SLOW_DOWN_RATIO: the fleet ends before its resets
    can_speed_up   paceRatio < SPEED_UP_RATIO: quota expires unused
    ok             between the two
    unknown        no account with a measured weekly window
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from claude_swap import balance

SCHEMA_VERSION = 1
SLOW_DOWN_RATIO = 1.05
SPEED_UP_RATIO = 0.75
# Below this elapsed share a projection divides by almost nothing.
MIN_ELAPSED_FOR_PROJECTION = 0.05
DATA_STATUSES = frozenset({"ok", "stale"})
# A reading older than this does not count: the account is listed, not measured.
MAX_READING_AGE_S = 2 * 3600.0
# Planning horizons: one day, three days, one week.
HORIZON_HOURS = (24, 72, 168)

NOTES = (
    "status: ok, no-accounts (nothing is set up) or unavailable (the usage source cannot be read).",
    "fetchedAt is the oldest provider read among the measured accounts; each account has its own.",
    "An account with no reading in the last 2 hours is listed with measured false and "
    "noRecentReading true: it is not counted as fuel.",
    "Per account: fiveHour and sevenDay carry usedPercent (0-100, the same number as pct) and "
    "resetsAt (ISO 8601). An account with no open 5-hour window has usedPercent 0 and no resetsAt.",
    "1 capacity unit = the weekly quota of one Pro account (pro 1, max 5, ultra 10).",
    "Plan sizes are not published: capacity units and token figures are estimates.",
    "expectedLossUnits is the quota that expires unused if the current pace continues.",
    "horizons: the fuel for a plan. inTankUnits is what is left now; expiringUnits is the part of "
    "it that is lost at a weekly reset inside the horizon unless it is used first; refillUnits "
    "comes back at those resets; availableUnits = inTankUnits + refillUnits. It is an upper "
    "bound: the 5-hour windows limit how fast one account can be used. reachableTokens "
    "applies that limit: plan with reachableTokens.",
    "Token figures are weighted tokens (see tokens.definition): output counts 5 times input, "
    "a cache read 0.01, and each model has a weight. They are estimates with a confidence word.",
)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window(raw) -> balance.Window | None:
    if not isinstance(raw, dict):
        return None
    pct = raw.get("pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
        return None
    return balance.Window(float(pct), _ts(raw.get("resetsAt")))


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _account(raw: dict, now: float, threshold: float, weekly_threshold: float) -> dict:
    """One account of the budget. ``measured`` is False with no usable reading."""
    plan = raw.get("plan") if isinstance(raw.get("plan"), str) else "unknown"
    capacity = balance.plan_capacity(plan)
    out: dict = {
        "id": raw.get("id"),
        "plan": plan,
        "capacityUnits": capacity,
        "status": raw.get("status"),
        "fetchedAt": raw.get("fetchedAt") if isinstance(raw.get("fetchedAt"), str) else None,
        "measured": False,
        "usableNow": False,
    }
    if raw.get("status") not in DATA_STATUSES:
        return out
    fetched = _ts(out["fetchedAt"])
    if fetched is None or now - fetched > MAX_READING_AGE_S:
        # An account the provider has not answered for hours is not fuel:
        # its old reading can even look like a full new week (account 2,
        # 2026-11-17 to 2026-11-21: no reading for 4 days, counted usable).
        out["noRecentReading"] = True
        return out
    account = balance.Account(0, plan, _window(raw.get("fiveHour")), _window(raw.get("sevenDay")))
    weekly = balance._weekly(account, now)
    if weekly is None:
        return out
    pct7, elapsed, counted = weekly
    no_five = balance._plan_key(plan) in balance.NO_FIVE_HOUR_LIMIT_PLANS
    pct5, reset5 = balance._window(account.five_hour, now)
    reset7 = balance._next_weekly_reset(account.seven_day, now)

    blocked_by, usable_at = None, None
    if pct7 >= weekly_threshold:
        blocked_by, usable_at = "sevenDay", reset7
    elif not no_five and pct5 is not None and pct5 >= threshold:
        blocked_by, usable_at = "fiveHour", reset5

    projected = None
    if counted and elapsed >= MIN_ELAPSED_FOR_PROJECTION:
        projected = min(100.0, pct7 / elapsed)
    remaining_pct = max(0.0, 100.0 - pct7)
    out.update({
        "measured": True,
        "usableNow": blocked_by is None,
        "blockedBy": blocked_by,
        "usableAt": _iso(usable_at),
        "sevenDay": {
            "pct": _round(pct7),
            "usedPercent": _round(pct7),
            "resetsAt": _iso(reset7),
            "elapsedPct": _round(elapsed * 100.0) if counted else None,
            "remainingPct": _round(remaining_pct),
            "remainingUnits": _round(capacity * remaining_pct / 100.0, 3),
            "projectedEndPct": _round(projected),
            "expectedLossUnits": (
                None if projected is None else _round(capacity * (100.0 - projected) / 100.0, 3)
            ),
        },
    })
    if not no_five and pct5 is not None:
        out["fiveHour"] = {
            "pct": _round(pct5),
            "usedPercent": _round(pct5),
            "resetsAt": _iso(reset5),
            "headroomPct": _round(max(0.0, threshold - pct5)),
        }
    # Internal fields for the fleet sums; removed before the payload leaves.
    out["_u"], out["_e"], out["_counted"], out["_reset7"] = pct7 / 100.0, elapsed, counted, reset7
    out["_reset5"], out["_head5"] = reset5, (None if no_five or pct5 is None else max(0.0, threshold - pct5))
    out["_line5"] = None if no_five else threshold
    return out


def _add_tokens(account: dict, rate: dict | None) -> None:
    """Token figures of one measured account, from its per-percent rates."""
    account["_week_rate"] = account["_five_rate"] = None
    if not isinstance(rate, dict):
        return
    week, five = rate.get("weekPerPct"), rate.get("fivePerPct")
    if not isinstance(week, (int, float)) or week <= 0:
        return
    account["_week_rate"] = float(week)
    account["tokenConfidence"] = rate.get("confidence")
    seven = account["sevenDay"]
    seven["tokensPerPercent"] = round(week)
    seven["remainingTokens"] = round(seven["remainingPct"] * week)
    if "fiveHour" in account and isinstance(five, (int, float)) and five > 0:
        account["_five_rate"] = float(five)
        account["fiveHour"]["tokensPerPercent"] = round(five)
        account["fiveHour"]["headroomTokens"] = round(account["fiveHour"]["headroomPct"] * five)


FIVE_HOURS_S = 5 * 3600.0


def _reachable_tokens(account: dict, now: float, end: float) -> float | None:
    """Tokens of one account that fit before ``end``: the weekly quota in
    reach, limited by what its 5-hour windows let through. None with no rate."""
    week = account["_week_rate"]
    if week is None:
        return None
    weekly = account["sevenDay"]["remainingPct"] * week
    if account["_reset7"] is not None and account["_reset7"] <= end:
        weekly += 100.0 * week
    line, five = account["_line5"], account["_five_rate"]
    if line is None or five is None:  # no 5-hour limit, or no rate for it
        return weekly
    start = now
    through = 0.0
    if account["_reset5"] is not None and account["_reset5"] > now:  # a window is open
        through += (account["_head5"] or 0.0) * five
        start = account["_reset5"]
    if end > start:
        windows = -(-(end - start) // FIVE_HOURS_S)  # ceil: a window started late still counts
        through += windows * line * five
    return min(weekly, through)


def _advice(accounts: list[dict], pace_ratio: float | None, next_usable: float | None) -> tuple[str, str]:
    measured = [a for a in accounts if a["measured"]]
    usable = [a for a in measured if a["usableNow"]]
    if not measured:
        return "unknown", "no account has a measured weekly window"
    if not usable:
        when = f"; the next account is usable at {_iso(next_usable)}" if next_usable else ""
        return "blocked", f"no account is usable now{when}"
    count = f"{len(usable)} of {len(accounts)} accounts usable"
    if pace_ratio is None:
        return "ok", f"{count}; the week is too new for a pace"
    pct = f"the fleet is at {pace_ratio * 100:.0f}% of the pace that uses the whole week"
    if pace_ratio > SLOW_DOWN_RATIO:
        return "slow_down", f"{pct}; it ends before its resets; {count}"
    if pace_ratio < SPEED_UP_RATIO:
        return "can_speed_up", f"{pct}; quota expires unused; {count}"
    return "ok", f"{pct}; {count}"


def build_budget(
    pace: dict,
    now: float,
    *,
    threshold: float = balance.DEFAULT_THRESHOLD,
    weekly_threshold: float | None = None,
    token_rates: dict | None = None,
    token_meta: dict | None = None,
    horizon_hours: tuple | None = None,
) -> dict:
    """The budget of the fleet at ``now``, from one ``cswap pace`` payload.

    ``threshold`` and ``weekly_threshold`` are the switch lines of the
    engine: an account at or over a line is not usable. A source that failed
    (``pace`` with an ``error``) gives advice ``unknown`` and no account.

    ``token_rates`` maps an account id to ``{"weekPerPct", "fivePerPct",
    "confidence"}`` (see ``budget_calibrate.rates``). With it, every quota
    figure also comes in tokens. ``token_meta`` describes the token unit.
    """
    weekly_line = threshold if weekly_threshold is None else weekly_threshold
    raw_accounts = pace.get("accounts") if isinstance(pace, dict) else None
    accounts = [
        _account(raw, now, threshold, weekly_line)
        for raw in (raw_accounts if isinstance(raw_accounts, list) else [])
        if isinstance(raw, dict)
    ]

    used = line = capacity = remaining = 0.0
    for a in accounts:
        if not a["measured"]:
            continue
        capacity += a["capacityUnits"]
        remaining += a["sevenDay"]["remainingUnits"]
        if a["_counted"]:
            used += a["capacityUnits"] * a["_u"]
            line += a["capacityUnits"] * a["_e"]
    pace_ratio = used / line if line >= balance.F_FLOOR else None
    expected_loss = None
    if pace_ratio is not None and line / max(capacity, 1e-9) >= MIN_ELAPSED_FOR_PROJECTION:
        expected_loss = capacity * max(0.0, 1.0 - min(1.0, pace_ratio))

    waits = [_ts(a["usableAt"]) for a in accounts if a["measured"] and not a["usableNow"] and a.get("usableAt")]
    next_usable = min((w for w in waits if w is not None), default=None)
    advice, reason = _advice(accounts, pace_ratio, next_usable)

    resets = sorted(
        (
            {
                "at": _iso(a["_reset7"]),
                "accountId": a["id"],
                "expiringUnits": a["sevenDay"]["remainingUnits"],
                "refillUnits": a["capacityUnits"],
                "hoursFromNow": _round((a["_reset7"] - now) / 3600.0, 1),
            }
            for a in accounts
            if a["measured"] and a["_reset7"] is not None
        ),
        key=lambda r: r["at"],
    )
    rates = token_rates if isinstance(token_rates, dict) else {}
    measured_accounts = [a for a in accounts if a["measured"]]
    for a in measured_accounts:
        _add_tokens(a, rates.get(str(a["id"])))
    has_tokens = bool(measured_accounts) and all(a["_week_rate"] is not None for a in measured_accounts)
    horizons = []
    for hours in (horizon_hours or HORIZON_HOURS):
        end = now + hours * 3600.0
        inside = [a for a in accounts if a["measured"] and a["_reset7"] is not None and a["_reset7"] <= end]
        expiring = sum(a["sevenDay"]["remainingUnits"] for a in inside)
        refill = sum(a["capacityUnits"] for a in inside)
        total = remaining + refill
        row = {
            "hours": hours,
            "until": _iso(end),
            "inTankUnits": _round(remaining, 3),
            "expiringUnits": _round(expiring, 3),
            "refillUnits": _round(refill, 3),
            "availableUnits": _round(total, 3),
        }
        if has_tokens:
            row["availableTokens"] = round(
                sum(a["sevenDay"]["remainingTokens"] for a in measured_accounts)
                + sum(100.0 * a["_week_rate"] for a in inside)
            )
            row["reachableTokens"] = min(
                row["availableTokens"],
                round(sum(_reachable_tokens(a, now, end) for a in measured_accounts)),
            )
            row["reachablePctOfAvailable"] = (
                _round(100.0 * row["reachableTokens"] / row["availableTokens"], 1)
                if row["availableTokens"] else None
            )
        horizons.append(row)

    tokens = None
    if has_tokens:
        order = {"prior": 0, "low": 1, "medium": 2, "high": 3}
        words = [a.get("tokenConfidence") for a in measured_accounts]
        tokens = {
            "estimate": True,
            **(token_meta if isinstance(token_meta, dict) else {}),
            "confidence": min(words, key=lambda w: order.get(w, 0)) if words else None,
            "remainingTokens": sum(a["sevenDay"]["remainingTokens"] for a in measured_accounts),
            "usableNowTokens": sum(
                a["sevenDay"]["remainingTokens"] for a in measured_accounts if a["usableNow"]
            ),
            "fiveHourHeadroomTokens": sum(
                a["fiveHour"].get("headroomTokens", 0)
                for a in measured_accounts if a["usableNow"] and "fiveHour" in a
            ),
        }

    for a in accounts:
        for key in [k for k in a if k.startswith("_")]:
            a.pop(key, None)

    measured = [a for a in accounts if a["measured"]]
    if not isinstance(pace, dict) or pace.get("error") or not isinstance(raw_accounts, list):
        status = "unavailable"
    elif not accounts:
        status = "no-accounts"
    else:
        status = "ok"
    reads = sorted(t for t in (_ts(a.get("fetchedAt")) for a in measured) if t is not None)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": status,
        "generatedAt": _iso(now),
        # The OLDEST provider read among the measured accounts: data is as old as this.
        "fetchedAt": _iso(reads[0]) if reads else None,
        "newestFetchedAt": _iso(reads[-1]) if reads else None,
        "advice": advice,
        "reason": reason,
        "fleet": {
            "accounts": len(accounts),
            "measuredAccounts": len(measured),
            "usableNow": sum(1 for a in measured if a["usableNow"]),
            "nextUsableAt": _iso(next_usable) if advice == "blocked" else None,
            "capacityUnits": _round(capacity, 3),
            "remainingUnits": _round(remaining, 3),
            "usableNowUnits": _round(
                sum(a["sevenDay"]["remainingUnits"] for a in measured if a["usableNow"]), 3
            ),
            "weekUsedPct": _round(100.0 * used / capacity) if capacity else None,
            "weekElapsedPct": _round(100.0 * line / capacity) if capacity else None,
            "paceRatio": _round(pace_ratio, 3),
            "expectedLossUnits": _round(expected_loss, 3),
            "thresholdPct": threshold,
            "weeklyThresholdPct": weekly_line,
        },
        "accounts": accounts,
        "resets": resets,
        "horizons": horizons,
        "tokens": tokens,
        "notes": list(NOTES),
    }


def advice_line(budget: dict) -> str:
    """One line for a prompt hook: the advice, the reason, where to get more."""
    fleet = budget.get("fleet") or {}
    units = fleet.get("usableNowUnits")
    extra = f"; {units:g} capacity units usable now" if isinstance(units, (int, float)) else ""
    return (
        f"cswap budget: {budget.get('advice')} - {budget.get('reason')}{extra}. "
        "Details: MCP tool get_budget."
    )


# --- Prompt hook ----------------------------------------------------------

HOOK_DWELL_S = 600.0
HOOK_SESSIONS_KEPT = 50
QUIET_ADVICE = frozenset({"ok", "unknown"})


def hook_should_show(previous: dict | None, advice: str, now: float, dwell_s: float = HOOK_DWELL_S) -> bool:
    """Show the advice line to a session only when it carries news.

    A first line in a session needs an advice that asks for something. Later,
    the advice must differ from the last one shown, and ``dwell_s`` must have
    passed, so an advice that flips at a border does not flood the context.
    """
    if not isinstance(previous, dict) or "advice" not in previous:
        return advice not in QUIET_ADVICE
    if advice == previous.get("advice"):
        return False
    shown_at = previous.get("shownAt")
    return not isinstance(shown_at, (int, float)) or now - shown_at >= dwell_s


def hook_record(state: dict, session: str, advice: str, now: float) -> dict:
    """``state`` with ``session`` set to the advice just shown; old sessions drop."""
    sessions = dict(state.get("sessions") or {}) if isinstance(state, dict) else {}
    sessions[session] = {"advice": advice, "shownAt": now}
    newest = sorted(sessions, key=lambda k: sessions[k].get("shownAt") or 0.0)[-HOOK_SESSIONS_KEPT:]
    return {"schemaVersion": SCHEMA_VERSION, "sessions": {k: sessions[k] for k in newest}}
