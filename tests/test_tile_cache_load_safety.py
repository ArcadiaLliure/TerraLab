import shutil
import uuid
from pathlib import Path

import numpy as np

from TerraLab.terrain.infrastructure.dem_tiles import TileCache


def _make_local_tmp_dir() -> Path:
    root = Path.cwd() / ".pytest_local_tmp"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"tile_cache_safety_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_tile_cache_npy_load_uses_safe_mode(monkeypatch):
    """Comprova que la carrega de cache NPY evita memmap i retorna float32 estable."""
    tmp_dir = _make_local_tmp_dir()
    asc_path = tmp_dir / "tile.asc"
    npy_path = tmp_dir / "tile.npy"
    asc_path.write_text(
        "ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\nNODATA_value -9999\n0 1\n2 3\n",
        encoding="utf-8",
    )
    np.save(str(npy_path), np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64))

    captured_kwargs = {}
    real_np_load = np.load

    def _spy_np_load(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return real_np_load(*args, **kwargs)

    monkeypatch.setattr(
        "TerraLab.terrain.infrastructure.dem_tiles.np.load",
        _spy_np_load,
    )

    try:
        cache = TileCache(capacity=2)
        tile_info = {
            "path": str(asc_path),
            "header": {"NCOLS": 2, "NROWS": 2, "CELLSIZE": 1.0},
        }
        data, header = cache.load(tile_info)

        assert header is not None
        assert isinstance(data, np.ndarray)
        assert data.dtype == np.float32
        assert captured_kwargs.get("mmap_mode", "missing") is None
        assert captured_kwargs.get("allow_pickle", "missing") is False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
