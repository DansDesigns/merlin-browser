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

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
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
        self._qt_player = None
        self._qt_audio = None
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
    def _start_builtin(self) -> bool:
        """Play in Qt's multimedia module, inside this tab."""
        try:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PyQt6.QtMultimediaWidgets import QVideoWidget
        except Exception as exc:                         # noqa: BLE001
            self.info.setText(f"Built-in player unavailable: {exc}")
            return False

        video = QVideoWidget(self)
        video.setStyleSheet("background:#000;")
        layout = self.layout()
        index = layout.indexOf(self.surface)
        layout.insertWidget(index, video, 1)
        self.surface.hide()

        player = QMediaPlayer(self)
        audio = QAudioOutput(self)
        player.setAudioOutput(audio)
        player.setVideoOutput(video)
        player.errorOccurred.connect(
            lambda _code, text: self.info.setText(f"Could not play: {text}"))
        player.playbackStateChanged.connect(self._builtin_state)
        player.setSource(QUrl(self.url))
        player.play()

        self._qt_player, self._qt_audio, self._qt_video = player, audio, video
        self.backend = "builtin"
        self.btn_play.setEnabled(True)
        self.position.setEnabled(True)
        self.info.setText("Built-in player")
        self._timer.start()
        self.titleChanged.emit(self.display_name())
        return True

    def _builtin_state(self, state) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.btn_play.setText("\u23f8" if playing else "\u25b6")

    def toggle_pause(self) -> None:
        if self._qt_player is not None:
            from PyQt6.QtMultimedia import QMediaPlayer

            if self._qt_player.playbackState() == \
                    QMediaPlayer.PlaybackState.PlayingState:
                self._qt_player.pause()
            else:
                self._qt_player.play()
            return
        if self._vlc_player is not None:
            self._vlc_player.pause()
            self.btn_play.setText(
                "\u25b6" if not self._vlc_player.is_playing() else "\u23f8")

    def stop(self) -> None:
        self._timer.stop()
        if self._qt_player is not None:
            try:
                self._qt_player.stop()
                self._qt_player.setSource(QUrl())
            except Exception:                            # noqa: BLE001
                pass
        if self._vlc_player is not None:
            try:
                self._vlc_player.stop()
            except Exception:                            # noqa: BLE001
                pass
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        self.closed.emit()

    def _seek(self, value: int) -> None:
        if self._qt_player is not None:
            length = self._qt_player.duration()
            if length > 0 and self._qt_player.isSeekable():
                self._qt_player.setPosition(int(length * value / 1000))
            return
        if self._vlc_player is not None:
            self._vlc_player.set_position(value / 1000.0)

    def _tick(self) -> None:
        if self._qt_player is not None:
            now = self._qt_player.position() // 1000
            length = self._qt_player.duration() // 1000
            if length > 0 and self._qt_player.isSeekable():
                if not self.position.isSliderDown():
                    self.position.setValue(int(1000 * now / max(1, length)))
                self.info.setText(f"{now // 60}:{now % 60:02d} / "
                                  f"{length // 60}:{length % 60:02d}")
            else:
                # a live stream has no end to seek towards
                self.position.setEnabled(False)
                self.info.setText(f"LIVE  {now // 60}:{now % 60:02d}")
            return
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
