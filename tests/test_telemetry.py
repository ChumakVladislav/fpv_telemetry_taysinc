"""Regression tests for Taisync Telemetry.

Runs headless. Either:

    python tests/test_telemetry.py          # no extra dependencies
    python -m pytest tests                  # if pytest is installed

The fixture `fixtures/live_samples.json` is a real capture from the ground
unit (55 fields, 1410 MHz link). Several of the bugs these tests pin down --
AIR QUALITY latching at 20 %, "GND overflow" never clearing -- only show up
with the hardware's cumulative counters, so the tests use its actual data.
"""

import copy
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAISYNC_OFFLINE"] = "1"

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live_samples.json"

_spec = importlib.util.spec_from_file_location("telemetry_app", ROOT / "fpv_telemetry_v1_39.py")
app_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app_mod)

from PySide6.QtWidgets import QApplication  # noqa: E402

QAPP = QApplication.instance() or QApplication([])
LIVE = json.loads(FIXTURE.read_text(encoding="utf-8"))["samples"]


def drain(ms: int = 0):
    end = time.perf_counter() + ms / 1000.0
    QAPP.processEvents()
    while time.perf_counter() < end:
        QAPP.processEvents()


def make_window():
    win = app_mod.MainWindow()
    win.resize(1280, 900)
    win.show()
    drain(50)
    win._apply_responsive_layout()
    drain()
    return win


def close_window(win):
    win.close()
    drain()


def advance(sample, seconds, ldpc_err_rate=0.0, overflow=0):
    """Return a copy of `sample` moved forward in time like the real unit."""
    out = copy.deepcopy(sample)
    for side, fps in (("air", 370), ("ground", 5000)):
        frames = int(fps * seconds)
        fails = int(round(frames * ldpc_err_rate))
        out[f"{side}LdpcFail"] += fails
        out[f"{side}LdpcPass"] += frames - fails
    out["groundTxOverflowCnt"] += overflow
    return out


# --------------------------------------------------------------- pure functions

def test_rssi_scale_no_longer_saturates_on_real_link():
    s = LIVE[-1]
    air_best = max(s["airRSSI0"], s["airRSSI1"])           # -20 dBm
    ground_best = max(s["groundRSSI0"], s["groundRSSI1"])  # -54 dBm
    air_pct = app_mod.best_rssi_to_percent(air_best)
    ground_pct = app_mod.best_rssi_to_percent(ground_best)
    assert air_pct == 100
    assert ground_pct < 100, "a 34 dB weaker ground signal must read lower than air"
    assert app_mod.best_rssi_to_percent(app_mod.RSSI_MIN_DBM) == 0


def test_ldpc_rate_mapping():
    f = app_mod.ldpc_rate_to_percent
    assert f(None) is None
    assert f(0) == 100
    assert f(0.002) >= 99
    assert f(0.1) == 90
    assert f(1.0) == 70
    assert f(5.0) == 30
    assert f(50) == 0
    assert f(0.5) > f(2.0) > f(8.0)


def test_parse_mcs_and_antenna():
    assert app_mod.parse_mcs("16QAM_1_2(7.86Mbps)") == ("16QAM 1/2", 7.86)
    assert app_mod.parse_mcs("weird") == ("weird", None)
    assert app_mod.parse_mcs(None) == ("N/A", None)
    assert app_mod.active_antenna("auto-rf2") == ("auto", 1)
    assert app_mod.active_antenna("auto-rf1") == ("auto", 0)
    assert app_mod.active_antenna("manual rf1") == ("manual", 0)
    assert app_mod.active_antenna(None) == ("", None)


def test_tx_power_formatting():
    assert app_mod.format_tx_power(1) == "TX 1 dBm · 1.3 mW"
    assert app_mod.format_tx_power(27) == "TX 27 dBm · 501 mW"
    assert app_mod.format_tx_power(None) == "TX --"


def test_counter_window_deltas_and_reboot():
    cw = app_mod.CounterWindow(window=3)
    cw.update({"x": 100}, ["x"])
    assert cw.recent("x") is None, "no delta from a single sample"
    for v in (110, 130, 160, 200):
        cw.update({"x": v}, ["x"])
    assert cw.recent("x") == 20 + 30 + 40, "only the last 3 deltas count"
    cw.update({"x": 5}, ["x"])  # went backwards: unit rebooted
    assert cw.recent("x") is None


# --------------------------------------------- the "AIR stuck at 20 %" bug

def test_air_quality_does_not_decay_with_lifetime_counter():
    """The bug the user saw in the field: AIR at 20 % and never moving.

    Old code mapped the lifetime fail *total* to quality, so a perfect link
    still reached CRIT once the total passed 100. Hold the link perfectly
    clean, let the totals grow far past that, and quality must stay GOOD.
    """
    win = make_window()
    try:
        sample = copy.deepcopy(LIVE[-1])
        sample["airLdpcFail"] = 5000   # lifetime total way past the old 100 cliff
        sample["groundLdpcFail"] = 5000
        for _ in range(8):
            win.on_data(sample)
            drain()
            sample = advance(sample, 1.2, ldpc_err_rate=0.0)
        s = win.last_state
        assert s.air_ldpc_rate == 0.0
        assert s.air_quality_pct == 100, f"clean link must read 100%, got {s.air_quality_pct}"
        assert s.ground_quality_pct == 100
        assert app_mod.quality_status(s.air_quality_pct) == "GOOD"
    finally:
        close_window(win)


def test_air_quality_reacts_to_real_errors_and_recovers():
    win = make_window()
    try:
        sample = copy.deepcopy(LIVE[-1])
        for _ in range(6):
            sample = advance(sample, 1.2, ldpc_err_rate=0.04)  # 4 % FEC errors
            win.on_data(sample)
            drain()
        degraded = win.last_state.air_quality_pct
        assert degraded <= 40, f"4% FEC errors must degrade quality, got {degraded}"
        assert any("FEC errors" in m for m in win.warn_label.text().split(" | "))
        for _ in range(7):
            sample = advance(sample, 1.2, ldpc_err_rate=0.0)
            win.on_data(sample)
            drain()
        recovered = win.last_state.air_quality_pct
        assert recovered == 100, f"quality must recover once errors stop, got {recovered}"
        assert "FEC" not in win.warn_label.text()
    finally:
        close_window(win)


def test_ground_quality_now_uses_fec_too():
    win = make_window()
    try:
        sample = copy.deepcopy(LIVE[-1])
        for _ in range(6):
            sample = advance(sample, 1.2, ldpc_err_rate=0.06)
            win.on_data(sample)
            drain()
        assert win.last_state.ground_quality_pct < 50, "ground must reflect FEC errors as well"
    finally:
        close_window(win)


# --------------------------------------------- warnings

def test_historical_overflow_does_not_warn_forever():
    """Live unit reported groundTxOverflowCnt=8 the whole time: a past event."""
    assert LIVE[0]["groundTxOverflowCnt"] == LIVE[-1]["groundTxOverflowCnt"] == 8
    win = make_window()
    try:
        for sample in LIVE:
            win.on_data(sample)
            drain()
        assert "overflow" not in win.warn_label.text().lower()
        assert win.warn_bar.isHidden(), f"clean live link should show no warnings: {win.warn_label.text()}"
    finally:
        close_window(win)


def test_new_overflow_warns_then_clears():
    win = make_window()
    try:
        sample = copy.deepcopy(LIVE[-1])
        win.on_data(sample)
        sample = advance(sample, 1.2, overflow=3)
        win.on_data(sample)
        drain()
        assert "GND TX overflow +3" in win.warn_label.text()
        for _ in range(win.counters.window + 1):
            sample = advance(sample, 1.2)
            win.on_data(sample)
            drain()
        assert "overflow" not in win.warn_label.text().lower(), "warning must clear on its own"
    finally:
        close_window(win)


def test_warning_priority_and_no_duplicates():
    data = dict(LIVE[-1], UAV="offline", airAD9361Temp=70, groundAD9361Temp=65)
    msgs = app_mod.warning_messages(data, 400, 300, False, False)
    assert msgs[0] == "UAV offline", f"link loss must come first, got {msgs}"
    assert "FPV unreachable" not in msgs, "UAV and FPV are the same signal"
    assert "RC unreachable" in msgs


def test_clean_live_payload_has_no_warnings():
    assert app_mod.warning_messages(LIVE[-1], 5, 3, True, True) == []
    assert app_mod.warning_messages({}, None, None, None, None) == []


# --------------------------------------------- what the user asked to see

def test_both_antennas_and_tx_power_visible():
    win = make_window()
    try:
        s = LIVE[-1]
        win.on_data(s)
        drain()
        air, gnd = win.air_signal_card, win.ground_signal_card
        assert air.bars[0].dbm == s["airRSSI0"] and air.bars[1].dbm == s["airRSSI1"]
        assert gnd.bars[0].dbm == s["groundRSSI0"] and gnd.bars[1].dbm == s["groundRSSI1"]
        assert air.bars[1].active and not air.bars[0].active      # auto-rf2
        assert gnd.bars[0].active and not gnd.bars[1].active      # auto-rf1
        assert air.tx.text() == "TX 1 dBm · 1.3 mW"
        assert gnd.tx.text() == "TX 27 dBm · 501 mW"
        assert win.link_card._value_text == "16QAM 1/2"
        assert "7.86 Mb/s" in win.link_card._detail_text and "1410 MHz" in win.link_card._detail_text
        assert win.control_card._value_text == "BIND"
    finally:
        close_window(win)


def test_compact_overlay_shows_both_antennas():
    win = make_window()
    try:
        win.toggle_compact_view()
        drain()
        s = LIVE[-1]
        win.on_data(s)
        drain()
        text = win.quick_air_signal.text()
        assert f"{s['airRSSI0']} / {s['airRSSI1']}" in text, text
        assert "TX 1" in text
        assert f"{s['groundRSSI0']} / {s['groundRSSI1']}" in win.quick_ground_signal.text()
        win.toggle_compact_view()
        drain()
    finally:
        close_window(win)


def test_traffic_chart_autoscales_to_real_traffic():
    win = make_window()
    try:
        win.view_tabs.setCurrentIndex(1)
        drain(50)
        for sample in LIVE:
            win.on_data(sample)
            drain()
        chart = win.traffic_chart
        peak_down = max(app_mod.parse_kbs(x["downlinkDataRate"]) for x in LIVE)
        assert chart._down_top is not None and peak_down <= chart._down_top <= peak_down * 3
        assert chart._up_top <= 10, "1 kb/s uplink must not be drawn on a 3000 kb/s axis"
        assert "7.86 Mb/s" in chart._painted_stats
        win.view_tabs.setCurrentIndex(0)
    finally:
        close_window(win)


def test_traffic_tab_before_any_data():
    """Opening Traffic while the unit is unreachable must not raise."""
    win = make_window()
    try:
        win.view_tabs.setCurrentIndex(1)
        drain(50)
        win.view_tabs.setCurrentIndex(0)
        drain()
        win.view_tabs.setCurrentIndex(1)
        drain(50)
        assert "Waiting" in win.traffic_chart.stats.text()
        win.view_tabs.setCurrentIndex(0)
    finally:
        close_window(win)


# --------------------------------------------- UI regressions from v1.39

def test_inline_titles_track_live_values():
    win = make_window()
    try:
        win.resize(700, 700)
        drain(100)
        win._apply_responsive_layout()
        drain()
        sample = copy.deepcopy(LIVE[-1])
        for temp in (40, 47, 55):
            sample = advance(sample, 1.2)
            sample["airAD9361Temp"] = temp
            win.on_data(sample)
            drain()
            assert f"{temp} °C" in win.air_temp_card.title.text() or f"{temp} °C" in win.air_temp_card.title.toolTip()
    finally:
        close_window(win)


def test_zoom_survives_data_poll():
    win = make_window()
    try:
        before = win._painted_style_factor
        win.adjust_scale(0.08)
        win.adjust_scale(0.08)
        zoomed = win._painted_style_factor
        assert zoomed != before
        win.on_data(LIVE[-1])
        drain()
        assert win._painted_style_factor == zoomed
        win.reset_scale()
    finally:
        close_window(win)


def test_demo_mode_isolated_from_real_data():
    win = make_window()
    try:
        win.toggle_demo()
        drain()
        assert "TEST DATA" in win.title_bar.title.text()
        for _ in range(3):
            win._demo_tick()
            drain()
        demo_state = win.last_state
        win.on_data(LIVE[-1])
        win.on_error("timeout")
        assert win.last_state is demo_state, "real data leaked into test mode"
        assert win.last_state.air_tx_dbm == 1 and win.last_state.mcs_mbps is not None
        win.toggle_demo()
        drain()
        assert "TEST DATA" not in win.title_bar.title.text()
    finally:
        close_window(win)


def test_poll_cost_and_no_restyle():
    win = make_window()
    calls = {"n": 0}
    original = win.setStyleSheet

    def counting(sheet):
        calls["n"] += 1
        return original(sheet)

    win.setStyleSheet = counting
    try:
        sample = copy.deepcopy(LIVE[-1])
        win.on_data(sample)
        drain()
        start = time.perf_counter()
        for _ in range(60):
            sample = advance(sample, 1.2)
            win.on_data(sample)
            drain()
        per_poll_ms = (time.perf_counter() - start) * 1000 / 60
        assert calls["n"] == 0, f"{calls['n']} full-window restyles during polling"
        assert per_poll_ms < 15, f"{per_poll_ms:.1f} ms per poll"
    finally:
        close_window(win)


def _run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
