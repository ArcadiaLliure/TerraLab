"""Explicit installation and import of user-provided assets."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.utils import get_config_value
from TerraLab.data.assets.discovery import AssetDiscoveryMixin
from TerraLab.data.assets.runtime import (
    AssetOperationCancelled,
    ProgressFn,
    _progress,
    build_gaia_catalog_spawned,
    convert_milkyway_fits_to_png,
    convert_planck_fits_to_cache,
    set_asset_config,
)
from TerraLab.data.asc_cache_builder import materialize_asc_caches_spawned
from TerraLab.data.source_catalog import LayerType, SourceHealthStatus
from TerraLab.data.source_inspection import inspect_data_source


class AssetInstallMixin:
    def import_files(
        self,
        asset_id: str,
        files: Iterable[str],
        progress_callback: Optional[ProgressFn] = None,
        options: Optional[Dict[str, object]] = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Dict[str, object]:
        opts = dict(options or {})
        paths = [Path(p) for p in files if str(p).strip()]
        if asset_id == "climate_metno":
            ua = self.get_user_agent()
            self._mark_asset_state("climate_metno", bool(ua), "")
            if not ua:
                raise ValueError(
                    "Cal definir un User-Agent per MET Norway abans d'activar el clima."
                )
            return {"ok": True, "message": "User-Agent configurat."}

        if not paths:
            raise ValueError("No s'han seleccionat fitxers.")

        if asset_id == "gaia_catalog":
            out_dir = Path(self.layout["data_gaia"])
            build_healpy_index = bool(opts.get("build_healpy_index", True))
            healpy_nside = int(opts.get("healpy_nside", 512) or 512)
            healpy_chunk_rows = int(
                opts.get("healpy_chunk_rows", 1_000_000) or 1_000_000
            )
            summary = build_gaia_catalog_spawned(
                [str(p) for p in paths],
                str(out_dir),
                output_basename="stars_catalog",
                write_npz=False,
                write_npy=True,
                write_zst=False,
                build_healpy_index=build_healpy_index,
                healpy_nside=healpy_nside,
                healpy_chunk_rows=healpy_chunk_rows,
                progress_callback=progress_callback,
            )
            no_gaia_src = (
                Path(__file__).resolve().parents[2]
                / "data"
                / "stars"
                / "no_gaia_stars.json"
            )
            no_gaia_dst = out_dir / "no_gaia_stars.json"
            try:
                if (not no_gaia_dst.exists()) and no_gaia_src.exists():
                    self._copy_file(no_gaia_src, no_gaia_dst)
            except Exception:
                log_suppressed_exception(__name__, "AssetInstallMixin.import_files")
            npz_path = out_dir / "stars_catalog.npz"
            npy_path = out_dir / "stars_catalog.npy"
            zst_path = out_dir / "stars_catalog.zst"
            selected_path = (
                npy_path
                if npy_path.exists()
                else (npz_path if npz_path.exists() else zst_path)
            )
            set_asset_config("gaia_catalog_path", str(selected_path))
            self._mark_asset_state("gaia_catalog", True, str(selected_path))
            return {"ok": True, "summary": summary, "stored_in": str(out_dir)}

        if asset_id == "milkyway_texture":
            out_dir = Path(self.layout["data_milkyway"])
            dst_png = out_dir / "milkyway_overlay.png"
            src = paths[0]
            _progress(progress_callback, 5.0, "Processant Via Lactia...")
            remove_stars = bool(
                opts.get(
                    "remove_stars",
                    get_config_value("milkyway_starless_enabled", True),
                )
            )
            png_compress_level = int(
                get_config_value("milkyway_png_compress_level", 0)
            )
            if src.suffix.lower() == ".fits":
                summary = convert_milkyway_fits_to_png(
                    str(src),
                    str(dst_png),
                    remove_stars=remove_stars,
                    png_compress_level=png_compress_level,
                    progress_callback=progress_callback,
                )
            else:
                self._copy_file(src, dst_png)
                summary = {"output_png": str(dst_png)}
                _progress(progress_callback, 100.0, "Via Lactia importada.")
            set_asset_config("milkyway_overlay_texture_path", str(dst_png))
            set_asset_config("milkyway_starless_enabled", bool(remove_stars))
            set_asset_config(
                "milkyway_png_compress_level",
                int(max(0, min(9, png_compress_level))),
            )
            self._mark_asset_state("milkyway_texture", True, str(dst_png))
            return {"ok": True, "summary": summary, "stored_in": str(out_dir)}

        if asset_id == "planck_dust":
            out_dir = Path(self.layout["data_planck"])
            out_dir.mkdir(parents=True, exist_ok=True)
            src = paths[0]
            _progress(
                progress_callback,
                10.0,
                "Convertint FITS Planck a cache runtime...",
            )
            summary = convert_planck_fits_to_cache(
                fits_path=str(src),
                output_npz=str(out_dir / "planck_dust_opacity_eq_u16.npz"),
                output_zst=str(out_dir / "planck_dust_opacity_eq_u16.npy.zst"),
                write_zst=True,
                workers=max(1, (os.cpu_count() or 2) - 1),
            )
            _progress(progress_callback, 100.0, "Planck convertit.")
            dust_path = out_dir / "planck_dust_opacity_eq_u16.npz"
            set_asset_config("dust_map_path", str(dust_path))
            self._mark_asset_state("planck_dust", True, str(dust_path))
            return {"ok": True, "summary": summary, "stored_in": str(out_dir)}

        layer_type = self._asset_layer_type(asset_id)
        if layer_type is not None:
            parent_key = {
                LayerType.ELEVATION: "data_elevation",
                LayerType.ORTHOPHOTO_RGB: "data_surface",
                LayerType.SURFACE_RGB: "data_surface",
                LayerType.SURFACE_CATEGORICAL: "data_surface",
                LayerType.LIGHT_POLLUTION: "data_light_pollution",
            }[layer_type]
            fallback_parent = (
                Path(self.layout["root"])
                / "data"
                / "earth"
                / (
                    "surface"
                    if layer_type
                    in {
                        LayerType.ORTHOPHOTO_RGB,
                        LayerType.SURFACE_RGB,
                        LayerType.SURFACE_CATEGORICAL,
                    }
                    else layer_type.value.replace("_", "-")
                )
            )
            parent = Path(self.layout.get(parent_key, fallback_parent))
            parent.mkdir(parents=True, exist_ok=True)
            display_name = str(opts.get("display_name") or paths[0].stem)
            dataset_dir = self._unique_dataset_path(
                parent,
                display_name,
                layer_type.value,
            )
            staging_dir = Path(
                tempfile.mkdtemp(
                    prefix=f".{self._dataset_slug(display_name, layer_type.value)}-",
                    dir=str(parent),
                )
            ).resolve(strict=False)
            copied = 0
            try:
                total = max(1, len(paths))
                for idx, src in enumerate(paths):
                    if cancelled is not None and cancelled():
                        raise AssetOperationCancelled(
                            "Importació pausada abans de copiar."
                        )
                    if not src.exists():
                        raise FileNotFoundError(str(src))
                    _progress(
                        progress_callback,
                        5.0 + 75.0 * idx / total,
                        f"Important {src.name}...",
                    )
                    if src.is_dir():
                        for child in src.rglob("*"):
                            if cancelled is not None and cancelled():
                                raise AssetOperationCancelled(
                                    "Importació pausada durant la còpia."
                                )
                            if child.is_file():
                                destination = staging_dir / child.relative_to(src)
                                self._copy_file(child, destination)
                    elif src.suffix.lower() == ".zip":
                        self._mark_install_state(asset_id, "extracting")
                        self._validate_and_extract_zip(
                            src,
                            staging_dir,
                            progress_callback=progress_callback,
                            cancelled=cancelled,
                        )
                    elif src.suffix.lower() == ".7z":
                        self._safe_extract_7z(src, staging_dir)
                    elif src.suffix.lower() in {".tif", ".tiff"}:
                        self._copy_raster_with_sidecars(
                            src, staging_dir / src.name
                        )
                    else:
                        self._copy_file(src, staging_dir / src.name)
                    copied += 1

                raster_candidates = sorted(
                    (
                        child
                        for child in staging_dir.rglob("*")
                        if child.is_file()
                        and child.suffix.lower()
                        in {".tif", ".tiff", ".asc", ".txt", ".npy", ".vrt", ".img", ".jp2"}
                    ),
                    key=lambda child: child.name.lower(),
                )
                if not raster_candidates:
                    raise ValueError(
                        "No s'ha trobat cap raster compatible després d'extreure o copiar."
                    )
                staging_registered_path: Path = staging_dir
                if layer_type is LayerType.LIGHT_POLLUTION:
                    tiffs = [
                        child
                        for child in raster_candidates
                        if child.suffix.lower() in {".tif", ".tiff"}
                    ]
                    if not tiffs:
                        raise ValueError("L'arxiu no conté cap GeoTIFF DVNL.")
                    staging_registered_path = tiffs[-1]

                metadata = self._geospatial_metadata(asset_id, managed=True)
                source_attribution = (
                    "Font d'ortofoto aportada per l'usuari."
                    if asset_id == "orthophoto"
                    else self.get_spec(asset_id).credits
                )
                source_license = (
                    ""
                    if asset_id == "orthophoto"
                    else self.get_spec(asset_id).license_note
                )
                self._mark_install_state(asset_id, "registering")
                _progress(
                    progress_callback,
                    94.0,
                    "Indexant i validant bandes, dtype, CRS, transformació, "
                    "límits, resolució i nodata…",
                )
                registered_relative = staging_registered_path.relative_to(
                    staging_dir
                )
                native_register = (
                    getattr(self._register_data_source, "__func__", None)
                    is AssetDiscoveryMixin._register_data_source
                )
                if native_register:
                    try:
                        inspected = inspect_data_source(
                            str(staging_registered_path),
                            layer_type,
                            source_id=f"{asset_id}-validation",
                            metadata=metadata,
                        )
                    except Exception:
                        # Keep an invalid linked/imported source diagnosable in
                        # the catalogue. It remains disabled and is never
                        # considered prepared by _register_data_source.
                        os.replace(staging_dir, dataset_dir)
                        staging_dir = None
                        invalid_path = (
                            dataset_dir / registered_relative
                            if layer_type is LayerType.LIGHT_POLLUTION
                            else dataset_dir
                        )
                        self._register_data_source(
                            invalid_path,
                            layer_type,
                            display_name=display_name,
                            priority=int(opts.get("priority", 0) or 0),
                            provenance="managed",
                            attribution=source_attribution,
                            license=source_license,
                            metadata=metadata,
                        )
                        raise
                    metadata = dict(inspected.metadata)
                    metadata["inspection_status"] = (
                        SourceHealthStatus.HEALTHY.value
                    )
                # Publish the fully validated tree in one filesystem operation.
                os.replace(staging_dir, dataset_dir)
                staging_dir = None
                registered_path: Path = (
                    dataset_dir / registered_relative
                    if layer_type is LayerType.LIGHT_POLLUTION
                    else dataset_dir
                )
                source = self._register_data_source(
                    registered_path,
                    layer_type,
                    display_name=display_name,
                    priority=int(opts.get("priority", 0) or 0),
                    provenance="managed",
                    attribution=source_attribution,
                    license=source_license,
                    metadata=metadata,
                )

                asc_caches = []
                observer_auto = {"applied": False}
                if layer_type is LayerType.ELEVATION:
                    _progress(progress_callback, 88.0, "Materialitzant caches ASC...")
                    asc_caches = materialize_asc_caches_spawned(
                        dataset_dir,
                        max_workers=4,
                        progress_callback=(
                            lambda percent, message: _progress(
                                progress_callback,
                                88.0 + float(percent) * 0.06,
                                message,
                            )
                        ),
                    )
                    observer_auto = self._auto_configure_observer_from_dem(dataset_dir)
                self._mark_asset_state(asset_id, True, "")
                self._mark_install_state(asset_id, "prepared")
                _progress(
                    progress_callback,
                    100.0,
                    "Completat: font registrada a la biblioteca.",
                )
                return {
                    "ok": True,
                    "stored_in": str(
                        registered_path
                        if layer_type is LayerType.LIGHT_POLLUTION
                        else dataset_dir
                    ),
                    "source_id": str(source.id),
                    "files_processed": int(copied),
                    "asc_caches": int(len(asc_caches)),
                    "observer_auto": observer_auto,
                }
            except Exception as exc:
                if staging_dir is not None and staging_dir.exists():
                    try:
                        resolved_parent = parent.resolve(strict=False)
                        resolved_staging = staging_dir.resolve(strict=False)
                        resolved_staging.relative_to(resolved_parent)
                        if resolved_staging.name.startswith("."):
                            shutil.rmtree(resolved_staging)
                    except (OSError, ValueError):
                        pass
                if isinstance(exc, AssetOperationCancelled):
                    self._mark_install_state(asset_id, "paused")
                else:
                    self._mark_asset_state(asset_id, False, "")
                    self._mark_install_state(asset_id, "error", error=str(exc))
                raise

        if asset_id == "ngc_catalog":
            out_dir = Path(self.layout["data_ngc"])
            dst = out_dir / "openngc_catalog.csv"
            self._copy_file(paths[0], dst)
            _progress(progress_callback, 100.0, "Cataleg NGC importat.")
            set_asset_config("ngc_catalog_path", str(dst))
            self._mark_asset_state("ngc_catalog", True, str(dst))
            self.library.update_asset(
                "ngc_catalog",
                managed=True,
                source_path=str(paths[0].resolve(strict=False)),
                checksum_sha256=self._sha256(dst),
                size_bytes=int(dst.stat().st_size),
                attribution="OpenNGC by Mattia Verga and contributors",
                license="CC-BY-SA-4.0",
            )
            return {"ok": True, "stored_in": str(dst)}

        if asset_id == "solar_system_ephemeris":
            src = paths[0]
            if src.suffix.lower() != ".bsp":
                raise ValueError("L'efemèride del sistema solar ha de ser un fitxer .bsp.")
            out_dir = Path(self.layout["data_ephemeris"])
            dst = out_dir / "de421.bsp"
            self._copy_file(src, dst)
            _progress(progress_callback, 100.0, "Efemèride DE421 importada.")
            self._mark_asset_state(asset_id, True, str(dst))
            return {"ok": True, "stored_in": str(dst)}

        raise KeyError(f"Unknown asset id: {asset_id}")

