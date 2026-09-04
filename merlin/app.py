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
    parser.add_argument("--tor", action="store_true",
                        help="route this window's traffic through a local Tor "
                             "daemon (implies --private)")
    parser.add_argument("--proxy", metavar="URL",
                        help="route through a proxy, e.g. socks5://127.0.0.1:1080")
    parser.add_argument("--app", metavar="URL",
                        help="open URL in a frameless single-purpose window")
    parser.add_argument("--profile", metavar="NAME", default="merlin",
                        help="named storage profile (default: merlin)")
    parser.add_argument("--embed-icon", metavar="EXE",
                        help="write Merlin's icon into a Windows executable")
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


APP_ID = "DansDesigns.Merlin.Browser"


def shortcut_carries_app_id() -> bool:
    """Did the installer manage to tag a shortcut with our App User Model ID?

    Only worth claiming an ID if something answers to it. With an ID and no
    matching shortcut, Windows stops using the window's own icon and falls back
    to the icon of the running executable, which for a Python program is
    Python's. Claiming an identity nothing answers to is worse than claiming
    none.

    The installer writes this marker only after the tagging succeeds, so the
    answer costs one file check rather than launching PowerShell on every
    start-up.
    """
    if os.name != "nt":
        return False
    marker = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "appid.txt")
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            return handle.read().strip() == APP_ID
    except OSError:
        return False


def set_windows_app_id() -> None:
    """Claim a taskbar identity only when a shortcut backs it up.

    MERLIN_APP_ID=1 forces it on, MERLIN_APP_ID=0 forces it off.
    """
    if os.name != "nt":
        return
    # Off. Not "off by default", off.
    #
    # Windows matches an App ID against the shortcut a window was launched
    # from. Tagging the Start Menu entries is not enough: pinning copies the
    # shortcut into its own folder, and that copy is untagged. Merlin will not
    # write to shortcuts it did not create, so a pinned launch can never match.
    #
    # An ID that does not resolve to a shortcut is worse than none: Windows
    # abandons the window's own icon and falls back to the icon of the running
    # executable, which for a Python program is Python's. Nothing tags a
    # shortcut any more, so nothing should claim an ID either.
    if os.environ.get("MERLIN_APP_ID") != "1":
        return
    try:
        import ctypes
        from ctypes import wintypes

        # Must match the id the installer stamps onto the shortcuts, or
        # Windows has nothing to tie the running window to. No version in it:
        # the id has to stay the same across updates or every release would
        # look like a different application and pin separately.
        app_id = APP_ID
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
    from .winicon import process_image

    image = process_image()
    print("Process image  :", image)
    print("Running as     :", os.path.basename(image))
    print("sys.executable :", sys.executable)
    if os.path.basename(image).lower() != os.path.basename(sys.executable).lower():
        print("                 the venv launcher started a child process; the"
              " window belongs to the image above")

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
        from .winexe import build_group, group_size_in_exe, parse_ico

        expected = 0
        if path:
            try:
                with open(path, "rb") as fh:
                    expected = len(build_group(parse_ico(fh.read())))
            except Exception:                            # noqa: BLE001
                expected = 0
        embedded = group_size_in_exe(image)
        if embedded and embedded == expected:
            verdict = "matches merlin.ico"
        elif embedded:
            verdict = f"present but {embedded} bytes, expected {expected}"
        else:
            verdict = ("not embedded here, which is expected when the window "
                       "is hosted by the interpreter rather than Merlin.exe")
        print("Icon in the exe:", verdict)
        claimed = os.environ.get("MERLIN_APP_ID") == "1"
        print("App ID         :", APP_ID if claimed else
              "not claimed, so the taskbar uses the window icon")
        status = ""
        try:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(root, "build-status.txt"),
                      encoding="utf-8") as handle:
                status = handle.read().strip()
        except OSError:
            pass
        if status:
            print("Merlin.exe     :", "built, so the window is Merlin's own"
                  if status == "built" else
                  "NOT built, so the window belongs to the interpreter and "
                  "the taskbar shows its icon")
        print("Shortcut tagged:",
              "yes -- this overrides the window icon, reinstall to clear it"
              if shortcut_carries_app_id() else "no (correct)")
        from PyQt6.QtGui import QIcon as _QIcon

        probe = QWidget()
        probe.setWindowTitle("Merlin icon check")
        probe.resize(200, 100)
        icon = _QIcon(path) if path else _QIcon()
        probe.setWindowIcon(icon)
        probe.show()
        app.processEvents()
        print("Qt window icon :",
              "set, sizes " + str([s.width() for s in icon.availableSizes()])
              if not icon.isNull() else "EMPTY, Qt could not load the file")
        ok = winicon.apply_to_window(probe, path)
        print("WM_SETICON     :", "accepted" if ok else "FAILED")
        print("LoadImageW     :", winicon.describe(path))
        print()
        if winicon.is_store_python():
            print("VERDICT: this is the Microsoft Store build of Python.")
            print("  Store apps get their taskbar identity and icon from the")
            print("  package manifest, so the window icon above is set")
            print("  correctly and then ignored. Windows shows the Python")
            print("  package's icon because it considers that package to be")
            print("  the application. No change inside Merlin can override it.")
            print("  Install a normal Python and reinstall:")
            print("      winget install Python.Python.3.13")
        else:
            print("The taskbar icon for a running window comes from the two")
            print("lines above. The exe line only matters if both of them fail.")
        probe.close()
    else:
        print("Note           :", winicon.describe(path))
    return 0


def build_chromium_flags(settings: cfg.Settings, args=None) -> str:
    flags = [
        "--enable-features=WebRTCPipeWireCapturer,OverlayScrollbar",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "--autoplay-policy=user-gesture-required",
        "--disable-breakpad",
        "--no-pings",
    ]
    if settings.get("block_webrtc_leak"):
        flags.append("--force-webrtc-ip-handling-policy=default_public_interface_only")
    from . import privacy

    proxy_url = ""
    if args is not None and getattr(args, "proxy", None):
        proxy_url = args.proxy
    elif args is not None and getattr(args, "tor", False):
        proxy_url = privacy.tor_proxy_url()
    else:
        mode = settings.get("proxy_mode", "none")
        if mode == "tor":
            proxy_url = privacy.tor_proxy_url()
        elif mode == "custom":
            proxy_url = (settings.get("proxy_url") or "").strip()
    flags += privacy.proxy_flags(proxy_url)

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

    if args.embed_icon:
        from .brand import icon_path
        from .winexe import set_exe_icon

        ok, message = set_exe_icon(args.embed_icon, icon_path())
        print(("OK: " if ok else "Failed: ") + message)
        return 0 if ok else 1

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
    if args.tor:
        args.private = True
        from . import privacy

        if not privacy.tor_available():
            # Never start unproxied when Tor was asked for: the window would
            # look like a Tor window and carry the user's own address.
            print("Merlin: --tor was requested but no Tor daemon is listening "
                  f"on 127.0.0.1:{privacy.TOR_PORTS[0]} or "
                  f":{privacy.TOR_PORTS[1]}.\n\n"
                  + privacy.install_hint(), file=sys.stderr)
            return 3

    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = build_chromium_flags(settings, args)
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWebEngineCore import QWebEngineProfile
    # QtWebEngineWidgets must be imported before the QApplication exists
    from PyQt6 import QtWebEngineWidgets  # noqa: F401
    from PyQt6.QtWidgets import QApplication

    mark("Qt imported")
    # Hand over to a Merlin already running, unless this is meant to be its
    # own session. Done before QApplication so the second process costs almost
    # nothing when it is only carrying a link.
    share = (settings.get("single_instance", True)
             and not args.private and not args.tor and not args.app)
    if share:
        from . import single

        if single.hand_off(list(args.urls), args.profile):
            return 0

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

    if settings.get("gpu_protection", True):
        from PyQt6.QtWebEngineCore import QWebEngineScript

        from . import fingerprint

        guard = QWebEngineScript()
        guard.setName("merlin-gpu-guard")
        guard.setSourceCode(fingerprint.build_script(fingerprint.new_seed()))
        # before the page's own scripts, or a tracker could read the real
        # values first, and in the page's world so that page scripts see it
        guard.setInjectionPoint(
            QWebEngineScript.InjectionPoint.DocumentCreation)
        guard.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        guard.setRunsOnSubFrames(True)
        profile.scripts().insert(guard)


    history = History()
    bookmarks = Bookmarks()
    mark("history and bookmarks open")

    window = BrowserWindow(app, settings, profile, filter_engine, filter_loader,
                           interceptor, history, bookmarks,
                           private=bool(args.private or args.tor),
                           via_tor=bool(args.tor))

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

    if share:
        from . import single

        instance_server = single.InstanceServer(args.profile, app)
        if instance_server.listen():
            instance_server.urls_received.connect(window.adopt_urls)
            app.setProperty("merlin_instance_server", instance_server)

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
