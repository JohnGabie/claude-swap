"""The lines window (SL3, SL6): its page, how the tray drives its window, and what the page calls.

The general lines (``autoswitch.threshold`` and ``autoswitch.weeklyThreshold``) are edited here; an
account's own lines are only shown. ``LinesWindow`` opens the window beside the pill gear and hides it;
``LinesWindowApi`` is what the page calls through ``pywebview.api``. The pure parts (the card ``lines``
field, the general lines) are import-safe without pywebview.

Privacy (SL6): no email, organization name or token reaches the page, a return value or a log line. A
failure returns a fixed text and logs its exception type name only.
"""

from __future__ import annotations

import logging
import math
import threading

from claude_swap import fleet_page, lines, worktime
from claude_swap.models import AccountPolicy
from claude_swap.settings import AutoSwitchSettings
from claude_swap.tray import THRESHOLD_MAX, THRESHOLD_MIN

_logger = logging.getLogger("claude-swap")

LINES_TITLE = "cswap lines"
LINES_W, LINES_H = 320, 520
RELOAD_S = 30  # the page reads its data again this often while the window is open
RESET_THRESHOLD = AutoSwitchSettings().threshold  # "Restaurar padrão" (SL3)
FIVE_KEY, WEEK_KEY = "autoswitch.threshold", "autoswitch.weeklyThreshold"
DAYS_KEY = "autoswitch.workDays"
DAY_BOXES = (("Seg", "Segunda-feira"), ("Ter", "Terça-feira"), ("Qua", "Quarta-feira"), ("Qui", "Quinta-feira"),
             ("Sex", "Sexta-feira"), ("Sáb", "Sábado"), ("Dom", "Domingo"))

WRITE_FAILED = "Não foi possível salvar o limite."
READ_FAILED = "Não foi possível ler os limites."
CLOSE_FAILED = "Não foi possível fechar a janela."


def _days_text(value) -> str:
    """The normal form of a stored work-days text; a missing or bad one reads as ``all`` (WD2)."""
    try:
        return worktime.format_days(worktime.parse_days(value))
    except ValueError:
        return "all"


def _number(value) -> bool:
    """An int or a finite float; a bool is not a number here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---- the effective lines (SL2, SL6) --------------------------------------------------------

def line_fields(policy: AccountPolicy, threshold: float, weekly_threshold: float | None) -> dict:
    """One account's effective lines, and which of them are its own: ``{five, week, own5, ownWeek}``."""
    five, weekly = lines.account_lines(policy, threshold, weekly_threshold)
    return {"five": float(five), "week": float(lines.effective_weekly(five, weekly)),
            "own5": policy.threshold is not None, "ownWeek": policy.weekly_threshold is not None}


def general_lines(threshold: float, weekly_threshold: float | None) -> dict:
    """The effective general lines: ``{five, week}``; an unset weekly line is the 5-hour line."""
    return {"five": float(threshold), "week": float(lines.effective_weekly(threshold, weekly_threshold))}


def with_lines(cards: list[dict], policies: dict, threshold: float, weekly_threshold: float | None) -> list[dict]:
    """Copies of the cards, each with its ``lines`` field; an account the store does not know gets the general lines."""
    out = []
    for card in cards:
        shown = dict(card)
        shown["lines"] = line_fields(policies.get(card.get("num")) or AccountPolicy(), threshold, weekly_threshold)
        out.append(shown)
    return out


def _use(card: dict, label: str) -> float | None:
    """The current use of one window of a card, or None with no reading."""
    row = next((r for r in card.get("rows") or () if r.get("label") == label), None)
    pct = row.get("pct") if row else None
    return float(pct) if _number(pct) else None


# ---- the window --------------------------------------------------------------------------

class LinesWindow:
    """Open or hidden state of the lines window. ``gtk(fn)`` runs ``fn(view)`` on the GTK loop;
    ``anchor()`` is ``PillController.gear_anchor()``."""

    def __init__(self, gtk, anchor):
        self._gtk = gtk
        self._anchor = anchor
        self._open = threading.Event()

    @property
    def is_open(self) -> bool:
        return self._open.is_set()

    def open(self) -> None:
        x, y = fleet_page.panel_position(self._anchor(), size=(LINES_W, LINES_H))
        self._open.set()

        def show(view):
            view.window.show_all()
            view.window.deiconify()
            view.window.present()  # the keyboard focus, so Esc closes it
            view.window.move(x, y)  # after mapping, which can recentre the window
            view.webview.run_javascript("window.linesOpen&&window.linesOpen()", None, None, None)

        self._gtk(show)

    def close(self) -> None:
        self._open.clear()

        def hide(view):
            view.webview.run_javascript("window.linesClosed&&window.linesClosed()", None, None, None)
            view.window.hide()

        self._gtk(hide)

    def toggle(self) -> None:
        """The pill gear: the first click opens the window, the second hides it."""
        self.close() if self.is_open else self.open()


# ---- what the page calls -------------------------------------------------------------------

class LinesWindowApi:
    """``pywebview.api`` of the lines page: ``get()``, ``set(key, value)`` and ``close()`` (SL6).

    ``core()`` gives the core settings, ``policies()`` the account policies, ``cards()`` the tray's latest
    cards in pill order, ``auto()`` the auto-switch state. ``save_settings`` is ``DesktopApi.save_settings``;
    ``on_close`` hides the window; ``on_saved`` asks the tray for a refresh.
    """

    def __init__(self, core, policies, cards, auto, save_settings, on_close, on_saved):
        self._core = core
        self._policies = policies
        self._cards = cards
        self._auto = auto
        self._save_settings = save_settings
        self._on_close = on_close
        self._on_saved = on_saved

    def get(self) -> dict:
        try:
            core = self._core()
            policies = self._policies()
            accounts = []
            for card in self._cards():
                num = card.get("num")
                fields = line_fields(policies.get(num) or AccountPolicy(), core.threshold, core.weekly_threshold)
                accounts.append({"num": num, **fields, "use5": _use(card, "5h"), "useWeek": _use(card, "7d")})
            return {"five": float(core.threshold),
                    "week": None if core.weekly_threshold is None else float(core.weekly_threshold),
                    "autoSwitch": self._auto() is True,
                    "workDays": _days_text(getattr(core, "work_days", "all")), "accounts": accounts}
        except Exception as e:  # noqa: BLE001 - the page keeps its last data
            _logger.warning("lines window read failed: %s", type(e).__name__)
            return {"ok": False, "error": READ_FAILED}

    def set(self, key, value=None) -> dict:
        try:
            core = self._write(key, value)
            if core is None:
                return {"ok": False, "error": WRITE_FAILED}
            res = self._save(core)
            if not (isinstance(res, dict) and res.get("ok") is True):
                return {"ok": False, "error": WRITE_FAILED}
            self._on_saved()
            return {"ok": True, "error": None}
        except Exception as e:  # noqa: BLE001 - the page shows the fixed text
            _logger.warning("lines window write failed: %s", type(e).__name__)
            return {"ok": False, "error": WRITE_FAILED}

    def close(self) -> dict:
        try:
            self._on_close()
            return {"ok": True, "error": None}
        except Exception as e:  # noqa: BLE001
            _logger.warning("lines window close failed: %s", type(e).__name__)
            return {"ok": False, "error": CLOSE_FAILED}

    @staticmethod
    def _write(key, value) -> dict | None:
        """The core settings one ``set`` writes, or None for a bad input."""
        if key == "reset":
            return {FIVE_KEY: RESET_THRESHOLD, WEEK_KEY: None}
        if key == "workDays":
            try:
                return {DAYS_KEY: worktime.format_days(worktime.parse_days(value))}
            except ValueError:
                return None
        if key == "week" and value is None:
            return {WEEK_KEY: None}
        if key not in ("five", "week") or not _number(value) or not THRESHOLD_MIN <= value <= THRESHOLD_MAX:
            return None
        return {FIVE_KEY if key == "five" else WEEK_KEY: value}

    def _save(self, core: dict):
        """``save_settings`` in a worker thread (a running engine restarts there); its result."""
        box: dict = {}

        def work():
            try:
                box["res"] = self._save_settings(core, {})
            except Exception as e:  # noqa: BLE001
                box["error"] = type(e).__name__

        worker = threading.Thread(target=work, name="cswap-lines-save", daemon=True)
        worker.start()
        worker.join()
        if "error" in box:
            _logger.warning("lines window write failed: %s", box["error"])
        return box.get("res")


# ---- the page --------------------------------------------------------------------------------
# Frame A of the mock "Limites no pill", with the FP2 colours. Dark only, as the fleet panel.

LINES_PAGE = r"""<!doctype html><meta charset=utf-8><title>cswap lines</title><style>
:root{color-scheme:dark;--surface:#000000;--raised:#121A1C;--track:#1E2426;--ink:#F4EEDB;--ink2:#CFC6A8;--muted:#8F8873;
 --grid:rgba(233,216,166,.08);--spent:#0A9396;--avail:#94D2BD;--warning:#EE9B00;--critical:#CF4332}
*{box-sizing:border-box}
html,body{margin:0;width:320px;height:520px;overflow:hidden;background:#000000}
body{font:13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);user-select:none;cursor:default;
 display:flex;flex-direction:column;padding:16px 18px 12px;background:var(--surface)}
.head{display:flex;justify-content:space-between;align-items:center;gap:10px}
h1{font:650 14px/1.2 system-ui,sans-serif;margin:0}
.x{width:24px;height:24px;border-radius:7px;display:grid;place-items:center;color:var(--ink2);background:var(--raised);
 cursor:pointer;font-size:12px;flex:none}
.x:hover{color:var(--ink)}
.sub{color:var(--muted);font-size:11.5px;margin:2px 0 10px}
.off{color:var(--muted);font-size:11.5px;margin:-6px 0 10px}
.off[hidden]{display:none}
.row{margin-bottom:12px}
.top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px}
.top i{font-style:normal;color:var(--ink2)}
.top em{font-style:normal;font:650 15px/1 system-ui,sans-serif;font-variant-numeric:tabular-nums}
.hint{color:var(--muted);font-size:11px;margin-top:4px}
input[type=range]{-webkit-appearance:none;appearance:none;display:block;width:100%;height:18px;margin:0;background:transparent;
 outline:none;--c:var(--spent);--p:0%}
input[type=range].week{--c:var(--avail)}
input[type=range]:disabled{--c:#3A4446}
input[type=range]::-webkit-slider-runnable-track{height:4px;border-radius:2px;
 background:linear-gradient(to right,var(--c) var(--p),var(--track) var(--p))}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;margin-top:-6px;border-radius:50%;
 background:var(--ink);box-shadow:0 0 0 3px rgba(244,238,219,.15);cursor:pointer}
input[type=range]:disabled::-webkit-slider-thumb{background:#5A6466;cursor:default}
.tg{display:flex;align-items:center;gap:8px;color:var(--ink2);font-size:12px;margin-top:8px;cursor:pointer;width:max-content}
.tg input{display:none}
.tg u{width:26px;height:15px;border-radius:8px;background:var(--track);position:relative;flex:none}
.tg u::after{content:"";position:absolute;left:2px;top:2px;width:11px;height:11px;border-radius:50%;background:var(--muted)}
.tg input:checked+u{background:var(--spent)}
.tg input:checked+u::after{left:13px;background:var(--ink)}
.days{display:flex;gap:4px}
.day{flex:1;cursor:pointer}
.day input{display:none}
.day u{display:block;text-decoration:none;text-align:center;font-size:11.5px;line-height:1;padding:6px 0;border-radius:6px;
 background:var(--raised);color:var(--muted)}
.day input:checked+u{background:var(--spent);color:var(--ink)}
.sec{border-top:1px solid var(--grid);padding-top:10px;flex:1;min-height:0;display:flex;flex-direction:column}
.sec>b{display:block;font:600 11px/1 system-ui,sans-serif;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;
 margin-bottom:6px}
.accs{flex:1;min-height:0;overflow-y:auto;margin:0 -6px}
.acc{display:grid;grid-template-columns:28px 1fr auto;align-items:center;gap:8px;padding:5px 6px;border-radius:8px}
.acc:hover{background:var(--raised)}
.acc .n{font:650 12px/1 system-ui,sans-serif;color:var(--ink2)}
.acc .v{color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums}
.chip{font:600 11px/1 system-ui,sans-serif;padding:3px 7px;border-radius:6px;white-space:nowrap;background:var(--raised);
 color:var(--muted)}
.chip.own{background:rgba(238,155,0,.14);color:var(--warning)}
.err{color:var(--critical);font-size:11.5px;margin-top:6px}
.err:empty{display:none}
.foot{display:flex;justify-content:space-between;align-items:center;margin-top:8px;color:var(--muted);font-size:11.5px}
.foot a{color:var(--avail);text-decoration:none;cursor:pointer}
</style>
<div class=head><h1>Limites de troca</h1><b class=x id=close title="Fechar">&#x2715;</b></div>
<div class=sub>Quando a conta ativa chega na linha, o cswap troca de conta.</div>
<div class=off id=off hidden>Auto-switch desligado: os limites valem quando ligar.</div>
<div class=row>
 <div class=top><i>Sessão de 5 h</i><em id=v5>&ndash;</em></div>
 <input id=s5 class=five type=range min=__MIN__ max=__MAX__ step=1>
 <div class=hint id=h5>&nbsp;</div>
</div>
<div class=row>
 <div class=top><i>Semana</i><em id=vw>&ndash;</em></div>
 <input id=sw class=week type=range min=__MIN__ max=__MAX__ step=1>
 <label class=tg><input type=checkbox id=same><u></u>Igual à de 5 h</label>
</div>
<div class=row>
 <div class=top><i>Dias de trabalho</i></div>
 <div class=days>__DAYS__</div>
 <div class=hint>O ritmo conta só esses dias. Uso fora deles é exceção.</div>
</div>
<div class=sec><b>Por conta</b><div class=accs id=accs></div></div>
<div class=err id=err></div>
<div class=foot><span>Salvo ao soltar</span><a id=reset href="#">Restaurar padrão</a></div>
<script>
const $=id=>document.getElementById(id);
const LMIN=__MIN__,LMAX=__MAX__,RELOAD_MS=__RELOAD_MS__;
const FAIL='__FAIL__';
const s5=$('s5'),sw=$('sw'),same=$('same');
let timer=null,held=false,waiting=null;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// A whole number has no decimal; any other value has one, with a comma ("99,9")
const lnum=v=>{const r=Math.round(Number(v)*10)/10;return Number.isInteger(r)?String(r):r.toFixed(1).replace('.',',');};
const slider=v=>String(Math.min(LMAX,Math.round(Number(v))));
const use=v=>typeof v==='number'?Math.round(v)+'%':'–';
function chip(a){
 if(!(a.own5||a.ownWeek)) return '<span class=chip>geral</span>';
 const t=[a.own5?'5 h '+lnum(a.five):'',a.ownWeek?'sem '+lnum(a.week):''].filter(Boolean).join(' · ');
 return `<span class="chip own">${t}</span>`;
}
const row=a=>`<div class=acc><span class=n>#${esc(a.num)}</span><span class=v>5 h ${use(a.use5)} · sem ${use(a.useWeek)}</span>${chip(a)}</div>`;
function fill(s){s.style.setProperty('--p',((Number(s.value)-LMIN)/(LMAX-LMIN)*100).toFixed(2)+'%');}
function showFive(v){$('v5').textContent=lnum(v)+'%';$('h5').textContent='Troca quando a janela de 5 h passa de '+lnum(v)+'%.';fill(s5);}
function showWeek(v){$('vw').textContent=lnum(v)+'%';fill(sw);}
function paint(d){
 const wk=d.week==null?d.five:d.week;
 s5.value=slider(d.five);showFive(d.five);
 same.checked=d.week==null;sw.disabled=d.week==null;sw.value=slider(wk);showWeek(wk);
 dayset(d.workDays).forEach((v,i)=>{wd[i].checked=v;});
 $('off').hidden=d.autoSwitch===true;
 $('accs').innerHTML=d.accounts.map(row).join('');
}
// The work days (WD6): seven toggles; the text is the normal form of WD2 (runs of 3 or more as a-b)
const WD=['mon','tue','wed','thu','fri','sat','sun'],wd=WD.map((_,i)=>$('wd'+i));
function dayset(t){
 if(typeof t!=='string'||t==='all') return WD.map(()=>true);
 const on=WD.map(()=>false);
 t.split(',').forEach(p=>{const r=p.split('-'),a=WD.indexOf(r[0]),b=r.length>1?WD.indexOf(r[1]):a;
  for(let i=a;a>=0&&i<=b;i++) on[i]=true;});
 return on;
}
function dayform(on){
 const n=on.filter(Boolean).length;
 if(n===7) return 'all';
 const out=[];
 for(let i=0;i<7;i++){
  if(!on[i]) continue;
  let j=i;while(j+1<7&&on[j+1]) j++;
  if(j-i>=2) out.push(WD[i]+'-'+WD[j]); else for(let k=i;k<=j;k++) out.push(WD[k]);
  i=j;
 }
 return out.join(',');
}
const bridge=()=>window.pywebview&&pywebview.api;
function load(){
 if(!bridge()) return;
 pywebview.api.get().then(d=>{if(!(d&&Array.isArray(d.accounts)))return;if(held)waiting=d;else paint(d);},()=>{});
}
function write(key,value){
 if(!bridge()) return;
 pywebview.api.set(key,value).then(r=>{$('err').textContent=r&&r.ok===true?'':(r&&typeof r.error==='string'&&r.error)||FAIL;load();},
  ()=>{$('err').textContent=FAIL;load();});
}
function close(){if(bridge())pywebview.api.close();}
// A read that lands while a slider is held waits for the release (SL3): a redraw would move the thumb mid-drag.
function release(){held=false;if(waiting){const d=waiting;waiting=null;paint(d);}}
const letGo=()=>setTimeout(release,0); // one turn later: the slider's own change event reads its value first
s5.addEventListener('pointerdown',()=>{held=true;});
sw.addEventListener('pointerdown',()=>{held=true;});
document.addEventListener('pointerup',letGo);
document.addEventListener('pointercancel',letGo);
s5.addEventListener('input',()=>{showFive(Number(s5.value));if(same.checked){sw.value=s5.value;showWeek(Number(sw.value));}});
s5.addEventListener('change',()=>write('five',Number(s5.value)));
sw.addEventListener('input',()=>showWeek(Number(sw.value)));
sw.addEventListener('change',()=>write('week',Number(sw.value)));
same.addEventListener('change',()=>{
 sw.disabled=same.checked;
 if(same.checked){sw.value=s5.value;showWeek(Number(sw.value));write('week',null);}
 else write('week',Number(sw.value));
});
wd.forEach(box=>box.addEventListener('change',()=>{
 const on=wd.map(b=>b.checked);
 if(!on.some(Boolean)){box.checked=true;return;} // the last day on cannot be turned off
 write('workDays',dayform(on));
}));
$('reset').addEventListener('click',e=>{e.preventDefault();write('reset',null);});
$('close').addEventListener('click',close);
document.addEventListener('keydown',e=>{if(e.key==='Escape')close();});
window.linesOpen=()=>{load();clearInterval(timer);timer=setInterval(load,RELOAD_MS);};
window.linesClosed=()=>clearInterval(timer);
window.addEventListener('pywebviewready',load);
</script>""".replace("__DAYS__", "".join(
    f'<label class=day title="{name}"><input type=checkbox id=wd{i}><u>{label}</u></label>'
    for i, (label, name) in enumerate(DAY_BOXES))).replace("__MIN__", str(THRESHOLD_MIN)).replace("__MAX__", str(THRESHOLD_MAX)).replace(
    "__RELOAD_MS__", str(RELOAD_S * 1000)).replace("__FAIL__", WRITE_FAILED)
