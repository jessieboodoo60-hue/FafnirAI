"""Windows that leave Fafnir's screen.

Dragging a desktop window past the edge of Fafnir's monitor (or "Move to another screen")
turns it into a real Windows window that can sit on any monitor. It shows the same app,
live, from the same page code (index.html?solo=<app>) and talks to Python through the same
web channel, so everything (data, conversation, confirmations) keeps working.
"""
from __future__ import annotations

import ctypes
import itertools
import json
from urllib.parse import urlencode

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

TITLES = {"fafnir": "Fafnir", "mail": "Mail", "calendar": "Calendar", "tasks": "Tasks", "notices": "Notices",
          "files": "Files", "business": "Business", "settings": "Settings", "viewer": "Viewer"}
_ids = itertools.count(1)


def _round_corners(win) -> None:
    """Ask Windows 11 for rounded corners on this frameless window (ignored elsewhere)."""
    try:
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(int(win.winId()), 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


class PopOut(QWebEngineView):
    def __init__(self, desk, app: str, args: dict, web_index):
        super().__init__()
        self.desk, self.app = desk, app
        self.pid = f"pop{next(_ids)}"
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowTitle(f"{TITLES.get(app, app.title())} · {desk.cfg.assistant.name}")
        self.setPage(QWebEnginePage(desk.profile, self))
        self.page().setWebChannel(desk.channel)
        self.page().setBackgroundColor(QColor(30, 31, 36))
        self.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self.setMinimumSize(360, 260)
        url = QUrl.fromLocalFile(str(web_index))
        url.setQuery(urlencode({"solo": app, "id": self.pid, "args": json.dumps(args or {})}))
        self.load(url)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        _round_corners(self)

    def action(self, what: str, arg: int = 0) -> None:
        handle = self.windowHandle()
        if what == "move" and handle:
            handle.startSystemMove()
        elif what == "resize" and handle:
            edges = Qt.Edges()
            for bit, edge in ((1, Qt.TopEdge), (2, Qt.LeftEdge), (4, Qt.RightEdge), (8, Qt.BottomEdge)):
                if arg & bit:
                    edges |= edge
            handle.startSystemResize(edges)
        elif what == "close":
            self.close()
        elif what == "minimize":
            self.showMinimized()
        elif what == "maximize":
            self.showNormal() if self.isMaximized() else self.showMaximized()

    def closeEvent(self, event) -> None:
        self.desk.pop_closed(self)
        super().closeEvent(event)


def place(desk, pop: PopOut, opts: dict) -> None:
    """Size and position a new pop-out: under the mouse when dragged off the screen edge,
    otherwise centred on the next monitor."""
    w, h = int(opts.get("w") or 760), int(opts.get("h") or 540)
    screens = QGuiApplication.screens()
    home = desk.screen()
    if opts.get("drag"):
        cur = QCursor.pos()
        target = QGuiApplication.screenAt(cur) or home
        avail = target.availableGeometry()
        w, h = min(w, avail.width()), min(h, avail.height())
        x = cur.x() - int(opts.get("grabX", w // 2))
        y = cur.y() - int(opts.get("grabY", 18))
        x = max(avail.left(), min(x, avail.right() - w))
        y = max(avail.top(), min(y, avail.bottom() - h))
    else:
        others = [s for s in screens if s is not home] or [home]
        target = others[0]
        avail = target.availableGeometry()
        w, h = min(w, avail.width() - 40), min(h, avail.height() - 40)
        x, y = avail.center().x() - w // 2, avail.center().y() - h // 2
    pop.resize(w, h)
    pop.move(x, y)
    pop.show()
    pop.raise_()
    pop.activateWindow()
    if opts.get("drag"):
        # the mouse button is still down: keep dragging, now as a real window
        QTimer.singleShot(30, lambda: pop.windowHandle() and pop.windowHandle().startSystemMove())
