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

BLOCK = {"block", "list-item", "table", "table-row", "table-row-group",
         "table-header-group", "table-footer-group", "flex", "grid", "table-caption"}


class DisplayList:
    """Drawing instructions in page coordinates, in painting order."""

    def __init__(self):
        self.items: list = []            # ("rect", QRectF, rgba) | ("text", x, y, str, QFont, rgba, deco)
        self.links: list = []            # (QRectF, href)
        self.anchors: dict = {}          # id -> y
        self.height = 0.0
        self.width = 0.0
        self.canvas = None               # the page's background colour


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
    def __init__(self, document, styles: dict, width: float, zoom: float = 1.0):
        self._runs: list = []
        self._last_baseline = None
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
        self.out.height = height
        self.out.width = self.width
        return self.out

    # ------------------------------------------------------------ blocks
    def _edges(self, style: dict, reference: float):
        z = self.zoom
        margin = [(_px(style.get(f"margin-{s}"), reference, auto=None)) for s in ("top", "right", "bottom", "left")]
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

        # the background goes under the children, so its place is kept now
        background_index = len(self.out.items)
        self.out.items.append(None)

        inner_height, first_baseline = self._contents(element, style, content_x, content_y, content_width)
        # where this block's first line of text sits, for a list marker outside it
        self._last_baseline = first_baseline

        height_value = _px(style.get("height"), 0.0, auto=None)
        if height_value is not None and not isinstance(style.get("height"), tuple):
            inner_height = height_value * z
        box_height = border[0] + padding[0] + inner_height + padding[2] + border[2]
        box_width = border[3] + padding[3] + content_width + padding[1] + border[1]
        box = QRectF(box_x, box_y, box_width, box_height)

        colour = style.get("background-color")
        paint = []
        if colour and colour[3] > 0 and not (root_level and self.out.canvas == colour):
            paint.append(("rect", box, colour))
        paint.extend(self._borders(style, box, border))
        self.out.items[background_index] = ("group", paint)

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
        return isinstance(node, Element) and self.styles[node].get("display") in BLOCK

    def _contents(self, element: Element, style: dict, x: float, y: float, width: float):
        """Children of a block: returns (height, baseline of the first line)."""
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
            if self._is_block(child):
                flush_run()
                child_style = self.styles[child]
                margin, _p, _b = self._edges(child_style, width)
                top = margin[0] or 0.0
                bottom = margin[2] or 0.0
                collapsed = max(pending_margin, top)
                start = cursor + collapsed - top
                self._last_baseline = None
                used = self._block(child, child_style, x, start, width)
                if first_baseline is None:
                    first_baseline = self._last_baseline
                cursor = start + used - bottom
                pending_margin = bottom
            else:
                run.append(child)
        flush_run()
        cursor += pending_margin
        return cursor - y, first_baseline

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
                if node.tag == "img":
                    out.append(("image", node, node_style, link))
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
        items: list = []
        self._items(nodes, block_style, None, items)
        pre = block_style.get("white-space") in ("pre", "pre-wrap", "pre-line", "break-spaces")
        # words and spaces, each with its style
        pieces = []          # (text, style, link) or ("\n", ...) for breaks, or image
        at_line_start = True
        for kind, payload, style, link in items:
            if kind == "anchor":
                pieces.append(("anchor", payload, style, link))
                continue
            if kind == "break":
                pieces.append(("\n", style, link))
                at_line_start = True
                continue
            if kind == "image":
                pieces.append(("image", payload, style, link))
                at_line_start = False
                continue
            text = payload
            if style.get("white-space") in ("pre", "pre-wrap", "break-spaces"):
                for index, line in enumerate(text.split("\n")):
                    if index:
                        pieces.append(("\n", style, link))
                    if line:
                        pieces.append((line.replace("\t", "    "), style, link))
                continue
            text = re.sub(r"\s+", " ", text)
            for token in re.findall(r"\S+| ", text):
                if token == " " and (at_line_start or (pieces and pieces[-1][0] == " ")):
                    continue
                pieces.append((token, style, link))
                at_line_start = False

        lines = []           # each: list of (x, width, text, style, link, ascent, descent, lh, font) or images
        line: list = []
        line_width = 0.0
        anchors_here = []

        def finish(force=False):
            nonlocal line, line_width
            while line and line[-1][2] == " ":
                line_width -= line[-1][1]
                line.pop()
            if line or force:
                lines.append((line, line_width, list(anchors_here)))
            anchors_here.clear()
            line = []
            line_width = 0.0

        for piece in pieces:
            if piece[0] == "anchor":
                anchors_here.append(piece[1])
                continue
            if piece[0] == "\n":
                finish(force=True)
                continue
            if piece[0] == "image":
                element, style, link = piece[1], piece[2], piece[3]
                w = _px(style.get("width"), width, auto=None)
                h = _px(style.get("height"), 0, auto=None)
                w = w if w is not None else float(element.attrs.get("width", "0") or 0)
                h = h if h is not None else float(element.attrs.get("height", "0") or 0)
                w, h = w * self.zoom, h * self.zoom
                if w <= 0 or h <= 0:
                    continue
                if line and line_width + w > width:
                    finish()
                line.append(("image", w, element, style, link, h, 0.0, h, None))
                line_width += w
                continue
            text, style, link = piece
            font, metrics = self.fonts.get(style)
            advance = metrics.horizontalAdvance(text)
            if text == " " and not line:
                continue
            if not pre and text != " " and line and line_width + advance > width:
                finish()
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
            line_width += advance
        finish()

        align = block_style.get("text-align", "left")
        cursor = y
        first_baseline = None
        for parts, used, anchors in lines:
            if not parts:
                # an empty line from a <br>: as tall as the block's font
                font, metrics = self.fonts.get(block_style)
                cursor += metrics.lineSpacing()
                continue
            ascent = max(p[5] for p in parts)
            descent = max(p[6] for p in parts)
            line_height = max(max(p[7] for p in parts), ascent + descent)
            baseline = cursor + (line_height - (ascent + descent)) / 2 + ascent
            if first_baseline is None:
                first_baseline = baseline
            offset = {"center": (width - used) / 2, "right": width - used,
                      "end": width - used}.get(align, 0.0)
            pen = x + max(0.0, offset)
            for anchor in anchors:
                self.out.anchors.setdefault(anchor, cursor)
            run = None                  # the text run being extended
            for part in parts:
                if part[0] == "image":
                    _k, w, element, style, link, h, _d, _lh, _f = part
                    rect = QRectF(pen, baseline - h, w, h)
                    self.out.items.append(("image", rect, element.attrs.get("src", ""),
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
                    self._runs.append((run, baseline, cursor, line_height))
                pen += advance
            for run, run_baseline, top, height in self._runs:
                style, link, start_x, text, advance, font = run
                # an inline element's own background, behind its text only
                background = style.get("background-color")
                if background and background[3] > 0 and style is not block_style:
                    _f, metrics = self.fonts.get(style)
                    self.out.items.append(("rect", QRectF(
                        start_x, run_baseline - metrics.ascent(), advance,
                        metrics.ascent() + metrics.descent()), background))
                self.out.items.append(("text", start_x, run_baseline, text, font,
                                       style.get("color", (0, 0, 0, 255)),
                                       style.get("text-decoration", "none")))
                if link:
                    self.out.links.append((QRectF(start_x, top, advance, height), link))
            self._runs = []
            cursor += line_height
        return cursor - y, first_baseline


def _roman(number: int) -> str:
    values = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
              (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for value, letters in values:
        while number >= value:
            out += letters
            number -= value
    return out
