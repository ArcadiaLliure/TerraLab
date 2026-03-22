import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

USECOLS = ["source_id", "ra", "dec", "phot_g_mean_mag", "bp_rp"]


def count_ecsv_rows(path: Path) -> int:
    count = 0
    header_seen = False
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line:
                continue
            if line.startswith("#"):
                continue
            if not header_seen:
                header_seen = True
                continue
            if line.strip():
                count += 1
    return count


def convert_file(
    ecsv_path: Path, npz_path: Path, chunksize: int, overwrite: bool
) -> None:
    if npz_path.exists() and not overwrite:
        print(f"[SKIP] {npz_path.name} already exists")
        return

    t0 = time.time()
    total_rows = count_ecsv_rows(ecsv_path)
    if total_rows <= 0:
        print(f"[WARN] No data rows: {ecsv_path.name}")
        return

    source_id = np.empty(total_rows, dtype=np.int64)
    ra = np.empty(total_rows, dtype=np.float32)
    dec = np.empty(total_rows, dtype=np.float32)
    mag = np.empty(total_rows, dtype=np.float32)
    bp_rp = np.empty(total_rows, dtype=np.float32)

    offset = 0
    reader = pd.read_csv(
        ecsv_path,
        comment="#",
        usecols=USECOLS,
        chunksize=chunksize,
        low_memory=False,
    )

    for chunk in reader:
        sid_chunk = (
            pd.to_numeric(chunk["source_id"], errors="coerce")
            .fillna(-1)
            .to_numpy(dtype=np.int64, copy=False)
        )
        ra_chunk = pd.to_numeric(chunk["ra"], errors="coerce").to_numpy(
            dtype=np.float32, copy=False
        )
        dec_chunk = pd.to_numeric(chunk["dec"], errors="coerce").to_numpy(
            dtype=np.float32, copy=False
        )
        mag_chunk = pd.to_numeric(
            chunk["phot_g_mean_mag"], errors="coerce"
        ).to_numpy(dtype=np.float32, copy=False)
        bprp_chunk = pd.to_numeric(chunk["bp_rp"], errors="coerce").to_numpy(
            dtype=np.float32, copy=False
        )

        n = len(chunk)
        end = offset + n
        if end > total_rows:
            n = total_rows - offset
            end = total_rows
            sid_chunk = sid_chunk[:n]
            ra_chunk = ra_chunk[:n]
            dec_chunk = dec_chunk[:n]
            mag_chunk = mag_chunk[:n]
            bprp_chunk = bprp_chunk[:n]

        source_id[offset:end] = sid_chunk
        ra[offset:end] = ra_chunk
        dec[offset:end] = dec_chunk
        mag[offset:end] = mag_chunk
        bp_rp[offset:end] = np.nan_to_num(
            bprp_chunk, nan=0.8, posinf=2.0, neginf=-0.5
        )

        offset = end
        print(f"[{ecsv_path.name}] {offset}/{total_rows}")

    if offset < total_rows:
        source_id = source_id[:offset]
        ra = ra[:offset]
        dec = dec[:offset]
        mag = mag[:offset]
        bp_rp = bp_rp[:offset]

    valid = np.isfinite(ra) & np.isfinite(dec) & np.isfinite(mag)
    if not np.all(valid):
        source_id = source_id[valid]
        ra = ra[valid]
        dec = dec[valid]
        mag = mag[valid]
        bp_rp = bp_rp[valid]

    np.savez(
        npz_path,
        source_id=source_id,
        ra=ra,
        dec=dec,
        mag=mag,
        bp_rp=bp_rp,
    )

    dt = time.time() - t0
    print(
        f"[OK] {ecsv_path.name} -> {npz_path.name} ({len(ra)} rows) in {dt:.1f}s"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Gaia ECSV catalogs to NPZ"
    )
    parser.add_argument(
        "--stars-dir",
        default=str(
            Path(__file__).resolve().parents[1] / "TerraLab" / "data" / "stars"
        ),
        help="Directory containing MAGNITUD_*.ecsv files",
    )
    parser.add_argument("--chunksize", type=int, default=1_000_000)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    stars_dir = Path(args.stars_dir)
    if not stars_dir.exists():
        raise SystemExit(f"Directory not found: {stars_dir}")

    ecsv_files = sorted(stars_dir.glob("MAGNITUD_*.ecsv"))
    if not ecsv_files:
        raise SystemExit(f"No MAGNITUD_*.ecsv files found in: {stars_dir}")

    print(f"Converting {len(ecsv_files)} ECSV files in: {stars_dir}")
    for ecsv_path in ecsv_files:
        npz_path = ecsv_path.with_suffix(".npz")
        convert_file(
            ecsv_path,
            npz_path,
            chunksize=args.chunksize,
            overwrite=args.overwrite,
        )


if __name__ == "__main__":
    main()
