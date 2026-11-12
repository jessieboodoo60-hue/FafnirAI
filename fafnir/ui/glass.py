"""The "liquid glass" look: an aurora background, and frosted panels that show a blurred,
tinted view of whatever background is behind them.

Qt can't blur what's behind a widget, so the window keeps a pre-blurred copy of its
background and each GlassFrame paints the matching piece of it, then a light tint, a soft
top highlight and a thin bright edge.
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import QFrame, QWidget

# palette for the whole app
TEXT = "#eef6ff"
DIM = "#93a8bd"
ACCENT = "#7fe6ff"
GOOD = "#5dffb6"
WARN = "#ffc46b"
BAD = "#ff7a86"
VIOLET = "#b49cff"


def aurora(width: int, height: int, seed: int = 7) -> QPixmap:
    """A soft aurora: deep navy with large glowing colour clouds and a little grain."""
    img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    base = QLinearGradient(0, 0, width * 0.4, height)
    base.setColorAt(0.0, QColor("#060b1a"))
    base.setColorAt(0.55, QColor("#0a1230"))
    base.setColorAt(1.0, QColor("#05101c"))
    p.fillRect(0, 0, width, height, base)
    rnd = random.Random(seed)
    blobs = [  # (x, y, radius, colour, strength) as fractions of the screen
        (0.15, 0.20, 0.55, "#1b6bff", 0.55), (0.82, 0.12, 0.45, "#8a5cff", 0.50),
        (0.70, 0.78, 0.60, "#00c2b8", 0.42), (0.25, 0.85, 0.45, "#3fd0ff", 0.38),
        (0.50, 0.45, 0.35, "#ff5fd2", 0.16), (0.95, 0.55, 0.30, "#4f7dff", 0.30),
    ]
    p.setCompositionMode(QPainter.CompositionMode_Screen)
    for fx, fy, fr, color, strength in blobs:
        cx, cy = fx * width, fy * height
        r = fr * max(width, height)
        g = QRadialGradient(QPointF(cx, cy), r)
        c = QColor(color)
        c.setAlphaF(strength)
        g.setColorAt(0.0, c)
        c2 = QColor(color)
        c2.setAlphaF(strength * 0.35)
        g.setColorAt(0.45, c2)
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(g)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), r, r * 0.75)
    # faint ribbons of light, like an aurora curtain
    for i in range(3):
        path = QPainterPath()
        y0 = height * (0.25 + 0.2 * i)
        path.moveTo(0, y0)
        for k in range(1, 9):
            x = width * k / 8
            path.lineTo(x, y0 + math.sin(k * 0.9 + i) * height * 0.06 + rnd.uniform(-20, 20))
        pen = QPen(QColor(150, 230, 255, 26), height * 0.05)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
    # grain
    p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    for _ in range(int(width * height / 900)):
        a = rnd.randint(6, 18)
        p.setPen(QColor(255, 255, 255, a) if rnd.random() < 0.5 else QColor(0, 0, 0, a))
        p.drawPoint(rnd.randrange(width), rnd.randrange(height))
    p.end()
    return QPixmap.fromImage(img)


def load_background(width: int, height: int, path: str = "") -> QPixmap:
    """The owner's own image (cover-scaled and darkened a little), or the aurora."""
    if path:
        img = QPixmap(str(Path(path).expanduser()))
        if not img.isNull():
            scaled = img.scaled(width, height, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            out = QPixmap(width, height)
            p = QPainter(out)
            p.drawPixmap((width - scaled.width()) // 2, (height - scaled.height()) // 2, scaled)
            p.fillRect(0, 0, width, height, QColor(4, 8, 20, 110))  # keep text readable
            p.end()
            return out
    return aurora(width, height)


def blurred(pix: QPixmap, strength: int = 18) -> QPixmap:
    """A cheap, smooth blur: shrink a lot, then scale back up (twice)."""
    w, h = pix.width(), pix.height()
    small = pix.scaled(max(1, w // strength), max(1, h // strength), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    mid = small.scaled(max(1, w // 4), max(1, h // 4), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return mid.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


class GlassFrame(QFrame):
    """A frosted glass panel. Put normal widgets inside it; it draws its own glass."""

    def __init__(self, parent: QWidget | None = None, radius: int = 22, tint: QColor | None = None):
        super().__init__(parent)
        self.radius = radius
        self.tint = tint or QColor(255, 255, 255, 20)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("glass")

    def _backdrop(self) -> QPixmap | None:
        w = self.window()
        return getattr(w, "bg_blur", None)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, self.radius, self.radius)
        # soft outer shadow
        for i, a in ((6, 10), (3, 18)):
            sp = QPainterPath()
            sp.addRoundedRect(r.adjusted(-i, -i + 3, i, i + 3), self.radius + i, self.radius + i)
            p.fillPath(sp, QColor(0, 0, 0, a))
        p.setClipPath(path)
        back = self._backdrop()
        if back is not None:
            origin = self.mapTo(self.window(), QPoint(0, 0))
            p.drawPixmap(0, 0, back, origin.x(), origin.y(), self.width(), self.height())
        p.fillPath(path, self.tint)
        # light catching the top of the glass
        sheen = QLinearGradient(0, 0, 0, r.height())
        sheen.setColorAt(0.0, QColor(255, 255, 255, 34))
        sheen.setColorAt(0.18, QColor(255, 255, 255, 8))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(sheen))
        p.setClipping(False)
        # bright, thin edge: stronger at the top-left
        edge = QLinearGradient(r.topLeft(), r.bottomRight())
        edge.setColorAt(0.0, QColor(255, 255, 255, 110))
        edge.setColorAt(0.5, QColor(255, 255, 255, 28))
        edge.setColorAt(1.0, QColor(255, 255, 255, 60))
        p.setPen(QPen(QBrush(edge), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()
