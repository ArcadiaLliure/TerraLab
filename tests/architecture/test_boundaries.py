"""Static dependency and canonical-ownership contracts."""

from __future__ import annotations

import ast
from collections import defaultdict
from collections import Counter
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "TerraLab"


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _imports(path: Path) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _assert_no_imports(root: Path, forbidden: tuple[str, ...]) -> None:
    violations: list[str] = []
    for path in _python_files(root):
        for imported in _imports(path):
            if any(
                imported == prefix or imported.startswith(f"{prefix}.")
                for prefix in forbidden
            ):
                violations.append(
                    f"{path.relative_to(ROOT).as_posix()} -> {imported}"
                )
    assert not violations, "\n".join(violations)


def test_domain_and_layer_boundaries() -> None:
    _assert_no_imports(
        PACKAGE / "terrain" / "domain",
        ("PyQt5", "TerraLab.ui", "rasterio", "requests"),
    )
    light_pollution_domain = PACKAGE / "light_pollution" / "domain"
    if light_pollution_domain.exists():
        _assert_no_imports(light_pollution_domain, ("PyQt5", "rasterio"))
    _assert_no_imports(PACKAGE / "data", ("TerraLab.ui",))
    _assert_no_imports(PACKAGE / "scene", ("TerraLab.ui",))
    _assert_no_imports(PACKAGE / "terrain", ("TerraLab.ui",))
    _assert_no_imports(PACKAGE, ("TerraLab.tools",))
    _assert_no_imports(PACKAGE / "ui", ("scripts", "tools"))


def test_phase_02_render_contracts_do_not_depend_on_qt_or_backends() -> None:
    contracts = PACKAGE / "scene" / "contracts.py"
    forbidden = (
        "PyQt5",
        "TerraLab.application",
        "TerraLab.render",
        "TerraLab.runtime",
        "TerraLab.ui",
    )
    imports = _imports(contracts)
    assert not [
        imported
        for imported in imports
        if any(
            imported == prefix or imported.startswith(f"{prefix}.")
            for prefix in forbidden
        )
    ]
    _assert_no_imports(
        PACKAGE / "application",
        ("PyQt5", "TerraLab.render", "TerraLab.ui"),
    )
    removed_render_runtime = (
        PACKAGE / "runtime" / "offscreen_renderer.py",
        PACKAGE / "runtime" / "render_service.py",
        PACKAGE / "runtime" / "hosted_surface_service.py",
    )
    assert not [path for path in removed_render_runtime if path.exists()]


def test_final_core_and_infrastructure_boundaries_are_graphics_free() -> None:
    """Qt may exist only below the selected PyQt presentation adapter."""

    forbidden = (
        "PyQt5",
        "PySide",
        "Qt",
        "TerraLab.ui",
        "TerraLab.widgets",
    )
    for relative in (
        "core",
        "application",
        "infrastructure",
    ):
        path = PACKAGE / relative
        if path.exists():
            _assert_no_imports(path, forbidden)

    legacy_render_modules = (
        PACKAGE / "runtime" / "offscreen_renderer.py",
        PACKAGE / "runtime" / "render_service.py",
        PACKAGE / "runtime" / "hosted_surface_service.py",
        PACKAGE / "infrastructure" / "runtime" / "render_service.py",
        PACKAGE / "infrastructure" / "runtime" / "hosted_surface_service.py",
    )
    assert not [path for path in legacy_render_modules if path.exists()]


def test_ui_process_cannot_import_science_or_render_implementations() -> None:
    _assert_no_imports(
        PACKAGE / "ui",
        (
            "TerraLab.render",
            "TerraLab.terrain",
            "TerraLab.data.catalogs",
            "numpy",
            "rasterio",
            "skyfield",
        ),
    )


def test_ui_process_never_uses_blocking_qprocess_waits() -> None:
    violations: list[str] = []
    for path in _python_files(PACKAGE / "ui"):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("waitFor")
            ):
                violations.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                    f"{node.func.attr}"
                )
    assert not violations, "\n".join(violations)


def test_ui_has_no_calculation_threads_or_fixed_16ms_render_loop() -> None:
    violations: list[str] = []
    for path in _python_files(PACKAGE / "ui"):
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [alias.name for alias in node.names]
                )
                if "QThread" in names:
                    violations.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                        "QThread"
                    )
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"start", "setInterval"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == 16
            ):
                continue
            violations.append(
                f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                f"{node.func.attr}(16)"
            )
    assert not violations, "\n".join(violations)


def test_canonical_runtime_types_have_one_definition() -> None:
    expected = {
        "AstronomicalWidget": PACKAGE / "ui" / "astronomical_widget.py",
        "AstroCanvas": PACKAGE / "ui" / "astro_canvas.py",
        "ByteLRU": PACKAGE / "common" / "cache.py",
        "GenerationController": PACKAGE / "common" / "cancellation.py",
    }
    definitions: dict[str, list[Path]] = defaultdict(list)
    for path in _python_files(PACKAGE):
        for node in _tree(path).body:
            if isinstance(node, ast.ClassDef) and node.name in expected:
                definitions[node.name].append(path)

    for name, canonical_path in expected.items():
        assert definitions[name] == [canonical_path], (
            f"{name} definitions: "
            f"{[path.relative_to(ROOT).as_posix() for path in definitions[name]]}"
        )
    removed_bridges = (
        PACKAGE / "ui" / "sky_widget.py",
        PACKAGE / "ui" / "sky_widget_impl.py",
        PACKAGE / "widgets" / "sky_widget.py",
        PACKAGE / "widgets" / "sky_legacy_components.py",
        PACKAGE / "widgets" / "scope_runtime_cache.py",
    )
    assert not [path for path in removed_bridges if path.exists()]


def test_terrain_worker_has_one_runtime_owner() -> None:
    coordinator = PACKAGE / "terrain" / "terrain_coordinator.py"
    constructors: list[str] = []
    for path in _python_files(PACKAGE):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "HorizonWorker"
            ):
                constructors.append(path.relative_to(ROOT).as_posix())
    assert constructors == [coordinator.relative_to(ROOT).as_posix()]
    ui_sources = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in _python_files(PACKAGE / "ui")
    )
    assert "horizon_worker" not in ui_sources
    assert "horizon_thread" not in ui_sources


def test_renderers_do_not_expose_parallel_impl_functions() -> None:
    renderer_paths = [
        PACKAGE / "render" / name
        for name in (
            "grid_renderer.py",
            "horizon_renderer.py",
            "overlays_renderer.py",
            "sky_renderer.py",
            "stars_renderer.py",
        )
    ]
    violations = [
        f"{path.name}:{node.lineno}:{node.name}"
        for path in renderer_paths
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.endswith("_impl")
    ]
    assert not violations, "\n".join(violations)


def test_packages_do_not_use_numbered_fragments_or_dynamic_namespace_copying() -> (
    None
):
    source_roots = (PACKAGE, ROOT / "tests")
    numbered = [
        path.relative_to(ROOT).as_posix()
        for source_root in source_roots
        for path in _python_files(source_root)
        if path.stem.startswith("part_") and path.stem[5:].isdigit()
    ]
    numbered_imports: list[str] = []
    namespace_copying: list[str] = []
    for path in (
        path
        for source_root in source_roots
        for path in _python_files(source_root)
    ):
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported_names = [node.module or ""] + [
                    alias.name for alias in node.names
                ]
            else:
                imported_names = []
            if any(
                any(
                    part.startswith("part_") and part[5:].isdigit()
                    for part in imported.split(".")
                )
                for imported in imported_names
            ):
                numbered_imports.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                )
            if not isinstance(node, ast.Call):
                if (
                    isinstance(node, (ast.Assign, ast.AnnAssign))
                    and isinstance(
                        node.targets[0]
                        if isinstance(node, ast.Assign)
                        else node.target,
                        ast.Subscript,
                    )
                    and isinstance(
                        (
                            node.targets[0]
                            if isinstance(node, ast.Assign)
                            else node.target
                        ).value,
                        ast.Call,
                    )
                    and isinstance(
                        (
                            node.targets[0]
                            if isinstance(node, ast.Assign)
                            else node.target
                        ).value.func,
                        ast.Name,
                    )
                    and (
                        node.targets[0]
                        if isinstance(node, ast.Assign)
                        else node.target
                    ).value.func.id
                    == "globals"
                ):
                    namespace_copying.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                    )
                continue
            calls_globals = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "globals"
            )
            if calls_globals:
                namespace_copying.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                )
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "vars"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id.startswith("_runtime")
            ):
                namespace_copying.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                )
    assert numbered == []
    assert numbered_imports == []
    assert namespace_copying == []


def test_shared_star_catalog_limit_has_one_owner() -> None:
    assignments: list[str] = []
    for path in _python_files(PACKAGE):
        for node in ast.walk(_tree(path)):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            if any(
                isinstance(target, ast.Name)
                and target.id == "STAR_CATALOG_NAKED_EYE_MAX_MAG"
                for target in targets
            ):
                assignments.append(path.relative_to(ROOT).as_posix())
    assert assignments == ["TerraLab/data/constants.py"]


def test_pure_terrain_render_does_not_perform_file_io() -> None:
    violations: list[str] = []
    for path in _python_files(PACKAGE / "terrain" / "render"):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                violations.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:open"
                )
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "open",
                "read_bytes",
                "read_text",
                "write_bytes",
                "write_text",
            }:
                violations.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                    f"{node.func.attr}"
                )
    assert violations == []


def test_silent_generic_exception_debt_does_not_increase() -> None:
    baseline_path = (
        ROOT / "docs" / "architecture" / "silent_exception_baseline.json"
    )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    allowed = Counter(baseline["file_counts"])
    current: Counter[str] = Counter()
    for path in _python_files(PACKAGE):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.ExceptHandler)
                and isinstance(node.type, ast.Name)
                and node.type.id == "Exception"
                and len(node.body) == 1
                and isinstance(node.body[0], ast.Pass)
            ):
                current[path.relative_to(ROOT).as_posix()] += 1
    violations = {
        path: {"allowed": allowed[path], "current": count}
        for path, count in current.items()
        if count > allowed[path]
    }
    assert not violations, violations
    assert sum(current.values()) <= int(baseline["total"])


def test_runtime_readers_never_enable_numpy_pickle() -> None:
    violations: list[str] = []
    for source_root in (PACKAGE, ROOT / "scripts"):
        for path in _python_files(source_root):
            for node in ast.walk(_tree(path)):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if (
                        keyword.arg == "allow_pickle"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        violations.append(
                            f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                        )
    assert not violations, violations


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_internal_import(name: str, modules: set[str]) -> str | None:
    candidate = name
    while candidate:
        if candidate in modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def _cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(edges.get(node, ())):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1 or (
            len(component) == 1 and component[0] in edges.get(component[0], ())
        ):
            result.append(sorted(component))

    for node in sorted(edges):
        if node not in indices:
            visit(node)
    return sorted(result)


def test_product_module_graph_is_acyclic() -> None:
    paths = _python_files(PACKAGE)
    path_by_module = {_module_name(path): path for path in paths}
    modules = set(path_by_module)
    edges: dict[str, set[str]] = defaultdict(set)
    for module, path in path_by_module.items():
        for imported in _imports(path):
            target = _resolve_internal_import(imported, modules)
            if target and target != module:
                edges[module].add(target)
        edges.setdefault(module, set())
    assert _cycles(edges) == []
