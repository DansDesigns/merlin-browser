"""Merlin's player, placed over the page's own video.

When the page cannot decode a video, a live stream in H.264 for instance,
Merlin's built-in player plays it instead, and it does so in the same place:
laid over the page's own player, the same size, following it as the page
scrolls or resizes, rather than in a tab of its own. The page stays as it was,
chat and all.

The picture is drawn by FrameView, an ordinary widget Merlin paints itself, so
it stacks correctly above the page. Playback runs in PlaybackWorker on its own
thread, exactly as in the player tab, so a stop that sticks inside Qt
Multimedia cannot freeze the browser.
"""
from __future__ import annotations

from PyQt6.QtCore import QRect, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .playertab import _ABANDONED, FrameView, PlaybackWorker

# Where the page's player is, in CSS pixels, and whether it can be seen. The
# page's own video is kept paused and silent while Merlin's plays over it.
PLACE_JS = r"""
(function () {
  var box = document.getElementById('movie_player')
         || document.querySelector('video');
  document.querySelectorAll('video').forEach(function (v) {
    if (!v.paused) { v.pause(); }
    v.muted = true;
  });
  if (!box) { return {found: false}; }
  var r = box.getBoundingClientRect();
  return {found: true, x: r.left, y: r.top, w: r.width, h: r.height,
          hidden: document.hidden || r.width < 20 || r.height < 20};
})()
"""


class InPagePlayer(QWidget):
    """Merlin's player over the page's own video area."""

    closed = pyqtSignal()
    _ask_toggle = pyqtSignal()
    _ask_shut_down = pyqtSignal()

    def __init__(self, view, stream: str, page: str, note=None):
        super().__init__(view)
        self.view = view
        self.stream = stream
        self.page = page
        self._note = note or (lambda _text: None)
        self._stopped = False
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("InPagePlayer { background: #000; }")

        self.picture = FrameView(self)
        self.picture.setMinimumSize(0, 0)
        self.picture.mousePressEvent = lambda _event: self._ask_toggle.emit()

        # a thin bar along the bottom, like the page's own controls
        bar = QWidget(self)
        bar.setObjectName("InPageBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setStyleSheet(
            "#InPageBar { background: rgba(0, 0, 0, 170); }"
            "#InPageBar QLabel { color: #f2f3f7; background: transparent; }"
            "#InPageBar QPushButton { color: #f2f3f7; background: transparent;"
            " border: none; padding: 2px 8px; font-size: 14px; }"
            "#InPageBar QPushButton:hover { background: rgba(255,255,255,40);"
            " border-radius: 4px; }")
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 2, 6, 2)
        self.play_button = QPushButton("\u23f8", bar)
        self.play_button.setToolTip("Play or pause")
        self.play_button.clicked.connect(self._ask_toggle.emit)
        self.status = QLabel("Merlin's player: connecting...", bar)
        close_button = QPushButton("\u2715", bar)
        close_button.setToolTip("Close Merlin's player")
        close_button.clicked.connect(self.stop)
        row.addWidget(self.play_button)
        row.addWidget(self.status, 1)
        row.addWidget(close_button)
        self.bar = bar

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.picture, 1)
        layout.addWidget(bar)

        # no parent: the thread must be able to outlive this widget
        self._thread = QThread()
        self._worker = PlaybackWorker(stream)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._worker.image.connect(self._first_picture)
        self._worker.image.connect(self.picture.show_image)
        self._worker.playing.connect(self._playing)
        self._worker.progress.connect(self._progress)
        self._worker.failed.connect(self._failed)
        self._ask_toggle.connect(self._worker.toggle)
        self._ask_shut_down.connect(self._worker.shut_down)
        self._had_picture = False

        self.hide()
        self._place_timer = QTimer(self)
        self._place_timer.setInterval(150)
        self._place_timer.timeout.connect(self._ask_where)
        self._place_timer.start()
        self._ask_where()
        self._thread.start()
        self._note("in-page player: starting")

    # ------------------------------------------------------------ placement
    def _ask_where(self) -> None:
        if self._stopped:
            return
        try:
            self.view.page().runJavaScript(PLACE_JS, self._place)
        except RuntimeError:
            self.stop()                                  # the view has gone

    def _place(self, where) -> None:
        if self._stopped or not isinstance(where, dict) or not where.get("found"):
            if not self._stopped:
                self.hide()
            return
        if where.get("hidden"):
            self.hide()
            return
        zoom = self.view.zoomFactor()
        rect = QRect(round(where["x"] * zoom), round(where["y"] * zoom),
                     round(where["w"] * zoom), round(where["h"] * zoom))
        rect = rect.intersected(self.view.rect())
        if rect.width() < 20 or rect.height() < 20:
            self.hide()
            return
        if self.geometry() != rect:
            self.setGeometry(rect)
        if not self.isVisible():
            self.show()
        self.raise_()

    # ------------------------------------------------------------ playback
    def _playing(self, playing: bool) -> None:
        self.play_button.setText("\u23f8" if playing else "\u25b6")

    def _first_picture(self, _image) -> None:
        if not self._had_picture:
            self._had_picture = True
            self._note("in-page player: playing")
            self._worker.image.disconnect(self._first_picture)

    def _progress(self, position: int, duration: int, seekable: bool) -> None:
        now = position // 1000
        if duration > 0 and seekable:
            length = duration // 1000
            self.status.setText(f"Merlin's player  {now // 60}:{now % 60:02d} / "
                                f"{length // 60}:{length % 60:02d}")
        else:
            self.status.setText(f"Merlin's player  LIVE  {now // 60}:{now % 60:02d}")

    def _failed(self, text: str) -> None:
        self.status.setText(f"Merlin's player could not play this: {text}")
        self._note(f"in-page player: could not play: {text}")

    def stop(self) -> None:
        """Stop playing and step aside, leaving the page as it was."""
        if self._stopped:
            return
        self._stopped = True
        self._place_timer.stop()
        worker, thread = self._worker, self._thread
        worker.want_frames = False
        # The worker reports once more as it stops, "not playing" at least,
        # by which time this widget may be gone. Qt drops connections to a
        # deleted object's methods by itself, but not to anything else, so
        # every connection from the worker is cut here, before asking it to
        # stop.
        for signal in (worker.image, worker.playing, worker.progress, worker.failed):
            try:
                signal.disconnect()
            except TypeError:
                pass
        self.picture.clear()
        # asked, not waited for: if Qt's stop sticks, only the worker waits
        _ABANDONED.add((worker, thread))
        thread.finished.connect(lambda pair=(worker, thread): _ABANDONED.discard(pair))
        self._ask_shut_down.emit()
        self.hide()
        self._note("in-page player: stopped")
        self.closed.emit()
        self.deleteLater()
