"""Tests for qgis_layer_inspect — strict TDD against a mocked plugin executor."""

from __future__ import annotations

import os

import pytest

from qgis_mcp_workflows.errors import ExecutorError, LayerNotFoundError
from qgis_mcp_workflows.server import qgis_layer_inspect


def test_vector_shapefile_returns_layer_info(fake_executor):
    """The full happy path for a polygon shapefile: load → info → remove."""
    fake_executor.responses["add_vector_layer"] = {
        "id": "L_polbnda",
        "name": "polbnda_jpn_new",
        "type": "vector_2",
        "feature_count": 47,
    }
    fake_executor.responses["get_layer_info"] = {
        "id": "L_polbnda",
        "name": "polbnda_jpn_new",
        "type": "vector_2",
        "crs": "EPSG:4326",
        "extent": {"xmin": 122.9, "ymin": 24.0, "xmax": 153.99, "ymax": 45.55},
        "feature_count": 47,
        "geometry_type": 2,
        "fields": [
            {"name": "nam", "type": "String", "length": 80},
            {"name": "nam_ja", "type": "String", "length": 80},
        ],
        "is_valid": True,
        "source": "/data/polbnda_jpn_new.shp",
        "provider": "ogr",
    }
    fake_executor.responses["remove_layer"] = {"ok": True}

    input_path = "/data/polbnda_jpn_new.shp"
    result = qgis_layer_inspect(input_path)

    assert result.path == os.path.abspath(input_path)  # contract: tool returns absolute path
    assert result.geometry_type == "polygon"
    assert result.crs == "EPSG:4326"
    assert result.n_features == 47
    assert result.extent == [122.9, 24.0, 153.99, 45.55]
    assert [f.name for f in result.fields] == ["nam", "nam_ja"]
    assert result.fields[0].type == "String"

    commands = [c[0] for c in fake_executor.calls]
    assert commands == ["add_vector_layer", "get_layer_info", "remove_layer"]


def test_raster_geotiff_dispatches_raster_loader(fake_executor):
    """A .tif extension routes through add_raster_layer, not add_vector_layer."""
    fake_executor.responses["add_raster_layer"] = {
        "id": "L_jaxa",
        "name": "2024JPN_v25.04_100m",
        "type": "raster",
        "width": 8000,
        "height": 12000,
    }
    fake_executor.responses["get_layer_info"] = {
        "id": "L_jaxa",
        "name": "2024JPN_v25.04_100m",
        "type": "raster",
        "crs": "EPSG:4326",
        "extent": {"xmin": 122.9, "ymin": 24.0, "xmax": 153.99, "ymax": 45.55},
        "is_valid": True,
        "source": "/data/2024JPN_v25.04_100m.tif",
        "provider": "gdal",
        "width": 8000,
        "height": 12000,
        "band_count": 1,
    }
    fake_executor.responses["remove_layer"] = {"ok": True}

    result = qgis_layer_inspect("/data/2024JPN_v25.04_100m.tif")

    assert result.geometry_type == "raster"
    assert result.n_features == 0  # rasters: convention
    assert result.fields == []
    commands = [c[0] for c in fake_executor.calls]
    assert commands == ["add_raster_layer", "get_layer_info", "remove_layer"]


def test_remove_layer_runs_even_if_get_info_fails(fake_executor):
    """Cleanup discipline: a transient layer must always be removed."""
    fake_executor.responses["add_vector_layer"] = {
        "id": "L_x", "name": "x", "type": "vector_2", "feature_count": 0,
    }

    def raise_executor_error(_params):
        raise ExecutorError("get_layer_info", "kaboom")

    fake_executor.responses["get_layer_info"] = raise_executor_error
    fake_executor.responses["remove_layer"] = {"ok": True}

    with pytest.raises(ExecutorError):
        qgis_layer_inspect("/data/foo.shp")

    commands = [c[0] for c in fake_executor.calls]
    assert commands == ["add_vector_layer", "get_layer_info", "remove_layer"], (
        "remove_layer must run in finally even when get_layer_info raises"
    )


def test_layer_not_found_propagates(fake_executor):
    """If the plugin can't open the file, the typed error escapes — no remove."""
    def raise_not_found(_params):
        raise LayerNotFoundError("/missing/path.shp")

    fake_executor.responses["add_vector_layer"] = raise_not_found

    with pytest.raises(LayerNotFoundError):
        qgis_layer_inspect("/missing/path.shp")

    commands = [c[0] for c in fake_executor.calls]
    assert commands == ["add_vector_layer"], "no get_layer_info, no remove_layer if load failed"


def test_geometry_type_translation(fake_executor):
    """Plugin's vector_{0,1,2} map to point/line/polygon."""
    cases = [("vector_0", "point"), ("vector_1", "line"), ("vector_2", "polygon")]
    for plugin_type, expected in cases:
        fake_executor.calls.clear()
        fake_executor.responses["add_vector_layer"] = {
            "id": "L", "name": "x", "type": plugin_type, "feature_count": 1,
        }
        fake_executor.responses["get_layer_info"] = {
            "id": "L", "name": "x", "type": plugin_type, "crs": "EPSG:4326",
            "extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1},
            "feature_count": 1, "geometry_type": int(plugin_type.split("_")[1]),
            "fields": [], "is_valid": True, "source": "x.shp", "provider": "ogr",
        }
        fake_executor.responses["remove_layer"] = {"ok": True}
        result = qgis_layer_inspect("/x.shp")
        assert result.geometry_type == expected, f"{plugin_type} should map to {expected}"
