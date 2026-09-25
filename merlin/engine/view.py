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

from PyQt6.QtCore import QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter
from PyQt6.QtWidgets import QScrollBar, QWidget

from . import html as html_parser
from .css import Styler
from .layout import Layout
from .paint import paint

USER_AGENT = "Mozilla/5.0 (compatible; MerlinEngine/0.1)"


def fetch(url: str, timeout: int = 20) -> tuple:
    """(ok, final url, text or reason). Runs off the UI thread."""
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
            request = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-GB,en;q=0.8"})
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


class MerlinView(QWidget):
    titleChanged = pyqtSignal(str)
    urlChanged = pyqtSignal(QUrl)
    iconChanged = pyqtSignal(QIcon)
    loadStarted = pyqtSignal()
    loadProgress = pyqtSignal(int)
    loadFinished = pyqtSignal(bool)
    linkHovered = pyqtSignal(str)
    _fetched = pyqtSignal(int, bool, str, str)      # load number, ok, url, text

    def __init__(self, parent=None):
        super().__init__(parent)
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

    # ------------------------------------------------ what Merlin asks for
    def url(self) -> QUrl:
        return QUrl(self._url)

    def title(self) -> str:
        return self._title

    def icon(self) -> QIcon:
        return QIcon()

    def history(self) -> _History:
        return self._history

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
        self._load_number += 1
        number = self._load_number
        self._url = url
        self._pending_fragment = url.fragment()
        self.urlChanged.emit(self.url())
        self.loadStarted.emit()
        self.loadProgress.emit(10)
        target = url.toString(QUrl.UrlFormattingOption.RemoveFragment)

        def work():
            # whatever goes wrong here becomes an error page, never a page
            # left loading for ever
            try:
                ok, final, text = fetch(target)
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
        self._styles = Styler(self._document, viewport=(self._page_width(), self.height())).compute()
        title = self._document.title or self._url.toString()
        if title != self._title:
            self._title = title
            self.titleChanged.emit(title)
        self._scroll = 0.0
        self._layout()

    def _page_width(self) -> float:
        return max(100.0, float(self.width() - self.scrollbar.sizeHint().width()))

    def _layout(self) -> None:
        if self._document is None:
            return
        self._display = Layout(self._document, self._styles, self._page_width(), self._zoom).run()
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
            paint(painter, self._display, visible)
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
