from __future__ import annotations

from types import SimpleNamespace

from TerraLab.data.layer_manager import LayerId, LayerState
from TerraLab.light_pollution.modes import LP_MODE_BORTLE
from TerraLab.ui.astronomical_widget import AstronomicalWidget
from TerraLab.ui.data_layers_dialog import DataLayerChanges, _DATA_LAYERS_STYLE


class _CheckedBox:
    def __init__(self, checked: bool):
        self.checked = bool(checked)

    def isChecked(self):
        return self.checked

    def blockSignals(self, _blocked):
        return None

    def setChecked(self, checked):
        self.checked = bool(checked)


class _LayerGuideHarness:
    _missing_layer_target = AstronomicalWidget._missing_layer_target
    _guide_missing_layer = AstronomicalWidget._guide_missing_layer

    @staticmethod
    def _schedule_lifecycle_callback(_delay, callback):
        callback()


class _LayerStatusManager:
    def __init__(self, states):
        self.states = dict(states)

    def status(self, layer_id):
        return SimpleNamespace(state=self.states[layer_id])


def test_data_layers_dialog_uses_dark_theme_for_native_qt_surfaces():
    assert "QTabWidget::pane" in _DATA_LAYERS_STYLE
    assert "QTreeWidget::item:selected" in _DATA_LAYERS_STYLE
    assert "QComboBox QAbstractItemView" in _DATA_LAYERS_STYLE
    assert "background-color: #0d1a30" in _DATA_LAYERS_STYLE


def test_user_click_on_missing_layer_opens_focused_library():
    opened = []
    widget = _LayerGuideHarness()
    widget.layer_manager = _LayerStatusManager(
        {LayerId.SKY_MILKY_WAY: LayerState.MISSING}
    )
    widget.open_data_layers_dialog = lambda **kwargs: opened.append(kwargs)
    guided = widget._guide_missing_layer(True, LayerId.SKY_MILKY_WAY)

    assert guided is True
    assert opened == [{"focus_layer_id": LayerId.SKY_MILKY_WAY}]


def test_surface_checkbox_does_not_prompt_when_either_product_is_ready():
    widget = _LayerGuideHarness()
    widget.layer_manager = _LayerStatusManager(
        {
            LayerId.EARTH_SURFACE_CATEGORICAL: LayerState.MISSING,
            LayerId.EARTH_SURFACE_RGB: LayerState.READY,
        }
    )

    assert widget._missing_layer_target(LayerId.EARTH_SURFACE) is None


def test_light_pollution_can_enable_with_runtime_fallback_without_onboarding(
    monkeypatch,
):
    config_writes = []
    monkeypatch.setattr(
        "TerraLab.ui.widget_mixins.layers.set_config_value",
        lambda key, value: config_writes.append((key, value)),
    )
    dummy = SimpleNamespace(
        chk_light_pollution=_CheckedBox(True),
        light_pollution_mode=LP_MODE_BORTLE,
        terrain_coordinator=SimpleNamespace(reload_config=lambda: None),
        _persist_visibility_state=lambda *_args: None,
        _apply_light_pollution_graphics=lambda: None,
        _ensure_asset_before_enable=lambda *_args: (_ for _ in ()).throw(
            AssertionError("normal layer use must not open onboarding")
        ),
    )

    AstronomicalWidget.on_light_pollution_toggled(dummy, True)

    assert dummy.light_pollution_enabled is True
    assert ("light_pollution_enabled", True) in config_writes


def test_runtime_effective_source_overrides_catalogue_candidate_in_label():
    configured = SimpleNamespace(display_name="Configured RGB")
    catalogue = SimpleNamespace(display_name="Configured RGB")
    fallback = SimpleNamespace(
        display_name="Fallback land cover",
        resolution_m=10.0,
    )
    selection = SimpleNamespace(
        configured=configured,
        effective=catalogue,
        reason="automatic",
    )
    registry = SimpleNamespace(
        get=lambda source_id: fallback if source_id == "fallback" else None
    )

    text, tooltip = AstronomicalWidget._effective_layer_label(
        SimpleNamespace(),
        selection,
        "Paleta sintètica",
        runtime={
            "source_id": "fallback",
            "status": "automatic_runtime_fallback",
        },
        registry=registry,
    )

    assert text == "Fallback land cover · 10 m"
    assert "Candidata del catàleg: Configured RGB" in tooltip
    assert "Motiu: automatic_runtime_fallback" in tooltip


def test_runtime_empty_surface_source_reports_synthetic_fallback():
    selection = SimpleNamespace(
        configured=None,
        effective=SimpleNamespace(display_name="Broken RGB"),
        reason="automatic",
    )

    text, tooltip = AstronomicalWidget._effective_layer_label(
        SimpleNamespace(),
        selection,
        "Paleta sintètica",
        runtime={"source_id": "", "status": "fallback_synthetic"},
        registry=SimpleNamespace(get=lambda _source_id: None),
    )

    assert text == "Paleta sintètica"
    assert "Motiu: fallback_synthetic" in tooltip


def test_surface_only_catalogue_change_recolors_latest_profile_without_bake():
    calls = []
    coordinator = SimpleNamespace(
        request_surface_refresh=lambda *args: calls.append(
            ("surface", args)
        ),
        abort_current_job=lambda: calls.append(("abort", ())),
    )
    dummy = SimpleNamespace(
        horizon_worker=SimpleNamespace(),
        terrain_coordinator=coordinator,
        _effective_data_sources_payload={"runtime": "old"},
        _refresh_data_layer_indicators=lambda: calls.append(
            ("labels", ())
        ),
        request_relocation=lambda: calls.append(("relocate", ())),
    )

    AstronomicalWidget._apply_data_layer_changes(
        dummy, DataLayerChanges(surface=True)
    )

    assert ("surface", ()) in calls
    assert not any(name in {"abort", "relocate"} for name, _args in calls)
    assert dummy._effective_data_sources_payload is None


def test_checked_surface_layer_requests_initial_refresh_once():
    calls = []
    dummy = SimpleNamespace(
        chk_surface_layer=_CheckedBox(True),
        _initial_surface_refresh_requested=False,
        on_surface_layer_toggled=lambda checked: calls.append(bool(checked)),
    )

    AstronomicalWidget._activate_checked_surface_layer_startup(dummy)
    AstronomicalWidget._activate_checked_surface_layer_startup(dummy)

    assert calls == [True]
    assert dummy._initial_surface_refresh_requested is True


def test_disabling_surface_layer_hides_material_and_does_not_start_sampling(
    monkeypatch,
):
    refresh_calls = []
    cancel_calls = []
    visibility_calls = []
    tooltip_hides = []
    monkeypatch.setattr(
        "TerraLab.ui.widget_mixins.layers.QToolTip.hideText",
        lambda: tooltip_hides.append(True),
    )
    dummy = SimpleNamespace(
        terrain_coordinator=SimpleNamespace(
            request_surface_refresh=lambda **kwargs: refresh_calls.append(kwargs),
            cancel_surface_refresh=lambda: cancel_calls.append(True),
        ),
        canvas=SimpleNamespace(
            horizon_overlay=SimpleNamespace(
                set_surface_visible=lambda visible: visibility_calls.append(
                    bool(visible)
                )
            ),
            update=lambda: None,
        ),
        _persist_visibility_state=lambda *_args: None,
        _surface_refresh_view_kwargs=lambda: {"view_fov_deg": 75.0},
    )

    AstronomicalWidget.on_surface_layer_toggled(dummy, False)

    assert refresh_calls == []
    assert cancel_calls == [True]
    assert visibility_calls == [False]
    assert tooltip_hides == [True]
