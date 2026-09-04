"""The import dialog: pick a browser, choose what to bring over, import it."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QVBoxLayout,
)

from .importer import candidate_profiles, read_password_csv, read_source


class ImportDialog(QDialog):
    def __init__(self, settings, history, bookmarks, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.history = history
        self.bookmarks = bookmarks
        self.setWindowTitle("Import from another browser")
        self.resize(540, 480)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Browsers found on this computer</b>", self))

        self.sources = candidate_profiles()
        self.list = QListWidget(self)
        for name, _kind, path in self.sources:
            item = QListWidgetItem(name)
            item.setToolTip(path)
            self.list.addItem(item)
        if self.sources:
            self.list.setCurrentRow(0)
        else:
            self.list.addItem("No other browser profiles were found")
            self.list.setEnabled(False)
        layout.addWidget(self.list, 1)

        self.want_bookmarks = QCheckBox("Bookmarks", self)
        self.want_bookmarks.setChecked(True)
        self.want_history = QCheckBox("Browsing history", self)
        self.want_history.setChecked(True)
        layout.addWidget(self.want_bookmarks)
        layout.addWidget(self.want_history)

        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.report = QLabel("", self)
        self.report.setWordWrap(True)
        layout.addWidget(self.report)

        note = QLabel(
            "<b>Saved passwords</b><br>"
            "Merlin does not read another browser's password store. Those are "
            "encrypted with a key held by the operating system, and reading "
            "them would mean shipping the same technique used to steal saved "
            "logins.<br><br>"
            "Export them from the other browser instead, from its own password "
            "settings where it can ask you to confirm, then import that file "
            "here. Delete it afterwards: it is plain text.\n\n"
            + __import__("merlin.passwords", fromlist=["x"]).backend_note(), self)
        note.setWordWrap(True)
        note.setStyleSheet("color:#9a9ba1; font-size:12px;")
        layout.addWidget(note)

        row = QHBoxLayout()
        csv_button = QPushButton("Import passwords from CSV...", self)
        csv_button.clicked.connect(self._import_passwords)
        row.addWidget(csv_button)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(self)
        self.import_button = buttons.addButton(
            "Import", QDialogButtonBox.ButtonRole.AcceptRole)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.import_button.clicked.connect(self._run)
        close.clicked.connect(self.accept)
        layout.addWidget(buttons)
        self.import_button.setEnabled(bool(self.sources))

    def _run(self) -> None:
        row = self.list.currentRow()
        if not self.sources or row < 0:
            return
        name, kind, path = self.sources[row]

        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.report.setText(f"Reading {name}...")
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

        marks, hist = read_source(kind, path,
                                  self.want_bookmarks.isChecked(),
                                  self.want_history.isChecked())

        existing = {b.get("url") for b in self.bookmarks.items}
        added_marks = 0
        for entry in marks:
            if entry["url"] not in existing:
                self.bookmarks.add(entry["url"], entry["title"])
                existing.add(entry["url"])
                added_marks += 1

        added_history = 0
        for entry in hist:
            self.history.add(entry["url"], entry["title"])
            added_history += 1

        self.progress.setVisible(False)
        message = (f"Imported {added_marks} bookmarks and {added_history} "
                   f"history entries from {name}.")
        if not (added_marks or added_history):
            message += " Nothing new was found; it may already be imported."
        self.report.setText(message)

    def _import_passwords(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a password export", "",
            "CSV files (*.csv);;All files (*)")
        if not path:
            return
        entries, problem = read_password_csv(path)
        if problem:
            QMessageBox.warning(self, "Could not import", problem)
            return

        from . import passwords

        if not passwords.backend():
            QMessageBox.warning(self, "Cannot save logins",
                                passwords.backend_note())
            return
        added, trouble = passwords.add_many(entries)
        if trouble:
            QMessageBox.warning(self, "Could not save", trouble)
            return
        QMessageBox.information(
            self, "Logins imported",
            f"{len(entries)} read, {added} new ones saved.\n\n"
            + passwords.backend_note()
            + "\n\nDelete that CSV now: it holds your passwords in plain "
              "text. Use the key button in the toolbar, or the page's "
              "right-click menu, to fill a saved login.")
