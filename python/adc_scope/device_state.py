"""!
@file device_state.py
@brief Host-side model of the device state, updated from console replies.

The firmware has no query for analog power or for the ADC run state, so those two values
are tracked from the last acknowledged ANALOG_PWR / ADC START|STOP command and are None
("unknown") right after connecting. The stream itself tells whether data is flowing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from adc_common.ads127l14 import AdcSettingsReport
from adc_common.console_protocol import (DeviceTemp, InitReport, PgaSettingsReport, parse_adc_settings,
                                         parse_device_temp_reply, parse_key_value_reply, parse_pga_gain_reply,
                                         parse_pga_settings)

from .console_client import ConsoleReply

TOPIC_POWER: str = "power"
TOPIC_RUN: str = "run"
TOPIC_PGA: str = "pga"
TOPIC_ADC: str = "adc"
TOPIC_STATUS: str = "status"
TOPIC_ID: str = "id"
TOPIC_ADC_STALE: str = "adc_stale"      #!< configuration changed, a DISPLAY_SETTINGS refresh is needed
TOPIC_PGA_STALE: str = "pga_stale"      #!< wipers changed outside SET_GAIN (reset), refresh needed
TOPIC_TEMP: str = "temp"                #!< new DEVICE_TEMP reading


@dataclass
class DeviceState:
    """!
    @brief Everything the GUI knows about the device.
    """
    console_connected: bool = False
    analog_power: Optional[bool] = None         #!< None = unknown
    adc_running: Optional[bool] = None          #!< last acknowledged START/STOP, None = unknown
    pga_rdac: Optional[List[int]] = None
    pga_eeprom: Optional[List[int]] = None
    pga_enabled: Optional[bool] = None
    adc: Optional[AdcSettingsReport] = None
    adc_status: Optional[int] = None
    device_id: Optional[int] = None
    rev_id: Optional[int] = None
    init: Optional[InitReport] = None
    temp: Optional[DeviceTemp] = None           #!< last DEVICE_TEMP reading
    last_error: str = ""

    @property
    def pga_available(self) -> bool:
        """!
        @brief The PGA potentiometers are supplied from the analog rail and only respond with it on.
        @return True when analog power was switched on by this application.
        """
        return self.analog_power is True

    def reset(self) -> None:
        """!
        @brief Forget everything (console disconnected or new target).
        """
        self.__init__()  # type: ignore[misc]

    def apply_reply(self, reply: ConsoleReply) -> Set[str]:
        """!
        @brief Update the model from a finished command.
        @param reply  Reply delivered by ConsoleClient.
        @return Set of TOPIC_* names that changed.
        """
        changed: Set[str] = set()
        words: List[str] = reply.command.upper().split()
        if not words:
            return changed
        status: str = reply.status_line
        if reply.error:
            self.last_error = f"{reply.command}: {reply.error}"
        ok: bool = reply.ok
        head: str = " ".join(words[:2])
        if head == "ANALOG_PWR ON" and ok:
            self.analog_power = True
            changed.add(TOPIC_POWER)
        elif head == "ANALOG_PWR OFF" and ok:
            self.analog_power = False
            self.adc_running = False             # the firmware also withdraws the run request
            self.pga_rdac = None                 # potentiometers are unpowered: values unknown
            self.pga_eeprom = None
            changed |= {TOPIC_POWER, TOPIC_PGA, TOPIC_RUN}
        elif head == "ADC START" and ok:
            self.adc_running = True
            changed.add(TOPIC_RUN)
        elif head == "ADC STOP" and ok:
            self.adc_running = False
            changed.add(TOPIC_RUN)
        elif head in ("PGA SET_GAIN", "PGA READ_GAIN"):
            gains: Optional[List[int]] = parse_pga_gain_reply(status)
            if gains is not None:
                self.pga_rdac = gains
                changed.add(TOPIC_PGA)
        elif head == "PGA DISPLAY_SETTINGS":
            rep: PgaSettingsReport = parse_pga_settings(reply.lines)
            if rep.valid:
                self.pga_rdac, self.pga_eeprom, self.pga_enabled = rep.rdac, rep.eeprom, rep.enabled
                changed.add(TOPIC_PGA)
        elif head in ("PGA RESET", "PGA SAVE_EEPROM"):
            changed.add(TOPIC_PGA_STALE)
        elif head == "ADC DISPLAY_SETTINGS":
            adc: AdcSettingsReport = parse_adc_settings(reply.lines)
            if adc.valid:
                self.adc = adc
                self.adc_status = adc.status
                self.device_id, self.rev_id = adc.device_id, adc.rev_id
                changed |= {TOPIC_ADC, TOPIC_STATUS, TOPIC_ID}
        elif head in ("ADC STATUS", "ADC CLEAR_STATUS"):
            kv = parse_key_value_reply(status)
            if "STATUS" in kv:
                self.adc_status = int(kv["STATUS"], 0)
                changed.add(TOPIC_STATUS)
            elif ok:
                changed.add(TOPIC_ADC_STALE)
        elif head == "ADC READ_ID":
            kv = parse_key_value_reply(status)
            if "DEVICE_ID" in kv:
                self.device_id = int(kv["DEVICE_ID"], 0)
                self.rev_id = int(kv.get("REV_ID", "0"), 0)
                changed.add(TOPIC_ID)
        elif head in ("ADC SET", "ADC RESET"):
            changed.add(TOPIC_ADC_STALE)
        elif words[0] == "DEVICE_TEMP":
            t: Optional[DeviceTemp] = parse_device_temp_reply(status)
            if t is not None:
                self.temp = t
                changed.add(TOPIC_TEMP)
        return changed
