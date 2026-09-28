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


def _open(request, timeout: int, opener=None):
    """Open a request, through a cookie-keeping opener when there is one."""
    return opener.open(request, timeout=timeout) if opener is not None \
        else urllib.request.urlopen(request, timeout=timeout)


def cookie_opener(jar):
    """An opener that keeps and sends cookies in jar; None without one."""
    if jar is None:
        return None
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def new_cookie_jar():
    """A cookie jar for this session, or None if the module is missing.

    http.cookiejar is not among the modules Merlin.exe was built to carry, so
    an older executable may not have it; then pages load without cookies
    rather than not at all.
    """
    try:
        import http.cookiejar

        return http.cookiejar.CookieJar()
    except Exception:                                      # noqa: BLE001
        return None


def _registrable(host: str) -> str:
    """Roughly the site a host belongs to: example.co.uk for www.example.co.uk."""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in (
            "co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "me"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def fetch_bytes(url: str, headers: dict | None = None, timeout: int = 20,
                limit: int = 16 * 1024 * 1024, opener=None) -> tuple:
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
            with _open(request, timeout, opener) as response:
                return (True, response.geturl(), response.read(limit),
                        response.headers.get("Content-Type", ""))
        return False, url, f"MerlinEngine cannot open {scheme}: addresses yet.".encode(), ""
    except Exception as exc:                              # noqa: BLE001
        return False, url, str(exc).encode(), ""


def fetch(url: str, timeout: int = 20, headers: dict | None = None,
          data: bytes | None = None, content_type: str = "", opener=None) -> tuple:
    """(ok, final url, text or reason). Runs off the UI thread."""
    if url.startswith("view-source:"):
        import html

        ok, final, text = fetch(url[len("view-source:"):], timeout, headers, opener=opener)
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
            if data is not None:
                # a form sent by POST
                sent["Content-Type"] = content_type or "application/x-www-form-urlencoded"
            request = urllib.request.Request(url, data=data, headers=sent,
                                             method="POST" if data is not None else "GET")
            with _open(request, timeout, opener) as response:
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


def _svg_image(data: bytes) -> QImage:
    """An SVG file drawn to an image at its own size, for <img src="...svg">."""
    try:
        from PyQt6.QtCore import QByteArray
        from PyQt6.QtSvg import QSvgRenderer
    except Exception:                                      # noqa: BLE001
        return QImage()
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        return QImage()
    size = renderer.defaultSize()
    if size.width() <= 0 or size.height() <= 0:
        size.setWidth(300)
        size.setHeight(150)
    picture = QImage(size.width() * 2, size.height() * 2, QImage.Format.Format_ARGB32)
    picture.fill(0)
    painter = QPainter(picture)
    renderer.render(painter)
    painter.end()
    picture.setDevicePixelRatio(2.0)      # drawn at twice the size, for sharpness
    return picture


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
        self._widgets: dict = {}       # form control element -> its Qt widget
        self._places: dict = {}        # form control element -> (rect, fixed)
        self._buttons: list = []       # (rect, element, fixed) for buttons
        self._focused_once = False
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
    def _cookies(self):
        """This window's cookie jar: private windows keep theirs apart."""
        owner = self._host if self._host is not None else MerlinView
        jar = getattr(owner, "_merlin_cookie_jar", None)
        if jar is None and not getattr(owner, "_merlin_cookie_jar_tried", False):
            jar = new_cookie_jar()
            try:
                owner._merlin_cookie_jar = jar
                owner._merlin_cookie_jar_tried = True
            except Exception:                              # noqa: BLE001
                pass
        return jar

    def _navigate(self, url: QUrl, record: bool, body: bytes | None = None,
                  content_type: str = "") -> None:
        scheme = url.scheme().lower()
        if scheme == "merlin":
            # Merlin's own addresses, handled by the window as for Chromium's tabs
            action = {"listen": "start_dictation", "addtile": "add_start_tile"}.get(url.host())
            handler = getattr(self._host, action, None) if action else None
            if handler is not None:
                QTimer.singleShot(0, handler)
            return
        if scheme in ("mailto", "tel", "javascript", "sms"):
            return
        same_page = (self._document is not None and url.hasFragment() and body is None
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
        if body is not None and self._url.isValid():
            headers["Origin"] = self._url.adjusted(
                QUrl.UrlFormattingOption.RemovePath | QUrl.UrlFormattingOption.RemoveQuery
                | QUrl.UrlFormattingOption.RemoveFragment).toString()
        opener = cookie_opener(self._cookies())
        self.urlChanged.emit(self.url())
        self.loadStarted.emit()
        self.loadProgress.emit(10)
        target = url.toString(QUrl.UrlFormattingOption.RemoveFragment)

        def work():
            # whatever goes wrong here becomes an error page, never a page
            # left loading for ever
            try:
                ok, final, text = fetch(target, headers=headers, data=body,
                                        content_type=content_type, opener=opener)
            except Exception as exc:                  # noqa: BLE001
                ok, final, text = False, target, f"MerlinEngine failed loading it: {exc}"
            try:
                self._fetched.emit(number, ok, final, text)
            except RuntimeError:
                pass                     # the tab was closed while this loaded

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
        self._clear_controls()
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

        page_site = _registrable(self._url.host())
        own_opener = cookie_opener(self._cookies())

        def work(src, address):
            # cookies go only to the page's own site: another site's images
            # never learn who is looking
            same = _registrable(QUrl(address).host()) == page_site
            ok, _final, data, _kind = fetch_bytes(address, headers,
                                                  opener=own_opener if same else None)
            try:
                self._image_fetched.emit(number, src, data if ok else b"", ok)
            except RuntimeError:
                pass                     # the tab was closed while this loaded

        for src in wanted[:200]:                  # enough for any ordinary page
            address = self._url.resolved(QUrl(src)).toString()
            threading.Thread(target=work, args=(src, address), daemon=True).start()

    def _on_image(self, number: int, src: str, data: bytes, ok: bool) -> None:
        if number != self._load_number:
            return
        picture = QImage.fromData(data) if ok and data else QImage()
        if picture.isNull() and ok and b"<svg" in data[:4096]:
            picture = _svg_image(data)
        self._images[src] = picture if not picture.isNull() else False
        self._relayout.start()                   # batch several arrivals into one

    def _page_width(self) -> float:
        return max(100.0, float(self.width() - self.scrollbar.sizeHint().width()))

    def _layout(self) -> None:
        if self._document is None:
            return
        self._display = Layout(self._document, self._styles, self._page_width(), self._zoom,
                               images=self._images,
                               viewport_height=float(max(1, self.height())),
                               live_controls=True).run()
        self._update_scrollbar()
        self._sync_controls()
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
        self._place_controls()
        self.update()

    def _scroll_to_fragment(self, fragment: str) -> None:
        if self._display and fragment in self._display.anchors:
            self.scrollbar.setValue(int(self._display.anchors[fragment]))

    # ------------------------------------------------------------ forms
    def _clear_controls(self) -> None:
        for widget in self._widgets.values():
            widget.hide()
            widget.deleteLater()
        self._widgets.clear()
        self._places.clear()
        self._buttons = []
        self._focused_once = False

    def _sync_controls(self) -> None:
        """Make a widget for each form field laid out, and put each in its place."""
        if self._display is None:
            return
        found, buttons = {}, []
        for source, fixed in ((self._display, False), (self._display.fixed, True)):
            if source is None:
                continue
            for item in source.items:
                if item and item[0] == "control":
                    found[item[2]] = (item[1], fixed, item[3], item[4])
                elif item and item[0] == "button":
                    buttons.append((item[1], item[2], fixed))
        for element in [e for e in self._widgets if e not in found]:
            widget = self._widgets.pop(element)
            widget.hide()
            widget.deleteLater()
        for element, (rect, fixed, font, colour) in found.items():
            widget = self._widgets.get(element)
            if widget is None:
                widget = self._make_control(element)
                if widget is None:
                    continue
                self._widgets[element] = widget
            self._style_control(widget, font, colour)
        self._places = {e: (r, f) for e, (r, f, _font, _colour) in found.items()}
        self._buttons = buttons
        self._place_controls()
        if not self._focused_once and self._widgets:
            self._focused_once = True
            # autofocus, when the page itself has the focus: never taken from
            # the address bar
            if self.hasFocus():
                for element, widget in self._widgets.items():
                    if "autofocus" in element.attrs:
                        widget.setFocus()
                        break

    def _place_controls(self) -> None:
        bottom = self.height()
        right = self.width() - self.scrollbar.sizeHint().width()
        for element, widget in self._widgets.items():
            rect, fixed = self._places.get(element, (None, False))
            if rect is None:
                widget.hide()
                continue
            top = rect.top() - (0.0 if fixed else self._scroll)
            height = max(rect.height(), 8.0)
            widget.setGeometry(round(rect.left()), round(top), max(8, round(rect.width())),
                               round(height))
            widget.setVisible(top + height > 0 and top < bottom and rect.left() < right)

    def _make_control(self, element):
        from PyQt6.QtWidgets import (QCheckBox, QComboBox, QLineEdit, QPlainTextEdit,
                                     QRadioButton)

        from . import forms

        kind = forms.kind_of(element)
        value = forms.default_value(element)
        if kind in ("checkbox", "radio"):
            widget = QCheckBox(self) if kind == "checkbox" else QRadioButton(self)
            widget.setChecked(bool(value))
            if kind == "radio":
                # grouped by form and name, as on the web; Qt would group every
                # radio button in the view as one
                widget.setAutoExclusive(False)
                widget.toggled.connect(lambda on, e=element: self._radio_toggled(e, on))
        elif kind == "select":
            widget = QComboBox(self)
            options = [o for o in element.elements() if o.tag == "option"]
            for option in options:
                widget.addItem(" ".join(option.text().split()), forms.option_value(option))
            chosen = [i for i, o in enumerate(options) if "selected" in o.attrs]
            if chosen:
                widget.setCurrentIndex(chosen[0])
        elif kind == "textarea":
            widget = QPlainTextEdit(self)
            widget.setPlainText(str(value))
            widget.setFrameShape(QPlainTextEdit.Shape.NoFrame)
            widget.setPlaceholderText(element.attrs.get("placeholder", ""))
            if "readonly" in element.attrs:
                widget.setReadOnly(True)
        else:
            widget = QLineEdit(self)
            widget.setFrame(False)
            if kind == "file":
                widget.setPlaceholderText("Files cannot be sent from Merlin Engine yet")
                widget.setReadOnly(True)
            else:
                widget.setText(str(value))
                widget.setPlaceholderText(element.attrs.get("placeholder", ""))
            if kind == "password":
                widget.setEchoMode(QLineEdit.EchoMode.Password)
            try:
                if element.attrs.get("maxlength"):
                    widget.setMaxLength(max(0, int(element.attrs["maxlength"])))
            except ValueError:
                pass
            if "readonly" in element.attrs:
                widget.setReadOnly(True)
            # Enter sends the form, as in any browser
            widget.returnPressed.connect(lambda e=element: self._submit(e))
        if forms._disabled(element):
            widget.setEnabled(False)
        widget.setProperty("merlin_control", True)
        return widget

    @staticmethod
    def _style_control(widget, font, colour) -> None:
        from PyQt6.QtGui import QPalette

        widget.setFont(font)
        rgba = f"rgba({colour[0]},{colour[1]},{colour[2]},{colour[3] / 255:.2f})"
        name = type(widget).__name__
        if name in ("QLineEdit", "QPlainTextEdit"):
            # transparent: the page's CSS has drawn the field's box already
            widget.setStyleSheet(f"{name} {{ background: transparent; border: none; "
                                 f"padding: 0px; margin: 0px; color: {rgba}; }}")
            palette = widget.palette()
            softer = QColor(colour[0], colour[1], colour[2], max(60, colour[3] * 55 // 100))
            palette.setColor(QPalette.ColorRole.PlaceholderText, softer)
            widget.setPalette(palette)
        elif name == "QComboBox":
            widget.setStyleSheet(f"QComboBox {{ background: transparent; border: none; "
                                 f"color: {rgba}; padding: 0px 2px; }}")

    def _radio_toggled(self, element, on: bool) -> None:
        if not on:
            return
        from . import forms

        name, form = element.attrs.get("name"), forms.form_of(element)
        for other, widget in self._widgets.items():
            if other is not element and other.attrs.get("name") == name \
                    and forms.kind_of(other) == "radio" and forms.form_of(other) is form:
                widget.setChecked(False)

    def _values(self) -> dict:
        """What each form field holds now."""
        values = {}
        for element, widget in self._widgets.items():
            name = type(widget).__name__
            if name in ("QCheckBox", "QRadioButton"):
                values[element] = widget.isChecked()
            elif name == "QComboBox":
                values[element] = widget.currentData() if widget.currentIndex() >= 0 else ""
            elif name == "QPlainTextEdit":
                values[element] = widget.toPlainText()
            else:
                values[element] = widget.text()
        return values

    def _submit(self, element, submitter=None) -> None:
        """Send the form element belongs to, as its button or Enter asks."""
        from . import forms

        form = element if element.tag == "form" else forms.form_of(element)
        if form is None:
            return
        if submitter is None:
            # Enter in a field: the form's first submit button sends it
            submitter = next((c for c in forms.controls_of(form)
                              if forms.kind_of(c) in ("submit", "image")), None)
        pairs = forms.form_data(form, self._values(), submitter)
        where = forms.submission(form, pairs, self._url.toString(), submitter)
        if where is None:
            return
        url = QUrl(where["url"])
        new_tab = getattr(self._host, "new_tab", None)
        if where["target"] == "_blank" and where["method"] == "GET" and new_tab is not None:
            new_tab(where["url"])
        elif where["method"] == "POST":
            self._navigate(url, record=True, body=where["body"], content_type=where["type"])
        else:
            self.setUrl(url)

    def _reset(self, form) -> None:
        from . import forms

        for control in forms.controls_of(form):
            widget = self._widgets.get(control)
            if widget is None:
                continue
            value = forms.default_value(control)
            name = type(widget).__name__
            if name in ("QCheckBox", "QRadioButton"):
                widget.setChecked(bool(value))
            elif name == "QComboBox":
                index = widget.findData(value)
                widget.setCurrentIndex(max(0, index))
            elif name == "QPlainTextEdit":
                widget.setPlainText(str(value))
            else:
                widget.setText(str(value))

    def _button_at(self, position):
        point = position.toPointF() if hasattr(position, "toPointF") else position
        for rect, element, fixed in reversed(self._buttons):
            y = point.y() + (0.0 if fixed else self._scroll)
            if rect.contains(point.__class__(point.x(), y)):
                return element
        return None

    def _press_button(self, element) -> None:
        from . import forms

        kind = forms.kind_of(element)
        if forms._disabled(element):
            return
        if kind in ("submit", "image"):
            self._submit(element, submitter=element)
        elif kind == "reset":
            form = forms.form_of(element)
            if form is not None:
                self._reset(form)

    def _activate_label(self, label) -> None:
        """A click on a <label>: tick its box, or put the cursor in its field."""
        from . import forms

        target = None
        if label.attrs.get("for"):
            root = label
            while root.parent is not None:
                root = root.parent
            target = next((e for e in root.elements() if e.attrs.get("id") == label.attrs["for"]), None)
        if target is None:
            target = next((e for e in label.elements() if forms.kind_of(e)), None)
        if target is None:
            return
        widget = self._widgets.get(target)
        if widget is None:
            if forms.kind_of(target) in ("submit", "image", "reset"):
                self._press_button(target)
            return
        name = type(widget).__name__
        if name == "QCheckBox":
            widget.setChecked(not widget.isChecked())
        elif name == "QRadioButton":
            widget.setChecked(True)
        else:
            widget.setFocus()

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
            pictures = {k: v for k, v in self._images.items() if v is not False}
            paint(painter, self._display, visible, pictures)
            if self._display.fixed is not None:
                # position: fixed stays where it is on the window as the page scrolls
                painter.resetTransform()
                paint(painter, self._display.fixed,
                      QRectF(0, 0, self.width(), self.height()), pictures)
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
        if self._display.fixed is not None:
            # fixed boxes sit on top, in window coordinates
            for rect, href in self._display.fixed.links:
                if rect.contains(point):
                    return href
        point = point.__class__(point.x(), point.y() + self._scroll)
        for rect, href in self._display.links:
            if rect.contains(point):
                return href
        return ""

    def mouseMoveEvent(self, event) -> None:                  # noqa: N802
        from .layout import LabelTarget

        href = self._link_at(event.position())
        button = self._button_at(event.position())
        state = (href, button)
        if state != self._hovered:
            self._hovered = state
            pointing = button is not None or (href and not isinstance(href, LabelTarget))
            self.setCursor(Qt.CursorShape.PointingHandCursor if pointing else Qt.CursorShape.ArrowCursor)
            shown = "" if not href or isinstance(href, LabelTarget) else \
                self._url.resolved(QUrl(href)).toString()
            self.linkHovered.emit(shown)

    def mouseReleaseEvent(self, event) -> None:               # noqa: N802
        from .layout import LabelTarget

        if event.button() != Qt.MouseButton.LeftButton:
            return
        button = self._button_at(event.position())
        if button is not None:
            self._press_button(button)
            return
        href = self._link_at(event.position())
        if isinstance(href, LabelTarget):
            self._activate_label(href.element)
            return
        if not href or href.lower().startswith("javascript:"):
            return
        self.setUrl(self._url.resolved(QUrl(href)))
