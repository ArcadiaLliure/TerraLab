from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

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
class TreeNode:
    path: Path
    subdirs: list["TreeNode"]
    py_files: list[Path]


@dataclass(frozen=True)
class TreeGlyphs:
    tee: str
    corner: str
    vertical: str
    space: str


UNICODE_GLYPHS = TreeGlyphs(
    tee="├── ", corner="└── ", vertical="│   ", space="    "
)
ASCII_GLYPHS = TreeGlyphs(
    tee="|-- ", corner="\\-- ", vertical="|   ", space="    "
)


def _supports_unicode_stdout() -> bool:
    encoding = sys.stdout.encoding or "utf-8"
    try:
        "├".encode(encoding)
        return True
    except Exception:
        return False


def count_lines(path: Path) -> int:
    newline_count = 0
    total_bytes = 0
    last_byte = b""

    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            newline_count += chunk.count(b"\n")
            last_byte = chunk[-1:]

    if total_bytes == 0:
        return 0
    if last_byte == b"\n":
        return newline_count
    return newline_count + 1


def build_tree(
    root: Path, ignored_dirs: set[str], include_hidden: bool
) -> TreeNode | None:
    subdirs: list[TreeNode] = []
    py_files: list[Path] = []

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return None

    for entry in entries:
        if entry.is_dir():
            if entry.name in ignored_dirs:
                continue
            if (not include_hidden) and entry.name.startswith("."):
                continue
            child = build_tree(entry, ignored_dirs, include_hidden)
            if child is not None:
                subdirs.append(child)
        elif entry.is_file() and entry.suffix == ".py":
            py_files.append(entry)

    if not subdirs and not py_files:
        return None
    return TreeNode(path=root, subdirs=subdirs, py_files=py_files)


def render_tree_lines(node: TreeNode, glyphs: TreeGlyphs) -> list[str]:
    lines = [f"{node.path.name or str(node.path)}/"]

    def _walk(current: TreeNode, prefix: str) -> None:
        entries: list[tuple[str, TreeNode | Path]] = []
        entries.extend(("dir", child) for child in current.subdirs)
        entries.extend(("file", file_path) for file_path in current.py_files)

        for index, (entry_type, entry) in enumerate(entries):
            is_last = index == len(entries) - 1
            branch = glyphs.corner if is_last else glyphs.tee
            next_prefix = prefix + (
                glyphs.space if is_last else glyphs.vertical
            )

            if entry_type == "dir":
                child = entry
                assert isinstance(child, TreeNode)
                lines.append(f"{prefix}{branch}{child.path.name}/")
                _walk(child, next_prefix)
            else:
                file_path = entry
                assert isinstance(file_path, Path)
                line_count = count_lines(file_path)
                lines.append(
                    f"{prefix}{branch}{file_path.name} ({line_count})"
                )

    _walk(node, "")
    return lines


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Muestra un arbol de carpetas con archivos .py y su numero de lineas "
            "entre parentesis."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Ruta raiz desde donde construir el arbol (por defecto: carpeta actual).",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Incluye carpetas ocultas (excepto caches comunes ignoradas).",
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

    ignored = set(DEFAULT_IGNORED_DIRS)

    tree = build_tree(root, ignored, args.include_hidden)
    glyphs = UNICODE_GLYPHS if _supports_unicode_stdout() else ASCII_GLYPHS
    if tree is None:
        print(f"{root.name}/")
        print(f"{glyphs.corner}(sin archivos .py)")
        return 0

    for line in render_tree_lines(tree, glyphs):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
