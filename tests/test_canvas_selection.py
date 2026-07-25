import math

import numpy as np

from TerraLab.ui.canvas_selection import CanvasSelection
from TerraLab.ui.astro_canvas import AstroCanvas


def test_normalize_planet_key():
    assert CanvasSelection.normalize_planet_key("  Ju Piter  ") == "jupiter"
    assert CanvasSelection.normalize_planet_key("MARS") == "mars"


def test_extract_star_coords_valid():
    out = CanvasSelection.extract_star_coords({"ra": 12.5, "dec": -4.2})
    assert out is not None
    assert abs(out[0] - 12.5) < 1e-9
    assert abs(out[1] + 4.2) < 1e-9


def test_extract_star_coords_nan():
    assert (
        CanvasSelection.extract_star_coords({"ra": math.nan, "dec": 2.0})
        is None
    )
    assert (
        CanvasSelection.extract_star_coords({"ra": 2.0, "dec": math.nan})
        is None
    )


class _SkyPickStub:
    def __init__(self):
        self._selection = CanvasSelection(self)
        self.visible_sky_objects = []

    def _pick_ngc_at(self, sx, sy, click_radius=26.0):
        return None


def test_pick_sky_object_at_hit():
    stub = _SkyPickStub()
    stub.visible_sky_objects = [
        {
            "type": "sun",
            "key": "sun",
            "name": "Sun",
            "alt": 20.0,
            "az": 180.0,
            "sx": 100.0,
            "sy": 100.0,
            "radius_px": 8.0,
        }
    ]
    picked = AstroCanvas._pick_sky_object_at(
        stub, 108.0, 104.0, click_radius=20.0
    )
    assert picked is not None
    assert picked.get("type") == "sun"


def test_pick_sky_object_at_miss():
    stub = _SkyPickStub()
    stub.visible_sky_objects = [
        {
            "type": "sun",
            "key": "sun",
            "name": "Sun",
            "alt": 20.0,
            "az": 180.0,
            "sx": 100.0,
            "sy": 100.0,
            "radius_px": 8.0,
        }
    ]
    picked = AstroCanvas._pick_sky_object_at(
        stub, 500.0, 500.0, click_radius=20.0
    )
    assert picked is None


def test_pick_sky_object_empty_list():
    stub = _SkyPickStub()
    picked = AstroCanvas._pick_sky_object_at(
        stub, 100.0, 100.0, click_radius=20.0
    )
    assert picked is None


def test_register_visible_sky_object_appends_pickable_item():
    stub = _SkyPickStub()
    AstroCanvas._register_visible_sky_object(
        stub,
        "planet",
        "jupiter",
        "Jupiter",
        31.2,
        210.5,
        320.0,
        180.0,
        6.5,
        mag=-2.1,
    )
    assert len(stub.visible_sky_objects) == 1
    item = stub.visible_sky_objects[0]
    assert item["type"] == "planet"
    assert item["key"] == "jupiter"
    assert item["az"] == 210.5


class _StarPickStub:
    _pick_star_at = AstroCanvas._pick_star_at

    def __init__(self):
        self._selection = CanvasSelection(self)
        self.visible_stars = [0]
        self.visible_stars_sx = np.array([150.0], dtype=np.float32)
        self.visible_stars_sy = np.array([120.0], dtype=np.float32)
        self._active_catalog_ra = np.array([10.0], dtype=np.float32)
        self._active_catalog_dec = np.array([20.0], dtype=np.float32)
        self._active_catalog_mag = np.array([2.5], dtype=np.float32)
        self._active_catalog_bp_rp = np.array([0.8], dtype=np.float32)
        self.parent_widget = type(
            "PW",
            (),
            {
                "np_ra": np.array([10.0], dtype=np.float32),
                "np_dec": np.array([20.0], dtype=np.float32),
                "np_mag": np.array([2.5], dtype=np.float32),
                "np_bp_rp": np.array([0.8], dtype=np.float32),
            },
        )()


def test_pick_star_at_resolves_visible_index_to_star_object():
    stub = _StarPickStub()
    star = stub._pick_star_at(151.0, 119.0, click_radius=20.0)
    assert star is not None
    assert abs(float(star["ra"]) - 10.0) < 1e-6
    assert abs(float(star["dec"]) - 20.0) < 1e-6
    assert abs(float(star["mag"]) - 2.5) < 1e-6


class _ScopeFallbackPickStub:
    _pick_star_at = AstroCanvas._pick_star_at
    _resolve_star_by_index = AstroCanvas._resolve_star_by_index

    def __init__(self):
        self._selection = CanvasSelection(self)
        self.visible_stars = [1]
        self.visible_stars_sx = np.array([201.0], dtype=np.float32)
        self.visible_stars_sy = np.array([101.0], dtype=np.float32)
        self.parent_widget = type(
            "PW",
            (),
            {
                # Catalog legacy global (wrong star for this visible index).
                "np_ra": np.array([300.0, 301.0], dtype=np.float32),
                "np_dec": np.array([50.0, 51.0], dtype=np.float32),
                "np_mag": np.array([1.0, 1.1], dtype=np.float32),
                "np_bp_rp": np.array([0.0, 0.1], dtype=np.float32),
                # Scope fallback catalog (true source rendered on screen).
                "_stars_fallback_active": True,
                "_scope_base_ra": np.array([10.0, 42.0], dtype=np.float32),
                "_scope_base_dec": np.array([5.0, -7.0], dtype=np.float32),
                "_scope_base_mag": np.array([2.0, 3.5], dtype=np.float32),
                "_scope_base_bp_rp": np.array([0.3, 0.9], dtype=np.float32),
                "_scope_base_r": np.array([220.0, 180.0], dtype=np.float32),
                "_scope_base_g": np.array([210.0, 170.0], dtype=np.float32),
                "_scope_base_b": np.array([200.0, 160.0], dtype=np.float32),
            },
        )()


def test_pick_star_at_uses_scope_fallback_catalog_when_active():
    stub = _ScopeFallbackPickStub()
    star = stub._pick_star_at(200.0, 100.0, click_radius=20.0)
    assert star is not None
    assert abs(float(star["ra"]) - 42.0) < 1e-6
    assert abs(float(star["dec"]) - (-7.0)) < 1e-6
    assert abs(float(star["mag"]) - 3.5) < 1e-6


class _ActiveCatalogPickStub:
    _pick_star_at = AstroCanvas._pick_star_at
    _resolve_star_by_index = AstroCanvas._resolve_star_by_index

    def __init__(self):
        self._selection = CanvasSelection(self)
        self.visible_stars = [0]
        self.visible_stars_sx = np.array([50.0], dtype=np.float32)
        self.visible_stars_sy = np.array([60.0], dtype=np.float32)
        self._active_catalog_ra = np.array([123.4], dtype=np.float32)
        self._active_catalog_dec = np.array([-22.5], dtype=np.float32)
        self._active_catalog_mag = np.array([4.7], dtype=np.float32)
        self._active_catalog_bp_rp = np.array([1.2], dtype=np.float32)
        self._active_catalog_r = np.array([170.0], dtype=np.float32)
        self._active_catalog_g = np.array([165.0], dtype=np.float32)
        self._active_catalog_b = np.array([150.0], dtype=np.float32)
        self.parent_widget = type(
            "PW",
            (),
            {
                # Legacy arrays intentionally different from active catalog.
                "np_ra": np.array([250.0], dtype=np.float32),
                "np_dec": np.array([35.0], dtype=np.float32),
                "np_mag": np.array([0.5], dtype=np.float32),
                "np_bp_rp": np.array([0.0], dtype=np.float32),
                "_stars_fallback_active": False,
            },
        )()


def test_pick_star_at_prefers_active_render_catalog():
    stub = _ActiveCatalogPickStub()
    star = stub._pick_star_at(51.0, 61.0, click_radius=20.0)
    assert star is not None
    assert abs(float(star["ra"]) - 123.4) < 1e-5
    assert abs(float(star["dec"]) - (-22.5)) < 1e-5
    assert abs(float(star["mag"]) - 4.7) < 1e-5


class _LookupSkyStub(_SkyPickStub):
    _lookup_visible_sky_object = AstroCanvas._lookup_visible_sky_object
    _normalize_planet_key = AstroCanvas._normalize_planet_key


def test_lookup_visible_sky_object_matches_planet_key():
    stub = _LookupSkyStub()
    stub.visible_sky_objects = [
        {
            "type": "planet",
            "key": "jupiter",
            "name": "Jupiter",
            "alt": 30.0,
            "az": 200.0,
            "sx": 0.0,
            "sy": 0.0,
            "radius_px": 4.0,
        },
        {
            "type": "planet",
            "key": "mars",
            "name": "Mars",
            "alt": 40.0,
            "az": 220.0,
            "sx": 0.0,
            "sy": 0.0,
            "radius_px": 4.0,
        },
    ]
    target = {
        "kind": "sky",
        "type": "planet",
        "key": "jupiter",
        "alt": 30.0,
        "az": 200.0,
    }
    item = stub._lookup_visible_sky_object(target)
    assert item is not None
    assert item["key"] == "jupiter"
