"""The screen-edge usage pill (Linux), a design after vinzdg/codenotch (MIT).

A small pill pinned to one screen edge shows a ring per account; hovering a ring
opens a card with the account's 5-hour and weekly windows. Clicking a ring
switches to that account; the "+" logs in to a new one without switching.

Wayland has no way for an app to pin itself to an edge, so the tray runs on
XWayland (``GDK_BACKEND=x11``) where a frameless, transparent, always-on-top window
can be placed exactly. Geometry, the SVG outline and the payloads handed to the
pages are pure functions (unit-tested headless); the ``PillController`` at the
bottom is the only part that touches pywebview, imported lazily.
"""

from __future__ import annotations

import json
import threading

EDGES = ("right", "left", "top", "bottom")
THEMES = ("black", "white")
DEFAULT_EDGE, DEFAULT_THEME = "right", "black"

THICKNESS = 68  # pill depth, perpendicular to its edge
FLARE = 14  # radius of the concave corners where the pill leaves the edge
CORNER = 18  # radius of the pill's own convex corners
PAD = 8  # space between the flares and the first/last item
ITEM = 66  # one account along the edge: ring + percentage
ADD = 36  # the "+" button
CARD_W, ARROW, GAP = 268, 12, 6  # hover card width, its pointer, gap to the pill


def normalize_edge(value) -> str:
    return value if value in EDGES else DEFAULT_EDGE


def normalize_theme(value) -> str:
    return value if value in THEMES else DEFAULT_THEME


def is_vertical(edge: str) -> bool:
    """Pill stands on the left/right edge (rings stacked top to bottom)."""
    return edge in ("left", "right")


def pill_length(n_accounts: int) -> int:
    return 2 * (FLARE + PAD) + n_accounts * ITEM + ADD


def pill_size(edge: str, n_accounts: int) -> tuple[int, int]:
    length = pill_length(n_accounts)
    return (THICKNESS, length) if is_vertical(edge) else (length, THICKNESS)


def item_center(index: int) -> float:
    """Distance of account ``index``'s ring centre from the pill's start, along the edge."""
    return FLARE + PAD + index * ITEM + ITEM / 2


def pill_path(edge: str, length: int, thickness: int = THICKNESS) -> str:
    """SVG outline of the pill, in the pill window's own coordinates.

    Drawn once for a pill hugging the right edge (x grows away from the screen,
    y runs along it), then mapped onto the other edges. Starting at the screen
    edge it curves away tangentially (the concave "flare"), turns the convex
    corner, runs along the face, and mirrors that at the far end.
    """
    f, c, t, n = FLARE, CORNER, thickness, length
    # Points in "d, s" space: d = depth from the screen edge, s = position along it.
    segs = [
        ("M", (0, 0)),
        ("A", f, f, 0, (f, f)),
        ("L", (t - c, f)),
        ("A", c, c, 1, (t, f + c)),
        ("L", (t, n - f - c)),
        ("A", c, c, 1, (t - c, n - f)),
        ("L", (f, n - f)),
        ("A", f, f, 0, (0, n)),
        ("Z",),
    ]

    # Right: X = t - d, Y = s. Left: X = d, Y = s. Top: X = s, Y = d. Bottom: X = s, Y = t - d.
    # A reflection (negative determinant: right and top) reverses arc direction; a pure
    # rotation or identity (left, bottom) keeps it.
    def pt(d, s):
        if edge == "right":
            return t - d, s
        if edge == "left":
            return d, s
        if edge == "top":
            return s, d
        return s, t - d

    flips = edge in ("right", "top")
    out = []
    for seg in segs:
        if seg[0] == "M":
            out.append("M%g %g" % pt(*seg[1]))
        elif seg[0] == "L":
            out.append("L%g %g" % pt(*seg[1]))
        elif seg[0] == "A":
            _, rx, ry, sweep, end = seg
            out.append("A%g %g 0 0 %d %g %g" % (rx, ry, (1 - sweep) if flips else sweep, *pt(*end)))
        else:
            out.append("Z")
    return " ".join(out)


def pill_position(edge: str, size: tuple[int, int], area: tuple[int, int, int, int]) -> tuple[int, int]:
    """Top-left of the pill: against ``edge`` of the work area, centred along it."""
    w, h = size
    ax, ay, aw, ah = area
    if edge == "right":
        return ax + aw - w, ay + (ah - h) // 2
    if edge == "left":
        return ax, ay + (ah - h) // 2
    if edge == "top":
        return ax + (aw - w) // 2, ay
    return ax + (aw - w) // 2, ay + ah - h


def card_height(n_rows: int) -> int:
    return 76 + max(1, n_rows) * 50


CARD_MAX_H = card_height(2) + 8  # the tallest card (5-hour + weekly rows), plus slack for its border/rounding


def window_layout(edge: str, n_accounts: int, area: tuple[int, int, int, int]) -> dict:
    """Geometry of the one window that holds both the pill and its hover card.

    Like Codenotch, the card lives inside the pill's own transparent window and the
    window's input region is cut down to the pill and card (see ``set_hot``), so the
    empty remainder never intercepts clicks. Returns the window's screen rectangle and
    the pill's rectangle inside it; the page places the card itself.
    """
    pw, ph = pill_size(edge, n_accounts)
    ax, ay, aw, ah = area
    room = GAP + ARROW  # pill -> card
    if is_vertical(edge):
        w, h = pw + room + CARD_W, max(ph, CARD_MAX_H)
        x = ax + aw - w if edge == "right" else ax
        centre = ay + ah / 2
        y = int(max(ay, min(centre - h / 2, ay + ah - h)))
        px = w - pw if edge == "right" else 0
        py = int(round(centre - y - ph / 2))
    else:
        w, h = max(pw, CARD_W), ph + room + CARD_MAX_H
        y = ay if edge == "top" else ay + ah - h
        centre = ax + aw / 2
        x = int(max(ax, min(centre - w / 2, ax + aw - w)))
        px = int(round(centre - x - pw / 2))
        py = 0 if edge == "top" else h - ph
    return {"win": [int(x), int(y), int(w), int(h)], "pill": [int(px), int(py), int(pw), int(ph)]}


# ---- payload for the page ------------------------------------------------------

def _row(card: dict, label: str) -> dict | None:
    return next((r for r in card.get("rows") or () if r.get("label") == label), None)


def _pct(row: dict | None) -> float | None:
    v = row.get("pct") if row else None
    return float(v) if isinstance(v, (int, float)) else None


def pill_payload(cards: list[dict], edge: str, theme: str, layout: dict, fold: bool = False) -> dict:
    """Everything the page needs to draw the pill and, on hover, each account's card."""
    pw, ph = pill_size(edge, len(cards))
    return {
        "edge": edge,
        "theme": theme,
        "vertical": is_vertical(edge),
        "path": pill_path(edge, pill_length(len(cards))),
        "size": [pw, ph],
        "layout": layout,
        "fold": bool(fold),
        "pad": FLARE + PAD,
        "item": ITEM,
        "card": {"w": CARD_W, "arrow": ARROW, "gap": GAP},
        "items": [
            {
                "num": c["num"],
                "active": bool(c.get("active")),
                "disabled": bool(c.get("disabled")),
                "five": _pct(_row(c, "5h")),
                "week": _pct(_row(c, "7d")),
                "title": c.get("title", ""),
                "tag": c.get("tag", ""),
                "message": c.get("message"),
                "rows": [
                    {"label": r["label"], "pct": r.get("pct"), "reset": r.get("reset"), "age": r.get("age")}
                    for r in c.get("rows") or ()
                ],
            }
            for c in cards
        ],
    }


# ---- page ----------------------------------------------------------------------

_THEME_CSS = """
:root{--bg:#000;--fg:#f2f0e3;--dim:#8d8d93;--track:#2c2c30;--line:transparent;
 --l0:#34d17a;--l1:#f5c542;--l2:#ff8a3d;--l3:#ff4d3d;--hover:#ffffff14}
:root[data-theme=white]{--bg:#f6f5f1;--fg:#16161a;--dim:#6d6d74;--track:#dedcd5;--line:#cfcdc5;
 --l0:#17a35a;--l1:#c99a0a;--l2:#e06a14;--l3:#d8321f;--hover:#00000010}
html,body{margin:0;height:100%;background:transparent;overflow:hidden;user-select:none;cursor:default;
 font:500 12px/1.2 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--fg)}
.l0{--c:var(--l0)}.l1{--c:var(--l1)}.l2{--c:var(--l2)}.l3{--c:var(--l3)}
"""

PILL_PAGE = """<!doctype html><meta charset=utf-8><style>""" + _THEME_CSS + """
/* Motion follows Codenotch: its ease (.32,.72,.24,1) and spring (.34,1.4,.64,1), a 28 ms stagger per ring. */
:root{--ease:cubic-bezier(.32,.72,.24,1);--spring:cubic-bezier(.34,1.4,.64,1);--rest-edge:#5c5c5c;--slide:10px 0}
:root[data-theme=white]{--rest-edge:#86868b}
body.edge-left{--slide:-10px 0}body.edge-top{--slide:0 -10px}body.edge-bottom{--slide:0 10px}
#root{position:relative;width:100vw;height:100vh}
#pill{position:absolute;clip-path:var(--clip-open);transition:clip-path .2s var(--ease),opacity .1s ease}
body{--clip-open:inset(0 0 0 0 round 0)}
body.edge-right{--clip-rest:inset(calc(50% - 39.5px) 0 calc(50% - 39.5px) calc(100% - 10px) round 6px 0 0 6px)}
body.edge-left{--clip-rest:inset(calc(50% - 39.5px) calc(100% - 10px) calc(50% - 39.5px) 0 round 0 6px 6px 0)}
body.edge-top{--clip-rest:inset(0 calc(50% - 39.5px) calc(100% - 10px) calc(50% - 39.5px) round 0 0 6px 6px)}
body.edge-bottom{--clip-rest:inset(calc(100% - 10px) calc(50% - 39.5px) 0 calc(50% - 39.5px) round 6px 6px 0 0)}
body.folded #pill{clip-path:var(--clip-rest);transition:clip-path .36s var(--ease),opacity .6s ease}
body.hidden #pill{opacity:0}
svg.shape{position:absolute;inset:0;width:100%;height:100%}
/* What is left of a folded pill: an outline, the only thing that shows black on a black wallpaper */
#rest{position:absolute;box-sizing:border-box;background:var(--bg);border:1px solid var(--rest-edge);pointer-events:none;
 opacity:0;transition:opacity .16s ease}
body.folded #rest{opacity:1;transition:opacity .2s ease .18s}
body.edge-right #rest{right:0;top:50%;translate:0 -50%;width:10px;height:79px;border-right:none;border-radius:6px 0 0 6px}
body.edge-left #rest{left:0;top:50%;translate:0 -50%;width:10px;height:79px;border-left:none;border-radius:0 6px 6px 0}
body.edge-top #rest{top:0;left:50%;translate:-50% 0;width:79px;height:10px;border-top:none;border-radius:0 0 6px 6px}
body.edge-bottom #rest{bottom:0;left:50%;translate:-50% 0;width:79px;height:10px;border-bottom:none;border-radius:6px 6px 0 0}
#items{position:absolute;display:flex;align-items:center}
.vertical #items{left:0;right:0;flex-direction:column}
.horizontal #items{top:0;bottom:0;flex-direction:row}
.item,.add{transition:opacity .12s ease calc(var(--i,0) * 14ms),translate .2s var(--ease) calc(var(--i,0) * 14ms),background .15s ease,color .15s ease}
body.folded .item,body.folded .add{transition:opacity .18s ease calc(var(--i,0) * 28ms),translate .3s var(--ease) calc(var(--i,0) * 28ms)}
#pill.entering .item,#pill.entering .add,body.folded .item,body.folded .add{opacity:0;translate:var(--slide)}
.item{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;border-radius:12px;
 cursor:pointer;position:relative}
.item:hover{background:var(--hover)}
.item .pct{font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}
.item.off{opacity:.45}
.item .ring{transition:transform .3s var(--spring)}
.item:hover .ring{transform:scale(1.07)}
.item.pressed .ring{transform:scale(.93)}
.ring .track{stroke:var(--track)}
.ring .arc{stroke:var(--c);stroke-linecap:round;transition:stroke-dasharray .6s var(--ease),stroke .3s ease}
.ring .num{fill:var(--fg);font:700 11px system-ui,sans-serif}
.ring .disc{transition:fill .3s ease}
.item.active .ring .disc{fill:var(--fg)}
.item.active .ring .num{fill:var(--bg)}
.add{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;
 color:var(--dim);font-size:20px;font-weight:400;cursor:pointer}
.add:hover{background:var(--hover);color:var(--fg)}
@keyframes spin{to{transform:rotate(360deg)}}
.add.busy{animation:spin 1.2s steps(12) infinite}
#card{position:absolute;visibility:hidden;opacity:0;background:var(--bg);border:1px solid var(--line);border-radius:16px;
 padding:14px 16px;box-sizing:border-box;transform:translate(var(--cx,0),var(--cy,0)) scale(.97);transform-origin:var(--origin,center);
 transition:opacity .16s ease,transform .3s var(--ease),visibility 0s linear .3s}
body.edge-right{--cx:8px;--origin:right center}body.edge-left{--cx:-8px;--origin:left center}
body.edge-top{--cy:-8px;--origin:center top}body.edge-bottom{--cy:8px;--origin:center bottom}
#card.show{visibility:visible;opacity:1;transform:none;transition:opacity .18s ease,transform .3s var(--ease),visibility 0s}
/* Moving from one ring to another glides the card rather than jumping it */
#card.show.glide{transition:opacity .18s ease,transform .3s var(--ease),top .28s var(--ease),left .28s var(--ease),visibility 0s}
.arrow{position:absolute;width:0;height:0;border:6px solid transparent;transition:top .28s var(--ease),left .28s var(--ease)}
.head{margin-bottom:10px;min-width:0}
.title{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tag{color:var(--dim);font-size:11px;white-space:nowrap}
.head .tag{margin-top:2px;overflow:hidden;text-overflow:ellipsis}
.row{margin-bottom:12px}.row:last-child{margin-bottom:0}
.top{display:flex;justify-content:space-between;color:var(--dim);font-size:11.5px;margin-bottom:5px}
.bar{height:6px;border-radius:3px;background:var(--track);overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px;background:var(--c);transition:width .5s var(--ease)}
.used{margin-top:4px;font-size:12px}
.msg{color:var(--dim);font-size:12px;padding:6px 0}
@media (prefers-reduced-motion: reduce){
 #pill,body.folded #pill,#rest,.item,.add,body.folded .item,body.folded .add,.item .ring,.ring .arc,.ring .disc,#card,#card.show,#card.show.glide,.arrow,.bar i{transition:none}
 .add.busy{animation:none}
}
</style>
<div id=root><div id=pill><svg class=shape><path id=body /></svg><div id=rest></div><div id=items></div></div><div id=card></div></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const R5=17,R7=11.5,C5=2*Math.PI*R5,C7=2*Math.PI*R7;
const lvl=p=>p==null?0:p<50?0:p<75?1:p<90?2:3;
const NAMES={'5h':'Current session','7d':'Weekly (all models)'};
const resetText=t=>!t?'':/^resets/i.test(t)?t[0].toUpperCase()+t.slice(1):'Resets '+t;
// Both arcs always exist (an empty one has no length), so a reading arriving or changing animates rather than appearing.
const dash=(c,p)=>`${p==null?0:c*Math.min(p,100)/100} ${c}`;
const arc=(r,c,p,w,k)=>`<circle class=track cx=21 cy=21 r=${r} fill=none stroke-width=${w}></circle>`+
 `<circle class="arc ${k}" cx=21 cy=21 r=${r} fill=none stroke-width=${w} stroke-dasharray="${dash(c,p)}" transform="rotate(-90 21 21)"></circle>`;
function ring(it){
 return `<svg class=ring width=42 height=42 viewBox="0 0 42 42">`+arc(R5,C5,it.five,4,'five')+arc(R7,C7,it.week,2.5,'week')+
  `<circle class=disc cx=21 cy=21 r=8.5 fill=none></circle><text class=num x=21 y=25 text-anchor=middle>${esc(it.num)}</text></svg>`;
}
const pill=document.getElementById('pill'), card=document.getElementById('card');
let P=null, hoverId=null, hideTimer=null, foldTimer=null, busyAdd=false, folded=false, pointerIn=false, builtKey=null;

/* Hover and collapse follow Codenotch (MIT, vinzdg/codenotch, windows/codenotch/ui/notch.html): the card
   stays open while the pointer is over the pill or the card, and collapses after a 250 ms grace.
   Three independent triggers, because no single one is reliable on every compositor:
   - pure geometry on each mousemove (pill rect, card rect, and the box around both),
   - mouseout with no relatedTarget (the pointer left the page),
   - on Linux, GTK enter/leave events on the window, relayed by the native side as codenotchPointer(). */
function rectOf(el){const r=el.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom};}
function inRect(x,y,r,pad){return x>=r.left-pad&&y>=r.top-pad&&x<r.right+pad&&y<r.bottom+pad;}
function cellAt(x,y){
 const cells=[...pill.querySelectorAll('.item')];
 for(let i=0;i<cells.length;i++){ if(inRect(x,y,rectOf(cells[i]),2)) return i; }
 return null;
}
// The card and pill as laid out, not as mid-animation (a scaled card measures smaller while it opens)
function cardRect(){return {left:+card.style.left.slice(0,-2)||0,top:+card.style.top.slice(0,-2)||0,right:(+card.style.left.slice(0,-2)||0)+card.offsetWidth,bottom:(+card.style.top.slice(0,-2)||0)+card.offsetHeight};}
function pointerInHot(x,y){
 const p=rectOf(pill);
 if(inRect(x,y,p,4))return true;
 if(!card.classList.contains('show'))return false;
 const c=cardRect();
 if(inRect(x,y,c,4))return true;
 return inRect(x,y,{left:Math.min(p.left,c.left),top:Math.min(p.top,c.top),right:Math.max(p.right,c.right),bottom:Math.max(p.bottom,c.bottom)},0);
}
function hideCard(){card.classList.remove('show','glide');hoverId=null;reportHot();}
function scheduleHide(){clearTimeout(hideTimer);hideTimer=setTimeout(hideCard,250);}
function showCard(i){
 clearTimeout(hideTimer);
 const wasOpen=card.classList.contains('show');
 card.classList.toggle('glide',wasOpen); // glide between rings; the first appearance just fades in place
 hoverId=i;
 renderCard();
 card.classList.add('show');
 reportHot();
}

function renderCard(){
 const it=P.items[hoverId]; if(!it){hideCard();return;}
 const rows=it.rows.map(r=>`<div class="row l${lvl(r.pct)}"><div class=top><span>${esc(NAMES[r.label]||r.label)}</span><span>${esc(resetText(r.reset))}</span></div>
  <div class=bar><i style="width:${Math.min(r.pct||0,100)}%"></i></div>
  <div class=used>${r.pct==null?'\\u2013':Math.round(r.pct)+'% used'}${r.age?' <span class=tag>'+esc(r.age)+'</span>':''}</div></div>`).join('');
 card.innerHTML=`<div class=head><div class=title>${esc(it.title)}</div><div class=tag>${esc(it.tag)}${it.disabled?' \\u00b7 paused':''}</div></div>`+
  rows+(it.message&&!it.rows.length?`<div class=msg>${esc(it.message)}</div>`:'')+'<div class=arrow></div>';
 placeCard();
}
// Show first, then place: the card's height is only known once it has content (Codenotch's placeCard).
function placeCard(){
 const [lx,ly,lw,lh]=P.layout.pill, [,,W,H]=P.layout.win, {w:cw,arrow:A,gap:G}=P.card;
 const ch=card.offsetHeight, v=P.vertical, i=hoverId, along=(P.pad+i*P.item+P.item/2);
 const clamp=(n,lo,hi)=>Math.max(lo,Math.min(n,hi));
 let x,y,ax;
 if(v){
  x=P.edge==='right'?lx-G-A-cw:lx+lw+G+A;
  y=clamp(ly+along-ch/2,0,H-ch); ax=ly+along-y;
 }else{
  y=P.edge==='top'?ly+lh+G+A:ly-G-A-ch;
  x=clamp(lx+along-cw/2,0,W-cw); ax=lx+along-x;
 }
 ax=clamp(ax,14,(v?ch:cw)-14);
 Object.assign(card.style,{left:x+'px',top:y+'px',width:cw+'px'});
 const ar=card.querySelector('.arrow'), col='var(--bg)';
 const sides={right:`right:-12px;top:${ax-6}px;border-left-color:${col}`,left:`left:-12px;top:${ax-6}px;border-right-color:${col}`,
  top:`top:-12px;left:${ax-6}px;border-bottom-color:${col}`,bottom:`bottom:-12px;left:${ax-6}px;border-top-color:${col}`};
 ar.style.cssText=sides[P.edge];
}
/* The window's input region is cut down to these rectangles (native side), so the rest of this
   transparent window never swallows a click. The tail bridges the gap between pill and card.
   Folded, only the resting sliver and a wake band in front of it take input (Codenotch's wakeRect). */
const WAKE_BAND=34;
function wakeRect(){
 const [lx,ly,lw,lh]=P.layout.pill, b=WAKE_BAND, L=79;
 if(P.edge==='right') return {left:lx+lw-10-b,top:ly+lh/2-L/2,right:lx+lw,bottom:ly+lh/2+L/2};
 if(P.edge==='left') return {left:lx,top:ly+lh/2-L/2,right:lx+10+b,bottom:ly+lh/2+L/2};
 if(P.edge==='top') return {left:lx+lw/2-L/2,top:ly,right:lx+lw/2+L/2,bottom:ly+10+b};
 return {left:lx+lw/2-L/2,top:ly+lh-10-b,right:lx+lw/2+L/2,bottom:ly+lh};
}
function reportHot(){
 if(!P||!window.pywebview||!pywebview.api)return;
 let rs;
 if(folded) rs=[wakeRect()];
 else{
  const p=rectOf(pill); rs=[p];
  if(card.classList.contains('show')){
   const c=cardRect();
   rs.push(c);
   // the gap between pill and card (and the card's pointer) must stay inside the input region too
   if(P.vertical) rs.push({left:Math.min(c.right,p.right),top:Math.max(p.top,c.top),right:Math.max(c.left,p.left),bottom:Math.min(p.bottom,c.bottom)});
   else rs.push({left:Math.max(p.left,c.left),top:Math.min(c.bottom,p.bottom),right:Math.min(p.right,c.right),bottom:Math.max(c.top,p.top)});
  }
 }
 pywebview.api.set_hot(rs.filter(r=>r.right>r.left&&r.bottom>r.top).map(r=>[r.left,r.top,r.right-r.left,r.bottom-r.top]));
}
card.addEventListener('transitionend',e=>{ if(e.target===card&&(e.propertyName==='transform'||e.propertyName==='top'||e.propertyName==='left')) reportHot(); });

/* Show on hover, as Codenotch: opens on contact, folds FOLD_GRACE after the pointer has gone. */
const FOLD_GRACE=450, SLIVER_HOLD=2000; // after folding, the sliver itself fades away SLIVER_HOLD later
let restTimer=null;
function foldAllowed(){return !!(P&&P.fold)&&!pointerIn;}
function setFolded(f){
 if(f===folded)return;
 folded=f;
 clearTimeout(restTimer);
 if(f){clearTimeout(hideTimer);hideCard();restTimer=setTimeout(()=>{if(folded)document.body.classList.add('hidden');},SLIVER_HOLD);}
 else document.body.classList.remove('hidden');
 document.body.classList.toggle('folded',f);
 reportHot();
}
function unfold(){clearTimeout(foldTimer);setFolded(false);}
function scheduleFold(){
 clearTimeout(foldTimer);
 if(!P||!P.fold){setFolded(false);return;}
 if(!foldAllowed())return;
 foldTimer=setTimeout(()=>{if(foldAllowed())setFolded(true);},FOLD_GRACE);
}

document.addEventListener('mousemove',e=>{
 if(!P)return;
 pointerIn=true; clearTimeout(foldTimer);
 if(folded) unfold();
 if(pointerInHot(e.clientX,e.clientY)){
  clearTimeout(hideTimer);
  const i=cellAt(e.clientX,e.clientY);
  if(i!==null&&i!==hoverId) showCard(i);
 }else if(card.classList.contains('show')) scheduleHide();
});
document.addEventListener('mouseout',e=>{
 if(!e.relatedTarget){ pointerIn=false; scheduleFold(); if(card.classList.contains('show')) scheduleHide(); }
});
// Called by the native GTK crossing events (which own their own 250 ms leave grace).
window.addEventListener('pywebviewready',reportHot); // the first reports can precede the bridge
window.codenotchPointer=inside=>{
 pointerIn=inside;
 if(inside){ clearTimeout(hideTimer); unfold(); }
 else{ clearTimeout(hideTimer); hideCard(); scheduleFold(); }
};

const items=document.getElementById('items');
function applyItem(d,it){
 d.className=d.className.replace(/\\b(l[0-3]|active|off)\\b/g,'').trim()+' l'+lvl(it.five)+(it.active?' active':'')+(it.disabled?' off':'');
 for(const [k,p,c] of [['five',it.five,C5],['week',it.week,C7]]){
  const el=d.querySelector('.arc.'+k); if(el) el.setAttribute('stroke-dasharray',dash(c,p));
 }
 d.querySelector('.pct').textContent=it.five==null?'\\u2013':Math.round(it.five)+'%';
}
// The structure is rebuilt only when it changes, so a refresh animates the arcs rather than replaying the entrance
function build(p){
 items.innerHTML='';
 p.items.forEach((it,i)=>{
  const d=document.createElement('div');
  d.className='item'; d.style.setProperty('--i',i);
  d.style.width=d.style.height=p.item+'px';
  d.innerHTML=ring(it)+'<span class=pct></span>';
  d.onclick=()=>{d.classList.add('pressed');pywebview.api.switch(it.num).then(()=>d.classList.remove('pressed'));};
  applyItem(d,it);
  items.appendChild(d);
 });
 const a=document.createElement('div');a.className='add'+(busyAdd?' busy':'');a.textContent='+';a.title='Add account (browser login)';
 a.style.setProperty('--i',p.items.length);
 a.onclick=()=>{if(busyAdd)return;busyAdd=true;a.classList.add('busy');
  pywebview.api.add().then(()=>{busyAdd=false;a.classList.remove('busy');});};
 items.appendChild(a);
 pill.classList.add('entering');
 requestAnimationFrame(()=>requestAnimationFrame(()=>pill.classList.remove('entering')));
}
function render(p){
 P=p;
 document.documentElement.dataset.theme=p.theme;
 document.body.classList.remove('vertical','horizontal','edge-right','edge-left','edge-top','edge-bottom');
 document.body.classList.add(p.vertical?'vertical':'horizontal','edge-'+p.edge);
 const [lx,ly,lw,lh]=p.layout.pill;
 Object.assign(pill.style,{left:lx+'px',top:ly+'px',width:lw+'px',height:lh+'px'});
 const svg=pill.querySelector('svg.shape');
 svg.setAttribute('viewBox','0 0 '+p.size[0]+' '+p.size[1]);
 const b=document.getElementById('body'); b.setAttribute('d',p.path);
 b.setAttribute('fill','var(--bg)'); b.setAttribute('stroke','var(--line)');
 const pad=p.pad+'px';
 if(p.vertical){items.style.top=pad;items.style.bottom=pad;items.style.left=items.style.right='0';items.style.flexDirection='column';}
 else{items.style.left=pad;items.style.right=pad;items.style.top=items.style.bottom='0';items.style.flexDirection='row';}
 const key=p.edge+'|'+p.items.map(i=>i.num).join(',');
 if(key!==builtKey){ build(p); builtKey=key; }
 else [...items.querySelectorAll('.item')].forEach((d,i)=>applyItem(d,p.items[i]));
 if(hoverId!==null){ if(hoverId<p.items.length) renderCard(); else hideCard(); }
 reportHot();
 scheduleFold();
}
</script>"""


# ---- the window --------------------------------------------------------------

def work_area() -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the primary monitor minus panels/docks, or None if unknown."""
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        r = monitor.get_workarea()
        return r.x, r.y, r.width, r.height
    except Exception:
        return None


class PillBridge:
    """What the page calls through ``pywebview.api``; thin, delegates to the controller."""

    def __init__(self, controller):
        self._c = controller

    def switch(self, num):
        return self._c.switch(num)

    def add(self):
        return self._c.add_account()

    def set_hot(self, rects):
        self._c.set_hot(rects)


class PillController:
    """Owns the pill window and keeps it in step with the account cards.

    ``api`` is the shared ``DesktopApi``; ``on_change`` asks the tray to refresh;
    ``notify(title, body)`` reports outcomes. ``settings`` is read live, so a
    changed edge/theme only needs ``relayout()``.
    """

    def __init__(self, webview, api, settings, on_change, notify):
        self._wv = webview
        self._api = api
        self.settings = settings
        self._on_change = on_change
        self._notify = notify
        self.cards: list[dict] = []
        self.window = None
        self._hot: list[list[float]] = []
        self._loaded = threading.Event()
        self._visible = False

    # ---- setup -----------------------------------------------------------
    def create(self) -> None:
        edge = normalize_edge(self.settings.pill_edge)
        lay = window_layout(edge, 0, (0, 0, 1600, 900))["win"]
        self.window = self._wv.create_window(
            "cswap pill", html=PILL_PAGE, js_api=PillBridge(self), width=lay[2], height=lay[3],
            frameless=True, transparent=True, on_top=True, focus=False, hidden=True,
            resizable=False, easy_drag=False, min_size=(8, 8))
        self.window.events.loaded += self._loaded.set

    def start(self) -> None:
        """Call from webview's ready callback (a worker thread): tune the window, show the pill."""
        _tune_x11(self.window)
        self._loaded.wait(10)
        self.set_visible(self.settings.pill_enabled)

    # ---- state -----------------------------------------------------------
    def set_visible(self, on: bool) -> None:
        self._visible = on
        if on:
            self.relayout()
        else:
            _gtk_call(self.window, lambda w: w.hide())

    def update(self, cards: list[dict]) -> None:
        """New account data from the tray's refresh loop."""
        resized = len(cards) != len(self.cards)
        self.cards = list(cards)
        if not self._visible:
            return
        self.relayout() if resized else self._push()

    def relayout(self) -> None:
        lay = self._layout()
        self._hot = []  # until the page reports again, only the pill takes input
        self._push()
        _place(self.window, lay["win"], lambda: self._hot or [lay["pill"]])

    def _layout(self) -> dict:
        edge = normalize_edge(self.settings.pill_edge)
        return window_layout(edge, len(self.cards), work_area() or self._screen_area())

    def _screen_area(self):
        s = self._wv.screens[0]
        return s.x, s.y, s.width, s.height

    def _push(self) -> None:
        if not self._loaded.is_set():
            return
        edge = normalize_edge(self.settings.pill_edge)
        payload = pill_payload(self.cards, edge, normalize_theme(self.settings.pill_theme), self._layout(),
                               fold=bool(getattr(self.settings, "pill_fold", False)))
        self._run_js("render(%s)" % json.dumps(payload))

    def _run_js(self, script: str) -> None:
        """Run JS in the page without waiting for it.

        ``Window.evaluate_js`` blocks until the page answers, and tray-menu callbacks can run on the
        GTK main thread (pystray shares its loop), which the answer needs: calling it from there
        deadlocked the whole app. This queues the call on the loop instead, so any thread may use it.
        """
        from gi.repository import GLib
        from webview.platforms import gtk as wgtk

        def run():
            view = wgtk.BrowserView.instances.get(self.window.uid)
            if view is not None:
                view.webview.run_javascript(script, None, None, None)
            return False

        GLib.idle_add(run)

    def set_hot(self, rects) -> None:
        """The page's clickable rectangles; the window's input region is cut down to them."""
        try:
            self._hot = [[float(n) for n in r] for r in rects]
        except (TypeError, ValueError):
            return
        _gtk_call(self.window, lambda w: _apply_input_region(w, self._hot))

    # ---- actions ---------------------------------------------------------
    def switch(self, num) -> dict:
        res = self._api.switch(num)
        if not res["ok"] and res["output"]:
            self._notify("cswap", res["output"])
        self._on_change()
        return {"ok": res["ok"]}

    def add_account(self) -> dict:
        self._notify("cswap", "Opening your browser to sign in. Your current account stays active.")
        res = self._api.add_login()
        self._notify("cswap", res["output"] or ("Account added." if res["ok"] else "Could not add the account."))
        self._on_change()
        return {"ok": res["ok"]}

    def destroy(self) -> None:
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass


def _gtk_call(window, fn) -> None:
    """Run ``fn(gtk_window)`` on the GTK main loop.

    pywebview's own show/resize wrappers wait on events a window created hidden never
    fires, so the pill drives its GTK window directly.
    """
    from gi.repository import GLib
    from webview.platforms import gtk as wgtk

    def run():
        view = wgtk.BrowserView.instances.get(window.uid)
        if view is not None:
            fn(view.window)
        return False

    GLib.idle_add(run)


def _apply_input_region(gtk_window, rects) -> None:
    """Make only ``rects`` (CSS px == logical px) receive input; clicks elsewhere fall through.

    Done through ctypes on the libraries GTK has already loaded: PyGObject can only hand a
    ``cairo.Region`` to GDK when the optional python3-gi-cairo bridge is installed.
    """
    import ctypes
    import math

    gdk_window = gtk_window.get_window()
    if gdk_window is None:
        return

    class Rect(ctypes.Structure):
        _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int)]

    cairo = ctypes.CDLL("libcairo.so.2")
    gdk = ctypes.CDLL("libgdk-3.so.0")
    cairo.cairo_region_create.restype = ctypes.c_void_p
    cairo.cairo_region_union_rectangle.argtypes = [ctypes.c_void_p, ctypes.POINTER(Rect)]
    cairo.cairo_region_destroy.argtypes = [ctypes.c_void_p]
    gdk.gdk_window_input_shape_combine_region.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]

    region = cairo.cairo_region_create()
    try:
        for x, y, w, h in rects:
            left, top = math.floor(x), math.floor(y)
            rect = Rect(left, top, math.ceil(x + w) - left, math.ceil(y + h) - top)
            cairo.cairo_region_union_rectangle(region, ctypes.byref(rect))
        # hash() of a PyGObject is the address of the underlying GObject
        gdk.gdk_window_input_shape_combine_region(hash(gdk_window), region, 0, 0)
    finally:
        cairo.cairo_region_destroy(region)


def _place(window, win_rect, hot) -> None:
    """Size, show and position the window (position last: mapping can recentre it)."""
    x, y, w, h = win_rect

    def apply(gw):
        gw.set_size_request(1, 1)  # a larger earlier size must not pin the minimum
        gw.resize(w, h)
        gw.show_all()
        gw.move(x, y)
        _apply_input_region(gw, hot() or [[0, 0, w, h]])

    _gtk_call(window, apply)


LEAVE_GRACE_MS = 250  # Codenotch's LEAVE_GRACE (linux_pointer.rs)


def _tune_x11(window) -> None:
    """Dock type hint, hidden from taskbar/pager, resizable; plus Codenotch's native pointer events.

    GTK enter/leave on the window are relayed to the page as ``codenotchPointer(bool)``, with a
    250 ms grace before a leave (``linux_pointer.rs``). They are what a compositor reliably sends;
    DOM mouseleave is not.
    """
    import time

    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, GLib
        from webview.platforms import gtk as wgtk
    except Exception:
        return

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if wgtk.BrowserView.instances.get(window.uid) is not None:
            break
        time.sleep(0.1)
    else:
        return
    view = wgtk.BrowserView.instances[window.uid]

    def js(inside: bool) -> None:
        view.webview.run_javascript(
            "window.codenotchPointer&&window.codenotchPointer(%s)" % ("true" if inside else "false"), None, None, None)

    pending: list[int | None] = [None]

    def cancel() -> None:
        if pending[0] is not None:
            GLib.source_remove(pending[0])
            pending[0] = None

    def leave_now() -> bool:
        pending[0] = None
        js(False)
        return False

    def on_leave(_w, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            cancel()
            pending[0] = GLib.timeout_add(LEAVE_GRACE_MS, leave_now)
        return False

    def on_enter(_w, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            cancel()
            js(True)
        return False

    def apply(w):
        w.set_resizable(True)
        w.set_skip_taskbar_hint(True)
        w.set_skip_pager_hint(True)
        w.set_type_hint(Gdk.WindowTypeHint.DOCK)
        w.stick()
        w.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        w.connect("enter-notify-event", on_enter)
        w.connect("leave-notify-event", on_leave)
        w.connect("destroy", lambda _w: cancel())

    _gtk_call(window, apply)
