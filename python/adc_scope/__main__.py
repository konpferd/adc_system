"""!
@file __main__.py
@brief Entry point: python -m adc_scope
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):                       # executed as a script: make the package importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adc_scope.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
