"""!
@file main_window.py
@brief Main window of the ADC scope: wiring between the backend threads and the widgets.

Layout:
    left   : connection, link health / packet loss, capture, display, channels,
             horizontal, vertical and trigger controls
    center : run bar (RUN/STOP, SINGLE, AUTO), scope screen, measurement table
    right  : device control panel (power, START/STOP, PGA, ADC config, console)

Timers (GUI thread):
    display  (~30 fps)  : acquisition -> decimation -> traces
    measure  (5 Hz)     : automatic measurements of the displayed record
    status   (4 Hz)     : link health, capture meter, device panel
    console  (20 Hz)    : console events -> console view, device model, error pop-ups
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from adc_common import protocol
from adc_common.console_protocol import LineKind
from adc_common.style import (CHANNEL_COLORS, COLOR_ERROR, COLOR_IDLE, COLOR_INFO, COLOR_OK, COLOR_WARN,
                              DEFAULT_UV_PER_LSB, PANEL_MIN_WIDTH, PANEL_WIDTH, UNIT_NAMES, UNIT_RAW,
                              UNIT_SIGNED, UNIT_VOLT, status_css)

from .. import config, units
from ..acquisition import Acquisition, Snapshot, SweepStatus
from ..capture import CaptureSession, ConversionJob, next_capture_name, sanitize_name, unique_capture_name
from ..console_client import ConsoleClient, ConsoleEvent, ConsoleReply
from ..device_state import (TOPIC_ADC, TOPIC_ADC_STALE, TOPIC_PGA, TOPIC_PGA_STALE, TOPIC_POWER,
                            TOPIC_TEMP, DeviceState)
from ..link_health import LinkHealth, LinkState, WindowStats, classify
from ..ring_buffer import SampleRing
from ..signal_analysis import Edge, Measurement, TriggerMode, decimate_minmax, measure
from ..stream_client import StreamReceiver
from .device_panel import DevicePanel
from .scope_view import ScopeView
from .widgets import CollapsibleBox, ErrorPopup, StatusIcon, color_swatch, confirm

TARGET_SIM: int = 0
TARGET_DEVICE: int = 1

MEAS_COLUMNS: List[str] = ["Vpp", "Freq", "Period", "Vmax", "Vmin", "Vavg", "Vrms"]

_LINK_LOOK: Dict[LinkState, Tuple[str, str]] = {
    LinkState.NO_CONNECTION: (COLOR_IDLE, "error"),
    LinkState.DEVICE_OFF: ("#424242", "off"),
    LinkState.STOPPED: (COLOR_INFO, "pause"),
    LinkState.HEALTHY: (COLOR_OK, "ok"),
    LinkState.MEDIUM_LOSS: (COLOR_WARN, "warn"),
    LinkState.HEAVY_LOSS: (COLOR_ERROR, "error"),
}


@dataclass
class ChannelSettings:
    """!
    @brief Display settings of one channel (offset kept in LSB so unit changes are lossless).
    """
    enabled: bool = True
    gain: float = 1.0
    offset_lsb: float = 0.0


def nice_125(x: float) -> float:
    """!
    @brief Round up to the next 1-2-5 step (oscilloscope scale values).
    @param x  Positive value.
    @return Smallest 1/2/5 x 10^n >= x.
    """
    if x <= 0 or not math.isfinite(x):
        return 1.0
    e: float = math.floor(math.log10(x))
    for m in (1.0, 2.0, 5.0, 10.0):
        if m * 10 ** e >= x * (1 - 1e-9):
            return m * 10 ** e
    return 10 ** (e + 1)


def make_unit_spin(value: float = 0.0, bounds: Optional[Tuple[float, float]] = None) -> pg.SpinBox:
    """!
    @brief Spin box with decade stepping used for unit-dependent values.
    @param value   Initial value.
    @param bounds  Optional (min, max).
    @return pyqtgraph SpinBox.
    """
    sb: pg.SpinBox = pg.SpinBox(value=value, dec=True, step=0.5, minStep=1e-12, decimals=4, compactHeight=False)
    if bounds is not None:
        sb.setOpts(bounds=bounds)
    return sb


def set_spin_unit(sb: pg.SpinBox, unit: int) -> None:
    """!
    @brief Configure suffix / SI prefix / step of a value spin box for a display unit.
    @param sb    Spin box.
    @param unit  Display unit.
    """
    if unit == UNIT_VOLT:
        sb.setOpts(suffix="V", siPrefix=True, minStep=1e-9, decimals=4)
    else:
        sb.setOpts(suffix="", siPrefix=False, minStep=1.0, decimals=9)


class MainWindow(QtWidgets.QMainWindow):
    """!
    @brief Top-level window of the ADC scope.
    """

    def __init__(self) -> None:
        """!
        @brief Create backend objects, build the UI, restore settings and start the timers.
        """
        super().__init__()
        self.setWindowTitle("ADC scope")
        self._fit_to_screen(1800, 1000)

        # ---- backend
        self.ring: SampleRing = SampleRing(config.RING_CAPACITY)
        self.health: LinkHealth = LinkHealth()
        self.rx: StreamReceiver = StreamReceiver(self.ring, self.health)
        self.console: ConsoleClient = ConsoleClient()
        self.dev: DeviceState = DeviceState()
        self.acq: Acquisition = Acquisition(self.ring)
        self.capture: Optional[CaptureSession] = None
        self.conversion: Optional[ConversionJob] = None
        self.ch: List[ChannelSettings] = [ChannelSettings() for _ in range(4)]
        self.snapshot: Optional[Snapshot] = None
        self.frozen: bool = False
        self.connected: bool = False
        self.console_allowed: bool = False
        self.unit: int = UNIT_VOLT
        self.uv_per_lsb: float = DEFAULT_UV_PER_LSB
        self.y_center: float = 0.0
        self.y_span: float = 10.0
        self.hyst_lsb: float = 50_000.0
        self._last_fs: float = 0.0
        self._chain: List[str] = []
        self._chain_title: str = ""
        self._render_ms: float = 0.0
        self._last_meas: List[Optional[Measurement]] = [None] * 4
        self._temp_pending: bool = False           #!< a DEVICE_TEMP poll is in flight
        self._temp_supported: bool = True          #!< cleared when the firmware rejects DEVICE_TEMP
        self._temp_alarm: bool = False             #!< over-temperature warning currently raised
        self.errors: ErrorPopup = ErrorPopup(self)
        self.settings: QtCore.QSettings = QtCore.QSettings("adc_system", "adc_scope")

        # ---- UI
        root: QtWidgets.QWidget = QtWidgets.QWidget()
        self.setCentralWidget(root)
        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout(root)
        hbox.setContentsMargins(4, 4, 4, 4)
        hbox.addWidget(self._build_left())
        hbox.addWidget(self._build_center(), 1)
        self.device_panel: DevicePanel = DevicePanel()
        self.device_panel.sigSend.connect(self.send_command)
        self.device_panel.sigSendChain.connect(self.send_chain)
        self.btn_dev_toggle: QtWidgets.QToolButton = QtWidgets.QToolButton()
        self.btn_dev_toggle.setCheckable(True)
        self.btn_dev_toggle.setChecked(True)
        self.btn_dev_toggle.setFixedWidth(18)
        self.btn_dev_toggle.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                                          QtWidgets.QSizePolicy.Policy.Expanding)
        self.btn_dev_toggle.setShortcut(QtGui.QKeySequence("Ctrl+D"))
        self.btn_dev_toggle.toggled.connect(self._set_device_panel_visible)
        hbox.addWidget(self.btn_dev_toggle)
        hbox.addWidget(self.device_panel)
        self._set_device_panel_visible(True)
        self.statusBar().showMessage("Select a target and press Connect")

        self._restore_settings()
        self._apply_unit(initial=True)
        self._apply_timebase()
        self._apply_trigger()
        self._update_connection_widgets()

        # ---- timers
        self.t_display: QtCore.QTimer = QtCore.QTimer(self)
        self.t_display.setSingleShot(True)
        self.t_display.timeout.connect(self._on_display_timer)
        self.t_display.start(config.DISPLAY_INTERVAL_MS)
        self.t_measure: QtCore.QTimer = QtCore.QTimer(self)
        self.t_measure.timeout.connect(self._on_measure_timer)
        self.t_measure.start(config.MEASURE_INTERVAL_MS)
        self.t_status: QtCore.QTimer = QtCore.QTimer(self)
        self.t_status.timeout.connect(self._on_status_timer)
        self.t_status.start(config.STATUS_INTERVAL_MS)
        self.t_console: QtCore.QTimer = QtCore.QTimer(self)
        self.t_console.timeout.connect(self._on_console_timer)
        self.t_console.start(config.CONSOLE_POLL_MS)
        self.t_temp: QtCore.QTimer = QtCore.QTimer(self)
        self.t_temp.timeout.connect(self._poll_temperature)
        self.t_temp.start(config.TEMP_POLL_MS)

    # =====================================================================================
    # UI construction
    # =====================================================================================
    def _build_left(self) -> QtWidgets.QScrollArea:
        """!
        @brief Build the left control panel.
        @return Scroll area holding the panel.
        """
        panel: QtWidgets.QWidget = QtWidgets.QWidget()
        panel.setMinimumWidth(PANEL_MIN_WIDTH)
        lay: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(panel)

        # ---- connection
        g: QtWidgets.QGroupBox = QtWidgets.QGroupBox("Connection")
        f: QtWidgets.QFormLayout = QtWidgets.QFormLayout(g)
        self.cmb_target: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.cmb_target.addItems(["Simulator (localhost)", "Device (TCP/IP)"])
        self.cmb_target.currentIndexChanged.connect(self._update_connection_widgets)
        self.edt_ip: QtWidgets.QLineEdit = QtWidgets.QLineEdit(config.DEVICE_HOST)
        self.edt_ip.setInputMask("")
        self.spn_sport: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.spn_cport: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        for sp, v in ((self.spn_sport, config.STREAM_PORT), (self.spn_cport, config.CONSOLE_PORT)):
            sp.setRange(1, 65535)
            sp.setValue(v)
        ports: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        ports.addWidget(QtWidgets.QLabel("stream"))
        ports.addWidget(self.spn_sport)
        ports.addWidget(QtWidgets.QLabel("console"))
        ports.addWidget(self.spn_cport)
        self.chk_sim_console: QtWidgets.QCheckBox = QtWidgets.QCheckBox("Use simulator console emulation (testing)")
        self.chk_sim_console.setToolTip("adc_simulator.py emulates the device console on port 5000.\n"
                                        "Off = device controls are disabled in simulation mode.")
        self.btn_connect: QtWidgets.QPushButton = QtWidgets.QPushButton("Connect")
        self.btn_connect.setMinimumHeight(32)
        self.btn_connect.setStyleSheet("font-weight: bold;")
        self.btn_connect.clicked.connect(self._toggle_connect)
        self.lbl_conn: QtWidgets.QLabel = QtWidgets.QLabel("-")
        self.lbl_conn.setWordWrap(True)
        f.addRow("Target", self.cmb_target)
        f.addRow("Device IP", self.edt_ip)
        f.addRow("Ports", ports)
        f.addRow(self.chk_sim_console)
        f.addRow(self.btn_connect)
        f.addRow(self.lbl_conn)
        lay.addWidget(g)

        # ---- link health
        g = QtWidgets.QGroupBox(f"Data link (packet loss, last {config.HEALTH_WINDOW_S:.0f} s)")
        v: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(g)
        row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        self.ico_link: StatusIcon = StatusIcon(26)
        self.lbl_link: QtWidgets.QLabel = QtWidgets.QLabel("No connection")
        self.lbl_link.setStyleSheet(status_css(COLOR_IDLE))
        row.addWidget(self.ico_link)
        row.addWidget(self.lbl_link, 1)
        v.addLayout(row)
        self.lbl_link_detail: QtWidgets.QLabel = QtWidgets.QLabel("")
        self.lbl_link_detail.setFont(QtGui.QFont("Monospace", 8))
        v.addWidget(self.lbl_link_detail)
        lay.addWidget(g)

        # ---- capture
        g = QtWidgets.QGroupBox("Capture to CSV")
        f = QtWidgets.QFormLayout(g)
        nrow: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        self.edt_name: QtWidgets.QLineEdit = QtWidgets.QLineEdit(config.CAPTURE_DEFAULT_NAME)
        self.edt_name.setToolTip("File name (.csv is added). The name increments after each capture.")
        btn_dir: QtWidgets.QPushButton = QtWidgets.QPushButton("Folder")
        btn_dir.setToolTip(config.CAPTURE_DIR)
        btn_dir.clicked.connect(self._open_capture_dir)
        nrow.addWidget(self.edt_name, 1)
        nrow.addWidget(btn_dir)
        self.spn_limit: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.spn_limit.setRange(1, config.CAPTURE_LIMIT_MAX_BYTES // 1_000_000)
        self.spn_limit.setValue(config.CAPTURE_LIMIT_MAX_BYTES // 1_000_000)
        self.spn_limit.setSuffix(" MB CSV")
        self.chk_keep_raw: QtWidgets.QCheckBox = QtWidgets.QCheckBox("Keep raw .bin file")
        self.btn_capture: QtWidgets.QPushButton = QtWidgets.QPushButton()
        self.btn_capture.setMinimumHeight(36)
        self.btn_capture.clicked.connect(self._toggle_capture)
        self.bar_fill: QtWidgets.QProgressBar = QtWidgets.QProgressBar()
        self.bar_fill.setRange(0, 1000)
        self.bar_fill.setValue(0)
        self.bar_fill.setTextVisible(True)
        self.bar_fill.setFormat("idle")
        self.lbl_capture: QtWidgets.QLabel = QtWidgets.QLabel("")
        self.lbl_capture.setWordWrap(True)
        f.addRow("Name", nrow)
        f.addRow("Size limit", self.spn_limit)
        f.addRow(self.chk_keep_raw)
        f.addRow(self.btn_capture)
        f.addRow("Fill", self.bar_fill)
        f.addRow(self.lbl_capture)
        lay.addWidget(g)
        self._set_capture_button(False)

        # ---- display
        g = QtWidgets.QGroupBox("Display")
        f = QtWidgets.QFormLayout(g)
        self.cmb_unit: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.cmb_unit.addItems(UNIT_NAMES)
        self.cmb_unit.setCurrentIndex(UNIT_VOLT)
        self.cmb_unit.currentIndexChanged.connect(lambda _i: self._apply_unit())
        self.spn_uv: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
        self.spn_uv.setDecimals(4)
        self.spn_uv.setRange(1e-4, 1e6)
        self.spn_uv.setValue(DEFAULT_UV_PER_LSB)
        self.spn_uv.setSuffix(" uV/LSB")
        self.spn_uv.setKeyboardTracking(False)
        self.spn_uv.valueChanged.connect(lambda _v: self._apply_unit())
        self.spn_lw: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
        self.spn_lw.setRange(0.5, 6.0)
        self.spn_lw.setSingleStep(0.5)
        self.spn_lw.setDecimals(1)
        self.spn_lw.setValue(1.0)
        self.spn_lw.setSuffix(" px")
        self.spn_lw.valueChanged.connect(lambda w: self.scope.set_line_width(w))
        self.chk_gaps: QtWidgets.QCheckBox = QtWidgets.QCheckBox("Mark lost-frame gaps (red)")
        self.chk_gaps.setChecked(True)
        f.addRow("Units", self.cmb_unit)
        f.addRow("Conversion", self.spn_uv)
        f.addRow("Line width", self.spn_lw)
        f.addRow(self.chk_gaps)
        lay.addWidget(g)

        # ---- channels
        g = QtWidgets.QGroupBox("Channels (display only)")
        grid: QtWidgets.QGridLayout = QtWidgets.QGridLayout(g)
        grid.addWidget(QtWidgets.QLabel("On"), 0, 0, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Gain ×"), 0, 2)
        grid.addWidget(QtWidgets.QLabel("Offset"), 0, 3)
        self.chk_ch: List[QtWidgets.QCheckBox] = []
        self.spn_gain: List[QtWidgets.QDoubleSpinBox] = []
        self.spn_off: List[pg.SpinBox] = []
        for c in range(4):
            chk: QtWidgets.QCheckBox = QtWidgets.QCheckBox(f"ch{c}")
            chk.setChecked(True)
            chk.toggled.connect(lambda on, ch=c: self._on_channel_enable(ch, on))
            gs: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
            gs.setRange(*config.GAIN_RANGE)
            gs.setDecimals(3)
            gs.setValue(1.0)
            gs.setStepType(QtWidgets.QAbstractSpinBox.StepType.AdaptiveDecimalStepType)
            gs.setKeyboardTracking(False)
            gs.valueChanged.connect(lambda val, ch=c: self._on_gain(ch, val))
            osb: pg.SpinBox = make_unit_spin()
            osb.sigValueChanged.connect(lambda sb, ch=c: self._on_offset_spin(ch, float(sb.value())))
            zero: QtWidgets.QPushButton = QtWidgets.QPushButton("0")
            zero.setFixedWidth(26)
            zero.setToolTip("Reset offset")
            zero.clicked.connect(lambda _=False, ch=c: self._set_offset_units(ch, 0.0))
            grid.addWidget(color_swatch(CHANNEL_COLORS[c]), c + 1, 0)
            grid.addWidget(chk, c + 1, 1)
            grid.addWidget(gs, c + 1, 2)
            grid.addWidget(osb, c + 1, 3)
            grid.addWidget(zero, c + 1, 4)
            self.chk_ch.append(chk)
            self.spn_gain.append(gs)
            self.spn_off.append(osb)
        grid.setColumnStretch(3, 1)
        lay.addWidget(g)

        # ---- horizontal
        g = QtWidgets.QGroupBox("Horizontal")
        f = QtWidgets.QFormLayout(g)
        self.cmb_tb: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for tb in config.TIMEBASES:
            self.cmb_tb.addItem(units.si_format(tb, "s", 3) + "/div", tb)
        self.cmb_tb.setCurrentIndex(self._tb_index(config.DEFAULT_TIMEBASE))
        self.cmb_tb.currentIndexChanged.connect(lambda _i: self._apply_timebase())
        self.spn_pos: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.spn_pos.setRange(0, 100)
        self.spn_pos.setValue(50)
        self.spn_pos.setSuffix(" % pre-trigger")
        self.spn_pos.valueChanged.connect(lambda _v: self._apply_timebase())
        self.lbl_record: QtWidgets.QLabel = QtWidgets.QLabel("")
        f.addRow("Timebase", self.cmb_tb)
        f.addRow("Trigger position", self.spn_pos)
        f.addRow(self.lbl_record)
        lay.addWidget(g)

        # ---- vertical
        g = QtWidgets.QGroupBox("Vertical view (all channels)")
        f = QtWidgets.QFormLayout(g)
        self.spn_ydiv: pg.SpinBox = make_unit_spin(1.0, (1e-12, None))
        self.spn_ydiv.sigValueChanged.connect(lambda sb: self._set_y_view(self.y_center, float(sb.value()) * config.V_DIVS))
        self.spn_ycen: pg.SpinBox = make_unit_spin(0.0)
        self.spn_ycen.sigValueChanged.connect(lambda sb: self._set_y_view(float(sb.value()), self.y_span))
        btn_autoy: QtWidgets.QPushButton = QtWidgets.QPushButton("Auto Y")
        btn_autoy.clicked.connect(self._auto_y)
        f.addRow("Scale", self.spn_ydiv)
        f.addRow("Center", self.spn_ycen)
        f.addRow(btn_autoy)
        lay.addWidget(g)

        # ---- trigger
        g = QtWidgets.QGroupBox("Trigger")
        f = QtWidgets.QFormLayout(g)
        self.cmb_mode: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for m in TriggerMode:
            self.cmb_mode.addItem(m.value, m)
        self.cmb_src: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.cmb_src.addItems([f"ch{c}" for c in range(4)])
        self.cmb_edge: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for e in Edge:
            self.cmb_edge.addItem(e.value, e)
        self.spn_level: pg.SpinBox = make_unit_spin(0.0)
        self.spn_hyst: pg.SpinBox = make_unit_spin(0.0, (0.0, None))
        self.spn_hyst.setToolTip("Noise rejection: the signal must move this far back across the\n"
                                 "level before the next edge is accepted.")
        btn_50: QtWidgets.QPushButton = QtWidgets.QPushButton("Level 50 %")
        btn_50.setToolTip("Set the level to the middle of the source signal")
        btn_50.clicked.connect(self._level_50)
        for w in (self.cmb_mode, self.cmb_src, self.cmb_edge):
            w.currentIndexChanged.connect(lambda _i: self._apply_trigger())
        self.spn_level.sigValueChanged.connect(lambda sb: self._on_level_spin(float(sb.value())))
        self.spn_hyst.sigValueChanged.connect(lambda sb: self._on_hyst_spin(float(sb.value())))
        f.addRow("Mode", self.cmb_mode)
        f.addRow("Source", self.cmb_src)
        f.addRow("Slope", self.cmb_edge)
        f.addRow("Level", self.spn_level)
        f.addRow("Noise reject", self.spn_hyst)
        f.addRow(btn_50)
        lay.addWidget(g)
        lay.addStretch(1)

        scroll: QtWidgets.QScrollArea = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setFixedWidth(PANEL_WIDTH)
        return scroll

    def _build_center(self) -> QtWidgets.QWidget:
        """!
        @brief Build run bar, scope screen and measurement table.
        @return Container widget.
        """
        w: QtWidgets.QWidget = QtWidgets.QWidget()
        v: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)

        bar: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        self.btn_run: QtWidgets.QPushButton = QtWidgets.QPushButton()
        self.btn_run.setMinimumSize(110, 34)
        self.btn_run.setShortcut(QtGui.QKeySequence("Space"))
        self.btn_run.setToolTip("Freeze / resume the display (Space)")
        self.btn_run.clicked.connect(self._toggle_run)
        self.btn_single: QtWidgets.QPushButton = QtWidgets.QPushButton("SINGLE")
        self.btn_single.setMinimumSize(80, 34)
        self.btn_single.clicked.connect(self._single)
        btn_auto: QtWidgets.QPushButton = QtWidgets.QPushButton("AUTO")
        btn_auto.setMinimumSize(80, 34)
        btn_auto.setToolTip("Auto setup: timebase, trigger level and vertical scale from the signal")
        btn_auto.clicked.connect(self._auto_setup)
        self.btn_reset_view: QtWidgets.QPushButton = QtWidgets.QPushButton("Reset view")
        self.btn_reset_view.clicked.connect(self._reset_x_view)
        self.btn_reset_view.setVisible(False)
        self.lbl_sweep: QtWidgets.QLabel = QtWidgets.QLabel("NO DATA")
        self.lbl_sweep.setMinimumWidth(90)
        self.lbl_sweep.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lbl_info: QtWidgets.QLabel = QtWidgets.QLabel("")
        # the info text changes every frame: it must never dictate the window's minimum width
        self.lbl_info.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        self.lbl_info.setMinimumWidth(0)
        btn_png: QtWidgets.QPushButton = QtWidgets.QPushButton("Save PNG...")
        btn_png.setShortcut(QtGui.QKeySequence("Ctrl+S"))
        btn_png.clicked.connect(self._save_png)
        btn_clip: QtWidgets.QPushButton = QtWidgets.QPushButton("Copy")
        btn_clip.setToolTip("Copy screen to clipboard")
        btn_clip.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setPixmap(self.center_widget.grab()))
        for b in (self.btn_run, self.btn_single, btn_auto, self.btn_reset_view):
            bar.addWidget(b)
        bar.addWidget(self.lbl_sweep)
        bar.addWidget(self.lbl_info, 1)
        bar.addWidget(btn_png)
        bar.addWidget(btn_clip)
        v.addLayout(bar)
        self._set_run_button()

        self.scope: ScopeView = ScopeView()
        self.scope.sigOffsetDragged.connect(self._on_offset_dragged)
        self.scope.sigLevelDragged.connect(self._on_level_dragged)
        self.scope.sigYViewChanged.connect(self._on_y_view_mouse)
        self.scope.sigXViewChanged.connect(lambda _a, _b: self._render())
        v.addWidget(self.scope, 1)

        self.tbl: QtWidgets.QTableWidget = QtWidgets.QTableWidget(4, len(MEAS_COLUMNS))
        self.tbl.setHorizontalHeaderLabels(MEAS_COLUMNS)
        self.tbl.setVerticalHeaderLabels([f"ch{c}" for c in range(4)])
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.tbl.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tbl.horizontalHeader().setMinimumSectionSize(40)
        self.tbl.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tbl.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.tbl.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.tbl.verticalHeader().setDefaultSectionSize(22)
        self.tbl.setFixedHeight(22 * 4 + self.tbl.horizontalHeader().sizeHint().height() + 6)
        font: QtGui.QFont = QtGui.QFont("Monospace", 10)
        for c in range(4):
            hdr: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem(f"ch{c}")
            hdr.setForeground(QtGui.QBrush(QtGui.QColor(CHANNEL_COLORS[c])))
            f2: QtGui.QFont = QtGui.QFont()
            f2.setBold(True)
            hdr.setFont(f2)
            self.tbl.setVerticalHeaderItem(c, hdr)
            for k in range(len(MEAS_COLUMNS)):
                it: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem("----")
                it.setTextAlignment(int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
                it.setFont(font)
                it.setForeground(QtGui.QBrush(QtGui.QColor(CHANNEL_COLORS[c])))
                self.tbl.setItem(c, k, it)
        v.addWidget(self.tbl)
        self.center_widget: QtWidgets.QWidget = w
        w.setMinimumWidth(400)
        w.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        return w

    # =====================================================================================
    # Connection
    # =====================================================================================
    def _target_host(self) -> str:
        """!
        @brief Address of the selected target.
        @return Host string.
        """
        return config.SIM_HOST if self.cmb_target.currentIndex() == TARGET_SIM else self.edt_ip.text().strip()

    def _update_connection_widgets(self, *_a: object) -> None:
        """!
        @brief Enable/disable connection inputs according to target and state.
        @param _a  Ignored signal arguments.
        """
        sim: bool = self.cmb_target.currentIndex() == TARGET_SIM
        self.edt_ip.setEnabled(not sim and not self.connected)
        self.chk_sim_console.setEnabled(sim and not self.connected)
        self.cmb_target.setEnabled(not self.connected)
        self.spn_sport.setEnabled(not self.connected)
        self.spn_cport.setEnabled(not self.connected)
        self.btn_connect.setText("Disconnect" if self.connected else "Connect")
        if not self.connected:
            self.device_panel.set_available(
                False, "Device console is disabled in simulation mode."
                if sim and not self.chk_sim_console.isChecked() else "Not connected.")

    def _toggle_connect(self) -> None:
        """!
        @brief Connect to / disconnect from the selected target.
        """
        if self.connected:
            self._disconnect()
            return
        host: str = self._target_host()
        if not host:
            QtWidgets.QMessageBox.warning(self, "Connection", "Enter the device IP address.")
            return
        sim: bool = self.cmb_target.currentIndex() == TARGET_SIM
        self.connected = True
        self.rx.start(host, self.spn_sport.value())
        self.console_allowed = (not sim) or self.chk_sim_console.isChecked()
        self.dev.reset()
        if self.console_allowed:
            self.console.start(host, self.spn_cport.value())
            self.device_panel.set_available(False, f"Connecting to console {host}:{self.spn_cport.value()} ...")
        else:
            self.device_panel.set_available(
                False, "Device console is disabled in simulation mode.\n(Tick 'Use simulator console "
                       "emulation' in Connection to test the controls against adc_simulator.py.)")
        self._update_connection_widgets()
        self.statusBar().showMessage(f"Connecting to {host} ...")

    def _disconnect(self) -> None:
        """!
        @brief Stop capture, stream and console.
        """
        if self.capture is not None:
            self._stop_capture()
        self.rx.stop()
        self.console.stop()
        self.dev.reset()
        self._chain = []
        self.connected = False
        self._update_connection_widgets()
        self.statusBar().showMessage("Disconnected")

    # =====================================================================================
    # Console / device control
    # =====================================================================================
    def send_command(self, cmd: str) -> None:
        """!
        @brief Queue one console command.
        @param cmd  Command text.
        """
        if not self._console_send(cmd):
            self.statusBar().showMessage("Console not connected - command not sent")

    def _console_send(self, cmd: str, tag: Optional[str] = None, quiet: bool = False) -> bool:
        """!
        @brief Queue a console command; announce stream restarts to the receiver.

        ADC START and ADC RESET make the firmware restart the DMA, which drops a partly filled
        buffer. Announcing it lets the receiver ignore that one counter jump instead of
        reporting it as frame loss.

        @param cmd    Command text.
        @param tag    Reply tag.
        @param quiet  Background poll (kept out of the console view).
        @return False when the console is not connected.
        """
        if " ".join(cmd.upper().split()[:2]) in ("ADC START", "ADC RESET"):
            self.rx.expect_restart()
        return self.console.send(cmd, tag=tag, quiet=quiet)

    def send_chain(self, cmds: List[str], title: str) -> None:
        """!
        @brief Execute commands one after another; stop at the first error.
        @param cmds   Commands.
        @param title  Name used in error messages.
        """
        if self._chain:
            QtWidgets.QMessageBox.information(self, title, f"'{self._chain_title}' is still in progress.")
            return
        if not cmds or not self.console.connected:
            self.statusBar().showMessage("Console not connected - command not sent")
            return
        self._chain = list(cmds[1:])
        self._chain_title = title
        self._console_send(cmds[0], tag="chain")

    def _on_console_timer(self) -> None:
        """!
        @brief Process console events (GUI thread).
        """
        events: List[ConsoleEvent] = self.console.poll()
        for ev in events:
            if ev.kind == "line":
                self.device_panel.append_console(ev.text, ev.line_kind, quiet=ev.quiet)
                if not ev.in_reply and ev.line_kind == LineKind.FAULT:
                    self.errors.show_error("Device reported a fault", ev.text.strip())
            elif ev.kind == "tx":
                self.device_panel.append_console(ev.text, outgoing=True, quiet=ev.quiet)
            elif ev.kind == "reply" and ev.reply is not None:
                self._on_reply(ev.reply)
            elif ev.kind == "banner" and ev.init is not None:
                if ev.init.errors:
                    self.errors.show_error("Device start-up", "\n".join(ev.init.errors))
                # PGA values are only meaningful with analog power on (see _on_reply / TOPIC_POWER)
                self.console.send("ADC DISPLAY_SETTINGS", tag="init")
            elif ev.kind == "connected":
                self.dev.console_connected = True
                self._temp_pending = False
                self._temp_supported = True
                self.device_panel.set_available(True)
                self.device_panel.append_console(f"--- connected to {ev.text} ---", LineKind.INFO)
                self.statusBar().showMessage(f"Console connected ({ev.text})")
            elif ev.kind == "disconnected":
                self.dev.reset()
                self._chain = []
                self._temp_pending = False
                if self.connected and self.console_allowed:
                    self.device_panel.set_available(False, "Console disconnected - reconnecting ...")
                self.device_panel.append_console("--- disconnected ---", LineKind.WARNING)
            elif ev.kind == "status":
                self.statusBar().showMessage(ev.text)

    def _on_reply(self, reply: ConsoleReply) -> None:
        """!
        @brief Apply a command reply to the model, handle chains and errors.
        @param reply  Finished reply.
        """
        topics = self.dev.apply_reply(reply)
        in_chain: bool = reply.tag == "chain"
        if reply.tag == "temp":
            self._temp_pending = False
            if TOPIC_TEMP in topics:
                self._check_overtemp()
            elif reply.error or reply.timed_out:
                # firmware without DEVICE_TEMP / XADC unavailable: stop polling, no pop-up storm
                self._temp_supported = False
                self.statusBar().showMessage(f"Temperature polling disabled: {reply.error}")
            self._refresh_device_panel()
            return
        if reply.error:
            title: str = f"{self._chain_title}: {reply.command}" if in_chain else reply.command
            self.errors.show_error(title, reply.error)
        if TOPIC_PGA in topics:
            self.device_panel.load_gains_from_device()
        if TOPIC_POWER in topics and self.dev.analog_power:
            # The potentiometers power up from EEPROM; resync the firmware shadow once the
            # analog rail has settled (PGA RESET re-reads wipers and EEPROM).
            QtCore.QTimer.singleShot(config.PGA_POWER_SETTLE_MS, self._refresh_pga_after_power_on)
        if TOPIC_ADC in topics:
            self.device_panel.load_config_from_device()
        if in_chain:
            if reply.error or reply.timed_out:
                self._chain = []
                if TOPIC_ADC_STALE in topics:
                    self.console.send("ADC DISPLAY_SETTINGS")
                if TOPIC_PGA_STALE in topics:
                    self.console.send("PGA DISPLAY_SETTINGS")
            elif self._chain:
                self._console_send(self._chain.pop(0), tag="chain")
            else:
                self.statusBar().showMessage(f"{self._chain_title}: done")
        else:
            if TOPIC_ADC_STALE in topics:
                self.console.send("ADC DISPLAY_SETTINGS")
            if TOPIC_PGA_STALE in topics:
                self.console.send("PGA DISPLAY_SETTINGS")
        self._refresh_device_panel()

    def _refresh_pga_after_power_on(self) -> None:
        """!
        @brief Read the PGA state after analog power has been switched on.
        """
        if self.console.connected and self.dev.analog_power:
            self.send_chain(["PGA RESET", "PGA DISPLAY_SETTINGS"], "PGA read after power-on")

    def _set_device_panel_visible(self, visible: bool) -> None:
        """!
        @brief Show or collapse the device control panel to give the screen more room.
        @param visible  True = panel shown.
        """
        self.device_panel.setVisible(visible)
        self.btn_dev_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow if visible
                                         else QtCore.Qt.ArrowType.LeftArrow)
        self.btn_dev_toggle.setToolTip("Hide device control panel (Ctrl+D)" if visible
                                       else "Show device control panel (Ctrl+D)")

    def _poll_temperature(self) -> None:
        """!
        @brief 1 Hz DEVICE_TEMP poll; skipped while other commands are queued or one is in flight.
        """
        if (not self.console.connected or not self._temp_supported or self._temp_pending
                or self.console.pending_commands() > 0):
            return
        if self.console.send("DEVICE_TEMP", tag="temp", quiet=True):
            self._temp_pending = True

    def _check_overtemp(self) -> None:
        """!
        @brief Raise the over-temperature warning once per excursion (with hysteresis).
        """
        t = self.dev.temp
        if t is None:
            return
        warn: float = t.warn_level if t.warn_level is not None else config.TEMP_DEFAULT_WARN_C
        if not self._temp_alarm and t.now >= warn:
            self._temp_alarm = True
            self.statusBar().showMessage(f"OVER-TEMPERATURE: {t.now:.1f} \u00B0C (warning level {warn:.0f} \u00B0C)")
            box: QtWidgets.QMessageBox = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            box.setWindowTitle("Device over-temperature")
            box.setText(f"<b>Zynq die temperature {t.now:.1f} \u00B0C</b> has reached the warning level "
                        f"of {warn:.0f} \u00B0C.")
            box.setInformativeText("Check cooling / airflow. Consider stopping the acquisition or switching "
                                   "the analog power off.")
            box.setWindowModality(QtCore.Qt.WindowModality.NonModal)
            box.show()
        elif self._temp_alarm and t.now < warn - config.TEMP_HYSTERESIS_C:
            self._temp_alarm = False
            self.statusBar().showMessage(f"Device temperature back to normal: {t.now:.1f} \u00B0C")

    def _refresh_device_panel(self) -> None:
        """!
        @brief Push the device model and stream state into the device panel.
        """
        flowing: Optional[bool] = None
        if self.rx.stats.connected:
            age: Optional[float] = self.health.data_age()
            flowing = age is not None and age < config.NO_DATA_TIMEOUT_S
        self.device_panel.update_state(self.dev, flowing)

    # =====================================================================================
    # Units / channels / vertical
    # =====================================================================================
    def _apply_unit(self, initial: bool = False) -> None:
        """!
        @brief Apply unit / multiplier change to every unit-dependent widget.
        @param initial  True during start-up (keeps the restored Y view).
        """
        old_unit, old_uv = self.unit, self.uv_per_lsb
        self.unit = self.cmb_unit.currentIndex()
        self.uv_per_lsb = self.spn_uv.value()
        self.spn_uv.setEnabled(self.unit == UNIT_VOLT)
        for sb in (*self.spn_off, self.spn_ydiv, self.spn_ycen, self.spn_level, self.spn_hyst):
            set_spin_unit(sb, self.unit)
        label, unit_str, si = units.unit_axis(self.unit)
        self.scope.set_axis_units(label, unit_str, si)
        if not initial:
            if UNIT_RAW in (old_unit, self.unit) and old_unit != self.unit:
                self._auto_y()
            else:                                    # signed <-> volt: convert the view exactly
                c_lsb: float = units.delta_units_to_lsb(self.y_center, old_unit, old_uv)
                s_lsb: float = units.delta_units_to_lsb(self.y_span, old_unit, old_uv)
                self.y_center = units.delta_lsb_to_units(c_lsb, self.unit, self.uv_per_lsb)
                self.y_span = units.delta_lsb_to_units(s_lsb, self.unit, self.uv_per_lsb)
        self._set_y_view(self.y_center, self.y_span)
        for c in range(4):
            self._show_offset_spin(c)
        self._show_trigger_spins()
        self._render()
        self._last_meas = [None] * 4

    def _set_y_view(self, center: float, span: float) -> None:
        """!
        @brief Set the vertical view and synchronise the spin boxes.
        @param center  Screen center.
        @param span    Screen height (8 divisions).
        """
        self.y_center, self.y_span = float(center), max(float(span), 1e-15)
        self.scope.set_y_view(self.y_center, self.y_span)
        for sb, val in ((self.spn_ycen, self.y_center), (self.spn_ydiv, self.y_span / config.V_DIVS)):
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)
        self._update_markers()

    def _on_y_view_mouse(self, center: float, span: float) -> None:
        """!
        @brief Mouse zoom/pan on the Y axis.
        @param center  New center.
        @param span    New height.
        """
        self.y_center, self.y_span = center, span
        for sb, val in ((self.spn_ycen, center), (self.spn_ydiv, span / config.V_DIVS)):
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)
        self._render()

    def _auto_y(self) -> None:
        """!
        @brief Fit the vertical view to the displayed traces (1-2-5 scale).
        """
        snap: Optional[Snapshot] = self.snapshot
        lo, hi = math.inf, -math.inf
        if snap is not None:
            for c in range(4):
                if not self.ch[c].enabled:
                    continue
                y: np.ndarray = self._display_values(c, snap.data[c][-config.MEASURE_MAX_SAMPLES:])
                if y.size:
                    lo, hi = min(lo, float(y.min())), max(hi, float(y.max()))
        if not math.isfinite(lo):
            full: float = units.delta_lsb_to_units(2 * protocol.SIGNED_MAX, self.unit, self.uv_per_lsb)
            lo, hi = (-full / 2, full / 2) if self.unit != UNIT_RAW else (0.0, float(protocol.CODE_MASK))
        if hi - lo <= 0:
            hi, lo = hi + 1, lo - 1
        per_div: float = nice_125((hi - lo) * 1.15 / config.V_DIVS)
        self._set_y_view(0.5 * (lo + hi), per_div * config.V_DIVS)
        self._render()

    def _offset_units(self, c: int) -> float:
        """!
        @brief Channel offset in display units.
        @param c  Channel.
        @return Offset.
        """
        return units.delta_lsb_to_units(self.ch[c].offset_lsb, self.unit, self.uv_per_lsb)

    def _show_offset_spin(self, c: int) -> None:
        """!
        @brief Update the offset spin box of a channel from the stored LSB value.
        @param c  Channel.
        """
        sb: pg.SpinBox = self.spn_off[c]
        sb.blockSignals(True)
        sb.setValue(self._offset_units(c))
        sb.blockSignals(False)

    def _set_offset_units(self, c: int, value: float) -> None:
        """!
        @brief Set a channel offset given in display units.
        @param c      Channel.
        @param value  Offset.
        """
        self.ch[c].offset_lsb = units.delta_units_to_lsb(value, self.unit, self.uv_per_lsb)
        self._show_offset_spin(c)
        self._update_markers()
        self._render()

    def _on_offset_spin(self, c: int, value: float) -> None:
        """!
        @brief Offset typed / stepped in the spin box.
        @param c      Channel.
        @param value  Offset in display units.
        """
        self.ch[c].offset_lsb = units.delta_units_to_lsb(value, self.unit, self.uv_per_lsb)
        self._update_markers()
        self._render()

    def _on_offset_dragged(self, c: int, y: float) -> None:
        """!
        @brief Ground marker dragged on the screen.
        @param c  Channel.
        @param y  New marker position (display units).
        """
        self.ch[c].offset_lsb = units.delta_units_to_lsb(y, self.unit, self.uv_per_lsb)
        self._show_offset_spin(c)
        self._update_trigger_line()
        self._render()

    def _on_gain(self, c: int, value: float) -> None:
        """!
        @brief Display gain changed.
        @param c      Channel.
        @param value  Multiplier.
        """
        self.ch[c].gain = float(value)
        self._update_markers()
        self._render()

    def _on_channel_enable(self, c: int, on: bool) -> None:
        """!
        @brief Show / hide a channel.
        @param c   Channel.
        @param on  Enabled.
        """
        self.ch[c].enabled = on
        self._update_markers()
        self._render()
        if not on:
            self._fill_meas_row(c, None)

    def _display_values(self, c: int, codes: np.ndarray) -> np.ndarray:
        """!
        @brief Convert codes of a channel to display coordinates (units x gain + offset).
        @param c      Channel.
        @param codes  Signed codes.
        @return float64 array.
        """
        return units.codes_to_units(codes, self.unit, self.uv_per_lsb) * self.ch[c].gain + self._offset_units(c)

    def _update_markers(self) -> None:
        """!
        @brief Reposition ground markers and the trigger line.
        """
        for c in range(4):
            zero: float = units.codes_to_units(np.zeros(1, dtype=np.int32), self.unit, self.uv_per_lsb)[0]
            self.scope.set_offset_marker(c, zero * self.ch[c].gain + self._offset_units(c), self.ch[c].enabled)
        self._update_trigger_line()

    # =====================================================================================
    # Horizontal / trigger
    # =====================================================================================
    def _tb_index(self, tb: float) -> int:
        """!
        @brief Combo index of the timebase closest to a value.
        @param tb  Seconds per division.
        @return Index.
        """
        return int(np.argmin([abs(math.log10(t / tb)) for t in config.TIMEBASES]))

    def _apply_timebase(self) -> None:
        """!
        @brief Apply timebase / trigger position.
        """
        self.acq.timebase = float(self.cmb_tb.currentData())
        self.acq.position = self.spn_pos.value() / 100.0
        self.acq.invalidate()
        if not self.frozen:
            lo, hi = self.acq.x_range()
            self.scope.set_x_view(lo, hi)
        self._update_record_label()

    def _update_record_label(self) -> None:
        """!
        @brief Show record length and limit the timebase list to what the buffer can hold.
        """
        fs: float = self.ring.sample_rate
        model = self.cmb_tb.model()
        tb_max: float = self.acq.max_timebase(fs) if fs > 0 else math.inf
        for i, tb in enumerate(config.TIMEBASES):
            item = model.item(i)
            if item is not None:
                item.setEnabled(tb <= tb_max * 1.0001)
        if fs > 0 and self.acq.timebase > tb_max:
            idx: int = max(i for i, tb in enumerate(config.TIMEBASES) if tb <= tb_max * 1.0001) \
                if any(tb <= tb_max for tb in config.TIMEBASES) else 0
            self.cmb_tb.setCurrentIndex(idx)
            return
        if fs > 0:
            n: int = self.acq.window_samples(fs)
            self.lbl_record.setText(f"Record: {n:,} samples @ {fs / 1e3:,.3f} kS/s "
                                    f"({1e6 / fs:.3f} us/sample)")
        else:
            self.lbl_record.setText("Record: waiting for data")

    def _apply_trigger(self) -> None:
        """!
        @brief Copy the trigger widgets into the acquisition settings.
        """
        t = self.acq.trigger
        mode: TriggerMode = self.cmb_mode.currentData()
        t.source = self.cmb_src.currentIndex()
        t.edge = self.cmb_edge.currentData()
        t.hysteresis_lsb = self.hyst_lsb
        if mode != t.mode:
            t.mode = mode
            if mode == TriggerMode.SINGLE:
                self.acq.arm_single()
                self._set_frozen(False)
        self.acq.invalidate()
        self._update_trigger_line()

    def _show_trigger_spins(self) -> None:
        """!
        @brief Update level / hysteresis spin boxes from the stored LSB values.
        """
        lvl: float = units.level_lsb_to_units(self.acq.trigger.level_lsb, self.unit, self.uv_per_lsb)
        hy: float = units.delta_lsb_to_units(self.hyst_lsb, self.unit, self.uv_per_lsb)
        for sb, val in ((self.spn_level, lvl), (self.spn_hyst, hy)):
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)
        self._update_trigger_line()

    def _on_level_spin(self, value: float) -> None:
        """!
        @brief Trigger level typed in display units.
        @param value  Level.
        """
        self.acq.trigger.level_lsb = units.level_units_to_lsb(value, self.unit, self.uv_per_lsb)
        self.acq.invalidate()
        self._update_trigger_line()

    def _on_hyst_spin(self, value: float) -> None:
        """!
        @brief Noise rejection typed in display units.
        @param value  Hysteresis.
        """
        self.hyst_lsb = abs(units.delta_units_to_lsb(value, self.unit, self.uv_per_lsb))
        self.acq.trigger.hysteresis_lsb = self.hyst_lsb
        self.acq.invalidate()

    def _on_level_dragged(self, y: float) -> None:
        """!
        @brief Trigger line dragged on the screen.
        @param y  Line position in display coordinates.
        """
        src: int = self.acq.trigger.source
        g: float = self.ch[src].gain or 1.0
        level_units: float = (y - self._offset_units(src)) / g
        self.acq.trigger.level_lsb = units.level_units_to_lsb(level_units, self.unit, self.uv_per_lsb)
        self.acq.invalidate()
        self.spn_level.blockSignals(True)
        self.spn_level.setValue(level_units)
        self.spn_level.blockSignals(False)

    def _update_trigger_line(self) -> None:
        """!
        @brief Place the trigger line at the level of the source channel (in display coordinates).
        """
        src: int = self.acq.trigger.source
        lvl: float = units.level_lsb_to_units(self.acq.trigger.level_lsb, self.unit, self.uv_per_lsb)
        y: float = lvl * self.ch[src].gain + self._offset_units(src)
        self.scope.set_trigger_level(y, CHANNEL_COLORS[src], True)

    def _level_50(self) -> None:
        """!
        @brief Set the trigger level to the middle of the source signal.
        """
        src: int = self.acq.trigger.source
        m: Optional[Measurement] = self._last_meas[src] or self._measure_now(src)
        if m is None or not math.isfinite(m.vpp):
            return
        mid_units: float = 0.5 * (m.vmax + m.vmin)
        self.acq.trigger.level_lsb = units.level_units_to_lsb(mid_units, self.unit, self.uv_per_lsb)
        if self.hyst_lsb <= 0 or self.hyst_lsb > units.delta_units_to_lsb(m.vpp, self.unit, self.uv_per_lsb):
            self.hyst_lsb = 0.05 * abs(units.delta_units_to_lsb(m.vpp, self.unit, self.uv_per_lsb))
            self.acq.trigger.hysteresis_lsb = self.hyst_lsb
        self.acq.invalidate()
        self._show_trigger_spins()

    # =====================================================================================
    # Run control
    # =====================================================================================
    def _set_run_button(self) -> None:
        """!
        @brief Rigol-style RUN/STOP button colors.
        """
        if self.frozen:
            self.btn_run.setText("STOP")
            self.btn_run.setStyleSheet(f"background-color: {COLOR_ERROR}; color: white; font-weight: bold;")
        else:
            self.btn_run.setText("RUN")
            self.btn_run.setStyleSheet(f"background-color: {COLOR_OK}; color: white; font-weight: bold;")

    def _set_frozen(self, frozen: bool) -> None:
        """!
        @brief Freeze or resume the display.
        @param frozen  True = keep the current picture.
        """
        self.frozen = frozen
        self.scope.set_frozen(frozen)
        self.btn_reset_view.setVisible(frozen)
        if not frozen:
            lo, hi = self.acq.x_range()
            self.scope.set_x_view(lo, hi)
        self._set_run_button()

    def _toggle_run(self) -> None:
        """!
        @brief RUN/STOP button.
        """
        if self.frozen:
            if self.acq.trigger.mode == TriggerMode.SINGLE:
                self.cmb_mode.setCurrentIndex(self.cmb_mode.findData(TriggerMode.AUTO))
            self._set_frozen(False)
        else:
            self._set_frozen(True)

    def _single(self) -> None:
        """!
        @brief Arm a single acquisition.
        """
        idx: int = self.cmb_mode.findData(TriggerMode.SINGLE)
        if self.cmb_mode.currentIndex() != idx:
            self.cmb_mode.setCurrentIndex(idx)          # arms through _apply_trigger
        else:
            self.acq.arm_single()
            self._set_frozen(False)

    def _reset_x_view(self) -> None:
        """!
        @brief Restore the full record view while frozen.
        """
        lo, hi = self.acq.x_range()
        self.scope.set_x_view(lo, hi)
        self._render()

    def _measure_now(self, c: int) -> Optional[Measurement]:
        """!
        @brief Measure a channel on the newest data, independent of the display state.
        @param c  Channel.
        @return Measurement or None without data.
        """
        epoch, total, fs = self.ring.state()
        n: int = min(total - self.ring.oldest(), int(max(fs * 0.2, 4096)), config.MEASURE_MAX_SAMPLES)
        if fs <= 0 or n < 16:
            return None
        d: Optional[np.ndarray] = self.ring.read(total - n, n, [c], epoch)
        if d is None:
            return None
        return measure(units.codes_to_units(d[0], self.unit, self.uv_per_lsb), fs)

    def _auto_setup(self) -> None:
        """!
        @brief AUTO: choose timebase (~3 periods), trigger level (50 %) and vertical scale.
        """
        src: int = self.acq.trigger.source
        if not self.ch[src].enabled:
            on: List[int] = [c for c in range(4) if self.ch[c].enabled]
            if on:
                src = on[0]
                self.cmb_src.setCurrentIndex(src)
        m: Optional[Measurement] = self._measure_now(src)
        if m is None:
            self.statusBar().showMessage("AUTO: no data")
            return
        self.cmb_mode.setCurrentIndex(self.cmb_mode.findData(TriggerMode.AUTO))
        self.cmb_edge.setCurrentIndex(self.cmb_edge.findData(Edge.RISING))
        if math.isfinite(m.freq) and m.freq > 0:
            want: float = 3.0 / m.freq / config.H_DIVS
            cands: List[int] = [i for i, tb in enumerate(config.TIMEBASES) if tb >= want]
            self.cmb_tb.setCurrentIndex(cands[0] if cands else len(config.TIMEBASES) - 1)
        self._last_meas[src] = m
        self._level_50()
        self._set_frozen(False)
        QtCore.QTimer.singleShot(300, self._auto_y)

    # =====================================================================================
    # Display
    # =====================================================================================
    def _active_channels(self) -> List[int]:
        """!
        @brief Channels that have to be read from the ring (enabled + trigger source).
        @return Sorted channel list.
        """
        chans: set = {c for c in range(4) if self.ch[c].enabled}
        chans.add(self.acq.trigger.source)
        return sorted(chans)

    def _on_display_timer(self) -> None:
        """!
        @brief Acquire and draw; reschedules itself with an adaptive period.
        """
        t0: float = time.perf_counter()
        try:
            if not self.frozen:
                snap: Optional[Snapshot] = self.acq.acquire(self._active_channels())
                if snap is not None:
                    self.snapshot = snap
                    self._render()
                if self.acq.trigger.mode == TriggerMode.SINGLE and self.acq.single_done and snap is not None:
                    self._set_frozen(True)
            self._update_sweep_label()
        finally:
            dt_ms: float = (time.perf_counter() - t0) * 1000.0
            self._render_ms = 0.8 * self._render_ms + 0.2 * dt_ms
            self.t_display.start(int(max(config.DISPLAY_INTERVAL_MS, 1.5 * self._render_ms)))

    def _update_sweep_label(self) -> None:
        """!
        @brief Sweep status text (T'D / AUTO / WAIT / STOP) and info line.
        """
        st: str = SweepStatus.STOP if self.frozen else self.acq.status
        col: str = {SweepStatus.TRIGGERED: COLOR_OK, SweepStatus.AUTO: COLOR_INFO, SweepStatus.WAIT: COLOR_WARN,
                    SweepStatus.STOP: COLOR_ERROR}.get(st, COLOR_IDLE)
        self.lbl_sweep.setText(st)
        self.lbl_sweep.setStyleSheet(f"background-color: {col}; color: white; font-weight: bold; padding: 6px;")
        fs: float = self.ring.sample_rate
        tb: str = units.si_format(self.acq.timebase, "s", 3)
        self.lbl_info.setText(f"  {tb}/div   fs {fs / 1e3:,.3f} kS/s   "
                              f"Trig {self.acq.trigger.mode.value} ch{self.acq.trigger.source} "
                              f"{self.acq.trigger.edge.value}   draw {self._render_ms:.0f} ms")

    def _render(self) -> None:
        """!
        @brief Draw the current snapshot (only the visible part when frozen and zoomed).
        """
        snap: Optional[Snapshot] = self.snapshot
        if snap is None:
            for c in range(4):
                self.scope.set_trace(c, None, None)
            return
        n: int = snap.n
        i0, i1 = 0, n
        if self.frozen:
            lo, hi = self.scope.x_view()
            i0 = int(max(0, math.floor((lo - snap.x0) * snap.fs) - 1))
            i1 = int(min(n, math.ceil((hi - snap.x0) * snap.fs) + 2))
            if i1 - i0 < 2:
                i0, i1 = 0, n
        dx: float = 1.0 / snap.fs
        x0: float = snap.x0 + i0 * dx
        for c in range(4):
            if not self.ch[c].enabled:
                self.scope.set_trace(c, None, None)
                continue
            codes: np.ndarray = snap.data[c, i0:i1]
            if self.unit == UNIT_RAW:
                codes = codes & np.int32(protocol.CODE_MASK)
            x, yc = decimate_minmax(codes, x0, dx, config.DECIMATE_POINTS)
            scale: float = self.uv_per_lsb * 1e-6 if self.unit == UNIT_VOLT else 1.0
            y: np.ndarray = yc * (scale * self.ch[c].gain) + self._offset_units(c)
            self.scope.set_trace(c, x, y)
        if self.chk_gaps.isChecked() and snap.epoch == self.ring.epoch:
            gaps = self.ring.gaps_in(snap.start_abs, snap.start_abs + n)
            self.scope.set_gaps([snap.x0 + (g - snap.start_abs) * dx for g, _s in gaps])
        else:
            self.scope.set_gaps([])
        self.scope.set_overlay(f"{'TRIG' if snap.triggered else 'FREE'}  {n:,} pts")

    # =====================================================================================
    # Measurements
    # =====================================================================================
    def _on_measure_timer(self) -> None:
        """!
        @brief Measure every enabled channel on the displayed record.
        """
        snap: Optional[Snapshot] = self.snapshot
        if snap is None:
            return
        min_vpp: float = units.delta_lsb_to_units(16, self.unit, self.uv_per_lsb)
        for c in range(4):
            if not self.ch[c].enabled:
                self._fill_meas_row(c, None)
                continue
            codes: np.ndarray = snap.data[c][-config.MEASURE_MAX_SAMPLES:]
            m: Measurement = measure(units.codes_to_units(codes, self.unit, self.uv_per_lsb), snap.fs, min_vpp)
            self._last_meas[c] = m
            self._fill_meas_row(c, m)

    def _fill_meas_row(self, c: int, m: Optional[Measurement]) -> None:
        """!
        @brief Write one row of the measurement table.
        @param c  Channel.
        @param m  Measurement (None = channel off).
        """
        if m is None:
            vals: List[str] = ["----"] * len(MEAS_COLUMNS)
        else:
            a = lambda v: units.format_amplitude(v, self.unit)  # noqa: E731
            vals = [a(m.vpp), units.si_format(m.freq, "Hz", 5), units.si_format(m.period, "s", 4),
                    a(m.vmax), a(m.vmin), a(m.vavg), a(m.vrms)]
        for k, txt in enumerate(vals):
            item = self.tbl.item(c, k)
            if item is not None:
                item.setText(txt)

    # =====================================================================================
    # Status (link health, capture, device panel)
    # =====================================================================================
    def _on_status_timer(self) -> None:
        """!
        @brief Periodic refresh of everything that is not drawn on the scope.
        """
        st = self.rx.stats
        ws: WindowStats = self.health.stats()
        state: LinkState = classify(st.connected, self.health.data_age(), ws, self.dev.analog_power)
        if not self.connected:
            state = LinkState.NO_CONNECTION
        color, glyph = _LINK_LOOK[state]
        self.ico_link.set_state(color, glyph)
        self.lbl_link.setText(state.value)
        self.lbl_link.setStyleSheet(status_css(color))
        self.lbl_link_detail.setText(
            f"lost  {ws.lost:,} frames ({ws.loss_pct:.3f} %), {ws.gaps} gaps\n"
            f"recv  {ws.received:,} samples in {ws.span_s:.1f} s\n"
            f"rate  header {st.sample_rate_hz:,} Hz | meas {st.measured_rate / 1e3:,.1f} kS/s\n"
            f"      {st.throughput_bps / 1e6:.2f} MB/s\n"
            f"total {st.total_samples:,} samples, lost {st.total_lost:,}\n"
            f"      batches lost {st.lost_batches}, resync {st.resync_bytes} B")
        conn: List[str] = [f"Stream: {'connected' if st.connected else ('connecting...' if self.connected else 'off')}"]
        if self.connected and self.console_allowed:
            conn.append(f"Console: {'connected' if self.console.connected else 'connecting...'}")
        elif self.connected:
            conn.append("Console: disabled (simulation)")
        if st.last_error and not st.connected and self.connected:
            conn.append(st.last_error)
        self.lbl_conn.setText("\n".join(conn))

        fs: float = self.ring.sample_rate
        if fs != self._last_fs:                    # stream rate changed: adapt timebase limits
            if self._last_fs:
                self.statusBar().showMessage(f"Sample rate changed: {self._last_fs:,.0f} -> {fs:,.0f} Hz")
            self._last_fs = fs
            self.acq.invalidate()
            self._update_record_label()
        self._update_capture_ui()
        self._refresh_device_panel()

    # =====================================================================================
    # Capture
    # =====================================================================================
    def _set_capture_button(self, active: bool) -> None:
        """!
        @brief Capture button look.
        @param active  Capture running.
        """
        if active:
            self.btn_capture.setText("■  STOP CAPTURE")
            self.btn_capture.setStyleSheet(f"background-color: {COLOR_ERROR}; color: white; font-weight: bold;")
        else:
            self.btn_capture.setText("●  CAPTURE")
            self.btn_capture.setStyleSheet(f"color: {COLOR_ERROR}; font-weight: bold;")

    def _toggle_capture(self) -> None:
        """!
        @brief Start or stop a capture.
        """
        if self.capture is not None:
            self._stop_capture()
            return
        if self.conversion is not None:
            QtWidgets.QMessageBox.information(self, "Capture", "The previous capture is still being converted.")
            return
        if not self.connected:
            QtWidgets.QMessageBox.information(self, "Capture", "Connect to a target first.")
            return
        name: str = unique_capture_name(config.CAPTURE_DIR, sanitize_name(self.edt_name.text()))
        if name != sanitize_name(self.edt_name.text()):
            self.statusBar().showMessage(f"File exists - capturing as {name}")
        self.edt_name.setText(name)
        sess: CaptureSession = CaptureSession(config.CAPTURE_DIR, name, self.spn_limit.value() * 1_000_000,
                                              self.chk_keep_raw.isChecked())
        try:
            sess.start()
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Capture", f"Cannot create the capture file:\n{e}")
            return
        self.capture = sess
        self.rx.set_capture(sess)
        self.edt_name.setEnabled(False)
        self.spn_limit.setEnabled(False)
        self._set_capture_button(True)

    def _stop_capture(self) -> None:
        """!
        @brief Stop the active capture and start the CSV conversion.
        """
        sess: Optional[CaptureSession] = self.capture
        if sess is None:
            return
        self.rx.set_capture(None)
        sess.stop()
        self.capture = None
        self._set_capture_button(False)
        self.edt_name.setEnabled(True)
        self.spn_limit.setEnabled(True)
        if sess.error:
            QtWidgets.QMessageBox.critical(self, "Capture", sess.error)
        if sess.stats.samples == 0:
            try:
                os.remove(sess.raw_path)
            except OSError:
                pass
            self.lbl_capture.setText("Capture stopped - no data received, nothing saved.")
            return
        self.conversion = ConversionJob(sess)
        self.conversion.start()
        self.btn_capture.setEnabled(False)

    def _update_capture_ui(self) -> None:
        """!
        @brief Fill meter, auto-stop at the size limit and conversion progress.
        """
        sess: Optional[CaptureSession] = self.capture
        if sess is not None:
            s = sess.stats
            if not sess.active:                       # size limit reached or write error
                self._stop_capture()
                if s.limit_reached:
                    self.statusBar().showMessage("Capture size limit reached")
                return
            dur: float = time.time() - s.started
            self.bar_fill.setValue(int(sess.fill_fraction * 1000))
            self.bar_fill.setFormat(f"{s.csv_bytes / 1e6:,.1f} / {sess.limit_bytes / 1e6:,.0f} MB  "
                                    f"({sess.fill_fraction * 100:.1f} %)")
            self.lbl_capture.setText(f"Recording {sess.name}.csv\n{s.samples:,} samples, {dur:.1f} s, "
                                     f"lost frames {s.lost_frames:,}")
            return
        job: Optional[ConversionJob] = self.conversion
        if job is not None:
            self.bar_fill.setValue(int(job.fraction * 1000))
            self.bar_fill.setFormat(f"converting to CSV {job.fraction * 100:.0f} %")
            if job.done:
                s2: CaptureSession = job.session
                if job.error:
                    QtWidgets.QMessageBox.critical(self, "Capture", f"CSV conversion failed:\n{job.error}\n\n"
                                                                    f"Raw data kept in {s2.raw_path}")
                    self.lbl_capture.setText("Conversion failed - raw file kept.")
                else:
                    self.lbl_capture.setText(
                        f"Saved {os.path.basename(s2.csv_path)}\n{job.rows_done:,} samples, "
                        f"{os.path.getsize(s2.csv_path) / 1e6:,.1f} MB, lost frames {s2.stats.lost_frames:,}")
                    self.statusBar().showMessage(f"Capture saved: {s2.csv_path}")
                    if self.edt_name.text() == s2.name:
                        self.edt_name.setText(next_capture_name(s2.name))
                self.conversion = None
                self.btn_capture.setEnabled(True)
                self.bar_fill.setValue(0)
                self.bar_fill.setFormat("idle")

    def _open_capture_dir(self) -> None:
        """!
        @brief Open the capture folder in the file manager.
        """
        os.makedirs(config.CAPTURE_DIR, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(config.CAPTURE_DIR))

    # =====================================================================================
    # Export / settings / shutdown
    # =====================================================================================
    def _save_png(self) -> None:
        """!
        @brief Save the screen (plot + measurements) as PNG.
        """
        os.makedirs(config.CAPTURE_DIR, exist_ok=True)
        default: str = os.path.join(config.CAPTURE_DIR, f"scope_{time.strftime('%Y%m%d_%H%M%S')}.png")
        path, _f = QtWidgets.QFileDialog.getSaveFileName(self, "Save screen as PNG", default, "PNG image (*.png)")
        if path:
            if not path.lower().endswith(".png"):
                path += ".png"
            if self.center_widget.grab().save(path, "PNG"):
                self.statusBar().showMessage(f"Saved {path}")

    def _fit_to_screen(self, width: int, height: int) -> None:
        """!
        @brief Resize the window to the wanted size, limited to the available screen area.
        @param width   Wanted width in pixels.
        @param height  Wanted height in pixels.
        """
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        if screen is None:
            self.resize(width, height)
            return
        avail: QtCore.QRect = screen.availableGeometry()
        self.resize(min(width, avail.width() - 20), min(height, avail.height() - 40))

    def _restore_settings(self) -> None:
        """!
        @brief Restore the last used settings (QSettings).
        """
        s: QtCore.QSettings = self.settings

        def get(key: str, default: object, typ: type) -> object:
            try:
                v = s.value(key, default)
                return typ(v) if v is not None else default
            except (TypeError, ValueError):
                return default
        self.cmb_target.setCurrentIndex(int(get("target", TARGET_SIM, int)))
        self.edt_ip.setText(str(get("ip", config.DEVICE_HOST, str)))
        self.spn_sport.setValue(int(get("stream_port", config.STREAM_PORT, int)))
        self.spn_cport.setValue(int(get("console_port", config.CONSOLE_PORT, int)))
        self.chk_sim_console.setChecked(str(get("sim_console", "false", str)).lower() == "true")
        self.edt_name.setText(str(get("capture_name", config.CAPTURE_DEFAULT_NAME, str)))
        self.spn_limit.setValue(int(get("capture_limit_mb", self.spn_limit.maximum(), int)))
        self.cmb_unit.blockSignals(True)
        self.cmb_unit.setCurrentIndex(int(get("unit", UNIT_VOLT, int)))
        self.cmb_unit.blockSignals(False)
        self.spn_uv.blockSignals(True)
        self.spn_uv.setValue(float(get("uv_per_lsb", DEFAULT_UV_PER_LSB, float)))
        self.spn_uv.blockSignals(False)
        self.cmb_tb.blockSignals(True)
        self.cmb_tb.setCurrentIndex(self._tb_index(float(get("timebase", config.DEFAULT_TIMEBASE, float))))
        self.cmb_tb.blockSignals(False)
        self.y_center = float(get("y_center", 0.0, float))
        self.y_span = float(get("y_span", 10.0, float))
        self.unit = self.cmb_unit.currentIndex()
        self.uv_per_lsb = self.spn_uv.value()
        self.btn_dev_toggle.setChecked(str(get("device_panel", "true", str)).lower() == "true")
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
            self._fit_to_screen(self.width(), self.height())    # saved on a larger monitor?

    def _save_settings(self) -> None:
        """!
        @brief Persist the user settings.
        """
        s: QtCore.QSettings = self.settings
        s.setValue("target", self.cmb_target.currentIndex())
        s.setValue("ip", self.edt_ip.text())
        s.setValue("stream_port", self.spn_sport.value())
        s.setValue("console_port", self.spn_cport.value())
        s.setValue("sim_console", "true" if self.chk_sim_console.isChecked() else "false")
        s.setValue("capture_name", self.edt_name.text())
        s.setValue("capture_limit_mb", self.spn_limit.value())
        s.setValue("unit", self.cmb_unit.currentIndex())
        s.setValue("uv_per_lsb", self.spn_uv.value())
        s.setValue("timebase", float(self.cmb_tb.currentData()))
        s.setValue("y_center", self.y_center)
        s.setValue("y_span", self.y_span)
        s.setValue("device_panel", "true" if self.btn_dev_toggle.isChecked() else "false")
        s.setValue("geometry", self.saveGeometry())

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:
        """!
        @brief Finish a running capture / conversion, stop threads, save settings.
        @param ev  Close event.
        """
        if self.capture is not None:
            self._stop_capture()
        if self.conversion is not None and not self.conversion.done:
            if confirm(self, "Quit", "A capture is still being converted to CSV.",
                       "Yes = wait for the conversion to finish, No = quit now (the raw .bin file is kept)."):
                dlg: QtWidgets.QProgressDialog = QtWidgets.QProgressDialog("Converting capture to CSV...", "Cancel",
                                                                           0, 1000, self)
                dlg.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
                dlg.show()
                while not self.conversion.done:
                    if dlg.wasCanceled():
                        self.conversion.cancel()
                    dlg.setValue(int(self.conversion.fraction * 1000))
                    QtWidgets.QApplication.processEvents()
                    self.conversion.wait(0.1)
                dlg.close()
            else:
                self.conversion.cancel()
                self.conversion.wait(5.0)
        self._save_settings()
        self.rx.stop()
        self.console.stop()
        super().closeEvent(ev)
