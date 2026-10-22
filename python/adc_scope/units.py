"""!
@file units.py
@brief Conversion between signed ADC codes and the selectable vertical units.

Units (adc_common.style):
  UNIT_RAW    : raw unsigned 24-bit code as transmitted (0 .. 16 777 215)
  UNIT_SIGNED : two's complement code in LSB (-8 388 608 .. 8 388 607)
  UNIT_VOLT   : signed code x multiplier (default 0.596 uV/LSB), shown in volts

Internally offsets are stored as LSB deltas and trigger levels as signed LSB values, so a
unit change never loses settings.
"""
from __future__ import annotations

import math
from typing import Tuple

import numpy as np

from adc_common.protocol import CODE_MASK
from adc_common.style import UNIT_RAW, UNIT_SIGNED, UNIT_VOLT


def codes_to_units(codes: np.ndarray, unit: int, uv_per_lsb: float) -> np.ndarray:
    """!
    @brief Convert signed codes to display units.
    @param codes       Signed integer codes (any shape).
    @param unit        UNIT_RAW, UNIT_SIGNED or UNIT_VOLT.
    @param uv_per_lsb  Voltage multiplier in microvolts per LSB.
    @return float64 array of the same shape.
    """
    if unit == UNIT_RAW:
        return (codes.astype(np.int64) & CODE_MASK).astype(np.float64)
    if unit == UNIT_SIGNED:
        return codes.astype(np.float64)
    return codes.astype(np.float64) * (uv_per_lsb * 1e-6)


def delta_lsb_to_units(delta_lsb: float, unit: int, uv_per_lsb: float) -> float:
    """!
    @brief Convert a difference (offset, amplitude) from LSB to display units.
    @param delta_lsb   Difference in LSB.
    @param unit        Display unit.
    @param uv_per_lsb  Voltage multiplier.
    @return Difference in display units.
    """
    return delta_lsb * uv_per_lsb * 1e-6 if unit == UNIT_VOLT else float(delta_lsb)


def delta_units_to_lsb(delta: float, unit: int, uv_per_lsb: float) -> float:
    """!
    @brief Convert a difference from display units to LSB.
    @param delta       Difference in display units.
    @param unit        Display unit.
    @param uv_per_lsb  Voltage multiplier.
    @return Difference in LSB.
    """
    return delta / (uv_per_lsb * 1e-6) if unit == UNIT_VOLT else float(delta)


def level_lsb_to_units(level_lsb: float, unit: int, uv_per_lsb: float) -> float:
    """!
    @brief Convert an absolute signed level to display units.
    @param level_lsb   Signed level in LSB.
    @param unit        Display unit.
    @param uv_per_lsb  Voltage multiplier.
    @return Level in display units.
    """
    if unit == UNIT_RAW:
        return float(int(round(level_lsb)) & CODE_MASK)
    return delta_lsb_to_units(level_lsb, unit, uv_per_lsb)


def level_units_to_lsb(level: float, unit: int, uv_per_lsb: float) -> float:
    """!
    @brief Convert an absolute level in display units to a signed LSB level.
    @param level       Level in display units.
    @param unit        Display unit.
    @param uv_per_lsb  Voltage multiplier.
    @return Signed level in LSB.
    """
    if unit == UNIT_RAW:
        r: int = int(round(level)) & CODE_MASK
        return float(r - (1 << 24) if r >= (1 << 23) else r)
    return delta_units_to_lsb(level, unit, uv_per_lsb)


def unit_axis(unit: int) -> Tuple[str, str, bool]:
    """!
    @brief Axis labelling for a unit.
    @param unit  Display unit.
    @return Tuple (label, unit string, use SI prefixes).
    """
    if unit == UNIT_RAW:
        return "Raw code", "", False
    if unit == UNIT_SIGNED:
        return "Signed code [LSB]", "", False
    return "Amplitude", "V", True


def unit_suffix(unit: int) -> str:
    """!
    @brief Short suffix used in spin boxes.
    @param unit  Display unit.
    @return " V", " LSB" or " code".
    """
    return {UNIT_RAW: " code", UNIT_SIGNED: " LSB"}.get(unit, " V")


_SI_PREFIXES = {-12: "p", -9: "n", -6: "u", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}


def si_format(value: float, unit: str, digits: int = 4) -> str:
    """!
    @brief Format a number with an SI prefix ("1.234 mV", "48.00 kHz").
    @param value   Value in base units.
    @param unit    Unit symbol.
    @param digits  Significant digits.
    @return Formatted text; "----" for None/NaN/inf.
    """
    if value is None or not math.isfinite(value):
        return "----"
    if value == 0:
        return f"0 {unit}".rstrip()
    exp: int = int(math.floor(math.log10(abs(value)) / 3) * 3)
    exp = max(-12, min(9, exp))
    scaled: float = value / 10 ** exp
    decimals: int = max(0, digits - 1 - int(math.floor(math.log10(abs(scaled)))))
    return f"{scaled:.{decimals}f} {_SI_PREFIXES[exp]}{unit}".rstrip()


def format_amplitude(value: float, unit: int) -> str:
    """!
    @brief Format an amplitude-type measurement in the active unit.
    @param value  Value in display units.
    @param unit   Display unit.
    @return Text (codes as integers / one decimal, volts with SI prefix).
    """
    if value is None or not math.isfinite(value):
        return "----"
    if unit == UNIT_VOLT:
        return si_format(value, "V")
    return f"{value:.1f}" if abs(value) < 1000 and value != int(value) else f"{value:.0f}"
