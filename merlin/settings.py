"""Persistent settings for Merlin Browser.

Config lives at $XDG_CONFIG_HOME/merlin/settings.json (default ~/.config/merlin).
Nothing here imports QtWebEngine, so it is safe to import before the
QApplication exists.
"""
from __future__ import annotations

import copy
import json
import os
import sys

from PyQt6.QtCore import QObject, pyqtSignal

from .brand import APP_SCHEME, APP_SLUG

def _base_dirs() -> tuple[str, str, str]:
    """Config, data and cache roots, following each platform's convention."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        local = os.environ.get("LOCALAPPDATA") or appdata
        root = os.path.join(appdata, APP_SLUG.capitalize())
        local_root = os.path.join(local, APP_SLUG.capitalize())
        return root, os.path.join(local_root, "data"), os.path.join(local_root, "cache")
    if sys.platform == "darwin":
        support = os.path.expanduser("~/Library/Application Support")
        return (os.path.join(support, APP_SLUG),
                os.path.join(support, APP_SLUG, "data"),
                os.path.expanduser(f"~/Library/Caches/{APP_SLUG}"))
    return (
        os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                    os.path.expanduser("~/.config")), APP_SLUG),
        os.path.join(os.environ.get("XDG_DATA_HOME",
                                    os.path.expanduser("~/.local/share")), APP_SLUG),
        os.path.join(os.environ.get("XDG_CACHE_HOME",
                                    os.path.expanduser("~/.cache")), APP_SLUG),
    )


CONFIG_DIR, DATA_DIR, CACHE_DIR = _base_dirs()

SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
FILTER_DIR = os.path.join(DATA_DIR, "filters")
BOOKMARKS_FILE = os.path.join(CONFIG_DIR, "bookmarks.json")
HISTORY_DB = os.path.join(DATA_DIR, "history.sqlite")

MAX_START_TILES = 5

SEARCH_ENGINES = {
    "DuckDuckGo": "https://duckduckgo.com/?q={}",
    "Google": "https://www.google.com/search?q={}",
    "Bing": "https://www.bing.com/search?q={}",
    "Brave": "https://search.brave.com/search?q={}",
    "Startpage": "https://www.startpage.com/sp/search?query={}",
    "Ecosia": "https://www.ecosia.org/search?q={}",
    "Qwant": "https://www.qwant.com/?q={}",
    "Mojeek": "https://www.mojeek.com/search?q={}",
    "Yahoo": "https://search.yahoo.com/search?p={}",
    "Wikipedia": "https://en.wikipedia.org/w/index.php?search={}",
    "YouTube": "https://www.youtube.com/results?search_query={}",
    "Custom": "",
}

# Short prefixes: typing "wiki merlin" searches Wikipedia for merlin.
SEARCH_KEYWORDS = {
    "ddg": "DuckDuckGo", "g": "Google", "b": "Bing", "br": "Brave",
    "sp": "Startpage", "ec": "Ecosia", "qw": "Qwant", "mj": "Mojeek",
    "yh": "Yahoo", "wiki": "Wikipedia", "yt": "YouTube",
}

DEFAULT_FILTER_LISTS = [
    "https://easylist.to/easylist/easylist.txt",
    "https://easylist.to/easylist/easyprivacy.txt",
    "https://secure.fanboy.co.nz/fanboy-annoyance.txt",
]

DEFAULTS = {
    # --- window / chrome ---
    "hide_window_decorations": False,
    "show_window_buttons_when_frameless": True,
    "remember_window_geometry": True,
    "tab_orientation": "horizontal",   # horizontal | left | right
    "page_corner_radius": 10,          # 0 turns the rounded page corners off
    "start_background": "midnight",    # see ui.START_BACKGROUNDS, or image:<path>
    "start_tiles": [
        {"title": "Wikipedia", "url": "https://wikipedia.org"},
        {"title": "Hacker News", "url": "https://news.ycombinator.com"},
        {"title": "GitHub", "url": "https://github.com"},
        {"title": "Codeberg", "url": "https://codeberg.org"},
    ],
    "window_geometry": None,
    "window_maximized": False,
    "dark_ui": True,
    "ui_font_pt": 0,                   # 0 = whatever the system provides
    # --- browsing ---
    "home_page": f"{APP_SCHEME}://start",
    "new_tab_page": f"{APP_SCHEME}://start",
    "search_engine": "DuckDuckGo",
    "custom_search_url": "",           # must contain {} where the query goes
    "search_keywords_enabled": True,
    "restore_session": True,
    "single_instance": True,           # links reuse the window already open
    "last_session": [],
    "default_zoom": 1.0,
    # --- privacy / shields ---
    "adblock_enabled": True,
    "cosmetic_filtering": True,
    "block_third_party_cookies": True,
    "send_do_not_track": True,
    "https_upgrade": True,
    "block_webrtc_leak": True,
    "shields_exceptions": [],          # hostnames where shields are off
    "filter_lists": list(DEFAULT_FILTER_LISTS),
    # --- media ---
    "player_mode": "embedded",         # embedded | window | libvlc | off
    "player_command": "",              # empty = auto-detect (mpv, vlc, ...)
    "player_args": "",
    "auto_offer_player": True,         # offer when the engine cannot decode
    "libvlc_ack": False,               # user acknowledged the in-process note
    # --- web apps ---
    "web_apps": [],                    # {name, url, icon, shortcut}
    # --- input ---
    "swipe_navigation": True,          # two-finger swipe = back / forward
    "invert_swipe": False,
    # --- updates ---
    "check_updates_on_start": True,
    "last_seen_version": "",
    # --- proxy ---
    "proxy_mode": "none",              # none | tor | custom
    "proxy_url": "",                   # socks5://host:port or http://host:port
    # --- engine ---
    "user_agent": "",                  # empty = QtWebEngine default
    "chromium_flags": "",              # extra flags appended at launch
}


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, CACHE_DIR, FILTER_DIR):
        os.makedirs(d, exist_ok=True)


class Settings(QObject):
    """Dict-backed settings with a change signal."""

    changed = pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        ensure_dirs()
        self._data = copy.deepcopy(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                for key, value in stored.items():
                    if key in DEFAULTS:
                        self._data[key] = value
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        ensure_dirs()
        tmp = SETTINGS_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
            os.replace(tmp, SETTINGS_FILE)
        except OSError:
            pass

    # --------------------------------------------------------------- access
    def get(self, key: str, fallback=None):
        return self._data.get(key, DEFAULTS.get(key, fallback))

    def set(self, key: str, value, save: bool = True) -> None:
        if self._data.get(key) == value:
            return
        self._data[key] = value
        if save:
            self.save()
        self.changed.emit(key, value)

    def toggle(self, key: str) -> bool:
        value = not bool(self.get(key))
        self.set(key, value)
        return value

    def template_for(self, name: str = "") -> str:
        name = name or self.get("search_engine")
        if name == "Custom":
            custom = (self.get("custom_search_url") or "").strip()
            if "{}" in custom:
                return custom
            return SEARCH_ENGINES["DuckDuckGo"]
        return SEARCH_ENGINES.get(name) or SEARCH_ENGINES["DuckDuckGo"]

    def search_url(self, term: str) -> str:
        from urllib.parse import quote_plus

        engine = ""
        if self.get("search_keywords_enabled") and " " in term:
            prefix, _, rest = term.partition(" ")
            candidate = SEARCH_KEYWORDS.get(prefix.lower())
            if candidate and rest.strip():
                engine, term = candidate, rest.strip()
        return self.template_for(engine).format(quote_plus(term))

    # ------------------------------------------------------- start tiles
    def tiles(self) -> list[dict]:
        raw = self.get("start_tiles") or []
        clean = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url", "")).strip()
            if not url:
                continue
            title = str(entry.get("title", "")).strip() or url
            clean.append({"title": title, "url": url})
            if len(clean) >= MAX_START_TILES:
                break
        return clean

    def set_tiles(self, tiles: list[dict]) -> None:
        self.set("start_tiles", tiles[:MAX_START_TILES])

    def add_tile(self, title: str, url: str) -> bool:
        tiles = self.tiles()
        if len(tiles) >= MAX_START_TILES or not url.strip():
            return False
        tiles.append({"title": title.strip() or url.strip(),
                      "url": url.strip()})
        self.set_tiles(tiles)
        return True

    # ----------------------------------------------------------- shields
    def shields_enabled_for(self, host: str) -> bool:
        if not self.get("adblock_enabled"):
            return False
        host = (host or "").lower().lstrip(".")
        for exception in self.get("shields_exceptions", []):
            if host == exception or host.endswith("." + exception):
                return False
        return True

    def set_shields_for(self, host: str, enabled: bool) -> None:
        host = (host or "").lower().lstrip(".")
        if not host:
            return
        exceptions = list(self.get("shields_exceptions", []))
        if enabled and host in exceptions:
            exceptions.remove(host)
        elif not enabled and host not in exceptions:
            exceptions.append(host)
        else:
            return
        self.set("shields_exceptions", exceptions)
