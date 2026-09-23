"""The player tab.

Holds a video surface plus transport controls, and fills it one of two ways:

  * an external player process reparented into the surface (mpv --wid,
    VLC --drawable-xid), which keeps the decoder in its own address space, or
  * in-process libVLC through python-vlc, which is opt-in because it loads the
    whole FFmpeg stack into the browser.

The widget presents the same interface either way, so the browser does not care
which backend is in use.
"""
from __future__ import annotations

import os
import sys

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from . import media


class VideoSurface(QWidget):
    """A plain native widget for a player to draw into."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAutoFillBackground(True)
        self.setStyleSheet("background:#000;")
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


class FrameView(QWidget):
    """Video drawn by Merlin itself, one image at a time.

    Not QVideoWidget. In Qt 6 that puts the picture in a separate native
    window, wrapped in a QWindowContainer, and a native window always draws on
    top of the ordinary widgets around it. So the video covered the tab strip
    when it widened over the page, ignored the rounded page corners, and upset
    the repainting of the strip beside it. This is an ordinary widget, painted
    inside Merlin's own window, so it stacks like everything else.

    It is handed finished images, never decoded frames. A frame can point into
    the decoder's own buffers, which are freed when the player stops, and
    painting one after that read freed memory. The playback worker turns each
    frame into an image, which owns its memory, before it leaves that thread.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = None
        # every pixel is painted, so Qt need not clear behind it first
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMinimumSize(160, 90)

    def show_image(self, image) -> None:
        if image is not None and not image.isNull():
            self._image = image
            self.update()

    def current_image(self):
        return self._image

    def clear(self) -> None:
        """Forget the picture, when the player stops."""
        self._image = None
        self.update()

    def paintEvent(self, event) -> None:                 # noqa: N802
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QPainter

        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        image = self._image
        if image is not None:
            size = image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect((self.width() - size.width()) // 2,
                           (self.height() - size.height()) // 2,
                           size.width(), size.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(target, image)
        painter.end()


# Workers told to stop but not yet finished. Kept here, not dropped: Qt ends
# the whole program if a QThread is destroyed while it is still running, and
# a stop can get stuck inside Qt Multimedia (see PlaybackWorker).
_ABANDONED: set = set()


class PlaybackWorker(QObject):
    """Qt's media player, run on a thread of its own.

    QMediaPlayer.stop() can get stuck, in Qt Multimedia's own streaming engine:
    about one stop in thirty while a stream plays, in testing, with the old
    QVideoWidget just as with this. On the interface thread that froze the
    whole browser when a player tab was closed. Here it can only hold up this
    worker, which is then left to finish in its own time.

    The tab talks to it only through queued signals, and it answers the same
    way, so nothing on the interface thread ever waits on the player.
    """

    image = pyqtSignal(object)            # QImage, safe to keep and paint
    progress = pyqtSignal(int, int, bool)  # position ms, duration ms, seekable
    playing = pyqtSignal(bool)
    failed = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        # read from other threads; a plain flag is enough for a hint like this
        self.want_frames = True
        self._player = self._audio = self._sink = self._timer = None

    def start(self) -> None:
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._sink = QVideoSink(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoSink(self._sink)
        # direct: converted on whichever thread delivers the frame, while the
        # frame is certainly still valid, and only the image is passed on
        self._sink.videoFrameChanged.connect(
            self._frame, Qt.ConnectionType.DirectConnection)
        self._player.errorOccurred.connect(
            lambda _code, text: self.failed.emit(text))
        self._player.playbackStateChanged.connect(
            lambda state: self.playing.emit(
                state == QMediaPlayer.PlaybackState.PlayingState))
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._report)
        self._timer.start()
        self._player.setSource(QUrl(self.url))
        self._player.play()

    def _frame(self, frame) -> None:
        if not self.want_frames or frame is None or not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            self.image.emit(image)

    def _report(self) -> None:
        if self._player is not None:
            self.progress.emit(self._player.position(), self._player.duration(),
                               self._player.isSeekable())

    def toggle(self) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        if self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def seek(self, per_mille: int) -> None:
        if self._player is not None and self._player.isSeekable():
            length = self._player.duration()
            if length > 0:
                self._player.setPosition(int(length * per_mille / 1000))

    def shut_down(self) -> None:
        """Silence first, then stop: a stop that sticks must not keep playing."""
        self.want_frames = False
        if self._timer is not None:
            self._timer.stop()
        if self._audio is not None:
            self._audio.setMuted(True)
        if self._sink is not None:
            try:
                self._sink.videoFrameChanged.disconnect()
            except TypeError:
                pass
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        self.thread().quit()


class PlayerTab(QWidget):
    """A tab that plays one media URL."""

    titleChanged = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, url: str, settings, parent=None):
        super().__init__(parent)
        self.url = url
        self.settings = settings
        self.process = None
        self._vlc_instance = None
        self._vlc_player = None
        self._worker = None
        self._worker_thread = None
        self._qt_video = None
        self.backend = "none"
        # force_builtin: set by the browser for streams the web engine cannot
        # decode, so they play here even when no external player is installed
        self.force_builtin = False
        # the page a stream came from, for a readable tab name: the stream's
        # own address is a long CDN path that says nothing useful
        self.title_hint = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.surface = VideoSurface(self)
        layout.addWidget(self.surface, 1)

        bar = QWidget(self)
        bar.setStyleSheet("background:#1b1c20;")
        controls = QHBoxLayout(bar)
        controls.setContentsMargins(10, 6, 10, 6)

        self.btn_play = QPushButton("\u23f8", bar)
        self.btn_play.setFixedWidth(38)
        self.btn_play.clicked.connect(self.toggle_pause)
        self.btn_stop = QPushButton("\u23f9", bar)
        self.btn_stop.setFixedWidth(38)
        self.btn_stop.clicked.connect(self.stop)

        self.position = QSlider(Qt.Orientation.Horizontal, bar)
        self.position.setRange(0, 1000)
        self.position.sliderMoved.connect(self._seek)

        self.info = QLabel("", bar)
        self.info.setStyleSheet("color:#9a9ba1;font-size:12px;")

        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_stop)
        controls.addWidget(self.position, 1)
        controls.addWidget(self.info)
        layout.addWidget(bar)
        self.controls_bar = bar

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

        QTimer.singleShot(0, self.start)

    # ------------------------------------------------------------------
    def display_name(self) -> str:
        if self.title_hint:
            hint = QUrl(self.title_hint)
            host = (hint.host() or "").replace("www.", "")
            return f"\u25b6 {host}"[:38] if host else "\u25b6 Stream"
        name = os.path.basename(QUrl(self.url).path()) or self.url
        return name[:38] or "Player"

    def start(self) -> None:
        mode = self.settings.get("player_mode", "embedded")
        if mode == "libvlc" and media.has_libvlc():
            if self._start_libvlc():
                return
        # Qt's own multimedia module: nothing to install, and its FFmpeg has
        # H.264, which the web engine's does not. Used when asked for, and as
        # the fallback when no external player can be found.
        wants_builtin = self.force_builtin or mode == "builtin"
        if wants_builtin or not media.find_player(
                self.settings.get("player_command")):
            if self._start_builtin():
                return
        embeddable, why = media.embedding_supported()
        window_id = int(self.surface.winId()) if (mode == "embedded" and embeddable) else 0
        if mode == "embedded" and not embeddable:
            self.info.setText(f"Separate window: {why}")
        ok, message, proc = media.launch(
            self.url,
            self.settings.get("player_command"),
            window_id,
            self.settings.get("player_args"),
        )
        self.process = proc
        self.backend = "process"
        self.info.setText(message if not self.info.text() else self.info.text())
        if not ok:
            self.info.setText(message)
            return
        # an externally embedded player owns its own transport controls
        self.controls_bar.setVisible(window_id == 0 or True)
        self.position.setEnabled(False)
        self.btn_play.setEnabled(False)
        self.titleChanged.emit(self.display_name())

    def _start_libvlc(self) -> bool:
        try:
            import vlc
        except Exception:                                # noqa: BLE001
            return False
        args = ["--no-xlib"] if os.environ.get("QT_QPA_PLATFORM") == "offscreen" else []
        extra = (self.settings.get("player_args") or "").split()
        try:
            self._vlc_instance = vlc.Instance(args + extra)
            if self._vlc_instance is None:
                return False
            self._vlc_player = self._vlc_instance.media_player_new()
            player_media = self._vlc_instance.media_new(self.url)
            self._vlc_player.set_media(player_media)
            handle = int(self.surface.winId())
            if os.name == "nt":
                self._vlc_player.set_hwnd(handle)
            elif sys.platform == "darwin":
                self._vlc_player.set_nsobject(handle)
            else:
                self._vlc_player.set_xwindow(handle)
            self._vlc_player.play()
        except Exception as exc:                         # noqa: BLE001
            self.info.setText(f"libVLC failed: {exc}")
            return False
        self.backend = "libvlc"
        self.info.setText(f"libVLC {media.libvlc_version()}")
        self._timer.start()
        self.titleChanged.emit(self.display_name())
        return True

    # ------------------------------------------------------------- controls
    # the tab asks the worker, never calls into the player itself
    _ask_toggle = pyqtSignal()
    _ask_seek = pyqtSignal(int)
    _ask_shut_down = pyqtSignal()

    def _start_builtin(self) -> bool:
        """Play in Qt's multimedia module, on a worker thread, in this tab."""
        try:
            from PyQt6.QtMultimedia import QMediaPlayer  # noqa: F401

            video = FrameView(self)
        except Exception as exc:                         # noqa: BLE001
            self.info.setText(f"Built-in player unavailable: {exc}")
            return False

        layout = self.layout()
        index = layout.indexOf(self.surface)
        layout.insertWidget(index, video, 1)
        self.surface.hide()

        # no parent: the thread must be able to outlive this tab
        thread = QThread()
        worker = PlaybackWorker(self.url)
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        worker.image.connect(video.show_image)
        worker.progress.connect(self._builtin_progress)
        worker.playing.connect(self._builtin_state)
        worker.failed.connect(lambda text: self.info.setText(f"Could not play: {text}"))
        self._ask_toggle.connect(worker.toggle)
        self._ask_seek.connect(worker.seek)
        self._ask_shut_down.connect(worker.shut_down)
        thread.start()

        self._worker, self._worker_thread, self._qt_video = worker, thread, video
        self.backend = "builtin"
        self.btn_play.setEnabled(True)
        self.position.setEnabled(True)
        self.info.setText("Built-in player")
        self.titleChanged.emit(self.display_name())
        return True

    def _builtin_state(self, playing: bool) -> None:
        self.btn_play.setText("\u23f8" if playing else "\u25b6")

    def _builtin_progress(self, position: int, duration: int, seekable: bool) -> None:
        now, length = position // 1000, duration // 1000
        if length > 0 and seekable:
            self.position.setEnabled(True)
            if not self.position.isSliderDown():
                self.position.setValue(int(1000 * now / max(1, length)))
            self.info.setText(f"{now // 60}:{now % 60:02d} / "
                              f"{length // 60}:{length % 60:02d}")
        else:
            # a live stream has no end to seek towards
            self.position.setEnabled(False)
            self.info.setText(f"LIVE  {now // 60}:{now % 60:02d}")

    def showEvent(self, event) -> None:                  # noqa: N802
        super().showEvent(event)
        if self._worker is not None:
            self._worker.want_frames = True

    def hideEvent(self, event) -> None:                  # noqa: N802
        super().hideEvent(event)
        if self._worker is not None:
            # a player in a background tab keeps playing but draws nothing
            self._worker.want_frames = False

    def toggle_pause(self) -> None:
        if self._worker is not None:
            self._ask_toggle.emit()
            return
        if self._vlc_player is not None:
            self._vlc_player.pause()
            self.btn_play.setText(
                "\u25b6" if not self._vlc_player.is_playing() else "\u23f8")

    def stop(self) -> None:
        self._timer.stop()
        if self._worker is not None:
            # Asked, not waited for: if Qt's stop sticks, only the worker
            # waits. Kept alive until it finishes, however long that is.
            worker, thread = self._worker, self._worker_thread
            self._worker = self._worker_thread = None
            worker.want_frames = False
            if self._qt_video is not None:
                self._qt_video.clear()
            _ABANDONED.add((worker, thread))
            thread.finished.connect(lambda pair=(worker, thread): _ABANDONED.discard(pair))
            self._ask_shut_down.emit()
        if self._vlc_player is not None:
            try:
                self._vlc_player.stop()
            except Exception:                            # noqa: BLE001
                pass
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        self.closed.emit()

    def _seek(self, value: int) -> None:
        if self._worker is not None:
            self._ask_seek.emit(value)
            return
        if self._vlc_player is not None:
            self._vlc_player.set_position(value / 1000.0)

    def _tick(self) -> None:
        if self._vlc_player is None:
            return
        try:
            pos = self._vlc_player.get_position()
            if not self.position.isSliderDown() and pos >= 0:
                self.position.setValue(int(pos * 1000))
            length = self._vlc_player.get_length() // 1000
            now = self._vlc_player.get_time() // 1000
            if length > 0:
                self.info.setText(
                    f"{now // 60}:{now % 60:02d} / {length // 60}:{length % 60:02d}")
        except Exception:                                # noqa: BLE001
            pass

    def closeEvent(self, event):  # noqa: N802
        self.stop()
        super().closeEvent(event)
