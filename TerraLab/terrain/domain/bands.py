"""Terrain distance-band generation and naming."""



def format_band_distance(distance_m: float) -> str:
    """Format a distance for stable, human-readable terrain band IDs."""

    distance_m = float(distance_m)
    if distance_m < 1_000:
        return str(int(distance_m))
    if distance_m < 10_000:
        value_km = distance_m / 1_000
        if value_km != int(value_km):
            return f"{value_km:.1f}".rstrip("0").rstrip(".") + "k"
        return f"{int(value_km)}k"
    return f"{int(round(distance_m / 1_000))}k"


_fmt = format_band_distance


def generate_bands(n: int = 20, max_dist_m: float | None = None) -> list:
    """
    Genera N bandes d'horitzó amb distribució logarítmica per zones (piecewise bilog).

    Zones:
      · Zona propera  (0 → near_km):  2/3 de les N bandes.
        Recull detall de primer pla (colls, turons, cingleres).
      · Zona llunyana (near_km → max): 1/3 de les N bandes.
        Representa la boira atmosfèrica amb menys bandes (l'ull no n'aprecia el detall).

    La distància mínima de la primera banda és step_min_m (≥ resolució del baker)
    per evitar artefactes "paret" quan l'observador és a la vora d'un penya-segat.

    Args:
        n: Nombre total de bandes (10=Baix, 20=Normal, 40=Alt, 60=Ultra, 80=Extrem)
        max_dist_m: Distància màxima de càlcul en metres
    Returns:
        Llista de dicts amb 'id', 'min', 'max' en metres
    """
    import math as _math
    if max_dist_m is None:
        raise ValueError("max_dist_m must be the resolved visibility radius")
    max_dist_m = max(1.0, float(max_dist_m))

    # ── Paramètres de partició ────────────────────────────────────────────────
    # Distància de tall entre zona propera i zona llunyana
    near_km = 5_000.0  # metres
    # Fracció de bandes dedicades a la zona propera (2/3)
    near_frac = 2.0 / 3.0
    # Distància mínima del primer extrem de banda
    # El baker ara comença a 0.5m, però les bandes < 1m no aporten informació visual extra
    step_min_m = 1.0

    n_near = max(2, round(n * near_frac))
    n_far = max(1, n - n_near)

    # ── Zona propera: log entre step_min_m i near_km ─────────────────────────
    log_n_min = _math.log(step_min_m)
    log_n_max = _math.log(near_km)

    near_inner = []
    for i in range(1, n_near + 1):
        t = i / n_near
        v = _math.exp(log_n_min + (log_n_max - log_n_min) * t)
        near_inner.append(min(v, near_km))

    # ── Zona llunyana: log entre near_km i max_dist_m ────────────────────────
    log_f_min = _math.log(near_km)
    log_f_max = _math.log(max_dist_m)

    far_inner = []
    for i in range(1, n_far + 1):
        t = i / n_far
        v = _math.exp(log_f_min + (log_f_max - log_f_min) * t)
        far_inner.append(min(v, max_dist_m))

    # ── Punts de tall combinats ───────────────────────────────────────────────
    # Sempre comencem des de 0 i garantim near_km com a punt de transició
    breakpoints = [0.0] + near_inner + far_inner

    # ── Etiquetes de zona i format de noms ───────────────────────────────────
    _zone_labels = [
        (0, 750, "gnd"),
        (750, 5_000, "near"),
        (5_000, 25_000, "mid"),
        (25_000, 100_000, "far"),
        (100_000, 999_999, "haze"),
    ]

    def _zone_for(m):
        for lo, hi, label in _zone_labels:
            if m < hi:
                return label
        return "haze"

    # ── Construcció de la llista de bandes ────────────────────────────────────
    bands = []
    total_bps = len(breakpoints)
    for i in range(total_bps - 1):
        lo = breakpoints[i]
        hi = breakpoints[i + 1]
        if hi <= lo:
            continue  # Salta bandes buides (pot passar per arrodoniments)
        zone = _zone_for(lo)
        band_id = f"{zone}_{_fmt(lo)}_{_fmt(hi)}"
        bands.append({"id": band_id, "min": lo, "max": hi})

    return bands


# Àlies retrocompatible — qualitat per defecte = 20 bandes
DEFAULT_BANDS = None


