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


def main() -> None:
    """CLI entry — placeholder for Task 4 (writes the GeoPackage)."""
    raise NotImplementedError("GeoPackage writing lands in Task 4 — parsing only for now.")


if __name__ == "__main__":
    main()
