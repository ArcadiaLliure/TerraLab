"""Normal-use management dialog for typed geospatial data sources."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from TerraLab.common.utils import getTraduction
from TerraLab.terrain.data_sources import (
    DataSourceRegistry,
    LayerSelectionService,
    LayerType,
    SelectionMode,
    SourceHealthStatus,
)
from TerraLab.terrain.representation import normalize_terrain_representation_mode


class _InspectionSignals(QObject):
    completed = pyqtSignal(str, object, str)


class _InspectionTask(QRunnable):
    """Metadata-only raster inspection performed outside the UI thread."""

    def __init__(self, source) -> None:
        super().__init__()
        self.source = source
        self.signals = _InspectionSignals()

    def run(self) -> None:
        source_id = str(getattr(self.source, "id", ""))
        try:
            from TerraLab.terrain.source_inspection import (
                inspect_registered_source,
            )

            result = inspect_registered_source(self.source)
            self.signals.completed.emit(source_id, result, "")
        except Exception as exc:
            self.signals.completed.emit(source_id, None, str(exc))


@dataclass(frozen=True)
class DataLayerChanges:
    """Invalidation scope produced while the dialog was open."""

    elevation: bool = False
    surface: bool = False
    light_pollution: bool = False
    representation: bool = False


class _DurableChangeEvent(QObject):
    """Signal-like event that replays completions to late subscribers.

    A modal dialog returns control to its caller before worker-pool callbacks
    are necessarily drained.  A plain Qt signal can therefore be emitted in
    the small interval between ``exec_()`` returning and the parent connecting
    its invalidation handler.  This event retains that short-lived history and
    exposes the familiar ``connect`` operation, closing that race without
    keeping the dialog open or blocking raster inspection.
    """

    notified = pyqtSignal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._history: list[DataLayerChanges] = []

    def connect(self, callback: Callable[[DataLayerChanges], None]) -> None:
        """Connect ``callback`` and synchronously replay earlier events."""

        self.notified.connect(callback)
        for changes in tuple(self._history):
            callback(changes)

    def publish(self, changes: DataLayerChanges) -> None:
        """Persist and emit one post-close invalidation scope."""

        self._history.append(changes)
        self.notified.emit(changes)


_TYPE_LABELS = {
    LayerType.ELEVATION: "Elevació",
    LayerType.SURFACE_RGB: "Superfície RGB / ortofoto",
    LayerType.SURFACE_CATEGORICAL: "Superfície categòrica",
    LayerType.LIGHT_POLLUTION: "Contaminació lumínica",
}


def _source_value(source, name: str, default=None):
    value = getattr(source, name, default)
    return default if value is None else value


def _source_signature(source) -> tuple:
    def extent(name: str) -> tuple[float, ...] | None:
        value = _source_value(source, name, None)
        if not value:
            return None
        try:
            return tuple(float(component) for component in value)
        except (TypeError, ValueError):
            return None

    resolution = _source_value(source, "resolution_m", None)
    try:
        resolution = float(resolution) if resolution is not None else None
    except (TypeError, ValueError):
        resolution = None
    health = _source_value(
        source, "health_status", SourceHealthStatus.UNVERIFIED
    )
    return (
        str(_source_value(source, "id", "")),
        str(_source_value(source, "layer_type", "")),
        str(_source_value(source, "path", "")),
        int(_source_value(source, "priority", 0)),
        bool(_source_value(source, "enabled", True)),
        str(_source_value(source, "fingerprint", "")),
        str(_source_value(source, "crs", "") or ""),
        resolution,
        extent("bounds"),
        extent("coverage"),
        str(getattr(health, "value", health)),
    )


def _changes_for_layer(layer_type: LayerType | str) -> DataLayerChanges:
    try:
        kind = (
            layer_type
            if isinstance(layer_type, LayerType)
            else LayerType(str(layer_type))
        )
    except ValueError:
        return DataLayerChanges()
    return DataLayerChanges(
        elevation=kind is LayerType.ELEVATION,
        surface=kind
        in {LayerType.SURFACE_RGB, LayerType.SURFACE_CATEGORICAL},
        light_pollution=kind is LayerType.LIGHT_POLLUTION,
    )


class DataLayersDialog(QDialog):
    """Manage datasets and active selections without reopening onboarding."""

    def __init__(
        self,
        parent=None,
        *,
        registry: Optional[DataSourceRegistry] = None,
        latitude: float = 0.0,
        longitude: float = 0.0,
    ) -> None:
        super().__init__(parent)
        self.registry = registry or DataSourceRegistry.default()
        self.selection = LayerSelectionService(self.registry)
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self._updating = False
        self._inspection_jobs = {}
        self._inspection_pool = QThreadPool.globalInstance()
        self._dialog_closed = False
        self.post_close_changes = _DurableChangeEvent(self)
        self._before = self._snapshot()
        self.changes = DataLayerChanges()

        self.setWindowTitle(
            getTraduction("DataLayers.Title", "Capes de dades")
        )
        self.resize(920, 650)
        root = QVBoxLayout(self)
        root.addWidget(self._build_active_group())
        root.addWidget(self._build_installed_group(), 1)

        footer = QHBoxLayout()
        footer.addWidget(
            QLabel(
                getTraduction(
                    "DataLayers.RemoveNote",
                    "Eliminar del catàleg no esborra els fitxers del disc.",
                )
            )
        )
        footer.addStretch(1)
        close_button = QPushButton(
            getTraduction("Terrain.CloseButton", "Tancar")
        )
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        root.addLayout(footer)
        self._refresh()

    def done(self, result: int) -> None:
        """Mark the modal session closed before its nested loop unwinds."""

        self._dialog_closed = True
        super().done(result)

    def _build_active_group(self) -> QGroupBox:
        group = QGroupBox(
            getTraduction("DataLayers.Active", "Capes actives")
        )
        form = QFormLayout(group)

        self.combo_elevation = QComboBox()
        self.combo_elevation.currentIndexChanged.connect(
            lambda _index: self._selection_changed(
                "elevation", self.combo_elevation
            )
        )
        form.addRow("Elevació:", self.combo_elevation)
        self.lbl_effective_elevation = QLabel("")
        self.lbl_effective_elevation.setWordWrap(True)
        form.addRow("Elevació efectiva:", self.lbl_effective_elevation)

        self.combo_surface = QComboBox()
        self.combo_surface.currentIndexChanged.connect(
            lambda _index: self._selection_changed(
                "surface", self.combo_surface
            )
        )
        form.addRow("Superfície:", self.combo_surface)
        self.lbl_effective_surface = QLabel("")
        self.lbl_effective_surface.setWordWrap(True)
        form.addRow("Superfície efectiva:", self.lbl_effective_surface)

        self.combo_light = QComboBox()
        self.combo_light.currentIndexChanged.connect(
            lambda _index: self._selection_changed(
                "light_pollution", self.combo_light
            )
        )
        form.addRow("Contaminació lumínica:", self.combo_light)
        self.lbl_effective_light = QLabel("")
        self.lbl_effective_light.setWordWrap(True)
        form.addRow("Font efectiva:", self.lbl_effective_light)

        self.combo_representation = QComboBox()
        self.combo_representation.addItem("Perfil", "profile")
        self.combo_representation.addItem("Relleu", "relief")
        self.combo_representation.setToolTip(
            "Perfil: més ràpid, horitzó topogràfic sense volum complet.\n"
            "Relleu: malla de terreny, volum, ombres i superfície."
        )
        self.combo_representation.currentIndexChanged.connect(
            self._representation_changed
        )
        form.addRow("Representació del terreny:", self.combo_representation)
        return group

    def _build_installed_group(self) -> QGroupBox:
        group = QGroupBox(
            getTraduction("DataLayers.Installed", "Fonts instal·lades")
        )
        layout = QVBoxLayout(group)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(8)
        self.tree.setHeaderLabels(
            [
                "Nom",
                "Tipus",
                "Resolució",
                "CRS",
                "Prioritat",
                "Estat",
                "Cobertura",
                "Path",
            ]
        )
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 190)
        self.tree.setColumnWidth(1, 170)
        self.tree.setColumnWidth(7, 260)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        add_file = QPushButton("Afegir fitxer…")
        add_file.clicked.connect(self._add_file)
        buttons.addWidget(add_file)
        add_folder = QPushButton("Afegir carpeta / mosaic…")
        add_folder.clicked.connect(self._add_folder)
        buttons.addWidget(add_folder)
        toggle = QPushButton("Activar / desactivar")
        toggle.clicked.connect(self._toggle_selected)
        buttons.addWidget(toggle)
        priority_up = QPushButton("Prioritat +")
        priority_up.clicked.connect(lambda: self._adjust_priority(1))
        buttons.addWidget(priority_up)
        priority_down = QPushButton("Prioritat −")
        priority_down.clicked.connect(lambda: self._adjust_priority(-1))
        buttons.addWidget(priority_down)
        remove = QPushButton("Eliminar del catàleg")
        remove.clicked.connect(self._remove_selected)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return group

    def _snapshot(self) -> tuple:
        sources = tuple(
            sorted(_source_signature(source) for source in self.registry.list_sources())
        )
        selections = []
        for role in ("elevation", "surface", "light_pollution"):
            selection = self.registry.get_selection(role)
            selections.append(
                (
                    role,
                    str(_source_value(selection, "mode", "auto")),
                    str(_source_value(selection, "source_id", "")),
                )
            )
        return (
            sources,
            tuple(selections),
            str(self.registry.representation_mode.value),
        )

    def _source_id(self, source) -> str:
        return str(
            _source_value(source, "id", _source_value(source, "source_id", ""))
        )

    def _fill_selection_combo(self, combo: QComboBox, role: str, sources) -> None:
        selection = self.registry.get_selection(role)
        selected_id = str(_source_value(selection, "source_id", "") or "")
        selected_mode = str(_source_value(selection, "mode", "auto"))
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Automàtic", None)
        for source in sources:
            combo.addItem(
                str(_source_value(source, "display_name", self._source_id(source))),
                self._source_id(source),
            )
        if "manual" in selected_mode.lower() and selected_id:
            index = combo.findData(selected_id)
            combo.setCurrentIndex(max(0, index))
        else:
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    @staticmethod
    def _resolution_text(source) -> str:
        value = _source_value(source, "resolution_m", None)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "—"
        return f"{value:g} m" if value > 0 else "—"

    @staticmethod
    def _coverage_text(source) -> str:
        coverage = _source_value(source, "coverage", None)
        if not coverage or len(coverage) != 4:
            return "—"
        west, south, east, north = (float(value) for value in coverage)
        return f"{west:.3f}, {south:.3f} → {east:.3f}, {north:.3f}"

    def _result_text(self, result, fallback: str) -> str:
        source = _source_value(result, "effective", None)
        reason = str(_source_value(result, "reason", "") or "")
        if source is None:
            return f"{fallback} · {reason}" if reason else fallback
        name = str(_source_value(source, "display_name", self._source_id(source)))
        resolution = self._resolution_text(source)
        suffix = f" · {resolution}" if resolution != "—" else ""
        if reason and reason not in {"selected", "automatic"}:
            suffix += f" · {reason}"
        return name + suffix

    def _refresh(self) -> None:
        self._updating = True
        sources = list(self.registry.list_sources())
        elevation = [s for s in sources if s.layer_type == LayerType.ELEVATION]
        surface = [
            s
            for s in sources
            if s.layer_type
            in {LayerType.SURFACE_RGB, LayerType.SURFACE_CATEGORICAL}
        ]
        light = [
            s for s in sources if s.layer_type == LayerType.LIGHT_POLLUTION
        ]
        self._fill_selection_combo(self.combo_elevation, "elevation", elevation)
        self._fill_selection_combo(self.combo_surface, "surface", surface)
        self._fill_selection_combo(
            self.combo_light, "light_pollution", light
        )

        mode = self.registry.representation_mode
        mode_index = self.combo_representation.findData(mode.value)
        self.combo_representation.blockSignals(True)
        self.combo_representation.setCurrentIndex(max(0, mode_index))
        self.combo_representation.blockSignals(False)

        self.tree.clear()
        for source in sources:
            path = str(source.path)
            exists = Path(path).expanduser().exists()
            state = "Activa" if source.enabled else "Desactivada"
            if not exists:
                state = "No disponible"
            elif source.health_status is SourceHealthStatus.PENDING:
                state = "Inspeccionant…"
            elif source.health_status is SourceHealthStatus.INVALID:
                state = "Invàlida"
            item = QTreeWidgetItem(
                [
                    str(source.display_name),
                    _TYPE_LABELS.get(source.layer_type, source.layer_type.value),
                    self._resolution_text(source),
                    str(source.crs or "—"),
                    str(int(source.priority)),
                    state,
                    self._coverage_text(source),
                    path,
                ]
            )
            item.setData(0, Qt.UserRole, self._source_id(source))
            tooltip = "\n".join(
                part
                for part in (
                    str(source.provenance or ""),
                    str(source.attribution or ""),
                    str(source.license or ""),
                    str(source.metadata.get("inspection_error", "") or ""),
                    path,
                )
                if part
            )
            item.setToolTip(0, tooltip)
            self.tree.addTopLevelItem(item)
            if exists and source.health_status in {
                SourceHealthStatus.UNVERIFIED,
                SourceHealthStatus.PENDING,
            }:
                self._schedule_inspection(source)

        elevation_result = self.selection.select_elevation(
            self.latitude, self.longitude
        )
        surface_result = self.selection.select_surface(
            self.latitude, self.longitude
        )
        light_result = self.selection.select_light_pollution(
            self.latitude, self.longitude
        )
        self.lbl_effective_elevation.setText(
            self._result_text(elevation_result, "Horitzó pla")
        )
        self.lbl_effective_surface.setText(
            self._result_text(surface_result, "Paleta sintètica")
        )
        self.lbl_effective_light.setText(
            self._result_text(light_result, "Sense raster (fallback)")
        )
        self._updating = False
        self._update_changes()

    def _selection_changed(self, role: str, combo: QComboBox) -> None:
        if self._updating:
            return
        source_id = combo.currentData()
        if source_id:
            self.registry.set_selection(
                role,
                mode=SelectionMode.MANUAL,
                source_id=str(source_id),
            )
        else:
            self.registry.set_selection(
                role, mode=SelectionMode.AUTOMATIC, source_id=None
            )
        self._refresh()

    def _representation_changed(self, _index: int) -> None:
        if self._updating:
            return
        self.registry.set_representation_mode(
            normalize_terrain_representation_mode(
                self.combo_representation.currentData()
            )
        )
        self._update_changes()

    def _choose_layer_type(self) -> Optional[LayerType]:
        labels = [_TYPE_LABELS[layer_type] for layer_type in _TYPE_LABELS]
        selected, ok = QInputDialog.getItem(
            self,
            "Tipus de font",
            "Quines dades conté?",
            labels,
            0,
            False,
        )
        if not ok:
            return None
        for layer_type, label in _TYPE_LABELS.items():
            if selected == label:
                return layer_type
        return None

    def _add_file(self) -> None:
        layer_type = self._choose_layer_type()
        if layer_type is None:
            return
        filters = {
            LayerType.ELEVATION: "Raster d'elevació (*.tif *.tiff *.asc *.txt *.npy)",
            LayerType.SURFACE_RGB: "Raster RGB/RGBA (*.tif *.tiff *.vrt *.img *.jp2 *.png *.jpg *.jpeg *.webp)",
            LayerType.SURFACE_CATEGORICAL: "Raster categòric (*.tif *.tiff *.vrt *.img *.jp2)",
            LayerType.LIGHT_POLLUTION: "Raster de contaminació lumínica (*.tif *.tiff *.vrt *.img)",
        }
        path, _ = QFileDialog.getOpenFileName(
            self, "Afegir font de dades", "", filters[layer_type]
        )
        if path:
            self._register_path(path, layer_type)

    def _add_folder(self) -> None:
        layer_type = self._choose_layer_type()
        if layer_type is None:
            return
        path = QFileDialog.getExistingDirectory(
            self, "Afegir carpeta o mosaic"
        )
        if path:
            self._register_path(path, layer_type)

    def _register_path(self, path: str, layer_type: LayerType) -> None:
        default_name = Path(path).stem if os.path.isfile(path) else Path(path).name
        name, ok = QInputDialog.getText(
            self, "Nom de la font", "Nom visible:", text=default_name
        )
        if not ok:
            return
        try:
            source = self.registry.register_path(
                path,
                layer_type,
                display_name=str(name or default_name),
                enabled=False,
                metadata={
                    "inspection_status": SourceHealthStatus.PENDING.value,
                    "enable_after_inspection": True,
                },
            )
        except Exception as exc:
            QMessageBox.critical(self, "No s'ha pogut afegir", str(exc))
            return
        self._refresh()
        self._schedule_inspection(source)

    def _schedule_inspection(self, source) -> None:
        source_id = self._source_id(source)
        if not source_id or source_id in self._inspection_jobs:
            return
        task = _InspectionTask(source)
        self._inspection_jobs[source_id] = task
        task.signals.completed.connect(self._inspection_finished)
        self._inspection_pool.start(task)

    def _inspection_finished(
        self, source_id: str, result: object, error: str
    ) -> None:
        self._inspection_jobs.pop(str(source_id), None)
        source = self.registry.get(str(source_id))
        if source is None:
            return
        update_applied = False
        try:
            if result is not None:
                fields = result.registry_fields()
                metadata = dict(fields.get("metadata", {}) or {})
                enable_after = bool(
                    metadata.pop(
                        "enable_after_inspection",
                        source.enabled,
                    )
                )
                metadata.pop("inspection_error", None)
                metadata["inspection_status"] = (
                    SourceHealthStatus.HEALTHY.value
                )
                fields["metadata"] = metadata
                fields["enabled"] = enable_after
                self.registry.update(source.id, **fields)
                update_applied = True
            elif error:
                metadata = dict(source.metadata)
                metadata.pop("inspection_version", None)
                metadata.pop("enable_after_inspection", None)
                metadata["inspection_error"] = str(error)
                metadata["inspection_status"] = (
                    SourceHealthStatus.INVALID.value
                )
                self.registry.update(
                    source.id,
                    metadata=metadata,
                    enabled=False,
                )
                update_applied = True
        except Exception as exc:
            print(f"[DataLayersDialog] Metadata update failed: {exc}")
        self._refresh()
        if update_applied and bool(getattr(self, "_dialog_closed", False)):
            event = getattr(self, "post_close_changes", None)
            publish = getattr(event, "publish", None)
            if callable(publish):
                publish(_changes_for_layer(source.layer_type))

    def _selected_source(self):
        item = self.tree.currentItem()
        if item is None:
            return None
        return self.registry.get(str(item.data(0, Qt.UserRole) or ""))

    def _toggle_selected(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        if source.health_status is SourceHealthStatus.INVALID:
            metadata = dict(source.metadata)
            metadata.pop("inspection_error", None)
            metadata["inspection_status"] = SourceHealthStatus.PENDING.value
            metadata["enable_after_inspection"] = True
            source = self.registry.update(
                self._source_id(source),
                metadata=metadata,
                enabled=False,
            )
            self._refresh()
            self._schedule_inspection(source)
            return
        if source.health_status is SourceHealthStatus.PENDING:
            return
        self.registry.update(self._source_id(source), enabled=not source.enabled)
        self._refresh()

    def _adjust_priority(self, delta: int) -> None:
        source = self._selected_source()
        if source is None:
            return
        self.registry.update(
            self._source_id(source), priority=int(source.priority) + int(delta)
        )
        self._refresh()

    def _remove_selected(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        answer = QMessageBox.question(
            self,
            "Eliminar del catàleg",
            f"Vols retirar «{source.display_name}» del catàleg?\n\n"
            "Els fitxers no s'esborraran del disc.",
        )
        if answer != QMessageBox.Yes:
            return
        self.registry.remove(self._source_id(source))
        self._refresh()

    def _update_changes(self) -> None:
        before_sources, before_selections, before_mode = self._before
        after_sources, after_selections, after_mode = self._snapshot()

        def by_type(signatures, type_name: str) -> tuple:
            return tuple(row for row in signatures if row[1] == type_name)

        selection_before = dict(
            (row[0], row[1:]) for row in before_selections
        )
        selection_after = dict((row[0], row[1:]) for row in after_selections)
        elevation_changed = (
            by_type(before_sources, LayerType.ELEVATION.value)
            != by_type(after_sources, LayerType.ELEVATION.value)
            or selection_before.get("elevation")
            != selection_after.get("elevation")
        )
        surface_types = {
            LayerType.SURFACE_RGB.value,
            LayerType.SURFACE_CATEGORICAL.value,
        }
        surface_before = tuple(
            row for row in before_sources if row[1] in surface_types
        )
        surface_after = tuple(
            row for row in after_sources if row[1] in surface_types
        )
        light_changed = (
            by_type(before_sources, LayerType.LIGHT_POLLUTION.value)
            != by_type(after_sources, LayerType.LIGHT_POLLUTION.value)
            or selection_before.get("light_pollution")
            != selection_after.get("light_pollution")
        )
        self.changes = DataLayerChanges(
            elevation=elevation_changed,
            surface=(
                surface_before != surface_after
                or selection_before.get("surface")
                != selection_after.get("surface")
            ),
            light_pollution=light_changed,
            representation=before_mode != after_mode,
        )
