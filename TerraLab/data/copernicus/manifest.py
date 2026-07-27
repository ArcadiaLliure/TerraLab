"""Atomic resumable-download manifest for one Copernicus plan."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .constants import (
    DOWNLOAD_PIPELINE_VERSION,
    IMAGE_SERVER_URL,
    PRODUCT_NAME,
    SERVICE_PIXEL_TYPE,
)
from .planning import DownloadPlan, Fragment
from .validation import (
    CopernicusDownloadError,
    ManifestMismatchError,
    RasterValidation,
    _sha256_file,
    read_mosaic_radiometric_metadata,
    validate_final_geotiff,
    validate_fragment_raster,
)


MANIFEST_SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _plan_identity(plan: DownloadPlan) -> str:
    payload = {
        "pipeline_version": DOWNLOAD_PIPELINE_VERSION,
        "request": plan.request.to_dict(),
        "grid": {
            "bounds": plan.grid.bounds.to_dict(),
            "width_px": plan.grid.width_px,
            "height_px": plan.grid.height_px,
            "resolution_m": plan.grid.resolution_m,
        },
        "fragments": [
            {
                "id": fragment.id,
                "row_offset": fragment.row_offset,
                "column_offset": fragment.column_offset,
                "width_px": fragment.width_px,
                "height_px": fragment.height_px,
                "bounds": fragment.bounds_3035.to_dict(),
            }
            for fragment in plan.fragments
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DownloadManifest:
    """Atomic fragment-level state that survives application restarts."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        plan: DownloadPlan,
    ) -> None:
        self.path = Path(path).resolve(strict=False)
        self.plan = plan
        self.identity = _plan_identity(plan)
        self._lock = threading.RLock()
        self._payload: dict[str, object]
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CopernicusDownloadError(
                    f"Manifest de fragments invàlid: {self.path}"
                ) from exc
            if not isinstance(loaded, dict):
                raise CopernicusDownloadError("Manifest de fragments invàlid.")
            if str(loaded.get("plan_identity", "")) != self.identity:
                raise ManifestMismatchError(
                    "El manifest existent correspon a una altra selecció."
                )
            self._payload = loaded
        else:
            self._payload = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "pipeline_version": DOWNLOAD_PIPELINE_VERSION,
                "plan_identity": self.identity,
                "product": PRODUCT_NAME,
                "service_url": IMAGE_SERVER_URL,
                "transport_pixel_type": SERVICE_PIXEL_TYPE,
                "output_pixel_type": plan.request.pixel_type,
                "request": plan.request.to_dict(),
                "estimate": plan.estimate.to_dict(),
                "created_utc": _utc_now(),
                "updated_utc": _utc_now(),
                "fragments": {},
            }
            self._save_locked()

    def _records_locked(self) -> dict[str, object]:
        records = self._payload.setdefault("fragments", {})
        if not isinstance(records, dict):
            records = {}
            self._payload["fragments"] = records
        return records

    def _save_locked(self) -> None:
        self._payload["updated_utc"] = _utc_now()
        _atomic_json(self.path, self._payload)

    def fragment_path(self, fragment: Fragment, fragment_root: Path) -> Path:
        return fragment_root / f"{fragment.id}.tif"

    def is_complete(
        self,
        fragment: Fragment,
        fragment_root: str | os.PathLike[str],
    ) -> bool:
        root = Path(fragment_root)
        path = self.fragment_path(fragment, root)
        with self._lock:
            record = self._records_locked().get(fragment.id)
            if not isinstance(record, Mapping):
                return False
            if str(record.get("status", "")) != "complete":
                return False
            try:
                if int(record.get("size_bytes", -1)) != int(path.stat().st_size):
                    return False
            except OSError:
                return False
            expected_sha256 = str(record.get("sha256", "") or "")
        if expected_sha256:
            try:
                if _sha256_file(path) != expected_sha256:
                    return False
            except OSError:
                return False
        try:
            validate_fragment_raster(path, fragment, self.plan.request)
            return True
        except Exception:
            return False

    def mark_complete(self, fragment: Fragment, path: Path) -> None:
        with self._lock:
            self._records_locked()[fragment.id] = {
                "status": "complete",
                "path": str(path),
                "size_bytes": int(path.stat().st_size),
                "sha256": _sha256_file(path),
                "completed_utc": _utc_now(),
                "row_offset": fragment.row_offset,
                "column_offset": fragment.column_offset,
                "width_px": fragment.width_px,
                "height_px": fragment.height_px,
                "pixel_type": SERVICE_PIXEL_TYPE,
                "bounds_3035": fragment.bounds_3035.to_dict(),
            }
            self._save_locked()

    def adopt_existing(
        self,
        fragment: Fragment,
        fragment_root: str | os.PathLike[str],
    ) -> bool:
        """Durably adopt an unrecorded complete TIFF left by an interrupted run.

        ``download_fragment`` publishes a fragment with an atomic rename.  A
        sibling future can still fail after that rename but before the manager
        processes the successful future and records it.  Only an absent or
        incomplete manifest record is eligible for adoption: a recorded file
        whose size or digest changed must be downloaded again.
        """

        root = Path(fragment_root)
        path = self.fragment_path(fragment, root)
        with self._lock:
            record = self._records_locked().get(fragment.id)
            if (
                isinstance(record, Mapping)
                and str(record.get("status", "")) == "complete"
            ):
                return False
        if not path.is_file():
            return False
        try:
            validate_fragment_raster(path, fragment, self.plan.request)
        except Exception:
            return False
        self.mark_complete(fragment, path)
        return True

    def mark_final(
        self,
        path: Path,
        validation: RasterValidation,
    ) -> None:
        with self._lock:
            self._payload["status"] = "complete"
            self._payload["output"] = {
                "path": str(path),
                "size_bytes": int(path.stat().st_size),
                "sha256": _sha256_file(path),
                "width_px": validation.width_px,
                "height_px": validation.height_px,
                "band_count": validation.band_count,
                "pixel_type": validation.pixel_type,
                "source_pixel_type": SERVICE_PIXEL_TYPE,
                "radiometric_conversion": read_mosaic_radiometric_metadata(
                    path
                ),
                "crs": validation.crs,
                "bounds_3035": validation.bounds.to_dict(),
                "tiled": validation.tiled,
                "nodata": validation.nodata,
                "overviews": list(validation.overviews),
                "completed_utc": _utc_now(),
            }
            self._save_locked()

    def validated_final(
        self,
        expected_path: str | os.PathLike[str],
    ) -> RasterValidation | None:
        """Return a verified completed mosaic, or ``None`` if it is stale."""

        path = Path(expected_path).resolve(strict=False)
        with self._lock:
            if str(self._payload.get("status", "")) != "complete":
                return None
            record = self._payload.get("output")
            if not isinstance(record, Mapping):
                return None
            recorded_path = Path(str(record.get("path", ""))).resolve(
                strict=False
            )
            if recorded_path != path:
                return None
            try:
                if int(record.get("size_bytes", -1)) != int(
                    path.stat().st_size
                ):
                    return None
            except OSError:
                return None
            expected_sha256 = str(record.get("sha256", "") or "")
        if not expected_sha256:
            return None
        try:
            if _sha256_file(path) != expected_sha256:
                return None
            return validate_final_geotiff(path, self.plan)
        except Exception:
            return None

    def adopt_final(
        self,
        expected_path: str | os.PathLike[str],
    ) -> RasterValidation | None:
        """Adopt a valid mosaic published just before an interrupted manifest write."""

        path = Path(expected_path).resolve(strict=False)
        with self._lock:
            # A recorded output that no longer matches its size/digest is
            # stale or corrupted, never an adoption candidate.
            if str(self._payload.get("status", "")) == "complete" or isinstance(
                self._payload.get("output"), Mapping
            ):
                return None
        if not path.is_file():
            return None
        try:
            validation = validate_final_geotiff(path, self.plan)
        except Exception:
            return None
        self.mark_final(path, validation)
        return validation

    @property
    def completed_count(self) -> int:
        with self._lock:
            return sum(
                isinstance(record, Mapping)
                and str(record.get("status", "")) == "complete"
                for record in self._records_locked().values()
            )
