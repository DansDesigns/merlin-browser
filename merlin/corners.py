"""Smooth rounded corners for the page area.

A region mask is binary: every pixel is either in or out, so the curve comes
out visibly stepped. There is no antialiased clipping for a native rendering
surface, which the web view is.

So the corners are not cut at all. They are covered instead: a frameless,
click-through, translucent window sits over the page area and paints four
antialiased wedges in the surrounding chrome colour. Because it is a top-level
window with per-pixel alpha, the compositor blends it properly, and the curve
is as smooth as anything else drawn with antialiasing on.

This is the same approach as the rounded_corners utility at
https://github.com/DansDesigns/rounded_corners, which does it for whole
screens rather than one widget.

If a translucent top-level cannot be created, the caller falls back to the
region mask, which is square-edged but always works.
"""
from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import QWidget


class CornerOverlay(QWidget):
    def __init__(self, parent=None):
        # Qt.Tool: a utility window owned by the browser window, with no
        # taskbar button and no entry in the window list.
        #
        # This was briefly a plain Qt.Window, to stop the platform hiding it
        # when the active window changed. That gave it its own taskbar button,
        # and since it is transparent apart from four corner wedges, clicking
        # it opened what looked like an empty window. The hiding is handled
        # properly now, by re-placing the overlay whenever the browser window's
        # activation or state changes, so the tool type can come back.
        super().__init__(parent, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.Tool
                         | Qt.WindowType.WindowTransparentForInput
                         | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self._owner = parent
        self._radius = 10
        self._colour = QColor("#1b1c20")
        self._left = 0
        self._right = 0
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def configure(self, radius: int, colour: str, left: int = 0,
                  right: int = 0) -> None:
        """Radius, colour, and how far the corners are inset either side.

        The insets move the corners without moving the window, so the tab strip
        can widen over the page without the overlay being resized on every
        frame of the animation.
        """
        changed = (radius, colour, left, right) != (
            self._radius, self._colour.name(), self._left, self._right)
        self._radius = max(0, int(radius))
        self._colour = QColor(colour)
        self._left = max(0, int(left))
        self._right = max(0, int(right))
        if changed:
            self.update()

    def follow(self, global_rect) -> None:
        """Sit exactly over the page area, or hide if there is nothing to do."""
        if self._radius <= 0 or global_rect.width() <= 0 or global_rect.height() <= 0:
            self.hide()
            return
        changed = self.geometry() != global_rect
        self.setGeometry(global_rect)
        if not self.isVisible():
            self.show()
        self.raise_()
        if changed:
            # a resize leaves the old contents behind until it is redrawn
            self.update()

    def paintEvent(self, event):  # noqa: N802
        radius = self._radius
        if radius <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Source)

        # Clear first. Source mode writes only what it draws and leaves the
        # rest of the surface untouched, so without this the wedges from the
        # previous size stayed on screen: the artefacts along the corners as
        # the tab strip widened and narrowed again.
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)

        # Only the four corner wedges are painted, never the space between
        # them. Filling everything outside the rounded rectangle would cover
        # the tab strip, which sits under this window when it is expanded.
        area = QRectF(self.rect()).adjusted(self._left, 0, -self._right, 0)
        if area.width() <= radius * 2 or area.height() <= radius * 2:
            painter.end()
            return

        for corner_x, corner_y in (
            (area.left(), area.top()),
            (area.right() - radius, area.top()),
            (area.left(), area.bottom() - radius),
            (area.right() - radius, area.bottom() - radius),
        ):
            box = QRectF(corner_x, corner_y, radius, radius)
            square = QPainterPath()
            square.addRect(box)
            rounded = QPainterPath()
            rounded.addRoundedRect(area, radius, radius)
            painter.fillPath(square.subtracted(rounded), self._colour)
        painter.end()
