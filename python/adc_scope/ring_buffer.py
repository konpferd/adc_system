"""!
@file ring_buffer.py
@brief Lock-protected circular history of the decoded samples used by the display.

Samples are addressed by an absolute index (number of samples written since the last
clear()), so readers can keep stable references to trigger positions while the writer
keeps appending.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Deque, List, Optional, Tuple

import numpy as np

from adc_common.protocol import NUM_CHANNELS


class SampleRing:
    """!
    @brief Fixed-size circular buffer of int32 signed codes, shape (NUM_CHANNELS, capacity).
    """

    def __init__(self, capacity: int) -> None:
        """!
        @brief Allocate the buffer.
        @param capacity  Samples per channel.
        """
        self.capacity: int = int(capacity)
        self._data: np.ndarray = np.zeros((NUM_CHANNELS, self.capacity), dtype=np.int32)
        self._total: int = 0
        self._epoch: int = 0
        self._sample_rate: float = 0.0
        self._lock: threading.Lock = threading.Lock()
        self._gaps: Deque[Tuple[int, int]] = deque(maxlen=4096)

    # ---- writer side ---------------------------------------------------------------------
    def clear(self, sample_rate: float) -> None:
        """!
        @brief Drop the history (e.g. after a sample-rate change) and start a new epoch.
        @param sample_rate  Sample rate of the data that follows.
        """
        with self._lock:
            self._total = 0
            self._epoch += 1
            self._sample_rate = float(sample_rate)
            self._gaps.clear()

    def write(self, block: np.ndarray, gap_offsets: Optional[np.ndarray] = None,
              gap_sizes: Optional[np.ndarray] = None) -> None:
        """!
        @brief Append a block of samples.
        @param block        int32 array (NUM_CHANNELS, n).
        @param gap_offsets  Optional offsets inside the block where frames were lost before.
        @param gap_sizes    Number of lost frames at each gap offset.
        """
        n: int = block.shape[1]
        if n == 0:
            return
        with self._lock:
            if n >= self.capacity:
                block = block[:, -self.capacity:]
                start_abs: int = self._total + n - self.capacity
                n_w: int = self.capacity
            else:
                start_abs = self._total
                n_w = n
            pos: int = start_abs % self.capacity
            first: int = min(n_w, self.capacity - pos)
            self._data[:, pos:pos + first] = block[:, :first]
            if first < n_w:
                self._data[:, :n_w - first] = block[:, first:]
            if gap_offsets is not None and gap_sizes is not None:
                for off, size in zip(gap_offsets.tolist(), gap_sizes.tolist()):
                    self._gaps.append((self._total + int(off), int(size)))
            self._total += n

    # ---- reader side ---------------------------------------------------------------------
    @property
    def total(self) -> int:
        """!
        @brief Absolute index one past the newest sample.
        @return Number of samples written in the current epoch.
        """
        return self._total

    @property
    def epoch(self) -> int:
        """!
        @brief Counter incremented by every clear().
        @return Current epoch.
        """
        return self._epoch

    @property
    def sample_rate(self) -> float:
        """!
        @brief Sample rate of the buffered data.
        @return Rate in Hz (0 before the first frame).
        """
        return self._sample_rate

    def state(self) -> Tuple[int, int, float]:
        """!
        @brief Consistent snapshot of (epoch, total, sample_rate).
        @return Tuple (epoch, total, sample_rate).
        """
        with self._lock:
            return self._epoch, self._total, self._sample_rate

    def oldest(self) -> int:
        """!
        @brief Absolute index of the oldest sample still available.
        @return Index (>= 0).
        """
        return max(0, self._total - self.capacity)

    def read(self, start_abs: int, count: int, channels: Optional[List[int]] = None,
             epoch: Optional[int] = None) -> Optional[np.ndarray]:
        """!
        @brief Copy a range of samples.
        @param start_abs  Absolute index of the first sample.
        @param count      Number of samples.
        @param channels   Channel indices to copy (default: all).
        @param epoch      Expected epoch; None skips the check.
        @return int32 array (len(channels), count), or None if the range is no longer
                (or not yet) available or the epoch changed.
        """
        chans: List[int] = list(range(NUM_CHANNELS)) if channels is None else channels
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                return None
            if count <= 0 or start_abs < max(0, self._total - self.capacity) or start_abs + count > self._total:
                return None
            pos: int = start_abs % self.capacity
            first: int = min(count, self.capacity - pos)
            out: np.ndarray = np.empty((len(chans), count), dtype=np.int32)
            out[:, :first] = self._data[chans, pos:pos + first]
            if first < count:
                out[:, first:] = self._data[chans, :count - first]
            return out

    def gaps_in(self, start_abs: int, stop_abs: int) -> List[Tuple[int, int]]:
        """!
        @brief Frame-loss positions inside a range.
        @param start_abs  First absolute index.
        @param stop_abs   One past the last absolute index.
        @return List of (absolute index, lost frames).
        """
        with self._lock:
            return [(i, s) for i, s in self._gaps if start_abs <= i < stop_abs]
