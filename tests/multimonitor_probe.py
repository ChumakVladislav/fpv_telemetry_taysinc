"""Helper for test_drag_to_second_monitor: runs in its own process.

Qt picks its platform once per process, so the two-monitor offscreen setup
can't share the main test process. Run from a directory containing
`screens.json`; Qt's offscreen option parser splits on ':', so the config
path has to be relative (no drive letter).

    python multimonitor_probe.py <path to app .py>

Prints one JSON line with the window position after each step.
"""

import importlib.util
import json
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen:configfile=screens.json"
os.environ["TAISYNC_OFFLINE"] = "1"

spec = importlib.util.spec_from_file_location("telemetry_app", sys.argv[1])
app_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_mod)

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
result = {"screens": [[s.geometry().x(), s.geometry().width()] for s in QGuiApplication.screens()]}

win = app_mod.MainWindow(persist=False)
win.resize(800, 700)
win.move(100, 100)
win.show()
app.processEvents()
result["start"] = [win.x(), win.y()]

# Grab the title bar at (300, 120) and drag the cursor 2400 px to the right,
# onto the second monitor, the way a user would.
grab = QPoint(300, 120)
win.on_drag_started(grab)
for step in range(1, 13):
    win.on_drag_moved(grab + QPoint(200 * step, 0))
    app.processEvents()
result["after_drag"] = [win.x(), win.y()]

# Snap must use the monitor the window is now on.
win.snap_right()
app.processEvents()
result["after_snap"] = [win.x(), win.y()]

# Compact overlay dragged across too.
win.toggle_compact_view()
app.processEvents()
win.move(200, 50)
app.processEvents()
ev_from = QPoint(250, 60)
win.drag_anchor = ev_from
win.drag_window_pos = win.frameGeometry().topLeft()
win._move_within_screen(win.drag_window_pos + (QPoint(2300, 60) - ev_from), QPoint(2300, 60))
app.processEvents()
result["compact_after_drag"] = [win.x(), win.y()]
win.close()
print(json.dumps(result))
