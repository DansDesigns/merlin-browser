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

from . import forms
from .dom import Element, Text

# Markers for the pieces of a line that are not text. Objects, not strings:
# a word of text is a string, and the word "image" or "anchor" in a page was
# once taken for one of these.
_IMAGE, _ANCHOR, _BREAK = object(), object(), object()
_FLOAT, _ABSOLUTE = object(), object()
_BOX = object()                          # an inline-block, or an inline <svg>

INLINE_BOXES = ("inline-block", "inline-flex", "inline-grid", "inline-table")

BLOCK = {"block", "list-item", "table", "table-row", "table-row-group",
         "table-header-group", "table-footer-group", "flex", "grid", "table-caption",
         "table-cell", "grid"}


class LabelTarget(str):
    """A <label> in the links list: clicking it acts on its control."""

    def __new__(cls, element):
        made = super().__new__(cls, "label")
        made.element = element
        return made


LAYOUT_BUDGET = 6.0         # seconds a layout pass measures precisely


class SummaryTarget(str):
    """A <summary> in the links list: clicking it opens or closes its <details>."""

    def __new__(cls, element):
        made = super().__new__(cls, "summary")
        made.element = element
        return made


def _visually_hidden(style: dict) -> bool:
    """clip: rect(0 0 0 0) or clip-path: inset(50%): hidden, though laid out."""
    clip = str(style.get("clip", "")).replace(",", " ").split()
    if clip and clip[0].startswith("rect(") and all(
            re.sub(r"[^0-9.]", "", c or "0") in ("", "0", "0.") for c in
            " ".join(clip).replace("rect(", "").replace(")", "").split()):
        return True
    path = str(style.get("clip-path", "")).replace(" ", "").lower()
    return path in ("inset(50%)", "inset(100%)", "polygon(0000)")


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
        self.simplified = False          # the time budget ran out: estimates used
        self.canvas_gradients = []       # a gradient on <html> or <body>: the whole page
        self.boxes = []                  # (rect, element) of each box: what a click lands on
        self.animated = False            # whether anything on it is animated: paint keeps time
        self.animated_boxes = []         # where: only those in view are drawn again
        self.sticky = []                 # position: sticky boxes, each a dict (see Layout)

    def sticky_offset(self, info: dict, scroll: float) -> float:
        """How far a sticky box is moved down with the page scrolled by scroll.

        It keeps to its place until reaching its top offset from the window's
        top, then holds there, but never past the bottom of its parent.
        """
        wanted = scroll + info["top"] - info["start"]
        room = info.get("limit", info["start"] + info["height"]) - (info["start"] + info["height"])
        return max(0.0, min(wanted, max(0.0, room)))


class _Fonts:
    """QFont objects shared between identical styles, measured once."""

    GENERIC = {
        "serif": QFont.StyleHint.Serif, "sans-serif": QFont.StyleHint.SansSerif,
        "monospace": QFont.StyleHint.Monospace, "cursive": QFont.StyleHint.Cursive,
        "fantasy": QFont.StyleHint.Fantasy, "system-ui": QFont.StyleHint.SansSerif,
    }

    def __init__(self, zoom: float, aliases=None):
        self.zoom = zoom
        self._cache: dict = {}
        # a site's web fonts: its CSS family name -> the families Qt loaded them as
        self.aliases = aliases or {}

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
                # A generic name stands for real fonts, here in the list: given
                # to Qt as only a hint, it was passed over, and a font named
                # after it, such as Noto Color Emoji on Google, drew the text,
                # its wide spaces opening gaps between every word.
                named.extend(_installed_for(family.lower()))
            else:
                named.extend(self.aliases.get(family.lower(), [family]))
        # emoji fonts are for emoji: after every font for text, never first
        emoji = [f for f in named if "emoji" in f.lower()]
        named = [f for f in named if "emoji" not in f.lower()]
        if not any(_installed(f) for f in named):
            named.extend(_installed_for("sans-serif" if hint != QFont.StyleHint.Serif else "serif"))
        named.extend(emoji)
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


FIELD_TAGS = ("input", "textarea", "select")
_FAMILIES = None
_FOR_GENERIC = {
    "sans-serif": ["Segoe UI", "Arial", "Helvetica", "Liberation Sans", "DejaVu Sans", "Noto Sans", "Ubuntu", "Cantarell"],
    "system-ui": ["Segoe UI", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans", "Arial"],
    "serif": ["Times New Roman", "Georgia", "Liberation Serif", "DejaVu Serif", "Noto Serif", "Times"],
    "monospace": ["Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono", "Courier New", "Noto Sans Mono"],
    "cursive": ["Comic Sans MS", "URW Chancery L"],
    "fantasy": ["Impact", "Papyrus"],
}


def _installed(family: str) -> bool:
    global _FAMILIES
    if _FAMILIES is None:
        from PyQt6.QtGui import QFontDatabase

        _FAMILIES = {name.lower() for name in QFontDatabase.families()}
    return family.lower() in _FAMILIES


def _installed_for(generic: str) -> list:
    """The installed fonts a generic family (sans-serif, serif...) means here,
    the system's own font among them for sans-serif and system-ui."""
    found = [name for name in _FOR_GENERIC.get(generic, []) if _installed(name)]
    if generic in ("sans-serif", "system-ui", "cursive", "fantasy"):
        from PyQt6.QtGui import QFontDatabase

        system = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
        if system and system not in found and "emoji" not in system.lower():
            found.append(system)
    return found


def _px(value, reference: float = 0.0, auto: float | None = 0.0) -> float | None:
    """A computed length as px: percentages of reference, 'auto' as auto."""
    if value == "auto" or value is None:
        return auto
    if isinstance(value, tuple):
        return reference * value[1] / 100
    return float(value)



def _origin(value, width: float, height: float):
    """transform-origin (or perspective-origin) as a point in the box.

    Keywords (left, center, right, top, bottom), lengths and percentages, as
    CSS gives them; 50% 50% when not set.
    """
    words = str(value or "").strip().lower().split()
    x, y = width / 2, height / 2
    spots = {"left": ("x", 0.0), "right": ("x", 1.0), "top": ("y", 0.0), "bottom": ("y", 1.0)}
    horizontal_set = False
    for index, word in enumerate(words[:2]):
        if word in spots:
            axis, share = spots[word]
            if axis == "x":
                x = width * share
                horizontal_set = True
            else:
                y = height * share
            continue
        if word == "center":
            continue
        try:
            if word.endswith("%"):
                amount = float(word[:-1]) / 100
                value_px = amount * (width if (index == 0 and not horizontal_set) else height)
            else:
                value_px = float(word[:-2]) if word.endswith("px") else float(word)
        except ValueError:
            continue
        if index == 0:
            x = value_px
            horizontal_set = True
        else:
            y = value_px
    return x, y


def _is_percentage(value) -> bool:
    """A width given as a share of its container (50%, or calc() with one)."""
    if isinstance(value, tuple) and value:
        if value[0] == "%":
            return True
        if value[0] == "mix" or (value[0] == "calc" and "%" in str(value)):
            return True
    return isinstance(value, str) and value.strip().endswith("%")


def _layer_of(value, index: int) -> str:
    """One background layer's part of a comma list (size, position, repeat),
    the list repeated if it is shorter than the layers, as CSS has it."""
    parts = _split_layers(str(value or ""))
    return parts[index % len(parts)].strip() if parts else ""


def _split_layers(value: str) -> list:
    out, depth, current = [], 0, []
    for character in value:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            out.append("".join(current))
            current = []
        else:
            current.append(character)
    out.append("".join(current))
    return [part for part in out if part.strip()]


def _object_fit(style: dict):
    """How an image fills its box: object-fit and object-position, as given."""
    fit = str(style.get("object-fit") or "fill").strip().lower()
    if fit not in ("contain", "cover", "none", "scale-down"):
        return None                              # fill: stretched to the box, as it was
    where = str(style.get("object-position") or "50% 50%").strip().lower().split()
    named = {"left": 0.0, "top": 0.0, "center": 0.5, "right": 1.0, "bottom": 1.0}

    def part(word, default):
        if word in named:
            return named[word]
        if word.endswith("%"):
            try:
                return float(word[:-1]) / 100.0
            except ValueError:
                return default
        return default
    if len(where) == 1:
        x = part(where[0], 0.5)
        y = 0.5 if where[0] not in ("top", "bottom") else named[where[0]]
        if where[0] in ("top", "bottom"):
            x = 0.5
    else:
        first, second = where[0], where[1]
        if first in ("top", "bottom") or second in ("left", "right"):
            first, second = second, first
        x, y = part(first, 0.5), part(second, 0.5)
    return (fit, x, y)

class Layout:
    def __init__(self, document, styles: dict, width: float, zoom: float = 1.0,
                 images: dict | None = None, viewport_height: float = 768.0,
                 live_controls: bool = False, font_aliases: dict | None = None):
        # In a view, form fields are real widgets: layout draws their boxes,
        # leaves their text to the widget, and says where each one is.
        self.live_controls = live_controls
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
        # Measurements, remembered for this pass. Flex and grid measure an item
        # several ways to size it, and an item that is itself a flex container
        # measures its own items again: without remembering, the work
        # multiplied with every level of nesting, and a page nested ten deep
        # took minutes (1.7.2 froze on such sites). Each entry keeps its element
        # and style alive, so their ids cannot be reused by other objects
        # within the pass and give a wrong answer.
        self._measured: dict = {}
        self._sticky_waiting: dict = {}   # parent element -> its sticky children
        self._perspectives: list = []     # the perspectives the boxes being laid out are seen in
        # each element's place in the page, for painting equal z-indexes in order
        self._tree_order = {id(e): n for n, e in enumerate(document.root.elements(), 1)}
        # A time budget for the pass: past it, measuring gives way to quick
        # estimates, so no page can hold the window up for long
        import time as _time

        self._clock = _time.monotonic
        self._deadline = self._clock() + LAYOUT_BUDGET
        self.simplified = False
        self.document = document
        self.styles = styles
        self.zoom = zoom
        self.width = width
        self.fonts = _Fonts(zoom, font_aliases)
        self.out = DisplayList()

    # ------------------------------------------------------------ entry
    def run(self) -> DisplayList:
        root = self.document.root
        root_style = self.styles[root]
        body = self.document.body
        # the root's background, or else the body's, paints the whole canvas,
        # colour and gradients both, and that element no longer paints its own
        self._canvas_owner = None
        for element in (root, body):
            element_style = self.styles.get(element, {})
            colour = element_style.get("background-color")
            gradients = element_style.get("background-image")
            if (colour and colour[3] > 0) or gradients:
                if colour and colour[3] > 0:
                    self.out.canvas = colour
                self.out.canvas_gradients = list(gradients or [])
                self._canvas_owner = element
                break
        height = self._block(root, root_style, 0.0, 0.0, self.width, root_level=True)
        # what is positioned against the page, and then against the window
        page = QRectF(0.0, 0.0, self.width, self.viewport_height)
        for element, style, static_x, static_y in self._absolute[0]:
            self._place_absolute(element, style, page, static_x, static_y)
        if self._fixed:
            # Each fixed box is laid out against the window on its own, then
            # put back in the page where it came, pinned to the window by a
            # fixed_push when painted. GitHub's opening section is fixed with
            # z-index 0, and the sections after it scroll up over it; painted
            # last, over everything, it had stayed in front of them.
            saved = self.out
            fixed = DisplayList()
            fixed.link_z = []
            fixed.control_z = {}
            pieces = []
            for element, style, static_x, static_y in self._fixed:
                self.out = DisplayList()
                self._place_absolute(element, style, page, static_x, static_y)
                pieces.append(self.out.items)
                fixed.items.extend(self.out.items)
                fixed.links.extend(self.out.links)
                fixed.boxes.extend(self.out.boxes)
                fixed.animated = fixed.animated or self.out.animated
                fixed.animated_boxes.extend(self.out.animated_boxes)
                try:
                    z = int(str(style.get("z-index", "auto")).strip())
                except ValueError:
                    z = 0
                fixed.link_z.extend([z] * len(self.out.links))
                for piece_item in self.out.items:
                    if piece_item is not None and piece_item[0] == "control":
                        fixed.control_z[piece_item[2]] = z
            self.out = saved
            self.out.fixed = fixed
            # A fixed box inside another (GitHub's header, in a fixed wrapper) has
            # its place in its outer box's piece: pieces are put back until none
            # are left, and one inside another is not pinned a second time
            items = self.out.items
            for _round in range(len(pieces) + 1):
                filled = False
                depth, depths = 0, []
                for item in items:
                    if item is not None and item[0] == "fixed_push":
                        depth += 1
                    elif item is not None and item[0] == "fixed_pop":
                        depth -= 1
                    depths.append(depth)
                for index in range(len(items) - 1, -1, -1):
                    item = items[index]
                    if item is not None and item[0] == "fixed_slot" and item[1] < len(pieces):
                        piece = pieces[item[1]]
                        items[index:index + 1] = (list(piece) if depths[index] else
                                                  [("fixed_push",)] + piece + [("fixed_pop",)])
                        filled = True
                if not filled:
                    break
        self.out.simplified = self.simplified
        bottom, pinned = 0.0, 0
        for item in self.out.items:
            if item is not None and item[0] == "fixed_push":
                pinned += 1
            elif item is not None and item[0] == "fixed_pop":
                pinned -= 1
            elif not pinned:
                bottom = max(bottom, _bottom_edge(item))
        # boxes count whether or not anything is drawn in them, as in a browser:
        # an empty absolutely placed box below the rest had left it unreachable
        for rect, _element in self.out.boxes:
            bottom = max(bottom, rect.bottom())
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
        if self._measuring and _is_percentage(style.get("width")):
            # Measured, a width that is a share of the room counts as auto, as
            # CSS has it: 100% of the space it was measured in had made
            # AlterniTech's button wrappers 50,000 pixels wide
            width_value = None
        z = self.zoom
        if width_value is not None:
            width_value *= z
        max_width = _px(style.get("max-width"), available, auto=None)
        min_width = _px(style.get("min-width"), available, auto=None)
        horizontal_extra = padding[1] + padding[3] + border[1] + border[3]
        block_image = element.tag == "img"
        if block_image and style.get("box-sizing") != "border-box":
            # an image laid out as a block (display: block, a flex or grid
            # item) is as wide as its picture unless told otherwise, and keeps
            # its proportions: it had stretched across its container
            if width_value is None:
                image_w, _image_h = self._image_size(element, style, available)
                width_value = image_w
        if block_image and style.get("box-sizing") == "border-box" and width_value is None:
            image_w, _image_h = self._image_size(element, style, available)
            width_value = image_w + horizontal_extra
        if style.get("box-sizing") == "border-box":
            # widths given for the border box hold its padding and border: a
            # 200px box of border-box had come out 250 wide with its padding,
            # on nearly every site, which give everything border-box
            if width_value is not None:
                width_value = max(0.0, width_value - horizontal_extra)
            if max_width is not None:
                max_width = max(0.0, max_width - horizontal_extra / z)
            if min_width is not None:
                min_width = max(0.0, min_width - horizontal_extra / z)
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
        if href is None and element.tag == "summary" and self.live_controls:
            href = SummaryTarget(element)
        enclosing_link = self._link
        if href is not None:
            self._link = href
        min_height = self._length(style.get("min-height"), 0.0, vertical=True)
        max_height = self._length(style.get("max-height"), 0.0, vertical=True)
        fixed_height = self._length(style.get("height"), 0.0, vertical=True)
        if element.tag == "img" and fixed_height is None:
            _iw, image_h = self._image_size(element, style, available)
            given_w = _px(style.get("width"), available, auto=None)
            picture = self.images.get(element.attrs.get("src", "").strip())
            if picture and given_w is not None and hasattr(picture, "width") and picture.width() > 0:
                # a width given and the height left: the height in proportion,
                # to the content's width (without padding and border, if they
                # were in the width given)
                content_w = given_w * self.zoom
                if style.get("box-sizing") == "border-box":
                    content_w = max(0.0, content_w - horizontal_extra)
                image_h = content_w * picture.height() / picture.width()
            if image_h:
                fixed_height = image_h
                if style.get("box-sizing") == "border-box":
                    fixed_height += padding[0] + padding[2] + border[0] + border[2]
        if style.get("box-sizing") == "border-box":
            # heights given for the border box hold its padding and border, as
            # widths do: Google's 40px Sign in button had grown to 60
            vertical = padding[0] + padding[2] + border[0] + border[2]
            min_height = None if min_height is None else max(0.0, min_height - vertical)
            max_height = None if max_height is None else max(0.0, max_height - vertical)
            fixed_height = None if fixed_height is None else max(0.0, fixed_height - vertical)

        layered = style.get("position") in ("relative", "absolute", "fixed", "sticky") \
            and not self._measuring
        if layered:
            try:
                z_index = int(str(style.get("z-index", "auto")).strip())
                given = True
            except ValueError:
                z_index, given = 0, False         # auto: with the positioned, at 0
            # Whether it starts a stacking context of its own: a z-index given,
            # or fixed or sticky. One with z-index: auto does not, and the
            # positioned boxes inside it are stacked with its surroundings:
            # GitHub's header, z-index 99, was held inside such an ancestor at 0,
            # and the page scrolled over it.
            context = given or style.get("position") in ("fixed", "sticky")
            self.out.items.append(("layer_push", z_index, context,
                                   self._tree_order.get(id(element), 0)))
        # A transform, or an animation of one or of opacity, is drawn by paint
        # at the element's box, and makes it a stacking context, as in CSS.
        from .css import TransformOps

        transform_ops = style.get("transform") if isinstance(style.get("transform"), TransformOps) else None
        animations = style.get("animations")
        moving = bool(transform_ops or animations) and not self._measuring
        animates_opacity = bool(animations) and any(
            "opacity" in frame[1] for animation in animations for frame in animation["frames"])
        own_layer = moving and not layered
        if own_layer:
            self.out.items.append(("layer_push", 0, True, self._tree_order.get(id(element), 0)))
        sticky = None
        if style.get("position") == "sticky" and not self._measuring:
            top_offset = self._length(style.get("top"), 0.0, vertical=True)
            if top_offset is not None:
                sticky = {"top": top_offset, "start": y + (margin[0] or 0.0), "height": 0.0,
                          "links": len(self.out.links)}
                self.out.items.append(("sticky_push", sticky))
                self._sticky_waiting.setdefault(element.parent, []).append(sticky)
                self.out.sticky.append(sticky)
        # opacity covers the box and all inside it; hidden by clip or clip-path
        # counts as opacity 0, drawn as nothing but still taking its room
        opacity = style.get("opacity", 1.0)
        if not isinstance(opacity, float):
            opacity = 1.0
        if _visually_hidden(style):
            opacity = 0.0
        if opacity < 0.999 and not self._measuring and not (moving and animates_opacity):
            self.out.items.append(("opacity_push", opacity))
        xform_index = None
        if moving:
            xform_index = len(self.out.items)
            self.out.items.append(None)          # ("xform_push", spec), once the box is known
            if animations:
                self.out.animated = True
        # a perspective is for the children: where it is seen from, once known
        perspective = None
        depth = self._length(style.get("perspective"), 0.0) if style.get("perspective") not in (
            None, "none") and not self._measuring else None
        if depth:
            perspective = {"depth": depth, "origin": style.get("perspective-origin"), "box": None}
            self._perspectives.append(perspective)
        # the background goes under the children, so its place is kept now
        background_index = len(self.out.items)
        links_before = len(self.out.links)
        self.out.items.append(None)
        overflow = style.get("overflow")
        clips = (overflow in ("hidden", "clip", "auto", "scroll") and not root_level
                 and element is not self.document.body and not self._measuring)
        clip_index = None
        if clips:
            clip_index = len(self.out.items)
            self.out.items.append(None)          # ("clip_push", rect, radius), once known
            clip_links = len(self.out.links)
        position = style.get("position")
        positioned = position in ("relative", "absolute", "fixed", "sticky")
        if positioned:
            self._absolute.append([])        # descendants placed against this box
        # A block that starts a formatting context keeps its floats to itself,
        # and grows to hold them
        display = style.get("display")
        new_context = (root_level or style.get("_bfc") or style.get("float") in ("left", "right")
                       # a clearfix (its ::after clears) contains its floats,
                       # as a new context does: GitHub's profile name, floated,
                       # had run on and pushed the sidebar's details beside it
                       or style.get("-merlin-clear-after") in ("left", "right", "both")
                       or position in ("absolute", "fixed")
                       or style.get("overflow") not in (None, "", "visible")
                       or display in ("flow-root", "table", "table-cell", "inline-block",
                                      "flex", "inline-flex", "grid", "inline-grid"))
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
        elif element.tag == "svg":
            inner_height, first_baseline = self._svg_block(element, style, content_x,
                                                           content_y, content_width)
        # a form field is a field whatever its display: Google's search box is a
        # textarea with display: flex, laid out as a flex box with no field in it
        elif style.get("display") in ("grid", "inline-grid") and element.tag not in FIELD_TAGS:
            inner_height, first_baseline = self._grid(
                element, style, content_x, content_y, content_width, room=fixed_height)
        elif style.get("display") in ("flex", "inline-flex") and element.tag not in FIELD_TAGS:
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
        content_height = inner_height
        if fixed_height is not None:
            inner_height = fixed_height
        if min_height is not None:
            inner_height = max(inner_height, min_height)
        if max_height is not None:
            inner_height = min(inner_height, max_height)
        if inner_height > content_height + 0.5 and forms.kind_of(element) in forms.BUTTON_KINDS:
            # a button's content sits in its middle, as browsers draw buttons
            self._shift(background_index + 1, links_before, (inner_height - content_height) / 2)
        self._link = enclosing_link
        if self._measuring and width_value is None and style.get("display") in ("block", "list-item", "flow-root"):
            # Measured, a block of auto width is as wide as what it holds, as
            # CSS sizes it, not as wide as the room it was measured in: an
            # empty block with a border (Square's link underline) had drawn
            # across 100,000 pixels, and its link measured as wide as that.
            right = content_x
            for item in self.out.items[background_index + 1:]:
                if item is not None:
                    right = max(right, _right_edge(item))
            content_width = max(0.0, right - content_x)
            if min_width is not None:
                content_width = max(content_width, min_width * z)
        box_height = border[0] + padding[0] + inner_height + padding[2] + border[2]
        box_width = border[3] + padding[3] + content_width + padding[1] + border[1]
        box = QRectF(box_x, box_y, box_width, box_height)
        if self._measuring:
            # where the box ends, padding and border too, painted or not: a
            # button with no background had measured without its right padding
            self.out.items.append(("extent", box.right()))
        if element.tag == "img" and not self._measuring:
            content_box = QRectF(box_x + border[3] + padding[3], box_y + border[0] + padding[0],
                                 content_width, inner_height)
            self.out.items.append(("image", content_box, element.attrs.get("src", "").strip(),
                                   element.attrs.get("alt", ""), _object_fit(style)))

        colour = style.get("background-color")
        radius = self._length(style.get("border-top-left-radius"), box_width) or 0.0
        radius = min(radius, box_width / 2, box_height / 2)
        paint = []
        hidden = style.get("visibility") in ("hidden", "collapse")
        if hidden:
            colour = None                       # keeps its space, draws nothing
        owns_canvas = element is getattr(self, "_canvas_owner", None)
        if colour and colour[3] > 0 and not owns_canvas \
                and not (root_level and self.out.canvas == colour):
            paint.append(("rrect", box, colour, radius) if radius > 0.5 else ("rect", box, colour))
        gradients = style.get("background-image")
        if gradients and not hidden and not owns_canvas:
            count = len(gradients)
            for index in range(count - 1, -1, -1):          # the first layer is on top
                gradient = gradients[index]
                if gradient and gradient[0] == "url":
                    paint.append(("bgimage", box, gradient[1], _layer_of(style.get("background-size"), index),
                                  _layer_of(style.get("background-position"), index),
                                  _layer_of(style.get("background-repeat"), index), radius))
                else:
                    paint.append(("gradient", box, gradient, radius))
        if hidden:
            border = [0.0, 0.0, 0.0, 0.0]
        if radius > 0.5 and len(set(border)) == 1 and border[0] > 0:
            # one border all round, drawn along the rounded edge
            paint.append(("rborder", box, style.get("border-top-color") or style.get("color"),
                          border[0], radius))
        else:
            paint.extend(self._borders(style, box, border))
        self.out.items[background_index] = ("group", paint)
        if href is not None:
            self.out.links.append((box, href))
        if self.live_controls and forms.kind_of(element) in forms.BUTTON_KINDS \
                and not self._measuring:
            self.out.items.append(("button", box, element))

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
            # Out of flow, an absolute box takes no part in its container's
            # size: measured, it is left out. Square's menu items each held a
            # drop-down placed absolutely, and measured as wide as it, so they
            # stacked one a line where they should sit in a row.
            if not self._measuring:
                for child, child_style, static_x, static_y in waiting:
                    self._place_absolute(child, child_style, inside, static_x + dx, static_y + dy)
        if not self._measuring:
            self.out.boxes.append((box, element))
        if clip_index is not None:
            # overflow: hidden clips what is inside to the padding box, and
            # what can be clicked with it: a hidden link is not clickable
            area = QRectF(box.left() + border[3], box.top() + border[0],
                          max(0.0, box.width() - border[1] - border[3]),
                          max(0.0, box.height() - border[0] - border[2]))
            self.out.items[clip_index] = ("clip_push", area, max(0.0, radius - max(border)))
            self.out.items.append(("clip_pop",))
            kept = []
            for rect, target in self.out.links[clip_links:]:
                inside_rect = rect.intersected(area)
                if not inside_rect.isEmpty():
                    kept.append((inside_rect, target))
            del self.out.links[clip_links:]
            self.out.links.extend(kept)
        if perspective is not None:
            perspective["box"] = (box.x(), box.y(), box.width(), box.height())
            self._perspectives.pop()
        if xform_index is not None:
            # the element's own box and origin; the perspective of the parent
            # it sits in, if any; and its animations, played by paint
            bx, by, bw, bh = box.x(), box.y(), box.width(), box.height()
            ox, oy = _origin(style.get("transform-origin"), bw, bh)
            seen = self._perspectives[-1] if self._perspectives else None
            self.out.items[xform_index] = ("xform_push", {
                "box": (bx, by, bw, bh), "origin": (bx + ox, by + oy, 0.0),
                "ops": list(transform_ops or []), "animations": animations,
                "opacity": opacity, "animates_opacity": animates_opacity,
                "perspective": seen, "backface": style.get("backface-visibility") == "hidden"})
            self.out.items.append(("xform_pop",))
            if animations:
                self.out.animated_boxes.append(QRectF(box))
        if opacity < 0.999 and not self._measuring and not (moving and animates_opacity):
            self.out.items.append(("opacity_pop",))
        if sticky is not None:
            sticky["height"] = box.height()
            sticky["links_end"] = len(self.out.links)
            self.out.items.append(("sticky_pop",))
        # this box is done: sticky children now know how far they may go
        for info in self._sticky_waiting.pop(element, []):
            info["limit"] = content_y + inner_height
        if layered:
            self.out.items.append(("layer_pop",))
        if own_layer:
            self.out.items.append(("layer_pop",))

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
            items_before, links_before = len(self.out.items), len(self.out.links)
            height, baseline = self._inline([Text(shown[0])], shown[1], x, y, width)
            _font, metrics = self.fonts.get(style)
            # at least one line tall, whatever it holds: an empty field with no
            # placeholder had no line at all, and was drawn flat
            if height < metrics.lineSpacing():
                height = metrics.lineSpacing()
                if baseline is None:
                    baseline = y + metrics.ascent()
            if element.tag == "textarea":
                try:
                    rows = max(1, int(element.attrs.get("rows", "2")))
                except ValueError:
                    rows = 2
                height = max(height, rows * metrics.lineSpacing())
            kind = forms.kind_of(element)
            if self.live_controls and not self._measuring and kind not in forms.BUTTON_KINDS:
                # the widget draws the value, the caret and the placeholder
                del self.out.items[items_before:]
                del self.out.links[links_before:]
                font, _metrics = self.fonts.get(style)
                self.out.items.append(("control", QRectF(x, y, width, height), element,
                                       font, style.get("color", (0, 0, 0, 255))))
            return height, baseline
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
        if style.get("-merlin-clear-after") in ("left", "right", "both"):
            # its ::after clears floats (the clearfix): the box ends below them,
            # and they go no further
            below = self._floats_bottom(style["-merlin-clear-after"])
            if below is not None and below > cursor:
                cursor = below
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
        if self._clock() > self._deadline:
            # out of time: a quick estimate from the text alone
            self.simplified = True
            if narrowest:
                return 0.0
            _font, metrics = self.fonts.get(style)
            return min(600.0 * self.zoom, metrics.horizontalAdvance(element.text()[:200]))
        if element.tag == "svg":
            # an <svg>'s size is its own, from its attributes, not from what
            # is inside it; measured by contents it came out 0 wide
            return self._svg_size(element, style, 100000.0)[0]
        return _Measure(self).extent(element, style, 1.0 if narrowest else 100000.0)

    def _item_box(self, element: Element, style: dict, x: float, y: float,
                  border_width: float, border_height: float | None = None) -> float:
        """Lay out one flex item at a size already decided; returns its height."""
        _m, padding, border = self._edges(style, border_width)
        z = self.zoom
        item = dict(style)
        item["width"] = max(0.0, border_width - padding[1] - padding[3] - border[1] - border[3]) / z
        item["min-width"] = item["max-width"] = None
        # the sizes given here are the content's, padding and border taken off
        # already: as border-box they had been taken off twice, and Merlin's
        # own new-tab tiles came out narrower than their names
        item["box-sizing"] = "content-box"
        for side in ("top", "right", "bottom", "left"):
            item[f"margin-{side}"] = 0.0
        if border_height is not None:
            item["height"] = max(0.0, border_height - padding[0] - padding[2]
                                 - border[0] - border[2]) / z
            item["min-height"] = item["max-height"] = None
        display = item.get("display")
        # an item is always laid out as a block, whatever it was
        item["display"] = "flex" if display in ("flex", "inline-flex") else \
            "grid" if display in ("grid", "inline-grid") else \
            ("table" if display == "table" else "block")
        item["_bfc"] = True                  # each item keeps its floats to itself
        item["float"] = None
        return self._block(element, item, x, y, border_width)

    def _scratch_height(self, element, style, border_width) -> float:
        """How tall an item would be at a width, without drawing it."""
        key = ("height", id(element), id(style), round(border_width, 3))
        found = self._measured.get(key)
        if found is not None:
            return found[0]
        if self._clock() > self._deadline:
            self.simplified = True
            _font, metrics = self.fonts.get(style)
            return metrics.lineSpacing()
        height = self._scratch_height_now(element, style, border_width)
        self._measured[key] = (height, element, style)
        return height

    def _scratch_height_now(self, element, style, border_width) -> float:
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
            # with box-sizing: border-box, a width (and flex-basis, min and max)
            # includes padding and border, which come off it here: GitHub's
            # pinned repositories, two at 50% with padding, had wrapped to one
            # a row
            inner = (lambda value: None if value is None else max(0.0, value - edges)) \
                if item_style.get("box-sizing") == "border-box" else (lambda value: value)
            fixed = inner(self._length(item_style.get("width"), width))
            basis_value = item_style.get("flex-basis", "auto")
            basis = inner(self._length(basis_value, width)) if basis_value not in (None, "auto") else None
            if basis is None:
                basis = fixed if fixed is not None else self._natural_width(element, item_style, False)
            # The automatic minimum is the smaller of the narrowest content and
            # any width given: an item with width: 300px and little in it can
            # still shrink, rather than pushing the row off a narrow screen.
            narrowest = self._natural_width(element, item_style, True)
            if fixed is not None:
                narrowest = min(narrowest, fixed)
            low = inner(self._length(item_style.get("min-width"), width))
            high = inner(self._length(item_style.get("max-width"), width))
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
            _resolve_flexible(line, width - gaps, outer)
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
            if (basis is not None or fixed_height is not None) \
                    and item_style.get("min-height") in (None, "auto") \
                    and item_style.get("overflow", "visible") in (None, "visible") \
                    and item_style.get("overflow-y", "visible") in (None, "visible"):
                # A flex item is never smaller than its content (its automatic
                # minimum size), unless it clips what overflows. Square's
                # column held AlterniTech's products at height: 100%, laid out
                # 355 high: the footer came after the first row of products,
                # the rest drawn beneath it.
                as_content = dict(item_style, height=None, **{"flex-basis": "auto"})
                height = max(height, self._scratch_height(element, as_content, border_width))
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
            # blocked, or failed: with a size given it keeps its box, as in
            # browsers, so a failure never moves the page; without one, no room
            w = self._length(style.get("width"), available)
            h = self._length(style.get("height"), 0.0, vertical=True)
            try:
                w = w if w is not None else float(element.attrs.get("width", "")) * self.zoom
                h = h if h is not None else float(element.attrs.get("height", "")) * self.zoom
            except ValueError:
                return 0.0, 0.0
            return (w, h) if w and h else (0.0, 0.0)
        # in device-independent pixels: an image drawn at twice its size for
        # sharpness, as SVG ones are, is still its own size on the page
        natural = ((picture.width() / picture.devicePixelRatio(),
                    picture.height() / picture.devicePixelRatio())
                   if picture is not None else None)
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
            if kind in ("rect", "image", "rrect", "rborder", "svg", "control", "button",
                        "gradient", "clip_push"):
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
                if node.tag == "svg" or display in INLINE_BOXES:
                    # laid out as a box of its own, sitting on the line
                    out.append(("box", node, node_style, link))
                    continue
                if node.tag in ("input", "textarea", "select"):
                    shown = self._control_text(node, node_style)
                    if shown is not None:
                        out.append(("text", shown[0], shown[1], link))
                    continue
                href = node.attrs.get("href") if node.tag == "a" and "href" in node.attrs else link
                if node.tag == "label" and self.live_controls and href is None:
                    href = LabelTarget(node)
                if node.tag == "summary" and self.live_controls and href is None:
                    href = SummaryTarget(node)
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
            if kind == "box":
                pieces.append((_BOX, payload, style, link))
                at_line_start = False
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
                    if part[0] == "box":
                        _k, w, element, style, link, ascent, descent, _lh, extra = part
                        m_left, m_top, border_width, _m_bottom = extra
                        top = baseline - ascent + m_top
                        if element.tag == "svg":
                            self._svg_item(element, style, QRectF(pen + m_left, top,
                                                                  border_width,
                                                                  ascent + descent - m_top - extra[3]))
                        else:
                            saved_link = self._link
                            self._link = link
                            self._item_box(element, style, pen + m_left, top, border_width)
                            self._link = saved_link
                        pen += w
                        run = None
                        continue
                    if part[0] == "image":
                        _k, w, element, style, link, h, _d, _lh, _f = part
                        rect = QRectF(pen, baseline - h, w, h)
                        self.out.items.append(("image", rect, element.attrs.get("src", "").strip(),
                                               element.attrs.get("alt", ""), _object_fit(style)))
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
                    if style.get("visibility") in ("hidden", "collapse"):
                        continue                   # its room is kept, nothing drawn
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
            if kind is _BOX:
                part = self._inline_box(piece[1], piece[2], piece[3], width)
                if part is None:
                    continue
                if line and used + part[1] > span[1] - span[0]:
                    emit(False)
                line.append(part)
                used += part[1]
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
            # a pixel's tolerance: words measured one by one come to a hair
            # more than the phrase measured whole, and text given exactly its
            # own width had wrapped ("All / Products" on AlterniTech's buttons)
            if wraps and text != " " and line and used + advance > span[1] - span[0] + 1.0:
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

    # ------------------------------------------------------- inline boxes
    def _inline_box(self, element: Element, style: dict, link, available: float):
        """An inline-block or inline <svg>, sized, as a part of a line.

        Its text baseline sits on the line's baseline, as in browsers; a box
        with no text sits with its bottom margin edge there.
        """
        margin, padding, border = self._edges(style, available)
        m_top, m_right, m_bottom, m_left = (m or 0.0 for m in margin)
        edges = padding[1] + padding[3] + border[1] + border[3]
        if element.tag == "svg":
            w, h = self._svg_size(element, style, available)
            if w <= 0 or h <= 0:
                return None
            ascent = h + m_top + m_bottom
            return ("box", w + m_left + m_right, element, style, link, ascent, 0.0, ascent,
                    (m_left, m_top, w, m_bottom))
        fixed = self._length(style.get("width"), available)
        if self._measuring and _is_percentage(style.get("width")):
            fixed = None             # measured, a percentage of the room counts as auto
        if fixed is not None:
            border_width = fixed + edges
        elif element.tag == "select":
            # wide enough for its longest option, and the arrow
            _font, metrics = self.fonts.get(style)
            longest = max((metrics.horizontalAdvance(" ".join(o.text().split()))
                           for o in element.elements() if o.tag == "option"), default=40.0)
            border_width = longest + 28 * self.zoom + edges
        elif element.tag in ("input", "textarea") and \
                element.attrs.get("type", "text").lower() not in (
                    "checkbox", "radio", "submit", "button", "reset", "hidden", "image"):
            # a text field with no width: as browsers, about 20 characters wide
            attribute = "cols" if element.tag == "textarea" else "size"
            try:
                characters = int(element.attrs.get(attribute, "20"))
            except ValueError:
                characters = 20
            _font, metrics = self.fonts.get(style)
            border_width = characters * metrics.averageCharWidth() + edges
        else:
            border_width = self._natural_width(element, style, False) + edges
        border_width = min(border_width, max(1.0, available - m_left - m_right))
        high = self._length(style.get("max-width"), available)
        low = self._length(style.get("min-width"), available)
        if high is not None:
            border_width = min(border_width, high + edges)
        if low is not None:
            border_width = max(border_width, low + edges)
        saved, saved_runs = self.out, self._runs
        held = (self._floats, self._absolute, self._fixed)
        self._floats, self._absolute, self._fixed = [], [[]], []
        self.out = DisplayList()
        self._runs = []
        try:
            self._last_baseline = None
            height = self._item_box(element, style, 0.0, 0.0, border_width)
            baseline = self._last_baseline
        finally:
            self.out, self._runs = saved, saved_runs
            self._floats, self._absolute, self._fixed = held
        if baseline is None:
            ascent, descent = height + m_top + m_bottom, 0.0
        else:
            ascent, descent = baseline + m_top, height - baseline + m_bottom
        return ("box", border_width + m_left + m_right, element, style, link, ascent, descent,
                ascent + descent, (m_left, m_top, border_width, m_bottom))

    # ---------------------------------------------------------------- svg
    def _svg_size(self, element: Element, style: dict, available: float):
        """An <svg>'s drawn size: CSS, then its attributes, then its viewBox."""
        w = self._length(style.get("width"), available)
        h = self._length(style.get("height"), 0.0, vertical=True)

        def attribute(name):
            value = element.attrs.get(name, "").strip()
            try:
                return float(value.rstrip("px")) * self.zoom if value and not value.endswith("%") else None
            except ValueError:
                return None

        w = w if w is not None else attribute("width")
        h = h if h is not None else attribute("height")
        box = [float(v) for v in re.split(r"[\s,]+", element.attrs.get("viewbox", "").strip())
               if re.match(r"^-?[\d.]+$", v)]
        ratio = box[2] / box[3] if len(box) == 4 and box[3] else None
        if w is None and h is None:
            w, h = (300.0 * self.zoom, 150.0 * self.zoom) if not ratio else \
                (min(available, 300.0 * self.zoom), min(available, 300.0 * self.zoom) / ratio)
        elif w is None:
            w = h * ratio if ratio else h
        elif h is None:
            h = w / ratio if ratio else w
        return w, h

    def _svg_item(self, element: Element, style: dict, rect: QRectF) -> None:
        if style.get("visibility") in ("hidden", "collapse"):
            return
        self.out.items.append(("svg", rect, svg_markup(element, style)))
        if self._link:
            self.out.links.append((rect, self._link))

    def _svg_block(self, element: Element, style: dict, x: float, y: float, width: float):
        """An <svg> laid out as a block, a flex or a grid item."""
        w, h = self._svg_size(element, style, width)
        fixed_w = self._length(style.get("width"), width)
        if fixed_w is None and style.get("display") in ("block",) and element.attrs.get("width") is None:
            w = width
        self._svg_item(element, style, QRectF(x, y, w, h))
        return h, None

    # ---------------------------------------------------------------- grid
    def _grid_items(self, element: Element, x: float, y: float):
        items = []
        for child in element.children:
            if isinstance(child, Element):
                child_style = self.styles[child]
                if child_style.get("display") == "none":
                    continue
                if child_style.get("position") in ("absolute", "fixed"):
                    self._wait_for_placement(child, child_style, x, y)
                    continue
                items.append((child, child_style))
            elif isinstance(child, Text) and child.data.strip():
                anonymous = Element("span")
                anonymous.append(Text(child.data))
                anonymous.parent = element
                inherited = {k: v for k, v in self.styles[element].items()
                             if k in ("color", "font-family", "font-size", "font-weight",
                                      "font-style", "line-height", "text-align",
                                      "white-space", "text-decoration")}
                inherited["display"] = "block"
                items.append((anonymous, inherited))
        return sorted(items, key=lambda pair: pair[1].get("order", 0))

    def _expand_tracks(self, tracks: list, room: float, gap: float):
        """A track list with repeat(auto-fill / auto-fit, ...) resolved for room."""
        fixed_room = 0.0
        repeats = None
        for track in tracks:
            if track[0] == "repeat":
                repeats = track
            else:
                fixed_room += self._track_floor(track, room) + gap
        out = []
        for track in tracks:
            if track[0] != "repeat":
                out.append(track)
                continue
            inner = track[2] or [("auto",)]
            each = sum(max(self._track_floor(t, room), 1.0) for t in inner) + gap * len(inner)
            count = max(1, int((room - fixed_room + gap) // each)) if each > 0 else 1
            out.extend(inner * count)
        return out, (repeats[1] if repeats else "")

    def _track_floor(self, track, room: float) -> float:
        """The smallest a track can be before content is considered."""
        kind = track[0]
        if kind == "px":
            return track[1] * self.zoom
        if kind == "pct":
            return room * track[1] / 100
        if kind == "mix":                         # calc() of a percentage and lengths
            return max(0.0, room * track[1] / 100 + track[2] * self.zoom)
        if kind == "minmax":
            return self._track_floor(track[1], room)
        return 0.0

    @staticmethod
    def _grid_line(value) -> tuple:
        """A placement value: ("line", n), ("span", n), ("name", s) or ("auto",)."""
        value = (value or "auto").strip()
        if value in ("auto", ""):
            return ("auto",)
        if value.startswith("span"):
            try:
                return ("span", max(1, int(value.split()[1])))
            except (IndexError, ValueError):
                return ("span", 1)
        try:
            return ("line", int(value))
        except ValueError:
            return ("name", value)

    def _grid(self, element: Element, style: dict, x: float, y: float, width: float,
              room: float | None = None):
        """Lay out a grid container's items; returns (height, first baseline)."""
        items = self._grid_items(element, x, y)
        if not items:
            return 0.0, None
        column_gap = self._length(style.get("column-gap"), width) or 0.0
        row_gap = self._length(style.get("row-gap"), width) or 0.0
        areas = style.get("grid-template-areas") or []
        column_tracks, fit = self._expand_tracks(style.get("grid-template-columns") or [],
                                                 width, column_gap)
        if not column_tracks and areas:
            column_tracks = [("auto",)] * max(len(r) for r in areas)
        if not column_tracks:
            column_tracks = [("auto",)]
        row_tracks = list(style.get("grid-template-rows") or [])
        row_tracks = [t for t in row_tracks if t[0] != "repeat"]
        auto_rows = [t for t in (style.get("grid-auto-rows") or []) if t[0] != "repeat"] or [("auto",)]

        # named areas as (row, column, rows, columns), 0-based
        named = {}
        for r, row in enumerate(areas):
            for c, name in enumerate(row):
                if name == ".":
                    continue
                top, left, bottom, right = named.get(name, (r, c, r, c))
                named[name] = (min(top, r), min(left, c), max(bottom, r), max(right, c))
        count = len(column_tracks)

        def resolve(start, end, lines):
            """Start line and span on one axis; None when left to auto-placement."""
            a, b = self._grid_line(start), self._grid_line(end)

            def number(line):
                # 0-based line index; -1 is the last line, which is line
                # count + 1: in a four-column grid, index 4
                n = line[1]
                return n - 1 if n > 0 else max(0, lines + 1 + n)

            if a[0] == "line" and b[0] == "line":
                first, last = number(a), number(b)
                if last < first:
                    first, last = last, first
                return first, max(1, last - first)
            if a[0] == "line":
                return number(a), b[1] if b[0] == "span" else 1
            if b[0] == "line":
                span = a[1] if a[0] == "span" else 1
                return max(0, number(b) - span), span
            return None, (a[1] if a[0] == "span" else b[1] if b[0] == "span" else 1)

        placed = []                    # (element, style, row, column, rows, columns)
        taken = set()
        waiting = []
        for child, child_style in items:
            area = child_style.get("grid-area-name")
            if area in named:
                top, left, bottom, right = named[area]
                placed.append((child, child_style, top, left, bottom - top + 1, right - left + 1))
                continue
            column, columns = resolve(child_style.get("grid-column-start"),
                                      child_style.get("grid-column-end"), count)
            row, rows = resolve(child_style.get("grid-row-start"),
                                child_style.get("grid-row-end"), len(row_tracks) or 1)
            columns = min(columns, max(1, count))
            if column is not None and row is not None:
                placed.append((child, child_style, row, column, rows, columns))
            else:
                waiting.append((child, child_style, row, column, rows, columns))
        # An item placed in columns past those declared adds columns, as CSS
        # says. Without this, auto-placement looked for ever for room that
        # never came, and froze Merlin (1.7.1, on real sites' stylesheets).
        count = max([count] + [c + cs for _e, _s, _r, c, _rs, cs in placed + waiting
                               if c is not None])
        for _c, _s, row, column, rows, columns in placed:
            for r in range(row, row + rows):
                for c in range(column, column + columns):
                    taken.add((r, c))
        cursor = [0, 0]
        for child, child_style, row, column, rows, columns in waiting:
            r, c = (row, 0) if row is not None else tuple(cursor)
            if column is not None:
                c = column
            steps = 0
            while True:
                steps += 1
                if steps > 100000:
                    # a second line of defence: never loop without end; the
                    # item goes on a row of its own below everything
                    r = max((rr for rr, _cc in taken), default=-1) + 1
                    c = column if column is not None else 0
                    break
                if c + columns > max(count, columns):
                    r, c = r + 1, (column if column is not None else 0)
                    continue
                if all((rr, cc) not in taken for rr in range(r, r + rows)
                       for cc in range(c, c + columns)):
                    break
                if column is not None:
                    r += 1
                else:
                    c += 1
            placed.append((child, child_style, r, c, rows, columns))
            for rr in range(r, r + rows):
                for cc in range(c, c + columns):
                    taken.add((rr, cc))
            if row is None and column is None:
                cursor = [r, c + columns]
        count = max(count, max(p[3] + p[5] for p in placed))
        if fit == "auto-fit":
            # auto-fit folds away columns no item uses
            used = max(p[3] + p[5] for p in placed)
            column_tracks = column_tracks[:max(used, 1)]
        while len(column_tracks) < count:
            column_tracks.append(("auto",))
        row_count = max(p[2] + p[4] for p in placed)
        while len(row_tracks) < row_count:
            row_tracks.append(auto_rows[(len(row_tracks)) % len(auto_rows)])

        # ---------- column sizes
        def content(child, child_style, narrowest):
            _m, padding, border = self._edges(child_style, width)
            edges = padding[1] + padding[3] + border[1] + border[3]
            fixed = self._length(child_style.get("width"), width)
            if fixed is not None:
                return fixed + edges
            if child.tag == "svg":
                return self._svg_size(child, child_style, width)[0]
            return self._natural_width(child, child_style, narrowest) + edges

        low = [0.0] * len(column_tracks)
        high = [0.0] * len(column_tracks)
        for child, child_style, _r, c, _rows, columns in placed:
            if columns == 1 and c < len(column_tracks):
                low[c] = max(low[c], content(child, child_style, True))
                high[c] = max(high[c], content(child, child_style, False))
        sizes, flexible = [], {}
        for index, track in enumerate(column_tracks):
            kind = track[0]
            if kind in ("px", "pct", "mix"):
                sizes.append(self._track_floor(track, width))
            elif kind == "fr":
                sizes.append(low[index])
                flexible[index] = (track[1], low[index])
            elif kind == "minmax":
                floor = self._track_floor(track[1], width) if track[1][0] in ("px", "pct", "mix") \
                    else (low[index] if track[1][0] in ("auto", "min") else high[index])
                ceiling = track[2]
                if ceiling[0] == "fr":
                    sizes.append(floor)
                    flexible[index] = (ceiling[1], floor)
                elif ceiling[0] in ("px", "pct", "mix"):
                    sizes.append(max(floor, min(self._track_floor(ceiling, width),
                                                max(high[index], floor))))
                else:
                    sizes.append(max(floor, high[index]))
            elif kind == "min":
                sizes.append(low[index])
            else:                                   # auto and max-content
                sizes.append(high[index])
        gaps = column_gap * (len(sizes) - 1)
        free = width - sum(sizes) - gaps
        # Maximize tracks, as the specification orders it: room left first
        # grows tracks with a definite most (minmax(0, 960px), or GitHub's
        # minmax(0, calc(100% - sidebar - gutter))) towards it, shared evenly,
        # before fr tracks take any or auto tracks are stretched. Left out,
        # GitHub's main column stayed as narrow as a word.
        limits = {}
        for index, track in enumerate(column_tracks):
            if track[0] == "minmax" and track[2][0] in ("px", "pct", "mix") and index not in flexible:
                limit = self._track_floor(track[2], width)
                if limit > sizes[index]:
                    limits[index] = limit
        if getattr(self, "_grid_measuring", 0):
            limits = {}                          # measuring: no room to hand out
        while free > 0.5 and limits:
            share = free / len(limits)
            for index in list(limits):
                grow = min(share, limits[index] - sizes[index])
                sizes[index] += grow
                free -= grow
                if limits[index] - sizes[index] < 0.5:
                    del limits[index]
        if flexible and free > 0:
            # share the room left among the fr tracks, none below its floor
            active = dict(flexible)
            room_left = width - gaps - sum(s for i, s in enumerate(sizes) if i not in active)
            for _round in range(len(active) + 1):
                total = sum(fr for fr, _floor in active.values()) or 1.0
                unit = room_left / total
                small = {i for i, (fr, floor) in active.items() if fr * unit < floor}
                if not small:
                    for i, (fr, _floor) in active.items():
                        sizes[i] = fr * unit
                    break
                for i in small:
                    sizes[i] = active[i][1]
                    room_left -= active[i][1]
                    del active[i]
        elif free > 0 and not flexible and not getattr(self, "_grid_measuring", 0):
            # with no fr track to take it, the room left stretches the auto
            # tracks, as the specification's last sizing step says
            stretchy = [i for i, t in enumerate(column_tracks) if t[0] == "auto"]
            if stretchy and style.get("justify-content", "normal") in ("normal", "stretch", ""):
                for i in stretchy:
                    sizes[i] += free / len(stretchy)
        elif free < 0:
            # too wide: auto tracks give way, down to their narrowest content
            give = [(i, sizes[i] - low[i]) for i, t in enumerate(column_tracks)
                    if t[0] in ("auto", "max") and sizes[i] > low[i]]
            total = sum(g for _i, g in give)
            if total > 0:
                for i, g in give:
                    sizes[i] -= min(g, -free * g / total)
        column_x = []
        pen = x
        spare = width - sum(sizes) - gaps
        start, between = self._justify(style.get("justify-content", "start"), spare, len(sizes), False)
        pen += start
        for size in sizes:
            column_x.append(pen)
            pen += size + column_gap + between

        def area_width(c, columns):
            return sum(sizes[c:c + columns]) + (column_gap + between) * (columns - 1)

        # ---------- row sizes
        heights = [0.0] * len(row_tracks)
        measured = {}
        for index, (child, child_style, r, c, rows, columns) in enumerate(placed):
            margin, padding, border = self._edges(child_style, width)
            cell = area_width(c, columns)
            justify = child_style.get("justify-self", "auto")
            if justify in ("auto", "normal", ""):
                justify = style.get("justify-items", "stretch") or "stretch"
            fixed = self._length(child_style.get("width"), cell)
            m_left, m_right = margin[3] or 0.0, margin[1] or 0.0
            if fixed is not None:
                border_width = fixed + padding[1] + padding[3] + border[1] + border[3]
            elif justify in ("stretch", "normal"):
                border_width = cell - m_left - m_right
            else:
                border_width = min(content(child, child_style, False), cell - m_left - m_right)
            fixed_height = self._length(child_style.get("height"), 0.0, vertical=True)
            if child.tag == "svg":
                height = self._svg_size(child, child_style, border_width)[1]
            elif fixed_height is not None:
                height = fixed_height
            else:
                height = self._scratch_height(child, child_style, border_width)
            measured[index] = (border_width, height, justify, margin)
            if rows == 1 and r < len(heights):
                heights[r] = max(heights[r], height + (margin[0] or 0.0) + (margin[2] or 0.0))
        for index, track in enumerate(row_tracks):
            kind = track[0]
            if kind == "px":
                heights[index] = track[1] * self.zoom
            elif kind == "minmax" and track[1][0] == "px":
                heights[index] = max(heights[index], track[1][1] * self.zoom)
        # an item spanning rows taller than they are: the last of them grows
        for index, (child, child_style, r, c, rows, columns) in enumerate(placed):
            if rows > 1:
                need = measured[index][1]
                have = sum(heights[r:r + rows]) + row_gap * (rows - 1)
                if need > have:
                    heights[min(r + rows, len(heights)) - 1] += need - have
        if room is not None:
            flexible_rows = [i for i, t in enumerate(row_tracks)
                             if t[0] == "fr" or (t[0] == "minmax" and t[2][0] == "fr")]
            spare_rows = room - sum(heights) - row_gap * (len(heights) - 1)
            if flexible_rows and spare_rows > 0:
                for i in flexible_rows:
                    heights[i] += spare_rows / len(flexible_rows)
        row_y = []
        pen = y
        for height in heights:
            row_y.append(pen)
            pen += height + row_gap
        total = sum(heights) + row_gap * (len(heights) - 1)

        # ---------- the items, in their areas
        first_baseline = None
        for index, (child, child_style, r, c, rows, columns) in enumerate(placed):
            border_width, height, justify, margin = measured[index]
            cell_w = area_width(c, columns)
            cell_h = sum(heights[r:r + rows]) + row_gap * (rows - 1)
            m_top, m_right, m_bottom, m_left = (m or 0.0 for m in margin)
            spare_x = cell_w - border_width - m_left - m_right
            offset_x = {"center": spare_x / 2, "end": spare_x, "flex-end": spare_x,
                        "right": spare_x}.get(justify, 0.0)
            align = child_style.get("align-self", "auto")
            if align in ("auto", "normal", ""):
                align = style.get("align-items", "stretch") or "stretch"
            forced = None
            spare_y = cell_h - height - m_top - m_bottom
            offset_y = 0.0
            if align in ("stretch", "normal") and \
                    self._length(child_style.get("height"), 0.0, vertical=True) is None:
                forced = max(height, cell_h - m_top - m_bottom)
            else:
                offset_y = {"center": spare_y / 2, "end": spare_y,
                            "flex-end": spare_y}.get(align, 0.0)
            bx = column_x[c] + m_left + max(0.0, offset_x)
            by = row_y[r] + m_top + max(0.0, offset_y)
            if child.tag == "svg":
                self._svg_item(child, child_style, QRectF(bx, by, border_width,
                                                          forced if forced else height))
                continue
            self._last_baseline = None
            self._item_box(child, child_style, bx, by, border_width, forced)
            if first_baseline is None:
                first_baseline = self._last_baseline
        return total, first_baseline

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
                                   element.attrs.get("alt", ""), _object_fit(style)))
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
            # its place in the page is kept, so it is painted there in the
            # stacking order, by its z-index, and not simply over everything
            self.out.items.append(("fixed_slot", len(self._fixed)))
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
                                   element.attrs.get("alt", ""), _object_fit(style)))
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
        """The width content takes when laid out in width: remembered per pass."""
        key = ("extent", id(element), id(style), width)
        found = self.layout._measured.get(key)
        if found is not None:
            return found[0]
        result = self._extent_now(element, style, width)
        self.layout._measured[key] = (result, element, style)
        return result

    def _extent_now(self, element, style, width: float) -> float:
        saved = self.layout.out
        was_measuring = self.layout._measuring
        held = (self.layout._floats, self.layout._absolute, self.layout._fixed)
        self.layout._floats, self.layout._absolute, self.layout._fixed = [], [[]], []
        self.layout.out = DisplayList()
        self.layout._measuring = True
        try:
            if style.get("display") in ("flex", "inline-flex"):
                return self._flex_natural(element, style, narrowest=width <= 1.0)
            elif style.get("display") in ("grid", "inline-grid"):
                # A grid's natural width is what its content needs: laid out
                # from the start, with no room handed out. Centred in the
                # 100,000 pixels it was measured in, GitHub's button content
                # came to 50,000 wide, its "Code" drawn off to the right.
                plain = dict(style, **{"justify-content": "start", "justify-items": "start"})
                self.layout._grid_measuring = getattr(self.layout, "_grid_measuring", 0) + 1
                try:
                    self.layout._grid(element, plain, 0.0, 0.0, width)
                finally:
                    self.layout._grid_measuring -= 1
            else:
                stacked = self._stacked_natural(element, width <= 1.0)
                if stacked is not None:
                    return stacked
                self.layout._contents(element, style, 0.0, 0.0, width)
            right = 0.0
            for item in self.layout.out.items:
                right = max(right, _right_edge(item))
            return right
        finally:
            self.layout.out = saved
            self.layout._measuring = was_measuring
            self.layout._floats, self.layout._absolute, self.layout._fixed = held


    _BLOCKISH = ("block", "flex", "grid", "table", "list-item", "flow-root")

    def _outer_natural(self, item, item_style, narrowest: bool) -> float:
        """A box's natural width with its padding, borders and margins."""
        layout = self.layout
        margin, padding, border = layout._edges(item_style, 0.0)
        edges = padding[1] + padding[3] + border[1] + border[3]
        given = item_style.get("width")
        fixed = given * layout.zoom if isinstance(given, float) else None
        if fixed is not None and item_style.get("box-sizing") == "border-box":
            outer = fixed
        else:
            inner = fixed if fixed is not None else layout._natural_width(item, item_style, narrowest)
            low, high = item_style.get("min-width"), item_style.get("max-width")
            if isinstance(high, float):
                inner = min(inner, high * layout.zoom)
            if isinstance(low, float):
                inner = max(inner, low * layout.zoom)
            outer = inner + edges
        return outer + (margin[1] or 0.0) + (margin[3] or 0.0)

    def _stacked_natural(self, element, narrowest: bool):
        """A block holding only blocks: as wide as its widest, from their own widths.

        Measured by laying it out on an unbounded line, a flex container inside
        stretched to that line, and anything it pushed to its far end (a GitHub
        menu's chevron) made the block as wide as the line. None when the block
        holds text or inline boxes, which are measured as before.
        """
        styles = self.layout.styles
        blocks = []
        for child in element.children:
            if isinstance(child, Element):
                child_style = styles.get(child, {})
                display = child_style.get("display")
                if display == "none" or child_style.get("position") in ("absolute", "fixed"):
                    continue
                if display not in self._BLOCKISH:
                    return None
                blocks.append((child, child_style))
            elif isinstance(child, Text) and child.data.strip():
                return None
        if not blocks:
            return None
        return max(self._outer_natural(c, cs, narrowest) for c, cs in blocks)

    def _flex_natural(self, element, style, narrowest: bool) -> float:
        """A flex container's content width, as CSS defines it, from its items.

        Laid out on an unbounded line and measured at its right-most edge, as
        it was, justify-content: space-between (or center, or an auto margin)
        sent the last item to the far end: a GitHub menu button came out
        100,000 pixels wide, and everything after it off the screen. The
        widest a row can be is its items side by side, with their margins and
        the gaps between; the narrowest, for a row that wraps, its widest item,
        and for one that does not, all its items at their narrowest. A column
        is as wide as its widest item.
        """
        layout = self.layout
        saved_origin = getattr(layout, "_flex_origin", (0.0, 0.0))
        layout._flex_origin = (0.0, 0.0)
        try:
            items = layout._flex_items(element)
        finally:
            layout._flex_origin = saved_origin
        row = str(style.get("flex-direction", "row")).startswith("row")
        wraps = style.get("flex-wrap", "nowrap") in ("wrap", "wrap-reverse")
        # a percentage has nothing to be a percentage of while measuring, so
        # such an item is measured by what is in it
        sizes = [self._outer_natural(item, item_style, narrowest) for item, item_style in items]
        if not sizes:
            return 0.0
        if not row or (narrowest and wraps):
            return max(sizes)
        gap = layout._length(style.get("column-gap"), 0.0) or 0.0
        return sum(sizes) + gap * (len(sizes) - 1)


def _resolve_flexible(line: list, room: float, outer) -> None:
    """Grow or shrink a flex line's items to fill room, as CSS resolves them.

    The free space is shared out, by flex-grow, or when shrinking by
    flex-shrink times the base size. An item that would pass its limit (its
    max-width growing; its narrowest content or min-width shrinking) is held
    at it and frozen, and the rest is shared among the others again. Clamping
    without sharing again, as before, lost the frozen item's share: GitHub's
    header, its menu unable to shrink, overflowed by the share the menu could
    not take, taking Sign in and Sign up off the screen.
    """
    frozen = set()
    for _round in range(len(line) + 1):
        free = room - sum(outer(e) for e in line)
        active = [e for e in line if id(e) not in frozen]
        if not active or abs(free) < 0.01:
            return
        if free > 0:
            total = sum(e["grow"] for e in active)
            if total <= 0:
                return
            shares = {id(e): free * e["grow"] / total for e in active}
            over = [e for e in active
                    if e["ceiling"] is not None and e["size"] + shares[id(e)] > e["ceiling"]]
            if not over:
                for e in active:
                    e["size"] += shares[id(e)]
                return
            for e in over:
                e["size"] = max(e["size"], e["ceiling"])
                frozen.add(id(e))
        else:
            weights = {id(e): e["shrink"] * e["base"] for e in active}
            total = sum(weights.values())
            if total <= 0:
                return
            shares = {id(e): free * weights[id(e)] / total for e in active}
            under = [e for e in active if e["size"] + shares[id(e)] < e["floor"]]
            if not under:
                for e in active:
                    e["size"] += shares[id(e)]
                return
            for e in under:
                e["size"] = min(e["size"], e["floor"]) if e["size"] < e["floor"] else e["floor"]
                frozen.add(id(e))


def _bottom_edge(item) -> float:
    if item is None:
        return 0.0
    if item[0] == "group":
        return max((_bottom_edge(sub) for sub in item[1]), default=0.0)
    if item[0] in ("rect", "image", "rrect", "rborder", "svg", "gradient"):
        return item[1].bottom()
    if item[0] == "text":
        return item[2] + QFontMetricsF(item[4]).descent()
    return 0.0


def _right_edge(item) -> float:
    if item is None:
        return 0.0
    if item[0] == "group":
        return max((_right_edge(sub) for sub in item[1]), default=0.0)
    if item[0] in ("rect", "image", "rrect", "rborder", "svg", "gradient", "bgimage"):
        return item[1].right()
    if item[0] == "extent":
        return item[1]
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


# ------------------------------------------------------------------- svg

# The HTML parser lowercases names, and SVG's are case-sensitive: viewBox as
# viewbox is simply ignored by an SVG renderer. These are put back.
_SVG_NAMES = {n.lower(): n for n in (
    "viewBox", "preserveAspectRatio", "gradientUnits", "gradientTransform",
    "patternUnits", "patternContentUnits", "patternTransform", "clipPathUnits",
    "markerWidth", "markerHeight", "markerUnits", "refX", "refY", "stdDeviation",
    "textLength", "lengthAdjust", "spreadMethod", "maskUnits", "maskContentUnits",
    "filterUnits", "primitiveUnits", "baseFrequency", "numOctaves", "pathLength",
    "startOffset", "attributeName", "repeatCount", "keyTimes", "keySplines",
    "linearGradient", "radialGradient", "clipPath", "foreignObject", "textPath",
    "feGaussianBlur", "feOffset", "feBlend", "feColorMatrix", "feComposite",
    "feFlood", "feMerge", "feMergeNode", "feMorphology", "feTurbulence")}


def _svg_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def svg_markup(element: Element, style: dict) -> str:
    """An inline <svg> as standalone SVG, for Qt's renderer.

    currentColor takes the colour of the surrounding text, and a fill or
    stroke the page's CSS gives the <svg> is passed on to it.
    """
    colour = style.get("color", (0, 0, 0, 255))

    def attributes(node, root):
        attrs = dict(node.attrs)
        if root:
            attrs.setdefault("xmlns", "http://www.w3.org/2000/svg")
            attrs["color"] = "#%02x%02x%02x" % colour[:3]
            for name in ("fill", "stroke"):
                value = style.get(name)
                if isinstance(value, str) and value.strip():
                    attrs[name] = value.strip()
            if "viewbox" not in attrs and attrs.get("width") and attrs.get("height"):
                attrs["viewbox"] = f"0 0 {attrs['width']} {attrs['height']}"
        parts = []
        for key, value in attrs.items():
            name = _SVG_NAMES.get(key, key)
            if name == "style":
                continue
            value = (value or "").replace("currentcolor", "currentColor")
            if value.strip().lower() == "currentcolor":
                value = "#%02x%02x%02x" % colour[:3]
            parts.append(f'{name}="{_svg_escape(value)}"')
        return (" " + " ".join(parts)) if parts else ""

    def render(node, root=False):
        tag = _SVG_NAMES.get(node.tag, node.tag)
        inner = []
        for child in node.children:
            if isinstance(child, Element):
                inner.append(render(child))
            else:
                inner.append(_svg_escape(child.data))
        return f"<{tag}{attributes(node, root)}>{''.join(inner)}</{tag}>"

    return render(element, root=True)
