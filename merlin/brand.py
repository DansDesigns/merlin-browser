"""Names, version and icon lookup, kept in one place."""
from __future__ import annotations

APP_NAME = "Merlin"
APP_TAGLINE = "Merlin Browser"
APP_SLUG = "merlin"              # config dirs, desktop file, executable name
APP_SCHEME = "merlin"            # merlin://start
def _read_version() -> str:
    """The version, read from version.txt.

    One source of truth. It used to be written here as well, so the installer
    reporting version.txt and the browser reporting this constant disagreed
    whenever one was updated without the other.

    version.txt is looked for beside the package and one level up, which covers
    both a source checkout and an install where it sits next to the app.
    """
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here, os.path.dirname(here)):
        try:
            with open(os.path.join(folder, "version.txt"), "r",
                      encoding="utf-8") as handle:
                first = handle.readline().strip()
            if first:
                return first.split()[0]
        except OSError:
            continue
    return "0.0.0"


APP_VERSION = _read_version()

# Sibling project, referenced in About only.

START_URLS = (
    f"{APP_SCHEME}://start",
    f"{APP_SCHEME}://newtab",
    "about:newtab",
)


def icon_path() -> str:
    """Absolute path to the logo, or an empty string.

    The icon files live INSIDE the package, next to this module. They used to
    sit at the top level of the source tree, which worked when running from a
    checkout and failed after installation, because neither installer copied
    them alongside the code. A null icon meant setWindowIcon was never called,
    so Windows fell back to showing the interpreter's own icon in the taskbar.
    Keeping them in the package means they travel with it however it is copied.
    """
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    roots = (here, os.path.dirname(here), os.getcwd())
    for root in roots:
        for name in (f"{APP_SLUG}.ico", f"{APP_SLUG}.png", f"{APP_SLUG}.svg"):
            path = os.path.join(root, name)
            if os.path.isfile(path):
                return path
    return ""


def app_icon():
    from PyQt6.QtGui import QIcon

    path = icon_path()
    if path:
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    return QIcon.fromTheme(f"{APP_SLUG}-browser")


def user_agent_suffix() -> str:
    return f"{APP_NAME}/{APP_VERSION}"
