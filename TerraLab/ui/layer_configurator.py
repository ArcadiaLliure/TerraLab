"""Shared layer-library configurator used during and after onboarding."""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QEvent, QEasingCurve, QPropertyAnimation, QTimer, QUrl, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
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
from TerraLab.data.resumable_download import human_bytes


_STATE_ICON = {
    LayerState.READY: "✓",
    LayerState.PARTIAL: "◐",
    LayerState.MISSING: "○",
    LayerState.INVALID: "!",
    LayerState.PLANNED: "…",
    LayerState.DOWNLOADING: "⇣",
    LayerState.PAUSED: "‖",
    LayerState.EXTRACTING: "⚙",
    LayerState.ERROR: "!",
}


class _LayerRow(QFrame):
    changed = pyqtSignal(str, str)
    interacted = pyqtSignal(str)

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

        self.product_details = QLabel()
        self.product_details.setWordWrap(True)
        self.product_details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.product_details.setStyleSheet("color: #aebed8; font-size: 10px;")
        root.addWidget(self.product_details)

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
            layer_id
            in {
                LayerId.EARTH_TERRAIN,
                LayerId.EARTH_SURFACE_CATEGORICAL,
                LayerId.EARTH_SURFACE_RGB,
            }
        )
        actions.addWidget(self.link_folder_button)
        self.prepare_button = QPushButton("Preparar / copiar…")
        self.prepare_button.clicked.connect(self._open_asset_wizard)
        actions.addWidget(self.prepare_button)
        self.activate_button = QPushButton("Fer activa")
        self.activate_button.clicked.connect(self._activate_surface)
        self.activate_button.setVisible(
            layer_id
            in {
                LayerId.EARTH_SURFACE_CATEGORICAL,
                LayerId.EARTH_SURFACE_RGB,
            }
        )
        actions.addWidget(self.activate_button)
        root.addLayout(actions)
        self._attention_animation = None
        self._attention_effect = None
        for widget in (self, *self.findChildren(QWidget)):
            widget.installEventFilter(self)
        self.refresh()

    def eventFilter(self, watched, event):
        """Stop the guided highlight as soon as the user chooses this row."""

        mouse_activation = event.type() == QEvent.MouseButtonPress
        key_activation = event.type() == QEvent.KeyPress and event.key() in {
            Qt.Key_Enter,
            Qt.Key_Return,
            Qt.Key_Space,
        }
        if self._attention_animation is not None and (
            mouse_activation or key_activation
        ):
            self.stop_attention()
            self.interacted.emit(self.layer_id.value)
        return super().eventFilter(watched, event)

    def start_attention(self) -> None:
        """Draw a bright pulsing frame around a row that needs user action."""

        self.stop_attention()
        self.setStyleSheet(
            "QFrame#assetRow {"
            " background-color: #182f50; border: 2px solid #ffe45e;"
            " border-radius: 8px; }"
        )
        effect = QGraphicsDropShadowEffect(self)
        effect.setOffset(0, 0)
        effect.setColor(QColor(255, 220, 55, 235))
        effect.setBlurRadius(9.0)
        self.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"blurRadius", self)
        animation.setDuration(900)
        animation.setStartValue(7.0)
        animation.setKeyValueAt(0.5, 28.0)
        animation.setEndValue(7.0)
        animation.setEasingCurve(QEasingCurve.InOutSine)
        animation.setLoopCount(-1)
        self._attention_effect = effect
        self._attention_animation = animation
        animation.start()

    def stop_attention(self) -> None:
        animation = self._attention_animation
        if animation is not None:
            animation.stop()
        self._attention_animation = None
        self._attention_effect = None
        self.setGraphicsEffect(None)
        self.setStyleSheet("")

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
        spec = self.manager.assets.get_spec(self.descriptor.asset_id)
        details = []
        if spec.provider:
            details.append(f"Proveïdor: {spec.provider}")
        if spec.nominal_resolution_m:
            details.append(f"Resolució nominal: {spec.nominal_resolution_m:g} m")
        if spec.approximate_download_bytes:
            details.append(
                f"Descàrrega aproximada: {human_bytes(spec.approximate_download_bytes)}"
            )
        if spec.nominal_crs:
            details.append(f"CRS: {spec.nominal_crs}")
        if spec.geographic_extent:
            details.append(f"Extensió: {spec.geographic_extent}")
        if spec.credits:
            details.append(f"Crèdits: {spec.credits}")
        if spec.license_note:
            details.append(f"Condicions: {spec.license_note}")
        self.product_details.setText("\n".join(details))
        self.product_details.setVisible(bool(details))
        resource_lines = []
        for resource in status.resources:
            state = "disponible" if resource.ready else "no disponible"
            location = f" · {resource.path}" if resource.path else ""
            details = f" · {resource.details}" if resource.details else ""
            active = (
                " · ACTIVA"
                if resource.ready and resource.name == status.effective_source
                else ""
            )
            resource_lines.append(
                f"{resource.name}: {state}{active}{details}{location}"
            )
        self.resources.setText("\n".join(resource_lines))
        self.resources.setVisible(bool(resource_lines))
        for child, checkbox in self.child_checks.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(self.manager.child_visible(child))
            checkbox.blockSignals(False)
        if self.activate_button.isVisible():
            source_ids = {resource.id for resource in status.resources if resource.ready}
            active = bool(status.effective_source)
            self.activate_button.setText("Activa" if active else "Fer activa")
            self.activate_button.setEnabled(bool(source_ids) and not active)

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
        result = dialog.exec_()
        self.refresh()
        if result == QDialog.Accepted:
            self.changed.emit(self.layer_id.value, "source")

    def _activate_surface(self) -> None:
        status = self.manager.status(self.layer_id)
        ready = [resource for resource in status.resources if resource.ready]
        if not ready:
            return
        self.manager.set_source(self.layer_id, ready[0].id)
        self.refresh()
        self.changed.emit(self.layer_id.value, "selection")


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

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs, 1)
        self._group_tabs: dict[LayerGroup, int] = {}
        self._group_scrolls: dict[LayerGroup, QScrollArea] = {}
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
                row.changed.connect(self._row_changed)
                self._rows[descriptor.id] = row
                layout.addWidget(row)
            layout.addStretch(1)
            scroll.setWidget(content)
            self._group_scrolls[group] = scroll
            self._group_tabs[group] = self.tabs.addTab(scroll, title)
        self.refresh()

    def focus_layer(self, layer_id: LayerId | str, *, pulse: bool = True) -> None:
        """Open the right group and bring one layer action row into view."""

        try:
            normalized = (
                layer_id if isinstance(layer_id, LayerId) else LayerId(str(layer_id))
            )
            descriptor = self.manager.descriptor(normalized)
            row = self._rows[descriptor.id]
        except (KeyError, ValueError):
            return
        self.tabs.setCurrentIndex(self._group_tabs[descriptor.group])
        if pulse:
            row.start_attention()

        def _reveal() -> None:
            scroll = self._group_scrolls.get(descriptor.group)
            if scroll is not None:
                scroll.ensureWidgetVisible(row, 12, 24)
            row.prepare_button.setFocus(Qt.OtherFocusReason)

        QTimer.singleShot(0, _reveal)

    def _row_changed(self, _layer_id: str, change: str) -> None:
        if change in {"visibility", "selection"}:
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

