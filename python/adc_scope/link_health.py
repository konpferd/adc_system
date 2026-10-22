"""!
@file link_health.py
@brief Packet-loss statistics over a sliding time window and link state classification.
"""
from __future__ import annotations

import enum
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

from . import config


class LinkState(enum.Enum):
    """!
    @brief Overall health of the data link shown next to the loss counter.
    """
    NO_CONNECTION = "No connection"           #!< stream socket not connected
    DEVICE_OFF = "Device off"                 #!< connected, no data, analog power off/unknown
    STOPPED = "No data (ADC stopped)"         #!< connected, no data, analog power on
    HEALTHY = "Healthy"                       #!< data flowing, no loss in the window
    MEDIUM_LOSS = "Medium data loss"          #!< 0 < loss < LOSS_HEAVY_PCT
    HEAVY_LOSS = "Heavy data loss"            #!< loss >= LOSS_HEAVY_PCT


@dataclass
class WindowStats:
    """!
    @brief Totals over the analysis window.
    """
    received: int = 0          #!< samples received
    lost: int = 0              #!< frames lost (counter gaps)
    gaps: int = 0              #!< number of discontinuities
    span_s: float = 0.0        #!< time covered by the window

    @property
    def loss_pct(self) -> float:
        """!
        @brief Lost frames as a percentage of expected frames.
        @return Percentage 0..100.
        """
        exp: int = self.received + self.lost
        return 100.0 * self.lost / exp if exp else 0.0


class LinkHealth:
    """!
    @brief Thread-safe accumulator of per-chunk receive/loss counts.
    """

    def __init__(self, window_s: float = config.HEALTH_WINDOW_S) -> None:
        """!
        @brief Create an empty accumulator.
        @param window_s  Length of the sliding window in seconds.
        """
        self.window_s: float = window_s
        self._events: Deque[Tuple[float, int, int, int]] = deque()
        self._lock: threading.Lock = threading.Lock()
        self._last_data: Optional[float] = None

    def reset(self) -> None:
        """!
        @brief Forget all history (new connection).
        """
        with self._lock:
            self._events.clear()
            self._last_data = None

    def add(self, received: int, lost: int, gaps: int, t: Optional[float] = None) -> None:
        """!
        @brief Record one received chunk.
        @param received  Samples received.
        @param lost      Frames lost before/inside the chunk.
        @param gaps      Discontinuities found.
        @param t         Monotonic timestamp (default: now).
        """
        now: float = time.monotonic() if t is None else t
        with self._lock:
            self._events.append((now, received, lost, gaps))
            self._last_data = now
            self._trim(now)

    def _trim(self, now: float) -> None:
        """!
        @brief Remove events older than the window (lock must be held).
        @param now  Current monotonic time.
        """
        limit: float = now - self.window_s
        while self._events and self._events[0][0] < limit:
            self._events.popleft()

    def stats(self, t: Optional[float] = None) -> WindowStats:
        """!
        @brief Totals over the last window.
        @param t  Monotonic timestamp (default: now).
        @return Window totals.
        """
        now: float = time.monotonic() if t is None else t
        with self._lock:
            self._trim(now)
            s: WindowStats = WindowStats()
            for _t, r, l, g in self._events:
                s.received += r
                s.lost += l
                s.gaps += g
            if self._events:
                s.span_s = now - self._events[0][0]
            return s

    def data_age(self, t: Optional[float] = None) -> Optional[float]:
        """!
        @brief Seconds since the last received chunk.
        @param t  Monotonic timestamp (default: now).
        @return Age in seconds, or None if nothing was received yet.
        """
        now: float = time.monotonic() if t is None else t
        with self._lock:
            return None if self._last_data is None else now - self._last_data


def classify(connected: bool, data_age: Optional[float], stats: WindowStats,
             analog_power: Optional[bool]) -> LinkState:
    """!
    @brief Derive the link state shown to the user.
    @param connected     Stream socket connected.
    @param data_age      Seconds since the last data (None = never).
    @param stats         Loss statistics of the window.
    @param analog_power  Last known analog power state (None = unknown).
    @return Link state.
    """
    if not connected:
        return LinkState.NO_CONNECTION
    if data_age is None or data_age > config.NO_DATA_TIMEOUT_S:
        return LinkState.STOPPED if analog_power else LinkState.DEVICE_OFF
    pct: float = stats.loss_pct
    if pct >= config.LOSS_HEAVY_PCT:
        return LinkState.HEAVY_LOSS
    if stats.lost > 0 and pct > config.LOSS_MEDIUM_PCT:
        return LinkState.MEDIUM_LOSS
    return LinkState.HEALTHY
