import json
import os
from pathlib import Path

CONFIG_DIR  = Path.home() / ".config" / "ponder"
CONFIG_FILE = CONFIG_DIR / "config.json"
INDEX_DIR   = CONFIG_DIR / "index"

DEFAULTS = {
    "web_backend":    "ddg",       # ddg | google | brave | serper | searxng
    "brave_api_key":  "",
    "google_api_key": "",
    "google_cx":      "",
    "serper_api_key": "",
    "searxng_url":    "http://localhost:8080",
    "blend_results":  False,
    "show_summary":   True,
    "safe_search":    False,
    "dark_mode":      "light",
    "font_scale":     1.0,
    "font_family":    "",
    "background_image": "none",
    "bg_opacity":      15,
    "host":           "127.0.0.1",
    "port":           7000,
    "open_browser":   True,
    "watch_dirs":     ["~/Documents", "~/Projects"],
    "exclude_patterns": [
        "*.pyc", "*.pyo", ".git", ".svn", "node_modules",
        "__pycache__", "*.jpg", "*.jpeg", "*.png", "*.gif",
        "*.mp4", "*.mkv", "*.mp3", "*.zip", "*.tar", "*.gz",
        "*.iso", "*.bin", "*.exe", "*.so", "*.o"
    ],
    "max_file_mb":    10,
    # ── update & donate ───────────────────────────
    "github_repo":   "https://github.com/DansDesigns/Ponder",
    "donate_url":    "https://alternitech.square.site/product/donation/6",
}


class Config:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        self._data = {**DEFAULTS}
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text())
                self._data.update(saved)
            except Exception:
                pass  # fall back to defaults silently

    def save(self):
        CONFIG_FILE.write_text(json.dumps(self._data, indent=2))

    def update(self, data: dict):
        for k, v in data.items():
            if k in self._data:
                self._data[k] = v

    def to_dict(self) -> dict:
        return {**self._data}

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(key)
