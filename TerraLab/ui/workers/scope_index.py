"""Background builder for the telescope spatial index."""

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot


class ScopeIndexWarmWorker(QObject):
    ready = pyqtSignal(object, object, float)
    error = pyqtSignal(str)

    @pyqtSlot(object, object, object, float)
    def build(self, ra_all, dec_all, mag_all, max_mag):
        try:
            from TerraLab.render.stars_renderer import (
                build_scope_spatial_index_payload,
            )

            sorted_indices, offsets = build_scope_spatial_index_payload(
                ra_all,
                dec_all,
                mag_all=mag_all,
                max_mag=float(max_mag),
            )
            self.ready.emit(sorted_indices, offsets, float(max_mag))
        except Exception as exc:
            self.error.emit(str(exc))
