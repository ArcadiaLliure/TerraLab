"""Search index helpers for planets, stars and NGC objects."""

from __future__ import annotations

import json
import math
import os
import unicodedata
from pathlib import Path
from typing import Optional

from TerraLab.astro.ngc_catalog import iter_ngc_aliases, load_ngc_catalog
from TerraLab.common.utils import getTraduction


class AstroSearchEngine:
    def __init__(self) -> None:
        self.search_index: dict[str, dict] = {}
        self.search_lookup: dict[str, dict] = {}

    @staticmethod
    def normalize_key(text: str) -> str:
        lowered = (text or "").strip().lower()
        folded = unicodedata.normalize("NFKD", lowered)
        return "".join(ch for ch in folded if not unicodedata.combining(ch))

    def build_index(self, celestial_objects, named_star_entries, ngc_entries):
        self.search_index = {}
        self.search_lookup = {}

        def register_name(name: str, info: dict) -> None:
            if not name:
                return
            n = str(name).strip()
            if not n:
                return
            self.search_index[n] = info
            k = self.normalize_key(n)
            if k and k not in self.search_lookup:
                self.search_lookup[k] = info

        planet_aliases = {
            "sun": ["Sol", "Sun", "Soleil", "Sole"],
            "moon": ["Lluna", "Luna", "Moon", "Lune"],
            "mercury": ["Mercuri", "Mercurio", "Mercury", "Mercure"],
            "venus": ["Venus"],
            "mars": ["Mart", "Marte", "Mars"],
            "jupiter": ["Jupiter", "Júpiter"],
            "saturn": ["Saturn", "Saturno"],
            "uranus": ["Urà", "Ura", "Urano", "Uranus"],
            "neptune": ["Neptú", "Neptu", "Neptuno", "Neptune"],
            "pluto": ["Plutó", "Pluto", "Plutón", "Pluton"],
        }
        for key, aliases in planet_aliases.items():
            canon = aliases[0]
            info = {"type": "planet", "key": key, "name": canon}
            for alias in aliases:
                register_name(alias, info)

        for star in celestial_objects or []:
            if not isinstance(star, dict):
                continue
            name = str(star.get("name", "")).strip()
            if name and not name.lower().startswith("gaia"):
                register_name(name, {"type": "star", "obj": star})

        for star in named_star_entries or []:
            if not isinstance(star, dict):
                continue
            name = str(star.get("name", "")).strip()
            if name:
                register_name(name, {"type": "star", "obj": star})

        for obj in ngc_entries or []:
            info = {"type": "ngc", "obj": obj, "name": getattr(obj, "common_name", None) or getattr(obj, "name", "NGC")}
            for alias in iter_ngc_aliases(obj):
                register_name(alias, info)

        return self.search_index, self.search_lookup

    def names(self) -> list[str]:
        return sorted(list(self.search_index.keys()), key=lambda x: x.lower())

    def lookup(self, text: str) -> Optional[dict]:
        raw = str(text or "").strip()
        if not raw:
            return None
        info = self.search_index.get(raw)
        if info is not None:
            return info
        norm = self.normalize_key(raw)
        info = self.search_lookup.get(norm)
        if info is not None:
            return info
        for key in sorted(self.search_lookup.keys()):
            if norm and norm in key:
                return self.search_lookup[key]
        return None

    def center_on_object(self, widget, info: dict) -> None:
        az = alt = None
        selected_payload = None

        if info["type"] == "star":
            star = info["obj"]
            az, alt = widget.get_horizontal_coords(star["ra"], star["dec"])
            selected_payload = {"kind": "star", "star": star, "info": info}
        elif info["type"] == "planet":
            data = widget._prepare_skyfield_cache_for_search()
            if data:
                p_key = self.normalize_key(info.get("key", ""))
                target_name = self.normalize_key(info.get("name", ""))
                kind = "planet"
                key = p_key or target_name or "planet"
                label = str(info.get("name") or key.title())

                if p_key == "sun" or target_name == "sol":
                    sun = data.get("sun", {})
                    az = sun.get("az")
                    alt = sun.get("alt")
                    kind = "sun"
                    key = "sun"
                    label = "Sun"
                elif p_key == "moon" or target_name in ("lluna", "luna", "moon"):
                    moon = data.get("moon", {})
                    az = moon.get("az")
                    alt = moon.get("alt")
                    kind = "moon"
                    key = "moon"
                    label = "Moon"
                else:
                    for p in data.get("planets", []):
                        p_key_low = self.normalize_key(p.get("key", ""))
                        p_name_low = self.normalize_key(p.get("name", ""))
                        if p_key_low.startswith(p_key) or p_name_low == target_name:
                            az = p.get("az")
                            alt = p.get("alt")
                            key = p_key_low or p_name_low or key
                            label = str(p.get("name", label))
                            break
                selected_payload = {
                    "kind": "sky",
                    "type": kind,
                    "key": key,
                    "name": label,
                }
        elif info["type"] == "ngc":
            obj = info["obj"]
            az, alt = widget.get_horizontal_coords(obj.ra_deg, obj.dec_deg)
            selected_payload = {"kind": "ngc", "obj": obj, "info": info}
        else:
            selected_payload = {"kind": str(info.get("type", "object")), "obj": info.get("obj"), "info": info}

        if az is not None and alt is not None:
            az %= 360.0
            if isinstance(selected_payload, dict) and selected_payload.get("kind") == "sky":
                selected_payload["az"] = float(az)
                selected_payload["alt"] = float(alt)
            widget.target_azimuth = None
            widget.target_elevation = None
            if hasattr(widget, "anim_timer"):
                widget.anim_timer.stop()
            if hasattr(widget.canvas, "_set_selected_target"):
                widget.canvas._set_selected_target(selected_payload)
            widget.canvas.azimuth_offset = az
            widget.canvas.elevation_angle = alt
            widget.canvas.dragging = False
            if widget.canvas.scope_mode_enabled():
                widget.canvas.scope_controller.set_center((alt, az))
            if alt < -5 and hasattr(widget.canvas, "hint_overlay"):
                hint = "Object below horizon ({alt:.1f} deg)".format(alt=alt)
                widget.canvas.hint_overlay.show_hint(hint)
            widget.canvas.update()


def ensure_widget_search_engine(widget) -> AstroSearchEngine:
    engine = widget.__dict__.get("_search_engine")
    if engine is None:
        engine = AstroSearchEngine()
        widget.__dict__["_search_engine"] = engine
    return engine


def build_search_index_for_widget(widget) -> None:
    engine = ensure_widget_search_engine(widget)
    named_entries = widget._load_named_star_search_entries()
    # Search index must remain stable regardless of layer visibility.
    # Deep-sky overlay toggles rendering only, not search discoverability.
    ngc_entries = widget._load_ngc_search_entries()
    engine.build_index(
        getattr(widget, "celestial_objects", []),
        named_entries,
        ngc_entries,
    )
    widget.search_index = dict(engine.search_index)
    widget.search_lookup = dict(engine.search_lookup)
    widget._attach_search_completer(engine.names())
    print(f"[AstroWidget] Search index built: {len(widget.search_index)} objects.")


def on_search_triggered_for_widget(widget, text_override=None) -> None:
    raw = text_override if isinstance(text_override, str) else widget.txt_search.text()
    text = str(raw).strip()
    if not text:
        return
    engine = ensure_widget_search_engine(widget)
    info = engine.lookup(text)
    if info:
        center_on_object_for_widget(widget, info)
    else:
        msg = getTraduction("Astro.SearchNotFound", "Object '{name}' not found in index.").format(name=text)
        print(f"[AstroWidget] {msg}")


def center_on_object_for_widget(widget, info):
    engine = ensure_widget_search_engine(widget)
    return engine.center_on_object(widget, info)


def _resolve_named_star_path(path: str | Path | None) -> str:
    if path:
        p = Path(path)
        if p.is_file():
            return str(p)
    return str(Path(__file__).resolve().parents[1] / "data" / "stars" / "no_gaia_stars.json")


def load_named_star_entries(path: str | Path | None = None):
    resolved = _resolve_named_star_path(path)
    if not os.path.isfile(resolved):
        return []

    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return []

    rows = []
    names = []
    if isinstance(payload, dict):
        rows = payload.get("data") or []
        metadata = payload.get("metadata") or []
        for item in metadata:
            if isinstance(item, dict):
                names.append(str(item.get("name", "")))
    elif isinstance(payload, list):
        rows = payload

    if not rows or not names:
        return []

    idx = {name: i for i, name in enumerate(names)}
    name_i = idx.get("designation")
    ra_i = idx.get("ra")
    dec_i = idx.get("dec")
    sid_i = idx.get("source_id")
    if name_i is None or ra_i is None or dec_i is None:
        return []

    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        if max(name_i, ra_i, dec_i) >= len(row):
            continue
        try:
            name = str(row[name_i] or "").strip()
            if (not name) or name.lower().startswith("gaia dr3 "):
                continue
            ra = float(row[ra_i])
            dec = float(row[dec_i])
            if not math.isfinite(ra) or not math.isfinite(dec):
                continue
            info = {"name": name, "ra": ra, "dec": dec}
            if sid_i is not None and sid_i < len(row):
                try:
                    info["source_id"] = int(row[sid_i])
                except Exception:
                    pass
            out.append(info)
        except Exception:
            continue
    return out


def load_ngc_entries(path: str | Path | None):
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    try:
        return load_ngc_catalog(p)
    except Exception:
        return []
