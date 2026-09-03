"""Windows taskbar icon, set the blunt way.

Qt's setWindowIcon should be enough, and on most systems it is. It was not here:
the taskbar kept showing the interpreter's icon. Two things were working against
it.

First, an explicit Application User Model ID makes Windows resolve the button's
icon by hunting for a Start Menu shortcut carrying that same ID. `WScript.Shell`,
which is what the installer has available, cannot write `System.AppUserModel.ID`
onto a `.lnk`, so no shortcut ever carried it. With that lookup failing, Windows
falls back to the icon of the executable owning the window, which is
`pythonw.exe`. The ID is therefore opt-in now rather than always-on.

Second, rather than hoping Qt's HICON conversion lands, this module loads the
`.ico` with `LoadImageW` and pushes it onto the window handle with `WM_SETICON`,
which is the message the shell actually reads. Both the large icon (Alt-Tab, the
taskbar) and the small one (title bar) are set, each at the size Windows asks
for, so neither is a blurry rescale of the other.

Everything here is a no-op off Windows.
"""
from __future__ import annotations

import os

WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

SM_CXICON = 11
SM_CYICON = 12
SM_CXSMICON = 49
SM_CYSMICON = 50

# keep the handles alive for the life of the process; if they are garbage
# collected the shell is left pointing at freed icon resources
_handles: list[int] = []


def available() -> bool:
    return os.name == "nt"


def apply_to_window(window, icon_file: str) -> bool:
    """Push icon_file onto a window's handle. True if the shell accepted it."""
    if not available() or not icon_file or not os.path.isfile(icon_file):
        return False
    if not icon_file.lower().endswith(".ico"):
        return False

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = wintypes.LPARAM
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int

        hwnd = int(window.winId())
        if not hwnd:
            return False

        applied = False
        for which, cx_metric, cy_metric in (
            (ICON_BIG, SM_CXICON, SM_CYICON),
            (ICON_SMALL, SM_CXSMICON, SM_CYSMICON),
        ):
            cx = user32.GetSystemMetrics(cx_metric) or 32
            cy = user32.GetSystemMetrics(cy_metric) or 16
            handle = user32.LoadImageW(None, icon_file, IMAGE_ICON, cx, cy,
                                       LR_LOADFROMFILE)
            if not handle:
                continue
            _handles.append(handle)
            user32.SendMessageW(hwnd, WM_SETICON, which, handle)
            applied = True
        return applied
    except Exception:                                    # noqa: BLE001
        return False


def process_image() -> str:
    """The executable that actually hosts this process.

    Not sys.executable. A virtualenv's pythonw.exe on Windows is a redirector
    that starts the base interpreter as a child process, and the child is what
    owns the window. sys.executable still reports the venv path, so it says
    Merlin.exe while the window really belongs to Python's own pythonw.exe,
    which is where the taskbar takes its fallback icon from.
    """
    if os.name != "nt":
        import sys

        return sys.executable
    try:
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(32768)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE,
                                                wintypes.LPWSTR, wintypes.DWORD]
        if kernel32.GetModuleFileNameW(None, buffer, len(buffer)):
            return buffer.value
    except Exception:                                    # noqa: BLE001
        pass
    import sys

    return sys.executable


def is_store_python() -> bool:
    """Is this process a Microsoft Store build of Python?

    Store applications run with MSIX package identity, and Windows takes a
    taskbar button's identity and icon from the package manifest rather than
    from the window. The window icon is then set correctly and ignored, so this
    is worth naming rather than leaving as a mystery.
    """
    return "\\windowsapps\\" in process_image().lower()


def describe(icon_file: str) -> str:
    """One line for --icon-check, so a failure can be seen rather than guessed."""
    if not available():
        return "not Windows; the desktop file and window icon are used instead"
    if not icon_file:
        return "no icon file found next to the package"
    if not os.path.isfile(icon_file):
        return f"icon file missing: {icon_file}"
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        handle = user32.LoadImageW(None, icon_file, IMAGE_ICON, 32, 32,
                                   LR_LOADFROMFILE)
        if handle:
            return f"LoadImageW read {icon_file} successfully"
        error = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        return f"LoadImageW could not read {icon_file} (error {error})"
    except Exception as exc:                             # noqa: BLE001
        return f"icon check failed: {exc}"
