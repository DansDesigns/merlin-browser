"""View page source, to select and copy, and Save page as, in several forms.

Both work for either kind of tab. A Merlin Engine tab has the page exactly as
it came, and the stylesheets it loaded; a Chromium tab has the page as it is
now, after its scripts have run, which Chromium provides.
"""
from __future__ import annotations

import os
import re

from PyQt6.QtCore import QStandardPaths, Qt, QUrl
from PyQt6.QtGui import QFont, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                             QPlainTextEdit, QPushButton, QVBoxLayout)


def _is_engine(view) -> bool:
    return type(view).__name__ == "MerlinView"


def _file_name(title: str, fallback: str = "page") -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title or "").strip(" .")
    return (name or fallback)[:120]


def _downloads() -> str:
    return QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DownloadLocation) or os.path.expanduser("~")


# ------------------------------------------------------------------ source

class SourceViewer(QDialog):
    """A page's source as plain text: select any part, or copy it all.

    Merlin Engine's own view of view-source: could not be selected; this can,
    for either engine. For a Merlin Engine tab each stylesheet the page loaded
    is here too, as a layout problem often lies in them.
    """

    def __init__(self, window, view):
        super().__init__(window)
        self.window_ref = window
        self.view = view
        self.setWindowTitle(f"Source of {view.url().toString()}")
        self.resize(980, 720)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.which = QComboBox(self)
        top.addWidget(QLabel("Show:", self))
        top.addWidget(self.which, 1)
        layout.addLayout(top)
        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.text.setFont(mono)
        layout.addWidget(self.text, 1)
        buttons = QHBoxLayout()
        self.size_label = QLabel(self)
        buttons.addWidget(self.size_label, 1)
        wrap = QPushButton("Wrap lines", self)
        wrap.setCheckable(True)
        wrap.toggled.connect(lambda on: self.text.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if on else QPlainTextEdit.LineWrapMode.NoWrap))
        copy = QPushButton("Copy all", self)
        copy.clicked.connect(self.copy_all)
        in_tab = QPushButton("Open in a tab", self)
        in_tab.setToolTip("The page's source in a tab of its own, as before")
        in_tab.clicked.connect(self.open_in_tab)
        close = QPushButton("Close", self)
        close.clicked.connect(self.close)
        for button in (wrap, copy, in_tab, close):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self.copy_all)
        self.sources: list = []
        self.which.currentIndexChanged.connect(self._show)
        self._gather()

    def _gather(self) -> None:
        view = self.view
        if _is_engine(view):
            self.sources.append(("Page HTML, as it came", view._markup or ""))
            links = []
            if view._document is not None:
                links = view._document.stylesheet_sources()
            sheets = view._sheets if view._sheets is not None else []
            for number, text in enumerate(sheets):
                where = "inline <style>"
                if number < len(links) and links[number][0] == "link":
                    where = view.url().resolved(QUrl(links[number][1])).toString()
                self.sources.append((f"Stylesheet {number + 1}: {where}", text or ""))
            self._fill()
            return
        # Chromium: the page as it is now, which it hands over when ready
        self.sources.append(("Page HTML, as it is now", "Loading..."))
        self._fill()
        try:
            view.page().toHtml(self._chromium_html)
        except Exception as exc:                               # noqa: BLE001
            self._chromium_html(f"Could not read the page: {exc}")

    def _chromium_html(self, html: str) -> None:
        try:
            self.sources[0] = ("Page HTML, as it is now", html or "")
            if self.which.currentIndex() == 0:
                self._show(0)
        except RuntimeError:
            pass                                    # the window was closed first

    def _fill(self) -> None:
        self.which.blockSignals(True)
        self.which.clear()
        for label, _text in self.sources:
            self.which.addItem(label)
        self.which.blockSignals(False)
        self._show(0)

    def _show(self, index: int) -> None:
        if 0 <= index < len(self.sources):
            text = self.sources[index][1]
            self.text.setPlainText(text)
            lines = text.count("\n") + 1 if text else 0
            self.size_label.setText(f"{len(text):,} characters, {lines:,} lines")

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self.text.toPlainText())
        self.size_label.setText(self.size_label.text().split(" \u2014")[0] + " \u2014 copied")

    def open_in_tab(self) -> None:
        self.window_ref.new_tab("view-source:" + self.view.url().toString())
        self.close()


# ------------------------------------------------------------------ save as

FORMATS = [
    ("html", "Web page, HTML only (*.html)"),
    ("complete", "Web page, complete (*.mhtml *.html)"),
    ("pdf", "PDF document (*.pdf)"),
    ("txt", "Plain text (*.txt)"),
    ("png", "Picture of the page (*.png)"),
    ("zip", "Page and stylesheets, for debugging (*.zip)"),
]


def save_page_as(window) -> None:
    """Main menu > Save page as...: ask where and in what form, then save."""
    view = window.current()
    if view is None:
        window.status_label.setText("There is no page to save")
        return
    engine = _is_engine(view)
    name = _file_name(view.title() or view.url().host())
    filters = [label for _kind, label in FORMATS]
    if engine:
        # Merlin Engine's complete page is one HTML file, its stylesheets in it
        filters[1] = "Web page, complete (*.html)"
    path, chosen = QFileDialog.getSaveFileName(
        window, "Save page as", os.path.join(_downloads(), name + ".html"), ";;".join(filters))
    if not path:
        return
    kind = FORMATS[filters.index(chosen)][0] if chosen in filters else "html"
    extension = {"html": ".html", "complete": ".html" if engine else ".mhtml", "pdf": ".pdf",
                 "txt": ".txt", "png": ".png", "zip": ".zip"}[kind]
    if not os.path.splitext(path)[1]:
        path += extension
    try:
        if engine:
            _save_engine(window, view, kind, path)
        else:
            _save_chromium(window, view, kind, path)
    except Exception as exc:                                   # noqa: BLE001
        window.status_label.setText(f"Could not save the page: {exc}")


def _done(window, path: str) -> None:
    window.status_label.setText(f"Saved {os.path.basename(path)} to {os.path.dirname(path)}")


def _save_engine(window, view, kind: str, path: str) -> None:
    if kind == "zip":
        from .updater import APP_VERSION

        made = view.save_for_debugging(os.path.dirname(path), APP_VERSION)
        if os.path.abspath(made) != os.path.abspath(path):
            os.replace(made, path)
    elif kind == "html":
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(view._markup or "")
    elif kind == "complete":
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(view.html_with_stylesheets())
    elif kind == "txt":
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(view.page_text())
    elif kind == "pdf":
        view.save_pdf(path)
    elif kind == "png":
        view.save_picture(path)
    _done(window, path)


def _save_chromium(window, view, kind: str, path: str) -> None:
    page = view.page()

    def write(text: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text or "")
        _done(window, path)

    if kind == "html":
        page.toHtml(write)
    elif kind == "txt":
        page.toPlainText(write)
    elif kind == "pdf":
        def printed(where, ok):
            window.status_label.setText(f"Saved {os.path.basename(where)} to {os.path.dirname(where)}"
                                        if ok else "Could not save the page as PDF")
            try:
                page.pdfPrintingFinished.disconnect(printed)
            except Exception:                                  # noqa: BLE001
                pass
        page.pdfPrintingFinished.connect(printed)
        page.printToPdf(path)
    elif kind == "png":
        view.grab().save(path, "PNG")
        _done(window, path)
    elif kind == "complete":
        from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest

        # Chromium saves it through a download; Merlin's handler then uses the
        # place already chosen here rather than asking again
        window._saving_page_path = path
        page.save(path, QWebEngineDownloadRequest.SavePageFormat.MimeHtmlSaveFormat)
    elif kind == "zip":
        import json
        import zipfile

        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice

        shot = QByteArray()
        buffer = QBuffer(shot)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        view.grab().toImage().save(buffer, "PNG")

        def zipped(html: str) -> None:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("page.html", html or "")
                bundle.writestr("screenshot.png", bytes(shot))
                bundle.writestr("manifest.json", json.dumps(
                    {"url": view.url().toString(), "engine": "chromium",
                     "viewport": [view.width(), view.height()]}, indent=2))
            _done(window, path)
        page.toHtml(zipped)
