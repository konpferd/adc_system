"""!
@file device_panel.py
@brief Device control panel: analog power, START/STOP, PGA gains, ADC configuration and the
       raw console (TCP port 5000).

The panel never talks to the network itself: it emits sigSend / sigSendChain with console
command strings and is refreshed by the main window through update_state().
"""
from __future__ import annotations

import html
from typing import List, Optional

from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from adc_common import ads127l14 as ads
from adc_common.console_protocol import LineKind
from adc_common.style import (CHANNEL_COLORS, COLOR_ERROR, COLOR_IDLE, COLOR_INFO, COLOR_OK, COLOR_WARN,
                              PANEL_MIN_WIDTH, PANEL_WIDTH, status_css)

from ..config import TEMP_DEFAULT_WARN_C, TEMP_NEAR_MARGIN_C
from ..device_state import DeviceState
from .widgets import CollapsibleBox, HexSpinBox, color_swatch, confirm

CONSOLE_MAX_BLOCKS: int = 5000

## Console view colors: fixed dark background so the text stays readable in light and dark OS themes
CONSOLE_BG: str = "#1e1e1e"
CONSOLE_TX_COLOR: str = "#64b5f6"           #!< commands sent by this application
_KIND_COLORS = {LineKind.OK: "#81c784", LineKind.ERR: "#ef5350", LineKind.FAULT: "#ef5350",
                LineKind.WARNING: "#ffb74d", LineKind.DETAIL: "#e57373", LineKind.INFO: "#e0e0e0"}


class DevicePanel(QtWidgets.QScrollArea):
    """!
    @brief Right-hand control panel for the real device.
    """

    sigSend = QtCore.Signal(str)                 #!< single console command
    sigSendChain = QtCore.Signal(list, str)      #!< commands executed in order, abort on error; title

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        """!
        @brief Build all controls (disabled until a console connects).
        @param parent  Parent widget.
        """
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFixedWidth(PANEL_WIDTH)
        self._state: DeviceState = DeviceState()
        self._history: List[str] = []
        self._hist_pos: int = 0

        panel: QtWidgets.QWidget = QtWidgets.QWidget()
        panel.setMinimumWidth(PANEL_MIN_WIDTH)
        lay: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(panel)
        self.setWidget(panel)

        self.lbl_disabled: QtWidgets.QLabel = QtWidgets.QLabel()
        self.lbl_disabled.setWordWrap(True)
        self.lbl_disabled.setStyleSheet(f"color: {COLOR_IDLE}; font-style: italic; padding: 4px;"
                                        f"background: #eeeeee; border: 1px solid #cccccc;")
        lay.addWidget(self.lbl_disabled)

        self.body: QtWidgets.QWidget = QtWidgets.QWidget()
        body: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.body)

        # ---- analog power + run control
        self.btn_power: QtWidgets.QPushButton = QtWidgets.QPushButton()
        self.btn_power.setMinimumHeight(64)
        self.btn_power.clicked.connect(self._on_power)
        body.addWidget(self.btn_power)

        # ---- Zynq die temperature (DEVICE_TEMP, polled by the main window)
        g = QtWidgets.QGroupBox("Device temperature (Zynq XADC)")
        tv: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout(g)
        self.lbl_temp: QtWidgets.QLabel = QtWidgets.QLabel("--")
        self.lbl_temp_detail: QtWidgets.QLabel = QtWidgets.QLabel("")
        self.lbl_temp_detail.setFont(QtGui.QFont("Monospace", 8))
        tv.addWidget(self.lbl_temp)
        tv.addWidget(self.lbl_temp_detail, 1)
        body.addWidget(g)

        g: QtWidgets.QGroupBox = QtWidgets.QGroupBox("Acquisition")
        v: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(g)
        row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        self.btn_start: QtWidgets.QPushButton = QtWidgets.QPushButton("▶  START")
        self.btn_stop: QtWidgets.QPushButton = QtWidgets.QPushButton("■  STOP")
        for b in (self.btn_start, self.btn_stop):
            b.setMinimumHeight(34)
        self.btn_start.setStyleSheet(f"font-weight: bold; color: {COLOR_OK};")
        self.btn_stop.setStyleSheet(f"font-weight: bold; color: {COLOR_ERROR};")
        self.btn_start.clicked.connect(lambda: self.sigSend.emit("ADC START"))
        self.btn_stop.clicked.connect(lambda: self.sigSend.emit("ADC STOP"))
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        v.addLayout(row)
        self.lbl_run: QtWidgets.QLabel = QtWidgets.QLabel("-")
        v.addWidget(self.lbl_run)
        body.addWidget(g)

        # ---- PGA
        g = QtWidgets.QGroupBox("PGA gain (digital potentiometer code)")
        self.grp_pga: QtWidgets.QGroupBox = g
        grid: QtWidgets.QGridLayout = QtWidgets.QGridLayout(g)
        grid.addWidget(QtWidgets.QLabel("Ch"), 0, 0, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Set"), 0, 2)
        grid.addWidget(QtWidgets.QLabel("Device / EEPROM"), 0, 3, 1, 2)
        self.spn_gain: List[HexSpinBox] = []
        self.sld_gain: List[QtWidgets.QSlider] = []
        self.lbl_gain: List[QtWidgets.QLabel] = []
        for c in range(4):
            r: int = 1 + 2 * c
            grid.addWidget(color_swatch(CHANNEL_COLORS[c]), r, 0)
            grid.addWidget(QtWidgets.QLabel(f"ch{c}"), r, 1)
            sp: HexSpinBox = HexSpinBox(ads_max_code())
            sp.setValue(0x40)
            sl: QtWidgets.QSlider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            sl.setRange(0, ads_max_code())
            sl.setValue(0x40)
            sp.valueChanged.connect(sl.setValue)
            sl.valueChanged.connect(sp.setValue)
            sp.valueChanged.connect(self._mark_pending)
            lb: QtWidgets.QLabel = QtWidgets.QLabel("-- / --")
            lb.setFont(QtGui.QFont("Monospace", 9))
            grid.addWidget(sp, r, 2)
            grid.addWidget(lb, r, 3, 1, 2)
            grid.addWidget(sl, r + 1, 1, 1, 4)
            self.spn_gain.append(sp)
            self.sld_gain.append(sl)
            self.lbl_gain.append(lb)
        row = QtWidgets.QHBoxLayout()
        self.btn_gain_apply: QtWidgets.QPushButton = QtWidgets.QPushButton("Apply")
        self.btn_gain_apply.setToolTip("PGA SET_GAIN <ch0> <ch1> <ch2> <ch3>")
        self.btn_gain_apply.clicked.connect(self._apply_gain)
        btn_read: QtWidgets.QPushButton = QtWidgets.QPushButton("Read")
        btn_read.setToolTip("PGA DISPLAY_SETTINGS (wipers + EEPROM)")
        btn_read.clicked.connect(lambda: self.sigSend.emit("PGA DISPLAY_SETTINGS"))
        btn_save: QtWidgets.QPushButton = QtWidgets.QPushButton("Save EEPROM")
        btn_save.clicked.connect(self._save_eeprom)
        btn_rst: QtWidgets.QPushButton = QtWidgets.QPushButton("Reset")
        btn_rst.setToolTip("PGA RESET: hardware reset, wipers reload from EEPROM")
        btn_rst.clicked.connect(self._reset_pga)
        for b in (self.btn_gain_apply, btn_read, btn_save, btn_rst):
            row.addWidget(b)
        grid.addLayout(row, 9, 0, 1, 5)
        body.addWidget(g)
        self.lbl_pga_power: QtWidgets.QLabel = QtWidgets.QLabel("PGA needs analog power - switch ANALOG POWER on.")
        self.lbl_pga_power.setWordWrap(True)
        self.lbl_pga_power.setStyleSheet(f"color: {COLOR_WARN}; font-style: italic;")
        body.addWidget(self.lbl_pga_power)

        # ---- ADC configuration (collapsible)
        self.box_adc: CollapsibleBox = CollapsibleBox("ADC configuration (ADS127L14)", expanded=False)
        av: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(self.box_adc.content)
        av.setContentsMargins(4, 0, 0, 0)
        self.lbl_adc: QtWidgets.QLabel = QtWidgets.QLabel("not read yet")
        self.lbl_adc.setFont(QtGui.QFont("Monospace", 8))
        self.lbl_adc.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_adc.setWordWrap(True)
        av.addWidget(self.lbl_adc)
        row = QtWidgets.QHBoxLayout()
        for text, cmd in (("Refresh", "ADC DISPLAY_SETTINGS"), ("Read ID", "ADC READ_ID"),
                          ("Status", "ADC STATUS"), ("Clear status", "ADC CLEAR_STATUS")):
            b = QtWidgets.QPushButton(text)
            b.setToolTip(cmd)
            b.clicked.connect(lambda _=False, c=cmd: self.sigSend.emit(c))
            row.addWidget(b)
        av.addLayout(row)

        g = QtWidgets.QGroupBox("Reconfigure")
        f: QtWidgets.QFormLayout = QtWidgets.QFormLayout(g)
        self.cmb_speed: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for i, n in enumerate(ads.SPEED_NAMES):
            self.cmb_speed.addItem(f"{i}: {n} (fCLK ≤ {ads.SPEED_FCLK_MAX_HZ[i] / 1e6:.2f} MHz)", i)
        self.cmb_clk: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for d in ads.CLK_DIV_VALUES:
            self.cmb_clk.addItem(f"/{d}  (fCLK {ads.FCLK_INT_HZ / d / 1e6:.3f} MHz)", d)
        self.cmb_dclk: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for d in ads.DCLK_DIV_VALUES:
            self.cmb_dclk.addItem(f"/{d}  (DCLK {ads.FCLK_INT_HZ / d / 1e6:.3f} MHz)", d)
        self.cmb_fltr: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.cmb_fltr.setMaxVisibleItems(32)
        for i in range(32):
            self.cmb_fltr.addItem(f"{i}: {ads.filter_description(i)}", i)
        f.addRow("Speed mode", self.cmb_speed)
        f.addRow("CLK divider", self.cmb_clk)
        f.addRow("DCLK divider", self.cmb_dclk)
        f.addRow("Filter (FLTR)", self.cmb_fltr)
        self.lbl_preview: QtWidgets.QLabel = QtWidgets.QLabel("")
        self.lbl_preview.setWordWrap(True)
        f.addRow(self.lbl_preview)
        row = QtWidgets.QHBoxLayout()
        self.btn_cfg_apply: QtWidgets.QPushButton = QtWidgets.QPushButton("Apply configuration...")
        self.btn_cfg_apply.clicked.connect(self._apply_config)
        btn_revert: QtWidgets.QPushButton = QtWidgets.QPushButton("Revert")
        btn_revert.clicked.connect(self._load_config_widgets)
        btn_adc_reset: QtWidgets.QPushButton = QtWidgets.QPushButton("ADC reset...")
        btn_adc_reset.setToolTip("ADC RESET: hardware reset + default configuration")
        btn_adc_reset.clicked.connect(self._reset_adc)
        row.addWidget(self.btn_cfg_apply)
        row.addWidget(btn_revert)
        row.addWidget(btn_adc_reset)
        f.addRow(row)
        for cmb in (self.cmb_speed, self.cmb_clk, self.cmb_dclk, self.cmb_fltr):
            cmb.currentIndexChanged.connect(self._update_preview)
        av.addWidget(g)
        body.addWidget(self.box_adc)

        # ---- console (collapsible)
        self.box_console: CollapsibleBox = CollapsibleBox("Console (raw, port 5000)", expanded=False)
        cv: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(self.box_console.content)
        cv.setContentsMargins(4, 0, 0, 0)
        self.txt_console: QtWidgets.QPlainTextEdit = QtWidgets.QPlainTextEdit()
        self.txt_console.setReadOnly(True)
        self.txt_console.setMaximumBlockCount(CONSOLE_MAX_BLOCKS)
        self.txt_console.setFont(QtGui.QFont("Monospace", 8))
        self.txt_console.setStyleSheet(f"QPlainTextEdit {{ background-color: {CONSOLE_BG}; color: #e0e0e0; }}")
        self.txt_console.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.txt_console.setMinimumHeight(260)
        cv.addWidget(self.txt_console)
        row = QtWidgets.QHBoxLayout()
        self.edt_cmd: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.edt_cmd.setPlaceholderText("command, e.g. HELP  (Up/Down = history)")
        self.edt_cmd.returnPressed.connect(self._send_manual)
        self.edt_cmd.installEventFilter(self)
        btn_send: QtWidgets.QPushButton = QtWidgets.QPushButton("Send")
        btn_send.clicked.connect(self._send_manual)
        row.addWidget(self.edt_cmd, 1)
        row.addWidget(btn_send)
        cv.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.chk_polls: QtWidgets.QCheckBox = QtWidgets.QCheckBox("Show background polls (DEVICE_TEMP)")
        self.chk_polls.setChecked(False)
        btn_help: QtWidgets.QPushButton = QtWidgets.QPushButton("HELP")
        btn_help.clicked.connect(lambda: self.sigSend.emit("HELP"))
        btn_ch: QtWidgets.QPushButton = QtWidgets.QPushButton("CH_STATUS")
        btn_ch.clicked.connect(lambda: self.sigSend.emit("CH_STATUS"))
        btn_clear: QtWidgets.QPushButton = QtWidgets.QPushButton("Clear")
        btn_clear.clicked.connect(self.txt_console.clear)
        cv.addWidget(self.chk_polls)
        row.addWidget(btn_help)
        row.addWidget(btn_ch)
        row.addWidget(btn_clear)
        cv.addLayout(row)
        body.addWidget(self.box_console)
        lay.addStretch(1)

        self._load_config_widgets()
        self.update_state(self._state, None)
        self.set_available(False, "Not connected.")

    # ---- availability ------------------------------------------------------------------------
    def set_available(self, available: bool, reason: str = "") -> None:
        """!
        @brief Enable or gray out every device control.
        @param available  Console connected and allowed.
        @param reason     Message shown while disabled.
        """
        self.body.setEnabled(available)
        self.lbl_disabled.setVisible(not available)
        self.lbl_disabled.setText(reason)

    # ---- state -> widgets --------------------------------------------------------------------------
    def update_state(self, st: DeviceState, data_flowing: Optional[bool]) -> None:
        """!
        @brief Refresh indicators from the device model.
        @param st            Device state.
        @param data_flowing  Stream currently delivering data (None = stream not connected).
        """
        self._state = st
        pwr: Optional[bool] = st.analog_power
        if pwr is None:
            txt, col = "ANALOG POWER: UNKNOWN\n(click to switch ON)", COLOR_WARN
        elif pwr:
            txt, col = "ANALOG POWER: ON\n(click to switch OFF)", COLOR_OK
        else:
            txt, col = "ANALOG POWER: OFF\n(click to switch ON)", "#757575"
        self.btn_power.setText(txt)
        self.btn_power.setStyleSheet(
            f"QPushButton {{ background-color: {col}; color: white; font-weight: bold; font-size: 12pt;"
            f" border-radius: 6px; }} QPushButton:disabled {{ background-color: #bdbdbd; }}")
        run_cmd: str = {None: "unknown", True: "START", False: "STOP"}[st.adc_running]
        flow: str = {None: "stream not connected", True: "data flowing", False: "no data"}[data_flowing]
        color: str = COLOR_OK if data_flowing else (COLOR_IDLE if data_flowing is None else COLOR_WARN)
        self.lbl_run.setText(f"Last command: {run_cmd}   |   Stream: {flow}")
        self.lbl_run.setStyleSheet(f"color: {color};")
        for c in range(4):
            dev: str = f"0x{st.pga_rdac[c]:02X}" if st.pga_rdac else "--"
            ee: str = f"0x{st.pga_eeprom[c]:02X}" if st.pga_eeprom else "--"
            self.lbl_gain[c].setText(f"{dev} / {ee}")
        self.grp_pga.setEnabled(st.pga_available)
        self.lbl_pga_power.setVisible(not st.pga_available)
        self._mark_pending()
        self._show_adc()
        self._show_temp()

    def _show_temp(self) -> None:
        """!
        @brief Show the last DEVICE_TEMP reading, colored against the device's warning level.
        """
        t = self._state.temp
        if t is None:
            self.lbl_temp.setText("--")
            self.lbl_temp.setStyleSheet(status_css(COLOR_IDLE))
            self.lbl_temp_detail.setText("")
            return
        warn: float = t.warn_level if t.warn_level is not None else TEMP_DEFAULT_WARN_C
        if t.now >= warn:
            color: str = COLOR_ERROR
        elif t.now >= warn - TEMP_NEAR_MARGIN_C:
            color = COLOR_WARN
        else:
            color = COLOR_OK
        self.lbl_temp.setText(f"{t.now:.1f} \u00B0C")
        self.lbl_temp.setStyleSheet(status_css(color))
        self.lbl_temp_detail.setText(f"min {t.min:.1f}  max {t.max:.1f}\nwarning at {warn:.0f} \u00B0C")

    def load_gains_from_device(self) -> None:
        """!
        @brief Copy the device wiper codes into the edit boxes.
        """
        if not self._state.pga_rdac:
            return
        for c, sp in enumerate(self.spn_gain):
            sp.blockSignals(True)
            sp.setValue(self._state.pga_rdac[c])
            sp.blockSignals(False)
            self.sld_gain[c].blockSignals(True)
            self.sld_gain[c].setValue(self._state.pga_rdac[c])
            self.sld_gain[c].blockSignals(False)
        self._mark_pending()

    def _mark_pending(self, *_a: object) -> None:
        """!
        @brief Highlight gain boxes whose value differs from the device.
        @param _a  Ignored signal arguments.
        """
        dev: Optional[List[int]] = self._state.pga_rdac
        for c, sp in enumerate(self.spn_gain):
            pending: bool = dev is not None and sp.value() != dev[c]
            sp.setStyleSheet("background-color: #fff3c4;" if pending else "")

    def _show_adc(self) -> None:
        """!
        @brief Render the parsed DISPLAY_SETTINGS report.
        """
        a = self._state.adc
        st: DeviceState = self._state
        if a is None:
            txt: str = "Configuration not read yet (Refresh)."
            if st.adc_status is not None:
                txt += f"\nStatus: 0x{st.adc_status:02X}"
            self.lbl_adc.setText(txt)
            return
        c: ads.AdcConfig = a.config
        lines: List[str] = [
            f"ADC           : {'enabled' if a.enabled else 'DISABLED'}",
            f"ID / rev      : 0x{(st.device_id or 0):02X} / 0x{(st.rev_id or 0):02X}",
            f"Status        : 0x{(st.adc_status if st.adc_status is not None else a.status):02X}",
            f"Speed mode    : {ads.SPEED_NAMES[c.speed_mode]} ({c.speed_mode})",
            f"Clock         : {a.clock_source}, /{c.clk_div} -> fCLK {a.fclk_hz / 1e6:.3f} MHz",
            f"Filter        : {a.filter_text} (FLTR={c.fltr})",
            f"Sample rate   : {a.sample_rate_hz:,.3f} Hz",
            f"DCLK          : /{c.dclk_div} -> {a.fdclk_hz / 1e6:.3f} MHz, {a.dclk_per_frame} per frame",
            f"Channels      : {'identical' if a.channels_identical else 'MISMATCH'}",
        ]
        text: str = "\n".join(lines)
        if a.warnings:
            text += "\n" + "\n".join(a.warnings)
        self.lbl_adc.setText(text)
        self.lbl_adc.setStyleSheet(f"color: {COLOR_ERROR};" if (a.warnings or not a.channels_identical) else "")

    def load_config_from_device(self) -> None:
        """!
        @brief Re-synchronise the reconfiguration combos with the device (after a refresh).
        """
        self._load_config_widgets()

    def _load_config_widgets(self) -> None:
        """!
        @brief Set the reconfiguration combos to the active device configuration.
        """
        c: ads.AdcConfig = self._state.adc.config if self._state.adc else ads.AdcConfig()
        for cmb, val in ((self.cmb_speed, c.speed_mode), (self.cmb_clk, c.clk_div),
                         (self.cmb_dclk, c.dclk_div), (self.cmb_fltr, c.fltr)):
            cmb.blockSignals(True)
            cmb.setCurrentIndex(max(0, cmb.findData(val)))
            cmb.blockSignals(False)
        self._update_preview()

    def config_from_widgets(self) -> ads.AdcConfig:
        """!
        @brief Configuration selected in the combos.
        @return AdcConfig.
        """
        return ads.AdcConfig(speed_mode=int(self.cmb_speed.currentData()), clk_div=int(self.cmb_clk.currentData()),
                             dclk_div=int(self.cmb_dclk.currentData()), fltr=int(self.cmb_fltr.currentData()))

    def _update_preview(self, *_a: object) -> None:
        """!
        @brief Show the resulting sample rate and any limit violation of the selection.
        @param _a  Ignored signal arguments.
        """
        new: ads.AdcConfig = self.config_from_widgets()
        txt: str = f"Resulting sample rate: <b>{new.rate_mhz() / 1000:,.3f} Hz</b>"
        warn: List[str] = new.warnings()
        if warn:
            txt += "<br>" + "<br>".join(f"<span style='color:{COLOR_ERROR}'>⚠ {html.escape(w)}</span>"
                                        for w in warn)
        self.lbl_preview.setText(txt)

    # ---- console view --------------------------------------------------------------------------
    def append_console(self, text: str, kind: LineKind = LineKind.INFO, outgoing: bool = False,
                       quiet: bool = False) -> None:
        """!
        @brief Add one line to the console view.
        @param text      Line text.
        @param kind      Classification (for the color).
        @param outgoing  True for commands sent by this application.
        @param quiet     Line of a background poll; shown only when "Show background polls" is ticked.
        """
        if quiet and not self.chk_polls.isChecked():
            return
        if outgoing:
            h: str = f"<span style='color:{CONSOLE_TX_COLOR}; font-weight:bold'>&gt; {html.escape(text)}</span>"
        else:
            h = f"<span style='color:{_KIND_COLORS.get(kind, '#e0e0e0')}'>{html.escape(text).replace(chr(9), '    ')}</span>"
        self.txt_console.appendHtml(h)

    def eventFilter(self, obj: QtCore.QObject, ev: QtCore.QEvent) -> bool:
        """!
        @brief Up/Down command history in the input line.
        @param obj  Watched object.
        @param ev   Event.
        @return True when the event was consumed.
        """
        if obj is getattr(self, "edt_cmd", None) and ev.type() == QtCore.QEvent.Type.KeyPress and self._history:
            key: int = ev.key()  # type: ignore[attr-defined]
            if key == QtCore.Qt.Key.Key_Up:
                self._hist_pos = max(0, self._hist_pos - 1)
                self.edt_cmd.setText(self._history[self._hist_pos])
                return True
            if key == QtCore.Qt.Key.Key_Down:
                self._hist_pos = min(len(self._history), self._hist_pos + 1)
                self.edt_cmd.setText(self._history[self._hist_pos] if self._hist_pos < len(self._history) else "")
                return True
        return super().eventFilter(obj, ev)

    # ---- actions ---------------------------------------------------------------------------------
    def _send_manual(self) -> None:
        """!
        @brief Send the text typed into the console input.
        """
        cmd: str = self.edt_cmd.text().strip()
        if not cmd:
            return
        if not self._history or self._history[-1] != cmd:
            self._history.append(cmd)
        self._hist_pos = len(self._history)
        self.edt_cmd.clear()
        self.sigSend.emit(cmd)

    def _on_power(self) -> None:
        """!
        @brief Toggle analog power (switching off asks for confirmation).
        """
        if self._state.analog_power:
            if not confirm(self, "Analog power", "Switch the <b>analog power OFF</b>?",
                           "The analog front end and the ADC inputs lose their supply; the measurement "
                           "will read ~0 until power is switched on again."):
                return
            self.sigSend.emit("ANALOG_PWR OFF")
        else:
            self.sigSend.emit("ANALOG_PWR ON")

    def _apply_gain(self) -> None:
        """!
        @brief Send PGA SET_GAIN with the four edited codes.
        """
        codes: str = " ".join(f"0x{sp.value():02X}" for sp in self.spn_gain)
        self.sigSend.emit(f"PGA SET_GAIN {codes}")

    def _save_eeprom(self) -> None:
        """!
        @brief Store the current wipers in the potentiometer EEPROM (confirmed).
        """
        if confirm(self, "PGA EEPROM", "Store the current PGA wiper codes in the EEPROM?",
                   "They become the power-on / reset values of the digital potentiometers."):
            self.sigSendChain.emit(["PGA SAVE_EEPROM", "PGA DISPLAY_SETTINGS"], "PGA save to EEPROM")

    def _reset_pga(self) -> None:
        """!
        @brief Hardware reset of the potentiometers (confirmed).
        """
        if confirm(self, "PGA reset", "Reset the PGA potentiometers?",
                   "The wipers reload the values stored in EEPROM."):
            self.sigSendChain.emit(["PGA RESET", "PGA DISPLAY_SETTINGS"], "PGA reset")

    def _reset_adc(self) -> None:
        """!
        @brief Hardware reset of the ADC with default configuration (confirmed).
        """
        if confirm(self, "ADC reset", "<b>Reset the ADC</b> and load the default configuration?",
                   "Streaming is interrupted, the sample rate returns to the firmware default."):
            self.sigSendChain.emit(["ADC RESET", "ADC DISPLAY_SETTINGS"], "ADC reset")

    def _apply_config(self) -> None:
        """!
        @brief Send the changed ADC settings after a confirmation listing every change.
        """
        cur: ads.AdcConfig = self._state.adc.config if self._state.adc else ads.AdcConfig()
        new: ads.AdcConfig = self.config_from_widgets()
        cmds: List[str] = new.command_sequence(cur)
        if not cmds:
            QtWidgets.QMessageBox.information(self, "ADC configuration", "Nothing to change.")
            return
        rows: List[str] = []
        for name, a, b in (("Speed mode", f"{cur.speed_mode} ({ads.SPEED_NAMES[cur.speed_mode]})",
                            f"{new.speed_mode} ({ads.SPEED_NAMES[new.speed_mode]})"),
                           ("CLK divider", f"/{cur.clk_div}", f"/{new.clk_div}"),
                           ("DCLK divider", f"/{cur.dclk_div}", f"/{new.dclk_div}"),
                           ("Filter", f"{cur.fltr}: {ads.filter_description(cur.fltr)}",
                            f"{new.fltr}: {ads.filter_description(new.fltr)}"),
                           ("Sample rate", f"{cur.rate_mhz() / 1000:,.3f} Hz", f"{new.rate_mhz() / 1000:,.3f} Hz")):
            mark: str = " style='font-weight:bold'" if a != b else ""
            rows.append(f"<tr{mark}><td>{name}</td><td>{html.escape(a)}</td><td>→</td><td>{html.escape(b)}</td></tr>")
        warn: List[str] = new.warnings()
        detail: str = "Commands: " + "; ".join(cmds)
        if warn:
            detail += "\n\nWARNING:\n" + "\n".join(warn)
        text: str = ("<b>Change the ADC configuration?</b><br>The stream sample rate may change and "
                     "running captures will contain the transition.<table>" + "".join(rows) + "</table>")
        if confirm(self, "ADC configuration", text, detail):
            self.sigSendChain.emit(cmds + ["ADC DISPLAY_SETTINGS"], "ADC configuration")


def ads_max_code() -> int:
    """!
    @brief Largest PGA wiper code (PGA_SPI_MAX_CODE).
    @return 0x7F.
    """
    return 0x7F
