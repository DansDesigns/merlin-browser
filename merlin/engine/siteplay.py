"""Videos from sites whose players Merlin Engine cannot run yet: played in
the page all the same.

YouTube's player, and most sites' (Vimeo's, Twitch's), stream their video
through Media Source Extensions, which Merlin Engine does not have yet. For
these, yt-dlp (media.py, with Node.js for YouTube's challenge) finds the
page's stream, a single address with sound and picture, and Merlin plays it
in the page, where the site's own player would have been:

  YouTube         a watch, shorts or live page: its video over the player's
                  box, found at once and played, as YouTube plays it; an
                  embedded one (youtube.com/embed/, in a frame) when its play
                  button is pressed. Its picture shown until then.
  other sites     a <video> the page streams itself (a blob: address), asked
                  to play: its page's stream, found then, played in it.

The person's YouTube and Google cookies go with the lookup, so their own
videos, and those YouTube asks them to sign in for, play as for them.
"""
from __future__ import annotations

import re
import threading
import urllib.parse

from PyQt6.QtCore import QObject, QRectF, QUrl, pyqtSignal

YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                 "youtube-nocookie.com", "www.youtube-nocookie.com", "youtu.be")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

# YouTube's player, outermost last: the first with a box is where the video goes
PLAYER_BOXES = ("#movie_player", ".html5-video-player", "#player-container-inner", "#player-container",
                "#ytd-player", "ytd-player", "#shorts-player", "ytm-player", "#player", ".player-container",
                "#full-bleed-container")

FINDING = "Finding the video…"


def youtube_video(url: str):
    """(video id, is an embed) for a YouTube video page, or None."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return None
    path = parts.path or ""
    if host == "youtu.be":
        found = path.strip("/").split("/")[0]
        return (found, False) if _ID.match(found or "") else None
    if path == "/watch":
        found = urllib.parse.parse_qs(parts.query).get("v", [""])[0]
        return (found, False) if _ID.match(found or "") else None
    for prefix, embed in (("/embed/", True), ("/shorts/", False), ("/live/", False), ("/v/", True)):
        if path.startswith(prefix):
            found = path[len(prefix):].split("/")[0]
            return (found, embed) if _ID.match(found or "") else None
    return None


def cookie_rows(jar) -> list:
    """YouTube's and Google's cookies from a Merlin Engine jar, as media.py
    hands them to yt-dlp; with those Chromium's tabs keep (media.watch_cookies)."""
    from .. import media

    rows = []
    try:
        for cookie in list(jar or []):
            if not media._cookie_site(cookie.domain):
                continue
            rows.append({"domain": cookie.domain, "path": cookie.path or "/", "secure": bool(cookie.secure),
                         "expiry": int(cookie.expires or 0), "name": cookie.name, "value": cookie.value or ""})
    except Exception:                                      # noqa: BLE001
        pass
    seen = {(r["name"], r["domain"], r["path"]) for r in rows}
    for row in media.cookies_snapshot():
        if (row["name"], row["domain"], row["path"]) not in seen:
            rows.append(row)
    return rows


class _Relay(QObject):
    """Back to the interface thread, from a lookup's."""
    done = pyqtSignal(object)


class SitePlayMixin:
    """MerlinView's videos found by yt-dlp. Uses MediaMixin's controllers."""

    def _site_init(self) -> None:
        self._site = None                  # {"key", "id", "embed", "page", "state", "reason"}
        self._media_site_places = {}
        self._site_lookups = {}            # page address -> "finding" | "found" | "failed"
        if not hasattr(self, "_site_relay"):
            self._site_relay = _Relay()
            self._site_relay.done.connect(self._site_found)

    # The lookup: media.resolve_stream (url, timeout, cookies) -> (ok, stream or reason)
    def _site_resolver(self):
        from .. import media

        return getattr(type(self), "site_resolver", None) or media.resolve_stream

    @staticmethod
    def _site_missing(page: str) -> list:
        """What the lookup needs and Merlin has not fetched: [(name, size, fetch)]."""
        from .. import media

        missing = []
        if not media.has_ytdlp():
            missing.append(("yt-dlp", "about 3 MB", media.fetch_ytdlp))
        if media._needs_js_runtime(page) and not media.node_path():
            missing.append(("Node.js", "about 30 MB", media.fetch_node))
        return missing

    # ------------------------------------------------------------ YouTube's pages
    def _site_sync(self) -> None:
        """On a YouTube video page: its video, over its player."""
        found = youtube_video(self._url.toString())
        current = getattr(self, "_site", None)
        if found is None:
            if current is not None:
                self._site_end()
            return
        video, embed = found
        embed = embed or self.is_frame()
        if current is None or current["id"] != video:
            if current is not None:
                self._site_end()
            key = f"site:yt:{video}"
            controller = self._new_player(key, None, False)
            controller.forced_controls = True
            controller.controls = True
            controller.detached = True
            page = f"https://www.youtube.com/watch?v={video}"
            self._site = {"key": key, "id": video, "embed": embed, "page": page,
                          "thumbnail": f"https://i.ytimg.com/vi/{video}/hqdefault.jpg"}
            controller.poster_src = self._site["thumbnail"]
            self._want_image(self._site["thumbnail"])
            if not embed:
                # a watch page plays its video, as YouTube's does
                controller._want_play = True
                self._site_lookup(key, page, person=False)
        controller = self._players.get(self._site["key"])
        if controller is None:
            return
        picture = self._images.get(self._site["thumbnail"])
        if controller.poster is None and picture is not None and picture is not False and hasattr(picture, "isNull"):
            controller.poster = picture
        self._media_site_places = {self._site["key"]: self._site_place()}
        self._media_places.update(self._media_site_places)

    def _site_place(self):
        """Where YouTube's player is: (rect, fixed, None)."""
        if self._site and self._site.get("embed"):
            # an embedded player (a frame of youtube.com/embed/) is the whole frame
            return (QRectF(0, 0, self.width(), self.height()), True, None)
        display = self._display
        best = None
        if display is not None and self._document is not None:
            from .css import matches, parse_selector

            selectors = [parse_selector(text) for text in PLAYER_BOXES]
            boxes = {}
            for rect, element in display.boxes:
                if rect.width() >= 200:
                    held = boxes.get(id(element))
                    if held is None or rect.width() * max(rect.height(), 1) > held[0].width() * max(held[0].height(), 1):
                        boxes[id(element)] = (rect, element)
            for selector in selectors:
                if selector is None:
                    continue
                for rect, element in boxes.values():
                    if matches(selector, element):
                        best = QRectF(rect)
                        break
                if best is not None:
                    break
        width = self._page_width()
        if best is None:
            # no player laid out (YouTube's own scripts have not built it):
            # where it would be, at the top of the page
            room = min(width - 48.0, 1280.0)
            best = QRectF((width - room) / 2, 72.0, room, room * 9 / 16)
        if best.height() < best.width() * 0.3:
            best.setHeight(best.width() * 9 / 16)
        return (best, False, None)

    def _site_end(self) -> None:
        current = getattr(self, "_site", None)
        if current is not None:
            controller = self._players.pop(current["key"], None)
            if controller is not None:
                controller.shut_down()
                controller.deleteLater()
            self._media_places.pop(current["key"], None)
        self._site = None
        self._media_site_places = {}

    # ------------------------------------------------------------ finding a stream
    def _site_stream_for(self, key, controller, person: bool = False) -> None:
        """A video asked to play with nothing Merlin can play: its page's
        stream, found by yt-dlp (YouTube's own page: its video's). On other
        sites only when a person presses play: a page's script asking would
        have run yt-dlp on every page with a video."""
        if controller is None:
            return
        site = getattr(self, "_site", None)
        if site is not None and key == site["key"]:
            self._site_lookup(key, site["page"], person)
            return
        if not person:
            return
        found = youtube_video(self._url.toString())
        page = f"https://www.youtube.com/watch?v={found[0]}" if found else self._url.toString()
        if not page.startswith(("http://", "https://")):
            return
        self._site_lookup(key, page, person)

    def _site_lookup(self, key, page: str, person: bool = False) -> None:
        controller = self._players.get(key)
        if controller is None:
            return
        if getattr(controller, "site_finding", False) or getattr(controller, "site_stream", ""):
            return
        # what yt-dlp needs, fetched after asking, and only when a person
        # pressed play: never a question as a page opens
        missing = [] if getattr(type(self), "site_resolver", None) is not None else self._site_missing(page)
        if missing:
            if not person or not self._site_may_fetch(missing):
                controller.note = ("To play this video Merlin needs " + " and ".join(n for n, _s, _f in missing)
                                   + ". Press play to fetch " + ("them" if len(missing) > 1 else "it") + ".")
                controller._want_play = False
                controller.changed.emit()
                return
        controller.site_finding = True
        controller.note = FINDING
        controller.error = ""
        controller.changed.emit()
        rows = cookie_rows(self._cookies())
        resolver = self._site_resolver()
        relay = self._site_relay

        def work():
            try:
                for _name, _size, fetch in missing:
                    fetch()
                ok, result = resolver(page, 60, rows)
            except Exception as exc:                       # noqa: BLE001
                ok, result = False, str(exc)
            try:
                relay.done.emit((key, page, ok, result))
            except RuntimeError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _site_may_fetch(self, missing: list) -> bool:
        """Asked once: may Merlin fetch what the lookup needs?"""
        from PyQt6.QtWidgets import QMessageBox

        listing = "\n".join(f"  • {name}, {size}" for name, size, _f in missing)
        answer = QMessageBox.question(
            self.window(), "Fetch what the video needs",
            f"To play this video in the page, Merlin needs:\n\n{listing}\n\n"
            "yt-dlp finds a video's stream on its page; Node.js is what it needs for "
            "YouTube's. Both come from their projects' own releases and are kept in "
            "Merlin's folder. Fetch them now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        return answer == QMessageBox.StandardButton.Yes

    def _site_found(self, answer) -> None:
        key, page, ok, result = answer
        controller = self._players.get(key)
        if controller is None:
            return
        controller.site_finding = False
        if ok and result:
            controller.site_stream = result
            controller.note = ""
            want = controller.wants_to_play()
            controller.set_source(result, preload=True, autoplay=want)
            if want:
                controller.play()
        else:
            from .. import media

            reason = str(result or "").strip().splitlines()[0] if str(result or "").strip() else ""
            if result == media.JS_RUNTIME_MISSING:
                controller.note = "This video needs Node.js, which Merlin could not fetch."
            elif "is not installed" in reason:
                controller.note = "This video needs yt-dlp, which Merlin could not fetch."
            else:
                controller.note = "Merlin could not find this video's stream." + (
                    f"\n{reason[:160]}" if reason else "")
            controller._want_play = False
        controller.changed.emit()
