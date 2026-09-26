"""HTML to a document tree.

Python's own html.parser does the tokenising: it is part of the standard
library, forgiving of broken markup, and brings no dependency. On top of it
this builds the tree the way browsers do for the common cases real pages rely
on: void elements never take children, a new block closes an open paragraph,
a new list item closes the previous one, and so on. It is not the full HTML
standard's tree construction, which has many more such rules; those can be
added as pages show they are needed.
"""
from __future__ import annotations

from html.parser import HTMLParser

from .dom import Document, Element, Text

# elements that can never have children
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}

# starting one of these closes a paragraph that is still open
CLOSES_P = {
    "address", "article", "aside", "blockquote", "details", "div", "dl",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "main", "menu", "nav", "ol", "p", "pre",
    "section", "table", "ul",
}

# starting the key closes an open element named in the value, if it is the
# nearest one of its kind: <li> after <li>, <td> after <th>, and so on
IMPLIED_END = {
    "li": {"li"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    # a new row closes the previous row, cells and all: stopping at an open
    # cell instead nested every row inside the one before
    "tr": {"tr"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "option": {"option"},
    "thead": {"thead", "tbody", "tfoot"},
    "tbody": {"thead", "tbody", "tfoot"},
    "tfoot": {"thead", "tbody", "tfoot"},
}

# what may come before the body without starting it
HEAD_ONLY = {"head", "title", "style", "meta", "link", "base", "script", "noscript",
             "template"}

# an implied end never reaches past one of these
SCOPE = {"ul", "ol", "dl", "table", "select", "html", "body", "div"}


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("html")
        self.stack: list[Element] = [self.root]

    @property
    def current(self) -> Element:
        return self.stack[-1]

    def _ensure_body(self) -> None:
        """Start the body if real content arrives with none open.

        Browsers always have a <body>, whether or not the page wrote one, and
        a great many pages do not. Without it the default margin and every
        body { ... } rule had nothing to apply to.
        """
        if len(self.stack) == 1 and self.root.find("body") is None:
            body = Element("body")
            self.root.append(body)
            self.stack.append(body)

    def _close(self, tag: str) -> bool:
        """Close the nearest open element with this tag, and all inside it."""
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return True
        return False

    def _close_implied(self, tag: str) -> None:
        if tag in CLOSES_P:
            for index in range(len(self.stack) - 1, 0, -1):
                name = self.stack[index].tag
                if name == "p":
                    del self.stack[index:]
                    break
                if name in SCOPE or name in ("button",):
                    break
        closes = IMPLIED_END.get(tag)
        if closes:
            for index in range(len(self.stack) - 1, 0, -1):
                name = self.stack[index].tag
                if name in closes:
                    del self.stack[index:]
                    break
                if name in SCOPE and name not in closes:
                    break

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "html":
            for key, value in attrs:
                self.root.attrs.setdefault(key, value or "")
            return
        if tag == "body":
            existing = self.root.find("body")
            if existing is not None:
                # the body was started already: this tag only adds attributes
                for key, value in attrs:
                    existing.attrs.setdefault(key.lower(), value or "")
                if existing not in self.stack:
                    self.stack = [self.root, existing]
                return
        elif tag not in HEAD_ONLY:
            self._ensure_body()
        self._close_implied(tag)
        element = Element(tag, {key.lower(): (value if value is not None else "")
                                for key, value in attrs})
        self.current.append(element)
        if tag not in VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self._close(tag.lower())                 # <div/> in HTML is just <div>

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID or tag == "html":
            return
        self._close(tag)                             # a stray end tag is ignored

    def handle_data(self, data):
        if not data:
            return
        if data.strip():
            self._ensure_body()
        last = self.current.children[-1] if self.current.children else None
        if isinstance(last, Text):
            last.data += data
        else:
            self.current.append(Text(data))


def parse(markup: str, url: str = "") -> Document:
    """Build a document from HTML, however untidy."""
    builder = _Builder()
    builder.feed(markup)
    builder.close()
    return Document(builder.root, url)
