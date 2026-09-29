"""Crash logging.

A segmentation fault or an abort inside the engine leaves nothing behind: the
window disappears and there is no exception to catch. faulthandler writes the
Python stack at that moment, which is usually enough to name the call that was
in flight.

This lives in the package rather than in merlin-run.py. That script is frozen
into Merlin.exe when it is built, so anything in it only changes on a rebuild,
and an update from Settings never rebuilds. Here, an update carries it.
"""
from __future__ import annotations

import os

_HANDLE = None

# .txt so Windows opens it on a double click; the name is the one that matters
LOG_NAME = "merlin-log.txt"


def _code_location() -> str:
    """Which copy of the application is running: disk, or inside the exe.

    Written into the log on every start, so the file answers the question
    whether or not anything crashes.
    """
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle and here.startswith(os.path.abspath(bundle)):
        return f"{here}  (INSIDE Merlin.exe, so updates on disk are not running)"
    return f"{here}  (on disk, so updates take effect)"


def _version() -> str:
    try:
        from .brand import APP_VERSION

        return APP_VERSION
    except Exception:          # noqa: BLE001
        return "unknown"


def crash_log_candidates() -> list:
    """Where the crash log could go, best first.

    Documents comes first on Windows because it is somewhere findable and
    reliably writable for the signed-in user. The application data folder is
    tried next, then the temporary folder, which always works.
    """
    places = []
    override = os.environ.get("MERLIN_CRASH_LOG", "").strip()
    if override:
        places.append(os.path.dirname(override) or override)

    home = os.path.expanduser("~")
    if os.name == "nt":
        documents = os.path.join(home, "Documents")
        if not os.path.isdir(documents):
            documents = os.path.join(home, "OneDrive", "Documents")
        places.append(os.path.join(documents, "Merlin"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            places.append(os.path.join(local, "Merlin"))
    else:
        state = os.environ.get("XDG_STATE_HOME") or os.path.join(
            home, ".local", "state")
        places.append(os.path.join(state, "merlin"))
        places.append(os.path.join(home, "Merlin"))

    import tempfile

    places.append(os.path.join(tempfile.gettempdir(), "Merlin"))
    return places


def enable() -> str:
    """Write a stack trace to a file if the process is killed outright.

    A segmentation fault or an abort inside the engine leaves nothing behind:
    the window disappears and there is no exception to catch. faulthandler
    writes the Python side of the stack at that moment.

    Each candidate folder is tried by actually writing to it. Creating the
    folder can succeed where writing a file cannot, so only a real write
    proves the location is usable.
    """
    try:
        import datetime
        import faulthandler
    except Exception:          # noqa: BLE001
        return ""

    for folder in crash_log_candidates():
        # one log for everything Merlin writes: starts, which copy is running,
        # and the stack if it is ever killed outright
        path = os.path.join(folder, LOG_NAME)
        try:
            os.makedirs(folder, exist_ok=True)
            handle = open(path, "a", encoding="utf-8", buffering=1)
            handle.write(f"\n--- Merlin started {datetime.datetime.now()} ---\n")
            handle.write(f"code from : {_code_location()}\n")
            handle.write(f"version   : {_version()}\n")
            handle.flush()
        except Exception:      # noqa: BLE001
            continue
        try:
            faulthandler.enable(file=handle, all_threads=True)
        except Exception:      # noqa: BLE001
            handle.close()
            continue
        global _HANDLE
        _HANDLE = handle          # kept alive for the life of the process
        install_excepthook()
        os.environ["MERLIN_CRASH_LOG"] = path
        return path
    return ""


def install_excepthook() -> None:
    """Log an error in a Qt slot instead of letting it end the process.

    PyQt6 aborts the whole application when a Python exception escapes a
    slot or a timer callback, but only while the default exception hook is in
    place. With this one, a bug in, say, a favicon handler is written to
    merlin-log.txt and the browser carries on, rather than taking every window
    and tab down with it.
    """
    import datetime
    import sys
    import traceback

    def hook(kind, value, trace):
        text = "".join(traceback.format_exception(kind, value, trace))
        try:
            sys.__stderr__.write(text)
        except Exception:                                # noqa: BLE001
            pass
        if _HANDLE is not None:
            try:
                _HANDLE.write(f"\n--- error {datetime.datetime.now()} ---\n")
                _HANDLE.write(text)
                _HANDLE.flush()
            except Exception:                            # noqa: BLE001
                pass

    sys.excepthook = hook


def note(text: str) -> None:
    """A timestamped line in merlin-log.txt, for things worth timing."""
    import datetime

    if _HANDLE is None:
        return
    try:
        _HANDLE.write(f"{datetime.datetime.now():%H:%M:%S.%f} {text}\n")
        _HANDLE.flush()
    except Exception:                                    # noqa: BLE001
        pass


_WATCH = None
FREEZE_SECONDS = 20


def watch_for_freezes(app) -> None:
    """Write every thread's stack to the log if Merlin stops responding.

    A freeze leaves no trace otherwise: someone ends Merlin, and the log just
    stops, as it did for 1.7.1. While the event loop runs, a timer re-arms
    faulthandler every second; if the loop stalls for FREEZE_SECONDS, the
    stacks of all threads go into merlin-log.txt, showing where it is stuck.
    Merlin is not ended, and the stacks are written once for each freeze.
    """
    global _WATCH
    if _HANDLE is None or _WATCH is not None:
        return
    try:
        import faulthandler

        from PyQt6.QtCore import QTimer
    except Exception:          # noqa: BLE001
        return

    def rearm() -> None:
        try:
            faulthandler.dump_traceback_later(FREEZE_SECONDS, repeat=False, file=_HANDLE,
                                              exit=False)
        except Exception:      # noqa: BLE001
            pass

    _WATCH = QTimer(app)
    _WATCH.setInterval(1000)
    _WATCH.timeout.connect(rearm)
    _WATCH.start()
    rearm()


def stop_watching() -> None:
    """At shutdown: no freeze report for the time it takes to exit."""
    global _WATCH
    try:
        import faulthandler

        faulthandler.cancel_dump_traceback_later()
    except Exception:          # noqa: BLE001
        pass
    if _WATCH is not None:
        try:
            _WATCH.stop()
        except Exception:      # noqa: BLE001
            pass
        _WATCH = None
