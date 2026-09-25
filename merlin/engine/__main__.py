"""A small window for trying MerlinEngine on its own:

    python -m merlin.engine https://example.com
    python -m merlin.engine page.html
"""
from __future__ import annotations

import os
import sys

from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (QApplication, QLineEdit, QMainWindow, QToolBar)

from .view import MerlinView


def main(argv: list) -> int:
    app = QApplication(sys.argv[:1])
    window = QMainWindow()
    window.resize(1000, 760)
    view = MerlinView(window)
    window.setCentralWidget(view)
    bar = QToolBar(window)
    window.addToolBar(bar)
    back = bar.addAction("\u2190")
    forward = bar.addAction("\u2192")
    reload = bar.addAction("\u21bb")
    address = QLineEdit(bar)
    bar.addWidget(address)
    back.triggered.connect(view.back)
    forward.triggered.connect(view.forward)
    reload.triggered.connect(view.reload)

    def go():
        text = address.text().strip()
        if os.path.exists(text):
            view.setUrl(QUrl.fromLocalFile(os.path.abspath(text)))
        else:
            view.setUrl(QUrl.fromUserInput(text))

    def sync(*_):
        address.setText(view.url().toString())
        back.setEnabled(view.history().canGoBack())
        forward.setEnabled(view.history().canGoForward())

    address.returnPressed.connect(go)
    view.urlChanged.connect(sync)
    view.loadFinished.connect(sync)
    view.titleChanged.connect(lambda t: window.setWindowTitle(f"{t} - MerlinEngine"))
    view.linkHovered.connect(lambda url: window.statusBar().showMessage(url))
    window.show()
    if argv:
        address.setText(argv[0])
        go()
    else:
        view.setHtml("<title>MerlinEngine</title><body style='font-family:sans-serif;margin:40px'>"
                     "<h1>MerlinEngine</h1><p>Type an address above.</p></body>")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
