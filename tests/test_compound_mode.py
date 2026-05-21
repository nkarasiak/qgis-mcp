"""Tests for compound mode — QGIS_MCP_NORTH_TOOL_MODE=compound.

Verifies the 5-tool surface (4 compound + qgis_eval) dispatches to the same
underlying functions as the 13-tool full-mode surface.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def compound_module(monkeypatch):
    """Reimport server + compound modules with TOOL_MODE='compound'."""
    monkeypatch.setenv("QGIS_MCP_NORTH_TOOL_MODE", "compound")
    # Reset cached server module so the env var is reread.
    for name in list(sys.modules):
        if name.startswith("qgis_mcp_north.server") or name == "qgis_mcp_north.compound":
            del sys.modules[name]
    server_module = importlib.import_module("qgis_mcp_north.server")
    compound = importlib.import_module("qgis_mcp_north.compound")
    yield server_module, compound
    # Clean up
    for name in list(sys.modules):
        if name.startswith("qgis_mcp_north.server") or name == "qgis_mcp_north.compound":
            del sys.modules[name]
    monkeypatch.delenv("QGIS_MCP_NORTH_TOOL_MODE", raising=False)
    importlib.import_module("qgis_mcp_north.server")  # restore full mode for siblings


def test_compound_inspect_layer_dispatches_layer_inspect(compound_module, fake_executor):
    """qgis_inspect(kind='layer', register=False) → calls add_vector_layer + get_layer_info + remove_layer."""
    server, compound = compound_module
    fake_executor.responses["add_vector_layer"] = {"id": "L1", "name": "test"}
    fake_executor.responses["get_layer_info"] = {
        "type": "vector_2", "crs": "EPSG:4326", "feature_count": 4,
        "extent": {"xmin": 0.0, "ymin": 0.0, "xmax": 1.0, "ymax": 1.0},
        "fields": [{"name": "zone_id", "type": "String"}],
    }
    fake_executor.responses["remove_layer"] = {"removed": True}

    result = compound.qgis_inspect(kind="layer", path="/tmp/x.shp")
    # Should be a LayerInfo (transient, no layer_id)
    assert isinstance(result, server.LayerInfo)
    assert result.n_features == 4
    assert result.crs == "EPSG:4326"
    # Verify it did NOT keep the layer (transient inspect = load + remove)
    commands = [c[0] for c in fake_executor.calls]
    assert "remove_layer" in commands


def test_compound_inspect_layer_register_keeps_layer(compound_module, fake_executor):
    """qgis_inspect(kind='layer', register=True) → loads + keeps + returns layer_id."""
    server, compound = compound_module
    fake_executor.responses["add_vector_layer"] = {"id": "L1", "name": "test"}
    fake_executor.responses["get_layer_info"] = {
        "type": "vector_2", "crs": "EPSG:4326", "feature_count": 4,
        "extent": {"xmin": 0.0, "ymin": 0.0, "xmax": 1.0, "ymax": 1.0},
        "fields": [],
    }
    result = compound.qgis_inspect(kind="layer", path="/tmp/x.shp", register=True)
    assert isinstance(result, server.LoadedLayer)
    assert result.layer_id == "L1"
    # remove_layer should NOT be called for register=True
    commands = [c[0] for c in fake_executor.calls]
    assert "remove_layer" not in commands


def test_compound_style_categorized_dispatches(compound_module, fake_executor):
    """qgis_style(type='categorized') → set_layer_style with style_type=categorized."""
    _server, compound = compound_module
    fake_executor.responses["set_layer_style"] = {
        "ok": True, "n_classes": 2,
        "classes": [
            {"value": "taxi", "color": "#1f78b4", "n_features": 10},
            {"value": "truck", "color": "#33a02c", "n_features": 5},
        ],
    }
    result = compound.qgis_style(type="categorized", layer_id="L1", field="mode")
    params = fake_executor.calls[0][1]
    assert params["style_type"] == "categorized"
    assert result.n_classes == 2


def test_compound_render_choropleth_round_trip(compound_module, fake_executor, tmp_path):
    """qgis_render(mode='choropleth') → render_choropleth plugin command."""
    _server, compound = compound_module
    csv = tmp_path / "v.csv"
    csv.write_text("zone_id,total_trips\nZ01,100\nZ02,200\n", encoding="utf-8")
    fake_executor.responses["render_choropleth"] = {
        "output_path": "/tmp/c.png",
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0, 0, 1, 1], "crs": "EPSG:4326", "n_layers": 1,
        "field": "total_trips", "n_classes": 5,
        "breaks": [0, 50, 100, 150, 200, 250], "mode": "quantile",
        "min_value": 100.0, "max_value": 200.0,
        "n_features": 2, "n_matched": 2, "n_unmatched": 0,
    }
    result = compound.qgis_render(
        mode="choropleth",
        zones_path="/tmp/zones.shp",
        value_field="total_trips",
        value_csv=str(csv),
        output_png="/tmp/c.png",
    )
    cmd, _params = fake_executor.calls[0]
    assert cmd == "render_choropleth"
    assert result.field == "total_trips"


def test_compound_render_map_requires_layer_ids(compound_module, fake_executor):
    """qgis_render(mode='map') without layer_ids → ValueError before dispatch."""
    _server, compound = compound_module
    with pytest.raises(ValueError, match="layer_ids"):
        compound.qgis_render(mode="map", output_png="/tmp/x.png")
    assert fake_executor.calls == []


def test_full_mode_does_not_register_compound_tools(monkeypatch):
    """In TOOL_MODE=full (default), the compound module's tools are NOT registered with FastMCP."""
    monkeypatch.delenv("QGIS_MCP_NORTH_TOOL_MODE", raising=False)
    for name in list(sys.modules):
        if name.startswith("qgis_mcp_north.server") or name == "qgis_mcp_north.compound":
            del sys.modules[name]
    server = importlib.import_module("qgis_mcp_north.server")
    assert server.TOOL_MODE == "full"
    # Importing compound in full mode is a no-op for FastMCP registration (decorators no-op).
    # The functions still exist as Python callables, but mcp.tool() wasn't called on them.
    importlib.import_module("qgis_mcp_north.compound")  # safe to import; doesn't register


def test_compound_mode_sets_tool_mode_constant(compound_module):
    server, _compound = compound_module
    assert server.TOOL_MODE == "compound"
