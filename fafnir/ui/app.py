"""Fafnir's desktop app: a fullscreen "liquid glass" screen that Fafnir runs.

Layout (top to bottom):
- top bar: name, model/cost, status, clock
- RIGHT NOW: a one-line briefing and the most important things at this moment, ranked
  (urgent notices, meetings soon, overdue tasks, VIP email, things due today, ...)
- the middle: side panels (calendar, tasks, notices, email, business & social, activity)
  around a centre stage. The stage holds the animated orb, or, when Fafnir puts something
  on screen ("pull up my emails"), that content, with the orb shrunk into the corner.
- the conversation and a type box (Fafnir can make it small or hide it)

Fafnir controls the screen through tools (screen_show / screen_clear / screen_layout /
screen_briefing, see fafnir/tools/screen_tools.py); they arrive here via fafnir/screen.py.

Keys: Esc stops Fafnir talking · F11 toggles fullscreen · Ctrl+Q quits.
"""
from __future__ import annotations

import html
import json
import sys
import threading
import time
import tomllib
from datetime import date, datetime, timedelta

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QTextBrowser, QVBoxLayout, QWidget,
)

from ..common import build_agent
from ..config import ROOT, ConfigError, load_config
from ..engine import Engine, Events, TurnStats
from ..screen import PANELS, SCREEN
from ..tools.control import CONFIRM_MARK
from . import glass
from .glass import ACCENT, BAD, DIM, GOOD, TEXT, VIOLET, WARN, GlassFrame
from .orb import Orb
from .priorities import rank

INSTANCE_KEY = "FafnirDesktopApp"

# old names kept for code below
C_TEXT, C_DIM, C_ACCENT, C_GOOD, C_WARN, C_BAD = TEXT, DIM, ACCENT, GOOD, WARN, BAD
C_LINE = "#3a4f66"
TAG_COLOR = {"urgent": BAD, "important": WARN, "good": GOOD, "info": TEXT}

STYLE = f"""
QWidget {{ color: {TEXT}; font-family: 'Segoe UI'; font-size: 15px; background: transparent; }}
QLabel#panelTitle {{ color: {ACCENT}; font-family: 'Bahnschrift SemiBold'; font-size: 12px; letter-spacing: 3px; }}
QLabel#brand {{ color: {TEXT}; font-family: 'Bahnschrift SemiBold'; font-size: 24px; letter-spacing: 10px; }}
QLabel#clock {{ font-family: 'Bahnschrift Light'; font-size: 30px; color: {TEXT}; }}
QLabel#date, QLabel#meta {{ color: {DIM}; font-size: 13px; }}
QLabel#status {{ font-family: 'Bahnschrift'; font-size: 15px; letter-spacing: 4px; }}
QLabel#caption {{ color: {TEXT}; font-size: 18px; }}
QLabel#briefing {{ font-family: 'Segoe UI Semibold'; font-size: 22px; color: {TEXT}; }}
QLabel#item {{ font-size: 15px; }}
QLabel#itemDim {{ color: {DIM}; font-size: 12px; }}
QLabel#stageTitle {{ font-family: 'Segoe UI Semibold'; font-size: 26px; }}
QLabel#cardTitle {{ font-family: 'Segoe UI Semibold'; font-size: 17px; }}
QTextBrowser {{ background: transparent; border: none; font-size: 15px; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.18); border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QLineEdit {{ background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.18); border-radius: 14px;
             padding: 11px 16px; font-size: 15px; selection-background-color: {ACCENT}; }}
QLineEdit:focus {{ border-color: rgba(127,230,255,0.7); background: rgba(255,255,255,0.10); }}
QFrame#chip {{ background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.14); border-radius: 16px; }}
QFrame#row {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; }}
QFrame#card {{ background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.14); border-radius: 18px; }}
QPushButton {{ border-radius: 12px; padding: 12px 34px; font-family: 'Bahnschrift SemiBold'; font-size: 16px;
               letter-spacing: 2px; }}
QPushButton#yes {{ background: {GOOD}; color: #03140c; border: none; }}
QPushButton#yes:hover {{ background: #8fffcf; }}
QPushButton#no {{ background: rgba(255,255,255,0.08); color: {TEXT}; border: 1px solid rgba(255,255,255,0.25); }}
QPushButton#no:hover {{ background: rgba(255,255,255,0.16); }}
QPushButton#mini {{ background: rgba(255,255,255,0.06); color: {DIM}; border: 1px solid rgba(255,255,255,0.14);
                    border-radius: 9px; padding: 3px 10px; font-size: 10px; letter-spacing: 2px; }}
QPushButton#mini:hover {{ color: {TEXT}; background: rgba(255,255,255,0.14); }}
QPushButton#dismiss {{ background: transparent; color: {DIM}; border: none; padding: 0 4px; font-size: 14px;
                       letter-spacing: 0; }}
QPushButton#dismiss:hover {{ color: {BAD}; }}
"""

STATUS_TEXT = {
    "idle": ("READY", ACCENT), "listening": ("LISTENING", GOOD), "thinking": ("THINKING", WARN),
    "speaking": ("SPEAKING", ACCENT), "confirming": ("NEEDS YOUR OK", BAD), "offline": ("VOICE OFF", DIM),
}


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()           # remove now; deleteLater alone leaves it drawn
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _label(text: str, name: str = "item", color: str = "", wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName(name)
    lab.setWordWrap(wrap)
    if color:
        lab.setStyleSheet(f"color: {color};")
    return lab


# ---- engine -> Qt bridge (engine events arrive on background threads) ----------------
class Bridge(QObject, Events):
    sig_heard = Signal(str)
    sig_reply_start = Signal()
    sig_reply_text = Signal(str)
    sig_tool = Signal(str, object, object, bool)
    sig_reply_end = Signal(bool, object)
    sig_notice = Signal(str)
    sig_error = Signal(str)
    sig_confirm = Signal(str)
    sig_confirm_done = Signal(bool, bool)
    sig_panels = Signal(object)
    sig_info = Signal(str)        # status lines from other parts (phone link)
    sig_screen = Signal(str, object)  # Fafnir's screen tools: (action, payload)

    def heard(self, text): self.sig_heard.emit(text)
    def reply_start(self): self.sig_reply_start.emit()
    def reply_text(self, delta): self.sig_reply_text.emit(delta)
    def tool(self, name, args, result, is_error): self.sig_tool.emit(name, args, result, is_error)
    def reply_end(self, interrupted, stats): self.sig_reply_end.emit(interrupted, stats)
    def notice(self, text): self.sig_notice.emit(text)
    def error(self, text): self.sig_error.emit(text)
    def confirm_request(self, action): self.sig_confirm.emit(action)
    def confirm_done(self, approved, timed_out): self.sig_confirm_done.emit(approved, timed_out)


# ---- panels ----------------------------------------------------------------------------
class Panel(GlassFrame):
    def __init__(self, title: str, key: str = ""):
        super().__init__()
        self.key = key
        # panels share whatever height there is (extra rows are clipped, never pushing the
        # window taller than the screen)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        self.setMinimumHeight(90)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        self.header = QHBoxLayout()
        self.title = _label(title, "panelTitle", wrap=False)
        self.header.addWidget(self.title)
        self.header.addStretch(1)
        lay.addLayout(self.header)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        lay.addLayout(self.body)
        lay.addStretch(1)

    def set_items(self, items: list[tuple[str, str, str]], empty: str = "Nothing here.", limit: int = 6) -> None:
        """items: (main text, small text, colour or '')"""
        _clear(self.body)
        if not items:
            items = [(empty, "", DIM)]
        for main, small, color in items[:limit]:
            row = QWidget()
            v = QVBoxLayout(row)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(1)
            v.addWidget(_label(main, "item", color))
            if small:
                v.addWidget(_label(small, "itemDim"))
            self.body.addWidget(row)
        if len(items) > limit:
            self.body.addWidget(_label(f"+ {len(items) - limit} more", "itemDim"))


class NoticesPanel(Panel):
    """Held notices from the heartbeat, each dismissible, plus Clear and Pause buttons."""

    URGENCY_COLOR = {"urgent": BAD, "important": WARN, "info": ""}

    def __init__(self, on_dismiss, on_toggle_pause):
        super().__init__("NOTICES", "notices")
        self.on_dismiss = on_dismiss
        self.clear_btn = QPushButton("CLEAR")
        self.pause_btn = QPushButton("PAUSE")
        for b in (self.clear_btn, self.pause_btn):
            b.setObjectName("mini")
            b.setCursor(Qt.PointingHandCursor)
            self.header.addWidget(b)
        self.clear_btn.clicked.connect(lambda: on_dismiss("all"))
        self.pause_btn.clicked.connect(on_toggle_pause)
        self._shown: tuple = ()

    def set_paused(self, paused: bool, enabled: bool = True) -> None:
        self.pause_btn.setVisible(enabled)
        self.pause_btn.setText("RESUME" if paused else "PAUSE")
        self.title.setText("NOTICES · PAUSED" if paused else "NOTICES")
        self.title.setStyleSheet(f"color: {DIM};" if paused else "")

    def set_notices(self, notices) -> None:
        sig = tuple((n.id, n.title) for n in notices)
        if sig == self._shown:
            return
        self._shown = sig
        _clear(self.body)
        self.clear_btn.setVisible(len(notices) > 1)
        if not notices:
            self.body.addWidget(_label("All clear.", "itemDim"))
            return
        for n in notices[:5]:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(_label(n.title, "item", self.URGENCY_COLOR.get(n.urgency, "")))
            when = n.created[11:16]
            col.addWidget(_label(f"{when} · {n.detail}" if n.detail else when, "itemDim"))
            h.addLayout(col, 1)
            x = QPushButton("✕")
            x.setObjectName("dismiss")
            x.setCursor(Qt.PointingHandCursor)
            x.setToolTip("Dismiss")
            x.clicked.connect(lambda _=False, nid=n.id: self.on_dismiss(nid))
            h.addWidget(x, 0, Qt.AlignTop)
            self.body.addWidget(row)
        if len(notices) > 5:
            self.body.addWidget(_label(f"+ {len(notices) - 5} more (ask “what did I miss?”)", "itemDim"))


class RightNow(GlassFrame):
    """The strip at the top: a one-line briefing and the most important things, ranked."""

    def __init__(self):
        super().__init__(radius=26, tint=QColor(255, 255, 255, 26))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(26, 18, 22, 18)
        lay.setSpacing(22)
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(_label("RIGHT NOW", "panelTitle", wrap=False))
        self.briefing = _label("", "briefing")
        self.briefing.setMinimumWidth(380)
        left.addWidget(self.briefing)
        left.addStretch(1)
        lay.addLayout(left, 4)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(12)
        lay.addLayout(self.chips, 7)

    def set(self, briefing: str, items: list[dict]) -> None:
        self.briefing.setText(briefing)
        _clear(self.chips)
        if not items:
            self.chips.addStretch(1)
            return
        for it in items[:5]:
            chip = QFrame()
            chip.setObjectName("chip")
            color = TAG_COLOR.get(it["tag"], TEXT)
            chip.setStyleSheet(f"QFrame#chip {{ border-top: 2px solid {color}; }}")
            v = QVBoxLayout(chip)
            v.setContentsMargins(14, 10, 14, 10)
            v.setSpacing(2)
            v.addWidget(_label(f"{it['icon']}  {it['title']}", "item", color if it["tag"] in ("urgent", "important") else ""))
            if it.get("detail"):
                v.addWidget(_label(it["detail"], "itemDim"))
            v.addStretch(1)
            chip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.chips.addWidget(chip, 1)


class Stage(GlassFrame):
    """The centre stage: whatever Fafnir puts on screen."""

    def __init__(self, on_close):
        super().__init__(radius=28, tint=QColor(255, 255, 255, 22))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(30, 24, 26, 24)
        lay.setSpacing(12)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.kicker = _label("ON SCREEN", "panelTitle", wrap=False)
        self.title = _label("", "stageTitle")
        self.subtitle = _label("", "itemDim")
        titles.addWidget(self.kicker)
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        close = QPushButton("CLOSE")
        close.setObjectName("mini")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(on_close)
        head.addWidget(close, 0, Qt.AlignTop)
        lay.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        lay.addWidget(self.scroll, 1)

    def show_content(self, p: dict) -> None:
        self.title.setText(p.get("title", ""))
        self.subtitle.setText(p.get("subtitle", ""))
        self.subtitle.setVisible(bool(p.get("subtitle")))
        body = QWidget()
        kind, items = p.get("kind", "list"), p.get("items", [])
        if kind == "text":
            v = QVBoxLayout(body)
            tb = QTextBrowser()
            tb.setOpenExternalLinks(True)
            tb.setMarkdown(p.get("text", ""))
            tb.setStyleSheet("font-size: 17px;")
            v.addWidget(tb)
        elif kind == "cards":
            g = QGridLayout(body)
            g.setSpacing(14)
            cols = 3 if len(items) > 4 else 2
            for i, it in enumerate(items):
                card = QFrame()
                card.setObjectName("card")
                c = QVBoxLayout(card)
                c.setContentsMargins(18, 14, 18, 14)
                c.addWidget(_label(it.get("title", ""), "cardTitle", TAG_COLOR.get(it.get("tag", ""), "")))
                for k in ("meta", "subtitle", "body"):
                    if it.get(k):
                        c.addWidget(_label(it[k], "itemDim" if k != "body" else "item"))
                c.addStretch(1)
                g.addWidget(card, i // cols, i % cols)
            g.setRowStretch(len(items) // cols + 1, 1)
        elif kind == "table":
            g = QGridLayout(body)
            g.setHorizontalSpacing(24)
            g.setVerticalSpacing(8)
            for j, col in enumerate(p.get("columns", [])):
                g.addWidget(_label(col.upper(), "panelTitle"), 0, j)
            for i, row in enumerate(items, start=1):
                cells = row if isinstance(row, list) else [row.get("title", ""), row.get("subtitle", ""), row.get("meta", "")]
                for j, cell in enumerate(cells):
                    g.addWidget(_label(cell, "item"), i, j)
            g.setRowStretch(len(items) + 1, 1)
        else:  # list
            v = QVBoxLayout(body)
            v.setSpacing(8)
            for it in items:
                row = QFrame()
                row.setObjectName("row")
                h = QHBoxLayout(row)
                h.setContentsMargins(16, 10, 16, 10)
                col = QVBoxLayout()
                col.setSpacing(1)
                col.addWidget(_label(it.get("title", ""), "item", TAG_COLOR.get(it.get("tag", ""), "")))
                if it.get("subtitle") or it.get("body"):
                    col.addWidget(_label(it.get("subtitle") or it.get("body"), "itemDim"))
                h.addLayout(col, 1)
                if it.get("meta"):
                    h.addWidget(_label(it["meta"], "itemDim", wrap=False), 0, Qt.AlignTop)
                v.addWidget(row)
            v.addStretch(1)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        body.setAttribute(Qt.WA_TranslucentBackground)
        self.scroll.setWidget(body)


class CenterArea(QWidget):
    """Holds the orb, status line, caption and the stage. With nothing on stage the orb is
    big in the middle; with content on stage the orb shrinks into the bottom-right corner."""

    def __init__(self, orb: Orb, status: QLabel, caption: QLabel, stage: Stage):
        super().__init__()
        self.orb, self.status, self.caption, self.stage = orb, status, caption, stage
        orb.setMinimumSize(60, 60)  # it shrinks into the corner when the stage is showing
        for w in (orb, status, caption, stage):
            w.setParent(self)
        stage.hide()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def resizeEvent(self, _event) -> None:
        self.relayout()

    def relayout(self) -> None:
        w, h = self.width(), self.height()
        if self.stage.isVisible():
            size = 104  # the orb sits under the stage, beside the status line
            self.stage.setGeometry(0, 0, w, h - size)
            self.orb.setGeometry(w // 2 - size - 70, h - size, size, size)
            self.status.setGeometry(w // 2 - 60, h - size // 2 - 18, w // 2 + 60, 36)
            self.status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.caption.hide()
        else:
            cap_h = 64
            orb_h = max(160, h - 40 - cap_h)
            self.orb.setGeometry(0, 0, w, orb_h)
            self.status.setGeometry(0, orb_h, w, 36)
            self.status.setAlignment(Qt.AlignCenter)
            self.caption.setGeometry(20, orb_h + 36, w - 40, cap_h)
            self.caption.show()


class ConfirmOverlay(GlassFrame):
    def __init__(self, parent, on_answer):
        super().__init__(parent, radius=26, tint=QColor(255, 80, 100, 46))
        self.on_answer = on_answer
        lay = QVBoxLayout(self)
        lay.setContentsMargins(36, 28, 36, 28)
        lay.setSpacing(14)
        lay.addWidget(_label(f"{'FAFNIR'} NEEDS YOUR OK", "panelTitle", BAD, wrap=False))
        self.text = _label("", "item")
        self.text.setStyleSheet("font-size: 20px;")
        lay.addWidget(self.text)
        self.detail = _label("", "item")
        self.detail.setStyleSheet(f"color: {DIM}; font-family: Consolas; font-size: 13px;")
        lay.addWidget(self.detail)
        self.hint = _label("", "meta")
        lay.addWidget(self.hint)
        row = QHBoxLayout()
        row.addStretch(1)
        no, yes = QPushButton("NO"), QPushButton("YES, DO IT")
        no.setObjectName("no")
        yes.setObjectName("yes")
        no.clicked.connect(lambda: self.on_answer(False))
        yes.clicked.connect(lambda: self.on_answer(True))
        row.addWidget(no)
        row.addWidget(yes)
        lay.addLayout(row)
        self.hide()

    def ask(self, action: str, talk_key: str) -> None:
        main, _, cmd = action.partition(CONFIRM_MARK)
        self.text.setText(main)
        self.detail.setText(cmd)
        self.detail.setVisible(bool(cmd))
        self.hint.setText(f"Click, or hold {talk_key} and say yes or no. No answer in 60 seconds counts as no.")
        self.setFixedWidth(min(780, int(self.parent().width() * 0.6)))
        self.adjustSize()
        self.move((self.parent().width() - self.width()) // 2, int(self.parent().height() * 0.30))
        self.show()
        self.raise_()


# ---- main window -----------------------------------------------------------------------
class MainWindow(QWidget):
    def __init__(self, cfg, app_cfg: dict, engine: Engine, bridge: Bridge, heartbeat=None, notices=None):
        super().__init__()
        self.cfg, self.app_cfg, self.engine, self.bridge = cfg, app_cfg, engine, bridge
        self.heartbeat, self.notices = heartbeat, notices
        self.talk_key = app_cfg.get("talk_key_label", "M5")
        self.setObjectName("root")
        self.setWindowTitle(cfg.assistant.name)
        self.setStyleSheet(STYLE)
        self.activity: list[tuple[str, str, str]] = []
        self.caption_clear_at = 0.0
        self._data: dict = {}
        self._briefing_override: tuple[str, float] | None = None
        self.bg = self.bg_blur = None
        self._bg_size = None
        self._build()
        self._wire()
        self._load_recent_activity()
        self._load_today_conversation()
        self.refresh_panels()

    # background: drawn behind everything; panels show a blurred copy (glass)
    def _ensure_background(self) -> None:
        size = (self.width(), self.height())
        if self._bg_size == size or size[0] < 50:
            return
        self._bg_size = size
        self.bg = glass.load_background(*size, path=str(self.app_cfg.get("background", "")))
        self.bg_blur = glass.blurred(self.bg)

    def resizeEvent(self, event) -> None:
        self._ensure_background()
        super().resizeEvent(event)

    def paintEvent(self, _event) -> None:
        self._ensure_background()
        p = QPainter(self)
        if self.bg is not None:
            p.drawPixmap(0, 0, self.bg)
        p.end()

    # layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(34, 22, 34, 24)
        root.setSpacing(16)

        top = QHBoxLayout()
        brand = _label(self.cfg.assistant.name.upper(), "brand", wrap=False)
        top.addWidget(brand)
        top.addSpacing(22)
        self.meta = _label("", "meta", wrap=False)
        top.addWidget(self.meta, 0, Qt.AlignBottom)
        top.addStretch(1)
        clock_box = QVBoxLayout()
        clock_box.setSpacing(0)
        self.clock = _label("", "clock", wrap=False)
        self.clock.setAlignment(Qt.AlignRight)
        self.date = _label("", "date", wrap=False)
        self.date.setAlignment(Qt.AlignRight)
        clock_box.addWidget(self.clock)
        clock_box.addWidget(self.date)
        top.addLayout(clock_box)
        root.addLayout(top)

        self.right_now = RightNow()
        root.addWidget(self.right_now)

        mid = QHBoxLayout()
        mid.setSpacing(20)
        self.left_col = QVBoxLayout()
        self.left_col.setSpacing(16)
        self.right_col = QVBoxLayout()
        self.right_col.setSpacing(16)
        self.panels = {
            "today": Panel("TODAY", "today"),
            "tasks": Panel("TASKS", "tasks"),
            "notices": NoticesPanel(self.dismiss_notice, self.toggle_pause),
            "email": Panel("EMAIL", "email"),
            "business": Panel("BUSINESS & SOCIAL", "business"),
            "activity": Panel("ACTIVITY", "activity"),
        }
        self.p_today, self.p_tasks = self.panels["today"], self.panels["tasks"]
        self.p_notices, self.p_activity = self.panels["notices"], self.panels["activity"]
        self.p_email, self.p_business = self.panels["email"], self.panels["business"]
        left_w, right_w = QWidget(), QWidget()
        left_w.setLayout(self.left_col)
        right_w.setLayout(self.right_col)
        for w in (left_w, right_w):
            w.setMinimumWidth(330)
            w.setMaximumWidth(460)
        self.left_col.setContentsMargins(0, 0, 0, 0)
        self.right_col.setContentsMargins(0, 0, 0, 0)

        self.orb = Orb(self.engine)
        self.status = _label("", "status", wrap=False)
        self.status.setAlignment(Qt.AlignCenter)
        self.caption = _label("", "caption")
        self.caption.setAlignment(Qt.AlignCenter)
        self.stage = Stage(self.clear_stage)
        self.center = CenterArea(self.orb, self.status, self.caption, self.stage)

        mid.addWidget(left_w, 3)
        mid.addWidget(self.center, 5)
        mid.addWidget(right_w, 3)
        root.addLayout(mid, 6)

        self.convo = GlassFrame(radius=24)
        cl = QVBoxLayout(self.convo)
        cl.setContentsMargins(22, 12, 22, 14)
        cl.setSpacing(8)
        self.convo_title = _label("CONVERSATION", "panelTitle", wrap=False)
        cl.addWidget(self.convo_title)
        self.log = QTextBrowser()
        self.log.setOpenExternalLinks(True)
        self.log.viewport().setAutoFillBackground(False)
        cl.addWidget(self.log, 1)
        self.input = QLineEdit()
        self.input.setPlaceholderText(f"Type to {self.cfg.assistant.name}, or hold {self.talk_key} to talk")
        cl.addWidget(self.input)
        self.convo.setMinimumHeight(220)
        root.addWidget(self.convo, 3)
        self.root_layout = root
        QTimer.singleShot(200, self._scroll_log_to_end)

        self.confirm = ConfirmOverlay(self, self._answer)
        self.apply_layout(SCREEN.layout)

    # Fafnir's control of the screen ------------------------------------------------
    def on_screen(self, action: str, payload: dict) -> None:
        if action == "show":
            self.stage.show_content(payload)
            self.stage.show()
            self.center.relayout()
            self._append_text(f"🖥  On screen: {payload.get('title', '')}", DIM)
        elif action == "clear":
            self.clear_stage()
        elif action == "layout":
            self.apply_layout(SCREEN.layout)
        elif action == "briefing":
            self._briefing_override = (payload.get("text", ""), time.monotonic() + 30 * 60)
            self.update_right_now()

    def clear_stage(self) -> None:
        self.stage.hide()
        SCREEN.showing = ""
        self.center.relayout()

    def apply_layout(self, layout: dict) -> None:
        order = [k for k in layout.get("order") or PANELS if k in self.panels]
        order += [k for k in PANELS if k not in order]
        hidden = set(layout.get("hidden") or [])
        visible = [k for k in order if k not in hidden]
        for col in (self.left_col, self.right_col):
            while col.count():
                col.takeAt(0)
        half = (len(visible) + 1) // 2
        for i, k in enumerate(visible):
            (self.left_col if i < half else self.right_col).addWidget(self.panels[k], 1)
            self.panels[k].show()
        for k in hidden:
            self.panels[k].hide()
        mode = layout.get("conversation") or "show"
        self.convo.setVisible(True)  # the type box always stays
        self.log.setVisible(mode != "hide")
        self.convo_title.setVisible(mode != "hide")
        self.log.setMaximumHeight(16777215 if mode == "show" else 70)
        self.convo.setMinimumHeight({"show": 220, "small": 140}.get(mode, 0))
        self.root_layout.setStretchFactor(self.convo, 3 if mode == "show" else 0)
        QTimer.singleShot(50, self._scroll_log_to_end)

    def _scroll_log_to_end(self) -> None:
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def update_right_now(self) -> None:
        d = dict(self._data)
        d["notices"] = self.notices.active() if self.notices is not None else []
        vips = []
        if self.heartbeat is not None:
            vips = self.heartbeat.settings.get("email", {}).get("vip_senders", [])
        briefing, items = rank(d, vips)
        if self._briefing_override and time.monotonic() < self._briefing_override[1]:
            briefing = self._briefing_override[0]
        self.right_now.set(briefing, items)

    def _wire(self) -> None:
        b = self.bridge
        b.sig_heard.connect(self.on_heard)
        b.sig_reply_start.connect(self.on_reply_start)
        b.sig_reply_text.connect(self.on_reply_text)
        b.sig_tool.connect(self.on_tool)
        b.sig_reply_end.connect(self.on_reply_end)
        b.sig_notice.connect(lambda t: self.set_caption(t, DIM, 4))
        b.sig_error.connect(self.on_error)
        b.sig_confirm.connect(lambda a: self.confirm.ask(a, self.talk_key))
        b.sig_confirm_done.connect(self.on_confirm_done)
        b.sig_panels.connect(self.on_panels)
        b.sig_info.connect(lambda t: (self._append_text(f"📱 {t}", DIM), print(f"[phone] {t}", flush=True)))
        b.sig_screen.connect(self.on_screen)
        self.input.returnPressed.connect(self.on_enter)
        QShortcut(QKeySequence("Escape"), self, activated=self.engine.interrupt)
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)

        self.tick = QTimer(self)
        self.tick.timeout.connect(self.on_tick)
        self.tick.start(250)
        self.panel_timer = QTimer(self)
        self.panel_timer.timeout.connect(self.refresh_panels)
        self.panel_timer.start(5 * 60 * 1000)
        self.notice_timer = QTimer(self)
        self.notice_timer.timeout.connect(self.on_notices_tick)
        self.notice_timer.start(3000)
        self.on_notices_tick()

    # heartbeat notices: shown in the panel; important ones spoken when Fafnir is free
    def on_notices_tick(self) -> None:
        if self.notices is None:
            self.p_notices.set_notices([])
            self.p_notices.set_paused(False, enabled=False)
            self.update_right_now()
            return
        active = self.notices.active()
        self.p_notices.set_notices(active)
        paused = bool(self.heartbeat and self.heartbeat.paused)
        self.p_notices.set_paused(paused, enabled=self.heartbeat is not None)
        self.update_right_now()
        if paused or self.engine.state != "idle":
            return
        for n in active:
            if n.announced or n.urgency not in ("important", "urgent"):
                continue
            if n.urgency != "urgent" and _in_quiet_hours(self.heartbeat.settings.get("quiet_hours", "") if self.heartbeat else ""):
                continue  # held silently in the panel until morning
            self.notices.mark_announced(n.id)
            self.engine.announce(f"Heads up. {n.title}.")
            self.set_caption(n.title, WARN if n.urgency == "important" else BAD, 10)
            break  # one at a time; the next waits until Fafnir is free again

    def dismiss_notice(self, notice_id: str) -> None:
        if self.notices is not None:
            self.notices.dismiss(notice_id)
        self.on_notices_tick()

    def toggle_pause(self) -> None:
        if self.heartbeat is None:
            return
        if self.heartbeat.paused:
            self.heartbeat.resume()
        else:
            self.heartbeat.pause(0)
            self.engine.interrupt()
        self.on_notices_tick()

    # conversation log: each speaker label, message and tool line is its own paragraph
    def _cursor(self) -> QTextCursor:
        c = self.log.textCursor()
        c.movePosition(QTextCursor.End)
        return c

    def _new_line(self, space_before: float = 0) -> QTextCursor:
        c = self._cursor()
        if not self.log.document().isEmpty():
            from PySide6.QtGui import QTextBlockFormat
            bf = QTextBlockFormat()
            bf.setTopMargin(space_before)
            c.insertBlock(bf)
        return c

    def _write(self, c: QTextCursor, s: str, color: str = TEXT, small: bool = False) -> None:
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if small:
            f.setFontFamilies(["Bahnschrift"])
            f.setFontPointSize(9)
            f.setFontLetterSpacing(130)
        c.insertText(s, f)
        self.log.setTextCursor(c)
        self.log.ensureCursorVisible()

    def _append_text(self, s: str, color: str = TEXT) -> None:
        """A standalone line (notices, errors)."""
        self._write(self._new_line(6), s.strip("\n"), color)

    def _speaker(self, who: str, color: str) -> None:
        self._write(self._new_line(14), who, color, small=True)

    def add_user(self, text: str, spoken: bool) -> None:
        self._speaker("YOU" + (" · SPOKEN" if spoken else ""), DIM)
        self._write(self._new_line(), text)

    def set_caption(self, text: str, color: str = TEXT, seconds: float = 0) -> None:
        self.caption.setText(html.escape(text))
        self.caption.setStyleSheet(f"color: {color};")
        self.caption_clear_at = time.monotonic() + seconds if seconds else 0

    # engine events
    def on_heard(self, text: str) -> None:
        self.add_user(text, spoken=True)
        self.set_caption(f"“{text}”", TEXT)

    def on_reply_start(self) -> None:
        self._speaker(self.cfg.assistant.name.upper(), ACCENT)
        self._reply_line = None  # paragraph the streamed reply is going into

    def on_reply_text(self, delta: str) -> None:
        if self._reply_line is None:
            delta = delta.lstrip("\n")
            if not delta:
                return
            self._reply_line = self._new_line()
        self._write(self._cursor(), delta)

    def on_tool(self, name, args, result, is_error) -> None:
        label = name.replace("_", " ")
        first = next((str(v) for v in (args or {}).values() if isinstance(v, (str, list)) and v), "")
        if isinstance(first, str) and len(first) > 48:
            first = first[:45] + "..."
        if result is None:
            line, color = f"{label} (web)", ACCENT
        elif is_error:
            line, color = f"✗ {label}", BAD
        else:
            line, color = f"✓ {label}", GOOD
        self.activity.insert(0, (line, f"{datetime.now():%H:%M} · {first}" if first else f"{datetime.now():%H:%M}", color))
        self.activity = self.activity[:8]
        self.p_activity.set_items(self.activity, limit=5)
        self._write(self._new_line(4), f"    {line}{(' · ' + first) if first else ''}", color if is_error else DIM)
        self._reply_line = None  # text after a tool starts a fresh paragraph

    def on_reply_end(self, interrupted: bool, stats: TurnStats) -> None:
        if interrupted:
            self._write(self._cursor(), "  [interrupted]", DIM)
        if stats.cost and self.cfg.model.show_cost:
            self.meta.setText(f"{self.cfg.model.name} · last reply {stats.cost.removeprefix('cost ')}")
        self.set_caption("", TEXT)
        self.refresh_panels()

    def on_error(self, text: str) -> None:
        self._append_text(f"\n[{text}]\n", BAD)
        self.set_caption(text, BAD, 8)

    def on_confirm_done(self, approved: bool, timed_out: bool) -> None:
        self.confirm.hide()
        msg = "Approved." if approved else ("No answer, so I didn't do it." if timed_out else "Declined.")
        self._append_text(f"\n  {msg}\n", GOOD if approved else DIM)

    def _answer(self, approved: bool) -> None:
        self.engine.answer_confirm(approved)

    def on_enter(self) -> None:
        text = self.input.text().strip()
        self.input.clear()
        if not text:
            return
        if text == "/reset":
            self.engine.reset()
            self.log.clear()
            self._append_text("New conversation. Memory reloaded.\n", DIM)
            return
        if self.engine.pending_confirm is None:
            self.add_user(text, spoken=False)
        self.engine.submit_text(text)

    def on_tick(self) -> None:
        now = datetime.now()
        self.clock.setText(now.strftime("%I:%M %p").lstrip("0"))
        self.date.setText(now.strftime("%A %d %B").replace(" 0", " "))
        state = self.engine.state
        if state == "idle":
            if not self.engine.voice:
                text, color = "READY · TYPE BELOW (VOICE OFF)", DIM
            elif self.engine.wake:
                text, color = f"READY · SAY “{self.engine.wake.name.upper()}” OR HOLD {self.talk_key}", ACCENT
            else:
                text, color = f"READY · HOLD {self.talk_key} TO TALK", ACCENT
        else:
            text, color = STATUS_TEXT.get(state, STATUS_TEXT["idle"])
        if self.stage.isVisible() and state == "idle":
            text = "READY"
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
        if self.caption_clear_at and time.monotonic() > self.caption_clear_at:
            self.set_caption("")
        if not self.meta.text():
            self.meta.setText(self.cfg.model.name)

    # side panels and the Right-now strip (data loaded off the UI thread)
    def refresh_panels(self) -> None:
        threading.Thread(target=self._load_panels, daemon=True).start()

    def _load_panels(self) -> None:
        reg, out = self.engine.agent.registry, {}

        def get(key, tool, **args):
            if tool not in reg.names():
                return None
            try:
                return reg.read(tool, **args)
            except Exception as e:
                out[f"{key}_error"] = str(e)[:120]
                return None

        data = get("tasks", "business_list_items", status="open")
        if data is not None:
            out["tasks"] = data.get("items", [])
        if (ROOT / "data" / "google_token.json").exists():
            today = date.today()
            ev = get("events", "calendar_list_events", start=today.isoformat(),
                     end=(today + timedelta(days=1)).isoformat(), max_results=20)
            if ev is not None:
                out["events"] = ev.get("events", [])
            gt = get("gtasks", "google_tasks_list")
            if gt is not None:
                out["gtasks"] = gt.get("tasks", [])
            mail = get("emails", "gmail_search", query="is:unread is:important newer_than:2d", max_results=8)
            if mail is not None:
                out["emails"] = mail.get("emails", [])
            yt = get("youtube", "youtube_channel")
            if yt is not None and yt.get("channel"):
                out["youtube"] = yt["channel"]
        else:
            out["google_missing"] = True
        self.bridge.sig_panels.emit(out)

    def on_panels(self, d: dict) -> None:
        self._data = d
        today = date.today()
        if d.get("google_missing"):
            msg = "Google isn't connected yet. Ask “what's on my calendar?” to connect."
            for p in (self.p_today, self.p_email):
                p.set_items([(msg, "", DIM)])
        if "events" in d:
            items = []
            for e in d["events"]:
                s = e.get("start", "")
                tag = "" if s[:10] == today.isoformat() else "Tomorrow · "
                if e.get("all_day"):
                    when = f"{tag}All day"
                else:
                    try:
                        when = tag + datetime.fromisoformat(s).strftime("%I:%M %p").lstrip("0")
                    except ValueError:
                        when = s
                past = not e.get("all_day") and s and datetime.fromisoformat(s) < datetime.now().astimezone()
                items.append((e.get("title", ""), when + (f" · {e['location']}" if e.get("location") else ""),
                              DIM if past else ""))
            self.p_today.set_items(items, "Nothing on today or tomorrow.")
        elif d.get("events_error"):
            self.p_today.set_items([(f"Couldn't load the calendar ({d['events_error']}). Retrying soon.", "", DIM)])
        tasks = []
        for t in d.get("tasks", []) + [dict(g, kind="Google Tasks") for g in d.get("gtasks", []) if not g.get("done")]:
            due, color, small = (t.get("due") or "")[:10], "", t.get("kind", "")
            if due:
                if due < today.isoformat():
                    color, small = BAD, f"overdue · {due}"
                elif due == today.isoformat():
                    color, small = WARN, "due today"
                else:
                    small = f"due {due}"
            tasks.append((t.get("title", ""), small, color))
        if "tasks" in d or "gtasks" in d:
            self.p_tasks.set_items(tasks, "No open tasks.")
        if "emails" in d:
            mails = []
            for m in d["emails"]:
                sender = (m.get("from") or "").split("<")[0].strip().strip('"') or m.get("from", "")
                mails.append((m.get("subject") or "(no subject)", sender, ""))
            self.p_email.set_items(mails, "No unread important email.")
        elif d.get("emails_error"):
            self.p_email.set_items([(f"Couldn't load email ({d['emails_error']}).", "", DIM)])
        biz = []
        yt = d.get("youtube")
        if yt:
            stats = []
            for k, label in (("subscribers", "subscribers"), ("views", "views"), ("videos", "videos")):
                if str(yt.get(k, "")).isdigit():
                    stats.append(f"{int(yt[k]):,} {label}")
            biz.append((f"▶  {yt.get('title', 'YouTube')}", " · ".join(stats), ""))
        open_items = [t for t in d.get("tasks", []) if t.get("kind") in ("client", "deadline", "note")]
        for t in open_items[:3]:
            biz.append((f"💼  {t.get('title', '')}", t.get("kind", ""), ""))
        self.p_business.set_items(biz, "Business and social stats will show here.")
        self.update_right_now()

    def _load_today_conversation(self) -> None:
        """Reopen with today's conversation so far (Fafnir remembers it too)."""
        try:
            from ..tools import CONVERSATIONS
            entries = CONVERSATIONS.today()[-40:]
        except Exception:
            entries = []
        if not entries:
            return
        self._write(self._new_line(), "EARLIER TODAY", DIM, small=True)
        for e in entries:
            who = "YOU" if e["role"] == "user" else self.cfg.assistant.name.upper()
            self._speaker(f"{who} · {e['time'][11:16]}", DIM if e["role"] == "user" else ACCENT)
            self._write(self._new_line(), e["text"], DIM)
        self._write(self._new_line(14), "─" * 24 + "  now  " + "─" * 24, C_LINE)

    def _load_recent_activity(self) -> None:
        path = ROOT / "logs" / "actions.log"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-8:]
        except OSError:
            lines = []
        for raw in reversed(lines):
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            ok = not e.get("error")
            when = e.get("time", "")[11:16]
            self.activity.append((("✓ " if ok else "✗ ") + e.get("tool", "").replace("_", " "),
                                  when, GOOD if ok else BAD))
        self.p_activity.set_items(self.activity, "Nothing yet.", limit=5)

    # window
    def toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def closeEvent(self, event) -> None:
        SCREEN.attach(None)
        self.engine.stop()
        super().closeEvent(event)


# ---- startup ---------------------------------------------------------------------------
def _in_quiet_hours(spec: str) -> bool:
    """spec like "23:00-07:00"; empty means never quiet."""
    try:
        a, b = (datetime.strptime(x.strip(), "%H:%M").time() for x in spec.split("-"))
    except ValueError:
        return False
    now = datetime.now().time()
    return (a <= now < b) if a < b else (now >= a or now < b)


def _app_config() -> dict:
    try:
        with open(ROOT / "config.toml", "rb") as f:
            return tomllib.load(f).get("app", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def pick_screen(app: QApplication, spec: str):
    screens, primary = app.screens(), app.primaryScreen()
    pg = primary.geometry()
    spec = (spec or "right").lower()
    if spec == "primary":
        return primary
    if spec == "right":
        cands = [s for s in screens if s.geometry().left() >= pg.right()]
        return min(cands, key=lambda s: s.geometry().left()) if cands else primary
    if spec == "left":
        cands = [s for s in screens if s.geometry().right() <= pg.left()]
        return max(cands, key=lambda s: s.geometry().right()) if cands else primary
    if spec == "above":
        cands = [s for s in screens if s.geometry().bottom() <= pg.top()]
        return max(cands, key=lambda s: s.geometry().bottom()) if cands else primary
    for s in screens:  # a name like "DISPLAY2"
        if spec in s.name().lower():
            return s
    return primary


def _redirect_output_if_windowless() -> None:
    """pythonw (no console) has no stdout; send prints and errors to logs/app.log."""
    if sys.stdout is None or sys.stderr is None:
        (ROOT / "logs").mkdir(exist_ok=True)
        f = open(ROOT / "logs" / "app.log", "a", encoding="utf-8", buffering=1)
        f.write(f"\n--- started {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


def run_app(argv: list[str]) -> int:
    _redirect_output_if_windowless()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Fafnir")
    app.setFont(QFont("Segoe UI", 11))

    # only one Fafnir at a time: a second launch just brings the first to the front
    probe = QLocalSocket()
    probe.connectToServer(INSTANCE_KEY)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(INSTANCE_KEY)
    server = QLocalServer()
    server.listen(INSTANCE_KEY)

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Setup problem: {e}")
        return 1
    app_cfg = _app_config()
    agent = build_agent(cfg)
    bridge = Bridge()
    try:
        engine = Engine(cfg, agent, bridge, voice=True)
    except Exception as e:  # bad talk key or audio device: run with typing only
        print(f"Voice unavailable: {e}")
        engine = Engine(cfg, agent, bridge, voice=False)
    from ..heartbeat.checks import default_checks
    from ..heartbeat.runner import Heartbeat, load_settings
    from ..tools import HEARTBEAT, NOTICES
    heartbeat = Heartbeat(agent.registry, NOTICES, default_checks(load_settings()))
    HEARTBEAT["hb"] = heartbeat
    win = MainWindow(cfg, app_cfg, engine, bridge, heartbeat=heartbeat, notices=NOTICES)
    SCREEN.attach(lambda action, payload: bridge.sig_screen.emit(action, payload))  # Fafnir's screen tools
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win.showFullScreen(),
                                          win.raise_(), win.activateWindow()))
    if not engine.voice:
        missing = cfg.voice.missing_keys()
        win._append_text(
            ("Voice is off: add " + ", ".join(missing) + " to .env.\n") if missing
            else "Voice is off (couldn't start the microphone, speaker or talk key). Typing still works.\n", C_DIM)

    screen = pick_screen(app, str(app_cfg.get("screen", "right")))
    win.setGeometry(screen.geometry())
    win.show()
    if win.windowHandle():
        win.windowHandle().setScreen(screen)
    win.setGeometry(screen.geometry())
    if app_cfg.get("fullscreen", True) and "--windowed" not in argv:
        win.showFullScreen()
    engine.start()
    heartbeat.start()
    # phone links: Discord now, WhatsApp once Meta approves the business (either may be unset)
    from .. import discord_link, phone as whatsapp
    for make in (discord_link.make_bridge, whatsapp.make_bridge):
        try:
            link = make(engine, NOTICES, on_status=bridge.sig_info.emit, name=cfg.assistant.name)
            if link is not None:
                link.start()
        except Exception as e:
            bridge.sig_info.emit(f"Phone link failed to start: {e}")
    return app.exec()
