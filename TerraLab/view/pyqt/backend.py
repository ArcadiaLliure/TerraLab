"""QPainter implementation of the neutral renderer contract.

The backend is a presentation adapter: it creates Qt paint devices and
translates a resolved :class:`RenderPlanBundle` to QPainter calls.  Scientific
selection, terrain preparation and picking are complete before ``render``.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from collections.abc import Mapping

from PyQt5 import sip
from PyQt5.QtGui import QGuiApplication, QImage, QPainter

from TerraLab.core.rendering_contracts.contracts import (
    PickRequest,
    PresenterKind,
    RasterFrameOutput,
    RenderBackendLifecycleError,
    RenderCapability,
    RenderOutput,
    RenderOutputPort,
    RenderTarget,
    RenderTargetKind,
    SharedRasterTarget,
)
from TerraLab.core.rendering_contracts.plans import (
    RenderPlanBundle,
    TerrainMeshResource,
)
from TerraLab.render.qpainter.bodies import QPainterBodiesAdapter
from TerraLab.render.qpainter.constellations import (
    render_qpainter_constellation_plan,
)
from TerraLab.render.qpainter.deep_sky import QPainterDeepSkyAdapter
from TerraLab.render.qpainter.interaction import (
    render_measurement_plan,
    render_selection_plan,
)
from TerraLab.render.qpainter.labels import QPainterLabelsAdapter
from TerraLab.render.qpainter.scope import render_qpainter_scope_plan
from TerraLab.render.qpainter.sky import QPainterSkyBackgroundAdapter
from TerraLab.render.qpainter.stars import QPainterStarAdapter
from TerraLab.render.qpainter.terrain import QPainterTerrainAdapter
from TerraLab.scene.contracts import JSONValue, freeze_json_mapping


class QPainterRendererBackend:
    """Paint an already-resolved render bundle directly to a shared raster."""

    backend_id = "qpainter"
    capabilities = frozenset(
        {
            RenderCapability.RASTER_SHARED_FRAME,
            RenderCapability.PICKING,
            RenderCapability.INTERACTION,
            RenderCapability.SKY_BACKGROUND,
            RenderCapability.STARS,
            RenderCapability.EPHEMERIS_BODIES,
            RenderCapability.DEEP_SKY,
            RenderCapability.GRID,
            RenderCapability.LABELS,
            RenderCapability.SCOPE,
            RenderCapability.CONSTELLATIONS,
            RenderCapability.MEASUREMENTS,
            RenderCapability.TERRAIN_GEOMETRY,
            RenderCapability.TERRAIN_MATERIALS,
        }
    )
    target_kinds = frozenset({RenderTargetKind.SHARED_RASTER})
    presenter_kinds = frozenset({PresenterKind.SHARED_FRAME})
    requires_host_pick = False

    def __init__(self) -> None:
        self._output: RenderOutputPort | None = None
        self._closed = False
        self._started = False
        self._qt_application: QGuiApplication | None = None
        self._sky_adapter = QPainterSkyBackgroundAdapter()
        self._stars_adapter = QPainterStarAdapter()
        self._bodies_adapter = QPainterBodiesAdapter()
        self._deep_sky_adapter = QPainterDeepSkyAdapter()
        self._labels_adapter = QPainterLabelsAdapter()
        self._terrain_adapter = QPainterTerrainAdapter()

    def start(self, output_port: RenderOutputPort) -> None:
        if self._closed:
            raise RenderBackendLifecycleError(
                "Cannot restart a closed backend"
            )
        if self._started:
            raise RenderBackendLifecycleError(
                "QPainter backend is already started"
            )
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self._qt_application = QGuiApplication.instance() or QGuiApplication(
            [sys.argv[0]]
        )
        self._output = output_port
        self._started = True

    def render(
        self, plan: RenderPlanBundle, target: RenderTarget
    ) -> RenderOutput:
        self._require_started()
        if not isinstance(target, SharedRasterTarget):
            raise TypeError("QPainter requires a SharedRasterTarget")
        started = time.perf_counter()
        image: QImage | None = None
        painter: QPainter | None = None
        try:
            address = ctypes.addressof(
                ctypes.c_ubyte.from_buffer(target.pixels)
            )
            image = QImage(
                sip.voidptr(address),
                target.handle.width,
                target.handle.height,
                target.handle.stride,
                QImage.Format_ARGB32_Premultiplied,
            )
            painter = QPainter(image)
            logical_width = max(1, int(plan.frame.viewport.width))
            logical_height = max(1, int(plan.frame.viewport.height))
            painter.scale(
                target.handle.width / logical_width,
                target.handle.height / logical_height,
            )
            metadata = self._paint_plan(
                painter, plan, logical_width, logical_height
            )
        finally:
            if painter is not None and painter.isActive():
                painter.end()
            del painter
            del image
        output = RasterFrameOutput(
            generation=plan.generation,
            handle=target.handle,
            render_ms=round((time.perf_counter() - started) * 1000.0, 3),
            metadata=freeze_json_mapping(metadata),
        )
        if self._output is not None:
            self._output.frame_ready(output)
        return output

    def request_pick(
        self,
        request: PickRequest,
        plan: RenderPlanBundle | None = None,
    ) -> None:
        """Reject direct picks: the shared controller resolves CPU policy.

        QPainter has no host-side hit-test implementation.  Calling the
        application planner here used to make the view own selection policy;
        :class:`SceneRenderController` now invokes that planner for every
        non-host backend before publishing a typed result.
        """

        self._require_started()
        raise RenderBackendLifecycleError(
            "QPainter picks must be coordinated by SceneRenderController"
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False
        self._output = None
        self._sky_adapter.clear_cache()
        self._bodies_adapter.clear_cache()

    def _require_started(self) -> None:
        if self._closed or not self._started:
            raise RenderBackendLifecycleError(
                "QPainter backend has not started"
            )

    def _paint_plan(
        self,
        painter: QPainter,
        plan: RenderPlanBundle,
        width: int,
        height: int,
    ) -> Mapping[str, JSONValue]:
        """Materialise resolved plans in their model-defined layer order."""

        terrain_calls = 0
        celestial = plan.celestial
        if celestial is not None:
            sky_result = self._sky_adapter.paint(
                painter, celestial.sky_background, width=width, height=height
            )
            self._deep_sky_adapter.paint_milkyway(
                painter, celestial.milkyway, width, height
            )
            self._bodies_adapter.paint_trails(
                painter, width, height, celestial.trails
            )
            star_calls = self._stars_adapter.paint(
                painter, celestial.stars, pure_colors=celestial.pure_colors
            )
            self._bodies_adapter.paint_bodies(painter, celestial.bodies)
            self._deep_sky_adapter.paint_deep_sky(painter, celestial.deep_sky)
        else:
            sky_result = None
            star_calls = 0

        for scene_plan in sorted(
            plan.plans, key=lambda item: item.layer_order
        ):
            for primitive in scene_plan.primitives:
                if isinstance(primitive, TerrainMeshResource):
                    terrain_calls += self._terrain_adapter.paint_mesh(
                        painter, primitive
                    )

        overlays = plan.overlays
        if overlays is not None:
            self._labels_adapter.paint_grid(painter, overlays.grid)
            self._labels_adapter.paint_compass(painter, overlays.compass)
            self._labels_adapter.paint_text_batch(painter, overlays.labels)
            render_qpainter_scope_plan(painter, overlays.scope)
            render_selection_plan(painter, overlays.selection)
            render_measurement_plan(painter, overlays.measurements)
            render_qpainter_constellation_plan(
                painter, overlays.constellations
            )
            self._labels_adapter.paint_hud(painter, overlays.hud)

        metadata: dict[str, JSONValue] = {
            "plan_native": True,
            "celestial_plan_native": celestial is not None,
            "terrain_plan_native": any(
                isinstance(primitive, TerrainMeshResource)
                for scene_plan in plan.plans
                for primitive in scene_plan.primitives
            ),
            "overlay_interaction_plan_native": overlays is not None,
            "qpainter_terrain_calls": terrain_calls,
            "qpainter_star_calls": int(star_calls),
        }
        if celestial is not None:
            metadata["visible_stars"] = int(celestial.stars.visible_count)
        if sky_result is not None:
            metadata["sky_cache_hit"] = sky_result.cache_hit
        return metadata


__all__ = ("QPainterRendererBackend",)
