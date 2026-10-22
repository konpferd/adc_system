"""!
@file protocol.py
@brief ADC stream wire format, sample decoding, frame-loss accounting and capture files.

Stream (TCP, port 5001, one client at a time), little-endian:

    header (20 bytes):
        magic(u32) = 0x41444331 ('ADC1')   seq(u32)   word_count(u32)
        sample_rate_hz(u32)   channel_mask(u8)   bits_per_sample(u8)   reserved(2 x u8)

    payload: word_count entries of 16 bytes:
        bytes 0-2   : ch3 (u24)
        bytes 3-5   : ch2 (u24)
        bytes 6-8   : ch1 (u24)
        bytes 9-11  : ch0 (u24)
        bytes 12-15 : hw_frame_ctr (u32), +1 per ADC conversion frame

Raw capture file (".bin"): a sequence of records
    recv_time (f64) | 20-byte header | payload

CSV capture file (one row per sample, raw unsigned 24-bit channel codes):
    recv_time,batch_seq,sample_rate_hz,hw_frame_ctr,ch0,ch1,ch2,ch3
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------
STREAM_PORT: int = 5001                    #!< ADC_STREAM_PORT in the firmware
CONSOLE_PORT: int = 5000                   #!< CONSOLE_PORT in console.h
DEFAULT_DEVICE_IP: str = "192.168.1.10"    #!< static IP set in main.c
FRAME_MAGIC: int = 0x41444331              #!< 'ADC1'
FRAME_MAGIC_BYTES: bytes = struct.pack("<I", FRAME_MAGIC)
HEADER_FMT: str = "<IIIIBBBB"
HEADER_STRUCT: struct.Struct = struct.Struct(HEADER_FMT)
HEADER_LEN: int = HEADER_STRUCT.size       #!< 20 bytes
ENTRY_SIZE: int = 16                       #!< ADC_ENTRY_SIZE in adc_hw.h
MAX_WORDS_SANE: int = 1 << 20              #!< header sanity limit used for resynchronisation
NUM_CHANNELS: int = 4
ADC_BITS: int = 24
CODE_MASK: int = (1 << ADC_BITS) - 1
SIGNED_MAX: int = (1 << (ADC_BITS - 1)) - 1
SIGNED_MIN: int = -(1 << (ADC_BITS - 1))
COUNTER_MOD: int = 1 << 32
CHANNEL_BYTE_OFFSET: Tuple[int, int, int, int] = (9, 6, 3, 0)   #!< byte offset of ch0..ch3
COUNTER_BYTE_OFFSET: int = 12
REC_PREFIX_STRUCT: struct.Struct = struct.Struct("<d")
REC_PREFIX_LEN: int = REC_PREFIX_STRUCT.size  #!< 8 bytes
CSV_HEADER: str = "recv_time,batch_seq,sample_rate_hz,hw_frame_ctr,ch0,ch1,ch2,ch3"
CSV_FLOAT_FORMAT: str = "%.6f"


# --------------------------------------------------------------------------------------
# Frame header
# --------------------------------------------------------------------------------------
@dataclass
class FrameHeader:
    """!
    @brief Decoded 20-byte stream frame header (adc_frame_header_t in adc_stream.c).
    """
    magic: int
    seq: int
    word_count: int
    sample_rate_hz: int
    channel_mask: int
    bits_per_sample: int

    @classmethod
    def unpack(cls, buf: bytes | bytearray | memoryview, offset: int = 0) -> "FrameHeader":
        """!
        @brief Decode a header from a byte buffer.
        @param buf     Buffer that holds at least HEADER_LEN bytes after offset.
        @param offset  Byte offset of the header inside buf.
        @return The decoded header (not validated).
        """
        magic, seq, wc, fs, mask, bits, _r0, _r1 = HEADER_STRUCT.unpack_from(buf, offset)
        return cls(magic, seq, wc, fs, mask, bits)

    def pack(self) -> bytes:
        """!
        @brief Encode the header into its 20-byte wire representation.
        @return Header bytes.
        """
        return HEADER_STRUCT.pack(self.magic, self.seq & 0xFFFFFFFF, self.word_count,
                                  self.sample_rate_hz, self.channel_mask, self.bits_per_sample, 0, 0)

    def is_valid(self) -> bool:
        """!
        @brief Check the magic word and the word count sanity limit.
        @return True when the header can be trusted.
        """
        return self.magic == FRAME_MAGIC and 0 < self.word_count <= MAX_WORDS_SANE


def header_is_valid(buf: bytes | bytearray | memoryview, offset: int = 0) -> bool:
    """!
    @brief Fast validity check of a header located inside a buffer.
    @param buf     Buffer holding the header.
    @param offset  Byte offset of the header.
    @return True when magic matches and word_count is within 1..MAX_WORDS_SANE.
    """
    magic, _seq, wc = struct.unpack_from("<III", buf, offset)
    return magic == FRAME_MAGIC and 0 < wc <= MAX_WORDS_SANE


# --------------------------------------------------------------------------------------
# Sample decoding / encoding
# --------------------------------------------------------------------------------------
def decode_u24(entries: np.ndarray, offset: int) -> np.ndarray:
    """!
    @brief Decode one little-endian 24-bit field from an (N,16) entry array.
    @param entries  uint8 array of shape (N, ENTRY_SIZE).
    @param offset   Byte offset of the field inside each entry.
    @return uint32 array (N,) with the unsigned 24-bit codes.
    """
    return (entries[:, offset].astype(np.uint32)
            | (entries[:, offset + 1].astype(np.uint32) << 8)
            | (entries[:, offset + 2].astype(np.uint32) << 16))


def decode_counter(entries: np.ndarray) -> np.ndarray:
    """!
    @brief Extract hw_frame_ctr from an (N,16) entry array.
    @param entries  uint8 array of shape (N, ENTRY_SIZE).
    @return uint32 array (N,).
    """
    return np.ascontiguousarray(entries[:, COUNTER_BYTE_OFFSET:COUNTER_BYTE_OFFSET + 4]).view("<u4").ravel()


def decode_entries(entries: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """!
    @brief Decode counter and raw unsigned channel codes (same layout as the CSV columns).
    @param entries  uint8 array of shape (N, ENTRY_SIZE).
    @return Tuple (hw_frame_ctr, ch0, ch1, ch2, ch3), all uint32 arrays of length N.
    """
    return (decode_counter(entries),
            decode_u24(entries, CHANNEL_BYTE_OFFSET[0]), decode_u24(entries, CHANNEL_BYTE_OFFSET[1]),
            decode_u24(entries, CHANNEL_BYTE_OFFSET[2]), decode_u24(entries, CHANNEL_BYTE_OFFSET[3]))


def decode_signed_block(entries: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """!
    @brief Decode all channels as sign-extended codes in one channel-major block.

    Sign extension is done with an arithmetic shift: (u24 << 8) as int32 >> 8.

    @param entries  uint8 array of shape (N, ENTRY_SIZE).
    @return Tuple (codes, ctr): codes is int32 (NUM_CHANNELS, N), ctr is uint32 (N,).
    """
    n: int = entries.shape[0]
    codes: np.ndarray = np.empty((NUM_CHANNELS, n), dtype=np.int32)
    for ch, off in enumerate(CHANNEL_BYTE_OFFSET):
        u: np.ndarray = ((entries[:, off].astype(np.int32) << 8)
                         | (entries[:, off + 1].astype(np.int32) << 16)
                         | (entries[:, off + 2].astype(np.int32) << 24))
        codes[ch] = u >> 8
    return codes, decode_counter(entries)


def to_signed(raw: np.ndarray, bits: int = ADC_BITS) -> np.ndarray:
    """!
    @brief Interpret unsigned raw codes as two's complement values.
    @param raw   Unsigned codes (any integer dtype).
    @param bits  Code width in bits.
    @return int32 array of signed codes.
    """
    x: np.ndarray = raw.astype(np.int64)
    sign_bit: int = 1 << (bits - 1)
    return ((x ^ sign_bit) - sign_bit).astype(np.int32)


def to_unsigned(signed: np.ndarray) -> np.ndarray:
    """!
    @brief Convert signed codes back to the raw unsigned 24-bit representation.
    @param signed  Signed codes (any integer dtype).
    @return uint32 array with values 0..2^24-1.
    """
    return (signed.astype(np.int64) & CODE_MASK).astype(np.uint32)


def encode_entries(codes: np.ndarray, counter_start: int) -> bytes:
    """!
    @brief Build a payload from signed codes (inverse of decode_signed_block).
    @param codes          Integer array (N, NUM_CHANNELS) of signed codes, ch0 in column 0.
    @param counter_start  hw_frame_ctr value of the first entry (wraps at 2^32).
    @return Payload bytes, N * ENTRY_SIZE long.
    """
    n: int = codes.shape[0]
    u: np.ndarray = (codes.astype(np.int64) & CODE_MASK).astype(np.uint32)
    ctr: np.ndarray = ((counter_start + np.arange(n, dtype=np.uint64)) & 0xFFFFFFFF).astype(np.uint32)
    arr: np.ndarray = np.zeros((n, ENTRY_SIZE), dtype=np.uint8)
    for ch, off in enumerate(CHANNEL_BYTE_OFFSET):
        v: np.ndarray = u[:, ch]
        arr[:, off] = v & 0xFF
        arr[:, off + 1] = (v >> 8) & 0xFF
        arr[:, off + 2] = (v >> 16) & 0xFF
    arr[:, COUNTER_BYTE_OFFSET:COUNTER_BYTE_OFFSET + 4] = ctr.view(np.uint8).reshape(n, 4)
    return arr.tobytes()


def pack_frame(codes: np.ndarray, counter_start: int, seq: int, sample_rate_hz: int) -> bytes:
    """!
    @brief Build a complete stream frame (header + payload), byte-identical to the firmware.
    @param codes           Integer array (N, NUM_CHANNELS) of signed codes.
    @param counter_start   hw_frame_ctr of the first entry.
    @param seq             Batch sequence number.
    @param sample_rate_hz  Sample rate written into the header.
    @return Frame bytes.
    """
    hdr: FrameHeader = FrameHeader(FRAME_MAGIC, seq, codes.shape[0], int(sample_rate_hz), 0x0F, ADC_BITS)
    return hdr.pack() + encode_entries(codes, counter_start)


# --------------------------------------------------------------------------------------
# Frame-loss accounting
# --------------------------------------------------------------------------------------
def counter_steps(ctr: np.ndarray, prev_last: Optional[int]) -> np.ndarray:
    """!
    @brief Wrap-safe differences of consecutive hw_frame_ctr values.
    @param ctr        uint32 counter values of the new block.
    @param prev_last  Last counter value of the previous block, or None for the first block.
    @return int64 array of steps (1 = continuous), one per transition.
    """
    c: np.ndarray = ctr.astype(np.int64)
    if prev_last is not None:
        c = np.concatenate((np.array([prev_last], dtype=np.int64), c))
    return np.diff(c) & 0xFFFFFFFF


def count_lost(ctr: np.ndarray, prev_last: Optional[int]) -> Tuple[int, int]:
    """!
    @brief Count lost frames from the 32-bit hw_frame_ctr.

    A step of 1 is continuous. A step k > 1 means k-1 frames were lost. Backward jumps
    (step >= 2^31, e.g. after a device reset) are counted as discontinuities only.

    @param ctr        uint32 counter values of the new block.
    @param prev_last  Last counter value of the previous block, or None.
    @return Tuple (lost_frames, discontinuities).
    """
    d: np.ndarray = counter_steps(ctr, prev_last)
    bad: np.ndarray = d != 1
    n_disc: int = int(bad.sum())
    if not n_disc:
        return 0, 0
    fwd: np.ndarray = bad & (d > 1) & (d < (1 << 31))
    lost: int = int((d[fwd] - 1).sum())
    return lost, n_disc


# --------------------------------------------------------------------------------------
# CSV size estimation (used for the capture fill meter)
# --------------------------------------------------------------------------------------
def decimal_digits(x: np.ndarray) -> np.ndarray:
    """!
    @brief Number of decimal digits of non-negative integers (0 has 1 digit).
    @param x  Non-negative integer array.
    @return int64 array of digit counts.
    """
    v: np.ndarray = x.astype(np.int64)
    d: np.ndarray = np.ones(v.shape, dtype=np.int64)
    p: int = 10
    while True:
        m: np.ndarray = v >= p
        if not m.any():
            return d
        d += m
        p *= 10


def csv_row_bytes(recv_time: float, seq: int, sample_rate_hz: int,
                  ctr: np.ndarray, raw_channels: np.ndarray, newline_len: int = len(os.linesep)) -> np.ndarray:
    """!
    @brief Exact byte length of each CSV row a batch will produce.
    @param recv_time       Batch receive timestamp (formatted with CSV_FLOAT_FORMAT).
    @param seq             Batch sequence number.
    @param sample_rate_hz  Header sample rate.
    @param ctr             uint32 counters of the batch (N,).
    @param raw_channels    Unsigned codes (NUM_CHANNELS, N).
    @param newline_len     Line terminator length written by pandas (os.linesep).
    @return int64 array (N,) of row lengths in bytes.
    """
    const: int = (len(CSV_FLOAT_FORMAT % recv_time) + len(str(seq)) + len(str(sample_rate_hz))
                  + 7 + newline_len)
    n: np.ndarray = decimal_digits(ctr) + const
    for ch in range(raw_channels.shape[0]):
        n += decimal_digits(raw_channels[ch])
    return n


# --------------------------------------------------------------------------------------
# Raw capture file -> CSV
# --------------------------------------------------------------------------------------
@dataclass
class RawIndex:
    """!
    @brief Batch index of a raw capture file.
    """
    times: List[float]
    seqs: List[int]
    rates: List[int]
    offsets: List[int]
    word_counts: List[int]
    truncated: bool = False

    @property
    def total_rows(self) -> int:
        """!
        @brief Total number of samples in the indexed file.
        @return Sum of all word counts.
        """
        return int(sum(self.word_counts))


def make_raw_record(recv_time: float, header: bytes | bytearray | memoryview,
                    payload: bytes | bytearray | memoryview) -> bytes:
    """!
    @brief Build one raw capture record.
    @param recv_time  Receive timestamp (seconds since epoch).
    @param header     20-byte stream header.
    @param payload    Frame payload.
    @return Record bytes (prefix + header + payload).
    """
    return REC_PREFIX_STRUCT.pack(recv_time) + bytes(header) + bytes(payload)


def index_raw_file(mm: np.ndarray) -> RawIndex:
    """!
    @brief Walk the records of a memory-mapped raw capture file.
    @param mm  uint8 memmap of the whole file.
    @return Batch index. Stops at the first corrupt or truncated record.
    """
    size: int = int(mm.shape[0])
    idx: RawIndex = RawIndex([], [], [], [], [])
    off: int = 0
    while off + REC_PREFIX_LEN + HEADER_LEN <= size:
        (t,) = REC_PREFIX_STRUCT.unpack_from(mm, off)
        magic, seq, wc, sr = struct.unpack_from("<IIII", mm, off + REC_PREFIX_LEN)
        if magic != FRAME_MAGIC:
            idx.truncated = True
            break
        payload_off: int = off + REC_PREFIX_LEN + HEADER_LEN
        end: int = payload_off + wc * ENTRY_SIZE
        if end > size:
            idx.truncated = True
            break
        idx.times.append(t)
        idx.seqs.append(seq)
        idx.rates.append(sr)
        idx.offsets.append(payload_off)
        idx.word_counts.append(wc)
        off = end
    return idx


def convert_raw_to_csv(raw_path: str, csv_path: str, batches_per_chunk: int = 256,
                       progress: Optional[Callable[[int, int], bool]] = None) -> int:
    """!
    @brief Decode a raw capture file into the established CSV format.

    Decoding is vectorised with numpy and written in chunks through pandas, giving the same
    text as adc_logger_fast.py (float_format "%.6f", unsigned channel codes).

    @param raw_path           Input raw (.bin) file.
    @param csv_path           Output CSV file (overwritten).
    @param batches_per_chunk  Batches decoded per write chunk.
    @param progress           Optional callback(rows_done, total_rows); return False to cancel.
    @return Number of rows written, or -1 if cancelled.
    @throws FileNotFoundError when raw_path does not exist.
    """
    import pandas as pd  # lazy: only the converter needs pandas

    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    if os.path.getsize(raw_path) < REC_PREFIX_LEN + HEADER_LEN:
        with open(csv_path, "w", newline="") as out:
            out.write(CSV_HEADER + os.linesep)
        return 0

    mm: np.ndarray = np.memmap(raw_path, dtype=np.uint8, mode="r")
    try:
        idx: RawIndex = index_raw_file(mm)
        nb: int = len(idx.offsets)
        total: int = idx.total_rows
        done: int = 0
        with open(csv_path, "w", newline="") as out:
            out.write(CSV_HEADER + os.linesep)
            for i in range(0, nb, batches_per_chunk):
                sl: slice = slice(i, min(i + batches_per_chunk, nb))
                parts: List[np.ndarray] = [mm[o:o + w * ENTRY_SIZE]
                                           for o, w in zip(idx.offsets[sl], idx.word_counts[sl])]
                a: np.ndarray = np.concatenate(parts).reshape(-1, ENTRY_SIZE)
                ctr, ch0, ch1, ch2, ch3 = decode_entries(a)
                wc_arr: np.ndarray = np.array(idx.word_counts[sl])
                df = pd.DataFrame({
                    "recv_time": np.repeat(np.array(idx.times[sl]), wc_arr),
                    "batch_seq": np.repeat(np.array(idx.seqs[sl], dtype=np.uint32), wc_arr),
                    "sample_rate_hz": np.repeat(np.array(idx.rates[sl], dtype=np.uint32), wc_arr),
                    "hw_frame_ctr": ctr,
                    "ch0": ch0, "ch1": ch1, "ch2": ch2, "ch3": ch3,
                })
                df.to_csv(out, header=False, index=False, float_format=CSV_FLOAT_FORMAT)
                done += len(df)
                if progress is not None and progress(done, total) is False:
                    return -1
        return done
    finally:
        del mm


def now() -> float:
    """!
    @brief Wall-clock timestamp used for recv_time (seconds since the epoch).
    @return time.time().
    """
    return time.time()
