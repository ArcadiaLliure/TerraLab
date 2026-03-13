"""Asset manager for per-layer onboarding, downloads and imports."""

from __future__ import annotations

import os
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from TerraLab.common.app_paths import ensure_runtime_layout
from TerraLab.common.utils import get_config_value, set_config_value
from TerraLab.tools.convert_planck_dust import convert_planck_fits_to_cache
from TerraLab.util.gaia_importer import build_gaia_catalog_from_tables
from TerraLab.util.milkyway_importer import convert_milkyway_fits_to_png


ProgressFn = Callable[[float, str], None]


@dataclass(frozen=True)
class AssetSpec:
    asset_id: str
    title: str
    source_url: str
    accepted_formats: str
    credits: str = ""
    allow_multiple: bool = False
    auto_download_url: Optional[str] = None


def _progress(callback: Optional[ProgressFn], percent: float, message: str) -> None:
    if callback is None:
        return
    try:
        callback(float(percent), str(message))
    except Exception:
        pass


class AssetManager:
    def __init__(self) -> None:
        self.layout = ensure_runtime_layout()
        self.specs: Dict[str, AssetSpec] = {
            "climate_metno": AssetSpec(
                asset_id="climate_metno",
                title="Clima MET Norway",
                source_url="https://api.met.no/weatherapi/locationforecast/2.0/compact",
                accepted_formats="Configuracio (User-Agent obligatori)",
                credits="MET Norway (api.met.no) - Weather data under provider terms.",
                allow_multiple=False,
                auto_download_url=None,
            ),
            "gaia_catalog": AssetSpec(
                asset_id="gaia_catalog",
                title="Cataleg Gaia",
                source_url="https://gea.esac.esa.int/archive/",
                accepted_formats="ECSV (.ecsv), CSV (.csv) o ZST (.zst)",
                credits="ESA Gaia Archive / DPAC - Gaia DR3 terms apply.",
                allow_multiple=True,
                auto_download_url=None,
            ),
            "milkyway_texture": AssetSpec(
                asset_id="milkyway_texture",
                title="Via Lactia",
                source_url="https://galaxy.phy.cmich.edu/~axel/mwpan2/mwpan2_RGB_3600.fits",
                accepted_formats="FITS (.fits) o PNG RGBA (.png)",
                credits="Milky Way panorama source by Axel Mellinger (mwpan2).",
                allow_multiple=False,
                auto_download_url="https://galaxy.phy.cmich.edu/~axel/mwpan2/mwpan2_RGB_3600.fits",
            ),
            "planck_dust": AssetSpec(
                asset_id="planck_dust",
                title="Pols Planck",
                source_url="https://irsa.ipac.caltech.edu/data/Planck/release_2/all-sky-maps/maps/component-maps/foregrounds/COM_CompMap_Dust-GNILC-Model-Opacity_2048_R2.01.fits",
                accepted_formats="FITS (.fits)",
                credits="ESA / Planck Collaboration / Planck Legacy Archive.",
                allow_multiple=False,
                auto_download_url="https://irsa.ipac.caltech.edu/data/Planck/release_2/all-sky-maps/maps/component-maps/foregrounds/COM_CompMap_Dust-GNILC-Model-Opacity_2048_R2.01.fits",
            ),
            "elevation_dem": AssetSpec(
                asset_id="elevation_dem",
                title="Elevacions",
                source_url="https://gisco-services.ec.europa.eu/dem/5degree/mosaic/EU_DEM_mosaic_5deg.ZIP",
                accepted_formats="GeoTIFF (.tif/.tiff) o TXT/ASC (.txt/.asc), tambe ZIP",
                credits="EU-DEM (Copernicus/EEA via GISCO services).",
                allow_multiple=True,
                auto_download_url="https://gisco-services.ec.europa.eu/dem/5degree/mosaic/EU_DEM_mosaic_5deg.ZIP",
            ),
            "light_pollution": AssetSpec(
                asset_id="light_pollution",
                title="Contaminacio luminica",
                source_url="https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/15IKI5",
                accepted_formats="GeoTIFF (.tif/.tiff)",
                credits="VIIRS/DMSP composite via Harvard Dataverse DOI:10.7910/DVN/15IKI5.",
                allow_multiple=False,
                auto_download_url=None,
            ),
            "ngc_catalog": AssetSpec(
                asset_id="ngc_catalog",
                title="Cataleg NGC",
                source_url="https://github.com/mattiaverga/OpenNGC/blob/master/database_files/NGC.csv",
                accepted_formats="CSV (.csv)",
                credits="OpenNGC catalogue by Mattia Verga and contributors.",
                allow_multiple=False,
                auto_download_url="https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv",
            ),
        }

    def _mark_asset_state(self, asset_id: str, ready: bool, path: str = "") -> None:
        now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        set_config_value(f"assets.{asset_id}.ready", bool(ready))
        set_config_value(f"assets.{asset_id}.path", str(path or ""))
        set_config_value(f"assets.{asset_id}.updated_utc", now_utc)

    def get_specs(self) -> List[AssetSpec]:
        return list(self.specs.values())

    def get_spec(self, asset_id: str) -> AssetSpec:
        if asset_id not in self.specs:
            raise KeyError(f"Unknown asset id: {asset_id}")
        return self.specs[asset_id]

    def onboarding_asset_order(self) -> List[str]:
        return [
            "climate_metno",
            "gaia_catalog",
            "milkyway_texture",
            "planck_dust",
            "elevation_dem",
            "light_pollution",
            "ngc_catalog",
        ]

    def get_user_agent(self) -> str:
        return str(get_config_value("weather.metno_user_agent", "") or "").strip()

    def set_user_agent(self, value: str) -> None:
        ua = str(value or "").strip()
        set_config_value("weather.metno_user_agent", ua)
        self._mark_asset_state("climate_metno", bool(ua), "")

    def asset_ready(self, asset_id: str) -> bool:
        return bool(self.asset_status(asset_id).get("ready", False))

    def asset_status(self, asset_id: str) -> Dict[str, object]:
        layout = self.layout
        if asset_id == "climate_metno":
            ua = self.get_user_agent()
            return {
                "ready": bool(ua),
                "reason": "ok" if ua else "missing_user_agent",
                "path": "",
            }
        if asset_id == "gaia_catalog":
            p_npz = Path(layout["data_gaia"]) / "stars_catalog.npz"
            p_zst = Path(layout["data_gaia"]) / "stars_catalog.zst"
            p_npy = Path(layout["data_gaia"]) / "stars_catalog.npy"
            for candidate in (p_npz, p_zst, p_npy):
                if candidate.exists():
                    return {"ready": True, "reason": "ok", "path": str(candidate)}
            packaged_dir = Path(__file__).resolve().parents[1] / "data" / "stars"
            packaged_candidates = (
                packaged_dir / "stars_catalog.zst",
                packaged_dir / "stars_catalog.npz",
                packaged_dir / "stars_catalog.npy",
            )
            for candidate in packaged_candidates:
                if candidate.exists():
                    return {"ready": True, "reason": "packaged_catalog", "path": str(candidate)}
            return {"ready": False, "reason": "missing_catalog", "path": str(p_npz)}
        if asset_id == "milkyway_texture":
            p = Path(layout["data_milkyway"]) / "milkyway_overlay.png"
            exists = p.exists()
            return {"ready": exists, "reason": "ok" if exists else "missing_texture", "path": str(p)}
        if asset_id == "planck_dust":
            p = Path(layout["data_planck"]) / "planck_dust_opacity_eq_u16.npz"
            exists = p.exists()
            return {"ready": exists, "reason": "ok" if exists else "missing_dust_map", "path": str(p)}
        if asset_id == "elevation_dem":
            folder = Path(layout["data_elevation"])
            files = [*folder.glob("*.tif"), *folder.glob("*.tiff"), *folder.glob("*.txt"), *folder.glob("*.asc")]
            has_files = bool(files)
            return {"ready": has_files, "reason": "ok" if has_files else "missing_dem", "path": str(folder)}
        if asset_id == "light_pollution":
            p = Path(layout["data_light_pollution"]) / "light_pollution.tif"
            exists = p.exists()
            return {"ready": exists, "reason": "ok" if exists else "missing_raster", "path": str(p)}
        if asset_id == "ngc_catalog":
            p = Path(layout["data_ngc"]) / "openngc_catalog.csv"
            exists = p.exists()
            return {"ready": exists, "reason": "ok" if exists else "missing_catalog", "path": str(p)}
        return {"ready": False, "reason": "unknown_asset", "path": ""}

    @staticmethod
    def _copy_file(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    def _download_file(self, url: str, target_path: Path, progress_callback: Optional[ProgressFn] = None) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _progress(progress_callback, 2.0, f"Descarregant {url}")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "TerraLab/1.0 (onboarding downloader)",
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as response, target_path.open("wb") as out:
            total = int(response.headers.get("Content-Length", "0") or "0")
            downloaded = 0
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = 2.0 + 78.0 * (float(downloaded) / float(total))
                    _progress(progress_callback, pct, f"Descarregat {downloaded}/{total} bytes")
                else:
                    pct = min(80.0, 2.0 + (downloaded / (1024.0 * 1024.0)) * 2.5)
                    _progress(progress_callback, pct, f"Descarregat {downloaded} bytes")
        _progress(progress_callback, 82.0, "Descarrega completada.")
        return target_path

    def download_and_prepare(
        self,
        asset_id: str,
        progress_callback: Optional[ProgressFn] = None,
        options: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        spec = self.get_spec(asset_id)
        if not spec.auto_download_url:
            raise ValueError(f"Asset {asset_id} does not support automatic download.")
        tmp_dir = Path(self.layout["tmp"])
        ext = Path(spec.auto_download_url).suffix or ".bin"
        tmp_path = tmp_dir / f"{asset_id}_download{ext}"
        downloaded = self._download_file(spec.auto_download_url, tmp_path, progress_callback=progress_callback)
        return self.import_files(
            asset_id,
            [str(downloaded)],
            progress_callback=progress_callback,
            options=options,
        )

    def import_files(
        self,
        asset_id: str,
        files: Iterable[str],
        progress_callback: Optional[ProgressFn] = None,
        options: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        opts = dict(options or {})
        paths = [Path(p) for p in files if str(p).strip()]
        if asset_id == "climate_metno":
            ua = self.get_user_agent()
            self._mark_asset_state("climate_metno", bool(ua), "")
            if not ua:
                raise ValueError("Cal definir un User-Agent per MET Norway abans d'activar el clima.")
            return {"ok": True, "message": "User-Agent configurat."}

        if not paths:
            raise ValueError("No s'han seleccionat fitxers.")

        if asset_id == "gaia_catalog":
            out_dir = Path(self.layout["data_gaia"])
            summary = build_gaia_catalog_from_tables(
                [str(p) for p in paths],
                str(out_dir),
                output_basename="stars_catalog",
                write_npz=True,
                write_npy=False,
                write_zst=False,
                progress_callback=progress_callback,
            )
            no_gaia_src = Path(__file__).resolve().parents[1] / "data" / "stars" / "no_gaia_stars.json"
            no_gaia_dst = out_dir / "no_gaia_stars.json"
            try:
                if (not no_gaia_dst.exists()) and no_gaia_src.exists():
                    self._copy_file(no_gaia_src, no_gaia_dst)
            except Exception:
                pass
            npz_path = out_dir / "stars_catalog.npz"
            npy_path = out_dir / "stars_catalog.npy"
            zst_path = out_dir / "stars_catalog.zst"
            selected_path = npz_path if npz_path.exists() else (npy_path if npy_path.exists() else zst_path)
            set_config_value("gaia_catalog_path", str(selected_path))
            self._mark_asset_state("gaia_catalog", True, str(selected_path))
            return {"ok": True, "summary": summary, "stored_in": str(out_dir)}

        if asset_id == "milkyway_texture":
            out_dir = Path(self.layout["data_milkyway"])
            dst_png = out_dir / "milkyway_overlay.png"
            src = paths[0]
            _progress(progress_callback, 5.0, "Processant Via Lactia...")
            remove_stars = bool(opts.get("remove_stars", get_config_value("milkyway_starless_enabled", True)))
            png_compress_level = int(get_config_value("milkyway_png_compress_level", 0))
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
            set_config_value("milkyway_overlay_texture_path", str(dst_png))
            set_config_value("milkyway_starless_enabled", bool(remove_stars))
            set_config_value("milkyway_png_compress_level", int(max(0, min(9, png_compress_level))))
            self._mark_asset_state("milkyway_texture", True, str(dst_png))
            return {"ok": True, "summary": summary, "stored_in": str(out_dir)}

        if asset_id == "planck_dust":
            out_dir = Path(self.layout["data_planck"])
            out_dir.mkdir(parents=True, exist_ok=True)
            src = paths[0]
            fits_path = out_dir / "COM_CompMap_Dust-GNILC-Model-Opacity_2048_R2.01.fits"
            self._copy_file(src, fits_path)
            _progress(progress_callback, 10.0, "Convertint FITS Planck a cache runtime...")
            summary = convert_planck_fits_to_cache(
                fits_path=str(fits_path),
                output_npz=str(out_dir / "planck_dust_opacity_eq_u16.npz"),
                output_zst=str(out_dir / "planck_dust_opacity_eq_u16.npy.zst"),
                write_zst=True,
                workers=max(1, (os.cpu_count() or 2) - 1),
            )
            _progress(progress_callback, 100.0, "Planck convertit.")
            dust_path = out_dir / "planck_dust_opacity_eq_u16.npz"
            set_config_value("dust_map_path", str(dust_path))
            self._mark_asset_state("planck_dust", True, str(dust_path))
            return {"ok": True, "summary": summary, "stored_in": str(out_dir)}

        if asset_id == "elevation_dem":
            out_dir = Path(self.layout["data_elevation"])
            out_dir.mkdir(parents=True, exist_ok=True)
            copied = 0
            total = max(1, len(paths))
            for idx, src in enumerate(paths):
                ext = src.suffix.lower()
                if ext == ".zip":
                    _progress(progress_callback, 5.0 + (80.0 * idx / total), f"Descomprimint {src.name}...")
                    with zipfile.ZipFile(src, "r") as zf:
                        zf.extractall(out_dir)
                else:
                    self._copy_file(src, out_dir / src.name)
                copied += 1
                _progress(progress_callback, 5.0 + (90.0 * (idx + 1) / total), f"Importat {src.name}")
            set_config_value("raster_path", str(out_dir))
            self._mark_asset_state("elevation_dem", True, str(out_dir))
            return {"ok": True, "stored_in": str(out_dir), "files_processed": int(copied)}

        if asset_id == "light_pollution":
            out_dir = Path(self.layout["data_light_pollution"])
            dst = out_dir / "light_pollution.tif"
            self._copy_file(paths[0], dst)
            _progress(progress_callback, 100.0, "Raster de contaminacio luminica importat.")
            set_config_value("dvnl_path", str(dst))
            self._mark_asset_state("light_pollution", True, str(dst))
            return {"ok": True, "stored_in": str(dst)}

        if asset_id == "ngc_catalog":
            out_dir = Path(self.layout["data_ngc"])
            dst = out_dir / "openngc_catalog.csv"
            self._copy_file(paths[0], dst)
            _progress(progress_callback, 100.0, "Cataleg NGC importat.")
            set_config_value("ngc_catalog_path", str(dst))
            self._mark_asset_state("ngc_catalog", True, str(dst))
            return {"ok": True, "stored_in": str(dst)}

        raise KeyError(f"Unknown asset id: {asset_id}")
