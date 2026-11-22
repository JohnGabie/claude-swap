"""The live budget: ``cswap pace`` payload + settings + calibration file.

Reads local files only (the stored usage, ``settings.json`` and the optional
``budget_calibration.json``). No network call, no credential read beyond what
``cswap pace`` already does for the plan word.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from claude_swap import budget, paths, token_model

CALIBRATION_FILENAME = "budget_calibration.json"
HOOK_STATE_FILENAME = "budget_hook.json"


# New polls are added to the calibration store when it is older than this.
RECALIBRATE_AFTER_S = 15 * 60.0


def _plans(pace: dict) -> dict[str, str]:
    return {
        str(a.get("id")): a.get("plan")
        for a in (pace.get("accounts") or [])
        if isinstance(a, dict) and isinstance(a.get("plan"), str)
    }


def account_lines(root: Path, threshold: float, weekly_threshold: float | None) -> dict | None:
    """``{str(id): (5-hour line, weekly line or None)}`` of the accounts with a line of their own.

    Reads ``sequence.json`` of ``root`` and writes nothing. A read error gives
    None: every account then uses the general lines. Never raises.
    """
    try:
        from claude_swap import lines
        from claude_swap.switcher import ClaudeAccountSwitcher

        data = json.loads((Path(root) / "sequence.json").read_text(encoding="utf-8"))
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            return None
        out = {}
        for number in accounts:
            policy = ClaudeAccountSwitcher._policy_from_data(data, str(number))
            if policy.threshold is None and policy.weekly_threshold is None:
                continue
            out[str(number)] = lines.account_lines(policy, threshold, weekly_threshold)
        return out
    except Exception:  # noqa: BLE001 - the general lines still give a budget
        return None


def token_rates(root: Path, pace: dict, now: float) -> dict:
    """Per-account token rates; the store is brought up to date first. Never raises."""
    from claude_swap import budget_calibrate

    try:
        path = Path(root) / CALIBRATION_FILENAME
        try:
            fresh = now - path.stat().st_mtime < RECALIBRATE_AFTER_S
        except OSError:
            fresh = False
        store = budget_calibrate.load_store(root) if fresh else budget_calibrate.calibrate(root)
    except Exception:  # noqa: BLE001 - the priors still give an estimate
        store = budget_calibrate.empty_store()
    return budget_calibrate.rates(store, _plans(pace))


def current_budget(now: float | None = None, horizon_hours: tuple | None = None,
                   refresh_calibration: bool = True) -> dict:
    """The budget of the fleet now. Never raises: a failure gives advice ``unknown``.

    With ``refresh_calibration=False`` nothing is written: the token rates
    come from the calibration store as it is.
    """
    now = time.time() if now is None else now
    try:
        from claude_swap import budget_calibrate
        from claude_swap.cli import _pace_payload
        from claude_swap.settings import load_settings

        root = paths.get_backup_root()
        pace, _code = _pace_payload()
        settings = load_settings(root)
        return budget.build_budget(
            pace, now,
            threshold=settings.threshold,
            weekly_threshold=settings.weekly_threshold,
            token_rates=(
                token_rates(root, pace, now) if refresh_calibration
                else budget_calibrate.rates(budget_calibrate.load_store(root), _plans(pace))
            ),
            token_meta=token_model.meta(),
            horizon_hours=horizon_hours,
            account_lines=account_lines(root, settings.threshold, settings.weekly_threshold),
        )
    except Exception:  # noqa: BLE001 - a planner must get an answer, not a traceback
        return budget.build_budget({}, now)


def hook_line(session: str, now: float | None = None) -> str | None:
    """The advice line for ``session``, or None when there is no news.

    Keeps the last advice shown to each session in ``cache/budget_hook.json``.
    Never raises.
    """
    now = time.time() if now is None else now
    try:
        from claude_swap.settings import atomic_write_json

        current = current_budget(now)
        path = paths.get_backup_root() / "cache" / HOOK_STATE_FILENAME
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        sessions = state.get("sessions") if isinstance(state, dict) else None
        previous = sessions.get(session) if isinstance(sessions, dict) else None
        advice = current["advice"]
        if not budget.hook_should_show(previous, advice, now):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, budget.hook_record(state, session, advice, now))
        return budget.advice_line(current)
    except Exception:  # noqa: BLE001 - a hook must never break a prompt
        return None


def measure_account(account=None, now: float | None = None) -> dict:
    """Token rate of one account (or of all): rates, own sums and confidence. Never raises."""
    now = time.time() if now is None else now
    try:
        from claude_swap import budget_calibrate
        from claude_swap.cli import _pace_payload

        root = paths.get_backup_root()
        pace, _code = _pace_payload()
        plans = _plans(pace)
        rates = token_rates(root, pace, now)
        store = budget_calibrate.load_store(root)
        wanted = None if account is None else str(account)
        rows = []
        for number in sorted(plans, key=lambda n: (len(n), n)):
            if wanted is not None and number != wanted:
                continue
            own = store["accounts"].get(number) or {}
            rate = rates[number]
            rows.append({
                "id": int(number) if number.isdigit() else number,
                "plan": plans[number],
                "confidence": rate["confidence"],
                "weekly": {
                    "weightedTokensPerPoint": round(rate["weekPerPct"]),
                    "prior": round(token_model.prior_rate(plans[number], "week")),
                    "ownPoints": rate["ownWeekPoints"],
                    "ownTokens": round((own.get("week") or {}).get("tokens") or 0),
                },
                "fiveHour": {
                    "weightedTokensPerPoint": round(rate["fivePerPct"]),
                    "prior": round(token_model.prior_rate(plans[number], "five")),
                    "ownPoints": rate["ownFivePoints"],
                    "ownTokens": round((own.get("five") or {}).get("tokens") or 0),
                },
                "externalPoints": own.get("externalPoints", 0.0),
            })
        last = store.get("lastPoll")
        return {
            "generatedAt": budget._iso(now),
            "accounts": rows,
            "lastPointAt": budget._iso(last[0]) if isinstance(last, list) and last else None,
            "drift": None,
            **token_model.meta(),
            "assumptions": [
                "the rate is the account's own data pulled toward the community prior",
                "drift is not computed yet: the store keeps totals, not a recent window",
                "externalPoints are weekly points that rose with no token in the local logs",
            ],
        }
    except Exception:  # noqa: BLE001
        return {"generatedAt": budget._iso(now), "accounts": [], "error": "unavailable"}
