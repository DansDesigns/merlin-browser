"""The document tree: elements and text, as the HTML parser builds them.

Deliberately small. Each element keeps its tag, its attributes and its
children; styling and layout information is attached later by the cascade
(css.py) and the layout (layout.py), never stored in the tree itself, so the
same document can be laid out again at another width or zoom.
"""
from __future__ import annotations


class Node:
    __slots__ = ("parent", "children")

    def __init__(self):
        self.parent: Element | None = None
        self.children: list[Node] = []

    def append(self, child: "Node") -> "Node":
        child.parent = self
        self.children.append(child)
        return child


class Text(Node):
    __slots__ = ("data",)

    def __init__(self, data: str):
        super().__init__()
        self.data = data

    def __repr__(self):
        return f"Text({self.data[:30]!r})"


class Element(Node):
    __slots__ = ("tag", "attrs")

    def __init__(self, tag: str, attrs: dict | None = None):
        super().__init__()
        self.tag = tag
        self.attrs = attrs or {}

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def classes(self) -> set:
        return set(self.attrs.get("class", "").split())

    def elements(self):
        """Every element below this one, in document order."""
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.elements()

    def find(self, tag: str):
        for element in self.elements():
            if element.tag == tag:
                return element
        return None

    def text(self) -> str:
        """All the text below this element, joined."""
        parts = []
        for child in self.children:
            if isinstance(child, Text):
                parts.append(child.data)
            elif isinstance(child, Element):
                parts.append(child.text())
        return "".join(parts)

    def __repr__(self):
        return f"<{self.tag}{' #' + self.id if self.id else ''}>"


class Document:
    """A parsed page: its tree, where it came from, and what it is called."""

    def __init__(self, root: Element, url: str = ""):
        self.root = root
        self.url = url

    @property
    def body(self) -> Element:
        return self.root.find("body") or self.root

    @property
    def title(self) -> str:
        found = self.root.find("title")
        return " ".join(found.text().split()) if found else ""

    def stylesheets(self) -> list[str]:
        """The text of every <style> element, in order, each within its media."""
        return [source[1] for source in self.stylesheet_sources() if source[0] == "style"]

    def stylesheet_sources(self) -> list:
        """Every stylesheet in document order: ("style", text) for one in the
        page, ("link", href, media) for one it links to."""
        found = []
        for element in self.root.elements():
            if element.tag == "style":
                media = element.attrs.get("media", "").strip()
                text = element.text()
                found.append(("style", f"@media {media} {{{text}}}" if media and media != "all"
                              else text))
            elif element.tag == "link" and element.attrs.get("href"):
                rel = element.attrs.get("rel", "").lower().split()
                if "stylesheet" in rel and "alternate" not in rel \
                        and "disabled" not in element.attrs:
                    found.append(("link", element.attrs["href"].strip(),
                                  element.attrs.get("media", "").strip()))
        return found
