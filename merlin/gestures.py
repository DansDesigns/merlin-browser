"""Two-finger swipe navigation.

Left-to-right goes back, right-to-left goes forward.

Two input paths, because they arrive as completely different events:

* **Trackpads** send horizontal wheel events with a phase (Begin, Update, End).
  Deltas accumulate across a gesture and reset when it ends.
* **Touchscreens** send raw touch events. Two points moving together
  horizontally by enough pixels counts as a swipe.

The filter lives on the QApplication rather than on the web view, because
QtWebEngine renders into a private child widget that swallows input before a
view-level filter would see it. Each event is traced back up to its window.

Vertical movement is checked against horizontal so that ordinary scrolling with
a slight sideways drift never navigates. A cooldown stops one long flick from
walking several steps through history.
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer

# Trackpads report pixel deltas; mice report angle deltas in eighths of a degree.
# The thresholds are deliberately long: a short flick should not take you back a
# page, and the arrow is there to show how much further you have to go.
PIXEL_THRESHOLD = 260       # accumulated px before a swipe registers
ANGLE_THRESHOLD = 620       # accumulated 1/8-degree units for wheel devices
TOUCH_THRESHOLD = 190       # px of travel for a two-finger touchscreen swipe
MIN_DURATION = 0.16         # a swipe faster than this is a flick, not a gesture
IDLE_TIMEOUT = 1000         # ms of no movement before the arrow fades away
DOMINANCE = 1.6             # horizontal must beat vertical by this factor
COOLDOWN = 0.6              # seconds between navigations


class SwipeNavigator(QObject):
    """Application-wide filter turning horizontal swipes into back/forward."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._pixel_x = 0.0
        self._pixel_y = 0.0
        self._angle_x = 0.0
        self._angle_y = 0.0
        self._last_fire = 0.0
        self._touch_start: tuple[float, float] | None = None
        self._touch_points = 0
        self._active_window = None
        self._gesture_started = 0.0

        # A gesture that simply stops, without the trackpad sending an end
        # phase, used to leave the arrow sitting on screen. This clears it.
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(IDLE_TIMEOUT)
        self._idle.timeout.connect(self._on_idle)

    # ------------------------------------------------------------------
    def _enabled(self) -> bool:
        return bool(self.settings.get("swipe_navigation", True))

    def _window_for(self, obj):
        """Walk up from the event's widget to a BrowserWindow, if any."""
        from .browser import BrowserWindow

        widget = obj
        for _ in range(12):
            if widget is None:
                return None
            if isinstance(widget, BrowserWindow):
                return widget
            window = getattr(widget, "window", None)
            if callable(window):
                candidate = window()
                if isinstance(candidate, BrowserWindow):
                    return candidate
            widget = getattr(widget, "parent", lambda: None)()
        return None

    def _indicator(self, window):
        ind = getattr(window, "swipe_indicator", None)
        return ind

    def _can_navigate(self, window, forward: bool) -> bool:
        view = window.current()
        if view is None or not hasattr(view, "page"):
            return False
        history = view.page().history()
        return history.canGoForward() if forward else history.canGoBack()

    def _update_progress(self, obj, forward: bool, progress: float) -> None:
        """Grow the arrow as the swipe travels, before anything is committed."""
        window = self._window_for(obj)
        if window is None:
            self._active_window = None
            return
        self._active_window = window
        indicator = self._indicator(window)
        if indicator is None:
            return
        if not self._can_navigate(window, forward):
            indicator.finish(False)
            return
        indicator.update_gesture(forward, progress)
        self._idle.start()

    def _on_idle(self) -> None:
        self._end_gesture(False)
        self._reset()

    def _end_gesture(self, triggered: bool) -> None:
        self._idle.stop()
        window = self._active_window
        if window is None:
            return
        indicator = self._indicator(window)
        if indicator is not None:
            indicator.finish(triggered)
        self._active_window = None

    def _fire(self, obj, forward: bool) -> bool:
        now = time.monotonic()
        if now - self._last_fire < COOLDOWN:
            return False
        window = self._window_for(obj)
        if window is None:
            return False
        view = window.current()
        if view is None or not hasattr(view, "page"):
            return False
        history = view.page().history()
        if forward:
            if not history.canGoForward():
                return False
            view.forward()
            window.status_label.setText("Forward")
        else:
            if not history.canGoBack():
                return False
            view.back()
            window.status_label.setText("Back")
        self._last_fire = now
        self._end_gesture(True)
        self._reset()
        return True

    def _reset(self) -> None:
        self._pixel_x = self._pixel_y = 0.0
        self._angle_x = self._angle_y = 0.0
        self._touch_start = None
        self._gesture_started = 0.0
        self._idle.stop()

    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):                    # noqa: N802
        if not self._enabled():
            return False
        kind = event.type()

        if kind == QEvent.Type.Wheel:
            return self._handle_wheel(obj, event)
        if kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                    QEvent.Type.TouchEnd):
            return self._handle_touch(obj, event)
        return False

    def _handle_wheel(self, obj, event) -> bool:
        phase = event.phase()
        if phase == Qt.ScrollPhase.ScrollBegin:
            self._reset()
        if not self._gesture_started:
            self._gesture_started = time.monotonic()

        pixel = event.pixelDelta()
        angle = event.angleDelta()
        self._pixel_x += pixel.x()
        self._pixel_y += pixel.y()
        self._angle_x += angle.x()
        self._angle_y += angle.y()

        # prefer pixel deltas when the device supplies them
        if abs(self._pixel_x) > 0:
            horizontal, vertical, threshold = (
                self._pixel_x, self._pixel_y, PIXEL_THRESHOLD)
        else:
            horizontal, vertical, threshold = (
                self._angle_x, self._angle_y, ANGLE_THRESHOLD)

        if abs(horizontal) < abs(vertical) * DOMINANCE:
            if phase == Qt.ScrollPhase.ScrollEnd:
                self._end_gesture(False)
                self._reset()
            return False

        # Fingers moving left-to-right scroll the content leftwards, which Qt
        # reports as a positive x delta. That direction is "back".
        forward = horizontal < 0
        if self.settings.get("invert_swipe", False):
            forward = not forward

        progress = min(1.0, abs(horizontal) / float(threshold))
        if progress > 0.06:
            self._update_progress(obj, forward, progress)

        elapsed = time.monotonic() - self._gesture_started
        if progress < 1.0 or elapsed < MIN_DURATION:
            if phase == Qt.ScrollPhase.ScrollEnd:
                self._end_gesture(False)
                self._reset()
            return False
        return self._fire(obj, forward)

    def _handle_touch(self, obj, event) -> bool:
        try:
            points = event.points()
        except AttributeError:
            return False

        if event.type() == QEvent.Type.TouchEnd:
            self._touch_start = None
            self._touch_points = 0
            self._end_gesture(False)
            return False

        if len(points) != 2:
            self._touch_start = None
            self._touch_points = len(points)
            return False

        centre_x = sum(p.position().x() for p in points) / 2.0
        centre_y = sum(p.position().y() for p in points) / 2.0

        if self._touch_start is None:
            self._touch_start = (centre_x, centre_y)
            self._gesture_started = time.monotonic()
            return False

        dx = centre_x - self._touch_start[0]
        dy = centre_y - self._touch_start[1]
        if abs(dx) < abs(dy) * DOMINANCE:
            return False

        # Here dx is the direction the fingers travelled: right means back.
        forward = dx < 0
        if self.settings.get("invert_swipe", False):
            forward = not forward

        progress = min(1.0, abs(dx) / float(TOUCH_THRESHOLD))
        if progress > 0.06:
            self._update_progress(obj, forward, progress)
        if progress < 1.0 or (time.monotonic() - self._gesture_started) < MIN_DURATION:
            return False
        return self._fire(obj, forward)
