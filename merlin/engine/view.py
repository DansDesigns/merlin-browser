"""MerlinView: a page rendered by MerlinEngine, as a Qt widget.

Its signals carry the same names and meanings as the ones Merlin's browser
window already listens to on Chromium's view (titleChanged, urlChanged,
loadStarted, loadProgress, loadFinished, linkHovered), so it can in time sit
where Chromium's view sits. Loading happens off the UI thread; parsing,
styling and layout run on it, and are fast enough for ordinary documents.

Not here yet: JavaScript, cookies, forms, images, and the content blocker.
"""
from __future__ import annotations

import threading
import urllib.parse
import urllib.request

from PyQt6.QtCore import QObject, QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter
from PyQt6.QtWidgets import QScrollBar, QWidget

from . import html as html_parser
from .css import Styler
from .layout import Layout
from .paint import paint

USER_AGENT = "Mozilla/5.0 (compatible; MerlinEngine/0.1)"


def fetch_bytes(url: str, headers: dict | None = None, timeout: int = 20,
                limit: int = 16 * 1024 * 1024) -> tuple:
    """(ok, final url, bytes or reason, content type). Off the UI thread."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    try:
        if scheme == "data":
            header, _comma, data = url[5:].partition(",")
            kind = header.split(";")[0] or "text/plain"
            if header.endswith(";base64"):
                import base64

                return True, url, base64.b64decode(data), kind
            return True, url, urllib.parse.unquote_to_bytes(data), kind
        if scheme == "file":
            with open(QUrl(url).toLocalFile(), "rb") as handle:
                return True, url, handle.read(limit), ""
        if scheme in ("http", "https"):
            sent = {"User-Agent": USER_AGENT, "Accept": "*/*",
                    "Accept-Language": "en-GB,en;q=0.8"}
            sent.update(headers or {})
            request = urllib.request.Request(url, headers=sent)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return (True, response.geturl(), response.read(limit),
                        response.headers.get("Content-Type", ""))
        return False, url, f"MerlinEngine cannot open {scheme}: addresses yet.".encode(), ""
    except Exception as exc:                              # noqa: BLE001
        return False, url, str(exc).encode(), ""


def fetch(url: str, timeout: int = 20, headers: dict | None = None) -> tuple:
    """(ok, final url, text or reason). Runs off the UI thread."""
    if url.startswith("view-source:"):
        import html

        ok, final, text = fetch(url[len("view-source:"):], timeout, headers)
        if not ok:
            return ok, url, text
        return True, url, (f"<title>Source of {html.escape(final)}</title>"
                           f"<pre style='white-space:pre-wrap'>{html.escape(text)}</pre>")
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    try:
        if scheme == "data":
            header, _comma, data = url[5:].partition(",")
            if header.endswith(";base64"):
                import base64

                return True, url, base64.b64decode(data).decode("utf-8", "replace")
            return True, url, urllib.parse.unquote(data)
        if scheme == "file":
            with open(QUrl(url).toLocalFile(), "rb") as handle:
                return True, url, _decode(handle.read(), "")
        if scheme in ("http", "https"):
            sent = {"User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                    "Accept-Language": "en-GB,en;q=0.8"}
            sent.update(headers or {})
            request = urllib.request.Request(url, headers=sent)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(8 * 1024 * 1024)
                kind = response.headers.get("Content-Type", "")
                text = _decode(body, kind)
                if "html" not in kind and kind and not kind.startswith("text/"):
                    return False, response.geturl(), f"This is {kind.split(';')[0]}, not a page."
                if kind.startswith("text/plain"):
                    import html

                    text = f"<pre>{html.escape(text)}</pre>"
                return True, response.geturl(), text
        return False, url, f"MerlinEngine cannot open {scheme}: addresses yet."
    except Exception as exc:                              # noqa: BLE001
        return False, url, str(exc)


def _decode(body: bytes, content_type: str) -> str:
    charset = ""
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip().strip("'\"")
    if not charset:
        head = body[:2048].decode("ascii", "replace").lower()
        marker = head.find("charset=")
        if marker >= 0:
            charset = head[marker + 8:].split('"')[0].split("'")[0].split(">")[0].split(";")[0].strip("\"' /")
    try:
        return body.decode(charset or "utf-8", "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


class _History:
    """Back and forward, as the browser window asks Chromium's history."""

    def __init__(self):
        self.entries: list[QUrl] = []
        self.index = -1

    def canGoBack(self) -> bool:                              # noqa: N802
        return self.index > 0

    def canGoForward(self) -> bool:                           # noqa: N802
        return 0 <= self.index < len(self.entries) - 1

    def push(self, url: QUrl) -> None:
        del self.entries[self.index + 1:]
        self.entries.append(url)
        self.index = len(self.entries) - 1


class _Page(QObject):
    """What Merlin's window asks of Chromium's page, answered by MerlinEngine.

    The window was written for Chromium, and reaches through view.page() for
    a few things. Each is answered here: history, the profile, reload, the
    hover signal and the hiding rules. Running JavaScript, which MerlinEngine
    cannot do yet, answers with nothing rather than failing.
    """

    fullScreenRequested = pyqtSignal(object)
    featurePermissionRequested = pyqtSignal(object, object)
    permissionRequested = pyqtSignal(object)
    renderProcessTerminated = pyqtSignal(object, int)

    def __init__(self, view: "MerlinView", profile=None):
        super().__init__(view)
        self._view = view
        self._profile = profile
        self.linkHovered = view.linkHovered

    def runJavaScript(self, _script, *rest):                  # noqa: N802
        callback = next((r for r in rest if callable(r)), None)
        if callback is not None:
            QTimer.singleShot(0, lambda: callback(None))

    def history(self):
        return self._view.history()

    def profile(self):
        return self._profile

    def url(self) -> QUrl:
        return self._view.url()

    def title(self) -> str:
        return self._view.title()

    def triggerAction(self, _action, *_rest):                 # noqa: N802
        self._view.reload()

    def apply_cosmetic(self, _css: str) -> None:
        self._view.refresh_hiding()


class MerlinView(QWidget):
    titleChanged = pyqtSignal(str)
    urlChanged = pyqtSignal(QUrl)
    iconChanged = pyqtSignal(QIcon)
    loadStarted = pyqtSignal()
    loadProgress = pyqtSignal(int)
    loadFinished = pyqtSignal(bool)
    linkHovered = pyqtSignal(str)
    _fetched = pyqtSignal(int, bool, str, str)      # load number, ok, url, text
    _image_fetched = pyqtSignal(int, str, bytes, bool)   # load number, src, data, ok

    def __init__(self, parent=None, host=None, profile=None):
        """host, when given, is Merlin's window: its content blocker and
        settings apply here as they do to Chromium's tabs."""
        super().__init__(parent)
        self._host = host
        self._images: dict = {}        # src -> QImage, or False: blocked or failed
        self._find = ("", -1)          # what was searched for, and where it was
        self._found_rect = None
        self._page = _Page(self, profile)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._url = QUrl()
        self._title = ""
        self._zoom = 1.0
        self._document = None
        self._styles = None
        self._display = None
        self._scroll = 0.0
        self._hovered = ""
        self._load_number = 0
        self._history = _History()
        self._pending_fragment = ""
        self.scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self.scrollbar.valueChanged.connect(self._scrolled)
        self._relayout = QTimer(self)
        self._relayout.setSingleShot(True)
        self._relayout.setInterval(60)
        self._relayout.timeout.connect(self._layout)
        self._fetched.connect(self._on_fetched)
        self._image_fetched.connect(self._on_image)

    # ------------------------------------------------ what Merlin asks for
    def url(self) -> QUrl:
        return QUrl(self._url)

    def title(self) -> str:
        return self._title

    def icon(self) -> QIcon:
        return QIcon()

    def history(self) -> _History:
        return self._history

    def page(self) -> _Page:
        return self._page

    # --------------------------------------------- Merlin's window, if any
    def _interceptor(self):
        return getattr(self._host, "interceptor", None)

    def _headers(self) -> dict:
        settings = getattr(self._host, "settings", None)
        if settings is not None and settings.get("send_do_not_track"):
            return {"DNT": "1", "Sec-GPC": "1"}
        return {}

    def _hiding_css(self) -> str:
        """The content blocker's hiding rules for this site, as CSS."""
        finder = getattr(self._host, "cosmetic_css_for", None)
        host = self._url.host()
        if finder is None or not host:
            return ""
        try:
            return finder(host) or ""
        except Exception:                                  # noqa: BLE001
            return ""

    def _blocked(self, url: str, kind: str) -> bool:
        """Whether the content blocker stops something this page pulls in."""
        interceptor = self._interceptor()
        if interceptor is None or not url.startswith(("http://", "https://")):
            return False
        return interceptor.check(url, self._url.host(), kind)

    def refresh_hiding(self) -> None:
        """Styles again, for when the site's shields were switched."""
        if self._document is not None:
            self._styles = Styler(self._document, viewport=(self._page_width(), self.height()),
                                  extra_css=self._hiding_css()).compute()
            self._layout()

    def zoomFactor(self) -> float:                            # noqa: N802
        return self._zoom

    def setZoomFactor(self, factor: float) -> None:           # noqa: N802
        self._zoom = max(0.25, min(5.0, factor))
        self._layout()

    def setUrl(self, url: QUrl) -> None:                      # noqa: N802
        self._navigate(QUrl(url), record=True)

    load = setUrl

    def setHtml(self, markup: str, base: QUrl = QUrl()) -> None:  # noqa: N802
        self._load_number += 1
        self._url = QUrl(base) if base.isValid() else QUrl("about:blank")
        self.loadStarted.emit()
        self._show(markup)
        self.urlChanged.emit(self.url())
        self.loadFinished.emit(True)

    def back(self) -> None:
        if self._history.canGoBack():
            self._history.index -= 1
            self._navigate(self._history.entries[self._history.index], record=False)

    def forward(self) -> None:
        if self._history.canGoForward():
            self._history.index += 1
            self._navigate(self._history.entries[self._history.index], record=False)

    def reload(self) -> None:
        if self._url.isValid() and self._url.scheme() not in ("", "about"):
            self._navigate(self._url, record=False)

    def stop(self) -> None:
        self._load_number += 1                    # a fetch still running is ignored

    # ------------------------------------------------------- navigation
    def _navigate(self, url: QUrl, record: bool) -> None:
        same_page = (self._document is not None and url.hasFragment()
                     and url.adjusted(QUrl.UrlFormattingOption.RemoveFragment)
                     == self._url.adjusted(QUrl.UrlFormattingOption.RemoveFragment))
        if record:
            self._history.push(url)
        if same_page:
            self._url = url
            self.urlChanged.emit(self.url())
            self._scroll_to_fragment(url.fragment())
            return
        interceptor = self._interceptor()
        if interceptor is not None and url.scheme() == "http":
            url = interceptor.upgrade(url)
        self._load_number += 1
        number = self._load_number
        self._url = url
        self._pending_fragment = url.fragment()
        headers = self._headers()
        self.urlChanged.emit(self.url())
        self.loadStarted.emit()
        self.loadProgress.emit(10)
        target = url.toString(QUrl.UrlFormattingOption.RemoveFragment)

        def work():
            # whatever goes wrong here becomes an error page, never a page
            # left loading for ever
            try:
                ok, final, text = fetch(target, headers=headers)
            except Exception as exc:                  # noqa: BLE001
                ok, final, text = False, target, f"MerlinEngine failed loading it: {exc}"
            self._fetched.emit(number, ok, final, text)

        threading.Thread(target=work, daemon=True).start()

    def _on_fetched(self, number: int, ok: bool, final: str, text: str) -> None:
        if number != self._load_number:
            return                                 # superseded or stopped
        self.loadProgress.emit(70)
        if final and QUrl(final) != self._url.adjusted(QUrl.UrlFormattingOption.RemoveFragment):
            final_url = QUrl(final)
            if self._pending_fragment:
                final_url.setFragment(self._pending_fragment)
            self._url = final_url
            self.urlChanged.emit(self.url())
        if not ok:
            import html

            text = (f"<title>Could not open this page</title><body style='font-family:"
                    f"sans-serif;margin:40px'><h2>MerlinEngine could not open this page</h2>"
                    f"<p style='color:#555'>{html.escape(self._url.toString())}</p>"
                    f"<p>{html.escape(text)}</p></body>")
        self._show(text)
        self.loadProgress.emit(100)
        self.loadFinished.emit(ok)
        if self._pending_fragment:
            self._scroll_to_fragment(self._pending_fragment)

    # ----------------------------------------------------- the pipeline
    def _show(self, markup: str) -> None:
        self._document = html_parser.parse(markup, self._url.toString())
        self._images = {}
        self._find = ("", -1)
        self._found_rect = None
        self._styles = Styler(self._document, viewport=(self._page_width(), self.height()),
                              extra_css=self._hiding_css()).compute()
        title = self._document.title or self._url.toString()
        if title != self._title:
            self._title = title
            self.titleChanged.emit(title)
        self._scroll = 0.0
        self._layout()
        self._load_images()

    # ----------------------------------------------------------- images
    def _load_images(self) -> None:
        """Fetch every image the page shows, off the UI thread.

        Each is checked with the content blocker first, as Chromium's tabs
        are; a blocked one takes no room. As images arrive the page is laid
        out again, since an image with no size given is as big as it is.
        """
        number = self._load_number
        headers = dict(self._headers())
        headers["Accept"] = "image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5"
        page = self._url.toString()
        if page.startswith(("http://", "https://")):
            headers["Referer"] = page
        wanted = []
        for element in self._document.root.elements():
            if element.tag != "img" or self._styles.get(element, {}).get("display") == "none":
                continue
            src = element.attrs.get("src", "").strip()
            if not src or src in self._images or src in wanted:
                continue
            address = self._url.resolved(QUrl(src)).toString()
            if self._blocked(address, "image"):
                self._images[src] = False
                continue
            wanted.append(src)
        if not wanted:
            if any(v is False for v in self._images.values()):
                self._layout()
            return

        def work(src, address):
            ok, _final, data, _kind = fetch_bytes(address, headers)
            self._image_fetched.emit(number, src, data if ok else b"", ok)

        for src in wanted[:200]:                  # enough for any ordinary page
            address = self._url.resolved(QUrl(src)).toString()
            threading.Thread(target=work, args=(src, address), daemon=True).start()

    def _on_image(self, number: int, src: str, data: bytes, ok: bool) -> None:
        if number != self._load_number:
            return
        picture = QImage.fromData(data) if ok and data else QImage()
        self._images[src] = picture if not picture.isNull() else False
        self._relayout.start()                   # batch several arrivals into one

    def _page_width(self) -> float:
        return max(100.0, float(self.width() - self.scrollbar.sizeHint().width()))

    def _layout(self) -> None:
        if self._document is None:
            return
        self._display = Layout(self._document, self._styles, self._page_width(), self._zoom,
                               images=self._images).run()
        self._update_scrollbar()
        self.update()

    def _update_scrollbar(self) -> None:
        total = self._display.height if self._display else 0.0
        spare = max(0, int(total - self.height()))
        self.scrollbar.blockSignals(True)
        self.scrollbar.setRange(0, spare)
        self.scrollbar.setPageStep(max(1, self.height()))
        self.scrollbar.setSingleStep(40)
        self.scrollbar.setValue(int(min(self._scroll, spare)))
        self.scrollbar.blockSignals(False)
        self._scroll = float(self.scrollbar.value())

    def _scrolled(self, value: int) -> None:
        self._scroll = float(value)
        self.update()

    def _scroll_to_fragment(self, fragment: str) -> None:
        if self._display and fragment in self._display.anchors:
            self.scrollbar.setValue(int(self._display.anchors[fragment]))

    # ------------------------------------------------------ find in page
    def findText(self, text: str, flags=None, callback=None) -> None:     # noqa: N802
        """Find text on the page, the next match on each call, and scroll to it."""
        from PyQt6.QtGui import QFontMetricsF

        if not text or not self._display:
            self._find = ("", -1)
            self._found_rect = None
            self.update()
            return
        backward = bool(flags) and "Backward" in str(flags)
        hits = []
        for item in self._display.items:
            for sub in (item[1] if item and item[0] == "group" else [item]):
                if sub and sub[0] == "text":
                    _k, x, baseline, words, font, _c, _d = sub
                    start = words.lower().find(text.lower())
                    while start >= 0:
                        metrics = QFontMetricsF(font)
                        left = x + metrics.horizontalAdvance(words[:start])
                        width = metrics.horizontalAdvance(words[start:start + len(text)])
                        hits.append(QRectF(left, baseline - metrics.ascent(), width,
                                           metrics.ascent() + metrics.descent()))
                        start = words.lower().find(text.lower(), start + 1)
        hits.sort(key=lambda r: (round(r.top()), r.left()))
        if not hits:
            self._find = (text, -1)
            self._found_rect = None
            self.update()
            return
        last = self._find[1] if self._find[0] == text else -1
        index = (last - 1) % len(hits) if backward else (last + 1) % len(hits)
        self._find = (text, index)
        self._found_rect = hits[index]
        top = hits[index].top()
        if not (self._scroll <= top <= self._scroll + self.height() - 40):
            self.scrollbar.setValue(int(max(0.0, top - self.height() / 3)))
        self.update()

    # ------------------------------------------------------------ Qt
    def resizeEvent(self, event) -> None:                     # noqa: N802
        bar = self.scrollbar.sizeHint().width()
        self.scrollbar.setGeometry(self.width() - bar, 0, bar, self.height())
        self._relayout.start()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:                      # noqa: N802
        painter = QPainter(self)
        canvas = self._display.canvas if self._display and self._display.canvas else (255, 255, 255, 255)
        painter.fillRect(self.rect(), QColor(*canvas))
        if self._display:
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            painter.translate(0, -self._scroll)
            visible = QRectF(0, self._scroll, self.width(), self.height())
            if self._found_rect is not None:
                painter.fillRect(self._found_rect.adjusted(-1, -1, 1, 1), QColor(255, 214, 0, 200))
            paint(painter, self._display, visible,
                  {k: v for k, v in self._images.items() if v is not False})
        painter.end()

    def wheelEvent(self, event) -> None:                      # noqa: N802
        self.scrollbar.setValue(self.scrollbar.value() - event.angleDelta().y())

    def keyPressEvent(self, event) -> None:                   # noqa: N802
        steps = {Qt.Key.Key_Down: 40, Qt.Key.Key_Up: -40,
                 Qt.Key.Key_PageDown: self.height() - 40, Qt.Key.Key_PageUp: -(self.height() - 40),
                 Qt.Key.Key_Space: self.height() - 40}
        if event.key() in steps:
            self.scrollbar.setValue(self.scrollbar.value() + steps[event.key()])
        elif event.key() == Qt.Key.Key_Home:
            self.scrollbar.setValue(0)
        elif event.key() == Qt.Key.Key_End:
            self.scrollbar.setValue(self.scrollbar.maximum())
        else:
            super().keyPressEvent(event)

    def _link_at(self, position) -> str:
        if not self._display:
            return ""
        point = position.toPointF() if hasattr(position, "toPointF") else position
        point = point.__class__(point.x(), point.y() + self._scroll)
        for rect, href in self._display.links:
            if rect.contains(point):
                return href
        return ""

    def mouseMoveEvent(self, event) -> None:                  # noqa: N802
        href = self._link_at(event.position())
        if href != self._hovered:
            self._hovered = href
            self.setCursor(Qt.CursorShape.PointingHandCursor if href else Qt.CursorShape.ArrowCursor)
            self.linkHovered.emit(self._url.resolved(QUrl(href)).toString() if href else "")

    def mouseReleaseEvent(self, event) -> None:               # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        href = self._link_at(event.position())
        if not href or href.lower().startswith("javascript:"):
            return
        self.setUrl(self._url.resolved(QUrl(href)))
