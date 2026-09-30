"""Painting: the display list onto a QPainter.

Only what falls inside the area being painted is drawn, so a long page costs
no more to scroll through than a short one.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetricsF, QPen

from .layout import DisplayList


class SvgPicture:
    """An SVG image kept as drawing instructions, drawn sharp at any size.

    It answers what layout asks of a picture (width, height, isNull), with its
    own size in page pixels, so an <img src="...svg"> sizes as a bitmap would.
    """

    def __init__(self, renderer):
        self.renderer = renderer
        size = renderer.defaultSize()
        self._w = size.width() if size.width() > 0 else 300
        self._h = size.height() if size.height() > 0 else 150

    def width(self) -> int:
        return self._w

    def height(self) -> int:
        return self._h

    @staticmethod
    def devicePixelRatio() -> float:                      # noqa: N802
        return 1.0

    def isNull(self) -> bool:                             # noqa: N802
        return not self.renderer.isValid()

    def render(self, painter, rect) -> None:
        self.renderer.render(painter, rect)


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


_PUSHES = ("clip_push", "opacity_push", "sticky_push")
_POPS = ("clip_pop", "opacity_pop", "sticky_pop")


def _layer_ends(display: DisplayList) -> dict:
    """Where each layer_push is closed: worked out once for a display list."""
    ends = getattr(display, "_layer_ends", None)
    if ends is not None and getattr(display, "_layer_count", -1) == len(display.items):
        return ends
    ends, stack = {}, []
    for index, item in enumerate(display.items):
        if item is None:
            continue
        if item[0] == "layer_push":
            stack.append(index)
        elif item[0] == "layer_pop" and stack:
            ends[stack.pop()] = index
    for index in stack:                     # never closed: runs to the end
        ends[index] = len(display.items)
    display._layer_ends, display._layer_count = ends, len(display.items)
    return ends


def _apply(painter, display: DisplayList, item, shown: list, scroll: float) -> None:
    """One clip, opacity or sticky offset, after painter.save()."""
    kind = item[0]
    if kind == "clip_push":
        rect, radius = item[1], item[2]
        if radius > 0.5:
            from PyQt6.QtGui import QPainterPath

            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
        else:
            painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        shown.append(shown[-1])
    elif kind == "opacity_push":
        painter.setOpacity(painter.opacity() * item[1])
        shown.append(shown[-1])
    else:                                   # sticky_push
        moved = display.sticky_offset(item[1], scroll)
        painter.translate(0, moved)
        shown.append(shown[-1].translated(0, -moved))


def paint(painter, display: DisplayList, visible: QRectF, images=None) -> None:
    """Draw the parts of the page inside visible, given in page coordinates.

    In CSS's stacking order, not simply document order: a positioned box
    (relative, absolute, fixed or sticky) is a layer, painted after the
    ordinary content around it and ordered by its z-index, negative ones
    first. A pinned header with a z-index therefore stays above the content
    scrolling under it, which had been painted over it.
    """
    _paint_range(painter, display, 0, len(display.items), [visible], images or {},
                 _layer_ends(display), visible.top(), [])


def _paint_range(painter, display, start, end, shown, images, ends, scroll, active) -> None:
    items = display.items
    # the layers at this level, with the clips, opacities and sticky offsets
    # in force where each begins, to be set again when it is painted later
    layers, in_force = [], []
    index = start
    while index < end:
        item = items[index]
        if item is not None:
            kind = item[0]
            if kind == "layer_push":
                close = ends.get(index, end)
                # only what was set up at this level: whatever encloses this
                # level is already in force while it paints, and applying it
                # again doubled opacity and sticky offsets
                layers.append((item[1], len(layers), index, close, list(in_force)))
                index = close + 1
                continue
            if kind in _PUSHES:
                in_force.append(item)
            elif kind in _POPS and in_force:
                in_force.pop()
        index += 1
    below = sorted(l for l in layers if l[0] < 0)
    above = sorted(l for l in layers if l[0] >= 0)
    for layer in below:
        _paint_layer(painter, display, layer, shown, images, ends, scroll)
    # the content at this level, in document order
    depth = 0
    here = [shown[0]] + list(shown[1:])
    index = start
    while index < end:
        item = items[index]
        if item is None:
            index += 1
            continue
        kind = item[0]
        if kind == "layer_push":
            index = ends.get(index, end) + 1
            continue
        if kind in _PUSHES:
            painter.save()
            depth += 1
            _apply(painter, display, item, here, scroll)
        elif kind in _POPS:
            if depth:
                painter.restore()
                depth -= 1
                if len(here) > 1:
                    here.pop()
        elif kind == "group":
            for sub in item[1]:
                _draw(painter, sub, here[-1], images)
        elif kind != "layer_pop":
            _draw(painter, item, here[-1], images)
        index += 1
    while depth:
        painter.restore()
        depth -= 1
    for layer in above:
        _paint_layer(painter, display, layer, shown, images, ends, scroll)


def _paint_layer(painter, display, layer, shown, images, ends, scroll) -> None:
    _z, _order, start, close, in_force = layer
    painter.save()
    here = list(shown)
    for item in in_force:
        painter.save()
        _apply(painter, display, item, here, scroll)
    _paint_range(painter, display, start + 1, close, here, images, ends, scroll, in_force)
    for _item in in_force:
        painter.restore()
    painter.restore()


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
    elif kind == "gradient":
        rect, spec, radius = item[1], item[2], item[3]
        if rect.intersects(visible):
            fill_gradient(painter, rect, spec, radius)
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
            if isinstance(picture, SvgPicture):
                picture.render(painter, rect)          # sharp at any size
            else:
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


# ------------------------------------------------------------------ gradients

def _stop_positions(stops: list, length: float) -> list:
    """Each stop's place from 0 to 1, as CSS spreads those left unplaced."""
    places = []
    for _colour, position in stops:
        if position is None:
            places.append(None)
        elif isinstance(position, tuple):
            places.append(position[1] / 100)
        else:
            places.append(position / length if length else 0.0)
    if places and places[0] is None:
        places[0] = 0.0
    if places and places[-1] is None:
        places[-1] = 1.0
    index = 0
    while index < len(places):
        if places[index] is None:
            start = index - 1
            end = index
            while places[end] is None:
                end += 1
            for step, k in enumerate(range(index, end), 1):
                places[k] = places[start] + (places[end] - places[start]) * step / (end - start)
            index = end
        index += 1
    for k in range(1, len(places)):
        places[k] = max(places[k], places[k - 1])    # never back along the line
    return [min(1.0, max(0.0, p)) for p in places]


def gradient_brush(rect: QRectF, spec):
    """A Qt brush for a CSS gradient over rect."""
    import math

    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QBrush, QLinearGradient, QRadialGradient, QTransform

    w, h = rect.width(), rect.height()
    centre = rect.center()
    if spec[0] == "linear":
        _k, angle, stops = spec
        a = math.radians(angle)
        length = abs(w * math.sin(a)) + abs(h * math.cos(a))
        dx, dy = math.sin(a) * length / 2, -math.cos(a) * length / 2
        gradient = QLinearGradient(QPointF(centre.x() - dx, centre.y() - dy),
                                   QPointF(centre.x() + dx, centre.y() + dy))
        for (colour, _p), place in zip(stops, _stop_positions(stops, length)):
            gradient.setColorAt(place, _colour(colour))
        return QBrush(gradient)
    _k, shape, size, position, stops = spec

    def at(value, extent):
        return extent * value[1] / 100 if isinstance(value, tuple) else value

    cx, cy = rect.left() + at(position[0], w), rect.top() + at(position[1], h)
    sides_x = (abs(cx - rect.left()), abs(rect.right() - cx))
    sides_y = (abs(cy - rect.top()), abs(rect.bottom() - cy))
    if isinstance(size, float):
        rx = ry = size
    elif shape == "circle":
        corners = [math.hypot(x, y) for x in sides_x for y in sides_y]
        rx = ry = {"closest-side": min(sides_x + sides_y), "farthest-side": max(sides_x + sides_y),
                   "closest-corner": min(corners)}.get(size, max(corners))
    else:
        near = (min(sides_x), min(sides_y))
        far = (max(sides_x), max(sides_y))
        rx, ry = {"closest-side": near, "farthest-side": far,
                  "closest-corner": (near[0] * math.sqrt(2), near[1] * math.sqrt(2))}.get(
            size, (far[0] * math.sqrt(2), far[1] * math.sqrt(2)))
    rx, ry = max(rx, 0.01), max(ry, 0.01)
    gradient = QRadialGradient(QPointF(cx, cy), rx)
    for (colour, _p), place in zip(stops, _stop_positions(stops, rx)):
        gradient.setColorAt(place, _colour(colour))
    brush = QBrush(gradient)
    if abs(rx - ry) > 0.01:
        # an ellipse: a circle stretched about its centre
        brush.setTransform(QTransform().translate(cx, cy).scale(1.0, ry / rx).translate(-cx, -cy))
    return brush


_DITHERED: dict = {}          # (spec, size, ratio) -> the finished picture, newest last
_NOISE = None
DITHER_FROM = 40_000          # pixels: smaller gradients do not band visibly


def _noise_tile():
    """64x64 of noise under one 8-bit shade, in 16-bit colour, made once."""
    global _NOISE
    if _NOISE is None:
        import random

        from PyQt6.QtGui import QColor, QImage

        tile = QImage(64, 64, QImage.Format.Format_RGBA64_Premultiplied)
        shade = random.Random(1729)            # the same grain every time
        for y in range(64):
            for x in range(64):
                n = shade.randrange(257)       # below one 8-bit step (257 in 16 bits)
                # opaque, so the small values stay whole when added: with no
                # alpha a premultiplied pixel's colour is nothing
                tile.setPixelColor(x, y, QColor.fromRgba64(n, n, n, 65535))
        _NOISE = tile
    return _NOISE


def _dithered(rect: QRectF, spec, ratio: float):
    """The gradient drawn smoothly: dithered, and at the screen's own pixels.

    Qt draws gradients to 8 bits a channel without dithering, so a dark, gentle
    blend became stripes of one shade, some 150 pixels wide. Here it is drawn in
    16 bits a channel, noise below one 8-bit step is added, and the result is
    reduced to 8 bits: each pixel then falls to the shade above or below as
    often as the true colour lies near it, as browsers do. Kept, so scrolling
    and redrawing reuse it.
    """
    from PyQt6.QtGui import QImage, QPainter as _Painter

    width, height = max(1, round(rect.width() * ratio)), max(1, round(rect.height() * ratio))
    key = (repr(spec), width, height)
    found = _DITHERED.pop(key, None)
    if found is None:
        deep = QImage(width, height, QImage.Format.Format_RGBA64_Premultiplied)
        deep.fill(0)
        painter = _Painter(deep)
        painter.fillRect(QRectF(0, 0, width, height), gradient_brush(QRectF(0, 0, width, height), spec))
        # half a shade down first: the noise below is added, from nothing up to
        # a whole shade, and the reduction to 8 bits rounds, so without this
        # every colour came out half a shade light
        from PyQt6.QtGui import QColor as _Colour

        painter.setCompositionMode(_Painter.CompositionMode.CompositionMode_Difference)
        painter.fillRect(QRectF(0, 0, width, height), _Colour.fromRgba64(128, 128, 128, 65535))
        painter.setCompositionMode(_Painter.CompositionMode.CompositionMode_Plus)
        # an image texture, which keeps its 16 bits: a pixmap is usually 8 bits
        # a channel, and noise under one 8-bit step came through as nothing
        from PyQt6.QtGui import QBrush

        grain = QBrush()
        grain.setTextureImage(_noise_tile())
        painter.fillRect(QRectF(0, 0, width, height), grain)
        painter.end()
        found = deep.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        found.setDevicePixelRatio(ratio)
    _DITHERED[key] = found
    while len(_DITHERED) > 12:
        _DITHERED.pop(next(iter(_DITHERED)))
    return found


def fill_gradient(painter, rect: QRectF, spec, radius: float = 0.0) -> None:
    ratio = 1.0
    try:
        ratio = float(painter.device().devicePixelRatioF())
    except Exception:                                      # noqa: BLE001
        pass
    area = rect.width() * rect.height() * ratio * ratio
    if DITHER_FROM <= area <= 12_000_000:
        picture = _dithered(rect, spec, ratio)
        painter.save()
        if radius > 0.5:
            from PyQt6.QtGui import QPainterPath

            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
        painter.drawImage(rect, picture)
        painter.restore()
        return
    brush = gradient_brush(rect, spec)
    painter.save()
    if radius > 0.5:
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(brush)
        painter.drawRoundedRect(rect, radius, radius)
    else:
        painter.fillRect(rect, brush)
    painter.restore()
