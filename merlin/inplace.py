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

from PyQt6.QtCore import QPoint, QRect, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPen
from PyQt6.QtWidgets import QWidget

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


class PlayerControls(QWidget):
    """Controls over the picture, in the manner of YouTube's.

    A gradient along the foot, a thin red progress bar that thickens under the
    pointer, and a row of controls: play and pause, volume with mute, the time
    or a live badge, then close and fullscreen at the right. They fade out when
    the pointer rests over a playing video and come back when it moves. Drawn
    here rather than taken from YouTube, whose own icons and code are Google's.
    """

    toggle = pyqtSignal()
    muted = pyqtSignal(bool)
    volume = pyqtSignal(float)
    seek_to = pyqtSignal(int)          # per mille
    seek_by = pyqtSignal(int)          # milliseconds
    fullscreen = pyqtSignal()
    close_player = pyqtSignal()

    BAR = 48                           # height of the control row
    RED = QColor("#ff0033")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.playing = False
        self.is_muted = False
        self.level = 1.0
        self.position = 0
        self.duration = 0
        self.seekable = False
        self.full = False
        self.message = "Connecting..."
        self.sticky = False
        self._shown = True
        self._hover = ""
        self._dragging_volume = False
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.setInterval(2500)
        self._hide.timeout.connect(self._fade)

    # ---------------------------------------------------------------- state
    @property
    def live(self) -> bool:
        return not (self.duration > 0 and self.seekable)

    def set_progress(self, position: int, duration: int, seekable: bool) -> None:
        self.position, self.duration, self.seekable = position, duration, seekable
        # The clock ticks whether or not anything plays, and wiped an error
        # the moment it was shown: a stream that failed at once read "LIVE".
        # A message that matters stays until the picture arrives.
        if not self.sticky:
            self.message = ""
        self.update()

    def picture_arrived(self) -> None:
        self.sticky = False
        self.message = ""
        self.update()

    def set_playing(self, playing: bool) -> None:
        self.playing = playing
        if playing:
            self._hide.start()
        else:
            self._reveal(hold=True)
        self.update()

    def say(self, text: str, sticky: bool = True) -> None:
        self.message = text
        self.sticky = sticky
        self._reveal(hold=True)

    def _reveal(self, hold: bool = False) -> None:
        self._shown = True
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._hide.stop()
        if self.playing and not hold:
            self._hide.start()
        self.update()

    def _fade(self) -> None:
        if self.playing and not self._dragging_volume:
            self._shown = False
            self.setCursor(Qt.CursorShape.BlankCursor)
            self.update()

    # --------------------------------------------------------------- layout
    def _areas(self) -> dict:
        w, h = self.width(), self.height()
        row = h - self.BAR
        areas = {
            "progress": QRect(12, row - 10, w - 24, 14),
            "play": QRect(8, row, 44, self.BAR),
            "mute": QRect(52, row, 40, self.BAR),
            "fullscreen": QRect(w - 52, row, 44, self.BAR),
            "close": QRect(w - 96, row, 44, self.BAR),
        }
        if self._hover in ("mute", "volume") or self._dragging_volume:
            areas["volume"] = QRect(94, row, 64, self.BAR)
        return areas

    def _which(self, point) -> str:
        if not self._shown:
            return ""
        for name, area in self._areas().items():
            if area.contains(point):
                return name
        return ""

    # ---------------------------------------------------------------- input
    def mouseMoveEvent(self, event) -> None:                 # noqa: N802
        point = event.position().toPoint()
        if self._dragging_volume:
            self._volume_at(point.x())
        hover = self._which(point)
        if hover != self._hover:
            self._hover = hover
            self.update()
        self._reveal()

    def leaveEvent(self, event) -> None:                     # noqa: N802
        self._hover = ""
        if self.playing:
            self._hide.start(600)
        self.update()

    def mousePressEvent(self, event) -> None:                # noqa: N802
        self.setFocus()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position().toPoint()
        where = self._which(point)
        self._reveal()
        if where == "play":
            self.toggle.emit()
        elif where == "mute":
            self.is_muted = not self.is_muted
            self.muted.emit(self.is_muted)
        elif where == "volume":
            self._dragging_volume = True
            self._volume_at(point.x())
        elif where == "progress":
            if not self.live:
                area = self._areas()["progress"]
                self.seek_to.emit(int(1000 * (point.x() - area.left())
                                      / max(1, area.width())))
        elif where == "fullscreen":
            self.fullscreen.emit()
        elif where == "close":
            self.close_player.emit()
        else:
            self.toggle.emit()
        self.update()

    def mouseReleaseEvent(self, event) -> None:              # noqa: N802
        self._dragging_volume = False

    def mouseDoubleClickEvent(self, event) -> None:          # noqa: N802
        if not self._which(event.position().toPoint()):
            self.fullscreen.emit()

    def keyPressEvent(self, event) -> None:                  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Space, Qt.Key.Key_K):
            self.toggle.emit()
        elif key == Qt.Key.Key_M:
            self.is_muted = not self.is_muted
            self.muted.emit(self.is_muted)
        elif key == Qt.Key.Key_F:
            self.fullscreen.emit()
        elif key == Qt.Key.Key_Escape and self.full:
            self.fullscreen.emit()
        elif key == Qt.Key.Key_Left:
            self.seek_by.emit(-5000)
        elif key == Qt.Key.Key_Right:
            self.seek_by.emit(5000)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = 0.05 if key == Qt.Key.Key_Up else -0.05
            self.level = max(0.0, min(1.0, self.level + step))
            self.is_muted = False
            self.volume.emit(self.level)
            self.muted.emit(False)
        else:
            super().keyPressEvent(event)
            return
        self._reveal()
        self.update()

    def _volume_at(self, x: int) -> None:
        area = QRect(98, 0, 56, 1)
        self.level = max(0.0, min(1.0, (x - area.left()) / area.width()))
        self.is_muted = self.level == 0.0
        self.volume.emit(self.level)
        self.muted.emit(self.is_muted)
        self.update()

    # -------------------------------------------------------------- drawing
    def paintEvent(self, event) -> None:                     # noqa: N802
        if not self._shown:
            return
        from PyQt6.QtGui import QLinearGradient, QPainter, QPainterPath

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        row = h - self.BAR
        white = QColor("#ffffff")

        shade = QLinearGradient(0, h - 150, 0, h)
        shade.setColorAt(0.0, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, 170))
        painter.fillRect(0, h - 150, w, 150, shade)

        # the progress bar: full and red when live, like YouTube's
        areas = self._areas()
        track = areas["progress"]
        thick = 5 if self._hover == "progress" else 3
        line = QRect(track.left(), track.center().y() - thick // 2, track.width(), thick)
        painter.fillRect(line, QColor(255, 255, 255, 60))
        if self.live:
            played = line.width()
        else:
            played = int(line.width() * self.position / max(1, self.duration))
        painter.fillRect(QRect(line.left(), line.top(), played, thick), self.RED)
        if not self.live and self._hover == "progress":
            painter.setBrush(self.RED)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPoint(line.left() + played, line.center().y()), 6, 6)

        def glow(name):
            if self._hover == name:
                painter.setBrush(QColor(255, 255, 255, 30))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(areas[name].adjusted(4, 6, -4, -6), 8, 8)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(white)

        # play or pause
        glow("play")
        painter.setBrush(white)
        cx, cy = areas["play"].center().x(), row + self.BAR // 2
        if self.playing:
            painter.drawRoundedRect(QRect(cx - 7, cy - 9, 5, 18), 1, 1)
            painter.drawRoundedRect(QRect(cx + 2, cy - 9, 5, 18), 1, 1)
        else:
            path = QPainterPath()
            path.moveTo(cx - 6, cy - 10)
            path.lineTo(cx + 9, cy)
            path.lineTo(cx - 6, cy + 10)
            path.closeSubpath()
            painter.drawPath(path)

        # volume: a speaker, with waves for the level or a cross when muted
        glow("mute")
        painter.setBrush(white)
        vx, vy = areas["mute"].center().x() - 6, cy
        speaker = QPainterPath()
        speaker.moveTo(vx - 6, vy - 4)
        speaker.lineTo(vx - 2, vy - 4)
        speaker.lineTo(vx + 3, vy - 9)
        speaker.lineTo(vx + 3, vy + 9)
        speaker.lineTo(vx - 2, vy + 4)
        speaker.lineTo(vx - 6, vy + 4)
        speaker.closeSubpath()
        painter.drawPath(speaker)
        pen = QPen(white, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.is_muted or self.level == 0.0:
            painter.drawLine(vx + 7, vy - 4, vx + 14, vy + 4)
            painter.drawLine(vx + 14, vy - 4, vx + 7, vy + 4)
        else:
            painter.drawArc(QRect(vx, vy - 5, 10, 10), -50 * 16, 100 * 16)
            if self.level > 0.5:
                painter.drawArc(QRect(vx - 2, vy - 9, 18, 18), -50 * 16, 100 * 16)

        text_left = areas["mute"].right() + 8
        if "volume" in areas:
            slider = QRect(98, cy - 2, 56, 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.fillRect(slider, QColor(255, 255, 255, 80))
            level = 0.0 if self.is_muted else self.level
            painter.fillRect(QRect(slider.left(), slider.top(),
                                   int(slider.width() * level), 4), white)
            painter.setBrush(white)
            painter.drawEllipse(QPoint(slider.left() + int(slider.width() * level),
                                       cy), 6, 6)
            text_left = areas["volume"].right() + 8

        # the time, or a live badge
        font = painter.font()
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        text_rect = QRect(text_left, row, max(0, w - text_left - 110), self.BAR)
        align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        painter.setPen(white)
        if self.message:
            painter.drawText(text_rect, align, self.message)
        elif self.live:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.RED)
            painter.drawEllipse(QPoint(text_left + 5, cy), 4, 4)
            painter.setPen(white)
            painter.drawText(text_rect.adjusted(16, 0, 0, 0), align, "LIVE")
        else:
            def clock(ms):
                seconds = ms // 1000
                hours, rest = divmod(seconds, 3600)
                minutes, seconds = divmod(rest, 60)
                return (f"{hours}:{minutes:02d}:{seconds:02d}" if hours
                        else f"{minutes}:{seconds:02d}")
            painter.drawText(text_rect, align,
                             f"{clock(self.position)} / {clock(self.duration)}")

        # close, then fullscreen, at the right
        glow("close")
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        kx, ky = areas["close"].center().x(), cy
        painter.drawLine(kx - 6, ky - 6, kx + 6, ky + 6)
        painter.drawLine(kx + 6, ky - 6, kx - 6, ky + 6)

        glow("fullscreen")
        painter.setPen(pen)
        fx, fy = areas["fullscreen"].center().x(), cy
        d, c = 9, 4                                  # half size, corner length
        inward = -1 if self.full else 1
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x, y = fx + sx * d, fy + sy * d
            if self.full:
                x, y = fx + sx * (d - c), fy + sy * (d - c)
            painter.drawLine(x, y, x - sx * c * inward, y)
            painter.drawLine(x, y, x, y - sy * c * inward)
        painter.end()


class InPagePlayer(QWidget):
    """Merlin's player over the page's own video area."""

    STALL_MS = 12000

    closed = pyqtSignal()
    # no picture arrived in time: the stream was accepted but is not playing
    stalled = pyqtSignal()
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

        # YouTube-style controls, laid over the picture
        self.controls = PlayerControls(self)
        self.controls.toggle.connect(self._ask_toggle.emit)
        self.controls.fullscreen.connect(self.toggle_fullscreen)
        self.controls.close_player.connect(self.stop)

        self._full_screen = False

        # no parent: the thread must be able to outlive this widget
        self._thread = QThread()
        self._worker = PlaybackWorker(stream)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._worker.image.connect(self._first_picture)
        self._worker.image.connect(self.picture.show_image)
        self._worker.playing.connect(self._playing)
        self.controls.muted.connect(self._worker.set_muted)
        self.controls.volume.connect(self._worker.set_volume)
        self.controls.seek_to.connect(self._worker.seek)
        self.controls.seek_by.connect(self._worker.seek_by)
        self._worker.progress.connect(self._progress)
        self._worker.failed.connect(self._failed)
        self._ask_toggle.connect(self._worker.toggle)
        self._ask_shut_down.connect(self._worker.shut_down)
        self._had_picture = False
        self._gave_up = False

        self.hide()
        self._place_timer = QTimer(self)
        self._place_timer.setInterval(150)
        self._place_timer.timeout.connect(self._ask_where)
        self._place_timer.start()
        self._ask_where()
        self._thread.start()
        self._note("in-page player: starting")

        # A stream YouTube accepts at the top but refuses further down gives
        # the player nothing to show, and it waits quietly for ever: a black
        # picture and a Play button that does nothing. Given no picture in
        # this long, it says so, and the window tries another way.
        self._stall = QTimer(self)
        self._stall.setSingleShot(True)
        self._stall.setInterval(self.STALL_MS)
        self._stall.timeout.connect(self._no_picture)
        self._stall.start()

    # ------------------------------------------------------------ placement
    def _ask_where(self) -> None:
        if self._stopped:
            return
        try:
            self.view.page().runJavaScript(PLACE_JS, self._place)
        except RuntimeError:
            self.stop()                                  # the view has gone

    def _place(self, where) -> None:
        if self._full_screen:
            return
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
        self.controls.set_playing(playing)

    def _no_picture(self) -> None:
        if self._stopped or self._had_picture:
            return
        self._note("in-page player: no picture after "
                   f"{self.STALL_MS // 1000}s, the stream is not playing")
        self.controls.say("This stream isn't sending a picture...")
        self._give_up()

    def _give_up(self) -> None:
        """Tell the window once, whichever of failure or stall came first."""
        if self._gave_up or self._stopped:
            return
        self._gave_up = True
        self.stalled.emit()

    def _first_picture(self, _image) -> None:
        if not self._had_picture:
            self._stall.stop()
            self._had_picture = True
            self.controls.picture_arrived()
            self._note("in-page player: playing")
            self._worker.image.disconnect(self._first_picture)

    def _progress(self, position: int, duration: int, seekable: bool) -> None:
        self.controls.set_progress(position, duration, seekable)

    def _failed(self, text: str) -> None:
        self.controls.say(f"Merlin's player could not play this: {text}")
        self._note(f"in-page player: could not play: {text}")
        if not self._had_picture:
            # it has said it cannot: no need to wait out the stall timer
            self._stall.stop()
            self._give_up()

    # ------------------------------------------------------------ layout
    def resizeEvent(self, event) -> None:                    # noqa: N802
        self.picture.setGeometry(self.rect())
        self.controls.setGeometry(self.rect())
        super().resizeEvent(event)

    def toggle_fullscreen(self) -> None:
        """Fill the screen, or go back into the page.

        Merlin's own window goes fullscreen and the player covers it, rather
        than the player leaving for a window of its own. A separate window
        could open behind Merlin, and closing it handed focus elsewhere, which
        could leave Merlin minimised. With one window neither can happen, and
        the window returns to exactly the state it was in before.
        """
        top = self.view.window()
        if not self._full_screen:
            self._full_screen = True
            self._place_timer.stop()
            self._window_state = top.windowState()
            self.setParent(top)
            self.setGeometry(top.rect())
            self.show()
            self.raise_()
            top.installEventFilter(self)
            top.showFullScreen()
            self._corners(top, visible=False)
            self.controls.full = True
            self.controls.setFocus()
            self.controls.update()
        else:
            self._leave_fullscreen(restore=True)

    def _leave_fullscreen(self, restore: bool) -> None:
        """Back into the page, and the window back as it was."""
        if not self._full_screen:
            return
        self._full_screen = False
        top = self.window() if self.parent() is not self.view else self.view.window()
        top.removeEventFilter(self)
        self.setParent(self.view)
        self.show()
        if restore or top.isFullScreen():
            top.setWindowState(self._window_state)
        elif self._window_state & Qt.WindowState.WindowMaximized:
            # fullscreen was ended elsewhere, Merlin's Esc or F11, which
            # leaves the window normal; it was maximised before, so it is again
            top.setWindowState(self._window_state)
        top.raise_()
        top.activateWindow()
        self._corners(top, visible=True)
        self.controls.full = False
        self.controls.setFocus()
        self.controls.update()
        self._place_timer.start()
        self._ask_where()

    @staticmethod
    def _corners(top, visible: bool) -> None:
        """The rounded corner overlay would draw over a fullscreen picture."""
        overlay = getattr(getattr(top, "tabs", None), "_overlay", None)
        if overlay is not None:
            try:
                overlay.setVisible(visible)
            except RuntimeError:
                pass

    def eventFilter(self, watched, event) -> bool:           # noqa: N802
        """While fullscreen, keep covering the window as it resizes."""
        from PyQt6.QtCore import QEvent

        # events can still arrive while the window is being destroyed
        if not getattr(self, "_full_screen", False):
            return False
        if self._full_screen and event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
            self.raise_()
        elif (self._full_screen and event.type() == QEvent.Type.WindowStateChange
              and not watched.isFullScreen()):
            # Fullscreen ended some other way: Merlin's own Esc shortcut, F11,
            # or the system. The player follows the window back, rather than
            # staying stretched over it.
            QTimer.singleShot(0, lambda: self._leave_fullscreen(restore=False))
        return False

    def stop(self) -> None:
        """Stop playing and step aside, leaving the page as it was."""
        if self._stopped:
            return
        self._stopped = True
        self._place_timer.stop()
        self._stall.stop()
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
        if self._full_screen:
            # leave the window as it was before fullscreen, not stuck in it
            self.toggle_fullscreen()
        # asked, not waited for: if Qt's stop sticks, only the worker waits
        _ABANDONED.add((worker, thread))
        thread.finished.connect(lambda pair=(worker, thread): _ABANDONED.discard(pair))
        self._ask_shut_down.emit()
        self.hide()
        self._note("in-page player: stopped")
        self.closed.emit()
        self.deleteLater()
