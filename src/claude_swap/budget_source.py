"""The live budget: ``cswap pace`` payload + settings + calibration file.

Reads local files only (the stored usage, ``settings.json`` and the optional
``budget_calibration.json``). No network call, no credential read beyond what
``cswap pace`` already does for the plan word.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from claude_swap import budget, paths

CALIBRATION_FILENAME = "budget_calibration.json"
HOOK_STATE_FILENAME = "budget_hook.json"


# The measurement is run again when the stored one is older than this: the
# sample grows with the decision log.
RECALIBRATE_AFTER_S = 6 * 3600.0


def read_calibration(backup_root: Path) -> tuple[float | None, str | None, str | None]:
    """``(tokens per percent point of a Pro account, date measured, confidence)`` or Nones."""
    try:
        raw = json.loads((Path(backup_root) / CALIBRATION_FILENAME).read_text(encoding="utf-8"))
        value = raw.get("tokensPerPercentPro")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return None, None, None
        measured = raw.get("measuredAt")
        word = raw.get("confidence")
        return (
            float(value),
            measured if isinstance(measured, str) else None,
            word if isinstance(word, str) else None,
        )
    except (OSError, ValueError, AttributeError):
        return None, None, None


def _refresh_calibration(root: Path, pace: dict, now: float) -> None:
    """Measure again when there is no measurement or it is old. Never raises."""
    try:
        path = Path(root) / CALIBRATION_FILENAME
        try:
            if now - path.stat().st_mtime < RECALIBRATE_AFTER_S:
                return
        except OSError:
            pass
        from claude_swap import budget_calibrate

        plans = {
            str(a.get("id")): a.get("plan")
            for a in (pace.get("accounts") or [])
            if isinstance(a, dict) and isinstance(a.get("plan"), str)
        }
        budget_calibrate.calibrate(root, plans)
    except Exception:  # noqa: BLE001 - the budget does not depend on the measurement
        pass


def current_budget(now: float | None = None) -> dict:
    """The budget of the fleet now. Never raises: a failure gives advice ``unknown``."""
    now = time.time() if now is None else now
    try:
        from claude_swap.cli import _pace_payload
        from claude_swap.settings import load_settings

        root = paths.get_backup_root()
        pace, _code = _pace_payload()
        settings = load_settings(root)
        _refresh_calibration(root, pace, now)
        per_pct, measured_at, word = read_calibration(root)
        return budget.build_budget(
            pace, now,
            threshold=settings.threshold,
            weekly_threshold=settings.weekly_threshold,
            tokens_per_percent_pro=per_pct,
            tokens_measured_at=measured_at,
            tokens_confidence=word,
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
