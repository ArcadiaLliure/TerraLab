import os
import shutil
import tempfile
import zipfile
from datetime import datetime

from TerraLab.common.utils import get_config_value
from TerraLab.widgets.physical_math import (
    AtmosphericMath,
    InstrumentOpticsMath,
    VisualPhotometryMath,
)


# Compensacio de render de mLim:
# - 0.0: prioritat a realisme fisic pur.
# - >0: compensa perdues visuals extra del pipeline de render.
DEFAULT_RENDER_MLIM_COMPENSATION_MAG = 0.0
RENDER_MLIM_COMPENSATION_DESCRIPTION = (
    "Compensa penalitzacions visuals addicionals del pipeline de render; "
    "manteniu-la a 0.0 si es prioritza realisme físic."
)


def compute_airmass(h_deg):
    return AtmosphericMath.airmass_from_altitude_deg(h_deg)


COPERNICUS_CAMS_DATASET = "cams-global-atmospheric-composition-forecasts"
COPERNICUS_AOD_VARIABLE = "total_aerosol_optical_depth_550nm"
COPERNICUS_PRESSURE_VARIABLE = "surface_pressure"
CDS_CACHE_TTL_SECONDS = 12 * 3600
CDS_CACHE_KEY_AOD = f"{COPERNICUS_CAMS_DATASET}:{COPERNICUS_AOD_VARIABLE}"
CDS_CACHE_KEY_PRESSURE = f"{COPERNICUS_CAMS_DATASET}:{COPERNICUS_PRESSURE_VARIABLE}"
CDS_CACHE_KEY_COMBINED = f"{COPERNICUS_CAMS_DATASET}:combined_metrics"


def _build_cds_client(timeout_s=15, api_key=None, api_url=None):
    try:
        import cdsapi
    except Exception:
        return None

    config_api_key = str(api_key or get_config_value("copernicus_api_key", "") or "").strip()
    config_api_url = str(
        api_url
        or get_config_value("copernicus_api_url", "https://cds.climate.copernicus.eu/api")
        or "https://cds.climate.copernicus.eu/api"
    ).strip()

    auth_kwargs = []
    if config_api_key:
        auth_kwargs.append({"url": config_api_url, "key": config_api_key})
    auth_kwargs.append({})

    # Versions of cdsapi have slightly different constructor args.
    base_kwargs_list = (
        {"quiet": True, "debug": False, "verify": True, "timeout": timeout_s, "progress": False},
        {"quiet": True, "debug": False, "verify": True, "timeout": timeout_s},
        {"quiet": True, "debug": False},
    )
    for auth in auth_kwargs:
        for kwargs in base_kwargs_list:
            try:
                return cdsapi.Client(**kwargs, **auth)
            except TypeError:
                continue
            except Exception:
                continue
    return None


def _copernicus_cycle_from_utc(now_utc):
    hour = int(now_utc.hour)
    run_hour = 12 if hour >= 12 else 0
    lead_hour = hour - run_hour
    run_date = now_utc.strftime("%Y-%m-%d")
    run_time = f"{run_hour:02d}:00"
    return run_date, run_time, str(max(0, lead_hour))


def _download_copernicus_cams_snapshot(lat, lon, now_utc, api_key=None, api_url=None):
    client = _build_cds_client(api_key=api_key, api_url=api_url)
    if client is None:
        return None

    run_date, run_time, lead_hour = _copernicus_cycle_from_utc(now_utc)
    north = max(-90.0, min(90.0, float(lat) + 0.2))
    south = max(-90.0, min(90.0, float(lat) - 0.2))
    west = max(-180.0, min(180.0, float(lon) - 0.2))
    east = max(-180.0, min(180.0, float(lon) + 0.2))

    # CAMS delivers gridded data. We request a tiny bbox around the observer.
    base_request = {
        "date": [run_date],
        "type": ["forecast"],
        "time": [run_time],
        "leadtime_hour": [lead_hour],
        "variable": [COPERNICUS_AOD_VARIABLE, COPERNICUS_PRESSURE_VARIABLE],
        "area": [north, west, south, east],
    }
    analysis_request = {
        "date": [run_date],
        "type": ["analysis"],
        "time": [f"{int(now_utc.hour):02d}:00"],
        "leadtime_hour": ["0"],
        "variable": [COPERNICUS_AOD_VARIABLE, COPERNICUS_PRESSURE_VARIABLE],
        "area": [north, west, south, east],
    }
    request_variants = [
        {**base_request, "data_format": "netcdf"},
        {**base_request, "format": "netcdf"},
        {**analysis_request, "data_format": "netcdf"},
        {**analysis_request, "format": "netcdf"},
        {**base_request, "variable": ["aod550", "surface_pressure"], "data_format": "netcdf"},
        {**base_request, "variable": ["aod550", "sp"], "data_format": "netcdf"},
    ]

    workdir = tempfile.mkdtemp(prefix="terralab_cds_")
    target_path = os.path.join(workdir, "cams_snapshot.nc")
    try:
        for request in request_variants:
            try:
                if os.path.exists(target_path):
                    os.remove(target_path)
                client.retrieve(COPERNICUS_CAMS_DATASET, request, target_path)
                if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                    return target_path
            except Exception:
                continue
    except Exception:
        pass
    shutil.rmtree(workdir, ignore_errors=True)
    return None


def _first_scalar_value(raw):
    data = raw
    try:
        data = raw[:]
    except Exception:
        pass
    try:
        if hasattr(data, "filled"):
            data = data.filled(float("nan"))
    except Exception:
        pass
    try:
        return float(data.flat[0])
    except Exception:
        pass
    try:
        return float(data[0])
    except Exception:
        return None


def _find_variable(variables, aliases):
    if not variables:
        return None
    lower_map = {str(name).lower(): name for name in variables.keys()}
    for alias in aliases:
        if alias in variables:
            return variables[alias]
        mapped_name = lower_map.get(str(alias).lower())
        if mapped_name is not None:
            return variables[mapped_name]
    alias_tokens = [str(alias).lower() for alias in aliases]
    for name, variable in variables.items():
        lname = str(name).lower()
        if any(token in lname for token in alias_tokens):
            return variable
    return None


def _extract_aod_pressure_from_netcdf(path):
    aod = None
    pressure_hpa = None

    nc_path = path
    cleanup_paths = []
    try:
        with open(path, "rb") as fh:
            header = fh.read(4)
        if header.startswith(b"PK"):
            extract_dir = tempfile.mkdtemp(prefix="terralab_cds_zip_")
            cleanup_paths.append(extract_dir)
            with zipfile.ZipFile(path, "r") as zf:
                nc_members = [m for m in zf.namelist() if str(m).lower().endswith(".nc")]
                if not nc_members:
                    return None, None
                extracted_member = zf.extract(nc_members[0], path=extract_dir)
                nc_path = extracted_member
    except Exception:
        pass

    try:
        # Try netCDF4 first if available.
        try:
            from netCDF4 import Dataset  # type: ignore

            with Dataset(nc_path, "r") as ds:
                variables = ds.variables
                aod_var = _find_variable(
                    variables,
                    (
                        "total_aerosol_optical_depth_550nm",
                        "total_aerosol_optical_depth_at_550_nm",
                        "aod550",
                    ),
                )
                pressure_var = _find_variable(variables, ("surface_pressure", "sp"))
                aod = _first_scalar_value(aod_var) if aod_var is not None else None
                pressure_hpa = _first_scalar_value(pressure_var) if pressure_var is not None else None
        except Exception:
            from scipy.io import netcdf  # type: ignore

            with netcdf.netcdf_file(nc_path, "r", mmap=False) as ds:
                variables = ds.variables
                aod_var = _find_variable(
                    variables,
                    (
                        "total_aerosol_optical_depth_550nm",
                        "total_aerosol_optical_depth_at_550_nm",
                        "aod550",
                    ),
                )
                pressure_var = _find_variable(variables, ("surface_pressure", "sp"))
                aod = _first_scalar_value(aod_var.data if aod_var is not None else None) if aod_var is not None else None
                pressure_hpa = (
                    _first_scalar_value(pressure_var.data if pressure_var is not None else None)
                    if pressure_var is not None
                    else None
                )
    except Exception:
        aod = None
        pressure_hpa = None
    finally:
        for file_path in cleanup_paths:
            try:
                if file_path and os.path.exists(file_path):
                    if os.path.isdir(file_path):
                        shutil.rmtree(file_path, ignore_errors=True)
                    else:
                        os.remove(file_path)
            except Exception:
                pass

    if pressure_hpa is not None and pressure_hpa > 2000.0:
        pressure_hpa = pressure_hpa / 100.0
    if aod is not None and aod < 0.0:
        aod = 0.0
    if pressure_hpa is not None and pressure_hpa <= 0.0:
        pressure_hpa = None
    return aod, pressure_hpa


def fetch_copernicus_aod_pressure(lat, lon, now_utc, api_key=None, api_url=None):
    if not isinstance(now_utc, datetime):
        now_utc = datetime.utcnow()
    elif now_utc.tzinfo is not None:
        now_utc = now_utc.replace(tzinfo=None)
    snapshot_path = _download_copernicus_cams_snapshot(lat, lon, now_utc, api_key=api_key, api_url=api_url)
    if not snapshot_path:
        return None, None
    try:
        return _extract_aod_pressure_from_netcdf(snapshot_path)
    finally:
        try:
            parent_dir = os.path.dirname(snapshot_path)
            shutil.rmtree(parent_dir, ignore_errors=True)
        except Exception:
            pass


def _cache_entry_is_fresh(entry, now_utc, ttl_seconds=CDS_CACHE_TTL_SECONDS):
    if not isinstance(entry, dict):
        return False
    ts = entry.get("ts")
    if not isinstance(ts, datetime):
        return False
    return abs((now_utc - ts).total_seconds()) < float(ttl_seconds)


def _read_cds_cached_metrics(cache, now_utc):
    # Backward compatibility with previous flat cache format.
    if isinstance(cache, dict):
        ts = cache.get("ts")
        if isinstance(ts, datetime):
            age = abs((now_utc - ts).total_seconds())
            if age < CDS_CACHE_TTL_SECONDS:
                return cache.get("aod"), cache.get("pressure_hpa")

    datasets = cache.get("datasets", {}) if isinstance(cache, dict) else {}
    if not isinstance(datasets, dict):
        return None, None

    combined = datasets.get(CDS_CACHE_KEY_COMBINED)
    if _cache_entry_is_fresh(combined, now_utc):
        return combined.get("aod"), combined.get("pressure_hpa")

    aod_entry = datasets.get(CDS_CACHE_KEY_AOD)
    pressure_entry = datasets.get(CDS_CACHE_KEY_PRESSURE)
    if _cache_entry_is_fresh(aod_entry, now_utc) and _cache_entry_is_fresh(pressure_entry, now_utc):
        return aod_entry.get("value"), pressure_entry.get("value")
    return None, None


def _write_cds_cached_metrics(cache, now_utc, aod, pressure_hpa):
    datasets = {}
    if isinstance(cache, dict) and isinstance(cache.get("datasets"), dict):
        datasets = dict(cache.get("datasets", {}))
    datasets[CDS_CACHE_KEY_AOD] = {"ts": now_utc, "value": aod}
    datasets[CDS_CACHE_KEY_PRESSURE] = {"ts": now_utc, "value": pressure_hpa}
    datasets[CDS_CACHE_KEY_COMBINED] = {"ts": now_utc, "aod": aod, "pressure_hpa": pressure_hpa}
    return {
        "ts": now_utc,
        "aod": aod,
        "pressure_hpa": pressure_hpa,
        "datasets": datasets,
    }


def compute_extinction_k(aod, pressure_hpa, k_fallback=0.20):
    return AtmosphericMath.extinction_k_mag_per_airmass(aod, pressure_hpa, k_fallback=k_fallback)


def _is_camera_profile(instrument_profile):
    return InstrumentOpticsMath.is_camera_profile(instrument_profile)


def _compute_exit_pupil_mm(aperture_mm, focal_mm, ocular_mm, instrument_profile):
    if _is_camera_profile(instrument_profile):
        return None
    magnification = InstrumentOpticsMath.magnification(
        telescope_focal_mm=focal_mm,
        eyepiece_focal_mm=ocular_mm,
        is_camera=False,
    )
    return InstrumentOpticsMath.exit_pupil_mm(
        aperture_mm=aperture_mm,
        magnification=magnification,
        is_camera=False,
    )


def _compute_loss_and_transmission(extinction_k_mag_airmass, airmass_x):
    loss_mag = AtmosphericMath.loss_mag_from_k_airmass(extinction_k_mag_airmass, airmass_x)
    transmission = AtmosphericMath.transmission_from_loss_mag(loss_mag)
    return loss_mag, transmission


def _compute_bortle_nelm_mag(bortle_class):
    return VisualPhotometryMath.bortle_to_nelm_mag(bortle_class)


def _compute_general_render_mlim_mag(bortle_class, render_compensation_mag=DEFAULT_RENDER_MLIM_COMPENSATION_MAG):
    return VisualPhotometryMath.general_render_limit_mag(
        bortle_class=bortle_class,
        render_compensation_mag=render_compensation_mag,
    )


def update_telescope_hud(state, allow_remote_fetch=True):
    if not bool(state.get("scope_enabled", False)):
        return state

    aperture_mm = max(1e-6, float(state.get("aperture_mm", 80.0)))
    focal_mm = max(1e-6, float(state.get("focal_mm", 250.0)))
    ocular_mm = max(1e-6, float(state.get("ocular_mm", 20.0)))
    instrument_profile = str(state.get("instrument_profile", "telescope"))
    h_deg = float(state.get("h_deg", 0.0))
    lat = float(state.get("lat", 0.0))
    lon = float(state.get("lon", 0.0))
    weather_enabled = bool(state.get("weather_enabled", False))
    copernicus_api_key = str(state.get("copernicus_api_key", "") or "").strip()
    copernicus_api_url = str(state.get("copernicus_api_url", "") or "").strip()
    now_utc = state.get("now_utc")
    if not isinstance(now_utc, datetime):
        now_utc = datetime.utcnow()
    elif now_utc.tzinfo is not None:
        now_utc = now_utc.replace(tzinfo=None)

    exit_pupil_mm = _compute_exit_pupil_mm(
        aperture_mm=aperture_mm,
        focal_mm=focal_mm,
        ocular_mm=ocular_mm,
        instrument_profile=instrument_profile,
    )
    airmass_x = compute_airmass(h_deg)

    cache = state.get("_wx_cache", {})
    aod = None
    pressure_hpa = None
    if weather_enabled:
        aod, pressure_hpa = _read_cds_cached_metrics(cache, now_utc)

        if (aod is None or pressure_hpa is None) and bool(allow_remote_fetch):
            aod, pressure_hpa = fetch_copernicus_aod_pressure(
                lat,
                lon,
                now_utc,
                api_key=copernicus_api_key,
                api_url=copernicus_api_url if copernicus_api_url else None,
            )
            state["_wx_cache"] = _write_cds_cached_metrics(cache, now_utc, aod, pressure_hpa)
        elif aod is not None and pressure_hpa is not None and isinstance(cache, dict):
            # Preserve existing structure and refresh flat fields for compatibility.
            state["_wx_cache"] = _write_cds_cached_metrics(cache, now_utc, aod, pressure_hpa)
    else:
        # Climate switch OFF => force offline fallback (k_fallback).
        aod = None
        pressure_hpa = None

    k_fallback = float(state.get("k_fallback", 0.20))
    k = compute_extinction_k(aod, pressure_hpa, k_fallback=k_fallback)
    loss_mag, transmission = _compute_loss_and_transmission(k, airmass_x)

    state["hud_metrics"] = {
        "exit_pupil_mm": exit_pupil_mm,
        "airmass_x": airmass_x,
        "extinction_k": k,
        "loss_mag": loss_mag,
        "transmission": transmission,
        "k_fallback_used": bool(aod is None or pressure_hpa is None),
    }
    return state


def on_telescope_view_enabled(state, allow_remote_fetch=True):
    state["scope_enabled"] = True
    update_telescope_hud(state, allow_remote_fetch=allow_remote_fetch)
    update_star_rendering_params(state)
    return state


def on_resize(state):
    panel_x = int(state.get("panel_x", 0))
    panel_y = int(state.get("panel_y", 0))
    panel_w = int(state.get("panel_w", 0))
    button_w = int(state.get("button_w", 30))
    button_h = int(state.get("button_h", 24))
    margin_right = int(state.get("margin_right", 20))
    overlap_top = int(state.get("overlap_top", 1))

    x = panel_x + panel_w - button_w - margin_right
    y = panel_y - button_h + overlap_top
    state["collapse_button_pos"] = (x, y)
    return state


def update_star_rendering_params(state):
    scope_enabled = bool(state.get("scope_enabled", False))
    auto_bortle = bool(state.get("auto_bortle", True))
    bortle = max(1.0, min(9.0, float(state.get("bortle", 1.0))))
    scope_mlim = float(state.get("scope_mlim", 6.0))
    manual_mlim = float(state.get("manual_mlim", 6.0))
    render_compensation_mag = float(
        state.get("render_compensation_mag", DEFAULT_RENDER_MLIM_COMPENSATION_MAG)
    )

    if auto_bortle:
        general_mlim, physical_nelm = _compute_general_render_mlim_mag(
            bortle_class=bortle,
            render_compensation_mag=render_compensation_mag,
        )
    else:
        general_mlim = manual_mlim
        physical_nelm = manual_mlim

    general_mlim = max(-12.0, min(9.0, general_mlim))
    state["general_mlim_physical"] = float(physical_nelm)
    state["general_mlim_compensation_mag"] = float(render_compensation_mag if auto_bortle else 0.0)
    state["general_mlim_compensation_description"] = RENDER_MLIM_COMPENSATION_DESCRIPTION
    state["general_mlim"] = general_mlim
    state["scope_mlim"] = scope_mlim
    state["render_mag_limit"] = scope_mlim if scope_enabled else general_mlim
    return state
