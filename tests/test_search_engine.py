from TerraLab.astro.ngc_catalog import NGCObject
from TerraLab.astro.search_engine import (
    AstroSearchEngine,
    build_search_index_for_widget,
)


def _engine() -> AstroSearchEngine:
    engine = AstroSearchEngine()
    engine.build_index(
        celestial_objects=[{"name": "Capella", "ra": 79.17, "dec": 45.99}],
        named_star_entries=[{"name": "Rigel", "ra": 78.63, "dec": -8.20}],
        ngc_entries=[
            NGCObject(
                name="NGC1976",
                obj_type="Cl+N",
                ra_deg=83.8187,
                dec_deg=-5.3897,
                maj_deg=1.5,
                min_deg=1.0,
                pos_ang_deg=0.0,
                mag_v=4.0,
                mag_b=4.0,
                surf_br_B=None,
                hubble_type=None,
                messier_nr=42,
                common_name="Orion Nebula",
                notes=None,
            )
        ],
    )
    return engine


def test_lookup_jupiter():
    info = _engine().lookup("Jupiter")
    assert info is not None
    assert info["type"] == "planet"


def test_lookup_case_insensitive():
    info = _engine().lookup("MARS")
    assert info is not None
    assert info["type"] == "planet"


def test_lookup_accents():
    info = _engine().lookup("Júpiter")
    assert info is not None
    assert info["type"] == "planet"


def test_lookup_named_star():
    info = _engine().lookup("Capella")
    assert info is not None
    assert info["type"] == "star"


def test_lookup_not_found():
    assert _engine().lookup("DefinitelyNotInCatalog") is None


def test_lookup_partial():
    info = _engine().lookup("Jup")
    assert info is not None
    assert info["type"] == "planet"


def test_normalize_key_accents():
    key = AstroSearchEngine.normalize_key("Júpiter")
    assert key == "jupiter"


class _AnimTimer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        """Executa el metode stop de la classe _AnimTimer.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        self.stopped = True


class _ScopeController:
    def __init__(self):
        self.center = None

    def set_center(self, center):
        """Defineix center a la instancia de _ScopeController.

        Par?metres:
        - center (Any): Valor del parametre 'center'.

        Retorna:
        - None.
        """
        self.center = center


class _CanvasStub:
    def __init__(self):
        self.selected_target = None
        self.azimuth_offset = 0.0
        self.elevation_angle = 0.0
        self.dragging = True
        self.scope_controller = _ScopeController()

    def _set_selected_target(self, payload):
        self.selected_target = payload

    def scope_mode_enabled(self):
        """Executa el metode scope_mode_enabled de la classe _CanvasStub.

        Par?metres:
        - Cap.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        return False

    def update(self):
        """Executa el metode update de la classe _CanvasStub.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        return None


class _WidgetStub:
    def __init__(self):
        self.canvas = _CanvasStub()
        self.anim_timer = _AnimTimer()
        self.target_azimuth = 1.0
        self.target_elevation = 1.0

    def get_horizontal_coords(self, ra, dec):
        """Obte horizontal coords de la instancia de _WidgetStub.

        Par?metres:
        - ra (Any): Valor del parametre 'ra'.
        - dec (Any): Valor del parametre 'dec'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        _ = (ra, dec)
        return 123.0, 45.0

    def _prepare_skyfield_cache_for_search(self):
        return {
            "planets": [
                {
                    "key": "jupiter",
                    "name": "Jupiter",
                    "az": 210.5,
                    "alt": 31.2,
                },
            ]
        }


def test_center_on_object_sets_star_payload_compatible_with_canvas():
    widget = _WidgetStub()
    info = {
        "type": "star",
        "obj": {"name": "Capella", "ra": 79.17, "dec": 45.99},
    }
    _engine().center_on_object(widget, info)

    assert widget.canvas.selected_target is not None
    assert widget.canvas.selected_target["kind"] == "star"
    assert "star" in widget.canvas.selected_target
    assert widget.canvas.azimuth_offset == 123.0
    assert widget.canvas.elevation_angle == 45.0


def test_center_on_object_sets_planet_payload_as_sky_target():
    widget = _WidgetStub()
    info = {"type": "planet", "key": "jupiter", "name": "Jupiter"}
    _engine().center_on_object(widget, info)

    assert widget.canvas.selected_target is not None
    assert widget.canvas.selected_target["kind"] == "sky"
    assert widget.canvas.selected_target["type"] == "planet"
    assert widget.canvas.selected_target["key"] == "jupiter"
    assert widget.canvas.selected_target["az"] == 210.5
    assert widget.canvas.selected_target["alt"] == 31.2


class _WidgetIndexStub:
    def __init__(self):
        self.celestial_objects = []
        self.search_index = {}
        self.search_lookup = {}
        self.attached_names = []

    def _load_named_star_search_entries(self):
        return []

    def _load_ngc_search_entries(self):
        return [
            NGCObject(
                name="NGC1976",
                obj_type="Cl+N",
                ra_deg=83.8187,
                dec_deg=-5.3897,
                maj_deg=1.5,
                min_deg=1.0,
                pos_ang_deg=0.0,
                mag_v=4.0,
                mag_b=4.0,
                surf_br_B=None,
                hubble_type=None,
                messier_nr=42,
                common_name="Orion Nebula",
                notes=None,
            )
        ]

    def _attach_search_completer(self, names):
        self.attached_names = list(names)


def test_build_search_index_includes_ngc_even_if_layer_visibility_is_off():
    widget = _WidgetIndexStub()
    build_search_index_for_widget(widget)
    keys = [str(k).lower() for k in widget.search_index.keys()]
    assert any(k in {"m42", "messier 42", "ngc1976", "ngc 1976"} for k in keys)
