from __future__ import annotations

import argparse
import ast
import os
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

DEFAULT_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}


VERB_TO_ACTION_ES = {
    "get": "obtener",
    "set": "actualizar",
    "update": "actualizar",
    "load": "cargar",
    "save": "guardar",
    "build": "construir",
    "create": "crear",
    "generate": "generar",
    "render": "renderizar",
    "draw": "dibujar",
    "paint": "dibujar",
    "calculate": "calcular",
    "compute": "calcular",
    "estimate": "estimar",
    "parse": "parsear",
    "normalize": "normalizar",
    "validate": "validar",
    "check": "validar",
    "ensure": "asegurar",
    "handle": "gestionar",
    "on": "responder",
    "sync": "sincronizar",
    "toggle": "alternar",
    "init": "inicializar",
    "prepare": "preparar",
    "apply": "aplicar",
    "resolve": "resolver",
    "start": "iniciar",
    "stop": "detener",
    "clear": "limpiar",
    "delete": "eliminar",
    "remove": "eliminar",
    "find": "buscar",
    "search": "buscar",
    "pick": "seleccionar",
    "select": "seleccionar",
    "open": "abrir",
    "close": "cerrar",
}


VERB_TO_CATEGORY = {
    "get": "acceso_datos",
    "set": "gestion_estado",
    "update": "gestion_estado",
    "load": "entrada_datos",
    "save": "salida_datos",
    "build": "construccion",
    "create": "construccion",
    "generate": "construccion",
    "render": "renderizado",
    "draw": "renderizado",
    "paint": "renderizado",
    "calculate": "calculo",
    "compute": "calculo",
    "estimate": "calculo",
    "parse": "procesamiento",
    "normalize": "procesamiento",
    "validate": "validacion",
    "check": "validacion",
    "ensure": "validacion",
    "handle": "eventos",
    "on": "eventos",
    "sync": "sincronizacion",
    "toggle": "control_ui",
    "init": "inicializacion",
    "prepare": "inicializacion",
    "apply": "aplicacion",
    "resolve": "resolucion",
    "start": "orquestacion",
    "stop": "orquestacion",
    "clear": "mantenimiento",
    "delete": "mantenimiento",
    "remove": "mantenimiento",
    "find": "busqueda",
    "search": "busqueda",
    "pick": "seleccion",
    "select": "seleccion",
    "open": "control_ui",
    "close": "control_ui",
}


@dataclass
class MethodSummary:
    file_path: Path
    class_qualname: str
    method_name: str
    qualname: str
    start: int
    end: int
    lines: int
    cyclomatic: int
    cognitive: int
    complexity_level: str
    is_async: bool
    functionality: str
    responsibility: str


@dataclass
class ClassSummary:
    file_path: Path
    class_qualname: str
    class_name: str
    start: int
    end: int
    lines: int
    methods: list[MethodSummary]
    cyclomatic_total: int
    cognitive_total: int
    complexity_level: str
    functionality: str
    responsibility: str


@dataclass
class AnalysisResult:
    classes: list[ClassSummary]
    methods: list[MethodSummary]
    files_scanned: int
    parse_errors: list[str]


def _node_start_lineno(node: ast.AST) -> int:
    lineno = int(getattr(node, "lineno", 0) or 0)
    for dec in getattr(node, "decorator_list", []):
        dec_line = int(getattr(dec, "lineno", 0) or 0)
        if dec_line > 0 and (lineno <= 0 or dec_line < lineno):
            lineno = dec_line
    return max(1, lineno)


def _node_end_lineno(node: ast.AST) -> int:
    end = int(getattr(node, "end_lineno", 0) or 0)
    if end <= 0:
        end = int(getattr(node, "lineno", 1) or 1)
    return max(1, end)


def _span_lines(start: int, end: int) -> int:
    if end < start:
        end = start
    return (end - start) + 1


def _split_identifier(name: str) -> list[str]:
    if not name:
        return []
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    normalized = normalized.replace("-", "_")
    return [part.lower() for part in normalized.strip("_").split("_") if part]


def _first_sentence(text: str) -> str:
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not line:
        return ""
    for separator in (". ", "; ", ": "):
        if separator in line:
            return line.split(separator, 1)[0].strip() + "."
    return line[:180]


def _verb_category(name: str) -> str:
    tokens = _split_identifier(name)
    if not tokens:
        return "general"
    return VERB_TO_CATEGORY.get(tokens[0], "general")


def _verb_action(name: str) -> str:
    tokens = _split_identifier(name)
    if not tokens:
        return "gestionar"
    return VERB_TO_ACTION_ES.get(tokens[0], "gestionar")


def _phrase_from_tokens(tokens: list[str], fallback: str) -> str:
    if not tokens:
        return fallback
    return " ".join(tokens)


def _summarize_method_functionality(
    method_name: str,
    class_name: str,
    docstring: str,
) -> str:
    if docstring.strip():
        return _first_sentence(docstring)

    if method_name == "__init__":
        return "Inicializa estado y dependencias de la clase."
    if method_name.startswith("__") and method_name.endswith("__"):
        return "Implementa comportamiento especial del protocolo de Python."

    tokens = _split_identifier(method_name)
    action = _verb_action(method_name)
    if tokens and tokens[0] in {"on", "handle"}:
        event_tokens = tokens[1:] if len(tokens) > 1 else ["evento"]
        event_phrase = _phrase_from_tokens(event_tokens, "evento")
        return f"Gestiona el evento '{event_phrase}' dentro de {class_name}."

    object_tokens = tokens[1:] if len(tokens) > 1 else ["flujo", "principal"]
    object_phrase = _phrase_from_tokens(object_tokens, "flujo principal")
    return f"{action.capitalize()} {object_phrase}."


def _summarize_method_responsibility(
    method_name: str,
    class_name: str,
    docstring: str,
) -> str:
    if docstring.strip():
        base = _first_sentence(docstring)
        if base:
            return f"Responsable de: {base}"

    category = _verb_category(method_name).replace("_", " ")
    if method_name == "__init__":
        return f"Responsable de construir una instancia utilizable de {class_name}."
    if method_name.startswith("__") and method_name.endswith("__"):
        return f"Responsable de integracion de {class_name} con el runtime de Python."
    return f"Responsable de la logica de {category} en {class_name}."


def _summarize_class_functionality(
    class_name: str,
    methods: list[MethodSummary],
    docstring: str,
) -> str:
    if docstring.strip():
        return _first_sentence(docstring)

    name_tokens = _split_identifier(class_name)
    domain = _phrase_from_tokens(name_tokens, class_name)

    categories = [
        VERB_TO_CATEGORY.get(_split_identifier(m.method_name)[0], "general")
        for m in methods
        if _split_identifier(m.method_name)
    ]
    if categories:
        top_categories = [
            item[0].replace("_", " ")
            for item in Counter(categories).most_common(2)
        ]
        categories_text = " y ".join(top_categories)
        return f"Centraliza logica de {domain}, con foco en {categories_text}."
    return f"Centraliza logica de {domain}."


def _summarize_class_responsibility(
    class_name: str,
    methods: list[MethodSummary],
    docstring: str,
) -> str:
    if docstring.strip():
        base = _first_sentence(docstring)
        if base:
            return f"Responsable de: {base}"

    name_tokens = _split_identifier(class_name)
    domain = _phrase_from_tokens(name_tokens, class_name)
    method_count = len(methods)
    return (
        f"Responsable de mantener estado y coordinar {method_count} metodos "
        f"relacionados con {domain}."
    )


def _complexity_level(cyclomatic: int, cognitive: int) -> str:
    if cognitive >= 30 or cyclomatic >= 20:
        return "ALTA"
    if cognitive >= 15 or cyclomatic >= 10:
        return "MEDIA"
    return "BAJA"


def _is_wildcard_match_case(match_case: ast.match_case) -> bool:
    pattern = match_case.pattern
    return (
        isinstance(pattern, ast.MatchAs)
        and pattern.pattern is None
        and pattern.name is None
    )


def _iter_ast_without_nested_defs(root: ast.AST) -> Iterable[ast.AST]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if child is not root and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            stack.append(child)


def _bool_chain_complexity(expr: ast.AST) -> int:
    score = 0
    for node in ast.walk(expr):
        if isinstance(node, ast.BoolOp):
            score += max(0, len(node.values) - 1)
    return score


def _cyclomatic_complexity(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> int:
    score = 1
    for item in _iter_ast_without_nested_defs(node):
        if item is node:
            continue
        if isinstance(
            item,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ExceptHandler,
                ast.IfExp,
                ast.Assert,
            ),
        ):
            score += 1
        elif isinstance(item, ast.BoolOp):
            score += max(0, len(item.values) - 1)
        elif isinstance(item, ast.comprehension):
            score += 1 + len(item.ifs)
        elif isinstance(item, ast.Match):
            score += sum(
                0 if _is_wildcard_match_case(case) else 1
                for case in item.cases
            )
    return max(1, score)


def _expr_cognitive_complexity(expr: ast.AST, nesting: int) -> int:
    score = 0
    for node in ast.walk(expr):
        if isinstance(node, ast.IfExp):
            score += 1 + nesting + _bool_chain_complexity(node.test)
        elif isinstance(node, ast.comprehension):
            score += 1 + nesting
            score += len(node.ifs) * (1 + nesting)
            for cond in node.ifs:
                score += _bool_chain_complexity(cond)
    return score


def _has_recursive_call(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
) -> bool:
    for item in _iter_ast_without_nested_defs(node):
        if not isinstance(item, ast.Call):
            continue
        func = item.func
        if isinstance(func, ast.Name) and func.id == name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
    return False


def _cognitive_if(node: ast.If, nesting: int) -> int:
    score = 1 + nesting + _bool_chain_complexity(node.test)
    score += _cognitive_block(node.body, nesting + 1)
    if node.orelse:
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            score += _cognitive_if(node.orelse[0], nesting)
        else:
            score += _cognitive_block(node.orelse, nesting + 1)
    return score


def _cognitive_block(statements: list[ast.stmt], nesting: int) -> int:
    score = 0
    for stmt in statements:
        if isinstance(stmt, ast.If):
            score += _cognitive_if(stmt, nesting)
            continue

        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            score += 1 + nesting
            score += _expr_cognitive_complexity(stmt.iter, nesting + 1)
            score += _cognitive_block(stmt.body, nesting + 1)
            score += _cognitive_block(stmt.orelse, nesting + 1)
            continue

        if isinstance(stmt, ast.While):
            score += 1 + nesting + _bool_chain_complexity(stmt.test)
            score += _cognitive_block(stmt.body, nesting + 1)
            score += _cognitive_block(stmt.orelse, nesting + 1)
            continue

        if isinstance(stmt, ast.Try):
            score += _cognitive_block(stmt.body, nesting + 1)
            for handler in stmt.handlers:
                score += 1 + nesting
                if handler.type is not None:
                    score += _expr_cognitive_complexity(handler.type, nesting)
                score += _cognitive_block(handler.body, nesting + 1)
            score += _cognitive_block(stmt.orelse, nesting + 1)
            score += _cognitive_block(stmt.finalbody, nesting + 1)
            continue

        if isinstance(stmt, ast.Match):
            for case in stmt.cases:
                if not _is_wildcard_match_case(case):
                    score += 1 + nesting
                if case.guard is not None:
                    score += 1 + nesting + _bool_chain_complexity(case.guard)
                score += _cognitive_block(case.body, nesting + 1)
            continue

        if isinstance(stmt, (ast.Break, ast.Continue)):
            score += 1
            continue

        if isinstance(
            stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue

        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.expr):
                score += _expr_cognitive_complexity(child, nesting)
    return score


def _cognitive_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    score = _cognitive_block(node.body, nesting=0)
    if _has_recursive_call(node, node.name):
        score += 1
    return max(0, score)


def _iter_python_files(
    root: Path, include_hidden: bool, ignored_dirs: set[str]
) -> Iterable[Path]:
    for current_root, dirnames, filenames in os.walk(root):
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames, key=str.lower):
            if dirname in ignored_dirs:
                continue
            if (not include_hidden) and dirname.startswith("."):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames, key=str.lower):
            if filename.endswith(".py"):
                yield Path(current_root) / filename


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _collect_classes_from_nodes(
    nodes: list[ast.stmt],
    file_path: Path,
    prefix: str,
    out_classes: list[ClassSummary],
    out_methods: list[MethodSummary],
) -> None:
    for node in nodes:
        if not isinstance(node, ast.ClassDef):
            continue

        class_qualname = f"{prefix}.{node.name}" if prefix else node.name
        class_start = _node_start_lineno(node)
        class_end = _node_end_lineno(node)
        class_doc = ast.get_docstring(node, clean=True) or ""

        methods: list[MethodSummary] = []
        for stmt in node.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            method_start = _node_start_lineno(stmt)
            method_end = _node_end_lineno(stmt)
            method_lines = _span_lines(method_start, method_end)

            method_doc = ast.get_docstring(stmt, clean=True) or ""
            cyclomatic = _cyclomatic_complexity(stmt)
            cognitive = _cognitive_complexity(stmt)
            level = _complexity_level(cyclomatic, cognitive)

            method_summary = MethodSummary(
                file_path=file_path,
                class_qualname=class_qualname,
                method_name=stmt.name,
                qualname=f"{class_qualname}.{stmt.name}",
                start=method_start,
                end=method_end,
                lines=method_lines,
                cyclomatic=cyclomatic,
                cognitive=cognitive,
                complexity_level=level,
                is_async=isinstance(stmt, ast.AsyncFunctionDef),
                functionality=_summarize_method_functionality(
                    stmt.name,
                    node.name,
                    method_doc,
                ),
                responsibility=_summarize_method_responsibility(
                    stmt.name,
                    node.name,
                    method_doc,
                ),
            )
            methods.append(method_summary)
            out_methods.append(method_summary)

        class_cyclomatic = sum(m.cyclomatic for m in methods)
        class_cognitive = sum(m.cognitive for m in methods)
        class_level = _complexity_level(class_cyclomatic, class_cognitive)
        class_summary = ClassSummary(
            file_path=file_path,
            class_qualname=class_qualname,
            class_name=node.name,
            start=class_start,
            end=class_end,
            lines=_span_lines(class_start, class_end),
            methods=methods,
            cyclomatic_total=class_cyclomatic,
            cognitive_total=class_cognitive,
            complexity_level=class_level,
            functionality=_summarize_class_functionality(
                node.name, methods, class_doc
            ),
            responsibility=_summarize_class_responsibility(
                node.name, methods, class_doc
            ),
        )
        out_classes.append(class_summary)

        _collect_classes_from_nodes(
            node.body,
            file_path=file_path,
            prefix=class_qualname,
            out_classes=out_classes,
            out_methods=out_methods,
        )


def analyze_project(root: Path, include_hidden: bool) -> AnalysisResult:
    classes: list[ClassSummary] = []
    methods: list[MethodSummary] = []
    parse_errors: list[str] = []
    files_scanned = 0

    for py_file in _iter_python_files(
        root, include_hidden, set(DEFAULT_IGNORED_DIRS)
    ):
        files_scanned += 1
        try:
            src = py_file.read_text(encoding="utf-8-sig")
            module = ast.parse(src, filename=str(py_file))
        except UnicodeDecodeError:
            try:
                src = py_file.read_text(encoding="utf-8-sig", errors="replace")
                module = ast.parse(src, filename=str(py_file))
            except Exception as exc:
                parse_errors.append(f"{py_file}: {exc}")
                continue
        except Exception as exc:
            parse_errors.append(f"{py_file}: {exc}")
            continue

        _collect_classes_from_nodes(
            module.body,
            file_path=py_file,
            prefix="",
            out_classes=classes,
            out_methods=methods,
        )

    classes.sort(
        key=lambda c: (c.file_path.as_posix(), c.start, c.class_qualname)
    )
    methods.sort(key=lambda m: (m.file_path.as_posix(), m.start, m.qualname))
    return AnalysisResult(
        classes=classes,
        methods=methods,
        files_scanned=files_scanned,
        parse_errors=parse_errors,
    )


def _col_letter(index: int) -> str:
    letters: list[str] = []
    n = index
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _cell_xml(row_idx: int, col_idx: int, value: object) -> str:
    ref = f"{_col_letter(col_idx)}{row_idx}"
    if value is None or value == "":
        return f'<c r="{ref}"/>'

    if isinstance(value, bool):
        v = 1 if value else 0
        return f'<c r="{ref}" t="b"><v>{v}</v></c>'

    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'

    text = escape(str(value))
    if text != text.strip() or "\n" in text:
        t_node = f'<t xml:space="preserve">{text}</t>'
    else:
        t_node = f"<t>{text}</t>"
    return f'<c r="{ref}" t="inlineStr"><is>{t_node}</is></c>'


def _rows_to_sheet_xml(rows: list[list[object]]) -> str:
    max_row = max(1, len(rows))
    max_col = max(1, max((len(row) for row in rows), default=1))
    max_ref = f"{_col_letter(max_col)}{max_row}"

    row_nodes: list[str] = []
    for row_idx, row in enumerate(rows, start=1):
        cell_nodes = "".join(
            _cell_xml(row_idx=row_idx, col_idx=col_idx, value=value)
            for col_idx, value in enumerate(row, start=1)
        )
        row_nodes.append(f'<row r="{row_idx}">{cell_nodes}</row>')

    sheet_data = "".join(row_nodes)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{max_ref}"/>'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"<sheetData>{sheet_data}</sheetData>"
        "</worksheet>"
    )


def _sanitize_sheet_name(name: str, used: set[str]) -> str:
    safe = "".join(
        "_" if ch in r"[]:*?/\\" else ch for ch in (name or "Sheet")
    )
    safe = safe.strip() or "Sheet"
    safe = safe[:31]
    base = safe
    idx = 2
    while safe in used:
        suffix = f"_{idx}"
        safe = f"{base[: max(1, 31 - len(suffix))]}{suffix}"
        idx += 1
    used.add(safe)
    return safe


def write_excel_workbook(
    output_path: Path, sheets: list[tuple[str, list[list[object]]]]
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    used_sheet_names: set[str] = set()
    safe_sheets: list[tuple[str, list[list[object]]]] = [
        (_sanitize_sheet_name(name, used_sheet_names), rows)
        for name, rows in sheets
    ]

    content_type_overrides = [
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    ]
    for idx in range(1, len(safe_sheets) + 1):
        content_type_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{''.join(content_type_overrides)}"
        "</Types>"
    )

    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    workbook_sheet_nodes: list[str] = []
    workbook_rel_nodes: list[str] = []
    for idx, (sheet_name, _rows) in enumerate(safe_sheets, start=1):
        safe_name_xml = escape(sheet_name)
        workbook_sheet_nodes.append(
            f'<sheet name="{safe_name_xml}" sheetId="{idx}" r:id="rId{idx}"/>'
        )
        workbook_rel_nodes.append(
            f'<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{''.join(workbook_sheet_nodes)}</sheets>"
        "</workbook>"
    )

    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{''.join(workbook_rel_nodes)}"
        "</Relationships>"
    )

    with ZipFile(output_path, mode="w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        for idx, (_sheet_name, rows) in enumerate(safe_sheets, start=1):
            zf.writestr(
                f"xl/worksheets/sheet{idx}.xml", _rows_to_sheet_xml(rows)
            )


def build_class_rows(result: AnalysisResult, root: Path) -> list[list[object]]:
    rows: list[list[object]] = [
        [
            "File",
            "Class",
            "Start",
            "End",
            "Lines",
            "Methods",
            "CyclomaticTotal",
            "CognitiveTotal_EstSonarLike",
            "ComplexityLevel",
            "Functionality",
            "Responsibility",
            "ComplexityNote",
        ]
    ]

    for cls in result.classes:
        rows.append(
            [
                _relative_path(cls.file_path, root),
                cls.class_qualname,
                cls.start,
                cls.end,
                cls.lines,
                len(cls.methods),
                cls.cyclomatic_total,
                cls.cognitive_total,
                cls.complexity_level,
                cls.functionality,
                cls.responsibility,
                "Cognitive complexity is an estimation inspired by Sonar rules.",
            ]
        )
    return rows


def build_method_rows(
    result: AnalysisResult, root: Path
) -> list[list[object]]:
    rows: list[list[object]] = [
        [
            "File",
            "Class",
            "Method",
            "QualifiedName",
            "Async",
            "Start",
            "End",
            "Lines",
            "Cyclomatic",
            "Cognitive_EstSonarLike",
            "ComplexityLevel",
            "Functionality",
            "Responsibility",
            "ComplexityNote",
        ]
    ]

    for method in result.methods:
        rows.append(
            [
                _relative_path(method.file_path, root),
                method.class_qualname,
                method.method_name,
                method.qualname,
                method.is_async,
                method.start,
                method.end,
                method.lines,
                method.cyclomatic,
                method.cognitive,
                method.complexity_level,
                method.functionality,
                method.responsibility,
                "Cognitive complexity is an estimation inspired by Sonar rules.",
            ]
        )
    return rows


def build_overview_rows(
    result: AnalysisResult, root: Path
) -> list[list[object]]:
    method_cognitive = [m.cognitive for m in result.methods]
    method_cyclo = [m.cyclomatic for m in result.methods]
    high_methods = [m for m in result.methods if m.complexity_level == "ALTA"]

    avg_cognitive = (
        statistics.mean(method_cognitive) if method_cognitive else 0.0
    )
    avg_cyclo = statistics.mean(method_cyclo) if method_cyclo else 0.0

    rows: list[list[object]] = [
        ["Metric", "Value"],
        ["Root", str(root)],
        ["FilesScanned", result.files_scanned],
        ["Classes", len(result.classes)],
        ["Methods", len(result.methods)],
        ["MethodsComplexityHigh", len(high_methods)],
        ["CyclomaticAvg_Method", round(avg_cyclo, 2)],
        ["CognitiveAvg_Method", round(avg_cognitive, 2)],
        [],
        [
            "TopMethodsByCognitive",
            "File",
            "Class",
            "Method",
            "Cognitive",
            "Cyclomatic",
            "Lines",
            "ComplexityLevel",
        ],
    ]

    top_methods = sorted(
        result.methods,
        key=lambda m: (m.cognitive, m.cyclomatic, m.lines),
        reverse=True,
    )[:30]
    for method in top_methods:
        rows.append(
            [
                method.qualname,
                _relative_path(method.file_path, root),
                method.class_qualname,
                method.method_name,
                method.cognitive,
                method.cyclomatic,
                method.lines,
                method.complexity_level,
            ]
        )

    if result.parse_errors:
        rows.append([])
        rows.append(["ParseErrors", "Detail"])
        for err in result.parse_errors:
            rows.append(["ERROR", err])

    return rows


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Genera un resumen cognitivo de clases/metodos en Excel con "
            "lineas, complejidad estimada y descripcion de funcionalidad."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Ruta raiz a analizar (por defecto: carpeta actual).",
    )
    parser.add_argument(
        "--output",
        default="tools/resumen_cognitivo_clases_metodos.xlsx",
        help="Ruta del archivo de salida .xlsx.",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Incluye carpetas ocultas.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"La ruta no existe: {root}", file=sys.stderr)
        return 1
    if not root.is_dir():
        print(f"La ruta no es un directorio: {root}", file=sys.stderr)
        return 1

    result = analyze_project(
        root=root, include_hidden=bool(args.include_hidden)
    )

    output_path = Path(args.output)
    if output_path.suffix.lower() != ".xlsx":
        output_path = output_path.with_suffix(".xlsx")
    output_path = output_path.resolve()

    write_excel_workbook(
        output_path=output_path,
        sheets=[
            ("Resumen", build_overview_rows(result, root)),
            ("Clases", build_class_rows(result, root)),
            ("Metodos", build_method_rows(result, root)),
        ],
    )

    print(f"Excel generado: {output_path}")
    print(
        "Resumen: "
        f"files={result.files_scanned}, classes={len(result.classes)}, methods={len(result.methods)}, "
        f"parse_errors={len(result.parse_errors)}"
    )
    if result.parse_errors:
        print("Hay errores de parseo. Revisa la hoja 'Resumen' del Excel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
