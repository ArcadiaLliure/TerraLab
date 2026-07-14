"""CLI curt per descarregar Gaia en format de teseles.

Mantingut separat del flux legacy `download_gaia_tap.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Permet execució standalone: `python tools/download_gaia_tiles.py ...`
if __package__ in (None, ""):
    _repo_root = Path(__file__).resolve().parents[2]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

from TerraLab.data.gaia_downloader import main


if __name__ == "__main__":
    raise SystemExit(main())
