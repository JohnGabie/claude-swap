"""Windows system-tray app for claude-swap (``cswap tray``).

The tray icon is the app; the desktop window (``claude_swap.desktop``) is opened
from it and hides back to the tray when closed. Like the window and the macOS
menu bar, this is a thin shell over ``ClaudeAccountSwitcher`` and the core
auto-switch engine: all account, usage and engine logic is reached through
``desktop.DesktopApi`` so nothing is duplicated.

The pure helpers (settings, labels, tooltip, start-with-Windows registry value)
are import-safe without pystray/pywebview so they can be unit-tested headless;
both are imported lazily inside ``run``.
"""

from __future__ import annotations

import io
import json
import sys
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError

REFRESH_INTERVAL_S = 60
TOOLTIP_MAX = 127  # Windows truncates NOTIFYICONDATA.szTip at 127 chars
STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE = "cswap-tray"
MUTEX_NAME = "Local\\cswap-tray"


@dataclass
class TraySettings:
    """Tray preferences, persisted as JSON next to the other cswap settings.

    Auto-switch *policy* (threshold, cooldown, ...) stays in core settings
    (``claude_swap.settings``); only the on/off choices of this app live here.
    Notifications start off: the user opts in from the tray menu.
    """

    auto_switch: bool = False
    notifications: bool = False
    startup_initialized: bool = False  # start-with-Windows is registered once, then left alone

    @classmethod
    def load(cls, path: Path) -> "TraySettings":
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {
            f.name: raw[f.name]
            for f in fields(cls)
            if f.name in raw and isinstance(raw[f.name], bool)
        }
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# ---- labels (operate on ``desktop.card_dict`` output) ----------------------

def _usage_text(card: dict) -> str:
    parts = [
        f"{row['label']} {row['pct']:.0f}%"
        for row in card.get("rows") or ()
        if isinstance(row.get("pct"), (int, float))
    ]
    return " · ".join(parts) or card.get("message") or "no usage yet"


def account_label(card: dict) -> str:
    """One account's menu row: title, then its usage."""
    suffix = "  (disabled)" if card.get("disabled") else ""
    return f"{card['title']}{suffix}  {_usage_text(card)}"


def tooltip(cards: list[dict]) -> str:
    """Hover text of the tray icon: the active account and its usage."""
    active = next((c for c in cards if c.get("active")), None)
    if active is None:
        return "cswap"
    return f"cswap — {active['title']}  {_usage_text(active)}"[:TOOLTIP_MAX]


# ---- start with Windows ----------------------------------------------------

def startup_command(python: str | None = None) -> str:
    """Command line the Run key launches: windowless Python running this module."""
    exe = Path(python or sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return f'"{windowless if windowless.is_file() else exe}" -m claude_swap.tray'


def is_startup_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
            winreg.QueryValueEx(key, STARTUP_VALUE)
        return True
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    """Add or remove this app's own Run-key value (never touches other entries)."""
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_VALUE, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_VALUE)
            except FileNotFoundError:
                pass


def _acquire_single_instance():
    """Return a mutex handle if this is the only tray, else None (keep it referenced)."""
    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


# ---- the app ---------------------------------------------------------------

def run(switcher) -> int:
    """Run the tray app until Quit; returns 0, or 1 if a dependency is missing."""
    if sys.platform != "win32":
        print("cswap tray is only available on Windows.", file=sys.stderr)
        return 1
    try:
        import pystray
        import webview
        from PIL import Image
    except ImportError:
        print("cswap tray needs pystray and pywebview: pip install 'claude-swap[tray]'", file=sys.stderr)
        return 1
    from claude_swap.desktop import BG, DesktopApi, TITLE, _make_notifier, tint_title_bar
    from claude_swap.desktop_page import PAGE

    mutex = _acquire_single_instance()
    if mutex is None:
        return 0  # already running: its icon is the one to use

    settings_path = Path(switcher.backup_dir) / "tray_settings.json"
    settings = TraySettings.load(settings_path)
    if not settings.startup_initialized:
        try:
            set_startup(True)
        except OSError:
            pass
        settings.startup_initialized = True
        settings.save(settings_path)

    api = DesktopApi(switcher)
    notifier = _make_notifier(switcher)
    api.notifier = notifier if settings.notifications else None

    window = webview.create_window(
        TITLE, html=PAGE, js_api=api, width=560, height=720, min_size=(440, 420),
        background_color=BG, hidden=True,
    )

    def pick_file(kind):
        types = ("cswap export (*.cswap;*.json)", "All files (*.*)")
        if kind == "save":
            got = window.create_file_dialog(
                webview.FileDialog.SAVE, save_filename="accounts.cswap", file_types=types)
        else:
            got = window.create_file_dialog(webview.FileDialog.OPEN, file_types=types)
        if not got:
            return None
        return got if isinstance(got, str) else got[0]

    api.pick_file = pick_file
    quitting = threading.Event()

    def on_closing():
        if quitting.is_set():
            return None
        window.hide()  # closing the window leaves the app in the tray
        return False

    window.events.closing += on_closing

    def open_window():
        window.show()
        window.restore()
        tint_title_bar(TITLE)

    # ---- display state, refreshed in the background ----------------------
    cards: list[dict] = []
    wake = threading.Event()

    def refresh_loop():
        while not quitting.is_set():
            snap = api.snapshot()
            if "accounts" in snap:
                cards[:] = snap["accounts"]
                icon.title = tooltip(cards)
                icon.update_menu()
            wake.wait(REFRESH_INTERVAL_S)
            wake.clear()

    def switch_to(num):
        def action():
            res = api.switch(num)
            if not res["ok"] and res["output"]:
                icon.notify(res["output"], "cswap")
            wake.set()
        return lambda: threading.Thread(target=action, daemon=True).start()

    def switch_best():
        def action():
            res = api.best()
            if not res["ok"] and res["output"]:
                icon.notify(res["output"], "cswap")
            wake.set()
        threading.Thread(target=action, daemon=True).start()

    def toggle_auto():
        settings.auto_switch = not settings.auto_switch
        settings.save(settings_path)
        api.auto(settings.auto_switch)

    def toggle_notifications():
        settings.notifications = not settings.notifications
        settings.save(settings_path)
        api.notifier = notifier if settings.notifications else None

    def quit_app():
        quitting.set()
        wake.set()
        api.shutdown()
        icon.stop()
        window.destroy()

    def build_menu():
        accounts = [
            pystray.MenuItem(
                account_label(card), switch_to(card["num"]),
                checked=lambda _item, c=card: c["active"], radio=True,
            )
            for card in cards
        ] or [pystray.MenuItem("No managed accounts", None, enabled=False)]
        return (
            pystray.MenuItem("Open cswap", open_window, default=True),
            pystray.Menu.SEPARATOR,
            *accounts,
            pystray.MenuItem("Switch to best", switch_best),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Auto-switch", toggle_auto, checked=lambda _i: settings.auto_switch),
            pystray.MenuItem("Notifications", toggle_notifications, checked=lambda _i: settings.notifications),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        )

    ico = Path(__file__).with_name("cswap.ico")
    image = Image.open(ico) if ico.is_file() else Image.new("RGB", (64, 64), BG)
    icon = pystray.Icon("cswap", image, "cswap", pystray.Menu(build_menu))

    def on_ready():
        if settings.auto_switch:
            api.auto(True)
        icon.run_detached()
        threading.Thread(target=refresh_loop, daemon=True).start()

    webview.start(on_ready, icon=str(ico) if ico.is_file() else None)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # pythonw has no console: sys.stdout/stderr are None.
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()
    from claude_swap.switcher import ClaudeAccountSwitcher

    try:
        if "--install-startup" in argv:
            set_startup(True)
            print("cswap tray will start with Windows.")
            return 0
        if "--uninstall-startup" in argv:
            set_startup(False)
            print("cswap tray will no longer start with Windows.")
            return 0
        return run(ClaudeAccountSwitcher())
    except ClaudeSwitchError as e:
        _show_error(str(e))
    except Exception as e:  # windowless: surface everything
        _show_error(f"{type(e).__name__}: {e}")
    return 1


def _show_error(text: str) -> None:
    from claude_swap.desktop import _show_error as show

    show(text)


if __name__ == "__main__":
    sys.exit(main())
