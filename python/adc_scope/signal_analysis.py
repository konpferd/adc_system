"""!
@file signal_analysis.py
@brief Trigger search, automatic measurements and min/max decimation for display.

All functions are pure numpy and operate on float64 data in display units.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np


class Edge(enum.Enum):
    """!
    @brief Trigger slope.
    """
    RISING = "Rising"
    FALLING = "Falling"
    EITHER = "Either"


class TriggerMode(enum.Enum):
    """!
    @brief Sweep mode (Rigol naming).
    """
    AUTO = "Auto"        #!< trigger when possible, free-run otherwise
    NORMAL = "Normal"    #!< update only on a trigger
    SINGLE = "Single"    #!< capture one trigger, then stop


def rising_crossings(x: np.ndarray, level: float, hysteresis: float) -> np.ndarray:
    """!
    @brief Indices i where x crosses 'level' upwards (x[i-1] < level <= x[i]) with noise rejection.

    A crossing only counts when the signal was below (level - hysteresis) since the
    previous accepted crossing, so noise riding on a slow edge produces one event only.

    @param x           Signal.
    @param level       Threshold.
    @param hysteresis  Re-arm distance below the level (>= 0).
    @return int64 array of crossing indices (ascending).
    """
    if x.size < 2:
        return np.empty(0, dtype=np.int64)
    above: np.ndarray = x >= level
    cand: np.ndarray = np.nonzero(above[1:] & ~above[:-1])[0] + 1
    if cand.size == 0 or hysteresis <= 0:
        return cand
    armed_idx: np.ndarray = np.where(x < level - hysteresis, np.arange(x.size), -1)
    last_armed: np.ndarray = np.maximum.accumulate(armed_idx)
    la: np.ndarray = last_armed[cand - 1]
    # greedy pass in numpy: a candidate is valid if re-armed after the previous *valid* one.
    # Re-arming after the previous candidate is a close approximation and fully vectorised.
    prev: np.ndarray = np.concatenate(([-1], cand[:-1]))
    keep: np.ndarray = la > prev
    keep[0] = la[0] >= 0 or x[0] < level - hysteresis
    return cand[keep]


def crossings(x: np.ndarray, level: float, hysteresis: float, edge: Edge) -> np.ndarray:
    """!
    @brief Trigger-type crossings for any slope.
    @param x           Signal.
    @param level       Threshold.
    @param hysteresis  Noise rejection distance.
    @param edge        Slope.
    @return Sorted int64 indices.
    """
    if edge == Edge.RISING:
        return rising_crossings(x, level, hysteresis)
    if edge == Edge.FALLING:
        return rising_crossings(-x, -level, hysteresis)
    return np.union1d(rising_crossings(x, level, hysteresis), rising_crossings(-x, -level, hysteresis))


def interpolate_crossing(x: np.ndarray, idx: np.ndarray, level: float) -> np.ndarray:
    """!
    @brief Sub-sample crossing positions by linear interpolation.
    @param x      Signal.
    @param idx    Crossing indices (x[idx-1] and x[idx] on opposite sides of level).
    @param level  Threshold.
    @return float64 fractional sample positions.
    """
    a: np.ndarray = x[idx - 1]
    b: np.ndarray = x[idx]
    d: np.ndarray = b - a
    frac: np.ndarray = np.divide(level - a, d, out=np.zeros_like(d), where=d != 0)
    return (idx - 1) + np.clip(frac, 0.0, 1.0)


def find_last_trigger(x: np.ndarray, level: float, hysteresis: float, edge: Edge,
                      lo: int, hi: int) -> Optional[float]:
    """!
    @brief Newest trigger position within an index window.
    @param x           Signal.
    @param level       Threshold.
    @param hysteresis  Noise rejection distance.
    @param edge        Slope.
    @param lo          First allowed index.
    @param hi          One past the last allowed index.
    @return Fractional index of the newest crossing in [lo, hi), or None.
    """
    c: np.ndarray = crossings(x, level, hysteresis, edge)
    c = c[(c >= max(lo, 1)) & (c < hi)]
    if c.size == 0:
        return None
    i: np.ndarray = c[-1:]
    lvl: float = level
    return float(interpolate_crossing(x, i, lvl)[0])


@dataclass
class Measurement:
    """!
    @brief Automatic measurements of one channel (display units, before display gain/offset).
    """
    vmax: float = math.nan
    vmin: float = math.nan
    vpp: float = math.nan
    vavg: float = math.nan
    vrms: float = math.nan
    freq: float = math.nan
    period: float = math.nan
    samples: int = 0


def measure(x: np.ndarray, fs: float, min_vpp: float = 0.0) -> Measurement:
    """!
    @brief Rigol-style measurements: Vmax, Vmin, Vpp, Vavg, Vrms (AC+DC), frequency, period.

    Frequency uses rising crossings of the mid level (max+min)/2 with 10 % Vpp hysteresis,
    averaged over all complete periods in the record (sub-sample interpolated).

    @param x        Signal in display units.
    @param fs       Sample rate in Hz.
    @param min_vpp  Below this Vpp no frequency is reported (signal considered flat).
    @return Measurement set (NaN for values that cannot be determined).
    """
    m: Measurement = Measurement(samples=int(x.size))
    if x.size == 0:
        return m
    m.vmax = float(x.max())
    m.vmin = float(x.min())
    m.vpp = m.vmax - m.vmin
    m.vavg = float(x.mean())
    m.vrms = float(math.sqrt(float(np.dot(x, x)) / x.size))
    if fs > 0 and m.vpp > min_vpp and m.vpp > 0:
        mid: float = 0.5 * (m.vmax + m.vmin)
        c: np.ndarray = rising_crossings(x, mid, 0.1 * m.vpp)
        if c.size >= 2:
            t: np.ndarray = interpolate_crossing(x, c, mid)
            span: float = float(t[-1] - t[0])
            if span > 0:
                m.period = span / (c.size - 1) / fs
                m.freq = 1.0 / m.period
    return m


def decimate_minmax(y: np.ndarray, x0: float, dx: float, max_pairs: int) -> Tuple[np.ndarray, np.ndarray]:
    """!
    @brief Peak-preserving decimation for drawing.

    The record is split into max_pairs bins; each bin contributes its minimum and its
    maximum, so every spike stays visible at any zoom level.

    @param y          Samples.
    @param x0         X value of y[0].
    @param dx         X step between samples.
    @param max_pairs  Number of bins (output has 2*max_pairs points).
    @return Tuple (x, y) arrays ready for plotting.
    """
    n: int = y.size
    if n <= 2 * max_pairs:
        return x0 + dx * np.arange(n, dtype=np.float64), y.astype(np.float64, copy=False)
    k: int = int(math.ceil(n / max_pairs))
    bins: int = n // k
    body: np.ndarray = y[:bins * k].reshape(bins, k)
    mn: np.ndarray = body.min(axis=1)
    mx: np.ndarray = body.max(axis=1)
    yy: np.ndarray = np.empty(2 * bins, dtype=np.float64)
    yy[0::2] = mn
    yy[1::2] = mx
    xs: np.ndarray = x0 + dx * (np.arange(bins, dtype=np.float64) * k)
    xx: np.ndarray = np.empty(2 * bins, dtype=np.float64)
    xx[0::2] = xs
    xx[1::2] = xs + dx * (k - 1)
    if bins * k < n:                                     # tail
        tail: np.ndarray = y[bins * k:]
        xt: float = x0 + dx * bins * k
        xx = np.concatenate((xx, [xt, x0 + dx * (n - 1)]))
        yy = np.concatenate((yy, [float(tail.min()), float(tail.max())]))
    return xx, yy
