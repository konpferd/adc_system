"""!
@file acquisition.py
@brief Oscilloscope sweep logic: selects which part of the sample history is displayed.

Implements the Rigol-like trigger modes on top of SampleRing:
  AUTO   - show the newest trigger; if none occurs within the auto timeout, free-run
  NORMAL - show the newest trigger; keep the previous picture while waiting
  SINGLE - wait for one trigger, deliver it once and stop

The trigger is searched on the signed codes of the source channel (level and hysteresis
in LSB), so it is independent of the display unit, gain and offset.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from . import config
from .ring_buffer import SampleRing
from .signal_analysis import Edge, TriggerMode, find_last_trigger


@dataclass
class TriggerSettings:
    """!
    @brief Trigger configuration.
    """
    mode: TriggerMode = TriggerMode.AUTO
    source: int = 0
    edge: Edge = Edge.RISING
    level_lsb: float = 0.0
    hysteresis_lsb: float = 0.0


@dataclass
class Snapshot:
    """!
    @brief One displayed record.

    Sample k of data is located at time x0 + k / fs (seconds, 0 = trigger point).
    """
    data: np.ndarray            #!< int32 (NUM_CHANNELS, N) signed codes
    fs: float                   #!< sample rate
    x0: float                   #!< time of sample 0 relative to the trigger point
    start_abs: int              #!< absolute ring index of sample 0
    epoch: int                  #!< ring epoch the data belongs to
    triggered: bool             #!< True when aligned on a trigger, False for free-run
    t_wall: float               #!< monotonic time of the acquisition

    @property
    def n(self) -> int:
        """!
        @brief Number of samples per channel.
        @return Sample count.
        """
        return int(self.data.shape[1])


class SweepStatus:
    """!
    @brief Status texts shown in the run bar (Rigol style).
    """
    TRIGGERED: str = "T'D"
    AUTO: str = "AUTO"
    WAIT: str = "WAIT"
    STOP: str = "STOP"
    NO_DATA: str = "NO DATA"


class Acquisition:
    """!
    @brief Produces display snapshots according to timebase and trigger settings.
    """

    def __init__(self, ring: SampleRing) -> None:
        """!
        @brief Create the sweep controller.
        @param ring  Sample history to read from.
        """
        self.ring: SampleRing = ring
        self.trigger: TriggerSettings = TriggerSettings()
        self.timebase: float = config.DEFAULT_TIMEBASE          #!< seconds per division
        self.position: float = 0.5                             #!< fraction of the screen before the trigger
        self.status: str = SweepStatus.NO_DATA
        self.single_done: bool = False
        self._last_trigger_time: float = 0.0
        self._last_total: int = 0
        self._last_epoch: int = -1
        self._last_trig_abs: Optional[float] = None
        self._single_min_abs: int = 0

    # ---- geometry --------------------------------------------------------------------------
    def window_samples(self, fs: float) -> int:
        """!
        @brief Samples on screen for the current timebase.
        @param fs  Sample rate.
        @return Record length (at least 16, at most 90 % of the ring).
        """
        n: int = int(round(self.timebase * config.H_DIVS * fs))
        return max(16, min(n, int(self.ring.capacity * 0.9)))

    def max_timebase(self, fs: float) -> float:
        """!
        @brief Longest timebase the ring buffer can fill at a given rate.
        @param fs  Sample rate.
        @return Seconds per division.
        """
        return (self.ring.capacity * 0.9) / (config.H_DIVS * fs) if fs > 0 else math.inf

    def x_range(self) -> Tuple[float, float]:
        """!
        @brief Visible time range relative to the trigger point.
        @return Tuple (left, right) in seconds.
        """
        span: float = self.timebase * config.H_DIVS
        return -self.position * span, (1.0 - self.position) * span

    def invalidate(self) -> None:
        """!
        @brief Forget the remembered trigger and rescan the history (trigger settings changed).
        """
        self._last_trig_abs = None
        self._last_total = 0

    def arm_single(self) -> None:
        """!
        @brief Re-arm the SINGLE sweep.
        """
        self.single_done = False
        self._single_min_abs = self.ring.total          # only triggers after arming count
        self.invalidate()
        self._last_trigger_time = time.monotonic()

    # ---- acquisition -------------------------------------------------------------------------
    def acquire(self, channels: Optional[List[int]] = None) -> Optional[Snapshot]:
        """!
        @brief Produce the record to display now.
        @param channels  Channels to copy (default all); others are returned as zeros.
        @return New snapshot, or None when the picture should not change (waiting / no data).
        """
        epoch, total, fs = self.ring.state()
        now: float = time.monotonic()
        if fs <= 0 or total == 0:
            self.status = SweepStatus.NO_DATA
            return None
        if self.trigger.mode == TriggerMode.SINGLE and self.single_done:
            self.status = SweepStatus.STOP
            return None
        if epoch != self._last_epoch:
            self._last_epoch = epoch
            self._last_total = 0
            self._last_trig_abs = None
            self._single_min_abs = 0
            self._last_trigger_time = now
        n: int = self.window_samples(fs)
        pre: int = int(round(n * self.position))
        post: int = n - pre
        oldest: int = max(0, total - self.ring.capacity)
        if total - oldest < n:                              # not enough history yet
            if self.trigger.mode != TriggerMode.AUTO:
                self.status = SweepStatus.WAIT
                return None
            return self._free_run(epoch, total, fs, min(n, total - oldest), pre, now, channels)

        # ---- trigger search on the source channel
        hi: int = total - post                               # newest admissible trigger index
        lo: int = oldest + pre
        if self.trigger.mode == TriggerMode.SINGLE:
            lo = max(lo, self._single_min_abs)
            if hi - lo <= 2:
                self.status = SweepStatus.WAIT
                return None
        # only data that arrived since the last call needs scanning; older triggers are remembered
        new_data: int = total - self._last_total if self._last_total else hi - lo
        span: int = min(max(new_data + 64, 4096), config.TRIGGER_SEARCH_MAX, hi - lo)
        search_lo: int = hi - span
        trig_abs: Optional[float] = None
        if span > 2:
            margin: int = min(search_lo - oldest, 4096)      # extra history so hysteresis can arm
            src: Optional[np.ndarray] = self.ring.read(search_lo - margin, span + margin,
                                                       [self.trigger.source], epoch)
            if src is not None:
                x: np.ndarray = src[0].astype(np.float64)
                t: Optional[float] = find_last_trigger(x, self.trigger.level_lsb, self.trigger.hysteresis_lsb,
                                                       self.trigger.edge, margin, margin + span)
                if t is not None:
                    trig_abs = search_lo - margin + t
        self._last_total = total
        if trig_abs is None and self._last_trig_abs is not None and lo <= self._last_trig_abs < hi:
            trig_abs = self._last_trig_abs                   # newest trigger is still the old one
        self._last_trig_abs = trig_abs

        if trig_abs is not None:
            start: int = int(math.floor(trig_abs)) - pre
            start = max(oldest, min(start, total - n))
            data: Optional[np.ndarray] = self._read(start, n, epoch, channels)
            if data is None:
                return None
            self._last_trigger_time = now
            self.status = SweepStatus.TRIGGERED
            if self.trigger.mode == TriggerMode.SINGLE:
                self.single_done = True
            return Snapshot(data, fs, (start - trig_abs) / fs, start, epoch, True, now)

        if self.trigger.mode == TriggerMode.AUTO:
            timeout: float = max(0.1, 1.5 * n / fs)
            if now - self._last_trigger_time > timeout:
                return self._free_run(epoch, total, fs, n, pre, now, channels)
            self.status = SweepStatus.TRIGGERED
            return None
        self.status = SweepStatus.WAIT
        return None

    def _free_run(self, epoch: int, total: int, fs: float, n: int, pre: int, now: float,
                  channels: Optional[List[int]]) -> Optional[Snapshot]:
        """!
        @brief Untriggered record ending at the newest sample (AUTO mode).
        @param epoch     Ring epoch.
        @param total     Newest absolute index + 1.
        @param fs        Sample rate.
        @param n         Record length.
        @param pre       Pre-trigger samples of a full record.
        @param now       Monotonic time.
        @param channels  Channels to copy.
        @return Snapshot or None.
        """
        start: int = total - n
        data: Optional[np.ndarray] = self._read(start, n, epoch, channels)
        if data is None:
            return None
        self.status = SweepStatus.AUTO
        return Snapshot(data, fs, -pre / fs, start, epoch, False, now)

    def _read(self, start: int, n: int, epoch: int, channels: Optional[List[int]]) -> Optional[np.ndarray]:
        """!
        @brief Read a record, filling unselected channels with zeros.
        @param start     Absolute start index.
        @param n         Length.
        @param epoch     Ring epoch.
        @param channels  Channels to copy (None = all).
        @return int32 (NUM_CHANNELS, n) or None.
        """
        if channels is None:
            return self.ring.read(start, n, None, epoch)
        out: np.ndarray = np.zeros((4, n), dtype=np.int32)
        if not channels:
            return out
        part: Optional[np.ndarray] = self.ring.read(start, n, channels, epoch)
        if part is None:
            return None
        out[channels] = part
        return out
