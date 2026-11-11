"""Fafnir's desktop app: a fullscreen dark HUD on a chosen monitor.

Layout: today's calendar and open tasks on the left, the animated core in the middle,
notices and recent activity on the right, the conversation and a type box along the bottom.
Hold the talk key (M5) anywhere to speak; confirmations appear as Yes/No buttons and can
also be answered by voice.

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
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ..common import build_agent
from ..config import ROOT, ConfigError, load_config
from ..engine import Engine, Events, TurnStats
from ..tools.control import CONFIRM_MARK
from .orb import Orb

INSTANCE_KEY = "FafnirDesktopApp"

C_BG, C_PANEL, C_LINE = "#04070c", "#0a1119", "#143049"
C_TEXT, C_DIM, C_ACCENT = "#d3e9f7", "#5f7d93", "#3fd0ff"
C_GOOD, C_WARN, C_BAD = "#3dffa8", "#ffb347", "#ff5f6d"

STYLE = f"""
QWidget#root {{ background: {C_BG}; }}
QWidget {{ color: {C_TEXT}; font-family: 'Segoe UI'; font-size: 15px; }}
QFrame#panel {{ background: {C_PANEL}; border: 1px solid {C_LINE}; border-radius: 14px; }}
QLabel#panelTitle {{ color: {C_ACCENT}; font-family: 'Bahnschrift SemiBold'; font-size: 13px;
                     letter-spacing: 3px; }}
QLabel#brand {{ color: {C_ACCENT}; font-family: 'Bahnschrift SemiBold'; font-size: 26px; letter-spacing: 10px; }}
QLabel#clock {{ font-family: 'Bahnschrift Light'; font-size: 26px; color: {C_TEXT}; }}
QLabel#date, QLabel#meta {{ color: {C_DIM}; font-size: 14px; }}
QLabel#status {{ font-family: 'Bahnschrift'; font-size: 20px; letter-spacing: 4px; }}
QLabel#caption {{ color: {C_TEXT}; font-size: 19px; }}
QLabel#item {{ font-size: 15px; }}
QLabel#itemDim {{ color: {C_DIM}; font-size: 13px; }}
QTextBrowser {{ background: transparent; border: none; font-size: 16px; }}
QLineEdit {{ background: #070d14; border: 1px solid {C_LINE}; border-radius: 10px; padding: 10px 14px;
             font-size: 16px; selection-background-color: {C_ACCENT}; }}
QLineEdit:focus {{ border-color: {C_ACCENT}; }}
QFrame#confirm {{ background: #10070a; border: 2px solid {C_BAD}; border-radius: 18px; }}
QPushButton {{ border-radius: 10px; padding: 12px 34px; font-family: 'Bahnschrift SemiBold';
               font-size: 17px; letter-spacing: 2px; }}
QPushButton#yes {{ background: {C_GOOD}; color: #02130b; border: none; }}
QPushButton#no {{ background: transparent; color: {C_TEXT}; border: 1px solid {C_DIM}; }}
QPushButton#mini {{ background: transparent; color: {C_DIM}; border: 1px solid {C_LINE}; border-radius: 7px;
                    padding: 3px 10px; font-size: 11px; letter-spacing: 2px; }}
QPushButton#mini:hover {{ color: {C_TEXT}; border-color: {C_DIM}; }}
QPushButton#dismiss {{ background: transparent; color: {C_DIM}; border: none; padding: 0 4px; font-size: 14px;
                       letter-spacing: 0; }}
QPushButton#dismiss:hover {{ color: {C_BAD}; }}
QPushButton#yes:hover {{ background: #7dffc6; }}
QPushButton#no:hover {{ border-color: {C_TEXT}; }}
"""

STATUS_TEXT = {
    "idle": ("READY", C_ACCENT), "listening": ("LISTENING", C_GOOD), "thinking": ("THINKING", C_WARN),
    "speaking": ("SPEAKING", C_ACCENT), "confirming": ("NEEDS YOUR OK", C_BAD), "offline": ("VOICE OFF", C_DIM),
}


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

    def heard(self, text): self.sig_heard.emit(text)
    def reply_start(self): self.sig_reply_start.emit()
    def reply_text(self, delta): self.sig_reply_text.emit(delta)
    def tool(self, name, args, result, is_error): self.sig_tool.emit(name, args, result, is_error)
    def reply_end(self, interrupted, stats): self.sig_reply_end.emit(interrupted, stats)
    def notice(self, text): self.sig_notice.emit(text)
    def error(self, text): self.sig_error.emit(text)
    def confirm_request(self, action): self.sig_confirm.emit(action)
    def confirm_done(self, approved, timed_out): self.sig_confirm_done.emit(approved, timed_out)


# ---- widgets ---------------------------------------------------------------------------
class Panel(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        self.header = QHBoxLayout()
        self.title = QLabel(title)
        self.title.setObjectName("panelTitle")
        self.header.addWidget(self.title)
        self.header.addStretch(1)
        lay.addLayout(self.header)
        self.body = QVBoxLayout()
        self.body.setSpacing(9)
        lay.addLayout(self.body)
        lay.addStretch(1)

    def set_items(self, items: list[tuple[str, str, str]], empty: str = "Nothing here.") -> None:
        """items: (main text, small text, colour or '')"""
        while self.body.count():
            w = self.body.takeAt(0).widget()
            if w:
                w.hide()           # remove now; deleteLater alone leaves it drawn
                w.setParent(None)
                w.deleteLater()
        if not items:
            items = [(empty, "", C_DIM)]
        for main, small, color in items[:9]:
            row = QWidget()
            v = QVBoxLayout(row)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(1)
            a = QLabel(main)
            a.setObjectName("item")
            a.setWordWrap(True)
            if color:
                a.setStyleSheet(f"color: {color};")
            v.addWidget(a)
            if small:
                b = QLabel(small)
                b.setObjectName("itemDim")
                b.setWordWrap(True)
                v.addWidget(b)
            self.body.addWidget(row)


class NoticesPanel(Panel):
    """Held notices from the heartbeat, each dismissible, plus Clear and Pause buttons."""

    URGENCY_COLOR = {"urgent": C_BAD, "important": C_WARN, "info": ""}

    def __init__(self, on_dismiss, on_toggle_pause):
        super().__init__("NOTICES")
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
        self.title.setStyleSheet(f"color: {C_DIM};" if paused else "")

    def set_notices(self, notices) -> None:
        sig = tuple((n.id, n.title) for n in notices)
        if sig == self._shown:
            return
        self._shown = sig
        while self.body.count():
            w = self.body.takeAt(0).widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.clear_btn.setVisible(len(notices) > 1)
        if not notices:
            empty = QLabel("All clear.")
            empty.setObjectName("itemDim")
            self.body.addWidget(empty)
            return
        for n in notices[:7]:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            col = QVBoxLayout()
            col.setSpacing(1)
            a = QLabel(n.title)
            a.setObjectName("item")
            a.setWordWrap(True)
            color = self.URGENCY_COLOR.get(n.urgency, "")
            if color:
                a.setStyleSheet(f"color: {color};")
            col.addWidget(a)
            when = n.created[11:16]
            b = QLabel(f"{when} · {n.detail}" if n.detail else when)
            b.setObjectName("itemDim")
            b.setWordWrap(True)
            col.addWidget(b)
            h.addLayout(col, 1)
            x = QPushButton("✕")
            x.setObjectName("dismiss")
            x.setCursor(Qt.PointingHandCursor)
            x.setToolTip("Dismiss")
            x.clicked.connect(lambda _=False, nid=n.id: self.on_dismiss(nid))
            h.addWidget(x, 0, Qt.AlignTop)
            self.body.addWidget(row)
        if len(notices) > 7:
            more = QLabel(f"+ {len(notices) - 7} more (ask “what did I miss?”)")
            more.setObjectName("itemDim")
            self.body.addWidget(more)


class ConfirmOverlay(QFrame):
    def __init__(self, parent, on_answer):
        super().__init__(parent)
        self.setObjectName("confirm")
        self.on_answer = on_answer
        lay = QVBoxLayout(self)
        lay.setContentsMargins(36, 28, 36, 28)
        lay.setSpacing(14)
        t = QLabel("FAFNIR NEEDS YOUR OK")
        t.setObjectName("panelTitle")
        t.setStyleSheet(f"color: {C_BAD};")
        lay.addWidget(t)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setStyleSheet("font-size: 20px;")
        lay.addWidget(self.text)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color: {C_DIM}; font-family: Consolas; font-size: 13px;")
        lay.addWidget(self.detail)
        self.hint = QLabel()
        self.hint.setObjectName("meta")
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
        self.setFixedWidth(min(760, int(self.parent().width() * 0.6)))
        self.adjustSize()
        self.move((self.parent().width() - self.width()) // 2, int(self.parent().height() * 0.28))
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
        self._build()
        self._wire()
        self._load_recent_activity()
        self._load_today_conversation()
        self.refresh_panels()

    # layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 26, 36, 26)
        root.setSpacing(18)

        top = QHBoxLayout()
        brand = QLabel(self.cfg.assistant.name.upper())
        brand.setObjectName("brand")
        top.addWidget(brand)
        top.addSpacing(24)
        self.meta = QLabel()
        self.meta.setObjectName("meta")
        top.addWidget(self.meta, 0, Qt.AlignBottom)
        top.addStretch(1)
        clock_box = QVBoxLayout()
        clock_box.setSpacing(0)
        self.clock = QLabel()
        self.clock.setObjectName("clock")
        self.clock.setAlignment(Qt.AlignRight)
        self.date = QLabel()
        self.date.setObjectName("date")
        self.date.setAlignment(Qt.AlignRight)
        clock_box.addWidget(self.clock)
        clock_box.addWidget(self.date)
        top.addLayout(clock_box)
        root.addLayout(top)

        mid = QHBoxLayout()
        mid.setSpacing(22)
        left = QVBoxLayout()
        left.setSpacing(18)
        self.p_today = Panel("TODAY")
        self.p_tasks = Panel("TASKS")
        left.addWidget(self.p_today, 3)
        left.addWidget(self.p_tasks, 2)
        mid.addLayout(left, 3)

        center = QVBoxLayout()
        center.setSpacing(6)
        self.orb = Orb(self.engine)
        self.orb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        center.addWidget(self.orb, 1)
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setAlignment(Qt.AlignCenter)
        center.addWidget(self.status)
        self.caption = QLabel()
        self.caption.setObjectName("caption")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)
        self.caption.setMinimumHeight(56)
        center.addWidget(self.caption)
        mid.addLayout(center, 5)

        right = QVBoxLayout()
        right.setSpacing(18)
        self.p_notices = NoticesPanel(self.dismiss_notice, self.toggle_pause)
        self.p_activity = Panel("ACTIVITY")
        right.addWidget(self.p_notices, 2)
        right.addWidget(self.p_activity, 3)
        mid.addLayout(right, 3)
        root.addLayout(mid, 5)

        convo = QFrame()
        convo.setObjectName("panel")
        cl = QVBoxLayout(convo)
        cl.setContentsMargins(22, 14, 22, 16)
        ct = QLabel("CONVERSATION")
        ct.setObjectName("panelTitle")
        cl.addWidget(ct)
        self.log = QTextBrowser()
        self.log.setOpenExternalLinks(True)
        cl.addWidget(self.log, 1)
        self.input = QLineEdit()
        self.input.setPlaceholderText(f"Type to {self.cfg.assistant.name}, or hold {self.talk_key} to talk")
        cl.addWidget(self.input)
        root.addWidget(convo, 3)

        self.confirm = ConfirmOverlay(self, self._answer)

    def _wire(self) -> None:
        b = self.bridge
        b.sig_heard.connect(self.on_heard)
        b.sig_reply_start.connect(self.on_reply_start)
        b.sig_reply_text.connect(self.on_reply_text)
        b.sig_tool.connect(self.on_tool)
        b.sig_reply_end.connect(self.on_reply_end)
        b.sig_notice.connect(lambda t: self.set_caption(t, C_DIM, 4))
        b.sig_error.connect(self.on_error)
        b.sig_confirm.connect(lambda a: self.confirm.ask(a, self.talk_key))
        b.sig_confirm_done.connect(self.on_confirm_done)
        b.sig_panels.connect(self.on_panels)
        b.sig_info.connect(lambda t: self._append_text(f"📱 {t}", C_DIM))
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
            return
        active = self.notices.active()
        self.p_notices.set_notices(active)
        paused = bool(self.heartbeat and self.heartbeat.paused)
        self.p_notices.set_paused(paused, enabled=self.heartbeat is not None)
        if paused or self.engine.state != "idle":
            return
        for n in active:
            if n.announced or n.urgency not in ("important", "urgent"):
                continue
            if n.urgency != "urgent" and _in_quiet_hours(self.heartbeat.settings.get("quiet_hours", "") if self.heartbeat else ""):
                continue  # held silently in the panel until morning
            self.notices.mark_announced(n.id)
            self.engine.announce(f"Heads up. {n.title}.")
            self.set_caption(n.title, C_WARN if n.urgency == "important" else C_BAD, 10)
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

    def _write(self, c: QTextCursor, s: str, color: str = C_TEXT, small: bool = False) -> None:
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if small:
            f.setFontFamilies(["Bahnschrift"])
            f.setFontPointSize(9)
            f.setFontLetterSpacing(130)
        c.insertText(s, f)
        self.log.setTextCursor(c)
        self.log.ensureCursorVisible()

    def _append_text(self, s: str, color: str = C_TEXT) -> None:
        """A standalone line (notices, errors)."""
        self._write(self._new_line(6), s.strip("\n"), color)

    def _speaker(self, who: str, color: str) -> None:
        self._write(self._new_line(14), who, color, small=True)

    def add_user(self, text: str, spoken: bool) -> None:
        self._speaker("YOU" + (" · SPOKEN" if spoken else ""), C_DIM)
        self._write(self._new_line(), text)

    def set_caption(self, text: str, color: str = C_TEXT, seconds: float = 0) -> None:
        self.caption.setText(html.escape(text))
        self.caption.setStyleSheet(f"color: {color};")
        self.caption_clear_at = time.monotonic() + seconds if seconds else 0

    # engine events
    def on_heard(self, text: str) -> None:
        self.add_user(text, spoken=True)
        self.set_caption(f"“{text}”", C_TEXT)

    def on_reply_start(self) -> None:
        self._speaker(self.cfg.assistant.name.upper(), C_ACCENT)
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
            line, color = f"{label} (web)", C_ACCENT
        elif is_error:
            line, color = f"✗ {label}", C_BAD
        else:
            line, color = f"✓ {label}", C_GOOD
        self.activity.insert(0, (line, f"{datetime.now():%H:%M} · {first}" if first else f"{datetime.now():%H:%M}", color))
        self.activity = self.activity[:8]
        self.p_activity.set_items(self.activity)
        self._write(self._new_line(4), f"    {line}{(' · ' + first) if first else ''}", color if is_error else C_DIM)
        self._reply_line = None  # text after a tool starts a fresh paragraph

    def on_reply_end(self, interrupted: bool, stats: TurnStats) -> None:
        if interrupted:
            self._write(self._cursor(), "  [interrupted]", C_DIM)
        if stats.cost and self.cfg.model.show_cost:
            self.meta.setText(f"{self.cfg.model.name} · last reply {stats.cost.removeprefix('cost ')}")
        self.set_caption("", C_TEXT)
        self.refresh_panels()

    def on_error(self, text: str) -> None:
        self._append_text(f"\n[{text}]\n", C_BAD)
        self.set_caption(text, C_BAD, 8)

    def on_confirm_done(self, approved: bool, timed_out: bool) -> None:
        self.confirm.hide()
        msg = "Approved." if approved else ("No answer, so I didn't do it." if timed_out else "Declined.")
        self._append_text(f"\n  {msg}\n", C_GOOD if approved else C_DIM)

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
            self._append_text("New conversation. Memory reloaded.\n", C_DIM)
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
                text, color = "READY · TYPE BELOW (VOICE OFF)", C_DIM
            elif self.engine.wake:
                text, color = f"READY · SAY “{self.engine.wake.name.upper()}” OR HOLD {self.talk_key}", C_ACCENT
            else:
                text, color = f"READY · HOLD {self.talk_key} TO TALK", C_ACCENT
        else:
            text, color = STATUS_TEXT.get(state, STATUS_TEXT["idle"])
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
        if self.caption_clear_at and time.monotonic() > self.caption_clear_at:
            self.set_caption("")
        if not self.meta.text():
            self.meta.setText(self.cfg.model.name)

    # side panels (loaded off the UI thread)
    def refresh_panels(self) -> None:
        threading.Thread(target=self._load_panels, daemon=True).start()

    def _load_panels(self) -> None:
        reg, out = self.engine.agent.registry, {}
        try:
            data = reg.read("business_list_items", status="open")
            out["tasks"] = data.get("items", [])
        except Exception as e:
            out["tasks_error"] = str(e)
        if "calendar_list_events" in reg.names() and (ROOT / "data" / "google_token.json").exists():
            try:
                today = date.today()
                ev = reg.read("calendar_list_events", start=today.isoformat(),
                              end=(today + timedelta(days=1)).isoformat(), max_results=20)
                out["events"] = ev.get("events", [])
            except Exception as e:
                out["events_error"] = f"Couldn't load the calendar just now ({str(e)[:80]}). Retrying in 5 minutes."
        else:
            out["events_error"] = "Google Calendar not connected yet. Ask “what's on my calendar?” to connect."
        self.bridge.sig_panels.emit(out)

    def on_panels(self, d: dict) -> None:
        today = date.today()
        if "events" in d:
            items = []
            for e in d["events"]:
                s = e.get("start", "")
                when_day = s[:10]
                tag = "" if when_day == today.isoformat() else "Tomorrow · "
                if e.get("all_day"):
                    when = f"{tag}All day"
                else:
                    try:
                        when = tag + datetime.fromisoformat(s).strftime("%I:%M %p").lstrip("0")
                    except ValueError:
                        when = s
                past = not e.get("all_day") and s and datetime.fromisoformat(s) < datetime.now().astimezone()
                items.append((e.get("title", ""), when + (f" · {e['location']}" if e.get("location") else ""),
                              C_DIM if past else ""))
            self.p_today.set_items(items, "Nothing on today or tomorrow.")
        else:
            self.p_today.set_items([(d.get("events_error", ""), "", C_DIM)])
        if "tasks" in d:
            items = []
            for t in d["tasks"]:
                due, color, small = t.get("due") or "", "", t.get("kind", "")
                if due:
                    if due < today.isoformat():
                        color, small = C_BAD, f"overdue · {due}"
                    elif due == today.isoformat():
                        color, small = C_WARN, "due today"
                    else:
                        small = f"due {due}"
                items.append((t.get("title", ""), small, color))
            self.p_tasks.set_items(items, "No open tasks.")
        else:
            self.p_tasks.set_items([(d.get("tasks_error", ""), "", C_BAD)])

    def _load_today_conversation(self) -> None:
        """Reopen with today's conversation so far (Fafnir remembers it too)."""
        try:
            from ..tools import CONVERSATIONS
            entries = CONVERSATIONS.today()[-40:]
        except Exception:
            entries = []
        if not entries:
            return
        self._write(self._new_line(), "EARLIER TODAY", C_DIM, small=True)
        for e in entries:
            who = "YOU" if e["role"] == "user" else self.cfg.assistant.name.upper()
            self._speaker(f"{who} · {e['time'][11:16]}", C_DIM if e["role"] == "user" else C_ACCENT)
            self._write(self._new_line(), e["text"], C_DIM)
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
                                  when, C_GOOD if ok else C_BAD))
        self.p_activity.set_items(self.activity, "Nothing yet.")

    # window
    def toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def closeEvent(self, event) -> None:
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
