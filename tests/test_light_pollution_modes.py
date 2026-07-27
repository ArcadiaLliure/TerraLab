from __future__ import annotations

from types import SimpleNamespace

import pytest

from TerraLab.light_pollution.modes import (
    LIGHT_POLLUTION_MODES,
    LP_MODE_AUTOMATIC,
    LP_MODE_BORTLE,
    LP_MODE_MAGNITUDE,
    normalize_light_pollution_mode,
    resolve_bortle_class,
)
from TerraLab.ui.widget_mixins import layers as light_pollution_ui
from TerraLab.ui.astronomical_widget import AstronomicalWidget
from TerraLab.ui.widget_runtime_helpers import request_relocation
from TerraLab.widgets.telescope_runtime import update_star_rendering_params


class _Label:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = str(text)


class _Slider:
    def __init__(self):
        self._value = 0
        self.minimum = 0
        self.maximum = 0
        self.enabled = True
        self.inverted = False
        self.tooltip = ""
        self._lbl_min = _Label()
        self._lbl_max = _Label()
        self._lbl_curr = _Label()

    def blockSignals(self, _blocked):
        return None

    def setRange(self, minimum, maximum):
        self.minimum = int(minimum)
        self.maximum = int(maximum)

    def setValue(self, value):
        self._value = int(value)

    def value(self):
        return self._value

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)

    def setInvertedAppearance(self, inverted):
        self.inverted = bool(inverted)

    def setToolTip(self, text):
        self.tooltip = str(text)


class _Combo:
    def __init__(self, modes):
        self.modes = tuple(modes)

    def itemData(self, index):
        return self.modes[int(index)]


def _control_widget(mode):
    widget = SimpleNamespace(
        light_pollution_mode=mode,
        auto_bortle_estimate=4,
        bortle_value=7,
        magnitude_limit=8.0,
        _catalog_max_mag=12.37,
        slider_light=_Slider(),
        lbl_light_text=_Label(),
    )
    widget._catalog_magnitude_upper_bound = lambda: (
        AstronomicalWidget._catalog_magnitude_upper_bound(widget)
    )
    widget.format_light_pollution_slider_value = lambda value: (
        AstronomicalWidget.format_light_pollution_slider_value(widget, value)
    )
    widget._configure_light_pollution_slider = lambda *args: (
        AstronomicalWidget._configure_light_pollution_slider(widget, *args)
    )
    return widget


def test_modes_are_explicit_and_legacy_manual_migrates_to_magnitude():
    assert LIGHT_POLLUTION_MODES == (
        LP_MODE_BORTLE,
        LP_MODE_MAGNITUDE,
        LP_MODE_AUTOMATIC,
    )
    assert normalize_light_pollution_mode(None, legacy_auto=True) == LP_MODE_AUTOMATIC
    assert normalize_light_pollution_mode(None, legacy_auto=False) == LP_MODE_MAGNITUDE
    assert normalize_light_pollution_mode("manual") == LP_MODE_MAGNITUDE


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (LP_MODE_AUTOMATIC, 5.0),
        (LP_MODE_BORTLE, 7.0),
        (LP_MODE_MAGNITUDE, 4.2),
    ],
)
def test_each_mode_resolves_its_own_graphical_bortle(mode, expected):
    actual = resolve_bortle_class(
        mode,
        automatic_bortle=5,
        bortle_value=7,
        magnitude_limit=6.0,
    )
    assert actual == pytest.approx(expected)


def test_automatic_controls_are_locked_to_estimated_bortle(monkeypatch):
    monkeypatch.setattr(
        light_pollution_ui,
        "getTraduction",
        lambda _key, default: default,
    )
    widget = _control_widget(LP_MODE_AUTOMATIC)

    AstronomicalWidget._sync_light_pollution_controls(widget)

    assert widget.slider_light.enabled is False
    assert widget.slider_light.minimum == 1
    assert widget.slider_light.maximum == 9
    assert widget.slider_light.value() == 4
    assert widget.slider_light.inverted is True
    assert widget.slider_light._lbl_min.text == "9"
    assert widget.slider_light._lbl_max.text == "1"


def test_bortle_controls_are_editable_from_nine_to_one(monkeypatch):
    monkeypatch.setattr(
        light_pollution_ui,
        "getTraduction",
        lambda _key, default: default,
    )
    widget = _control_widget(LP_MODE_BORTLE)

    AstronomicalWidget._sync_light_pollution_controls(widget)

    assert widget.slider_light.enabled is True
    assert widget.slider_light.value() == 7
    assert widget.slider_light._lbl_min.text == "9"
    assert widget.slider_light._lbl_max.text == "1"


def test_magnitude_controls_end_at_faintest_catalog_star(monkeypatch):
    monkeypatch.setattr(
        light_pollution_ui,
        "getTraduction",
        lambda _key, default: default,
    )
    widget = _control_widget(LP_MODE_MAGNITUDE)

    AstronomicalWidget._sync_light_pollution_controls(widget)

    assert widget.slider_light.enabled is True
    assert widget.slider_light.minimum == -270
    assert widget.slider_light.maximum == 124
    assert widget.slider_light._lbl_max.text == "12.4"


def test_selecting_automatic_recalculates_immediately(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        light_pollution_ui,
        "set_config_value",
        lambda key, value: persisted.append((key, value)),
    )
    widget = SimpleNamespace(
        combo_lp_mode=_Combo(LIGHT_POLLUTION_MODES),
        light_pollution_mode=LP_MODE_BORTLE,
        sync_calls=0,
        graphics_calls=0,
        recalculate_calls=0,
    )
    widget._sync_light_pollution_controls = lambda: setattr(
        widget, "sync_calls", widget.sync_calls + 1
    )
    widget._apply_light_pollution_graphics = lambda: setattr(
        widget, "graphics_calls", widget.graphics_calls + 1
    )
    widget.recalculate_automatic_light_pollution = lambda: setattr(
        widget, "recalculate_calls", widget.recalculate_calls + 1
    )

    AstronomicalWidget.on_lp_mode_changed(widget, 2)

    assert widget.light_pollution_mode == LP_MODE_AUTOMATIC
    assert widget.recalculate_calls == 1
    assert persisted == [("light_pollution_mode", LP_MODE_AUTOMATIC)]


def test_location_change_recalculates_only_in_automatic_mode(monkeypatch):
    monkeypatch.setattr(
        "TerraLab.ui.widget_runtime_helpers.set_config_value",
        lambda _key, _value: None,
    )

    def build_widget(mode):
        canvas = SimpleNamespace(
            _sf_cache={},
            _eclipse_cache={},
            _last_skyfield_update=0,
            hint_overlay=SimpleNamespace(show_hint=lambda _text: None),
        )
        widget = SimpleNamespace(
            txt_lat=SimpleNamespace(text=lambda: "40.0"),
            txt_lon=SimpleNamespace(text=lambda: "2.0"),
            latitude=41.0,
            longitude=1.0,
            light_pollution_mode=mode,
            canvas=canvas,
            bake_debounce_timer=SimpleNamespace(start=lambda _ms: None),
            time_bar=SimpleNamespace(update_params=lambda *_args: None),
                manual_day=1,
                recalculate_calls=0,
                _observer_offset=0.0,
                terrain_coordinator=SimpleNamespace(
                    get_bare_elevation=lambda _lat, _lon: None
                ),
                update_altitude_label=lambda: None,
            )
        widget.recalculate_automatic_light_pollution = lambda: setattr(
            widget, "recalculate_calls", widget.recalculate_calls + 1
        )
        widget.coverage_sync_calls = 0
        widget._sync_surface_mode_control = lambda: setattr(
            widget,
            "coverage_sync_calls",
            widget.coverage_sync_calls + 1,
        )
        return widget

    automatic_widget = build_widget(LP_MODE_AUTOMATIC)
    magnitude_widget = build_widget(LP_MODE_MAGNITUDE)

    request_relocation(automatic_widget)
    request_relocation(magnitude_widget)

    assert automatic_widget.recalculate_calls == 1
    assert magnitude_widget.recalculate_calls == 0
    assert automatic_widget.coverage_sync_calls == 1
    assert magnitude_widget.coverage_sync_calls == 1


def test_magnitude_rendering_preserves_full_requested_range():
    state = {
        "scope_enabled": False,
        "light_pollution_mode": LP_MODE_MAGNITUDE,
        "bortle": 9,
        "scope_mlim": 20.0,
        "magnitude_limit": -27.0,
    }
    update_star_rendering_params(state)
    assert state["render_mag_limit"] == pytest.approx(-27.0)

    state["magnitude_limit"] = 17.4
    update_star_rendering_params(state)
    assert state["render_mag_limit"] == pytest.approx(17.4)
