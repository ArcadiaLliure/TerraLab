"""Onboarding dialogs: first-run welcome and per-layer asset assistant."""

from __future__ import annotations

import os
import json
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
    QLabel,
    QLineEdit,
    QMessageBox,
    QInputDialog,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from TerraLab.common.utils import getTraduction, set_config_value
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

        self.setWindowTitle(f"TerraLab - {self.spec.title}")
        self.setModal(True)
        self.resize(760, 500)
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
                gaia_log_line = "\nLog persistent: %APPDATA%/TerraLab/logs/gaia_tap_last.log\n"
            extra_help = (
                "\n\nEl boto 'Descarregar automaticament' executa el TAP en segon pla (Python natiu).\n"
                "Flux: primer prepara estrelles visibles (mag <= 8), i despres continua per lots "
                "fins a la magnitud objectiu, amb progressio reanudable.\n"
                f"{gaia_log_line}"
                "Opcional (avancat): pots executar-ho manualment amb:\n"
                "python tools/download_gaia_tap.py --mag-limit 15 --yes"
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
        self.txt_user_agent.setPlaceholderText("TerraLab/1.0 (contact@example.com)")
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
        self.btn_open_source = QPushButton(getTraduction("Onboarding.OpenSource", "Obrir font oficial"))
        self.btn_open_source.clicked.connect(self._open_source)
        actions.addWidget(self.btn_open_source)

        self.btn_auto_download = QPushButton(getTraduction("Onboarding.AutoDownload", "Descarregar automaticament"))
        self.btn_auto_download.setEnabled(self._supports_auto_download())
        self.btn_auto_download.clicked.connect(self._auto_download)
        actions.addWidget(self.btn_auto_download)

        self.btn_attach = QPushButton(getTraduction("Onboarding.AttachFiles", "Adjuntar fitxer(s)"))
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
        self.btn_close = QPushButton(getTraduction("Onboarding.Close", "Tancar"))
        self.btn_close.clicked.connect(self.reject)
        footer.addWidget(self.btn_close)
        root.addLayout(footer)

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
        QMessageBox.warning(self, "TerraLab", "Cal definir un User-Agent valid.")

    def _attach_files(self):
        if self.asset_id == "climate_metno":
            return
        allow_multiple = bool(self.spec.allow_multiple)
        filters = (
            "Data files (*.fits *.png *.ecsv *.csv *.zst *.zip *.tif *.tiff *.asc *.txt);;"
            "All files (*.*)"
        )
        if allow_multiple:
            files, _ = QFileDialog.getOpenFileNames(self, "Selecciona fitxers", "", filters)
        else:
            single, _ = QFileDialog.getOpenFileName(self, "Selecciona fitxer", "", filters)
            files = [single] if single else []
        files = [f for f in files if f and os.path.exists(f)]
        if not files:
            return
        self._start_job(mode="import", files=files, options=self._collect_options())

    def _auto_download(self):
        if self.asset_id == "gaia_catalog":
            state = self._load_gaia_tap_state()
            pending = isinstance(state, dict) and str(state.get("status", "")).lower() not in {"done", "completed", "success"}
            if pending:
                try:
                    pct = float(state.get("progress_percent", 0.0) or 0.0)
                except Exception:
                    pct = 0.0
                msg = (
                    "S'ha detectat una descarrega Gaia pendent.\n"
                    f"Progres guardat: {pct:.1f}%.\n\n"
                    "Vols reprendre-la ara?"
                )
                ans = QMessageBox.question(self, "TerraLab", msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if ans == QMessageBox.Yes:
                    self._start_gaia_tap_process(resume=True)
                    return
            self._start_gaia_tap_process(resume=False)
            return
        if not self.spec.auto_download_url:
            return
        self._start_job(mode="download", files=[], options=self._collect_options())

    def start_gaia_tap_resume(self) -> None:
        self._start_gaia_tap_process(resume=True)

    def _start_gaia_tap_process(self, *, resume: bool = False, mag_limit: Optional[float] = None) -> None:
        resolved_mag_limit = mag_limit
        if not resume:
            if resolved_mag_limit is None:
                mag_default = 15.0
                try:
                    mag_default = float(self.manager.layout.get("gaia_mag_limit_default", 15.0))
                except Exception:
                    pass
                mag_dialog = QInputDialog(self)
                mag_dialog.setWindowTitle("Gaia TAP")
                mag_dialog.setLabelText("Magnitud maxima G:")
                mag_dialog.setInputMode(QInputDialog.DoubleInput)
                mag_dialog.setDoubleRange(1.0, 23.0)
                mag_dialog.setDoubleDecimals(2)
                mag_dialog.setDoubleValue(float(mag_default))
                mag_dialog.setStyleSheet(_ASTRO_DIALOG_STYLE)
                if mag_dialog.exec_() != QDialog.Accepted:
                    return
                resolved_mag_limit = float(mag_dialog.doubleValue())
            else:
                resolved_mag_limit = float(resolved_mag_limit)

        project_root = Path(__file__).resolve().parents[1]
        script_path = project_root / "tools" / "download_gaia_tap.py"
        if not script_path.exists():
            QMessageBox.critical(self, "TerraLab", f"No s'ha trobat l'script: {script_path}")
            return

        if self._gaia_tap_process is not None:
            QMessageBox.information(self, "TerraLab", "Ja hi ha un proces Gaia TAP en execucio.")
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
        args = [
            "-u",
            str(script_path),
            "--yes",
            "--state-file",
            str(state_path),
            "--log-file",
            str(log_path),
        ]
        if resume:
            args.append("--resume")
        else:
            args.extend(["--mag-limit", f"{float(resolved_mag_limit):.2f}"])
        process.setArguments(args)
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._on_gaia_tap_output)
        process.finished.connect(self._on_gaia_tap_finished)
        process.errorOccurred.connect(self._on_gaia_tap_error)

        self.btn_open_source.setEnabled(False)
        self.btn_attach.setEnabled(False)
        self.btn_auto_download.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.progress.setRange(0, 0)  # indeterminate while waiting first progress markers
        self.lbl_status.setText(
            getTraduction("Onboarding.DownloadingStars", "Descarregant estrelles...")
        )
        self.txt_process_log.clear()
        self.txt_process_log.setVisible(True)
        self._append_gaia_tap_log_line(f"[gaia-tap] log file: {log_path}")
        self._append_gaia_tap_log_line(f"[gaia-tap] state file: {state_path}")
        self._gaia_tap_out_buffer = ""
        self._gaia_tap_process = process
        process.start()
        if not process.waitForStarted(5000):
            err_txt = str(process.errorString() or "No es pot iniciar el proces Gaia TAP.")
            self._cleanup_gaia_tap_process()
            self.progress.setRange(0, 100)
            self.lbl_status.setText("Error en iniciar Gaia TAP.")
            self.btn_open_source.setEnabled(True)
            self.btn_attach.setEnabled(True)
            self.btn_auto_download.setEnabled(self._supports_auto_download())
            self.btn_close.setEnabled(True)
            QMessageBox.critical(self, "TerraLab", f"{err_txt}\n\n{self._gaia_tap_log_hint()}")
            return

    def _resolve_gaia_tap_log_path(self) -> Path:
        try:
            root = Path(self.manager.layout.get("root", Path.home())).resolve()
        except Exception:
            root = Path.home()
        return root / "logs" / "gaia_tap_last.log"

    def _resolve_gaia_tap_state_path(self) -> Path:
        try:
            root = Path(self.manager.layout.get("root", Path.home())).resolve()
        except Exception:
            root = Path.home()
        return root / "logs" / "gaia_tap_state.json"

    def _load_gaia_tap_state(self) -> Optional[dict]:
        path = self._resolve_gaia_tap_state_path()
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                return payload
        except Exception:
            return None
        return None

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
        if not bool(state.get("visible_ready", False)):
            return
        output_dir = Path(str(state.get("output_dir", "") or "")).expanduser()
        basename = str(state.get("basename", "stars_catalog") or "stars_catalog").strip() or "stars_catalog"
        candidates = (
            output_dir / f"{basename}.npy",
            output_dir / f"{basename}.npz",
            output_dir / f"{basename}.zst",
        )
        visible_catalog_path = next((p for p in candidates if p.exists() and p.is_file()), None)
        if visible_catalog_path is None:
            return
        if visible_catalog_path.stat().st_size <= 0:
            return
        if str(visible_catalog_path.suffix).lower() in {".npy", ".npz"}:
            try:
                if visible_catalog_path.suffix.lower() == ".npy":
                    arr = np.load(visible_catalog_path, mmap_mode="r", allow_pickle=False)
                    rows = int(len(arr))
                else:
                    with np.load(visible_catalog_path, allow_pickle=False) as data:
                        if "ra" in data:
                            rows = int(len(data["ra"]))
                        elif "RA" in data:
                            rows = int(len(data["RA"]))
                        else:
                            rows = 0
                if rows <= 0:
                    return
            except Exception:
                return
        self._gaia_visible_ready = True
        self._completed = True
        self._detach_gaia_tap_process_for_background()
        self.progress.setRange(0, 100)
        try:
            pct_val = float(state.get("progress_percent", 0.0) or 0.0)
        except Exception:
            pct_val = 0.0
        self.progress.setValue(max(0, min(100, int(round(pct_val)))))
        self.lbl_status.setText("Cataleg visible preparat. Es continua descarregant en segon pla...")
        self.accept()

    def _append_gaia_tap_log_line(self, line: str) -> None:
        text = str(line or "").rstrip("\r\n")
        if not text:
            return
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

        m2 = re.search(r"\[gaia-tap\]\s+download\s+([0-9]+(?:\.[0-9]+)?)%", text)
        if m2 and self.progress.maximum() == 0:
            try:
                pct2 = max(0, min(100, int(round(float(m2.group(1)) * 0.75))))
                self.progress.setRange(0, 100)
                self.progress.setValue(pct2)
            except Exception:
                pass

        m3 = re.search(r"\[gaia-progress\]\s+([0-9]+(?:\.[0-9]+)?)%", text)
        if m3:
            try:
                pct3 = max(0, min(100, int(round(float(m3.group(1))))))
                if self.progress.maximum() == 0:
                    self.progress.setRange(0, 100)
                self.progress.setValue(pct3)
                status = getTraduction("Onboarding.DownloadingStars", "Descarregant estrelles...")
                self.lbl_status.setText(f"{status} ({pct3}%)")
            except Exception:
                pass

        if "[gaia-ui]" in text:
            self._maybe_close_after_visible_ready()

    def _on_gaia_tap_output(self):
        proc = self._gaia_tap_process
        if proc is None:
            return
        chunk = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not chunk:
            return
        merged = self._gaia_tap_out_buffer + chunk
        merged = merged.replace("\r\n", "\n").replace("\r", "\n")
        lines = merged.split("\n")
        self._gaia_tap_out_buffer = lines.pop() if lines else ""
        for line in lines:
            self._append_gaia_tap_log_line(line)
        self._maybe_close_after_visible_ready()

    def _on_gaia_tap_finished(self, exit_code: int, exit_status: QProcess.ExitStatus):
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
            self.lbl_status.setText("Cataleg Gaia preparat correctament.")
            QMessageBox.information(
                self,
                "TerraLab",
                "Importacio Gaia completada.\n\n"
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
                pct_val = float(state.get("progress_percent", 0.0) or 0.0)
                pct_hint = f"\nProgres guardat: {pct_val:.1f}% (es pot reprendre)."
            except Exception:
                pct_hint = ""
        self.lbl_status.setText("Gaia TAP ha finalitzat, pero no s'ha detectat un cataleg valid.")
        status_name = "normal" if exit_status == QProcess.NormalExit else "crash"
        QMessageBox.warning(
            self,
            "TerraLab",
            "El proces TAP ha acabat, pero TerraLab no troba el cataleg Gaia preparat.\n"
            f"Exit code: {int(exit_code)} ({status_name}).\n"
            f"{log_hint}{pct_hint}",
        )

    def _on_gaia_tap_error(self, _error):
        proc = self._gaia_tap_process
        err_txt = "Error executant Gaia TAP."
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
        self.lbl_status.setText("Error en Gaia TAP.")
        QMessageBox.critical(self, "TerraLab", f"{err_txt}\n\n{log_hint}")

    def _collect_options(self) -> dict:
        if self.asset_id == "milkyway_texture":
            return {
                "remove_stars": bool(self.chk_remove_stars.isChecked()),
            }
        return {}

    def _start_job(self, mode: str, files: Iterable[str], options: Optional[dict] = None):
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
        msg = (
            "Importacio completada.\n\n"
            "Les dades ja son dins la carpeta de TerraLab.\n"
            "Si vols, ja pots esborrar el fitxer original que has adjuntat."
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

    def __init__(self, manager: AssetManager, parent=None, *, mandatory: bool = False):
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
            lbl_meta = QLabel(f"{spec.accepted_formats} | Credits: {spec.credits}")
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
            btn_source.clicked.connect(lambda _=False, url=spec.source_url: QDesktopServices.openUrl(QUrl(url)))
            row_layout.addWidget(btn_source)

            btn_run = QPushButton("Configurar")
            btn_run.clicked.connect(lambda _=False, aid=asset_id: self._run_asset_wizard(aid))
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
            label.setObjectName("assetStatusOk" if ready else "assetStatusMissing")
            label.style().unpolish(label)
            label.style().polish(label)

    def _run_asset_wizard(self, asset_id: str) -> None:
        dlg = AssetOnboardingDialog(self.manager, asset_id, self)
        ok = dlg.exec_() == QDialog.Accepted and bool(getattr(dlg, "completed", False))
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
        self.pages.setCurrentIndex(min(self.pages.count() - 1, self._data_page_index + 1))
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
        if self._mandatory:
            return
        super().reject()
