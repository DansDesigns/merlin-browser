"""<video> and <audio> in Merlin Engine.

A page's video is played by Qt Multimedia (its FFmpeg decodes H.264, AAC,
VP9, Opus and HLS playlists) on a thread of its own, and its pictures are
drawn into the page by paint.py, in the page's own order: what a site puts
over its video (its own buttons, captions) stays over it, and is clicked. A
widget over the page would have covered them.

  MediaWorker      the player, on its thread; only queued signals reach it,
                   so nothing on the interface thread ever waits on it
  MediaController  one per <video> or <audio>: its state, the picture to
                   draw, the controls drawn over it, and what they do
  FullScreenVideo  a window of its own, the whole screen, for full screen

The view (view.py) makes a controller for each <video> laid out, keeps it
across changes the page's scripts make (by the element's script number), and
tells the page's scripts what it does (js/host.js: HTMLMediaElement).
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QThread, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

BAR = 36.0                    # the controls' height
PLAYABLE = (".mp4", ".m4v", ".webm", ".ogv", ".ogg", ".mov", ".m3u8", ".mp3", ".m4a", ".aac",
            ".oga", ".opus", ".wav", ".flac", ".mpd")


class MediaWorker(QObject):
    """Qt's media player, on a thread of its own (as the player tab's: a stop
    can stick in Qt Multimedia, and must not hold up the window)."""

    image = pyqtSignal(object)                 # QImage, its own memory
    progress = pyqtSignal(int, int, bool)      # position ms, duration ms, seekable
    playing = pyqtSignal(bool)
    status = pyqtSignal(str)                   # loading, loaded, buffering, ended, invalid
    failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.want_frames = True
        self._player = self._audio = self._sink = self._timer = None
        self._loop = False
        self._peeking = False          # playing, silent, only for the first picture
        self._muted = False

    def _ensure(self) -> None:
        if self._player is not None:
            return
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._sink = QVideoSink(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoSink(self._sink)
        # converted where the frame is delivered, while it is certainly valid
        self._sink.videoFrameChanged.connect(self._frame, Qt.ConnectionType.DirectConnection)
        self._player.errorOccurred.connect(lambda _code, text: self.failed.emit(text or "cannot be played"))
        self._player.playbackStateChanged.connect(
            lambda state: self.playing.emit(state == QMediaPlayer.PlaybackState.PlayingState))
        self._player.mediaStatusChanged.connect(self._status)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._report)
        self._timer.start()

    def _status(self, status) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        if self._peeking and status in (QMediaPlayer.MediaStatus.LoadedMedia,
                                        QMediaPlayer.MediaStatus.BufferedMedia) \
                and self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            # its first picture, as browsers show before it is played: played
            # silently until a picture comes, then paused at the start
            self._audio.setMuted(True)
            self._player.play()

        names = {QMediaPlayer.MediaStatus.LoadingMedia: "loading",
                 QMediaPlayer.MediaStatus.LoadedMedia: "loaded",
                 QMediaPlayer.MediaStatus.BufferingMedia: "buffering",
                 QMediaPlayer.MediaStatus.StalledMedia: "buffering",
                 QMediaPlayer.MediaStatus.BufferedMedia: "loaded",
                 QMediaPlayer.MediaStatus.EndOfMedia: "ended",
                 QMediaPlayer.MediaStatus.InvalidMedia: "invalid"}
        name = names.get(status)
        if name:
            self.status.emit(name)
        if name == "ended" and self._loop:
            self._player.setPosition(0)
            self._player.play()

    def _frame(self, frame) -> None:
        if not self.want_frames or frame is None or not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            self.image.emit(image)
            if self._peeking:
                self._peeking = False
                # back on the worker's thread: the frame came on the decoder's
                from PyQt6.QtCore import QMetaObject

                QMetaObject.invokeMethod(self, "_peeked", Qt.ConnectionType.QueuedConnection)

    @pyqtSlot()
    def _peeked(self) -> None:
        if self._player is None:
            return
        self._player.pause()
        self._player.setPosition(0)
        self._audio.setMuted(self._muted)

    def _report(self) -> None:
        if self._player is not None:
            self.progress.emit(self._player.position(), self._player.duration(), self._player.isSeekable())

    @pyqtSlot(str, bool)
    def load(self, url: str, play: bool) -> None:
        self._ensure()
        self._peeking = not play
        self._player.setSource(QUrl(url))
        if play:
            self._player.play()

    @pyqtSlot()
    def play(self) -> None:
        if self._player is not None:
            if self._peeking:
                self._peeking = False
            self._audio.setMuted(self._muted)
            self._player.play()

    @pyqtSlot()
    def pause(self) -> None:
        if self._player is not None:
            self._player.pause()

    @pyqtSlot(int)
    def seek(self, milliseconds: int) -> None:
        if self._player is not None:
            self._player.setPosition(max(0, milliseconds))
            self._report()

    @pyqtSlot(float)
    def set_volume(self, level: float) -> None:
        self._ensure()
        self._audio.setVolume(max(0.0, min(1.0, level)))

    @pyqtSlot(bool)
    def set_muted(self, muted: bool) -> None:
        self._ensure()
        self._muted = bool(muted)
        self._audio.setMuted(bool(muted))

    @pyqtSlot(bool)
    def set_loop(self, loop: bool) -> None:
        self._loop = bool(loop)

    @pyqtSlot(float)
    def set_rate(self, rate: float) -> None:
        self._ensure()
        self._player.setPlaybackRate(max(0.0625, min(16.0, rate)))

    @pyqtSlot()
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
        # gone on this thread, where they were made, before it ends
        for part in (self._timer, self._sink, self._audio, self._player):
            if part is not None:
                part.deleteLater()
        self._player = self._audio = self._sink = self._timer = None
        self.deleteLater()
        QTimer.singleShot(0, self.thread().quit)


def _time(ms: int) -> str:
    seconds = max(0, int(ms // 1000))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _fitted(area: QRectF, width: float, height: float, fit: str) -> QRectF:
    """Where a picture of width by height goes in area: contain, as a
    video's picture is, unless the page says cover or fill."""
    if width <= 0 or height <= 0 or fit == "fill":
        return QRectF(area)
    scale_w, scale_h = area.width() / width, area.height() / height
    scale = max(scale_w, scale_h) if fit == "cover" else min(scale_w, scale_h)
    w, h = width * scale, height * scale
    return QRectF(area.center().x() - w / 2, area.center().y() - h / 2, w, h)


class MediaController(QObject):
    """One <video> or <audio>: its player, what to draw, its controls."""

    changed = pyqtSignal()                      # draw it again
    natural_size = pyqtSignal(int, int)         # its picture's size, when first known
    event = pyqtSignal(str)                     # play, pause, ended, timeupdate... for the page's scripts
    full_screen = pyqtSignal(bool)
    wants_source = pyqtSignal(bool)             # asked to play, with nothing it can play; by a person

    def __init__(self, audio_only: bool = False, parent=None):
        super().__init__(parent)
        self.audio_only = audio_only
        self.source = ""
        self.poster = None                      # QImage, from the page's pictures
        self.image = None
        self.playing = False
        self.started = False                    # played at least once
        self.ended = False
        self.position = 0
        self.duration = 0
        self.seekable = False
        self.volume = 1.0
        self.muted = False
        self.loop = False
        self.rate = 1.0
        self.status = ""                        # loading, loaded, buffering, ended, invalid
        self.error = ""
        self.note = ""                          # what is going on, said over the picture
        self.width = 0
        self.height = 0
        self.controls = False                   # the page asked for controls, or Merlin did
        self.hovered = False
        self.fit = "contain"
        self._loaded = ""                       # the source the player has
        self._want_play = False
        self._worker = None
        self._thread = None
        self._last_time_event = 0.0
        self.element = None
        self.poster_src = ""
        self.detached = False                   # not on the page: new Audio(), a site's player
        self.forced_controls = False            # Merlin's controls, whatever the page asked
        self.site_stream = ""                   # found by yt-dlp (siteplay.py)
        self.site_finding = False

    # ------------------------------------------------------------ the player
    def _start(self) -> None:
        if self._worker is not None:
            return
        self._thread = QThread()
        self._worker = MediaWorker()
        self._worker.moveToThread(self._thread)
        self._worker.image.connect(self._image)
        self._worker.progress.connect(self._progress)
        self._worker.playing.connect(self._playing)
        self._worker.status.connect(self._status)
        self._worker.failed.connect(self._failed)
        self._thread.start()
        self._call("set_volume", float(self.volume))
        self._call("set_muted", bool(self.muted))
        self._call("set_loop", bool(self.loop))

    def _call(self, name: str, *args) -> None:
        """A slot of the worker's, run on its thread, not waited for."""
        if self._worker is None:
            return
        from PyQt6.QtCore import Q_ARG, QMetaObject

        kinds = {str: "QString", bool: "bool", int: "int", float: "double"}
        QMetaObject.invokeMethod(self._worker, name, Qt.ConnectionType.QueuedConnection,
                                 *[Q_ARG(kinds[type(a)], a) for a in args])

    def set_source(self, url: str, preload: bool = True, autoplay: bool = False) -> None:
        """What to play. Fetched now (for its size and first picture) unless
        preload is none, played now if autoplay."""
        if url == self.source and (self._loaded == url or not preload):
            if autoplay and not self.playing:
                self.play()
            return
        self.source = url
        self.ended = False
        self.error = ""
        self.position = self.duration = 0
        self.image = None if url else self.image
        self._loaded = ""
        if url and (preload or autoplay or self._want_play):
            self._load(play=autoplay or self._want_play)
        self.changed.emit()

    def _load(self, play: bool) -> None:
        self._start()
        self._loaded = self.source
        self.status = "loading"
        self._want_play = play
        self._call("load", self.source, bool(play))
        self.event.emit("loadstart")

    def play(self, person: bool = False) -> None:
        if not self.source:
            self._want_play = True              # once there is one (a stream being found)
            self.changed.emit()
            self.wants_source.emit(person)
            return
        if self.ended:
            self.ended = False
            self._call("seek", 0)
        if self._loaded != self.source:
            self._load(play=True)
        else:
            self._call("play")
        self._want_play = True

    def pause(self) -> None:
        self._want_play = False
        self._call("pause")

    def toggle(self, person: bool = False) -> None:
        if self.playing or (self._want_play and not self.started and self.source):
            self.pause()
        else:
            self.play(person)

    def seek(self, seconds: float) -> None:
        self.position = int(max(0.0, seconds) * 1000)
        self._call("seek", self.position)
        self.event.emit("seeking")
        self.event.emit("seeked")
        self.event.emit("timeupdate")
        self.changed.emit()

    def set_volume(self, level: float) -> None:
        self.volume = max(0.0, min(1.0, float(level)))
        self._call("set_volume", float(self.volume))
        self.event.emit("volumechange")
        self.changed.emit()

    def set_muted(self, muted: bool) -> None:
        self.muted = bool(muted)
        self._call("set_muted", self.muted)
        self.event.emit("volumechange")
        self.changed.emit()

    def set_loop(self, loop: bool) -> None:
        self.loop = bool(loop)
        self._call("set_loop", self.loop)

    def set_rate(self, rate: float) -> None:
        try:
            self.rate = float(rate) or 1.0
        except (TypeError, ValueError):
            return
        self._call("set_rate", self.rate)
        self.event.emit("ratechange")

    def wants_to_play(self) -> bool:
        return self._want_play

    def shut_down(self) -> None:
        if self._worker is not None:
            self._call("shut_down")
            thread, self._thread = self._thread, None
            worker, self._worker = self._worker, None
            # left to finish in its own time: a stop that sticks holds up only it;
            # the worker deletes itself on its thread, the thread once it ends
            thread.finished.connect(thread.deleteLater)
            _RETIRED.append(thread)
            thread.finished.connect(lambda t=thread: _RETIRED.remove(t) if t in _RETIRED else None)

    # ------------------------------------------------------------ from the player
    def _image(self, image) -> None:
        if self.audio_only:
            return
        first = self.image is None or (image.width(), image.height()) != (self.width, self.height)
        self.image = image
        if first and image.width() > 0 and image.height() > 0:
            self.width, self.height = image.width(), image.height()
            self.natural_size.emit(self.width, self.height)
            self.event.emit("loadedmetadata")
            self.event.emit("loadeddata")
            self.event.emit("canplay")
        self.changed.emit()

    def _progress(self, position: int, duration: int, seekable: bool) -> None:
        moved = position != self.position
        new_length = duration != self.duration
        self.position, self.duration, self.seekable = position, duration, seekable
        if new_length:
            self.event.emit("durationchange")
            if self.audio_only:
                self.event.emit("loadedmetadata")
        if moved:
            now = time.monotonic()
            if now - self._last_time_event >= 0.24:
                self._last_time_event = now
                self.event.emit("timeupdate")
            self.changed.emit()

    def _playing(self, playing: bool) -> None:
        if playing and not self._want_play:
            return                              # the silent look for its first picture
        if playing == self.playing:
            return
        self.playing = playing
        if playing:
            self.started = True
            self.ended = False
            self.note = ""
            self.event.emit("play")
            self.event.emit("playing")
        else:
            self.event.emit("pause")
        self.changed.emit()

    def _status(self, status: str) -> None:
        self.status = status
        if status == "ended" and not self.loop:
            self.ended = True
            self.playing = False
            self._want_play = False
            self.event.emit("pause")
            self.event.emit("ended")
        elif status == "buffering":
            self.event.emit("waiting")
        elif status == "loaded" and self.audio_only:
            self.event.emit("canplay")
        self.changed.emit()

    def _failed(self, text: str) -> None:
        self.error = text
        self.playing = False
        self.event.emit("error")
        self.changed.emit()

    # ------------------------------------------------------------ drawn
    def shows_controls(self) -> bool:
        return self.controls or self.audio_only

    def draw(self, painter: QPainter, rect: QRectF) -> None:
        """The picture (or poster, or a black box), and the controls over it."""
        if rect.width() < 1 or rect.height() < 1:
            return
        painter.save()
        painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        if not self.audio_only:
            painter.fillRect(rect, QColor(0, 0, 0))
            picture = self.image if self.image is not None and self.started else None
            if picture is None and self.poster is not None and not self.poster.isNull():
                picture = self.poster
            elif picture is None and self.image is not None:
                picture = self.image                # the first picture, before playing
            if picture is not None:
                target = _fitted(rect, picture.width(), picture.height(), self.fit)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.drawImage(target, picture)
        else:
            painter.fillRect(rect, QColor(241, 243, 244))
        message = self.error and "This video cannot be played here" or self.note
        if message and not self.audio_only:
            self._say(painter, rect, message)
        elif not self.audio_only and not self.playing and (not self.started or self.ended) \
                and (self.shows_controls() or self.source == "" or self._want_play):
            self._big_play(painter, rect)
        if self.shows_controls() and (self.audio_only or self.hovered or not self.playing):
            self._bar(painter, rect)
        painter.restore()

    def _say(self, painter, rect, text) -> None:
        painter.fillRect(rect, QColor(0, 0, 0, 150))
        font = QFont(painter.font())
        font.setPixelSize(max(12, min(18, int(rect.height() / 14))))
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(rect.adjusted(12, 12, -12, -12), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)

    def _big_play(self, painter, rect) -> None:
        size = max(36.0, min(68.0, min(rect.width(), rect.height()) / 4))
        centre = rect.center()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawEllipse(centre, size / 2, size / 2)
        triangle = QPainterPath()
        triangle.moveTo(centre.x() - size * 0.14, centre.y() - size * 0.2)
        triangle.lineTo(centre.x() + size * 0.22, centre.y())
        triangle.lineTo(centre.x() - size * 0.14, centre.y() + size * 0.2)
        triangle.closeSubpath()
        painter.setBrush(QColor(255, 255, 255))
        painter.drawPath(triangle)

    def _parts(self, rect: QRectF) -> dict:
        """Where each control is."""
        bar = QRectF(rect.left(), rect.bottom() - BAR, rect.width(), BAR) if not self.audio_only \
            else QRectF(rect.left(), rect.center().y() - BAR / 2, rect.width(), BAR)
        y = bar.top()
        play = QRectF(bar.left() + 4, y, 32, BAR)
        full = QRectF(bar.right() - 36, y, 32, BAR) if not self.audio_only else QRectF()
        mute = QRectF((full.left() if not self.audio_only else bar.right() - 4) - 32, y, 32, BAR)
        time_room = 92.0 if self.duration >= 3600_000 else 78.0
        clock = QRectF(play.right() + 2, y, time_room, BAR)
        track = QRectF(clock.right() + 6, y + BAR / 2 - 3, max(0.0, mute.left() - clock.right() - 14), 6)
        return {"bar": bar, "play": play, "time": clock, "track": track, "mute": mute, "full": full}

    def _bar(self, painter, rect) -> None:
        parts = self._parts(rect)
        bar = parts["bar"]
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self.audio_only:
            shade = QLinearGradient(bar.topLeft(), bar.bottomLeft())
            shade.setColorAt(0, QColor(0, 0, 0, 0))
            shade.setColorAt(1, QColor(0, 0, 0, 170))
            painter.fillRect(bar.adjusted(0, -12, 0, 0), shade)
        ink = QColor(255, 255, 255) if not self.audio_only else QColor(32, 33, 36)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        play = parts["play"]
        c = play.center()
        if self.playing:
            painter.drawRect(QRectF(c.x() - 6, c.y() - 7, 4, 14))
            painter.drawRect(QRectF(c.x() + 2, c.y() - 7, 4, 14))
        else:
            triangle = QPainterPath()
            triangle.moveTo(c.x() - 5, c.y() - 8)
            triangle.lineTo(c.x() + 8, c.y())
            triangle.lineTo(c.x() - 5, c.y() + 8)
            triangle.closeSubpath()
            painter.drawPath(triangle)
        font = QFont(painter.font())
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(ink)
        shown = _time(self.position) + (" / " + _time(self.duration) if self.duration > 0 else "")
        painter.drawText(parts["time"], Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, shown)
        track = parts["track"]
        if track.width() > 8:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 90) if not self.audio_only else QColor(0, 0, 0, 50))
            painter.drawRoundedRect(track, 3, 3)
            if self.duration > 0:
                done = QRectF(track.left(), track.top(), track.width() * min(1.0, self.position / self.duration),
                              track.height())
                painter.setBrush(QColor(255, 0, 0) if not self.audio_only else QColor(26, 115, 232))
                painter.drawRoundedRect(done, 3, 3)
        mute = parts["mute"].center()
        painter.setBrush(ink)
        speaker = QPainterPath()
        speaker.moveTo(mute.x() - 8, mute.y() - 3)
        speaker.lineTo(mute.x() - 4, mute.y() - 3)
        speaker.lineTo(mute.x() + 1, mute.y() - 8)
        speaker.lineTo(mute.x() + 1, mute.y() + 8)
        speaker.lineTo(mute.x() - 4, mute.y() + 3)
        speaker.lineTo(mute.x() - 8, mute.y() + 3)
        speaker.closeSubpath()
        painter.drawPath(speaker)
        painter.setPen(QPen(ink, 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.muted or self.volume == 0:
            painter.drawLine(QPointF(mute.x() + 4, mute.y() - 4), QPointF(mute.x() + 10, mute.y() + 4))
            painter.drawLine(QPointF(mute.x() + 10, mute.y() - 4), QPointF(mute.x() + 4, mute.y() + 4))
        else:
            painter.drawArc(QRectF(mute.x() - 2, mute.y() - 6, 10, 12), -60 * 16, 120 * 16)
        if not self.audio_only:
            f = parts["full"].center()
            for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                corner = QPointF(f.x() + dx * 7, f.y() + dy * 6)
                painter.drawLine(corner, QPointF(corner.x() - dx * 4, corner.y()))
                painter.drawLine(corner, QPointF(corner.x(), corner.y() - dy * 4))

    def hit(self, rect: QRectF, point: QPointF) -> str:
        """What a click at point does: play, seek, mute, full, or video (the
        picture itself: play or pause), or "" for nothing of the video's."""
        if not rect.contains(point):
            return ""
        if self.shows_controls():
            parts = self._parts(rect)
            if parts["bar"].contains(point):
                for name in ("play", "mute", "full"):
                    if parts[name].contains(point):
                        return name
                track = parts["track"].adjusted(0, -10, 0, 10)
                if track.contains(point) and parts["track"].width() > 0:
                    return f"seek:{(point.x() - track.left()) / track.width():.4f}"
                return "bar"
            return "video"
        if not self.started and (self.source == "" or self._want_play):
            return "video"                       # Merlin's own play button, shown
        return ""

    def act(self, what: str) -> None:
        """What a person's click on it does."""
        if what in ("play", "video"):
            self.toggle(person=True)
        elif what == "mute":
            self.set_muted(not self.muted)
        elif what == "full":
            self.full_screen.emit(True)
        elif what.startswith("seek:") and self.duration > 0:
            self.seek(float(what[5:]) * self.duration / 1000.0)

    def state(self) -> dict:
        """What the page's scripts see of it."""
        ready = 0
        if self.duration > 0 or self.width:
            ready = 1
        if self.image is not None or self.status in ("loaded", "buffered") or self.playing:
            ready = 4
        return {"paused": not self.playing and not (self._want_play and self.status in ("loading", "buffering")),
                "ended": self.ended, "currentTime": self.position / 1000.0,
                "duration": (self.duration / 1000.0) if self.duration > 0 else None,
                "volume": self.volume, "muted": self.muted, "videoWidth": self.width,
                "videoHeight": self.height, "readyState": ready, "currentSrc": self._loaded or self.source,
                "error": self.error, "playbackRate": self.rate, "seekable": self.seekable}


_RETIRED: list = []


class FullScreenVideo(QWidget):
    """A video on the whole screen: its picture and controls, Escape or a
    double click to leave."""

    def __init__(self, controller: MediaController):
        super().__init__(None, Qt.WindowType.Window)
        self.controller = controller
        self.setWindowTitle("Video")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        controller.changed.connect(self.update)
        self._hide_cursor = QTimer(self)
        self._hide_cursor.setSingleShot(True)
        self._hide_cursor.setInterval(2500)
        self._hide_cursor.timeout.connect(lambda: self.setCursor(Qt.CursorShape.BlankCursor))
        self._was_controls = controller.controls

    def paintEvent(self, event) -> None:              # noqa: N802
        painter = QPainter(self)
        try:
            self.controller.controls = True
            self.controller.draw(painter, QRectF(self.rect()))
        finally:
            self.controller.controls = self._was_controls
            painter.end()

    def mouseMoveEvent(self, event) -> None:          # noqa: N802
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._hide_cursor.start()
        if not self.controller.hovered:
            self.controller.hovered = True
            self.update()

    def mouseReleaseEvent(self, event) -> None:       # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.controller.controls = True
        try:
            what = self.controller.hit(QRectF(self.rect()), event.position())
        finally:
            self.controller.controls = self._was_controls
        if what == "full":
            self.close()
        elif what:
            self.controller.act(what)

    def mouseDoubleClickEvent(self, event) -> None:   # noqa: N802
        self.close()

    def keyPressEvent(self, event) -> None:           # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_F):
            self.close()
        elif key in (Qt.Key.Key_Space, Qt.Key.Key_K):
            self.controller.toggle()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_L):
            self.controller.seek(self.controller.position / 1000 + (5 if key == Qt.Key.Key_Right else 10))
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_J):
            self.controller.seek(self.controller.position / 1000 - (5 if key == Qt.Key.Key_Left else 10))
        elif key == Qt.Key.Key_M:
            self.controller.set_muted(not self.controller.muted)

    def closeEvent(self, event) -> None:              # noqa: N802
        try:
            self.controller.changed.disconnect(self.update)
        except TypeError:
            pass
        self.controller.full_screen.emit(False)
        super().closeEvent(event)


def media_source(element, base_url: str) -> str:
    """The address a <video> or <audio> plays: its src, or the first of its
    <source>s it can play. A blob: address (a page's own streaming, Media
    Source Extensions) is not one Merlin can play yet: ""."""
    from PyQt6.QtCore import QUrl as _Url

    def resolved(value: str) -> str:
        value = (value or "").strip()
        if not value or value.startswith(("blob:", "mediasource:")):
            return ""
        return _Url(base_url).resolved(_Url(value)).toString() if base_url else value

    found = resolved(element.attrs.get("src", ""))
    if found:
        return found
    for child in element.children:
        if getattr(child, "tag", "") != "source":
            continue
        kind = (child.attrs.get("type") or "").lower()
        if kind and not can_play(kind):
            continue
        found = resolved(child.attrs.get("src", ""))
        if found:
            return found
    return ""


def can_play(kind: str) -> str:
    """canPlayType: "probably", "maybe" or "", for what Qt Multimedia's
    FFmpeg plays."""
    kind = (kind or "").strip().lower()
    if not kind:
        return ""
    base = kind.split(";")[0].strip()
    good = {"video/mp4", "video/webm", "video/ogg", "audio/mp4", "audio/mpeg", "audio/mp3", "audio/webm",
            "audio/ogg", "audio/wav", "audio/wave", "audio/x-wav", "audio/flac", "audio/aac",
            "video/quicktime", "application/vnd.apple.mpegurl", "application/x-mpegurl",
            "audio/mpegurl", "video/x-m4v", "audio/x-m4a", "audio/opus"}
    if base not in good:
        return ""
    if "codecs" in kind:
        codecs = kind.split("codecs", 1)[1].lower()
        known = ("avc1", "avc3", "mp4a", "vp8", "vp9", "vp09", "opus", "vorbis", "theora", "flac",
                 "mp3", "hev1", "hvc1", "av01")
        names = [c.strip(" =\"'") for c in codecs.split(",")]
        if names and all(any(n.startswith(k) for k in known) for n in names if n):
            return "probably"
        return ""
    return "maybe"


class MediaMixin:
    """MerlinView's videos: a controller for each <video> and <audio> laid
    out, kept by the element's script number (so a page whose scripts change
    it keeps playing), drawn by paint.py, clicked here, told to the page's
    scripts."""

    def _media_init(self) -> None:
        self._players = {}               # media_key -> MediaController
        self._media_places = {}        # media_key -> (rect in page terms, fixed, element)
        self._media_sizes = {}         # media_key -> (width, height), for layout
        self._media_full = None        # the full-screen window, if one is open
        site_init = getattr(self, "_site_init", None)
        if site_init is not None:
            site_init()                # siteplay.py

    def _clear_media(self) -> None:
        for controller in list(getattr(self, "_players", {}).values()):
            controller.shut_down()
            controller.deleteLater()
        full = getattr(self, "_media_full", None)
        if full is not None:
            try:
                full.close()
            except RuntimeError:
                pass
        self._media_init()

    def _paint_site_media(self, painter, visible) -> None:
        """A video Merlin plays in a site's player (YouTube's), over the
        player's box: drawn on top, as the player's own would be."""
        for key, (rect, fixed, _element) in getattr(self, "_media_site_places", {}).items():
            controller = self._players.get(key)
            if controller is None:
                continue
            shown = rect if not fixed else rect.translated(0, self._scroll)
            if shown.intersects(visible):
                controller.draw(painter, shown)

    def _media_picture_arrived(self, src: str) -> None:
        """A picture fetched: the poster of any video waiting for it."""
        picture = self._images.get(src)
        if picture is None or picture is False or not hasattr(picture, "isNull") or picture.isNull():
            return
        for key, controller in self._players.items():
            if getattr(controller, "poster_src", "") == src:
                controller.poster = picture
                self._media_changed(key)

    def _media_registry(self) -> dict:
        return {key: (controller, self._media_places.get(key)) for key, controller in self._players.items()}

    # ------------------------------------------------------------ made and kept
    def _new_player(self, key, element, audio_only: bool) -> "MediaController":
        controller = MediaController(audio_only=audio_only, parent=self)
        controller.changed.connect(lambda k=key: self._media_changed(k))
        controller.natural_size.connect(lambda w, h, k=key: self._media_natural_size(k, w, h))
        controller.event.connect(lambda name, k=key: self._media_event(k, name))
        controller.full_screen.connect(lambda on, k=key: self._media_full_screen(k, on))
        controller.wants_source.connect(lambda person, k=key: self._media_unplayable(k, self._players.get(k), person))
        controller.element = element
        controller.detached = False
        self._players[key] = controller
        return controller

    def _sync_media(self, laid_out: dict) -> None:
        """laid_out: media_key -> (rect, fixed, element), each laid out now."""
        from .layout import media_key  # noqa: F401  (the keys come from layout)

        for key in [k for k, c in self._players.items()
                    if k not in laid_out and not k.startswith("site:") and not c.detached]:
            controller = self._players.pop(key)
            controller.shut_down()
            controller.deleteLater()
            self._media_sizes.pop(key, None)
        page = self._url.toString()
        for key, (rect, fixed, element) in laid_out.items():
            controller = self._players.get(key)
            if controller is None:
                controller = self._new_player(key, element, element.tag == "audio")
                controller.muted = "muted" in element.attrs
                controller.set_volume(controller.volume)
                if controller.muted:
                    controller.set_muted(True)
            controller.element = element
            controller.detached = False
            controller.controls = "controls" in element.attrs or getattr(controller, "forced_controls", False)
            controller.set_loop("loop" in element.attrs)
            style = (self._styles or {}).get(element, {})
            fit = str(style.get("object-fit") or "contain").lower()
            controller.fit = fit if fit in ("contain", "cover", "fill") else "contain"
            poster = (element.attrs.get("poster") or "").strip()
            controller.poster_src = poster
            if poster:
                picture = self._images.get(poster)
                if picture is None and ("__pending__" + poster) not in self._images:
                    self._want_image(poster)
                elif picture is not None and picture is not False and hasattr(picture, "isNull"):
                    controller.poster = picture
            if not getattr(controller, "site_stream", ""):
                source = media_source(element, page)
                preload = (element.attrs.get("preload") or "metadata").strip().lower() != "none"
                # sound starts only when asked for, or with none: browsers allow
                # a video to play by itself only muted
                autoplay = "autoplay" in element.attrs and controller.muted
                controller.set_source(source, preload=preload, autoplay=autoplay)
        site_sync = getattr(self, "_site_sync", None)
        if site_sync is not None:
            site_sync()                # siteplay.py: YouTube's video, over its player
        self._media_places = {k: v for k, v in laid_out.items()}
        for key, place in getattr(self, "_media_site_places", {}).items():
            self._media_places[key] = place

    # ------------------------------------------------------------ from the player
    def _media_view_rects(self, key) -> list:
        place = self._media_places.get(key)
        if place is None:
            return []
        rect, fixed, _element = place
        top = rect.top() - (0.0 if fixed else self._scroll)
        return [QRectF(rect.left(), top, rect.width(), rect.height()).toAlignedRect().adjusted(-1, -1, 1, 1)]

    def _media_changed(self, key) -> None:
        for rect in self._media_view_rects(key):
            self.update(rect)

    def _media_natural_size(self, key, width: int, height: int) -> None:
        if self._media_sizes.get(key) == (width, height):
            return
        self._media_sizes[key] = (width, height)
        controller = self._players.get(key)
        element = getattr(controller, "element", None)
        style = (self._styles or {}).get(element, {}) if element is not None else {}
        # laid out again only if its size depends on its picture's
        if element is not None and (style.get("height") in (None, "auto") or style.get("width") in (None, "auto")):
            self._layout()

    def _media_event(self, key, name: str) -> None:
        controller = self._players.get(key)
        element = getattr(controller, "element", None)
        number = element.attrs.get("data-mjs") if element is not None else key
        if self._script is None or controller is None or not str(number or "").isdigit():
            return
        target = int(number)
        self._script.send({"type": "media_event", "target": target, "event": name, "state": controller.state()})

    def _media_full_screen(self, key, on: bool) -> None:
        controller = self._players.get(key)
        if controller is None:
            return
        if on and self._media_full is None:
            window = FullScreenVideo(controller)
            self._media_full = window
            window.destroyed.connect(lambda *_a: setattr(self, "_media_full", None))
            window.showFullScreen()
            window.activateWindow()
            window.setFocus()
            self._media_event(key, "fullscreenchange")
        elif not on:
            self._media_full = None
            self._media_event(key, "fullscreenchange")

    # ------------------------------------------------------------ the pointer
    def _media_under(self, position):
        """The video the pointer is over, if what is on top there is it:
        (key, controller, rect in the window)."""
        point = position.toPointF() if hasattr(position, "toPointF") else position
        top = None
        try:
            found = self._elements_at(point)
            top = found[0] if found else None
        except Exception:                                  # noqa: BLE001
            top = None
        for key, (rect, fixed, element) in reversed(list(self._media_places.items())):
            shown = QRectF(rect.left(), rect.top() - (0.0 if fixed else self._scroll), rect.width(), rect.height())
            if not shown.contains(point):
                continue
            site = key.startswith("site:")
            if site or top is None or top is element or _holds(element, top):
                controller = self._players.get(key)
                if controller is not None:
                    return key, controller, shown
        return None

    def _media_hover(self, position) -> None:
        under = self._media_under(position) if self._players else None
        for key, controller in self._players.items():
            on = under is not None and under[0] == key
            if controller.hovered != on:
                controller.hovered = on
                self._media_changed(key)

    def _media_click(self, position) -> bool:
        """A click on a video's controls (or Merlin's play button): done here,
        and not passed to the page."""
        under = self._media_under(position) if self._players else None
        if under is None:
            return False
        key, controller, rect = under
        point = position.toPointF() if hasattr(position, "toPointF") else position
        what = controller.hit(rect, point)
        if not what:
            return False
        controller.act(what)
        return True

    def _media_double_click(self, position) -> bool:
        under = self._media_under(position) if self._players else None
        if under is None or not under[1].shows_controls() or under[1].audio_only:
            return False
        self._media_full_screen(under[0], True)
        return True

    # ------------------------------------------------------------ from the page's scripts
    def _media_from_script(self, message: dict) -> None:
        number = str(message.get("target"))
        key = number
        controller = self._players.get(key)
        element = self._by_script_id.get(number) if hasattr(self, "_by_script_id") else None
        tag = element.tag if element is not None else str(message.get("tag") or "")
        if controller is None and tag in ("video", "audio"):
            # a video or sound a script made and never put in the page (new
            # Audio()), or one that is hidden: played all the same, as in a
            # browser, by its address as the script gives it
            controller = self._new_player(key, element, tag == "audio")
            controller.detached = True
            if element is not None:
                controller.muted = "muted" in element.attrs
            given = str(message.get("src") or "")
            if given and message.get("action") != "src":
                controller.set_source(given, preload=True)
        if controller is None:
            return
        action = message.get("action")
        value = message.get("value")
        if action == "src":
            controller.site_stream = ""
            source = str(value or "")
            if source and not source.startswith(("blob:", "mediasource:")):
                source = self._url.resolved(QUrl(source)).toString()
            elif source.startswith(("blob:", "mediasource:")):
                source = ""
            controller.set_source(source, preload=True, autoplay=False)
        elif action == "play":
            controller.play()
        elif action == "pause":
            controller.pause()
        elif action == "seek":
            try:
                controller.seek(float(value))
            except (TypeError, ValueError):
                pass
        elif action == "volume":
            try:
                controller.set_volume(float(value))
            except (TypeError, ValueError):
                pass
        elif action == "muted":
            controller.set_muted(bool(value))
        elif action == "loop":
            controller.set_loop(bool(value))
        elif action == "rate":
            controller.set_rate(value)
        elif action == "fullscreen":
            self._media_full_screen(key, bool(value))
        elif action == "load":
            source = controller.source
            controller.source = ""
            controller.set_source(source, preload=True, autoplay=False)
        self._media_event(key, "statechange")

    def _media_unplayable(self, key, controller, person: bool = False) -> None:
        """A video the page streams itself (Media Source Extensions), which
        Merlin cannot play yet: its page's own stream, found by yt-dlp, where
        yt-dlp knows the site (siteplay.py)."""
        finder = getattr(self, "_site_stream_for", None)
        if finder is not None:
            finder(key, controller, person)


def _holds(outer, inner) -> bool:
    node = getattr(inner, "parent", None)
    while node is not None:
        if node is outer:
            return True
        node = getattr(node, "parent", None)
    return False
