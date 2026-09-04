"""Widgets, theming and the settings dialog for Merlin Browser."""
from __future__ import annotations

import html
import os

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSizePolicy, QSlider, QSpinBox, QTabWidget,
    QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import settings as cfg
from .brand import APP_SCHEME

# --------------------------------------------------------------------- theme
DARK_QSS = """
QMainWindow, QWidget { background: #1b1c20; color: #e6e6e9; }
QToolBar { background: #1b1c20; border: 0; padding: 3px 6px; spacing: 2px; }
QToolButton { background: transparent; border: 0; border-radius: 6px; padding: 5px; }
QToolButton:hover { background: #2c2e34; }
QToolButton:pressed { background: #3a3d45; }
QToolButton:disabled { color: #55575e; }
QLineEdit {
    background: #2a2c32; border: 1px solid #34363d; border-radius: 14px;
    padding: 6px 12px; selection-background-color: #4b6cd6; color: #f0f0f3;
}
QLineEdit:focus { border: 1px solid #5a7ce0; background: #313339; }
QTabWidget::pane { border: 0; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    background: #232429; color: #b8b9bf; padding: 7px 12px; margin: 3px 2px 0 0;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
    min-width: 90px; max-width: 220px;
}
QTabBar::tab:selected { background: #2f3138; color: #ffffff; }
QTabBar::tab:hover { background: #2a2c32; }
QTabBar::close-button { image: none; border-radius: 6px; }
QMenu { background: #232429; border: 1px solid #34363d; padding: 5px; }
QMenu::item { padding: 6px 22px 6px 14px; border-radius: 5px; }
QMenu::item:selected { background: #3a3d45; }
QStatusBar { background: #1b1c20; color: #9a9ba1; }
QDialog { background: #1f2025; }
QPushButton {
    background: #33353d; border: 1px solid #3f424b; border-radius: 6px;
    padding: 6px 14px;
}
QPushButton:hover { background: #3d404a; }
QListWidget, QPlainTextEdit {
    background: #232429; border: 1px solid #34363d; border-radius: 6px;
}
QCheckBox { padding: 3px; }
"""

LIGHT_QSS = """
QMainWindow, QWidget { background: #f6f6f8; color: #1a1a1c; }
QToolBar { background: #f6f6f8; border: 0; padding: 3px 6px; spacing: 2px; }
QToolButton { background: transparent; border: 0; border-radius: 6px; padding: 5px; }
QToolButton:hover { background: #e3e4e8; }
QLineEdit {
    background: #ffffff; border: 1px solid #d3d4d9; border-radius: 14px;
    padding: 6px 12px;
}
QLineEdit:focus { border: 1px solid #5a7ce0; }
QTabWidget::pane { border: 0; }
QTabBar::tab {
    background: #e7e8ec; padding: 7px 12px; margin: 3px 2px 0 0;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
    min-width: 90px; max-width: 220px;
}
QTabBar::tab:selected { background: #ffffff; }
"""


def apply_theme(app, dark: bool) -> None:
    app.setStyleSheet(DARK_QSS if dark else LIGHT_QSS)


def apply_font(app, point_size: int) -> None:
    """Set the interface font size. 0 restores the platform default.

    Windows display scaling can leave the interface much larger than intended,
    and there is no reliable way to detect what the user actually wants, so
    this is exposed directly rather than guessed at.
    """
    font = app.font()
    if not point_size:
        default = getattr(app, "_merlin_default_font", None)
        if default is not None:
            app.setFont(default)
        return
    if not hasattr(app, "_merlin_default_font"):
        app._merlin_default_font = QFont(font)
    font.setPointSize(int(point_size))
    app.setFont(font)


def icon(name: str, fallback: str = "") -> QIcon:
    ico = QIcon.fromTheme(name)
    if ico.isNull() and fallback:
        ico = QIcon.fromTheme(fallback)
    return ico


def search_address(template: str) -> str:
    """google.com/search from https://www.google.com/search?q={} .

    The host alone is ambiguous once several engines share a domain, so the
    path is kept and only the query string is dropped.
    """
    text = template.split("{}")[0]
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("www."):
        text = text[4:]
    for cut in ("?", "&"):
        if cut in text:
            text = text.split(cut)[0]
    return text.rstrip("/") or template


# ---------------------------------------------------------------- start page
START_BACKGROUNDS = {
    "midnight": ("Midnight",
                 "radial-gradient(circle at 50% 18%, #262a36 0%, #15161a 70%)"),
    "ink":      ("Ink", "#101114"),
    "slate":    ("Slate",
                 "linear-gradient(160deg, #2b3040 0%, #171a21 75%)"),
    "aurora":   ("Aurora",
                 "linear-gradient(150deg, #10243a 0%, #1d3b4d 45%, #12181f 100%)"),
    "ember":    ("Ember",
                 "linear-gradient(155deg, #3a2320 0%, #1c1517 70%)"),
    "moss":     ("Moss",
                 "linear-gradient(150deg, #1b3026 0%, #14181a 75%)"),
    "arcane":   ("Arcane",
                 "radial-gradient(circle at 30% 20%, #33245c 0%, #14121f 68%)"),
    "paper":    ("Paper (light)", "#eceef3"),
}


RANDOM_BACKGROUND = "random"


def background_css(setting: str) -> tuple[str, bool]:
    """Return (css background value, is_light).

    "random" picks a different built-in each time a new tab page is drawn, so
    the choice is made here rather than stored: the setting stays "random" and
    every new tab gets its own.
    """
    setting = setting or "midnight"
    if setting == RANDOM_BACKGROUND:
        import random

        setting = random.choice([k for k in START_BACKGROUNDS])
    if setting.startswith("image:"):
        path = setting[6:]
        data = image_data_uri(path)
        if data:
            return (f"#101114 url('{data}') center/cover no-repeat fixed", False)
        return START_BACKGROUNDS["midnight"][1], False
    name, css = START_BACKGROUNDS.get(setting, START_BACKGROUNDS["midnight"])
    return css, setting == "paper"


def image_data_uri(path: str, limit: int = 6 * 1024 * 1024) -> str:
    """Inline a local image so setHtml can show it without file access."""
    import base64
    import mimetypes

    try:
        if not path or not os.path.isfile(path):
            return ""
        if os.path.getsize(path) > limit:
            return ""
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as fh:
            payload = base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return ""
    return f"data:{mime};base64,{payload}"


START_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 26px;
  font: 15px/1.5 system-ui, "Segoe UI", Cantarell, sans-serif;
  background: __BG__;
  color: #e8e8ec;
}
body.light { color: #1b1c20; }
body.light input { background: #ffffff; border-color: #cfd2da; color: #16171a; }
body.light a.tile { background: #ffffffcc; border-color: #d3d6de; color: #2b2d33; }
body.light a.tile:hover { background: #ffffff; color: #000; }
body.light .stat { color: #5c5f68; }
body.light h1 { text-shadow: none; }
h1 { font-size: 40px; margin: 0; letter-spacing: -1px; font-weight: 650; }
h1 span { color: #6f8ff0; }
form { display: flex; width: min(620px, 86vw); position: relative; }
input {
  flex: 1; padding: 14px 52px 14px 18px; font-size: 16px; border-radius: 26px;
  border: 1px solid #33363f; background: #22242b; color: #f2f2f5; outline: none;
}
input:focus { border-color: #6f8ff0; }
a.mic {
  position: absolute; right: 9px; top: 50%; transform: translateY(-50%);
  display: flex; align-items: center; justify-content: center;
  width: 34px; height: 34px; border-radius: 50%; color: #9a9ba1;
  text-decoration: none;
}
a.mic:hover { color: #ffffff; background: rgba(255, 255, 255, 0.10); }
body.light a.mic { color: #4a4d55; }
body.light a.mic:hover { color: #14161a; background: rgba(0, 0, 0, 0.07); }
.links { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center;
         width: min(680px, 90vw); }
a.tile {
  padding: 10px 16px; border-radius: 10px; background: #22242b;
  border: 1px solid #2e313a; color: #cfd1d8; text-decoration: none; font-size: 14px;
}
a.tile:hover { background: #2c2f38; color: #fff; }
a.tile.add { font-size: 18px; line-height: 1; padding: 8px 18px; color: #8d8f98; }
a.tile.add:hover { color: #fff; border-color: #6f8ff0; }
body.light a.tile.add { color: #2f333c; }
body.light a.tile.add:hover { color: #14161a; }
.stat { color: #8d8f98; font-size: 13px; }
"""


def start_page_html(settings: cfg.Settings, blocked_total: int = 0,
                    bookmarks=None) -> str:
    background, is_light = background_css(settings.get("start_background"))
    entries = settings.tiles()
    tiles = []
    for item in entries:
        tiles.append(
            f'<a class="tile" href="{html.escape(item["url"], quote=True)}" '
            f'title="{html.escape(item["url"], quote=True)}">'
            f'{html.escape(item["title"][:28])}</a>'
        )
    if len(entries) < cfg.MAX_START_TILES:
        # merlin://addtile is caught before navigation and opens the editor
        tiles.append(
            f'<a class="tile add" href="{APP_SCHEME}://addtile" '
            f'title="Add a shortcut">+</a>'
        )

    engine = settings.get("search_engine")
    template = cfg.SEARCH_ENGINES.get(engine, cfg.SEARCH_ENGINES["DuckDuckGo"])
    action = template.split("{}")[0]
    param = "q"
    if "query=" in template:
        param = "query"
    elif "search=" in template:
        param = "search"
    action = action.rsplit(param + "=", 1)[0].rstrip("?&")

    body_class = ' class="light"' if is_light else ""
    css = START_CSS.replace("__BG__", background)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>New Tab</title><style>{css}</style></head><body{body_class}>
<h1>Merlin<span>.</span></h1>
<form method="get" action="{html.escape(action, quote=True)}">
  <input name="{param}" autofocus placeholder="Search..." autocomplete="off">
  <a class="mic" href="{APP_SCHEME}://listen" title="Search by voice">
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path d="M12 3.4a2.8 2.8 0 0 1 2.8 2.8v5a2.8 2.8 0 0 1-5.6 0v-5A2.8 2.8 0 0 1 12 3.4z"
            fill="currentColor"/>
      <path d="M6.2 11.2a5.8 5.8 0 0 0 11.6 0" fill="none" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round"/>
      <path d="M12 17.1v3.4" fill="none" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round"/>
    </svg>
  </a>
</form>
<div class="links">{''.join(tiles)}</div>
<div class="stat">{blocked_total} requests blocked this session</div>
</body></html>"""


# -------------------------------------------------------------- frameless UI
class WindowButtons(QWidget):
    """Minimise / maximise / close, shown only when decorations are hidden."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(2)

        self.btn_min = self._make("window-minimize", "\u2013", "Minimise")
        self.btn_max = self._make("window-maximize", "\u25a1", "Maximise")
        self.btn_close = self._make("window-close", "\u2715", "Close")
        self.btn_close.setObjectName("closeButton")
        # always red, not just on hover: without a title bar this is the only
        # close affordance on screen, so it should be unmistakable
        self.btn_close.setStyleSheet(
            "QToolButton#closeButton {"
            "  background: #d33; color: #ffffff; border-radius: 6px;"
            "  font-weight: 600;"
            "}"
            "QToolButton#closeButton:hover { background: #f04747; }"
            "QToolButton#closeButton:pressed { background: #a92020; }"
        )

        self.btn_min.clicked.connect(self._window.showMinimized)
        self.btn_max.clicked.connect(self._toggle_max)
        self.btn_close.clicked.connect(self._window.close)
        for button in (self.btn_min, self.btn_max, self.btn_close):
            layout.addWidget(button)

    def _make(self, theme_name: str, text: str, tip: str) -> QToolButton:
        button = QToolButton(self)
        themed = QIcon.fromTheme(theme_name)
        if themed.isNull():
            button.setText(text)
        else:
            button.setIcon(themed)
            button.setIconSize(QSize(14, 14))
        button.setToolTip(tip)
        button.setFixedSize(QSize(30, 26))
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _toggle_max(self) -> None:
        toggle = getattr(self._window, "toggle_maximise", None)
        if callable(toggle):
            toggle()
        elif self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()


class FindBar(QWidget):
    search = pyqtSignal(str, bool)     # text, forward
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        self.field = QLineEdit(self)
        self.field.setPlaceholderText("Find in page")
        self.field.setClearButtonEnabled(True)
        self.result = QLabel("", self)
        prev_button = QPushButton("\u2191", self)
        next_button = QPushButton("\u2193", self)
        close_button = QPushButton("\u2715", self)
        for button in (prev_button, next_button, close_button):
            button.setFixedWidth(34)
        layout.addWidget(self.field, 1)
        layout.addWidget(self.result)
        layout.addWidget(prev_button)
        layout.addWidget(next_button)
        layout.addWidget(close_button)

        self.field.textChanged.connect(lambda t: self.search.emit(t, True))
        self.field.returnPressed.connect(
            lambda: self.search.emit(self.field.text(), True))
        next_button.clicked.connect(
            lambda: self.search.emit(self.field.text(), True))
        prev_button.clicked.connect(
            lambda: self.search.emit(self.field.text(), False))
        close_button.clicked.connect(self.closed.emit)

    def focus(self) -> None:
        self.field.setFocus()
        self.field.selectAll()


# ----------------------------------------------------------- settings dialog
class SettingsDialog(QDialog):
    def __init__(self, settings: cfg.Settings, window, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.window_ref = window
        self.setWindowTitle("Merlin Settings")
        self.resize(620, 560)

        tabs = QTabWidget(self)
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._search_tab(), "Search")
        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._shortcuts_tab(), "Shortcuts")
        tabs.addTab(self._shields_tab(), "Shields")
        tabs.addTab(self._media_tab(), "Media")
        tabs.addTab(self._advanced_tab(), "Advanced")
        tabs.addTab(self._updates_tab(), "Updates")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        self._fit_to_tabs(tabs)
        layout.addWidget(buttons)

    # ------------------------------------------------------------ helpers
    def _check(self, label: str, key: str, tip: str = "") -> QCheckBox:
        box = QCheckBox(label, self)
        box.setChecked(bool(self.settings.get(key)))
        if tip:
            box.setToolTip(tip)
        box.toggled.connect(lambda value, k=key: self.settings.set(k, value))
        return box

    # ------------------------------------------------------------- general
    def _general_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        home = QLineEdit(self.settings.get("home_page"), page)
        home.editingFinished.connect(
            lambda: self.settings.set("home_page", home.text().strip()))
        form.addRow("Home page", home)

        new_tab = QLineEdit(self.settings.get("new_tab_page"), page)
        new_tab.editingFinished.connect(
            lambda: self.settings.set("new_tab_page", new_tab.text().strip()))
        form.addRow("New tab page", new_tab)

        form.addRow(self._check(
            "Two-finger swipe to go back and forward", "swipe_navigation",
            "Trackpad or touchscreen. Left to right goes back, right to left "
            "goes forward."))
        form.addRow(self._check(
            "Reverse the swipe direction", "invert_swipe",
            "Some trackpad configurations report the opposite direction."))
        form.addRow(self._check("Restore tabs from last session", "restore_session"))
        form.addRow(self._check("Remember window size and position",
                                "remember_window_geometry"))
        return page

    def _fit_to_tabs(self, tabs) -> None:
        """Widen the dialog until the whole tab strip fits.

        Otherwise the strip collapses behind little scroll arrows and half the
        sections are hidden behind them.
        """
        bar = tabs.tabBar()
        bar.setUsesScrollButtons(False)
        needed = bar.sizeHint().width()
        margins = self.layout().contentsMargins()
        frame = margins.left() + margins.right() + 24
        self.setMinimumWidth(max(self.minimumWidth(), needed + frame))
        self.resize(max(self.width(), needed + frame), self.height())

    # -------------------------------------------------------------- search
    def _search_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel("<b>Default search engine</b>", page))

        self.engine_list = QTreeWidget(page)
        self.engine_list.setColumnCount(3)
        self.engine_list.setHeaderLabels(["Engine", "Address", "Prefix"])
        self.engine_list.setRootIsDecorated(False)
        self.engine_list.setAlternatingRowColors(False)
        self.engine_list.setUniformRowHeights(True)
        header = self.engine_list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self._engine_names = list(cfg.SEARCH_ENGINES)
        keyword_for = {name: key for key, name in cfg.SEARCH_KEYWORDS.items()}
        current = self.settings.get("search_engine")
        for name in self._engine_names:
            template = cfg.SEARCH_ENGINES.get(name) or ""
            address = search_address(template) if template else "your own URL"
            key = keyword_for.get(name)
            item = QTreeWidgetItem([name, address, f"{key} …" if key else ""])
            item.setData(0, Qt.ItemDataRole.UserRole, name)
            item.setForeground(1, QColor("#8fa9f2"))
            item.setForeground(2, QColor("#8d8f98"))
            self.engine_list.addTopLevelItem(item)
            if name == current:
                self.engine_list.setCurrentItem(item)
        self.engine_list.currentItemChanged.connect(self._engine_chosen)
        layout.addWidget(self.engine_list, 1)

        form = QFormLayout()
        self.custom_search = QLineEdit(self.settings.get("custom_search_url"), page)
        self.custom_search.setPlaceholderText("https://example.com/search?q={}")
        self.custom_search.editingFinished.connect(self._custom_search_changed)
        form.addRow("Custom URL", self.custom_search)
        layout.addLayout(form)

        self.search_preview = QLabel("", page)
        self.search_preview.setWordWrap(True)
        self.search_preview.setStyleSheet("color:#9a9ba1; font-size:12px;")
        layout.addWidget(self.search_preview)
        self._update_search_preview()

        speech_row = QHBoxLayout()
        speech_row.addWidget(QLabel("Speech model", page))
        self.speech_path = QLineEdit(self.settings.get("speech_model_path"), page)
        self.speech_path.setPlaceholderText(
            "left empty, Merlin downloads a small English model on first use")
        self.speech_path.editingFinished.connect(
            lambda: self.settings.set("speech_model_path",
                                      self.speech_path.text().strip()))
        speech_row.addWidget(self.speech_path, 1)
        browse = QPushButton("Choose...", page)

        def pick_model():
            from PyQt6.QtWidgets import QFileDialog

            folder = QFileDialog.getExistingDirectory(
                self, "Choose a Vosk model folder")
            if folder:
                self.speech_path.setText(folder)
                self.settings.set("speech_model_path", folder)

        browse.clicked.connect(pick_model)
        speech_row.addWidget(browse)
        layout.addLayout(speech_row)

        speech_note = QLabel(
            "The microphone button on the new tab page transcribes on this "
            "machine with Vosk. Audio is never sent anywhere. The library and "
            "a 40 MB model are fetched on first use, after asking.", page)
        speech_note.setWordWrap(True)
        speech_note.setStyleSheet("color:#9a9ba1; font-size:12px;")
        layout.addWidget(speech_note)

        layout.addWidget(self._check(
            "Enable keyword prefixes", "search_keywords_enabled",
            "Type a prefix and a space to use another engine once, without "
            "changing the default."))

        hint = QLabel(
            "Prefixes: " + ", ".join(
                f"{k} = {v}" for k, v in list(cfg.SEARCH_KEYWORDS.items())[:6])
            + ", …", page)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9a9ba1; font-size:12px;")
        layout.addWidget(hint)
        return page

    def _engine_chosen(self, item, _previous=None) -> None:
        if item is None:
            return
        name = item.data(0, Qt.ItemDataRole.UserRole)
        self.settings.set("search_engine", name)
        self.custom_search.setEnabled(name == "Custom")
        self._update_search_preview()

    def _custom_search_changed(self) -> None:
        text = self.custom_search.text().strip()
        self.settings.set("custom_search_url", text)
        self._update_search_preview()

    def _update_search_preview(self) -> None:
        name = self.settings.get("search_engine")
        self.custom_search.setEnabled(name == "Custom")
        if name == "Custom" and "{}" not in (
                self.settings.get("custom_search_url") or ""):
            self.search_preview.setText(
                "The custom URL needs {} where the search text goes. "
                "Falling back to DuckDuckGo until it does.")
            return
        self.search_preview.setText(
            "Searching for 'merlin' goes to:\n" + self.settings.search_url("merlin"))

    # ---------------------------------------------------------- appearance
    def _appearance_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        decorations = QCheckBox("Hide window decorations (frameless window)", page)
        decorations.setChecked(bool(self.settings.get("hide_window_decorations")))
        decorations.setToolTip(
            "Removes the system title bar. Drag the empty tab strip to move the "
            "window; drag the window edges to resize. Shortcut: Ctrl+Shift+D"
        )
        decorations.toggled.connect(
            lambda value: self.settings.set("hide_window_decorations", value))
        layout.addWidget(decorations)

        layout.addWidget(self._check(
            "Show minimise / maximise / close buttons when frameless",
            "show_window_buttons_when_frameless"))
        layout.addWidget(self._check("Dark interface", "dark_ui"))

        row = QHBoxLayout()
        row.addWidget(QLabel("Tab bar position", page))
        self.tabpos_box = QComboBox(page)
        self._tab_modes = [
            ("horizontal", "Across the top"),
            ("left", "Vertical, left edge"),
            ("right", "Vertical, right edge"),
        ]
        for _key, label in self._tab_modes:
            self.tabpos_box.addItem(label)
        current = self.settings.get("tab_orientation", "horizontal")
        for i, (key, _label) in enumerate(self._tab_modes):
            if key == current:
                self.tabpos_box.setCurrentIndex(i)
        self.tabpos_box.currentIndexChanged.connect(
            lambda i: self.settings.set("tab_orientation", self._tab_modes[i][0]))
        row.addWidget(self.tabpos_box, 1)
        layout.addLayout(row)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Interface size", page))
        self.font_box = QComboBox(page)
        self._font_sizes = [(0, "System default"), (8, "Small (8pt)"),
                            (9, "Normal (9pt)"), (10, "Medium (10pt)"),
                            (11, "Large (11pt)"), (12, "Larger (12pt)")]
        for _pt, label in self._font_sizes:
            self.font_box.addItem(label)
        current_pt = int(self.settings.get("ui_font_pt", 0) or 0)
        for i, (pt, _l) in enumerate(self._font_sizes):
            if pt == current_pt:
                self.font_box.setCurrentIndex(i)
        self.font_box.currentIndexChanged.connect(
            lambda i: self.settings.set("ui_font_pt", self._font_sizes[i][0]))
        size_row.addWidget(self.font_box, 1)
        layout.addLayout(size_row)

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Theme", page))
        self.theme_box = QComboBox(page)
        self._theme_modes = [("manual", "Chosen by hand"),
                             ("auto", "Follow the time of day")]
        for _key, label in self._theme_modes:
            self.theme_box.addItem(label)
        current_mode = self.settings.get("theme_mode", "manual")
        for i, (key, _l) in enumerate(self._theme_modes):
            if key == current_mode:
                self.theme_box.setCurrentIndex(i)
        self.theme_box.currentIndexChanged.connect(
            lambda i: self.settings.set("theme_mode", self._theme_modes[i][0]))
        theme_row.addWidget(self.theme_box, 1)
        layout.addLayout(theme_row)

        hours_row = QHBoxLayout()
        hours_row.addWidget(QLabel("Light from", page))
        self.light_hour = QSpinBox(page)
        self.light_hour.setRange(0, 23)
        self.light_hour.setSuffix(":00")
        self.light_hour.setValue(int(self.settings.get("theme_light_hour", 7) or 0))
        self.light_hour.valueChanged.connect(
            lambda v: self.settings.set("theme_light_hour", v))
        hours_row.addWidget(self.light_hour)
        hours_row.addWidget(QLabel("dark from", page))
        self.dark_hour = QSpinBox(page)
        self.dark_hour.setRange(0, 23)
        self.dark_hour.setSuffix(":00")
        self.dark_hour.setValue(int(self.settings.get("theme_dark_hour", 19) or 0))
        self.dark_hour.valueChanged.connect(
            lambda v: self.settings.set("theme_dark_hour", v))
        hours_row.addWidget(self.dark_hour)
        hours_row.addStretch(1)
        layout.addLayout(hours_row)

        layout.addWidget(self._check("Show a clock in the status bar",
                                     "show_clock"))

        corner_row = QHBoxLayout()
        corner_row.addWidget(QLabel("Page corner rounding", page))
        self.corner_slider = QSlider(Qt.Orientation.Horizontal, page)
        self.corner_slider.setRange(0, 28)
        self.corner_slider.setSingleStep(1)
        self.corner_slider.setPageStep(2)
        self.corner_slider.setTickInterval(4)
        self.corner_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.corner_slider.setValue(int(self.settings.get("page_corner_radius", 10) or 0))
        self.corner_value = QLabel("", page)
        self.corner_value.setFixedWidth(58)

        def corner_changed(value):
            self.corner_value.setText("Square" if value == 0 else f"{value} px")
            self.settings.set("page_corner_radius", value)

        self.corner_slider.valueChanged.connect(corner_changed)
        corner_changed(self.corner_slider.value())
        corner_row.addWidget(self.corner_slider, 1)
        corner_row.addWidget(self.corner_value)
        layout.addLayout(corner_row)

        layout.addWidget(self._check(
            "Smooth the page corners", "smooth_corners",
            "Covers the corners with an antialiased overlay instead of cutting "
            "them with a mask, which cannot be antialiased and looks stepped."))

        bg_row = QHBoxLayout()
        bg_row.addWidget(QLabel("New tab background", page))
        self.bg_box = QComboBox(page)
        self._bg_keys = list(START_BACKGROUNDS) + [RANDOM_BACKGROUND]
        for key in self._bg_keys:
            label = ("Random, a different one each new tab"
                     if key == RANDOM_BACKGROUND else START_BACKGROUNDS[key][0])
            self.bg_box.addItem(label)
        self.bg_box.addItem("Custom image...")
        current_bg = self.settings.get("start_background", "midnight")
        if current_bg.startswith("image:"):
            self.bg_box.setCurrentIndex(len(self._bg_keys))
        elif current_bg in self._bg_keys:
            self.bg_box.setCurrentIndex(self._bg_keys.index(current_bg))
        self.bg_box.currentIndexChanged.connect(self._background_changed)
        bg_row.addWidget(self.bg_box, 1)
        layout.addLayout(bg_row)

        self.bg_path_label = QLabel("", page)
        self.bg_path_label.setWordWrap(True)
        self.bg_path_label.setStyleSheet("color:#9a9ba1; font-size:12px;")
        if current_bg.startswith("image:"):
            self.bg_path_label.setText(current_bg[6:])
        layout.addWidget(self.bg_path_label)

        hint = QLabel(
            "Vertical strips stay narrow and show only favicons, then widen to "
            "full titles when the pointer moves over them. They float above the "
            "page rather than pushing it, so hovering never reflows what you "
            "are reading.", page)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9a9ba1; font-size:12px;")
        layout.addWidget(hint)

        note = QLabel(
            "Launch flags: --no-decorations, --decorations, --app URL.\n"
            "The bundled .desktop file exposes a 'Frameless window' action that "
            "starts Merlin with decorations hidden without changing this setting.",
            page,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#9a9ba1;")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # ---------------------------------------------------------- shortcuts
    def _shortcuts_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel(
            f"<b>New tab shortcuts</b> (up to {cfg.MAX_START_TILES})", page))

        self.tile_table = QTreeWidget(page)
        self.tile_table.setColumnCount(2)
        self.tile_table.setHeaderLabels(["Name", "Address"])
        self.tile_table.setRootIsDecorated(False)
        self.tile_table.setUniformRowHeights(True)
        header = self.tile_table.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tile_table.itemChanged.connect(self._tile_edited)
        layout.addWidget(self.tile_table, 1)

        row = QHBoxLayout()
        self.tile_add = QPushButton("Add", page)
        self.tile_add.clicked.connect(self._tile_add)
        remove = QPushButton("Remove", page)
        remove.clicked.connect(self._tile_remove)
        up = QPushButton("Move up", page)
        up.clicked.connect(lambda: self._tile_move(-1))
        down = QPushButton("Move down", page)
        down.clicked.connect(lambda: self._tile_move(1))
        reset = QPushButton("Reset", page)
        reset.clicked.connect(self._tile_reset)
        for button in (self.tile_add, remove, up, down, reset):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        note = QLabel(
            "Double-click a name or address to edit it. The new tab page shows "
            "a + button while there is room for another.", page)
        note.setWordWrap(True)
        note.setStyleSheet("color:#9a9ba1; font-size:12px;")
        layout.addWidget(note)

        self._reload_tiles()
        return page

    def _reload_tiles(self) -> None:
        self.tile_table.blockSignals(True)
        self.tile_table.clear()
        for tile in self.settings.tiles():
            item = QTreeWidgetItem([tile["title"], tile["url"]])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.tile_table.addTopLevelItem(item)
        self.tile_table.blockSignals(False)
        self.tile_add.setEnabled(
            self.tile_table.topLevelItemCount() < cfg.MAX_START_TILES)

    def _tiles_from_table(self) -> list[dict]:
        out = []
        for i in range(self.tile_table.topLevelItemCount()):
            item = self.tile_table.topLevelItem(i)
            url = item.text(1).strip()
            if url:
                out.append({"title": item.text(0).strip() or url, "url": url})
        return out

    def _commit_tiles(self) -> None:
        self.settings.set_tiles(self._tiles_from_table())
        self._reload_tiles()

    def _tile_edited(self, _item, _column) -> None:
        self._commit_tiles()

    def _tile_add(self) -> None:
        from PyQt6.QtWidgets import QInputDialog

        if self.tile_table.topLevelItemCount() >= cfg.MAX_START_TILES:
            return
        url, ok = QInputDialog.getText(self, "Add a shortcut", "Address:",
                                       text="https://")
        if not ok or not url.strip():
            return
        if "://" not in url:
            url = "https://" + url.strip()
        name, ok = QInputDialog.getText(self, "Add a shortcut", "Name:",
                                        text=url.split("//")[-1].split("/")[0])
        if not ok:
            return
        self.settings.add_tile(name, url)
        self._reload_tiles()

    def _tile_remove(self) -> None:
        item = self.tile_table.currentItem()
        if item is None:
            return
        self.tile_table.takeTopLevelItem(
            self.tile_table.indexOfTopLevelItem(item))
        self._commit_tiles()

    def _tile_move(self, delta: int) -> None:
        item = self.tile_table.currentItem()
        if item is None:
            return
        index = self.tile_table.indexOfTopLevelItem(item)
        target = index + delta
        if not 0 <= target < self.tile_table.topLevelItemCount():
            return
        self.tile_table.takeTopLevelItem(index)
        self.tile_table.insertTopLevelItem(target, item)
        self.tile_table.setCurrentItem(item)
        self._commit_tiles()

    def _tile_reset(self) -> None:
        import copy

        self.settings.set_tiles(copy.deepcopy(cfg.DEFAULTS["start_tiles"]))
        self._reload_tiles()

    # ------------------------------------------------------------ shields
    def _shields_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._check("Block ads and trackers", "adblock_enabled"))
        layout.addWidget(self._check("Hide ad placeholders (cosmetic filtering)",
                                     "cosmetic_filtering"))
        layout.addWidget(self._check("Block third-party cookies",
                                     "block_third_party_cookies"))
        layout.addWidget(self._check("Send Do Not Track / Global Privacy Control",
                                     "send_do_not_track"))
        layout.addWidget(self._check(
            "Hide the graphics card from websites", "gpu_protection",
            "WebGL and WebGPU report your exact GPU and driver. That barely "
            "changes, so it identifies this machine across unrelated sites. "
            "Merlin reports one generic value, empties the WebGPU "
            "descriptors, and varies the extension list per site. Applies to "
            "new windows."))
        layout.addWidget(self._check("Upgrade navigations to HTTPS", "https_upgrade"))
        layout.addWidget(self._check("Prevent WebRTC local IP leaks",
                                     "block_webrtc_leak"))

        layout.addWidget(QLabel("Filter lists (one URL per line):", page))
        self.lists_edit = QPlainTextEdit(
            "\n".join(self.settings.get("filter_lists", [])), page)
        self.lists_edit.setMaximumHeight(120)
        layout.addWidget(self.lists_edit)

        row = QHBoxLayout()
        save_button = QPushButton("Save lists", page)
        update_button = QPushButton("Update filter lists now", page)
        save_button.clicked.connect(self._save_lists)
        update_button.clicked.connect(self._update_lists)
        row.addWidget(save_button)
        row.addWidget(update_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.rule_label = QLabel(page)
        self._refresh_rule_label()
        layout.addWidget(self.rule_label)

        clear_button = QPushButton("Clear per-site shield exceptions", page)
        clear_button.clicked.connect(
            lambda: self.settings.set("shields_exceptions", []))
        layout.addWidget(clear_button)
        layout.addStretch(1)
        return page

    def _refresh_rule_label(self) -> None:
        engine = getattr(self.window_ref, "filter_engine", None)
        count = engine.rule_count if engine else 0
        self.rule_label.setText(f"{count:,} network rules loaded")
        self.rule_label.setStyleSheet("color:#9a9ba1;")

    def _save_lists(self) -> None:
        urls = [line.strip() for line in self.lists_edit.toPlainText().splitlines()
                if line.strip().startswith("http")]
        self.settings.set("filter_lists", urls)

    def _update_lists(self) -> None:
        self._save_lists()
        loader = getattr(self.window_ref, "filter_loader", None)
        if loader:
            loader.refresh_async()
            self.rule_label.setText("Downloading filter lists in the background...")

    # -------------------------------------------------------------- media
    def _media_tab(self) -> QWidget:
        from . import media

        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel("<b>How Merlin plays media the engine refuses</b>",
                                page))

        self.mode_box = QComboBox(page)
        self._modes = [
            ("embedded", "In a tab, as a separate process"),
            ("window", "In its own window, as a separate process"),
            ("libvlc", "In a tab, inside this process (libVLC)"),
            ("off", "Disabled"),
        ]
        for _key, label in self._modes:
            self.mode_box.addItem(label)
        current = self.settings.get("player_mode", "embedded")
        for i, (key, _label) in enumerate(self._modes):
            if key == current:
                self.mode_box.setCurrentIndex(i)
        self.mode_box.currentIndexChanged.connect(self._mode_changed)
        layout.addWidget(self.mode_box)

        self.mode_note = QLabel("", page)
        self.mode_note.setWordWrap(True)
        self.mode_note.setStyleSheet("color:#9a9ba1; font-size:12px;")
        layout.addWidget(self.mode_note)
        self._refresh_mode_note()

        form = QFormLayout()
        detected = media.find_player()
        self.player_edit = QLineEdit(self.settings.get("player_command"), page)
        self.player_edit.setPlaceholderText(
            f"auto-detected: {os.path.basename(detected)}" if detected
            else "no player found; install mpv or vlc")
        self.player_edit.editingFinished.connect(
            lambda: self.settings.set("player_command",
                                      self.player_edit.text().strip()))
        form.addRow("Player command", self.player_edit)

        args_edit = QLineEdit(self.settings.get("player_args"), page)
        args_edit.setPlaceholderText("--ytdl-format=bestvideo+bestaudio")
        args_edit.editingFinished.connect(
            lambda: self.settings.set("player_args", args_edit.text().strip()))
        form.addRow("Extra arguments", args_edit)
        layout.addLayout(form)

        layout.addWidget(self._check(
            "Offer the player when a page's media fails to decode",
            "auto_offer_player"))

        probe_button = QPushButton("Open codec report", page)
        probe_button.clicked.connect(self.window_ref.show_codecs)
        layout.addWidget(probe_button)

        bits = []
        embeddable, why = media.embedding_supported()
        bits.append("Tab embedding: " + ("available" if embeddable else why))
        bits.append("libVLC bindings: " + (media.libvlc_version() or "not installed"))
        bits.append("yt-dlp: " + ("installed" if media.has_ytdlp()
                                  else "not installed (needed for streaming sites)"))
        status = QLabel("\n".join(bits), page)
        status.setWordWrap(True)
        status.setStyleSheet("color:#9a9ba1; font-size:12px;")
        layout.addWidget(status)
        layout.addStretch(1)
        return page

    def _background_changed(self, index: int) -> None:
        from PyQt6.QtWidgets import QFileDialog, QMessageBox

        if index < len(self._bg_keys):
            self.settings.set("start_background", self._bg_keys[index])
            self.bg_path_label.setText("")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a background image", os.path.expanduser("~"),
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp)")
        if not path:
            current = self.settings.get("start_background", "midnight")
            if current in self._bg_keys:
                self.bg_box.setCurrentIndex(self._bg_keys.index(current))
            return
        if not image_data_uri(path):
            QMessageBox.warning(
                self, "Image not usable",
                "That file could not be read, or it is larger than 6 MB.\n\n"
                "The image is embedded directly in the new-tab page, so very "
                "large files would slow every new tab down.")
            return
        self.settings.set("start_background", "image:" + path)
        self.bg_path_label.setText(path)

    def _mode_changed(self, index: int) -> None:
        from . import media
        from PyQt6.QtWidgets import QMessageBox

        key = self._modes[index][0]
        if key == "libvlc" and not self.settings.get("libvlc_ack"):
            answer = QMessageBox.warning(
                self, "Run the player inside the browser?",
                media.LIBVLC_NOTE + "\n\nEnable it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                for i, (k, _l) in enumerate(self._modes):
                    if k == self.settings.get("player_mode", "embedded"):
                        self.mode_box.setCurrentIndex(i)
                return
            self.settings.set("libvlc_ack", True)
        if key == "libvlc" and not media.has_libvlc():
            QMessageBox.information(
                self, "python-vlc missing",
                "In-process playback needs the VLC bindings:\n\n"
                "  sudo apt install vlc-plugin-base python3-vlc")
        self.settings.set("player_mode", key)
        self._refresh_mode_note()

    def _refresh_mode_note(self) -> None:
        from . import media

        key = self.settings.get("player_mode", "embedded")
        notes = {
            "embedded": "mpv or VLC runs as a child process and its video output "
                        "is reparented into the tab, so it looks embedded while "
                        "FFmpeg stays in another address space. X11 or XWayland "
                        "only.",
            "window": "The player opens normally, in its own window. The most "
                      "robust option, and the only one that works on native "
                      "Wayland.",
            "libvlc": media.LIBVLC_NOTE,
            "off": "Media the engine cannot decode simply fails, as it would in "
                   "any other browser.",
        }
        self.mode_note.setText(notes.get(key, ""))

    # ------------------------------------------------------------ updates
    def _updates_tab(self) -> QWidget:
        from .brand import APP_NAME, APP_VERSION
        from . import updater as upd

        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel(
            f"<b>{APP_NAME} Browser {APP_VERSION}</b>", page))

        source = QLabel(
            f"Checks <code>version.txt</code> in<br>"
            f"<a href='{upd.REPO_URL}' style='color:#8fa9f2'>"
            f"{upd.REPO_OWNER}/{upd.REPO_NAME}</a>", page)
        source.setOpenExternalLinks(True)
        source.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(source)

        self.update_status = QLabel("", page)
        self.update_status.setWordWrap(True)
        layout.addWidget(self.update_status)

        self.update_notes = QPlainTextEdit(page)
        self.update_notes.setReadOnly(True)
        self.update_notes.setMaximumHeight(110)
        self.update_notes.setPlaceholderText("Release notes appear here.")
        layout.addWidget(self.update_notes)

        row = QHBoxLayout()
        self._pending_version = ""
        self.check_button = QPushButton("Check now", page)
        self.check_button.clicked.connect(self._check_updates)
        row.addWidget(self.check_button)

        open_button = QPushButton("Open releases page", page)
        open_button.clicked.connect(self.window_ref.open_releases)
        row.addWidget(open_button)
        row.addStretch(1)
        layout.addLayout(row)

        layout.addWidget(self._check("Check for updates at start-up",
                                     "check_updates_on_start"))

        note = QLabel(
            "Merlin never downloads or installs updates by itself. It tells you "
            "a newer version exists and links to it; you decide what to run.",
            page)
        note.setWordWrap(True)
        note.setStyleSheet("color:#9a9ba1; font-size:12px;")
        layout.addWidget(note)
        layout.addStretch(1)

        # Connect to bound methods of this dialog, never to lambdas.
        # A lambda's receiver context is the sender, so Qt cannot drop the
        # connection when the dialog is destroyed: the connections piled up,
        # one more per open, and the next signal reached widgets whose C++
        # objects were already gone. That is a use-after-free, and it is what
        # made opening and closing menus and dialogs quickly kill the browser.
        # With a bound method of a QObject, Qt disconnects automatically the
        # moment the receiver dies.
        updater = self.window_ref.updater
        updater.status.connect(self._show_update_status)
        updater.available.connect(self._show_update_notes)
        updater.installed.connect(self._update_finished)
        if updater.latest:
            self.update_status.setText(
                f"Latest seen: {updater.latest}")
            self.update_notes.setPlainText(updater.notes)
        return page

    def closeEvent(self, event):  # noqa: N802
        """Release our updater connections explicitly.

        Relying on receiver destruction alone still left the connection count
        climbing by one per dialog, so this severs them deterministically.
        """
        updater = getattr(self.window_ref, "updater", None)
        if updater is not None:
            for signal, slot in (
                (updater.status, self._show_update_status),
                (updater.available, self._show_update_notes),
            ):
                try:
                    signal.disconnect(slot)
                except TypeError:
                    pass
        super().closeEvent(event)

    def _show_update_status(self, text: str) -> None:
        self.update_status.setText(text)

    def _show_update_notes(self, version: str, notes: str) -> None:
        self.update_notes.setPlainText(notes)
        # a newer version exists, so the button becomes the way to get it
        self.check_button.setText(f"Download and install {version}")
        self.check_button.setToolTip(
            "Replaces the application files in place. Your settings, "
            "bookmarks, history and the virtualenv are left alone.")
        self._pending_version = version

    def _update_finished(self, ok: bool, message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox

        self.update_status.setText(message)
        self.check_button.setEnabled(True)
        if ok:
            self.check_button.setText("Check now")
            self._pending_version = ""
            QMessageBox.information(self, "Update installed", message)
        else:
            QMessageBox.warning(self, "Update not installed", message)

    def _check_updates(self) -> None:
        """Check, or install if a check has already found something."""
        if getattr(self, "_pending_version", ""):
            self.check_button.setEnabled(False)
            self.update_status.setText("Downloading...")
            self.window_ref.updater.install_latest()
            return
        self.update_notes.clear()
        self.window_ref.updater.check()

    # ----------------------------------------------------------- advanced
    def _advanced_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        agent = QLineEdit(self.settings.get("user_agent"), page)
        agent.setPlaceholderText("Leave empty for the engine default")
        agent.editingFinished.connect(
            lambda: self.settings.set("user_agent", agent.text().strip()))
        form.addRow("User agent", agent)

        flags = QLineEdit(self.settings.get("chromium_flags"), page)
        flags.setPlaceholderText("--disable-reading-from-canvas --force-dark-mode")
        flags.editingFinished.connect(
            lambda: self.settings.set("chromium_flags", flags.text().strip()))
        form.addRow("Extra engine flags", flags)

        from . import privacy

        proxy_box = QComboBox(page)
        self._proxy_modes = [
            ("none", "No proxy"),
            ("tor", "Tor, via a local daemon"),
            ("custom", "Custom proxy"),
        ]
        for _key, label in self._proxy_modes:
            proxy_box.addItem(label)
        current_mode = self.settings.get("proxy_mode", "none")
        for i, (key, _l) in enumerate(self._proxy_modes):
            if key == current_mode:
                proxy_box.setCurrentIndex(i)
        proxy_box.currentIndexChanged.connect(
            lambda i: self.settings.set("proxy_mode", self._proxy_modes[i][0]))
        form.addRow("Proxy", proxy_box)

        proxy_url = QLineEdit(self.settings.get("proxy_url"), page)
        proxy_url.setPlaceholderText("socks5://127.0.0.1:1080")
        proxy_url.editingFinished.connect(
            lambda: self.settings.set("proxy_url", proxy_url.text().strip()))
        form.addRow("Proxy address", proxy_url)

        port = privacy.find_tor_port()
        proxy_note = QLabel(
            ("Tor daemon detected on 127.0.0.1:%d." % port if port
             else "No local Tor daemon found on 127.0.0.1:9050 or :9150.")
            + "\n\nRouting through Tor hides your address from sites, but it "
              "is not Tor Browser: that also normalises screen size, fonts and "
              "timezone so its users look alike. Merlin does not, so sites can "
              "still fingerprint this browser.\n\n"
              "Proxy changes take effect when Merlin restarts. A Tor window "
              "from the menu starts its own process straight away.", page)
        proxy_note.setWordWrap(True)
        proxy_note.setStyleSheet("color:#9a9ba1; font-size:12px;")
        form.addRow(proxy_note)

        clear_history = QPushButton("Clear browsing history", page)
        clear_history.clicked.connect(self.window_ref.clear_history)
        form.addRow(clear_history)

        clear_cache = QPushButton("Clear cache and cookies", page)
        clear_cache.clicked.connect(self.window_ref.clear_cache)
        form.addRow(clear_cache)

        # Shown in the dialog rather than behind a console flag, because
        # "which build am I actually running, and did the Windows icon step
        # work" are the two questions that keep needing an answer.
        import sys as _sys

        from .brand import APP_VERSION, icon_path
        from .winicon import process_image as privacy_image

        lines = [f"Version: {APP_VERSION}",
                 f"Package: {os.path.dirname(os.path.abspath(cfg.__file__))}",
                 f"Running as: {os.path.basename(privacy_image())}"]
        if os.name == "nt":
            from .winexe import build_group, group_size_in_exe, parse_ico

            expected = 0
            path = icon_path()
            if path:
                try:
                    with open(path, "rb") as fh:
                        expected = len(build_group(parse_ico(fh.read())))
                except Exception:                        # noqa: BLE001
                    expected = 0
            from .winicon import is_store_python

            image = privacy_image()
            if is_store_python():
                lines.append(
                    "Taskbar icon: this is the Microsoft Store build of "
                    "Python. Windows takes a Store app's taskbar icon from its "
                    "package, so Merlin's window icon is set and then ignored. "
                    "Install Python from python.org and reinstall Merlin to "
                    "fix it.")
                embedded = expected = 0
            embedded = group_size_in_exe(image)
            if not os.path.basename(image).lower().startswith("merlin"):
                lines.append(
                    "Taskbar icon: the window is hosted by "
                    f"{os.path.basename(image)}, not Merlin.exe, so the "
                    "taskbar falls back to that program's icon. The shortcut "
                    "tag is what makes Windows use Merlin's instead.")
            elif embedded and embedded == expected:
                lines.append("Taskbar icon: embedded in Merlin.exe correctly")
            elif embedded:
                lines.append(f"Taskbar icon: Merlin.exe holds a {embedded}-byte "
                             f"icon, expected {expected}")
            else:
                lines.append("Taskbar icon: NOT embedded; Merlin.exe still has "
                             "the interpreter's icon")
        diagnostics = QLabel("\n".join(lines), page)
        diagnostics.setWordWrap(True)
        diagnostics.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        diagnostics.setStyleSheet("color:#9a9ba1; font-size:12px;")
        form.addRow(diagnostics)

        paths = QLabel(
            f"Config: {cfg.CONFIG_DIR}\nData: {cfg.DATA_DIR}\nCache: {cfg.CACHE_DIR}",
            page)
        paths.setStyleSheet("color:#9a9ba1; font-size:12px;")
        form.addRow(paths)

        note = QLabel(
            "User agent and engine flag changes take effect after a restart.", page)
        note.setStyleSheet("color:#9a9ba1; font-size:12px;")
        form.addRow(note)
        return page


class Separator(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.VLine)
        self.setStyleSheet("color:#34363d;")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)


def user_stylesheet_script(css: str, name: str):
    """Build a QWebEngineScript that injects CSS at document creation."""
    from PyQt6.QtWebEngineCore import QWebEngineScript
    import json as _json

    source = (
        "(function(){var css=" + _json.dumps(css) + ";"
        "function add(){var s=document.createElement('style');"
        "s.setAttribute('data-merlin','1');s.textContent=css;"
        "(document.head||document.documentElement).appendChild(s);}"
        "if(document.documentElement)add();"
        "else document.addEventListener('DOMContentLoaded',add);})();"
    )
    script = QWebEngineScript()
    script.setName(name)
    script.setSourceCode(source)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
    script.setRunsOnSubFrames(True)
    return script


def path_exists(path: str) -> bool:
    return bool(path) and os.path.exists(path)
