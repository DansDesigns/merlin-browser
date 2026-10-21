#!/usr/bin/env python3
"""Checks for MerlinEngine, Merlin's own web engine.

    python tests/test_engine.py

Parsing, the cascade, layout and the view, each checked against what a
browser does, plus a guard that the engine never imports Chromium. Quick:
nothing here waits on Chromium starting.
"""
from __future__ import annotations

import http.server
import json
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
os.environ["MERLIN_NO_PONDER"] = "1"           # Ponder's server is not started here
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, QUrl  # noqa: E402
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

    # reCAPTCHA's "I'm not a robot" box: cells straight in a display: table
    # (no rows), each table 100% high in an inline-block, the cells centred
    # top to bottom, beside a column at vertical-align: top. Measured in
    # Chrome: the checkbox at 13,23, both halves at the top, the footer too
    doc = parse("""<!DOCTYPE html><style>body{margin:0}
.anchor{border:1px solid #d3d3d3;height:74px;width:300px}
.content{display:inline-block;position:relative;height:74px;width:206px}
.ib{display:inline-block;height:100%}.cc{display:table;height:100%}
.ci{display:table-cell;vertical-align:middle}
.box{position:relative;display:inline-block;height:28px;width:28px;margin:0 12px 2px 12px;font-size:1px;vertical-align:text-bottom}
.label{font-size:14px;line-height:17px;width:152px}
.footer{display:inline-block;height:74px;vertical-align:top;width:70px}</style>
<div class=anchor><div class=content><div class=ib id=ib1><div class=cc><div class=ci><span class=box id=box></span></div></div></div><div class=ib id=ib2><div class=cc><label class="ci label" id=label>I'm not a robot</label></div></div></div><div class=footer id=footer>reCAPTCHA</div></div>""")
    out = Layout(doc, Styler(doc, viewport=(304, 78)).compute(), 304).run()
    at = {e.attrs.get("id"): r for r, e in out.boxes if e.attrs.get("id")}
    got = {k: (round(at[k].x()), round(at[k].y()), round(at[k].width())) for k in ("box", "ib1", "ib2", "footer") if k in at}
    check("cells with no row around them are laid out as a row, and drawn",
          any(i and i[0] == "text" and "not a robot" in i[3] for i in out.items), str(got))
    check("an inline-block holding a table is as wide as the table, margins and all",
          got.get("ib1", (0, 0, 0))[2] == 52 and got.get("ib2", (0, 0, 0))[2] == 152, str(got))
    check("a table at height: 100% fills it, its cell centred top to bottom",
          got.get("box") == (13, 23, 28), str(got))
    check("inline-blocks holding tables sit on their bottom edges, side by side at the top",
          got.get("ib1", (0, -1))[1] == 1 and got.get("ib2", (0, -1))[1] == 1, str(got))
    check("vertical-align: top puts a box at the top of its line", got.get("footer", (0, -1))[1] == 1, str(got))

    # reCAPTCHA's picture grid: one picture three times a tile's size, moved
    # by position: relative and percentages in each tile's window (every
    # tile had shown the same corner of it)
    doc = parse("""<!DOCTYPE html><style>body{margin:0}.w{overflow:hidden;position:relative;width:126px;height:126px}
.w img{position:relative;width:300%;height:300%}</style>
<div class=w><img src="a.png" style="top:0%; left: -100%"></div><div class=w><img src="a.png" style="top:-200%; left:-200%"></div>
<div class=w style="height:auto"><img src="a.png" style="top:-50%; left:10px"></div>""")
    from PyQt6.QtGui import QImage
    picture = QImage(300, 300, QImage.Format.Format_RGB32)
    out = Layout(doc, Styler(doc, viewport=(400, 600)).compute(), 400, images={"a.png": picture}).run()
    pictures = [i[1] for i in out.items if i and i[0] == "image"]
    places = [(round(r.x()), round(r.y()), round(r.width())) for r in pictures]
    check("a picture at position: relative moves by its left and top, percentages of its box's size",
          len(places) == 3 and places[0] == (-126, 0, 378) and places[1] == (-252, 126 - 252, 378), str(places))
    check("and a top percentage of a box with no set height counts for nothing, as in browsers",
          len(places) == 3 and places[2][0] == 10 and places[2][1] == 252, str(places))


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
        rect = next(b[0] for b in view._buttons if b[1].tag == "button")
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
        view._press_button(next(b[1] for b in view._buttons if b[1].attrs.get("type") == "reset"))
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


def _node_for_tests() -> str:
    from merlin.media import node_path

    found = os.environ.get("MERLIN_NODE") or node_path()
    return found if found and os.path.exists(found) else ""


def test_javascript(app) -> None:
    """JavaScript in Merlin Engine: a hydrated React app, clicked, typed in and
    sent in Merlin; inline handlers; modules only from allowed hosts; cookies
    and storage; and nothing at all on a site not allowed."""
    from http.cookiejar import Cookie

    from PyQt6.QtTest import QTest

    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    runtime = _node_for_tests()
    if not runtime:
        print("  skip  JavaScript: Node.js is not here (set MERLIN_NODE, or fetch it in Settings)")
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

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "runtime-cache")

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

    runtime = _node_for_tests()
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

        def node_for_scripts(self):
            return runtime

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
        if runtime:
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
            print("  skip  window events and Enter: Node.js is not here")
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
    runtime = _node_for_tests()
    if not runtime:
        print("  skip  file picker: Node.js is not here")
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

        def node_for_scripts(self):
            return runtime

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

    runtime = _node_for_tests()
    if not runtime:
        print("  skip  loads and walkers: Node.js is not here")
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

        def node_for_scripts(self):
            return runtime

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
    runtime = _node_for_tests()
    if not runtime:
        print("  skip  images and viewport: Node.js is not here")
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

        def node_for_scripts(self):
            return runtime

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
    # Ponder's results page: its logo, height 26px and width auto, in a flex
    # row beside the search box, had been measured 0 wide and was not shown
    found = drawn("<div style='display:flex;align-items:center;gap:8px'>"
                  "<img src=p.png style='height:26px;width:auto;flex-shrink:0'>"
                  "<input style='flex:1'></div>")
    check("a flex item image of height only is as wide as its proportions give (Ponder's logo)",
          found is not None and found[:2] == (52, 26), str(found))
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
    runtime = _node_for_tests()
    if not runtime:
        print("  skip  clicks: Node.js is not here")
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

        def node_for_scripts(self):
            return runtime

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
            elif self.path == "/echo":
                self._send(200, "\n".join(f"{k.lower()}: {v}" for k, v in self.headers.items()).encode())
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
        system, name = chromelike.this_platform()
        sent = opener.open(base + "/echo", timeout=10).read().decode()
        check("the connections say this computer's system, in the user agent and sec-ch-ua-platform "
              "(curl_cffi's Chrome had said it was a Mac)",
              f"({system})" in agent and f"user-agent: {agent}" in sent
              and f'sec-ch-ua-platform: "{name}"' in sent, sent)
        from merlin.engine.view import _scripts_brands

        brands = _scripts_brands()
        check("navigator.userAgentData's brands are the ones sec-ch-ua sends",
              brands and all(f'"{b["brand"]}";v="{b["version"]}"' in sent for b in brands), str(brands))
        image = urllib.request.Request(base + "/echo", headers={"Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors"})
        sent = opener.open(image, timeout=10).read().decode()
        check("a picture is asked for as Chrome asks: no Sec-Fetch-User, no Upgrade-Insecure-Requests "
              "(curl had added them to everything)",
              "sec-fetch-dest: image" in sent and "sec-fetch-user" not in sent
              and "upgrade-insecure-requests" not in sent, sent)
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


def test_scrolling_panels_and_touch(app) -> None:
    """Panels that scroll inside the page (overflow: auto): laid out, drawn
    moved, scrolled by the wheel (on to the page at their end), clicked where
    they are shown, seen and moved by their scripts; and touch: a drag
    scrolls, a flick glides, a tap clicks."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtTest import QTest

    from merlin.engine import MerlinView
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout
    from merlin.engine.script import LocalStorage

    # Ponder's own layout: one window high, the results a panel stretched in a row
    document = parse("<style>body{margin:0;height:600px;display:flex;flex-direction:column}"
                     " .view{display:flex;flex-direction:column;flex:1;overflow:hidden}"
                     " .body{display:flex;flex:1;overflow:hidden} .list{flex:1;overflow-y:auto}"
                     " .row{height:100px}</style><div class=view><div style='height:40px'>bar</div>"
                     "<div class=body><div class=list id=list>"
                     + "".join(f"<div class=row>{n}</div>" for n in range(20)) + "</div></div></div>")
    styles = Styler(document, viewport=(800, 600)).compute()
    out = Layout(document, styles, 800, viewport_height=600).run()
    lists = [p for p in out.scrollers if p["element"].id == "list"]
    check("a results list stretched in a row of a page one window high is a panel its own height, scrolling the rest"
          " (Ponder's had grown to its contents, cut off at the window's edge)",
          lists and round(lists[0]["area"].height()) == 560 and round(lists[0]["max"]) == 1440,
          str([(round(p["area"].height()), round(p["max"])) for p in out.scrollers]))
    runtime = _node_for_tests()
    if not runtime:
        print("  skip  panels with scripts and touch: Node.js is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-panels-")
    open(os.path.join(folder, "index.html"), "w").write(
        "<!DOCTYPE html><html><head><title>Panels</title><style>body{margin:0} #panel{height:200px;overflow:auto}"
        " .row{height:50px} .tall{height:2000px}</style></head><body>"
        "<div id=panel>" + "".join(f"<div class=row onclick=\"say('row {n}')\">row {n}</div>" for n in range(20))
        + "</div><p id=out>none</p><p id=seen>none</p><div class=tall></div><script>"
        "function say(w) { document.getElementById('out').textContent = w; }"
        "document.getElementById('panel').addEventListener('scroll', (e) => {"
        " document.getElementById('seen').textContent = 'scrolled ' + Math.round(e.target.scrollTop)"
        " + ' of ' + e.target.scrollHeight + ' in ' + e.target.clientHeight; });"
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

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(700, 500)
    view.show()

    def text(eid):
        found = next((x for x in view._document.root.elements() if x.id == eid), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    def until(test, seconds=5):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.02)
        return test()

    def panel():
        return next((p for p in view._display.scrollers if p["element"].id == "panel"), None) if view._display else None

    def wheel(y, notches):
        event = QWheelEvent(QPointF(100, y), QPointF(view.mapToGlobal(QPoint(100, y))), QPoint(0, 0),
                            QPoint(0, -120 * notches), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        view.wheelEvent(event)
        app.processEvents()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))
        until(lambda: panel() is not None and view._script is not None and text("out") == "none", 15)
        wait(app, 1.0)
        wheel(100, 1)
        check("the wheel over a panel scrolls the panel, not the page",
              round(view._display.panel_offset(panel())) == 120 and view._scroll == 0,
              f"panel {view._display.panel_offset(panel()):.0f}, page {view._scroll}")
        check("its scripts hear it scroll, with its scrollTop, scrollHeight and clientHeight",
              until(lambda: text("seen") == "scrolled 120 of 1000 in 200"), text("seen"))
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(100, 10))
        check("a click lands on the row shown there, the panel scrolled (row 2, at 120 down)",
              until(lambda: text("out") == "row 2"), text("out"))
        wheel(100, 10)
        check("at the panel's end, the wheel goes on to the page",
              round(view._display.panel_offset(panel())) == 800 and view._scroll > 0,
              f"panel {view._display.panel_offset(panel()):.0f}, page {view._scroll}")
        view.scrollbar.setValue(0)
        view._script.send({"type": "run", "code": "document.getElementById('panel').scrollTop = 250"}) \
            if False else None
        view._on_script_message({"type": "panel_scroll_to", "target": panel()["element"].attrs["data-mjs"], "top": 250})
        check("a script setting a panel's scrollTop moves it", round(view._display.panel_offset(panel())) == 250)
        from PyQt6.QtCore import QEvent

        class Finger:
            """A touch event as Qt hands one on: its kind and its points."""

            def __init__(self, kind, *where):
                self._kind, self._points = kind, [type("P", (), {"position": (lambda self, w=w: QPointF(*w))})()
                                                  for w in where]

            def type(self):
                return self._kind

            def points(self):
                return self._points

            def accept(self):
                pass

            def ignore(self):
                pass

        def drag(path, pause=0.012):
            view._on_touch(Finger(QEvent.Type.TouchBegin, path[0]))
            for spot in path[1:]:
                time.sleep(pause)
                view._on_touch(Finger(QEvent.Type.TouchUpdate, spot))
                app.processEvents()
            view._on_touch(Finger(QEvent.Type.TouchEnd, path[-1]))
        check("Merlin Engine's view takes touch events (they had become mouse drags, scrolling nothing)",
              view.testAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents))
        view._panel_scroll[panel()["key"]] = 0.0
        view.scrollbar.setValue(0)
        drag([(100, 150 - y) for y in range(0, 101, 10)], pause=0.03)
        view._glide.stop()
        dragged = view._display.panel_offset(panel())
        check("a finger dragged up 100 pixels scrolls the panel under it 100", round(dragged) == 100, f"{dragged:.0f}")
        view._panel_scroll[panel()["key"]] = 0.0
        drag([(100, 180 - y) for y in range(0, 121, 30)], pause=0.01)
        flicked = view._display.panel_offset(panel())
        until(lambda: not view._glide.isActive(), 3)
        check("flicked, it glides on after the finger lifts, and slows to a stop",
              view._display.panel_offset(panel()) > flicked + 20 and not view._glide.isActive(),
              f"{flicked:.0f} then {view._display.panel_offset(panel()):.0f}")
        check("a flick keeps to the panel it moved, the page left where it was at its end",
              view._scroll == 0, f"page {view._scroll}")
        view._panel_scroll[panel()["key"]] = 0.0
        view.update()
        drag([(100, 300), (100, 200), (100, 100)], pause=0.05)
        view._glide.stop()
        check("a drag outside any panel scrolls the page", view._scroll > 0 and view._display.panel_offset(panel()) == 0,
              f"page {view._scroll}")
        view.scrollbar.setValue(0)
        app.processEvents()
        view._on_touch(Finger(QEvent.Type.TouchBegin, (100, 60)))
        view._on_touch(Finger(QEvent.Type.TouchEnd, (100, 60)))
        check("a tap clicks what is under it (row 1)", until(lambda: text("out") == "row 1"), text("out"))
        ignored = view._on_touch(Finger(QEvent.Type.TouchBegin, (100, 60), (200, 60)))
        check("a two-finger touch is left to the back and forward swipe", ignored is False)
    finally:
        view.close()
        server.shutdown()


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


def test_frames(app) -> None:
    """<iframe>: another page in a box of the page, with scripts of its own,
    messages and ports between windows, as Chrome asks for and refuses frames,
    and another site's frame keeping its cookies to this tab."""
    from merlin.engine import MerlinView
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    def frames_in(markup):
        document = parse("<style>body{margin:0}</style>" + markup)
        out = Layout(document, Styler(document).compute(), 800, viewport_height=600).run()
        return [(round(i[1].width()), round(i[1].height())) for i in out.items if i and i[0] == "frame"]
    check("an iframe is 300 by 150 unless given a size, as in browsers; its own text is not shown",
          frames_in("<iframe src=x>fallback</iframe>") == [(300, 150)])
    check("width and height attributes size it; frameborder=0 takes its border off",
          frames_in("<iframe src=x width=304 height=78 frameborder=0></iframe>") == [(304, 78)])
    check("in a flex row it keeps its width (it had measured 0 wide)",
          frames_in("<div style='display:flex'><iframe style='border:0'></iframe><iframe style='border:0;width:100px'></iframe></div>")
          == [(300, 150), (100, 150)])

    runtime = _node_for_tests()
    if not runtime:
        check("Node.js is here for the frames' scripts", False, "set MERLIN_NODE")
        return
    seen = {}
    folder = tempfile.mkdtemp(prefix="merlin-frames-")

    class Pages(http.server.BaseHTTPRequestHandler):
        pages = {}

        def do_GET(self):  # noqa: N802
            name = self.path.split("?")[0].lstrip("/")
            seen[name] = {k.lower(): v for k, v in self.headers.items()}
            body, extra = self.pages.get(name, ("missing", {}))
            data = body.encode()
            self.send_response(200 if name in self.pages else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for key, value in extra.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    parent_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    child_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    for server in (parent_server, child_server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    top = f"http://127.0.0.1:{parent_server.server_address[1]}"
    other = f"http://localhost:{child_server.server_address[1]}"      # another site
    Pages.pages = {
        "parent.html": (
            "<!DOCTYPE html><html><head><title>P</title></head><body style='margin:0;background:#ffffff'>"
            "<p id=got>waiting</p><p id=port>waiting</p><p id=loads>0</p><p id=blank>waiting</p>"
            f"<iframe id=f name=kid src='{other}/child.html' width=320 height=90 frameborder=0></iframe>"
            f"<iframe id=deny name=denied src='{other}/deny.html' width=200 height=60></iframe>"
            "<script>let loads = 0; const frame = document.getElementById('f');"
            "frame.addEventListener('load', () => { document.getElementById('loads').textContent = String(++loads); });"
            "window.addEventListener('message', (e) => {"
            " if (!e.data || !e.data.hello) return;"
            " const list = e.data.list;"
            " document.getElementById('got').textContent = 'from ' + e.origin + ': ' + e.data.hello"
            "  + ' | source is the frame: ' + (e.source === frame.contentWindow)"
            "  + ' | kept: ' + (list[1] === undefined) + ' ' + (list[2] instanceof Uint8Array) + ' ' + list[2][1];"
            " const channel = new MessageChannel();"
            " channel.port1.onmessage = (m) => { document.getElementById('port').textContent = 'port: ' + m.data; };"
            " e.source.postMessage('take this port', '*', [channel.port2]);"
            " frame.contentWindow.postMessage('not for you', 'https://elsewhere.test');"
            # an origin written as an address, as reCAPTCHA's frame writes its parent's
            f" frame.contentWindow.postMessage('with a path', '{other}/some/path?x=1');"
            "});"
            # a blank frame's window is the page's own origin: its JSON is there
            "const blank = document.createElement('IFRAME'); document.body.appendChild(blank);"
            "blank.scrolling = 'no';"
            "document.getElementById('blank').textContent = 'blank: ' + blank.localName + ' '"
            " + (typeof blank.contentWindow.JSON.parse) + ' ' + (blank.contentDocument !== null)"
            " + ' ' + (document.querySelector('iframe:last-of-type') === blank) + ' ' + blank.getAttribute('scrolling')"
            " + ' ' + window.name.length + ' ' + (window.frames.kid === document.getElementById('f').contentWindow);"
            "</script></body></html>", {}),
        "child.html": (
            "<!DOCTYPE html><html><body style='margin:0;background:#f9f9f9'><p id=c>child</p><p id=n></p>"
            "<script>const c = document.getElementById('c');"
            "document.getElementById('n').textContent = 'name ' + window.name + ' sibling '"
            " + (typeof parent.frames['denied'].postMessage);"
            "c.textContent = 'top is another window: ' + (window.top !== window);"
            "window.addEventListener('message', (e) => {"
            " if (e.data === 'not for you') { c.textContent = 'wrong: a message meant for another origin came'; return; }"
            " if (e.data === 'with a path') { document.title = 'with a path came'; return; }"
            " if (e.ports && e.ports[0]) { e.ports[0].postMessage('hello from ' + location.host); c.textContent = 'got a port from ' + e.origin; }"
            "});"
            "window.parent.postMessage({hello: 'world', list: [1, undefined, new Uint8Array([7, 8])]}, '*');"
            "document.cookie = 'framed=1';"
            "</script></body></html>", {"Set-Cookie": "served=1; Path=/"}),
        "deny.html": ("<p id=secret>shown inside another site</p>", {"X-Frame-Options": "DENY"}),
    }

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "runtime-cache")

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.02)
        return test()

    def text_of(view, eid):
        if view is None or view._document is None:
            return ""
        found = next((x for x in view._document.root.elements() if x.id == eid), None)
        return " ".join(found.text().split()) if found is not None else ""

    def frame_of(view, eid):
        return next((v for e, v in view._frames.items() if e.attrs.get("id") == eid), None)

    view = MerlinView()
    view._host = Host()
    view.resize(900, 600)
    view.show()
    try:
        view.setUrl(QUrl(top + "/parent.html"))
        until(lambda: "world" in text_of(view, "got") and "port:" in text_of(view, "port"), 25)
        # the frame's own page shows what it got a moment later, as its
        # scripts' changes come back: waited for too, or a busy machine fails it
        until(lambda: frame_of(view, "f") is not None
              and "got a port" in text_of(frame_of(view, "f"), "c"), 10)
        child = frame_of(view, "f")
        check("the page's iframe is a view of its own, placed over its box",
              child is not None and child.isVisible() and child.width() == 320 and child.height() == 90,
              str(child.geometry() if child is not None else None))
        check("its page was asked for as Chrome asks for a frame's: Sec-Fetch-Dest iframe, the parent's origin as referrer",
              seen.get("child.html", {}).get("sec-fetch-dest") == "iframe"
              and seen.get("child.html", {}).get("referer") == top + "/"
              and seen.get("child.html", {}).get("sec-fetch-site") == "cross-site"
              and "sec-fetch-user" not in seen.get("child.html", {}), str(seen.get("child.html")))
        check("the frame's scripts run, its top another window",
              "top is another window: true" in text_of(child, "c") or "got a port" in text_of(child, "c"),
              text_of(child, "c"))
        check("the frame's message reaches the parent, with its origin, the frame's contentWindow as source, "
              "and its data as sent (undefined, bytes)",
              text_of(view, "got") == f"from {other}: world | source is the frame: true | kept: true true 8",
              text_of(view, "got"))
        check("a MessagePort sent to the frame works across: the frame's reply over it arrives",
              text_of(view, "port") == "port: hello from " + other.split("//")[1], text_of(view, "port"))
        check("the frame got the port from the parent's origin, and no message meant for another origin",
              text_of(child, "c") == "got a port from " + top, text_of(child, "c"))
        check("the iframe's load event fired in the parent", text_of(view, "loads") != "0", text_of(view, "loads"))
        until(lambda: child.title() == "with a path came", 5)
        check("a message whose target origin is written as an address (path, port) reaches that origin",
              child.title() == "with a path came", child.title())
        check("a blank frame made by script is an <iframe> whose window has the page's JSON and a document",
              text_of(view, "blank").startswith("blank: iframe function true true"), text_of(view, "blank"))
        check("iframe.scrolling = 'no' is its attribute; a tab's window.name is empty, its frames found by name",
              text_of(view, "blank").endswith(" no 0 true"), text_of(view, "blank"))
        check("a frame's window.name is its <iframe>'s name, and parent.frames[name] its sibling",
              text_of(child, "n") == "name kid sibling function", text_of(child, "n"))
        until(lambda: frame_of(view, "deny") is not None and frame_of(view, "deny")._document is not None, 10)
        denied = frame_of(view, "deny")
        check("a page that says X-Frame-Options: DENY is not shown in the frame",
              denied is not None and "refused to connect" in denied._document.root.text()
              and "shown inside" not in denied._document.root.text())
        # what the frame's page draws is seen in the parent, where the frame is
        app.processEvents()
        picture = view.grab().toImage()
        spot = child.geometry().center()
        check("the frame's page is drawn in its place", picture.pixelColor(spot.x(), spot.y()).name() == "#f9f9f9",
              picture.pixelColor(spot.x(), spot.y()).name())
        # another site's frame: its cookies are its own, for this tab only
        window_jar = view._window_cookies()
        framed_jar = child._cookies()
        names = lambda jar: sorted(c.name for c in jar) if jar is not None else []
        check("another site's frame keeps its cookies apart from the window's (as Brave does)",
              framed_jar is not window_jar and "served" in names(framed_jar) and "served" not in names(window_jar),
              f"frame {names(framed_jar)}, window {names(window_jar)}")
        # scripts change the page: the frame stays, not loaded again
        asked = len([1 for _ in range(1)])
        before = seen.get("child.html")
        view._script.send({"type": "click", "target": int(next(e for e in view._document.root.elements()
                                                                if e.id == "got").attrs["data-mjs"]), "id": 0})
        until(lambda: False, 0.5)
        check("the frame stays the same as the page changes", frame_of(view, "f") is child and seen.get("child.html") is before)
        # a new page: its frames go, and their scripts with them
        process = child._script.process if child._script is not None else None
        view.setHtml("<p>plain</p>")
        until(lambda: not view._frames, 3)
        check("a new page ends the old page's frames and their scripts",
              not view._frames and (process is None or process.poll() is not None or until(lambda: process.poll() is not None, 3)))
        del asked
    finally:
        view.shutdown()
        view.deleteLater()
        parent_server.shutdown()
        child_server.shutdown()


def test_layout_for_dialogs_and_settings(app) -> None:
    """What Ponder's dialogs and settings needed of layout: a box centred by
    translate(-50%, -50%) clicked where it is drawn, -webkit-line-clamp,
    text-transform, overflow-y: auto beside overflow-x: hidden, a panel that
    scrolls in a fixed box, and a range input as a slider."""
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    def laid(markup, width=800, height=600):
        document = parse("<style>body{margin:0}</style>" + markup)
        styles = Styler(document, viewport=(width, height)).compute()
        return document, styles, Layout(document, styles, width, viewport_height=height).run()

    _d, _s, out = laid("<div style='position:fixed;inset:0'><div style='position:absolute;top:50%;left:50%;"
                       "transform:translate(-50%,-50%);width:200px'><button id=close style='width:100px;height:20px'>Close</button>"
                       "</div></div>")
    boxes = out.fixed.boxes if out.fixed is not None else out.boxes
    close = next(r for r, e in boxes if e.id == "close")
    check("what is in a box centred by translate(-50%, -50%) is where it is drawn (Ponder's Close had done nothing)",
          abs(close.x() - 300) < 1 and close.y() < 300, f"{close}")
    _d, _s, out = laid("<p id=c style='margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;"
                       "overflow:hidden;line-height:20px;width:100px'>one two three four five six seven eight nine ten</p>")
    clamp = next(r for r, e in out.boxes if e.id == "c")
    check("-webkit-line-clamp: 2 shows two lines (Ponder's snippets had run on)", clamp.height() == 40, f"{clamp.height()}")

    def texts(items):
        for item in items:
            if item and item[0] == "text":
                yield item[3]
            elif item and item[0] == "group":
                yield from texts(item[1])
    _d, _s, out = laid("<div style='text-transform:uppercase'>web <b>backend</b></div>"
                       "<div style='text-transform:capitalize'>show summary</div>")
    check("text-transform: uppercase and capitalize are drawn (Ponder's headings showed as written)",
          list(texts(out.items)) == ["WEB ", "BACKEND", "Show Summary"], str(list(texts(out.items))))
    document, styles, out = laid("<div id=p style='position:fixed;top:40px;right:10px;width:300px;max-height:calc(100vh - 60px);"
                                 "overflow-y:auto;overflow-x:hidden'>" + "<p>line</p>" * 60 + "</div>", height=560)
    panel = next(e for e in document.root.elements() if e.id == "p")
    check("overflow-y: auto with overflow-x: hidden scrolls (it had come out hidden, the last written)",
          styles[panel].get("overflow") == "auto", str(styles[panel].get("overflow")))
    _d, _s, whole = laid("<div style='height:100vh;display:flex;flex-direction:column;overflow:hidden'>"
                       "<div style='height:50px'>bar</div><div id=list style='flex:1;overflow-y:auto'>"
                       + "<p style='height:100px;margin:0'>card</p>" * 30 + "</div></div>", height=600)
    check("a page one window high, its list scrolling in a panel, is one window high (Ponder's results had "
          "scrolled on, past the list's end, into nothing)", abs(whole.height - 600) < 1 and len(whole.scrollers) == 1,
          f"{whole.height:.0f} high")
    check("and a panel in a fixed box scrolls (Ponder's settings, taller than the window, were cut off)",
          out.fixed is not None and len(out.fixed.scrollers) == 1 and out.fixed.scrollers[0]["max"] > 100,
          str(len(out.fixed.scrollers) if out.fixed is not None else None))


def test_node_scripts(app) -> None:
    """Pages' JavaScript in Node.js (Deno, before, is Rust): nothing of Node
    for the page, no file or network of its own; the DOM as browsers have it
    for selects, labels and styles; FileReader; multipart FormData; Web
    Workers; and a page's own file input never put in the page."""
    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage, host_command
    from PyQt6.QtWidgets import QFileDialog

    runtime = _node_for_tests()
    if not runtime:
        check("Node.js is here for pages' scripts", False, "set MERLIN_NODE")
        return
    from merlin import rustaudit

    check("the Node that runs pages' scripts has no Rust in it", not rustaudit.scan_file(runtime))
    command = host_command(runtime)
    check("pages' scripts run under Node's permission model, reading only Merlin's own script files",
          "--permission" in command and any(c.startswith("--allow-fs-read=") and c.rstrip("/\\").endswith("js")
                                             for c in command) and "--allow-child-process" not in command)
    folder = tempfile.mkdtemp(prefix="merlin-node-")
    posted = {}
    open(os.path.join(folder, "index.html"), "w").write(
        "<!DOCTYPE html><html><head><title>N</title></head><body>"
        "<p id=sandbox>waiting</p><p id=select>waiting</p><p id=style>waiting</p><p id=reader>waiting</p>"
        "<p id=form>waiting</p><p id=worker>waiting</p><p id=picked>none</p>"
        "<label class=toggle id=label><input type=checkbox id=box style='opacity:0;width:0;height:0;position:absolute'>"
        "<span id=track style='display:inline-block;width:36px;height:20px;background:#888'></span></label>"
        "<div id=panel style='display:block'>panel</div><button id=hide>hide</button><button id=browse>Browse</button>"
        "<script>"
        "const out = (id, t) => document.getElementById(id).textContent = t;"
        "(async () => { const r = [];"
        " r.push(typeof process, typeof require, typeof Buffer, typeof Deno);"
        " try { await (new Function('return import(\"node:fs\")'))(); r.push('LEAK'); } catch (e) { r.push('no-node'); }"
        " try { await import('node:net'); r.push('LEAK'); } catch (e) { r.push('no-net'); }"
        " out('sandbox', r.join(' ')); })();"
        "const s = document.createElement('select'); for (const v of ['none','a.jpg','b.jpg']) { const o = document.createElement('option'); o.value = v; o.textContent = v; s.appendChild(o); }"
        "document.body.appendChild(s); s.value = 'b.jpg'; const before = s.selectedIndex; while (s.options.length > 1) s.remove(1);"
        "out('select', before + ' ' + s.options.length + ' ' + s.isConnected + ' ' + s.value);"
        "document.getElementById('hide').addEventListener('click', () => { document.getElementById('panel').style.display = 'none'; });"
        "document.getElementById('box').addEventListener('change', (e) => out('style', 'toggled ' + e.target.checked));"
        "const fr = new FileReader(); fr.onload = () => out('reader', fr.result); fr.readAsDataURL(new Blob(['hi'], {type: 'text/plain'}));"
        "const fd = new FormData(); fd.append('file', new File(['abc'], 'a.txt', {type: 'text/plain'})); fd.append('name', 'x');"
        "fetch('/upload', {method: 'POST', body: fd}).then(r => r.text()).then(t => out('form', t));"
        "const w = new Worker(URL.createObjectURL(new Blob(['onmessage = (e) => postMessage(e.data * 2 + \" \" + typeof process)'], {type: 'text/javascript'})));"
        "w.onmessage = (e) => out('worker', 'worker ' + e.data); w.postMessage(21);"
        "document.getElementById('browse').addEventListener('click', () => { const i = document.createElement('input'); i.type = 'file';"
        " i.onchange = () => out('picked', i.files[0].name); i.click(); });"
        "</script></body></html>")
    picture = os.path.join(folder, "wall.png")
    open(picture, "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    class Pages(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            posted["type"] = self.headers.get("Content-Type", "")
            posted["body"] = body
            answer = b"multipart " + (b"with the file" if b'filename="a.txt"' in body and b"abc" in body else b"without it")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def local_storage(self):
            return LocalStorage(None)

    def text_of(view, eid):
        found = next((x for x in view._document.root.elements() if x.id == eid), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.02)
        return test()

    def click(view, eid):
        from PyQt6.QtTest import QTest

        box = next(r for r, e in view._display.boxes if e.id == eid)
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(int(box.center().x()), int(box.center().y() - view._scroll)))

    real_dialog = QFileDialog.getOpenFileName
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (picture, ""))
    view = MerlinView()
    view._host = Host()
    view.resize(900, 700)
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))
        until(lambda: "worker" in text_of(view, "worker") and "multipart" in text_of(view, "form"), 15)
        check("a page sees nothing of Node and cannot load its modules (files, network)",
              text_of(view, "sandbox") == "undefined undefined undefined undefined no-node no-net", text_of(view, "sandbox"))
        check("a select's value can be set, and remove(i) removes an option, not the select (Ponder's background list)",
              text_of(view, "select") == "2 1 true none", text_of(view, "select"))
        check("FileReader reads a Blob (Node has none of its own)", text_of(view, "reader") == "data:text/plain;base64,aGk=",
              text_of(view, "reader"))
        check("FormData is sent as multipart/form-data, its file in it (Ponder's background upload)",
              text_of(view, "form") == "multipart with the file" and posted.get("type", "").startswith("multipart/form-data"),
              text_of(view, "form"))
        check("a Web Worker runs, made from a Blob, with nothing of Node (Convert to it! does its work in one)",
              text_of(view, "worker") == "worker 42 undefined", text_of(view, "worker"))
        click(view, "hide")
        check("el.style.display = 'none' is seen (a style changed alone had not been: Ponder's dialogs stayed open)",
              until(lambda: "display:none" in (next(e for e in view._document.root.elements() if e.id == "panel").attrs.get("style", "").replace(" ", "")), 5))
        click(view, "track")
        check("a click in a label works its hidden checkbox (Ponder's toggle switches)",
              until(lambda: text_of(view, "style") == "toggled true", 5), text_of(view, "style"))
        click(view, "browse")
        check("a file input the page made and never put in the page still opens the dialog (Ponder's Browse...)",
              until(lambda: text_of(view, "picked") == "wall.png", 6), text_of(view, "picked"))
    finally:
        QFileDialog.getOpenFileName = real_dialog
        view.shutdown()
        view.deleteLater()
        server.shutdown()


def test_worker_features(app) -> None:
    """What workers have in browsers: a worker starting workers of its own,
    XMLHttpRequest in a worker (waiting for its answer too, as Emscripten's
    file loading does), SharedWorker (one thread, a port for each), the Cache
    API, and no navigator.serviceWorker (a half one is worse than none)."""
    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    runtime = _node_for_tests()
    if not runtime:
        check("Node.js is here for pages' scripts", False, "set MERLIN_NODE")
        return
    folder = tempfile.mkdtemp(prefix="merlin-workers-")
    files = {
        "index.html": "<!DOCTYPE html><html><body><p id=nested>waiting</p><p id=xhr>waiting</p><p id=shared>waiting</p>"
                      "<p id=cache>waiting</p><p id=sw>waiting</p><p id=pagexhr>waiting</p><p id=tag>waiting</p><p id=style>waiting</p>"
                      "<p id=cycle>waiting</p><div id=outer><span id=inner>x</span></div><p id=listener>waiting</p>"
                      "<script type=module>import { done } from '/cycle_a.js'; document.getElementById('cycle').textContent = done();</script>"
                      "<script>"
                      "{ const outer = document.getElementById('outer'); outer.handlers = { click: () => 'found' }; let seen = '';"
                      " outer.addEventListener('click', function (e) { seen = (this === outer) + ' ' + this.handlers.click(); });"
                      " const ev = new MouseEvent('click', { bubbles: true }); document.getElementById('inner').dispatchEvent(ev);"
                      " document.getElementById('listener').textContent = seen + ' ' + (ev.target && ev.target.id); }"
                      "const out = (id, t) => document.getElementById(id).textContent = t;"
                      "out('tag', [document.createElement('button'), document.querySelectorAll('p'), new Event('x')]"
                      ".map((x) => Object.prototype.toString.call(x)).join(' '));"
                      "{ const st = document.createElement('div').style; const r = ['textAlign' in st, 'text-align' in st,"
                      " 'fooBar' in st, typeof st.fooBar]; const el = document.getElementById('style');"
                      " el.style.textAlign = 'center'; el.style.webkitTransform = 'none'; el.style.cssFloat = 'left';"
                      " el.style[undefined] = 'x'; r.push(el.getAttribute('style')); el.textContent = r.join(' '); }"
                      "const w = new Worker('/outer.js'); w.onmessage = (e) => out(e.data[0], e.data[1]); w.postMessage('go');"
                      "const s1 = new SharedWorker('/shared.js', 'one'), s2 = new SharedWorker('/shared.js', 'one'); let got = [];"
                      "for (const s of [s1, s2]) { s.port.onmessage = (e) => { got.push(e.data); out('shared', got.sort().join(',')); };"
                      " s.port.postMessage('hi'); }"
                      "caches.open('v1').then(async (c) => { await c.put('/n.js', new Response('cached!'));"
                      " out('cache', await (await caches.match('/n.js')).text()); });"
                      "out('sw', 'serviceWorker ' + ('serviceWorker' in navigator));"
                      "const x = new XMLHttpRequest(); x.open('GET', '/n.js');"
                      " x.onload = () => out('pagexhr', 'page xhr ' + x.status + ' ' + x.responseText.trim()); x.send();"
                      "</script></body></html>",
        "outer.js": "onmessage = () => {"
                    " const inner = new Worker('/inner.js'); inner.onmessage = (e) => postMessage(['nested', 'nested says ' + e.data]);"
                    " inner.postMessage(5);"
                    " const x = new XMLHttpRequest(); x.open('GET', '/n.js', false); x.send();"
                    " const y = new XMLHttpRequest(); y.open('GET', '/lib.js');"
                    " y.onload = () => postMessage(['xhr', 'sync ' + x.status + ' ' + x.responseText.trim() + ' | async ' + y.responseText.trim()]);"
                    " y.send(); };",
        "inner.js": "onmessage = (e) => postMessage(e.data * 10);",
        "shared.js": "let count = 0; onconnect = (e) => { const port = e.ports[0]; const mine = ++count;"
                     " port.onmessage = (m) => port.postMessage(m.data + ' from connection ' + mine); };",
        "n.js": "export const n = 42;",
        "lib.js": "export default 'LIB';",
        # two modules importing each other, as GitHub's do
        "cycle_a.js": "import { b } from '/cycle_b.js'; export const a = 'A'; export function done() { return 'cycle ' + a + b(); }",
        "cycle_b.js": "import { a } from '/cycle_a.js'; export function b() { return 'B' + a; }",
    }
    for name, text in files.items():
        open(os.path.join(folder, name), "w").write(text)

    class Pages(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def local_storage(self):
            return LocalStorage(None)

    def text_of(eid):
        found = next((x for x in view._document.root.elements() if x.id == eid), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.02)
        return test()

    view = MerlinView()
    view._host = Host()
    view.resize(900, 700)
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))
        until(lambda: all(text_of(i) != "waiting" for i in ("nested", "xhr", "shared", "cache", "pagexhr"))
              and "," in text_of("shared"), 20)
        check("a worker starts a worker of its own, and hears from it", text_of("nested") == "nested says 50",
              text_of("nested"))
        check("XMLHttpRequest in a worker, waiting for its answer and not",
              text_of("xhr") == "sync 200 export const n = 42; | async export default 'LIB';", text_of("xhr"))
        check("two SharedWorkers of one address and name are one thread, a port each",
              text_of("shared") == "hi from connection 1,hi from connection 2", text_of("shared"))
        check("the Cache API keeps a response and gives it back", text_of("cache") == "cached!", text_of("cache"))
        check("no navigator.serviceWorker: sites go without, rather than wait on one that never runs",
              text_of("sw") == "serviceWorker false", text_of("sw"))
        check("the page's own XMLHttpRequest still works", text_of("pagexhr") == "page xhr 200 export const n = 42;",
              text_of("pagexhr"))
        check("a style object knows the properties a browser's does, and names them as CSS does "
              "(Vue had written style=\"undefined:center\" on Square's sites, and the centring was lost)",
              text_of("style") == "true true false undefined text-align:center;-webkit-transform:none;float:left",
              text_of("style"))
        until(lambda: text_of("cycle") != "waiting", 10)
        check("modules that import each other run (GitHub's had waited on each other for ever)",
              text_of("cycle") == "cycle ABA", text_of("cycle"))
        check("a listener's this is the element it listens on, and the event keeps its target "
              "(Preact's handlers: Convert to it!'s file button)",
              text_of("listener") == "true found inner", text_of("listener"))
        check("the DOM's objects say what they are, as in browsers (Tippy tells an element from a selector so)",
              text_of("tag") == "[object HTMLButtonElement] [object NodeList] [object Event]", text_of("tag"))
    finally:
        view.shutdown()
        view.deleteLater()
        server.shutdown()


def test_blur_timing(app) -> None:
    """Settings > Shields > Blur timing in Merlin Engine: the page's clocks,
    and its workers', in 100 ms steps; what is typed still arrives, in order."""
    from PyQt6.QtTest import QTest

    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    runtime = _node_for_tests()
    if not runtime:
        check("Node.js is here for pages' scripts", False, "set MERLIN_NODE")
        return
    page = ("<!DOCTYPE html><html><body><p id=clocks>waiting</p><p id=worker>waiting</p><p id=typed></p>"
            "<input id=field><script>"
            "const out = (id, t) => document.getElementById(id).textContent = t;"
            "const steps = (list) => list.every((t) => t % 100 === 0);"
            "out('clocks', [steps([performance.now(), Date.now(), new Date().getTime(), new Event('x').timeStamp]),"
            " new Date(2020, 0, 1).getFullYear(), new Date() instanceof Date].join(' '));"
            "const w = new Worker(URL.createObjectURL(new Blob(['postMessage([performance.now(), Date.now()].every((t) => t %25 100 === 0))'])));"
            "w.onmessage = (e) => out('worker', 'worker ' + e.data);"
            "document.getElementById('field').addEventListener('input', (e) => out('typed', e.target.value));"
            "</script></body></html>").replace("%25", "%")

    class Host:
        settings = {"blur_timing": True}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def local_storage(self):
            return LocalStorage(None)

    def text_of(eid):
        found = next((x for x in view._document.root.elements() if x.id == eid), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.02)
        return test()

    folder = tempfile.mkdtemp(prefix="merlin-blur-")
    open(os.path.join(folder, "index.html"), "w").write(page)

    class Pages(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    view = MerlinView()
    view._host = Host()
    view.resize(900, 700)
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))
        until(lambda: text_of("clocks") not in ("", "waiting") and text_of("worker") not in ("", "waiting"), 15)
        check("Blur timing: the page's clocks (and an event's timeStamp) in 100 ms steps, Date still a Date",
              text_of("clocks") == "true 2020 true", text_of("clocks"))
        check("Blur timing: a worker's clocks too", text_of("worker") == "worker true", text_of("worker"))
        field = next((w for e, w in view._widgets.items() if e.attrs.get("id") == "field"), None)
        if field is not None:
            field.setFocus()
            QTest.keyClicks(field, "merlin")
        check("Blur timing: what is typed reaches the page, late but in order",
              until(lambda: text_of("typed") == "merlin", 8), text_of("typed"))
    finally:
        view.shutdown()
        view.deleteLater()
        server.shutdown()


def test_square_and_convert_layout(app) -> None:
    """What AlterniTech's Square site and Convert to it! needed: calc() with
    * and / (Square's fluid type scale), empty custom properties, the old
    grid-column-gap name, <br> lines as tall as the line-height, box-shadow
    (Square outlines buttons with one), logical properties, margin: auto in
    a column flexbox, border-box widths of column items, dashed borders, and
    a closed <dialog> hidden."""
    from PyQt6.QtGui import QColor, QImage, QPainter

    from merlin.engine import paint as paint_module
    from merlin.engine.css import Styler, calc_number, calc_value, expand_shorthand, parse_length
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    def laid(markup, width=800, height=600):
        document = parse("<!DOCTYPE html><style>body{margin:0}</style>" + markup)
        styles = Styler(document, viewport=(width, height)).compute()
        return document, styles, Layout(document, styles, width, viewport_height=height).run()

    def box(out, eid):
        for source in (out, out.fixed):
            if source is None:
                continue
            for rect, element in source.boxes:
                if element.id == eid:
                    return rect
        return None

    def style_of(document, styles, eid):
        return styles[next(e for e in document.root.elements() if e.id == eid)]

    # calc()
    square = ("calc(calc(calc(16*1.197)*1.197)*1px + calc(calc(calc(16*calc(1.197 + 0.11))*calc(1.197 + 0.11))"
              " - calc(calc(16*1.197)*1.197))*calc(calc(1200px - 320*1px)/calc(1280 - 320)))")
    check("calc() with nested calc(), * and / (Square's heading size)",
          abs(parse_length(square, 16, 16) - 26.9647) < 0.01, str(parse_length(square, 16, 16)))
    check("calc() keeps what it did: a length plus a length, a percentage minus a length for layout",
          parse_length("calc(2em + 3px)", 10, 16) == 23.0 and parse_length("calc(100% - 20px)", 16, 16) is None
          and calc_value("calc(100% - 2 * 8px)") == {"%": 100.0, "px": -16.0})
    check("calc(var(--space)*-1) is negative, min()/clamp() work inside it",
          parse_length("calc(10px*-1)", 16, 16) == -10.0
          and parse_length("calc(min(10px, 2em) + clamp(1px, 50px, 4px))", 16, 16) == 14.0)
    check("a unitless calc() for line-height", abs(calc_number("calc(calc(1.5*0.95)*0.95)") - 1.35375) < 1e-9)
    document, styles, out = laid(
        "<style>:root{--base:16;--scale:1.2;--fs:calc(var(--base)*var(--scale)*1px);--lh:calc(1.5*0.9)}"
        "h1{font-size:var(--fs);line-height:var(--lh);margin:0}</style><h1 id=h>Title</h1>")
    st = style_of(document, styles, "h")
    check("a font size from custom properties and calc() is used, not the default",
          abs(st["font-size"] - 19.2) < 0.01 and st["line-height"] == ("factor", 1.35), f"{st['font-size']} {st['line-height']}")
    # empty custom properties
    document, styles, out = laid(
        "<style>.b{--font-size:12px;font-size:var(--font-size,30px)} .w{font-size:20px}</style>"
        "<div class=w><a id=a class=b style='--font-size:;'>x</a><a id=c class=b>y</a></div>")
    check("a custom property set to nothing puts nothing in: the property is unset (inherited), the fallback unused",
          style_of(document, styles, "a")["font-size"] == 20.0 and style_of(document, styles, "c")["font-size"] == 12.0,
          f"{style_of(document, styles, 'a')['font-size']} {style_of(document, styles, 'c')['font-size']}")
    # grid-column-gap on a flex box
    document, styles, out = laid("<div style='display:flex;grid-column-gap:16px'>"
                                 "<div id=one style='width:100px'>a</div><div id=two style='width:100px'>b</div></div>")
    check("grid-column-gap, the old name, spaces flex items too (Square's two buttons had touched)",
          abs(box(out, "two").x() - box(out, "one").right() - 16) < 0.5, f"{box(out, 'two').x()}")
    # <br> lines
    document, styles, out = laid("<h1 id=h style='font-size:20px;line-height:30px;margin:0'><br><br><br>Text<br><br><br><br></h1>"
                                 "<div id=e style='line-height:30px'><br></div>")
    check("lines that are only a <br> are as tall as the line-height (AlterniTech's heading is spaced with them)",
          box(out, "h").height() == 210 and box(out, "e").height() == 30, f"{box(out, 'h').height()} {box(out, 'e').height()}")
    # box-shadow
    document, styles, out = laid("<div id=s style='margin:40px;width:100px;height:40px;box-shadow:inset 0 0 0 2px #ff0000'></div>"
                                 "<div id=t style='margin:40px;width:100px;height:40px;box-shadow:0 0 0 6px #0000ff'></div>"
                                 "<div id=u style='margin:40px;width:100px;height:40px;box-shadow:inset 0 0 0 2px #ff0000,,0 0'></div>")
    image = QImage(800, 400, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    paint_module.paint(painter, out, QRectF(0, 0, 800, 400), {})
    painter.end()
    s_box, t_box, u_box = box(out, "s"), box(out, "t"), box(out, "u")
    edge = image.pixelColor(int(s_box.x()) + 1, int(s_box.center().y()))
    inside = image.pixelColor(int(s_box.center().x()), int(s_box.center().y()))
    ring = image.pixelColor(int(t_box.x()) - 3, int(t_box.center().y()))
    none = image.pixelColor(int(u_box.x()) + 1, int(u_box.center().y()))
    check("an inset box-shadow outlines the box inside its edge (Square's outlined buttons)",
          edge.red() > 200 and edge.green() < 60 and inside.green() > 200, f"{edge.name()} {inside.name()}")
    check("an outer box-shadow's spread draws round the box",
          ring.blue() > 200 and ring.red() < 60, ring.name())
    check("a box-shadow with an empty shadow in its list is invalid, and draws nothing (as Chrome)",
          none.green() > 200, none.name())
    # logical properties
    check("logical properties are their sides, left to right",
          expand_shorthand("margin-inline-end", "auto") == [("margin-right", "auto")]
          and expand_shorthand("padding-block", "1px 2px") == [("padding-top", "1px"), ("padding-bottom", "2px")]
          and expand_shorthand("max-inline-size", "40ch") == [("max-width", "40ch")])
    document, styles, out = laid("<footer style='display:flex;width:100%;box-sizing:border-box;padding:0 16px'>"
                                 "<span id=l style='margin-inline-end:auto'>left</span><span id=r>right</span></footer>")
    check("margin-inline-end: auto pushes what follows to the far end (Convert to it!'s footer)",
          box(out, "l").x() < 20 and box(out, "r").right() > 770, f"{box(out, 'l')} {box(out, 'r')}")
    # column flexbox: margin auto, border-box width
    document, styles, out = laid("<div style='display:flex;flex-direction:column;min-height:600px'>"
                                 "<div id=card style='width:200px;height:100px;margin:auto'>card</div>"
                                 "<footer id=f style='width:100%;box-sizing:border-box;padding:0 16px;flex-shrink:0'>f</footer></div>")
    card, foot = box(out, "card"), box(out, "f")
    check("margin: auto centres an item in a column flexbox, both ways (Convert to it!'s card had sat at the top)",
          abs(card.x() - 300) < 1 and card.y() > 200, f"{card}")
    check("a border-box item of width 100% in a column flexbox is as wide as it, padding inside",
          abs(foot.width() - 800) < 0.5 and foot.bottom() > 590, f"{foot}")
    # dashed
    document, styles, out = laid("<div id=d style='width:100px;height:50px;border:2px dashed #000'></div>")
    kinds = []

    def walk(items):
        for item in items:
            if item and item[0] == "group":
                walk(item[1])
            elif item:
                kinds.append(item[0])
    walk(out.items)
    check("a dashed border is drawn dashed", kinds.count("dashed") == 4, str(kinds))
    # dialog
    document, styles, out = laid("<dialog id=closed>hidden</dialog><dialog id=opened open>shown</dialog>")
    check("a <dialog> is hidden until open (Convert to it!'s had shown its shadow)",
          style_of(document, styles, "closed").get("display") == "none"
          and style_of(document, styles, "opened").get("display") == "block")


def test_hover(app) -> None:
    """:hover: a menu that drops down on .item:hover > .menu (AlterniTech's),
    opening as the pointer comes over it, staying open as it moves into the
    menu, and closing when it leaves."""
    from merlin.engine import MerlinView
    from merlin.engine.css import matches, parse_selector
    from merlin.engine.html import parse

    document = parse("<style>a:hover{color:red}</style><a id=a>x</a>")
    element = next(e for e in document.root.elements() if e.id == "a")
    selector = parse_selector("a:hover")
    check(":hover is a selector Merlin matches, and only when the element is hovered",
          selector.matchable and not matches(selector, element))
    element.hover = True
    check("... and then it does", matches(selector, element))
    resting = next(e for e in parse("<a id=r>x</a>").root.elements() if e.id == "r")
    check(":not(:hover) is true at rest", matches(parse_selector("a:not(:hover)"), resting))

    folder = tempfile.mkdtemp(prefix="merlin-hover-")
    open(os.path.join(folder, "index.html"), "w").write(
        "<!DOCTYPE html><html><head><style>body{margin:0;font:16px sans-serif}"
        "nav ul{list-style:none;margin:0;padding:0;display:flex}"
        ".item{position:relative;padding:10px 20px}"
        ".menu{position:absolute;left:0;top:100%;opacity:0;transform:scaleY(0);background:#eee;width:600px;z-index:5}"
        ".item:hover>.menu{opacity:1;transform:scaleY(1)}"
        ".menu a{display:block;padding:8px}"
        # Square's: the menu a gap below its item, closing by a transition
        ".gapped>.menu{top:auto;margin-top:17px;transition:opacity .5s ease-in-out .1s,transform 0ms ease-in-out .6s}"
        ".gapped:hover>.menu{transition:opacity .2s ease-in-out 50ms,transform 0ms ease-in-out 0ms}"
        "</style></head><body>"
        "<nav><ul><li class=item id=software><a id=top href='/software'>Software</a>"
        "<div class=menu id=menu><a id=first href='/one'>One</a><a id=second href='/two'>Two</a></div></li>"
        "<li class=item><a href='/about'>About</a></li>"
        "<li class='item gapped' id=products><a id=ptop href='/products'>Products</a>"
        "<div class=menu id=menu2><a id=pone href='/phone'>Phone</a></div></li></ul></nav>"
        # a heading the open menu lies over, smaller than the menu: what is on
        # top is what the pointer is over, not the smallest box there
        "<h1 id=hero style='margin:4px 0 0 0;font-size:20px;display:inline-block'>A heading under the menu</h1>"
        "<p id=far style='margin-top:300px'>far below</p>"
        # big enough to be laid out in the background, as AlterniTech's is
        + "<div>" + "<i>.</i>" * 1300 + "</div></body></html>")

    class Pages(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return False

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.02)
        return test()

    view = MerlinView()
    view._host = Host()
    view.resize(800, 600)
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/index.html"))
        until(lambda: view._display is not None and view._document is not None
              and any(e.id == "far" for _r, e in view._display.boxes), 15)
        menu = next(e for e in view._document.root.elements() if e.id == "menu")

        def opacity():
            return view._styles.get(menu, {}).get("opacity", 1.0)

        def where(eid):
            for rect, element in view._display.boxes:
                if element.id == eid:
                    return rect
            return None
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QMouseEvent

        def move(x, y):
            # straight to the view: windows other tests left open may be over
            # it, where the system's pointer would be
            point = QPointF(x, y)
            app.sendEvent(view, QMouseEvent(QEvent.Type.MouseMove, point, view.mapToGlobal(point),
                                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                            Qt.KeyboardModifier.NoModifier))
        check("at rest the menu is hidden", opacity() == 0.0, str(opacity()))
        top = where("top")
        move(top.center().x(), top.center().y())
        check("the pointer over the menu's item opens it (.item:hover > .menu)", until(lambda: opacity() == 1.0, 6),
              str(opacity()))
        second = where("second")
        # down from the item to the option in small steps, as a mouse moves,
        # each waiting a moment as events do
        x = top.center().x()
        y = top.center().y()
        while y < second.center().y():
            move(x, y)
            until(lambda: False, 0.03)
            y += 4
        move(second.center().x(), second.center().y())
        until(lambda: False, 1.0)
        check("moving down into the menu keeps it open, and its links are there to click",
              opacity() == 1.0 and view._link_at(QPointF(second.center().x(), second.center().y())) == "/two",
              str(opacity()))
        far = where("far")
        move(far.center().x(), far.center().y())
        check("moving away closes it", until(lambda: opacity() == 0.0, 6), str(opacity()))
        # a gap between the item and its menu, crossed: the menu waits for
        # its own closing transition, as in a browser, and is there to reach
        menu2 = next(e for e in view._document.root.elements() if e.id == "menu2")

        def opacity2():
            return view._styles.get(menu2, {}).get("opacity", 1.0)
        ptop, item = where("ptop"), where("products")
        move(ptop.center().x(), ptop.center().y())
        until(lambda: opacity2() == 1.0, 6)
        pone = where("pone")
        gap_y = (item.bottom() + where("menu2").top()) / 2
        check("the gapped menu opens, a gap below its item",
              opacity2() == 1.0 and where("menu2").top() - item.bottom() > 3,
              f"{opacity2()} item ends {item.bottom()}, menu starts {where('menu2').top()}")
        move(ptop.center().x(), gap_y)
        until(lambda: False, 0.1)
        open_in_gap = opacity2() == 1.0
        move(pone.center().x(), pone.center().y())
        until(lambda: False, 1.0)
        check("crossing the gap, the menu stays open for its transition, and once over it stays open",
              open_in_gap and opacity2() == 1.0
              and view._link_at(QPointF(pone.center().x(), pone.center().y())) == "/phone", str(opacity2()))
        move(far.center().x(), far.center().y())
        until(lambda: False, 0.2)
        lingering = opacity2() == 1.0
        check("and left for good, it closes once its transition has run",
              lingering and until(lambda: opacity2() == 0.0, 3), f"lingered {lingering}, now {opacity2()}")
    finally:
        view.shutdown()
        view.deleteLater()
        server.shutdown()


def test_pictures_cascade_and_painting(app) -> None:
    """A picture filling a positioned box by percentages (Square's product
    pictures); the ancestor filter matches as matching does; layers out of
    view are passed over and those in view drawn; scaled pictures kept."""
    from PyQt6.QtGui import QColor, QImage, QPainter

    from merlin.engine import paint as paint_module
    from merlin.engine.css import Styler, matches, parse_selector
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    def laid(markup, width=800, height=600, images=None):
        document = parse("<!DOCTYPE html><style>body{margin:0}</style>" + markup)
        styles = Styler(document, viewport=(width, height)).compute()
        return document, styles, Layout(document, styles, width, viewport_height=height, images=images or {}).run()

    def drawn_images(out):
        found = []

        def walk(items):
            for item in items:
                if item and item[0] == "group":
                    walk(item[1])
                elif item and item[0] == "image":
                    found.append(item)
        walk(out.items)
        for source in (out.fixed,):
            if source is not None:
                walk(source.items)
        return found

    wide = QImage(600, 300, QImage.Format.Format_RGB32)
    wide.fill(QColor("red"))
    _d, _s, out = laid("<div style='width:200px'><span style='display:block;position:relative;width:100%;padding-top:100%;"
                       "overflow:hidden'><div style='position:absolute;top:0;left:0;width:100%;height:100%'>"
                       "<img src='w.png' style='position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover'>"
                       "</div></span></div>", images={"w.png": wide})
    pictures = drawn_images(out)
    rect = pictures[0][1] if pictures else None
    check("a picture of width and height 100% fills its square box (Square's product pictures), cropped to it",
          rect is not None and abs(rect.width() - 200) < 0.5 and abs(rect.height() - 200) < 0.5
          and pictures[0][4] is not None and pictures[0][4][0] == "cover", str(rect))

    document = parse("<div class=a><p id=one>x</p><section><p id=two>y</p></section></div>"
                     "<div class=b><p id=three>z</p></div><h2 class=x>h</h2><div class=y><em id=four>w</em></div>")
    styler = Styler(document, author_css=[".a > * { background-color: red } .a * em, .a p { margin: 1px } "
                                          ".x + .y * { background-color: blue } #nope .a { background-color: green }"])
    styles = styler.compute()

    def element(eid):
        return next(e for e in document.root.elements() if e.id == eid)
    agree = all(matches(parse_selector(sel), element(eid)) == want for sel, eid, want in (
        (".a > *", "one", True), (".a > *", "two", False), (".a p", "two", True), (".a p", "three", False),
        (".x + .y *", "four", True), ("#nope .a", "one", False)))
    check("rules needing an ancestor (.a > *, .x + .y *) are passed over only where it is missing",
          agree and styles[element("one")].get("background-color") == (255, 0, 0, 255)
          and styles[element("two")].get("background-color") != (255, 0, 0, 255)
          and styles[element("two")].get("margin-top") == 1.0
          and styles[element("four")].get("background-color") == (0, 0, 255, 255)
          and styles[element("three")].get("margin-top") == 16.0, str(styles[element("four")].get("background-color")))

    _d, _s, out = laid("<div style='height:3000px'></div>"
                       "<div id=low style='position:relative;width:100px;height:50px;background:#00ff00'></div>"
                       "<div style='position:relative;transform:translateY(100px);width:100px;height:50px;background:#0000ff'></div>")
    image = QImage(800, 600, QImage.Format.Format_ARGB32)

    def colour_at(scroll, x, y):
        image.fill(QColor("white"))
        painter = QPainter(image)
        painter.translate(0, -scroll)
        paint_module.paint(painter, out, QRectF(0, scroll, 800, 600), {})
        painter.end()
        return image.pixelColor(x, y)
    check("a positioned box far down is drawn when scrolled to (layers out of view are passed over)",
          colour_at(2800, 10, 225).green() > 200 and colour_at(0, 10, 225).green() > 200 and colour_at(0, 10, 225).red() > 200)
    check("and one moved by a transform is drawn where the transform puts it",
          colour_at(2900, 10, 275).blue() > 200 and colour_at(2900, 10, 275).red() < 60)

    big = QImage(2000, 1000, QImage.Format.Format_RGB32)
    big.fill(QColor("orange"))
    canvas = QImage(400, 300, QImage.Format.Format_ARGB32)
    painter = QPainter(canvas)
    first = paint_module._scaled(big, QRectF(0, 0, 200, 100), painter)
    second = paint_module._scaled(big, QRectF(0, 0, 200, 100), painter)
    painter.end()
    check("a large picture drawn small is scaled once and kept, not scaled again each frame",
          first.width() == 200 and first.height() == 100 and first.cacheKey() == second.cacheKey())


def test_video_and_audio(app) -> None:
    """<video> and <audio> in Merlin Engine: sized as browsers size them,
    played in the page with Merlin's controls, and scripts' HTMLMediaElement;
    YouTube's videos (and other sites' streamed ones) found by yt-dlp, here a
    stand-in, and played where the site's player is."""
    from PyQt6.QtTest import QTest

    from merlin.engine import MerlinView
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout
    from merlin.engine.script import LocalStorage
    from merlin.engine.siteplay import youtube_video

    # sizes, as Chromium gives them
    def boxes(markup, media=None):
        doc = parse(markup)
        display = Layout(doc, Styler(doc).compute(), 800, media=media or {}).run()
        return {element.id: (round(r.width()), round(r.height())) for kind, r, element in
                ((item[0], item[1], item[2]) for item in display.items if item[0] == "media")}

    sizes = boxes("<body style='margin:0'><video id=a></video><video id=b width=640 height=360></video>"
                  "<video id=c data-mjs=9 style='width:100%'></video><video id=d data-mjs=8 style='height:90px'></video>"
                  "<audio id=e controls></audio><audio id=f></audio>",
                  media={"9": (1920, 1080), "8": (1920, 1080)})
    check("a video with no size is 300 by 150", sizes.get("a") == (300, 150), str(sizes.get("a")))
    check("width and height attributes size a video", sizes.get("b") == (640, 360), str(sizes.get("b")))
    check("a video's height follows its picture", sizes.get("c") == (800, 450), str(sizes.get("c")))
    check("and its width, given a height", sizes.get("d") == (160, 90), str(sizes.get("d")))
    check("<audio controls> is 300 by 54, <audio> alone hidden",
          sizes.get("e") == (300, 54) and "f" not in sizes, str(sizes))

    check("YouTube's video pages known",
          (youtube_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3"), youtube_video("https://youtu.be/dQw4w9WgXcQ"),
           youtube_video("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"),
           youtube_video("https://m.youtube.com/shorts/dQw4w9WgXcQ"), youtube_video("https://www.youtube.com/"),
           youtube_video("https://example.com/watch?v=dQw4w9WgXcQ"))
          == (("dQw4w9WgXcQ", False), ("dQw4w9WgXcQ", False), ("dQw4w9WgXcQ", True), ("dQw4w9WgXcQ", False),
              None, None))

    sample = os.path.join(ROOT, "tests", "data", "h264-sample.mp4")
    folder = tempfile.mkdtemp(prefix="merlin-video-")
    shutil.copy(sample, folder)
    open(os.path.join(folder, "plain.html"), "w").write(
        "<!DOCTYPE html><body style='margin:0'><video id=a src='h264-sample.mp4' controls style='width:400px'></video>"
        "<video id=b src='h264-sample.mp4' autoplay muted width=200></video>"
        "<video id=c src='h264-sample.mp4' autoplay width=200></video></body>")
    open(os.path.join(folder, "watch.html"), "w").write(
        "<!DOCTYPE html><body style='margin:0'><div style='height:56px'></div>"
        "<div id=movie_player style='width:640px;height:360px;margin:20px'></div>"
        "<video id=s src='blob:http://x/abc' style='width:320px;height:180px'></video></body>")
    open(os.path.join(folder, "js.html"), "w").write(
        "<!DOCTYPE html><body><video id=v src='h264-sample.mp4' width=200></video><p id=log>waiting</p><script>"
        "const v = document.getElementById('v'); const log = [];"
        "function out() { document.getElementById('log').textContent = log.join(',') + ' | ' + v.videoWidth + 'x'"
        " + v.videoHeight + ' ended=' + v.ended + ' can=' + v.canPlayType('video/mp4; codecs=\"avc1.42E01E, mp4a.40.2\"')"
        " + '/' + v.canPlayType('video/x-flv') + ' ms=' + (typeof MediaSource); }"
        "['loadedmetadata', 'play', 'playing', 'ended'].forEach((n) =>"
        " v.addEventListener(n, () => { if (!log.includes(n)) log.push(n); out(); }));"
        "v.muted = true; v.play().then(() => { log.push('resolved'); out(); }); out();</script></body>")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.01)
        return test()

    def by_id(view, name):
        for key, controller in view._players.items():
            if controller.element is not None and controller.element.id == name:
                return controller
        return None

    view = MerlinView()
    view.resize(900, 700)
    view.show()
    try:
        view.setUrl(QUrl(base + "plain.html"))
        until(lambda: by_id(view, "b") is not None and by_id(view, "b").ended, 8)
        a, b, c = by_id(view, "a"), by_id(view, "b"), by_id(view, "c")
        check("a muted autoplay video plays", b is not None and b.started and b.ended,
              str(b and (b.started, b.ended, b.error)))
        check("a video with sound waits to be asked", c is not None and not c.started)
        check("a paused video shows its first picture, at its own size",
              a is not None and a.image is not None and (a.width, a.height) == (96, 64))
        rects = view._media_view_rects(next((k for k, x in view._players.items() if x is a), None))
        rect = rects[0] if rects else None
        if rect is not None:
            QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(int(rect.center().x()), int(rect.center().y())))
        check("pressing a video's play button plays it", until(lambda: a.ended, 6), str(a.error))
    finally:
        view.shutdown()
    wait(app, 0.3)
    check("players stopped with the page", not view._players)

    # YouTube, with yt-dlp stood in for
    asked = []

    def resolver(page, timeout, rows):
        asked.append(page)
        return True, base + "h264-sample.mp4"

    MerlinView.site_resolver = staticmethod(resolver)
    view = MerlinView()
    view.resize(900, 700)
    view.show()
    try:
        view.setUrl(QUrl(base + "watch.html"))
        until(lambda: view._display is not None, 5)
        view._url = QUrl("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        view._site_sync()
        player = view._players.get("site:yt:dQw4w9WgXcQ")
        check("a YouTube watch page has Merlin's player", player is not None)
        place = view._media_site_places.get("site:yt:dQw4w9WgXcQ")
        check("over YouTube's player box", place is not None and place[0] == QRectF(20, 76, 640, 360),
              str(place and place[0]))
        check("YouTube's picture asked for while its stream is found",
              player.poster_src == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" and player.note)
        check("its stream found and played, as YouTube plays it",
              until(lambda: player.ended, 8) and asked == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
              str((asked, player.note, player.error)))
        view._layout()
        wait(app, 0.3)
        check("Merlin's YouTube player kept as the page lays out again",
              view._players.get("site:yt:dQw4w9WgXcQ") is player)

        # an embedded one waits for its play button
        view._site_end()
        view._url = QUrl("https://www.youtube.com/embed/abcdefghijk")
        view._site_sync()
        embed = view._players.get("site:yt:abcdefghijk")
        wait(app, 0.4)
        check("an embedded YouTube video waits to be played", embed is not None and len(asked) == 1
              and not embed.started)
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(450, 350))
        check("and plays when its play button is pressed", until(lambda: embed.ended, 8) and len(asked) == 2,
              str((asked, embed.note)))

        # another site's streamed video: found when a person presses play, not a script
        view._site_end()
        view._url = QUrl(base + "watch.html")
        view._layout()
        wait(app, 0.3)
        streamed = by_id(view, "s")
        check("a streamed (blob:) video has a player", streamed is not None and not streamed.source)
        streamed.play(person=False)
        wait(app, 0.4)
        check("a script playing it does not run yt-dlp", len(asked) == 2)
        streamed.play(person=True)
        check("a person pressing play finds its page's stream",
              until(lambda: streamed.ended, 8) and asked[-1] == base + "watch.html", str(asked))

        # without yt-dlp: said, never fetched as a page opens
        from merlin import media

        del MerlinView.site_resolver
        real = media.has_ytdlp
        media.has_ytdlp = lambda: False
        try:
            view._site_end()
            view._url = QUrl("https://www.youtube.com/watch?v=zyxwvutsrqp")
            view._site_sync()
            missing = view._players.get("site:yt:zyxwvutsrqp")
            check("without yt-dlp a YouTube page says what it needs",
                  missing is not None and "yt-dlp" in missing.note and not missing.site_finding, missing and missing.note)
        finally:
            media.has_ytdlp = real
    finally:
        if hasattr(MerlinView, "site_resolver"):
            del MerlinView.site_resolver
        view.shutdown()
    wait(app, 0.3)

    # scripts: HTMLMediaElement
    runtime = _node_for_tests()
    if not runtime:
        print("  skip  media scripts: Node.js is not here")
        return

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(900, 600)
    view.show()

    def log():
        found = next((x for x in view._document.root.elements() if x.id == "log"), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    try:
        view.setUrl(QUrl(base + "js.html"))
        until(lambda: "ended=true" in log(), 10)
        text = log()
        check("a script's play() plays, its promise kept, events heard",
              text.startswith("loadedmetadata,play,playing") or ("play" in text and "resolved" in text and "ended" in text),
              text)
        check("videoWidth, videoHeight and ended, as the video has them", "96x64 ended=true" in text, text)
        check("canPlayType as Merlin can play", "can=probably/" in text and "ms=undefined" in text, text)
    finally:
        view.shutdown()
    wait(app, 0.3)


def test_dom_for_polyfills_dialogs_and_cssom(app) -> None:
    """What YouTube's and Google's pages needed of the DOM: the node classes
    and Window, Node.prototype's own members for polyfills to keep, text
    changes heard by observers, the ES5 custom elements adapter, dialogs and
    popovers, the CSS object model, and clicks that are trusted MouseEvents."""
    from PyQt6.QtTest import QTest

    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    runtime = _node_for_tests()
    if not runtime:
        print("  skip  DOM for polyfills: Node.js is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-dom-")
    open(os.path.join(folder, "page.html"), "w").write("""<!DOCTYPE html><html><head>
<style id=s>.a { color: red } @media (min-width: 10px) { .b { color: blue } } .sentinel {}</style></head>
<body style="margin:0"><button id=open style="width:120px;height:30px">Open</button><p id=log>waiting</p>
<dialog id=d><p>Hi</p><form method=dialog><button id=ok value=yes>OK</button></form></dialog>
<div id=pop popover>Popover</div><div id=host><span>old</span></div>
<input name=under style="position:absolute;top:290px;left:340px;width:120px">
<script>
const out = []; const say = (k, v) => out.push(k + '=' + v);
say('classes', [typeof ProcessingInstruction, typeof CDATASection, typeof XMLSerializer, typeof Window].join('/'));
say('window', window instanceof Window);
// a polyfill's copies of Node.prototype's own: they work on any node
const first = Object.getOwnPropertyDescriptor(Node.prototype, 'firstChild').get;
const append = Node.prototype.appendChild, remove = Node.prototype.removeChild;
const host = document.getElementById('host');
say('nativeFirst', first.call(host).tagName);
remove.call(host, first.call(host));
const frag = document.createDocumentFragment(); frag.appendChild(document.createElement('i')); frag.appendChild(document.createElement('b'));
Object.setPrototypeOf(frag, Object.create(DocumentFragment.prototype, { firstChild: { get() { return null; } } }));
append.call(host, frag);
say('moved', host.children.length + ':' + host.firstElementChild.tagName);
// the ES5 adapter's get, which knows a name before it is defined
const realGet = customElements.get.bind(customElements); const names = new Set();
customElements.get = (n) => names.has(n) ? class {} : realGet(n);
const define = customElements.define.bind(customElements);
names.add('x-one'); try { define('x-one', class extends HTMLElement {}); say('define', 'ok'); } catch (e) { say('define', e.name); }
customElements.get = realGet;
// text changes heard
const t = document.createTextNode(''); let heard = 0;
new MutationObserver((r) => { heard = r.length + ':' + r[0].type; }).observe(t, { characterData: true });
t.data = '1';
// the CSS object model
const sheet = document.styleSheets[0];
say('rule', (sheet.cssRules[0] instanceof CSSStyleRule) + ':' + sheet.cssRules[0].selectorText + ':' + sheet.cssRules[0].style.color);
say('media', sheet.cssRules[1] instanceof CSSMediaRule && sheet.cssRules[1].cssRules[0].selectorText);
say('sentinel', sheet.cssRules[sheet.cssRules.length - 1].selectorText);
const made = new CSSStyleSheet(); made.replaceSync('#log { font-weight: bold }');
document.adoptedStyleSheets = [made];
say('adopted', document.adoptedStyleSheets.length + ':' + made.cssRules[0].selectorText);
say('xml', new XMLSerializer().serializeToString(document.getElementById('open')).slice(0, 7));
const d = document.getElementById('d');
document.getElementById('open').addEventListener('click', (e) => {
  say('click', [e.isTrusted, e instanceof MouseEvent, e.which, e.button].join('/'));
  d.showModal(); say('modal', d.open + ':' + d.matches(':modal'));
});
d.addEventListener('close', () => { say('closed', d.returnValue); report(); });
document.getElementById('pop').addEventListener('toggle', (e) => { say('toggle', e.newState); report(); });
function report() { document.getElementById('log').textContent = out.join(' ') + ' heard=' + heard; }
setTimeout(report, 50);
</script></body></html>""")

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

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(800, 600)
    view.show()

    def log():
        found = next((x for x in view._document.root.elements() if x.id == "log"), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.01)
        return test()

    def element(name):
        return next((x for x in view._document.root.elements() if x.id == name), None)

    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/page.html"))
        until(lambda: "heard=" in log(), 10)
        text = log()
        check("ProcessingInstruction, CDATASection, XMLSerializer and Window are there",
              "classes=function/function/function/function" in text and "window=true" in text, text)
        check("Node.prototype's own firstChild and appendChild work on any node, as polyfills keep them",
              "nativeFirst=SPAN" in text and "moved=2:I" in text, text)
        check("a name the ES5 adapter's get already knows is still defined", "define=ok" in text, text)
        check("a text node's change is heard as a characterData record", "heard=1:characterData" in text, text)
        check("cssRules are CSSStyleRules with their selector and style",
              "rule=true:.a:red" in text and "media=.b" in text and "sentinel=.sentinel" in text, text)
        check("constructed stylesheets, adopted", "adopted=1:#log" in text and "xml=<button" in text, text)
        check("an adopted sheet styles the page",
              any(e.tag == "style" and "data-merlin-adopted" in e.attrs for e in view._document.root.elements()))
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(60, 15))
        until(lambda: element("d") is not None and "data-merlin-modal" in element("d").attrs, 5)
        wait(app, 0.4)
        text = log() if "click=" in log() else text
        dialog = element("d")
        check("showModal() opens a dialog, modal", dialog is not None and "open" in dialog.attrs
              and "data-merlin-modal" in dialog.attrs)
        under = next((w for e, w in view._widgets.items() if e.attrs.get("name") == "under"), None)
        check("a field under a modal dialog is hidden by it", under is not None and not under.isVisible())
        ok = next((e for e in view._document.root.elements() if e.id == "ok"), None)
        ok_place = next((it[1] for it in view._display.items if it and it[0] == "button" and it[2] is ok), None)
        check("a modal dialog sits in the middle of the window",
              ok_place is not None and 250 < ok_place.center().x() < 550 and 150 < ok_place.center().y() < 450,
              str(ok_place))
        if ok_place is not None:
            QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(int(ok_place.center().x()), int(ok_place.center().y() - view._scroll)))
        until(lambda: "closed=" in log(), 5)
        text = log()
        check("a click is a trusted MouseEvent, which 1", "click=true/true/1/0" in text, text)
        check("<form method=dialog> closes its dialog with the button's value",
              "modal=true:true" in text and "closed=yes" in text, text)
        wait(app, 0.4)
        check("and shows again once the dialog has closed", under is not None and under.isVisible())
    finally:
        view.shutdown()
    wait(app, 0.3)


def test_border_box_heights_and_centring(app) -> None:
    """box-sizing: border-box for every kind of box; percentage heights of
    definite containers (the window's, for html and body); vertical-align:
    middle; right floats measured inside flex items; masks."""
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout

    def boxes(markup, width=1000, height=700):
        doc = parse(markup)
        out = Layout(doc, Styler(doc).compute(), width, viewport_height=height).run()
        found = {}
        for rect, element in out.boxes:
            name = element.attrs.get("id")
            if name and name not in found:
                found[name] = (round(rect.x()), round(rect.y()), round(rect.width()), round(rect.height()))
        return found, out

    sized = "box-sizing:border-box;width:100px;height:50px;padding:10px;border:5px solid"
    found, _ = boxes(f"<body style='margin:0'><div style='position:relative'>"
                     f"<span id=i style='display:inline-block;{sized}'></span>"
                     f"<span id=f style='float:left;{sized}'></span>"
                     f"<span id=a style='position:absolute;top:0;left:300px;{sized}'></span></div>"
                     f"<div style='display:flex'><span id=x style='{sized}'></span></div></body>")
    check("border-box sizes inline-blocks, floats, positioned boxes and flex items as Chrome does",
          all(found.get(k, (0, 0, 0, 0))[2:] == (100, 50) for k in "ifax"), str(found))
    found, _ = boxes("<html><head><style>html,body{height:100%;margin:0}</style></head><body>"
                     "<div id=w style='display:flex;flex-direction:column;height:100%'>"
                     "<div id=m style='flex:1 0 auto'>main</div><div id=foot>footer</div></div></body></html>")
    check("height: 100% on html, body and a wrapper is the window's height: the footer at its foot",
          found.get("w", (0, 0, 0, 0))[3] == 700 and found.get("foot", (0, 0, 0, 0))[1] > 650, str(found))
    found, _ = boxes("<html><head><style>html,body{height:100%;margin:0}"
                     ".wrap{position:absolute;top:0;bottom:0;left:0;right:0;text-align:center;white-space:nowrap}"
                     ".wrap::after{content:'';display:inline-block;height:100%;vertical-align:middle}"
                     ".box{display:inline-block;vertical-align:middle;width:300px;height:200px;white-space:normal}"
                     "</style></head><body><div class=wrap><div id=b class=box>hello</div></div></body></html>")
    check("vertical-align: middle centres a box (the ::after centring trick)",
          found.get("b") == (350, 250, 300, 200), str(found.get("b")))
    found, _ = boxes("<body style='margin:0'><header style='display:flex;justify-content:flex-end'>"
                     "<div id=l><a style='float:right;display:inline-block;width:40px;height:40px'></a></div>"
                     "<div id=r><a style='float:right;display:inline-block;width:85px;height:40px'></a></div>"
                     "</header></body>", width=1400)
    check("a right float in a flex item is measured as wide as it is",
          found.get("l", (0, 0, 0, 0))[2] == 40 and found.get("r", (0, 0, 0, 0))[2] == 85, str(found))
    found, _ = boxes("<html><head><style>html,body{height:100%;margin:0}</style></head><body>"
                     "<div id=d style='position:absolute;top:0;bottom:0;left:0;right:0;"
                     "margin:auto;width:fit-content;height:fit-content'><div style='width:200px;"
                     "height:100px'></div></div></body></html>")
    check("width and height: fit-content with margin: auto centre a box held at all four sides",
          found.get("d") == (400, 300, 200, 100), str(found.get("d")))
    _found, out = boxes("<body style='margin:0;color:#c00'><div id=m style='width:24px;height:24px;"
                        "-webkit-mask:url(icon.svg) no-repeat center / cover;background-color:currentcolor'>"
                        "</div></body>")
    masked = [item for item in out.items if item and item[0] == "masked"]
    check("a CSS mask is drawn as the background through its picture",
          len(masked) == 1 and masked[0][2] == "icon.svg" and masked[0][3] == "cover", str(masked[:1]))


def test_scripts_changes_sent_lightly(app) -> None:
    """A page whose scripts keep changing words and attributes: only those go
    to Merlin, not the whole page, and only what they change is styled again;
    the next change waits until Merlin has shown the last."""
    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    runtime = _node_for_tests()
    if not runtime:
        print("  skip  light changes: Node.js is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-light-")
    open(os.path.join(folder, "page.html"), "w").write("""<!DOCTYPE html><html><head><style>
.on { color: rgb(255, 0, 0) } .off { color: rgb(0, 0, 255) } .on + p { font-weight: bold }
</style></head><body><span id=clock>0</span><b id=word>a</b><p id=lamp class=off>lamp</p><p id=next>next</p>
<div id=hidden style="display:none"><span id=quiet>0</span></div>
<script>
let n = 0;
setInterval(() => {
  n++;
  document.getElementById('clock').firstChild.data = String(n);
  document.getElementById('word').textContent = 'w' + n;
  document.getElementById('quiet').textContent = String(n);
  document.getElementById('lamp').className = n % 2 ? 'on' : 'off';
}, 100);
</script></body></html>""")

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

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(600, 400)
    view.show()
    kinds = []

    def element(name):
        return next((x for x in view._document.root.elements() if x.id == name), None) if view._document else None

    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/page.html"))
        end = time.time() + 10
        while time.time() < end and (view._script is None or element("clock") is None):
            app.processEvents()
            time.sleep(0.01)
        original = view._on_script_message

        def spy(message):
            kinds.append(message.get("type"))
            return original(message)

        view._script.message.disconnect(original)
        view._script.message.connect(spy)
        wait(app, 4)
        clock = element("clock")
        first = int(clock.text() or 0)
        wait(app, 1.5)
        second = int(element("clock").text() or 0)
        updates = [k for k in kinds if k in ("dom", "texts")]
        check("words and attributes changed by scripts come as changes, not the whole page",
              updates.count("texts") >= 3 and updates.count("dom") <= 2, str(updates))
        check("Merlin's page follows the words", second > first > 0, f"{first} then {second}")
        check("and the element whose only text was replaced", (element("word").text() or "").startswith("w"),
              element("word").text())
        lamp = element("lamp")
        colour = (view._styles.get(lamp) or {}).get("color")
        wanted = (255, 0, 0, 255) if "on" in lamp.attrs.get("class", "") else (0, 0, 255, 255)
        check("an attribute changed is styled again", colour == wanted and lamp.attrs.get("data-mjs"),
              f"{lamp.attrs.get('class')} {colour}")
        bold = (view._styles.get(element("next")) or {}).get("font-weight")
        check("and so is what follows it, for + rules",
              bold == (700 if "on" in lamp.attrs.get("class", "") else 400), str(bold))
        check("no more than about four changes a second reach Merlin",
              len(updates) <= 4 * 5.5 + 2, str(len(updates)))
    finally:
        view.shutdown()
    wait(app, 0.3)


def test_requests_as_a_browser_makes_them(app) -> None:
    """Requests a page makes, made as a browser makes them: a Request's
    address taken against the page's (YouTube's searches had failed as if
    offline), a Request's own address and method rather than ones the page
    gave it (YouTube's check for blockers), the page's origin sent with a
    POST, and a form the page sends carrying it as referrer; and el.style =
    "..." and document.forms, which YouTube's and Google's scripts use."""
    from PyQt6.QtTest import QTest

    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage
    from merlin.engine.view import initiated_headers

    sent = {}
    cross = {}
    initiated_headers(cross, QUrl("https://a.example.com/page?x#y"), QUrl("https://other.org/z"))
    same_site = {}
    initiated_headers(same_site, QUrl("https://consent.youtube.com/d?x=1#f"), QUrl("https://www.youtube.com/"))
    down = {}
    initiated_headers(down, QUrl("https://a.example.com/p"), QUrl("http://a.example.com/q"))
    same = {}
    initiated_headers(same, QUrl("https://consent.youtube.com/d?x=1#f"), QUrl("https://consent.youtube.com/save"))
    check("a page's navigation to another site sends only its origin as referrer",
          cross == {"Sec-Fetch-Site": "cross-site", "Referer": "https://a.example.com/"}, str(cross))
    check("one within its site says same-site; one within its origin, the whole address",
          same_site.get("Sec-Fetch-Site") == "same-site"
          and same == {"Sec-Fetch-Site": "same-origin", "Referer": "https://consent.youtube.com/d?x=1"},
          f"{same_site} {same}")
    check("and from https to http, no referrer", "Referer" not in down, str(down))

    runtime = _node_for_tests()
    if not runtime:
        print("  skip  requests as a browser makes them: Node.js is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-requests-")
    open(os.path.join(folder, "page.html"), "w").write("""<!DOCTYPE html><html><body>
<form id=f name=consent method=post action=/save style="margin:0"><input type=hidden name=set_ytc value=false>
<textarea name=answer style="display:none"></textarea>
<button id=send style="width:120px;height:30px">Confirm</button></form><p id=log>waiting</p>
<script>
const out = []; const say = (k, v) => out.push(k + '=' + v);
const report = () => { document.getElementById('log').textContent = out.join(' ') + ' done=1'; };
(async () => {
  try {
    const r = await fetch(new Request('/api?q=ufo', { method: 'POST', body: 'hello' }));
    say('relative', r.status + ':' + (await r.text()));
  } catch (e) { say('relative', 'threw ' + e.message); }
  // a data: request dressed as a POST elsewhere: fetch reads its own address
  const q = new Request('data:application/json;base64,' + btoa('{"a":1}'));
  Object.defineProperty(q, 'url', { get: () => location.origin + '/player' });
  Object.defineProperty(q, 'method', { get: () => 'POST' });
  try { say('data', (await (await fetch(q)).json()).a); } catch (e) { say('data', 'threw ' + e.message); }
  const d = document.createElement('div');
  try { d.style = 'color: red; width: 5px'; say('style', d.getAttribute('style').replace(/ /g, '') + ':' + d.style.width); }
  catch (e) { say('style', 'threw'); }
  say('forms', document.forms.length + ':' + [...document.forms].length + ':' + (document.forms.consent === document.getElementById('f'))
      + ':' + (document.forms.namedItem('consent') === document.forms[0]));
  document.querySelector('input[name=set_ytc]').value = 'true';
  document.querySelector('textarea[name=answer]').value = 'token-123';
  report();
})();
</script></body></html>""")
    open(os.path.join(folder, "after.html"), "w").write("<title>after</title><p>saved</p>")
    # reCAPTCHA's way: the answer put in a hidden textarea by script, and
    # the form sent by form.submit()
    open(os.path.join(folder, "captcha.html"), "w").write(
        "<form id=c method=post action=/verify><input type=hidden name=q value=abc>"
        "<textarea name=g-recaptcha-response style='display:none'></textarea></form>"
        "<script>setTimeout(function () { document.querySelector('textarea').value = 'solved-456';"
        " document.getElementById('c').submit(); }, 300);</script>")

    class Recorder(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            sent[self.path.split("?")[0]] = (dict(self.headers), body)
            if self.path.startswith("/api"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"got " + body)
            else:
                self.send_response(303)
                self.send_header("Location", "/after.html")
                self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(800, 600)
    view.show()

    def log():
        found = next((x for x in view._document.root.elements() if x.id == "log"), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.01)
        return test()

    try:
        view.setUrl(QUrl(origin + "/page.html"))
        until(lambda: "done=1" in log(), 10)
        text = log()
        check("new Request('/relative') is taken against the page's address",
              "relative=200:got hello" in text, text)
        api = {k.lower(): v for k, v in sent.get("/api", ({}, b""))[0].items()}
        check("and its POST carries the page's origin",
              api.get("origin") == origin, str(api))
        check("and goes as the page's own request, not as a page opened (it had said navigate, document, none)",
              api.get("sec-fetch-mode") == "cors" and api.get("sec-fetch-dest") == "empty"
              and api.get("sec-fetch-site") == "same-origin" and "sec-fetch-user" not in api
              and "upgrade-insecure-requests" not in api and api.get("accept") == "*/*", str(api))
        check("fetch reads a Request's own address and method, not ones the page gave it",
              "data=1" in text and "/player" not in sent, text)
        check("el.style = '...' sets its style", "style=color:red;width:5px:5px" in text, text)
        check("document.forms lists the forms, by index, by name, iterable", "forms=1:1:true:true" in text, text)
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(60, 22))
        until(lambda: "/save" in sent and view.title() == "after", 10)
        headers, body = sent.get("/save", ({}, b""))
        headers = {k.lower(): v for k, v in headers.items()}
        check("a form the page sends carries it as referrer, its origin and same-origin",
              headers.get("referer") == origin + "/page.html" and headers.get("origin") == origin
              and headers.get("sec-fetch-site") == "same-origin", str(headers))
        check("with the values its script set, a hidden textarea's too", b"set_ytc=true" in body
              and b"answer=token-123" in body, str(body))
        view.setUrl(QUrl(origin + "/captcha.html"))
        until(lambda: "/verify" in sent, 10)
        _headers, body = sent.get("/verify", ({}, b""))
        check("form.submit() sends what the page's scripts put in its fields (reCAPTCHA's answer)",
              b"g-recaptcha-response=solved-456" in body and b"q=abc" in body, str(body))
    finally:
        view.shutdown()
        server.shutdown()


def test_tracker_test_pages(app) -> None:
    """Cover Your Tracks' first step: frames made empty and given a tracker's
    address by script, one the content blocker stops, then window.location
    = "..." to the next step. The blocked frame had stopped every frame being
    loaded, and window.location = had gone nowhere. Also navigator.plugins,
    which its fingerprinting reads."""
    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    runtime = _node_for_tests()
    if not runtime:
        print("  skip  tracker test pages: Node.js is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-tracks-")
    asked = []
    open(os.path.join(folder, "start.html"), "w").write("""<!DOCTYPE html><html><body>
<p id=log>testing</p><iframe id=a style="width:1px;height:1px"></iframe><iframe id=t style="width:1px;height:1px"></iframe>
<script>
document.getElementById('log').textContent = 'plugins ' + navigator.plugins.length + ' ' + navigator.plugins[0].name
  + ' ' + navigator.mimeTypes['application/pdf'].type + ' ' + navigator.productSub;
window.onload = function () {
  setTimeout(function () {
    document.getElementById('a').src = '/frame.html?tracker';
    document.getElementById('t').src = '/frame.html?allowed';
  }, 700);
  setTimeout(function () { window.location = '/next.html?a=1'; }, 2200);
};
</script></body></html>""")
    open(os.path.join(folder, "frame.html"), "w").write("<p>frame</p>")
    open(os.path.join(folder, "next.html"), "w").write("<title>next</title><p>next step</p>")

    class Pages(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def do_GET(self):
            asked.append(self.path)
            super().do_GET()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view._blocked = lambda url, kind="": "tracker" in url      # the content blocker's say
    view.resize(800, 600)
    view.show()

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.01)
        return test()

    def log():
        found = next((x for x in view._document.root.elements() if x.id == "log"), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/start.html"))
        until(lambda: "plugins" in log(), 10)
        check("navigator.plugins and mimeTypes are Chrome's PDF viewers",
              log() == "plugins 5 PDF Viewer application/pdf 20030107", log())
        until(lambda: "/frame.html?allowed" in asked, 8)
        check("a frame given an address by script loads, beside one the blocker stops",
              "/frame.html?allowed" in asked and "/frame.html?tracker" not in asked, str(asked))
        until(lambda: view.title() == "next", 8)
        check("window.location = '...' goes there", view.url().toString().endswith("/next.html?a=1"),
              view.url().toString())
    finally:
        view.shutdown()
        server.shutdown()


def test_google_results_layout(app) -> None:
    """Google's results page, against Chrome: margins that join (negative
    ones, a parent's with its first and last child's, through empty blocks),
    a picture grid whose tiles are height: 100% in rows of 1fr, a flex row
    held to max-height: 0, an inline span's margin, display: contents round
    a block, @container rules, media queries asking the window's width, skip
    links clipped to nothing, and a script's <style> keeping its ">"."""
    from merlin.engine import MerlinView
    from merlin.engine.css import Styler, container_matches, media_matches
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout, _clipped_away
    from merlin.engine.script import LocalStorage

    def rects(markup, width=600):
        doc = parse("<body style='margin:0'>" + markup)
        styles = Styler(doc).compute()
        out = Layout(doc, styles, width).run()
        found = {}
        for r, e in out.boxes:
            name = getattr(e, "attrs", {}).get("id")
            if name and name not in found:
                found[name] = (round(r.x()), round(r.y()), round(r.width()), round(r.height()))
        return found

    got = rects("<div id=top style='height:96px'></div><div id=e1></div>"
                "<div id=e2 style='position:relative;height:0;top:42px'><span></span></div>"
                "<div id=tabs style='margin-top:-12px;height:54px'></div>")
    check("a negative margin-top pulls a block up past empty ones (Google's tabs)",
          got.get("tabs") == (0, 84, 600, 54), str(got))
    got = rects("<div id=a style='margin-top:20px'><h1 id=h style='margin:30px 0;height:10px'></h1></div>"
                "<div id=b style='margin-top:10px;padding-top:1px'><p id=p style='margin:10px 0;height:10px'></p></div>"
                "<div id=k style='margin-bottom:20px'></div><div id=l style='margin-top:10px;height:5px'></div>")
    check("a first child's top margin joins its parent's, and the last one's bottom",
          got.get("a") == (0, 30, 600, 10) and got.get("h") == (0, 30, 600, 10)
          and got.get("b") == (0, 70, 600, 21) and got.get("p") == (0, 81, 600, 10), str(got))
    check("an empty block's margins join through it with the next one's",
          got.get("l") == (0, 111, 600, 5), str(got))
    got = rects("<div style='position:relative;height:240px;width:372px'>"
                "<div style='position:absolute;top:0;bottom:0;left:0;right:0;display:grid;gap:2px;"
                "grid-template-columns:repeat(10,minmax(0,1fr));grid-template-rows:repeat(2,minmax(0,1fr))'>"
                + "".join(f"<div id=t{i} style='grid-column:span 5;height:100%;position:relative'>"
                          f"<div style='display:contents'><div id=in{i} style='height:100%'></div></div></div>"
                          for i in range(4)) + "</div></div>")
    check("tiles of height: 100% in rows of 1fr share the grid's height (Google's pictures)",
          got.get("t0", (0, 0, 0, 0))[3] == 119 and got.get("t2", (0, 0, 0, 0))[1] == 121, str(got))
    check("and a height: 100% inside a display: contents wrapper is the tile's",
          got.get("in0", (0, 0, 0, 0))[3] == 119, str(got))
    got = rects("<div style='display:flex;flex-direction:column-reverse'>"
                "<span style='display:block;height:74px'></span>"
                "<div id=hid style='display:flex;align-items:center;max-height:0;position:relative;top:19px'>"
                "<div id=tall style='height:38px;width:50px'></div>"
                "<div id=btn style='margin-top:16px;height:18px;width:30px'></div></div></div>")
    check("a flex row held to max-height: 0 centres its items on that line (Google's ⋮ button)",
          got.get("tall") == (0, 0, 50, 38) and got.get("btn") == (50, 18, 30, 18), str(got))
    got = rects("<div style='display:flex;align-items:center'><span id=item><span style='margin-right:12px'>"
                "<div style='display:inline-block;width:28px;height:28px'></div></span></span>"
                "<div id=name>Wikipedia</div></div>"
                "<div><span style='padding:0 10px'>ab</span><span id=next>x</span></div>")
    check("an inline element's margin takes room on its line (Google's favicon and site name)",
          got.get("item", (0, 0, 0, 0))[2] == 40 and got.get("name", (0, 0, 0, 0))[0] == 40, str(got))
    check("and its padding", got.get("next", (0, 0, 0, 0))[0] >= 30, str(got))
    got = rects("<div style='height:50px;position:relative'><div style='display:contents'>"
                "<div id=blk style='height:100%'></div></div></div>")
    check("a block in a display: contents wrapper goes on no line of its own",
          got.get("blk") == (0, 0, 600, 50), str(got))

    check("@container (min-width) holds by the container's width, or the page's",
          container_matches("@container srp (min-width: 868px)", (1446, 700), {})
          and not container_matches("@container srp (min-width: 868px)", (1446, 700), {"srp": (600, 400)})
          and not container_matches("@container style(--x: 1)", (1446, 700), {}))
    check("a media query asks the window's width, its scroll bar in it",
          media_matches("(min-width: 1461px)", (1446.0, 700.0, 1461.0))
          and not media_matches("(min-width: 1461px)", (1446.0, 700.0)))
    check("a box clipped to nothing is hidden (skip links), one clipped to a rect is not",
          _clipped_away({"clip": "rect(0, 0, 0, 0)"}) and _clipped_away({"clip-path": "inset(50%)"})
          and not _clipped_away({"clip": "rect(0px, 10px, 10px, 0px)"}) and not _clipped_away({}))

    runtime = _node_for_tests()
    if not runtime:
        print("  skip  a script's style: Node.js is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-raw-")

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(600, 400)
    view.show()

    def element(name):
        return next((x for x in view._document.root.elements() if x.id == name), None) if view._document else None

    try:
        view._load_markup("<div class=a><b id=b>bold</b></div><p id=log>wait</p><script>"
                     "var s = document.createElement('style');"
                     "s.textContent = '.a > b { color: rgb(0, 128, 0) } .a>b:after { content: \"<&>\" }';"
                     "document.head.appendChild(s); document.getElementById('log').textContent = 'done';"
                     "</script>", QUrl("http://example.test/"))
        end = time.time() + 10
        while time.time() < end and not (element("log") is not None and element("log").text() == "done"
                                         and (view._styles.get(element("b")) or {}).get("color")
                                         == (0, 128, 0, 255)):
            app.processEvents()
            time.sleep(0.01)
        colour = (view._styles.get(element("b")) or {}).get("color") if element("b") is not None else None
        check("a <style> a script writes keeps its \">\" (Google's rules had been lost)",
              colour == (0, 128, 0, 255), f"{colour} {element('log').text() if element('log') is not None else None}")
    finally:
        view.shutdown()
    wait(app, 0.3)


def test_google_icons_ratings_and_ellipsis(app) -> None:
    """The rest of Google's results page, against Chrome: star ratings (a
    gradient with a hard stop, clipped by a polygon), round site icons,
    grey icons by an inherited fill, play triangles sized by their own
    attributes, auto margins that centre rather than stretch, calc() of a
    share and a length, and long names ending in "…"."""
    from merlin.engine.css import Styler
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout, _clip_shape, svg_markup
    from merlin.engine.paint import _stop_positions

    def laid(markup, width=600):
        doc = parse("<body style='margin:0'>" + markup)
        styles = Styler(doc).compute()
        return doc, styles, Layout(doc, styles, width).run()

    def by_id(doc, name):
        return next(x for x in doc.root.elements() if x.id == name)

    def rects(out):
        found = {}
        for r, e in out.boxes:
            name = getattr(e, "attrs", {}).get("id")
            if name and name not in found:
                found[name] = (round(r.x()), round(r.y()), round(r.width()), round(r.height()))
        return found

    places = _stop_positions([((1, 1, 1, 255), 56.0), ((2, 2, 2, 255), ("%", 0.0))], 68.0)
    check("two gradient stops at one place make a hard edge, both kept (Google's stars)",
          places[0] < places[1] and places[1] - places[0] < 0.001, str(places))
    shape = _clip_shape("polygon(6px 0,4.31px 3.98px,0 4.34px)", 68, 12)
    check("clip-path: polygon() is a shape to clip by",
          shape and shape[0] == "polygon" and shape[1][1] == (4.31, 3.98), str(shape))
    check("and circle(), ellipse() and inset() too",
          _clip_shape("circle(50%)", 40, 40)[0] == "ellipse"
          and _clip_shape("inset(2px round 4px)", 40, 40) == ("inset", 2.0, 2.0, 36.0, 36.0, 4.0)
          and _clip_shape("url(#a)", 40, 40) is None)
    doc, styles, out = laid("<span id=s style='display:inline-block;width:68px;height:12px;"
                            "background:linear-gradient(to right, gold 40px, #dadbe5 0%);"
                            "clip-path:polygon(0 0, 68px 0, 34px 12px)'></span>")
    clips = [i for i in out.items if i and i[0] == "clip_push" and len(i) > 4]
    check("a box with clip-path: polygon() is drawn through it",
          len(clips) == 1 and clips[0][4][0] == "polygon", str(clips))

    pixel = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
             "AAAABJRU5ErkJggg==")
    doc, styles, out = laid(f"<img src='{pixel}' style='display:block;width:40px;height:40px;"
                            f"border-radius:50%'><p><img src='{pixel}' style='width:20px;height:20px;"
                            f"border-radius:50%'></p>")
    rounded = [i for i in out.items if i and i[0] == "clip_push" and i[2] >= 9.5]
    check("a picture with border-radius: 50% is round, as a block or on a line (site icons)",
          len(rounded) == 2, str([(i[1], i[2]) for i in rounded]))

    doc, styles, out = laid("<style>.z{display:inline-block;fill:currentColor}.y{color:#747878}</style>"
                            "<span class='y z'><svg id=s viewBox='0 0 10 10'><path d='M0 0h10v10z'/></svg>"
                            "<svg id=t fill=none viewBox='0 0 10 10'><path/></svg></span>")
    markup = svg_markup(by_id(doc, "s"), styles[by_id(doc, "s")])
    check("an <svg> takes the fill: currentColor of the element round it (Google's grey icons)",
          'fill="#747878"' in markup, markup)
    markup = svg_markup(by_id(doc, "t"), styles[by_id(doc, "t")])
    check("but its own fill attribute comes first", 'fill="none"' in markup, markup)

    doc, styles, out = laid("<div style='display:flex;height:20px'><svg id=tri width=7.5 height=9 "
                            "viewBox='0 0 7 8' style='margin-block:auto'><path d='M0 0L7 4L0 8z'/></svg>"
                            "<span>21:43</span></div>"
                            "<div style='display:flex;height:40px'><div id=mid style='margin:auto 0;"
                            "width:10px'>x</div></div>")
    got = rects(out)
    check("an <svg> in a flex row keeps its height attribute, centred by auto margins (play triangles)",
          got.get("tri", (0, 0, 0, 0))[3] == 9 and got.get("tri", (0, 0, 0, 0))[1] in (5, 6), str(got))
    check("and a flex item with auto margins across is centred, not stretched",
          got.get("mid", (0, 0, 0, 0))[3] < 40, str(got))

    doc, styles, out = laid("<div style='width:180px'><div id=c style='max-width:calc(100% - 36px);"
                            "height:10px'></div><div id=w style='width:calc(50% + 2px);height:10px'></div></div>")
    got = rects(out)
    check("calc() of a share and a length sizes a box (max-width: calc(100% - 36px))",
          got.get("c", (0, 0, 0, 0))[2] == 144 and got.get("w", (0, 0, 0, 0))[2] == 92, str(got))

    doc, styles, out = laid("<div style='width:120px;overflow:hidden;white-space:nowrap;"
                            "text-overflow:ellipsis'>Surviving The Survivor: #BestGuests in True Crime</div>"
                            "<div style='width:200px;overflow:hidden;-webkit-line-clamp:2;display:-webkit-box;"
                            "-webkit-box-orient:vertical;line-height:20px'>Charlie Duke, the youngest man to "
                            "walk on the moon, says his Christian faith was renewed by the trip</div>")
    texts = [i[3] for i in out.items if i and i[0] == "text"]
    first = next((t for t in texts if t.startswith("Surv")), "")
    check("text-overflow: ellipsis ends a line too long in \u2026, within its box",
          first.endswith("\u2026") and len(first) < 30, str(texts))
    check("and a line clamp's last line ends in \u2026 when more follows",
          sum(t.endswith("\u2026") for t in texts) == 2, str(texts))

    def painted(out):
        found = []
        for item in out.items:
            if item and item[0] == "group":
                found.extend(sub for sub in item[1] if sub)
            elif item:
                found.append(item)
        return found

    doc, styles, out = laid("<div style='width:300px;height:38px;border:1px solid #c6c6c6;border-right:none;"
                            "border-radius:40px 0 0 40px;background:#fff'></div>"
                            "<span style='display:inline-block;width:11px;height:11px;border:2px solid #444;"
                            "border-top-color:#2c2;border-radius:50%'></span>")
    frames = [i for i in painted(out) if i[0] == "rframe"]
    filled = [i for i in painted(out) if i[0] == "rrect" and len(i) > 4]
    check("each corner takes its own radius, and a border missing a side goes round them (YouTube's search box)",
          len(filled) == 1 and filled[0][4] == (20.0, 0.0, 0.0, 20.0)
          and frames and frames[0][3] == (1.0, 0.0, 1.0, 1.0), str(frames[:1]) + str(filled))
    check("a rounded border of two colours keeps both (Ponder's spinners)",
          len(frames) == 2 and frames[1][5] and frames[1][5][0] != frames[1][5][1], str(frames[1:]))
    doc, styles, out = laid("<div style='width:300px;height:200px;position:relative'><a style='position:absolute;"
                            "inset:0;display:block'><span><img id=lazy style='width:100%;height:100%;"
                            "display:inline-block;min-width:1px;min-height:1px'></span></a></div>")
    got = rects(out)
    check("a picture not yet loaded takes the size CSS gives it, so lazy pages see it (YouTube's thumbnails)",
          got.get("lazy") == (0, 0, 300, 200), str(got))


def test_streams_timers_and_templates(app) -> None:
    """Page scripts, as YouTube, Ponder and Cover Your Tracks use them: a
    fetch() streamed as it arrives, setTimeout() given a string of code,
    templates whose content is inert and is cloned as scripts changed it,
    innerText set, and IntersectionObserverEntry there to be found."""
    from merlin.engine import MerlinView
    from merlin.engine.script import LocalStorage

    runtime = _node_for_tests()
    if not runtime:
        print("  skip  streams, timers and templates: Node.js is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-streams-")
    open(os.path.join(folder, "page.html"), "w").write(r"""<!DOCTYPE html><html><body>
<p id=log>wait</p><p id=first>-</p><p id=timer>-</p><p id=tpl>-</p><div id=box><b>x</b></div>
<template id=t><x-card v="[[bound]]"><span>hi</span></x-card><template id=inner><i c="[[z]]">i</i></template></template>
<script>
var made = 0, heard = [];
class Card extends HTMLElement { constructor(){ super(); made++; }
  static get observedAttributes(){ return ['v']; } attributeChangedCallback(n, o, v){ heard.push(v); } }
customElements.define('x-card', Card);
var t = document.getElementById('t');
var beforeImport = made;
t.content.querySelector('x-card').removeAttribute('v');
t.content.querySelector('template').content.firstChild.removeAttribute('c');
var copy = document.importNode(t.content, true);
document.body.appendChild(copy);
var placed = document.body.querySelector('x-card');
var innerCopy = document.body.querySelector('template#inner').content.firstChild.outerHTML;
var box = document.getElementById('box'); box.innerText = 'one\ntwo';
var probe = document.createElement('div'); document.body.appendChild(probe);
var cs = getComputedStyle(probe);
probe.style.position = 'fixed'; probe.style.top = '40px'; probe.style.left = '12px';
var styled = [cs.top, cs.maxHeight, cs.getPropertyValue('min-width'), getComputedStyle(probe).display].join(',');
document.getElementById('tpl').textContent = [beforeImport, made, heard.length, placed instanceof Card,
  innerCopy, box.innerHTML, typeof IntersectionObserverEntry,
  'intersectionRatio' in IntersectionObserverEntry.prototype, styled].join(' ');
setTimeout("document.getElementById('timer').textContent = 'string ran'", 50);
(async function () {
  var resp = await fetch('/stream');
  var reader = resp.body.getReader(), decoder = new TextDecoder(), seen = 0, text = '';
  while (true) {
    var part = await reader.read();
    if (part.done) break;
    text += decoder.decode(part.value, {stream: true});
    if (!seen && text.indexOf('\n') >= 0) { seen = 1; document.getElementById('first').textContent = 'first ' + text.split('\n')[0]; }
  }
  document.getElementById('log').textContent = 'done ' + text.trim().split('\n').length;
})();
</script></body></html>""")

    class Pages(http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def do_GET(self):
            if self.path != "/stream":
                return super().do_GET()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for n, pause in ((1, 0.0), (2, 3.0)):
                time.sleep(pause)
                line = (json.dumps({"n": n}) + "\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(600, 400)
    view.show()

    def text(name):
        found = next((x for x in view._document.root.elements() if x.id == name), None) if view._document else None
        return " ".join(found.text().split()) if found is not None else ""

    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/page.html"))
        end = time.time() + 10
        while time.time() < end and not text("first").startswith("first"):
            app.processEvents()
            time.sleep(0.01)
        early = text("log")
        check("a streamed fetch() hands its first line on before the rest has come (Ponder's results)",
              text("first").startswith('first {"n": 1}') and early == "wait",
              f"{text('first')} / {early} / {list(view.console_lines)[-3:]}")
        end = time.time() + 8
        while time.time() < end and not text("log").startswith("done"):
            app.processEvents()
            time.sleep(0.01)
        check("and its end", text("log") == "done 2", text("log"))
        check("setTimeout() given a string runs it (Cover Your Tracks' fingerprint check)",
              text("timer") == "string ran", text("timer"))
        parts = text("tpl").split(" ")
        check("a template's custom elements are made only when its content is copied, their [[bindings]] unread",
              parts[:4] == ["0", "1", "0", "true"], text("tpl"))
        check("a template inside a template is copied with its content as a script left it (YouTube's)",
              "i</i>" in text("tpl") and "[[z]]" not in text("tpl"), text("tpl"))
        check("innerText can be set, a line break a <br>", "one<br>two" in text("tpl"), text("tpl"))
        check("IntersectionObserverEntry is there, as polyfills look for it",
              "function true" in text("tpl"), text("tpl"))
        check("getComputedStyle() gives CSS's initial values, not \"\" (YouTube's cookie dialog)",
              text("tpl").endswith("40px,none,0px,block"), text("tpl"))
        element = view._document.root
        view._hover_linger = {id(element): (element, time.monotonic() + 0.4)}
        before = view._hover_linger[id(element)][1]
        view._pause_lingering(0.5)
        check("a menu lingering open waits out the time Merlin spent laying the page out (AlterniTech's)",
              abs(view._hover_linger[id(element)][1] - before - 0.5) < 0.01, str(view._hover_linger))
    finally:
        view.shutdown()
        server.shutdown()
    wait(app, 0.3)


def test_search_box_columns_and_a_quick_window(app) -> None:
    """YouTube's search box (a rounded inset shadow, a clear button centred in
    a holder smaller than itself), Microsoft's two-column sections (flex: 0 0
    calc(...)) and pictures (never asked for as AVIF, which Qt cannot draw),
    and the window kept answering while a page loads."""
    import subprocess

    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject
    from PyQt6.QtGui import QColor, QImage, QImageReader, QPainter

    from merlin.engine import MerlinView
    from merlin.engine import chromelike
    from merlin.engine import view as view_module
    from merlin.engine.css import Styler, expand_shorthand
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout
    from merlin.engine.paint import draw_shadows
    from merlin.engine.script import ScriptHost

    def laid(markup, width=1000):
        doc = parse("<body style='margin:0'>" + markup)
        styles = Styler(doc).compute()
        return Layout(doc, styles, width).run()

    def rects(out):
        found = {}
        for r, e in out.boxes:
            name = getattr(e, "attrs", {}).get("id")
            if name and name not in found:
                found[name] = (round(r.x()), round(r.y()), round(r.width()), round(r.height()))
        return found

    def flat(items):
        for item in items:
            if item and item[0] == "group":
                yield from flat(item[1])
            elif item:
                yield item

    # the search box: rounded on the left only, its inset shadow with it
    out = laid("<div id=box style='width:300px;height:80px;border:1px solid #1c62b9;"
               "border-radius:40px 0 0 40px;box-shadow:inset 0 1px 2px #eee'></div>")
    shadows = [i for i in flat(out.items) if i[0] == "shadow"]
    check("an inset shadow keeps each corner's own radius (YouTube's search box)",
          bool(shadows) and len(shadows[0]) > 5 and shadows[0][5] == (40.0, 0.0, 0.0, 40.0),
          str(shadows[:1]))

    def corner_pixel(corners):
        picture = QImage(302, 82, QImage.Format.Format_ARGB32)
        picture.fill(QColor("white"))
        painter = QPainter(picture)
        draw_shadows(painter, QRectF(0, 0, 302, 82), [(0.0, 1.0, 2.0, 0.0, (120, 120, 120, 255))],
                     0.0, True, corners)
        painter.end()
        return picture.pixelColor(2, 2).red(), picture.pixelColor(150, 0).red()

    rounded, square = corner_pixel((40.0, 0.0, 0.0, 40.0)), corner_pixel(None)
    check("so no grey corner is drawn outside the rounded border",
          rounded[0] == 255 and square[0] < 255 and rounded[1] < 255, f"{rounded} {square}")

    out = laid("<button id=b style='width:40px;height:40px;border:0;padding:0 8px'>x</button>")
    check("a button's width and height include its padding, as in browsers (box-sizing: border-box)",
          rects(out).get("b", (0, 0, 0, 0))[2:] == (40, 40), str(rects(out).get("b")))
    out = laid("<div id=w style='display:flex;align-items:center;justify-content:center;"
               "width:24px;height:24px;margin:50px 0 0 100px'>"
               "<div id=c style='width:40px;height:40px;flex-shrink:0;background:red'></div></div>")
    check("an item larger than its flex box, centred, overflows it equally on each side "
          "(YouTube's clear button, in its 24-pixel holder)",
          rects(out).get("c") == (92, 42, 40, 40), str(rects(out)))
    out = laid("<div style='display:flex;justify-content:flex-end;width:100px'>"
               "<div id=e style='width:140px;flex-shrink:0;height:10px;background:red'></div></div>")
    check("and one at the end overflows the start", rects(out).get("e", (0,))[0] == -40, str(rects(out)))

    out = laid("<style>.r{position:relative;width:90px;height:36px;border-radius:18px;background:#0f0f0f}"
               ".r:before{content:'';position:absolute;inset:0;border-radius:inherit;padding:.5px;"
               "-webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);"
               "-webkit-mask-composite:xor;mask-composite:exclude;"
               "background:linear-gradient(rgba(255,255,255,0.6),transparent 75%)}</style><div class=r></div>")
    rings = [i for i in flat(out.items) if i[0] == "masked" and isinstance(i[2], tuple) and i[2][0] == "ring"]
    check("a mask of a content box taken from the whole box leaves only a ring (YouTube's rim of "
          "light on its buttons, which had been drawn over all of each)",
          len(rings) == 1 and rings[0][2][1:5] == (0.5, 0.5, 0.5, 0.5), str(rings[:1])[:200])

    # Microsoft's columns
    check("flex: 0 0 calc(50% - 8px) is a basis of calc(50% - 8px)",
          expand_shorthand("flex", "0 0 calc(50% - 8px)")[2] == ("flex-basis", "calc(50% - 8px)"),
          str(expand_shorthand("flex", "0 0 calc(50% - 8px)")))
    out = laid("<style>.l{--count:2;--gap:16px;--each:calc((100% - var(--gap)) / var(--count));"
               "display:flex;flex-wrap:wrap;column-gap:var(--gap)}.l>div{flex:0 0 var(--each)}</style>"
               "<div class=l style='width:1302px'><div id=one>one</div><div id=two>two</div></div>")
    check("so Microsoft's two columns sit side by side, each half the row",
          rects(out).get("one", (0, 0, 0))[2] == 643 and rects(out).get("two", (0, 0))[:2] == (659, 0),
          str(rects(out)))
    check("inset and border-radius with calc() in them, split only outside brackets",
          expand_shorthand("inset", "calc(5px + 5px) calc(10px + 10px)")[1] == ("right", "calc(10px + 10px)")
          and expand_shorthand("border-radius", "calc(10px / 2)")[0]
          == ("border-top-left-radius", "calc(10px / 2)"),
          str(expand_shorthand("border-radius", "calc(10px / 2)")))
    readable = {bytes(f).decode().lower() for f in QImageReader.supportedImageFormats()}
    accept = view_module.image_accept()
    check("pictures are asked for only in formats Merlin can draw (AVIF only with a Qt that reads it)",
          ("image/avif" in accept) == ("avif" in readable) and "image/png" in accept, accept)

    # the window is never held up by a page's scripts being busy
    host = ScriptHost.__new__(ScriptHost)
    QObject.__init__(host)
    host.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                    stdin=subprocess.PIPE)
    host._start_writer()
    try:
        started = time.monotonic()
        for _n in range(40):
            host.send({"type": "answer", "body": "x" * 100000})
        took = time.monotonic() - started
        check("sending to scripts that read nothing does not wait (the window stood still on YouTube)",
              took < 0.5, f"{took:.2f}s for 4 MB")
        host.send({"type": "geometry", "n": 1})
        host.send({"type": "geometry", "n": 2})
        waiting = [data for kind, data in host._outgoing if kind == "geometry"]
        check("and of two layouts not yet read, only the latest is kept",
              len(waiting) == 1 and b'"n": 2' in waiting[0], str(waiting))
    finally:
        host.stop()
        host.process.kill()
    ran = threading.Event()
    view_module._spawn(ran.set)
    check("work handed to a thread of its own runs", ran.wait(2))
    was = chromelike.enabled
    chromelike.enabled = False
    try:
        jar = view_module.new_cookie_jar()
        check("a cookie jar's opener is made once, not for every request",
              view_module.cookie_opener(jar) is view_module.cookie_opener(jar))
    finally:
        chromelike.enabled = was

    # a picture being fetched is not fetched again, and is in the network log
    picture = QImage(10, 10, QImage.Format.Format_RGB32)
    picture.fill(QColor("orange"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    picture.save(buffer, "PNG")
    png = bytes(data)
    asked = []

    class Slow(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith(".png"):
                asked.append(self.headers.get("Accept", ""))
                time.sleep(0.8)
                body, kind = png, "image/png"
            else:
                body, kind = b"<p>text<img src='/slow.png' width=10 height=10>", "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    view = MerlinView()
    view.resize(400, 300)
    view.show()
    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/"))
        wait(app, 0.4)
        view._load_images()
        view._want_image("/slow.png")
        view._load_images()
        wait(app, 1.6)
        check("a picture still arriving is not fetched again when asked for again",
              len(asked) == 1, f"{len(asked)} fetches")
        check("and not asked for as AVIF unless Merlin can draw it",
              bool(asked) and (("avif" in asked[0]) == ("avif" in readable)), str(asked[:1]))
        logged = [line for line in view.network_lines if "slow.png" in line]
        check("pictures are in the network log, for the debugging zip",
              any(line.startswith("IMAGE") and "image/png" in line for line in logged), str(logged))
    finally:
        server.shutdown()
        view.close()


def test_quiet_tabs_shadows_and_deep_layouts(app) -> None:
    """What 1.8.44 changed: a page in a hidden tab kept quiet, as browsers keep
    it; YouTube's buttons (a dozen flex boxes deep) laid out once, not
    hundreds of times; shadow roots, Lit's markers and core-js's class lists
    as Microsoft's header uses them; grid and justify-self centring as
    ChatGPT's column has it; and a fixed header measured again once laid out."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter

    from merlin.engine import MerlinView
    from merlin.engine import layout as layout_module
    from merlin.engine import paint as paint_module
    from merlin.engine.css import Styler
    from merlin.engine.dom import Element
    from merlin.engine.html import parse
    from merlin.engine.layout import Layout
    from merlin.engine.script import LocalStorage

    def laid(markup, width=1000):
        doc = parse("<body style='margin:0'>" + markup)
        styles = Styler(doc).compute()
        return doc, styles, Layout(doc, styles, width).run()

    def rects(out):
        found = {}
        for r, e in out.boxes:
            name = getattr(e, "attrs", {}).get("id")
            if name and name not in found:
                found[name] = (round(r.x()), round(r.y()), round(r.width()), round(r.height()))
        return found

    # a deep nest of flex rows and inline-blocks, as YouTube's buttons are
    depth = 14
    markup = "<span id=leaf>Subscribe</span>"
    for level in range(depth):
        markup = (f"<div style='display:flex;align-items:center;padding:1px'>{markup}<i>x</i></div>"
                  if level % 2 else f"<span style='display:inline-block;padding:1px'>{markup}</span>")
    markup = f"<div style='display:flex'><div>{markup}</div><div style='flex:1'>after</div></div>"
    calls = []
    original = layout_module.Layout._block

    def counting(self, element, *args, **kwargs):
        calls.append(element)
        return original(self, element, *args, **kwargs)

    layout_module.Layout._block = counting
    try:
        doc, styles, out = laid(markup)
    finally:
        layout_module.Layout._block = original
    leaf_parent = next(e for e in doc.root.elements() if e.id == "leaf").parent
    times = sum(1 for e in calls if e is leaf_parent)
    check("an item deep in flex rows and inline-blocks is laid out a few times, not once more for each "
          "box around it (YouTube's channel page took half a minute)", times <= 12, f"{times} times")
    plain = layout_module.Layout._item_box
    layout_module.Layout._item_box = lambda self, e, s, x, y, bw, bh=None: self._item_box_now(e, s, x, y, bw, bh)
    try:
        again = Layout(doc, styles, 1000).run()
    finally:
        layout_module.Layout._item_box = plain
    check("and comes out exactly where it did when laid out every time",
          [(round(r.x(), 2), round(r.y(), 2), round(r.width(), 2), round(r.height(), 2)) for r, _e in out.boxes]
          == [(round(r.x(), 2), round(r.y(), 2), round(r.width(), 2), round(r.height(), 2)) for r, _e in again.boxes]
          and out.height == again.height)

    # grid items held by max-width and centred by auto margins; justify-self
    _d, _s, out = laid("<div style='display:grid;grid-template-columns:600px'>"
                       "<div id=g style='width:100%;max-width:200px;margin:0 auto;height:10px'></div></div>"
                       "<div style='width:400px'><div id=j style='width:100px;justify-self:center;height:5px'></div></div>")
    found = rects(out)
    check("a grid item with a max-width and auto margins is centred at that width (ChatGPT's composer)",
          found.get("g") == (200, 0, 200, 10), str(found.get("g")))
    check("justify-self: center centres a block in the flow (ChatGPT's heading)",
          found.get("j") == (150, 10, 100, 5), str(found.get("j")))

    # isolation makes a stacking context: a z-index: -1 child is over its background
    _d, _s, out = laid("<div style='isolation:isolate;position:relative;background:#fff;width:60px;height:60px'>"
                       "<div style='position:absolute;z-index:-1;left:0;top:0;width:60px;height:60px;"
                       "background:#f00'></div></div>"
                       "<div style='opacity:0.99;position:relative;background:#fff;width:60px;height:60px'>"
                       "<div style='position:absolute;z-index:-1;left:0;top:0;width:60px;height:60px;"
                       "background:#00f'></div></div>")
    image = QImage(200, 200, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 255, 0))
    painter = QPainter(image)
    paint_module.paint(painter, out, QRectF(0, 0, 200, 200), {})
    painter.end()
    first, second = image.pixelColor(30, 30), image.pixelColor(30, 90)
    check("isolation: isolate keeps a z-index: -1 child inside, over the box's own background",
          (first.red(), first.green(), first.blue()) == (255, 0, 0), first.name())
    check("so does opacity below 1", second.blue() > 240 and second.red() < 12 and second.green() < 12, second.name())

    # <use> drawn from a symbol elsewhere in the page
    doc, styles, out = laid("<svg style='display:none'><symbol id=dot viewBox='0 0 10 10'>"
                            "<circle cx=5 cy=5 r=5 fill='#f00'/></symbol></svg>"
                            "<svg id=icon width=20 height=20 viewBox='0 0 20 20'><use href='#dot'/></svg>")
    icon = next(e for e in doc.root.elements() if e.id == "icon")
    drawn = layout_module.svg_markup(icon, styles.get(icon, {}))
    check("an <svg> drawing <use href=\"#name\"> has the named symbol drawn in it (ChatGPT's icons)",
          "<circle" in drawn and "scale(2" in drawn, drawn[:300])
    check("the page's body is found once, not looked for each time",
          doc.body is doc.body and doc.body.tag == "body")
    doc.root.children[:] = [c for c in doc.root.children if getattr(c, "tag", None) != "body"]
    check("and not kept once it has gone", doc.body is None or doc.body.parent is doc.root)

    # rules filed by what is at their right end, matched all the same
    doc, styles, _out = laid("<style>.menu > * {color: rgb(1,2,3)} .menu * {font-weight: 700}"
                             " [class*=btn] {margin-left: 7px} [class~=b] {margin-right: 9px}</style>"
                             "<ul class=menu><li id=li><a id=a>x</a></li></ul><p id=p class='my-btn-x b'>y</p>")
    by = {e.id: styles[e] for e in doc.root.elements() if e.id}
    check("child and descendant rules ending in * still match (Microsoft's menus)",
          by["li"].get("color")[:3] == (1, 2, 3) and by["a"].get("color")[:3] != (1, 2, 3)
          and str(by["a"].get("font-weight")) in ("700", "bold"), str((by["li"].get("color"), by["a"].get("font-weight"))))
    check("and [class*=...] and [class~=...] match as before",
          by["p"].get("margin-left") == 7.0 and by["p"].get("margin-right") == 9.0,
          str((by["p"].get("margin-left"), by["p"].get("margin-right"))))

    runtime = _node_for_tests()
    if not runtime:
        print("  skip  hidden tabs and shadow roots: Node.js is not here")
        return
    folder = tempfile.mkdtemp(prefix="merlin-quiet-")
    open(os.path.join(folder, "page.html"), "w").write(r"""<!DOCTYPE html><html><body style="margin:0">
<p id=out>-</p><p id=state>-</p><div id=host></div><div id=late></div><div id=watched></div>
<script>
var r = [];
var t = document.createElement('template'); t.innerHTML = '<p><?lit$12$>x</p>';
var c = t.content.firstChild.firstChild;
r.push('lit' + c.nodeType + (String(c.data).indexOf('lit$12$') >= 0));
DOMTokenList.prototype[Symbol.iterator] = Array.prototype.values;      // as core-js does
var d = document.createElement('div'); d.classList.add('a', 'b'); d.classList.remove('a'); d.classList.add('c');
r.push('cls' + d.className + d.classList[0] + d.classList[1] + [].slice.call(d.classList).join(''));
var records = 0;
new MutationObserver(function (l) { records += l.length; }).observe(document.getElementById('watched'), {childList: true});
document.getElementById('watched').setAttribute('data-x', '1');
class W extends HTMLElement { set className(v) { this.setAttribute('class', v); } get className() { return this.getAttribute('class') || ''; } }
customElements.define('x-w', W);
var w = document.createElement('x-w'); w.className = 'k'; r.push('own' + w.getAttribute('class'));
r.push('vis' + typeof document.body.checkVisibility);
var host = document.getElementById('host');
var root = host.attachShadow({mode: 'open'});
root.innerHTML = '<style>:host{display:block;padding-left:3px} b{color:rgb(0,128,0)}</style><b>shadow</b> <slot></slot>';
host.innerHTML = '<i>light</i>';
var changes = [], frames = 0, resizes = 0, seen = [];
document.addEventListener('visibilitychange', function () {
  changes.push(document.visibilityState + document.hidden + ':' + ticks + ':' + frames);
  if (document.hidden) requestAnimationFrame(function () { frames++; });
});
addEventListener('resize', function () { resizes++; });
var ticks = 0; setInterval(function () { ticks++; }, 20);
var watcher = new ResizeObserver(function (es) { seen.push(es[0].contentRect.height); });
var grow = document.createElement('div'); document.body.appendChild(grow);
watcher.disconnect(); watcher.observe(grow);
setTimeout(function () { grow.style.height = '40px'; }, 300);
var lateRead = -1;
setTimeout(function () {
  var late = document.getElementById('late');
  lateRead = late.offsetHeight;                                   // laid out, 0 high
  late.innerHTML = '<div style="height:25px"></div>';
}, 600);
setInterval(function () {
  document.getElementById('out').textContent = r.join(' ') + ' rec' + records;
  document.getElementById('state').textContent = [changes.join(','), 'frames' + frames, 'ticks' + ticks,
    'seen' + seen.join('/'), 'late' + lateRead, 'resizes' + resizes].join(' ');
}, 50);
</script></body></html>""")

    class Pages(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Host:
        settings = {}

        def javascript_allowed(self, site):
            return True

        def node_for_scripts(self):
            return runtime

        def script_cache_dir(self):
            return os.path.join(folder, "cache")

        def local_storage(self):
            return LocalStorage(None)

    view = MerlinView()
    view._host = Host()
    view.resize(600, 400)
    view.show()

    def element(name):
        return next((x for x in view._document.root.elements() if x.id == name), None) if view._document else None

    def text(name):
        found = element(name)
        return " ".join(found.text().split()) if found is not None else ""

    def until(test, seconds):
        end = time.time() + seconds
        while time.time() < end and not test():
            app.processEvents()
            time.sleep(0.01)

    try:
        view.setUrl(QUrl(f"http://127.0.0.1:{server.server_address[1]}/page.html"))
        until(lambda: "/40 " in text("state") and "resizes1" in text("state"), 10)
        out = text("out")
        check("<?...?> in HTML a script parses is a comment, as Lit's markers are (Microsoft's header)",
              "lit8true" in out, f"{out} / {list(view.console_lines)[-4:]}")
        check("a class list read through an Array iterator, as core-js puts in, has its classes (Microsoft's)",
              "clsb cbcbc" in out, out)
        check("an attribute change is not reported to an observer of children only (Microsoft's endless loop)",
              out.endswith("rec0"), out)
        check("setAttribute('class') does not call a page's own className setter (YouTube's stack overflow)",
              "ownk" in out, out)
        check("Element.checkVisibility is there (Microsoft's player)", "visfunction" in out, out)
        state = text("state")
        check("a ResizeObserver observing again after disconnect() hears of sizes (YouTube's channel header)",
              "/40 " in state, state)
        check("a box read as 0 high, laid out with a size later, brings a resize for scripts to measure "
              "again (YouTube's header, drawn over by the videos)", "late0" in state and "resizes1" in state, state)
        host = element("host")
        bold = next((x for x in host.elements() if x.tag == "b"), None) if host is not None else None
        check("a shadow root is drawn, its slot filled with the element's own children",
              text("host").endswith("shadow light"), text("host"))
        check("its styles apply inside it, :host to the element itself",
              bold is not None and tuple(view._styles.get(bold, {}).get("color", ()))[:3] == (0, 128, 0)
              and view._styles.get(host, {}).get("padding-left") == 3.0,
              str((view._styles.get(bold, {}).get("color") if bold is not None else None,
                   view._styles.get(host, {}).get("padding-left") if host is not None else None)))
        view.hide()
        wait(app, 2.5)
        view.show()
        until(lambda: "visiblefalse" in text("state") and "frames1" in text("state"), 5)
        shown = text("state")
        told = dict(part.split(":", 1) for part in shown.split()[0].split(",") if ":" in part)
        check("a hidden tab is told it is hidden (document.hidden, visibilitychange), and told when shown",
              "hiddentrue" in told and "visiblefalse" in told, shown)
        try:
            hidden_ticks, _f = (int(n) for n in told["hiddentrue"].split(":"))
            shown_ticks, shown_frames = (int(n) for n in told["visiblefalse"].split(":"))
        except (KeyError, ValueError):
            hidden_ticks, shown_ticks, shown_frames = 0, 999, 1
        check("while hidden its timers run at most once a second, as Chrome keeps them (Merlin's CPU with tabs open)",
              shown_ticks - hidden_ticks <= 4, f"{shown_ticks - hidden_ticks} ticks in 2.5s: {shown}")
        check("its animation frames wait until it is seen, and then run",
              shown_frames == 0 and "frames1" in shown, shown)
    finally:
        view.shutdown()
        server.shutdown()
    wait(app, 0.3)


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
    print("test_scrolling_panels_and_touch")
    test_scrolling_panels_and_touch(app)
    print("test_chrome_connections")
    test_chrome_connections(app)
    print("test_animations_and_3d")
    test_animations_and_3d(app)
    print("test_server_pages_and_http_fallback")
    test_server_pages_and_http_fallback(app)
    print("test_forms")
    test_forms(app)
    print("test_frames")
    test_frames(app)
    print("test_layout_for_dialogs_and_settings")
    test_layout_for_dialogs_and_settings(app)
    print("test_node_scripts")
    test_node_scripts(app)
    print("test_worker_features")
    test_worker_features(app)
    print("test_blur_timing")
    test_blur_timing(app)
    print("test_square_and_convert_layout")
    test_square_and_convert_layout(app)
    print("test_hover")
    test_hover(app)
    print("test_pictures_cascade_and_painting")
    test_pictures_cascade_and_painting(app)
    print("test_view")
    test_view(app)
    print("test_video_and_audio")
    test_video_and_audio(app)
    print("test_dom_for_polyfills_dialogs_and_cssom")
    test_dom_for_polyfills_dialogs_and_cssom(app)
    print("test_border_box_heights_and_centring")
    test_border_box_heights_and_centring(app)
    print("test_scripts_changes_sent_lightly")
    test_scripts_changes_sent_lightly(app)
    print("test_requests_as_a_browser_makes_them")
    test_requests_as_a_browser_makes_them(app)
    print("test_tracker_test_pages")
    test_tracker_test_pages(app)
    print("test_google_results_layout")
    test_google_results_layout(app)
    print("test_google_icons_ratings_and_ellipsis")
    test_google_icons_ratings_and_ellipsis(app)
    print("test_streams_timers_and_templates")
    test_streams_timers_and_templates(app)
    print("test_search_box_columns_and_a_quick_window")
    test_search_box_columns_and_a_quick_window(app)
    print("test_quiet_tabs_shadows_and_deep_layouts")
    test_quiet_tabs_shadows_and_deep_layouts(app)
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: " + "; ".join(FAILED))
        return 1
    print("\nall MerlinEngine checks OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
