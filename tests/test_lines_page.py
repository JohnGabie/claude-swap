"""lines_page: the lines window (SL3, SL6): its page, its window driving and its API.

Synthetic data only. The page runs in node with a DOM stub whose elements keep their real
listeners by type; every event goes through the page's own listeners, never its functions.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading

import pytest

from claude_swap import fleet_page, lines_page
from claude_swap.models import AccountPolicy
from claude_swap.settings import AutoSwitchSettings

from tests.test_fleet_page import _oklch, _page_colors

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

OK = {"ok": True, "error": None}


def page_script() -> str:
    m = re.search(r"<script>(.*)</script>", lines_page.LINES_PAGE, re.S)
    assert m
    return m.group(1)


# ---- the page: static checks ---------------------------------------------------


@needs_node
def test_lines_page_script_passes_node_check(tmp_path):
    path = tmp_path / "lines.js"
    path.write_text(page_script(), encoding="utf-8")
    run = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr


def test_lines_page_has_no_purple_hue():
    # FP2: one palette for pill and panels; no colour with a purple hue
    colors = _page_colors(lines_page.LINES_PAGE)
    assert colors
    bad = [name for name, rgb in colors if (lambda L, C, h: C >= 0.05 and 290 <= h <= 345)(*_oklch(*rgb))]
    assert not bad, bad
    page = lines_page.LINES_PAGE.upper()
    for token in ("#000000", "#0A9396", "#94D2BD", "#EE9B00", "#CF4332", "#F4EEDB", "#8F8873", "#121A1C"):
        assert token in page, token


def test_lines_page_copy_and_layout():
    # SL3: pt-BR copy, the order of the parts, the sliders' range and the window size
    page = lines_page.LINES_PAGE
    order = ["<title>cswap lines</title>", ">Limites de troca<", "id=close",
             ">Quando a conta ativa chega na linha, o cswap troca de conta.<",
             ">Auto-switch desligado: os limites valem quando ligar.<",
             ">Sessão de 5 h<", "id=s5", ">Semana<", "id=sw", ">Igual à de 5 h<", ">Por conta<", "id=accs",
             ">Salvo ao soltar<", ">Restaurar padrão<"]
    at = [page.index(s) for s in order]
    assert at == sorted(at)
    assert page.count("type=range min=50 max=99 step=1") == 2
    assert "html,body{margin:0;width:320px;height:520px;overflow:hidden;background:#000000}" in page
    assert ".accs{flex:1;min-height:0;overflow-y:auto" in page  # the account list scrolls inside the window
    assert "@" not in page


# ---- the page: driven through its real listeners in a node DOM stub ----------------

_DRIVER = r"""
const vm = require('vm'), fs = require('fs');
const els = {}, docL = {}, winL = {}, timers = [], calls = [];
function el(id) {
  if (!els[id]) {
    const L = {};
    els[id] = { id, L, value: '', disabled: false, checked: false, hidden: false, textContent: '', innerHTML: '',
      style: { props: {}, setProperty(k, v) { this.props[k] = String(v); } },
      addEventListener(t, f) { (L[t] = L[t] || []).push(f); } };
  }
  return els[id];
}
const sync = v => ({ then(f) { return sync(f(v)); } });
const unhandled = [];
const reject = () => ({ then(f, g) { if (g) g(new Error('x')); else unhandled.push(1); return sync(undefined); } });
const answers = { get: () => sync(DATA), set: () => sync({ ok: true, error: null }), close: () => sync({ ok: true, error: null }) };
let DATA = null;
const api = {
  get: (...a) => { calls.push(['get', ...a]); return answers.get(...a); },
  set: (...a) => { calls.push(['set', ...a]); return answers.set(...a); },
  close: (...a) => { calls.push(['close', ...a]); return answers.close(...a); } };
const ctx = vm.createContext({
  document: { getElementById: el, addEventListener(t, f) { (docL[t] = docL[t] || []).push(f); } },
  addEventListener(t, f) { (winL[t] = winL[t] || []).push(f); },
  setInterval(f, ms) { timers.push({ f, ms, on: true }); return timers.length; },
  clearInterval(id) { if (timers[id - 1]) timers[id - 1].on = false; },
  pywebview: { api }, Math, JSON, Number, String, Array, isFinite });
vm.runInContext('var window = globalThis;', ctx);
const pre = process.argv[4] || '';
const fire = (L, t, e) => { for (const f of (L[t] || [])) f(e || {}); };
const ev = () => { const e = { prevented: false, preventDefault() { e.prevented = true; } }; return e; };
const state = () => Object.fromEntries(Object.entries(els).map(([k, v]) => [k, { value: v.value, disabled: v.disabled,
  checked: v.checked, hidden: v.hidden, textContent: v.textContent, innerHTML: v.innerHTML, p: v.style.props['--p'] }]));
const ready = d => { DATA = d; fire(winL, 'pywebviewready'); };
const window = ctx;
if (pre) eval(pre);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
process.stdout.write(JSON.stringify(eval(process.argv[3])));
"""


def run_page(tmp_path, expr: str, pre: str = ""):
    (tmp_path / "lines.js").write_text(page_script(), encoding="utf-8")
    (tmp_path / "driver.js").write_text(_DRIVER, encoding="utf-8")
    run = subprocess.run(["node", str(tmp_path / "driver.js"), str(tmp_path / "lines.js"), expr, pre],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def account(num, use5=12.0, use_week=40.0, five=90.0, week=95.0, own5=False, own_week=False):
    return {"num": str(num), "five": five, "week": week, "own5": own5, "ownWeek": own_week,
            "use5": use5, "useWeek": use_week}


def data(five=90.0, week=95.0, auto=True, accounts=None):
    if accounts is None:
        accounts = [account(1), account(2, use5=None, use_week=None),
                    account(5, 48.0, 81.0, five=80.0, week=90.0, own5=True, own_week=True),
                    account(6, 44.0, 51.0, week=85.0, own_week=True)]
    return {"five": five, "week": week, "autoSwitch": auto, "accounts": accounts}


def drive(tmp_path, steps: str, d=None, pre: str = ""):
    return run_page(tmp_path, "(() => { ready(%s); %s })()" % (json.dumps(d if d is not None else data()), steps), pre)


@needs_node
def test_page_paints_get_on_ready(tmp_path):
    out = drive(tmp_path, "return {calls, s: state()};")
    s = out["s"]
    assert out["calls"] == [["get"]]
    assert s["s5"]["value"] == "90" and s["v5"]["textContent"] == "90%"
    assert s["h5"]["textContent"] == "Troca quando a janela de 5 h passa de 90%."
    assert s["s5"]["p"] == "81.63%"  # (90 - 50) / 49
    assert s["sw"]["value"] == "95" and s["vw"]["textContent"] == "95%" and s["sw"]["p"] == "91.84%"
    assert s["same"]["checked"] is False and s["sw"]["disabled"] is False
    assert s["off"]["hidden"] is True
    assert s["accs"]["innerHTML"] == (
        '<div class=acc><span class=n>#1</span><span class=v>5 h 12% · sem 40%</span><span class=chip>geral</span></div>'
        '<div class=acc><span class=n>#2</span><span class=v>5 h – · sem –</span><span class=chip>geral</span></div>'
        '<div class=acc><span class=n>#5</span><span class=v>5 h 48% · sem 81%</span>'
        '<span class="chip own">5 h 80 · sem 90</span></div>'
        '<div class=acc><span class=n>#6</span><span class=v>5 h 44% · sem 51%</span>'
        '<span class="chip own">sem 85</span></div>')


@needs_node
def test_page_paints_unset_week_auto_off_and_decimals(tmp_path):
    d = data(five=99.9, week=None, auto=False,
             accounts=[account(3, 7.4, 0.5, five=99.9, week=99.9, own5=True), account("<i>")])
    s = drive(tmp_path, "return state();", d)
    assert s["same"]["checked"] is True and s["sw"]["disabled"] is True
    assert s["s5"]["value"] == "99" and s["sw"]["value"] == "99"  # the slider ends at 99
    assert s["v5"]["textContent"] == "99,9%" and s["vw"]["textContent"] == "99,9%"
    assert s["h5"]["textContent"] == "Troca quando a janela de 5 h passa de 99,9%."
    assert s["off"]["hidden"] is False
    assert s["accs"]["innerHTML"] == ('<div class=acc><span class=n>#3</span><span class=v>5 h 7% · sem 1%</span>'
                                      '<span class="chip own">5 h 99,9</span></div>'
                                      '<div class=acc><span class=n>#&lt;i&gt;</span><span class=v>5 h 12% · sem 40%</span>'
                                      '<span class=chip>geral</span></div>')


@needs_node
def test_page_drops_a_failed_read(tmp_path):
    # a get() that is not the payload, or a rejected one, keeps the last painted values
    out = drive(tmp_path, "answers.get = () => sync({ok: false, error: 'x'}); fire(winL, 'pywebviewready');"
                          " answers.get = () => sync(null); fire(winL, 'pywebviewready');"
                          " answers.get = () => reject(); fire(winL, 'pywebviewready');"
                          " return {calls, unhandled, s: state()};")
    assert out["calls"] == [["get"]] * 4 and out["unhandled"] == []
    assert out["s"]["s5"]["value"] == "90" and out["s"]["accs"]["innerHTML"].count("class=acc>") == 4


@needs_node
def test_page_without_the_bridge_does_nothing(tmp_path):
    out = run_page(tmp_path, "(() => { delete ctx.pywebview; DATA = null; fire(winL, 'pywebviewready');"
                             " el('s5').value = '70'; fire(el('s5').L, 'change', {});"
                             " fire(el('close').L, 'click', ev()); return calls; })()")
    assert out == []


@needs_node
def test_five_slider_shows_on_input_and_writes_on_release(tmp_path):
    out = drive(tmp_path, "calls.length = 0; el('s5').value = '72'; fire(el('s5').L, 'input', {});"
                          " const during = {calls: calls.slice(), s: state()};"
                          " fire(el('s5').L, 'change', {}); return {during, calls};")
    s = out["during"]["s"]
    assert out["during"]["calls"] == []  # never during a drag
    assert s["v5"]["textContent"] == "72%" and s["h5"]["textContent"] == "Troca quando a janela de 5 h passa de 72%."
    assert s["s5"]["p"] == "44.90%"
    assert s["sw"]["value"] == "95" and s["vw"]["textContent"] == "95%"  # the week has its own line
    assert out["calls"] == [["set", "five", 72], ["get"]]  # the write, then a read


@needs_node
def test_five_slider_moves_the_week_when_same(tmp_path):
    out = drive(tmp_path, "el('s5').value = '72'; fire(el('s5').L, 'input', {}); return state();", data(week=None))
    assert out["sw"]["value"] == "72" and out["vw"]["textContent"] == "72%" and out["sw"]["p"] == "44.90%"


@needs_node
def test_week_slider_shows_on_input_and_writes_on_release(tmp_path):
    out = drive(tmp_path, "calls.length = 0; el('sw').value = '93'; fire(el('sw').L, 'input', {});"
                          " const during = {calls: calls.slice(), s: state()};"
                          " fire(el('sw').L, 'change', {}); return {during, calls};")
    assert out["during"]["calls"] == []
    assert out["during"]["s"]["vw"]["textContent"] == "93%" and out["during"]["s"]["sw"]["p"] == "87.76%"
    assert out["during"]["s"]["v5"]["textContent"] == "90%"
    assert out["calls"] == [["set", "week", 93], ["get"]]


@needs_node
def test_same_toggle_on_unsets_the_week(tmp_path):
    out = drive(tmp_path, "calls.length = 0; answers.get = () => sync({}); el('same').checked = true;"
                          " fire(el('same').L, 'change', {}); return {calls, s: state()};")
    assert out["calls"] == [["set", "week", None], ["get"]]
    assert out["s"]["sw"]["disabled"] is True and out["s"]["sw"]["value"] == "90"
    assert out["s"]["vw"]["textContent"] == "90%" and out["s"]["sw"]["p"] == "81.63%"


@needs_node
def test_same_toggle_off_writes_the_shown_value(tmp_path):
    out = drive(tmp_path, "calls.length = 0; answers.get = () => sync({}); el('same').checked = false;"
                          " fire(el('same').L, 'change', {}); return {calls, s: state()};", data(five=88.0, week=None))
    assert out["calls"] == [["set", "week", 88], ["get"]]
    assert out["s"]["sw"]["disabled"] is False


@needs_node
def test_reset_link_writes_reset(tmp_path):
    out = drive(tmp_path, "calls.length = 0; const e = ev(); fire(el('reset').L, 'click', e);"
                          " return {calls, prevented: e.prevented};")
    assert out == {"calls": [["set", "reset", None], ["get"]], "prevented": True}


@needs_node
def test_close_button_and_escape_close(tmp_path):
    out = drive(tmp_path, "calls.length = 0; fire(el('close').L, 'click', ev()); const a = calls.slice();"
                          " calls.length = 0; fire(docL, 'keydown', {key: 'Enter'}); const b = calls.slice();"
                          " fire(docL, 'keydown', {key: 'Escape'}); return {a, b, c: calls};")
    assert out == {"a": [["close"]], "b": [], "c": [["close"]]}


@needs_node
def test_write_result_shows_one_error_line(tmp_path):
    out = drive(tmp_path, "const seen = []; const step = () => { fire(el('s5').L, 'change', {}); seen.push(el('err').textContent); };"
                          " answers.set = () => sync({ok: false, error: 'Falhou aqui.'}); step();"
                          " answers.set = () => reject(); step();"
                          " answers.set = () => sync({ok: false}); step();"
                          " answers.set = () => sync(null); step();"
                          " answers.set = () => sync({ok: false, error: 5}); step();"
                          " answers.set = () => sync({ok: 1, error: null}); step();"
                          " answers.set = () => sync({ok: true, error: null}); step();"
                          " return {seen, calls: calls.filter(c => c[0] === 'get').length};")
    assert out["seen"] == ["Falhou aqui."] + [lines_page.WRITE_FAILED] * 5 + [""]
    assert out["calls"] == 8  # ready, then one read after each write


@needs_node
def test_open_reads_and_reloads_every_30_s_until_closed(tmp_path):
    out = drive(tmp_path, "calls.length = 0; window.linesOpen(); const opened = calls.slice();"
                          " const live = timers.filter(t => t.on).map(t => t.ms); calls.length = 0;"
                          " timers.filter(t => t.on).forEach(t => t.f()); const tick = calls.slice();"
                          " window.linesOpen(); const again = timers.filter(t => t.on).length;"
                          " window.linesClosed(); return {opened, live, tick, again, after: timers.filter(t => t.on).length};")
    assert out == {"opened": [["get"]], "live": [30000], "tick": [["get"]], "again": 1, "after": 0}


# ---- the window: GTK show, present, move, hide ------------------------------------------


class FakeView:
    def __init__(self):
        self.log = []
        view = self

        class W:
            def __getattr__(self, name):
                return lambda *a: view.log.append((name, *a))

        class V:
            def run_javascript(self, script, *_a):
                view.log.append(("js", script))

        self.window, self.webview = W(), V()


ANCHOR = {"edge": "left", "square": [8, 600, 30, 30], "area": [0, 0, 1920, 1080]}


def test_lines_window_opens_beside_the_gear_and_closes():
    view = FakeView()
    win = lines_page.LinesWindow(lambda fn: fn(view), lambda: ANCHOR)
    assert win.is_open is False
    win.toggle()
    x, y = fleet_page.panel_position(ANCHOR, size=(320, 520))
    assert view.log == [("show_all",), ("deiconify",), ("present",), ("move", x, y),
                        ("js", "window.linesOpen&&window.linesOpen()")]
    assert win.is_open is True
    view.log.clear()
    win.toggle()
    assert view.log == [("js", "window.linesClosed&&window.linesClosed()"), ("hide",)]
    assert win.is_open is False


# ---- LinesWindowApi ----------------------------------------------------------------------

CORE = AutoSwitchSettings(threshold=90.0, weekly_threshold=97.0)
POLICIES = {"1": AccountPolicy(), "2": AccountPolicy(threshold=85.0), "3": AccountPolicy(weekly_threshold=92.5),
            "4": AccountPolicy(threshold=80.0, weekly_threshold=93.0)}


def card(num, five=10.0, week=20.0, title=None):
    rows = [{"label": "5h", "pct": five, "reset": None, "age": None},
            {"label": "7d", "pct": week, "reset": None, "age": None}]
    return {"num": str(num), "title": title or f"#{num}", "active": False, "disabled": False,
            "rows": [r for r in rows if r["pct"] is not None], "message": None}


def make_api(core=CORE, policies=None, cards=None, auto=True, save=None, closed=None, saved=None):
    return lines_page.LinesWindowApi(
        lambda: core, lambda: POLICIES if policies is None else policies,
        lambda: [card(1, 12.0, 40.0), card(2, None, None), card(3, 48.4, 81.0), card(4)] if cards is None else cards,
        lambda: auto, save or (lambda core_, app: {"ok": True, "output": "settings saved"}),
        (lambda: closed.append(1)) if closed is not None else (lambda: None),
        (lambda: saved.append(1)) if saved is not None else (lambda: None))


def test_lines_window_get_shape():
    got = make_api().get()
    assert got == {
        "five": 90.0, "week": 97.0, "autoSwitch": True,
        "accounts": [
            {"num": "1", "five": 90.0, "week": 97.0, "own5": False, "ownWeek": False, "use5": 12.0, "useWeek": 40.0},
            {"num": "2", "five": 85.0, "week": 97.0, "own5": True, "ownWeek": False, "use5": None, "useWeek": None},
            {"num": "3", "five": 90.0, "week": 92.5, "own5": False, "ownWeek": True, "use5": 48.4, "useWeek": 81.0},
            {"num": "4", "five": 80.0, "week": 93.0, "own5": True, "ownWeek": True, "use5": 10.0, "useWeek": 20.0},
        ]}
    # no general weekly line: "week" is null, each account's effective weekly line is its 5-hour line
    unset = make_api(core=AutoSwitchSettings(threshold=88.0), auto=False).get()
    assert unset["week"] is None and unset["autoSwitch"] is False and unset["five"] == 88.0
    assert [(a["five"], a["week"]) for a in unset["accounts"]] == [(88.0, 88.0), (85.0, 85.0), (88.0, 92.5), (80.0, 93.0)]
    assert all(isinstance(a["five"], float) and isinstance(a["week"], float) for a in unset["accounts"])
    # an account the store does not know gets the general lines
    assert make_api(policies={}).get()["accounts"][3] == {
        "num": "4", "five": 90.0, "week": 97.0, "own5": False, "ownWeek": False, "use5": 10.0, "useWeek": 20.0}


def test_lines_window_get_drops_bad_use_values():
    cards = [card(1, True, float("nan")), card(2, "12", float("inf"))]
    got = make_api(cards=cards).get()["accounts"]
    assert [(a["use5"], a["useWeek"]) for a in got] == [(None, None), (None, None)]


def test_lines_window_set_and_reset():
    seen = []

    def save(core, app):
        seen.append((core, app, threading.current_thread() is threading.main_thread()))
        return {"ok": True, "output": "settings saved"}

    saved = []
    api = make_api(save=save, saved=saved)
    assert api.set("five", 80) == OK
    assert api.set("week", 93.5) == OK
    assert api.set("week", None) == OK
    assert api.set("reset", "ignored") == OK
    assert api.set("reset") == OK
    reset = {"autoswitch.threshold": 90.0, "autoswitch.weeklyThreshold": None}
    assert seen == [({"autoswitch.threshold": 80}, {}, False), ({"autoswitch.weeklyThreshold": 93.5}, {}, False),
                    ({"autoswitch.weeklyThreshold": None}, {}, False), (reset, {}, False), (reset, {}, False)]
    assert saved == [1] * 5  # each write asks the tray for a refresh


@pytest.mark.parametrize("key,value", [
    ("five", None), ("five", 49), ("five", 99.5), ("five", True), ("five", "80"), ("five", float("nan")),
    ("week", 100), ("week", 49.9), ("week", False), ("week", [90]), ("day", 80), (None, 80),
])
def test_lines_window_set_refuses_bad_input(key, value, caplog):
    seen, saved = [], []
    api = make_api(save=lambda core, app: seen.append(core) or {"ok": True}, saved=saved)
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        assert api.set(key, value) == {"ok": False, "error": lines_page.WRITE_FAILED}
    assert seen == [] and saved == []
    assert caplog.records == []  # a refusal, not a failure


def test_lines_window_set_reports_a_failed_save(caplog):
    saved = []
    failed = make_api(save=lambda core, app: {"ok": False, "output": "could not read /x/a@example.com"}, saved=saved)
    assert failed.set("five", 80) == {"ok": False, "error": lines_page.WRITE_FAILED}

    def boom(core, app):
        raise OSError("a@example.com token sk-1")

    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        assert make_api(save=boom, saved=saved).set("five", 80) == {"ok": False, "error": lines_page.WRITE_FAILED}
    assert make_api(save=lambda core, app: None, saved=saved).set("five", 80)["ok"] is False
    assert saved == []
    assert "OSError" in caplog.text and "@" not in caplog.text and "sk-1" not in caplog.text


def test_lines_window_close_calls_back():
    closed = []
    assert make_api(closed=closed).close() == OK
    assert closed == [1]


def test_lines_window_api_catches_every_exception(caplog):
    def boom():
        raise RuntimeError("a@example.com")

    api = lines_page.LinesWindowApi(boom, boom, boom, boom, lambda c, a: {"ok": True}, boom, boom)
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        results = [api.get(), api.set("five", 80), api.close()]
    assert results == [{"ok": False, "error": lines_page.READ_FAILED}, {"ok": False, "error": lines_page.WRITE_FAILED},
                       {"ok": False, "error": lines_page.CLOSE_FAILED}]
    assert caplog.text.count("RuntimeError") == 3 and "@" not in caplog.text


def test_with_lines_and_general_lines():
    cards = [card(1), card(2), card(9)]
    shown = lines_page.with_lines(cards, POLICIES, 90.0, None)
    assert [c["lines"] for c in shown] == [
        {"five": 90.0, "week": 90.0, "own5": False, "ownWeek": False},
        {"five": 85.0, "week": 85.0, "own5": True, "ownWeek": False},
        {"five": 90.0, "week": 90.0, "own5": False, "ownWeek": False},
    ]
    assert all("lines" not in c for c in cards)  # copies; the tray's cards stay as they are
    assert lines_page.general_lines(90.0, None) == {"five": 90.0, "week": 90.0}
    assert lines_page.general_lines(88, 97) == {"five": 88.0, "week": 97.0}
    assert isinstance(lines_page.general_lines(88, 97)["five"], float)

