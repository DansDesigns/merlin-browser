"""Smooth rounded corners for the page area.

A region mask is binary: every pixel is either in or out, so the curve comes
out visibly stepped. There is no antialiased clipping for a native rendering
surface, which the web view is.

So the corners are not cut at all. They are covered instead: a click-through
layer inside the window sits over the page area and paints four antialiased
wedges in the surrounding chrome colour, the curve as smooth as anything else
drawn with antialiasing on.

This is the same approach as the rounded_corners utility at
https://github.com/DansDesigns/rounded_corners, which does it for whole
screens rather than one widget.

With smoothing turned off, the caller uses the region mask instead, which is
square-edged.
"""
from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import QWidget


class CornerOverlay(QWidget):
    """The page's four corners, painted antialiased in the chrome's colour, as
    a layer inside the browser window, just above the page.

    It had been a window of its own (a frameless, translucent tool window kept
    on top of the browser). On Linux that window drew over other windows
    (a dark line through the Settings dialog, along the page's edge), left a
    dark strip where the window manager did not blend it, and was counted as a
    second Merlin window by window lists that show every window (Alternix's
    running apps). A child layer has none of that: Qt draws it with the window,
    over the page (Merlin Engine's and Chromium's alike), and nowhere else.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._radius = 10
        self._colour = QColor("#1b1c20")
        self._left = 0
        self._right = 0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def configure(self, radius: int, colour: str, left: int = 0,
                  right: int = 0) -> None:
        """Radius, colour, and how far the corners are inset either side.

        The insets move the corners without moving the layer, so the tab strip
        can widen over the page without the layer being resized on every frame
        of the animation.
        """
        changed = (radius, colour, left, right) != (
            self._radius, self._colour.name(), self._left, self._right)
        self._radius = max(0, int(radius))
        self._colour = QColor(colour)
        self._left = max(0, int(left))
        self._right = max(0, int(right))
        if changed:
            self.update()

    def follow(self, rect) -> None:
        """Sit exactly over the page area (in the parent's terms), or hide if
        there is nothing to do."""
        if self._radius <= 0 or rect.width() <= 0 or rect.height() <= 0:
            self.hide()
            return
        if self.geometry() != rect:
            self.setGeometry(rect)
        if not self.isVisible():
            self.show()
        self.raise_()

    def paintEvent(self, event):  # noqa: N802
        radius = self._radius
        if radius <= 0:
            return
        # Only the four corner wedges, never the space between them: what is
        # under the layer shows through everywhere else.
        area = QRectF(self.rect()).adjusted(self._left, 0, -self._right, 0)
        if area.width() <= radius * 2 or area.height() <= radius * 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rounded = QPainterPath()
        rounded.addRoundedRect(area, radius, radius)
        for corner_x, corner_y in (
            (area.left(), area.top()),
            (area.right() - radius, area.top()),
            (area.left(), area.bottom() - radius),
            (area.right() - radius, area.bottom() - radius),
        ):
            square = QPainterPath()
            square.addRect(QRectF(corner_x, corner_y, radius, radius))
            painter.fillPath(square.subtracted(rounded), self._colour)
        painter.end()
