"""!
@file stream_client.py
@brief Receiver thread for the ADC sample stream (TCP port 5001).

Per received socket chunk the thread does:
    parse all complete frames (with byte-wise resynchronisation on a bad header)
    -> one vectorised decode of all payloads
    -> frame-loss count from hw_frame_ctr
    -> append to the display ring buffer, update link health
    -> hand the chunk to an active capture session
The thread reconnects automatically while it is running.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from adc_common import protocol
from adc_common.protocol import ENTRY_SIZE, FRAME_MAGIC_BYTES, HEADER_LEN

from . import config
from .capture import CaptureSession
from .link_health import LinkHealth
from .ring_buffer import SampleRing


@dataclass
class StreamStats:
    """!
    @brief Counters published by the receiver (read by the GUI without locking).
    """
    connected: bool = False
    sample_rate_hz: int = 0
    total_samples: int = 0
    total_lost: int = 0
    total_gaps: int = 0
    lost_batches: int = 0
    batches: int = 0
    resync_bytes: int = 0
    rate_changes: int = 0
    connects: int = 0
    restarts: int = 0                 #!< intentional restarts whose counter jump was ignored
    measured_rate: float = 0.0        #!< received samples per second (1 s average)
    throughput_bps: float = 0.0       #!< received payload bytes per second
    last_error: str = ""


class StreamReceiver:
    """!
    @brief Background thread that connects to the stream port and keeps decoding.
    """

    def __init__(self, ring: SampleRing, health: LinkHealth) -> None:
        """!
        @brief Create an idle receiver.
        @param ring    Display buffer to fill.
        @param health  Loss statistics accumulator.
        """
        self.ring: SampleRing = ring
        self.health: LinkHealth = health
        self.stats: StreamStats = StreamStats()
        self.host: str = ""
        self.port: int = 0
        self._capture: Optional[CaptureSession] = None
        self._stop: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._prev_ctr: Optional[int] = None
        self._prev_seq: Optional[int] = None
        self._meas_t0: float = 0.0
        self._meas_n: int = 0
        self._restart_deadline: float = 0.0

    # ---- control -------------------------------------------------------------------------
    def start(self, host: str, port: int) -> None:
        """!
        @brief Start (or restart) receiving from host:port.
        @param host  Device or simulator address.
        @param port  Stream port.
        """
        self.stop()
        self.host, self.port = host, port
        self.stats = StreamStats()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="stream-rx", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """!
        @brief Stop the thread and close the connection.
        """
        self._stop.set()
        sock: Optional[socket.socket] = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.stats.connected = False

    @property
    def running(self) -> bool:
        """!
        @brief Whether the receiver thread is active (connected or retrying).
        @return True while started.
        """
        return self._thread is not None and self._thread.is_alive()

    def expect_restart(self, window_s: float = config.RESTART_EXPECT_S) -> None:
        """!
        @brief Announce an intentional stream restart (ADC START / ADC RESET).

        Every (re)start makes the firmware reset the DMA and drop the half-filled buffer, so
        hw_frame_ctr jumps once. The first discontinuity seen within window_s is therefore
        treated as a restart, not as lost frames.

        @param window_s  How long the expectation stays armed.
        """
        self._restart_deadline = time.monotonic() + window_s

    def set_capture(self, session: Optional[CaptureSession]) -> None:
        """!
        @brief Attach or detach a capture session (takes effect with the next chunk).
        @param session  Active session, or None.
        """
        self._capture = session

    # ---- thread ----------------------------------------------------------------------------
    def _run(self) -> None:
        """!
        @brief Thread body: connect / receive / reconnect until stopped.
        """
        while not self._stop.is_set():
            try:
                sock: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.RCVBUF_BYTES)
                sock.settimeout(config.CONNECT_TIMEOUT_S)
                sock.connect((self.host, self.port))
                sock.settimeout(0.5)
            except OSError as e:
                self.stats.last_error = f"connect {self.host}:{self.port}: {e}"
                try:
                    sock.close()
                except (OSError, UnboundLocalError):
                    pass
                self._stop.wait(config.RECONNECT_INTERVAL_S)
                continue
            self._sock = sock
            self.stats.connected = True
            self.stats.connects += 1
            self.stats.last_error = ""
            self.health.reset()
            self._prev_ctr = None
            self._prev_seq = None
            self._meas_t0, self._meas_n = time.monotonic(), 0
            try:
                self._receive(sock)
            except (OSError, ConnectionError) as e:
                if not self._stop.is_set():
                    self.stats.last_error = f"connection lost: {e}"
            finally:
                self.stats.connected = False
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
            if not self._stop.is_set():
                self._stop.wait(config.RECONNECT_INTERVAL_S)

    def _receive(self, sock: socket.socket) -> None:
        """!
        @brief Receive loop for one connection.
        @param sock  Connected socket.
        @throws ConnectionError when the peer closes the connection.
        """
        buf: bytearray = bytearray(config.RX_CHUNK_BYTES)
        fill: int = 0
        while not self._stop.is_set():
            if fill == len(buf):                        # a frame larger than the buffer: grow (copy)
                nb: bytearray = bytearray(len(buf) * 2)
                nb[:fill] = buf[:fill]
                buf = nb
            try:
                with memoryview(buf) as mv:
                    n: int = sock.recv_into(mv[fill:], len(buf) - fill)
            except socket.timeout:
                self._update_rate(0)
                continue
            if n == 0:
                raise ConnectionError("closed by peer")
            fill += n
            consumed: int = self._parse(buf, fill)
            if consumed:
                rest: int = fill - consumed
                buf[:rest] = buf[consumed:fill]
                fill = rest

    def _parse(self, buf: bytearray, fill: int) -> int:
        """!
        @brief Locate complete frames in the buffer and process them.
        @param buf   Receive buffer.
        @param fill  Number of valid bytes in buf.
        @return Number of bytes consumed from the start of buf.
        """
        pos: int = 0
        frames: List[Tuple[int, int, int, int]] = []           # (offset, seq, word_count, rate)
        while fill - pos >= HEADER_LEN:
            if not protocol.header_is_valid(buf, pos):
                j: int = buf.find(FRAME_MAGIC_BYTES, pos + 1, fill)
                if j < 0:
                    keep: int = max(pos, fill - 3)              # magic may straddle the chunk end
                    self.stats.resync_bytes += keep - pos
                    pos = keep
                    break
                self.stats.resync_bytes += j - pos
                pos = j
                continue
            _m, seq, wc, rate = struct.unpack_from("<IIII", buf, pos)
            need: int = HEADER_LEN + wc * ENTRY_SIZE
            if fill - pos < need:
                break
            frames.append((pos, seq, wc, rate))
            pos += need
        if frames:
            self._process(buf, frames)
        return pos

    def _process(self, buf: bytearray, frames: List[Tuple[int, int, int, int]]) -> None:
        """!
        @brief Decode a list of frames, grouped by sample rate.
        @param buf     Receive buffer holding the frames.
        @param frames  (offset, seq, word_count, rate) of each frame, in stream order.
        """
        t_recv: float = time.time()
        i: int = 0
        while i < len(frames):
            rate: int = frames[i][3]
            j: int = i
            while j < len(frames) and frames[j][3] == rate:
                j += 1
            self._process_group(buf, frames[i:j], rate, t_recv)
            i = j

    def _process_group(self, buf: bytearray, frames: List[Tuple[int, int, int, int]], rate: int,
                       t_recv: float) -> None:
        """!
        @brief Decode consecutive frames that share one sample rate.
        @param buf     Receive buffer.
        @param frames  Frames of the group.
        @param rate    Their header sample rate.
        @param t_recv  Receive timestamp of the chunk.
        """
        st: StreamStats = self.stats
        if rate != st.sample_rate_hz:                       # the device was reconfigured
            if st.sample_rate_hz:
                st.rate_changes += 1
            st.sample_rate_hz = rate
            self.ring.clear(rate)
        parts: List[np.ndarray] = [np.frombuffer(buf, dtype=np.uint8, count=wc * ENTRY_SIZE, offset=off + HEADER_LEN)
                                   for off, _s, wc, _r in frames]
        entries: np.ndarray = np.concatenate(parts).reshape(-1, ENTRY_SIZE)
        del parts
        codes, ctr = protocol.decode_signed_block(entries)
        if self._restart_deadline and self._prev_ctr is not None:
            if time.monotonic() > self._restart_deadline:
                self._restart_deadline = 0.0
            elif ((int(ctr[0]) - self._prev_ctr) & 0xFFFFFFFF) != 1:
                self._prev_ctr = None                      # restart gap: not counted as loss
                self._restart_deadline = 0.0
                st.restarts += 1
        steps: np.ndarray = protocol.counter_steps(ctr, self._prev_ctr)
        bad: np.ndarray = np.nonzero(steps != 1)[0]
        lost: int = 0
        gap_sizes: Optional[np.ndarray] = None
        if bad.size:
            s: np.ndarray = steps[bad]
            gap_sizes = np.where((s > 1) & (s < (1 << 31)), s - 1, 0)
            lost = int(gap_sizes.sum())
            if self._prev_ctr is None:                      # steps are relative to ctr[0]: index = bad + 1
                bad = bad + 1
        self._prev_ctr = int(ctr[-1])
        for _off, seq, _wc, _r in frames:                   # batch sequence continuity
            if self._prev_seq is not None:
                gap: int = (seq - self._prev_seq - 1) & 0xFFFFFFFF
                if gap < 0x80000000:
                    st.lost_batches += gap
            self._prev_seq = seq
        n: int = codes.shape[1]
        self.ring.write(codes, bad if bad.size else None, gap_sizes)
        self.health.add(n, lost, int(bad.size))
        st.total_samples += n
        st.total_lost += lost
        st.total_gaps += int(bad.size)
        st.batches += len(frames)
        self._update_rate(n)
        cap: Optional[CaptureSession] = self._capture
        if cap is not None and cap.active:
            raw: List[Tuple[bytes, bytes, int, int, int]] = [
                (bytes(buf[off:off + HEADER_LEN]), bytes(buf[off + HEADER_LEN:off + HEADER_LEN + wc * ENTRY_SIZE]),
                 seq, r, wc) for off, seq, wc, r in frames]
            cap.feed(t_recv, raw, ctr, codes, lost)

    def _update_rate(self, n: int) -> None:
        """!
        @brief Update the measured sample rate and throughput (1 s averages).
        @param n  Samples received since the last call.
        """
        self._meas_n += n
        now: float = time.monotonic()
        dt: float = now - self._meas_t0
        if dt >= 1.0:
            self.stats.measured_rate = self._meas_n / dt
            self.stats.throughput_bps = self._meas_n * ENTRY_SIZE / dt
            self._meas_t0, self._meas_n = now, 0
