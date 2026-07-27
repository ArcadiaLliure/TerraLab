"""Reusable streaming HTTP downloads with durable resume metadata.

The data-layer manager owns this service.  It has no Qt dependency: GUI
workers pass a cooperative cancellation predicate and receive structured
progress while all network and filesystem work remains off the UI thread.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import ssl
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

import requests
import truststore
from requests.adapters import HTTPAdapter


DOWNLOAD_METADATA_SCHEMA_VERSION = 1
_CONTENT_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.I)


class _NativeTrustHTTPAdapter(HTTPAdapter):
    """Use the OS trust engine without weakening TLS verification.

    The S2GLC host does not currently send its intermediate certificate.
    Native trust engines can retrieve that missing intermediate, while the
    static CA bundle used by Requests cannot build the certificate chain.
    """

    def __init__(self, ssl_context: ssl.SSLContext, *args, **kwargs) -> None:
        self._ssl_context = ssl_context
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs) -> None:
        kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def _native_trust_session() -> requests.Session:
    session = requests.Session()
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    session.mount("https://", _NativeTrustHTTPAdapter(context))
    return session


class DownloadCancelled(RuntimeError):
    """Cooperative pause/cancel; a valid partial file is retained."""


class InsufficientSpaceError(OSError):
    """The destination cannot hold the archive, extraction and safety margin."""

    def __init__(self, required: int, available: int) -> None:
        self.required = max(0, int(required))
        self.available = max(0, int(available))
        super().__init__(
            "Espai insuficient: calen "
            f"{self.required} bytes lliures i només n'hi ha "
            f"{self.available}."
        )


@dataclass(frozen=True)
class DownloadProgress:
    phase: str
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_bytes_s: float = 0.0
    eta_seconds: float | None = None
    detail: str = ""

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return -1.0
        return max(
            0.0,
            min(100.0, 100.0 * self.downloaded_bytes / self.total_bytes),
        )


@dataclass(frozen=True)
class PartialDownload:
    url: str
    target_path: str
    partial_path: str
    metadata_path: str
    downloaded_bytes: int
    expected_size: int
    etag: str = ""
    last_modified: str = ""
    status: str = "partial"
    updated_utc: str = ""

    @property
    def resumable(self) -> bool:
        return self.downloaded_bytes > 0 and Path(self.partial_path).is_file()


def _now_utc() -> str:
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
        # Virus scanners and file-indexing services can briefly retain a
        # handle to the previous JSON on Windows.  Retrying the same atomic
        # replacement keeps the metadata durable without falling back to an
        # in-place write that could be torn by a crash.
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt >= 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def load_partial_metadata(path: str | os.PathLike[str]) -> PartialDownload | None:
    metadata_path = Path(path)
    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    try:
        return PartialDownload(
            url=str(payload.get("url", "")),
            target_path=str(payload.get("target_path", "")),
            partial_path=str(payload.get("partial_path", "")),
            metadata_path=str(metadata_path),
            downloaded_bytes=int(payload.get("downloaded_bytes", 0) or 0),
            expected_size=int(payload.get("expected_size", 0) or 0),
            etag=str(payload.get("etag", "") or ""),
            last_modified=str(payload.get("last_modified", "") or ""),
            status=str(payload.get("status", "partial") or "partial"),
            updated_utc=str(payload.get("updated_utc", "") or ""),
        )
    except (TypeError, ValueError):
        return None


def human_bytes(value: int | float) -> str:
    size = float(max(0.0, float(value)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TiB"


def format_eta(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d} h {minutes:02d} min"
    if minutes:
        return f"{minutes:d} min {secs:02d} s"
    return f"{secs:d} s"


def progress_message(progress: DownloadProgress) -> str:
    phase = {
        "connecting": "Connectant",
        "downloading": "Descarregant",
        "verifying": "Verificant",
        "extracting": "Extraient",
        "registering": "Indexant i registrant",
        "completed": "Completat",
        "paused": "Pausada",
        "error": "Error",
    }.get(progress.phase, progress.phase)
    if progress.phase != "downloading":
        return f"{phase}: {progress.detail}" if progress.detail else phase
    total = human_bytes(progress.total_bytes) if progress.total_bytes else "?"
    speed = (
        f"{human_bytes(progress.speed_bytes_s)}/s"
        if progress.speed_bytes_s > 0
        else "—"
    )
    return (
        f"{phase}: {human_bytes(progress.downloaded_bytes)} / {total} "
        f"· {max(0.0, progress.percent):.1f}% · {speed} "
        f"· restant {format_eta(progress.eta_seconds)}"
    )


class ResumableDownloader:
    """Range-aware, retrying streaming downloader with stable ``.part`` files."""

    def __init__(
        self,
        partial_root: str | os.PathLike[str],
        *,
        session: requests.Session | None = None,
        chunk_size: int = 4 * 1024 * 1024,
        max_retries: int = 3,
        timeout: tuple[float, float] = (30.0, 90.0),
        disk_usage: Callable[[str | os.PathLike[str]], object] = shutil.disk_usage,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.partial_root = Path(partial_root).expanduser().resolve(strict=False)
        self.partial_root.mkdir(parents=True, exist_ok=True)
        self.session = session or _native_trust_session()
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self.max_retries = max(0, int(max_retries))
        self.timeout = timeout
        self.disk_usage = disk_usage
        self.retry_sleep = retry_sleep

    def paths_for(self, target_path: str | os.PathLike[str]) -> tuple[Path, Path]:
        target = Path(target_path)
        partial = self.partial_root / f"{target.name}.part"
        return partial, partial.with_name(f"{partial.name}.json")

    def partial_for(self, target_path: str | os.PathLike[str]) -> PartialDownload | None:
        partial, metadata = self.paths_for(target_path)
        state = load_partial_metadata(metadata)
        if state is None and partial.is_file():
            return PartialDownload(
                url="",
                target_path=str(Path(target_path)),
                partial_path=str(partial),
                metadata_path=str(metadata),
                downloaded_bytes=int(partial.stat().st_size),
                expected_size=0,
                status="partial",
            )
        if state is None:
            return None
        actual = int(partial.stat().st_size) if partial.is_file() else 0
        return PartialDownload(
            **{
                **asdict(state),
                "downloaded_bytes": actual,
                "partial_path": str(partial),
                "metadata_path": str(metadata),
            }
        )

    @staticmethod
    def required_space(
        archive_size: int,
        extracted_size: int,
        downloaded_bytes: int = 0,
        *,
        safety_margin_bytes: int | None = None,
    ) -> int:
        archive = max(0, int(archive_size))
        extracted = max(0, int(extracted_size))
        remaining = max(0, archive - max(0, int(downloaded_bytes)))
        margin = (
            max(1024**3, int(math.ceil((archive + extracted) * 0.10)))
            if safety_margin_bytes is None
            else max(0, int(safety_margin_bytes))
        )
        return remaining + extracted + margin

    def ensure_space(
        self,
        *,
        archive_size: int,
        extracted_size: int,
        downloaded_bytes: int = 0,
        safety_margin_bytes: int | None = None,
    ) -> int:
        required = self.required_space(
            archive_size,
            extracted_size,
            downloaded_bytes,
            safety_margin_bytes=safety_margin_bytes,
        )
        available = int(self.disk_usage(self.partial_root).free)
        if available < required:
            raise InsufficientSpaceError(required, available)
        return required

    def _save_state(
        self,
        metadata_path: Path,
        *,
        url: str,
        target: Path,
        partial: Path,
        downloaded: int,
        expected: int,
        etag: str,
        last_modified: str,
        status: str,
    ) -> None:
        _atomic_json(
            metadata_path,
            {
                "schema_version": DOWNLOAD_METADATA_SCHEMA_VERSION,
                "url": str(url),
                "target_path": str(target),
                "partial_path": str(partial),
                "downloaded_bytes": int(downloaded),
                "expected_size": int(expected),
                "etag": str(etag or ""),
                "last_modified": str(last_modified or ""),
                "status": str(status),
                "updated_utc": _now_utc(),
            },
        )

    @staticmethod
    def _emit(
        callback: Callable[[DownloadProgress], None] | None,
        progress: DownloadProgress,
    ) -> None:
        if callback is not None:
            callback(progress)

    def _remote_metadata(self, url: str) -> tuple[int, str, str]:
        try:
            response = self.session.head(
                url,
                allow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "TerraLab/1.0 (layer library downloader)"},
            )
            response.raise_for_status()
        except requests.RequestException:
            return 0, "", ""
        try:
            size = int(response.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            size = 0
        return (
            max(0, size),
            str(response.headers.get("ETag", "") or ""),
            str(response.headers.get("Last-Modified", "") or ""),
        )

    def download(
        self,
        url: str,
        target_path: str | os.PathLike[str],
        *,
        expected_size: int = 0,
        extracted_size: int = 0,
        progress: Callable[[DownloadProgress], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        safety_margin_bytes: int | None = None,
    ) -> Path:
        target = Path(target_path).expanduser().resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial, metadata_path = self.paths_for(target)
        partial.parent.mkdir(parents=True, exist_ok=True)
        self._emit(progress, DownloadProgress("connecting", detail=str(url)))
        if cancelled is not None and cancelled():
            raise DownloadCancelled("Descàrrega pausada abans de connectar.")

        state = self.partial_for(target)
        downloaded = int(partial.stat().st_size) if partial.is_file() else 0
        remote_size, remote_etag, remote_modified = self._remote_metadata(url)
        total = max(0, int(expected_size or remote_size or (state.expected_size if state else 0)))

        compatible = state is None or not state.url or state.url == str(url)
        if state is not None and state.url and state.url != str(url):
            compatible = False
        if state is not None and downloaded:
            if state.etag and remote_etag and state.etag != remote_etag:
                compatible = False
            if (
                not state.etag
                and state.last_modified
                and remote_modified
                and state.last_modified != remote_modified
            ):
                compatible = False
            if state.expected_size and total and state.expected_size != total:
                compatible = False
        if not compatible and partial.exists():
            stale = partial.with_name(
                f"{partial.name}.stale-{int(time.time())}"
            )
            os.replace(partial, stale)
            downloaded = 0

        self.ensure_space(
            archive_size=total,
            extracted_size=extracted_size,
            downloaded_bytes=downloaded,
            safety_margin_bytes=safety_margin_bytes,
        )
        etag = remote_etag or (state.etag if state and compatible else "")
        last_modified = remote_modified or (
            state.last_modified if state and compatible else ""
        )
        self._save_state(
            metadata_path,
            url=url,
            target=target,
            partial=partial,
            downloaded=downloaded,
            expected=total,
            etag=etag,
            last_modified=last_modified,
            status="downloading",
        )

        attempt = 0
        started = time.monotonic()
        speed_started_bytes = downloaded
        backup: Path | None = None
        while True:
            response = None
            try:
                if cancelled is not None and cancelled():
                    raise DownloadCancelled(
                        "Descàrrega pausada; el fitxer .part es conserva per reprendre-la."
                    )
                offset = int(partial.stat().st_size) if partial.exists() else 0
                headers = {
                    "User-Agent": "TerraLab/1.0 (layer library downloader)",
                    "Accept": "*/*",
                }
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                    validator = etag or last_modified
                    if validator:
                        headers["If-Range"] = validator
                response = self.session.get(
                    url,
                    headers=headers,
                    stream=True,
                    allow_redirects=True,
                    timeout=self.timeout,
                )
                response.raise_for_status()

                append = False
                if offset:
                    if response.status_code == 206:
                        match = _CONTENT_RANGE.match(
                            str(response.headers.get("Content-Range", "") or "")
                        )
                        if match is None or int(match.group(1)) != offset:
                            raise IOError(
                                "El servidor ha retornat un rang incompatible amb el fitxer parcial."
                            )
                        range_end = int(match.group(2))
                        if range_end < offset:
                            raise IOError(
                                "El servidor ha retornat un Content-Range buit o invertit."
                            )
                        if match.group(3) != "*":
                            range_total = int(match.group(3))
                            if total and range_total != total:
                                raise IOError(
                                    "La mida remota ha canviat durant la represa; "
                                    "el fitxer parcial es conserva sense concatenar."
                                )
                            if total <= 0:
                                total = range_total
                        append = True
                    elif response.status_code == 200:
                        # Preserve the older valid partial until the replacement
                        # has completed.  A failed non-Range restart can then
                        # restore the larger of the two partial downloads.
                        backup = partial.with_name(f"{partial.name}.range-backup")
                        if backup.exists():
                            backup.unlink()
                        os.replace(partial, backup)
                        offset = 0
                    else:
                        raise IOError(
                            f"Resposta HTTP {response.status_code} invàlida per reprendre."
                        )

                response_etag = str(response.headers.get("ETag", "") or "")
                response_modified = str(
                    response.headers.get("Last-Modified", "") or ""
                )
                if append and etag and response_etag and response_etag != etag:
                    raise IOError("L'ETag remot ha canviat durant la represa.")
                etag = response_etag or etag
                last_modified = response_modified or last_modified
                try:
                    response_length = int(
                        response.headers.get("Content-Length", "0") or 0
                    )
                except (TypeError, ValueError):
                    response_length = 0
                content_total = offset + response_length if response_length else 0
                if total <= 0:
                    total = content_total
                mode = "ab" if append else "wb"
                with partial.open(mode) as output:
                    downloaded = offset
                    for chunk in response.iter_content(chunk_size=self.chunk_size):
                        if cancelled is not None and cancelled():
                            raise DownloadCancelled(
                                "Descàrrega pausada; el fitxer .part es conserva per reprendre-la."
                            )
                        if not chunk:
                            continue
                        output.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(1e-6, time.monotonic() - started)
                        speed = max(0.0, (downloaded - speed_started_bytes) / elapsed)
                        eta = (
                            max(0.0, (total - downloaded) / speed)
                            if total > downloaded and speed > 0.0
                            else (0.0 if total and downloaded >= total else None)
                        )
                        self._save_state(
                            metadata_path,
                            url=url,
                            target=target,
                            partial=partial,
                            downloaded=downloaded,
                            expected=total,
                            etag=etag,
                            last_modified=last_modified,
                            status="downloading",
                        )
                        self._emit(
                            progress,
                            DownloadProgress(
                                "downloading",
                                downloaded,
                                total,
                                speed,
                                eta,
                            ),
                        )
                    output.flush()
                    os.fsync(output.fileno())
                if total and downloaded != total:
                    raise IOError(
                        f"Mida incompleta: {downloaded} bytes de {total}."
                    )
                if backup is not None:
                    backup.unlink(missing_ok=True)
                    backup = None
                os.replace(partial, target)
                metadata_path.unlink(missing_ok=True)
                return target
            except DownloadCancelled:
                if backup is not None and backup.exists():
                    current_size = partial.stat().st_size if partial.exists() else 0
                    if backup.stat().st_size > current_size:
                        partial.unlink(missing_ok=True)
                        os.replace(backup, partial)
                    else:
                        backup.unlink(missing_ok=True)
                    backup = None
                downloaded = partial.stat().st_size if partial.exists() else 0
                self._save_state(
                    metadata_path,
                    url=url,
                    target=target,
                    partial=partial,
                    downloaded=downloaded,
                    expected=total,
                    etag=etag,
                    last_modified=last_modified,
                    status="paused",
                )
                self._emit(
                    progress,
                    DownloadProgress("paused", downloaded, total),
                )
                raise
            except (requests.RequestException, OSError, IOError) as exc:
                if backup is not None and backup.exists():
                    current_size = partial.stat().st_size if partial.exists() else 0
                    if backup.stat().st_size > current_size:
                        partial.unlink(missing_ok=True)
                        os.replace(backup, partial)
                    else:
                        backup.unlink(missing_ok=True)
                    backup = None
                downloaded = partial.stat().st_size if partial.exists() else 0
                self._save_state(
                    metadata_path,
                    url=url,
                    target=target,
                    partial=partial,
                    downloaded=downloaded,
                    expected=total,
                    etag=etag,
                    last_modified=last_modified,
                    status="error",
                )
                if attempt >= self.max_retries:
                    raise RuntimeError(
                        f"La descàrrega ha fallat després de {attempt + 1} intents: {exc}"
                    ) from exc
                attempt += 1
                self.retry_sleep(min(8.0, float(2 ** (attempt - 1))))
            finally:
                if response is not None:
                    response.close()


__all__ = [
    "DOWNLOAD_METADATA_SCHEMA_VERSION",
    "DownloadCancelled",
    "DownloadProgress",
    "InsufficientSpaceError",
    "PartialDownload",
    "ResumableDownloader",
    "format_eta",
    "human_bytes",
    "load_partial_metadata",
    "progress_message",
]
