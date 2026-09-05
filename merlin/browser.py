"""Merlin Browser main window."""
from __future__ import annotations

import os

import time

from PyQt6.QtCore import (
    QEvent, QSize, QStandardPaths, QStringListModel, Qt, QTimer, QUrl,
)
from PyQt6.QtGui import QAction, QCursor, QDesktopServices, QIcon, QKeySequence, QShortcut
from PyQt6.QtWebEngineCore import (
    QWebEngineProfile,
    QWebEnginePage,
    QWebEngineScript,
    QWebEngineSettings,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication, QCompleter, QFileDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QSizePolicy, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from . import adblock, icons, media, settings as cfg
from .gestures import SwipeNavigator
from .updater import RELEASES_URL, Updater
from . import webapps
from .playertab import PlayerTab
from .store import Bookmarks, History
from .swipeui import SwipeIndicator
from .tabs import TabContainer
from .ui import (
    FindBar, SettingsDialog, WindowButtons, apply_font, apply_theme, icon,
    start_page_html,
)

RESIZE_MARGIN = 6
from .brand import (
    APP_BLURB, APP_NAME, APP_SCHEME, APP_VERSION, START_URLS,
)


# --------------------------------------------------------------------- page
class WebPage(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile, window: "BrowserWindow", parent=None):
        super().__init__(profile, parent)
        self.window_ref = window
        self._cosmetic_host = None
        if hasattr(self, "certificateError"):
            try:
                self.certificateError.connect(self._on_certificate_error)
            except (AttributeError, TypeError):
                pass

    # cosmetic filtering is applied per navigation, before the load starts
    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame):  # noqa: N802
        if url.scheme() == APP_SCHEME and url.host() == "addtile":
            QTimer.singleShot(0, self.window_ref.add_start_tile)
            return False
        if url.scheme() == APP_SCHEME and url.host() == "listen":
            QTimer.singleShot(0, self.window_ref.start_dictation)
            return False
        if is_main_frame:
            self.apply_cosmetic(url.host())
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def apply_cosmetic(self, host: str) -> None:
        settings = self.window_ref.settings
        if not settings.get("cosmetic_filtering") or not settings.shields_enabled_for(host):
            css = ""
        else:
            css = self.window_ref.cosmetic_css_for(host)
        if host == self._cosmetic_host:
            return
        self._cosmetic_host = host
        collection = self.scripts()
        for script in list(collection.find("merlin-cosmetic")):
            collection.remove(script)
        if not css:
            return
        from .ui import user_stylesheet_script

        collection.insert(user_stylesheet_script(css, "merlin-cosmetic"))

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        return  # keep the terminal quiet

    def _on_certificate_error(self, error):
        try:
            description = error.description()
        except AttributeError:
            description = "Certificate error"
        answer = QMessageBox.warning(
            self.window_ref,
            "Certificate problem",
            f"{description}\n\n{error.url().toString()}\n\nContinue anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            error.acceptCertificate()
        else:
            error.rejectCertificate()


class WebView(QWebEngineView):
    def __init__(self, window: "BrowserWindow", profile: QWebEngineProfile, parent=None):
        super().__init__(parent)
        self.window_ref = window
        page = WebPage(profile, window, self)
        self.setPage(page)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.configure(page)
        page.fullScreenRequested.connect(self._on_fullscreen)
        if hasattr(page, "featurePermissionRequested"):
            page.featurePermissionRequested.connect(self._on_permission)

    @staticmethod
    def configure(page: QWebEnginePage) -> None:
        attr = QWebEngineSettings.WebAttribute
        s = page.settings()
        for flag, value in (
            (attr.JavascriptEnabled, True),
            (attr.LocalStorageEnabled, True),
            (attr.FullScreenSupportEnabled, True),
            (attr.ScrollAnimatorEnabled, True),
            (attr.PdfViewerEnabled, True),
            (attr.PluginsEnabled, True),
            (attr.DnsPrefetchEnabled, True),
            (attr.ScreenCaptureEnabled, True),
            (attr.WebGLEnabled, True),
            (attr.Accelerated2dCanvasEnabled, True),
            (attr.JavascriptCanOpenWindows, True),
            (attr.LocalContentCanAccessRemoteUrls, False),
            (attr.AllowRunningInsecureContent, False),
            (attr.PlaybackRequiresUserGesture, True),
        ):
            try:
                s.setAttribute(flag, value)
            except (AttributeError, TypeError):
                pass

    def contextMenuEvent(self, event):  # noqa: N802
        menu = self.createStandardContextMenu()
        request = self.lastContextMenuRequest()

        from . import passwords as _pw

        page_url = self.url().toString() if hasattr(self, "url") else ""
        if _pw.for_host(page_url):
            fill = menu.addAction("Fill saved login")
            fill.triggered.connect(
                lambda _c=False: self.window_ref.fill_saved_login())
            menu.insertAction(menu.actions()[0], fill)

        selected = (request.selectedText() if request is not None else "").strip()
        if selected:
            engine = self.window_ref.settings.get("search_engine")
            shown = selected if len(selected) <= 32 else selected[:31] + "\u2026"
            action = menu.addAction(f'Search {engine} for "{shown}"')
            action.triggered.connect(
                lambda _checked=False, text=selected:
                    self.window_ref.search_selection(text))
            menu.insertAction(menu.actions()[0], action)
            menu.insertSeparator(menu.actions()[1])

        if request is not None and self.window_ref.settings.get("player_mode") != "off":
            media_url = request.mediaUrl().toString()
            link_url = request.linkUrl().toString()
            target = media_url or link_url or self.url().toString()
            menu.addSeparator()
            label = ("Play video with Merlin's player" if media_url
                     else "Open link with Merlin's player" if link_url
                     else "Play this page with Merlin's player")
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, u=target: self.window_ref.open_in_player(u))
        menu.exec(event.globalPos())

    def createWindow(self, window_type):  # noqa: N802
        # focus_url is skipped for these: the page is about to supply an
        # address, and focusing an empty bar suppressed it
        if window_type == QWebEnginePage.WebWindowType.WebBrowserBackgroundTab:
            return self.window_ref.new_tab(background=True)
        return self.window_ref.new_tab(focus_address=False)

    def _on_fullscreen(self, request):
        request.accept()
        self.window_ref.set_web_fullscreen(request.toggleOn())

    def _on_permission(self, origin, feature):
        page = self.page()
        names = {
            QWebEnginePage.Feature.Geolocation: "know your location",
            QWebEnginePage.Feature.MediaAudioCapture: "use your microphone",
            QWebEnginePage.Feature.MediaVideoCapture: "use your camera",
            QWebEnginePage.Feature.MediaAudioVideoCapture: "use your camera and microphone",
            QWebEnginePage.Feature.Notifications: "send notifications",
            QWebEnginePage.Feature.DesktopVideoCapture: "capture your screen",
            QWebEnginePage.Feature.DesktopAudioVideoCapture: "capture your screen and audio",
        }
        what = names.get(feature, "use a device feature")
        answer = QMessageBox.question(
            self.window_ref, "Permission request",
            f"Allow {origin.host()} to {what}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        policy = (QWebEnginePage.PermissionPolicy.PermissionGrantedByUser
                  if answer == QMessageBox.StandardButton.Yes
                  else QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)
        page.setFeaturePermission(origin, feature, policy)


# ------------------------------------------------------------------- window
class BrowserWindow(QMainWindow):
    def __init__(self, app: QApplication, settings: cfg.Settings,
                 profile: QWebEngineProfile, filter_engine, filter_loader,
                 interceptor, history: History, bookmarks: Bookmarks,
                 private: bool = False, via_tor: bool = False):
        super().__init__()
        self.app = app
        self.settings = settings
        self.profile = profile
        self.filter_engine = filter_engine
        self.filter_loader = filter_loader
        self.interceptor = interceptor
        self.history = history
        self.bookmarks = bookmarks
        self.private = private
        self.via_tor = via_tor
        self._frameless = False
        self._pseudo_max = False
        self._restore_rect = None
        self._web_fullscreen = False
        self._cosmetic_cache: dict[str, str] = {}
        self._blocked_session = 0
        self._codec_probe: dict = {}
        self.updater = Updater(self.settings, self)
        self.updater.available.connect(self._on_update_available)

        suffix = " (Tor)" if via_tor else (" (Private)" if private else "")
        self.setWindowTitle(f"{APP_NAME} Browser{suffix}")
        # The window's own icon is what the taskbar uses for a running window,
        # so it is loaded from the file directly if the application-level one
        # was never set, rather than depending on start-up order.
        window_icon = self.app.windowIcon()
        if window_icon.isNull():
            from .brand import app_icon

            window_icon = app_icon()
        if not window_icon.isNull():
            self.setWindowIcon(window_icon)
        self.setMinimumSize(QSize(420, 320))
        self.setMouseTracking(True)

        self._build_ui()
        self._build_shortcuts()

        self.settings.changed.connect(self._on_setting_changed)
        self.interceptor.blocked.connect(self._on_blocked)

        self.apply_decorations(self.settings.get("hide_window_decorations"))
        self.restore_geometry()

    # ---------------------------------------------------------------- build
    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setMouseTracking(True)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toolbar = QToolBar("Navigation", self)
        self.toolbar.setMovable(False)
        self.toolbar.setIconSize(QSize(18, 18))
        self.toolbar.setMouseTracking(True)

        self.act_back = QAction("Back", self)
        self.act_forward = QAction("Forward", self)
        self.act_reload = QAction("Reload", self)
        self.act_home = QAction("Home", self)
        self.act_back.triggered.connect(lambda: self._current_do("back"))
        self.act_forward.triggered.connect(lambda: self._current_do("forward"))
        self.act_reload.triggered.connect(self.reload_or_stop)
        self.act_home.triggered.connect(
            lambda: self.navigate(self.settings.get("home_page")))
        for action in (self.act_back, self.act_forward, self.act_reload,
                       self.act_home):
            self.toolbar.addAction(action)

        # single actions live left of the address bar, menus to the right
        self.btn_bookmark = QToolButton(self)
        self.btn_bookmark.setToolTip("Bookmark this page (Ctrl+D)")
        self.btn_bookmark.clicked.connect(self.toggle_bookmark)
        self.toolbar.addWidget(self.btn_bookmark)

        self.url_bar = QLineEdit(self)
        self.url_bar.setPlaceholderText("Search or enter address")
        self.url_bar.setClearButtonEnabled(True)
        self.url_bar.returnPressed.connect(
            lambda: self.navigate(self.url_bar.text()))
        self.url_bar.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Fixed)
        self._install_completer()
        self.toolbar.addWidget(self.url_bar)

        self.btn_shields = QToolButton(self)
        self.btn_shields.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_shields.setText("0")
        self.btn_shields.setToolTip("Shields")
        self.btn_shields.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_shields.setMenu(self._shields_menu())
        self.toolbar.addWidget(self.btn_shields)

        self.btn_bookmarks = QToolButton(self)
        self.btn_bookmarks.setToolTip("Bookmarks (Ctrl+Shift+O)")
        self.btn_bookmarks.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.bookmarks_menu = QMenu(self)
        self.btn_bookmarks.setMenu(self.bookmarks_menu)
        self.toolbar.addWidget(self.btn_bookmarks)

        self.btn_history = QToolButton(self)
        self.btn_history.setToolTip("History (Ctrl+H)")
        self.btn_history.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.history_menu = QMenu(self)
        self.history_menu.aboutToShow.connect(self._fill_history_menu)
        self.btn_history.setMenu(self.history_menu)

        self.btn_downloads = QToolButton(self)
        self.btn_downloads.setToolTip("Downloads (Ctrl+J)")
        self.btn_downloads.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.downloads_menu = QMenu(self)
        self.btn_downloads.setMenu(self.downloads_menu)

        self.btn_menu = QToolButton(self)
        self.btn_menu.setToolTip("Menu")
        self.btn_menu.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_menu.setMenu(self._main_menu())
        self.toolbar.addWidget(self.btn_history)
        self.toolbar.addWidget(self.btn_downloads)
        self.toolbar.addWidget(self.btn_menu)

        self.window_buttons = WindowButtons(self, self)
        self.window_buttons_action = self.toolbar.addWidget(self.window_buttons)
        self.window_buttons_action.setVisible(False)

        self.tabs = TabContainer(self)
        self.tabs.setMouseTracking(True)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.newTabRequested.connect(lambda: self.new_tab())
        self.tabs.tabDetached.connect(self.detach_tab)
        self.tabs.setOrientation(self.settings.get("tab_orientation", "horizontal"))
        self.tabs.set_smooth_corners(self.settings.get("smooth_corners", True))
        self.tabs.set_corner_radius(self.settings.get("page_corner_radius", 10))
        self.tabs.set_palette("dark" if self.settings.get("dark_ui") else "light")
        for bar in (self.tabs.h_bar, self.tabs.v_strip):
            bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            bar.customContextMenuRequested.connect(
                lambda pos, b=bar: self._tab_context_menu(b.mapToGlobal(pos)))

        self.swipe_indicator = SwipeIndicator(self)

        self.find_bar = FindBar(self)
        self.find_bar.setVisible(False)
        self.find_bar.search.connect(self._do_find)
        self.find_bar.closed.connect(self.hide_find)

        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.find_bar)
        self.setCentralWidget(central)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)
        self.insertToolBarBreak(self.toolbar)

        self.apply_icons()
        self._fill_bookmarks_menu()
        self.downloads: list[dict] = []
        self._fill_downloads_menu()

        self.status = self.statusBar()
        self.status_label = QLabel("", self)
        self.status.addWidget(self.status_label, 1)

        # QStatusBar draws a frame around every item it holds, which showed as
        # a grey divider to the left of the clock in both themes.
        self.status.setStyleSheet("QStatusBar::item { border: 0px; }")
        self.clock_label = QLabel("", self)
        self.status.addPermanentWidget(self.clock_label)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(10_000)
        self._clock_timer.timeout.connect(self._tick)
        self._clock_timer.start()
        self._style_clock()
        self._tick()
        if self.via_tor:
            marker = QLabel("  Tor  ", self)
            marker.setToolTip("Traffic in this window goes through Tor")
            marker.setStyleSheet(
                "background:#7d4698; color:#ffffff; border-radius:6px;"
                " padding:1px 8px; font-weight:600;")
            self.status.addPermanentWidget(marker)
        # no permanent engine label: the status bar is for what you are doing
        # right now, and "Chromium engine" never changes

    def _install_completer(self) -> None:
        """Build the address bar's history model.

        This used to run after every single page load: a fresh sqlite query
        plus a new QCompleter over a couple of thousand URLs, on the UI thread,
        every time anything finished loading. It now uses one model that is
        refreshed at most every 30 seconds.
        """
        self._completer_model = QStringListModel(self.history.suggestions(), self)
        completer = QCompleter(self._completer_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(12)
        self.url_bar.setCompleter(completer)
        self._completer_stamp = time.monotonic()

    def _refresh_completer(self) -> None:
        if time.monotonic() - getattr(self, "_completer_stamp", 0) < 30:
            return
        self._completer_stamp = time.monotonic()
        model = getattr(self, "_completer_model", None)
        if model is not None:
            model.setStringList(self.history.suggestions())

    def _shields_menu(self) -> QMenu:
        menu = QMenu(self)
        self.act_shields_site = QAction("Shields up for this site", self, checkable=True)
        self.act_shields_site.triggered.connect(self._toggle_site_shields)
        menu.addAction(self.act_shields_site)
        menu.addSeparator()
        for label, key in (
            ("Block ads and trackers", "adblock_enabled"),
            ("Cosmetic filtering", "cosmetic_filtering"),
            ("Block third-party cookies", "block_third_party_cookies"),
            ("HTTPS upgrade", "https_upgrade"),
            ("Send Do Not Track", "send_do_not_track"),
        ):
            action = QAction(label, self, checkable=True)
            action.setChecked(bool(self.settings.get(key)))
            action.triggered.connect(
                lambda checked, k=key: self.settings.set(k, checked))
            menu.addAction(action)
        menu.addSeparator()
        update = QAction("Update filter lists", self)
        update.triggered.connect(lambda: self.filter_loader.refresh_async())
        menu.addAction(update)
        menu.aboutToShow.connect(self._sync_shields_menu)
        return menu

    def _sync_shields_menu(self) -> None:
        host = self._current_host()
        self.act_shields_site.setChecked(self.settings.shields_enabled_for(host))
        self.act_shields_site.setText(
            f"Shields up for {host}" if host else "Shields up for this site")

    def _main_menu(self) -> QMenu:
        menu = QMenu(self)

        def add(label, slot, shortcut=""):
            action = QAction(label, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            menu.addAction(action)
            return action

        add("New tab", lambda: self.new_tab(), "Ctrl+T")
        add("New window", self.new_window, "Ctrl+N")
        add("New private window", self.new_private_window, "Ctrl+Shift+N")
        add("New Tor window", self.new_tor_window)
        menu.addSeparator()

        self.act_dark = QAction("Dark mode", self, checkable=True)
        self.act_dark.setChecked(bool(self.settings.get("dark_ui")))
        self.act_dark.setShortcut(QKeySequence("Ctrl+Shift+L"))
        def dark_toggled(checked):
            # taking the switch yourself turns off following the clock
            self.settings.set("theme_mode", "manual")
            self.settings.set("dark_ui", checked)

        self.act_dark.triggered.connect(dark_toggled)
        menu.addAction(self.act_dark)
        menu.addSeparator()

        self.act_decorations = QAction("Hide window decorations", self, checkable=True)
        self.act_decorations.setShortcut(QKeySequence("Ctrl+Shift+D"))
        self.act_decorations.setChecked(bool(self.settings.get("hide_window_decorations")))
        self.act_decorations.triggered.connect(
            lambda checked: self.settings.set("hide_window_decorations", checked))
        menu.addAction(self.act_decorations)

        add("Full screen", self.toggle_fullscreen, "F11")
        menu.addSeparator()
        add("Find in page", self.show_find, "Ctrl+F")
        add("Zoom in", lambda: self.zoom(0.1), "Ctrl+=")
        add("Zoom out", lambda: self.zoom(-0.1), "Ctrl+-")
        add("Reset zoom", lambda: self.zoom(0), "Ctrl+0")
        menu.addSeparator()
        add("Play page with Merlin's player", lambda: self.open_in_player(),
            "Ctrl+Shift+P")
        add("Fill a saved login", self.fill_saved_login)
        add("Import from another browser", self.import_from_browser)
        add("Install this page as an app", self.install_web_app)
        add("Codec report", self.show_codecs)
        add("History", self.show_history, "Ctrl+H")
        add("Bookmarks", self.show_bookmarks, "Ctrl+Shift+O")
        add("View page source", self.view_source, "Ctrl+U")
        menu.addSeparator()
        add("Settings", self.show_settings, "Ctrl+,")
        add("About Merlin", self.show_about)
        menu.addSeparator()
        add("Quit", self.app.quit, "Ctrl+Q")
        return menu

    def _build_shortcuts(self) -> None:
        pairs = [
            ("Ctrl+T", lambda: self.new_tab()),
            ("Ctrl+W", lambda: self.close_tab(self.tabs.currentIndex())),
            ("Ctrl+Shift+T", self.reopen_tab),
            ("Ctrl+L", self.focus_url),
            ("Alt+D", self.focus_url),
            ("F6", self.focus_url),
            ("Ctrl+R", self.reload_or_stop),
            ("F5", self.reload_or_stop),
            ("Ctrl+Shift+R", lambda: self._current_do("reload_bypass")),
            ("F11", self.toggle_fullscreen),
            ("Ctrl+Shift+D", lambda: self.settings.toggle("hide_window_decorations")),
            ("Ctrl+Shift+L", lambda: self.settings.toggle("dark_ui")),
            ("Ctrl+Shift+I", lambda: self.toggle_pin()),
            ("Ctrl+J", self.show_downloads),
            ("Ctrl+Shift+S", self.start_dictation),
            ("Ctrl+F", self.show_find),
            ("Escape", self.on_escape),
            ("Ctrl+D", self.toggle_bookmark),
            ("Ctrl+H", self.show_history),
            ("Ctrl+Shift+P", lambda: self.open_in_player()),
            ("Ctrl+U", self.view_source),
            ("Ctrl+,", self.show_settings),
            ("Alt+Left", lambda: self._current_do("back")),
            ("Alt+Right", lambda: self._current_do("forward")),
            ("Ctrl+Tab", lambda: self.cycle_tab(1)),
            ("Ctrl+Shift+Tab", lambda: self.cycle_tab(-1)),
        ]
        for sequence, slot in pairs:
            QShortcut(QKeySequence(sequence), self, activated=slot)
        for i in range(1, 9):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self,
                      activated=lambda idx=i - 1: self.tabs.setCurrentIndex(idx))
        self._closed_tabs: list[str] = []

    def is_maximised(self) -> bool:
        return self.isMaximized() or self._pseudo_max

    def toggle_maximise(self) -> None:
        """Maximise, avoiding showMaximized() while frameless.

        A frameless window handed to showMaximized() is the one arrangement
        that was reported to crash, and it is also the one that covers the
        taskbar, because there is no frame for the shell to account for.
        Setting the geometry to the screen's available area instead sidesteps
        both: the taskbar stays visible and no window-manager negotiation over
        a frame that does not exist is involved.
        """
        try:
            if not self._frameless:
                if self.isMaximized():
                    self.showNormal()
                else:
                    self.showMaximized()
                return

            if self._pseudo_max:
                if self._restore_rect is not None:
                    self.setGeometry(self._restore_rect)
                self._restore_rect = None
                self._pseudo_max = False
            else:
                self._restore_rect = self.geometry()
                screen = self.screen() or QApplication.primaryScreen()
                if screen is not None:
                    self.setGeometry(screen.availableGeometry())
                self._pseudo_max = True
            self._update_max_button()
        except Exception as exc:                         # noqa: BLE001
            self.status_label.setText(f"Could not resize the window: {exc}")

    def _update_max_button(self) -> None:
        buttons = getattr(self, "window_buttons", None)
        if buttons is None:
            return
        dark = bool(self.settings.get("dark_ui"))
        buttons.btn_max.setIcon(icons.coloured_icon(
            "restore" if self.is_maximised() else "maximise",
            icons.DARK_FG if dark else icons.LIGHT_FG, 14))

    def apply_icons(self) -> None:
        """Repaint every toolbar icon in the current theme's foreground."""
        dark = bool(self.settings.get("dark_ui"))
        self.toolbar.setIconSize(icons.icon_size())
        for action, name in (
            (self.act_back, "back"), (self.act_forward, "forward"),
            (self.act_reload, "reload"), (self.act_home, "home"),
        ):
            action.setIcon(icons.themed_icon(name, dark))
        self.btn_menu.setIcon(icons.themed_icon("menu", dark))
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            name = widget.property("merlin_icon") if isinstance(widget, WebView) else None
            if name:
                self.tabs.setTabIcon(i, icons.themed_icon(name, dark, 16))
        self.btn_bookmarks.setIcon(icons.themed_icon("bookmarks", dark))
        self.btn_downloads.setIcon(icons.themed_icon("download", dark))
        self.btn_history.setIcon(icons.themed_icon("history", dark))
        self._style_clock(dark)
        self.btn_shields.setIcon(icons.themed_icon("shield", dark))
        self._update_bookmark_button()
        for button, name in (
            (self.window_buttons.btn_min, "minimise"),
            (self.window_buttons.btn_max, "maximise"),
            (self.window_buttons.btn_close, "close"),
        ):
            colour = "#ffffff" if name == "close" else (
                icons.DARK_FG if dark else icons.LIGHT_FG)
            button.setText("")
            button.setIcon(icons.coloured_icon(name, colour, 14))

    # ------------------------------------------------------------ bookmarks
    def _fill_bookmarks_menu(self) -> None:
        """Rebuild the bookmarks menu.

        Called when the bookmarks change, not from aboutToShow. Clearing a menu
        from inside its own aboutToShow deletes the QActions of a popup that may
        still be tearing down from the previous click, which is exactly the sort
        of thing that only shows up when someone clicks quickly.
        """
        menu = self.bookmarks_menu
        if menu.isVisible():
            return
        menu.clear()
        dark = bool(self.settings.get("dark_ui"))
        items = self.bookmarks.items
        if not items:
            empty = menu.addAction("No bookmarks yet")
            empty.setEnabled(False)
        for item in items[:40]:
            title = (item.get("title") or item["url"])[:60]
            action = menu.addAction(icons.themed_icon("star_full", dark), title)
            action.setToolTip(item["url"])
            action.triggered.connect(
                lambda _checked=False, u=item["url"]: self.navigate(u))
        menu.addSeparator()
        add = menu.addAction("Bookmark this page")
        add.triggered.connect(self.toggle_bookmark)
        show = menu.addAction("Open bookmarks page")
        show.triggered.connect(self.show_bookmarks)

    # ------------------------------------------------------------ decorations
    def apply_decorations(self, hide: bool) -> None:
        """Add or remove the system title bar without losing the session."""
        hide = bool(hide)
        if hide == self._frameless and self.isVisible():
            return
        self._frameless = hide
        was_visible = self.isVisible()
        geometry = self.geometry()
        maximized = self.isMaximized()

        flags = self.windowFlags()
        if hide:
            flags |= Qt.WindowType.FramelessWindowHint
        else:
            flags &= ~Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(flags)

        self.tabs.set_frameless(hide)
        self.window_buttons_action.setVisible(
            hide and bool(self.settings.get("show_window_buttons_when_frameless")))
        margin = RESIZE_MARGIN if hide else 0
        self.centralWidget().layout().setContentsMargins(margin, 0, margin, margin)

        if was_visible:
            # setWindowFlags hides the window; restore it exactly as it was
            self.show()
            if maximized:
                self.showMaximized()
            else:
                self.setGeometry(geometry)
            self.raise_()
            self.activateWindow()

    def _edges_at(self, pos):
        if not self._frameless or self.is_maximised() or self.isFullScreen():
            return None
        rect = self.rect()
        margin = RESIZE_MARGIN + 2
        edges = Qt.Edge(0)
        if pos.x() <= margin:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= rect.width() - margin:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= margin:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= rect.height() - margin:
            edges |= Qt.Edge.BottomEdge
        return edges if int(edges) else None

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        # The corner overlay is placed while the tabs are built, and then this
        # window is shown on top of it, so it ends up behind and the corners
        # look square until something raises it again. That is why hovering the
        # tab strip appeared to fix it. Re-place it once the window is really
        # on screen, and again shortly after for platforms that map windows
        # asynchronously.
        QTimer.singleShot(0, self._refresh_corners)
        QTimer.singleShot(350, self._refresh_corners)
        if not getattr(self, "_icon_applied", False):
            from . import winicon
            from .brand import icon_path

            # no-op off Windows, so this path is exercised by every test run
            self._icon_applied = winicon.apply_to_window(self, icon_path())

    def moveEvent(self, event):  # noqa: N802
        super().moveEvent(event)
        self._refresh_corners()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        # Another window taking focus, or this one being minimised and
        # restored, can leave the corner overlay behind or hidden. Anything
        # that changes this window's state re-places it.
        if event.type() in (QEvent.Type.ActivationChange,
                            QEvent.Type.WindowStateChange):
            self._refresh_corners()

    def _refresh_corners(self) -> None:
        tabs = getattr(self, "tabs", None)
        if tabs is None:
            return
        if self.isMinimized() or not self.isVisible():
            overlay = getattr(tabs, "_overlay", None)
            if overlay is not None:
                overlay.hide()
            return
        tabs._round_page()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        indicator = getattr(self, "swipe_indicator", None)
        if indicator is not None and indicator.isVisible():
            indicator._reposition()

    def mousePressEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                edges = self._edges_at(event.position().toPoint())
                if edges is not None:
                    handle = self.windowHandle()
                    if handle is not None:
                        handle.startSystemResize(edges)
                        return
        except Exception:                                # noqa: BLE001
            pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        try:
            edges = self._edges_at(event.position().toPoint())
        except Exception:                                # noqa: BLE001
            edges = None
        if edges is None:
            self.unsetCursor()
        else:
            left = bool(edges & Qt.Edge.LeftEdge)
            right = bool(edges & Qt.Edge.RightEdge)
            top = bool(edges & Qt.Edge.TopEdge)
            bottom = bool(edges & Qt.Edge.BottomEdge)
            if (left and top) or (right and bottom):
                shape = Qt.CursorShape.SizeFDiagCursor
            elif (right and top) or (left and bottom):
                shape = Qt.CursorShape.SizeBDiagCursor
            elif left or right:
                shape = Qt.CursorShape.SizeHorCursor
            else:
                shape = Qt.CursorShape.SizeVerCursor
            self.setCursor(QCursor(shape))
        super().mouseMoveEvent(event)

    # ------------------------------------------------------------------ tabs
    def new_tab(self, url: str | None = None, background: bool = False,
                defer: bool = False, focus_address: bool = True) -> WebView:
        """Open a tab. With defer, the page is not fetched until it is shown.

        Restoring a session used to load every tab at once: twenty tabs meant
        twenty simultaneous page loads competing for the network and the CPU
        before the browser felt usable. Deferred tabs cost a widget and nothing
        else until you click them.
        """
        view = WebView(self, self.profile, self)
        index = self.tabs.addTab(view, "New tab")
        view.titleChanged.connect(lambda title, v=view: self._on_title(v, title))
        view.iconChanged.connect(lambda ico, v=view: self._on_icon(v, ico))
        view.urlChanged.connect(lambda u, v=view: self._on_url(v, u))
        view.loadStarted.connect(lambda v=view: self._on_load_state(v, True))
        view.loadFinished.connect(lambda ok, v=view: self._on_load_state(v, False, ok))
        view.loadProgress.connect(lambda p, v=view: self._on_progress(v, p))
        view.page().linkHovered.connect(self._on_hover)
        view.loadFinished.connect(
            lambda ok, v=view: v.page().runJavaScript(media.MEDIA_ERROR_WATCH_JS)
            if ok else None)
        view.setZoomFactor(float(self.settings.get("default_zoom", 1.0)))

        if not background:
            self.tabs.setCurrentIndex(index)
        target = url if url is not None else self.settings.get("new_tab_page")
        if defer and background and target:
            # Restored tabs are not fetched until selected, so they have no
            # favicon and looked like empty rows: the window appeared to have
            # one tab. A placeholder marks them as pages waiting to open.
            self._set_page_icon(view, "pending")
            view.setProperty("pending_url", target)
            label = QUrl(target).host() or target
            self.tabs.setTabText(index, label[:40] or "Tab")
            self.tabs.setTabToolTip(index, target)
        else:
            self.load_in(view, target)
        if not background and focus_address:
            QTimer.singleShot(0, self.focus_url)
        return view

    def close_tab(self, index: int) -> None:
        if index < 0 or self.tabs.count() == 0:
            return
        view = self.tabs.widget(index)
        if isinstance(view, WebView):
            url = view.url().toString()
            if not url or url == "about:blank":
                # never opened, so its address is still only pending
                url = view.property("pending_url") or ""
            if url and url != "about:blank":
                self._closed_tabs.append(url)
        self.tabs.removeTab(index)
        if isinstance(view, WebView):
            # Stop the load and drop every connection to this window before
            # the view goes. Destroying a view that is still fetching a page,
            # or letting its signals arrive afterwards, is a way to take the
            # engine down with it.
            try:
                view.stop()
            except Exception:                            # noqa: BLE001
                pass
            try:
                view.disconnect()
            except Exception:                            # noqa: BLE001
                pass
        if isinstance(view, QWidget):
            view.deleteLater()
        if self.tabs.count() == 0:
            if self.settings.get("restore_session"):
                self.save_session()
            self.close()

    def reopen_tab(self) -> None:
        if self._closed_tabs:
            self.new_tab(self._closed_tabs.pop())

    def fill_saved_login(self) -> None:
        """Put a saved username and password into the form on this page."""
        from . import passwords

        view = self.current()
        if view is None or not hasattr(view, "url"):
            return
        url = view.url().toString()
        matches = passwords.for_host(url)
        if not matches:
            if not passwords.backend():
                QMessageBox.information(self, "No saved logins",
                                        passwords.backend_note())
            else:
                QMessageBox.information(
                    self, "No saved login",
                    f"Nothing saved for {passwords.host_of(url) or 'this site'}."
                    "\n\nImport logins from the menu, under Import from "
                    "another browser.")
            return

        entry = matches[0]
        if len(matches) > 1:
            from PyQt6.QtWidgets import QInputDialog

            names = [m.get("username") or "(no username)" for m in matches]
            chosen, ok = QInputDialog.getItem(
                self, "Which login?", f"Saved for {entry.get('host', '')}:",
                names, 0, False)
            if not ok:
                return
            entry = matches[names.index(chosen)]

        script = passwords.fill_script(entry.get("username", ""),
                                       entry.get("password", ""))
        view.page().runJavaScript(
            script, lambda outcome: self.status_label.setText(
                "Login filled" if outcome == "filled"
                else str(outcome or "could not fill this page")))

    def import_from_browser(self) -> None:
        """Bring bookmarks and history across from another browser."""
        from .importui import ImportDialog

        dialog = ImportDialog(self.settings, self.history, self.bookmarks, self)
        dialog.exec()
        self._refresh_corners()
        self._fill_bookmarks_menu()
        self._refresh_completer()

    def install_web_app(self) -> None:
        """Turn the current page into a standalone app with its own shortcut."""
        from PyQt6.QtWidgets import QInputDialog

        view = self.current()
        if view is None or not hasattr(view, "url"):
            return
        url = view.url().toString()
        if not url or url.startswith(("about:", "data:")):
            QMessageBox.information(
                self, "Nothing to install",
                "Open the page you want as an app first.")
            return

        suggested = view.title() or QUrl(url).host()
        name, ok = QInputDialog.getText(self, "Install as app", "App name:",
                                        text=suggested[:48])
        if not ok or not name.strip():
            return
        name = name.strip()

        slug = webapps.slugify(name)
        icon_path = webapps.save_icon(view.icon(), slug)
        installed, where = webapps.install(name, url, icon_path)
        if not installed:
            QMessageBox.warning(self, "Could not install", where)
            return

        entry = {"name": name, "url": url, "icon": icon_path, "shortcut": where}
        apps = [a for a in self.settings.get("web_apps", [])
                if a.get("shortcut") != where]
        apps.append(entry)
        self.settings.set("web_apps", apps)
        self.status_label.setText(f"Installed {name}")
        QMessageBox.information(
            self, "App installed",
            f"{name} was added to your applications.\n\n"
            "It opens in its own window, without browser chrome, and always "
            "loads the live site.")

    def detach_tab(self, index: int) -> None:
        """Pull a tab out into a window of its own.

        The view itself is not moved between windows: a QWebEngineView carries
        a page bound to this window's profile and reparenting it across
        top-level windows is unreliable. The address is opened in the new
        window and the original tab closes, which is what dragging a tab out
        looks like from the outside.
        """
        if not 0 <= index < self.tabs.count():
            return
        if self.tabs.count() <= 1:
            self.status_label.setText("The only tab cannot be pulled out")
            return
        # Building several browser windows without returning to the event loop
        # crashes the engine, so only one detach is started per turn. A drag
        # cannot produce two anyway; a repeating event could.
        if getattr(self, "_detaching", False):
            return
        self._detaching = True
        QTimer.singleShot(0, lambda: setattr(self, "_detaching", False))
        view = self.tabs.widget(index)
        url = view.url().toString() if hasattr(view, "url") else ""
        if not url or url.startswith("about:"):
            self.status_label.setText("Nothing to open in a new window yet")
            return

        try:
            view.stop()
        except Exception:                                # noqa: BLE001
            pass

        window = self.new_window(url)
        if window is None:
            return

        # Close the old tab on the next turn of the event loop. Destroying a
        # QWebEngineView in the middle of this call, while it is still loading
        # and while a second window is being built around the same profile,
        # crashes the engine; letting the current work finish first does not.
        def close_later(target=view):
            position = self.tabs.indexOf(target)
            if position >= 0 and self.tabs.count() > 1:
                self.close_tab(position)

        QTimer.singleShot(0, close_later)

    def adopt_urls(self, urls: list) -> None:
        """Open URLs handed over by another instance, and come to the front."""
        for url in urls:
            self.new_tab(url)
        if not urls:
            self.new_tab()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def toggle_pin(self, index: int | None = None) -> None:
        """Pin or unpin a tab, holding it at the start of the strip."""
        if index is None:
            index = self.tabs.currentIndex()
        if index < 0:
            return
        widget = self.tabs.widget(index)
        self.tabs.set_pinned(index, not self.tabs.is_pinned(index))
        moved = self.tabs.indexOf(widget)
        self.status_label.setText(
            "Tab pinned" if self.tabs.is_pinned(moved) else "Tab unpinned")

    def cycle_tab(self, delta: int) -> None:
        count = self.tabs.count()
        if count:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + delta) % count)

    def _tab_context_menu(self, global_pos) -> None:
        index = self.tabs.tab_at_global(global_pos)
        menu = QMenu(self)
        if index >= 0:
            pinned = self.tabs.is_pinned(index)
            menu.addAction("Unpin tab" if pinned else "Pin tab",
                           lambda: self.toggle_pin(index))
            menu.addSeparator()
            menu.addAction("Reload", lambda: self.tabs.widget(index).reload())
            menu.addAction("Duplicate", lambda: self.new_tab(
                self.tabs.widget(index).url().toString()))
            menu.addAction("Close", lambda: self.close_tab(index))
            menu.addAction("Close other tabs", lambda: self._close_others(index))
        menu.addAction("New tab", lambda: self.new_tab())
        menu.exec(global_pos)

    def _close_others(self, keep: int) -> None:
        for i in reversed(range(self.tabs.count())):
            if i != keep:
                self.close_tab(i)

    def current(self) -> WebView | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, WebView) else None

    def _current_do(self, what: str) -> None:
        view = self.current()
        if not view:
            return
        if what == "back":
            view.back()
        elif what == "forward":
            view.forward()
        elif what == "reload":
            view.reload()
        elif what == "reload_bypass":
            view.page().triggerAction(QWebEnginePage.WebAction.ReloadAndBypassCache)

    # ------------------------------------------------------------ navigation
    def navigate(self, text: str) -> None:
        view = self.current() or self.new_tab()
        self.load_in(view, text)

    def load_in(self, view: WebView, text: str) -> None:
        text = (text or "").strip()
        if not text or text in START_URLS:
            self.show_start_page(view)
            return
        url = self.normalise(text)
        if url is None:
            self.show_start_page(view)
            return
        if (media.looks_like_media(url.toString())
                and self.settings.get("player_mode", "embedded") != "off"):
            self.open_in_player(url.toString())
            return
        view.setProperty("merlin_start", False)
        view.setProperty("merlin_icon", None)
        view.setUrl(url)

    def normalise(self, text: str) -> QUrl | None:
        text = text.strip()
        if not text:
            return None
        if text in START_URLS:
            return None
        if os.path.exists(os.path.expanduser(text)):
            return QUrl.fromLocalFile(os.path.abspath(os.path.expanduser(text)))
        known_schemes = ("about:", "data:", "blob:", "file:", "mailto:", "ftp:",
                         "view-source:", "chrome:", "magnet:")
        if "://" in text or text.startswith(known_schemes):
            return QUrl(text)
        first = text.split("/")[0]
        host = first.split(":")[0]
        is_local = host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
        looks_like_host = is_local or (
            " " not in text and "." in first and not first.endswith(".")
            and not first.startswith(".")
        )
        if looks_like_host:
            return QUrl(("http://" if is_local else "https://") + text)
        return QUrl(self.settings.search_url(text))

    def search_selection(self, text: str, new_tab: bool = True) -> None:
        """Search the selected text with the current engine."""
        text = (text or "").strip()
        if not text:
            return
        url = self.settings.search_url(text)
        if new_tab:
            self.new_tab(url)
        else:
            self.navigate(url)

    def start_dictation(self) -> None:
        """Speak a search. Recognised locally, never sent anywhere."""
        from . import dictation

        if getattr(self, "_dictation", None) is not None \
                and self._dictation.listening():
            self._dictation.stop()          # pressed again: stop early
            return

        missing = dictation.what_is_missing(self.settings)
        if missing:
            detail = "\n".join(f"  - {item}" for item in missing)
            answer = QMessageBox.question(
                self, "Set up voice search?",
                "Speaking to search runs entirely on this computer: the audio "
                "is never sent anywhere.\n\nTo do that Merlin needs:\n"
                + detail
                + "\n\nFetch them now? It is a one-off, and needs a "
                  "connection.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._speech_setup = dictation.Setup(self.settings, self)
            self._speech_setup.progress.connect(self.status_label.setText)
            self._speech_setup.finished.connect(self._speech_setup_done)
            self.status_label.setText("Setting up voice search...")
            self._speech_setup.run()
            return

        self._begin_listening()

    def _speech_setup_done(self, ok: bool, message: str) -> None:
        self.status_label.setText(message)
        if ok:
            self._begin_listening()
        else:
            QMessageBox.warning(self, "Voice search not available", message)

    def _begin_listening(self) -> None:
        from . import dictation

        if getattr(self, "_dictation", None) is None:
            self._dictation = dictation.Dictation(self.settings, self)
            self._dictation.started.connect(
                lambda: self.status_label.setText("Listening... speak now"))
            self._dictation.finished.connect(self._dictation_result)
            self._dictation.failed.connect(
                lambda why: self.status_label.setText(why))
        self._dictation.start()

    def _dictation_result(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self.status_label.setText("Nothing was heard")
            return
        self.status_label.setText(f"Heard: {text}")
        self.navigate(self.settings.search_url(text))

    def add_start_tile(self) -> None:
        """Prompt for a shortcut, then refresh every open start page."""
        from PyQt6.QtWidgets import QInputDialog

        if len(self.settings.tiles()) >= cfg.MAX_START_TILES:
            QMessageBox.information(
                self, "Shortcuts full",
                f"The new tab page holds {cfg.MAX_START_TILES} shortcuts. "
                "Remove one in Settings, Shortcuts to make room.")
            return

        url, ok = QInputDialog.getText(
            self, "Add a shortcut", "Address:", text="https://")
        if not ok or not url.strip() or url.strip() == "https://":
            return
        target = self.normalise(url.strip())
        if target is None:
            return
        suggestion = target.host() or url.strip()
        if suggestion.startswith("www."):
            suggestion = suggestion[4:]
        title, ok = QInputDialog.getText(
            self, "Add a shortcut", "Name:", text=suggestion)
        if not ok:
            return
        if self.settings.add_tile(title, target.toString()):
            self.refresh_start_pages()

    def refresh_start_pages(self) -> None:
        for index in range(self.tabs.count()):
            view = self.tabs.widget(index)
            if isinstance(view, WebView) and view.property("merlin_start"):
                self.show_start_page(view)

    def show_start_page(self, view: WebView) -> None:
        html_text = start_page_html(self.settings, self._blocked_session,
                                    self.bookmarks.items)
        view.setHtml(html_text, QUrl("about:blank"))
        view.setProperty("merlin_start", True)
        self._set_page_icon(view, "home")

    def reload_or_stop(self) -> None:
        view = self.current()
        if not view:
            return
        if getattr(view, "_loading", False):
            view.stop()
        else:
            view.reload()

    # -------------------------------------------------------------- signals
    def _on_title(self, view: WebView, title: str) -> None:
        index = self.tabs.indexOf(view)
        if index >= 0:
            self.tabs.setTabText(index, (title or "New tab")[:40])
            self.tabs.setTabToolTip(index, title or "")
        if view is self.current():
            self.setWindowTitle(f"{title} - Merlin" if title else "Merlin Browser")
        self.history.update_title(view.url().toString(), title)

    def _set_page_icon(self, view: WebView, name: str) -> None:
        """Give an internally rendered page a themed tab icon.

        These pages have no favicon of their own, and a blank square in a
        narrow vertical strip identifies nothing.
        """
        view.setProperty("merlin_icon", name)
        index = self.tabs.indexOf(view)
        if index >= 0:
            self.tabs.setTabIcon(index, icons.themed_icon(
                name, bool(self.settings.get("dark_ui")), 16))

    def _on_icon(self, view: WebView, ico: QIcon) -> None:
        if view.property("merlin_icon"):
            return
        index = self.tabs.indexOf(view)
        if index >= 0 and not ico.isNull():
            self.tabs.setTabIcon(index, ico)

    def _on_url(self, view: WebView, url: QUrl) -> None:
        if url.scheme() in ("http", "https", "file", "ftp"):
            if view.property("merlin_start") or view.property("merlin_icon"):
                view.setProperty("merlin_start", False)
                view.setProperty("merlin_icon", None)
                index = self.tabs.indexOf(view)
                if index >= 0:
                    self.tabs.setTabIcon(index, QIcon())
        if view is self.current():
            # a real address always wins over an empty, freshly focused bar
            force = bool(url.toString()) and not self.url_bar.text().strip()
            self._update_url_bar(url, force=force)
            self._update_shield_badge()
            self._update_bookmark_button()
        if not view.property("merlin_start"):
            self.history.add(url.toString(), view.title())

    def _on_load_state(self, view: WebView, loading: bool, ok: bool = True) -> None:
        view._loading = loading
        if view is self.current():
            dark = bool(self.settings.get("dark_ui"))
            self.act_reload.setIcon(
                icons.themed_icon("stop" if loading else "reload", dark))
            self.act_reload.setToolTip("Stop" if loading else "Reload")
        if not loading:
            # The flag is NOT cleared here. setHtml fires loadFinished straight
            # away, so clearing it on load completion wiped it a moment after
            # the start page was drawn, and nothing could find those tabs again
            # to refresh them. It is cleared when navigating away instead.
            QTimer.singleShot(1500, lambda v=view: self._offer_player_for_failed_media(v))
            self._update_nav_actions()
            self._update_shield_badge()
            if view is self.current():
                self.status_label.setText("" if ok else "Load failed")
            self._refresh_completer()

    def _on_progress(self, view: WebView, progress: int) -> None:
        if view is self.current() and 0 < progress < 100:
            self.status_label.setText(f"Loading... {progress}%")
        elif view is self.current():
            self.status_label.setText("")

    def _on_hover(self, link: str) -> None:
        self.status_label.setText(link[:200])

    def _on_tab_changed(self, index: int) -> None:
        view = self.current()
        if not view:
            return
        pending = view.property("pending_url")
        if pending:
            view.setProperty("pending_url", None)
            # Start the load on the next turn of the event loop rather than
            # from inside currentChanged. Switching tabs again while a load
            # begins meant two loads starting from inside two signals, with
            # the engine still settling the first.
            QTimer.singleShot(
                0, lambda v=view, u=pending: self._load_when_still_open(v, u))
        self._update_url_bar(view.url())
        self._update_nav_actions()
        self._update_shield_badge()
        self._update_bookmark_button()
        self.setWindowTitle(f"{view.title()} - Merlin" if view.title() else "Merlin Browser")

    def view_is_alive(self, view) -> bool:
        """Is this view still a tab in this window?

        Anything deferred with a timer has to ask. A tab can be closed while
        the timer is pending, and touching the deleted C++ object then raises
        out of the timer callback, which ends the process. That is the crash
        after closing a tab and switching between two that were still loading.
        """
        if view is None:
            return False
        try:
            return self.tabs.indexOf(view) >= 0
        except RuntimeError:
            return False                                 # already destroyed

    def _load_when_still_open(self, view, url: str) -> None:
        """Load a restored tab, unless it was closed in the meantime."""
        if not self.view_is_alive(view):
            return
        self.load_in(view, url)

    def _on_blocked(self, host: str, url: str) -> None:
        self._blocked_session += 1
        view = self.current()
        if view and view.url().host() == host:
            self._update_shield_badge()

    def _on_setting_changed(self, key: str, value) -> None:
        if key == "hide_window_decorations":
            self.apply_decorations(value)
            if hasattr(self, "act_decorations"):
                self.act_decorations.setChecked(bool(value))
        elif key == "show_window_buttons_when_frameless":
            self.window_buttons_action.setVisible(self._frameless and bool(value))
        elif key == "tab_orientation":
            self.tabs.setOrientation(value)
        elif key == "page_corner_radius":
            self.tabs.set_corner_radius(value)
        elif key == "smooth_corners":
            self.tabs.set_smooth_corners(bool(value))
        elif key in ("show_clock", "theme_mode", "theme_light_hour",
                     "theme_dark_hour"):
            self._tick()
        elif key == "ui_font_pt":
            apply_font(self.app, int(value or 0))
        elif key == "dark_ui":
            apply_theme(self.app, bool(value))
            self.apply_icons()
            self.tabs.set_palette("dark" if value else "light")
            if hasattr(self, "act_dark"):
                self.act_dark.setChecked(bool(value))
        elif key in ("start_background", "start_tiles"):
            self.refresh_start_pages()
        elif key in ("adblock_enabled", "cosmetic_filtering", "shields_exceptions"):
            self._cosmetic_cache.clear()
            self._update_shield_badge()
        elif key == "block_third_party_cookies":
            self._apply_cookie_filter()

    # ------------------------------------------------------------- ui state
    def _update_url_bar(self, url: QUrl, force: bool = False) -> None:
        """Show a page's address.

        Focus normally suppresses this, so that a page loading in the
        background cannot overwrite what someone is in the middle of typing.
        A tab opened from a link is the exception: new_tab focuses the address
        bar, the link's URL arrives a moment later and was being dropped, so
        the bar stayed empty until you switched tabs and back.
        """
        if self.url_bar.hasFocus() and not force:
            return
        text = url.toString()
        if text in ("about:blank", ""):
            text = ""
        self.url_bar.setText(text)
        self.url_bar.setCursorPosition(0)

    def _update_nav_actions(self) -> None:
        view = self.current()
        if not view:
            return
        history = view.page().history()
        self.act_back.setEnabled(history.canGoBack())
        self.act_forward.setEnabled(history.canGoForward())

    def _current_host(self) -> str:
        view = self.current()
        return view.url().host() if view else ""

    def _update_shield_badge(self) -> None:
        host = self._current_host()
        count = self.interceptor.count_for(host) if host else 0
        enabled = self.settings.shields_enabled_for(host) if host else \
            bool(self.settings.get("adblock_enabled"))
        self.btn_shields.setText(str(count))
        self.btn_shields.setIcon(
            icons.themed_icon("shield" if enabled else "shield_off",
                              bool(self.settings.get("dark_ui"))))
        self.btn_shields.setToolTip(
            f"{count} requests blocked on {host}" if host else "Shields")

    def _update_bookmark_button(self) -> None:
        view = self.current()
        url = view.url().toString() if view else ""
        dark = bool(self.settings.get("dark_ui"))
        self.btn_bookmark.setIcon(icons.themed_icon(
            "star_full" if self.bookmarks.contains(url) else "star", dark))

    def _toggle_site_shields(self, checked: bool) -> None:
        host = self._current_host()
        if host:
            self.settings.set_shields_for(host, checked)
            self._cosmetic_cache.pop(host, None)
            view = self.current()
            if view:
                view.page().apply_cosmetic("")   # force recompute next nav
                view.reload()

    def cosmetic_css_for(self, host: str) -> str:
        if host not in self._cosmetic_cache:
            self._cosmetic_cache[host] = self.filter_engine.cosmetic_css(host)
            if len(self._cosmetic_cache) > 64:
                self._cosmetic_cache.pop(next(iter(self._cosmetic_cache)))
        return self._cosmetic_cache[host]

    # -------------------------------------------------------------- actions
    def focus_url(self) -> None:
        self.url_bar.setFocus()
        self.url_bar.selectAll()

    def toggle_bookmark(self) -> None:
        view = self.current()
        if not view:
            return
        added = self.bookmarks.toggle(view.url().toString(), view.title())
        self._update_bookmark_button()
        self._fill_bookmarks_menu()
        self.status_label.setText("Bookmark added" if added else "Bookmark removed")

    def zoom(self, delta: float) -> None:
        view = self.current()
        if not view:
            return
        factor = 1.0 if delta == 0 else max(0.25, min(5.0, view.zoomFactor() + delta))
        view.setZoomFactor(factor)
        self.status_label.setText(f"Zoom {int(factor * 100)}%")

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def set_web_fullscreen(self, on: bool) -> None:
        self._web_fullscreen = on
        self.toolbar.setVisible(not on)
        self.tabs.set_bar_visible(not on)
        self.status.setVisible(not on)
        if on:
            self.showFullScreen()
        else:
            self.showNormal()

    def show_find(self) -> None:
        self.find_bar.setVisible(True)
        self.find_bar.focus()

    def hide_find(self) -> None:
        self.find_bar.setVisible(False)
        view = self.current()
        if view:
            view.findText("")

    def on_escape(self) -> None:
        if self.find_bar.isVisible():
            self.hide_find()
        elif self._web_fullscreen:
            self.set_web_fullscreen(False)
        elif self.isFullScreen():
            self.showNormal()

    def _do_find(self, text: str, forward: bool) -> None:
        view = self.current()
        if not view:
            return
        flags = QWebEnginePage.FindFlag(0)
        if not forward:
            flags |= QWebEnginePage.FindFlag.FindBackward
        view.findText(text, flags)

    def view_source(self) -> None:
        view = self.current()
        if view:
            self.new_tab("view-source:" + view.url().toString())

    def show_history(self) -> None:
        rows = self.history.recent(500)
        body = "".join(
            f'<tr><td><a href="{u}">{(t or u)[:90]}</a></td>'
            f'<td class="u">{u[:70]}</td></tr>'
            for u, t, _ in rows
        )
        html_text = _list_page("History", body)
        view = self.new_tab()
        view.setHtml(html_text, QUrl("about:blank"))
        view.setProperty("merlin_start", True)
        self._set_page_icon(view, "reload")

    def show_bookmarks(self) -> None:
        body = "".join(
            f'<tr><td><a href="{b["url"]}">{(b.get("title") or b["url"])[:90]}</a></td>'
            f'<td class="u">{b["url"][:70]}</td></tr>'
            for b in self.bookmarks.items
        )
        view = self.new_tab()
        view.setHtml(_list_page("Bookmarks", body), QUrl("about:blank"))
        view.setProperty("merlin_start", True)
        self._set_page_icon(view, "bookmarks")

    def show_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self, self)
        dialog.exec()
        # a dialog taking and giving back focus disturbs the corner overlay
        self._refresh_corners()

    def _on_update_available(self, version: str, notes: str) -> None:
        self.status_label.setText(
            f"Merlin {version} is available - see Settings, Updates")

    def open_releases(self) -> None:
        QDesktopServices.openUrl(QUrl(RELEASES_URL))

    def show_about(self) -> None:
        from PyQt6.QtCore import QT_VERSION_STR

        QMessageBox.about(
            self, "About Merlin",
            f"<h3>{APP_NAME} Browser {APP_VERSION}</h3>"
            f"<p>{APP_BLURB}</p>"
            f"<p>Qt {QT_VERSION_STR}<br>"
            f"{self.filter_engine.rule_count:,} filter rules<br>"
            f"{self._engine_summary()}</p>"
        )

    # ----------------------------------------------------------- diagnostics
    def _engine_summary(self) -> str:
        codecs = dict(self._codec_probe.get("codecs", []))
        if not codecs:
            return "Chromium engine"
        licensed = [n for n in ("H.264 / AVC", "AAC")
                    if codecs.get(n, "no") != "no"]
        if len(licensed) == 2:
            return "Chromium engine, licensed codecs on"
        return "Chromium engine, licensed codecs off"

    def probe_codecs(self, then=None) -> None:
        """Ask the engine what it can decode, using a scratch page."""
        probe_view = WebView(self, self.profile, self)
        probe_view.setHtml("<html><body></body></html>")

        def when_loaded(ok):
            if not ok:
                probe_view.deleteLater()
                return

            def got(raw):
                self._codec_probe = media.parse_probe(raw)
                probe_view.deleteLater()
                if then:
                    then(self._codec_probe)

            probe_view.page().runJavaScript(media.CODEC_PROBE_JS, got)

        probe_view.loadFinished.connect(when_loaded)

    def show_codecs(self) -> None:
        view = self.new_tab()

        def render(probe):
            player = media.find_player(self.settings.get("player_command"))
            view.setHtml(
                media.codec_report_html(
                    probe, player, media.has_ytdlp(),
                    self.settings.get("player_mode", "embedded"),
                    media.libvlc_version()),
                QUrl("about:blank"),
            )
            view.setProperty("merlin_start", True)

        if self._codec_probe:
            render(self._codec_probe)
        else:
            self.probe_codecs(then=render)

    def open_in_player(self, url: str = "", new_tab: bool = True) -> None:
        """Send a URL to the media player, in a tab or its own window."""
        view = self.current()
        if not url and view:
            url = view.url().toString()
        if not url:
            return
        mode = self.settings.get("player_mode", "embedded")
        if mode == "off":
            self.status_label.setText("Player is disabled in Settings, Media.")
            return

        if mode == "window" or not new_tab:
            ok, message, _proc = media.launch(
                url, self.settings.get("player_command"), 0,
                self.settings.get("player_args"))
            self.status_label.setText(message)
            if not ok:
                QMessageBox.information(self, "Player", message)
            return

        tab = PlayerTab(url, self.settings, self)
        index = self.tabs.addTab(tab, tab.display_name())
        tab.titleChanged.connect(
            lambda title, t=tab: self.tabs.setTabText(self.tabs.indexOf(t), title))
        tab.closed.connect(lambda t=tab: self._close_player_tab(t))
        self.tabs.setCurrentIndex(index)
        self.status_label.setText(f"Playing {url[:70]}")

    def _close_player_tab(self, tab) -> None:
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.removeTab(index)
        tab.deleteLater()

    def _offer_player_for_failed_media(self, view) -> None:
        if not self.view_is_alive(view):
            return
        """If the engine choked on a media element, offer the player."""
        if not self.settings.get("auto_offer_player"):
            return
        if self.settings.get("player_mode", "embedded") == "off":
            return

        def got(url):
            if not url or view is not self.current():
                return
            if url == getattr(view, "_offered_media", None):
                return
            view._offered_media = url
            answer = QMessageBox.question(
                self, "Unsupported media",
                "This engine build cannot decode that media.\n\n"
                "Play it with Merlin's player instead?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.open_in_player(url)

        view.page().runJavaScript(media.FAILED_MEDIA_JS, got)

    def clear_history(self) -> None:
        self.history.clear()
        self.status_label.setText("History cleared")

    def clear_cache(self) -> None:
        self.profile.clearHttpCache()
        self.profile.cookieStore().deleteAllCookies()
        self.status_label.setText("Cache and cookies cleared")

    def new_window(self, url: str | None = None) -> "BrowserWindow":
        window = BrowserWindow(
            self.app, self.settings, self.profile, self.filter_engine,
            self.filter_loader, self.interceptor, self.history, self.bookmarks,
        )
        window.new_tab(url)
        window.show()
        self.app.setProperty("merlin_windows",
                             (self.app.property("merlin_windows") or []) + [window])
        return window

    def new_private_window(self) -> "BrowserWindow":
        private_profile = QWebEngineProfile(self)          # off the record
        private_profile.setUrlRequestInterceptor(self.interceptor)
        window = BrowserWindow(
            self.app, self.settings, private_profile, self.filter_engine,
            self.filter_loader, self.interceptor, self.history, self.bookmarks,
            private=True,
        )
        window._private_profile = private_profile           # keep it alive
        window.new_tab()
        window.show()
        self.app.setProperty("merlin_windows",
                             (self.app.property("merlin_windows") or []) + [window])
        return window

    def new_tor_window(self) -> None:
        """Open a separate Merlin routed through a local Tor daemon."""
        from . import privacy

        if not privacy.tor_available():
            started = False
            if privacy.tor_binary():
                answer = QMessageBox.question(
                    self, "Start Tor?",
                    "Tor is installed but not running. Start it now?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes)
                if answer == QMessageBox.StandardButton.Yes:
                    self.status_label.setText("Starting Tor...")
                    QApplication.processEvents()
                    started, message = privacy.start_tor()
                    self.status_label.setText(message)
            if not started:
                QMessageBox.information(
                    self, "Tor is not running",
                    "Merlin routes through a Tor daemon running on this "
                    "machine; it does not bundle one.\n\n"
                    + privacy.install_hint())
                return

        answer = QMessageBox.question(
            self, "Open a Tor window?",
            "Traffic in the new window goes through Tor, so sites see an exit "
            "node's address instead of yours, and DNS is resolved at the Tor "
            "end.\n\n"
            "This is not Tor Browser. Tor Browser also makes its users look "
            "alike, by normalising screen size, fonts, timezone and much else. "
            "Merlin does not, so a site can still fingerprint this browser. "
            "Treat it as hiding your address, not as anonymity.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return

        ok, message = privacy.launch_tor_window()
        self.status_label.setText(message)
        if not ok:
            QMessageBox.warning(self, "Could not open a Tor window", message)

    def _style_clock(self, dark: bool | None = None) -> None:
        """Clock text follows the theme: white on dark, black on light.

        Called from apply_icons and again once the clock exists, because
        apply_icons runs while the toolbar is being built, before the status
        bar and its clock have been created.
        """
        clock = getattr(self, "clock_label", None)
        if clock is None:
            return
        if dark is None:
            dark = bool(self.settings.get("dark_ui"))
        colour = icons.DARK_FG if dark else icons.LIGHT_FG
        clock.setStyleSheet(f"color:{colour}; padding-right:8px;"
                            " background: transparent;")

    def _tick(self) -> None:
        """Update the clock, and the theme if it follows the time of day."""
        from datetime import datetime

        now = datetime.now()
        if self.settings.get("show_clock", True):
            self.clock_label.setText(now.strftime("%H:%M"))
            self.clock_label.setToolTip(now.strftime("%A %d %B %Y"))
            self.clock_label.show()
        else:
            self.clock_label.hide()

        if self.settings.get("theme_mode") == "auto":
            wanted = self.dark_wanted_now(now.hour)
            if wanted != bool(self.settings.get("dark_ui")):
                self.settings.set("dark_ui", wanted)

    def dark_wanted_now(self, hour: int) -> bool:
        """Dark outside the light hours, light between them.

        Written so that a light span crossing midnight still works, which it
        does if someone sets light at 22 and dark at 6.
        """
        light_at = int(self.settings.get("theme_light_hour", 7) or 0)
        dark_at = int(self.settings.get("theme_dark_hour", 19) or 0)
        if light_at == dark_at:
            return True
        if light_at < dark_at:
            is_light = light_at <= hour < dark_at
        else:
            is_light = hour >= light_at or hour < dark_at
        return not is_light

    def _apply_cookie_filter(self) -> None:
        store = self.profile.cookieStore()
        settings = self.settings

        def cookie_filter(request):
            if not settings.get("block_third_party_cookies"):
                return True
            return not request.thirdParty

        store.setCookieFilter(cookie_filter)

    # -------------------------------------------------------------- session
    def save_session(self) -> None:
        """Remember every tab's address for next time.

        A restored tab is not loaded until it is selected, and until then its
        url() is empty: the address it is waiting to open sits in pending_url.
        Reading only url() meant that quitting without visiting the restored
        tabs saved just the one that had loaded, and the rest were lost.
        """
        if self.private:
            return
        urls = []
        for i in range(self.tabs.count()):
            view = self.tabs.widget(i)
            if not isinstance(view, WebView) or view.property("merlin_start"):
                continue
            url = view.url().toString()
            if not url or url == "about:blank":
                url = view.property("pending_url") or ""
            if url and url != "about:blank":
                urls.append(url)
        self.settings.set("last_session", urls, save=False)
        if self.settings.get("remember_window_geometry"):
            geometry = self.geometry()
            self.settings.set(
                "window_geometry",
                [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
                save=False,
            )
            self.settings.set("window_maximized", self.isMaximized(), save=False)
        self.settings.save()

    def restore_geometry(self) -> None:
        if not self.settings.get("remember_window_geometry"):
            self.resize(1280, 820)
            return
        geometry = self.settings.get("window_geometry")
        if isinstance(geometry, list) and len(geometry) == 4:
            self.setGeometry(*[int(v) for v in geometry])
        else:
            self.resize(1280, 820)
        if self.settings.get("window_maximized"):
            self.showMaximized()

    def closeEvent(self, event):  # noqa: N802
        self.save_session()
        super().closeEvent(event)

    # ------------------------------------------------------------ downloads
    def _fill_history_menu(self) -> None:
        """Recent pages, newest first.

        Rebuilt on demand rather than kept in step, because history changes on
        every page load and this is only looked at when opened.
        """
        menu = self.history_menu
        menu.clear()
        dark = bool(self.settings.get("dark_ui"))
        # recent() yields (url, title, visited_at) rows, not dictionaries
        entries = self.history.recent(15)
        if not entries:
            empty = menu.addAction("Nothing visited yet")
            empty.setEnabled(False)
        for row in entries:
            url = row[0]
            title = (row[1] or url or "")[:60]
            action = menu.addAction(icons.themed_icon("history", dark, 16), title)
            action.setToolTip(url)
            action.triggered.connect(
                lambda _c=False, u=url: self.navigate(u))
        menu.addSeparator()
        full = menu.addAction("Show all history")
        full.triggered.connect(self.show_history)
        clear = menu.addAction("Clear history...")
        clear.triggered.connect(self.clear_history_prompt)

    def clear_history_prompt(self) -> None:
        answer = QMessageBox.question(
            self, "Clear history",
            "Remove every page from your browsing history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.history.clear()
            self.status_label.setText("History cleared")

    def _fill_downloads_menu(self) -> None:
        """Rebuild the downloads list.

        Built when downloads change rather than from aboutToShow: clearing a
        menu while it is opening deletes actions the popup is still using.
        """
        menu = self.downloads_menu
        if menu.isVisible():
            return
        menu.clear()
        dark = bool(self.settings.get("dark_ui"))
        if not self.downloads:
            empty = menu.addAction("No downloads yet")
            empty.setEnabled(False)
        for item in reversed(self.downloads[-20:]):
            name = os.path.basename(item["path"])
            state = item["state"]
            label = name if state == "done" else f"{name}  ({state})"
            action = menu.addAction(icons.themed_icon("download", dark), label)
            action.setToolTip(item["path"])
            if state == "done":
                action.triggered.connect(
                    lambda _c=False, p=item["path"]: self.open_download(p))
            else:
                action.setEnabled(False)
        if self.downloads:
            menu.addSeparator()
            folder = menu.addAction("Open downloads folder")
            folder.triggered.connect(self.open_downloads_folder)
            clear = menu.addAction("Clear list")
            clear.triggered.connect(self.clear_downloads)

    def open_download(self, path: str) -> None:
        if os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            self.status_label.setText("That file has been moved or deleted")

    def open_downloads_folder(self) -> None:
        folder = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation)
        if self.downloads:
            folder = os.path.dirname(self.downloads[-1]["path"]) or folder
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def clear_downloads(self) -> None:
        self.downloads.clear()
        self._fill_downloads_menu()

    def show_downloads(self) -> None:
        self._fill_downloads_menu()
        self.btn_downloads.showMenu()

    def handle_download(self, download) -> None:
        default_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation) or os.path.expanduser("~")
        suggested = download.downloadFileName()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save file", os.path.join(default_dir, suggested))
        if not path:
            download.cancel()
            return
        download.setDownloadDirectory(os.path.dirname(path))
        download.setDownloadFileName(os.path.basename(path))
        download.accept()
        entry = {"path": path, "state": "downloading"}
        self.downloads.append(entry)
        self._fill_downloads_menu()
        self.status_label.setText(f"Downloading {os.path.basename(path)}...")

        def finished():
            entry["state"] = "done"
            self._fill_downloads_menu()
            self.status_label.setText(f"Saved {os.path.basename(path)}")

        download.isFinishedChanged.connect(finished)


def _list_page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
 body{{font:14px/1.6 system-ui,sans-serif;background:#17181c;color:#e6e6ea;
       margin:0;padding:32px;}}
 h1{{font-weight:600;font-size:24px;margin:0 0 18px;}}
 table{{width:100%;border-collapse:collapse;}}
 td{{padding:7px 10px;border-bottom:1px solid #26282f;vertical-align:top;}}
 a{{color:#8fa9f2;text-decoration:none;}} a:hover{{text-decoration:underline;}}
 .u{{color:#7d7f88;font-size:12px;width:40%;}}
</style></head><body><h1>{title}</h1><table>{body}</table></body></html>"""
