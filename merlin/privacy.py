"""Routing traffic through Tor, or any other proxy.

What this does: sends Merlin's requests through a SOCKS proxy, with DNS resolved
at the proxy end so lookups do not leak. Pointed at a local Tor daemon, that
means your traffic reaches sites through the Tor network and they see an exit
node's address rather than yours.

What this is not: Tor Browser. Tor Browser does far more than route traffic. It
normalises window size, fonts, timezone, canvas and dozens of other signals so
that its users look alike. Merlin does none of that, so a site can still
fingerprint this browser and potentially recognise it across sessions. Treat it
as "my address is hidden from this site", not as anonymity. For anything where
being identified carries real risk, use Tor Browser.

Chromium applies a proxy per process, not per window, so a Tor window is a
separate Merlin process rather than another window in this one.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys

# Tor's own daemon listens on 9050; Tor Browser's bundled daemon on 9150.
TOR_PORTS = (9050, 9150)
TOR_HOST = "127.0.0.1"


def port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_tor_port() -> int:
    """The first local Tor SOCKS port that accepts a connection, or 0."""
    for port in TOR_PORTS:
        if port_open(TOR_HOST, port):
            return port
    return 0


def tor_available() -> bool:
    return find_tor_port() != 0


def tor_binary() -> str:
    return shutil.which("tor") or ""


def start_tor() -> tuple[bool, str]:
    """Start a local tor daemon if one is installed but not running."""
    if find_tor_port():
        return True, "Tor is already running"
    binary = tor_binary()
    if not binary:
        return False, "no tor daemon installed"
    try:
        subprocess.Popen(
            [binary],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"could not start tor: {exc}"

    # give it a moment to open its listener
    import time

    for _ in range(20):
        time.sleep(0.5)
        if find_tor_port():
            return True, "Tor started"
    return False, "tor was started but its SOCKS port never opened"


def install_hint() -> str:
    if os.name == "nt":
        return ("Install the Tor Expert Bundle, or run Tor Browser alongside "
                "Merlin: its daemon listens on 127.0.0.1:9150 and Merlin will "
                "find it automatically.\n\n"
                "    winget install TorProject.TorBrowser")
    return ("Install Tor and start it:\n\n"
            "    sudo apt install tor && sudo systemctl start tor\n"
            "    sudo dnf install tor && sudo systemctl start tor\n"
            "    sudo pacman -S tor && sudo systemctl start tor\n\n"
            "Or run Tor Browser alongside Merlin; its daemon listens on "
            "127.0.0.1:9150.")


def proxy_flags(proxy_url: str) -> list[str]:
    """Chromium switches for a proxy.

    No --host-resolver-rules here. The rule that would force remote lookups
    contains spaces, and these flags go into QTWEBENGINE_CHROMIUM_FLAGS as one
    space-separated string, so it split into fragments and Chromium saw
    nonsense. A socks5:// proxy already resolves names at the proxy end, which
    is the behaviour that matters.
    """
    if not proxy_url:
        return []
    return [f"--proxy-server={proxy_url}", "--proxy-bypass-list=<-loopback>"]


def tor_proxy_url(port: int = 0) -> str:
    port = port or find_tor_port()
    return f"socks5://{TOR_HOST}:{port}" if port else ""


def launch_tor_window(extra: list[str] | None = None) -> tuple[bool, str]:
    """Start a second Merlin process routed through Tor.

    A new process because Chromium takes its proxy from the command line for
    the whole process; a window inside this one could not have its own.
    """
    port = find_tor_port()
    if not port:
        return False, "Tor is not running"

    run_py = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "merlin-run.py")
    command = [sys.executable]
    command += [run_py] if os.path.isfile(run_py) else ["-m", "merlin"]
    command += ["--tor", "--private", "--profile", "tor"]
    command += extra or []

    try:
        subprocess.Popen(command, start_new_session=True)
    except OSError as exc:
        return False, f"could not start the Tor window: {exc}"
    return True, f"Tor window starting, via 127.0.0.1:{port}"
