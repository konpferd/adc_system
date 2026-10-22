"""!
@file style.py
@brief Common look of all ADC GUI tools (colors, units, panel geometry, pyqtgraph setup).

Every Qt tool in the project imports its colors and unit names from here so plots stay
consistent between the live scope, the capture viewer and the simulator.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

CHANNELS: List[str] = ["ch0", "ch1", "ch2", "ch3"]
CHANNEL_COLORS: List[str] = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e"]
DEFAULT_COLORS: Dict[str, str] = dict(zip(CHANNELS, CHANNEL_COLORS))

DEFAULT_UV_PER_LSB: float = 0.596          #!< microvolts per LSB of the signed 24-bit code

UNIT_RAW: int = 0
UNIT_SIGNED: int = 1
UNIT_VOLT: int = 2
UNIT_NAMES: List[str] = [
    "Raw (unsigned code)",
    "24-bit signed (LSB)",
    "24-bit signed x voltage conversion",
]

PANEL_WIDTH: int = 385                    #!< width of the scrollable control panels
PANEL_MIN_WIDTH: int = 340

COLOR_OK: str = "#2a8a2a"
COLOR_ERROR: str = "#c62828"
COLOR_WARN: str = "#e08a00"
COLOR_IDLE: str = "#555555"
COLOR_INFO: str = "#1565c0"
COLOR_GAP: Tuple[int, int, int, int] = (220, 0, 0, 110)

STATUS_FONT_CSS: str = "font-weight: bold; font-size: 13pt;"
MONO_FONT_FAMILY: str = "Monospace"


def status_css(color: str) -> str:
    """!
    @brief Style sheet of the large bold status labels.
    @param color  Text color.
    @return CSS string.
    """
    return f"{STATUS_FONT_CSS} color: {color};"


def configure_pyqtgraph() -> None:
    """!
    @brief Apply the global pyqtgraph options shared by all tools (white background).
    """
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=False, background="w", foreground="k")
