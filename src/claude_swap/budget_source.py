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


def current_budget(now: float | None = None) -> dict:
    """The budget of the fleet now. Never raises: a failure gives advice ``unknown``."""
    now = time.time() if now is None else now
    try:
        from claude_swap.cli import _pace_payload
        from claude_swap.settings import load_settings

        root = paths.get_backup_root()
        pace, _code = _pace_payload()
        settings = load_settings(root)
        return budget.build_budget(
            pace, now,
            threshold=settings.threshold,
            weekly_threshold=settings.weekly_threshold,
            token_rates=token_rates(root, pace, now),
            token_meta=token_model.meta(),
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
