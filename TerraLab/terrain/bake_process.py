import argparse
import contextlib
import json
import math
import os
import sys
import time
from typing import Iterable

import numpy as np

from TerraLab.terrain.engine import (
    HorizonBaker,
    HorizonProfile,
    build_flat_horizon_profile,
    generate_bands,
)
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
    normalize_terrain_representation_mode,
)


_EVENT_STREAM = None
_EVENT_STREAM_INIT_FAILED = False


def _resolve_event_stream():
    """Resol el canal de sortida dels esdeveniments JSON del subprocess."""
    global _EVENT_STREAM
    global _EVENT_STREAM_INIT_FAILED

    if _EVENT_STREAM is not None:
        return _EVENT_STREAM
    if _EVENT_STREAM_INIT_FAILED:
        return None

    for stream in (getattr(sys, "__stdout__", None), getattr(sys, "stdout", None)):
        if stream is None:
            continue
        if hasattr(stream, "write") and hasattr(stream, "flush"):
            _EVENT_STREAM = stream
            return _EVENT_STREAM

    try:
        # Important a Windows/pythonw: pot no existir cap stream d'alt nivell.
        _EVENT_STREAM = os.fdopen(
            os.dup(1),
            "w",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
        return _EVENT_STREAM
    except Exception:
        _EVENT_STREAM_INIT_FAILED = True
        return None


def _emit_event(event_type: str, **payload) -> None:
    """Emet un esdeveniment JSONL cap al pare sense bloquejar el bake."""
    event = {"type": event_type, **payload}
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    event_stream = _resolve_event_stream()
    if event_stream is None:
        print(
            f"[HorizonBakeProcess] Event stream unavailable (event={event_type}).",
            file=sys.stderr,
            flush=True,
        )
        return
    try:
        event_stream.write(line)
        event_stream.flush()
    except Exception as exc:
        print(
            f"[HorizonBakeProcess] Event emit failed ({event_type}): {exc}",
            file=sys.stderr,
            flush=True,
        )


def _phase_progress(job_id: str, phase: str, start_pct: float, end_pct: float):
    span = float(end_pct) - float(start_pct)
    last_emitted = {"percent": None}

    def _callback(percent: float, _msg: str = "") -> None:
        sub_pct = max(0.0, min(100.0, float(percent)))
        mapped = float(start_pct) + (sub_pct / 100.0) * span
        previous = last_emitted["percent"]
        if (
            previous is not None
            and mapped < float(end_pct)
            and mapped - previous < 1.0
        ):
            return
        last_emitted["percent"] = mapped
        _emit_event(
            "progress", job_id=job_id, phase=phase, percent=round(mapped, 1)
        )

    return _callback


def _resolve_light_pollution_path(explicit_path: str | None = None) -> str:
    if explicit_path and os.path.isfile(explicit_path):
        return str(explicit_path)
    try:
        from TerraLab.config import ConfigManager

        config = ConfigManager()
        if not bool(config.get("light_pollution_enabled", True)):
            return ""
        lp_path = config.get("dvnl_path", "")
        if lp_path and os.path.exists(lp_path):
            return lp_path
    except Exception:
        pass

    base_dir = os.path.dirname(os.path.dirname(__file__))
    local_default = os.path.join(
        base_dir, "data", "light_pollution", "C_DVNL 2022.tif"
    )
    if os.path.exists(local_default):
        return local_default
    return ""


def _create_provider(tiles_dir, progress_callback=None):
    """
    Create the terrain DEM provider used by the bake subprocess.

    Input CRS:
        - The provider API accepts observer coordinates in `EPSG:4326`.
    Internal CRS:
        - Horizon sampling runs in `EPSG:25831`.
    Output CRS:
        - Not applicable (returns a provider object).
    """
    from TerraLab.terrain.providers import create_elevation_provider

    return create_elevation_provider(tiles_dir, progress_callback=progress_callback)


def _resolve_raycast_step_m(provider) -> float:
    default_step_m = 50.0
    fine_dem_threshold_m = 6.0

    resolution_m = None
    try:
        getter = getattr(provider, "get_nominal_resolution_m", None)
        if callable(getter):
            resolution_m = getter()
    except Exception:
        resolution_m = None

    try:
        resolution_m = float(resolution_m)
    except (TypeError, ValueError):
        resolution_m = None

    if resolution_m is not None and resolution_m > 0:
        if resolution_m <= fine_dem_threshold_m:
            step_m = max(5.0, float(resolution_m))
            print(
                "[HorizonBakeProcess] DEM resolution "
                f"{resolution_m:.2f}m -> raycast base step {step_m:.1f}m",
                file=sys.stderr,
                flush=True,
            )
            return step_m
        print(
            "[HorizonBakeProcess] DEM resolution "
            f"{resolution_m:.2f}m -> raycast base step {default_step_m:.1f}m",
            file=sys.stderr,
            flush=True,
        )
    return default_step_m


def _circular_distance_deg(a: float, b: float) -> float:
    diff = abs(float(a) - float(b)) % 360.0
    return min(diff, 360.0 - diff)


def _build_priority_azimuth_order(
    azimuths: Iterable[float],
    view_azimuth: float,
    view_fov_deg: float,
) -> list[int]:
    azimuth_values = list(float(a) for a in azimuths)
    half_fov = max(12.0, min(180.0, float(view_fov_deg) * 0.5 + 8.0))
    primary = []
    secondary = []
    for idx, az in enumerate(azimuth_values):
        dist = _circular_distance_deg(az, view_azimuth)
        target = primary if dist <= half_fov else secondary
        target.append((dist, idx))
    primary.sort(key=lambda item: (item[0], item[1]))
    secondary.sort(key=lambda item: (item[0], item[1]))
    return [idx for _, idx in primary] + [idx for _, idx in secondary]


def _atomic_save_profile(profile: HorizonProfile, path: str) -> None:
    save_started_ns = time.perf_counter_ns()
    tmp_path = f"{path}.tmp.npz"
    max_attempts = 6
    last_exc = None
    for attempt in range(max_attempts):
        try:
            profile.save(tmp_path)
            os.replace(tmp_path, path)
            elapsed_s = (time.perf_counter_ns() - save_started_ns) / 1e9
            try:
                from TerraLab.common.perf_events import append_perf_event
                from TerraLab.common.performance import process_memory_bytes

                rss_bytes, peak_rss_bytes = process_memory_bytes()
                append_perf_event(
                    "terrain.serialization",
                    elapsed_s=round(elapsed_s, 6),
                    azimuths=int(len(profile.azimuths)),
                    bands=int(len(profile.bands)),
                    mesh_present=bool(profile.terrain_mesh),
                    output_bytes=int(os.path.getsize(path)),
                    rss_bytes=int(rss_bytes),
                    peak_rss_bytes=int(peak_rss_bytes),
                )
            except Exception:
                pass
            return
        except PermissionError as exc:
            last_exc = exc
            # On Windows the preview file can be briefly locked by the reader.
            # Retry a few times before giving up.
            time.sleep(0.03 * (attempt + 1))
        except Exception:
            # Keep non-permission errors visible to caller.
            raise
    if last_exc is not None:
        raise last_exc


def _unique_preview_path(base_path: str, current: int) -> str:
    """Return an immutable preview name so Windows readers never block replace."""

    root, extension = os.path.splitext(os.path.abspath(base_path))
    extension = extension or ".npz"
    return (
        f"{root}_{max(0, int(current)):08d}_"
        f"{time.monotonic_ns()}{extension}"
    )


def _save_preview_snapshot(
    path: str,
    lat: float,
    lon: float,
    azimuths,
    bands,
    light_domes,
    light_peak_distances,
    resolved_mask,
    resolved_radius_m: float,
) -> None:
    profile = HorizonProfile(
        azimuths=azimuths,
        bands=bands,
        observer_lat=float(lat),
        observer_lon=float(lon),
        light_domes=light_domes,
        light_peak_distances=light_peak_distances,
        resolved_mask=resolved_mask,
        resolved_radius_m=float(resolved_radius_m),
    )
    _atomic_save_profile(profile, path)


def _reduced_preview_payload(
    azimuths,
    bands,
    light_domes,
    light_peak_distances,
    resolved_mask,
    *,
    maximum_azimuths: int = 1440,
):
    """Return a resolved-only, angularly bounded snapshot payload."""
    azimuths = np.asarray(azimuths)
    resolved = np.asarray(resolved_mask, dtype=bool)
    resolved_indices = np.flatnonzero(resolved)
    limit = max(1, int(maximum_azimuths))
    if resolved_indices.size > limit:
        positions = np.linspace(
            0, resolved_indices.size - 1, limit, dtype=np.int64
        )
        resolved_indices = resolved_indices[positions]
    # Priority-order processing resolves the visible view first.  Sort only
    # after selection so that early previews retain those useful samples.
    indices = np.sort(resolved_indices)
    reduced_bands = []
    for band in bands:
        reduced = {
            key: value
            for key, value in band.items()
            if key not in {
                "angles", "dists", "heights", "surface_angles",
                "surface_dists", "surface_heights",
            }
        }
        for key in (
            "angles", "dists", "heights", "surface_angles",
            "surface_dists", "surface_heights",
        ):
            if key in band:
                reduced[key] = np.asarray(band[key])[indices]
        reduced_bands.append(reduced)
    return (
        azimuths[indices],
        reduced_bands,
        np.asarray(light_domes)[indices],
        np.asarray(light_peak_distances)[indices],
        np.ones(indices.size, dtype=bool),
    )


def _legacy_main():
    """Punt d'entrada del subprocess de bake d'horitzo."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Bake a horizon profile in a separate process."
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--tiles-dir", required=True)
    parser.add_argument("--observer-offset", type=float, default=0.0)
    parser.add_argument("--bands", type=int, default=20)
    parser.add_argument("--ray-step-deg", type=float, default=0.5)
    parser.add_argument("--output", required=True)
    parser.add_argument("--preview-path", required=True)
    parser.add_argument("--view-azimuth", type=float, default=180.0)
    parser.add_argument("--view-fov-deg", type=float, default=90.0)
    parser.add_argument("--view-elevation", type=float, default=0.0)
    parser.add_argument("--range-settings-json", required=True)
    args = parser.parse_args()
    from TerraLab.terrain.ray_precision import normalize_ray_step_deg, ray_count
    ray_step_deg = normalize_ray_step_deg(args.ray_step_deg)

    job_id = str(args.job_id)
    print(
        (
            "[HorizonBakeProcess] Start "
            f"job={job_id} "
            f"exe={sys.executable} "
            f"event_stream_ready={_resolve_event_stream() is not None}"
        ),
        file=sys.stderr,
        flush=True,
    )
    provider = None
    light_sampler = None
    preview_path = os.path.abspath(args.preview_path)
    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        if not os.path.exists(args.tiles_dir):
            raise FileNotFoundError(
                f"Tiles directory not found: {args.tiles_dir}"
            )

        _emit_event(
            "progress",
            job_id=job_id,
            phase="prepare",
            percent=0.0,
            current=0,
            total=0,
        )
        with contextlib.redirect_stdout(sys.stderr):
            provider = _create_provider(
                args.tiles_dir,
                progress_callback=_phase_progress(
                    job_id, "prepare", 0.0, 15.0
                ),
            )
            x_utm, y_utm = provider.transform_coordinates(args.lat, args.lon)
            from TerraLab.terrain.crs import meridian_convergence_degrees

            baker = HorizonBaker(provider)
            baker.grid_convergence_deg = meridian_convergence_degrees(
                args.lon, args.lat
            )
            from TerraLab.terrain.visibility_range import EARTH_RADIUS_M, TerrainRangeSettings, resolve_visibility_range
            settings = TerrainRangeSettings.from_mapping(json.loads(args.range_settings_json))
            baker.R = EARTH_RADIUS_M * (
                settings.effective_earth_radius_factor
                if settings.atmospheric_refraction_enabled else 1.0
            )
            try:
                ground_h = provider.get_elevation(x_utm, y_utm)
            except Exception:
                ground_h = None
            if ground_h is None:
                ground_h = 200.0
            range_result = resolve_visibility_range(
                settings, float(ground_h) + float(args.observer_offset) + float(baker.eye_height)
            )
            vis_radius = float(range_result.resolved_radius_m)
            # Providers sample missing tiles lazily during raycasting. Preloading only
            # the configurable near field avoids materialising a 530 km disk at full DEM resolution.
            preload_radius = min(vis_radius, settings.immediate_preload_radius_km * 1000.0)
            print(
                "[HorizonBakeProcess] Visibility range "
                f"mode={settings.mode} observer_elevation_m={range_result.observer_elevation_m:.1f} "
                f"calculated_radius_km={range_result.calculated_radius_m / 1000.0:.1f} "
                f"applied_radius_km={vis_radius / 1000.0:.1f} "
                f"refraction_factor={(settings.effective_earth_radius_factor if settings.atmospheric_refraction_enabled else 1.0):.6f} "
                f"maximum_km={settings.maximum_radius_km:.1f}",
                file=sys.stderr, flush=True,
            )
            try:
                provider.prepare_region(
                    x_utm,
                    y_utm,
                    preload_radius,
                    progress_callback=_phase_progress(
                        job_id, "prepare", 15.0, 35.0
                    ),
                    abort_check=None,
                )
            except TypeError:
                provider.prepare_region(
                    x_utm,
                    y_utm,
                    preload_radius,
                    progress_callback=_phase_progress(
                        job_id, "prepare", 15.0, 35.0
                    ),
                )

            try:
                from TerraLab.terrain.light_pollution_sampler import (
                    LightPollutionSampler,
                )
                from TerraLab.terrain.providers import CRS_TERRAIN_INTERNAL

                lp_path = _resolve_light_pollution_path()
                light_sampler = LightPollutionSampler(
                    lp_path if lp_path else None
                )
                if light_sampler and lp_path:
                    light_sampler.prepare_region_from_terrain_xy(
                        x_terrain=float(x_utm),
                        y_terrain=float(y_utm),
                        radius_m=float(vis_radius),
                        input_crs=CRS_TERRAIN_INTERNAL,
                    )
            except Exception as exc:
                print(
                    f"[HorizonBakeProcess] Light pollution sampler unavailable: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                light_sampler = None

            band_defs = generate_bands(max(1, int(args.bands)), max_dist_m=vis_radius)
            azimuths = [i * ray_step_deg for i in range(ray_count(ray_step_deg))]
            azimuth_order = _build_priority_azimuth_order(
                azimuths=azimuths,
                view_azimuth=float(args.view_azimuth) % 360.0,
                view_fov_deg=max(1.0, float(args.view_fov_deg)),
            )
            preview_every = max(1, int(math.ceil(len(azimuths) / 18.0)))
            last_preview_emit = {"t": 0.0}

            def _preview_callback(
                current,
                total,
                az_arr,
                bands_arr,
                domes,
                peak_distances,
                resolved_mask,
            ):
                now = time.time()
                if current < total and (now - last_preview_emit["t"]) < 0.20:
                    return
                try:
                    preview_payload = _reduced_preview_payload(
                        az_arr,
                        bands_arr,
                        domes,
                        peak_distances,
                        resolved_mask,
                    )
                    snapshot_path = _unique_preview_path(
                        preview_path, current
                    )
                    _save_preview_snapshot(
                        snapshot_path,
                        args.lat,
                        args.lon,
                        *preview_payload,
                        vis_radius,
                    )
                except Exception as exc:
                    # Preview persistence is best-effort; do not abort the bake.
                    print(
                        f"[HorizonBakeProcess] Preview snapshot skipped: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return
                last_preview_emit["t"] = now
                _emit_event(
                    "preview",
                    job_id=job_id,
                    observer={"lat": float(args.lat), "lon": float(args.lon)},
                    snapshot_path=snapshot_path,
                    current=int(current),
                    total=int(total),
                    resolved_radius_m=vis_radius,
                )

            (
                azimuths_arr,
                bands,
                light_domes,
                light_peak_distances,
                resolved_mask,
            ) = baker.bake_progressive(
                obs_x=x_utm,
                obs_y=y_utm,
                obs_h_ground=float(ground_h) + float(args.observer_offset),
                step_m=_resolve_raycast_step_m(provider),
                d_max=vis_radius,
                delta_az_deg=ray_step_deg,
                band_defs=band_defs,
                azimuth_order=azimuth_order,
                progress_callback=lambda pct, _msg: _emit_event(
                    "progress",
                    job_id=job_id,
                    phase="bake",
                    percent=round(35.0 + (float(pct) / 100.0) * 63.0, 1),
                    current=int(
                        max(
                            0,
                            min(
                                len(azimuths),
                                round((float(pct) / 100.0) * len(azimuths)),
                            ),
                        )
                    ),
                    total=int(len(azimuths)),
                ),
                preview_callback=_preview_callback,
                preview_every=preview_every,
                light_sampler=light_sampler,
                abort_check=None,
            )

            _emit_event(
                "progress",
                job_id=job_id,
                phase="terrain_mesh",
                percent=98.7,
                current=len(azimuths),
                total=len(azimuths),
            )
            terrain_mesh = baker.build_view_mesh(
                obs_x=x_utm,
                obs_y=y_utm,
                obs_h_ground=float(ground_h) + float(args.observer_offset),
                d_max=vis_radius,
                delta_az_deg=ray_step_deg,
            )

            _emit_event(
                "progress",
                job_id=job_id,
                phase="save",
                percent=99.0,
                current=len(azimuths),
                total=len(azimuths),
            )
            final_profile = HorizonProfile(
                azimuths=azimuths_arr,
                bands=bands,
                observer_lat=float(args.lat),
                observer_lon=float(args.lon),
                light_domes=light_domes,
                light_peak_distances=light_peak_distances,
                resolved_mask=np.asarray(resolved_mask, dtype=bool),
                terrain_mesh=terrain_mesh,
                resolved_radius_m=vis_radius,
            )
            _atomic_save_profile(final_profile, output_path)

        _emit_event("done", job_id=job_id, profile_path=output_path, resolved_radius_m=vis_radius)
    except Exception as exc:
        _emit_event("error", job_id=job_id, message=str(exc))
        raise
    finally:
        try:
            if light_sampler and hasattr(light_sampler, "close"):
                light_sampler.close()
        except Exception:
            pass
        try:
            if provider and hasattr(provider, "close"):
                provider.close()
        except Exception:
            pass


def _path_has_elevation_data(path_value: str | None) -> bool:
    raw = str(path_value or "").strip()
    if not raw:
        return False
    path = os.path.abspath(os.path.expanduser(raw))
    if os.path.isfile(path):
        return os.path.splitext(path)[1].lower() in {
            ".tif",
            ".tiff",
            ".vrt",
            ".img",
            ".jp2",
            ".asc",
            ".txt",
            ".npy",
        }
    if not os.path.isdir(path):
        return False
    allowed = {".tif", ".tiff", ".vrt", ".img", ".jp2", ".asc", ".txt", ".npy"}
    return any(
        os.path.splitext(name)[1].lower() in allowed
        for root, _dirs, files in os.walk(path)
        for name in files
    )


def _observer_elevation_sample(provider, x: float, y: float):
    """Sample observer height while retaining typed-chain provenance."""

    sampler = getattr(provider, "sample_elevations", None)
    if callable(sampler):
        try:
            input_crs = str(getattr(provider, "internal_crs", "EPSG:25831"))
            batch = sampler(float(x), float(y), input_crs=input_crs)
            if bool(np.asarray(batch.valid).item()):
                value = float(np.asarray(batch.values).item())
                source_index = int(np.asarray(batch.source_indices).item())
                providers = tuple(getattr(provider, "providers", ()) or ())
                source_id = None
                if 0 <= source_index < len(providers):
                    source_id = str(getattr(providers[source_index], "source_id", "") or "") or None
                return value, source_id, bool(source_id)
        except Exception:
            pass
    value = provider.get_elevation(float(x), float(y))
    return (float(value) if value is not None else None), None, False


def _runtime_source_metadata(args, provider, sampled_source_id, has_runtime_provenance):
    requested = tuple(str(value) for value in (args.elevation_source_id or ()) if str(value))
    if not requested:
        requested = tuple(
            str(getattr(item, "source_id", "") or "")
            for item in tuple(getattr(provider, "providers", ()) or ())
            if str(getattr(item, "source_id", "") or "")
        )
    configured = str(args.effective_elevation_source_id or "") or None
    status = str(args.elevation_source_status or "automatic")
    if has_runtime_provenance and sampled_source_id:
        effective = str(sampled_source_id)
        if configured and configured != effective and not status.endswith("_sample_fallback"):
            status += "_sample_fallback"
    else:
        effective = configured
    return requested, effective, status


def _load_source_snapshot(path_value: str | None):
    path = str(path_value or "").strip()
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        payload = payload.get("sources", payload.get("items", []))
    return payload if isinstance(payload, list) else None


def main(argv=None):
    """Bake a real or explicit flat-fallback profile and return it."""

    parser = argparse.ArgumentParser(
        description="Bake a horizon profile in a separate process."
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--tiles-dir", default="")
    parser.add_argument("--elevation-sources-json", default="")
    parser.add_argument("--elevation-source-id", action="append", default=[])
    parser.add_argument("--effective-elevation-source-id", default="")
    parser.add_argument("--elevation-source-status", default="automatic")
    parser.add_argument("--representation-mode", default="relief")
    parser.add_argument("--light-pollution-path", default="")
    parser.add_argument("--light-pollution-sources-json", default="")
    parser.add_argument("--observer-offset", type=float, default=0.0)
    parser.add_argument("--bands", type=int, default=20)
    parser.add_argument("--ray-step-deg", type=float, default=0.5)
    parser.add_argument("--output", required=True)
    parser.add_argument("--preview-path", required=True)
    parser.add_argument("--view-azimuth", type=float, default=180.0)
    parser.add_argument("--view-fov-deg", type=float, default=90.0)
    parser.add_argument("--view-elevation", type=float, default=0.0)
    parser.add_argument("--range-settings-json", default="{}")
    parser.add_argument("--sampling-settings-json", default="{}")
    parser.add_argument("--viewport-height-px", type=int, default=1080)
    parser.add_argument("--view-zoom-level", type=float, default=1.0)
    args = parser.parse_args(argv)
    from TerraLab.terrain.ray_precision import normalize_ray_step_deg, ray_count
    ray_step_deg = normalize_ray_step_deg(args.ray_step_deg)

    job_id = str(args.job_id)
    mode = normalize_terrain_representation_mode(args.representation_mode)
    output_path = os.path.abspath(args.output)
    preview_path = os.path.abspath(args.preview_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    provider = None
    light_sampler = None
    try:
        source_snapshot = _load_source_snapshot(args.elevation_sources_json)
        provider_input = source_snapshot if source_snapshot else args.tiles_dir
        has_elevation = bool(source_snapshot) or _path_has_elevation_data(args.tiles_dir)
        requested_ids = tuple(str(value) for value in args.elevation_source_id)
        if not has_elevation:
            profile = build_flat_horizon_profile(
                observer_lat=args.lat,
                observer_lon=args.lon,
                representation_mode=mode,
                band_defs=[
                    {"id": "flat_0_150k", "min": 0.0, "max": 150_000.0}
                ],
                delta_az_deg=ray_step_deg,
            )
            profile.elevation_source_ids = requested_ids
            profile.effective_elevation_source_id = None
            profile.elevation_source_status = "fallback_no_elevation"
            _atomic_save_profile(profile, preview_path)
            _atomic_save_profile(profile, output_path)
            _emit_event(
                "done",
                job_id=job_id,
                profile_path=output_path,
                resolved_radius_m=float(profile.resolved_radius_m or 0.0),
            )
            return profile

        _emit_event("progress", job_id=job_id, phase="prepare", percent=0.0)
        provider = _create_provider(
            provider_input,
            progress_callback=_phase_progress(job_id, "prepare", 0.0, 15.0),
        )
        x_utm, y_utm = provider.transform_coordinates(args.lat, args.lon)
        from TerraLab.terrain.crs import meridian_convergence_degrees

        grid_convergence_deg = meridian_convergence_degrees(args.lon, args.lat)
        baker = HorizonBaker(provider)
        baker.grid_convergence_deg = grid_convergence_deg
        from TerraLab.terrain.render_pipeline import TerrainSamplingSettings

        try:
            sampling_payload = json.loads(args.sampling_settings_json or "{}")
        except json.JSONDecodeError:
            sampling_payload = {}
        baker.sampling_settings = TerrainSamplingSettings.from_mapping(
            sampling_payload
        )
        baker.sampling_pixels_per_radian = max(
            1.0,
            (float(args.viewport_height_px) / 45.0)
            * max(0.001, float(args.view_zoom_level))
            * (180.0 / math.pi),
        )
        ground_h, sampled_source_id, has_runtime_provenance = _observer_elevation_sample(
            provider, x_utm, y_utm
        )
        if ground_h is None:
            ground_h = 200.0
        source_ids, effective_source_id, source_status = _runtime_source_metadata(
            args, provider, sampled_source_id, has_runtime_provenance
        )

        from TerraLab.terrain.visibility_range import (
            EARTH_RADIUS_M,
            TerrainRangeSettings,
            resolve_visibility_range,
        )

        try:
            settings_payload = json.loads(args.range_settings_json or "{}")
        except json.JSONDecodeError:
            settings_payload = {}
        settings = TerrainRangeSettings.from_mapping(settings_payload)
        baker.R = EARTH_RADIUS_M * (
            settings.effective_earth_radius_factor
            if settings.atmospheric_refraction_enabled
            else 1.0
        )
        range_result = resolve_visibility_range(
            settings,
            float(ground_h) + float(args.observer_offset) + float(getattr(baker, "eye_height", 1.7)),
        )
        vis_radius = float(range_result.resolved_radius_m)
        try:
            provider.prepare_region(
                x_utm,
                y_utm,
                min(vis_radius, settings.immediate_preload_radius_km * 1000.0),
                progress_callback=_phase_progress(job_id, "prepare", 15.0, 35.0),
            )
        except TypeError:
            provider.prepare_region(x_utm, y_utm, vis_radius)

        light_snapshot = _load_source_snapshot(args.light_pollution_sources_json)
        if light_snapshot:
            from TerraLab.terrain.light_pollution_sampler import create_light_pollution_sampler

            light_sampler = create_light_pollution_sampler(light_snapshot)
        else:
            try:
                lp_path = _resolve_light_pollution_path(args.light_pollution_path)
            except TypeError:
                lp_path = _resolve_light_pollution_path()
            if lp_path:
                from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler

                light_sampler = LightPollutionSampler(lp_path)

        # The vectorized light sampler is fast only when its projected raster
        # window has been prepared.  Without this step a 530 km bake used to
        # fall back to roughly 1.8 million individual GeoTIFF reads.
        if light_sampler is not None:
            from TerraLab.terrain.providers import CRS_TERRAIN_INTERNAL

            light_sampler.prepare_region_from_terrain_xy(
                x_terrain=float(x_utm),
                y_terrain=float(y_utm),
                radius_m=float(vis_radius),
                input_crs=CRS_TERRAIN_INTERNAL,
            )

        band_defs = generate_bands(max(1, int(args.bands)), max_dist_m=vis_radius)
        azimuths = [index * ray_step_deg for index in range(ray_count(ray_step_deg))]
        latest_preview = {"profile": None}

        def preview_callback(current, total, az_arr, bands_arr, domes, peaks, resolved):
            try:
                (
                    preview_azimuths,
                    preview_bands,
                    preview_domes,
                    preview_peaks,
                    preview_resolved,
                ) = _reduced_preview_payload(
                    az_arr, bands_arr, domes, peaks, resolved
                )
                preview = HorizonProfile(
                    azimuths=preview_azimuths,
                    bands=preview_bands,
                    observer_lat=float(args.lat),
                    observer_lon=float(args.lon),
                    light_domes=preview_domes,
                    light_peak_distances=preview_peaks,
                    resolved_mask=preview_resolved,
                    resolved_radius_m=vis_radius,
                    representation_mode=mode,
                    geometry_source=TerrainGeometrySource.REAL_ELEVATION,
                    geometry_id=f"real:{job_id}",
                    elevation_source_ids=source_ids,
                    effective_elevation_source_id=effective_source_id,
                    elevation_source_status=source_status,
                    observer_x=float(x_utm),
                    observer_y=float(y_utm),
                    grid_convergence_deg=grid_convergence_deg,
                )
                snapshot_path = _unique_preview_path(
                    preview_path, current
                )
                _atomic_save_profile(preview, snapshot_path)
                latest_preview["profile"] = preview
            except Exception as exc:
                # A preview is optional and must never cancel the final bake.
                print(
                    f"[HorizonBakeProcess] Preview snapshot skipped: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            _emit_event(
                "preview",
                job_id=job_id,
                snapshot_path=snapshot_path,
                current=int(current),
                total=int(total),
                resolved_radius_m=vis_radius,
            )

        az_arr, bands, domes, peaks, resolved = baker.bake_progressive(
            obs_x=x_utm,
            obs_y=y_utm,
            obs_h_ground=float(ground_h) + float(args.observer_offset),
            step_m=_resolve_raycast_step_m(provider),
            d_max=vis_radius,
            delta_az_deg=ray_step_deg,
            band_defs=band_defs,
            azimuth_order=_build_priority_azimuth_order(
                azimuths, args.view_azimuth, args.view_fov_deg
            ),
            progress_callback=_phase_progress(job_id, "bake", 35.0, 98.0),
            preview_callback=preview_callback,
            preview_every=max(1, int(math.ceil(len(azimuths) / 18.0))),
            light_sampler=light_sampler,
            abort_check=None,
        )
        terrain_mesh = None
        if mode is TerrainRepresentationMode.RELIEF:
            _emit_event("progress", job_id=job_id, phase="terrain_mesh", percent=98.7)
            terrain_mesh = baker.build_view_mesh(
                obs_x=x_utm,
                obs_y=y_utm,
                obs_h_ground=float(ground_h) + float(args.observer_offset),
                d_max=vis_radius,
                delta_az_deg=ray_step_deg,
            )
        final = HorizonProfile(
            azimuths=np.asarray(az_arr),
            bands=bands,
            observer_lat=float(args.lat),
            observer_lon=float(args.lon),
            light_domes=domes,
            light_peak_distances=peaks,
            resolved_mask=np.asarray(resolved, dtype=bool),
            terrain_mesh=terrain_mesh,
            resolved_radius_m=vis_radius,
            representation_mode=mode,
            geometry_source=TerrainGeometrySource.REAL_ELEVATION,
            geometry_id=f"real:{job_id}",
            elevation_source_ids=source_ids,
            effective_elevation_source_id=effective_source_id,
            elevation_source_status=source_status,
            observer_x=float(x_utm),
            observer_y=float(y_utm),
            grid_convergence_deg=grid_convergence_deg,
        )
        _atomic_save_profile(final, output_path)
        # Preserve the historical fixed preview path for tooling that inspects
        # it after completion.  It is published only once, after preview
        # generation has stopped, so no live reader can block a subsequent
        # os.replace on Windows.
        if latest_preview["profile"] is not None:
            _atomic_save_profile(latest_preview["profile"], preview_path)
        _emit_event(
            "done",
            job_id=job_id,
            profile_path=output_path,
            resolved_radius_m=vis_radius,
        )
        return final
    except Exception as exc:
        _emit_event("error", job_id=job_id, message=str(exc))
        raise
    finally:
        for resource in (light_sampler, provider):
            if resource is not None and hasattr(resource, "close"):
                try:
                    resource.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
