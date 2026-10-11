#!/usr/bin/env python3
"""
adc_stream_logger.py

Connects to the Zynq ADC stream TCP server (adc_stream_thread, port 5001)
and logs decoded 4-channel samples to CSV.

All connection/output settings are static constants below -- just run
    python adc_stream_logger.py
and it connects, logs, and (optionally) stops after COUNT_LIMIT samples.

Wire format (matches the CURRENT adc_controller.v / adc_hw.c / adc_stream.c):

    header (20 bytes, native little-endian struct on Zynq/ARM):
        magic(u32) seq(u32) word_count(u32) sample_rate_hz(u32)
        channel_mask(u8) bits_per_sample(u8) reserved(2x u8)
    (bits_per_sample in this header is currently a stale/unused field left
    over from an earlier RTL revision -- the real per-channel width is fixed
    at 24 bits in the current design. Don't rely on it.)

    payload: word_count x entries, ADC_ENTRY_SIZE = 16 bytes each.

    Each entry is built in adc_controller.v as:
        word_data <= {cont_counter, data_reg_0, data_reg_1, data_reg_2, data_reg_3};
    Verilog concatenation puts the FIRST item at the most-significant end, and
    AXI4-Stream byte convention puts tdata[7:0] at the lowest memory address,
    so working from word_data[127:0] down to word_data[0] maps to INCREASING
    byte address -- which puts data_reg_3 (the LAST item) at byte 0:

        bytes 0-2   : ch3   (u24, LITTLE-endian)
        bytes 3-5   : ch2   (u24, little-endian)
        bytes 6-8   : ch1   (u24, little-endian)
        bytes 9-11  : ch0   (u24, little-endian)
        bytes 12-15 : hw_frame_ctr (u32, little-endian) -- a free-running
                      counter incremented once per ADC frame IN THE RTL
                      (cont_counter), NOT the same thing as the TCP header's
                      `frame_seq` (which counts DMA *batches*, not individual
                      ADC entries).

    THIS BYTE ORDER IS DERIVED FROM READING THE VERILOG, NOT CONFIRMED ON A
    CAPTURE. hw_frame_ctr should increment by exactly 1 every entry -- the
    script checks this on the first batch and prints a loud warning if it
    doesn't, since that's the single cheapest way to confirm (or refute) the
    byte order above without needing a logic analyzer.
"""

import csv
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# Static configuration -- edit these instead of passing CLI args.
# ---------------------------------------------------------------------------
HOST = "192.168.1.10"          # board IP
PORT = 5001                    # ADC_STREAM_PORT in adc_stream.h
OUT_FILE = "captures/adc_stream_samples.csv"
COUNT_LIMIT = 0                # stop after this many samples (0 = run until Ctrl+C)
STALL_TIMEOUT_S = 3.0          # recv() timeout -> "still waiting" warning, not an error
ROWS_PER_FLUSH = 2000          # batch CSV writes to avoid stalling the board's socket
# ---------------------------------------------------------------------------

MAGIC = 0x41444331  # 'ADC1'
HEADER_FMT = "<IIIIBBBB"       # matches adc_frame_header_t (native struct packing, LE)
HEADER_LEN = struct.calcsize(HEADER_FMT)  # 20 bytes

ADC_ENTRY_SIZE = 16             # matches ADC_ENTRY_SIZE in adc_hw.h (current design)
NUM_CHANNELS = 4                 # matches ADC_NUM_CHANNELS in adc_hw.h


def recv_exact(sock, n):
    """Read exactly n bytes or raise ConnectionError on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by server")
        buf += chunk
    return bytes(buf)


def decode_entry(entry):
    """Decode one 16-byte entry as packed by adc_controller.v's word_data concat.
    See the byte-order derivation in the module docstring."""
    ch3 = int.from_bytes(entry[0:3],  "little", signed=False)
    ch2 = int.from_bytes(entry[3:6],  "little", signed=False)
    ch1 = int.from_bytes(entry[6:9],  "little", signed=False)
    ch0 = int.from_bytes(entry[9:12], "little", signed=False)
    hw_frame_ctr = int.from_bytes(entry[12:16], "little", signed=False)
    return hw_frame_ctr, ch0, ch1, ch2, ch3

def main():
    print(f"Connecting to {HOST}:{PORT} ...")
    sock = socket.create_connection((HOST, PORT), timeout=5)
    # A finite timeout turns a silent hang (board stalls, connection goes
    # quiet) into a visible, recoverable event instead of recv() blocking
    # forever with no output at all.
    sock.settimeout(STALL_TIMEOUT_S)

    total_samples = 0
    frames = 0
    t_start = time.time()
    t_last_data = time.time()

    checked_counter_order = False
    last_hw_ctr = None

    # Buffer rows and flush in batches instead of calling writer.writerow()
    # per sample. A synchronous disk write on every single sample is slow
    # enough to fall behind the incoming stream, which fills the TCP
    # receive window, which stalls the board's blocking lwip_write() --
    # both sides can end up stuck with zero error output. Batching removes
    # this as a bottleneck.
    row_buffer = []

    with open(OUT_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["recv_time", "batch_seq", "sample_rate_hz",
                          "hw_frame_ctr", "ch0", "ch1", "ch2", "ch3"])

        try:
            while True:
                try:
                    hdr_bytes = recv_exact(sock, HEADER_LEN)
                except socket.timeout:
                    idle_s = time.time() - t_last_data
                    print(f"WARNING: no data for {idle_s:.1f}s "
                          f"(board stalled, network issue, or link down?) "
                          f"-- still waiting...", file=sys.stderr)
                    continue  # keep waiting; doesn't count as a real error

                magic, batch_seq, word_count, sample_rate_hz, chan_mask, bits, r0, r1 = \
                    struct.unpack(HEADER_FMT, hdr_bytes)

                if magic != MAGIC:
                    print(f"WARNING: bad magic 0x{magic:08X}, resyncing...",
                          file=sys.stderr)
                    continue  # drop this "frame"; next recv attempts to resync

                payload = recv_exact(sock, word_count * ADC_ENTRY_SIZE)
                t_last_data = time.time()
                recv_time = t_last_data

                for i in range(word_count):
                    entry = payload[i * ADC_ENTRY_SIZE:(i + 1) * ADC_ENTRY_SIZE]
                    hw_frame_ctr, ch0, ch1, ch2, ch3 = decode_entry(entry)
                    row_buffer.append([f"{recv_time:.6f}", batch_seq, sample_rate_hz,
                                        hw_frame_ctr, ch0, ch1, ch2, ch3])

                    if not checked_counter_order:
                        if last_hw_ctr is not None and hw_frame_ctr != (last_hw_ctr + 1) & 0xFFFFFFFF:
                            print(f"WARNING: hw_frame_ctr did not increment by 1 "
                                  f"({last_hw_ctr} -> {hw_frame_ctr}). The byte-order "
                                  f"derivation in decode_entry() is likely WRONG -- "
                                  f"try reversing the byte order (big-endian instead "
                                  f"of little-endian, or vice versa) before trusting "
                                  f"any of this data.", file=sys.stderr)
                            checked_counter_order = True  # only warn once
                        elif i > 5:
                            print("hw_frame_ctr sanity check passed: increments by 1 as expected.")
                            checked_counter_order = True
                        last_hw_ctr = hw_frame_ctr

                if len(row_buffer) >= ROWS_PER_FLUSH:
                    writer.writerows(row_buffer)
                    row_buffer.clear()

                total_samples += word_count
                frames += 1
                if frames % 200 == 0:
                    elapsed = time.time() - t_start
                    rate = total_samples / elapsed if elapsed > 0 else 0
                    print(f"batches={frames} samples={total_samples} "
                          f"({rate/1000:.1f} kS/s avg)")

                if COUNT_LIMIT and total_samples >= COUNT_LIMIT:
                    print("Reached COUNT_LIMIT, stopping.")
                    break

        except KeyboardInterrupt:
            print("\nStopped by user.")
        except ConnectionError as e:
            print(f"Connection ended: {e}")
        finally:
            if row_buffer:
                writer.writerows(row_buffer)
                row_buffer.clear()
            sock.close()
            elapsed = time.time() - t_start
            print(f"Wrote {total_samples} samples ({frames} batches) to {OUT_FILE} "
                  f"in {elapsed:.1f}s")


if __name__ == "__main__":
    main()