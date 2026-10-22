#!/usr/bin/env python3
"""!
@file test_backend.py
@brief Automated tests of the protocol, analysis and network backend (no Qt needed).

Starts the simulator's stream server and emulated console in-process on spare ports and
runs the adc_scope backend against them.

Run from the python/ folder:   python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from typing import List, Optional

import numpy as np

ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "test_generator"))

import adc_simulator as sim                                            # noqa: E402
from adc_common import ads127l14 as ads                                # noqa: E402
from adc_common import console_protocol as cp                          # noqa: E402
from adc_common import protocol                                        # noqa: E402
from adc_scope.acquisition import Acquisition, TriggerSettings         # noqa: E402
from adc_scope.capture import (CaptureSession, ConversionJob,          # noqa: E402
                               next_capture_name, sanitize_name)
from adc_scope.console_client import ConsoleClient, ConsoleEvent       # noqa: E402
from adc_scope.device_state import TOPIC_TEMP, DeviceState             # noqa: E402
from adc_scope.link_health import LinkHealth, LinkState, classify      # noqa: E402
from adc_scope.ring_buffer import SampleRing                           # noqa: E402
from adc_scope.signal_analysis import Edge, TriggerMode, decimate_minmax, measure  # noqa: E402
from adc_scope.stream_client import StreamReceiver                     # noqa: E402
from device_emulator import ConsoleServer                              # noqa: E402


def free_port() -> int:
    """!
    @brief Ask the OS for an unused TCP port.
    @return Port number.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestProtocol(unittest.TestCase):
    """!
    @brief Wire format and console text round trips.
    """

    def test_frame_roundtrip(self) -> None:
        """!
        @brief Encode random codes and decode them again, including the counter wrap.
        """
        rng: np.random.Generator = np.random.default_rng(1)
        codes: np.ndarray = rng.integers(protocol.SIGNED_MIN, protocol.SIGNED_MAX + 1, (777, 4))
        frame: bytes = protocol.pack_frame(codes, 0xFFFFFF00, 3, 800_000)
        hdr: protocol.FrameHeader = protocol.FrameHeader.unpack(frame)
        self.assertTrue(hdr.is_valid())
        self.assertEqual((hdr.seq, hdr.word_count, hdr.sample_rate_hz), (3, 777, 800_000))
        ent: np.ndarray = np.frombuffer(frame[protocol.HEADER_LEN:], np.uint8).reshape(-1, 16)
        sig, ctr = protocol.decode_signed_block(ent)
        np.testing.assert_array_equal(sig.T, codes)
        self.assertEqual(protocol.count_lost(ctr, None), (0, 0))
        self.assertEqual(protocol.count_lost(ctr[::3], None)[0], 2 * (len(ctr[::3]) - 1))
        ctr0, c0, _c1, _c2, _c3 = protocol.decode_entries(ent)
        np.testing.assert_array_equal(protocol.to_signed(c0), codes[:, 0])

    def test_backward_jump_is_not_loss(self) -> None:
        """!
        @brief A counter reset (device restart) must not count billions of lost frames.
        """
        lost, disc = protocol.count_lost(np.array([5, 6, 7], dtype=np.uint32), 1_000_000)
        self.assertEqual((lost, disc), (0, 1))

    def test_adc_settings_text_roundtrip(self) -> None:
        """!
        @brief Every configuration printed like the firmware parses back identically.
        """
        for speed in range(4):
            for clk in ads.CLK_DIV_VALUES:
                for dclk in ads.DCLK_DIV_VALUES:
                    for fltr in (0, 8, 9, 27, 31):
                        cfg: ads.AdcConfig = ads.AdcConfig(speed, clk, dclk, fltr)
                        rep = cp.parse_adc_settings(cp.format_adc_settings(cfg))
                        self.assertTrue(rep.valid)
                        self.assertEqual(rep.config, cfg)
                        self.assertEqual(bool(rep.warnings), bool(cfg.warnings()))

    def test_rates_match_firmware_table(self) -> None:
        """!
        @brief Spot-check the sample-rate formula against hand-computed values.
        """
        self.assertEqual(ads.AdcConfig().rate_hz(), 800_000)
        self.assertEqual(ads.sample_rate_hz(ads.FCLK_INT_HZ, 1, 8), 1_066_667)
        self.assertEqual(ads.sample_rate_hz(ads.FCLK_INT_HZ, 8, 31), 10)

    def test_command_order(self) -> None:
        """!
        @brief Speed mode / divider are ordered so every intermediate state is legal.
        """
        cur: ads.AdcConfig = ads.AdcConfig(speed_mode=2, clk_div=1)
        new: ads.AdcConfig = ads.AdcConfig(speed_mode=1, clk_div=2)
        self.assertEqual(new.command_sequence(cur), ["ADC SET CLK_DIV 2", "ADC SET SPEED_MODE 1"])
        self.assertEqual(cur.command_sequence(new), ["ADC SET SPEED_MODE 2", "ADC SET CLK_DIV 1"])

    def test_tokenizer_and_classes(self) -> None:
        """!
        @brief Prompt detection and line classification.
        """
        t: cp.ConsoleTokenizer = cp.ConsoleTokenizer()
        toks = t.feed(b"OK FLTR=9\r\n> DMA Timeout!\r\n\tS2MM_LEN:\t0x1\r")
        toks += t.feed(b"\n> ")
        self.assertEqual([k for k, _ in toks], ["line", "prompt", "line", "line", "prompt"])
        self.assertEqual(cp.classify_line("DMA Timeout!"), cp.LineKind.FAULT)
        self.assertEqual(cp.classify_line("\tS2MM_LEN:\t0x1"), cp.LineKind.DETAIL)
        self.assertEqual(cp.classify_line("XADC initialization error [ERR -3]"), cp.LineKind.FAULT)
        self.assertEqual(cp.classify_line("ERR unknown command"), cp.LineKind.ERR)
        self.assertEqual(cp.classify_line("[ADC CONFIG] Configuring X ... FAIL [ERR: -3]"), cp.LineKind.FAULT)


    def test_device_temp_reply(self) -> None:
        """!
        @brief DEVICE_TEMP text round trip, including negative temperatures.
        """
        t = cp.parse_device_temp_reply(cp.format_device_temp_reply(43512, -1020, 47100, 75))
        self.assertIsNotNone(t)
        self.assertEqual((t.now, t.min, t.max, t.warn_level), (43.512, -1.02, 47.1, 75.0))
        self.assertIsNone(cp.parse_device_temp_reply("ERR unknown command"))


class TestAnalysis(unittest.TestCase):
    """!
    @brief Measurements, trigger and decimation.
    """

    def test_measure_sine(self) -> None:
        """!
        @brief Frequency and amplitude of a noisy sine.
        """
        fs: float = 1_000_000.0
        t: np.ndarray = np.arange(200_000) / fs
        x: np.ndarray = 3.0 * np.sin(2 * np.pi * 12_345.6 * t) + 0.5 + np.random.default_rng(0).normal(0, 0.01, t.size)
        m = measure(x, fs)
        self.assertAlmostEqual(m.freq, 12_345.6, delta=0.5)
        self.assertAlmostEqual(m.vpp, 6.0, delta=0.1)
        self.assertAlmostEqual(m.vavg, 0.5, delta=0.01)
        self.assertAlmostEqual(m.vrms, float(np.sqrt(4.5 + 0.25)), delta=0.01)

    def test_trigger_alignment(self) -> None:
        """!
        @brief The trigger point lands at x = 0 on the rising edge at the requested level.
        """
        fs: float = 500_000.0
        ring: SampleRing = SampleRing(1 << 20)
        ring.clear(fs)
        t: np.ndarray = np.arange(300_000) / fs
        s: np.ndarray = (2e6 * np.sin(2 * np.pi * 1000 * t)).astype(np.int32)
        ring.write(np.vstack([s, s, s, s]))
        acq: Acquisition = Acquisition(ring)
        acq.timebase = 100e-6
        acq.trigger = TriggerSettings(TriggerMode.NORMAL, 0, Edge.RISING, 1e6, 1e4)
        snap = acq.acquire()
        self.assertIsNotNone(snap)
        k: int = int(round(-snap.x0 * fs))
        self.assertLess(snap.data[0, k - 1], 1e6 + 1)
        self.assertGreaterEqual(snap.data[0, k + 1], 1e6)

    def test_decimation_keeps_peaks(self) -> None:
        """!
        @brief Min/max decimation preserves single-sample spikes.
        """
        y: np.ndarray = np.zeros(1_000_001)
        y[123_457] = 7.0
        y[999_999] = -3.0
        _x, yy = decimate_minmax(y, 0.0, 1.0, 1000)
        self.assertEqual(yy.max(), 7.0)
        self.assertEqual(yy.min(), -3.0)

    def test_capture_names(self) -> None:
        """!
        @brief Capture name increment and sanitising.
        """
        self.assertEqual(next_capture_name("run_009"), "run_010")
        self.assertEqual(next_capture_name("mic"), "mic_1")
        self.assertEqual(sanitize_name(' a/b:c.csv '), "a_b_c")


class TestLive(unittest.TestCase):
    """!
    @brief Backend against the simulator (stream + emulated console).
    """

    state: sim.SimState
    server: sim.Server
    console: ConsoleServer
    sport: int
    cport: int

    @classmethod
    def setUpClass(cls) -> None:
        """!
        @brief Start the simulator servers on spare ports.
        """
        cls.sport, cls.cport = free_port(), free_port()
        cls.state = sim.SimState()
        cls.server = sim.Server(cls.state)
        cls.server.start(cls.sport)
        cls.console = ConsoleServer(cls.state.device, "127.0.0.1", cls.cport)
        cls.console.start()
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls) -> None:
        """!
        @brief Stop the servers and remove temporary files.
        """
        cls.server.stop()
        cls.console.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _wait_events(self, cc: ConsoleClient, pred, timeout: float = 5.0) -> List[ConsoleEvent]:
        """!
        @brief Collect console events until a predicate matches one of them.
        @param cc       Console client.
        @param pred     Callable(event) -> bool.
        @param timeout  Seconds.
        @return All collected events.
        """
        out: List[ConsoleEvent] = []
        end: float = time.time() + timeout
        while time.time() < end:
            evs: List[ConsoleEvent] = cc.poll()
            out += evs
            if any(pred(e) for e in evs):
                return out
            time.sleep(0.02)
        self.fail("expected console event not received")
        return out

    def test_stream_console_capture(self) -> None:
        """!
        @brief End-to-end: stream decode, loss detection, console control, capture to CSV.
        """
        ring: SampleRing = SampleRing(1 << 22)
        health: LinkHealth = LinkHealth()
        rx: StreamReceiver = StreamReceiver(ring, health)
        rx.start("127.0.0.1", self.sport)
        cc: ConsoleClient = ConsoleClient()
        cc.start("127.0.0.1", self.cport)
        dev: DeviceState = DeviceState()
        try:
            time.sleep(1.5)
            self.assertTrue(rx.stats.connected)
            self.assertEqual(rx.stats.sample_rate_hz, 800_000)
            self.assertEqual(rx.stats.total_lost, 0)
            ep, tot, fs = ring.state()
            x: Optional[np.ndarray] = ring.read(tot - 100_000, 100_000, None, ep)
            self.assertIsNotNone(x)
            m = measure(x[0].astype(float), fs)
            self.assertAlmostEqual(m.freq, 10_000.0, delta=1.0)

            self._wait_events(cc, lambda e: e.kind == "banner")
            for cmd in ("ADC DISPLAY_SETTINGS", "PGA SET_GAIN 0x20 0x40 0x40 0x7F", "ANALOG_PWR ON", "bogus"):
                cc.send(cmd)
            evs = self._wait_events(cc, lambda e: e.kind == "reply" and e.reply.command == "bogus")
            replies = [e.reply for e in evs if e.kind == "reply"]
            for r in replies:
                dev.apply_reply(r)
            self.assertEqual(dev.adc.config, ads.AdcConfig())
            self.assertEqual(dev.pga_rdac, [0x20, 0x40, 0x40, 0x7F])
            self.assertTrue(dev.analog_power)
            self.assertEqual(replies[-1].error, "ERR unknown command")

            # frame loss injection -> medium/heavy loss classification
            self.state.set_drop(0.05)
            time.sleep(1.5)
            self.state.set_drop(0.0)
            ws = health.stats()
            self.assertGreater(ws.lost, 0)
            self.assertIn(classify(True, health.data_age(), ws, True), (LinkState.MEDIUM_LOSS, LinkState.HEAVY_LOSS))

            # capture with size limit, CSV size estimate must be exact
            sess: CaptureSession = CaptureSession(self.tmp, "cap", 5_000_000)
            sess.start()
            rx.set_capture(sess)
            end: float = time.time() + 10
            while sess.active and time.time() < end:
                time.sleep(0.05)
            rx.set_capture(None)
            sess.stop()
            self.assertTrue(sess.stats.limit_reached)
            job: ConversionJob = ConversionJob(sess)
            job.start()
            self.assertTrue(job.wait(30))
            self.assertIsNone(job.error)
            self.assertEqual(os.path.getsize(sess.csv_path), sess.stats.csv_bytes)
            with open(sess.csv_path) as f:
                self.assertEqual(f.readline().strip(), protocol.CSV_HEADER)

            # sample-rate change through the console
            cc.send("ADC SET FLTR 8")
            self._wait_events(cc, lambda e: e.kind == "reply")
            time.sleep(1.0)
            self.assertEqual(rx.stats.sample_rate_hz, 1_066_667)
            self.assertEqual(ring.sample_rate, 1_066_667)

            # DEVICE_TEMP as a quiet background poll
            cc.send("DEVICE_TEMP", tag="temp", quiet=True)
            evs = self._wait_events(cc, lambda e: e.kind == "reply" and e.reply.tag == "temp")
            reply = [e.reply for e in evs if e.kind == "reply"][-1]
            self.assertIn(TOPIC_TEMP, dev.apply_reply(reply))
            self.assertEqual(dev.temp.warn_level, 75.0)
            self.assertTrue(all(e.quiet for e in evs if e.kind in ("tx", "line")))

            # ADC STOP -> silence (no DMA timeout dumps any more), state STOPPED
            cc.send("ADC STOP")
            evs = []
            t_end: float = time.time() + 3.5
            while time.time() < t_end:
                evs += cc.poll()
                time.sleep(0.05)
            self.assertFalse(any(e.kind == "line" and "DMA Timeout" in e.text for e in evs))
            self.assertEqual(classify(True, health.data_age(), health.stats(), True), LinkState.STOPPED)

            # START restarts the DMA: the single counter jump is not reported as loss
            lost_before: int = rx.stats.total_lost
            rx.expect_restart()
            cc.send("ADC START")
            cc.send("ADC SET FLTR 9")
            time.sleep(1.0)
            self.assertLess(health.data_age(), 0.5)
            self.assertEqual(rx.stats.total_lost, lost_before)
            self.assertEqual(rx.stats.restarts, 1)
        finally:
            rx.stop()
            cc.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
