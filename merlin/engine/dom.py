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
    __slots__ = ("tag", "attrs", "_class_cache", "pseudo", "comments")

    def __init__(self, tag: str, attrs: dict | None = None):
        super().__init__()
        self.tag = tag
        self.attrs = attrs or {}
        self.pseudo = None          # "before" or "after": a pseudo-element's box, made by the cascade
        self.comments = None        # [(child index, text)]: kept for the page's scripts, not drawn

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def classes(self) -> frozenset:
        # split once, and again only if the attribute changes: the cascade
        # asked 4.4 million times for GitHub's front page
        source = self.attrs.get("class", "")
        try:
            cached = self._class_cache
        except AttributeError:
            cached = None
        if cached is None or cached[0] is not source:
            cached = (source, frozenset(source.split()))
            self._class_cache = cached
        return cached[1]

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



_RAW = {"script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes", "plaintext"}


def to_html(document) -> str:
    """The document as HTML, as its tree now is: html, head and body always there.

    A page's scripts are handed this, rather than the page as it came, so they
    start from the tree Merlin Engine built: a page written without <html> or
    <body> had become, for the script host's DOM, a document made of its first
    element alone.
    """
    from .html import VOID

    out = ["<!DOCTYPE html>"]

    def escape(text: str, attribute: bool = False) -> str:
        text = text.replace("&", "&amp;")
        return text.replace('"', "&quot;") if attribute else text.replace("<", "&lt;").replace(">", "&gt;")

    def walk(node, raw: bool) -> None:
        if isinstance(node, Text):
            out.append(node.data if raw else escape(node.data))
            return
        if not isinstance(node, Element) or node.pseudo:
            return                   # ::before and ::after are drawn, not part of the page
        out.append("<" + node.tag)
        for name, value in node.attrs.items():
            out.append(f' {name}="{escape(value, True)}"')
        out.append(">")
        if node.tag in VOID:
            return
        # the page's comments back in their places: Svelte, Vue and React mark
        # where their components are with them, and hydrate from those marks.
        # Left out, Hugging Face's app found nothing to start from.
        notes = list(node.comments or [])
        place = 0
        for child in node.children:
            if isinstance(child, Element) and child.pseudo:
                continue
            while notes and notes[0][0] <= place:
                out.append(f"<!--{notes.pop(0)[1]}-->")
            walk(child, node.tag in _RAW)
            place += 1
        for _place, text in notes:
            out.append(f"<!--{text}-->")
        out.append(f"</{node.tag}>")

    walk(document.root, False)
    return "".join(out)
