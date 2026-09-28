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
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QPoint, Qt, QUrl                     # noqa: E402
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
    check("a gradient background falls back to its first colour",
          expand_shorthand("background", "linear-gradient(90deg,#123456,#fff)")
          == [("background-color", "#123456")])


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
          sizes == [(240, 160), (120, 80), (100, 67)], str(sizes))
    check("a blocked or missing image takes no room", (50, 50) not in sizes)


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
    _flex_check("and is not part of the scrolling page", boxes(out).get(R), None)



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
        check("a failed load shows an error page instead of hanging",
              heard["finished"][-1] is False and "could not open" in view.title().lower())
    finally:
        server.shutdown()
        view.close()


def main() -> int:
    app = QApplication(sys.argv[:1])
    for test in (test_no_chromium, test_without_html_parser, test_parsing, test_cascade, test_layout,
                 test_body_and_markers, test_tables, test_flexbox,
                 test_floats_and_positioning, test_grid_svg_inline_block,
                 test_real_world_css, test_files_ftp_smb):
        print(test.__name__)
        test()
    print("test_images")
    test_images(app)
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
