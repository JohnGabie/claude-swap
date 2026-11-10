"""Read-only usage feed for ``cswap pace --json``.

Projects :meth:`ClaudeAccountSwitcher.accounts_snapshot` (with ``fetch=set()``
and ``read_only=True``: the stored usage only, no network call, no token
renewal, no write) to the pace contract in
``docs/superpowers/specs/2026-11-10-pace-command-design.md``.

Privacy is by construction, not by filtering:

- Each account object is built key by key from an allowlist. Nothing from the
  snapshot (email, organization, account UUID, credential, ``last_error``) is
  ever copied as a dict, so a new snapshot field cannot leak through here.
- ``error`` is always one status word. No exception text, type or path is
  ever placed in the payload, and this module logs nothing.
- The plan helper reads the stored credential and returns only the plan word.
  It parses the blob itself and imports no token accessor.

This module writes nothing: it calls no store writer, and the read-only
snapshot skips ``clear_dead_token`` and the org-field migration of the
sequence file.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)

if TYPE_CHECKING:
    from claude_swap.models import AccountSnapshot
    from claude_swap.switcher import ClaudeAccountSwitcher

#: The pace contract's own version. Raise it only on a breaking change (P3).
SCHEMA_VERSION = 1

#: Exit codes of the pace contract. Exit 2 stays with argparse.
EXIT_OK = 0
EXIT_PARTIAL = 3
EXIT_UNAVAILABLE = 4

#: Plan words. Only these two stored values are recognized; anything else
#: (missing, ``None``, another tier name) reads as ``PLAN_UNKNOWN``.
PLAN_UNKNOWN = "unknown"
KNOWN_PLANS = frozenset({"pro", "max"})

#: Status words.
STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_API_KEY = "api_key"
STATUS_TOKEN_EXPIRED = "token_expired"
STATUS_RELOGIN_NEEDED = "relogin_needed"
STATUS_NO_CREDENTIALS = "no_credentials"
STATUS_UNAVAILABLE = "unavailable"

#: Statuses that carry usage windows. Every other status carries ``error``.
DATA_STATUSES = frozenset({STATUS_OK, STATUS_STALE})

#: Statuses of an account that was read. Every other status is a failed
#: account (token_expired, relogin_needed, no_credentials, unavailable).
READ_STATUSES = frozenset({STATUS_OK, STATUS_STALE, STATUS_API_KEY})

#: Collector sentinel -> status word. A sentinel not listed here (keychain
#: unavailable, foreign credential, a future one) reads as unavailable: its
#: usage is not this account's readable value (P2).
_SENTINEL_STATUS = {
    USAGE_API_KEY: STATUS_API_KEY,
    USAGE_TOKEN_EXPIRED: STATUS_TOKEN_EXPIRED,
    USAGE_RELOGIN_REQUIRED: STATUS_RELOGIN_NEEDED,
    USAGE_NO_CREDENTIALS: STATUS_NO_CREDENTIALS,
}

#: The only keys an account object may hold.
ACCOUNT_KEYS = frozenset(
    {"id", "alias", "plan", "status", "error", "fetchedAt", "fiveHour", "sevenDay"}
)
#: The only keys a window object may hold.
WINDOW_KEYS = frozenset({"pct", "resetsAt"})

#: Stored window name -> contract key.
_WINDOWS = (("five_hour", "fiveHour"), ("seven_day", "sevenDay"))

#: Valid ``pct`` range, inclusive.
PCT_MIN = 0.0
PCT_MAX = 100.0


def unavailable_payload() -> dict:
    """The whole-source failure payload: no accounts, one status word."""
    return {"schemaVersion": SCHEMA_VERSION, "error": STATUS_UNAVAILABLE}


def plan_from_credential(blob: object) -> str:
    """Return the plan word of one stored credential blob, and nothing else.

    Reads ``claudeAiOauth.subscriptionType``. Returns ``"pro"`` or ``"max"``
    only for exactly those stored values; anything else, including an
    unparsable blob or an API key, returns ``"unknown"``. Never raises, logs
    nothing, and keeps no reference to the blob.
    """
    if not isinstance(blob, str) or not blob:
        return PLAN_UNKNOWN
    try:
        data = json.loads(blob)
    except ValueError:
        return PLAN_UNKNOWN
    if not isinstance(data, dict):
        return PLAN_UNKNOWN
    oauth_block = data.get("claudeAiOauth")
    if not isinstance(oauth_block, dict):
        return PLAN_UNKNOWN
    plan = oauth_block.get("subscriptionType")
    if isinstance(plan, str) and plan in KNOWN_PLANS:
        return plan
    return PLAN_UNKNOWN


def read_plan(switcher: "ClaudeAccountSwitcher", account: "AccountSnapshot") -> str:
    """Return the plan word of one account's stored credential.

    The active account reads Claude Code's live store (the credential in
    use); every other account reads its slot backup. Both reads are the
    switcher's non-mutating readers. The blob stays local to this function
    and only the plan word leaves it. Any failure returns ``"unknown"``.
    """
    try:
        if account.is_active:
            blob = switcher._read_active_credentials().value
        else:
            blob = switcher._read_account_credentials(account.number, account.email)
    except Exception:
        return PLAN_UNKNOWN
    return plan_from_credential(blob)


def _iso_utc(epoch_s: object) -> str | None:
    """A stored epoch time as ISO 8601 UTC in whole seconds, or None when it
    cannot be converted.

    Only a real int or float is a time: a bool, str, bytes or any other type
    is None, never coerced. A value that is not finite or is out of the
    datetime range is None. The stored value never reaches the output.
    """
    if isinstance(epoch_s, bool) or not isinstance(epoch_s, (int, float)):
        return None
    try:
        seconds = float(epoch_s)
        if not math.isfinite(seconds):
            return None
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (ValueError, OverflowError, OSError, TypeError, ArithmeticError):
        return None


def _reset_time(value: object) -> str | None:
    """A stored reset time as ISO 8601 UTC in whole seconds, or None without
    a time zone or when it cannot be converted. Sub-second digits are cut,
    not rounded.

    An aware value at the edge of the datetime range (for example
    ``0001-01-01T00:00:00+01:00``) parses but raises OverflowError in the
    UTC conversion. Any conversion error drops only this ``resetsAt``; it
    must not make the whole command unavailable.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return (
            parsed.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (ValueError, OverflowError, TypeError, ArithmeticError):
        return None


def _pct(value: object) -> float | None:
    """A stored utilization as a float in range, or None when unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        # An int too large for a float (for example 10**400) raises here.
        pct = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    if not math.isfinite(pct) or not PCT_MIN <= pct <= PCT_MAX:
        return None
    return pct


def _window(stored: object) -> dict | None:
    """Project one stored window to ``{"pct", "resetsAt"}`` from an allowlist.

    An unusable ``pct`` drops the window (no guessed value, P2). A reset time
    without a time zone drops only ``resetsAt`` (P3).
    """
    if not isinstance(stored, dict):
        return None
    pct = _pct(stored.get("pct"))
    if pct is None:
        return None
    out: dict = {"pct": pct}
    resets_at = _reset_time(stored.get("resets_at"))
    if resets_at is not None:
        out["resetsAt"] = resets_at
    return out


def _status(account: "AccountSnapshot") -> str:
    """One status word for one snapshot account."""
    entry = account.usage
    if account.kind == "api_key":
        return STATUS_API_KEY
    if entry.sentinel is not None:
        return _SENTINEL_STATUS.get(entry.sentinel, STATUS_UNAVAILABLE)
    if not isinstance(entry.last_good, dict) or entry.fetched_at is None:
        return STATUS_UNAVAILABLE
    # A stored time that cannot be converted fails this account only: no
    # invented time, and no envelope-wide unavailable for one bad row (P2).
    if _iso_utc(entry.fetched_at) is None:
        return STATUS_UNAVAILABLE
    # decision_value() is the decision-grade read the --list JSON uses: the
    # last-good measurement while it is recent enough to act on.
    if isinstance(entry.decision_value(), dict):
        return STATUS_OK
    return STATUS_STALE


def _account_object(
    switcher: "ClaudeAccountSwitcher", account: "AccountSnapshot"
) -> dict:
    """Build one account object, key by key, from the allowlist."""
    status = _status(account)
    out: dict = {"id": int(account.number)}
    if isinstance(account.alias, str) and account.alias:
        out["alias"] = account.alias
    out["plan"] = read_plan(switcher, account)
    out["status"] = status
    if status not in DATA_STATUSES:
        out["error"] = status
        return out
    entry = account.usage
    fetched_at = _iso_utc(entry.fetched_at)
    if fetched_at is None:
        # _status already gives unavailable here; keep no invented time (P2).
        out["status"] = STATUS_UNAVAILABLE
        out["error"] = STATUS_UNAVAILABLE
        return out
    out["fetchedAt"] = fetched_at
    last_good = entry.last_good
    for stored_key, contract_key in _WINDOWS:
        window = _window(last_good.get(stored_key))
        if window is not None:
            out[contract_key] = window
    return out


def _exit_code(accounts: list[dict]) -> int:
    """0 when no account failed (also for zero accounts), 4 when every
    account failed, else 3 (at least one failed and at least one was read)."""
    read = sum(1 for a in accounts if a["status"] in READ_STATUSES)
    if read == len(accounts):
        return EXIT_OK
    if read == 0:
        return EXIT_UNAVAILABLE
    return EXIT_PARTIAL


def build_pace(switcher: "ClaudeAccountSwitcher") -> tuple[dict, int]:
    """Build the ``cswap pace --json`` payload and its exit code.

    Reads the stored usage only, with a snapshot that writes nothing
    (``accounts_snapshot(fetch=set(), read_only=True)``). Any
    exception gives :func:`unavailable_payload` and exit 4; its text, type
    and traceback never reach the payload.
    """
    try:
        snapshot = switcher.accounts_snapshot(fetch=set(), read_only=True)
        accounts = [_account_object(switcher, acc) for acc in snapshot.accounts]
    except Exception:
        return unavailable_payload(), EXIT_UNAVAILABLE
    for account in accounts:
        if not set(account) <= ACCOUNT_KEYS:
            return unavailable_payload(), EXIT_UNAVAILABLE
        for _stored_key, contract_key in _WINDOWS:
            if contract_key in account and not set(account[contract_key]) <= WINDOW_KEYS:
                return unavailable_payload(), EXIT_UNAVAILABLE
    return {"schemaVersion": SCHEMA_VERSION, "accounts": accounts}, _exit_code(accounts)
