"""Frames (<iframe>) for Merlin Engine.

A frame is another page, shown in a box of the page that holds it. Here each
is a MerlinView of its own, a child widget placed over the frame's box, as a
form field is: it fetches its page, styles, lays out and draws it, and runs its
scripts in a Deno process of its own, as Chrome runs each site's frames apart.
So everything a page can do, a frame's page can do.

What joins a frame to its parent is here:

  * which frames a page has, made and placed as the page is laid out, kept
    while scripts change the page, and their pages loaded (src or srcdoc)
  * the request a frame's page is fetched with: Sec-Fetch-Dest: iframe and
    the parent as its referrer, as Chrome sends; and a site that will not be
    shown in another site's frame (X-Frame-Options, CSP frame-ancestors) is
    not shown, as Chrome does not show it
  * a frame of another site than the tab's keeps its cookies and storage
    apart, for this tab only (as Brave's ephemeral third-party storage does):
    it cannot follow the user from site to site
  * messages between windows: postMessage to and from a frame, its parent and
    the top, with the sender's origin and a source to reply to, and
    MessagePorts carried across (reCAPTCHA's frames talk over MessageChannels)

Limits, for now: a frame of the same site cannot reach into its parent's
document (or the parent into the frame's), and what is drawn over a frame by
its parent's page is drawn under it.
"""
from __future__ import annotations

import itertools
import re
import urllib.parse
import weakref

from PyQt6.QtCore import QTimer, QUrl

MAX_DEPTH = 4            # frames within frames, this deep at most
MAX_FRAMES = 16           # frames for one page, in all its depths (each runs its own scripts)
HELD_FOR = 8000          # ms a message for a window still loading is held

_TOKENS = itertools.count(1)
VIEWS: dict = {}         # a window's token -> weakref to its view
PORT_ROUTES: dict = {}   # a carried port's id -> (token, token) of its two ends


def register(view) -> int:
    token = next(_TOKENS)
    VIEWS[token] = weakref.ref(view)
    return token


def view_for(token):
    found = VIEWS.get(token)
    view = found() if found is not None else None
    if view is None:
        VIEWS.pop(token, None)
    return view


def origin_of(url: QUrl) -> str:
    """scheme://host[:port], or "null" for an address with none."""
    if not url.isValid() or url.scheme() not in ("http", "https"):
        return "null"
    port = url.port()
    default = {"http": 80, "https": 443}.get(url.scheme())
    return f"{url.scheme()}://{url.host()}" + (f":{port}" if port not in (-1, default) else "")


def registrable(host: str) -> str:
    from .view import _registrable

    return _registrable(host)


def fetch_site(initiator: QUrl, target: QUrl) -> str:
    """Sec-Fetch-Site for a frame's page asked for by initiator's page."""
    if origin_of(initiator) == origin_of(target) and origin_of(target) != "null":
        return "same-origin"
    if registrable(initiator.host()) == registrable(target.host()) and initiator.scheme() == target.scheme():
        return "same-site"
    return "cross-site"


def refuses_framing(headers: dict, frame_url: str, ancestors: list) -> bool:
    """Whether a page will not be shown in these frames' parents: its
    X-Frame-Options, or its Content-Security-Policy frame-ancestors, as Chrome
    reads them (ancestors: the origins of every window above, nearest first)."""
    found = {k.lower(): v for k, v in (headers or {}).items()}
    own = origin_of(QUrl(frame_url))
    policy = found.get("content-security-policy", "")
    for directive in policy.split(";"):
        parts = directive.strip().split()
        if not parts or parts[0].lower() != "frame-ancestors":
            continue
        sources = [p.strip("'\"").lower() for p in parts[1:]]
        if "none" in sources or not sources:
            return True
        return not all(_source_allows(sources, origin, own) for origin in ancestors)
    xfo = found.get("x-frame-options", "").strip().lower()
    if xfo.startswith("deny"):
        return True
    if xfo.startswith("sameorigin"):
        return any(origin != own for origin in ancestors)
    return False


def _source_allows(sources: list, origin: str, own: str) -> bool:
    for source in sources:
        if source == "*":
            return True
        if source == "self" and origin == own:
            return True
        if source.endswith(":") and origin.startswith(source):
            return True
        pattern = re.escape(source.rstrip("/")).replace(r"\*", r"[^/]+")
        if "://" not in source:
            pattern = r"https?://" + pattern
        if re.fullmatch(pattern, origin):
            return True
    return False


def refused_page(url: str) -> str:
    import html

    host = html.escape(QUrl(url).host() or url)
    return ("<!DOCTYPE html><title></title><body style='margin:0;background:#f1f3f4;font:13px sans-serif;"
            "color:#5f6368;display:flex;align-items:center;justify-content:center;height:100vh'>"
            f"<p>{host} refused to connect.</p></body>")


class FramesMixin:
    """MerlinView's frames: as a parent, the frames its page has; as a frame,
    how it differs from a tab's page. Mixed into MerlinView."""

    # ---------------------------------------------------- as a frame
    def _init_frames(self) -> None:
        self._frames: dict = {}          # <iframe> element -> its view
        self._frame_loaded_src: dict = {}  # its view -> what it was given (src or srcdoc)
        self._frame_parent = None        # the view this one is a frame of
        self._frame_depth = 0
        self._frame_scrolling = True
        self._frame_jars: dict = {}      # (top only) another site's frames' cookies, this tab only
        self._frame_blocked: dict = {}   # a frame's address -> whether the content blocker stops it
        self._token = register(self)
        self._held: list = []            # messages for this window's scripts, before they can take them
        self._scripts_settled = False
        self._held_timer = None

    def is_frame(self) -> bool:
        return self._frame_parent is not None

    def frame_top(self):
        view = self
        while view._frame_parent is not None:
            view = view._frame_parent
        return view

    def _ancestor_origins(self) -> list:
        found, view = [], self._frame_parent
        while view is not None:
            found.append(origin_of(view._url))
            view = view._frame_parent
        return found

    def _cross_site_frame(self) -> bool:
        top = self.frame_top()
        return top is not self and registrable(self._url.host()) != registrable(top._url.host())

    def _frame_cookies(self, window_jar):
        """A frame of another site than the tab's: cookies of its own, for this
        tab's page only."""
        if not self.is_frame() or not self._cross_site_frame():
            return window_jar
        from .view import new_cookie_jar

        top = self.frame_top()
        site = registrable(self._url.host())
        jar = top._frame_jars.get(site)
        if jar is None:
            jar = top._frame_jars[site] = new_cookie_jar()
        return jar

    def _frame_headers(self, headers: dict, url: QUrl) -> dict:
        """A frame's page is asked for as Chrome asks: as an iframe's, from its parent."""
        parent = self._frame_parent
        headers = dict(headers)
        headers["Sec-Fetch-Dest"] = "iframe"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = fetch_site(parent._url, url) if parent is not None else "none"
        headers.pop("Sec-Fetch-User", None)
        if parent is not None and parent._url.scheme() in ("http", "https"):
            # Chrome's default referrer policy: the origin only, to another site
            same = origin_of(parent._url) == origin_of(url)
            headers["Referer"] = (parent._url.toString(QUrl.UrlFormattingOption.RemoveFragment) if same
                                  else origin_of(parent._url) + "/")
        return headers

    def _frame_script_state(self) -> dict:
        state = {"token": self._token}
        if self.is_frame():
            state["frame"] = {"parent": self._frame_parent._token, "top": self.frame_top()._token}
        return state

    # ---------------------------------------------------- as a parent
    def _clear_frames(self) -> None:
        for view in list(self._frames.values()):
            self._end_frame(view)
        self._frames = {}
        self._frame_loaded_src = {}
        self._frame_blocked = {}
        if not self.is_frame():
            self._frame_jars = {}

    def _end_frame(self, view) -> None:
        try:
            view._clear_frames()
            view.shutdown()
            view.hide()
            view.deleteLater()
        except RuntimeError:
            pass
        VIEWS.pop(getattr(view, "_token", None), None)
        self._frame_loaded_src.pop(view, None)

    def _frame_count(self) -> int:
        return sum(1 + view._frame_count() for view in self._frames.values())

    def _sync_frames(self, laid_out: dict) -> None:
        """Frames for the page's <iframe>s: made, kept, loaded, and placed.

        laid_out: element -> (rect, fixed) for those with a box; an <iframe>
        without one (display: none) still has its page, as in a browser, and
        its messages: a frame kept out of sight is often there to talk."""
        if self._document is None:
            return
        present = [e for e in self._document.root.elements() if e.tag in ("iframe", "frame")]
        for element in [e for e in self._frames if e not in present]:
            self._end_frame(self._frames.pop(element))
        budget = MAX_FRAMES - self.frame_top()._frame_count()
        for element in present:
            view = self._frames.get(element)
            wanted = self._frame_source(element)
            if view is None:
                if wanted is None or self._frame_depth + 1 > MAX_DEPTH or budget <= 0:
                    continue
                view = self._make_frame(element)
                if view is None:
                    continue
                budget -= 1
                self._frames[element] = view
            if self._frame_loaded_src.get(view) != wanted:
                self._frame_loaded_src[view] = wanted
                self._load_frame(view, wanted)
            if element in laid_out:
                self._places[element] = laid_out[element]
            else:
                self._places.pop(element, None)
            scrolling = element.attrs.get("scrolling", "").strip().lower() not in ("no", "0")
            if scrolling != view._frame_scrolling:
                view._frame_scrolling = scrolling
                view._update_scrollbar()
            if abs(view._zoom - self._zoom) > 0.001:
                view._zoom = self._zoom
                view._layout()
        self.scrollbar.raise_()

    def _frame_source(self, element):
        """What a frame shows: ("srcdoc", markup), ("url", address), or ("blank",);
        None for one the content blocker stops."""
        if "srcdoc" in element.attrs:
            return ("srcdoc", element.attrs.get("srcdoc", ""))
        src = (element.attrs.get("src") or "").strip()
        if not src or src.lower().startswith(("about:", "javascript:")):
            return ("blank",)
        address = self._url.resolved(QUrl(src))
        if address.scheme() not in ("http", "https", "data"):
            return ("blank",)
        text = address.toString()
        # asked once a page: asked at every layout, a blocked frame had been
        # counted again each time
        blocked = self._frame_blocked.get(text)
        if blocked is None:
            blocked = self._frame_blocked[text] = self._blocked(text, "subdocument")
        return None if blocked else ("url", text)

    def _make_frame(self, element):
        from .view import MerlinView

        try:
            view = MerlinView(self, host=self._host, profile=self._page.profile())
        except Exception:                                  # noqa: BLE001
            return None
        view._frame_parent = self
        view._frame_depth = self._frame_depth + 1
        view._zoom = self._zoom
        view.setProperty("merlin_frame", True)
        view.resize(300, 150)
        view.hide()
        view.loadFinished.connect(lambda _ok, v=view: self._frame_finished(v))
        self._tell_frame_token(element, view)
        return view

    def _tell_frame_token(self, element, view) -> None:
        known = element.attrs.get("data-mjs")
        if self._script is not None and known:
            self._script.send({"type": "frame_token", "target": int(known), "token": view._token})

    def _load_frame(self, view, wanted) -> None:
        view._held = []
        if wanted[0] == "srcdoc":
            view._load_markup(wanted[1], self._url)
        elif wanted[0] == "url":
            view.setUrl(QUrl(wanted[1]))
        else:
            view._load_markup("", self._url)

    def _frame_finished(self, view) -> None:
        """A frame's page is in: its <iframe>'s load event, for the parent's scripts."""
        for element, held in self._frames.items():
            if held is view and self._script is not None and element.attrs.get("data-mjs"):
                self._script.send({"type": "frame_loaded", "target": int(element.attrs["data-mjs"])})
                break

    def _rekey_frames(self, old_elements: dict) -> None:
        """The page's elements were made again from what its scripts left:
        each frame stays with its <iframe>, found by the scripts' number."""
        by_number = {e.attrs.get("data-mjs"): v for e, v in old_elements.items() if e.attrs.get("data-mjs")}
        kept = {}
        for element in self._document.root.elements():
            known = element.attrs.get("data-mjs")
            if known and known in by_number:
                kept[element] = by_number.pop(known)
        for view in by_number.values():
            self._end_frame(view)
        for element in [e for e in old_elements if not e.attrs.get("data-mjs")]:
            self._end_frame(old_elements[element])
        self._frames = kept

    # ---------------------------------------------------- messages
    def _window_message(self, message: dict) -> None:
        """postMessage, a port's message, or a navigation, from this window's
        scripts to another window."""
        kind = message.get("type")
        if kind == "port_message":
            route = PORT_ROUTES.get(message.get("port"))
            if route is None:
                return
            other = route[1] if route[0] == self._token else route[0]
            target = view_for(other)
            if target is not None:
                self._carry_ports(message.get("ports"), target)
                target._deliver({"type": "port_message", "port": message.get("port"),
                                 "data": message.get("data"), "ports": message.get("ports") or []})
            return
        target = self._window_target(message)
        if target is None:
            return
        if kind == "navigate_window":
            url = target._url.resolved(QUrl(str(message.get("url", ""))))
            if url.scheme() in ("http", "https"):
                QTimer.singleShot(0, lambda v=target, u=url: v.setUrl(u))
            return
        if kind != "post":
            return
        sender = origin_of(self._url) if self._url.scheme() in ("http", "https") else self._inherited_origin()
        wanted = str(message.get("targetOrigin") or "/")
        receiver = origin_of(target._url) if target._url.scheme() in ("http", "https") else target._inherited_origin()
        if wanted == "/":
            wanted = sender
        if wanted != "*" and wanted.rstrip("/") != receiver:
            return                                     # meant for another page
        self._carry_ports(message.get("ports"), target)
        target._deliver({"type": "message", "data": message.get("data"), "origin": sender,
                         "source": self._token, "ports": message.get("ports") or []})

    def _inherited_origin(self) -> str:
        """An about:blank or srcdoc frame's origin: its parent's."""
        view = self
        while view is not None:
            if view._url.scheme() in ("http", "https"):
                return origin_of(view._url)
            view = view._frame_parent
        return "null"

    def _window_target(self, message: dict):
        if message.get("token"):
            return view_for(int(message["token"]))
        known = message.get("target")
        if known is not None:
            element = self._by_script_id.get(str(known))
            return self._frames.get(element) if element is not None else None
        return None

    def _carry_ports(self, ids, target) -> None:
        for port in ids or []:
            PORT_ROUTES[port] = (self._token, target._token)

    def _deliver(self, message: dict) -> None:
        """A message for this window's scripts: now, or once they can take it."""
        if self._script is not None and self._scripts_settled:
            self._script.send(message)
            return
        self._held.append(message)
        if self._held_timer is None:
            # a page whose scripts never finish loading gets its messages anyway
            self._held_timer = QTimer(self)
            self._held_timer.setSingleShot(True)
            self._held_timer.timeout.connect(self._scripts_ready_for_messages)
        if not self._held_timer.isActive():
            self._held_timer.start(HELD_FOR)

    def _scripts_ready_for_messages(self) -> None:
        if self._script is None:
            return
        self._scripts_settled = True
        held, self._held = self._held, []
        for message in held:
            self._script.send(message)

    def _scripts_started_for_frames(self) -> None:
        """This page's scripts have started: its frames' tokens go to them."""
        self._scripts_settled = False
        for element, view in self._frames.items():
            self._tell_frame_token(element, view)
