"""Qt terrain overlay controller composed from focused responsibilities."""

from PyQt5.QtCore import QObject, pyqtSignal

from TerraLab.terrain.overlay_mixins import (
    OverlayProfileCacheMixin,
    OverlayCategoryHitTestMixin,
    OverlayDrawDispatchMixin,
    OverlayLightContextMixin,
    OverlayLightingCacheMixin,
    OverlayMaterialBuildMixin,
    OverlayProjectionGeometryMixin,
    OverlayInterpolatedMaterialMixin,
    OverlayTrianglePaintMixin,
    OverlayBandDrawMixin,
    OverlayFallbackMixin,
)


class HorizonOverlay(
    OverlayFallbackMixin,
    OverlayBandDrawMixin,
    OverlayTrianglePaintMixin,
    OverlayInterpolatedMaterialMixin,
    OverlayProjectionGeometryMixin,
    OverlayMaterialBuildMixin,
    OverlayLightingCacheMixin,
    OverlayLightContextMixin,
    OverlayDrawDispatchMixin,
    OverlayCategoryHitTestMixin,
    OverlayProfileCacheMixin,
    QObject,
):
    """Render terrain profiles and immutable material caches through Qt."""

    request_update = pyqtSignal()
