#!/usr/bin/env python3
"""!
@file adc_simulator.py
@brief Virtual 4-channel 24-bit ADC device: TCP stream + emulated console + GUI + local control API.

STREAM (same bytes as the Zynq board), TCP port 5001:
    header  <IIIIBBBB (20 B): magic 'ADC1', batch_seq, word_count,
                              sample_rate_hz, chan_mask, bits, 0, 0
    payload word_count * 16 B per entry:
        [0:3] ch3  [3:6] ch2  [6:9] ch1  [9:12] ch0   (24-bit LE codes)
        [12:16] hw_frame_ctr (32-bit LE)

CONSOLE (same commands and texts as console.c), TCP port 5000 - see device_emulator.py.
    ANALOG_PWR ON/OFF, ADC START/STOP/RESET/STATUS/..., ADC SET ..., PGA ...
    - ADC STOP pauses the stream; START restarts it (one hw_frame_ctr jump, like the DMA reset)
    - DEVICE_TEMP reports a simulated die temperature (override: POST /device {"temp": 80})
    - ANALOG_PWR OFF makes the inputs read ~0
    - PGA codes scale the amplitude by code/0x40 (simulation model)
    - ADC SET FLTR / CLK_DIV change the sample rate like the real ADC

GUI: Qt window (same look as the capture viewer): sliders (log scale for frequency and
noise) + exact entry boxes per channel, server/device status panel and a live signal preview.

CONTROL API (for scripts / AI agents), HTTP + JSON, http://127.0.0.1:5002
    GET  /state                      full config, stats, limits, emulated device state
    POST /channel/<0-3>   {"wave":"sine","freq":1000,"amp":50,"offset":0,
                           "noise":true,"noise_rms":60}   (any subset)
    POST /channels        {"all":{...}}  or  {"channels":[{...},{...},...]}
    POST /drop            {"percent": 5}          drop batches (counter jumps)
    POST /fault           {"type":"stall","seconds":4} | {"type":"disconnect"}
                          | {"type":"console","text":"[ADC CONFIG] ... FAIL [ERR: -3]"}
                          | {"type":"dma_timeout"}   (firmware DMA timeout register dump)
    POST /server          {"action":"start","port":5001} | {"action":"stop"}
    POST /rate            {"hz": 800000}          sample rate; the stream header, the pacing
                                                  and the counter step all follow it
    POST /device          {"analog_power":true,"running":true,"temp":80}   set emulated device state
                          ("temp": die temperature override in deg C, null = simulated)
    POST /reset           back to start-up defaults
    GET  /capture?samples=4096&seed=1&start_counter=4931694&format=json|csv|frames
         Generates data offline with the CURRENT settings, without any socket:
         json   = signed codes per channel + counter
         csv    = same columns as the logger's adc_stream_samples.csv
         frames = raw binary, byte-identical to the TCP stream (for replay/parsers)
    Examples:
      curl http://127.0.0.1:5002/state
      curl -X POST http://127.0.0.1:5002/channel/0 -d '{"wave":"square","freq":2000}'
      curl "http://127.0.0.1:5002/capture?samples=2048&format=csv" -o test.csv

IMPORT as a library (no GUI, no sockets):
      import adc_simulator as sim
      codes, ctr = sim.generate(cfgs, n_batches=10)        # int codes (N,4), counter (N,)
      raw = sim.make_frames(cfgs, n_batches=10)            # bytes, exact wire format

Needs numpy. The GUI needs pyqtgraph + PySide6 (see requirements.txt); the library,
the stream server, the console and the control API work without them (HEADLESS = True).
"""
from __future__ import annotations

import io
import json
import math
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adc_common import protocol                                   # noqa: E402
from adc_common.protocol import CSV_HEADER, pack_frame             # noqa: E402
from device_emulator import RESTART_DROP_FRAMES, ConsoleServer, DeviceEmulator  # noqa: E402

# ----------------------------- settings -----------------------------
BIND_HOST: str = "0.0.0.0"          # stream server: reachable from the LAN
PORT: int = protocol.STREAM_PORT
SAMPLE_RATE_HZ: int = 800_000       # start-up value; changeable at run time (GUI / POST /rate / console)
RATE_LIMITS: Tuple[int, int] = (10, 10_000_000)   # Hz. Real-time pacing in Python tops out well below the upper end
WORDS_PER_BATCH: int = 512          # set to your firmware's ADC_WORDS_PER_BATCH
REALTIME: bool = True               # False = send as fast as the network allows
HEADLESS: bool = False              # True = no window, run with the defaults below
START_COUNTER: int = 0
SEED: int = 1

CONSOLE_ENABLED: bool = True        # emulate the device console (port 5000)
CONSOLE_BIND_HOST: str = "0.0.0.0"
CONSOLE_PORT: int = protocol.CONSOLE_PORT
BOOT_ANALOG_POWER: bool = True      # emulated state after start-up
BOOT_ADC_RUNNING: bool = True       # True keeps the old behaviour: data flows immediately

API_ENABLED: bool = True
API_BIND_HOST: str = "127.0.0.1"    # keep local: the API has no encryption
API_PORT: int = 5002
API_TOKEN: Optional[str] = None     # e.g. "secret" -> clients must send header X-Token: secret
MAX_CAPTURE_SAMPLES: int = 500_000

# Start-up values. wave: sine, square, triangle, sawtooth, dc, drift
# amp = % of full scale; offset and noise_rms in ADC codes (24-bit: +-8,388,607)
DEFAULT_CHANNELS: List[Dict[str, Any]] = [
    dict(wave="sine",     freq=10_000.0,  amp=50.0, offset=0.0, noise=True, noise_rms=60.0),
    dict(wave="square",   freq=15000.0,    amp=30.0, offset=0.0, noise=True, noise_rms=60.0),
    dict(wave="triangle", freq=25_000.0,  amp=20.0, offset=0.0, noise=True, noise_rms=60.0),
    dict(wave="sine",     freq=50_000.0, amp=10.0, offset=0.0, noise=True, noise_rms=60.0),
]
POWER_OFF_NOISE_RMS: float = 3.0    # codes seen on the inputs while analog power is off
# --------------------------------------------------------------------

try:                                 # same colors as the other tools (pyqtgraph not needed here)
    from adc_common.style import CHANNEL_COLORS
except ImportError:                  # pragma: no cover
    CHANNEL_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e"]

ENTRY_SIZE: int = protocol.ENTRY_SIZE
FULL_SCALE: int = protocol.SIGNED_MAX
WAVES: List[str] = ["sine", "square", "triangle", "sawtooth", "dc", "drift"]
LIMITS: Dict[str, Tuple[float, float]] = {"amp": (0.0, 100.0),
                                          "offset": (-float(FULL_SCALE), float(FULL_SCALE)),
                                          "noise_rms": (0.0, 1e7)}


def limits_for(fs: float) -> Dict[str, Tuple[float, float]]:
    """!
    @brief Field limits for a given sample rate (frequency is limited by Nyquist).
    @param fs  Sample rate in Hz.
    @return Dictionary field -> (min, max).
    """
    return {"freq": (0.0, fs / 2), **LIMITS}


# ------------------------------ signals ------------------------------
_DRIFT_F: Tuple[float, ...] = (0.05, 0.13, 0.31)
_DRIFT_P: Tuple[float, ...] = (0.7, 2.1, 4.0)
_DRIFT_W: Tuple[float, ...] = (1.0, 0.6, 0.3)


def drift(t: np.ndarray) -> np.ndarray:
    """!
    @brief Slow common wander in [-1, 1], like real shorted-input data.
    @param t  Time stamps in seconds.
    @return Wander values.
    """
    d: np.ndarray = np.zeros_like(t, dtype=float)
    for f, p, w in zip(_DRIFT_F, _DRIFT_P, _DRIFT_W):
        d += w * np.sin(2 * np.pi * f * t + p)
    return d / sum(_DRIFT_W)


def wave_shape(kind: str, ph: np.ndarray) -> np.ndarray:
    """!
    @brief Unit waveform in [-1, 1] for a phase in cycles (0..1).
    @param kind  Waveform name (see WAVES).
    @param ph    Phase in cycles.
    @return Waveform values.
    """
    if kind == "sine":
        return np.sin(2 * np.pi * ph)
    if kind == "square":
        return np.where(ph < 0.5, 1.0, -1.0)
    if kind == "triangle":
        return 4 * np.abs(ph - 0.5) - 1
    if kind == "sawtooth":
        return 2 * ph - 1
    return np.zeros_like(ph)            # "dc"


def render(cfg: Dict[str, Any], ph: np.ndarray, t: np.ndarray, rng: np.random.Generator,
           gain: float = 1.0) -> np.ndarray:
    """!
    @brief One channel as float ADC codes.
    @param cfg   Channel settings (wave, amp, offset, noise, noise_rms).
    @param ph    Phase in cycles for every sample.
    @param t     Time of every sample in seconds.
    @param rng   Noise generator.
    @param gain  Amplitude factor of the emulated PGA; 0 = analog power off.
    @return Codes as floats (not clipped).
    """
    if gain <= 0.0:
        return rng.normal(0.0, POWER_OFF_NOISE_RMS, len(ph))
    s: np.ndarray = drift(t) if cfg["wave"] == "drift" else wave_shape(cfg["wave"], ph)
    v: np.ndarray = cfg["offset"] + gain * cfg["amp"] / 100.0 * FULL_SCALE * s
    if cfg["noise"] and cfg["noise_rms"] > 0:
        v = v + rng.normal(0.0, cfg["noise_rms"], len(v))
    return v


class Generator:
    """!
    @brief Phase-continuous sample source: changing a frequency never makes a jump.
    """

    def __init__(self, fs: float = SAMPLE_RATE_HZ, seed: int = SEED) -> None:
        """!
        @brief Create a generator.
        @param fs    Sample rate in Hz.
        @param seed  Noise seed.
        """
        self.fs: float = fs
        self.rng: np.random.Generator = np.random.default_rng(seed)
        self.phase: np.ndarray = np.zeros(4)
        self.n: int = 0
        self.t_base: float = 0.0           # time already elapsed before the last rate change

    def set_rate(self, fs: float) -> None:
        """!
        @brief Change the sample rate without a jump in phase or in the slow 'drift' time.
        @param fs  New sample rate in Hz.
        """
        if fs != self.fs:
            self.t_base += self.n / self.fs
            self.n = 0
            self.fs = fs

    def block(self, cfgs: Sequence[Dict[str, Any]], count: int,
              gains: Sequence[float] = (1.0, 1.0, 1.0, 1.0)) -> np.ndarray:
        """!
        @brief Generate the next block of samples.
        @param cfgs   Four channel settings.
        @param count  Number of samples.
        @param gains  Per-channel amplitude factors (PGA model, 0 = power off).
        @return int64 array (count, 4) of signed codes, clipped to 24 bit.
        """
        idx: np.ndarray = np.arange(1, count + 1)
        t: np.ndarray = self.t_base + (self.n + np.arange(count)) / self.fs
        out: np.ndarray = np.empty((count, 4), dtype=np.int64)
        for c, cfg in enumerate(cfgs):
            f: float = min(max(float(cfg["freq"]), 0.0), self.fs / 2)
            cyc: np.ndarray = self.phase[c] + f / self.fs * idx
            self.phase[c] = cyc[-1] % 1.0
            out[:, c] = np.rint(render(cfg, cyc % 1.0, t, self.rng, gains[c]))
        self.n += count
        return np.clip(out, -(1 << 23), (1 << 23) - 1)


def pack_batch(codes: np.ndarray, counter_start: int, batch_seq: int, fs: float = SAMPLE_RATE_HZ) -> bytes:
    """!
    @brief Encode one stream frame (header + payload).
    @param codes          Signed codes (N, 4).
    @param counter_start  hw_frame_ctr of the first sample.
    @param batch_seq      Batch sequence number.
    @param fs             Sample rate written into the header.
    @return Frame bytes.
    """
    return pack_frame(codes, counter_start & 0xFFFFFFFF, batch_seq, int(fs))


def generate(cfgs: Sequence[Dict[str, Any]], n_batches: int, seed: int = SEED,
             start_counter: int = START_COUNTER, fs: float = SAMPLE_RATE_HZ) -> Tuple[np.ndarray, np.ndarray]:
    """!
    @brief Offline generation with the same noise order as the stream.
    @param cfgs           Four channel settings.
    @param n_batches      Number of WORDS_PER_BATCH batches.
    @param seed           Noise seed.
    @param start_counter  hw_frame_ctr of the first sample.
    @param fs             Sample rate in Hz.
    @return Tuple (signed codes (N,4), hw counter (N,)).
    """
    gen: Generator = Generator(fs, seed)
    codes: np.ndarray = np.concatenate([gen.block(cfgs, WORDS_PER_BATCH) for _ in range(n_batches)])
    ctr: np.ndarray = (start_counter + np.arange(len(codes), dtype=np.uint64)) & 0xFFFFFFFF
    return codes, ctr


def make_frames(cfgs: Sequence[Dict[str, Any]], n_batches: int, seed: int = SEED,
                start_counter: int = START_COUNTER, fs: float = SAMPLE_RATE_HZ) -> bytes:
    """!
    @brief Offline generation of the exact byte stream the TCP server would send.
    @param cfgs           Four channel settings.
    @param n_batches      Number of batches.
    @param seed           Noise seed.
    @param start_counter  hw_frame_ctr of the first sample.
    @param fs             Sample rate in Hz.
    @return Concatenated frames.
    """
    codes, _ = generate(cfgs, n_batches, seed, start_counter, fs)
    out: bytearray = bytearray()
    for b in range(n_batches):
        chunk: np.ndarray = codes[b * WORDS_PER_BATCH:(b + 1) * WORDS_PER_BATCH]
        out += pack_batch(chunk, (start_counter + b * WORDS_PER_BATCH) & 0xFFFFFFFF, b, fs)
    return bytes(out)


# ------------------------------- state -------------------------------
def clean_channel(kw: Dict[str, Any], fs: float = SAMPLE_RATE_HZ) -> Dict[str, Any]:
    """!
    @brief Validate/clamp a partial channel update.
    @param kw  Partial channel settings.
    @param fs  Current sample rate (frequency limit).
    @return Cleaned settings.
    @throws ValueError with a readable message.
    """
    out: Dict[str, Any] = {}
    lim: Dict[str, Tuple[float, float]] = limits_for(fs)
    for k, v in kw.items():
        if k == "wave":
            if v not in WAVES:
                raise ValueError(f"wave must be one of {WAVES}")
            out[k] = v
        elif k == "noise":
            if not isinstance(v, (bool, int)):
                raise ValueError("noise must be true or false")
            out[k] = bool(v)
        elif k in lim:
            try:
                x: float = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
            if not math.isfinite(x):
                raise ValueError(f"{k} must be finite")
            lo, hi = lim[k]
            out[k] = min(max(x, lo), hi)
        else:
            raise ValueError(f"unknown field '{k}' (allowed: wave, {', '.join(lim)}, noise)")
    return out


class SimState:
    """!
    @brief Settings shared by the GUI, the stream threads, the console and the API.
    """

    def __init__(self) -> None:
        """!
        @brief Create the start-up state.
        """
        self.lock: threading.Lock = threading.Lock()
        self.channels: List[Dict[str, Any]] = [dict(c) for c in DEFAULT_CHANNELS]
        self.drop_prob: float = 0.0
        self.clients: int = 0
        self.sent: int = 0
        self.dropped: int = 0
        self.ext_version: int = 0        # bumped by changes NOT made in the GUI -> GUI refreshes
        self.stall_until: float = 0.0
        self.kick_epoch: int = 0
        self.sample_rate: int = SAMPLE_RATE_HZ   # current rate in Hz (header, pacing, counter step)
        self.actual_rate: float = 0.0            # measured generation rate of the stream thread
        self.device: DeviceEmulator = DeviceEmulator(BOOT_ANALOG_POWER, BOOT_ADC_RUNNING,
                                                     on_rate_change=lambda hz: self.set_rate(hz, src="device"))

    def snapshot(self) -> Tuple[List[Dict[str, Any]], float]:
        """!
        @brief Thread-safe copy of the channel settings and the drop probability.
        @return Tuple (channel settings, drop probability).
        """
        with self.lock:
            return [dict(c) for c in self.channels], self.drop_prob

    def apply(self, updates: Dict[int, Dict[str, Any]], src: str = "api") -> None:
        """!
        @brief Apply validated channel updates; nothing changes if any update is invalid.
        @param updates  {channel_index: partial settings}.
        @param src      "gui" for changes made in the window (no GUI refresh needed).
        @throws ValueError on invalid input.
        """
        cleaned: Dict[int, Dict[str, Any]] = {}
        for i, kw in updates.items():
            if i not in range(4):
                raise ValueError("channel must be 0..3")
            cleaned[i] = clean_channel(kw, self.sample_rate)
        with self.lock:
            for i, kw in cleaned.items():
                self.channels[i].update(kw)
            if src != "gui":
                self.ext_version += 1

    def set_channel(self, i: int, src: str = "api", **kw: Any) -> None:
        """!
        @brief Update one channel.
        @param i    Channel index.
        @param src  Origin of the change.
        @param kw   Partial settings.
        """
        self.apply({i: kw}, src)

    def set_drop(self, p: float, src: str = "api") -> None:
        """!
        @brief Set the probability of dropping a batch (counter still advances).
        @param p    Probability 0..1.
        @param src  Origin of the change.
        """
        with self.lock:
            self.drop_prob = min(max(float(p), 0.0), 1.0)
            if src != "gui":
                self.ext_version += 1

    def reset(self) -> None:
        """!
        @brief Return channels, drop rate, sample rate and device state to start-up values.
        """
        with self.lock:
            self.channels = [dict(c) for c in DEFAULT_CHANNELS]
            self.drop_prob = 0.0
        self.device.reset()
        with self.lock:
            self.sample_rate = SAMPLE_RATE_HZ
            self.ext_version += 1

    def set_rate(self, hz: float, src: str = "api") -> None:
        """!
        @brief Change the sample rate. Header, pacing, counter step and Nyquist follow.
        @param hz   New rate in Hz (clamped to RATE_LIMITS).
        @param src  Origin of the change.
        @throws ValueError for non-numeric input.
        """
        try:
            x: float = float(hz)
        except (TypeError, ValueError):
            raise ValueError("hz must be a number")
        if not math.isfinite(x):
            raise ValueError("hz must be finite")
        lo, hi = RATE_LIMITS
        rate: int = int(min(max(round(x), lo), hi))
        with self.lock:
            self.sample_rate = rate
            for c in self.channels:                     # keep frequencies below the new Nyquist
                c["freq"] = min(c["freq"], rate / 2)
            self.actual_rate = 0.0
            if src != "gui":
                self.ext_version += 1

    def stall(self, seconds: float) -> None:
        """!
        @brief Silence the link for a while (the ADC keeps sampling, so the counter jumps).
        @param seconds  Stall duration.
        """
        with self.lock:
            self.stall_until = time.time() + seconds

    def kick_clients(self) -> None:
        """!
        @brief Disconnect every stream client.
        """
        with self.lock:
            self.kick_epoch += 1

    def full_state(self, server: "Server") -> Dict[str, Any]:
        """!
        @brief Everything the API reports in GET /state.
        @param server  Stream server.
        @return JSON-friendly dictionary.
        """
        dev: Dict[str, Any] = self.device.snapshot()
        with self.lock:
            return {
                "sample_rate_hz": self.sample_rate, "words_per_batch": WORDS_PER_BATCH,
                "actual_rate_hz": round(self.actual_rate), "rate_limits": list(RATE_LIMITS),
                "channels": [dict(c) for c in self.channels],
                "drop_percent": round(self.drop_prob * 100, 3),
                "server": {"running": server.running, "port": server.port, "clients": self.clients},
                "batches_sent": self.sent, "batches_dropped": self.dropped,
                "stalled": self.stall_until > time.time(),
                "device": dev,
                "waves": WAVES, "limits": {k: list(v) for k, v in limits_for(self.sample_rate).items()},
            }


# ------------------------------ stream server ------------------------------
def serve_client(conn: socket.socket, addr: Tuple[str, int], state: SimState, stop: threading.Event) -> None:
    """!
    @brief Stream frames to one client until it disconnects or the server stops.
    @param conn   Client socket.
    @param addr   Client address.
    @param state  Shared simulator state.
    @param stop   Server stop event.
    """
    print(f"[+] client connected: {addr[0]}:{addr[1]}")
    dev: DeviceEmulator = state.device
    fs: int = state.sample_rate
    gen: Generator = Generator(fs, SEED)
    rng: np.random.Generator = np.random.default_rng(SEED + 1)
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(10.0)
    meas_t0: float = time.perf_counter()
    meas_n: int = 0
    counter: int = START_COUNTER
    seq: int = 0
    stall_t0: Optional[float] = None
    next_due: float = time.perf_counter()
    epoch: int = state.kick_epoch
    restart_epoch: int = dev.restart_epoch
    with state.lock:
        state.clients += 1
    try:
        while not stop.is_set() and state.kick_epoch == epoch:
            wall: float = time.time()
            if state.sample_rate != fs:                  # rate changed (GUI / API / console)
                fs = state.sample_rate
                gen.set_rate(fs)
                next_due = time.perf_counter()
                meas_t0, meas_n = next_due, 0
            if not dev.running:                          # not requested: DMA idle, counter frozen
                time.sleep(0.02)
                next_due = time.perf_counter()
                meas_t0, meas_n = next_due, 0
                state.actual_rate = 0.0
                continue
            if dev.restart_epoch != restart_epoch:       # START / RESET: DMA restart drops a partial buffer
                restart_epoch = dev.restart_epoch
                counter = (counter + RESTART_DROP_FRAMES) & 0xFFFFFFFF
                gen.n += RESTART_DROP_FRAMES
            if state.stall_until > wall:                 # fault injection: silent link
                if stall_t0 is None:
                    stall_t0 = wall
                time.sleep(0.05)
                continue
            if stall_t0 is not None:                     # hardware kept sampling meanwhile
                lost: int = int((wall - stall_t0) * fs)
                counter = (counter + lost) & 0xFFFFFFFF
                gen.n += lost
                stall_t0 = None
                next_due = time.perf_counter()
                meas_t0, meas_n = next_due, 0
            if REALTIME:
                now: float = time.perf_counter()
                if now < next_due:
                    time.sleep(min(next_due - now, 0.002))
                    continue
                if now - next_due > 0.5:                 # we were stalled: do not burst
                    next_due = now
            next_due += WORDS_PER_BATCH / fs

            cfgs, drop_p = state.snapshot()
            codes: np.ndarray = gen.block(cfgs, WORDS_PER_BATCH, dev.channel_gains())
            meas_n += WORDS_PER_BATCH                    # dropped batches count: the ADC kept sampling
            now = time.perf_counter()
            if now - meas_t0 >= 1.0:
                state.actual_rate = meas_n / (now - meas_t0)
                meas_t0, meas_n = now, 0
            if drop_p and rng.random() < drop_p:
                counter = (counter + WORDS_PER_BATCH) & 0xFFFFFFFF
                with state.lock:
                    state.dropped += 1
                continue
            conn.sendall(pack_batch(codes, counter, seq, fs))
            counter = (counter + WORDS_PER_BATCH) & 0xFFFFFFFF
            seq += 1
            with state.lock:
                state.sent += 1
    except OSError:
        pass
    finally:
        conn.close()
        with state.lock:
            state.clients -= 1
            if state.clients == 0:
                state.actual_rate = 0.0
        print(f"[-] client disconnected: {addr[0]}:{addr[1]}")


class Server:
    """!
    @brief Stream listener (one thread per client).
    """

    def __init__(self, state: SimState) -> None:
        """!
        @brief Create a stopped server.
        @param state  Shared simulator state.
        """
        self.state: SimState = state
        self.running: bool = False
        self.port: int = PORT
        self.sock: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None
        self.stop_evt: threading.Event = threading.Event()

    def start(self, port: int) -> None:
        """!
        @brief Bind and start accepting stream clients.
        @param port  TCP port.
        @throws OSError when the port is in use.
        """
        if self.running:
            return
        srv: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":                 # on Windows REUSEADDR would hide "port in use"
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((BIND_HOST, port))
        except OSError:
            srv.close()
            raise
        srv.listen(4)
        srv.settimeout(0.2)
        self.sock, self.port = srv, port
        self.stop_evt = threading.Event()
        self.running = True
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()
        print(f"Virtual ADC listening on {BIND_HOST}:{port} "
              f"({self.state.sample_rate / 1e3:.0f} kS/s, {WORDS_PER_BATCH} words/batch)")

    def _accept(self) -> None:
        """!
        @brief Accept loop; spawns serve_client() per connection.
        """
        assert self.sock is not None
        while not self.stop_evt.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=serve_client,
                             args=(conn, addr, self.state, self.stop_evt),
                             daemon=True).start()

    def stop(self) -> None:
        """!
        @brief Stop the server and every client thread.
        """
        if not self.running:
            return
        self.stop_evt.set()
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)   # accept() wakes within 0.2 s; only then is the port really free
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        print("Stream server stopped.")


# ------------------------------- control API -------------------------------
class ApiHandler(BaseHTTPRequestHandler):
    """!
    @brief HTTP/JSON control API handler.
    """

    def log_message(self, *args: Any) -> None:
        """!
        @brief Silence the default per-request logging.
        @param args  Ignored.
        """
        pass

    def _reply(self, code: int, obj: Any = None, raw: Optional[bytes] = None,
               ctype: str = "application/json") -> None:
        """!
        @brief Send a response.
        @param code   HTTP status.
        @param obj    JSON body (used when raw is None).
        @param raw    Raw body.
        @param ctype  Content type.
        """
        body: bytes = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        """!
        @brief Read and decode the JSON request body.
        @return Body object ({} when empty).
        @throws ValueError for invalid JSON.
        """
        n: int = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            obj: Any = json.loads(self.rfile.read(n))
        except ValueError:
            raise ValueError("body is not valid JSON")
        if not isinstance(obj, dict):
            raise ValueError("body must be a JSON object")
        return obj

    def do_GET(self) -> None:
        """!
        @brief GET entry point.
        """
        self._route("GET")

    def do_POST(self) -> None:
        """!
        @brief POST entry point.
        """
        self._route("POST")

    def _route(self, method: str) -> None:
        """!
        @brief Authenticate, dispatch and convert exceptions to HTTP errors.
        @param method  "GET" or "POST".
        """
        if API_TOKEN and self.headers.get("X-Token") != API_TOKEN:
            return self._reply(401, {"error": "missing or wrong X-Token header"})
        url = urlparse(self.path)
        path: str = url.path.rstrip("/") or "/"
        try:
            self._dispatch(method, path, parse_qs(url.query))
        except ValueError as e:
            self._reply(400, {"error": str(e)})
        except Exception as e:                      # keep the simulator alive on any bug
            self._reply(500, {"error": f"{type(e).__name__}: {e}"})

    def _dispatch(self, method: str, path: str, q: Dict[str, List[str]]) -> None:
        """!
        @brief Endpoint routing (see the file header for the list).
        @param method  HTTP method.
        @param path    URL path.
        @param q       Query parameters.
        """
        st: SimState = self.server.sim_state      # type: ignore[attr-defined]
        srv: Server = self.server.sim_server      # type: ignore[attr-defined]
        if method == "GET" and path in ("/", "/help"):
            return self._reply(200, {"doc": "see the header of adc_simulator.py",
                                     "endpoints": ["GET /state", "POST /channel/<0-3>", "POST /channels",
                                                   "POST /drop", "POST /fault", "POST /server", "POST /rate",
                                                   "POST /device", "POST /reset", "GET /capture"]})
        if method == "GET" and path == "/state":
            return self._reply(200, st.full_state(srv))
        if method == "GET" and path == "/capture":
            return self._capture(st, q)
        if method == "POST" and path.startswith("/channel/"):
            try:
                i: int = int(path.split("/")[2])
            except (IndexError, ValueError):
                raise ValueError("use /channel/<0-3>")
            st.set_channel(i, **self._body())
            return self._reply(200, st.full_state(srv))
        if method == "POST" and path == "/channels":
            body: Dict[str, Any] = self._body()
            updates: Dict[int, Dict[str, Any]] = {}
            if "all" in body:
                for i in range(4):
                    updates[i] = dict(body["all"])
            if "channels" in body:
                if not isinstance(body["channels"], list) or len(body["channels"]) > 4:
                    raise ValueError("'channels' must be a list of up to 4 objects")
                for i, kw in enumerate(body["channels"]):
                    updates.setdefault(i, {}).update(kw)
            if not updates:
                raise ValueError('send {"all":{...}} or {"channels":[{...},...]}')
            st.apply(updates)
            return self._reply(200, st.full_state(srv))
        if method == "POST" and path == "/drop":
            st.set_drop(float(self._body().get("percent", 0)) / 100.0)
            return self._reply(200, st.full_state(srv))
        if method == "POST" and path == "/fault":
            body = self._body()
            kind: Optional[str] = body.get("type")
            if kind == "stall":
                sec: float = float(body.get("seconds", 3))
                if not 0 < sec <= 120:
                    raise ValueError("seconds must be in (0, 120]")
                st.stall(sec)
            elif kind == "disconnect":
                st.kick_clients()
            elif kind == "dma_timeout":
                st.device.dma_timeout_dump(1)
                st.device.log("DMA: resetting S2MM completed")
            elif kind == "console":
                st.device.log(str(body.get("text", "[ADC CONFIG] Configuring GEN_CFG2 ... FAIL [ERR: -3]")))
            else:
                raise ValueError('type must be "stall", "disconnect", "dma_timeout" or "console"')
            return self._reply(200, {"ok": True, "fault": kind})
        if method == "POST" and path == "/server":
            body = self._body()
            action: Optional[str] = body.get("action")
            if action == "start":
                try:
                    srv.start(int(body.get("port", srv.port)))
                except OSError as e:
                    raise ValueError(f"cannot start: {e}")
            elif action == "stop":
                srv.stop()
            else:
                raise ValueError('action must be "start" or "stop"')
            return self._reply(200, st.full_state(srv))
        if method == "POST" and path == "/rate":
            body = self._body()
            if "hz" not in body:
                raise ValueError('send {"hz": 800000}')
            st.set_rate(body["hz"])
            return self._reply(200, st.full_state(srv))
        if method == "POST" and path == "/device":
            body = self._body()
            with st.device.lock:
                if "analog_power" in body:
                    st.device.analog_power = bool(body["analog_power"])
                if "running" in body:
                    st.device.running = bool(body["running"])
                if "temp" in body:
                    st.device.temp_override_c = None if body["temp"] is None else float(body["temp"])
            return self._reply(200, st.full_state(srv))
        if method == "POST" and path == "/reset":
            st.reset()
            return self._reply(200, st.full_state(srv))
        self._reply(404, {"error": f"no such endpoint: {method} {path}"})

    def _capture(self, st: SimState, q: Dict[str, List[str]]) -> None:
        """!
        @brief GET /capture: offline generation in json, csv or raw frame format.
        @param st  Simulator state.
        @param q   Query parameters.
        """
        def arg(name: str, default: int) -> int:
            try:
                return int(q.get(name, [default])[0])
            except ValueError:
                raise ValueError(f"{name} must be an integer")
        samples: int = arg("samples", 4096)
        seed: int = arg("seed", SEED)
        start: int = arg("start_counter", START_COUNTER) & 0xFFFFFFFF
        fmt: str = q.get("format", ["json"])[0]
        if not 1 <= samples <= MAX_CAPTURE_SAMPLES:
            raise ValueError(f"samples must be 1..{MAX_CAPTURE_SAMPLES}")
        if fmt not in ("json", "csv", "frames"):
            raise ValueError("format must be json, csv or frames")
        n_batches: int = -(-samples // WORDS_PER_BATCH)       # whole batches only
        cfgs, _ = st.snapshot()
        fs: int = st.sample_rate
        if fmt == "frames":
            return self._reply(200, raw=make_frames(cfgs, n_batches, seed, start, fs),
                               ctype="application/octet-stream")
        codes, ctr = generate(cfgs, n_batches, seed, start, fs)
        n: int = len(codes)
        if fmt == "json":
            return self._reply(200, {
                "sample_rate_hz": fs, "samples": n, "start_counter": start,
                "seed": seed, "settings": cfgs, "encoding": "signed 24-bit codes",
                "hw_frame_ctr": ctr.tolist(),
                "channels": {f"ch{c}": codes[:, c].tolist() for c in range(4)}})
        # csv in the logger's own format (raw unsigned 24-bit codes)
        t0: float = time.time()
        idx: np.ndarray = np.arange(n)
        table: np.ndarray = np.column_stack([t0 + idx / fs, idx // WORDS_PER_BATCH,
                                             np.full(n, fs), ctr.astype(np.float64),
                                             *[(codes[:, c] & 0xFFFFFF) for c in range(4)]])
        buf: io.StringIO = io.StringIO()
        buf.write(CSV_HEADER + "\n")
        np.savetxt(buf, table, delimiter=",", fmt=["%.6f", "%d", "%d", "%d", "%d", "%d", "%d", "%d"])
        self._reply(200, raw=buf.getvalue().encode(), ctype="text/csv")


def start_api(state: SimState, server: Server) -> ThreadingHTTPServer:
    """!
    @brief Start the HTTP control API in a background thread.
    @param state   Simulator state.
    @param server  Stream server.
    @return The HTTP server object.
    """
    if API_BIND_HOST not in ("127.0.0.1", "localhost") and not API_TOKEN:
        print("WARNING: control API is reachable from the network without a token.")
    httpd: ThreadingHTTPServer = ThreadingHTTPServer((API_BIND_HOST, API_PORT), ApiHandler)
    httpd.sim_state, httpd.sim_server = state, server       # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"Control API on http://{API_BIND_HOST}:{API_PORT}  (GET /state)")
    return httpd


# --------------------------------- GUI ---------------------------------
def fmt_time(s: float) -> str:
    """!
    @brief Short human-readable duration.
    @param s  Seconds.
    @return e.g. "250 us", "1.5 ms", "3 s".
    """
    if s < 1e-3:
        return f"{s * 1e6:.0f} us"
    if s < 1:
        return f"{s * 1e3:.1f} ms"
    return f"{s:.0f} s"


def run_gui(state: SimState, server: Server, start_error: Optional[BaseException] = None,
            api_url: Optional[str] = None, console: Optional[ConsoleServer] = None) -> None:
    """!
    @brief Qt GUI (PySide6 + pyqtgraph) with the same look as the capture viewer.
    @param state        Simulator state.
    @param server       Stream server.
    @param start_error  Error raised when the stream server failed to start, if any.
    @param api_url      Control API URL shown in the status bar.
    @param console      Emulated console server (None when disabled).
    """
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtGui, QtWidgets
    from adc_common.style import PANEL_MIN_WIDTH, PANEL_WIDTH, configure_pyqtgraph, status_css

    configure_pyqtgraph()
    app = pg.mkQApp("Virtual ADC device")
    app.aboutToQuit.connect(server.stop)
    if console is not None:
        app.aboutToQuit.connect(console.stop)

    NMAX: float = 1e6
    LN: float = math.log10(NMAX)

    def LF() -> float:
        return math.log10(max(state.sample_rate / 2, 10.0))            # follows the sample rate

    # slider position (0..1000) <-> value
    def f_from(p: float) -> float: return 10 ** (p / 1000 * LF())                    # 1 Hz .. Nyquist (log)
    def f_to(f: float) -> float: return 0.0 if f <= 1 else min(1000.0, math.log10(f) / LF() * 1000)
    def a_from(p: float) -> float: return p / 10                                      # 0..100 % (linear)
    def a_to(a: float) -> float: return a * 10
    def n_from(p: float) -> float: return 0.0 if p <= 0.5 else 10 ** (p / 1000 * LN)  # 0, then 1 .. 1e6 (log)
    def n_to(n: float) -> float: return 0.0 if n <= 1 else min(1000.0, math.log10(n) / LN * 1000)
    def fmt(v: float) -> str: return f"{v:.0f}" if abs(v) >= 1e5 else f"{v:.5g}"
    def num(text: str) -> float: return float(text.replace(",", "."))

    win = QtWidgets.QMainWindow()
    win.setWindowTitle("Virtual ADC device")
    win.resize(1500, 900)
    root = QtWidgets.QWidget()
    win.setCentralWidget(root)
    hbox = QtWidgets.QHBoxLayout(root)

    panel = QtWidgets.QWidget()
    panel.setMinimumWidth(PANEL_MIN_WIDTH)
    lay = QtWidgets.QVBoxLayout(panel)

    # ---- server
    err: List[str] = [f"Cannot start: {start_error}" if start_error else ""]
    btn = QtWidgets.QPushButton("Stop server")
    btn.setShortcut(QtGui.QKeySequence("Ctrl+R"))
    lay.addWidget(btn)

    g = QtWidgets.QGroupBox("Server")
    f = QtWidgets.QFormLayout(g)
    port_edit = QtWidgets.QLineEdit(str(PORT))
    spn_drop = QtWidgets.QDoubleSpinBox()
    spn_drop.setRange(0.0, 100.0)
    spn_drop.setDecimals(2)
    spn_drop.setSingleStep(1.0)
    spn_drop.setSuffix(" %")
    spn_rate = QtWidgets.QSpinBox()
    spn_rate.setRange(*RATE_LIMITS)
    spn_rate.setSingleStep(1000)
    spn_rate.setSuffix(" Hz")
    spn_rate.setKeyboardTracking(False)          # apply on Enter / focus-out, not per keystroke
    spn_rate.setValue(state.sample_rate)
    cmb_rate = QtWidgets.QComboBox()
    cmb_rate.addItem("Presets...", 0)
    for hz in (8_000, 48_000, 96_000, 192_000, 400_000, 800_000, 1_024_000, 2_048_000):
        cmb_rate.addItem(f"{hz / 1e3:g} kS/s", hz)
    f.addRow("Port", port_edit)
    f.addRow("Sample rate", spn_rate)
    f.addRow("Preset", cmb_rate)
    f.addRow("Dropped batches", spn_drop)
    lay.addWidget(g)

    g = QtWidgets.QGroupBox("Status")
    v = QtWidgets.QVBoxLayout(g)
    lbl_status = QtWidgets.QLabel("-")
    lbl_info = QtWidgets.QLabel("")
    lbl_info.setFont(QtGui.QFont("Monospace", 8))
    v.addWidget(lbl_status)
    v.addWidget(lbl_info)
    if api_url:
        v.addWidget(QtWidgets.QLabel(f"API: {api_url}"))
    lay.addWidget(g)

    # ---- emulated device (console) state
    g = QtWidgets.QGroupBox("Emulated device (console port %d)" % CONSOLE_PORT if console else
                            "Emulated device (console disabled)")
    v = QtWidgets.QVBoxLayout(g)
    row = QtWidgets.QHBoxLayout()
    chk_pwr = QtWidgets.QCheckBox("Analog power")
    chk_run = QtWidgets.QCheckBox("ADC running")
    row.addWidget(chk_pwr)
    row.addWidget(chk_run)
    v.addLayout(row)
    lbl_dev = QtWidgets.QLabel("")
    lbl_dev.setFont(QtGui.QFont("Monospace", 8))
    v.addWidget(lbl_dev)
    lay.addWidget(g)

    def push_device(*_a: Any) -> None:
        with state.device.lock:
            state.device.analog_power = chk_pwr.isChecked()
            state.device.running = chk_run.isChecked()

    chk_pwr.toggled.connect(push_device)
    chk_run.toggled.connect(push_device)

    def set_status(text: str, color: str) -> None:
        lbl_status.setText(text)
        lbl_status.setStyleSheet(status_css(color))

    def toggle() -> None:
        if server.running:
            server.stop()
            return
        try:
            server.start(int(port_edit.text()))
            err[0] = ""
        except (OSError, ValueError) as e:
            err[0] = f"Cannot start: {e}"

    btn.clicked.connect(toggle)

    def push_drop(value: float) -> None:
        state.set_drop(value / 100.0, src="gui")

    def refresh_drop() -> None:
        spn_drop.blockSignals(True)
        spn_drop.setValue(state.drop_prob * 100)
        spn_drop.blockSignals(False)

    spn_drop.valueChanged.connect(push_drop)

    def push_rate(value: int) -> None:
        state.set_rate(value, src="gui")
        for r in refreshers:                     # Nyquist changed: frequencies and sliders
            r()

    def refresh_rate() -> None:
        spn_rate.blockSignals(True)
        spn_rate.setValue(state.sample_rate)
        spn_rate.blockSignals(False)

    def pick_preset(idx: int) -> None:
        hz = cmb_rate.itemData(idx)
        if hz:
            spn_rate.setValue(hz)
            cmb_rate.setCurrentIndex(0)

    spn_rate.valueChanged.connect(push_rate)
    cmb_rate.currentIndexChanged.connect(pick_preset)

    # ---- channel panels (sliders + exact entries)
    refreshers: List[Any] = []

    def make_panel(i: int) -> None:
        g = QtWidgets.QGroupBox(f"ch{i}")
        grid = QtWidgets.QGridLayout(g)
        swatch = QtWidgets.QLabel()
        swatch.setFixedSize(16, 16)
        swatch.setStyleSheet(f"background-color: {CHANNEL_COLORS[i]};")
        cmb = QtWidgets.QComboBox()
        cmb.addItems(WAVES)
        chk = QtWidgets.QCheckBox("Noise")
        top = QtWidgets.QHBoxLayout()
        top.addWidget(swatch)
        top.addWidget(cmb, 1)
        top.addWidget(chk)
        grid.addLayout(top, 0, 0, 1, 2)
        edits: Dict[str, Any] = {}
        sliders: Dict[str, Any] = {}

        def push() -> None:
            upd: Dict[str, Any] = {"wave": cmb.currentText(), "noise": chk.isChecked()}
            for key, ed in edits.items():
                try:
                    upd[key] = num(ed.text())
                except ValueError:
                    pass                          # half-typed number: keep the old value
            try:
                state.set_channel(i, src="gui", **upd)
            except ValueError:
                pass

        def to_slider(key: str, to: Any) -> None:
            try:
                pos: float = to(num(edits[key].text()))
            except ValueError:
                return
            sliders[key].blockSignals(True)
            sliders[key].setValue(int(round(pos)))
            sliders[key].blockSignals(False)

        def param(row: int, label: str, key: str, to: Any, from_pos: Any) -> None:
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            ed = QtWidgets.QLineEdit()
            grid.addWidget(ed, row, 1)
            edits[key] = ed
            ed.editingFinished.connect(lambda: ed.setText(fmt(state.channels[i][key])))
            if from_pos:
                sl = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
                sl.setRange(0, 1000)
                grid.addWidget(sl, row + 1, 0, 1, 2)
                sliders[key] = sl

                def typed(_t: str) -> None:
                    push()
                    to_slider(key, to)

                def slid(pos: int) -> None:
                    ed.setText(fmt(from_pos(float(pos))))
                    push()

                ed.textEdited.connect(typed)
                sl.valueChanged.connect(slid)
            else:
                ed.textEdited.connect(lambda _t: push())

        param(1, "Freq [Hz]", "freq", f_to, f_from)
        param(3, "Amplitude [% FS]", "amp", a_to, a_from)
        param(5, "Noise RMS [codes]", "noise_rms", n_to, n_from)
        param(7, "Offset [codes]", "offset", None, None)

        def refresh() -> None:
            c = state.snapshot()[0][i]
            for w in (cmb, chk):
                w.blockSignals(True)
            cmb.setCurrentText(c["wave"])
            chk.setChecked(c["noise"])
            for w in (cmb, chk):
                w.blockSignals(False)
            for key, ed in edits.items():
                ed.setText(fmt(c[key]))
            for key, to in (("freq", f_to), ("amp", a_to), ("noise_rms", n_to)):
                to_slider(key, to)

        refresh()
        cmb.currentIndexChanged.connect(lambda _i: push())
        chk.toggled.connect(lambda _b: push())
        refreshers.append(refresh)
        lay.addWidget(g)

    for i in range(4):
        make_panel(i)
    lay.addStretch(1)
    refresh_drop()
    refresh_rate()

    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(panel)
    scroll.setFixedWidth(PANEL_WIDTH)
    hbox.addWidget(scroll)

    # ---- preview (one plot per channel, each with its own time window)
    view = pg.GraphicsLayoutWidget()
    hbox.addWidget(view, 1)
    plots: List[Any] = []
    curves: List[Any] = []
    notes: List[Any] = []
    for c in range(4):
        p = view.addPlot(row=c, col=0)
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setMouseEnabled(x=False, y=False)
        p.setMenuEnabled(False)
        p.hideButtons()
        ax = p.getAxis("left")
        ax.enableAutoSIPrefix(False)
        ax.setWidth(70)
        p.setLabel("left", f"ch{c} [codes]")
        p.setLabel("bottom", "Time", units="s")
        curves.append(p.plot(pen=pg.mkPen(CHANNEL_COLORS[c], width=1), skipFiniteCheck=True))
        note = pg.TextItem(color="#444", anchor=(0, 0))
        p.addItem(note)
        plots.append(p)
        notes.append(note)
    plots[0].setTitle("Preview of the generated signals (not the transmitted data)")

    W: int = 1200
    prev_rng: np.random.Generator = np.random.default_rng(5)

    def draw() -> None:
        cfgs, _ = state.snapshot()
        gains: List[float] = state.device.channel_gains()
        for c, cfg in enumerate(cfgs):
            fr: float = min(max(cfg["freq"], 0.0), state.sample_rate / 2)
            if cfg["wave"] == "drift":
                window: float = 20.0
            elif cfg["wave"] == "dc" or fr <= 0:
                window = 5e-3
            else:
                window = min(max(3.0 / fr, 20e-6), 0.05)
            t: np.ndarray = np.linspace(0, window, W)
            vals: np.ndarray = np.clip(render(cfg, (fr * t) % 1.0, t, prev_rng, gains[c]), -FULL_SCALE - 1, FULL_SCALE)
            peak: float = max(float(np.abs(vals).max()), 1.0)
            curves[c].setData(t, vals)
            plots[c].setXRange(0, window, padding=0)
            plots[c].setYRange(-peak * 1.1, peak * 1.1, padding=0)
            notes[c].setText(f"{cfg['wave']}   {fmt_time(window)} shown   peak {peak:.0f}   "
                             f"PGA x{gains[c]:.3g}")
            notes[c].setPos(0, peak * 1.1)

    seen: List[int] = [state.ext_version]

    def tick() -> None:
        btn.setText("Stop server" if server.running else "Start server")
        if state.ext_version != seen[0]:          # changed through the API / console: update widgets
            seen[0] = state.ext_version
            for r in refreshers:
                r()
            refresh_drop()
            refresh_rate()
        dev: Dict[str, Any] = state.device.snapshot()
        for w, key in ((chk_pwr, "analog_power"), (chk_run, "running")):
            w.blockSignals(True)
            w.setChecked(bool(dev[key]))
            w.blockSignals(False)
        adc: Dict[str, Any] = dev["adc"]
        lbl_dev.setText(f"PGA      {' '.join(f'0x{x:02X}' for x in dev['pga_rdac'])}\n"
                        f"EEPROM   {' '.join(f'0x{x:02X}' for x in dev['pga_eeprom'])}\n"
                        f"ADC      speed {adc['speed_mode']}  clk/{adc['clk_div']}  dclk/{adc['dclk_div']}  "
                        f"fltr {adc['fltr']}\n"
                        f"         config rate {adc['rate_hz']} Hz   console clients {dev['console_clients']}\n"
                        f"TEMP     {dev['temp_c']:.1f} C"
                        + (" (override)" if dev['temp_override_c'] is not None else ""))
        with state.lock:
            fs, actual = state.sample_rate, state.actual_rate
            info: str = (f"port {server.port}   clients {state.clients}\n"
                         f"sent {state.sent}   dropped {state.dropped}\n"
                         f"header rate {fs} Hz   measured {actual:.0f} Hz")
            slow: bool = REALTIME and state.clients > 0 and dev["running"] and 0 < actual < 0.97 * fs
        if err[0]:
            set_status(err[0], "#c62828")
        elif not dev["running"]:
            set_status("ADC STOPPED (console)", "#e08a00")
        elif slow:
            set_status("RUNNING - too slow for this rate", "#e08a00")
        elif server.running:
            set_status("RUNNING", "#2a8a2a")
        else:
            set_status("stopped", "#555555")
        lbl_info.setText(info)
        draw()

    win.statusBar().showMessage(f"Control API: {api_url}" if api_url else "Control API disabled")
    timer = QtCore.QTimer()
    timer.timeout.connect(tick)
    timer.start(300)
    tick()
    win.show()
    pg.exec()


def main() -> None:
    """!
    @brief Start the stream server, the emulated console, the API and (unless HEADLESS) the GUI.
    """
    state: SimState = SimState()
    server: Server = Server(state)
    err: Optional[BaseException] = None
    try:
        server.start(PORT)
    except OSError as e:
        err = e
        print(f"Cannot start stream server: {e}")
    console: Optional[ConsoleServer] = None
    if CONSOLE_ENABLED:
        console = ConsoleServer(state.device, CONSOLE_BIND_HOST, CONSOLE_PORT)
        try:
            console.start()
        except OSError as e:
            print(f"Cannot start emulated console: {e}")
            console = None
    api_url: Optional[str] = None
    if API_ENABLED:
        try:
            start_api(state, server)
            api_url = f"http://{API_BIND_HOST}:{API_PORT}"
        except OSError as e:
            print(f"Cannot start control API: {e}")
    if HEADLESS or "--headless" in sys.argv:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.stop()
            if console:
                console.stop()
        return
    run_gui(state, server, err, api_url, console)


if __name__ == "__main__":
    main()
