"""Query and validate stars datasets stored as NPY/NPZ.

Supports:
- structured NPY (e.g. stars_catalog.npy)
- split NPY directory (gaia_cache_*.npy)
- NPZ with common key names (ra/dec/mag/bp_rp/source_id)
- SQL-like queries (SELECT/FROM/WHERE/ORDER BY/LIMIT)
- SQL aggregates (MAX/MIN/AVG/SUM/COUNT)
- Virtual field `app_id` (HUD-compatible index sorted by magnitude)

Examples:
    python scripts/query_stars_npy.py --input C:\\Users\\Manel\\AppData\\Roaming\\TerraLab\\data\\stars_catalog.npy --where "phot_g_mean_mag<=13.5" --head 20
    python scripts/query_stars_npy.py --input TerraLab\\data\\temporal --where "ra~80:90" --where "dec~-10:10" --where "mag<=12" --stats
    python scripts/query_stars_npy.py --input TerraLab\\data\\temporal --where "phot_g_mean_mag<=11" --out out.csv
    python scripts/query_stars_npy.py --input TerraLab\\data\\temporal --sql "SELECT source_id, ra, dec, phot_g_mean_mag FROM stars WHERE phot_g_mean_mag <= 11 AND dec >= -10 ORDER BY phot_g_mean_mag ASC LIMIT 50"
    python scripts/query_stars_npy.py --input C:\\Users\\Manel\\AppData\\Roaming\\TerraLab\\data\\stars_catalog.npy --sql "SELECT MAX(phot_g_mean_mag) FROM stars WHERE dec < 0"
    python scripts/query_stars_npy.py --input C:\\Users\\Manel\\AppData\\Roaming\\TerraLab\\data\\stars_catalog.npy --sql "SELECT app_id, source_id, ra, dec, phot_g_mean_mag FROM stars WHERE app_id = 4738316"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import numpy as np


FIELD_ALIASES = {
    "id": "source_id",
    "sourceid": "source_id",
    "appid": "app_id",
    "app_id": "app_id",
    "rowid": "row_id",
    "row_id": "row_id",
    "ra": "ra",
    "dec": "dec",
    "mag": "phot_g_mean_mag",
    "g_mag": "phot_g_mean_mag",
    "phot_g_mean_mag": "phot_g_mean_mag",
    "bp_rp": "bp_rp",
    "bprp": "bp_rp",
}

COND_RE = re.compile(r"^\s*([A-Za-z_][\w]*)\s*(<=|>=|==|!=|<|>|~)\s*(.+?)\s*$")
SQL_RE = re.compile(
    r"^\s*select\s+(?P<select>.+?)\s+from\s+(?P<table>[A-Za-z_][\w]*)"
    r"(?:\s+where\s+(?P<where>.+?))?"
    r"(?:\s+order\s+by\s+(?P<order_field>[A-Za-z_][\w]*)(?:\s+(?P<order_dir>asc|desc))?)?"
    r"(?:\s+limit\s+(?P<limit>\d+))?\s*$",
    flags=re.IGNORECASE,
)
SQL_COND_RE = re.compile(r"^\s*([A-Za-z_][\w]*)\s*(<=|>=|<>|!=|=|<|>)\s*(.+?)\s*$", flags=re.IGNORECASE)
SQL_AGG_RE = re.compile(
    r"^\s*(max|min|avg|sum|count)\s*\(\s*(\*|[A-Za-z_][\w]*)\s*\)\s*(?:as\s+([A-Za-z_][\w]*))?\s*$",
    flags=re.IGNORECASE,
)


@dataclass
class SQLQuery:
    fields: List[str]
    conditions: List[Tuple[str, str, str]]
    sort: str
    limit: int | None
    aggregations: List[Tuple[str, str, str]]


def _strip_sql_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and ((v[0] == "'" and v[-1] == "'") or (v[0] == '"' and v[-1] == '"')):
        return v[1:-1]
    return v


def parse_sql_query(sql: str, columns: Dict[str, np.ndarray]) -> SQLQuery:
    normalized = " ".join(sql.strip().rstrip(";").split())
    m = SQL_RE.match(normalized)
    if not m:
        raise ValueError(
            "Invalid --sql query. Supported subset: "
            "SELECT <fields|*> FROM stars [WHERE cond AND cond] [ORDER BY field [ASC|DESC]] [LIMIT n]"
        )

    table = str(m.group("table") or "").lower()
    if table != "stars":
        raise ValueError(f"Unsupported table '{table}'. Use FROM stars")

    select_expr = str(m.group("select") or "").strip()
    select_parts = [part.strip() for part in select_expr.split(",") if part.strip()]
    if not select_parts:
        raise ValueError("SELECT list is empty")

    fields: List[str] = []
    aggregations: List[Tuple[str, str, str]] = []
    for part in select_parts:
        am = SQL_AGG_RE.match(part)
        if am:
            fn = str(am.group(1)).lower()
            arg_raw = str(am.group(2))
            alias_raw = am.group(3)
            if arg_raw == "*":
                arg_field = "*"
                default_alias = "count_all"
            else:
                arg_field = resolve_field(arg_raw, columns)
                default_alias = f"{fn}_{arg_field}"
            alias = str(alias_raw).strip() if alias_raw else default_alias
            aggregations.append((fn, arg_field, alias))
            continue

        if part == "*":
            if len(select_parts) > 1:
                raise ValueError("SELECT * cannot be combined with other columns/aggregates")
            fields = list(columns.keys())
            continue

        fields.append(resolve_field(part, columns))

    if aggregations and fields:
        raise ValueError("Mixed SELECT with aggregates and plain columns is not supported. Use one mode.")
    if not aggregations and not fields:
        raise ValueError("SELECT list is empty")

    conditions: List[Tuple[str, str, str]] = []
    where_expr = m.group("where")
    if where_expr:
        chunks = [chunk.strip() for chunk in re.split(r"\s+and\s+", where_expr, flags=re.IGNORECASE) if chunk.strip()]
        for chunk in chunks:
            cm = SQL_COND_RE.match(chunk)
            if not cm:
                raise ValueError(
                    f"Unsupported WHERE condition '{chunk}'. "
                    "Use field <op> value with operators: =, !=, <>, <, <=, >, >="
                )
            field_raw, op, value_raw = cm.group(1), cm.group(2), cm.group(3)
            field = resolve_field(field_raw, columns)
            op_norm = "==" if op == "=" else ("!=" if op in ("!=", "<>") else op)
            value = _strip_sql_value(value_raw)
            conditions.append((field, op_norm, value))

    sort = ""
    order_field = m.group("order_field")
    if order_field:
        sfield = resolve_field(order_field.strip(), columns)
        order_dir = str(m.group("order_dir") or "asc").strip().lower()
        if order_dir not in ("asc", "desc"):
            raise ValueError("ORDER BY direction must be ASC or DESC")
        sort = f"{sfield}:{order_dir}"

    limit = None
    limit_raw = m.group("limit")
    if limit_raw is not None:
        limit = int(limit_raw)
        if limit < 0:
            raise ValueError("LIMIT must be >= 0")

    return SQLQuery(fields=fields, conditions=conditions, sort=sort, limit=limit, aggregations=aggregations)


def _find_col(raw: Dict[str, np.ndarray], candidates: Iterable[str]) -> str | None:
    keys = list(raw.keys())
    lower_map = {k.lower(): k for k in keys}
    for name in candidates:
        if name in raw:
            return name
        k = lower_map.get(str(name).lower())
        if k is not None:
            return k
    return None


def _canonicalize(raw: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    out: Dict[str, np.ndarray] = {}

    mapping = {
        "source_id": ("source_id", "ids", "id"),
        "ra": ("ra", "RA"),
        "dec": ("dec", "DEC"),
        "phot_g_mean_mag": ("phot_g_mean_mag", "mag", "g_mag"),
        "bp_rp": ("bp_rp", "bprp"),
        "pmra": ("pmra",),
        "pmdec": ("pmdec",),
        "parallax": ("parallax",),
    }

    consumed = set()
    for target, candidates in mapping.items():
        found = _find_col(raw, candidates)
        if found is not None:
            out[target] = np.asarray(raw[found])
            consumed.add(found)

    # Keep extra fields too.
    for key, val in raw.items():
        if key not in consumed:
            out[key] = np.asarray(val)

    if "ra" not in out or "dec" not in out or "phot_g_mean_mag" not in out:
        missing = [k for k in ("ra", "dec", "phot_g_mean_mag") if k not in out]
        raise ValueError(f"Missing required columns: {missing}")

    # Normalize dtypes.
    out["ra"] = np.asarray(out["ra"], dtype=np.float64)
    out["dec"] = np.asarray(out["dec"], dtype=np.float64)
    out["phot_g_mean_mag"] = np.asarray(out["phot_g_mean_mag"], dtype=np.float32)
    if "bp_rp" in out:
        out["bp_rp"] = np.asarray(out["bp_rp"], dtype=np.float32)
    if "source_id" in out:
        out["source_id"] = np.asarray(out["source_id"], dtype=np.int64)
    return out


def _validate_lengths(data: Dict[str, np.ndarray]) -> int:
    lengths = {k: len(v) for k, v in data.items()}
    if not lengths:
        raise ValueError("No columns loaded")
    n = min(lengths.values())
    if any(v != n for v in lengths.values()):
        mismatched = {k: v for k, v in lengths.items() if v != n}
        raise ValueError(f"Column length mismatch. min={n} mismatched={mismatched}")
    return n


def load_structured_npy(path: Path) -> Dict[str, np.ndarray]:
    arr = np.load(path, allow_pickle=False)
    if not isinstance(arr, np.ndarray) or arr.dtype.names is None:
        raise ValueError(f"Not a structured NPY: {path}")
    raw = {name: np.asarray(arr[name]) for name in arr.dtype.names}
    return _canonicalize(raw)


def load_split_npy_dir(path: Path) -> Dict[str, np.ndarray]:
    required = {
        "ra": path / "gaia_cache_ra.npy",
        "dec": path / "gaia_cache_dec.npy",
        "phot_g_mean_mag": path / "gaia_cache_mag.npy",
    }
    for key, p in required.items():
        if not p.exists():
            raise FileNotFoundError(f"Missing {key} file: {p}")

    raw: Dict[str, np.ndarray] = {
        "ra": np.load(required["ra"], allow_pickle=False),
        "dec": np.load(required["dec"], allow_pickle=False),
        "phot_g_mean_mag": np.load(required["phot_g_mean_mag"], allow_pickle=False),
    }
    optional = {
        "bp_rp": path / "gaia_cache_bprp.npy",
        "source_id": path / "gaia_cache_ids.npy",
        "pmra": path / "gaia_cache_pmra.npy",
        "pmdec": path / "gaia_cache_pmdec.npy",
        "parallax": path / "gaia_cache_parallax.npy",
    }
    for key, p in optional.items():
        if p.exists():
            try:
                raw[key] = np.load(p, allow_pickle=False)
            except Exception:
                raw[key] = np.load(p, allow_pickle=True)
    return _canonicalize(raw)


def load_npz(path: Path) -> Dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as npz:
        raw = {k: np.asarray(npz[k]) for k in npz.files}
    return _canonicalize(raw)


def load_dataset(input_path: Path) -> Dict[str, np.ndarray]:
    if input_path.is_dir():
        data = load_split_npy_dir(input_path)
    elif input_path.suffix.lower() == ".npy":
        data = load_structured_npy(input_path)
    elif input_path.suffix.lower() == ".npz":
        data = load_npz(input_path)
    else:
        raise ValueError(f"Unsupported input: {input_path} (use dir/.npy/.npz)")
    _validate_lengths(data)
    return data


def add_virtual_fields(data: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    n = _validate_lengths(data)

    if "row_id" not in data:
        data["row_id"] = np.arange(n, dtype=np.int64)

    if "app_id" not in data:
        # HUD-compatible index: catalog sorted by phot_g_mean_mag (ascending, stable).
        mag = np.asarray(data["phot_g_mean_mag"], dtype=np.float64)
        order = np.argsort(mag, kind="mergesort")
        app_id = np.empty(n, dtype=np.int64)
        app_id[order] = np.arange(n, dtype=np.int64)
        data["app_id"] = app_id

    return data


def resolve_field(field: str, columns: Dict[str, np.ndarray]) -> str:
    alias = FIELD_ALIASES.get(field.lower(), field)
    if alias in columns:
        return alias
    for k in columns.keys():
        if k.lower() == alias.lower():
            return k
    raise KeyError(f"Unknown field '{field}'. Available: {', '.join(columns.keys())}")


def parse_where(where: str, columns: Dict[str, np.ndarray]) -> Tuple[str, str, str]:
    m = COND_RE.match(where)
    if not m:
        raise ValueError(f"Invalid --where expression: '{where}'")
    field_raw, op, value = m.group(1), m.group(2), m.group(3).strip()
    field = resolve_field(field_raw, columns)
    return field, op, value


def apply_condition(col: np.ndarray, op: str, value: str) -> np.ndarray:
    if col.dtype.kind in "iuf":
        c = np.asarray(col, dtype=np.float64)
        if op == "~":
            lo_s, hi_s = (value.split(":", 1) + [""])[:2]
            lo = float(lo_s) if lo_s != "" else -np.inf
            hi = float(hi_s) if hi_s != "" else np.inf
            return (c >= lo) & (c <= hi)
        v = float(value)
        if op == "<":
            return c < v
        if op == "<=":
            return c <= v
        if op == ">":
            return c > v
        if op == ">=":
            return c >= v
        if op == "==":
            return c == v
        if op == "!=":
            return c != v
        raise ValueError(f"Unsupported operator '{op}' for numeric field")

    s = np.asarray(col).astype(str)
    if op == "==":
        return s == value
    if op == "!=":
        return s != value
    raise ValueError(f"Operator '{op}' only supports numeric fields, except ==/!= for text")


def write_output(path: Path, fields: List[str], data: Dict[str, np.ndarray], idx: np.ndarray) -> None:
    if path.suffix.lower() == ".csv":
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(fields)
            for i in idx:
                writer.writerow([data[f][i].item() if hasattr(data[f][i], "item") else data[f][i] for f in fields])
        return
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        with path.open("w", encoding="utf-8") as fh:
            for i in idx:
                row = {f: (data[f][i].item() if hasattr(data[f][i], "item") else data[f][i]) for f in fields}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return
    raise ValueError("Unsupported --out format. Use .csv or .jsonl")


def write_aggregate_output(path: Path, labels: List[str], values: List[object]) -> None:
    if path.suffix.lower() == ".csv":
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(labels)
            writer.writerow(values)
        return
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        row = {k: v for k, v in zip(labels, values)}
        with path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return
    raise ValueError("Unsupported --out format. Use .csv or .jsonl")


def _format_scalar(value: object) -> str:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if np.isnan(value):
            return "nan"
    return str(value)


def compute_aggregations(
    aggregations: List[Tuple[str, str, str]],
    data: Dict[str, np.ndarray],
    idx: np.ndarray,
) -> Tuple[List[str], List[object]]:
    labels: List[str] = []
    values: List[object] = []

    for fn, field, alias in aggregations:
        labels.append(alias)
        if fn == "count":
            if field == "*":
                values.append(int(len(idx)))
            else:
                values.append(int(len(data[field][idx])))
            continue

        if field == "*":
            raise ValueError(f"{fn.upper()}(*) is not supported. Use COUNT(*) or {fn.upper()}(field)")

        col = np.asarray(data[field][idx])
        if col.dtype.kind not in "iuf":
            raise ValueError(f"{fn.upper()} requires numeric field, got '{field}' dtype={col.dtype}")
        c = np.asarray(col, dtype=np.float64)
        if len(c) == 0:
            values.append(float("nan"))
            continue

        if fn == "max":
            values.append(float(np.max(c)))
        elif fn == "min":
            values.append(float(np.min(c)))
        elif fn == "avg":
            values.append(float(np.mean(c)))
        elif fn == "sum":
            values.append(float(np.sum(c)))
        else:
            raise ValueError(f"Unsupported aggregate '{fn}'")

    return labels, values


def main() -> None:
    parser = argparse.ArgumentParser(description="Query stars NPY/NPZ datasets")
    parser.add_argument("--input", required=True, help="Path to dataset (.npy/.npz or split-npy directory)")
    parser.add_argument(
        "--sql",
        default="",
        help=(
            "SQL-like query, e.g. "
            "\"SELECT source_id,ra,dec,phot_g_mean_mag FROM stars "
            "WHERE phot_g_mean_mag <= 13.5 AND dec >= -10 ORDER BY phot_g_mean_mag ASC LIMIT 100\". "
            "Supports MAX/MIN/AVG/SUM/COUNT and virtual field app_id."
        ),
    )
    parser.add_argument("--where", action="append", default=[], help="Filter expression, e.g. phot_g_mean_mag<=13.5 or ra~80:90")
    parser.add_argument("--fields", default="source_id,ra,dec,phot_g_mean_mag,bp_rp", help="Comma-separated fields for output")
    parser.add_argument("--head", type=int, default=10, help="Preview rows")
    parser.add_argument("--sort", default="", help="Sort by field or field:desc")
    parser.add_argument("--count-only", action="store_true", help="Only print counts")
    parser.add_argument("--stats", action="store_true", help="Print numeric stats for selected fields")
    parser.add_argument("--out", default="", help="Output file (.csv or .jsonl)")
    args = parser.parse_args()

    data = add_virtual_fields(load_dataset(Path(args.input)))
    n_total = _validate_lengths(data)
    mask = np.ones(n_total, dtype=bool)
    conditions: List[Tuple[str, str, str]] = []
    selected_fields: List[str]
    sort_spec = args.sort
    row_limit: int | None = None
    sql_query: SQLQuery | None = None

    if args.sql.strip():
        sql_query = parse_sql_query(args.sql, data)
        selected_fields = sql_query.fields
        conditions.extend(sql_query.conditions)
        if sql_query.sort:
            sort_spec = sql_query.sort
        row_limit = sql_query.limit
    else:
        selected_fields = [resolve_field(x.strip(), data) for x in args.fields.split(",") if x.strip()]
        for expr in args.where:
            conditions.append(parse_where(expr, data))

    for field, op, value in conditions:
        mask &= apply_condition(data[field], op, value)

    idx = np.where(mask)[0]

    if sql_query is not None and sql_query.aggregations:
        labels, values = compute_aggregations(sql_query.aggregations, data, idx)
        print(f"rows_total={n_total}")
        print(f"rows_filtered={len(idx)}")
        if not args.count_only:
            print(",".join(labels))
            print(",".join(_format_scalar(v) for v in values))
        if args.out:
            out_path = Path(args.out)
            write_aggregate_output(out_path, labels, values)
            print(f"written={out_path} rows=1")
        return

    if sort_spec:
        sort_field, _, sort_dir = sort_spec.partition(":")
        sfield = resolve_field(sort_field.strip(), data)
        order = np.argsort(data[sfield][idx], kind="mergesort")
        if sort_dir.strip().lower() == "desc":
            order = order[::-1]
        idx = idx[order]

    if row_limit is not None:
        idx = idx[: max(0, int(row_limit))]

    print(f"rows_total={n_total}")
    print(f"rows_filtered={len(idx)}")

    if args.stats and selected_fields:
        for f in selected_fields:
            col = np.asarray(data[f][idx]) if len(idx) else np.asarray([], dtype=np.float64)
            if col.dtype.kind in "iuf" and len(col) > 0:
                colf = np.asarray(col, dtype=np.float64)
                print(
                    f"stats[{f}] min={np.min(colf):.6f} "
                    f"max={np.max(colf):.6f} mean={np.mean(colf):.6f} std={np.std(colf):.6f}"
                )

    if (not args.count_only) and args.head > 0 and len(idx) > 0:
        head_idx = idx[: args.head]
        print(",".join(selected_fields))
        for i in head_idx:
            vals = []
            for f in selected_fields:
                v = data[f][i]
                vals.append(_format_scalar(v))
            print(",".join(vals))

    if args.out:
        out_path = Path(args.out)
        write_output(out_path, selected_fields, data, idx)
        print(f"written={out_path} rows={len(idx)}")


if __name__ == "__main__":
    main()
