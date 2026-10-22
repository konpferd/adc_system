"""!
@package adc_common
@brief Code shared by every ADC host tool (scope app, logger, CSV viewer, simulator).

Modules:
  - protocol  : stream wire format, sample decoding, frame-loss counting, raw/CSV capture files
  - ads127l14 : model of the ADS127L14 clock/filter configuration (mirrors the firmware math)
  - style     : common GUI look (channel colors, unit names, pyqtgraph setup); needs pyqtgraph

protocol and ads127l14 only depend on numpy (pandas is imported lazily for CSV export),
so the simulator can still run headless without any Qt package installed.
"""
