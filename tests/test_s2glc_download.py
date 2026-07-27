from __future__ import annotations

import threading
import zipfile
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import numpy as np
import rasterio
import truststore
from rasterio.transform import from_origin

from TerraLab.common.data_library import DataLibrary
from TerraLab.data.assets_manager import AssetManager, AssetOperationCancelled
from TerraLab.data.resumable_download import (
    DownloadCancelled,
    InsufficientSpaceError,
    ResumableDownloader,
)


class _RangeState:
    def __init__(self, content: bytes, *, support_range: bool = True) -> None:
        self.content = content
        self.support_range = support_range
        self.etag = '"s2glc-v1"'
        self.last_modified = "Wed, 22 Jul 2026 10:00:00 GMT"
        self.requests: list[tuple[str, str]] = []
        self.transient_failures = 0


class _RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _RangeState:
        return self.server.state  # type: ignore[attr-defined]

    def _headers(self, status: int, length: int, content_range: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.send_header("ETag", self.state.etag)
        self.send_header("Last-Modified", self.state.last_modified)
        self.send_header(
            "Accept-Ranges", "bytes" if self.state.support_range else "none"
        )
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib callback name
        self.state.requests.append(("HEAD", ""))
        self._headers(200, len(self.state.content))

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        raw_range = str(self.headers.get("Range", "") or "")
        self.state.requests.append(("GET", raw_range))
        if self.state.transient_failures > 0:
            self.state.transient_failures -= 1
            self._headers(503, 0)
            return
        start = 0
        if self.state.support_range and raw_range.startswith("bytes="):
            start = int(raw_range.removeprefix("bytes=").split("-", 1)[0])
            payload = self.state.content[start:]
            self._headers(
                206,
                len(payload),
                f"bytes {start}-{len(self.state.content) - 1}/{len(self.state.content)}",
            )
        else:
            payload = self.state.content
            self._headers(200, len(payload))
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format: str, *_args) -> None:
        return


class _Server:
    def __init__(self, state: _RangeState) -> None:
        self.state = state
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        self.httpd.state = state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.httpd.server_port}/s2glc.zip"

    def __exit__(self, *_exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5.0)


def _downloader(tmp_path: Path) -> ResumableDownloader:
    return ResumableDownloader(
        tmp_path / "partial",
        chunk_size=64 * 1024,
        max_retries=0,
        retry_sleep=lambda _seconds: None,
    )


def test_default_downloader_uses_native_certificate_store(tmp_path):
    downloader = _downloader(tmp_path)
    adapter = downloader.session.get_adapter("https://example.test/")

    assert adapter._ssl_context.__class__ is truststore.SSLContext
    assert adapter.poolmanager.connection_pool_kw["ssl_context"] is (
        adapter._ssl_context
    )


def test_new_download_is_streamed_and_published_atomically(tmp_path):
    content = bytes(range(256)) * 4096
    state = _RangeState(content)
    events = []
    target = tmp_path / "downloads" / "categorical.zip"
    with _Server(state) as url:
        result = _downloader(tmp_path).download(
            url,
            target,
            expected_size=len(content),
            safety_margin_bytes=0,
            progress=events.append,
        )

    assert result.read_bytes() == content
    assert events[0].phase == "connecting"
    assert events[-1].downloaded_bytes == len(content)
    assert events[-1].percent == 100.0
    assert not (tmp_path / "partial" / "categorical.zip.part").exists()
    assert not (tmp_path / "partial" / "categorical.zip.part.json").exists()


def test_cancel_restart_detects_partial_and_resumes_with_range(tmp_path):
    content = b"S2GLC" * 200_000
    state = _RangeState(content)
    target = tmp_path / "downloads" / "rgb.zip"
    pause = {"value": False}

    def progress(event):
        if event.phase == "downloading" and event.downloaded_bytes >= 64 * 1024:
            pause["value"] = True

    with _Server(state) as url:
        first = _downloader(tmp_path)
        with pytest.raises(DownloadCancelled):
            first.download(
                url,
                target,
                expected_size=len(content),
                safety_margin_bytes=0,
                progress=progress,
                cancelled=lambda: pause["value"],
            )
        partial = first.partial_for(target)
        assert partial is not None and partial.resumable
        assert partial.status == "paused"

        # A new service instance models a complete TerraLab restart.
        restarted = _downloader(tmp_path)
        detected = restarted.partial_for(target)
        assert detected is not None
        offset = detected.downloaded_bytes
        restarted.download(
            url,
            target,
            expected_size=len(content),
            safety_margin_bytes=0,
        )

    assert target.read_bytes() == content
    assert ("GET", f"bytes={offset}-") in state.requests


def test_server_ignoring_range_restarts_without_losing_valid_partial_on_failure(
    tmp_path,
):
    content = b"rgb" * 300_000
    state = _RangeState(content)
    target = tmp_path / "rgb.zip"
    pause = {"value": False}

    with _Server(state) as url:
        first = _downloader(tmp_path)

        def stop(event):
            pause["value"] = event.downloaded_bytes >= 64 * 1024

        with pytest.raises(DownloadCancelled):
            first.download(
                url,
                target,
                expected_size=len(content),
                safety_margin_bytes=0,
                progress=stop,
                cancelled=lambda: pause["value"],
            )
        offset = first.partial_for(target).downloaded_bytes
        state.support_range = False
        _downloader(tmp_path).download(
            url,
            target,
            expected_size=len(content),
            safety_margin_bytes=0,
        )

    assert target.read_bytes() == content
    assert ("GET", f"bytes={offset}-") in state.requests


def test_changed_etag_never_concatenates_incompatible_content(tmp_path):
    old = b"A" * 600_000
    new = b"B" * len(old)
    state = _RangeState(old)
    target = tmp_path / "categorical.zip"
    pause = {"value": False}
    with _Server(state) as url:
        downloader = _downloader(tmp_path)

        def stop(event):
            pause["value"] = event.downloaded_bytes >= 64 * 1024

        with pytest.raises(DownloadCancelled):
            downloader.download(
                url,
                target,
                expected_size=len(old),
                safety_margin_bytes=0,
                progress=stop,
                cancelled=lambda: pause["value"],
            )
        state.content = new
        state.etag = '"s2glc-v2"'
        _downloader(tmp_path).download(
            url,
            target,
            expected_size=len(new),
            safety_margin_bytes=0,
        )

    assert target.read_bytes() == new
    assert list((tmp_path / "partial").glob("*.stale-*"))


def test_transient_http_failure_is_retried_a_limited_number_of_times(tmp_path):
    content = b"retry" * 100_000
    state = _RangeState(content)
    state.transient_failures = 1
    target = tmp_path / "retry.zip"
    with _Server(state) as url:
        downloader = ResumableDownloader(
            tmp_path / "partial",
            chunk_size=64 * 1024,
            max_retries=2,
            retry_sleep=lambda _seconds: None,
        )
        downloader.download(
            url,
            target,
            expected_size=len(content),
            safety_margin_bytes=0,
        )
    assert target.read_bytes() == content
    assert sum(method == "GET" for method, _range in state.requests) == 2


def test_space_check_accounts_for_archive_extraction_and_margin(tmp_path):
    downloader = ResumableDownloader(
        tmp_path / "partial",
        disk_usage=lambda _path: SimpleNamespace(free=1_999),
    )
    assert downloader.required_space(
        1_000, 2_000, safety_margin_bytes=300
    ) == 3_300
    with pytest.raises(InsufficientSpaceError) as error:
        downloader.ensure_space(
            archive_size=1_000,
            extracted_size=2_000,
            safety_margin_bytes=300,
        )
    assert error.value.required == 3_300
    assert error.value.available == 1_999


def test_corrupt_zip_is_rejected_and_cancellation_preserves_archive(tmp_path):
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="ZIP corrupte"):
        AssetManager._validate_and_extract_zip(broken, tmp_path / "broken-out")
    assert broken.exists()

    archive = tmp_path / "valid.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
        handle.writestr("S2GLC.tif", b"x" * (256 * 1024))
    calls = {"count": 0}

    def cancelled():
        calls["count"] += 1
        return calls["count"] >= 3

    with pytest.raises(AssetOperationCancelled):
        AssetManager._validate_and_extract_zip(
            archive,
            tmp_path / "paused-out",
            cancelled=cancelled,
        )
    assert archive.exists()


def test_complete_s2glc_install_runs_all_phases_off_the_calling_thread(
    tmp_path, monkeypatch
):
    source_tiff = tmp_path / "source" / "S2GLC-codes.tif"
    source_tiff.parent.mkdir()
    with rasterio.open(
        source_tiff,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="uint8",
        crs="EPSG:3035",
        transform=from_origin(4_000_000.0, 3_000_000.0, 10.0, 10.0),
    ) as dataset:
        dataset.write(np.full((1, 4, 4), 82, dtype=np.uint8))
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.write(source_tiff, arcname=source_tiff.name)
    content = archive.read_bytes()
    state = _RangeState(content)
    monkeypatch.setattr(
        "TerraLab.data.assets_manager.set_config_value", lambda *_args: None
    )

    with _Server(state) as url:
        manager = AssetManager(DataLibrary(tmp_path / "library"))
        manager.specs["surface_categorical"] = replace(
            manager.get_spec("surface_categorical"),
            auto_download_url=url,
            expected_download_bytes=len(content),
            expected_extracted_bytes=source_tiff.stat().st_size,
        )
        events: list[tuple[float, str]] = []
        outcome = {}

        def work():
            try:
                outcome["result"] = manager.download_and_prepare(
                    "surface_categorical",
                    progress_callback=lambda percent, message: events.append(
                        (percent, message)
                    ),
                    options={"display_name": "S2GLC fixture"},
                )
            except Exception as exc:  # surfaced with useful pytest context
                outcome["error"] = exc

        worker = threading.Thread(target=work)
        worker.start()
        assert worker is not threading.current_thread()
        worker.join(timeout=20.0)

    assert not worker.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    assert outcome["result"]["ok"] is True
    messages = "\n".join(message for _percent, message in events)
    for phase in (
        "Connectant",
        "Descarregant",
        "Verificant",
        "Extraient",
        "Indexant",
        "Completat",
    ):
        assert phase in messages
    status = manager.asset_status("surface_categorical")
    assert status["ready"] is True
    assert status["install_state"] == "prepared"
