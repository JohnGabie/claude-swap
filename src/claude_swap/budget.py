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

NOTES = (
    "1 capacity unit = the weekly quota of one Pro account (pro 1, max 5, ultra 10).",
    "Plan sizes are not published: capacity units and token figures are estimates.",
    "expectedLossUnits is the quota that expires unused if the current pace continues.",
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
            "resetsAt": _iso(reset5),
            "headroomPct": _round(max(0.0, threshold - pct5)),
        }
    # Internal fields for the fleet sums; removed before the payload leaves.
    out["_u"], out["_e"], out["_counted"], out["_reset7"] = pct7 / 100.0, elapsed, counted, reset7
    return out


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
    tokens_per_percent_pro: float | None = None,
    tokens_measured_at: str | None = None,
    tokens_confidence: str | None = None,
) -> dict:
    """The budget of the fleet at ``now``, from one ``cswap pace`` payload.

    ``threshold`` and ``weekly_threshold`` are the switch lines of the
    engine: an account at or over a line is not usable. A source that failed
    (``pace`` with an ``error``) gives advice ``unknown`` and no account.
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
                "hoursFromNow": _round((a["_reset7"] - now) / 3600.0, 1),
            }
            for a in accounts
            if a["measured"] and a["_reset7"] is not None
        ),
        key=lambda r: r["at"],
    )
    for a in accounts:
        for key in ("_u", "_e", "_counted", "_reset7"):
            a.pop(key, None)

    tokens = None
    if tokens_per_percent_pro and tokens_per_percent_pro > 0:
        tokens = {
            "estimate": True,
            "tokensPerPercentPro": round(tokens_per_percent_pro),
            "measuredAt": tokens_measured_at,
            "confidence": tokens_confidence,
            "definition": "input + output + cache creation tokens; cache reads not counted",
            "remainingTokens": round(remaining * 100.0 * tokens_per_percent_pro),
            "usableNowTokens": round(
                sum(a["sevenDay"]["remainingUnits"] for a in accounts if a["measured"] and a["usableNow"])
                * 100.0 * tokens_per_percent_pro
            ),
        }

    measured = [a for a in accounts if a["measured"]]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": _iso(now),
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
