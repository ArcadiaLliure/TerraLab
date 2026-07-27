"""Asynchronous Skyfield ephemeris loading worker."""

from __future__ import annotations

import time

from PyQt5.QtCore import (
    QObject,
    pyqtSignal,
    pyqtSlot,
)
from skyfield.api import load, load_file


class SkyfieldLoaderWorker(QObject):
    """Background worker to load Skyfield ephemeris without freezing UI."""

    skyfield_ready = pyqtSignal(object, object)  # (ts, eph)

    @pyqtSlot()
    def load(self):

        t0 = time.time()
        try:
            from TerraLab.common.app_paths import ephemeris_path

            ts = load.timescale()
            path = ephemeris_path()
            eph = load_file(str(path)) if path is not None else None
            print(f"[SkyfieldLoader] Initialized in {time.time()-t0:.3f}s")
            self.skyfield_ready.emit(ts, eph)
        except Exception as e:
            print(f"[SkyfieldLoader] Error: {e}")
            self.skyfield_ready.emit(None, None)


