"""Tab strips.

One model, two views. ``TabContainer`` owns the list of tabs and a
QStackedWidget of pages, and renders that list either as a normal horizontal
QTabBar or as a vertical strip pinned to the left or right edge. It exposes the
slice of the QTabWidget API the browser actually uses, so switching orientation
at runtime needs no changes anywhere else.

Both views put the close button on the *left* inside each tab and offer a "+"
at the end of the strip.

The vertical strip floats above the page rather than sitting in the layout: it
reserves a narrow collapsed column, then animates out over the content when the
pointer enters it. Expanding by relayout would reflow the web view on every
hover, which looks terrible and costs a full page layout each time.
"""
from __future__ import annotations

from PyQt6.QtCore import (
    QEasingCurve, QPropertyAnimation, QSize, QTimer, Qt, pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtGui import (
    QColor, QCursor, QIcon, QPainter, QPainterPath, QRegion,
)
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QSpacerItem, QStackedWidget,
    QTabBar, QToolButton, QVBoxLayout, QWidget,
)

COLLAPSED_WIDTH = 46
EXPANDED_WIDTH = 226
ANIM_MS = 160

ICON_ROW_HEIGHT = 34
ICON_SIZE = 16
STRIP_MARGIN = 4
CHIP_BORDER = 3
# Left inset that puts the favicon's axis exactly on the column's axis. The
# chip's left border does not shift the contents, so it plays no part here:
# only the strip margin does.
ICON_INSET = (COLLAPSED_WIDTH // 2) - STRIP_MARGIN - (ICON_SIZE // 2)
# A little air on the right so a page's scrollbar, which Chromium draws hard
# against the edge, is not clipped by the rounded corner.
PAGE_EDGE_INSET = 5
THEME = {
    "dark": {
        "panel": "#1b1c20", "edge": "#26282e",
        "chip": "#232630", "chip_hover": "#2d303a",
        "chip_active": "#3c414f", "accent": "#6f8ff0",
        "text": "#c8cad2", "text_active": "#ffffff",
    },
    "light": {
        # same background as QMainWindow and the toolbar in LIGHT_QSS, so the
        # strip does not read as a separate panel
        "panel": "#f6f6f8", "edge": "#e0e2e8",
        "chip": "#ffffff", "chip_hover": "#ffffff",
        "chip_active": "#dde4f5", "accent": "#3b62d6",
        "text": "#3a3d45", "text_active": "#14161a",
    },
}
PANEL = THEME["dark"]["panel"]
PANEL_EDGE = THEME["dark"]["edge"]


def _toggle_window_max(window) -> None:
    """Maximise through the window's own handler, which knows about frameless."""
    toggle = getattr(window, "toggle_maximise", None)
    if callable(toggle):
        toggle()
    elif window.isMaximized():
        window.showNormal()
    else:
        window.showMaximized()


class CloseButton(QToolButton):
    """Small ✕ that sits on the left inside a tab."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("\u2715")
        self.setFixedSize(QSize(16, 16))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("Close tab")
        self.setStyleSheet(
            "QToolButton { border: none; border-radius: 8px; color: #9a9ba1;"
            " font-size: 10px; padding: 0; background: transparent; }"
            "QToolButton:hover { background: #d33; color: #fff; }"
        )


class PlusButton(QToolButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("+")
        self.setFixedSize(QSize(26, 26))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("New tab (Ctrl+T)")
        self.setStyleSheet(
            "QToolButton { border: none; border-radius: 6px; color: #c8c9d0;"
            " font-size: 17px; font-weight: 500; background: transparent; }"
            "QToolButton:hover { background: #34363d; color: #fff; }"
        )


# ------------------------------------------------------------- horizontal
class HorizontalTabBar(QTabBar):
    """Normal tab strip. Empty area drags the window when frameless."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setExpanding(False)
        self.setMovable(True)
        self.setTabsClosable(False)          # we supply our own, on the left
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setUsesScrollButtons(True)
        self.setDrawBase(False)
        self.frameless = False

    def mousePressEvent(self, event):
        if (self.frameless
                and event.button() == Qt.MouseButton.LeftButton
                and self.tabAt(event.position().toPoint()) == -1):
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.tabAt(event.position().toPoint()) == -1 and self.frameless:
            _toggle_window_max(self.window())
            return
        super().mouseDoubleClickEvent(event)


# --------------------------------------------------------------- vertical
class VerticalTabRow(QWidget):
    """One row in the vertical strip: [close] [icon] [title]."""

    clicked = pyqtSignal(object)
    closeRequested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.palette_name = "dark"
        self._side = "left"
        self._pinned = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(ICON_ROW_HEIGHT)
        self.setObjectName("tabRow")
        # Without this a plain QWidget subclass ignores stylesheet backgrounds
        # entirely: the text colour applied but the highlight never painted.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._selected = False
        self._expanded = False

        # Icon first, close last. With the close button on the left it was
        # hidden when collapsed, so every favicon jumped sideways as the strip
        # opened. Keeping the icon at a fixed left margin means the expansion
        # only reveals what is to the right of it, and nothing moves.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(ICON_INSET, 0, 8, 0)
        layout.setSpacing(8)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(QSize(ICON_SIZE, ICON_SIZE))

        self.text_label = QLabel(self)
        self.text_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        self.text_label.setTextFormat(Qt.TextFormat.PlainText)

        self.close_button = CloseButton(self)
        self.close_button.clicked.connect(lambda: self.closeRequested.emit(self))

        # A spacer that only expands while the row is collapsed. Without it the
        # hidden title takes its stretch with it, and a layout of fixed-size
        # widgets is centred by Qt, so the favicon slid to the middle of the
        # row halfway through the animation.
        self._filler = QSpacerItem(0, 0, QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Minimum)
        self.pin_label = QLabel("\U0001f4cc", self)
        self.pin_label.setVisible(False)
        self.pin_label.setFixedWidth(14)

        layout.addWidget(self.icon_label)
        layout.addWidget(self.pin_label)
        layout.addWidget(self.text_label, 1)
        layout.addItem(self._filler)
        layout.addWidget(self.close_button)
        self._restyle()

    # ------------------------------------------------------------------
    def set_side(self, side: str) -> None:
        """Put the favicon on whichever edge the strip is docked to.

        On a right-hand strip the row still ran icon-first from the left, so
        the favicon sat at the far side of the expanded panel and slid the full
        width of the animation instead of holding still in the column.
        """
        if side == self._side:
            return
        self._side = side
        layout = self.layout()
        for _ in range(layout.count()):
            layout.takeAt(0)
        if side == "right":
            layout.setContentsMargins(8, 0, ICON_INSET, 0)
            layout.addWidget(self.close_button)
            layout.addItem(self._filler)
            layout.addWidget(self.text_label, 1)
            layout.addWidget(self.pin_label)
            layout.addWidget(self.icon_label)
        else:
            layout.setContentsMargins(ICON_INSET, 0, 8, 0)
            layout.addWidget(self.icon_label)
            layout.addWidget(self.pin_label)
            layout.addWidget(self.text_label, 1)
            layout.addItem(self._filler)
            layout.addWidget(self.close_button)
        layout.invalidate()

    def set_palette(self, name: str) -> None:
        if name != self.palette_name:
            self.palette_name = name
            self._restyle()

    def set_pinned(self, pinned: bool) -> None:
        self._pinned = pinned
        self.pin_label.setVisible(pinned)

    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self._restyle()

    def _restyle(self) -> None:
        """Each row is its own chip, rather than one panel behind them all.

        Collapsed, the rows sit on the strip's own column, so a chip would just
        add noise and they stay clear. Expanded, every row gets its own rounded
        background: titles need something to sit on to be readable, but the
        gaps between the chips keep the page visible, so the strip never
        becomes the solid slab it was before.

        Selectors are by object name. A `VerticalTabRow` type selector does not
        match, so the application's blanket QWidget background would win, and
        without WA_StyledBackground a plain QWidget ignores them entirely.
        """
        c = THEME[self.palette_name]
        if self._selected:
            background = c["chip_active"]
            border = c["accent"]
            colour = c["text_active"]
        elif self._expanded:
            background = c["chip"]
            border = "transparent"
            colour = c["text"]
        else:
            background = "transparent"
            border = "transparent"
            colour = c["text"]
        self.setStyleSheet(
            f"#tabRow {{ background: {background}; border-radius: 7px;"
            f" border-left: 3px solid {border}; }}"
            f"#tabRow:hover {{ background: {c['chip_hover']}; }}"
            f"#tabRow QLabel {{ color: {colour}; background: transparent; }}")

    def set_text(self, text: str) -> None:
        self.text_label.setText(text)
        self.setToolTip(text)

    def set_icon(self, icon: QIcon) -> None:
        if icon is None or icon.isNull():
            self.icon_label.clear()
        else:
            self.icon_label.setPixmap(icon.pixmap(16, 16))

    def set_expanded(self, expanded: bool) -> None:
        """Collapsed shows only the favicon; expanded shows close plus title."""
        if expanded != self._expanded:
            self._expanded = expanded
            self._restyle()
        self.text_label.setVisible(expanded)
        self.close_button.setVisible(expanded)
        self.pin_label.setVisible(expanded and self._pinned)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # left margin is constant in both states: that is what keeps the icon
        # still while the row grows around it
        if expanded:
            self._filler.changeSize(0, 0, QSizePolicy.Policy.Fixed,
                                    QSizePolicy.Policy.Minimum)
        else:
            self._filler.changeSize(0, 0, QSizePolicy.Policy.Expanding,
                                    QSizePolicy.Policy.Minimum)
        self.layout().invalidate()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self)
        super().mousePressEvent(event)


class VerticalTabStrip(QWidget):
    """Vertical strip that widens on hover and narrows when the mouse leaves."""

    currentRequested = pyqtSignal(int)
    closeRequested = pyqtSignal(int)
    newTabRequested = pyqtSignal()
    widthChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expansion = 0.0
        self._side = "left"
        self.palette_name = "dark"
        self.rows: list[VerticalTabRow] = []
        self.setObjectName("verticalStrip")
        # The application stylesheet gives every QWidget a solid background,
        # and the style paints that before paintEvent runs, so the panel filled
        # the whole expanded width no matter what. Override it to transparent
        # and paint the narrow column ourselves.
        self.setStyleSheet("#verticalStrip { background: transparent; }")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(STRIP_MARGIN, 6, STRIP_MARGIN, 6)
        outer.setSpacing(2)

        # The tabs scroll; the + does not. Previously both sat in one column,
        # so once there were more tabs than fit, the button was pushed off the
        # bottom of the screen and became unreachable.
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # The scroll area and its contents must be transparent, otherwise the
        # application stylesheet's blanket QWidget background paints them and
        # the panel reappears across the full expanded width.
        # Targeted by object name. A descendant selector here also matched the
        # tab rows and wiped the selected row's highlight.
        self.scroll.setObjectName("tabScroll")
        self.scroll.setStyleSheet("#tabScroll { background: transparent; }")
        self.scroll.viewport().setObjectName("tabScrollViewport")
        self.scroll.viewport().setStyleSheet(
            "#tabScrollViewport { background: transparent; }")
        self.scroll.viewport().setAutoFillBackground(False)

        self.rows_host = QWidget()
        self.rows_host.setObjectName("rowsHost")
        self.rows_host.setStyleSheet("#rowsHost { background: transparent; }")
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(2)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.rows_host)
        outer.addWidget(self.scroll, 1)
        outer.addSpacing(34)

        # The + is pinned to the collapsed column, not centred in the strip.
        # Centring it meant the button slid sideways every time the strip
        # widened on hover, drifting away from the tab bar.
        # The + is placed by explicit geometry against the painted column,
        # not by nested layouts. Margins, chip borders and device scaling all
        # shifted it a few pixels and it never sat on the column's axis.
        self.plus = PlusButton(self)
        self.plus.clicked.connect(self.newTabRequested.emit)

        self.frameless = False

        self._anim = QPropertyAnimation(self, b"expansion", self)
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # While open, watch where the pointer actually is. leaveEvent alone
        # only fires past the far edge of the expanded strip, so the whole
        # width counted as "still hovering", including the empty space below
        # the last tab: the strip stayed open until the pointer was most of the
        # way into the page. Polling is simpler and more reliable here than
        # tracking mouse events across a scroll area and its children.
        self._watch = QTimer(self)
        self._watch.setInterval(70)
        self._watch.timeout.connect(self._check_pointer)

    # With no title bar and no horizontal strip, this empty space is the only
    # thing left to grab the window by.
    def _on_empty_space(self, point) -> bool:
        child = self.childAt(point)
        return child is None or child in (self.scroll, self.scroll.viewport(),
                                          self.rows_host)

    def mousePressEvent(self, event):
        if self.frameless and event.button() == Qt.MouseButton.LeftButton:
            if self._on_empty_space(event.position().toPoint()):
                handle = self.window().windowHandle()
                if handle is not None:
                    handle.startSystemMove()
                    return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.frameless and self._on_empty_space(event.position().toPoint()):
            _toggle_window_max(self.window())
            return
        super().mouseDoubleClickEvent(event)

    # --------------------------------------------------- hover animation
    def get_expansion(self) -> float:
        return self._expansion

    def set_expansion(self, value: float) -> None:
        self._expansion = value
        self._align_plus()
        expanded = value > 0.5
        for row in self.rows:
            row.set_expanded(expanded)
        self.widthChanged.emit()

    expansion = pyqtProperty(float, fget=get_expansion, fset=set_expansion)

    def current_width(self) -> int:
        return int(COLLAPSED_WIDTH + (EXPANDED_WIDTH - COLLAPSED_WIDTH)
                   * self._expansion)

    def _animate_to(self, target: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._expansion)
        self._anim.setEndValue(target)
        self._anim.start()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._align_plus()

    def enterEvent(self, event):
        self._animate_to(1.0)
        self._watch.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_to(0.0)
        self._watch.stop()
        super().leaveEvent(event)

    def _pointer_holds_open(self) -> bool:
        """Open only over the thin column, or over a tab itself."""
        global_pos = QCursor.pos()
        local = self.mapFromGlobal(global_pos)
        if not self.rect().contains(local):
            return False
        if self._side == "left":
            if local.x() <= COLLAPSED_WIDTH:
                return True
        elif local.x() >= self.width() - COLLAPSED_WIDTH:
            return True
        for row in self.rows:
            if row.isVisible() and row.rect().contains(row.mapFromGlobal(global_pos)):
                return True
        if self.plus.rect().contains(self.plus.mapFromGlobal(global_pos)):
            return True
        return False

    def _check_pointer(self) -> None:
        if self._pointer_holds_open():
            return
        self._watch.stop()
        self._animate_to(0.0)

    def set_side(self, side: str) -> None:
        self._side = side
        for row in self.rows:
            row.set_side(side)
        self._align_plus()
        self.update()

    def paintEvent(self, event):  # noqa: N802
        """Fill the whole widget.

        This used to paint only the collapsed column and leave the expanded
        area alone, on the assumption that the page would show through. It does
        not: sibling widgets are not composited, and the web view draws into its
        own native surface, so those pixels were simply never written and kept
        whatever the backing store last held. On X11 that showed up as garbled
        lines where the panel should be.

        The per-row chips are what stop this reading as one flat slab.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        colours = THEME[self.palette_name]
        painter.fillRect(self.rect(), QColor(colours["panel"]))
        edge = (QRect(self.width() - 1, 0, 1, self.height())
                if self._side == "left" else QRect(0, 0, 1, self.height()))
        painter.fillRect(edge, QColor(colours["edge"]))
        painter.end()

    def _align_plus(self) -> None:
        """Centre the + on the painted column, at the bottom of the strip."""
        size = self.plus.size()
        if self._side == "left":
            left = 0
        else:
            left = self.width() - COLLAPSED_WIDTH
        x = left + (COLLAPSED_WIDTH - size.width()) // 2
        y = self.height() - size.height() - 8
        self.plus.setGeometry(x, y, size.width(), size.height())
        self.plus.raise_()

    # ------------------------------------------------------------- model
    def rebuild(self, tabs: list[dict], current: int) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.rows = []
        for index, tab in enumerate(tabs):
            row = VerticalTabRow(self.rows_host)
            row.set_side(self._side)
            row.set_pinned(bool(tab.get("pinned")))
            row.set_text(tab["text"])
            row.set_icon(tab.get("icon"))
            row.set_palette(self.palette_name)
            row.set_selected(index == current)
            row.set_expanded(self._expansion > 0.5)
            row.clicked.connect(self._row_clicked)
            row.closeRequested.connect(self._row_closed)
            self.rows.append(row)
            self.rows_layout.addWidget(row)
        self.rows_layout.addStretch(1)

    def set_palette(self, name: str) -> None:
        self.palette_name = name
        for row in self.rows:
            row.set_palette(name)
        self.update()

    def update_selection(self, current: int) -> None:
        for index, row in enumerate(self.rows):
            row.set_palette(self.palette_name)
            row.set_selected(index == current)

    def _row_clicked(self, row) -> None:
        if row in self.rows:
            self.currentRequested.emit(self.rows.index(row))

    def _row_closed(self, row) -> None:
        if row in self.rows:
            self.closeRequested.emit(self.rows.index(row))


# -------------------------------------------------------------- container
class TabContainer(QWidget):
    """QTabWidget-shaped façade over a horizontal or vertical strip."""

    currentChanged = pyqtSignal(int)
    tabCloseRequested = pyqtSignal(int)
    newTabRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tabs: list[dict] = []
        self._orientation = "horizontal"
        self._frameless = False
        self._bar_visible = True

        self.stack = QStackedWidget(self)
        self.stack.currentChanged.connect(self._stack_changed)

        # horizontal pieces
        self.h_host = QWidget(self)
        h_layout = QHBoxLayout(self.h_host)
        h_layout.setContentsMargins(0, 0, 4, 0)
        h_layout.setSpacing(0)
        self.h_bar = HorizontalTabBar(self.h_host)
        self.h_bar.currentChanged.connect(self._bar_current_changed)
        self.h_bar.tabMoved.connect(self._tab_moved)
        self.h_plus = PlusButton(self.h_host)
        self.h_plus.clicked.connect(self.newTabRequested.emit)
        h_layout.addWidget(self.h_bar, 0)
        h_layout.addWidget(self.h_plus, 0, Qt.AlignmentFlag.AlignVCenter)
        h_layout.addStretch(1)

        # vertical pieces: child of self, positioned by hand so it floats
        self.v_strip = VerticalTabStrip(self)
        self.v_strip.currentRequested.connect(self.setCurrentIndex)
        self.v_strip.closeRequested.connect(self.tabCloseRequested.emit)
        self.v_strip.newTabRequested.connect(self.newTabRequested.emit)
        self.v_strip.widthChanged.connect(self._place_strip)
        self.v_strip.hide()

        self._corner_radius = 10
        self.setObjectName("tabContainer")
        self.setAutoFillBackground(True)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(0)
        self.root.addWidget(self.h_host)
        self.root.addWidget(self.stack, 1)

    # ------------------------------------------------------ QTabWidget API
    def addTab(self, widget: QWidget, text: str) -> int:  # noqa: N802
        self._tabs.append({"widget": widget, "text": text, "icon": QIcon(),
                           "tooltip": "", "pinned": False})
        self.stack.addWidget(widget)
        if self._orientation == "horizontal":
            self.h_bar.blockSignals(True)
            index = self.h_bar.addTab(text)
            self.h_bar.blockSignals(False)
            self._install_close_button(index)
        self._refresh()
        return len(self._tabs) - 1

    def removeTab(self, index: int) -> None:  # noqa: N802
        if not 0 <= index < len(self._tabs):
            return
        widget = self._tabs[index]["widget"]
        self._tabs.pop(index)
        self.stack.removeWidget(widget)
        if self._orientation == "horizontal":
            self.h_bar.blockSignals(True)
            self.h_bar.removeTab(index)
            self.h_bar.blockSignals(False)
        self._refresh()

    def count(self) -> int:
        return len(self._tabs)

    def widget(self, index: int):
        if 0 <= index < len(self._tabs):
            return self._tabs[index]["widget"]
        return None

    def indexOf(self, widget) -> int:  # noqa: N802
        for index, tab in enumerate(self._tabs):
            if tab["widget"] is widget:
                return index
        return -1

    def currentIndex(self) -> int:  # noqa: N802
        return self.stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if 0 <= index < len(self._tabs):
            self.stack.setCurrentIndex(index)

    def currentWidget(self):  # noqa: N802
        return self.stack.currentWidget()

    def setTabText(self, index: int, text: str) -> None:  # noqa: N802
        if not 0 <= index < len(self._tabs):
            return
        self._tabs[index]["text"] = text
        if self._orientation == "horizontal":
            self.h_bar.setTabText(index, self._display_text(self._tabs[index]))
        elif index < len(self.v_strip.rows):
            self.v_strip.rows[index].set_text(text)

    def tabText(self, index: int) -> str:  # noqa: N802
        if 0 <= index < len(self._tabs):
            return self._tabs[index]["text"]
        return ""

    def setTabIcon(self, index: int, icon: QIcon) -> None:  # noqa: N802
        if not 0 <= index < len(self._tabs):
            return
        self._tabs[index]["icon"] = icon
        if self._orientation == "horizontal":
            self.h_bar.setTabIcon(index, icon)
        elif index < len(self.v_strip.rows):
            self.v_strip.rows[index].set_icon(icon)

    def setTabToolTip(self, index: int, text: str) -> None:  # noqa: N802
        if not 0 <= index < len(self._tabs):
            return
        self._tabs[index]["tooltip"] = text
        if self._orientation == "horizontal":
            self.h_bar.setTabToolTip(index, text)
        elif index < len(self.v_strip.rows):
            self.v_strip.rows[index].setToolTip(text)

    # ------------------------------------------------------------ helpers
    def _install_close_button(self, index: int) -> None:
        """Close button on the LEFT inside the tab."""
        button = CloseButton(self.h_bar)
        button.clicked.connect(lambda _=False, b=button: self._close_from(b))
        self.h_bar.setTabButton(index, QTabBar.ButtonPosition.LeftSide, button)

    def _close_from(self, button) -> None:
        for index in range(self.h_bar.count()):
            if self.h_bar.tabButton(index,
                                    QTabBar.ButtonPosition.LeftSide) is button:
                self.tabCloseRequested.emit(index)
                return

    def _bar_current_changed(self, index: int) -> None:
        self.setCurrentIndex(index)

    def _stack_changed(self, index: int) -> None:
        if self._orientation == "horizontal":
            if self.h_bar.currentIndex() != index:
                self.h_bar.blockSignals(True)
                self.h_bar.setCurrentIndex(index)
                self.h_bar.blockSignals(False)
        else:
            self.v_strip.update_selection(index)
        self.currentChanged.emit(index)

    def _tab_moved(self, source: int, target: int) -> None:
        if not (0 <= source < len(self._tabs) and 0 <= target < len(self._tabs)):
            return
        self._tabs.insert(target, self._tabs.pop(source))
        widget = self.stack.widget(source)
        self.stack.removeWidget(widget)
        self.stack.insertWidget(target, widget)

    def _refresh(self) -> None:
        if self._orientation == "horizontal":
            for index in range(self.h_bar.count()):
                if self.h_bar.tabButton(
                        index, QTabBar.ButtonPosition.LeftSide) is None:
                    self._install_close_button(index)
        else:
            self.v_strip.rebuild(self._tabs, self.currentIndex())
            self._place_strip()

    # ------------------------------------------------------- orientation
    def setOrientation(self, orientation: str) -> None:  # noqa: N802
        if orientation not in ("horizontal", "left", "right"):
            orientation = "horizontal"
        if orientation == self._orientation:
            return
        self._orientation = orientation

        if orientation == "horizontal":
            self.v_strip.hide()
            self.h_host.setVisible(self._bar_visible)
            self.root.setContentsMargins(0, 0, 0, 0)
            self.h_bar.blockSignals(True)
            while self.h_bar.count():
                self.h_bar.removeTab(0)
            for tab in self._tabs:
                index = self.h_bar.addTab(tab["text"])
                self.h_bar.setTabIcon(index, tab["icon"])
                self.h_bar.setTabToolTip(index, tab["tooltip"])
            self.h_bar.setCurrentIndex(self.currentIndex())
            self.h_bar.blockSignals(False)
        else:
            self.h_host.hide()
            self.v_strip.set_side(orientation)
            self.v_strip.setVisible(self._bar_visible)
            self.v_strip.raise_()
        self._refresh()
        self._apply_reserved_margin()

    def orientation(self) -> str:
        return self._orientation

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._round_page()

    def _apply_reserved_margin(self) -> None:
        """Reserve the collapsed column, and keep the page off the right edge."""
        inset = PAGE_EDGE_INSET if self._corner_radius else 0
        if self._orientation == "left" and self._bar_visible:
            self.root.setContentsMargins(COLLAPSED_WIDTH, 0, inset, 0)
        elif self._orientation == "right" and self._bar_visible:
            self.root.setContentsMargins(0, 0, COLLAPSED_WIDTH, 0)
        else:
            self.root.setContentsMargins(0, 0, inset, 0)

    def _place_strip(self) -> None:
        if self._orientation == "horizontal" or not self.v_strip.isVisible():
            return
        width = self.v_strip.current_width()
        if self._orientation == "left":
            self.v_strip.setGeometry(0, 0, width, self.height())
        else:
            self.v_strip.setGeometry(self.width() - width, 0, width,
                                     self.height())
        self.v_strip.raise_()

    def set_corner_radius(self, radius: int) -> None:
        self._corner_radius = max(0, int(radius))
        self._apply_reserved_margin()
        self._round_page()

    def _round_page(self) -> None:
        """Clip the page area to rounded corners.

        The web view paints into its own surface and ignores a border-radius in
        a stylesheet, so the corners have to be cut with a mask. The mask is
        built from a painter path rather than by subtracting squares, which
        keeps the curve smooth.
        """
        if self.stack.width() <= 0 or self.stack.height() <= 0:
            return
        radius = self._corner_radius
        if radius <= 0:
            self.stack.clearMask()
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.stack.rect()), radius, radius)
        self.stack.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._place_strip()
        self._round_page()

    # ------------------------------------------------------------- extras
    def set_palette(self, name: str) -> None:
        """Follow the interface theme.

        The container is filled with the chrome colour as well, because the
        rounded corners cut out of the page reveal whatever is behind them: a
        mismatch there is exactly what made the corners show up as dark notches.
        """
        self.v_strip.set_palette(name)
        chrome = THEME[name]["panel"]
        self.setStyleSheet(f"#tabContainer {{ background: {chrome}; }}")
        self._round_page()

    def set_frameless(self, frameless: bool) -> None:
        self._frameless = frameless
        self.h_bar.frameless = frameless
        self.v_strip.frameless = frameless

    def set_bar_visible(self, visible: bool) -> None:
        self._bar_visible = visible
        if self._orientation == "horizontal":
            self.h_host.setVisible(visible)
            self.v_strip.hide()
        else:
            self.h_host.hide()
            self.v_strip.setVisible(visible)
        self._apply_reserved_margin()
        self._place_strip()

    def tab_at_global(self, global_pos) -> int:
        """Index of the tab under a global point, or -1."""
        if self._orientation == "horizontal":
            return self.h_bar.tabAt(self.h_bar.mapFromGlobal(global_pos))
        for index, row in enumerate(self.v_strip.rows):
            if row.rect().contains(row.mapFromGlobal(global_pos)):
                return index
        return -1

    # ------------------------------------------------------------- pinning
    def is_pinned(self, index: int) -> bool:
        return bool(0 <= index < len(self._tabs) and self._tabs[index].get("pinned"))

    def set_pinned(self, index: int, pinned: bool) -> None:
        """Pin a tab to the start of the strip.

        Pinned tabs are held at the top of a vertical strip, or the left of a
        horizontal one, in the order they were pinned.
        """
        if not 0 <= index < len(self._tabs):
            return
        tab = self._tabs[index]
        if bool(tab.get("pinned")) == pinned:
            return
        tab["pinned"] = pinned
        current = self.currentWidget()
        self._reorder_pinned()
        if current is not None:
            new_index = self.indexOf(current)
            if new_index >= 0:
                self.setCurrentIndex(new_index)

    def toggle_pinned(self, index: int) -> bool:
        self.set_pinned(index, not self.is_pinned(index))
        return self.is_pinned(self.indexOf(self.widget(index))
                              if 0 <= index < len(self._tabs) else -1)

    def _reorder_pinned(self) -> None:
        """Stable sort: pinned first, everything else in its existing order."""
        order = sorted(range(len(self._tabs)),
                       key=lambda i: (0 if self._tabs[i].get("pinned") else 1, i))
        if order == list(range(len(self._tabs))):
            self._rebuild_views()
            return
        self._tabs = [self._tabs[i] for i in order]
        widgets = [tab["widget"] for tab in self._tabs]
        for widget in widgets:
            self.stack.removeWidget(widget)
        for widget in widgets:
            self.stack.addWidget(widget)
        self._rebuild_views()

    def _rebuild_views(self) -> None:
        if self._orientation == "horizontal":
            self.h_bar.blockSignals(True)
            while self.h_bar.count():
                self.h_bar.removeTab(0)
            for tab in self._tabs:
                index = self.h_bar.addTab(self._display_text(tab))
                self.h_bar.setTabIcon(index, tab["icon"])
                self.h_bar.setTabToolTip(index, tab["tooltip"])
            self.h_bar.setCurrentIndex(self.currentIndex())
            self.h_bar.blockSignals(False)
        self._refresh()

    @staticmethod
    def _display_text(tab: dict) -> str:
        return ("\U0001f4cc " + tab["text"]) if tab.get("pinned") else tab["text"]

    def bar_widget(self) -> QWidget:
        return self.h_bar if self._orientation == "horizontal" else self.v_strip
