
# Diàleg de configuració del terreny (MDT/DEM) per a TerraLab.
# Gestiona la selecció de la carpeta de dades, la qualitat de l'horitzó
# i ofereix enllaços de descàrrega als principals repositoris de MDT públics.

import os
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QLabel, QPushButton,
                             QFileDialog, QHBoxLayout, QMessageBox, QComboBox)
from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices

from TerraLab.config import ConfigManager
from TerraLab.common.utils import getTraduction


# Presets de qualitat de l'horitzó: (clau de traducció, nombre de capes)
QUALITY_PRESETS = [
    ("Horizon.QualityLow",     10),
    ("Horizon.QualityNormal",  20),
    ("Horizon.QualityHigh",    40),
    ("Horizon.QualityUltra",   60),
    ("Horizon.QualityExtreme", 80),
]


class TerrainConfigDialog(QDialog):
    """
    Diàleg modal per configurar:
      - La carpeta dels fitxers MDT (GeoTIFF / ASC).
      - La qualitat de renderització de l'horitzó (nombre de capes).
    Tots els textos mostrats a l'usuari passen pel sistema de traducció.
    """

    # Senyal emès quan l'usuari canvia el nivell de qualitat (transmet el nombre de capes)
    quality_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(getTraduction("Terrain.ConfigTitle", "Configuració de Mapes Topogràfics"))
        self.resize(560, 440)
        self.setModal(True)
        self.config = ConfigManager()

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        # ── Títol ────────────────────────────────────────────────────────────
        lbl_title = QLabel(getTraduction("Terrain.ConfigTitle", "Configuració de Mapes Topogràfics"))
        font = lbl_title.font()
        font.setPointSize(12)
        font.setBold(True)
        lbl_title.setFont(font)
        lbl_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_title)

        # ── Descripció general ───────────────────────────────────────────────
        lbl_desc = QLabel(getTraduction(
            "Terrain.ConfigDesc",
            "TerraLab necessita dades d'elevació (MDT) per generar l'horitzó real.\n"
            "Si no en disposes, s'utilitzarà un paisatge generat proceduralment.\n\n"
            "Formats suportats: .asc (ESRI ASCII Grid) o .tif (GeoTIFF)."
        ))
        lbl_desc.setWordWrap(True)
        layout.addWidget(lbl_desc)

        # ── Enllaços de descàrrega ───────────────────────────────────────────

        # Enllaç ICGC (Catalunya)
        btn_icgc = QPushButton(getTraduction("Terrain.DownloadICGC", "Descarregar MDT de Catalunya (ICGC)"))
        btn_icgc.setCursor(Qt.PointingHandCursor)
        btn_icgc.setStyleSheet(
            "text-align: left; color: #4facfe; text-decoration: underline; "
            "background: transparent; border: none;"
        )
        btn_icgc.clicked.connect(self._open_icgc_link)
        layout.addWidget(btn_icgc)

        # Enllaç Copernicus (Europa)
        btn_copernicus = QPushButton(
            getTraduction("Terrain.DownloadCopernicus", "Descarregar DEM d'Europa (Copernicus / GISCO-EU)")
        )
        btn_copernicus.setCursor(Qt.PointingHandCursor)
        btn_copernicus.setStyleSheet(
            "text-align: left; color: #4facfe; text-decoration: underline; "
            "background: transparent; border: none;"
        )
        btn_copernicus.setToolTip(getTraduction(
            "Terrain.DownloadCopernicusTooltip",
            "El DEM europeu de 25m de resolució del projecte Copernicus/GISCO "
            "cobreix tot el continent. Disponible per descàrrega lliure en fitxers de 5×5 graus."
        ))
        btn_copernicus.clicked.connect(self._open_copernicus_link)
        layout.addWidget(btn_copernicus)

        # Nota per a la resta del món
        lbl_world = QLabel(getTraduction(
            "Terrain.DownloadWorld",
            "ℹ Per a altres regions del món, consulteu l'agència cartogràfica del vostre país "
            "(p. ex. USGS, IGN, OS, BKG…)."
        ))
        lbl_world.setStyleSheet("color: #aaa; font-style: italic;")
        lbl_world.setWordWrap(True)
        layout.addWidget(lbl_world)

        # ── Recomanació de cobertura ─────────────────────────────────────────
        lbl_rec = QLabel(getTraduction(
            "Terrain.Recommendation",
            "ℹ Recomanació: Descarregueu almenys 150km a la redona en fitxers "
            "trossejats (5×5° o similar)."
        ))
        lbl_rec.setStyleSheet("color: #aaa; font-style: italic;")
        lbl_rec.setWordWrap(True)
        layout.addWidget(lbl_rec)

        # ── Ruta actual ──────────────────────────────────────────────────────
        no_path_label = getTraduction("Terrain.CurrentPath", "Ruta actual: (No configurada)")
        self.lbl_path = QLabel(no_path_label)
        self.lbl_path.setStyleSheet("background: #222; padding: 8px; border-radius: 4px; color: #ddd;")
        self.lbl_path.setWordWrap(True)
        layout.addWidget(self.lbl_path)

        # ── Qualitat de l'horitzó ────────────────────────────────────────────
        quality_row = QHBoxLayout()

        lbl_quality = QLabel(getTraduction("Horizon.QualityLabel", "Qualitat de l'horitzó:"))
        tooltip_quality = getTraduction(
            "Horizon.QualityTooltip",
            "Nombre de capes de profunditat del terreny.\n"
            "Més capes = millor gradient visual, major temps de càlcul inicial."
        )
        lbl_quality.setToolTip(tooltip_quality)
        quality_row.addWidget(lbl_quality)

        self.combo_quality = QComboBox()
        self.combo_quality.setToolTip(tooltip_quality)

        current_n = self.config.get_horizon_quality()
        selected_idx = 1  # Normal per defecte
        for i, (key, n) in enumerate(QUALITY_PRESETS):
            label = getTraduction(key, f"{n} capes")
            self.combo_quality.addItem(label, userData=n)
            if n == current_n:
                selected_idx = i

        self.combo_quality.setCurrentIndex(selected_idx)
        self.combo_quality.currentIndexChanged.connect(self._on_quality_changed)
        quality_row.addWidget(self.combo_quality)
        quality_row.addStretch()
        layout.addLayout(quality_row)

        lbl_quality_note = QLabel(getTraduction(
            "Horizon.QualityNote",
            "📝 El canvi s'aplica al proper càlcul de l'horitzó (\"Regenerar\")."
        ))
        lbl_quality_note.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(lbl_quality_note)

        # ── Botons d'acció ───────────────────────────────────────────────────
        btn_layout = QHBoxLayout()

        btn_select = QPushButton(getTraduction("Terrain.SelectFolder", "Seleccionar Carpeta DEM..."))
        btn_select.clicked.connect(self._select_folder)
        btn_select.setStyleSheet("""
            QPushButton {
                background-color: #4facfe;
                color: white;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #00f2fe;
            }
        """)

        btn_close = QPushButton(getTraduction("Terrain.CloseButton", "Tancar"))
        btn_close.clicked.connect(self.accept)

        btn_layout.addWidget(btn_select)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

        # ── Estat inicial de la ruta ─────────────────────────────────────────
        curr = self.config.get_raster_path()
        if curr:
            tpl = getTraduction("Terrain.CurrentPathSet", "Ruta actual: {path}")
            self.lbl_path.setText(tpl.format(path=curr))

    # ── Mètodes privats ──────────────────────────────────────────────────────

    def _open_icgc_link(self):
        """Obre el portal de descàrregues del MDT de l'ICGC (Catalunya)."""
        url = QUrl("https://www.icgc.cat/ca/Descarregues/Elevacions/Model-Digital-del-Terreny-MDT")
        QDesktopServices.openUrl(url)

    def _open_copernicus_link(self):
        """Obre el repositori del DEM europeu de Copernicus (GISCO-EU)."""
        url = QUrl("https://gisco-services.ec.europa.eu/dem/5degree/mosaic/")
        QDesktopServices.openUrl(url)

    def _on_quality_changed(self, idx):
        """Desa la nova qualitat de l'horitzó i emet la senyal corresponent."""
        n = self.combo_quality.itemData(idx)
        if n is not None:
            self.config.set_horizon_quality(n)
            self.quality_changed.emit(n)
            print(f"[TerrainConfigDialog] Qualitat de l'horitzó establerta a {n} capes.")

    def _select_folder(self):
        """Obre un diàleg per seleccionar la carpeta dels fitxers MDT."""
        dialog_title = getTraduction("Terrain.SelectFolderDialog", "Seleccionar carpeta amb fitxers .asc o .tif")
        folder = QFileDialog.getExistingDirectory(self, dialog_title)
        if folder:
            # Comprova si la carpeta conté fitxers compatibles
            has_valid = any(
                f.lower().endswith(('.asc', '.txt', '.tif', '.tiff'))
                for f in os.listdir(folder)
            )
            if not has_valid:
                QMessageBox.warning(
                    self,
                    getTraduction("Terrain.EmptyFolder", "Carpeta buida o sense DEM suportat"),
                    getTraduction(
                        "Terrain.EmptyFolderMsg",
                        "No s'han trobat arxius .asc, .txt o .tif en aquesta carpeta.\n"
                        "Assegureu-vos de descomprimir els mapes aquí."
                    )
                )

            self.config.set_raster_path(folder)
            tpl = getTraduction("Terrain.CurrentPathSet", "Ruta actual: {path}")
            self.lbl_path.setText(tpl.format(path=folder))
