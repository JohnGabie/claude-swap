"""Pure parts of the screen-edge pill: geometry, outline, payloads, settings."""

import itertools
import json
from datetime import datetime, timezone

import pytest

from claude_swap import desktop, pill
from claude_swap.models import AccountSnapshot
from claude_swap.switcher import SENTINEL_NOTES
from claude_swap.tray import TraySettings
from claude_swap.tui import data
from claude_swap.usage_store import UsageEntry

NOW = 1_790_000_000.0
FAKE_EMAIL = "a@b.c"
FAKE_ORG = "a@b.c's Organization"  # a personal account's organization name holds its email


def window(remaining=3600.0, pct=50.0):
    """One usage window that resets ``remaining`` seconds after NOW."""
    return {"pct": pct, "resets_at": datetime.fromtimestamp(NOW + remaining, timezone.utc).isoformat()}


def card(num=1, five=50.0, week=10.0, active=False):
    return {
        "num": num, "title": f"#{num}  {FAKE_EMAIL}", "tag": FAKE_ORG, "active": active, "disabled": False,
        "message": None,
        "rows": [{"label": "5h", "pct": five, "reset": data.reset_text(window(), NOW), "age": None},
                 {"label": "7d", "pct": week, "reset": None, "age": None}],
    }


def test_normalize_falls_back_to_defaults():
    assert pill.normalize_edge("top") == "top"
    assert pill.normalize_edge("middle") == pill.DEFAULT_EDGE
    assert pill.normalize_theme("white") == "white"
    assert pill.normalize_theme(None) == pill.DEFAULT_THEME








AREA = (0, 32, 1600, 868)  # a 1600x900 screen minus a 32px top bar


def contains(outer, inner):
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ix + iw <= ox + ow and iy + ih <= oy + oh


@pytest.mark.parametrize("edge", pill.EDGES)
@pytest.mark.parametrize("n", [0, 1, 3, 6])
def test_window_holds_the_pill_and_room_for_the_card(edge, n):
    lay = pill.window_layout(edge, n, AREA)
    wx, wy, ww, wh = lay["win"]
    px, py, pw, ph = lay["pill"]
    assert (pw, ph) == pill.pill_size(edge, n)
    assert contains((0, 0, ww, wh), lay["pill"])
    assert contains(AREA, lay["win"]) or ww > AREA[2] or wh > AREA[3]
    # the window leaves space for the tallest card beside (or below/above) the pill
    if pill.is_vertical(edge):
        assert ww >= pw + pill.GAP + pill.ARROW + pill.CARD_W and wh >= pill.CARD_MAX_H
    else:
        assert wh >= ph + pill.GAP + pill.ARROW + pill.CARD_MAX_H and ww >= pill.CARD_W


@pytest.mark.parametrize("edge,flush", [
    ("right", lambda L: L["win"][0] + L["win"][2] == AREA[0] + AREA[2]),
    ("left", lambda L: L["win"][0] == AREA[0]),
    ("top", lambda L: L["win"][1] == AREA[1]),
    ("bottom", lambda L: L["win"][1] + L["win"][3] == AREA[1] + AREA[3]),
])
def test_window_is_flush_with_its_screen_edge(edge, flush):
    assert flush(pill.window_layout(edge, 3, AREA))


@pytest.mark.parametrize("edge", pill.EDGES)
def test_pill_stays_centred_along_the_edge_on_screen(edge):
    lay = pill.window_layout(edge, 3, AREA)
    wx, wy = lay["win"][:2]
    px, py, pw, ph = lay["pill"]
    if pill.is_vertical(edge):
        assert abs((wy + py + ph / 2) - (AREA[1] + AREA[3] / 2)) <= 1
    else:
        assert abs((wx + px + pw / 2) - (AREA[0] + AREA[2] / 2)) <= 1


def test_pill_is_against_the_screen_side_of_its_window():
    assert pill.window_layout("right", 3, AREA)["pill"][0] + pill.THICKNESS == pill.window_layout("right", 3, AREA)["win"][2]
    assert pill.window_layout("left", 3, AREA)["pill"][0] == 0
    assert pill.window_layout("top", 3, AREA)["pill"][1] == 0
    lay = pill.window_layout("bottom", 3, AREA)
    assert lay["pill"][1] + pill.THICKNESS == lay["win"][3]


@pytest.mark.parametrize("env,expected", [
    ({"I3SOCK": "/run/user/1000/i3/ipc-socket.1"}, True),
    ({"XDG_CURRENT_DESKTOP": "i3"}, True),
    ({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, False),
    ({}, False),
])
def test_is_i3(env, expected):
    assert pill.is_i3(env) is expected


def _i3_output(name, x, y, w, h, bar):
    return {
        "type": "output", "name": name, "rect": {"x": x, "y": y, "width": w, "height": h},
        "nodes": [
            {"type": "dockarea", "name": "topdock", "rect": {"x": x, "y": y, "width": w, "height": bar}},
            {"type": "con", "name": "content", "rect": {"x": x, "y": y + bar, "width": w, "height": h - bar}},
        ],
    }


def test_i3_content_rect_is_the_monitor_under_the_point_minus_its_bar():
    tree = {"nodes": [
        # i3's scratchpad output really does overlap the first monitor, with an empty content
        {"type": "output", "name": "__i3", "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}, "nodes": [
            {"type": "con", "name": "content", "rect": {"x": 0, "y": 0, "width": 0, "height": 0}},
        ]},
        _i3_output("DP-0", 0, 0, 1920, 1080, 30),
        _i3_output("DP-4", 1920, 0, 1920, 1080, 0),
    ]}
    assert pill.i3_content_rect(tree, 0, 0) == (0, 30, 1920, 1050)
    assert pill.i3_content_rect(tree, 1920, 0) == (1920, 0, 1920, 1080)
    assert pill.i3_content_rect(tree, 9999, 0) is None
    assert pill.i3_content_rect({}, 0, 0) is None


@pytest.mark.parametrize("edge", ["left", "right"])
def test_offset_moves_a_side_pill_along_its_edge(edge):
    area = (0, 30, 1920, 1050)
    base = pill.window_layout(edge, 3, area)["win"]
    assert pill.window_layout(edge, 3, area, 80)["win"][1] == base[1] + 80
    assert pill.window_layout(edge, 3, area, -80)["win"][1] == base[1] - 80


@pytest.mark.parametrize("offset", [5000, -5000])
def test_offset_keeps_the_pill_on_the_monitor_and_ignores_top_and_bottom(offset):
    area = (0, 30, 1920, 1050)
    assert pill.window_layout("top", 3, area, offset) == pill.window_layout("top", 3, area)
    lay = pill.window_layout("left", 3, area, offset)
    x, y, w, h = lay["win"]
    assert y >= 30 and y + h <= 30 + 1050
    assert contains((0, 0, w, h), lay["pill"])


def test_pill_payload_carries_each_accounts_card_data():
    lay = pill.window_layout("right", 2, AREA)
    payload = pill.pill_payload([card(1, 73, 15, active=True), card(2, None, None)], "right", "white", lay)
    assert payload["theme"] == "white" and payload["vertical"] is True and payload["layout"] == lay
    first, second = payload["items"]
    assert (first["five"], first["week"], first["active"]) == (73.0, 15.0, True)
    assert [r["label"] for r in first["rows"]] == ["5h", "7d"] and "title" not in first and "tag" not in first
    assert second["five"] is None
    json.dumps(payload)  # it is handed to the page as JSON


def test_tray_settings_keep_valid_strings_and_drop_bad_ones(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"pill_edge": "top", "pill_theme": "neon", "pill_enabled": False}))
    s = TraySettings.load(path)
    assert (s.pill_edge, s.pill_theme, s.pill_enabled) == ("top", "black", False)
    path.write_text(json.dumps({"pill_edge": 5}))
    assert TraySettings.load(path).pill_edge == "right"


def test_pill_rests_folded_by_default_and_the_setting_round_trips(tmp_path):
    assert TraySettings().pill_fold is True  # Codenotch's "show on hover": a sliver until the pointer reaches the edge
    path = tmp_path / "s.json"
    TraySettings(pill_fold=False).save(path)
    assert TraySettings.load(path).pill_fold is False


def test_payload_tells_the_page_whether_to_fold():
    lay = pill.window_layout("right", 1, AREA)
    assert pill.pill_payload([card()], "right", "black", lay)["fold"] is False
    assert pill.pill_payload([card()], "right", "black", lay, fold=True)["fold"] is True


def test_controller_never_blocks_on_the_page():
    """Window.evaluate_js waits for the page, and tray-menu callbacks can run on the GTK main
    thread that the answer needs: calling it from the controller froze the app on any menu change."""
    import inspect

    code = [ln for ln in inspect.getsource(pill.PillController).splitlines() if ".evaluate_js(" in ln]
    assert code == [], code


# ---- ranking sign ----------------------------------------------------------

def test_rank_of_accepts_only_one_to_three():
    assert [pill.rank_of({"rank": v}) for v in (1, 2, 3)] == [1, 2, 3]
    assert [pill.rank_of({"rank": v}) for v in (0, 4, None, "1", True, 1.0)] == [0] * 6
    assert pill.rank_of({}) == 0


def test_payload_carries_the_alert_text_only():
    lay = pill.window_layout("top", 1, (0, 0, 1000, 800))
    cards = [{"num": "2", "rows": [], "alert": "Plan lapsed or blocked by the organization: usage cannot be read."},
             {"num": "3", "rows": [], "alert": 5}]
    items = pill.pill_payload(cards, "top", "black", lay)["items"]
    assert items[0]["alert"].startswith("Plan lapsed") and items[1]["alert"] is None


def test_page_draws_the_alert_asterisk_and_its_text():
    assert "it.alert?'<span class=alert" in pill.PILL_PAGE
    assert "<div class=alert-msg>* ${esc(it.alert)}</div>" in pill.PILL_PAGE


def test_page_draws_the_sparkle_in_three_sizes_and_colors():
    for token in ("#e0a800", "#9aa3b0", "#b8692a", "function spark(rank)"):
        assert token in pill.PILL_PAGE


def test_payload_reveal_is_off_unless_asked():
    lay = pill.window_layout("top", 0, (0, 0, 1000, 800))
    assert pill.pill_payload([], "top", "black", lay)["reveal"] is False
    assert pill.pill_payload([], "top", "black", lay, reveal=True)["reveal"] is True


# ---- fleet panel (FP2, FP3, FP10, FP12) -------------------------------------

import os
import re
import shutil
import subprocess
import textwrap

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def page_script() -> str:
    """The page's own script, as the browser receives it (Python escapes already decoded)."""
    m = re.search(r"<script>(.*)</script>", pill.PILL_PAGE, re.S)
    assert m
    return m.group(1)


@needs_node
def test_page_script_passes_node_check(tmp_path):
    path = tmp_path / "page.js"
    path.write_text(page_script(), encoding="utf-8")
    run = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr


_DRIVER = textwrap.dedent("""
    // Loads the page script in a context where every DOM access is a no-op, then calls its pure functions.
    const vm = require('vm'), fs = require('fs');
    const pre = process.argv[4] || '';
    const stub = () => new Proxy(function () {}, {
      get: (t, k) => k === Symbol.toPrimitive ? () => 0 : stub(), apply: () => stub(), set: () => true });
    const ctx = vm.createContext({ document: stub(), window: { addEventListener() {} }, requestAnimationFrame() {},
      setTimeout() {}, clearTimeout() {}, Date, Math, JSON });
    if (pre) vm.runInContext(pre, ctx);
    vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
    const out = vm.runInContext(process.argv[3], ctx);
    process.stdout.write(JSON.stringify(out));
""")


def run_page(tmp_path, expr: str, pre: str = ""):
    (tmp_path / "page.js").write_text(page_script(), encoding="utf-8")
    (tmp_path / "driver.js").write_text(_DRIVER, encoding="utf-8")
    run = subprocess.run(["node", str(tmp_path / "driver.js"), str(tmp_path / "page.js"), expr, pre],
                         capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "UTC"})
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def budget(**over):
    b = {"plan": "pro", "fiveResetAt": "2026-11-22T00:29:00Z", "weekResetAt": "2026-11-22T05:59:00Z",
         "weekElapsedPct": 66.0, "projectedEndPct": 100.0, "tokensPerPercent": 1_200_000,
         "nowPerMin": 42_000.0, "alias": None, "readAgeS": 10.0}
    b.update(over)
    return b


def item_for(five=33.0, week=95.0, active=True, b=None, nobudget=False):
    c = card(4, five, week, active=active)
    if not nobudget:
        c["budget"] = budget(**(b or {}))
    lay = pill.window_layout("right", 1, AREA)
    return pill.pill_payload([c], "right", "black", lay)["items"][0]


def card_html(tmp_path, item):
    return run_page(tmp_path, "cardHtml(%s)" % json.dumps(item))


def card_text(tmp_path, item):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", card_html(tmp_path, item)))


@needs_node
def test_card_shows_the_fields_of_the_new_card(tmp_path):
    html = card_text(tmp_path, item_for())
    for text in ("#4", "conta ativa", "Pro", "lido agora", "Sessão de 5 h", "33% usado", "volta dom 00:29",
                 "Semana", "95% usado", "fecha em 100%", "renova dom 05:59", "Acaba antes de renovar",
                 "Consumo agora", "42K", "1% desta conta ≈", "1,2M"):
        assert text in html, text
    assert "@" not in card_html(tmp_path, item_for())  # never the email (FP-D4)


@needs_node
def test_card_head_uses_the_alias_and_never_the_email(tmp_path):
    html = card_text(tmp_path, item_for(b={"alias": "trabalho"}))
    assert "#4 trabalho \u00b7 conta ativa" in html and "a@b.c" not in html


@needs_node
def test_card_critical_line_only_when_the_week_ends_early_and_is_not_full(tmp_path):
    assert "Acaba antes de renovar" in card_html(tmp_path, item_for())
    assert "Acaba antes de renovar" not in card_html(tmp_path, item_for(b={"projectedEndPct": 80.0}))
    assert "Acaba antes de renovar" not in card_html(tmp_path, item_for(week=100.0))


@needs_node
def test_card_consumption_is_a_dash_on_an_inactive_account(tmp_path):
    html = card_text(tmp_path, item_for(active=False))
    assert "42K" not in html and "—" in html and "conta ativa" not in html


@needs_node
def test_card_read_age_words(tmp_path):
    assert "lido há 10 min" in card_html(tmp_path, item_for(b={"readAgeS": 600}))
    assert "lido há 5 h" in card_html(tmp_path, item_for(b={"readAgeS": 5 * 3600}))
    assert "lido há 3 d" in card_html(tmp_path, item_for(b={"readAgeS": 3 * 86400}))


@needs_node
def test_card_without_budget_keeps_the_rows_in_the_new_style_and_pt_br(tmp_path):
    html = card_html(tmp_path, item_for(nobudget=True))
    text = card_text(tmp_path, item_for(nobudget=True))
    assert "Sess\u00e3o de 5 h" in text and "Semana" in text and "33% usado" in text and "95% usado" in text
    assert "Current session" not in html and "Weekly" not in html and "used" not in text.replace("usado", "")
    assert "class=bar" in html and "Consumo agora" not in html
    assert "#4" in text and "@" not in html  # never the email (FP-D4)
    assert "volta em 1h" in text


@needs_node
def test_card_without_budget_paused_word_is_pt_br(tmp_path):
    it = item_for(nobudget=True)
    it["disabled"] = True
    text = card_text(tmp_path, it)
    assert "pausada" in text and "paused" not in text


@needs_node
def test_fallback_week_row_is_always_the_available_color(tmp_path):
    html = card_html(tmp_path, item_for(five=95.0, week=95.0, nobudget=True))
    five, week = html.split('<div class="row ')[1:]
    assert five.startswith("l2") and week.startswith("wk") and "l2" not in week
    css = pill.PILL_PAGE
    assert ".row.wk .bar{background:var(--avail-track)}" in css and ".row.wk .bar i{background:var(--avail)}" in css


@needs_node
def test_card_of_an_account_with_no_reading_shows_its_message_and_no_bars(tmp_path):
    for nobudget in (False, True):
        it = item_for(nobudget=nobudget)
        it.update(rows=[], message="Not signed in")
        html = card_html(tmp_path, it)
        assert "Not signed in" in html and "class=bar" not in html and "class=mb" not in html
        assert "@" not in html and "#4" in html


@needs_node
def test_card_bar_follows_the_level_rule_and_week_is_always_available(tmp_path):
    levels = run_page(tmp_path, "[0, 69.9, 70, 89.9, 90, 100, null].map(lvl)")
    assert levels == [0, 0, 1, 1, 2, 2, 0]
    html = card_html(tmp_path, item_for(five=95.0, week=10.0, b={"projectedEndPct": 20.0}))
    five, week = html.split("Semana")
    assert "l2" in five and "l2" not in week
    assert 'class="mb g"' in five and 'class="mb g"' not in week


def _vars(selector: str) -> dict:
    m = re.search(re.escape(selector) + r"\{(.*?)\}", pill._THEME_CSS, re.S)
    assert m, selector
    return dict(re.findall(r"--([\w-]+):\s*([^;]+?)\s*(?=;|--|$)", m.group(1).replace("\n", "").replace(";", ";")))


DARK = {"bg": "#000", "fg": "#F4EEDB", "ink2": "#CFC6A8", "dim": "#8F8873", "track": "#1E2426",
        "grid": "rgba(233,216,166,.08)", "raised": "#121A1C", "spent": "#0A9396", "spent-track": "#05282A",
        "avail": "#94D2BD", "avail-track": "#1D3A31", "warn": "#EE9B00", "crit": "#CF4332"}
WHITE = {"fg": "#16161a", "dim": "#6d6d74", "ink2": "#46464c", "track": "#dedcd5", "grid": "#cfcdc5", "raised": "#ebe9e2",
         "spent": "#087A7D", "avail": "#2F8A6F", "warn": "#B87700", "crit": "#C0341F",
         "spent-track": "#dedcd5", "avail-track": "#dedcd5"}


def test_theme_css_holds_every_fp2_value_dark_and_white():
    dark, white = _vars(":root,#card"), _vars(":root[data-theme=white]")
    for k, v in DARK.items():
        assert dark.get(k) == v, (k, dark.get(k))
    for k, v in WHITE.items():
        assert white.get(k) == v, (k, white.get(k))


def test_account_card_is_black_in_both_themes():
    # the dark values are set on #card itself, so the white theme on :root cannot reach it
    assert ":root,#card{" in pill._THEME_CSS and "#card{" in pill.PILL_PAGE.replace(pill._THEME_CSS, "")
    assert "color:var(--fg)" in re.search(r"#card\{[^}]*\}", pill.PILL_PAGE.replace(pill._THEME_CSS, "")).group(0)


def test_page_ring_colors_come_from_the_palette_by_level():
    css = pill._THEME_CSS
    assert ".l0{--c:var(--spent)}" in css and ".l1{--c:var(--warn)}" in css and ".l2{--c:var(--crit)}" in css
    # the outer arc follows its level, the inner one is always the available color
    assert ".ring .arc.five{stroke:var(--c)}" in pill.PILL_PAGE
    assert ".ring .arc.week{stroke:var(--avail)}" in pill.PILL_PAGE
    assert "p<70?0:p<90?1:2" in pill.PILL_PAGE


def test_page_keeps_sparkle_and_alert_colors():
    for token in ("#e0a800", "#9aa3b0", "#b8692a"):
        assert token in pill.PILL_PAGE


def test_page_draws_the_square_and_keeps_it_out_of_the_account_cells():
    page = pill.PILL_PAGE
    assert "pill-square" in page and "pywebview.api.fleet()" in page
    assert "class=item" not in re.search(r"function buildSquare.*?\n}", page, re.S).group(0)
    assert "P.square" in re.search(r"function placeCard.*?\n}", page, re.S).group(0)


def test_square_is_inside_the_pill_length_and_the_payload():
    assert pill.SQUARE == 56
    assert pill.pill_length(0) == 2 * (pill.FLARE + pill.PAD) + pill.SQUARE + pill.ADD + pill.GEAR
    assert pill.pill_length(3) - pill.pill_length(2) == pill.ITEM
    lay = pill.window_layout("right", 2, AREA)
    fleet = {"sparkline24h": [0.0] * 24, "pace": 86, "level": "neutral"}
    payload = pill.pill_payload([card(1), card(2)], "right", "black", lay, fleet=fleet)
    assert payload["fleet"] == fleet and payload["square"] == pill.SQUARE
    assert pill.pill_payload([card(1)], "right", "black", lay)["fleet"] is None
    assert payload["size"][1] == pill.pill_length(2)


def test_card_is_300_wide_and_the_window_has_room_for_it():
    assert pill.CARD_W == 300
    assert pill.CARD_MAX_H == 600  # SL4 replaces FP10's 372: the open "Limites desta conta" section; tallest measured 571


def test_payload_passes_the_card_budget_through():
    lay = pill.window_layout("right", 1, AREA)
    c = card(1)
    c["budget"] = budget()
    item = pill.pill_payload([c], "right", "black", lay)["items"][0]
    assert item["budget"]["tokensPerPercent"] == 1_200_000 and item["budget"]["fiveResetAt"].endswith("Z")
    assert pill.pill_payload([card(1)], "right", "black", lay)["items"][0]["budget"] is None


def make_controller(edge="left", on_fleet=None):
    class S:
        pill_edge, pill_theme, pill_fold, pill_offset, pill_enabled = edge, "black", False, 0, True

    return pill.PillController(None, None, S(), lambda: None, lambda *a: None, on_fleet=on_fleet)


def test_bridge_fleet_calls_on_fleet():
    calls = []
    ctl = make_controller(on_fleet=lambda: calls.append(1))
    pill.PillBridge(ctl).fleet()
    assert calls == [1]
    pill.PillBridge(make_controller()).fleet()  # no handler: nothing happens


def expected_square(edge, lay):
    wx, wy = lay["win"][:2]
    px, py, pw, ph = lay["pill"]
    along = pill.FLARE + pill.PAD
    if pill.is_vertical(edge):
        return [wx + px + (pw - 46) // 2, wy + py + along, 46, 46]
    return [wx + px + along, wy + py + (ph - 46) // 2, 46, 46]


@pytest.mark.parametrize("edge", pill.EDGES)
def test_panel_anchor_is_the_square_in_screen_pixels(edge, monkeypatch):
    monkeypatch.setattr(pill, "work_area", lambda: AREA)
    ctl = make_controller(edge)
    ctl.cards = [card(1), card(2)]
    a = ctl.panel_anchor()
    assert a["edge"] == edge and a["area"] == list(AREA)
    assert a["square"] == expected_square(edge, pill.window_layout(edge, 2, AREA))


@pytest.mark.parametrize("edge", pill.EDGES)
def test_panel_anchor_follows_the_placed_layout(edge, monkeypatch):
    monkeypatch.setattr(pill, "work_area", lambda: AREA)
    ctl = make_controller(edge)
    ctl.cards = [card(1), card(2)]
    placed = pill.window_layout(edge, 2, (40, 70, 1400, 700))  # not what _layout() computes now
    assert placed != pill.window_layout(edge, 2, AREA)
    ctl._placed = placed
    assert ctl.panel_anchor()["square"] == expected_square(edge, placed)


def test_push_sends_the_fleet_to_the_page(monkeypatch):
    monkeypatch.setattr(pill, "work_area", lambda: AREA)
    ctl = make_controller()
    sent = []
    ctl._run_js = sent.append
    ctl._loaded.set()
    fleet = {"sparkline24h": [2.0] * 24, "pace": 91, "level": "warning"}
    ctl.cards = [card(1)]
    ctl.fleet_data = fleet
    ctl._push()
    assert len(sent) == 1 and sent[0].startswith("render(") and sent[0].endswith(")")
    assert json.loads(sent[0][len("render("):-1])["fleet"] == fleet


def test_controller_update_keeps_the_fleet_for_the_payload():
    ctl = make_controller()
    fleet = {"sparkline24h": [1.0] * 24, "pace": 70, "level": "warning"}
    ctl.update([card(1)], fleet)
    assert ctl.fleet_data == fleet


_PRE_CELLS = """
  const rect = (l, t, w, h) => ({ getBoundingClientRect: () => ({ left: l, top: t, right: l + w, bottom: t + h }) });
  const sq = rect(10, 8, 46, 46), cells = [rect(0, 66, 66, 66), rect(0, 132, 66, 66)];
  const fakePill = { querySelectorAll: sel => [...(sel.includes('.pill-square') ? [sq] : []), ...(sel.includes('.item') ? cells : [])] };
  document = { getElementById: id => id === 'pill' ? fakePill : { addEventListener() {} }, addEventListener() {}, documentElement: {}, body: {} };
"""


@needs_node
def test_cell_at_skips_the_square_and_counts_accounts_only(tmp_path):
    assert run_page(tmp_path, "[cellAt(30, 30), cellAt(30, 90), cellAt(30, 150), cellAt(500, 500)]", _PRE_CELLS) == [None, 0, 1, None]


_PRE_SQUARE = """
  const poly = { a: {}, setAttribute(k, v) { this.a[k] = v; } }, span = { textContent: '' };
  const fakeSquare = { className: '', querySelector: s => s === 'polyline' ? poly : span };
"""


def square_view(tmp_path, fleet):
    return run_page(tmp_path, "(applySquare(fakeSquare, %s), {cls: fakeSquare.className, text: span.textContent, pts: poly.a.points})"
                    % json.dumps(fleet), _PRE_SQUARE)


@needs_node
def test_apply_square_shows_the_pace_and_the_level_class(tmp_path):
    spark = [float(i) for i in range(24)]
    assert square_view(tmp_path, {"sparkline24h": spark, "pace": 86, "level": "neutral"})["text"] == "86%"
    assert square_view(tmp_path, {"sparkline24h": spark, "pace": 86, "level": "neutral"})["cls"] == "pill-square"
    assert square_view(tmp_path, {"sparkline24h": spark, "pace": 70, "level": "warning"})["cls"] == "pill-square warning"
    assert square_view(tmp_path, {"sparkline24h": spark, "pace": 120, "level": "critical"})["cls"] == "pill-square critical"
    assert square_view(tmp_path, {"sparkline24h": spark, "pace": None, "level": "neutral"})["text"] == "\u2013"


@needs_node
def test_apply_square_draws_a_flat_line_with_no_history_and_the_data_otherwise(tmp_path):
    for fleet in (None, {"sparkline24h": [0.0] * 24, "pace": None, "level": "neutral"}):
        view = square_view(tmp_path, fleet)
        ys = {pt.split(",")[1] for pt in view["pts"].split(" ")}
        assert len(view["pts"].split(" ")) == 24 and ys == {"27.0"} and view["text"] == "\u2013"
    pts = square_view(tmp_path, {"sparkline24h": [float(i) for i in range(24)], "pace": 1, "level": "neutral"})["pts"].split(" ")
    assert pts[0].split(",")[1] == "27.0" and pts[-1].split(",")[1] == "7.0"  # the peak is the top of the line


_PRE_SQUARE_CARD = """
  const log = [], shown = new Set(), handlers = {};
  const fakeCard = { classList: { add: (...c) => c.forEach(x => shown.add(x)), remove: (...c) => c.forEach(x => shown.delete(x)) },
                     addEventListener() {} };
  const fakeEl = { style: { setProperty() {} }, addEventListener(t, f) { handlers[t] = f; }, querySelector: () => ({}) };
  const realDoc = document;
  document = { getElementById: id => id === 'card' ? fakeCard : realDoc.getElementById(id), createElement: () => fakeEl,
               addEventListener() {}, documentElement: {}, body: {} };
  pywebview = { api: { fleet() { log.push('fleet:' + [...shown].join(',')); } } };
"""


@needs_node
def test_pill_square_closes_the_card(tmp_path):
    # FP10: the card shows only while the pointer is over an account, so the square must close it.
    show = "(shown.add('show'), shown.add('glide'))"
    enter = run_page(tmp_path, "(buildSquare(), %s, handlers.mouseenter && handlers.mouseenter(), [...shown])" % show, _PRE_SQUARE_CARD)
    assert enter == []
    click = run_page(tmp_path, "(buildSquare(), %s, fakeEl.onclick(), log)" % show, _PRE_SQUARE_CARD)
    assert click == ["fleet:"]  # the card was already closed when the fleet panel was asked for


# ---- no identity in the card, pt-BR fallback rows (FP10) ----------------------

def real_card(num=1, usage=None, active=False, disabled=False, alias="", email=FAKE_EMAIL, org=FAKE_ORG):
    """A card exactly as desktop.py hands it to the pill, built from a fake account."""
    if usage is None:
        usage = UsageEntry(last_good={"five_hour": window(), "seven_day": window()}, age_s=0.0)
    acc = AccountSnapshot(number=str(num), email=email, org_name=org, org_uuid="org-fake", is_active=active,
                          kind="oauth", switchable=True, usage=usage, alias=alias, disabled=disabled)
    return desktop.card_dict(acc, NOW)


def payload_of(*cards, fleet=None):
    return pill.pill_payload(list(cards), "right", "black", pill.window_layout("right", len(cards), AREA), fleet=fleet)


def test_payload_sends_only_the_named_fields_to_the_page():
    c = real_card()
    c.update(email=FAKE_EMAIL, org_name=FAKE_ORG, budget={**budget(), "email": FAKE_EMAIL, "org": FAKE_ORG})
    c["rows"] = [{**r, "email": FAKE_EMAIL} for r in c["rows"]]
    fleet = {"sparkline24h": [1.0] * 24, "pace": 86, "level": "neutral", "email": FAKE_EMAIL}
    payload = payload_of(c, fleet=fleet)
    item = payload["items"][0]
    assert set(item) == {"num", "active", "disabled", "five", "week", "rank", "alert", "message", "budget", "rows"}
    assert set(item["budget"]) == set(budget())
    assert all(set(r) == {"label", "pct", "reset", "age"} for r in item["rows"])
    assert set(payload["fleet"]) == {"sparkline24h", "pace", "level"}


def test_payload_drops_an_alias_that_is_not_a_valid_alias():
    def alias_sent(alias):
        c = real_card()
        c["budget"] = budget(alias=alias)
        return payload_of(c)["items"][0]["budget"]["alias"]

    assert alias_sent(FAKE_EMAIL) is None
    assert alias_sent(f"x {FAKE_ORG}") is None
    assert alias_sent("trabalho") == "trabalho"
    assert alias_sent(None) is None


def every_card_branch():
    """Each reading state of desktop.py, with and without budget, active and paused; every identity field is fake."""
    good = {"five_hour": window(), "seven_day": window()}
    no_pct = {"five_hour": window(pct=None), "seven_day": window(pct=None)}
    usages = [
        UsageEntry(last_good=good, age_s=0.0),
        UsageEntry(last_good=good, age_s=600.0),
        UsageEntry(last_good=no_pct, age_s=0.0),
        UsageEntry(last_good=good, age_s=3 * 3600.0, last_error="http-500"),  # alert: old reading
        UsageEntry(last_error="oauth_not_allowed_for_organization"),  # alert and no reading
        UsageEntry(),  # "no usage yet"
        UsageEntry(sentinel="no credentials"),  # the one sentinel with no note of its own
        *(UsageEntry(sentinel=s) for s in SENTINEL_NOTES),
    ]
    cards = []
    for usage, active, disabled, has_budget in itertools.product(usages, *[(False, True)] * 3):
        n = len(cards) + 1
        alias = "" if n % 2 else "leakalias"  # the title is "#n  alias-or-email"
        c = real_card(n, usage, active, disabled, alias=alias)
        c.update(email=FAKE_EMAIL, org_name=FAKE_ORG)
        if has_budget:
            c["budget"] = budget(alias=FAKE_EMAIL)
        cards.append(c)
    blank = real_card(len(cards) + 1)
    blank.update(rows=[], message=None, alert=None)  # no reading and no message
    return cards + [blank]


# A DOM that records each string the page writes: innerHTML, textContent, title, className, style, attributes
_PRE_DOM = """
  var __wrote = [];
  const __node = () => {
    const set = {};
    return new Proxy(function () {}, {
      get: (t, k) => k === Symbol.toPrimitive ? () => 0 : k === Symbol.iterator ? function* () {} : k in set ? set[k] : __node(),
      set: (t, k, v) => { if (typeof v === 'string') __wrote.push(v); set[k] = v; return true; },
      apply: (t, self, args) => { for (const a of args) if (typeof a === 'string') __wrote.push(a); return __node(); } });
  };
  document = __node();
"""


@needs_node
def test_no_identity_reaches_the_payload_or_any_html_of_the_page(tmp_path):
    cards = every_card_branch()
    payload = payload_of(*cards, fleet={"sparkline24h": [1.0] * 24, "pace": 86, "level": "neutral"})
    sent = json.dumps(payload)
    out = run_page(tmp_path, "(p => (render(p), p.items.forEach((it, i) => showCard(i)),"
                   " {html: p.items.map(cardHtml), wrote: __wrote}))(%s)" % sent, _PRE_DOM)
    assert len(out["html"]) == len(cards) == 97
    for html in out["html"]:
        assert html + "<div class=arrow></div>" in out["wrote"]  # the recorder saw each card the page drew
    everything = [sent, *out["html"], *out["wrote"]]
    for leak in ("@", "leakalias", "Organization", "org-fake"):
        assert [text for text in everything if leak in text][:1] == [], leak


@needs_node
@pytest.mark.parametrize("active,disabled,head", [
    (False, False, "#4"),
    (True, False, "#4 · conta ativa"),
    (False, True, "#4 · pausada"),
    (True, True, "#4 · conta ativa · pausada"),
])
@pytest.mark.parametrize("read", [True, False])
def test_card_without_budget_head_is_the_number_and_the_state_only(tmp_path, active, disabled, head, read):
    c = real_card(4, None if read else UsageEntry(), active, disabled, alias="leakalias")
    html = card_html(tmp_path, payload_of(c)["items"][0])
    assert html.startswith(f"<div class=head><div class=t1>{head}</div></div>"), html
    assert "class=t2" not in html


def fallback_texts(tmp_path, usage):
    """(5-hour reset text, weekly reset text, 5-hour used line) of a card without budget."""
    html = card_html(tmp_path, payload_of(real_card(usage=usage))["items"][0])
    five, week = re.findall(r"<div class=top><span>[^<]*</span><span>([^<]*)</span>", html)
    used = re.sub(r"<[^>]+>", "", re.search(r"<div class=used>(.*?)</div>", html).group(1))
    return five, week, used


@needs_node
@pytest.mark.parametrize("win,source,rest", [
    (window(-60), "resets now", "agora"),
    (window(0), "resets now", "agora"),
    (window(0.5), "resets 0s", "em 0s"),
    (window(45), "resets 45s", "em 45s"),
    (window(720), "resets 12m", "em 12m"),
    (window(7980), "resets 2h 13m", "em 2h 13m"),
    (window(7200), "resets 2h", "em 2h"),
    (window(273600), "resets 3d 4h", "em 3d 4h"),
    (window(259200), "resets 3d", "em 3d"),
    ({"pct": 50.0}, None, None),  # no reset time: no text (the old code did this already)
    ({"pct": 50.0, "resets_at": "not a date"}, None, None),
])
def test_fallback_reset_is_pt_br_for_each_form_of_window_reset_text(tmp_path, win, source, rest):
    good = {"five_hour": win, "seven_day": win}
    assert data.window_reset_text(good, "five_hour", NOW) == source
    five, week, _ = fallback_texts(tmp_path, UsageEntry(last_good=good, age_s=0.0))
    assert (five, week) == ((f"volta {rest}", f"renova {rest}") if rest else ("", ""))


@needs_node
@pytest.mark.parametrize("age_s,source,used", [
    (None, None, "50% usado"),  # fresh: no age (the old code did this already)
    (179, None, "50% usado"),
    (180, "· 3m ago", "50% usado · há 3m"),
    (600, "· 10m ago", "50% usado · há 10m"),
    (7980, "· 2h 13m ago", "50% usado · há 2h 13m"),
    (7200, "· 2h ago", "50% usado · há 2h"),
    (273600, "· 3d 4h ago", "50% usado · há 3d 4h"),
    (259200, "· 3d ago", "50% usado · há 3d"),
])
def test_fallback_age_is_pt_br_for_each_form_of_format_age(tmp_path, age_s, source, used):
    good = {"five_hour": window(), "seven_day": window()}
    assert data.format_age(age_s) == source
    assert fallback_texts(tmp_path, UsageEntry(last_good=good, age_s=age_s))[2] == used


# ---- switch lines in the pill (SL3 gear, SL4 card section, SL5 chip and ticks, SL6 contract) ----

LINES = {"five": 90.0, "week": 95.0, "own5": False, "ownWeek": False}
GENERAL = {"five": 90.0, "week": 95.0}


def lines_card(num=5, five=48.0, week=81.0, lines=LINES, active=True, b=True):
    c = card(num, five, week, active=active)
    if b:
        c["budget"] = budget()
    if lines is not None:
        c["lines"] = dict(lines) if isinstance(lines, dict) else lines
    return c


def lines_payload(*cards, general=GENERAL, open_=False, theme="black"):
    cards = list(cards) or [lines_card()]
    lay = pill.window_layout("right", len(cards), AREA)
    return pill.pill_payload(cards, "right", theme, lay, general_lines=general, card_limits_open=open_)


# A bridge whose calls answer at once, so a test sees the whole chain of a click in one run.
_PRE_LINES = """
  var calls = [], hot = 0;
  const sync = v => ({ then(f) { f(v); return this; } });
  pywebview = { api: {
    set_account_line(n, k, v) { calls.push([n, k, v]); return sync({ ok: true, error: null }); },
    set_card_open(o) { calls.push(['open', o]); return sync({ ok: true }); },
    lines_window() { calls.push(['window']); return sync({ ok: true }); },
    set_hot() { hot++; }, fleet() {}, add() { return sync({}); }, switch() { return sync({}); } } };
  window.pywebview = pywebview;
"""


def run_lines(tmp_path, expr, payload):
    return run_page(tmp_path, "(p => {%s})(%s)" % (expr, json.dumps(payload)), _PRE_DOM + _PRE_LINES)


def text_of(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


TICK = re.compile(r'<line class=tick [^>]*stroke="([^"]+)"[^>]*rotate\(([\d.]+) 21 21\)')


@needs_node
def test_ring_tick_angles(tmp_path):
    # S10: the outer tick at the 5-hour line, the inner tick at the weekly line, line * 3.6 degrees
    it = lines_payload(lines_card(five=40.0, week=30.0, lines={**LINES, "five": 90.0, "week": 95.0}))["items"][0]
    assert TICK.findall(run_page(tmp_path, "ring(%s, 'black')" % json.dumps(it))) == [("#F4EEDB", "324.0"), ("#F4EEDB", "342.0")]
    assert {c for c, _ in TICK.findall(run_page(tmp_path, "ring(%s, 'white')" % json.dumps(it)))} == {"#16161a"}
    # a ring with no percent has no tick; an item with no lines has none at all
    no_five = {**it, "five": None}
    assert TICK.findall(run_page(tmp_path, "ring(%s, 'black')" % json.dumps(no_five))) == [("#F4EEDB", "342.0")]
    bare = {k: v for k, v in it.items() if k != "lines"}
    assert TICK.findall(run_page(tmp_path, "ring(%s, 'black')" % json.dumps(bare))) == []


@needs_node
def test_own_line_dot_class(tmp_path):
    # SL5: an account with any account line gets the warning dot (the item's "own" class)
    pre = "const fd = { className: 'item', querySelector: () => ({ setAttribute() {}, innerHTML: '' }) };"
    own = lines_payload(lines_card(lines={**LINES, "ownWeek": True, "week": 85.0}))["items"][0]
    general = lines_payload(lines_card())["items"][0]
    assert "own" in run_page(tmp_path, "(applyItem(fd, %s), fd.className)" % json.dumps(own), pre).split()
    assert "own" not in run_page(tmp_path, "(applyItem(fd, %s), fd.className)" % json.dumps(general), pre).split()
    assert ".item.own .dot{display:block}" in pill.PILL_PAGE and "box-shadow:0 0 0 2px var(--bg)" in pill.PILL_PAGE


@pytest.mark.parametrize("bad", [
    {**LINES, "five": "80"}, {**LINES, "week": None}, {**LINES, "own5": 1}, {**LINES, "ownWeek": "true"},
    {**LINES, "five": True}, {**LINES, "five": float("nan")}, {k: v for k, v in LINES.items() if k != "own5"},
    "five=80", [90, 95], 7,
])
def test_pill_payload_drops_bad_lines(bad):
    payload = lines_payload(lines_card(lines=bad), general={"five": "90", "week": 95.0}, open_="yes")
    assert "lines" not in payload["items"][0]
    assert payload["generalLines"] is None and payload["cardLimitsOpen"] is False
    good = lines_payload(lines_card(lines={**LINES, "five": 80, "own5": True, "extra": "x"}))
    assert good["items"][0]["lines"] == {"five": 80.0, "week": 95.0, "own5": True, "ownWeek": False}
    assert good["generalLines"] == GENERAL and good["cardLimitsOpen"] is False
    assert lines_payload(lines_card(), open_=True)["cardLimitsOpen"] is True


@needs_node
def test_card_limits_section_collapsed(tmp_path):
    # S14 code side: the card keeps its rows, adds "Limites desta conta" with the chip "geral", no slider
    p = lines_payload()
    out = run_lines(tmp_path, "const first = cardHtml(p.items[0]); render(p); return [first, cardHtml(p.items[0])];", p)
    for html in out:
        text = text_of(html)
        for want in ("Consumo agora", "1% desta conta ≈", "Limites desta conta", "geral"):
            assert want in text, want
        assert "type=range" not in html and "Usar os gerais" not in html
        assert text.index("1% desta conta") < text.index("Limites desta conta")
    # a card with only a message has no row; a card without budget rows keeps it
    msg = lines_payload(lines_card())["items"][0]
    msg.update(rows=[], message="Not signed in")
    assert "Limites desta conta" not in run_page(tmp_path, "cardHtml(%s)" % json.dumps(msg))
    nob = lines_payload(lines_card(b=False))["items"][0]
    assert "Limites desta conta" in run_page(tmp_path, "cardHtml(%s)" % json.dumps(nob))


def ranges(html):
    return re.findall(r"<input type=range[^>]*>", html)


@needs_node
def test_card_limits_section_open(tmp_path):
    # S15 code side: a click opens the section, "Usar os gerais" is selected, both sliders are disabled
    p = lines_payload()
    out = run_lines(tmp_path, "render(p); showCard(0); calls.length = 0; const h0 = hot; cardAct('toggle');"
                              " return {html: cardHtml(p.items[0]), calls, hot: hot - h0};", p)
    assert out["calls"] == [["open", True]] and out["hot"] >= 1
    html = out["html"]
    assert "<span class=on data-act=general>Usar os gerais</span>" in html
    assert "<span data-act=own>Próprios</span>" in html
    sliders = ranges(html)
    assert len(sliders) == 2 and all(" disabled" in s for s in sliders)
    assert "min=50 max=99 step=1" in sliders[0] and "data-kind=five" in sliders[0] and "data-kind=week" in sliders[1]
    assert "Geral: 5 h 90% · sem 95%" in text_of(html) and "Voltar ao geral" in html
    # the payload's open state opens it too; "Próprios" enables the sliders and writes nothing
    out = run_lines(tmp_path, "render(p); showCard(0); calls.length = 0; cardAct('own');"
                              " return {html: cardHtml(p.items[0]), calls};", lines_payload(open_=True))
    assert out["calls"] == [] and "<span class=on data-act=own>Próprios</span>" in out["html"]
    assert [" disabled" in s for s in ranges(out["html"])] == [False, False]


@needs_node
def test_card_own_line_writes_on_release(tmp_path):
    # S16 code side: a drag (input events) writes nothing; the release (change event) writes one key
    ev = "{target: {dataset: {kind: 'week', num: '5'}, value: '90', style: {setProperty() {}}}}"
    out = run_lines(tmp_path, "render(p); showCard(0); cardAct('own'); calls.length = 0;"
                              " onCardInput(%s); const afterInput = calls.slice(); onCardChange(%s);"
                              " return {afterInput, calls, html: cardHtml(p.items[0])};" % (ev, ev),
                    lines_payload(open_=True))
    assert out["afterInput"] == []
    assert out["calls"] == [["5", "week", 90]]
    assert '<span class="chip own">sem 90</span>' in out["html"]
    assert '<span class="lv own" data-v=week>90%</span>' in out["html"]


@needs_node
@pytest.mark.parametrize("act", ["back", "general"])
def test_card_back_to_general(tmp_path, act):
    # S17 code side: "Voltar ao geral" and "Usar os gerais" both clear the two account lines
    own = lines_card(lines={"five": 80.0, "week": 90.0, "own5": True, "ownWeek": True})
    out = run_lines(tmp_path, "render(p); showCard(0); calls.length = 0; cardAct('%s');"
                              " return {calls, html: cardHtml(p.items[0])};" % act, lines_payload(own, open_=True))
    assert out["calls"] == [["5", "five", None], ["5", "week", None]]
    assert '<span class="chip">geral</span>' in out["html"]


@needs_node
def test_line_chip_text(tmp_path):
    cases = [
        {"five": 80, "week": 90, "own5": True, "ownWeek": False},
        {"five": 90, "week": 99.9, "own5": False, "ownWeek": True},
        {"five": 80, "week": 90, "own5": True, "ownWeek": True},
        {"five": 82.5, "week": 90, "own5": True, "ownWeek": False},
        {"five": 90, "week": 95, "own5": False, "ownWeek": False},
    ]
    assert run_page(tmp_path, "%s.map(l => chipText(l))" % json.dumps(cases)) == [
        "5 h 80", "sem 99,9", "5 h 80 · sem 90", "5 h 82,5", "geral"]
    css = pill.PILL_PAGE
    assert ".chip.own{background:rgba(238,155,0,.14);color:var(--warn)}" in css
    assert "background:var(--raised);color:var(--dim)" in css


_PRE_MADE = """
  var __made = [];
  document.createElement = () => { const n = __node(); __made.push(n); return n; };
"""


@needs_node
def test_gear_in_layout(tmp_path):
    # SL3: the gear adds GEAR = 36 px after "+", and its click asks for the lines window
    assert pill.GEAR == 36
    for n in (0, 1, 6):
        assert pill.pill_length(n) == 2 * (pill.FLARE + pill.PAD) + pill.SQUARE + n * pill.ITEM + pill.ADD + 36
    p = lines_payload()
    out = run_page(tmp_path, "(p => { render(p); const kinds = __made.map(n => n.className);"
                             " const g = __made.find(n => n.className === 'gear'); calls.length = 0; g.onclick();"
                             " return {kinds, calls}; })(%s)" % json.dumps(p), _PRE_DOM + _PRE_LINES + _PRE_MADE)
    assert out["kinds"][-2:] == ["add", "gear"] and out["calls"] == [["window"]]
    assert "width:30px;height:30px;border-radius:8px" in re.search(r"\.gear\{[^}]*\}", pill.PILL_PAGE).group(0)


@pytest.mark.parametrize("edge", pill.EDGES)
def test_gear_anchor_is_the_gear_in_screen_pixels(edge, monkeypatch):
    monkeypatch.setattr(pill, "work_area", lambda: AREA)
    ctl = make_controller(edge)
    ctl.cards = [card(1), card(2)]
    lay = pill.window_layout(edge, 2, AREA)
    wx, wy = lay["win"][:2]
    px, py, pw, ph = lay["pill"]
    along = pill.FLARE + pill.PAD + pill.SQUARE + 2 * pill.ITEM + pill.ADD
    want = ([wx + px + (pw - 30) // 2, wy + py + along, 30, 30] if pill.is_vertical(edge)
            else [wx + px + along, wy + py + (ph - 30) // 2, 30, 30])
    a = ctl.gear_anchor()
    assert a == {"edge": edge, "square": want, "area": list(AREA)}
    # the gear ends GEAR - 30 = 6 px before the pill's padded end
    end = (wy + py + ph if pill.is_vertical(edge) else wx + px + pw) - pill.FLARE - pill.PAD
    assert (a["square"][1] if pill.is_vertical(edge) else a["square"][0]) + 30 + 6 == end


def test_bridge_lines_calls_on_lines():
    seen = []
    ctl = pill.PillController(None, None, make_controller().settings, lambda: None, lambda *a: None,
                              on_lines=lambda action, args: seen.append((action, args)) or {"ok": True, "error": None})
    bridge = pill.PillBridge(ctl)
    assert bridge.lines_window() == {"ok": True, "error": None}
    assert bridge.set_account_line("5", "week", 90) == {"ok": True, "error": None}
    assert bridge.set_card_open(True) == {"ok": True, "error": None}
    assert seen == [("toggle_window", {}), ("set_account_line", {"num": "5", "kind": "week", "value": 90}),
                    ("set_card_open", {"open": True})]
    alone = pill.PillBridge(make_controller())
    assert [alone.lines_window(), alone.set_account_line("5", "five", 80), alone.set_card_open(False)] == [{"ok": False}] * 3


def test_push_sends_general_lines_and_card_open(monkeypatch):
    monkeypatch.setattr(pill, "work_area", lambda: AREA)
    ctl = make_controller()
    ctl.settings.pill_card_limits_open = True
    sent = []
    ctl._run_js = sent.append
    ctl._loaded.set()
    ctl._visible = True
    ctl.cards = [lines_card()]
    ctl._placed = ctl._layout()
    ctl.update([lines_card()], general_lines=GENERAL)
    payload = json.loads(sent[-1][len("render("):-1])
    assert payload["generalLines"] == GENERAL and payload["cardLimitsOpen"] is True
    assert payload["items"][0]["lines"] == LINES


@needs_node
def test_no_rebuild_during_drag(tmp_path):
    # SL4: a refresh push leaves the card alone while a slider is held, and applies on release
    p = lines_payload(open_=True)
    out = run_lines(tmp_path, "render(p); showCard(0); cardAct('own');"
                              " const q = JSON.parse(JSON.stringify(p)); q.items[0].rows[0].pct = 77; q.items[0].five = 77;"
                              " dragStart(); const n = __wrote.length; render(q); const during = __wrote.slice(n);"
                              " dragEnd(); const after = __wrote.slice(n);"
                              " const want = cardHtml(q.items[0]) + '<div class=arrow></div>';"
                              " return {during: during.includes(want), after: after.includes(want),"
                              " own: want.includes('class=on data-act=own'), seen: want.includes('77%')};", p)
    assert out == {"during": False, "after": True, "own": True, "seen": True}  # the "Próprios" pick survives the push


FAKE_ID = "a@example.com"


@needs_node
def test_pill_lines_payload_has_no_identity(tmp_path):
    # S9 pill part: email, org name and an alias that holds an email never reach the payload or the page
    cards = []
    for n, lines in enumerate([LINES, {**LINES, "own5": True, "five": 80.0}, {**LINES, "ownWeek": True}], 1):
        c = lines_card(num=n, lines={**lines, "email": FAKE_ID, "alias": FAKE_ID}, active=n == 1)
        c.update(title=f"#{n}  {FAKE_ID}", tag=f"{FAKE_ID}'s Organization", email=FAKE_ID, org_name=FAKE_ID)
        c["budget"] = budget(alias=FAKE_ID)
        cards.append(c)
    payload = lines_payload(*cards, general={**GENERAL, "email": FAKE_ID}, open_=True)
    sent = json.dumps(payload)
    assert "@" not in sent
    out = run_lines(tmp_path, "render(p); const html = p.items.map((it, i) => (showCard(i), cardAct('own'), cardHtml(it)));"
                              " return {html, wrote: __wrote};", payload)
    assert len(out["html"]) == 3 and all("Limites desta conta" in h and "type=range" in h for h in out["html"])
    assert [t for t in [*out["html"], *out["wrote"]] if "@" in t or "Organization" in t] == []
