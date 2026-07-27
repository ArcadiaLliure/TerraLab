"""Onboarding dialogs: first-run welcome and per-layer asset assistant."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Dict, Iterable, Optional

from PyQt5.QtCore import (
    QObject,
    QProcess,
    QProcessEnvironment,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt5.QtGui import QDesktopServices, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.utils import (
    getTraduction,
    get_config_value,
    set_config_value,
)
from TerraLab.data.assets_manager import AssetManager
from TerraLab.runtime.protocol import (
    ARTIFACT_READY,
    COMPUTE_REQUEST,
    PROGRESS,
    WORKER_ERROR,
    Envelope,
    envelope,
)
from TerraLab.ui.design_system import DIALOG_STYLESHEET

_ASTRO_DIALOG_STYLE = """
QDialog {
    background-color: #0b1324;
    color: #ffe680;
}
QLabel, QCheckBox {
    color: #ffe680;
}
QInputDialog, QMessageBox {
    background-color: #0b1324;
    color: #ffe680;
}
QInputDialog QLabel, QMessageBox QLabel {
    color: #ffe680;
}
QFrame#panel {
    background-color: #13203a;
    border: 1px solid #223a63;
    border-radius: 12px;
}
QFrame#assetRow {
    background-color: #15233f;
    border: 1px solid #26416e;
    border-radius: 10px;
}
QLabel#titleLabel {
    color: #fff2a8;
    font-size: 20px;
    font-weight: 700;
}
QLabel#subtitleLabel {
    color: #ffd45a;
    font-size: 11px;
}
QLabel#assetStatusOk {
    color: #9be4c6;
    font-weight: 600;
}
QLabel#assetStatusMissing {
    color: #ffb2ab;
    font-weight: 600;
}
QPushButton {
    background-color: #1c365f;
    color: #e8f0ff;
    border: 1px solid #2f5488;
    border-radius: 8px;
    padding: 6px 12px;
}
QPushButton:hover {
    background-color: #24457a;
}
QPushButton:pressed {
    background-color: #18304f;
}
QPushButton:disabled {
    background-color: #14223b;
    color: #6f83a8;
    border-color: #1e3152;
}
QLineEdit, QTextEdit {
    background-color: #0f1b31;
    color: #fff0a8;
    border: 1px solid #27426f;
    border-radius: 8px;
    padding: 6px;
}
QProgressBar {
    border: 1px solid #2f4f82;
    border-radius: 7px;
    background-color: #0f1b31;
    text-align: center;
    color: #d9e5ff;
}
QProgressBar::chunk {
    background-color: #44b3ff;
    border-radius: 6px;
}
"""
_ASTRO_DIALOG_STYLE += DIALOG_STYLESHEET


_GAIA_BACKGROUND_PROCESSES = []
_GAIA_BACKGROUND_DOWNLOADS: dict[tuple[str, str], QProcess] = {}
_BACKGROUND_ASSET_JOBS = {}


def _background_job_key(manager: AssetManager, asset_id: str) -> tuple[str, str]:
    try:
        root = str(Path(manager.library.root).expanduser().resolve())
    except Exception:
        root = str(getattr(getattr(manager, "library", None), "root", ""))
    return os.path.normcase(root), str(asset_id)


def _keep_gaia_process_alive(
    proc: QProcess, key: tuple[str, str] | None = None
) -> None:
    if proc is None:
        return
    if proc in _GAIA_BACKGROUND_PROCESSES:
        if key is not None:
            _GAIA_BACKGROUND_DOWNLOADS[key] = proc
        return
    _GAIA_BACKGROUND_PROCESSES.append(proc)
    if key is not None:
        _GAIA_BACKGROUND_DOWNLOADS[key] = proc

    def _cleanup(*_args):
        try:
            _GAIA_BACKGROUND_PROCESSES.remove(proc)
        except Exception:
            log_suppressed_exception(__name__, "_keep_gaia_process_alive._cleanup")
        for stored_key, stored_proc in tuple(_GAIA_BACKGROUND_DOWNLOADS.items()):
            if stored_proc is proc:
                _GAIA_BACKGROUND_DOWNLOADS.pop(stored_key, None)
        try:
            proc.deleteLater()
        except Exception:
            log_suppressed_exception(__name__, "_keep_gaia_process_alive._cleanup")

    try:
        proc.finished.connect(_cleanup)
    except Exception:
        log_suppressed_exception(__name__, "_keep_gaia_process_alive")


class _CopernicusNodataProbeWorker(QObject):
    """Thin UI client for the advisory sample executed by Compute."""

    completed = pyqtSignal(object, str, bool)

    def __init__(self, request: object) -> None:
        super().__init__()
        self.request = request
        self._request_id = f"copernicus-probe-{uuid.uuid4().hex}"
        self._generation = 1
        self._finished = False
        app = QApplication.instance()
        self._runtime = getattr(app, "terralab_runtime", None)
        if self._runtime is not None:
            self._runtime.message_received.connect(self._on_message)
            self._runtime.worker_ready.connect(self._on_worker_ready)
            self._runtime.worker_unavailable.connect(
                self._on_worker_unavailable
            )

    def cancel(self) -> None:
        if self._finished:
            return
        self._finish(None, "", True)

    def run(self) -> None:
        if self._finished:
            return
        if self._runtime is None:
            self._finish(
                None,
                "El proceso Compute no está disponible.",
                False,
            )
            return
        sent = self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                {
                    "operation": "copernicus_probe",
                    "request": self.request.to_dict(),
                },
                request_id=self._request_id,
                generation=self._generation,
            ),
        )
        if not sent:
            return

    def _on_worker_ready(self, role: str) -> None:
        if role == "compute" and not self._finished:
            self.run()

    def _on_worker_unavailable(self, role: str, reason: str) -> None:
        if role == "compute" and not self._finished:
            self._finish(None, str(reason), False)

    def _on_message(self, role: str, message: Envelope) -> None:
        if (
            role != "compute"
            or self._finished
            or message.request_id != self._request_id
            or message.generation != self._generation
        ):
            return
        if message.kind == ARTIFACT_READY:
            self._finish(message.payload.get("value"), "", False)
        elif message.kind == WORKER_ERROR:
            self._finish(
                None,
                str(message.payload.get("message", "Error en Compute")),
                False,
            )

    def _finish(
        self, result: object, error_message: str, cancelled: bool
    ) -> None:
        if self._finished:
            return
        self._finished = True
        self._disconnect_runtime()
        self.completed.emit(result, error_message, cancelled)

    def _disconnect_runtime(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        for signal, callback in (
            (runtime.message_received, self._on_message),
            (runtime.worker_ready, self._on_worker_ready),
            (runtime.worker_unavailable, self._on_worker_unavailable),
        ):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass


class _AssetBackgroundJob(QObject):
    """Application-owned handle for an asset task executed by Compute."""

    progress = pyqtSignal(float, str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        manager: AssetManager,
        mode: str,
        asset_id: str,
        files: Optional[Iterable[str]] = None,
        options: Optional[dict] = None,
    ) -> None:
        super().__init__()
        self.manager = manager
        self.mode = str(mode)
        self.asset_id = str(asset_id)
        self.key = _background_job_key(manager, asset_id)
        self.percent = 0.0
        self.message = "Preparant tasca en segon pla..."
        self.running = True
        self.started = False
        self._files = tuple(str(value) for value in (files or ()))
        self._options = dict(options or {})
        self._job_id = f"asset-{uuid.uuid4().hex}"
        self._request_id = self._job_id
        self._generation = 1
        app = QApplication.instance()
        self._runtime = getattr(app, "terralab_runtime", None)
        if self._runtime is not None:
            self._runtime.message_received.connect(self._on_message)
            self._runtime.worker_ready.connect(self._on_worker_ready)
            self._runtime.worker_unavailable.connect(
                self._on_worker_unavailable
            )

    def start(self) -> None:
        if self.started or not self.running:
            return
        self.started = True
        if self._runtime is None:
            self._on_failed("El proceso Compute no está disponible.")
            return
        self._dispatch()

    def cancel(self) -> None:
        if not self.running or self._runtime is None:
            return
        self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                {
                    "operation": "asset_cancel",
                    "job_id": self._job_id,
                },
                request_id=f"{self._request_id}-cancel",
                generation=self._generation,
            ),
        )

    def _dispatch(self) -> bool:
        if self._runtime is None:
            return False
        return bool(
            self._runtime.send(
                "compute",
                envelope(
                    COMPUTE_REQUEST,
                    {
                        "operation": "asset_job",
                        "job_id": self._job_id,
                        "library_root": str(self.manager.library.root),
                        "mode": self.mode,
                        "asset_id": self.asset_id,
                        "files": list(self._files),
                        "options": dict(self._options),
                    },
                    request_id=self._request_id,
                    generation=self._generation,
                ),
            )
        )

    def _on_worker_ready(self, role: str) -> None:
        if role == "compute" and self.started and self.running:
            self._dispatch()

    def _on_worker_unavailable(self, role: str, reason: str) -> None:
        if role == "compute" and self.running:
            self._on_failed(str(reason))

    def _on_message(self, role: str, message: Envelope) -> None:
        if (
            role != "compute"
            or not self.running
            or message.request_id != self._request_id
            or message.generation != self._generation
            or message.payload.get("operation") != "asset_job"
        ):
            return
        if message.kind == PROGRESS:
            value = message.payload.get("value", {})
            self._on_progress(
                float(value.get("percent", -1.0)),
                str(value.get("message", "")),
            )
        elif message.kind == ARTIFACT_READY:
            self._on_completed(message.payload.get("value"))
        elif message.kind == WORKER_ERROR:
            self._on_failed(
                str(message.payload.get("message", "Error en Compute"))
            )

    def _on_progress(self, percent: float, message: str) -> None:
        self.percent = float(percent)
        self.message = str(message)
        self.progress.emit(self.percent, self.message)

    def _on_completed(self, result: object) -> None:
        self.running = False
        self.percent = 100.0
        self.message = "Dades preparades correctament."
        self.completed.emit(result)
        self._retire()

    def _on_failed(self, error_message: str) -> None:
        self.running = False
        self.message = str(error_message)
        self.failed.emit(self.message)
        self._retire()

    def _retire(self) -> None:
        runtime = self._runtime
        if runtime is not None:
            for signal, callback in (
                (runtime.message_received, self._on_message),
                (runtime.worker_ready, self._on_worker_ready),
                (
                    runtime.worker_unavailable,
                    self._on_worker_unavailable,
                ),
            ):
                try:
                    signal.disconnect(callback)
                except (TypeError, RuntimeError):
                    pass
        if _BACKGROUND_ASSET_JOBS.get(self.key) is self:
            _BACKGROUND_ASSET_JOBS.pop(self.key, None)


def _active_asset_job(
    manager: AssetManager, asset_id: str
) -> Optional[_AssetBackgroundJob]:
    job = _BACKGROUND_ASSET_JOBS.get(_background_job_key(manager, asset_id))
    return job if job is not None and bool(job.running) else None


def _start_asset_job(
    manager: AssetManager,
    mode: str,
    asset_id: str,
    files: Optional[Iterable[str]] = None,
    options: Optional[dict] = None,
    *,
    start_immediately: bool = True,
) -> _AssetBackgroundJob:
    key = _background_job_key(manager, asset_id)
    current = _BACKGROUND_ASSET_JOBS.get(key)
    if current is not None and bool(current.running):
        return current
    job = _AssetBackgroundJob(
        manager,
        mode=mode,
        asset_id=asset_id,
        files=files,
        options=options,
    )
    _BACKGROUND_ASSET_JOBS[key] = job
    if start_immediately:
        job.start()
    return job


class AssetOnboardingDialog(QDialog):
    """Mini-onboarding shown when a layer is enabled but required data is missing."""

    def __init__(self, manager: AssetManager, asset_id: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.asset_id = str(asset_id)
        self.spec = self.manager.get_spec(self.asset_id)
        self._completed = False
        self._job: Optional[_AssetBackgroundJob] = None
        self._job_detached = False
        self._copernicus_probe_worker: Optional[
            _CopernicusNodataProbeWorker
        ] = None
        self._copernicus_probe_context: Optional[tuple[object, object]] = None
        self._gaia_tap_process: Optional[QProcess] = None
        self._gaia_tap_out_buffer = ""
        self._gaia_tap_log_path: Optional[Path] = None
        self._gaia_tap_state_path: Optional[Path] = None
        self._gaia_visible_ready = False
        self._gaia_process_detached = False
        self._gaia_download_mode = "tiles"
        self._gaia_state_watch_timer: Optional[QTimer] = None
        self._gaia_last_polled_state_signature = ""

        self.setWindowTitle(f"TerraLab - {self.spec.title}")
        self.setModal(True)
        # Algun entorns Windows eleven el minim vertical real (>540) per marges/frame.
        # Partir d'una alcada inicial superior evita avisos de geometria i finestres truncades.
        self.resize(760, 560)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        if self.asset_id == "gaia_catalog":
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setStyleSheet(_ASTRO_DIALOG_STYLE)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(8)

        title = QLabel(self.spec.title)
        title.setObjectName("titleLabel")
        title_font = QFont()
        title_font.setPointSize(15)
        title_font.setBold(True)
        title.setFont(title_font)
        panel_layout.addWidget(title)

        desc = QLabel(
            getTraduction(
                "Onboarding.AssetNeedData",
                "Aquesta capa necessita dades per funcionar. Tria com vols preparar-les.",
            )
        )
        desc.setWordWrap(True)
        panel_layout.addWidget(desc)

        details = QTextEdit()
        details.setReadOnly(True)
        details.setMinimumHeight(168)
        extra_help = ""
        if self.asset_id == "gaia_catalog":
            try:
                gaia_log_path = self._resolve_gaia_tap_log_path()
                gaia_log_line = f"\nLog persistent: {gaia_log_path}\n"
            except Exception:
                gaia_log_line = (
                    "\nLog persistent: <biblioteca>/logs/gaia_tiles_last.log\n"
                )
            extra_help = (
                "\n\nEl boto 'Descarregar automaticament' executa el flux Gaia per teseles en segon pla.\n"
                "Flux: primer crea la tesela general (mag < 8), i despres completa teseles profundes "
                "fins a la magnitud objectiu, amb progressio reanudable.\n"
                f"{gaia_log_line}"
                "Opcional (avancat): pots executar-ho manualment amb:\n"
                "python tools/download_gaia_tiles.py --mag-limit 0 --tile-size-deg 10"
            )
        elif self.asset_id == "orthophoto":
            try:
                copernicus_log_path = (
                    Path(self.manager.layout["logs"])
                    / "copernicus_orthophoto_last.log"
                )
                extra_help = (
                    "\n\nLog persistent de la descàrrega:\n"
                    f"{copernicus_log_path}"
                )
            except Exception:
                extra_help = (
                    "\n\nLog persistent de la descàrrega:\n"
                    "<biblioteca>/logs/copernicus_orthophoto_last.log"
                )
        product_help = ""
        if self.spec.provider and self.spec.nominal_resolution_m:
            product_help = (
                f"Proveïdor:\n{self.spec.provider}\n\n"
                f"Tipus semàntic: {self.spec.semantic_type}\n"
                f"Resolució nominal: {self.spec.nominal_resolution_m:g} m\n"
                f"CRS nominal: {self.spec.nominal_crs}\n"
                f"Extensió: {self.spec.geographic_extent}\n\n"
            )
        conditions_help = (
            f"Condicions:\n{self.spec.license_note}\n\n"
            if self.spec.license_note
            else ""
        )
        details.setText(
            f"Font oficial:\n{self.spec.source_url}\n\n"
            f"{product_help}"
            f"Formats admesos:\n{self.spec.accepted_formats}\n\n"
            f"Credits:\n{self.spec.credits}\n\n"
            f"{conditions_help}"
            "Per defecte pots enllaçar dades pròpies a la seva ubicació original. "
            "Si tries preparar/copiar, els datasets i derivats es guarden a la biblioteca seleccionada.\n"
            f"Biblioteca activa: {self.manager.library.root}"
            f"{extra_help}"
        )
        panel_layout.addWidget(details)
        root.addWidget(panel)

        self.climate_block = QWidget()
        climate_layout = QGridLayout(self.climate_block)
        climate_layout.setContentsMargins(0, 0, 0, 0)
        climate_layout.addWidget(QLabel("METNO User-Agent"), 0, 0)
        self.txt_user_agent = QLineEdit()
        self.txt_user_agent.setPlaceholderText(
            "TerraLab/1.0 (contact@example.com)"
        )
        self.txt_user_agent.setText(self.manager.get_user_agent())
        climate_layout.addWidget(self.txt_user_agent, 0, 1)
        self.btn_save_user_agent = QPushButton("Desar")
        self.btn_save_user_agent.clicked.connect(self._save_user_agent)
        climate_layout.addWidget(self.btn_save_user_agent, 0, 2)
        self.climate_block.setVisible(self.asset_id == "climate_metno")
        root.addWidget(self.climate_block)

        self.milkyway_block = QWidget()
        milkyway_layout = QVBoxLayout(self.milkyway_block)
        milkyway_layout.setContentsMargins(0, 0, 0, 0)
        self.chk_remove_stars = QCheckBox("Eliminar estrellas (aprox StarNet)")
        self.chk_remove_stars.setChecked(True)
        self.chk_remove_stars.setToolTip(
            "Suprime puntos estelares de alto contraste en la textura de la Via Lactea."
        )
        milkyway_layout.addWidget(self.chk_remove_stars)
        self.milkyway_block.setVisible(self.asset_id == "milkyway_texture")
        root.addWidget(self.milkyway_block)

        self.s2glc_block = QWidget()
        s2glc_layout = QVBoxLayout(self.s2glc_block)
        s2glc_layout.setContentsMargins(0, 0, 0, 0)
        self.chk_remove_archive = QCheckBox(
            "Eliminar el ZIP després de validar i instal·lar el GeoTIFF"
        )
        self.chk_remove_archive.setChecked(False)
        self.chk_remove_archive.setToolTip(
            "Allibera espai només després que el GeoTIFF final s'hagi obert "
            "i registrat correctament. No elimina el GeoTIFF."
        )
        s2glc_layout.addWidget(self.chk_remove_archive)
        self.s2glc_block.setVisible(
            self.asset_id in {"surface_rgb", "surface_categorical"}
        )
        root.addWidget(self.s2glc_block)

        actions = QHBoxLayout()
        self.btn_open_source = QPushButton(
            getTraduction("Onboarding.OpenSource", "Obrir font oficial")
        )
        self.btn_open_source.clicked.connect(self._open_source)
        actions.addWidget(self.btn_open_source)

        self.btn_auto_download = QPushButton(
            getTraduction(
                "Onboarding.AutoDownload", "Descarregar automaticament"
            )
        )
        self.btn_auto_download.setEnabled(self._supports_auto_download())
        partial = self.manager.partial_download(self.asset_id)
        resumable_orthophoto = bool(
            self.asset_id == "orthophoto"
            and self.manager.asset_status("orthophoto").get(
                "resumable", False
            )
        )
        if (partial is not None and partial.resumable) or resumable_orthophoto:
            self.btn_auto_download.setText("Reprendre descàrrega")
        self.btn_auto_download.clicked.connect(self._auto_download)
        actions.addWidget(self.btn_auto_download)

        self.btn_attach = QPushButton(
            getTraduction("Onboarding.AttachFiles", "Copiar fitxer(s) a la biblioteca")
        )
        self.btn_attach.clicked.connect(self._attach_files)
        actions.addWidget(self.btn_attach)
        self.btn_attach_folder = QPushButton("Copiar carpeta a la biblioteca")
        self.btn_attach_folder.setVisible(
            self.asset_id in {"elevation_dem", "surface_rgb", "surface_categorical"}
        )
        self.btn_attach_folder.clicked.connect(self._attach_folder)
        actions.addWidget(self.btn_attach_folder)
        root.addLayout(actions)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("subtitleLabel")
        root.addWidget(self.lbl_status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.txt_process_log = QTextEdit()
        self.txt_process_log.setReadOnly(True)
        self.txt_process_log.setMinimumHeight(120)
        self.txt_process_log.setVisible(False)
        root.addWidget(self.txt_process_log)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.btn_background = QPushButton("Continuar en segon pla")
        self.btn_background.setToolTip(
            "Tanca aquesta finestra i manté la descàrrega activa. "
            "En tornar-la a obrir es recuperarà el progrés actual."
        )
        self.btn_background.setVisible(False)
        self.btn_background.clicked.connect(self._continue_in_background)
        footer.addWidget(self.btn_background)
        self.btn_cancel = QPushButton("Cancel·lar tasca")
        if self.asset_id in {
            "orthophoto",
            "surface_rgb",
            "surface_categorical",
        }:
            self.btn_cancel.setText("Pausar descàrrega")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel_job)
        footer.addWidget(self.btn_cancel)
        self.btn_close = QPushButton(
            getTraduction("Onboarding.Close", "Tancar")
        )
        self.btn_close.clicked.connect(self.reject)
        footer.addWidget(self.btn_close)
        root.addLayout(footer)

        if self.asset_id == "gaia_catalog":
            self._start_gaia_state_watch_timer()

        if self.asset_id == "climate_metno":
            self.btn_open_source.setEnabled(True)
            self.btn_auto_download.setEnabled(False)
            self.btn_attach.setEnabled(False)
            self.btn_attach_folder.setEnabled(False)

        self._restore_active_download()

    def _supports_auto_download(self) -> bool:
        if self.asset_id in {"gaia_catalog", "orthophoto"}:
            return True
        return bool(self.spec.auto_download_url)

    def _set_running_controls(self, *, allow_background: bool) -> None:
        self.btn_open_source.setEnabled(False)
        self.btn_attach.setEnabled(False)
        self.btn_attach_folder.setEnabled(False)
        self.btn_auto_download.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.btn_background.setVisible(bool(allow_background))
        self.btn_background.setEnabled(bool(allow_background))

    def _bind_asset_job(self, job: _AssetBackgroundJob) -> None:
        self._job = job
        job.progress.connect(self._on_progress)
        job.completed.connect(self._on_completed)
        job.failed.connect(self._on_failed)

    def _disconnect_asset_job(self) -> None:
        job = self._job
        if job is None:
            return
        for signal, callback in (
            (job.progress, self._on_progress),
            (job.completed, self._on_completed),
            (job.failed, self._on_failed),
        ):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
        self._job = None

    def _restore_active_download(self) -> None:
        """Reconnect a reopened dialog to the application-owned task."""

        job = _active_asset_job(self.manager, self.asset_id)
        if job is not None:
            self._bind_asset_job(job)
            self._set_running_controls(allow_background=job.mode == "download")
            self._on_progress(job.percent, job.message)
            return
        if self.asset_id != "gaia_catalog":
            return
        key = _background_job_key(self.manager, self.asset_id)
        process = _GAIA_BACKGROUND_DOWNLOADS.get(key)
        if process is None or process.state() == QProcess.NotRunning:
            _GAIA_BACKGROUND_DOWNLOADS.pop(key, None)
            return
        self._gaia_tap_process = process
        self._gaia_process_detached = False
        self._gaia_tap_log_path = self._resolve_gaia_tap_log_path()
        self._gaia_tap_state_path = self._resolve_gaia_tap_state_path()
        process.readyReadStandardOutput.connect(self._on_gaia_tap_output)
        process.finished.connect(self._on_gaia_tap_finished)
        process.errorOccurred.connect(self._on_gaia_tap_error)
        self._set_running_controls(allow_background=True)
        self.txt_process_log.setVisible(True)
        state = self._load_gaia_tap_state()
        percent = self._state_progress_percent(state)
        if isinstance(state, dict):
            self.lbl_status.setText(
                self._gaia_state_status_text(state, percent)
            )
        else:
            self.lbl_status.setText("Descarregant Gaia en segon pla...")
        if percent > 0.0:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, int(round(percent)))))
        else:
            self.progress.setRange(0, 0)

    def _continue_in_background(self) -> None:
        """Detach the progress window without stopping the active download."""

        process = self._gaia_tap_process
        if process is not None and process.state() != QProcess.NotRunning:
            self._detach_gaia_tap_process_for_background()
        elif self._job is not None and bool(self._job.running):
            self._disconnect_asset_job()
        else:
            return
        self._job_detached = True
        self._stop_gaia_state_watch_timer()
        QDialog.reject(self)

    @property
    def completed(self) -> bool:
        """Executa el metode completed de la classe AssetOnboardingDialog.

        Par?metres:
        - Cap.

        Retorna:
        - bool: Valor retornat pel metode.
        """
        return bool(self._completed)

    def _open_source(self):
        QDesktopServices.openUrl(QUrl(self.spec.source_url))

    def _save_user_agent(self):
        value = str(self.txt_user_agent.text() or "").strip()
        self.manager.set_user_agent(value)
        if value:
            self._completed = True
            QMessageBox.information(
                self,
                "TerraLab",
                "User-Agent desat. Ja pots activar la capa de clima.",
            )
            self.accept()
            return
        QMessageBox.warning(
            self, "TerraLab", "Cal definir un User-Agent valid."
        )

    def _attach_files(self):
        if self.asset_id == "climate_metno":
            return
        allow_multiple = bool(self.spec.allow_multiple)
        filters = (
            "Data files (*.fits *.png *.jpg *.jpeg *.ecsv *.csv *.npy *.npz *.zst *.zip *.7z *.bsp "
            "*.tif *.tiff *.vrt *.img *.jp2 *.asc *.txt);;"
            "All files (*.*)"
        )
        if allow_multiple:
            files, _ = QFileDialog.getOpenFileNames(
                self, "Selecciona fitxers", "", filters
            )
        else:
            single, _ = QFileDialog.getOpenFileName(
                self, "Selecciona fitxer", "", filters
            )
            files = [single] if single else []
        files = [f for f in files if f and os.path.exists(f)]
        if not files:
            return
        self._start_job(
            mode="import", files=files, options=self._collect_options()
        )

    def _attach_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Selecciona una carpeta o mosaic"
        )
        if folder:
            self._start_job(
                mode="import",
                files=[folder],
                options=self._collect_options(),
            )

    @staticmethod
    def _copernicus_estimate_value(
        estimate: object,
        name: str,
        default: int = 0,
    ) -> int:
        value = (
            estimate.get(name, default)
            if isinstance(estimate, dict)
            else getattr(estimate, name, default)
        )
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return max(0, int(default))

    def _confirm_copernicus_download(
        self,
        request: object,
        estimate: object,
        *,
        nodata_fraction: float | None = None,
        nodata_probe_error: str = "",
    ) -> bool:
        """Check disk space and confirm the selected download size."""

        from TerraLab.data import copernicus as copernicus_core

        format_bytes_dual = copernicus_core.format_bytes_dual
        bbox = getattr(request, "bbox_wgs84", None)
        coverage = copernicus_core.SERVICE_COVERAGE_WGS84
        bbox_values = (
            float(getattr(bbox, "west")),
            float(getattr(bbox, "south")),
            float(getattr(bbox, "east")),
            float(getattr(bbox, "north")),
        )
        if all(
            hasattr(coverage, field)
            for field in ("west", "south", "east", "north")
        ):
            coverage_values = (
                float(getattr(coverage, "west")),
                float(getattr(coverage, "south")),
                float(getattr(coverage, "east")),
                float(getattr(coverage, "north")),
            )
        else:
            coverage_values = tuple(
                float(value) for value in coverage
            )
        west, south, east, north = bbox_values
        cov_west, cov_south, cov_east, cov_north = coverage_values
        intersects_coverage = not (
            east <= cov_west
            or west >= cov_east
            or north <= cov_south
            or south >= cov_north
        )
        if not intersects_coverage:
            QMessageBox.critical(
                self,
                "Fora de la cobertura Copernicus",
                "El rectangle no intersecta la cobertura publicada del "
                "servei. Selecciona una àrea d'Europa.",
            )
            return False
        partially_outside = not (
            west >= cov_west
            and south >= cov_south
            and east <= cov_east
            and north <= cov_north
        )

        width_px = self._copernicus_estimate_value(
            estimate, "width_px"
        )
        height_px = self._copernicus_estimate_value(
            estimate, "height_px"
        )
        pixel_count = self._copernicus_estimate_value(
            estimate, "pixel_count"
        )
        raw_u16 = self._copernicus_estimate_value(
            estimate, "raw_u16_bytes"
        )
        raw_u8 = self._copernicus_estimate_value(
            estimate, "raw_u8_bytes"
        )
        compressed = self._copernicus_estimate_value(
            estimate,
            "compressed_estimate_max_bytes",
            self._copernicus_estimate_value(
                estimate, "compressed_estimate_bytes"
            ),
        )
        fragment_count = self._copernicus_estimate_value(
            estimate, "fragment_count"
        )
        if (
            width_px <= 0
            or height_px <= 0
            or pixel_count <= 0
            or raw_u8 <= 0
            or raw_u16 <= 0
            or fragment_count <= 0
        ):
            QMessageBox.critical(
                self,
                "Selecció Copernicus no vàlida",
                "No s'han pogut verificar les dimensions i els fragments "
                "de la selecció. No s'iniciarà la descàrrega.",
            )
            return False

        request_payload = (
            request.to_dict()
            if hasattr(request, "to_dict")
            and callable(request.to_dict)
            else {}
        )
        pixel_type = str(
            request_payload.get(
                "pixel_type",
                getattr(request, "pixel_type", "U8"),
            )
            or "U8"
        ).upper()
        output_format = str(
            request_payload.get(
                "output_format",
                request_payload.get(
                    "format",
                    getattr(request, "output_format", "GeoTIFF"),
                ),
            )
            or "GeoTIFF"
        )
        selected_raw = raw_u16 if pixel_type == "U16" else raw_u8
        estimated_output = compressed or selected_raw
        # ArcGIS clips native U16 samples when TIFF/U8 is requested, so the
        # robust pipeline transports U16 fragments even for a visual U8
        # output.  Account for that intermediate data, the final mosaic and
        # its overview pyramid instead of assuming two U8 copies.
        try:
            required_free = int(
                copernicus_core.CopernicusOrthophotoManager
                .required_working_space(
                    estimate,
                    pending_fragment_bytes=raw_u16,
                )
            )
        except Exception:
            final_with_overviews = int(
                math.ceil(estimated_output * 1.34)
            )
            subtotal = raw_u16 + final_with_overviews
            margin = max(
                256_000_000,
                int(math.ceil(subtotal * 0.15)),
            )
            required_free = subtotal + margin
        try:
            free_bytes = int(shutil.disk_usage(self.manager.library.root).free)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "No s'ha pogut comprovar el disc",
                str(exc),
            )
            return False
        if free_bytes < required_free:
            QMessageBox.critical(
                self,
                "Espai insuficient",
                "La selecció necessita aproximadament "
                f"{format_bytes_dual(required_free)} lliures, incloent "
                "fragments, mosaic final i marge de seguretat.\n\n"
                f"Espai disponible: {format_bytes_dual(free_bytes)}.",
            )
            return False

        size_gb = estimated_output / 1_000_000_000.0
        if size_gb < 1.0:
            severity = "Descàrrega de mida normal."
        elif size_gb < 10.0:
            severity = "Advertiment lleu: la descàrrega supera 1 GB."
        elif size_gb < 50.0:
            severity = "Advertiment important: la descàrrega supera 10 GB."
        elif size_gb <= 100.0:
            severity = (
                "Confirmació reforçada: la descàrrega supera 50 GB."
            )
        else:
            severity = (
                "La descàrrega supera 100 GB. Es recomana reduir l'àrea "
                "o utilitzar una resolució menor."
            )
        spatial_warnings = []
        if partially_outside:
            spatial_warnings.append(
                "Una part del rectangle queda fora de la cobertura "
                "publicada i pot produir NoData."
            )
        if nodata_fraction is not None and nodata_fraction > 0.50:
            spatial_warnings.append(
                "La mostra 128 × 128 estima aproximadament "
                f"{nodata_fraction * 100.0:.1f}% de mar o NoData."
            )
        if nodata_probe_error:
            spatial_warnings.append(
                "No s'ha pogut completar la mostra orientativa de mar o "
                "NoData. La descàrrega validarà igualment cada fragment."
            )
        warning_text = (
            "\n".join(spatial_warnings) + "\n\n"
            if spatial_warnings
            else ""
        )
        transport_line = (
            "Transport dels fragments: RGB U16 natiu"
            + (
                " (conversió U8 global local)\n"
                if pixel_type == "U8"
                else "\n"
            )
        )

        answer = QMessageBox.question(
            self,
            "Confirmar descàrrega Copernicus",
            f"{severity}\n\n{warning_text}"
            f"Raster: {width_px:,} × {height_px:,} píxels\n"
            f"Píxels totals: {pixel_count:,}\n"
            f"RGB U16 sense compressió: {format_bytes_dual(raw_u16)}\n"
            f"RGB U8 sense compressió: {format_bytes_dual(raw_u8)}\n"
            "Mida comprimida estimada: "
            f"{format_bytes_dual(estimated_output)}\n"
            f"{transport_line}"
            f"Fragments: {fragment_count}\n"
            f"Format: {output_format} · {pixel_type}\n"
            "Espai temporal recomanat: "
            f"{format_bytes_dual(required_free)}\n"
            f"Espai disponible: {format_bytes_dual(free_bytes)}\n\n"
            "La mida comprimida és una estimació orientativa.\n"
            "Vols iniciar la descàrrega?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No if size_gb >= 10.0 else QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return False
        if size_gb < 50.0:
            return True
        reinforced = QMessageBox.question(
            self,
            "Confirmació reforçada",
            "Aquesta tasca pot ocupar molt espai i trigar força temps. "
            "Els fragments vàlids es conservaran si la pauses.\n\n"
            "Confirma una segona vegada que vols continuar.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reinforced == QMessageBox.Yes

    def _restore_controls_after_copernicus_probe(self) -> None:
        self.btn_open_source.setEnabled(True)
        self.btn_attach.setEnabled(True)
        self.btn_attach_folder.setEnabled(True)
        self.btn_auto_download.setEnabled(self._supports_auto_download())
        self.btn_close.setEnabled(True)
        self.btn_cancel.setText(
            "Pausar descàrrega"
            if self.asset_id
            in {"orthophoto", "surface_rgb", "surface_categorical"}
            else "Cancel·lar tasca"
        )
        self.btn_cancel.setVisible(False)
        self.btn_background.setVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

    def _launch_copernicus_download(self, request: object) -> None:
        request_payload = request.to_dict()
        try:
            resolution_m = float(
                request_payload.get(
                    "resolution_m",
                    getattr(request, "resolution_m", 10.0),
                )
            )
        except (TypeError, ValueError):
            resolution_m = 10.0
        self._start_job(
            mode="download",
            files=[],
            options={
                "copernicus_request": request_payload,
                "display_name": (
                    "Copernicus HRIM 2018 True Colour · "
                    f"{resolution_m:g} m"
                ),
            },
        )

    def _on_copernicus_probe_completed(
        self,
        result: object,
        error_message: str,
        cancelled: bool,
    ) -> None:
        context = self._copernicus_probe_context
        self._copernicus_probe_context = None
        self._restore_controls_after_copernicus_probe()
        if context is None:
            return
        request, estimate = context
        if bool(cancelled):
            self.lbl_status.setText("Comprovació Copernicus cancel·lada.")
            return
        nodata_fraction = None
        if result is not None:
            try:
                value = (
                    result.get("fraction")
                    if isinstance(result, dict)
                    else getattr(result, "fraction", result)
                )
                nodata_fraction = float(value)
            except (TypeError, ValueError):
                nodata_fraction = None
        self.lbl_status.setText(
            "Comprovació espacial completada."
            if not error_message
            else "La mostra NoData no està disponible; es continuarà validant."
        )
        if self._confirm_copernicus_download(
            request,
            estimate,
            nodata_fraction=nodata_fraction,
            nodata_probe_error=str(error_message or ""),
        ):
            self._launch_copernicus_download(request)

    def _start_copernicus_preflight(
        self,
        request: object,
        estimate: object,
    ) -> None:
        current = self._copernicus_probe_worker
        if current is not None and not current._finished:
            return
        worker = _CopernicusNodataProbeWorker(request)
        worker.completed.connect(self._on_copernicus_probe_completed)
        self._copernicus_probe_worker = worker
        self._copernicus_probe_context = (request, estimate)
        self._set_running_controls(allow_background=False)
        self.btn_cancel.setText("Cancel·lar comprovació")
        self.progress.setRange(0, 0)
        self.lbl_status.setText(
            "Comprovant cobertura real de mar i NoData en segon pla…"
        )
        worker.run()

    def _auto_download_copernicus_orthophoto(self) -> None:
        from TerraLab.data.copernicus import (
            DownloadRequest,
            estimate_selection,
        )
        from TerraLab.ui.copernicus_orthophoto_dialog import (
            CopernicusOrthophotoSelectionDialog,
        )

        initial_request = None
        try:
            previous = self.manager.library.asset_state(
                "orthophoto"
            ).get("copernicus_request")
            if isinstance(previous, dict):
                initial_request = DownloadRequest.from_dict(previous)
        except Exception:
            initial_request = None
        dialog = CopernicusOrthophotoSelectionDialog(
            self,
            initial_request=initial_request,
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        request = dialog.download_request
        if request is None:
            QMessageBox.warning(
                self,
                "Selecció necessària",
                "Dibuixa una àrea vàlida abans de descarregar.",
            )
            return
        estimate = getattr(dialog, "_estimate", None)
        if estimate is None:
            try:
                estimate = estimate_selection(request)
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "No s'ha pogut estimar la selecció",
                    str(exc),
                )
                return
        self._start_copernicus_preflight(request, estimate)

    def _auto_download(self):
        if self.asset_id == "gaia_catalog":
            state = self._load_gaia_tap_state()
            pending = isinstance(state, dict) and str(
                state.get("status", "")
            ).lower() not in {"done", "completed", "success"}
            if pending:
                pct = self._state_progress_percent(state)
                msg = (
                    "S'ha detectat una descarrega Gaia pendent.\n"
                    f"Progres guardat: {pct:.1f}%.\n\n"
                    "Vols reprendre-la ara?"
                )
                ans = QMessageBox.question(
                    self,
                    "TerraLab",
                    msg,
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if ans == QMessageBox.Yes:
                    self._start_gaia_tap_process(resume=True)
                    return
            self._start_gaia_tap_process(resume=False)
            return
        if self.asset_id == "orthophoto":
            self._auto_download_copernicus_orthophoto()
            return
        if not self.spec.auto_download_url:
            return
        if self.asset_id in {"surface_rgb", "surface_categorical"}:
            partial = self.manager.partial_download(self.asset_id)
            resume_line = ""
            if partial is not None and partial.resumable:
                resume_line = (
                    "\n\nEs reprendrà el fitxer parcial existent "
                    f"({partial.downloaded_bytes / 1024**3:.2f} GiB)."
                )
            expected = self.spec.expected_download_bytes
            extracted = self.spec.expected_extracted_bytes
            answer = QMessageBox.question(
                self,
                "Descàrrega S2GLC europea",
                f"Es descarregarà el ZIP oficial de {self.spec.title}.\n\n"
                f"ZIP: {expected / 1024**3:.2f} GiB\n"
                f"GeoTIFF extret: {extracted / 1024**3:.2f} GiB\n"
                "TerraLab comprovarà l'espai per als dos fitxers i un marge, "
                "validarà el ZIP i no activarà la capa fins que el GeoTIFF "
                "s'hagi obert correctament."
                f"{resume_line}\n\nVols continuar?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        if self.asset_id == "elevation_dem":
            answer = QMessageBox.question(
                self,
                "Descàrrega EU-DEM molt gran",
                "El mosaic complet EU-DEM ocupa aproximadament 19,6 GB abans de l'extracció. "
                "TerraLab comprovarà l'espai, permetrà reprendre la descàrrega i no l'activarà "
                "fins que s'hagi verificat.\n\nVols continuar?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._start_job(
            mode="download", files=[], options=self._collect_options()
        )

    def start_gaia_tap_resume(self) -> None:
        """Executa el metode start_gaia_tap_resume de la classe AssetOnboardingDialog.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        self._start_gaia_tap_process(resume=True)

    def _start_gaia_tap_process(
        self, *, resume: bool = False, mag_limit: Optional[float] = None
    ) -> None:
        resolved_mag_limit = mag_limit
        if not resume:
            if resolved_mag_limit is None:
                mag_default = 0.0
                try:
                    mag_default = float(
                        self.manager.layout.get("gaia_mag_limit_default", 0.0)
                    )
                except Exception:
                    log_suppressed_exception(__name__, "AssetOnboardingDialog._start_gaia_tap_process")
                mag_dialog = QInputDialog(self)
                mag_dialog.setWindowTitle("Gaia TAP")
                mag_dialog.setLabelText("Magnitud maxima G (0 = sense limit):")
                mag_dialog.setInputMode(QInputDialog.DoubleInput)
                mag_dialog.setDoubleRange(0.0, 23.0)
                mag_dialog.setDoubleDecimals(2)
                mag_dialog.setDoubleValue(float(mag_default))
                mag_dialog.setStyleSheet(_ASTRO_DIALOG_STYLE)
                if mag_dialog.exec_() != QDialog.Accepted:
                    return
                resolved_mag_limit = float(mag_dialog.doubleValue())
            else:
                resolved_mag_limit = float(resolved_mag_limit)

        project_root = Path(__file__).resolve().parents[1]
        tile_script_path = project_root / "tools" / "download_gaia_tiles.py"
        legacy_script_path = project_root / "tools" / "download_gaia_tap.py"
        use_tiles_flow = tile_script_path.exists()
        script_path = tile_script_path if use_tiles_flow else legacy_script_path
        if not script_path.exists():
            QMessageBox.critical(
                self, "TerraLab", f"No s'ha trobat l'script: {script_path}"
            )
            return
        self._gaia_download_mode = "tiles" if use_tiles_flow else "tap"

        if self._gaia_tap_process is not None:
            QMessageBox.information(
                self, "TerraLab", "Ja hi ha un proces Gaia TAP en execucio."
            )
            return

        log_path = self._resolve_gaia_tap_log_path()
        state_path = self._resolve_gaia_tap_state_path()
        self._gaia_tap_log_path = log_path
        self._gaia_tap_state_path = state_path
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if log_path.exists():
                log_path.unlink()
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._start_gaia_tap_process")

        process = QProcess(self)
        process.setWorkingDirectory(str(project_root))
        process.setProgram(str(sys.executable))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("TERRALAB_DATA_ROOT", str(self.manager.library.root))
        process.setProcessEnvironment(env)
        args = ["-u", str(script_path)]
        max_parallel_requests = self._gaia_max_parallel_requests()
        if use_tiles_flow:
            try:
                tile_size_deg = float(
                    get_config_value("gaia_tile_size_deg", 10.0)
                )
            except Exception:
                tile_size_deg = 10.0
            tile_size_deg = float(max(2.0, min(30.0, tile_size_deg)))
            try:
                output_dir = Path(
                    str(self.manager.layout.get("data_gaia", project_root))
                ).expanduser()
            except Exception:
                output_dir = project_root
            args.extend(
                [
                    "--output-dir",
                    str(output_dir),
                    "--state-file",
                    str(state_path),
                    "--max-concurrent-requests",
                    str(max_parallel_requests),
                    "--tile-size-deg",
                    f"{tile_size_deg:.2f}",
                ]
            )
            if resume:
                pass
            else:
                args.append("--no-resume")
                args.extend(
                    ["--mag-limit", f"{float(resolved_mag_limit):.2f}"]
                )
        else:
            args.extend(
                [
                    "--yes",
                    "--state-file",
                    str(state_path),
                    "--log-file",
                    str(log_path),
                ]
            )
            if resume:
                args.append("--resume")
            else:
                args.extend(
                    ["--mag-limit", f"{float(resolved_mag_limit):.2f}"]
                )
        process.setArguments(args)
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._on_gaia_tap_output)
        process.finished.connect(self._on_gaia_tap_finished)
        process.errorOccurred.connect(self._on_gaia_tap_error)

        self._set_running_controls(allow_background=True)
        if resume:
            resume_state = self._load_gaia_tap_state()
            resume_pct = self._state_progress_percent(resume_state)
            if resume_pct > 0.0:
                self.progress.setRange(0, 100)
                self.progress.setValue(
                    max(0, min(100, int(round(resume_pct))))
                )
            else:
                self.progress.setRange(0, 0)
        else:
            self.progress.setRange(
                0, 0
            )  # indeterminate while waiting first progress markers
        self.lbl_status.setText(
            getTraduction(
                "Onboarding.DownloadingStars", "Descarregant estrelles..."
            )
        )
        self.txt_process_log.clear()
        self.txt_process_log.setVisible(True)
        self._append_gaia_tap_log_line(f"[gaia-tap] log file: {log_path}")
        self._append_gaia_tap_log_line(f"[gaia-tap] state file: {state_path}")
        if use_tiles_flow:
            self._append_gaia_tap_log_line(
                "[gaia-tap] mode=tiles "
                f"max_concurrent_requests={max_parallel_requests}"
            )
        self._gaia_tap_out_buffer = ""
        self._gaia_tap_process = process
        # Startup completion/failure is delivered by QProcess signals above.
        # Waiting here can freeze the entire UI for up to five seconds.
        process.start()

    def _resolve_gaia_tap_log_path(self) -> Path:
        try:
            root = Path(self.manager.layout.get("root", Path.home())).resolve()
        except Exception:
            root = Path.home()
        return root / "logs" / "gaia_tiles_last.log"

    def _resolve_gaia_tap_state_path(self) -> Path:
        try:
            root = Path(self.manager.layout.get("root", Path.home())).resolve()
        except Exception:
            root = Path.home()
        return root / "logs" / "gaia_tiles_state.json"

    def _resolve_legacy_gaia_tap_state_path(self) -> Path:
        """Retorna la ruta legacy del fitxer d'estat Gaia TAP."""
        try:
            root = Path(self.manager.layout.get("root", Path.home())).resolve()
        except Exception:
            root = Path.home()
        return root / "logs" / "gaia_tap_state.json"

    def _resolve_gaia_tiles_runtime_state_path(self) -> Path:
        """Retorna la ruta runtime de l'estat per teseles (`data_gaia`)."""
        try:
            runtime_gaia_dir = Path(
                str(self.manager.layout.get("data_gaia", ""))
            ).expanduser()
        except Exception:
            runtime_gaia_dir = Path()
        return runtime_gaia_dir / "gaia_tiles_state.json"

    def _load_json_state_file(self, path: Path) -> Optional[dict]:
        """Carrega un fitxer d'estat JSON si existeix i és vàlid."""
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                payload["_state_path"] = str(path)
                return payload
        except Exception:
            return None
        return None

    def _load_gaia_tap_state(self) -> Optional[dict]:
        tile_state = self._load_json_state_file(self._resolve_gaia_tap_state_path())
        runtime_tile_state = self._load_json_state_file(
            self._resolve_gaia_tiles_runtime_state_path()
        )
        legacy_state = self._load_json_state_file(
            self._resolve_legacy_gaia_tap_state_path()
        )
        if isinstance(tile_state, dict):
            tile_status = str(tile_state.get("status", "")).lower()
            if tile_status not in {"done", "completed", "success"}:
                return tile_state
        if isinstance(runtime_tile_state, dict):
            runtime_status = str(runtime_tile_state.get("status", "")).lower()
            if runtime_status not in {"done", "completed", "success"}:
                return runtime_tile_state
        if isinstance(legacy_state, dict):
            legacy_status = str(legacy_state.get("status", "")).lower()
            if legacy_status not in {"done", "completed", "success"}:
                return legacy_state
        if isinstance(tile_state, dict):
            return tile_state
        if isinstance(runtime_tile_state, dict):
            return runtime_tile_state
        if isinstance(legacy_state, dict):
            return legacy_state
        return None

    def _state_progress_percent(self, state: Optional[dict]) -> float:
        """Calcula percentatge de progrés aproximat per estat Gaia."""
        if not isinstance(state, dict):
            return 0.0
        try:
            if "progress_percent" in state:
                return float(max(0.0, min(100.0, float(state.get("progress_percent", 0.0) or 0.0))))
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._state_progress_percent")
        deep_tiles = state.get("deep_tiles")
        if not isinstance(deep_tiles, dict):
            return 0.0
        try:
            tile_size = float(state.get("tile_size_deg", 5.0) or 5.0)
        except Exception:
            tile_size = 5.0
        tile_size = max(0.1, float(tile_size))
        total_tiles = int((360.0 / tile_size) * (180.0 / tile_size))
        done_tiles = sum(
            1
            for item in deep_tiles.values()
            if isinstance(item, dict) and bool(item.get("done", False))
        )
        pct = 10.0 + 85.0 * (float(done_tiles) / float(max(1, total_tiles)))
        if bool(state.get("general_tile_done", False)):
            pct = max(pct, 9.0)
        if str(state.get("status", "")).lower() in {"done", "completed", "success"}:
            pct = 100.0
        return float(max(0.0, min(100.0, pct)))

    def _gaia_state_is_pending(self, state: Optional[dict]) -> bool:
        """Retorna `True` si l'estat Gaia indica descarrega pendent."""
        if not isinstance(state, dict):
            return False
        status_name = str(state.get("status", "")).strip().lower()
        phase_name = str(state.get("phase", "")).strip().lower()
        done_tokens = {"done", "completed", "success"}
        return status_name not in done_tokens and phase_name not in done_tokens

    def _gaia_state_status_text(self, state: dict, percent_value: float) -> str:
        """Construeix text breu de progrés per estat Gaia."""
        status_message = str(state.get("status_message", "") or "").strip()
        tile_id = str(state.get("current_tile_id", "") or "").strip()
        if not status_message:
            status_message = "Descarregant Gaia per teseles"
        if tile_id:
            status_message = f"{status_message} [{tile_id}]"
        return f"{status_message} ({percent_value:.1f}%)"

    def _start_gaia_state_watch_timer(self) -> None:
        """Activa polling periòdic de l'estat Gaia per refrescar el diàleg."""
        if self.asset_id != "gaia_catalog":
            return
        if self._gaia_state_watch_timer is not None:
            return
        timer = QTimer(self)
        timer.setInterval(900)
        timer.timeout.connect(self._poll_gaia_state_feedback)
        self._gaia_state_watch_timer = timer
        timer.start()
        QTimer.singleShot(120, self._poll_gaia_state_feedback)

    def _stop_gaia_state_watch_timer(self) -> None:
        """Atura el polling d'estat Gaia si està actiu."""
        timer = self._gaia_state_watch_timer
        if timer is None:
            return
        try:
            timer.stop()
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._stop_gaia_state_watch_timer")

    def _poll_gaia_state_feedback(self) -> None:
        """Actualitza progrés del diàleg des de fitxer d'estat Gaia."""
        if self.asset_id != "gaia_catalog":
            return
        state = self._load_gaia_tap_state()
        if not self._gaia_state_is_pending(state):
            return
        assert isinstance(state, dict)

        progress_percent = self._state_progress_percent(state)
        status_text = self._gaia_state_status_text(state, progress_percent)
        signature = f"{status_text}|{int(round(progress_percent))}"
        if signature != self._gaia_last_polled_state_signature:
            self._gaia_last_polled_state_signature = signature
            if not bool(self.txt_process_log.isVisible()):
                self.txt_process_log.setVisible(True)
            self.txt_process_log.append(f"[gaia-state] {status_text}")

        if progress_percent <= 0.0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(
                max(0, min(100, int(round(progress_percent))))
            )
        self.lbl_status.setText(status_text)

    def _gaia_max_parallel_requests(self) -> int:
        """Retorna concurrencia TAP per al flux Gaia per teseles."""
        raw_candidates = []
        try:
            raw_candidates.append(
                self.manager.layout.get("gaia_max_concurrent_requests")
            )
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._gaia_max_parallel_requests")
        try:
            raw_candidates.append(
                get_config_value("gaia_max_concurrent_requests", 2)
            )
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._gaia_max_parallel_requests")
        raw_candidates.append(2)

        for raw_value in raw_candidates:
            try:
                parsed_value = int(raw_value)
                if parsed_value > 0:
                    return int(max(1, min(8, parsed_value)))
            except Exception:
                continue
        return 2

    def _gaia_tap_log_hint(self) -> str:
        path = self._gaia_tap_log_path
        if path is None:
            return "No s'ha pogut determinar la ruta del log."
        return f"Log: {path}"

    def _cleanup_gaia_tap_process(self) -> None:
        proc = self._gaia_tap_process
        if proc is None:
            return
        for key, stored_proc in tuple(_GAIA_BACKGROUND_DOWNLOADS.items()):
            if stored_proc is proc:
                _GAIA_BACKGROUND_DOWNLOADS.pop(key, None)
        try:
            _GAIA_BACKGROUND_PROCESSES.remove(proc)
        except ValueError:
            pass
        try:
            proc.readyReadStandardOutput.disconnect(self._on_gaia_tap_output)
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._cleanup_gaia_tap_process")
        try:
            proc.finished.disconnect(self._on_gaia_tap_finished)
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._cleanup_gaia_tap_process")
        try:
            proc.errorOccurred.disconnect(self._on_gaia_tap_error)
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._cleanup_gaia_tap_process")
        try:
            proc.deleteLater()
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._cleanup_gaia_tap_process")
        self._gaia_tap_process = None
        self._gaia_tap_out_buffer = ""

    def _detach_gaia_tap_process_for_background(self) -> None:
        proc = self._gaia_tap_process
        if proc is None:
            return
        try:
            proc.readyReadStandardOutput.disconnect(self._on_gaia_tap_output)
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._detach_gaia_tap_process_for_background")
        try:
            proc.finished.disconnect(self._on_gaia_tap_finished)
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._detach_gaia_tap_process_for_background")
        try:
            proc.errorOccurred.disconnect(self._on_gaia_tap_error)
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._detach_gaia_tap_process_for_background")
        try:
            app = QApplication.instance()
            if app is not None:
                proc.setParent(app)
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._detach_gaia_tap_process_for_background")
        _keep_gaia_process_alive(
            proc, _background_job_key(self.manager, self.asset_id)
        )
        self._gaia_tap_process = None
        self._gaia_tap_out_buffer = ""
        self._gaia_process_detached = True

    def _maybe_close_after_visible_ready(self) -> None:
        if self.asset_id != "gaia_catalog":
            return
        if bool(self._gaia_visible_ready):
            return
        state = self._load_gaia_tap_state()
        if not isinstance(state, dict):
            return
        # Flux nou per teseles: tancar quan la tesela general ja existeix.
        if "general_tile_done" in state or "deep_tiles" in state:
            if not bool(state.get("general_tile_done", False)):
                return
            output_dir_raw = str(
                state.get("output_dir", "")
                or self.manager.layout.get("data_gaia", "")
            ).strip()
            if not output_dir_raw:
                return
            output_dir = Path(output_dir_raw).expanduser()
            tile_all_path = output_dir / "tile_all.npz"
            if not tile_all_path.exists() or (not tile_all_path.is_file()):
                return
            if tile_all_path.stat().st_size <= 0:
                return
            if int(state.get("general_tile_star_count", 0) or 0) <= 0:
                return

            self._gaia_visible_ready = True
            self._completed = True
            self._detach_gaia_tap_process_for_background()
            self.progress.setRange(0, 100)
            pct_val = self._state_progress_percent(state)
            self.progress.setValue(max(0, min(100, int(round(pct_val)))))
            self.lbl_status.setText(
                "Tesela general preparada. Es continua descarregant en segon pla..."
            )
            self.accept()
            return

        # Flux legacy TAP: manté compatibilitat.
        if not bool(state.get("visible_ready", False)):
            return
        output_dir = Path(str(state.get("output_dir", "") or "")).expanduser()
        basename = (
            str(state.get("basename", "stars_catalog") or "stars_catalog").strip()
            or "stars_catalog"
        )
        candidates = (
            output_dir / f"{basename}.npy",
            output_dir / f"{basename}.npz",
            output_dir / f"{basename}.zst",
        )
        visible_catalog_path = next((p for p in candidates if p.exists() and p.is_file()), None)
        if visible_catalog_path is None or visible_catalog_path.stat().st_size <= 0:
            return

        self._gaia_visible_ready = True
        self._completed = True
        self._detach_gaia_tap_process_for_background()
        self.progress.setRange(0, 100)
        pct_val = self._state_progress_percent(state)
        self.progress.setValue(max(0, min(100, int(round(pct_val)))))
        self.lbl_status.setText(
            "Cataleg visible preparat. Es continua descarregant en segon pla..."
        )
        self.accept()

    def _append_gaia_tap_log_line(self, line: str) -> None:
        text = str(line or "").rstrip("\r\n")
        if not text:
            return
        try:
            if self._gaia_tap_log_path is not None:
                self._gaia_tap_log_path.parent.mkdir(parents=True, exist_ok=True)
                with self._gaia_tap_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(text + "\n")
        except Exception:
            log_suppressed_exception(__name__, "AssetOnboardingDialog._append_gaia_tap_log_line")
        self.txt_process_log.append(text)
        self.lbl_status.setText(text)

        m = re.search(r"\[gaia-import\]\s+([0-9]+(?:\.[0-9]+)?)%", text)
        if m:
            try:
                pct = max(0, min(100, int(round(float(m.group(1))))))
                if self.progress.maximum() == 0:
                    self.progress.setRange(0, 100)
                self.progress.setValue(pct)
            except Exception:
                log_suppressed_exception(__name__, "AssetOnboardingDialog._append_gaia_tap_log_line")

        m2 = re.search(
            r"\[gaia-tap\]\s+download\s+([0-9]+(?:\.[0-9]+)?)%", text
        )
        if m2 and self.progress.maximum() == 0:
            try:
                pct2 = max(0, min(100, int(round(float(m2.group(1)) * 0.75))))
                self.progress.setRange(0, 100)
                self.progress.setValue(pct2)
            except Exception:
                log_suppressed_exception(__name__, "AssetOnboardingDialog._append_gaia_tap_log_line")

        m3 = re.search(
            r"\[gaia-progress\]\s+([0-9]+(?:\.[0-9]+)?)%\s*(.*)$",
            text,
        )
        if m3:
            try:
                pct3 = max(0, min(100, int(round(float(m3.group(1))))))
                if self.progress.maximum() == 0:
                    self.progress.setRange(0, 100)
                self.progress.setValue(pct3)
                msg_text = str(m3.group(2) or "").strip()
                if msg_text:
                    self.lbl_status.setText(f"{msg_text} ({pct3}%)")
                else:
                    status = getTraduction(
                        "Onboarding.DownloadingStars", "Descarregant estrelles..."
                    )
                    self.lbl_status.setText(f"{status} ({pct3}%)")
            except Exception:
                log_suppressed_exception(__name__, "AssetOnboardingDialog._append_gaia_tap_log_line")

        if "[gaia-ui]" in text:
            self._maybe_close_after_visible_ready()

    def _on_gaia_tap_output(self):
        proc = self._gaia_tap_process
        if proc is None:
            return
        chunk = bytes(proc.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        if not chunk:
            return
        merged = self._gaia_tap_out_buffer + chunk
        merged = merged.replace("\r\n", "\n").replace("\r", "\n")
        lines = merged.split("\n")
        self._gaia_tap_out_buffer = lines.pop() if lines else ""
        for line in lines:
            self._append_gaia_tap_log_line(line)
        self._maybe_close_after_visible_ready()

    def _on_gaia_tap_finished(
        self, exit_code: int, exit_status: QProcess.ExitStatus
    ):
        if self._gaia_tap_out_buffer:
            self._append_gaia_tap_log_line(self._gaia_tap_out_buffer)
            self._gaia_tap_out_buffer = ""
        log_hint = self._gaia_tap_log_hint()
        state = self._load_gaia_tap_state()
        self._cleanup_gaia_tap_process()
        self.btn_cancel.setVisible(False)
        self.btn_background.setVisible(False)

        self.progress.setRange(0, 100)
        ok = (
            int(exit_code) == 0
            and exit_status == QProcess.NormalExit
            and self.manager.asset_ready("gaia_catalog")
        )
        if ok:
            self.progress.setValue(100)
            self._completed = True
            self.lbl_status.setText("Dades Gaia preparades correctament.")
            QMessageBox.information(
                self,
                "TerraLab",
                "Preparacio Gaia completada.\n\n"
                f"Les dades ja són dins la biblioteca {self.manager.library.root}.\n"
                "Si vols, ja pots esborrar el fitxer original.\n\n"
                f"{log_hint}",
            )
            self.accept()
            return

        self.btn_open_source.setEnabled(True)
        self.btn_attach.setEnabled(True)
        self.btn_attach_folder.setEnabled(True)
        self.btn_close.setEnabled(True)
        self.btn_auto_download.setEnabled(self._supports_auto_download())
        pct_hint = ""
        if isinstance(state, dict):
            try:
                pct_val = self._state_progress_percent(state)
                pct_hint = (
                    f"\nProgres guardat: {pct_val:.1f}% (es pot reprendre)."
                )
            except Exception:
                pct_hint = ""
        self.lbl_status.setText(
            "La descarrega Gaia ha finalitzat, pero no s'ha detectat un cataleg valid."
        )
        status_name = (
            "normal" if exit_status == QProcess.NormalExit else "crash"
        )
        QMessageBox.warning(
            self,
            "TerraLab",
            "El proces Gaia ha acabat, pero TerraLab no troba el cataleg Gaia preparat.\n"
            f"Exit code: {int(exit_code)} ({status_name}).\n"
            f"{log_hint}{pct_hint}",
        )

    def _on_gaia_tap_error(self, _error):
        proc = self._gaia_tap_process
        err_txt = "Error executant la descarrega Gaia."
        if proc is not None:
            try:
                err_txt = str(proc.errorString() or err_txt)
            except Exception:
                log_suppressed_exception(__name__, "AssetOnboardingDialog._on_gaia_tap_error")
        log_hint = self._gaia_tap_log_hint()
        self._cleanup_gaia_tap_process()
        self.btn_cancel.setVisible(False)
        self.btn_background.setVisible(False)
        self.progress.setRange(0, 100)
        self.btn_open_source.setEnabled(True)
        self.btn_attach.setEnabled(True)
        self.btn_attach_folder.setEnabled(True)
        self.btn_auto_download.setEnabled(self._supports_auto_download())
        self.btn_close.setEnabled(True)
        self.lbl_status.setText("Error en descarrega Gaia.")
        QMessageBox.critical(self, "TerraLab", f"{err_txt}\n\n{log_hint}")

    def _collect_options(self) -> dict:
        if self.asset_id == "milkyway_texture":
            return {
                "remove_stars": bool(self.chk_remove_stars.isChecked()),
            }
        if self.asset_id in {"surface_rgb", "surface_categorical"}:
            return {
                "remove_archive": bool(self.chk_remove_archive.isChecked()),
                "display_name": self.spec.title,
            }
        return {}

    def _start_job(
        self, mode: str, files: Iterable[str], options: Optional[dict] = None
    ):
        self._set_running_controls(allow_background=str(mode) == "download")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_status.setText("Preparant tasca en segon pla...")

        job = _start_asset_job(
            self.manager,
            mode=mode,
            asset_id=self.asset_id,
            files=files,
            options=options,
            start_immediately=False,
        )
        self._bind_asset_job(job)
        if not job.started:
            job.start()

    def _cancel_job(self) -> None:
        probe_worker = getattr(self, "_copernicus_probe_worker", None)
        if probe_worker is not None and not probe_worker._finished:
            probe_worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.lbl_status.setText(
                "Cancel·lant la comprovació Copernicus…"
            )
            return
        process = getattr(self, "_gaia_tap_process", None)
        if process is not None and process.state() != QProcess.NotRunning:
            self.btn_cancel.setEnabled(False)
            self.btn_background.setEnabled(False)
            self.lbl_status.setText(
                "Cancel·lant Gaia… El progrés i les descàrregues parcials es conservaran."
            )
            process.terminate()

            def force_stop(proc=process) -> None:
                if proc.state() != QProcess.NotRunning:
                    proc.kill()

            QTimer.singleShot(3000, force_stop)
            return
        job = getattr(self, "_job", None)
        if job is None:
            return
        job.cancel()
        self.btn_cancel.setEnabled(False)
        self.btn_background.setEnabled(False)
        self.lbl_status.setText(
            "Cancel·lant… La descàrrega parcial es conservarà per reprendre-la."
        )

    def _on_progress(self, percent: float, message: str):
        pct = float(percent)
        if pct < 0.0:
            self.progress.setRange(0, 0)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 100)
            value = max(0, min(100, int(round(pct))))
            self.progress.setValue(value)
        self.lbl_status.setText(str(message))

    def _on_completed(self, result: object):
        self._disconnect_asset_job()
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._completed = True
        self.btn_cancel.setVisible(False)
        self.btn_background.setVisible(False)
        self.lbl_status.setText("Dades preparades correctament.")
        self.btn_close.setEnabled(True)
        extra = ""
        if isinstance(result, dict) and self.asset_id == "elevation_dem":
            observer_auto = result.get("observer_auto", {})
            if isinstance(observer_auto, dict) and bool(
                observer_auto.get("applied", False)
            ):
                try:
                    lat = float(observer_auto.get("lat"))
                    lon = float(observer_auto.get("lon"))
                    tz = str(observer_auto.get("timezone", "") or "").strip()
                    tz_suffix = f" ({tz})" if tz else ""
                    extra = (
                        "\n\nUbicacio detectada automaticament del DEM:\n"
                        f"lat={lat:.6f}, lon={lon:.6f}{tz_suffix}"
                    )
                except Exception:
                    extra = ""
        msg = (
            "Importacio completada.\n\n"
            f"Els derivats o la còpia administrada són a {self.manager.library.root}.\n"
            "Les fonts enllaçades continuen a la seva ubicació original."
            + extra
        )
        QMessageBox.information(self, "TerraLab", msg)
        self.accept()

    def _on_failed(self, error_message: str):
        self._disconnect_asset_job()
        self.btn_open_source.setEnabled(True)
        self.btn_attach.setEnabled(True)
        self.btn_attach_folder.setEnabled(True)
        self.btn_auto_download.setEnabled(self._supports_auto_download())
        self.btn_close.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self.btn_background.setVisible(False)
        self.progress.setRange(0, 100)
        if any(
            token in str(error_message).lower()
            for token in ("cancel", "paus")
        ):
            self.lbl_status.setText("Tasca pausada; es podrà reprendre.")
            self.btn_auto_download.setText("Reprendre descàrrega")
            QMessageBox.information(self, "TerraLab", str(error_message))
        else:
            self.lbl_status.setText("Error durant la preparacio de dades.")
            message_box = QMessageBox(self)
            message_box.setIcon(QMessageBox.Critical)
            message_box.setWindowTitle("TerraLab")
            message_box.setTextFormat(Qt.PlainText)
            message_box.setText(str(error_message))
            message_box.setStandardButtons(QMessageBox.Ok)
            message_box.exec_()

    def reject(self):
        """Executa el metode reject de la classe AssetOnboardingDialog.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        probe_worker = getattr(self, "_copernicus_probe_worker", None)
        if probe_worker is not None and not probe_worker._finished:
            probe_worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.lbl_status.setText(
                "Cancel·lant la comprovació Copernicus…"
            )
            return
        process = self._gaia_tap_process
        job = self._job
        if (
            process is not None
            and process.state() != QProcess.NotRunning
        ) or (job is not None and bool(job.running)):
            self._continue_in_background()
            return
        self._stop_gaia_state_watch_timer()
        proc = self._gaia_tap_process
        if proc is not None:
            try:
                if proc.state() != QProcess.NotRunning:
                    proc.terminate()
                    QTimer.singleShot(
                        2_000,
                        lambda process=proc: (
                            process.kill()
                            if process.state() != QProcess.NotRunning
                            else None
                        ),
                    )
            except Exception:
                log_suppressed_exception(__name__, "AssetOnboardingDialog.reject")
            self._cleanup_gaia_tap_process()
        super().reject()


class WelcomeOnboardingDialog(QDialog):
    """First-run product onboarding with optional per-asset setup."""

    def __init__(
        self, manager: AssetManager, parent=None, *, mandatory: bool = False
    ):
        super().__init__(parent)
        self.manager = manager
        self._mandatory = bool(mandatory)
        self._asset_status_labels: Dict[str, QLabel] = {}
        self._asset_run_buttons: Dict[str, QPushButton] = {}

        self.setWindowTitle("TerraLab - Benvinguda")
        self.setModal(True)
        self.resize(920, 620)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        if self._mandatory:
            self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setStyleSheet(_ASTRO_DIALOG_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        self.pages = QStackedWidget()
        root.addWidget(self.pages)

        self.pages.addWidget(
            self._build_text_page(
                "Benvingut a TerraLab",
                "TerraLab es una plataforma visual astronomica i geoespacial per explorar el cel, "
                "simular condicions reals d'observacio i treballar amb capes cientifiques verificables.",
            )
        )
        self.pages.addWidget(
            self._build_text_page(
                "Funcionalitats clau",
                "1) Cel fisic en temps real o simulat.\n"
                "2) Topografia i horitzo amb dades reals.\n"
                "3) Catalegs i capes cientifiques (Gaia, NGC, Planck, Via Lactia).\n"
                "4) Fluxos guiats d'importacio de dades amb processament en segon pla.",
            )
        )
        self._data_page_index = self.pages.count()
        self.pages.addWidget(self._build_data_page())
        self.pages.addWidget(
            self._build_text_page(
                "Preparat per comencar",
                "Ja tens l'assistent de dades disponible per capes i el podras reobrir quan vulguis.\n"
                "Si has omes algun pas ara, TerraLab t'ho tornara a demanar quan activis la capa corresponent.",
            )
        )

        nav = QHBoxLayout()
        self.btn_prev = QPushButton("Anterior")
        self.btn_prev.clicked.connect(self._prev)
        self.btn_skip_data = QPushButton("Ometre dades ara")
        self.btn_skip_data.clicked.connect(self._skip_data_step)
        self.btn_next = QPushButton("Seguent")
        self.btn_next.clicked.connect(self._next)
        nav.addWidget(self.btn_prev)
        nav.addWidget(self.btn_skip_data)
        nav.addStretch(1)
        nav.addWidget(self.btn_next)
        root.addLayout(nav)

        self._refresh_asset_statuses()
        self._refresh_nav()

    def _build_text_page(self, title_text: str, body_text: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(10)
        title = QLabel(title_text)
        title.setObjectName("titleLabel")
        body = QLabel(body_text)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        panel_layout.addWidget(title)
        panel_layout.addWidget(body, 1)
        layout.addWidget(panel, 1)
        return page

    def _build_data_page(self) -> QWidget:
        from TerraLab.data.layer_manager import LayerManager
        from TerraLab.ui.layer_configurator import LayerConfiguratorWidget

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(10)

        title = QLabel("Preparacio de dades")
        title.setObjectName("titleLabel")
        subtitle = QLabel(
            "Configura la visibilitat, les fonts pròpies i les descàrregues de les nou capes. "
            "També trobaràs aquest mateix gestor durant l'ús normal."
        )
        subtitle.setWordWrap(True)
        panel_layout.addWidget(title)
        panel_layout.addWidget(subtitle)

        self.layer_configurator = LayerConfiguratorWidget(
            LayerManager(self.manager),
            panel,
            asset_dialog_factory=AssetOnboardingDialog,
        )
        panel_layout.addWidget(self.layer_configurator, 1)
        layout.addWidget(panel, 1)
        return page

    def _refresh_asset_statuses(self) -> None:
        configurator = getattr(self, "layer_configurator", None)
        if configurator is not None:
            configurator.refresh()
        for asset_id, label in self._asset_status_labels.items():
            ready = bool(self.manager.asset_ready(asset_id))
            label.setText("Preparat" if ready else "Pendent")
            label.setObjectName(
                "assetStatusOk" if ready else "assetStatusMissing"
            )
            label.style().unpolish(label)
            label.style().polish(label)

    def _run_asset_wizard(self, asset_id: str) -> None:
        dlg = AssetOnboardingDialog(self.manager, asset_id, self)
        ok = dlg.exec_() == QDialog.Accepted and bool(
            getattr(dlg, "completed", False)
        )
        if ok:
            self._refresh_asset_statuses()

    def _refresh_nav(self):
        idx = self.pages.currentIndex()
        last = self.pages.count() - 1
        self.btn_prev.setEnabled(idx > 0)
        self.btn_skip_data.setVisible(idx == self._data_page_index)
        if idx >= last:
            self.btn_next.setText("Finalitzar")
        else:
            self.btn_next.setText("Seguent")

    def _prev(self):
        idx = self.pages.currentIndex()
        if idx > 0:
            self.pages.setCurrentIndex(idx - 1)
            self._refresh_nav()

    def _skip_data_step(self):
        if self.pages.currentIndex() != self._data_page_index:
            return
        self.pages.setCurrentIndex(
            min(self.pages.count() - 1, self._data_page_index + 1)
        )
        self._refresh_nav()

    def _next(self):
        idx = self.pages.currentIndex()
        last = self.pages.count() - 1
        if idx < last:
            self.pages.setCurrentIndex(idx + 1)
            self._refresh_nav()
            return
        set_config_value("ui_onboarding_done", True)
        self.accept()

    def reject(self):
        """Executa el metode reject de la classe WelcomeOnboardingDialog.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        if self._mandatory:
            return
        super().reject()
