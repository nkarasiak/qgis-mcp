"""Tests for qgis_compose_layout — FakeExecutor, no QGIS.

The programmatic QgsPrintLayout building is QGIS-only and verified live; here we
lock down the MCP-side param threading + response translation.
"""

from __future__ import annotations

import os

from qgis_mcp_workflows.server import qgis_compose_layout


def _ok() -> dict:
    return {
        "output_path": "/tmp/fig.png",
        "format": "png",
        "n_layers": 2,
        "items": ["map", "title", "legend", "scalebar", "north_arrow"],
        "page_size_mm": [297.0, 210.0],
    }


def test_threads_params_and_returns_result(fake_executor):
    fake_executor.responses["compose_layout"] = _ok()
    r = qgis_compose_layout(
        layer_paths=["/a.gpkg", "/b.shp"],
        output_path="/tmp/fig.png",
        title="Net flux",
        page="a4_landscape",
    )
    cmd, params = fake_executor.calls[0]
    assert cmd == "compose_layout"
    assert params["title"] == "Net flux"
    assert params["page"] == "a4_landscape"
    assert r.n_layers == 2
    assert "legend" in r.items
    assert r.page_size_mm == [297.0, 210.0]


def test_paths_absolute_and_toggles(fake_executor):
    fake_executor.responses["compose_layout"] = _ok()
    qgis_compose_layout(
        layer_paths=["rel.gpkg"],
        output_path="rel.png",
        legend=False,
        scale_bar=False,
        north_arrow=False,
    )
    params = fake_executor.calls[0][1]
    assert all(os.path.isabs(p) for p in params["layer_paths"])
    assert os.path.isabs(params["output_path"])
    assert params["legend"] is False
    assert params["scale_bar"] is False
    assert params["north_arrow"] is False
