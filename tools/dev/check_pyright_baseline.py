"""Fail when Pyright debt grows beyond the documented local baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = (
    ROOT / "docs" / "architecture" / "refactor_pyright_baseline.json"
)


def _run_pyright(scope: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-m", "pyright", scope, "--outputjson"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Pyright did not return JSON: {details}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the documented Pyright debt did not increase."
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args(argv)

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    scope = str(baseline["scope"])
    report = _run_pyright(scope)
    summary = report["summary"]
    errors = int(summary["errorCount"])
    warnings = int(summary["warningCount"])
    allowed_errors = int(baseline["error_count"])
    allowed_warnings = int(baseline["warning_count"])

    print(
        f"Pyright: {errors} errors, {warnings} warnings "
        f"(baseline: {allowed_errors}/{allowed_warnings})"
    )
    if errors > allowed_errors or warnings > allowed_warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
