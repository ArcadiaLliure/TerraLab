from __future__ import annotations

from types import SimpleNamespace

from TerraLab.terrain.data_sources import LayerType, SurfaceMode
from TerraLab.ui.astronomical_widget import AstronomicalWidget


class _Selector:
    def __init__(self):
        self.visible = None
        self.enabled = None

    def setVisible(self, visible):
        self.visible = bool(visible)

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _Switch:
    def __init__(self):
        self.value = None
        self.tooltip = ""
        self.enabled = None

    def blockSignals(self, _blocked):
        return None

    def setValue(self, value):
        self.value = int(value)

    def setToolTip(self, tooltip):
        self.tooltip = str(tooltip)

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _CheckBox:
    def __init__(self, checked):
        self.checked = bool(checked)
        self.enabled = True
        self.tooltip = ""
        self.text = ""
        self.style = ""

    def isChecked(self):
        return self.checked

    def blockSignals(self, _blocked):
        return None

    def setChecked(self, checked):
        self.checked = bool(checked)

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)

    def setToolTip(self, tooltip):
        self.tooltip = str(tooltip)

    def setText(self, text):
        self.text = str(text)

    def setStyleSheet(self, style):
        self.style = str(style)


class _Registry:
    def __init__(self, sources, selected_id=None):
        self.sources = list(sources)
        self.selected_id = selected_id
        self.surface_mode = SurfaceMode.LAND_COVER

    def list_sources(self):
        return list(self.sources)

    def get_selection(self, _role):
        return SimpleNamespace(source_id=self.selected_id)

    def get(self, source_id):
        return next((source for source in self.sources if source.id == source_id), None)

    def set_surface_mode(self, mode):
        self.surface_mode = SurfaceMode(mode)


class _Manager:
    def __init__(self, registry):
        self.data_sources = registry
        self.calls = []

    def set_source(self, layer_id, source_id):
        self.calls.append((layer_id, source_id))
        self.data_sources.selected_id = source_id


class _SurfaceControlHarness:
    _usable_surface_mode_sources = AstronomicalWidget._usable_surface_mode_sources
    _usable_layer_sources = AstronomicalWidget._usable_layer_sources
    _surface_sources_covering_observer = (
        AstronomicalWidget._surface_sources_covering_observer
    )
    _coverage_message = AstronomicalWidget._coverage_message
    _reject_out_of_coverage_layer = (
        AstronomicalWidget._reject_out_of_coverage_layer
    )
    _surface_coverage_message = AstronomicalWidget._surface_coverage_message
    _sync_coverage_control = AstronomicalWidget._sync_coverage_control
    _sync_non_surface_coverage_controls = (
        AstronomicalWidget._sync_non_surface_coverage_controls
    )
    _sync_surface_terrain_3d_control = (
        AstronomicalWidget._sync_surface_terrain_3d_control
    )
    _sync_surface_mode_control = AstronomicalWidget._sync_surface_mode_control
    on_surface_mode_changed = AstronomicalWidget.on_surface_mode_changed
    on_surface_visual_style_changed = (
        AstronomicalWidget.on_surface_visual_style_changed
    )
    on_topography_toggled = AstronomicalWidget.on_topography_toggled
    on_light_pollution_toggled = (
        AstronomicalWidget.on_light_pollution_toggled
    )


def _source(
    source_id,
    layer_type,
    *,
    enabled=True,
    priority=0,
    coverage=None,
):
    source = SimpleNamespace(
        id=source_id,
        layer_type=layer_type,
        enabled=enabled,
        available=True,
        priority=priority,
        resolution_m=10.0,
        coverage=coverage,
    )
    if coverage is not None:
        west, south, east, north = coverage
        source.covers = lambda latitude, longitude: (
            south <= latitude <= north
            and west <= longitude <= east
        )
    return source


def _harness(sources, selected_id=None):
    widget = _SurfaceControlHarness()
    registry = _Registry(sources, selected_id)
    widget.layer_manager = _Manager(registry)
    widget.surface_mode_selector = _Selector()
    widget.slider_surface_mode = _Switch()
    widget.surface_visual_style_selector = _Selector()
    widget.slider_surface_visual_style = _Switch()
    widget.latitude = 41.0
    widget.longitude = 2.0
    return widget


def test_surface_mode_switch_is_hidden_until_both_modes_are_usable():
    widget = _harness([_source("rgb", LayerType.SURFACE_RGB)], "rgb")

    widget._sync_surface_mode_control()

    assert widget.surface_mode_selector.visible is False


def test_visible_surface_allows_independent_terrain_3d():
    widget = _harness([_source("rgb", LayerType.SURFACE_RGB)], "rgb")
    persisted = []
    widget.chk_surface_layer = _CheckBox(True)
    widget.chk_terrain_3d = _CheckBox(False)
    widget._persist_visibility_state = (
        lambda key, checked: persisted.append((key, checked))
    )

    widget._sync_surface_mode_control()

    assert widget.chk_terrain_3d.checked is False
    assert widget.chk_terrain_3d.enabled is True
    assert persisted == []


def test_hidden_surface_reenables_terrain_3d_choice():
    widget = _harness([_source("rgb", LayerType.SURFACE_RGB)], "rgb")
    widget.chk_surface_layer = _CheckBox(False)
    widget.chk_terrain_3d = _CheckBox(True)

    widget._sync_surface_mode_control()

    assert widget.chk_terrain_3d.checked is True
    assert widget.chk_terrain_3d.enabled is True


def test_surface_mode_switch_reflects_the_selected_categorical_source():
    widget = _harness(
        [
            _source("ortho", LayerType.ORTHOPHOTO_RGB),
            _source("categorical", LayerType.SURFACE_CATEGORICAL),
        ],
        "categorical",
    )

    widget._sync_surface_mode_control()

    assert widget.surface_mode_selector.visible is True
    assert widget.slider_surface_mode.value == 1
    assert "categòric" in widget.slider_surface_mode.tooltip


def test_visual_style_switch_is_independent_of_surface_source(monkeypatch):
    widget = _harness(
        [_source("ortho", LayerType.ORTHOPHOTO_RGB)],
        "ortho",
    )
    widget.layer_manager.data_sources.surface_mode = SurfaceMode.ORTHOPHOTO
    monkeypatch.setattr(
        "TerraLab.ui.widget_mixins.layers.get_config_value",
        lambda key, default=None: (
            "vibrant" if key == "surface_visual_style" else default
        ),
    )

    widget._sync_surface_mode_control()

    assert widget.surface_mode_selector.visible is False
    assert widget.surface_visual_style_selector.visible is True
    assert widget.slider_surface_visual_style.value == 1
    assert "Vibrant" in widget.slider_surface_visual_style.tooltip


def test_visual_style_change_reloads_only_visual_render_settings(monkeypatch):
    widget = _harness(
        [_source("ortho", LayerType.ORTHOPHOTO_RGB)],
        "ortho",
    )
    saved = []
    reloaded = []
    updates = []
    refreshes = []
    monkeypatch.setattr(
        "TerraLab.ui.widget_mixins.layers.set_config_value",
        lambda key, value: saved.append((key, value)),
    )
    widget.terrain_coordinator = SimpleNamespace(
        request_surface_refresh=lambda **kwargs: refreshes.append(kwargs)
    )
    widget.canvas = SimpleNamespace(
        horizon_overlay=SimpleNamespace(
            reload_render_settings=lambda: reloaded.append(True)
        ),
        update=lambda: updates.append(True),
    )

    widget.on_surface_visual_style_changed(1)

    assert ("surface_visual_style", "vibrant") in saved
    assert ("categorical_edge_smoothing_enabled", True) in saved
    assert reloaded == [True]
    assert updates == [True]
    assert refreshes == []


def test_surface_mode_switch_selects_source_and_refreshes_visible_surface():
    widget = _harness(
        [
            _source("ortho", LayerType.ORTHOPHOTO_RGB),
            _source("categorical-low", LayerType.SURFACE_CATEGORICAL, priority=1),
            _source("categorical-high", LayerType.SURFACE_CATEGORICAL, priority=5),
        ],
        "rgb",
    )
    refreshes = []
    updates = []
    profile = object()
    widget._effective_data_sources_payload = {"stale": True}
    widget._full_horizon_profile = profile
    widget.chk_surface_layer = SimpleNamespace(isChecked=lambda: True)
    widget.terrain_coordinator = SimpleNamespace(
        request_surface_refresh=lambda **kwargs: refreshes.append(kwargs)
    )
    widget._surface_refresh_view_kwargs = lambda: {"view_fov_deg": 75.0}
    widget.canvas = SimpleNamespace(update=lambda: updates.append(True))
    widget.layer_manager.data_sources.surface_mode = SurfaceMode.ORTHOPHOTO

    widget.on_surface_mode_changed(1)

    assert widget.layer_manager.calls == []
    assert (
        widget.layer_manager.data_sources.surface_mode
        is SurfaceMode.LAND_COVER
    )
    assert widget._effective_data_sources_payload is None
    assert refreshes == [
        {
            "profile": profile,
            "surface_mode": SurfaceMode.LAND_COVER.value,
            "atomic_surface_swap": True,
            "view_fov_deg": 75.0,
        }
    ]
    assert updates == [True]


def test_out_of_coverage_orthophoto_is_rejected_with_explanation(monkeypatch):
    widget = _harness(
        [
            _source(
                "ortho",
                LayerType.ORTHOPHOTO_RGB,
                coverage=(0.97737, 42.57041, 1.03486, 42.59400),
            ),
            _source("categorical", LayerType.SURFACE_CATEGORICAL),
        ],
        "categorical",
    )
    messages = []
    monkeypatch.setattr(
        "TerraLab.ui.widget_mixins.surface_data.QMessageBox.information",
        lambda _parent, title, message: messages.append((title, message)),
    )
    widget.latitude = 41.21535
    widget.longitude = 0.80970

    widget.on_surface_mode_changed(0)

    assert (
        widget.layer_manager.data_sources.surface_mode
        is SurfaceMode.LAND_COVER
    )
    assert widget.slider_surface_mode.value == 1
    assert messages
    assert messages[0][0] == "Ortofoto fora de cobertura"
    assert "41.21535, 0.80970" in messages[0][1]
    assert "42.57041–42.59400 N" in messages[0][1]


def test_out_of_coverage_categorical_is_rejected_with_explanation(
    monkeypatch,
):
    widget = _harness(
        [
            _source("ortho", LayerType.ORTHOPHOTO_RGB),
            _source(
                "categorical",
                LayerType.SURFACE_CATEGORICAL,
                coverage=(0.97737, 42.57041, 1.03486, 42.59400),
            ),
        ],
        "ortho",
    )
    widget.layer_manager.data_sources.surface_mode = SurfaceMode.ORTHOPHOTO
    messages = []
    monkeypatch.setattr(
        "TerraLab.ui.widget_mixins.surface_data.QMessageBox.information",
        lambda _parent, title, message: messages.append((title, message)),
    )
    widget.latitude = 41.21535
    widget.longitude = 0.80970

    widget.on_surface_mode_changed(1)

    assert (
        widget.layer_manager.data_sources.surface_mode
        is SurfaceMode.ORTHOPHOTO
    )
    assert widget.slider_surface_mode.value == 0
    assert messages[0][0] == "Cobertura fora de l'àrea"
    assert "41.21535, 0.80970" in messages[0][1]


def test_topography_and_light_pollution_reject_out_of_coverage_activation(
    monkeypatch,
):
    widget = _harness(
        [
            _source(
                "dem",
                LayerType.ELEVATION,
                coverage=(0.97737, 42.57041, 1.03486, 42.59400),
            ),
            _source(
                "light",
                LayerType.LIGHT_POLLUTION,
                coverage=(0.97737, 42.57041, 1.03486, 42.59400),
            ),
        ]
    )
    widget.latitude = 41.21535
    widget.longitude = 0.80970
    messages = []
    persisted = []
    config_writes = []
    widget.chk_enable_village = _CheckBox(True)
    widget.chk_light_pollution = _CheckBox(True)
    widget.canvas = SimpleNamespace(update=lambda: None)
    widget.terrain_coordinator = SimpleNamespace(
        reload_config=lambda: None,
        initialize=lambda: None,
    )
    widget.light_pollution_mode = "bortle"
    widget._persist_visibility_state = (
        lambda key, checked: persisted.append((key, checked))
    )
    widget._apply_light_pollution_graphics = lambda: None
    monkeypatch.setattr(
        "TerraLab.ui.widget_mixins.layers.QMessageBox.information",
        lambda _parent, title, message: messages.append((title, message)),
    )
    monkeypatch.setattr(
        "TerraLab.ui.widget_mixins.layers.set_config_value",
        lambda key, value: config_writes.append((key, value)),
    )
    monkeypatch.setattr(
        "TerraLab.ui.widget_mixins.layers.QTimer.singleShot",
        lambda *_args: None,
    )

    widget.on_topography_toggled(True)
    widget.on_light_pollution_toggled(True)

    assert widget.chk_enable_village.checked is False
    assert widget.chk_light_pollution.checked is False
    assert widget.light_pollution_enabled is False
    assert ("topografia", False) in persisted
    assert ("contaminacio_luminica", False) in persisted
    assert ("light_pollution_enabled", False) in config_writes
    assert [title for title, _message in messages] == [
        "Topografia fora de cobertura",
        "Contaminació lumínica fora de cobertura",
    ]


def test_all_location_bound_controls_mark_out_of_area():
    outside = (0.97737, 42.57041, 1.03486, 42.59400)
    widget = _harness(
        [
            _source("ortho", LayerType.ORTHOPHOTO_RGB),
            _source(
                "categorical",
                LayerType.SURFACE_CATEGORICAL,
                coverage=outside,
            ),
            _source("dem", LayerType.ELEVATION, coverage=outside),
            _source(
                "light",
                LayerType.LIGHT_POLLUTION,
                coverage=outside,
            ),
        ],
        "ortho",
    )
    widget.lbl_surface_mode_rgb = _CheckBox(False)
    widget.lbl_surface_mode_categorical = _CheckBox(False)
    widget.chk_enable_village = _CheckBox(True)
    widget.chk_light_pollution = _CheckBox(True)

    widget._sync_surface_mode_control()

    assert widget.lbl_surface_mode_rgb.text == "Ortofoto"
    assert (
        widget.lbl_surface_mode_categorical.text
        == "Categòric (fora d'àrea)"
    )
    assert widget.chk_enable_village.text == "Topografia (fora d'àrea)"
    assert (
        widget.chk_light_pollution.text
        == "Contaminació lumínica (fora d'àrea)"
    )
    assert "#9b2f2f" in widget.lbl_surface_mode_categorical.style
    assert "41.00000, 2.00000" in widget.chk_enable_village.tooltip
