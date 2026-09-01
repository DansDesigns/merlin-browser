"""Names, version and icon lookup, kept in one place."""
from __future__ import annotations

APP_NAME = "Merlin"
APP_TAGLINE = "Merlin Browser"
APP_SLUG = "merlin"              # config dirs, desktop file, executable name
APP_SCHEME = "merlin"            # merlin://start
APP_VERSION = "1.5.8"

# Sibling project, referenced in About only.
FAMILY_NOTE = "Part of the same aviary as Kestrel."

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
