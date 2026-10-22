"""!
@file ads127l14.py
@brief Host-side model of the ADS127L14 clock / filter configuration.

The tables and the integer math mirror ads127l14_spi.c, so the host can predict the
sample rate and the validity of a configuration before sending it to the device.

Console commands that change the configuration (console.c):
    ADC SET SPEED_MODE <0-3>
    ADC SET CLK_DIV    <1|2|3|4|8>
    ADC SET DCLK_DIV   <1|2|4|8>
    ADC SET FLTR       <0-31>
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

FCLK_INT_HZ: int = 25_600_000                   #!< ADS127L14_FCLK_INT_HZ
FCLK_EXT_HZ: int = 0                            #!< ADS127L14_FCLK_EXT_HZ (0 = not fitted)
FCLK_MIN_HZ: int = 500_000                      #!< FCLK_MIN_HZ
SPEED_FCLK_MAX_HZ: Tuple[int, int, int, int] = (3_290_000, 13_150_000, 26_300_000, 33_660_000)
SPEED_NAMES: Tuple[str, str, str, str] = ("Low", "Mid", "High", "Max")
CLK_DIV_VALUES: Tuple[int, ...] = (1, 2, 3, 4, 8)   #!< index = CLK_CFG divider code
DCLK_DIV_VALUES: Tuple[int, ...] = (1, 2, 4, 8)     #!< index = DP_CFG2 DCLK divider code
MIN_DCLK_PER_FRAME: int = 24                        #!< 4-lane TDM, 24-bit data, no status/CRC

## OSR multiplied by 3 for every FLTR code (fltr_osr_x3 in ads127l14_spi.c)
FLTR_OSR_X3: Tuple[int, ...] = (
    96, 192, 384, 768, 1536, 3072, 6144, 12288,
    36, 48, 72, 96, 192, 384, 768, 1536, 3072, 6144, 12288,
    192, 384, 960, 1920, 3840, 9600, 19200, 38400, 96000,
    80000, 96000, 288000, 480000,
)

DEFAULT_SPEED_MODE: int = 2     #!< ADS127L14_SPI_DEFAULT_SPEED_MODE (High)
DEFAULT_DCLK_DIV: int = 1       #!< divider value, code 0
DEFAULT_CLK_DIV: int = 1        #!< divider value, code 0
DEFAULT_FLTR: int = 9           #!< Sinc4, OSR = 16


def filter_type(fltr: int) -> str:
    """!
    @brief Digital filter family for a FLTR code (same text as print_filter()).
    @param fltr  FLTR code 0..31.
    @return Filter family name.
    """
    if fltr < 8:
        return "Wideband"
    if fltr < 19:
        return "Sinc4"
    if fltr < 28:
        return "Sinc4+Sinc1 (total OSR)"
    if fltr < 30:
        return "Sinc3"
    return "Sinc3+Sinc1 (total OSR)"


def filter_osr(fltr: int) -> int:
    """!
    @brief Oversampling ratio of a FLTR code, rounded like the firmware prints it.
    @param fltr  FLTR code 0..31.
    @return Integer OSR.
    """
    return (FLTR_OSR_X3[fltr] + 1) // 3


def filter_description(fltr: int) -> str:
    """!
    @brief One-line description of a FLTR code.
    @param fltr  FLTR code 0..31.
    @return e.g. "Sinc4, OSR = 16".
    """
    return f"{filter_type(fltr)}, OSR = {filter_osr(fltr)}"


def clk_div_code(clk_div: int) -> int:
    """!
    @brief Convert a CLK divider value into its register code.
    @param clk_div  Divider value 1, 2, 3, 4 or 8.
    @return Register code 0..4.
    @throws ValueError for unsupported values.
    """
    if clk_div not in CLK_DIV_VALUES:
        raise ValueError(f"CLK_DIV must be one of {CLK_DIV_VALUES}")
    return CLK_DIV_VALUES.index(clk_div)


def sample_rate_mhz(f_source_hz: int, clk_div: int, fltr: int) -> int:
    """!
    @brief Output data rate in milli-hertz (ads127l14_spi_calc_sample_rate_mhz()).
    @param f_source_hz  Clock source frequency.
    @param clk_div      CLK divider value (1, 2, 3, 4, 8).
    @param fltr         FLTR code 0..31.
    @return Rate in mHz, 0 for an invalid input.
    """
    if f_source_hz <= 0 or clk_div not in CLK_DIV_VALUES or not 0 <= fltr <= 31:
        return 0
    num: int = f_source_hz * 3000
    den: int = clk_div * 2 * FLTR_OSR_X3[fltr]
    return num // den


def sample_rate_hz(f_source_hz: int, clk_div: int, fltr: int) -> int:
    """!
    @brief Output data rate in Hz, rounded like ads127l14_spi_get_sample_rate().
    @param f_source_hz  Clock source frequency.
    @param clk_div      CLK divider value.
    @param fltr         FLTR code.
    @return Rate in Hz.
    """
    return (sample_rate_mhz(f_source_hz, clk_div, fltr) + 500) // 1000


def clocking_ok(speed_mode: int, clk_div: int, f_source_hz: int = FCLK_INT_HZ) -> bool:
    """!
    @brief Check fCLK against the limits of a speed mode (clocking_ok() in the firmware).
    @param speed_mode   Speed mode 0..3.
    @param clk_div      CLK divider value.
    @param f_source_hz  Clock source frequency (0 = unknown external clock, always accepted).
    @return True if the combination is within the datasheet range.
    """
    if f_source_hz == 0:
        return True
    fclk: int = f_source_hz // clk_div
    return FCLK_MIN_HZ <= fclk <= SPEED_FCLK_MAX_HZ[speed_mode & 3]


@dataclass
class AdcConfig:
    """!
    @brief User-visible ADC configuration (the four values settable over the console).
    """
    speed_mode: int = DEFAULT_SPEED_MODE
    clk_div: int = DEFAULT_CLK_DIV
    dclk_div: int = DEFAULT_DCLK_DIV
    fltr: int = DEFAULT_FLTR
    f_source_hz: int = FCLK_INT_HZ

    def fclk_hz(self) -> int:
        """!
        @brief Modulator clock after the CLK divider.
        @return fCLK in Hz.
        """
        return self.f_source_hz // self.clk_div

    def fdclk_hz(self) -> int:
        """!
        @brief Data clock after the DCLK divider.
        @return DCLK frequency in Hz.
        """
        return self.f_source_hz // self.dclk_div

    def rate_mhz(self) -> int:
        """!
        @brief Output data rate in milli-hertz.
        @return Rate in mHz.
        """
        return sample_rate_mhz(self.f_source_hz, self.clk_div, self.fltr)

    def rate_hz(self) -> int:
        """!
        @brief Output data rate in Hz (value placed in the stream header).
        @return Rate in Hz.
        """
        return sample_rate_hz(self.f_source_hz, self.clk_div, self.fltr)

    def dclk_per_frame(self) -> int:
        """!
        @brief DCLK cycles available per FSYNC frame.
        @return Cycle count (0 if the rate is unknown).
        """
        r: int = self.rate_mhz()
        return (self.fdclk_hz() * 1000) // r if r else 0

    def warnings(self) -> List[str]:
        """!
        @brief Check the configuration against the firmware / datasheet limits.
        @return List of human-readable problems (empty when the configuration is valid).
        """
        out: List[str] = []
        if not clocking_ok(self.speed_mode, self.clk_div, self.f_source_hz):
            out.append(f"fCLK = {self.fclk_hz() / 1e6:.3f} MHz is outside the range of speed mode "
                       f"{SPEED_NAMES[self.speed_mode]} ({FCLK_MIN_HZ / 1e6:.1f} .. "
                       f"{SPEED_FCLK_MAX_HZ[self.speed_mode] / 1e6:.2f} MHz) - the device will refuse it")
        cyc: int = self.dclk_per_frame()
        if cyc and cyc < MIN_DCLK_PER_FRAME:
            out.append(f"only {cyc} DCLK cycles per frame (< {MIN_DCLK_PER_FRAME} needed) - "
                       f"data will be corrupted")
        return out

    def command_sequence(self, current: "AdcConfig") -> List[str]:
        """!
        @brief Console commands that move the device from 'current' to this configuration.

        Speed mode and CLK divider are ordered so that every intermediate state is valid
        (same rule as ads127l14_spi_set_clocking()).

        @param current  Configuration presently active on the device.
        @return Ordered list of command strings (empty if nothing changes).
        """
        cmds: List[str] = []
        speed_changes: bool = self.speed_mode != current.speed_mode
        div_changes: bool = self.clk_div != current.clk_div
        if speed_changes and div_changes:
            if clocking_ok(current.speed_mode, self.clk_div, self.f_source_hz):
                cmds += [f"ADC SET CLK_DIV {self.clk_div}", f"ADC SET SPEED_MODE {self.speed_mode}"]
            else:
                cmds += [f"ADC SET SPEED_MODE {self.speed_mode}", f"ADC SET CLK_DIV {self.clk_div}"]
        elif speed_changes:
            cmds.append(f"ADC SET SPEED_MODE {self.speed_mode}")
        elif div_changes:
            cmds.append(f"ADC SET CLK_DIV {self.clk_div}")
        if self.dclk_div != current.dclk_div:
            cmds.append(f"ADC SET DCLK_DIV {self.dclk_div}")
        if self.fltr != current.fltr:
            cmds.append(f"ADC SET FLTR {self.fltr}")
        return cmds


@dataclass
class AdcSettingsReport:
    """!
    @brief Everything printed by "ADC DISPLAY_SETTINGS" (ads127l14_spi_display_settings()).
    """
    enabled: bool = False
    device_id: int = 0
    rev_id: int = 0
    status: int = 0
    config: AdcConfig = field(default_factory=AdcConfig)
    clock_source: str = ""
    fclk_hz: int = 0
    filter_text: str = ""
    sample_rate_hz: float = 0.0
    fdclk_hz: int = 0
    dclk_per_frame: int = 0
    ch_cfg: List[Tuple[int, int]] = field(default_factory=list)
    channels_identical: bool = True
    warnings: List[str] = field(default_factory=list)
    valid: bool = False                     #!< True once a complete report was parsed
