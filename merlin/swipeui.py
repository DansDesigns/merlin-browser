"""The swipe indicator.

A circular arrow that grows out of the window edge as you swipe, the way Brave
and Chrome do it. The point is feedback before commitment: you can see how far
you are from triggering, and you can back out by reversing before the threshold.

It is a frameless child widget painted over the page rather than anything the
web view knows about, and it never takes mouse input.
"""
from __future__ import annotations

from PyQt6.QtCore import (
    QEasingCurve, QPropertyAnimation, QRectF, Qt, pyqtProperty,
)
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

BASE_SIZE = 34          # diameter at the moment the gesture starts
FULL_SIZE = 62          # diameter once the threshold is reached
EDGE_MARGIN = 10
IDLE = "#2b2d36"
ARMED = "#6f8ff0"
ARROW_IDLE = "#c9cbd6"
ARROW_ARMED = "#ffffff"


class SwipeIndicator(QWidget):
    """Circle plus arrow whose size and colour follow gesture progress."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

        self._progress = 0.0
        self._forward = False
        self._fading = False

        self._fade = QPropertyAnimation(self, b"progress", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._after_fade)

    # ------------------------------------------------------------ property
    def get_progress(self) -> float:
        return self._progress

    def set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self._reposition()
        self.update()

    progress = pyqtProperty(float, fget=get_progress, fset=set_progress)

    # -------------------------------------------------------------- layout
    def _diameter(self) -> int:
        return int(BASE_SIZE + (FULL_SIZE - BASE_SIZE) * self._progress)

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        size = self._diameter()
        # slides in from the edge as it grows, so it feels attached to the swipe
        travel = int(6 + 16 * self._progress)
        y = (parent.height() - size) // 2
        if self._forward:
            x = parent.width() - size - EDGE_MARGIN - travel + 22
        else:
            x = EDGE_MARGIN + travel - 22
        self.setGeometry(x, y, size, size)

    # --------------------------------------------------------------- shown
    def update_gesture(self, forward: bool, progress: float) -> None:
        if self._fading:
            self._fade.stop()
            self._fading = False
        self._forward = forward
        self.set_progress(progress)
        if not self.isVisible():
            self.show()
        self.raise_()

    def finish(self, triggered: bool) -> None:
        """Fade out. A triggered gesture flashes full size on the way."""
        if not self.isVisible():
            return
        self._fading = True
        self._fade.stop()
        self._fade.setStartValue(1.0 if triggered else self._progress)
        self._fade.setEndValue(0.0)
        self._fade.setDuration(240 if triggered else 150)
        self._fade.start()

    def _after_fade(self) -> None:
        if self._fading:
            self._fading = False
            self.hide()

    # -------------------------------------------------------------- paint
    def paintEvent(self, event):  # noqa: N802
        if self._progress <= 0.01:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        size = self.width()
        armed = self._progress >= 0.999
        alpha = int(70 + 165 * self._progress)

        disc = QColor(ARMED if armed else IDLE)
        disc.setAlpha(min(245, alpha + 40))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(disc)
        painter.drawEllipse(QRectF(1, 1, size - 2, size - 2))

        # progress ring, so the remaining distance is legible
        if not armed:
            ring = QPen(QColor(ARMED))
            ring.setWidthF(max(2.0, size * 0.055))
            ring.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(ring)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            inset = ring.widthF() / 2 + 1
            painter.drawArc(
                QRectF(inset, inset, size - inset * 2, size - inset * 2),
                90 * 16, -int(360 * 16 * self._progress))

        # arrow, scaled to the circle
        s = size / 64.0
        arrow = QPainterPath()
        if self._forward:
            arrow.moveTo(26 * s, 18 * s)
            arrow.lineTo(40 * s, 32 * s)
            arrow.lineTo(26 * s, 46 * s)
        else:
            arrow.moveTo(38 * s, 18 * s)
            arrow.lineTo(24 * s, 32 * s)
            arrow.lineTo(38 * s, 46 * s)
        pen = QPen(QColor(ARROW_ARMED if armed else ARROW_IDLE))
        pen.setWidthF(5.2 * s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(arrow)
        painter.end()
