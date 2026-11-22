"""The fleet panel (FP4-FP9): its page, where its window goes, and the data the tray feeds it.

The pure parts (placement, the card ``budget`` field, the decision events) are import-safe
without pywebview. ``FleetHistory`` keeps the usage history up to date in a worker thread,
under one lock. ``FleetPanelApi`` is what the page calls through ``pywebview.api``.

Privacy (FP7, FP11): project keys reach this page only. No email, organization, token or
path reaches a payload or a log line; a failed update logs its exception type name only.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from claude_swap import budget_calibrate, decision_log, fleet_view, usage_history
from claude_swap.models import normalize_alias
from claude_swap.pill import SQUARE_SIZE, THICKNESS

_logger = logging.getLogger("claude-swap")

PANEL_TITLE = "cswap fleet"
PANEL_W, PANEL_H = 720, 860
PANEL_GAP = 12  # px between the pill's body and the panel
HISTORY_UPDATE_S = 300  # FP11: the worker updates the usage history every 5 minutes
DEFAULT_PERIOD = "7d"
NOW_PERIOD = "1h"  # the period whose "Agora" tile gives the active card's "Consumo agora"

_PLAN = re.compile(r"[a-z_]+")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")


# ---- placement ---------------------------------------------------------------

def panel_position(anchor: dict, size: tuple[int, int] = (PANEL_W, PANEL_H), gap: int = PANEL_GAP) -> tuple[int, int]:
    """Top-left of the panel: beside the pill on the side away from its edge, clamped to the work area.

    Left and right pills: the panel's top is level with the pill square. Top and bottom pills:
    the panel's left is level with the square. ``anchor`` is ``PillController.panel_anchor()``.
    """
    edge = anchor["edge"]
    sx, sy, sw, sh = anchor["square"]
    ax, ay, aw, ah = anchor["area"]
    w, h = size
    inset = (THICKNESS - SQUARE_SIZE) // 2  # from the square to the pill's inner side
    if edge == "right":
        x, y = sx - inset - gap - w, sy
    elif edge == "left":
        x, y = sx + sw + inset + gap, sy
    elif edge == "top":
        x, y = sx, sy + sh + inset + gap
    else:
        x, y = sx, sy - inset - gap - h
    x = max(ax, min(x, ax + aw - w))
    y = max(ay, min(y, ay + ah - h))
    return int(x), int(y)


# ---- the card budget (FP10) ----------------------------------------------------

def _number(value):
    """An int or a finite float, else None (a bool is not a number here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _iso_text(value):
    return value if isinstance(value, str) and _ISO.fullmatch(value) else None


def _alias(value):
    try:
        return value if isinstance(value, str) and normalize_alias(value) == value else None
    except ValueError:
        return None


def _epoch(text):
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def card_budget(account: dict, alias, now_per_min, now: float) -> dict:
    """The ``budget`` field of one account card, from that account of the budget.

    Each value is typed or None: the plan is a plain lowercase word (else ``unknown``), the
    reset times are ISO text, the numbers are int or float. ``alias`` is the switcher's alias
    of the account; anything that is not a valid alias is dropped.
    """
    seven = account.get("sevenDay") if isinstance(account.get("sevenDay"), dict) else {}
    five = account.get("fiveHour") if isinstance(account.get("fiveHour"), dict) else {}
    plan = account.get("plan")
    fetched = _epoch(account.get("fetchedAt"))
    return {
        "plan": plan if isinstance(plan, str) and _PLAN.fullmatch(plan) else "unknown",
        "fiveResetAt": _iso_text(five.get("resetsAt")),
        "weekResetAt": _iso_text(seven.get("resetsAt")),
        "weekElapsedPct": _number(seven.get("elapsedPct")),
        "projectedEndPct": _number(seven.get("projectedEndPct")),
        "tokensPerPercent": _number(seven.get("tokensPerPercent")),
        "nowPerMin": _number(now_per_min),
        "alias": _alias(alias),
        "readAgeS": None if fetched is None else max(0.0, now - fetched),
    }


def with_budget(cards: list[dict], budget: dict, aliases: dict, now_per_min, now: float) -> list[dict]:
    """Copies of the cards; each card whose account is in the budget gets its ``budget`` field.

    A card's ``num`` is the slot number as text; a budget account's ``id`` is a number.
    Only the active card gets ``nowPerMin`` (its "Consumo agora").
    """
    accounts = budget.get("accounts") if isinstance(budget, dict) else None
    accounts = [a for a in accounts or () if isinstance(a, dict)]
    out = []
    for card in cards:
        shown = dict(card)
        num = card.get("num")
        account = next((a for a in accounts if str(a.get("id")) == num), None)
        if account is not None:
            shown["budget"] = card_budget(account, (aliases or {}).get(num),
                                          now_per_min if card.get("active") else None, now)
        out.append(shown)
    return out


def now_per_min(history: dict, now: float) -> float:
    """Weighted tokens per minute of the last hour: the panel's "Agora" tile (FP4)."""
    return fleet_view.build_fleet({}, history, [], now, NOW_PERIOD)["tiles"]["nowPerMin"]


# ---- decision events -----------------------------------------------------------

def _parse(line: str):
    """One decision-log line as a dict, or None for a line that is not a JSON object."""
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return event if isinstance(event, dict) else None


class DecisionCache:
    """The decision-log events of ``decision_log.tail(root, POLL_LINES)``, parsed once per file.

    Each log file (the current one and its rotated copies) is parsed again only when its
    (path, mtime, size) changes, so a period change of the panel reuses the parsed events.
    """

    def __init__(self, backup_root: Path):
        self._root = Path(backup_root)
        self._lock = threading.Lock()
        self._files: dict[str, tuple[tuple, list]] = {}

    def _paths(self) -> list[Path]:
        base = decision_log.log_path(self._root)
        rotated = [base.with_name(f"{decision_log.LOG_FILENAME}.{i}") for i in range(decision_log.BACKUPS, 0, -1)]
        return rotated + [base]  # oldest first, as decision_log.tail reads them

    def _lines(self, path: Path) -> list:
        """The parsed non-empty lines of one file (None for a bad line); [] for a missing file."""
        try:
            st = path.stat()
        except OSError:
            self._files.pop(str(path), None)
            return []
        key = (st.st_mtime_ns, st.st_size)
        cached = self._files.get(str(path))
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            with path.open(encoding="utf-8") as f:
                parsed = [_parse(line.rstrip("\n")) for line in f if line.strip()]
        except (OSError, ValueError):
            self._files.pop(str(path), None)
            return []
        self._files[str(path)] = (key, parsed)
        return parsed

    def events(self) -> list[dict]:
        """The events of the last ``POLL_LINES`` log lines, oldest first; bad lines dropped."""
        with self._lock:
            lines: list = []
            for path in self._paths():
                lines.extend(self._lines(path))
            limit = budget_calibrate.POLL_LINES
            return [e for e in lines[-limit:] if e is not None]


def read_decisions(backup_root: Path) -> list[dict]:
    """The decision-log events as dicts; a line that is not a JSON object is dropped."""
    return DecisionCache(backup_root).events()


# ---- the usage history worker (FP11) --------------------------------------------

class FleetHistory:
    """The usage history, updated under one lock; ``data()`` is None until the first build."""

    def __init__(self, backup_root: Path, projects_dir: Path, update=None, clock=time.time):
        self._root = Path(backup_root)
        self._projects = Path(projects_dir)
        self._update = update or usage_history.update
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict | None = None
        self.failed = False  # no history at all: the update and the stored file both failed

    def data(self) -> dict | None:
        return self._data

    def refresh(self) -> dict | None:
        """Read the new log lines. A failure logs its exception type name only and never raises."""
        with self._lock:
            try:
                self._data = self._update(self._root, self._projects, self._clock())
                self.failed = False
            except Exception as e:  # noqa: BLE001 - the tray must keep running
                _logger.warning("usage history update failed: %s", type(e).__name__)
                if self._data is None:
                    try:
                        self._data = usage_history.load(self._root)
                    except Exception as e2:  # noqa: BLE001 - e.g. RecursionError on a deep file
                        _logger.warning("usage history load failed: %s", type(e2).__name__)
                        self.failed = True
            return self._data

    def run(self, stop: threading.Event, interval: float = HISTORY_UPDATE_S) -> None:
        """The worker: build now, then update every ``interval`` seconds until ``stop`` is set."""
        while not stop.is_set():
            self.refresh()
            stop.wait(interval)


# ---- what the page calls -----------------------------------------------------------

class FleetPanelApi:
    """``pywebview.api`` of the panel page: ``get(period)`` and ``close()``."""

    def __init__(self, history: FleetHistory, budget_fn, decisions_fn, on_close, clock=time.time):
        self._history = history
        self._budget = budget_fn
        self._decisions = decisions_fn
        self._on_close = on_close
        self._clock = clock

    def get(self, period=DEFAULT_PERIOD) -> dict:
        """The ``build_fleet`` payload; ``{"loading": true}`` until the first history build."""
        if not (isinstance(period, str) and period in fleet_view.PERIODS):
            period = DEFAULT_PERIOD
        data = self._history.data()
        if data is None:
            return {"error": True} if self._history.failed else {"loading": True}
        try:
            payload = fleet_view.build_fleet(self._budget(), data, self._decisions(), self._clock(), period)
        except Exception as e:  # noqa: BLE001 - the page shows an empty panel, the log has the type only
            _logger.warning("fleet panel payload failed: %s", type(e).__name__)
            return {"error": True}
        # item 7 of the T4 review: the page does not use the model id, so it does not get it
        payload["models"] = [{k: v for k, v in m.items() if k != "id"} for m in payload["models"]]
        return payload

    def close(self) -> dict:
        self._on_close()
        return {"ok": True}


# ---- the page --------------------------------------------------------------------------
# Ported from the mock "Fleet panel v18" with the FP2 colours. Dark only (FP2: panel column).
# Every size is fixed: hover and a period change never move a section (S11).

FLEET_PAGE = r"""<!doctype html><meta charset=utf-8><title>cswap fleet</title><style>
:root{color-scheme:dark;--surface:#000000;--raised:#121A1C;--sel:#1E2426;--ink:#F4EEDB;--ink2:#CFC6A8;--muted:#8F8873;
 --grid:rgba(233,216,166,.08);--spent:#0A9396;--spent-track:#05282A;--avail:#94D2BD;--avail-track:#1D3A31;
 --warning:#EE9B00;--critical:#CF4332;--ease-out:cubic-bezier(.23,1,.32,1);--spring:cubic-bezier(.2,.9,.25,1)}
*{box-sizing:border-box}
html,body{margin:0;width:720px;height:860px;overflow:hidden;background:#000000}
body{font:12px system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);user-select:none;cursor:default}
.panel{position:relative;width:720px;height:860px;padding:22px 24px 24px;overflow:hidden;contain:layout;background:var(--surface)}
.body{transition:opacity .2s}
.stale .body{opacity:.55}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;height:132px}
.lead{width:250px;flex:none}
.x{width:26px;height:26px;border-radius:7px;color:var(--ink2);display:grid;place-items:center;font-size:14px;background:var(--raised);cursor:pointer;flex:none}
.x:hover{color:var(--ink)}
.hero{font:600 56px system-ui,sans-serif;line-height:1;letter-spacing:-1px;height:56px;white-space:nowrap}
.status{display:flex;align-items:center;gap:6px;font:600 13px system-ui,sans-serif;margin-top:8px;height:18px;white-space:nowrap}
.status i{width:16px;height:16px;border-radius:50%;display:grid;place-items:center;font:700 10px system-ui,sans-serif;font-style:normal;color:#000000;background:var(--muted);flex:none}
.status.neutral{color:var(--ink2)}
.status.warning i{background:var(--warning)}
.status.critical i{background:var(--critical);color:#FFFFFF}
.cap{color:var(--muted);font-size:12px;margin-top:6px;height:30px;overflow:hidden;line-height:1.25}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));flex:1;margin-top:6px;min-width:0}
.tile{padding:2px 14px;border-left:1px solid var(--grid);min-width:0;overflow:hidden}
.tile .l,.st .l{color:var(--muted);font-size:12px;white-space:nowrap}
.tile .v,.st .v{font:700 28px system-ui,sans-serif;letter-spacing:-.3px;margin-top:3px;white-space:nowrap;height:34px}
.tile .s,.st .s{color:var(--ink2);font-size:11px;margin-top:2px;height:30px;overflow:hidden;line-height:1.35}
.u{font-size:.55em;font-weight:500;color:var(--muted);margin-left:3px;letter-spacing:0}
.spark{display:block;margin-top:4px;cursor:crosshair}
.h{font:600 13px system-ui,sans-serif;height:24px;margin:22px 0 6px;display:flex;justify-content:space-between;align-items:center;white-space:nowrap}
.h span{color:var(--muted);font-weight:400;font-size:11.5px}
.ht{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:400px}
.ht em{font-style:normal;font-weight:400;color:var(--ink2);font-size:11.5px;margin-left:10px}
.ht em i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;vertical-align:0}
.ht em b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.seg{display:inline-flex;background:var(--raised);border-radius:8px;padding:2px}
.seg b{font:600 11px system-ui,sans-serif;padding:3px 8px;border-radius:6px;color:var(--ink2);cursor:pointer}
.seg b:hover{color:var(--ink)}
.seg b.on{background:var(--sel);color:var(--ink);box-shadow:inset 0 0 0 1px var(--grid)}
#chart{height:212px}
.lg{display:flex;gap:14px;color:var(--muted);font-size:11px;margin-top:4px;height:16px;white-space:nowrap;overflow:hidden}
.lg span{display:inline-flex;align-items:center;gap:6px}
.lg i{display:inline-block;width:14px;height:2px;border-radius:1px}
.lg i.dash{background:none;border-top:1px dashed var(--ink2);height:0}
.lg i.r5{width:8px;height:8px;border-radius:50%;border:2px solid var(--spent);background:none}
.lg i.d7{width:8px;height:8px;border-radius:1px;background:var(--avail);transform:rotate(45deg)}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:26px}
.cols>div{min-width:0}
.cols>div:last-child{display:flex;flex-direction:column}
#models{height:154px}
.segbar{display:flex;gap:2px;height:18px;margin:4px 0 8px}
.segbar i{display:flex;align-items:center;justify-content:center;overflow:hidden;height:18px;border-radius:2px;font-style:normal;
 transform-origin:left;animation:grow .3s var(--ease-out) both;animation-delay:var(--d,0ms);transition:flex-grow .25s var(--ease-out),opacity .2s}
.segbar i:first-child{border-radius:4px 2px 2px 4px}.segbar i:last-child{border-radius:2px 4px 4px 2px}.segbar i:only-child{border-radius:4px}
.segbar i em{opacity:0;font:600 11px system-ui,sans-serif;font-style:normal;transition:opacity .15s;white-space:nowrap}
.segbar i.on em,.segbar i.empty em{opacity:1}
.ml{display:grid;grid-template-columns:12px minmax(0,1fr) 42px 52px;gap:8px;align-items:center;height:20px}
.ml .sw{width:10px;height:10px;border-radius:3px}
.ml .mn{color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ml.on .mn{color:var(--ink)}
.ml .mp{text-align:right;font:600 12px system-ui,sans-serif;font-variant-numeric:tabular-nums}
.ml .mt{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.ml.blank{visibility:hidden}
#heat{height:118px}
.hrow{display:grid;grid-template-columns:96px minmax(0,1fr) 62px;gap:4px;align-items:center;height:17px}
.hn{color:var(--ink2);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hc{display:flex;align-items:center;overflow:hidden}
.hc i{display:block;height:12px;border-radius:3px;flex:none;transition:box-shadow .12s}
.hc i.on{box-shadow:inset 0 0 0 1.5px var(--ink)}
.hd{background:none!important;font:500 9.5px system-ui,sans-serif;color:var(--muted);font-style:normal;overflow:visible;white-space:nowrap}
.he{display:flex;justify-content:flex-end;align-items:baseline;gap:5px;font-variant-numeric:tabular-nums;width:62px;white-space:nowrap;overflow:hidden}
.he b{font:600 12px system-ui,sans-serif}.he small{color:var(--muted);font-size:10.5px}
#strip{height:170px}
.strip .mk{transform-box:fill-box;transform-origin:center;transition:transform .25s var(--spring)}
.strip .mk:hover,.strip .mk.on{transform:scale(1.15)}
#sro{height:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sro.live{color:var(--ink);font-size:11.5px}
.pair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);margin-top:auto;padding-top:16px;border-top:1px solid var(--grid);height:150px}
.st{position:relative;padding:0 16px 0 0;min-width:0;overflow:hidden}
.st+.st{padding:0 0 0 16px;border-left:1px solid var(--grid)}
.mini{display:block;margin-top:6px}
.mini rect{fill:var(--muted);transition:fill .15s}
.mini rect:hover,.mini rect.on{fill:var(--ink2)}
.ai{display:inline-grid;place-items:center;width:14px;height:14px;border-radius:50%;background:var(--warning);color:#000000;font:800 9.5px system-ui,sans-serif;font-style:normal;margin-right:6px;vertical-align:-2px}
.lr{border-bottom:1px dotted var(--muted)}.lr:hover{color:var(--ink)}
.num{display:inline-block}
.draw{stroke-dasharray:1;stroke-dashoffset:0;animation:draw .5s var(--ease-out) both}.draw.late{animation-delay:.35s}
.fill{animation:fade .3s ease-out .35s both}.fill.late{animation-delay:.6s}
@keyframes draw{from{stroke-dashoffset:1}}
@keyframes fade{from{opacity:0}}
@keyframes grow{from{transform:scaleX(0)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition-duration:.01s!important}}
</style>
<div class=panel id=panel>
 <div class=top id=top>
  <div class=lead><div class=hero id=hero>–</div><div class="status neutral" id=status><i>–</i>carregando…</div><div class=cap id=cap></div></div>
  <div class=tiles id=tiles>
   <div class=tile id=now><div class=l>Agora</div><div class=v><span class=num></span><span class=u>/min</span></div><svg class=spark width=80 height=24></svg><div class=s></div></div>
   <div class=tile id=lasts><div class=l>Dura nesse ritmo</div><div class=v></div><div class=s></div></div>
   <div class=tile id=usable><div class=l>Contas usáveis</div><div class=v></div><div class=s></div></div>
  </div>
  <span class=x id=close title="Fechar">✕</span>
 </div>
 <div class=body id=bodyw>
  <div class=h id=chead><div class=ht>Consumo <em id=cro></em></div><span class=seg id=seg><b data-r=1h>1 h</b><b data-r=6h>6 h</b><b data-r=12h>12 h</b><b data-r=1d>1 dia</b><b data-r=7d class=on>7 dias</b><b data-r=15d>15 dias</b><b data-r=30d>30 dias</b></span></div>
  <div id=chart></div>
  <div class=cols>
   <div><div class=h id=mhead>Por modelo <span id=mro></span></div><div id=models></div>
    <div class=h id=hhead>Por projeto <span id=hro></span></div><div id=heat></div></div>
   <div><div class=h id=shead>Próximas 72 horas <span>o que volta</span></div><div id=strip></div>
    <div class=pair id=pair><div class=st id=swst></div><div class=st id=lost></div></div></div>
  </div>
 </div>
</div>
<script>
const $=id=>document.getElementById(id);
const WD=['dom','seg','ter','qua','qui','sex','sáb'], DASH='—';
const PLABEL={'1h':'última hora','6h':'últimas 6 h','12h':'últimas 12 h','1d':'último dia','7d':'últimos 7 dias','15d':'últimos 15 dias','30d':'últimos 30 dias'};
const SPAN={'1h':3600,'6h':21600,'12h':43200,'1d':86400,'7d':604800,'15d':1296000,'30d':2592000};
const TICK={'1h':900,'6h':3600,'12h':10800,'1d':21600,'7d':86400,'15d':259200,'30d':432000};
// FP2 spent steps, strongest first; text inside a segment is black on the two light steps, white on the rest
const STEPS=['#1BB5B8','#0A9396','#087A7D','#06686A','#055557','#044345'], LIGHT=new Set([0,1]);
const MODEL_ROWS=6, HEAT_ROWS=6, FUEL_GAP=110;
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>typeof v==='number'&&isFinite(v);
const k=v=>{if(!num(v))return DASH;v=Math.max(0,v);return v>=999500?(v/1e6).toFixed(v>=9.95e6?0:1).replace('.',',')+'M':v>=1e3?Math.round(v/1e3)+'K':String(Math.round(v));};
const dec1=v=>v.toFixed(1).replace('.',',');
const pad=n=>String(n).padStart(2,'0');
const ts=iso=>Date.parse(iso)/1000;
const hhmm=d=>pad(d.getHours())+':'+pad(d.getMinutes());
const ddmm=d=>pad(d.getDate())+'/'+pad(d.getMonth()+1);
const when=t=>{const d=new Date(t*1000);return `${WD[d.getDay()]} ${ddmm(d)} ${hhmm(d)}`;};
const share=s=>!num(s)||s<=0?'':s<1?'<1%':Math.round(s)+'%';
const usd=v=>v.toFixed(2).replace('.',',');
function setNum(el,txt){el.textContent=txt;}
function hover(el,enter,leave){el.addEventListener('mouseenter',enter);el.addEventListener('mouseleave',leave);}
function niceMax(v){if(!(v>0))return 1;const e=Math.pow(10,Math.floor(Math.log10(v))),f=v/e;return (f<=1?1:f<=2?2:f<=5?5:10)*e;}

let P=null, PERIOD='7d', seq=0;
function empty(){const now=new Date().toISOString().slice(0,19)+'Z';
 return {period:PERIOD,pace:null,status:{word:'carregando…',level:'neutral'},caption:'',
  tiles:{nowPerMin:null,nowSpark24h:[],ratio:null,lastsHours:null,usableNowTokens:null,usable:null,accounts:null},
  series:{start:now,step:300,now:now,past:[],future:[],sustainable:0,fuel:[]},models:[],projects:{columns:[],rows:[]},
  events72h:[],switches:{count:0,today:0,bars:[],logSince:null},unused:{usd:0,tokens:0,items:[]},loading:true};}

// ---- top: hero, status, caption, tiles (FP4) ----
function renderTop(p){
 $('hero').textContent=num(p.pace)?p.pace+'%':'–';
 const lv=(p.status&&p.status.level)||'neutral', st=$('status');
 st.className='status '+lv;
 st.innerHTML=`<i>${lv==='critical'?'!':lv==='warning'?'↓':num(p.pace)?'✓':'–'}</i>${esc(p.status&&p.status.word)}`;
 $('cap').textContent=p.caption||'';
 const t=p.tiles||{}, nowEl=$('now');
 nowEl.querySelector('.num').textContent=k(t.nowPerMin);
 const s0=p.loading?'':num(t.ratio)?dec1(t.ratio)+'× o ritmo da semana':'sem ritmo da semana';
 nowEl.querySelector('.s').textContent=s0;
 spark(nowEl,t.nowSpark24h||[],k(t.nowPerMin),s0,ts(p.series.now));
 const L=t.lastsHours;
 $('lasts').querySelector('.v').innerHTML=!num(L)?DASH:L<48?Math.round(L)+'<span class=u>h</span>':dec1(L/24)+'<span class=u>d</span>';
 $('lasts').querySelector('.s').textContent=num(t.usableNowTokens)?`com ${k(t.usableNowTokens)} tokens usáveis`:'';
 $('usable').querySelector('.v').innerHTML=num(t.usable)?`${t.usable}<span class=u>de ${esc(t.accounts)}</span>`:DASH;
 $('usable').querySelector('.s').textContent=!num(t.usable)?'':t.usable===t.accounts?'todas com folga':`${t.accounts-t.usable} sem folga`;
}
// The 24-hour sparkline of "Agora": scrubbing it shows that hour in place of the tile's number
function spark(tile,v,n0,s0,now){
 const svg=tile.querySelector('.spark'),W=80,H=24,n=v.length;
 if(!n){svg.innerHTML='';svg.onmousemove=svg.onmouseleave=null;return;}
 const mx=Math.max(1,...v),x=i=>i*W/Math.max(1,n-1),y=a=>H-2-(a/mx)*(H-4);
 svg.innerHTML=`<path d="${v.map((a,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(a).toFixed(1)).join(' ')}" fill=none stroke="var(--spent)" stroke-width=1.5 stroke-linejoin=round stroke-linecap=round /><circle class=cur cx=${x(n-1)} cy=${y(v[n-1])} r=2.5 fill="var(--spent)" />`;
 const cur=svg.querySelector('.cur');
 svg.onmousemove=e=>{const r=svg.getBoundingClientRect(),i=Math.max(0,Math.min(n-1,Math.round((e.clientX-r.left)/r.width*(n-1)))),d=new Date((now-(n-i)*3600)*1000);
  tile.querySelector('.num').textContent=k(v[i]);tile.querySelector('.s').textContent=`${WD[d.getDay()]} ${pad(d.getHours())}h`;
  cur.setAttribute('cx',x(i));cur.setAttribute('cy',y(v[i]));};
 svg.onmouseleave=()=>{tile.querySelector('.num').textContent=n0;tile.querySelector('.s').textContent=s0;cur.setAttribute('cx',x(n-1));cur.setAttribute('cy',y(v[n-1]));};
}

// ---- consumption chart (FP5): past in the spent colour, what can be spent ahead in the available colour ----
const CW=672, CH=190, CL=40, CT=22;
function chart(p){
 const s=p.series, step=s.step||300, past=s.past||[], fut=s.future||[], now=ts(s.now), t0=ts(s.start), t1=now+Math.max(fut.length,1)*step;
 const R=CW-4, B=CH-20, x=t=>CL+(t-t0)/(t1-t0)*(R-CL);
 const mx=niceMax(Math.max(1,...past,...fut,s.sustainable||0)*1.1), y=v=>B-(Math.max(0,v)/mx)*(B-CT);
 const pp=past.map((v,i)=>[t0+(i+0.5)*step,v]), fp=fut.map((v,i)=>[now+i*step,v]);
 if(fp.length)fp.push([t1,fut[fut.length-1]]);
 const path=pts=>pts.map((q,i)=>(i?'L':'M')+x(q[0]).toFixed(1)+' '+y(q[1]).toFixed(1)).join(' ');
 const some=past.length||fut.length; // no data yet: the grid without numbers
 const grid=[0,mx/2,mx].map(v=>`<line x1=${CL} x2=${R} y1=${y(v)} y2=${y(v)} stroke="var(--grid)" /><text x=${CL-8} y=${y(v)+3.5} text-anchor=end font-size=10.5 fill="var(--muted)">${some?k(v):''}</text>`).join('');
 const nx=x(now);
 let g=grid;
 if(fp.length)g+=`<rect x=${nx} y=${CT-4} width=${R-nx} height=${B-CT+4} fill="var(--avail)" opacity=.04 />`;
 if(pp.length){const d=path(pp);g+=`<path class=fill d="${d} L${x(pp[pp.length-1][0])} ${B} L${x(pp[0][0])} ${B} Z" fill="var(--spent)" opacity=.12 /><path class=draw pathLength=1 d="${d}" fill=none stroke="var(--spent)" stroke-width=2 stroke-linejoin=round stroke-linecap=round />`;}
 if(fp.length){const d=path(fp);g+=`<path class="fill late" d="${d} L${x(t1)} ${B} L${nx} ${B} Z" fill="var(--avail)" opacity=.1 /><path class="draw late" pathLength=1 d="${d}" fill=none stroke="var(--avail)" stroke-width=2 stroke-linejoin=round stroke-linecap=round />`;}
 // the rate that closes the week, now (dashed: a reference, not data)
 if(num(s.sustainable)&&s.sustainable>0){const Y=y(s.sustainable);
  g+=`<line x1=${CL} x2=${R} y1=${Y} y2=${Y} stroke="var(--ink2)" stroke-width=1 stroke-dasharray="4 4" opacity=.7 /><text x=${CL+6} y=${Y-5} font-size=10.5 fill="var(--ink2)">ritmo que fecha a semana · ${k(s.sustainable)}/min</text>`;}
 // fuel labels: what can be spent up to +24 h, +3 days, +7 days; one that would overlap the previous is dropped
 let last=1e9;
 for(const f of [...(s.fuel||[])].sort((a,b)=>b.hours-a.hours)){const t=now+f.hours*3600;if(t>t1+1)continue;const X=x(t);if(last-X<FUEL_GAP)continue;last=X;
  g+=`<line x1=${X} x2=${X} y1=${CT-4} y2=${B} stroke="var(--avail)" opacity=.35 /><text x=${X} y=${CT-8} text-anchor=${X>R-30?'end':'middle'} font-size=10.5 fill="var(--ink2)"><tspan font-weight=600 fill="var(--ink)">${k(f.tokens)}</tspan> até ${esc(f.label)}</text>`;}
 // time ticks: hours up to 1 day, then local days
 const every=TICK[p.period]||86400;
 let first=Math.ceil(t0/every)*every;
 if(every>=86400){const d=new Date(t0*1000);d.setHours(24,0,0,0);first=d.getTime()/1000;}
 for(let t=first;t<=t1;t+=every){
  let tt=t;if(every>=86400){const d=new Date(t*1000);d.setHours(0,0,0,0);tt=d.getTime()/1000;}
  const X=x(tt);if(X<CL||X>R||Math.abs(X-nx)<24)continue;const d=new Date(tt*1000);
  g+=`<text x=${X} y=${CH-5} text-anchor=${X>R-18?'end':'middle'} font-size=10.5 fill="var(--muted)">${every<86400?hhmm(d):p.period==='7d'?WD[d.getDay()]:ddmm(d)}</text>`;}
 g+=`<line x1=${nx} x2=${nx} y1=${CT-4} y2=${B} stroke="var(--ink2)" /><text x=${nx} y=${CH-5} text-anchor=middle font-size=10.5 font-weight=600 fill="var(--ink)">agora</text>`;
 g+=`<line class=xh x1=0 x2=0 y1=${CT} y2=${B} stroke="var(--ink2)" opacity=0 /><circle class=dot r=4.5 cx=-10 cy=-10 stroke="#000000" stroke-width=2 fill="var(--spent)" />`;
 $('chart').innerHTML=`<svg id=ac width=${CW} height=${CH}>${g}</svg><div class=lg><span><i style="background:var(--spent)"></i>consumo</span><span><i style="background:var(--avail)"></i>quanto dá para gastar por minuto, até cada reset</span><span><i class=dash></i>ritmo que fecha a semana</span></div>`;
 const svg=$('ac'), xh=svg.querySelector('.xh'), dot=svg.querySelector('.dot');
 // hover: a cursor and a dot follow the pointer; the reading goes next to the title (no floating box)
 svg.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect(),px=Math.max(CL,Math.min(R,e.clientX-r.left)),t=t0+(px-CL)/(R-CL)*(t1-t0);
  const isPast=t<=now; let v;
  if(isPast){if(!past.length)return;v=past[Math.min(past.length-1,Math.max(0,Math.floor((t-t0)/step)))];}
  else{if(!fut.length)return;v=fut[Math.min(fut.length-1,Math.max(0,Math.floor((t-now)/step)))];}
  xh.setAttribute('x1',px);xh.setAttribute('x2',px);xh.setAttribute('opacity',1);
  dot.setAttribute('cx',px);dot.setAttribute('cy',y(v));dot.setAttribute('fill',isPast?'var(--spent)':'var(--avail)');
  $('cro').innerHTML=`<i style="background:var(${isPast?'--spent':'--avail'})"></i>${when(t)} · ${isPast?'consumo':'dá para gastar'} <b>${k(v)}/min</b>`;});
 svg.addEventListener('mouseleave',()=>{$('cro').innerHTML='';xh.setAttribute('opacity',0);dot.setAttribute('cx',-10);});
}

// ---- by model (FP6): segmented bar and list; the hovered model grows to at least 30% of the bar ----
let segs=[], bar=null;
function focusModel(i){if(!bar||!segs.length)return;
 const si=i==null?0:+segs[i].dataset.s, ti=Math.max(si,0.30);
 segs.forEach((g,j)=>{const sj=+g.dataset.s;g.style.flexGrow=i==null?Math.max(sj,0.004):j===i?ti:Math.max(sj*(1-ti)/Math.max(1e-6,1-si),0.004);g.classList.toggle('on',j===i);});
 document.querySelectorAll('#models .ml').forEach(r=>r.classList.toggle('on',i!=null&&+r.dataset.i===i));}
// FP6: six models or fewer, one row each; more, the top five and "outros (n)"
function modelRows(models){
 let rows=(models||[]).map(m=>({label:m.label,tokens:m.tokens,share:m.share}));
 if(rows.length>MODEL_ROWS){const rest=rows.slice(MODEL_ROWS-1);rows=rows.slice(0,MODEL_ROWS-1).concat([{label:`outros (${rest.length})`,tokens:rest.reduce((a,m)=>a+m.tokens,0),share:rest.reduce((a,m)=>a+m.share,0)}]);}
 return rows;
}
function models(p){
 const rows=modelRows(p.models);
 const col=i=>STEPS[Math.min(i,STEPS.length-1)];
 const segHtml=rows.length?rows.map((m,i)=>`<i data-i=${i} data-s=${m.share/100} style="--d:${i*60}ms;flex:${Math.max(m.share/100,0.004)};background:${col(i)}"><em style="color:${LIGHT.has(i)?'#000000':'#FFFFFF'}">${share(m.share)}</em></i>`).join('')
  :`<i class=empty style="flex:1;background:var(--raised)"><em style="color:var(--muted)">sem uso</em></i>`;
 const list=rows.map((m,i)=>`<div class=ml data-i=${i}><span class=sw style="background:${col(i)}"></span><span class=mn>${esc(m.label)}</span><span class=mp>${share(m.share)}</span><span class=mt>${k(m.tokens)}</span></div>`).join('')
  +'<div class="ml blank"><span class=sw></span><span class=mn>&nbsp;</span></div>'.repeat(Math.max(0,MODEL_ROWS-rows.length));
 const box=$('models');box.innerHTML=`<div class=segbar>${segHtml}</div>${list}`;
 $('mro').textContent=PLABEL[p.period]||'';
 segs=[...box.querySelectorAll('.segbar i[data-i]')];bar=box.querySelector('.segbar');
 box.querySelectorAll('.ml[data-i],.segbar i[data-i]').forEach(e=>hover(e,()=>focusModel(+e.dataset.i),()=>focusModel(null)));
}

// ---- by project (FP7): heat map, top 5 and "outros (n)"; a cell's hover writes its value and date ----
function heat(p){
 const pr=p.projects||{columns:[],rows:[]}, cols=(pr.columns||[]).map(ts), nc=Math.max(cols.length,1);
 const W=316,NM=96,END=62,gap=nc>20?1:2,cw=Math.max(1,(W-NM-END-8-gap*(nc-1))/nc);
 const dayCols=cols.length>1?cols[1]-cols[0]>=82800:false;
 const lab=j=>{const d=new Date(cols[j]*1000);return dayCols?ddmm(d):hhmm(d);};
 // a column label is drawn only where it has room: every other day when the columns are narrow, none at the very end
 const head=j=>{if(!cols.length||(nc-j)*(cw+gap)<26)return '';const d=new Date(cols[j]*1000);
  if(dayCols)return nc<=8?(cw>=22||j%2===0?WD[d.getDay()]:''):(j%(nc>20?7:4)===0?ddmm(d):'');return j%(nc>12?6:3)===0?hhmm(d):'';};
 const rows=(pr.rows||[]).slice(0,HEAT_ROWS);while(rows.length<HEAT_ROWS)rows.push({key:'',cells:[],tokens:0,share:0});
 const mx=Math.max(1,...rows.flatMap(r=>r.cells||[]));
 const shade=v=>!(v>0)?'var(--raised)':`rgba(10,147,150,${(0.16+0.84*Math.pow(v/mx,0.55)).toFixed(2)})`;
 $('hro').textContent=`tokens · ${PLABEL[p.period]||''}`;
 let h=`<div class=hrow style="height:14px"><span class=hn></span><span class=hc style="gap:${gap}px">${cols.map((_,j)=>`<i class=hd style="width:${cw}px">${head(j)}</i>`).join('')}</span><span class=he></span></div>`;
 h+=rows.map((r,ri)=>{const cells=cols.map((_,j)=>(r.cells||[])[j]||0);
  return `<div class=hrow data-r=${ri}><span class=hn>${esc(r.key)}</span><span class=hc style="gap:${gap}px">${cells.map((v,j)=>`<i data-j=${j} data-v=${v} style="width:${cw}px;background:${shade(v)}"></i>`).join('')}</span><span class=he>${r.key?`<b>${k(r.tokens)}</b><small>${share(r.share)||'0%'}</small>`:''}</span></div>`;}).join('');
 $('heat').innerHTML=h;
 document.querySelectorAll('#heat .hrow[data-r]').forEach(row=>{const he=row.querySelector('.he'),keep=he.innerHTML;
  row.querySelectorAll('.hc i').forEach(c=>hover(c,()=>{he.innerHTML=`<b>${k(+c.dataset.v)}</b><small>${lab(+c.dataset.j)}</small>`;c.classList.add('on');},
   ()=>{he.innerHTML=keep;c.classList.remove('on');}));});
}

// ---- next 72 hours (FP8): circle = a 5-hour window returns, diamond = a weekly reset ----
function strip(p){
 const W=316,L=6,R=W-6,span=72*3600,now=ts(p.series.now),x=t=>L+(t-now)/span*(R-L),placed=[];
 let g='';
 (p.events72h||[]).forEach((e,i)=>{const t=ts(e.at),X=x(t),left=/sobra (\d+)%/.exec(e.text||''),extra=e.kind==='week'&&left?' sobra '+left[1]+'%':'';
  const wl=extra?90:30,flip=X>R-90,a0=flip?X-wl-10:X-8,a1=flip?X+8:X+wl+10;
  let lvl=0;while(lvl<3&&placed.some(q=>q.lvl===lvl&&a0<q.a1&&q.a0<a1))lvl++;placed.push({a0,a1,lvl});
  const Y=104-lvl*26;
  g+=`<line x1=${X} x2=${X} y1=${Y+7} y2=124 stroke="var(--grid)" />`;
  g+=e.kind==='five'?`<circle class=mk data-i=${i} cx=${X} cy=${Y} r=6 fill="#000000" stroke="var(--spent)" stroke-width=2 />`
   :`<path class=mk data-i=${i} d="M${X} ${Y-7} L${X+7} ${Y} L${X} ${Y+7} L${X-7} ${Y} Z" fill="var(--avail)" />`;
  g+=`<text x=${flip?X-11:X+11} y=${Y+4} text-anchor=${flip?'end':'start'} font-size=12 fill="var(--ink2)"><tspan font-weight=600 fill="var(--ink)">#${esc(e.account)}</tspan>${extra}</text>`;});
 if(!(p.events72h||[]).length&&!p.loading)g+=`<text x=${W/2} y=70 text-anchor=middle font-size=11.5 fill="var(--muted)">nada volta nas próximas 72 h</text>`;
 g+=`<line x1=${L} x2=${R} y1=124 y2=124 stroke="var(--muted)" opacity=.5 />`;
 for(const hh of [0,24,48,72]){const X=x(now+hh*3600);g+=`<text x=${X} y=142 text-anchor=${hh===0?'start':hh===72?'end':'middle'} font-size=10.5 fill="var(--muted)">${hh===0?'agora':'+'+hh+' h'}</text>`;}
 const LEG='<span><i class=r5></i>janela de 5 h volta</span><span><i class=d7></i>semana renova</span>';
 $('strip').innerHTML=`<svg class=strip width=${W} height=148>${g}</svg><div class=lg id=sro>${LEG}</div>`;
 const sro=$('sro');
 $('strip').querySelectorAll('.mk').forEach(m=>hover(m,()=>{sro.textContent=p.events72h[+m.dataset.i].text;sro.classList.add('live');},()=>{sro.innerHTML=LEG;sro.classList.remove('live');}));
}

// ---- switches and unused quota (FP9) ----
function stats(p){
 const sw=p.switches||{count:0,today:0,bars:[]}, since=sw.logSince?ts(sw.logSince):null, start=ts(p.series.now)-(SPAN[p.period]||0);
 const sub=`${PLABEL[p.period]||''} · ${sw.today||0} hoje${since&&since>start?' · desde '+ddmm(new Date(since*1000)):''}`;
 const bars=sw.bars||[], nb=Math.max(bars.length,1), mx=Math.max(1,...bars.map(b=>b.count)), gap=nb>20?1:2, w=(120-gap*(nb-1))/nb;
 const short=(SPAN[p.period]||0)<=86400, lab=b=>{const d=new Date(ts(b.start)*1000);return short?hhmm(d):ddmm(d);};
 const s=$('swst');
 s.innerHTML=`<div class=l>Trocas de conta</div><div class=v><span class=num>${sw.count||0}</span></div><div class=s>${esc(sub)}</div>`
  +`<svg width=120 height=30 class=mini>${bars.map((b,j)=>{const h=Math.max(2,26*b.count/mx);return `<rect data-j=${j} x=${(j*(w+gap)).toFixed(1)} y=${28-h} width=${w.toFixed(1)} height=${h} rx=${nb>20?1:3} />`;}).join('')}</svg>`;
 s.querySelectorAll('rect').forEach(rc=>{const b=bars[+rc.dataset.j];hover(rc,()=>{setNum(s.querySelector('.num'),b.count);s.querySelector('.s').textContent=`em ${lab(b)}`;rc.classList.add('on');},
  ()=>{setNum(s.querySelector('.num'),sw.count||0);s.querySelector('.s').textContent=sub;rc.classList.remove('on');});});
 const u=p.unused||{usd:0,tokens:0,items:[]}, items=u.items||[];
 const lsub=items.length?`${k(u.tokens)} tokens`:`nada venceu · ${PLABEL[p.period]||''}`;
 const ls=$('lost');
 ls.innerHTML=`<div class=l>${u.usd>0?'<i class=ai>!</i>':''}Venceu sem uso</div><div class=v><span class=u style="margin:0 3px 0 0">US$</span><span class=num>${usd(u.usd||0)}</span></div>`
  +`<div class=s><span class=sub>${esc(lsub)}</span>${items.length?' · ':''}${items.map((x,i)=>`<span class=lr data-i=${i}>#${esc(x.account)} ${x.pct}%${x.noReading?' · sem leitura':''}</span>`).join(', ')}</div>`;
 ls.querySelectorAll('.lr').forEach(el=>{const x=items[+el.dataset.i],d=new Date(ts(x.at)*1000);
  hover(el,()=>{setNum(ls.querySelector('.num'),num(x.usd)?usd(x.usd):DASH);ls.querySelector('.sub').textContent=`conta ${x.account} · ${x.pct}% em ${ddmm(d)}${num(x.tokens)?' · '+k(x.tokens)+' tokens':''}`;},
   ()=>{setNum(ls.querySelector('.num'),usd(u.usd||0));ls.querySelector('.sub').textContent=lsub;});});
}

function render(p){renderTop(p);chart(p);models(p);heat(p);strip(p);stats(p);}
function markSeg(r){for(const b of document.querySelectorAll('#seg b'))b.classList.toggle('on',b.dataset.r===r);}
// A period change keeps the frame: the old render stays at lower opacity until the new payload arrives
function load(r){
 PERIOD=SPAN[r]?r:'7d';markSeg(PERIOD);
 const mine=++seq;document.body.classList.add('stale');
 if(!window.pywebview||!pywebview.api)return;
 pywebview.api.get(PERIOD).then(p=>{if(mine!==seq)return;document.body.classList.remove('stale');
  if(!p||p.loading||p.error){const e=empty();if(p&&p.error)e.status.word='sem dados agora';render(e);return;}
  P=p;render(p);}).catch(()=>{if(mine===seq)document.body.classList.remove('stale');});
}
for(const b of document.querySelectorAll('#seg b'))b.onclick=()=>load(b.dataset.r);
$('close').onclick=()=>{if(window.pywebview&&pywebview.api)pywebview.api.close();};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&window.pywebview&&pywebview.api)pywebview.api.close();});
// The tray calls these: on each open (7 dias again) and after the history update that an open starts
window.fleetOpen=()=>load('7d');
window.fleetReload=()=>load(PERIOD);
// S11: the [top, height] of each section; hover and a period change must leave it unchanged
window.__sections=()=>['top','tiles','chead','chart','mhead','models','hhead','heat','shead','strip','pair','swst','lost']
 .map(id=>{const r=$(id).getBoundingClientRect();return [Math.round(r.top),Math.round(r.height)];});
render(empty());
window.addEventListener('pywebviewready',()=>load(PERIOD));
</script>"""
