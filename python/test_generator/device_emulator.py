"""!
@file device_emulator.py
@brief Emulation of the device control console (console.c) for the virtual ADC.

Implements the firmware command table on TCP port 5000 with the same texts, prompts,
start-up banner, broadcast logging and error codes, plus the state those commands control:

  - analog power (ANALOG_PWR ON/OFF): with power off the simulated inputs read ~0
  - ADC run request (ADC START/STOP/RESET, ANALOG_PWR OFF): frames are only streamed while
    sampling is requested and a client is connected. Every start resets the DMA and drops
    the half-filled buffer, so hw_frame_ctr jumps once, as on the real board
  - DEVICE_TEMP: simulated Zynq die temperature with min/max tracking and the firmware's
    warning level (DEVICE_TEMP_C_WRN_LVL); an override can be set through the HTTP API
  - PGA wiper codes / EEPROM (PGA ...): the code scales the simulated amplitude
    (code / 0x40), a simulation-only model so gain changes are visible in the data.
    The potentiometers need analog power: without it SET_GAIN fails with -4 and
    switching power on reloads the wipers from EEPROM. DISPLAY_SETTINGS lists the
    codes by ADC channel, like SET_GAIN / READ_GAIN.
  - ADC configuration (ADC SET ...): FLTR and CLK_DIV change the stream sample rate using
    the same formula as the firmware

The emulator is used by adc_simulator.py; it has no Qt dependency.
"""
from __future__ import annotations

import os
import select
import socket
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adc_common import ads127l14 as ads                                    # noqa: E402
from adc_common.console_protocol import (EOL, PROMPT, format_adc_settings,  # noqa: E402
                                         format_device_temp_reply, format_gain_reply, format_pga_settings)

CONSOLE_MAX_CLIENTS: int = 4          #!< CONSOLE_MAX_CLIENTS in console.h
IDLE_FLUSH_S: float = 0.2             #!< IDLE_FLUSH_MS in console.c
RX_BUF_SIZE: int = 256                #!< CONSOLE_RX_BUF_SIZE
MAX_ARGS: int = 8                     #!< CONSOLE_MAX_ARGS
DMA_TIMEOUT_S: float = 2.5            #!< semaphore timeout in adc_hw_read()
TEMP_WARN_LEVEL_C: int = 75           #!< DEVICE_TEMP_C_WRN_LVL in console.h
TEMP_BASE_MC: int = 42000             #!< simulated idle die temperature, milli-degrees C
TEMP_RUN_RISE_MC: int = 3000          #!< extra heat while sampling
RESTART_DROP_FRAMES: int = 211        #!< frames lost when a (re)start drops the partial DMA buffer
PGA_DEFAULT_CODE: int = 0x40
PGA_UNITY_CODE: int = 0x40            #!< simulation model: amplitude x (code / 0x40)
DEVICE_ID: int = 0x04
REV_ID: int = 0x00

PGA_ERR_VERIFY: int = -4
ADS_OK: int = 0
ADS_ERR_PARAM: int = -2
ADS_ERR_CLK_LIMIT: int = -5


def c_strtol(text: str, base: int) -> Optional[int]:
    """!
    @brief Parse an integer like C strtol() with full-string validation (*end == '\\0').
    @param text  Token to parse.
    @param base  0 (auto: 0x hex, leading 0 octal, else decimal) or 10.
    @return The value, or None when the token is not a complete number.
    """
    s: str = text.strip()
    if not s:
        return None
    sign: int = 1
    if s[0] in "+-":
        sign = -1 if s[0] == "-" else 1
        s = s[1:]
    try:
        if base == 0:
            if s.lower().startswith("0x"):
                return sign * int(s[2:], 16)
            if len(s) > 1 and s.startswith("0"):
                return sign * int(s[1:], 8)
            return sign * int(s, 10)
        return sign * int(s, base)
    except ValueError:
        return None


class DeviceEmulator:
    """!
    @brief State of the emulated board plus the console command handlers.
    """

    def __init__(self, boot_power: bool = True, boot_running: bool = True,
                 on_rate_change: Optional[Callable[[int], None]] = None) -> None:
        """!
        @brief Create the device in its post-boot state.
        @param boot_power      Analog power state after boot.
        @param boot_running    ADC run state after boot.
        @param on_rate_change  Called with the new sample rate when FLTR / CLK_DIV change it.
        """
        self.lock: threading.RLock = threading.RLock()
        self.analog_power: bool = boot_power
        self.running: bool = boot_running
        self.pga_rdac: List[int] = [PGA_DEFAULT_CODE] * 4
        self.pga_eeprom: List[int] = [PGA_DEFAULT_CODE] * 4
        self.adc_cfg: ads.AdcConfig = ads.AdcConfig()
        self.adc_status: int = 0x00
        self.on_rate_change: Optional[Callable[[int], None]] = on_rate_change
        self._clients: List[socket.socket] = []
        self._clients_lock: threading.Lock = threading.Lock()
        self.commands_handled: int = 0
        self.restart_epoch: int = 0                 #!< bumped by every START (stream restarts the DMA)
        self.temp_override_c: Optional[float] = None
        self._t0: float = time.monotonic()
        self._temp_min_mc: Optional[int] = None
        self._temp_max_mc: Optional[int] = None

    # ---- shared state used by the stream server -------------------------------------
    def channel_gains(self) -> List[float]:
        """!
        @brief Amplitude factor of each channel derived from the PGA codes.
        @return Four factors (code / 0x40), 0 when analog power is off.
        """
        with self.lock:
            if not self.analog_power:
                return [0.0] * 4
            return [c / PGA_UNITY_CODE for c in self.pga_rdac]

    def snapshot(self) -> Dict[str, object]:
        """!
        @brief JSON-friendly copy of the device state (for the HTTP API and the GUI).
        @return Dictionary of state values.
        """
        with self.lock:
            return {"analog_power": self.analog_power, "running": self.running,
                    "pga_rdac": list(self.pga_rdac), "pga_eeprom": list(self.pga_eeprom),
                    "adc": {"speed_mode": self.adc_cfg.speed_mode, "clk_div": self.adc_cfg.clk_div,
                            "dclk_div": self.adc_cfg.dclk_div, "fltr": self.adc_cfg.fltr,
                            "rate_hz": self.adc_cfg.rate_hz(), "status": self.adc_status},
                    "console_clients": self.client_count(), "temp_c": self.temperature_mc() / 1000.0,
                    "temp_override_c": self.temp_override_c}

    def temperature_mc(self) -> int:
        """!
        @brief Simulated die temperature: slow wander, warmer while sampling, or the API override.
        @return Temperature in milli-degrees C.
        """
        import math
        if self.temp_override_c is not None:
            mc: int = int(round(self.temp_override_c * 1000))
        else:
            t: float = time.monotonic() - self._t0
            mc = TEMP_BASE_MC + int(1500 * math.sin(t / 60.0)) + (TEMP_RUN_RISE_MC if self.running else 0)
        self._temp_min_mc = mc if self._temp_min_mc is None else min(self._temp_min_mc, mc)
        self._temp_max_mc = mc if self._temp_max_mc is None else max(self._temp_max_mc, mc)
        return mc

    def reset(self) -> None:
        """!
        @brief Return to the post-boot defaults (used by the simulator's /reset endpoint).
        """
        with self.lock:
            self.pga_rdac = [PGA_DEFAULT_CODE] * 4
            self.pga_eeprom = [PGA_DEFAULT_CODE] * 4
            self.adc_cfg = ads.AdcConfig()
            self.adc_status = 0
        self._rate_changed()

    # ---- broadcast logging (console_printf) -----------------------------------------
    def add_client(self, sock: socket.socket) -> bool:
        """!
        @brief Register a console client for broadcast logs.
        @param sock  Client socket.
        @return False when all CONSOLE_MAX_CLIENTS slots are busy.
        """
        with self._clients_lock:
            if len(self._clients) >= CONSOLE_MAX_CLIENTS:
                return False
            self._clients.append(sock)
            return True

    def remove_client(self, sock: socket.socket) -> None:
        """!
        @brief Unregister a console client.
        @param sock  Client socket.
        """
        with self._clients_lock:
            if sock in self._clients:
                self._clients.remove(sock)

    def client_count(self) -> int:
        """!
        @brief Number of connected console clients.
        @return Client count.
        """
        with self._clients_lock:
            return len(self._clients)

    def broadcast(self, text: str) -> None:
        """!
        @brief Send log text to every console client (console_printf()).
        @param text  Text including its line terminators.
        """
        data: bytes = text.encode("latin-1", "replace")
        with self._clients_lock:
            for c in list(self._clients):
                try:
                    c.sendall(data)
                except OSError:
                    pass

    def log(self, *lines: str) -> None:
        """!
        @brief Broadcast complete log lines.
        @param lines  Lines without terminators.
        """
        self.broadcast("".join(line + EOL for line in lines))

    def dma_timeout_dump(self, hit_count: int) -> None:
        """!
        @brief Broadcast the register dump adc_hw_read() prints on a DMA timeout.
        @param hit_count  Value printed as the ISR hit counter.
        """
        self.log("DMA Timeout!",
                 "DMA buffer address: 0x00120000",
                 "\tS2MM_DMACR:\t0x00015003",
                 "\tS2MM_DMASR:\t0x00000000",
                 "\tS2MM_DA:\t0x00120000",
                 "\tS2MM_LEN:\t0x00002000",
                 "\tactive_buf:\t0",
                 "\tready_buf:\t1",
                 "\tdma_error:\t0x00000000",
                 f"\thit_count:\t{hit_count}",
                 "DMA: resetting S2MM...")

    def _rate_changed(self) -> None:
        """!
        @brief Notify the stream generator about the sample rate implied by the ADC config.
        """
        if self.on_rate_change is not None:
            self.on_rate_change(self.adc_cfg.rate_hz())

    # ---- command handlers ---------------------------------------------------------------
    def banner(self) -> str:
        """!
        @brief Text sent to a new console client (console_client_thread()).
        @return Banner including the prompts exactly as the firmware sends them.
        """
        return ("System initialization summary" + EOL + PROMPT
                + "DMA ready" + EOL + "GPIO ready" + EOL + "ADS127l14 ready" + EOL
                + "PGA ready" + EOL + "XADC ready" + EOL + "ADS127l14 configured" + EOL
                + "ADC console ready. Type HELP." + EOL + PROMPT)

    def handle_line(self, line: str) -> str:
        """!
        @brief Execute one command line (handle_line() + dispatch()).
        @param line  Command line without terminator.
        @return Reply text sent only to the requesting client (without the prompt).
        """
        argv: List[str] = line.replace("\t", " ").replace("\r", " ").replace("\n", " ").split()[:MAX_ARGS]
        if not argv:
            return ""
        with self.lock:
            self.commands_handled += 1
            table: Sequence[Tuple[str, str, object]] = ROOT_CMDS
            parent: Optional[str] = None
            depth: int = 0
            while depth < len(argv):
                entry = next((e for e in table if e[0].lower() == argv[depth].lower()), None)
                if entry is None:
                    return (f"ERR unknown {parent} sub-command" if parent else "ERR unknown command") + EOL
                depth += 1
                name, _help, target = entry
                if isinstance(target, tuple):
                    if depth >= len(argv):
                        return f"ERR missing {name} sub-command" + EOL
                    table, parent = target, name
                    continue
                return target(self, argv[depth:])
        return ""

    def cmd_help(self, _args: List[str]) -> str:
        """!
        @brief HELP: list the command table (print_help_table()).
        @param _args  Unused.
        @return Help text.
        """
        out: List[str] = ["Commands (case-insensitive) - PROTOTYPE PHASE:" + EOL]

        def walk(table: Sequence[Tuple[str, str, object]], prefix: str) -> None:
            for name, hlp, target in table:
                if isinstance(target, tuple):
                    walk(target, f"{prefix}{name} ")
                else:
                    width: int = 14 - len(prefix)
                    out.append(f"  {prefix}{name:<{max(width, 0)}} - {hlp}" + EOL)
        walk(ROOT_CMDS, "")
        return "".join(out)

    def cmd_ch_status(self, _args: List[str]) -> str:
        """!
        @brief CH_STATUS (placeholder in the firmware, always 1,1,1,1).
        @param _args  Unused.
        @return Reply text.
        """
        return "OK Channel status: 1, 1, 1, 1" + EOL

    def cmd_pwr_on(self, _args: List[str]) -> str:
        """!
        @brief ANALOG_PWR ON.
        @param _args  Unused.
        @return Reply text.
        """
        if not self.analog_power:
            self.pga_rdac = list(self.pga_eeprom)      # potentiometers power up from EEPROM
        self.analog_power = True
        return "OK Analog PWR ON" + EOL

    def cmd_pwr_off(self, _args: List[str]) -> str:
        """!
        @brief ANALOG_PWR OFF.
        @param _args  Unused.
        @return Reply text.
        """
        self.running = False                       # adc_stream_set_run(false)
        self.analog_power = False
        return "OK Analog PWR OFF" + EOL

    def cmd_adc_read_id(self, _args: List[str]) -> str:
        """!
        @brief ADC READ_ID.
        @param _args  Unused.
        @return Reply text.
        """
        return f"OK DEVICE_ID=0x{DEVICE_ID:02X} REV_ID=0x{REV_ID:02X}" + EOL

    def cmd_adc_reset(self, _args: List[str]) -> str:
        """!
        @brief ADC RESET: hardware reset and default configuration with the config log;
               the previous run request is restored afterwards (with a clean DMA restart).
        @param _args  Unused.
        @return Reply text.
        """
        was_running: bool = self.running
        self.adc_cfg = ads.AdcConfig()
        self.adc_status = 0
        c: ads.AdcConfig = self.adc_cfg
        steps: List[Tuple[str, int]] = [("GEN_CFG1", 0x05), ("GEN_CFG2", c.speed_mode << 1), ("DP_CFG1", 0x30),
                                        ("DP_CFG2", 0x00), ("CLK_CFG", 0x00)]
        for n in range(4):
            steps += [(f"CH{n}_CFG1", 0x00), (f"CH{n}_CFG2", c.fltr)]
        lines: List[str] = [f"[ADC CONFIG] Verifying Device ID ... PASS [ID: {DEVICE_ID:02X}]",
                            f"[ADC CONFIG] Revision: {REV_ID:02X}"]
        lines += [f"[ADC CONFIG] Configuring {n} ... PASS [CONFIG: {v:02X}]" for n, v in steps]
        lines.append(f"[ADC CONFIG] All channels identical. Sample rate: {c.rate_hz()} Hz")
        self.log(*lines)
        self._rate_changed()
        if was_running:
            self.restart_epoch += 1
        return "OK ADC reset done" + EOL

    def cmd_adc_start(self, _args: List[str]) -> str:
        """!
        @brief ADC START: pulse START and enable streaming.
        @param _args  Unused.
        @return Reply text.
        """
        self.running = True
        self.restart_epoch += 1                    # the stream thread restarts the DMA
        return "OK ADC sampling started ..." + EOL

    def cmd_adc_stop(self, _args: List[str]) -> str:
        """!
        @brief ADC STOP: stop sampling and streaming.
        @param _args  Unused.
        @return Reply text.
        """
        self.running = False
        return "OK ADC sampling stopped" + EOL

    def cmd_adc_status(self, _args: List[str]) -> str:
        """!
        @brief ADC STATUS.
        @param _args  Unused.
        @return Reply text.
        """
        return f"OK STATUS=0x{self.adc_status:02X}" + EOL

    def cmd_adc_clear_status(self, _args: List[str]) -> str:
        """!
        @brief ADC CLEAR_STATUS.
        @param _args  Unused.
        @return Reply text.
        """
        self.adc_status = 0
        return "OK STATUS register cleared" + EOL

    def cmd_adc_settings(self, _args: List[str]) -> str:
        """!
        @brief ADC DISPLAY_SETTINGS: broadcast the report, then reply OK.
        @param _args  Unused.
        @return Reply text.
        """
        self.log(*format_adc_settings(self.adc_cfg, True, DEVICE_ID, REV_ID, self.adc_status))
        return "OK ADC DISPLAY_SETTINGS completed" + EOL

    def cmd_set_speed_mode(self, args: List[str]) -> str:
        """!
        @brief ADC SET SPEED_MODE <0-3>.
        @param args  Remaining tokens.
        @return Reply text.
        """
        if len(args) < 1:
            return "ERR usage: ADC SET SPEED_MODE <0-3>" + EOL
        mode: Optional[int] = c_strtol(args[0], 0)
        if mode is None or not 0 <= mode <= 3:
            return "ERR SPEED_MODE must be <0-3>" + EOL
        if not ads.clocking_ok(mode, self.adc_cfg.clk_div, self.adc_cfg.f_source_hz):
            return f"ERR ADC speed mode update fail [ERR: {ADS_ERR_CLK_LIMIT}]" + EOL
        self.adc_cfg.speed_mode = mode
        return f"OK SPEED_MODE={mode}" + EOL

    def cmd_set_dclk_div(self, args: List[str]) -> str:
        """!
        @brief ADC SET DCLK_DIV <1|2|4|8>.
        @param args  Remaining tokens.
        @return Reply text.
        """
        if len(args) < 1:
            return "ERR usage: ADC SET DCLK_DIV <1|2|4|8>" + EOL
        div: Optional[int] = c_strtol(args[0], 10)
        if div not in ads.DCLK_DIV_VALUES:
            return "ERR DCLK_DIV must be one of following values <1|2|4|8>" + EOL
        self.adc_cfg.dclk_div = div
        return f"OK DCLK_DIV={div}" + EOL

    def cmd_set_clk_div(self, args: List[str]) -> str:
        """!
        @brief ADC SET CLK_DIV <1|2|3|4|8>.
        @param args  Remaining tokens.
        @return Reply text.
        """
        if len(args) < 1:
            return "ERR usage: ADC SET CLK_DIV <1|2|3|4|8>" + EOL
        div: Optional[int] = c_strtol(args[0], 10)
        if div not in ads.CLK_DIV_VALUES:
            return "ERR CLK_DIV must be one of following values <1|2|3|4|8>" + EOL
        if not ads.clocking_ok(self.adc_cfg.speed_mode, div, self.adc_cfg.f_source_hz):
            return f"ERR ADC clk_div update fail [ERR: {ADS_ERR_CLK_LIMIT}]" + EOL
        self.adc_cfg.clk_div = div
        self._rate_changed()
        return f"OK CLK_DIV={div}" + EOL

    def cmd_set_fltr(self, args: List[str]) -> str:
        """!
        @brief ADC SET FLTR <0-31>.
        @param args  Remaining tokens.
        @return Reply text.
        """
        if len(args) < 1:
            return "ERR usage: ADC SET FLTR <0-31>" + EOL
        f: Optional[int] = c_strtol(args[0], 0)
        if f is None or not 0 <= f <= 31:
            return "ERR FLTR must be <0-31>" + EOL
        self.adc_cfg.fltr = f
        self._rate_changed()
        return f"OK FLTR={f}" + EOL

    def cmd_device_temp(self, _args: List[str]) -> str:
        """!
        @brief DEVICE_TEMP: Zynq die temperature (XADC) with min/max and the warning level.
        @param _args  Unused.
        @return Reply text.
        """
        now: int = self.temperature_mc()
        return format_device_temp_reply(now, self._temp_min_mc or now, self._temp_max_mc or now,
                                        TEMP_WARN_LEVEL_C) + EOL

    def cmd_pga_set_gain(self, args: List[str]) -> str:
        """!
        @brief PGA SET_GAIN <ch0> <ch1> <ch2> <ch3> (0x00-0x7F).
        @param args  Remaining tokens.
        @return Reply text.
        """
        if len(args) != 4:
            return "ERR usage: PGA SET_GAIN <ch0> <ch1> <ch2> <ch3> (0x00-0x7F)" + EOL
        gains: List[int] = []
        for i, a in enumerate(args):
            v: Optional[int] = c_strtol(a, 0)
            if v is None or not 0 <= v <= 0x7F:
                return f"ERR CH{i} gain must be 0x00-0x7F" + EOL
            gains.append(v)
        if not self.analog_power:                      # unpowered pots read back 0 -> verify fails
            return f"ERR PGA channel gain update fail [ERR: {PGA_ERR_VERIFY}]" + EOL
        self.pga_rdac = gains
        return format_gain_reply(gains) + EOL

    def cmd_pga_read_gain(self, _args: List[str]) -> str:
        """!
        @brief PGA READ_GAIN.
        @param _args  Unused.
        @return Reply text.
        """
        return format_gain_reply(self.pga_rdac if self.analog_power else [0, 0, 0, 0]) + EOL

    def cmd_pga_settings(self, _args: List[str]) -> str:
        """!
        @brief PGA DISPLAY_SETTINGS: broadcast the report, then reply OK.
        @param _args  Unused.
        @return Reply text.
        """
        self.log(*format_pga_settings(self.pga_rdac, self.pga_eeprom))
        return "OK PGA DISPLAY_SETTINGS completed" + EOL

    def cmd_pga_reset(self, _args: List[str]) -> str:
        """!
        @brief PGA RESET: hardware reset reloads the wipers from EEPROM.
        @param _args  Unused.
        @return Reply text.
        """
        self.pga_rdac = list(self.pga_eeprom)
        return "OK PGA reset completed" + EOL

    def cmd_pga_save(self, _args: List[str]) -> str:
        """!
        @brief PGA SAVE_EEPROM: copy the wipers into EEPROM (2 x 50 ms programming time).
        @param _args  Unused.
        @return Reply text.
        """
        time.sleep(0.1)
        self.pga_eeprom = list(self.pga_rdac)
        return "OK PGA eeprom save completed" + EOL


## Command tables: (name, help, handler | sub-table), same order as console.c
ANALOG_PWR_CMDS: Tuple[Tuple[str, str, object], ...] = (
    ("ON", "enable analog power", DeviceEmulator.cmd_pwr_on),
    ("OFF", "disable analog power", DeviceEmulator.cmd_pwr_off),
)
ADC_SET_CMDS: Tuple[Tuple[str, str, object], ...] = (
    ("SPEED_MODE", "set ADC speed mode <0-3>", DeviceEmulator.cmd_set_speed_mode),
    ("DCLK_DIV", "set DCLK divider <1|2|4|8>", DeviceEmulator.cmd_set_dclk_div),
    ("CLK_DIV", "set CLK divider <1|2|3|4|8>", DeviceEmulator.cmd_set_clk_div),
    ("FLTR", "set ADC digital filter <0-31>", DeviceEmulator.cmd_set_fltr),
)
ADC_CMDS: Tuple[Tuple[str, str, object], ...] = (
    ("READ_ID", "read DEVICE_ID / REV_ID registers", DeviceEmulator.cmd_adc_read_id),
    ("RESET", "hardware reset of the ADC", DeviceEmulator.cmd_adc_reset),
    ("START", "pulse START and enable streaming", DeviceEmulator.cmd_adc_start),
    ("STOP", "stop sampling and streaming", DeviceEmulator.cmd_adc_stop),
    ("STATUS", "get ADC status register", DeviceEmulator.cmd_adc_status),
    ("CLEAR_STATUS", "clear ADC status register", DeviceEmulator.cmd_adc_clear_status),
    ("DISPLAY_SETTINGS", "display ADC settings", DeviceEmulator.cmd_adc_settings),
    ("SET", "set ADC parameters", ADC_SET_CMDS),
)
PGA_CMDS: Tuple[Tuple[str, str, object], ...] = (
    ("SET_GAIN", "set PGA gain on channels 0-3", DeviceEmulator.cmd_pga_set_gain),
    ("READ_GAIN", "read PGA gain on channels 0-3", DeviceEmulator.cmd_pga_read_gain),
    ("DISPLAY_SETTINGS", "display PGA settings", DeviceEmulator.cmd_pga_settings),
    ("RESET", "hardware reset of the PGA", DeviceEmulator.cmd_pga_reset),
    ("SAVE_EEPROM", "set current settings to PGA EEPROM", DeviceEmulator.cmd_pga_save),
)
ROOT_CMDS: Tuple[Tuple[str, str, object], ...] = (
    ("HELP", "list commands", DeviceEmulator.cmd_help),
    ("ANALOG_PWR", "analog power control", ANALOG_PWR_CMDS),
    ("ADC", "ADC control", ADC_CMDS),
    ("PGA", "PGA control", PGA_CMDS),
    ("CH_STATUS", "channel status", DeviceEmulator.cmd_ch_status),
    ("DEVICE_TEMP", "device internal temperature", DeviceEmulator.cmd_device_temp),
)


class ConsoleServer:
    """!
    @brief TCP listener of the emulated console (console_thread()).
    """

    def __init__(self, device: DeviceEmulator, bind_host: str, port: int) -> None:
        """!
        @brief Create a stopped console server.
        @param device     Device whose commands are executed.
        @param bind_host  Listen address.
        @param port       Listen port (5000 on the real board).
        """
        self.device: DeviceEmulator = device
        self.bind_host: str = bind_host
        self.port: int = port
        self.running: bool = False
        self._sock: Optional[socket.socket] = None
        self._stop: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """!
        @brief Bind and start accepting clients.
        @throws OSError when the port cannot be bound.
        """
        if self.running:
            return
        srv: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.bind_host, self.port))
        except OSError:
            srv.close()
            raise
        srv.listen(CONSOLE_MAX_CLIENTS)
        srv.settimeout(0.2)
        self._sock = srv
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._accept_loop, name="console-accept", daemon=True)
        self._thread.start()
        print(f"Emulated device console on {self.bind_host}:{self.port}")

    def stop(self) -> None:
        """!
        @brief Stop accepting clients and close the listener.
        """
        if not self.running:
            return
        self._stop.set()
        self.running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        """!
        @brief Accept clients; reject them with "ERR console busy" when all slots are used.
        """
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.device.add_client(conn):
                try:
                    conn.sendall(b"ERR console busy\r\n")
                finally:
                    conn.close()
                continue
            threading.Thread(target=self._client_loop, args=(conn,), name="console-client",
                             daemon=True).start()

    def _client_loop(self, conn: socket.socket) -> None:
        """!
        @brief Per-client receive loop with the firmware's line / idle-flush rules.
        @param conn  Client socket.
        """
        dev: DeviceEmulator = self.device
        buf: str = ""
        try:
            conn.sendall(dev.banner().encode("latin-1"))
            while not self._stop.is_set():
                r, _w, _x = select.select([conn], [], [], IDLE_FLUSH_S)
                if not r:
                    if buf:
                        reply: str = dev.handle_line(buf)
                        conn.sendall((reply + PROMPT).encode("latin-1"))
                        buf = ""
                    continue
                data: bytes = conn.recv(RX_BUF_SIZE)
                if not data:
                    break
                buf += data.decode("latin-1")
                while True:
                    cut: int = min((i for i in (buf.find("\r"), buf.find("\n")) if i >= 0), default=-1)
                    if cut < 0:
                        break
                    line: str = buf[:cut]
                    buf = buf[cut + 1:]
                    if line:
                        reply = dev.handle_line(line)
                        conn.sendall((reply + PROMPT).encode("latin-1"))
                if len(buf) >= RX_BUF_SIZE - 1:
                    buf = ""
                    conn.sendall(("ERR line too long" + EOL + PROMPT).encode("latin-1"))
        except OSError:
            pass
        finally:
            dev.remove_client(conn)
            try:
                conn.close()
            except OSError:
                pass
