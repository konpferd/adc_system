# ADC host tools (Python)

Host software for the ADS127L14 / TE0720 4-channel 24-bit ADC.

```
python/
├── run_adc_scope.py          launcher for the scope application
├── requirements.txt          numpy, pandas, pyqtgraph, PySide6 (+ optional pyarrow)
├── Doxyfile                  "doxygen" here -> docs/html
├── adc_common/               code shared by every tool
│   ├── protocol.py           stream wire format, decoding, loss counting, raw/CSV files
│   ├── ads127l14.py          clock/filter/sample-rate model (mirrors ads127l14_spi.c)
│   ├── console_protocol.py   console texts: parsers (host) + generators (simulator)
│   └── style.py              colors, unit names, pyqtgraph look
├── adc_scope/                real-time scope + capture + device control (NEW)
│   ├── config.py             all tunable constants (ports, buffer sizes, thresholds ...)
│   ├── stream_client.py      receiver thread (port 5001)
│   ├── console_client.py     console client (port 5000)
│   ├── acquisition.py        trigger / sweep logic
│   ├── signal_analysis.py    measurements, trigger search, min/max decimation
│   ├── capture.py            raw capture + CSV conversion
│   ├── link_health.py        packet loss over the last 5 s
│   ├── device_state.py       device model built from console replies
│   ├── gui/                  Qt widgets (main window, scope view, device panel)
│   └── captures/             CSV captures are saved here
├── capture_data_app/         adc_logger_fast.py (CLI logger), adc_plot_gui.py (CSV viewer)
├── test_generator/           adc_simulator.py + device_emulator.py (virtual device)
└── tests/test_backend.py     automated backend tests (no Qt needed)
```

## Install

```
python -m pip install -r requirements.txt
```

## Run

Simulator (virtual device: stream on 5001, emulated console on 5000, HTTP API on 5002):
```
python test_generator/adc_simulator.py
```

Scope:
```
python run_adc_scope.py                      # or: python -m adc_scope
python run_adc_scope.py --sim --sim-console --connect
python run_adc_scope.py --device 192.168.1.10 --connect
```

Tests (start no GUI, use spare ports):
```
python -m unittest discover -s tests -v
```

## Scope features

| Area | What it does |
|---|---|
| Target | *Simulator (localhost)* or *Device (TCP/IP)* with IP / ports. In simulation the device panel is disabled unless *Use simulator console emulation* is ticked. |
| Units | Raw unsigned code, signed 24-bit (LSB), signed × multiplier (default 0.596 µV/LSB, shown in V with SI prefixes). |
| Screen | All 4 channels in one plot, 10 × 8 divisions. Per channel: on/off, display gain ×, offset (spin box or drag the `n▶` marker). |
| Horizontal | Timebase 1 µs/div … 1 s/div (limited to what the ~8.4 M-sample buffer holds at the current rate), trigger position. |
| Vertical | Scale per division, center, *Auto Y*, mouse wheel / drag on the Y axis. |
| Trigger | Auto / Normal / Single, source, rising / falling / either, level (spin box or drag the `T◀` line), noise reject (hysteresis), *Level 50 %*. |
| Run control | RUN/STOP freezes the screen; while stopped, the X axis can be zoomed/panned with the mouse at full resolution. SINGLE, AUTO (auto setup). |
| Measurements | Vpp, frequency, period, Vmax, Vmin, Vavg, Vrms per channel, measured on the displayed record (before display gain/offset). Frequency is independent of the trigger. |
| Data link | Icon + text: Healthy / Medium loss / Heavy loss / No data (ADC stopped) / Device off / No connection. Loss from the 32-bit `hw_frame_ctr` over the last 5 s. Lost-frame positions can be marked in red on the screen. |
| Capture | Name field (auto-increments after each capture), size limit up to 1 GB **of CSV**, fill meter. Streaming writes a raw `.bin` (cheap even at 1 MS/s); the CSV is produced in the background after stop. Same CSV format as before: `recv_time,batch_seq,sample_rate_hz,hw_frame_ctr,ch0..ch3` (raw unsigned codes). |
| Sample rate | Every frame header carries `sample_rate_hz`; a change clears the display buffer and re-scales the time axis automatically. |
| Device panel | Analog power button (ON / OFF / UNKNOWN), START / STOP, PGA codes in hex (apply, read, save EEPROM, reset), ADC configuration report + reconfiguration (with a confirmation that lists every change, the resulting sample rate and limit violations), raw console with history. Every `ERR` reply and every fault log line opens an error pop-up. |

## Device behaviour the app relies on

* **Power / run state cannot be queried.** The firmware has no query for
  `ANALOG_PWR` or the START/STOP state. The app shows the last acknowledged command
  (UNKNOWN after connecting) and uses the stream ("data flowing") for the run state.
* **Streaming needs START and a client.** The firmware only runs the DMA while sampling is
  requested (`ADC START`) and a stream client is connected. A "DMA Timeout!" register dump
  therefore always means a real fault and opens an error pop-up.
* **Restarts are not frame loss.** Every START/RESET restarts the DMA and drops the partly
  filled buffer, so `hw_frame_ctr` jumps once. The app announces these commands to the
  receiver, which ignores that single jump (counted as a "restart", not as lost frames).
* **Temperature.** `DEVICE_TEMP` (Zynq XADC) is polled once per second in the background
  (hidden in the console view unless "Show background polls" is ticked). The device panel
  shows current/min/max; reaching the firmware's `DEVICE_WRN_LVL` opens a warning once,
  re-armed after cooling 2 °C below it.
* **Broadcast logs.** `ADS_LOG`/`PGA_LOG` output reaches every console client. Reports such as
  `DISPLAY_SETTINGS` are parsed from the lines received before the command's prompt.
* **Single stream client.** The board accepts one stream connection. Close the old
  logger before connecting the scope.

## Simulator additions

`adc_simulator.py` now also emulates the console (`device_emulator.py`), using the same
command table, texts and error codes as `console.c`:

* `ADC STOP` pauses the stream and prints the DMA timeout dump.
* `ANALOG_PWR OFF` makes the inputs read about 0.
* PGA codes scale the amplitude by code/0x40. This is a simulation-only model.
* `ADC SET FLTR` / `CLK_DIV` change the stream rate with the firmware's formula.
* `POST /fault {"type":"console"}` injects a fault log line, `{"type":"dma_timeout"}` the DMA
  register dump, for testing the pop-ups.
* `DEVICE_TEMP` returns a simulated die temperature; `POST /device {"temp": 80}` overrides it
  (`null` returns to the simulation) to test the over-temperature warning.
* `ANALOG_PWR OFF` also withdraws the run request; `ADC RESET` restores it afterwards.

`BOOT_ADC_RUNNING = True` keeps the old behaviour (data flows right away), so
`adc_logger_fast.py` works unchanged.
