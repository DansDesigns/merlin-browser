"""History and bookmark storage. Pure stdlib (sqlite3 + json)."""
from __future__ import annotations

import json
import os
import sqlite3
import time

from . import settings as cfg


class History:
    def __init__(self, path: str = cfg.HISTORY_DB):
        cfg.ensure_dirs()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS visits(
                   id INTEGER PRIMARY KEY,
                   url TEXT NOT NULL,
                   title TEXT,
                   visited_at REAL NOT NULL)"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON visits(url)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_time ON visits(visited_at)")
        self.conn.commit()

    def add(self, url: str, title: str = "") -> None:
        if not url or url.startswith(("merlin://", "about:", "data:")):
            return
        try:
            self.conn.execute(
                "INSERT INTO visits(url,title,visited_at) VALUES(?,?,?)",
                (url, title or "", time.time()),
            )
            self.conn.commit()
        except sqlite3.Error:
            pass

    def update_title(self, url: str, title: str) -> None:
        if not url or not title:
            return
        try:
            self.conn.execute(
                "UPDATE visits SET title=? WHERE url=? AND (title IS NULL OR title='')",
                (title, url),
            )
            self.conn.commit()
        except sqlite3.Error:
            pass

    def recent(self, limit: int = 300) -> list[tuple[str, str, float]]:
        try:
            cur = self.conn.execute(
                """SELECT url, MAX(title), MAX(visited_at) AS t FROM visits
                   GROUP BY url ORDER BY t DESC LIMIT ?""",
                (limit,),
            )
            return cur.fetchall()
        except sqlite3.Error:
            return []

    def suggestions(self, limit: int = 2000) -> list[str]:
        try:
            cur = self.conn.execute(
                """SELECT url FROM visits GROUP BY url
                   ORDER BY COUNT(*) DESC, MAX(visited_at) DESC LIMIT ?""",
                (limit,),
            )
            return [row[0] for row in cur.fetchall()]
        except sqlite3.Error:
            return []

    def search(self, term: str, limit: int = 200):
        like = f"%{term}%"
        try:
            cur = self.conn.execute(
                """SELECT url, MAX(title), MAX(visited_at) AS t FROM visits
                   WHERE url LIKE ? OR title LIKE ?
                   GROUP BY url ORDER BY t DESC LIMIT ?""",
                (like, like, limit),
            )
            return cur.fetchall()
        except sqlite3.Error:
            return []

    def clear(self) -> None:
        try:
            self.conn.execute("DELETE FROM visits")
            self.conn.commit()
            self.conn.execute("VACUUM")
        except sqlite3.Error:
            pass


class Bookmarks:
    def __init__(self, path: str = cfg.BOOKMARKS_FILE):
        self.path = path
        self.items: list[dict] = []
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self.items = [d for d in data if isinstance(d, dict) and d.get("url")]
        except (OSError, ValueError):
            self.items = []

    def save(self) -> None:
        cfg.ensure_dirs()
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, indent=2)
        except OSError:
            pass

    def contains(self, url: str) -> bool:
        return any(item.get("url") == url for item in self.items)

    def add(self, url: str, title: str = "") -> None:
        if not url or self.contains(url):
            return
        self.items.append({"url": url, "title": title or url, "added": time.time()})
        self.save()

    def remove(self, url: str) -> None:
        before = len(self.items)
        self.items = [i for i in self.items if i.get("url") != url]
        if len(self.items) != before:
            self.save()

    def toggle(self, url: str, title: str = "") -> bool:
        if self.contains(url):
            self.remove(url)
            return False
        self.add(url, title)
        return True
