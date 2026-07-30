"""Physical, versioned resource repositories for the deep-sky render slice."""

# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalMemberAccess=false

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception

try:
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None

try:
    from PyQt5.QtGui import QImage
except ImportError:
    QImage = None

from TerraLab.astro.ngc_catalog import load_ngc_catalog, ngc_display_label
from TerraLab.common.app_paths import ensure_runtime_layout
from TerraLab.common.utils import resource_path
from TerraLab.scene.plans.deep_sky import (
    DeepSkyCatalogHandle,
    TextureResourceHandle,
)


DEEP_SKY_DTYPE = np.dtype(
    [
        ("name", "U80"),
        ("kind", "U16"),
        ("ra", "f4"),
        ("dec", "f4"),
        ("mag", "f4"),
        ("maj", "f4"),
        ("min", "f4"),
        ("pa", "f4"),
    ]
)


def default_milkyway_texture_path() -> str:
    """Return the configured physical default without loading it."""

    try:
        return str(
            Path(ensure_runtime_layout()["data_milkyway"])
            / "milkyway_overlay.png"
        )
    except Exception:
        return "data/sky/milkyway_overlay.png"


def default_dust_map_path() -> str:
    """Return the configured physical dust-map default without loading it."""

    try:
        return str(
            Path(ensure_runtime_layout()["data_planck"])
            / "planck_dust_opacity_eq_u16.npz"
        )
    except Exception:
        return "data/sky/derived/planck_dust_opacity_eq_u16.npz"


def default_ngc_catalog_path() -> str:
    """Locate the bundled OpenNGC CSV without opening it."""

    package_root = Path(__file__).resolve().parents[1]
    primary = package_root / "data" / "sky" / "openngc_catalog.csv"
    if primary.is_file():
        return str(primary)
    return str(package_root / "data" / "openngc_catalog.csv")


def _resolve_path(path: str | None) -> Path | None:
    if not path:
        return None
    candidate = Path(str(path))
    if candidate.is_absolute():
        return candidate
    return Path(resource_path(str(candidate).replace("\\", "/"))).resolve()


def _content_hash(path: Path) -> str:
    """Hash a cold resource load once; warm lookups use the supplied identity."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_image_rgba(path: Path) -> np.ndarray | None:
    if _PILImage is not None:
        try:
            with _PILImage.open(path) as img:
                converted = img.convert("RGBA")
                arr = np.asarray(converted, dtype=np.float32) / 255.0
                return arr.copy()
        except Exception:
            log_suppressed_exception(__name__, "_read_image_rgba")
    if QImage is not None:
        image = QImage(str(path))
        if image.isNull():
            return None
        rgba = image.convertToFormat(QImage.Format_RGBA8888)
        pointer = rgba.bits()
        pointer.setsize(rgba.byteCount())
        array = np.frombuffer(pointer, dtype=np.uint8).reshape(
            rgba.height(), rgba.bytesPerLine() // 4, 4
        )
        return np.asarray(
            array[:, : rgba.width(), :] / 255.0, dtype=np.float32
        ).copy()
    return None


def _normalise_dust(values: np.ndarray) -> np.ndarray | None:
    if values.ndim != 2:
        return None
    if values.dtype == np.uint16:
        return np.asarray(values / 65535.0, dtype=np.float32)
    output = np.asarray(values, dtype=np.float32)
    if not np.isfinite(output).any():
        return None
    low, high = float(np.nanmin(output)), float(np.nanmax(output))
    if high <= low + 1e-12:
        return np.zeros_like(output, dtype=np.float32)
    return np.asarray((output - low) / (high - low), dtype=np.float32)


def _dust_rgba(values: np.ndarray) -> np.ndarray:
    scalar = np.asarray(np.clip(values, 0.0, 1.0), dtype=np.float32)
    return np.stack((scalar, scalar, scalar, np.ones_like(scalar)), axis=-1)


def _catalog_records_from_csv(path: Path) -> np.ndarray:
    items = load_ngc_catalog(path)
    records = np.zeros(len(items), dtype=DEEP_SKY_DTYPE)
    for index, item in enumerate(items):
        records[index] = (
            ngc_display_label(item),
            str(item.obj_type or "G"),
            float(item.ra_deg),
            float(item.dec_deg),
            float(item.effective_mag),
            float(item.maj_deg),
            float(item.min_deg),
            float(item.pos_ang_deg),
        )
    return records


def write_deep_sky_catalog_artifact(
    csv_path: str | Path, artifact_path: str | Path
) -> Path:
    """Convert an OpenNGC CSV into a pickle-free structured `.npy` artifact."""

    destination = Path(artifact_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    np.save(
        destination,
        _catalog_records_from_csv(Path(csv_path)),
        allow_pickle=False,
    )
    return destination


class SkyResourceRepository:
    """Own decoded bytes and mmaps; planners and QPainter receive handles only."""

    def __init__(self) -> None:
        self._texture_handles: dict[
            tuple[str, str, str], TextureResourceHandle
        ] = {}
        self._catalog_handles: dict[tuple[str, str], DeepSkyCatalogHandle] = {}
        self._catalog_mmaps: list[object] = []

    def close(self) -> None:
        for mmap in self._catalog_mmaps:
            close = getattr(mmap, "close", None)
            if callable(close):
                close()
        self._catalog_mmaps.clear()
        self._texture_handles.clear()
        self._catalog_handles.clear()

    def load_texture(
        self, path: str | None, *, version: str = "", dust: bool = False
    ) -> TextureResourceHandle:
        """Decode a PNG or dust map once for a declared path/version identity."""

        requested = path or (
            default_dust_map_path()
            if dust
            else default_milkyway_texture_path()
        )
        resolved = _resolve_path(requested)
        resolved_text = str(resolved) if resolved is not None else ""
        key = ("dust" if dust else "rgba", resolved_text, str(version))
        existing = self._texture_handles.get(key)
        if existing is not None:
            return existing
        if resolved is None or not resolved.is_file():
            handle = TextureResourceHandle(
                resolved_text or None,
                str(version or "missing"),
                "missing",
                None,
                False,
                "missing_resource",
            )
            self._texture_handles[key] = handle
            return handle
        try:
            content_hash = _content_hash(resolved)
            if dust:
                values = self._load_dust(resolved)
                rgba = _dust_rgba(values) if values is not None else None
            else:
                rgba = _read_image_rgba(resolved)
            available = rgba is not None
            handle = TextureResourceHandle(
                str(resolved),
                str(version or content_hash),
                content_hash,
                rgba,
                available,
                "" if available else "invalid_resource",
            )
        except Exception as exc:  # external asset format and decoder boundary
            handle = TextureResourceHandle(
                str(resolved),
                str(version or "invalid"),
                "invalid",
                None,
                False,
                type(exc).__name__,
            )
        self._texture_handles[key] = handle
        return handle

    @staticmethod
    def _load_dust(path: Path) -> np.ndarray | None:
        suffix = path.suffix.lower()
        if suffix == ".npz":
            with np.load(path, allow_pickle=False) as payload:
                for name in (
                    "opacity_u16",
                    "dust_u16",
                    "data_u16",
                    "opacity",
                    "dust",
                    "data",
                ):
                    if name in payload:
                        return _normalise_dust(np.asarray(payload[name]))
                keys = list(payload.keys())
                return (
                    _normalise_dust(np.asarray(payload[keys[0]]))
                    if keys
                    else None
                )
        if suffix == ".npy":
            return _normalise_dust(
                np.asarray(np.load(path, mmap_mode="r", allow_pickle=False))
            )
        if suffix == ".png":
            if _PILImage is not None:
                try:
                    with _PILImage.open(path) as img:
                        gray = img.convert("L")
                        return np.asarray(np.asarray(gray, dtype=np.float32) / 255.0).copy()
                except Exception:
                    log_suppressed_exception(__name__, "_load_dust")
            if QImage is not None:
                image = QImage(str(path)).convertToFormat(QImage.Format_Grayscale8)
                if image.isNull():
                    return None
                pointer = image.bits()
                pointer.setsize(image.byteCount())
                values = np.frombuffer(pointer, dtype=np.uint8).reshape(
                    image.height(), image.bytesPerLine()
                )
                return np.asarray(
                    values[:, : image.width()] / 255.0, dtype=np.float32
                ).copy()
            return None
        if str(path).lower().endswith(".npy.zst"):
            import zstandard as zstd

            with path.open("rb") as source:
                raw = zstd.ZstdDecompressor().decompress(source.read())
            return _normalise_dust(
                np.asarray(np.load(io.BytesIO(raw), allow_pickle=False))
            )
        return None

    def load_catalog(
        self, path: str | None, *, version: str = ""
    ) -> DeepSkyCatalogHandle:
        """Load CSV once or retain a read-only `.npy` catalogue mmap by identity."""

        requested = path or default_ngc_catalog_path()
        resolved = _resolve_path(requested)
        resolved_text = str(resolved) if resolved is not None else ""
        key = (resolved_text, str(version))
        existing = self._catalog_handles.get(key)
        if existing is not None:
            return existing
        if resolved is None or not resolved.is_file():
            handle = DeepSkyCatalogHandle(
                resolved_text or None,
                str(version or "missing"),
                "missing",
                None,
                False,
                "missing_resource",
            )
            self._catalog_handles[key] = handle
            return handle
        try:
            content_hash = _content_hash(resolved)
            if resolved.suffix.lower() == ".csv":
                records = _catalog_records_from_csv(resolved)
            else:
                records = np.load(resolved, mmap_mode="r", allow_pickle=False)
                mmap = getattr(records, "_mmap", None)
                if mmap is not None:
                    self._catalog_mmaps.append(mmap)
            handle = DeepSkyCatalogHandle(
                str(resolved),
                str(version or content_hash),
                content_hash,
                records,
                True,
            )
        except Exception as exc:  # external catalogue format and mmap boundary
            handle = DeepSkyCatalogHandle(
                str(resolved),
                str(version or "invalid"),
                "invalid",
                None,
                False,
                type(exc).__name__,
            )
        self._catalog_handles[key] = handle
        return handle
