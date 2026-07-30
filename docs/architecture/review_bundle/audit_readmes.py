"""Audit package README coverage, case-sensitive paths, links and similarity."""

from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote


ASPECT_PATTERNS = {
    "responsibility": re.compile(r"\S"),
    "public_api": re.compile(r"`[^`]+`|\.py\b|API\b", re.IGNORECASE),
    "allowed_dependencies": re.compile(
        r"dependenc|importa|usa\b|recibe\b|delega\b", re.IGNORECASE
    ),
    "forbidden_dependencies": re.compile(
        r"\bno\b|\bnunca\b|prohib", re.IGNORECASE
    ),
    "state_ownership": re.compile(
        r"estado|state|propiet|inmutable|cach[eé]", re.IGNORECASE
    ),
    "concurrency": re.compile(
        r"thread|worker|proceso|concurr|signal|señal|as[ií]ncr",
        re.IGNORECASE,
    ),
    "persistence": re.compile(
        r"persist|archivo|fichero|dataset|datos|cach[eé]", re.IGNORECASE
    ),
    "relevant_tests": re.compile(r"tests?|pruebas?", re.IGNORECASE),
}
LINK_PATTERN = re.compile(r"\]\(([^)]+)\)")


def exact_case_exists(path: Path) -> bool:
    path = path.resolve()
    anchor = Path(path.anchor)
    current = anchor
    for part in path.parts[1:]:
        try:
            names = {entry.name for entry in current.iterdir()}
        except OSError:
            return False
        if part not in names:
            return False
        current /= part
    return current.exists()


def normalized_body(text: str) -> str:
    words = re.findall(r"[a-záéíóúüñ0-9]+", text.casefold())
    return " ".join(words)


def audit_links(root: Path) -> list[dict[str, str]]:
    broken: list[dict[str, str]] = []
    for markdown in root.rglob("*.md"):
        if any(part in {".git", ".venv", "build", "dist"} for part in markdown.parts):
            continue
        text = markdown.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if (
                not target
                or target.startswith(("#", "http://", "https://", "mailto:"))
            ):
                continue
            relative = unquote(target.split("#", 1)[0])
            resolved = markdown.parent / relative
            if not resolved.exists() or not exact_case_exists(resolved):
                broken.append(
                    {
                        "source": markdown.relative_to(root).as_posix(),
                        "target": target,
                        "resolved": str(resolved.resolve()),
                    }
                )
    return broken


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    package_root = root / "TerraLab"
    package_dirs = sorted(
        init.parent
        for init in package_root.rglob("__init__.py")
        if "__pycache__" not in init.parts
    )

    readmes: list[dict[str, object]] = []
    normalized: dict[str, str] = {}
    for package_dir in package_dirs:
        actual_names = {entry.name for entry in package_dir.iterdir()}
        readme = package_dir / "README.md"
        text = readme.read_text(encoding="utf-8") if "README.md" in actual_names else ""
        prose = "\n".join(
            line for line in text.splitlines() if line.strip() and not line.startswith("#")
        )
        relative = readme.relative_to(root).as_posix()
        normalized[relative] = normalized_body(text)
        aspects = {
            name: bool(pattern.search(prose))
            for name, pattern in ASPECT_PATTERNS.items()
        }
        readmes.append(
            {
                "package": package_dir.relative_to(root).as_posix(),
                "readme": relative,
                "exact_case": "README.md" in actual_names,
                "line_count": len(text.splitlines()),
                "specific_content": len(normalized[relative].split()) >= 8,
                "aspects": aspects,
                "missing_aspects": [
                    name for name, present in aspects.items() if not present
                ],
            }
        )

    similarities: list[dict[str, object]] = []
    keys = sorted(normalized)
    for index, left in enumerate(keys):
        for right in keys[index + 1 :]:
            ratio = SequenceMatcher(None, normalized[left], normalized[right]).ratio()
            if ratio >= 0.75:
                similarities.append(
                    {"left": left, "right": right, "similarity": round(ratio, 4)}
                )

    result = {
        "package_count": len(package_dirs),
        "exact_case_readme_count": sum(
            bool(item["exact_case"]) for item in readmes
        ),
        "specific_readme_count": sum(
            bool(item["specific_content"]) for item in readmes
        ),
        "readmes": readmes,
        "high_similarity_pairs": similarities,
        "broken_internal_links": audit_links(root),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
