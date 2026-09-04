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



# ---------------------------------------------------------------- updating
# Merlin.exe is built with the merlin package inside it. That is what makes it
# a real application which owns its window, and therefore shows its own icon.
#
# It would normally also mean an update needs a rebuild. It does not: this
# prefers a copy of the package found on disk beside the executable, so an
# update replaces .py files there and takes effect next start. If they are
# missing or broken the bundled copy is used, so an interrupted update cannot
# stop Merlin starting.
def _app_dir_on_disk() -> str:
    if not getattr(sys, "frozen", False):
        return ""
    here = os.path.dirname(os.path.abspath(sys.executable))
    places = []
    override = os.environ.get("MERLIN_APP_DIR", "").strip()
    if override:
        places.append(override)
    try:
        with open(os.path.join(here, "app-path.txt"), encoding="utf-8") as handle:
            written = handle.readline().strip()
        if written:
            places.append(written)
    except OSError:
        pass
    places.append(os.path.abspath(os.path.join(here, "..", "..", "app")))
    for place in places:
        if place and os.path.isfile(os.path.join(place, "merlin", "app.py")):
            return place
    return ""


class _DiskFirst:
    """Load the merlin package from disk instead of from the bundle.

    Sits at the front of sys.meta_path, ahead of PyInstaller's own importer,
    which would otherwise always win.
    """

    def __init__(self, root: str):
        self.root = root

    def find_spec(self, name, path=None, target=None):
        import importlib.util

        if name != "merlin" and not name.startswith("merlin."):
            return None
        base = os.path.join(self.root, *name.split("."))
        init = os.path.join(base, "__init__.py")
        if os.path.isfile(init):
            return importlib.util.spec_from_file_location(
                name, init, submodule_search_locations=[base])
        single = base + ".py"
        if os.path.isfile(single):
            return importlib.util.spec_from_file_location(name, single)
        return None


def prefer_disk_copy() -> str:
    root = _app_dir_on_disk()
    if not root:
        return ""
    try:
        sys.meta_path.insert(0, _DiskFirst(root))
    except Exception:          # noqa: BLE001
        return ""
    return root


def main() -> int:
    prefer_disk_copy()
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
