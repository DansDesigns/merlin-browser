"""MerlinEngine: Merlin's own web engine, in pure Python on Qt's painting.

Built alongside Chromium, not in place of it yet: nothing in Merlin uses it
unless asked. The pipeline is the classic one, each stage its own module:

    html.py    markup to a document tree (dom.py)
    css.py     stylesheets, selectors, the cascade, computed styles
    layout.py  boxes and lines, as a display list
    paint.py   the display list onto a QPainter
    view.py    MerlinView, the widget: loading, navigation, scrolling

It uses no Chromium, no Rust and nothing beyond PyQt6 and Python's own
library. Try it with:  python -m merlin.engine <address or file>
"""
from .view import MerlinView

__all__ = ["MerlinView"]
