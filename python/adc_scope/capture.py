"""!
@file capture.py
@brief Capture of the live stream to disk: raw binary while streaming, CSV afterwards.

The receiver thread hands every received chunk to CaptureSession.feed(), which only
estimates the resulting CSV size and queues one bytes object for a writer thread, so the
hot path stays cheap even at ~1 MS/s. When the capture stops (button or size limit),
ConversionJob decodes the raw file into the established CSV format in the background.
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from adc_common import protocol
from adc_common.protocol import CSV_HEADER, REC_PREFIX_STRUCT

from . import config


def next_capture_name(name: str) -> str:
    """!
    @brief Name proposed for the next capture: increments a trailing number.

    "mic_test_007" -> "mic_test_008", "run9" -> "run10", "mic_test" -> "mic_test_1".

    @param name  Current name (without extension).
    @return Incremented name, zero padding preserved.
    """
    m = re.search(r"(\d+)$", name)
    if not m:
        return f"{name}_1"
    digits: str = m.group(1)
    return name[:m.start()] + str(int(digits) + 1).zfill(len(digits))


def sanitize_name(name: str) -> str:
    """!
    @brief Make a user-entered name safe as a file name.
    @param name  Raw text from the name field.
    @return Name with path separators and reserved characters replaced (never empty).
    """
    s: str = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    s = s.rstrip(". ")
    if s.lower().endswith(".csv"):
        s = s[:-4]
    return s or config.CAPTURE_DEFAULT_NAME


def unique_capture_name(directory: str, name: str) -> str:
    """!
    @brief First name, starting at 'name', whose CSV and raw files do not exist yet.
    @param directory  Capture folder.
    @param name       Wanted name.
    @return Free name (possibly incremented).
    """
    n: str = name
    while (os.path.exists(os.path.join(directory, n + ".csv"))
           or os.path.exists(os.path.join(directory, n + ".bin"))):
        n = next_capture_name(n)
    return n


@dataclass
class CaptureStats:
    """!
    @brief Live counters of a capture session.
    """
    samples: int = 0
    batches: int = 0
    csv_bytes: int = 0          #!< estimated size of the final CSV
    raw_bytes: int = 0
    lost_frames: int = 0
    started: float = 0.0
    stopped: float = 0.0
    limit_reached: bool = False


class CaptureSession:
    """!
    @brief One capture: raw file writer fed by the stream receiver thread.
    """

    def __init__(self, directory: str, name: str, limit_bytes: int, keep_raw: bool = False) -> None:
        """!
        @brief Prepare a capture (nothing is opened yet).
        @param directory    Output folder (created if needed).
        @param name         Base file name without extension.
        @param limit_bytes  Maximum size of the final CSV file.
        @param keep_raw     Keep the .bin file after a successful conversion.
        """
        self.directory: str = directory
        self.name: str = name
        self.limit_bytes: int = int(limit_bytes)
        self.keep_raw: bool = keep_raw
        self.csv_path: str = os.path.join(directory, name + ".csv")
        self.raw_path: str = os.path.join(directory, name + ".bin")
        self.stats: CaptureStats = CaptureStats()
        self.active: bool = False
        self.error: Optional[str] = None
        self._q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=config.CAPTURE_WRITE_QUEUE)
        self._file: Optional[object] = None
        self._thread: Optional[threading.Thread] = None
        self._lock: threading.Lock = threading.Lock()

    def start(self) -> None:
        """!
        @brief Open the raw file and start the writer thread.
        @throws OSError when the file cannot be created.
        """
        os.makedirs(self.directory, exist_ok=True)
        self._file = open(self.raw_path, "wb", buffering=4 * 1024 * 1024)
        self.stats = CaptureStats(started=time.time(), csv_bytes=len(CSV_HEADER) + len(os.linesep))
        self._thread = threading.Thread(target=self._writer, name="capture-writer", daemon=True)
        self._thread.start()
        self.active = True

    def _writer(self) -> None:
        """!
        @brief Writer thread: drains the queue to disk so disk hiccups never block the socket.
        """
        f = self._file
        assert f is not None
        try:
            while True:
                item: Optional[bytes] = self._q.get()
                if item is None:
                    break
                f.write(item)          # type: ignore[attr-defined]
        except OSError as e:
            self.error = f"write failed: {e}"
            self.active = False

    def feed(self, recv_time: float, frames: Sequence[Tuple[bytes, bytes, int, int, int]],
             ctr: np.ndarray, codes: np.ndarray, lost: int) -> bool:
        """!
        @brief Queue the frames of one received chunk (called from the receiver thread).

        Frames are accepted whole and in order until the estimated CSV size would exceed
        the limit; then the session marks itself full and refuses further data.

        @param recv_time  Receive timestamp of the chunk (seconds since the epoch).
        @param frames     List of (header bytes, payload bytes, seq, sample_rate_hz, word_count).
        @param ctr        uint32 counters of all samples in the chunk.
        @param codes      int32 signed codes (NUM_CHANNELS, N) of all samples in the chunk.
        @param lost       Frames lost inside / before this chunk.
        @return True while the session still accepts data.
        """
        if not self.active:
            return False
        raw_u: np.ndarray = protocol.to_unsigned(codes)
        accepted: List[bytes] = []
        prefix: bytes = REC_PREFIX_STRUCT.pack(recv_time)
        pos: int = 0
        added_csv: int = 0
        added_samples: int = 0
        full: bool = False
        for header, payload, seq, rate, wc in frames:
            row: np.ndarray = protocol.csv_row_bytes(recv_time, seq, rate, ctr[pos:pos + wc], raw_u[:, pos:pos + wc])
            nbytes: int = int(row.sum())
            if self.stats.csv_bytes + added_csv + nbytes > self.limit_bytes:
                full = True
                break
            accepted += (prefix, header, payload)
            added_csv += nbytes
            added_samples += wc
            pos += wc
        if accepted:
            blob: bytes = b"".join(accepted)
            self._q.put(blob)
            with self._lock:
                self.stats.csv_bytes += added_csv
                self.stats.raw_bytes += len(blob)
                self.stats.samples += added_samples
                self.stats.batches += len(accepted) // 3
                self.stats.lost_frames += lost
        if full:
            self.stats.limit_reached = True
            self.active = False
        return not full

    def stop(self) -> None:
        """!
        @brief Stop accepting data, flush the writer and close the raw file.
        """
        self.active = False
        if self._thread is not None:
            self._q.put(None)
            self._thread.join()
            self._thread = None
        if self._file is not None:
            self._file.close()           # type: ignore[attr-defined]
            self._file = None
        self.stats.stopped = time.time()

    @property
    def fill_fraction(self) -> float:
        """!
        @brief Estimated CSV size relative to the limit.
        @return Fraction 0..1.
        """
        return min(1.0, self.stats.csv_bytes / self.limit_bytes) if self.limit_bytes else 0.0


class ConversionJob:
    """!
    @brief Background raw -> CSV conversion of a finished capture.
    """

    def __init__(self, session: CaptureSession) -> None:
        """!
        @brief Prepare the conversion of a stopped session.
        @param session  Finished capture session.
        """
        self.session: CaptureSession = session
        self.rows_done: int = 0
        self.rows_total: int = max(session.stats.samples, 1)
        self.done: bool = False
        self.cancelled: bool = False
        self.error: Optional[str] = None
        self._cancel: threading.Event = threading.Event()
        self._thread: threading.Thread = threading.Thread(target=self._run, name="csv-convert", daemon=True)

    def start(self) -> None:
        """!
        @brief Start the conversion thread.
        """
        self._thread.start()

    def cancel(self) -> None:
        """!
        @brief Request cancellation (the partial CSV is left on disk, the raw file is kept).
        """
        self._cancel.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """!
        @brief Wait for completion.
        @param timeout  Seconds, None = forever.
        @return True when finished.
        """
        self._thread.join(timeout)
        return self.done

    @property
    def fraction(self) -> float:
        """!
        @brief Conversion progress.
        @return Fraction 0..1.
        """
        return min(1.0, self.rows_done / self.rows_total)

    def _progress(self, done: int, total: int) -> bool:
        """!
        @brief Progress callback of convert_raw_to_csv().
        @param done   Rows written.
        @param total  Rows to write.
        @return False to cancel.
        """
        self.rows_done, self.rows_total = done, max(total, 1)
        return not self._cancel.is_set()

    def _run(self) -> None:
        """!
        @brief Thread body: convert, then delete the raw file unless it has to be kept.
        """
        s: CaptureSession = self.session
        try:
            rows: int = protocol.convert_raw_to_csv(s.raw_path, s.csv_path, config.CAPTURE_BATCHES_PER_CHUNK,
                                                    self._progress)
            if rows < 0:
                self.cancelled = True
            else:
                self.rows_done = rows
                if not s.keep_raw:
                    os.remove(s.raw_path)
        except Exception as e:  # noqa: BLE001 - reported to the GUI
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.done = True
