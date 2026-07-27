"""Generate TerraLab's reproducible architecture inventories.

The command intentionally uses only the Python standard library so it can run
before the development extras are installed:

    python tools/dev/code_inventory.py

Only files tracked by Git or visible as non-ignored untracked files are
inventoried. Generated/ignored caches therefore cannot make the result depend
on a developer's machine.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence


INVENTORY_COLUMNS = (
    "path",
    "type",
    "size_bytes",
    "lines",
    "package",
    "runtime_or_dev",
    "imports_in_count",
    "imports_out_count",
    "public_symbols",
    "entrypoint",
    "dynamic_reference_risk",
    "side_effect_on_import",
    "network_access",
    "filesystem_write",
    "starts_thread_or_process",
    "qt_dependency",
    "tests_covering_file",
    "responsibility",
    "current_status",
    "proposed_action",
    "destination",
    "confidence",
    "evidence",
)

DEV_PREFIXES = (
    ".github/",
    ".vscode/",
    "benchmarks/",
    "build/",
    "dist/",
    "docs/",
    "plans/",
    "proves/",
    "scripts/",
    "tests/",
    "tools/",
)
GENERATED_PREFIXES = ("build/", "dist/", "TerraLab.egg-info/")
PYTHON_SUFFIXES = {".py", ".pyi"}
BINARY_SUFFIXES = {
    ".bsp",
    ".db",
    ".fits",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".npy",
    ".npz",
    ".pdf",
    ".png",
    ".tif",
    ".tiff",
    ".ttf",
    ".webp",
    ".xlsx",
    ".zip",
    ".zst",
}
NETWORK_PATTERN = re.compile(
    r"\b(requests|urllib|httpx|aiohttp|socket|cdsapi)\b|https?://",
    re.IGNORECASE,
)
WRITE_PATTERN = re.compile(
    r"\.(write|write_text|write_bytes|mkdir|replace|unlink|rename)\s*\("
    r"|open\s*\([^,\n]+,\s*[\"'][wax+]"
    r"|to_(csv|json|parquet|pickle)\s*\(",
)
CONCURRENCY_PATTERN = re.compile(
    r"\b(Thread|Process|Popen|ThreadPoolExecutor|ProcessPoolExecutor)\s*\("
    r"|\.submit\s*\(",
)
DYNAMIC_PATTERN = re.compile(
    r"\b(getattr|setattr|hasattr|import_module|find_class|load_class)\s*\("
    r"|\.connect\s*\(|\.emit\s*\(|singleShot\s*\(|__all__",
)
IMPORT_SIDE_EFFECT_PATTERN = re.compile(
    r"^\s*(?:register_[A-Za-z_]+\s*\(|Path\([^)]*\)\.mkdir\s*\(|"
    r"open\s*\(|[A-Za-z_][A-Za-z0-9_.]*\.start\s*\()",
    re.MULTILINE,
)
COMPATIBILITY_PATTERN = re.compile(
    r"\b(legacy|deprecated|compat(?:ibility)?|shim|alias|temporary|old)\b",
    re.IGNORECASE,
)
EXCEPT_SWALLOW_PATTERN = re.compile(
    r"except(?:\s+Exception)?(?:\s+as\s+\w+)?:\s*(?:#.*\n\s*)?"
    r"(?:pass|return(?:\s+None)?)\b",
)


def _git_paths(root: Path) -> list[str]:
    command = [
        "git",
        "-C",
        str(root),
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return sorted({line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()})


def _module_name(relative_path: str) -> str | None:
    path = Path(relative_path)
    if path.suffix != ".py":
        return None
    parts = list(path.with_suffix("").parts)
    if not parts or parts[0] not in {"TerraLab", "scripts"}:
        return None
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _line_count(path: Path, is_binary: bool) -> int:
    if is_binary:
        return 0
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _expression_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expression_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Call):
        return _expression_name(node.func)
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__


def _cyclomatic_complexity(node: ast.AST) -> int:
    branches = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.TryStar,
        ast.With,
        ast.AsyncWith,
        ast.IfExp,
        ast.Match,
        ast.comprehension,
    )
    score = 1
    for child in ast.walk(node):
        if isinstance(child, branches):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(0, len(child.values) - 1)
        elif isinstance(child, ast.ExceptHandler):
            score += 1
    return score


def _assigned_attributes(node: ast.AST) -> list[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets: list[ast.AST]
            if isinstance(child, ast.Assign):
                targets = list(child.targets)
            else:
                targets = [child.target]
            for target in targets:
                for nested in ast.walk(target):
                    if (
                        isinstance(nested, ast.Attribute)
                        and isinstance(nested.value, ast.Name)
                        and nested.value.id == "self"
                    ):
                        names.add(nested.attr)
    return sorted(names)


def _unreachable_lines(node: ast.AST) -> list[int]:
    result: list[int] = []

    def inspect_body(body: Sequence[ast.stmt]) -> None:
        terminated = False
        for statement in body:
            if terminated:
                result.append(int(getattr(statement, "lineno", 0)))
            if isinstance(statement, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                terminated = True
            for field_name in ("body", "orelse", "finalbody"):
                nested = getattr(statement, field_name, None)
                if isinstance(nested, list):
                    inspect_body(nested)
            if isinstance(statement, (ast.Try, ast.TryStar)):
                for handler in statement.handlers:
                    inspect_body(handler.body)
            if isinstance(statement, ast.Match):
                for case in statement.cases:
                    inspect_body(case.body)

    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        inspect_body(node.body)
    return sorted({line for line in result if line > 0})


def _is_passthrough_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = [
        statement
        for statement in node.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    return isinstance(body[0].value, (ast.Call, ast.Await))


def _symbol_kind(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> str:
    parent = parents.get(node)
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(parent, ast.ClassDef):
        return "method"
    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "nested_function"
    return "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"


@dataclass
class SymbolRecord:
    qualified_name: str
    name: str
    kind: str
    module: str
    path: str
    line: int
    end_line: int
    lines: int
    decorators: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    complexity: int = 1
    mutated_attributes: list[str] = field(default_factory=list)
    uses_globals: list[str] = field(default_factory=list)
    try_blocks: int = 0
    broad_exceptions: int = 0
    swallowed_exceptions: int = 0
    nested_functions: int = 0
    unreachable_lines: list[int] = field(default_factory=list)
    constant_condition_lines: list[int] = field(default_factory=list)
    passthrough: bool = False
    compatibility_only_risk: bool = False
    static_reference_count: int = 0
    instantiated_count: int = 0
    dynamic_reference_risk: bool = False
    public: bool = False


@dataclass
class ModuleRecord:
    module: str
    path: str
    imports: list[str]
    imported_internal_modules: list[str]
    classes: list[str]
    functions: list[str]
    methods: list[str]
    syntax_error: str | None
    module_unreachable_lines: list[int]
    constant_condition_lines: list[int]
    broad_exceptions: int
    swallowed_exceptions: int
    signal_definitions: list[str]
    connected_slots: list[str]
    emitted_signals: list[str]
    callback_parameters: list[str]


def _parse_python(
    relative_path: str,
    text: str,
) -> tuple[ModuleRecord, list[SymbolRecord]]:
    module = _module_name(relative_path) or relative_path
    try:
        tree = ast.parse(text, filename=relative_path)
    except SyntaxError as exc:
        return (
            ModuleRecord(
                module=module,
                path=relative_path,
                imports=[],
                imported_internal_modules=[],
                classes=[],
                functions=[],
                methods=[],
                syntax_error=f"{exc.msg} at {exc.lineno}:{exc.offset}",
                module_unreachable_lines=[],
                constant_condition_lines=[],
                broad_exceptions=0,
                swallowed_exceptions=0,
                signal_definitions=[],
                connected_slots=[],
                emitted_signals=[],
                callback_parameters=[],
            ),
            [],
        )

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    internal = sorted({name for name in imports if name == "TerraLab" or name.startswith("TerraLab.")})

    constant_lines = sorted(
        {
            int(node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, (ast.If, ast.While))
            and isinstance(node.test, ast.Constant)
        }
    )
    broad_exceptions = 0
    swallowed_exceptions = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None or _expression_name(node.type) in {"Exception", "BaseException"}:
            broad_exceptions += 1
        meaningful = [
            statement
            for statement in node.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        if meaningful and all(
            isinstance(statement, (ast.Pass, ast.Return)) for statement in meaningful
        ):
            swallowed_exceptions += 1

    classes: list[str] = []
    functions: list[str] = []
    methods: list[str] = []
    symbols: list[SymbolRecord] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        kind = _symbol_kind(node, parents)
        if kind == "class":
            classes.append(node.name)
        elif kind == "method":
            methods.append(node.name)
        elif kind in {"function", "async_function"}:
            functions.append(node.name)
        owners: list[str] = []
        current = parents.get(node)
        while current is not None:
            if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                owners.append(current.name)
            current = parents.get(current)
        owners.reverse()
        qualified = ".".join([module, *owners, node.name])
        node_try = [child for child in ast.walk(node) if isinstance(child, (ast.Try, ast.TryStar))]
        node_handlers = [
            child for child in ast.walk(node) if isinstance(child, ast.ExceptHandler)
        ]
        node_broad = sum(
            handler.type is None
            or _expression_name(handler.type) in {"Exception", "BaseException"}
            for handler in node_handlers
        )
        node_swallowed = sum(
            bool(handler.body)
            and all(isinstance(statement, (ast.Pass, ast.Return)) for statement in handler.body)
            for handler in node_handlers
        )
        global_names = sorted(
            {
                name
                for child in ast.walk(node)
                if isinstance(child, ast.Global)
                for name in child.names
            }
        )
        symbols.append(
            SymbolRecord(
                qualified_name=qualified,
                name=node.name,
                kind=kind,
                module=module,
                path=relative_path,
                line=int(getattr(node, "lineno", 0)),
                end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
                lines=max(
                    1,
                    int(getattr(node, "end_lineno", getattr(node, "lineno", 0)))
                    - int(getattr(node, "lineno", 0))
                    + 1,
                ),
                decorators=[_expression_name(item) for item in node.decorator_list],
                bases=(
                    [_expression_name(base) for base in node.bases]
                    if isinstance(node, ast.ClassDef)
                    else []
                ),
                complexity=_cyclomatic_complexity(node),
                mutated_attributes=_assigned_attributes(node),
                uses_globals=global_names,
                try_blocks=len(node_try),
                broad_exceptions=node_broad,
                swallowed_exceptions=node_swallowed,
                nested_functions=sum(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child is not node
                    for child in ast.walk(node)
                ),
                unreachable_lines=_unreachable_lines(node),
                constant_condition_lines=sorted(
                    {
                        int(child.lineno)
                        for child in ast.walk(node)
                        if isinstance(child, (ast.If, ast.While))
                        and isinstance(child.test, ast.Constant)
                    }
                ),
                passthrough=(
                    _is_passthrough_method(node)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else False
                ),
                compatibility_only_risk=bool(
                    COMPATIBILITY_PATTERN.search(
                        (ast.get_docstring(node) or "") + " " + node.name
                    )
                ),
                dynamic_reference_risk=bool(
                    re.search(
                        rf"[\"']{re.escape(node.name)}[\"']",
                        text,
                    )
                ),
                public=not node.name.startswith("_"),
            )
        )

    signal_definitions = sorted(
        {
            node.targets[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and _expression_name(node.value.func).endswith("pyqtSignal")
        }
        | {
            node.target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Call)
            and _expression_name(node.value.func).endswith("pyqtSignal")
        }
    )
    connected_slots = sorted(
        {
            _expression_name(node.args[0])
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _expression_name(node.func).endswith(".connect")
            and node.args
        }
    )
    emitted_signals = sorted(
        {
            _expression_name(node.func).removesuffix(".emit")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _expression_name(node.func).endswith(".emit")
        }
    )
    callback_parameters = sorted(
        {
            argument.arg
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if argument.arg.endswith(("_callback", "_fn", "_handler"))
            or argument.arg in {"callback", "progress", "on_progress"}
        }
    )
    return (
        ModuleRecord(
            module=module,
            path=relative_path,
            imports=sorted(set(imports)),
            imported_internal_modules=internal,
            classes=sorted(set(classes)),
            functions=sorted(set(functions)),
            methods=sorted(set(methods)),
            syntax_error=None,
            module_unreachable_lines=_unreachable_lines(tree),
            constant_condition_lines=constant_lines,
            broad_exceptions=broad_exceptions,
            swallowed_exceptions=swallowed_exceptions,
            signal_definitions=signal_definitions,
            connected_slots=connected_slots,
            emitted_signals=emitted_signals,
            callback_parameters=callback_parameters,
        ),
        symbols,
    )


def _resolved_internal_import(name: str, known_modules: set[str]) -> str | None:
    candidate = name
    while candidate:
        if candidate in known_modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def _find_cycles(edges: dict[str, list[str]]) -> list[list[str]]:
    """Return cyclic strongly-connected components using Tarjan's algorithm."""

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in edges.get(node, []):
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
        component.sort()
        is_self_cycle = len(component) == 1 and component[0] in edges.get(
            component[0], []
        )
        if len(component) > 1 or is_self_cycle:
            components.append(component)

    for module in sorted(edges):
        if module not in indices:
            visit(module)
    return sorted(components)


def _package_for_path(path: str) -> str:
    parts = Path(path).parts
    if path.startswith("TerraLab/") and len(parts) > 1:
        return ".".join(parts[:2]) if len(parts) > 2 else "TerraLab"
    return parts[0] if parts else ""


def _responsibility(path: str, text: str) -> str:
    if path in {"pyproject.toml", "README.md", "LICENSE"}:
        return {
            "pyproject.toml": "Canonical packaging metadata and tool configuration.",
            "README.md": "Project overview, installation and supported operations.",
            "LICENSE": "Project licence.",
        }[path]
    if path.startswith("tests/"):
        return "Automated characterization or contract test."
    if path.startswith("docs/"):
        return "Project documentation or architecture evidence."
    if path.startswith("benchmarks/") or "/benchmarks/" in path:
        return "Reproducible performance measurement."
    if path.startswith("scripts/") or path.startswith("tools/"):
        return "Command-line or developer tooling."
    if path.startswith("TerraLab/"):
        try:
            tree = ast.parse(text)
            doc = ast.get_docstring(tree)
            if doc:
                return " ".join(doc.strip().split())[:240]
        except SyntaxError:
            pass
        subsystem = Path(path).parts[1] if len(Path(path).parts) > 1 else "application"
        return f"TerraLab {subsystem} runtime."
    return "Repository support or data file."


def _proposed_action(path: str, text: str, lines: int) -> tuple[str, str, float, str]:
    destination = ""
    evidence: list[str] = []
    confidence = 0.75
    if path.startswith(GENERATED_PREFIXES):
        return "DELETE_CONFIRMED", "", 1.0, "Generated build/install artifact; source exists elsewhere."
    explicit: dict[str, tuple[str, str]] = {
        "setup.py": ("DELETE_CONFIRMED", "pyproject.toml"),
        "environment.yml": ("DELETE_CONFIRMED", "pyproject.toml"),
        "test_get_elevation_by_gps.py": (
            "DELETE_AFTER_MIGRATION",
            "TerraLab/cli/query_elevation.py",
        ),
        "TerraLab/common/runtime_cache.py": (
            "DELETE_AFTER_MIGRATION",
            "TerraLab/common/cache.py; TerraLab/common/cancellation.py",
        ),
        "TerraLab/ui/sky_widget_impl.py": (
            "DELETE_AFTER_MIGRATION",
            "TerraLab/ui/astronomical_widget.py; TerraLab/ui/astro_canvas.py",
        ),
        "TerraLab/widgets/sky_legacy_components.py": (
            "DELETE_AFTER_MIGRATION",
            "TerraLab/astro; TerraLab/data/catalogs; TerraLab/ui/workers",
        ),
        "TerraLab/widgets/sky_widget.py": (
            "DELETE_AFTER_MIGRATION",
            "TerraLab/ui/astronomical_widget.py",
        ),
        "TerraLab/tools/python_classes_methods.py": (
            "DELETE_AFTER_MIGRATION",
            "tools/dev/code_inventory.py",
        ),
        "TerraLab/tools/python_cognitive_summary.py": (
            "DELETE_AFTER_MIGRATION",
            "tools/dev/code_inventory.py",
        ),
        "TerraLab/tools/python_tree.py": (
            "DELETE_AFTER_MIGRATION",
            "tools/dev/code_inventory.py",
        ),
    }
    if path in explicit:
        action, destination = explicit[path]
        return action, destination, 0.98, "Explicitly identified migration target in the refactor brief."
    if Path(path).suffix.lower() in BINARY_SUFFIXES:
        return "REVIEW_BINARY", "", 0.9, "Binary/data asset requires consumer and packaging review."
    if path.startswith(".vscode/"):
        return "REVIEW_PUBLIC_API", "", 0.8, "Developer-local editor configuration."
    if path.endswith("__init__.py"):
        return "REVIEW_PUBLIC_API", "", 0.85, "Package public surface must be verified before changes."
    if path.startswith("TerraLab/tools/"):
        destination = path.removeprefix("TerraLab/")
        return "MOVE", destination, 0.9, "Developer tooling is currently inside the distributable package."
    if path.startswith("scripts/"):
        destination = "TerraLab/cli/" + Path(path).name
        return "MOVE", destination, 0.85, "Supported entry points must live in TerraLab.cli; others require classification."
    if path.startswith("TerraLab/") and lines > 800:
        evidence.append(f"Productive module has {lines} lines (>800 target).")
        return "SPLIT", "", 0.9, " ".join(evidence)
    if COMPATIBILITY_PATTERN.search(text):
        evidence.append("Contains compatibility/legacy/deprecation markers.")
        confidence = 0.72
        return "REFACTOR_IN_PLACE", "", confidence, " ".join(evidence)
    return "KEEP", "", confidence, "No confirmed replacement or deletion evidence."


def _file_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in PYTHON_SUFFIXES:
        return "python"
    if suffix in BINARY_SUFFIXES:
        return "binary"
    if suffix in {".md", ".rst", ".txt"}:
        return "documentation"
    if suffix in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"}:
        return "configuration"
    if not suffix:
        return "text"
    return suffix.removeprefix(".")


def _entrypoint_paths(root: Path) -> tuple[set[str], dict[str, str]]:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return set(), {}
    text = _safe_text(pyproject)
    scripts: dict[str, str] = {}
    in_scripts = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_scripts = stripped == "[project.scripts]"
            continue
        if not in_scripts or "=" not in line:
            continue
        name, value = line.split("=", 1)
        target = value.strip().strip("\"'")
        scripts[name.strip()] = target
    paths: set[str] = set()
    for target in scripts.values():
        module = target.partition(":")[0]
        candidate = module.replace(".", "/") + ".py"
        paths.add(candidate)
    return paths, scripts


def generate(root: Path, output_dir: Path) -> dict[str, Any]:
    root = root.resolve()
    paths = _git_paths(root)
    entrypoint_paths, entrypoints = _entrypoint_paths(root)
    texts: dict[str, str] = {}
    modules: dict[str, ModuleRecord] = {}
    symbols: list[SymbolRecord] = []

    for relative in paths:
        absolute = root / relative
        if not absolute.exists() or absolute.is_dir():
            continue
        if absolute.suffix.lower() in PYTHON_SUFFIXES:
            text = _safe_text(absolute)
            texts[relative] = text
            module_record, module_symbols = _parse_python(relative, text)
            modules[module_record.module] = module_record
            symbols.extend(module_symbols)
        elif absolute.suffix.lower() not in BINARY_SUFFIXES:
            texts[relative] = _safe_text(absolute)

    known_modules = set(modules)
    dependency_edges: dict[str, list[str]] = {}
    inbound: Counter[str] = Counter()
    for module, record in modules.items():
        resolved = sorted(
            {
                target
                for imported in record.imported_internal_modules
                if (target := _resolved_internal_import(imported, known_modules))
                and target != module
            }
        )
        dependency_edges[module] = resolved
        inbound.update(resolved)

    complete_python_text = "\n".join(
        texts[path] for path in texts if path.endswith(".py")
    )
    identifier_counts = Counter(
        re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", complete_python_text)
    )
    quoted_names = set(
        re.findall(
            r"(?:getattr|setattr|hasattr)\s*\([^,\n]+,\s*[\"']"
            r"([A-Za-z_][A-Za-z0-9_]*)[\"']",
            complete_python_text,
        )
    )
    call_counter = Counter(
        node.func.id
        for path, text in texts.items()
        if path.endswith(".py")
        for tree in [ast.parse(text, filename=path)]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    )
    for symbol in symbols:
        total = identifier_counts[symbol.name]
        symbol.static_reference_count = max(0, total - 1)
        symbol.instantiated_count = (
            call_counter[symbol.name] if symbol.kind == "class" else 0
        )
        symbol.dynamic_reference_risk = (
            symbol.dynamic_reference_risk or symbol.name in quoted_names
        )

    module_by_path = {record.path: record for record in modules.values()}
    test_texts = {
        path: text for path, text in texts.items() if path.startswith("tests/") and path.endswith(".py")
    }
    rows: list[dict[str, Any]] = []
    for relative in paths:
        absolute = root / relative
        if not absolute.exists() or absolute.is_dir():
            continue
        suffix = absolute.suffix.lower()
        is_binary = suffix in BINARY_SUFFIXES
        text = texts.get(relative, "")
        lines = _line_count(absolute, is_binary)
        module_record = module_by_path.get(relative)
        module_name = module_record.module if module_record else None
        public_symbols = []
        if module_record:
            public_symbols = sorted(
                {
                    symbol.name
                    for symbol in symbols
                    if symbol.path == relative
                    and symbol.kind in {"class", "function", "async_function"}
                    and symbol.public
                }
            )
        tests_covering: list[str] = []
        if module_name:
            stem = Path(relative).stem
            tests_covering = sorted(
                path
                for path, test_text in test_texts.items()
                if module_name in test_text
                or re.search(rf"\b{re.escape(stem)}\b", test_text)
            )
        action, destination, confidence, action_evidence = _proposed_action(
            relative, text, lines
        )
        side_effect = bool(
            relative.endswith(".py") and IMPORT_SIDE_EFFECT_PATTERN.search(text)
        )
        dynamic_risk = bool(DYNAMIC_PATTERN.search(text))
        evidence_parts = [action_evidence]
        if side_effect:
            evidence_parts.append("Potential top-level runtime call detected.")
        if module_record and module_record.syntax_error:
            evidence_parts.append(f"AST error: {module_record.syntax_error}")
        if module_record and module_record.broad_exceptions:
            evidence_parts.append(
                f"{module_record.broad_exceptions} broad exception handlers."
            )
        rows.append(
            {
                "path": relative,
                "type": _file_type(relative),
                "size_bytes": absolute.stat().st_size,
                "lines": lines,
                "package": _package_for_path(relative),
                "runtime_or_dev": (
                    "generated"
                    if relative.startswith(GENERATED_PREFIXES)
                    else "dev"
                    if relative.startswith(DEV_PREFIXES)
                    else "runtime"
                    if relative.startswith("TerraLab/")
                    else "repository"
                ),
                "imports_in_count": int(inbound.get(module_name or "", 0)),
                "imports_out_count": (
                    len(dependency_edges.get(module_name or "", []))
                ),
                "public_symbols": ";".join(public_symbols),
                "entrypoint": (
                    ";".join(
                        name
                        for name, target in entrypoints.items()
                        if target.partition(":")[0] == module_name
                    )
                    if relative in entrypoint_paths or module_name
                    else ""
                ),
                "dynamic_reference_risk": str(dynamic_risk).lower(),
                "side_effect_on_import": str(side_effect).lower(),
                "network_access": str(bool(NETWORK_PATTERN.search(text))).lower(),
                "filesystem_write": str(bool(WRITE_PATTERN.search(text))).lower(),
                "starts_thread_or_process": str(
                    bool(CONCURRENCY_PATTERN.search(text))
                ).lower(),
                "qt_dependency": str(
                    "PyQt5" in text or "PyQtWebEngine" in text
                ).lower(),
                "tests_covering_file": ";".join(tests_covering),
                "responsibility": _responsibility(relative, text),
                "current_status": (
                    "generated_duplicate"
                    if relative.startswith(GENERATED_PREFIXES)
                    else "compatibility_risk"
                    if COMPATIBILITY_PATTERN.search(text)
                    else "oversized"
                    if relative.startswith("TerraLab/") and lines > 800
                    else "active_or_unconfirmed"
                ),
                "proposed_action": action,
                "destination": destination,
                "confidence": f"{confidence:.2f}",
                "evidence": " ".join(part for part in evidence_parts if part),
            }
        )

    cycles = _find_cycles(dependency_edges)
    candidates: list[dict[str, Any]] = []
    for symbol in symbols:
        reasons: list[str] = []
        level = ""
        if symbol.static_reference_count == 0:
            reasons.append("No static name reference outside the definition.")
            level = "C" if symbol.dynamic_reference_risk else "A"
        if symbol.kind == "class" and symbol.instantiated_count == 0:
            reasons.append("No direct constructor call found.")
            level = level or ("C" if symbol.dynamic_reference_risk else "A")
        if symbol.passthrough:
            reasons.append("Single-call pass-through wrapper.")
            level = level or "B"
        if symbol.compatibility_only_risk:
            reasons.append("Compatibility/deprecation marker.")
            level = level or "B"
        if symbol.unreachable_lines:
            reasons.append(
                "Statements follow unconditional control transfer at lines "
                + ", ".join(map(str, symbol.unreachable_lines))
                + "."
            )
            level = level or "A"
        if reasons:
            candidates.append(
                {
                    "symbol": symbol.qualified_name,
                    "path": symbol.path,
                    "line": symbol.line,
                    "level": level,
                    "reasons": reasons,
                    "static_reference_count": symbol.static_reference_count,
                    "instantiated_count": symbol.instantiated_count,
                    "dynamic_reference_risk": symbol.dynamic_reference_risk,
                    "required_review": [
                        "direct and deferred imports",
                        "__all__ and entry points",
                        "Qt signal/slot and timer strings",
                        "thread/process/subprocess targets",
                        "configuration, manifests and serialization",
                        "tests, docs and PyInstaller hidden imports",
                    ],
                    "automatic_deletion_allowed": False,
                }
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    file_inventory = output_dir / "refactor_file_inventory.csv"
    with file_inventory.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INVENTORY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    symbol_payload = {
        "schema_version": 1,
        "generated_by": "tools/dev/code_inventory.py",
        "repository_root": ".",
        "modules": [asdict(modules[name]) for name in sorted(modules)],
        "symbols": [
            asdict(symbol)
            for symbol in sorted(
                symbols, key=lambda item: (item.path, item.line, item.qualified_name)
            )
        ],
    }
    (output_dir / "refactor_symbol_inventory.json").write_text(
        json.dumps(symbol_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    graph_payload = {
        "schema_version": 1,
        "generated_by": "tools/dev/code_inventory.py",
        "nodes": sorted(modules),
        "edges": [
            {"from": source, "to": target}
            for source in sorted(dependency_edges)
            for target in dependency_edges[source]
        ],
        "cycles": cycles,
    }
    (output_dir / "refactor_dependency_graph.json").write_text(
        json.dumps(graph_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    dead_payload = {
        "schema_version": 1,
        "generated_by": "tools/dev/code_inventory.py",
        "policy": (
            "Candidates are evidence prompts, not deletion instructions. "
            "Dynamic and packaging checks are mandatory before removal."
        ),
        "candidates": sorted(
            candidates, key=lambda item: (item["path"], item["line"], item["symbol"])
        ),
    }
    (output_dir / "refactor_dead_code_candidates.json").write_text(
        json.dumps(dead_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "files": len(rows),
        "python_modules": len(modules),
        "symbols": len(symbols),
        "dependency_edges": sum(map(len, dependency_edges.values())),
        "cycles": len(cycles),
        "dead_code_candidates": len(candidates),
        "output_dir": str(output_dir),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate TerraLab architecture inventory files."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root (default: inferred from this script).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: <root>/docs/architecture).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else root / "docs" / "architecture"
    )
    summary = generate(root, output_dir)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
