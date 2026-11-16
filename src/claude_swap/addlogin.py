"""Add an account by logging in, without touching the active login.

``claude auth login`` runs against a throwaway ``CLAUDE_CONFIG_DIR``, so the
default ``~/.claude`` login (and every running Claude Code) stays as it is.
``cswap add`` is then run against that same directory: ``add`` captures the
identity and credential of whatever profile the environment points at.

Used by ``cswap add --login`` and by the tray pill's "+" button.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError, ConfigError
from claude_swap.paths import get_default_claude_config_home, get_default_global_config_path

LOGIN_TIMEOUT_S = 300


def _live_files() -> list[Path]:
    return [get_default_claude_config_home() / ".credentials.json", get_default_global_config_path()]


def _live_email() -> str | None:
    """Email of the account the default profile is logged in as."""
    try:
        data = json.loads(get_default_global_config_path().read_text(encoding="utf-8"))
        return data.get("oauthAccount", {}).get("emailAddress") or None
    except (OSError, ValueError, AttributeError):
        return None


def _restore_if_switched(backup: dict[Path, bytes], email_before: str | None) -> None:
    """Undo the login if it somehow changed the active account.

    Compares the account identity only: a running Claude Code legitimately
    rewrites its token, and restoring an old copy of that would break it.
    """
    if _live_email() == email_before:
        return
    for path, data in backup.items():
        path.write_bytes(data)


def add_via_login(interactive: bool = False) -> str:
    """Log in to a new account in a temporary profile and add it to cswap.

    ``interactive`` keeps the terminal attached so ``claude`` can ask for a pasted
    code; otherwise stdin is closed (GUI callers). Returns cswap's own
    "Added Account N: ..." line.
    """
    claude = shutil.which("claude")
    if claude is None:
        raise ConfigError("The `claude` CLI was not found on PATH; it is needed to log in.")

    email_before = _live_email()
    backup = {p: p.read_bytes() for p in _live_files() if p.is_file()}
    tmp = Path(tempfile.mkdtemp(prefix="cswap-add."))
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp / "profile")}
    try:
        try:
            login = subprocess.run(
                [claude, "auth", "login"], env=env, timeout=LOGIN_TIMEOUT_S,
                stdin=None if interactive else subprocess.DEVNULL,
                capture_output=not interactive, text=True,
            )
        except subprocess.TimeoutExpired:
            raise ClaudeSwitchError("Login timed out; nothing was added.") from None
        if login.returncode != 0:
            raise ClaudeSwitchError("Login was not completed; nothing was added.")
        added = subprocess.run(
            [sys.executable, "-m", "claude_swap", "add"], env=env,
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        if added.returncode != 0:
            raise ClaudeSwitchError((added.stderr or added.stdout).strip() or "Could not add the account.")
        lines = [ln for ln in added.stdout.splitlines() if ln.startswith(("Added", "Updated", "Refreshed"))]
        return lines[0] if lines else added.stdout.strip()
    finally:
        _restore_if_switched(backup, email_before)
        shutil.rmtree(tmp, ignore_errors=True)
