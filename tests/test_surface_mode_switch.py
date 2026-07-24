from __future__ import annotations

from types import SimpleNamespace

from PyQt5.QtCore import QEvent, QPoint

from TerraLab.terrain.land_cover.legends.category_info import (
    LandCoverCategoryInfo,
)
from TerraLab.terrain.data_sources import LayerType, SurfaceMode
from TerraLab.ui.canvas_input_handler import CanvasInputHandler
from TerraLab.ui.sky_widget_impl import AstroCanvas, AstronomicalWidget


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
    _surface_sources_covering_observer = (
        AstronomicalWidget._surface_sources_covering_observer
    )
    _surface_coverage_message = AstronomicalWidget._surface_coverage_message
    _sync_surface_mode_control = AstronomicalWidget._sync_surface_mode_control
    on_surface_mode_changed = AstronomicalWidget.on_surface_mode_changed
    on_surface_visual_style_changed = (
        AstronomicalWidget.on_surface_visual_style_changed
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
        "TerraLab.ui.sky_widget_impl.get_config_value",
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
        "TerraLab.ui.sky_widget_impl.set_config_value",
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
        "TerraLab.ui.sky_widget_impl.QMessageBox.information",
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


class _TooltipEvent:
    def __init__(self, event_type=QEvent.ToolTip):
        self.event_type = event_type
        self.accepted = False
        self.ignored = False

    def type(self):
        return self.event_type

    def pos(self):
        return QPoint(4, 5)

    def globalPos(self):
        return QPoint(40, 50)

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


def _tooltip_canvas(*, mode, dragging, lookup):
    registry = SimpleNamespace(surface_mode=mode)
    parent = SimpleNamespace(
        layer_manager=SimpleNamespace(data_sources=registry),
        _dragging_time=False,
    )
    return SimpleNamespace(
        parent_widget=parent,
        dragging=dragging,
        horizon_overlay=SimpleNamespace(category_at_screen=lookup),
        _parent_checkbox_checked=lambda *_args: True,
        scope_mode_enabled=lambda: False,
        measurement_tool_active=lambda: False,
        drawing_mode_enabled=lambda: False,
        scope_controller=SimpleNamespace(dragging=False),
    )


def test_standard_tooltip_event_shows_cached_categorical_description(
    monkeypatch,
):
    shown = []
    monkeypatch.setattr(
        "TerraLab.ui.sky_widget_impl.QToolTip",
        SimpleNamespace(
            showText=lambda *args: shown.append(args),
            hideText=lambda: None,
        ),
    )
    info = LandCoverCategoryInfo(
        82,
        "Coberta d'arbres",
        "Descripció breu.",
        "S2GLC Europe 2017",
    )
    canvas = _tooltip_canvas(
        mode=SurfaceMode.LAND_COVER,
        dragging=False,
        lookup=lambda *_args: info,
    )
    event = _TooltipEvent()

    assert AstroCanvas.event(canvas, event) is True
    assert event.accepted is True
    assert "Classe 82 · S2GLC Europe 2017" in shown[0][1]


def test_mouse_move_shows_categorical_tooltip_without_waiting_for_qt_delay(
    monkeypatch,
):
    shown = []
    monkeypatch.setattr(
        "TerraLab.ui.sky_widget_impl.QToolTip",
        SimpleNamespace(
            showText=lambda *args: shown.append(args),
            hideText=lambda: None,
        ),
    )
    info = LandCoverCategoryInfo(
        82,
        "Coberta d'arbres",
        "Descripció breu.",
        "S2GLC Europe 2017",
    )
    canvas = _tooltip_canvas(
        mode=SurfaceMode.LAND_COVER,
        dragging=False,
        lookup=lambda *_args: info,
    )
    canvas._update_surface_tooltip_for_pointer = lambda event: (
        AstroCanvas._update_surface_tooltip_for_pointer(canvas, event)
    )
    canvas.drawing_mode_enabled = lambda: False
    canvas.scope_mode_enabled = lambda: False
    canvas.measurement_tool_active = lambda: False
    event = _TooltipEvent(QEvent.MouseMove)

    CanvasInputHandler(canvas).handle_mouse_move(event)

    assert len(shown) == 1
    assert "Coberta d&#x27;arbres" in shown[0][1]


def test_tooltip_is_suppressed_while_dragging_or_in_orthophoto(
    monkeypatch,
):
    lookups = []
    hidden = []
    monkeypatch.setattr(
        "TerraLab.ui.sky_widget_impl.QToolTip",
        SimpleNamespace(
            showText=lambda *_args: None,
            hideText=lambda: hidden.append(True),
        ),
    )
    for mode, dragging in (
        (SurfaceMode.LAND_COVER, True),
        (SurfaceMode.ORTHOPHOTO, False),
    ):
        canvas = _tooltip_canvas(
            mode=mode,
            dragging=dragging,
            lookup=lambda *_args: lookups.append(True),
        )
        event = _TooltipEvent()
        AstroCanvas.event(canvas, event)
        assert event.ignored is True

    assert lookups == []
    assert len(hidden) == 2
