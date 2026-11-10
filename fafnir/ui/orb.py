"""The animated core in the middle of the HUD. Its colour and motion show Fafnir's state;
it swells with the loudness of your voice (listening) or Fafnir's (speaking)."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

STATE_COLORS = {
    "idle": QColor(63, 208, 255),
    "listening": QColor(61, 255, 168),
    "thinking": QColor(255, 179, 71),
    "speaking": QColor(90, 220, 255),
    "confirming": QColor(255, 95, 109),
    "offline": QColor(110, 130, 150),
}
SPIN = {"idle": 0.15, "listening": 0.5, "thinking": 1.6, "speaking": 0.6, "confirming": 0.3, "offline": 0.05}


def _with_alpha(c: QColor, a: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


class Orb(QWidget):
    def __init__(self, source, parent=None):
        """`source` has .state (str) and .level (0..1)."""
        super().__init__(parent)
        self.source = source
        self.level = 0.0
        self.color = QColor(STATE_COLORS["idle"])
        self.angle = 0.0
        self._last = time.monotonic()
        self.setMinimumSize(320, 320)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(33)  # ~30 fps

    def paintEvent(self, _event) -> None:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        state = getattr(self.source, "state", "offline")
        target = STATE_COLORS.get(state, STATE_COLORS["idle"])
        # ease colour and loudness so changes glide instead of snapping
        k = min(1.0, dt * 6)
        self.color = QColor(
            int(self.color.red() + (target.red() - self.color.red()) * k),
            int(self.color.green() + (target.green() - self.color.green()) * k),
            int(self.color.blue() + (target.blue() - self.color.blue()) * k),
        )
        lvl = float(getattr(self.source, "level", 0.0) or 0.0)
        self.level += (lvl - self.level) * min(1.0, dt * (14 if lvl > self.level else 5))
        self.angle = (self.angle + dt * 360 * SPIN.get(state, 0.2) * 0.25) % 360

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        c = QPointF(w / 2, h / 2)
        R = min(w, h) * 0.36
        breathe = 0.5 + 0.5 * math.sin(now * (3.0 if state == "thinking" else 1.3))
        col, L = self.color, self.level

        # soft outer glow
        g = QRadialGradient(c, R * 1.6)
        g.setColorAt(0.0, _with_alpha(col, 0.22 + 0.25 * L))
        g.setColorAt(0.45, _with_alpha(col, 0.08 + 0.1 * L))
        g.setColorAt(1.0, _with_alpha(col, 0.0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, R * 1.6, R * 1.6)

        # rings that ripple with sound
        for i, (rr, a) in enumerate(((0.72, 0.55), (0.88, 0.35), (1.04, 0.2), (1.22, 0.1))):
            wobble = math.sin(now * (1.2 + i * 0.4) + i) * R * 0.012
            r = R * rr + wobble + L * R * (0.06 + i * 0.05)
            p.setPen(QPen(_with_alpha(col, a * (0.7 + 0.3 * breathe) + 0.2 * L), 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r, r)

        # spinning arc segments
        for i in range(3):
            r = R * (0.96 + i * 0.1)
            rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
            pen = QPen(_with_alpha(col, 0.75 - i * 0.2), 3.2 - i * 0.7)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            direction = -1 if i % 2 else 1
            start = (self.angle * (1 + i * 0.6) * direction + i * 120) % 360
            p.drawArc(rect, int(start * 16), int((50 - i * 10) * 16))
            p.drawArc(rect, int((start + 180) * 16), int((24 - i * 6) * 16))

        # bright core
        cr = R * (0.42 + 0.04 * breathe + 0.22 * L)
        core = QRadialGradient(c, cr)
        core.setColorAt(0.0, QColor(255, 255, 255, 235))
        core.setColorAt(0.35, _with_alpha(col, 0.95))
        core.setColorAt(0.8, _with_alpha(col, 0.25))
        core.setColorAt(1.0, _with_alpha(col, 0.0))
        p.setPen(Qt.NoPen)
        p.setBrush(core)
        p.drawEllipse(c, cr, cr)
        p.end()
