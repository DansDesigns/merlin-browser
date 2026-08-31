"""Merlin Browser entry point.

Chromium switches and custom URL schemes must be configured before the
QApplication and the first QWebEngineProfile exist, so the import order in
this module matters.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import settings as cfg


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="merlin-browser",
        description="Merlin - a Chromium-engine browser in Python/C++ with a "
                    "window-decoration toggle.",
    )
    decoration = parser.add_mutually_exclusive_group()
    decoration.add_argument(
        "--no-decorations", dest="decorations", action="store_false", default=None,
        help="start with the system title bar hidden (frameless)",
    )
    decoration.add_argument(
        "--decorations", dest="decorations", action="store_true", default=None,
        help="start with the system title bar shown",
    )
    parser.add_argument(
        "--persist-decorations", action="store_true",
        help="also write the --decorations/--no-decorations choice to settings",
    )
    parser.add_argument("--private", action="store_true",
                        help="open an off-the-record window")
    parser.add_argument("--app", metavar="URL",
                        help="open URL in a frameless single-purpose window")
    parser.add_argument("--profile", metavar="NAME", default="merlin",
                        help="named storage profile (default: merlin)")
    parser.add_argument("--icon-check", action="store_true",
                        help="report how the application icon resolves and exit")
    parser.add_argument("--timings", action="store_true",
                        help="print how long each start-up phase takes")
    parser.add_argument("--version", action="store_true",
                        help="print the version and exit")
    parser.add_argument("--codecs", action="store_true",
                        help="print the engine's codec support and exit")
    parser.add_argument("urls", nargs="*", help="URLs to open")
    return parser.parse_args(argv)


def set_windows_app_id() -> None:
    """Claim a distinct taskbar identity. Off unless asked for.

    An explicit Application User Model ID makes Windows resolve a taskbar
    button's icon by looking for a Start Menu shortcut carrying the same ID,
    and nothing available to the installer can write that ID onto a .lnk. The
    lookup fails and Windows falls back to the icon of the executable owning
    the window.

    That fallback is now harmless-to-helpful, because the installer gives
    Merlin its own Merlin.exe rather than running under pythonw.exe, so the
    process already has an identity of its own to group and pin by. Setting an
    ID on top of that only reintroduces the failed lookup.

    Set MERLIN_APP_ID=1 if you want an explicit one anyway.
    """
    if os.name != "nt" or os.environ.get("MERLIN_APP_ID") != "1":
        return
    try:
        import ctypes
        from ctypes import wintypes

        from .brand import APP_NAME, APP_VERSION

        app_id = f"DansDesigns.{APP_NAME}.Browser.{APP_VERSION.split('.')[0]}"
        func = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        func.argtypes = [wintypes.LPCWSTR]
        func.restype = ctypes.HRESULT
        func(app_id)
    except Exception:                                    # noqa: BLE001
        pass


def run_icon_check() -> int:
    """Report every step of icon resolution, so a failure is visible.

    Guessing which of the three mechanisms is broken has cost several rounds
    already. This runs the real path on a real window and prints what each part
    returned.
    """
    from . import winicon
    from .brand import APP_NAME, APP_VERSION, icon_path

    path = icon_path()
    print(f"{APP_NAME} {APP_VERSION} icon check")
    print("=" * 52)
    print("Icon file      :", path or "NOT FOUND next to the package")
    print("Platform       :", sys.platform, f"({os.name})")
    print("Running as     :", os.path.basename(sys.executable))
    print("Interpreter    :", sys.executable)

    if path:
        import struct

        with open(path, "rb") as fh:
            data = fh.read()
        try:
            count = struct.unpack("<H", data[4:6])[0]
            kinds = []
            offset = 6
            for _ in range(count):
                entry = struct.unpack("<BBBBHHII", data[offset:offset + 16])
                offset += 16
                blob = data[entry[7]:entry[7] + 8]
                png = blob == b"\x89PNG\r\n\x1a\n"
                kinds.append(f"{entry[0] or 256}{'png' if png else 'dib'}")
            print("Icon entries   :", ", ".join(kinds))
        except Exception as exc:                         # noqa: BLE001
            print("Icon entries   : could not parse:", exc)

    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication, QWidget

    app = QApplication([sys.argv[0]])
    if path:
        sizes = [s.width() for s in QIcon(path).availableSizes()]
        print("Qt can read    :", sizes or "NOTHING, Qt could not decode it")

    if os.name == "nt":
        print("App ID         :", "set" if os.environ.get("MERLIN_APP_ID") != "0"
              else "skipped by MERLIN_APP_ID=0")
        probe = QWidget()
        probe.setWindowTitle("Merlin icon check")
        probe.resize(200, 100)
        probe.show()
        app.processEvents()
        ok = winicon.apply_to_window(probe, path)
        print("WM_SETICON     :", "accepted" if ok else "FAILED")
        print("LoadImageW     :", winicon.describe(path))
        probe.close()
    else:
        print("Note           :", winicon.describe(path))
    return 0


def build_chromium_flags(settings: cfg.Settings) -> str:
    flags = [
        "--enable-features=WebRTCPipeWireCapturer,OverlayScrollbar",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "--autoplay-policy=user-gesture-required",
        "--disable-breakpad",
        "--no-pings",
    ]
    if settings.get("block_webrtc_leak"):
        flags.append("--force-webrtc-ip-handling-policy=default_public_interface_only")
    extra = (settings.get("chromium_flags") or "").strip()
    if extra:
        flags.append(extra)
    existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    if existing:
        flags.append(existing)
    return " ".join(flags)


def main(argv: list[str] | None = None) -> int:
    import time

    started = time.perf_counter()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)

    def mark(label: str) -> None:
        if getattr(args, "timings", False):
            print(f"  {label:<32} {time.perf_counter() - started:6.2f} s",
                  flush=True)

    if args.version:
        from .brand import APP_NAME, APP_VERSION

        print(f"{APP_NAME} Browser {APP_VERSION}")
        return 0

    if (sys.platform.startswith("linux")
            and not os.environ.get("DISPLAY")
            and not os.environ.get("WAYLAND_DISPLAY")
            and not os.environ.get("QT_QPA_PLATFORM")):
        print("No display server detected; Merlin needs X11 or Wayland.",
              file=sys.stderr)
        return 1

    if args.version:
        from .brand import APP_NAME, APP_VERSION

        print(f"{APP_NAME} Browser {APP_VERSION}")
        return 0

    if args.icon_check:
        set_windows_app_id()
        return run_icon_check()

    set_windows_app_id()

    if args.codecs:
        from . import media

        return media.main_standalone()

    cfg.ensure_dirs()
    settings = cfg.Settings()

    # --- must happen before QtWebEngine spins up -------------------------
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = build_chromium_flags(settings)
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWebEngineCore import QWebEngineProfile
    # QtWebEngineWidgets must be imported before the QApplication exists
    from PyQt6 import QtWebEngineWidgets  # noqa: F401
    from PyQt6.QtWidgets import QApplication

    mark("Qt imported")
    app = QApplication([sys.argv[0]])
    mark("QApplication created")
    app.setApplicationName("Merlin Browser")
    app.setApplicationDisplayName("Merlin")
    app.setDesktopFileName("merlin-browser")

    from .brand import app_icon, icon_path

    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
        mark(f"window icon from {os.path.basename(icon_path())}")
    else:
        print("Merlin: logo file not found next to the package; the taskbar "
              "will fall back to the interpreter icon.", file=sys.stderr)
    app.setOrganizationName("Merlin")
    app.setQuitOnLastWindowClosed(True)

    from . import adblock
    from .browser import BrowserWindow
    from .store import Bookmarks, History
    from .ui import apply_font, apply_theme

    apply_theme(app, bool(settings.get("dark_ui")))
    apply_font(app, int(settings.get("ui_font_pt", 0) or 0))

    # --- persistent profile ---------------------------------------------
    profile = QWebEngineProfile(args.profile, app)
    profile.setPersistentStoragePath(os.path.join(cfg.DATA_DIR, args.profile))
    profile.setCachePath(os.path.join(cfg.CACHE_DIR, args.profile))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    profile.setHttpCacheMaximumSize(600 * 1024 * 1024)
    if settings.get("user_agent"):
        profile.setHttpUserAgent(settings.get("user_agent"))
    if hasattr(profile, "setDownloadPath"):
        from PyQt6.QtCore import QStandardPaths

        profile.setDownloadPath(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation))

    mark("engine profile ready")

    # --- content blocking ------------------------------------------------
    filter_engine = adblock.FilterEngine()
    filter_loader = adblock.FilterLoader(filter_engine, settings, app)
    interceptor = adblock.RequestInterceptor(filter_engine, settings, app)
    profile.setUrlRequestInterceptor(interceptor)


    history = History()
    bookmarks = Bookmarks()
    mark("history and bookmarks open")

    window = BrowserWindow(app, settings, profile, filter_engine, filter_loader,
                           interceptor, history, bookmarks)

    def adopt_engine(engine):
        """Swap the freshly parsed rules in once the worker finishes."""
        interceptor.engine = engine
        window.filter_engine = engine
        window._cosmetic_cache.clear()

    def announce(count):
        mark(f"filter rules parsed ({count:,})")

    filter_loader.loaded.connect(announce)
    filter_loader.engine_ready.connect(adopt_engine)
    # the built-in list is tiny, so shields work from the first paint while the
    # full lists parse in the background
    filter_engine.load_text(adblock.BUILTIN_RULES)
    filter_loader.load_cached_async()

    # two-finger swipe navigation, filtered at application level because
    # QtWebEngine's render widget consumes input before a view filter sees it
    from .gestures import SwipeNavigator

    swipe = SwipeNavigator(settings, app)
    app.installEventFilter(swipe)
    app.setProperty("merlin_swipe", swipe)
    profile.downloadRequested.connect(window.handle_download)
    window._apply_cookie_filter()

    # --- decoration decision ---------------------------------------------
    frameless = bool(settings.get("hide_window_decorations"))
    if args.app:
        frameless = True
    if args.decorations is not None:
        frameless = not args.decorations
        if args.persist_decorations:
            settings.set("hide_window_decorations", frameless)
    window.apply_decorations(frameless)

    # --- initial tabs -----------------------------------------------------
    opened = False
    if args.app:
        window.new_tab(args.app)
        opened = True
    for url in args.urls:
        window.new_tab(url)
        opened = True
    if not opened and settings.get("restore_session"):
        restored = settings.get("last_session", [])[:20]
        for position, url in enumerate(restored):
            # the first tab loads now, the rest wait until they are selected
            window.new_tab(url, background=True, defer=position > 0)
            opened = True
        if opened:
            window.tabs.setCurrentIndex(0)
    if not opened:
        window.new_tab(settings.get("home_page"))

    window.show()

    # No "if Windows" here on purpose. Gating this on os.name meant the branch
    # never ran during testing, and a NameError inside it shipped: QTimer was
    # used before its import. apply_to_window is a no-op off Windows, so the
    # same code now runs on every platform and mistakes surface immediately.
    from . import winicon
    from .brand import icon_path

    icon_file = icon_path()
    if winicon.apply_to_window(window, icon_file):
        mark("taskbar icon set on the window handle")
    # the shell creates the taskbar button a moment after the window appears,
    # so an icon set before that exists can be missed
    QTimer.singleShot(400, lambda: winicon.apply_to_window(window, icon_file))
    QTimer.singleShot(1500, lambda: winicon.apply_to_window(window, icon_file))

    mark("window visible")
    app.setProperty("merlin_windows", [window])

    if args.private:
        window.new_private_window()

    # refresh filter lists shortly after start-up, never blocking the UI
    QTimer.singleShot(4000, filter_loader.refresh_if_stale)
    QTimer.singleShot(1200, window.probe_codecs)
    if settings.get("check_updates_on_start"):
        QTimer.singleShot(6000, lambda: window.updater.check(quiet=True))
    window.status_label.setText("Loading filter lists...")
    filter_loader.loaded.connect(
        lambda count: window.status_label.setText(f"{count:,} filter rules loaded"))
    filter_loader.status.connect(window.status_label.setText)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
