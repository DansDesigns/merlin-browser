"""Certificate problems: gathered per site behind a padlock, not asked one by one.

A page's images, video and scripts can each come with a certificate problem,
and asking about every one, as Merlin did, meant fifteen questions for one
YouTube page. Now the items a page pulls in are held back quietly and
counted; the address bar shows a warning padlock, and clicking it says what
was held back and offers to allow the site's certificates for this session. A
problem with the page itself is asked about once, in the notice bar.

"Not yet valid" nearly always means the computer's clock is behind: new
certificates look as if they start in the future. The padlock's window asks
the server for its time and says so when that is the cause.
"""
from __future__ import annotations

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QTextEdit,
                             QVBoxLayout)

from .clock import clock_skew, describe_skew        # noqa: F401

# sites whose certificate problems were allowed, for this session only: site ->
# "dates" (only the dates set aside, as for a clock that is wrong) or "any"
ALLOWED: dict = {}


def allow(site: str, level: str = "any") -> None:
    if site and (ALLOWED.get(site) != "any"):
        ALLOWED[site] = level


def level_for(problems) -> str:
    """"dates" when every problem is about dates; otherwise "any"."""
    from .clock import is_date_problem

    return "dates" if problems and all(is_date_problem(p) for p in problems) else "any"
CLOCK_CHECKED = False       # the clock is asked about once a session
CLOCK_SKEW = None


def site_of(url: QUrl) -> str:
    """The site a page belongs to: accounts.example.co.uk -> example.co.uk."""
    from .adblock import _registrable

    host = (url.host() or "").lower()
    return _registrable(host) if host else ""


class CertificateWindow(QDialog):
    """The padlock's window: what was held back, the clock, and allowing."""

    def __init__(self, window, view, site: str, troubles: list):
        super().__init__(window)
        self.window_ref = window
        self.view = view
        self.site = site
        self.level = level_for([t["description"] for t in troubles])
        self.setWindowTitle(f"Certificate problems on {site}")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        count = len(troubles)
        hosts = {}
        for trouble in troubles:
            hosts.setdefault(trouble["host"], []).append(trouble)
        heading = QLabel(
            f"<b>{count} item{'s' if count != 1 else ''} on this page "
            f"{'were' if count != 1 else 'was'} held back</b> because "
            f"{'their certificates' if count != 1 else 'its certificate'} could not be "
            f"trusted. The page itself may still work.", self)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        details = QTextEdit(self)
        details.setReadOnly(True)
        lines = []
        for host, items in sorted(hosts.items()):
            reasons = sorted({i["description"] for i in items})
            lines.append(f"{host}: {len(items)} item{'s' if len(items) != 1 else ''}\n  "
                         + "\n  ".join(reasons))
            for item in items[:3]:
                lines.append(f"    {item['url'][:140]}")
        details.setPlainText("\n".join(lines))
        layout.addWidget(details, 1)
        self.clock = QLabel("Checking the time with the server...", self)
        self.clock.setWordWrap(True)
        dated = [t for t in troubles if "valid" in t["description"].lower()
                 or "expired" in t["description"].lower() or "date" in t["kind"].lower()]
        if dated:
            layout.addWidget(self.clock)
            self._ask_the_time(dated[0]["host"])
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        allow = QPushButton(f"Allow for {site}", self)
        allow.setToolTip(f"Accept certificate problems on {site}'s pages until Merlin closes, "
                         f"and reload the page")
        allow.clicked.connect(self.allow)
        keep = QPushButton("Keep blocking", self)
        keep.clicked.connect(self.close)
        buttons.addWidget(allow)
        buttons.addWidget(keep)
        layout.addLayout(buttons)

    def _ask_the_time(self, host: str) -> None:
        import threading

        answer = {}

        def work():
            try:
                answer["skew"] = clock_skew(host)
            except Exception:                                  # noqa: BLE001
                answer["skew"] = None
            answer["done"] = True

        threading.Thread(target=work, daemon=True).start()

        def check():
            try:
                if answer.get("done"):
                    self.clock.setText(describe_skew(answer.get("skew")))
                else:
                    QTimer.singleShot(200, check)
            except RuntimeError:
                pass                                   # the window was closed
        QTimer.singleShot(200, check)

    def allow(self) -> None:
        allow(self.site, self.level)
        try:
            self.view.cert_troubles = []
        except Exception:                                      # noqa: BLE001
            pass
        self.window_ref._update_cert_indicator()
        self.view.reload()
        self.close()
