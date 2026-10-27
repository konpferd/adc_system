#!/usr/bin/env python3
"""!
@file adc_logger_fast.py
@brief Command-line high-speed stream logger (raw capture + CSV conversion).

Same wire format, same decoding, same CSV output format as the GUI capture of adc_scope:

    recv_time,batch_seq,sample_rate_hz,hw_frame_ctr,ch0,ch1,ch2,ch3

How it keeps up at ~1 MS/s
--------------------------
1. CAPTURE phase (hot path): per batch it does only
       recv_into() -> numpy loss check -> hand over to a writer thread
   The raw stream (timestamp + header + payload) is appended to a binary file.
   Lost frames are counted live from hw_frame_ctr (32-bit wrap-safe).
2. CONVERT phase (after capture / Ctrl+C): the raw file is decoded with vectorised numpy
   and written to the CSV in chunks via pandas (adc_common.protocol.convert_raw_to_csv).
   Set CONVERT_ONLY = True to convert an existing RAW_FILE without capturing.

Wire format and raw file layout: see adc_common/protocol.py.
Channel values are written to the CSV as raw unsigned 24-bit codes.
"""
from __future__ import annotations

import os
import queue
import socket
import struct
import sys
import threading
import time
from typing import Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adc_common import protocol                                              # noqa: E402
from adc_common.protocol import (ENTRY_SIZE as ADC_ENTRY_SIZE, HEADER_FMT, HEADER_LEN,  # noqa: E402
                                 REC_PREFIX_STRUCT, convert_raw_to_csv, count_lost, decode_counter,
                                 header_is_valid)

# ---------------------------------------------------------------------------
# Static configuration -- edit these instead of passing CLI args.
# ---------------------------------------------------------------------------
#HOST = "192.168.1.10"          # board IP
HOST: str = "127.0.0.1"         # localhost for simulation IP
PORT: int = protocol.STREAM_PORT
OUT_FILE: str = "captures/adc_stream_samples.csv"
RAW_FILE: str = "captures/adc_stream_raw.bin"
COUNT_LIMIT: int = 0                # stop after this many samples (0 = run until Ctrl+C)
STALL_TIMEOUT_S: float = 3.0        # recv() timeout -> "still waiting" warning, not an error
RCVBUF_BYTES: int = 8 * 1024 * 1024  # requested socket receive buffer (OS may cap it)
WRITE_QUEUE_BATCHES: int = 8192     # max batches buffered between receiver and disk writer
STATS_INTERVAL_S: float = 1.0       # live status print interval
CONVERT_TO_CSV: bool = True         # decode RAW_FILE -> OUT_FILE after capture
KEEP_RAW: bool = True               # keep RAW_FILE after a successful conversion
CONVERT_ONLY: bool = False          # True: skip capture, only convert existing RAW_FILE
BATCHES_PER_CHUNK: int = 256        # batches decoded/written per CSV chunk (~262k rows @1024)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def recv_exact_into(sock: socket.socket, view: memoryview) -> None:
    """!
    @brief Fill a memoryview completely; timeouts only print a warning and keep waiting.
    @param sock  Connected socket.
    @param view  Destination buffer.
    @throws ConnectionError when the server closes the connection.
    """
    n: int = len(view)
    got: int = 0
    while got < n:
        try:
            r: int = sock.recv_into(view[got:], n - got)
        except socket.timeout:
            print(f"WARNING: no data for {STALL_TIMEOUT_S:.1f}s "
                  f"(board stalled, network issue, or link down?) "
                  f"-- still waiting...", file=sys.stderr)
            continue
        if r == 0:
            raise ConnectionError("connection closed by server")
        got += r


def read_header(sock: socket.socket, hdr: bytearray, hdr_mv: memoryview) -> int:
    """!
    @brief Read one header; if it is invalid, slide byte-by-byte until a valid one is found.
    @param sock    Connected socket.
    @param hdr     20-byte header buffer.
    @param hdr_mv  memoryview of hdr.
    @return Number of bytes skipped while resynchronising.
    """
    recv_exact_into(sock, hdr_mv)
    skipped: int = 0
    while not header_is_valid(hdr):
        hdr[:-1] = hdr[1:]
        recv_exact_into(sock, hdr_mv[-1:])
        skipped += 1
    return skipped


def writer_thread(q: "queue.Queue[Optional[Tuple[bytes, bytearray]]]", f: object) -> None:
    """!
    @brief Drain the queue to disk so disk hiccups never block the socket.
    @param q  Queue of (record prefix + header, payload); None terminates.
    @param f  Open binary file.
    """
    while True:
        item = q.get()
        if item is None:
            break
        f.write(item[0])   # type: ignore[attr-defined]
        f.write(item[1])   # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Capture phase
# ---------------------------------------------------------------------------
def capture() -> None:
    """!
    @brief Receive the stream into RAW_FILE until Ctrl+C, COUNT_LIMIT or disconnect.
    """
    os.makedirs(os.path.dirname(RAW_FILE) or ".", exist_ok=True)

    print(f"Connecting to {HOST}:{PORT} ...")
    sock: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_BYTES)
    sock.settimeout(5)
    sock.connect((HOST, PORT))
    sock.settimeout(STALL_TIMEOUT_S)
    actual_rcvbuf: int = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    print(f"Connected. Socket receive buffer: {actual_rcvbuf / 1024:.0f} KiB "
          f"(requested {RCVBUF_BYTES / 1024:.0f} KiB)")

    f = open(RAW_FILE, "wb", buffering=4 * 1024 * 1024)
    q: "queue.Queue[Optional[Tuple[bytes, bytearray]]]" = queue.Queue(maxsize=WRITE_QUEUE_BATCHES)
    wt: threading.Thread = threading.Thread(target=writer_thread, args=(q, f), daemon=True)
    wt.start()

    hdr: bytearray = bytearray(HEADER_LEN)
    hdr_mv: memoryview = memoryview(hdr)

    total_samples: int = 0
    batches: int = 0
    lost_frames: int = 0
    discontinuities: int = 0
    lost_batches: int = 0
    resync_bytes: int = 0
    last_seq: Optional[int] = None
    prev_ctr: Optional[int] = None
    first_batch: bool = True
    sample_rate_hz: int = 0

    t_start: float = time.time()
    t_stat: float = t_start
    stat_batches: int = 0
    stat_samples: int = 0

    try:
        while True:
            resync_bytes += read_header(sock, hdr, hdr_mv)
            (_magic, seq, word_count, sample_rate_hz,
             _mask, _bits, _r0, _r1) = struct.unpack(HEADER_FMT, hdr)

            payload: bytearray = bytearray(word_count * ADC_ENTRY_SIZE)
            recv_exact_into(sock, memoryview(payload))
            t_recv: float = time.time()

            q.put((REC_PREFIX_STRUCT.pack(t_recv) + bytes(hdr), payload))

            # --- live loss accounting (vectorised, ~tens of microseconds) ---
            if last_seq is not None:
                gap: int = (seq - last_seq - 1) & 0xFFFFFFFF
                if gap < 0x80000000:      # ignore backwards jumps (e.g. board restart)
                    lost_batches += gap
            last_seq = seq

            a: np.ndarray = np.frombuffer(payload, dtype=np.uint8).reshape(-1, ADC_ENTRY_SIZE)
            ctr: np.ndarray = decode_counter(a)
            lost, disc = count_lost(ctr, prev_ctr)
            if first_batch:
                if disc:
                    print("WARNING: hw_frame_ctr does not increment by 1 inside the "
                          "first batch. Either frames are being lost inside the board "
                          "or the byte order is wrong.", file=sys.stderr)
                else:
                    print("hw_frame_ctr sanity check passed: increments by 1 as expected.")
                first_batch = False
            lost_frames += lost
            discontinuities += disc
            prev_ctr = int(ctr[-1])

            total_samples += word_count
            batches += 1
            stat_batches += 1
            stat_samples += word_count

            now: float = t_recv
            if now - t_stat >= STATS_INTERVAL_S:
                dt: float = now - t_stat
                expected: int = total_samples + lost_frames
                loss_pct: float = 100.0 * lost_frames / expected if expected else 0.0
                print(f"[{now - t_start:7.1f}s] "
                      f"{stat_batches / dt:6.0f} batch/s | "
                      f"{stat_samples / dt / 1000:7.1f} kS/s | "
                      f"{stat_samples * ADC_ENTRY_SIZE / dt / 1e6:5.2f} MB/s | "
                      f"lost frames: {lost_frames} ({loss_pct:.3f}%) | "
                      f"gaps: {discontinuities} | lost batches: {lost_batches} | "
                      f"write queue: {q.qsize()}")
                t_stat = now
                stat_batches = 0
                stat_samples = 0

            if COUNT_LIMIT and total_samples >= COUNT_LIMIT:
                print("Reached COUNT_LIMIT, stopping.")
                break

    except KeyboardInterrupt:
        print("\nStopped by user.")
    except ConnectionError as e:
        print(f"Connection ended: {e}")
    finally:
        sock.close()
        q.put(None)
        wt.join()
        f.close()
        elapsed: float = time.time() - t_start
        expected = total_samples + lost_frames
        loss_pct = 100.0 * lost_frames / expected if expected else 0.0
        print("-" * 72)
        print(f"Captured {total_samples} samples in {batches} batches, {elapsed:.1f}s "
              f"({total_samples / max(elapsed, 1e-9) / 1000:.1f} kS/s avg, "
              f"sample rate {sample_rate_hz} Hz)")
        print(f"Lost frames: {lost_frames} of {expected} expected ({loss_pct:.4f}%), "
              f"{discontinuities} gaps, {lost_batches} batches missing by seq, "
              f"{resync_bytes} bytes skipped while resyncing")
        print(f"Raw capture: {RAW_FILE}")
        print("-" * 72)


# ---------------------------------------------------------------------------
# Convert phase: raw -> CSV
# ---------------------------------------------------------------------------
def convert(raw_path: str, csv_path: str) -> bool:
    """!
    @brief Convert a raw capture into the CSV format with a console progress display.
    @param raw_path  Raw (.bin) file.
    @param csv_path  Output CSV.
    @return True on success.
    """
    if not os.path.exists(raw_path):
        print(f"Raw file not found: {raw_path}", file=sys.stderr)
        return False
    t0: float = time.time()

    def progress(done: int, total: int) -> bool:
        print(f"  {100.0 * done / max(total, 1):5.1f}% ({done}/{total} rows, {time.time() - t0:.1f}s)", end="\r")
        return True

    print(f"Converting {raw_path} -> {csv_path}")
    rows: int = convert_raw_to_csv(raw_path, csv_path, BATCHES_PER_CHUNK, progress)
    print(f"\nWrote {rows} samples to {csv_path} in {time.time() - t0:.1f}s")
    return rows > 0


def main() -> None:
    """!
    @brief Capture (unless CONVERT_ONLY) and convert according to the static configuration.
    """
    if not CONVERT_ONLY:
        capture()
    if CONVERT_TO_CSV or CONVERT_ONLY:
        ok: bool = convert(RAW_FILE, OUT_FILE)
        if ok and not KEEP_RAW:
            os.remove(RAW_FILE)
            print(f"Removed {RAW_FILE}")


if __name__ == "__main__":
    main()
