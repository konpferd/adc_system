"""!
@file config.py
@brief Static configuration of the ADC scope application (edit here, no CLI arguments).
"""
from __future__ import annotations

import os
from typing import List, Tuple

from adc_common import protocol

# ---- connection -----------------------------------------------------------------------
SIM_HOST: str = "127.0.0.1"                        #!< simulator address (adc_simulator.py)
DEVICE_HOST: str = protocol.DEFAULT_DEVICE_IP      #!< default board address
STREAM_PORT: int = protocol.STREAM_PORT
CONSOLE_PORT: int = protocol.CONSOLE_PORT
CONNECT_TIMEOUT_S: float = 2.0
RECONNECT_INTERVAL_S: float = 1.0                  #!< retry period while the link is down
RCVBUF_BYTES: int = 8 * 1024 * 1024                #!< requested socket receive buffer
RX_CHUNK_BYTES: int = 4 * 1024 * 1024              #!< size of the stream parse buffer
CONSOLE_REPLY_TIMEOUT_S: float = 5.0               #!< max time to wait for the prompt after a command
CONSOLE_BANNER_TIMEOUT_S: float = 1.5              #!< commands are held back until the banner is done
PGA_POWER_SETTLE_MS: int = 300                     #!< wait after ANALOG_PWR ON before reading the PGA
RESTART_EXPECT_S: float = 3.0                      #!< counter jump after START/RESET within this time is not loss
TEMP_POLL_MS: int = 1000                           #!< DEVICE_TEMP polling period
TEMP_DEFAULT_WARN_C: float = 75.0                  #!< used until the device reports DEVICE_WRN_LVL
TEMP_NEAR_MARGIN_C: float = 10.0                   #!< "getting warm" colour below the warning level
TEMP_HYSTERESIS_C: float = 2.0                     #!< warning re-arms after cooling this far below

# ---- display buffer / refresh -----------------------------------------------------------
RING_CAPACITY: int = 1 << 23                       #!< samples per channel kept for display (~8.4 M)
DISPLAY_INTERVAL_MS: int = 33                      #!< plot refresh period (~30 fps)
MEASURE_INTERVAL_MS: int = 200                     #!< measurement update period
STATUS_INTERVAL_MS: int = 250                      #!< status / health / capture meter update period
CONSOLE_POLL_MS: int = 50                          #!< console event polling period
DECIMATE_POINTS: int = 2000                        #!< min/max pairs drawn per trace
MEASURE_MAX_SAMPLES: int = 2_000_000               #!< measurements use at most this many samples
TRIGGER_SEARCH_MAX: int = 4_000_000                #!< max samples scanned for a trigger per refresh

## Timebase steps in s/div (1-2-5 sequence); 10 horizontal divisions are shown
TIMEBASES: List[float] = [m * 10.0 ** e for e in range(-6, 0) for m in (1.0, 2.0, 5.0)] + [1.0]
DEFAULT_TIMEBASE: float = 50e-6
H_DIVS: int = 10
V_DIVS: int = 8

# ---- link health ------------------------------------------------------------------------
HEALTH_WINDOW_S: float = 5.0                       #!< packet-loss analysis window
NO_DATA_TIMEOUT_S: float = 1.5                     #!< no frames for this long -> "no data"
LOSS_MEDIUM_PCT: float = 0.0                       #!< loss above this -> medium (orange)
LOSS_HEAVY_PCT: float = 1.0                        #!< loss at or above this -> heavy (red)

# ---- capture ----------------------------------------------------------------------------
PYTHON_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
CAPTURE_LIMIT_MAX_BYTES: int = 1_000_000_000       #!< 1 GB CSV maximum
CAPTURE_DEFAULT_NAME: str = "adc_stream_samples_001"
CAPTURE_WRITE_QUEUE: int = 8192                    #!< chunks buffered between receiver and disk
CAPTURE_BATCHES_PER_CHUNK: int = 256               #!< batches per CSV conversion chunk

# ---- units ------------------------------------------------------------------------------
GAIN_RANGE: Tuple[float, float] = (0.001, 1000.0)  #!< display gain multiplier limits
