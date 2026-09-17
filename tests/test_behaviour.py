#!/usr/bin/env python3
"""Behaviour tests for Merlin.

These run the real window against the real engine, offscreen, and check things
that only go wrong once everything is wired together: sessions, tab lifetime,
gestures, local addresses.

They live here rather than in a scratch directory because every one of them
exists to stop a bug coming back, and several of those bugs did come back after
the throwaway version of the test was lost.

    python3 tests/test_behaviour.py

Needs PyQt6 and PyQt6-WebEngine. Exits non-zero if anything fails.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-dev-shm-usage")
os.environ["HOME"] = tempfile.mkdtemp(prefix="merlin-tests-")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QPoint, QPointF, Qt, QTimer, QUrl   # noqa: E402
from PyQt6.QtGui import QWheelEvent                          # noqa: E402
from PyQt6.QtWidgets import QApplication                     # noqa: E402
from PyQt6 import QtWebEngineWidgets                         # noqa: E402,F401
from PyQt6.QtWebEngineCore import QWebEngineProfile          # noqa: E402

from merlin import adblock, settings as cfg                  # noqa: E402
from merlin.browser import BrowserWindow                     # noqa: E402
from merlin.gestures import SwipeNavigator                   # noqa: E402
from merlin.store import Bookmarks, History                  # noqa: E402
from merlin.ui import apply_theme                            # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((bool(passed), name, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"  {mark}  {name}" + (f"  ({detail})" if detail else ""))


def make_window(app, tag: str, **preset):
    settings = cfg.Settings()
    for key, value in preset.items():
        settings.set(key, value, save=False)
    apply_theme(app, True)
    profile = QWebEngineProfile(tag, app)
    engine = adblock.FilterEngine()
    loader = adblock.FilterLoader(engine, settings, app)
    interceptor = adblock.RequestInterceptor(engine, settings, app)
    profile.setUrlRequestInterceptor(interceptor)
    window = BrowserWindow(app, settings, profile, engine, loader, interceptor,
                           History(), Bookmarks())
    window.resize(900, 520)
    window.show()
    return window, settings, interceptor


def page(text: str, filler: int = 0) -> str:
    return f"data:text/html,<title>{text}</title>" + ("<p>x</p>" * filler)


# ----------------------------------------------------------------- sessions
def test_session(app) -> None:
    window, settings, _ = make_window(app, "t-session", restore_session=True)
    sites = ["https://example.com/one", "https://example.org/two",
             "https://example.net/three"]
    for position, url in enumerate(sites):
        window.new_tab(url, background=True, defer=position > 0)
    window.tabs.setCurrentIndex(0)
    wait(app, 1.2)

    window.save_session()
    saved = settings.get("last_session") or []
    check("every restored tab is saved, loaded or not",
          sorted(saved) == sorted(sites), f"{len(saved)} of {len(sites)}")

    window._closed_tabs.clear()
    window.close_tab(2)
    check("closing an unopened tab leaves it reopenable",
          bool(window._closed_tabs))
    window.close()


def test_placeholders(app) -> None:
    window, _, _ = make_window(app, "t-placeholder")
    window.new_tab(page("first"), background=True)
    for url in ("https://news.ycombinator.com/", "https://github.com/x"):
        window.new_tab(url, background=True, defer=True)
    window.tabs.setCurrentIndex(0)
    wait(app, 1.0)

    icons_shown = all(not window.tabs.tabIcon(i).isNull()
                      for i in range(1, window.tabs.count()))
    labels = [window.tabs.tabText(i) for i in range(1, window.tabs.count())]
    check("waiting tabs show a placeholder icon", icons_shown)
    check("waiting tabs are labelled with the host",
          labels == ["news.ycombinator.com", "github.com"], str(labels))
    window.close()


# --------------------------------------------------------------- tab churn
def test_tab_churn(app) -> None:
    """Close and switch while pages are loading, which used to crash."""
    window, _, _ = make_window(app, "t-churn")
    survived = True
    for round_number in range(30):
        while window.tabs.count() < 4:
            window.new_tab(page(f"p{round_number}", 400), background=True,
                           defer=window.tabs.count() > 0)
        if round_number % 3 == 0 and window.tabs.count() > 1:
            window.close_tab(0)
        else:
            window.tabs.setCurrentIndex(round_number % window.tabs.count())
        wait(app, 0.05)
    check("closing and switching between loading tabs survives", survived,
          "30 rounds")
    window.close()


# --------------------------------------------------------------- gestures
def test_gestures(app) -> None:
    window, settings, _ = make_window(app, "t-gesture")
    navigator = SwipeNavigator(settings, app)
    view = window.new_tab(page("one"))
    wait(app, 1.5)
    view.setUrl(QUrl(page("two")))
    wait(app, 1.5)

    def wheel(dx, dy):
        return QWheelEvent(
            QPointF(450, 300), QPointF(view.mapToGlobal(QPoint(450, 300))),
            QPoint(int(dx), int(dy)), QPoint(int(dx * 3), int(dy * 3)),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate, False)

    def feed(events):
        navigator._reset()
        navigator._last_fire = 0
        window.swipe_indicator.reset()
        fired = False
        for dx, dy in events:
            fired = navigator.eventFilter(view, wheel(dx, dy)) or fired
            time.sleep(0.04)
        return fired

    check("a scroll down then up with a sideways lean does not navigate",
          not feed([(9, -40)] * 8 + [(9, 40)] * 8 + [(9, -40)] * 8))
    check("a steady vertical scroll does not navigate",
          not feed([(8, -45)] * 20))
    check("a deliberate horizontal swipe does navigate",
          feed([(90, 1)] * 9))

    settings.set("swipe_distance", 900, save=False)
    check("a longer distance setting makes the same swipe too short",
          not feed([(90, 1)] * 6))
    window.close()


# ------------------------------------------------------- local addresses
def test_local_addresses(app) -> None:
    window, _, interceptor = make_window(app, "t-local")
    wait(app, 0.6)

    expected = {
        "192.168.1.50": "http://192.168.1.50",
        "openmediavault/": "http://openmediavault/",
        "omv.local": "http://omv.local",
        "example.com": "https://example.com",
    }
    for text, wanted in expected.items():
        url = window.normalise(text)
        check(f"typing {text!r} goes to {wanted}",
              url is not None and url.toString() == wanted,
              url.toString() if url else "none")

    search = window.normalise("how to tie a bowline")
    check("a phrase still searches", search is not None and "q=" in search.toString())

    from merlin.adblock import is_local_host
    check("private ranges count as local",
          all(is_local_host(h) for h in
              ("10.0.0.5", "172.16.4.2", "192.168.0.1", "169.254.1.1")))
    check("public addresses do not",
          not any(is_local_host(h) for h in
                  ("8.8.8.8", "172.32.0.1", "example.com")))
    window.close()


# ------------------------------------------------------------- app windows
def test_app_mode(app) -> None:
    """A page installed as an app gets a window without the browsing parts."""
    window, _, _ = make_window(app, "t-appmode")
    window.apply_app_mode()
    window.new_tab(page("site"))
    wait(app, 1.0)

    gone = {
        "address bar": window.url_bar.isVisible(),
        "tab strip": window.tabs.v_strip.isVisible(),
        "home": window.act_home.isVisible(),
        "bookmark star": window.btn_bookmark.isVisible(),
        "bookmarks menu": window.btn_bookmarks.isVisible(),
        "history menu": window.btn_history.isVisible(),
    }
    still_shown = [name for name, visible in gone.items() if visible]
    check("an app window drops the browsing parts", not still_shown,
          str(still_shown))

    kept = {
        "back": window.act_back.isVisible(),
        "forward": window.act_forward.isVisible(),
        "reload": window.act_reload.isVisible(),
        "shields": window.btn_shields.isVisible(),
        "downloads": window.btn_downloads.isVisible(),
        "menu": window.btn_menu.isVisible(),
    }
    missing = [name for name, visible in kept.items() if not visible]
    check("an app window keeps navigation, shields, downloads and the menu",
          not missing, str(missing))
    window.close()


# ------------------------------------------------------------------- run
def wait(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    app = QApplication([sys.argv[0]])
    for test in (test_session, test_placeholders, test_tab_churn,
                 test_gestures, test_local_addresses, test_app_mode):
        print(f"\n{test.__name__}")
        try:
            test(app)
        except Exception as exc:                          # noqa: BLE001
            import traceback

            check(test.__name__, False, str(exc))
            traceback.print_exc()

    failed = [name for ok, name, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(failed)} FAILED: " + ", ".join(failed))
        return 1
    print(f"all {len(RESULTS)} behaviour checks OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
