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


BG, CARD, FG, MUTED = "#16161a", "#1f1f24", "#e6e6e6", "#8a8a93"
ACCENT, TRACK, DANGER = "#d97757", "#2c2c33", "#e5484d"


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
            r.geometry("520x640")
            r.minsize(420, 360)
            r.configure(bg=BG)
            r.protocol("WM_DELETE_WINDOW", self._close)

            head = tk.Frame(r, bg=BG)
            head.pack(fill="x", padx=10, pady=8)
            add = self._btn(head, "+ Add ▾", None, menu=True)
            add.pack(side="left")
            m = tk.Menu(add, tearoff=0)
            m.add_command(label="Current login", command=self._add_current)
            m.add_command(label="From token…", command=self._add_token)
            add["menu"] = m
            self.auto_var = tk.BooleanVar(value=False)
            tk.Checkbutton(
                head, text="Auto", variable=self.auto_var, command=self._toggle_auto,
                bg=BG, fg=FG, selectcolor=CARD, activebackground=BG, activeforeground=FG,
            ).pack(side="left", padx=10)
            self._btn(head, "⟳", self.refresh).pack(side="right")
            self._btn(head, "Best", lambda: self._act(lambda: switcher.switch(strategy="best"))).pack(
                side="right", padx=6
            )

            # Output panel is packed first (bottom) so the card area takes the rest.
            self.panel = tk.Frame(r, bg=BG)
            self.panel.pack(side="bottom", fill="x")
            self.out_toggle = tk.Label(self.panel, text="▸ Output", bg=BG, fg=MUTED, cursor="hand2")
            self.out_toggle.pack(anchor="w", padx=10)
            self.out_toggle.bind("<Button-1>", lambda _e: self._toggle_panel())
            self.out = tk.Text(
                self.panel, height=10, bg=CARD, fg=FG, relief="flat", state="disabled",
                font=("Consolas", 9), wrap="word",
            )
            self.out.tag_configure("alert", foreground=DANGER)
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
            self.canvas.pack(fill="both", expand=True, padx=10)

            r.after(100, self._pump)
            r.after(500, self._drain_events)
            self._tick()

        # ---- widgets -------------------------------------------------------
        def _btn(self, parent, text, cmd, menu=False):
            kw = dict(text=text, bg=CARD, fg=FG, activebackground=ACCENT, activeforeground=FG,
                      relief="flat", padx=8)
            b = tk.Menubutton(parent, **kw) if menu else tk.Button(parent, command=cmd, **kw)
            self._buttons.append(b)
            return b

        def _set_busy(self, busy):
            self._busy = busy
            for b in self._buttons:
                try:
                    b.configure(state="disabled" if busy else "normal")
                except tk.TclError:
                    pass  # stale card button

        def _toggle_panel(self, show=None):
            show = (not self._panel_open) if show is None else show
            if show == self._panel_open:
                return
            self._panel_open = show
            self.out_toggle.configure(text=("▾ Output" if show else "▸ Output"))
            if show:
                self.out.pack(fill="x", padx=10, pady=(0, 8))
            else:
                self.out.pack_forget()

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
                    on_done(res)
            except queue.Empty:
                pass
            self.root.after(100, self._pump)

        # ---- refresh -------------------------------------------------------
        def _tick(self):
            self.refresh()
            self.root.after(60_000, self._tick)

        def refresh(self, full=True):
            if self._gate.request():
                self._bg(lambda: self.source.take(full=full), self._rendered)

        def _rendered(self, snap):
            self._render(snap)
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
                tk.Label(self.cards, text="No accounts yet — use + Add", bg=BG, fg=MUTED).pack(pady=20)
            for acc in snap.accounts:
                self._card(acc, snap.taken_at)
            self._set_busy(self._busy)

        def _card(self, acc, now):
            v = card_lines(acc, now)
            num = acc.number
            c = tk.Frame(self.cards, bg=CARD, padx=10, pady=8)
            c.pack(fill="x", pady=4)
            top = tk.Frame(c, bg=CARD)
            top.pack(fill="x")
            tk.Label(top, text=v.title, bg=CARD, fg=MUTED if v.disabled else FG,
                     font=("Segoe UI", 10, "bold")).pack(side="left")
            if v.tag:
                tk.Label(top, text=v.tag, bg=CARD, fg=MUTED).pack(side="left", padx=6)
            more = self._btn(top, "⋯", None, menu=True)
            more.pack(side="right")
            mm = tk.Menu(more, tearoff=0)
            mm.add_command(label="Switch", command=lambda: self._switch(num))
            mm.add_command(
                label="Enable" if v.disabled else "Disable",
                command=lambda: self._act(lambda: switcher.set_account_disabled(num, not v.disabled)),
            )
            mm.add_command(label="Remove…", command=lambda: self._remove(num, v.title))
            more["menu"] = mm
            if v.active:
                tk.Label(top, text="ACTIVE", bg=CARD, fg=ACCENT).pack(side="right", padx=6)
            elif v.switchable:
                self._btn(top, "Switch", lambda: self._switch(num)).pack(side="right", padx=6)
            if v.message:
                tk.Label(c, text=v.message, bg=CARD, fg=MUTED).pack(anchor="w")
            for row in v.rows:
                f = tk.Frame(c, bg=CARD)
                f.pack(fill="x", pady=2)
                tk.Label(f, text=row.label, width=3, bg=CARD, fg=MUTED).pack(side="left")
                bar = tk.Canvas(f, width=160, height=8, bg=TRACK, highlightthickness=0)
                bar.pack(side="left", padx=6)
                if row.pct is not None:
                    bar.create_rectangle(
                        0, 0, 160 * min(row.pct, 100) / 100, 8, width=0,
                        fill=DANGER if row.pct >= 90 else ACCENT,
                    )
                pct = "—" if row.pct is None else f"{row.pct:.0f}%"
                tk.Label(f, text=pct, width=5, bg=CARD, fg=FG).pack(side="left")
                extra = "  ".join(x for x in (row.reset, row.age) if x)
                tk.Label(f, text=extra, bg=CARD, fg=MUTED).pack(side="left")

        # ---- actions -------------------------------------------------------
        def _act(self, fn):
            if self._busy:
                return
            self._set_busy(True)
            self._bg(lambda: run_action(fn), self._acted)

        def _acted(self, res):
            self._set_busy(False)
            if isinstance(res, Exception):
                self.log(str(res), alert=True)
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
                    self.log("Waiting for the current action to finish…")
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
        switcher = ClaudeAccountSwitcher()
    except ClaudeSwitchError as e:
        _show_error(str(e))
        return 1
    return run(switcher)


if __name__ == "__main__":
    sys.exit(main())
