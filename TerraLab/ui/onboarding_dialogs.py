"""Onboarding dialogs: first-run welcome and per-layer asset assistant."""

from __future__ import annotations

import os
from typing import Iterable, Optional

from PyQt5.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot, QUrl
from PyQt5.QtGui import QDesktopServices, QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from TerraLab.common.utils import getTraduction, set_config_value
from TerraLab.data.assets_manager import AssetManager


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

        self.setWindowTitle(f"TerraLab - {self.spec.title}")
        self.setModal(True)
        self.resize(680, 420)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        title = QLabel(self.spec.title)
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        root.addWidget(title)

        desc = QLabel(
            getTraduction(
                "Onboarding.AssetNeedData",
                "Aquesta capa necessita dades per funcionar. Tria com vols preparar-les.",
            )
        )
        desc.setWordWrap(True)
        root.addWidget(desc)

        details = QTextEdit()
        details.setReadOnly(True)
        details.setMinimumHeight(110)
        details.setText(
            f"Font oficial:\n{self.spec.source_url}\n\n"
            f"Formats admesos:\n{self.spec.accepted_formats}\n\n"
            "Despres d'importar les dades, TerraLab les copia a la carpeta de l'app.\n"
            "Pots esborrar el fitxer original si vols."
        )
        root.addWidget(details)

        self.climate_block = QWidget()
        climate_layout = QGridLayout(self.climate_block)
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
        self.btn_auto_download.setEnabled(bool(self.spec.auto_download_url))
        self.btn_auto_download.clicked.connect(self._auto_download)
        actions.addWidget(self.btn_auto_download)

        self.btn_attach = QPushButton(getTraduction("Onboarding.AttachFiles", "Adjuntar fitxer(s)"))
        self.btn_attach.clicked.connect(self._attach_files)
        actions.addWidget(self.btn_attach)

        root.addLayout(actions)

        self.lbl_status = QLabel("")
        root.addWidget(self.lbl_status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

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
            "Data files (*.fits *.png *.ecsv *.csv *.zip *.tif *.tiff *.asc *.txt);;"
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
        if not self.spec.auto_download_url:
            return
        self._start_job(mode="download", files=[], options=self._collect_options())

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
        value = max(0, min(100, int(round(float(percent)))))
        self.progress.setValue(value)
        self.lbl_status.setText(str(message))

    def _on_completed(self, result: object):
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
        self.btn_auto_download.setEnabled(bool(self.spec.auto_download_url))
        self.btn_close.setEnabled(True)
        self.lbl_status.setText("Error durant la preparacio de dades.")
        QMessageBox.critical(self, "TerraLab", str(error_message))


class WelcomeOnboardingDialog(QDialog):
    """Mandatory first-run product onboarding."""

    def __init__(self, parent=None, *, mandatory: bool = False):
        super().__init__(parent)
        self._mandatory = bool(mandatory)
        self.setWindowTitle("TerraLab - Benvinguda")
        self.setModal(True)
        self.resize(800, 540)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        if self._mandatory:
            self.setWindowFlag(Qt.WindowCloseButtonHint, False)

        root = QVBoxLayout(self)
        self.pages = QStackedWidget()
        root.addWidget(self.pages)

        self.pages.addWidget(self._build_page(
            "Benvingut a TerraLab",
            "TerraLab es una plataforma visual astronomica i geoespacial per explorar el cel, "
            "simular condicions reals d'observacio i treballar amb capes cientifiques verificables.",
        ))
        self.pages.addWidget(self._build_page(
            "Funcionalitats clau",
            "1) Cel fisic en temps real o simulat.\n"
            "2) Topografia i horitzo amb dades reals.\n"
            "3) Catalegs i capes cientifiques (Gaia, NGC, Planck, Via Lactia).\n"
            "4) Fluxos guiats d'importacio de dades amb processament en segon pla.",
        ))
        self.pages.addWidget(self._build_page(
            "Com funciona el sistema de dades",
            "Quan activis una capa, TerraLab comprovara si ja tens les dades necessaries.\n"
            "Si falten, obrira un mini-assistent per descarregar/importar i preparar-ho tot.",
        ))
        self.pages.addWidget(self._build_page(
            "Preparat per comencar",
            "Aquest onboarding es obligatori en la primera execucio per deixar l'entorn llest.\n"
            "Despres el podras reobrir quan vulguis des de l'accio de guia rapida.",
        ))

        nav = QHBoxLayout()
        self.btn_prev = QPushButton("Anterior")
        self.btn_prev.clicked.connect(self._prev)
        self.btn_next = QPushButton("Seguent")
        self.btn_next.clicked.connect(self._next)
        nav.addWidget(self.btn_prev)
        nav.addStretch(1)
        nav.addWidget(self.btn_next)
        root.addLayout(nav)
        self._refresh_nav()

    def _build_page(self, title_text: str, body_text: str) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        title = QLabel(title_text)
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        title.setFont(font)
        body = QLabel(body_text)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(title)
        layout.addSpacing(10)
        layout.addWidget(body, 1)
        return w

    def _refresh_nav(self):
        idx = self.pages.currentIndex()
        last = self.pages.count() - 1
        self.btn_prev.setEnabled(idx > 0)
        self.btn_next.setText("Finalitzar" if idx >= last else "Seguent")

    def _prev(self):
        idx = self.pages.currentIndex()
        if idx > 0:
            self.pages.setCurrentIndex(idx - 1)
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
