"""Pure parts of the screen-edge pill: geometry, outline, payloads, settings."""

import json

import pytest

from claude_swap import pill
from claude_swap.tray import TraySettings


def card(num=1, five=50.0, week=10.0, active=False):
    return {
        "num": num, "title": f"#{num} a@b.c", "tag": "Pro", "active": active, "disabled": False,
        "message": None,
        "rows": [{"label": "5h", "pct": five, "reset": "in 1h", "age": None},
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
    assert [r["label"] for r in first["rows"]] == ["5h", "7d"] and first["title"] and first["tag"] == "Pro"
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
    const stub = () => new Proxy(function () {}, {
      get: (t, k) => k === Symbol.toPrimitive ? () => 0 : stub(), apply: () => stub(), set: () => true });
    const ctx = vm.createContext({ document: stub(), window: { addEventListener() {} }, requestAnimationFrame() {},
      setTimeout() {}, clearTimeout() {}, Date, Math, JSON });
    vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
    const out = vm.runInContext(process.argv[3], ctx);
    process.stdout.write(JSON.stringify(out));
""")


def run_page(tmp_path, expr: str):
    (tmp_path / "page.js").write_text(page_script(), encoding="utf-8")
    (tmp_path / "driver.js").write_text(_DRIVER, encoding="utf-8")
    run = subprocess.run(["node", str(tmp_path / "driver.js"), str(tmp_path / "page.js"), expr],
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
def test_card_without_budget_keeps_todays_rows_in_the_new_style(tmp_path):
    html = card_html(tmp_path, item_for(nobudget=True))
    assert "Current session" in html and "33% used" in html and "Sessão de 5 h" not in html
    assert "class=bar" in html and "Consumo agora" not in html


@needs_node
def test_card_of_an_account_with_no_reading_shows_its_message_and_no_bars(tmp_path):
    it = item_for()
    it.update(rows=[], message="Not signed in")
    html = card_html(tmp_path, it)
    assert "Not signed in" in html and "class=bar" not in html and "class=mb" not in html


@needs_node
def test_card_bar_follows_the_level_rule_and_week_is_always_available(tmp_path):
    levels = run_page(tmp_path, "[0, 69.9, 70, 89.9, 90, 100, null].map(lvl)")
    assert levels == [0, 0, 1, 1, 2, 2, 0]
    html = card_html(tmp_path, item_for(five=95.0, week=10.0, b={"projectedEndPct": 20.0}))
    five, week = html.split("Semana")
    assert "l2" in five and "l2" not in week


def test_page_ring_colors_come_from_the_palette_by_level():
    css = pill._THEME_CSS
    for token in ("--spent:#0A9396", "--avail:#94D2BD", "--warn:#EE9B00", "--crit:#CF4332", "--track:#1E2426",
                  "--spent:#087A7D", "--avail:#2F8A6F", "--warn:#B87700", "--crit:#C0341F", "--track:#dedcd5"):
        assert token in css, token
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
    assert "querySelectorAll('.item')" in page  # cellAt counts accounts only
    assert "class=item" not in re.search(r"function buildSquare.*?\n}", page, re.S).group(0)
    assert "P.square" in re.search(r"function placeCard.*?\n}", page, re.S).group(0)


def test_square_is_inside_the_pill_length_and_the_payload():
    assert pill.SQUARE == 56
    assert pill.pill_length(0) == 2 * (pill.FLARE + pill.PAD) + pill.SQUARE + pill.ADD
    assert pill.pill_length(3) - pill.pill_length(2) == pill.ITEM
    lay = pill.window_layout("right", 2, AREA)
    fleet = {"sparkline24h": [0.0] * 24, "pace": 86, "level": "neutral"}
    payload = pill.pill_payload([card(1), card(2)], "right", "black", lay, fleet=fleet)
    assert payload["fleet"] == fleet and payload["square"] == pill.SQUARE
    assert pill.pill_payload([card(1)], "right", "black", lay)["fleet"] is None
    assert payload["size"][1] == pill.pill_length(2)


def test_card_is_300_wide_and_the_window_has_room_for_it():
    assert pill.CARD_W == 300
    assert pill.CARD_MAX_H >= 330


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


@pytest.mark.parametrize("edge", pill.EDGES)
def test_panel_anchor_is_the_square_in_screen_pixels(edge, monkeypatch):
    monkeypatch.setattr(pill, "work_area", lambda: AREA)
    ctl = make_controller(edge)
    ctl.cards = [card(1), card(2)]
    a = ctl.panel_anchor()
    lay = pill.window_layout(edge, 2, AREA)
    wx, wy = lay["win"][:2]
    px, py, pw, ph = lay["pill"]
    sx, sy, sw, sh = a["square"]
    assert a["edge"] == edge and a["area"] == list(AREA) and (sw, sh) == (46, 46)
    along = pill.FLARE + pill.PAD
    if pill.is_vertical(edge):
        assert (sx, sy) == (wx + px + (pw - 46) // 2, wy + py + along)
    else:
        assert (sx, sy) == (wx + px + along, wy + py + (ph - 46) // 2)
    ctl._placed = lay  # the placed layout wins when there is one
    assert ctl.panel_anchor()["square"] == a["square"]


def test_controller_update_keeps_the_fleet_for_the_payload():
    ctl = make_controller()
    fleet = {"sparkline24h": [1.0] * 24, "pace": 70, "level": "warning"}
    ctl.update([card(1)], fleet)
    assert ctl.fleet_data == fleet
