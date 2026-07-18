"""Shared layer-library configurator used during and after onboarding."""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QUrl, Qt, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from TerraLab.data.layer_manager import (
    LayerGroup,
    LayerId,
    LayerManager,
    LayerState,
)
from TerraLab.ui.data_library_dialog import (
    configure_data_library,
    library_free_space,
)


_STATE_ICON = {
    LayerState.READY: "✓",
    LayerState.PARTIAL: "◐",
    LayerState.MISSING: "○",
    LayerState.INVALID: "!",
    LayerState.PLANNED: "…",
}


class _LayerRow(QFrame):
    changed = pyqtSignal(str, str)

    def __init__(self, manager: LayerManager, layer_id: LayerId, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.layer_id = layer_id
        self.descriptor = manager.descriptor(layer_id)
        self.setObjectName("assetRow")

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(5)
        heading = QHBoxLayout()
        self.visible = QCheckBox(self.descriptor.title)
        font = self.visible.font()
        font.setBold(True)
        self.visible.setFont(font)
        self.visible.toggled.connect(self._visibility_changed)
        heading.addWidget(self.visible, 1)
        self.status_label = QLabel()
        self.status_label.setMinimumWidth(116)
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading.addWidget(self.status_label)
        root.addLayout(heading)

        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setObjectName("subtitleLabel")
        root.addWidget(self.message)

        self.resources = QLabel()
        self.resources.setWordWrap(True)
        self.resources.setStyleSheet("color: #c8d8f3; font-size: 10px;")
        root.addWidget(self.resources)

        if layer_id is LayerId.SKY_SOLAR_SYSTEM:
            child_row = QHBoxLayout()
            child_row.setContentsMargins(22, 0, 0, 0)
            self.child_checks = {}
            for child, label in (("sun", "Sol"), ("moon", "Lluna"), ("planets", "Planetes")):
                checkbox = QCheckBox(label)
                checkbox.toggled.connect(
                    lambda checked, name=child: self._child_changed(name, checked)
                )
                self.child_checks[child] = checkbox
                child_row.addWidget(checkbox)
            child_row.addStretch(1)
            root.addLayout(child_row)
        else:
            self.child_checks = {}

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.source_button = QPushButton("Font oficial")
        self.source_button.clicked.connect(self._open_official_source)
        actions.addWidget(self.source_button)
        self.link_file_button = QPushButton("Enllaçar fitxer…")
        self.link_file_button.clicked.connect(self._link_file)
        self.link_file_button.setVisible(self.descriptor.supports_external)
        actions.addWidget(self.link_file_button)
        self.link_folder_button = QPushButton("Enllaçar carpeta…")
        self.link_folder_button.clicked.connect(self._link_folder)
        self.link_folder_button.setVisible(
            layer_id in {LayerId.EARTH_TERRAIN, LayerId.EARTH_SURFACE}
        )
        actions.addWidget(self.link_folder_button)
        self.prepare_button = QPushButton("Preparar / copiar…")
        self.prepare_button.clicked.connect(self._open_asset_wizard)
        actions.addWidget(self.prepare_button)
        root.addLayout(actions)
        self.refresh()

    def refresh(self) -> None:
        status = self.manager.status(self.layer_id)
        self.visible.blockSignals(True)
        self.visible.setChecked(status.visible)
        self.visible.blockSignals(False)
        icon = _STATE_ICON[status.state]
        self.status_label.setText(f"{icon} {status.label}")
        self.status_label.setAccessibleName(f"Estat: {status.label}")
        self.status_label.setObjectName(
            "assetStatusOk" if status.state is LayerState.READY else "assetStatusMissing"
        )
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.message.setText(status.message)
        resource_lines = []
        for resource in status.resources:
            state = "disponible" if resource.ready else "no disponible"
            location = f" · {resource.path}" if resource.path else ""
            resource_lines.append(f"{resource.name}: {state}{location}")
        self.resources.setText("\n".join(resource_lines))
        self.resources.setVisible(bool(resource_lines))
        for child, checkbox in self.child_checks.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(self.manager.child_visible(child))
            checkbox.blockSignals(False)

    def _visibility_changed(self, checked: bool) -> None:
        self.manager.set_visible(self.layer_id, checked)
        self.changed.emit(self.layer_id.value, "visibility")

    def _child_changed(self, child: str, checked: bool) -> None:
        self.manager.set_child_visible(child, checked)
        self.changed.emit(self.layer_id.value, f"child:{child}")

    def _open_official_source(self) -> None:
        url = self.manager.assets.get_spec(self.descriptor.asset_id).source_url
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _link_file(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            f"Enllaçar dades: {self.descriptor.title}",
            "",
            "Dades compatibles (*.*)",
        )
        if path:
            self._link(path)

    def _link_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, f"Enllaçar mosaic: {self.descriptor.title}"
        )
        if path:
            self._link(path)

    def _link(self, path: str) -> None:
        try:
            self.manager.add_external_source(
                self.layer_id,
                path,
                display_name=Path(path).stem or Path(path).name,
            )
        except Exception as exc:
            QMessageBox.critical(self, "No s'ha pogut enllaçar", str(exc))
            return
        self.refresh()
        self.changed.emit(self.layer_id.value, "source")

    def _open_asset_wizard(self) -> None:
        from TerraLab.ui.onboarding_dialogs import AssetOnboardingDialog

        dialog = AssetOnboardingDialog(
            self.manager.assets,
            self.descriptor.asset_id,
            self,
        )
        if dialog.exec_() == QDialog.Accepted:
            self.refresh()
            self.changed.emit(self.layer_id.value, "source")


class LayerConfiguratorWidget(QWidget):
    """Complete, scrollable layer library with sky/earth grouping."""

    layerChanged = pyqtSignal(str, str)
    libraryChangeRequested = pyqtSignal()

    def __init__(self, manager: LayerManager, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._rows: dict[LayerId, _LayerRow] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        library_row = QHBoxLayout()
        library_row.addWidget(QLabel("Dades:"))
        self.library_path = QLabel(str(manager.library.root))
        self.library_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.library_path.setStyleSheet("color: #e8f0ff; font-weight: 600;")
        library_row.addWidget(self.library_path, 1)
        change = QPushButton("Canviar…")
        change.clicked.connect(self._change_library)
        library_row.addWidget(change)
        open_button = QPushButton("Obrir")
        open_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(manager.library.root)))
        )
        library_row.addWidget(open_button)
        root.addLayout(library_row)
        self.library_status = QLabel()
        self.library_status.setObjectName("subtitleLabel")
        root.addWidget(self.library_status)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        root.addWidget(tabs, 1)
        for group, title in ((LayerGroup.SKY, "Cel"), (LayerGroup.EARTH, "Terra")):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            content = QWidget()
            layout = QVBoxLayout(content)
            layout.setContentsMargins(4, 6, 4, 6)
            layout.setSpacing(8)
            for descriptor in manager.list_layers(group):
                row = _LayerRow(manager, descriptor.id, content)
                row.changed.connect(self.layerChanged)
                self._rows[descriptor.id] = row
                layout.addWidget(row)
            layout.addStretch(1)
            scroll.setWidget(content)
            tabs.addTab(scroll, title)
        self.refresh()

    def refresh(self) -> None:
        free = library_free_space(self.manager.library)
        self.library_status.setText(
            f"✓ Accessible · {free / (1024 ** 3):.1f} GiB lliures · configuració vàlida"
        )
        for row in self._rows.values():
            row.refresh()

    def _change_library(self) -> None:
        if configure_data_library(self, changing=True) is not None:
            self.libraryChangeRequested.emit()


__all__ = ["LayerConfiguratorWidget"]

