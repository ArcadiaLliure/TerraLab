from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _assert_exact_case(path: Path) -> None:
    relative = path.resolve().relative_to(ROOT.resolve())
    current = ROOT
    for part in relative.parts:
        exact_names = {child.name for child in current.iterdir()}
        assert part in exact_names, (
            f"Path capitalization is not exact: {relative}"
        )
        current /= part


def test_public_readme_links_exist_with_exact_capitalization() -> None:
    readmes = (
        ROOT / "README.md",
        ROOT / "TerraLab" / "README.md",
        ROOT / "TerraLab" / "terrain" / "README.md",
    )
    for readme in readmes:
        _assert_exact_case(readme)
        for raw_target in MARKDOWN_LINK.findall(
            readme.read_text(encoding="utf-8")
        ):
            target = raw_target.split("#", 1)[0].strip()
            if (
                not target
                or "://" in target
                or target.startswith("mailto:")
            ):
                continue
            resolved = (readme.parent / target).resolve()
            assert resolved.exists(), (
                f"Broken link in {readme.relative_to(ROOT)}: {raw_target}"
            )
            _assert_exact_case(resolved)


def test_pyproject_declares_a_self_contained_wheel_contract() -> None:
    payload = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = payload["project"]
    setuptools = payload["tool"]["setuptools"]

    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert setuptools["include-package-data"] is False
    assert "data/*.json" not in setuptools["package-data"]["TerraLab"]
    assert not any(
        (ROOT / name).exists()
        for name in ("setup.py", "requirements.txt", "environment.yml")
    )

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Permission is hereby granted" in license_text


def test_all_declared_console_entry_points_are_importable() -> None:
    payload = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    for target in payload["project"]["scripts"].values():
        module_name, attribute = target.split(":", 1)
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute))
