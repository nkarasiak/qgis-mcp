"""Tests for qgis_render_diagram_map — FakeExecutor, no QGIS.

The QgsDiagramRenderer build is QGIS-only and verified live; here we lock down
the MCP-side param threading + response translation.
"""

from __future__ import annotations

import os

from qgis_mcp_workflows.server import qgis_render_diagram_map


def _ok() -> dict:
    return {
        "output_path": "/tmp/d.png", "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "diagram_type": "pie", "value_fields": ["am_arr", "am_dep"], "n_features": 225,
    }


def test_threads_params_and_returns_result(fake_executor):
    fake_executor.responses["render_diagram_map"] = _ok()
    r = qgis_render_diagram_map(
        layer_path="/z.gpkg", value_fields=["am_arr", "am_dep"],
        output_png="/o.png", diagram_type="pie", size=8.0,
    )
    cmd, params = fake_executor.calls[0]
    assert cmd == "render_diagram_map"
    assert params["value_fields"] == ["am_arr", "am_dep"]
    assert params["diagram_type"] == "pie"
    assert params["size"] == 8.0
    assert r.n_features == 225
    assert r.value_fields == ["am_arr", "am_dep"]


def test_paths_absolute_and_basemap_spec(fake_executor):
    fake_executor.responses["render_diagram_map"] = _ok()
    qgis_render_diagram_map(
        layer_path="rel.gpkg", value_fields=["x"], output_png="rel.png",
        basemap="positron",
    )
    params = fake_executor.calls[0][1]
    assert os.path.isabs(params["layer_path"])
    assert os.path.isabs(params["output_png"])
    assert params["basemap_spec"]["kind"] == "xyz"
