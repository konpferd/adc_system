#!/usr/bin/env python3
"""!
@file run_adc_scope.py
@brief Convenience launcher for the ADC scope (same as "python -m adc_scope").

Options: --sim | --device [ip]   pre-select the target
         --sim-console           enable the simulator console emulation
         --connect               connect immediately
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adc_scope.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
