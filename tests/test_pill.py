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
