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
    QEasingCurve, QPropertyAnimation, QSize, Qt, pyqtProperty, pyqtSignal,
)
from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QRegion
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QSpacerItem, QStackedWidget,
    QTabBar, QToolButton, QVBoxLayout, QWidget,
)

# Left inset for the favicon. Chosen so the icon sits centred in the collapsed
# column: (COLLAPSED_WIDTH - 2*outer margin - icon) / 2.
ICON_INSET = 11
PANEL = "#1b1c20"
PANEL_EDGE = "#26282e"
COLLAPSED_WIDTH = 46
EXPANDED_WIDTH = 226
ANIM_MS = 160


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
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)
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
        self.icon_label.setFixedSize(QSize(16, 16))

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
        layout.addWidget(self.icon_label)
        layout.addWidget(self.text_label, 1)
        layout.addItem(self._filler)
        layout.addWidget(self.close_button)
        self._restyle()

    # ------------------------------------------------------------------
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
        if not self._expanded:
            if self._selected:
                self.setStyleSheet(
                    "#tabRow { background: rgba(58, 63, 76, 245);"
                    " border-radius: 7px; border-left: 3px solid #6f8ff0; }"
                    "#tabRow QLabel { color: #ffffff; background: transparent; }")
            else:
                self.setStyleSheet(
                    "#tabRow { background: transparent; border-radius: 7px;"
                    " border-left: 3px solid transparent; }"
                    "#tabRow:hover { background: rgba(44, 47, 56, 235); }"
                    "#tabRow QLabel { color: #c3c5cd; background: transparent; }")
            return

        if self._selected:
            self.setStyleSheet(
                "#tabRow { background: rgba(60, 65, 79, 250);"
                " border-radius: 7px; border-left: 3px solid #6f8ff0; }"
                "#tabRow QLabel { color: #ffffff; background: transparent; }")
        else:
            self.setStyleSheet(
                "#tabRow { background: rgba(31, 33, 40, 232);"
                " border-radius: 7px; border-left: 3px solid transparent; }"
                "#tabRow:hover { background: rgba(45, 48, 58, 244); }"
                "#tabRow QLabel { color: #c8cad2; background: transparent; }")

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
        self.rows: list[VerticalTabRow] = []
        self.setObjectName("verticalStrip")
        # The application stylesheet gives every QWidget a solid background,
        # and the style paints that before paintEvent runs, so the panel filled
        # the whole expanded width no matter what. Override it to transparent
        # and paint the narrow column ourselves.
        self.setStyleSheet("#verticalStrip { background: transparent; }")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 6, 4, 6)
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

        # The + is pinned to the collapsed column, not centred in the strip.
        # Centring it meant the button slid sideways every time the strip
        # widened on hover, drifting away from the tab bar.
        # The row holding the + is fixed to the collapsed width. Letting it
        # span the strip meant its whole width grew on hover, so the button's
        # row stretched out across the expanded panel instead of staying put.
        self.plus_row = QWidget(self)
        self.plus_row.setFixedSize(COLLAPSED_WIDTH, 30)
        self.plus_row.setObjectName("plusRow")
        self.plus_row.setStyleSheet("#plusRow { background: transparent; }")
        plus_layout = QHBoxLayout(self.plus_row)
        plus_layout.setContentsMargins(0, 0, 0, 0)
        plus_layout.setSpacing(0)
        self.plus = PlusButton(self.plus_row)
        self.plus.clicked.connect(self.newTabRequested.emit)
        plus_layout.addWidget(self.plus, 0, Qt.AlignmentFlag.AlignCenter)
        self._plus_layout = plus_layout
        self._plus_holder = QWidget(self)
        self._plus_holder.setFixedHeight(30)
        self._plus_holder.setObjectName("plusHolder")
        self._plus_holder.setStyleSheet("#plusHolder { background: transparent; }")
        holder_layout = QHBoxLayout(self._plus_holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(0)
        holder_layout.addWidget(self.plus_row)
        holder_layout.addStretch(1)
        self._holder_layout = holder_layout
        outer.addWidget(self._plus_holder)
        self._align_plus()

        self.frameless = False

        self._anim = QPropertyAnimation(self, b"expansion", self)
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # With no title bar and no horizontal strip, this empty space is the only
    # thing left to grab the window by.
    def _on_empty_space(self, point) -> bool:
        child = self.childAt(point)
        return child is None or child in (self.scroll, self.scroll.viewport(),
                                          self.rows_host, self.plus_row,
                                          self._plus_holder)

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

    def enterEvent(self, event):
        self._animate_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_to(0.0)
        super().leaveEvent(event)

    def set_side(self, side: str) -> None:
        self._side = side
        self._align_plus()
        self.update()

    def paintEvent(self, event):  # noqa: N802
        """Paint only the collapsed column, not the whole expanded panel.

        Filling the entire widget meant hovering dropped a solid slab over the
        page. Now the narrow column keeps its panel, and the width the strip
        gains on hover stays transparent: the rows carry their own backgrounds,
        so the selected tab is the thing that appears to expand.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        column = QRect(0, 0, COLLAPSED_WIDTH, self.height())
        if self._side == "right":
            column.moveLeft(self.width() - COLLAPSED_WIDTH)
        painter.fillRect(column, QColor(PANEL))
        edge = QRect(column.right(), 0, 1, self.height()) if self._side == "left" \
            else QRect(column.left() - 1, 0, 1, self.height())
        painter.fillRect(edge, QColor(PANEL_EDGE))
        painter.end()

    def _align_plus(self) -> None:
        """Keep the + row over the collapsed column, whichever edge we are on.

        Only the empty holder either side of it takes up the extra width the
        strip gains on hover, so the button and its row never move or grow.
        """
        on_left = self._side == "left"
        while self._holder_layout.count():
            self._holder_layout.takeAt(0)
        if on_left:
            self._holder_layout.addWidget(self.plus_row)
            self._holder_layout.addStretch(1)
        else:
            self._holder_layout.addStretch(1)
            self._holder_layout.addWidget(self.plus_row)

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
            row.set_text(tab["text"])
            row.set_icon(tab.get("icon"))
            row.set_selected(index == current)
            row.set_expanded(self._expansion > 0.5)
            row.clicked.connect(self._row_clicked)
            row.closeRequested.connect(self._row_closed)
            self.rows.append(row)
            self.rows_layout.addWidget(row)
        self.rows_layout.addStretch(1)

    def update_selection(self, current: int) -> None:
        for index, row in enumerate(self.rows):
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
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(0)
        self.root.addWidget(self.h_host)
        self.root.addWidget(self.stack, 1)

    # ------------------------------------------------------ QTabWidget API
    def addTab(self, widget: QWidget, text: str) -> int:  # noqa: N802
        self._tabs.append({"widget": widget, "text": text, "icon": QIcon(),
                           "tooltip": ""})
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
            self.h_bar.setTabText(index, text)
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
        """Reserve the collapsed column so the page is never covered."""
        if self._orientation == "left" and self._bar_visible:
            self.root.setContentsMargins(COLLAPSED_WIDTH, 0, 0, 0)
        elif self._orientation == "right" and self._bar_visible:
            self.root.setContentsMargins(0, 0, COLLAPSED_WIDTH, 0)
        else:
            self.root.setContentsMargins(0, 0, 0, 0)

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

    def bar_widget(self) -> QWidget:
        return self.h_bar if self._orientation == "horizontal" else self.v_strip
