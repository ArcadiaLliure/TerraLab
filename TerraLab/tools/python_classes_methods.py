from __future__ import annotations

import argparse
import ast
import os
import sys
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


@dataclass
class MethodInfo:
    name: str
    start: int
    end: int
    lines: int
    is_async: bool


@dataclass
class ClassInfo:
    file_path: Path
    qualname: str
    start: int
    end: int
    lines: int
    methods: list[MethodInfo]


def _node_start_lineno(node: ast.AST) -> int:
    lineno = int(getattr(node, "lineno", 0) or 0)
    decorator_list = getattr(node, "decorator_list", [])
    for dec in decorator_list:
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


def _collect_classes(
    nodes: list[ast.stmt],
    file_path: Path,
    prefix: str,
    out: list[ClassInfo],
) -> None:
    for node in nodes:
        if not isinstance(node, ast.ClassDef):
            continue

        qualname = f"{prefix}.{node.name}" if prefix else node.name
        class_start = _node_start_lineno(node)
        class_end = _node_end_lineno(node)

        methods: list[MethodInfo] = []
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef):
                m_start = _node_start_lineno(stmt)
                m_end = _node_end_lineno(stmt)
                methods.append(
                    MethodInfo(
                        name=stmt.name,
                        start=m_start,
                        end=m_end,
                        lines=_span_lines(m_start, m_end),
                        is_async=False,
                    )
                )
            elif isinstance(stmt, ast.AsyncFunctionDef):
                m_start = _node_start_lineno(stmt)
                m_end = _node_end_lineno(stmt)
                methods.append(
                    MethodInfo(
                        name=stmt.name,
                        start=m_start,
                        end=m_end,
                        lines=_span_lines(m_start, m_end),
                        is_async=True,
                    )
                )

        out.append(
            ClassInfo(
                file_path=file_path,
                qualname=qualname,
                start=class_start,
                end=class_end,
                lines=_span_lines(class_start, class_end),
                methods=methods,
            )
        )

        _collect_classes(node.body, file_path, qualname, out)


def _iter_python_files(
    root: Path,
    ignored_dirs: set[str],
    include_hidden: bool,
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


def analyze_project(
    root: Path,
    ignored_dirs: set[str],
    include_hidden: bool,
) -> tuple[list[ClassInfo], list[str]]:
    classes: list[ClassInfo] = []
    errors: list[str] = []

    for py_file in _iter_python_files(root, ignored_dirs, include_hidden):
        try:
            src = py_file.read_text(encoding="utf-8-sig")
            module = ast.parse(src, filename=str(py_file))
        except UnicodeDecodeError:
            try:
                src = py_file.read_text(encoding="utf-8-sig", errors="replace")
                module = ast.parse(src, filename=str(py_file))
            except Exception as exc:
                errors.append(f"{py_file}: {exc}")
                continue
        except Exception as exc:
            errors.append(f"{py_file}: {exc}")
            continue

        _collect_classes(module.body, py_file, prefix="", out=classes)

    classes.sort(
        key=lambda item: (item.file_path.as_posix(), item.start, item.qualname)
    )
    return classes, errors


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
        num = 1 if value else 0
        return f'<c r="{ref}" t="b"><v>{num}</v></c>'

    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'

    text = escape(str(value))
    if (text != text.strip()) or ("\n" in text):
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


def write_excel_xlsx(
    output_path: Path,
    rows: list[list[object]],
    sheet_name: str = "ClassesMethods",
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    safe_sheet_name = (sheet_name or "Sheet1")[:31]
    safe_sheet_name = (
        safe_sheet_name.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
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

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        f'<sheet name="{safe_sheet_name}" sheetId="1" r:id="rId1"/>'
        "</sheets>"
        "</workbook>"
    )

    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )

    sheet_xml = _rows_to_sheet_xml(rows)

    with ZipFile(output_path, mode="w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def print_report(classes: list[ClassInfo], root: Path) -> None:
    if not classes:
        print("No se encontraron clases.")
        return

    grouped: dict[str, list[ClassInfo]] = {}
    for cls in classes:
        key = _relative_path(cls.file_path, root)
        grouped.setdefault(key, []).append(cls)

    for file_path in sorted(grouped.keys()):
        print(f"{file_path}:")
        for cls in grouped[file_path]:
            print(f"  {cls.qualname} ({cls.lines} lineas)")
            if not cls.methods:
                print("    -> (sin metodos)")
                continue
            for method in cls.methods:
                method_name = (
                    f"async {method.name}" if method.is_async else method.name
                )
                print(f"    -> {method_name} ({method.lines} lineas)")


def build_excel_rows(
    classes: list[ClassInfo], root: Path
) -> list[list[object]]:
    rows: list[list[object]] = [
        [
            "File",
            "Class",
            "ClassStart",
            "ClassEnd",
            "ClassLines",
            "Method",
            "MethodStart",
            "MethodEnd",
            "MethodLines",
            "MethodAsync",
        ]
    ]

    for cls in classes:
        rel_file = _relative_path(cls.file_path, root)
        if not cls.methods:
            rows.append(
                [
                    rel_file,
                    cls.qualname,
                    cls.start,
                    cls.end,
                    cls.lines,
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
            continue

        for method in cls.methods:
            rows.append(
                [
                    rel_file,
                    cls.qualname,
                    cls.start,
                    cls.end,
                    cls.lines,
                    method.name,
                    method.start,
                    method.end,
                    method.lines,
                    method.is_async,
                ]
            )
    return rows


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Lista todas las clases y sus metodos en archivos .py, "
            "incluyendo lineas que ocupan."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Ruta raiz del analisis (por defecto: carpeta actual).",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Incluye carpetas ocultas.",
    )
    parser.add_argument(
        "--excel",
        nargs="?",
        const="classes_methods_report.xlsx",
        default="",
        metavar="OUTPUT.xlsx",
        help=(
            "Exporta tambien a Excel (.xlsx). "
            "Si no se indica ruta: classes_methods_report.xlsx"
        ),
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

    classes, errors = analyze_project(
        root=root,
        ignored_dirs=set(DEFAULT_IGNORED_DIRS),
        include_hidden=bool(args.include_hidden),
    )
    print_report(classes, root)

    if args.excel:
        excel_path = Path(args.excel)
        if excel_path.suffix.lower() != ".xlsx":
            excel_path = excel_path.with_suffix(".xlsx")
        excel_path = excel_path.resolve()
        rows = build_excel_rows(classes, root)
        write_excel_xlsx(excel_path, rows, sheet_name="ClassesMethods")
        print(f"\nExcel generado: {excel_path}")

    if errors:
        print("\nArchivos con errores de parseo:", file=sys.stderr)
        for err in errors:
            print(f"- {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
