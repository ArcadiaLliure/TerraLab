"""OpenNGC catalog loader and alias helpers used by search/overlay."""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


_NAME_RE = re.compile(r"^(NGC|IC)\s*0*([0-9]+[A-Za-z]?)$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(out):
        return float(default)
    return out


def _to_opt_float(value: object) -> Optional[float]:
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _to_opt_int(value: object) -> Optional[int]:
    try:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(float(text))
        except Exception:
            m = re.search(r"-?\d+", text)
            if not m:
                return None
            return int(m.group(0))
    except Exception:
        return None


def _clean_text(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _row_map(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        nk = _normalized_key(k)
        if nk and nk not in out:
            out[nk] = v
    return out


def _pick_value(mapped: dict, *keys: str):
    for k in keys:
        nk = _normalized_key(k)
        if nk in mapped:
            return mapped[nk]
    return None


def _pick_key_value(mapped: dict, *keys: str):
    for k in keys:
        nk = _normalized_key(k)
        if nk in mapped:
            return nk, mapped[nk]
    return "", None


def _split_sexagesimal(text: str) -> Optional[List[str]]:
    raw = str(text or "").strip()
    if not raw:
        return None
    t = (
        raw.replace("h", ":")
        .replace("m", ":")
        .replace("s", "")
        .replace("d", ":")
        .replace("'", ":")
        .replace('"', "")
    )
    if ":" in t:
        parts = [p.strip() for p in t.split(":") if str(p).strip()]
        return parts if len(parts) >= 2 else None
    parts = [p.strip() for p in re.split(r"\s+", t) if str(p).strip()]
    return parts if len(parts) >= 2 else None


def _parse_ra_deg(value: object, *, assume_hours_for_scalar: bool = False) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    sexa = _split_sexagesimal(text)
    if sexa is not None:
        try:
            hh = abs(float(sexa[0]))
            mm = abs(float(sexa[1])) if len(sexa) >= 2 else 0.0
            ss = abs(float(sexa[2])) if len(sexa) >= 3 else 0.0
            return float((hh + mm / 60.0 + ss / 3600.0) * 15.0) % 360.0
        except Exception:
            pass

    out = _to_opt_float(text)
    if out is None:
        return None
    if assume_hours_for_scalar and 0.0 <= out <= 24.0:
        return float(out * 15.0) % 360.0
    return float(out) % 360.0


def _parse_dec_deg(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    sexa = _split_sexagesimal(text)
    if sexa is not None:
        try:
            sign = -1.0 if str(sexa[0]).strip().startswith("-") else 1.0
            dd = abs(float(sexa[0]))
            mm = abs(float(sexa[1])) if len(sexa) >= 2 else 0.0
            ss = abs(float(sexa[2])) if len(sexa) >= 3 else 0.0
            out = sign * (dd + mm / 60.0 + ss / 3600.0)
            return float(max(-90.0, min(90.0, out)))
        except Exception:
            pass

    out = _to_opt_float(text)
    if out is None:
        return None
    return float(max(-90.0, min(90.0, out)))


def _first_common_name(value: object) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    for sep in ("|", ";"):
        text = text.replace(sep, ",")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return None
    return parts[0]


def _normalize_obj_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text)
    match = _NAME_RE.match(compact)
    if not match:
        return text
    prefix = match.group(1).upper()
    number = match.group(2).upper()
    return f"{prefix}{number}"


@dataclass(frozen=True)
class NGCObject:
    name: str
    obj_type: str
    ra_deg: float
    dec_deg: float
    maj_deg: float
    min_deg: float
    pos_ang_deg: float
    mag_v: Optional[float]
    mag_b: Optional[float]
    surf_br_B: Optional[float]
    hubble_type: Optional[str]
    messier_nr: Optional[int]
    common_name: Optional[str]
    notes: Optional[str]

    @property
    def effective_mag(self) -> float:
        if self.mag_v is not None and math.isfinite(self.mag_v):
            return float(self.mag_v)
        if self.mag_b is not None and math.isfinite(self.mag_b):
            return float(self.mag_b)
        return 99.0


def load_ngc_catalog(path: str | Path) -> List[NGCObject]:
    csv_path = Path(path)
    if not csv_path.exists():
        return []

    items: List[NGCObject] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        delimiter = ","
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
            delimiter = str(getattr(dialect, "delimiter", ",") or ",")
        except Exception:
            if ";" in sample and "," not in sample.splitlines()[0]:
                delimiter = ";"
        reader = csv.DictReader(fh, delimiter=delimiter)
        for row in reader:
            if not isinstance(row, dict):
                continue

            mapped = _row_map(row)

            ra = _parse_ra_deg(
                _pick_value(mapped, "raj2000", "ra_deg", "radeg"),
                assume_hours_for_scalar=False,
            )
            if ra is None:
                ra = _parse_ra_deg(_pick_value(mapped, "ra"), assume_hours_for_scalar=True)
            dec = _parse_dec_deg(_pick_value(mapped, "dej2000", "dec_deg", "decj2000", "dec"))
            if ra is None or dec is None:
                continue

            raw_name = _clean_text(_pick_value(mapped, "name")) or ""
            if not raw_name:
                ngc_nr = _to_opt_int(_pick_value(mapped, "ngc"))
                ic_nr = _to_opt_int(_pick_value(mapped, "ic"))
                if ngc_nr is not None and ngc_nr > 0:
                    raw_name = f"NGC {ngc_nr}"
                elif ic_nr is not None and ic_nr > 0:
                    raw_name = f"IC {ic_nr}"

            name = _normalize_obj_name(raw_name)
            if not name:
                continue

            maj_key, maj_raw = _pick_key_value(mapped, "maj_ax_deg", "majaxdeg", "majax")
            min_key, min_raw = _pick_key_value(mapped, "min_ax_deg", "minaxdeg", "minax")
            maj_val = _to_opt_float(maj_raw)
            min_val = _to_opt_float(min_raw)

            if maj_val is None:
                maj = 0.10
            elif maj_key == "majax":
                # OpenNGC raw CSV stores MajAx in arcminutes.
                maj = max(0.02, float(maj_val) / 60.0)
            else:
                maj = max(0.02, float(maj_val))

            if min_val is None:
                min_ax = maj
            elif min_key == "minax":
                # OpenNGC raw CSV stores MinAx in arcminutes.
                min_ax = max(0.01, float(min_val) / 60.0)
            else:
                min_ax = max(0.01, float(min_val))

            if min_ax > maj:
                maj, min_ax = min_ax, maj

            item = NGCObject(
                name=name,
                obj_type=str(_pick_value(mapped, "obj_type", "type") or "").strip(),
                ra_deg=float(ra),
                dec_deg=float(dec),
                maj_deg=float(maj),
                min_deg=float(min_ax),
                pos_ang_deg=_to_float(_pick_value(mapped, "pos_ang", "posang"), 0.0),
                mag_v=_to_opt_float(_pick_value(mapped, "mag_v", "vmag")),
                mag_b=_to_opt_float(_pick_value(mapped, "mag_b", "bmag")),
                surf_br_B=_to_opt_float(_pick_value(mapped, "surf_br_B", "surfbrb", "surfbr")),
                hubble_type=_clean_text(_pick_value(mapped, "hubble_type", "hubble")),
                messier_nr=_to_opt_int(_pick_value(mapped, "messier_nr", "m")),
                common_name=_first_common_name(_pick_value(mapped, "comname", "common_names", "common names")),
                notes=_clean_text(_pick_value(mapped, "notes", "openngc_notes", "opengc_notes", "ned_notes")),
            )
            items.append(item)

    items.sort(key=lambda o: (o.effective_mag, o.name))
    return items


def iter_ngc_aliases(obj: NGCObject) -> List[str]:
    out: List[str] = []
    seen = set()

    def _add(value: object) -> None:
        text = str(value or "").strip()
        if not text:
            return
        if text in seen:
            return
        seen.add(text)
        out.append(text)

    base = str(obj.name or "").strip()
    _add(base)

    compact = re.sub(r"\s+", "", base)
    _add(compact)

    match = _NAME_RE.match(compact)
    if match:
        prefix = match.group(1).upper()
        number = match.group(2).upper()
        _add(f"{prefix}{number}")
        _add(f"{prefix} {number}")

    messier = obj.messier_nr
    if messier is not None and messier > 0:
        _add(f"M{messier}")
        _add(f"M {messier}")
        _add(f"Messier {messier}")

    common = str(obj.common_name or "").strip()
    if common:
        _add(common)
        for token in _TOKEN_RE.findall(common):
            if len(token) >= 3:
                _add(token)

    return out

