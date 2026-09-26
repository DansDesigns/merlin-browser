"""CSS: parsing stylesheets, matching selectors, and the cascade.

Covers what ordinary documents lean on: type, class, id and attribute
selectors with the descendant, child and sibling combinators; specificity and
!important; the default stylesheet every browser has; inheritance; and values
in px, em, rem, pt and percentages. Selectors this engine cannot evaluate yet,
such as :hover or ::before, never match, so a rule meant only for a moment of
interaction is not applied all the time.
"""
from __future__ import annotations

import re

from .dom import Document, Element

# ------------------------------------------------------------------ values

NAMED_COLOURS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000",
    "blue": "#0000ff", "yellow": "#ffff00", "orange": "#ffa500", "purple": "#800080",
    "gray": "#808080", "grey": "#808080", "silver": "#c0c0c0", "maroon": "#800000",
    "navy": "#000080", "teal": "#008080", "olive": "#808000", "lime": "#00ff00",
    "aqua": "#00ffff", "cyan": "#00ffff", "fuchsia": "#ff00ff", "magenta": "#ff00ff",
    "pink": "#ffc0cb", "brown": "#a52a2a", "gold": "#ffd700", "indigo": "#4b0082",
    "violet": "#ee82ee", "coral": "#ff7f50", "salmon": "#fa8072", "tomato": "#ff6347",
    "crimson": "#dc143c", "darkred": "#8b0000", "darkblue": "#00008b",
    "darkgreen": "#006400", "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9",
    "lightgray": "#d3d3d3", "lightgrey": "#d3d3d3", "lightblue": "#add8e6",
    "lightgreen": "#90ee90", "lightyellow": "#ffffe0", "whitesmoke": "#f5f5f5",
    "gainsboro": "#dcdcdc", "dimgray": "#696969", "dimgrey": "#696969",
    "slategray": "#708090", "steelblue": "#4682b4", "royalblue": "#4169e1",
    "dodgerblue": "#1e90ff", "skyblue": "#87ceeb", "tan": "#d2b48c",
    "beige": "#f5f5dc", "ivory": "#fffff0", "khaki": "#f0e68c", "linen": "#faf0e6",
    "snow": "#fffafa", "seagreen": "#2e8b57", "forestgreen": "#228b22",
    "chocolate": "#d2691e", "firebrick": "#b22222", "orchid": "#da70d6",
    "plum": "#dda0dd", "turquoise": "#40e0d0", "wheat": "#f5deb3",
    "aliceblue": "#f0f8ff", "ghostwhite": "#f8f8ff", "honeydew": "#f0fff0",
    "lavender": "#e6e6fa", "mintcream": "#f5fffa", "rebeccapurple": "#663399",
}


def parse_colour(value: str, current=(0, 0, 0, 255)):
    """A CSS colour as (r, g, b, a) with 0-255 parts, or None if not one."""
    value = value.strip().lower()
    if value == "transparent":
        return (0, 0, 0, 0)
    if value == "currentcolor":
        return current
    value = NAMED_COLOURS.get(value, value)
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits)
        if len(digits) == 6:
            digits += "ff"
        if len(digits) == 8:
            try:
                return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4, 6))
            except ValueError:
                return None
        return None
    match = re.match(r"rgba?\(([^)]*)\)", value)
    if match:
        parts = re.split(r"[\s,/]+", match.group(1).strip())
        try:
            channels = []
            for part in parts[:3]:
                channels.append(round(float(part[:-1]) * 2.55) if part.endswith("%")
                                else round(float(part)))
            alpha = 255
            if len(parts) > 3:
                a = parts[3]
                alpha = round(float(a[:-1]) * 2.55) if a.endswith("%") else round(float(a) * 255)
            return tuple(max(0, min(255, c)) for c in channels) + (max(0, min(255, alpha)),)
        except ValueError:
            return None
    return None


_LENGTH = re.compile(r"^(-?[\d.]+)(px|em|rem|pt|%|vw|vh|ex|ch)?$")


def parse_length(value: str, font_size: float, root_size: float, viewport=(1024, 768)):
    """A length as px, or ("%", n) for a percentage, "auto", or None."""
    value = value.strip().lower()
    maths = re.match(r"^(min|max|clamp)\((.*)\)$", value)
    if maths:
        # min(), max() and clamp() over lengths that resolve now. A percentage
        # needs the containing block, known only at layout, so it is set
        # aside; the rest still give a sensible answer, as with
        # width: min(620px, 86vw).
        parts = [p.strip() for p in _split_outside(maths.group(2), ",")]
        resolved = [parse_length(p, font_size, root_size, viewport) for p in parts]
        numbers = [r for r in resolved if isinstance(r, float)]
        if not numbers:
            return next((r for r in resolved if isinstance(r, tuple)), None)
        if maths.group(1) == "min":
            return min(numbers)
        if maths.group(1) == "max":
            return max(numbers)
        if len(resolved) == 3 and all(isinstance(r, float) for r in resolved):
            low, preferred, high = resolved
            return max(low, min(preferred, high))
        return numbers[len(numbers) // 2]
    calc = re.match(r"^calc\((.*)\)$", value)
    if calc:
        # the simplest calc(): one length plus or minus another
        found = re.match(r"^\s*([^\s]+)\s*([+-])\s*([^\s]+)\s*$", calc.group(1))
        if found:
            left = parse_length(found.group(1), font_size, root_size, viewport)
            right = parse_length(found.group(3), font_size, root_size, viewport)
            if isinstance(left, float) and isinstance(right, float):
                return left + right if found.group(2) == "+" else left - right
        return None
    if value in ("auto", "none", "normal"):
        return "auto"
    if value == "0":
        return 0.0
    match = _LENGTH.match(value)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "px"
    return {
        "px": number, "em": number * font_size, "rem": number * root_size,
        "pt": number * 4 / 3, "ex": number * font_size / 2, "ch": number * font_size / 2,
        "vw": number * viewport[0] / 100, "vh": number * viewport[1] / 100,
    }.get(unit, ("%", number)) if unit != "%" else ("%", number)


FONT_KEYWORDS = {
    "xx-small": 9, "x-small": 10, "small": 13, "medium": 16,
    "large": 18, "x-large": 24, "xx-large": 32, "xxx-large": 48,
}

INHERITED = {
    "color", "font-family", "font-size", "font-weight", "font-style",
    "line-height", "text-align", "white-space", "text-decoration",
    "list-style-type", "visibility", "cursor",
}

# ---------------------------------------------------------------- selectors


class Selector:
    """One complex selector, stored right to left for matching."""

    __slots__ = ("parts", "specificity", "key", "matchable")

    def __init__(self, parts, specificity, matchable=True):
        self.parts = parts                # [(compound, combinator-to-the-left)]
        self.specificity = specificity
        self.matchable = matchable
        compound = parts[0][0] if parts else {}
        if compound.get("id"):
            self.key = ("id", compound["id"])
        elif compound.get("classes"):
            self.key = ("class", sorted(compound["classes"])[0])
        elif compound.get("tag") and compound["tag"] != "*":
            self.key = ("tag", compound["tag"])
        else:
            self.key = ("any", "")


_SIMPLE = re.compile(
    r"""(?P<tag>\*|[a-zA-Z][\w-]*)
      | \#(?P<id>[\w-]+)
      | \.(?P<cls>[\w-]+)
      | \[(?P<attr>[^\]]+)\]
      | ::?(?P<pseudo>[\w-]+(?:\([^)]*\))?)""", re.X)


def _compound(text: str):
    """One compound selector, like a.link#main, as a dict; None if unusable."""
    compound = {"tag": "*", "id": "", "classes": set(), "attrs": [], "pseudo": []}
    position = 0
    matchable = True
    while position < len(text):
        match = _SIMPLE.match(text, position)
        if not match or match.end() == position:
            return None, False
        if match.group("tag"):
            compound["tag"] = match.group("tag").lower()
        elif match.group("id"):
            compound["id"] = match.group("id")
        elif match.group("cls"):
            compound["classes"].add(match.group("cls"))
        elif match.group("attr"):
            body = match.group("attr").strip()
            op = re.match(r"^([\w-]+)\s*([~|^$*]?=)?\s*(.*)$", body)
            if not op:
                return None, False
            value = op.group(3).strip().strip("'\"")
            flags = ""
            if value.endswith(" i") or value.endswith(" s"):
                value, flags = value[:-2].strip().strip("'\""), value[-1]
            compound["attrs"].append((op.group(1).lower(), op.group(2) or "", value, flags))
        else:
            pseudo = match.group("pseudo").lower()
            if pseudo in ("first-child", "last-child", "only-child", "root", "link",
                          "any-link", "empty"):
                compound["pseudo"].append(pseudo)
            else:
                matchable = False            # :hover, ::before and the like
        position = match.end()
    return compound, matchable


def parse_selector(text: str) -> Selector | None:
    text = text.strip()
    if not text:
        return None
    tokens = re.split(r"\s*([>+~])\s*|\s+", text)
    parts = []                           # left to right: [compound, combinator, ...]
    matchable = True
    specificity = [0, 0, 0]
    pending = " "
    sequence = []
    for token in tokens:
        if token is None or token == "":
            continue
        if token in (">", "+", "~"):
            pending = token
            continue
        compound, ok = _compound(token)
        if compound is None:
            return None
        matchable = matchable and ok
        specificity[0] += bool(compound["id"])
        specificity[1] += (len(compound["classes"]) + len(compound["attrs"])
                           + len(compound["pseudo"]))
        specificity[2] += compound["tag"] != "*"
        sequence.append((compound, pending))
        pending = " "
    if not sequence:
        return None
    # right to left, each with the combinator joining it to the one before
    right_to_left = []
    for index in range(len(sequence) - 1, -1, -1):
        compound = sequence[index][0]
        combinator = sequence[index][1] if index > 0 else ""
        right_to_left.append((compound, combinator))
    return Selector(right_to_left, tuple(specificity), matchable)


def _siblings_before(element: Element):
    parent = element.parent
    if parent is None:
        return []
    siblings = [c for c in parent.children if isinstance(c, Element)]
    return siblings[:siblings.index(element)]


def _matches_compound(compound, element: Element) -> bool:
    if compound["tag"] != "*" and compound["tag"] != element.tag:
        return False
    if compound["id"] and compound["id"] != element.id:
        return False
    if compound["classes"] and not compound["classes"] <= element.classes:
        return False
    for name, op, value, flags in compound["attrs"]:
        if name not in element.attrs:
            return False
        actual = element.attrs[name]
        if flags == "i":
            actual, value = actual.lower(), value.lower()
        if op == "=" and actual != value:
            return False
        if op == "~=" and value not in actual.split():
            return False
        if op == "|=" and not (actual == value or actual.startswith(value + "-")):
            return False
        if op == "^=" and not (value and actual.startswith(value)):
            return False
        if op == "$=" and not (value and actual.endswith(value)):
            return False
        if op == "*=" and not (value and value in actual):
            return False
    for pseudo in compound["pseudo"]:
        parent = element.parent
        siblings = ([c for c in parent.children if isinstance(c, Element)]
                    if parent is not None else [element])
        if pseudo == "first-child" and siblings[0] is not element:
            return False
        if pseudo == "last-child" and siblings[-1] is not element:
            return False
        if pseudo == "only-child" and len(siblings) != 1:
            return False
        if pseudo == "root" and element.parent is not None:
            return False
        if pseudo in ("link", "any-link") and not (element.tag == "a" and "href" in element.attrs):
            return False
        if pseudo == "empty" and element.children:
            return False
    return True


def matches(selector: Selector, element: Element) -> bool:
    if not selector.matchable:
        return False

    def match_from(index: int, candidate: Element) -> bool:
        compound, combinator = selector.parts[index]
        if not _matches_compound(compound, candidate):
            return False
        if index == len(selector.parts) - 1:
            return True
        if combinator == " ":
            node = candidate.parent
            while isinstance(node, Element):
                if match_from(index + 1, node):
                    return True
                node = node.parent
            return False
        if combinator == ">":
            return isinstance(candidate.parent, Element) and match_from(index + 1, candidate.parent)
        if combinator == "+":
            before = _siblings_before(candidate)
            return bool(before) and match_from(index + 1, before[-1])
        if combinator == "~":
            return any(match_from(index + 1, s) for s in _siblings_before(candidate))
        return False

    return match_from(0, element)


# --------------------------------------------------------------- stylesheet


class Rule:
    __slots__ = ("selector", "declarations", "order")

    def __init__(self, selector, declarations, order):
        self.selector = selector
        self.declarations = declarations
        self.order = order


def parse_declarations(text: str) -> list:
    """'color: red; margin: 0 !important' as [(name, value, important)]."""
    found = []
    for piece in _split_outside(text, ";"):
        if ":" not in piece:
            continue
        name, value = piece.split(":", 1)
        name, value = name.strip().lower(), value.strip()
        important = False
        if value.lower().endswith("!important"):
            important = True
            value = value[: -len("!important")].strip()
        if name and value:
            found.append((name, value, important))
    return found


def _split_outside(text: str, separator: str) -> list:
    """Split on separator, but not inside (), [] or quotes."""
    parts, depth, quote, start = [], 0, "", 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def parse_stylesheet(text: str, start_order: int = 0) -> list:
    """A stylesheet as rules. @media blocks are read unless meant only for print."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules = []
    order = start_order
    position = 0
    while position < len(text):
        brace = text.find("{", position)
        if brace < 0:
            break
        prelude = text[position:brace].strip()
        # find the matching close brace
        depth, index = 1, brace + 1
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        body = text[brace + 1:index - 1]
        position = index
        if prelude.startswith("@"):
            lowered = prelude.lower()
            if lowered.startswith("@media") and "print" not in lowered.replace("not print", ""):
                inner = parse_stylesheet(body, order)
                rules.extend(inner)
                order += len(inner) + 1
            # @font-face, @keyframes, @supports and the rest: not yet
            continue
        declarations = parse_declarations(body)
        if not declarations:
            continue
        for part in _split_outside(prelude, ","):
            selector = parse_selector(part)
            if selector is not None:
                rules.append(Rule(selector, declarations, order))
                order += 1
    return rules


# ---------------------------------------------------------------- cascade

DEFAULT_STYLESHEET = """
html, body, div, p, h1, h2, h3, h4, h5, h6, ul, ol, li, dl, dt, dd, pre,
blockquote, address, article, aside, footer, header, main, nav, section,
figure, figcaption, form, fieldset, hr, center, details, summary,
menu { display: block }
table { display: table; border-spacing: 2px }
caption { display: table-caption; text-align: center }
thead, tbody, tfoot { display: table-row-group }
tr { display: table-row }
td, th { display: table-cell; vertical-align: middle }
li { display: list-item }
head, script, style, title, meta, link, noscript, template, base, datalist,
param, source, track { display: none }
[hidden] { display: none }
body { margin: 8px }
p, blockquote, figure, dl, pre, ul, ol, menu, fieldset { margin-top: 1em; margin-bottom: 1em }
blockquote, figure { margin-left: 40px; margin-right: 40px }
h1 { font-size: 2em; font-weight: bold; margin-top: 0.67em; margin-bottom: 0.67em }
h2 { font-size: 1.5em; font-weight: bold; margin-top: 0.83em; margin-bottom: 0.83em }
h3 { font-size: 1.17em; font-weight: bold; margin-top: 1em; margin-bottom: 1em }
h4 { font-weight: bold; margin-top: 1.33em; margin-bottom: 1.33em }
h5 { font-size: 0.83em; font-weight: bold; margin-top: 1.67em; margin-bottom: 1.67em }
h6 { font-size: 0.67em; font-weight: bold; margin-top: 2.33em; margin-bottom: 2.33em }
ul, ol, menu { padding-left: 40px }
ul { list-style-type: disc }
ol { list-style-type: decimal }
ul ul { list-style-type: circle }
dd { margin-left: 40px }
b, strong, th { font-weight: bold }
i, em, cite, var, dfn, address { font-style: italic }
pre, code, kbd, samp, tt { font-family: monospace }
pre { white-space: pre }
a { color: #0000ee; text-decoration: underline; cursor: pointer }
u, ins { text-decoration: underline }
s, strike, del { text-decoration: line-through }
small { font-size: smaller }
big { font-size: larger }
center { text-align: center }
hr { border-top: 1px solid #888888; margin-top: 0.5em; margin-bottom: 0.5em }
th { text-align: center }
td, th { padding: 1px }
img { display: inline }
input, select, textarea, button { display: inline-block }
"""

_DEFAULT_RULES = parse_stylesheet(DEFAULT_STYLESHEET)


class Styler:
    """Matches a document's rules to its elements and computes their styles."""

    def __init__(self, document: Document, author_css: list[str] | None = None,
                 viewport=(1024, 768), extra_css: str = ""):
        self.document = document
        self.viewport = viewport
        author = []
        order = 100000
        for text in (author_css if author_css is not None else document.stylesheets()):
            parsed = parse_stylesheet(text, order)
            author.extend(parsed)
            order += len(parsed) + 1
        if extra_css:                          # Merlin's own additions, such as hiding rules
            author.extend(parse_stylesheet(extra_css, order))
        self._index = {"default": self._build_index(_DEFAULT_RULES),
                       "author": self._build_index(author)}
        self.styles: dict = {}

    @staticmethod
    def _build_index(rules):
        index: dict = {}
        for rule in rules:
            index.setdefault(rule.selector.key, []).append(rule)
        return index

    def _candidates(self, index, element: Element):
        keys = [("any", ""), ("tag", element.tag)]
        if element.id:
            keys.append(("id", element.id))
        keys.extend(("class", name) for name in element.classes)
        for key in keys:
            yield from index.get(key, ())

    def compute(self) -> dict:
        """Computed style for every element: a dict of property to value."""
        root_size = 16.0
        self._compute(self.document.root, None, root_size)
        return self.styles

    def _declared(self, element: Element) -> dict:
        found = []   # (layer, specificity, order, name, value)
        for origin, layer_normal, layer_important in (("default", 0, 5), ("author", 1, 4)):
            for rule in self._candidates(self._index[origin], element):
                if matches(rule.selector, element):
                    for name, value, important in rule.declarations:
                        found.append((layer_important if important else layer_normal,
                                      rule.selector.specificity, rule.order, name, value))
        for name, value in presentational_hints(element):
            found.append((1, (0, 0, 0), -1, name, value))
        inline = element.attrs.get("style")
        if inline:
            for name, value, important in parse_declarations(inline):
                found.append((4 if important else 2, (1, 0, 0), 10 ** 9, name, value))
        found.sort(key=lambda item: (item[0], item[1], item[2]))
        declared = {}
        for _layer, _spec, _order, name, value in found:
            for real_name, real_value in expand_shorthand(name, value):
                declared[real_name] = real_value
        return declared

    def _compute(self, element: Element, parent: dict | None, root_size: float) -> None:
        declared = self._declared(element)
        style = {}
        parent = parent or {}
        # inherited properties start from the parent's values
        for name in INHERITED:
            if name in parent:
                style[name] = parent[name]
        parent_font = parent.get("font-size", 16.0)
        # font-size first: em lengths depend on it
        style["font-size"] = self._font_size(declared.get("font-size"), parent_font, root_size)
        if element.parent is None:
            root_size = style["font-size"]
        font = style["font-size"]
        for name, value in declared.items():
            if name == "font-size":
                continue
            if value.strip().lower() == "inherit":
                if name in parent:
                    style[name] = parent[name]
                continue
            style[name] = self._value(name, value, font, root_size, style)
        style.setdefault("display", "inline")
        style.setdefault("color", (0, 0, 0, 255))
        style.setdefault("font-family", "sans-serif")
        style.setdefault("font-weight", 400)
        style.setdefault("font-style", "normal")
        style.setdefault("line-height", ("normal",))
        style.setdefault("text-align", "left")
        style.setdefault("white-space", "normal")
        style.setdefault("text-decoration", "none")
        style.setdefault("list-style-type", "disc")
        self.styles[element] = style
        for child in element.children:
            if isinstance(child, Element):
                self._compute(child, style, root_size)

    def _font_size(self, value, parent_size: float, root_size: float) -> float:
        if not value:
            return parent_size
        value = value.strip().lower()
        if value in FONT_KEYWORDS:
            return float(FONT_KEYWORDS[value])
        if value == "smaller":
            return parent_size / 1.2
        if value == "larger":
            return parent_size * 1.2
        length = parse_length(value, parent_size, root_size, self.viewport)
        if isinstance(length, tuple):
            return parent_size * length[1] / 100
        if isinstance(length, float) and length > 0:
            return length
        return parent_size

    def _value(self, name: str, value: str, font: float, root_size: float, style: dict):
        lowered = value.strip().lower()
        if name in ("color", "background-color") or name.endswith("-color"):
            colour = parse_colour(lowered, style.get("color", (0, 0, 0, 255)))
            return colour if colour is not None else style.get(name)
        if name == "font-weight":
            if lowered in ("bold", "bolder"):
                return 700
            if lowered in ("normal", "lighter"):
                return 400
            try:
                return int(float(lowered))
            except ValueError:
                return 400
        if name == "line-height":
            if lowered == "normal":
                return ("normal",)
            try:
                return ("factor", float(lowered))
            except ValueError:
                length = parse_length(lowered, font, root_size, self.viewport)
                if isinstance(length, tuple):
                    return ("factor", length[1] / 100)
                if isinstance(length, float):
                    return ("px", length)
                return ("normal",)
        if name == "font-family":
            return value.strip()
        if name in ("flex-grow", "flex-shrink"):
            try:
                return max(0.0, float(lowered))
            except ValueError:
                return 1.0 if name == "flex-shrink" else 0.0
        if name == "order":
            try:
                return int(float(lowered))
            except ValueError:
                return 0
        if name in ("flex-direction", "flex-wrap", "justify-content", "align-items",
                    "align-self", "align-content", "vertical-align", "border-collapse"):
            return lowered.split()[-1] if lowered else ""
        if name in ("display", "text-align", "white-space", "font-style",
                    "text-decoration", "list-style-type", "visibility",
                    "border-top-style", "border-right-style", "border-bottom-style",
                    "border-left-style", "cursor", "float", "position", "overflow",
                    "text-decoration-line", "box-sizing"):
            if name == "text-decoration":
                if "underline" in lowered:
                    return "underline"
                if "line-through" in lowered:
                    return "line-through"
                return "none"
            return lowered.split()[0] if lowered else ""
        if (name.startswith(("margin-", "padding-", "border-")) and name.endswith(("width", "top", "right", "bottom", "left"))) \
                or name in ("width", "max-width", "min-width", "height", "min-height", "max-height",
                            "text-indent", "row-gap", "column-gap", "flex-basis",
                            "border-top-left-radius", "border-top-right-radius",
                            "border-bottom-right-radius", "border-bottom-left-radius"):
            if name.startswith("border-") and name.endswith("-width"):
                lowered = {"thin": "1px", "medium": "3px", "thick": "5px"}.get(lowered, lowered)
            length = parse_length(lowered, font, root_size, self.viewport)
            return length if length is not None else "auto"
        return value.strip()


# -------------------------------------------------------------- shorthands


def _sides(values: list) -> list:
    if len(values) == 1:
        return values * 4
    if len(values) == 2:
        return [values[0], values[1], values[0], values[1]]
    if len(values) == 3:
        return [values[0], values[1], values[2], values[1]]
    return values[:4]


_BORDER_STYLES = {"none", "hidden", "dotted", "dashed", "solid", "double",
                  "groove", "ridge", "inset", "outset"}


def _border_parts(value: str):
    width = style = colour = None
    for token in _split_outside(value, " "):
        token = token.strip()
        if not token:
            continue
        lowered = token.lower()
        if lowered in _BORDER_STYLES:
            style = lowered
        elif lowered in ("thin", "medium", "thick") or re.match(r"^-?[\d.]", lowered):
            width = lowered
        else:
            colour = token
    return width, style, colour


def expand_shorthand(name: str, value: str) -> list:
    """A shorthand property as the longhand properties it sets."""
    sides = ("top", "right", "bottom", "left")
    if name in ("margin", "padding"):
        values = [v for v in _split_outside(value, " ") if v.strip()]
        if not values:
            return []
        return [(f"{name}-{side}", v) for side, v in zip(sides, _sides(values))]
    if name in ("border", "border-top", "border-right", "border-bottom", "border-left"):
        width, style, colour = _border_parts(value)
        if value.strip().lower() in ("none", "0"):
            width, style = "0", "none"
        targets = sides if name == "border" else (name.split("-")[1],)
        found = []
        for side in targets:
            found.append((f"border-{side}-width", width or "medium"))
            found.append((f"border-{side}-style", style or "none"))
            found.append((f"border-{side}-color", colour or "currentcolor"))
        return found
    if name in ("border-width", "border-style", "border-color"):
        kind = name.split("-")[1]
        values = [v for v in _split_outside(value, " ") if v.strip()]
        return [(f"border-{side}-{kind}", v) for side, v in zip(sides, _sides(values))]
    if name in ("background", "background-image") and "gradient(" in value.lower():
        # Gradients are not drawn yet: their first colour stands in, so a page
        # with light text on a dark gradient stays readable rather than
        # falling back to white.
        for token in re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|\b[a-zA-Z]+\b", value):
            if token.lower() not in ("linear", "radial", "conic", "gradient", "to", "deg",
                                     "left", "right", "top", "bottom", "repeating", "in",
                                     "circle", "ellipse", "at", "center", "closest",
                                     "farthest", "side", "corner", "srgb", "oklab"):
                colour = parse_colour(token)
                if colour is not None:
                    return [("background-color", token)]
        return []
    if name == "background":
        for token in reversed(_split_outside(value, " ")):
            if parse_colour(token) is not None:
                return [("background-color", token)]
        return [("background-color", "transparent")] if value.strip().lower() == "none" else []
    if name == "list-style":
        for token in value.split():
            if token.lower() in ("disc", "circle", "square", "decimal", "none",
                                 "lower-alpha", "upper-alpha", "lower-roman", "upper-roman"):
                return [("list-style-type", token)]
        return []
    if name == "font":
        found = []
        for token in value.split():
            lowered = token.lower()
            if lowered in ("bold", "bolder", "lighter") or lowered.isdigit():
                found.append(("font-weight", lowered))
            elif lowered in ("italic", "oblique"):
                found.append(("font-style", lowered))
            elif re.match(r"^[\d.]+(px|em|rem|pt|%)(/.*)?$", lowered):
                size, _slash, height = lowered.partition("/")
                found.append(("font-size", size))
                if height:
                    found.append(("line-height", height))
        family = value.split()[-1] if value.split() else ""
        if family and not re.match(r"^[\d.]", family):
            found.append(("font-family", family))
        return found
    if name == "text-decoration-line":
        return [("text-decoration", value)]
    if name == "gap" or name == "grid-gap":
        values = [v for v in value.split() if v]
        if not values:
            return []
        return [("row-gap", values[0]), ("column-gap", values[1] if len(values) > 1 else values[0])]
    if name == "flex-flow":
        found = []
        for token in value.lower().split():
            if token in ("row", "row-reverse", "column", "column-reverse"):
                found.append(("flex-direction", token))
            elif token in ("wrap", "nowrap", "wrap-reverse"):
                found.append(("flex-wrap", token))
        return found
    if name == "flex":
        tokens = value.lower().split()
        if tokens == ["none"]:
            return [("flex-grow", "0"), ("flex-shrink", "0"), ("flex-basis", "auto")]
        if tokens == ["auto"]:
            return [("flex-grow", "1"), ("flex-shrink", "1"), ("flex-basis", "auto")]
        if tokens in (["initial"], ["0 1 auto"]):
            return [("flex-grow", "0"), ("flex-shrink", "1"), ("flex-basis", "auto")]
        numbers = [t for t in tokens if re.match(r"^[\d.]+$", t)]
        lengths = [t for t in tokens if t not in numbers]
        grow = numbers[0] if numbers else "1"
        shrink = numbers[1] if len(numbers) > 1 else "1"
        # a flex with only numbers means a basis of 0: the space is shared
        # out from nothing, which is what makes flex: 1 columns equal
        basis = lengths[0] if lengths else "0px"
        return [("flex-grow", grow), ("flex-shrink", shrink), ("flex-basis", basis)]
    if name == "border-radius":
        values = [v for v in value.split("/")[0].split() if v]
        if not values:
            return []
        corners = ("top-left", "top-right", "bottom-right", "bottom-left")
        return [(f"border-{corner}-radius", v) for corner, v in zip(corners, _sides(values))]
    if name in ("place-items",):
        values = value.split()
        return [("align-items", values[0])] if values else []
    if name in ("place-content",):
        values = value.split()
        return ([("align-content", values[0]),
                 ("justify-content", values[-1])] if values else [])
    return [(name, value)]


# ---------------------------------------------------- presentational hints


def _html_length(value: str) -> str:
    """An HTML attribute length, "300" or "50%", as a CSS one."""
    value = (value or "").strip()
    if not value:
        return ""
    if value.endswith("%"):
        return value
    try:
        return f"{float(value)}px"
    except ValueError:
        return ""


def _enclosing(element: Element, tag: str):
    node = element.parent
    while isinstance(node, Element):
        if node.tag == tag:
            return node
        node = node.parent
    return None


def presentational_hints(element: Element) -> list:
    """What old HTML attributes say about style, as CSS declarations.

    Real pages still use border="1", cellpadding, width="100%", bgcolor and
    align, and browsers honour them as the weakest kind of author style: any
    CSS rule wins over them.
    """
    attrs = element.attrs
    tag = element.tag
    found = []
    if tag in ("table", "td", "th", "img", "col", "hr", "iframe") and attrs.get("width"):
        length = _html_length(attrs["width"])
        if length:
            found.append(("width", length))
    if tag in ("td", "th", "img", "tr", "iframe") and attrs.get("height"):
        length = _html_length(attrs["height"])
        if length:
            found.append(("height", length))
    if attrs.get("bgcolor") and tag in ("body", "table", "tr", "td", "th"):
        found.append(("background-color", attrs["bgcolor"]))
    if tag == "font":
        if attrs.get("color"):
            found.append(("color", attrs["color"]))
        if attrs.get("face"):
            found.append(("font-family", attrs["face"]))
    if tag == "body" and attrs.get("text"):
        found.append(("color", attrs["text"]))
    align = attrs.get("align", "").lower()
    if align:
        if tag == "table" and align == "center":
            found.extend([("margin-left", "auto"), ("margin-right", "auto")])
        elif tag in ("td", "th", "tr", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
            if align in ("left", "right", "center", "justify"):
                found.append(("text-align", align))
    if attrs.get("valign") and tag in ("td", "th", "tr"):
        found.append(("vertical-align", attrs["valign"].lower()))
    if tag == "table":
        border = attrs.get("border")
        if border is not None and border.strip() not in ("0", ""):
            width = _html_length(border) or "1px"
            found.extend([(f"border-{side}-{kind}", value)
                          for side in ("top", "right", "bottom", "left")
                          for kind, value in (("width", width), ("style", "outset"),
                                              ("color", "#808080"))])
        elif border is not None and border.strip() == "":
            found.extend([(f"border-{side}-{kind}", value)
                          for side in ("top", "right", "bottom", "left")
                          for kind, value in (("width", "1px"), ("style", "outset"),
                                              ("color", "#808080"))])
        if attrs.get("cellspacing") is not None:
            found.append(("border-spacing", _html_length(attrs["cellspacing"]) or "0px"))
    if tag in ("td", "th"):
        table = _enclosing(element, "table")
        if table is not None:
            padding = table.attrs.get("cellpadding")
            if padding is not None:
                found.append(("padding-top", _html_length(padding) or "0px"))
                found.append(("padding-right", _html_length(padding) or "0px"))
                found.append(("padding-bottom", _html_length(padding) or "0px"))
                found.append(("padding-left", _html_length(padding) or "0px"))
            border = table.attrs.get("border")
            if border is not None and border.strip() != "0":
                found.extend([(f"border-{side}-{kind}", value)
                              for side in ("top", "right", "bottom", "left")
                              for kind, value in (("width", "1px"), ("style", "inset"),
                                                  ("color", "#808080"))])
        if element.attrs.get("nowrap") is not None:
            found.append(("white-space", "nowrap"))
    return found
