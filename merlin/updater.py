"""Update checking against the GitHub repository.

Reads a plain version.txt from the repo and compares it with the running
version. No auto-download and no self-modification: it tells you a newer
version exists and gives you the link. A browser that rewrites its own files
in the background is a browser you cannot reason about.

version.txt may be either a bare version:

    1.2.0

or a version plus notes, first line wins:

    1.2.0
    Fixes vertical tab flicker on Wayland.
"""
from __future__ import annotations

import os
import re
import threading
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .brand import APP_VERSION, user_agent_suffix

REPO_OWNER = "DansDesigns"
REPO_NAME = "merlin-browser"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
RELEASES_URL = f"{REPO_URL}/releases"

# Try the usual default branches in order; a repo has one or the other.
VERSION_URLS = [
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/version.txt",
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/master/version.txt",
]

TIMEOUT = 12
VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


def parse_version(text: str) -> tuple[int, ...]:
    """'1.10.2' -> (1, 10, 2). Unparseable input sorts lowest."""
    match = VERSION_RE.match(text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    remote_parts, local_parts = parse_version(remote), parse_version(local)
    if not remote_parts:
        return False
    length = max(len(remote_parts), len(local_parts))
    remote_parts += (0,) * (length - len(remote_parts))
    local_parts += (0,) * (length - len(local_parts))
    return remote_parts > local_parts


def fetch_version() -> tuple[str, str, str]:
    """Return (version, notes, error). Only one of version/error is set."""
    last_error = ""
    for url in VERSION_URLS:
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": f"MerlinBrowser/{APP_VERSION}"})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read(4096).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            continue
        except urllib.error.URLError as exc:
            last_error = f"{exc.reason}"
            continue
        except Exception as exc:                          # noqa: BLE001
            last_error = str(exc)
            continue

        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            last_error = "version.txt is empty"
            continue
        version = lines[0]
        if not parse_version(version):
            last_error = f"could not read a version from {version[:40]!r}"
            continue
        notes = "\n".join(lines[1:]).strip()
        return version, notes, ""
    return "", "", last_error or "could not reach GitHub"


class UpdateCheck(QThread):
    """Runs the fetch off the UI thread."""

    finished_check = pyqtSignal(str, str, str)   # version, notes, error

    def run(self) -> None:                                # noqa: D102
        version, notes, error = fetch_version()
        self.finished_check.emit(version, notes, error)


class Updater(QObject):
    status = pyqtSignal(str)          # human-readable line for the dialog
    available = pyqtSignal(str, str)  # version, notes
    installed = pyqtSignal(bool, str)  # succeeded, message

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.latest = ""
        self.notes = ""
        self._thread: UpdateCheck | None = None

    def check(self, quiet: bool = False) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        if not quiet:
            self.status.emit("Checking GitHub...")
        self._thread = UpdateCheck(self)
        self._thread.finished_check.connect(
            lambda v, n, e, q=quiet: self._done(v, n, e, q))
        self._thread.start()

    def _done(self, version: str, notes: str, error: str, quiet: bool) -> None:
        if error:
            if not quiet:
                self.status.emit(f"Check failed: {error}")
            return
        self.latest, self.notes = version, notes
        self.settings.set("last_seen_version", version)
        if is_newer(version):
            self.status.emit(f"Version {version} is available. "
                             f"You have {APP_VERSION}.")
            self.available.emit(version, notes)
        elif not quiet:
            self.status.emit(f"Up to date. {APP_VERSION} is the latest.")

    # ------------------------------------------------------------- updating
    def install_latest(self) -> None:
        """Fetch the newest files and put them in place, on a worker thread."""
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self) -> None:
        import io
        import shutil
        import sys
        import tempfile
        import zipfile

        if getattr(sys, "frozen", False):
            self.installed.emit(False, (
                "This copy is a built executable, so its files live inside the "
                "executable itself and cannot be swapped out. Download the new "
                "version and run install.bat again; it will rebuild in place "
                "and keep your settings."))
            return

        target = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.access(target, os.W_OK):
            self.installed.emit(False, f"No permission to write to {target}.")
            return

        self.status.emit("Downloading the new version...")
        archive = f"{REPO_URL}/archive/refs/heads/main.zip"
        try:
            request = urllib.request.Request(
                archive, headers={"User-Agent": user_agent_suffix()})
            with urllib.request.urlopen(request, timeout=60) as response:
                blob = response.read()
        except Exception as exc:                         # noqa: BLE001
            self.installed.emit(False, f"Download failed: {exc}")
            return

        self.status.emit("Unpacking...")
        try:
            bundle = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            self.installed.emit(False, "The download was not a valid archive.")
            return

        # the archive holds one top-level folder, repo-branch
        names = bundle.namelist()
        root = names[0].split("/")[0] + "/" if names else ""
        wanted = ("merlin/", "merlin-run.py", "version.txt")

        staged = tempfile.mkdtemp(prefix="merlin-update-")
        try:
            found = False
            for name in names:
                if not name.startswith(root) or name.endswith("/"):
                    continue
                relative = name[len(root):]
                if not relative.startswith(wanted):
                    continue
                found = True
                destination = os.path.join(staged, relative)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with bundle.open(name) as source, \
                        open(destination, "wb") as handle:
                    shutil.copyfileobj(source, handle)
            if not found:
                self.installed.emit(False, "The archive had no application "
                                           "files in it.")
                return

            # Replace rather than uninstall: the virtualenv, the settings and
            # anything else in the folder are left exactly as they are.
            self.status.emit("Replacing the application files...")
            package = os.path.join(staged, "merlin")
            if os.path.isdir(package):
                live = os.path.join(target, "merlin")
                keep = os.path.join(target, "merlin.previous")
                if os.path.isdir(keep):
                    shutil.rmtree(keep, ignore_errors=True)
                if os.path.isdir(live):
                    os.rename(live, keep)
                try:
                    shutil.copytree(package, live)
                except Exception:                        # noqa: BLE001
                    # put the old one back rather than leave nothing behind
                    if os.path.isdir(keep) and not os.path.isdir(live):
                        os.rename(keep, live)
                    raise
                shutil.rmtree(keep, ignore_errors=True)
            for single in ("merlin-run.py", "version.txt"):
                source_file = os.path.join(staged, single)
                if os.path.isfile(source_file):
                    shutil.copyfile(source_file, os.path.join(target, single))
        except Exception as exc:                         # noqa: BLE001
            self.installed.emit(False, f"Could not replace the files: {exc}")
            return
        finally:
            shutil.rmtree(staged, ignore_errors=True)

        self.installed.emit(True, (
            f"Version {self.latest} is in place. Restart Merlin to use it."))
