"""Painting: the display list onto a QPainter.

Only what falls inside the area being painted is drawn, so a long page costs
no more to scroll through than a short one.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetricsF, QPainterPath, QPen

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


_PUSHES = ("clip_push", "opacity_push", "sticky_push", "fixed_push", "xform_push", "scroll_push")
_POPS = ("clip_pop", "opacity_pop", "sticky_pop", "fixed_pop", "xform_pop", "scroll_pop")

# The time animations are drawn at, in seconds since the page was shown: set
# by the view before each paint.
NOW = 0.0

# Where, among the pictures paint is given, the page's videos are: media_key
# -> (controller, place). Each draws its own picture and controls.
MEDIA_KEY = "\x00media"


def _transform(spec: dict):
    """An element's transform at NOW, as a QTransform, and the opacity it is
    drawn with (or None to leave it): its own transform, its animations
    sampled, about its origin, seen through its parent's perspective."""
    from PyQt6.QtGui import QTransform

    from . import animate
    from .layout import _origin

    if not spec.get("animations") and "_drawn" in spec:
        # a transform that does not move is worked out once, not every frame
        return spec["_drawn"]
    ops, opacity = spec["ops"], None
    box = spec["box"]
    for animation in spec.get("animations") or []:
        moved, faded = animate.sample(animation, NOW, spec["ops"], spec["opacity"], box)
        if moved is not None:
            ops = moved
        if faded is not None:
            opacity = faded
    if spec.get("animates_opacity") and opacity is None:
        opacity = spec["opacity"]
    m = animate.matrix(ops, box, spec["origin"])
    seen = spec.get("perspective")
    if seen and seen.get("box"):
        pb = seen["box"]
        px, py = _origin(seen.get("origin"), pb[2], pb[3])
        m = animate.with_perspective(m, (seen["depth"], pb[0] + px, pb[1] + py))
    if spec.get("backface") and animate.facing_away(m):
        opacity = 0.0                       # turned away, with its back hidden
    drawn = (QTransform(*animate.projected(m)), opacity)
    if not spec.get("animations"):
        spec["_drawn"] = drawn
    return drawn


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


def corner_path(rect: QRectF, corners):
    """A rectangle with each corner rounded by its own radius: (top left,
    top right, bottom right, bottom left), as CSS's border-radius gives."""
    from PyQt6.QtGui import QPainterPath

    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    tl, tr, br, bl = (max(0.0, min(r, w / 2, h / 2)) for r in corners)
    path = QPainterPath()
    path.moveTo(x + tl, y)
    path.lineTo(x + w - tr, y)
    if tr:
        path.arcTo(QRectF(x + w - 2 * tr, y, 2 * tr, 2 * tr), 90, -90)
    path.lineTo(x + w, y + h - br)
    if br:
        path.arcTo(QRectF(x + w - 2 * br, y + h - 2 * br, 2 * br, 2 * br), 0, -90)
    path.lineTo(x + bl, y + h)
    if bl:
        path.arcTo(QRectF(x, y + h - 2 * bl, 2 * bl, 2 * bl), 270, -90)
    path.lineTo(x, y + tl)
    if tl:
        path.arcTo(QRectF(x, y, 2 * tl, 2 * tl), 180, -90)
    path.closeSubpath()
    return path


def _shape_path(shape, rect: QRectF):
    """A clip-path shape (layout's _clip_shape) as a path, placed at rect."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath, QPolygonF

    x, y = rect.x(), rect.y()
    path = QPainterPath()
    if shape[0] == "polygon":
        path.addPolygon(QPolygonF([QPointF(x + px, y + py) for px, py in shape[1]]))
        path.closeSubpath()
    elif shape[0] == "ellipse":
        _k, cx, cy, rx, ry = shape
        path.addEllipse(QPointF(x + cx, y + cy), rx, ry)
    else:
        _k, left, top, w, h, r = shape
        path.addRoundedRect(QRectF(x + left, y + top, w, h), r, r)
    return path


def _apply(painter, display: DisplayList, item, shown: list, scroll: float) -> None:
    """One clip, opacity or sticky offset, after painter.save()."""
    kind = item[0]
    if kind == "clip_push":
        rect, radius = item[1], item[2]
        if len(item) > 4 and item[4]:
            painter.setClipPath(_shape_path(item[4], rect), Qt.ClipOperation.IntersectClip)
        elif radius > 0.5:
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
    elif kind == "xform_push":
        try:
            transform, opacity = _transform(item[1])
        except Exception:                                  # noqa: BLE001
            # an animation that cannot be drawn is drawn still, and not tried
            # again on every frame
            item[1]["animations"] = None
            from PyQt6.QtGui import QTransform as _Q

            transform, opacity = _Q(), None
        painter.setTransform(transform, True)
        if opacity is not None:
            painter.setOpacity(painter.opacity() * opacity)
        inverse, invertible = transform.inverted()
        # what is in view, in the element's own terms, so culling stays right
        shown.append(inverse.mapRect(shown[-1]) if invertible
                     else QRectF(-1e7, -1e7, 2e7, 2e7))
    elif kind == "scroll_push":
        # a panel scrolled inside the page: its contents moved up by its scroll
        moved = display.panel_offset(item[1])
        painter.translate(0, -moved)
        shown.append(shown[-1].translated(0, moved))
    elif kind == "fixed_push":
        # pinned to the window: the page's scroll undone for what is inside
        painter.translate(0, scroll)
        shown.append(shown[-1].translated(0, -scroll))
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


def _escaping(in_force: list) -> list:
    """What still applies to a layer: a fixed box escapes the clips and sticky
    offsets of what it sits in, being placed against the window; only their
    opacity stays with it. Kept, those clips hid GitHub's header entirely."""
    last = max((i for i, item in enumerate(in_force) if item[0] == "fixed_push"), default=None)
    if last is None:
        return list(in_force)
    return [item for item in in_force[:last] if item[0] == "opacity_push"] + in_force[last:]


def _paint_range(painter, display, start, end, shown, images, ends, scroll, active,
                 layers_here: bool = True) -> None:
    items = display.items
    # The layers at this level, with the clips, opacities and offsets in force
    # where each begins, to be set again when it is painted later. A layer that
    # is no stacking context of its own (z-index: auto) is painted here at 0,
    # and the layers inside it are this level's too, looked for inside it.
    layers, in_force = [], []
    index = start
    while layers_here and index < end:
        item = items[index]
        if item is not None:
            kind = item[0]
            if kind == "layer_push":
                close = ends.get(index, end)
                context = item[2] if len(item) > 2 else True
                # equal z-indexes go by the elements' order in the page, not by
                # the order laid out: an absolute box is laid out after what
                # follows it, and had been painted over it
                order = item[3] if len(item) > 3 else index
                layers.append((item[1], index, close, _escaping(in_force), context, order))
                if context:
                    index = close + 1
                    continue
            elif kind in _PUSHES:
                in_force.append(item)
            elif kind in _POPS and in_force:
                in_force.pop()
        index += 1
    below = sorted((l for l in layers if l[0] < 0), key=lambda l: (l[0], l[5]))
    above = sorted((l for l in layers if l[0] >= 0), key=lambda l: (l[0], l[5]))
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
        elif kind not in ("layer_pop", "fixed_slot"):
            _draw(painter, item, here[-1], images)
        index += 1
    while depth:
        painter.restore()
        depth -= 1
    for layer in above:
        _paint_layer(painter, display, layer, shown, images, ends, scroll)


_MOVES = ("xform_push", "fixed_push", "sticky_push", "scroll_push")


def _item_bounds(item):
    """Where an item draws, in page terms; None if it cannot be told."""
    kind = item[0]
    if kind == "masked":
        return QRectF(item[1])
    if kind == "group":
        found = None
        for sub in item[1]:
            if sub is None:
                continue
            rect = _item_bounds(sub)
            if rect is None:
                return None
            found = rect if found is None else found.united(rect)
        return found if found is not None else QRectF()     # an empty group draws nothing
    if kind == "text":
        _k, x, baseline, text, font = item[:5]
        metrics = QFontMetricsF(font)
        return QRectF(x, baseline - metrics.ascent(), metrics.horizontalAdvance(text) + 2,
                      metrics.ascent() + metrics.descent())
    if kind == "shadow":
        box, shadows = item[1], item[2]
        reach = max((abs(dx) + abs(dy) + blur * 2 + abs(spread) for dx, dy, blur, spread, _c in shadows),
                    default=0.0)
        return box.adjusted(-reach, -reach, reach, reach)
    if kind in _PUSHES or kind in _POPS or kind in ("layer_push", "layer_pop", "fixed_slot", "extent"):
        return QRectF()
    rect = item[1] if len(item) > 1 else None
    return rect if isinstance(rect, QRectF) else None


def _layer_bounds(display, start: int, close: int):
    """All a layer draws, in page terms, worked out once for a display list;
    None if anything in it moves (a transform, a pinned or sticky box, a
    panel that scrolls), or cannot be told."""
    cache = getattr(display, "_bounds_cache", None)
    if cache is None or getattr(display, "_bounds_count", -1) != len(display.items):
        cache = {}
        display._bounds_cache, display._bounds_count = cache, len(display.items)
    if start in cache:
        return cache[start]
    from PyQt6.QtGui import QTransform

    found = QRectF()
    # transforms that do not move are followed, what they hold mapped through
    # them; anything that moves (an animation, a pinned or sticky box, a
    # panel's scroll) and the layer is always gone through
    stack = [QTransform()]
    for item in display.items[start + 1:close]:
        if item is None:
            continue
        kind = item[0]
        if kind == "xform_push":
            spec = item[1]
            if spec.get("animations") or spec.get("perspective"):
                found = None
                break
            try:
                transform, _opacity = _transform(spec)
            except Exception:                              # noqa: BLE001
                found = None
                break
            stack.append(transform * stack[-1])
            continue
        if kind == "xform_pop":
            if len(stack) > 1:
                stack.pop()
            continue
        if kind in _MOVES:
            found = None
            break
        rect = _item_bounds(item)
        if rect is None:
            found = None
            break
        if not rect.isEmpty():
            if len(stack) > 1:
                rect = stack[-1].mapRect(rect)
            found = rect if found.isEmpty() else found.united(rect)
    cache[start] = found
    return found


def _paint_layer(painter, display, layer, shown, images, ends, scroll) -> None:
    _z, start, close, in_force, context, _order = layer
    bounds = _layer_bounds(display, start, close)
    if bounds is not None and not any(item[0] in _MOVES for item in in_force):
        # nothing of it in view: not gone through at all. GitHub has 2,000
        # positioned boxes, each a layer, every one had been gone through,
        # its transform worked out, on every frame of a scroll
        if bounds.isEmpty() or not bounds.intersects(shown[-1]):
            return
    painter.save()
    here = list(shown)
    for item in in_force:
        painter.save()
        _apply(painter, display, item, here, scroll)
    # a stacking context paints its own layers; one that is not leaves them to
    # the level above, which has gathered them already
    _paint_range(painter, display, start + 1, close, here, images, ends, scroll, in_force,
                 layers_here=context)
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
    elif kind == "masked":
        _draw_masked(painter, item, visible, images)
    elif kind == "bgimage":
        rect = item[1]
        picture = images.get(item[2])
        if rect.intersects(visible) and picture is not None and picture is not False \
                and not (hasattr(picture, "isNull") and picture.isNull()):
            draw_background_picture(painter, rect, picture, item[3], item[4], item[5], item[6])
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
            if len(item) > 4 and item[4]:
                painter.drawPath(corner_path(rect, item[4]))
            else:
                painter.drawRoundedRect(rect, radius, radius)
            painter.restore()
    elif kind == "rframe":
        rect, rgba, widths, corners = item[1], item[2], item[3], item[4]
        if rect.intersects(visible) and rgba:
            top, right, bottom, left = widths
            inner = rect.adjusted(left, top, -right, -bottom)
            inner_corners = (max(0.0, corners[0] - max(top, left)), max(0.0, corners[1] - max(top, right)),
                             max(0.0, corners[2] - max(bottom, right)), max(0.0, corners[3] - max(bottom, left)))
            path = corner_path(rect, corners)
            if inner.width() > 0 and inner.height() > 0:
                path = path.subtracted(corner_path(inner, inner_corners))
            painter.save()
            painter.setRenderHint(painter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            colours = item[5] if len(item) > 5 else None
            if not colours:
                painter.setBrush(_colour(rgba))
                painter.drawPath(path)
            else:
                # each side in its own colour, meeting the next on the
                # diagonal from the corner, as browsers draw them
                from PyQt6.QtGui import QPainterPath, QPolygonF

                c = rect.center()
                tl, tr = rect.topLeft(), rect.topRight()
                br, bl = rect.bottomRight(), rect.bottomLeft()
                for corner_a, corner_b, colour, width in ((tl, tr, colours[0], top), (tr, br, colours[1], right),
                                                          (br, bl, colours[2], bottom), (bl, tl, colours[3], left)):
                    if width <= 0 or not colour or colour[3] == 0:
                        continue
                    wedge = QPainterPath()
                    wedge.addPolygon(QPolygonF([corner_a, corner_b, c]))
                    wedge.closeSubpath()
                    painter.setBrush(_colour(colour))
                    painter.drawPath(path.intersected(wedge))
            painter.restore()
    elif kind == "media":
        rect, element = item[1], item[2]
        if rect.intersects(visible):
            from .layout import media_key

            found = (images.get(MEDIA_KEY) or {}).get(media_key(element))
            if found is not None:
                found[0].draw(painter, rect)
            elif element.tag != "audio":
                painter.fillRect(rect, QColor(0, 0, 0))
    elif kind == "shadow":
        box, shadows, radius, inset = item[1], item[2], item[3], item[4]
        corners = item[5] if len(item) > 5 else None
        reach = max((abs(dx) + abs(dy) + blur + abs(spread) for dx, dy, blur, spread, _c in shadows),
                    default=0.0)
        if box.adjusted(-reach, -reach, reach, reach).intersects(visible):
            draw_shadows(painter, box, shadows, radius, inset, corners)
    elif kind == "dashed":
        rect, rgba, style = item[1], item[2], item[3]
        if rect.intersects(visible) and rgba:
            painter.save()
            across = rect.width() >= rect.height()
            thickness = rect.height() if across else rect.width()
            pen = QPen(_colour(rgba), thickness)
            _pattern(pen, style)
            painter.setPen(pen)
            if across:
                y = rect.center().y()
                painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            else:
                x = rect.center().x()
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.restore()
    elif kind == "rborder":
        rect, rgba, width, radius = item[1], item[2], item[3], item[4]
        if rect.intersects(visible) and rgba:
            painter.save()
            painter.setRenderHint(painter.RenderHint.Antialiasing, True)
            pen = QPen(_colour(rgba), width)
            if len(item) > 5:
                _pattern(pen, item[5])
            painter.setPen(pen)
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
        _k, rect, src, alt = item[:4]
        fit = item[4] if len(item) > 4 else None
        if not rect.intersects(visible):
            return
        picture = images.get(src)
        if picture is not None and not picture.isNull():
            target = _fitted(rect, picture, fit)
            clipped = fit is not None and not rect.contains(target)
            if clipped:
                painter.save()
                painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
            if isinstance(picture, SvgPicture):
                picture.render(painter, target)        # sharp at any size
            else:
                painter.drawImage(target, _scaled(picture, target, painter))
            if clipped:
                painter.restore()
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


# Pictures scaled to the size they are drawn at, kept: a large picture shown
# small (GitHub's) had been scaled down again on every frame of a scroll.
# Bounded by their pixels, the least recently drawn going first.
_SCALED: "dict" = {}
_SCALED_PIXELS = [0]
_SCALED_LIMIT = 48_000_000          # about 190 MB at 4 bytes a pixel


def _scaled(picture, target: QRectF, painter):
    from collections import OrderedDict

    global _SCALED
    try:
        ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        transform = painter.worldTransform()
        ratio *= max(abs(transform.m11()), abs(transform.m22())) or 1.0
    except Exception:                                      # noqa: BLE001
        ratio = 1.0
    width, height = round(target.width() * ratio), round(target.height() * ratio)
    if width <= 0 or height <= 0 or (abs(width - picture.width()) <= 1 and abs(height - picture.height()) <= 1):
        return picture
    if picture.width() * picture.height() < 64 * 64 or width * height > 16_000_000:
        return picture                       # small ones are quick; huge ones not kept
    if not isinstance(_SCALED, OrderedDict):
        _SCALED = OrderedDict()
    key = (picture.cacheKey(), width, height)
    found = _SCALED.get(key)
    if found is not None:
        _SCALED.move_to_end(key)
        return found
    made = picture.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    made.setDevicePixelRatio(1.0)
    _SCALED[key] = made
    _SCALED_PIXELS[0] += width * height
    while _SCALED_PIXELS[0] > _SCALED_LIMIT and len(_SCALED) > 1:
        (_k, old) = _SCALED.popitem(last=False)
        _SCALED_PIXELS[0] -= old.width() * old.height()
    return made


def _pattern(pen: QPen, style) -> None:
    """A dashed or dotted border's pen: dashes three times as long as the
    border is thick, dots round, as browsers draw them."""
    if style == "dashed":
        pen.setDashPattern([3.0, 2.0])
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    elif style == "dotted":
        pen.setDashPattern([0.01, 2.0])
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)


def _shape(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    if radius > 0.5:
        path.addRoundedRect(rect, radius, radius)
    else:
        path.addRect(rect)
    return path


def draw_shadows(painter, box: QRectF, shadows, radius: float, inset: bool, corners=None) -> None:
    """box-shadow: each shadow (dx, dy, blur, spread, rgba), the first on top.

    An outer shadow is drawn outside the box only, as browsers do (a box with
    no background shows none inside it); an inset one inside it only. A blur
    is drawn as layers, each a little larger and fainter, which comes close
    to the browsers' Gaussian at a fraction of the work."""
    painter.save()
    painter.setRenderHint(painter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    def shape(rect, change):
        # the box's outline grown or shrunk by change, each corner with it
        if corners:
            return corner_path(rect, [max(0.0, c + change) if c > 0.5 else 0.0 for c in corners])
        return _shape(rect, max(0.0, radius + change) if radius > 0.5 else 0.0)

    outline = corner_path(box, corners) if corners else _shape(box, radius)
    for dx, dy, blur, spread, rgba in reversed(shadows):
        if not rgba or rgba[3] == 0:
            continue
        reach = abs(dx) + abs(dy) + blur * 2 + abs(spread) + 2
        everything = QPainterPath()
        everything.addRect(box.adjusted(-reach, -reach, reach, reach))
        painter.save()
        painter.setClipPath(outline if inset else everything.subtracted(outline),
                            Qt.ClipOperation.IntersectClip)
        steps = 1 if blur < 1.0 else max(3, min(12, int(blur / 2)))
        alpha = rgba[3] / 255
        # each of n layers this faint, so that where all overlap it is the
        # shadow's own colour
        each = alpha if steps == 1 else 1 - (1 - alpha) ** (1 / steps)
        colour = QColor(rgba[0], rgba[1], rgba[2], max(1, round(each * 255)))
        for step in range(steps):
            # from the blur's far edge to its near one
            grow = 0.0 if steps == 1 else blur * (1 - 2 * (step + 0.5) / steps)
            if inset:
                hole = box.translated(dx, dy).adjusted(spread - grow, spread - grow,
                                                       -spread + grow, -spread + grow)
                path = QPainterPath()
                path.addRect(box.adjusted(-reach, -reach, reach, reach))
                if hole.width() > 0 and hole.height() > 0:
                    path = path.subtracted(corner_path(hole, [max(0.0, c - spread + grow) for c in corners])
                                           if corners else _shape(hole, max(0.0, radius - spread + grow)))
            else:
                grown = box.translated(dx, dy).adjusted(-spread - grow, -spread - grow,
                                                        spread + grow, spread + grow)
                if grown.width() <= 0 or grown.height() <= 0:
                    continue
                path = shape(grown, spread + grow)
            painter.fillPath(path, colour)
        painter.restore()
    painter.restore()


def _background_length(word: str, room: float):
    """A length in a background's size or position: px, %, or a bare number."""
    word = word.strip().lower()
    try:
        if word.endswith("%"):
            return room * float(word[:-1]) / 100.0
        if word.endswith("px"):
            return float(word[:-2])
        if word.endswith("rem") or word.endswith("em"):
            return float(word.rstrip("rem").rstrip("e") or 0) * 16.0
        return float(word)
    except ValueError:
        return None


def _draw_masked(painter, item, visible: QRectF, images) -> None:
    """A box's background through its CSS mask: drawn in a layer of its own,
    kept only where the mask's picture is opaque. Until the picture is in,
    nothing is drawn, as in browsers (an icon is not a filled square first)."""
    _kind, rect, source, size, position, repeat, parts = item
    if not rect.intersects(visible) or rect.width() < 0.5 or rect.height() < 0.5:
        return
    if isinstance(source, tuple) and source and source[0] == "ring":
        # only the ring between the box's edge and an inner box shows
        _ring, left, top, right, bottom, rounding = source
        hole = rect.adjusted(left, top, -right, -bottom)
        if isinstance(rounding, tuple):
            outer = corner_path(rect, rounding)
            shrink = max(left, top, right, bottom)
            inner = corner_path(hole, [max(0.0, r - shrink) for r in rounding])
        else:
            outer = _shape(rect, rounding)
            inner = _shape(hole, max(0.0, rounding - max(left, top, right, bottom)))
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.setClipPath(outer.subtracted(inner) if hole.width() > 0 and hole.height() > 0 else outer,
                            Qt.ClipOperation.IntersectClip)
        for part in parts:
            if part is not None:
                _draw(painter, part, visible, images)
        painter.restore()
        return
    picture = images.get(source)
    if picture is None or picture is False or (hasattr(picture, "isNull") and picture.isNull()):
        return
    from PyQt6.QtGui import QImage, QPainter

    scale = max(1.0, abs(painter.worldTransform().m11()) or 1.0)
    width, height = max(1, int(rect.width() * scale + 0.999)), max(1, int(rect.height() * scale + 0.999))
    if width * height > 16_000_000:
        return
    layer = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    layer.fill(Qt.GlobalColor.transparent)
    shape = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    shape.fill(Qt.GlobalColor.transparent)
    inner = QPainter(layer)
    inner.scale(scale, scale)
    inner.translate(-rect.x(), -rect.y())
    for part in parts:
        if part is not None:
            _draw(inner, part, rect, images)
    inner.end()
    cut = QPainter(shape)
    cut.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    cut.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    cut.scale(scale, scale)
    cut.translate(-rect.x(), -rect.y())
    draw_background_picture(cut, rect, picture, size, position, repeat, 0.0)
    cut.end()
    inner = QPainter(layer)
    inner.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    inner.drawImage(0, 0, shape)
    inner.end()
    painter.drawImage(rect, layer)


def draw_background_picture(painter, rect: QRectF, picture, size: str, position: str,
                            repeat: str, radius: float) -> None:
    """A background picture in its box, as CSS draws one: sized by
    background-size (cover, contain, auto, lengths), placed by
    background-position, repeated by background-repeat (repeat by default),
    and clipped to the box and its rounded corners."""
    if isinstance(picture, SvgPicture):
        own_w, own_h = float(picture.width()), float(picture.height())
    else:
        own_w, own_h = float(picture.width()), float(picture.height())
    if own_w <= 0 or own_h <= 0 or rect.width() <= 0 or rect.height() <= 0:
        return
    words = (size or "auto").strip().lower().split()
    if words[:1] == ["cover"]:
        scale = max(rect.width() / own_w, rect.height() / own_h)
        tile_w, tile_h = own_w * scale, own_h * scale
    elif words[:1] == ["contain"]:
        scale = min(rect.width() / own_w, rect.height() / own_h)
        tile_w, tile_h = own_w * scale, own_h * scale
    else:
        width_word = words[0] if words else "auto"
        height_word = words[1] if len(words) > 1 else "auto"
        tile_w = None if width_word == "auto" else _background_length(width_word, rect.width())
        tile_h = None if height_word == "auto" else _background_length(height_word, rect.height())
        if tile_w is None and tile_h is None:
            tile_w, tile_h = own_w, own_h
        elif tile_w is None:
            tile_w = tile_h * own_w / own_h
        elif tile_h is None:
            tile_h = tile_w * own_h / own_w
    if not tile_w or not tile_h or tile_w <= 0 or tile_h <= 0:
        return
    # where: keywords, percentages (of the room left), lengths
    spots = (position or "0% 0%").strip().lower().split()
    named_x = {"left": "0%", "center": "50%", "right": "100%"}
    named_y = {"top": "0%", "center": "50%", "bottom": "100%"}
    if len(spots) == 1:
        spots = [spots[0], "center"] if spots[0] not in ("top", "bottom") else ["center", spots[0]]
    first, second = spots[0], spots[1]
    if first in named_y and first != "center" or second in ("left", "right"):
        first, second = second, first
    first, second = named_x.get(first, first), named_y.get(second, second)

    def place(word, room, tile):
        if word.endswith("%"):
            try:
                return (room - tile) * float(word[:-1]) / 100.0
            except ValueError:
                return 0.0
        found = _background_length(word, room)
        return found if found is not None else 0.0
    x = rect.x() + place(first, rect.width(), tile_w)
    y = rect.y() + place(second, rect.height(), tile_h)
    repeat = (repeat or "repeat").strip().lower()
    across = repeat in ("repeat", "repeat-x", "space", "round") or repeat.startswith("repeat ")
    down = repeat in ("repeat", "repeat-y", "space", "round") or repeat.endswith(" repeat")
    painter.save()
    if radius and radius > 0.5:
        from PyQt6.QtGui import QPainterPath

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
    else:
        painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
    xs = [x]
    if across:
        start = x - tile_w * (int((x - rect.x()) / tile_w) + 1)
        xs = [start + tile_w * n for n in range(int(rect.width() / tile_w) + 3)]
    ys = [y]
    if down:
        start = y - tile_h * (int((y - rect.y()) / tile_h) + 1)
        ys = [start + tile_h * n for n in range(int(rect.height() / tile_h) + 3)]
    if len(xs) * len(ys) > 4000:                      # a tiny tile over a huge box: enough
        xs, ys = xs[:80], ys[:50]
    for top in ys:
        for left in xs:
            target = QRectF(left, top, tile_w, tile_h)
            if not target.intersects(rect):
                continue
            if isinstance(picture, SvgPicture):
                picture.render(painter, target)
            else:
                painter.drawImage(target, picture)
    painter.restore()


def fill_page_layers(painter, rect: QRectF, layers, images=None) -> None:
    """The background of <html> or <body>, over the whole page: its gradients
    and, since 1.8.16, its pictures. The pictures had been handed to the
    gradient code, which failed on them in the middle of painting; on Linux Qt
    then ended the process, and Merlin could not start."""
    for layer in reversed(list(layers or [])):
        try:
            if layer and layer[0] == "url":
                picture = (images or {}).get(layer[1])
                if picture is not None and picture is not False \
                        and not (hasattr(picture, "isNull") and picture.isNull()):
                    draw_background_picture(painter, rect, picture, layer[2], layer[3], layer[4], 0.0)
            elif layer and layer[0] in ("linear", "radial"):
                fill_gradient(painter, rect, layer)
        except Exception:                                  # noqa: BLE001
            continue                                       # a layer that cannot be drawn is left out


def _fitted(rect: QRectF, picture, fit) -> QRectF:
    """Where a picture is drawn in its box, by object-fit: stretched to it
    (fill), whole within it (contain), covering it and cropped (cover), at its
    own size (none), or the smaller of none and contain (scale-down); placed
    by object-position. Every picture had been stretched, sites' photos warped."""
    if fit is None:
        return rect
    kind, at_x, at_y = fit
    if isinstance(picture, SvgPicture):
        own_w, own_h = picture.width(), picture.height()
    else:
        own_w, own_h = float(picture.width()), float(picture.height())
    if own_w <= 0 or own_h <= 0 or rect.width() <= 0 or rect.height() <= 0:
        return rect
    contain = min(rect.width() / own_w, rect.height() / own_h)
    if kind == "contain":
        scale = contain
    elif kind == "cover":
        scale = max(rect.width() / own_w, rect.height() / own_h)
    elif kind == "none":
        scale = 1.0
    else:                                          # scale-down
        scale = min(1.0, contain)
    w, h = own_w * scale, own_h * scale
    return QRectF(rect.x() + (rect.width() - w) * at_x, rect.y() + (rect.height() - h) * at_y, w, h)


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
    places = [min(1.0, max(0.0, p)) for p in places]
    # Two stops at one place make a hard edge in CSS; Qt keeps only the last
    # colour given for a place, so the second is put a hair further on (or
    # the first a hair back, at the end). Google's star ratings, a gradient
    # from gold to grey at 56px, had come out all grey.
    for k in range(1, len(places)):
        if places[k] <= places[k - 1]:
            places[k] = places[k - 1] + 1e-6
    if places and places[-1] > 1.0:
        top = len(places) - 1
        for k in range(top, -1, -1):
            places[k] = min(places[k], 1.0 - (top - k) * 1e-6)
    return places


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
    if not spec or spec[0] not in ("linear", "radial"):
        return                                             # not a gradient: nothing to fill
    ratio = 1.0
    try:
        ratio = float(painter.device().devicePixelRatioF())
    except Exception:                                      # noqa: BLE001
        pass
    area = rect.width() * rect.height() * ratio * ratio
    # only a gradient opaque throughout is dithered: the dithering is drawn
    # with opaque blends, and a see-through gradient (GitHub's overlay, white
    # at 0 to 10%) came out as a solid sheet over everything under it
    opaque = all(colour[3] >= 255 for colour, _place in spec[-1])
    if opaque and DITHER_FROM <= area <= 12_000_000:
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
