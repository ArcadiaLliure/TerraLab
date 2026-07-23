from __future__ import annotations

from types import SimpleNamespace

from TerraLab.data.layer_manager import LayerId
from TerraLab.terrain.data_sources import LayerType
from TerraLab.ui.sky_widget_impl import AstronomicalWidget


class _Selector:
    def __init__(self):
        self.visible = None

    def setVisible(self, visible):
        self.visible = bool(visible)


class _Switch:
    def __init__(self):
        self.value = None
        self.tooltip = ""

    def blockSignals(self, _blocked):
        return None

    def setValue(self, value):
        self.value = int(value)

    def setToolTip(self, tooltip):
        self.tooltip = str(tooltip)


class _Registry:
    def __init__(self, sources, selected_id=None):
        self.sources = list(sources)
        self.selected_id = selected_id

    def list_sources(self):
        return list(self.sources)

    def get_selection(self, _role):
        return SimpleNamespace(source_id=self.selected_id)

    def get(self, source_id):
        return next((source for source in self.sources if source.id == source_id), None)


class _Manager:
    def __init__(self, registry):
        self.data_sources = registry
        self.calls = []

    def set_source(self, layer_id, source_id):
        self.calls.append((layer_id, source_id))
        self.data_sources.selected_id = source_id


class _SurfaceControlHarness:
    _usable_surface_mode_sources = AstronomicalWidget._usable_surface_mode_sources
    _sync_surface_mode_control = AstronomicalWidget._sync_surface_mode_control
    on_surface_mode_changed = AstronomicalWidget.on_surface_mode_changed


def _source(source_id, layer_type, *, enabled=True, priority=0):
    return SimpleNamespace(
        id=source_id,
        layer_type=layer_type,
        enabled=enabled,
        available=True,
        priority=priority,
        resolution_m=10.0,
    )


def _harness(sources, selected_id=None):
    widget = _SurfaceControlHarness()
    registry = _Registry(sources, selected_id)
    widget.layer_manager = _Manager(registry)
    widget.surface_mode_selector = _Selector()
    widget.slider_surface_mode = _Switch()
    widget.latitude = 41.0
    widget.longitude = 2.0
    return widget


def test_surface_mode_switch_is_hidden_until_both_modes_are_usable():
    widget = _harness([_source("rgb", LayerType.SURFACE_RGB)], "rgb")

    widget._sync_surface_mode_control()

    assert widget.surface_mode_selector.visible is False


def test_surface_mode_switch_reflects_the_selected_categorical_source():
    widget = _harness(
        [
            _source("rgb", LayerType.SURFACE_RGB),
            _source("categorical", LayerType.SURFACE_CATEGORICAL),
        ],
        "categorical",
    )

    widget._sync_surface_mode_control()

    assert widget.surface_mode_selector.visible is True
    assert widget.slider_surface_mode.value == 1
    assert "categòric" in widget.slider_surface_mode.tooltip


def test_surface_mode_switch_selects_source_and_refreshes_visible_surface():
    widget = _harness(
        [
            _source("rgb", LayerType.SURFACE_RGB),
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

    widget.on_surface_mode_changed(1)

    assert widget.layer_manager.calls == [
        (LayerId.EARTH_SURFACE_CATEGORICAL, "categorical-high")
    ]
    assert widget._effective_data_sources_payload is None
    assert refreshes == [
        {
            "profile": profile,
            "surface_layer_type": LayerType.SURFACE_CATEGORICAL.value,
            "atomic_surface_swap": True,
            "view_fov_deg": 75.0,
        }
    ]
    assert updates == [True]
