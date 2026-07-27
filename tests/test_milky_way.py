from __future__ import annotations

import math
import shutil
import uuid
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from PyQt5.QtGui import QImage, QPainter

from TerraLab.render.sky.milkyway_overlay import MilkyWayOverlay
from TerraLab.render.sky_renderer import SkyRenderer
from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    LP_MODE_MAGNITUDE,
)
from TerraLab.scene.camera import Camera
from TerraLab.render.qt.context import RenderContext
from TerraLab.scene.scene_state import SceneState
from TerraLab.data.converters.planck import convert_planck_fits_to_cache


def _image_to_rgba(image: QImage) -> np.ndarray:
    img = image.convertToFormat(QImage.Format_RGBA8888)
    ptr = img.bits()
    ptr.setsize(img.byteCount())
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape(
        img.height(), img.bytesPerLine() // 4, 4
    )
    return arr[:, : img.width(), :].copy()


def _build_state(**overlay_cfg) -> SceneState:
    state = SceneState(
        camera=Camera(),
        latitude=41.4,
        longitude=2.1,
        ut_hour=22.0,
        day_of_year=120,
        bortle=2.0,
        light_pollution_mode=LP_MODE_AUTOMATIC,
        magnitude_limit=6.5,
    )
    base_cfg = {
        "enabled": True,
        "texture_path": "data/sky/milkyway_overlay.png",
        "opacity": 1.0,
        "blend_mode": "normal",
        "ra_offset_deg": 0.0,
        "coord_frame": "galactic",
        "lat_flip": True,
        "lon_flip": True,
        "sample_scale": 1.0,
        "auto_opacity": True,
        "light_pollution_mode": LP_MODE_AUTOMATIC,
        "bortle": 2.0,
        "magnitude_limit": 6.5,
        "scope_enabled": False,
        "scope_iso": 800.0,
        "scope_exposure_s": 15.0,
        "scope_aperture_f_number": 2.8,
        "dust_map_enabled": False,
        "dust_map_path": "data/sky/derived/missing_map.npz",
        "dust_density_strength": 0.0,
        "dust_extinction_strength": 0.0,
    }
    base_cfg.update(overlay_cfg)
    state.extras = {"milkyway_overlay": base_cfg}
    return state


def _render_overlay(
    overlay: MilkyWayOverlay, state: SceneState, w: int = 128, h: int = 64
) -> QImage:
    image = QImage(w, h, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        ctx = RenderContext(
            painter=painter, width=w, height=h, diagnostics=None
        )
        overlay.render(ctx, state)
    finally:
        painter.end()
    return image


def test_milkyway_png_loads_and_has_alpha() -> None:
    overlay = MilkyWayOverlay()
    px = overlay.sample_rgba_at_radec(ra_deg=0.0, dec_deg=0.0)
    assert px is not None
    assert overlay._texture_rgba is not None
    assert overlay._texture_rgba.shape[:2] == (1800, 3600)


def test_ra_wrap_is_seamless_for_0_and_360() -> None:
    overlay = MilkyWayOverlay()
    a = overlay.sample_rgba_at_radec(
        ra_deg=0.0, dec_deg=5.0, ra_offset_deg=0.0
    )
    b = overlay.sample_rgba_at_radec(
        ra_deg=360.0, dec_deg=5.0, ra_offset_deg=0.0
    )
    assert a is not None and b is not None
    assert a == b


def test_dec_clamp_handles_poles_without_error() -> None:
    overlay = MilkyWayOverlay()
    north_pole = overlay.sample_rgba_at_radec(ra_deg=42.0, dec_deg=90.0)
    north_over = overlay.sample_rgba_at_radec(ra_deg=42.0, dec_deg=120.0)
    south_pole = overlay.sample_rgba_at_radec(ra_deg=42.0, dec_deg=-90.0)
    south_over = overlay.sample_rgba_at_radec(ra_deg=42.0, dec_deg=-120.0)
    assert north_pole == north_over
    assert south_pole == south_over


def test_enabled_false_disables_layer() -> None:
    overlay = MilkyWayOverlay()
    state = _build_state(enabled=False)
    img = _render_overlay(overlay, state)
    rgba = _image_to_rgba(img)
    assert int(np.sum(rgba)) == 0


def test_opacity_changes_visual_result() -> None:
    overlay = MilkyWayOverlay()
    state_hi = _build_state(opacity=1.0, blend_mode="normal")
    state_lo = _build_state(opacity=0.2, blend_mode="normal")
    img_hi = _render_overlay(overlay, state_hi)
    img_lo = _render_overlay(overlay, state_lo)
    sum_hi = int(np.sum(_image_to_rgba(img_hi)))
    sum_lo = int(np.sum(_image_to_rgba(img_lo)))
    assert sum_hi > sum_lo


def test_manual_mag_mode_is_direct_not_inverted() -> None:
    overlay = MilkyWayOverlay()
    state_low = _build_state(
        light_pollution_mode=LP_MODE_MAGNITUDE,
        magnitude_limit=-27.0,
        bortle=1.0,
    )
    state_high = _build_state(
        light_pollution_mode=LP_MODE_MAGNITUDE,
        magnitude_limit=10.0,
        bortle=1.0,
    )
    state_low.sun_alt = -25.0
    state_high.sun_alt = -25.0
    img_low = _render_overlay(overlay, state_low)
    img_high = _render_overlay(overlay, state_high)
    alpha_low = int(np.sum(_image_to_rgba(img_low)[..., 3]))
    alpha_high = int(np.sum(_image_to_rgba(img_high)[..., 3]))
    assert alpha_high > alpha_low


def test_ra_offset_rotates_sampling() -> None:
    overlay = MilkyWayOverlay()
    samples_no = [
        overlay.sample_rgba_at_radec(
            ra_deg=15.0, dec_deg=0.0, ra_offset_deg=0.0
        ),
        overlay.sample_rgba_at_radec(
            ra_deg=75.0, dec_deg=0.0, ra_offset_deg=0.0
        ),
        overlay.sample_rgba_at_radec(
            ra_deg=150.0, dec_deg=0.0, ra_offset_deg=0.0
        ),
    ]
    samples_rot = [
        overlay.sample_rgba_at_radec(
            ra_deg=15.0, dec_deg=0.0, ra_offset_deg=180.0
        ),
        overlay.sample_rgba_at_radec(
            ra_deg=75.0, dec_deg=0.0, ra_offset_deg=180.0
        ),
        overlay.sample_rgba_at_radec(
            ra_deg=150.0, dec_deg=0.0, ra_offset_deg=180.0
        ),
    ]
    assert any(a != b for a, b in zip(samples_no, samples_rot))


def test_galactic_texture_requires_180_offset_for_core_alignment() -> None:
    overlay = MilkyWayOverlay()
    gc_no = overlay.sample_rgba_at_radec(
        ra_deg=266.4051,
        dec_deg=-28.936175,
        ra_offset_deg=0.0,
        coord_frame="galactic",
    )
    gc_yes = overlay.sample_rgba_at_radec(
        ra_deg=266.4051,
        dec_deg=-28.936175,
        ra_offset_deg=180.0,
        coord_frame="galactic",
    )
    assert gc_no is not None and gc_yes is not None
    assert int(gc_yes[0]) > int(gc_no[0])


def test_lat_flip_changes_sampling_for_current_galactic_asset() -> None:
    overlay = MilkyWayOverlay()
    a = overlay.sample_rgba_at_radec(
        ra_deg=80.90140845070422,
        dec_deg=-70.0,
        ra_offset_deg=180.0,
        coord_frame="galactic",
        lat_flip=False,
        lon_flip=True,
    )
    b = overlay.sample_rgba_at_radec(
        ra_deg=80.90140845070422,
        dec_deg=-70.0,
        ra_offset_deg=180.0,
        coord_frame="galactic",
        lat_flip=True,
        lon_flip=True,
    )
    assert a is not None and b is not None
    rgb_delta = (
        abs(int(a[0]) - int(b[0]))
        + abs(int(a[1]) - int(b[1]))
        + abs(int(a[2]) - int(b[2]))
    )
    assert rgb_delta > 100


def test_lon_flip_changes_sampling_for_current_galactic_asset() -> None:
    overlay = MilkyWayOverlay()
    a = overlay.sample_rgba_at_radec(
        ra_deg=80.8939,
        dec_deg=-69.7561,
        ra_offset_deg=180.0,
        coord_frame="galactic",
        lat_flip=True,
        lon_flip=False,
    )
    b = overlay.sample_rgba_at_radec(
        ra_deg=80.8939,
        dec_deg=-69.7561,
        ra_offset_deg=180.0,
        coord_frame="galactic",
        lat_flip=True,
        lon_flip=True,
    )
    assert a is not None and b is not None
    rgb_delta = (
        abs(int(a[0]) - int(b[0]))
        + abs(int(a[1]) - int(b[1]))
        + abs(int(a[2]) - int(b[2]))
    )
    assert rgb_delta > 150


def test_optional_missing_dust_map_does_not_break_render() -> None:
    overlay = MilkyWayOverlay()
    state = _build_state(
        dust_map_enabled=True,
        dust_map_path="data/sky/derived/this_file_does_not_exist.npz",
        dust_density_strength=0.5,
        dust_extinction_strength=0.5,
    )
    img = _render_overlay(overlay, state)
    rgba = _image_to_rgba(img)
    assert int(np.sum(rgba[..., 3])) > 0


def test_milkyway_hidden_in_daylight() -> None:
    overlay = MilkyWayOverlay()
    state = _build_state()
    state.sun_alt = 5.0
    img = _render_overlay(overlay, state)
    rgba = _image_to_rgba(img)
    assert int(np.sum(rgba[..., 3])) == 0


def test_milkyway_hidden_from_bortle_5_in_auto_mode() -> None:
    overlay = MilkyWayOverlay()
    state = _build_state(light_pollution_mode=LP_MODE_AUTOMATIC, bortle=5.0)
    state.sun_alt = -25.0
    img = _render_overlay(overlay, state)
    rgba = _image_to_rgba(img)
    assert int(np.sum(rgba[..., 3])) == 0


def test_equatorial_to_galactic_conversion_for_galactic_center() -> None:
    ra = np.asarray([266.4051], dtype=np.float32)
    dec = np.asarray([-28.936175], dtype=np.float32)
    l_deg, b_deg = MilkyWayOverlay._equatorial_to_galactic_deg(ra, dec)
    galactic_longitude = float(l_deg[0] % 360.0)
    b = float(b_deg[0])
    l_err = min(
        abs(galactic_longitude),
        abs(galactic_longitude - 360.0),
    )
    assert l_err < 0.2
    assert math.isclose(b, 0.0, abs_tol=0.2)


def test_stars_disabled_does_not_raise_and_returns_empty_result() -> None:
    image = QImage(64, 64, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        ctx = RenderContext(
            painter=painter, width=64, height=64, diagnostics=None
        )
        state = SceneState(
            camera=Camera(),
            latitude=41.4,
            longitude=2.1,
            ut_hour=22.0,
            day_of_year=120,
            ra=np.array([0.0, 10.0], dtype=np.float32),
            dec=np.array([0.0, 5.0], dtype=np.float32),
            mag=np.array([1.0, 2.0], dtype=np.float32),
            extras={
                "stars_enabled": False,
                "milkyway_overlay": {
                    "enabled": False,
                },
            },
        )
        result = SkyRenderer().render(ctx, state)
    finally:
        painter.end()

    assert result.total_in_view == 0
    assert result.after_mag_cut == 0
    assert result.after_bucket == 0


def _write_minimal_planck_fits(path: Path) -> None:
    vals = np.linspace(0.0, 11.0, 12, dtype=np.float32)
    col = fits.Column(name="TAU353", format="E", array=vals)
    table_hdu = fits.BinTableHDU.from_columns([col])
    table_hdu.header["PIXTYPE"] = "HEALPIX"
    table_hdu.header["ORDERING"] = "RING"
    table_hdu.header["NSIDE"] = 1
    table_hdu.header["COORDSYS"] = "G"
    hdul = fits.HDUList([fits.PrimaryHDU(), table_hdu])
    hdul.writeto(path, overwrite=True)


def _workspace_temp_dir() -> Path:
    root = Path(__file__).resolve().parent / ".pytest_tmp"
    root.mkdir(parents=True, exist_ok=True)
    folder = root / f"milkyway_{uuid.uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_planck_converter_generates_npz_with_multiprocessing() -> None:
    tmp_dir = _workspace_temp_dir()
    try:
        fits_path = tmp_dir / "planck_minimal.fits"
        out_npz = tmp_dir / "planck_dust_u16.npz"
        _write_minimal_planck_fits(fits_path)

        summary = convert_planck_fits_to_cache(
            fits_path=str(fits_path),
            output_npz=str(out_npz),
            width=64,
            height=32,
            workers=2,
            chunk_rows=8,
            write_zst=False,
        )

        assert out_npz.exists()
        assert summary["workers"] == 2
        with np.load(out_npz) as payload:
            assert "opacity_u16" in payload
            arr = payload["opacity_u16"]
            assert arr.shape == (32, 64)
            assert arr.dtype == np.uint16
            assert int(np.min(arr)) >= 0
            assert int(np.max(arr)) <= 65535
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_planck_converter_zst_branch_when_available() -> None:
    pytest.importorskip("zstandard")
    tmp_dir = _workspace_temp_dir()
    try:
        fits_path = tmp_dir / "planck_minimal_zst.fits"
        out_npz = tmp_dir / "planck_dust_u16.npz"
        out_zst = tmp_dir / "planck_dust_u16.npy.zst"
        _write_minimal_planck_fits(fits_path)

        summary = convert_planck_fits_to_cache(
            fits_path=str(fits_path),
            output_npz=str(out_npz),
            output_zst=str(out_zst),
            width=64,
            height=32,
            workers=2,
            chunk_rows=8,
            write_zst=True,
            zst_level=3,
        )

        assert summary["zst_written"] is True
        assert out_zst.exists()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
