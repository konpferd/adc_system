"""!
@file scope_view.py
@brief Oscilloscope screen: one plot with all four channel traces, channel ground markers,
       a draggable trigger level, trigger-point marker and lost-frame markers.

Grid: 10 horizontal x 8 vertical divisions, like a bench oscilloscope. Traces are drawn
in display coordinates: value x display gain + offset.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui

from adc_common.style import CHANNEL_COLORS, COLOR_GAP

from .. import config


class ScopeView(pg.PlotWidget):
    """!
    @brief The plot widget of the scope.
    """

    sigOffsetDragged = QtCore.Signal(int, float)     #!< channel, new marker y (display units)
    sigLevelDragged = QtCore.Signal(float)           #!< new trigger line y (display units)
    sigYViewChanged = QtCore.Signal(float, float)    #!< center, span after mouse zoom/pan
    sigXViewChanged = QtCore.Signal(float, float)    #!< visible x range while frozen

    def __init__(self, parent: Optional[QtCore.QObject] = None) -> None:
        """!
        @brief Build the plot and all overlay items.
        @param parent  Parent widget.
        """
        super().__init__(parent)
        self.plot_item: pg.PlotItem = self.getPlotItem()
        p: pg.PlotItem = self.plot_item
        p.showGrid(x=True, y=True, alpha=0.35)
        p.setMenuEnabled(False)
        p.hideButtons()
        p.setMouseEnabled(x=False, y=True)
        p.getAxis("left").setWidth(70)
        p.setLabel("bottom", "Time", units="s")
        p.setClipToView(False)
        self._updating: bool = False
        self._frozen: bool = False

        self.curves: List[pg.PlotCurveItem] = []
        for c in range(4):
            cur: pg.PlotCurveItem = pg.PlotCurveItem(pen=pg.mkPen(CHANNEL_COLORS[c], width=1), skipFiniteCheck=True)
            cur.setZValue(10 + c)
            p.addItem(cur)
            self.curves.append(cur)

        self.offset_markers: List[pg.InfiniteLine] = []
        for c in range(4):
            ln: pg.InfiniteLine = pg.InfiniteLine(
                angle=0, movable=True, pen=pg.mkPen(CHANNEL_COLORS[c], width=1, style=QtCore.Qt.PenStyle.DotLine),
                hoverPen=pg.mkPen(CHANNEL_COLORS[c], width=2),
                label=f"{c}▶", labelOpts={"position": 0.0, "color": CHANNEL_COLORS[c], "anchors": [(0, 0.5), (0, 0.5)]})
            ln.setZValue(5)
            ln.sigDragged.connect(lambda line, ch=c: self.sigOffsetDragged.emit(ch, float(line.value())))
            p.addItem(ln, ignoreBounds=True)
            self.offset_markers.append(ln)

        self.trigger_line: pg.InfiniteLine = pg.InfiniteLine(
            angle=0, movable=True, pen=pg.mkPen("#7b1fa2", width=1, style=QtCore.Qt.PenStyle.DashLine),
            hoverPen=pg.mkPen("#7b1fa2", width=2),
            label="T◀", labelOpts={"position": 1.0, "color": "#7b1fa2", "anchors": [(1, 0.5), (1, 0.5)]})
        self.trigger_line.setZValue(30)
        self.trigger_line.sigDragged.connect(lambda line: self.sigLevelDragged.emit(float(line.value())))
        p.addItem(self.trigger_line, ignoreBounds=True)

        self.trigger_point: pg.InfiniteLine = pg.InfiniteLine(
            pos=0.0, angle=90, movable=False, pen=pg.mkPen((120, 120, 120), width=1, style=QtCore.Qt.PenStyle.DashLine))
        p.addItem(self.trigger_point, ignoreBounds=True)

        self.gap_item: pg.PlotCurveItem = pg.PlotCurveItem(pen=pg.mkPen(COLOR_GAP, width=1), connect="pairs",
                                                           skipFiniteCheck=True)
        self.gap_item.setZValue(2)
        p.addItem(self.gap_item, ignoreBounds=True)

        self.overlay: pg.TextItem = pg.TextItem(color="#333", anchor=(0, 0))
        self.overlay.setZValue(50)
        p.addItem(self.overlay, ignoreBounds=True)
        self.frozen_text: pg.TextItem = pg.TextItem("STOPPED - drag / wheel to zoom, Reset view to return",
                                                    color="#c62828", anchor=(1, 0))
        self.frozen_text.setZValue(50)
        self.frozen_text.setVisible(False)
        p.addItem(self.frozen_text, ignoreBounds=True)

        vb: pg.ViewBox = p.getViewBox()
        vb.sigYRangeChanged.connect(self._on_y_range)
        vb.sigXRangeChanged.connect(self._on_x_range)
        self._x: Tuple[float, float] = (-1e-3, 1e-3)
        self._y: Tuple[float, float] = (-1.0, 1.0)

    # ---- view geometry -----------------------------------------------------------------------
    def set_x_view(self, lo: float, hi: float) -> None:
        """!
        @brief Set the horizontal range (10 divisions).
        @param lo  Left edge in seconds.
        @param hi  Right edge in seconds.
        """
        self._updating = True
        self._x = (lo, hi)
        self.plot_item.setXRange(lo, hi, padding=0)
        div: float = (hi - lo) / config.H_DIVS
        self.plot_item.getAxis("bottom").setTickSpacing(major=div, minor=div / 5)
        self._updating = False
        self._place_text()

    def set_y_view(self, center: float, span: float) -> None:
        """!
        @brief Set the vertical range (8 divisions).
        @param center  Screen center in display units.
        @param span    Full screen height in display units.
        """
        span = max(abs(span), 1e-15)
        self._updating = True
        self._y = (center - span / 2, center + span / 2)
        self.plot_item.setYRange(self._y[0], self._y[1], padding=0)
        self._set_y_ticks(span)
        self._updating = False
        self._place_text()

    def _set_y_ticks(self, span: float) -> None:
        """!
        @brief Major tick per division, minor tick per 1/5 division.
        @param span  Screen height.
        """
        div: float = span / config.V_DIVS
        self.plot_item.getAxis("left").setTickSpacing(major=div, minor=div / 5)

    def _on_y_range(self, _vb: pg.ViewBox, rng: Sequence[float]) -> None:
        """!
        @brief Mouse zoom/pan on Y: keep 8 divisions and report the new view.
        @param _vb  View box.
        @param rng  New (min, max).
        """
        if self._updating:
            return
        lo, hi = float(rng[0]), float(rng[1])
        self._y = (lo, hi)
        self._set_y_ticks(hi - lo)
        self._place_text()
        self.sigYViewChanged.emit(0.5 * (lo + hi), hi - lo)

    def _on_x_range(self, _vb: pg.ViewBox, rng: Sequence[float]) -> None:
        """!
        @brief Mouse zoom/pan on X (only possible while frozen).
        @param _vb  View box.
        @param rng  New (min, max).
        """
        if self._updating:
            return
        lo, hi = float(rng[0]), float(rng[1])
        self._x = (lo, hi)
        div: float = (hi - lo) / config.H_DIVS
        self.plot_item.getAxis("bottom").setTickSpacing(major=div, minor=div / 5)
        self._place_text()
        self.sigXViewChanged.emit(lo, hi)

    def x_view(self) -> Tuple[float, float]:
        """!
        @brief Current horizontal range.
        @return (left, right) in seconds.
        """
        return self._x

    def y_view(self) -> Tuple[float, float]:
        """!
        @brief Current vertical range.
        @return (bottom, top) in display units.
        """
        return self._y

    def set_frozen(self, frozen: bool) -> None:
        """!
        @brief Enable mouse zoom/pan on X while the display is stopped.
        @param frozen  Display stopped.
        """
        self._frozen = frozen
        self.plot_item.setMouseEnabled(x=frozen, y=True)
        self.frozen_text.setVisible(frozen)
        self._place_text()

    def _place_text(self) -> None:
        """!
        @brief Keep the overlay texts in the top corners.
        """
        self.overlay.setPos(self._x[0], self._y[1])
        self.frozen_text.setPos(self._x[1], self._y[1])

    # ---- content -------------------------------------------------------------------------------
    def set_axis_units(self, label: str, units: str, si: bool) -> None:
        """!
        @brief Label the vertical axis.
        @param label  Axis label.
        @param units  Unit string ("" for codes).
        @param si     Use SI prefixes.
        """
        ax: pg.AxisItem = self.plot_item.getAxis("left")
        ax.enableAutoSIPrefix(si)
        self.plot_item.setLabel("left", label, units=units or None)

    def set_trace(self, ch: int, x: Optional[np.ndarray], y: Optional[np.ndarray]) -> None:
        """!
        @brief Update or hide one trace.
        @param ch  Channel index.
        @param x   X values (None hides the trace).
        @param y   Y values in display coordinates.
        """
        cur: pg.PlotCurveItem = self.curves[ch]
        if x is None or y is None:
            cur.setData([], [])
            cur.setVisible(False)
            return
        cur.setVisible(True)
        cur.setData(x, y)

    def set_line_width(self, width: float) -> None:
        """!
        @brief Trace line width.
        @param width  Pixels.
        """
        for c, cur in enumerate(self.curves):
            cur.setPen(pg.mkPen(CHANNEL_COLORS[c], width=width))

    def set_offset_marker(self, ch: int, y: float, visible: bool) -> None:
        """!
        @brief Position of a channel ground marker.
        @param ch       Channel index.
        @param y        Display coordinate of the channel's zero.
        @param visible  Show the marker.
        """
        ln: pg.InfiniteLine = self.offset_markers[ch]
        ln.setVisible(visible)
        if not ln.moving:
            ln.blockSignals(True)
            ln.setValue(y)
            ln.blockSignals(False)

    def set_trigger_level(self, y: float, color: str, visible: bool) -> None:
        """!
        @brief Position and color of the trigger level line.
        @param y        Display coordinate of the level.
        @param color    Source channel color.
        @param visible  Show the line.
        """
        ln: pg.InfiniteLine = self.trigger_line
        ln.setVisible(visible)
        ln.setPen(pg.mkPen(color, width=1, style=QtCore.Qt.PenStyle.DashLine))
        ln.setHoverPen(pg.mkPen(color, width=2))
        if ln.label is not None:
            ln.label.setColor(color)
        if not ln.moving:
            ln.blockSignals(True)
            ln.setValue(y)
            ln.blockSignals(False)

    def set_gaps(self, xs: Sequence[float]) -> None:
        """!
        @brief Vertical red markers at lost-frame positions.
        @param xs  X positions (empty to clear).
        """
        if not len(xs):
            self.gap_item.setData([], [])
            return
        lo, hi = self._y
        xa: np.ndarray = np.repeat(np.asarray(xs, dtype=np.float64), 2)
        ya: np.ndarray = np.tile([lo, hi], len(xs))
        self.gap_item.setData(xa, ya)

    def set_overlay(self, text: str) -> None:
        """!
        @brief Info text in the top-left corner.
        @param text  Text.
        """
        self.overlay.setText(text)

    def grab_image(self) -> QtGui.QPixmap:
        """!
        @brief Screenshot of the plot.
        @return Pixmap.
        """
        return self.grab()
