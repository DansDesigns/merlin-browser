"""Toolbar icons that follow the theme: white on dark, black on light.

Icons are drawn from built-in SVG paths rather than pulled from the desktop
theme, because a theme icon is whatever colour its designer chose and will
disappear against the opposite background. The colour is substituted into the
SVG before rendering, so every icon is guaranteed to contrast.
"""
from __future__ import annotations

from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

DARK_FG = "#ececf0"
LIGHT_FG = "#1a1a1c"
DISABLED_DARK = "#55575e"
DISABLED_LIGHT = "#a9aab0"

_STROKE = ('fill="none" stroke="{c}" stroke-width="2" '
           'stroke-linecap="round" stroke-linejoin="round"')

PATHS = {
    "back":     f'<path d="M15 5 L8 12 L15 19" {_STROKE}/>',
    "forward":  f'<path d="M9 5 L16 12 L9 19" {_STROKE}/>',
    "reload":   ('<path d="M19 12a7 7 0 1 1-2.1-5" ' + _STROKE + '/>'
                 '<path d="M19 4 L19 8 L15 8" ' + _STROKE + '/>'),
    "stop":     f'<path d="M7 7 L17 17 M17 7 L7 17" {_STROKE}/>',
    "home":     ('<path d="M4 11 L12 4 L20 11" ' + _STROKE + '/>'
                 '<path d="M6.5 10.5 L6.5 19 L17.5 19 L17.5 10.5" '
                 + _STROKE + '/>'),
    "menu":     ('<path d="M4 7 H20 M4 12 H20 M4 17 H20" ' + _STROKE + '/>'),
    "star":     ('<path d="M12 4.5 L14.3 9.4 L19.5 10.1 L15.7 13.8 '
                 'L16.7 19 L12 16.5 L7.3 19 L8.3 13.8 L4.5 10.1 '
                 'L9.7 9.4 Z" ' + _STROKE + '/>'),
    "star_full": ('<path d="M12 4.5 L14.3 9.4 L19.5 10.1 L15.7 13.8 '
                  'L16.7 19 L12 16.5 L7.3 19 L8.3 13.8 L4.5 10.1 '
                  'L9.7 9.4 Z" fill="{c}" stroke="{c}" stroke-width="1.6" '
                  'stroke-linejoin="round"/>'),
    "bookmarks": ('<path d="M6 4 H18 V20 L12 15.5 L6 20 Z" ' + _STROKE + '/>'),
    "shield":   ('<path d="M12 3.5 L19 6.2 V11.5 C19 15.6 16 18.8 12 20.5 '
                 'C8 18.8 5 15.6 5 11.5 V6.2 Z" ' + _STROKE + '/>'),
    "shield_off": ('<path d="M12 3.5 L19 6.2 V11.5 C19 15.6 16 18.8 12 20.5 '
                   'C8 18.8 5 15.6 5 11.5 V6.2 Z" ' + _STROKE + '/>'
                   '<path d="M5 3.5 L19 20.5" ' + _STROKE + '/>'),
    "plus":     f'<path d="M12 6 V18 M6 12 H18" {_STROKE}/>',
    "close":    f'<path d="M6.5 6.5 L17.5 17.5 M17.5 6.5 L6.5 17.5" {_STROKE}/>',
    "minimise": f'<path d="M6 12 H18" {_STROKE}/>',
    "maximise": ('<rect x="6.5" y="6.5" width="11" height="11" rx="1.5" '
                 + _STROKE + '/>'),
    "restore":  ('<rect x="5.5" y="8.5" width="9" height="9" rx="1.5" '
                 + _STROKE + '/>'
                 '<path d="M8.5 5.5 H18.5 V15.5" ' + _STROKE + '/>'),
    "play":     ('<path d="M8 5.5 L18 12 L8 18.5 Z" fill="{c}" stroke="{c}" '
                 'stroke-width="1.5" stroke-linejoin="round"/>'),
    "search":   ('<circle cx="11" cy="11" r="6" ' + _STROKE + '/>'
                 '<path d="M15.5 15.5 L20 20" ' + _STROKE + '/>'),
}


def _svg(name: str, colour: str) -> bytes:
    body = PATHS.get(name, PATHS["close"]).replace("{c}", colour)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            f'{body}</svg>').encode("utf-8")


def _pixmap(name: str, colour: str, size: int, ratio: float = 2.0) -> QPixmap:
    """Render at `ratio` times the pixel density for crisp edges.

    The painter works in LOGICAL coordinates once the pixmap carries a device
    pixel ratio, so the target rect is `size`, not `size * ratio`. Passing the
    physical size draws the glyph at twice its box and crops it to the corner,
    which is what made the toolbar icons look enormous and broken.
    """
    renderer = QSvgRenderer(QByteArray(_svg(name, colour)))
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def themed_icon(name: str, dark: bool, size: int = 18) -> QIcon:
    """An icon in the theme's foreground colour, dimmed when disabled."""
    normal = DARK_FG if dark else LIGHT_FG
    disabled = DISABLED_DARK if dark else DISABLED_LIGHT
    icon = QIcon()
    icon.addPixmap(_pixmap(name, normal, size), QIcon.Mode.Normal)
    icon.addPixmap(_pixmap(name, normal, size), QIcon.Mode.Active)
    icon.addPixmap(_pixmap(name, disabled, size), QIcon.Mode.Disabled)
    return icon


def coloured_icon(name: str, colour: str, size: int = 18) -> QIcon:
    return QIcon(_pixmap(name, colour, size))


def icon_size() -> QSize:
    return QSize(18, 18)
