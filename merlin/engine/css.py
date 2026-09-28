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
      | (?P<colons>::?)(?P<pseudo>[\w-]+)""", re.X)

# pseudo-classes that depend only on the document, so can be matched at rest
_STRUCTURAL = {"first-child", "last-child", "only-child", "root", "link", "any-link",
               "empty", "first-of-type", "last-of-type", "only-of-type", "disabled",
               "enabled", "checked", "required", "optional", "read-only", "read-write",
               "defined", "scope"}


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
            end = match.end()
            argument = None
            if end < len(text) and text[end] == "(":
                depth, index = 1, end + 1
                while index < len(text) and depth:
                    if text[index] == "(":
                        depth += 1
                    elif text[index] == ")":
                        depth -= 1
                    index += 1
                argument = text[end + 1:index - 1].strip()
                end = index
            if match.group("colons") == "::" or pseudo in ("before", "after",
                                                            "first-line", "first-letter"):
                matchable = False            # pseudo-elements: not yet
            elif argument is not None and pseudo in ("not", "is", "where", "matches",
                                                     "-webkit-any"):
                inner = [parse_selector(part) for part in _split_outside(argument, ",")]
                inner = [x for x in inner if x is not None]
                if pseudo == "not":
                    # :not() of what cannot be told at rest, such as :hover,
                    # is left true: at rest the element is not hovered
                    compound["pseudo"].append(("not", inner))
                else:
                    usable = [x for x in inner if x.matchable]
                    if not usable:
                        matchable = False
                    compound["pseudo"].append(("is" if pseudo != "where" else "where", usable))
            elif argument is not None and pseudo in ("nth-child", "nth-last-child",
                                                     "nth-of-type", "nth-last-of-type"):
                formula = _nth(argument)
                if formula is None:
                    matchable = False
                else:
                    compound["pseudo"].append((pseudo, formula))
            elif pseudo in _STRUCTURAL:
                compound["pseudo"].append(pseudo)
            else:
                matchable = False            # :hover, :focus, :target and the like
            position = end
            continue
        position = match.end()
    return compound, matchable


def parse_selector(text: str) -> Selector | None:
    text = text.strip()
    if not text:
        return None
    tokens = _selector_tokens(text)
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
        specificity[1] += len(compound["classes"]) + len(compound["attrs"])
        specificity[2] += compound["tag"] != "*"
        for pseudo in compound["pseudo"]:
            if isinstance(pseudo, tuple) and pseudo[0] in ("not", "is", "where"):
                if pseudo[0] != "where" and pseudo[1]:
                    strongest = max(x.specificity for x in pseudo[1])
                    specificity = [a + b for a, b in zip(specificity, strongest)]
            else:
                specificity[1] += 1
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
        if isinstance(pseudo, tuple):
            kind, argument = pseudo
            if kind == "not":
                if any(x.matchable and matches(x, element) for x in argument):
                    return False
            elif kind in ("is", "where"):
                if not any(matches(x, element) for x in argument):
                    return False
            else:
                pool = siblings
                if kind.endswith("of-type"):
                    pool = [c for c in siblings if c.tag == element.tag]
                if kind.startswith("nth-last"):
                    pool = list(reversed(pool))
                if not _nth_holds(argument, pool.index(element) + 1):
                    return False
            continue
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
        if pseudo in ("first-of-type", "last-of-type", "only-of-type"):
            same = [c for c in siblings if c.tag == element.tag]
            if pseudo == "first-of-type" and same[0] is not element:
                return False
            if pseudo == "last-of-type" and same[-1] is not element:
                return False
            if pseudo == "only-of-type" and len(same) != 1:
                return False
        control = element.tag in ("input", "button", "select", "textarea", "option",
                                  "optgroup", "fieldset")
        if pseudo == "disabled" and not (control and "disabled" in element.attrs):
            return False
        if pseudo == "enabled" and not (control and "disabled" not in element.attrs):
            return False
        if pseudo == "checked" and not (("checked" in element.attrs and element.tag == "input")
                                        or ("selected" in element.attrs and element.tag == "option")):
            return False
        if pseudo == "required" and "required" not in element.attrs:
            return False
        if pseudo == "optional" and (not control or "required" in element.attrs):
            return False
        if pseudo == "read-only" and control and "readonly" not in element.attrs \
                and "disabled" not in element.attrs:
            return False
        if pseudo == "read-write" and not (control and "readonly" not in element.attrs
                                           and "disabled" not in element.attrs):
            return False
    return True


def _selector_tokens(text: str) -> list:
    """A selector split into compounds and combinators, never inside brackets.

    :nth-child(2n + 1) and :not(.a .b) keep their spaces and plus signs.
    """
    tokens, current, depth, quote = [], [], 0, ""
    for character in text:
        if quote:
            current.append(character)
            if character == quote:
                quote = ""
            continue
        if character in "'\"":
            quote = character
        elif character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        if depth == 0 and character in " \t\n>+~":
            if current:
                tokens.append("".join(current))
                current = []
            if character in ">+~":
                tokens.append(character)
            continue
        current.append(character)
    if current:
        tokens.append("".join(current))
    return tokens


def _nth(argument: str):
    """An+B from :nth-child(): (a, b), or None if it cannot be read."""
    text = argument.lower().replace(" ", "").split("of")[0]
    if text == "odd":
        return (2, 1)
    if text == "even":
        return (2, 0)
    found = re.match(r"^([+-]?\d*)n([+-]\d+)?$", text)
    if found:
        a = found.group(1)
        a = 1 if a in ("", "+") else -1 if a == "-" else int(a)
        return (a, int(found.group(2) or 0))
    if re.match(r"^[+-]?\d+$", text):
        return (0, int(text))
    return None


def _nth_holds(formula, position: int) -> bool:
    a, b = formula
    if a == 0:
        return position == b
    return (position - b) % a == 0 and (position - b) // a >= 0


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
    __slots__ = ("selector", "declarations", "order", "media")

    def __init__(self, selector, declarations, order, media=()):
        self.selector = selector
        self.declarations = declarations
        self.order = order
        self.media = media          # the @media conditions it sits inside, all to hold


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


def parse_stylesheet(text: str, start_order: int = 0, media: tuple = ()) -> list:
    """A stylesheet as rules, each keeping the @media conditions around it.

    Whether those conditions hold is decided later, for the window's size, so
    resizing across a breakpoint only has to choose rules again, not parse.
    @supports and @layer blocks are read; @container, @font-face, @keyframes
    and the like are not yet. Statement at-rules such as @import, @charset and
    @layer a, b; end at their semicolon.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules = []
    order = start_order
    position = 0
    length = len(text)
    while position < length:
        while position < length and text[position] in " \t\r\n":
            position += 1
        if position >= length:
            break
        brace = text.find("{", position)
        if text.startswith("@", position):
            semicolon = text.find(";", position)
            if semicolon >= 0 and (brace < 0 or semicolon < brace):
                position = semicolon + 1          # @import, @charset, @layer a, b;
                continue
        if brace < 0:
            break
        prelude = text[position:brace].strip()
        # find the matching close brace
        depth, index = 1, brace + 1
        while index < length and depth:
            character = text[index]
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
            index += 1
        body = text[brace + 1:index - 1]
        position = index
        if prelude.startswith("@"):
            lowered = prelude.lower()
            if lowered.startswith("@media"):
                inner = parse_stylesheet(body, order, media + (lowered[6:].strip(),))
            elif lowered.startswith("@supports"):
                # what is asked about is, in the main, what is supported here
                inner = [] if lowered[9:].strip().startswith("not") else \
                    parse_stylesheet(body, order, media)
            elif lowered.startswith("@layer") or lowered.startswith("@document"):
                inner = parse_stylesheet(body, order, media)
            else:
                inner = []          # @font-face, @keyframes, @container, @page...
            rules.extend(inner)
            order += len(inner) + 1
            continue
        declarations = parse_declarations(body)
        if not declarations:
            continue
        for part in _split_outside(prelude, ","):
            selector = parse_selector(part)
            if selector is not None:
                rules.append(Rule(selector, declarations, order, media))
                order += 1
    return rules


# Parsed stylesheets, by their text: a site's CSS is parsed once and reused
# from page to page.
_PARSED: dict = {}


def parsed(text: str) -> list:
    rules = _PARSED.get(text)
    if rules is None:
        rules = parse_stylesheet(text)
        if len(_PARSED) > 40:
            _PARSED.pop(next(iter(_PARSED)))
        _PARSED[text] = rules
    return rules


# ------------------------------------------------------------------- @media

_UNITS = {"px": 1.0, "em": 16.0, "rem": 16.0, "pt": 4 / 3, "cm": 96 / 2.54, "mm": 96 / 25.4,
          "in": 96.0, "vw": None, "vh": None}


def _media_length(text: str, viewport):
    found = re.match(r"^\s*(-?[\d.]+)\s*([a-z%]*)\s*$", text or "")
    if not found:
        return None
    number, unit = float(found.group(1)), found.group(2) or "px"
    if unit == "vw":
        return number * viewport[0] / 100
    if unit == "vh":
        return number * viewport[1] / 100
    factor = _UNITS.get(unit)
    return number * factor if factor else None


def _feature(feature: str, viewport, scheme: str) -> bool:
    """One media feature in brackets, as width >= 600px or min-width: 40em."""
    feature = feature.strip().lower()
    width, height = viewport
    # range syntax: (width >= 600px), (400px <= width < 900px)
    ranged = re.match(r"^(?:([^<>=]+?)\s*(<=|>=|<|>|=)\s*)?(width|height|aspect-ratio)"
                      r"\s*(?:(<=|>=|<|>|=)\s*([^<>=]+))?$", feature)
    if ranged and (ranged.group(2) or ranged.group(4)):
        size = width if ranged.group(3) == "width" else height
        if ranged.group(3) == "aspect-ratio":
            return True
        ok = True
        if ranged.group(1) is not None:
            limit = _media_length(ranged.group(1), viewport)
            ok = ok and limit is not None and _compare(limit, ranged.group(2), size)
        if ranged.group(5) is not None:
            limit = _media_length(ranged.group(5), viewport)
            ok = ok and limit is not None and _compare(size, ranged.group(4), limit)
        return ok
    name, _colon, value = feature.partition(":")
    name, value = name.strip(), value.strip()
    if name in ("min-width", "max-width", "min-height", "max-height",
                "min-device-width", "max-device-width"):
        limit = _media_length(value, viewport)
        if limit is None:
            return False
        size = width if "width" in name else height
        return size >= limit if name.startswith("min") else size <= limit
    if name == "orientation":
        return value == ("portrait" if height >= width else "landscape")
    if name == "prefers-color-scheme":
        return value == scheme
    if name == "prefers-reduced-motion":
        return value == "no-preference"
    if name == "prefers-contrast":
        return value == "no-preference"
    if name in ("hover", "any-hover"):
        return value in ("hover", "")
    if name in ("pointer", "any-pointer"):
        return value in ("fine", "")
    if name in ("min-resolution", "-webkit-min-device-pixel-ratio", "min--moz-device-pixel-ratio"):
        number = re.match(r"^([\d.]+)", value)
        return bool(number) and float(number.group(1)) <= 1.0
    if name in ("color", "min-color", "monochrome", "display-mode", "scripting",
                "forced-colors", "inverted-colors", "update", "dynamic-range"):
        return {"monochrome": False, "forced-colors": value == "none",
                "inverted-colors": value == "none", "scripting": value == "none",
                "display-mode": value == "browser", "dynamic-range": value == "standard"}.get(name, True)
    return False                               # unknown: as browsers, not matched


def _compare(a: float, op: str, b: float) -> bool:
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "=": a == b}[op]


def media_matches(query: str, viewport=(1024, 768), scheme: str = "light") -> bool:
    """Whether a media query list holds for a screen of this size."""
    query = query.strip().lower()
    if not query:
        return True
    for part in _split_outside(query, ","):
        part = part.strip()
        negate = False
        if part.startswith("not "):
            negate, part = True, part[4:].strip()
        elif part.startswith("only "):
            part = part[5:].strip()
        ok = True
        for piece in re.split(r"\s+and\s+", part):
            piece = piece.strip()
            if not piece:
                continue
            if piece.startswith("("):
                inner = piece[1:-1] if piece.endswith(")") else piece[1:]
                if inner.startswith("not "):
                    ok = ok and not _feature(inner[4:].strip("() "), viewport, scheme)
                elif " or " in inner:
                    ok = ok and any(_feature(x.strip("() "), viewport, scheme)
                                    for x in inner.split(" or "))
                else:
                    ok = ok and _feature(inner, viewport, scheme)
            else:
                ok = ok and piece in ("all", "screen")
        if ok != negate:
            return True
    return False


def _var(value: str, custom: dict, depth: int = 0):
    """value with each var(--name, fallback) replaced; None if one cannot be.

    A custom property may itself use var(); a loop or a missing name with no
    fallback makes the whole value invalid, as the standard says, and the
    property then takes its inherited or initial value.
    """
    if "var(" not in value:
        return value
    if depth > 24:
        return None
    out = []
    position = 0
    while True:
        start = value.find("var(", position)
        if start < 0:
            out.append(value[position:])
            break
        out.append(value[position:start])
        depth_here, index = 1, start + 4
        while index < len(value) and depth_here:
            if value[index] == "(":
                depth_here += 1
            elif value[index] == ")":
                depth_here -= 1
            index += 1
        inside = value[start + 4:index - 1]
        name, comma, fallback = inside.partition(",")
        name = name.strip()
        found = custom.get(name)
        if found is not None and found.strip():
            # a variable that cannot itself be resolved, as in a loop, counts as
            # missing: the fallback, if there is one, stands in
            found = _var(found, custom, depth + 1)
        else:
            found = None
        if found is None:
            if not comma:
                return None
            found = _var(fallback.strip(), custom, depth + 1)
            if found is None:
                return None
        out.append(found)
        position = index
    return "".join(out)


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
input, select, textarea, button { display: inline-block; font-size: 13.33px;
  font-family: sans-serif; color: #000000 }
input, textarea, select { border: 1px solid #8f8f9d; padding: 2px 3px; background-color: #ffffff }
select { border-radius: 3px }
button, input[type="submit"], input[type="button"], input[type="reset"] {
  border: 1px solid #8f8f9d; padding: 2px 8px; background-color: #e9e9ed; border-radius: 3px;
  text-align: center; cursor: pointer }
input[type="checkbox"], input[type="radio"] { border: none; padding: 0; background-color: transparent;
  margin: 3px 3px 3px 4px }
input[type="hidden"] { display: none }
"""

_DEFAULT_RULES = parse_stylesheet(DEFAULT_STYLESHEET)


class Styler:
    """Matches a document's rules to its elements and computes their styles."""

    def __init__(self, document: Document, author_css: list[str] | None = None,
                 viewport=(1024, 768), extra_css: str = "", scheme: str = "light"):
        self.document = document
        self.viewport = viewport
        self.scheme = scheme
        # each stylesheet's rules, with where in the cascade that sheet begins
        self._sheets = []
        base = 100000
        texts = list(author_css if author_css is not None else document.stylesheets())
        if extra_css:                          # Merlin's own additions, such as hiding rules
            texts.append(extra_css)
        for text in texts:
            rules = parsed(text)
            self._sheets.append((base, rules))
            base += 1000000
        self._choose()
        self.styles: dict = {}

    def _choose(self) -> None:
        """Index the rules whose @media conditions hold for the viewport now."""
        self._media = {}
        author: dict = {}
        for base, rules in self._sheets:
            for rule in rules:
                if rule.media and not all(self._holds(q) for q in rule.media):
                    continue
                author.setdefault(rule.selector.key, []).append((rule, base))
        default: dict = {}
        for rule in _DEFAULT_RULES:
            default.setdefault(rule.selector.key, []).append((rule, 0))
        self._index = {"default": default, "author": author}

    def _holds(self, query: str) -> bool:
        found = self._media.get(query)
        if found is None:
            found = media_matches(query, self.viewport, self.scheme)
            self._media[query] = found
        return found

    def media_changed(self, viewport) -> bool:
        """Whether a window of this size would choose different @media rules."""
        return any(media_matches(q, viewport, self.scheme) != held
                   for q, held in self._media.items())

    def restyle(self, viewport) -> dict:
        """Styles again for a new viewport: the rules are chosen again, not parsed."""
        self.viewport = viewport
        self._choose()
        self.styles = {}
        return self.compute()

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

    def _declared(self, element: Element) -> list:
        """Every declaration for the element, weakest first, not yet expanded."""
        found = []   # (layer, specificity, order, name, value)
        for origin, layer_normal, layer_important in (("default", 0, 5), ("author", 1, 4)):
            for rule, base in self._candidates(self._index[origin], element):
                if matches(rule.selector, element):
                    for name, value, important in rule.declarations:
                        found.append((layer_important if important else layer_normal,
                                      rule.selector.specificity, base + rule.order, name, value))
        for name, value in presentational_hints(element):
            found.append((1, (0, 0, 0), -1, name, value))
        inline = element.attrs.get("style")
        if inline:
            for name, value, important in parse_declarations(inline):
                found.append((4 if important else 2, (1, 0, 0), 10 ** 9, name, value))
        found.sort(key=lambda item: (item[0], item[1], item[2]))
        return [(name, value) for _layer, _spec, _order, name, value in found]

    def _compute(self, element: Element, parent: dict | None, root_size: float) -> None:
        parent = parent or {}
        ordered = self._declared(element)
        # Custom properties first: inherited, then the element's own, which
        # any var() below may use. A page that sets none shares its parent's.
        custom = parent.get("--", {})
        own = [(n, v) for n, v in ordered if n.startswith("--")]
        if own:
            custom = dict(custom)
            for name, value in own:
                custom[name] = value
        declared = {}
        for name, value in ordered:
            if name.startswith("--"):
                continue
            if "var(" in value:
                value = _var(value, custom)
                if value is None:
                    continue           # invalid once substituted: as if not set
            for real_name, real_value in expand_shorthand(name, value):
                declared[real_name] = real_value
        style = {"--": custom}
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
            keyword = value.strip().lower()
            if keyword == "inherit" or (keyword == "unset" and name in INHERITED):
                if name in parent:
                    style[name] = parent[name]
                continue
            if keyword in ("initial", "unset", "revert", "revert-layer"):
                style.pop(name, None)          # its initial value, set below
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
        if name == "transform":
            return _translation(lowered, font, root_size, self.viewport)
        if name in ("grid-template-columns", "grid-template-rows",
                    "grid-auto-columns", "grid-auto-rows"):
            return parse_tracks(lowered, font, root_size, self.viewport)
        if name == "grid-template-areas":
            # each quoted string is one row of area names
            rows = [a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', value)]
            return [row.split() for row in rows]
        if name in ("grid-column-start", "grid-column-end", "grid-row-start",
                    "grid-row-end", "grid-auto-flow"):
            return lowered.strip()
        if name in ("justify-items", "justify-self"):
            return lowered.split()[-1] if lowered else ""
        if name in ("flex-direction", "flex-wrap", "justify-content", "align-items",
                    "align-self", "align-content", "vertical-align", "border-collapse",
                    "clear"):
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
                            "top", "right", "bottom", "left",
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
    if name == "inset":
        values = [v for v in value.split() if v]
        if not values:
            return []
        return [(side, v) for side, v in zip(("top", "right", "bottom", "left"), _sides(values))]
    if name == "overflow-x" or name == "overflow-y":
        return [("overflow", value)] if value.strip().lower() not in ("visible", "") else []
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
        return ([("align-items", values[0]), ("justify-items", values[-1])]
                if values else [])
    if name == "place-self":
        values = value.split()
        return ([("align-self", values[0]), ("justify-self", values[-1])]
                if values else [])
    if name in ("grid-column", "grid-row"):
        start, _slash, end = value.partition("/")
        return [(f"{name}-start", start.strip() or "auto"),
                (f"{name}-end", end.strip() or "auto")]
    if name == "grid-area":
        parts = [p.strip() for p in value.split("/")]
        if len(parts) == 1 and parts[0] and not re.match(r"^-?\d", parts[0]) \
                and not parts[0].startswith("span"):
            # a name from grid-template-areas: all four edges from it
            return [("grid-area-name", parts[0])]
        parts += ["auto"] * (4 - len(parts))
        return [("grid-row-start", parts[0]), ("grid-column-start", parts[1]),
                ("grid-row-end", parts[2]), ("grid-column-end", parts[3])]
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


def _translation(value: str, font: float, root_size: float, viewport):
    """The translate() parts of a transform, as (x, y) lengths; None if none.

    Only translation is taken: it is how positioned elements are centred
    (left: 50%; transform: translateX(-50%)). Each part is px, or a
    percentage of the element's own size, which only layout knows.
    """
    x = y = 0.0
    found = False
    for kind, body in re.findall(r"(translate[xy]?|translate3d)\(([^)]*)\)", value):
        parts = [p.strip() for p in body.split(",")]
        if kind == "translatex":
            parts = [parts[0], "0"]
        elif kind == "translatey":
            parts = ["0", parts[0]]
        elif len(parts) == 1:
            parts.append("0")
        lengths = [parse_length(p, font, root_size, viewport) for p in parts[:2]]
        if any(length is None or length == "auto" for length in lengths):
            continue
        x, y = lengths
        found = True
    return (x, y) if found else None


# ------------------------------------------------------------ grid tracks


def _track(token: str, font: float, root_size: float, viewport):
    """One track size: ("px", n) ("pct", n) ("fr", n) ("auto",) ("min",) ("max",)."""
    token = token.strip()
    if token in ("auto", ""):
        return ("auto",)
    if token == "min-content":
        return ("min",)
    if token == "max-content":
        return ("max",)
    if token.endswith("fr"):
        try:
            return ("fr", float(token[:-2]))
        except ValueError:
            return ("auto",)
    length = parse_length(token, font, root_size, viewport)
    if isinstance(length, tuple):
        return ("pct", length[1])
    if isinstance(length, float):
        return ("px", length)
    return ("auto",)


def parse_tracks(value: str, font: float, root_size: float, viewport) -> list:
    """A grid track list as a list of tracks, repeat() kept for layout.

    minmax(a, b) is ("minmax", a, b), fit-content(x) is treated as
    minmax(auto, x), and repeat(n | auto-fill | auto-fit, ...) is
    ("repeat", n, [tracks]), since auto-fill needs the width to resolve.
    Line names in [brackets] are skipped.
    """
    value = re.sub(r"\[[^\]]*\]", " ", value or "").strip()
    if not value or value == "none":
        return []
    tracks = []
    for token in _split_outside(value, " "):
        token = token.strip()
        if not token:
            continue
        found = re.match(r"^(repeat|minmax|fit-content)\((.*)\)$", token)
        if not found:
            tracks.append(_track(token, font, root_size, viewport))
            continue
        kind, inner = found.group(1), found.group(2)
        args = [a.strip() for a in _split_outside(inner, ",")]
        if kind == "minmax" and len(args) == 2:
            tracks.append(("minmax", _track(args[0], font, root_size, viewport),
                           _track(args[1], font, root_size, viewport)))
        elif kind == "fit-content" and args:
            tracks.append(("minmax", ("auto",), _track(args[0], font, root_size, viewport)))
        elif kind == "repeat" and len(args) == 2:
            count = args[0]
            inner_tracks = parse_tracks(args[1], font, root_size, viewport)
            if count in ("auto-fill", "auto-fit"):
                tracks.append(("repeat", count, inner_tracks))
            else:
                try:
                    tracks.extend(inner_tracks * max(1, min(1000, int(count))))
                except ValueError:
                    pass
    return tracks
