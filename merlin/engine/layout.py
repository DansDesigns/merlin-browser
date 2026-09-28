"""Layout: where every box and every word goes, and what to paint there.

Works in normal flow, the way a document without floats or positioning is
laid out: blocks stack down the page, each as wide as it may be, and text
inside them is broken into lines by measuring it with the real fonts. The
result is a display list, plain drawing instructions in page coordinates,
plus the areas of links and the positions of elements with an id, so the view
can scroll to #fragments. Laying out again, at another width or zoom, means
calling this again on the same styled document.

Not yet here: floats, positioning, flexbox and grid, and tables, whose rows
and cells are laid out as ordinary blocks and inline text for now.
"""
from __future__ import annotations

import re

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFont, QFontMetricsF

from .dom import Element, Text

# Markers for the pieces of a line that are not text. Objects, not strings:
# a word of text is a string, and the word "image" or "anchor" in a page was
# once taken for one of these.
_IMAGE, _ANCHOR, _BREAK = object(), object(), object()
_FLOAT, _ABSOLUTE = object(), object()

BLOCK = {"block", "list-item", "table", "table-row", "table-row-group",
         "table-header-group", "table-footer-group", "flex", "grid", "table-caption",
         "table-cell"}


class DisplayList:
    """Drawing instructions in page coordinates, in painting order."""

    def __init__(self):
        self.items: list = []            # ("rect", QRectF, rgba) | ("text", x, y, str, QFont, rgba, deco)
        self.links: list = []            # (QRectF, href)
        self.anchors: dict = {}          # id -> y
        self.height = 0.0
        self.width = 0.0
        self.canvas = None               # the page's background colour
        self.fixed = None                # position: fixed, drawn against the window


class _Fonts:
    """QFont objects shared between identical styles, measured once."""

    GENERIC = {
        "serif": QFont.StyleHint.Serif, "sans-serif": QFont.StyleHint.SansSerif,
        "monospace": QFont.StyleHint.Monospace, "cursive": QFont.StyleHint.Cursive,
        "fantasy": QFont.StyleHint.Fantasy, "system-ui": QFont.StyleHint.SansSerif,
    }

    def __init__(self, zoom: float):
        self.zoom = zoom
        self._cache: dict = {}

    def get(self, style: dict):
        key = (style["font-family"], style["font-size"], style["font-weight"],
               style["font-style"])
        found = self._cache.get(key)
        if found:
            return found
        families = [f.strip().strip("'\"") for f in style["font-family"].split(",") if f.strip()]
        font = QFont()
        hint = QFont.StyleHint.SansSerif
        named = []
        for family in families:
            if family.lower() in self.GENERIC:
                hint = self.GENERIC[family.lower()]
                if family.lower() == "monospace":
                    named.extend(["DejaVu Sans Mono", "Consolas", "Courier New", "monospace"])
            else:
                named.append(family)
        if named:
            font.setFamilies(named)
        font.setStyleHint(hint)
        font.setPixelSize(max(1, round(style["font-size"] * self.zoom)))
        weight = min(900, max(100, round(style["font-weight"] / 100) * 100))
        font.setWeight(QFont.Weight(weight))
        font.setItalic(style["font-style"] in ("italic", "oblique"))
        metrics = QFontMetricsF(font)
        self._cache[key] = (font, metrics)
        return font, metrics


def _px(value, reference: float = 0.0, auto: float | None = 0.0) -> float | None:
    """A computed length as px: percentages of reference, 'auto' as auto."""
    if value == "auto" or value is None:
        return auto
    if isinstance(value, tuple):
        return reference * value[1] / 100
    return float(value)


class Layout:
    def __init__(self, document, styles: dict, width: float, zoom: float = 1.0,
                 images: dict | None = None, viewport_height: float = 768.0):
        self.viewport_height = viewport_height
        self._floats: list = []           # floats in the current formatting context
        self._absolute: list = [[]]       # waiting for their containing block
        self._fixed: list = []            # waiting for the window
        # src -> QImage once loaded, or False when it will never show
        # (blocked, or failed): such an image takes no room
        self.images = images or {}
        self._runs: list = []
        self._last_baseline = None
        self._measuring = False
        self._link = None                 # the href of an enclosing block-level link
        self.document = document
        self.styles = styles
        self.zoom = zoom
        self.width = width
        self.fonts = _Fonts(zoom)
        self.out = DisplayList()

    # ------------------------------------------------------------ entry
    def run(self) -> DisplayList:
        root = self.document.root
        root_style = self.styles[root]
        body = self.document.body
        # the root's background, or else the body's, paints the whole canvas
        for element in (root, body):
            colour = self.styles.get(element, {}).get("background-color")
            if colour and colour[3] > 0:
                self.out.canvas = colour
                break
        height = self._block(root, root_style, 0.0, 0.0, self.width, root_level=True)
        # what is positioned against the page, and then against the window
        page = QRectF(0.0, 0.0, self.width, self.viewport_height)
        for element, style, static_x, static_y in self._absolute[0]:
            self._place_absolute(element, style, page, static_x, static_y)
        if self._fixed:
            saved = self.out
            self.out = DisplayList()
            for element, style, static_x, static_y in self._fixed:
                self._place_absolute(element, style, page, static_x, static_y)
            fixed, self.out = self.out, saved
            self.out.fixed = fixed
        bottom = max((_bottom_edge(item) for item in self.out.items), default=0.0)
        self.out.height = max(height, bottom)
        self.out.width = self.width
        return self.out

    # ------------------------------------------------------------ blocks
    def _edges(self, style: dict, reference: float):
        z = self.zoom
        # a margin never set is 0; only an explicit "auto" is auto. Treating
        # the unset ones as auto centred tables and max-width blocks that had
        # asked for nothing of the sort.
        margin = []
        for side in ("top", "right", "bottom", "left"):
            value = style.get(f"margin-{side}")
            margin.append(0.0 if value is None else _px(value, reference, auto=None))
        padding = [(_px(style.get(f"padding-{s}"), reference) or 0.0) * z for s in ("top", "right", "bottom", "left")]
        border = []
        for side in ("top", "right", "bottom", "left"):
            kind = style.get(f"border-{side}-style", "none")
            width = _px(style.get(f"border-{side}-width"), reference) or 0.0
            border.append(width * z if kind not in ("none", "hidden") else 0.0)
        margin = [m * z if m is not None else None for m in margin]
        return margin, padding, border

    def _block(self, element: Element, style: dict, x: float, y: float,
               available: float, root_level: bool = False) -> float:
        """Lay out a block box at (x, y); returns its height including margins."""
        margin, padding, border = self._edges(style, available)
        width_value = _px(style.get("width"), available, auto=None)
        z = self.zoom
        if width_value is not None:
            width_value *= z
        max_width = _px(style.get("max-width"), available, auto=None)
        min_width = _px(style.get("min-width"), available, auto=None)
        horizontal_extra = padding[1] + padding[3] + border[1] + border[3]
        if width_value is None:
            m_left = margin[3] or 0.0
            m_right = margin[1] or 0.0
            content_width = max(0.0, available - m_left - m_right - horizontal_extra)
            if max_width is not None and content_width > max_width * z:
                content_width = max_width * z
                if margin[3] is None and margin[1] is None:
                    m_left = (available - content_width - horizontal_extra) / 2
        else:
            content_width = width_value
            if max_width is not None:
                content_width = min(content_width, max_width * z)
            if min_width is not None:
                content_width = max(content_width, min_width * z)
            left_auto, right_auto = margin[3] is None, margin[1] is None
            spare = available - content_width - horizontal_extra
            if left_auto and right_auto:
                m_left = max(0.0, spare / 2)
            elif left_auto:
                m_left = max(0.0, spare - (margin[1] or 0.0))
            else:
                m_left = margin[3] or 0.0
        box_x = x + m_left
        content_x = box_x + border[3] + padding[3]
        box_y = y + (margin[0] or 0.0)
        content_y = box_y + border[0] + padding[0]
        if element.id:
            self.out.anchors.setdefault(element.id, box_y)
        # a link laid out as a block: everything inside it leads there, and so
        # does the whole box, so a tile can be clicked anywhere on it
        href = element.attrs.get("href") if element.tag == "a" and "href" in element.attrs else None
        enclosing_link = self._link
        if href is not None:
            self._link = href
        min_height = self._length(style.get("min-height"), 0.0, vertical=True)
        max_height = self._length(style.get("max-height"), 0.0, vertical=True)
        fixed_height = self._length(style.get("height"), 0.0, vertical=True)

        # the background goes under the children, so its place is kept now
        background_index = len(self.out.items)
        links_before = len(self.out.links)
        self.out.items.append(None)
        position = style.get("position")
        positioned = position in ("relative", "absolute", "fixed", "sticky")
        if positioned:
            self._absolute.append([])        # descendants placed against this box
        # A block that starts a formatting context keeps its floats to itself,
        # and grows to hold them
        display = style.get("display")
        new_context = (root_level or style.get("_bfc") or style.get("float") in ("left", "right")
                       or position in ("absolute", "fixed")
                       or style.get("overflow") not in (None, "", "visible")
                       or display in ("flow-root", "table", "table-cell", "inline-block",
                                      "flex", "inline-flex"))
        if new_context:
            outer_floats, self._floats = self._floats, []

        if style.get("display") == "table":
            if width_value is None:
                # a table is only as wide as its contents need, up to the space
                natural = self._table_natural_width(element, style)
                if natural < content_width:
                    spare = content_width - natural
                    content_width = natural
                    if margin[3] is None and margin[1] is None:
                        box_x += spare / 2
                        content_x += spare / 2
            inner_height, first_baseline = self._table(element, style, content_x, content_y, content_width)
        elif style.get("display") in ("flex", "inline-flex"):
            inner_height, first_baseline = self._flex(
                element, style, content_x, content_y, content_width,
                room=fixed_height, least=min_height)
        else:
            inner_height, first_baseline = self._contents(element, style, content_x, content_y, content_width)
        # where this block's first line of text sits, for a list marker outside it
        self._last_baseline = first_baseline

        if new_context:
            float_bottom = max((f["bottom"] for f in self._floats), default=None)
            if float_bottom is not None:
                inner_height = max(inner_height, float_bottom - content_y)
            self._floats = outer_floats
        if fixed_height is not None:
            inner_height = fixed_height
        if min_height is not None:
            inner_height = max(inner_height, min_height)
        if max_height is not None:
            inner_height = min(inner_height, max_height)
        self._link = enclosing_link
        box_height = border[0] + padding[0] + inner_height + padding[2] + border[2]
        box_width = border[3] + padding[3] + content_width + padding[1] + border[1]
        box = QRectF(box_x, box_y, box_width, box_height)

        colour = style.get("background-color")
        radius = self._length(style.get("border-top-left-radius"), box_width) or 0.0
        radius = min(radius, box_width / 2, box_height / 2)
        paint = []
        if colour and colour[3] > 0 and not (root_level and self.out.canvas == colour):
            paint.append(("rrect", box, colour, radius) if radius > 0.5 else ("rect", box, colour))
        if radius > 0.5 and len(set(border)) == 1 and border[0] > 0:
            # one border all round, drawn along the rounded edge
            paint.append(("rborder", box, style.get("border-top-color") or style.get("color"),
                          border[0], radius))
        else:
            paint.extend(self._borders(style, box, border))
        self.out.items[background_index] = ("group", paint)
        if href is not None:
            self.out.links.append((box, href))

        # position: relative, and translate(), move the box once laid out;
        # nothing around it moves
        dx = dy = 0.0
        if position == "relative":
            left = self._length(style.get("left"), available)
            right = self._length(style.get("right"), available)
            top = self._length(style.get("top"), 0.0, vertical=True)
            bottom = self._length(style.get("bottom"), 0.0, vertical=True)
            dx = left if left is not None else (-right if right is not None else 0.0)
            dy = top if top is not None else (-bottom if bottom is not None else 0.0)
        translate = style.get("transform")
        if isinstance(translate, tuple):
            tx, ty = translate
            dx += box_width * tx[1] / 100 if isinstance(tx, tuple) else tx * z
            dy += box_height * ty[1] / 100 if isinstance(ty, tuple) else ty * z
        if dx or dy:
            self._shift(background_index, links_before, dy, dx=dx)
            box = box.translated(dx, dy)
        if positioned:
            waiting = self._absolute.pop()
            inside = QRectF(box.left() + border[3], box.top() + border[0],
                            box.width() - border[1] - border[3],
                            box.height() - border[0] - border[2])
            for child, child_style, static_x, static_y in waiting:
                self._place_absolute(child, child_style, inside, static_x + dx, static_y + dy)

        if style.get("display") == "list-item" and first_baseline is not None:
            self._marker(element, style, content_x, first_baseline)
        return (margin[0] or 0.0) + box_height + (margin[2] or 0.0)

    def _borders(self, style: dict, box: QRectF, border: list) -> list:
        found = []
        top, right, bottom, left = border
        def colour(side):
            return style.get(f"border-{side}-color") or style.get("color", (0, 0, 0, 255))
        if top:
            found.append(("rect", QRectF(box.left(), box.top(), box.width(), top), colour("top")))
        if bottom:
            found.append(("rect", QRectF(box.left(), box.bottom() - bottom, box.width(), bottom), colour("bottom")))
        if left:
            found.append(("rect", QRectF(box.left(), box.top(), left, box.height()), colour("left")))
        if right:
            found.append(("rect", QRectF(box.right() - right, box.top(), right, box.height()), colour("right")))
        return found

    def _marker(self, element: Element, style: dict, content_x: float, baseline: float) -> None:
        kind = style.get("list-style-type", "disc")
        if kind == "none":
            return
        font, metrics = self.fonts.get(style)
        if kind in ("decimal", "lower-alpha", "upper-alpha", "lower-roman", "upper-roman"):
            siblings = [c for c in element.parent.children
                        if isinstance(c, Element) and self.styles[c].get("display") == "list-item"]
            number = siblings.index(element) + 1 + int(element.parent.attrs.get("start", "1") or 1) - 1
            if kind == "lower-alpha":
                text = chr(ord("a") + (number - 1) % 26) + "."
            elif kind == "upper-alpha":
                text = chr(ord("A") + (number - 1) % 26) + "."
            elif kind in ("lower-roman", "upper-roman"):
                text = _roman(number) + "."
                text = text.lower() if kind == "lower-roman" else text
            else:
                text = f"{number}."
        else:
            text = {"disc": "\u2022", "circle": "\u25e6", "square": "\u25aa"}.get(kind, "\u2022")
        width = metrics.horizontalAdvance(text + " ")
        self.out.items.append(("text", content_x - width, baseline, text, font,
                               style.get("color", (0, 0, 0, 255)), "none"))

    # ---------------------------------------------------------- contents
    def _is_block(self, node) -> bool:
        """A block in the normal flow: floats and positioned boxes are not."""
        if not isinstance(node, Element):
            return False
        style = self.styles[node]
        return (style.get("display") in BLOCK and style.get("float") not in ("left", "right")
                and style.get("position") not in ("absolute", "fixed"))

    @staticmethod
    def _out_of_flow(style: dict) -> str:
        if style.get("position") in ("absolute", "fixed"):
            return "positioned"
        if style.get("float") in ("left", "right"):
            return "float"
        return ""

    def _control_text(self, element: Element, style: dict):
        """What a form control shows, and in what style; None for nothing.

        Controls cannot be used yet; this only draws them, so a page's search
        box or button is at least there to see: its value, or its placeholder
        in a softer colour.
        """
        kind = element.attrs.get("type", "text").lower() if element.tag == "input" else element.tag
        if kind in ("hidden",):
            return None
        if kind in ("checkbox",):
            return ("\u2611" if "checked" in element.attrs else "\u2610", style)
        if kind in ("radio",):
            return ("\u25c9" if "checked" in element.attrs else "\u25cb", style)
        value = element.attrs.get("value", "")
        if element.tag == "textarea":
            value = element.text()
        if element.tag == "select":
            chosen = [o for o in element.elements() if o.tag == "option"]
            picked = [o for o in chosen if "selected" in o.attrs] or chosen[:1]
            value = picked[0].text().strip() if picked else ""
        if kind in ("submit", "button", "reset") and not value:
            value = {"submit": "Submit", "reset": "Reset"}.get(kind, "")
        if value:
            return (value, style)
        placeholder = element.attrs.get("placeholder", "")
        softer = dict(style)
        colour = style.get("color", (0, 0, 0, 255))
        softer["color"] = (colour[0], colour[1], colour[2], max(60, colour[3] * 55 // 100))
        # a non-breaking space still gives an empty box the height of a line
        return (placeholder or "\u00a0", softer)

    def _contents(self, element: Element, style: dict, x: float, y: float, width: float):
        """Children of a block: returns (height, baseline of the first line)."""
        if element.tag in ("input", "textarea", "select"):
            shown = self._control_text(element, style)
            if shown is None:
                return 0.0, None
            return self._inline([Text(shown[0])], shown[1], x, y, width)
        children = [c for c in element.children
                    if not (isinstance(c, Element) and self.styles[c].get("display") == "none")]
        if not any(self._is_block(c) for c in children):
            return self._inline(children, style, x, y, width)
        # some children are blocks: runs of inline content between them
        # become anonymous blocks of their own
        cursor = y
        pending_margin = 0.0
        first_baseline = None
        run: list = []

        def flush_run():
            nonlocal cursor, pending_margin, first_baseline
            if run and any(not (isinstance(n, Text) and not n.data.strip()) for n in run):
                cursor += pending_margin
                pending_margin = 0.0
                used, baseline = self._inline(run, style, x, cursor, width)
                if first_baseline is None:
                    first_baseline = baseline
                cursor += used
            run.clear()

        for child in children:
            out_of_flow = self._out_of_flow(self.styles[child]) if isinstance(child, Element) else ""
            if out_of_flow == "positioned":
                self._wait_for_placement(child, self.styles[child], x, cursor + pending_margin)
                continue
            if out_of_flow == "float" and not any(
                    not (isinstance(n, Text) and not n.data.strip()) for n in run):
                self._place_float(child, self.styles[child], x, cursor + pending_margin, width)
                continue
            if self._is_block(child):
                flush_run()
                child_style = self.styles[child]
                margin, _p, _b = self._edges(child_style, width)
                top = margin[0] or 0.0
                bottom = margin[2] or 0.0
                collapsed = max(pending_margin, top)
                start = cursor + collapsed - top
                clear = child_style.get("clear")
                if clear in ("left", "right", "both"):
                    below = self._floats_bottom(clear)
                    if below is not None and start + top < below:
                        start = below - top
                # a block starting its own context sits beside floats, not under them
                block_x, block_width = x, width
                if self._floats and (child_style.get("overflow") not in (None, "", "visible")
                                     or child_style.get("display") in ("flow-root", "table",
                                                                       "flex")):
                    left, right = self._room(start + top, 1.0, x, width)
                    block_x, block_width = left, right - left
                self._last_baseline = None
                used = self._block(child, child_style, block_x, start, block_width)
                if first_baseline is None:
                    first_baseline = self._last_baseline
                cursor = start + used - bottom
                pending_margin = bottom
            else:
                run.append(child)
        flush_run()
        cursor += pending_margin
        return cursor - y, first_baseline

    # ------------------------------------------------------------ lengths
    def _length(self, value, reference: float, vertical: bool = False):
        """A computed length in page pixels, zoom included; None for auto.

        Percentages are of reference, which is already in page pixels. A
        percentage height with nothing definite to refer to is treated as
        auto, as browsers do.
        """
        if value is None or value == "auto":
            return None
        if isinstance(value, tuple):
            if vertical and reference <= 0:
                return None
            return reference * value[1] / 100
        try:
            return float(value) * self.zoom
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------ flexbox
    def _flex_items(self, element: Element):
        """The container's items, in order: its elements, and loose text."""
        items = []
        for child in element.children:
            if isinstance(child, Element):
                child_style = self.styles[child]
                if child_style.get("display") == "none":
                    continue
                if child_style.get("position") in ("absolute", "fixed"):
                    # not a flex item, by the specification: placed on its own
                    # once the container is laid out, from the container's start
                    self._wait_for_placement(child, child_style, *self._flex_origin)
                    continue
                items.append((child, child_style))
            elif isinstance(child, Text) and child.data.strip():
                # text directly in a flex container becomes an item of its own
                anonymous = Element("span")
                anonymous.append(Text(child.data))
                anonymous.parent = element
                inherited = {k: v for k, v in self.styles[element].items()
                             if k in ("color", "font-family", "font-size", "font-weight",
                                      "font-style", "line-height", "text-align",
                                      "white-space", "text-decoration")}
                inherited.update({"display": "block"})
                items.append((anonymous, inherited))
        # order, keeping document order among equals
        return sorted(items, key=lambda pair: pair[1].get("order", 0))

    def _natural_width(self, element: Element, style: dict, narrowest: bool) -> float:
        """An item's content width with no line breaks, or broken at every chance."""
        return _Measure(self).extent(element, style, 1.0 if narrowest else 100000.0)

    def _item_box(self, element: Element, style: dict, x: float, y: float,
                  border_width: float, border_height: float | None = None) -> float:
        """Lay out one flex item at a size already decided; returns its height."""
        _m, padding, border = self._edges(style, border_width)
        z = self.zoom
        item = dict(style)
        item["width"] = max(0.0, border_width - padding[1] - padding[3] - border[1] - border[3]) / z
        item["min-width"] = item["max-width"] = None
        for side in ("top", "right", "bottom", "left"):
            item[f"margin-{side}"] = 0.0
        if border_height is not None:
            item["height"] = max(0.0, border_height - padding[0] - padding[2]
                                 - border[0] - border[2]) / z
            item["min-height"] = item["max-height"] = None
        display = item.get("display")
        # an item is always laid out as a block, whatever it was
        item["display"] = "flex" if display in ("flex", "inline-flex") else \
            ("table" if display == "table" else "block")
        item["_bfc"] = True                  # each item keeps its floats to itself
        item["float"] = None
        return self._block(element, item, x, y, border_width)

    def _scratch_height(self, element, style, border_width) -> float:
        """How tall an item would be at a width, without drawing it."""
        saved, saved_runs = self.out, self._runs
        held = (self._floats, self._absolute, self._fixed)
        self._floats, self._absolute, self._fixed = [], [[]], []
        self.out = DisplayList()
        self._runs = []
        try:
            return self._item_box(element, style, 0.0, 0.0, border_width)
        finally:
            self.out, self._runs = saved, saved_runs
            self._floats, self._absolute, self._fixed = held

    def _flex(self, element: Element, style: dict, x: float, y: float, width: float,
              room: float | None = None, least: float | None = None):
        """Lay out a flex container's items; returns (height, first baseline)."""
        self._flex_origin = (x, y)
        items = self._flex_items(element)
        if not items:
            return 0.0, None
        direction = style.get("flex-direction", "row")
        if direction.startswith("column"):
            return self._flex_column(items, style, x, y, width, room, least,
                                     reverse=direction.endswith("reverse"))
        return self._flex_row(items, style, x, y, width, room, least,
                              reverse=direction.endswith("reverse"))

    @staticmethod
    def _justify(kind: str, spare: float, count: int, reverse: bool):
        """Where the first item starts, and the extra space between items."""
        if reverse:
            kind = {"flex-start": "flex-end", "start": "end", "left": "right",
                    "flex-end": "flex-start", "end": "start", "right": "left"}.get(kind, kind)
        if spare <= 0 or count == 0:
            return 0.0, 0.0
        if kind in ("flex-end", "end", "right"):
            return spare, 0.0
        if kind == "center":
            return spare / 2, 0.0
        if kind == "space-between":
            return (0.0, spare / (count - 1)) if count > 1 else (0.0, 0.0)
        if kind == "space-around":
            return spare / count / 2, spare / count
        if kind == "space-evenly":
            return spare / (count + 1), spare / (count + 1)
        return 0.0, 0.0

    def _flex_row(self, items, style, x, y, width, room, least, reverse=False):
        wrap = style.get("flex-wrap", "nowrap") in ("wrap", "wrap-reverse")
        column_gap = self._length(style.get("column-gap"), width) or 0.0
        row_gap = self._length(style.get("row-gap"), width) or 0.0
        justify = style.get("justify-content", "flex-start")
        align_items = style.get("align-items", "stretch")

        entries = []
        for element, item_style in items:
            margin, padding, border = self._edges(item_style, width)
            edges = padding[1] + padding[3] + border[1] + border[3]
            fixed = self._length(item_style.get("width"), width)
            basis_value = item_style.get("flex-basis", "auto")
            basis = self._length(basis_value, width) if basis_value not in (None, "auto") else None
            if basis is None:
                basis = fixed if fixed is not None else self._natural_width(element, item_style, False)
            # The automatic minimum is the smaller of the narrowest content and
            # any width given: an item with width: 300px and little in it can
            # still shrink, rather than pushing the row off a narrow screen.
            narrowest = self._natural_width(element, item_style, True)
            if fixed is not None:
                narrowest = min(narrowest, fixed)
            low = self._length(item_style.get("min-width"), width)
            high = self._length(item_style.get("max-width"), width)
            # an item's automatic minimum is its narrowest content
            floor = low if low is not None else narrowest
            size = max(basis, low or 0.0)
            if high is not None:
                size = min(size, high)
            entries.append({
                "element": element, "style": item_style, "margin": margin,
                "edges": edges, "base": basis, "size": size, "floor": floor, "ceiling": high,
                "grow": item_style.get("flex-grow", 0.0), "shrink": item_style.get("flex-shrink", 1.0),
            })
        if reverse:
            entries.reverse()

        def outer(entry):
            m = entry["margin"]
            return entry["size"] + entry["edges"] + (m[1] or 0.0) + (m[3] or 0.0)

        # break into lines
        lines, line, used = [], [], 0.0
        for entry in entries:
            need = outer(entry) + (column_gap if line else 0.0)
            if wrap and line and used + need > width + 0.01:
                lines.append(line)
                line, used = [], 0.0
                need = outer(entry)
            line.append(entry)
            used += need
        if line:
            lines.append(line)

        cursor = y
        first_baseline = None
        single = len(lines) == 1
        for number, line in enumerate(lines):
            gaps = column_gap * (len(line) - 1)
            free = width - sum(outer(e) for e in line) - gaps
            if free > 0 and sum(e["grow"] for e in line) > 0:
                total = sum(e["grow"] for e in line)
                for entry in line:
                    entry["size"] += free * entry["grow"] / total
                    if entry["ceiling"] is not None:
                        entry["size"] = min(entry["size"], entry["ceiling"])
            elif free < 0:
                weights = [e["shrink"] * e["base"] for e in line]
                total = sum(weights)
                if total > 0:
                    for entry, weight in zip(line, weights):
                        entry["size"] = max(entry["floor"], entry["size"] + free * weight / total)
            spare = width - sum(outer(e) for e in line) - gaps
            autos = sum((e["margin"][3] is None) + (e["margin"][1] is None) for e in line)
            if spare > 0 and autos:
                share, spare = spare / autos, 0.0
            else:
                share = 0.0
            start, between = self._justify(justify, spare, len(line), reverse)

            heights = []
            for entry in line:
                fixed_height = self._length(entry["style"].get("height"), 0.0, vertical=True)
                border_width = entry["size"] + entry["edges"]
                height = fixed_height if fixed_height is not None else \
                    self._scratch_height(entry["element"], entry["style"], border_width)
                heights.append(height)
            cross = max((h + (e["margin"][0] or 0.0) + (e["margin"][2] or 0.0)
                         for h, e in zip(heights, line)), default=0.0)
            if single:
                # one line fills the container's height, if it has one
                cross = max(cross, room or 0.0, least or 0.0)

            pen = x + start
            for entry, height in zip(line, heights):
                m = entry["margin"]
                left = m[3] if m[3] is not None else share
                right = m[1] if m[1] is not None else share
                border_width = entry["size"] + entry["edges"]
                align = entry["style"].get("align-self", "auto")
                if align in ("auto", "normal", ""):
                    align = align_items
                top_margin, bottom_margin = m[0] or 0.0, m[2] or 0.0
                forced = None
                if align in ("stretch", "normal") and \
                        self._length(entry["style"].get("height"), 0.0, vertical=True) is None:
                    forced = max(height, cross - top_margin - bottom_margin)
                    offset = 0.0
                else:
                    taken = height + top_margin + bottom_margin
                    offset = {"flex-end": cross - taken, "end": cross - taken,
                              "center": (cross - taken) / 2}.get(align, 0.0)
                if m[0] is None and m[2] is None:          # auto margins across: centre
                    offset = (cross - height) / 2
                    top_margin = 0.0
                self._last_baseline = None
                self._item_box(entry["element"], entry["style"], pen + left,
                               cursor + offset + top_margin, border_width, forced)
                if first_baseline is None:
                    first_baseline = self._last_baseline
                pen += left + border_width + right + column_gap + between
            cursor += cross + (row_gap if number < len(lines) - 1 else 0.0)
        return cursor - y, first_baseline

    def _flex_column(self, items, style, x, y, width, room, least, reverse=False):
        row_gap = self._length(style.get("row-gap"), width) or 0.0
        justify = style.get("justify-content", "flex-start")
        align_items = style.get("align-items", "stretch")
        entries = []
        for element, item_style in items:
            margin, padding, border = self._edges(item_style, width)
            edges = padding[1] + padding[3] + border[1] + border[3]
            align = item_style.get("align-self", "auto")
            if align in ("auto", "normal", ""):
                align = align_items
            left, right = margin[3] or 0.0, margin[1] or 0.0
            fixed = self._length(item_style.get("width"), width)
            if fixed is not None:
                border_width = fixed + edges
            elif align in ("stretch", "normal") and not (margin[3] is None and margin[1] is None):
                border_width = width - left - right
            else:
                border_width = min(self._natural_width(element, item_style, False) + edges,
                                   width - left - right)
            low = self._length(item_style.get("min-width"), width)
            high = self._length(item_style.get("max-width"), width)
            if high is not None:
                border_width = min(border_width, high + edges)
            if low is not None:
                border_width = max(border_width, low + edges)
            basis_value = item_style.get("flex-basis", "auto")
            basis = self._length(basis_value, room or 0.0, vertical=True) \
                if basis_value not in (None, "auto") else None
            fixed_height = self._length(item_style.get("height"), room or 0.0, vertical=True)
            height = basis if basis is not None else (
                fixed_height if fixed_height is not None
                else self._scratch_height(element, item_style, border_width))
            entries.append({"element": element, "style": item_style, "margin": margin,
                            "width": border_width, "height": height, "align": align,
                            "grow": item_style.get("flex-grow", 0.0)})
        if reverse:
            entries.reverse()

        def outer(entry):
            m = entry["margin"]
            return entry["height"] + (m[0] or 0.0) + (m[2] or 0.0)

        gaps = row_gap * (len(entries) - 1)
        available = room if room is not None else least
        grown = set()
        if available is not None:
            free = available - sum(outer(e) for e in entries) - gaps
            total = sum(e["grow"] for e in entries)
            if free > 0 and total > 0:
                for index, entry in enumerate(entries):
                    if entry["grow"]:
                        entry["height"] += free * entry["grow"] / total
                        grown.add(index)
            spare = available - sum(outer(e) for e in entries) - gaps
        else:
            spare = 0.0
        start, between = self._justify(justify, spare, len(entries), reverse)
        cursor = y + start
        first_baseline = None
        for index, entry in enumerate(entries):
            m = entry["margin"]
            left, right = m[3], m[1]
            spare_across = width - entry["width"] - (left or 0.0) - (right or 0.0)
            if left is None and right is None:
                offset = spare_across / 2 + 0.0
                left = 0.0
            elif entry["align"] == "center":
                offset = spare_across / 2
            elif entry["align"] in ("flex-end", "end", "right"):
                offset = spare_across
            else:
                offset = 0.0
            cursor += m[0] or 0.0
            self._last_baseline = None
            forced = entry["height"] if index in grown else None
            used = self._item_box(entry["element"], entry["style"], x + (left or 0.0) + offset,
                                  cursor, entry["width"], forced)
            if first_baseline is None:
                first_baseline = self._last_baseline
            cursor += max(used, entry["height"] if forced is not None else used) + (m[2] or 0.0)
            if index < len(entries) - 1:
                cursor += row_gap + between
        return cursor - y, first_baseline

    # ------------------------------------------------------------ images
    def _image_size(self, element, style: dict, available: float):
        """Width and height for an image: given, natural, or kept in proportion."""
        picture = self.images.get(element.attrs.get("src", "").strip())
        if picture is False:
            return 0.0, 0.0                     # blocked, or failed: takes no room
        natural = (picture.width(), picture.height()) if picture is not None else None
        w = _px(style.get("width"), available, auto=None)
        h = _px(style.get("height"), 0.0, auto=None) \
            if not isinstance(style.get("height"), tuple) else None
        if w is None and h is None and natural:
            w, h = float(natural[0]), float(natural[1])
        elif w is not None and h is None:
            h = w * natural[1] / natural[0] if natural and natural[0] else 0.0
        elif h is not None and w is None:
            w = h * natural[0] / natural[1] if natural and natural[1] else 0.0
        if w is None or h is None:
            return 0.0, 0.0                     # not loaded yet, and no size given
        w, h = w * self.zoom, h * self.zoom
        limit = _px(style.get("max-width"), available, auto=None)
        if limit is not None:
            limit = limit * self.zoom if not isinstance(style.get("max-width"), tuple) else limit
            if w > limit > 0:
                h = h * limit / w
                w = limit
        return w, h

    # ------------------------------------------------------------ tables
    def _table_grid(self, table):
        """Rows of the table as lists of cells, and the caption if any."""
        rows, caption = [], None

        def take_rows(node):
            for child in node.children:
                if not isinstance(child, Element):
                    continue
                display = self.styles[child].get("display")
                if display == "none":
                    continue
                if display == "table-row":
                    rows.append([c for c in child.children
                                 if isinstance(c, Element)
                                 and self.styles[c].get("display") == "table-cell"])
                    rows[-1] = (child, rows[-1])
                elif display in ("table-row-group", "table-header-group",
                                 "table-footer-group"):
                    take_rows(child)

        for child in table.children:
            if isinstance(child, Element) and self.styles[child].get("display") == "table-caption":
                caption = child
        take_rows(table)
        return rows, caption

    @staticmethod
    def _span(cell) -> int:
        try:
            return max(1, min(50, int(cell.attrs.get("colspan", "1") or 1)))
        except ValueError:
            return 1

    def _spacing(self, style: dict) -> float:
        if style.get("border-collapse") == "collapse":
            return 0.0
        value = str(style.get("border-spacing", "0")).split()[0]
        found = re.match(r"^([\d.]+)", value)
        return float(found.group(1)) * self.zoom if found else 0.0

    def _columns(self, table, style: dict):
        """Each column's narrowest and widest content, cell edges included."""
        rows, _caption = self._table_grid(table)
        count = max((sum(self._span(c) for c in cells) for _r, cells in rows), default=0)
        low, high = [0.0] * count, [0.0] * count
        spanning = []
        measure = _Measure(self)
        for _row, cells in rows:
            column = 0
            for cell in cells:
                span = self._span(cell)
                cell_style = self.styles[cell]
                _m, padding, border = self._edges(cell_style, 0.0)
                edges = padding[1] + padding[3] + border[1] + border[3]
                fixed = _px(cell_style.get("width"), 0.0, auto=None) \
                    if not isinstance(cell_style.get("width"), tuple) else None
                narrow = measure.extent(cell, cell_style, 1.0) + edges
                wide = measure.extent(cell, cell_style, 100000.0) + edges
                if fixed is not None:
                    narrow = max(narrow, fixed * self.zoom + edges)
                    wide = max(narrow, fixed * self.zoom + edges)
                if span == 1 and column < count:
                    low[column] = max(low[column], narrow)
                    high[column] = max(high[column], wide)
                elif span > 1:
                    spanning.append((column, min(span, count - column), narrow, wide))
                column += span
        # A cell spanning columns needing more than they give it: share the
        # extra out across the columns it spans.
        spacing = self._spacing(style)
        for start, span, narrow, wide in spanning:
            if span <= 0:
                continue
            gap = spacing * (span - 1)
            for sizes, need in ((low, narrow), (high, wide)):
                have = sum(sizes[start:start + span]) + gap
                if need > have:
                    extra = (need - have) / span
                    for index in range(start, start + span):
                        sizes[index] += extra
        return low, high

    def _table_natural_width(self, table, style: dict) -> float:
        _low, high = self._columns(table, style)
        spacing = self._spacing(style)
        return sum(high) + spacing * (len(high) + 1)

    def _table(self, table, style: dict, x: float, y: float, width: float):
        rows, caption = self._table_grid(table)
        spacing = self._spacing(style)
        low, high = self._columns(table, style)
        count = len(low)
        cursor = y
        first_baseline = None
        if caption is not None:
            cursor += self._block(caption, self.styles[caption], x, cursor, width)
        if not count:
            return cursor - y, first_baseline
        room = max(0.0, width - spacing * (count + 1))
        if sum(high) <= room:
            # everything fits on one line: share out any spare room
            extra = room - sum(high)
            total = sum(high) or 1.0
            widths = [h + extra * (h / total) for h in high]
        elif sum(low) >= room:
            widths = list(low)
        else:
            spare = room - sum(low)
            give = [h - l for l, h in zip(low, high)]
            total = sum(give) or 1.0
            widths = [l + spare * g / total for l, g in zip(low, give)]
        cursor += spacing
        for row, cells in rows:
            row_style = self.styles[row]
            row_start = len(self.out.items)
            self.out.items.append(None)          # the row's background, once known
            placed = []                          # (cell, x, width, start items, links, height)
            column = 0
            for cell in cells:
                span = self._span(cell)
                if column >= count:
                    break
                cell_x = x + spacing + sum(widths[:column]) + spacing * column
                cell_width = sum(widths[column:column + span]) + spacing * (span - 1)
                cell_style = self.styles[cell]
                _m, padding, border = self._edges(cell_style, cell_width)
                inner_width = max(0.0, cell_width - padding[1] - padding[3] - border[1] - border[3])
                items_before, links_before = len(self.out.items), len(self.out.links)
                self.out.items.append(None)      # the cell's background and borders
                outer_floats, self._floats = self._floats, []
                inner, baseline = self._contents(
                    cell, cell_style, cell_x + border[3] + padding[3],
                    cursor + border[0] + padding[0], inner_width)
                cell_floats = max((f["bottom"] for f in self._floats), default=None)
                if cell_floats is not None:
                    inner = max(inner, cell_floats - (cursor + border[0] + padding[0]))
                self._floats = outer_floats
                height = inner + padding[0] + padding[2] + border[0] + border[2]
                fixed = _px(cell_style.get("height"), 0.0, auto=None) \
                    if not isinstance(cell_style.get("height"), tuple) else None
                if fixed is not None:
                    height = max(height, fixed * self.zoom)
                if first_baseline is None:
                    first_baseline = baseline
                placed.append((cell_style, cell_x, cell_width, items_before, links_before,
                               height, inner, padding, border,
                               len(self.out.items), len(self.out.links)))
                column += span
            row_height = max((p[5] for p in placed), default=0.0)
            fixed_row = _px(row_style.get("height"), 0.0, auto=None) \
                if not isinstance(row_style.get("height"), tuple) else None
            if fixed_row is not None:
                row_height = max(row_height, fixed_row * self.zoom)
            for (cell_style, cell_x, cell_width, items_before, links_before,
                 height, inner, padding, border, items_after, links_after) in placed:
                align = cell_style.get("vertical-align", "middle")
                content_room = row_height - padding[0] - padding[2] - border[0] - border[2]
                shift = {"top": 0.0, "bottom": content_room - inner,
                         "baseline": 0.0}.get(align, (content_room - inner) / 2)
                if shift > 0.5:
                    # this cell's own content only: moving everything laid out
                    # since it began moved the later cells in the row too
                    self._shift(items_before + 1, links_before, shift,
                                items_after, links_after)
                box = QRectF(cell_x, cursor, cell_width, row_height)
                paint = []
                colour = cell_style.get("background-color")
                if colour and colour[3] > 0:
                    paint.append(("rect", box, colour))
                paint.extend(self._borders(cell_style, box, border))
                self.out.items[items_before] = ("group", paint)
            colour = row_style.get("background-color")
            row_box = QRectF(x + spacing, cursor, max(0.0, width - 2 * spacing), row_height)
            self.out.items[row_start] = ("group", [("rect", row_box, colour)]
                                         if colour and colour[3] > 0 else [])
            cursor += row_height + spacing
        return cursor - y, first_baseline

    def _shift(self, items_from: int, links_from: int, dy: float,
               items_to: int | None = None, links_to: int | None = None,
               dx: float = 0.0) -> None:
        """Move a stretch of what was laid out by dx across and dy down."""
        def moved(item):
            if item is None:
                return None
            kind = item[0]
            if kind == "group":
                return ("group", [moved(sub) for sub in item[1]])
            if kind in ("rect", "image", "rrect", "rborder"):
                return (kind, item[1].translated(dx, dy)) + tuple(item[2:])
            if kind == "text":
                return (kind, item[1] + dx, item[2] + dy) + item[3:]
            return item
        for index in range(items_from, len(self.out.items) if items_to is None else items_to):
            self.out.items[index] = moved(self.out.items[index])
        for index in range(links_from, len(self.out.links) if links_to is None else links_to):
            rect, href = self.out.links[index]
            self.out.links[index] = (rect.translated(dx, dy), href)

    # ------------------------------------------------------------ inline
    def _items(self, nodes, style: dict, link: str | None, out: list) -> None:
        """Flatten inline content into (kind, payload, style, link) items."""
        for node in nodes:
            if isinstance(node, Text):
                out.append(("text", node.data, style, link))
            elif isinstance(node, Element):
                node_style = self.styles[node]
                display = node_style.get("display")
                if display == "none":
                    continue
                if node.tag == "br":
                    out.append(("break", "", node_style, link))
                    continue
                kind = self._out_of_flow(node_style)
                if kind:
                    out.append(("absolute" if kind == "positioned" else "float",
                                node, node_style, link))
                    continue
                if node.tag == "img":
                    out.append(("image", node, node_style, link))
                    continue
                if node.tag in ("input", "textarea", "select"):
                    shown = self._control_text(node, node_style)
                    if shown is not None:
                        out.append(("text", shown[0], shown[1], link))
                    continue
                href = node.attrs.get("href") if node.tag == "a" and "href" in node.attrs else link
                if node.id:
                    out.append(("anchor", node.id, node_style, href))
                if display in BLOCK:
                    # a block inside inline content: on a line of its own
                    out.append(("break", "", node_style, href))
                    self._items(node.children, node_style, href, out)
                    out.append(("break", "", node_style, href))
                else:
                    self._items(node.children, node_style, href, out)

    def _inline(self, nodes, block_style: dict, x: float, y: float, width: float):
        """Lay out inline content, a line at a time; returns (height, first baseline).

        One pass, building each line and placing it before starting the next,
        because floats make a line's room depend on how far down it sits.
        """
        items: list = []
        self._items(nodes, block_style, self._link, items)
        pre = block_style.get("white-space") in ("pre", "pre-wrap", "pre-line", "break-spaces")
        pieces = []
        at_line_start = True
        for kind, payload, style, link in items:
            if kind == "anchor":
                pieces.append((_ANCHOR, payload, style, link))
                continue
            if kind == "break":
                pieces.append((_BREAK, style, link))
                at_line_start = True
                continue
            if kind == "image":
                pieces.append((_IMAGE, payload, style, link))
                at_line_start = False
                continue
            if kind in ("float", "absolute"):
                pieces.append((_FLOAT if kind == "float" else _ABSOLUTE, payload, style, link))
                continue
            text = payload
            if style.get("white-space") in ("pre", "pre-wrap", "break-spaces"):
                for index, line in enumerate(text.split("\n")):
                    if index:
                        pieces.append((_BREAK, style, link))
                    if line:
                        pieces.append((line.replace("\t", "    "), style, link))
                continue
            text = re.sub(r"\s+", " ", text)
            for token in re.findall(r"\S+| ", text):
                if token == " " and (at_line_start or (pieces and pieces[-1][0] == " ")):
                    continue
                pieces.append((token, style, link))
                at_line_start = False

        _font, block_metrics = self.fonts.get(block_style)
        guess = block_metrics.lineSpacing()
        align = block_style.get("text-align", "left")
        state = {"cursor": y, "first": None}
        line: list = []
        used = 0.0
        anchors_here: list = []
        deferred: list = []                 # floats met mid-line, placed after it
        span = list(self._room(y, guess, x, width))

        def emit(force: bool) -> None:
            nonlocal line, used
            while line and line[-1][0] == "text" and line[-1][2] == " ":
                used -= line[-1][1]
                line.pop()
            cursor = state["cursor"]
            if line:
                ascent = max(p[5] for p in line)
                descent = max(p[6] for p in line)
                line_height = max(max(p[7] for p in line), ascent + descent)
                baseline = cursor + (line_height - (ascent + descent)) / 2 + ascent
                if state["first"] is None:
                    state["first"] = baseline
                room = span[1] - span[0]
                # while measuring, alignment would only push text towards the
                # far end of a very long line and make its content look enormous
                offset = 0.0 if self._measuring else {
                    "center": (room - used) / 2, "right": room - used,
                    "end": room - used}.get(align, 0.0)
                pen = span[0] + max(0.0, offset)
                for anchor in anchors_here:
                    self.out.anchors.setdefault(anchor, cursor)
                runs = []
                run = None
                for part in line:
                    if part[0] == "image":
                        _k, w, element, style, link, h, _d, _lh, _f = part
                        rect = QRectF(pen, baseline - h, w, h)
                        self.out.items.append(("image", rect, element.attrs.get("src", "").strip(),
                                               element.attrs.get("alt", "")))
                        if link:
                            self.out.links.append((rect, link))
                        pen += w
                        run = None
                        continue
                    _k, advance, text, style, link, _a, _d, _lh, font = part
                    if run is not None and run[0] is style and run[1] == link:
                        run[3] += text
                        run[4] += advance
                    else:
                        run = [style, link, pen, text, advance, font]
                        runs.append(run)
                    pen += advance
                for style, link, start_x, text, advance, font in runs:
                    # an inline element's own background, behind its text only
                    background = style.get("background-color")
                    if background and background[3] > 0 and style is not block_style:
                        _f, metrics = self.fonts.get(style)
                        self.out.items.append(("rect", QRectF(
                            start_x, baseline - metrics.ascent(), advance,
                            metrics.ascent() + metrics.descent()), background))
                    self.out.items.append(("text", start_x, baseline, text, font,
                                           style.get("color", (0, 0, 0, 255)),
                                           style.get("text-decoration", "none")))
                    if link:
                        self.out.links.append((QRectF(start_x, cursor, advance, line_height), link))
                cursor += line_height
            elif force:
                # an empty line from a <br>: as tall as the block's font
                for anchor in anchors_here:
                    self.out.anchors.setdefault(anchor, cursor)
                cursor += guess
            else:
                for anchor in anchors_here:
                    self.out.anchors.setdefault(anchor, cursor)
            anchors_here.clear()
            line = []
            used = 0.0
            state["cursor"] = cursor
            for piece in deferred:
                self._place_float(piece[1], piece[2], x, cursor, width)
            deferred.clear()
            span[:] = self._room(cursor, guess, x, width)

        for piece in pieces:
            kind = piece[0]
            if kind is _ANCHOR:
                anchors_here.append(piece[1])
                continue
            if kind is _BREAK:
                emit(True)
                continue
            if kind is _FLOAT:
                if line:
                    deferred.append(piece)
                else:
                    self._place_float(piece[1], piece[2], x, state["cursor"], width)
                    span[:] = self._room(state["cursor"], guess, x, width)
                continue
            if kind is _ABSOLUTE:
                self._wait_for_placement(piece[1], piece[2], span[0] + used, state["cursor"])
                continue
            if kind is _IMAGE:
                element, style, link = piece[1], piece[2], piece[3]
                w, h = self._image_size(element, style, width)
                if w <= 0 or h <= 0:
                    continue
                if line and used + w > span[1] - span[0]:
                    emit(False)
                line.append(("image", w, element, style, link, h, 0.0, h, None))
                used += w
                continue
            text, style, link = piece
            if text == " " and not line:
                continue
            font, metrics = self.fonts.get(style)
            advance = metrics.horizontalAdvance(text)
            wraps = not pre and style.get("white-space") != "nowrap"
            if wraps and text != " " and line and used + advance > span[1] - span[0]:
                emit(False)
            # a word with no room beside floats goes down below them
            steps = 0
            while (wraps and not line and advance > span[1] - span[0] + 0.01
                   and self._floats_beside(state["cursor"], guess, x, width) and steps < 50):
                state["cursor"] = self._next_float_bottom(state["cursor"], guess, x, width)
                span[:] = self._room(state["cursor"], guess, x, width)
                steps += 1
            size = style["font-size"] * self.zoom
            lh = style.get("line-height", ("normal",))
            if lh[0] == "factor":
                line_height = lh[1] * size
            elif lh[0] == "px":
                line_height = lh[1] * self.zoom
            else:
                line_height = metrics.lineSpacing()
            line.append(("text", advance, text, style, link, metrics.ascent(),
                         metrics.descent(), line_height, font))
            used += advance
        emit(False)
        return state["cursor"] - y, state["first"]

    # ------------------------------------------------------------ floats
    def _room(self, y: float, height: float, x: float, width: float):
        """The room left beside the floats, from y for height: (left, right)."""
        left, right = x, x + width
        for f in self._floats:
            if f["top"] < y + height and f["bottom"] > y:
                if f["side"] == "left":
                    left = max(left, f["right"])
                else:
                    right = min(right, f["left"])
        return left, max(left, right)

    def _floats_beside(self, y: float, height: float, x: float, width: float) -> bool:
        return any(f["top"] < y + height and f["bottom"] > y for f in self._floats)

    def _next_float_bottom(self, y: float, height: float, x: float, width: float) -> float:
        bottoms = [f["bottom"] for f in self._floats
                   if f["top"] < y + height and f["bottom"] > y]
        return min(bottoms) if bottoms else y + height

    def _floats_bottom(self, side: str):
        bottoms = [f["bottom"] for f in self._floats if side == "both" or f["side"] == side]
        return max(bottoms) if bottoms else None

    def _place_float(self, element: Element, style: dict, x: float, y: float, width: float) -> None:
        """Place a float against the left or right edge, as high as it fits."""
        side = style.get("float")
        margin, padding, border = self._edges(style, width)
        m_top, m_right, m_bottom, m_left = (m or 0.0 for m in margin)
        edges = padding[1] + padding[3] + border[1] + border[3]
        if element.tag == "img":
            image_w, image_h = self._image_size(element, style, width)
            if image_w <= 0 or image_h <= 0:
                return
            border_width = image_w
        else:
            fixed = self._length(style.get("width"), width)
            if fixed is not None:
                border_width = fixed + edges
            else:
                # as wide as its content, up to the room there is
                border_width = min(self._natural_width(element, style, False) + edges,
                                   max(0.0, width - m_left - m_right))
            low = self._length(style.get("min-width"), width)
            high = self._length(style.get("max-width"), width)
            if high is not None:
                border_width = min(border_width, high + edges)
            if low is not None:
                border_width = max(border_width, low + edges)
        outer = border_width + m_left + m_right
        top = y
        for _step in range(100):
            left, right = self._room(top, 1.0, x, width)
            if right - left >= outer - 0.01 or not self._floats_beside(top, 1.0, x, width):
                break
            top = self._next_float_bottom(top, 1.0, x, width)
        left, right = self._room(top, 1.0, x, width)
        box_x = left + m_left if side == "left" else right - m_right - border_width
        box_y = top + m_top
        if element.tag == "img":
            rect = QRectF(box_x, box_y, border_width, image_h)
            self.out.items.append(("image", rect, element.attrs.get("src", "").strip(),
                                   element.attrs.get("alt", "")))
            if self._link:
                self.out.links.append((rect, self._link))
            height = image_h
        else:
            height = self._item_box(element, style, box_x, box_y, border_width)
        self._floats.append({"side": side, "top": top, "left": box_x - m_left,
                             "right": box_x + border_width + m_right,
                             "bottom": box_y + height + m_bottom})

    # --------------------------------------------------------- positioning
    def _wait_for_placement(self, element: Element, style: dict, static_x: float,
                            static_y: float) -> None:
        """Hold a positioned box until its containing block is laid out."""
        if style.get("position") == "fixed":
            self._fixed.append((element, style, static_x, static_y))
        else:
            self._absolute[-1].append((element, style, static_x, static_y))

    def _place_absolute(self, element: Element, style: dict, block: QRectF,
                        static_x: float, static_y: float) -> None:
        """Place an absolutely positioned box against its containing block."""
        margin, padding, border = self._edges(style, block.width())
        m_top, m_right, m_bottom, m_left = (m or 0.0 for m in margin)
        edges_x = padding[1] + padding[3] + border[1] + border[3]
        edges_y = padding[0] + padding[2] + border[0] + border[2]
        left = self._length(style.get("left"), block.width())
        right = self._length(style.get("right"), block.width())
        top = self._length(style.get("top"), block.height(), vertical=True)
        bottom = self._length(style.get("bottom"), block.height(), vertical=True)
        fixed = self._length(style.get("width"), block.width())
        if element.tag == "img":
            image_w, image_h = self._image_size(element, style, block.width())
            border_width = image_w
        elif fixed is not None:
            border_width = fixed + edges_x
        elif left is not None and right is not None:
            border_width = max(0.0, block.width() - left - right - m_left - m_right)
        else:
            # shrink to fit: as wide as its content, up to the room there is
            room = block.width() - (left or 0.0) - (right or 0.0) - m_left - m_right
            border_width = min(self._natural_width(element, style, False) + edges_x,
                               max(0.0, room))
        high = self._length(style.get("max-width"), block.width())
        low = self._length(style.get("min-width"), block.width())
        if high is not None:
            border_width = min(border_width, high + edges_x)
        if low is not None:
            border_width = max(border_width, low + edges_x)
        if left is not None:
            box_x = block.left() + left + m_left
        elif right is not None:
            box_x = block.right() - right - m_right - border_width
        else:
            box_x = static_x + m_left
        forced = None
        fixed_height = self._length(style.get("height"), block.height(), vertical=True)
        if fixed_height is not None:
            forced = fixed_height + edges_y
        elif top is not None and bottom is not None:
            forced = max(0.0, block.height() - top - bottom - m_top - m_bottom)
        box_y = block.top() + top + m_top if top is not None else static_y + m_top
        items_before, links_before = len(self.out.items), len(self.out.links)
        if element.tag == "img":
            if image_w <= 0 or image_h <= 0:
                return
            rect = QRectF(box_x, box_y, image_w, image_h)
            self.out.items.append(("image", rect, element.attrs.get("src", "").strip(),
                                   element.attrs.get("alt", "")))
            height = image_h
        else:
            height = self._item_box(element, style, box_x, box_y, border_width, forced)
        if top is None and bottom is not None:
            # held by its bottom edge: now that its height is known, move it there
            target = block.bottom() - bottom - m_bottom - height
            self._shift(items_before, links_before, target - box_y)

class _Measure:
    """Lay something out into a scratch display list, to learn how wide it is."""

    def __init__(self, layout):
        self.layout = layout

    def extent(self, element, style, width: float) -> float:
        saved = self.layout.out
        was_measuring = self.layout._measuring
        held = (self.layout._floats, self.layout._absolute, self.layout._fixed)
        self.layout._floats, self.layout._absolute, self.layout._fixed = [], [[]], []
        self.layout.out = DisplayList()
        self.layout._measuring = True
        try:
            if style.get("display") in ("flex", "inline-flex"):
                self.layout._flex(element, style, 0.0, 0.0, width)
            else:
                self.layout._contents(element, style, 0.0, 0.0, width)
            right = 0.0
            for item in self.layout.out.items:
                right = max(right, _right_edge(item))
            return right
        finally:
            self.layout.out = saved
            self.layout._measuring = was_measuring
            self.layout._floats, self.layout._absolute, self.layout._fixed = held


def _bottom_edge(item) -> float:
    if item is None:
        return 0.0
    if item[0] == "group":
        return max((_bottom_edge(sub) for sub in item[1]), default=0.0)
    if item[0] in ("rect", "image", "rrect", "rborder"):
        return item[1].bottom()
    if item[0] == "text":
        return item[2] + QFontMetricsF(item[4]).descent()
    return 0.0


def _right_edge(item) -> float:
    if item is None:
        return 0.0
    if item[0] == "group":
        return max((_right_edge(sub) for sub in item[1]), default=0.0)
    if item[0] in ("rect", "image", "rrect", "rborder"):
        return item[1].right()
    if item[0] == "text":
        return item[1] + QFontMetricsF(item[4]).horizontalAdvance(item[3])
    return 0.0


def _roman(number: int) -> str:
    values = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
              (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for value, letters in values:
        while number >= value:
            out += letters
            number -= value
    return out
