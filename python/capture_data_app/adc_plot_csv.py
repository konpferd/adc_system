#!/usr/bin/env python3
"""Plot 4-channel 24-bit ADC data from adc_stream_samples.csv.

Just run:  python plot_adc.py
The CSV is looked up next to this script first, then in the current folder.
Tweak the settings below if needed.
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ----------------------------- settings -----------------------------
CSV_NAME = "captures/adc_stream_samples.csv"
SIGNED = True           # 24-bit two's complement (False = plain unsigned codes)
VREF = None             # e.g. 2.5 to show volts instead of raw codes
START_S = None          # window start in seconds (None = from beginning)
STOP_S = None           # window end in seconds (None = to the end)
SHOW_SPECTRUM = True    # extra figure with spectrum of each channel
MAX_POINTS = 200_000    # decimate plot above this many points
# --------------------------------------------------------------------

CHANNELS = ["ch0", "ch1", "ch2", "ch3"]
ADC_BITS = 24


def find_csv() -> Path:
    for base in (Path(__file__).resolve().parent, Path.cwd()):
        p = base / CSV_NAME
        if p.exists():
            return p
    sys.exit(f"Could not find {CSV_NAME} next to the script or in the current folder.")


def sign_extend(x: np.ndarray, bits: int = ADC_BITS) -> np.ndarray:
    x = x.astype(np.int64) & ((1 << bits) - 1)
    return np.where(x >= (1 << (bits - 1)), x - (1 << bits), x)


def load(path: Path):
    df = pd.read_csv(path)
    missing = [c for c in ["hw_frame_ctr", "sample_rate_hz", *CHANNELS] if c not in df.columns]
    if missing:
        sys.exit(f"Missing columns in CSV: {missing}")

    fs = float(df["sample_rate_hz"].iloc[0])
    ctr = df["hw_frame_ctr"].to_numpy(dtype=np.int64)

    step = np.diff(ctr)
    gap_idx = np.flatnonzero(step != 1)
    lost = int(np.sum(step[gap_idx] - 1)) if len(gap_idx) else 0

    print(f"File: {path}")
    print(f"Samples: {len(df)}  fs: {fs:.0f} Hz  duration: {len(df) / fs:.3f} s")
    if len(gap_idx):
        print(f"WARNING: {len(gap_idx)} counter discontinuities, ~{lost} samples lost/duplicated")
        for i in gap_idx[:10]:
            print(f"  counter {ctr[i]} -> {ctr[i + 1]} (step {step[i]})")
        if len(gap_idx) > 10:
            print(f"  ... {len(gap_idx) - 10} more")
    else:
        print("Counter is continuous: no lost samples.")

    data = {}
    for ch in CHANNELS:
        raw = df[ch].to_numpy()
        vals = sign_extend(raw).astype(float) if SIGNED else raw.astype(float)
        if VREF is not None:
            vals = vals / (2 ** (ADC_BITS - 1) if SIGNED else 2 ** ADC_BITS) * VREF
        data[ch] = vals

    t = (ctr - ctr[0]) / fs
    return t, data, gap_idx, fs


def main():
    t, data, gap_idx, fs = load(find_csv())
    unit = "V" if VREF is not None else "code"

    mask = np.ones_like(t, dtype=bool)
    if START_S is not None:
        mask &= t >= START_S
    if STOP_S is not None:
        mask &= t <= STOP_S
    if not mask.any():
        sys.exit("Selected window contains no samples.")

    idx = np.flatnonzero(mask)
    stride = max(1, len(idx) // MAX_POINTS)
    idx = idx[::stride]
    if stride > 1:
        print(f"Plot decimated by {stride}")

    fig, axes = plt.subplots(4, 1, sharex=True, figsize=(12, 8))
    for ax, ch in zip(axes, CHANNELS):
        ax.plot(t[idx], data[ch][idx], lw=0.7)
        ax.set_ylabel(f"{ch} [{unit}]")
        ax.grid(True, alpha=0.3)
        for g in gap_idx:
            if mask[g]:
                ax.axvline(t[g], color="red", alpha=0.4, lw=0.8)
    axes[-1].set_xlabel("Time [s]")
    title = "ADC channels"
    if len(gap_idx):
        title += f"  (red lines: {len(gap_idx)} gaps)"
    fig.suptitle(title)
    fig.tight_layout()

    if SHOW_SPECTRUM:
        n = int(mask.sum())
        win = np.hanning(n)
        freqs = np.fft.rfftfreq(n, 1 / fs)
        fig2, ax2 = plt.subplots(figsize=(12, 5))
        for ch in CHANNELS:
            x = data[ch][mask]
            x = (x - x.mean()) * win
            mag = np.abs(np.fft.rfft(x)) / (win.sum() / 2)
            ax2.semilogy(freqs, mag + 1e-12, lw=0.7, label=ch)
        ax2.set_xlabel("Frequency [Hz]")
        ax2.set_ylabel(f"Amplitude [{unit}]")
        ax2.set_title("Spectrum (Hann window, DC removed)")
        ax2.grid(True, which="both", alpha=0.3)
        ax2.legend()
        fig2.tight_layout()

    plt.show()


if __name__ == "__main__":
    main()