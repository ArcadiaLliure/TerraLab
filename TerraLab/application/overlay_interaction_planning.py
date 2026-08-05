"""Application coordination for phase-06 overlay and interaction plans."""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from TerraLab.application.interaction import ApplicationInteractionController
from TerraLab.application.ports.font_metrics import FontMetricsPort
from TerraLab.scene.contracts import SceneFrame
from TerraLab.scene.picking import PickIndex, PickRecord
from TerraLab.scene.plans.celestial import CelestialLayerPlans
from TerraLab.scene.plans.interaction import project_measurement_plan
from TerraLab.scene.plans.labels import (
    CompassPlanner,
    FontStyle,
    GridPlan,
    GridPlanner,
    HudPlanner,
    InformationalLabelPlanner,
    LineSegment,
    ScreenRect,
    StrokeStyle,
    TextCandidate,
    TextLayoutPlanner,
    TextMetrics,
    TextStyle,
)
from TerraLab.scene.plans.overlays import (
    CirclePrimitive,
    ConstellationOverlayPlan,
    OverlayLayerPlans,
    ScopeOverlayPlan,
)
from TerraLab.scene.projection import (
    project_universal_stereo_point,
    radec_to_altaz_numpy,
    unproject_universal_stereo_point,
)
from TerraLab.scene.spherical_math import slerp_arc_points


class LogicalFontMetrics:
    """Stable CPU fallback implementing the application font-metrics port."""

    revision = "logical-font-metrics-v1"

    def measure_text(self, text: str, style: TextStyle) -> TextMetrics:
        size = max(1.0, float(style.font.pixel_size))
        return TextMetrics(len(text) * size * 0.56, size * 0.78, size * 0.22)


@dataclass(frozen=True, slots=True)
class OverlayPlanningResult:
    """Plan plus its generation-bound model-side pick index."""

    overlays: OverlayLayerPlans
    pick_index: PickIndex


class OverlayInteractionPlanCoordinator:
    """Coordinates Model planners; it owns no toolkit objects or paint calls."""

    _CONSTELLATION_STYLE = StrokeStyle((100, 180, 255, 180), 1.5)
    _CONSTELLATION_SELECTED = StrokeStyle((255, 220, 80, 240), 2.5)
    _CONSTELLATION_PREVIEW = StrokeStyle((255, 120, 120, 220), 1.5, True)
    _CONSTELLATION_PREVIEW_SNAPPED = StrokeStyle((120, 255, 120, 220), 1.5, True)
    _CONSTELLATION_LABEL = TextStyle(
        FontStyle("SansSerif", 10.0, 700),
        (220, 240, 255, 230),
        (20, 30, 45, 180),
        4.0,
        2.0,
        4.0,
    )

    def __init__(self, metrics: FontMetricsPort | None = None) -> None:
        self._metrics = metrics or LogicalFontMetrics()
        self._layout = TextLayoutPlanner()
        self._grid = GridPlanner()
        self._compass = CompassPlanner(self._layout)
        self._hud = HudPlanner(self._layout)
        self._interaction = ApplicationInteractionController()
        self._last_measurement_clear_revision = -1

    @property
    def interaction(self) -> ApplicationInteractionController:
        return self._interaction

    def build(
        self, frame: SceneFrame, celestial: CelestialLayerPlans
    ) -> OverlayPlanningResult:
        """Build all overlay primitives from one already-resolved frame."""

        state = celestial.render_state
        width, height = int(frame.viewport.width), int(frame.viewport.height)
        layers = frozenset(layer.value for layer in frame.layers.order)
        index = self._pick_index(frame, celestial)
        selection_position = None
        if frame.selection.altitude is not None and frame.selection.azimuth is not None:
            point = project_universal_stereo_point(
                frame.selection.altitude, frame.selection.azimuth, width, height,
                state.camera,
            )
            if point is not None:
                selection_position = (float(point[0]), float(point[1]))
        self._interaction.sync_selection(frame.selection, index, selection_position)
        self._interaction.sync_measurement(
            frame.measurement.tool, frame.measurement.clear_revision
        )
        grid = (
            self._grid.plan(state, width, height)
            if "grid" in layers
            else GridPlan((), StrokeStyle((0, 255, 255, 80), 1.0, True))
        )
        compass = self._compass.plan(state, width, height, self._metrics)
        labels = self._labels(celestial, width, height)
        hud = self._hud.plan(
            state,
            width,
            celestial.stars.visible_count,
            self._metrics,
            debug_lines=self._debug_lines(frame),
            visible=frame.presentation.hud_visible,
        )
        measurements = project_measurement_plan(
            self._interaction.build_measurement_plan(frame.generation),
            state,
            width,
            height,
            self._metrics,
        )
        overlays = OverlayLayerPlans(
            generation=frame.generation,
            grid=grid,
            compass=compass,
            labels=labels,
            hud=hud,
            scope=self._scope(frame, celestial),
            constellations=self._constellations(frame, celestial),
            selection=self._interaction.build_selection_plan(
                frame.generation, (time.monotonic() % 1.15) / 1.15
            ),
            measurements=measurements,
        )
        return OverlayPlanningResult(overlays, index)

    def _labels(self, celestial: CelestialLayerPlans, width: int, height: int):
        candidates = (
            InformationalLabelPlanner.planet_candidates(celestial.bodies.planets, width, height)
            + InformationalLabelPlanner.deep_sky_candidates(celestial.deep_sky.glyphs, width, height)
        )
        return self._layout.plan(
            candidates,
            self._metrics,
            cache_key=("resolved-object-labels", width, height, candidates),
        )

    @staticmethod
    def _debug_lines(frame: SceneFrame) -> tuple[str, ...]:
        if not frame.presentation.debug_render_metrics:
            return ()
        return (
            "DEBUG | Sky SCENE | Stars SCENE | Overlays SCENE",
            "Solar SCENE | Milky Way SCENE | NGC SCENE",
        )

    def _scope(
        self, frame: SceneFrame, celestial: CelestialLayerPlans
    ) -> ScopeOverlayPlan:
        width, height = int(frame.viewport.width), int(frame.viewport.height)
        viewport = ScreenRect(0.0, 0.0, float(width), float(height))
        scope = frame.scope
        if not scope.enabled:
            return ScopeOverlayPlan(False, viewport)
        center = (float(width) * 0.5, float(height) * 0.5)
        if scope.center_sky is not None:
            projected = project_universal_stereo_point(
                scope.center_sky[0], scope.center_sky[1], width, height,
                celestial.render_state.camera,
            )
            if projected is not None:
                center = (float(projected[0]), float(projected[1]))
        w_deg, h_deg = scope.fov_deg
        px_per_deg = min(width, height) / max(0.1, w_deg)
        radius = min(w_deg, h_deg) * px_per_deg * 0.5
        rect_size = (w_deg * px_per_deg, h_deg * px_per_deg)
        cx, cy = center
        crosshair = (
            LineSegment((cx - 12.0, cy), (cx + 12.0, cy), StrokeStyle((255, 200, 80, 200), 1.5)),
            LineSegment((cx, cy - 12.0), (cx, cy + 12.0), StrokeStyle((255, 200, 80, 200), 1.5)),
        )
        style = TextStyle(FontStyle("SansSerif", 9.0), (240, 240, 255, 220))
        text = f"FOV {w_deg:.2f}\N{DEGREE SIGN} × {h_deg:.2f}\N{DEGREE SIGN}"
        readout = self._layout.plan(
            (TextCandidate(text, (10.0, float(height) - 12.0), "baseline_left", style, 20_000, viewport, 200, "scope:readout"),),
            self._metrics,
            cache_key=("scope-readout", width, height, text),
            collision_padding=(0.0, 0.0),
        )
        return ScopeOverlayPlan(
            True,
            viewport,
            scope.shape.value,
            center,
            radius if scope.shape.value == "circle" else 0.0,
            rect_size if scope.shape.value == "rectangle" else None,
            crosshair=crosshair,
            readout=readout,
        )

    def _constellations(
        self, frame: SceneFrame, celestial: CelestialLayerPlans
    ) -> ConstellationOverlayPlan:
        state = celestial.render_state
        width, height = int(frame.viewport.width), int(frame.viewport.height)
        source = frame.constellation
        if not source.visible:
            return ConstellationOverlayPlan(source.enabled, False)
        segments: list[LineSegment] = []
        nodes: list[CirclePrimitive] = []
        candidates: list[TextCandidate] = []
        for group_index, group in enumerate(source.groups):
            projected_nodes: list[tuple[float, float]] = []
            selected_group = group_index == source.selected_group_index or group_index in source.selected_group_indices
            previous = None
            for node_index, node in enumerate(group.nodes):
                altitude, azimuth = radec_to_altaz_numpy(
                    np.asarray([node.ra], dtype=np.float32), np.asarray([node.dec], dtype=np.float32),
                    state.latitude, state.longitude, state.ut_hour, state.day_of_year_utc, year=state.year_utc,
                )
                if altitude is None or azimuth is None:
                    previous = node
                    continue
                point = project_universal_stereo_point(float(altitude[0]), float(azimuth[0]), width, height, state.camera)
                if point is not None:
                    position = (float(point[0]), float(point[1]))
                    selected = selected_group or (group_index == source.selected_group_index and node_index == source.selected_node_index)
                    nodes.append(CirclePrimitive(position, 6.0 if selected else 4.0, (255, 230, 100, 255) if selected else (180, 220, 255, 220), StrokeStyle((255, 200, 50, 180), 2.0) if selected else None))
                    projected_nodes.append(position)
                    if node.star_name:
                        candidates.append(TextCandidate(node.star_name, (position[0] + 8.0, position[1] - 8.0), "baseline_left", TextStyle(FontStyle("SansSerif", 8.0), (200, 220, 255, 180)), 500, ScreenRect(0.0, 0.0, float(width), float(height)), 110, f"constellation:node:{group_index}:{node_index}"))
                if previous is not None and node.connect:
                    arc = slerp_arc_points((previous.dec, previous.ra), (node.dec, node.ra), 16)
                    points = tuple(
                        (float(projected[0]), float(projected[1]))
                        for alt, az in arc
                        if (projected := project_universal_stereo_point(alt, az, width, height, state.camera)) is not None
                    )
                    if len(points) >= 2:
                        selected_segment = selected_group or (group_index == source.selected_group_index and node_index - 1 == source.selected_segment_index) or (group_index, node_index - 1) in source.selected_segments
                        segments.extend(LineSegment(a, b, self._CONSTELLATION_SELECTED if selected_segment else self._CONSTELLATION_STYLE, 105) for a, b in zip(points, points[1:]))
                previous = node
            if projected_nodes and group.name:
                x = sum(point[0] for point in projected_nodes) / len(projected_nodes)
                y = sum(point[1] for point in projected_nodes) / len(projected_nodes)
                candidates.append(TextCandidate(group.name, (x, y), "baseline_center", self._CONSTELLATION_LABEL, 1_000, ScreenRect(0.0, 0.0, float(width), float(height)), 120, f"constellation:group:{group_index}"))
        preview = None
        if source.group_drawing_active and source.preview_ra_dec and source.active_group_index is not None and 0 <= source.active_group_index < len(source.groups):
            group = source.groups[source.active_group_index]
            if group.nodes:
                last = group.nodes[-1]
                endpoints: list[tuple[float, float]] = []
                for ra, dec in ((last.ra, last.dec), source.preview_ra_dec):
                    alt, az = radec_to_altaz_numpy(np.asarray([ra], dtype=np.float32), np.asarray([dec], dtype=np.float32), state.latitude, state.longitude, state.ut_hour, state.day_of_year_utc, year=state.year_utc)
                    if alt is not None and az is not None and (point := project_universal_stereo_point(float(alt[0]), float(az[0]), width, height, state.camera)) is not None:
                        endpoints.append((float(point[0]), float(point[1])))
                if len(endpoints) == 2:
                    preview = LineSegment(endpoints[0], endpoints[1], self._CONSTELLATION_PREVIEW_SNAPPED if source.preview_snapped else self._CONSTELLATION_PREVIEW, 130)
        labels = self._layout.plan(tuple(candidates), self._metrics, cache_key=("constellations", width, height, tuple(candidates)))
        return ConstellationOverlayPlan(source.enabled, source.visible, tuple(segments), tuple(nodes), labels, preview)

    @staticmethod
    def _pick_index(frame: SceneFrame, celestial: CelestialLayerPlans) -> PickIndex:
        state = celestial.render_state
        sky_records = tuple(
            PickRecord("sky", pick.key, pick.name, pick.altitude_deg, pick.azimuth_deg, pick.screen_x, pick.screen_y, max(8.0, pick.radius_px), pick.magnitude, {"type": pick.body_type})
            for pick in celestial.bodies.picks
        )
        ngc_records = tuple(
            PickRecord("ngc", pick.name, pick.name, pick.altitude_deg, pick.azimuth_deg, pick.screen_x, pick.screen_y, pick.radius_px, None, {"ra": pick.right_ascension_deg, "dec": pick.declination_deg})
            for pick in celestial.deep_sky.picks
        )
        return PickIndex(
            generation=frame.generation,
            sky_objects=sky_records,
            ngc_objects=ngc_records,
            star_catalog_indices=celestial.stars.picks.catalog_indices,
            star_screen_x=celestial.stars.picks.screen_x,
            star_screen_y=celestial.stars.picks.screen_y,
            star_ra=state.np_ra,
            star_dec=state.np_dec,
            star_mag=state.np_mag,
            star_bp_rp=state.np_bp_rp,
            unproject_fn=lambda x, y: unproject_universal_stereo_point(x, y, frame.viewport.width, frame.viewport.height, state.camera),
        )
