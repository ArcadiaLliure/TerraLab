"""Import-time safety contract for canonical runtime modules."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_imports_do_not_create_files_or_application(tmp_path: Path) -> None:
    sandbox = tmp_path / "profile"
    sandbox.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    probe = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from PyQt5.QtWidgets import QApplication",
            "import TerraLab",
            "import TerraLab.common.cache",
            "import TerraLab.common.cancellation",
            "import TerraLab.common.deprecation_registry",
            "import TerraLab.scene.render_state",
            "import TerraLab.terrain.domain.profile",
            "import TerraLab.ui.astro_canvas",
            "assert QApplication.instance() is None",
            "assert not any(Path('.').iterdir())",
            "assert not any(Path(sys.argv[1]).rglob('*'))",
        )
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(ROOT),
            "HOME": str(sandbox),
            "USERPROFILE": str(sandbox),
            "APPDATA": str(sandbox),
            "LOCALAPPDATA": str(sandbox),
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(sandbox)],
        cwd=runtime,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
