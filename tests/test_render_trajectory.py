"""Tests for qgis_render_trajectory — mocked plugin executor, no QGIS required.

Verifies the MCP-side responsibilities:
- CSV header validation (lon/lat/time/id columns) → FieldNotFoundError pre-dispatch
- Stride sampling (sample_rate + max_points ceiling)
- Empty-after-extent-filter → EmptyAfterFilterError
- GPX path bypasses CSV parse
- movingpandas integration: speed field attached when import succeeds
- Plugin receives a normalized payload (features list, render_mode, etc.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from qgis_mcp_workflows.errors import EmptyAfterFilterError, FieldNotFoundError
from qgis_mcp_workflows.server import qgis_render_trajectory


def _ok_response(**overrides) -> dict:
    base = {
        "output_path": "/tmp/traj.png",
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [139.68, 35.69, 139.74, 35.74],
        "crs": "EPSG:4326", "n_layers": 1,
        "n_trajectories": 3,
        "n_points_total": 30,
        "n_points_rendered": 30,
        "downsampled": False,
        "time_range": ["2026-01-01 08:00:00", "2026-01-01 10:34:30"],
        "modes": None,
        "used_movingpandas": False,
    }
    base.update(overrides)
    return base


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join(str(x) for x in r))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


FIXTURE_DIR = Path(__file__).parent / "fixtures"
TINY_TRAJ = FIXTURE_DIR / "tiny_trajectory.csv"


def test_csv_dispatched_with_features(fake_executor):
    """Happy path: tiny fixture parsed, dispatched to plugin."""
    fake_executor.responses["render_trajectory"] = _ok_response()

    result = qgis_render_trajectory(
        input_path=str(TINY_TRAJ),
        output_png="/tmp/traj.png",
        render_mode="lines",
    )

    cmd, params = fake_executor.calls[0]
    assert cmd == "render_trajectory"
    assert params["render_mode"] == "lines"
    assert len(params["features"]) == 30
    assert params["features"][0]["trip_id"] == "T01"
    assert params["features"][0]["lon"] == pytest.approx(139.700)
    assert params["features"][0]["lat"] == pytest.approx(35.700)
    assert result.n_trajectories == 3
    assert result.downsampled is False


def test_sample_rate_applied_stride(fake_executor):
    """sample_rate=0.1 keeps every 10th row (30 → 3)."""
    fake_executor.responses["render_trajectory"] = _ok_response(
        n_points_total=30, n_points_rendered=3
    )
    qgis_render_trajectory(
        input_path=str(TINY_TRAJ), output_png="/tmp/traj.png", sample_rate=0.1
    )
    params = fake_executor.calls[0][1]
    assert len(params["features"]) == 3


def test_max_points_triggers_downsample(fake_executor):
    """When rows > max_points, downsample_flag is set and features are capped."""
    fake_executor.responses["render_trajectory"] = _ok_response(
        n_points_total=30, n_points_rendered=10, downsampled=True
    )
    qgis_render_trajectory(
        input_path=str(TINY_TRAJ), output_png="/tmp/traj.png", max_points=10
    )
    params = fake_executor.calls[0][1]
    # 30 features capped to <=10 by second-stride sampling
    assert len(params["features"]) <= 10


def test_missing_lon_col_raises_field_not_found(fake_executor, tmp_path: Path):
    """CSV missing lon column fails before plugin dispatch."""
    bad_csv = tmp_path / "bad.csv"
    _write_csv(bad_csv, ["trip_id", "datetime", "latitude"], [["T01", "2026-01-01 00:00:00", "35.0"]])
    with pytest.raises(FieldNotFoundError, match="lon"):
        qgis_render_trajectory(input_path=str(bad_csv), output_png="/tmp/x.png")
    assert fake_executor.calls == []


def test_missing_id_col_raises_field_not_found(fake_executor, tmp_path: Path):
    bad_csv = tmp_path / "bad.csv"
    _write_csv(bad_csv, ["lon", "lat", "datetime"], [["139.7", "35.7", "2026-01-01 00:00:00"]])
    with pytest.raises(FieldNotFoundError, match="trip_id"):
        qgis_render_trajectory(input_path=str(bad_csv), output_png="/tmp/x.png")


def test_empty_after_extent_filter_raises(fake_executor):
    """Extent clip that drops every point → EmptyAfterFilterError before dispatch."""
    with pytest.raises(EmptyAfterFilterError, match="extent"):
        qgis_render_trajectory(
            input_path=str(TINY_TRAJ),
            output_png="/tmp/x.png",
            extent=[0.0, 0.0, 1.0, 1.0],  # nowhere near Tokyo
        )
    assert fake_executor.calls == []


def test_gpx_skips_csv_parse(fake_executor, tmp_path: Path):
    """Input ending in .gpx is sent through as a path, not parsed as CSV."""
    fake_executor.responses["render_trajectory"] = _ok_response()
    gpx_path = tmp_path / "track.gpx"
    gpx_path.write_text("<?xml version='1.0'?><gpx></gpx>", encoding="utf-8")
    qgis_render_trajectory(input_path=str(gpx_path), output_png="/tmp/x.png")
    params = fake_executor.calls[0][1]
    assert params.get("features") is None
    assert params["input_path"] == os.path.abspath(str(gpx_path))


def test_heatmap_mode_dispatches(fake_executor):
    fake_executor.responses["render_trajectory"] = _ok_response()
    qgis_render_trajectory(
        input_path=str(TINY_TRAJ), output_png="/tmp/x.png", render_mode="heatmap"
    )
    params = fake_executor.calls[0][1]
    assert params["render_mode"] == "heatmap"


def test_extent_clip_keeps_in_range_points(fake_executor):
    """Extent that catches only T01 (NW area) should leave 10 points."""
    fake_executor.responses["render_trajectory"] = _ok_response(
        n_points_total=30, n_points_rendered=10
    )
    qgis_render_trajectory(
        input_path=str(TINY_TRAJ),
        output_png="/tmp/x.png",
        extent=[139.69, 35.69, 139.72, 35.72],
    )
    params = fake_executor.calls[0][1]
    # T01 is at lon 139.700..139.718, lat 35.700..35.715 → all 10 inside
    # T02 is at lat 35.690..35.704 → some inside, some out
    # T03 is at lat 35.720..35.734 → all out
    feature_count = len(params["features"])
    assert 10 <= feature_count <= 20


def test_movingpandas_skipped_when_unavailable(fake_executor):
    """When movingpandas is not importable, used_movingpandas=False, no speed field."""
    fake_executor.responses["render_trajectory"] = _ok_response()
    with patch("qgis_mcp_workflows.server._HAS_MP", False):
        qgis_render_trajectory(
            input_path=str(TINY_TRAJ), output_png="/tmp/x.png", render_mode="lines"
        )
    params = fake_executor.calls[0][1]
    assert params.get("speed_field") is None
    assert params["used_movingpandas"] is False


def test_movingpandas_attaches_speed_when_available(fake_executor):
    """When movingpandas IS available and mode=lines and no mode_col, speed_kmh attaches.

    Requires pandas (test mock uses pd.DataFrame). pandas ships transitively via the
    [trajectory] extra; skip when developing against the base install.
    """
    pytest.importorskip("pandas")
    fake_executor.responses["render_trajectory"] = _ok_response(used_movingpandas=True)

    class _FakeTrajCollection:
        def __init__(self, *args, **kwargs):
            self.trajectories = []

        def add_speed(self, *args, **kwargs):
            pass

        def to_point_gdf(self):
            import pandas as pd
            return pd.DataFrame({"speed_kmh": [10.0] * 30})

    fake_mp = type(sys)("movingpandas")
    fake_mp.TrajectoryCollection = _FakeTrajCollection
    with (
        patch.dict(sys.modules, {"movingpandas": fake_mp}),
        patch("qgis_mcp_workflows.server._HAS_MP", True),
    ):
        qgis_render_trajectory(
            input_path=str(TINY_TRAJ),
            output_png="/tmp/x.png",
            render_mode="lines",
        )
    params = fake_executor.calls[0][1]
    assert params["used_movingpandas"] is True
    assert params.get("speed_field") == "speed_kmh"
    assert all("speed_kmh" in feat for feat in params["features"])


def test_paths_resolved_to_absolute(fake_executor, tmp_path: Path):
    fake_executor.responses["render_trajectory"] = _ok_response()
    # Use the fixture but pass an absolute path for output_png + relative basemap
    qgis_render_trajectory(
        input_path=str(TINY_TRAJ),
        output_png="rel_out.png",
        basemap_paths=["rel_basemap.shp"],
    )
    params = fake_executor.calls[0][1]
    assert os.path.isabs(params["output_png"])
    assert all(os.path.isabs(p) for p in params["basemap_paths"])
