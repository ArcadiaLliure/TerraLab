from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_vertical_drawer_preserves_controls_and_quick_actions(
    tmp_path: Path,
) -> None:
    appdata = tmp_path / "appdata"
    data_root = tmp_path / "library"
    config_dir = appdata / "TerraLab" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        """
        {
          "ui_onboarding_done": true,
          "performance": {
            "scope_preload_mode": "disabled",
            "defer_catalog_until_horizon_preview": true
          }
        }
        """,
        encoding="utf-8",
    )

    script = textwrap.dedent(
        """
        from PyQt5.QtCore import QPoint
        from PyQt5.QtWidgets import (
            QApplication,
            QGroupBox,
            QScrollArea,
            QStackedWidget,
        )
        from TerraLab.ui.astronomical_widget import AstronomicalWidget

        app = QApplication.instance() or QApplication([])
        widget = AstronomicalWidget(parent=None, frameless=False)
        widget._build_deferred_controls_ui()
        widget.resize(1024, 768)
        widget.show()
        app.processEvents()

        assert widget.size().width() == 1024
        assert widget.size().height() == 768
        assert isinstance(widget.drawer_stack, QStackedWidget)
        assert widget.panels_widget is widget.control_drawer
        assert not hasattr(widget, "control_tabs")
        assert widget.control_drawer.width() == 258
        assert widget.drawer_rail.width() == 44
        assert list(widget.drawer_pages) == [
            "location",
            "sky",
            "earth",
            "tools",
        ]
        assert widget.drawer_stack.currentWidget() is widget.sky_controls_page
        assert widget._current_drawer_key == "sky"
        assert widget.drawer_buttons["sky"].isChecked()
        assert widget.control_drawer.findChildren(QScrollArea) == []

        old_group_titles = {
            group.title() for group in widget.findChildren(QGroupBox)
        }
        assert "Localització" not in old_group_titles
        assert "Visió del cel" not in old_group_titles
        assert "Visió del terra" not in old_group_titles

        for control in (
            widget.txt_lat,
            widget.txt_lon,
            widget.spin_extra_height,
            widget.lbl_date,
            widget.btn_realtime,
        ):
            assert widget.location_controls_page.isAncestorOf(control)

        for control in (
            widget.chk_clima,
            widget.chk_solar_system,
            widget.chk_planets,
            widget.chk_sun_moon,
            widget.chk_enable_milkyway,
            widget.chk_enable_planck_dust,
            widget.chk_deep_space,
            widget.chk_enable_sky,
            widget.slider_size,
            widget.slider_spikes,
            widget.txt_search,
            widget.chk_trails,
            widget.btn_scope_panel,
            widget.btn_tools_panel,
            widget.scope_focal_spin,
            widget.scope_instrument_combo,
            widget.scope_aperture_spin,
            widget.scope_iso_spin,
            widget.scope_exposure_spin,
            widget.scope_ra_h_spin,
            widget.scope_dec_d_spin,
            widget.scope_shape_combo,
            widget.scope_sensor_combo,
            widget.scope_aspect_combo,
            widget.scope_speed_combo,
        ):
            assert widget.sky_controls_page.isAncestorOf(control)

        for control in (
            widget.btn_manage_layers,
            widget.chk_enable_horizon,
            widget.chk_enable_village,
            widget.chk_light_pollution,
            widget.chk_terrain_3d,
            widget.combo_layers,
            widget.slider_terrain_depth,
            widget.slider_terrain_ray_precision,
        ):
            assert widget.earth_controls_page.isAncestorOf(control)

        for control in (
            widget.btn_tool_ruler,
            widget.btn_tool_square,
            widget.btn_tool_rect,
            widget.btn_tool_circle,
            widget.btn_tool_clear,
            widget.btn_const_visibility,
            widget.btn_const_draw,
            widget.btn_const_new,
            widget.btn_const_eraser,
            widget.btn_const_rename,
        ):
            assert widget.tools_controls_page.isAncestorOf(control)

        assert [
            widget.btn_toolbar_realtime.text(),
            widget.btn_toolbar_search.text(),
            widget.btn_toolbar_layers.text(),
            widget.btn_toolbar_scope.text(),
            widget.btn_toolbar_tools.text(),
        ] == [
            "Temps real",
            "Cercar objecte",
            "Gestionar capes",
            "Tub / Telescopi",
            "Eines",
        ]

        initial_realtime = widget.btn_realtime.isChecked()
        widget.btn_toolbar_realtime.click()
        assert widget.btn_realtime.isChecked() is (not initial_realtime)
        assert (
            widget.btn_toolbar_realtime.isChecked()
            is widget.btn_realtime.isChecked()
        )

        widget.btn_toolbar_search.click()
        assert widget.drawer_stack.currentWidget() is widget.sky_controls_page
        assert widget.sky_mode_stack.currentWidget() is widget.sky_base_page
        assert widget.txt_search.hasFocus()

        widget.btn_toolbar_tools.click()
        assert widget.drawer_stack.currentWidget() is widget.tools_controls_page
        assert widget._current_drawer_key == "tools"

        widget.btn_toolbar_scope.click()
        assert widget.drawer_stack.currentWidget() is widget.sky_controls_page
        assert widget.sky_mode_stack.currentWidget() is widget.scope_panel
        assert widget.btn_scope_panel.isChecked()

        widget.btn_scope_back.click()
        assert widget.sky_mode_stack.currentWidget() is widget.sky_base_page
        assert not widget.btn_scope_panel.isChecked()

        widget.set_control_drawer("sky")
        app.processEvents()
        canvas_width_open = widget.canvas.width()
        widget.drawer_buttons["sky"].click()
        app.processEvents()
        assert widget.control_drawer.isHidden()
        assert widget._current_drawer_key is None
        assert not any(
            button.isChecked()
            for button in widget.drawer_buttons.values()
        )
        assert widget.drawer_rail.isVisible()
        assert widget.canvas.width() >= canvas_width_open + 250

        widget.drawer_buttons["location"].click()
        app.processEvents()
        assert widget.control_drawer.isVisible()
        assert widget.drawer_stack.currentWidget() is widget.location_controls_page
        assert widget.drawer_buttons["location"].isChecked()
        assert widget.canvas.width() <= canvas_width_open + 8

        for key, page in widget.drawer_pages.items():
            widget.set_control_drawer(key)
            app.processEvents()
            assert widget.drawer_stack.currentWidget() is page

        def assert_control_fits(page, control):
            top = control.mapTo(page, QPoint(0, 0)).y()
            assert top >= 0
            assert top + control.height() <= page.height(), (
                control.objectName() or control.text(),
                top,
                control.height(),
                page.height(),
            )

        for page, last_control in (
            (widget.location_controls_page, widget.btn_realtime),
            (widget.sky_base_page, widget.btn_tools_panel),
            (
                widget.earth_controls_page,
                widget.slider_terrain_ray_precision,
            ),
            (widget.tools_controls_page, widget.lbl_constellation_state),
        ):
            assert_control_fits(page, last_control)

        widget.set_control_drawer("sky")
        widget.btn_scope_panel.setChecked(True)
        app.processEvents()
        assert_control_fits(widget.scope_panel, widget.btn_scope_exit)

        assert widget.canvas.hud_visible
        widget.canvas.btn_hud_toggle.setChecked(False)
        assert not widget.canvas.hud_visible
        assert widget.canvas.btn_human_eye.isHidden()
        widget.canvas.btn_hud_toggle.setChecked(True)
        assert widget.canvas.hud_visible

        timeline_layout = widget.frame_controls.layout()
        assert timeline_layout.count() == 1
        assert timeline_layout.indexOf(widget.time_bar) == 0
        assert widget.content_layout.indexOf(widget.quick_toolbar) == 0
        assert widget.content_layout.indexOf(widget.viewport_shell) == 1
        assert widget.content_layout.indexOf(widget.frame_controls) == 2
        viewport_layout = widget.viewport_shell.layout()
        assert viewport_layout.indexOf(widget.canvas) == 0
        assert viewport_layout.indexOf(widget.control_drawer) == 1
        assert viewport_layout.indexOf(widget.drawer_rail) == 2

        widget.close()
        app.processEvents()
        assert not hasattr(widget.terrain_coordinator, "worker_thread")
        assert not hasattr(widget.canvas, "_thread")
        """
    )
    env = os.environ.copy()
    env.update(
        {
            "APPDATA": str(appdata),
            "QT_QPA_PLATFORM": "offscreen",
            "TERRALAB_DATA_ROOT": str(data_root),
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
    )
