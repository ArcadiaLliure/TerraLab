"""Onboarding dialogs: first-run welcome and per-layer asset assistant."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional

import numpy as np
from PyQt5.QtCore import (
    QObject,
    QProcess,
    QProcessEnvironment,
    Qt,
    QTimer,
    QThread,
    QUrl,
    pyqtSignal,
    pyqtSlot,
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

from TerraLab.common.utils import (
    getTraduction,
    get_config_value,
    set_config_value,
)
from TerraLab.data.assets_manager import AssetManager

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


_GAIA_BACKGROUND_PROCESSES = []


def _keep_gaia_process_alive(proc: QProcess) -> None:
    if proc is None:
        return
    if proc in _GAIA_BACKGROUND_PROCESSES:
        return
    _GAIA_BACKGROUND_PROCESSES.append(proc)

    def _cleanup(*_args):
        try:
            _GAIA_BACKGROUND_PROCESSES.remove(proc)
        except Exception:
            pass
        try:
            proc.deleteLater()
        except Exception:
            pass

    try:
        proc.finished.connect(_cleanup)
    except Exception:
        pass


class _AssetJobWorker(QObject):
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
    ):
        super().__init__()
        self.manager = manager
        self.mode = str(mode)
        self.asset_id = str(asset_id)
        self.files = list(files or [])
        self.options = dict(options or {})

    @pyqtSlot()
    def run(self):
        """Executa el metode run de la classe _AssetJobWorker.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """

        def _cb(percent: float, message: str):
            self.progress.emit(float(percent), str(message))

        try:
            if self.mode == "download":
                result = self.manager.download_and_prepare(
                    self.asset_id,
                    progress_callback=_cb,
                    options=self.options,
                )
            else:
                result = self.manager.import_files(
                    self.asset_id,
                    self.files,
                    progress_callback=_cb,
                    options=self.options,
                )
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class AssetOnboardingDialog(QDialog):
    """Mini-onboarding shown when a layer is enabled but required data is missing."""

    def __init__(self, manager: AssetManager, asset_id: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.asset_id = str(asset_id)
        self.spec = self.manager.get_spec(self.asset_id)
        self._completed = False
        self._thread = None
        self._worker = None
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
                    "\nLog persistent: %APPDATA%/TerraLab/logs/gaia_tiles_last.log\n"
                )
            extra_help = (
                "\n\nEl boto 'Descarregar automaticament' executa el flux Gaia per teseles en segon pla.\n"
                "Flux: primer crea la tesela general (mag < 8), i despres completa teseles profundes "
                "fins a la magnitud objectiu, amb progressio reanudable.\n"
                f"{gaia_log_line}"
                "Opcional (avancat): pots executar-ho manualment amb:\n"
                "python tools/download_gaia_tiles.py --mag-limit 0 --tile-size-deg 10"
            )
        details.setText(
            f"Font oficial:\n{self.spec.source_url}\n\n"
            f"Formats admesos:\n{self.spec.accepted_formats}\n\n"
            f"Credits:\n{self.spec.credits}\n\n"
            "Despres d'importar les dades, TerraLab les copia a la carpeta de l'app (%APPDATA%/TerraLab).\n"
            "Pots esborrar el fitxer original si vols."
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
        self.btn_auto_download.clicked.connect(self._auto_download)
        actions.addWidget(self.btn_auto_download)

        self.btn_attach = QPushButton(
            getTraduction("Onboarding.AttachFiles", "Adjuntar fitxer(s)")
        )
        self.btn_attach.clicked.connect(self._attach_files)
        actions.addWidget(self.btn_attach)
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

    def _supports_auto_download(self) -> bool:
        if self.asset_id == "gaia_catalog":
            return True
        return bool(self.spec.auto_download_url)

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
            "Data files (*.fits *.png *.ecsv *.csv *.zst *.zip *.tif *.tiff *.asc *.txt);;"
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
        if not self.spec.auto_download_url:
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
                    pass
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
            pass

        process = QProcess(self)
        process.setWorkingDirectory(str(project_root))
        process.setProgram(str(sys.executable))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
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

        self.btn_open_source.setEnabled(False)
        self.btn_attach.setEnabled(False)
        self.btn_auto_download.setEnabled(False)
        self.btn_close.setEnabled(False)
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
        process.start()
        if not process.waitForStarted(5000):
            err_txt = str(
                process.errorString()
                or "No es pot iniciar el proces Gaia TAP."
            )
            self._cleanup_gaia_tap_process()
            self.progress.setRange(0, 100)
            self.lbl_status.setText("Error en iniciar Gaia TAP.")
            self.btn_open_source.setEnabled(True)
            self.btn_attach.setEnabled(True)
            self.btn_auto_download.setEnabled(self._supports_auto_download())
            self.btn_close.setEnabled(True)
            QMessageBox.critical(
                self, "TerraLab", f"{err_txt}\n\n{self._gaia_tap_log_hint()}"
            )
            return

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
            pass
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
            pass

    def _poll_gaia_state_feedback(self) -> None:
        """Actualitza progrés del diàleg des de fitxer d'estat Gaia."""
        if self.asset_id != "gaia_catalog":
            return
        if self._gaia_tap_process is not None:
            # Quan el procés llançat per aquest diàleg està viu, el progrés ja arriba per stdout.
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
            pass
        try:
            raw_candidates.append(
                get_config_value("gaia_max_concurrent_requests", 2)
            )
        except Exception:
            pass
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
        try:
            proc.readyReadStandardOutput.disconnect(self._on_gaia_tap_output)
        except Exception:
            pass
        try:
            proc.finished.disconnect(self._on_gaia_tap_finished)
        except Exception:
            pass
        try:
            proc.errorOccurred.disconnect(self._on_gaia_tap_error)
        except Exception:
            pass
        try:
            proc.deleteLater()
        except Exception:
            pass
        self._gaia_tap_process = None
        self._gaia_tap_out_buffer = ""

    def _detach_gaia_tap_process_for_background(self) -> None:
        proc = self._gaia_tap_process
        if proc is None:
            return
        try:
            proc.readyReadStandardOutput.disconnect(self._on_gaia_tap_output)
        except Exception:
            pass
        try:
            proc.finished.disconnect(self._on_gaia_tap_finished)
        except Exception:
            pass
        try:
            proc.errorOccurred.disconnect(self._on_gaia_tap_error)
        except Exception:
            pass
        try:
            app = QApplication.instance()
            if app is not None:
                proc.setParent(app)
        except Exception:
            pass
        _keep_gaia_process_alive(proc)
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
            try:
                with np.load(tile_all_path, allow_pickle=False) as tile_npz:
                    if "ra" in tile_npz:
                        row_count = int(len(tile_npz["ra"]))
                    elif "RA" in tile_npz:
                        row_count = int(len(tile_npz["RA"]))
                    else:
                        row_count = 0
                if row_count <= 0:
                    return
            except Exception:
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
            pass
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
                pass

        m2 = re.search(
            r"\[gaia-tap\]\s+download\s+([0-9]+(?:\.[0-9]+)?)%", text
        )
        if m2 and self.progress.maximum() == 0:
            try:
                pct2 = max(0, min(100, int(round(float(m2.group(1)) * 0.75))))
                self.progress.setRange(0, 100)
                self.progress.setValue(pct2)
            except Exception:
                pass

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
                pass

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
                "Les dades ja son dins la carpeta de TerraLab.\n"
                "Si vols, ja pots esborrar el fitxer original.\n\n"
                f"{log_hint}",
            )
            self.accept()
            return

        self.btn_open_source.setEnabled(True)
        self.btn_attach.setEnabled(True)
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
                pass
        log_hint = self._gaia_tap_log_hint()
        self._cleanup_gaia_tap_process()
        self.progress.setRange(0, 100)
        self.btn_open_source.setEnabled(True)
        self.btn_attach.setEnabled(True)
        self.btn_auto_download.setEnabled(self._supports_auto_download())
        self.btn_close.setEnabled(True)
        self.lbl_status.setText("Error en descarrega Gaia.")
        QMessageBox.critical(self, "TerraLab", f"{err_txt}\n\n{log_hint}")

    def _collect_options(self) -> dict:
        if self.asset_id == "milkyway_texture":
            return {
                "remove_stars": bool(self.chk_remove_stars.isChecked()),
            }
        return {}

    def _start_job(
        self, mode: str, files: Iterable[str], options: Optional[dict] = None
    ):
        self.btn_open_source.setEnabled(False)
        self.btn_attach.setEnabled(False)
        self.btn_auto_download.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_status.setText("Preparant tasca en segon pla...")

        thread = QThread(self)
        worker = _AssetJobWorker(
            self.manager,
            mode=mode,
            asset_id=self.asset_id,
            files=files,
            options=options,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

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
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._completed = True
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
            "Les dades ja son dins la carpeta de TerraLab.\n"
            "Si vols, ja pots esborrar el fitxer original que has adjuntat."
            + extra
        )
        QMessageBox.information(self, "TerraLab", msg)
        self.accept()

    def _on_failed(self, error_message: str):
        self.btn_open_source.setEnabled(True)
        self.btn_attach.setEnabled(True)
        self.btn_auto_download.setEnabled(self._supports_auto_download())
        self.btn_close.setEnabled(True)
        self.progress.setRange(0, 100)
        self.lbl_status.setText("Error durant la preparacio de dades.")
        QMessageBox.critical(self, "TerraLab", str(error_message))

    def reject(self):
        """Executa el metode reject de la classe AssetOnboardingDialog.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        self._stop_gaia_state_watch_timer()
        proc = self._gaia_tap_process
        if proc is not None:
            try:
                if proc.state() != QProcess.NotRunning:
                    proc.terminate()
                    if not proc.waitForFinished(2000):
                        proc.kill()
                        proc.waitForFinished(2000)
            except Exception:
                pass
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
            "Aquest pas replica els mini-assistents de cada capa. "
            "Pots preparar-ho ara o ometre-ho i fer-ho mes tard."
        )
        subtitle.setWordWrap(True)
        panel_layout.addWidget(title)
        panel_layout.addWidget(subtitle)

        for asset_id in self.manager.onboarding_asset_order():
            spec = self.manager.get_spec(asset_id)
            row = QFrame()
            row.setObjectName("assetRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 10, 10, 10)
            row_layout.setSpacing(8)

            text_box = QVBoxLayout()
            text_box.setSpacing(2)
            lbl_title = QLabel(spec.title)
            f = lbl_title.font()
            f.setBold(True)
            lbl_title.setFont(f)
            lbl_meta = QLabel(
                f"{spec.accepted_formats} | Credits: {spec.credits}"
            )
            lbl_meta.setObjectName("subtitleLabel")
            lbl_meta.setWordWrap(True)
            text_box.addWidget(lbl_title)
            text_box.addWidget(lbl_meta)
            row_layout.addLayout(text_box, 1)

            status_lbl = QLabel("")
            status_lbl.setMinimumWidth(95)
            status_lbl.setAlignment(Qt.AlignCenter)
            self._asset_status_labels[asset_id] = status_lbl
            row_layout.addWidget(status_lbl)

            btn_source = QPushButton("Font")
            btn_source.clicked.connect(
                lambda _=False, url=spec.source_url: QDesktopServices.openUrl(
                    QUrl(url)
                )
            )
            row_layout.addWidget(btn_source)

            btn_run = QPushButton("Configurar")
            btn_run.clicked.connect(
                lambda _=False, aid=asset_id: self._run_asset_wizard(aid)
            )
            self._asset_run_buttons[asset_id] = btn_run
            row_layout.addWidget(btn_run)

            panel_layout.addWidget(row)

        panel_layout.addStretch(1)
        layout.addWidget(panel, 1)
        return page

    def _refresh_asset_statuses(self) -> None:
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
