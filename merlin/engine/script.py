"""JavaScript for Merlin Engine: a Deno (V8) process for each tab, where allowed.

The page's DOM and scripts live in that process (js/host.js), run by Deno with
no permissions: no files, no network, no environment. It may import modules
only from the page's own hosts. Everything else the page asks for (fetch,
XMLHttpRequest, scripts) is done here by Merlin, with the page's cookies,
through the content blocker. After scripts change the page, the new page comes
back and Merlin Engine styles and lays it out; clicks, typing and scrolling go
to the page's scripts as events.

JavaScript is off unless a site is allowed, in Settings or when a page asks.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from PyQt6.QtCore import QObject, pyqtSignal

HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "host.js")
MEMORY_MB = 2048


def import_map(markup: str, page_url: str):
    """The page's <script type="importmap">, its addresses made whole; or None.

    GitHub's modules import bare names such as react/jsx-runtime, which the
    page's import map turns into addresses; without it they could not load.
    """
    found = re.findall(r"""<script\b[^>]*type\s*=\s*["']?importmap["']?[^>]*>(.*?)</script>""",
                       markup or "", re.I | re.S)
    if not found:
        return None
    merged = {"imports": {}, "scopes": {}}
    for text in found:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        for name, target in (data.get("imports") or {}).items():
            merged["imports"][name] = urllib.parse.urljoin(page_url, target)
        for scope, mapping in (data.get("scopes") or {}).items():
            merged["scopes"][urllib.parse.urljoin(page_url, scope)] = {
                name: urllib.parse.urljoin(page_url, target) for name, target in (mapping or {}).items()}
    return merged if merged["imports"] or merged["scopes"] else None


def import_hosts(markup: str, page_url: str) -> list:
    """The hosts a page's modules may be imported from: its own, and those its
    module scripts and module preloads name."""
    hosts = set()
    page = urllib.parse.urlsplit(page_url)
    if page.netloc:
        hosts.add(page.netloc)
    mapped = import_map(markup, page_url) or {}
    for target in list((mapped.get("imports") or {}).values()) + [
            t for scope in (mapped.get("scopes") or {}).values() for t in scope.values()]:
        host = urllib.parse.urlsplit(target).netloc
        if host:
            hosts.add(host)
    for tag in re.findall(r"<(?:script|link)\b[^>]*>", markup or "", re.I):
        if not re.search(r"""type\s*=\s*["']?module|rel\s*=\s*["']?modulepreload""", tag, re.I):
            continue
        found = re.search(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", tag, re.I)
        if found:
            host = urllib.parse.urlsplit(urllib.parse.urljoin(page_url, found.group(1))).netloc
            if host:
                hosts.add(host)
    return sorted(hosts)


class ScriptHost(QObject):
    """One page's scripts, in a Deno process of their own."""

    message = pyqtSignal(object)          # a dict from the page's scripts
    ended = pyqtSignal()

    def __init__(self, deno: str, hosts: list, cache_dir: str, parent=None, mapping=None):
        super().__init__(parent)
        command = [deno, "run", "--quiet", "--no-prompt", "--no-config", "--no-lock",
                   f"--v8-flags=--max-old-space-size={MEMORY_MB}"]
        if hosts:
            command.append("--allow-import=" + ",".join(hosts))
        if mapping:
            os.makedirs(cache_dir, exist_ok=True)
            import tempfile

            handle, path = tempfile.mkstemp(prefix="importmap-", suffix=".json", dir=cache_dir)
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(mapping, out)
            command.append("--import-map=" + path)
        command.append(HOST)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000               # no console window
        os.makedirs(cache_dir, exist_ok=True)
        env = dict(os.environ, DENO_DIR=cache_dir, NO_COLOR="1", DENO_NO_UPDATE_CHECK="1")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, **kwargs)
        # Deno's own messages, should it fail: the last of them, for the log
        from collections import deque

        self.errors = deque(maxlen=30)
        threading.Thread(target=self._read_errors, daemon=True).start()
        self._lock = threading.Lock()
        self._closed = False
        # the process goes with its tab: when the view is deleted, this is too,
        # and a Deno process had been left running for every tab closed
        process = self.process
        self.destroyed.connect(lambda *_a: _kill(process))
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            try:
                self.message.emit(message)
            except RuntimeError:
                return                                   # the tab was closed
        try:
            self.ended.emit()
        except RuntimeError:
            pass

    def _read_errors(self) -> None:
        for line in self.process.stderr:
            self.errors.append(line.decode("utf-8", "replace").rstrip())

    def why_it_ended(self) -> str:
        """The process's exit code and its last words, once it has ended."""
        try:
            code = self.process.wait(timeout=2)
        except Exception:                                  # noqa: BLE001
            code = None
        tail = " | ".join(line for line in list(self.errors)[-6:] if line.strip())
        return f"code {code}" + (f": {tail[:600]}" if tail else "")

    def send(self, message: dict) -> None:
        if self._closed:
            return
        data = (json.dumps(message) + "\n").encode("utf-8")
        with self._lock:
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._closed = True

    def stop(self) -> None:
        self._closed = True
        try:
            self.process.kill()
        except Exception:                                  # noqa: BLE001
            pass


def _kill(process) -> None:
    try:
        process.kill()
    except Exception:                                      # noqa: BLE001
        pass


def page_fetch(url: str, method: str, headers: dict, body, opener=None, timeout: int = 20) -> dict:
    """A request made for a page's script: status, headers and body, as fetch() wants.

    An HTTP error is an answer like any other, as in a browser; only a failure
    to reach the server is an error.
    """
    data = base64.b64decode(body) if body else None
    sent = {k: v for k, v in (headers or {}).items()
            if k.lower() not in ("host", "content-length", "accept-encoding")}
    sent["Accept-Encoding"] = "gzip, deflate"          # what Merlin can unpack
    request = urllib.request.Request(url, data=data, method=method or "GET", headers=sent)
    try:
        from . import chromelike

        if opener is None and chromelike.usable():
            opener = chromelike.ChromeOpener()              # as Chrome connects
        response = (opener.open(request, timeout=timeout) if opener is not None
                    else urllib.request.urlopen(request, timeout=timeout))
    except urllib.error.HTTPError as error:
        response = error
    except Exception as exc:                               # noqa: BLE001
        return {"error": str(exc)}
    with response:
        from .view import _body

        raw = _body(response, 32 * 1024 * 1024)
        answer_headers = {k.lower(): v for k, v in response.headers.items()
                          if k.lower() not in ("content-encoding", "content-length", "set-cookie")}
        return {"status": getattr(response, "status", None) or response.getcode() or 200,
                "statusText": getattr(response, "reason", "") or "",
                "headers": answer_headers, "url": response.geturl(),
                "body": base64.b64encode(raw).decode("ascii")}


def cookie_string(jar, url: str) -> str:
    """document.cookie for url: the cookies a script may see, not HttpOnly ones."""
    if jar is None:
        return ""
    parts = urllib.parse.urlsplit(url)
    pairs = []
    for cookie in jar:
        if cookie.has_nonstandard_attr("HttpOnly") or cookie.has_nonstandard_attr("httponly"):
            continue
        domain = cookie.domain.lstrip(".")
        if not (parts.hostname == domain or (parts.hostname or "").endswith("." + domain)):
            continue
        if not parts.path.startswith(cookie.path or "/"):
            continue
        if cookie.secure and parts.scheme != "https":
            continue
        pairs.append(f"{cookie.name}={cookie.value}")
    return "; ".join(pairs)


def set_cookie(jar, url: str, header: str) -> None:
    """A cookie set by document.cookie, into the tab's cookie jar."""
    if jar is None:
        return
    from email.message import Message

    class _Response:
        def __init__(self, value):
            self._message = Message()
            self._message["Set-Cookie"] = value

        def info(self):
            return self._message

    request = urllib.request.Request(url)
    for cookie in jar.make_cookies(_Response(header), request):
        jar.set_cookie(cookie)


class LocalStorage:
    """localStorage, kept for each site between sessions (not in private windows)."""

    def __init__(self, folder: str | None):
        self.folder = folder
        self._memory: dict = {}

    def _path(self, origin: str) -> str | None:
        if not self.folder:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", origin)[:120]
        return os.path.join(self.folder, safe + ".json")

    def load(self, origin: str) -> dict:
        if origin in self._memory:
            return dict(self._memory[origin])
        path = self._path(origin)
        items = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    items = json.load(handle)
            except (OSError, ValueError):
                items = {}
        self._memory[origin] = items
        return dict(items)

    def save(self, origin: str, items: dict) -> None:
        self._memory[origin] = dict(items)
        path = self._path(origin)
        if not path:
            return
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(path + ".part", "w", encoding="utf-8") as handle:
                json.dump(items, handle)
            os.replace(path + ".part", path)
        except OSError:
            pass
