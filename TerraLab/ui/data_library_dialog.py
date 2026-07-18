"""Mandatory chooser and safe migration flow for the TerraLab data library."""

from __future__ import annotations

import shutil
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QProgressDialog,
    QWidget,
)

from TerraLab.common.data_library import (
    DataLibrary,
    DataLibraryError,
    DataLibraryMigrationCancelled,
    application_state_root,
    configured_data_root,
)


def _human_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"


def _choose_root(parent: QWidget | None, initial: str = "") -> Path | None:
    selected = QFileDialog.getExistingDirectory(
        parent,
        "Tria la biblioteca de dades de TerraLab",
        initial,
        QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
    )
    if not selected:
        return None
    return Path(selected).expanduser().resolve(strict=False)


def _migrate_with_progress(
    library: DataLibrary,
    parent: QWidget | None,
    *,
    source: Path | None = None,
    versioned: bool = False,
) -> object:
    source = Path(source or application_state_root()).resolve(strict=False)
    total = (
        sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
        if versioned
        else library.legacy_managed_size(source)
    )
    progress = QProgressDialog(
        "Copiant i verificant les dades administrades…",
        "Cancel·lar",
        0,
        1000,
        parent,
    )
    progress.setWindowTitle("Migració de la biblioteca")
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.setValue(0)

    def update(done: int, expected: int, current: str) -> None:
        denominator = max(1, expected or total)
        progress.setValue(min(1000, int(1000 * done / denominator)))
        progress.setLabelText(
            f"Copiant i verificant {_human_size(done)} / {_human_size(denominator)}\n{current}"
        )
        QApplication.processEvents()

    try:
        migrate = (
            library.migrate_from_library if versioned else library.migrate_from_legacy
        )
        return migrate(source, progress=update, cancelled=progress.wasCanceled)
    finally:
        progress.close()


def configure_data_library(
    parent: QWidget | None = None,
    *,
    changing: bool = False,
) -> DataLibrary | None:
    """Choose a root, optionally migrate old managed data, and persist it."""

    current = configured_data_root()
    initial = str(current or Path.home())
    while True:
        root = _choose_root(parent, initial)
        if root is None:
            return None
        initial = str(root)
        if root == application_state_root().resolve(strict=False):
            QMessageBox.warning(
                parent,
                "Carpeta no admesa",
                "La biblioteca no pot ser %APPDATA%/TerraLab. Tria una carpeta de dades pròpia.",
            )
            continue
        library = DataLibrary(root)
        try:
            library.validate_root()
        except DataLibraryError as exc:
            QMessageBox.critical(parent, "Biblioteca no vàlida", str(exc))
            continue

        legacy_size = library.legacy_managed_size(application_state_root())
        should_migrate = False
        migrate_source: Path | None = None
        migrate_versioned = False
        if changing and current is not None and Path(current) != root:
            current_library = DataLibrary(current)
            current_size = sum(
                path.stat().st_size
                for path in current_library.root.rglob("*")
                if path.is_file()
            )
            answer = QMessageBox.question(
                parent,
                "Moure la biblioteca",
                f"Vols copiar i verificar {_human_size(current_size)} des de la biblioteca actual?\n\n"
                "Les fonts externes continuaran en la seva ubicació original.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Cancel:
                continue
            should_migrate = answer == QMessageBox.Yes
            migrate_source = current_library.root
            migrate_versioned = True
        elif legacy_size > 0 and not changing:
            answer = QMessageBox.question(
                parent,
                "Migrar dades existents",
                "TerraLab ha trobat "
                f"{_human_size(legacy_size)} de dades administrades a %APPDATA%.\n\n"
                "Vols copiar-les, verificar-les i conservar intacte l'origen?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Cancel:
                continue
            should_migrate = answer == QMessageBox.Yes
        try:
            migration_report = None
            if should_migrate:
                migration_report = _migrate_with_progress(
                    library,
                    parent,
                    source=migrate_source,
                    versioned=migrate_versioned,
                )
            else:
                library.initialize(persist_pointer=True)
        except DataLibraryMigrationCancelled:
            QMessageBox.information(
                parent,
                "Migració cancel·lada",
                "No s'ha canviat la ubicació activa i les dades originals continuen intactes.",
            )
            continue
        except DataLibraryError as exc:
            QMessageBox.critical(parent, "No s'ha pogut preparar la biblioteca", str(exc))
            continue
        if migration_report is not None:
            origin = Path(getattr(migration_report, "source_root"))
            delete_answer = QMessageBox.question(
                parent,
                "Migració verificada",
                "La còpia s'ha completat i verificat. Vols eliminar ara les dades "
                f"administrades de l'origen?\n\n{origin}\n\n"
                "Les fonts externes no s'eliminaran.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if delete_answer == QMessageBox.Yes:
                try:
                    if migrate_versioned:
                        library.cleanup_previous_library(origin)
                    else:
                        library.cleanup_legacy_source(origin)
                except DataLibraryError as exc:
                    QMessageBox.warning(
                        parent,
                        "No s'ha pogut netejar l'origen",
                        "La biblioteca nova ja és vàlida, però no s'ha eliminat "
                        f"tot l'origen:\n{exc}",
                    )
        if changing:
            QMessageBox.information(
                parent,
                "Biblioteca actualitzada",
                "La nova ubicació s'utilitzarà després de reiniciar TerraLab.",
            )
        return library


def ensure_data_library_for_gui(parent: QWidget | None = None) -> DataLibrary:
    """Return the configured library or require the user to choose one."""

    try:
        return DataLibrary.current(require_configured=True, create=True)
    except DataLibraryError as exc:
        try:
            configured = configured_data_root() is not None
        except DataLibraryError:
            configured = True
        if configured:
            QMessageBox.warning(
                parent,
                "Biblioteca inaccessible",
                f"La biblioteca configurada no es pot utilitzar:\n{exc}\n\nTria'n una altra.",
            )
    library = configure_data_library(parent)
    if library is None:
        raise DataLibraryError(
            "Cal triar una biblioteca de dades abans d'iniciar TerraLab."
        )
    return library


def library_free_space(library: DataLibrary) -> int:
    return int(shutil.disk_usage(library.root).free)


__all__ = [
    "configure_data_library",
    "ensure_data_library_for_gui",
    "library_free_space",
]
