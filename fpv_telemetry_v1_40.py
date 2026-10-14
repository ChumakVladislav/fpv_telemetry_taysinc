
import csv
import os
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import math
import platform
import re
import subprocess
import time
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import requests
except ModuleNotFoundError:
    requests = None

try:
    import pyqtgraph as pg
except ModuleNotFoundError:
    pg = None

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, QThread, QTimer, Qt, Signal, Slot, QObject, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QKeySequence, QShortcut, QIcon, QCursor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSpacerItem,
    QSizeGrip,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QTabWidget,
    QPlainTextEdit,
    QSplitter,
)

APP_VERSION = "1.40"
DEFAULT_GROUND_HOST = "192.168.199.16"
DEFAULT_AIR_HOST = "192.168.199.18"
DEFAULT_RC_HOST = "192.168.199.23"
POLL_MS = 1200
# 0.35s was tighter than a real Wi-Fi round trip to the ground unit, so the app
# flapped to "NO API" constantly. 1.0s still fits comfortably inside POLL_MS.
REQUEST_TIMEOUT = 1.0
PING_INTERVAL_MS = 1500
PING_TIMEOUT_MS = 700
# Number of consecutive poll failures tolerated before the cards are blanked.
ERROR_GRACE_POLLS = 3
APP_NAME = "TaisyncTelemetry"
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # Directory the .exe lives in (not the temp unpack dir).
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR


def resource_path(name: str) -> Path:
    """Locate a bundled read-only asset (works frozen and from source)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / name
        if candidate.exists():
            return candidate
    return APP_DIR / name


def user_data_dir() -> Path:
    """Per-user writable location for settings and logs.

    Once installed under Program Files the application directory is read-only
    and the working directory is whatever launched us, so settings and CSV logs
    have to live somewhere the user can actually write.

    TAISYNC_DATA_DIR overrides it; the test suite points it at a temp dir so
    it never touches the real settings or logs.
    """
    override = os.environ.get("TAISYNC_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


DATA_DIR = user_data_dir()
ICON_PATH = resource_path("fpv_telemetry_icon.ico")
# Keep honouring a settings file sitting next to a source checkout so existing
# installs do not lose their IPs; otherwise use the per-user directory.
_LEGACY_SETTINGS = Path.cwd() / "telemetry_settings.json"
SETTINGS_PATH = (
    _LEGACY_SETTINGS
    if _LEGACY_SETTINGS.exists() and not os.environ.get("TAISYNC_DATA_DIR")
    else DATA_DIR / "telemetry_settings.json"
)
LOGS_DIR = DATA_DIR / "logs"
PING_DEBUG_PATH = DATA_DIR / "ping_debug.log"
PING_DEBUG_MAX_BYTES = 256 * 1024
RESIZE_MARGIN = 6
MIN_W = 300
MIN_H = 680
MAX_SAMPLES = 180
# Compact overlay size. Wide enough for both antennas + TX power per side.
COMPACT_W = 500
COMPACT_H = 58
# Signal bar scale. Measured on the real link: -20 dBm (air, close range) and
# -54..-68 dBm (ground). -95 is around the receiver floor, -45 is "plenty".
# Tune here if the bars feel too optimistic or pessimistic in the field.
RSSI_MIN_DBM = -95.0
RSSI_MAX_DBM = -45.0
# FEC error rate (over the counter window) at which a warning is raised.
LDPC_WARN_RATE_PCT = 1.0
# Set TAISYNC_OFFLINE=1 to build the UI without polling or pinging anything.
# Used by the test suite so live hardware on the network cannot leak into it.
NETWORK_ENABLED = os.environ.get("TAISYNC_OFFLINE", "") != "1"
# Resize handling is coalesced through a single-shot timer so a drag does not
# re-run the (expensive) relayout + restyle pass on every pixel.
RESIZE_DEBOUNCE_MS = 60
# Width/height must move this far past a breakpoint before the layout flips
# back, otherwise the UI oscillates while the user drags across the threshold.
LAYOUT_HYSTERESIS = 24
# Responsive breakpoints, all measured on the telemetry panel width (the window
# minus its margins), so every decision uses the same number.
#   < TWO_COLUMN_MIN_W   "medium": one column, many rows, values inline
#   >= TWO_COLUMN_MIN_W  "large":  two columns, air left / ground right
# v1.39 switched to two columns only past 860 px of panel *and* 760 px of
# height, so a short window stayed single-column however wide it got -- the
# opposite of what a short window needs. Width alone decides now.
TWO_COLUMN_MIN_W = 600
STATUS_SIDE_BY_SIDE_MIN_W = 520
# Single column: full-size cards (big value line) only when 10 rows fit.
SINGLE_COLUMN_FULL_CARDS_MIN_H = 1000
# Two columns: fold values into titles only on a really short window.
TWO_COLUMN_INLINE_MAX_H = 640
DISTANCE_LARGE_MIN_W = 1000
# Below this window width the title bar buttons switch to one-glyph labels.
TITLE_FULL_LABELS_MIN_W = 600


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def below_threshold(current: Optional[bool], value: float, threshold: float, band: float = LAYOUT_HYSTERESIS) -> bool:
    """Threshold test with hysteresis so layouts don't flip-flop mid-drag."""
    if current is None:
        return value < threshold
    if current:
        return value < threshold + band
    return value < threshold


def clamp_to_rect(target: QPoint, width: int, height: int, area: QRect) -> QPoint:
    """Top-left that keeps a width x height window inside `area`."""
    x = max(area.left(), min(target.x(), area.right() - width + 1))
    y = max(area.top(), min(target.y(), area.bottom() - height + 1))
    return QPoint(x, y)


def linear_map(value: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
    if in_max == in_min:
        return out_min
    ratio = (value - in_min) / (in_max - in_min)
    return out_min + ratio * (out_max - out_min)


def parse_number(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except Exception:
        return None


def parse_kbs(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().lower().replace("kbs", "").replace("kb/s", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def to_dbm(v: Any) -> Optional[float]:
    n = parse_number(v)
    if n is None:
        return None
    if n > 0:
        n = -abs(n)
    return n


def safe_text(v: Any, default: str = "N/A") -> str:
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def best_rssi_to_percent(best_rssi: Optional[float]) -> Optional[int]:
    """RSSI in dBm -> 0..100 %.

    The old window was -94..-74 dBm, so anything stronger than -74 read 100 %.
    On the real link (measured -20 dBm air, -54 dBm ground) both bars sat at
    100 % and a 34 dB difference was invisible. See RSSI_MIN_DBM/RSSI_MAX_DBM.
    """
    if best_rssi is None:
        return None
    percent = linear_map(best_rssi, THRESHOLDS["rssi_min_dbm"], THRESHOLDS["rssi_max_dbm"], 0, 100)
    return int(round(clamp(percent, 0, 100)))


def snr_to_percent(snr: Optional[float]) -> Optional[int]:
    if snr is None:
        return None
    percent = linear_map(snr, 6, 20, 0, 100)
    return int(round(clamp(percent, 0, 100)))


# Frame error rate (%) -> quality (%). Piecewise so that the scale spends its
# resolution where FEC links actually live: a clean link sits well under 0.1 %,
# and anything past a few percent is in real trouble.
_LDPC_RATE_POINTS = ((0.0, 100), (0.1, 90), (1.0, 70), (5.0, 30), (10.0, 0))


def ldpc_rate_to_percent(rate_pct: Optional[float]) -> Optional[int]:
    if rate_pct is None:
        return None
    if rate_pct <= 0:
        return 100
    for (x0, y0), (x1, y1) in zip(_LDPC_RATE_POINTS, _LDPC_RATE_POINTS[1:]):
        if rate_pct <= x1:
            return int(round(linear_map(rate_pct, x0, x1, y0, y1)))
    return 0


def ldpc_error_rate(fail_delta: Optional[float], pass_delta: Optional[float]) -> Optional[float]:
    """Share of LDPC frames that failed, in percent, over a counter window.

    Taisync reports airLdpcFail / airLdpcPass as totals since power-on. Earlier
    versions fed the raw fail *total* into the quality score, so the score only
    ever went down: past 100 lifetime failures (a 0.03 % error rate on the
    measured link) AIR QUALITY pinned at 20 % CRIT and never recovered.
    """
    if fail_delta is None or pass_delta is None:
        return None
    frames = fail_delta + pass_delta
    if frames <= 0:
        return None
    return 100.0 * fail_delta / frames


def combine_quality(snr: Optional[float], ldpc_rate_pct: Optional[float]) -> Optional[int]:
    """Link quality: the worse of SNR and recent FEC error rate."""
    vals = [v for v in (snr_to_percent(snr), ldpc_rate_to_percent(ldpc_rate_pct)) if v is not None]
    if not vals:
        return None
    return min(vals)


_MCS_RE = re.compile(r"^\s*([A-Za-z0-9]+?)_(\d+)_(\d+)\s*\(\s*([\d.]+)\s*Mbps\s*\)", re.IGNORECASE)


def parse_mcs(value: Any) -> tuple:
    """'16QAM_1_2(7.86Mbps)' -> ('16QAM 1/2', 7.86). Unknown formats pass through."""
    text = safe_text(value, "")
    m = _MCS_RE.match(text)
    if not m:
        return (text or "N/A", None)
    mod, num, den, mbps = m.groups()
    return (f"{mod.upper()} {num}/{den}", float(mbps))


def active_antenna(value: Any) -> tuple:
    """'auto-rf2' -> ('auto', 1). Index is 0 for RF1, 1 for RF2, None if unknown."""
    text = safe_text(value, "").lower()
    mode = "auto" if "auto" in text else "manual" if "man" in text else ""
    idx = 0 if "rf1" in text else 1 if "rf2" in text else None
    return (mode, idx)


def nice_ceiling(value: float) -> float:
    """Round up to 1/2/5 x 10^n for tidy chart axes."""
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for step in (1, 2, 5, 10):
        if value <= step * base:
            return float(step * base)
    return float(10 * base)


def format_error_rate(rate_pct: Optional[float]) -> str:
    if rate_pct is None:
        return "--"
    if rate_pct == 0:
        return "0%"
    if rate_pct < 0.01:
        return "<0.01%"
    if rate_pct < 1:
        return f"{rate_pct:.2f}%"
    return f"{rate_pct:.1f}%"


class CounterWindow:
    """Rolling deltas of the device's since-power-on counters.

    LDPC fails/passes, TX overflows and RX discards are all reported as
    running totals. What matters is how much they moved recently:
    a historical overflow from an hour ago is not a warning now. Keeps the last
    `window` per-poll deltas and treats a decrease as a device reboot.
    """

    def __init__(self, window: int = 5):
        self.window = window
        self.prev: Dict[str, float] = {}
        self.hist: Dict[str, deque] = {}

    def reset(self):
        self.prev.clear()
        self.hist.clear()

    def update(self, data: Dict[str, Any], keys) -> None:
        for key in keys:
            value = parse_number(data.get(key))
            if value is None:
                continue
            prev = self.prev.get(key)
            self.prev[key] = value
            if prev is None:
                continue
            delta = value - prev
            if delta < 0:
                # Counter went backwards: the unit rebooted. Start over.
                self.hist[key] = deque(maxlen=self.window)
                continue
            self.hist.setdefault(key, deque(maxlen=self.window)).append(delta)

    def recent(self, key: str) -> Optional[float]:
        """Sum of the deltas currently in the window, None until one exists."""
        hist = self.hist.get(key)
        if not hist:
            return None
        return float(sum(hist))

    def span_polls(self, key: str) -> int:
        hist = self.hist.get(key)
        return len(hist) if hist else 0


COUNTER_KEYS = (
    "airLdpcFail", "airLdpcPass", "groundLdpcFail", "groundLdpcPass",
    "airTxOverflowCnt", "groundTxOverflowCnt", "airRxDiscardCnt", "groundRxDiscardCnt",
)


def read_percent(*values: Any) -> Optional[int]:
    for value in values:
        n = parse_number(value)
        if n is None:
            continue
        return int(round(clamp(n, 0, 100)))
    return None


# Colour / warning thresholds. Editable in SET -> Пороги and saved with the
# settings; THRESHOLDS is the live copy every colour function reads.
#   green when value is on the good side of *_good, yellow up to *_warn/_crit,
#   red beyond. For temperature and latency, higher is worse.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "signal_good": 70,      # signal %, green above
    "signal_warn": 50,      # signal %, yellow from here, red below
    "quality_good": 75,     # quality %, GOOD from here
    "quality_warn": 45,     # quality %, MID from here, CRIT below
    "temp_warn": 50,        # °C, yellow / "warm" warning from here
    "temp_crit": 60,        # °C, red / "high" warning above
    "latency_warn": 100,    # ms, yellow from here
    "latency_crit": 250,    # ms, red above
    "rssi_min_dbm": RSSI_MIN_DBM,   # signal bar 0 %
    "rssi_max_dbm": RSSI_MAX_DBM,   # signal bar 100 %
}
THRESHOLDS: Dict[str, float] = dict(DEFAULT_THRESHOLDS)


def set_thresholds(values: Optional[Dict[str, Any]]) -> None:
    """Load thresholds from settings, ignoring unknown keys and junk values,
    and keeping each pair in a sane order."""
    THRESHOLDS.clear()
    THRESHOLDS.update(DEFAULT_THRESHOLDS)
    for key, value in (values or {}).items():
        n = parse_number(value)
        if key in THRESHOLDS and n is not None:
            THRESHOLDS[key] = n
    for low, high in (("signal_warn", "signal_good"), ("quality_warn", "quality_good"),
                      ("temp_warn", "temp_crit"), ("latency_warn", "latency_crit")):
        if THRESHOLDS[low] > THRESHOLDS[high]:
            THRESHOLDS[low], THRESHOLDS[high] = THRESHOLDS[high], THRESHOLDS[low]
    if THRESHOLDS["rssi_max_dbm"] - THRESHOLDS["rssi_min_dbm"] < 5:
        THRESHOLDS["rssi_min_dbm"] = DEFAULT_THRESHOLDS["rssi_min_dbm"]
        THRESHOLDS["rssi_max_dbm"] = DEFAULT_THRESHOLDS["rssi_max_dbm"]


def _level_high_good(value, good_key: str, warn_key: str) -> Optional[int]:
    """0 good, 1 warn, 2 bad, None unknown -- for metrics where more is better."""
    if value is None:
        return None
    if value >= THRESHOLDS[good_key]:
        return 0
    if value >= THRESHOLDS[warn_key]:
        return 1
    return 2


def _level_low_good(value, warn_key: str, crit_key: str) -> Optional[int]:
    """Same, for metrics where less is better (temperature, latency)."""
    if value is None:
        return None
    if value < THRESHOLDS[warn_key]:
        return 0
    if value <= THRESHOLDS[crit_key]:
        return 1
    return 2


_LEVEL_COLORS = ("#2ecc71", "#f1c40f", "#ff5c5c")


def _level_color(level: Optional[int]) -> str:
    return "#7a7f87" if level is None else _LEVEL_COLORS[level]


def metric_color(percent: Optional[int]) -> str:
    return _level_color(_level_high_good(percent, "quality_good", "quality_warn"))


def quality_status(percent: Optional[int]) -> str:
    level = _level_high_good(percent, "quality_good", "quality_warn")
    return "N/A" if level is None else ("GOOD", "MID", "CRIT")[level]


def quality_accent(percent: Optional[int]) -> str:
    level = _level_high_good(percent, "quality_good", "quality_warn")
    return "#182129" if level is None else ("#163126", "#3b3416", "#3a1b1b")[level]


def signal_color(percent: Optional[int]) -> str:
    return _level_color(_level_high_good(percent, "signal_good", "signal_warn"))


def temp_color(temp_c: Optional[float]) -> str:
    return _level_color(_level_low_good(temp_c, "temp_warn", "temp_crit"))


def latency_color(ms: Optional[int]) -> str:
    return _level_color(_level_low_good(ms, "latency_warn", "latency_crit"))


def status_color(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "track":
        return "#2ecc71"
    if s == "init":
        return "#f39c12"
    return "#ff5c5c"


def ping_debug(line: str) -> None:
    """Append to the ping debug log, but stop once it hits the size cap.

    Previously every failed ping appended here forever, so an unreachable host
    turned into continuous disk writes inside the polling loop.
    """
    try:
        if PING_DEBUG_PATH.exists() and PING_DEBUG_PATH.stat().st_size > PING_DEBUG_MAX_BYTES:
            return
        PING_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PING_DEBUG_PATH.open('a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


def ping_latency_ms(host: str, timeout_ms: int = 900) -> Optional[int]:
    if not host:
        return None
    host = str(host).strip()
    if not host:
        return None

    system = platform.system().lower()
    creationflags = 0
    startupinfo = None

    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
        patterns = [
            r"time[=<]\s*(\d+(?:[\.,]\d+)?)\s*ms",
            r"час[=<]\s*(\d+(?:[\.,]\d+)?)\s*мс",
            r"tempo[=<]\s*(\d+(?:[\.,]\d+)?)\s*ms",
            r"temps[=<]\s*(\d+(?:[\.,]\d+)?)\s*ms",
            r"tiempo[=<]\s*(\d+(?:[\.,]\d+)?)\s*ms",
        ]
        success_words = ["Reply from", "Відповідь від", "Ответ от", "Respuesta desde", "Réponse de"]
        failure_words = [
            "Destination host unreachable",
            "Request timed out",
            "General failure",
            "could not find host",
            "Заданный узел недоступен",
            "Превышен интервал ожидания",
            "не вдалося знайти вузол",
        ]

        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW

        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    else:
        timeout_s = max(1, int(round(timeout_ms / 1000.0)))
        cmd = ["ping", "-c", "1", "-W", str(timeout_s), host]
        patterns = [r"time=(\d+(?:[\.,]\d+)?)\s*ms"]
        success_words = ["bytes from"]
        failure_words = ["100% packet loss", "Name or service not known", "Temporary failure in name resolution"]

    try:
        result = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=max(2.0, timeout_ms / 1000.0 + 1.0),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        output = result.stdout + "\n" + result.stderr
        lowered = output.lower()
        if any(word.lower() in lowered for word in failure_words):
            return None
        for pattern in patterns:
            m = re.search(pattern, output, re.IGNORECASE)
            if m and any(word.lower() in lowered for word in success_words):
                value = m.group(1).replace(',', '.')
                return int(round(float(value)))
        if result.returncode == 0 and any(word.lower() in lowered for word in success_words) and re.search(r"\bttl\b", lowered):
            return 1
        ping_debug(f"HOST={host} RC={result.returncode}\n{output}\n---\n")
    except Exception as e:
        ping_debug(f"HOST={host} EXC={e}\n---\n")
        return None
    return None

def safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return default


def warning_messages(
    data: dict,
    air_latency: Optional[int],
    ground_latency: Optional[int],
    fpv_online: Optional[bool],
    rc_online: Optional[bool],
    counters: Optional["CounterWindow"] = None,
) -> list[str]:
    """Active warnings, most severe first.

    Counter-based warnings (FEC errors, overflows, discards) look at how much
    the counters moved over the recent window, not at their since-power-on
    totals -- otherwise one overflow an hour ago keeps "GND overflow 8" lit for
    the rest of the session, which is exactly what the live link showed.
    """
    msgs = []

    # 1. Link / control. `fpv_online` is derived from the same UAV field, so
    #    only report it once.
    uav = data.get("UAV")
    if uav is not None and str(uav).strip().lower() not in ("online", "ok"):
        msgs.append(f"UAV {safe_text(uav, 'unknown')}")
    elif uav is None and fpv_online is False:
        msgs.append("FPV unreachable")
    bind = data.get("bindStatus")
    if bind is not None and str(bind).strip().lower() not in ("bind", "bound", "ok"):
        msgs.append(f"BIND {safe_text(bind, 'unknown')}")
    if rc_online is False:
        msgs.append("RC unreachable")

    # 2. Data integrity, from counter deltas.
    if counters is not None:
        for side, label in (("air", "AIR"), ("ground", "GND")):
            rate = ldpc_error_rate(counters.recent(f"{side}LdpcFail"), counters.recent(f"{side}LdpcPass"))
            if rate is not None and rate >= LDPC_WARN_RATE_PCT:
                msgs.append(f"{label} FEC errors {rate:.1f}%")
        for key, label in (
            ("airTxOverflowCnt", "AIR TX overflow"),
            ("groundTxOverflowCnt", "GND TX overflow"),
            ("airRxDiscardCnt", "AIR RX discard"),
            ("groundRxDiscardCnt", "GND RX discard"),
        ):
            moved = counters.recent(key)
            if moved:
                msgs.append(f"{label} +{int(moved)}")

    # 3. Thermal.
    for key, label in (("airAD9361Temp", "AIR"), ("groundAD9361Temp", "GND")):
        temp = parse_number(data.get(key))
        if temp is None:
            continue
        if temp > THRESHOLDS["temp_crit"]:
            msgs.append(f"{label} temp high {temp:.0f}C")
        elif temp >= THRESHOLDS["temp_warn"]:
            msgs.append(f"{label} temp warm {temp:.0f}C")

    # 4. Latency.
    for latency, label in ((air_latency, "AIR"), (ground_latency, "GND")):
        if latency is None:
            continue
        if latency > THRESHOLDS["latency_crit"]:
            msgs.append(f"{label} latency high {latency} ms")
        elif latency >= THRESHOLDS["latency_warn"]:
            msgs.append(f"{label} latency {latency} ms")

    return msgs[:6]


class DemoSource:
    """Synthetic telemetry for the TEST button.

    Shaped after a payload captured from the real ground unit (1410 MHz link,
    55 fields): the same keys, the same value formats, and -- importantly --
    the same *cumulative* counters. LDPC fails/passes, overflows and discards
    only ever grow, exactly like the hardware, so the simulator exercises the
    delta logic rather than hiding bugs in it.

    Flies a repeating 3-minute profile out to ~2.8 km and back: RSSI and SNR
    fall off with range, the FEC error rate climbs, temperatures build, the
    MCS steps down, and the link drops to "init" at the turnaround.
    """

    PERIOD_S = 180.0
    _MCS_STEPS = (
        (0.30, "64QAM_2_3(13.1Mbps)"),
        (0.60, "16QAM_1_2(7.86Mbps)"),
        (0.85, "QPSK_1_2(3.93Mbps)"),
        (1.01, "BPSK_1_2(1.97Mbps)"),
    )

    def __init__(self):
        self.reset()

    def reset(self):
        self.start = time.monotonic()
        self._last = self.start
        self.totals = {
            "airLdpcFail": 0.0, "airLdpcPass": 0.0,
            "groundLdpcFail": 0.0, "groundLdpcPass": 0.0,
            "airTxOverflowCnt": 0.0, "groundTxOverflowCnt": 0.0,
            "airRxDiscardCnt": 0.0, "groundRxDiscardCnt": 0.0,
        }

    def _phase(self, now: float) -> float:
        """0.0 at launch, 1.0 at the far point, back to 0.0 on return."""
        elapsed = (now - self.start) % self.PERIOD_S
        half = self.PERIOD_S / 2.0
        return elapsed / half if elapsed <= half else (self.PERIOD_S - elapsed) / half

    def payload(self) -> Dict[str, Any]:
        now = time.monotonic()
        dt = max(0.0, min(5.0, now - self._last))
        self._last = now
        p = self._phase(now)
        t = now - self.start
        jitter = 0.5 * math.sin(t * 2.3) + 0.3 * math.sin(t * 5.7)
        link_up = p < 0.93

        # Frame counters advance at roughly the rates seen on the real unit
        # (~370 air / ~5000 ground LDPC frames per second), with an error rate
        # that is negligible close in and several percent at the far point.
        err = clamp(0.00002 + 0.07 * (p ** 4) + 0.004 * max(0.0, jitter), 0.0, 0.5)
        for side, fps in (("air", 370.0), ("ground", 5000.0)):
            frames = fps * dt if link_up else 0.0
            self.totals[f"{side}LdpcFail"] += frames * err
            self.totals[f"{side}LdpcPass"] += frames * (1.0 - err)
        if p > 0.8:
            self.totals["airTxOverflowCnt"] += dt * 2.0
        if p > 0.88:
            self.totals["groundRxDiscardCnt"] += dt * 3.0

        air_rssi0 = -24.0 - 64.0 * p + 1.5 * jitter
        air_rssi1 = air_rssi0 + (5.0 if jitter > 0 else -6.0)
        gnd_rssi0 = -50.0 - 40.0 * p + 1.5 * jitter
        gnd_rssi1 = gnd_rssi0 - 9.0 + 2.0 * jitter
        mcs = next(text for limit, text in self._MCS_STEPS if p < limit)
        downlink = 600.0 - 380.0 * p + 60.0 * jitter
        uplink = 1.0 + 2.0 * max(0.0, jitter)

        payload = {
            "CurRxFreq": 1410, "CurTxFreq": 1410,
            "groundCurrentRxFreq": 1410, "groundCurrentTxFreq": 1410,
            "MCS": mcs,
            "UAV": "online" if link_up else "offline",
            "bindStatus": "bind",
            "airLinkStatus": "track" if link_up else "init",
            "groundLinkStatus": "track" if link_up else "init",
            "airRSSI0": int(round(air_rssi0)), "airRSSI1": int(round(air_rssi1)),
            "groundRSSI0": int(round(gnd_rssi0)), "groundRSSI1": int(round(gnd_rssi1)),
            "airSnr": int(round(clamp(25.0 - 17.0 * p + jitter, 0, 40))),
            "groundSnr": int(round(clamp(29.0 - 18.0 * p + jitter, 0, 40))),
            "airLinkQuality": f"{int(clamp(100 - 70 * p + 3 * jitter, 0, 100))}%",
            "groundLinkQuality": f"{int(clamp(100 - 62 * p + 3 * jitter, 0, 100))}%",
            "airAD9361Temp": int(round(36 + 26.0 * p)),
            "groundAD9361Temp": int(round(43 + 9.0 * p)),
            "airFPGATemp": 0, "groundFPGATemp": 0,
            "airCurrentAnt": "auto-rf2" if air_rssi1 >= air_rssi0 else "auto-rf1",
            "groundCurrentAnt": "auto-rf1" if gnd_rssi0 >= gnd_rssi1 else "auto-rf2",
            "airTxPower": 1, "groundTxPower": 27,
            "distance": int(round(2700.0 * p)),
            "downlinkDataRate": f"{int(max(0, downlink))}kbs",
            "uplinkDataRate": f"{int(max(0, uplink))}kbs",
            "_demo": True,
        }
        payload.update({k: int(v) for k, v in self.totals.items()})
        return payload

    def latency(self):
        """Fake air/ground/rc ping values that track the flight profile."""
        now = time.monotonic()
        p = self._phase(now)
        jitter = 0.5 * math.sin((now - self.start) * 3.1)
        air = int(clamp(8 + 300 * (p ** 2) + 6 * jitter, 1, 999))
        ground = int(clamp(3 + 12 * p + 2 * jitter, 1, 999))
        return air, ground, 4


@dataclass
class TelemetryState:
    timestamp: str = ""
    air_status: str = "N/A"
    ground_status: str = "N/A"
    distance_text: str = "N/A"
    distance_value: Optional[float] = None
    air_signal_pct: Optional[int] = None
    ground_signal_pct: Optional[int] = None
    air_quality_pct: Optional[int] = None
    ground_quality_pct: Optional[int] = None
    air_pure_quality_pct: Optional[int] = None
    ground_pure_quality_pct: Optional[int] = None
    air_best_rssi: Optional[float] = None
    ground_best_rssi: Optional[float] = None
    air_snr: Optional[float] = None
    ground_snr: Optional[float] = None
    air_ldpc_fail: Optional[float] = None
    ground_ldpc_fail: Optional[float] = None
    air_latency_ms: Optional[int] = None
    ground_latency_ms: Optional[int] = None
    air_ant: str = "N/A"
    ground_ant: str = "N/A"
    mcs: str = "N/A"
    bitrate: str = "N/A"
    freq: str = "N/A"
    air_tx: str = "N/A"
    ground_tx: str = "N/A"
    uplink_kbs: Optional[float] = None
    downlink_kbs: Optional[float] = None
    link_summary: str = "NO DATA"
    last_error: str = ""
    # Per-antenna RSSI, so both RF chains are visible rather than just the best.
    air_rssi0: Optional[float] = None
    air_rssi1: Optional[float] = None
    ground_rssi0: Optional[float] = None
    ground_rssi1: Optional[float] = None
    air_ant_mode: str = ""
    ground_ant_mode: str = ""
    air_ant_index: Optional[int] = None
    ground_ant_index: Optional[int] = None
    # Module output power in dBm, as configured on the unit.
    air_tx_dbm: Optional[float] = None
    ground_tx_dbm: Optional[float] = None
    # FEC frame error rate (%) over the recent counter window.
    air_ldpc_rate: Optional[float] = None
    ground_ldpc_rate: Optional[float] = None
    mcs_mode: str = "N/A"
    mcs_mbps: Optional[float] = None
    freq_mhz: Optional[float] = None
    air_temp_c: Optional[float] = None
    ground_temp_c: Optional[float] = None
    uav: str = "N/A"


DEFAULT_ALARM: Dict[str, Any] = {
    "enabled": True,        # master switch (M mutes for the session)
    "link_loss": True,      # TRACK lost / ground unit unreachable
    "quality_below": 30,    # air or ground quality T below this %, 0 = off
    "repeat_s": 10,         # repeat while the condition lasts, 0 = once
    "restore": True,        # short rising tone when TRACK comes back
}

# (key, label, min, max, step, suffix) for the Пороги tab.
THRESHOLD_FIELDS = (
    ("signal_good", "Сигнал: зелений від", 0, 100, 1, " %"),
    ("signal_warn", "Сигнал: жовтий від (нижче — червоний)", 0, 100, 1, " %"),
    ("quality_good", "Якість: GOOD від", 0, 100, 1, " %"),
    ("quality_warn", "Якість: MID від (нижче — CRIT)", 0, 100, 1, " %"),
    ("temp_warn", "Температура: жовта від", 0, 120, 1, " °C"),
    ("temp_crit", "Температура: червона вище", 0, 120, 1, " °C"),
    ("latency_warn", "Пінг: жовтий від", 1, 5000, 10, " ms"),
    ("latency_crit", "Пінг: червоний вище", 1, 5000, 10, " ms"),
    ("rssi_min_dbm", "Шкала сигналу: 0 % при", -130, 0, 1, " dBm"),
    ("rssi_max_dbm", "Шкала сигналу: 100 % при", -130, 0, 1, " dBm"),
)


class SettingsDialog(QDialog):
    test_sound = Signal()

    def __init__(self, ground_host: str, air_host: str, rc_host: str, auto_log: bool = True, parent=None,
                 thresholds: Optional[Dict[str, float]] = None, alarm: Optional[Dict[str, Any]] = None,
                 log_retention_days: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(500, 460)
        thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        alarm = {**DEFAULT_ALARM, **(alarm or {})}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        # ---------------------------------------------------------- connection
        conn = QWidget()
        conn_lay = QVBoxLayout(conn)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        self.ground_edit = QLineEdit(ground_host)
        self.air_edit = QLineEdit(air_host)
        self.rc_edit = QLineEdit(rc_host)

        # An empty field falls back to the default, and the placeholder says
        # which one, so clearing a field can't silently leave a host unset.
        for edit, default in (
            (self.ground_edit, DEFAULT_GROUND_HOST),
            (self.air_edit, DEFAULT_AIR_HOST),
            (self.rc_edit, DEFAULT_RC_HOST),
        ):
            edit.setPlaceholderText(default)
            edit.setClearButtonEnabled(True)
            edit.setObjectName("streamEdit")

        grid.addWidget(QLabel("Ground IP"), 0, 0)
        grid.addWidget(self.ground_edit, 0, 1)
        grid.addWidget(QLabel("Air IP"), 1, 0)
        grid.addWidget(self.air_edit, 1, 1)
        grid.addWidget(QLabel("RC IP (пульт)"), 2, 0)
        grid.addWidget(self.rc_edit, 2, 1)
        conn_lay.addLayout(grid)

        defaults_btn = QPushButton("Стандартні IP")
        defaults_btn.setToolTip(f"Ground {DEFAULT_GROUND_HOST} · Air {DEFAULT_AIR_HOST} · RC {DEFAULT_RC_HOST}")
        defaults_btn.clicked.connect(self.reset_defaults)
        conn_lay.addWidget(defaults_btn, 0, Qt.AlignLeft)
        info = QLabel("API URL формується автоматично як http://<ground_ip>/v2/baseband.json")
        info.setWordWrap(True)
        info.setStyleSheet("color: #8ea1b2; font-size: 12px;")
        conn_lay.addWidget(info)
        conn_lay.addStretch(1)
        tabs.addTab(conn, "Підключення")

        # ---------------------------------------------------------- thresholds
        thr = QWidget()
        thr_lay = QVBoxLayout(thr)
        thr_grid = QGridLayout()
        thr_grid.setVerticalSpacing(6)
        self.threshold_spins: Dict[str, QSpinBox] = {}
        for row, (key, label, lo, hi, step, suffix) in enumerate(THRESHOLD_FIELDS):
            spin = QSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
            spin.setSuffix(suffix)
            spin.setValue(int(round(thresholds[key])))
            self.threshold_spins[key] = spin
            thr_grid.addWidget(QLabel(label), row, 0)
            thr_grid.addWidget(spin, row, 1)
        thr_lay.addLayout(thr_grid)
        thr_defaults = QPushButton("Стандартні пороги")
        thr_defaults.clicked.connect(self.reset_thresholds)
        thr_lay.addWidget(thr_defaults, 0, Qt.AlignLeft)
        thr_info = QLabel("Пороги кольорів карток і попереджень. Сигнал % рахується по шкалі dBm нижче.")
        thr_info.setWordWrap(True)
        thr_info.setStyleSheet("color: #8ea1b2; font-size: 12px;")
        thr_lay.addWidget(thr_info)
        thr_lay.addStretch(1)
        tabs.addTab(thr, "Пороги")

        # ---------------------------------------------------------- sound
        snd = QWidget()
        snd_lay = QVBoxLayout(snd)
        self.alarm_enabled = QCheckBox("Звукові сигнали увімкнені (M — тимчасово вимкнути)")
        self.alarm_enabled.setChecked(bool(alarm["enabled"]))
        self.alarm_link = QCheckBox("Втрата лінку (немає TRACK або немає зв'язку з Ground)")
        self.alarm_link.setChecked(bool(alarm["link_loss"]))
        self.alarm_restore = QCheckBox("Короткий сигнал, коли лінк відновився")
        self.alarm_restore.setChecked(bool(alarm["restore"]))
        snd_lay.addWidget(self.alarm_enabled)
        snd_lay.addWidget(self.alarm_link)
        snd_lay.addWidget(self.alarm_restore)
        snd_grid = QGridLayout()
        self.alarm_quality = QSpinBox()
        self.alarm_quality.setRange(0, 100)
        self.alarm_quality.setSuffix(" %")
        self.alarm_quality.setSpecialValueText("вимкнено")
        self.alarm_quality.setValue(int(alarm["quality_below"]))
        self.alarm_repeat = QSpinBox()
        self.alarm_repeat.setRange(0, 300)
        self.alarm_repeat.setSuffix(" с")
        self.alarm_repeat.setSpecialValueText("один раз")
        self.alarm_repeat.setValue(int(alarm["repeat_s"]))
        snd_grid.addWidget(QLabel("Якість AIR або GND нижче"), 0, 0)
        snd_grid.addWidget(self.alarm_quality, 0, 1)
        snd_grid.addWidget(QLabel("Повторювати, поки триває, кожні"), 1, 0)
        snd_grid.addWidget(self.alarm_repeat, 1, 1)
        snd_lay.addLayout(snd_grid)
        test_btn = QPushButton("Перевірити звук")
        test_btn.clicked.connect(self.test_sound.emit)
        snd_lay.addWidget(test_btn, 0, Qt.AlignLeft)
        snd_lay.addStretch(1)
        tabs.addTab(snd, "Звук")

        # ---------------------------------------------------------- logs
        logs = QWidget()
        logs_lay = QVBoxLayout(logs)
        self.auto_log_check = QCheckBox("Автоматично писати лог польоту (CSV) при запуску")
        self.auto_log_check.setChecked(auto_log)
        logs_lay.addWidget(self.auto_log_check)
        ret_row = QHBoxLayout()
        self.retention_check = QCheckBox("Видаляти логи, старші за")
        self.retention_days = QSpinBox()
        self.retention_days.setRange(1, 3650)
        self.retention_days.setSuffix(" дн.")
        self.retention_check.setChecked(log_retention_days > 0)
        self.retention_days.setValue(log_retention_days if log_retention_days > 0 else 30)
        self.retention_days.setEnabled(log_retention_days > 0)
        self.retention_check.toggled.connect(self.retention_days.setEnabled)
        ret_row.addWidget(self.retention_check)
        ret_row.addWidget(self.retention_days)
        ret_row.addStretch(1)
        logs_lay.addLayout(ret_row)
        logs_info = QLabel(f"Логи: {LOGS_DIR}\nОчищення вимкнене за замовчуванням. "
                           "Файл, що зараз пишеться, ніколи не видаляється.")
        logs_info.setWordWrap(True)
        logs_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        logs_info.setStyleSheet("color: #8ea1b2; font-size: 12px;")
        logs_lay.addWidget(logs_info)
        logs_lay.addStretch(1)
        tabs.addTab(logs, "Логи")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setStyleSheet(
            "QDialog, QWidget { background: #101418; color: #eaf2f8; }"
            # Default indicators are dark-on-dark here: an unticked box vanished.
            "QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #6c8194;"
            " border-radius: 3px; background: #0c1116; }"
            "QCheckBox::indicator:checked { background: #2e86c1; border-color: #5bc8ff; }"
            "QCheckBox:disabled, QSpinBox:disabled { color: #5d6b78; }"
        )

    def reset_defaults(self):
        self.ground_edit.setText(DEFAULT_GROUND_HOST)
        self.air_edit.setText(DEFAULT_AIR_HOST)
        self.rc_edit.setText(DEFAULT_RC_HOST)

    def reset_thresholds(self):
        for key, spin in self.threshold_spins.items():
            spin.setValue(int(round(DEFAULT_THRESHOLDS[key])))

    def values(self):
        return {
            "ground_host": self.ground_edit.text().strip() or DEFAULT_GROUND_HOST,
            "air_host": self.air_edit.text().strip() or DEFAULT_AIR_HOST,
            "rc_host": self.rc_edit.text().strip() or DEFAULT_RC_HOST,
            "auto_log": self.auto_log_check.isChecked(),
            "thresholds": {key: spin.value() for key, spin in self.threshold_spins.items()},
            "alarm": {
                "enabled": self.alarm_enabled.isChecked(),
                "link_loss": self.alarm_link.isChecked(),
                "restore": self.alarm_restore.isChecked(),
                "quality_below": self.alarm_quality.value(),
                "repeat_s": self.alarm_repeat.value(),
            },
            "log_retention_days": self.retention_days.value() if self.retention_check.isChecked() else 0,
        }


# ----------------------------------------------------------------- sound alarm

def _tone_wav(segments, rate: int = 22050) -> bytes:
    """Little WAV of (freq_hz, ms) segments; freq 0 is silence."""
    import io
    import struct
    import wave
    frames = bytearray()
    for freq, ms in segments:
        n = int(rate * ms / 1000)
        fade = max(1, int(rate * 0.006))
        for i in range(n):
            if freq <= 0:
                sample = 0.0
            else:
                env = min(1.0, i / fade, (n - i) / fade)
                sample = 0.55 * env * math.sin(2 * math.pi * freq * i / rate)
            frames += struct.pack("<h", int(sample * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


SOUNDS = {
    # Two-tone, three times: hard to miss, unlike a system "ding".
    "alarm": [(988, 160), (740, 160)] * 3,
    "warn": [(880, 120), (0, 80), (880, 120)],
    "restore": [(660, 90), (880, 90), (1175, 140)],
}


class SoundPlayer:
    """Plays the alarm tones without blocking the UI.

    Uses winsound (stdlib, no Qt Multimedia in the build) with WAV files
    generated once into the data dir; falls back to the system beep. Offline
    mode (the test suite) only records what would have played.
    """

    def __init__(self):
        self.played: list = []
        self._paths: Dict[str, Path] = {}

    def _path(self, name: str) -> Path:
        if name not in self._paths:
            folder = DATA_DIR / "sounds"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{name}.wav"
            data = _tone_wav(SOUNDS[name])
            if not path.exists() or path.stat().st_size != len(data):
                path.write_bytes(data)
            self._paths[name] = path
        return self._paths[name]

    def play(self, name: str):
        self.played.append(name)
        if not NETWORK_ENABLED:
            return
        try:
            import winsound
            winsound.PlaySound(str(self._path(name)),
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            QApplication.beep()


class AlarmMonitor:
    """Decides when to sound, from one evaluation per poll.

    A condition sounds when it starts, then every `repeat_s` while it lasts.
    The link alarm only arms after TRACK has been seen once, so opening the
    app before the drone is powered doesn't start it shrieking.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.armed = False
        self.since: Dict[str, float] = {}
        self.last_played: Dict[str, float] = {}

    def evaluate(self, now: float, track: Optional[bool], qualities, cfg: Dict[str, Any]) -> list:
        """Sounds to play now. `track`: True/False, None = no data at all."""
        out = []
        if track:
            if self.armed and "link" in self.since and cfg.get("restore", True):
                out.append("restore")
            self.armed = True
        conditions = {
            "link": bool(cfg.get("link_loss", True)) and self.armed and not track,
            "quality": bool(track) and cfg.get("quality_below", 0) > 0 and any(
                q is not None and q < cfg["quality_below"] for q in qualities),
        }
        repeat = float(cfg.get("repeat_s", 0) or 0)
        for name, active in conditions.items():
            if not active:
                self.since.pop(name, None)
                self.last_played.pop(name, None)
                continue
            if name not in self.since:
                self.since[name] = now
                self.last_played[name] = now
                out.append("alarm" if name == "link" else "warn")
            elif repeat > 0 and now - self.last_played[name] >= repeat:
                self.last_played[name] = now
                out.append("alarm" if name == "link" else "warn")
        if not cfg.get("enabled", True):
            return []
        # Link loss outranks low quality; one sound per poll is plenty.
        for name in ("alarm", "restore", "warn"):
            if name in out:
                return [name]
        return []


def cleanup_old_logs(days: int, keep: Optional[Path] = None, now: Optional[float] = None) -> list:
    """Delete telemetry_*.csv older than `days` (by modification time).

    Off when days <= 0. Only touches the app's own log files in LOGS_DIR, and
    never the file currently being written.
    """
    if days <= 0:
        return []
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = []
    try:
        candidates = list(LOGS_DIR.glob("telemetry_*.csv"))
    except Exception:
        return removed
    for path in candidates:
        try:
            if keep is not None and path.resolve() == Path(keep).resolve():
                continue
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except Exception:
            pass
    return removed


# CSV log layout, one row per poll. (column, label for the Logs tab pickers.)
# Units are in the column names so the file is readable on its own in Excel.
LOG_COLUMNS = (
    ("timestamp", "Час"),
    ("source", "Джерело (live / demo)"),
    ("link", "Лінк"),
    ("uav", "UAV"),
    ("distance_m", "Відстань, м"),
    ("air_rssi0_dbm", "AIR RSSI RF1, dBm"),
    ("air_rssi1_dbm", "AIR RSSI RF2, dBm"),
    ("air_rssi_best_dbm", "AIR RSSI кращий, dBm"),
    ("ground_rssi0_dbm", "GND RSSI RF1, dBm"),
    ("ground_rssi1_dbm", "GND RSSI RF2, dBm"),
    ("ground_rssi_best_dbm", "GND RSSI кращий, dBm"),
    ("air_snr_db", "AIR SNR, dB"),
    ("ground_snr_db", "GND SNR, dB"),
    ("air_quality_pct", "AIR якість T, %"),
    ("ground_quality_pct", "GND якість T, %"),
    ("air_lq_pct", "AIR якість P (модуль), %"),
    ("ground_lq_pct", "GND якість P (модуль), %"),
    ("air_fec_err_pct", "AIR помилки FEC, %"),
    ("ground_fec_err_pct", "GND помилки FEC, %"),
    ("mcs", "MCS"),
    ("capacity_mbps", "Ємність каналу, Mb/s"),
    ("freq_mhz", "Частота, MHz"),
    ("downlink_kbs", "Downlink, kb/s"),
    ("uplink_kbs", "Uplink, kb/s"),
    ("air_temp_c", "AIR темп., °C"),
    ("ground_temp_c", "GND темп., °C"),
    ("air_ping_ms", "AIR пінг, ms"),
    ("ground_ping_ms", "GND пінг, ms"),
    ("rc_ping_ms", "RC пінг, ms"),
    ("air_ant", "AIR антена"),
    ("ground_ant", "GND антена"),
    ("air_tx_dbm", "AIR TX, dBm"),
    ("ground_tx_dbm", "GND TX, dBm"),
    ("warnings", "Попередження"),
)
LOG_FIELDS = [name for name, _ in LOG_COLUMNS]
LOG_LABELS = dict(LOG_COLUMNS)
# v1.39 and earlier wrote asdict(TelemetryState). The Logs tab still opens
# those files; these map the new column names onto the old ones.
LOG_ALIASES = {
    "link": ("link_summary",),
    "distance_m": ("distance_value",),
    "air_snr_db": ("air_snr",),
    "ground_snr_db": ("ground_snr",),
    "air_rssi_best_dbm": ("air_best_rssi",),
    "ground_rssi_best_dbm": ("ground_best_rssi",),
    "air_ping_ms": ("air_latency_ms",),
    "ground_ping_ms": ("ground_latency_ms",),
    "air_temp_c": ("air_temp_c",),
    "ground_temp_c": ("ground_temp_c",),
}


def log_label(column: str) -> str:
    """Picker label for a column, including the old v1.39 column names."""
    if column in LOG_LABELS:
        return LOG_LABELS[column]
    for name, aliases in LOG_ALIASES.items():
        if column in aliases:
            return LOG_LABELS[name]
    return column


def _fmt_num(v: Optional[float], digits: int = 0) -> str:
    """Number for the CSV: blank when missing, no trailing '.0' noise."""
    if v is None:
        return ""
    if digits == 0:
        return str(int(round(v)))
    return f"{v:.{digits}f}"


class CsvLogger:
    """Flight log, one CSV row per poll.

    The file is opened lazily on the first row, so starting the app with no
    radio in range does not litter the logs folder with empty files. Rows are
    flushed as they are written: a crash or a pulled battery loses at most the
    current row, and the Logs tab can open the file that is still recording.
    """

    def __init__(self):
        self.file = None
        self.writer = None
        self.path: Optional[Path] = None
        self.enabled = False
        self.demo = False
        self.error = ""

    def start(self, demo: bool = False):
        self.stop()
        self.enabled = True
        self.demo = demo
        self.error = ""

    def stop(self):
        self.enabled = False
        if self.file:
            try:
                self.file.close()
            except Exception:
                pass
        self.file = None
        self.writer = None
        self.path = None

    def _open(self):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.path = LOGS_DIR / f"telemetry_{'demo_' if self.demo else ''}{stamp}.csv"
        # utf-8-sig so Excel picks the encoding up instead of mangling °C.
        self.file = self.path.open('w', newline='', encoding='utf-8-sig')
        self.writer = csv.DictWriter(self.file, fieldnames=LOG_FIELDS, extrasaction='ignore')
        self.writer.writeheader()

    def log(self, row: Dict[str, Any]):
        if not self.enabled:
            return
        try:
            if self.file is None:
                self._open()
            self.writer.writerow(row)
            self.file.flush()
        except Exception as e:
            # A full disk or a vanished USB stick must not take the overlay
            # down with it; stop logging and let the REC button say why.
            self.error = str(e)
            self.stop()


# ----------------------------------------------------------------- log analysis

def parse_log_time(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text).strip())
    except Exception:
        return None


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class LogEvent:
    t: float            # seconds from the start of the log
    row: int            # row index, for highlighting on the scatter plot
    text: str
    kind: str           # start / end / loss / restore / gap / warn


class LogData:
    """A loaded CSV log: raw rows plus every numeric column as floats."""

    def __init__(self, path: Path, columns: list, rows: list):
        self.path = Path(path)
        self.columns = columns
        self.rows = rows
        stamps = [parse_log_time(r.get("timestamp", "")) for r in rows]
        first = next((s for s in stamps if s is not None), None)
        self.stamps = stamps
        self.t = [None if (s is None or first is None) else (s - first).total_seconds() for s in stamps]
        self.numeric: Dict[str, list] = {}
        for col in columns:
            if col == "timestamp":
                continue
            values = [parse_number(r.get(col)) if r.get(col) not in (None, "") else None for r in rows]
            filled = [r.get(col) for r in rows if r.get(col) not in (None, "")]
            parsed = [v for v in values if v is not None]
            # Numeric if most non-empty cells parse; "auto-rf2" or "TRACK" don't.
            if parsed and len(parsed) >= 0.8 * len(filled):
                self.numeric[col] = values
        link_col = self.col("link")
        self.track = [
            None if not link_col else str(r.get(link_col, "")).strip().upper() == "TRACK"
            for r in rows
        ]

    def col(self, name: str) -> Optional[str]:
        """Resolve a v1.40 column name against this file (old logs use aliases)."""
        if name in self.columns:
            return name
        for alias in LOG_ALIASES.get(name, ()):
            if alias in self.columns:
                return alias
        return None

    def series(self, name: str) -> Optional[list]:
        col = self.col(name)
        return self.numeric.get(col) if col else None

    @property
    def duration(self) -> Optional[float]:
        ts = [t for t in self.t if t is not None]
        return ts[-1] if ts else None


def load_log(path) -> LogData:
    with Path(path).open(newline='', encoding='utf-8-sig', errors='replace') as f:
        reader = csv.DictReader(f)
        rows = [row for row in reader]
        columns = list(reader.fieldnames or [])
    return LogData(Path(path), columns, rows)


def linear_fit(xs, ys) -> Optional[tuple]:
    """Least squares y = a + b*x over the pairs where both are present.

    Returns (intercept, slope, pearson_r, n) or None if there is too little
    data or no spread in x.
    """
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    if sxx <= 0:
        return None
    slope = sxy / sxx
    r = sxy / math.sqrt(sxx * syy) if syy > 0 else 0.0
    return (my - slope * mx, slope, r, n)


_NUM_RE = re.compile(r"[+\-]?\d+(?:\.\d+)?")


def _warning_key(text: str) -> str:
    """'AIR latency 140 ms' and 'AIR latency 180 ms' are the same warning."""
    return _NUM_RE.sub("#", text.strip())


def detect_events(data: LogData, gap_s: float = max(5.0, 4 * POLL_MS / 1000.0), limit: int = 1000) -> list:
    """Things worth jumping to: link loss/restore, data gaps, new warnings."""
    events: list = []
    rows = data.rows
    if not rows:
        return events
    link_col = data.col("link")
    prev_link = None
    loss_start: Optional[float] = None
    prev_t: Optional[float] = None
    prev_warn: set = set()
    for i, row in enumerate(rows):
        t = data.t[i]
        tt = t if t is not None else (prev_t or 0.0)
        if i == 0:
            events.append(LogEvent(tt, i, "Початок запису", "start"))
        if prev_t is not None and t is not None and t - prev_t > gap_s:
            events.append(LogEvent(prev_t, i - 1, f"Немає даних {format_duration(t - prev_t)}", "gap"))
        if link_col:
            link = str(row.get(link_col, "")).strip() or "N/A"
            if prev_link is not None and link != prev_link:
                if link.upper() == "TRACK":
                    lost_for = "" if loss_start is None else f" після {format_duration(tt - loss_start)}"
                    events.append(LogEvent(tt, i, f"Лінк відновлено (TRACK){lost_for}", "restore"))
                    loss_start = None
                else:
                    if prev_link.upper() == "TRACK":
                        loss_start = tt
                    events.append(LogEvent(tt, i, f"Втрата лінку: {link}", "loss"))
            prev_link = link
        warn_text = str(row.get("warnings", "") or "")
        current = {w.strip() for w in warn_text.split("|") if w.strip()}
        keys = {_warning_key(w): w for w in current}
        for key, text in keys.items():
            if key not in prev_warn:
                events.append(LogEvent(tt, i, f"⚠ {text}", "warn"))
        prev_warn = set(keys)
        if t is not None:
            prev_t = t
        if len(events) >= limit:
            events.append(LogEvent(tt, i, f"... показано перші {limit} подій", "end"))
            return events
    events.append(LogEvent(prev_t or 0.0, len(rows) - 1, "Кінець запису", "end"))
    return events


def _min_max(values: Optional[list], pick=min) -> Optional[tuple]:
    """(value, row index) of the min or max, ignoring blanks."""
    if not values:
        return None
    pairs = [(v, i) for i, v in enumerate(values) if v is not None]
    if not pairs:
        return None
    return pick(pairs, key=lambda p: p[0])


def summarize_log(data: LogData) -> list:
    """Human-readable findings, one per line."""
    lines = []
    n = len(data.rows)
    lines.append(f"{data.path.name} · {n} рядків · тривалість {format_duration(data.duration)}")
    if n == 0:
        return lines

    def at(i):
        t = data.t[i] if i is not None and i < len(data.t) else None
        return f" (на {format_duration(t)})" if t is not None else ""

    if any(v is not None for v in data.track):
        known = [v for v in data.track if v is not None]
        track_pct = 100.0 * sum(1 for v in known if v) / len(known)
        losses = sum(
            1 for i in range(1, len(data.track))
            if data.track[i - 1] and data.track[i] is False
        )
        lines.append(f"Лінк TRACK {track_pct:.0f}% часу · втрат лінку: {losses}")

    dist = data.series("distance_m")
    peak = _min_max(dist, max)
    if peak:
        lines.append(f"Макс. відстань {peak[0]:.0f} м{at(peak[1])}")

    parts = []
    for side, label in (("air", "AIR"), ("ground", "GND")):
        low = _min_max(data.series(f"{side}_snr_db"), min)
        if low:
            parts.append(f"{label} {low[0]:g} dB{at(low[1])}")
    if parts:
        lines.append("Мін. SNR: " + " · ".join(parts))

    parts = []
    for side, label in (("air", "AIR"), ("ground", "GND")):
        low = _min_max(data.series(f"{side}_rssi_best_dbm"), min)
        if low:
            parts.append(f"{label} {low[0]:g} dBm{at(low[1])}")
    if parts:
        lines.append("Мін. RSSI: " + " · ".join(parts))

    # The question this tab exists for: how does the link degrade with range?
    # Fit SNR against distance over the rows where the link was actually up.
    if dist:
        for side, label in (("air", "AIR"), ("ground", "GND")):
            snr = data.series(f"{side}_snr_db")
            if not snr:
                continue
            xs = [d / 1000.0 if (d is not None and data.track[i] is not False) else None for i, d in enumerate(dist)]
            fit = linear_fit(xs, snr)
            if fit and fit[1] < 0 and max(x for x in xs if x is not None) > 0:
                a, b, r, cnt = fit
                # Where the trend line crosses the bottom of the SNR scale.
                floor_km = (6.0 - a) / b
                lines.append(
                    f"{label} SNR ≈ {a:.1f} {b:+.1f} dB/км (r={r:.2f}, {cnt} точок)"
                    + (f" → SNR 6 dB прогноз на ~{floor_km:.1f} км" if floor_km > 0 else "")
                )
        for side, label in (("air", "AIR"), ("ground", "GND")):
            q = data.series(f"{side}_quality_pct")
            if not q:
                continue
            first_bad = next((i for i, v in enumerate(q) if v is not None and v < 45), None)
            if first_bad is not None and dist[first_bad] is not None:
                lines.append(f"{label} якість < 45% вперше на {dist[first_bad]:.0f} м{at(first_bad)}")

    parts = []
    for side, label in (("air", "AIR"), ("ground", "GND")):
        hot = _min_max(data.series(f"{side}_temp_c"), max)
        if hot:
            parts.append(f"{label} {hot[0]:.0f} °C")
    if parts:
        lines.append("Макс. темп.: " + " · ".join(parts))

    parts = []
    for side, label in (("air", "AIR"), ("ground", "GND"), ("rc", "RC")):
        slow = _min_max(data.series(f"{side}_ping_ms"), max)
        if slow:
            parts.append(f"{label} {slow[0]:.0f} ms{at(slow[1])}")
    if parts:
        lines.append("Макс. пінг: " + " · ".join(parts))
    return lines


class _FillSplitter(QSplitter):
    """Splitter that asks for little and takes whatever space is left.

    The tabs live in a QScrollArea, which sizes its page from the preferred
    height. pyqtgraph prefers 600 px, which pushed the event list below the
    bottom of the window behind a scrollbar.
    """

    def sizeHint(self):
        return QSize(300, 260)


class LogsTab(QWidget):
    """Opens a flight log and helps find why something happened.

    Two views over the same file: a metric against time (with link-loss
    stretches shaded red), or any metric against any other (distance vs SNR
    by default) with a trend line. The event list on the side jumps to link
    losses, data gaps and the moments new warnings appeared.
    """

    MODE_TIME = 0
    MODE_SCATTER = 1
    DEFAULT_X = ("distance_m",)
    DEFAULT_Y = ("air_snr_db", "ground_snr_db", "air_rssi_best_dbm")

    def __init__(self, recording_path=None):
        super().__init__()
        self.setObjectName("logsTab")
        self._recording_path = recording_path or (lambda: None)
        self.data: Optional[LogData] = None
        self.events: list = []
        self._marker = None
        self._highlight = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        files_row = QHBoxLayout()
        files_row.setSpacing(6)
        self.file_combo = QComboBox()
        self.file_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.file_combo.setMinimumContentsLength(10)
        self.file_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.file_combo.activated.connect(self._on_file_chosen)
        files_row.addWidget(self.file_combo, 1)
        self.refresh_btn = QPushButton("⟳")
        self.refresh_btn.setToolTip("Оновити список і перечитати файл")
        self.open_btn = QPushButton("Відкрити…")
        self.folder_btn = QPushButton("Папка")
        self.folder_btn.setToolTip(str(LOGS_DIR))
        for btn in (self.refresh_btn, self.open_btn, self.folder_btn):
            btn.setObjectName("topBtn")
            files_row.addWidget(btn)
        self.refresh_btn.clicked.connect(self.reload)
        self.open_btn.clicked.connect(self._open_dialog)
        self.folder_btn.clicked.connect(self._open_folder)
        layout.addLayout(files_row)

        self.summary = QLabel("Логів ще немає. Запис вмикається кнопкою REC або клавішею R.")
        self.summary.setObjectName("logSummary")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.summary)

        axes_row = QHBoxLayout()
        axes_row.setSpacing(6)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["За часом", "X ↔ Y"])
        self.mode_combo.setToolTip("За часом: метрика Y протягом польоту.\nX ↔ Y: залежність, напр. SNR від відстані.")
        self.x_label = QLabel("X")
        self.x_combo = QComboBox()
        self.y_combo = QComboBox()
        for combo in (self.x_combo, self.y_combo):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(6)
            combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        axes_row.addWidget(self.mode_combo)
        axes_row.addWidget(self.x_label)
        axes_row.addWidget(self.x_combo, 1)
        axes_row.addWidget(QLabel("Y"))
        axes_row.addWidget(self.y_combo, 1)
        layout.addLayout(axes_row)
        self.mode_combo.setCurrentIndex(self.MODE_SCATTER)
        self.mode_combo.currentIndexChanged.connect(self.redraw)
        self.x_combo.currentIndexChanged.connect(self.redraw)
        self.y_combo.currentIndexChanged.connect(self.redraw)

        splitter = _FillSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        if pg is not None:
            self.plot = pg.PlotWidget()
            self.plot.showGrid(x=True, y=True, alpha=0.2)
            self.plot.setMinimumHeight(140)
            splitter.addWidget(self.plot)
        else:
            self.plot = None
            placeholder = QLabel("pyqtgraph не встановлено — графік недоступний")
            placeholder.setAlignment(Qt.AlignCenter)
            splitter.addWidget(placeholder)
        self.event_list = QListWidget()
        self.event_list.setObjectName("eventList")
        self.event_list.setMinimumHeight(80)
        self.event_list.currentRowChanged.connect(self._on_event_selected)
        splitter.addWidget(self.event_list)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

    # ------------------------------------------------------------ file list

    def log_files(self) -> list:
        try:
            files = [p for p in LOGS_DIR.glob("*.csv") if p.is_file()]
        except Exception:
            return []
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def refresh_files(self, select: Optional[Path] = None):
        current = select or (self.data.path if self.data else None)
        recording = self._recording_path()
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        paths = self.log_files()
        if current is not None and current not in paths and Path(current).exists():
            paths.insert(0, Path(current))
        for p in paths:
            try:
                kb = p.stat().st_size / 1024.0
            except Exception:
                kb = 0.0
            mark = "  ● запис" if recording is not None and Path(recording) == p else ""
            self.file_combo.addItem(f"{p.name}  ({kb:.0f} KB){mark}", str(p))
        if current is not None:
            idx = self.file_combo.findData(str(current))
            if idx >= 0:
                self.file_combo.setCurrentIndex(idx)
        self.file_combo.blockSignals(False)
        return paths

    def showEvent(self, event):
        super().showEvent(event)
        paths = self.refresh_files()
        if self.data is None and paths:
            self.load_path(paths[0])

    def reload(self):
        self.refresh_files()
        if self.data is not None:
            self.load_path(self.data.path)
        elif self.file_combo.count():
            self.load_path(Path(self.file_combo.itemData(0)))

    def _on_file_chosen(self, index: int):
        path = self.file_combo.itemData(index)
        if path:
            self.load_path(Path(path))

    def _open_dialog(self):
        start = str(LOGS_DIR if LOGS_DIR.exists() else Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Відкрити лог", start, "CSV (*.csv);;Усі файли (*)")
        if path:
            self.load_path(Path(path))
            self.refresh_files(Path(path))

    def _open_folder(self):
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOGS_DIR)))

    # ------------------------------------------------------------ loading

    def load_path(self, path: Path):
        try:
            data = load_log(path)
        except Exception as e:
            self.data = None
            self.summary.setText(f"Не вдалося відкрити {Path(path).name}: {e}")
            self.event_list.clear()
            if self.plot is not None:
                self.plot.clear()
            return
        self.data = data
        self._fill_axes()
        self.summary.setText("\n".join(summarize_log(data)))
        self.events = detect_events(data)
        idx = self.file_combo.findData(str(data.path))
        if idx < 0:
            self.refresh_files(data.path)
        else:
            self.file_combo.setCurrentIndex(idx)
        self.event_list.blockSignals(True)
        self.event_list.clear()
        colors = {"loss": "#ff7b7b", "gap": "#ffb347", "warn": "#ffd24d", "restore": "#2ecc71"}
        for ev in self.events:
            item = QListWidgetItem(f"{format_duration(ev.t):>7}   {ev.text}")
            if ev.kind in colors:
                item.setForeground(QColor(colors[ev.kind]))
            self.event_list.addItem(item)
        self.event_list.blockSignals(False)
        self.redraw()

    def _fill_axes(self):
        prev_x = self.x_combo.currentData()
        prev_y = self.y_combo.currentData()
        cols = [c for c in self.data.columns if c in self.data.numeric]
        for combo in (self.x_combo, self.y_combo):
            combo.blockSignals(True)
            combo.clear()
            for c in cols:
                combo.addItem(log_label(c), c)
        self._select(self.x_combo, prev_x, self.DEFAULT_X)
        self._select(self.y_combo, prev_y, self.DEFAULT_Y)
        for combo in (self.x_combo, self.y_combo):
            combo.blockSignals(False)

    def _select(self, combo: QComboBox, previous, defaults):
        for name in ([previous] if previous else []) + [self.data.col(d) for d in defaults]:
            if not name:
                continue
            idx = combo.findData(name)
            if idx >= 0:
                combo.setCurrentIndex(idx)
                return

    # ------------------------------------------------------------ drawing

    def redraw(self, *_):
        scatter = self.mode_combo.currentIndex() == self.MODE_SCATTER
        self.x_label.setVisible(scatter)
        self.x_combo.setVisible(scatter)
        if self.plot is None:
            return
        self.plot.clear()
        self._marker = None
        self._highlight = None
        if self.data is None or not self.data.rows:
            return
        y_col = self.y_combo.currentData()
        x_col = self.x_combo.currentData()
        if not y_col:
            return
        ys = self.data.numeric.get(y_col, [])
        nan = float("nan")
        if scatter:
            if not x_col:
                return
            xs = self.data.numeric.get(x_col, [])
            good_x, good_y, bad_x, bad_y = [], [], [], []
            for i, (x, y) in enumerate(zip(xs, ys)):
                if x is None or y is None:
                    continue
                if self.data.track[i] is False:
                    bad_x.append(x)
                    bad_y.append(y)
                else:
                    good_x.append(x)
                    good_y.append(y)
            self.plot.addItem(pg.ScatterPlotItem(good_x, good_y, size=6, pen=None, brush=pg.mkBrush(46, 204, 113, 150)))
            if bad_x:
                self.plot.addItem(pg.ScatterPlotItem(bad_x, bad_y, size=7, pen=None, brush=pg.mkBrush(255, 92, 92, 200)))
            fit = linear_fit([x if self.data.track[i] is not False else None for i, x in enumerate(xs)], ys)
            title = ""
            if fit and xs:
                a, b, r, n = fit
                present = [x for x in xs if x is not None]
                lo, hi = min(present), max(present)
                self.plot.plot([lo, hi], [a + b * lo, a + b * hi], pen=pg.mkPen("#5bc8ff", width=2, style=Qt.DashLine))
                title = f"тренд: y = {a:.2f} {b:+.4f}·x   r = {r:.2f}   n = {n}"
            self.plot.setTitle(title, color="#b9c7d3", size="9pt")
            self.plot.setLabel("bottom", log_label(x_col))
        else:
            ts = [nan if t is None else t / 60.0 for t in self.data.t]
            vals = [nan if v is None else v for v in ys]
            self.plot.plot(ts, vals, pen=pg.mkPen("#5bc8ff", width=2), connect="finite")
            # Shade every stretch where the link was not TRACK.
            start = None
            for i, ok in enumerate(self.data.track + [True]):
                t = self.data.t[i] if i < len(self.data.t) else None
                if ok is False and start is None and t is not None:
                    start = t
                elif ok is not False and start is not None:
                    end = self.data.t[i - 1] if self.data.t[i - 1] is not None else start
                    if end <= start:  # a single row: give it one poll of width
                        end = start + POLL_MS / 1000.0
                    region = pg.LinearRegionItem(
                        values=(start / 60.0, end / 60.0),
                        movable=False, brush=pg.mkBrush(255, 92, 92, 50), pen=pg.mkPen(None))
                    self.plot.addItem(region)
                    start = None
            self.plot.setTitle("червоне — лінк не TRACK", color="#b9c7d3", size="9pt")
            self.plot.setLabel("bottom", "хвилини від початку")
        self.plot.setLabel("left", log_label(y_col))
        self.plot.enableAutoRange()
        self._on_event_selected(self.event_list.currentRow())

    def _on_event_selected(self, index: int):
        if self.plot is None or self.data is None or not (0 <= index < len(self.events)):
            return
        ev = self.events[index]
        if self.mode_combo.currentIndex() == self.MODE_TIME:
            if self._marker is None:
                self._marker = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#ffd24d", width=2))
                self.plot.addItem(self._marker)
            self._marker.setPos(ev.t / 60.0)
        else:
            x_col, y_col = self.x_combo.currentData(), self.y_combo.currentData()
            xs = self.data.numeric.get(x_col, [])
            ys = self.data.numeric.get(y_col, [])
            if ev.row < len(xs) and xs[ev.row] is not None and ys[ev.row] is not None:
                if self._highlight is None:
                    self._highlight = pg.ScatterPlotItem(size=16, pen=pg.mkPen("#ffd24d", width=2), brush=None)
                    self.plot.addItem(self._highlight)
                self._highlight.setData([xs[ev.row]], [ys[ev.row]])


class PollWorker(QObject):
    data_ready = Signal(dict)
    error_ready = Signal(str)

    def __init__(self):
        super().__init__()
        self.session = requests.Session() if requests else None
        self.timer = None
        self.busy = False
        self.api_url = ""
        self._stopping = False

    @Slot(str)
    def set_api_url(self, url: str):
        """Invoked from the GUI thread via a queued signal, runs on the worker."""
        self.api_url = url.strip()

    @Slot()
    def start(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)
        self.poll()

    def request_stop(self):
        """Callable from any thread: only flips the flag, touches no QObject."""
        self._stopping = True

    @Slot()
    def poll(self):
        if self.busy or self._stopping:
            return
        if self.session is None:
            self.error_ready.emit("requests missing")
            return
        if not self.api_url:
            self.error_ready.emit("API URL is empty")
            return

        self.busy = True
        try:
            r = self.session.get(self.api_url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            self.data_ready.emit(r.json())
        except Exception as e:
            self.error_ready.emit(str(e))
        finally:
            self.busy = False


class NetworkWorker(QObject):
    """Pings every configured host on one timer, all hosts concurrently.

    This replaces the old PingWorker + LatencyWorker pair, which each ran their
    own timer and pinged sequentially: up to five `ping` subprocesses per second
    with a worst case of ~2s of serialised blocking per sweep.
    """

    results_ready = Signal(dict)

    KEYS = ("air", "ground", "rc")

    def __init__(self):
        super().__init__()
        self.timer = None
        self.hosts: Dict[str, str] = {key: "" for key in self.KEYS}
        self.busy = False
        self.pool: Optional[ThreadPoolExecutor] = None
        self._stopping = False

    @Slot(dict)
    def set_hosts(self, hosts: Dict[str, str]):
        for key in self.KEYS:
            if key in hosts:
                self.hosts[key] = (hosts.get(key) or "").strip()

    @Slot()
    def start(self):
        self.pool = ThreadPoolExecutor(max_workers=len(self.KEYS), thread_name_prefix="ping")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(PING_INTERVAL_MS)
        self.poll()

    def request_stop(self):
        """Callable from any thread: only flips the flag, touches no QObject."""
        self._stopping = True

    @Slot()
    def poll(self):
        if self.busy or self._stopping or self.pool is None:
            return
        self.busy = True
        try:
            hosts = dict(self.hosts)
            futures = {
                key: self.pool.submit(ping_latency_ms, host, PING_TIMEOUT_MS)
                for key, host in hosts.items() if host
            }
            latency: Dict[str, Optional[int]] = {key: None for key in self.KEYS}
            for key, future in futures.items():
                try:
                    latency[key] = future.result(timeout=(PING_TIMEOUT_MS / 1000.0) + 2.0)
                except Exception:
                    latency[key] = None
            if not self._stopping:
                self.results_ready.emit({"hosts": hosts, "latency": latency})
        finally:
            self.busy = False


class MetricCard(QFrame):
    """A single telemetry tile.

    The card owns its own rendering: callers push logical values and the card
    decides what to paint. Every widget-level write is guarded by a cache, so a
    poll that changes nothing costs nothing. In particular the card, not the
    window, is responsible for the compact (title-inline) rendering -- the old
    code had the window rewrite `card.title` behind `update_data`'s back, which
    left stale values baked into the title text.
    """

    def __init__(self, title: str, big: bool = False):
        super().__init__()
        self.setObjectName("card")
        self._base_title = title
        self._big = big

        # Logical state.
        self._value_text = "--"
        self._detail_text = ""
        self._color: Optional[str] = None
        self._status_text: Optional[str] = None
        self._accent: Optional[str] = None
        self._compact = False
        self._value_px: Optional[int] = None

        # What has actually been pushed to the widgets.
        self._painted_title: Optional[str] = None
        self._painted_value: Optional[str] = None
        self._painted_detail: Optional[str] = None
        self._painted_value_style: Optional[str] = None
        self._painted_accent_style: Optional[str] = None
        self._painted_widths = (0, 0, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        # Cards stretch to fill the grid row, so centre the content instead of
        # pinning it to the top with a lump of dead space underneath.
        layout.addStretch(1)

        self.title = QLabel(title)
        self.title.setObjectName("cardTitle")
        self.title.setWordWrap(False)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout.addWidget(self.title)

        self.value = QLabel("--")
        self.value.setObjectName("cardValueBig" if big else "cardValue")
        self.value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.value.setWordWrap(False)
        self.value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.value.setMinimumWidth(0)
        layout.addWidget(self.value)

        self.detail = QLabel("")
        self.detail.setObjectName("cardDetail")
        self.detail.setWordWrap(False)
        self.detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.detail.setMinimumWidth(0)
        layout.addWidget(self.detail)
        layout.addStretch(1)

    def update_data(self, value: str, detail: str = "", color: Optional[str] = None, status_text: Optional[str] = None, accent: Optional[str] = None):
        changed = False
        for attr, new in (
            ("_value_text", value),
            ("_detail_text", detail),
            ("_color", color),
            ("_status_text", status_text),
            ("_accent", accent),
        ):
            if getattr(self, attr) != new:
                setattr(self, attr, new)
                changed = True
        if changed:
            self._render()

    def set_compact(self, compact: bool):
        """Inline mode: the value is folded into the title to save vertical space."""
        if compact == self._compact:
            return
        self._compact = compact
        self._render()

    def set_value_font_px(self, px: Optional[int]):
        """Explicit value font size. Composed with the colour in one stylesheet
        so the two no longer overwrite each other."""
        if px == self._value_px:
            return
        self._value_px = px
        self._render()

    def _elide(self, label: QLabel, text: str) -> str:
        width = label.width()
        if width <= 2:
            return text
        metrics = label.fontMetrics()
        if metrics.horizontalAdvance(text) <= width:
            label.setToolTip("")
            return text
        label.setToolTip(text)
        return metrics.elidedText(text, Qt.ElideRight, width)

    def _render(self):
        title = self._base_title
        if self._status_text:
            title = f"{title} • {self._status_text}"
        if self._compact and self._value_text:
            title = f"{title} — {self._value_text}"

        widths = (self.title.width(), self.value.width(), self.detail.width())
        width_changed = widths != self._painted_widths
        self._painted_widths = widths

        if title != self._painted_title or width_changed:
            self._painted_title = title
            self.title.setText(self._elide(self.title, title))

        show_value = not self._compact
        if self.value.isVisibleTo(self) != show_value:
            self.value.setVisible(show_value)
        if show_value and (self._value_text != self._painted_value or width_changed):
            self._painted_value = self._value_text
            self.value.setText(self._elide(self.value, self._value_text))

        if self._detail_text != self._painted_detail or width_changed:
            self._painted_detail = self._detail_text
            self.detail.setText(self._elide(self.detail, self._detail_text))

        value_style = ""
        if self._color:
            value_style += f"color: {self._color};"
        if self._value_px:
            value_style += f" font-size: {self._value_px}px; font-weight: 900;"
        if value_style != self._painted_value_style:
            self._painted_value_style = value_style
            self.value.setStyleSheet(value_style)

        accent_style = (
            f"#card{{background:{self._accent}; border:1px solid #3a4652; border-radius:10px;}}"
            if self._accent else ""
        )
        if accent_style != self._painted_accent_style:
            self._painted_accent_style = accent_style
            self.setStyleSheet(accent_style)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-elide against the new width.
        self._render()


def elide_into(label: QLabel, text: str) -> None:
    """Set `text` on `label`, eliding to its width; full text goes to the tooltip."""
    width = label.width()
    metrics = label.fontMetrics()
    if width <= 2 or metrics.horizontalAdvance(text) <= width:
        label.setToolTip("")
        label.setText(text)
        return
    label.setToolTip(text)
    label.setText(metrics.elidedText(text, Qt.ElideRight, width))


def dbm_to_mw(dbm: Optional[float]) -> Optional[float]:
    return None if dbm is None else 10 ** (dbm / 10.0)


def format_tx_power(dbm: Optional[float]) -> str:
    if dbm is None:
        return "TX --"
    mw = dbm_to_mw(dbm)
    mw_text = f"{mw:.1f}" if mw < 10 else f"{mw:.0f}"
    return f"TX {dbm:g} dBm · {mw_text} mW"


class AntennaBar(QWidget):
    """One RF chain: label, a level bar scaled like the signal %, and dBm.

    Painted by hand rather than built from QProgressBar + labels: there are
    four of these updating every poll, and a single paintEvent that only runs
    when the value actually changes is far cheaper than restyling widgets.
    """

    def __init__(self, label: str):
        super().__init__()
        self.setObjectName("antBar")
        self.label = label
        self.dbm: Optional[float] = None
        self.active = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_state(self, dbm: Optional[float], active: bool):
        if dbm == self.dbm and active == self.active:
            return
        self.dbm = dbm
        self.active = active
        value = "--" if dbm is None else f"{dbm:.0f} dBm"
        self.setToolTip(f"{self.label}: {value}" + ("  (active antenna)" if active else ""))
        self.update()

    def sizeHint(self):
        return QSize(220, self.fontMetrics().height() + 8)

    def minimumSizeHint(self):
        return QSize(120, self.fontMetrics().height() + 6)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.updateGeometry()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        fm = painter.fontMetrics()
        pct = best_rssi_to_percent(self.dbm)
        color = QColor(signal_color(pct))

        dot = max(6, fm.height() // 3)
        label_x = dot + 6
        label_w = fm.horizontalAdvance("RF2") + 8
        value_font = painter.font()
        value_font.setBold(True)
        value_w = fm.horizontalAdvance("-100 dBm") + 10

        if self.active:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(0, (h - dot) / 2.0, dot, dot))
        painter.setPen(QColor("#eaf2f8" if self.active else "#8ea1b2"))
        painter.drawText(QRectF(label_x, 0, label_w, h), int(Qt.AlignVCenter | Qt.AlignLeft), self.label)

        bar_x = label_x + label_w
        bar_w = max(10.0, w - bar_x - value_w)
        bar_h = max(6.0, round(h * 0.42))
        bar_y = (h - bar_h) / 2.0
        radius = bar_h / 2.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#0c1116"))
        painter.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), radius, radius)
        if pct:
            fill_w = max(bar_h, bar_w * pct / 100.0)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), radius, radius)

        painter.setFont(value_font)
        painter.setPen(color)
        text = "--" if self.dbm is None else f"{self.dbm:.0f} dBm"
        painter.drawText(QRectF(w - value_w, 0, value_w, h), int(Qt.AlignVCenter | Qt.AlignRight), text)
        painter.end()


class SignalCard(QFrame):
    """Signal tile for one side of the link: both antennas plus TX power.

    Replaces the old "best RSSI as a percentage" card, which only ever showed
    the stronger chain and saturated at 100 %, and the separate ANTENNA card,
    whose content (which chain is active) now lives on the bars themselves.
    """

    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("card")
        self._base_title = title
        self._compact = False
        self._title_text = title
        self._detail_text = ""
        self._tx_text = "TX --"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        layout.addStretch(1)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title = QLabel(title)
        self.title.setObjectName("cardTitle")
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        header.addWidget(self.title, 1)
        self.tx = QLabel(self._tx_text)
        self.tx.setObjectName("txPill")
        self.tx.setToolTip("Configured module output power")
        header.addWidget(self.tx, 0, Qt.AlignRight)
        layout.addLayout(header)

        self.bars = [AntennaBar("RF1"), AntennaBar("RF2")]
        for bar in self.bars:
            layout.addWidget(bar)

        self.detail = QLabel("")
        self.detail.setObjectName("cardDetail")
        self.detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout.addWidget(self.detail)
        layout.addStretch(1)

    def update_data(self, pct, rssi0, rssi1, active_index, mode, tx_dbm, note: str = ""):
        self.bars[0].set_state(rssi0, active_index == 0)
        self.bars[1].set_state(rssi1, active_index == 1)
        title = self._base_title if pct is None else f"{self._base_title} • {pct}%"
        tx_text = format_tx_power(tx_dbm)
        parts = []
        if note:
            parts.append(note)
        else:
            if mode:
                parts.append(mode)
            if active_index is not None:
                parts.append(f"active RF{active_index + 1}")
            if rssi0 is not None and rssi1 is not None:
                parts.append(f"RF1-RF2 {rssi0 - rssi1:+.0f} dB")
        detail = " · ".join(parts)
        if (title, detail, tx_text) != (self._title_text, self._detail_text, self._tx_text):
            self._title_text, self._detail_text, self._tx_text = title, detail, tx_text
            self.tx.setText(tx_text)
            self._render()

    def set_compact(self, compact: bool):
        if compact == self._compact:
            return
        self._compact = compact
        self.detail.setVisible(not compact)

    def set_value_font_px(self, px):
        pass

    def setMinimumHeight(self, height: int):
        # Never let the responsive layout squeeze the two bars together.
        layout = self.layout()
        floor = layout.minimumSize().height() if layout is not None else 0
        super().setMinimumHeight(max(height, floor))

    def _render(self):
        elide_into(self.title, self._title_text)
        elide_into(self.detail, self._detail_text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()


class TitleBar(QFrame):
    drag_started = Signal(QPoint)
    drag_moved = Signal(QPoint)
    snap_right = Signal()
    toggle_top = Signal()
    toggle_compact = Signal()
    toggle_help = Signal()
    toggle_demo = Signal()
    toggle_rec = Signal()
    open_settings = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("titleBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        self.title = QLabel("Taisync Telemetry")
        self.title.setObjectName("appTitle")
        layout.addWidget(self.title)
        layout.addItem(QSpacerItem(10, 10, QSizePolicy.Expanding, QSizePolicy.Minimum))

        self.fpv_label = QLabel("FPV")
        self.fpv_label.setObjectName("rcLabel")
        layout.addWidget(self.fpv_label)

        self.fpv_dot = QFrame()
        self.fpv_dot.setObjectName("fpvDot")
        self.fpv_dot.setFixedSize(12, 12)
        layout.addWidget(self.fpv_dot)

        self.rc_label = QLabel("RC")
        self.rc_label.setObjectName("rcLabel")
        layout.addWidget(self.rc_label)

        self.rc_dot = QFrame()
        self.rc_dot.setObjectName("rcDot")
        self.rc_dot.setFixedSize(12, 12)
        layout.addWidget(self.rc_dot)

        self.expand_btn = QPushButton("COMPACT")
        self.settings_btn = QPushButton("SET")
        self.demo_btn = QPushButton("TEST")
        # Logging used to be reachable only through the R hotkey, with its
        # REC indicator created and immediately hidden -- so in practice the
        # app had no visible logging at all.
        self.rec_btn = QPushButton("REC")
        # HELP used to be created and then immediately hidden, so the hotkey
        # list documented in it was unreachable.
        self.help_btn = QPushButton("HELP")
        self.pin_btn = QPushButton("TOP")
        self.snap_btn = QPushButton(">")
        self.close_btn = QPushButton("X")

        self.buttons = [self.rec_btn, self.expand_btn, self.settings_btn, self.demo_btn, self.help_btn, self.pin_btn, self.snap_btn, self.close_btn]
        for btn in self.buttons:
            btn.setObjectName("topBtn")
            btn.setFixedHeight(28)
            layout.addWidget(btn)
        # Short labels for a narrow window, where eight word-labelled buttons
        # got squeezed down to unreadable fragments ("RE", "MP.", "SE1").
        self._narrow = False
        self.set_label(self.settings_btn, "SET", "⚙")
        self.set_label(self.help_btn, "HELP", "?")

        self.expand_btn.setToolTip("Toggle compact overlay")
        self.settings_btn.setToolTip("Ground / Air / RC IP settings")
        self.demo_btn.setToolTip("Run simulated telemetry, no hardware needed (D)")
        self.help_btn.setToolTip("Hotkeys")
        self.pin_btn.setToolTip("Always on top (T)")
        self.snap_btn.setToolTip("Snap to right edge")
        self.close_btn.setToolTip("Close")

        self.expand_btn.clicked.connect(self.toggle_compact.emit)
        self.settings_btn.clicked.connect(self.open_settings.emit)
        self.demo_btn.clicked.connect(self.toggle_demo.emit)
        self.rec_btn.clicked.connect(self.toggle_rec.emit)
        self.help_btn.clicked.connect(self.toggle_help.emit)
        self.pin_btn.clicked.connect(self.toggle_top.emit)
        self.snap_btn.clicked.connect(self.snap_right.emit)

        self._fpv_state: Any = "unset"
        self._rc_state: Any = "unset"
        self.set_fpv_online(None)
        self.set_rc_online(None)

    def set_label(self, btn: QPushButton, full: str, short: Optional[str] = None):
        btn._full_text = full
        btn._short_text = short or full
        btn.setText(btn._short_text if self._narrow else full)

    def set_narrow(self, narrow: bool):
        if narrow == self._narrow:
            return
        self._narrow = narrow
        for btn in self.buttons:
            full = getattr(btn, "_full_text", btn.text())
            btn.setText(getattr(btn, "_short_text", full) if narrow else full)
        self.title.setVisible(not narrow)

    @staticmethod
    def _dot_style(online: Optional[bool]) -> str:
        color = "#7a7f87" if online is None else "#2ecc71" if online else "#ff5c5c"
        return f"background:{color}; border:1px solid #32414d; border-radius:6px;"

    def set_fpv_online(self, online: Optional[bool]):
        if online == self._fpv_state:
            return
        self._fpv_state = online
        self.fpv_dot.setStyleSheet(self._dot_style(online))

    def set_rc_online(self, online: Optional[bool]):
        if online == self._rc_state:
            return
        self._rc_state = online
        self.rc_dot.setStyleSheet(self._dot_style(online))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_started.emit(event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self.drag_moved.emit(event.globalPosition().toPoint())
        super().mouseMoveEvent(event)


class TrafficChart(QWidget):
    """Downlink (left axis) and uplink (right axis) over the last few minutes.

    Both axes auto-scale independently. The old fixed 0..3000 kb/s range put
    the real link's 215-604 kb/s downlink in the bottom fifth of the plot and
    its ~1 kb/s uplink flat on the axis, and drew a "3 Mb/s limit" line that
    had nothing to do with the actual channel. Capacity now comes from the MCS
    the unit reports and is shown as utilisation in the stats line.
    """

    DOWN_FLOOR_KBS = 50.0
    UP_FLOOR_KBS = 5.0

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.max_samples = MAX_SAMPLES
        self.uplink_history = deque(maxlen=MAX_SAMPLES)
        self.downlink_history = deque(maxlen=MAX_SAMPLES)
        self.capacity_mbps: Optional[float] = None
        self.mcs_mode = ""
        self._dirty = False
        self._x_cache: list = []
        self._down_top: Optional[float] = None
        self._up_top: Optional[float] = None
        self._painted_stats: Optional[str] = None

        self.stats = QLabel("Waiting for traffic...")
        self.stats.setObjectName("chartStats")
        self.stats.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout.addWidget(self.stats)

        if pg is None:
            placeholder = QLabel("pyqtgraph не встановлено — графік недоступний")
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setStyleSheet("color: #ff5c5c; font-size: 14px;")
            layout.addWidget(placeholder)
            self.plot_widget = None
            return

        pg.setConfigOption("background", "#101418")
        pg.setConfigOption("foreground", "#eaf2f8")

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget.setLabel("left", "Downlink (kb/s)", color="#5bc8ff")
        self.plot_widget.setLabel("bottom", "seconds (0 = now)")
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.hideButtons()
        plot_item = self.plot_widget.getPlotItem()
        plot_item.vb.disableAutoRange()

        legend = self.plot_widget.addLegend(offset=(10, 10))

        self.downlink_curve = self.plot_widget.plot(
            [], [], pen=pg.mkPen("#5bc8ff", width=2), name="Downlink"
        )

        self.right_axis = pg.ViewBox()
        self.right_axis.disableAutoRange()
        self.right_axis.setMouseEnabled(x=False, y=False)
        self.plot_widget.scene().addItem(self.right_axis)
        self.plot_widget.getAxis("right").linkToView(self.right_axis)
        self.right_axis.setXLink(plot_item.vb)
        self.plot_widget.showAxis("right")
        self.plot_widget.getAxis("right").setLabel("Uplink (kb/s)", color="#f1c40f")

        self.uplink_curve = pg.PlotDataItem(
            [], [], pen=pg.mkPen("#f1c40f", width=2), name="Uplink"
        )
        self.right_axis.addItem(self.uplink_curve)
        legend.addItem(self.uplink_curve, "Uplink")

        plot_item.vb.sigResized.connect(self._sync_right_axis)
        self._apply_ranges(self.DOWN_FLOOR_KBS, self.UP_FLOOR_KBS)
        self._apply_x_range()

        layout.addWidget(self.plot_widget, 1)

    def _sync_right_axis(self):
        if self.plot_widget is None:
            return
        self.right_axis.setGeometry(self.plot_widget.getPlotItem().vb.sceneBoundingRect())

    def set_link(self, mcs_mode: str, capacity_mbps: Optional[float]):
        self.mcs_mode = mcs_mode or ""
        self.capacity_mbps = capacity_mbps

    def add_sample(self, uplink_kbs, downlink_kbs):
        self.uplink_history.append(uplink_kbs or 0)
        self.downlink_history.append(downlink_kbs or 0)
        self._dirty = True
        # Only push into pyqtgraph when the Traffic tab is actually on screen.
        # Samples keep accumulating either way and are flushed on showEvent.
        if self.isVisible():
            self._flush()

    @staticmethod
    def _next_top(current: Optional[float], peak: float, floor: float) -> float:
        """Axis top with headroom; grows at once, shrinks only when far too big."""
        wanted = nice_ceiling(max(floor, peak * 1.2))
        if current is None or wanted > current or wanted * 2.5 < current:
            return wanted
        return current

    def _apply_ranges(self, down_top: float, up_top: float):
        if self.plot_widget is None:
            return
        if down_top != self._down_top:
            self._down_top = down_top
            self.plot_widget.getPlotItem().vb.setYRange(0, down_top, padding=0)
        if up_top != self._up_top:
            self._up_top = up_top
            self.right_axis.setYRange(0, up_top, padding=0)

    def _apply_x_range(self):
        span = (self.max_samples - 1) * POLL_MS / 1000.0
        self.plot_widget.getPlotItem().vb.setXRange(-span, 0, padding=0)

    def _stats_text(self) -> str:
        if not self.downlink_history:
            return "Waiting for traffic..."
        down_now = self.downlink_history[-1]
        up_now = self.uplink_history[-1]
        down_peak = max(self.downlink_history)
        text = f"Downlink {down_now:.0f} kb/s (peak {down_peak:.0f})   Uplink {up_now:.0f} kb/s"
        if self.capacity_mbps:
            used = 100.0 * down_now / (self.capacity_mbps * 1000.0)
            text += f"   |   {self.mcs_mode} capacity {self.capacity_mbps:g} Mb/s, {used:.0f}% used"
        return text

    def _flush(self):
        stats = self._stats_text()
        if stats != self._painted_stats:
            self._painted_stats = stats
            elide_into(self.stats, stats)
        if self.plot_widget is None or not self._dirty:
            return
        self._dirty = False
        count = len(self.uplink_history)
        if count == 0:
            # Tab opened before the first sample (e.g. the unit is unreachable).
            return
        if len(self._x_cache) != count:
            step = POLL_MS / 1000.0
            self._x_cache = [-(count - 1 - i) * step for i in range(count)]
        self.downlink_curve.setData(self._x_cache, list(self.downlink_history))
        self.uplink_curve.setData(self._x_cache, list(self.uplink_history))
        self._apply_ranges(
            self._next_top(self._down_top, max(self.downlink_history), self.DOWN_FLOOR_KBS),
            self._next_top(self._up_top, max(self.uplink_history), self.UP_FLOOR_KBS),
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._painted_stats:
            elide_into(self.stats, self._painted_stats)

    def showEvent(self, event):
        super().showEvent(event)
        self._dirty = True
        self._flush()
        # The linked right-hand ViewBox has no valid geometry until the plot has
        # been laid out at least once.
        QTimer.singleShot(0, self._sync_right_axis)


class MainWindow(QMainWindow):
    # Emitted from the GUI thread, delivered to the worker threads as queued
    # invocations. Calling worker methods directly (as the old code did) ran
    # blocking pings on the GUI thread and froze the window for seconds.
    api_url_changed = Signal(str)
    hosts_changed = Signal(dict)
    ping_requested = Signal()

    def __init__(self, persist: bool = True):
        super().__init__()
        # persist=False (the --selftest run) leaves the user's settings, window
        # position and logs folder untouched.
        self.persist = persist
        self.scale_factor = 1.0
        self.fit_factor = 1.0
        self.drag_anchor = None
        self.drag_window_pos = None
        self.compact_mode = False
        self.topmost = True
        self.position_locked = False
        self.last_banner_text = None
        self.last_banner_color = None
        self.logger = CsvLogger()
        self.rec_enabled = False
        self.auto_log = True
        self.alarm_cfg: Dict[str, Any] = dict(DEFAULT_ALARM)
        self.log_retention_days = 0  # 0 = never delete old logs
        self.sound = SoundPlayer()
        self.alarm = AlarmMonitor()
        self.muted = False
        self._last_warnings: list = []
        self._saved_window: Optional[dict] = None
        # Size of the full window, kept while the compact overlay is shown so
        # EXPAND returns to it instead of a 500 px wide sliver.
        self._full_size = QSize(1280, 900)
        self._resize_mode = None
        self._resize_start_geo = None
        self._resize_start_pos = None
        self._cards = []
        self._single_column: Optional[bool] = None
        self._status_stacked: Optional[bool] = None
        self._inline_cards: Optional[bool] = None
        self._painted_style_factor: Optional[float] = None
        self._painted_footer: Optional[str] = None
        self._painted_warn: Optional[str] = None
        self._painted_raw: Optional[str] = None
        self._painted_distance_px: Optional[int] = None
        self._quick_host_key: Any = None
        self._quick_metric_key: Any = None
        self._error_streak = 0
        self.demo_mode = False
        self.demo_source = DemoSource()
        # Deltas of the unit's since-power-on counters (LDPC, overflow, ...).
        self.counters = CounterWindow()
        self._last_poll_t: Optional[float] = None
        self._poll_dt: Optional[float] = None
        self.help_dialog = None
        self.last_data = None
        self.last_state: Optional[TelemetryState] = None
        self.last_air_latency = None
        self.last_ground_latency = None
        self.last_rc_latency = None

        self.ground_host = DEFAULT_GROUND_HOST
        self.air_host = DEFAULT_AIR_HOST
        self.rc_host = DEFAULT_RC_HOST
        self.rc_online = None
        self.fpv_online = None

        self.load_settings()

        self.setWindowTitle("Taisync Telemetry")
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1280, 900)
        self.setMinimumSize(MIN_W, MIN_H)
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setMouseTracking(True)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        self.title_bar = TitleBar()
        root_layout.addWidget(self.title_bar)

        self.status_row = QFrame()
        self.status_row.setObjectName("statusRow")
        status_layout = QGridLayout(self.status_row)
        status_layout.setContentsMargins(10, 8, 10, 8)
        status_layout.setHorizontalSpacing(10)
        status_layout.setVerticalSpacing(0)
        self.link_banner = QLabel("NO DATA")
        self.link_banner.setObjectName("linkBanner")
        self.link_banner.setAlignment(Qt.AlignCenter)
        self.distance_card = MetricCard("DISTANCE", big=True)
        self.distance_card.setMinimumHeight(0)
        self.distance_card.setMaximumHeight(86)
        self.distance_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        status_layout.addWidget(self.link_banner, 0, 0)
        status_layout.addWidget(self.distance_card, 0, 1)
        status_layout.setColumnStretch(0, 1)
        status_layout.setColumnStretch(1, 1)
        root_layout.addWidget(self.status_row)

        self.quick_panel = QFrame()
        self.quick_panel.setObjectName("quickPanel")
        quick_layout = QGridLayout(self.quick_panel)
        quick_layout.setContentsMargins(5, 5, 5, 5)
        quick_layout.setHorizontalSpacing(4)
        quick_layout.setVerticalSpacing(4)
        self.quick_air_signal = QLabel("AIR\n-- / --")
        self.quick_air_quality = QLabel("A Q\n--")
        self.quick_ground_signal = QLabel("GND\n-- / --")
        self.quick_ground_quality = QLabel("G Q\n--")
        self.quick_fpv = QLabel("FPV")
        self.quick_fpv.setObjectName("quickRcLabel")
        self.quick_fpv.setAlignment(Qt.AlignCenter)
        self.quick_fpv_dot = QFrame()
        self.quick_fpv_dot.setObjectName("quickFpvDot")
        self.quick_fpv_dot.setFixedSize(16, 16)
        self.quick_rc = QLabel("RC")
        self.quick_rc.setObjectName("quickRcLabel")
        self.quick_rc.setAlignment(Qt.AlignCenter)
        self.quick_rc_dot = QFrame()
        self.quick_rc_dot.setObjectName("quickRcDot")
        self.quick_rc_dot.setFixedSize(16, 16)
        self.quick_expand = QPushButton("⤢")
        self.quick_expand.setObjectName("quickExpand")
        self.quick_expand.setToolTip("Open full telemetry")
        self.quick_panel.setMouseTracking(True)
        for metric in (self.quick_air_signal, self.quick_air_quality, self.quick_ground_signal, self.quick_ground_quality):
            metric.setObjectName("quickMetric")
            metric.setAlignment(Qt.AlignCenter)
            metric.setMinimumSize(52, 42)
            metric.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            metric.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # Signal cells carry both antennas and the TX power, so they get the room.
        for metric in (self.quick_air_signal, self.quick_ground_signal):
            metric.setMinimumSize(92, 42)
        self.quick_fpv.setMinimumSize(38, 42)
        self.quick_fpv.setStyleSheet("color: #eaf2f8; font-size: 11px; font-weight: 800;")
        self.quick_rc.setMinimumSize(32, 42)
        self.quick_rc.setStyleSheet("color: #eaf2f8; font-size: 11px; font-weight: 800;")
        self.quick_expand.setMinimumSize(46, 46)
        self.quick_expand.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.quick_expand.setStyleSheet('font-size: 20px; font-weight: 900;')
        quick_layout.addWidget(self.quick_air_signal, 0, 0)
        quick_layout.addWidget(self.quick_air_quality, 0, 1)
        quick_layout.addWidget(self.quick_ground_signal, 0, 2)
        quick_layout.addWidget(self.quick_ground_quality, 0, 3)
        quick_layout.addWidget(self.quick_fpv, 0, 4)
        quick_layout.addWidget(self.quick_fpv_dot, 0, 5, alignment=Qt.AlignCenter)
        quick_layout.addWidget(self.quick_rc, 0, 6)
        quick_layout.addWidget(self.quick_rc_dot, 0, 7, alignment=Qt.AlignCenter)
        quick_layout.addWidget(self.quick_expand, 0, 8)
        root_layout.addWidget(self.quick_panel)
        self.quick_expand.clicked.connect(self.toggle_compact_view)
        self.quick_panel.mousePressEvent = self._quick_panel_mouse_press
        self.quick_panel.mouseMoveEvent = self._quick_panel_mouse_move
        self.quick_panel.mouseReleaseEvent = self._quick_panel_mouse_release

        self.telemetry_scroll = QScrollArea()
        self.telemetry_scroll.setWidgetResizable(True)
        self.telemetry_scroll.setFrameShape(QFrame.NoFrame)
        self.telemetry_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.telemetry_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.telemetry_panel = QWidget()
        self.telemetry_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        telemetry_layout = QVBoxLayout(self.telemetry_panel)
        telemetry_layout.setContentsMargins(0, 0, 0, 0)
        telemetry_layout.setSpacing(10)
        self.telemetry_scroll.setWidget(self.telemetry_panel)

        self.cards_grid = QGridLayout()
        grid = self.cards_grid
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        # Row layout (two columns, air left / ground right):
        #   signal (RF1 + RF2 bars, TX power)  |  signal
        #   quality (SNR + FEC error rate)     |  quality
        #   link (MCS, freq, throughput)       |  RC ping latency
        #   temperature                        |  temperature
        #   ping latency                       |  ping latency
        # The old ANTENNA cards are gone: which chain is active is now marked
        # directly on the antenna bars. The CONTROL card is gone too: its BIND
        # value only repeated what TRACK in the banner already says, and its
        # SBUS/PPM frame rates are meaningless here -- RC goes over UART as CRSF.
        self.air_signal_card = SignalCard("AIR SIGNAL")
        self.ground_signal_card = SignalCard("GROUND SIGNAL")
        self.air_quality_card = MetricCard("AIR QUALITY")
        self.ground_quality_card = MetricCard("GROUND QUALITY")
        self.link_card = MetricCard("LINK")
        self.rc_latency_card = MetricCard("RC LAT")
        self.air_temp_card = MetricCard("AIR TEMP")
        self.ground_temp_card = MetricCard("GROUND TEMP")
        self.air_latency_card = MetricCard("AIR LAT")
        self.ground_latency_card = MetricCard("GND LAT")
        cards = [
            self.air_signal_card,
            self.ground_signal_card,
            self.air_quality_card,
            self.ground_quality_card,
            self.link_card,
            self.rc_latency_card,
            self.air_temp_card,
            self.ground_temp_card,
            self.air_latency_card,
            self.ground_latency_card,
        ]
        self._cards = cards
        positions = [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1), (4, 0), (4, 1)]
        for c, pos in zip(cards, positions):
            c.setMinimumHeight(0)
            c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            grid.addWidget(c, *pos)
        for row in range(5):
            grid.setRowStretch(row, 1)
        self.view_tabs = QTabWidget()
        self.view_tabs.setObjectName("viewTabs")
        telemetry_layout.addWidget(self.view_tabs)

        telemetry_tab = QWidget()
        telemetry_tab_layout = QVBoxLayout(telemetry_tab)
        telemetry_tab_layout.setContentsMargins(0, 0, 0, 0)
        telemetry_tab_layout.setSpacing(8)
        telemetry_tab_layout.addLayout(grid)

        self.footer = QLabel(f"API: {self.api_url()}")
        self.footer.setObjectName("footer")
        telemetry_tab_layout.addWidget(self.footer)

        self.warn_bar = QFrame()
        self.warn_bar.setObjectName("warnBar")
        warn_layout = QHBoxLayout(self.warn_bar)
        warn_layout.setContentsMargins(10, 6, 10, 6)
        warn_layout.setSpacing(8)
        self.warn_label = QLabel("WARN: --")
        self.warn_label.setObjectName("warnLabel")
        self.warn_label.setWordWrap(True)
        warn_layout.addWidget(self.warn_label, 1)
        telemetry_tab_layout.addWidget(self.warn_bar)
        self.warn_bar.hide()

        raw_tab = QWidget()
        raw_tab_layout = QVBoxLayout(raw_tab)
        raw_tab_layout.setContentsMargins(0, 0, 0, 0)
        raw_tab_layout.setSpacing(8)
        self.raw_data_view = QPlainTextEdit()
        self.raw_data_view.setPlainText("Waiting for telemetry...")
        self.raw_data_view.setObjectName("rawDataView")
        self.raw_data_view.setReadOnly(True)
        self.raw_data_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        raw_tab_layout.addWidget(self.raw_data_view)

        traffic_tab = QWidget()
        traffic_tab_layout = QVBoxLayout(traffic_tab)
        traffic_tab_layout.setContentsMargins(0, 0, 0, 0)
        traffic_tab_layout.setSpacing(8)
        self.traffic_chart = TrafficChart()
        traffic_tab_layout.addWidget(self.traffic_chart)

        self.view_tabs.addTab(telemetry_tab, "Telemetry")
        self.view_tabs.addTab(traffic_tab, "Traffic")
        self.view_tabs.addTab(raw_tab, "Raw Data")
        self.logs_tab = LogsTab(lambda: self.logger.path if self.logger.enabled else None)
        self.view_tabs.addTab(self.logs_tab, "Logs")
        self.view_tabs.currentChanged.connect(self._on_view_tab_changed)

        # The scroll area used to sit inside a single-child QSplitter, which
        # bought nothing and cost a 12px handle plus an extra layout level.
        root_layout.addWidget(self.telemetry_scroll, 1)

        self.title_bar.close_btn.clicked.connect(self.close)
        self.title_bar.drag_started.connect(self.on_drag_started)
        self.title_bar.drag_moved.connect(self.on_drag_moved)
        self.title_bar.snap_right.connect(self.snap_right)
        self.title_bar.toggle_top.connect(self.toggle_topmost)
        self.title_bar.toggle_compact.connect(self.toggle_compact_view)
        self.title_bar.toggle_help.connect(self.show_help)
        self.title_bar.toggle_demo.connect(self.toggle_demo)
        self.title_bar.toggle_rec.connect(self.toggle_recording)
        self.title_bar.open_settings.connect(self.show_settings)

        # Drives the synthetic feed at the same cadence as the real poller.
        self.demo_timer = QTimer(self)
        self.demo_timer.setInterval(POLL_MS)
        self.demo_timer.timeout.connect(self._demo_tick)

        QShortcut(QKeySequence("T"), self, activated=self.toggle_topmost)
        QShortcut(QKeySequence("L"), self, activated=self.toggle_lock)
        QShortcut(QKeySequence("R"), self, activated=self.toggle_recording)
        QShortcut(QKeySequence("D"), self, activated=self.toggle_demo)
        QShortcut(QKeySequence("M"), self, activated=self.toggle_mute)
        QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self.adjust_scale(0.08))
        QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: self.adjust_scale(0.08))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self.adjust_scale(-0.08))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self.reset_scale)

        # Coalesces resize work: a drag fires hundreds of resizeEvents, and the
        # relayout + restyle pass behind them is far too heavy to run per pixel.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(RESIZE_DEBOUNCE_MS)
        self._resize_timer.timeout.connect(self._apply_responsive_layout)

        self.apply_styles()
        self._restore_window_state()
        self._update_pin_label()
        if self.auto_log and self.persist:
            self.set_recording(True)
        # Old-log cleanup (off unless enabled in SET -> Логи): at start-up and
        # every few hours for an overlay left running for days.
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.setInterval(6 * 3600 * 1000)
        self._cleanup_timer.timeout.connect(self.cleanup_logs)
        if self.persist:
            self.cleanup_logs()
            self._cleanup_timer.start()
        self.update_rec_indicator()
        self._update_demo_indicator()
        self.refresh_connection_labels()
        QTimer.singleShot(0, self.apply_compact_view)

        self.thread = QThread(self)
        self.worker = PollWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.data_ready.connect(self.on_data)
        self.worker.error_ready.connect(self.on_error)
        self.api_url_changed.connect(self.worker.set_api_url)
        self.worker.set_api_url(self.api_url())  # safe: thread not started yet
        if NETWORK_ENABLED:
            self.thread.start()

        self.net_thread = QThread(self)
        self.net_worker = NetworkWorker()
        self.net_worker.moveToThread(self.net_thread)
        self.net_thread.started.connect(self.net_worker.start)
        self.net_worker.results_ready.connect(self.on_net_results)
        self.hosts_changed.connect(self.net_worker.set_hosts)
        self.ping_requested.connect(self.net_worker.poll)
        self.net_worker.set_hosts(self._host_map())  # safe: thread not started yet
        if NETWORK_ENABLED:
            self.net_thread.start()

        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(20, 20)
        self._size_grip.setToolTip("Resize window")
        self._position_size_grip()

    def api_url(self) -> str:
        return f"http://{self.ground_host}/v2/baseband.json"

    def _host_map(self) -> Dict[str, str]:
        return {"air": self.air_host, "ground": self.ground_host, "rc": self.rc_host}

    def load_settings(self):
        try:
            if SETTINGS_PATH.exists():
                raw = SETTINGS_PATH.read_text(encoding="utf-8-sig").strip()
                if raw:
                    data = json.loads(raw)
                    self.ground_host = str(data.get("ground_host", self.ground_host)).strip() or self.ground_host
                    self.air_host = str(data.get("air_host", self.air_host)).strip() or self.air_host
                    # Before 1.40 the RC IP defaulted to empty, so saved files
                    # carry "" -- treat that as "use the default" too.
                    self.rc_host = str(data.get("rc_host", self.rc_host)).strip() or self.rc_host
                    self.auto_log = bool(data.get("auto_log", self.auto_log))
                    set_thresholds(data.get("thresholds"))
                    alarm = data.get("alarm")
                    if isinstance(alarm, dict):
                        self.alarm_cfg = {**DEFAULT_ALARM, **{k: v for k, v in alarm.items() if k in DEFAULT_ALARM}}
                    self.log_retention_days = max(0, safe_int(data.get("log_retention_days", 0)))
                    window = data.get("window")
                    self._saved_window = window if isinstance(window, dict) else None
        except Exception:
            pass

    def save_settings(self):
        if not self.persist:
            return
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ground_host": self.ground_host,
                "air_host": self.air_host,
                "rc_host": self.rc_host,
                "auto_log": self.auto_log,
                "log_retention_days": self.log_retention_days,
                "alarm": self.alarm_cfg,
                "thresholds": dict(THRESHOLDS),
                "window": self._window_state(),
            }
            SETTINGS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._settings_error = ""
        except Exception as e:
            self._settings_error = str(e)
        self.update_footer()

    def update_footer(self):
        """Footer line, rebuilt only when it actually changes."""
        error = getattr(self, "_settings_error", "")
        if error:
            text = f"API: {self.api_url()}  |  settings: ERROR"
            tip = f"{SETTINGS_PATH}\n{error}"
        else:
            saved = SETTINGS_PATH.exists()
            text = f"API: {self.api_url()}  |  settings: {'saved' if saved else 'defaults'}"
            tip = str(SETTINGS_PATH)
        if text != self._painted_footer:
            self._painted_footer = text
            self.footer.setText(text)
            self.footer.setToolTip(tip)

    def closeEvent(self, event):
        self.save_settings()
        self.logger.stop()
        workers = (getattr(self, "worker", None), getattr(self, "net_worker", None))
        threads = (getattr(self, "thread", None), getattr(self, "net_thread", None))
        for w in workers:
            if w is not None:
                try:
                    w.request_stop()
                except Exception:
                    pass
        # Ask every thread to quit before waiting on any of them, so shutdown is
        # parallel rather than serialised. A ping in flight can take ~1.7s.
        for th in threads:
            try:
                if th is not None:
                    th.quit()
            except Exception:
                pass
        for th in threads:
            try:
                if th is not None:
                    th.wait(3000)
            except Exception:
                pass
        super().closeEvent(event)

    def toggle_compact_view(self):
        self.compact_mode = not self.compact_mode
        self.apply_compact_view()

    def apply_compact_view(self):
        compact = self.compact_mode
        self.quick_panel.setVisible(compact)
        self.title_bar.setVisible(True)
        self.title_bar.setMaximumHeight(0 if compact else 16777215)
        self.status_row.setVisible(not compact)
        self.telemetry_scroll.setVisible(not compact)
        self.title_bar.expand_btn.setVisible(not compact)
        self.title_bar.settings_btn.setVisible(not compact)
        self.title_bar.demo_btn.setVisible(not compact)
        self.title_bar.help_btn.setVisible(not compact)
        self.title_bar.set_label(self.title_bar.expand_btn, "EXPAND" if compact else "COMPACT", "▭")
        self.title_bar.pin_btn.setVisible(not compact)
        self.title_bar.snap_btn.setVisible(not compact)
        if compact:
            if self.height() > COMPACT_H:
                self._full_size = self.size()
            root = self.centralWidget().layout()
            if root:
                root.setContentsMargins(0, 0, 0, 0)
                root.setSpacing(0)
            self.quick_panel.setFixedSize(COMPACT_W, COMPACT_H)
            self.quick_panel.setStyleSheet("background: transparent; border: 0;")
            self.centralWidget().setStyleSheet("background: transparent;")
            self.centralWidget().setFixedSize(COMPACT_W, COMPACT_H)
            self.setFixedSize(COMPACT_W, COMPACT_H)
        else:
            # Read before the minimum size below grows the window on its own.
            was_overlay = self.height() <= COMPACT_H
            self.quick_panel.setStyleSheet("")
            self.centralWidget().setStyleSheet("")
            root = self.centralWidget().layout()
            if root:
                root.setContentsMargins(8, 8, 8, 8)
                root.setSpacing(8)
            self.quick_panel.setMinimumSize(0, 0)
            self.quick_panel.setMaximumSize(16777215, 16777215)
            self.centralWidget().setMinimumSize(0, 0)
            self.centralWidget().setMaximumSize(16777215, 16777215)
            self.setMinimumSize(MIN_W, MIN_H)
            self.setMaximumSize(16777215, 16777215)
            if was_overlay:
                self.resize(self._full_size)
                # Growing from the overlay can push the window off the bottom
                # or right edge of the monitor it is on.
                self._move_within_screen(self.pos())
            # Force the responsive modes to be recomputed from scratch.
            self._single_column = None
            self._status_stacked = None
            self._inline_cards = None
            self._apply_responsive_layout()
        if self.last_state is not None:
            self.update_compact_metrics(self.last_state)

    def refresh_connection_labels(self):
        self.update_footer()
        self.title_bar.set_fpv_online(self.fpv_online)
        self.title_bar.set_rc_online(self.rc_online)
        self._update_quick_host_indicators()
        self.update_warnings()

    def _update_quick_host_indicators(self):
        key = (self.fpv_online, self.rc_online, self.air_host, self.rc_host)
        if key == self._quick_host_key:
            return
        self._quick_host_key = key
        fpv_color = "#7a7f87" if self.fpv_online is None else "#2ecc71" if self.fpv_online else "#ff5c5c"
        rc_color = "#7a7f87" if self.rc_online is None else "#2ecc71" if self.rc_online else "#ff5c5c"
        self.quick_fpv_dot.setStyleSheet(f"background:{fpv_color}; border:1px solid #32414d; border-radius:8px;")
        self.quick_rc_dot.setStyleSheet(f"background:{rc_color}; border:1px solid #32414d; border-radius:8px;")
        fpv_tip = "FPV IP not set" if not self.air_host else f"FPV: {self.air_host}"
        rc_tip = "RC IP not set" if not self.rc_host else f"RC: {self.rc_host}"
        self.quick_fpv_dot.setToolTip(fpv_tip)
        self.quick_fpv.setToolTip(fpv_tip)
        self.quick_rc_dot.setToolTip(rc_tip)
        self.quick_rc.setToolTip(rc_tip)

    def show_settings(self):
        dlg = SettingsDialog(self.ground_host, self.air_host, self.rc_host, self.auto_log, self,
                             thresholds=THRESHOLDS, alarm=self.alarm_cfg,
                             log_retention_days=self.log_retention_days)
        # The test button plays even when muted: that's what it is for.
        dlg.test_sound.connect(lambda: self.sound.play("alarm"))
        if dlg.exec() != QDialog.Accepted:
            return
        values = dlg.values()
        self.ground_host = values["ground_host"]
        self.air_host = values["air_host"]
        self.rc_host = values["rc_host"]
        self.auto_log = values["auto_log"]
        self.alarm_cfg = {**DEFAULT_ALARM, **values["alarm"]}
        self.log_retention_days = values["log_retention_days"]
        set_thresholds(values["thresholds"])
        self._apply_threshold_change()
        self.save_settings()
        self.cleanup_logs()
        self.rc_online = None
        self.last_air_latency = None
        self.last_ground_latency = None
        self.last_rc_latency = None
        # Queued into the worker threads; never call worker methods directly
        # from here -- a ping takes up to ~1.7s and would freeze the window.
        self.api_url_changed.emit(self.api_url())
        self.hosts_changed.emit(self._host_map())
        self.ping_requested.emit()
        self._error_streak = 0
        self.refresh_connection_labels()
        self.update_latency_cards()
        if self.last_data is not None:
            self.on_data(self.last_data)

    def on_net_results(self, result: dict):
        if self.demo_mode:
            return
        latency = result.get("latency", {})
        hosts = result.get("hosts", {})
        self.last_air_latency = latency.get("air")
        self.last_ground_latency = latency.get("ground")
        self.last_rc_latency = latency.get("rc")
        self.rc_online = None if not hosts.get("rc") else latency.get("rc") is not None
        self.title_bar.set_rc_online(self.rc_online)
        self._update_quick_host_indicators()
        self.update_latency_cards()
        self.update_warnings()

    def update_latency_cards(self):
        if hasattr(self, "air_latency_card"):
            air = self.last_air_latency
            self.air_latency_card.update_data(
                '--' if air is None else f'{air} ms',
                f'ping {self.air_host or "N/A"}',
                latency_color(air),
            )
        if hasattr(self, "ground_latency_card"):
            ground = self.last_ground_latency
            self.ground_latency_card.update_data(
                '--' if ground is None else f'{ground} ms',
                f'ping {self.ground_host or "N/A"}',
                latency_color(ground),
            )
        if hasattr(self, "rc_latency_card"):
            rc = self.last_rc_latency
            if not self.rc_host:
                value, detail = '--', 'RC IP not set (SET)'
            else:
                value = '--' if rc is None else f'{rc} ms'
                detail = f'ping {self.rc_host}' + (' · no reply' if self.rc_online is False else '')
            self.rc_latency_card.update_data(value, detail, latency_color(rc))

    def update_banner(self, text: str, color: str):
        """The banner shows link state only.

        It used to be written by two independent code paths (link summary here,
        warnings in `_apply_warning_banner`) that fought over the same label and
        over each other's caches, so it could latch onto stale text and lose its
        box styling. Warnings now live in the warning bar, which is what that
        widget was built for.
        """
        if text == self.last_banner_text and color == self.last_banner_color:
            return
        self.last_banner_text = text
        self.last_banner_color = color
        self.link_banner.setText(text)
        # Box styling comes from the #linkBanner QSS rule; only the colour is
        # per-state, so the two no longer overwrite each other.
        self.link_banner.setStyleSheet(f"color: {color};")

    def update_compact_metrics(self, s: TelemetryState):
        # Only the compact overlay uses these widgets; skip the work entirely
        # when it is not on screen.
        if not self.compact_mode:
            return
        key = (s.air_rssi0, s.air_rssi1, s.air_tx_dbm, s.air_signal_pct, s.air_quality_pct,
               s.ground_rssi0, s.ground_rssi1, s.ground_tx_dbm, s.ground_signal_pct, s.ground_quality_pct)
        if key == self._quick_metric_key:
            return
        self._quick_metric_key = key

        def rf(v):
            return '--' if v is None else f"{v:.0f}"

        def head(side, tx):
            return side if tx is None else f"{side} · TX {tx:g}"

        def pct(v):
            return '--' if v is None else f"{v}%"

        cells = (
            (self.quick_air_signal,
             head('AIR', s.air_tx_dbm) + "\n" + f"{rf(s.air_rssi0)} / {rf(s.air_rssi1)}",
             signal_color(s.air_signal_pct),
             f"AIR  RF1 {rf(s.air_rssi0)} dBm  RF2 {rf(s.air_rssi1)} dBm" + "\n" + format_tx_power(s.air_tx_dbm)),
            (self.quick_air_quality,
             "A Q\n" + pct(s.air_quality_pct),
             metric_color(s.air_quality_pct),
             f"Air quality {pct(s.air_quality_pct)}  SNR {s.air_snr}  FEC errors {format_error_rate(s.air_ldpc_rate)}"),
            (self.quick_ground_signal,
             head('GND', s.ground_tx_dbm) + "\n" + f"{rf(s.ground_rssi0)} / {rf(s.ground_rssi1)}",
             signal_color(s.ground_signal_pct),
             f"GROUND  RF1 {rf(s.ground_rssi0)} dBm  RF2 {rf(s.ground_rssi1)} dBm" + "\n" + format_tx_power(s.ground_tx_dbm)),
            (self.quick_ground_quality,
             "G Q\n" + pct(s.ground_quality_pct),
             metric_color(s.ground_quality_pct),
             f"Ground quality {pct(s.ground_quality_pct)}  SNR {s.ground_snr}  FEC errors {format_error_rate(s.ground_ldpc_rate)}"),
        )
        for widget, text, color, tip in cells:
            widget.setText(text)
            widget.setToolTip(tip)
            widget.setStyleSheet(f"background: #10161c; border: 1px solid {color}; border-radius: 6px; color: {color}; font-size: 10px; font-weight: 800;")

    def build_state(self, data: Dict[str, Any]) -> TelemetryState:
        """Turn one raw payload into display values.

        Field names are the ones the ground unit actually sends (verified
        against a live 55-field capture). Earlier versions also probed a dozen
        guessed aliases -- airQuality, uplinkLQ, groundLinkQualit, groundLdpc,
        A-AD9361Temp, ... -- none of which the hardware ever reports.
        """
        air_rssi0, air_rssi1 = to_dbm(data.get('airRSSI0')), to_dbm(data.get('airRSSI1'))
        ground_rssi0, ground_rssi1 = to_dbm(data.get('groundRSSI0')), to_dbm(data.get('groundRSSI1'))
        air_best = max([v for v in (air_rssi0, air_rssi1) if v is not None], default=None)
        ground_best = max([v for v in (ground_rssi0, ground_rssi1) if v is not None], default=None)
        air_status = safe_text(data.get('airLinkStatus'))
        ground_status = safe_text(data.get('groundLinkStatus'))
        both_track = air_status.lower() == 'track' and ground_status.lower() == 'track'
        # The unit estimates distance from the radio link itself, so 0 m with
        # the aircraft on the bench is a real reading, not a missing GPS fix.
        distance = parse_number(data.get('distance'))
        distance_text = f"{int(round(distance))} m" if both_track and distance is not None else 'N/A'
        air_snr = parse_number(data.get('airSnr'))
        ground_snr = parse_number(data.get('groundSnr'))

        # FEC quality from how the counters moved recently -- symmetric for both
        # sides now. Ground used to skip LDPC entirely, most likely because the
        # lifetime-total bug made it read CRIT.
        air_ldpc_rate = ldpc_error_rate(self.counters.recent('airLdpcFail'), self.counters.recent('airLdpcPass'))
        ground_ldpc_rate = ldpc_error_rate(self.counters.recent('groundLdpcFail'), self.counters.recent('groundLdpcPass'))

        mcs_mode, mcs_mbps = parse_mcs(data.get('MCS'))
        air_ant_mode, air_ant_index = active_antenna(data.get('airCurrentAnt'))
        ground_ant_mode, ground_ant_index = active_antenna(data.get('groundCurrentAnt'))
        freq = parse_number(data.get('groundCurrentTxFreq'))
        if freq is None:
            freq = parse_number(data.get('CurTxFreq'))

        return TelemetryState(
            timestamp=datetime.now().isoformat(timespec='milliseconds'),
            air_status=air_status,
            ground_status=ground_status,
            distance_text=distance_text,
            distance_value=distance,
            air_signal_pct=best_rssi_to_percent(air_best),
            ground_signal_pct=best_rssi_to_percent(ground_best),
            air_quality_pct=combine_quality(air_snr, air_ldpc_rate),
            ground_quality_pct=combine_quality(ground_snr, ground_ldpc_rate),
            air_pure_quality_pct=read_percent(str(data.get('airLinkQuality', '')).replace('%', '')),
            ground_pure_quality_pct=read_percent(str(data.get('groundLinkQuality', '')).replace('%', '')),
            air_best_rssi=air_best,
            ground_best_rssi=ground_best,
            air_snr=air_snr,
            ground_snr=ground_snr,
            air_ldpc_fail=parse_number(data.get('airLdpcFail')),
            ground_ldpc_fail=parse_number(data.get('groundLdpcFail')),
            air_latency_ms=self.last_air_latency,
            ground_latency_ms=self.last_ground_latency,
            air_ant=safe_text(data.get('airCurrentAnt')),
            ground_ant=safe_text(data.get('groundCurrentAnt')),
            mcs=safe_text(data.get('MCS')),
            bitrate=safe_text(data.get('downlinkDataRate')),
            freq=f"{freq:g} MHz" if freq is not None else 'N/A',
            air_tx=safe_text(data.get('airTxPower')),
            ground_tx=safe_text(data.get('groundTxPower')),
            uplink_kbs=parse_kbs(data.get('uplinkDataRate')),
            downlink_kbs=parse_kbs(data.get('downlinkDataRate')),
            link_summary='TRACK' if both_track else f"AIR {air_status} / GND {ground_status}",
            air_rssi0=air_rssi0,
            air_rssi1=air_rssi1,
            ground_rssi0=ground_rssi0,
            ground_rssi1=ground_rssi1,
            air_ant_mode=air_ant_mode,
            ground_ant_mode=ground_ant_mode,
            air_ant_index=air_ant_index,
            ground_ant_index=ground_ant_index,
            air_tx_dbm=parse_number(data.get('airTxPower')),
            ground_tx_dbm=parse_number(data.get('groundTxPower')),
            air_ldpc_rate=air_ldpc_rate,
            ground_ldpc_rate=ground_ldpc_rate,
            mcs_mode=mcs_mode,
            mcs_mbps=mcs_mbps,
            freq_mhz=freq,
            air_temp_c=parse_number(data.get('airAD9361Temp')),
            ground_temp_c=parse_number(data.get('groundAD9361Temp')),
            uav=safe_text(data.get('UAV')),
        )

    def toggle_demo(self):
        """Feed the UI from DemoSource instead of the radio."""
        self.demo_mode = not self.demo_mode
        # Counter deltas across two different sources are meaningless (the
        # simulator's totals start from zero, the unit's are in the millions).
        self.counters.reset()
        self.alarm.reset()
        if self.rec_enabled:
            # Simulated flights go to their own telemetry_demo_*.csv so they
            # can never be mistaken for a real one in the Logs tab.
            self.logger.start(demo=self.demo_mode)
            self.update_rec_indicator()
        if self.demo_mode:
            self.demo_source.reset()
            self.demo_timer.start()
            self._demo_tick()
        else:
            self.demo_timer.stop()
            self._error_streak = 0
            self.last_data = None
            self.last_air_latency = None
            self.last_ground_latency = None
            self.last_rc_latency = None
            self.rc_online = None
            self.fpv_online = None
            self.update_latency_cards()
            self.on_error("waiting for telemetry")
        self._update_demo_indicator()

    def _update_demo_indicator(self):
        # Simulated numbers must never be mistakable for real telemetry, so the
        # button lights up and the window title says so outright.
        btn = self.title_bar.demo_btn
        if self.demo_mode:
            self.title_bar.set_label(btn, "TEST ON", "T✓")
            btn.setStyleSheet(
                "background:#5c4a1d; border:1px solid #ffd24d; border-radius:6px;"
                " color:#ffd24d; padding:3px 7px; font-weight:800;"
            )
        else:
            self.title_bar.set_label(btn, "TEST", "T")
            btn.setStyleSheet("")
        self._refresh_title_text()

    def _refresh_title_text(self):
        title = "Taisync Telemetry"
        self.title_bar.title.setToolTip(f"v{APP_VERSION}")
        if self.demo_mode:
            title += "  —  TEST DATA (simulated)"
        if self.position_locked:
            title += " [LOCK]"
        if self.muted:
            title += " [MUTE]"
        self.title_bar.title.setText(title)

    def _demo_tick(self):
        data = self.demo_source.payload()
        air, ground, rc = self.demo_source.latency()
        self.last_air_latency = air
        self.last_ground_latency = ground
        self.last_rc_latency = rc
        self.rc_online = True
        self.title_bar.set_rc_online(True)
        self.update_latency_cards()
        self._apply_data(data)

    def on_data(self, data: Dict[str, Any]):
        # Real telemetry is ignored while the simulator is driving the UI.
        if self.demo_mode:
            return
        self._apply_data(data)

    def _apply_data(self, data: Dict[str, Any]):
        self.last_data = data
        self._error_streak = 0
        if hasattr(self, "view_tabs") and self.view_tabs.currentIndex() == 2:
            self.update_raw_data_view(data)
        # Counters first: build_state reads their deltas.
        self.counters.update(data, COUNTER_KEYS)
        s = self.build_state(data)
        self.last_state = s
        uav = data.get("UAV")
        self.fpv_online = None if uav is None else str(uav).strip().lower() == "online"
        self.title_bar.set_fpv_online(self.fpv_online)
        self._update_quick_host_indicators()
        self.update_compact_metrics(s)
        banner_status = 'track' if s.link_summary == 'TRACK' else 'init' if 'INIT' in s.link_summary.upper() else 'lost'
        self.update_banner(s.link_summary, status_color(banner_status))
        self.distance_card.update_data(s.distance_text, f"AIR: {s.air_status} | GND: {s.ground_status}", '#ecf0f1')
        self._render_cards(s)
        # Latency cards are owned by on_net_results; repeating them here was
        # duplicate work on a value that had not changed.
        self.update_warnings(data)
        self._evaluate_alarm(s.link_summary == 'TRACK', (s.air_quality_pct, s.ground_quality_pct))
        self.logger.log(self._log_row(s))
        if self.logger.error and self.rec_enabled:
            self.update_rec_indicator()
        if hasattr(self, "traffic_chart"):
            self.traffic_chart.set_link(s.mcs_mode, s.mcs_mbps)
            self.traffic_chart.add_sample(s.uplink_kbs, s.downlink_kbs)

    def _render_cards(self, s: TelemetryState):
        self.air_signal_card.update_data(
            s.air_signal_pct, s.air_rssi0, s.air_rssi1, s.air_ant_index, s.air_ant_mode, s.air_tx_dbm)
        self.ground_signal_card.update_data(
            s.ground_signal_pct, s.ground_rssi0, s.ground_rssi1, s.ground_ant_index, s.ground_ant_mode, s.ground_tx_dbm)

        for card, q, pure, snr, rate in (
            (self.air_quality_card, s.air_quality_pct, s.air_pure_quality_pct, s.air_snr, s.air_ldpc_rate),
            (self.ground_quality_card, s.ground_quality_pct, s.ground_pure_quality_pct, s.ground_snr, s.ground_ldpc_rate),
        ):
            value = f"T {'--' if q is None else f'{q}%'} | P {'--' if pure is None else f'{pure}%'}"
            snr_text = '--' if snr is None else f"{snr:g} dB"
            detail = f"SNR {snr_text} · FEC errors {format_error_rate(rate)}"
            card.update_data(value, detail, metric_color(q), quality_status(q), quality_accent(q))

        capacity = f"{s.mcs_mbps:g} Mb/s" if s.mcs_mbps else "capacity N/A"
        freq = f"{s.freq_mhz:g} MHz" if s.freq_mhz is not None else "freq N/A"
        down = '--' if s.downlink_kbs is None else f"{s.downlink_kbs:.0f}"
        up = '--' if s.uplink_kbs is None else f"{s.uplink_kbs:.0f}"
        self.link_card.update_data(
            s.mcs_mode, f"{capacity} · {freq} · DL {down} / UL {up} kb/s", '#8bd3ff')

        for card, temp in ((self.air_temp_card, s.air_temp_c), (self.ground_temp_card, s.ground_temp_c)):
            card.update_data('--' if temp is None else f'{temp:.0f} °C', 'AD9361 RF transceiver', temp_color(temp))

    def _on_view_tab_changed(self, index: int):
        if index == 2:
            self.update_raw_data_view()

    def update_raw_data_view(self, data: Optional[dict] = None):
        if not hasattr(self, "raw_data_view") or not hasattr(self, "view_tabs"):
            return
        if self.view_tabs.currentIndex() != 2 and data is None:
            return
        data = data if data is not None else (self.last_data or {})
        try:
            text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(data)

        # Compare against a cached copy rather than toPlainText(), which would
        # rebuild the whole document as a string on every poll.
        if text == self._painted_raw:
            return
        self._painted_raw = text
        vbar = self.raw_data_view.verticalScrollBar()
        hbar = self.raw_data_view.horizontalScrollBar()
        vpos = vbar.value()
        hpos = hbar.value()
        self.raw_data_view.setPlainText(text)
        vbar.setValue(vpos)
        hbar.setValue(hpos)

    def update_warnings(self, data: Optional[dict] = None):
        if not hasattr(self, "warn_bar") or not hasattr(self, "warn_label"):
            return
        data = data if data is not None else (self.last_data or {})
        msgs = warning_messages(data, self.last_air_latency, self.last_ground_latency, self.fpv_online, self.rc_online, self.counters)
        self._last_warnings = msgs
        text = " | ".join(msgs) if msgs else ""
        if text == self._painted_warn:
            return
        self._painted_warn = text
        # The warning bar used to be hidden in both branches, so it could never
        # appear no matter what was wrong.
        if msgs:
            self.warn_label.setText("WARN: " + text)
            self.warn_bar.show()
        else:
            self.warn_label.setText("WARN: --")
            self.warn_bar.hide()

    def on_error(self, message: str):
        if self.demo_mode:
            return
        self._error_streak += 1
        # A single dropped poll is normal on a wireless link. Keep the last good
        # reading on screen for a few cycles instead of flashing the whole panel
        # to "--" and back.
        if self._error_streak < ERROR_GRACE_POLLS and self.last_data is not None:
            return
        self.update_banner('NO API', '#ff7b7b')
        self.distance_card.update_data('N/A', message[:80], '#bdc3c7')
        for card in (self.air_signal_card, self.ground_signal_card):
            card.update_data(None, None, None, None, '', None, note='waiting for data')
        self.air_quality_card.update_data('--', 'waiting for data', '#7a7f87', 'N/A', '#182129')
        self.ground_quality_card.update_data('--', 'waiting for data', '#7a7f87', 'N/A', '#182129')
        self.link_card.update_data('--', 'waiting for data', '#7a7f87')
        # The unit may have rebooted while unreachable; start the deltas over.
        self.counters.reset()
        self._evaluate_alarm(False, ())
        self._update_quick_host_indicators()
        # Outages belong in the flight log too: they are often the very event
        # someone opens the log to explain. Only into a log that already has
        # real data, so running with no radio in range creates no files.
        if self.logger.file is not None:
            self.logger.log(self._log_row(None, link="NO API", warnings=message[:120]))

    def show_help(self):
        if self.help_dialog is not None:
            try:
                self.help_dialog.close()
            except Exception:
                pass
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Taisync Telemetry {APP_VERSION} — Help")
        dlg.setWindowFlags(Qt.Window | Qt.Tool)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(8)
        title = QLabel("Гарячі клавіші")
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #eaf2f8;")
        lay.addWidget(title)
        items = [
            ("T", "Topmost on/off"),
            ("L", "Lock/unlock drag"),
            ("R / REC", "Запис логу польоту в CSV on/off"),
            ("D", "Test data on/off (no hardware)"),
            ("M", "Звукові сигнали: тимчасово вимкнути / увімкнути"),
            ("Ctrl + +", "Scale up"),
            ("Ctrl + -", "Scale down"),
            ("Ctrl + 0", "Reset scale"),
            (">", "Snap right"),
            ("COMPACT", "Shrink to the small overlay"),
            ("SET", "Ground/Air/RC IP, автозапис логу"),
        ]
        for k, v in items:
            row = QLabel(f"{k} — {v}")
            row.setStyleSheet("font-size: 14px; color: #d6eaf8;")
            lay.addWidget(row)

        legend_title = QLabel("Як читати картки")
        legend_title.setStyleSheet("font-size: 16px; font-weight: 800; color: #eaf2f8; padding-top: 8px;")
        lay.addWidget(legend_title)
        legend = [
            "SIGNAL — RF1 / RF2: рівень кожної антени в dBm; крапка = активна антена. "
            f"Шкала смуги {THRESHOLDS['rssi_min_dbm']:g}…{THRESHOLDS['rssi_max_dbm']:g} dBm. TX — вихідна потужність модуля.",
            "QUALITY — T: розрахунок програми (гірше з SNR і частки помилок FEC за останні ~6 с). "
            "P: якість лінку, яку повідомляє сам модуль.",
            "LINK — модуляція (MCS), пропускна здатність, частота, поточний трафік.",
            "RC LAT — пінг пульта (IP задається в SET).",
            "Звук: сигнал при втраті TRACK / зв'язку з Ground (тільки після першого TRACK), "
            "і при якості нижче порогу; повтор поки триває. Налаштування — SET → Звук. "
            "Пороги кольорів — SET → Пороги.",
            "Logs — відкриває записаний лог: графік за часом (червоне — лінк не TRACK) "
            "або X ↔ Y (напр. SNR від відстані) з лінією тренду; список подій — втрати лінку, "
            "пропуски даних, нові попередження. Клік по події показує її на графіку.",
            f"Логи лежать у {LOGS_DIR}",
            "Попередження по лічильниках (overflow, discard, FEC) з'являються лише коли "
            "лічильник росте, і гаснуть самі.",
        ]
        for text in legend:
            row = QLabel(text)
            row.setWordWrap(True)
            row.setStyleSheet("font-size: 13px; color: #b9c7d3;")
            lay.addWidget(row)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        lay.addWidget(close_btn)
        dlg.setStyleSheet("background: #101418; color: #eaf2f8;")
        dlg.resize(500, 600)
        dlg.show()
        self.help_dialog = dlg

    def update_rec_indicator(self):
        btn = self.title_bar.rec_btn
        if self.logger.error:
            self.rec_enabled = False
            self.title_bar.set_label(btn, "REC !", "!")
            btn.setStyleSheet("background:#3a1b1b; border:1px solid #ff5c5c; border-radius:6px; color:#ff5c5c; padding:3px 7px; font-weight:800;")
            btn.setToolTip(f"Запис зупинено через помилку:\n{self.logger.error}\nНатисніть, щоб спробувати знову (R)")
        elif self.rec_enabled:
            self.title_bar.set_label(btn, "● REC", "●")
            btn.setStyleSheet("background:#3a1b1b; border:1px solid #ff5c5c; border-radius:6px; color:#ff7b7b; padding:3px 7px; font-weight:800;")
            where = self.logger.path or LOGS_DIR
            btn.setToolTip(f"Йде запис логу (R — зупинити)\n{where}\nФайл створюється з першими даними.")
        else:
            self.title_bar.set_label(btn, "REC", "○")
            btn.setStyleSheet("")
            btn.setToolTip(f"Почати запис логу польоту в CSV (R)\n{LOGS_DIR}")

    def _evaluate_alarm(self, track: Optional[bool], qualities):
        for name in self.alarm.evaluate(time.monotonic(), track, qualities, self.alarm_cfg):
            if not self.muted:
                self.sound.play(name)

    def toggle_mute(self):
        """M: silence alarms for this session without touching the settings."""
        self.muted = not self.muted
        self._refresh_title_text()

    def cleanup_logs(self) -> list:
        keep = self.logger.path if self.logger.enabled else None
        return cleanup_old_logs(self.log_retention_days, keep)

    def _apply_threshold_change(self):
        """Repaint everything whose colour depends on a threshold."""
        self._quick_metric_key = None
        for card in (self.air_signal_card, self.ground_signal_card):
            for bar in card.bars:
                bar.update()
        if self.last_state is not None:
            self._render_cards(self.last_state)
            self.update_compact_metrics(self.last_state)
        self.update_latency_cards()
        self._painted_warn = None
        self.update_warnings()

    def set_recording(self, on: bool):
        self.rec_enabled = on
        if on:
            self.logger.start(demo=self.demo_mode)
        else:
            self.logger.stop()
        self.update_rec_indicator()

    def toggle_recording(self):
        self.set_recording(not self.rec_enabled)

    def _log_row(self, s: Optional[TelemetryState], link: Optional[str] = None, warnings: Optional[str] = None) -> Dict[str, Any]:
        """One CSV row. `s` is None for an outage row (no payload at all)."""
        row: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec='milliseconds'),
            "source": "demo" if self.demo_mode else "live",
            "link": link or (s.link_summary if s else ""),
            "air_ping_ms": _fmt_num(self.last_air_latency),
            "ground_ping_ms": _fmt_num(self.last_ground_latency),
            "rc_ping_ms": _fmt_num(self.last_rc_latency),
            "warnings": warnings if warnings is not None else " | ".join(self._last_warnings),
        }
        if s is None:
            return row
        row.update({
            "uav": s.uav,
            "distance_m": _fmt_num(s.distance_value),
            "air_rssi0_dbm": _fmt_num(s.air_rssi0),
            "air_rssi1_dbm": _fmt_num(s.air_rssi1),
            "air_rssi_best_dbm": _fmt_num(s.air_best_rssi),
            "ground_rssi0_dbm": _fmt_num(s.ground_rssi0),
            "ground_rssi1_dbm": _fmt_num(s.ground_rssi1),
            "ground_rssi_best_dbm": _fmt_num(s.ground_best_rssi),
            "air_snr_db": _fmt_num(s.air_snr, 1),
            "ground_snr_db": _fmt_num(s.ground_snr, 1),
            "air_quality_pct": _fmt_num(s.air_quality_pct),
            "ground_quality_pct": _fmt_num(s.ground_quality_pct),
            "air_lq_pct": _fmt_num(s.air_pure_quality_pct),
            "ground_lq_pct": _fmt_num(s.ground_pure_quality_pct),
            "air_fec_err_pct": _fmt_num(s.air_ldpc_rate, 3),
            "ground_fec_err_pct": _fmt_num(s.ground_ldpc_rate, 3),
            "mcs": s.mcs_mode,
            "capacity_mbps": _fmt_num(s.mcs_mbps, 2),
            "freq_mhz": _fmt_num(s.freq_mhz),
            "downlink_kbs": _fmt_num(s.downlink_kbs),
            "uplink_kbs": _fmt_num(s.uplink_kbs),
            "air_temp_c": _fmt_num(s.air_temp_c),
            "ground_temp_c": _fmt_num(s.ground_temp_c),
            "air_ant": s.air_ant,
            "ground_ant": s.ground_ant,
            "air_tx_dbm": _fmt_num(s.air_tx_dbm, 1),
            "ground_tx_dbm": _fmt_num(s.ground_tx_dbm, 1),
        })
        return row

    def on_drag_started(self, global_pos: QPoint):
        if self.position_locked or self._resize_mode:
            return
        self.drag_anchor = global_pos
        self.drag_window_pos = self.frameGeometry().topLeft()

    def on_drag_moved(self, global_pos: QPoint):
        if self.position_locked or self._resize_mode:
            return
        if self.drag_anchor is None or self.drag_window_pos is None:
            return
        delta = global_pos - self.drag_anchor
        self._move_within_screen(self.drag_window_pos + delta, global_pos)

    def _quick_panel_mouse_press(self, event):
        if self.compact_mode and event.button() == Qt.LeftButton and not self.position_locked and not self._resize_mode:
            self.drag_anchor = event.globalPosition().toPoint()
            self.drag_window_pos = self.frameGeometry().topLeft()
            event.accept()
            return
        QFrame.mousePressEvent(self.quick_panel, event)

    def _quick_panel_mouse_move(self, event):
        if self.compact_mode and (event.buttons() & Qt.LeftButton) and self.drag_anchor is not None and self.drag_window_pos is not None and not self.position_locked and not self._resize_mode:
            cursor = event.globalPosition().toPoint()
            self._move_within_screen(self.drag_window_pos + (cursor - self.drag_anchor), cursor)
            event.accept()
            return
        QFrame.mouseMoveEvent(self.quick_panel, event)

    def _quick_panel_mouse_release(self, event):
        if self.compact_mode:
            self.drag_anchor = None
            self.drag_window_pos = None
            event.accept()
            return
        QFrame.mouseReleaseEvent(self.quick_panel, event)

    def _move_within_screen(self, target: QPoint, cursor: Optional[QPoint] = None):
        """Move to `target`, kept fully inside one monitor's work area.

        The monitor is the one under the cursor while dragging, otherwise the
        one under the window's centre. v1.39 always clamped to the monitor
        the window was *already* on (`self.screen()`), so dragging it towards
        a second monitor stopped dead at the edge of the first: the bug that
        made the overlay impossible to move to another display.
        """
        probe = cursor if cursor is not None else QPoint(target.x() + self.width() // 2, target.y() + self.height() // 2)
        screen = (QGuiApplication.screenAt(probe) or QGuiApplication.screenAt(QCursor.pos())
                  or self.screen() or QGuiApplication.primaryScreen())
        if not screen:
            self.move(target)
            return
        self.move(clamp_to_rect(target, self.width(), self.height(), screen.availableGeometry()))

    def _window_state(self) -> dict:
        size = self._full_size if self.compact_mode else self.size()
        pos = self.frameGeometry().topLeft()
        return {
            "x": pos.x(), "y": pos.y(),
            "w": size.width(), "h": size.height(),
            "compact": self.compact_mode,
            "topmost": self.topmost,
        }

    def _restore_window_state(self):
        """Put the window back where it was last closed, if that is still on a
        connected monitor; otherwise snap it to the right edge as before."""
        saved = self._saved_window or {}
        try:
            w = max(MIN_W, int(saved.get("w", 0)))
            h = max(MIN_H, int(saved.get("h", 0)))
            x, y = int(saved["x"]), int(saved["y"])
        except Exception:
            self.snap_right()
            return
        self._full_size = QSize(w, h)
        self.compact_mode = bool(saved.get("compact", False))
        if saved.get("topmost") is False:
            self.topmost = False
            self.setWindowFlags((self.windowFlags() & ~Qt.WindowStaysOnTopHint) | Qt.Window | Qt.FramelessWindowHint)
        self.resize(w, h)
        shown_w, shown_h = (COMPACT_W, COMPACT_H) if self.compact_mode else (w, h)
        rect = QRect(x, y, shown_w, shown_h)
        # Require a usable chunk of the window on some monitor, so a display
        # that was unplugged since last time can't strand it off-screen.
        visible = any(
            (screen.availableGeometry() & rect).width() >= min(120, shown_w)
            and (screen.availableGeometry() & rect).height() >= min(40, shown_h)
            for screen in QGuiApplication.screens()
        )
        if visible:
            self.move(x, y)
        else:
            self.snap_right()

    def snap_right(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        x = geo.right() - self.width() - 8
        y = geo.top() + 8
        self.move(clamp_to_rect(QPoint(x, y), self.width(), self.height(), geo))

    def toggle_topmost(self):
        self.topmost = not self.topmost
        # setWindowFlags recreates the native window and drops the position, so
        # capture and restore it.
        pos = self.frameGeometry().topLeft()
        flags = self.windowFlags()
        if self.topmost:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags | Qt.Window | Qt.FramelessWindowHint)
        self.show()
        self.move(pos)
        self._update_pin_label()

    def _update_pin_label(self):
        self.title_bar.set_label(self.title_bar.pin_btn, "TOP" if self.topmost else "NORMAL",
                                 "▲" if self.topmost else "△")

    def toggle_lock(self):
        self.position_locked = not self.position_locked
        self._refresh_title_text()

    def adjust_scale(self, delta: float):
        self.scale_factor = clamp(self.scale_factor + delta, 0.8, 1.45)
        self._apply_responsive_layout()
        self.apply_styles()

    def reset_scale(self):
        self.scale_factor = 1.0
        self._apply_responsive_layout()
        self.apply_styles()

    def detect_resize_mode(self, pos) -> Optional[str]:
        rect = self.rect()
        right = rect.width() - pos.x() <= RESIZE_MARGIN
        bottom = rect.height() - pos.y() <= RESIZE_MARGIN
        if right and bottom:
            return 'bottom-right'
        if right:
            return 'right'
        if bottom:
            return 'bottom'
        return None

    def _is_on_resize_zone(self, global_pos: QPoint) -> bool:
        pos = self.mapFromGlobal(global_pos)
        if pos.x() < 0 or pos.y() < 0 or pos.x() > self.width() or pos.y() > self.height():
            return False
        return self.detect_resize_mode(pos) is not None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            mode = self.detect_resize_mode(event.position().toPoint())
            if mode:
                self._resize_mode = mode
                self._resize_start_geo = self.geometry()
                self._resize_start_pos = event.globalPosition().toPoint()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._resize_mode and self._resize_start_geo and self._resize_start_pos:
            delta = event.globalPosition().toPoint() - self._resize_start_pos
            geo = QRect(self._resize_start_geo)
            if self._resize_mode in ('right', 'bottom-right'):
                geo.setWidth(max(MIN_W, self._resize_start_geo.width() + delta.x()))
            if self._resize_mode in ('bottom', 'bottom-right'):
                geo.setHeight(max(MIN_H, self._resize_start_geo.height() + delta.y()))
            self.setGeometry(geo)
            self._position_size_grip()
            event.accept()
            return
        if self._is_on_resize_zone(event.globalPosition().toPoint()):
            mode = self.detect_resize_mode(pos)
            if mode == 'bottom-right':
                self.setCursor(QCursor(Qt.SizeFDiagCursor))
            elif mode == 'right':
                self.setCursor(QCursor(Qt.SizeHorCursor))
            elif mode == 'bottom':
                self.setCursor(QCursor(Qt.SizeVerCursor))
        else:
            self.setCursor(Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._resize_mode = None
        self._resize_start_geo = None
        self._resize_start_pos = None
        self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def _panel_width(self) -> int:
        if hasattr(self, 'telemetry_scroll'):
            return self.telemetry_scroll.viewport().width()
        return self.width()

    def _apply_responsive_layout(self):
        """Single entry point for all size-dependent work.

        Driven by the debounce timer, never directly from resizeEvent.
        """
        if self.compact_mode:
            return
        self._narrow_title = below_threshold(getattr(self, "_narrow_title", None), self.width(), TITLE_FULL_LABELS_MIN_W)
        self.title_bar.set_narrow(self._narrow_title)
        self._relayout_status_row()
        self._relayout_cards()
        self._scale_cards_for_window()

    def _relayout_status_row(self):
        layout = self.status_row.layout()
        width = self._panel_width()
        stacked = below_threshold(self._status_stacked, width, STATUS_SIDE_BY_SIDE_MIN_W)
        # Tearing the layout apart and rebuilding it re-parents widgets, so only
        # do it when the arrangement genuinely changes.
        if stacked == self._status_stacked and layout.count() > 0:
            return
        self._status_stacked = stacked
        while layout.count():
            layout.takeAt(0)
        if stacked:
            layout.addWidget(self.link_banner, 0, 0)
            layout.addWidget(self.distance_card, 1, 0)
            layout.setColumnStretch(0, 1)
            layout.setColumnStretch(1, 0)
        else:
            layout.addWidget(self.link_banner, 0, 0)
            layout.addWidget(self.distance_card, 0, 1)
            layout.setColumnStretch(0, 1)
            layout.setColumnStretch(1, 1)

    def _relayout_cards(self):
        width = self._panel_width()
        # Width only. The old `or height < 760` kept a short-but-wide window
        # in one tall column, which is exactly backwards.
        one_column = below_threshold(self._single_column, width, TWO_COLUMN_MIN_W)
        if one_column == self._single_column and self.cards_grid.count() > 0:
            return
        if self._single_column is not None:
            # The inline threshold differs per mode; don't carry its
            # hysteresis state across a column switch.
            self._inline_cards = None
        self._single_column = one_column
        while self.cards_grid.count():
            self.cards_grid.takeAt(0)
        for i, card in enumerate(self._cards):
            row, col = (i, 0) if one_column else (i // 2, i % 2)
            self.cards_grid.addWidget(card, row, col)
        self.cards_grid.setColumnStretch(0, 1)
        self.cards_grid.setColumnStretch(1, 0 if one_column else 1)

    def _scale_cards_for_window(self):
        width = max(300, self._panel_width())
        height = max(420, self.height())
        factor_w = max(0.82, min(1.15, width / 820.0))
        factor_h = max(0.84, min(1.08, height / 820.0))
        factor = min(factor_w, factor_h)
        # Inline (value folded into the title) is about vertical room: ten
        # stacked rows need it far sooner than five rows of two.
        if self._single_column:
            inline_mode = below_threshold(self._inline_cards, self.height(), SINGLE_COLUMN_FULL_CARDS_MIN_H)
        else:
            inline_mode = below_threshold(self._inline_cards, self.height(), TWO_COLUMN_INLINE_MAX_H)
        self._inline_cards = inline_mode
        distance_compact = width < DISTANCE_LARGE_MIN_W or self.height() < 760

        card_min_height = int((60 if inline_mode else 76) * factor)
        for card in self._cards:
            if card.minimumHeight() != card_min_height:
                card.setMinimumHeight(card_min_height)
            # The card renders its own inline form; the window no longer
            # rewrites title text behind the card's back.
            card.set_compact(inline_mode)

        dist_px = max(16, int((20 if distance_compact else 34) * factor))
        if dist_px != self._painted_distance_px:
            self._painted_distance_px = dist_px
            self.distance_card.set_value_font_px(dist_px)
            self.distance_card.setMaximumHeight(int(max(68, (88 if distance_compact else 84) * factor)))

        self.fit_factor = factor
        self.apply_styles()

    def _position_size_grip(self):
        if self._size_grip:
            self._size_grip.move(max(0, self.width() - self._size_grip.width()), max(0, self.height() - self._size_grip.height()))
            self._size_grip.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_size_grip()
        # Cheap here, everything expensive is deferred to the debounce timer.
        if not self.compact_mode and hasattr(self, "_resize_timer"):
            self._resize_timer.start()

    def apply_styles(self):
        """One stylesheet for the whole window.

        There used to be two: `apply_styles` (driven by the Ctrl +/- zoom) and
        `apply_styles_scaled` (driven by the window size). The size-driven one
        ran on every poll and every resize, so it always won and the zoom
        hotkeys did nothing. Now the user zoom and the auto-fit factor are
        multiplied into a single factor, and the sheet is only pushed to Qt when
        the rounded factor actually changes -- setting a stylesheet on the top
        level window re-polishes and re-lays-out the entire widget tree, which
        is what made the UI feel sluggish.
        """
        factor = clamp(self.scale_factor * self.fit_factor, 0.7, 1.7)
        factor = round(factor, 2)
        if factor == self._painted_style_factor:
            return
        self._painted_style_factor = factor

        base = max(9, int(11 * factor))
        medium = max(16, int(22 * factor))
        big = max(24, int(36 * factor))
        self.setStyleSheet(f"""
QMainWindow, QWidget {{ background: #0f1419; color: #eaf2f8; font-family: Segoe UI, Arial, sans-serif; }}
/* The QWidget rule above also matches QLabel, which painted an opaque
   #0f1419 band behind every piece of text sitting on a lighter card. */
QLabel {{ background: transparent; }}
#titleBar, #quickPanel, #statusRow {{ background: #151d24; border: 1px solid #263341; border-radius: 9px; }}
#appTitle {{ font-size: {max(17, int(24*factor))}px; font-weight: 800; }}
#topBtn, #quickExpand {{ background: #1d2832; border: 1px solid #32414d; border-radius: 6px; padding: 3px 7px; font-size: {base}px; font-weight: 700; }}
#topBtn:hover {{ background: #253341; }}
#linkBanner {{ background:#19232d; border:1px solid #263341; border-radius:12px; padding:8px; font-size:{max(14,int(18*factor))}px; font-weight:800; min-height: 0px; }}
#card {{ background:#182129; border:1px solid #263341; border-radius:10px; }}
#cardTitle {{ font-size:{base}px; font-weight:700; color:#8ea1b2; letter-spacing:1px; }}
#cardValue {{ font-size:{medium}px; font-weight:800; }}
#cardValueBig {{ font-size:{big}px; font-weight:900; }}
#cardDetail {{ font-size:{max(8, base-1)}px; color:#b9c7d3; }}
#antBar {{ background: transparent; font-size:{base + 1}px; font-weight:700; min-height:{max(18, int(base * 1.9))}px; }}
#txPill {{ background:#10161c; border:1px solid #32414d; border-radius:6px; padding:1px 7px; color:#8bd3ff; font-size:{base}px; font-weight:800; }}
#chartStats {{ color:#b9c7d3; font-size:{base}px; font-weight:600; padding: 0 4px; }}
#footer {{ font-size:{base}px; color:#6f8598; padding: 2px; }}
#warnBar {{ background:#2a2113; border:1px solid #5c4a1d; border-radius:8px; }}
#warnLabel {{ color:#ffd24d; font-size:{base}px; font-weight:700; }}
#rawDataView {{ background:#0c1116; border:1px solid #263341; border-radius:8px; color:#c9d7e2; font-family: Consolas, monospace; font-size:{base}px; }}
#viewTabs::pane {{ border:1px solid #263341; border-radius:8px; }}
#quickMetric {{ background:#10161c; border:1px solid #2b3b48; border-radius:6px; font-size:{base}px; font-weight:800; padding:1px; }}
#rcLabel {{ color:#eaf2f8; font-size:{base}px; font-weight:800; padding-left:4px; }}
#recLabel {{ font-size:{base}px; font-weight:800; padding-right:6px; }}
#fpvDot {{ background:#7a7f87; border:1px solid #32414d; border-radius:6px; }}
#rcDot {{ background:#7a7f87; border:1px solid #32414d; border-radius:6px; }}
#quickFpvDot {{ background:#7a7f87; border:1px solid #32414d; border-radius:8px; }}
#quickRcDot {{ background:#7a7f87; border:1px solid #32414d; border-radius:8px; }}
#quickRcLabel {{ color:#eaf2f8; font-size:{base}px; font-weight:800; }}
QComboBox {{ background:#10161c; border:1px solid #32414d; border-radius:6px; padding:2px 6px; font-size:{base}px; }}
QComboBox QAbstractItemView {{ background:#10161c; selection-background-color:#253341; }}
#eventList {{ background:#0c1116; border:1px solid #263341; border-radius:8px; font-family: Consolas, monospace; font-size:{base}px; }}
#logSummary {{ color:#d6eaf8; font-size:{base}px; padding: 2px 4px; }}
#streamEdit {{ background: #0f1419; color: #eaf2f8; border: 1px solid #32414d; border-radius: 8px; padding: 8px 10px; font-size: {base}px; }}
""")
        # Font metrics changed, so every elided label needs recomputing.
        for card in list(self._cards) + [getattr(self, "distance_card", None)]:
            if card is not None:
                card._render()


def run_selftest(app: QApplication) -> int:
    """Headless build check: --selftest

    Packaged as a windowed .exe there is no console to print to, so the result
    goes to a file next to the report path and into the exit code. Useful for
    confirming a build actually works on a machine you just copied it to.
    """
    lines = []
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            fn()
            lines.append(f"PASS  {name}")
        except Exception as e:
            ok = False
            lines.append(f"FAIL  {name}: {type(e).__name__}: {e}")

    lines.append(f"Taisync Telemetry selftest  frozen={FROZEN}")
    lines.append(f"app dir   : {APP_DIR}")
    lines.append(f"data dir  : {DATA_DIR}")
    lines.append(f"settings  : {SETTINGS_PATH}")
    lines.append(f"icon      : {ICON_PATH} (exists={ICON_PATH.exists()})")
    lines.append(f"pyqtgraph : {'yes' if pg is not None else 'MISSING'}")
    lines.append(f"requests  : {'yes' if requests is not None else 'MISSING'}")
    lines.append("")

    win = MainWindow(persist=False)
    check("window builds", lambda: win.show())
    check("icon present", lambda: (_ for _ in ()).throw(RuntimeError("icon missing")) if not ICON_PATH.exists() else None)
    check("data dir writable", lambda: (DATA_DIR.mkdir(parents=True, exist_ok=True),
                                        (DATA_DIR / ".writetest").write_text("ok", encoding="utf-8"),
                                        (DATA_DIR / ".writetest").unlink()))
    check("demo data feeds UI", lambda: (win.toggle_demo(), app.processEvents(),
                                         (_ for _ in ()).throw(RuntimeError("no state"))
                                         if win.last_state is None else None))

    def antennas_and_power():
        for _ in range(3):
            win._demo_tick()
            app.processEvents()
        for card in (win.air_signal_card, win.ground_signal_card):
            if any(bar.dbm is None for bar in card.bars):
                raise RuntimeError(f"{card._base_title}: antenna level missing")
            if not card.tx.text().startswith("TX ") or card.tx.text() == "TX --":
                raise RuntimeError(f"{card._base_title}: TX power missing")
        if win.last_state.air_ldpc_rate is None:
            raise RuntimeError("FEC error rate not computed from counter deltas")
    check("both antennas + TX power shown", antennas_and_power)

    def tabs():
        for i in range(win.view_tabs.count()):
            win.view_tabs.setCurrentIndex(i)
            app.processEvents()
    check("all tabs render (incl. traffic chart)", tabs)
    check("traffic chart active", lambda: (_ for _ in ()).throw(RuntimeError("pyqtgraph unavailable"))
          if win.traffic_chart.plot_widget is None else None)

    def resizes():
        for w, h in ((1280, 900), (820, 760), (620, 700), (1280, 900)):
            win.resize(w, h)
            app.processEvents()
            win._apply_responsive_layout()
            app.processEvents()
    check("responsive layout", resizes)
    check("compact overlay", lambda: (win.toggle_compact_view(), app.processEvents(),
                                      win.toggle_compact_view(), app.processEvents()))

    def log_roundtrip():
        import tempfile
        rows = []
        for _ in range(5):
            win._demo_tick()
            app.processEvents()
            rows.append(win._log_row(win.last_state))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selftest.csv"
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            data = load_log(path)
            if len(data.rows) != 5 or data.series("air_snr_db") is None:
                raise RuntimeError("log did not read back")
            summarize_log(data)
            detect_events(data)
            win.logs_tab.load_path(path)
            win.view_tabs.setCurrentWidget(win.logs_tab)
            app.processEvents()
            win.logs_tab.mode_combo.setCurrentIndex(LogsTab.MODE_TIME)
            app.processEvents()
    check("flight log write / read / analyse", log_roundtrip)
    # Generates the alarm WAVs and checks winsound is in the build, without
    # actually playing anything during a build.
    check("alarm sounds available", lambda: (
        [SoundPlayer()._path(name) for name in SOUNDS],
        __import__("winsound") if platform.system().lower() == "windows" else None))
    win.toggle_demo()
    win.close()
    app.processEvents()

    lines.append("")
    lines.append("RESULT: " + ("OK" if ok else "FAILED"))
    report = "\n".join(lines)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "selftest.txt").write_text(report, encoding="utf-8")
        lines.append(f"report written to {DATA_DIR / 'selftest.txt'}")
    except Exception:
        pass
    if sys.stdout is not None:
        try:
            print(report)
        except Exception:
            pass
    return 0 if ok else 1


# Named mutex held for the life of the process. The installer's AppMutex
# refers to the same name, so it can tell the app is running and ask to close
# it before overwriting files.
INSTANCE_MUTEX = "TaisyncTelemetry_SingleInstance"


def acquire_single_instance() -> bool:
    """False if another copy is already running (Windows only).

    Two copies poll and ping the same units twice and fight over the log;
    there's no use for it.
    """
    if platform.system().lower() != "windows":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
        already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        # Keep a reference so the handle lives as long as the process.
        acquire_single_instance.handle = handle
        return not already
    except Exception:
        return True


def main():
    app = QApplication(sys.argv)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    if "--selftest" in sys.argv:
        sys.exit(run_selftest(app))
    if not acquire_single_instance():
        QMessageBox.information(None, "Taisync Telemetry", "Taisync Telemetry вже запущена.")
        sys.exit(0)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
