#!/usr/bin/env python3
"""Direct entry point.

Launchers point the interpreter straight at this file, which puts its own
folder on sys.path. No PYTHONPATH, no .pth, no site-packages lookup.

It also refuses to fail silently. Under pythonw.exe there is no console, so an
unhandled exception at start-up produces absolutely nothing on screen and the
Start Menu entry looks broken. Anything that goes wrong here is written to a
log file and, on Windows, shown in a message box.
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
sys.path.insert(0, HERE)


def _ensure_streams() -> None:
    """pythonw.exe has no console, and leaves sys.stdout as None.

    Any print() then raises AttributeError, which for a windowed process means
    the app dies with nothing on screen. Point the missing streams at the null
    device so ordinary output is harmless.
    """
    for name in ("stdout", "stderr", "stdin"):
        if getattr(sys, name, None) is None:
            mode = "r" if name == "stdin" else "w"
            try:
                setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))
            except OSError:
                pass


_ensure_streams()


def log_path() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "Merlin")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        folder = os.path.join(base, "merlin")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        folder = os.path.expanduser("~")
    return os.path.join(folder, "startup-error.log")


def report(message: str) -> None:
    path = log_path()
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(message)
    except OSError:
        path = "(could not be written)"

    sys.stderr.write(message + "\n")

    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                f"{message}\n\nSaved to:\n{path}",
                "Merlin could not start",
                0x10,          # MB_ICONERROR
            )
        except Exception:      # noqa: BLE001
            pass


def main() -> int:
    try:
        from merlin.app import main as run
    except Exception:          # noqa: BLE001
        report("Merlin's files could not be loaded.\n\n"
               f"Looked in: {HERE}\n"
               f"Python:    {sys.executable}\n\n"
               + traceback.format_exc())
        return 2

    try:
        return run(sys.argv[1:])
    except SystemExit:
        raise
    except Exception:          # noqa: BLE001
        report("Merlin hit an error while starting.\n\n"
               f"Python: {sys.executable}\n\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
