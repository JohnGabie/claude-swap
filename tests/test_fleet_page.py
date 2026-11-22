"""fleet_page: the fleet panel page, its placement, and the data the tray feeds it (FP4-FP12)."""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from claude_swap import decision_log, fleet_page, fleet_view, pill, token_model, tray

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def page_script() -> str:
    m = re.search(r"<script>(.*)</script>", fleet_page.FLEET_PAGE, re.S)
    assert m
    return m.group(1)


# ---- the page ----------------------------------------------------------------


@needs_node
def test_page_script_passes_node_check(tmp_path):
    path = tmp_path / "panel.js"
    path.write_text(page_script(), encoding="utf-8")
    run = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr


def _oklch(r: float, g: float, b: float) -> tuple[float, float, float]:
    def lin(c):
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = lin(r), lin(g), lin(b)
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    A = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    B = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return L, math.hypot(A, B), math.degrees(math.atan2(B, A)) % 360


def _page_colors(text: str) -> list[tuple[str, tuple[float, float, float]]]:
    out = []
    for h in re.findall(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", text):
        full = h if len(h) == 6 else "".join(c * 2 for c in h)
        out.append(("#" + h, tuple(int(full[i:i + 2], 16) for i in (0, 2, 4))))
    for r, g, b in re.findall(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", text):
        out.append((f"rgb({r},{g},{b})", (int(r), int(g), int(b))))
    return out


FP2_DARK = ("#0A9396", "#1BB5B8", "#087A7D", "#06686A", "#055557", "#044345", "#05282A", "#94D2BD",
            "#1D3A31", "#EE9B00", "#CF4332", "#F4EEDB", "#CFC6A8", "#8F8873", "#121A1C")


def test_page_uses_fp2_colors_and_no_purple_hue():
    page = fleet_page.FLEET_PAGE.upper()
    missing = [c for c in FP2_DARK if c not in page]
    assert not missing, missing
    assert "RGBA(233,216,166,.08)" in page.replace(" ", "")  # grid
    bad = [name for name, rgb in _page_colors(fleet_page.FLEET_PAGE)
           if (lambda L, C, h: C >= 0.05 and 290 <= h <= 345)(*_oklch(*rgb))]
    assert not bad, bad


def test_page_defines_sections_and_keys():
    script = page_script()
    assert re.search(r"window\.__sections\s*=", script)
    # Esc and the close button call close(); the period selector has the seven keys, 7 dias by default
    assert "Escape" in script and "api.close()" in script
    for key in fleet_view.PERIODS:
        assert re.search(rf"data-r={key}[ >]", fleet_page.FLEET_PAGE), key
    assert "data-r=7d class=on>7 dias<" in fleet_page.FLEET_PAGE
    assert "carregando" in fleet_page.FLEET_PAGE
    # the subscription marker of the 72-hour strip is out of scope (FP0)
    assert "assinatura" not in fleet_page.FLEET_PAGE


# ---- placement ---------------------------------------------------------------

AREA = [0, 30, 1920, 1050]


def anchor(edge, square, area=AREA):
    return {"edge": edge, "square": list(square), "area": list(area)}


def test_panel_position_beside_the_pill_on_each_edge():
    inset = (pill.THICKNESS - pill.SQUARE_SIZE) // 2
    gap = fleet_page.PANEL_GAP
    W, H = fleet_page.PANEL_W, fleet_page.PANEL_H
    # right: the panel ends left of the pill, top aligned with the square
    assert fleet_page.panel_position(anchor("right", (1863, 100, 46, 46))) == (1863 - inset - gap - W, 100)
    # left: the panel starts right of the pill
    assert fleet_page.panel_position(anchor("left", (11, 100, 46, 46))) == (11 + 46 + inset + gap, 100)
    # top: below the pill, left aligned with the square
    assert fleet_page.panel_position(anchor("top", (600, 41, 46, 46))) == (600, 41 + 46 + inset + gap)
    # bottom: above the pill
    assert fleet_page.panel_position(anchor("bottom", (600, 1023, 46, 46))) == (600, 1023 - inset - gap - H)


def test_panel_position_clamped_to_the_work_area():
    W, H = fleet_page.PANEL_W, fleet_page.PANEL_H
    # a square low on a right pill: the panel stops at the bottom of the area
    assert fleet_page.panel_position(anchor("right", (1863, 900, 46, 46))) == (1863 - 11 - fleet_page.PANEL_GAP - W,
                                                                               AREA[1] + AREA[3] - H)
    # a square far right on a top pill: the panel stops at the right of the area
    assert fleet_page.panel_position(anchor("top", (1800, 41, 46, 46)))[0] == AREA[0] + AREA[2] - W
    # an area smaller than the panel: the panel starts at the area's corner
    assert fleet_page.panel_position(anchor("left", (11, 100, 46, 46), area=(100, 50, 600, 500))) == (100, 50)


# ---- card budget -------------------------------------------------------------

NOW = datetime(2026, 11, 21, 22, 0, 0, tzinfo=timezone.utc).timestamp()


def budget_account(num=4, **over):
    a = {"id": num, "plan": "pro", "fetchedAt": "2026-11-21T21:59:00Z", "status": "ok",
         "fiveHour": {"pct": 33.0, "resetsAt": "2026-11-22T00:29:00Z"},
         "sevenDay": {"pct": 95.0, "resetsAt": "2026-11-22T05:59:00Z", "elapsedPct": 66.0,
                      "projectedEndPct": 100.0, "tokensPerPercent": 1_200_000, "remainingPct": 5.0,
                      "remainingTokens": 6_000_000}}
    a.update(over)
    return a


def test_card_budget_field_rules():
    b = fleet_page.card_budget(budget_account(), "work", 42000.0, NOW)
    assert b == {"plan": "pro", "fiveResetAt": "2026-11-22T00:29:00Z", "weekResetAt": "2026-11-22T05:59:00Z",
                 "weekElapsedPct": 66.0, "projectedEndPct": 100.0, "tokensPerPercent": 1_200_000,
                 "nowPerMin": 42000.0, "alias": "work", "readAgeS": 60.0}
    bad = budget_account(plan="Max <b>", fetchedAt="yesterday",
                         fiveHour={"pct": 1, "resetsAt": 1795302682},
                         sevenDay={"resetsAt": "tomorrow", "elapsedPct": True, "projectedEndPct": "100",
                                   "tokensPerPercent": float("nan")})
    b = fleet_page.card_budget(bad, "me@example.com", True, NOW)
    assert b == {"plan": "unknown", "fiveResetAt": None, "weekResetAt": None, "weekElapsedPct": None,
                 "projectedEndPct": None, "tokensPerPercent": None, "nowPerMin": None, "alias": None,
                 "readAgeS": None}
    assert fleet_page.card_budget(budget_account(plan="max\n"), None, None, NOW)["plan"] == "unknown"


def test_cards_get_budget_by_text_id():
    cards = [{"num": "1", "active": False}, {"num": "4", "active": True}, {"num": "9", "active": False}]
    budget = {"accounts": [budget_account(1), budget_account(4, plan="max")]}
    out = fleet_page.with_budget(cards, budget, {"4": "work"}, 42000.0, NOW)
    assert out[0]["budget"]["plan"] == "pro" and out[0]["budget"]["nowPerMin"] is None
    assert out[1]["budget"]["plan"] == "max" and out[1]["budget"]["alias"] == "work"
    assert out[1]["budget"]["nowPerMin"] == 42000.0  # "Consumo agora" is the active account's
    assert "budget" not in out[2]
    assert "budget" not in cards[1]  # the tray's own cards are not changed


# ---- decision events ---------------------------------------------------------


def test_read_decisions_drops_bad_lines(tmp_path):
    lines = ['{"event": "switch", "ts": "2026-11-21T10:00:00Z"}', "not json", "[1, 2]", '{"event": "poll"',
             '{"event": "poll", "ts": "2026-11-21T10:01:00Z"}', "[" * 100000]
    decision_log.log_path(tmp_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert fleet_page.read_decisions(tmp_path) == [
        {"event": "switch", "ts": "2026-11-21T10:00:00Z"}, {"event": "poll", "ts": "2026-11-21T10:01:00Z"}]


# ---- history worker and panel API ------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _log_line(ts, mid, model, cwd, out=1000):
    return json.dumps({"type": "assistant", "timestamp": _iso(ts), "requestId": "r" + mid, "cwd": cwd,
                       "message": {"id": mid, "model": model, "usage": {"input_tokens": 10, "output_tokens": out}}}) + "\n"


@pytest.fixture
def fake_home(tmp_path):
    from pathlib import Path

    home = Path.home()  # the throwaway home of conftest's _isolate_real_home, never the real one
    assert "isolated_home" in str(home)
    root = tmp_path / "root"
    projects = tmp_path / "claude" / "projects"
    (projects / "p1").mkdir(parents=True)
    return home, root, projects


def test_history_to_panel_join(fake_home):
    home, root, projects = fake_home
    now = time.time()
    log = projects / "p1" / "s.jsonl"
    log.write_text(
        _log_line(now - 600, "a", "claude-opus-5-5", f"{home}/projects/guilda")
        + _log_line(now - 1200, "b", "claude-haiku-4-5-20251001", f"{home}/projects/claude-swap.worktrees/x"),
        encoding="utf-8")
    history = fleet_page.FleetHistory(root, projects)
    panel = fleet_page.FleetPanelApi(history, lambda: {"accounts": []}, lambda: [], lambda: None)
    assert panel.get("1h") == {"loading": True}
    history.refresh()
    p = panel.get("1h")
    assert [m["label"] for m in p["models"]] == ["Opus 5.5", "Haiku 4.5"]
    assert [r["key"] for r in p["projects"]["rows"][:2]] == ["guilda", "claude-swap"]
    opus = token_model.weighted_tokens("claude-opus-5-5", {"input_tokens": 10, "output_tokens": 1000})
    haiku = token_model.weighted_tokens("claude-haiku-4-5-20251001", {"input_tokens": 10, "output_tokens": 1000})
    assert p["tiles"]["nowPerMin"] == pytest.approx((opus + haiku) / 60)
    assert sum(p["series"]["past"]) * p["series"]["step"] / 60 == pytest.approx(opus + haiku)
    # an unknown period falls back to the default one
    assert panel.get("2y")["period"] == fleet_page.DEFAULT_PERIOD
    assert panel.get(["7d"])["period"] == fleet_page.DEFAULT_PERIOD


def test_worker_error_logs_only_the_type_name(tmp_path, caplog):
    def boom(*_a, **_k):
        raise ValueError(f"cannot read {tmp_path}/secret for me@example.com")

    history = fleet_page.FleetHistory(tmp_path / "root", tmp_path / "projects", update=boom)
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        data = history.refresh()
    assert data["buckets"] == {}  # the page leaves "carregando…" with an empty history
    text = caplog.text
    assert "ValueError" in text
    assert "secret" not in text and "@" not in text and str(tmp_path) not in text


def test_history_worker_updates_until_stopped(tmp_path):
    import threading

    calls = []
    stop = threading.Event()

    def update(root, projects, now):
        calls.append(now)
        if len(calls) == 2:
            stop.set()
        return {"buckets": {}}

    history = fleet_page.FleetHistory(tmp_path, tmp_path, update=update, clock=lambda: 7.0)
    worker = threading.Thread(target=history.run, args=(stop,), kwargs={"interval": 0.01}, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and calls == [7.0, 7.0]


def test_panel_close_calls_back():
    closed = []
    panel = fleet_page.FleetPanelApi(None, dict, list, lambda: closed.append(1))
    assert panel.close() == {"ok": True} and closed == [1]


# ---- privacy -----------------------------------------------------------------


def test_pill_and_panel_payloads_hold_no_email(fake_home):
    home, root, projects = fake_home
    now = time.time()
    (projects / "p1" / "s.jsonl").write_text(
        _log_line(now - 600, "a", "claude-opus-5-5", f"{home}/projects/me@example.com"), encoding="utf-8")
    history = fleet_page.FleetHistory(root, projects)
    history.refresh()
    email = "me@example.com"
    cards = [{"num": "1", "title": f"#1  {email}", "tag": f"{email}'s Organization", "active": True,
              "disabled": False, "rows": [{"label": "5h", "pct": 10.0, "reset": None, "age": None}],
              "message": None, "alert": None, "rank": 0}]
    budget = {"fleet": {"paceRatio": 0.9, "weekUsedPct": 50.0, "weekElapsedPct": 55.0, "usableNow": 1, "accounts": 1},
              "accounts": [budget_account(1, plan=email, email=email, orgName=f"{email}'s Organization")]}
    shown, square = tray.pill_inputs(cards, budget, {"1": email}, history.data(), now)
    lay = pill.window_layout("right", 1, (0, 0, 1920, 1080))
    pill_text = json.dumps(pill.pill_payload(shown, "right", "black", lay, fleet=square))
    panel = fleet_page.FleetPanelApi(history, lambda: budget, lambda: [{"event": "switch", "ts": _iso(now - 60),
                                                                         "email": email}], lambda: None)
    panel_text = json.dumps([panel.get(p) for p in fleet_view.PERIODS])
    assert "@" not in pill_text
    assert "@" not in panel_text


def test_budget_and_mcp_do_not_import_usage_history():
    code = ("import sys, claude_swap.budget_source, claude_swap.mcp_server, claude_swap.cli;"
            "print([m for m in sys.modules if m.endswith('usage_history')])")
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "[]"
