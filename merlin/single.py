"""One window, not one per link.

Opening a link from another application, or running merlin-browser again,
should add a tab to the Merlin already on screen rather than starting a second
browser with its own profile lock and its own taskbar button.

A local socket does this: the first instance listens on a named socket, later
ones connect, hand over their URLs and exit. The name includes the profile and
the user, so separate profiles stay separate and two users on one machine do
not collide.

Private and Tor windows deliberately opt out: the point of them is a separate
session, so they always start their own process.
"""
from __future__ import annotations

import getpass
import os

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from .brand import APP_SLUG

CONNECT_TIMEOUT = 400        # ms to wait for an existing instance to answer
WRITE_TIMEOUT = 1000


def server_name(profile: str = "default") -> str:
    try:
        user = getpass.getuser()
    except Exception:                                    # noqa: BLE001
        user = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return f"{APP_SLUG}-{profile}-{user}"


def hand_off(urls: list[str], profile: str = "default") -> bool:
    """Give our URLs to a running instance. True if one took them."""
    socket = QLocalSocket()
    socket.connectToServer(server_name(profile))
    if not socket.waitForConnected(CONNECT_TIMEOUT):
        return False
    payload = "\n".join(urls) if urls else "\n"
    socket.write(payload.encode("utf-8"))
    socket.waitForBytesWritten(WRITE_TIMEOUT)
    socket.disconnectFromServer()
    return True


class InstanceServer(QObject):
    """Listens for later instances and reports the URLs they were given."""

    urls_received = pyqtSignal(list)

    def __init__(self, profile: str = "default", parent=None):
        super().__init__(parent)
        self.profile = profile
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)

    def listen(self) -> bool:
        name = server_name(self.profile)
        # A crashed instance can leave the socket behind and block binding.
        QLocalServer.removeServer(name)
        return bool(self._server.listen(name))

    def _on_connection(self) -> None:
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        socket.readyRead.connect(lambda s=socket: self._read(s))
        socket.disconnected.connect(socket.deleteLater)

    def _read(self, socket) -> None:
        raw = bytes(socket.readAll()).decode("utf-8", "replace")
        urls = [line.strip() for line in raw.splitlines() if line.strip()]
        self.urls_received.emit(urls)
