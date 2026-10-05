"""MerlinView: a page rendered by MerlinEngine, as a Qt widget.

Its signals carry the same names and meanings as the ones Merlin's browser
window already listens to on Chromium's view (titleChanged, urlChanged,
loadStarted, loadProgress, loadFinished, linkHovered), so it can in time sit
where Chromium's view sits. Loading happens off the UI thread; parsing,
styling and layout run on it, and are fast enough for ordinary documents.

Not here yet: JavaScript, cookies, forms, images, and the content blocker.
"""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, QRectF, QStandardPaths, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter
from PyQt6.QtWidgets import QScrollBar, QWidget

from . import html as html_parser
from .css import Styler, media_matches
from .layout import Layout
from .paint import paint

# What a page load says about itself when Merlin's own user agent is not to
# hand. "MerlinEngine/0.1" was refused outright by sites' bot protection, with
# 403 Forbidden, so it now reads as the browser it is.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def _body(response, limit: int) -> bytes:
    """A response's body, decompressed as the server's Content-Encoding says."""
    import zlib

    data = response.read(limit)
    encoding = (response.headers.get("Content-Encoding", "") or "").strip().lower()
    try:
        if encoding in ("gzip", "x-gzip"):
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data)
        if encoding == "deflate":
            try:
                return zlib.decompressobj().decompress(data)
            except zlib.error:
                return zlib.decompressobj(-zlib.MAX_WBITS).decompress(data)
    except zlib.error:
        pass
    return data


def _reader(response):
    """read(n) for a download, decompressing as it streams when need be."""
    import zlib

    encoding = (response.headers.get("Content-Encoding", "") or "").strip().lower()
    if encoding not in ("gzip", "x-gzip", "deflate"):
        return response.read
    unpack = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding != "deflate" else zlib.MAX_WBITS)

    def read(n: int) -> bytes:
        while True:
            chunk = response.read(n)
            if not chunk:
                return unpack.flush()
            out = unpack.decompress(chunk)
            if out:
                return out
    return read


def _subresource(headers: dict, destination: str) -> None:
    """Mark a request as a stylesheet's or an image's, not a page load's."""
    headers["Sec-Fetch-Dest"] = destination
    headers["Sec-Fetch-Mode"] = "no-cors"
    headers["Sec-Fetch-Site"] = "same-origin"
    headers.pop("Sec-Fetch-User", None)
    headers.pop("Upgrade-Insecure-Requests", None)


def page_headers(user_agent: str = "") -> dict:
    """What a browser sends when it asks for a page."""
    return {"User-Agent": user_agent or USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"}


def _open(request, timeout: int, opener=None):
    """Open a request, through a cookie-keeping opener when there is one."""
    return opener.open(request, timeout=timeout) if opener is not None \
        else urllib.request.urlopen(request, timeout=timeout)


_LENIENT: dict = {}


def lenient_context(level: str):
    """A TLS context for a site whose certificate problems were allowed.

    "dates" checks the certificate fully, chain and name, and sets aside only
    its dates: a computer whose clock is behind sees every new certificate as
    not yet valid. "any" trusts the site's certificate as it is, as clicking
    through in Chromium does, for a router's self-signed one say.
    """
    import ssl

    context = _LENIENT.get(level)
    if context is None:
        context = ssl.create_default_context()
        if level == "dates":
            context.verify_flags |= 0x200000          # X509_V_FLAG_NO_CHECK_TIME
        else:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        _LENIENT[level] = context
    return context


def cookie_opener(jar, lenient: str = ""):
    """An opener keeping cookies in jar, and lenient with an allowed site's
    certificate; None when neither is wanted."""
    handlers = []
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    if lenient:
        handlers.append(urllib.request.HTTPSHandler(context=lenient_context(lenient)))
    return urllib.request.build_opener(*handlers) if handlers else None


def certificate_failure(text) -> str:
    """The certificate problem in a failure's message, or "" if it is not one."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = text or ""
    if "CERTIFICATE_VERIFY_FAILED" not in text:
        return ""
    found = re.search(r"certificate verify failed: ([^(]+)", text)
    return (found.group(1).strip() if found else "certificate verify failed")


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


_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?["']?([^"')\s;]+)["']?\s*\)?\s*([^;]*);""",
                     re.I)


def _expand_imports(text: str, base: str, get) -> str:
    """A stylesheet with its @import rules replaced by what they import."""
    def replace(match):
        imported = get(urllib.parse.urljoin(base, match.group(1)))
        media = match.group(2).strip()
        if media and not media.lower().startswith(("layer", "supports")):
            return f"@media {media} {{{imported}}}"
        return imported
    return _IMPORT.sub(replace, text)


def _registrable(host: str) -> str:
    """Roughly the site a host belongs to: example.co.uk for www.example.co.uk.

    An IP address, or a name with no dots such as localhost, is a site of its
    own: 127.0.0.1 had been taken as the site "0.1", so a site allowed
    JavaScript in the window was not the one the tab looked for.
    """
    import ipaddress

    bare = host.lower().strip(".").strip("[]")
    try:
        ipaddress.ip_address(bare)
        return bare
    except ValueError:
        pass
    parts = bare.split(".")
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
                return (True, response.geturl(), _body(response, limit),
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
                body = _body(response, 8 * 1024 * 1024)
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


def fetch_page(url: str, headers: dict | None = None, data: bytes | None = None,
               content_type: str = "", opener=None, download_dir: str = "",
               progress=None) -> tuple:
    """(ok, final url, page, downloaded path or "") for any address a tab opens.

    FTP, SMB and local folders go through remote.py. A web response that is
    not a page, text or an image is saved to the Downloads folder, streamed to
    disk, and the page says where; an image is shown on a page of its own.
    """
    from . import remote

    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme in ("ftp", "ftps", "smb", "file") and not url.startswith("view-source:"):
        return remote.open_remote(url, download_dir, progress)
    if scheme not in ("http", "https"):
        ok, final, text = fetch(url, headers=headers, data=data,
                                content_type=content_type, opener=opener)
        return ok, final, text, ""
    sent = {"User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-GB,en;q=0.8"}
    sent.update(headers or {})
    if data is not None:
        sent["Content-Type"] = content_type or "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=sent,
                                     method="POST" if data is not None else "GET")
    try:
        with _open(request, 20, opener) as response:
            final = response.geturl()
            kind = response.headers.get("Content-Type", "")
            disposition = response.headers.get("Content-Disposition", "") or ""
            name = _download_name(disposition, final)
            attachment = disposition.lower().startswith("attachment")
            shown = remote.shown_type(kind, "" if kind else name)
            if not attachment and shown == "html":
                return True, final, _decode(_body(response, 8 * 1024 * 1024), kind), ""
            if not attachment and shown == "text":
                body = _body(response, 8 * 1024 * 1024)
                return True, final, remote.text_page(name, body), ""
            if not attachment and kind.lower().startswith("image/"):
                import html as escape

                return True, final, (f"<title>{escape.escape(name)}</title><body style='margin:0;"
                                     f"background:#2b2d33;text-align:center'><img src="
                                     f"'{escape.escape(final)}' style='max-width:100%'></body>"), ""
            if not download_dir:
                return False, final, f"This is {kind.split(';')[0] or 'a file'}, not a page.", ""
            total = int(response.headers.get("Content-Length", "0") or 0)
            if (response.headers.get("Content-Encoding", "") or "").strip():
                total = 0                  # the length given is of the compressed body
            path, size = remote.save_stream(_reader(response), download_dir, name, progress, total)
            return True, final, remote.download_page(path, size), path
    except urllib.error.HTTPError as error:
        # A server's own answer to an error (a site's 404 page, Google's
        # "unusual traffic" page for a 429) is shown, as browsers show it;
        # only an answer with no page in it gets Merlin Engine's own.
        try:
            kind = error.headers.get("Content-Type", "") if error.headers else ""
            if "html" in kind or "text/plain" in kind:
                body = _decode(_body(error, 8 * 1024 * 1024), kind)
                if body.strip():
                    return True, error.geturl() or url, body, ""
        except Exception:                                  # noqa: BLE001
            pass
        return False, url, str(error), ""
    except Exception as exc:                              # noqa: BLE001
        return False, url, str(exc), ""


def _download_name(disposition: str, url: str) -> str:
    """The name to save a download as: the server's, or the address's last part."""
    found = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", disposition, re.I)
    if found:
        return urllib.parse.unquote(found.group(1).strip().strip('"'))
    found = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.I)
    if found:
        return found.group(1).strip()
    tail = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1])
    return tail or "download"


# A page with more elements than this is laid out on a thread: GitHub's front
# page has about 1,800; an ordinary article a few hundred.
BIG_PAGE = 1200


def needs_javascript(document) -> bool:
    """Whether a page shows little or nothing until scripts build it.

    Such a page, YouTube's among them, arrives as an empty frame and many
    scripts; Merlin Engine cannot run them yet. A page with plenty of text is
    fine however many scripts it has, since it shows without them.
    """
    if document is None:
        return False
    skipped = {"script", "style", "noscript", "template", "head", "title"}
    text = []
    scripts = script_size = 0

    def walk(node):
        nonlocal scripts, script_size
        for child in node.children:
            tag = getattr(child, "tag", None)
            if tag is None:
                text.append(child.data)
            elif tag == "script":
                scripts += 1
                script_size += len(child.text())
            elif tag not in skipped:
                walk(child)

    walk(document.root)
    visible = len(" ".join("".join(text).split()))
    asks = any("javascript" in e.text().lower() and ("enable" in e.text().lower()
                                                    or "turn on" in e.text().lower()
                                                    or "required" in e.text().lower())
               for e in document.root.elements() if e.tag == "noscript")
    if asks and visible < 1500:
        return True
    return visible < 200 and (scripts >= 3 or script_size > 20000)


_FONTS: dict = {}          # a font file's hash -> the families Qt knows it by


def register_font(data: bytes) -> list:
    """Give Qt a web font; the family names it knows it by, or [] if it cannot.

    Each file is given once a session, however many pages use it. Qt reads
    TrueType, OpenType and, on most systems, WOFF and WOFF2; a WOFF it will not
    take is unpacked here and given again.
    """
    import hashlib

    from PyQt6.QtCore import QByteArray
    from PyQt6.QtGui import QFontDatabase

    key = hashlib.sha1(data).hexdigest()
    if key in _FONTS:
        return _FONTS[key]
    font_id = QFontDatabase.addApplicationFontFromData(QByteArray(data))
    if font_id < 0 and data[:4] == b"wOFF":
        unpacked = woff_to_sfnt(data)
        if unpacked:
            font_id = QFontDatabase.addApplicationFontFromData(QByteArray(unpacked))
    names = list(QFontDatabase.applicationFontFamilies(font_id)) if font_id >= 0 else []
    _FONTS[key] = names
    return names


def woff_to_sfnt(data: bytes):
    """A WOFF font as the TrueType or OpenType file it packs; None if broken.

    WOFF is the font's own tables, each compressed with zlib, and a header
    saying where they are: the tables are unpacked and laid out again as the
    file they came from.
    """
    import struct
    import zlib

    try:
        flavor, _length, count = struct.unpack(">4sIH", data[4:14])
        tables = []
        for i in range(count):
            tag, offset, packed, size, checksum = struct.unpack(
                ">4sIIII", data[44 + 20 * i:64 + 20 * i])
            raw = data[offset:offset + packed]
            tables.append((tag, checksum, zlib.decompress(raw) if packed < size else raw))
        power = 1
        while power * 2 <= count:
            power *= 2
        search = power * 16
        out = [struct.pack(">4sHHHH", flavor, count, search, power.bit_length() - 1,
                           count * 16 - search)]
        offset = 12 + 16 * count
        records, bodies = [], []
        for tag, checksum, body in sorted(tables):
            records.append(struct.pack(">4sIII", tag, checksum, offset, len(body)))
            padded = body + b"\0" * (-len(body) % 4)
            bodies.append(padded)
            offset += len(padded)
        return b"".join(out + records + bodies)
    except (struct.error, zlib.error, ValueError):
        return None


def _gather_fonts(sheets_with_bases) -> list:
    """The @font-face rules of the page's stylesheets, their urls made whole."""
    from .css import font_faces

    faces = []
    for text, base in sheets_with_bases:
        if not text or "@font-face" not in text.lower():
            continue
        for face in font_faces(text):
            face["sources"] = [(urllib.parse.urljoin(base, url), kind)
                               for url, kind in face["sources"]]
            faces.append(face)
    return faces


HTTP_ONLY_HOSTS: set = set()       # sites found this session to answer only on http
RETRIES = 2                        # further tries for a connection that failed
RETRY_PAUSES = (1.0, 2.5)          # seconds before each


def without_noscript(markup: str) -> str:
    """markup with its <noscript> blocks taken out: for a page whose scripts
    run, as their content is then nothing at all, styles and refreshes too."""
    return re.sub(r"<noscript\b[^>]*>.*?</noscript\s*>", "", markup or "", flags=re.I | re.S)


def connection_failure(text: str) -> bool:
    """Whether a page failed to connect at all, which trying again can mend.

    Not a server's answer (404, 500), nor a certificate problem: those come
    back the same however often asked.
    """
    text = (text or "").lower()
    if "certificate_verify_failed" in text or "http error" in text:
        return False
    return any(sign in text for sign in (
        "connection refused", "connection reset", "connection aborted", "timed out", "timeout",
        "name or service not known", "getaddrinfo failed", "temporary failure in name resolution",
        "nodename nor servname", "no route to host", "network is unreachable", "remote end closed",
        "eof occurred", "winerror 10054", "winerror 10060", "winerror 10061", "winerror 11001",
        "urlopen error"))


def _note(text: str) -> None:
    """A line in merlin-log.txt."""
    try:
        from .. import crashlog

        crashlog.note(text)
    except Exception:                                      # noqa: BLE001
        pass


def _skew_for(host: str, problem: str):
    """For a date problem, how far off this computer's clock is; else None."""
    from ..clock import clock_skew, is_date_problem

    if not host or not is_date_problem(problem):
        return None
    try:
        return clock_skew(host)
    except Exception:                                      # noqa: BLE001
        return None


def _svg_picture(data: bytes):
    """An SVG file as an SvgPicture, drawn sharp at the size it is shown."""
    try:
        from PyQt6.QtCore import QByteArray
        from PyQt6.QtSvg import QSvgRenderer

        from .paint import SvgPicture
    except Exception:                                      # noqa: BLE001
        return None
    renderer = QSvgRenderer(QByteArray(data))
    return SvgPicture(renderer) if renderer.isValid() else None


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
    _fetched = pyqtSignal(int, bool, str, str, object)   # number, ok, url, text, prepared
    _image_fetched = pyqtSignal(int, str, object, bool)  # load number, src, image or data, ok
    loginSubmitted = pyqtSignal(str, str, str)       # page, username, password
    downloadFinished = pyqtSignal(str)               # the saved file
    certTrouble = pyqtSignal(str, str)               # host, certificate problem
    _retrying = pyqtSignal(int, int, int)            # load number, attempt, attempts in all
    _fell_back = pyqtSignal(int, str)                # load number, host opened over http
    _laid_out = pyqtSignal(int, object)              # layout number, the display list
    _script_prepared = pyqtSignal(int, str, object)  # load number, page from scripts, prepared
    _restyled = pyqtSignal(int, object)              # load number, the worker's answer
    _icon_fetched = pyqtSignal(int, bytes)           # load number, the icon's data
    _font_fetched = pyqtSignal(int, str, bytes)      # load number, CSS family, the font file
    needsScript = pyqtSignal(str)                    # a page built by JavaScript: its address

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
        self.cert_troubles: list = []  # items held back for certificate problems
        self._cert_failed_url = None   # the page a certificate stopped
        self._sheets = None            # the page's stylesheets, linked and inline
        self._styler = None
        self._markup = ""              # the page as it came, for styling it again
        self._patches: dict = {}       # element number -> attributes changed since
        self._media: dict = {}         # each @media query, and whether it held
        self._viewport_units = False   # whether the styles used vw, vh, vmin or vmax
        self._styled_at = None         # the window size they were worked out for
        self._restyling = False
        self._restyle_again = False
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
        self._image_relayout = QTimer(self)
        self._image_relayout.setSingleShot(True)
        self._image_relayout.setInterval(250)
        self._image_relayout.timeout.connect(self._layout)
        self._fetched.connect(self._on_fetched)
        self._image_fetched.connect(self._on_image)
        self._restyled.connect(self._on_restyled)
        self._retrying.connect(self._on_retrying)
        self._fell_back.connect(self._on_fell_back)
        self._laid_out.connect(self._on_laid_out)
        self._script_prepared.connect(self._apply_script_dom)
        # JavaScript: a Deno process for the page, where the site is allowed
        self._script = None
        self._script_waiting: dict = {}
        self._script_next = 0
        self._script_busy = False
        self._script_queued = None
        self._script_rest_until = 0.0
        self._script_rest = QTimer(self)
        self._script_rest.setSingleShot(True)
        self._script_rest.timeout.connect(self._script_rested)
        self._by_script_id: dict = {}
        from collections import deque

        self.console_lines = deque(maxlen=400)
        self.script_status = "not started"
        # CSS animations: drawn about 30 times a second, only while the page
        # has some and the tab can be seen
        self._animation_epoch = time.monotonic()
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(33)
        self._animation_timer.timeout.connect(self._animation_tick)
        self._script_scroll = QTimer(self)
        self._script_scroll.setSingleShot(True)
        self._script_scroll.setInterval(100)
        self._script_scroll.timeout.connect(self._send_scroll)
        self._layout_number = 0
        self._layout_running = False
        self._layout_again = False
        self._finish_when_laid_out = None
        self._element_count = 0
        self._icon_fetched.connect(self._on_icon)
        self._font_fetched.connect(self._on_font)
        self._font_faces: list = []
        self._font_aliases: dict = {}
        self._icon = QIcon()

    # ------------------------------------------------ what Merlin asks for
    def url(self) -> QUrl:
        return QUrl(self._url)

    def title(self) -> str:
        return self._title

    def icon(self) -> QIcon:
        return self._icon

    def history(self) -> _History:
        return self._history

    def page(self) -> _Page:
        return self._page

    # --------------------------------------------- Merlin's window, if any
    def _interceptor(self):
        return getattr(self._host, "interceptor", None)

    def _headers(self) -> dict:
        """What every request from this tab sends: a browser's headers, Merlin's
        user agent, and Do Not Track when that is switched on."""
        agent = ""
        profile = self._page.profile() if self._page is not None else None
        try:
            agent = profile.httpUserAgent() if profile is not None else ""
        except Exception:                                  # noqa: BLE001
            agent = ""
        settings = getattr(self._host, "settings", None)
        if not agent and settings is not None:
            agent = settings.get("user_agent") or ""
        headers = page_headers(agent)
        if settings is not None and settings.get("send_do_not_track"):
            headers.update({"DNT": "1", "Sec-GPC": "1"})
        return headers

    def _hiding_css(self) -> str:
        """The content blocker's hiding rules for this site, as CSS."""
        return self._hiding_css_for(self._url.host())

    def _hiding_css_for(self, host: str) -> str:
        finder = getattr(self._host, "cosmetic_css_for", None)
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
            self._restyle()

    def zoomFactor(self) -> float:                            # noqa: N802
        return self._zoom

    def setZoomFactor(self, factor: float) -> None:           # noqa: N802
        self._zoom = max(0.25, min(5.0, factor))
        self._layout()

    def setUrl(self, url: QUrl) -> None:                      # noqa: N802
        self._navigate(QUrl(url), record=True)

    load = setUrl

    def setHtml(self, markup: str, base: QUrl = QUrl()) -> None:  # noqa: N802
        self._stop_script()
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
    def _leniency(self, host: str) -> str:
        allowed = getattr(self._host, "certificate_allowed", None)
        return allowed(_registrable(host)) if allowed is not None and host else ""

    def _cert_trouble(self, url: str, problem: str) -> None:
        """Something the page pulls in, held back for its certificate."""
        host = QUrl(url).host()
        if len(self.cert_troubles) < 500:
            self.cert_troubles.append({"url": url, "host": host, "description": problem,
                                       "kind": "", "main": False})
        try:
            self.certTrouble.emit(host, problem)
        except RuntimeError:
            pass

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
        if scheme != "merlin":
            self._stop_script()
        # for this navigation only: the host typed with no scheme, if it was
        fallback_host = self.property("http_fallback") or ""
        self.setProperty("http_fallback", None)
        # a site found to answer only on http, earlier this session: reloads
        # and links to its https address are tried over http too
        if not fallback_host and url.host().lower() in HTTP_ONLY_HOSTS:
            fallback_host = url.host()
        # whether this page's scripts will run: then <noscript> is nothing
        scripting = False
        allowed = getattr(self._host, "javascript_allowed", None)
        if allowed is not None and scheme in ("http", "https") and allowed(_registrable(url.host())):
            scripting = bool(getattr(self._host, "deno_for_scripts", lambda: "")())
        if scheme == "merlin":
            # Merlin's own addresses, handled by the window as for Chromium's tabs
            if url.host() == "allow-certificate":
                site = url.path().strip("/")
                level = "dates" if "level=dates" in url.query() else "any"
                allow = getattr(self._host, "allow_certificate_site", None)
                if allow is not None and site:
                    allow(site, level)
                    self.cert_troubles = []
                    if self._cert_failed_url is not None:
                        self._navigate(QUrl(self._cert_failed_url), record=False)
                return
            action = {"listen": "start_dictation", "addtile": "add_start_tile"}.get(url.host())
            handler = getattr(self._host, action, None) if action else None
            if handler is not None:
                QTimer.singleShot(0, handler)
            return
        if scheme in ("mailto", "tel", "javascript", "sms"):
            return
        # a site sent to Chromium before, because it needs JavaScript, goes there
        route = getattr(self._host, "route_to_chromium", None)
        if route is not None and route(url):
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
        host_name = url.host()
        lenient = self._leniency(host_name)
        opener = cookie_opener(self._cookies(), lenient)
        plain_opener = cookie_opener(None, lenient)
        self.cert_troubles = []
        # what styling needs, taken here on the UI thread: the loading thread
        # then parses the CSS and runs the cascade without touching the window
        viewport = (self._page_width(), float(max(1, self.height())))
        hiding = self._hiding_css_for(host_name)
        from PyQt6.QtCore import QStandardPaths

        download_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation) or os.path.expanduser("~")

        def progress(percent: int) -> None:
            try:
                if number == self._load_number:
                    self.loadProgress.emit(percent)
            except RuntimeError:
                pass                             # the tab was closed
        self.urlChanged.emit(self.url())
        self.loadStarted.emit()
        self.loadProgress.emit(10)
        target = url.toString(QUrl.UrlFormattingOption.RemoveFragment)

        def work():
            # whatever goes wrong here becomes an error page, never a page
            # left loading for ever
            prepared = None
            try:
                for attempt in range(1, RETRIES + 2):
                    ok, final, text, saved = fetch_page(
                        target, headers=headers, data=body, content_type=content_type,
                        opener=opener, download_dir=download_dir, progress=progress)
                    # a connection that failed is tried again, twice, before the
                    # page says it could not be opened: a site that answers on
                    # the second try had shown "could not open" first
                    if ok or attempt > RETRIES or not connection_failure(text) or body is not None:
                        break
                    try:
                        self._retrying.emit(number, attempt + 1, RETRIES + 1)
                    except RuntimeError:
                        return
                    time.sleep(RETRY_PAUSES[attempt - 1])
                    if number != self._load_number:
                        return                         # gone elsewhere meanwhile
                if (not ok and body is None and connection_failure(text) and fallback_host
                        and target.startswith("https://")
                        and urllib.parse.urlsplit(target).hostname == fallback_host.lower()):
                    # typed with no scheme: a site that does not answer on
                    # https at all is tried over http, as Brave does
                    plain = "http://" + target[len("https://"):]
                    ok, final, text, saved = fetch_page(
                        plain, headers=headers, data=body, content_type=content_type,
                        opener=opener, download_dir=download_dir, progress=progress)
                    if ok:
                        try:
                            self._fell_back.emit(number, fallback_host)
                        except RuntimeError:
                            return
                progress(25)                      # the page is here
                if ok:
                    # the page as it came, for the debugging zip: with the
                    # scripts kept beside it, it can be run again from the start
                    if number == self._load_number and not saved:
                        self._arrived_markup = text
                    if scripting and not saved:
                        # As a browser running scripts does: what is inside
                        # <noscript> is nothing. Google's hid every div there,
                        # for browsers without scripts, and sent them elsewhere.
                        text = without_noscript(text)
                    # parsed here, off the UI thread, with its stylesheets
                    # fetched before the page is shown, as browsers do
                    prepared = self._prepare(text, final, headers, opener, host_name,
                                             viewport, hiding, plain_opener)
                    if saved:
                        prepared["download"] = saved
                problem = "" if ok else certificate_failure(text)
                if problem:
                    prepared = {"certificate": problem, "skew": _skew_for(url.host(), problem)}
            except Exception as exc:                  # noqa: BLE001
                ok, final, text = False, target, f"MerlinEngine failed loading it: {exc}"
            try:
                self._fetched.emit(number, ok, final, text, prepared)
            except RuntimeError:
                pass                     # the tab was closed while this loaded

        threading.Thread(target=work, daemon=True).start()

    def _certificate_page(self, problem: str, skew) -> str:
        """Why a page could not be opened, for its certificate, and a way on."""
        import html

        from ..clock import describe_skew, is_date_problem

        self._cert_failed_url = self._url.toString()
        host = self._url.host()
        site = _registrable(host)
        self.cert_troubles = [{"url": self._url.toString(), "host": host,
                               "description": problem, "kind": "", "main": True}]
        try:
            self.certTrouble.emit(host, problem)
        except RuntimeError:
            pass
        dates = is_date_problem(problem)
        why = describe_skew(skew) if (dates and skew is not None) else (
            "A certificate that is not valid yet, or has expired, usually means this "
            "computer's clock is wrong." if dates else
            "The site's certificate is not one this computer trusts: a router's or a "
            "device's own, say, or one standing in for the real site.")
        level = "dates" if dates else "any"
        going_on = ("Merlin Engine still checks the certificate is the site's own and "
                    "properly signed, and sets aside only its dates."
                    if dates else "Merlin Engine will trust the certificate it is shown.")
        return (f"<title>Certificate problem</title><body style='font-family:sans-serif;"
                f"margin:40px;color:#1d2030;max-width:720px'>"
                f"<h2>{html.escape(host)}'s certificate could not be trusted</h2>"
                f"<p style='color:#555'>{html.escape(problem)}</p>"
                f"<p style='line-height:1.5'>{html.escape(why)}</p>"
                f"<p style='line-height:1.5;color:#555'>Continuing is for this session only. "
                f"{html.escape(going_on)}</p>"
                f"<p><a href='merlin://allow-certificate/{html.escape(site)}?level={level}' "
                f"style='display:inline-block;padding:8px 16px;background:#3a5bd9;color:white;"
                f"border-radius:6px;text-decoration:none'>Continue to {html.escape(site)}</a></p>"
                f"</body>")

    def _stylesheet_cache(self) -> dict:
        owner = self._host if self._host is not None else MerlinView
        cache = getattr(owner, "_merlin_stylesheets", None)
        if cache is None:
            cache = {}
            try:
                owner._merlin_stylesheets = cache
            except Exception:                              # noqa: BLE001
                pass
        return cache

    def _prepare(self, markup: str, page_url: str, headers: dict, opener, host_name: str,
                 viewport=None, hiding: str = "", plain_opener=None):
        """The page parsed, and its linked stylesheets fetched, in order.

        Runs on the loading thread. Each stylesheet is checked with the
        content blocker, fetched in parallel with the others, and its @imports
        followed; what could not be fetched is simply left out. Stylesheets are
        kept for the session, so a site's CSS is fetched once.
        """
        import concurrent.futures

        document = html_parser.parse(markup, page_url)
        sources = document.stylesheet_sources()
        links = [s for s in sources if s[0] == "link"]
        if not links:
            prepared = self._styled(document, [s[1] for s in sources], viewport, hiding,
                                    markup, page_url)
            prepared["font_faces"] = _gather_fonts([(s[1], page_url) for s in sources])
            return prepared
        cache = self._stylesheet_cache()
        page_site = _registrable(host_name)
        sheet_headers = dict(headers)
        sheet_headers["Accept"] = "text/css,*/*;q=0.1"
        _subresource(sheet_headers, "style")
        sheet_headers["Referer"] = page_url

        def get(url: str, depth: int = 0) -> str:
            if url in cache:
                return cache[url]
            interceptor = self._interceptor()
            if interceptor is not None and url.startswith(("http://", "https://")) \
                    and interceptor.check(url, host_name, "stylesheet"):
                return ""
            same = _registrable(QUrl(url).host()) == page_site
            ok, final, data, _kind = fetch_bytes(url, sheet_headers, timeout=12,
                                                 limit=4 * 1024 * 1024,
                                                 opener=opener if same else plain_opener)
            if not ok:
                problem = certificate_failure(data)
                if problem:
                    self._cert_trouble(url, problem)
                return ""
            text = data.decode("utf-8", "replace")
            if depth < 3 and "@import" in text:
                text = _expand_imports(text, final or url, lambda u: get(u, depth + 1))
            if len(cache) > 80:
                cache.pop(next(iter(cache)))
            cache[url] = text
            return text

        addresses = [urllib.parse.urljoin(page_url, s[1]) for s in links]
        done = [0]
        number = self._load_number

        def get_counted(address):
            text = get(address)
            done[0] += 1
            try:
                if number == self._load_number:
                    # 25% to 60% across the stylesheets, one step as each arrives
                    self.loadProgress.emit(25 + int(35 * done[0] / max(1, len(addresses))))
            except RuntimeError:
                pass
            return text

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            fetched = dict(zip(addresses, pool.map(get_counted, addresses)))
        sheets, with_bases = [], []
        for source in sources:
            if source[0] == "style":
                sheets.append(source[1])
                with_bases.append((source[1], page_url))
                continue
            address = urllib.parse.urljoin(page_url, source[1])
            text = fetched.get(address, "")
            media = source[2]
            sheets.append(f"@media {media} {{{text}}}" if media and media != "all" else text)
            with_bases.append((text, address))        # a font's url is the sheet's, not the page's
        prepared = self._styled(document, sheets, viewport, hiding, markup, page_url)
        prepared["font_faces"] = _gather_fonts(with_bases)
        return prepared

    @staticmethod
    def _styled(document, sheets, viewport, hiding, markup: str = "", url: str = "") -> dict:
        """Parse the CSS and run the cascade, here on the loading thread.

        Parsing megabytes of CSS and styling thousands of elements took over a
        second on the UI thread for a GitHub-sized page, freezing the window;
        neither needs Qt, so both happen before the page is handed over.
        """
        prepared = {"document": document, "sheets": sheets, "markup": markup}
        if viewport is None:
            return prepared
        from . import worker

        job = {"markup": markup, "url": url, "sheets": sheets, "viewport": viewport,
               "hiding": hiding, "want": "document"}
        prepared["styling"] = True
        # in a worker process, truly alongside the rest of Merlin; if none can
        # be had, here, as before
        answer = worker.POOL.style(job) if markup else None
        if answer is None:
            answer = worker.style_page(job) if markup else None
        if answer is None:
            styler = Styler(document, author_css=sheets, viewport=viewport, extra_css=hiding)
            answer = {"document": document, "styles": styler.compute(), "media": dict(styler._media),
                      "viewport_units": styler.viewport_units, "viewport": viewport}
        prepared.update(document=answer["document"], styles=answer["styles"],
                        media=answer["media"], viewport_units=answer.get("viewport_units", False),
                        styled_at=answer.get("viewport", viewport))
        return prepared

    def _on_fetched(self, number: int, ok: bool, final: str, text: str,
                    prepared=None) -> None:
        if number != self._load_number:
            return                                 # superseded or stopped
        self.loadProgress.emit(70)
        if final and QUrl(final) != self._url.adjusted(QUrl.UrlFormattingOption.RemoveFragment):
            final_url = QUrl(final)
            if self._pending_fragment:
                final_url.setFragment(self._pending_fragment)
            self._url = final_url
            self.urlChanged.emit(self.url())
        if not ok and prepared and prepared.get("certificate"):
            text = self._certificate_page(prepared["certificate"], prepared.get("skew"))
            prepared = None
        elif not ok:
            import html

            text = (f"<title>Could not open this page</title><body style='font-family:"
                    f"sans-serif;margin:40px'><h2>MerlinEngine could not open this page</h2>"
                    f"<p style='color:#555'>{html.escape(self._url.toString())}</p>"
                    f"<p>{html.escape(text)}</p></body>")
        self.loadProgress.emit(80)                   # styled: laying it out
        download = prepared.get("download") if prepared else None
        self._finish_when_laid_out = (ok, download)
        self._show(text, prepared if ok else None)
        if ok and not download:
            self._start_script()
        if not self._layout_running:
            self._finish_load()

    def _finish_load(self) -> None:
        """The page is laid out and shown: the load is finished."""
        if self._finish_when_laid_out is None:
            return
        ok, download = self._finish_when_laid_out
        self._finish_when_laid_out = None
        self.loadProgress.emit(100)
        self.loadFinished.emit(ok)
        if download:
            # announced once the page saying so has loaded, so nothing that
            # tidies up after a load clears the message
            self.downloadFinished.emit(download)
        if self._pending_fragment:
            self._scroll_to_fragment(self._pending_fragment)

    # ----------------------------------------------------- the pipeline
    def _show(self, markup: str, prepared=None) -> None:
        self._animation_epoch = time.monotonic()        # a new page's animations begin now
        if prepared is not None:
            self._document = prepared["document"]
            self._sheets = prepared["sheets"]
        else:
            self._document = html_parser.parse(markup, self._url.toString())
            self._sheets = None                 # the page's own <style> blocks
        self._clear_controls()
        self._images = {}
        self._icon = QIcon()
        self._element_count = sum(1 for _ in self._document.root.elements())
        if self._element_count > BIG_PAGE:
            self._display = None          # the old page goes; the new one is being laid out
        self._find = ("", -1)
        self._found_rect = None
        self._markup = prepared.get("markup", markup) if prepared else markup
        self._font_faces = prepared.get("font_faces", []) if prepared else []
        self._font_aliases = {}
        self._patches = {}
        if prepared is not None and prepared.get("styles") is not None:
            # styled off the UI thread, in a worker; a window resized meanwhile
            # is caught by _layout, which has the page styled again
            self._styles = prepared["styles"]
            self._media = prepared.get("media", {})
            self._viewport_units = prepared.get("viewport_units", False)
            self._styled_at = prepared.get("styled_at")
        else:
            styled_at = (self._page_width(), float(self.height()))
            styler = Styler(self._document, author_css=self._sheets,
                            viewport=styled_at, extra_css=self._hiding_css())
            self._styles = styler.compute()
            self._media = dict(styler._media)
            self._viewport_units = styler.viewport_units
            self._styled_at = styled_at
        self._styler = None
        title = self._document.title or self._url.toString()
        if title != self._title:
            self._title = title
            self.titleChanged.emit(title)
        self._scroll = 0.0
        self._layout()
        self._load_images()
        self._load_icon()
        self._load_fonts()
        if self._url.scheme() in ("http", "https") and needs_javascript(self._document):
            self.needsScript.emit(self._url.toString())

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
        _subresource(headers, "image")
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
        lenient = self._leniency(self._url.host())
        own_opener = cookie_opener(self._cookies(), lenient)
        plain_opener = cookie_opener(None, lenient)

        def work(src, address):
            # cookies go only to the page's own site: another site's images
            # never learn who is looking
            same = _registrable(QUrl(address).host()) == page_site
            ok, _final, data, _kind = fetch_bytes(address, headers,
                                                  opener=own_opener if same else plain_opener)
            if not ok:
                problem = certificate_failure(data)
                if problem:
                    self._cert_trouble(address, problem)
            # decoded here, off the UI thread; an SVG is left to the UI thread
            decoded = None
            if ok and data and b"<svg" not in data[:4096]:
                decoded = QImage.fromData(data)
            try:
                self._image_fetched.emit(number, src, decoded if decoded is not None
                                         else (data if ok else b""), ok)
            except RuntimeError:
                pass                     # the tab was closed while this loaded

        for src in wanted[:200]:                  # enough for any ordinary page
            address = self._url.resolved(QUrl(src)).toString()
            threading.Thread(target=work, args=(src, address), daemon=True).start()

    def _load_fonts(self) -> None:
        """Fetch the page's web fonts, once it shows, and lay it out with them.

        The first source of each @font-face in a format Qt reads is fetched;
        the content blocker is asked about each, as a font. Settings > Merlin
        Engine > "Use sites' own fonts" can turn them off: fonts can be used
        to follow people from site to site.
        """
        if not self._font_faces:
            return
        settings = getattr(self._host, "settings", None)
        if settings is not None and not settings.get("web_fonts", True):
            return
        readable = ("woff2", "woff", "truetype", "opentype", "ttf", "otf", "")
        chosen, seen = [], set()
        for face in self._font_faces:
            for url, kind in face["sources"]:
                ending = url.lower().split("?")[0].split("#")[0]
                if kind in readable or ending.endswith((".woff2", ".woff", ".ttf", ".otf")):
                    if url not in seen and not self._blocked(url, "font"):
                        seen.add(url)
                        chosen.append((face["family"], url))
                    break
        if not chosen:
            return
        number = self._load_number
        headers = dict(self._headers())
        headers["Accept"] = "font/woff2,font/woff,application/font-woff,*/*;q=0.5"
        _subresource(headers, "font")
        lenient = self._leniency(self._url.host())
        plain_opener = cookie_opener(None, lenient)

        def get(item):
            family, url = item
            ok, _final, data, _kind = fetch_bytes(url, headers, timeout=15,
                                                  limit=6 * 1024 * 1024, opener=plain_opener)
            if ok and data:
                try:
                    self._font_fetched.emit(number, family, data)
                except RuntimeError:
                    pass                               # the tab was closed

        def work():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(get, chosen[:40]))

        threading.Thread(target=work, daemon=True).start()

    def _on_font(self, number: int, family: str, data: bytes) -> None:
        if number != self._load_number:
            return
        names = register_font(data)
        if not names:
            return
        known = self._font_aliases.setdefault(family.lower(), [])
        for name in names:
            if name not in known:
                known.append(name)
        # laid out again with the font, at most once a quarter-second
        if not self._image_relayout.isActive():
            self._image_relayout.start()

    def _icon_address(self) -> str:
        """The page's own icon, near a tab's size, or else the site's /favicon.ico."""
        if self._document is None or self._url.scheme() not in ("http", "https"):
            return ""
        best, best_score = "", None
        for element in self._document.root.elements():
            if element.tag != "link" or not element.attrs.get("href"):
                continue
            rel = element.attrs.get("rel", "").lower().split()
            if not ("icon" in rel or "apple-touch-icon" in rel):
                continue
            sizes = element.attrs.get("sizes", "").lower()
            size = 0
            for part in sizes.split():
                if "x" in part and part.split("x")[0].isdigit():
                    size = int(part.split("x")[0])
            kind = element.attrs.get("type", "").lower()
            score = abs((size or 32) - 32) + (40 if "apple-touch-icon" in rel else 0) \
                - (5 if "svg" in kind or element.attrs["href"].lower().endswith(".svg") else 0)
            if best_score is None or score < best_score:
                best, best_score = element.attrs["href"].strip(), score
        if best:
            return self._url.resolved(QUrl(best)).toString()
        root = self._url.adjusted(QUrl.UrlFormattingOption.RemovePath
                                  | QUrl.UrlFormattingOption.RemoveQuery
                                  | QUrl.UrlFormattingOption.RemoveFragment)
        return root.toString() + "/favicon.ico"

    def _load_icon(self) -> None:
        """Fetch the tab's icon, once the page shows, so it never holds the page up."""
        address = self._icon_address()
        if not address or self._blocked(address, "image"):
            return
        number = self._load_number
        headers = dict(self._headers())
        headers["Accept"] = "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8,*/*;q=0.5"
        _subresource(headers, "image")
        same = _registrable(QUrl(address).host()) == _registrable(self._url.host())
        lenient = self._leniency(self._url.host())
        opener = cookie_opener(self._cookies() if same else None, lenient)

        def work():
            ok, _final, data, _kind = fetch_bytes(address, headers, timeout=10,
                                                  limit=512 * 1024, opener=opener)
            if ok and data:
                try:
                    self._icon_fetched.emit(number, data)
                except RuntimeError:
                    pass                             # the tab was closed

        threading.Thread(target=work, daemon=True).start()

    def _on_icon(self, number: int, data: bytes) -> None:
        from PyQt6.QtGui import QPixmap

        if number != self._load_number:
            return
        picture = QImage.fromData(data)
        if picture.isNull() and b"<svg" in data[:4096]:
            svg = _svg_picture(data)
            if svg is not None and not svg.isNull():
                picture = QImage(32, 32, QImage.Format.Format_ARGB32)
                picture.fill(0)
                painter = QPainter(picture)
                svg.render(painter, QRectF(0, 0, 32, 32))
                painter.end()
        if picture.isNull():
            return
        self._icon = QIcon(QPixmap.fromImage(picture))
        self.iconChanged.emit(self._icon)

    def _on_image(self, number: int, src: str, data: bytes, ok: bool) -> None:
        if number != self._load_number:
            return
        if isinstance(data, QImage):
            picture = data                       # decoded on the loading thread
        else:
            picture = QImage()
            if ok and data and b"<svg" in data[:4096]:
                picture = _svg_picture(data)     # kept as vectors: sharp at any size
            if (picture is None or picture.isNull()) and ok and data:
                picture = QImage.fromData(data)
        self._images[src] = picture if picture is not None and not picture.isNull() else False
        if self._image_moves_layout(src):
            # at most one layout for images in a quarter-second, however many
            # arrive: each had laid the whole page out again
            if not self._image_relayout.isActive():
                self._image_relayout.start()
        else:
            self.update()                        # its box was already there: just draw it

    def _image_moves_layout(self, src: str) -> bool:
        """Whether an image arriving changes the layout: only if no size is given.

        An image with a width and height takes its box whether it has loaded
        or not, so its arrival needs only drawing.
        """
        if self._document is None:
            return False
        for element in self._document.root.elements():
            if element.tag != "img" or element.attrs.get("src", "").strip() != src:
                continue
            style = self._styles.get(element, {})
            given = lambda name: (style.get(name) not in (None, "auto")          # noqa: E731
                                  and not isinstance(style.get(name), tuple)) \
                or element.attrs.get(name, "").strip().rstrip("px").isdigit()
            if not (given("width") and given("height")):
                return True
        return False

    def _restyle(self) -> None:
        """Style the page again, off the UI thread, and lay it out when done."""
        if self._document is None or not self._markup:
            return
        if self._restyling:
            self._restyle_again = True           # once this one is back
            return
        self._restyling = True
        number = self._load_number
        job = {"markup": self._markup, "url": self._url.toString(), "sheets": self._sheets,
               "viewport": (self._page_width(), float(max(1, self.height()))),
               "hiding": self._hiding_css(), "patches": dict(self._patches),
               "want": "styles"}

        def work():
            from . import worker

            answer = worker.POOL.style(job)
            if answer is None:
                try:
                    answer = worker.style_page(job)
                except Exception:                          # noqa: BLE001
                    answer = None
            try:
                self._restyled.emit(number, answer)
            except RuntimeError:
                pass                             # the tab was closed

        threading.Thread(target=work, daemon=True).start()

    def _on_restyled(self, number: int, answer) -> None:
        self._restyling = False
        if number == self._load_number and answer is not None and self._document is not None:
            elements = [self._document.root] + list(self._document.root.elements())
            styles = answer.get("styles") or []
            if len(styles) == len(elements):
                self._styles = {e: st for e, st in zip(elements, styles) if st is not None}
                self._media = answer.get("media", self._media)
                self._viewport_units = answer.get("viewport_units", self._viewport_units)
                self._layout()
        if self._restyle_again:
            self._restyle_again = False
            self._restyle()

    # ------------------------------------------------------ JavaScript
    def _start_script(self) -> None:
        """Run the page's scripts, if its site may, in a Deno process of its own."""
        from .script import LocalStorage, ScriptHost, cookie_string, import_hosts, import_map

        if self._url.scheme() not in ("http", "https") or "<script" not in (self._markup or "").lower():
            self.script_status = "no scripts on this page"
            return
        allowed = getattr(self._host, "javascript_allowed", None)
        site = _registrable(self._url.host())
        if allowed is None or not allowed(site):
            self.script_status = "JavaScript is off in Settings"
            return
        deno = getattr(self._host, "deno_for_scripts", lambda: "")()
        if not deno:
            self.script_status = "Deno, which runs JavaScript, is not here yet (being fetched)"
            return
        cache = getattr(self._host, "script_cache_dir", lambda: "")() or os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation), "merlin-js")
        try:
            host = ScriptHost(deno, import_hosts(self._markup, self._url.toString()), cache, self,
                              mapping=import_map(self._markup, self._url.toString()))
        except Exception as exc:                            # noqa: BLE001
            self.script_status = f"JavaScript could not start: {exc}"
            self.console_lines.append(("error", self.script_status))
            _note(f"javascript: {self._url.host()}: {self.script_status}")
            return
        self.script_status = f"started with {deno}"
        self._script = host
        self._script_bodies = {}
        self._script_cache = cache
        self._script_hosts = import_hosts(self._markup, self._url.toString())
        host.message.connect(self._on_script_message)
        host.ended.connect(lambda h=host: self._script_ended(h))
        storage = getattr(self._host, "local_storage", None)
        storage = storage() if storage is not None else LocalStorage(None)
        self._storage = storage
        headers = self._headers()
        # The page as Merlin Engine's parser built it (html, head and body
        # there), each element numbered here: the scripts' DOM takes the same
        # numbers, so the layout already done can be given to them at once, and
        # their first measurements are real rather than 0.
        from .dom import to_html

        for number, element in enumerate([self._document.root] + list(self._document.root.elements()), 1):
            element.attrs["data-mjs"] = str(number)
        whole = to_html(self._document)
        host.send({"type": "load", "html": whole, "geometry": self._geometry_now(), "state": {
            "url": self._url.toString(), "width": self._page_width(), "height": float(self.height()),
            "userAgent": headers.get("User-Agent", ""), "language": "en-GB",
            "cookie": cookie_string(self._cookies(), self._url.toString()),
            "storage": storage.load(self._origin()),
            "scheme": "dark" if getattr(self._host, "dark", False) else "light"}})

    def _origin(self) -> str:
        return f"{self._url.scheme()}://{self._url.authority()}"

    def shutdown(self) -> None:
        """The tab is closing: its page's scripts end with it."""
        self._stop_script()

    def _stop_script(self) -> None:
        if self._script is not None:
            host, self._script = self._script, None
            host.stop()
            host.deleteLater()
        for callback in list(self._script_waiting.values()):
            try:
                callback[0](False)
            except Exception:                              # noqa: BLE001
                pass
        self._script_waiting.clear()
        self._script_queued = None

    def _on_fell_back(self, number: int, host: str) -> None:
        HTTP_ONLY_HOSTS.add(host.lower())
        if number != self._load_number:
            return
        label = getattr(self._host, "status_label", None)
        if label is not None:
            label.setText(f"Not secure: {host} does not answer over https, so it was opened over http")

    def _on_retrying(self, number: int, attempt: int, attempts: int) -> None:
        if number != self._load_number:
            return
        label = getattr(self._host, "status_label", None)
        if label is not None:
            label.setText(f"Retrying connection... ({attempt} of {attempts})")

    def _script_ended(self, host) -> None:
        if host is self._script:
            self._script = None
            self.script_status = f"the scripts' process ended ({host.why_it_ended()})"
            self.console_lines.append(("error", self.script_status))
            _note(f"javascript: {self._url.host()}: {self.script_status}")

    def _script_event(self, message: dict, then) -> None:
        """Send an event to the page's scripts; then(prevented) when they answer.

        A page that does not answer in 1.5 seconds is not waited for: the
        default goes ahead, as if nothing had stopped it.
        """
        self._script_next += 1
        number = self._script_next
        message["id"] = number
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._script_answered(number, False))
        self._script_waiting[number] = (then, timer)
        timer.start(1500)
        self._script.send(message)

    def _script_answered(self, number: int, prevented: bool) -> None:
        waiting = self._script_waiting.pop(number, None)
        if waiting is None:
            return
        then, timer = waiting
        timer.stop()
        timer.deleteLater()
        then(prevented)

    def _script_input(self, element, text, checked=None) -> None:
        element = self._current(element)
        if self._script is None or not element.attrs.get("data-mjs"):
            return
        message = {"type": "input", "target": int(element.attrs["data-mjs"]), "id": 0}
        if text is not None:
            message["value"] = text
        if checked is not None:
            message["checked"] = bool(checked)
        self._script.send(message)

    def _send_geometry(self) -> None:
        """Where each element was laid out, for the page's scripts: their
        getBoundingClientRect, sizes and IntersectionObserver are worked out
        from it, and from the scroll, sent as it changes."""
        if self._script is None or self._display is None:
            return
        self._script.send({"type": "geometry", "boxes": self._geometry_now(),
                           "height": float(self.height())})

    def _geometry_now(self) -> dict:
        boxes = {}
        if self._display is None:
            return boxes
        sources = [(self._display.boxes, False)]
        if self._display.fixed is not None:
            sources.append((self._display.fixed.boxes, True))
        for listed, fixed in sources:
            for rect, element in listed:
                known = element.attrs.get("data-mjs")
                if known and known not in boxes:
                    boxes[known] = [round(rect.x(), 1), round(rect.y(), 1), round(rect.width(), 1),
                                    round(rect.height(), 1), fixed]
        return boxes

    def _send_scroll(self) -> None:
        if self._script is not None:
            self._script.send({"type": "scroll", "x": 0, "y": float(self._scroll)})

    def _current(self, element):
        """The element as it is now: scripts' updates replace the page's elements."""
        if element is None or not getattr(element, "attrs", None):
            return element
        known = element.attrs.get("data-mjs")
        return self._by_script_id.get(known, element) if known else element

    def _element_at(self, position):
        """The script id of the element a click at position lands on, or None."""
        if self._display is None:
            return None
        point = position.toPointF() if hasattr(position, "toPointF") else position
        candidates = []
        if self._display.fixed is not None:
            candidates += [(r, e) for r, e in self._display.fixed.boxes if r.contains(point)]
        if not candidates:
            page_point = point.__class__(point.x(), point.y() + self._scroll)
            candidates = [(r, e) for r, e in self._display.boxes if r.contains(page_point)]
        for rect, element in sorted(candidates, key=lambda c: c[0].width() * c[0].height()):
            if element.attrs.get("data-mjs"):
                return int(element.attrs["data-mjs"])
        return None

    def _on_script_message(self, message: dict) -> None:
        kind = message.get("type")
        host = self._script
        if host is None:
            return
        if kind == "dom":
            self._script_dom(message.get("html", ""), message.get("title", ""))
        elif kind == "fetch":
            self._script_fetch(host, message)
        elif kind == "handled":
            self._script_answered(message.get("id"), bool(message.get("prevented")))
        elif kind == "url":
            self._url = QUrl(message.get("url", ""))
            self.urlChanged.emit(self.url())
        elif kind == "navigate":
            QTimer.singleShot(0, lambda u=message.get("url", ""): self.setUrl(QUrl(u)))
        elif kind == "reload":
            QTimer.singleShot(0, self.reload)
        elif kind == "history":
            step = message.get("step", 0)
            QTimer.singleShot(0, self.back if step < 0 else self.forward if step > 0 else self.reload)
        elif kind == "open":
            opener = getattr(self._host, "new_tab", None)
            if opener is not None:
                QTimer.singleShot(0, lambda u=message.get("url", ""): opener(u, engine=True))
        elif kind == "cookie":
            from .script import set_cookie

            set_cookie(self._cookies(), self._url.toString(), message.get("value", ""))
        elif kind == "storage":
            storage = getattr(self, "_storage", None)
            if storage is not None:
                storage.save(self._origin(), message.get("items", {}))
        elif kind == "scroll":
            self.scrollbar.setValue(int(message.get("y", 0) or 0))
        elif kind == "alert":
            label = getattr(self._host, "status_label", None)
            if label is not None:
                label.setText("This page says: " + str(message.get("text", ""))[:200])
        elif kind == "console":
            self.console_lines.append((message.get("level", "log"), message.get("text", "")))
        elif kind == "ready":
            self.script_status = "running"

    def _script_fetch(self, host, message: dict) -> None:
        """A request from the page's script: made here, as the page's."""
        from .script import page_fetch

        url = message.get("url", "")
        same = _registrable(QUrl(url).host()) == _registrable(self._url.host())
        blocked = self._blocked(url, "xmlhttprequest")
        lenient = self._leniency(self._url.host())
        opener = cookie_opener(self._cookies() if same else None, lenient)
        base = {k: v for k, v in self._headers().items()
                if k in ("User-Agent", "Accept-Language", "DNT", "Sec-GPC")}
        headers = dict(base, **(message.get("headers") or {}))
        headers.setdefault("Referer", self._url.toString())

        def work():
            answer = ({"error": "blocked by the content blocker"} if blocked else
                      page_fetch(url, message.get("method", "GET"), headers, message.get("body"), opener))
            # a script, kept for the debugging zip (scripts only, not data)
            kind = str((answer.get("headers") or {}).get("content-type", "")).lower()
            if "javascript" in kind or "ecmascript" in kind or url.split("?")[0].endswith((".js", ".mjs")):
                try:
                    import base64 as _b64

                    self._script_bodies[url] = _b64.b64decode(answer.get("body") or "")
                except Exception:                          # noqa: BLE001
                    pass
            answer.update(type="answer", id=message.get("id"))
            host.send(answer)

        threading.Thread(target=work, daemon=True).start()

    def _script_dom(self, markup: str, title: str) -> None:
        """The page as scripts left it: styled off the UI thread, then shown.

        Not more often than the page can bear: after each update the next
        waits at least 0.3 seconds, or twice what the last one took to style
        and lay out. A page whose scripts change it all the time (Hugging
        Face's) had kept the whole browser busy restyling it. Only the latest
        change waiting is shown.
        """
        now = time.monotonic()
        if self._script_busy or now < self._script_rest_until:
            self._script_queued = (markup, title)          # only the latest counts
            if not self._script_busy and not self._script_rest.isActive():
                self._script_rest.start(max(1, int((self._script_rest_until - now) * 1000)))
            return
        self._script_busy = True
        self._script_started = now
        number = self._load_number
        page_url = self._url.toString()
        headers = self._headers()
        host_name = self._url.host()
        lenient = self._leniency(host_name)
        opener = cookie_opener(self._cookies(), lenient)
        plain = cookie_opener(None, lenient)
        viewport = (self._page_width(), float(max(1, self.height())))
        hiding = self._hiding_css_for(host_name)

        def work():
            try:
                prepared = self._prepare(markup, page_url, headers, opener, host_name, viewport,
                                         hiding, plain)
                prepared["title"] = title
            except Exception:                              # noqa: BLE001
                prepared = None
            try:
                self._script_prepared.emit(number, markup, prepared)
            except RuntimeError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _script_rested(self) -> None:
        if self._script_queued is not None and self._script is not None:
            markup, title = self._script_queued
            self._script_queued = None
            self._script_dom(markup, title)

    def _apply_script_dom(self, number: int, markup: str, prepared) -> None:
        """Show the page as scripts changed it, keeping the scroll, the fields and
        what is typed in them, and the pictures already fetched."""
        self._script_busy = False
        applied = number == self._load_number and prepared is not None and self._script is not None
        if number == self._load_number and prepared is not None and self._script is not None:
            old = {e.attrs.get("data-mjs"): w for e, w in self._widgets.items() if e.attrs.get("data-mjs")}
            self._document = prepared["document"]
            self._sheets = prepared.get("sheets", self._sheets)
            self._styles = prepared["styles"]
            self._media = prepared.get("media", {})
            self._viewport_units = prepared.get("viewport_units", False)
            self._styled_at = prepared.get("styled_at")
            self._markup = markup
            self._by_script_id = {}
            kept = {}
            for element in self._document.root.elements():
                known = element.attrs.get("data-mjs")
                if not known:
                    continue
                self._by_script_id[known] = element
                # a field's value as the script left it
                if "data-mjs-value" in element.attrs and element.tag in ("input", "textarea", "select"):
                    element.attrs["value"] = element.attrs["data-mjs-value"]
                if "data-mjs-checked" in element.attrs:
                    element.attrs["checked"] = ""
                if known in old:
                    kept[element] = old.pop(known)
            for widget in old.values():
                widget.hide()
                widget.deleteLater()
            self._widgets = kept
            self._element_count = sum(1 for _ in self._document.root.elements())
            title = prepared.get("title") or ""
            if title and title != self._title:
                self._title = title
                self.titleChanged.emit(title)
            self._layout()
            self._load_images()
        if applied:
            cost = time.monotonic() - getattr(self, "_script_started", time.monotonic())
            self._script_rest_until = time.monotonic() + max(0.3, 2 * cost)
        if self._script_queued is not None:
            markup, title = self._script_queued
            self._script_queued = None
            self._script_dom(markup, title)

    def _page_width(self) -> float:
        return max(100.0, float(self.width() - self.scrollbar.sizeHint().width()))

    def _layout(self) -> None:
        if self._document is None:
            return
        # across a breakpoint, the page is styled again, in the background;
        # until then it keeps its present styles
        viewport = (self._page_width(), float(self.height()))
        if self._media and any(media_matches(q, viewport) != held
                               for q, held in self._media.items()):
            self._media = {q: media_matches(q, viewport) for q in self._media}
            self._restyle()
        elif self._viewport_units and self._styled_at is not None and (
                abs(self._styled_at[0] - viewport[0]) > 1 or abs(self._styled_at[1] - viewport[1]) > 1):
            # vw and vh were worked out for another size: the new tab page,
            # styled while its view was still tiny, kept its search box 86px wide
            self._styled_at = viewport
            self._restyle()
        if self._element_count > BIG_PAGE:
            self._layout_in_background()
            return
        self._display = Layout(self._document, self._styles, self._page_width(), self._zoom,
                               images=self._images,
                               viewport_height=float(max(1, self.height())),
                               live_controls=True, font_aliases=self._font_aliases).run()
        self._shown_laid_out()

    def _layout_in_background(self) -> None:
        """Lay a big page out on a thread, so the window keeps drawing meanwhile.

        Qt measures and lays out text off the main thread happily; only drawing
        the window needs it. Python still takes turns, but the main thread gets
        its turns, so the loading bar and everything else keep moving.
        """
        if self._layout_running:
            self._layout_again = True               # once this one is done
            return
        self._layout_running = True
        self._layout_number += 1
        number = self._layout_number
        document, styles = self._document, self._styles
        width, zoom = self._page_width(), self._zoom
        images = dict(self._images)
        aliases = {k: list(v) for k, v in self._font_aliases.items()}
        height = float(max(1, self.height()))

        def work():
            try:
                out = Layout(document, styles, width, zoom, images=images,
                             viewport_height=height, live_controls=True,
                             font_aliases=aliases).run()
            except Exception:                              # noqa: BLE001
                out = None
            try:
                self._laid_out.emit(number, out)
            except RuntimeError:
                pass                                 # the tab was closed

        threading.Thread(target=work, daemon=True).start()

    def _on_laid_out(self, number: int, out) -> None:
        self._layout_running = False
        if number == self._layout_number and out is not None:
            self._display = out
            self._shown_laid_out()
        if self._layout_again:
            self._layout_again = False
            self._layout_in_background()
        else:
            self._finish_load()

    def _shown_laid_out(self) -> None:
        self._update_scrollbar()
        self._sync_controls()
        self.update()
        self._send_geometry()
        display = self._display
        moving = display is not None and (display.animated or (
            display.fixed is not None and getattr(display.fixed, "animated", False)))
        if moving and not self._animation_timer.isActive():
            self._animation_timer.start()
        elif not moving:
            self._animation_timer.stop()

    def _animation_tick(self) -> None:
        """Draw again only where something animated is in view, with room for
        how far it moves; nothing at all when none is."""
        if not self.isVisible() or self.window() is None or self.window().isMinimized():
            return
        display = self._display
        if display is None:
            return
        zoom = getattr(self, "_zoom", 1.0) or 1.0
        view_height = self.height() / zoom
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QRegion

        region = QRegion()
        pieces = [(rect, self._scroll) for rect in display.animated_boxes]
        if display.fixed is not None:
            pieces += [(rect, 0.0) for rect in getattr(display.fixed, "animated_boxes", [])]
        for rect, scroll in pieces:
            top = rect.top() - scroll
            # room around it: a transform or marquee may carry it a box's size away
            reach_x, reach_y = rect.width(), rect.height()
            if top + rect.height() + reach_y < 0 or top - reach_y > view_height:
                continue
            region += QRect(int((rect.left() - reach_x) * zoom), int((top - reach_y) * zoom),
                            int((rect.width() + 2 * reach_x) * zoom) + 2,
                            int((rect.height() + 2 * reach_y) * zoom) + 2)
        if not region.isEmpty():
            self.update(region)
        if self._display.simplified:
            label = getattr(self._host, "status_label", None)
            if label is not None:
                label.setText("This page is too complex for Merlin Engine to lay out fully; "
                              "parts of it are approximate")

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
        if self._script is not None and not self._script_scroll.isActive():
            self._script_scroll.start()
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
        self._control_sticky = {}
        for source, fixed in ((self._display, False), (self._display.fixed, True)):
            if source is None:
                continue
            around = []
            pinned = 0
            for item in source.items:
                # a fixed box's fields are taken from the fixed list, pinned to
                # the window; its copy in the page's list is passed over here
                if item and item[0] == "fixed_push":
                    pinned += 1
                    continue
                if item and item[0] == "fixed_pop":
                    pinned -= 1
                    continue
                if pinned and not fixed:
                    continue
                if item and item[0] == "sticky_push":
                    around.append(item[1])
                elif item and item[0] == "sticky_pop" and around:
                    around.pop()
                elif item and item[0] == "control":
                    found[item[2]] = (item[1], fixed, item[3], item[4])
                    if around:
                        self._control_sticky[item[2]] = around[-1]
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
            sticky = getattr(self, "_control_sticky", {}).get(element)
            if sticky is not None and self._display is not None:
                top += self._display.sticky_offset(sticky, self._scroll)
            height = max(rect.height(), 8.0)
            widget.setGeometry(round(rect.left()), round(top), max(8, round(rect.width())),
                               round(height))
            shown = top + height > 0 and top < bottom and rect.left() < right
            if shown and fixed:
                # a field is a widget over the page; one in a fixed box at
                # z-index 0 or below (GitHub's email box) hides while content
                # that scrolls over the box covers it, as the box itself is
                level = getattr(self._display.fixed, "control_z", {}).get(element, 1)
                if level <= 0 and self._covered(QRectF(rect.left(), top, rect.width(), height),
                                                level):
                    shown = False
            widget.setVisible(shown)

    def _covers(self) -> list:
        """Where the page draws over the stacking level 0: (page rect, z-index).

        The backgrounds and pictures of positioned boxes in the page itself,
        not in fixed boxes; worked out once for each layout.
        """
        display = self._display
        if display is None:
            return []
        if getattr(display, "_covers", None) is not None:
            return display._covers
        found, layers, pinned = [], [], 0
        for item in display.items:
            if item is None:
                continue
            kind = item[0]
            if kind == "fixed_push":
                pinned += 1
            elif kind == "fixed_pop":
                pinned -= 1
            elif kind == "layer_push":
                layers.append(item[1])
            elif kind == "layer_pop" and layers:
                layers.pop()
            elif layers and not pinned:
                # a box's background and border come as a group
                for sub in (item[1] if kind == "group" else [item]):
                    if sub and sub[0] in ("rect", "rrect", "gradient", "image"):
                        found.append((sub[1], layers[-1]))
        display._covers = found
        return found

    def _covered(self, window_rect: QRectF, level: int) -> bool:
        centre = window_rect.center()
        for rect, z in self._covers():
            if z >= level and rect.translated(0, -self._scroll).contains(centre):
                return True
        return False

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
            widget.toggled.connect(lambda on, e=element: self._script_input(e, None, on))
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
        elif kind == "file":
            from PyQt6.QtWidgets import QPushButton

            widget = QPushButton(self)
            widget.setProperty("merlin_files", [])
            widget.setProperty("merlin_file_button", True)
            self._show_files(widget, element)
            widget.clicked.connect(lambda _checked=False, e=element, w=widget: self._choose_files(e, w))
        else:
            widget = QLineEdit(self)
            widget.setFrame(False)
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
            widget.textEdited.connect(lambda text, e=element: self._script_input(e, text))
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

    @staticmethod
    def _show_files(widget, element) -> None:
        files = widget.property("merlin_files") or []
        many = "multiple" in element.attrs
        if not files:
            widget.setText("Choose files…" if many else "Choose a file…")
        elif len(files) == 1:
            import os

            widget.setText(os.path.basename(files[0]))
        else:
            widget.setText(f"{len(files)} files")
        widget.setToolTip("\n".join(files))

    def _choose_files(self, element, widget) -> None:
        from PyQt6.QtWidgets import QFileDialog

        from . import forms

        which = forms.dialog_filter(element.attrs.get("accept", ""))
        if "multiple" in element.attrs:
            files, _filter = QFileDialog.getOpenFileNames(self, "Choose files", "", which)
        else:
            path, _filter = QFileDialog.getOpenFileName(self, "Choose a file", "", which)
            files = [path] if path else []
        if files:
            widget.setProperty("merlin_files", files)
            self._show_files(widget, element)

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
            elif widget.property("merlin_file_button"):
                values[element] = list(widget.property("merlin_files") or [])
            else:
                values[element] = widget.text()
        return values

    def _submit(self, element, submitter=None, scripts_asked: bool = False) -> None:
        """Send the form element belongs to, as its button or Enter asks."""
        from . import forms

        element = self._current(element)
        submitter = self._current(submitter) if submitter is not None else None
        form = element if element.tag == "form" else forms.form_of(element)
        if form is None:
            return
        if self._script is not None and not scripts_asked and form.attrs.get("data-mjs"):
            # the page's submit handlers first: most frameworks send forms themselves
            self._script_event({"type": "submit", "target": int(form.attrs["data-mjs"])},
                               lambda prevented: None if prevented else
                               self._submit(element, submitter, scripts_asked=True))
            return
        if submitter is None:
            # Enter in a field: the form's first submit button sends it
            submitter = next((c for c in forms.controls_of(form)
                              if forms.kind_of(c) in ("submit", "image")), None)
        values = self._values()
        pairs = forms.form_data(form, values, submitter)
        login = self._login_in(form, values)
        if login is not None:
            # the site's origin only: a login page's address can carry
            # one-time tokens, which have no place among saved passwords
            origin = self._url.adjusted(QUrl.UrlFormattingOption.RemovePath
                                        | QUrl.UrlFormattingOption.RemoveQuery
                                        | QUrl.UrlFormattingOption.RemoveFragment
                                        | QUrl.UrlFormattingOption.RemoveUserInfo)
            self.loginSubmitted.emit(origin.toString() or self._url.toString(),
                                     login[0], login[1])
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

    def _login_in(self, form, values: dict):
        """(username, password) when the form holds a filled-in password field."""
        from . import forms

        controls = forms.controls_of(form)
        for index, control in enumerate(controls):
            if forms.kind_of(control) == "password" and values.get(control):
                user = ""
                # the username is the text or email field nearest before it
                for earlier in reversed(controls[:index]):
                    if forms.kind_of(earlier) in ("text", "email", "tel") and values.get(earlier):
                        user = str(values[earlier])
                        break
                return user, str(values[control])
        return None

    def fill_login(self, username: str, password: str) -> str:
        """Fill a saved login into the page: "filled", or why it could not be."""
        from PyQt6.QtWidgets import QLineEdit

        from . import forms

        fields = [(e, w) for e, w in self._widgets.items() if isinstance(w, QLineEdit)]
        secret = next(((e, w) for e, w in fields if forms.kind_of(e) == "password"
                       and w.isEnabled() and not w.isReadOnly()), None)
        if secret is None:
            return "No password field on this page"
        element, widget = secret
        widget.setText(password)
        form = forms.form_of(element)
        if username and form is not None:
            controls = forms.controls_of(form)
            position = controls.index(element)
            for earlier in reversed(controls[:position]):
                if forms.kind_of(earlier) in ("text", "email", "tel") \
                        and earlier in self._widgets:
                    self._widgets[earlier].setText(username)
                    break
        widget.setFocus()
        return "filled"

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
            elif widget.property("merlin_file_button"):
                widget.setProperty("merlin_files", [])
                self._show_files(widget, control)
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

    # ------------------------------------------------------ for debugging
    def save_for_debugging(self, folder: str, version: str = "") -> str:
        """The page as Merlin Engine has it, zipped, to send for a look.

        The HTML as it came, every stylesheet in the order the cascade reads
        them, a screenshot of how it was drawn, and what it was drawn at.
        Returns the zip's path. Nothing is sent anywhere by this.
        """
        import json
        import time
        import zipfile

        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice

        from .remote import unique_path

        os.makedirs(folder, exist_ok=True)
        host = (self._url.host() or "page").replace(":", "_")
        path = unique_path(folder, f"merlin-engine-{host}-{time.strftime('%Y%m%d-%H%M%S')}.zip")
        shot = QByteArray()
        buffer = QBuffer(shot)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.grab().toImage().save(buffer, "PNG")
        sheets = self._sheets if self._sheets is not None else (
            self._document.stylesheets() if self._document is not None else [])
        manifest = {"url": self._url.toString(), "version": version,
                    "viewport": [self._page_width(), self.height()], "zoom": self._zoom,
                    "scroll": self._scroll, "sheets": len(sheets),
                    "simplified": bool(self._display and self._display.simplified),
                    "scripts": getattr(self, "script_status", ""),
                    "scripts_changed_page": "data-mjs=" in (self._markup or "")}
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("page.html", self._markup or "")
            for number, text in enumerate(sheets):
                bundle.writestr(f"sheets/{number:03d}.css", text or "")
            bundle.writestr("screenshot.png", bytes(shot))
            bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
            bundle.writestr("console.txt", "\n".join(f"[{level}] {text}" for level, text
                                                     in getattr(self, "console_lines", [])))
            self._bundle_scripts(bundle)
            arrived = getattr(self, "_arrived_markup", "")
            if arrived:
                bundle.writestr("original.html", arrived)
        return path

    def _bundle_scripts(self, bundle) -> None:
        """The scripts the page ran, so it can be run again elsewhere, offline,
        as it ran here: plain scripts as Merlin fetched them for it, and the
        page's modules from Deno's cache. Scripts only, not what the page
        fetched as data."""
        import json

        index = []
        for number, (url, body) in enumerate(list(getattr(self, "_script_bodies", {}).items())):
            name = f"scripts/{number:04d}.js"
            bundle.writestr(name, body)
            index.append({"url": url, "file": name})
        bundle.writestr("scripts/index.json", json.dumps(index, indent=1))
        cache = getattr(self, "_script_cache", "")
        hosts = getattr(self, "_script_hosts", [])
        remote = os.path.join(cache, "remote") if cache else ""
        if not remote or not os.path.isdir(remote):
            return
        for host in hosts:
            for scheme in ("https", "http"):
                folder = os.path.join(remote, scheme, host.replace(":", "_port_"))   # as Deno names them
                if not os.path.isdir(folder):
                    continue
                for base, _dirs, files in os.walk(folder):
                    for file in files:
                        full = os.path.join(base, file)
                        try:
                            if os.path.getsize(full) <= 20 * 1024 * 1024:
                                bundle.write(full, "deno-cache/" + os.path.relpath(full, cache).replace(os.sep, "/"))
                        except OSError:
                            pass

    # ------------------------------------------------------ saving the page
    def html_with_stylesheets(self) -> str:
        """The page with each linked stylesheet written into it, one file."""
        markup = self._markup or ""
        if self._document is None or not self._sheets:
            return markup
        sources = self._document.stylesheet_sources()
        texts = iter(zip(sources, self._sheets))
        linked = {}
        for (kind, *rest), text in texts:
            if kind == "link":
                linked[rest[0]] = (text, rest[1])

        def inline(match):
            href = re.search(r"""href\s*=\s*["']?([^"'\s>]+)""", match.group(0), re.I)
            found = linked.get(href.group(1).strip()) if href else None
            if not found:
                return match.group(0)
            text, media = found
            media_attr = f' media="{media}"' if media and media != "all" else ""
            return f"<style{media_attr}>\n{text}\n</style>"

        return re.sub(r"<link\b[^>]*rel\s*=\s*[\"']?[^>]*stylesheet[^>]*>", inline, markup, flags=re.I)

    def page_text(self) -> str:
        """The page's words in reading order, a line for each line drawn."""
        if self._display is None:
            return ""
        lines, current, last = [], [], None
        for item in self._display.items:
            subs = item[1] if item and item[0] == "group" else [item]
            for sub in subs:
                if not sub or sub[0] != "text":
                    continue
                baseline = round(sub[2])
                if last is not None and abs(baseline - last) > 2:
                    lines.append(("".join(current).strip(), last))
                    current = []
                current.append(sub[3])
                last = baseline
        if current:
            lines.append(("".join(current).strip(), last))
        out, previous = [], None
        for text, baseline in lines:
            if previous is not None and baseline - previous > 40:
                out.append("")                          # a gap between blocks
            out.append(text)
            previous = baseline
        return "\n".join(out).strip() + "\n"

    def _paint_page(self, painter, top: float, height: float) -> None:
        """The page from top, for height, as it would be drawn scrolled there."""
        from .paint import fill_gradient

        width = self._page_width()
        area = QRectF(0, top, width, height)
        canvas = self._display.canvas or (255, 255, 255, 255)
        painter.fillRect(area, QColor(*canvas))
        for gradient in reversed(self._display.canvas_gradients or []):
            fill_gradient(painter, QRectF(0, 0, width, max(height, self._display.height)), gradient)
        paint(painter, self._display, area,
              {k: v for k, v in self._images.items() if v is not False})

    def save_picture(self, path: str, limit: int = 32000) -> None:
        """The whole page as one picture, not just what the window shows."""
        if self._display is None:
            self.grab().save(path, "PNG")
            return
        width = int(self._page_width())
        height = int(min(max(self._display.height, 1), limit))
        picture = QImage(width, height, QImage.Format.Format_ARGB32)
        picture.fill(QColor("white"))
        painter = QPainter(picture)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self._paint_page(painter, 0.0, float(height))
        painter.end()
        picture.save(path, "PNG")

    def save_pdf(self, path: str) -> None:
        """The page on A4 pages, fitted to the width, one after another."""
        from PyQt6.QtCore import QMarginsF
        from PyQt6.QtGui import QPageLayout, QPageSize, QPdfWriter

        if self._display is None:
            return
        writer = QPdfWriter(path)
        writer.setResolution(96)
        writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4),
                                         QPageLayout.Orientation.Portrait,
                                         QMarginsF(10, 10, 10, 10), QPageLayout.Unit.Millimeter))
        writer.setTitle(self._title or self._url.toString())
        painter = QPainter(writer)
        area = writer.pageLayout().paintRectPixels(96)
        width = self._page_width()
        scale = area.width() / width if width else 1.0
        slice_height = area.height() / scale
        top = 0.0
        total = max(self._display.height, 1.0)
        first = True
        while top < total:
            if not first:
                writer.newPage()
            first = False
            painter.save()
            painter.scale(scale, scale)
            painter.translate(0, -top)
            painter.setClipRect(QRectF(0, top, width, slice_height))
            self._paint_page(painter, top, slice_height)
            painter.restore()
            top += slice_height
        painter.end()

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
        if self._script is not None:
            self._script.send({"type": "resize", "width": self._page_width(),
                               "height": float(self.height())})
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
            if self._display.canvas_gradients:
                # a gradient on <html> or <body> spans the whole page
                from .paint import fill_gradient

                page = QRectF(0, 0, self.width(), max(float(self.height()), self._display.height))
                for gradient in reversed(self._display.canvas_gradients):
                    fill_gradient(painter, page, gradient)
            if self._found_rect is not None:
                painter.fillRect(self._found_rect.adjusted(-1, -1, 1, 1), QColor(255, 214, 0, 200))
            pictures = {k: v for k, v in self._images.items() if v is not False}
            # fixed boxes are in the page, in the stacking order, pinned there
            from . import paint as painting

            # animations are drawn at the time since this page was shown
            painting.NOW = time.monotonic() - self._animation_epoch
            paint(painter, self._display, visible, pictures)
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
        below = ""
        if self._display.fixed is not None:
            # fixed boxes, in window coordinates: one with a z-index above 0 is
            # on top; one at 0 or below (GitHub's opening section) lies under
            # what scrolls over it, whose own links come first
            levels = getattr(self._display.fixed, "link_z", [])
            for number, (rect, href) in enumerate(self._display.fixed.links):
                if rect.contains(point):
                    if (levels[number] if number < len(levels) else 1) > 0:
                        return href
                    below = below or href
        point = point.__class__(point.x(), point.y() + self._scroll)
        for info in reversed(self._display.sticky):
            moved = self._display.sticky_offset(info, self._scroll)
            if moved:
                for rect, href in self._display.links[info["links"]:info.get("links_end", info["links"])]:
                    if rect.translated(0, moved).contains(point):
                        return href
        # the last laid out is, as a rule, the one on top
        for rect, href in reversed(self._display.links):
            if rect.contains(point):
                return href
        return below

    def mouseMoveEvent(self, event) -> None:                  # noqa: N802
        from .layout import LabelTarget

        href = self._link_at(event.position())
        button = self._button_at(event.position())
        state = (href, button)
        if state != self._hovered:
            self._hovered = state
            pointing = button is not None or (href and not isinstance(href, LabelTarget))
            from .layout import SummaryTarget

            if isinstance(href, SummaryTarget):
                pointing = True
            self.setCursor(Qt.CursorShape.PointingHandCursor if pointing else Qt.CursorShape.ArrowCursor)
            shown = "" if not href or isinstance(href, LabelTarget) or href == "summary" else \
                self._url.resolved(QUrl(href)).toString()
            self.linkHovered.emit(shown)

    def mouseReleaseEvent(self, event) -> None:               # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position = event.position()
        target = self._element_at(position) if self._script is not None else None
        if target is not None:
            self._script_event({"type": "click", "target": target, "x": position.x(),
                                "y": position.y()},
                               lambda prevented: None if prevented else self._default_click(position))
            return
        self._default_click(position)

    def _default_click(self, position) -> None:
        """What a click does when no script stops it: a link, a button, a label."""
        from .layout import LabelTarget

        button = self._button_at(position)
        if button is not None:
            self._press_button(button)
            return
        href = self._link_at(position)
        if isinstance(href, LabelTarget):
            self._activate_label(href.element)
            return
        from .layout import SummaryTarget

        if isinstance(href, SummaryTarget):
            details = href.element.parent
            if details is not None and details.tag == "details":
                if "open" in details.attrs:
                    del details.attrs["open"]
                else:
                    details.attrs["open"] = ""
                # the change is carried to the worker, which styles the page again
                elements = [self._document.root] + list(self._document.root.elements())
                self._patches[elements.index(details)] = dict(details.attrs)
                self._restyle()
            return
        if not href or href.lower().startswith("javascript:"):
            return
        self.setUrl(self._url.resolved(QUrl(href)))
