"""Generate a synthetic 134-polygon zone fixture for benchmarks.

Produces a regular grid of square polygons over a small area near Tokyo.
Output: tests/benchmarks/fixtures/scaled_zones_134.geojson — committed to the
repo so benchmarks don't depend on this script at run-time.

Run once:
    uv run --no-sync scripts/build_scaled_zones.py

Idempotent — regenerates the file in-place. Deterministic — no random numbers.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

# 134 polygons total: 12 cols by 12 rows = 144, trimmed to 134.
N_ZONES = 134
COLS = 12
ROWS = math.ceil(N_ZONES / COLS)

# Bounding box near Tokyo (synthetic — does not represent any real region).
LON_MIN = 139.50
LON_MAX = 139.80
LAT_MIN = 35.60
LAT_MAX = 35.85

LON_STEP = (LON_MAX - LON_MIN) / COLS
LAT_STEP = (LAT_MAX - LAT_MIN) / ROWS


def main() -> None:
    out_path = Path(__file__).resolve().parents[1] / "tests" / "benchmarks" / "fixtures" / "scaled_zones_134.geojson"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    features = []
    count = 0
    for row in range(ROWS):
        for col in range(COLS):
            if count >= N_ZONES:
                break
            x0 = LON_MIN + col * LON_STEP
            x1 = x0 + LON_STEP
            y0 = LAT_MIN + row * LAT_STEP
            y1 = y0 + LAT_STEP
            zone_id = f"S{count:03d}"
            # Synthetic deterministic value, useful as a choropleth target.
            total_trips = ((count * 37) % 10_000) + (count // 4)
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "zone_id": zone_id,
                        "total_trips": total_trips,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [x0, y0],
                                [x1, y0],
                                [x1, y1],
                                [x0, y1],
                                [x0, y0],
                            ]
                        ],
                    },
                }
            )
            count += 1

    payload = {
        "type": "FeatureCollection",
        "name": "scaled_zones_134",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }

    out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(features)} polygons to {out_path}")


if __name__ == "__main__":
    main()
