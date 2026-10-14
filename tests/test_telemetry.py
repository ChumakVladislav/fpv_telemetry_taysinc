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
import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAISYNC_OFFLINE"] = "1"
# Settings, window position and flight logs go to a throwaway directory, never
# to the real %APPDATA%\TaisyncTelemetry.
_DATA_TMP = tempfile.TemporaryDirectory(prefix="taisync_test_")
os.environ["TAISYNC_DATA_DIR"] = _DATA_TMP.name

ROOT = Path(__file__).resolve().parent.parent
APP_FILE = ROOT / "fpv_telemetry_v1_40.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live_samples.json"

_spec = importlib.util.spec_from_file_location("telemetry_app", APP_FILE)
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
    finally:
        close_window(win)


def test_no_control_card_rc_latency_instead():
    """SBUS/PPM is irrelevant (RC goes over UART as CRSF) and BIND duplicated
    TRACK, so the CONTROL card is gone; its slot shows RC ping."""
    win = make_window()
    try:
        assert not hasattr(win, "control_card")
        assert win.rc_host == app_mod.DEFAULT_RC_HOST == "192.168.199.23"
        win.on_net_results({"hosts": win._host_map(), "latency": {"air": 5, "ground": 3, "rc": 7}})
        win.on_data(LIVE[-1])
        drain()
        assert win.rc_latency_card._value_text == "7 ms"
        assert "192.168.199.23" in win.rc_latency_card._detail_text
        texts = " ".join(c._base_title + str(getattr(c, "_detail_text", "")) for c in win._cards)
        assert "SBUS" not in texts and "PPM" not in texts and "BIND" not in texts
        assert "sbusRxCnt" not in app_mod.COUNTER_KEYS
    finally:
        close_window(win)


def test_settings_dialog_prefills_rc_ip():
    dlg = app_mod.SettingsDialog("", "", "")
    values = dlg.values()
    assert values["rc_host"] == "192.168.199.23"
    assert values["ground_host"] == app_mod.DEFAULT_GROUND_HOST
    dlg.rc_edit.setText("10.0.0.5")
    dlg.reset_defaults()
    assert dlg.values()["rc_host"] == "192.168.199.23"


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
        win.resize(450, 700)  # narrow: single column, values inline
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


# --------------------------------------------- v1.40: layout breakpoints

def _layout_at(win, w, h):
    win.resize(w, h)
    drain(30)
    win._apply_responsive_layout()
    drain()
    return win._single_column, win._inline_cards, win._status_stacked


def test_layout_breakpoints():
    """compact overlay / medium (one column) / large (two columns).

    v1.39 needed 860 px of panel *and* 760 px of height for two columns, so a
    wide-but-short window was stuck in one long column.
    """
    win = make_window()
    try:
        single, inline, stacked = _layout_at(win, 420, 900)
        assert single and inline and stacked, "narrow window: one column, values inline"
        single, inline, _ = _layout_at(win, 680, 720)
        assert not single, "680 px wide is enough for two columns"
        assert not inline, "two columns at 720 px high still fit full cards"
        single, _, stacked = _layout_at(win, 1280, 700)
        assert not single and not stacked, "wide but short: two columns, not one tall one"
        single, inline, _ = _layout_at(win, 420, 1100)
        assert single and not inline, "tall narrow window has room for full cards"
        # Hysteresis: just under the breakpoint going back keeps two columns...
        _layout_at(win, 900, 800)
        threshold_w = app_mod.TWO_COLUMN_MIN_W + 16  # panel width + window margins
        single, _, _ = _layout_at(win, threshold_w + 5, 800)
        assert not single
        # ...and well under it switches.
        single, _, _ = _layout_at(win, threshold_w - 30, 800)
        assert single
    finally:
        close_window(win)


def test_expand_returns_to_previous_size():
    win = make_window()
    try:
        win.resize(1000, 800)
        drain(30)
        win.toggle_compact_view()
        drain()
        assert (win.width(), win.height()) == (app_mod.COMPACT_W, app_mod.COMPACT_H)
        win.toggle_compact_view()
        drain(30)
        assert (win.width(), win.height()) == (1000, 800), (win.width(), win.height())
    finally:
        close_window(win)


# --------------------------------------------- v1.40: multi-monitor

def test_drag_to_second_monitor():
    """The window used to stop dead at the edge of the monitor it started on."""
    with tempfile.TemporaryDirectory() as tmp:
        screens = {"screens": [
            {"name": "A", "x": 0, "y": 0, "width": 1920, "height": 1080},
            {"name": "B", "x": 1920, "y": 0, "width": 1920, "height": 1080},
        ]}
        Path(tmp, "screens.json").write_text(json.dumps(screens), encoding="utf-8")
        env = dict(os.environ)
        env.pop("QT_QPA_PLATFORM", None)
        out = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "multimonitor_probe.py"), str(APP_FILE)],
            cwd=tmp, env=env, capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr[-2000:]
        result = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(result["screens"]) == 2, result
    assert result["after_drag"][0] >= 1920, f"window did not cross to monitor 2: {result}"
    assert result["after_drag"][0] + 800 <= 3840, f"window must stay inside monitor 2: {result}"
    assert result["after_snap"][0] > 1920 + 1000, f"snap must use monitor 2: {result}"
    assert result["compact_after_drag"][0] >= 1920, f"compact overlay did not cross: {result}"


def test_window_position_restored_only_if_on_screen():
    settings = app_mod.SETTINGS_PATH
    try:
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({
            "ground_host": "192.168.199.16", "air_host": "192.168.199.18", "rc_host": "",
            "window": {"x": 40, "y": 30, "w": 900, "h": 760, "compact": False, "topmost": True},
        }), encoding="utf-8")
        win = app_mod.MainWindow()
        try:
            assert (win.x(), win.y(), win.width(), win.height()) == (40, 30, 900, 760)
            assert win.rc_host == "192.168.199.23", "pre-1.40 empty RC IP must fall back to the default"
        finally:
            close_window(win)
        # A monitor that is gone: must not strand the window off-screen.
        settings.write_text(json.dumps({"window": {"x": 9000, "y": 9000, "w": 900, "h": 760}}), encoding="utf-8")
        win = app_mod.MainWindow()
        try:
            screen = app_mod.QGuiApplication.primaryScreen().availableGeometry()
            assert screen.contains(win.frameGeometry().topLeft()), (win.x(), win.y())
        finally:
            close_window(win)
    finally:
        settings.unlink(missing_ok=True)


# --------------------------------------------- v1.40: flight log

def _write_log(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_recording_writes_clean_csv():
    win = make_window()
    try:
        win.set_recording(False)
        win.set_recording(True)
        assert "REC" in win.title_bar.rec_btn.text() and win.title_bar.rec_btn.isVisibleTo(win)
        assert win.logger.path is None, "file is created lazily on the first row"
        for sample in LIVE:
            win.on_data(sample)
            drain()
        path = win.logger.path
        assert path is not None and path.exists()
        win.set_recording(False)
        data = app_mod.load_log(path)
        assert data.columns == app_mod.LOG_FIELDS
        assert len(data.rows) == len(LIVE)
        last = data.rows[-1]
        s = LIVE[-1]
        assert last["air_rssi0_dbm"] == str(s["airRSSI0"])
        assert last["link"] == "TRACK" and last["source"] == "live"
        assert data.series("air_snr_db")[-1] == float(s["airSnr"])
        assert "sbus" not in ",".join(data.columns).lower()
    finally:
        close_window(win)


def test_demo_is_logged_to_separate_file():
    win = make_window()
    try:
        win.set_recording(True)
        win.toggle_demo()
        for _ in range(3):
            win._demo_tick()
            drain()
        demo_path = win.logger.path
        assert demo_path is not None and "demo" in demo_path.name
        win.toggle_demo()
        assert win.logger.path is None or "demo" not in win.logger.path.name
        win.set_recording(False)
    finally:
        close_window(win)


def test_linear_fit():
    a, b, r, n = app_mod.linear_fit([0, 1, 2, 3, None], [20, 18, 16, 14, 99])
    assert abs(a - 20) < 1e-9 and abs(b + 2) < 1e-9 and abs(r + 1) < 1e-9 and n == 4
    assert app_mod.linear_fit([1, 1, 1], [1, 2, 3]) is None
    assert app_mod.linear_fit([1, 2], [1, 2]) is None


def test_log_analysis_finds_events_and_snr_trend():
    rows = []
    t0 = datetime(2026, 10, 14, 12, 0, 0)
    for i in range(60):
        dist = i * 50.0
        link = "TRACK" if not (30 <= i < 35) else "AIR init / GND init"
        warn = "AIR latency 150 ms" if 10 <= i < 14 else ""
        sec = i * 1.2 + (20 if i >= 50 else 0)  # 20 s gap before row 50
        rows.append({
            "timestamp": (t0 + timedelta(seconds=sec)).isoformat(timespec="milliseconds"),
            "link": link,
            "distance_m": f"{dist:.0f}",
            "air_snr_db": f"{25 - dist / 200:.1f}",
            "ground_snr_db": f"{28 - dist / 250:.1f}",
            "air_quality_pct": "100" if dist < 2000 else "40",
            "air_ant": "auto-rf1",
            "warnings": warn,
        })
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "flight.csv"
        _write_log(path, rows)
        data = app_mod.load_log(path)
    assert "air_ant" not in data.numeric and "distance_m" in data.numeric
    events = app_mod.detect_events(data)
    kinds = [e.kind for e in events]
    assert kinds[0] == "start" and kinds[-1] == "end"
    assert kinds.count("loss") == 1 and kinds.count("restore") == 1
    assert kinds.count("gap") == 1
    assert sum(1 for e in events if "latency" in e.text) == 1, "a warning is reported once when it appears"
    summary = "\n".join(app_mod.summarize_log(data))
    assert "втрат лінку: 1" in summary
    assert "Макс. відстань 2950 м" in summary
    assert "AIR SNR ≈ 25.0 -5.0 dB/км" in summary, summary
    assert "AIR якість < 45% вперше на 2000 м" in summary, summary


def test_logs_tab_opens_old_v139_format():
    """Logs from v1.39 (asdict(TelemetryState) columns) still open."""
    row = {
        "timestamp": "2026-09-13T14:38:10.000", "air_status": "track", "ground_status": "track",
        "distance_text": "120 m", "distance_value": "120.0", "air_snr": "21", "ground_snr": "25",
        "link_summary": "TRACK", "air_best_rssi": "-40",
    }
    rows = [dict(row), dict(row, timestamp="2026-09-13T14:38:11.200", distance_value="180.0", air_snr="19"),
            dict(row, timestamp="2026-09-13T14:38:12.400", distance_value="240.0", air_snr="17", link_summary="AIR init / GND init")]
    win = make_window()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.csv"
            _write_log(path, rows)
            win.view_tabs.setCurrentWidget(win.logs_tab)
            drain(30)
            win.logs_tab.load_path(path)
            drain()
            tab = win.logs_tab
            assert tab.data.col("link") == "link_summary"
            assert tab.data.track == [True, True, False]
            assert tab.x_combo.currentData() == "distance_value"
            assert tab.y_combo.currentData() == "air_snr"
            assert tab.event_list.count() >= 3
            tab.mode_combo.setCurrentIndex(tab.MODE_TIME)
            drain()
            tab.event_list.setCurrentRow(1)
            drain()
            win.view_tabs.setCurrentIndex(0)
    finally:
        close_window(win)


# --------------------------------------------- v1.40: thresholds, sound, cleanup

def test_thresholds_drive_colours_and_warnings():
    try:
        assert app_mod.temp_color(55) == "#f1c40f"
        assert app_mod.quality_status(70) == "MID"
        app_mod.set_thresholds({"temp_warn": 60, "temp_crit": 70, "quality_good": 65, "junk": 1})
        assert app_mod.temp_color(55) == "#2ecc71"
        assert app_mod.quality_status(70) == "GOOD"
        assert "junk" not in app_mod.THRESHOLDS
        assert app_mod.warning_messages({"airAD9361Temp": 65}, None, None, None, None) == ["AIR temp warm 65C"]
        # Swapped pair is put back in order rather than breaking the colours.
        app_mod.set_thresholds({"latency_warn": 300, "latency_crit": 100})
        assert app_mod.THRESHOLDS["latency_warn"] == 100 and app_mod.THRESHOLDS["latency_crit"] == 300
        app_mod.set_thresholds({"rssi_min_dbm": -80, "rssi_max_dbm": -60})
        assert app_mod.best_rssi_to_percent(-70) == 50
    finally:
        app_mod.set_thresholds(None)
    assert app_mod.THRESHOLDS == app_mod.DEFAULT_THRESHOLDS


def test_settings_dialog_tabs_roundtrip():
    dlg = app_mod.SettingsDialog("1.1.1.1", "2.2.2.2", "3.3.3.3", True,
                                 thresholds={"temp_warn": 55}, alarm={"quality_below": 0},
                                 log_retention_days=0)
    v = dlg.values()
    assert v["thresholds"]["temp_warn"] == 55 and v["thresholds"]["latency_crit"] == 250
    assert v["alarm"]["quality_below"] == 0 and v["alarm"]["link_loss"] is True
    assert v["log_retention_days"] == 0, "cleanup must be off by default"
    assert not dlg.retention_days.isEnabled()
    dlg.retention_check.setChecked(True)
    dlg.retention_days.setValue(14)
    dlg.threshold_spins["temp_warn"].setValue(40)
    dlg.reset_thresholds()
    v = dlg.values()
    assert v["log_retention_days"] == 14 and v["thresholds"]["temp_warn"] == 50


def test_alarm_monitor_logic():
    cfg = dict(app_mod.DEFAULT_ALARM, repeat_s=10, quality_below=30)
    m = app_mod.AlarmMonitor()
    assert m.evaluate(0, False, (), cfg) == [], "no alarm before the first TRACK"
    assert m.evaluate(1, None, (), cfg) == []
    assert m.evaluate(2, True, (90, 90), cfg) == []
    assert m.evaluate(3, False, (), cfg) == ["alarm"], "TRACK lost"
    assert m.evaluate(5, False, (), cfg) == [], "not every poll"
    assert m.evaluate(13.5, False, (), cfg) == ["alarm"], "repeats after repeat_s"
    assert m.evaluate(14, True, (90, 90), cfg) == ["restore"]
    assert m.evaluate(15, True, (20, 90), cfg) == ["warn"], "quality below threshold"
    assert m.evaluate(16, True, (20, 90), cfg) == []
    assert m.evaluate(17, True, (60, 90), cfg) == []
    assert m.evaluate(18, False, (), dict(cfg, enabled=False)) == [], "master switch"
    once = app_mod.AlarmMonitor()
    c1 = dict(cfg, repeat_s=0)
    once.evaluate(0, True, (), c1)
    assert once.evaluate(1, False, (), c1) == ["alarm"]
    assert once.evaluate(100, False, (), c1) == [], "repeat 0 = once"


def test_window_sounds_on_link_loss_and_mute():
    win = make_window()
    try:
        win.alarm_cfg = dict(app_mod.DEFAULT_ALARM)
        win.sound.played.clear()
        good = copy.deepcopy(LIVE[-1])
        win.on_data(good)
        lost = dict(good, airLinkStatus="init", groundLinkStatus="init")
        win.on_data(lost)
        assert win.sound.played == ["alarm"]
        win.on_data(good)
        assert win.sound.played[-1] == "restore"
        win.toggle_mute()
        assert "[MUTE]" in win.title_bar.title.text()
        n = len(win.sound.played)
        win.on_data(lost)
        assert len(win.sound.played) == n, "muted"
        win.toggle_mute()
        # Ground unit unreachable counts as link loss (after the grace polls).
        win.on_data(good)
        n = len(win.sound.played)
        for _ in range(app_mod.ERROR_GRACE_POLLS):
            win.on_error("timeout")
        assert win.sound.played[n:] == ["alarm"]
    finally:
        close_window(win)


def test_sound_files_are_valid_wav():
    import io
    import wave
    for name, segments in app_mod.SOUNDS.items():
        with wave.open(io.BytesIO(app_mod._tone_wav(segments))) as w:
            assert w.getnframes() > 1000, name


def test_log_cleanup_off_by_default_and_spares_current_file():
    logs = app_mod.LOGS_DIR
    logs.mkdir(parents=True, exist_ok=True)
    old = logs / "telemetry_20200101_000000.csv"
    current = logs / "telemetry_20200101_000001.csv"
    other = logs / "notes.csv"
    for p in (old, current, other):
        p.write_text("x", encoding="utf-8")
        os.utime(p, (time.time() - 90 * 86400,) * 2)
    try:
        assert app_mod.cleanup_old_logs(0) == []
        assert old.exists()
        win = make_window()
        try:
            assert win.log_retention_days == 0
            assert old.exists(), "default settings must not delete anything"
        finally:
            close_window(win)
        removed = app_mod.cleanup_old_logs(30, keep=current)
        assert removed == [old]
        assert current.exists() and other.exists()
    finally:
        for p in (old, current, other):
            p.unlink(missing_ok=True)


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
