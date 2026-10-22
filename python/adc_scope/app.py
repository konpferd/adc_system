"""!
@file app.py
@brief Application start-up: Qt application, global pyqtgraph options, main window.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

_PY_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PY_ROOT not in sys.path:                        # adc_common lives next to adc_scope
    sys.path.insert(0, _PY_ROOT)


LOG_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adc_scope_error.log")


def _log_exception(exc_type: type, exc: BaseException, tb: object) -> None:
    """!
    @brief Write an unhandled exception to adc_scope_error.log and stderr.

    Installed as sys.excepthook so that exceptions raised inside Qt slots are recorded too.

    @param exc_type  Exception class.
    @param exc       Exception instance.
    @param tb        Traceback object.
    """
    import time
    import traceback
    text: str = "".join(traceback.format_exception(exc_type, exc, tb))
    sys.stderr.write(text)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{text}\n")
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    """!
    @brief Run the application; any start-up error is logged to adc_scope_error.log.
    @param argv  Command line (default sys.argv).
    @return Process exit code.
    """
    sys.excepthook = _log_exception
    try:
        return _run(argv)
    except Exception:
        _log_exception(*sys.exc_info())
        return 1


def _run(argv: Optional[List[str]] = None) -> int:
    """!
    @brief Create the Qt application and run the main window.
    @param argv  Command line (default sys.argv); "--sim" / "--device <ip>" pre-select the target,
                 "--connect" connects immediately.
    @return Process exit code.
    """
    args: List[str] = list(sys.argv if argv is None else argv)
    import pyqtgraph as pg
    from adc_common.style import configure_pyqtgraph
    from adc_scope.gui.main_window import TARGET_DEVICE, TARGET_SIM, MainWindow

    configure_pyqtgraph()
    app = pg.mkQApp("ADC scope")
    win: MainWindow = MainWindow()
    if "--sim" in args:
        win.cmb_target.setCurrentIndex(TARGET_SIM)
    if "--device" in args:
        win.cmb_target.setCurrentIndex(TARGET_DEVICE)
        i: int = args.index("--device")
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            win.edt_ip.setText(args[i + 1])
    if "--sim-console" in args:
        win.chk_sim_console.setChecked(True)
    win.show()
    if "--connect" in args:
        win._toggle_connect()
    return int(pg.exec() or 0)


if __name__ == "__main__":
    sys.exit(main())
