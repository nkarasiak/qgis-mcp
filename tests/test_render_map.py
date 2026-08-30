"""Tests for qgis_render_map — pragmatic, mocked-executor unit tests."""

from __future__ import annotations

import os

from qgis_mcp_workflows.server import qgis_render_map


def _render_response(out: str = "/tmp/x.png") -> dict:
    return {
        "output_path": out,
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [122.0, 24.0, 154.0, 46.0],
        "crs": "EPSG:4326",
        "n_layers": 1,
    }


def test_pass_through_to_plugin(fake_executor):
    fake_executor.responses["render_layers_to_path"] = _render_response("/abs/x.png")

    result = qgis_render_map(
        layer_ids=["L1", "L2"],
        output_png="/abs/x.png",
        width=2000, height=1500, dpi=200,
        extent=[120.0, 20.0, 150.0, 50.0],
        background="#fafafa",
    )

    cmd, params = fake_executor.calls[0]
    assert cmd == "render_layers_to_path"
    assert params["layer_ids"] == ["L1", "L2"]
    assert params["output_png"] == os.path.abspath("/abs/x.png")
    assert params["width"] == 2000
    assert params["height"] == 1500
    assert params["dpi"] == 200
    assert params["extent"] == [120.0, 20.0, 150.0, 50.0]
    assert params["background"] == "#fafafa"

    assert result.output_path == "/abs/x.png"
    assert result.n_layers == 1
    assert result.crs == "EPSG:4326"


def test_extent_omitted_when_none(fake_executor):
    """Plugin infers extent when not given — so we drop the param."""
    fake_executor.responses["render_layers_to_path"] = _render_response()
    qgis_render_map(layer_ids=["L1"], output_png="/x.png")
    params = fake_executor.calls[0][1]
    assert "extent" not in params or params["extent"] is None


def test_output_path_resolved_to_absolute(fake_executor):
    """Relative output paths get resolved client-side, then sent absolute."""
    fake_executor.responses["render_layers_to_path"] = _render_response()
    qgis_render_map(layer_ids=["L1"], output_png="relative.png")
    params = fake_executor.calls[0][1]
    assert os.path.isabs(params["output_png"])
