"""Desktop window for claude-swap (``cswap-desktop``).

A thin GUI shell over ``ClaudeAccountSwitcher`` and the core auto-switch engine
(``claude_swap.autoswitch``) — it never re-implements account, usage, or
auto-switch logic. Usage for display comes from ``switcher.accounts_snapshot()``
and is formatted with the TUI's shared helpers (``claude_swap.tui.data``).

Built on ``tkinter`` (stdlib). The pure helpers below (card and event-line
formatting) are import-safe without tkinter so they can be unit-tested headless;
``tkinter`` is imported lazily inside the app glue.
"""

from __future__ import annotations

import io
import queue
import re
import sys
import threading
from dataclasses import dataclass

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import AccountSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.data import (
    clock_stamp,
    format_age,
    sentinel_label,
    window_pct,
    window_reset_text,
)

_TITLE_MAX = 32
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WINDOWS = (("5h", "five_hour"), ("7d", "seven_day"))

ALERT_KINDS = frozenset(
    {"all-exhausted", "account-quarantined", "config-warning", "error"}
)


@dataclass(frozen=True)
class WindowRow:
    label: str
    pct: float | None
    reset: str | None
    age: str | None


@dataclass(frozen=True)
class CardView:
    title: str
    tag: str
    active: bool
    disabled: bool
    switchable: bool
    rows: tuple[WindowRow, ...]
    message: str | None


def card_lines(acc: AccountSnapshot, now: float) -> CardView:
    """Everything one account card shows, as plain data."""
    title = f"#{acc.number}  {acc.alias or acc.email}"
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1] + "…"
    rows: tuple[WindowRow, ...] = ()
    message = None
    usage = acc.usage
    if usage.sentinel:
        message = sentinel_label(usage.sentinel)
    elif not usage.last_good:
        message = "no usage yet"
    else:
        age = format_age(usage.age_s)
        rows = tuple(
            WindowRow(
                label,
                window_pct(usage.last_good, key),
                window_reset_text(usage.last_good, key, now),
                age,
            )
            for label, key in _WINDOWS
        )
    return CardView(
        title=title,
        tag=acc.display_tag,
        active=acc.is_active,
        disabled=acc.disabled,
        switchable=acc.switchable,
        rows=rows,
        message=message,
    )


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def event_line(ev) -> tuple[str, bool]:
    """Event-log text and whether it deserves alert styling."""
    return f"{clock_stamp()} {ev.human()}", ev.kind in ALERT_KINDS


# Zen-browser-style warm dark palette.
BG, CARD, CARD_HOVER, INSET = "#1b1b1f", "#26252b", "#2e2d34", "#141417"
FG, MUTED, ACCENT, DANGER = "#f2f0e3", "#9b98a3", "#f76f53", "#e5484d"
TRACK, GHOST, GHOST_HOVER = "#34333a", "#34333a", "#403f47"
PRIMARY_HOVER, CHIP = "#ffffff", "#3a2a26"


def rounded_points(x1: float, y1: float, x2: float, y2: float, r: float) -> list[float]:
    """Polygon points for a rounded rectangle (draw with ``smooth=True``)."""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2,
        x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1,
    ]


def colorref(hex_color: str) -> int:
    """'#rrggbb' -> Win32 COLORREF (0x00bbggrr)."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return b << 16 | g << 8 | r


def tint_title_bar(root) -> None:
    """Paint the Windows title bar to match the body (no-op elsewhere)."""
    if sys.platform != "win32":
        return
    import ctypes

    root.update_idletasks()  # the frame HWND exists only once Tk has mapped it
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
    on = ctypes.c_int(1)
    dwm(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 fallback)
    for attr, color in ((35, BG), (36, FG)):  # DWMWA_CAPTION_COLOR / TEXT_COLOR, Win11 only
        ref = ctypes.c_int(colorref(color))
        dwm(hwnd, attr, ctypes.byref(ref), ctypes.sizeof(ref))


class RefreshGate:
    """No-overlap rule for refreshes, remembering one skipped request."""

    def __init__(self):
        self._inflight = False
        self._pending = False

    def request(self) -> bool:
        """True if the caller should start a refresh now."""
        if self._inflight:
            self._pending = True
            return False
        self._inflight = True
        return True

    def done(self) -> bool:
        """Mark the refresh finished; True if one was requested meanwhile."""
        self._inflight = False
        pending, self._pending = self._pending, False
        return pending


class _EngineCrash:
    """Minimal event stand-in so a crashed engine thread shows up in the log."""

    kind = "error"

    def __init__(self, msg: str):
        self._msg = msg

    def human(self) -> str:
        return f"auto-switch engine crashed: {self._msg}"


def run(switcher) -> int:
    """Open the window; returns 0 when it closes, 1 if tkinter is missing."""
    try:
        import tkinter as tk
        from tkinter import messagebox, simpledialog
    except ImportError:
        print("cswap-desktop needs tkinter (install your Python's tk package).", file=sys.stderr)
        return 1

    from claude_swap.autoswitch import AutoSwitchEngine
    from claude_swap.settings import load_settings
    from claude_swap.snapshot_source import SnapshotSource
    from claude_swap.tui.data import run_action
    from tkinter import font as tkfont

    fonts: dict = {}  # filled once a Tk root exists

    def make_fonts():
        have = set(tkfont.families())

        def pick(*names):
            return next((n for n in names if n in have), "TkDefaultFont")

        ui, mono = pick("Segoe UI", "SF Pro Text", "Helvetica Neue", "DejaVu Sans"), pick(
            "Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono")
        fonts.update(
            ui=tkfont.Font(family=ui, size=9),
            small=tkfont.Font(family=ui, size=8),
            name=tkfont.Font(family=ui, size=11, weight="bold"),
            title=tkfont.Font(family=pick("Georgia", "Times New Roman"), size=16),
            mono=tkfont.Font(family=mono, size=9),
        )

    def recolor(widget, old, new):
        """Swap a background colour through a widget subtree (card hover)."""
        try:
            if widget.cget("bg") == old:
                widget.configure(bg=new)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            recolor(child, old, new)

    def round_box(parent, fill, radius=14, pad=16):
        """A rounded, borderless box: (outer canvas, inner frame to fill).

        Tk has no rounded corners, so the shape is a smoothed polygon on a
        canvas whose height follows the inner frame.
        """
        c = tk.Canvas(parent, bg=parent.cget("bg"), highlightthickness=0, height=1)
        inner = tk.Frame(c, bg=fill)
        win = c.create_window(pad, pad, window=inner, anchor="nw")

        def redraw(_e=None):
            w, h = c.winfo_width(), c.winfo_height()
            c.delete("bg")
            c.create_polygon(rounded_points(0, 0, w, h, radius), smooth=True,
                             fill=inner.cget("bg"), outline="", tags="bg")
            c.tag_lower("bg")
            c.itemconfigure(win, width=max(1, w - 2 * pad))

        inner.bind("<Configure>", lambda e: c.configure(height=e.height + 2 * pad), add="+")
        c.bind("<Configure>", redraw, add="+")

        def fit():
            # An unmapped inner frame never gets <Configure>; size from its request.
            inner.update_idletasks()
            c.configure(height=inner.winfo_reqheight() + 2 * pad)

        c.fit = fit
        return c, inner

    class Pill(tk.Canvas):
        """Fully rounded button (or static chip when ``command`` is None)."""

        STYLES = {  # fill, hover fill, text
            "primary": (FG, PRIMARY_HOVER, BG),
            "ghost": (GHOST, GHOST_HOVER, FG),
            "chip": (CHIP, CHIP, ACCENT),
        }

        def __init__(self, parent, text, command=None, kind="ghost", menu=None, font=None):
            self.font = font or fonts["ui"]
            h = self.font.metrics("linespace") + 12
            w = max(h, self.font.measure(text) + 26)  # never narrower than a circle
            super().__init__(parent, width=w, height=h, bg=parent.cget("bg"),
                             highlightthickness=0, cursor="hand2" if (command or menu) else "")
            self.fill, self.hover, self.ink = self.STYLES[kind]
            self.command, self.menu, self.enabled = command, menu, True
            self.shape = self.create_polygon(rounded_points(0, 0, w, h, h / 2), smooth=True,
                                             fill=self.fill, outline="")
            self.label = self.create_text(w / 2, h / 2, text=text, fill=self.ink, font=self.font)
            if command or menu:
                self.bind("<Enter>", lambda _e: self._paint(self.hover))
                self.bind("<Leave>", lambda _e: self._paint(self.fill))
                self.bind("<ButtonRelease-1>", self._click)

        def _paint(self, fill):
            if self.enabled:
                self.itemconfigure(self.shape, fill=fill)

        def _click(self, _e):
            if not self.enabled:
                return
            if self.menu is not None:
                self.menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + 4)
            elif self.command:
                self.command()

        def set_text(self, text):
            self.itemconfigure(self.label, text=text)

        def set_enabled(self, on):
            self.enabled = on
            self.itemconfigure(self.label, fill=self.ink if on else MUTED)
            self.itemconfigure(self.shape, fill=self.fill)

    class Toggle(tk.Canvas):
        """Sliding on/off switch bound to a BooleanVar."""

        def __init__(self, parent, var, command):
            super().__init__(parent, width=36, height=20, bg=parent.cget("bg"),
                             highlightthickness=0, cursor="hand2")
            self.var, self.command = var, command
            self.bind("<ButtonRelease-1>", self._click)
            var.trace_add("write", lambda *_a: self._draw())
            self._draw()

        def _click(self, _e):
            self.var.set(not self.var.get())
            self.command()

        def _draw(self):
            on = self.var.get()
            self.delete("all")
            self.create_polygon(rounded_points(0, 0, 36, 20, 10), smooth=True,
                                fill=ACCENT if on else GHOST, outline="")
            x = 18 if on else 2
            self.create_oval(x, 2, x + 16, 18, fill=FG if on else MUTED, outline="")

    class DesktopApp:
        def __init__(self):
            self.switcher = switcher
            self.source = SnapshotSource(switcher)
            self._q: queue.Queue = queue.Queue()
            self._events: queue.Queue = queue.Queue()
            self._engine = None
            self._busy = False
            self._gate = RefreshGate()
            self._closing = False
            self._engine_thread = None
            self._buttons: list = []

            r = self.root = tk.Tk()
            r.title("cswap")
            r.geometry("540x680")
            r.minsize(440, 380)
            r.configure(bg=BG)
            try:
                tint_title_bar(r)
            except Exception:  # cosmetic only; never block the window
                pass
            r.protocol("WM_DELETE_WINDOW", self._close)
            make_fonts()

            head = tk.Frame(r, bg=BG)
            head.pack(fill="x", padx=16, pady=(14, 10))
            tk.Label(head, text="cswap", bg=BG, fg=FG, font=fonts["title"]).pack(side="left")
            self._btn(head, "⟳", self.refresh).pack(side="right")
            self._btn(head, "Best", lambda: self._act(lambda: switcher.switch(strategy="best")),
                      kind="primary").pack(side="right", padx=6)
            m = tk.Menu(r, tearoff=0)
            m.add_command(label="Current login", command=self._add_current)
            m.add_command(label="From token…", command=self._add_token)
            self._btn(head, "+ Add", None, menu=m).pack(side="right")
            self.auto_var = tk.BooleanVar(value=False)
            Toggle(head, self.auto_var, self._toggle_auto).pack(side="right", padx=(6, 14))
            tk.Label(head, text="Auto", bg=BG, fg=MUTED, font=fonts["ui"]).pack(side="right")

            # Output panel is packed first (bottom) so the card area takes the rest.
            self.panel = tk.Frame(r, bg=BG)
            self.panel.pack(side="bottom", fill="x", padx=16, pady=(6, 14))
            self.out_toggle = Pill(self.panel, "Output  ⌃", self._toggle_panel, font=fonts["small"])
            self.out_toggle.pack(anchor="w")
            self.out_box, inner = round_box(self.panel, INSET, radius=12, pad=10)
            self.out = tk.Text(
                inner, height=9, bg=INSET, fg=FG, relief="flat", bd=0, state="disabled",
                font=fonts["mono"], wrap="word", highlightthickness=0, insertbackground=FG,
            )
            self.out.pack(fill="both")
            self.out.tag_configure("alert", foreground=DANGER)
            self.out_box.fit()
            self._panel_open = False

            self.canvas = tk.Canvas(r, bg=BG, highlightthickness=0)
            self.cards = tk.Frame(self.canvas, bg=BG)
            win = self.canvas.create_window((0, 0), window=self.cards, anchor="nw")
            self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(win, width=e.width))
            self.cards.bind(
                "<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            )
            self.canvas.bind_all(
                "<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
            )
            self.canvas.pack(fill="both", expand=True, padx=16)

            r.after(100, self._pump)
            r.after(500, self._drain_events)
            self._tick()

        # ---- widgets -------------------------------------------------------
        def _btn(self, parent, text, cmd, menu=None, kind="ghost"):
            b = Pill(parent, text, cmd, kind=kind, menu=menu)
            self._buttons.append(b)
            return b

        def _set_busy(self, busy):
            self._busy = busy
            for b in self._buttons:
                try:
                    b.set_enabled(not busy)
                except tk.TclError:
                    pass  # stale card button

        def _toggle_panel(self, show=None):
            show = (not self._panel_open) if show is None else show
            if show == self._panel_open:
                return
            self._panel_open = show
            self.out_toggle.set_text("Output  ⌄" if show else "Output  ⌃")
            if show:
                self.out_box.pack(fill="x", pady=(8, 0))
            else:
                self.out_box.pack_forget()

        def log(self, text, alert=False, stamp=True):
            lines = [ln for ln in strip_ansi(text).splitlines() if ln.strip()]
            if not lines:
                return
            self.out.configure(state="normal")
            for ln in lines:
                prefix = f"{clock_stamp()} " if stamp else ""
                self.out.insert("end", f"{prefix}{ln}\n", "alert" if alert else "")
            self.out.see("end")
            self.out.configure(state="disabled")
            if alert:
                self._toggle_panel(True)

        # ---- threading -----------------------------------------------------
        def _bg(self, fn, on_done):
            def work():
                try:
                    res = fn()
                except Exception as e:  # surfaced on the Tk thread
                    res = e
                self._q.put((on_done, res))

            threading.Thread(target=work, daemon=True).start()

        def _pump(self):
            try:
                while True:
                    on_done, res = self._q.get_nowait()
                    try:
                        on_done(res)
                    except Exception as e:  # one bad callback must not stop the pump
                        self.log(f"{type(e).__name__}: {e}", alert=True)
            except queue.Empty:
                pass
            finally:
                self.root.after(100, self._pump)

        # ---- refresh -------------------------------------------------------
        def _tick(self):
            self.refresh()
            self.root.after(60_000, self._tick)

        def refresh(self, full=True):
            if self._gate.request():
                self._bg(lambda: self.source.take(full=full), self._rendered)

        def _rendered(self, snap):
            try:
                self._render(snap)
            finally:
                if self._gate.done():  # a refresh was asked for mid-flight
                    self.refresh()

        def _render(self, snap):
            if isinstance(snap, Exception):
                self.log(f"refresh failed: {snap}", alert=True)
                return
            for w in self.cards.winfo_children():
                w.destroy()
            self._buttons = [b for b in self._buttons if b.winfo_exists()]
            if not snap.accounts:
                tk.Label(self.cards, text="No accounts yet — use + Add", bg=BG, fg=MUTED,
                         font=fonts["ui"]).pack(pady=40)
            for acc in snap.accounts:
                self._card(acc, snap.taken_at)
            self._set_busy(self._busy)

        def _card(self, acc, now):
            v = card_lines(acc, now)
            num = acc.number
            box, c = round_box(self.cards, CARD)
            box.pack(fill="x", pady=(0, 12))
            top = tk.Frame(c, bg=CARD)
            top.pack(fill="x")
            tk.Label(top, text="●", bg=CARD, fg=ACCENT if v.active else TRACK,
                     font=fonts["small"]).pack(side="left", padx=(0, 8))
            tk.Label(top, text=v.title, bg=CARD, fg=MUTED if v.disabled else FG,
                     font=fonts["name"]).pack(side="left")
            mm = tk.Menu(self.root, tearoff=0)
            mm.add_command(label="Switch", command=lambda: self._switch(num))
            mm.add_command(
                label="Enable" if v.disabled else "Disable",
                command=lambda: self._act(lambda: switcher.set_account_disabled(num, not v.disabled)),
            )
            mm.add_command(label="Remove…", command=lambda: self._remove(num, v.title))
            self._btn(top, "⋯", None, menu=mm).pack(side="right")
            if v.active:
                Pill(top, "active", kind="chip", font=fonts["small"]).pack(side="right", padx=6)
            elif v.switchable:
                self._btn(top, "Switch", lambda: self._switch(num), kind="primary").pack(
                    side="right", padx=6)
            if v.disabled:
                Pill(top, "disabled", kind="ghost", font=fonts["small"]).pack(side="right", padx=6)
            if v.tag:  # own line, so long org names never squeeze the buttons
                tk.Label(c, text=v.tag, bg=CARD, fg=MUTED, font=fonts["small"]).pack(
                    anchor="w", padx=(22, 0))
            if v.message:
                tk.Label(c, text=v.message, bg=CARD, fg=MUTED, font=fonts["ui"]).pack(
                    anchor="w", pady=(10, 0))
            for i, row in enumerate(v.rows):
                f = tk.Frame(c, bg=CARD)
                f.pack(fill="x", pady=(12 if i == 0 else 6, 0))
                tk.Label(f, text=row.label, width=3, anchor="w", bg=CARD, fg=MUTED,
                         font=fonts["small"]).pack(side="left")
                bar = tk.Canvas(f, width=170, height=8, bg=CARD, highlightthickness=0)
                bar.pack(side="left", padx=(4, 10))
                bar.create_line(4, 4, 166, 4, width=6, capstyle="round", fill=TRACK)
                if row.pct:
                    x = 4 + 162 * min(row.pct, 100) / 100
                    bar.create_line(4, 4, x, 4, width=6, capstyle="round",
                                    fill=DANGER if row.pct >= 90 else ACCENT)
                pct = "—" if row.pct is None else f"{row.pct:.0f}%"
                tk.Label(f, text=pct, width=4, anchor="e", bg=CARD, fg=FG,
                         font=fonts["ui"]).pack(side="left")
                extra = "  ".join(x for x in (row.reset, row.age) if x)
                tk.Label(f, text=extra, bg=CARD, fg=MUTED, font=fonts["small"]).pack(
                    side="left", padx=(10, 0))
            box.fit()
            self._hover(box)

        def _hover(self, box):
            def paint(old, new):
                for child in box.winfo_children():
                    recolor(child, old, new)
                box.itemconfigure("bg", fill=new)

            def leave(_e):
                # Moving onto a child also fires <Leave>; only un-hover when truly outside.
                w = self.root.winfo_containing(*self.root.winfo_pointerxy())
                if w is None or not str(w).startswith(str(box)):
                    paint(CARD_HOVER, CARD)

            box.bind("<Enter>", lambda _e: paint(CARD, CARD_HOVER), add="+")
            box.bind("<Leave>", leave, add="+")

        # ---- actions -------------------------------------------------------
        def _act(self, fn):
            if self._busy:
                return
            self._set_busy(True)
            self._bg(lambda: run_action(fn), self._acted)

        def _acted(self, res):
            self._set_busy(False)
            if isinstance(res, Exception):
                self.log(f"{type(res).__name__}: {res}", alert=True)
            else:
                self.log(res.output, alert=not res.ok)
            if self._closing:
                self._close()
                return
            self.refresh()

        def _switch(self, num):
            self._act(lambda: switcher.switch_to(num))

        def _remove(self, num, title):
            if messagebox.askyesno("Remove account", f"Remove {title}?", parent=self.root):
                self._act(lambda: switcher.remove_account(num, assume_yes=True))

        def _add_current(self):
            self._act(lambda: switcher.add_account(assume_yes=True))

        def _add_token(self):
            email = simpledialog.askstring("Add from token", "Email:", parent=self.root)
            if not email or not email.strip():
                return
            token = simpledialog.askstring("Add from token", "Token:", show="•", parent=self.root)
            if not token or not token.strip():
                return
            self._act(lambda: switcher.add_account_from_token(token=token.strip(), email=email.strip()))

        # ---- auto-switch engine (mirrors menubar.py) -----------------------
        def _toggle_auto(self):
            if self.auto_var.get():
                self._start_engine()
            else:
                self._stop_engine()

        def _start_engine(self):
            if self._engine is not None:
                return
            try:
                engine = AutoSwitchEngine(
                    switcher, load_settings(switcher.backup_dir), self._events.put, dry_run=False
                )
            except Exception as e:
                self.log(f"auto-switch failed to start: {e}", alert=True)
                self.auto_var.set(False)
                return
            self._engine = engine
            self._engine_thread = threading.Thread(
                target=self._run_engine, args=(engine,), daemon=True
            )
            self._engine_thread.start()

        def _run_engine(self, engine):
            try:
                engine.run_loop()
            except Exception as e:
                self._events.put(_EngineCrash(str(e)))

        def _stop_engine(self):
            if self._engine is not None:
                self._engine.stop()
                self._engine = None

        def _drain_events(self):
            try:
                while True:
                    ev = self._events.get_nowait()
                    text, alert = event_line(ev)
                    self.log(text, alert, stamp=False)  # event_line already stamps
                    if ev.kind == "switch" and not getattr(ev, "dry_run", False):
                        self.refresh()
            except queue.Empty:
                pass
            self.root.after(500, self._drain_events)

        def _close(self):
            if self._busy:  # don't kill a credential write mid-way
                if not self._closing:
                    self._closing = True
                    self.log("Waiting for the current action to finish…", alert=True)
                return
            self._stop_engine()
            if self._engine_thread is not None:
                self._engine_thread.join(timeout=5)  # may be mid-switch
            self.root.destroy()

    DesktopApp().root.mainloop()
    return 0


def _show_error(text: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        print(text, file=sys.stderr)
        return
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("cswap desktop", text)
    root.destroy()


def main() -> int:
    # --windowed exes have no console: sys.stdout/stderr are None.
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()
    try:
        return run(ClaudeAccountSwitcher())
    except ClaudeSwitchError as e:
        _show_error(str(e))
    except Exception as e:  # windowed exe has no console: surface everything
        _show_error(f"{type(e).__name__}: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
