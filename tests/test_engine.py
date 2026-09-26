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
                 test_body_and_markers, test_tables):
        print(test.__name__)
        test()
    print("test_images")
    test_images(app)
    print("test_view")
    test_view(app)
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: " + "; ".join(FAILED))
        return 1
    print("\nall MerlinEngine checks OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
