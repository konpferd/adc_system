#!/usr/bin/env python3
"""!
@file adc_plot_gui.py
@brief ADC capture viewer: time-domain + FFT plots of a capture CSV, with packet/frame
       loss analysis based on hw_frame_ctr.

Requirements:
    pip install numpy pandas pyqtgraph PySide6        (PyQt5 also works)
    pip install pyarrow                               (optional, much faster CSV loading)

Usage:
    python adc_plot_gui.py [captures/adc_stream_samples.csv]
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adc_common import protocol                                                   # noqa: E402
from adc_common.style import (CHANNELS, DEFAULT_COLORS, DEFAULT_UV_PER_LSB, PANEL_MIN_WIDTH,  # noqa: E402
                              PANEL_WIDTH, UNIT_NAMES, UNIT_RAW, UNIT_SIGNED, UNIT_VOLT,
                              configure_pyqtgraph, status_css)

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
DEFAULT_CSV: str = "captures/adc_stream_samples.csv"
ADC_BITS: int = protocol.ADC_BITS          # raw values are 24-bit two's complement
COUNTER_MOD: int = protocol.COUNTER_MOD    # hw_frame_ctr is a 32-bit unsigned counter
MAX_LISTED_GAPS: int = 20
MARKER_SYMBOLS: Dict[str, str] = {"Circle": "o", "Square": "s", "Triangle": "t",
                                  "Diamond": "d", "Plus": "+", "Cross": "x"}
MAX_FILL_FRAMES: int = 50_000_000   # safety limit for the "fill gaps" FFT mode

configure_pyqtgraph()


# --------------------------------------------------------------------------------------
# Data handling (no GUI code here)
# --------------------------------------------------------------------------------------
@dataclass
class Capture:
    """!
    @brief A loaded capture file with derived time axis and loss analysis.
    """
    path: str
    fs: float
    ctr: np.ndarray                       # uint64, raw hardware counter
    raw: Dict[str, np.ndarray] = field(default_factory=dict)      # ch -> uint32 codes as in the CSV
    signed: Dict[str, np.ndarray] = field(default_factory=dict)   # ch -> int32 sign-extended codes
    t: Optional[np.ndarray] = None                  # seconds, from unwrapped counter
    pos: Optional[np.ndarray] = None                # int64 frame index of each row (unwrapped counter - first)
    gap_idx: Optional[np.ndarray] = None            # i: discontinuity between row i and i+1
    gap_diff: Optional[np.ndarray] = None           # counter step at each discontinuity
    lost: int = 0
    duplicates: int = 0
    seg: Tuple[int, int] = (0, 0)                   # longest gap-free segment [start, stop)

    @property
    def n(self) -> int:
        """!
        @brief Number of samples.
        @return Row count.
        """
        return len(self.ctr)


def to_signed(x: np.ndarray, bits: int) -> np.ndarray:
    """!
    @brief Interpret unsigned raw codes as signed two's complement.
    @param x     Unsigned codes.
    @param bits  Code width.
    @return int32 signed codes.
    """
    return protocol.to_signed(x, bits)


def apply_lsb_filter(signed: np.ndarray, n: int, round_mode: bool) -> np.ndarray:
    """!
    @brief Discard the n lowest bits of the signed code (truncate or round to nearest).
    @param signed      Signed codes.
    @param n           Bits to discard.
    @param round_mode  True = round to nearest, False = truncate.
    @return Filtered int32 codes.
    """
    if n <= 0:
        return signed
    x = signed.astype(np.int64)
    if round_mode:
        x = x + (1 << (n - 1))
    x = (x >> n) << n
    if round_mode:                                   # avoid overflowing the 24-bit range
        x = np.minimum(x, (1 << (ADC_BITS - 1)) - (1 << n))
    return x.astype(np.int32)


def analyze_loss(ctr: np.ndarray) -> Tuple[int, int, np.ndarray, np.ndarray]:
    """!
    @brief Frame-loss analysis of a whole capture (handles the 32-bit wrap).
    @param ctr  hw_frame_ctr column.
    @return Tuple (lost_frames, duplicates, gap_idx, gap_diff).
    """
    diff = np.diff(ctr.astype(np.uint64)) % COUNTER_MOD
    gap_idx = np.nonzero(diff != 1)[0]
    gap_diff = diff[gap_idx]
    lost = int(np.where(gap_diff > 1, gap_diff - 1, 0).sum())
    duplicates = int((gap_diff == 0).sum())
    return lost, duplicates, gap_idx, gap_diff


def longest_contiguous_segment(n: int, gap_idx: np.ndarray) -> Tuple[int, int]:
    """!
    @brief Longest run of rows without a counter discontinuity.
    @param n        Number of rows.
    @param gap_idx  Row indices i with a discontinuity between i and i+1.
    @return [start, stop) row range.
    """
    starts = np.concatenate(([0], gap_idx + 1))
    stops = np.concatenate((gap_idx + 1, [n]))
    k = int(np.argmax(stops - starts))
    return int(starts[k]), int(stops[k])


def load_capture(path: str) -> Capture:
    """!
    @brief Load a capture CSV and run the loss analysis.
    @param path  CSV file.
    @return Capture object.
    @throws ValueError for missing columns / too few samples.
    """
    cols = ["hw_frame_ctr", "sample_rate_hz"]
    header = pd.read_csv(path, nrows=0).columns
    chans = [c for c in CHANNELS if c in header]
    missing = [c for c in cols if c not in header]
    if missing or not chans:
        raise ValueError(f"Missing columns in CSV: {missing or CHANNELS}")
    usecols = cols + chans
    try:                                   # fast path if pyarrow is installed
        df = pd.read_csv(path, usecols=usecols, engine="pyarrow")
    except Exception:
        df = pd.read_csv(path, usecols=usecols)
    if len(df) < 2:
        raise ValueError("CSV contains fewer than 2 samples")

    ctr = df["hw_frame_ctr"].to_numpy(dtype=np.uint64)
    fs = float(df["sample_rate_hz"].iloc[0])
    if fs <= 0:
        raise ValueError("Invalid sample_rate_hz")

    cap = Capture(path=path, fs=fs, ctr=ctr)
    for ch in chans:
        cap.raw[ch] = df[ch].to_numpy(dtype=np.uint32)
        cap.signed[ch] = to_signed(cap.raw[ch], ADC_BITS)

    cap.lost, cap.duplicates, cap.gap_idx, cap.gap_diff = analyze_loss(ctr)
    # unwrapped time axis so that lost frames show up as time gaps
    steps = np.diff(ctr) % COUNTER_MOD
    cap.pos = np.concatenate(([0], np.cumsum(steps.astype(np.int64)))).astype(np.int64)
    cap.t = cap.pos / fs
    cap.seg = longest_contiguous_segment(cap.n, cap.gap_idx)
    return cap


def compute_spectrum(x: np.ndarray, fs: float, window: str, detrend: bool) -> Tuple[np.ndarray, np.ndarray]:
    """!
    @brief Single-sided amplitude spectrum (same units as x, peak amplitude).
    @param x        Samples.
    @param fs       Sample rate.
    @param window   Window name (Rectangular, Hann, Hamming, Blackman).
    @param detrend  Remove the mean first.
    @return Tuple (frequency, amplitude).
    """
    x = np.asarray(x, dtype=np.float64)
    if detrend:
        x = x - x.mean()
    n = len(x)
    w = {
        "Rectangular": np.ones(n),
        "Hann": np.hanning(n),
        "Hamming": np.hamming(n),
        "Blackman": np.blackman(n),
    }[window]
    spec = np.fft.rfft(x * w)
    amp = np.abs(spec) / (w.sum() / 2.0)   # coherent-gain corrected
    amp[0] /= 2.0
    freq = np.fft.rfftfreq(n, 1.0 / fs)
    return freq, amp


def summary_text(cap: Capture) -> str:
    """!
    @brief Text summary of a capture (sizes, rate, loss statistics, first gaps).
    @param cap  Loaded capture.
    @return Multi-line text.
    """
    total = cap.n + cap.lost
    pct = 100.0 * cap.lost / total if total else 0.0
    s0, s1 = cap.seg
    lines = [
        f"File:           {os.path.basename(cap.path)}",
        f"Samples:        {cap.n}",
        f"Sample rate:    {cap.fs:.0f} Hz",
        f"Duration:       {cap.t[-1]:.6f} s",
        f"Counter range:  {cap.ctr[0]} .. {cap.ctr[-1]}",
        f"Discontinuities:{len(cap.gap_idx):>6}",
        f"Lost frames:    {cap.lost} ({pct:.4f} % of {total} expected)",
        f"Duplicates:     {cap.duplicates}",
    ]
    if len(cap.gap_idx):
        lost_per = np.where(cap.gap_diff > 1, cap.gap_diff - 1, 0)
        lines.append(f"Largest gap:    {int(lost_per.max())} frames ({(lost_per.max()/cap.lost * 100):.4f}% of all lost frames)")
    lines.append(f"Longest clean run: {s1 - s0} samples ({((s1 - s0)/total * 100):.4f}% of capture)")
    for i, d in list(zip(cap.gap_idx, cap.gap_diff))[:MAX_LISTED_GAPS]:
        lines.append(f"  gap ctr={cap.ctr[i]} -> {cap.ctr[i + 1]} (step {d})")
    if len(cap.gap_idx) > MAX_LISTED_GAPS:
        lines.append(f"  ... {len(cap.gap_idx) - MAX_LISTED_GAPS} more")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        """!
        @brief Build the viewer window.
        """
        super().__init__()
        self.setWindowTitle("ADC capture viewer")
        self.resize(1500, 900)
        self.cap = None
        self.colors = dict(DEFAULT_COLORS)
        self.t_plots, self.t_curves, self.t_gaps = {}, {}, {}
        self.f_plots, self.f_curves = {}, {}
        self.fft_dirty = True
        self._filt_cache, self._filt_key = {}, None

        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        hbox = QtWidgets.QHBoxLayout(root)
        hbox.addWidget(self._build_panel())

        self.tabs = QtWidgets.QTabWidget()
        self.time_view = pg.GraphicsLayoutWidget()
        self.fft_view = pg.GraphicsLayoutWidget()
        self.tabs.addTab(self.time_view, "Time domain")
        self.tabs.addTab(self.fft_view, "FFT")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        hbox.addWidget(self.tabs, 1)

        self.statusBar().showMessage("Open a capture CSV to start")

    # ---- control panel -------------------------------------------------------------
    def _build_panel(self) -> QtWidgets.QScrollArea:
        """!
        @brief Build the left control panel.
        @return Scroll area with the panel.
        """
        panel = QtWidgets.QWidget()
        panel.setMinimumWidth(PANEL_MIN_WIDTH)
        lay = QtWidgets.QVBoxLayout(panel)

        btn_open = QtWidgets.QPushButton("Open CSV...")
        btn_open.setShortcut(QtGui.QKeySequence("Ctrl+O"))
        btn_open.clicked.connect(self.open_csv)
        lay.addWidget(btn_open)

        # display group
        g = QtWidgets.QGroupBox("Display")
        f = QtWidgets.QFormLayout(g)
        self.cmb_layout = QtWidgets.QComboBox()
        self.cmb_layout.addItems(["One plot per channel", "All channels on one plot"])
        self.cmb_unit = QtWidgets.QComboBox()
        self.cmb_unit.addItems(UNIT_NAMES)
        self.cmb_unit.setCurrentIndex(UNIT_VOLT)
        self.spn_uv = QtWidgets.QDoubleSpinBox()
        self.spn_uv.setDecimals(4)
        self.spn_uv.setRange(1e-6, 1e6)
        self.spn_uv.setValue(DEFAULT_UV_PER_LSB)
        self.spn_uv.setSuffix(" uV/LSB")
        self.chk_gaps = QtWidgets.QCheckBox("Mark lost-frame gaps (red)")
        self.chk_gaps.setChecked(True)
        f.addRow("Layout", self.cmb_layout)
        f.addRow("Units", self.cmb_unit)
        f.addRow("Conversion", self.spn_uv)
        f.addRow(self.chk_gaps)
        lay.addWidget(g)

        # channels group
        g = QtWidgets.QGroupBox("Channels")
        grid = QtWidgets.QGridLayout(g)
        self.chk_ch, self.btn_color = {}, {}
        for r, ch in enumerate(CHANNELS):
            chk = QtWidgets.QCheckBox(ch)
            chk.setChecked(True)
            chk.toggled.connect(self._on_layout_changed)
            btn = QtWidgets.QPushButton("Color")
            btn.clicked.connect(lambda _=False, c=ch: self._pick_color(c))
            self._style_color_button(btn, self.colors[ch])
            grid.addWidget(chk, r, 0)
            grid.addWidget(btn, r, 1)
            self.chk_ch[ch], self.btn_color[ch] = chk, btn
        lay.addWidget(g)

        # FFT group
        g = QtWidgets.QGroupBox("FFT")
        f = QtWidgets.QFormLayout(g)
        self.cmb_range = QtWidgets.QComboBox()
        self.cmb_range.addItems(["Largest gap-free run",
                                 "Whole capture (gaps filled with average)"])
        self.cmb_win = QtWidgets.QComboBox()
        self.cmb_win.addItems(["Hann", "Rectangular", "Hamming", "Blackman"])
        self.chk_detrend = QtWidgets.QCheckBox("Remove DC")
        self.chk_detrend.setChecked(True)
        self.chk_logf = QtWidgets.QCheckBox("Log frequency axis")
        self.chk_logf.setChecked(True)
        self.chk_db = QtWidgets.QCheckBox("Amplitude in dB")
        self.chk_db.setChecked(True)
        f.addRow("Samples", self.cmb_range)
        f.addRow("Window", self.cmb_win)
        f.addRow(self.chk_detrend)
        f.addRow(self.chk_logf)
        f.addRow(self.chk_db)
        lay.addWidget(g)

        # line style
        g = QtWidgets.QGroupBox("Line style")
        f = QtWidgets.QFormLayout(g)
        self.spn_lw = QtWidgets.QDoubleSpinBox()
        self.spn_lw.setRange(0.0, 10.0)
        self.spn_lw.setSingleStep(0.5)
        self.spn_lw.setDecimals(1)
        self.spn_lw.setValue(1.0)
        self.spn_lw.setSuffix(" px")
        self.chk_marker = QtWidgets.QCheckBox("Show point markers")
        self.cmb_symbol = QtWidgets.QComboBox()
        self.cmb_symbol.addItems(list(MARKER_SYMBOLS))
        self.spn_msize = QtWidgets.QSpinBox()
        self.spn_msize.setRange(2, 20)
        self.spn_msize.setValue(5)
        self.spn_msize.setSuffix(" px")
        f.addRow("Line width", self.spn_lw)
        f.addRow(self.chk_marker)
        f.addRow("Marker", self.cmb_symbol)
        f.addRow("Marker size", self.spn_msize)
        lay.addWidget(g)
        self.spn_lw.valueChanged.connect(self._restyle_all)
        self.chk_marker.toggled.connect(self._restyle_all)
        self.cmb_symbol.currentIndexChanged.connect(self._restyle_all)
        self.spn_msize.valueChanged.connect(self._restyle_all)

        # manual time-axis control
        g = QtWidgets.QGroupBox("Time axis (manual)")
        f = QtWidgets.QFormLayout(g)
        self.spn_start = QtWidgets.QDoubleSpinBox()
        self.spn_span = QtWidgets.QDoubleSpinBox()
        for sp in (self.spn_start, self.spn_span):
            sp.setDecimals(6)
            sp.setSuffix(" s")
            sp.setKeyboardTracking(False)
        self.spn_start.setRange(-1e9, 1e9)
        self.spn_span.setRange(1e-6, 1e9)
        self.spn_span.setValue(1.0)
        row = QtWidgets.QHBoxLayout()
        btn_prev = QtWidgets.QPushButton("< Pan")
        btn_next = QtWidgets.QPushButton("Pan >")
        btn_full = QtWidgets.QPushButton("Full range")
        btn_prev.clicked.connect(lambda: self._pan(-1))
        btn_next.clicked.connect(lambda: self._pan(1))
        btn_full.clicked.connect(self._full_range)
        for b in (btn_prev, btn_next, btn_full):
            row.addWidget(b)
        f.addRow("Start", self.spn_start)
        f.addRow("Span", self.spn_span)
        f.addRow(row)
        lay.addWidget(g)
        self.spn_start.valueChanged.connect(self._apply_manual_x)
        self.spn_span.valueChanged.connect(self._apply_manual_x)

        # noise filter
        g = QtWidgets.QGroupBox("Noise filter")
        f = QtWidgets.QFormLayout(g)
        self.spn_lsb = QtWidgets.QSpinBox()
        self.spn_lsb.setRange(0, 16)
        self.spn_lsb.setSuffix(" bit(s)")
        self.cmb_lsbmode = QtWidgets.QComboBox()
        self.cmb_lsbmode.addItems(["Truncate (floor)", "Round to nearest"])
        self.lbl_lsb = QtWidgets.QLabel("")
        f.addRow("Ignore lowest", self.spn_lsb)
        f.addRow("Mode", self.cmb_lsbmode)
        f.addRow(self.lbl_lsb)
        lay.addWidget(g)
        self.spn_lsb.valueChanged.connect(self._on_filter_changed)
        self.cmb_lsbmode.currentIndexChanged.connect(self._on_filter_changed)

        # export
        row = QtWidgets.QHBoxLayout()
        btn_png = QtWidgets.QPushButton("Save PNG...")
        btn_png.setShortcut(QtGui.QKeySequence("Ctrl+S"))
        btn_png.clicked.connect(self.save_png)
        btn_clip = QtWidgets.QPushButton("Copy to clipboard")
        btn_clip.setShortcut(QtGui.QKeySequence("Ctrl+C"))
        btn_clip.clicked.connect(self.copy_png)
        row.addWidget(btn_png)
        row.addWidget(btn_clip)
        lay.addLayout(row)

        # packet-loss summary
        g = QtWidgets.QGroupBox("Packet / frame loss")
        v = QtWidgets.QVBoxLayout(g)
        self.lbl_loss = QtWidgets.QLabel("-")
        self.lbl_loss.setStyleSheet("font-weight: bold; font-size: 13pt;")
        self.txt_summary = QtWidgets.QPlainTextEdit()
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setFont(QtGui.QFont("Monospace", 8))
        self.txt_summary.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.txt_summary.setMinimumHeight(140)
        v.addWidget(self.lbl_loss)
        v.addWidget(self.txt_summary)
        lay.addWidget(g, 1)

        # signals
        self.cmb_layout.currentIndexChanged.connect(self._on_layout_changed)
        self.cmb_unit.currentIndexChanged.connect(self._on_unit_changed)
        self.spn_uv.valueChanged.connect(self._on_unit_changed)
        self.chk_gaps.toggled.connect(self._on_unit_changed)
        for w in (self.cmb_win, self.cmb_range):
            w.currentIndexChanged.connect(self._on_fft_option)
        self.chk_detrend.toggled.connect(self._on_fft_option)
        self.chk_db.toggled.connect(self._on_fft_option)
        self.chk_logf.toggled.connect(self._on_fft_option)
        self._on_unit_combo_enable()
        self._update_lsb_label()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setFixedWidth(PANEL_WIDTH)
        return scroll

    @staticmethod
    def _style_color_button(btn: QtWidgets.QPushButton, color: str) -> None:
        """!
        @brief Color a channel button with readable text.
        @param btn    Button.
        @param color  Channel color.
        """
        qc = QtGui.QColor(color)
        fg = "white" if qc.lightness() < 128 else "black"
        btn.setStyleSheet(f"background-color: {color}; color: {fg};")

    def _pick_color(self, ch: str) -> None:
        """!
        @brief Choose a new channel color.
        @param ch  Channel name.
        """
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.colors[ch]), self, f"Color for {ch}")
        if not c.isValid():
            return
        self.colors[ch] = c.name()
        self._style_color_button(self.btn_color[ch], self.colors[ch])
        for curves in (self.t_curves, self.f_curves):
            if ch in curves:
                self._style_curve(curves[ch], ch)

    def _style_curve(self, curve: pg.PlotDataItem, ch: str) -> None:
        """!
        @brief Apply color, line width and point-marker settings to one curve.
        @param curve  Curve item.
        @param ch     Channel name.
        """
        color = self.colors[ch]
        lw = self.spn_lw.value()
        markers = self.chk_marker.isChecked()
        if lw <= 0 and not markers:
            lw = 1.0                      # never leave a curve invisible
        curve.setPen(pg.mkPen(color, width=lw) if lw > 0 else pg.mkPen(None))
        if markers:
            curve.setSymbol(MARKER_SYMBOLS[self.cmb_symbol.currentText()])
            curve.setSymbolSize(self.spn_msize.value())
            curve.setSymbolPen(pg.mkPen(color))
            curve.setSymbolBrush(pg.mkBrush(color))
        else:
            curve.setSymbol(None)

    def _restyle_all(self, *_: object) -> None:
        """!
        @brief Re-apply the line style to every curve.
        @param _  Ignored signal arguments.
        """
        for curves in (self.t_curves, self.f_curves):
            for ch, curve in curves.items():
                self._style_curve(curve, ch)

    # ---- loading ---------------------------------------------------------------------
    def open_csv(self, _: object = None, path: Optional[str] = None) -> None:
        """!
        @brief Load a capture (file dialog when no path is given).
        @param _     Ignored signal argument.
        @param path  CSV path or None.
        """
        if path is None:
            start = os.path.dirname(self.cap.path) if self.cap else os.path.dirname(DEFAULT_CSV)
            path, _f = QtWidgets.QFileDialog.getOpenFileName(
                self, "Open ADC capture", start, "CSV files (*.csv);;All files (*)")
        if not path:
            return
        self.statusBar().showMessage(f"Loading {path} ...")
        QtWidgets.QApplication.processEvents()
        t0 = time.perf_counter()
        try:
            self.cap = load_capture(path)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Load error", str(e))
            self.statusBar().showMessage("Load failed")
            return
        cap = self.cap
        self._filt_cache, self._filt_key = {}, None
        for ch in CHANNELS:  # disable channels not present in this file
            self.chk_ch[ch].setEnabled(ch in cap.raw)
        self.setWindowTitle(f"ADC capture viewer - {os.path.basename(path)}")
        self.txt_summary.setPlainText(summary_text(cap))
        pct = 100.0 * cap.lost / (cap.n + cap.lost)
        ok = cap.lost == 0 and cap.duplicates == 0
        self.lbl_loss.setText(
            "No frame loss" if ok else f"{cap.lost} lost frames ({pct:.3f} %)")
        self.lbl_loss.setStyleSheet(status_css('#2a8a2a' if ok else '#c62828'))
        self.fft_dirty = True
        self._rebuild()
        self.statusBar().showMessage(
            f"Loaded {cap.n} samples in {time.perf_counter() - t0:.2f} s")

    # ---- plot construction -------------------------------------------------------------
    def _active_channels(self) -> List[str]:
        """!
        @brief Channels present in the file and enabled.
        @return Channel names.
        """
        if not self.cap:
            return []
        return [c for c in CHANNELS if c in self.cap.raw and self.chk_ch[c].isChecked()]

    def _make_plots(self, glw: pg.GraphicsLayoutWidget, xlabel: str, xunits: str) -> Dict[str, pg.PlotItem]:
        """!
        @brief Create plot items for the active channels.
        @param glw     Layout widget.
        @param xlabel  X axis label.
        @param xunits  X axis unit.
        @return {channel: PlotItem}.
        """
        glw.clear()
        chans = self._active_channels()
        plots = {}
        if not chans:
            return plots
        title = f"{os.path.basename(self.cap.path)}"
        if self.cmb_layout.currentIndex() == 1:
            p = glw.addPlot(row=0, col=0)
            p.addLegend(offset=(-10, 10))
            for ch in chans:
                plots[ch] = p
            ordered = [p]
        else:
            first = None
            for i, ch in enumerate(chans):
                p = glw.addPlot(row=i, col=0)
                if first is None:
                    first = p
                else:
                    p.setXLink(first)
                plots[ch] = p
            ordered = [plots[c] for c in chans]
        for p in ordered:
            p.showGrid(x=True, y=True, alpha=0.3)
            p.setDownsampling(auto=True, mode="peak")   # keeps every peak, fast at any zoom
            p.setClipToView(True)
        ordered[0].setTitle(title)
        ordered[-1].setLabel("bottom", xlabel, units=xunits)
        return plots

    def _rebuild(self) -> None:
        """!
        @brief Rebuild both views (layout/channel selection changed).
        """
        if not self.cap:
            return
        self.t_plots = self._make_plots(self.time_view, "Time", "s")
        self.t_curves, self.t_gaps = {}, {}
        self.f_plots = {}
        self.f_curves = {}
        self.fft_view.clear()
        for ch, p in self.t_plots.items():
            curve = p.plot(name=ch, skipFiniteCheck=True)
            self._style_curve(curve, ch)
            self.t_curves[ch] = curve
        for p in {id(p): p for p in self.t_plots.values()}.values():
            gap = pg.PlotCurveItem(pen=pg.mkPen((220, 0, 0, 110), width=1), connect="pairs",
                                  skipFiniteCheck=True)
            p.addItem(gap)
            self.t_gaps[id(p)] = (p, gap)
        self._update_time()
        p0 = next(iter(self.t_plots.values()), None)
        if p0 is not None:
            p0.getViewBox().sigXRangeChanged.connect(self._on_xrange)
            self._on_xrange(None, p0.viewRange()[0])
        self.fft_dirty = True
        if self.tabs.currentIndex() == 1:
            self._update_fft()

    # ---- data -> plots -----------------------------------------------------------------
    def _unit_info(self) -> Tuple[int, str, Optional[str], bool]:
        """!
        @brief Current unit description.
        @return Tuple (unit, label text, unit string, SI prefix).
        """
        u = self.cmb_unit.currentIndex()
        if u == UNIT_RAW:
            return u, "code [LSB]", None, False
        if u == UNIT_SIGNED:
            return u, "signed [LSB]", None, False
        return u, "", "V", True

    def _codes(self, ch: str) -> np.ndarray:
        """!
        @brief Signed codes after the 'ignore n LSBs' filter (cached).
        @param ch  Channel name.
        @return int32 codes.
        """
        n = self.spn_lsb.value()
        if n == 0:
            return self.cap.signed[ch]
        key = (n, self.cmb_lsbmode.currentIndex())
        if self._filt_key != key:
            self._filt_cache, self._filt_key = {}, key
        if ch not in self._filt_cache:
            self._filt_cache[ch] = apply_lsb_filter(self.cap.signed[ch], n, key[1] == 1)
        return self._filt_cache[ch]

    def _series(self, ch: str) -> np.ndarray:
        """!
        @brief Channel data in the selected unit.
        @param ch  Channel name.
        @return Data array.
        """
        u = self.cmb_unit.currentIndex()
        if u == UNIT_RAW:
            if self.spn_lsb.value() == 0:
                return self.cap.raw[ch]
            return (self._codes(ch) & ((1 << ADC_BITS) - 1)).astype(np.uint32)
        codes = self._codes(ch)
        if u == UNIT_SIGNED:
            return codes
        return codes.astype(np.float64) * (self.spn_uv.value() * 1e-6)

    def _set_ylabel(self, p: pg.PlotItem, ch_name: str, text: str, units: Optional[str], si: bool) -> None:
        """!
        @brief Label the Y axis of a plot.
        @param p        Plot.
        @param ch_name  Channel / title text.
        @param text     Unit text for code units.
        @param units    Physical unit or None.
        @param si       Use SI prefixes.
        """
        p.getAxis("left").enableAutoSIPrefix(si)
        label = ch_name if units else (f"{ch_name} {text}".strip())
        p.setLabel("left", label, units=units)

    def _update_time(self) -> None:
        """!
        @brief Redraw the time-domain curves and the gap markers.
        """
        cap = self.cap
        if not cap or not self.t_plots:
            return
        _u, text, units, si = self._unit_info()
        overlay = self.cmb_layout.currentIndex() == 1
        ranges = {}
        for ch, curve in self.t_curves.items():
            y = self._series(ch)
            curve.setData(cap.t, y)
            p = self.t_plots[ch]
            lo, hi = float(y.min()), float(y.max())
            old = ranges.get(id(p), (lo, hi))
            ranges[id(p)] = (min(lo, old[0]), max(hi, old[1]))
            if not overlay:
                self._set_ylabel(p, ch, text, units, si)
        if overlay:
            p = next(iter(self.t_plots.values()))
            self._set_ylabel(p, "Amplitude" if units else "", text, units, si)
        # gap markers: one vertical line per discontinuity, in one single item
        for pid, (p, item) in self.t_gaps.items():
            if len(cap.gap_idx) and self.chk_gaps.isChecked():
                lo, hi = ranges[pid]
                tg = 0.5 * (cap.t[cap.gap_idx] + cap.t[cap.gap_idx + 1])
                item.setData(np.repeat(tg, 2), np.tile([lo, hi], len(tg)))
            else:
                item.setData([], [])
            p.enableAutoRange(axis="y")

    def _update_fft(self) -> None:
        """!
        @brief Recompute and draw the spectra.
        """
        cap = self.cap
        if not cap:
            return
        self.f_plots = self._make_plots(self.fft_view, "Frequency", "Hz")
        self.f_curves = {}
        db = self.chk_db.isChecked()
        logf = self.chk_logf.isChecked()
        _u, text, units, si = self._unit_info()
        s0, s1 = cap.seg
        overlay = self.cmb_layout.currentIndex() == 1
        total = int(cap.pos[-1]) + 1
        fill = self.cmb_range.currentIndex() == 1
        if fill and total > MAX_FILL_FRAMES:
            fill = False
            self.statusBar().showMessage(
                f"Capture too long to fill gaps ({total} frames) - using the largest gap-free run")
        n_fft = total if fill else s1 - s0
        for ch, p in self.f_plots.items():
            y = self._series(ch)
            if fill:                     # uniform time base, lost frames = channel average
                full = np.full(total, y.mean(dtype=np.float64))
                full[cap.pos] = y
                y = full
            else:
                y = y[s0:s1]
            freq, amp = compute_spectrum(y, cap.fs, self.cmb_win.currentText(),
                                         self.chk_detrend.isChecked())
            if logf:                     # drop the DC bin (log(0))
                freq, amp = freq[1:], amp[1:]
            if db:
                amp = 20.0 * np.log10(np.maximum(amp, 1e-30))
            curve = p.plot(freq, amp, name=ch, skipFiniteCheck=True)
            self._style_curve(curve, ch)
            self.f_curves[ch] = curve
            p.setLogMode(x=logf, y=False)
            ax = p.getAxis("left")
            if db:
                ax.enableAutoSIPrefix(False)
                unit_txt = "dBV" if units == "V" else "dB LSB"
                p.setLabel("left", unit_txt if overlay else f"{ch} [{unit_txt}]", units=None)
            else:
                ax.enableAutoSIPrefix(si)
                p.setLabel("left", ("Amplitude" if overlay else ch) if units
                           else ("Amplitude [LSB]" if overlay else f"{ch} [LSB]"), units=units)
            p.enableAutoRange()
        if self.f_plots:
            first = next(iter(self.f_plots.values()))
            what = (f"whole capture, {total - cap.n if total > cap.n else 0} lost frames filled with average"
                    if fill else "longest gap-free run")
            first.setTitle(f"{os.path.basename(cap.path)} - FFT, {self.cmb_win.currentText()} window, "
                           f"{n_fft} samples ({what}), RBW {cap.fs / n_fft:.3f} Hz")
        self.fft_dirty = False

    # ---- slots ---------------------------------------------------------------------------
    def _on_layout_changed(self, *_: object) -> None:
        """!
        @brief Layout or channel selection changed.
        @param _  Ignored.
        """
        self._rebuild()

    def _on_unit_combo_enable(self) -> None:
        """!
        @brief Enable the multiplier only for the voltage unit.
        """
        self.spn_uv.setEnabled(self.cmb_unit.currentIndex() == UNIT_VOLT)

    def _update_lsb_label(self) -> None:
        """!
        @brief Describe the effect of the LSB filter.
        """
        n = self.spn_lsb.value()
        step = (1 << n)
        txt = f"Effective step: {step} LSB = {step * self.spn_uv.value():.3f} uV"
        self.lbl_lsb.setText(txt if n else "Filter off (full 24-bit resolution)")

    def _on_filter_changed(self, *_: object) -> None:
        """!
        @brief LSB filter changed.
        @param _  Ignored.
        """
        self._on_unit_changed()

    # ---- manual time axis -------------------------------------------------------------------
    def _first_time_plot(self) -> Optional[pg.PlotItem]:
        """!
        @brief First time-domain plot (x axes are linked).
        @return Plot or None.
        """
        return next(iter(self.t_plots.values()), None)

    def _on_xrange(self, _vb: pg.ViewBox, rng: Tuple[float, float]) -> None:
        """!
        @brief Keep the Start/Span boxes in sync with mouse zoom/pan.
        @param _vb  View box.
        @param rng  New x range.
        """
        lo, hi = float(rng[0]), float(rng[1])
        span = max(hi - lo, 1e-6)
        for sp, v in ((self.spn_start, lo), (self.spn_span, span)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)
            sp.setSingleStep(span * 0.1)

    def _apply_manual_x(self, *_: object) -> None:
        """!
        @brief Apply the Start/Span boxes.
        @param _  Ignored.
        """
        p = self._first_time_plot()
        if p is not None:
            start, span = self.spn_start.value(), self.spn_span.value()
            p.setXRange(start, start + span, padding=0)

    def _pan(self, direction: int) -> None:
        """!
        @brief Pan by 90 % of the visible span.
        @param direction  -1 left, +1 right.
        """
        p = self._first_time_plot()
        if p is not None:
            lo, hi = p.viewRange()[0]
            shift = direction * (hi - lo) * 0.9
            p.setXRange(lo + shift, hi + shift, padding=0)

    def _full_range(self) -> None:
        """!
        @brief Show the whole capture.
        """
        p = self._first_time_plot()
        if p is not None and self.cap:
            p.setXRange(float(self.cap.t[0]), float(self.cap.t[-1]), padding=0)

    def _on_unit_changed(self, *_: object) -> None:
        """!
        @brief Unit, multiplier or gap marking changed.
        @param _  Ignored.
        """
        self._update_lsb_label()
        self._on_unit_combo_enable()
        self._update_time()
        self.fft_dirty = True
        if self.tabs.currentIndex() == 1:
            self._update_fft()

    def _on_fft_option(self, *_: object) -> None:
        """!
        @brief FFT option changed.
        @param _  Ignored.
        """
        self.fft_dirty = True
        if self.tabs.currentIndex() == 1:
            self._update_fft()

    def _on_tab_changed(self, idx: int) -> None:
        """!
        @brief Compute the FFT lazily when its tab is opened.
        @param idx  Tab index.
        """
        if idx == 1 and self.fft_dirty:
            self._update_fft()

    # ---- export ----------------------------------------------------------------------------
    def _grab(self) -> QtGui.QPixmap:
        """!
        @brief Screenshot of the current tab.
        @return Pixmap.
        """
        return self.tabs.currentWidget().grab()

    def save_png(self) -> None:
        """!
        @brief Save the current view as PNG.
        """
        if not self.cap:
            return
        stem = os.path.splitext(self.cap.path)[0]
        kind = "fft" if self.tabs.currentIndex() == 1 else "time"
        default = f"{stem}_{kind}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save current view as PNG", default, "PNG image (*.png)")
        if path:
            if not path.lower().endswith(".png"):
                path += ".png"
            if self._grab().save(path, "PNG"):
                self.statusBar().showMessage(f"Saved {path}")
            else:
                QtWidgets.QMessageBox.warning(self, "Save error", f"Could not write {path}")

    def copy_png(self) -> None:
        """!
        @brief Copy the current view to the clipboard.
        """
        if self.cap:
            QtWidgets.QApplication.clipboard().setPixmap(self._grab())
            self.statusBar().showMessage("Current view copied to clipboard")


def main() -> None:
    """!
    @brief Start the viewer (optional CSV path as first argument).
    """
    app = pg.mkQApp("ADC capture viewer")
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.open_csv(path=sys.argv[1])
    else:
        QtCore.QTimer.singleShot(0, win.open_csv)
    pg.exec()


if __name__ == "__main__":
    main()