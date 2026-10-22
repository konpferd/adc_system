"""!
@file widgets.py
@brief Small reusable widgets: collapsible group, status LED icon, hex spin box, error pop-up.
"""
from __future__ import annotations

import html
import time
from typing import List, Optional

from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from adc_common.style import COLOR_ERROR, COLOR_IDLE, COLOR_OK, COLOR_WARN


class CollapsibleBox(QtWidgets.QWidget):
    """!
    @brief Group with a clickable header that shows / hides its content.
    """

    toggled = QtCore.Signal(bool)

    def __init__(self, title: str, expanded: bool = False, parent: Optional[QtWidgets.QWidget] = None) -> None:
        """!
        @brief Create the box.
        @param title     Header text.
        @param expanded  Initial state.
        @param parent    Parent widget.
        """
        super().__init__(parent)
        self._btn: QtWidgets.QToolButton = QtWidgets.QToolButton()
        self._btn.setText(title)
        self._btn.setCheckable(True)
        self._btn.setChecked(expanded)
        self._btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._btn.setArrowType(QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow)
        self._btn.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self._btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self._btn.toggled.connect(self._on_toggle)
        line: QtWidgets.QFrame = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        self.content: QtWidgets.QWidget = QtWidgets.QWidget()
        self.content.setVisible(expanded)
        lay: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self._btn)
        lay.addWidget(line)
        lay.addWidget(self.content)

    def _on_toggle(self, on: bool) -> None:
        """!
        @brief Show / hide the content.
        @param on  New state.
        """
        self._btn.setArrowType(QtCore.Qt.ArrowType.DownArrow if on else QtCore.Qt.ArrowType.RightArrow)
        self.content.setVisible(on)
        self.toggled.emit(on)

    def set_expanded(self, on: bool) -> None:
        """!
        @brief Programmatically expand or collapse.
        @param on  Expanded state.
        """
        self._btn.setChecked(on)

    def is_expanded(self) -> bool:
        """!
        @brief Current state.
        @return True when expanded.
        """
        return self._btn.isChecked()


class StatusIcon(QtWidgets.QWidget):
    """!
    @brief Round status icon with a glyph (check, exclamation, cross, power, pause).
    """

    GLYPHS = {"ok": "✔", "warn": "!", "error": "✖", "off": "⏻", "idle": "–", "pause": "❚❚"}

    def __init__(self, size: int = 22, parent: Optional[QtWidgets.QWidget] = None) -> None:
        """!
        @brief Create a grey icon.
        @param size    Diameter in pixels.
        @param parent  Parent widget.
        """
        super().__init__(parent)
        self._size: int = size
        self._color: QtGui.QColor = QtGui.QColor(COLOR_IDLE)
        self._glyph: str = self.GLYPHS["idle"]
        self.setFixedSize(size, size)

    def set_state(self, color: str, glyph: str) -> None:
        """!
        @brief Change color and glyph.
        @param color  Fill color.
        @param glyph  Key of GLYPHS or a literal character.
        """
        self._color = QtGui.QColor(color)
        self._glyph = self.GLYPHS.get(glyph, glyph)
        self.update()

    def paintEvent(self, _ev: QtGui.QPaintEvent) -> None:
        """!
        @brief Draw the filled circle and the glyph.
        @param _ev  Paint event.
        """
        p: QtGui.QPainter = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(1, 1, self._size - 2, self._size - 2)
        p.setPen(QtGui.QColor("white"))
        f: QtGui.QFont = p.font()
        f.setBold(True)
        f.setPixelSize(int(self._size * 0.55))
        p.setFont(f)
        p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, self._glyph)
        p.end()


class HexSpinBox(QtWidgets.QSpinBox):
    """!
    @brief Spin box that shows and accepts values as "0xNN".
    """

    def __init__(self, maximum: int = 0x7F, parent: Optional[QtWidgets.QWidget] = None) -> None:
        """!
        @brief Create the spin box.
        @param maximum  Largest accepted value.
        @param parent   Parent widget.
        """
        super().__init__(parent)
        self.setRange(0, maximum)
        self.setDisplayIntegerBase(16)
        self.setPrefix("0x")
        self.setKeyboardTracking(False)

    def textFromValue(self, value: int) -> str:
        """!
        @brief Two-digit upper-case hex text.
        @param value  Integer value.
        @return Text without prefix.
        """
        return f"{value:02X}"

    def valueFromText(self, text: str) -> int:
        """!
        @brief Parse hex text with or without "0x".
        @param text  Entered text.
        @return Value (0 when invalid).
        """
        t: str = text.strip().lower().replace("0x", "")
        try:
            return int(t, 16)
        except ValueError:
            return 0

    def validate(self, text: str, pos: int) -> object:
        """!
        @brief Accept partial hex input.
        @param text  Current text.
        @param pos   Cursor position.
        @return Qt validator state tuple.
        """
        t: str = text.strip().lower()
        t = t[2:] if t.startswith("0x") else t
        if t == "":
            return (QtGui.QValidator.State.Intermediate, text, pos)
        try:
            v: int = int(t, 16)
        except ValueError:
            return (QtGui.QValidator.State.Invalid, text, pos)
        if v > self.maximum():
            return (QtGui.QValidator.State.Invalid, text, pos)
        return (QtGui.QValidator.State.Acceptable, text, pos)


class ErrorPopup:
    """!
    @brief Non-modal error window that collects device errors instead of stacking dialogs.
    """

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        """!
        @brief Create the (hidden) pop-up manager.
        @param parent  Main window.
        """
        self._parent: QtWidgets.QWidget = parent
        self._box: Optional[QtWidgets.QMessageBox] = None
        self._items: List[str] = []

    def show_error(self, title: str, text: str) -> None:
        """!
        @brief Show an error, appending to an already open pop-up.
        @param title  Short title (e.g. the command).
        @param text   Error text from the device.
        """
        stamp: str = time.strftime("%H:%M:%S")
        self._items.append(f"[{stamp}] {title}: {text}")
        self._items = self._items[-30:]
        if self._box is None:
            box: QtWidgets.QMessageBox = QtWidgets.QMessageBox(self._parent)
            box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
            box.setWindowTitle("Device error")
            box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
            box.setWindowModality(QtCore.Qt.WindowModality.NonModal)
            box.finished.connect(self._closed)
            self._box = box
        self._box.setText(f"<b>{html.escape(title)}</b><br>{html.escape(text)}")
        self._box.setDetailedText("\n".join(self._items))
        self._box.show()
        self._box.raise_()

    def _closed(self, _result: int) -> None:
        """!
        @brief Pop-up closed: start a fresh list next time.
        @param _result  Dialog result.
        """
        self._items = []
        self._box = None


def confirm(parent: QtWidgets.QWidget, title: str, text: str, detail: str = "") -> bool:
    """!
    @brief Modal Yes/No warning used before risky device operations.
    @param parent  Parent widget.
    @param title   Window title.
    @param text    Main question (rich text allowed).
    @param detail  Optional informative text.
    @return True when the user confirmed.
    """
    box: QtWidgets.QMessageBox = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
    box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
    return box.exec() == QtWidgets.QMessageBox.StandardButton.Yes


def color_swatch(color: str, size: int = 14) -> QtWidgets.QLabel:
    """!
    @brief Small colored square used next to channel names.
    @param color  Fill color.
    @param size   Edge length in pixels.
    @return Label widget.
    """
    lbl: QtWidgets.QLabel = QtWidgets.QLabel()
    lbl.setFixedSize(size, size)
    lbl.setStyleSheet(f"background-color: {color}; border: 1px solid #444;")
    return lbl


def state_color(ok: Optional[bool]) -> str:
    """!
    @brief Color for a tri-state flag.
    @param ok  True, False or None (unknown).
    @return Green, grey or amber.
    """
    if ok is None:
        return COLOR_WARN
    return COLOR_OK if ok else COLOR_IDLE


__all__ = ["CollapsibleBox", "StatusIcon", "HexSpinBox", "ErrorPopup", "confirm", "color_swatch",
           "state_color", "COLOR_ERROR"]
