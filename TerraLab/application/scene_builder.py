"""Pure composition of a typed :class:`SceneFrame` from application inputs."""

from __future__ import annotations

from dataclasses import replace

from TerraLab.application.commands import (
    ModelSnapshots,
    SceneFrameInputs,
    UserViewState,
)
from TerraLab.light_pollution.modes import (
    bortle_to_magnitude,
    normalize_light_pollution_mode,
    resolve_bortle_class,
)
from TerraLab.scene.contracts import (
    EarthLayerState,
    Layer,
    LayerState,
    LightPollutionMode,
    PresentationState,
    ResourceVersions,
    SceneFrame,
    ScopeState,
    TerrainState,
    Viewport,
)
from TerraLab.scene.render_state import resolve_earth_layer_visibility


class SceneFrameBuilder:
    """Build immutable frame DTOs without QApplication, Qt, or renderer imports."""

    def build(
        self,
        user_view: UserViewState,
        model: ModelSnapshots,
        resources: ResourceVersions,
        viewport: Viewport,
        *,
        generation: int = 0,
    ) -> SceneFrame:
        earth = self._earth_layers(user_view)
        mode = LightPollutionMode(
            normalize_light_pollution_mode(user_view.light_pollution.mode)
        )
        bortle = resolve_bortle_class(
            mode.value,
            automatic_bortle=user_view.light_pollution.automatic_bortle,
            bortle_value=user_view.light_pollution.bortle_value,
            magnitude_limit=user_view.light_pollution.magnitude_limit,
            light_pollution_enabled=earth.light_pollution_enabled,
        )
        effective_bortle = int(round(bortle))
        magnitude_limit = (
            user_view.light_pollution.magnitude_limit
            if mode is LightPollutionMode.MAGNITUDE
            and earth.light_pollution_enabled
            else bortle_to_magnitude(effective_bortle)
        )
        scope = ScopeState(
            enabled=user_view.scope.enabled,
            center_sky=user_view.scope.center_sky,
            fov_deg=user_view.scope.fov_deg,
            shape=user_view.scope.shape,
            settings=user_view.scope.settings,
        )
        return SceneFrame(
            generation=int(generation),
            viewport=viewport,
            time=user_view.time,
            observer=user_view.observer,
            camera=user_view.camera,
            layers=LayerState(self._layer_order(user_view, earth)),
            light_pollution_mode=mode,
            bortle=effective_bortle,
            magnitude_limit=float(magnitude_limit),
            terrain=TerrainState(
                visibility=earth,
                surface_visual_style=user_view.terrain.surface_visual_style,
            ),
            scope=scope,
            resources=resources,
            weather=replace(model.weather, bortle=effective_bortle),
            ephemeris=model.ephemeris,
            selection=model.selection,
            measurement=model.measurement,
            constellation=model.constellation,
            milkyway=user_view.milkyway,
            trails=user_view.trails,
            presentation=PresentationState(
                hud_visible=user_view.presentation.hud_visible,
                debug_render_metrics=user_view.presentation.debug_render_metrics,
                pure_colors=user_view.presentation.pure_colors,
                spike_magnitude_threshold=user_view.presentation.spike_magnitude_threshold,
                star_scale=user_view.presentation.star_scale,
                auto_star_scale_multiplier=(
                    user_view.presentation.auto_star_scale_multiplier
                ),
                scope_k_fallback=user_view.presentation.scope_k_fallback,
                interaction_active=user_view.presentation.interaction_active,
                naked_eye_cap=user_view.presentation.naked_eye_cap,
            ),
        )

    def build_from_inputs(
        self,
        inputs: SceneFrameInputs,
        viewport: Viewport,
        *,
        generation: int = 0,
    ) -> SceneFrame:
        return self.build(
            inputs.user_view,
            inputs.model,
            inputs.resources,
            viewport,
            generation=generation,
        )

    @staticmethod
    def _earth_layers(user_view: UserViewState) -> EarthLayerState:
        intent = user_view.terrain
        resolved = resolve_earth_layer_visibility(
            horizon_enabled=intent.horizon_enabled,
            topography_enabled=intent.topography_enabled,
            surface_enabled=intent.surface_enabled,
            terrain_3d_enabled=intent.terrain_3d_enabled,
            light_pollution_enabled=intent.light_pollution_enabled,
        )
        return EarthLayerState(
            horizon_enabled=resolved.horizon_enabled,
            topography_enabled=resolved.topography_enabled,
            surface_enabled=resolved.surface_enabled,
            terrain_3d_enabled=resolved.terrain_3d_enabled,
            light_pollution_enabled=resolved.light_pollution_enabled,
        )

    @staticmethod
    def _layer_order(
        user_view: UserViewState,
        earth: EarthLayerState,
    ) -> tuple[Layer, ...]:
        requested = user_view.layers
        layers: list[Layer] = []
        if requested.stars_enabled:
            layers.append(Layer.STARS)
        if requested.milkyway_enabled:
            layers.append(Layer.MILKYWAY)
        if requested.solar_system_enabled and requested.sun_moon_enabled:
            layers.append(Layer.SUN_MOON)
        if requested.solar_system_enabled and requested.planets_enabled:
            layers.append(Layer.PLANETS)
        if earth.horizon_enabled:
            layers.append(Layer.TERRAIN)
        if requested.grid_enabled:
            layers.append(Layer.GRID)
        if requested.deep_sky_enabled:
            layers.append(Layer.DEEP_SKY)
        return tuple(layers)
