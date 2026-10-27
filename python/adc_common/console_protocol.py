"""!
@file console_protocol.py
@brief Text protocol of the device console (TCP port 5000, console.c).

Behaviour of the firmware console that the host has to cope with:
  - Commands are case-insensitive words terminated by CR or LF (or 200 ms of idle time).
  - Each executed command ends with the prompt "> " (no line terminator).
  - Replies start with "OK" or "ERR"; HELP replies with plain text.
  - Log output of the drivers (ADS_LOG, PGA_LOG, ADC_HW_LOG) is broadcast to *every*
    connected console client at any time, e.g. "ADC DISPLAY_SETTINGS" prints its report
    as log lines followed by "OK ADC DISPLAY_SETTINGS completed".
  - The DMA is only waited on while sampling is requested AND a stream client is
    connected, so a "DMA Timeout!" register dump always reports a real fault.
  - "DEVICE_TEMP" reports the Zynq die temperature (XADC) together with the warning level
    compiled into the firmware.

This module holds the parsers used by the host application and the text generators used by
the simulator, so both sides are guaranteed to agree on the format.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .ads127l14 import (AdcConfig, AdcSettingsReport, DCLK_DIV_VALUES, MIN_DCLK_PER_FRAME,
                        SPEED_NAMES, CLK_DIV_VALUES, clocking_ok, filter_description)

PROMPT: str = "> "
EOL: str = "\r\n"
BANNER_DONE_TEXT: str = "ADC console ready. Type HELP."
NUM_PGA_CHANNELS: int = 4
PGA_MAX_CODE: int = 0x7F


# --------------------------------------------------------------------------------------
# Line classification
# --------------------------------------------------------------------------------------
class LineKind(enum.Enum):
    """!
    @brief Category of a console line, used for colouring and error pop-ups.
    """
    OK = "ok"              #!< successful command reply
    ERR = "err"            #!< failed command reply ("ERR ...")
    FAULT = "fault"        #!< asynchronous log reporting a device fault
    WARNING = "warning"    #!< asynchronous warning (configuration out of spec, ...)
    DETAIL = "detail"      #!< supporting detail of a fault report (DMA register dump)
    INFO = "info"          #!< anything else


## Register dump and recovery lines that follow "DMA Timeout!" (shown, but no extra pop-up)
_DETAIL_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"^DMA buffer address:",
    r"^\s*S2MM_(DMACR|DMASR|DA|LEN):",
    r"^\s*(active_buf|ready_buf|dma_error|hit_count):",
    r"^DMA: resetting S2MM",
))

_FAULT_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"^DMA Timeout!",
    r"\bFAIL\b",
    r"initialization (critical )?error",
    r"configuration error",
    r"\bMISMATCH\b",
    r"recovery failed",
    r"restart failed",
    r"invalid ready buffer",
    r"caller asked for",
    r"configuration Fail",
))


def classify_line(line: str) -> LineKind:
    """!
    @brief Classify one console line.
    @param line  Line text without terminator and without leading prompt.
    @return The line category.
    """
    s: str = line.strip()
    if s.startswith("OK"):
        return LineKind.OK
    if s.startswith("ERR"):
        return LineKind.ERR
    for p in _FAULT_PATTERNS:
        if p.search(s):
            return LineKind.FAULT
    for p in _DETAIL_PATTERNS:
        if p.search(line):
            return LineKind.DETAIL
    for p in _FAULT_PATTERNS:
        if p.search(s):
            return LineKind.FAULT
    if "WARNING" in s:
        return LineKind.WARNING
    return LineKind.INFO


# --------------------------------------------------------------------------------------
# Stream tokenizer: bytes -> lines / prompts
# --------------------------------------------------------------------------------------
class ConsoleTokenizer:
    """!
    @brief Splits the console byte stream into lines and prompt markers.

    The prompt "> " has no terminator and may be followed directly by an asynchronous
    log line ("> DMA Timeout!"), so it is detected at the start of a line.
    """

    def __init__(self) -> None:
        """!
        @brief Create an empty tokenizer.
        """
        self._buf: str = ""

    def feed(self, data: bytes) -> List[Tuple[str, str]]:
        """!
        @brief Add received bytes and return the complete tokens.
        @param data  Raw bytes received from the socket.
        @return List of (kind, text) with kind "prompt" or "line".
        """
        self._buf += data.decode("latin-1")
        out: List[Tuple[str, str]] = []
        while True:
            if self._buf.startswith(PROMPT):
                out.append(("prompt", ""))
                self._buf = self._buf[len(PROMPT):]
                continue
            m = re.search(r"\r\n|\n|\r", self._buf)
            if m is None:
                break
            line: str = self._buf[:m.start()]
            # a lone '\r' at the very end may be the first half of "\r\n": wait for more
            if m.group() == "\r" and m.end() == len(self._buf):
                break
            self._buf = self._buf[m.end():]
            if line == "":
                continue
            out.append(("line", line))
        return out

    def pending(self) -> str:
        """!
        @brief Text received after the last complete token.
        @return Partial line (may be empty).
        """
        return self._buf


# --------------------------------------------------------------------------------------
# Reply parsers
# --------------------------------------------------------------------------------------
_RE_HEX: str = r"0x([0-9A-Fa-f]+)"
_RE_GAIN: re.Pattern = re.compile(r"CH(\d)=0x([0-9A-Fa-f]{1,2})")


def parse_pga_gain_reply(line: str) -> Optional[List[int]]:
    """!
    @brief Parse "OK CH0=0x40, CH1=0x40, CH2=0x40, CH3=0x40" (PGA SET_GAIN / READ_GAIN).
    @param line  Reply line.
    @return List of 4 gain codes, or None if the line does not match.
    """
    if not line.startswith("OK"):
        return None
    found: Dict[int, int] = {int(c): int(v, 16) for c, v in _RE_GAIN.findall(line)}
    if sorted(found) != list(range(NUM_PGA_CHANNELS)):
        return None
    return [found[i] for i in range(NUM_PGA_CHANNELS)]


def parse_key_value_reply(line: str) -> Dict[str, str]:
    """!
    @brief Parse "OK KEY=value [KEY2=value2]" replies (SPEED_MODE, FLTR, STATUS, DEVICE_ID ...).
    @param line  Reply line.
    @return Dictionary of keys to raw value strings (empty when nothing matches).
    """
    return dict(re.findall(r"([A-Z_]+)=(0x[0-9A-Fa-f]+|-?\d+)", line))


@dataclass
class PgaSettingsReport:
    """!
    @brief Content of "PGA DISPLAY_SETTINGS".
    """
    enabled: bool = False
    rdac: List[int] = field(default_factory=lambda: [0] * NUM_PGA_CHANNELS)
    eeprom: List[int] = field(default_factory=lambda: [0] * NUM_PGA_CHANNELS)
    valid: bool = False


def parse_pga_settings(lines: Iterable[str]) -> PgaSettingsReport:
    """!
    @brief Parse the lines printed by pga_spi_display_settings().

    The firmware lists the codes by ADC channel (ch0..ch3), the same numbering as
    SET_GAIN / READ_GAIN; the returned arrays use that index.

    @param lines  Console lines (other lines are ignored).
    @return Parsed report; valid is True when all 8 values were found.
    """
    rep: PgaSettingsReport = PgaSettingsReport()
    seen: int = 0
    for line in lines:
        s: str = line.strip()
        m = re.match(r"PGA (enabled|disabled)$", s)
        if m:
            rep.enabled = m.group(1) == "enabled"
            continue
        m = re.match(r"PGA channel (\d)\s*:\s*" + _RE_HEX, s)
        if m:
            rep.rdac[int(m.group(1))] = int(m.group(2), 16)
            seen |= 1 << int(m.group(1))
            continue
        m = re.match(r"PGA eeprom ch(\d)\s*:\s*" + _RE_HEX, s)
        if m:
            rep.eeprom[int(m.group(1))] = int(m.group(2), 16)
            seen |= 1 << (4 + int(m.group(1)))
    rep.valid = seen == 0xFF
    return rep


def parse_adc_settings(lines: Iterable[str]) -> AdcSettingsReport:
    """!
    @brief Parse the lines printed by ads127l14_spi_display_settings().
    @param lines  Console lines (other lines are ignored).
    @return Parsed report; valid is True when the speed/clock/filter/rate lines were found.
    """
    rep: AdcSettingsReport = AdcSettingsReport()
    cfg: AdcConfig = AdcConfig()
    got: set = set()
    for line in lines:
        s: str = line.strip()
        if re.match(r"ADC (enabled|disabled)$", s):
            rep.enabled = s.endswith("enabled")
            got.add("enabled")
            continue
        m = re.match(r"Device ID / rev\s*:\s*" + _RE_HEX + r"\s*/\s*" + _RE_HEX, s)
        if m:
            rep.device_id, rep.rev_id = int(m.group(1), 16), int(m.group(2), 16)
            continue
        m = re.match(r"Status\s*:\s*" + _RE_HEX, s)
        if m:
            rep.status = int(m.group(1), 16)
            continue
        m = re.match(r"Speed mode\s*:\s*\w+\s*\((\d)\)", s)
        if m:
            cfg.speed_mode = int(m.group(1))
            got.add("speed")
            continue
        m = re.match(r"Clock source\s*:\s*(.+?),\s*/(\d+)\s*->\s*fCLK\s*=\s*(\d+)\s*Hz", s)
        if m:
            rep.clock_source = m.group(1)
            cfg.clk_div = int(m.group(2))
            rep.fclk_hz = int(m.group(3))
            got.add("clock")
            continue
        m = re.match(r"Filter\s*:\s*(.+?)\s*\(FLTR=(\d+)\)", s)
        if m:
            rep.filter_text = m.group(1)
            cfg.fltr = int(m.group(2))
            got.add("filter")
            continue
        m = re.match(r"Sample rate\s*:\s*([\d.]+)\s*Hz", s)
        if m:
            rep.sample_rate_hz = float(m.group(1))
            got.add("rate")
            continue
        m = re.match(r"DCLK\s*:\s*/(\d+)\s*->\s*(\d+)\s*Hz(?:,\s*(\d+)\s*DCLK per FSYNC frame)?", s)
        if m:
            cfg.dclk_div = int(m.group(1))
            rep.fdclk_hz = int(m.group(2))
            rep.dclk_per_frame = int(m.group(3)) if m.group(3) else 0
            if "WARNING" in s:
                rep.warnings.append(s[s.index("WARNING"):])
            continue
        m = re.match(r"CH(\d) CFG1/CFG2\s*:\s*" + _RE_HEX + r"\s*/\s*" + _RE_HEX, s)
        if m:
            rep.ch_cfg.append((int(m.group(2), 16), int(m.group(3), 16)))
            continue
        m = re.match(r"Channels\s*:\s*(\w+)", s)
        if m:
            rep.channels_identical = m.group(1) == "identical"
            continue
        if s.startswith("WARNING"):
            rep.warnings.append(s)
    if cfg.clk_div not in CLK_DIV_VALUES:
        cfg.clk_div = 1
    if cfg.dclk_div not in DCLK_DIV_VALUES:
        cfg.dclk_div = 1
    rep.config = cfg
    rep.valid = {"speed", "clock", "filter", "rate"} <= got or (got == {"enabled"} and not rep.enabled)
    return rep


@dataclass
class InitReport:
    """!
    @brief Start-up summary printed to every new console client.
    """
    items: Dict[str, bool] = field(default_factory=dict)   #!< component -> OK
    errors: List[str] = field(default_factory=list)


def parse_init_line(line: str, report: InitReport) -> None:
    """!
    @brief Update the start-up summary with one banner line.
    @param line    Banner line.
    @param report  Report updated in place.
    """
    s: str = line.strip()
    for key, ok_txt in (("DMA", "DMA ready"), ("GPIO", "GPIO ready"),
                        ("ADS127L14", "ADS127l14 ready"), ("PGA", "PGA ready"), ("XADC", "XADC ready"),
                        ("ADS127L14 config", "ADS127l14 configured")):
        if s == ok_txt:
            report.items[key] = True
            return
    patterns: Tuple[Tuple[str, str], ...] = (
        ("DMA", r"^DMA initialization"), ("GPIO", r"^GPIO initialization"),
        ("ADS127L14", r"^ADS127l14 initialization"), ("PGA", r"^PGA initialization"),
        ("XADC", r"^XADC initialization"), ("ADS127L14 config", r"^ADS127l14 configuration"))
    for key, pat in patterns:
        if re.match(pat, s):
            report.items[key] = False
            report.errors.append(s)
            return


@dataclass
class DeviceTemp:
    """!
    @brief Reply of "DEVICE_TEMP": Zynq die temperature from the XADC, degrees C.
    """
    now: float
    min: float
    max: float
    warn_level: Optional[float] = None      #!< DEVICE_TEMP_C_WRN_LVL of the firmware


_RE_TEMP: re.Pattern = re.compile(r"DEVICE_TEMP=(-?\d+(?:\.\d+)?)\s+MIN=(-?\d+(?:\.\d+)?)\s+"
                                  r"MAX=(-?\d+(?:\.\d+)?)(?:\s+DEVICE_WRN_LVL=(-?\d+(?:\.\d+)?))?")


def parse_device_temp_reply(line: str) -> Optional[DeviceTemp]:
    """!
    @brief Parse "OK DEVICE_TEMP=43.512 MIN=31.020 MAX=47.100 DEVICE_WRN_LVL=75".
    @param line  Reply line.
    @return Parsed values, or None when the line does not match.
    """
    if not line.startswith("OK"):
        return None
    m = _RE_TEMP.search(line)
    if m is None:
        return None
    return DeviceTemp(float(m.group(1)), float(m.group(2)), float(m.group(3)),
                      float(m.group(4)) if m.group(4) is not None else None)


# --------------------------------------------------------------------------------------
# Text generators (simulator side) - formats copied from the firmware sources
# --------------------------------------------------------------------------------------
def format_rate_mhz(mhz: int) -> str:
    """!
    @brief Format a milli-hertz rate like print_rate() ("800000.000 Hz").
    @param mhz  Rate in mHz.
    @return Formatted text.
    """
    return f"{mhz // 1000}.{mhz % 1000:03d} Hz"


def format_adc_settings(cfg: AdcConfig, enabled: bool = True, device_id: int = 0x04, rev_id: int = 0x00,
                        status: int = 0x00) -> List[str]:
    """!
    @brief Produce the report of ads127l14_spi_display_settings() as complete lines.
    @param cfg        Active configuration.
    @param enabled    ADC ready flag.
    @param device_id  DEV_ID register.
    @param rev_id     REV_ID register.
    @param status     STATUS register.
    @return Lines without terminators.
    """
    lines: List[str] = ["====== ADC SETTINGS ======",
                        f"ADC {'enabled' if enabled else 'disabled'}",
                        f"Device ID / rev : 0x{device_id:02X} / 0x{rev_id:02X}",
                        f"Status          : 0x{status:02X}"]
    if not enabled:
        return lines + ["=========================="]
    lines.append(f"Speed mode      : {SPEED_NAMES[cfg.speed_mode]} ({cfg.speed_mode})")
    lines.append(f"Clock source    : internal oscillator, /{cfg.clk_div} -> fCLK = {cfg.fclk_hz()} Hz")
    if not clocking_ok(cfg.speed_mode, cfg.clk_div, cfg.f_source_hz):
        lines.append("  WARNING: fCLK is outside the range allowed for this speed mode")
    lines.append(f"Filter          : {filter_description(cfg.fltr)} (FLTR={cfg.fltr})")
    lines.append(f"Sample rate     : {format_rate_mhz(cfg.rate_mhz())} per channel")
    dl: str = f"DCLK            : /{cfg.dclk_div} -> {cfg.fdclk_hz()} Hz"
    cyc: int = cfg.dclk_per_frame()
    if cfg.rate_mhz():
        dl += f", {cyc} DCLK per FSYNC frame"
        if cyc < MIN_DCLK_PER_FRAME:
            dl += "  WARNING: < 24 bits needed per frame"
    lines.append(dl)
    for n in range(4):
        lines.append(f"CH{n} CFG1/CFG2   : 0x00 / 0x{cfg.fltr & 0x1F:02X}")
    lines.append("Channels        : identical")
    lines.append("==========================")
    return lines


def format_pga_settings(rdac: Sequence[int], eeprom: Sequence[int], enabled: bool = True) -> List[str]:
    """!
    @brief Produce the report of pga_spi_display_settings() as lines.
    @param rdac     Current wiper codes indexed by logical channel ch0..ch3.
    @param eeprom   EEPROM codes indexed by logical channel ch0..ch3.
    @param enabled  PGA ready flag.
    @return Lines without terminators, listed by ADC channel like the firmware.
    """
    lines: List[str] = ["====== PGA SETTINGS ======", f"PGA {'enabled' if enabled else 'disabled'}", ""]
    lines += [f"PGA channel {i}  : 0x{rdac[i]:02X}" for i in range(4)]
    lines.append("")
    lines += [f"PGA eeprom ch{i} : 0x{eeprom[i]:02X}" for i in range(4)]
    lines.append("==========================")
    return lines


def format_gain_reply(gains: Sequence[int]) -> str:
    """!
    @brief Reply line of PGA SET_GAIN / READ_GAIN.
    @param gains  Four wiper codes.
    @return "OK CH0=0x.., CH1=0x.., CH2=0x.., CH3=0x.."
    """
    return "OK " + ", ".join(f"CH{i}=0x{g:02X}" for i, g in enumerate(gains))


def format_millideg(mc: int) -> str:
    """!
    @brief Milli-degrees as "-12.345" (mc_str() in console.c).
    @param mc  Temperature in milli-degrees C.
    @return Formatted text.
    """
    a: int = abs(mc)
    return f"{'-' if mc < 0 else ''}{a // 1000}.{a % 1000:03d}"


def format_device_temp_reply(now_mc: int, min_mc: int, max_mc: int, warn_level: int) -> str:
    """!
    @brief Reply line of DEVICE_TEMP (cmd_device_temp() in console.c).
    @param now_mc      Current temperature, milli-degrees C.
    @param min_mc      Minimum since power-up, milli-degrees C.
    @param max_mc      Maximum since power-up, milli-degrees C.
    @param warn_level  DEVICE_TEMP_C_WRN_LVL in degrees C.
    @return "OK DEVICE_TEMP=.. MIN=.. MAX=.. DEVICE_WRN_LVL=.."
    """
    return (f"OK DEVICE_TEMP={format_millideg(now_mc)} MIN={format_millideg(min_mc)} "
            f"MAX={format_millideg(max_mc)} DEVICE_WRN_LVL={warn_level}")
