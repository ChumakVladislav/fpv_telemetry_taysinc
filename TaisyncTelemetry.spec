# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for Taisync Telemetry.

Builds a one-folder application: dist/TaisyncTelemetry/TaisyncTelemetry.exe

One-folder (not one-file) is deliberate. A one-file build unpacks ~100 MB of Qt
into a temp directory on every launch, which adds several seconds of startup and
is a common false-positive trigger for antivirus. The folder starts instantly and
the installer hides it from the user anyway.
"""

from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "TaisyncTelemetry"
ENTRY = "fpv_telemetry_v1_40.py"
ICON = "fpv_telemetry_icon.ico"

# Qt ships far more than this app uses; dropping these keeps the build to a
# sane size without touching anything that is actually imported.
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets", "PySide6.QtQml",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtTest", "PySide6.QtHelp",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtSql", "PySide6.QtWebSockets",
    "PySide6.QtWebChannel", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtHttpServer", "PySide6.QtNetworkAuth",
    "tkinter", "matplotlib", "scipy", "pandas", "PIL", "IPython", "pytest",
    "PyQt5", "PyQt6", "PySide2",
]

a = Analysis(
    [ENTRY],
    pathex=[],
    binaries=[],
    # Bundled read-only assets. resource_path() in the app resolves these via
    # sys._MEIPASS when frozen.
    datas=[(ICON, ".")],
    hiddenimports=collect_submodules("pyqtgraph.graphicsItems"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# Excluding a Python module does not remove the Qt DLL behind it, so strip the
# large unused Qt components explicitly. Only QtCore/QtGui/QtWidgets are used
# (pyqtgraph draws through the raster backend), and no QTranslator is ever
# constructed, so Qt's own translations are dead weight too.
#
# Deliberately kept: opengl32sw.dll (~20 MB). It is Qt's software OpenGL
# fallback and is what keeps the window rendering on machines with no usable
# GPU driver -- not worth trading for size on hardware we don't control.
_DROP = (
    "pyside6/translations/",
    "pyside6/qt6quick", "pyside6/qt6qml", "pyside6/qt6pdf",
    "pyside6/qtquick", "pyside6/qtqml", "pyside6/qtpdf",
)


def _is_dropped(entry):
    name = entry[0].replace("\\", "/").lower()
    return any(part in name for part in _DROP)


a.binaries = [x for x in a.binaries if not _is_dropped(x)]
a.datas = [x for x in a.datas if not _is_dropped(x)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
