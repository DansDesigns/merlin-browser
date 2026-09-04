#!/usr/bin/env python3
"""Entry point for the built Merlin.exe.

The executable carries the interpreter, Qt and the web engine. It does not
carry the merlin package: that is read from disk at run time, so an update is
a matter of replacing .py files rather than rebuilding the executable.

Where the package lives, in order:

  MERLIN_APP_DIR                 set this to override everything
  app-path.txt next to the exe   written by the installer
  ../../app relative to the exe  the normal layout, bin\\Merlin\\Merlin.exe
                                 with the application in app\\
  the folder holding this file   running from a source checkout

If none of them has a merlin package, the window would never appear and the
process would exit silently, so the reason is reported instead.
"""
from __future__ import annotations

import os
import sys

# Imported here, at the top, and never used directly.
#
# PyInstaller decides what to bundle by reading this file's imports. The real
# work happens after sys.path is adjusted, so without these it sees a script
# that imports nothing, bundles no Qt, and produces an executable that cannot
# start. Then the installer falls back to the interpreter and the taskbar icon
# is Python's again.
try:  # noqa: SIM105
    from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets      # noqa: F401
    from PyQt6 import QtWebEngineCore, QtWebEngineWidgets      # noqa: F401
    from PyQt6 import QtMultimedia, QtSvg                      # noqa: F401
except Exception:                                              # noqa: BLE001
    pass


def _candidates() -> list:
    here = os.path.dirname(os.path.abspath(__file__))
    found = []

    override = os.environ.get("MERLIN_APP_DIR", "").strip()
    if override:
        found.append(override)

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        pointer = os.path.join(exe_dir, "app-path.txt")
        try:
            with open(pointer, encoding="utf-8") as handle:
                written = handle.readline().strip()
            if written:
                found.append(written)
        except OSError:
            pass
        # bin\Merlin\Merlin.exe  ->  ..\..\app
        found.append(os.path.abspath(os.path.join(exe_dir, "..", "..", "app")))
        found.append(exe_dir)

    found.append(here)
    return found


def application_dir() -> str:
    for candidate in _candidates():
        if candidate and os.path.isfile(
                os.path.join(candidate, "merlin", "app.py")):
            return candidate
    return ""


def _complain(message: str) -> int:
    """Say what went wrong, on screen if there is no console to print to."""
    sys.stderr.write(message + "\n")
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv[:1])
        QMessageBox.critical(None, "Merlin cannot start", message)
        del app
    except Exception:                                    # noqa: BLE001
        pass
    return 1


def main() -> int:
    folder = application_dir()
    if not folder:
        looked = "\n".join(f"  {path}" for path in _candidates() if path)
        return _complain(
            "Merlin's application files were not found. Looked in:\n"
            + looked
            + "\n\nReinstalling will put them back.")

    if folder not in sys.path:
        sys.path.insert(0, folder)

    try:
        from merlin.app import main as run
    except Exception as exc:                             # noqa: BLE001
        return _complain(
            f"Merlin's application files in\n  {folder}\ncould not be loaded:"
            f"\n  {exc}\n\nIf an update was interrupted, reinstalling will "
            "put them back.")

    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
