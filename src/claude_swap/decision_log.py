"""Decision log of the auto-switch engine, kept for bug reports.

One JSON line per engine event, in ``<backup_root>/decisions.jsonl``. The
file rotates (1 MB, 3 backups). Each line is the event's ``to_json()``
payload after ``scrub``: account numbers stay, emails, organization names
and account names do not. ``build_report`` joins the last lines with the
settings and the engine state into one text that is safe to share.

A failed write never reaches the engine: the log is an aid, not a
dependency.
"""

from __future__ import annotations

import json
import re
import threading
from collections import deque
from pathlib import Path

LOG_FILENAME = "decisions.jsonl"
MAX_BYTES = 1024 * 1024
BACKUPS = 3
REPORT_LINES = 400

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
# Keys whose value identifies a person or an organization.
_PRIVATE_KEYS = frozenset({
    "email", "emailaddress", "org", "organization", "organizationname",
    "orgname", "name", "displayname", "fullname", "accountname",
    "token", "accesstoken", "refreshtoken", "apikey",
})


def scrub(value):
    """Copy of ``value`` with no private key and no email-like text."""
    if isinstance(value, dict):
        return {
            _EMAIL.sub("<email>", str(k)): scrub(v)
            for k, v in value.items()
            if str(k).replace("_", "").replace("-", "").lower() not in _PRIVATE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _EMAIL.sub("<email>", value)
    return value


def log_path(backup_root: Path) -> Path:
    return Path(backup_root) / LOG_FILENAME


class DecisionLog:
    """Appends scrubbed engine events to the rotating decision log."""

    def __init__(self, backup_root: Path, *, max_bytes: int = MAX_BYTES, backups: int = BACKUPS):
        self._path = log_path(backup_root)
        self._max_bytes = max_bytes
        self._backups = backups
        self._lock = threading.Lock()

    def write(self, event) -> None:
        """Append one event. Never raises."""
        try:
            line = json.dumps(scrub(event.to_json()), separators=(",", ":"), default=str)
            with self._lock:
                self._rotate_if_full(len(line) + 1)
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:  # noqa: BLE001 - the log must not break the engine
            pass

    def _rotate_if_full(self, incoming: int) -> None:
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size + incoming <= self._max_bytes:
            return
        for i in range(self._backups, 0, -1):
            src = self._path if i == 1 else self._path.with_name(f"{LOG_FILENAME}.{i - 1}")
            if src.exists():
                src.replace(self._path.with_name(f"{LOG_FILENAME}.{i}"))


def tail(backup_root: Path, lines: int = REPORT_LINES) -> list[str]:
    """The last ``lines`` log lines, oldest first, across the rotated files."""
    base = log_path(backup_root)
    files = [base.with_name(f"{LOG_FILENAME}.{i}") for i in range(BACKUPS, 0, -1)] + [base]
    kept: deque[str] = deque(maxlen=lines)
    for path in files:
        try:
            with path.open(encoding="utf-8") as f:
                kept.extend(line.rstrip("\n") for line in f if line.strip())
        except OSError:
            continue
    return list(kept)


def _scrubbed_json_file(path: Path) -> str:
    try:
        return json.dumps(scrub(json.loads(path.read_text(encoding="utf-8"))), indent=2, default=str)
    except FileNotFoundError:
        return "(no file)"
    except (OSError, ValueError) as e:
        return f"(unreadable: {type(e).__name__})"


def build_report(backup_root: Path, *, version: str = "", lines: int = REPORT_LINES) -> str:
    """Text for a bug report: version, settings, engine state, last decisions.

    Every part goes through ``scrub``; the lines of the log were scrubbed
    when written and are scrubbed again here.
    """
    root = Path(backup_root)
    decisions = [_EMAIL.sub("<email>", line) for line in tail(root, lines)]
    parts = [
        f"cswap debug report (version {version or 'unknown'})",
        "",
        "## settings.json",
        _scrubbed_json_file(root / "settings.json"),
        "",
        "## autoswitch_state.json",
        _scrubbed_json_file(root / "autoswitch_state.json"),
        "",
        f"## last {len(decisions)} decisions",
        *(decisions or ["(no decisions logged)"]),
    ]
    return "\n".join(parts) + "\n"
