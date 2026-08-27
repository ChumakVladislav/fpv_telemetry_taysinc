
import csv
import os
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import platform
import re
import subprocess
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import json
from typing import Any, Dict, Optional

try:
    import requests
except ModuleNotFoundError:
    requests = None

try:
    import pyqtgraph as pg
except ModuleNotFoundError:
    pg = None

from PySide6.QtCore import QPoint, QRect, QThread, QTimer, Qt, Signal, QObject
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut, QIcon, QCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QSpacerItem,
    QSizeGrip,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QTabWidget,
    QPlainTextEdit,
)

DEFAULT_GROUND_HOST = "192.168.199.16"
DEFAULT_AIR_HOST = "192.168.199.18"
DEFAULT_RC_HOST = ""
POLL_MS = 1200
REQUEST_TIMEOUT = 0.35
BASE_DIR = Path(__file__).resolve().parent
ICON_PATH = BASE_DIR / "fpv_telemetry_icon.ico"
LOGS_DIR = BASE_DIR / "logs"
SETTINGS_PATH = Path.cwd() / "telemetry_settings.json"
PING_DEBUG_PATH = Path.cwd() / "ping_debug.log"
RESIZE_MARGIN = 6
MIN_W = 300
MIN_H = 680
MAX_SAMPLES = 180


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


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
    if best_rssi is None:
        return None
    percent = linear_map(best_rssi, -94, -74, 0, 100)
    return int(round(clamp(percent, 0, 100)))


def snr_to_percent(snr: Optional[float]) -> Optional[int]:
    if snr is None:
        return None
    percent = linear_map(snr, 6, 20, 0, 100)
    return int(round(clamp(percent, 0, 100)))


def ldpc_fail_to_percent(ldpc_fail: Optional[float]) -> Optional[int]:
    if ldpc_fail is None:
        return None
    if ldpc_fail <= 10:
        return 100
    if ldpc_fail >= 100:
        return 20
    percent = linear_map(ldpc_fail, 10, 100, 100, 20)
    return int(round(clamp(percent, 20, 100)))


def combine_quality(snr: Optional[float], ldpc_fail: Optional[float]) -> Optional[int]:
    snr_pct = snr_to_percent(snr)
    ldpc_pct = ldpc_fail_to_percent(ldpc_fail)
    vals = [v for v in (snr_pct, ldpc_pct) if v is not None]
    if not vals:
        return None
    return min(vals)


def read_percent(*values: Any) -> Optional[int]:
    for value in values:
        n = parse_number(value)
        if n is None:
            continue
        return int(round(clamp(n, 0, 100)))
    return None


def metric_color(percent: Optional[int]) -> str:
    if percent is None:
        return "#7a7f87"
    if percent >= 75:
        return "#2ecc71"
    if percent >= 45:
        return "#f1c40f"
    return "#ff5c5c"


def quality_status(percent: Optional[int]) -> str:
    if percent is None:
        return "N/A"
    if percent >= 75:
        return "GOOD"
    if percent >= 45:
        return "MID"
    return "CRIT"


def quality_accent(percent: Optional[int]) -> str:
    if percent is None:
        return "#182129"
    if percent >= 75:
        return "#163126"
    if percent >= 45:
        return "#3b3416"
    return "#3a1b1b"


def signal_color(percent: Optional[int]) -> str:
    if percent is None:
        return "#7a7f87"
    if percent > 70:
        return "#2ecc71"
    if percent >= 50:
        return "#f1c40f"
    return "#ff5c5c"


def temp_color(temp_c: Optional[float]) -> str:
    if temp_c is None:
        return "#7a7f87"
    if temp_c < 50:
        return "#2ecc71"
    if temp_c <= 60:
        return "#f1c40f"
    return "#ff5c5c"


def latency_color(ms: Optional[int]) -> str:
    if ms is None:
        return "#7a7f87"
    if ms < 100:
        return "#2ecc71"
    if ms <= 250:
        return "#f1c40f"
    return "#ff5c5c"


def status_color(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "track":
        return "#2ecc71"
    if s == "init":
        return "#f39c12"
    return "#ff5c5c"


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
        try:
            with PING_DEBUG_PATH.open('a', encoding='utf-8') as f:
                f.write(f"HOST={host} RC={result.returncode}\n{output}\n---\n")
        except Exception:
            pass
    except Exception as e:
        try:
            with PING_DEBUG_PATH.open('a', encoding='utf-8') as f:
                f.write(f"HOST={host} EXC={e}\n---\n")
        except Exception:
            pass
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


def warning_messages(data: dict, air_latency: Optional[int], ground_latency: Optional[int], fpv_online: Optional[bool], rc_online: Optional[bool]) -> list[str]:
    msgs = []

    if fpv_online is False:
        msgs.append("FPV unreachable")
    if rc_online is False:
        msgs.append("RC unreachable")

    air_temp = safe_int(data.get("airAD9361Temp"), 0)
    ground_temp = safe_int(data.get("groundAD9361Temp"), 0)
    if air_temp > 60:
        msgs.append(f"AIR temp high {air_temp}C")
    elif air_temp >= 50:
        msgs.append(f"AIR temp warm {air_temp}C")
    if ground_temp > 60:
        msgs.append(f"GND temp high {ground_temp}C")
    elif ground_temp >= 50:
        msgs.append(f"GND temp warm {ground_temp}C")

    if air_latency is not None and air_latency > 250:
        msgs.append(f"AIR latency high {air_latency} ms")
    elif air_latency is not None and air_latency >= 100:
        msgs.append(f"AIR latency medium {air_latency} ms")
    if ground_latency is not None and ground_latency > 250:
        msgs.append(f"GND latency high {ground_latency} ms")
    elif ground_latency is not None and ground_latency >= 100:
        msgs.append(f"GND latency medium {ground_latency} ms")

    air_fail = safe_int(data.get("airLdpcFail"), 0)
    air_pass = safe_int(data.get("airLdpcPass"), 0)
    ground_fail = safe_int(data.get("groundLdpcFail"), 0)
    ground_pass = safe_int(data.get("groundLdpcPass"), 0)
    air_total = air_fail + air_pass
    ground_total = ground_fail + ground_pass
    air_fail_pct = (100.0 * air_fail / air_total) if air_total > 0 else 0.0
    ground_fail_pct = (100.0 * ground_fail / ground_total) if ground_total > 0 else 0.0
    if air_fail_pct >= 1.0:
        msgs.append(f"AIR LDPC fail {air_fail_pct:.2f}%")
    if ground_fail_pct >= 1.0:
        msgs.append(f"GND LDPC fail {ground_fail_pct:.2f}%")

    if safe_int(data.get("airTxOverflowCnt"), 0) > 0:
        msgs.append(f"AIR overflow {safe_int(data.get('airTxOverflowCnt'), 0)}")
    if safe_int(data.get("groundTxOverflowCnt"), 0) > 0:
        msgs.append(f"GND overflow {safe_int(data.get('groundTxOverflowCnt'), 0)}")
    if safe_int(data.get("airRxDiscardCnt"), 0) > 0:
        msgs.append(f"AIR discard {safe_int(data.get('airRxDiscardCnt'), 0)}")
    if safe_int(data.get("groundRxDiscardCnt"), 0) > 0:
        msgs.append(f"GND discard {safe_int(data.get('groundRxDiscardCnt'), 0)}")

    if str(data.get("UAV", "")).lower() not in ("online", "ok"):
        msgs.append(f"UAV {data.get('UAV', 'unknown')}")
    if str(data.get("bindStatus", "")).lower() not in ("bind", "bound", "ok"):
        msgs.append(f"BIND {data.get('bindStatus', 'unknown')}")

    return msgs[:4]


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


class SettingsDialog(QDialog):
    def __init__(self, ground_host: str, air_host: str, rc_host: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection Settings")
        self.setModal(True)
        self.resize(420, 220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("IP налаштування")
        title.setStyleSheet("font-size: 18px; font-weight: 800; color: #eaf2f8;")
        layout.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        self.ground_edit = QLineEdit(ground_host)
        self.air_edit = QLineEdit(air_host)
        self.rc_edit = QLineEdit(rc_host)

        for edit in (self.ground_edit, self.air_edit, self.rc_edit):
            edit.setPlaceholderText("192.168.x.x")
            edit.setClearButtonEnabled(True)
            edit.setObjectName("streamEdit")

        grid.addWidget(QLabel("Ground IP"), 0, 0)
        grid.addWidget(self.ground_edit, 0, 1)
        grid.addWidget(QLabel("Air IP"), 1, 0)
        grid.addWidget(self.air_edit, 1, 1)
        grid.addWidget(QLabel("RC IP"), 2, 0)
        grid.addWidget(self.rc_edit, 2, 1)
        layout.addLayout(grid)

        info = QLabel("API URL формується автоматично як http://<ground_ip>/v2/baseband.json")
        info.setWordWrap(True)
        info.setStyleSheet("color: #8ea1b2; font-size: 12px;")
        layout.addWidget(info)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setStyleSheet("background: #101418; color: #eaf2f8;")

    def values(self):
        return {
            "ground_host": self.ground_edit.text().strip(),
            "air_host": self.air_edit.text().strip(),
            "rc_host": self.rc_edit.text().strip(),
        }


class CsvLogger:
    def __init__(self):
        self.file = None
        self.writer = None
        self.path = None
        self.enabled = False

    def start(self):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOGS_DIR / f"telemetry_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.file = self.path.open('w', newline='', encoding='utf-8')
        self.writer = None
        self.enabled = True

    def stop(self):
        self.enabled = False
        if self.file:
            self.file.close()
        self.file = None
        self.writer = None

    def log(self, state: TelemetryState):
        if not self.enabled or not self.file:
            return
        row = asdict(state)
        if self.writer is None:
            self.writer = csv.DictWriter(self.file, fieldnames=list(row.keys()))
            self.writer.writeheader()
        self.writer.writerow(row)
        self.file.flush()


class PollWorker(QObject):
    data_ready = Signal(dict)
    error_ready = Signal(str)

    def __init__(self):
        super().__init__()
        self.session = requests.Session() if requests else None
        self.timer = None
        self.busy = False
        self.api_url = ""

    def set_api_url(self, url: str):
        self.api_url = url.strip()

    def start(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)
        self.poll()

    def poll(self):
        if self.busy:
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


class PingWorker(QObject):
    status_ready = Signal(str, object, object)

    def __init__(self):
        super().__init__()
        self.timer = None
        self.hosts = {}
        self.busy = False

    def set_hosts(self, hosts: Dict[str, str]):
        self.hosts = dict(hosts)

    def start(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(2000)
        self.poll()

    def poll(self):
        if self.busy:
            return
        self.busy = True
        try:
            for key, host in self.hosts.items():
                if not host:
                    self.status_ready.emit(key, None, None)
                    continue
                latency = ping_latency_ms(host, timeout_ms=700)
                self.status_ready.emit(key, host, latency)
        finally:
            self.busy = False


class LatencyWorker(QObject):
    latency_ready = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.timer = None
        self.air_host = ""
        self.ground_host = ""
        self.busy = False

    def set_hosts(self, air_host: str, ground_host: str):
        self.air_host = (air_host or "").strip()
        self.ground_host = (ground_host or "").strip()

    def start(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1200)
        self.poll()

    def poll(self):
        if self.busy:
            return
        self.busy = True
        try:
            air = ping_latency_ms(self.air_host, timeout_ms=700) if self.air_host else None
            ground = ping_latency_ms(self.ground_host, timeout_ms=700) if self.ground_host else None
            self.latency_ready.emit(air, ground)
        finally:
            self.busy = False


class MetricCard(QFrame):
    def __init__(self, title: str, big: bool = False):
        super().__init__()
        self.setObjectName("card")
        self._last_value = None
        self._last_detail = None
        self._last_color = None
        self._last_status = None
        self._base_title = title
        self._big = big

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

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
        if value != self._last_value:
            self.value.setText(value)
            self._last_value = value
        if detail != self._last_detail:
            self.detail.setText(detail)
            self._last_detail = detail
        if color != self._last_color:
            self.value.setStyleSheet(f"color: {color};" if color else "")
            self._last_color = color
        wanted_title = self._base_title if not status_text else f"{self._base_title} • {status_text}"
        if wanted_title != self._last_status:
            self.title.setText(wanted_title)
            self._last_status = wanted_title
        if accent:
            self.setStyleSheet(f"#card{{background:{accent}; border:1px solid #3a4652; border-radius:14px;}}")
        else:
            self.setStyleSheet("")


class TitleBar(QFrame):
    drag_started = Signal(QPoint)
    drag_moved = Signal(QPoint)
    snap_right = Signal()
    toggle_top = Signal()
    toggle_debug = Signal()
    toggle_compact = Signal()
    toggle_help = Signal()
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

        self.rec_label = QLabel("")
        self.rec_label.hide()
        self.rec_label.setObjectName("recLabel")
        layout.addWidget(self.rec_label)

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

        self.expand_btn = QPushButton("EXPAND")
        self.settings_btn = QPushButton("SET")
        self.help_btn = QPushButton("HELP")
        self.help_btn.hide()
        self.debug_btn = QPushButton("DEBUG")
        self.debug_btn.hide()
        self.pin_btn = QPushButton("TOP")
        self.snap_btn = QPushButton(">")
        self.close_btn = QPushButton("X")

        for btn in [self.expand_btn, self.settings_btn, self.help_btn, self.debug_btn, self.pin_btn, self.snap_btn, self.close_btn]:
            btn.setObjectName("topBtn")
            btn.setFixedHeight(28)
            layout.addWidget(btn)

        self.expand_btn.clicked.connect(self.toggle_compact.emit)
        self.settings_btn.clicked.connect(self.open_settings.emit)
        self.help_btn.clicked.connect(self.toggle_help.emit)
        self.debug_btn.clicked.connect(self.toggle_debug.emit)
        self.pin_btn.clicked.connect(self.toggle_top.emit)
        self.snap_btn.clicked.connect(self.snap_right.emit)
        self.set_fpv_online(None)
        self.set_rc_online(None)

    def set_fpv_online(self, online: Optional[bool]):
        color = "#7a7f87" if online is None else "#2ecc71" if online else "#ff5c5c"
        self.fpv_dot.setStyleSheet(f"background:{color}; border:1px solid #32414d; border-radius:6px;")

    def set_rc_online(self, online: Optional[bool]):
        color = "#7a7f87" if online is None else "#2ecc71" if online else "#ff5c5c"
        self.rc_dot.setStyleSheet(f"background:{color}; border:1px solid #32414d; border-radius:6px;")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_started.emit(event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self.drag_moved.emit(event.globalPosition().toPoint())
        super().mouseMoveEvent(event)


class TrafficChart(QWidget):
    LIMIT_KBS = 3000  # ~3 Mb/s

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.max_samples = MAX_SAMPLES
        self.uplink_history = []
        self.downlink_history = []

        if pg is None:
            placeholder = QLabel("pyqtgraph \u043d\u0435 \u0432\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u043e \u2014 \u0433\u0440\u0430\u0444\u0456\u043a \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0438\u0439")
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
        self.plot_widget.setLabel("bottom", "Samples")
        self.plot_widget.setYRange(0, self.LIMIT_KBS)

        legend = self.plot_widget.addLegend()

        self.downlink_curve = self.plot_widget.plot(
            [], [], pen=pg.mkPen("#5bc8ff", width=2), name="Downlink"
        )

        self.right_axis = pg.ViewBox()
        self.plot_widget.scene().addItem(self.right_axis)
        self.plot_widget.getAxis("right").linkToView(self.right_axis)
        self.right_axis.setXLink(self.plot_widget.getPlotItem().vb)
        self.plot_widget.showAxis("right")
        self.plot_widget.getAxis("right").setLabel("Uplink (kb/s)", color="#f1c40f")
        self.right_axis.setYRange(0, self.LIMIT_KBS)

        self.uplink_curve = pg.PlotDataItem(
            [], [], pen=pg.mkPen("#f1c40f", width=2), name="Uplink"
        )
        self.right_axis.addItem(self.uplink_curve)
        legend.addItem(self.uplink_curve, "Uplink")

        limit_line = pg.InfiniteLine(
            pos=self.LIMIT_KBS, angle=0,
            pen=pg.mkPen("#ff5c5c", width=1, style=Qt.DashLine)
        )
        self.plot_widget.addItem(limit_line)

        def _update_views():
            self.right_axis.setGeometry(self.plot_widget.getPlotItem().vb.sceneBoundingRect())

        self.plot_widget.getPlotItem().vb.sigResized.connect(_update_views)

        layout.addWidget(self.plot_widget)

    def add_sample(self, uplink_kbs, downlink_kbs):
        if self.plot_widget is None:
            return
        self.uplink_history.append(uplink_kbs or 0)
        self.downlink_history.append(downlink_kbs or 0)
        if len(self.uplink_history) > self.max_samples:
            self.uplink_history.pop(0)
            self.downlink_history.pop(0)
        x = list(range(len(self.uplink_history)))
        self.downlink_curve.setData(x, self.downlink_history)
        self.uplink_curve.setData(x, self.uplink_history)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.scale_factor = 1.0
        self.drag_anchor = None
        self.drag_window_pos = None
        self.debug_mode = False
        self.compact_mode = False
        self.topmost = True
        self.position_locked = False
        self.last_banner_text = None
        self.last_banner_color = None
        self.logger = CsvLogger()
        self.rec_enabled = False
        self.times = []
        self.distances = []
        self.qualities = []
        self._resize_mode = None
        self._resize_start_geo = None
        self._resize_start_pos = None
        self._cards = []
        self._single_column = False
        self.help_dialog = None
        self.last_data = None
        self.last_air_latency = None
        self.last_ground_latency = None

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
        self.link_banner = QLabel("WARNINGS")
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
        self.quick_air_signal = QLabel("A SIG\n--")
        self.quick_air_quality = QLabel("A Q\n--")
        self.quick_ground_signal = QLabel("G SIG\n--")
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
            metric.setMinimumSize(62, 42)
            metric.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            metric.setAttribute(Qt.WA_TransparentForMouseEvents, True)
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

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setHandleWidth(12)
        self.main_splitter.setChildrenCollapsible(False)
        root_layout.addWidget(self.main_splitter, 1)

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
        self.air_signal_card = MetricCard("AIR SIGNAL")
        self.ground_signal_card = MetricCard("GROUND SIGNAL")
        self.air_quality_card = MetricCard("AIR QUALITY")
        self.ground_quality_card = MetricCard("GROUND QUALITY")
        self.air_ant_card = MetricCard("AIR ANTENNA")
        self.ground_ant_card = MetricCard("GROUND ANTENNA")
        self.air_temp_card = MetricCard("AIR TEMP")
        self.ground_temp_card = MetricCard("GROUND TEMP")
        self.air_latency_card = MetricCard("AIR LAT")
        self.ground_latency_card = MetricCard("GND LAT")
        cards = [
            self.air_signal_card,
            self.ground_signal_card,
            self.air_quality_card,
            self.ground_quality_card,
            self.air_ant_card,
            self.ground_ant_card,
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
        self.view_tabs.currentChanged.connect(self._on_view_tab_changed)

        self.main_splitter.addWidget(self.telemetry_scroll)
        self.main_splitter.setStretchFactor(0, 5)
        self.main_splitter.setSizes([820])

        self.title_bar.close_btn.clicked.connect(self.close)
        self.title_bar.drag_started.connect(self.on_drag_started)
        self.title_bar.drag_moved.connect(self.on_drag_moved)
        self.title_bar.snap_right.connect(self.snap_right)
        self.title_bar.toggle_top.connect(self.toggle_topmost)
        self.title_bar.toggle_compact.connect(self.toggle_compact_view)
        self.title_bar.toggle_help.connect(self.show_help)
        self.title_bar.open_settings.connect(self.show_settings)

        QShortcut(QKeySequence("T"), self, activated=self.toggle_topmost)
        QShortcut(QKeySequence("L"), self, activated=self.toggle_lock)
        QShortcut(QKeySequence("R"), self, activated=self.toggle_recording)
        QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self.adjust_scale(0.08))
        QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: self.adjust_scale(0.08))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self.adjust_scale(-0.08))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self.reset_scale)

        self.apply_styles()
        self.snap_right()
        self.title_bar.pin_btn.setText("TOP")
        self.update_rec_indicator()
        self.refresh_connection_labels()
        self.debug_settings_status()
        QTimer.singleShot(0, self.apply_compact_view)

        self.thread = QThread(self)
        self.worker = PollWorker()
        self.worker.set_api_url(self.api_url())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.data_ready.connect(self.on_data)
        self.worker.error_ready.connect(self.on_error)
        self.thread.start()

        self.ping_thread = QThread(self)
        self.ping_worker = PingWorker()
        self.ping_worker.set_hosts({"rc": self.rc_host})
        self.ping_worker.moveToThread(self.ping_thread)
        self.ping_thread.started.connect(self.ping_worker.start)
        self.ping_worker.status_ready.connect(self.on_host_status)
        self.ping_thread.start()

        self.latency_thread = QThread(self)
        self.latency_worker = LatencyWorker()
        self.latency_worker.set_hosts(self.air_host, self.ground_host)
        self.latency_worker.moveToThread(self.latency_thread)
        self.latency_thread.started.connect(self.latency_worker.start)
        self.latency_worker.latency_ready.connect(self.on_latency_update)
        self.latency_thread.start()

        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(20, 20)
        self._size_grip.setToolTip("Resize window")
        self._position_size_grip()

    def api_url(self) -> str:
        return f"http://{self.ground_host}/v2/baseband.json"

    def load_settings(self):
        try:
            if SETTINGS_PATH.exists():
                raw = SETTINGS_PATH.read_text(encoding="utf-8").strip()
                if raw:
                    data = json.loads(raw)
                    self.ground_host = str(data.get("ground_host", self.ground_host)).strip() or self.ground_host
                    self.air_host = str(data.get("air_host", self.air_host)).strip() or self.air_host
                    self.rc_host = str(data.get("rc_host", self.rc_host)).strip()
        except Exception:
            pass

    def save_settings(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ground_host": self.ground_host,
                "air_host": self.air_host,
                "rc_host": self.rc_host,
            }
            SETTINGS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            try:
                self.footer.setText(f"API: {self.api_url()} | CFG:ERR|{e}")
            except Exception:
                pass

    def debug_settings_status(self):
        try:
            exists = SETTINGS_PATH.exists()
            size = SETTINGS_PATH.stat().st_size if exists else 0
            self.footer.setText(f"API: {self.api_url()} | CFG:{'OK' if exists else 'MISS'}|{size}B | {SETTINGS_PATH.name}")
        except Exception:
            self.footer.setText(f"API: {self.api_url()} | CFG:ERR")

    def closeEvent(self, event):
        self.logger.stop()
        for th in (getattr(self, "thread", None), getattr(self, "ping_thread", None), getattr(self, "latency_thread", None)):
            try:
                if th is not None:
                    th.quit()
                    th.wait(1000)
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
        self.main_splitter.setVisible(not compact)
        self.title_bar.expand_btn.setVisible(not compact)
        self.title_bar.settings_btn.setVisible(not compact)
        self.title_bar.expand_btn.setText("EXPAND" if compact else "COMPACT")
        self.title_bar.pin_btn.setVisible(not compact)
        self.title_bar.snap_btn.setVisible(not compact)
        if compact:
            root = self.centralWidget().layout()
            if root:
                root.setContentsMargins(0, 0, 0, 0)
                root.setSpacing(0)
            self.quick_panel.setFixedSize(430, 58)
            self.quick_panel.setStyleSheet("background: transparent; border: 0;")
            self.centralWidget().setStyleSheet("background: transparent;")
            self.centralWidget().setFixedSize(430, 58)
            self.setFixedSize(430, 58)
        else:
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
            self._single_column = None
            self._relayout_status_row()
            self._relayout_cards()
            self._scale_cards_for_window()
        if compact:
            self.setFixedSize(430, 58)

    def refresh_connection_labels(self):
        self.footer.setText(f"API: {self.api_url()}")
        self.title_bar.set_fpv_online(self.fpv_online)
        self.title_bar.set_rc_online(self.rc_online)
        self._update_quick_host_indicators()
        self.update_warnings()

    def _update_quick_host_indicators(self):
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
        dlg = SettingsDialog(self.ground_host, self.air_host, self.rc_host, self)
        if dlg.exec() != QDialog.Accepted:
            return
        values = dlg.values()
        self.ground_host = (values["ground_host"] or self.ground_host).strip()
        self.air_host = (values["air_host"] or self.air_host).strip()
        self.rc_host = (values["rc_host"] or "").strip()
        self.save_settings()
        self.worker.set_api_url(self.api_url())
        if hasattr(self, "ping_worker"):
            self.rc_online = None
            self.ping_worker.set_hosts({"rc": self.rc_host})
        if hasattr(self, "latency_worker"):
            self.last_air_latency = None
            self.last_ground_latency = None
            self.latency_worker.set_hosts(self.air_host, self.ground_host)
        self.refresh_connection_labels()
        self.debug_settings_status()
        if hasattr(self, "ping_worker"):
            try:
                self.ping_worker.poll()
            except Exception:
                pass
        if hasattr(self, "latency_worker"):
            try:
                self.latency_worker.poll()
            except Exception:
                pass
        if self.last_data is not None:
            self.on_data(self.last_data)

    def on_host_status(self, key: str, host: Optional[str], latency: Optional[int]):
        online = None if not host else bool(latency is not None)
        if key == "rc":
            self.rc_online = online
        self.title_bar.set_rc_online(self.rc_online)
        self._update_quick_host_indicators()

    def on_latency_update(self, air_latency: Optional[int], ground_latency: Optional[int]):
        self.last_air_latency = air_latency
        self.last_ground_latency = ground_latency
        if hasattr(self, "air_latency_card"):
            self.air_latency_card.update_data('--' if air_latency is None else f'{air_latency} ms', f'ping {self.air_host or "N/A"}', latency_color(air_latency))
        if hasattr(self, "ground_latency_card"):
            self.ground_latency_card.update_data('--' if ground_latency is None else f'{ground_latency} ms', f'ping {self.ground_host or "N/A"}', latency_color(ground_latency))
        self.update_warnings()

    def _quality_debug_text(self, label: str, snr: Optional[float], ldpc: Optional[float], q: Optional[int]) -> str:
        return f"{label} SNR={snr if snr is not None else 'N/A'} LDPC={ldpc if ldpc is not None else 'N/A'} Q={q if q is not None else 'N/A'}"

    def update_banner(self, text: str, color: str):
        if text != self.last_banner_text or color != self.last_banner_color:
            self.link_banner.setText(text)
            self.link_banner.setStyleSheet(
                f"background: #19232d; border: 1px solid #263341; border-radius: 12px; padding: 12px; font-size: {int(24 * self.scale_factor)}px; font-weight: 800; color: {color};"
            )
            self.last_banner_text = text
            self.last_banner_color = color

    def update_compact_metrics(self, s: TelemetryState):
        self.quick_air_signal.setToolTip(f"Air signal: {s.air_signal_pct}% | RSSI: {s.air_best_rssi} dBm")
        self.quick_air_quality.setToolTip(f"Air quality: {s.air_quality_pct}% | SNR: {s.air_snr} | LDPC: {s.air_ldpc_fail}")
        self.quick_ground_signal.setToolTip(f"Ground signal: {s.ground_signal_pct}% | RSSI: {s.ground_best_rssi} dBm")
        self.quick_ground_quality.setToolTip(f"Ground quality: {s.ground_quality_pct}% | SNR: {s.ground_snr} | LDPC: {s.ground_ldpc_fail}")
        self.quick_air_signal.setText(f"A SIG\n{'--' if s.air_signal_pct is None else str(s.air_signal_pct) + '%'}")
        self.quick_air_quality.setText(f"A Q\n{'--' if s.air_quality_pct is None else str(s.air_quality_pct) + '%'}")
        self.quick_ground_signal.setText(f"G SIG\n{'--' if s.ground_signal_pct is None else str(s.ground_signal_pct) + '%'}")
        self.quick_ground_quality.setText(f"G Q\n{'--' if s.ground_quality_pct is None else str(s.ground_quality_pct) + '%'}")
        for widget, value in ((self.quick_air_signal, s.air_signal_pct), (self.quick_air_quality, s.air_quality_pct), (self.quick_ground_signal, s.ground_signal_pct), (self.quick_ground_quality, s.ground_quality_pct)):
            color = metric_color(value)
            widget.setStyleSheet(f"background: #10161c; border: 1px solid {color}; border-radius: 6px; color: {color}; font-size: 10px; font-weight: 800;")

    def build_state(self, data: Dict[str, Any]) -> TelemetryState:
        air_rssi = [to_dbm(data.get('airRSSI0')), to_dbm(data.get('airRSSI1'))]
        ground_rssi = [to_dbm(data.get('groundRSSI0')), to_dbm(data.get('groundRSSI1'))]
        air_best = max([v for v in air_rssi if v is not None], default=None)
        ground_best = max([v for v in ground_rssi if v is not None], default=None)
        air_status = safe_text(data.get('airLinkStatus'))
        ground_status = safe_text(data.get('groundLinkStatus'))
        both_track = air_status.lower() == 'track' and ground_status.lower() == 'track'
        distance = parse_number(data.get('distance'))
        distance_text = f"{int(round(distance))} m" if both_track and distance is not None else 'N/A'
        air_snr = parse_number(data.get('airSnr'))
        ground_snr = parse_number(data.get('groundSnr'))
        air_ldpc = parse_number(data.get('airLdpcFail'))
        ground_ldpc = parse_number(data.get('groundLdpcFail'))
        ground_alt = parse_number(data.get('groundLdpc')) or parse_number(data.get('groundLdpFail')) or parse_number(data.get('groundFail'))
        air_pure_quality = read_percent(
            str(data.get('airLinkQuality')).replace('%', '').strip() if data.get('airLinkQuality') is not None else None,
            data.get('airQuality'), data.get('uplinkLQ'), data.get('uplinkQuality')
        )
        ground_pure_quality = read_percent(
            str(data.get('groundLinkQuality')).replace('%', '').strip() if data.get('groundLinkQuality') is not None else None,
            str(data.get('groundLinkQualit')).replace('%', '').strip() if data.get('groundLinkQualit') is not None else None,
            data.get('groundQuality'), data.get('downlinkLQ'), data.get('downlinkQuality')
        )
        air_latency = self.last_air_latency
        ground_latency = self.last_ground_latency
        bitrate_text = str(data.get('downlinkDataRate')) if data.get('downlinkDataRate') is not None else 'N/A'
        uplink_kbs = parse_kbs(data.get('uplinkDataRate'))
        downlink_kbs = parse_kbs(data.get('downlinkDataRate'))
        freq_text = f"{data.get('groundCurrentTxFreq')} MHz" if data.get('groundCurrentTxFreq') is not None else 'N/A'
        air_true_quality = combine_quality(air_snr, air_ldpc)
        ground_true_quality = snr_to_percent(ground_snr)
        return TelemetryState(
            timestamp=datetime.now().isoformat(timespec='milliseconds'),
            air_status=air_status,
            ground_status=ground_status,
            distance_text=distance_text,
            distance_value=distance,
            air_signal_pct=best_rssi_to_percent(air_best),
            ground_signal_pct=best_rssi_to_percent(ground_best),
            air_quality_pct=air_true_quality,
            ground_quality_pct=ground_true_quality,
            air_pure_quality_pct=air_pure_quality,
            ground_pure_quality_pct=ground_pure_quality,
            air_best_rssi=air_best,
            ground_best_rssi=ground_best,
            air_snr=air_snr,
            ground_snr=ground_snr,
            air_ldpc_fail=air_ldpc,
            ground_ldpc_fail=ground_ldpc if ground_ldpc is not None else ground_alt,
            air_latency_ms=air_latency,
            ground_latency_ms=ground_latency,
            air_ant=safe_text(data.get('airCurrentAnt')),
            ground_ant=safe_text(data.get('groundCurrentAnt')),
            mcs=safe_text(data.get('MCS')),
            bitrate=bitrate_text,
            freq=freq_text,
            air_tx=safe_text(data.get('airTxPower')),
            ground_tx=safe_text(data.get('groundTxPower')),
            uplink_kbs=uplink_kbs,
            downlink_kbs=downlink_kbs,
            link_summary='TRACK' if both_track else f"AIR {air_status} / GND {ground_status}",
        )

    def on_data(self, data: Dict[str, Any]):
        self.last_data = data
        if hasattr(self, "view_tabs") and self.view_tabs.currentIndex() == 2:
            self.update_raw_data_view(data)
        air_temp = parse_number(data.get('airAD9361Temp')) or parse_number(data.get('A-AD9361Temp'))
        ground_temp = parse_number(data.get('groundAD9361Temp')) or parse_number(data.get('G-AD9361Temp'))
        s = self.build_state(data)
        uav_status = str(data.get("UAV", "")).strip().lower()
        self.fpv_online = uav_status == "online"
        self.title_bar.set_fpv_online(self.fpv_online)
        self._update_quick_host_indicators()
        self.update_compact_metrics(s)
        banner_status = 'track' if s.link_summary == 'TRACK' else 'init' if 'INIT' in s.link_summary.upper() else 'lost'
        self.update_banner(s.link_summary, status_color(banner_status))
        self.distance_card.update_data(s.distance_text, f"AIR: {s.air_status} | GND: {s.ground_status}", '#ecf0f1')
        self.air_signal_card.update_data('--' if s.air_signal_pct is None else f"{s.air_signal_pct}%", f"Best RSSI: {s.air_best_rssi:.0f} dBm" if s.air_best_rssi is not None else 'Best RSSI: N/A', signal_color(s.air_signal_pct))
        self.ground_signal_card.update_data('--' if s.ground_signal_pct is None else f"{s.ground_signal_pct}%", f"Best RSSI: {s.ground_best_rssi:.0f} dBm" if s.ground_best_rssi is not None else 'Best RSSI: N/A', signal_color(s.ground_signal_pct))
        air_quality_text = f"T {('--' if s.air_quality_pct is None else str(s.air_quality_pct) + '%')} | P {('--' if s.air_pure_quality_pct is None else str(s.air_pure_quality_pct) + '%')}"
        air_quality_detail = f"True: {self._quality_debug_text('AIR', s.air_snr, s.air_ldpc_fail, s.air_quality_pct)} | Pure: {('--' if s.air_pure_quality_pct is None else str(s.air_pure_quality_pct) + '%')}"
        ground_quality_text = f"T {('--' if s.ground_quality_pct is None else str(s.ground_quality_pct) + '%')} | P {('--' if s.ground_pure_quality_pct is None else str(s.ground_pure_quality_pct) + '%')}"
        ground_quality_detail = f"True: SNR={s.ground_snr if s.ground_snr is not None else 'N/A'} Q={s.ground_quality_pct if s.ground_quality_pct is not None else 'N/A'} | Pure: {('--' if s.ground_pure_quality_pct is None else str(s.ground_pure_quality_pct) + '%')} | LDPC raw={s.ground_ldpc_fail if s.ground_ldpc_fail is not None else 'N/A'}"
        self.air_quality_card.update_data(air_quality_text, air_quality_detail, metric_color(s.air_quality_pct), quality_status(s.air_quality_pct), quality_accent(s.air_quality_pct))
        self.ground_quality_card.update_data(ground_quality_text, ground_quality_detail, metric_color(s.ground_quality_pct), quality_status(s.ground_quality_pct), quality_accent(s.ground_quality_pct))
        self.air_ant_card.update_data(s.air_ant, 'auto/manual + rf1/rf2', '#8bd3ff')
        self.ground_ant_card.update_data(s.ground_ant, 'auto/manual + rf1/rf2', '#8bd3ff')
        self.air_temp_card.update_data('--' if air_temp is None else f'{air_temp:.1f} °C', 'A-AD9361Temp' if air_temp is not None else 'A-AD9361Temp: N/A', temp_color(air_temp))
        self.ground_temp_card.update_data('--' if ground_temp is None else f'{ground_temp:.1f} °C', 'G-AD9361Temp' if ground_temp is not None else 'G-AD9361Temp: N/A', temp_color(ground_temp))
        self.air_latency_card.update_data('--' if s.air_latency_ms is None else f'{s.air_latency_ms} ms', f'ping {self.air_host or 'N/A'}', latency_color(s.air_latency_ms))
        self.ground_latency_card.update_data('--' if s.ground_latency_ms is None else f'{s.ground_latency_ms} ms', f'ping {self.ground_host or 'N/A'}', latency_color(s.ground_latency_ms))
        if not self.compact_mode:
            self._scale_cards_for_window()
        self.logger.log(s)
        if hasattr(self, "traffic_chart"):
            self.traffic_chart.add_sample(s.uplink_kbs, s.downlink_kbs)

    def _warning_summary(self) -> str:
        if not hasattr(self, "warn_label"):
            return "OK"
        text = self.warn_label.text().replace("WARN: ", "").strip()
        return text if text and text != "--" else "OK"

    def _warning_summary(self) -> str:
        if not hasattr(self, "warn_label"):
            return "OK"
        text = self.warn_label.text().replace("WARN: ", "").strip()
        return text if text and text != "--" else "OK"

    def _apply_warning_banner(self):
        if not hasattr(self, "link_banner"):
            return
        summary = self._warning_summary()
        self.link_banner.setText("WARNINGS" if summary == "OK" else summary)
        if summary == "OK":
            self.link_banner.setStyleSheet("color: #2ecc71;")
        else:
            self.link_banner.setStyleSheet("color: #ffd24d;")

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
        msgs = warning_messages(data, self.last_air_latency, self.last_ground_latency, self.fpv_online, self.rc_online)
        if msgs:
            self.warn_bar.hide()
            self.warn_label.setText("WARN: " + " | ".join(msgs))
        else:
            self.warn_bar.hide()
            self.warn_label.setText("WARN: --")
        self._apply_warning_banner()

    def on_error(self, message: str):
        self.update_banner('NO API', '#ff7b7b')
        self.distance_card.update_data('N/A', message[:80], '#bdc3c7')
        self.air_signal_card.update_data('--', 'waiting for data', '#7a7f87')
        self.ground_signal_card.update_data('--', 'waiting for data', '#7a7f87')
        self.air_quality_card.update_data('--', 'waiting for data', '#7a7f87', 'N/A', '#182129')
        self.ground_quality_card.update_data('--', 'waiting for data', '#7a7f87', 'N/A', '#182129')
        self.air_ant_card.update_data('--', 'waiting for data', '#7a7f87')
        self.ground_ant_card.update_data('--', 'waiting for data', '#7a7f87')
        self._update_quick_host_indicators()

    def show_help(self):
        if self.help_dialog is not None:
            try:
                self.help_dialog.close()
            except Exception:
                pass
        dlg = QDialog(self)
        dlg.setWindowTitle("Hotkeys Help")
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
            ("R", "Logger on/off"),
            ("Ctrl + +", "Scale up"),
            ("Ctrl + -", "Scale down"),
            ("Ctrl + 0", "Reset scale"),
            (">", "Snap right"),
            ("SET", "Ground/Air/RC IP settings"),
        ]
        for k, v in items:
            row = QLabel(f"{k} — {v}")
            row.setStyleSheet("font-size: 14px; color: #d6eaf8;")
            lay.addWidget(row)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        lay.addWidget(close_btn)
        dlg.setStyleSheet("background: #101418; color: #eaf2f8;")
        dlg.resize(360, 300)
        dlg.show()
        self.help_dialog = dlg

    def update_rec_indicator(self):
        self.title_bar.rec_label.setText("REC ON" if self.rec_enabled else "REC OFF")
        self.title_bar.rec_label.setStyleSheet("color: #ff5c5c;" if self.rec_enabled else "color: #7a7f87;")

    def toggle_recording(self):
        self.rec_enabled = not self.rec_enabled
        if self.rec_enabled:
            self.logger.start()
        else:
            self.logger.stop()
        self.update_rec_indicator()

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
        self._move_within_screen(self.drag_window_pos + delta)

    def _quick_panel_mouse_press(self, event):
        if self.compact_mode and event.button() == Qt.LeftButton and not self.position_locked and not self._resize_mode:
            self.drag_anchor = event.globalPosition().toPoint()
            self.drag_window_pos = self.frameGeometry().topLeft()
            event.accept()
            return
        QFrame.mousePressEvent(self.quick_panel, event)

    def _quick_panel_mouse_move(self, event):
        if self.compact_mode and (event.buttons() & Qt.LeftButton) and self.drag_anchor is not None and self.drag_window_pos is not None and not self.position_locked and not self._resize_mode:
            delta = event.globalPosition().toPoint() - self.drag_anchor
            self._move_within_screen(self.drag_window_pos + delta)
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

    def _move_within_screen(self, target):
        screen = self.screen() or QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if not screen:
            self.move(target)
            return
        geo = screen.availableGeometry()
        x = max(geo.left(), min(target.x(), geo.right() - self.width() + 1))
        y = max(geo.top(), min(target.y(), geo.bottom() - self.height() + 1))
        self.move(x, y)

    def snap_right(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        x = geo.right() - self.width() - 8
        y = geo.top() + 8
        self._move_within_screen(QPoint(x, y))

    def toggle_topmost(self):
        self.topmost = not self.topmost
        flags = self.windowFlags()
        if self.topmost:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags | Qt.Window | Qt.FramelessWindowHint)
        self.show()
        self.title_bar.pin_btn.setText("TOP" if self.topmost else "NORMAL")

    def toggle_lock(self):
        self.position_locked = not self.position_locked
        self.title_bar.title.setText("Taisync Telemetry [LOCK]" if self.position_locked else "Taisync Telemetry")

    def adjust_scale(self, delta: float):
        self.scale_factor = clamp(self.scale_factor + delta, 0.8, 1.45)
        self.apply_styles()

    def reset_scale(self):
        self.scale_factor = 1.0
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

    def _relayout_status_row(self):
        layout = self.status_row.layout()
        stacked = (not self.compact_mode) and ((self.telemetry_scroll.viewport().width() if hasattr(self, 'telemetry_scroll') else self.width()) < 760 or self.height() < 650)
        while layout.count():
            layout.takeAt(0)
        if stacked:
            layout.addWidget(self.link_banner, 0, 0)
            layout.addWidget(self.distance_card, 1, 0)
            layout.setColumnStretch(0, 1)
        else:
            layout.addWidget(self.link_banner, 0, 0)
            layout.addWidget(self.distance_card, 0, 1)
            layout.setColumnStretch(0, 1)
            layout.setColumnStretch(1, 1)

    def _relayout_cards(self):
        one_column = (not self.compact_mode) and (((self.telemetry_scroll.viewport().width() if hasattr(self, 'telemetry_scroll') else self.width()) < 860) or self.height() < 760)
        if one_column == self._single_column and self.cards_grid.count() > 0:
            return
        self._single_column = one_column
        while self.cards_grid.count():
            self.cards_grid.takeAt(0)
        for i, card in enumerate(self._cards):
            row, col = (i, 0) if one_column else (i // 2, i % 2)
            self.cards_grid.addWidget(card, row, col)
        self.cards_grid.setColumnStretch(0, 1)
        self.cards_grid.setColumnStretch(1, 0 if one_column else 1)

    def _scale_cards_for_window(self):
        panel_width = self.telemetry_scroll.viewport().width() if hasattr(self, 'telemetry_scroll') else self.width()
        width = max(300, panel_width)
        height = max(420, self.height())
        factor_w = max(0.82, min(1.15, width / 820.0))
        factor_h = max(0.84, min(1.08, height / 820.0))
        factor = min(factor_w, factor_h)
        inline_mode = self.width() < 760 or self.height() < 720
        distance_compact = self.width() < 1120 or self.height() < 760
        for card in self._cards:
            is_latency = getattr(card, '_base_title', '') in ('AIR LATENCY', 'GROUND LATENCY', 'AIR LAT', 'GND LAT')
            card.setMinimumHeight(int((60 if inline_mode else 76) * factor))
            value_text = card.value.text()
            title_text = card.title.text()
            base_title = getattr(card, '_base_title', title_text.split(' - ')[0].split(' • ')[0])
            status_suffix = ''
            if ' • ' in title_text:
                status_suffix = ' • ' + title_text.split(' • ', 1)[1].split(' - ')[0]
            if inline_mode:
                if is_latency:
                    card.value.setVisible(False)
                    compact_value = '--' if not value_text else value_text
                    card.title.setText(f"{base_title} - {compact_value}{status_suffix}")
                else:
                    card.value.setVisible(False)
                    if ' - ' not in title_text and value_text and value_text != '--':
                        card.title.setText(f"{title_text} - {value_text}")
            else:
                card.value.setVisible(True)
                card.title.setText(base_title + status_suffix)
        dist_size = max(16, int((20 if distance_compact else 34) * factor))
        self.distance_card.value.setStyleSheet(f"color: #ecf0f1; font-size: {dist_size}px; font-weight: 900;")
        self.distance_card.setMaximumHeight(int(max(68, (88 if distance_compact else 84) * factor)))
        self.apply_styles_scaled(factor)

    def _position_size_grip(self):
        if self._size_grip:
            self._size_grip.move(max(0, self.width() - self._size_grip.width()), max(0, self.height() - self._size_grip.height()))
            self._size_grip.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_size_grip()
        self._relayout_status_row()
        self._relayout_cards()
        if not self.compact_mode:
            self._scale_cards_for_window()

    def apply_styles_scaled(self, factor):
        base = max(9, int(11 * factor))
        medium = max(16, int(22 * factor))
        big = max(24, int(36 * factor))
        self.setStyleSheet(f"""
QMainWindow, QWidget {{ background: #0f1419; color: #eaf2f8; font-family: Segoe UI, Arial, sans-serif; }}
#titleBar, #quickPanel, #statusRow {{ background: #151d24; border: 1px solid #263341; border-radius: 9px; }}
#appTitle {{ font-size: {max(17, int(24*factor))}px; font-weight: 800; }}
#topBtn, #quickExpand {{ background: #1d2832; border: 1px solid #32414d; border-radius: 6px; padding: 3px 7px; font-size: {base}px; font-weight: 700; }}
#linkBanner {{ background:transparent; border:none; padding:4px; font-size:{max(14,int(18*factor))}px; font-weight:800; min-height: 0px; }}
#card {{ background:#182129; border:1px solid #263341; border-radius:10px; }}
#cardTitle {{ font-size:{base}px; font-weight:700; color:#8ea1b2; letter-spacing:1px; }}
#cardValue {{ font-size:{medium}px; font-weight:800; }}
#cardValueBig {{ font-size:{big}px; font-weight:900; }}
#cardDetail {{ font-size:{max(8, base-1)}px; color:#b9c7d3; }}
#footer {{ font-size:{base}px; color:#6f8598; }}
#quickMetric {{ background:#10161c; border:1px solid #2b3b48; border-radius:6px; font-size:{base}px; font-weight:800; padding:1px; }}
#rcLabel {{ color:#eaf2f8; font-size:{base}px; font-weight:800; padding-left:4px; }}
#fpvDot {{ background:#7a7f87; border:1px solid #32414d; border-radius:6px; }}
#rcDot {{ background:#7a7f87; border:1px solid #32414d; border-radius:6px; }}
#quickFpvDot {{ background:#7a7f87; border:1px solid #32414d; border-radius:8px; }}
#quickRcDot {{ background:#7a7f87; border:1px solid #32414d; border-radius:8px; }}
#quickRcLabel {{ color:#eaf2f8; font-size:{base}px; font-weight:800; }}
#streamEdit {{ background: #0f1419; color: #eaf2f8; border: 1px solid #32414d; border-radius: 8px; padding: 8px 10px; font-size: {base}px; }}
""")

    def apply_styles(self):
        base = int(12 * self.scale_factor)
        medium = int(28 * self.scale_factor)
        big = int(48 * self.scale_factor)
        title = int(26 * self.scale_factor)
        banner = int(24 * self.scale_factor)
        self.setStyleSheet(f"""
QMainWindow, QWidget {{
background: #0f1419;
color: #eaf2f8;
font-family: Segoe UI, Arial, sans-serif;
}}
#quickPanel {{
background: #151d24;
border: 1px solid #263341;
border-radius: 10px;
}}
#quickMetric {{
background: #10161c;
border: 1px solid #2b3b48;
border-radius: 8px;
font-size: {base}px;
font-weight: 800;
color: #2ecc71;
padding: 2px;
}}
#quickExpand {{
background: #253341;
border: 1px solid #41586a;
border-radius: 8px;
font-size: {medium}px;
font-weight: 800;
}}
#titleBar {{
background: #151d24;
border: 1px solid #263341;
border-radius: 12px;
}}
#appTitle {{
font-size: {title}px;
font-weight: 800;
letter-spacing: 1px;
}}
#recLabel {{
color: #ff7b7b;
font-size: {base}px;
font-weight: 800;
padding-right: 6px;
}}
#rcLabel {{
color: #eaf2f8;
font-size: {base}px;
font-weight: 800;
padding-left: 4px;
}}
#rcDot {{
background: #7a7f87;
border: 1px solid #32414d;
border-radius: 6px;
}}
#quickRcDot {{
background: #7a7f87;
border: 1px solid #32414d;
border-radius: 8px;
}}
#topBtn {{
background: #1d2832;
border: 1px solid #32414d;
border-radius: 8px;
padding: 4px 10px;
font-size: {base}px;
font-weight: 700;
}}
#topBtn:hover {{
background: #253341;
}}
#linkBanner {{
background: #19232d;
border: 1px solid #263341;
border-radius: 12px;
padding: 12px;
font-size: {banner}px;
font-weight: 800;
}}
#card {{
background: #182129;
border: 1px solid #263341;
border-radius: 14px;
}}
#cardTitle {{
font-size: {base}px;
font-weight: 700;
color: #8ea1b2;
letter-spacing: 1px;
}}
#cardValue {{
font-size: {medium}px;
font-weight: 800;
}}
#cardValueBig {{
font-size: {big}px;
font-weight: 900;
}}
#cardDetail {{
font-size: {base}px;
color: #b9c7d3;
}}
#footer {{
font-size: {max(10, base-1)}px;
color: #6f8598;
padding: 2px 2px;
}}
#streamEdit {{
background: #0f1419;
color: #eaf2f8;
border: 1px solid #32414d;
border-radius: 8px;
padding: 8px 10px;
font-size: {base}px;
}}
""")


def main():
    app = QApplication(sys.argv)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
