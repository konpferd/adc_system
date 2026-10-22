"""!
@package adc_scope
@brief Real-time oscilloscope, capture and device-control application for the ADS127L14 board.

Run with:  python -m adc_scope   (from the python/ folder)   or   python run_adc_scope.py

Backend modules (no Qt dependency, unit-testable):
  - ring_buffer     : lock-protected per-channel sample history for display
  - stream_client   : TCP stream receiver thread (port 5001), decoding and loss counting
  - link_health     : packet-loss statistics over the last 5 seconds
  - capture         : raw capture writer and raw -> CSV conversion
  - console_client  : TCP console client (port 5000) with command/reply matching
  - device_state    : device state model fed by console replies
  - signal_analysis : trigger search, measurements, min/max decimation
  - units           : code <-> display unit conversion

GUI modules live in adc_scope.gui (PySide6 + pyqtgraph).
"""
__version__: str = "1.0.0"
