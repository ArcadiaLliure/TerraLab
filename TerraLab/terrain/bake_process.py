import argparse
import contextlib
import json
import math
import os
import sys
import time
from typing import Iterable
import numpy as np

from TerraLab.terrain.engine import HorizonBaker, HorizonProfile, generate_bands


def _emit_event(event_type: str, **payload) -> None:
    event = {"type": event_type, **payload}
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    out = getattr(sys, "__stdout__", sys.stdout)
    out.write(line)
    out.flush()


def _phase_progress(job_id: str, phase: str, start_pct: float, end_pct: float):
    span = float(end_pct) - float(start_pct)

    def _callback(percent: float, _msg: str = "") -> None:
        sub_pct = max(0.0, min(100.0, float(percent)))
        mapped = float(start_pct) + (sub_pct / 100.0) * span
        _emit_event("progress", job_id=job_id, phase=phase, percent=round(mapped, 1))

    return _callback


def _resolve_light_pollution_path() -> str:
    try:
        from TerraLab.config import ConfigManager

        config = ConfigManager()
        lp_path = config.get("dvnl_path", "")
        if lp_path and os.path.exists(lp_path):
            return lp_path
    except Exception:
        pass

    base_dir = os.path.dirname(os.path.dirname(__file__))
    local_default = os.path.join(base_dir, "data", "light_pollution", "C_DVNL 2022.tif")
    if os.path.exists(local_default):
        return local_default
    return ""


def _create_provider(tiles_dir: str, progress_callback=None):
    is_tiff = False
    tiff_path = None

    if os.path.isfile(tiles_dir) and tiles_dir.lower().endswith((".tif", ".tiff")):
        is_tiff = True
        tiff_path = tiles_dir
    elif os.path.isdir(tiles_dir):
        tifs = [f for f in os.listdir(tiles_dir) if f.lower().endswith((".tif", ".tiff"))]
        if tifs:
            is_tiff = True
            tiff_path = os.path.join(tiles_dir, tifs[0])

    if is_tiff and tiff_path:
        from TerraLab.terrain.providers import TiffRasterWindowProvider

        provider = TiffRasterWindowProvider(tiff_path)
    else:
        from TerraLab.terrain.providers import AscRasterProvider

        provider = AscRasterProvider(tiles_dir)

    provider.initialize(progress_callback=progress_callback)
    return provider


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
    tmp_path = f"{path}.tmp.npz"
    profile.save(tmp_path)
    os.replace(tmp_path, path)


def _save_preview_snapshot(
    path: str,
    lat: float,
    lon: float,
    azimuths,
    bands,
    light_domes,
    light_peak_distances,
    resolved_mask,
) -> None:
    profile = HorizonProfile(
        azimuths=azimuths,
        bands=bands,
        observer_lat=float(lat),
        observer_lon=float(lon),
        light_domes=light_domes,
        light_peak_distances=light_peak_distances,
        resolved_mask=resolved_mask,
    )
    _atomic_save_profile(profile, path)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Bake a horizon profile in a separate process.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--tiles-dir", required=True)
    parser.add_argument("--observer-offset", type=float, default=0.0)
    parser.add_argument("--bands", type=int, default=20)
    parser.add_argument("--output", required=True)
    parser.add_argument("--preview-path", required=True)
    parser.add_argument("--view-azimuth", type=float, default=180.0)
    parser.add_argument("--view-fov-deg", type=float, default=90.0)
    parser.add_argument("--view-elevation", type=float, default=0.0)
    args = parser.parse_args()

    job_id = str(args.job_id)
    provider = None
    light_sampler = None
    preview_path = os.path.abspath(args.preview_path)
    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        if not os.path.exists(args.tiles_dir):
            raise FileNotFoundError(f"Tiles directory not found: {args.tiles_dir}")

        _emit_event("progress", job_id=job_id, phase="prepare", percent=0.0, current=0, total=0)
        with contextlib.redirect_stdout(sys.stderr):
            provider = _create_provider(
                args.tiles_dir,
                progress_callback=_phase_progress(job_id, "prepare", 0.0, 15.0),
            )
            baker = HorizonBaker(provider)
            x_utm, y_utm = provider.transform_coordinates(args.lat, args.lon)
            vis_radius = 150000.0
            try:
                provider.prepare_region(
                    x_utm,
                    y_utm,
                    vis_radius,
                    progress_callback=_phase_progress(job_id, "prepare", 15.0, 35.0),
                    abort_check=None,
                )
            except TypeError:
                provider.prepare_region(
                    x_utm,
                    y_utm,
                    vis_radius,
                    progress_callback=_phase_progress(job_id, "prepare", 15.0, 35.0),
                )

            try:
                from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler

                lp_path = _resolve_light_pollution_path()
                light_sampler = LightPollutionSampler(lp_path if lp_path else None)
            except Exception as exc:
                print(f"[HorizonBakeProcess] Light pollution sampler unavailable: {exc}", file=sys.stderr, flush=True)
                light_sampler = None

            try:
                ground_h = provider.get_elevation(x_utm, y_utm)
            except Exception:
                ground_h = None
            if ground_h is None:
                ground_h = 200.0

            band_defs = generate_bands(max(1, int(args.bands)))
            azimuths = [i * 0.5 for i in range(int(round(360.0 / 0.5)))]
            azimuth_order = _build_priority_azimuth_order(
                azimuths=azimuths,
                view_azimuth=float(args.view_azimuth) % 360.0,
                view_fov_deg=max(1.0, float(args.view_fov_deg)),
            )
            preview_every = max(12, min(48, int(max(1, len(azimuths) // 18))))
            last_preview_emit = {"t": 0.0}

            def _preview_callback(current, total, az_arr, bands_arr, domes, peak_distances, resolved_mask):
                now = time.time()
                if current < total and (now - last_preview_emit["t"]) < 0.20:
                    return
                _save_preview_snapshot(
                    preview_path,
                    args.lat,
                    args.lon,
                    az_arr,
                    bands_arr,
                    domes,
                    peak_distances,
                    np.asarray(resolved_mask, dtype=bool),
                )
                last_preview_emit["t"] = now
                _emit_event(
                    "preview",
                    job_id=job_id,
                    observer={"lat": float(args.lat), "lon": float(args.lon)},
                    snapshot_path=preview_path,
                    current=int(current),
                    total=int(total),
                )

            azimuths_arr, bands, light_domes, light_peak_distances, resolved_mask = baker.bake_progressive(
                obs_x=x_utm,
                obs_y=y_utm,
                obs_h_ground=float(ground_h) + float(args.observer_offset),
                step_m=50.0,
                d_max=vis_radius,
                delta_az_deg=0.5,
                band_defs=band_defs,
                azimuth_order=azimuth_order,
                progress_callback=lambda pct, _msg: _emit_event(
                    "progress",
                    job_id=job_id,
                    phase="bake",
                    percent=round(35.0 + (float(pct) / 100.0) * 63.0, 1),
                    current=int(max(0, min(len(azimuths), round((float(pct) / 100.0) * len(azimuths))))),
                    total=int(len(azimuths)),
                ),
                preview_callback=_preview_callback,
                preview_every=preview_every,
                light_sampler=light_sampler,
                abort_check=None,
            )

            _emit_event("progress", job_id=job_id, phase="save", percent=99.0, current=len(azimuths), total=len(azimuths))
            final_profile = HorizonProfile(
                azimuths=azimuths_arr,
                bands=bands,
                observer_lat=float(args.lat),
                observer_lon=float(args.lon),
                light_domes=light_domes,
                light_peak_distances=light_peak_distances,
                resolved_mask=np.asarray(resolved_mask, dtype=bool),
            )
            _atomic_save_profile(final_profile, output_path)

        _emit_event("done", job_id=job_id, profile_path=output_path)
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


if __name__ == "__main__":
    main()
