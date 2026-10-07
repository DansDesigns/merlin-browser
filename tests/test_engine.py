#!/usr/bin/env python3
"""Checks for MerlinEngine, Merlin's own web engine.

    python tests/test_engine.py

Parsing, the cascade, layout and the view, each checked against what a
browser does, plus a guard that the engine never imports Chromium. Quick:
nothing here waits on Chromium starting.
"""
from __future__ import annotations

import http.server
import os
import sys
import urllib.request
import socket
import tempfile
import shutil
import threading
import urllib.error
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QPoint, Qt, QTimer, QUrl             # noqa: E402
from PyQt6.QtWidgets import QApplication                      # noqa: E402

FAILED: list = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED.append(label)


def wait(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_no_chromium() -> None:
    """The engine is Merlin's own: nothing in it may reach for Chromium."""
    folder = os.path.join(ROOT, "merlin", "engine")
    offenders = []
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            text = open(os.path.join(folder, name), encoding="utf-8").read()
            if "QtWebEngine" in text or "QWebEngine" in text:
                offenders.append(name)
    check("MerlinEngine imports nothing from Chromium", not offenders, ", ".join(offenders))


def test_without_html_parser() -> None:
    """On a Merlin.exe with no html.parser the engine uses its own copy."""
    import subprocess

    script = (
        "import importlib.abc, sys\n"
        "class M(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name in ('html.parser', '_markupbase'):\n"
        "            raise ModuleNotFoundError(name)\n"
        "sys.meta_path.insert(0, M())\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "from merlin.engine import html\n"
        "doc = html.parse('<title>T</title><p>one<p>two<table><tr><td>a<td>b</table>"
        "<!-- c --><script>if (a < b) {}</script>&amp; &copy;')\n"
        "print(html.HTMLParser.__module__)\n"
        "print(','.join(e.tag for e in doc.root.elements()))\n"
        "print(doc.title, doc.body.text()[-3:])\n")
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    lines = done.stdout.splitlines()
    check("without html.parser, the engine falls back to its own copy",
          done.returncode == 0 and lines[:1] == ["merlin.engine._htmlparser"],
          (done.stderr or done.stdout).strip()[-120:])
    check("and builds the same tree the standard parser does",
          lines[1:] == ["title,body,p,p,table,tr,td,td,script", "T & \u00a9"], str(lines[1:]))


def test_parsing() -> None:
    from merlin.engine.dom import Element
    from merlin.engine.html import parse

    doc = parse("<title>T</title><p>one<p>two<div>block</div>"
                "<ul><li>a<li>b</ul><img src=x><br>after")
    # the content sits in the body, made for it as browsers do
    body = [c for c in doc.body.children if isinstance(c, Element)]
    tags = [e.tag for e in body]
    check("a new paragraph closes the open one, a div closes a paragraph",
          tags[:3] == ["p", "p", "div"], str(tags))
    lists = doc.root.find("ul")
    check("list items close each other",
          [c.tag for c in lists.children if isinstance(c, Element)] == ["li", "li"])
    image = doc.root.find("img")
    check("void elements never take children", image is not None and not image.children)
    check("the title is read", doc.title == "T")
    broken = parse("<div><span>unclosed<div>x</p></b></div>")
    check("stray and missing end tags do not break the tree",
          broken.root.find("span") is not None)


def test_cascade() -> None:
    from merlin.engine.css import Styler, parse_colour
    from merlin.engine.html import parse

    doc = parse("""<style>
      p { color: red } .note { color: green } #special { color: blue }
      div p { font-size: 20px } div > p.note { font-weight: bold }
      p:hover { color: orange } .loud { color: purple !important }
      h1 + p { font-style: italic }
    </style><body style="font-size: 10px"><h1>H</h1><p id=a>after</p>
    <div><p class=note>n</p><p id=special class=note>s</p>
    <p class=loud id=l style="color: teal">l</p><span>x <em>e</em></span>
    <p id=em style="font-size: 2em">two em</p></div></body>""")
    styles = Styler(doc).compute()

    def of(key, value):
        return styles[next(e for e in doc.root.elements() if e.attrs.get(key) == value)]

    check("specificity: an id beats a class beats a type",
          of("id", "special")["color"] == (0, 0, 255, 255)
          and of("class", "note")["color"] == (0, 128, 0, 255))
    check("!important beats an inline style", of("id", "l")["color"] == (128, 0, 128, 255))
    check("combinators: child, descendant and adjacent sibling",
          of("class", "note")["font-weight"] == 700
          and of("id", "special")["font-size"] == 20.0
          and of("id", "a")["font-style"] == "italic")
    check(":hover rules do not apply while nothing is hovered",
          of("id", "a")["color"] == (255, 0, 0, 255))
    em = next(e for e in doc.root.elements() if e.tag == "em")
    check("inherited values pass down", styles[em]["font-size"] == 10.0)
    check("em font sizes are relative to the parent", of("id", "em")["font-size"] == 20.0)
    check("colours: hex, rgba and names",
          parse_colour("#abc") == (170, 187, 204, 255)
          and parse_colour("rgba(10,20,30,.5)") == (10, 20, 30, 128)
          and parse_colour("rebeccapurple") == (102, 51, 153, 255))


def test_layout() -> None:
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    words = " ".join(["word"] * 60)
    doc = parse(f"<body style='margin:0'><p id=p style='margin:0'>{words}</p>"
                "<div id=box style='width:200px;margin:0 auto;height:10px'></div>"
                "<p style='margin:30px 0'>a</p><p style='margin:10px 0'>b</p></body>")
    styles = Styler(doc).compute()
    wide = Layout(doc, styles, 800).run()
    narrow = Layout(doc, styles, 300).run()
    check("text wraps to fit: a narrower page is taller", narrow.height > wide.height,
          f"{wide.height:.0f} -> {narrow.height:.0f}")
    from PyQt6.QtGui import QFontMetricsF

    for label, out, width in (("800", wide, 800), ("300", narrow, 300)):
        texts = [item for item in out.items if item and item[0] == "text"]
        widest = max(item[1] + QFontMetricsF(item[4]).horizontalAdvance(item[3])
                     for item in texts)
        check(f"no line of text runs past a {label}px page",
              texts and widest <= width + 0.5, f"widest line ends at {widest:.1f}")
    # margin: auto centres a fixed width block
    from PyQt6.QtCore import QRectF  # noqa: F401
    doc2 = parse("<body style='margin:0'><div style='width:200px;margin:0 auto;"
                 "height:20px;background:red'></div></body>")
    out = Layout(doc2, Styler(doc2).compute(), 800).run()
    rects = [sub for item in out.items if item and item[0] == "group"
             for sub in item[1] if sub[0] == "rect"]
    check("margin: auto centres a block of fixed width",
          rects and abs(rects[0][1].left() - 300) < 1, str(rects[0][1] if rects else None))
    # adjacent vertical margins collapse into the larger
    doc3 = parse("<body style='margin:0'><div style='margin-bottom:30px;height:10px'></div>"
                 "<div id=second style='margin-top:10px;height:10px'></div></body>")
    out3 = Layout(doc3, Styler(doc3).compute(), 800).run()
    check("adjacent vertical margins collapse", abs(out3.height - 50) < 1, f"{out3.height}")


def test_body_and_markers() -> None:
    from merlin.engine.css import Styler, expand_shorthand
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    doc = parse("<title>t</title><style>body { margin: 12px }</style><p>x")
    out = Layout(doc, Styler(doc).compute(), 400).run()
    first = next(i for i in out.items if i and i[0] == "text")
    check("a page with no <body> tag still gets one, and its margin",
          doc.body.tag == "body" and abs(first[1] - 12) < 0.5, f"x={first[1]:.0f}")
    # the word "image" in text was once taken for an image marker
    doc = parse("<p>an image and an anchor in plain words</p>")
    out = Layout(doc, Styler(doc).compute(), 400).run()
    words = " ".join(i[3] for i in out.items if i and i[0] == "text")
    check("the words image and anchor are just words", "image" in words and "anchor" in words)
    # gradients are drawn now; the shorthand keeps them as gradients
    check("a gradient background is kept as a gradient",
          expand_shorthand("background", "linear-gradient(90deg,#123456,#fff)")
          == [("background-image", "linear-gradient(90deg,#123456,#fff)"),
              ("background-color", "transparent")])


def test_tables() -> None:
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    # markup that never closes its rows or cells, as old pages do
    doc = parse("<table border=1 cellpadding=4><tr><th>A<th>B<tr><td>one<td>two"
                "<tr><td colspan=2>spanning both columns here</table>")
    rows = [e for e in doc.root.elements() if e.tag == "tr"]
    check("a new row closes the previous one rather than nesting in it",
          all(r.parent.tag == "table" for r in rows) and len(rows) == 3)
    styles = Styler(doc).compute()
    out = Layout(doc, styles, 800).run()
    texts = {i[3]: i for i in out.items if i and i[0] == "text"}
    check("every cell's text is laid out",
          all(t in texts for t in ("A", "B", "one", "two", "spanning both columns here")))
    # "one", not the header "A": header cells are centred in their column
    check("a table with no width is as wide as its contents, at the left",
          texts["one"][1] < 20 and max(i[1] for i in texts.values()) < 400,
          f"one at x={texts['one'][1]:.0f}")
    check("a spanning cell widens its columns rather than wrapping",
          texts["spanning both columns here"][2] > texts["one"][2])
    # a tall cell does not push the next cell in its row out of place
    doc = parse("<table><tr><td>short<td>" + "word " * 40 + "<td>last</table>")
    out = Layout(doc, Styler(doc).compute(), 300).run()
    by = {i[3].strip(): i for i in out.items if i and i[0] == "text"}
    tall = [i for i in out.items if i and i[0] == "text" and "word" in i[3]]
    top, bottom = min(i[2] for i in tall), max(i[2] for i in tall)
    check("short cells sit centred beside a tall one, each in its own place",
          top < by["short"][2] < bottom and abs(by["short"][2] - by["last"][2]) < 1)


def test_images(app) -> None:
    from PyQt6.QtGui import QColor, QImage

    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    picture = QImage(240, 160, QImage.Format.Format_RGB32)
    picture.fill(QColor("steelblue"))
    doc = parse("<p><img src=a.png> <img src=a.png width=120> "
                "<img src=gone.png width=50 height=50></p>"
                "<div style='width:100px'><img src=a.png style='max-width:100%'></div>")
    images = {"a.png": picture, "gone.png": False}
    out = Layout(doc, Styler(doc).compute(), 800, images=images).run()
    sizes = [(round(i[1].width()), round(i[1].height())) for i in out.items if i and i[0] == "image"]
    check("images: natural size, width alone keeps the shape, max-width fits the column",
          sizes == [(240, 160), (120, 80), (50, 50), (100, 67)], str(sizes))
    # as in browsers, one with a size given keeps its box, so a failure never
    # moves the page; one without a size takes no room
    check("a blocked or missing image with a size keeps its box", (50, 50) in sizes)


def _flex_check(label, got, want):
    check(label, got == want, "" if got == want else f"got {got}, want {want}")


def test_flexbox() -> None:
    """Flexbox, against the positions the specification requires."""
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout


    def boxes(markup, width=600):
        """Each element with an id, as (x, y, w, h) of its background box."""
        doc = parse("<body style='margin:0'>" + markup)
        styles = Styler(doc).compute()
        out = Layout(doc, styles, width).run()
        found = {}
        rects = []
        for item in out.items:
            if item and item[0] == "group":
                for sub in item[1]:
                    if sub[0] in ("rect", "rrect"):
                        rects.append(sub)
        # coloured boxes identify items: each test gives each item its own colour
        for sub in rects:
            r = sub[1]
            found[sub[2][:3]] = (round(r.x()), round(r.y()), round(r.width()), round(r.height()))
        return found, out




    R, G, B = (255, 0, 0), (0, 128, 0), (0, 0, 255)
    item = "<div style='background:{c};height:20px;{extra}'></div>"


    def three(style, extras=("", "", ""), width=600):
        markup = f"<div style='display:flex;{style}'>" + "".join(
            item.format(c=c, extra=e) for c, e in zip(("red", "green", "blue"), extras)) + "</div>"
        found, _out = boxes(markup, width)
        return [found.get(c) for c in (R, G, B)]


    # widths from width, then justify-content along a 600px row
    print("row, fixed widths, justify-content")
    fixed = ("width:100px", "width:100px", "width:100px")
    _flex_check("flex-start", [b[0] for b in three("", fixed)], [0, 100, 200])
    _flex_check("flex-end", [b[0] for b in three("justify-content:flex-end", fixed)], [300, 400, 500])
    _flex_check("center", [b[0] for b in three("justify-content:center", fixed)], [150, 250, 350])
    _flex_check("space-between", [b[0] for b in three("justify-content:space-between", fixed)], [0, 250, 500])
    _flex_check("space-around", [b[0] for b in three("justify-content:space-around", fixed)], [50, 250, 450])
    _flex_check("space-evenly", [b[0] for b in three("justify-content:space-evenly", fixed)], [75, 250, 425])
    _flex_check("gap between items", [b[0] for b in three("gap:10px", fixed)], [0, 110, 220])
    _flex_check("row-reverse", [b[0] for b in three("flex-direction:row-reverse", fixed)], [500, 400, 300])

    print("growing and shrinking")
    _flex_check("flex:1 shares equally", [b[2] for b in three("", ("flex:1", "flex:1", "flex:1"))], [200, 200, 200])
    _flex_check("flex-grow 1:2:1 of the spare room",
          [b[2] for b in three("", ("width:100px;flex-grow:1", "width:100px;flex-grow:2",
                                    "width:100px;flex-grow:1"))], [175, 250, 175])
    _flex_check("one grows, one fixed, one with margin-left:auto",
          [(b[0], b[2]) for b in three("", ("width:50px", "flex:1", "width:50px"))],
          [(0, 50), (50, 500), (550, 50)])
    _flex_check("margin-left:auto pushes an item to the end",
          [b[0] for b in three("", ("width:50px", "width:50px", "width:50px;margin-left:auto"))],
          [0, 50, 550])
    _flex_check("shrinking in proportion when too wide",
          [b[2] for b in three("", ("width:300px", "width:300px", "width:300px"), width=600)],
          [200, 200, 200])

    print("wrapping")
    wrapped = three("flex-wrap:wrap", ("width:250px", "width:250px", "width:250px"))
    _flex_check("the third item wraps to a second line", [(b[0], b[1]) for b in wrapped],
          [(0, 0), (250, 0), (0, 20)])

    print("cross axis")
    tall = ("height:60px;width:50px", "width:50px", "width:50px")
    stretch = three("", ("height:60px;width:50px", "width:50px;height:auto", "width:50px;height:auto"))
    _flex_check("align-items: center", [b[1] for b in three("align-items:center", tall)], [0, 20, 20])
    _flex_check("align-items: flex-end", [b[1] for b in three("align-items:flex-end", tall)], [0, 40, 40])

    # stretch: items with no height fill the line
    markup = ("<div style='display:flex'><div style='background:red;width:50px;height:60px'></div>"
              "<div style='background:green;width:50px'></div></div>")
    found, _ = boxes(markup)
    _flex_check("align-items: stretch fills the line", found.get(G), (50, 0, 50, 60))

    print("column direction")
    column = three("flex-direction:column;align-items:center", fixed)
    _flex_check("column stacks, align-items:center centres across", [(b[0], b[1]) for b in column],
          [(250, 0), (250, 20), (250, 40)])
    markup = ("<div style='display:flex;flex-direction:column;justify-content:center;"
              "min-height:200px;background:#010203'><div style='background:red;height:20px'></div></div>")
    found, _ = boxes(markup)
    _flex_check("min-height with justify-content:center centres vertically", found.get(R), (0, 90, 600, 20))
    _flex_check("and the container is its min-height tall", found.get((1, 2, 3)), (0, 0, 600, 200))

    print("text in a flex container, and order")
    found, out = boxes("<div style='display:flex;gap:20px'><span style='order:2'>second</span>"
                       "<span style='order:1'>first</span></div>")
    texts = sorted((round(i[1]), i[3]) for i in out.items if i and i[0] == "text")
    _flex_check("order puts items in its sequence", [t for _x, t in texts], ["first", "second"])



def test_floats_and_positioning() -> None:
    """Floats and positioning, against the positions CSS requires."""
    from PyQt6.QtGui import QFontMetricsF                          # noqa: F401

    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout


    def lay(markup, width=600, viewport=400):
        doc = parse("<body style='margin:0;font-size:16px'>" + markup)
        return Layout(doc, Styler(doc, viewport=(width, viewport)).compute(), width,
                      viewport_height=viewport).run()


    def boxes(out, fixed=False):
        found = {}
        source = out.fixed if fixed else out
        for item in (source.items if source else []):
            subs = item[1] if item and item[0] == "group" else [item]
            for sub in subs:
                if sub and sub[0] in ("rect", "rrect"):
                    r = sub[1]
                    found[sub[2][:3]] = (round(r.x()), round(r.y()), round(r.width()), round(r.height()))
        return found


    def texts(out):
        return [(round(i[1]), round(i[2]), i[3]) for i in out.items if i and i[0] == "text"]


    R, G, B = (255, 0, 0), (0, 128, 0), (0, 0, 255)
    words = " ".join(["word"] * 60)

    print("floats")
    out = lay(f"<div style='float:left;width:100px;height:50px;background:red'></div><p style='margin:0'>{words}</p>")
    lines = sorted({(x, y) for x, y, _t in texts(out)}, key=lambda p: p[1])
    first_x = min(x for x, y in lines if y == lines[0][1])
    late = [x for x, y in lines if y > 60]
    _flex_check("text beside a left float starts after it", first_x, 100)
    _flex_check("and returns to the edge below it", min(late) if late else None, 0)

    out = lay(f"<div style='float:right;width:100px;height:50px;background:red'></div><p style='margin:0'>{words}</p>")
    from PyQt6.QtGui import QFontMetricsF                             # noqa: E402
    ends = [i[1] + QFontMetricsF(i[4]).horizontalAdvance(i[3]) for i in out.items
            if i and i[0] == "text" and i[2] < 45]
    _flex_check("text beside a right float stops before it", max(ends) <= 500.5, True)
    _flex_check("the right float sits at the right edge", boxes(out).get(R), (500, 0, 100, 50))

    out = lay("<div style='float:left;width:100px;height:40px;background:red'></div>"
              "<div style='float:left;width:100px;height:40px;background:green'></div>")
    _flex_check("two left floats sit side by side", [boxes(out).get(R)[:2], boxes(out).get(G)[:2]],
          [(0, 0), (100, 0)])

    out = lay("<div style='float:left;width:400px;height:40px;background:red'></div>"
              "<div style='float:left;width:300px;height:40px;background:green'></div>")
    _flex_check("a float with no room beside drops below", boxes(out).get(G)[:2], (0, 40))

    out = lay("<div style='float:left;width:100px;height:80px;background:red'></div>"
              "<div style='clear:both;height:10px;background:blue'></div>")
    _flex_check("clear: both starts below the float", boxes(out).get(B)[1], 80)

    out = lay("<div style='overflow:hidden;background:#010203'>"
              "<div style='float:left;width:100px;height:90px;background:red'></div></div>")
    _flex_check("overflow: hidden grows to hold its floats", boxes(out).get((1, 2, 3))[3], 90)

    out = lay("<div style='float:left;width:150px;height:60px;background:red'></div>"
              "<div style='overflow:hidden;height:60px;background:blue'></div>")
    _flex_check("a new context sits beside a float: the sidebar layout", boxes(out).get(B)[:3], (150, 0, 450))

    print("positioning")
    out = lay("<div style='position:relative;top:10px;left:20px;width:50px;height:30px;background:red'></div>"
              "<div style='width:50px;height:30px;background:green'></div>")
    _flex_check("relative moves the box", boxes(out).get(R)[:2], (20, 10))
    _flex_check("but not what follows it", boxes(out).get(G)[:2], (0, 30))

    container = ("<div style='position:relative;width:400px;height:200px;margin-left:50px;"
                 "margin-top:20px;background:#010203'>{}</div>")
    out = lay(container.format("<div style='position:absolute;top:0;right:0;width:50px;height:30px;background:red'></div>"))
    _flex_check("absolute top/right against a positioned parent", boxes(out).get(R)[:2], (400, 20))
    out = lay(container.format("<div style='position:absolute;bottom:10px;left:10px;width:50px;height:30px;background:red'></div>"))
    _flex_check("absolute bottom/left", boxes(out).get(R)[:2], (60, 180))
    out = lay(container.format("<div style='position:absolute;left:10px;right:10px;top:5px;height:20px;background:red'></div>"))
    _flex_check("left and right stretch it between them", boxes(out).get(R)[:3], (60, 25, 380))
    out = lay(container.format("<div style='position:absolute;top:0;left:0;bottom:0;width:20px;background:red'></div>"))
    _flex_check("top and bottom stretch it too", boxes(out).get(R)[3], 200)
    out = lay(container.format("<p style='margin:0;height:40px'>x</p>"
                               "<div style='position:absolute;width:30px;height:30px;background:red'></div>"))
    _flex_check("with no sides given, it stays where it would have been", boxes(out).get(R)[:2], (50, 60))
    out = lay(container.format("<div style='position:absolute;top:0;left:0;background:red'>short</div>"))
    _flex_check("with no width, it shrinks to its content", boxes(out).get(R)[2] < 100, True)
    out = lay(container.format("<div style='position:absolute;left:50%;top:0;width:100px;height:20px;"
                               "transform:translateX(-50%);background:red'></div>"))
    _flex_check("left: 50% with translateX(-50%) centres it", boxes(out).get(R)[:2], (200, 20))
    out = lay("<div style='height:50px'></div><div style='position:absolute;top:5px;left:5px;"
              "width:20px;height:20px;background:red'></div>")
    _flex_check("with no positioned ancestor, it goes against the page", boxes(out).get(R)[:2], (5, 5))

    print("fixed")
    out = lay("<div style='height:2000px'></div><div style='position:fixed;top:0;left:0;right:0;"
              "height:40px;background:red'></div>")
    _flex_check("a fixed box is drawn against the window, not the page", boxes(out, fixed=True).get(R),
          (0, 0, 600, 40))
    # it is in the page's list, where it comes in the stacking order, but inside
    # fixed_push and fixed_pop, which pin it to the window as the page scrolls
    pinned, depth = [], 0
    for item in out.items:
        if item and item[0] == "fixed_push":
            depth += 1
        elif item and item[0] == "fixed_pop":
            depth -= 1
        elif item and item[0] == "group":
            pinned.extend((depth, sub) for sub in item[1] if sub and sub[0] == "rect")
        elif item and item[0] == "rect":
            pinned.append((depth, item))
    red = [(d, r) for d, r in pinned if r[2][:3] == (255, 0, 0)]
    check("and in the page it is pinned to the window, not scrolling with it",
          red and all(d > 0 for d, _r in red), str([(d, r[1]) for d, r in red]))



def test_grid_svg_inline_block() -> None:
    """Grid, inline-block, SVG and visibility, against CSS's rules."""
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout
    COLOURS = ["#ff0000", "#008000", "#0000ff", "#ffff00", "#ff00ff", "#00ffff", "#808080"]


    def lay(markup, width=600):
        doc = parse("<body style='margin:0;font-size:16px'>" + markup)
        return Layout(doc, Styler(doc, viewport=(width, 400)).compute(), width).run()


    def rects(out):
        found = {}
        for item in out.items:
            subs = item[1] if item and item[0] == "group" else [item]
            for sub in subs:
                if sub and sub[0] in ("rect", "rrect"):
                    r = sub[1]
                    found["#%02x%02x%02x" % sub[2][:3]] = (round(r.x()), round(r.y()),
                                                            round(r.width()), round(r.height()))
        return found


    def grid(style, n, item_style="", extras=None, width=600):
        extras = extras or [""] * n
        markup = f"<div style='display:grid;{style}'>" + "".join(
            f"<div style='background:{COLOURS[i]};height:20px;{item_style};{extras[i]}'></div>"
            for i in range(n)) + "</div>"
        found = rects(lay(markup, width))
        return [found.get(COLOURS[i]) for i in range(n)]


    print("grid tracks")
    b = grid("grid-template-columns:100px 1fr 2fr", 3)
    _flex_check("100px 1fr 2fr", [(x, w) for x, _y, w, _h in b], [(0, 100), (100, 167), (267, 333)])
    b = grid("grid-template-columns:repeat(3, 1fr);gap:10px", 3)
    _flex_check("repeat(3, 1fr) with a 10px gap", [x for x, *_ in b], [0, 203, 407])
    b = grid("grid-template-columns:repeat(auto-fill, minmax(180px, 1fr));gap:12px", 7)
    _flex_check("auto-fill minmax(180px, 1fr): three 192px columns",
          [(x, y, w) for x, y, w, _h in b[:4]], [(0, 0, 192), (204, 0, 192), (408, 0, 192), (0, 32, 192)])
    b = grid("grid-template-columns:repeat(auto-fit, minmax(150px, 1fr))", 2)
    _flex_check("auto-fit folds away empty columns: two items share the width", [w for _x, _y, w, _h in b],
          [300, 300])
    b = grid("grid-template-columns:repeat(4, 1fr)", 3, extras=["grid-column:1 / -1", "grid-column:span 2", ""])
    _flex_check("grid-column 1 / -1 spans all; span 2 takes two", [(x, y, w) for x, y, w, _h in b],
          [(0, 0, 600), (0, 20, 300), (300, 20, 150)])
    b = grid('grid-template-columns:150px 1fr;grid-template-areas:"head head" "side main"', 3,
             extras=["grid-area:head", "grid-area:side", "grid-area:main"])
    _flex_check("named areas: a header over a sidebar and main", [(x, y, w) for x, y, w, _h in b],
          [(0, 0, 600), (0, 20, 150), (150, 20, 450)])
    b = grid("grid-template-columns:1fr 1fr", 2, item_style="height:auto",
             extras=["height:60px", ""])
    _flex_check("a row is as tall as its tallest item; the other stretches", [h for *_r, h in b], [60, 60])
    b = grid("grid-template-columns:1fr 1fr;align-items:center", 2, extras=["height:60px", ""])
    _flex_check("align-items: center", [y for _x, y, _w, _h in b], [0, 20])
    b = grid("grid-template-columns:200px;justify-items:center", 1, extras=["width:100px"])
    _flex_check("justify-items: center", b[0][0], 50)
    b = grid("grid-auto-rows:50px", 2, item_style="height:auto")
    _flex_check("no columns given: one column, each item its own row; grid-auto-rows",
          [(x, y, w, h) for x, y, w, h in b], [(0, 0, 600, 50), (0, 50, 600, 50)])

    print("inline-block")
    found = rects(lay("<div><span style='display:inline-block;width:80px;height:30px;background:#ff0000'></span>"
                      "<span style='display:inline-block;width:80px;height:30px;background:#008000'></span></div>"))
    _flex_check("inline-blocks sit side by side on a line", [found.get("#ff0000")[:3], found.get("#008000")[:3]],
          [(0, 0, 80), (80, 0, 80)])
    out = lay("<p style='margin:0'>before <span style='display:inline-block;padding:4px 10px;"
              "background:#0000ff;color:white'>Button</span> after</p>")
    box = rects(out).get("#0000ff")
    words = {i[3].strip(): i for i in out.items if i and i[0] == "text"}
    _flex_check("an inline-block keeps its padding and background, and its text",
          box is not None and "Button" in words and box[2] > 50, True)
    _flex_check("its text sits on the same baseline as the words around it",
          abs(words["Button"][2] - words["before"][2]) < 0.5, True)

    print("svg")
    out = lay("<p style='color:#cc0000;margin:0'>icon <svg viewbox='0 0 24 24' width=20 height=20>"
              "<circle cx=12 cy=12 r=10 fill=currentColor /></svg></p>")
    svgs = [i for i in out.items if i and i[0] == "svg"]
    _flex_check("an inline svg is laid out at its size", [(round(i[1].width()), round(i[1].height())) for i in svgs],
          [(20, 20)])
    _flex_check("viewBox keeps its case, and currentColor takes the text colour",
          'viewBox="0 0 24 24"' in svgs[0][2] and 'fill="#cc0000"' in svgs[0][2], True)
    out = lay("<svg viewbox='0 0 100 50' style='width:200px'></svg>")
    _flex_check("an svg with only a width keeps its viewBox's shape",
          [(round(i[1].width()), round(i[1].height())) for i in out.items if i and i[0] == "svg"], [(200, 100)])

    print("visibility")
    out = lay("<p style='margin:0'><span style='visibility:hidden'>hidden words</span> shown</p>")
    drawn = {i[3].strip(): round(i[1]) for i in out.items if i and i[0] == "text"}
    _flex_check("hidden text is not drawn, but keeps its room", ("hidden" not in " ".join(drawn))
          and drawn.get("shown", 0) > 60, True)



def test_forms(app) -> None:
    """Forms, typed into and sent, against a server that echoes what it gets."""
    import json
    import urllib.request

    from PyQt6.QtTest import QTest

    from merlin.engine import MerlinView

    seen = []
    page = """<title>Form</title><form action="/echo" method="{m}">
      <label for=n>Name</label> <input id=n name=name placeholder="Your name">
      <input type=password name=secret>
      <label><input type=checkbox name=news> News</label>
      <input type=radio name=size value=s><input type=radio name=size value=l checked>
      <select name=colour><option value=r>Red</option><option value=g>Green</option></select>
      <textarea name=note></textarea>
      <button name=go value=sent>Send</button> <input type=reset>
    </form><img src="http://localhost:{port}/pixel.png">"""

    class Echo(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, body, cookie=False):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if cookie:
                self.send_header("Set-Cookie", "session=abc123; Path=/")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            path, _q, query = self.path.partition("?")
            seen.append({"method": "GET", "path": path, "query": query,
                         "cookie": self.headers.get("Cookie", "")})
            if path.startswith("/form"):
                return self._reply(page.format(m="post" if "post" in path else "get",
                                               port=self.server.server_address[1]), True)
            self._reply("<title>Echo</title>")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            seen.append({"method": "POST", "path": self.path, "body": body,
                         "origin": self.headers.get("Origin", "")})
            self._reply("<title>Posted</title>")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    view = MerlinView()
    view.resize(700, 600)
    view.show()

    def field(name, value=None):
        return next((w for e, w in view._widgets.items() if e.attrs.get("name") == name
                     and (value is None or e.attrs.get("value") == value)), None)

    try:
        view.setUrl(QUrl(base + "/form-get"))
        wait(app, 1.5)
        kinds = sorted(type(w).__name__ for w in view._widgets.values())
        check("form fields become live widgets", kinds == sorted(
            ["QLineEdit", "QLineEdit", "QCheckBox", "QRadioButton", "QRadioButton",
             "QComboBox", "QPlainTextEdit"]), str(kinds))
        check("an empty field is still a line tall", field("secret").height() >= 12,
              str(field("secret").geometry()))
        QTest.keyClicks(field("name"), "Dan Miles")
        QTest.keyClicks(field("secret"), "hunter2")
        label = next(h for r, h in view._display.links if h == "label"
                     and h.element.text().strip() == "News")
        rect = next(r for r, h in view._display.links if h is label)
        QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                         pos=QPoint(int(rect.center().x()), int(rect.center().y() - view._scroll)))
        check("clicking a label ticks its box", field("news").isChecked())
        field("size", "s").setChecked(True)
        check("radio buttons in a group untick each other", not field("size", "l").isChecked())
        field("colour").setCurrentIndex(1)
        QTest.keyClicks(field("note"), "two")
        QTest.keyClick(field("note"), Qt.Key.Key_Return)
        QTest.keyClicks(field("note"), "lines")
        QTest.keyClick(field("name"), Qt.Key.Key_Return)
        wait(app, 1.5)
        sent = [r for r in seen if r["path"] == "/echo"][-1]
        check("Enter sends the form by GET, with what was typed, ticked and chosen",
              sent["query"] == "name=Dan+Miles&secret=hunter2&news=on&size=s&colour=g"
                               "&note=two%0D%0Alines&go=sent", sent["query"])
        check("the page's cookie comes back with it", sent["cookie"] == "session=abc123",
              sent["cookie"])
        pixel = [r for r in seen if r["path"] == "/pixel.png"]
        check("another site's image gets no cookie", pixel and pixel[0]["cookie"] == "", str(pixel))
        view.setUrl(QUrl(base + "/form-post"))
        wait(app, 1.5)
        QTest.keyClicks(field("name"), "Posted")
        rect = next(r for r, e, f in view._buttons if e.tag == "button")
        QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                         pos=QPoint(int(rect.center().x()), int(rect.center().y() - view._scroll)))
        wait(app, 1.5)
        posted = [r for r in seen if r["method"] == "POST"]
        check("clicking the button sends it by POST, with an Origin",
              posted and "name=Posted" in posted[-1]["body"] and posted[-1]["origin"] == base,
              str(posted[-1:]))
        view.setUrl(QUrl(base + "/form-get"))
        wait(app, 1.5)
        QTest.keyClicks(field("name"), "gone")
        view._press_button(next(e for r, e, f in view._buttons if e.attrs.get("type") == "reset"))
        check("Reset puts the page's own values back", field("name").text() == "")
        QTest.keyClicks(field("name"), "kept")
        view.resize(520, 600)
        wait(app, 0.5)
        check("what was typed survives the page being laid out again", field("name").text() == "kept")
        view.setUrl(QUrl("merlin://listen"))
        check("merlin:// addresses are not loaded as pages", view.url().scheme() != "merlin")
    finally:
        server.shutdown()
        view.close()


def test_real_world_css() -> None:
    """@media, CSS variables, the at-rules and modern selectors."""
    from merlin.engine.css import Styler, media_matches
    from merlin.engine.html import parse

    def check(label, got, want):                               # noqa: F811
        _flex_check(label, got, want)
    print("media queries, for a 1000 x 700 window")
    vp = (1000, 700)
    cases = [
        ("(min-width: 768px)", True), ("(max-width: 767px)", False), ("screen and (min-width: 1200px)", False),
        ("print", False), ("screen", True), ("not print", True), ("only screen and (max-width: 1000px)", True),
        ("(min-width: 40em)", True), ("(min-width: 80em)", False), ("(width >= 600px)", True),
        ("(400px <= width < 900px)", False), ("(orientation: landscape)", True),
        ("(prefers-color-scheme: dark)", False), ("(prefers-color-scheme: light)", True),
        ("(max-width: 500px), (min-width: 900px)", True), ("(prefers-reduced-motion: reduce)", False),
        ("(hover: hover) and (pointer: fine)", True), ("(some-unknown-feature)", False),
    ]
    got = [media_matches(q, vp) for q, _want in cases]
    check("18 media queries", [q for (q, want), g in zip(cases, got) if g != want], [])


    def styles(markup, viewport=(1000, 700)):
        doc = parse(markup)
        styler = Styler(doc, viewport=viewport)
        computed = styler.compute()
        by_id = {e.id: computed[e] for e in doc.root.elements() if e.id}
        return by_id, styler, doc


    print("@media in a stylesheet, and resizing across a breakpoint")
    css = """<style>
     #a { color: red }
     @media (max-width: 600px) { #a { color: blue } }
     @media screen and (min-width: 601px) { #a { font-size: 20px } }
     @media print { #a { color: green } }
     @supports (display: grid) { #b { color: purple } }
     @supports not (display: grid) { #b { color: orange } }
     @charset "utf-8"; @import url(x.css); @layer base, theme;
     @layer base { #c { color: teal } }
     #d { color: navy }
    </style><p id=a>a</p><p id=b>b</p><p id=c>c</p><p id=d>d</p>"""
    by_id, styler, doc = styles(css)
    check("wide: the wide rule, not the narrow or print ones",
          (by_id["a"]["color"], by_id["a"]["font-size"]), ((255, 0, 0, 255), 20.0))
    check("@supports read, @supports not skipped", by_id["b"]["color"], (128, 0, 128, 255))
    check("@layer read", by_id["c"]["color"], (0, 128, 128, 255))
    check("a rule after @charset, @import and @layer statements is kept", by_id["d"]["color"],
          (0, 0, 128, 255))
    check("a narrower window would choose other rules", styler.media_changed((500, 700)), True)
    check("a slightly different wide one would not", styler.media_changed((990, 700)), False)
    narrow = styler.restyle((500, 700))
    a = next(e for e in doc.root.elements() if e.id == "a")
    check("restyled narrow: the narrow rule now", (narrow[a]["color"], narrow[a]["font-size"]),
          ((0, 0, 255, 255), 16.0))
    by_id, _s, _d = styles("<style media='(max-width: 600px)'>#a{color:blue}</style><p id=a>a</p>")
    check("<style media=...> holds only when its media does", by_id["a"]["color"], (0, 0, 0, 255))

    print("CSS variables")
    by_id, _s, _d = styles("""<style>
     :root { --brand: #3a5bd9; --gap: 4px 8px; --size: 18px; --alias: var(--brand) }
     #v1 { color: var(--brand) }
     #v2 { color: var(--missing, rgb(0, 128, 0)) }
     #v3 { color: var(--alias) }
     #v4 { padding: var(--gap) }
     #v5 { font-size: var(--size) }
     .dark { --brand: #ffffff }
     #v7 { color: var(--nothing) }
     #v8 { --x: var(--y); --y: var(--x); color: var(--x, red) }
    </style><div style="color: rgb(1,2,3)">
    <p id=v1>1</p><p id=v2>2</p><p id=v3>3</p><p id=v4>4</p><p id=v5>5</p>
    <div class=dark><p id=v6 style="color: var(--brand)">6</p></div><p id=v7>7</p><p id=v8>8</p></div>""")
    check("var(--brand)", by_id["v1"]["color"], (58, 91, 217, 255))
    check("a missing variable uses its fallback", by_id["v2"]["color"], (0, 128, 0, 255))
    check("a variable defined through another", by_id["v3"]["color"], (58, 91, 217, 255))
    check("a variable holding two values, in a shorthand",
          (by_id["v4"]["padding-top"], by_id["v4"]["padding-right"]), (4.0, 8.0))
    check("a length from a variable", by_id["v5"]["font-size"], 18.0)
    check("redefined lower down, it changes for that part of the page", by_id["v6"]["color"],
          (255, 255, 255, 255))
    check("missing with no fallback: inherited as if not set", by_id["v7"]["color"], (1, 2, 3, 255))
    check("two variables defined by each other: the fallback, no hang", by_id["v8"]["color"],
          (255, 0, 0, 255))
    doc = parse("""<style>
     li:nth-child(odd) { color: red }  li:nth-child(3n + 2) { font-weight: bold }
     li:nth-last-child(1) { font-style: italic }
     p:not(.skip) { color: blue }  p:not(:hover) { font-weight: bold }
     :is(h1, h2).t { color: green }  :where(.w) { color: orange }  .w { color: purple }
     span:first-of-type { color: teal }  span:last-of-type { font-style: italic }
     input:disabled { color: gray }  input:enabled { color: navy }  input:checked { font-weight: bold }
     ul > li:not(:first-child):not(:last-child) { text-decoration: underline }
     p::before { color: pink }
    </style>
    <ul><li id=l1>1<li id=l2>2<li id=l3>3<li id=l4>4<li id=l5>5</ul>
    <p id=p1>a</p><p id=p2 class=skip>b</p><h2 id=h class=t>h</h2><div id=w class=w>w</div>
    <div><b>x</b><span id=s1>s</span><i>y</i><span id=s2>t</span></div>
    <input id=i1 disabled><input id=i2 type=checkbox checked>""")
    st = Styler(doc).compute()
    e = {x.id: st[x] for x in doc.root.elements() if x.id}
    black, red = (0, 0, 0, 255), (255, 0, 0, 255)
    check(":nth-child(odd)", [e[f"l{i}"]["color"] for i in range(1, 6)], [red, black, red, black, red])
    check(":nth-child(3n + 2), spaces inside the brackets", [e[f"l{i}"]["font-weight"] for i in range(1, 6)], [400, 700, 400, 400, 700])
    check(":nth-last-child(1)", [e[f"l{i}"]["font-style"] for i in range(1, 6)], ["normal"] * 4 + ["italic"])
    check(":not(.skip)", (e["p1"]["color"], e["p2"]["color"]), ((0, 0, 255, 255), black))
    check(":not(:hover) holds at rest", e["p1"]["font-weight"], 700)
    check(":is(h1, h2).t", e["h"]["color"], (0, 128, 0, 255))
    check(":where() adds no specificity: .w after it wins", e["w"]["color"], (128, 0, 128, 255))
    check(":first-of-type and :last-of-type", (e["s1"]["color"], e["s2"]["font-style"]), ((0, 128, 128, 255), "italic"))
    check(":disabled, :enabled, :checked", (e["i1"]["color"], e["i2"]["color"], e["i2"]["font-weight"]),
          ((128, 128, 128, 255), (0, 0, 128, 255), 700))
    check("chained :not()s", [e[f"l{i}"]["text-decoration"] for i in range(1, 6)], ["none", "underline", "underline", "underline", "none"])
    check("::before is not applied to the element itself", e["p1"]["color"], (0, 0, 255, 255))


def test_files_ftp_smb() -> None:
    """Sending files; FTP and SMB folders and files, byte for byte."""
    import email
    import email.policy
    import shutil
    import subprocess

    from merlin.engine import forms, remote
    from merlin.engine.html import parse

    work = tempfile.mkdtemp(prefix="merlin-remote-")
    payload = bytes(range(256)) * 64 + b"\r\n\x00 tail"
    source = os.path.join(work, "report.pdf")
    open(source, "wb").write(payload)
    doc = parse("<form method=post enctype=multipart/form-data><input name=t value=x>"
                "<input type=file name=doc><input type=file name=none></form>")
    form = doc.root.find("form")
    fields = {e.attrs["name"]: e for e in doc.root.elements() if e.attrs.get("name")}
    sent = forms.submission(form, forms.form_data(form, {fields["doc"]: [source]}), "https://x/")
    message = email.message_from_bytes(f"Content-Type: {sent['type']}\r\n\r\n".encode()
                                       + sent["body"], policy=email.policy.HTTP)
    parts = list(message.iter_parts())
    check("a file is sent byte for byte, with its name and type",
          parts[1].get_payload(decode=True) == payload and parts[1].get_filename() == "report.pdf"
          and parts[1].get_content_type() == "application/pdf")
    check("an empty file field sends an empty part", parts[2].get_filename() == "")
    check("a Windows network path from an smb:// address",
          remote.unc_path("smb://nas/Media/a%20b.mkv") == "\\\\nas\\Media\\a b.mkv",
          remote.unc_path("smb://nas/Media/a%20b.mkv"))
    downloads = os.path.join(work, "Downloads")
    # FTP, when a server can be started here
    try:
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer
    except ImportError:
        print("  skip  FTP: pyftpdlib is not installed")
    else:
        import logging

        logging.getLogger("pyftpdlib").setLevel(logging.CRITICAL)
        root = os.path.join(work, "ftp")
        os.makedirs(os.path.join(root, "docs"))
        open(os.path.join(root, "archive.bin"), "wb").write(payload)
        open(os.path.join(root, "readme.txt"), "w").write("Hello from FTP")
        users = DummyAuthorizer()
        users.add_anonymous(root)
        users.add_user("dan", "pw", root, perm="elr")
        handler = type("H", (FTPHandler,), {"authorizer": users})
        server = FTPServer(("127.0.0.1", 0), handler)
        port = server.address[1]
        threading.Thread(target=server.serve_forever, kwargs={"handle_exit": False},
                         daemon=True).start()
        try:
            ok, final, page, _saved = remote.open_remote(f"ftp://127.0.0.1:{port}/docs", downloads)
            check("an FTP folder is an index page, its address ending in /",
                  ok and final.endswith("/docs/") and 'href="../"' in page, final)
            ok, _f, page, _saved = remote.open_remote(f"ftp://127.0.0.1:{port}/readme.txt", downloads)
            check("an FTP text file is shown", ok and "Hello from FTP" in page)
            ok, _f, _p, saved = remote.open_remote(f"ftp://127.0.0.1:{port}/archive.bin", downloads)
            check("an FTP file downloads byte for byte (binary mode)",
                  ok and open(saved, "rb").read() == payload)
            ok, _f, page, _saved = remote.open_remote(f"ftp://dan:wrong@127.0.0.1:{port}/", downloads)
            check("a wrong FTP password is reported", not ok and "530" in page, page[:60])
        finally:
            server.close_all()
    # SMB, when Samba and smbclient are installed here
    if not (shutil.which("smbd") and shutil.which("smbclient")):
        print("  skip  SMB: Samba (smbd) and smbclient are not installed")
        return
    share = os.path.join(work, "share")
    os.makedirs(os.path.join(share, "Photos"))
    open(os.path.join(share, "Photos", "a photo.bin"), "wb").write(payload)
    state = os.path.join(work, "smbstate")
    os.makedirs(os.path.join(state, "ncalrpc"))
    os.chmod(work, 0o755)
    for folder, _dirs, names in os.walk(share):
        os.chmod(folder, 0o755)
        for name in names:
            os.chmod(os.path.join(folder, name), 0o644)
    config = os.path.join(work, "smb.conf")
    open(config, "w").write(f"""[global]
  smb ports = 4455
  interfaces = lo
  bind interfaces only = yes
  map to guest = Bad User
  disable netbios = yes
  server min protocol = SMB2
  private dir = {state}
  lock directory = {state}
  state directory = {state}
  cache directory = {state}
  pid directory = {state}
  ncalrpc dir = {state}/ncalrpc
  log file = {state}/log
[public]
  path = {share}
  guest ok = yes
  read only = yes
""")
    os.makedirs("/run/samba/ncalrpc", exist_ok=True)
    # in a session of its own: smbd signals its whole process group as it
    # stops, which, shared, would stop these tests with it
    subprocess.run(["smbd", "-s", config, "-D"], capture_output=True, start_new_session=True)
    time.sleep(3)
    try:
        ok, _f, page, _saved = remote.open_remote("smb://127.0.0.1:4455/", downloads)
        check("an SMB server's shares are listed", ok and 'href="public/"' in page, page[-120:])
        ok, final, page, _saved = remote.open_remote("smb://127.0.0.1:4455/public/Photos", downloads)
        check("an SMB folder is an index page", ok and final.endswith("/Photos/"), page[-120:])
        ok, _f, _p, saved = remote.open_remote(
            "smb://127.0.0.1:4455/public/Photos/a%20photo.bin", downloads)
        check("an SMB file downloads byte for byte", ok and open(saved, "rb").read() == payload)
    finally:
        pid = os.path.join(state, "smbd.pid")
        if os.path.exists(pid):
            os.kill(int(open(pid).read().strip()), 15)


def test_no_freeze_on_real_grids() -> None:
    """Grid items past the declared columns: 1.7.1 froze Merlin on these.

    Run in a separate process with a time limit, so a hang fails the check
    rather than stalling every test after it.
    """
    import subprocess

    script = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "from merlin.engine.html import parse\n"
        "from merlin.engine.css import Styler\n"
        "from merlin.engine.layout import Layout\n"
        "for markup in (\n"
        "  \"<div style='display:grid;grid-template-columns:100px 100px'>"
        "<div style='grid-column:4;height:10px;background:#ff0000'></div><div>b</div></div>\",\n"
        "  \"<div style='display:grid;grid-template-columns:1fr'><div style='grid-column:2 / span 3'>a</div></div>\",\n"
        "  \"<div style='display:grid'><div style='grid-column:1 / -1'>a</div><div style='grid-column:3'>b</div></div>\"):\n"
        "    doc = parse(\"<body style='margin:0'>\" + markup)\n"
        "    out = Layout(doc, Styler(doc).compute(), 800).run()\n"
        "print('finished')\n")
    try:
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              timeout=30)
        finished = "finished" in done.stdout
        detail = (done.stderr or done.stdout)[-160:]
    except subprocess.TimeoutExpired:
        finished, detail = False, "still running after 30 seconds: it hangs"
    check("a grid item past the declared columns does not freeze layout", finished, detail)
    if not finished:
        return          # laying it out here would hang these tests too
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    doc = parse("<body style='margin:0'><div style='display:grid;grid-template-columns:100px 100px'>"
                "<div style='grid-column:4;height:10px;background:#ff0000'></div>"
                "<div style='height:10px;background:#00ff00'></div></div>")
    out = Layout(doc, Styler(doc).compute(), 800).run()
    rects = {sub[2][:3]: sub[1] for item in out.items if item and item[0] == "group"
             for sub in item[1] if sub[0] == "rect"}
    placed, other = rects.get((255, 0, 0)), rects.get((0, 255, 0))
    check("and the grid gains columns for it, as CSS says",
          placed is not None and round(placed.x()) == 500 and round(other.x()) == 0,
          f"{placed} {other}")


def test_deep_nesting_stays_quick() -> None:
    """Flex nested many deep: 1.7.2 took minutes, measuring the same items over
    and over, and froze on real sites. In a separate process, under a limit."""
    import subprocess

    script = (
        "import os, sys, time\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "from merlin.engine.html import parse\n"
        "from merlin.engine.css import Styler\n"
        "from merlin.engine.layout import Layout\n"
        "inner = '<span>label</span><span>more text here</span>'\n"
        "for _ in range(12):\n"
        "    inner = f\"<div style='display:flex;gap:4px'><div>{inner}</div><div>side</div></div>\"\n"
        "doc = parse(inner)\n"
        "styles = Styler(doc).compute()\n"
        "start = time.perf_counter()\n"
        "out = Layout(doc, styles, 1000).run()\n"
        "print(f'{time.perf_counter() - start:.2f} simplified={out.simplified}')\n")
    try:
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              timeout=30)
        took = done.stdout.strip()
    except subprocess.TimeoutExpired:
        took = ""
    seconds = float(took.split()[0]) if took else None
    check("flex nested 12 deep lays out in well under a second",
          seconds is not None and seconds < 1.0, took or "still running after 30 seconds")
    check("without needing the time budget's estimates", took.endswith("simplified=False"), took)


def test_gradients() -> None:
    """Gradients drawn by the CSS geometry, measured in pixels."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter

    from merlin.engine.css import parse_gradients
    from merlin.engine.paint import fill_gradient

    def sample(css, points, size=(200, 100)):
        img = QImage(*size, QImage.Format.Format_ARGB32)
        img.fill(QColor("magenta"))
        painter = QPainter(img)
        fill_gradient(painter, QRectF(0, 0, *size), parse_gradients(css)[0])
        painter.end()
        return [img.pixelColor(x, y).red() for x, y in points]

    red = sample("linear-gradient(90deg, #000, #fff)", [(0, 50), (100, 50), (199, 50)])
    check("linear-gradient at 90deg runs left to right", red[0] < 5 and 120 < red[1] < 136 and red[2] > 250, str(red))
    red = sample("linear-gradient(#000, #fff)", [(100, 0), (100, 99)])
    check("with no angle, top to bottom", red[0] < 5 and red[1] > 250, str(red))
    red = sample("linear-gradient(90deg, #000 0%, #fff 45%, #000 100%)", [(90, 50)])
    check("a stop at 45% is its colour at 45%", red[0] > 250, str(red))
    red = sample("radial-gradient(circle, #fff, #000)", [(100, 50), (0, 0)])
    check("radial-gradient: its colour at the centre, the last at the far corner",
          red[0] > 250 and red[1] < 5, str(red))
    layers = parse_gradients("radial-gradient(circle at 30% 20%, #33245c 0%, #14121f 68%)")
    check("the new-tab page's radial background is read whole",
          layers and layers[0][1] == "circle" and layers[0][3] == (("%", 30.0), ("%", 20.0)), str(layers))


def test_clipping_opacity_details_svg(app) -> None:
    """overflow clipping, opacity, <details> and sharp SVG images, in pixels."""
    import base64

    from PyQt6.QtTest import QTest

    from merlin.engine import MerlinView

    svg = base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16'>"
                           b"<rect x='0' y='0' width='8' height='16' fill='#ff0000'/></svg>").decode()
    page = f"""<body style='margin:0;background:#ffffff'>
    <h2 style='position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);
     top:0;left:0;margin:0;font-size:40px;color:#000000'><a href='/hidden'>Navigation Menu</a></h2>
    <div style='position:absolute;top:0;left:300px;opacity:0;background:#00ff00;width:100px;height:40px'></div>
    <div style='position:absolute;top:0;left:450px;opacity:0.5;background:#0000ff;width:100px;height:40px'></div>
    <div style='position:absolute;top:60px;left:0;width:100px;height:40px;overflow:hidden;border-radius:12px'>
     <div style='width:400px;height:200px;background:#ff00ff'></div></div>
    <details style='position:absolute;top:120px;left:0;width:200px'>
     <summary style='height:24px;background:#dddddd'>More</summary>
     <div style='height:40px;background:#00ffff'></div></details>
    <img src='data:image/svg+xml;base64,{svg}' style='position:absolute;top:200px;left:0;width:128px;height:128px'>
    </body>"""
    view = MerlinView()
    view.resize(700, 400)
    view.show()
    try:
        view.setHtml(page, QUrl("about:blank"))
        wait(app, 0.4)
        shot = view.grab().toImage()

        def at(x, y):
            return shot.pixelColor(x, y).name()

        check("a screen-reader-only heading, clipped to 1px, is not drawn",
              all(at(x, y) == "#ffffff" for x in (10, 60, 120) for y in (15, 25)))
        check("and its link cannot be clicked",
              not any(h == "/hidden" and r.width() > 2 for r, h in view._display.links))
        check("opacity: 0 draws nothing", at(350, 20) == "#ffffff", at(350, 20))
        blue = shot.pixelColor(500, 20)
        check("opacity: 0.5 draws half-way", abs(blue.red() - 127) < 4 and blue.blue() == 255, blue.name())
        check("overflow: hidden cuts at the box's edge",
              at(95, 80) == "#ff00ff" and at(105, 80) == "#ffffff", f"{at(95, 80)} {at(105, 80)}")
        check("and at its rounded corner", at(1, 61) == "#ffffff", at(1, 61))
        check("a closed <details> hides all but its summary", at(20, 155) == "#ffffff", at(20, 155))
        rect = next(r for r, h in view._display.links if h == "summary")
        QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                         pos=QPoint(int(rect.center().x()), int(rect.center().y())))
        wait(app, 0.3)
        shot = view.grab().toImage()
        check("clicking its summary opens it", at(20, 155) == "#00ffff", at(20, 155))
        edge = [at(x, 260) for x in (62, 63, 64, 65)]
        check("an SVG image drawn 8x its size keeps a clean edge",
              edge[0] == "#ff0000" and edge[-1] == "#ffffff"
              and sum(e not in ("#ff0000", "#ffffff") for e in edge) <= 1, str(edge))
    finally:
        view.close()


def test_browser_headers_and_compression(app) -> None:
    """Sites refusing what does not look like a browser; compressed responses.

    MerlinEngine/0.1 as its user agent got 403 Forbidden from sites' bot
    protection. Pages, stylesheets and downloads are also often compressed.
    """
    import gzip
    import json
    import urllib.request

    from PyQt6.QtCore import QStandardPaths

    from merlin.engine import MerlinView

    seen = []

    class Picky(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, kind):
            data = gzip.compress(body if isinstance(body, bytes) else body.encode())
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            agent = self.headers.get("User-Agent", "")
            seen.append((self.path, self.headers.get("Sec-Fetch-Dest"), agent))
            if self.path == "/seen":
                body = json.dumps(seen).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
                return
            if not ("Mozilla/5.0" in agent and "MerlinEngine" not in agent
                    and self.headers.get("Accept-Language") and self.headers.get("Sec-Fetch-Mode")):
                self.send_response(403)
                self.end_headers()
                return
            if self.path == "/style.css":
                return self._send("h1 { color: rgb(200, 0, 0) }", "text/css")
            if self.path == "/data.bin":
                return self._send(bytes(range(256)) * 40, "application/octet-stream")
            self._send("<title>Picky</title><link rel=stylesheet href=/style.css><h1>In</h1>",
                       "text/html; charset=utf-8")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Picky)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    view = MerlinView()
    view.resize(600, 400)
    try:
        try:
            urllib.request.urlopen(base + "/")
            refused = False
        except urllib.error.HTTPError as error:
            refused = error.code == 403
        check("the test server refuses what does not look like a browser", refused)
        view.setUrl(QUrl(base + "/"))
        wait(app, 1.5)
        h1 = next((e for e in view._document.root.elements() if e.tag == "h1"), None)
        check("MerlinEngine is let in", view.title() == "Picky", view.title())
        check("and its compressed page and stylesheet are read",
              h1 is not None and view._styles[h1]["color"] == (200, 0, 0, 255))
        marks = {path: dest for path, dest, _agent in seen}
        check("the page is asked for as a document, its stylesheet as a style",
              marks.get("/") == "document" and marks.get("/style.css") == "style", str(marks))
        view.setUrl(QUrl(base + "/data.bin"))
        wait(app, 1.5)
        saved = os.path.join(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation), "data.bin")
        check("a compressed download is saved decompressed, byte for byte",
              os.path.exists(saved) and open(saved, "rb").read() == bytes(range(256)) * 40)
        if os.path.exists(saved):
            os.remove(saved)
    finally:
        server.shutdown()
        view.close()


def test_worker_processes() -> None:
    """Pages styled in worker processes, alongside Merlin rather than taking
    turns with it: the same styles as styling here, and a fallback."""
    from merlin.engine import worker
    from merlin.engine.css import Styler
    from merlin.engine.html import parse

    markup = ("<style>@media (max-width: 600px) { p { color: red } } :root { --c: #123456 } "
              "h1 { color: var(--c) } .a:not(.b) { font-weight: bold }</style>"
              "<h1>t</h1><p class=a>one</p><details><summary>s</summary><p>in</p></details>")
    job = {"markup": markup, "url": "", "sheets": None, "viewport": (1000, 700), "want": "document"}
    pool = worker.Pool(size=1)
    try:
        answer = pool.style(job)
        check("a worker process answers", answer is not None and pool.workers
              and pool.workers[0].alive())
        here = parse(markup)
        local = Styler(here, viewport=(1000, 700)).compute()
        theirs = [answer["styles"][e] for e in [answer["document"].root] + list(answer["document"].root.elements())]
        ours = [local[e] for e in [here.root] + list(here.root.elements())]
        check("and styles the page exactly as styling here would", theirs == ours)
        narrow = pool.style(dict(job, viewport=(500, 700), want="styles"))
        elements = [answer["document"].root] + list(answer["document"].root.elements())
        p_index = next(i for i, e in enumerate(elements) if e.tag == "p")
        check("restyling for a narrow window chooses the narrow @media rules",
              narrow["styles"][p_index]["color"] == (255, 0, 0, 255))
        pool.workers[0].process.kill()
        check("a worker that dies is replaced", pool.style(job) is not None)
    finally:
        pool.stop()
    broken = worker.Pool(size=1)
    real = worker.worker_command
    worker.worker_command = lambda: ["/nonexistent/merlin", "--engine-worker"]
    try:
        check("when no worker can start, the caller is told to style it itself",
              broken.style(job) is None and broken.failed)
    finally:
        worker.worker_command = real


def test_sticky_and_icons(app) -> None:
    """position: sticky, by pixels as the page scrolls; and tab icons."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QColor, QImage

    from merlin.engine import MerlinView

    page = """<body style='margin:0;background:#ffffff'>
    <div style='height:100px;background:#eeeeee'></div>
    <section style='height:1200px;background:#ffffff'>
     <div style='position:sticky;top:0;height:40px;background:#ff0000'><a href='/stuck'>link</a></div>
     <div style='height:800px'></div></section>
    <div style='height:1500px;background:#0000ff'></div></body>"""
    view = MerlinView()
    view.resize(600, 400)
    view.show()
    try:
        view.setHtml(page, QUrl("http://example.test/"))
        wait(app, 0.3)

        def at(y):
            return view.grab().toImage().pixelColor(300, y).name()

        check("a sticky bar sits in its place at first", at(120) == "#ff0000" and at(20) == "#eeeeee")
        view.scrollbar.setValue(500)
        wait(app, 0.1)
        check("scrolled into its section, it holds at the top", at(20) == "#ff0000", at(20))
        link = next(r for r, h in view._display.links if h == "/stuck")
        # the link's own middle, as it is drawn now: 100px from its place, held at the top
        check("and its link is clicked where it is drawn",
              view._link_at(QPointF(link.center().x(), link.center().y() - 100)) == "/stuck")
        view.scrollbar.setValue(1500)
        wait(app, 0.1)
        check("past its section's end, it leaves with it", at(20) == "#0000ff", at(20))
    finally:
        view.close()
    folder = tempfile.mkdtemp(prefix="merlin-icons-")
    picture = QImage(32, 32, QImage.Format.Format_ARGB32)
    picture.fill(QColor("#ff8800"))
    picture.save(os.path.join(folder, "named.png"))
    picture.fill(QColor("#00aa44"))
    picture.save(os.path.join(folder, "favicon.ico"), "ICO")
    open(os.path.join(folder, "named.html"), "w").write(
        "<title>Named</title><link rel='shortcut icon' href='/named.png'><p>x")
    open(os.path.join(folder, "plain.html"), "w").write("<title>Plain</title><p>x")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for name, colour in (("named.html", "#ff8800"), ("plain.html", "#00aa44")):
            view = MerlinView()
            got = []
            view.iconChanged.connect(got.append)
            view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/{name}"))
            wait(app, 2.0)
            shade = got[0].pixmap(16, 16).toImage().pixelColor(8, 8).name() if got else None
            check(f"a tab icon: {'the page names one' if 'named' in name else 'the site has /favicon.ico'}",
                  shade == colour, str(shade))
            view.close()
    finally:
        server.shutdown()


def test_all_and_script_pages() -> None:
    """all: unset / revert / initial, and telling pages that need JavaScript."""
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.view import needs_javascript

    doc = parse("<style>.u { all: unset } .r { background: red; all: revert } .i { color: red }"
                " .i2 { all: initial }</style><button id=plain>a</button><button id=u class=u>b</button>"
                "<button id=r class=r>c</button><div class=i><p id=i2 class=i2>d</p></div>")
    styles = Styler(doc).compute()
    e = {x.id: styles[x] for x in doc.root.elements() if x.id}
    check("all: unset strips a button's default look",
          e["u"].get("background-color") in (None, (0, 0, 0, 0)) and not e["u"].get("border-top-width"))
    check("all: revert goes back to it",
          e["r"]["background-color"] == e["plain"]["background-color"], str(e["r"]["background-color"]))
    check("all: initial takes initial values, not inherited ones", e["i2"]["color"] == (0, 0, 0, 255))
    shell = "<body><div id=app></div>" + "<script>var x = '" + "1" * 30000 + "';</script>" * 3 + "</body>"
    article = "<body><article><p>" + "words " * 400 + "</p></article>" + "<script>a()</script>" * 6
    asks = "<body><noscript>You need to enable JavaScript to run this app.</noscript><div id=root></div>"
    login = "<body><form><input type=password><button>Log in</button></form></body>"
    check("a page built by scripts, like YouTube's, needs JavaScript", needs_javascript(parse(shell)))
    check("one that asks for it in <noscript> does too", needs_javascript(parse(asks)))
    check("an article with scripts does not", not needs_javascript(parse(article)))
    check("nor a login page without scripts", not needs_javascript(parse(login)))


def test_images_do_not_relayout(app) -> None:
    """Images arriving: laid out again only when they change the layout.

    Each arrival had laid the whole page out again, a dozen times and more on
    a big page, holding up the window while images trickled in.
    """
    import base64

    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt6.QtGui import QColor, QImage

    from merlin.engine import MerlinView

    picture = QImage(20, 20, QImage.Format.Format_RGB32)
    picture.fill(QColor("orange"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    picture.save(buffer, "PNG")
    png = bytes(data)

    class Slow(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith(".png"):
                time.sleep(0.05 * int(self.path.split("img")[1].split(".")[0]))
                body, kind = png, "image/png"
            else:
                body = ("<p>text" + "".join(f"<img src='/img{i}.png' width=20 height=20>"
                                            for i in range(20)) + "<img src='/img3.png'>").encode()
                kind = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    view = MerlinView()
    view.resize(600, 400)
    view.show()
    layouts = []
    real = view._layout
    view._layout = lambda: (layouts.append(1), real())[1]
    view._relayout.timeout.disconnect()
    view._relayout.timeout.connect(view._layout)
    view._image_relayout.timeout.disconnect()
    view._image_relayout.timeout.connect(view._layout)
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/"))
        wait(app, 2.5)
        shown = sum(1 for v in view._images.values() if v is not False)
        check("all the images arrive", shown == 20, str(shown))
        check("twenty sized images arriving lay the page out only a few times",
              len(layouts) <= 4, f"{len(layouts)} layouts")
        del base64
    finally:
        server.shutdown()
        view.close()


def _test_certificates(folder: str, start_in_hours: float = 2.0, name: str = "localhost"):
    """A test authority, and a certificate from it starting later: as every
    certificate looks on a computer whose clock is behind. None without the
    cryptography library, which only these tests need."""
    try:
        import datetime
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Merlin Test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=60))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          # key identifiers and usage, as every real authority's certificate has
          # them: Python 3.13 checks strictly and refuses a chain without them
          .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [x509.DNSName(name)] + ([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                                    if name == "localhost" else [])
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
            .not_valid_before(now + datetime.timedelta(hours=start_in_hours))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    paths = {k: os.path.join(folder, f"{k}.pem") for k in ("ca", "cert", "key")}
    open(paths["ca"], "wb").write(ca.public_bytes(serialization.Encoding.PEM))
    open(paths["cert"], "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(paths["key"], "wb").write(key.private_bytes(serialization.Encoding.PEM,
                                                     serialization.PrivateFormat.TraditionalOpenSSL,
                                                     serialization.NoEncryption()))
    return paths


def test_certificate_leniency() -> None:
    """An allowed site's certificate: only the dates set aside, all else checked."""
    import ssl
    import urllib.request

    from merlin.clock import describe_skew, is_date_problem
    from merlin.engine.view import certificate_failure, lenient_context

    check("a certificate failure is read from the message",
          certificate_failure("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify "
                              "failed: certificate is not yet valid (_ssl.c:1029)>")
          == "certificate is not yet valid")
    check("and a date problem told apart", is_date_problem("certificate is not yet valid")
          and not is_date_problem("unable to get local issuer certificate"))
    check("a clock behind is described", "1 hour 2 minutes behind" in describe_skew(3720))
    folder = tempfile.mkdtemp(prefix="merlin-tls-")
    good = _test_certificates(folder)
    if good is None:
        print("  skip  certificates: the cryptography library is not installed")
        return
    other = _test_certificates(tempfile.mkdtemp(prefix="merlin-tls-"), name="elsewhere.test")

    def serve(paths):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(paths["cert"], paths["key"])
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def opens(server, context, ca):
        context.load_verify_locations(ca)
        try:
            urllib.request.urlopen(f"https://localhost:{server.server_address[1]}/",
                                   context=context, timeout=5).read(1)
            return "opened"
        except Exception as exc:                            # noqa: BLE001
            return str(getattr(exc, "reason", exc))

    right, wrong = serve(good), serve(other)
    try:
        check("normally, a certificate not valid yet is refused",
              "not yet valid" in opens(right, ssl.create_default_context(), good["ca"]))
        dates = lenient_context("dates")
        fresh = ssl.create_default_context()
        fresh.verify_flags = dates.verify_flags
        check("allowed for dates, it opens", opens(right, fresh, good["ca"]) == "opened")
        fresh = ssl.create_default_context()
        fresh.verify_flags = dates.verify_flags
        check("but a certificate for another name is still refused",
              "match" in opens(wrong, fresh, other["ca"]).lower()
              or "mismatch" in opens(wrong, fresh, other["ca"]).lower())
        fresh = ssl.create_default_context()
        fresh.verify_flags = dates.verify_flags
        refusals = []
        for trust in (other["ca"], None):          # another authority; the system's list only
            fresh = ssl.create_default_context()
            fresh.verify_flags = dates.verify_flags
            if trust:
                refusals.append(opens(right, fresh, trust))
            else:
                try:
                    urllib.request.urlopen(f"https://localhost:{right.server_address[1]}/",
                                           context=fresh, timeout=5).read(1)
                    refusals.append("opened")
                except Exception as exc:                    # noqa: BLE001
                    refusals.append(str(getattr(exc, "reason", exc)))
        # what matters is the refusal, whatever OpenSSL calls it
        check("and one from an authority not trusted is still refused",
              all(r != "opened" and "CERTIFICATE_VERIFY_FAILED" in r for r in refusals), str(refusals))
    finally:
        right.shutdown()
        wrong.shutdown()


def test_stacking_order(app) -> None:
    """Positioned boxes painted in CSS's stacking order, by z-index."""
    from merlin.engine import MerlinView

    page = """<body style='margin:0;background:#ffffff'>
    <div style='position:sticky;top:0;z-index:10;height:40px;background:#ff0000'></div>
    <div style='position:relative;height:300px;background:#00ff00'></div>
    <div style='position:relative;height:40px'>
      <div style='position:absolute;z-index:-1;top:0;left:0;width:100px;height:40px;background:#0000ff'></div>
      <div style='width:50px;height:40px;background:#ffff00'></div></div>
    <div style='position:relative;overflow:hidden;width:100px;height:40px;background:#eeeeee'>
      <div style='position:absolute;top:0;left:0;width:400px;height:40px;background:#ff00ff'></div></div>
    <div style='opacity:0.5'><div style='opacity:0.5;height:40px;background:#000000'></div></div>
    <div style='height:2000px'></div></body>"""
    view = MerlinView()
    view.resize(600, 600)
    view.show()
    try:
        view.setHtml(page, QUrl("about:blank"))
        wait(app, 0.3)
        view.scrollbar.setValue(100)
        wait(app, 0.1)
        check("a sticky header with a z-index stays above positioned content scrolling under it",
              view.grab().toImage().pixelColor(300, 20).name() == "#ff0000")
        view.scrollbar.setValue(0)
        wait(app, 0.1)
        shot = view.grab().toImage()
        at = lambda x, y: shot.pixelColor(x, y)                         # noqa: E731
        check("a z-index below 0 is behind ordinary content",
              at(25, 360).name() == "#ffff00" and at(75, 360).name() == "#0000ff")
        check("a positioned child of overflow: hidden, painted later, is still clipped",
              at(50, 400).name() == "#ff00ff" and at(150, 400).name() == "#ffffff")
        check("opacity inside opacity is applied once each (a quarter)",
              abs(at(300, 440).red() - 191) < 4, str(at(300, 440).red()))
    finally:
        view.close()


def test_big_page_laid_out_in_background(app) -> None:
    """A big page is laid out on a thread: the window keeps drawing meanwhile,
    and the load is finished only once the page is laid out."""
    from merlin.engine import MerlinView
    from merlin.engine import view as engine_view

    rows = "".join(f"<div style='display:flex;gap:6px'><div style='flex:1'><b>Item {i}</b> "
                   f"<span>words to wrap</span></div><div style='width:120px'>{i}</div></div>"
                   for i in range(1500))
    folder = tempfile.mkdtemp(prefix="merlin-big-")
    open(os.path.join(folder, "big.html"), "w").write("<title>Big</title><body>" + rows)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    view = MerlinView()
    view.resize(1200, 800)
    view.show()
    last, gaps, finished = [None], [], []

    def tick():
        now = time.perf_counter()
        if last[0] is not None:
            gaps.append(now - last[0])
        last[0] = now

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(tick)
    view.loadFinished.connect(lambda ok: finished.append(view._display is not None
                                                        and view._display.height > 10000))
    try:
        timer.start()
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/big.html"))
        wait(app, 5.0)
        timer.stop()
        check("a big page is laid out off the main thread",
              view._element_count > engine_view.BIG_PAGE)
        check("the window kept drawing: no pause as long as a quarter-second",
              gaps and max(gaps) < 0.25, f"longest {max(gaps or [0]):.2f}s")
        check("and the load finished only once the page was laid out", finished == [True],
              str(finished))
    finally:
        server.shutdown()
        view.close()


def test_viewport_units_follow_the_window(app) -> None:
    """Styles using vw or vh are worked out again when the window's size changes.

    The new tab page, styled while its view was still tiny, kept its search box
    and tiles as narrow as they were then: 86vw of a 100px view.
    """
    from merlin.engine import MerlinView

    page = ("<body style='margin:0'><div style='display:flex;flex-wrap:wrap;width:min(680px, 90vw)'>"
            + "".join(f"<a href='https://t{i}.test/' style='display:block;padding:8px'>Tile {i}</a>"
                      for i in range(5)) + "</div>")
    view = MerlinView()
    view.resize(100, 30)
    try:
        view.setHtml(page, QUrl("about:blank"))
        view.resize(1600, 800)
        view.show()
        wait(app, 2.0)
        # each tile's top edge: a link has a rectangle for its box and one for its text
        tops = {}
        for rect, href in view._display.links:
            if href.startswith("https://t"):
                tops[href] = min(tops.get(href, rect.y()), rect.y())
        check("styled while tiny, then grown: vw is worked out again, the tiles in one row",
              len(tops) == 5 and len({round(y) for y in tops.values()}) == 1, str(tops))
    finally:
        view.close()


def test_gradients_dithered() -> None:
    """A large gradient is dithered: its blend smooth, not stripes of one shade."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter

    from merlin.engine import paint as engine_paint
    from merlin.engine.css import parse_gradients

    spec = parse_gradients("linear-gradient(90deg, #0f2438 0%, #1b3a4d 100%)")[0]
    img = QImage(1800, 400, QImage.Format.Format_ARGB32)
    img.fill(QColor("magenta"))
    painter = QPainter(img)
    engine_paint.fill_gradient(painter, QRectF(0, 0, 1800, 400), spec)
    painter.end()
    errors = []
    for x0 in range(0, 1800, 32):
        block = [img.pixelColor(x, y).red() for x in range(x0, min(x0 + 32, 1800)) for y in range(180, 220)]
        errors.append(abs(sum(block) / len(block) - (15 + 12 * (x0 + 16) / 1800)))
    check("a dark, gentle gradient follows its true blend (dithered, no stripes)",
          sum(errors) / len(errors) < 0.08, f"{sum(errors) / len(errors):.3f} of a shade on average")
    check("and it is not lightened overall by the dithering",
          abs(sum(img.pixelColor(x, 200).red() for x in range(1800)) / 1800 - 21.0) < 0.3)


def test_github_header_bugs() -> None:
    """Four bugs GitHub's header met, each on its own.

    Its menu buttons drew in a browser's own grey, one of them 100,000 pixels
    wide, pushing Sign in and Sign up off the screen, and its colours from
    camelCase variables came to nothing.
    """
    from merlin.engine.css import Styler, expand_shorthand
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    check("background: 0 0 (a minifier's background: none) clears a button's own grey",
          ("background-color", "transparent") in expand_shorthand("background", "0 0"))
    check("and background with only an image clears the colour too",
          ("background-color", "transparent") in expand_shorthand("background", "url(a.png) no-repeat"))
    doc = parse("<style>:root{--brand-bgColor:#0fbf3e} p{background-color:var(--brand-bgColor)}</style><p>x")
    p = next(e for e in doc.root.elements() if e.tag == "p")
    check("a camelCase custom property keeps its case and is found",
          Styler(doc).compute()[p]["background-color"] == (15, 191, 62, 255))
    # a flex button with space-between, in a block, in a flex row
    markup = ("<body style='margin:0'><ul style='display:flex;margin:0;padding:0;list-style:none'>"
              "<li><div><button style='display:flex;justify-content:space-between;background:0 0;border:0'>"
              "Platform <span>v</span></button></div></li><li><a href='/pricing'>Pricing</a></li></ul>")
    doc = parse(markup)
    styles = Styler(doc).compute()
    out = Layout(doc, styles, 1000).run()
    pricing = next(r for r, h in out.links if h == "/pricing")
    check("a space-between button in a block is as wide as its content, not the whole line",
          pricing.x() < 300, f"Pricing at x={pricing.x():.0f}")
    # a row whose first item cannot shrink: the other takes all the shrinking
    markup = ("<body style='margin:0'><div style='display:flex;width:1000px'>"
              "<nav style='white-space:nowrap'>Platform Solutions Resources Open Source "
              "Enterprise Pricing</nav>"
              "<div style='width:100%;display:flex;justify-content:flex-end'><a href='/login'>Sign in</a></div></div>")
    doc = parse(markup)
    out = Layout(doc, Styler(doc).compute(), 1000).run()
    login = next(r for r, h in out.links if h == "/login")
    check("an item that cannot shrink gives its share to the rest: nothing pushed off",
          login.right() <= 1000.5, f"Sign in ends at x={login.right():.0f}")


def test_finer_rule_index() -> None:
    """Rules filed under attributes, :root, :where() and :is(): the same styles
    as trying every rule on every element, found far faster."""
    from merlin.engine import css as engine_css
    from merlin.engine.css import Styler
    from merlin.engine.html import parse

    sheet = ("[data-mode=dark] { color: rgb(1, 2, 3) } :root { --x: 4px } "
             ":where(.a, .b) { margin-left: 7px } :is(.c) { padding-left: 3px } "
             ":not(.z) { border-top-width: 1px } * { outline-width: 2px } "
             "p::before { color: red } a:hover { color: red } li:first-child { font-size: 30px }")
    markup = ("<html data-mode=dark><body><p class=a>a</p><p class=b data-mode=dark>b</p>"
              "<div class='c z'>c</div><ul><li>1</li><li>2</li></ul><a href=#>x</a>")
    doc = parse("<style>" + sheet + "</style>" + markup)
    fast = Styler(doc).compute()
    elements = [doc.root] + list(doc.root.elements())
    # the reference: every rule tried on every element
    real = engine_css.index_keys
    engine_css.index_keys = lambda selector: [("any", "")]
    try:
        slow = Styler(doc).compute()
    finally:
        engine_css.index_keys = real
    check("filed finely, rules give the same styles as trying every one everywhere",
          all(fast.get(e) == slow.get(e) for e in elements))
    # a sheet loaded twice around another: the later copy takes the later place
    doc = parse("<style>p { color: rgb(0, 0, 255) }</style><style>p { color: rgb(255, 0, 0) }</style>"
                "<style>p { color: rgb(0, 0, 255) }</style><p>x")
    p = next(e for e in doc.root.elements() if e.tag == "p")
    check("a stylesheet given twice: its second copy still wins over what came between",
          Styler(doc).compute()[p]["color"] == (0, 0, 255, 255))


def test_web_fonts(app) -> None:
    """@font-face: read, fetched from its sheet's own address, and used; off when
    switched off; and WOFF unpacked by Merlin when Qt will not take it."""
    import zlib

    from PyQt6.QtGui import QFontMetricsF

    from merlin.engine import MerlinView
    from merlin.engine.css import font_faces
    from merlin.engine.view import woff_to_sfnt

    faces = font_faces("@font-face{font-family:'Mona Sans';src:local(Mona),"
                       "url(m.woff2) format('woff2 supports variations'),url(m.woff);font-weight:200 900}")
    check("@font-face is read: family, sources in order, weight",
          faces == [{"family": "Mona Sans", "sources": [("m.woff2", "woff2 supports variations"),
                                                        ("m.woff", "")],
                     "weight": "200 900", "style": "normal"}], str(faces))
    ttf_path = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                                 "C:/Windows/Fonts/consola.ttf") if os.path.exists(p)), None)
    if ttf_path is None:
        print("  skip  web fonts: no monospace TrueType font found to serve")
        return
    ttf = open(ttf_path, "rb").read()
    # a WOFF made by hand: each table zlib-packed, as the format lays them out
    import struct
    count = struct.unpack(">H", ttf[4:6])[0]
    tables = [struct.unpack(">4sIII", ttf[12 + 16 * i:28 + 16 * i]) for i in range(count)]
    body, entries, offset = b"", [], 44 + 20 * count
    for tag, checksum, where, length in tables:
        raw = ttf[where:where + length]
        packed = zlib.compress(raw)
        packed = packed if len(packed) < len(raw) else raw
        entries.append(struct.pack(">4sIIII", tag, offset + len(body), len(packed), length, checksum))
        body += packed + b"\0" * (-len(packed) % 4)
    woff = struct.pack(">4s4sIHHIHHIIIII", b"wOFF", ttf[:4], 44 + 20 * count + len(body), count, 0,
                       len(ttf), 1, 0, 0, 0, 0, 0, 0) + b"".join(entries) + body
    unpacked = woff_to_sfnt(woff)
    check("a WOFF font is unpacked into the font it packs",
          unpacked is not None and unpacked[:4] == ttf[:4] and len(unpacked) >= len(ttf) - 64)
    folder = tempfile.mkdtemp(prefix="merlin-fonts-")
    os.makedirs(os.path.join(folder, "css", "fonts"))
    open(os.path.join(folder, "css", "fonts", "face.woff"), "wb").write(woff)
    open(os.path.join(folder, "css", "site.css"), "w").write(
        "@font-face{font-family:'Merlin Test Face';src:url(fonts/face.woff) format('woff')}"
        "p.web{font-family:'Merlin Test Face',serif;font-size:20px}")
    open(os.path.join(folder, "index.html"), "w").write(
        "<title>Fonts</title><link rel=stylesheet href=/css/site.css><p class=web>iiiiiiiiii</p>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        def __init__(self, on):
            self.settings = {"web_fonts": on}

    widths = {}
    try:
        for on in (True, False):
            view = MerlinView()
            view._host = Host(on)
            view.resize(800, 300)
            view.show()
            view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))
            wait(app, 3.0)
            runs = [i for i in view._display.items if i and i[0] == "text" and i[3].startswith("iii")]
            widths[on] = sum(QFontMetricsF(i[4]).horizontalAdvance(i[3]) for i in runs)
            if on:
                check("a site's own font, from its stylesheet's folder, is used",
                      "merlin test face" in view._font_aliases, str(view._font_aliases))
            view.close()
        check("text in the site's monospace font is wider than without it",
              widths[True] > widths[False] * 1.4, f"{widths[True]:.0f} against {widths[False]:.0f}")
    finally:
        server.shutdown()


def test_fixed_in_stacking_order(app) -> None:
    """position: fixed painted in CSS's stacking order, not over everything.

    GitHub's opening section is fixed at z-index 0 and the page scrolls up over
    it; its header, fixed at 99 inside a fixed wrapper, inside a box with
    z-index: auto and overflow hidden, stays over everything.
    """
    from PyQt6.QtCore import QPointF

    from merlin.engine import MerlinView

    page = """<body style='margin:0;background:#ffffff'>
    <div style='position:relative;overflow:hidden;height:10px'>
      <div style='position:fixed;top:0;left:0;right:0;z-index:99'>
        <div style='position:fixed;top:0;left:0;width:60px;height:40px;background:#00ff00'></div>
      </div>
    </div>
    <div style='position:fixed;top:0;left:0;width:100%;height:300px;z-index:0;background:#ff0000'>
      <a href='/hero' style='display:block;height:140px'>hero</a><input name=q></div>
    <div style='height:300px'></div>
    <section style='position:relative;z-index:1;height:800px;background:#0000ff'>
      <a href='/content' style='display:block;height:800px'>content</a></section>
    <div style='height:1500px'></div></body>"""
    view = MerlinView()
    view.resize(600, 400)
    view.show()
    try:
        view.setHtml(page, QUrl("http://example.test/"))
        wait(app, 0.3)

        def at(x, y):
            return view.grab().toImage().pixelColor(x, y).name()

        check("a fixed box at z-index 0 shows while nothing covers it", at(300, 100) == "#ff0000", at(300, 100))
        check("a fixed box nested in a fixed box, inside overflow: hidden, is drawn",
              at(20, 20) == "#00ff00", at(20, 20))
        field = next(w for e, w in view._widgets.items() if e.attrs.get("name") == "q")
        check("its field shows while uncovered", field.isVisible())
        view.scrollbar.setValue(250)
        wait(app, 0.2)
        check("scrolled, later content with z-index 1 comes over the z-index 0 box",
              at(300, 100) == "#0000ff", at(300, 100))
        check("while the box with z-index 99, held in an auto ancestor, stays over all",
              at(20, 20) == "#00ff00", at(20, 20))
        check("the covered box's field hides", not field.isVisible())
        check("a click where content covers the z-index 0 box goes to the content",
              view._link_at(QPointF(300, 100)) == "/content", view._link_at(QPointF(300, 100)))
    finally:
        view.close()


def test_overlays_and_document_order(app) -> None:
    """A see-through gradient stays see-through, and equal z-indexes paint in the
    page's order. Together these hid GitHub's opening section: its overlay,
    white at 0 to 10%, came out solid, and was painted over it."""
    from merlin.engine import MerlinView

    page = """<body style='margin:0;background:#ffffff'>
    <div style='position:relative;height:500px'>
      <div style='position:absolute;inset:0;background:linear-gradient(#fff0, #ffffff1a)'></div>
      <div style='position:relative;height:500px'>
        <div style='height:500px;background:#ff0000'></div></div>
    </div></body>"""
    view = MerlinView()
    view.resize(700, 500)
    view.show()
    try:
        view.setHtml(page, QUrl("about:blank"))
        wait(app, 0.3)
        shot = view.grab().toImage()
        colour = shot.pixelColor(350, 100)
        check("an absolute box earlier in the page is painted under a later one",
              colour.name() == "#ff0000", colour.name())
    finally:
        view.close()
    # the overlay alone, over red, at a size that would be dithered
    page = """<body style='margin:0;background:#ff0000'>
    <div style='position:absolute;top:0;left:0;width:700px;height:500px;
     background:linear-gradient(#fff0, #ffffff1a)'></div></body>"""
    view = MerlinView()
    view.resize(700, 500)
    view.show()
    try:
        view.setHtml(page, QUrl("about:blank"))
        wait(app, 0.3)
        top = view.grab().toImage().pixelColor(350, 5)
        check("a large see-through gradient lets what is under it show (not dithered opaque)",
              top.red() > 240 and top.green() < 30, top.name())
    finally:
        view.close()


def _shown_style(view, eid) -> dict:
    """The computed style of the element with id eid and of its nearest hidden
    ancestor, if one is: display none anywhere above counts."""
    element = next((x for x in view._document.root.elements() if x.id == eid), None)
    while element is not None:
        style = view._styles.get(element, {})
        if style.get("display") == "none":
            return style
        element = element.parent
    return {}


def _deno_for_tests() -> str:
    from merlin.media import deno_path

    found = os.environ.get("MERLIN_DENO") or deno_path()
    return found if found and os.path.exists(found) else ""


def test_javascript(app) -> None:
    """JavaScript in Merlin Engine: a hydrated React app, clicked, typed in and
    sent in Merlin; inline handlers; modules only from allowed hosts; cookies
    and storage; and nothing at all on a site not allowed."""
    from http.cookiejar import Cookie

    from PyQt6.QtTest import QTest

    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    deno = _deno_for_tests()
    if not deno:
        print("  skip  JavaScript: Deno is not here (set MERLIN_DENO, or fetch it in Settings)")
        return
    folder = tempfile.mkdtemp(prefix="merlin-js-")
    fixtures = os.path.join(ROOT, "tests", "fixtures", "react18")
    for name in os.listdir(fixtures):
        shutil.copy(os.path.join(fixtures, name), folder)
    open(os.path.join(folder, "repos.json"), "w").write('[{"name": "merlin-browser"}, {"name": "naru"}]')
    os.makedirs(os.path.join(folder, "js"))
    open(os.path.join(folder, "js", "lib.js"), "w").write("export const greet = w => `hello from ${w}`;")
    open(os.path.join(folder, "js", "main.js"), "w").write(
        "import { greet } from './lib.js';\n"
        "document.getElementById('mod').textContent = greet('modules');\n"
        "document.getElementById('cookie').textContent = 'cookies: ' + document.cookie;\n"
        "document.getElementById('kept').textContent = 'kept: ' + localStorage.getItem('visits');\n"
        "localStorage.setItem('visits', String(Number(localStorage.getItem('visits') || 0) + 1));")
    open(os.path.join(folder, "js", "greet.js"), "w").write(
        "export const greet = (w) => `mapped import for ${w}`;")
    open(os.path.join(folder, "js", "fade.js"), "w").write(
        "import { greet } from 'lib/greet';\n"
        "document.getElementById('mapped').textContent = greet('modules');\n"
        "class Fancy extends HTMLElement {}\n"
        "Fancy.observedAttributes = ['size'];\n"
        "customElements.define('fancy-el', Fancy);\n"
        "document.getElementById('elements').textContent = 'custom elements: ' + Fancy.observedAttributes.join();\n"
        "const hero = document.getElementById('hero');\n"
        "new IntersectionObserver((es) => { for (const e of es) hero.className = e.isIntersecting ? '' : 'faded'; })"
        ".observe(document.getElementById('sentinel'));\n"
        "const box = document.getElementById('measured').getBoundingClientRect();\n"
        "document.getElementById('size').textContent = `measured ${Math.round(box.width)}x${Math.round(box.height)}`;")
    open(os.path.join(folder, "fade.html"), "w").write(
        "<!DOCTYPE html><html><head><title>Fade</title>"
        "<script type=importmap>{\"imports\": {\"lib/greet\": \"/js/greet.js\"}}</script>"
        "<style>body{margin:0} #hero{position:fixed;top:0;left:0;right:0;height:200px;z-index:0;background:#f00}"
        "#hero.faded{opacity:0} #sentinel{height:300px} #measured{width:250px;height:40px}</style></head>"
        "<body><div id=hero>pinned</div><div id=sentinel></div><p id=mapped>waiting</p><p id=elements></p>"
        "<div id=measured></div><p id=size></p><div style='height:3000px'></div>"
        "<script type=module src=/js/fade.js></script></body></html>")
    open(os.path.join(folder, "order.js"), "w").write(
        "document.getElementById('order').textContent = 'config: ' + window.siteConfig.name;\n"
        "document.getElementById('domain').textContent = 'domain: ' + document.domain;\n"
        "requestAnimationFrame(() => { const broken = undefined; broken.charAt(0); });\n"
        "setTimeout(() => { document.getElementById('after').textContent = 'still running after the error'; }, 300);")
    open(os.path.join(folder, "order.html"), "w").write(
        "<!DOCTYPE html><html><head><title>Order</title><script defer src='/order.js'></script></head>"
        "<body><p id=order>waiting</p><p id=domain></p><p id=after></p>"
        "<script>window.siteConfig = { name: 'alterniTech' };</script></body></html>")
    open(os.path.join(folder, "js", "n.js"), "w").write("export const n = 42;")
    open(os.path.join(folder, "features.html"), "w").write(
        "<!DOCTYPE html><html><head><title>Features</title></head><body>"
        "<p id=a></p><p id=b></p><p id=c></p><p id=d></p><p id=e></p>"
        "<script>const out = (id, t) => document.getElementById(id).textContent = t;"
        "out('a', 'process: ' + typeof process + ', Buffer: ' + typeof Buffer);"
        "out('b', 'escape: ' + CSS.escape('1a.b') + ', supports: ' + CSS.supports('display', 'grid'));"
        "out('c', 'selectors: ' + document.querySelectorAll(':target, p').length + ' ' + document.body.matches(':hover'));"
        "import('/js/n.js').then(m => out('d', 'plain-script import(): ' + m.n));</script>"
        "<script type=module>import { n } from './js/n.js'; out('e', 'inline module static import: ' + n);</script>"
        "</body></html>")
    open(os.path.join(folder, "google-like.html"), "w").write(
        "<!DOCTYPE html><html><head><title>G</title>"
        "<noscript><style>table,div,span,p{display:none}</style></noscript><style>p{margin:0}</style></head><body>"
        "<div><p id=made>waiting</p></div><p id=parts></p><input name=q><input name=q><p id=elements></p>"
        "<p id=sheets></p><p id=busy>0</p>"
        "<script>document.getElementById('made').textContent = 'made by script';"
        "var s = document.createElement('a'); s.href = 'https://example.test/shop/item?x=1#top';"
        "document.getElementById('parts').textContent = [s.protocol, s.host, s.pathname, s.search, s.hash, s.pathname.charAt(0)].join(' | ');"
        "const out = ['byName: ' + document.getElementsByName('q').length];"
        "customElements.define('toggle-switch', class extends HTMLElement {});"
        "try { customElements.define('toggle-switch', class extends HTMLElement {}); out.push('no refusal'); }"
        "catch (e) { out.push('refused: ' + (e instanceof DOMException) + ' ' + e.name); }"
        "document.getElementById('elements').textContent = out.join(' | ');"
        "var extra = document.createElement('style'); document.head.appendChild(extra);"
        "extra.sheet.insertRule('.added { color: blue }', 0);"
        "document.getElementById('sheets').textContent = 'sheets: ' + document.styleSheets.length + ', ' + extra.textContent.trim();"
        "let n = 0; const ticking = setInterval(() => { document.getElementById('busy').textContent = String(++n);"
        " if (n >= 100) clearInterval(ticking); }, 20);</script></body></html>")
    open(os.path.join(folder, "more.html"), "w").write(
        "<title>More</title><p id=mod>waiting</p><p id=cookie></p><p id=kept></p>"
        "<button id=old onclick=\"this.textContent='clicked inline'\" style='padding:10px'>old style</button>"
        "<script type=module src=/js/main.js></script>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    storage = LocalStorage(tempfile.mkdtemp(prefix="merlin-storage-"))

    class Host:
        settings = {}

        def __init__(self, allowed):
            self.allowed = allowed

        def javascript_allowed(self, site):
            return self.allowed

        def deno_for_scripts(self):
            return deno

        def script_cache_dir(self):
            return os.path.join(folder, "deno-cache")

        def local_storage(self):
            return storage

    def text_of(view, eid):
        if view._document is None:
            return None
        found = next((x for x in view._document.root.elements() if x.id == eid), None)
        return " ".join(found.text().split()) if found is not None else None

    def until(view, test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.02)
        return test()

    def click(view, eid):
        box = next(r for r, e in view._display.boxes if e.id == eid)
        QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                         pos=QPoint(int(box.center().x()), int(box.center().y() - view._scroll)))

    try:
        view = MerlinView()
        view._host = Host(False)
        view.resize(900, 600)
        view.show()
        view.setUrl(QUrl(base + "/index.html"))
        wait(app, 2.5)
        check("on a site not allowed, no script runs", view._script is None and not text_of(view, "repos"))
        view.close()

        view = MerlinView()
        view._host = Host(True)
        view.resize(900, 600)
        view.show()
        view.setUrl(QUrl(base + "/index.html"))
        check("allowed, a server-rendered React app hydrates, with data it fetched",
              until(view, lambda: "merlin-browser" in (text_of(view, "repos") or ""), 15),
              str(text_of(view, "repos")))
        click(view, "count")
        check("a click in Merlin reaches React's handler",
              until(view, lambda: text_of(view, "count") == "Clicked 1 times", 5), str(text_of(view, "count")))
        field = next(w for e, w in view._widgets.items() if e.id == "q")
        field.setFocus()
        QTest.keyClicks(field, "hello")
        check("typing in Merlin's field reaches React's state",
              until(view, lambda: text_of(view, "echo") == "typed: hello", 5), str(text_of(view, "echo")))
        field = next(w for e, w in view._widgets.items() if e.id == "q")
        check("and the field keeps its text and focus as React re-renders",
              field.text() == "hello" and field.hasFocus())
        QTest.keyClick(field, Qt.Key.Key_Return)
        check("Enter goes to React's onSubmit, which keeps Merlin on the page",
              until(view, lambda: text_of(view, "sent") == "sent: hello", 5)
              and view.url().toString().endswith("index.html"), str(text_of(view, "sent")))
        check("with no errors in the page's console",
              not [t for level, t in view.console_lines if level == "error"],
              str([t for level, t in view.console_lines if level == "error"][:2]))
        # cookies: the page sees its own, not HttpOnly ones
        jar = view._cookies()
        for name, httponly in (("visible", False), ("secret", True)):
            jar.set_cookie(Cookie(0, name, "1", None, False, "127.0.0.1", False, False, "/", True, False,
                                  None, False, None, None, {"HttpOnly": None} if httponly else {}))
        view.setUrl(QUrl(base + "/more.html"))
        check("an ES module and its import load, from the page's own host",
              until(view, lambda: text_of(view, "mod") == "hello from modules", 15), str(text_of(view, "mod")))
        check("document.cookie shows the page's cookies but not HttpOnly ones",
              "visible=1" in (text_of(view, "cookie") or "") and "secret" not in (text_of(view, "cookie") or ""),
              str(text_of(view, "cookie")))
        click(view, "old")
        check("an inline onclick attribute runs", until(view, lambda: text_of(view, "old") == "clicked inline", 5),
              str(text_of(view, "old")))
        view.setUrl(QUrl(base + "/more.html"))
        check("localStorage is kept from one visit to the next",
              until(view, lambda: text_of(view, "kept") == "kept: 1", 15), str(text_of(view, "kept")))
        # GitHub's ways: an import map, observedAttributes assigned, measuring,
        # and a section that fades as a marker scrolls out of view
        view.setUrl(QUrl(base + "/fade.html"))
        check("a bare module name resolves through the page's import map",
              until(view, lambda: text_of(view, "mapped") == "mapped import for modules", 15),
              str(text_of(view, "mapped")))
        check("an element class may have observedAttributes assigned",
              text_of(view, "elements") == "custom elements: size", str(text_of(view, "elements")))
        check("getBoundingClientRect gives the real size from the first run",
              text_of(view, "size") == "measured 250x40", str(text_of(view, "size")))

        def hero():
            return next((x.attrs.get("class", "") for x in view._document.root.elements() if x.id == "hero"), None)

        view.scrollbar.setValue(600)
        check("IntersectionObserver sees a marker scroll out of view: the section fades",
              until(view, lambda: hero() == "faded", 5), str(hero()))
        view.scrollbar.setValue(0)
        check("and scrolled back, it comes back", until(view, lambda: hero() == "", 5), str(hero()))
        # Square's ways: defer scripts after the inline ones that set them up;
        # and an error in a callback does not end the page's scripts
        view.setUrl(QUrl(base + "/order.html"))
        check("a defer script runs after the inline scripts below it, as HTML orders them",
              until(view, lambda: text_of(view, "order") == "config: alterniTech", 15),
              str(text_of(view, "order")))
        check("document.domain is the page's host", text_of(view, "domain") == "domain: 127.0.0.1",
              str(text_of(view, "domain")))
        check("an error in a frame callback is reported, and the scripts go on",
              until(view, lambda: text_of(view, "after") == "still running after the error", 5)
              and view._script is not None, str(text_of(view, "after")))
        check("and the console shows the code where it went wrong",
              any(">>>HERE>>> charAt(0)" in text for level, text in view.console_lines),
              str([t for level, t in view.console_lines if "near" in t][:1]))
        # what GitHub, Google and Hugging Face tripped on
        view.setUrl(QUrl(base + "/features.html"))
        check("a page sees no Node globals (process, Buffer), as in a browser",
              until(view, lambda: text_of(view, "a") == "process: undefined, Buffer: undefined", 15),
              str(text_of(view, "a")))
        check("CSS.escape and CSS.supports exist", text_of(view, "b") == "escape: \\31 a\\.b, supports: true",
              str(text_of(view, "b")))
        check("a selector with :target or :hover finds what it can rather than failing",
              text_of(view, "c") == "selectors: 5 false", str(text_of(view, "c")))
        check("import() in a plain script loads from the page's site, and strings are left alone",
              until(view, lambda: text_of(view, "d") == "plain-script import(): 42", 5), str(text_of(view, "d")))
        check("an inline module sees a plain script's const, and imports relative to the page",
              until(view, lambda: text_of(view, "e") == "inline module static import: 42", 5),
              str(text_of(view, "e")))
        # Google's no-script fallback, Square's addresses, GitHub's elements
        view.setUrl(QUrl(base + "/google-like.html"))
        check("where scripts run, <noscript> is nothing: its style does not hide the page",
              until(view, lambda: text_of(view, "made") == "made by script", 15)
              and _shown_style(view, "made").get("display") != "none", str(text_of(view, "made")))
        check("a link has its address's parts, as in a browser (Square reads link.pathname)",
              text_of(view, "parts") == "https: | example.test | /shop/item | ?x=1 | #top | /",
              str(text_of(view, "parts")))
        check("document.getElementsByName, and a second definition refused with a NotSupportedError",
              text_of(view, "elements") == "byName: 2 | refused: true NotSupportedError",
              str(text_of(view, "elements")))
        check("document.styleSheets lists the page's sheets, and insertRule adds to one (Google's CSS)",
              text_of(view, "sheets") == "sheets: 2, .added { color: blue }", str(text_of(view, "sheets")))
        applied = []
        real_apply = view._apply_script_dom
        view._apply_script_dom = lambda *a: (applied.append(1), real_apply(*a))
        view._script_prepared.disconnect()
        view._script_prepared.connect(view._apply_script_dom)
        check("a page changed every 20ms is shown at a pace the browser can bear, ending as it ends",
              until(view, lambda: text_of(view, "busy") == "100", 10) and len(applied) < 30,
              f"{len(applied)} updates, showing {text_of(view, 'busy')}")
        view.close()
    finally:
        server.shutdown()


def test_animations_and_3d(app) -> None:
    """CSS animations, 2D and 3D transforms, played and drawn by Merlin Engine."""
    from merlin.engine import MerlinView
    from merlin.engine.animate import matrix, sample
    from merlin.engine.css import parse_transform_ops

    page = """<style>body{margin:0;background:#fff}
    @media (min-width: 10px) { @keyframes slide { from { transform: translateX(0) } to { transform: translateX(-50%) } } }
    @keyframes fadein { from { opacity: 0 } to { opacity: 1 } }
    #strip { position:absolute; top:0; left:0; width:800px; height:40px; animation: slide 2s linear infinite;
             background: #f00; border-left: 400px solid #00f }
    #fade { position:absolute; top:60px; left:0; width:100px; height:40px; background:#0a0; opacity:0;
            animation: fadein .3s linear forwards }
    #turn { position:absolute; top:150px; left:50px; width:100px; height:100px; background:#000; transform: rotate(45deg) }
    #stage { position:absolute; top:300px; left:300px; width:200px; height:120px; perspective: 400px }
    #card { width:200px; height:120px; background:#f0f; transform: rotateY(60deg) }
    #back { position:absolute; top:150px; left:300px; width:100px; height:100px; background:#ff0;
            transform: rotateY(180deg); backface-visibility: hidden }
    #tall { position:absolute; top:500px; height:3000px; width:10px }
    </style><div id=strip></div><div id=fade></div><div id=turn></div><div id=stage><div id=card></div></div>
    <div id=back></div><div id=tall></div>"""
    view = MerlinView()
    view.resize(700, 500)
    view.show()
    try:
        view.setHtml(page, QUrl("about:blank"))
        wait(app, 0.4)

        def pixel(x, y):
            return view.grab().toImage().pixelColor(x, y).name()

        check("a page with animations keeps time", view._animation_timer.isActive())

        def boundary():
            # where the strip's blue border gives way to its red: it moves left
            row = view.grab().toImage()
            return next((x for x in range(0, 700) if row.pixelColor(x, 20).name() == "#ff0000"), None)

        before = boundary()
        wait(app, 0.5)
        after = boundary()
        check("a keyframes animation moves its element (a marquee)",
              before is not None and after is not None and after < before - 30, f"{before} then {after}")
        check("a fade-in from opacity 0 ends shown, and stays (forwards)", pixel(50, 80) == "#00aa00",
              pixel(50, 80))
        check("rotate(45deg) turns a square into a diamond",
              pixel(52, 152) == "#ffffff" and pixel(100, 140) == "#000000",
              f"corner {pixel(52, 152)}, top point {pixel(100, 140)}")
        image = view.grab().toImage()

        def height(x):
            return sum(1 for y in range(260, 460) if image.pixelColor(x, y).name() == "#ff00ff")

        columns = [x for x in range(280, 520) if height(x) > 0]
        check("rotateY(60deg) in a parent's perspective is drawn foreshortened: its far edge shorter",
              bool(columns) and columns[-1] - columns[0] < 150 and height(columns[0] + 2) > height(columns[-1] - 2) + 20,
              f"{columns[0] if columns else None}..{columns[-1] if columns else None}")
        check("a box turned to its back, with backface-visibility hidden, is not drawn",
              pixel(350, 200) == "#ffffff", pixel(350, 200))
        updates = []
        real_update = view.update
        view.update = lambda *a: updates.append(a)
        view._animation_tick()
        check("with an animation in view, its part of the window is drawn again", bool(updates))
        view.scrollbar.setValue(1500)
        wait(app, 0.2)
        updates.clear()
        view._animation_tick()
        check("with it scrolled out of view, nothing is drawn again for it", not updates, str(updates))
        view.update = real_update
    finally:
        view.close()
    # GitHub's marquee: translateX(calc(-100% - gap)), a percentage and pixels
    ops = parse_transform_ops("translatex(calc(-100% - 64px))", 16, 16, (1000, 800))
    marquee = {"duration": 60.0, "iterations": float("inf"), "timing": "linear",
               "frames": [(0.0, {"transform": parse_transform_ops("translate(0)", 16, 16, (1000, 800))}, None),
                          (1.0, {"transform": ops}, None)]}
    moved, _ = sample(marquee, 30.0, [], 1.0, (0, 0, 2000, 60))
    shift = matrix(moved, (0, 0, 2000, 60), (0, 0, 0))[0][3]
    check("calc() of a percentage and pixels in a keyframe: GitHub's marquee moves as it should",
          abs(shift + 1032) < 1, f"{shift:.0f}px at 30s")


def test_noscript_without_scripts_and_fonts(app) -> None:
    """With JavaScript off, <noscript> shows, as it should; and a font list
    with a generic family before an emoji font draws text in a text font."""
    from merlin.engine import MerlinView
    from merlin.engine.layout import _Fonts

    view = MerlinView()
    view.show()
    try:
        view.setHtml("<noscript><p id=fallback>for browsers without scripts</p></noscript>", QUrl("about:blank"))
        wait(app, 0.3)
        shown = next((x for x in view._document.root.elements() if x.id == "fallback"), None)
        check("without scripts running, what is in <noscript> is shown", shown is not None)
    finally:
        view.close()
    fonts = _Fonts(1.0)
    font, metrics = fonts.get({"font-family": "Not A Real Font,sans-serif,Noto Color Emoji", "font-size": 20.0,
                               "font-weight": 400, "font-style": "normal"})
    families = font.families()
    check("a generic family is real fonts in its place, and an emoji font comes after them",
          families and "emoji" not in families[1].lower() and "emoji" in families[-1].lower()
          if any("emoji" in f.lower() for f in families) else bool(families), str(families))
    space, letter = metrics.horizontalAdvance(" "), metrics.horizontalAdvance("n")
    check("so a space is an ordinary width, not an emoji font's", 0 < space <= letter, f"space {space:.1f}, n {letter:.1f}")


def test_github_profile_layout(app) -> None:
    """What GitHub's profile page needed: a grid column of calc(100% - ...),
    the spare room going to it, a clearfix, and border-box flex items."""
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout
    from merlin.engine.animate import sample

    page = """<style>* { box-sizing: border-box } body { margin: 0 }
    .layout { display: grid; grid-template-columns: auto 0 minmax(0, calc(100% - 300px - 20px)); grid-gap: 10px; width: 1000px }
    .side { width: 300px } .main { grid-column: 3 }
    .cf::after { content: ""; display: table; clear: both }
    .name { float: left; width: 100%; height: 50px }
    .list { display: flex; flex-wrap: wrap; width: 600px } .item { width: 50%; padding: 0 8px; height: 30px }
    textarea { display: flex; width: 400px }</style>
    <div class=layout><div class=side><div class=cf><div class=name>name</div></div><p id=details>details</p></div>
    <div class=main id=main><div class=list><div class=item id=one>a</div><div class=item id=two>b</div></div></div></div>
    <textarea name=q rows=1></textarea>"""
    document = parse(page)
    styles = Styler(document, viewport=(1200, 800)).compute()
    layout = Layout(document, styles, 1200, viewport_height=800)
    layout.live_controls = True
    out = layout.run()
    box = {e.attrs.get("id") or e.attrs.get("class"): r for r, e in out.boxes}
    check("a grid column of minmax(0, calc(100% - sidebar - gutter)) gets its room, not a sliver",
          box["main"].width() > 600, f"{box['main'].width():.0f}px")
    check("a clearfix contains its floats: what follows starts below them, not beside",
          box["details"].x() == box["side"].x() and box["details"].y() >= box["name"].bottom(),
          f"details at x {box['details'].x():.0f}, y {box['details'].y():.0f}")
    check("border-box flex items of 50% with padding sit two to a row",
          abs(box["one"].y() - box["two"].y()) < 1 and box["two"].x() > box["one"].x(),
          f"{box['one']} {box['two']}")
    check("a textarea with display: flex is still a field, with a text box to type in",
          any(it and it[0] == "control" and it[2].tag == "textarea" for it in out.items))
    zero = {"duration": 0.0, "iterations": float("inf"), "timing": "linear", "fill": "none",
            "frames": [(0.0, {"opacity": 0.0}, None), (1.0, {"opacity": 1.0}, None)]}
    try:
        result = sample(zero, 1.0, [], 1.0, (0, 0, 10, 10))
        check("an infinite animation of no duration is over at once, not an error every frame",
              result == (None, None), str(result))
    except Exception as exc:                               # noqa: BLE001
        check("an infinite animation of no duration is over at once, not an error every frame", False, str(exc))
    document = parse("<style>:root{--d:1s}@keyframes b{from{opacity:0}to{opacity:1}}"
                     "#x{animation-name:b;animation-duration:var(--d)}</style><div id=x></div>")
    styles = Styler(document).compute()
    timed = styles[next(e for e in document.root.elements() if e.id == "x")]["animations"][0]["duration"]
    check("an animation's duration given by var() is read (animate.css)", timed == 1.0, f"{timed}s")


def test_window_events_enter_dns_and_saving(app) -> None:
    """Window events heard, a timer's error survived, Enter to the page's
    scripts and forms they send, secure DNS, and the debugging zip written in
    the background."""
    import json as _json
    import zipfile

    from PyQt6.QtCore import QTimer
    from PyQt6.QtTest import QTest

    from merlin import securedns
    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    deno = _deno_for_tests()
    folder = tempfile.mkdtemp(prefix="merlin-events-")
    open(os.path.join(folder, "index.html"), "w").write(
        "<!DOCTYPE html><html><head><title>Search</title></head><body><p id=heard></p>"
        "<input id=box name=q><form id=f action=/results.html><textarea id=ta name=q rows=1></textarea></form>"
        "<script>const out = []; const show = () => document.getElementById('heard').textContent = out.join(' | ');"
        "addEventListener('load', () => { out.push('load'); show(); });"
        "addEventListener('scroll', () => { out.push('scroll ' + scrollY); show(); });"
        "addEventListener('popstate', (e) => out.push('popstate ' + e.state.page));"
        "addEventListener('message', (e) => { out.push('message ' + e.data); show(); }); postMessage('hi', '*');"
        "dispatchEvent(new PopStateEvent('popstate', { state: { page: 2 } }));"
        "setTimeout(() => { undefinedThing.call(); }, 50);"
        "setTimeout(() => { out.push('alive'); show(); }, 300);"
        "document.getElementById('box').addEventListener('keydown', (e) => { if (e.key === 'Enter') {"
        " e.preventDefault(); location.href = '/results.html?from=box&q=' + encodeURIComponent(e.target.value); } });"
        "document.getElementById('ta').addEventListener('keydown', (e) => { if (e.key === 'Enter') {"
        " e.preventDefault(); document.getElementById('f').requestSubmit(); } });</script>"
        "<div style='height:3000px'></div></body></html>")
    open(os.path.join(folder, "results.html"), "w").write("<title>Results</title><p>results</p>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def deno_for_scripts(self):
            return deno

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    def until(view, test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.02)
        return test()

    def heard(view):
        found = next((x for x in view._document.root.elements() if x.id == "heard"), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    try:
        if deno:
            view = MerlinView()
            view._host = Host()
            view.resize(700, 400)
            view.show()
            finished = []
            view.loadFinished.connect(finished.append)
            view.setUrl(QUrl(base + "/index.html"))
            check("the window's load is heard once, and a popstate with its state",
                  until(view, lambda: "load" in heard(view), 15) and heard(view).count("load") == 1
                  and "popstate 2" in heard(view), heard(view))
            check("window.postMessage delivers a message event to the window (reCAPTCHA's had stopped on it)",
                  until(view, lambda: "message hi" in heard(view), 5), heard(view))
            check("an error in a timer is reported, and the page's scripts go on",
                  until(view, lambda: "alive" in heard(view), 5) and view._script is not None, heard(view))
            view.scrollbar.setValue(400)
            check("scrolling is heard by the window's scroll listeners",
                  until(view, lambda: "scroll 400" in heard(view), 5), heard(view))
            check("the page's load finishes, its scripts running", bool(finished))
            view.close()
            for field, wanted in (("box", "from=box&q=steam"), ("ta", "results.html?q=steam")):
                # a view of its own each time: on a reload in the same view the
                # previous page could answer for the new one
                view = MerlinView()
                view._host = Host()
                view.resize(700, 400)
                view.show()
                view.setUrl(QUrl(base + "/index.html"))
                until(view, lambda: "load" in heard(view), 15)
                widget = next(w for e, w in view._widgets.items() if e.id == field)
                widget.setFocus()
                QTest.keyClicks(widget, "steam")
                wait(app, 0.4)
                widget = next(w for e, w in view._widgets.items() if e.id == field)
                QTest.keyClick(widget, Qt.Key.Key_Return)
                check(f"Enter in a {'text field' if field == 'box' else 'textarea'} goes to the page's keydown,"
                      " which sends the search",
                      until(view, lambda: wanted in view.url().toString(), 8), view.url().toString())
                view.close()
        else:
            print("  skip  window events and Enter: Deno is not here")
        # the debugging zip, written in the background: the window goes on
        view = MerlinView()
        view.show()
        view.setHtml("<title>t</title><p>page</p>", QUrl("about:blank"))
        wait(app, 0.3)
        view._script_bodies = {f"https://example.test/{i}.js": os.urandom(40000) for i in range(120)}
        ticks, result = [], {}
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(20)
        started = time.monotonic()
        view.save_for_debugging(tempfile.mkdtemp(), "t", done=lambda path, error: result.update(path=path, error=error))
        while "path" not in result and time.monotonic() - started < 30:
            app.processEvents()
            time.sleep(0.005)
        timer.stop()
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        check("the debugging zip is named only once whole: no part left over",
              os.path.exists(result["path"]) and not os.path.exists(result["path"] + ".part"))
        check("the debugging zip is written in the background, the window going on meanwhile",
              not result.get("error") and zipfile.ZipFile(result["path"]).namelist().count("scripts/index.json") == 1
              and "network.txt" in zipfile.ZipFile(result["path"]).namelist()
              and (max(gaps) if gaps else 0) < 0.25, f"longest pause {max(gaps) * 1000 if gaps else 0:.0f}ms")
        view.close()
    finally:
        server.shutdown()
    # secure DNS, against a pretend service in Cloudflare's JSON form
    asked = []

    class DoH(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            name, kind = query["name"][0], query["type"][0]
            asked.append(name)
            answer = [{"name": name, "type": 1, "TTL": 120, "data": "127.0.0.1"}] \
                if name == "merlin-test.example" and kind == "A" else []
            body = _json.dumps({"Status": 0, "Answer": answer}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/dns-json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    doh = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DoH)
    threading.Thread(target=doh.serve_forever, daemon=True).start()
    site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    endpoints, original = securedns.ENDPOINTS, socket.getaddrinfo
    try:
        securedns.ENDPOINTS = [f"http://127.0.0.1:{doh.server_address[1]}/resolve"]
        securedns._cache.clear()
        securedns.install(True)
        body = urllib.request.urlopen(f"http://merlin-test.example:{site.server_address[1]}/results.html",
                                      timeout=5).read().decode()
        check("with secure DNS, a name is looked up over HTTPS (a name only the service knows loads)",
              "results" in body and "merlin-test.example" in asked)
        asked.clear()
        urllib.request.urlopen(f"http://127.0.0.1:{site.server_address[1]}/results.html", timeout=5).read()
        check("an address or a local name is never sent to the service", not asked, str(asked))
    finally:
        securedns.install(False)
        securedns.ENDPOINTS = endpoints
        socket.getaddrinfo = original
        doh.shutdown()
        site.shutdown()


def test_pseudo_elements(app) -> None:
    """::before and ::after: icon fonts' icons, quotes, separators, attr();
    drawn, but never part of the page scripts see."""
    from merlin.engine.css import Styler
    from merlin.engine.dom import to_html
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    page = ("<style>.ti-settings::before { content: \"\\eb20\"; color: rgb(200, 0, 0) }"
            "q::before { content: open-quote } q:after { content: close-quote }"
            ".crumb + .crumb::before { content: ' / ' }"
            "a[data-count]::after { content: ' (' attr(data-count) ')' }"
            ".none::before { content: none } img::before { content: 'never' }</style>"
            "<p id=icon><i class='ti ti-settings'></i></p><p id=quote><q>quoted</q></p>"
            "<p id=crumbs><span class=crumb>Home</span><span class=crumb>Docs</span></p>"
            "<p id=count><a data-count=5 href=#>Issues</a></p><p id=none class=none>plain</p><img src=x.png>")
    document = parse(page)
    styles = Styler(document).compute()

    def text(eid):
        return " ".join(next(e for e in document.root.elements() if e.id == eid).text().split())

    check("an icon font's ::before gives its character (Tabler's settings icon)", text("icon") == "\ueb20",
          repr(text("icon")))
    icon = next(e for e in document.root.elements() if e.pseudo and e.parent.tag == "i")
    check("with its own style", styles[icon].get("color") == (200, 0, 0, 255), str(styles[icon].get("color")))
    check("quotes, separators and attr() in content, :after with one colon too",
          text("quote") == "\u201cquoted\u201d" and text("crumbs") == "Home / Docs" and text("count") == "Issues (5)",
          f"{text('quote')} | {text('crumbs')} | {text('count')}")
    check("content: none makes nothing, and an image has no ::before",
          text("none") == "plain" and not any(e.pseudo and e.parent.tag == "img" for e in document.root.elements()))
    out = Layout(document, styles, 800, viewport_height=600).run()
    check("the icon is drawn", any(it and it[0] == "text" and "\ueb20" in it[3] for it in out.items))
    check("but scripts never see a pseudo-element, as in a browser",
          "merlin-pseudo" not in to_html(document) and "\ueb20" not in to_html(document))
    Styler(document).compute()
    check("styled again, none doubles", sum(1 for e in document.root.elements() if e.pseudo) == 5)


def test_escaped_selectors_grids_and_files(app) -> None:
    """Tailwind's escaped class names, emoji in class names, a grid's natural
    width, and a page's own button opening the file dialog."""
    from PyQt6.QtWidgets import QFileDialog

    from merlin.engine import MerlinView
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout
    from merlin.engine.script import LocalStorage

    css = (r".size-\[\.6rem\] { width: 9.6px } .w-1\/2 { width: 50px } .lg\:flex { display: flex }"
           r" .\!hidden { display: none } .h-\[calc\(100\%-2rem\)\] { height: 77px }"
           r" .\32 xl\:p-4 { padding: 4px } .📚19-10-3qfj5z { width: 31px } .\31 23 span { width: 7px }")
    document = parse("<style>" + css + "</style><svg id=a class='size-[.6rem]'></svg><div id=b class='w-1/2'></div>"
                     "<div id=c class='lg:flex'></div><div id=d class='!hidden'></div><div id=e class='h-[calc(100%-2rem)]'></div>"
                     "<div id=f class='2xl:p-4'></div><div id=g class='📚19-10-3qfj5z'></div><div class='123'><span id=h>x</span></div>")
    styles = Styler(document, viewport=(1461, 737)).compute()

    def style(eid):
        return styles[next(e for e in document.root.elements() if e.id == eid)]

    check("Tailwind's escaped class names match: size-[.6rem], w-1/2, lg:flex, !hidden, h-[calc(...)], 2xl:p-4",
          style("a").get("width") == 9.6 and style("b").get("width") == 50.0 and style("c").get("display") == "flex"
          and style("d").get("display") == "none" and style("e").get("height") == 77.0 and style("f").get("padding-top") == 4.0)
    check("a class name with emoji in it matches (Square's), and a hex escape with its space",
          style("g").get("width") == 31.0 and style("h").get("width") == 7.0)
    page = ("<style>body{margin:0} button{display:flex;justify-content:space-between;padding:0 12px;font-size:14px}"
            ".content{display:grid;flex:1 0 auto;grid-template-areas:'lead text trail';justify-content:center;"
            "grid-template-columns:min-content minmax(0,auto) min-content}"
            ".label{grid-area:text;white-space:nowrap}</style><div style='display:flex'><button id=code>"
            "<span class=content><span class=label id=label>Code</span></span></button></div>")
    document = parse(page)
    styles = Styler(document).compute()
    out = Layout(document, styles, 1400, viewport_height=600).run()
    boxes = {e.attrs.get("id"): r for r, e in out.boxes if e.attrs.get("id")}
    check("a grid in a flex button is as wide as its content, its label inside (GitHub's Code button)",
          boxes["code"].width() < 200 and boxes["code"].x() <= boxes["label"].x() < boxes["code"].right(),
          f"button {boxes['code'].width():.0f} wide, label at {boxes['label'].x():.0f}")
    deno = _deno_for_tests()
    if not deno:
        print("  skip  file picker: Deno is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-pick-")
    open(os.path.join(folder, "index.html"), "w").write(
        "<!DOCTYPE html><html><head><title>Pick</title></head><body>"
        "<input type=file id=file accept='image/*' style='display:none'><button id=browse>Browse</button><p id=out>none</p>"
        "<script>const input = document.getElementById('file');"
        "document.getElementById('browse').addEventListener('click', () => input.click());"
        "input.addEventListener('change', () => { const f = input.files[0]; const r = new FileReader();"
        "r.onload = () => { document.getElementById('out').textContent = f.name + ' ' + f.type + ' ' + String(r.result).slice(0, 22); };"
        "r.readAsDataURL(f); });</script></body></html>")
    picture = os.path.join(folder, "wallpaper.png")
    open(picture, "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def deno_for_scripts(self):
            return deno

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    asked = []
    real_dialog = QFileDialog.getOpenFileName
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (asked.append(1), (picture, ""))[1])
    view = MerlinView()
    view._host = Host()
    view.resize(600, 400)
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))
        wait(app, 3.0)
        box = next(r for r, e in view._display.boxes if e.id == "browse")
        from PyQt6.QtTest import QTest

        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(int(box.center().x()), int(box.center().y())))
        end = time.time() + 6
        out = ""
        while time.time() < end and "wallpaper" not in out:
            app.processEvents()
            time.sleep(0.02)
            out = " ".join(next(e for e in view._document.root.elements() if e.id == "out").text().split())
        check("a page's own Browse button opens the file dialog, and the page reads the file chosen (Ponder)",
              asked and out.startswith("wallpaper.png image/png data:image/png;base64,"), out)
    finally:
        QFileDialog.getOpenFileName = real_dialog
        view.close()
        server.shutdown()


def test_loads_and_walkers(app) -> None:
    """A stylesheet link or image a page adds fires its load (webpack waits for
    a route's CSS so: Square's router had waited for ever), and the whole
    TreeWalker, NodeFilter, document.implementation and performance.timing."""
    from merlin.engine import MerlinView

    deno = _deno_for_tests()
    if not deno:
        print("  skip  loads and walkers: Deno is not here")
        return
    page = ("<!DOCTYPE html><html><head><title>Loads</title></head><body><div id=r><p id=a>A<b id=b>B</b></p><p id=c>C</p></div>"
            "<p id=css>waiting</p><p id=img>waiting</p><p id=walk></p><script>"
            "const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = '/route.css';"
            "new Promise((resolve, reject) => { link.onload = resolve; link.onerror = reject; document.head.appendChild(link); })"
            ".then(() => { document.getElementById('css').textContent = 'route css loaded'; });"
            "const picture = new Image(); picture.onload = () => { document.getElementById('img').textContent = 'image preloaded'; };"
            "picture.src = '/photo.png';"
            "const out = ['timing ' + (performance.timing.responseStart > 0),"
            " 'inert ' + document.implementation.createHTMLDocument('x').body.tagName];"
            "const w = document.createTreeWalker(document.getElementById('r'), NodeFilter.SHOW_ELEMENT);"
            "const seen = []; while (w.nextNode()) seen.push(w.currentNode.id); out.push('next ' + seen.join(','));"
            "const v = document.createTreeWalker(document.getElementById('r'), NodeFilter.SHOW_ELEMENT);"
            "out.push('first ' + v.firstChild().id + ' sibling ' + v.nextSibling().id);"
            "const f = document.createTreeWalker(document.getElementById('r'), NodeFilter.SHOW_ELEMENT,"
            " { acceptNode: (n) => n.id === 'b' ? NodeFilter.FILTER_SKIP : NodeFilter.FILTER_ACCEPT });"
            "const kept = []; while (f.nextNode()) kept.push(f.currentNode.id); out.push('filtered ' + kept.join(','));"
            "document.getElementById('walk').textContent = out.join(' | ');</script></body></html>")
    folder = tempfile.mkdtemp(prefix="merlin-loads-")
    open(os.path.join(folder, "index.html"), "w").write(page)
    open(os.path.join(folder, "route.css"), "w").write("p { margin: 0 }")
    # a real picture: an image now fires load only if it truly loads
    import struct as _struct
    import zlib as _zlib

    def _chunk(kind, data):
        return _struct.pack(">I", len(data)) + kind + data + _struct.pack(">I", _zlib.crc32(kind + data) & 0xffffffff)
    open(os.path.join(folder, "photo.png"), "wb").write(
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", _struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", _zlib.compress(b"\x00\x80\x80\x80")) + _chunk(b"IEND", b""))

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    from merlin.engine.script import LocalStorage

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def deno_for_scripts(self):
            return deno

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))

        def text(eid):
            if view._document is None:
                return ""
            found = next((x for x in view._document.root.elements() if x.id == eid), None)
            return " ".join(found.text().split()) if found is not None else ""

        end = time.time() + 15
        while time.time() < end and not (text("css") == "route css loaded" and text("img") == "image preloaded"):
            app.processEvents()
            time.sleep(0.02)
        check("a stylesheet link a script adds fires its load (webpack's way of loading a route's CSS)",
              text("css") == "route css loaded", text("css"))
        check("an image preloaded with new Image() fires its load", text("img") == "image preloaded", text("img"))
        check("TreeWalker whole, with NodeFilter's constants; document.implementation; performance.timing",
              text("walk") == "timing true | inert BODY | next a,b,c | first a sibling c | filtered a,c", text("walk"))
    finally:
        view.close()
        server.shutdown()


def test_images_fonts_and_viewport(app) -> None:
    """Images load or fail as they really go, with their size; fonts are
    fetched in a form every platform reads; localhost is reached on IPv4 first;
    the root element's client size is the viewport's; fields validate."""
    import merlin.engine.view as engine_view
    from merlin import securedns
    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    # fonts: WOFF before WOFF2 (Qt on Windows cannot read WOFF2)
    view = MerlinView()
    view._font_faces = [{"family": "tabler-icons", "sources": [
        ("https://cdn.example/fonts/tabler-icons.woff2?v3", "woff2"),
        ("https://cdn.example/fonts/tabler-icons.woff?", "woff"),
        ("https://cdn.example/fonts/tabler-icons.ttf?v3", "truetype")]}]
    asked = []
    real_fetch = engine_view.fetch_bytes
    engine_view.fetch_bytes = lambda url, *a, **k: (asked.append(url), (False, url, b"", ""))[1]
    try:
        view._load_fonts()
        wait(app, 0.5)
    finally:
        engine_view.fetch_bytes = real_fetch
    check("a web font is fetched as WOFF before WOFF2, which Qt on Windows cannot read (Ponder's icons)",
          asked[:1] == ["https://cdn.example/fonts/tabler-icons.woff?"], str(asked))
    view.close()
    securedns.install(False)
    first = socket.getaddrinfo("localhost", 80, 0, socket.SOCK_STREAM)[0][0]
    check("localhost is reached on IPv4 first (two seconds a request on Windows otherwise)", first == socket.AF_INET)
    deno = _deno_for_tests()
    if not deno:
        print("  skip  images and viewport: Deno is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-images-")
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 2, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + b"\x80\x80\x80" * 3 for _ in range(2)))) + chunk(b"IEND", b""))
    open(os.path.join(folder, "proxy.png"), "wb").write(png)
    open(os.path.join(folder, "photo.png"), "wb").write(png)
    open(os.path.join(folder, "hydrate.html"), "w").write(
        "<!DOCTYPE html><html><head><title>Hydrate</title></head><body><div id=app><!--[--><p>server made</p><!--]--></div>"
        "<p id=two>one<!---->two</p><p id=marks>waiting</p><script>"
        "const getFirst = Object.getOwnPropertyDescriptor(Node.prototype, 'firstChild').get;"
        "const tpl = document.createElement('template'); tpl.innerHTML = '<!----><p>x</p>';"
        "window.svelteWay = getFirst.call(tpl.content).nodeType + ' ' + getFirst.call(document.body).nodeType;"
        "try { null.cloneNode(); } catch (e) { console.error(e); }"
        "const app = document.getElementById('app'); const nodes = [...app.childNodes];"
        "const first = nodes[0], last = nodes[nodes.length - 1];"
        "document.getElementById('marks').textContent = 'first ' + (first.nodeType === 8 ? first.data : 'none')"
        " + ' | last ' + (last.nodeType === 8 ? last.data : 'none') + ' | texts '"
        " + [...document.getElementById('two').childNodes].filter((n) => n.nodeType === 3).length"
        " + ' | ' + window.svelteWay;</script></body></html>")
    open(os.path.join(folder, "index.html"), "w").write(
        "<!DOCTYPE html><html><head><title>Images</title></head><body><div id=tile></div><p id=out>waiting</p>"
        "<p id=pre>waiting</p><p id=view></p><div id=sized style='width:240px;height:60px'></div><p id=size>waiting</p>"
        "<form><input id=e type=email required></form><script>"
        "const img = document.createElement('img');"
        "img.onerror = () => { img.onerror = null; img.onload = () => { document.getElementById('out').textContent ="
        " 'proxy loaded ' + img.naturalWidth + 'x' + img.naturalHeight; }; img.src = '/proxy.png'; };"
        "img.onload = () => { document.getElementById('out').textContent = 'direct loaded (wrong)'; };"
        "img.src = '/missing.png'; document.getElementById('tile').appendChild(img);"
        "const p = new Image(); p.onload = () => { document.getElementById('pre').textContent ="
        " 'preloaded ' + p.naturalWidth + 'x' + p.naturalHeight; }; p.src = '/photo.png';"
        "const e = document.getElementById('e'); const v = [document.documentElement.clientWidth > 300,"
        " e.checkValidity(), e.validity.valueMissing]; e.value = 'a@b.co'; e.setCustomValidity('taken');"
        " v.push(e.validity.customError, e.validationMessage); e.setCustomValidity(''); v.push(e.validity.valid);"
        "new ResizeObserver((entries) => { document.getElementById('size').textContent = 'observed '"
        " + Math.round(entries[0].contentRect.width) + 'x' + Math.round(entries[0].contentRect.height); })"
        ".observe(document.getElementById('sized'));"
        "document.getElementById('view').textContent = v.join(' ');</script></body></html>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def deno_for_scripts(self):
            return deno

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(800, 500)
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))

        def text(eid):
            found = next((x for x in view._document.root.elements() if x.id == eid), None) if view._document else None
            return " ".join(found.text().split()) if found is not None else ""
        end = time.time() + 12
        while time.time() < end and (text("out") in ("", "waiting") or text("pre") in ("", "waiting")):
            app.processEvents()
            time.sleep(0.02)
        check("an image that fails fires error, and the page's fallback loads with its real size (Ponder's proxy)",
              text("out") == "proxy loaded 3x2", text("out"))
        check("an image preloaded with new Image() reports its real size", text("pre") == "preloaded 3x2", text("pre"))
        check("the root element's client width is the viewport's (Square chose its phone layout on 0), and fields validate",
              text("view") == "true false true true taken true", text("view"))
        end = time.time() + 6
        while time.time() < end and text("size") in ("", "waiting"):
            app.processEvents()
            time.sleep(0.02)
        check("ResizeObserver reports an element's real size (Square sizes its logo so; it had said 0)",
              text("size") == "observed 240x60", text("size"))
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/hydrate.html"))
        end = time.time() + 12
        while time.time() < end and text("marks") in ("", "waiting"):
            app.processEvents()
            time.sleep(0.02)
        check("the page's comment marks reach its scripts in place (Svelte hydrates Hugging Face from them),"
              " and <!----> keeps two texts apart", text("marks").startswith("first [ | last ] | texts 2"), text("marks"))
        check("Node.prototype's firstChild getter, called on any node as Svelte calls it, gives the real child",
              text("marks").endswith("| 8 1"), text("marks"))
        check("an Error a page logs itself comes with where it was thrown",
              any("null.cloneNode" in t or ">>>HERE>>>" in t or "hydrate.html" in t for level, t in view.console_lines
                  if level == "error"), str([t[:80] for level, t in view.console_lines if level == "error"][:3]))
    finally:
        view.close()
        server.shutdown()


def test_box_sizing_and_measuring(app) -> None:
    """border-box for widths and heights, as nearly every site sets it; the
    old -webkit-box display; and a menu's items measured without their
    absolute drop-downs and empty underlines, so they sit in a row."""
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    page = ("<style>body{margin:0} .b{box-sizing:border-box;padding:20px;border:5px solid}"
            "ul{margin:0;padding:0;width:600px} li{display:inline-block;position:relative;margin:0 16px}"
            "li a{display:block;white-space:nowrap;position:relative}"
            "li a:after{content:'';display:block;border-bottom:2px solid;height:0}"
            ".drop{position:absolute;top:100%;left:0;width:300px}</style>"
            "<div class=b id=w style='width:200px'>w</div><div class=b id=h style='height:100px;width:300px'>h</div>"
            "<div class=b id=m style='min-height:100px;width:300px'>m</div><div class=b id=x style='max-width:250px'>x</div>"
            "<div id=c style='width:200px;padding:20px;border:5px solid'>content-box</div>"
            "<a id=pill style='display:block;width:120px;box-sizing:border-box;min-height:40px;line-height:18px;"
            "padding:10px 20px'>Sign in</a><div><span id=clamp style='display:-webkit-box'>clamped</span></div>"
            "<ul>" + "".join(f"<li id=i{n}><a>Item {n}</a><div class=drop>a long drop-down menu of links</div></li>"
                             for n in range(4)) + "</ul>")
    document = parse(page)
    styles = Styler(document).compute()
    out = Layout(document, styles, 1000, viewport_height=600).run()
    box = {e.attrs.get("id"): r for r, e in out.boxes if e.attrs.get("id")}
    check("border-box: a 200px wide box is 200 wide with its padding and border (250 before)",
          round(box["w"].width()) == 200 and round(box["h"].width()) == 300 and round(box["h"].height()) == 100,
          f"{box['w'].width():.0f}, {box['h'].width():.0f}x{box['h'].height():.0f}")
    check("border-box: min-height and max-width hold padding and border too; content-box adds them",
          round(box["m"].height()) == 100 and round(box["x"].width()) == 250 and round(box["c"].width()) == 250,
          f"{box['m'].height():.0f} {box['x'].width():.0f} {box['c'].width():.0f}")
    check("a 40px min-height button of border-box is 40 high (Google's Sign in had been 60)",
          round(box["pill"].height()) == 40, f"{box['pill'].height():.0f}")
    check("display: -webkit-box makes a box (for clamped text)", "clamp" in box and styles[
        next(e for e in document.root.elements() if e.id == "clamp")].get("display") == "block")
    check("menu items with absolute drop-downs and empty underlines sit in a row (Square's had stacked)",
          len({round(box[f"i{n}"].y()) for n in range(4)}) == 1 and box["i0"].width() < 150,
          f"tops {[round(box[f'i{n}'].y()) for n in range(4)]}, first {box['i0'].width():.0f} wide")


def test_images_as_blocks_and_fitting(app) -> None:
    """Images laid out as blocks or flex items are drawn, at their own size or
    in proportion; object-fit and object-position; and a flex item of
    border-box keeps room for its padding (Merlin's new-tab tiles)."""
    from PyQt6.QtGui import QImage

    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout
    from merlin.engine.paint import _fitted

    picture = QImage(200, 100, QImage.Format.Format_RGB32)

    def images_in(items):
        for item in items:
            if isinstance(item, tuple) and item and item[0] == "image":
                yield item
            elif isinstance(item, tuple) and item and item[0] == "group":
                yield from images_in(item[1])

    def drawn(body):
        document = parse("<style>body{margin:0}</style>" + body)
        styles = Styler(document).compute()
        out = Layout(document, styles, 800, images={"p.png": picture}, viewport_height=600).run()
        found = list(images_in(out.items))
        if not found:
            return None
        item = found[0]
        target = _fitted(item[1], picture, item[4] if len(item) > 4 else None)
        return (round(item[1].width()), round(item[1].height()), round(target.width()), round(target.height()),
                round(target.x() - item[1].x()), round(target.y() - item[1].y()))
    check("an image of display: block is drawn, at its own size (it had not been drawn at all)",
          drawn("<img src=p.png style='display:block'>") == (200, 100, 200, 100, 0, 0),
          str(drawn("<img src=p.png style='display:block'>")))
    check("an image that is a flex item is drawn", drawn(
        "<div style='display:flex'><img src=p.png style='width:100px;height:100px'></div>") is not None)
    check("a block image given a width only keeps its proportions",
          drawn("<img src=p.png style='display:block;width:100px'>")[:2] == (100, 50))
    check("in proportion to its content's width, with border-box padding",
          drawn("<img src=p.png style='display:block;box-sizing:border-box;width:120px;padding:10px'>")[:2] == (100, 50))
    check("object-fit: cover fills its box and is cropped, centred; contain fits whole",
          drawn("<img src=p.png style='display:block;width:100px;height:100px;object-fit:cover'>") == (100, 100, 200, 100, -50, 0)
          and drawn("<img src=p.png style='display:block;width:100px;height:100px;object-fit:contain'>") == (100, 100, 100, 50, 0, 25),
          str(drawn("<img src=p.png style='display:block;width:100px;height:100px;object-fit:cover'>")))
    check("object-position places it: left top",
          drawn("<img src=p.png style='display:block;width:100px;height:100px;object-fit:cover;object-position:left top'>")[4:] == (0, 0))
    document = parse("<style>body{margin:0} .tiles{display:flex;gap:8px} .tiles a{box-sizing:border-box;padding:10px 16px;"
                     "white-space:nowrap}</style><div class=tiles><a id=t>Convert Files</a></div>")
    styles = Styler(document).compute()
    out = Layout(document, styles, 800, viewport_height=600).run()
    tile = next(r for r, e in out.boxes if e.id == "t")
    texts = [item for item in out.items if item and item[0] == "text"]
    check("a flex item of border-box is wide enough for its text and its padding (new-tab tiles)",
          texts and tile.width() >= 32 + max(t[1] + 0 for t in texts) * 0 + 60, f"{tile.width():.0f} wide")


def test_backgrounds_blocks_in_inlines_and_columns(app) -> None:
    """Background pictures (size, position, repeat, under an overlay), a
    stylesheet's urls made whole, a block inside an inline element, and a
    column flex item never smaller than its content."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter

    from merlin.engine import paint as painting
    from merlin.engine.css import Styler, absolute_urls
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    picture = QImage(200, 100, QImage.Format.Format_RGB32)
    picture.fill(QColor(255, 0, 0))

    def drawn(css):
        document = parse(f"<style>body{{margin:0}} div{{width:400px;height:100px}}</style><div style='{css}'></div>")
        styles = Styler(document).compute()
        out = Layout(document, styles, 800, images={"p.png": picture}, viewport_height=300).run()
        image = QImage(800, 300, QImage.Format.Format_RGB32)
        image.fill(QColor(255, 255, 255))
        painter = QPainter(image)
        painting.paint(painter, out, QRectF(0, 0, 800, 300), images={"p.png": picture})
        painter.end()
        red = [image.pixelColor(x, 50).red() == 255 and image.pixelColor(x, 50).green() == 0 for x in range(400)]
        return red, image.pixelColor(200, 50)
    red, _middle = drawn("background: url(p.png) center / cover no-repeat")
    check("a background picture is drawn (none had been): cover fills its box", all(red))
    red, _middle = drawn("background: url(p.png) center / contain no-repeat")
    check("contain fits it whole, centred", red.index(True) == 100 and not red[50] and not red[350])
    red, _middle = drawn("background-image: url(p.png); background-size: 50px 25px")
    check("a background picture repeats by default", all(red))
    _red, middle = drawn("--o: linear-gradient(rgba(0,0,0,.6), rgba(0,0,0,.6)); --s: url(p.png);"
                         " background-image: var(--o), var(--s); background-size: cover")
    check("an overlay over a picture, both from var() (AlterniTech's hero): the picture dimmed beneath",
          (middle.red(), middle.green(), middle.blue()) == (102, 0, 0), str((middle.red(), middle.green(), middle.blue())))
    check("a stylesheet's url() is made whole against its own address; data: is left",
          absolute_urls(".h{background:url('../img/a.jpg')} .d{background:url(data:x)}", "https://s.test/css/m.css")
          == ".h{background:url('https://s.test/img/a.jpg')} .d{background:url(data:x)}")
    for label, markup in (("a span", "<span><header id=h style='display:block;background:black;height:50px'>x</header></span>"),
                          ("a custom element (GitHub's react-partial)",
                           "<react-partial><div><header id=h style='display:flex;height:50px'><a>x</a></header></div></react-partial>")):
        document = parse("<style>body{margin:0}</style>" + markup)
        styles = Styler(document).compute()
        out = Layout(document, styles, 800, viewport_height=400).run()
        header = [r for r, e in out.boxes if e.id == "h"]
        check(f"a block inside {label} is laid out as a block (GitHub's header had no box)",
              header and round(header[0].width()) == 800 and round(header[0].height()) == 50)
    document = parse("<style>body{margin:0} .main{display:flex;flex-direction:column;min-height:300px}"
                     " .col{height:100%;flex-grow:1} .tall{height:900px}</style>"
                     "<div class=main><div class=col id=col><div class=tall></div></div></div><footer id=f>end</footer>")
    styles = Styler(document).compute()
    out = Layout(document, styles, 800, viewport_height=300).run()
    boxes = {e.attrs.get("id"): r for r, e in out.boxes if e.attrs.get("id")}
    check("a column flex item at height: 100% grows to its content; what follows comes after it (AlterniTech's products)",
          boxes["col"].height() >= 900 and boxes["f"].y() >= 900, f"column {boxes['col'].height():.0f}, footer at {boxes['f'].y():.0f}")


def test_quirks_mode_and_center(app) -> None:
    """A page with no doctype is in quirks mode: its tables start their text
    afresh (Hacker News, centred by <center>, is left-aligned in its table);
    <center> centres the boxes in it; the mode is kept for the page's scripts."""
    from merlin.engine.css import Styler
    from merlin.engine.dom import to_html
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    page = ("<html><body style='margin:0'><center><table id=main width='50%'><tr><td id=cell>story</td></tr></table>"
            "</center></body></html>")
    check("a page with no doctype is in quirks mode; <!DOCTYPE html> and HTML 4.01 Strict are not",
          parse(page).quirks and not parse("<!DOCTYPE html>" + page).quirks
          and not parse('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd">'
                        + page).quirks)
    for doctype, wanted in (("", "left"), ("<!DOCTYPE html>", "center")):
        document = parse(doctype + page)
        styles = Styler(document).compute()
        cell = next(e for e in document.root.elements() if e.id == "cell")
        check(f"in {'quirks' if not doctype else 'standards'} mode a table's text inside <center> is {wanted}"
              " (Hacker News had been centred)", styles[cell].get("text-align") == wanted,
              str(styles[cell].get("text-align")))
    document = parse(page)
    styles = Styler(document).compute()
    out = Layout(document, styles, 800, viewport_height=600).run()
    table = next(r for r, e in out.boxes if e.id == "main")
    check("<center> centres the 50% table in it, as browsers do", round(table.x()) == 200 and round(table.width()) == 400,
          f"x {table.x():.0f} w {table.width():.0f}")
    check("a quirks page goes to its scripts without a doctype, so it stays in quirks mode through them",
          not to_html(parse(page)).lower().startswith("<!doctype") and
          to_html(parse("<!DOCTYPE html>" + page)).startswith("<!DOCTYPE html>"))


def test_button_groups_measured(app) -> None:
    """A group of buttons sized to their content: a percentage width counts as
    auto when measured, an unpainted box's padding counts, and text given
    exactly its own width stays on one line (AlterniTech's two buttons)."""
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    page = ("<style>body{margin:0;font-size:16px} .group{display:inline-flex;gap:8px} .wrap{flex:1 1 auto}"
            ".btn{display:inline-flex;width:100%;box-sizing:border-box;padding:0 24px;justify-content:center}"
            ".t{display:block;padding-left:.16px} p{margin:0}</style><div class=group id=g>"
            "<div class=wrap><a class=btn id=b1><span class=t><p>Free Software</p></span></a></div>"
            "<div class=wrap><a class=btn id=b2><span class=t><p>All Products</p></span></a></div></div>")
    document = parse(page)
    styles = Styler(document).compute()
    out = Layout(document, styles, 1400, viewport_height=600).run()
    lines = [item[3] for item in out.items if item and item[0] == "text"]
    boxes = {e.id: r for r, e in out.boxes if e.id}
    check("button labels each stay on one line (they had wrapped: Free / Software, All / Products)",
          lines == ["Free Software", "All Products"], str(lines))
    check("a group of width: 100% buttons is as wide as their content, not the room (it had measured 100,000)",
          boxes["g"].width() < 400, f"{boxes['g'].width():.0f}")
    check("each button's padding on both sides counts, painted or not, and the gap between them",
          boxes["b1"].width() > 140 and boxes["b2"].x() - boxes["b1"].right() >= 7.5,
          f"{boxes['b1'].width():.0f} wide, gap {boxes['b2'].x() - boxes['b1'].right():.0f}")


def test_clicks_land_where_browsers_land(app) -> None:
    """A click on an inline element reaches its own onclick, through a fixed
    layer of pointer-events: none behind the page (Ponder's wallpaper, its
    pills and its Toggle Search Functions); and an SVG of width="1em" is its
    text's size (Hugging Face's header icons)."""
    from merlin.engine import MerlinView
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout
    from merlin.engine.script import LocalStorage

    document = parse("<style>body{margin:0;font-size:16px}</style><a>Models <svg id=i width='1em' height='1em'"
                     " viewBox='0 0 24 24'><rect width='24' height='24'/></svg></a>"
                     "<svg id=r width='2rem' height='1.5em' viewBox='0 0 10 10'></svg>")
    styles = Styler(document).compute()
    out = Layout(document, styles, 800, viewport_height=600).run()
    sizes = {}
    for item in out.items:
        if item and item[0] == "svg":
            sizes.setdefault("svg", []).append((round(item[1].width()), round(item[1].height())))
    check("an SVG of width=\"1em\" is its text's size, and rem and em read too (they had been 300 square)",
          sizes.get("svg", [])[:2] == [(16, 16), (32, 24)], str(sizes.get("svg")))
    deno = _deno_for_tests()
    if not deno:
        print("  skip  clicks: Deno is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-clicks-")
    open(os.path.join(folder, "index.html"), "w").write(
        "<!DOCTYPE html><html><head><title>Clicks</title><style>body{margin:0}"
        "body::before{content:'';position:fixed;inset:0;background:rgba(0,0,0,.1);pointer-events:none;z-index:-1}"
        "</style></head><body><div id=toggle onclick=\"say('panel')\" style='padding:8px'>Toggle Search Functions:</div>"
        "<div style='padding:8px'><span onclick=\"say('web')\">Web</span> <span onclick=\"say('wiki')\">Wiki</span></div>"
        "<p id=out>none</p><script>function say(w) { document.getElementById('out').textContent = 'heard ' + w; }"
        "</script></body></html>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def deno_for_scripts(self):
            return deno

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(700, 400)
    view.show()
    from PyQt6.QtTest import QTest
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))
        wait(app, 3.0)

        def said():
            found = next((x for x in view._document.root.elements() if x.id == "out"), None)
            return " ".join(found.text().split()) if found is not None else ""

        def click_on(word):
            for item in view._display.items:
                if item and item[0] == "text" and item[3].strip() == word:
                    QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                                     pos=QPoint(int(item[1]) + 4, int(item[2]) - 4 - int(view._scroll)))
                    return
        for word, wanted in (("Wiki", "heard wiki"), ("Toggle Search Functions:", "heard panel")):
            click_on(word)
            end = time.time() + 5
            while time.time() < end and said() != wanted:
                app.processEvents()
                time.sleep(0.02)
            check(f"a click on {word!r} reaches its own onclick, through a fixed layer of pointer-events: none"
                  " (Ponder's)", said() == wanted, said())
    finally:
        view.close()
        server.shutdown()


def test_chrome_connections(app) -> None:
    """Merlin Engine's connections made as Chrome's (curl_cffi): Chrome's
    handshake (GREASE, HTTP/2 offered), cookies kept through redirects, gzip
    unpacked once, urllib's errors as before, and scripts told the user agent
    the connections send."""
    import gzip as _gzip
    import http.cookiejar
    import socket as _socket
    import struct
    import urllib.error

    from merlin.engine import chromelike
    from merlin.engine.view import connection_failure

    if not chromelike.AVAILABLE:
        print("  skip  Chrome's connections: curl_cffi is not installed")
        return

    class Server(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, body=b"", headers=()):
            self.send_response(code)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/gz":
                self._send(200, _gzip.compress(b"once"), [("Content-Encoding", "gzip")])
            elif self.path == "/login":
                self._send(302, b"", [("Location", "/home"), ("Set-Cookie", "session=abc; Path=/")])
            elif self.path == "/home":
                self._send(200, ("cookie " + self.headers.get("Cookie", "none")).encode())
            elif self.path == "/done":
                self._send(200, b"after")
            else:
                self._send(404, b"our own 404")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._send(303, b"", [("Location", "/done")])

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        jar = http.cookiejar.CookieJar()
        opener = chromelike.ChromeOpener(jar)
        check("a cookie set in a redirect is kept and sent on (logins, Google)",
              opener.open(base + "/login", timeout=10).read() == b"cookie session=abc")
        check("a gzip body is unpacked once", opener.open(base + "/gz", timeout=10).read() == b"once")
        posted = opener.open(urllib.request.Request(base + "/f", data=b"q=1", method="POST"), timeout=10)
        check("a POST answered 303 is followed with a GET", posted.read() == b"after")
        try:
            opener.open(base + "/missing", timeout=10)
            check("a 404 comes back as urllib's HTTPError, with the server's page", False)
        except urllib.error.HTTPError as error:
            check("a 404 comes back as urllib's HTTPError, with the server's page",
                  error.code == 404 and error.read() == b"our own 404")
        try:
            opener.open("http://127.0.0.1:1/", timeout=5)
        except urllib.error.URLError as error:
            check("a refused connection is one Merlin's retry knows", connection_failure(str(error)), str(error)[:80])
        agent = chromelike.user_agent()
        from merlin.engine.view import _scripts_user_agent
        check("the page's scripts are told the user agent the connections send (one browser, not two)",
              agent.startswith("Mozilla/5.0") and "Chrome/" in agent and _scripts_user_agent("x") == agent, agent)
    finally:
        server.shutdown()
    # the handshake, as a site sees it first
    listener = _socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    hello = []

    def take():
        connection, _ = listener.accept()
        connection.settimeout(3)
        try:
            hello.append(connection.recv(16384))
        finally:
            connection.close()
    taker = threading.Thread(target=take, daemon=True)
    taker.start()
    try:
        chromelike.ChromeOpener(lenient="any").open(f"https://localhost:{listener.getsockname()[1]}/", timeout=3)
    except Exception:                                       # noqa: BLE001
        pass
    taker.join(3)
    listener.close()
    data = hello[0] if hello else b""
    grease = any((struct.unpack(">H", data[i:i + 2])[0] & 0x0f0f) == 0x0a0a and data[i] == data[i + 1]
                 for i in range(5, len(data) - 1))
    check("the TLS handshake is Chrome's: GREASE in it, HTTP/2 offered (Python's had neither)",
          data[:1] == b"\x16" and grease and b"\x02h2" in data, f"{len(data)} bytes")


def test_page_background_pictures_and_safe_painting(app) -> None:
    """A picture on <body>, under a gradient, is drawn over the whole page (it
    had been handed to the gradient code, and 1.8.19 could not start on Linux);
    and a failure while painting is logged, never a crash."""
    from PyQt6.QtGui import QColor, QImage

    import merlin.engine.view as engine_view
    from merlin.engine import MerlinView

    view = MerlinView()
    view.resize(400, 300)
    view.show()
    view.setHtml("<html><body style=\"margin:0;background:linear-gradient(rgba(0,0,0,.5),rgba(0,0,0,.5)),"
                 " url(p.png) center / cover no-repeat\"><p>x</p></body></html>", QUrl("about:blank"))
    wait(app, 1.0)
    check("a picture on <body> is a page-background layer with its size, position and repeat",
          [layer[0] for layer in view._display.canvas_gradients] == ["linear", "url"]
          and view._display.canvas_gradients[1][2:] == ("cover", "center", "no-repeat"),
          str(view._display.canvas_gradients))
    picture = QImage(20, 20, QImage.Format.Format_RGB32)
    picture.fill(QColor(255, 0, 0))
    view._images["p.png"] = picture
    colour = view.grab().toImage().pixelColor(200, 250)
    check("and is drawn, dimmed by the gradient over it", abs(colour.red() - 128) < 6 and colour.green() < 6,
          str((colour.red(), colour.green(), colour.blue())))
    real = engine_view.MerlinView._paint_into
    engine_view.MerlinView._paint_into = lambda self, painter: 1 / 0
    try:
        view.grab()
        check("a failure while painting a page is caught, not a crash", True)
    except Exception as exc:                                # noqa: BLE001
        check("a failure while painting a page is caught, not a crash", False, str(exc))
    finally:
        engine_view.MerlinView._paint_into = real
        view.close()


def test_modern_colours() -> None:
    """CSS Color 4 and 5, as GitHub uses them: unread, faint lines came out solid."""
    from merlin.engine.css import parse_colour

    for text, wanted in (("color-mix(in srgb, #fff 10%, transparent)", (255, 255, 255, 26)),
                         ("color-mix(in srgb, red, blue)", (128, 0, 128, 255)),
                         ("color-mix(in srgb, red 30%, blue 30%)", (128, 0, 128, 153)),
                         ("color-mix(in oklab, white, black)", (99, 99, 99, 255)),
                         ("oklab(0.5 0 0)", (99, 99, 99, 255)),
                         ("hwb(120 0% 0%)", (0, 255, 0, 255)),
                         ("color(srgb 1 0 0 / 0.5)", (255, 0, 0, 128)),
                         ("light-dark(#fff, #000)", (255, 255, 255, 255)),
                         ("rgb(from #ff0000 r g b / 50%)", (255, 0, 0, 128))):
        check(f"{text} is {wanted}", parse_colour(text) == wanted, str(parse_colour(text)))


def test_server_pages_and_http_fallback(app) -> None:
    """A server's own error page is shown, as in a browser; and an address
    typed with no scheme, whose site does not answer on https, opens over http."""
    from PyQt6.QtWidgets import QLabel

    from merlin.engine import MerlinView
    import merlin.engine.view as engine_view

    folder = tempfile.mkdtemp(prefix="merlin-pages-")
    open(os.path.join(folder, "index.html"), "w").write("<title>Plain</title><p>over http</p>")

    class Site(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

        def send_error(self, code, message=None, explain=None):
            body = b"<title>Our own 404</title><p>Nothing here, from the site itself</p>"
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    said = []

    class Host:
        settings = {}
        status_label = QLabel()

        def javascript_allowed(self, site):
            return False

    host = Host()
    real = host.status_label.setText
    host.status_label.setText = lambda text: (said.append(text), real(text))
    view = MerlinView()
    view._host = host
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/missing.html"))
        wait(app, 1.5)
        check("a server's own error page is shown, as browsers show it", view.title() == "Our own 404",
              view.title())
    finally:
        server.shutdown()
    try:
        plain = http.server.ThreadingHTTPServer(("127.0.0.1", 80), Site)
    except OSError:
        print("  skip  http fallback: port 80 is not free here")
        view.close()
        return
    threading.Thread(target=plain.serve_forever, daemon=True).start()
    pauses = engine_view.RETRY_PAUSES
    engine_view.RETRY_PAUSES = (0.1, 0.1)
    try:
        engine_view.HTTP_ONLY_HOSTS.clear()
        view.setProperty("http_fallback", "127.0.0.1")
        view.setUrl(QUrl("https://127.0.0.1/index.html"))
        wait(app, 3.0)
        check("from the address bar, a site not answering on https opens over http, saying so",
              view.title() == "Plain" and any(t.startswith("Not secure") for t in said), view.title())
        view.setUrl(QUrl("https://127.0.0.1/index.html"))
        wait(app, 3.0)
        check("found to answer only on http, the site is remembered: a link to it opens over http",
              view.title() == "Plain", view.title())
        engine_view.HTTP_ONLY_HOSTS.clear()
        view.setUrl(QUrl("https://127.0.0.1/index.html"))
        wait(app, 3.0)
        check("but a link to https:// for a site not known to need http is never opened over http",
              view.title() != "Plain", view.title())
    finally:
        engine_view.RETRY_PAUSES = pauses
        plain.shutdown()
        view.close()


def test_connection_retry(app) -> None:
    """A connection that fails is tried again before the page says it could
    not be opened; a server's own answer, such as 404, is not."""
    from PyQt6.QtWidgets import QLabel

    from merlin.engine import MerlinView

    folder = tempfile.mkdtemp(prefix="merlin-retry-")
    open(os.path.join(folder, "index.html"), "w").write("<title>Late</title><p>here on a later try</p>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    probe = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    port = probe.server_address[1]
    probe.server_close()                      # a port that will be free, not yet listening
    servers = []

    def start_late():
        time.sleep(1.5)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Quiet)
        servers.append(server)
        server.serve_forever()

    threading.Thread(target=start_late, daemon=True).start()
    said = []

    class Host:
        settings = {}
        status_label = QLabel()

        def javascript_allowed(self, site):
            return False

    host = Host()
    real = host.status_label.setText
    host.status_label.setText = lambda text: (said.append(text), real(text))
    view = MerlinView()
    view._host = host
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{port}/index.html"))
        wait(app, 6.0)
        check("a refused connection is retried, saying so, and the page then loads",
              any(t.startswith("Retrying connection") for t in said) and view.title() == "Late",
              f"{[t for t in said if 'Retrying' in t]} | {view.title()!r}")
        said.clear()
        view.setUrl(QUrl(f"http://127.0.0.1:{port}/missing.html"))
        wait(app, 2.0)
        check("a 404 is not retried", not any("Retrying" in t for t in said), str(said))
    finally:
        view.close()
        for server in servers:
            server.shutdown()


def test_view(app) -> None:
    from merlin.engine import MerlinView

    folder = tempfile.mkdtemp(prefix="merlin-engine-")
    with open(os.path.join(folder, "one.html"), "w") as handle:
        handle.write("<title>One</title><body><p><a href='#far'>down</a> "
                     "<a href='two.html'>two</a></p><div style='height:2000px'></div>"
                     "<h2 id=far>Far</h2></body>")
    with open(os.path.join(folder, "two.html"), "w") as handle:
        handle.write("<title>Two</title><p>second</p>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    view = MerlinView()
    view.resize(700, 500)
    view.show()
    heard = {"titles": [], "finished": []}
    view.titleChanged.connect(heard["titles"].append)
    view.loadFinished.connect(heard["finished"].append)

    def click(href):
        for rect, target in view._display.links:
            if target == href:
                centre = rect.center()
                from PyQt6.QtTest import QTest

                QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                                 pos=QPoint(int(centre.x()), int(centre.y() - view._scroll)))
                return True
        return False

    try:
        view.setUrl(QUrl(base + "one.html"))
        wait(app, 1.5)
        check("a page loads over HTTP, with the signals Merlin listens to",
              view.title() == "One" and heard["finished"] == [True], str(heard))
        click("#far")
        wait(app, 0.3)
        check("a #fragment link scrolls to its element",
              view._scroll > 1500, f"{view._scroll:.0f}")
        view.scrollbar.setValue(0)
        click("two.html")
        wait(app, 1.5)
        check("clicking a link navigates", view.title() == "Two")
        view.back()
        wait(app, 1.5)
        check("back returns, and forward becomes possible",
              view.title() == "One" and view.history().canGoForward())
        view.setUrl(QUrl(base + "missing.html"))
        wait(app, 1.5)
        # since 1.8.4 the server's own error page is shown, as browsers show it
        check("a missing page shows the server's error page instead of hanging",
              bool(heard["finished"]) and view.title() == "Error response", view.title())
    finally:
        server.shutdown()
        view.close()


def main() -> int:
    app = QApplication(sys.argv[:1])
    for test in (test_no_chromium, test_without_html_parser, test_parsing, test_cascade, test_layout,
                 test_body_and_markers, test_tables, test_flexbox,
                 test_floats_and_positioning, test_grid_svg_inline_block,
                 test_real_world_css, test_files_ftp_smb, test_no_freeze_on_real_grids,
                 test_deep_nesting_stays_quick, test_gradients, test_worker_processes,
                 test_all_and_script_pages, test_certificate_leniency, test_gradients_dithered,
                 test_github_header_bugs, test_finer_rule_index):
        print(test.__name__)
        test()
    print("test_images")
    test_images(app)
    print("test_clipping_opacity_details_svg")
    test_clipping_opacity_details_svg(app)
    print("test_browser_headers_and_compression")
    test_browser_headers_and_compression(app)
    print("test_sticky_and_icons")
    test_sticky_and_icons(app)
    print("test_images_do_not_relayout")
    test_images_do_not_relayout(app)
    print("test_stacking_order")
    test_stacking_order(app)
    print("test_big_page_laid_out_in_background")
    test_big_page_laid_out_in_background(app)
    print("test_viewport_units_follow_the_window")
    test_viewport_units_follow_the_window(app)
    print("test_web_fonts")
    test_web_fonts(app)
    print("test_fixed_in_stacking_order")
    test_fixed_in_stacking_order(app)
    print("test_overlays_and_document_order")
    test_overlays_and_document_order(app)
    print("test_javascript")
    test_javascript(app)
    print("test_connection_retry")
    test_connection_retry(app)
    print("test_modern_colours")
    test_modern_colours()
    print("test_noscript_without_scripts_and_fonts")
    test_noscript_without_scripts_and_fonts(app)
    print("test_github_profile_layout")
    test_github_profile_layout(app)
    print("test_window_events_enter_dns_and_saving")
    test_window_events_enter_dns_and_saving(app)
    print("test_pseudo_elements")
    test_pseudo_elements(app)
    print("test_escaped_selectors_grids_and_files")
    test_escaped_selectors_grids_and_files(app)
    print("test_loads_and_walkers")
    test_loads_and_walkers(app)
    print("test_images_fonts_and_viewport")
    test_images_fonts_and_viewport(app)
    print("test_box_sizing_and_measuring")
    test_box_sizing_and_measuring(app)
    print("test_images_as_blocks_and_fitting")
    test_images_as_blocks_and_fitting(app)
    print("test_backgrounds_blocks_in_inlines_and_columns")
    test_backgrounds_blocks_in_inlines_and_columns(app)
    print("test_quirks_mode_and_center")
    test_quirks_mode_and_center(app)
    print("test_button_groups_measured")
    test_button_groups_measured(app)
    print("test_clicks_land_where_browsers_land")
    test_clicks_land_where_browsers_land(app)
    print("test_page_background_pictures_and_safe_painting")
    test_page_background_pictures_and_safe_painting(app)
    print("test_chrome_connections")
    test_chrome_connections(app)
    print("test_animations_and_3d")
    test_animations_and_3d(app)
    print("test_server_pages_and_http_fallback")
    test_server_pages_and_http_fallback(app)
    print("test_forms")
    test_forms(app)
    print("test_view")
    test_view(app)
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: " + "; ".join(FAILED))
        return 1
    print("\nall MerlinEngine checks OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
