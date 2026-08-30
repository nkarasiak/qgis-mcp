"""Build assets/drm_network.gpkg from per-prefecture DRM TSVs.

DRM (Digital Road Map) source: H:\\Dropbox\\PFLOW\\data\\network\\drm_NN.tsv
(NN = 01..47 by prefecture, ~14 GB total, EPSG:4326, tab-separated, no header).

Schema per DESIGN.md §10:
    1=link_id, 2=from_node, 3=to_node, 4=road_class_code, 5-8=c5..c8 (opaque),
    9=from_lon, 10=from_lat, 11=to_lon, 12=to_lat, 13/14=wkt_linestring

Output: assets/drm_network.gpkg — single GeoPackage, all prefectures combined,
indexed by link_id. Used as the static layer for qgis_render_link_density.

Run once (or whenever DRM is updated):
    uv run --no-sync --extra drm scripts/build_drm_network.py \\
        --drm-dir 'H:/Dropbox/PFLOW/data/network' \\
        --output assets/drm_network.gpkg

Idempotent: regenerates in place. Skip-existing via --skip-existing.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Iterator
from pathlib import Path

# WKT LINESTRING parser — matches DRM's column 14 format. Captures the
# space-separated lon/lat pairs inside the parens.
_LINESTRING_RE = re.compile(r"^\s*LINESTRING\s*\(\s*(.+?)\s*\)\s*$", re.IGNORECASE)


def parse_drm_tsv(path: Path) -> Iterator[dict]:
    """Stream-parse a DRM TSV. Yields one dict per non-blank line.

    Each dict has keys: link_id (str), road_class (str), from_lon (float),
    from_lat (float), to_lon (float), to_lat (float), coords (list[tuple[float, float]]).

    Raises ValueError on a non-LINESTRING WKT in column 14 — silent skip would
    mask DRM-vintage drift.
    """
    with path.open(encoding="utf-8", newline="") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            cols = line.split("\t")
            if len(cols) < 13:
                raise ValueError(
                    f"{path}:{line_no} expected >=13 tab-separated columns, got {len(cols)}"
                )
            wkt_col = cols[13] if len(cols) > 13 else cols[12]
            match = _LINESTRING_RE.match(wkt_col)
            if not match:
                raise ValueError(
                    f"{path}:{line_no} column 14 not a LINESTRING WKT: {wkt_col[:80]!r}"
                )
            coords: list[tuple[float, float]] = []
            for pair in match.group(1).split(","):
                parts = pair.strip().split()
                if len(parts) != 2:
                    raise ValueError(
                        f"{path}:{line_no} malformed WKT coord pair: {pair!r}"
                    )
                lon, lat = float(parts[0]), float(parts[1])
                coords.append((lon, lat))
            yield {
                "link_id": cols[0],
                "road_class": cols[3],
                "from_lon": float(cols[8]),
                "from_lat": float(cols[9]),
                "to_lon": float(cols[10]),
                "to_lat": float(cols[11]),
                "coords": coords,
            }


def build_geopackage(
    tsv_paths: list[Path],
    output_path: Path,
    skip_existing: bool = False,
    progress_every: int = 100_000,
) -> dict:
    """Combine multiple DRM TSVs into a single GeoPackage.

    Returns a summary dict: {"n_links": int, "n_files": int, "elapsed_s": float}.
    """
    import pyogrio
    from shapely.geometry import LineString

    if skip_existing and output_path.exists():
        return {"n_links": 0, "n_files": 0, "elapsed_s": 0.0, "skipped": True}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    t0 = time.monotonic()
    n_links = 0

    import geopandas as gpd

    write_kwargs = dict(driver="GPKG", layer="links")
    first_file = True

    for tsv_path in tsv_paths:
        print(f"  reading {tsv_path.name} ...", flush=True)
        link_ids: list[str] = []
        road_classes: list[str] = []
        geoms: list[LineString] = []
        for row in parse_drm_tsv(tsv_path):
            link_ids.append(row["link_id"])
            road_classes.append(row["road_class"])
            geoms.append(LineString(row["coords"]))
            n_links += 1
            if n_links % progress_every == 0:
                print(f"    {n_links:,} links parsed", flush=True)

        gdf = gpd.GeoDataFrame(
            {"link_id": link_ids, "road_class": road_classes},
            geometry=geoms,
            crs="EPSG:4326",
        )
        if first_file:
            pyogrio.write_dataframe(gdf, output_path, append=False, **write_kwargs)
            first_file = False
        else:
            pyogrio.write_dataframe(gdf, output_path, append=True, **write_kwargs)

    elapsed = time.monotonic() - t0
    print(f"  wrote {n_links:,} links to {output_path} in {elapsed:.1f}s", flush=True)
    return {"n_links": n_links, "n_files": len(tsv_paths), "elapsed_s": elapsed}


def main() -> None:
    parser = argparse.ArgumentParser(prog="build_drm_network")
    parser.add_argument(
        "--drm-dir",
        default="H:/Dropbox/PFLOW/data/network",
        help="Directory containing drm_NN.tsv files (default: H:/Dropbox/PFLOW/data/network).",
    )
    parser.add_argument(
        "--output",
        default="assets/drm_network.gpkg",
        help="Output GeoPackage path (default: assets/drm_network.gpkg).",
    )
    parser.add_argument(
        "--pattern",
        default="drm_*.tsv",
        help="Glob pattern for input files (default: drm_*.tsv).",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Exit successfully if output already exists.",
    )
    args = parser.parse_args()

    drm_dir = Path(args.drm_dir)
    output = Path(args.output).resolve()

    tsv_paths = sorted(drm_dir.glob(args.pattern))
    if not tsv_paths:
        sys.exit(f"No files matching {args.pattern!r} in {drm_dir}")

    print(f"Found {len(tsv_paths)} TSV files in {drm_dir}", flush=True)
    summary = build_geopackage(
        tsv_paths=tsv_paths,
        output_path=output,
        skip_existing=args.skip_existing,
    )
    if summary.get("skipped"):
        print(f"Skipped — {output} already exists.")
        return
    print(
        f"Done: {summary['n_links']:,} links from {summary['n_files']} prefectures "
        f"-> {output} ({summary['elapsed_s']:.1f}s)."
    )


if __name__ == "__main__":
    main()
