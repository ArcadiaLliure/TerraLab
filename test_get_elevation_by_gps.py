#!/usr/bin/env python3
"""Consulta una elevación GPS con el mismo proveedor DEM que TerraLab.

Ejemplos::

    python test_get_elevation_by_gps.py 41.193151965759704 1.2025659030876152
    python test_get_elevation_by_gps.py 41.22 1.09 --json
    python test_get_elevation_by_gps.py 41.22 1.09 --dem-path D:\\DEM

La entrada siempre es latitud/longitud WGS84 (EPSG:4326). El script usa
``RasterProvider.transform_coordinates`` y ``sample_elevations`` exactamente
como el proceso de bake de TerraLab. No inicia Qt ni modifica la configuración.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from TerraLab.common.utils import get_config_value
from TerraLab.terrain.providers import create_elevation_provider


DEFAULT_EYE_HEIGHT_M = 1.7
TERRALAB_MISSING_DEM_FALLBACK_M = 200.0


@dataclass(frozen=True, slots=True)
class ElevationResult:
    """Resultado serializable de la consulta de elevación."""

    latitude_deg: float
    longitude_deg: float
    internal_crs: str
    projected_x_m: float
    projected_y_m: float
    dem_path: str
    provider: str
    source_id: str | None
    nominal_resolution_m: float | None
    dem_elevation_m: float | None
    terralab_ground_elevation_m: float
    observer_offset_m: float
    observer_ground_elevation_m: float
    eye_height_m: float
    observer_eye_elevation_m: float
    used_terralab_fallback: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _configured_dem_path(dem_path: str | os.PathLike[str] | None) -> str:
    selected = str(dem_path or "").strip()
    if not selected:
        selected = str(get_config_value("raster_path", "") or "").strip()
    if not selected:
        raise FileNotFoundError(
            "TerraLab no tiene 'raster_path' configurado; usa --dem-path."
        )
    resolved = os.path.abspath(os.path.expanduser(selected))
    if not os.path.exists(resolved):
        raise FileNotFoundError(f"No existe la fuente DEM: {resolved}")
    return resolved


def _validate_gps(latitude: float, longitude: float) -> tuple[float, float]:
    latitude = float(latitude)
    longitude = float(longitude)
    if not math.isfinite(latitude) or not -90.0 <= latitude <= 90.0:
        raise ValueError("La latitud debe estar entre -90 y 90 grados.")
    if not math.isfinite(longitude) or not -180.0 <= longitude <= 180.0:
        raise ValueError("La longitud debe estar entre -180 y 180 grados.")
    return latitude, longitude


def _sample_like_terralab(provider, x: float, y: float):
    """Replica `_observer_elevation_sample` del proceso de bake."""

    sampler = getattr(provider, "sample_elevations", None)
    if callable(sampler):
        try:
            input_crs = str(getattr(provider, "internal_crs", "EPSG:25831"))
            batch = sampler(float(x), float(y), input_crs=input_crs)
            if bool(np.asarray(batch.valid).item()):
                elevation = float(np.asarray(batch.values).item())
                source_index = int(np.asarray(batch.source_indices).item())
                providers = tuple(getattr(provider, "providers", ()) or ())
                if 0 <= source_index < len(providers):
                    source_id = str(
                        getattr(providers[source_index], "source_id", "") or ""
                    )
                else:
                    source_id = str(getattr(provider, "source_id", "") or "")
                return elevation, source_id or None
        except Exception:
            # TerraLab mantiene esta misma compatibilidad con proveedores
            # antiguos que sólo implementan get_elevation().
            pass

    elevation = provider.get_elevation(float(x), float(y))
    if elevation is None or not math.isfinite(float(elevation)):
        return None, None
    source_id = str(getattr(provider, "source_id", "") or "")
    return float(elevation), source_id or None


def get_elevation_by_gps(
    latitude: float,
    longitude: float,
    *,
    dem_path: str | os.PathLike[str] | None = None,
    observer_offset_m: float = 0.0,
    eye_height_m: float = DEFAULT_EYE_HEIGHT_M,
    allow_terralab_fallback: bool = True,
) -> ElevationResult:
    """Resuelve la elevación DEM y las alturas de observador de TerraLab."""

    latitude, longitude = _validate_gps(latitude, longitude)
    observer_offset_m = float(observer_offset_m)
    eye_height_m = float(eye_height_m)
    if not math.isfinite(observer_offset_m):
        raise ValueError("El offset del observador debe ser finito.")
    if not math.isfinite(eye_height_m) or eye_height_m < 0.0:
        raise ValueError("La altura del ojo debe ser finita y no negativa.")

    resolved_dem_path = _configured_dem_path(dem_path)
    provider = create_elevation_provider(resolved_dem_path)
    try:
        x, y = provider.transform_coordinates(latitude, longitude)
        dem_elevation_m, source_id = _sample_like_terralab(provider, x, y)
        if dem_elevation_m is None and not allow_terralab_fallback:
            raise LookupError(
                "La coordenada no tiene cobertura válida en el DEM seleccionado."
            )
        ground_elevation_m = (
            TERRALAB_MISSING_DEM_FALLBACK_M
            if dem_elevation_m is None
            else float(dem_elevation_m)
        )
        observer_ground_m = ground_elevation_m + observer_offset_m
        resolution = provider.get_nominal_resolution_m()
        resolution = (
            float(resolution)
            if resolution is not None and math.isfinite(float(resolution))
            else None
        )
        return ElevationResult(
            latitude_deg=latitude,
            longitude_deg=longitude,
            internal_crs=str(provider.get_native_crs()),
            projected_x_m=float(x),
            projected_y_m=float(y),
            dem_path=resolved_dem_path,
            provider=type(provider).__name__,
            source_id=source_id,
            nominal_resolution_m=resolution,
            dem_elevation_m=dem_elevation_m,
            terralab_ground_elevation_m=ground_elevation_m,
            observer_offset_m=observer_offset_m,
            observer_ground_elevation_m=observer_ground_m,
            eye_height_m=eye_height_m,
            observer_eye_elevation_m=observer_ground_m + eye_height_m,
            used_terralab_fallback=dem_elevation_m is None,
        )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Obtiene la elevación de una coordenada GPS usando exactamente el "
            "proveedor DEM configurado en TerraLab."
        )
    )
    parser.add_argument("latitude", type=float, help="Latitud WGS84 en grados")
    parser.add_argument("longitude", type=float, help="Longitud WGS84 en grados")
    parser.add_argument(
        "--dem-path",
        "--dem",
        dest="dem_path",
        help="GeoTIFF o directorio DEM; por defecto usa raster_path de TerraLab",
    )
    parser.add_argument(
        "--observer-offset-m",
        type=float,
        default=0.0,
        help="Altura adicional del observador sobre el terreno (default: 0)",
    )
    parser.add_argument(
        "--eye-height-m",
        type=float,
        default=DEFAULT_EYE_HEIGHT_M,
        help="Altura del ojo sobre el observador (default: 1.7)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Falla fuera del DEM en vez de usar el fallback TerraLab de 200 m",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emite sólo JSON, adecuado para scripts",
    )
    parser.add_argument(
        "--precision",
        type=int,
        default=3,
        choices=range(0, 10),
        metavar="0..9",
        help="Decimales de la salida humana (default: 3)",
    )
    return parser


def _print_human(result: ElevationResult, precision: int) -> None:
    number = f"{{:.{int(precision)}f}}"
    print(
        "GPS WGS84: "
        f"{result.latitude_deg:.9f}, {result.longitude_deg:.9f}"
    )
    print(
        f"Coordenadas {result.internal_crs}: "
        f"X={result.projected_x_m:.3f} m, Y={result.projected_y_m:.3f} m"
    )
    print(f"DEM: {result.dem_path}")
    source = f", source_id={result.source_id}" if result.source_id else ""
    print(f"Proveedor: {result.provider}{source}")
    if result.nominal_resolution_m is not None:
        print(f"Resolución nominal: {number.format(result.nominal_resolution_m)} m")
    if result.dem_elevation_m is None:
        print("Elevación DEM: sin cobertura válida")
        print(
            "ADVERTENCIA: TerraLab usaría su fallback de "
            f"{number.format(result.terralab_ground_elevation_m)} m."
        )
    else:
        print(
            "Elevación del terreno TerraLab: "
            f"{number.format(result.terralab_ground_elevation_m)} m s.n.m."
        )
    print(
        "Elevación del observador con offset: "
        f"{number.format(result.observer_ground_elevation_m)} m s.n.m."
    )
    print(
        "Elevación del ojo usada por el raycast: "
        f"{number.format(result.observer_eye_elevation_m)} m s.n.m."
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        # Algunos proveedores antiguos escriben progreso durante initialize().
        # En modo JSON se desvía a stderr para garantizar stdout parseable.
        output_context = (
            contextlib.redirect_stdout(sys.stderr)
            if args.json
            else contextlib.nullcontext()
        )
        with output_context:
            result = get_elevation_by_gps(
                args.latitude,
                args.longitude,
                dem_path=args.dem_path,
                observer_offset_m=args.observer_offset_m,
                eye_height_m=args.eye_height_m,
                allow_terralab_fallback=not args.strict,
            )
    except (FileNotFoundError, LookupError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    except Exception as exc:
        parser.exit(1, f"error al consultar el DEM: {exc}\n")

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human(result, args.precision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
