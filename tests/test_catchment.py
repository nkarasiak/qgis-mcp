"""Tests for qgis_render_catchment — FakeExecutor, no QGIS.

The Voronoi/Processing build is QGIS-only and verified live; here we lock down
the MCP-side param threading + response translation.
"""

from __future__ import annotations

import os

from qgis_mcp_workflows.server import qgis_render_catchment


def _ok() -> dict:
    return {
        "output_path": "/tmp/c.png", "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 2,
        "method": "voronoi", "n_points": 1439, "n_catchments": 1439,
    }


def test_threads_params_and_returns_result(fake_executor):
    fake_executor.responses["render_catchment"] = _ok()
    r = qgis_render_catchment(points_path="/s.gpkg", output_png="/o.png")
    cmd, params = fake_executor.calls[0]
    assert cmd == "render_catchment"
    assert params["method"] == "voronoi"
    assert os.path.isabs(params["points_path"])
    assert r.n_catchments == 1439
    assert r.n_points == 1439


def test_basemap_spec_threaded(fake_executor):
    fake_executor.responses["render_catchment"] = _ok()
    qgis_render_catchment(
        points_path="s.gpkg", output_png="o.png", basemap="positron",
    )
    params = fake_executor.calls[0][1]
    assert os.path.isabs(params["points_path"])
    assert params["basemap_spec"]["kind"] == "xyz"
