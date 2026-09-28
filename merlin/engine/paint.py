"""Painting: the display list onto a QPainter.

Only what falls inside the area being painted is drawn, so a long page costs
no more to scroll through than a short one.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetricsF, QPen

from .layout import DisplayList


_SVG_CACHE: dict = {}


def _svg_renderer(markup: str):
    """A Qt SVG renderer for the markup, made once; None if SVG cannot draw.

    Qt's SVG module is loaded only when a page has SVG in it, and a failure
    leaves the picture out rather than the page.
    """
    renderer = _SVG_CACHE.get(markup)
    if renderer is None and markup not in _SVG_CACHE:
        try:
            from PyQt6.QtCore import QByteArray
            from PyQt6.QtSvg import QSvgRenderer

            renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
            if not renderer.isValid():
                renderer = None
        except Exception:                                  # noqa: BLE001
            renderer = None
        if len(_SVG_CACHE) > 500:
            _SVG_CACHE.clear()
        _SVG_CACHE[markup] = renderer
    return renderer


def _colour(rgba) -> QColor:
    return QColor(rgba[0], rgba[1], rgba[2], rgba[3])


def paint(painter, display: DisplayList, visible: QRectF, images=None) -> None:
    """Draw the parts of the page inside visible, given in page coordinates."""
    images = images or {}
    for item in display.items:
        if item is None:
            continue
        if item[0] == "group":
            for sub in item[1]:
                _draw(painter, sub, visible, images)
        else:
            _draw(painter, item, visible, images)


def _draw(painter, item, visible: QRectF, images) -> None:
    kind = item[0]
    if kind == "rect":
        rect = item[1]
        if rect.intersects(visible):
            painter.fillRect(rect, _colour(item[2]))
    elif kind == "svg":
        rect, markup = item[1], item[2]
        if rect.intersects(visible):
            renderer = _svg_renderer(markup)
            if renderer is not None:
                renderer.render(painter, rect)
    elif kind == "rrect":
        rect, rgba, radius = item[1], item[2], item[3]
        if rect.intersects(visible):
            painter.save()
            painter.setRenderHint(painter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_colour(rgba))
            painter.drawRoundedRect(rect, radius, radius)
            painter.restore()
    elif kind == "rborder":
        rect, rgba, width, radius = item[1], item[2], item[3], item[4]
        if rect.intersects(visible) and rgba:
            painter.save()
            painter.setRenderHint(painter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(_colour(rgba), width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            half = width / 2
            painter.drawRoundedRect(rect.adjusted(half, half, -half, -half),
                                    max(0.0, radius - half), max(0.0, radius - half))
            painter.restore()
    elif kind == "text":
        _k, x, baseline, text, font, rgba, decoration = item
        metrics = QFontMetricsF(font)
        top = baseline - metrics.ascent()
        if top > visible.bottom() or baseline + metrics.descent() < visible.top():
            return
        painter.setFont(font)
        painter.setPen(_colour(rgba))
        painter.drawText(QPointF(x, baseline), text)
        if decoration in ("underline", "line-through"):
            width = metrics.horizontalAdvance(text)
            thickness = max(1.0, metrics.lineWidth())
            y = baseline + metrics.underlinePos() if decoration == "underline" \
                else baseline - metrics.strikeOutPos()
            painter.fillRect(QRectF(x, y, width, thickness), _colour(rgba))
    elif kind == "image":
        _k, rect, src, alt = item
        if not rect.intersects(visible):
            return
        picture = images.get(src)
        if picture is not None and not picture.isNull():
            painter.drawImage(rect, picture)
            return
        # not loaded, or not loadable: a quiet placeholder with the alt text
        painter.setPen(QPen(QColor(160, 160, 160), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        if alt:
            painter.setPen(QColor(110, 110, 110))
            painter.drawText(rect.adjusted(4, 2, -4, -2),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                             | Qt.TextFlag.TextWordWrap, alt)
