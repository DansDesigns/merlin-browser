"""Import from another browser.

Bookmarks and history are read straight out of the other browser's profile:
Chromium keeps bookmarks in a JSON file and history in SQLite, Firefox keeps
both in SQLite. Nothing is written to the other browser's files; the databases
are copied to a temporary location first, because a browser that is running
holds a lock on them.

Passwords are deliberately not read. In Chromium they are encrypted with a key
held in the OS keystore, DPAPI on Windows and the login keyring elsewhere, and
prising that open would mean reproducing a technique whose whole purpose is to
extract someone's saved credentials. Every browser can export its own passwords
to CSV from its settings, with the user present and authenticated, and that file
is what Merlin imports. Same result, without Merlin containing a credential
dumper.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field


@dataclass
class Source:
    name: str
    kind: str                      # "chromium" or "firefox"
    path: str
    bookmarks: int = 0
    history: int = 0
    notes: list = field(default_factory=list)


def _local_app_data() -> str:
    return os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))


def _app_data() -> str:
    return os.environ.get("APPDATA", os.path.expanduser("~"))


def candidate_profiles() -> list[tuple[str, str, str]]:
    """(display name, kind, profile directory) for browsers we can read."""
    out: list[tuple[str, str, str]] = []
    home = os.path.expanduser("~")

    if os.name == "nt":
        local, roaming = _local_app_data(), _app_data()
        chromium = [
            ("Google Chrome", os.path.join(local, "Google", "Chrome", "User Data")),
            ("Microsoft Edge", os.path.join(local, "Microsoft", "Edge", "User Data")),
            ("Brave", os.path.join(local, "BraveSoftware", "Brave-Browser",
                                   "User Data")),
            ("Vivaldi", os.path.join(local, "Vivaldi", "User Data")),
            ("Opera", os.path.join(roaming, "Opera Software", "Opera Stable")),
        ]
        firefox = [("Firefox", os.path.join(roaming, "Mozilla", "Firefox",
                                            "Profiles"))]
    elif sys_is_mac():
        support = os.path.join(home, "Library", "Application Support")
        chromium = [
            ("Google Chrome", os.path.join(support, "Google", "Chrome")),
            ("Microsoft Edge", os.path.join(support, "Microsoft Edge")),
            ("Brave", os.path.join(support, "BraveSoftware", "Brave-Browser")),
        ]
        firefox = [("Firefox", os.path.join(support, "Firefox", "Profiles"))]
    else:
        config = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
        chromium = [
            ("Google Chrome", os.path.join(config, "google-chrome")),
            ("Chromium", os.path.join(config, "chromium")),
            ("Microsoft Edge", os.path.join(config, "microsoft-edge")),
            ("Brave", os.path.join(config, "BraveSoftware", "Brave-Browser")),
            ("Vivaldi", os.path.join(config, "vivaldi")),
        ]
        firefox = [("Firefox", os.path.join(home, ".mozilla", "firefox"))]

    for label, root in chromium:
        if not os.path.isdir(root):
            continue
        for profile in ("Default", "Profile 1", "Profile 2", ""):
            directory = os.path.join(root, profile) if profile else root
            if os.path.isfile(os.path.join(directory, "Bookmarks")) or \
                    os.path.isfile(os.path.join(directory, "History")):
                suffix = f" ({profile})" if profile and profile != "Default" else ""
                out.append((label + suffix, "chromium", directory))

    for label, root in firefox:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            directory = os.path.join(root, entry)
            if os.path.isfile(os.path.join(directory, "places.sqlite")):
                out.append((f"{label} ({entry.split('.')[-1]})", "firefox",
                            directory))
    return out


def sys_is_mac() -> bool:
    import sys

    return sys.platform == "darwin"


def _open_copy(path: str):
    """Copy a database aside before opening it.

    A running browser holds a lock, and opening the original read-write would
    fail or, worse, disturb it.
    """
    if not os.path.isfile(path):
        return None, None
    handle = tempfile.NamedTemporaryFile(prefix="merlin-import-", delete=False)
    handle.close()
    try:
        shutil.copyfile(path, handle.name)
        for extra in ("-wal", "-shm"):
            if os.path.isfile(path + extra):
                shutil.copyfile(path + extra, handle.name + extra)
        return sqlite3.connect(handle.name), handle.name
    except (OSError, sqlite3.Error):
        try:
            os.remove(handle.name)
        except OSError:
            pass
        return None, None


def _discard(path: str) -> None:
    for name in (path, path + "-wal", path + "-shm"):
        try:
            if name and os.path.isfile(name):
                os.remove(name)
        except OSError:
            pass


def read_chromium_bookmarks(directory: str) -> list[dict]:
    path = os.path.join(directory, "Bookmarks")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []

    found: list[dict] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("type") == "url" and node.get("url"):
            found.append({"title": node.get("name") or node["url"],
                          "url": node["url"]})
        for child in node.get("children") or []:
            walk(child)

    for root in (data.get("roots") or {}).values():
        walk(root)
    return found


def read_chromium_history(directory: str, limit: int = 5000) -> list[dict]:
    connection, temp = _open_copy(os.path.join(directory, "History"))
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT url, title FROM urls WHERE url LIKE 'http%' "
            "ORDER BY last_visit_time DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()
        _discard(temp)
    return [{"url": u, "title": t or u} for u, t in rows]


def read_firefox_bookmarks(directory: str) -> list[dict]:
    connection, temp = _open_copy(os.path.join(directory, "places.sqlite"))
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT b.title, p.url FROM moz_bookmarks b "
            "JOIN moz_places p ON b.fk = p.id "
            "WHERE b.type = 1 AND p.url LIKE 'http%'").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()
        _discard(temp)
    return [{"title": t or u, "url": u} for t, u in rows]


def read_firefox_history(directory: str, limit: int = 5000) -> list[dict]:
    connection, temp = _open_copy(os.path.join(directory, "places.sqlite"))
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT url, title FROM moz_places WHERE url LIKE 'http%' "
            "ORDER BY last_visit_date DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()
        _discard(temp)
    return [{"url": u, "title": t or u} for u, t in rows]


def read_source(kind: str, directory: str, want_bookmarks: bool,
                want_history: bool) -> tuple[list[dict], list[dict]]:
    bookmarks: list[dict] = []
    history: list[dict] = []
    if kind == "chromium":
        if want_bookmarks:
            bookmarks = read_chromium_bookmarks(directory)
        if want_history:
            history = read_chromium_history(directory)
    else:
        if want_bookmarks:
            bookmarks = read_firefox_bookmarks(directory)
        if want_history:
            history = read_firefox_history(directory)
    return bookmarks, history


def read_password_csv(path: str) -> tuple[list[dict], str]:
    """Read a password export produced by another browser.

    Chrome, Edge, Brave and Firefox all write url/username/password columns,
    with the header spelled slightly differently, so the columns are matched by
    name rather than position.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        return [], f"Could not read the file: {exc}"
    if not rows:
        return [], "That file has no entries in it."

    def pick(row, *names):
        for name in names:
            for key in row:
                if key and key.strip().lower() == name:
                    return (row[key] or "").strip()
        return ""

    out = []
    for row in rows:
        url = pick(row, "url", "login_uri", "website", "hostname")
        user = pick(row, "username", "login_username", "login", "user")
        secret = pick(row, "password", "login_password")
        if url and secret:
            out.append({"url": url, "username": user, "password": secret})
    if not out:
        return [], ("No url/username/password columns were found. Export again "
                    "from the other browser's password settings.")
    return out, ""
