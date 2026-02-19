
import os
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QLabel, QPushButton, 
                             QFileDialog, QHBoxLayout, QMessageBox)
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices

from TerraLab.config import ConfigManager

class TerrainConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuración del Terreno")
        self.resize(500, 300)
        self.setModal(True)
        self.config = ConfigManager()
        
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        
        # Title
        lbl_title = QLabel("Configuración de Mapas Topográficos")
        font = lbl_title.font()
        font.setPointSize(12)
        font.setBold(True)
        lbl_title.setFont(font)
        lbl_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_title)
        
        # Description
        desc = (
            "TerraLab necesita datos de elevación (MDT) para generar el horizonte real.\n"
            "Si no dispones de ellos, se utilizará un paisaje generado proceduralmente.\n\n"
            "Formato soportado: Archivos .asc (ESRI ASCII Grid) o .txt."
        )
        lbl_desc = QLabel(desc)
        lbl_desc.setWordWrap(True)
        layout.addWidget(lbl_desc)
        
        # Link
        btn_link = QPushButton("Descargar MDT de Cataluña (ICGC)")
        btn_link.setCursor(Qt.PointingHandCursor)
        btn_link.setStyleSheet("text-align: left; color: #4facfe; text-decoration: underline; background: transparent; border: none;")
        btn_link.clicked.connect(self.open_icgc_link)
        layout.addWidget(btn_link)
        
        # Recommendation
        lbl_rec = QLabel("ℹ Recomendación: Descargar al menos 150km a la redonda en ficheros troceados (5x5km o similar).")
        lbl_rec.setStyleSheet("color: #aaa; font-style: italic;")
        lbl_rec.setWordWrap(True)
        layout.addWidget(lbl_rec)
        
        # Current Path Display
        self.lbl_path = QLabel("Ruta actual: (No configurada)")
        self.lbl_path.setStyleSheet("background: #222; padding: 8px; border-radius: 4px; color: #ddd;")
        self.lbl_path.setWordWrap(True)
        layout.addWidget(self.lbl_path)
        
        # Buttons
        btn_layout = QHBoxLayout()
        
        btn_select = QPushButton("Seleccionar Carpeta...")
        btn_select.clicked.connect(self.select_folder)
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
        
        btn_close = QPushButton("Cerrar")
        btn_close.clicked.connect(self.accept)
        
        btn_layout.addWidget(btn_select)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        
        layout.addLayout(btn_layout)
        
        # Load initial state
        curr = self.config.get_raster_path()
        if curr:
            self.lbl_path.setText(f"Ruta actual: {curr}")

    def open_icgc_link(self):
        # ICGC Downloads section
        url = QUrl("https://www.icgc.cat/ca/Descarregues/Elevacions/Model-Digital-del-Terreny-MDT")
        QDesktopServices.openUrl(url)

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta con ficheros .asc")
        if folder:
            # Check for valid content
            has_asc = any(f.endswith(('.asc', '.txt')) for f in os.listdir(folder))
            if not has_asc:
                # Warning but allow it (maybe user will add files later)
                QMessageBox.warning(self, "Carpeta vacía", 
                                    "No se han encontrado archivos .asc o .txt en esta carpeta.\n"
                                    "Asegúrate de descomprimir los mapas aquí.")
            
            self.config.set_raster_path(folder)
            self.lbl_path.setText(f"Ruta actual: {folder}")
            # We don't close immediately, user might want to read more or close manually
