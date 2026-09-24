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
from PyQt6.QtWidgets import QMessageBox, QApplication                     # noqa: E402
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

    gone["shields"] = window.btn_shields.isVisible()
    gone["downloads"] = window.btn_downloads.isVisible()
    gone["menu"] = window.btn_menu.isVisible()
    still_shown = [name for name, visible in gone.items() if visible]
    check("an app window has no menus at all", not still_shown,
          str(still_shown))

    kept = {
        "back": window.act_back.isVisible(),
        "forward": window.act_forward.isVisible(),
        "reload": window.act_reload.isVisible(),
        "title bar toggle": window.btn_decorations.isVisible(),
    }
    missing = [name for name, visible in kept.items() if not visible]
    check("an app window keeps back, forward, reload and the title bar toggle",
          not missing, str(missing))

    # with no address bar to push them over, the right hand group needs the
    # spacer or it bunches up against the reload button
    window.apply_decorations(True)
    wait(app, 0.4)
    buttons = window.window_buttons
    right_edge = buttons.x() + buttons.width()
    check("window controls sit at the right of the toolbar",
          right_edge >= window.toolbar.width() - 14,
          f"ends at {right_edge} of {window.toolbar.width()}")
    # the menu is gone from an app window; the title bar toggle is the last
    # thing before the spacer
    check("the title bar toggle sits left of the window controls",
          window.btn_decorations.x() < buttons.x())
    window.close()


def test_decorations_per_window(app) -> None:
    """The title bar belongs to one window, and the tick tells the truth."""
    from PyQt6.QtCore import Qt as _Qt

    one, settings, _ = make_window(app, "t-dec1",
                                   hide_window_decorations=False)
    one.new_tab(page("one"))
    two, _, _ = make_window(app, "t-dec2")
    two.new_tab(page("two"))
    wait(app, 0.8)

    def frameless(window):
        return bool(window.windowFlags() & _Qt.WindowType.FramelessWindowHint)

    check("the tick starts in step with the window",
          one.act_decorations.isChecked() == frameless(one))

    one.act_decorations.trigger()
    wait(app, 0.3)
    check("hiding the title bar changes only that window",
          frameless(one) and not frameless(two))
    check("the tick still agrees after toggling",
          one.act_decorations.isChecked() == frameless(one))

    one.act_decorations.trigger()
    wait(app, 0.3)
    check("toggling back brings the title bar back", not frameless(one))
    one.close()
    two.close()


def test_address_bar_selects(app) -> None:
    """Clicking into the address bar selects the whole address."""
    from PyQt6.QtCore import QPoint as _P
    from PyQt6.QtTest import QTest

    window, _, _ = make_window(app, "t-select")
    window.new_tab()
    wait(app, 0.8)
    bar = window.url_bar
    bar.setText("https://www.youtube.com/watch?v=abc")
    window.current().setFocus()
    wait(app, 0.2)
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton,
                     pos=_P(bar.width() // 2, bar.height() // 2))
    wait(app, 0.2)
    check("the first click selects the whole address",
          bar.selectedText() == bar.text())
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton,
                     pos=_P(bar.width() // 3, bar.height() // 2))
    wait(app, 0.1)
    check("a second click places the cursor instead", bar.selectedText() == "")
    window.close()


def test_theme_after_closing_tabs(app) -> None:
    """Close tabs, then switch theme straight away. This used to crash."""
    window, settings, _ = make_window(app, "t-theme", dark_ui=True,
                                      theme_mode="manual")
    window.new_tab()
    for _ in range(12):
        for _ in range(3):
            window.new_tab(page("t", 200))
        while window.tabs.count() > 1:
            window.close_tab(window.tabs.count() - 1)
        window.act_dark.trigger()
        wait(app, 0.03)
    check("switching theme right after closing tabs survives", True,
          "12 rounds")
    window.close()


def test_live_stream_offer(app) -> None:
    """A live stream the engine cannot play goes to the player by itself."""
    window, _, _ = make_window(app, "t-live")
    original = BrowserWindow.__dict__["_is_youtube_video"]
    BrowserWindow._is_youtube_video = staticmethod(lambda url: True)
    started = []
    window.play_stream = lambda page: started.append(page)
    try:
        view = window.new_tab()
        view.setHtml("<title>live</title><div id=movie_player>"
                     "<div class='ytp-live-badge'>LIVE</div></div>",
                     QUrl("https://www.youtube.com/watch?v=test"))
        wait(app, 2.0)
        window._check_live_stream(view)
        wait(app, 1.0)
        check("a live stream the engine cannot play opens in the player by itself",
              started == ["https://www.youtube.com/watch?v=test"], str(started))
        check("with no bar to click first", not window.notice_bar.isVisible())
        window._check_live_stream(view)
        wait(app, 1.0)
        check("and only once for the same page", len(started) == 1)
    finally:
        BrowserWindow._is_youtube_video = original
        window.close()


def test_builtin_player_decodes_h264(app) -> None:
    """Qt Multimedia really decodes H.264, the codec live streams use.

    Plays a one second H.264 clip and counts decoded frames. Qt's list of
    advertised codecs cannot be trusted for this: it leaves H.264 out even
    though its own bundled FFmpeg decodes it.
    """
    try:
        from PyQt6.QtMultimedia import QMediaPlayer, QVideoSink
    except Exception as exc:                              # noqa: BLE001
        check("Qt Multimedia is installed", False, str(exc))
        return
    sample = os.path.join(ROOT, "tests", "data", "h264-sample.mp4")
    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoSink(sink)
    frames = []
    sink.videoFrameChanged.connect(lambda frame: frames.append(frame.width()))
    player.setSource(QUrl.fromLocalFile(sample))
    player.play()
    wait(app, 3.0)
    player.stop()
    check("Qt Multimedia decodes H.264", len(frames) > 3,
          f"{len(frames)} frames")


def test_youtube_needs_a_runtime(app) -> None:
    """Without Deno, a YouTube lookup says why instead of failing oddly."""
    from merlin import media as _media

    original = _media.deno_path
    _media.deno_path = lambda: ""
    original_ytdlp = _media.ytdlp_path
    _media.ytdlp_path = lambda: "/bin/true"
    try:
        ok, why = _media.resolve_stream("https://www.youtube.com/watch?v=x")
        check("a YouTube lookup without Deno names the missing runtime",
              not ok and why == _media.JS_RUNTIME_MISSING)
        check("other sites are not held back by it",
              _media._needs_js_runtime("https://example.com/a") is False)
    finally:
        _media.deno_path = original
        _media.ytdlp_path = original_ytdlp


def test_slot_errors_are_not_fatal(app) -> None:
    """An exception in a timer callback is logged, and Merlin keeps going."""
    import subprocess

    script = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "from merlin import crashlog\n"
        "crashlog.enable()\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "from PyQt6.QtCore import QTimer\n"
        "app = QApplication([sys.argv[0]])\n"
        "def broken():\n"
        "    raise TypeError('a bug in a timer callback')\n"
        "QTimer.singleShot(100, broken)\n"
        "QTimer.singleShot(600, lambda: (print('ALIVE'), app.quit()))\n"
        "app.exec()\n")
    done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=60)
    check("an error in a slot does not end Merlin",
          done.returncode == 0 and "ALIVE" in done.stdout,
          f"exit {done.returncode}")


def test_notice_bar_is_readable(app) -> None:
    """The notice bar's text stands out from its background in both themes."""
    from merlin.ui import NoticeBar, apply_theme as _theme

    def luminance(colour):
        def channel(value):
            value /= 255
            return value / 12.92 if value <= 0.03928 else (
                (value + 0.055) / 1.055) ** 2.4
        return (0.2126 * channel(colour.red()) + 0.7152 * channel(colour.green())
                + 0.0722 * channel(colour.blue()))

    for dark in (False, True):
        _theme(app, dark)
        bar = NoticeBar()
        bar.resize(640, 40)
        bar.show_notice("A live stream this engine cannot decode.", "Play")
        bar.show()
        wait(app, 0.2)
        image = bar.grab().toImage()
        background = image.pixelColor(3, image.height() // 2)
        middle = image.height() // 2
        glyph = max((image.pixelColor(x, y) for x in range(10, 280)
                     for y in range(middle - 6, middle + 6)),
                    key=lambda c: abs(c.lightness() - background.lightness()))
        high, low = sorted((luminance(glyph), luminance(background)), reverse=True)
        ratio = (high + 0.05) / (low + 0.05)
        check(f"the notice bar is readable in the {'dark' if dark else 'light'} theme",
              ratio >= 4.5, f"contrast {ratio:.1f}:1")
        bar.close()
    _theme(app, True)


def test_closing_is_prompt(app) -> None:
    """Merlin's process ends promptly after its window closes."""
    import subprocess

    script = (
        "import os, sys, threading, time\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = '--no-sandbox --disable-gpu'\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "import merlin.browser as B\n"
        "from merlin import media\n"
        "from PyQt6.QtCore import QTimer\n"
        "Real = B.BrowserWindow\n"
        "class W(Real):\n"
        "    def __init__(self, *a, **k):\n"
        "        super().__init__(*a, **k)\n"
        "        QTimer.singleShot(2500, self.go)\n"
        "    def go(self):\n"
        "        def run():\n"
        "            try:\n"
        "                media._run_helper(['sleep', '60'], timeout=90)\n"
        "            except Exception:\n"
        "                pass\n"
        "        threading.Thread(target=run, daemon=True).start()\n"
        "        time.sleep(0.3)\n"
        "        print('CLOSING', time.time(), flush=True)\n"
        "        self.close()\n"
        "B.BrowserWindow = W\n"
        "import merlin.app as A\n"
        "A.BrowserWindow = W\n"
        "A.main([])\n")
    home = tempfile.mkdtemp(prefix="merlin-close-")
    env = dict(os.environ, HOME=home)
    started = time.time()
    done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=90, env=env)
    ended = time.time()
    closing = [float(line.split()[1]) for line in done.stdout.splitlines()
               if line.startswith("CLOSING")]
    after = ended - closing[0] if closing else ended - started
    check("the process ends within a few seconds of closing, even mid-lookup",
          bool(closing) and after < 5.0, f"{after:.2f}s after close")


def test_youtube_asks_for_hls_first(app) -> None:
    """YouTube's HLS formats are asked for first, then other clients."""
    import stat

    from merlin import media as _media

    folder = tempfile.mkdtemp(prefix="fake-ytdlp-")
    fake = os.path.join(folder, "yt-dlp")
    with open(fake, "w") as handle:
        handle.write(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "a = ' '.join(sys.argv[1:])\n"
            "if 'player_client=web_safari' in a:\n"
            "    print('https://example.invalid/live.m3u8'); sys.exit(0)\n"
            "sys.stderr.write('ERROR: Requested format is not available\\n')\n"
            "sys.exit(1)\n")
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    saved = (_media.ytdlp_path, _media.deno_path, _media._ytdlp_argv)
    _media.ytdlp_path = lambda: fake
    _media.deno_path = lambda: "/bin/true"
    _media._ytdlp_argv = lambda p: [p]
    try:
        ok, found = _media.resolve_stream("https://www.youtube.com/watch?v=x")
        check("a YouTube live page is found through its HLS formats",
              ok and found.endswith(".m3u8"), found)
        check("HLS through the Safari client is asked for first",
              "web_safari" in " ".join(_media.YOUTUBE_ATTEMPTS[0][1]))
    finally:
        _media.ytdlp_path, _media.deno_path, _media._ytdlp_argv = saved


def test_released_views_are_dead(app) -> None:
    """A page released at shutdown is not mistaken for a live one."""
    from PyQt6.QtCore import QCoreApplication, QEvent

    window, _, _ = make_window(app, "t-released")
    window.new_tab(page("a"))
    for url in ("https://example.org/two", "https://example.net/three"):
        window.new_tab(url, background=True, defer=True)
    wait(app, 1.0)
    view = window.tabs.widget(2)
    check("a restored tab counts as alive before shutdown",
          window.view_is_alive(view))
    window.tabs.setCurrentIndex(2)       # queues its load for the next turn
    window.release_pages()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    check("once released it no longer counts as alive",
          not window.view_is_alive(view))
    errors = []
    saved = sys.excepthook
    sys.excepthook = lambda *a: errors.append(a[1])
    try:
        wait(app, 0.4)                   # let the queued load run
    finally:
        sys.excepthook = saved
    check("the queued load finds nothing to do, and raises nothing",
          not errors, str(errors[:1]))


def test_codec_engine_swap_is_safe(app) -> None:
    """A Qt WebEngine that lacks H.264 is refused and nothing is left changed."""
    import importlib.util
    import shutil as _shutil

    spec = importlib.util.spec_from_file_location(
        "use_codec_engine", os.path.join(ROOT, "tools", "use-codec-engine.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    check("the engine in use is recognised as lacking H.264",
          not tool.has_codecs({"h264": "", "aac": "", "mse_h264": False}))
    check("an engine that plays both would be accepted",
          tool.has_codecs({"h264": "probably", "aac": "probably", "mse_h264": True}))

    scratch = tempfile.mkdtemp(prefix="codec-prefix-")
    os.makedirs(os.path.join(scratch, "lib", "cmake", "Qt6WebEngineCore"))
    with open(os.path.join(scratch, "lib", "cmake", "Qt6WebEngineCore",
                           "Qt6WebEngineCoreConfigVersion.cmake"), "w") as handle:
        handle.write('set(PACKAGE_VERSION "6.9.9")')
    check("a build's version is read from its CMake files",
          tool.prefix_version(scratch) == "6.9.9")
    files = tool.engine_files(scratch, installed=False)
    check("a folder without the engine library still reserves its place",
          len(files) >= 2 and "WebEngineCore" in files[0])
    _shutil.rmtree(scratch, ignore_errors=True)


def test_engine_download_is_checked(app) -> None:
    """A published engine is used only if it matches its published checksum."""
    import hashlib
    import http.server
    import importlib.util
    import io
    import tarfile
    import threading
    import zipfile

    spec = importlib.util.spec_from_file_location(
        "use_codec_engine", os.path.join(ROOT, "tools", "use-codec-engine.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    www = tempfile.mkdtemp(prefix="engine-releases-")
    version = "6.99.9"
    name = tool.asset_name(version)

    def publish(folder, members, tamper=False):
        release = os.path.join(www, folder, f"engine-{version}")
        os.makedirs(release, exist_ok=True)
        path = os.path.join(release, name)
        if name.endswith(".zip"):
            with zipfile.ZipFile(path, "w") as bundle:
                for member, data in members:
                    bundle.writestr(member, data)
        else:
            with tarfile.open(path, "w:gz") as bundle:
                for member, data in members:
                    info = tarfile.TarInfo(member)
                    info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        with open(path + ".sha256", "w") as out:
            out.write(f"{digest}  {name}\n")
        if tamper:
            with open(path, "ab") as out:
                out.write(b"altered after publishing")

    publish("good", [("resources/icudtl.dat", b"x" * 64)])
    publish("tampered", [("resources/icudtl.dat", b"x" * 64)], tamper=True)
    publish("hostile", [("../../escaped.txt", b"owned")])

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=www, **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    saved = os.environ.get("MERLIN_ENGINE_URL")
    try:
        os.environ["MERLIN_ENGINE_URL"] = base + "/good"
        prefix, _ = tool.fetch_published(version, tempfile.mkdtemp())
        check("a published build that matches its checksum is unpacked",
              bool(prefix) and os.path.isfile(
                  os.path.join(prefix, "resources", "icudtl.dat")))

        os.environ["MERLIN_ENGINE_URL"] = base + "/tampered"
        cache = tempfile.mkdtemp()
        prefix, why = tool.fetch_published(version, cache)
        check("a download that does not match its checksum is thrown away",
              not prefix and "checksum" in why
              and not os.path.exists(os.path.join(cache, name)), why)

        os.environ["MERLIN_ENGINE_URL"] = base + "/hostile"
        prefix, why = tool.fetch_published(version, tempfile.mkdtemp())
        check("an archive that writes outside its folder is refused",
              not prefix and "unsafe" in why, why)

        os.environ["MERLIN_ENGINE_URL"] = base + "/nothing"
        prefix, why = tool.fetch_published(version, tempfile.mkdtemp())
        check("with nothing published it says so, without failing",
              not prefix and "published" in why, why)
    finally:
        server.shutdown()
        if saved is None:
            os.environ.pop("MERLIN_ENGINE_URL", None)
        else:
            os.environ["MERLIN_ENGINE_URL"] = saved


def test_engine_update_bookkeeping(app) -> None:
    """Staging is asked for only when it can help, and failures are not retried."""
    import json as _json
    import time as _time

    from merlin import codecengine as ce

    saved = {k: os.environ.get(k) for k in ("XDG_DATA_HOME", "LOCALAPPDATA")}
    scratch = tempfile.mkdtemp(prefix="engine-state-")
    os.environ["XDG_DATA_HOME"] = scratch
    os.environ["LOCALAPPDATA"] = scratch
    real_can = ce.can_replace_engine
    ce.can_replace_engine = lambda: True
    try:
        check("a new engine version is worth looking for", ce.should_stage("6.99.1"))

        ce._write_state({"none_published": {"version": "6.99.1", "at": _time.time()}})
        check("nothing published is not asked again the same day",
              not ce.should_stage("6.99.1"))
        ce._write_state({"none_published": {"version": "6.99.1",
                                            "at": _time.time() - 90000}})
        check("but is asked again the next day", ce.should_stage("6.99.1"))

        ce._write_state({"failed": ["6.99.1"]})
        check("a version that failed is not tried again",
              not ce.should_stage("6.99.1"))
        check("while a newer one still is", ce.should_stage("6.99.2"))

        ce._write_state({"staged": {"version": "6.99.2", "prefix": scratch}})
        real_in_use = ce.engine_in_use
        ce.engine_in_use = lambda target: True
        try:
            outcome = ce.apply_staged(say=lambda _t: None)
        finally:
            ce.engine_in_use = real_in_use
        state = _json.load(open(ce._state_file()))
        check("with the engine held by another Merlin, the update waits",
              outcome == "" and state.get("staged", {}).get("version") == "6.99.2"
              and "6.99.2" not in state.get("failed", []))

        ce._write_state({})
        check("with nothing staged, a start does nothing",
              ce.apply_staged(say=lambda _t: None) == "")
    finally:
        ce.can_replace_engine = real_can
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_stream_taken_from_the_page(app) -> None:
    """The live stream's address comes from YouTube's own player, and only
    for the video actually on screen."""
    from merlin import media as _media

    window, _, _ = make_window(app, "t-pagestream")
    view = window.new_tab()
    results = {}

    def ask(name, html, address):
        view.setHtml(html, QUrl(address))
        wait(app, 1.2)
        view.page().runJavaScript(_media.YOUTUBE_STREAM_JS,
                                  lambda r: results.__setitem__(name, r or {}))
        wait(app, 0.6)

    player = ("<div id=movie_player></div><script>"
              "document.getElementById('movie_player').getPlayerResponse = "
              "function(){return {videoDetails:{videoId:'%s'},"
              "streamingData:{hlsManifestUrl:'https://example.invalid/%s.m3u8'}};};"
              "</script>")
    ask("current", player % ("LIVE1", "live1"), "https://www.youtube.com/watch?v=LIVE1")
    ask("stale", player % ("OLDVID", "old"), "https://www.youtube.com/watch?v=LIVE1")
    ask("live path", player % ("ABCDEF", "abc"), "https://www.youtube.com/live/ABCDEF")
    ask("none", "<p>nothing here</p>", "https://www.youtube.com/watch?v=LIVE1")

    check("the stream for the video on screen is found",
          results.get("current", {}).get("hls", "").endswith("live1.m3u8"))
    check("a response for a different video is ignored",
          results.get("stale", {}).get("hls") == "")
    check("a /live/ address is matched too",
          results.get("live path", {}).get("hls", "").endswith("abc.m3u8"))
    check("a page without a stream gives nothing",
          results.get("none", {}).get("hls") == "")
    window.close()


def test_player_plays_in_the_page(app) -> None:
    """Merlin's player sits over the page's own video box, the same size."""
    from merlin.inplace import InPagePlayer
    from merlin.playertab import PlayerTab

    window, _, _ = make_window(app, "t-inpage")
    view = window.new_tab()
    view.setHtml(
        "<body style='margin:0'><div style='height:50px'></div>"
        "<div id=movie_player style='margin-left:30px;width:480px;height:270px'>"
        "</div></body>", QUrl("https://www.youtube.com/watch?v=IN"))
    wait(app, 1.5)
    sample = os.path.join(ROOT, "tests", "data", "h264-sample.mp4")
    window._play_in_page(view, QUrl.fromLocalFile(sample).toString(),
                         "https://www.youtube.com/watch?v=IN")
    frames = []
    player = window._inplace.get(view)
    player._worker.image.connect(lambda _image: frames.append(1))
    wait(app, 2.5)
    zoom = view.zoomFactor()
    expected = (round(30 * zoom), round(50 * zoom), round(480 * zoom), round(270 * zoom))
    got = (player.x(), player.y(), player.width(), player.height())
    check("the player sits exactly over the page's own video box",
          got == expected, f"{got} for {expected}")
    check("it plays there", len(frames) > 3, f"{len(frames)} frames")
    check("and no separate player tab is opened",
          not any(isinstance(window.tabs.widget(i), PlayerTab)
                  for i in range(window.tabs.count())))

    window._on_load_state(view, True)                    # a reload begins
    wait(app, 0.3)
    check("a reload stops it, so the page can be tried afresh",
          view not in window._inplace)
    window.close()


def test_player_controls(app) -> None:
    """The in-page player's controls behave as YouTube's do."""
    from PyQt6.QtCore import QPoint as _P
    from PyQt6.QtTest import QTest

    from merlin.inplace import PlayerControls

    controls = PlayerControls()
    controls.resize(640, 360)
    controls.show()
    heard = []
    for name in ("toggle", "fullscreen", "close_player"):
        getattr(controls, name).connect(lambda n=name: heard.append(n))
    controls.muted.connect(lambda m: heard.append(f"muted {m}"))
    controls.seek_by.connect(lambda ms: heard.append(f"seek {ms}"))
    controls.setFocus()
    for key in (Qt.Key.Key_Space, Qt.Key.Key_K, Qt.Key.Key_M, Qt.Key.Key_F,
                Qt.Key.Key_Left, Qt.Key.Key_Right):
        QTest.keyClick(controls, key)
    check("space and K pause, M mutes, F goes fullscreen, arrows seek",
          heard == ["toggle", "toggle", "muted True", "fullscreen",
                    "seek -5000", "seek 5000"], str(heard))

    heard.clear()
    row = controls.height() - PlayerControls.BAR // 2
    QTest.mouseClick(controls, Qt.MouseButton.LeftButton, pos=_P(320, 150))
    QTest.mouseClick(controls, Qt.MouseButton.LeftButton, pos=_P(30, row))
    QTest.mouseClick(controls, Qt.MouseButton.LeftButton, pos=_P(640 - 74, row))
    QTest.mouseClick(controls, Qt.MouseButton.LeftButton, pos=_P(640 - 30, row))
    check("clicks on the picture, play, close and fullscreen do what they say",
          heard == ["toggle", "toggle", "close_player", "fullscreen"], str(heard))

    controls.set_progress(62000, 0, False)
    check("a stream with no end counts as live", controls.live)
    controls.set_progress(3000, 12000, True)
    check("one with an end and seeking does not", not controls.live)

    controls._hide.setInterval(200)
    controls.set_playing(True)
    wait(app, 0.5)
    check("the controls fade while a video plays and the pointer rests",
          not controls._shown)
    controls.set_playing(False)
    check("and come back when it pauses", controls._shown)
    controls.close()


def test_filter_options_read_for_what_they_mean(app) -> None:
    """generichide exempts a site from general hiding; unsupported options
    that change a rule's meaning drop the rule rather than widen it."""
    from merlin.adblock import FilterEngine, _registrable

    engine = FilterEngine()
    engine.load_text("\n".join([
        "##.generic-ad",
        "example.org##.site-ad",
        "@@||www.example.org^$generichide",
        "||popups.example^$popup",
        "$csp=script-src 'self',domain=csp.example",
        "||ads.example^",
        "||www.example.org/ads/",
    ]))
    css = engine.cosmetic_css("www.example.org")
    check("a generichide site gets its own hiding rules but no general ones",
          ".site-ad" in css and ".generic-ad" not in css, css[:80])
    # read as a plain exception, generichide allowed every request to the
    # site, so a rule blocking part of it never fired
    check("and generichide does not let every request to the site through",
          engine.should_block("https://www.example.org/ads/banner.js", "script", False,
                              "www.example.org"))
    check("a popup rule no longer blocks ordinary requests",
          not engine.should_block("https://popups.example/a.js", "script", True, "site.example"))
    check("a csp rule no longer blocks every request on its site",
          not engine.should_block("https://csp.example/app.js", "script", False, "csp.example"))
    check("ordinary blocking still works",
          engine.should_block("https://ads.example/a.js", "script", True, "site.example"))
    check("IP addresses are sites of their own",
          _registrable("127.0.0.1") == "127.0.0.1" and _registrable("10.0.0.1") != _registrable("127.0.0.1"))


def test_blocked_list_and_app_links(app) -> None:
    """The shield menu lists what was blocked; app windows keep to their site."""
    window, _, _ = make_window(app, "t-blocked")
    view = window.new_tab()
    view.setHtml("<p>x</p>", QUrl("https://news.example.com/story"))
    wait(app, 1.0)
    window._on_blocked("news.example.com", "https://ads.example/one.js")
    window._on_blocked("news.example.com", "https://track.example/pixel.gif")
    window._sync_shields_menu()
    check("the shield menu counts what was blocked on this page",
          window.blocked_menu.title() == "Blocked on this page (2)", window.blocked_menu.title())
    window.app_site = "example.com"
    check("an app window keeps links within its own site",
          window.in_app_site(QUrl("https://www.example.com/next"))
          and not window.in_app_site(QUrl("https://elsewhere.org/")))
    window.close()


def test_page_fullscreen_restores_the_window(app) -> None:
    """Leaving a page's own fullscreen restores the window as it was."""
    window, _, _ = make_window(app, "t-webfull")
    window.new_tab(page("a"))
    window.apply_app_mode()
    window.showMaximized()
    wait(app, 0.5)
    window.set_web_fullscreen(True)
    wait(app, 0.5)
    window.set_web_fullscreen(False)
    wait(app, 0.5)
    check("an app window gets no tab strip back after a page's fullscreen",
          not window.tabs._bar_visible)
    check("and a maximised window comes back maximised", window.isMaximized())
    window.close()


def test_stream_lookup_is_quick(app) -> None:
    """yt-dlp is asked every way at once, the page more than once."""
    import stat
    import time as _time

    from merlin import media as _media

    folder = tempfile.mkdtemp(prefix="slow-ytdlp-")
    fake = os.path.join(folder, "yt-dlp")
    with open(fake, "w") as handle:
        handle.write(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "a = ' '.join(sys.argv[1:])\n"
            "if 'player_client' not in a:\n"
            "    time.sleep(1); print('https://example.invalid/s.m3u8'); sys.exit(0)\n"
            "time.sleep(5); sys.exit(1)\n")
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    saved = (_media.ytdlp_path, _media.deno_path, _media._ytdlp_argv)
    _media.ytdlp_path = lambda: fake
    _media.deno_path = lambda: "/bin/true"
    _media._ytdlp_argv = lambda p: [p]
    try:
        began = _time.monotonic()
        ok, _found = _media.resolve_stream("https://www.youtube.com/watch?v=q")
        took = _time.monotonic() - began
        check("the way that works answers without waiting for the ones that fail",
              ok and took < 3.5, f"{took:.1f}s")
    finally:
        _media.ytdlp_path, _media.deno_path, _media._ytdlp_argv = saved

    window, _, _ = make_window(app, "t-late")
    page_url = "https://www.youtube.com/watch?v=LATE"
    original = BrowserWindow.__dict__["_is_youtube_video"]
    BrowserWindow._is_youtube_video = staticmethod(lambda url: True)
    asked, played = [], []
    window._play_with_ytdlp = lambda page: asked.append(page)
    window._on_stream_resolved = lambda page, ok, result: played.append(result)
    try:
        view = window.new_tab()
        view.setHtml(
            "<div id=movie_player></div><script>setTimeout(function(){"
            "document.getElementById('movie_player').getPlayerResponse=function(){"
            "return {videoDetails:{videoId:'LATE'},"
            "streamingData:{hlsManifestUrl:'https://example.invalid/late.m3u8'}};};"
            "}, 2500);</script>", QUrl(page_url))
        wait(app, 1.0)
        window.play_stream(page_url)
        wait(app, 6.0)
        check("an address that reaches the page a little late is still used",
              played == ["https://example.invalid/late.m3u8"] and not asked,
              f"played {played}, yt-dlp {asked}")
    finally:
        BrowserWindow._is_youtube_video = original
        window.close()


def test_stream_failure_is_not_a_box(app) -> None:
    """No stream: the bar says so and offers to try again; nothing modal."""
    window, _, _ = make_window(app, "t-nobox")
    boxes = []
    saved = QMessageBox.information
    QMessageBox.information = lambda *a, **k: boxes.append(a)
    try:
        window._on_stream_resolved("https://www.youtube.com/watch?v=n", False, "refused")
        check("a stream that cannot be found is reported in the bar",
              window.notice_bar.isVisible()
              and window.notice_bar.action.text() == "Try again")
        check("without a box that stops everything", not boxes)
    finally:
        QMessageBox.information = saved
        window.close()


def test_stalled_stream_recovers(app) -> None:
    """A stream that gives no picture moves to yt-dlp, then to the bar."""
    import http.server
    import threading

    from merlin import media as _media
    from merlin.inplace import PlayerControls

    # the trace finds where a stream is refused
    folder = tempfile.mkdtemp(prefix="trace-")
    with open(os.path.join(folder, "top.m3u8"), "w") as handle:
        handle.write("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlevel.m3u8\n")
    with open(os.path.join(folder, "level.m3u8"), "w") as handle:
        handle.write("#EXTM3U\n#EXTINF:2,\nseg.ts\n")

    class Refusing(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=folder, **k)

        def do_GET(self):
            if self.path.endswith(".ts"):
                self.send_error(403)
                return
            super().do_GET()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refusing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        lines = _media.trace_stream(f"http://127.0.0.1:{server.server_address[1]}/top.m3u8")
        check("the log shows the stream refused at its first segment",
              lines[-1] == "first segment: refused, HTTP 403", " ; ".join(lines))
    finally:
        server.shutdown()

    # an error stays on the controls until a picture arrives
    controls = PlayerControls()
    controls.say("Merlin's player could not play this: refused")
    controls.set_progress(1000, 0, False)
    check("an error is not wiped by the next tick of the clock",
          "could not play" in controls.message)
    controls.picture_arrived()
    check("and clears once a picture arrives", controls.message == "")

    # a stall on the page's address tries yt-dlp; on yt-dlp's, the bar
    window, _, _ = make_window(app, "t-stall")
    page_url = "https://www.youtube.com/watch?v=STALL"
    view = window.new_tab()
    view.setHtml("<p>x</p>", QUrl(page_url))
    wait(app, 1.0)
    tried = []
    window._play_with_ytdlp = lambda page: tried.append(page)
    window._on_stalled(view, page_url, "page")
    wait(app, 2.0)
    check("a stall on the page's address tries yt-dlp's instead", tried == [page_url])
    window._on_stalled(view, page_url, "yt-dlp")
    check("a stall on yt-dlp's too ends in the bar, with Try again",
          window.notice_bar.isVisible() and window.notice_bar.action.text() == "Try again")
    window.close()


def test_youtube_cookies_for_ytdlp(app) -> None:
    """YouTube's cookies are kept safely and handed to yt-dlp, then deleted."""
    import glob
    import stat

    from PyQt6.QtCore import QByteArray
    from PyQt6.QtNetwork import QNetworkCookie

    from merlin import media as _media

    window, _, _ = make_window(app, "t-cookies")
    profile = window.profile
    window.new_tab(page("a"))
    wait(app, 1.0)
    _media._COOKIES.clear()
    _media.watch_cookies(profile.cookieStore())
    for domain, name in ((".youtube.com", "VISITOR_INFO1_LIVE"), (".example.org", "other")):
        cookie = QNetworkCookie(QByteArray(name.encode()), QByteArray(b"v"))
        cookie.setDomain(domain)
        cookie.setPath("/")
        profile.cookieStore().setCookie(cookie, QUrl(f"https://{domain.lstrip('.')}/"))
    wait(app, 1.5)
    # reading them later read freed memory and crashed Merlin; copied now
    rows = _media.cookies_snapshot()
    check("YouTube's cookies are kept, and read safely later",
          [r["name"] for r in rows] == ["VISITOR_INFO1_LIVE"], str(rows))

    folder = tempfile.mkdtemp(prefix="cookie-ytdlp-")
    fake = os.path.join(folder, "yt-dlp")
    with open(fake, "w") as handle:
        handle.write(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "a = sys.argv[1:]\n"
            "text = open(a[a.index('--cookies') + 1]).read() if '--cookies' in a else ''\n"
            "if 'VISITOR_INFO1_LIVE' in text:\n"
            "    print('https://example.invalid/c.m3u8'); sys.exit(0)\n"
            "sys.stderr.write('ERROR: The page needs to be reloaded.\\n'); sys.exit(1)\n")
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    saved = (_media.ytdlp_path, _media.deno_path, _media._ytdlp_argv)
    _media.ytdlp_path = lambda: fake
    _media.deno_path = lambda: "/bin/true"
    _media._ytdlp_argv = lambda p: [p]
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "merlin-yt-*")))
    try:
        ok, _found = _media.resolve_stream("https://www.youtube.com/watch?v=k", cookies=rows)
        check("yt-dlp is given the page's YouTube cookies", ok)
        left = set(glob.glob(os.path.join(tempfile.gettempdir(), "merlin-yt-*"))) - before
        check("and the cookie file is deleted afterwards", not left, str(left))
    finally:
        _media.ytdlp_path, _media.deno_path, _media._ytdlp_argv = saved
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
                 test_gestures, test_local_addresses, test_app_mode,
                 test_decorations_per_window, test_address_bar_selects,
                 test_theme_after_closing_tabs, test_live_stream_offer,
                 test_builtin_player_decodes_h264,
                 test_youtube_needs_a_runtime,
                 test_slot_errors_are_not_fatal, test_notice_bar_is_readable,
                 test_closing_is_prompt, test_youtube_asks_for_hls_first,
                 test_released_views_are_dead, test_codec_engine_swap_is_safe,
                 test_engine_download_is_checked,
                 test_engine_update_bookkeeping, test_stream_taken_from_the_page,
                 test_player_plays_in_the_page, test_player_controls,
                 test_filter_options_read_for_what_they_mean,
                 test_blocked_list_and_app_links,
                 test_page_fullscreen_restores_the_window,
                 test_stream_lookup_is_quick, test_stream_failure_is_not_a_box,
                 test_stalled_stream_recovers, test_youtube_cookies_for_ytdlp):
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
