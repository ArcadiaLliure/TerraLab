"""Non-blocking bootstrap for the process-owned astronomical runtime."""

from __future__ import annotations

from TerraLab.common.utils import get_config_value
from TerraLab.light_pollution.modes import is_automatic_mode


def _queue_initial_automatic_light_pollution(widget) -> None:
    if not is_automatic_mode(
        getattr(widget, "light_pollution_mode", None)
    ):
        return
    try:
        widget.recalculate_automatic_light_pollution()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        print(f"[AstroWidget] Auto-Bortle startup sync error: {exc}")


def widget_start_async_bootstrap(widget) -> None:
    """Dispatch startup work without creating calculation threads in UI."""

    if getattr(widget, "_async_bootstrap_started", False):
        return
    widget._async_bootstrap_started = True
    widget._horizon_progress_ui_ts = 0.0
    widget._horizon_progress_min_interval_s = max(
        0.05,
        float(
            get_config_value(
                "performance.horizon_progress_min_interval_s",
                0.10,
            )
        ),
    )
    widget._horizon_preview_min_interval_s = max(
        0.10,
        float(
            get_config_value(
                "performance.horizon_preview_min_interval_s",
                0.50,
            )
        ),
    )
    widget._horizon_preview_last_apply_ts = 0.0
    widget._horizon_preview_schedule_id = 0
    widget._horizon_preview_flush_scheduled = False
    widget._horizon_preview_pending_payload = None

    # Both requests cross the Compute process boundary. Neither is a gate for
    # constructing controls or showing the base renderer frame.
    widget._start_catalog_loader_async(reason="bootstrap")
    terrain = widget.terrain_coordinator
    terrain.set_observer_offset(
        float(get_config_value("observer_offset", 0.0))
    )
    widget._schedule_lifecycle_callback(0, terrain.initialize)
    widget._schedule_lifecycle_callback(
        250,
        lambda w=widget: _queue_initial_automatic_light_pollution(w),
    )

    def trigger_bake() -> None:
        print(
            "[AstroWidget] Emitting bake request for "
            f"{widget.latitude}, {widget.longitude}"
        )
        widget._begin_horizon_bake()

    widget._schedule_lifecycle_callback(300, trigger_bake)
