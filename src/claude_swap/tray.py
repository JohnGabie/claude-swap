"""System-tray app (Windows and Linux) for claude-swap (``cswap tray``).

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
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError

_logger = logging.getLogger("claude-swap")

REFRESH_INTERVAL_S = 60
TOOLTIP_MAX = 127  # Windows truncates NOTIFYICONDATA.szTip at 127 chars
STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE = "cswap-tray"
MUTEX_NAME = "Local\\cswap-tray"
AUTOSTART_FILE = "cswap-tray.desktop"  # Linux: ~/.config/autostart
LOCK_FILE = "cswap-tray.lock"  # Linux single-instance lock, in the runtime dir
SUPPORTED = sys.platform in ("win32", "linux")
STARTUP_LABEL = "Start with Windows" if sys.platform == "win32" else "Start on login"


@dataclass
class TraySettings:
    """Tray preferences, persisted as JSON next to the other cswap settings.

    Auto-switch *policy* (threshold, cooldown, ...) stays in core settings
    (``claude_swap.settings``); only the on/off choices of this app live here.
    Notifications start off: the user opts in from the tray menu.
    """

    auto_switch: bool = False
    notifications: bool = False
    # Which events notify once notifications are on.
    notify_switch: bool = True
    notify_quarantine: bool = True
    notify_exhausted: bool = True
    notify_config: bool = True
    notify_errors: bool = True
    startup_initialized: bool = False  # start-with-Windows is registered once, then left alone
    # The screen-edge pill (Linux only, see claude_swap.pill).
    pill_enabled: bool = sys.platform == "linux"
    pill_fold: bool = True  # "Show on hover": the pill rests as a small sliver and opens when the pointer reaches the edge
    pill_edge: str = "right"
    pill_offset: int = 0  # px below centre on the left/right edges (negative: above)
    pill_theme: str = "black"
    pill_card_limits_open: bool = False  # the card section "Limites desta conta" (SL4), one state for all accounts

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
            if f.name in raw and isinstance(raw[f.name], type(getattr(defaults, f.name)))
        }
        settings = cls(**kwargs)
        from claude_swap.pill import normalize_edge, normalize_theme

        settings.pill_edge = normalize_edge(settings.pill_edge)
        settings.pill_theme = normalize_theme(settings.pill_theme)
        return settings

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# (settings field, event kinds it covers, label, help) for the per-event toggles.
NOTIFY_EVENTS = (
    ("notify_switch", ("switch",), "Account switched", "When auto-switch moves to another account"),
    ("notify_quarantine", ("account-quarantined", "account-unquarantined"),
     "Account needs re-login", "When an account is taken out of rotation, or comes back"),
    ("notify_exhausted", ("all-exhausted",), "All accounts at limit", "When there is no account left to switch to"),
    ("notify_config", ("config-warning",), "Setting ignored", "When an auto-switch setting is not applied"),
    ("notify_errors", ("error",), "Auto-switch stopped", "When the auto-switch engine fails"),
)


def muted_kinds(settings: TraySettings) -> set[str]:
    """Event kinds the user turned off."""
    return {k for field, kinds, *_ in NOTIFY_EVENTS if not getattr(settings, field) for k in kinds}


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


# ---- menu shape ------------------------------------------------------------

# Auto-switch strategies as the "Switch mode" submenu shows them.
SWITCH_MODES = (
    ("best", "Simple: most headroom"),
    ("balanced", "Balanced: even use of all accounts"),
    ("consume-first", "Consume first: soonest reset"),
    ("weekly-first", "Weekly first"),
    ("priority-consume-first", "Priority tiers"),
)
THRESHOLD_MIN, THRESHOLD_MAX = 50, 99


# The state of a row is written in its text. Themed check and radio marks
# can be invisible (seen with a GTK theme that draws them as a thin line).
def toggle_label(name: str, on: bool) -> str:
    return f"{name}: {'On' if on else 'Off'}"


def choice_label(label: str, selected: bool) -> str:
    return f"{'\u25cf' if selected else '\u25cb'}  {label}"


def mode_label(strategy: str) -> str:
    """Short name of a strategy for the 'Switch mode' row."""
    full = dict(SWITCH_MODES).get(strategy, strategy)
    return full.split(":")[0]


def threshold_label(value: float) -> str:
    return f"{value:g}%"


def clamp_threshold(value) -> int | None:
    """The slider value as a whole percent in range, or None for a bad value."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return int(max(THRESHOLD_MIN, min(THRESHOLD_MAX, round(number))))


THRESHOLD_PAGE = """<!doctype html><meta charset=utf-8><style>
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:#1b1b1f;color:#f2f0e3;font:14px system-ui,sans-serif;padding:20px 22px;display:flex;flex-direction:column;user-select:none}
h1{font-size:15px;font-weight:600;margin:0 0 4px}
p{margin:0;color:#a9a79c;font-size:12.5px;line-height:1.4}
#val{font-size:44px;font-weight:700;font-variant-numeric:tabular-nums;text-align:center;margin:14px 0 6px}
input[type=range]{width:100%;accent-color:#e0a800;outline:none}
.ends{display:flex;justify-content:space-between;color:#8d8b82;font-size:11px;margin-top:2px}
.row{display:flex;gap:10px;justify-content:flex-end;margin-top:auto}
button{font:inherit;padding:7px 16px;border-radius:8px;border:1px solid #3a3a41;background:#26262b;color:inherit;cursor:pointer}
button.ok{background:#e0a800;border-color:#e0a800;color:#1b1b1f;font-weight:600}
button:hover{filter:brightness(1.12)}
</style>
<h1>Switch threshold</h1>
<p>Auto-switch leaves the active account when its usage reaches this value.</p>
<div id=val>&ndash;</div>
<input id=s type=range min=__MIN__ max=__MAX__ step=1>
<div class=ends><span>__MIN__%</span><span>__MAX__%</span></div>
<div class=row><button id=cancel>Cancel</button><button id=ok class=ok>Confirm</button></div>
<script>
const s=document.getElementById('s'), val=document.getElementById('val');
const show=()=>{val.textContent=s.value+'%';};
s.addEventListener('input',show);
window.setValue=v=>{s.value=v;show();};
document.getElementById('cancel').onclick=()=>pywebview.api.cancel();
document.getElementById('ok').onclick=()=>pywebview.api.confirm(Number(s.value));
document.addEventListener('keydown',e=>{if(e.key==='Escape')pywebview.api.cancel();if(e.key==='Enter')pywebview.api.confirm(Number(s.value));});
window.addEventListener('pywebviewready',()=>pywebview.api.current().then(window.setValue));
</script>""".replace("__MIN__", str(THRESHOLD_MIN)).replace("__MAX__", str(THRESHOLD_MAX))


def show_account_rows(*, pill_available: bool, pill_enabled: bool) -> bool:
    """The menu lists the accounts only when no pill shows them."""
    return not (pill_available and pill_enabled)


# ---- pill square and card budget (fleet panel, FP3, FP10, FP12) ----------------

def pill_inputs(cards: list[dict], budget: dict, aliases: dict, history: dict | None, now: float):
    """What the refresh loop hands the pill: the cards with their ``budget`` field, and the square.

    A card's budget joins the budget account whose ``str(id)`` is the card's ``num``. Without a
    usage history yet, the square is a flat line.
    """
    from claude_swap import fleet_page, fleet_view

    history = history if isinstance(history, dict) else {}
    shown = fleet_page.with_budget(cards, budget, aliases, fleet_page.now_per_min(history, now), now)
    return shown, fleet_view.square(budget, history, now)


def refresh_pill(pill, cards: list[dict], budget_fn, aliases: dict, history, now: float, latest: dict,
                 general_lines: dict | None = None) -> None:
    """One refresh of the pill. A failure logs its exception type name only; the pill still gets the cards."""
    shown, square = cards, None
    try:
        latest["budget"] = budget_fn(now)
        shown, square = pill_inputs(cards, latest["budget"], aliases, history, now)
    except Exception as e:  # noqa: BLE001 - the refresh loop must go on
        _logger.warning("pill refresh failed: %s", type(e).__name__)
    pill.update(shown, fleet=square, general_lines=general_lines)


# ---- switch lines (SL3, SL4, SL6) -------------------------------------------------

LINE_KINDS = {"five": "threshold", "week": "weeklyThreshold"}  # the pill's kind -> the account record key
LINES_REFUSED = "invalid input"
LINES_FAILED = "the lines action failed"


def lines_inputs(cards: list[dict], policies_fn, core_fn) -> tuple[list[dict], dict | None]:
    """The cards with their ``lines`` field, and the effective general lines (SL6).

    A failure logs its exception type name only; the cards then go on without lines.
    """
    from claude_swap import lines_page

    try:
        core = core_fn()
        shown = lines_page.with_lines(cards, policies_fn(), core.threshold, core.weekly_threshold)
        return shown, lines_page.general_lines(core.threshold, core.weekly_threshold)
    except Exception as e:  # noqa: BLE001 - the refresh loop must go on
        _logger.warning("pill lines failed: %s", type(e).__name__)
        return cards, None


def _line_value_ok(value) -> bool:
    """None (clear), or an int or float in the account line range (NaN fails the range); a bool is refused."""
    from claude_swap.models import ACCOUNT_THRESHOLD_MAX, ACCOUNT_THRESHOLD_MIN

    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return ACCOUNT_THRESHOLD_MIN <= value <= ACCOUNT_THRESHOLD_MAX


class LinesActions:
    """``on_lines(action, args)`` of the pill (SL6): it checks each input, then runs the action.

    ``write_line`` is ``switcher.write_account_line``; ``pill_nums()`` gives the account numbers of the
    current pill; ``toggle_window`` opens or hides the lines window; ``on_change`` asks for a refresh.
    Each call returns ``{"ok": bool, "error": str | None}`` and never raises: a failure returns a fixed
    text and logs its exception type name only.
    """

    def __init__(self, write_line, settings: TraySettings, settings_path: Path, pill_nums, toggle_window, on_change):
        self._write_line = write_line
        self._settings = settings
        self._path = settings_path
        self._pill_nums = pill_nums
        self._toggle_window = toggle_window
        self._on_change = on_change

    def __call__(self, action, args) -> dict:
        try:
            if action == "toggle_window":
                self._toggle_window()
                return {"ok": True, "error": None}
            if not isinstance(args, dict):
                return {"ok": False, "error": LINES_REFUSED}
            if action == "set_account_line":
                return self._set_account_line(args)
            if action == "set_card_open":
                return self._set_card_open(args)
            return {"ok": False, "error": LINES_REFUSED}
        except Exception as e:  # noqa: BLE001 - the page gets a fixed text, the log the type only
            _logger.warning("lines action failed: %s", type(e).__name__)
            return {"ok": False, "error": LINES_FAILED}

    def _set_account_line(self, args: dict) -> dict:
        num, kind = args.get("num"), args.get("kind")
        if not (isinstance(num, str) and num.isascii() and num.isdigit() and num in self._pill_nums()):
            return {"ok": False, "error": LINES_REFUSED}
        if not isinstance(kind, str) or kind not in LINE_KINDS or "value" not in args or not _line_value_ok(args["value"]):
            return {"ok": False, "error": LINES_REFUSED}
        res = self._write_line(num, LINE_KINDS[kind], args["value"])
        if res.get("ok") is True:
            self._on_change()
        return {"ok": res.get("ok") is True, "error": res.get("error")}

    def _set_card_open(self, args: dict) -> dict:
        value = args.get("open")
        if not isinstance(value, bool):
            return {"ok": False, "error": LINES_REFUSED}
        self._settings.pill_card_limits_open = value
        self._settings.save(self._path)
        return {"ok": True, "error": None}


# ---- start with Windows ----------------------------------------------------

def startup_command(python: str | None = None) -> str:
    """Command line the Run key launches: windowless Python running this module."""
    exe = Path(python or sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return f'"{windowless if windowless.is_file() else exe}" -m claude_swap.tray'


def _autostart_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / AUTOSTART_FILE


def autostart_entry(python: str | None = None) -> str:
    """Linux autostart .desktop file launching this module."""
    return (
        "[Desktop Entry]\nType=Application\nName=cswap tray\n"
        f"Exec=\"{python or sys.executable}\" -m claude_swap.tray\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def is_startup_enabled() -> bool:
    if sys.platform == "linux":
        return _autostart_path().is_file()
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
    """Add or remove this app's own startup entry (never touches other entries)."""
    if sys.platform == "linux":
        path = _autostart_path()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(autostart_entry(), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        return
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
    """Return a handle if this is the only tray, else None (keep it referenced)."""
    if sys.platform == "linux":
        import fcntl

        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        lock = open(runtime / LOCK_FILE, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            return None
        return lock  # the flock lives as long as this file object

    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


# ---- the app ---------------------------------------------------------------

def run(switcher) -> int:
    """Run the tray app until Quit; returns 0, or 1 if a dependency is missing."""
    if not SUPPORTED:
        print("cswap tray is only available on Windows and Linux.", file=sys.stderr)
        return 1
    if sys.platform == "linux":
        # Wayland can't pin windows to an edge; XWayland can (the pill needs that).
        os.environ.setdefault("GDK_BACKEND", "x11")
        # WebKit's DMABUF renderer draws nothing on some GPU drivers (NVIDIA:
        # "Failed to create GBM buffer", blank windows, invisible pill).
        # Shared-memory buffers work everywhere.
        os.environ.setdefault("WEBKIT_DMABUF_RENDERER_FORCE_SHM", "1")
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
    notifier.muted = muted_kinds(settings)
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

    def aliases() -> dict:
        """The switcher's alias of each account (the email of the same rows is dropped here)."""
        try:
            return {num: alias for num, alias, _email in switcher.list_aliases()}
        except Exception:  # noqa: BLE001 - a card without alias is fine
            return {}

    def refresh_loop():
        while not quitting.is_set():
            snap = api.snapshot()
            if "accounts" in snap:
                cards[:] = snap["accounts"]
                if pill is not None:
                    from claude_swap.budget_source import current_budget

                    shown, general = lines_inputs(cards, switcher.account_policies, core_settings)
                    refresh_pill(pill, shown, current_budget, aliases(), history.data(), time.time(), latest,
                                 general_lines=general)
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
        icon.update_menu()  # greys or restores 'Switch balanced'

    def set_notifications(on):
        settings.notifications = on
        settings.save(settings_path)
        api.notifier = notifier if on else None

    def toggle_notifications():
        set_notifications(not settings.notifications)
        icon.update_menu()

    class AppOptions:
        """The tray's own options, shown in the window's Settings dialog."""

        def get(self):
            rows = [
                {"key": "notifications", "label": "Notifications", "group": "Notifications",
                 "help": "Desktop notifications for the events below", "value": settings.notifications},
            ]
            rows += [
                {"key": field, "label": label, "group": "Notifications", "help": help_, "value": getattr(settings, field)}
                for field, _kinds, label, help_ in NOTIFY_EVENTS
            ]
            rows.append({"key": "startup", "label": STARTUP_LABEL, "group": "App",
                         "help": "Launch the tray when you sign in", "value": is_startup_enabled()})
            return rows

        def set(self, key, value):
            if key == "notifications":
                set_notifications(value)
            elif key == "startup":
                set_startup(value)
            elif key in {f for f, *_ in NOTIFY_EVENTS}:
                setattr(settings, key, value)
                settings.save(settings_path)
                notifier.muted = muted_kinds(settings)
            icon.update_menu()

    api.app_options = AppOptions()

    pill = None
    history = None  # the usage history (FP11), Linux only
    latest: dict = {}  # the last budget of the refresh loop, for the panel
    panel_window = None
    panel_open = threading.Event()
    lines_window = None  # the lines window (SL3), opened from the pill gear
    if sys.platform == "linux":
        from claude_swap import fleet_page, lines_page
        from claude_swap.desktop_notify import send_notification
        from claude_swap.pill import EDGES, THEMES, PillController
        from claude_swap.usage_log import claude_projects_dir

        def say(title, body):
            threading.Thread(target=send_notification, args=(title, body), daemon=True).start()

        root = Path(switcher.backup_dir)
        history = fleet_page.FleetHistory(root, claude_projects_dir())
        decisions = fleet_page.DecisionCache(root)  # parsed once per log file, reused on a period change

        def panel_budget():
            if latest.get("budget") is None:
                from claude_swap.budget_source import current_budget

                latest["budget"] = current_budget()
            return latest["budget"]

        # Like the threshold window: driven through GTK, not through pywebview's show/hide.
        def panel_gtk(fn):
            from gi.repository import GLib
            from webview.platforms import gtk as wgtk

            def run():
                view = wgtk.BrowserView.instances.get(panel_window.uid)
                if view is not None:
                    fn(view)
                return False

            GLib.idle_add(run)

        def panel_js(script):
            panel_gtk(lambda view: view.webview.run_javascript(script, None, None, None))

        def close_panel():
            panel_open.clear()
            panel_gtk(lambda view: view.window.hide())

        def open_panel():
            x, y = fleet_page.panel_position(pill.panel_anchor())
            panel_open.set()

            def show(view):
                view.window.show_all()
                view.window.deiconify()
                view.window.present()  # the keyboard focus, so Esc closes it
                view.window.move(x, y)  # after mapping, which can recentre the window
                view.webview.run_javascript("window.fleetOpen&&window.fleetOpen()", None, None, None)

            panel_gtk(show)

            def update_then_reload():  # FP11: an open also updates the usage history
                history.refresh()
                if panel_open.is_set():
                    panel_js("window.fleetReload&&window.fleetReload()")

            threading.Thread(target=update_then_reload, daemon=True).start()

        def toggle_panel():
            """The pill square: the first click opens the panel, the second closes it."""
            close_panel() if panel_open.is_set() else open_panel()

        panel_window = webview.create_window(
            fleet_page.PANEL_TITLE, html=fleet_page.FLEET_PAGE,
            js_api=fleet_page.FleetPanelApi(history, panel_budget, decisions.events, close_panel),
            width=fleet_page.PANEL_W, height=fleet_page.PANEL_H, resizable=False,
            background_color="#000000", hidden=True,
        )

        def on_panel_closing():
            if quitting.is_set():
                return None
            close_panel()
            return False

        panel_window.events.closing += on_panel_closing

        # The lines window (SL3): driven through GTK like the fleet panel, placed beside the pill gear.
        def lines_gtk(fn):
            from gi.repository import GLib
            from webview.platforms import gtk as wgtk

            def run():
                view = wgtk.BrowserView.instances.get(lines_window.uid)
                if view is not None:
                    fn(view)
                return False

            GLib.idle_add(run)

        lines_ui = lines_page.LinesWindow(lines_gtk, lambda: pill.gear_anchor())
        lines_window = webview.create_window(
            lines_page.LINES_TITLE, html=lines_page.LINES_PAGE,
            js_api=lines_page.LinesWindowApi(
                lambda: core_settings(), switcher.account_policies, lambda: list(cards),
                lambda: settings.auto_switch, api.save_settings, lines_ui.close, wake.set),
            width=lines_page.LINES_W, height=lines_page.LINES_H, resizable=False,
            background_color="#000000", hidden=True,
        )

        def on_lines_closing():
            if quitting.is_set():
                return None
            lines_ui.close()
            return False

        lines_window.events.closing += on_lines_closing

        on_lines = LinesActions(switcher.write_account_line, settings, settings_path,
                                lambda: [c.get("num") for c in pill.cards], lines_ui.toggle, wake.set)
        pill = PillController(webview, api, settings, wake.set, say, on_fleet=toggle_panel, on_lines=on_lines)
        pill.create()

    def set_pill_option(field, value):
        setattr(settings, field, value)
        settings.save(settings_path)
        if field == "pill_enabled":
            pill.set_visible(value)
        elif settings.pill_enabled:
            pill.relayout(reveal=True)  # show where it went, also with "Show on hover"
        icon.update_menu()

    def pill_option(field, value):
        """A zero-argument menu action (pystray decides how to call it by argument count)."""
        return lambda: set_pill_option(field, value)

    def add_account():
        threading.Thread(target=pill.add_account if pill else api.add_login, daemon=True).start()

    def quit_app():
        quitting.set()
        wake.set()
        api.shutdown()
        icon.stop()
        if pill is not None:
            pill.destroy()
        threshold_window.destroy()
        if panel_window is not None:
            panel_window.destroy()
        if lines_window is not None:
            lines_window.destroy()
        window.destroy()

    def core_settings():
        from claude_swap.settings import load_settings

        return load_settings(Path(switcher.backup_dir))

    def save_core(key, value):
        """Write one core setting off the menu thread; a running engine restarts."""
        def action():
            res = api.save_settings({key: value}, {})
            if not res["ok"] and res["output"]:
                icon.notify(res["output"], "cswap")
            icon.update_menu()
        threading.Thread(target=action, daemon=True).start()

    def set_mode(strategy):
        return lambda: save_core("autoswitch.strategy", strategy)

    # The threshold window is driven through GTK directly, as the pill is:
    # pywebview's show/hide wrappers wait on events that a window created
    # hidden never fires, so a second open could hang.
    def threshold_gtk(fn):
        from gi.repository import GLib
        from webview.platforms import gtk as wgtk

        def run():
            view = wgtk.BrowserView.instances.get(threshold_window.uid)
            if view is not None:
                fn(view)
            return False

        GLib.idle_add(run)

    def hide_threshold():
        threshold_gtk(lambda view: view.window.hide())

    class ThresholdDialog:
        """What the threshold window calls through ``pywebview.api``."""

        def current(self):
            return clamp_threshold(core_settings().threshold)

        def confirm(self, value):
            number = clamp_threshold(value)
            hide_threshold()
            if number is not None:
                save_core("autoswitch.threshold", number)

        def cancel(self):
            hide_threshold()

    threshold_window = webview.create_window(
        "cswap threshold", html=THRESHOLD_PAGE, js_api=ThresholdDialog(), width=380, height=290,
        resizable=False, background_color=BG, hidden=True,
    )

    def on_threshold_closing():
        if quitting.is_set():
            return None
        hide_threshold()
        return False

    threshold_window.events.closing += on_threshold_closing

    def open_threshold():
        value = clamp_threshold(core_settings().threshold)

        def show(view):
            view.webview.run_javascript("window.setValue&&window.setValue(%d)" % value, None, None, None)
            view.window.show_all()
            view.window.deiconify()
            view.window.present()

        threshold_gtk(show)

    def build_menu():
        accounts = ()
        if show_account_rows(pill_available=pill is not None, pill_enabled=settings.pill_enabled):
            accounts = [
                pystray.MenuItem(choice_label(account_label(card), bool(card["active"])), switch_to(card["num"]))
                for card in cards
            ] or [pystray.MenuItem("No managed accounts", None, enabled=False)]
        core = core_settings()
        auto_on = lambda _i: settings.auto_switch  # noqa: E731 - mode and threshold belong to auto-switch
        mode_menu = pystray.MenuItem(
            f"Switch mode: {mode_label(core.strategy)}",
            pystray.Menu(*(
                pystray.MenuItem(choice_label(label, core.strategy == name), set_mode(name))
                for name, label in SWITCH_MODES
            )),
            enabled=auto_on,
        )
        threshold_item = pystray.MenuItem(
            f"Switch threshold: {threshold_label(core.threshold)}\u2026", open_threshold, enabled=auto_on)
        pill_items = ()
        if pill is not None:
            pill_items = (
                pystray.MenuItem("Pill", pystray.Menu(
                    pystray.MenuItem(toggle_label("Show pill", settings.pill_enabled),
                                     lambda: set_pill_option("pill_enabled", not settings.pill_enabled)),
                    pystray.MenuItem(toggle_label("Show only on hover", settings.pill_fold),
                                     lambda: set_pill_option("pill_fold", not settings.pill_fold)),
                    pystray.Menu.SEPARATOR,
                    *(pystray.MenuItem(choice_label(f"{e.capitalize()} edge", settings.pill_edge == e),
                                       pill_option("pill_edge", e)) for e in EDGES),
                    pystray.Menu.SEPARATOR,
                    *(pystray.MenuItem(choice_label(f"{t.capitalize()} theme", settings.pill_theme == t),
                                       pill_option("pill_theme", t)) for t in THEMES),
                )),
            )
        return (
            pystray.MenuItem("Open cswap", open_window, default=True),
            pystray.Menu.SEPARATOR,
            *accounts,
            pystray.MenuItem("Add account (browser login)", add_account),
            pystray.MenuItem("Switch to best", switch_best),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(toggle_label("Auto-switch", settings.auto_switch), toggle_auto),
            mode_menu,
            threshold_item,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(toggle_label("Notifications", settings.notifications), toggle_notifications),
            *pill_items,
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
        if history is not None:  # FP11: built at start, then updated every 5 minutes
            threading.Thread(target=history.run, args=(quitting,), daemon=True).start()
        threading.Thread(target=refresh_loop, daemon=True).start()
        if pill is not None:
            threading.Thread(target=pill.start, daemon=True).start()

    window_icon = ico if ico.is_file() else None
    if window_icon and sys.platform == "linux":
        # GTK can't load .ico files: hand the window a PNG copy.
        png = Path(switcher.backup_dir) / "cswap.png"
        try:
            image.save(png)
            window_icon = png
        except (OSError, ValueError):
            window_icon = None
    webview.start(on_ready, icon=str(window_icon) if window_icon else None)
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
            print(f"cswap tray will start on {'Windows startup' if sys.platform == 'win32' else 'login'}.")
            return 0
        if "--uninstall-startup" in argv:
            set_startup(False)
            print("cswap tray will no longer start automatically.")
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
